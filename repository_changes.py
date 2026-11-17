"""Observe repository content changes around commands, including already-dirty files."""

from __future__ import annotations

import hashlib
import os
import subprocess
from subprocess import run as run_git
from dataclasses import dataclass, field
from pathlib import Path


EXCLUDED_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
                 ".mypy_cache", ".next", "dist", "build", "coverage", ".agent_tmp"}


@dataclass
class RepositorySnapshot:
    files: dict[str, tuple[tuple[int, int, int, int], str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @classmethod
    def capture(cls, root: Path, previous: RepositorySnapshot | None = None) -> RepositorySnapshot:
        snapshot = cls()
        try:
            listing = run_git(
                ["git", "ls-files", "-c", "-o", "--exclude-standard", "-z"], cwd=root,
                capture_output=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            listing = None
        if listing is not None and listing.returncode == 0:
            paths = sorted(set(os.fsdecode(item) for item in listing.stdout.split(b"\0") if item))
        else:
            paths = []
            for directory, dirs, files in os.walk(root, onerror=lambda exc: snapshot.errors.append(str(exc))):
                dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS
                           and Path(directory) / name != root / ".context" / "observability"
                           and not (Path(directory) / name).is_symlink()]
                paths.extend((Path(directory) / name).relative_to(root).as_posix() for name in files)
        for relative in paths:
            path = Path(relative)
            if (any(part in EXCLUDED_DIRS for part in path.parts)
                    or path.parts[:2] == (".context", "observability")):
                continue
            target = root / path
            try:
                target.resolve().relative_to(root.resolve())
                if not target.is_file() or target.is_symlink():
                    continue
                info = target.stat()
                signature = (info.st_mtime_ns, info.st_ctime_ns, info.st_size, info.st_ino)
                prior = previous.files.get(relative) if previous is not None else None
                if prior is not None and prior[0] == signature:
                    digest = prior[1]
                else:
                    with target.open("rb") as stream:
                        digest = hashlib.file_digest(stream, "sha256").hexdigest()
                snapshot.files[relative] = (signature, digest)
            except FileNotFoundError:
                continue
            except (OSError, ValueError) as exc:
                snapshot.errors.append(f"{relative}: {exc}")
        return snapshot

    def changed_since(self, previous: RepositorySnapshot) -> list[str]:
        def digest(snapshot: RepositorySnapshot, path: str) -> str | None:
            entry = snapshot.files.get(path)
            return entry[1] if entry else None
        return sorted(path for path in self.files.keys() | previous.files.keys()
                      if digest(self, path) != digest(previous, path))
