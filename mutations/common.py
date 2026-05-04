from __future__ import annotations

import difflib
import hashlib
import os
from pathlib import Path
from typing import Iterable, Optional

from .models import MutationResult, make_mutation_result


def resolve_root(root: Path | str | None = None) -> Path:
    return Path(root or Path.cwd()).resolve()


def normalize_relative_path(root: Path, file_path: str) -> str:
    value = str(file_path or "").strip()
    if not value:
        return ""
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            return str(candidate.resolve().relative_to(root.resolve())).replace(os.sep, "/")
        except Exception:
            return str(candidate).replace(os.sep, "/")
    return value.replace(os.sep, "/")


def safe_join(root: Path, file_path: str) -> Path:
    candidate = (root / file_path).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {file_path}") from exc
    return candidate


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def line_delta(before: str, after: str) -> int:
    diff = difflib.unified_diff(
        before.splitlines(),
        after.splitlines(),
        lineterm="",
    )
    changed = 0
    for line in diff:
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith(("+", "-")):
            changed += 1
    return changed


def unified_diff_text(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def ensure_trailing_newline_like(original: str, updated: str) -> str:
    if original.endswith("\n") and updated and not updated.endswith("\n"):
        return updated + "\n"
    return updated


def already_present_adjacent(original: str, anchor: str, new_text: str, *, position: str) -> bool:
    if not anchor or not new_text:
        return False
    if position == "before":
        return f"{new_text}{anchor}" in original
    return f"{anchor}{new_text}" in original


def file_missing_result(operation_id: str, file_path: str, mutation_type: str) -> MutationResult:
    return make_mutation_result(
        operation_id=operation_id,
        file_path=file_path,
        mutation_type=mutation_type,
        ok=False,
        applied=False,
        reason="file_not_found",
        diagnostics=[{"code": "FILE_NOT_FOUND", "message": f"File not found: {file_path}"}],
        preconditions={"file_exists": False},
    )


def expected_hash_mismatch(path: str, mutation_type: str, expected_hash: str, actual_hash: str) -> MutationResult:
    return make_mutation_result(
        operation_id=f"{mutation_type}:{path}",
        file_path=path,
        mutation_type=mutation_type,
        ok=False,
        applied=False,
        reason="expected_hash_mismatch",
        before_hash=actual_hash,
        drift_detected=True,
        diagnostics=[
            {
                "code": "EXPECTED_HASH_MISMATCH",
                "message": f"Expected hash {expected_hash[:12]} but found {actual_hash[:12]}.",
            }
        ],
        preconditions={"expected_hash": expected_hash},
    )


def with_expected_hash(root: Path, path: str, file_text: str, mutation_type: str, expected_hash: Optional[str]) -> Optional[MutationResult]:
    if not expected_hash:
        return None
    actual_hash = sha256_text(file_text)
    if actual_hash == expected_hash:
        return None
    return expected_hash_mismatch(path, mutation_type, expected_hash, actual_hash)


def split_lines(text: str) -> list[str]:
    return text.splitlines()


def join_lines(lines: Iterable[str], *, trailing_newline: bool) -> str:
    updated = "\n".join(lines)
    if trailing_newline and updated and not updated.endswith("\n"):
        updated += "\n"
    return updated