from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Iterable, Iterator, Optional, Sequence

MAX_READ_BYTES = 1_000_000
MAX_SEARCH_PREVIEW = 240
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache", "dist", "build"}
CONFIG_FILENAMES = {
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "requirements.txt",
    "package.json",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "tsconfig.json",
    "tsconfig.test.json",
    "tsconfig.integration.json",
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".prettierrc",
    "vite.config.ts",
    "vite.config.js",
    "pytest.ini",
    ".github/workflows",
}


def resolve_root(root: Path | str | None = None) -> Path:
    return Path(root or Path.cwd()).expanduser().resolve()


def safe_join(root: Path, rel_path: str) -> Path:
    normalized = str(rel_path or "").strip()
    if not normalized:
        raise ValueError("Path cannot be empty")
    candidate = (root / normalized).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"Path escapes root: {rel_path}") from exc
    return candidate


def normalize_relpath(root: Path, path: Path | str) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        return str(candidate.resolve().relative_to(root)).replace(os.sep, "/")
    return str(candidate).replace(os.sep, "/")


def is_hidden_name(name: str) -> bool:
    return name.startswith(".") and name != ".env"


def should_skip_relative_parts(parts: Sequence[str], include_hidden: bool) -> bool:
    for part in parts:
        if part in SKIP_DIRS:
            return True
        if not include_hidden and is_hidden_name(part):
            return True
    return False


def matches_glob(rel_path: str, pattern: str) -> bool:
    normalized_path = rel_path.replace(os.sep, "/")
    normalized_pattern = pattern.replace(os.sep, "/")
    if fnmatch.fnmatch(normalized_path, normalized_pattern):
        return True
    pure_path = PurePosixPath(normalized_path)
    if pure_path.match(normalized_pattern):
        return True
    if normalized_pattern.startswith("**/") and pure_path.match(normalized_pattern[3:]):
        return True
    if "**/" in normalized_pattern:
        collapsed_pattern = normalized_pattern.replace("**/", "")
        if collapsed_pattern != normalized_pattern and matches_glob(normalized_path, collapsed_pattern):
            return True
    return False


def iter_files(root: Path, include_hidden: bool = False) -> Iterable[Path]:
    for current_root, dirnames, filenames in os.walk(root):
        current_path = Path(current_root)
        rel_dir = current_path.relative_to(root)
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS and (include_hidden or not is_hidden_name(name))
        ]
        if should_skip_relative_parts(rel_dir.parts, include_hidden):
            dirnames[:] = []
            continue
        for filename in filenames:
            if not include_hidden and is_hidden_name(filename):
                continue
            yield current_path / filename


def iter_tree(base: Path, include_hidden: bool, max_depth: int) -> Iterator[Path]:
    for current_root, dirnames, filenames in os.walk(base):
        current_path = Path(current_root)
        rel_dir = current_path.relative_to(base)
        depth = len(rel_dir.parts)
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS and (include_hidden or not is_hidden_name(name))
        ]
        if depth >= max_depth:
            dirnames[:] = []
        for dirname in sorted(dirnames, key=str.lower):
            yield current_path / dirname
        for filename in sorted(filenames, key=str.lower):
            if not include_hidden and is_hidden_name(filename):
                continue
            yield current_path / filename


def is_binary_data(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:1024]
    non_text = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return non_text > max(16, len(sample) // 8)


def read_text(path: Path, *, max_bytes: int = MAX_READ_BYTES) -> str:
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"File too large to read safely: {len(data)} bytes")
    if is_binary_data(data):
        raise ValueError(f"Refusing to read binary file: {path.name}")
    return data.decode("utf-8", errors="replace")


def try_read_text(path: Path, *, max_bytes: int = MAX_READ_BYTES) -> tuple[Optional[str], Optional[str]]:
    try:
        data = path.read_bytes()
    except Exception:
        return None, "READ_FAILED"
    if len(data) > max_bytes:
        return None, "FILE_TOO_LARGE"
    if is_binary_data(data):
        return None, "BINARY_FILE"
    return data.decode("utf-8", errors="replace"), None


def infer_language(path: Path) -> str:
    suffix = path.suffix.lower()
    mapping = {
        ".py": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".css": "css",
        ".scss": "scss",
        ".md": "markdown",
        ".json": "json",
        ".toml": "toml",
        ".yml": "yaml",
        ".yaml": "yaml",
    }
    return mapping.get(suffix, suffix.lstrip(".") or "text")


def shorten(text: str, limit: int = MAX_SEARCH_PREVIEW) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tokenise_query(value: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_:\-/\.]+", str(value or ""))
    seen: list[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered not in seen:
            seen.append(lowered)
    return seen


def run_git(root: Path, args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(root), text=True, capture_output=True, timeout=timeout)


def ensure_git_repo(root: Path) -> bool:
    try:
        result = run_git(root, ["rev-parse", "--is-inside-work-tree"], timeout=10)
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def preview_lines(text: str, line_number: int, context_lines: int = 1) -> str:
    lines = text.splitlines()
    start = max(1, line_number - context_lines)
    end = min(len(lines), line_number + context_lines)
    chunk = []
    for index in range(start, end + 1):
        chunk.append(f"{index}: {lines[index - 1]}")
    return shorten("\n".join(chunk), 500)


def path_tokens(path: str) -> set[str]:
    lowered = path.lower().replace("\\", "/")
    tokens = re.split(r"[^a-z0-9_]+", lowered)
    return {token for token in tokens if token}


def is_hidden_name(name: str) -> bool:
    return name.startswith(".") and name != ".env"


def should_skip_relative_parts(parts: Sequence[str], include_hidden: bool) -> bool:
    for part in parts:
        if part in SKIP_DIRS:
            return True
        if not include_hidden and is_hidden_name(part):
            return True
    return False


def matches_glob(rel_path: str, pattern: str) -> bool:
    normalized_path = rel_path.replace(os.sep, "/")
    normalized_pattern = pattern.replace(os.sep, "/")
    if fnmatch.fnmatch(normalized_path, normalized_pattern):
        return True
    pure_path = PurePosixPath(normalized_path)
    if pure_path.match(normalized_pattern):
        return True
    if normalized_pattern.startswith("**/") and pure_path.match(normalized_pattern[3:]):
        return True
    if "**/" in normalized_pattern:
        collapsed_pattern = normalized_pattern.replace("**/", "")
        if collapsed_pattern != normalized_pattern and matches_glob(normalized_path, collapsed_pattern):
            return True
    return False


def iter_files(root: Path, include_hidden: bool = False) -> Iterable[Path]:
    for current_root, dirnames, filenames in os.walk(root):
        current_path = Path(current_root)
        rel_dir = current_path.relative_to(root)
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS and (include_hidden or not is_hidden_name(name))
        ]
        if should_skip_relative_parts(rel_dir.parts, include_hidden):
            dirnames[:] = []
            continue
        for filename in filenames:
            if not include_hidden and is_hidden_name(filename):
                continue
            yield current_path / filename


def iter_tree(base: Path, include_hidden: bool, max_depth: int) -> Iterator[Path]:
    for current_root, dirnames, filenames in os.walk(base):
        current_path = Path(current_root)
        rel_dir = current_path.relative_to(base)
        depth = len(rel_dir.parts)
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS and (include_hidden or not is_hidden_name(name))
        ]
        if depth >= max_depth:
            dirnames[:] = []
        for dirname in sorted(dirnames, key=str.lower):
            yield current_path / dirname
        for filename in sorted(filenames, key=str.lower):
            if not include_hidden and is_hidden_name(filename):
                continue
            yield current_path / filename


def is_binary_data(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:1024]
    non_text = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return non_text > max(16, len(sample) // 8)


def read_text(path: Path, *, max_bytes: int = MAX_READ_BYTES) -> str:
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise ValueError(f"File too large to read safely: {len(data)} bytes")
    if is_binary_data(data):
        raise ValueError(f"Refusing to read binary file: {path.name}")
    return data.decode("utf-8", errors="replace")


def try_read_text(path: Path, *, max_bytes: int = MAX_READ_BYTES) -> tuple[Optional[str], Optional[str]]:
    try:
        data = path.read_bytes()
    except Exception:
        return None, "READ_FAILED"
    if len(data) > max_bytes:
        return None, "FILE_TOO_LARGE"
    if is_binary_data(data):
        return None, "BINARY_FILE"
    return data.decode("utf-8", errors="replace"), None


def infer_language(path: Path) -> str:
    suffix = path.suffix.lower()
    mapping = {
        ".py": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".css": "css",
        ".scss": "scss",
        ".md": "markdown",
        ".json": "json",
        ".toml": "toml",
        ".yml": "yaml",
        ".yaml": "yaml",
    }
    return mapping.get(suffix, suffix.lstrip(".") or "text")


def shorten(text: str, limit: int = MAX_SEARCH_PREVIEW) -> str:
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def tokenise_query(value: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_:\-/\.]+", str(value or ""))
    seen: list[str] = []
    for token in tokens:
        lowered = token.lower()
        if lowered not in seen:
            seen.append(lowered)
    return seen


def run_git(root: Path, args: list[str], timeout: int = 20) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=str(root), text=True, capture_output=True, timeout=timeout)


def ensure_git_repo(root: Path) -> bool:
    try:
        result = run_git(root, ["rev-parse", "--is-inside-work-tree"], timeout=10)
    except Exception:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def preview_lines(text: str, line_number: int, context_lines: int = 1) -> str:
    lines = text.splitlines()
    start = max(1, line_number - context_lines)
    end = min(len(lines), line_number + context_lines)
    chunk = []
    for index in range(start, end + 1):
        chunk.append(f"{index}: {lines[index - 1]}")
    return shorten("\n".join(chunk), 500)


def path_tokens(path: str) -> set[str]:
    lowered = path.lower().replace("\\", "/")
    tokens = re.split(r"[^a-z0-9_]+", lowered)
    return {token for token in tokens if token}