#!/usr/bin/env python3
from __future__ import annotations

import argparse
import difflib
import fnmatch
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple, cast

from diagnostics import (
    build_check,
    changed_files_check,
    config_validate,
    dead_code_check,
    dependency_check,
    duplication_check,
    format_check,
    lint_check,
    policy_check,
    project_problems,
    runtime_smoke_check,
    schema_validate,
    security_check,
    syntax_check,
    test_check,
    type_check,
)
from discovery import (
    find_canonical_implementation,
    find_entry_points,
    find_files as discovery_find_files,
    find_ownership,
    find_related_configs,
    find_related_files,
    find_related_tests,
    find_similar_code,
    find_symbol_definitions,
    find_symbol_references,
    get_changed_files as discovery_get_changed_files,
    investigate,
    list_files as discovery_list_files,
    outline_file,
    read_symbol,
    recent_changes,
    search_in_files,
    semantic_search,
    trace_dependencies,
)
from mutations import (
    append_block,
    batch_mutate,
    copy_file,
    create_file,
    delete_file,
    delete_range,
    delete_snippet,
    fill_template,
    insert_after,
    insert_before,
    insert_symbol_member,
    move_block,
    prepend_block,
    rename_file,
    rename_symbol,
    replace_range,
    replace_snippet,
    replace_symbol,
)


MAX_READ_BYTES = 1_000_000
MAX_WRITE_BYTES = 2_000_000
MAX_OUTPUT_CHARS = 50_000
DEFAULT_LIST_DEPTH = 2
SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache", ".pytest_cache"}


def emit(obj: Dict[str, Any], code: int = 0) -> None:
    print(json.dumps(obj, ensure_ascii=False))
    raise SystemExit(code)


def ok(tool: str, data: Dict[str, Any]) -> None:
    emit({"ok": True, "tool": tool, "data": data}, 0)


def err(tool: str, code: str, message: str, extra: Dict[str, Any] | None = None, exit_code: int = 1) -> None:
    payload: Dict[str, Any] = {
        "ok": False,
        "tool": tool,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if extra:
        payload["error"]["extra"] = extra
    emit(payload, exit_code)


def shorten(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]..."


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


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


def resolve_root(root: str) -> Path:
    p = Path(root).expanduser().resolve()
    if not p.exists():
        err("common", "ROOT_NOT_FOUND", f"Root does not exist: {p}")
    if not p.is_dir():
        err("common", "ROOT_NOT_DIR", f"Root is not a directory: {p}")
    return p


def safe_join(root: Path, rel_path: str) -> Path:
    if not rel_path.strip():
        err("common", "EMPTY_PATH", "Path cannot be empty.")
    target = (root / rel_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        err("common", "PATH_ESCAPE", f"Path escapes root: {rel_path}")
    return target


def is_hidden_name(name: str) -> bool:
    return name.startswith(".") and name != ".env"


def is_binary_data(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return True
    sample = data[:1024]
    non_text = sum(byte < 9 or 13 < byte < 32 for byte in sample)
    return non_text > max(16, len(sample) // 8)


def should_skip_relative_parts(parts: Sequence[str], include_hidden: bool) -> bool:
    for part in parts:
        if part in SKIP_DIRS:
            return True
        if not include_hidden and is_hidden_name(part):
            return True
    return False


def read_text(path: Path, *, tool: str = "read", max_bytes: int = MAX_READ_BYTES) -> str:
    data = path.read_bytes()
    if len(data) > max_bytes:
        err(tool, "FILE_TOO_LARGE", f"File too large to read safely: {len(data)} bytes")
    if is_binary_data(data):
        err(tool, "BINARY_FILE", f"Refusing to read binary file: {path.name}")
    return data.decode("utf-8", errors="replace")


def try_read_text(path: Path, *, max_bytes: int = MAX_READ_BYTES) -> Tuple[Optional[str], Optional[str]]:
    try:
        data = path.read_bytes()
    except Exception:
        return None, "READ_FAILED"
    if len(data) > max_bytes:
        return None, "FILE_TOO_LARGE"
    if is_binary_data(data):
        return None, "BINARY_FILE"
    return data.decode("utf-8", errors="replace"), None


def read_text_window(path: Path, start_line: int, end_line: int) -> Dict[str, Any]:
    if start_line < 1:
        err("read", "BAD_RANGE", "start_line must be >= 1")
    if end_line < start_line:
        err("read", "BAD_RANGE", "end_line must be >= start_line")

    collected: List[str] = []
    total_lines = 0
    total_chars = 0
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for total_lines, line in enumerate(handle, start=1):
            if total_lines < start_line:
                continue
            if total_lines > end_line:
                continue
            collected.append(line)
            total_chars += len(line.encode("utf-8"))
            if total_chars > MAX_READ_BYTES:
                err("read", "FILE_TOO_LARGE", "Requested line range is too large to read safely")

    return {
        "content": "".join(collected),
        "start_line": start_line,
        "end_line": min(end_line, total_lines),
        "total_lines": total_lines,
        "partial": True,
    }


def read_file_payload(root: Path, path: Path, start_line: int | None = None, end_line: int | None = None) -> Dict[str, Any]:
    sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
    if start_line is not None or end_line is not None:
        range_start = int(start_line or 1)
        range_end = int(end_line or range_start)
        payload = read_text_window(path, range_start, range_end)
        payload["path"] = str(path.relative_to(root))
        payload["size_bytes"] = path.stat().st_size
        payload["sha256"] = sha256
        return payload
    return {
        "path": str(path.relative_to(root)),
        "content": read_text(path, tool="read"),
        "size_bytes": path.stat().st_size,
        "sha256": sha256,
        "partial": False,
    }


def normalize_path_list(raw_value: Any, tool: str, field_name: str) -> List[str]:
    if not isinstance(raw_value, list) or not raw_value:
        err(tool, "BAD_REQUEST", f"{field_name} must be a non-empty list")
    out: List[str] = []
    for item in raw_value:
        if not isinstance(item, str) or not item.strip():
            err(tool, "BAD_REQUEST", f"Each item in {field_name} must be a non-empty string")
        out.append(item)
    return out


def load_json_file(path: Path, tool: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        err(tool, "INPUT_NOT_FOUND", f"Input file not found: {path}")
    except json.JSONDecodeError as exc:
        err(tool, "BAD_JSON", f"Invalid JSON input: {exc}")


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
    }
    return mapping.get(suffix, suffix.lstrip(".") or "text")


def parse_git_status_entries(status_text: str) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    entries: List[Dict[str, Any]] = []
    counts = {
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "conflicted": 0,
    }
    for raw_line in status_text.splitlines():
        if not raw_line or raw_line.startswith("##"):
            continue
        xy = raw_line[:2]
        raw_path = raw_line[3:]
        old_path: Optional[str] = None
        path = raw_path
        if " -> " in raw_path:
            old_path, path = raw_path.split(" -> ", 1)
        staged_code = xy[0]
        unstaged_code = xy[1]
        if xy == "??":
            counts["untracked"] += 1
        else:
            if staged_code not in {" ", "?"}:
                counts["staged"] += 1
            if unstaged_code not in {" ", "?"}:
                counts["unstaged"] += 1
            if staged_code == "U" or unstaged_code == "U" or xy in {"AA", "DD"}:
                counts["conflicted"] += 1
        entries.append(
            {
                "xy": xy,
                "path": path,
                "old_path": old_path,
                "staged_status": staged_code,
                "unstaged_status": unstaged_code,
            }
        )
    return entries, counts


def git_diff_text(root: Path, *, path: str | None = None, staged: bool = False, stat: bool = False, name_only: bool = False) -> str:
    args = ["diff"]
    if staged:
        args.append("--cached")
    if stat:
        args.append("--stat")
    if name_only:
        args.append("--name-only")
    if path:
        args.extend(["--", path])
    else:
        args.extend(["--", "."])
    result = run_git(root, args, timeout=20)
    if result.returncode != 0:
        err("git_diff", "GIT_DIFF_FAILED", result.stderr.strip() or "git diff failed")
    return result.stdout


def scan_python_symbols(text: str) -> List[Tuple[re.Pattern[str], str]]:
    return [
        (re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b(?:\((.*?)\))?:"), "class"),
        (re.compile(r"^\s*async\s+def\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*\((.*?)\)\s*(?:->\s*([^:]+))?:"), "async_function"),
        (re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*\((.*?)\)\s*(?:->\s*([^:]+))?:"), "function"),
    ]


def scan_frontend_symbols(text: str) -> List[Tuple[re.Pattern[str], str]]:
    return [
        (re.compile(r"^\s*export\s+default\s+function\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*\((.*?)\)"), "exported_function"),
        (re.compile(r"^\s*export\s+function\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*\((.*?)\)"), "exported_function"),
        (re.compile(r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*\((.*?)\)"), "function"),
        (re.compile(r"^\s*export\s+(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*=\s*(?:async\s*)?\((.*?)\)\s*=>"), "exported_variable"),
        (re.compile(r"^\s*(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*=\s*(?:async\s*)?\((.*?)\)\s*=>"), "variable"),
        (re.compile(r"^\s*export\s+class\s+([A-Za-z_][A-Za-z0-9_]*)\b(?:\s+extends\s+([^\s{]+))?"), "exported_class"),
        (re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b(?:\s+extends\s+([^\s{]+))?"), "class"),
        (re.compile(r"^\s*export\s+interface\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "interface"),
        (re.compile(r"^\s*export\s+type\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*="), "type"),
        (re.compile(r"^\s*export\s+enum\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "enum"),
    ]


def collect_symbols_for_file(path: Path, text: str) -> List[Dict[str, Any]]:
    language = infer_language(path)
    patterns: List[Tuple[re.Pattern[str], str]] = []
    if language == "python":
        patterns = scan_python_symbols(text)
        symbols: List[Dict[str, Any]] = []
        class_stack: List[Tuple[int, str]] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            if not stripped:
                continue
            indent = len(line) - len(line.lstrip(" "))
            while class_stack and indent <= class_stack[-1][0]:
                class_stack.pop()
            matched = False
            for pattern, kind in patterns:
                match = pattern.match(line)
                if not match:
                    continue
                name = match.group(1)
                signature = stripped
                entry: Dict[str, Any] = {
                    "name": name,
                    "kind": kind,
                    "line": lineno,
                    "signature": signature,
                    "language": language,
                }
                if kind == "class":
                    class_stack.append((indent, name))
                elif class_stack:
                    entry["parent"] = class_stack[-1][1]
                    entry["qualified_name"] = f"{class_stack[-1][1]}.{name}"
                    entry["kind"] = "method" if kind == "function" else "async_method"
                symbols.append(entry)
                matched = True
                break
            if matched:
                continue
        return symbols
    if language in {"javascript", "typescript"}:
        patterns = scan_frontend_symbols(text)
        symbols = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern, kind in patterns:
                match = pattern.match(line)
                if not match:
                    continue
                name = match.group(1)
                signature = line.strip()
                symbols.append(
                    {
                        "name": name,
                        "kind": kind,
                        "line": lineno,
                        "signature": signature,
                        "language": language,
                    }
                )
                break
        return symbols
    return []


def extract_python_imports(text: str) -> List[Dict[str, Any]]:
    imports: List[Dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        import_match = re.match(r"^import\s+(.+)$", stripped)
        if import_match:
            modules = [part.strip() for part in import_match.group(1).split(",") if part.strip()]
            for module in modules:
                imports.append({"line": lineno, "module": module.split(" as ", 1)[0].strip(), "kind": "import"})
            continue
        from_match = re.match(r"^from\s+([A-Za-z0-9_\.]+)\s+import\s+(.+)$", stripped)
        if from_match:
            names = [part.strip() for part in from_match.group(2).split(",") if part.strip()]
            imports.append({"line": lineno, "module": from_match.group(1), "names": names, "kind": "from_import"})
    return imports


def extract_frontend_imports(text: str) -> List[Dict[str, Any]]:
    imports: List[Dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        import_match = re.match(r"^import\s+(.+?)\s+from\s+[\"']([^\"']+)[\"']", stripped)
        if import_match:
            imports.append({"line": lineno, "module": import_match.group(2), "binding": import_match.group(1).strip(), "kind": "import"})
            continue
        bare_import_match = re.match(r"^import\s+[\"']([^\"']+)[\"']", stripped)
        if bare_import_match:
            imports.append({"line": lineno, "module": bare_import_match.group(1), "kind": "side_effect_import"})
    return imports


def extract_exports_for_file(path: Path, text: str, symbols: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    language = infer_language(path)
    exports: List[Dict[str, Any]] = []
    if language == "python":
        all_match = re.search(r"^__all__\s*=\s*\[(.*?)\]", text, flags=re.MULTILINE | re.DOTALL)
        if all_match:
            names = [part.strip().strip("\"'") for part in all_match.group(1).split(",") if part.strip()]
            for name in names:
                exports.append({"name": name, "kind": "explicit_export"})
        else:
            for symbol in symbols:
                name = str(symbol.get("name", ""))
                if name and not name.startswith("_") and symbol.get("kind") in {"class", "function", "async_function"}:
                    exports.append({"name": name, "kind": "implicit_export"})
        return exports
    if language in {"javascript", "typescript"}:
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            default_match = re.match(r"^export\s+default\s+(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)?", stripped)
            if default_match:
                exports.append({"line": lineno, "name": default_match.group(1) or "default", "kind": "default_export"})
                continue
            named_match = re.match(r"^export\s+\{(.+)\}", stripped)
            if named_match:
                names = [part.strip().split(" as ", 1)[-1].strip() for part in named_match.group(1).split(",") if part.strip()]
                for name in names:
                    exports.append({"line": lineno, "name": name, "kind": "named_export"})
        for symbol in symbols:
            if str(symbol.get("kind", "")).startswith("exported_"):
                exports.append({"line": symbol.get("line"), "name": symbol.get("name"), "kind": symbol.get("kind")})
    return exports


def extract_dependencies_for_file(path: Path, text: str, symbols: List[Dict[str, Any]]) -> Dict[str, Any]:
    language = infer_language(path)
    imports: List[Dict[str, Any]] = []
    if language == "python":
        imports = extract_python_imports(text)
    elif language in {"javascript", "typescript"}:
        imports = extract_frontend_imports(text)
    exports = extract_exports_for_file(path, text, symbols)
    return {"imports": imports, "exports": exports}


def summarize_file(root: Path, path: Path, include_content: bool = False) -> Dict[str, Any]:
    text = read_text(path, tool="summarize")
    symbols = collect_symbols_for_file(path, text)
    deps = extract_dependencies_for_file(path, text, symbols)
    summary: Dict[str, Any] = {
        "path": str(path.relative_to(root)),
        "language": infer_language(path),
        "size_bytes": path.stat().st_size,
        "line_count": len(text.splitlines()),
        "symbol_count": len(symbols),
        "symbols": symbols[:50],
        "import_count": len(deps["imports"]),
        "imports": deps["imports"][:50],
        "export_count": len(deps["exports"]),
        "exports": deps["exports"][:50],
    }
    if include_content:
        summary["content"] = shorten(text, 5000)
    return summary


def change_risk_for_file(path: str, language: str, added: int, deleted: int) -> str:
    rel = path.lower()
    churn = added + deleted
    if any(part in rel for part in ["migration", "schema", "auth", "payment", "security", "config", ".github/workflows"]):
        return "high"
    if language in {"python", "typescript", "javascript"} and churn >= 80:
        return "high"
    if language in {"python", "typescript", "javascript"} or rel.endswith((".json", ".yml", ".yaml", ".toml")):
        return "medium"
    return "low"


def validation_hint_for_file(path: str, language: str) -> str:
    rel = path.lower()
    if language == "python":
        return "Run focused Python tests or py_compile for touched modules."
    if language in {"typescript", "javascript"}:
        return "Run frontend build/tests or targeted linting for touched modules."
    if rel.endswith((".json", ".yml", ".yaml", ".toml")):
        return "Validate config consumers or run the relevant app startup/check command."
    if rel.endswith((".md", ".txt")):
        return "No code execution needed; verify links/examples if changed."
    return "Use the nearest project-specific validation for this file type."


def parse_numstat(output: str, scope: str) -> Dict[str, Dict[str, Any]]:
    stats: Dict[str, Dict[str, Any]] = {}
    for raw_line in output.splitlines():
        parts = raw_line.split("\t")
        if len(parts) != 3:
            continue
        added_raw, deleted_raw, path = parts
        added = 0 if added_raw == "-" else int(added_raw)
        deleted = 0 if deleted_raw == "-" else int(deleted_raw)
        stats[path] = {"added": added, "deleted": deleted, "scope": scope}
    return stats


def build_review_summary(root: Path, status_text: str) -> Dict[str, Any]:
    entries, counts = parse_git_status_entries(status_text)
    unstaged_stats = parse_numstat(run_git(root, ["diff", "--numstat", "--", "."], timeout=20).stdout, "unstaged")
    staged_stats = parse_numstat(run_git(root, ["diff", "--cached", "--numstat", "--", "."], timeout=20).stdout, "staged")
    files: List[Dict[str, Any]] = []
    for entry in entries:
        path = str(entry.get("path", ""))
        combined = {"added": 0, "deleted": 0, "staged_added": 0, "staged_deleted": 0, "unstaged_added": 0, "unstaged_deleted": 0}
        if path in unstaged_stats:
            combined["unstaged_added"] = int(unstaged_stats[path]["added"])
            combined["unstaged_deleted"] = int(unstaged_stats[path]["deleted"])
        if path in staged_stats:
            combined["staged_added"] = int(staged_stats[path]["added"])
            combined["staged_deleted"] = int(staged_stats[path]["deleted"])
        combined["added"] = combined["unstaged_added"] + combined["staged_added"]
        combined["deleted"] = combined["unstaged_deleted"] + combined["staged_deleted"]
        language = infer_language(Path(path))
        files.append(
            {
                **entry,
                **combined,
                "language": language,
                "risk": change_risk_for_file(path, language, combined["added"], combined["deleted"]),
                "validation": validation_hint_for_file(path, language),
            }
        )
    high_risk = [file for file in files if file["risk"] == "high"]
    return {
        "counts": counts,
        "files": files,
        "high_risk_paths": [file["path"] for file in high_risk],
        "review_summary": {
            "changed_file_count": len(files),
            "high_risk_count": len(high_risk),
            "recommended_next_step": "Review high-risk files first, then validate by file type.",
        },
    }


def write_text(path: Path, content: str) -> int:
    raw = content.encode("utf-8")
    if len(raw) > MAX_WRITE_BYTES:
        err("write", "FILE_TOO_LARGE", f"Refusing to write oversized file: {len(raw)} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return len(raw)


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
        current_depth = len(rel_dir.parts)
        dirnames[:] = [
            name for name in dirnames
            if name not in SKIP_DIRS and (include_hidden or not is_hidden_name(name))
        ]
        if current_depth >= max_depth:
            dirnames[:] = []
        for dirname in sorted(dirnames, key=str.lower):
            yield current_path / dirname
        for filename in sorted(filenames, key=str.lower):
            if not include_hidden and is_hidden_name(filename):
                continue
            yield current_path / filename


def format_entry(root: Path, path: Path) -> Dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path.relative_to(root)),
        "name": path.name,
        "is_dir": path.is_dir(),
        "size": stat.st_size if path.is_file() else None,
        "modified": int(stat.st_mtime),
    }


def resolve_search_base(root: Path, rel_path: str | None, tool: str) -> Path:
    base = safe_join(root, rel_path or ".")
    if not base.exists():
        err(tool, "NOT_FOUND", f"Path does not exist: {rel_path or '.'}")
    return base


def cmd_ls(args: argparse.Namespace) -> None:
    tool = "ls"
    root = resolve_root(args.root)
    base = safe_join(root, args.path or ".")
    if not base.exists():
        err(tool, "NOT_FOUND", f"Path does not exist: {args.path or '.'}")
    if not base.is_dir():
        err(tool, "NOT_DIR", f"Path is not a directory: {args.path or '.'}")
    items: List[Dict[str, Any]] = []
    count = 0
    recursive = bool(getattr(args, "recursive", False))
    max_depth = int(getattr(args, "max_depth", DEFAULT_LIST_DEPTH))
    glob = getattr(args, "glob", None)

    if recursive:
        iterator = iter_tree(base, include_hidden=args.hidden, max_depth=max_depth)
    else:
        iterator = iter(sorted(base.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())))

    for child in iterator:
        rel_parts = child.relative_to(root).parts
        if should_skip_relative_parts(rel_parts, args.hidden):
            continue
        rel = str(child.relative_to(root))
        if glob and not matches_glob(rel, glob):
            continue
        items.append(format_entry(root, child))
        count += 1
        if count >= args.limit:
            break

    ok(
        tool,
        {
            "root": str(root),
            "path": str(base.relative_to(root)),
            "items": items,
            "recursive": recursive,
            "max_depth": max_depth if recursive else 1,
            "glob": glob,
        },
    )


def cmd_read(args: argparse.Namespace) -> None:
    tool = "read"
    root = resolve_root(args.root)
    path = safe_join(root, args.path)
    if not path.exists():
        err(tool, "NOT_FOUND", f"File does not exist: {args.path}")
    if not path.is_file():
        err(tool, "NOT_FILE", f"Path is not a file: {args.path}")

    ok(tool, read_file_payload(root, path, args.start_line, args.end_line))


def cmd_inspect(args: argparse.Namespace) -> None:
    tool = "inspect"
    root = resolve_root(args.root)
    spec_file = Path(args.spec_file).expanduser().resolve()
    spec = load_json_file(spec_file, tool)
    if not isinstance(spec, dict):
        err(tool, "BAD_REQUEST", "Spec must be a JSON object")
    spec_dict = cast(Dict[str, Any], spec)
    raw_files = spec_dict.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        err(tool, "BAD_REQUEST", "Spec must contain a non-empty 'files' list")
    files: List[Dict[str, Any]] = []
    for raw_item in cast(List[Any], raw_files):
        if not isinstance(raw_item, dict):
            err(tool, "BAD_REQUEST", "Each file spec must be an object")
        files.append(cast(Dict[str, Any], raw_item))

    include_content = bool(spec_dict.get("include_content", True))
    inspect_results: List[Dict[str, Any]] = []
    for item in files:
        rel_path = item.get("path")
        if not isinstance(rel_path, str) or not rel_path.strip():
            err(tool, "BAD_REQUEST", "Each file spec must include a non-empty path")
        path = safe_join(root, cast(str, rel_path))
        if not path.exists():
            inspect_results.append({"path": rel_path, "ok": False, "error": {"code": "NOT_FOUND", "message": f"File does not exist: {rel_path}"}})
            continue
        if not path.is_file():
            inspect_results.append({"path": rel_path, "ok": False, "error": {"code": "NOT_FILE", "message": f"Path is not a file: {rel_path}"}})
            continue
        try:
            payload = {
                "path": str(path.relative_to(root)),
                "ok": True,
                "language": infer_language(path),
                "size_bytes": path.stat().st_size,
            }
            if include_content:
                payload.update(read_file_payload(root, path, item.get("start_line"), item.get("end_line")))
            inspect_results.append(payload)
        except SystemExit:
            raise
        except Exception as exc:
            inspect_results.append({"path": rel_path, "ok": False, "error": {"code": "INSPECT_FAILED", "message": str(exc)}})

    ok(tool, {"files": inspect_results, "count": len(inspect_results)})


def cmd_write(args: argparse.Namespace) -> None:
    tool = "write"
    root = resolve_root(args.root)
    content = _resolve_text_argument(args, value_attr="content", file_attr="input_file", required=True, tool=tool)
    path = safe_join(root, args.path)
    existed = path.exists() and path.is_file()
    result = create_file(args.path, content, overwrite=True, root=root)
    _fail_for_unsuccessful_mutation(tool, result)
    ok(
        tool,
        {
            "path": str(result.get("file_path", args.path)),
            "bytes_written": len(content.encode("utf-8")),
            "created": not existed,
            "sha256": str(result.get("after_hash", "") or ""),
            "diff": shorten(str(result.get("diff", "") or ""), MAX_OUTPUT_CHARS),
            "mutation": result,
        },
    )


def cmd_patch(args: argparse.Namespace) -> None:
    tool = "patch"
    root = resolve_root(args.root)
    result = replace_snippet(
        args.path,
        args.search,
        args.replace,
        replace_all=bool(args.all),
        root=root,
    )
    if not result.get("ok"):
        _fail_for_unsuccessful_mutation(tool, result, default_code="PATCH_FAILED")
    replacements = int((result.get("details") or {}).get("replacements", 0) or 0)
    if result.get("reason") == "already_applied":
        ok(
            tool,
            {
                "path": str(result.get("file_path", args.path)),
                "replacements": 0,
                "diff": "",
                "status": "already_applied",
                "message": "Patch appears to be already applied.",
                "mutation": result,
            },
        )
    ok(
        tool,
        {
            "path": str(result.get("file_path", args.path)),
            "replacements": replacements,
            "sha256": str(result.get("after_hash", "") or ""),
            "diff": shorten(str(result.get("diff", "") or ""), MAX_OUTPUT_CHARS),
            "mutation": result,
        },
    )


def _resolve_text_file(path_value: str, tool: str) -> str:
    src = Path(path_value).expanduser().resolve()
    if not src.exists():
        err(tool, "INPUT_NOT_FOUND", f"Input file not found: {src}")
    return src.read_text(encoding="utf-8")


def _resolve_text_argument(
    args: argparse.Namespace,
    *,
    value_attr: str,
    file_attr: str,
    required: bool,
    tool: str,
    default: Optional[str] = None,
) -> str:
    file_value = getattr(args, file_attr, None)
    if file_value:
        return _resolve_text_file(str(file_value), tool)
    value = getattr(args, value_attr, None)
    if isinstance(value, str):
        return value
    if required:
        err(tool, "MISSING_CONTENT", f"Provide --{value_attr.replace('_', '-')} or --{file_attr.replace('_', '-')}.")
    return default or ""


def patch_already_applied(original: str, search: str, replace: str) -> bool:
    if not search or search == replace:
        return False
    if replace == "":
        return False
    return search not in original and replace in original


def _mutation_error_parts(result: Dict[str, Any], default_code: str) -> tuple[str, str, Dict[str, Any]]:
    diagnostics = result.get("diagnostics") or []
    if isinstance(diagnostics, list) and diagnostics:
        first = diagnostics[0] if isinstance(diagnostics[0], dict) else {}
        code = str(first.get("code", default_code) or default_code)
        message = str(first.get("message", result.get("reason", "Mutation failed")) or "Mutation failed")
    else:
        code = default_code
        message = str(result.get("reason", "Mutation failed") or "Mutation failed")
    return code, message, {"mutation": result}


def _fail_for_unsuccessful_mutation(tool: str, result: Dict[str, Any], *, default_code: str = "MUTATION_FAILED") -> None:
    if result.get("ok"):
        return
    code, message, extra = _mutation_error_parts(result, default_code)
    err(tool, code, message, extra=extra)


def _emit_mutation_ok(tool: str, result: Dict[str, Any]) -> None:
    _fail_for_unsuccessful_mutation(tool, result)
    payload = dict(result)
    if "diff" in payload:
        payload["diff"] = shorten(str(payload.get("diff", "") or ""), MAX_OUTPUT_CHARS)
    payload["sha256"] = str(payload.get("after_hash", "") or "")
    ok(tool, payload)


def cmd_replace_range(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    new_text = _resolve_text_argument(args, value_attr="new_text", file_attr="input_file", required=False, tool="replace_range")
    _emit_mutation_ok(
        "replace_range",
        replace_range(args.path, int(args.start_line), int(args.end_line), new_text, root=root),
    )


def cmd_replace_snippet(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    old_text = _resolve_text_argument(args, value_attr="old_text", file_attr="old_file", required=True, tool="replace_snippet")
    new_text = _resolve_text_argument(args, value_attr="new_text", file_attr="new_file", required=False, tool="replace_snippet")
    _emit_mutation_ok(
        "replace_snippet",
        replace_snippet(
            args.path,
            old_text,
            new_text,
            expected_occurrences=int(args.expected_occurrences),
            replace_all=bool(args.all),
            root=root,
        ),
    )


def cmd_insert_before(args: argparse.Namespace) -> None:
    _cmd_insert_relative(args, position="before")


def cmd_insert_after(args: argparse.Namespace) -> None:
    _cmd_insert_relative(args, position="after")


def _cmd_insert_relative(args: argparse.Namespace, *, position: str) -> None:
    root = resolve_root(args.root)
    anchor_text = _resolve_text_argument(args, value_attr="anchor_text", file_attr="anchor_file", required=True, tool=f"insert_{position}")
    new_text = _resolve_text_argument(args, value_attr="new_text", file_attr="input_file", required=True, tool=f"insert_{position}")
    func = insert_before if position == "before" else insert_after
    _emit_mutation_ok(
        f"insert_{position}",
        func(
            args.path,
            anchor_text,
            new_text,
            expected_occurrences=int(args.expected_occurrences),
            root=root,
        ),
    )


def cmd_delete_range(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_mutation_ok("delete_range", delete_range(args.path, int(args.start_line), int(args.end_line), root=root))


def cmd_delete_snippet(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    text = _resolve_text_argument(args, value_attr="text", file_attr="input_file", required=True, tool="delete_snippet")
    _emit_mutation_ok(
        "delete_snippet",
        delete_snippet(args.path, text, expected_occurrences=int(args.expected_occurrences), root=root),
    )


def cmd_append_block(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    new_text = _resolve_text_argument(args, value_attr="new_text", file_attr="input_file", required=True, tool="append_block")
    _emit_mutation_ok("append_block", append_block(args.path, new_text, root=root))


def cmd_prepend_block(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    new_text = _resolve_text_argument(args, value_attr="new_text", file_attr="input_file", required=True, tool="prepend_block")
    _emit_mutation_ok("prepend_block", prepend_block(args.path, new_text, root=root))


def cmd_replace_symbol(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    new_text = _resolve_text_argument(args, value_attr="new_text", file_attr="input_file", required=True, tool="replace_symbol")
    _emit_mutation_ok(
        "replace_symbol",
        replace_symbol(args.path, str(args.symbol_name), str(args.symbol_kind), new_text, root=root),
    )


def cmd_insert_symbol_member(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    member_text = _resolve_text_argument(args, value_attr="member_text", file_attr="input_file", required=True, tool="insert_symbol_member")
    _emit_mutation_ok(
        "insert_symbol_member",
        insert_symbol_member(args.path, str(args.container_symbol), member_text, position=str(args.position), root=root),
    )


def cmd_rename_symbol(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_mutation_ok(
        "rename_symbol",
        rename_symbol(args.path, str(args.old_name), str(args.new_name), scope=str(args.scope), root=root),
    )


def cmd_move_block(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    anchor_text = _resolve_text_argument(args, value_attr="destination_anchor", file_attr="anchor_file", required=True, tool="move_block")
    _emit_mutation_ok(
        "move_block",
        move_block(
            args.path,
            int(args.start_line),
            int(args.end_line),
            anchor_text,
            position=str(args.position),
            root=root,
        ),
    )


def cmd_create_file(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    content = _resolve_text_argument(args, value_attr="content", file_attr="input_file", required=True, tool="create_file")
    _emit_mutation_ok("create_file", create_file(args.path, content, overwrite=bool(args.overwrite), root=root))


def cmd_delete_file(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_mutation_ok("delete_file", delete_file(args.path, root=root))


def cmd_rename_file(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_mutation_ok("rename_file", rename_file(args.old_path, args.new_path, root=root))


def cmd_copy_file(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_mutation_ok(
        "copy_file",
        copy_file(args.source_path, args.destination_path, overwrite=bool(args.overwrite), root=root),
    )


def cmd_fill_template(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    slots_path = Path(str(args.slots_file)).expanduser().resolve()
    if not slots_path.exists():
        err("fill_template", "SLOTS_NOT_FOUND", f"Slots file not found: {slots_path}")
    try:
        slots = json.loads(slots_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        err("fill_template", "INVALID_JSON", str(exc))
    if not isinstance(slots, dict):
        err("fill_template", "BAD_REQUEST", "Slots file must contain a JSON object.")
    _emit_mutation_ok("fill_template", fill_template(args.path, cast(Dict[str, str], slots), root=root))


def cmd_batch_mutate(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    spec_path = Path(str(args.spec_file)).expanduser().resolve()
    if not spec_path.exists():
        err("batch_mutate", "SPEC_NOT_FOUND", f"Spec file not found: {spec_path}")
    try:
        payload = json.loads(spec_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        err("batch_mutate", "INVALID_JSON", str(exc))
    if isinstance(payload, dict):
        operations = payload.get("operations")
        atomic = bool(payload.get("atomic", bool(args.atomic)))
    else:
        operations = payload
        atomic = bool(args.atomic)
    if not isinstance(operations, list):
        err("batch_mutate", "BAD_REQUEST", "Spec file must contain an operations list or an object with operations.")
    result = batch_mutate(cast(List[Dict[str, Any]], operations), atomic=atomic, root=root)
    if not result.get("ok"):
        first_failure = next((item for item in result.get("operations", []) if not item.get("ok")), None)
        if isinstance(first_failure, dict):
            _fail_for_unsuccessful_mutation("batch_mutate", first_failure)
        err("batch_mutate", "MUTATION_FAILED", "Batch mutate failed.", extra={"batch": result})
    ok("batch_mutate", cast(Dict[str, Any], result))


def cmd_grep(args: argparse.Namespace) -> None:
    tool = "grep"
    root = resolve_root(args.root)
    base = resolve_search_base(root, getattr(args, "path", None), tool)
    if command_exists("rg"):
        cmd = ["rg", "--json", "--line-number", "--color", "never", "--max-filesize", "1000K"]
        if args.ignore_case:
            cmd.append("-i")
        if getattr(args, "fixed_strings", False):
            cmd.append("-F")
        if args.hidden:
            cmd.append("--hidden")
        for skip_dir in sorted(SKIP_DIRS):
            cmd.extend(["-g", f"!**/{skip_dir}/**"])
        if args.glob:
            cmd.extend(["-g", args.glob])
        cmd.extend([args.pattern, str(base)])

        process = subprocess.Popen(
            cmd,
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        matches: List[Dict[str, Any]] = []
        limit_reached = False
        assert process.stdout is not None
        for line in process.stdout:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") != "match":
                continue
            data = event.get("data") or {}
            path_data = data.get("path") or {}
            line_data = data.get("lines") or {}
            matches.append(
                {
                    "path": path_data.get("text", ""),
                    "line": data.get("line_number"),
                    "text": str(line_data.get("text", "")).rstrip("\n"),
                }
            )
            if len(matches) >= args.limit:
                limit_reached = True
                process.terminate()
                break

        stdout_tail, stderr_text = process.communicate()
        if stdout_tail:
            for raw_line in stdout_tail.splitlines():
                if len(matches) >= args.limit:
                    break
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "match":
                    continue
                data = event.get("data") or {}
                path_data = data.get("path") or {}
                line_data = data.get("lines") or {}
                matches.append(
                    {
                        "path": path_data.get("text", ""),
                        "line": data.get("line_number"),
                        "text": str(line_data.get("text", "")).rstrip("\n"),
                    }
                )
        if process.returncode not in {0, 1, -15}:
            err(tool, "GREP_FAILED", stderr_text.strip() or "ripgrep search failed")
        ok(
            tool,
            {
                "matches": matches[: args.limit],
                "engine": "ripgrep",
                "path": str(base.relative_to(root)),
                "limit_reached": limit_reached or len(matches) >= args.limit,
            },
        )

    pattern = args.pattern
    flags = re.IGNORECASE if args.ignore_case else 0
    rx = re.compile("a^")
    try:
        rx = re.compile(pattern, flags) if not getattr(args, "fixed_strings", False) else re.compile(re.escape(pattern), flags)
    except re.error as exc:
        err(tool, "BAD_PATTERN", f"Invalid regex: {exc}")

    matches = []
    files_scanned = 0
    files_skipped = 0
    for file_path in iter_files(base if base.is_dir() else base.parent, include_hidden=args.hidden):
        rel = str(file_path.relative_to(root))
        if base.is_file() and file_path != base:
            continue
        if args.glob and not matches_glob(rel, args.glob):
            continue
        try:
            data = file_path.read_bytes()
            if len(data) > MAX_READ_BYTES or is_binary_data(data):
                files_skipped += 1
                continue
            text = data.decode("utf-8", errors="replace")
        except Exception:
            files_skipped += 1
            continue
        files_scanned += 1

        for lineno, line in enumerate(text.splitlines(), start=1):
            if rx.search(line):
                matches.append({"path": rel, "line": lineno, "text": line})
                if len(matches) >= args.limit:
                    ok(tool, {"matches": matches, "engine": "python", "path": str(base.relative_to(root)), "files_scanned": files_scanned, "files_skipped": files_skipped, "limit_reached": True})

    ok(tool, {"matches": matches, "engine": "python", "path": str(base.relative_to(root)), "files_scanned": files_scanned, "files_skipped": files_skipped, "limit_reached": False})


def cmd_find(args: argparse.Namespace) -> None:
    tool = "find"
    root = resolve_root(args.root)
    base = resolve_search_base(root, getattr(args, "path", None), tool)

    if command_exists("rg") and base.is_dir():
        cmd = ["rg", "--files"]
        if args.hidden:
            cmd.append("--hidden")
        for skip_dir in sorted(SKIP_DIRS):
            cmd.extend(["-g", f"!**/{skip_dir}/**"])
        cmd.extend(["-g", args.glob, str(base)])
        result = subprocess.run(cmd, cwd=str(root), text=True, capture_output=True, timeout=20)
        if result.returncode not in {0, 1}:
            err(tool, "FIND_FAILED", result.stderr.strip() or "rg --files failed")
        files = [line.strip() for line in result.stdout.splitlines() if line.strip()][: args.limit]
        ok(tool, {"files": files, "engine": "ripgrep", "path": str(base.relative_to(root)), "limit_reached": len(files) >= args.limit})

    out: List[str] = []
    candidates: Iterable[Path]
    if base.is_file():
        candidates = [base]
    else:
        candidates = iter_files(base, include_hidden=args.hidden)
    for file_path in candidates:
        rel = str(file_path.relative_to(root))
        if matches_glob(rel, args.glob):
            out.append(rel)
            if len(out) >= args.limit:
                break
    ok(tool, {"files": out, "engine": "python", "path": str(base.relative_to(root)), "limit_reached": len(out) >= args.limit})


def cmd_summarize(args: argparse.Namespace) -> None:
    tool = "summarize"
    root = resolve_root(args.root)
    base = resolve_search_base(root, getattr(args, "path", None), tool)
    summaries: List[Dict[str, Any]] = []
    candidates: Iterable[Path]
    if base.is_file():
        candidates = [base]
    else:
        candidates = iter_files(base, include_hidden=args.hidden)
    for file_path in candidates:
        rel = str(file_path.relative_to(root))
        if args.glob and not matches_glob(rel, args.glob):
            continue
        try:
            summaries.append(summarize_file(root, file_path, include_content=args.include_content))
        except SystemExit:
            raise
        except Exception:
            continue
        if len(summaries) >= args.limit:
            ok(tool, {"files": summaries, "path": str(base.relative_to(root)), "limit_reached": True})
    ok(tool, {"files": summaries, "path": str(base.relative_to(root)), "limit_reached": False})


def cmd_run(args: argparse.Namespace) -> None:
    tool = "run"
    root = resolve_root(args.root)
    # ensure 'result' is declared for static analysis; error paths call err() which exits
    result = subprocess.CompletedProcess(args=args.command or [], returncode=1, stdout="", stderr="")
    try:
        result = subprocess.run(
            args.command,
            cwd=str(root),
            text=True,
            capture_output=True,
            timeout=args.timeout,
        )
    except subprocess.TimeoutExpired:
        err(tool, "TIMEOUT", f"Command timed out after {args.timeout}s", {"command": args.command})
    except FileNotFoundError as exc:
        err(tool, "NOT_FOUND", str(exc))
    except Exception as exc:
        err(tool, "RUN_FAILED", str(exc))

    ok(
        tool,
        {
            "command": args.command,
            "returncode": result.returncode,
            "stdout": shorten(result.stdout),
            "stderr": shorten(result.stderr),
        },
    )


def git_diff(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "--", "."],
            cwd=str(root),
            text=True,
            capture_output=True,
            timeout=20,
        )
        if result.returncode == 0:
            return result.stdout
    except Exception:
        pass
    return ""


def run_git(root: Path, args: List[str], timeout: int = 20) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(root),
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def ensure_git_repo(root: Path) -> None:
    # default result to satisfy static analyzers; error paths call err() which exits
    result = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
    try:
        result = run_git(root, ["rev-parse", "--is-inside-work-tree"], timeout=10)
    except FileNotFoundError:
        err("git", "GIT_NOT_FOUND", "git executable not found")
    except Exception as exc:
        err("git", "GIT_CHECK_FAILED", str(exc))

    if result.returncode != 0 or result.stdout.strip() != "true":
        err("git", "NOT_GIT_REPO", f"Root is not a git repository: {root}")


def cmd_diff(args: argparse.Namespace) -> None:
    tool = "diff"
    root = resolve_root(args.root)
    diff = git_diff(root)
    ok(tool, {"diff": shorten(diff)})


def cmd_meta(args: argparse.Namespace) -> None:
    tool = "meta"
    root = resolve_root(args.root)

    git_status = ""
    git_branch = ""
    is_git_repo = False

    try:
        probe = run_git(root, ["rev-parse", "--is-inside-work-tree"], timeout=10)
        if probe.returncode == 0 and probe.stdout.strip() == "true":
            is_git_repo = True

            status_result = run_git(root, ["status", "--short", "--branch"], timeout=10)
            if status_result.returncode == 0:
                git_status = status_result.stdout

            branch_result = run_git(root, ["branch", "--show-current"], timeout=10)
            if branch_result.returncode == 0:
                git_branch = branch_result.stdout.strip()
    except Exception:
        pass

    top_level_entries = []
    try:
        for child in sorted(root.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))[:20]:
            if child.name in SKIP_DIRS or is_hidden_name(child.name):
                continue
            top_level_entries.append(format_entry(root, child))
    except Exception:
        top_level_entries = []

    ok(
        tool,
        {
            "root": str(root),
            "cwd": str(Path.cwd()),
            "file_count": sum(1 for _ in iter_files(root, include_hidden=True)),
            "is_git_repo": is_git_repo,
            "git_branch": git_branch,
            "git_status": shorten(git_status),
            "top_level_entries": top_level_entries,
            "tooling": {
                "ripgrep": command_exists("rg"),
                "git": command_exists("git"),
            },
            "python": sys.version,
            "platform": sys.platform,
        },
    )


def cmd_git_status(args: argparse.Namespace) -> None:
    tool = "git_status"
    root = resolve_root(args.root)
    ensure_git_repo(root)

    status_args = ["status", "--short", "--branch"]
    if getattr(args, "ignored", False):
        status_args.append("--ignored")
    result = run_git(root, status_args, timeout=15)
    if result.returncode != 0:
        err(tool, "GIT_STATUS_FAILED", result.stderr.strip() or "git status failed")

    entries, counts = parse_git_status_entries(result.stdout)
    ok(tool, {"status": shorten(result.stdout), "entries": entries[: args.limit], "counts": counts, "limit_reached": len(entries) > args.limit})


def cmd_git_diff(args: argparse.Namespace) -> None:
    tool = "git_diff"
    root = resolve_root(args.root)
    ensure_git_repo(root)

    target = args.path if getattr(args, "path", None) else None
    staged = bool(getattr(args, "staged", False))
    include_diff = not bool(getattr(args, "name_only", False) or getattr(args, "stat", False) and getattr(args, "summary_only", False))
    diff_text = git_diff_text(root, path=target, staged=staged, stat=False, name_only=False) if include_diff else ""
    stat_text = git_diff_text(root, path=target, staged=staged, stat=True, name_only=False) if getattr(args, "stat", False) else ""
    name_only_text = git_diff_text(root, path=target, staged=staged, stat=False, name_only=True) if getattr(args, "name_only", False) else ""
    names = [line.strip() for line in name_only_text.splitlines() if line.strip()]
    ok(tool, {"path": target, "staged": staged, "diff": shorten(diff_text), "stat": shorten(stat_text), "files": names[: args.limit], "limit_reached": len(names) > args.limit})


def cmd_symbols(args: argparse.Namespace) -> None:
    tool = "symbols"
    root = resolve_root(args.root)
    base = resolve_search_base(root, getattr(args, "path", None), tool)
    query = str(getattr(args, "query", "") or "").strip().lower()
    results: List[Dict[str, Any]] = []
    files_scanned = 0
    files_skipped = 0
    skipped_binary_files = 0
    candidates: Iterable[Path]
    if base.is_file():
        candidates = [base]
    else:
        candidates = iter_files(base, include_hidden=args.hidden)

    for file_path in candidates:
        rel = str(file_path.relative_to(root))
        if args.glob and not matches_glob(rel, args.glob):
            continue
        text, skip_reason = try_read_text(file_path)
        if text is None:
            files_skipped += 1
            if skip_reason == "BINARY_FILE":
                skipped_binary_files += 1
            continue
        files_scanned += 1
        symbols = collect_symbols_for_file(file_path, text)
        deps = extract_dependencies_for_file(file_path, text, symbols)
        for symbol in symbols:
            if query and query not in str(symbol.get("name", "")).lower() and query not in str(symbol.get("signature", "")).lower():
                continue
            results.append({"path": rel, "imports": deps["imports"][:20], "exports": deps["exports"][:20], **symbol})
            if len(results) >= args.limit:
                ok(tool, {"symbols": results, "path": str(base.relative_to(root)), "files_scanned": files_scanned, "files_skipped": files_skipped, "skipped_binary_files": skipped_binary_files, "limit_reached": True})

    ok(tool, {"symbols": results, "path": str(base.relative_to(root)), "files_scanned": files_scanned, "files_skipped": files_skipped, "skipped_binary_files": skipped_binary_files, "limit_reached": False})


def cmd_review(args: argparse.Namespace) -> None:
    tool = "review"
    root = resolve_root(args.root)
    ensure_git_repo(root)
    status_args = ["status", "--short", "--branch"]
    if getattr(args, "ignored", False):
        status_args.append("--ignored")
    status_result = run_git(root, status_args, timeout=20)
    if status_result.returncode != 0:
        err(tool, "GIT_STATUS_FAILED", status_result.stderr.strip() or "git status failed")
    review = build_review_summary(root, status_result.stdout)
    files = cast(List[Dict[str, Any]], review.get("files", []))
    if getattr(args, "path", None):
        target_prefix = str(args.path)
        files = [file for file in files if str(file.get("path", "")).startswith(target_prefix)]
        review["files"] = files
        review["review_summary"]["changed_file_count"] = len(files)
        review["high_risk_paths"] = [file["path"] for file in files if file.get("risk") == "high"]
        review["review_summary"]["high_risk_count"] = len(review["high_risk_paths"])
    review["files"] = files[: args.limit]
    review["limit_reached"] = len(files) > args.limit
    ok(tool, cast(Dict[str, Any], review))


def cmd_git_add(args: argparse.Namespace) -> None:
    tool = "git_add"
    root = resolve_root(args.root)
    ensure_git_repo(root)

    raw_paths = args.path if isinstance(args.path, list) else [args.path]
    rel_paths: List[str] = []
    for raw_path in raw_paths:
        path = safe_join(root, raw_path)
        rel_paths.append(str(path.relative_to(root)))

    result = run_git(root, ["add", "--", *rel_paths], timeout=15)
    if result.returncode != 0:
        err(tool, "GIT_ADD_FAILED", result.stderr.strip() or f"git add failed for {', '.join(rel_paths)}")

    ok(
        tool,
        {
            "path": rel_paths[0] if len(rel_paths) == 1 else None,
            "paths": rel_paths,
            "staged": True,
        },
    )


def cmd_git_restore(args: argparse.Namespace) -> None:
    tool = "git_restore"
    root = resolve_root(args.root)
    ensure_git_repo(root)

    path = safe_join(root, args.path)
    rel = str(path.relative_to(root))

    git_args = ["restore"]
    if getattr(args, "staged", False):
        git_args.append("--staged")
    git_args.extend(["--", rel])

    result = run_git(root, git_args, timeout=15)
    if result.returncode != 0:
        err(tool, "GIT_RESTORE_FAILED", result.stderr.strip() or f"git restore failed for {rel}")

    ok(tool, {"path": rel, "staged": bool(getattr(args, "staged", False)), "restored": True})


def cmd_git_commit(args: argparse.Namespace) -> None:
    tool = "git_commit"
    root = resolve_root(args.root)
    ensure_git_repo(root)

    result = run_git(root, ["commit", "-m", args.message], timeout=30)
    if result.returncode != 0:
        err(tool, "GIT_COMMIT_FAILED", result.stderr.strip() or "git commit failed")

    ok(tool, {"message": args.message, "output": shorten(result.stdout)})


def cmd_git_log(args: argparse.Namespace) -> None:
    tool = "git_log"
    root = resolve_root(args.root)
    ensure_git_repo(root)

    fmt = "%H%x1f%h%x1f%an%x1f%ad%x1f%s"
    result = run_git(
        root,
        ["log", f"-n{args.limit}", f"--date=iso", f"--pretty=format:{fmt}"],
        timeout=20,
    )
    if result.returncode != 0:
        err(tool, "GIT_LOG_FAILED", result.stderr.strip() or "git log failed")

    commits = []
    for line in result.stdout.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 5:
            full_hash, short_hash, author, date, subject = parts
            commits.append(
                {
                    "hash": full_hash,
                    "short_hash": short_hash,
                    "author": author,
                    "date": date,
                    "subject": subject,
                }
            )

    ok(tool, {"commits": commits})


def cmd_git_branch(args: argparse.Namespace) -> None:
    tool = "git_branch"
    root = resolve_root(args.root)
    ensure_git_repo(root)

    result = run_git(root, ["branch", "--show-current"], timeout=10)
    if result.returncode != 0:
        err(tool, "GIT_BRANCH_FAILED", result.stderr.strip() or "git branch failed")

    ok(tool, {"branch": result.stdout.strip()})


def _non_empty_paths(args: argparse.Namespace, *, required: bool = True) -> List[str]:
    raw_paths = getattr(args, "path", None)
    if raw_paths is None:
        if required:
            err("diagnostics", "MISSING_PATH", "Provide at least one --path value.")
        return []
    if not isinstance(raw_paths, list):
        raw_paths = [raw_paths]
    paths = [str(item).strip() for item in raw_paths if str(item).strip()]
    if required and not paths:
        err("diagnostics", "MISSING_PATH", "Provide at least one --path value.")
    return paths


def cmd_syntax_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    ok("syntax_check", syntax_check(_non_empty_paths(args), root=root))


def cmd_type_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    ok("type_check", type_check(_non_empty_paths(args), scope=str(args.scope), root=root))


def cmd_lint_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    ok("lint_check", lint_check(_non_empty_paths(args), scope=str(args.scope), root=root))


def cmd_format_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    ok("format_check", format_check(_non_empty_paths(args), root=root))


def cmd_config_validate(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    ok("config_validate", config_validate(_non_empty_paths(args), root=root))


def cmd_schema_validate(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    refs = getattr(args, "schema_ref", None)
    schema_refs = [str(item).strip() for item in refs if str(item).strip()] if isinstance(refs, list) else []
    ok("schema_validate", schema_validate(_non_empty_paths(args), schema_refs=schema_refs, root=root))


def cmd_dependency_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    ok("dependency_check", dependency_check(_non_empty_paths(args), root=root))


def cmd_build_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    raw_targets = getattr(args, "target", None)
    targets = [str(item).strip() for item in raw_targets if str(item).strip()] if isinstance(raw_targets, list) else None
    ok("build_check", build_check(targets=targets, root=root))


def cmd_test_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    raw_targets = getattr(args, "target", None)
    targets = [str(item).strip() for item in raw_targets if str(item).strip()] if isinstance(raw_targets, list) else None
    ok("test_check", test_check(targets=targets, mode=str(args.mode), root=root))


def cmd_runtime_smoke_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    target = str(args.target).strip() if getattr(args, "target", None) else None
    ok("runtime_smoke_check", runtime_smoke_check(target=target, root=root))


def cmd_security_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    raw_paths = getattr(args, "path", None)
    paths = [str(item).strip() for item in raw_paths if str(item).strip()] if isinstance(raw_paths, list) else None
    ok("security_check", security_check(paths=paths, root=root))


def cmd_dead_code_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    ok("dead_code_check", dead_code_check(_non_empty_paths(args), scope=str(args.scope), root=root))


def cmd_duplication_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    ok("duplication_check", duplication_check(_non_empty_paths(args), threshold=int(args.threshold), root=root))


def cmd_policy_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    ok("policy_check", policy_check(_non_empty_paths(args), root=root))


def cmd_changed_files_check(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    ok("changed_files_check", changed_files_check(root=root))


def cmd_project_problems(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    ok("project_problems", project_problems(mode=str(args.mode), root=root))


def _emit_discovery_ok(tool: str, payload: Dict[str, Any]) -> None:
    ok(tool, payload)


def cmd_discovery_list_files(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok(
        "list_files",
        discovery_list_files(
            str(args.path),
            recursive=bool(args.recursive),
            include_hidden=bool(args.hidden),
            limit=int(args.limit),
            max_depth=int(args.max_depth),
            root=root,
        ),
    )


def cmd_discovery_find_files(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    patterns = [str(item).strip() for item in getattr(args, "pattern", []) if str(item).strip()]
    if not patterns:
        err("find_files", "BAD_REQUEST", "Provide at least one --pattern value.")
    _emit_discovery_ok(
        "find_files",
        discovery_find_files(
            patterns,
            path=str(args.path),
            include_hidden=bool(args.hidden),
            limit=int(args.limit),
            root=root,
        ),
    )


def cmd_search_in_files(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok(
        "search_in_files",
        search_in_files(
            str(args.query),
            path=str(args.path),
            literal=bool(args.literal),
            regex=bool(args.regex),
            case_sensitive=bool(args.case_sensitive),
            include_hidden=bool(args.hidden),
            limit=int(args.limit),
            root=root,
        ),
    )


def cmd_outline_file(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok("outline_file", cast(Dict[str, Any], outline_file(str(args.path), root=root)))


def cmd_read_symbol(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok(
        "read_symbol",
        read_symbol(str(args.path), str(args.symbol_name), symbol_kind=str(args.symbol_kind) if args.symbol_kind else None, root=root),
    )


def cmd_find_symbol_definitions(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok(
        "find_symbol_definitions",
        find_symbol_definitions(
            str(args.symbol_name),
            path=str(args.path),
            symbol_kind=str(args.symbol_kind) if args.symbol_kind else None,
            include_hidden=bool(args.hidden),
            limit=int(args.limit),
            root=root,
        ),
    )


def cmd_find_symbol_references(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok(
        "find_symbol_references",
        find_symbol_references(
            str(args.symbol_name),
            path=str(args.path),
            include_hidden=bool(args.hidden),
            limit=int(args.limit),
            root=root,
        ),
    )


def cmd_trace_dependencies(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok(
        "trace_dependencies",
        cast(Dict[str, Any], trace_dependencies(str(args.path), direction=str(args.direction), depth=int(args.depth), root=root)),
    )


def cmd_find_related_files(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok("find_related_files", find_related_files(str(args.path), limit=int(args.limit), root=root))


def cmd_find_related_tests(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok(
        "find_related_tests",
        find_related_tests(str(args.target), path=str(args.path), limit=int(args.limit), root=root),
    )


def cmd_find_related_configs(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok(
        "find_related_configs",
        find_related_configs(str(args.target), path=str(args.path), limit=int(args.limit), root=root),
    )


def cmd_find_canonical_implementation(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok(
        "find_canonical_implementation",
        find_canonical_implementation(str(args.topic), path=str(args.path), limit=int(args.limit), root=root),
    )


def cmd_find_similar_code(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok(
        "find_similar_code",
        find_similar_code(
            query_file=str(args.query_file) if getattr(args, "query_file", None) else None,
            snippet=str(args.snippet) if getattr(args, "snippet", None) else None,
            path=str(args.path),
            limit=int(args.limit),
            root=root,
        ),
    )


def cmd_find_entry_points(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok("find_entry_points", find_entry_points(path=str(args.path), limit=int(args.limit), root=root))


def cmd_find_ownership(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok("find_ownership", find_ownership(str(args.target), path=str(args.path), root=root))


def cmd_recent_changes(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok("recent_changes", recent_changes(path=str(args.path), limit=int(args.limit), root=root))


def cmd_get_changed_files(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok("get_changed_files", discovery_get_changed_files(root=root))


def cmd_semantic_search(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok(
        "semantic_search",
        semantic_search(str(args.intent), path=str(args.path), limit=int(args.limit), root=root),
    )


def cmd_investigate(args: argparse.Namespace) -> None:
    root = resolve_root(args.root)
    _emit_discovery_ok(
        "investigate",
        cast(Dict[str, Any], investigate(str(args.topic), path=str(args.path), mode=str(args.mode), root=root)),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Agent CLI toolbelt")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p = sub.add_parser("ls")
    p.add_argument("--root", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--hidden", action="store_true")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--max-depth", type=int, default=DEFAULT_LIST_DEPTH)
    p.add_argument("--glob")
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("list-files")
    p.add_argument("--root", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--hidden", action="store_true")
    p.add_argument("--recursive", action="store_true")
    p.add_argument("--max-depth", type=int, default=3)
    p.set_defaults(func=cmd_discovery_list_files)

    p = sub.add_parser("read")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--start-line", type=int)
    p.add_argument("--end-line", type=int)
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("inspect")
    p.add_argument("--root", required=True)
    p.add_argument("--spec-file", required=True)
    p.set_defaults(func=cmd_inspect)

    p = sub.add_parser("write")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--content")
    p.add_argument("--input-file")
    p.set_defaults(func=cmd_write)

    p = sub.add_parser("patch")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--search", required=True)
    p.add_argument("--replace", required=True)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_patch)

    p = sub.add_parser("replace-range")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--start-line", required=True, type=int)
    p.add_argument("--end-line", required=True, type=int)
    p.add_argument("--new-text")
    p.add_argument("--input-file")
    p.set_defaults(func=cmd_replace_range)

    p = sub.add_parser("replace-snippet")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--old-text")
    p.add_argument("--old-file")
    p.add_argument("--new-text")
    p.add_argument("--new-file")
    p.add_argument("--expected-occurrences", type=int, default=1)
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_replace_snippet)

    p = sub.add_parser("insert-before")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--anchor-text")
    p.add_argument("--anchor-file")
    p.add_argument("--new-text")
    p.add_argument("--input-file")
    p.add_argument("--expected-occurrences", type=int, default=1)
    p.set_defaults(func=cmd_insert_before)

    p = sub.add_parser("insert-after")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--anchor-text")
    p.add_argument("--anchor-file")
    p.add_argument("--new-text")
    p.add_argument("--input-file")
    p.add_argument("--expected-occurrences", type=int, default=1)
    p.set_defaults(func=cmd_insert_after)

    p = sub.add_parser("delete-range")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--start-line", required=True, type=int)
    p.add_argument("--end-line", required=True, type=int)
    p.set_defaults(func=cmd_delete_range)

    p = sub.add_parser("delete-snippet")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--text")
    p.add_argument("--input-file")
    p.add_argument("--expected-occurrences", type=int, default=1)
    p.set_defaults(func=cmd_delete_snippet)

    p = sub.add_parser("append-block")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--new-text")
    p.add_argument("--input-file")
    p.set_defaults(func=cmd_append_block)

    p = sub.add_parser("prepend-block")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--new-text")
    p.add_argument("--input-file")
    p.set_defaults(func=cmd_prepend_block)

    p = sub.add_parser("replace-symbol")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--symbol-name", required=True)
    p.add_argument("--symbol-kind", required=True)
    p.add_argument("--new-text")
    p.add_argument("--input-file")
    p.set_defaults(func=cmd_replace_symbol)

    p = sub.add_parser("insert-symbol-member")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--container-symbol", required=True)
    p.add_argument("--member-text")
    p.add_argument("--input-file")
    p.add_argument("--position", choices=["start", "end"], default="end")
    p.set_defaults(func=cmd_insert_symbol_member)

    p = sub.add_parser("rename-symbol")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--old-name", required=True)
    p.add_argument("--new-name", required=True)
    p.add_argument("--scope", default="file")
    p.set_defaults(func=cmd_rename_symbol)

    p = sub.add_parser("move-block")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--start-line", required=True, type=int)
    p.add_argument("--end-line", required=True, type=int)
    p.add_argument("--destination-anchor")
    p.add_argument("--anchor-file")
    p.add_argument("--position", choices=["before", "after"], default="after")
    p.set_defaults(func=cmd_move_block)

    p = sub.add_parser("create-file")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--content")
    p.add_argument("--input-file")
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=cmd_create_file)

    p = sub.add_parser("delete-file")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.set_defaults(func=cmd_delete_file)

    p = sub.add_parser("rename-file")
    p.add_argument("--root", required=True)
    p.add_argument("--old-path", required=True)
    p.add_argument("--new-path", required=True)
    p.set_defaults(func=cmd_rename_file)

    p = sub.add_parser("copy-file")
    p.add_argument("--root", required=True)
    p.add_argument("--source-path", required=True)
    p.add_argument("--destination-path", required=True)
    p.add_argument("--overwrite", action="store_true")
    p.set_defaults(func=cmd_copy_file)

    p = sub.add_parser("fill-template")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--slots-file", required=True)
    p.set_defaults(func=cmd_fill_template)

    p = sub.add_parser("batch-mutate")
    p.add_argument("--root", required=True)
    p.add_argument("--spec-file", required=True)
    p.add_argument("--atomic", action="store_true")
    p.set_defaults(func=cmd_batch_mutate)

    p = sub.add_parser("grep")
    p.add_argument("--root", required=True)
    p.add_argument("--pattern", required=True)
    p.add_argument("--path")
    p.add_argument("--glob")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--ignore-case", action="store_true")
    p.add_argument("--fixed-strings", action="store_true")
    p.add_argument("--hidden", action="store_true")
    p.set_defaults(func=cmd_grep)

    p = sub.add_parser("find")
    p.add_argument("--root", required=True)
    p.add_argument("--path")
    p.add_argument("--glob", required=True)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--hidden", action="store_true")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("find-files")
    p.add_argument("--root", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--pattern", action="append", default=[])
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--hidden", action="store_true")
    p.set_defaults(func=cmd_discovery_find_files)

    p = sub.add_parser("search-in-files")
    p.add_argument("--root", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--query", required=True)
    p.add_argument("--literal", action="store_true")
    p.add_argument("--regex", action="store_true")
    p.add_argument("--case-sensitive", action="store_true")
    p.add_argument("--hidden", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_search_in_files)

    p = sub.add_parser("outline-file")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.set_defaults(func=cmd_outline_file)

    p = sub.add_parser("read-symbol")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--symbol-name", required=True)
    p.add_argument("--symbol-kind")
    p.set_defaults(func=cmd_read_symbol)

    p = sub.add_parser("find-symbol-definitions")
    p.add_argument("--root", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--symbol-name", required=True)
    p.add_argument("--symbol-kind")
    p.add_argument("--hidden", action="store_true")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_find_symbol_definitions)

    p = sub.add_parser("find-symbol-references")
    p.add_argument("--root", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--symbol-name", required=True)
    p.add_argument("--hidden", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_find_symbol_references)

    p = sub.add_parser("trace-dependencies")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--direction", choices=["imports", "imported_by", "both"], default="both")
    p.add_argument("--depth", type=int, default=1)
    p.set_defaults(func=cmd_trace_dependencies)

    p = sub.add_parser("find-related-files")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_find_related_files)

    p = sub.add_parser("find-related-tests")
    p.add_argument("--root", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_find_related_tests)

    p = sub.add_parser("find-related-configs")
    p.add_argument("--root", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_find_related_configs)

    p = sub.add_parser("find-canonical-implementation")
    p.add_argument("--root", required=True)
    p.add_argument("--topic", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_find_canonical_implementation)

    p = sub.add_parser("find-similar-code")
    p.add_argument("--root", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--query-file")
    p.add_argument("--snippet")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_find_similar_code)

    p = sub.add_parser("find-entry-points")
    p.add_argument("--root", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_find_entry_points)

    p = sub.add_parser("find-ownership")
    p.add_argument("--root", required=True)
    p.add_argument("--target", required=True)
    p.add_argument("--path", default=".")
    p.set_defaults(func=cmd_find_ownership)

    p = sub.add_parser("recent-changes")
    p.add_argument("--root", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_recent_changes)

    p = sub.add_parser("get-changed-files")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_get_changed_files)

    p = sub.add_parser("semantic-search")
    p.add_argument("--root", required=True)
    p.add_argument("--intent", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_semantic_search)

    p = sub.add_parser("investigate")
    p.add_argument("--root", required=True)
    p.add_argument("--topic", required=True)
    p.add_argument("--path", default=".")
    p.add_argument("--mode", choices=["fast", "standard", "deep"], default="standard")
    p.set_defaults(func=cmd_investigate)

    p = sub.add_parser("summarize")
    p.add_argument("--root", required=True)
    p.add_argument("--path")
    p.add_argument("--glob")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--hidden", action="store_true")
    p.add_argument("--include-content", action="store_true")
    p.set_defaults(func=cmd_summarize)

    p = sub.add_parser("git-status")
    p.add_argument("--root", required=True)
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--ignored", action="store_true")
    p.set_defaults(func=cmd_git_status)

    p = sub.add_parser("git-diff")
    p.add_argument("--root", required=True)
    p.add_argument("--path")
    p.add_argument("--staged", action="store_true")
    p.add_argument("--name-only", action="store_true")
    p.add_argument("--stat", action="store_true")
    p.add_argument("--summary-only", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=cmd_git_diff)

    p = sub.add_parser("review")
    p.add_argument("--root", required=True)
    p.add_argument("--path")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--ignored", action="store_true")
    p.set_defaults(func=cmd_review)

    p = sub.add_parser("symbols")
    p.add_argument("--root", required=True)
    p.add_argument("--path")
    p.add_argument("--glob")
    p.add_argument("--query")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--hidden", action="store_true")
    p.set_defaults(func=cmd_symbols)

    p = sub.add_parser("git-add")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True, action="append")
    p.set_defaults(func=cmd_git_add)

    p = sub.add_parser("git-restore")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True)
    p.add_argument("--staged", action="store_true")
    p.set_defaults(func=cmd_git_restore)

    p = sub.add_parser("git-commit")
    p.add_argument("--root", required=True)
    p.add_argument("--message", required=True)
    p.set_defaults(func=cmd_git_commit)

    p = sub.add_parser("git-log")
    p.add_argument("--root", required=True)
    p.add_argument("--limit", type=int, default=10)
    p.set_defaults(func=cmd_git_log)

    p = sub.add_parser("git-branch")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_git_branch)

    p = sub.add_parser("syntax-check")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True, action="append")
    p.set_defaults(func=cmd_syntax_check)

    p = sub.add_parser("type-check")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True, action="append")
    p.add_argument("--scope", default="changed")
    p.set_defaults(func=cmd_type_check)

    p = sub.add_parser("lint-check")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True, action="append")
    p.add_argument("--scope", default="changed")
    p.set_defaults(func=cmd_lint_check)

    p = sub.add_parser("format-check")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True, action="append")
    p.set_defaults(func=cmd_format_check)

    p = sub.add_parser("config-validate")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True, action="append")
    p.set_defaults(func=cmd_config_validate)

    p = sub.add_parser("schema-validate")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True, action="append")
    p.add_argument("--schema-ref", action="append")
    p.set_defaults(func=cmd_schema_validate)

    p = sub.add_parser("dependency-check")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True, action="append")
    p.set_defaults(func=cmd_dependency_check)

    p = sub.add_parser("build-check")
    p.add_argument("--root", required=True)
    p.add_argument("--target", action="append")
    p.set_defaults(func=cmd_build_check)

    p = sub.add_parser("test-check")
    p.add_argument("--root", required=True)
    p.add_argument("--target", action="append")
    p.add_argument("--mode", default="related")
    p.set_defaults(func=cmd_test_check)

    p = sub.add_parser("runtime-smoke-check")
    p.add_argument("--root", required=True)
    p.add_argument("--target")
    p.set_defaults(func=cmd_runtime_smoke_check)

    p = sub.add_parser("security-check")
    p.add_argument("--root", required=True)
    p.add_argument("--path", action="append")
    p.set_defaults(func=cmd_security_check)

    p = sub.add_parser("dead-code-check")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True, action="append")
    p.add_argument("--scope", default="project")
    p.set_defaults(func=cmd_dead_code_check)

    p = sub.add_parser("duplication-check")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True, action="append")
    p.add_argument("--threshold", type=int, default=30)
    p.set_defaults(func=cmd_duplication_check)

    p = sub.add_parser("policy-check")
    p.add_argument("--root", required=True)
    p.add_argument("--path", required=True, action="append")
    p.set_defaults(func=cmd_policy_check)

    p = sub.add_parser("changed-files-check")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_changed_files_check)

    p = sub.add_parser("project-problems")
    p.add_argument("--root", required=True)
    p.add_argument("--mode", default="standard")
    p.set_defaults(func=cmd_project_problems)

    p = sub.add_parser("run")
    p.add_argument("--root", required=True)
    p.add_argument("--timeout", type=int, default=60)
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("diff")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_diff)

    p = sub.add_parser("meta")
    p.add_argument("--root", required=True)
    p.set_defaults(func=cmd_meta)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.subcommand == "run":
        if not args.command:
            err("run", "MISSING_COMMAND", "Provide a command after 'run'.")
        if args.command and args.command[0] == "--":
            args.command = args.command[1:]

    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
