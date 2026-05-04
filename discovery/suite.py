from __future__ import annotations

import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Optional

from .analysis import (
    collect_symbols_for_file,
    constants_for_file,
    extract_dependencies_for_file,
    locate_symbol_range,
    sections_for_file,
)
from .common import (
    CONFIG_FILENAMES,
    ensure_git_repo,
    iter_files,
    iter_tree,
    matches_glob,
    normalize_relpath,
    path_tokens,
    preview_lines,
    read_text,
    resolve_root,
    run_git,
    safe_join,
    shorten,
    tokenise_query,
    try_read_text,
)
from .models import DependencyTrace, DiscoveryHit, DiscoveryResult, FileOutline, InvestigationResult


def list_files(
    path: str = ".",
    *,
    recursive: bool = True,
    include_hidden: bool = False,
    limit: int = 200,
    max_depth: int = 3,
    root: Path | str | None = None,
) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    base = safe_join(workspace_root, path)
    if not base.exists() or not base.is_dir():
        return _discovery_result(False, path, "list_files", [], {"error": "Path is not a directory."}, path=path)
    iterator: Iterable[Path] = (
        iter_tree(base, include_hidden=include_hidden, max_depth=max_depth)
        if recursive
        else sorted(base.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
    )
    hits: list[DiscoveryHit] = []
    for item in iterator:
        rel = normalize_relpath(workspace_root, item)
        hits.append(
            {
                "file_path": rel,
                "start_line": None,
                "end_line": None,
                "symbol_name": None,
                "symbol_kind": "directory" if item.is_dir() else "file",
                "match_type": "path_entry",
                "preview": item.name,
                "score": None,
                "details": {"is_dir": item.is_dir(), "size": item.stat().st_size if item.is_file() else None},
            }
        )
        if len(hits) >= limit:
            break
    return _discovery_result(
        True,
        path,
        "list_files",
        hits,
        {"count": len(hits), "recursive": recursive, "max_depth": max_depth},
        path=path,
    )


def find_files(
    patterns: list[str],
    *,
    path: str = ".",
    include_hidden: bool = False,
    limit: int = 200,
    root: Path | str | None = None,
) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    base = safe_join(workspace_root, path)
    if not patterns:
        return _discovery_result(False, "", "find_files", [], {"error": "patterns must be non-empty"}, path=path)
    candidates: Iterable[Path] = [base] if base.is_file() else iter_files(base, include_hidden=include_hidden)
    lowered_patterns = [pattern.lower() for pattern in patterns if pattern.strip()]
    hits: list[DiscoveryHit] = []
    for file_path in candidates:
        rel = normalize_relpath(workspace_root, file_path)
        rel_lower = rel.lower()
        basename = file_path.name.lower()
        matched_patterns = [
            pattern
            for pattern in lowered_patterns
            if matches_glob(rel, pattern) or pattern in rel_lower or pattern in basename
        ]
        if not matched_patterns:
            continue
        best_score = max(1.0 if pattern == basename or rel_lower.endswith(pattern) else 0.7 for pattern in matched_patterns)
        hits.append(
            {
                "file_path": rel,
                "start_line": None,
                "end_line": None,
                "symbol_name": None,
                "symbol_kind": "directory" if file_path.is_dir() else "file",
                "match_type": "path_pattern",
                "preview": rel,
                "score": round(best_score, 3),
                "details": {"matched_patterns": matched_patterns},
            }
        )
        if len(hits) >= limit:
            break
    hits.sort(key=lambda item: (-(item.get("score") or 0.0), str(item.get("file_path") or "")))
    return _discovery_result(
        True,
        ", ".join(patterns),
        "find_files",
        hits,
        {"count": len(hits), "patterns": patterns},
        path=path,
    )


def search_in_files(
    query: str,
    *,
    path: str = ".",
    literal: bool = False,
    regex: bool = False,
    case_sensitive: bool = False,
    include_hidden: bool = False,
    limit: int = 200,
    root: Path | str | None = None,
) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    base = safe_join(workspace_root, path)
    if not query.strip():
        return _discovery_result(False, query, "search_in_files", [], {"error": "query must be non-empty"}, path=path)
    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile(query if regex else re.escape(query) if literal else query, flags)
    candidates: Iterable[Path] = [base] if base.is_file() else iter_files(base, include_hidden=include_hidden)
    hits: list[DiscoveryHit] = []
    files_scanned = 0
    files_skipped = 0
    for file_path in candidates:
        text, skip_reason = try_read_text(file_path)
        if text is None:
            files_skipped += 1
            continue
        files_scanned += 1
        rel = normalize_relpath(workspace_root, file_path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not pattern.search(line):
                continue
            hits.append(
                {
                    "file_path": rel,
                    "start_line": lineno,
                    "end_line": lineno,
                    "symbol_name": None,
                    "symbol_kind": None,
                    "match_type": "content_match",
                    "preview": shorten(line.strip(), 240),
                    "score": 1.0,
                    "details": {"line_text": line.rstrip("\n")},
                }
            )
            if len(hits) >= limit:
                return _discovery_result(
                    True,
                    query,
                    "search_in_files",
                    hits,
                    {"count": len(hits), "files_scanned": files_scanned, "files_skipped": files_skipped},
                    path=path,
                )
    return _discovery_result(
        True,
        query,
        "search_in_files",
        hits,
        {"count": len(hits), "files_scanned": files_scanned, "files_skipped": files_skipped},
        path=path,
    )


def outline_file(file_path: str, *, root: Path | str | None = None) -> FileOutline:
    workspace_root = resolve_root(root)
    target = safe_join(workspace_root, file_path)
    text = read_text(target)
    symbols = collect_symbols_for_file(target, text)
    dependencies = extract_dependencies_for_file(target, text, symbols)
    constants = constants_for_file(target, text)
    file_sections = sections_for_file(target, text)
    language = "python" if target.suffix.lower() in {".py", ".pyi"} else target.suffix.lower().lstrip(".") or "text"
    return {
        "ok": True,
        "file_path": normalize_relpath(workspace_root, target),
        "language": language,
        "line_count": len(text.splitlines()),
        "imports": dependencies["imports"],
        "exports": dependencies["exports"],
        "symbols": symbols,
        "constants": constants,
        "sections": file_sections,
        "summary": {
            "symbol_count": len(symbols),
            "import_count": len(dependencies["imports"]),
            "export_count": len(dependencies["exports"]),
            "constant_count": len(constants),
            "section_count": len(file_sections),
        },
    }


def read_symbol(file_path: str, symbol_name: str, symbol_kind: Optional[str] = None, *, root: Path | str | None = None) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    target = safe_join(workspace_root, file_path)
    text = read_text(target)
    symbol_range = locate_symbol_range(target, text, symbol_name, symbol_kind)
    if symbol_range is None:
        return _discovery_result(False, symbol_name, "read_symbol", [], {"error": "symbol not found"}, path=file_path)
    start_line, end_line = symbol_range
    content = "\n".join(text.splitlines()[start_line - 1:end_line])
    hit: DiscoveryHit = {
        "file_path": normalize_relpath(workspace_root, target),
        "start_line": start_line,
        "end_line": end_line,
        "symbol_name": symbol_name,
        "symbol_kind": symbol_kind,
        "match_type": "symbol_body",
        "preview": shorten(content, 500),
        "score": 1.0,
        "is_definition": True,
        "details": {"content": content},
    }
    return _discovery_result(True, symbol_name, "read_symbol", [hit], {"count": 1}, path=file_path)


def find_symbol_definitions(
    symbol_name: str,
    *,
    path: str = ".",
    symbol_kind: Optional[str] = None,
    include_hidden: bool = False,
    limit: int = 100,
    root: Path | str | None = None,
) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    base = safe_join(workspace_root, path)
    candidates: Iterable[Path] = [base] if base.is_file() else iter_files(base, include_hidden=include_hidden)
    lowered_name = symbol_name.lower()
    hits: list[DiscoveryHit] = []
    for file_path in candidates:
        text, skip_reason = try_read_text(file_path)
        if text is None:
            continue
        for symbol in collect_symbols_for_file(file_path, text):
            name = str(symbol.get("name", "") or "")
            qualified = str(symbol.get("qualified_name", "") or "")
            kind = str(symbol.get("kind", "") or "")
            if lowered_name not in {name.lower(), qualified.lower()}:
                continue
            if symbol_kind and kind not in {symbol_kind, f"exported_{symbol_kind}"}:
                continue
            hits.append(
                {
                    "file_path": normalize_relpath(workspace_root, file_path),
                    "start_line": int(symbol.get("line", 0) or 0) or None,
                    "end_line": int(symbol.get("end_line", 0) or 0) or None,
                    "symbol_name": name,
                    "symbol_kind": kind,
                    "match_type": "definition",
                    "preview": str(symbol.get("signature", "") or ""),
                    "score": 1.0 if name.lower() == lowered_name else 0.9,
                    "is_definition": True,
                    "is_reference": False,
                    "details": dict(symbol),
                }
            )
            if len(hits) >= limit:
                return _discovery_result(True, symbol_name, "find_symbol_definitions", hits, {"count": len(hits)}, path=path)
    return _discovery_result(True, symbol_name, "find_symbol_definitions", hits, {"count": len(hits)}, path=path)


def find_symbol_references(
    symbol_name: str,
    *,
    path: str = ".",
    include_hidden: bool = False,
    limit: int = 200,
    root: Path | str | None = None,
) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    base = safe_join(workspace_root, path)
    token = re.compile(rf"\b{re.escape(symbol_name)}\b")
    candidates: Iterable[Path] = [base] if base.is_file() else iter_files(base, include_hidden=include_hidden)
    hits: list[DiscoveryHit] = []
    for file_path in candidates:
        text, skip_reason = try_read_text(file_path)
        if text is None:
            continue
        definitions = {(int(symbol.get("line", 0) or 0), str(symbol.get("name", "") or "")) for symbol in collect_symbols_for_file(file_path, text)}
        rel = normalize_relpath(workspace_root, file_path)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not token.search(line):
                continue
            is_definition = (lineno, symbol_name) in definitions
            hits.append(
                {
                    "file_path": rel,
                    "start_line": lineno,
                    "end_line": lineno,
                    "symbol_name": symbol_name,
                    "symbol_kind": None,
                    "match_type": "definition" if is_definition else "reference",
                    "preview": preview_lines(text, lineno, context_lines=1),
                    "score": 1.0 if is_definition else 0.8,
                    "is_definition": is_definition,
                    "is_reference": not is_definition,
                }
            )
            if len(hits) >= limit:
                return _discovery_result(True, symbol_name, "find_symbol_references", hits, {"count": len(hits)}, path=path)
    return _discovery_result(True, symbol_name, "find_symbol_references", hits, {"count": len(hits)}, path=path)


def trace_dependencies(
    file_path: str,
    *,
    direction: str = "both",
    depth: int = 1,
    root: Path | str | None = None,
) -> DependencyTrace:
    workspace_root = resolve_root(root)
    target = safe_join(workspace_root, file_path)
    rel = normalize_relpath(workspace_root, target)
    graph = _build_dependency_graph(workspace_root)
    imports_hits: list[dict[str, Any]] = []
    imported_by_hits: list[dict[str, Any]] = []
    if direction in {"imports", "both"}:
        imports_hits = _bfs_dependency_edges(graph["imports"], rel, depth)
    if direction in {"imported_by", "both"}:
        imported_by_hits = _bfs_dependency_edges(graph["imported_by"], rel, depth)
    edges = [*imports_hits, *imported_by_hits]
    return {
        "ok": True,
        "file_path": rel,
        "direction": direction,
        "depth": depth,
        "imports": imports_hits,
        "imported_by": imported_by_hits,
        "edges": edges,
        "summary": {
            "import_count": len(imports_hits),
            "imported_by_count": len(imported_by_hits),
            "edge_count": len(edges),
        },
    }


def find_related_files(file_path: str, *, limit: int = 50, root: Path | str | None = None) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    target = safe_join(workspace_root, file_path)
    rel = normalize_relpath(workspace_root, target)
    target_stem = target.stem.replace(".test", "").replace(".spec", "")
    target_tokens = path_tokens(rel)
    scored_hits: list[tuple[float, DiscoveryHit]] = []
    for candidate in iter_files(workspace_root):
        candidate_rel = normalize_relpath(workspace_root, candidate)
        if candidate_rel == rel:
            continue
        score = 0.0
        signals: list[str] = []
        if candidate.parent == target.parent:
            score += 0.3
            signals.append("same_directory")
        if candidate.stem.replace(".test", "").replace(".spec", "") == target_stem:
            score += 0.5
            signals.append("same_stem")
        overlap = len(target_tokens & path_tokens(candidate_rel))
        if overlap:
            score += min(0.4, overlap * 0.08)
            signals.append("token_overlap")
        if _is_companion_pair(rel, candidate_rel):
            score += 0.4
            signals.append("companion_pair")
        if score <= 0:
            continue
        scored_hits.append(
            (
                score,
                {
                    "file_path": candidate_rel,
                    "start_line": None,
                    "end_line": None,
                    "symbol_name": None,
                    "symbol_kind": "file",
                    "match_type": "related_file",
                    "preview": candidate_rel,
                    "score": round(score, 3),
                    "details": {"signals": signals},
                },
            )
        )
    hits = [hit for _, hit in sorted(scored_hits, key=lambda item: (-item[0], item[1]["file_path"]))[:limit]]
    return _discovery_result(True, rel, "find_related_files", hits, {"count": len(hits)}, path=rel)


def find_related_tests(target: str, *, path: str = ".", limit: int = 50, root: Path | str | None = None) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    base = safe_join(workspace_root, path)
    token = Path(target).stem.lower().replace(".test", "").replace(".spec", "")
    hits: list[DiscoveryHit] = []
    for candidate in iter_files(base):
        rel = normalize_relpath(workspace_root, candidate)
        rel_lower = rel.lower()
        if "test" not in rel_lower and "spec" not in rel_lower:
            continue
        if token not in rel_lower and token not in candidate.stem.lower():
            continue
        hits.append(
            {
                "file_path": rel,
                "start_line": None,
                "end_line": None,
                "symbol_name": None,
                "symbol_kind": "test_file",
                "match_type": "related_test",
                "preview": rel,
                "score": 1.0 if token in candidate.stem.lower() else 0.8,
            }
        )
        if len(hits) >= limit:
            break
    return _discovery_result(True, target, "find_related_tests", hits, {"count": len(hits)}, path=path)


def find_related_configs(target: str, *, path: str = ".", limit: int = 50, root: Path | str | None = None) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    base = safe_join(workspace_root, path)
    target_tokens = tokenise_query(target)
    hits: list[DiscoveryHit] = []
    for candidate in iter_files(base, include_hidden=True):
        rel = normalize_relpath(workspace_root, candidate)
        rel_lower = rel.lower()
        if candidate.name not in CONFIG_FILENAMES and not any(
            token in rel_lower for token in {"config", "workflow", "schema", "eslint", "tsconfig", "pytest", "package"}
        ):
            continue
        text, skip_reason = try_read_text(candidate)
        score = 0.4
        signals: list[str] = []
        if candidate.name in CONFIG_FILENAMES:
            score += 0.4
            signals.append("config_filename")
        if text:
            lower_text = text.lower()
            overlap = [token for token in target_tokens if token in lower_text or token in rel_lower]
            if overlap:
                score += min(0.5, len(overlap) * 0.1)
                signals.append("mentions_target")
        hits.append(
            {
                "file_path": rel,
                "start_line": None,
                "end_line": None,
                "symbol_name": None,
                "symbol_kind": "config",
                "match_type": "related_config",
                "preview": rel,
                "score": round(score, 3),
                "details": {"signals": signals},
            }
        )
    hits.sort(key=lambda item: (-(item.get("score") or 0.0), str(item.get("file_path") or "")))
    return _discovery_result(True, target, "find_related_configs", hits[:limit], {"count": min(len(hits), limit)}, path=path)


def find_canonical_implementation(topic: str, *, path: str = ".", limit: int = 20, root: Path | str | None = None) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    base = safe_join(workspace_root, path)
    query_tokens = tokenise_query(topic)
    if not query_tokens:
        return _discovery_result(False, topic, "find_canonical_implementation", [], {"error": "topic must produce search tokens"}, path=path)
    graph = _build_dependency_graph(workspace_root)
    hits: list[DiscoveryHit] = []
    for candidate in iter_files(base, include_hidden=True):
        rel = normalize_relpath(workspace_root, candidate)
        rel_lower = rel.lower()
        text, skip_reason = try_read_text(candidate)
        if text is None:
            continue
        lower_text = text.lower()
        matched = [token for token in query_tokens if token in rel_lower or token in lower_text]
        if not matched:
            continue
        score = min(1.2, len(matched) * 0.2)
        if any(token in candidate.stem.lower() for token in query_tokens):
            score += 0.4
        imported_by_count = len(graph["imported_by"].get(rel, []))
        score += min(0.7, imported_by_count * 0.15)
        symbols = collect_symbols_for_file(candidate, text)
        exports = extract_dependencies_for_file(candidate, text, symbols)["exports"]
        score += min(0.4, len(exports) * 0.1)
        if any(part in rel_lower for part in ["test", "spec", "mock", "fixture", "example", "dist", "build", "generated"]):
            score -= 0.9
        if any(part in rel_lower for part in ["readme", "changes.md", "planning/"]):
            score -= 0.5
        if score <= 0:
            continue
        preview = symbols[0]["signature"] if symbols else text.splitlines()[0] if text.splitlines() else rel
        hits.append(
            {
                "file_path": rel,
                "start_line": None,
                "end_line": None,
                "symbol_name": None,
                "symbol_kind": "file",
                "match_type": "canonical_candidate",
                "preview": shorten(preview, 240),
                "score": round(score, 3),
                "is_canonical_candidate": True,
                "details": {
                    "imports_count": len(graph["imports"].get(rel, [])),
                    "imported_by_count": imported_by_count,
                    "matched_tokens": matched,
                },
            }
        )
    hits.sort(key=lambda item: (-(item.get("score") or 0.0), str(item.get("file_path") or "")))
    return _discovery_result(True, topic, "find_canonical_implementation", hits[:limit], {"count": min(len(hits), limit)}, path=path)


def find_similar_code(
    *,
    query_file: Optional[str] = None,
    snippet: Optional[str] = None,
    path: str = ".",
    limit: int = 20,
    root: Path | str | None = None,
) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    base = safe_join(workspace_root, path)
    query_label = query_file or snippet or ""
    query_text = read_text(safe_join(workspace_root, query_file)) if query_file else snippet or ""
    query_tokens = set(tokenise_query(query_text))
    if not query_tokens:
        return _discovery_result(False, query_label, "find_similar_code", [], {"error": "query_file or snippet must provide searchable tokens"}, path=path)
    hits: list[DiscoveryHit] = []
    for candidate in iter_files(base):
        rel = normalize_relpath(workspace_root, candidate)
        if query_file and rel == query_file:
            continue
        text, skip_reason = try_read_text(candidate)
        if text is None:
            continue
        overlap = query_tokens & set(tokenise_query(text))
        if not overlap:
            continue
        score = len(overlap) / max(1, len(query_tokens | set(tokenise_query(text))))
        if score < 0.03:
            continue
        hits.append(
            {
                "file_path": rel,
                "start_line": None,
                "end_line": None,
                "symbol_name": None,
                "symbol_kind": "file",
                "match_type": "similar_code",
                "preview": shorten(text[:300], 240),
                "score": round(score, 3),
                "details": {"overlap_tokens": sorted(overlap)[:20]},
            }
        )
    hits.sort(key=lambda item: (-(item.get("score") or 0.0), str(item.get("file_path") or "")))
    return _discovery_result(True, query_label, "find_similar_code", hits[:limit], {"count": min(len(hits), limit)}, path=path)


def find_entry_points(*, path: str = ".", limit: int = 50, root: Path | str | None = None) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    base = safe_join(workspace_root, path)
    candidate_scores = {
        "main.py": 1.0,
        "__main__.py": 1.0,
        "app.py": 0.9,
        "server.py": 0.9,
        "manage.py": 0.9,
        "package.json": 0.8,
        "extension.ts": 0.85,
        "runTest.ts": 0.7,
    }
    hits: list[DiscoveryHit] = []
    for candidate in iter_files(base, include_hidden=True):
        rel = normalize_relpath(workspace_root, candidate)
        rel_lower = rel.lower()
        score = candidate_scores.get(candidate.name, 0.0)
        signals: list[str] = []
        if candidate.name in candidate_scores:
            signals.append("entry_filename")
        if any(token in rel_lower for token in ["entry", "bootstrap", "startup", "cli", "main", "server", "extension"]):
            score = max(score, 0.6)
            signals.append("entry_path")
        if candidate.name == "package.json":
            text, skip_reason = try_read_text(candidate)
            if text and '"scripts"' in text:
                score += 0.1
                signals.append("package_scripts")
        if score <= 0:
            continue
        hits.append(
            {
                "file_path": rel,
                "start_line": None,
                "end_line": None,
                "symbol_name": None,
                "symbol_kind": "entry_point",
                "match_type": "entry_point",
                "preview": rel,
                "score": round(score, 3),
                "details": {"signals": signals},
            }
        )
    hits.sort(key=lambda item: (-(item.get("score") or 0.0), str(item.get("file_path") or "")))
    return _discovery_result(True, path, "find_entry_points", hits[:limit], {"count": min(len(hits), limit)}, path=path)


def find_ownership(target: str, *, path: str = ".", root: Path | str | None = None) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    normalized_target = normalize_relpath(workspace_root, safe_join(workspace_root, target)) if target else path
    folder = Path(normalized_target).parent
    dominant_domain = "/".join(folder.parts[:2]) if folder.parts else "."
    hits: list[DiscoveryHit] = []
    for pattern, owners in _load_codeowners(workspace_root):
        if matches_glob(normalized_target, pattern.lstrip("/")):
            hits.append(
                {
                    "file_path": normalized_target,
                    "start_line": None,
                    "end_line": None,
                    "symbol_name": None,
                    "symbol_kind": "ownership",
                    "match_type": "codeowners",
                    "preview": " ".join(owners),
                    "score": 1.0,
                    "details": {"pattern": pattern, "owners": owners},
                }
            )
    hits.append(
        {
            "file_path": normalized_target,
            "start_line": None,
            "end_line": None,
            "symbol_name": None,
            "symbol_kind": "ownership",
            "match_type": "dominant_folder",
            "preview": dominant_domain,
            "score": 0.7,
            "details": {"dominant_domain": dominant_domain, "folder": str(folder).replace("\\", "/")},
        }
    )
    return _discovery_result(True, target, "find_ownership", hits, {"count": len(hits)}, path=path)


def recent_changes(*, path: str = ".", limit: int = 20, root: Path | str | None = None) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    if not ensure_git_repo(workspace_root):
        return _discovery_result(False, path, "recent_changes", [], {"error": "not a git repository"}, path=path)
    result = run_git(
        workspace_root,
        ["log", f"--max-count={limit}", "--name-only", "--pretty=format:%H%x1f%h%x1f%ad%x1f%s", "--date=short", "--", path],
        timeout=20,
    )
    if result.returncode != 0:
        return _discovery_result(False, path, "recent_changes", [], {"error": result.stderr.strip() or "git log failed"}, path=path)
    hits: list[DiscoveryHit] = []
    current_commit: dict[str, Any] | None = None
    for line in result.stdout.splitlines():
        if "\x1f" in line:
            full_hash, short_hash, date, subject = line.split("\x1f", 3)
            current_commit = {"hash": full_hash, "short_hash": short_hash, "date": date, "subject": subject}
            continue
        if not line.strip() or current_commit is None:
            continue
        hits.append(
            {
                "file_path": line.strip(),
                "start_line": None,
                "end_line": None,
                "symbol_name": None,
                "symbol_kind": "changed_file",
                "match_type": "recent_change",
                "preview": current_commit["subject"],
                "score": None,
                "details": dict(current_commit),
            }
        )
    return _discovery_result(True, path, "recent_changes", hits[:limit], {"count": min(len(hits), limit)}, path=path)


def get_changed_files(*, root: Path | str | None = None) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    if not ensure_git_repo(workspace_root):
        return _discovery_result(False, ".", "get_changed_files", [], {"error": "not a git repository"}, path=".")
    result = run_git(workspace_root, ["status", "--short"], timeout=10)
    if result.returncode != 0:
        return _discovery_result(False, ".", "get_changed_files", [], {"error": result.stderr.strip() or "git status failed"}, path=".")
    hits: list[DiscoveryHit] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        xy = line[:2]
        path_part = line[3:]
        if " -> " in path_part:
            _, path_part = path_part.split(" -> ", 1)
        hits.append(
            {
                "file_path": path_part,
                "start_line": None,
                "end_line": None,
                "symbol_name": None,
                "symbol_kind": "changed_file",
                "match_type": "git_status",
                "preview": xy,
                "score": None,
                "details": {"xy": xy},
            }
        )
    return _discovery_result(True, ".", "get_changed_files", hits, {"count": len(hits)}, path=".")


def semantic_search(intent: str, *, path: str = ".", limit: int = 20, root: Path | str | None = None) -> DiscoveryResult:
    workspace_root = resolve_root(root)
    base = safe_join(workspace_root, path)
    intent_tokens = tokenise_query(intent)
    if not intent_tokens:
        return _discovery_result(False, intent, "semantic_search", [], {"error": "intent must contain searchable tokens"}, path=path)
    hits: list[DiscoveryHit] = []
    for candidate in iter_files(base, include_hidden=True):
        rel = normalize_relpath(workspace_root, candidate)
        text, skip_reason = try_read_text(candidate)
        if text is None:
            continue
        symbols = collect_symbols_for_file(candidate, text)
        haystack_tokens = path_tokens(rel) | set(tokenise_query(text[:4000]))
        overlap = set(intent_tokens) & haystack_tokens
        if not overlap:
            continue
        score = len(overlap) / max(1, len(intent_tokens))
        if any(token in rel.lower() for token in intent_tokens):
            score += 0.2
        if symbols:
            score += min(0.2, len(symbols) * 0.02)
        preview = symbols[0].get("signature", text.splitlines()[0] if text.splitlines() else rel) if symbols else rel
        hits.append(
            {
                "file_path": rel,
                "start_line": int(symbols[0].get("line", 0) or 0) or None if symbols else None,
                "end_line": int(symbols[0].get("end_line", 0) or 0) or None if symbols else None,
                "symbol_name": str(symbols[0].get("name", "") or "") if symbols else None,
                "symbol_kind": str(symbols[0].get("kind", "") or "") if symbols else None,
                "match_type": "semantic_candidate",
                "preview": shorten(str(preview), 240),
                "score": round(score, 3),
                "details": {"matched_tokens": sorted(overlap)},
            }
        )
    hits.sort(key=lambda item: (-(item.get("score") or 0.0), str(item.get("file_path") or "")))
    return _discovery_result(True, intent, "semantic_search", hits[:limit], {"count": min(len(hits), limit)}, path=path)


def investigate(topic: str, *, path: str = ".", mode: str = "standard", root: Path | str | None = None) -> InvestigationResult:
    workspace_root = resolve_root(root)
    safe_join(workspace_root, path)
    mode_limits = {"fast": 5, "standard": 10, "deep": 20}
    limit = mode_limits.get(mode, 10)
    semantic = semantic_search(topic, path=path, limit=limit, root=workspace_root)
    canonical = find_canonical_implementation(topic, path=path, limit=limit, root=workspace_root)
    definitions = find_symbol_definitions(topic, path=path, limit=limit, root=workspace_root)
    topic_related_tests = find_related_tests(topic, path=path, limit=limit, root=workspace_root)
    topic_related_configs = find_related_configs(topic, path=path, limit=limit, root=workspace_root)

    likely_edit_targets: list[str] = []
    for source in [canonical.get("hits", []), definitions.get("hits", []), semantic.get("hits", [])]:
        for hit in source:
            file_path = str(hit.get("file_path", "") or "")
            if file_path and file_path not in likely_edit_targets:
                likely_edit_targets.append(file_path)

    related_test_paths = [str(hit.get("file_path", "") or "") for hit in topic_related_tests.get("hits", [])]
    related_config_paths = [str(hit.get("file_path", "") or "") for hit in topic_related_configs.get("hits", [])]
    for candidate in likely_edit_targets[:3]:
        candidate_tests = find_related_tests(candidate, path=path, limit=limit, root=workspace_root)
        candidate_configs = find_related_configs(candidate, path=path, limit=limit, root=workspace_root)
        for hit in candidate_tests.get("hits", []):
            file_path = str(hit.get("file_path", "") or "")
            if file_path and file_path not in related_test_paths:
                related_test_paths.append(file_path)
        for hit in candidate_configs.get("hits", []):
            file_path = str(hit.get("file_path", "") or "")
            if file_path and file_path not in related_config_paths:
                related_config_paths.append(file_path)

    dependency_edges: list[dict[str, Any]] = []
    for candidate in likely_edit_targets[:3]:
        trace = trace_dependencies(candidate, direction="both", depth=2 if mode == "deep" else 1, root=workspace_root)
        dependency_edges.extend(trace.get("edges", []))

    related_files: list[str] = []
    for candidate in likely_edit_targets[:3]:
        result = find_related_files(candidate, limit=5, root=workspace_root)
        for hit in result.get("hits", []):
            file_path = str(hit.get("file_path", "") or "")
            if file_path and file_path not in related_files:
                related_files.append(file_path)

    read_order = [*likely_edit_targets[:limit]]
    extras = [
        *related_test_paths,
        *related_config_paths,
        *related_files,
    ]
    for extra in extras:
        if extra and extra not in read_order:
            read_order.append(extra)

    notes: list[str] = []
    if likely_edit_targets:
        notes.append(f"Top edit candidate: {likely_edit_targets[0]}")
    if canonical.get("hits"):
        notes.append("Canonicality favors exported and imported-by files over tests and examples.")
    if related_test_paths:
        notes.append("Related tests were found and should be reviewed before mutation.")
    if related_config_paths:
        notes.append("Config coupling was detected and should be reviewed before structural edits.")

    return {
        "ok": True,
        "topic": topic,
        "path": path,
        "mode": mode,
        "canonical_candidates": [str(hit.get("file_path", "") or "") for hit in canonical.get("hits", [])[:limit]],
        "likely_edit_targets": likely_edit_targets[:limit],
        "related_tests": related_test_paths[:limit],
        "related_configs": related_config_paths[:limit],
        "relevant_symbols": [
            {
                "file_path": hit.get("file_path"),
                "symbol_name": hit.get("symbol_name"),
                "symbol_kind": hit.get("symbol_kind"),
                "start_line": hit.get("start_line"),
            }
            for hit in definitions.get("hits", [])[:limit]
        ],
        "dependency_edges": dependency_edges[: limit * 2],
        "recommended_read_order": read_order[: limit * 2],
        "notes": notes,
        "summary": {
            "canonical_candidate_count": len(canonical.get("hits", [])),
            "edit_target_count": len(likely_edit_targets),
            "related_test_count": len(related_test_paths),
            "related_config_count": len(related_config_paths),
            "dependency_edge_count": len(dependency_edges),
        },
        "details": {
            "semantic_hits": semantic.get("hits", [])[:limit],
            "related_files": related_files[:limit],
        },
    }


def _build_dependency_graph(root: Path) -> dict[str, dict[str, list[dict[str, Any]]]]:
    imports_graph: dict[str, list[dict[str, Any]]] = defaultdict(list)
    imported_by_graph: dict[str, list[dict[str, Any]]] = defaultdict(list)
    file_index = _workspace_file_index(root)
    for candidate in iter_files(root):
        rel = normalize_relpath(root, candidate)
        text, skip_reason = try_read_text(candidate)
        if text is None:
            continue
        dependencies = extract_dependencies_for_file(candidate, text, collect_symbols_for_file(candidate, text))
        for item in dependencies["imports"]:
            module = str(item.get("module", "") or "")
            resolved = _resolve_import_to_file(root, candidate, module, file_index)
            edge = {"from": rel, "to": resolved or module, "module": module, "line": item.get("line")}
            imports_graph[rel].append(edge)
            if resolved:
                imported_by_graph[resolved].append(edge)
    return {"imports": imports_graph, "imported_by": imported_by_graph}


def _workspace_file_index(root: Path) -> dict[str, str]:
    index: dict[str, str] = {}
    for candidate in iter_files(root):
        rel = normalize_relpath(root, candidate)
        index[rel] = rel
        index[candidate.stem] = rel
        dotted = rel.replace("/", ".")
        index[dotted] = rel
        for suffix in [".py", ".ts", ".tsx", ".js", ".jsx"]:
            if rel.endswith(suffix):
                index[rel[: -len(suffix)]] = rel
                index[dotted[: -len(suffix)]] = rel
    return index


def _resolve_import_to_file(root: Path, current_file: Path, module: str, file_index: dict[str, str]) -> Optional[str]:
    normalized = module.strip()
    if not normalized:
        return None
    if normalized.startswith("."):
        base = current_file.parent
        relative = normalized
        while relative.startswith("."):
            relative = relative[1:]
            if base != root:
                base = base.parent
        remainder = relative.lstrip("/").replace(".", "/")
        for suffix in ["", ".py", ".ts", ".tsx", ".js", ".jsx"]:
            candidate = (base / f"{remainder}{suffix}").resolve()
            if candidate.exists() and candidate.is_file():
                return normalize_relpath(root, candidate)
        return None
    if normalized in file_index:
        return file_index[normalized]
    module_path = normalized.replace(".", "/")
    if module_path in file_index:
        return file_index[module_path]
    for suffix in [".py", ".ts", ".tsx", ".js", ".jsx"]:
        if module_path + suffix in file_index:
            return file_index[module_path + suffix]
    return None


def _bfs_dependency_edges(graph: dict[str, list[dict[str, Any]]], start: str, depth: int) -> list[dict[str, Any]]:
    seen: set[tuple[str, str]] = set()
    queue: deque[tuple[str, int]] = deque([(start, 0)])
    out: list[dict[str, Any]] = []
    while queue:
        current, level = queue.popleft()
        if level >= depth:
            continue
        for edge in graph.get(current, []):
            destination = str(edge.get("to", edge.get("from", "")) or "")
            key = (str(edge.get("from", "") or ""), destination)
            if key in seen:
                continue
            seen.add(key)
            edge_copy = dict(edge)
            edge_copy["depth"] = level + 1
            out.append(edge_copy)
            queue.append((destination, level + 1))
    return out


def _is_companion_pair(left: str, right: str) -> bool:
    pairs = [
        ("controller", "service"),
        ("service", "repo"),
        ("schema", "route"),
        ("panel", "model"),
        ("component", "test"),
    ]
    left_lower = left.lower()
    right_lower = right.lower()
    return any((first in left_lower and second in right_lower) or (second in left_lower and first in right_lower) for first, second in pairs)


def _load_codeowners(root: Path) -> list[tuple[str, list[str]]]:
    for rel in ["CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"]:
        candidate = root / rel
        if not candidate.exists() or not candidate.is_file():
            continue
        entries: list[tuple[str, list[str]]] = []
        for line in candidate.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            parts = stripped.split()
            if len(parts) < 2:
                continue
            entries.append((parts[0], parts[1:]))
        return entries
    return []


def _discovery_result(
    ok: bool,
    query: str,
    result_type: str,
    hits: list[DiscoveryHit],
    summary: dict[str, Any],
    *,
    path: str,
) -> DiscoveryResult:
    return {
        "ok": ok,
        "query": query,
        "result_type": result_type,
        "hits": hits,
        "summary": summary,
        "path": path,
    }
