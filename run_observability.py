"""Immutable per-attempt reports alongside the compact live observability file."""

from __future__ import annotations

import hashlib
from pathlib import Path


def archive_snapshot(path: Path, content: str | None = None) -> Path | None:
    try:
        text = content if content is not None else path.read_text(encoding="utf-8")
        if not text.strip():
            return None
        archive = path.parent / ".context" / "observability"
        archive.mkdir(parents=True, exist_ok=True)
        target = archive / (hashlib.sha256(text.encode("utf-8")).hexdigest()[:24] + ".md")
        try:
            with target.open("x", encoding="utf-8") as stream:
                stream.write(text)
        except FileExistsError:
            pass
        return target
    except OSError:
        return None


def clear_archives(path: Path) -> None:
    archive = path.parent / ".context" / "observability"
    for report in archive.glob("*.md"):
        if len(report.stem) == 24 and all(char in "0123456789abcdef" for char in report.stem):
            try:
                report.unlink(missing_ok=True)
            except OSError:
                continue
