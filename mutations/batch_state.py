"""Batch-start preconditions, internally advanced versions, and safe rollback."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any

from .common import expected_hash_mismatch, file_missing_result, safe_join
from .models import MutationResult, make_mutation_result


def operation_paths(operation: dict[str, Any]) -> list[str]:
    paths = []
    for key in ("file_path", "path", "old_path", "new_path", "source_path", "destination_path"):
        value = operation.get(key)
        if value is not None and not isinstance(value, str):
            raise ValueError(f"Mutation path {key} must be a string")
        if value and value.strip() and value.strip() not in paths:
            paths.append(value.strip())
    return paths


def _primary_path(operation: dict[str, Any]) -> str | None:
    name = operation.get("file_path") or operation.get("path")
    return name.strip() if isinstance(name, str) else None


@dataclass(frozen=True)
class FileState:
    kind: str
    data: bytes = b""

    @classmethod
    def read(cls, path: Path) -> FileState:
        try:
            if path.is_file():
                return cls("file", path.read_bytes())
            return cls("directory" if path.is_dir() else "other" if path.exists() else "missing")
        except FileNotFoundError:
            return cls("missing")

    def digest(self, *, normalized: bool = False) -> str:
        data = self.data
        if normalized:
            data = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
        return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class ExpectedState:
    kind: str
    digest: str = ""
    allow_normalized: bool = False

    @classmethod
    def exact(cls, state: FileState) -> ExpectedState:
        return cls(state.kind, state.digest() if state.kind == "file" else "")

    def matches(self, actual: FileState) -> bool:
        if self.kind != actual.kind:
            return False
        if self.kind != "file" or self.digest == actual.digest():
            return True
        try:
            return self.allow_normalized and self.digest == actual.digest(normalized=True)
        except UnicodeError:
            return False


class BatchState:
    def __init__(self, root: Path, operations: list[dict[str, Any]], hash_operations: set[str]):
        self.root = root
        self.hash_operations = hash_operations
        self.paths: dict[str, Path] = {}
        # Resolve all paths before reading any file or applying any operation.
        for operation in operations:
            if not isinstance(operation, dict):
                raise ValueError("Every mutation operation must be an object")
            for name in operation_paths(operation):
                self.paths[name] = safe_join(root, name)
        self.snapshots = {path: FileState.read(path) for path in self.paths.values()}
        self.contents = dict(self.snapshots)
        self.versions = {path: ExpectedState.exact(state) for path, state in self.snapshots.items()}
        self.written: dict[Path, ExpectedState] = {}

    def validate_hashes(self, operations: list[dict[str, Any]]) -> MutationResult | None:
        for index, operation in enumerate(operations):
            kind = str(operation.get("type", "")).strip()
            declared = operation.get("expected_hash")
            if kind not in self.hash_operations or not declared:
                continue
            name = _primary_path(operation)
            if not isinstance(name, str) or name not in self.paths:
                continue  # The handler reports malformed operation arguments.
            state = self.snapshots[self.paths[name]]
            if state.kind != "file":
                result = file_missing_result(f"batch:{index}:{kind}", name, kind)
            else:
                actual = state.digest(normalized=kind != "replace_regex")
                if declared == actual:
                    continue
                result = expected_hash_mismatch(name, kind, str(declared), actual)
            result["details"].update(operation_index=index + 1, phase="batch_preflight", hash_scope="batch_start")
            return result
        return None

    def guard(self, operation: dict[str, Any], index: int, *, phase: str = "before_operation") -> MutationResult | None:
        for name in operation_paths(operation):
            try:
                target = safe_join(self.root, name)
                actual = FileState.read(target) if target == self.paths[name] else None
            except (ValueError, OSError, RuntimeError):
                return self._failure(name, index, phase, "File state became unavailable or the path escaped the workspace.")
            if target != self.paths[name]:
                return self._failure(name, index, phase, "Path resolution changed outside the batch.")
            expected = self.versions[target]
            if not expected.matches(actual):
                return self._failure(name, index, phase, "File changed outside the batch.", expected, actual)
        return None

    def prepare(self, operation: dict[str, Any]) -> dict[str, Any]:
        prepared = dict(operation)
        kind = str(operation.get("type", "")).strip()
        name = _primary_path(operation)
        if kind in self.hash_operations and isinstance(name, str) and name in self.paths:
            current = self.contents[self.paths[name]]
            if current.kind == "file":
                # Keep the original declaration on the input; dispatch a guard
                # for the latest version produced by this batch instead.
                prepared["expected_hash"] = current.digest(normalized=kind != "replace_regex")
        return prepared

    def record(self, operation: dict[str, Any], result: MutationResult, index: int) -> MutationResult | None:
        result.setdefault("details", {})["operation_index"] = index + 1
        if operation.get("expected_hash"):
            result["details"]["batch_start_expected_hash"] = operation["expected_hash"]
        if not result.get("applied"):
            return self.guard(operation, index, phase="after_operation") if result.get("ok") else None
        kind = str(operation.get("type", "")).strip()
        paths = operation_paths(operation)
        writes = [operation["destination_path"].strip()] if kind == "copy_file" else paths
        failure = None
        for name in writes:
            target = self.paths[name]
            removed = kind == "delete_file" or (kind == "rename_file" and name == str(operation.get("old_path", "")).strip())
            expected = ExpectedState("missing") if removed else ExpectedState("file", str(result.get("after_hash") or ""), kind != "replace_regex")
            try:
                actual = FileState.read(target) if safe_join(self.root, name) == target else None
            except (ValueError, OSError, RuntimeError):
                actual = None
            matches = actual is not None and expected.matches(actual)
            # A handler's result identifies its own output. Never adopt a
            # concurrent editor write observed after the handler as our version.
            own_version = ExpectedState.exact(actual) if matches else expected
            self.versions[target] = own_version
            self.written[target] = own_version
            if matches:
                self.contents[target] = actual
            if not matches and failure is None:
                failure = self._failure(name, index, "after_operation", "File changed after the batch wrote it.", expected, actual)
        return failure or self.guard(operation, index, phase="after_operation")

    def rollback(self) -> list[Path]:
        conflicts = []
        for target, expected in self.written.items():
            try:
                current = FileState.read(target) if target.resolve() == target else None
            except (ValueError, OSError, RuntimeError):
                current = None
            if current is None or not expected.matches(current):
                conflicts.append(target)
                continue
            original = self.snapshots[target]
            try:
                if original.kind == "file":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(original.data)
                elif original.kind == "missing" and target.is_file():
                    target.unlink()
            except OSError:
                conflicts.append(target)
        return conflicts

    def _failure(self, name: str, index: int, phase: str, message: str,
                 expected: ExpectedState | None = None, actual: FileState | None = None) -> MutationResult:
        return make_mutation_result(
            operation_id=f"batch:{index}:guard", file_path=name, mutation_type="batch_mutate",
            ok=False, applied=False, reason="concurrent_file_change", drift_detected=True,
            before_hash=actual.digest() if actual and actual.kind == "file" else None,
            diagnostics=[{"code": "BATCH_FILE_CHANGED", "message": f"Operation {index + 1}, {name}: {message} Re-read this file before retrying."}],
            details={"operation_index": index + 1, "phase": phase, "expected_hash": expected.digest if expected else None},
        )
