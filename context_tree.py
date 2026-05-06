"""
ContextTree — An in-context virtual filesystem for the agent.

Mounts:
  /repo     Real workspace files (metadata preloaded, content lazily cached)
  /facts    Issue-scoped durable facts as addressable paths
  /memory   Memory items addressable by ID
  /status   Agent state flags (read-only view)
  /skills   Registered skill definitions with preloaded caches

Design goals:
  - Reads are FREE: the agent can ls/cat/find/grep the tree without a tool call.
  - Writes stay as real tools: patch_file, write_file, run_shell, git_* still
    dispatch to ToolbeltRunner.
  - Facts become tree paths instead of flat key-value stores.
  - Skills deliver their payload via preloaded cache — no IO at query time.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import time
import ast
from json import JSONDecoder
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

_CAT_MAX_FULL_LINES = 200
_CAT_MAX_FULL_CHARS = 20000
_CAT_PREVIEW_LINES = 80


# ---------------------------------------------------------------------------
# Node types
# ---------------------------------------------------------------------------

@dataclass
class TreeNode:
    """Base tree node."""
    name: str
    parent: Optional["TreeNode"] = field(default=None, repr=False)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def path(self) -> str:
        parts: list[str] = []
        node: Optional[TreeNode] = self
        while node is not None:
            if node.name:  # skip empty root name
                parts.append(node.name)
            node = node.parent
        return "/".join(reversed(parts))

    def is_dir(self) -> bool:
        return isinstance(self, DirNode)

    def is_file(self) -> bool:
        return isinstance(self, FileNode)


@dataclass
class FileNode(TreeNode):
    """Leaf node — may lazily load content."""
    _content: Optional[str] = field(default=None, repr=False)
    _loader: Optional[Callable[[], str]] = field(default=None, repr=False)
    size: int = 0
    lines: int = 0

    @property
    def content(self) -> str:
        if self._content is None and self._loader is not None:
            self.content = self._loader()
        return self._content or ""

    @content.setter
    def content(self, value: str) -> None:
        self._content = value
        self.lines = value.count("\n") + 1 if value else 0
        self.size = len(value.encode("utf-8", errors="replace"))

    def content_loaded(self) -> bool:
        return self._content is not None

    def stat(self) -> Dict[str, Any]:
        info: Dict[str, Any] = {"name": self.name, "type": "file", "path": self.path(), "size": self.size, "lines": self.lines}
        info.update(self.metadata)
        return info


@dataclass
class DirNode(TreeNode):
    """Interior node — directory with children."""
    children: Dict[str, TreeNode] = field(default_factory=dict)

    def add_dir(self, name: str, **meta: Any) -> "DirNode":
        existing = self.children.get(name)
        if isinstance(existing, DirNode):
            existing.metadata.update(meta)
            return existing
        d = DirNode(name=name, parent=self, metadata=dict(meta))
        self.children[name] = d
        return d

    def add_file(
        self,
        name: str,
        *,
        content: Optional[str] = None,
        loader: Optional[Callable[[], str]] = None,
        size: int = 0,
        lines: int = 0,
        **meta: Any,
    ) -> FileNode:
        f = FileNode(
            name=name,
            parent=self,
            _content=content,
            _loader=loader,
            size=size,
            lines=lines,
            metadata=dict(meta),
        )
        self.children[name] = f
        return f

    def get(self, name: str) -> Optional[TreeNode]:
        return self.children.get(name)

    def ls(self, depth: int = 1, _current: int = 0) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        for child in sorted(self.children.values(), key=lambda c: (not c.is_dir(), c.name)):
            if child.is_dir():
                assert isinstance(child, DirNode)
                count = len(child.children)
                entry: Dict[str, Any] = {"name": child.name + "/", "type": "dir", "children": count}
                entry.update(child.metadata)
                entries.append(entry)
                if _current + 1 < depth:
                    for sub in child.ls(depth=depth, _current=_current + 1):
                        sub["name"] = child.name + "/" + sub["name"]
                        entries.append(sub)
            else:
                assert isinstance(child, FileNode)
                entries.append(child.stat())
        return entries

    def stat(self) -> Dict[str, Any]:
        return {"type": "dir", "path": self.path(), "children": len(self.children)}

    def walk(self) -> Iterator[TreeNode]:
        for child in self.children.values():
            yield child
            if isinstance(child, DirNode):
                yield from child.walk()


# ---------------------------------------------------------------------------
# Resolve helper
# ---------------------------------------------------------------------------

def _resolve(root: DirNode, path: str) -> Optional[TreeNode]:
    """Resolve a /-delimited path from root."""
    parts = [p for p in path.strip("/").split("/") if p and p != "."]
    node: TreeNode = root
    for part in parts:
        if not isinstance(node, DirNode):
            return None
        child = node.children.get(part)
        if child is None:
            return None
        node = child
    return node


# ---------------------------------------------------------------------------
# Skill registry
# ---------------------------------------------------------------------------

@dataclass
class SkillDefinition:
    """A registered skill the agent can invoke."""
    name: str
    description: str
    args_schema: Dict[str, Any] = field(default_factory=dict)
    tags: List[str] = field(default_factory=list)
    category: str = "general"
    priority: int = 0
    modes: List[str] = field(default_factory=list)
    cache: Optional[str] = None  # Preloaded payload (file content, data, etc.)
    handler: Optional[Callable[..., str]] = None  # Optional dynamic handler

    def execute(self, **kwargs: Any) -> str:
        if self.handler is not None:
            return self.handler(**kwargs)
        if self.cache is not None:
            return self.cache
        return f"Skill '{self.name}' has no handler or cache."


# ---------------------------------------------------------------------------
# ContextTree
# ---------------------------------------------------------------------------

class ContextTree:
    """
    Virtual filesystem the agent uses for zero-cost reads.

    Mount points:
      /repo     — real workspace
      /facts    — durable facts
      /memory   — memory items
      /status   — agent state
      /skills   — skill registry
    """

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self._root = DirNode(name="")
        self._skills: Dict[str, SkillDefinition] = {}

        # Create mount dirs
        self._repo = self._root.add_dir("repo")
        self._facts = self._root.add_dir("facts")
        self._memory = self._root.add_dir("memory")
        self._status = self._root.add_dir("status")
        self._skills_dir = self._root.add_dir("skills")

        # Caches
        self._repo_indexed = False
        self._content_cache: Dict[str, str] = {}  # rel_path → content

    LOG_ISSUES_ROOT = "log-issues"

    def _probe_repo_file(self, full_path: Path) -> Tuple[int, int]:
        """Return (size_bytes, line_count) without populating file content cache."""
        try:
            raw = full_path.read_bytes()
        except Exception:
            return 0, 0
        size = len(raw)
        if not raw:
            return size, 0
        return size, raw.count(b"\n") + (0 if raw.endswith(b"\n") else 1)

    # ------------------------------------------------------------------
    # /repo — Workspace indexing
    # ------------------------------------------------------------------

    def index_repo(
        self,
        *,
        max_files: int = 5000,
        exclude_dirs: Optional[set[str]] = None,
        symbols_fn: Optional[Callable[[str], List[str]]] = None,
    ) -> int:
        """Walk the real workspace and populate /repo with metadata nodes.
        File content is lazy-loaded on first access.
        Returns the number of files indexed."""
        if exclude_dirs is None:
            exclude_dirs = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build", ".tox", ".mypy_cache", ".pytest_cache"}

        count = 0
        root_str = str(self.workspace_root)

        for dirpath, dirnames, filenames in os.walk(root_str):
            # Prune excluded dirs in-place
            dirnames[:] = [d for d in dirnames if d not in exclude_dirs]

            rel_dir = os.path.relpath(dirpath, root_str)
            if rel_dir == ".":
                parent = self._repo
            else:
                parent = self._repo
                for part in rel_dir.split(os.sep):
                    parent = parent.add_dir(part)

            for fname in sorted(filenames):
                if count >= max_files:
                    break
                full = os.path.join(dirpath, fname)
                rel = os.path.relpath(full, root_str)
                file_size, line_count = self._probe_repo_file(Path(full))

                # Lazy content loader closure
                _full = full
                def _make_loader(p: str) -> Callable[[], str]:
                    def _load() -> str:
                        try:
                            return Path(p).read_text(errors="replace")
                        except Exception:
                            return ""
                    return _load

                meta: Dict[str, Any] = {}
                if symbols_fn:
                    try:
                        meta["symbols"] = symbols_fn(rel)
                    except Exception:
                        pass

                parent.add_file(
                    fname,
                    loader=_make_loader(_full),
                    size=file_size,
                    lines=line_count,
                    **meta,
                )
                count += 1

            if count >= max_files:
                break

        self._repo_indexed = True
        return count

    def preload_files(self, rel_paths: Sequence[str]) -> int:
        """Eagerly load content for specific files (e.g., planner-selected hot files)."""
        loaded = 0
        for rp in rel_paths:
            node = _resolve(self._repo, rp)
            if isinstance(node, FileNode) and not node.content_loaded():
                _ = node.content  # trigger lazy load
                loaded += 1
        return loaded

    def _ensure_repo_parent_dir(self, rel_path: str) -> DirNode:
        parent = self._repo
        rel_dir = os.path.dirname(rel_path)
        if not rel_dir or rel_dir == ".":
            return parent
        for part in Path(rel_dir).parts:
            parent = parent.add_dir(part)
        return parent

    def _make_repo_loader(self, full_path: Path) -> Callable[[], str]:
        def _load() -> str:
            try:
                return full_path.read_text(errors="replace")
            except Exception:
                return ""
        return _load

    def refresh_repo_file(self, rel_path: str) -> None:
        """Refresh one /repo file node after a write, create, or delete."""
        rel_path = str(rel_path or "").strip().removeprefix("/repo/").removeprefix("repo/")
        if not rel_path:
            return

        target = self.workspace_root / rel_path
        parent = self._ensure_repo_parent_dir(rel_path)
        filename = Path(rel_path).name

        if not target.exists():
            parent.children.pop(filename, None)
            return

        try:
            size, line_count = self._probe_repo_file(target)
        except OSError:
            size, line_count = 0, 0

        existing = parent.get(filename)
        if isinstance(existing, FileNode):
            existing._loader = self._make_repo_loader(target)
            existing._content = None
            existing.size = size
            existing.lines = line_count
            return

        parent.add_file(
            filename,
            loader=self._make_repo_loader(target),
            size=size,
            lines=line_count,
        )

    def invalidate_file(self, rel_path: str) -> None:
        """Clear cached content for a file after a write."""
        self.refresh_repo_file(rel_path)

    # ------------------------------------------------------------------
    # Symbol indexing / lookup
    # ------------------------------------------------------------------

    def _extract_python_symbols(self, content: str) -> List[Dict[str, Any]]:
        try:
            tree = ast.parse(content)
        except SyntaxError:
            return []

        symbols: List[Dict[str, Any]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                symbols.append({
                    "name": node.name,
                    "kind": "class",
                    "line": int(getattr(node, "lineno", 0) or 0),
                    "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 0)) or 0),
                })
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append({
                    "name": node.name,
                    "kind": "function",
                    "line": int(getattr(node, "lineno", 0) or 0),
                    "end_line": int(getattr(node, "end_lineno", getattr(node, "lineno", 0)) or 0),
                })
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        symbols.append({
                            "name": target.id,
                            "kind": "variable",
                            "line": int(getattr(target, "lineno", getattr(node, "lineno", 0)) or 0),
                            "end_line": int(getattr(target, "end_lineno", getattr(node, "lineno", 0)) or 0),
                        })
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                symbols.append({
                    "name": node.target.id,
                    "kind": "variable",
                    "line": int(getattr(node.target, "lineno", getattr(node, "lineno", 0)) or 0),
                    "end_line": int(getattr(node.target, "end_lineno", getattr(node, "lineno", 0)) or 0),
                })
        return sorted(symbols, key=lambda item: (item["line"], item["name"], item["kind"]))

    def _extract_jsts_symbols(self, content: str) -> List[Dict[str, Any]]:
        patterns: List[Tuple[str, re.Pattern[str]]] = [
            ("class", re.compile(r"^\s*export\s+default\s+class\s+([A-Za-z_$][\w$]*)\b|^\s*(?:export\s+)?class\s+([A-Za-z_$][\w$]*)\b")),
            ("function", re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)\s*\(")),
            ("function", re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(")),
            ("function", re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\b")),
            ("function", re.compile(r"^\s*(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*<[^>]+>\s*\(")),
            ("interface", re.compile(r"^\s*export\s+interface\s+([A-Za-z_$][\w$]*)\b|^\s*interface\s+([A-Za-z_$][\w$]*)\b")),
            ("type", re.compile(r"^\s*export\s+type\s+([A-Za-z_$][\w$]*)\b|^\s*type\s+([A-Za-z_$][\w$]*)\b")),
            ("enum", re.compile(r"^\s*export\s+enum\s+([A-Za-z_$][\w$]*)\b|^\s*enum\s+([A-Za-z_$][\w$]*)\b")),
            ("variable", re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=")),
        ]

        symbols: List[Dict[str, Any]] = []
        for idx, line in enumerate(content.splitlines(), start=1):
            for kind, pattern in patterns:
                match = pattern.search(line)
                if not match:
                    continue
                name = next((group for group in match.groups() if group), "")
                if not name:
                    continue
                symbols.append({
                    "name": name,
                    "kind": kind,
                    "line": idx,
                    "end_line": idx,
                })
                break
        return symbols

    def extract_symbols(self, path: str) -> List[Dict[str, Any]]:
        node = _resolve(self._root, path)
        if not isinstance(node, FileNode):
            return []

        content = node.content
        suffix = Path(path).suffix.lower()
        if suffix == ".py":
            return self._extract_python_symbols(content)
        if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
            return self._extract_jsts_symbols(content)
        return []

    def find_symbols(self, path: str, name: str, limit: int = 20) -> List[Dict[str, Any]]:
        node = _resolve(self._root, path)
        if node is None:
            return []

        matches: List[Dict[str, Any]] = []
        needle = name.strip()
        if not needle:
            return matches

        def _scan_file(file_node: FileNode) -> None:
            nonlocal matches
            if len(matches) >= limit:
                return
            file_path = "/" + file_node.path()
            for symbol in self.extract_symbols(file_path):
                if symbol.get("name") != needle:
                    continue
                match = dict(symbol)
                match["path"] = file_path
                matches.append(match)
                if len(matches) >= limit:
                    return

        if isinstance(node, FileNode):
            _scan_file(node)
            return matches

        assert isinstance(node, DirNode)
        for child in node.walk():
            if len(matches) >= limit:
                break
            if isinstance(child, FileNode):
                _scan_file(child)
        return matches

    # ------------------------------------------------------------------
    # /facts — Durable fact tree
    # ------------------------------------------------------------------

    def sync_facts(self, records: Sequence[Any]) -> None:
        """Rebuild /facts from IssueFactRecord-like objects.
        Structure: /facts/<issue_id>/<fact_type>/<key>"""
        self._facts.children.clear()
        for rec in records:
            issue_id = str(getattr(rec, "issue_id", "default"))
            fact_type = str(getattr(rec, "fact_type", "architecture"))
            key = str(getattr(rec, "key", "unknown"))
            value = str(getattr(rec, "value", ""))
            step = getattr(rec, "updated_step", 0)
            run_id = getattr(rec, "updated_run_id", 0)

            issue_dir = self._facts.add_dir(issue_id)
            type_dir = issue_dir.add_dir(fact_type)
            type_dir.add_file(key, content=value, updated_step=step, updated_run_id=run_id)

    def set_fact(self, issue_id: str, fact_type: str, key: str, value: str, **meta: Any) -> None:
        """Write a single fact into the tree."""
        issue_dir = self._facts.add_dir(issue_id)
        type_dir = issue_dir.add_dir(fact_type)
        existing = type_dir.get(key)
        if isinstance(existing, FileNode):
            existing.content = value
            existing.metadata.update(meta)
        else:
            type_dir.add_file(key, content=value, **meta)

    def get_fact(self, issue_id: str, fact_type: str, key: str) -> Optional[str]:
        node = _resolve(self._facts, f"{issue_id}/{fact_type}/{key}")
        if isinstance(node, FileNode):
            return node.content
        return None

    def clear_fact_scope(self, issue_id: str) -> None:
        """Remove all facts for a top-level issue scope."""
        self._facts.children.pop(issue_id, None)

    # ------------------------------------------------------------------
    # /facts/log-issues — parsed diagnostics from log files
    # ------------------------------------------------------------------

    def ingest_log_issues(self, path: str) -> List[Dict[str, Any]]:
        """Parse a log file and materialize distinct issues under /facts."""
        node = _resolve(self._root, path)
        if not isinstance(node, FileNode):
            return []
        content = node.content

        issues = self.ingest_diagnostic_content(content, source_path=path)
        return issues

    def ingest_diagnostic_content(self, content: str, *, source_path: str) -> List[Dict[str, Any]]:
        """Parse diagnostic text/json and materialize issues under /facts."""
        issues = self._parse_log_issues(content, source_path=source_path)
        self.clear_fact_scope(self.LOG_ISSUES_ROOT)
        for issue in issues:
            issue_id = str(issue["id"])
            for key, value in issue.items():
                if key == "id":
                    continue
                self.set_fact(self.LOG_ISSUES_ROOT, issue_id, key, str(value))
        return issues

    def ingest_browser_diagnostics(self, payload: Dict[str, Any], *, source_path: str) -> List[Dict[str, Any]]:
        """Materialize browser/runtime diagnostics under /facts/log-issues."""
        issues: List[Dict[str, Any]] = []
        route = str(payload.get("route", "") or "")
        final_url = str(payload.get("finalUrl", payload.get("url", "")) or "")

        def _append(issue: Dict[str, Any]) -> None:
            issue["id"] = f"issue-{len(issues) + 1:03d}"
            issues.append(issue)

        for entry in payload.get("consoleMessages", []) or []:
            if not isinstance(entry, dict):
                continue
            level = str(entry.get("type", "console") or "console")
            text = str(entry.get("text", "") or "").strip()
            if not text:
                continue
            location = entry.get("location") if isinstance(entry.get("location"), dict) else {}
            file_path = str(location.get("url", "") or "")
            line_no = str(location.get("lineNumber", "") or "")
            column_no = str(location.get("columnNumber", "") or "")
            _append({
                "kind": "Browser console message",
                "classification": f"browser_console_{level}",
                "message": text,
                "summary": f"{level}: {text}" + (f" ({route})" if route else ""),
                "status": "open",
                "tool": "playwright",
                "route": route,
                "url": final_url,
                "file": file_path,
                "line": line_no,
                "column": column_no,
                "severity": "8" if level in {"error", "warning"} else "4",
                "count": 1,
                "source": source_path,
                "example": json.dumps(entry, indent=2),
            })

        for entry in payload.get("pageErrors", []) or []:
            if isinstance(entry, dict):
                text = str(entry.get("message", "") or "").strip()
                stack = str(entry.get("stack", "") or "")
            else:
                text = str(entry).strip()
                stack = ""
            if not text:
                continue
            _append({
                "kind": "Browser page error",
                "classification": "browser_page_error",
                "message": text,
                "summary": f"pageerror: {text}" + (f" ({route})" if route else ""),
                "status": "open",
                "tool": "playwright",
                "route": route,
                "url": final_url,
                "file": "",
                "line": "",
                "column": "",
                "severity": "8",
                "count": 1,
                "source": source_path,
                "example": stack or text,
            })

        for entry in payload.get("requestFailures", []) or []:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url", "") or "")
            error_text = str(entry.get("errorText", "") or "request failed")
            method = str(entry.get("method", "") or "")
            _append({
                "kind": "Browser request failure",
                "classification": "browser_request_failure",
                "message": error_text,
                "summary": f"{method} {url} failed" if method or url else error_text,
                "status": "open",
                "tool": "playwright",
                "route": route,
                "url": final_url,
                "file": url,
                "line": "",
                "column": "",
                "severity": "8",
                "count": 1,
                "source": source_path,
                "example": json.dumps(entry, indent=2),
            })

        for entry in payload.get("responseErrors", []) or []:
            if not isinstance(entry, dict):
                continue
            url = str(entry.get("url", "") or "")
            status = str(entry.get("status", "") or "")
            status_text = str(entry.get("statusText", "") or "")
            _append({
                "kind": "Browser HTTP error",
                "classification": "browser_http_error",
                "message": f"HTTP {status} {status_text}".strip(),
                "summary": f"HTTP {status} {url}".strip(),
                "status": "open",
                "tool": "playwright",
                "route": route,
                "url": final_url,
                "file": url,
                "line": "",
                "column": "",
                "severity": "8",
                "count": 1,
                "source": source_path,
                "example": json.dumps(entry, indent=2),
            })

        self.clear_fact_scope(self.LOG_ISSUES_ROOT)
        for issue in issues:
            issue_id = str(issue["id"])
            for key, value in issue.items():
                if key == "id":
                    continue
                self.set_fact(self.LOG_ISSUES_ROOT, issue_id, key, str(value))
        return issues

    def list_log_issues(self) -> List[Dict[str, Any]]:
        root = _resolve(self._facts, self.LOG_ISSUES_ROOT)
        if not isinstance(root, DirNode):
            return []
        issues: List[Dict[str, Any]] = []
        for child in sorted(root.children.values(), key=lambda n: n.name):
            if not isinstance(child, DirNode):
                continue
            issue: Dict[str, Any] = {"id": child.name}
            for entry in child.children.values():
                if isinstance(entry, FileNode):
                    issue[entry.name] = entry.content
            issues.append(issue)
        return issues

    def show_log_issue(self, issue_id: str) -> Optional[Dict[str, Any]]:
        for issue in self.list_log_issues():
            if issue.get("id") == issue_id:
                return issue
        return None

    def log_issue_read_commands(self, issue: Dict[str, Any], *, radius: int = 20) -> List[str]:
        """Return focused read commands that help the model inspect an issue."""
        file_path = str(issue.get("file", "") or "").strip()
        if not file_path:
            return []
        repo_path = file_path if file_path.startswith("/repo/") else f"/repo/{file_path.removeprefix('repo/')}"
        line_text = str(issue.get("line", "") or "").strip()
        try:
            line_no = int(line_text)
        except Exception:
            line_no = 0
        if line_no > 0:
            start = max(1, line_no - radius)
            end = line_no + radius
            return [
                f"read-line-range {repo_path} {start}-{end}",
                f"cat {repo_path}:{start}-{end}",
            ]
        return [f"cat {repo_path}"]

    def format_log_issue_list(self, issues: Optional[List[Dict[str, Any]]] = None) -> str:
        """Render current-run diagnostic issues as a model-actionable checklist."""
        issue_items = self.list_log_issues() if issues is None else issues
        if not issue_items:
            return "(no run issues)"

        lines = [f"Run issues: {len(issue_items)}"]
        for issue in issue_items:
            issue_id = str(issue.get("id", "") or "")
            status = str(issue.get("status", "open") or "open")
            count = str(issue.get("count", "1") or "1")
            severity = str(issue.get("severity", "") or "").strip()
            kind = str(issue.get("kind", "") or "").strip()
            code = str(issue.get("code", "") or "").strip()
            file_path = str(issue.get("file", "") or "").strip()
            line_no = str(issue.get("line", "") or "").strip()
            column = str(issue.get("column", "") or "").strip()
            location = file_path
            if line_no:
                location += f":{line_no}"
                if column:
                    location += f":{column}"
            summary = str(issue.get("summary", issue.get("message", issue_id)) or "").strip()
            metadata = [part for part in [f"severity={severity}" if severity else "", kind, code] if part]
            header_bits = [f"- {issue_id} [{status}] x{count}"]
            if metadata:
                header_bits.append(" ".join(metadata))
            if location:
                header_bits.append(f"at {location}")
            lines.append(" ".join(header_bits))
            if summary:
                lines.append(f"  summary: {summary}")
            next_reads = self.log_issue_read_commands(issue)
            next_steps = [f"show-run-issue {issue_id}", *next_reads]
            lines.append(f"  next: {'; '.join(next_steps)}")
        return "\n".join(lines)

    def format_log_issue_detail(self, issue: Dict[str, Any]) -> str:
        """Render one issue with the high-value fields before verbose evidence."""
        issue_id = str(issue.get("id", "") or "")
        status = str(issue.get("status", "open") or "open")
        fields = [
            ("summary", issue.get("summary") or issue.get("message") or ""),
            ("message", issue.get("message") or ""),
            ("kind", issue.get("kind") or ""),
            ("classification", issue.get("classification") or ""),
            ("tool", issue.get("tool") or ""),
            ("code", issue.get("code") or ""),
            ("severity", issue.get("severity") or ""),
            ("file", issue.get("file") or ""),
            ("line", issue.get("line") or ""),
            ("column", issue.get("column") or ""),
            ("count", issue.get("count") or ""),
            ("source", issue.get("source") or ""),
        ]
        lines = [f"Run Issue {issue_id} [{status}]"]
        for key, value in fields:
            text = str(value or "").strip()
            if text:
                lines.append(f"{key}: {text}")

        next_reads = self.log_issue_read_commands(issue)
        if next_reads:
            lines.append("next_reads:")
            lines.extend(f"- {command}" for command in next_reads)

        example = str(issue.get("example", "") or "").strip()
        if example:
            lines.append("evidence:")
            lines.append(example)
        return "\n".join(lines)

    def resolve_log_issue(self, issue_id: str) -> bool:
        issue = self.show_log_issue(issue_id)
        if issue is None:
            return False
        self.set_fact(self.LOG_ISSUES_ROOT, issue_id, "status", "resolved")
        return True

    def reopen_log_issue(self, issue_id: str) -> bool:
        issue = self.show_log_issue(issue_id)
        if issue is None:
            return False
        self.set_fact(self.LOG_ISSUES_ROOT, issue_id, "status", "open")
        return True

    def _parse_log_issues(self, content: str, *, source_path: str) -> List[Dict[str, Any]]:
        lines = content.splitlines()
        issues_by_fingerprint: Dict[Tuple[str, str], Dict[str, Any]] = {}
        tsc_re = re.compile(
            r"^(?P<path>.+?)\((?P<line>\d+),(?P<column>\d+)\):\s*error\s+TS(?P<code>\d+):\s*(?P<message>.+)$",
            re.IGNORECASE,
        )
        runtime_exception_re = re.compile(
            r"^(?P<error_type>ReferenceError|TypeError|SyntaxError|RangeError|EvalError|URIError|Error):\s+(?P<message>.+)$"
        )
        stack_frame_re = re.compile(
            r"^\s*at\s+(?P<symbol>.*?)\s*\((?P<path>[^()\s]+?\.(?:tsx?|jsx?))(?:\?[^:)]*)?:(?P<line>\d+):(?P<column>\d+)\)\s*$"
        )
        stack_frame_bare_re = re.compile(
            r"^\s*at\s+(?P<path>[^()\s]+?\.(?:tsx?|jsx?))(?:\?[^:)]*)?:(?P<line>\d+):(?P<column>\d+)\s*$"
        )
        for diagnostic in self._extract_embedded_diagnostics(content):
            file_path = str(diagnostic.get("resource", "") or "")
            message = str(diagnostic.get("message", "") or "").strip()
            if not message:
                continue
            code = str(diagnostic.get("code", "") or "").strip()
            owner = str(diagnostic.get("owner", "") or diagnostic.get("source", "") or "")
            line_no = str(diagnostic.get("startLineNumber", "") or "")
            column_no = str(diagnostic.get("startColumn", "") or "")
            fingerprint = (message.lower(), file_path.lower())
            classification = self._classify_log_issue(
                message=message,
                file_path=file_path,
                plugin=owner,
                kind="Editor diagnostic",
            )
            issues_by_fingerprint[fingerprint] = {
                "kind": "Editor diagnostic",
                "classification": classification,
                "message": message,
                "summary": f"{message} ({file_path}:{line_no})" if file_path and line_no else message,
                "status": "open",
                "tool": owner or "editor",
                "file": file_path,
                "line": line_no,
                "column": column_no,
                "code": code,
                "severity": str(diagnostic.get("severity", "") or ""),
                "count": 1,
                "source": source_path,
                "example": json.dumps(diagnostic, indent=2),
            }
        error_re = re.compile(
            r"^(?:\d{1,2}:\d{2}:\d{2} [AP]M )?\[[^\]]+\](?: \([^)]+\))?\s+"
            r"(?P<kind>Internal server error|Pre-transform error|error):\s+(?P<message>.+)$"
        )
        runtime_re = re.compile(r"^(?:\s*⟶\s*)?unhandled action type:\s*(?P<action>\S+)\s*$", re.IGNORECASE)
        plugin_re = re.compile(r"^\s*Plugin:\s*(?P<plugin>.+)$")
        file_re = re.compile(r"^\s*File:\s*(?P<path>.+?):(?P<line>\d+):(?P<column>\d+)$")

        i = 0
        while i < len(lines):
            line = lines[i]
            match = error_re.match(line.strip())
            runtime_match = runtime_re.match(line.strip())
            if match is None and runtime_match is None:
                tsc_match = tsc_re.match(line.strip())
                if tsc_match is not None:
                    file_path = tsc_match.group("path").strip()
                    message = tsc_match.group("message").strip()
                    code = f"TS{tsc_match.group('code').strip()}"
                    line_no = tsc_match.group("line").strip()
                    column_no = tsc_match.group("column").strip()
                    fingerprint = (message.lower(), file_path.lower())
                    classification = self._classify_log_issue(
                        message=message,
                        file_path=file_path,
                        plugin="typescript",
                        kind="Compiler diagnostic",
                    )
                    issues_by_fingerprint[fingerprint] = {
                        "kind": "Compiler diagnostic",
                        "classification": classification,
                        "message": message,
                        "summary": f"{message} ({file_path}:{line_no})",
                        "status": "open",
                        "tool": "typescript",
                        "file": file_path,
                        "line": line_no,
                        "column": column_no,
                        "code": code,
                        "severity": "8",
                        "count": 1,
                        "source": source_path,
                        "example": line.strip(),
                    }
                    i += 1
                    continue
                runtime_exception_match = runtime_exception_re.match(line.strip())
                if runtime_exception_match is not None:
                    error_type = runtime_exception_match.group("error_type").strip()
                    message = f"{error_type}: {runtime_exception_match.group('message').strip()}"
                    kind = "Runtime exception"
                    file_path = ""
                    line_no = ""
                    column_no = ""
                    example_lines = [line.strip()]

                    j = i + 1
                    while j < len(lines):
                        current = lines[j]
                        stripped = current.strip()
                        if not stripped:
                            if len(example_lines) < 6:
                                example_lines.append(stripped)
                            j += 1
                            continue
                        if error_re.match(stripped) or tsc_re.match(stripped) or runtime_exception_re.match(stripped):
                            break
                        file_match = stack_frame_re.match(current) or stack_frame_bare_re.match(current)
                        if file_match is not None and not file_path:
                            file_path = file_match.group("path").strip()
                            line_no = file_match.group("line")
                            column_no = file_match.group("column")
                        if len(example_lines) < 6:
                            example_lines.append(stripped)
                        if stripped.startswith("The above error occurred in"):
                            j += 1
                            break
                        j += 1

                    fingerprint = (message.lower(), file_path.lower())
                    classification = self._classify_log_issue(
                        message=message,
                        file_path=file_path,
                        plugin="runtime",
                        kind=kind,
                    )
                    issues_by_fingerprint[fingerprint] = {
                        "kind": kind,
                        "classification": classification,
                        "message": message,
                        "summary": f"{message} ({file_path}:{line_no})" if file_path and line_no else message,
                        "status": "open",
                        "tool": "runtime",
                        "file": file_path,
                        "line": line_no,
                        "column": column_no,
                        "count": 1,
                        "source": source_path,
                        "example": "\n".join(example_lines),
                    }
                    i = j
                    continue
                i += 1
                continue

            if runtime_match is not None:
                action = runtime_match.group("action").strip()
                message = f"Unhandled action type: {action}"
                kind = "Runtime action error"
            else:
                message = match.group("message").strip()
                kind = match.group("kind").strip()
            plugin = ""
            file_path = ""
            line_no = ""
            column_no = ""
            example_lines = [line.strip()]

            j = i + 1
            while j < len(lines):
                current = lines[j]
                stripped = current.strip()
                if error_re.match(stripped):
                    break
                if stripped.startswith("[") or re.match(r"^\d{1,2}:\d{2}:\d{2} [AP]M ", stripped):
                    break
                plugin_match = plugin_re.match(current)
                if plugin_match is not None and not plugin:
                    plugin = plugin_match.group("plugin").strip()
                file_match = file_re.match(current)
                if file_match is not None and not file_path:
                    file_path = file_match.group("path").strip()
                    line_no = file_match.group("line")
                    column_no = file_match.group("column")
                if stripped and len(example_lines) < 6:
                    example_lines.append(stripped)
                j += 1

            fingerprint = (message.lower(), file_path.lower())
            existing = issues_by_fingerprint.get(fingerprint)
            if existing is None:
                classification = self._classify_log_issue(message=message, file_path=file_path, plugin=plugin, kind=kind)
                issues_by_fingerprint[fingerprint] = {
                    "kind": kind,
                    "classification": classification,
                    "message": message,
                    "summary": f"{message} ({file_path}:{line_no})" if file_path and line_no else message,
                    "status": "open",
                    "tool": plugin or "unknown",
                    "file": file_path,
                    "line": line_no,
                    "column": column_no,
                    "count": 1,
                    "source": source_path,
                    "example": "\n".join(example_lines),
                }
            else:
                existing["count"] = int(existing.get("count", 1)) + 1

            i = j

        issues = sorted(
            issues_by_fingerprint.values(),
            key=lambda issue: (str(issue.get("file", "")), str(issue.get("message", ""))),
        )
        for index, issue in enumerate(issues, start=1):
            issue["id"] = f"issue-{index:03d}"
        return issues

    def _extract_embedded_diagnostics(self, content: str) -> List[Dict[str, Any]]:
        decoder = JSONDecoder()
        diagnostics: List[Dict[str, Any]] = []
        seen: set[Tuple[str, str, str]] = set()
        length = len(content)
        index = 0
        while index < length:
            char = content[index]
            if char not in "[{":
                index += 1
                continue
            try:
                parsed, end = decoder.raw_decode(content[index:])
            except Exception:
                index += 1
                continue

            candidates: List[Any]
            if isinstance(parsed, list):
                candidates = parsed
            else:
                candidates = [parsed]

            found = False
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                if "resource" not in candidate or "message" not in candidate:
                    continue
                key = (
                    str(candidate.get("resource", "") or ""),
                    str(candidate.get("message", "") or ""),
                    str(candidate.get("code", "") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                diagnostics.append(candidate)
                found = True

            index += max(end, 1) if found else 1
        return diagnostics

    def _classify_log_issue(self, *, message: str, file_path: str, plugin: str, kind: str) -> str:
        lower_message = message.lower()
        lower_file = file_path.lower()
        lower_plugin = plugin.lower()
        lower_kind = kind.lower()

        if lower_kind == "editor diagnostic" or lower_plugin == "typescript":
            if "has no exported member" in lower_message:
                return "typescript_import_export_error"
            return "editor_diagnostic"
        if lower_plugin == "runtime" or lower_kind == "runtime exception":
            if lower_message.startswith("referenceerror:"):
                return "javascript_runtime_reference_error"
            if lower_message.startswith("typeerror:"):
                return "javascript_runtime_type_error"
            return "javascript_runtime_error"
        if "unhandled action type" in lower_message or "run_shell" in lower_message:
            return "runtime_command_failure"
        if "failed to resolve import" in lower_message:
            if lower_message.startswith('failed to resolve import "./') or lower_message.startswith("failed to resolve import '../"):
                return "local_import_missing"
            if "/src/" in lower_file or lower_file.endswith(".tsx") or lower_file.endswith(".ts"):
                imported = re.search(r'failed to resolve import "([^"]+)"', lower_message)
                if imported is not None and imported.group(1).startswith("."):
                    return "local_import_missing"
            return "dependency_resolution_error"
        if "optimized dependencies" in lower_message or "vite:import-analysis" in lower_plugin:
            return "dependency_resolution_error"
        if "internal server error" in lower_kind:
            return "build_runtime_error"
        return "uncategorized_error"

    # ------------------------------------------------------------------
    # /memory — Memory items
    # ------------------------------------------------------------------

    def sync_memory(self, items: Sequence[Any]) -> None:
        """Rebuild /memory from MemoryItem-like objects."""
        self._memory.children.clear()
        recent = self._memory.add_dir("recent")
        for item in items:
            item_id = str(getattr(item, "id", "unknown"))
            summary = str(getattr(item, "summary", ""))
            kind = str(getattr(item, "kind", "step"))
            meta = {}
            if hasattr(item, "metadata") and isinstance(item.metadata, dict):
                meta = {
                    "tool": item.metadata.get("tool"),
                    "paths": item.metadata.get("paths", [])[:6],
                    "tags": item.metadata.get("tags", [])[:8],
                    "importance": item.metadata.get("importance"),
                    "produced_by_step": item.metadata.get("produced_by_step"),
                }
            recent.add_file(
                item_id,
                content=summary,
                kind=kind,
                **meta,
            )

    # ------------------------------------------------------------------
    # /status — Agent state
    # ------------------------------------------------------------------

    def sync_status(self, state: Dict[str, Any]) -> None:
        """Write agent status flags as files under /status."""
        self._status.children.clear()
        for key, value in state.items():
            self._status.add_file(key, content=json.dumps(value, default=str))

    # ------------------------------------------------------------------
    # /skills — Skill registry
    # ------------------------------------------------------------------

    def register_skill(
        self,
        name: str,
        description: str,
        *,
        args_schema: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        category: str = "general",
        priority: int = 0,
        modes: Optional[List[str]] = None,
        cache: Optional[str] = None,
        handler: Optional[Callable[..., str]] = None,
    ) -> None:
        """Register a skill the agent can invoke."""
        skill = SkillDefinition(
            name=name,
            description=description,
            args_schema=args_schema or {},
            tags=[str(item).strip() for item in (tags or []) if str(item).strip()],
            category=str(category or "general").strip() or "general",
            priority=int(priority or 0),
            modes=[str(item).strip() for item in (modes or []) if str(item).strip()],
            cache=cache,
            handler=handler,
        )
        self._skills[name] = skill

        # Also add to /skills dir for ls/cat
        manifest = json.dumps({
            "name": name,
            "description": description,
            "args": args_schema or {},
            "tags": skill.tags,
            "category": skill.category,
            "priority": skill.priority,
            "modes": skill.modes,
            "has_cache": cache is not None,
            "has_handler": handler is not None,
        }, indent=2)
        self._skills_dir.add_file(name, content=manifest)

    def invoke_skill(self, name: str, **kwargs: Any) -> str:
        skill = self._skills.get(name)
        if skill is None:
            return f"Error: unknown skill '{name}'"
        return skill.execute(**kwargs)

    def list_skills(self) -> List[Dict[str, str]]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "category": s.category,
                "priority": str(s.priority),
                "tags": ", ".join(s.tags),
                "modes": ", ".join(s.modes),
            }
            for s in self._skills.values()
        ]

    def list_skills_payload(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": s.name,
                "description": s.description,
                "args_schema": dict(s.args_schema),
                "tags": list(s.tags),
                "category": s.category,
                "priority": s.priority,
                "modes": list(s.modes),
            }
            for s in self._skills.values()
        ]

    # ------------------------------------------------------------------
    # Command interface — zero-cost reads
    # ------------------------------------------------------------------

    def resolve(self, path: str) -> Optional[TreeNode]:
        return _resolve(self._root, path)

    def ls(self, path: str = "/", depth: int = 1) -> List[Dict[str, Any]]:
        node = _resolve(self._root, path)
        if isinstance(node, DirNode):
            return node.ls(depth=depth)
        if isinstance(node, FileNode):
            return [node.stat()]
        return [{"error": f"not found: {path}"}]

    def read_line_range(
        self,
        path: str,
        start_line: int,
        end_line: int,
        *,
        include_line_numbers: bool = True,
    ) -> str:
        node = _resolve(self._root, path)
        if not isinstance(node, FileNode):
            if isinstance(node, DirNode):
                return f"[directory: {path} — {len(node.children)} entries]"
            return f"[not found: {path}]"

        content = node.content
        lines = content.splitlines()
        if not lines:
            return "[empty file]"

        start = max(1, start_line)
        end = max(start, end_line if end_line > 0 else len(lines))
        if start > len(lines):
            return f"[line range out of bounds: {path}:{start}-{end} ({len(lines)} total lines)]"

        end = min(end, len(lines))
        selected = lines[start - 1:end]
        if not include_line_numbers:
            return "\n".join(selected)

        width = max(len(str(end)), 2)
        return "\n".join(f"{line_no:>{width}} | {text}" for line_no, text in zip(range(start, end + 1), selected))

    def cat(self, path: str, start_line: int = 0, end_line: int = 0) -> str:
        node = _resolve(self._root, path)
        if isinstance(node, FileNode):
            if start_line > 0 or end_line > 0:
                return self.read_line_range(path, start_line, end_line, include_line_numbers=True)
            content = node.content
            lines = content.splitlines()
            if len(lines) > _CAT_MAX_FULL_LINES or len(content) > _CAT_MAX_FULL_CHARS:
                preview_end = min(len(lines), _CAT_PREVIEW_LINES)
                preview = self.read_line_range(path, 1, preview_end, include_line_numbers=True)
                hint_end = min(len(lines), max(preview_end + 80, 160))
                return (
                    f"[file too large to dump fully: {path} "
                    f"({len(lines)} lines, {len(content.encode('utf-8', errors='replace'))} bytes)]\n"
                    f"Previewing lines 1-{preview_end}.\n"
                    f"Use `read-line-range {path} <start>-<end>` for focused reads, "
                    f"for example `read-line-range {path} {preview_end + 1}-{hint_end}`.\n"
                    f"{preview}"
                )
            return self.read_line_range(path, 1, 0, include_line_numbers=True)
        if isinstance(node, DirNode):
            return f"[directory: {path} — {len(node.children)} entries]"
        return f"[not found: {path}]"

    def stat(self, path: str) -> Dict[str, Any]:
        node = _resolve(self._root, path)
        if node is None:
            return {"error": f"not found: {path}"}
        if isinstance(node, DirNode):
            return node.stat()
        if isinstance(node, FileNode):
            return node.stat()
        return {"error": "unknown node type"}

    def find(self, path: str = "/", glob_pattern: str = "*", limit: int = 100) -> List[str]:
        node = _resolve(self._root, path)
        if not isinstance(node, DirNode):
            return []
        results: List[str] = []
        for child in node.walk():
            if fnmatch.fnmatch(child.name, glob_pattern):
                results.append(child.path())
                if len(results) >= limit:
                    break
        return results

    def grep(self, path: str, pattern: str, limit: int = 50) -> List[Dict[str, Any]]:
        """Search file contents within the tree. Only searches already-loaded files
        unless the path is under /facts, /memory, or /status (always loaded)."""
        node = _resolve(self._root, path)
        if node is None:
            return []

        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error:
            regex = re.compile(re.escape(pattern), re.IGNORECASE)

        results: List[Dict[str, Any]] = []
        nodes = [node] if isinstance(node, FileNode) else list(node.walk()) if isinstance(node, DirNode) else []

        for n in nodes:
            if not isinstance(n, FileNode):
                continue
            # For /repo, only search loaded content (don't trigger lazy IO)
            if n.path().startswith("repo/") and not n.content_loaded():
                continue
            content = n._content or ""
            for i, line in enumerate(content.splitlines(), 1):
                if regex.search(line):
                    results.append({"path": n.path(), "line": i, "text": line.strip()[:200]})
                    if len(results) >= limit:
                        return results
        return results

    # ------------------------------------------------------------------
    # Serialization — render the tree for prompt injection
    # ------------------------------------------------------------------

    def render_prompt_block(
        self,
        *,
        repo_depth: int = 2,
        include_facts: bool = True,
        include_memory: bool = True,
        include_status: bool = True,
        include_skills: bool = True,
        max_chars: int = 30000,
    ) -> str:
        """Render the full tree as a compact prompt block."""
        sections: List[str] = []

        # /repo
        repo_listing = self._repo.ls(depth=repo_depth)
        repo_lines = [f"  {e['name']}" + (f"  ({e.get('lines', '?')} lines)" if e.get("type") == "file" else f"  ({e.get('children', '?')} items)")
                       for e in repo_listing[:200]]
        sections.append("WORKSPACE TREE (/repo):\n" + "\n".join(repo_lines))

        # /facts
        if include_facts:
            fact_lines: List[str] = []
            for child in self._facts.walk():
                if isinstance(child, FileNode):
                    fact_lines.append(f"  /{child.path()} = {child.content[:200]}")
            if fact_lines:
                sections.append("FACTS (/facts):\n" + "\n".join(fact_lines))
            else:
                sections.append("FACTS (/facts): (empty)")

        # /memory
        if include_memory:
            mem_lines: List[str] = []
            for child in self._memory.walk():
                if isinstance(child, FileNode):
                    kind = child.metadata.get("kind", "")
                    paths = child.metadata.get("paths", [])
                    path_str = ", ".join(paths[:3]) if paths else ""
                    summary = child.content[:150]
                    mem_lines.append(f"  [{child.name}] {kind}: {summary}" + (f" ({path_str})" if path_str else ""))
            if mem_lines:
                sections.append(f"MEMORY (/memory): {len(mem_lines)} items\n" + "\n".join(mem_lines[:20]))
            else:
                sections.append("MEMORY (/memory): (empty)")

        # /status
        if include_status:
            status_lines: List[str] = []
            for child in sorted(self._status.children.values(), key=lambda c: c.name):
                if isinstance(child, FileNode):
                    val = child.content
                    status_lines.append(f"  {child.name}: {val}")
            if status_lines:
                sections.append("STATUS (/status):\n" + "\n".join(status_lines))

        # /skills
        if include_skills and self._skills:
            skill_lines = [f"  {s.name} — {s.description}" for s in self._skills.values()]
            sections.append("SKILLS (/skills):\n" + "\n".join(skill_lines))

        rendered = "\n\n".join(sections)
        if len(rendered) > max_chars:
            rendered = rendered[:max_chars] + "\n...[tree truncated]..."
        return rendered

    def render_hot_files_block(self, rel_paths: Sequence[str], max_chars: int = 50000) -> str:
        """Render content of preloaded 'hot' files for the prompt."""
        blocks: List[str] = []
        total = 0
        for rp in rel_paths:
            node = _resolve(self._repo, rp)
            if isinstance(node, FileNode) and node.content_loaded():
                header = f"--- /repo/{rp} ({node.lines} lines) ---"
                content = node.content
                if total + len(content) > max_chars:
                    remaining = max_chars - total
                    content = content[:remaining] + "\n...[truncated]..."
                blocks.append(header + "\n" + content)
                total += len(content)
                if total >= max_chars:
                    break
        return "\n\n".join(blocks) if blocks else "(no hot files loaded)"
