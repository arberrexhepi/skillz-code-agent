"""Read-only discovery dispatch for the beta command bridge."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

from . import suite
from .common import read_text, safe_join


DISCOVERY_ACTION_TYPES = frozenset({
    "list_files", "find_files", "search_in_files", "read_file", "outline_file", "read_symbol",
    "find_symbol_definitions", "find_symbol_references", "trace_dependencies",
    "find_related_files", "find_related_tests", "find_related_configs",
    "find_canonical_implementation", "find_similar_code", "find_entry_points",
    "find_ownership", "recent_changes", "get_changed_files", "semantic_search",
    "repo_map", "investigate",
})


def _read_file(
    file_path: str, *, start_line: int = 1, end_line: int = 0, root: Path,
) -> dict[str, Any]:
    target = safe_join(root.resolve(), file_path)
    lines = read_text(target).splitlines()
    start = max(1, int(start_line))
    end = min(len(lines), int(end_line) if end_line else start + 199)
    return {
        "ok": True,
        "file_path": str(target.relative_to(root.resolve())),
        "line_count": len(lines), "start_line": start, "end_line": end,
        "truncated": end < len(lines),
        "content": "\n".join(lines[start - 1:end]),
    }


def execute_discovery_action(action: dict[str, Any], *, root: Path) -> dict[str, Any]:
    """Dispatch only known read operations with the host-owned workspace root."""
    action_type = str(action.get("type", "") or "")
    try:
        if action_type not in DISCOVERY_ACTION_TYPES:
            raise ValueError(f"Unsupported read-only discovery action: {action_type}")
        if "root" in action:
            raise ValueError("Discovery cannot override the mounted workspace root")
        handler = _read_file if action_type == "read_file" else getattr(suite, action_type)
        parameters = inspect.signature(handler).parameters
        kwargs = {key: value for key, value in action.items() if key != "type"}
        if "hidden" in kwargs and "include_hidden" in parameters:
            kwargs["include_hidden"] = kwargs.pop("hidden")
        if action_type == "find_files" and "glob" in kwargs:
            kwargs["patterns"] = [kwargs.pop("glob")]
        if action_type in {"find_related_tests", "find_related_configs", "find_ownership"} and "target" not in kwargs and "path" in kwargs:
            kwargs["target"] = kwargs.pop("path")
        if "file_path" in parameters and "path" not in parameters and "path" in kwargs:
            kwargs["file_path"] = kwargs.pop("path")
        for key in ("path", "file_path", "query_file", "target"):
            value = kwargs.get(key)
            if not isinstance(value, str):
                continue
            if value in {"/repo", "repo", "/repo/"}:
                value = "."
            elif value.startswith("/repo/"):
                value = value.removeprefix("/repo/")
            elif value.startswith("repo/"):
                value = value.removeprefix("repo/")
            if Path(value).is_absolute():
                raise ValueError(f"{key} must be relative to /repo")
            if key != "target" or action_type == "find_ownership":
                safe_join(root.resolve(), value or ".")
            kwargs[key] = value
        if "limit" in kwargs:
            kwargs["limit"] = max(1, min(200, int(kwargs["limit"])))
        inspect.signature(handler).bind(**kwargs, root=root)
        return dict(handler(**kwargs, root=root))
    except (ValueError, TypeError, OSError, RuntimeError) as exc:
        return {"ok": False, "action": action_type, "error": str(exc)}
