"""Keep pre-edit diagnostic evidence separate from findings after goal edits."""

from __future__ import annotations

from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path

from diagnostics.backends import typescript_baseline
from mutation_runtime import EXPLICIT_MUTATION_TYPES
from repository_changes import RepositorySnapshot


_FILE_EDITS = EXPLICIT_MUTATION_TYPES | {"write_file", "patch_file", "replace_lines", "git_restore", "git_rm", "git_mv"}
_OPAQUE_EDITS = {"run_shell", "npm_command", "approve_npm_command", "mcp", "run_check"}
_TS_PATHS = {".ts", ".tsx", ".js", ".jsx", ".mts", ".mjs", ".cts", ".cjs"}


class DiagnosticProvenance:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.baseline: dict[tuple[str, str, str, str], set[int]] = {}
        self.original_sources: dict[str, bytes | None] = {}
        self.changed_paths: set[str] = set()
        self.edit_in_flight = False

    def _path(self, value: str) -> str:
        if not value:
            return ""
        try:
            return (self.root / value.removeprefix("/repo/").removeprefix("repo/")).resolve().relative_to(self.root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return ""

    def _identity(self, issue: dict) -> tuple[tuple[str, str, str, str], int]:
        reported_path = str(issue.get("file") or issue.get("path") or issue.get("resource") or "")
        path = self._path(reported_path) or reported_path
        code = str(issue.get("code") or issue.get("classification") or "")
        message = " ".join(str(issue.get("message") or "").split())
        column = str(issue.get("column") or issue.get("startColumn") or "")
        line = str(issue.get("line") or issue.get("startLineNumber") or "")
        return (path, code, message, column), int(line) if line.isdecimal() else 0

    def observe(self, issues: list[dict]) -> None:
        if self.changed_paths or self.edit_in_flight:
            return
        for issue in issues:
            if not isinstance(issue, dict) or issue.get("status") == "resolved":
                continue
            key, line = self._identity(issue)
            self.baseline.setdefault(key, set()).add(line)

    def _read(self, path: str) -> bytes | None:
        try:
            target = self.root / path
            target.resolve().relative_to(self.root)
            return target.read_bytes()
        except (OSError, RuntimeError, ValueError):
            return None

    def _capture_compiler_baseline(self, paths: set[str]) -> None:
        for path in sorted(paths - self.original_sources.keys()):
            if Path(path).suffix == ".py":
                # Python syntax checks are file-local, even after edits elsewhere.
                content = self._read(path)
                if content is None:
                    continue
                try:
                    compile(content, path, "exec")
                except SyntaxError as exc:
                    key = (path, "PY_COMPILE", f"{type(exc).__name__}: {exc.msg}", "1")
                    self.baseline.setdefault(key, set()).add(exc.lineno or 0)
        if self.changed_paths:
            return
        ts_target = next((path for path in sorted(paths) if Path(path).suffix.lower() in _TS_PATHS), "")
        if ts_target:
            try:
                self.observe(typescript_baseline(self.root, ts_target))
            except (OSError, ValueError):
                # An unavailable checker supplies no baseline evidence.
                pass

    def _action_paths(self, action: dict) -> set[str]:
        operations = action.get("operations") if action.get("type") == "batch_mutate" else [action]
        if not isinstance(operations, list):
            return set()
        paths = set()
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            for key in ("path", "file_path", "paths", "old_path", "new_path", "source_path", "destination_path", "source", "destination"):
                value = operation.get(key)
                values = value if isinstance(value, list) else [value]
                for item in values:
                    if not isinstance(item, str) or not item.strip():
                        continue
                    if path := self._path(item):
                        paths.add(path)
        return paths

    @contextmanager
    def track(self, action: dict, *, shell_tracks_changes: bool = False):
        kind = str(action.get("type") or "")
        opaque = kind in _OPAQUE_EDITS and (kind != "mcp" or action.get("operation") == "call")
        if kind not in _FILE_EDITS and not opaque:
            yield
            return
        paths = self._action_paths(action) if kind in _FILE_EDITS else set()
        self._capture_compiler_baseline(paths)
        before = {path: self._read(path) for path in paths}
        for path, content in before.items():
            self.original_sources.setdefault(path, content)
        host_accounting = shell_tracks_changes and kind == "run_shell"
        snapshot = RepositorySnapshot.capture(self.root) if opaque and not host_accounting else None
        self.edit_in_flight = True
        try:
            yield
        finally:
            self.edit_in_flight = False
            self.changed_paths.update(path for path, content in before.items() if self._read(path) != content)
            if shell_tracks_changes:
                self.changed_paths.update(path for path in action.get("_changed_paths", []) if not path.endswith(".tsbuildinfo"))
            if snapshot is not None:
                paths = RepositorySnapshot.capture(self.root, previous=snapshot).changed_since(snapshot)
                self.changed_paths.update(path for path in paths if not path.endswith(".tsbuildinfo"))

    def may_defer(self, issue: dict) -> bool:
        if not self.changed_paths and not self.edit_in_flight:
            return True
        key, line = self._identity(issue)
        previous = self.baseline.get(key)
        if not previous:
            return False
        path = key[0]
        if path not in self.original_sources:
            # Opaque writes supply changed paths, but may lack pre-edit source.
            return path not in self.changed_paths and line in previous
        old, current = self.original_sources[path], self._read(path)
        if old == current:
            return line in previous
        if old is None or current is None or not line:
            return False
        unchanged = SequenceMatcher(None, old.splitlines(), current.splitlines()).get_matching_blocks()
        return any(new < line <= new + size and start + line - new in previous for start, new, size in unchanged)
