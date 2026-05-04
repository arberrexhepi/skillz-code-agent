from __future__ import annotations

import json
import math
import re
import time
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\-./:]{1,63}")
PATH_RE = re.compile(r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+|[A-Za-z0-9_.-]+\.(?:py|js|ts|json|md|toml|yaml|yml|sh|txt)")


def tokenize(text: str) -> List[str]:
    return [m.group(0).lower() for m in TOKEN_RE.finditer(text)]


def shorten(text: str, n: int) -> str:
    text = text or ""
    return text if len(text) <= n else text[:n] + "\n...[truncated]..."


def uniq(seq: Iterable[str]) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in seq:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


@dataclass
class MemoryItem:
    id: str
    kind: str
    summary: str
    content: Dict[str, Any]
    metadata: Dict[str, Any]
    created_at: float = field(default_factory=time.time)

    def view_small(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "summary": self.summary,
            "metadata": {
                "tool": self.metadata.get("tool"),
                "paths": self.metadata.get("paths", [])[:6],
                "tags": self.metadata.get("tags", [])[:8],
                "entities": self.metadata.get("entities", [])[:8],
                "task_types": self.metadata.get("task_types", [])[:6],
                "error_type": self.metadata.get("error_type"),
                "branch": self.metadata.get("branch"),
                "produced_by_step": self.metadata.get("produced_by_step"),
                "importance": self.metadata.get("importance"),
            },
        }

    def is_expired(self, current_step: int) -> bool:
        ttl_steps = self.metadata.get("ttl_steps")
        produced_by_step = self.metadata.get("produced_by_step")
        if ttl_steps is None or produced_by_step is None:
            return False
        try:
            return int(current_step) - int(produced_by_step) > int(ttl_steps)
        except Exception:
            return False


@dataclass
class MemoryQuery:
    task: str
    action_type: Optional[str] = None
    paths: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    error_type: Optional[str] = None
    limit: int = 8
    current_branch: Optional[str] = None
    current_step: int = 0


class MemoryManager:
    """
    Tool-agnostic memory manager for coding agents.

    Core idea:
    - ingest every tool result as a normalized memory item
    - index via metadata
    - retrieve compact, relevant views for the next model step
    """

    def __init__(self, root: Path, capacity: int = 5000) -> None:
        self.root = root.resolve()
        self.capacity = capacity
        self.items: List[MemoryItem] = []
        self.path_index: Dict[str, List[str]] = {}
        self.tool_index: Dict[str, List[str]] = {}
        self.tag_index: Dict[str, List[str]] = {}
        self.entity_index: Dict[str, List[str]] = {}

    # -----------------------------
    # Public API
    # -----------------------------

    def ingest_step(
        self,
        *,
        task: str,
        thought: str,
        action: Dict[str, Any],
        result_ok: bool,
        result_payload: Dict[str, Any],
        step: int,
        branch: Optional[str] = None,
    ) -> MemoryItem:
        item = self._build_memory_item(
            task=task,
            thought=thought,
            action=action,
            result_ok=result_ok,
            result_payload=result_payload,
            step=step,
            branch=branch,
        )
        self._store(item)
        return item

    def lookup(self, query: MemoryQuery) -> Dict[str, Any]:
        candidates = self._candidate_items(query)
        ranked = self._rank_candidates(candidates, query)

        top = []
        for score, item in ranked[: query.limit]:
            view = item.view_small()
            view["score"] = round(score, 4)
            top.append(view)

        return {
            "query": {
                "task": query.task,
                "action_type": query.action_type,
                "paths": query.paths,
                "tags": query.tags,
                "entities": query.entities,
                "error_type": query.error_type,
                "current_branch": query.current_branch,
                "current_step": query.current_step,
            },
            "summary": {
                "items_total": len(self.items),
                "candidates_considered": len(candidates),
                "returned": len(top),
            },
            "relevant_memories": top,
        }

    def stats(self) -> Dict[str, Any]:
        return {
            "root": str(self.root),
            "items_total": len(self.items),
            "indexed_paths": len(self.path_index),
            "indexed_tools": len(self.tool_index),
            "indexed_tags": len(self.tag_index),
            "indexed_entities": len(self.entity_index),
        }

    def get_item(self, memory_id: str) -> Optional[Dict[str, Any]]:
        for item in self.items:
            if item.id == memory_id:
                return {
                    "id": item.id,
                    "kind": item.kind,
                    "summary": item.summary,
                    "content": item.content,
                    "metadata": item.metadata,
                    "created_at": item.created_at,
                }
        return None

    def get_items(self, memory_ids: List[str]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for memory_id in memory_ids:
            item = self.get_item(memory_id)
            if item is not None:
                out.append(item)
        return out

    # -----------------------------
    # Ingestion
    # -----------------------------

    def _build_memory_item(
        self,
        *,
        task: str,
        thought: str,
        action: Dict[str, Any],
        result_ok: bool,
        result_payload: Dict[str, Any],
        step: int,
        branch: Optional[str],
    ) -> MemoryItem:
        tool = str(action.get("type", "unknown"))
        kind = self._infer_kind(tool, result_payload)
        paths = self._extract_paths(action, result_payload)
        entities = self._extract_entities(task, thought, action, result_payload)
        tags = self._infer_tags(tool, result_payload, result_ok)
        task_types = self._infer_task_types(task, tool)
        error_type = self._infer_error_type(result_payload)
        importance = self._estimate_importance(tool, result_ok, result_payload)
        ttl_steps = self._estimate_ttl(tool, result_ok)

        summary = self._summarize_item(
            tool=tool,
            kind=kind,
            result_ok=result_ok,
            result_payload=result_payload,
            paths=paths,
            error_type=error_type,
        )

        content = self._compact_content(tool, result_payload)

        metadata = {
            "tool": tool,
            "paths": paths,
            "entities": entities,
            "tags": tags,
            "task_types": task_types,
            "error_type": error_type,
            "branch": branch,
            "produced_by_step": step,
            "importance": importance,
            "ttl_steps": ttl_steps,
            "result_ok": result_ok,
        }

        return MemoryItem(
            id=f"mem_{uuid.uuid4().hex[:12]}",
            kind=kind,
            summary=summary,
            content=content,
            metadata=metadata,
        )

    def _store(self, item: MemoryItem) -> None:
        self.items.append(item)
        if len(self.items) > self.capacity:
            removed = self.items.pop(0)
            self._deindex(removed)

        self._index(item)

    def _index(self, item: MemoryItem) -> None:
        for path in item.metadata.get("paths", []):
            self.path_index.setdefault(path, []).append(item.id)
        tool = item.metadata.get("tool")
        if tool:
            self.tool_index.setdefault(tool, []).append(item.id)
        for tag in item.metadata.get("tags", []):
            self.tag_index.setdefault(tag, []).append(item.id)
        for entity in item.metadata.get("entities", []):
            self.entity_index.setdefault(entity, []).append(item.id)

    def _deindex(self, item: MemoryItem) -> None:
        def _remove(index: Dict[str, List[str]], key: str, item_id: str) -> None:
            values = index.get(key)
            if not values:
                return
            index[key] = [x for x in values if x != item_id]
            if not index[key]:
                del index[key]

        for path in item.metadata.get("paths", []):
            _remove(self.path_index, path, item.id)
        tool = item.metadata.get("tool")
        if tool:
            _remove(self.tool_index, tool, item.id)
        for tag in item.metadata.get("tags", []):
            _remove(self.tag_index, tag, item.id)
        for entity in item.metadata.get("entities", []):
            _remove(self.entity_index, entity, item.id)

    # -----------------------------
    # Retrieval
    # -----------------------------

    def _candidate_items(self, query: MemoryQuery) -> List[MemoryItem]:
        direct_ids = set()

        for path in query.paths:
            direct_ids.update(self.path_index.get(path, []))

        if query.action_type:
            direct_ids.update(self.tool_index.get(query.action_type, []))

        for tag in query.tags:
            direct_ids.update(self.tag_index.get(tag, []))

        for entity in query.entities:
            direct_ids.update(self.entity_index.get(entity, []))

        if not direct_ids:
            candidates = list(self.items)
        else:
            by_id = {item.id: item for item in self.items}
            candidates = [by_id[item_id] for item_id in direct_ids if item_id in by_id]

        return [item for item in candidates if not item.is_expired(query.current_step)]

    def _rank_candidates(self, candidates: List[MemoryItem], query: MemoryQuery) -> List[Tuple[float, MemoryItem]]:
        q_tokens = Counter(tokenize(" ".join([query.task] + query.paths + query.tags + query.entities)))
        ranked: List[Tuple[float, MemoryItem]] = []

        for item in candidates:
            score = 0.0

            # token overlap
            item_tokens = Counter(
                tokenize(
                    " ".join(
                        [
                            item.summary,
                            " ".join(item.metadata.get("paths", [])),
                            " ".join(item.metadata.get("tags", [])),
                            " ".join(item.metadata.get("entities", [])),
                            " ".join(item.metadata.get("task_types", [])),
                            str(item.metadata.get("error_type") or ""),
                            str(item.metadata.get("tool") or ""),
                        ]
                    )
                )
            )
            score += self._token_score(q_tokens, item_tokens)

            # action type match
            if query.action_type and item.metadata.get("tool") == query.action_type:
                score += 2.0

            # path overlap
            paths = set(item.metadata.get("paths", []))
            if query.paths:
                overlap = len(paths.intersection(query.paths))
                score += overlap * 3.0

            # tag overlap
            tags = set(item.metadata.get("tags", []))
            if query.tags:
                overlap = len(tags.intersection(query.tags))
                score += overlap * 1.5

            # entity overlap
            entities = set(item.metadata.get("entities", []))
            if query.entities:
                overlap = len(entities.intersection(query.entities))
                score += overlap * 1.2

            # error type match
            if query.error_type and item.metadata.get("error_type") == query.error_type:
                score += 3.5

            # branch match
            if query.current_branch and item.metadata.get("branch") == query.current_branch:
                score += 0.75

            # recency
            produced_by_step = int(item.metadata.get("produced_by_step", 0) or 0)
            age_steps = max(0, query.current_step - produced_by_step)
            score += 1.0 / (1.0 + age_steps)

            # importance
            score += float(item.metadata.get("importance", 0.0) or 0.0)

            ranked.append((score, item))

        ranked.sort(key=lambda x: (-x[0], -(x[1].metadata.get("produced_by_step", 0) or 0)))
        return ranked

    def _token_score(self, q_tokens: Counter[str], item_tokens: Counter[str]) -> float:
        if not q_tokens or not item_tokens:
            return 0.0
        score = 0.0
        norm = math.sqrt(max(1, sum(item_tokens.values())))
        for token, q_weight in q_tokens.items():
            if token in item_tokens:
                score += min(q_weight, item_tokens[token])
        return score / norm

    # -----------------------------
    # Normalization helpers
    # -----------------------------

    def _infer_kind(self, tool: str, payload: Dict[str, Any]) -> str:
        if tool == "read_file":
            return "file_snapshot"
        if tool in {"write_file", "patch_file"}:
            return "edit_result"
        if tool in {"grep", "find_files"}:
            return "search_result"
        if tool == "run_shell":
            return "shell_result"
        if tool in {"git_status", "git_diff", "git_add", "git_restore", "git_commit", "git_log", "git_branch"}:
            return "git_result"
        if tool == "meta":
            return "repo_state"
        return "tool_result"

    def _extract_paths(self, action: Dict[str, Any], payload: Dict[str, Any]) -> List[str]:
        paths: List[str] = []

        action_path = action.get("path")
        if isinstance(action_path, str):
            paths.append(action_path)

        payload_path = payload.get("path")
        if isinstance(payload_path, str):
            paths.append(payload_path)

        if isinstance(payload.get("files"), list):
            for item in payload["files"]:
                if isinstance(item, str):
                    paths.append(item)
                elif isinstance(item, dict) and isinstance(item.get("path"), str):
                    paths.append(item["path"])

        if isinstance(payload.get("items"), list):
            for item in payload["items"]:
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    paths.append(item["path"])

        if isinstance(payload.get("matches"), list):
            for item in payload["matches"]:
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    paths.append(item["path"])

        for blob in [
            str(action),
            str(payload.get("stdout", "")),
            str(payload.get("stderr", "")),
            str(payload.get("diff", "")),
            str(payload.get("status", "")),
        ]:
            paths.extend(PATH_RE.findall(blob))

        return uniq(paths)[:24]

    def _extract_entities(
        self,
        task: str,
        thought: str,
        action: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> List[str]:
        text_parts = [
            task,
            thought,
            json.dumps(action, ensure_ascii=False),
            json.dumps(self._compact_content(str(action.get("type", "")), payload), ensure_ascii=False),
        ]
        tokens = tokenize("\n".join(text_parts))
        # Keep medium-signal identifiers, avoid drowning in noise
        entities = [
            tok for tok in tokens
            if len(tok) >= 3
            and tok not in {
                "json", "true", "false", "null", "path", "tool", "data", "type", "line",
                "stdout", "stderr", "returncode", "message", "content", "command", "root",
                "action", "files", "items", "matches", "limit"
            }
        ]
        return uniq(entities)[:20]

    def _infer_tags(self, tool: str, payload: Dict[str, Any], result_ok: bool) -> List[str]:
        tags = [tool]
        if result_ok:
            tags.append("success")
        else:
            tags.append("failure")

        if tool in {"read_file", "write_file", "patch_file"}:
            tags += ["file", "source"]
        if tool in {"grep", "find_files"}:
            tags += ["search", "discovery"]
        if tool == "run_shell":
            tags += ["shell", "validation"]
        if tool.startswith("git_"):
            tags += ["git", "repo_state"]

        if payload.get("returncode") not in (None, 0):
            tags.append("nonzero_returncode")
        if payload.get("diff"):
            tags.append("diff")
        if payload.get("matches"):
            tags.append("matches")
        if payload.get("stderr"):
            tags.append("stderr_present")

        error_type = self._infer_error_type(payload)
        if error_type:
            tags.append(error_type.lower())

        return uniq(tags)

    def _infer_task_types(self, task: str, tool: str) -> List[str]:
        task_l = task.lower()
        out: List[str] = []

        if any(x in task_l for x in ["fix", "bug", "error", "fail", "broken"]):
            out.append("debug")
        if any(x in task_l for x in ["edit", "change", "modify", "refactor", "update"]):
            out.append("edit")
        if any(x in task_l for x in ["test", "pytest", "validate", "check"]):
            out.append("validate")
        if any(x in task_l for x in ["commit", "git"]):
            out.append("git")
        if any(x in task_l for x in ["find", "search", "grep", "where"]):
            out.append("search")
        if any(x in task_l for x in ["explain", "understand", "inspect", "read"]):
            out.append("inspect")

        if tool == "run_shell":
            out.append("execution")
        if tool in {"write_file", "patch_file"}:
            out.append("edit")
        if tool in {"grep", "find_files"}:
            out.append("search")

        return uniq(out)

    def _infer_error_type(self, payload: Dict[str, Any]) -> Optional[str]:
        stderr = str(payload.get("stderr", "") or "")
        message = str(payload.get("message", "") or "")
        blob = stderr + "\n" + message

        patterns = [
            "SyntaxError",
            "NameError",
            "ImportError",
            "ModuleNotFoundError",
            "TypeError",
            "ValueError",
            "AssertionError",
            "FileNotFoundError",
            "PermissionError",
            "TimeoutExpired",
        ]
        for p in patterns:
            if p in blob:
                return p
        return None

    def _estimate_importance(self, tool: str, result_ok: bool, payload: Dict[str, Any]) -> float:
        score = 0.2
        if not result_ok:
            score += 0.6
        if tool in {"run_shell", "git_status", "git_diff", "write_file", "patch_file"}:
            score += 0.4
        if payload.get("returncode") not in (None, 0):
            score += 0.3
        if payload.get("diff"):
            score += 0.2
        if payload.get("matches"):
            score += 0.1
        return min(score, 2.0)

    def _estimate_ttl(self, tool: str, result_ok: bool) -> int:
        if tool in {"git_status", "git_diff", "meta"}:
            return 4
        if tool in {"run_shell"}:
            return 6 if not result_ok else 4
        if tool in {"write_file", "patch_file"}:
            return 10
        if tool in {"read_file", "grep", "find_files"}:
            return 8
        return 6

    def _summarize_item(
        self,
        *,
        tool: str,
        kind: str,
        result_ok: bool,
        result_payload: Dict[str, Any],
        paths: List[str],
        error_type: Optional[str],
    ) -> str:
        parts = [f"{tool} produced {kind}"]
        parts.append("success" if result_ok else "failure")

        if paths:
            parts.append(f"paths={paths[:3]}")
        if "returncode" in result_payload:
            parts.append(f"returncode={result_payload.get('returncode')}")
        if error_type:
            parts.append(f"error_type={error_type}")
        if "message" in result_payload:
            parts.append(f"message={shorten(str(result_payload.get('message')), 120)}")
        if "stdout" in result_payload and result_payload.get("stdout"):
            parts.append(f"stdout={shorten(str(result_payload.get('stdout')), 120)}")
        if "stderr" in result_payload and result_payload.get("stderr"):
            parts.append(f"stderr={shorten(str(result_payload.get('stderr')), 120)}")
        if "diff" in result_payload and result_payload.get("diff"):
            parts.append("diff_present")
        if "matches" in result_payload:
            parts.append(f"matches={len(result_payload.get('matches', []))}")

        return " | ".join(parts)

    def _compact_content(self, tool: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {}

        if "path" in payload:
            out["path"] = payload["path"]
        if "returncode" in payload:
            out["returncode"] = payload["returncode"]
        if "message" in payload:
            out["message"] = shorten(str(payload["message"]), 400)
        if "stdout" in payload:
            out["stdout"] = shorten(str(payload["stdout"]), 800)
        if "stderr" in payload:
            out["stderr"] = shorten(str(payload["stderr"]), 800)
        if "diff" in payload:
            out["diff"] = shorten(str(payload["diff"]), 1000)
        if "status" in payload:
            out["status"] = shorten(str(payload["status"]), 1000)
        if "branch" in payload:
            out["branch"] = payload["branch"]

        if "matches" in payload:
            matches = payload.get("matches", [])
            out["matches"] = matches[:10] if isinstance(matches, list) else matches

        if "files" in payload:
            files = payload.get("files", [])
            out["files"] = files[:20] if isinstance(files, list) else files

        if "items" in payload:
            items = payload.get("items", [])
            out["items"] = items[:20] if isinstance(items, list) else items

        if "content" in payload and tool == "read_file":
            out["content_preview"] = shorten(str(payload["content"]), 1200)

        return out