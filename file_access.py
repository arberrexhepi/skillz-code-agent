"""Explicit read-only scopes for structured agent reads. Mutations never use this resolver.

Process isolation (Docker for artifact sessions) enforces shell/validation access.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any


def read_scope(root: Path, value: str) -> tuple[Path, str]:
    normalized = value.replace("\\", "/")
    grants = json.loads(os.environ.get("SKILLZ_READ_ROOTS", "[]"))
    if not isinstance(grants, list):
        raise ValueError("Invalid read-only folder grants")
    if os.environ.get("SKILLZ_CONTEXT_ROOT"):
        grants = [*grants, {"path": os.environ["SKILLZ_CONTEXT_ROOT"]}]
    for grant in grants:
        raw = str(grant.get("path", ""))
        base = Path(raw)
        if not raw or not base.is_absolute():
            raise ValueError("Read-only folder grants must be absolute")
        prefix = raw.replace("\\", "/").rstrip("/")
        if normalized == prefix or normalized.startswith(prefix + "/"):
            canonical = base.resolve(strict=True)
            if canonical != base.absolute():
                raise ValueError("Shared folder changed; select it again")
            relative = normalized[len(prefix):].lstrip("/") or "."
            target = (canonical / relative).resolve()
            if not target.is_relative_to(canonical):
                raise ValueError("Path escapes the allowed read-only folder")
            return canonical, relative
    if normalized == "/repo":
        normalized = "."
    elif normalized.startswith("/repo/"):
        normalized = normalized[6:]
    target = (root / normalized).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError("Path is outside the opened repository and allowed read-only folders")
    return root, normalized


def qualify_read_paths(value: Any, root: Path) -> Any:
    if isinstance(value, list):
        return [qualify_read_paths(item, root) for item in value]
    if isinstance(value, dict):
        return {key: str(root / item).replace("\\", "/") if key in {"path", "file_path"} and isinstance(item, str) and not Path(item).is_absolute() else qualify_read_paths(item, root) for key, item in value.items()}
    return value
