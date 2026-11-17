"""Bounded source inspection with stable hashes and merged context windows."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .common import read_text, safe_join
from .suite import outline_file


def inspect_file(file_path: str, *, limit: int = 80, root: Path) -> dict:
    outline = dict(outline_file(file_path, root=root))
    text = read_text(safe_join(root.resolve(), file_path))
    outline["file_hash"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    limit = max(1, min(200, int(limit)))
    outline["truncated"] = any(len(outline[key]) > limit for key in ("imports", "exports", "symbols", "constants", "sections"))
    for key in ("imports", "exports", "symbols", "constants", "sections"):
        outline[key] = outline[key][:limit]
    while len(json.dumps(outline)) > 24000:
        key = max((key for key in ("imports", "exports", "symbols", "constants", "sections") if outline[key]),
                  key=lambda key: len(json.dumps(outline[key])), default=None)
        if key is None:
            break
        outline[key].pop()
        outline["truncated"] = True
    return outline


def read_context(file_path: str, query: str, *, before: int = 8, after: int = 12, limit: int = 8, root: Path) -> dict:
    if not query or "\n" in query:
        raise ValueError("read-context requires a nonempty, single-line literal search")
    text = read_text(safe_join(root.resolve(), file_path))
    lines = text.splitlines()
    before, after, limit = max(0, min(80, before)), max(0, min(80, after)), max(1, min(20, limit))
    matches = [index for index, line in enumerate(lines) if query in line]
    windows: list[list[int]] = []
    for index in matches[:limit]:
        start, end = max(0, index - before), min(len(lines), index + after + 1)
        if windows and start <= windows[-1][1]:
            windows[-1][1] = max(windows[-1][1], end)
        else:
            windows.append([start, end])
    blocks = []
    remaining_lines, remaining_chars = 300, 24000
    truncated = len(matches) > limit
    for start, end in windows:
        selected = []
        for line in lines[start:end]:
            if remaining_lines <= 0 or len(line) + 1 > remaining_chars:
                truncated = True
                break
            selected.append(line)
            remaining_lines -= 1
            remaining_chars -= len(line) + 1
        if selected:
            blocks.append({"start_line": start + 1, "end_line": start + len(selected), "content": "\n".join(selected)})
        if len(selected) < end - start:
            break
    return {"ok": True, "file_path": file_path, "file_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "line_count": len(lines), "match_count": len(matches), "blocks": blocks, "truncated": truncated}
