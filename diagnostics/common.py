from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Optional


SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mypy_cache",
    ".pytest_cache",
    "dist",
    "build",
    "coverage",
}

TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".md",
    ".txt",
    ".sh",
    ".env",
}
JS_TS_SUFFIXES = {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
PYTHON_SUFFIXES = {".py"}
CONFIG_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".env"}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", str(text or ""))


def resolve_root(root: Optional[Path | str] = None) -> Path:
    if root is None:
        return Path.cwd().resolve()
    return Path(root).resolve()


def normalize_repo_relative_path(root: Path, raw_path: str) -> str:
    value = str(raw_path or "").strip()
    if not value:
        return ""
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            return str(candidate.resolve().relative_to(root.resolve())).replace(os.sep, "/")
        except Exception:
            return str(candidate).replace(os.sep, "/")
    return str(Path(value)).replace(os.sep, "/")


def normalize_input_paths(root: Path, paths: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for path in paths:
        rel = normalize_repo_relative_path(root, path)
        if rel and rel not in normalized:
            normalized.append(rel)
    return normalized


def existing_files(root: Path, paths: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for rel_path in normalize_input_paths(root, paths):
        candidate = (root / rel_path).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.exists() and candidate.is_file():
            files.append(candidate)
    return files


def iter_project_files(root: Path) -> list[str]:
    files: list[str] = []
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in SKIP_DIRS]
        current_path = Path(current_root)
        for filename in filenames:
            path = current_path / filename
            if path.suffix.lower() in TEXT_SUFFIXES or filename in {"package.json", "tsconfig.json", ".env"}:
                files.append(str(path.relative_to(root)).replace(os.sep, "/"))
    return sorted(files)


def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def try_import_yaml() -> Any | None:
    try:
        import yaml  # type: ignore
    except Exception:
        return None
    return yaml


def run_command(command: list[str], *, cwd: Path, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=subprocess_env_no_color(),
    )


def subprocess_env_no_color() -> dict[str, str]:
    env = dict(os.environ)
    env.setdefault("NO_COLOR", "1")
    env.setdefault("FORCE_COLOR", "0")
    env.setdefault("CLICOLOR", "0")
    env.setdefault("TERM", "dumb")
    return env


def find_command(name: str, *, root: Path, start_dir: Optional[Path] = None) -> Optional[str]:
    current = (start_dir or root).resolve()
    root = root.resolve()
    while True:
        candidate = current / "node_modules" / ".bin" / name
        if candidate.exists() and candidate.is_file():
            return str(candidate)
        if current == root or root not in current.parents:
            break
        current = current.parent
    return shutil.which(name)


def detect_changed_files(root: Path) -> list[str]:
    if not (root / ".git").exists():
        return []
    try:
        result = run_command(["git", "status", "--short", "--untracked-files=all"], cwd=root, timeout=15)
    except Exception:
        return []
    if result.returncode != 0:
        return []
    changed: list[str] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.rstrip()
        if len(line) < 4:
            continue
        candidate = line[3:]
        if " -> " in candidate:
            candidate = candidate.split(" -> ", 1)[1]
        candidate = candidate.strip()
        if candidate and candidate not in changed:
            changed.append(candidate)
    return changed


def load_json_file(path: Path) -> Any:
    return json.loads(read_text_file(path))


def safe_tail(text: str, *, max_lines: int = 20) -> str:
    lines = strip_ansi(text).splitlines()
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[-max_lines:])


def combine_command_output(stdout: str, stderr: str) -> str:
    parts = [part.strip("\n") for part in [stdout, stderr] if str(part or "").strip()]
    return "\n".join(parts)


def command_failure_message(stdout: str, stderr: str, *, fallback: str, max_lines: int = 12) -> str:
    tail = safe_tail(combine_command_output(stdout, stderr), max_lines=max_lines).strip()
    return tail or fallback