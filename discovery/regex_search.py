"""Regex search and replacement previews, scoped by the discovery dispatcher."""

from pathlib import Path

from .common import safe_join
from regex_runtime import evaluate_regex, read_source, source_hash


def find_regex(file_path: str, pattern: str, *, replacement: str | None = None, flags: str = "", limit: int = 20,
               offset: int = 0, before: int = 2, after: int = 2, root: Path) -> dict:
    target = safe_join(root.resolve(), file_path)
    text = read_source(target)
    result = evaluate_regex(text, pattern=pattern, replacement=replacement, flags=flags, limit=limit,
                            offset=offset, before=before, after=after, file_path=file_path)
    result.pop("updated", None)
    return {**result, "file_path": file_path, "file_hash": source_hash(text)}
