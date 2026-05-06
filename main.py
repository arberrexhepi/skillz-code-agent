#!/usr/bin/env python3
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
import importlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple, cast
from issue_facts import FACT_TYPE_ARCHITECTURE, FACT_TYPE_GOAL, IssueFactLedger, IssueFactRecord
from memory_manager import MemoryManager, MemoryQuery
from project_diagnostics import run_backend_diagnostics
from diagnostics import changed_files_check as run_changed_files_check
from diagnostics import project_problems as run_project_problems
from runtime_catalog import (
    refresh_runtime_provider_catalog_once,
    runtime_model_lines,
    runtime_options_payload,
    runtime_provider_lines,
    supported_provider_keys,
    validate_provider_model_selection,
)
from skill_loader import MarkdownSkill, load_markdown_skills_from_dir

# Simple helpers used by memory integration. These are intentionally small
# and defensive: if you replace them with more sophisticated implementations
# (tokenizers, path regex), that's fine.
import re

# A permissive path-like regex used to infer paths from a task string.
PATH_RE = re.compile(r"[A-Za-z0-9_./\\-]+\.[A-Za-z0-9_]+")


def uniq(items: List[str]) -> List[str]:
    return list(dict.fromkeys(items))


def tokenize(text: str) -> List[str]:
    return re.findall(r"\w+", text.lower())


def _load_dotenv_if_present() -> None:
    """Load a simple .env file located next to this script into os.environ.

    This is intentionally minimal: it supports lines of the form KEY=VALUE,
    ignores comments and blank lines, and does not override already-set
    environment variables.
    """
    try:
        env_path = Path(__file__).with_name(".env")
    except Exception:
        return

    if not env_path.exists():
        return

    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            os.environ.setdefault(key, val)
    except Exception:
        return


_load_dotenv_if_present()


def _env_flag_enabled(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    return default

# Third-party SDKs:
#   pip install openai google-genai anthropic
#
# Env vars:
#   OPENAI_API_KEY=...
#   GEMINI_API_KEY=...
#   ANTHROPIC_API_KEY=...
#
# Examples:
#   python agent_cli.py --provider openai --model gpt-5.4 --root /path/to/repo
#   python agent_cli.py --provider gemini --model gemini-3-flash-preview --root /path/to/repo
#   python agent_cli.py --provider anthropic --model claude-sonnet-4-20250514 --root /path/to/repo
#
# Notes:
# - OpenAI uses the Responses API with reasoning.effort.
# - Gemini uses google-genai with thinking_config.
# - All file changes are constrained to --root.
# - Shell commands run with cwd=--root.
#
# Supported agent actions:
# - read_file
# - write_file
# - list_files
# - run_shell
# - finish

try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

try:
    from google import genai
    from google.genai import types as genai_types
except Exception:
    genai = None  # type: ignore
    genai_types = None  # type: ignore

try:
    Anthropic = getattr(importlib.import_module("anthropic"), "Anthropic")
except Exception:
    Anthropic = None  # type: ignore


# -----------------------------
# Helpers / safety
# -----------------------------

MAX_FILE_READ_BYTES = 100_000
MAX_FILE_WRITE_BYTES = 500_000
MAX_SHELL_OUTPUT_CHARS = 20_000
DEFAULT_MAX_STEPS = 200
MAX_ACTION_BATCH_SIZE = 4
TEXT_MUTATION_ACTION_TYPES = {
    "write_file",
    "patch_file",
    "replace_range",
    "replace_snippet",
    "insert_before",
    "insert_after",
    "delete_range",
    "delete_snippet",
    "append_block",
    "prepend_block",
    "move_block",
}
STRUCTURAL_MUTATION_ACTION_TYPES = {
    "replace_symbol",
    "insert_symbol_member",
    "rename_symbol",
}
FILESYSTEM_MUTATION_ACTION_TYPES = {
    "create_file",
    "delete_file",
    "rename_file",
    "copy_file",
    "fill_template",
}
MUTATION_ACTION_TYPES = TEXT_MUTATION_ACTION_TYPES | STRUCTURAL_MUTATION_ACTION_TYPES | FILESYSTEM_MUTATION_ACTION_TYPES
DISCOVERY_ACTION_TYPES = {
    "list_files",
    "read_file",
    "inspect_files",
    "summarize_files",
    "find_files",
    "search_in_files",
    "outline_file",
    "read_symbol",
    "find_symbol_definitions",
    "find_symbol_references",
    "trace_dependencies",
    "find_related_files",
    "find_related_tests",
    "find_related_configs",
    "find_canonical_implementation",
    "find_similar_code",
    "find_entry_points",
    "find_ownership",
    "recent_changes",
    "get_changed_files",
    "semantic_search",
    "investigate",
    "grep",
    "symbol_search",
    "changed_files_check",
    "project_problems",
}
TOOL_BACKED_ACTION_TYPES = {
    *DISCOVERY_ACTION_TYPES,
    *MUTATION_ACTION_TYPES,
    "run_shell",
    "diagnose",
    "batch_mutate",
    "show_diff",
    "meta",
    "git_status",
    "git_diff",
    "review_changes",
    "git_add",
    "git_restore",
    "git_commit",
    "git_log",
    "git_branch",
}
FACT_ACTION_TYPES = {"set_fact", "update_fact"}
EXPLORATION_ACTION_TYPES = {
    *DISCOVERY_ACTION_TYPES,
    "skill",
    "meta",
    "git_status",
    "git_diff",
    "review_changes",
}
REPO_FACTS_FILENAME = "repo_facts.md"
OBSERVABILITY_TRACE_BLOCK_LIMIT = 24
REPO_FACTS_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)
AUTO_EDIT_ATTEMPT_FACTS_PER_PATH = 6
ESTIMATED_MODEL_PRICING: List[Dict[str, Any]] = [
    {"provider": "openai", "match": "gpt-5.4-mini", "input_per_million": 0.25, "output_per_million": 2.00},
    {"provider": "openai", "match": "gpt-5.4", "input_per_million": 1.25, "output_per_million": 10.00},
    {"provider": "openai", "match": "gpt-5.3-codex", "input_per_million": 1.50, "output_per_million": 6.00},
    {"provider": "openai", "match": "gpt-5.2", "input_per_million": 1.00, "output_per_million": 8.00},
    {"provider": "anthropic", "match": "claude-sonnet-4", "input_per_million": 3.00, "output_per_million": 15.00},
    {"provider": "anthropic", "match": "claude-opus-4", "input_per_million": 15.00, "output_per_million": 75.00},
    {"provider": "anthropic", "match": "claude-3-7-sonnet", "input_per_million": 3.00, "output_per_million": 15.00},
    {"provider": "gemini", "match": "gemini-2.5-pro", "input_per_million": 1.25, "output_per_million": 10.00},
    {"provider": "gemini", "match": "gemini-2.5-flash", "input_per_million": 0.30, "output_per_million": 2.50},
    {"provider": "gemini", "match": "gemini-3", "input_per_million": 0.30, "output_per_million": 2.50},
    {"provider": "local", "match": "", "input_per_million": 0.0, "output_per_million": 0.0},
]


def eprint(*args: Any) -> None:
    print(*args, file=sys.stderr)


def shorten(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n...[truncated]..."


def _terminal_width(default: int = 100) -> int:
    try:
        return max(72, min(shutil.get_terminal_size((default, 20)).columns, 140))
    except Exception:
        return default


def _wrap_panel_line(text: str, width: int, *, indent: str = "") -> List[str]:
    raw = str(text or "")
    if not raw:
        return [indent.rstrip()]
    wrapped: List[str] = []
    for part in raw.splitlines() or [""]:
        lines = textwrap.wrap(
            part,
            width=max(20, width - len(indent)),
            initial_indent=indent,
            subsequent_indent=indent,
            break_long_words=False,
            break_on_hyphens=False,
        )
        wrapped.extend(lines or [indent.rstrip()])
    return wrapped


def _render_text_panel(title: str, body_lines: List[str]) -> str:
    width = _terminal_width()
    inner_width = width - 4
    top = "+" + "-" * (width - 2) + "+"
    lines = [top, f"| {title[:inner_width].ljust(inner_width)} |", top]
    for raw_line in body_lines:
        for wrapped in _wrap_panel_line(raw_line, inner_width):
            lines.append(f"| {wrapped[:inner_width].ljust(inner_width)} |")
    lines.append(top)
    return "\n".join(lines)


def _json_loads_lenient(text: str) -> Any:
    candidates = [text]
    repaired = _repair_common_model_json_issues(text)
    if repaired != text:
        candidates.append(repaired)

    last_exc: Optional[Exception] = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_exc = exc
            if "Invalid control character" in str(exc):
                try:
                    return json.loads(candidate, strict=False)
                except json.JSONDecodeError as strict_exc:
                    last_exc = strict_exc
            # Model occasionally returns a Python-style dict with single quotes.
            # ast.literal_eval handles that safely without executing arbitrary code.
            import ast
            try:
                result = ast.literal_eval(candidate.strip())
                if isinstance(result, dict):
                    return result
            except Exception:
                pass

    if last_exc is not None:
        raise last_exc
    raise ValueError("Could not decode JSON payload.")


def _repair_common_model_json_issues(text: str) -> str:
    repaired = str(text or "").strip()
    if not repaired:
        return repaired

    # Remove stray quotes immediately before a closing brace/bracket. This is a
    # recurring local-provider defect in planner control JSON.
    repaired = re.sub(r'"\s*"\s*([}\]])', r'"\1', repaired)
    repaired = re.sub(r',\s*"\s*([}\]])', r'\1', repaired)

    # Remove trailing commas before object/list terminators.
    repaired = re.sub(r',\s*([}\]])', r'\1', repaired)
    return repaired


def _normalize_planner_control_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return payload

    action = payload.get("action")
    if not isinstance(action, dict):
        return payload

    action_type = str(action.get("type", "") or "").strip()
    hoist_keys: Tuple[str, ...] = ()
    if action_type == "present_plan":
        hoist_keys = (
            "summary",
            "clarification_summary",
            "assumptions",
            "not_in_scope",
            "next_steps_preview",
            "confirmation_prompt",
            "goals",
        )
    elif action_type == "offer_discovery":
        hoist_keys = ("reason", "prompt", "recommended_mode")
    elif action_type == "ask_clarification":
        hoist_keys = ("question", "reason")
    elif action_type == "respond":
        hoist_keys = ("message",)

    if not hoist_keys:
        return payload

    normalized = dict(payload)
    normalized_action = dict(action)
    for key in hoist_keys:
        if key not in normalized_action and key in normalized:
            normalized_action[key] = normalized.pop(key)
    normalized["action"] = normalized_action
    return normalized


def _safe_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except Exception:
            return 0
    return 0


def _format_usd(value: float) -> str:
    if value >= 1:
        return f"${value:.2f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.6f}"


class TokenUsageEstimator:
    def __init__(self, pricing_catalog: Optional[List[Dict[str, Any]]] = None) -> None:
        self.pricing_catalog = pricing_catalog or ESTIMATED_MODEL_PRICING

    def estimate(
        self,
        *,
        provider: str,
        model: str,
        usage: Dict[str, Any],
    ) -> TokenUsageSnapshot:
        normalized_provider = str(provider or "").strip().lower()
        normalized_model = str(model or "").strip()
        input_tokens, output_tokens, total_tokens, reasoning_tokens = self._normalize_usage(
            normalized_provider, usage
        )
        pricing = self._lookup_pricing(normalized_provider, normalized_model)

        estimated_cost_usd: Optional[float] = None
        input_cost_usd: Optional[float] = None
        output_cost_usd: Optional[float] = None
        pricing_label = "unknown"
        input_rate: Optional[float] = None
        output_rate: Optional[float] = None
        if pricing is not None:
            input_rate = float(pricing.get("input_per_million", 0.0))
            output_rate = float(pricing.get("output_per_million", 0.0))
            input_cost_usd = (input_tokens / 1_000_000.0) * input_rate
            output_cost_usd = (output_tokens / 1_000_000.0) * output_rate
            estimated_cost_usd = input_cost_usd + output_cost_usd
            pricing_label = "estimated"

        return TokenUsageSnapshot(
            provider=normalized_provider,
            model=normalized_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
            reasoning_tokens=reasoning_tokens,
            estimated_cost_usd=estimated_cost_usd,
            input_cost_usd=input_cost_usd,
            output_cost_usd=output_cost_usd,
            pricing_label=pricing_label,
            input_per_million=input_rate,
            output_per_million=output_rate,
        )

    def render_cli_summary(self, snapshot: TokenUsageSnapshot) -> str:
        parts = [
            f"tokens in={snapshot.input_tokens:,}",
            f"out={snapshot.output_tokens:,}",
            f"total={snapshot.total_tokens:,}",
        ]
        if snapshot.reasoning_tokens:
            parts.append(f"reasoning={snapshot.reasoning_tokens:,}")
        if snapshot.estimated_cost_usd is not None:
            parts.append(f"est={_format_usd(snapshot.estimated_cost_usd)}")
            if snapshot.input_per_million is not None and snapshot.output_per_million is not None:
                parts.append(
                    f"rates=in {_format_usd(snapshot.input_per_million)}/1M, out {_format_usd(snapshot.output_per_million)}/1M"
                )
        else:
            parts.append("est=unknown")
        parts.append(f"model={snapshot.model}")
        return "Usage: " + " | ".join(parts)

    def _lookup_pricing(self, provider: str, model: str) -> Optional[Dict[str, Any]]:
        provider = provider.lower()
        model_l = model.lower()
        for item in self.pricing_catalog:
            item_provider = str(item.get("provider", "")).strip().lower()
            item_match = str(item.get("match", "")).strip().lower()
            if item_provider != provider:
                continue
            if not item_match or model_l.startswith(item_match):
                return item
        return None

    def _normalize_usage(self, provider: str, usage: Dict[str, Any]) -> Tuple[int, int, int, int]:
        input_tokens = 0
        output_tokens = 0
        reasoning_tokens = 0

        if provider in {"openai", "local"}:
            input_tokens = _safe_int(usage.get("input_tokens"))
            output_tokens = _safe_int(usage.get("output_tokens"))
            reasoning_tokens = _safe_int(usage.get("reasoning_tokens"))
            total_tokens = _safe_int(usage.get("total_tokens")) or (input_tokens + output_tokens)
            return input_tokens, output_tokens, total_tokens, reasoning_tokens

        if provider == "gemini":
            input_tokens = _safe_int(usage.get("prompt_token_count"))
            output_tokens = _safe_int(usage.get("candidates_token_count"))
            reasoning_tokens = _safe_int(usage.get("thoughts_token_count"))
            total_tokens = _safe_int(usage.get("total_token_count")) or (input_tokens + output_tokens)
            return input_tokens, output_tokens, total_tokens, reasoning_tokens

        total_tokens = _safe_int(usage.get("total_tokens")) or _safe_int(usage.get("total_token_count"))
        input_tokens = _safe_int(usage.get("input_tokens")) or _safe_int(usage.get("prompt_token_count"))
        output_tokens = _safe_int(usage.get("output_tokens")) or _safe_int(usage.get("candidates_token_count"))
        if total_tokens <= 0:
            total_tokens = input_tokens + output_tokens
        return input_tokens, output_tokens, total_tokens, reasoning_tokens


class ToolbeltRunner:
    def __init__(self, tool_script: Path, root: Path, timeout: int = 60) -> None:
        self.tool_script = tool_script.resolve()
        self.root = root.resolve()
        self.timeout = timeout

    def call(self, subcommand: str, *args: str) -> Dict[str, Any]:
        cmd = [sys.executable, str(self.tool_script), subcommand, "--root", str(self.root), *args]
        result = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=self.timeout,
        )
        stdout = result.stdout.strip()
        if not stdout:
            return {
                "ok": False,
                "tool": subcommand,
                "error": {
                    "code": "NO_OUTPUT",
                    "message": "Tool returned no stdout.",
                    "stderr": result.stderr.strip(),
                    "returncode": result.returncode,
                },
            }

        try:
            return cast(Dict[str, Any], _json_loads_lenient(stdout))
        except json.JSONDecodeError:
            return {
                "ok": False,
                "tool": subcommand,
                "error": {
                    "code": "BAD_JSON",
                    "message": "Tool returned non-JSON output.",
                    "stdout": shorten(stdout, 4000),
                    "stderr": shorten(result.stderr.strip(), 4000),
                    "returncode": result.returncode,
                },
            }


class PathLockManager:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._active_reads: Dict[str, int] = {}
        self._active_writes: Dict[str, int] = {}

    def _normalize(self, path: Optional[str]) -> str:
        normalized = str(path or ".").strip().strip("/")
        return normalized or "."

    def _overlaps(self, left: str, right: str) -> bool:
        if left == "." or right == ".":
            return True
        return left == right or left.startswith(right + "/") or right.startswith(left + "/")

    def _conflicts(self, mode: str, path: str) -> bool:
        if mode == "read":
            return any(self._overlaps(path, active_path) for active_path in self._active_writes)
        return any(self._overlaps(path, active_path) for active_path in self._active_writes) or any(
            self._overlaps(path, active_path) for active_path in self._active_reads
        )

    @contextmanager
    def acquire(self, mode: str, path: Optional[str]) -> Iterator[None]:
        normalized_path = self._normalize(path)
        normalized_mode = "write" if mode == "write" else "read"
        with self._condition:
            while self._conflicts(normalized_mode, normalized_path):
                self._condition.wait()
            if normalized_mode == "read":
                self._active_reads[normalized_path] = self._active_reads.get(normalized_path, 0) + 1
            else:
                self._active_writes[normalized_path] = self._active_writes.get(normalized_path, 0) + 1
        try:
            yield
        finally:
            with self._condition:
                if normalized_mode == "read":
                    remaining = self._active_reads.get(normalized_path, 0) - 1
                    if remaining > 0:
                        self._active_reads[normalized_path] = remaining
                    else:
                        self._active_reads.pop(normalized_path, None)
                else:
                    remaining = self._active_writes.get(normalized_path, 0) - 1
                    if remaining > 0:
                        self._active_writes[normalized_path] = remaining
                    else:
                        self._active_writes.pop(normalized_path, None)
                self._condition.notify_all()


_PATH_LOCK_REGISTRY: Dict[str, PathLockManager] = {}
_PATH_LOCK_REGISTRY_GUARD = threading.Lock()


def get_shared_path_lock_manager(root: Path) -> PathLockManager:
    key = str(root.resolve())
    with _PATH_LOCK_REGISTRY_GUARD:
        manager = _PATH_LOCK_REGISTRY.get(key)
        if manager is None:
            manager = PathLockManager()
            _PATH_LOCK_REGISTRY[key] = manager
        return manager


@dataclass(frozen=True)
class ParallelToolJob:
    label: str
    subcommand: str
    args: Tuple[str, ...] = ()
    action_type: str = ""
    lock_mode: str = "read"
    lock_path: str = "."


@dataclass(frozen=True)
class ParallelToolResult:
    index: int
    job: ParallelToolJob
    result: Dict[str, Any]
    duration_s: float


class ParallelToolRunner:
    def __init__(self, toolbelt: ToolbeltRunner, lock_manager: PathLockManager, max_workers: int = 4) -> None:
        self.toolbelt = toolbelt
        self.lock_manager = lock_manager
        self.max_workers = max(1, int(max_workers))

    def _run_job(self, index: int, job: ParallelToolJob) -> ParallelToolResult:
        started = time.time()
        with self.lock_manager.acquire(job.lock_mode, job.lock_path):
            result = self.toolbelt.call(job.subcommand, *job.args)
        return ParallelToolResult(
            index=index,
            job=job,
            result=result,
            duration_s=time.time() - started,
        )

    def run_batch(self, jobs: List[ParallelToolJob]) -> List[ParallelToolResult]:
        if not jobs:
            return []
        max_workers = min(self.max_workers, len(jobs))
        results: List[ParallelToolResult] = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(self._run_job, index, job) for index, job in enumerate(jobs)]
            for future in as_completed(futures):
                results.append(future.result())
        return sorted(results, key=lambda item: item.index)


def extract_first_json_object(text: str) -> Dict[str, Any]:
    """
    Accepts raw JSON or JSON inside a fenced block.
    """
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text.strip()

    # Try direct parse first.
    try:
        result = cast(Dict[str, Any], _json_loads_lenient(candidate))
        return _normalize_planner_control_payload(result)
    except json.JSONDecodeError:
        pass

    # Fall back to first balanced object.
    start = candidate.find("{")
    if start == -1:
        raise ValueError("No JSON object found in model output.")

    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(candidate[start:], start=start):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False

        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    result = cast(Dict[str, Any], _json_loads_lenient(candidate[start:i + 1]))
                    return _normalize_planner_control_payload(result)

    raise ValueError("Could not parse JSON object from model output.")


def ensure_within_root(root: Path, target: Path) -> Path:
    root = root.resolve()
    target = target.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise ValueError(f"Path escapes root: {target}")
    return target


def safe_join(root: Path, rel_path: str) -> Path:
    if not rel_path or rel_path.strip() == "":
        raise ValueError("Empty path.")
    path = (root / rel_path).resolve()
    return ensure_within_root(root, path)


def read_text_file(path: Path) -> str:
    data = path.read_bytes()
    if len(data) > MAX_FILE_READ_BYTES:
        raise ValueError(f"File too large to read safely ({len(data)} bytes).")
    return data.decode("utf-8", errors="replace")


def write_text_file(path: Path, content: str) -> None:
    data = content.encode("utf-8")
    if len(data) > MAX_FILE_WRITE_BYTES:
        raise ValueError(f"Refusing to write oversized file ({len(data)} bytes).")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def list_files(root: Path, limit: int = 200) -> List[str]:
    files: List[str] = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            try:
                rel = str(p.relative_to(root))
            except Exception:
                continue
            # Skip common noisy dirs
            parts = set(p.parts)
            if any(x in parts for x in {".git", "node_modules", ".venv", "venv", "__pycache__"}):
                continue
            files.append(rel)
            if len(files) >= limit:
                break
    return files


def run_shell(root: Path, command: str, timeout: int = 60) -> Dict[str, Any]:
    if not _env_flag_enabled("SHELL_ACCESS", True):
        return {
            "returncode": 126,
            "stdout": "",
            "stderr": "Shell access is disabled by SHELL_ACCESS=false.",
        }
    result = subprocess.run(
        command,
        cwd=str(root),
        shell=True,
        text=True,
        capture_output=True,
        timeout=timeout,
    )
    return {
        "returncode": result.returncode,
        "stdout": shorten(result.stdout, MAX_SHELL_OUTPUT_CHARS),
        "stderr": shorten(result.stderr, MAX_SHELL_OUTPUT_CHARS),
    }



# -----------------------------
# Backoff strategy
# -----------------------------

@dataclass
class BackoffStrategy:
    enabled: bool = False
    token_limit_k: int = 0  # input tokens per minute, in thousands (e.g. 30 = 30000)
    _window_start: float = field(default=0.0, repr=False)
    _window_tokens: int = field(default=0, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def token_limit(self) -> int:
        return self.token_limit_k * 1000

    def record_tokens(self, input_tokens: int) -> None:
        with self._lock:
            now = time.time()
            if now - self._window_start >= 60.0:
                self._window_start = now
                self._window_tokens = 0
            self._window_tokens += max(0, input_tokens)

    def should_wait(self) -> bool:
        if not self.enabled or self.token_limit <= 0:
            return False
        with self._lock:
            now = time.time()
            if now - self._window_start >= 60.0:
                return False
            return self._window_tokens >= self.token_limit

    def wait_seconds(self) -> float:
        with self._lock:
            elapsed = time.time() - self._window_start
            return max(0.0, 60.0 - elapsed)

    def window_tokens_used(self) -> int:
        with self._lock:
            now = time.time()
            if now - self._window_start >= 60.0:
                return 0
            return self._window_tokens

    def to_dict(self) -> Dict[str, Any]:
        return {
            "enabled": self.enabled,
            "token_limit_k": self.token_limit_k,
            "window_tokens_used": self.window_tokens_used(),
        }


def _is_rate_limit_error(exc: Exception) -> bool:
    cls_name = type(exc).__name__.lower()
    if "ratelimit" in cls_name or "rate_limit" in cls_name:
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None) or getattr(exc, "code", None)
    if status == 429:
        return True
    msg = str(exc).lower()
    return "rate limit" in msg or "rate_limit" in msg or "429" in msg[:20]


_BACKOFF_MAX_RETRIES = 3


# -----------------------------
# Model clients
# -----------------------------

class BaseModelClient:
    backoff: BackoffStrategy

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

    def complete(self, system: str, prompt: str) -> str:
        raise NotImplementedError

    def _complete_with_backoff(self, system: str, prompt: str) -> str:
        """Wrap the actual provider call with rate-limit backoff and retry."""
        backoff = getattr(self, "backoff", None)
        if backoff is None:
            self.backoff = BackoffStrategy()
            backoff = self.backoff
        for attempt in range(_BACKOFF_MAX_RETRIES):
            if backoff.enabled and backoff.should_wait():
                wait = backoff.wait_seconds()
                if wait > 0:
                    eprint(f"Backoff: token window limit reached ({backoff.token_limit_k}k). Waiting {wait:.0f}s...")
                    time.sleep(wait)
            try:
                result = self._do_complete(system, prompt)
                # record input tokens from last metrics
                metrics = self.get_last_metrics()
                usage = metrics.get("usage", {}) if isinstance(metrics, dict) else {}
                input_tokens = (
                    _safe_int(usage.get("input_tokens"))
                    or _safe_int(usage.get("prompt_token_count"))
                    or 0
                )
                if input_tokens > 0 and backoff.enabled:
                    backoff.record_tokens(input_tokens)
                return result
            except Exception as exc:
                if _is_rate_limit_error(exc) and attempt < _BACKOFF_MAX_RETRIES - 1:
                    wait = max(60.0, backoff.wait_seconds()) if backoff.enabled else 60.0
                    eprint(f"Rate limited (attempt {attempt + 1}/{_BACKOFF_MAX_RETRIES}). Waiting {wait:.0f}s before retry...")
                    time.sleep(wait)
                    # reset window after forced wait
                    if backoff.enabled:
                        with backoff._lock:
                            backoff._window_start = time.time()
                            backoff._window_tokens = 0
                    continue
                raise
        # unreachable but satisfies type checker
        raise RuntimeError("Backoff retries exhausted")

    def _do_complete(self, system: str, prompt: str) -> str:
        raise NotImplementedError

    def clone(self) -> "BaseModelClient":
        raise NotImplementedError

    def _set_last_metrics(self, metrics: Dict[str, Any]) -> None:
        self._last_metrics = metrics

    def get_last_metrics(self) -> Dict[str, Any]:
        metrics = getattr(self, "_last_metrics", None)
        return metrics if isinstance(metrics, dict) else {}


class AnthropicModelClient(BaseModelClient):
    def __init__(
        self,
        model: str,
        thinking_mode: str = "medium",
    ) -> None:
        if Anthropic is None:
            raise RuntimeError("anthropic package not installed. Run: pip install anthropic")
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set.")
        self.client = Anthropic(api_key=api_key)
        self.model = model
        self.thinking_mode = thinking_mode
        self.backoff = BackoffStrategy()

    def clone(self) -> BaseModelClient:
        c = AnthropicModelClient(
            model=self.model,
            thinking_mode=self.thinking_mode,
        )
        c.backoff = BackoffStrategy(enabled=self.backoff.enabled, token_limit_k=self.backoff.token_limit_k)
        return c

    def complete(self, system: str, prompt: str) -> str:
        return self._complete_with_backoff(system, prompt)

    def _do_complete(self, system: str, prompt: str) -> str:
        response = self.client.messages.create(
            model=self.model,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4096,
            temperature=0.1,
        )
        usage = getattr(response, "usage", None)
        usage_dict: Dict[str, Any] = {}
        if usage is not None:
            if isinstance(usage, dict):
                usage_dict = dict(usage)
            else:
                for key in [
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                ]:
                    value = getattr(usage, key, None)
                    if value is not None:
                        usage_dict[key] = value
        self._set_last_metrics(
            {
                "provider": "anthropic",
                "model": self.model,
                "usage": usage_dict,
            }
        )
        parts: List[str] = []
        for block in getattr(response, "content", []) or []:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
        return "\n".join(parts).strip()


class OpenAIModelClient(BaseModelClient):
    def __init__(
        self,
        model: str,
        thinking_mode: str = "medium",
        verbosity: str = "medium",
    ) -> None:
        if OpenAI is None:
            raise RuntimeError("openai package not installed. Run: pip install openai")
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not set.")
        self.client = OpenAI(api_key=api_key)
        self.model = model
        self.thinking_mode = thinking_mode
        self.verbosity = verbosity
        self.backoff = BackoffStrategy()

    def clone(self) -> BaseModelClient:
        c = OpenAIModelClient(
            model=self.model,
            thinking_mode=self.thinking_mode,
            verbosity=self.verbosity,
        )
        c.backoff = BackoffStrategy(enabled=self.backoff.enabled, token_limit_k=self.backoff.token_limit_k)
        return c

    def complete(self, system: str, prompt: str) -> str:
        return self._complete_with_backoff(system, prompt)

    def _do_complete(self, system: str, prompt: str) -> str:
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "input": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
        }

        # OpenAI Responses API reasoning effort.
        if self.thinking_mode and self.thinking_mode != "auto":
            kwargs["reasoning"] = {"effort": self.thinking_mode}

        # Verbosity is supported on newer GPT-5.x models; harmless to omit if not desired.
        if self.verbosity:
            kwargs["text"] = {"verbosity": self.verbosity}

        response = self.client.responses.create(**kwargs)
        usage = getattr(response, "usage", None)
        usage_dict: Dict[str, Any] = {}
        if usage is not None:
            if isinstance(usage, dict):
                usage_dict = dict(usage)
            else:
                usage_dict = {
                    "input_tokens": getattr(usage, "input_tokens", None),
                    "output_tokens": getattr(usage, "output_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                }
                output_details = getattr(usage, "output_tokens_details", None)
                if output_details is not None:
                    reasoning_tokens = getattr(output_details, "reasoning_tokens", None)
                    if reasoning_tokens is not None:
                        usage_dict["reasoning_tokens"] = reasoning_tokens
        self._set_last_metrics(
            {
                "provider": "openai",
                "model": self.model,
                "usage": usage_dict,
            }
        )
        text = getattr(response, "output_text", None)
        if text:
            return text

        # Fallback extraction.
        chunks: List[str] = []
        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                if getattr(content, "type", None) == "output_text":
                    chunks.append(getattr(content, "text", ""))
        return "\n".join(chunks).strip()


class LocalModelClient(OpenAIModelClient):
    def __init__(
        self,
        model: str,
        thinking_mode: str = "medium",
        verbosity: str = "medium",
    ) -> None:
        if OpenAI is None:
            raise RuntimeError("openai package not installed. Run: pip install openai")
        # Local provider is OpenAI-compatible; use a dummy key if none provided.
        api_key = os.getenv("LOCAL_LLM_API_KEY") or os.getenv("OPENAI_API_KEY") or "local-key"
        self.client = OpenAI(api_key=api_key, base_url="http://127.0.0.1:5051/v1")
        self.model = model
        self.thinking_mode = thinking_mode
        self.verbosity = verbosity
        self.backoff = BackoffStrategy()

    def clone(self) -> BaseModelClient:
        c = LocalModelClient(
            model=self.model,
            thinking_mode=self.thinking_mode,
            verbosity=self.verbosity,
        )
        c.backoff = BackoffStrategy(enabled=self.backoff.enabled, token_limit_k=self.backoff.token_limit_k)
        return c


def _normalize_chat_completions_base_url(url: str) -> str:
    normalized = str(url or "").strip().rstrip("/")
    if normalized.endswith("/chat/completions"):
        normalized = normalized[: -len("/chat/completions")]
    return normalized


class OllamaModelClient(BaseModelClient):
    def __init__(
        self,
        model: str,
        thinking_mode: str = "medium",
        verbosity: str = "medium",
        *,
        base_url: str = "http://127.0.0.1:11434/v1",
        api_key: str = "ollama-key",
        provider_name: str = "ollama-local",
    ) -> None:
        if OpenAI is None:
            raise RuntimeError("openai package not installed. Run: pip install openai")
        self.client = OpenAI(api_key=api_key, base_url=_normalize_chat_completions_base_url(base_url))
        self.model = model
        self.thinking_mode = thinking_mode
        self.verbosity = verbosity
        self.base_url = _normalize_chat_completions_base_url(base_url)
        self.provider_name = provider_name
        self.backoff = BackoffStrategy()

    def clone(self) -> BaseModelClient:
        c = OllamaModelClient(
            model=self.model,
            thinking_mode=self.thinking_mode,
            verbosity=self.verbosity,
            base_url=self.base_url,
            provider_name=self.provider_name,
        )
        c.backoff = BackoffStrategy(enabled=self.backoff.enabled, token_limit_k=self.backoff.token_limit_k)
        return c

    def complete(self, system: str, prompt: str) -> str:
        return self._complete_with_backoff(system, prompt)

    def _do_complete(self, system: str, prompt: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        usage = getattr(response, "usage", None)
        usage_dict: Dict[str, Any] = {}
        if usage is not None:
            if isinstance(usage, dict):
                usage_dict = dict(usage)
            else:
                usage_dict = {
                    "input_tokens": getattr(usage, "prompt_tokens", None),
                    "output_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                }
        self._set_last_metrics(
            {
                "provider": self.provider_name,
                "model": self.model,
                "usage": usage_dict,
            }
        )
        choices = getattr(response, "choices", []) or []
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        if message is None:
            return ""
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            chunks: List[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = str(item.get("text", "") or "").strip()
                    if text:
                        chunks.append(text)
                else:
                    text = str(getattr(item, "text", "") or "").strip()
                    if text:
                        chunks.append(text)
            return "\n".join(chunks).strip()
        return str(content or "").strip()


class GeminiModelClient(BaseModelClient):
    def __init__(
        self,
        model: str,
        thinking_mode: str = "auto",
    ) -> None:
        if genai is None or genai_types is None:
            raise RuntimeError("google-genai package not installed. Run: pip install google-genai")
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise RuntimeError("GEMINI_API_KEY is not set.")
        self.client = genai.Client(api_key=api_key)
        self.model = model
        self.thinking_mode = thinking_mode
        self.backoff = BackoffStrategy()

    def clone(self) -> BaseModelClient:
        c = GeminiModelClient(
            model=self.model,
            thinking_mode=self.thinking_mode,
        )
        c.backoff = BackoffStrategy(enabled=self.backoff.enabled, token_limit_k=self.backoff.token_limit_k)
        return c

    def _build_thinking_config(self) -> Optional[Any]:
        """
        Gemini 3 models: thinking_level
        Gemini 2.5 models: thinking_budget
        """
        m = self.model.lower()

        # Help static type checkers understand that genai_types is not None
        assert genai_types is not None

        if "gemini-3" in m:
            if self.thinking_mode in {"auto", "", None}:  # type: ignore
                return None
            mapping = {
                "minimal": "minimal",
                "low": "low",
                "medium": "medium",
                "high": "high",
            }
            level = mapping.get(self.thinking_mode)
            if not level:
                return None
            thinking_config_ctor = cast(Any, genai_types.ThinkingConfig)
            return thinking_config_ctor(thinking_level=level)

        # Gemini 2.5 style thinking_budget
        if "gemini-2.5" in m:
            mapping = {
                "none": 0,
                "minimal": 256,
                "low": 1024,
                "medium": 4096,
                "high": 8192,
                "auto": -1,
            }
            budget = mapping.get(self.thinking_mode, -1)
            thinking_config_ctor = cast(Any, genai_types.ThinkingConfig)
            return thinking_config_ctor(thinking_budget=budget)

        return None

    def complete(self, system: str, prompt: str) -> str:
        return self._complete_with_backoff(system, prompt)

    def _do_complete(self, system: str, prompt: str) -> str:
        config_kwargs: Dict[str, Any] = {
            "system_instruction": system,
            "temperature": 0.1,
        }
        thinking_config = self._build_thinking_config()
        if thinking_config is not None:
            config_kwargs["thinking_config"] = thinking_config

        # genai_types should be present because __init__ raised otherwise; assert for type checkers
        assert genai_types is not None
        response = self.client.models.generate_content(
            model=self.model,
            contents=prompt,
            config=genai_types.GenerateContentConfig(**config_kwargs),
        )
        usage_meta = getattr(response, "usage_metadata", None) or getattr(response, "usageMetadata", None)
        usage_dict: Dict[str, Any] = {}
        if usage_meta is not None:
            if isinstance(usage_meta, dict):
                usage_dict = dict(usage_meta)
            else:
                for key in [
                    "prompt_token_count",
                    "candidates_token_count",
                    "total_token_count",
                    "thoughts_token_count",
                ]:
                    value = getattr(usage_meta, key, None)
                    if value is not None:
                        usage_dict[key] = value
        self._set_last_metrics(
            {
                "provider": "gemini",
                "model": self.model,
                "usage": usage_dict,
            }
        )
        text = getattr(response, "text", None)
        if text:
            return text

        # Fallback extraction.
        parts: List[str] = []
        for cand in getattr(response, "candidates", []) or []:
            content = getattr(cand, "content", None)
            if not content:
                continue
            for part in getattr(content, "parts", []) or []:
                txt = getattr(part, "text", None)
                if txt:
                    parts.append(txt)
        return "\n".join(parts).strip()


@dataclass
class FactSubagentDecision:
    thought: str
    action: Dict[str, Any]
    raw: str = ""


class FactSubagent:
    def __init__(self, model_client: BaseModelClient) -> None:
        self.model = model_client

    def decide(
        self,
        *,
        task: str,
        fact_context: str,
        selected_goal_facts: str,
        recent_events: List[Dict[str, Any]],
    ) -> Optional[FactSubagentDecision]:
        raw = self.model.complete(
            self._system_prompt(),
            self._build_prompt(
                task=task,
                fact_context=fact_context,
                selected_goal_facts=selected_goal_facts,
                recent_events=recent_events,
            ),
        )
        try:
            obj = extract_first_json_object(raw)
        except Exception:
            return None

        thought = str(obj.get("thought", "") or "").strip()
        action = obj.get("action")
        if not isinstance(action, dict):
            return None
        action_type = str(action.get("type", "") or "").strip()
        if action_type not in {"set_fact", "update_fact"}:
            return None
        return FactSubagentDecision(thought=thought, action=action, raw=raw)

    def _system_prompt(self) -> str:
        return textwrap.dedent(
            """
            You are an issue-aware repo-fact subagent assisting a coding worker.

            You do not call tools. You only inspect the provided recent tool thoughts and results,
            then decide whether to record a durable repo fact.

            Return exactly one JSON object and nothing else.

            Schema:
            {
              "thought": "brief reasoning",
              "action": {
                "type": "set_fact" | "update_fact",
                "key": "fact_key",
                "value": "concise fact",
                "fact_type": "goal" | "architecture"
              }
            }

            Rules:
            - Every exploration produces a fact. Choose set_fact for a new fact or update_fact to refine an existing one.
            - Always include fact_type as either goal or architecture.
            - Use fact_type=goal for issue-local execution findings, tactical constraints, and facts tied to this specific task.
            - Use fact_type=architecture for reusable repo knowledge that should remain available across unrelated future tasks.
            - If a finding is mainly about a write attempt, diff, verification latch, or other tactical execution state, use fact_type=goal.
            - Facts must be concise and durable. Prefer one or two sentences.
            - Do not include line numbers, step-by-step plans, risks, or broad run summaries.
            - Reuse an existing fact key when the new information is clearly the same durable concept.
            """
        ).strip()

    def _build_prompt(
        self,
        *,
        task: str,
        fact_context: str,
        selected_goal_facts: str,
        recent_events: List[Dict[str, Any]],
    ) -> str:
        return textwrap.dedent(
            f"""
            TASK:
            {task}

            FACT CONTEXT:
            {fact_context}

            SELECTED GOAL FACTS:
            {selected_goal_facts}

            RECENT EXPLORATION EVENTS:
            {json.dumps(recent_events, indent=2)}

            Record a fact from these recent exploration events.
            Use fact_type=goal for task-local findings, fact_type=architecture for cross-task repo knowledge.
            """
        ).strip()


# -----------------------------
# Agent state
# -----------------------------

@dataclass
class ActionResult:
    ok: bool
    name: str
    payload: Dict[str, Any]


@dataclass
class ToolOutcome:
    ok: bool
    action_type: str
    status: str
    summary: str
    data: Optional[Dict[str, Any]]
    error: Optional[Dict[str, Any]]
    raw: Dict[str, Any]
    next_hint: Optional[str] = None


@dataclass
class AgentStep:
    step: int
    thought: str
    action: Dict[str, Any]
    result: ActionResult
    elapsed_s: float
    run_id: int = 0


@dataclass
class AgentConfig:
    provider: str
    model: str
    root: Path
    tool_script: Path
    max_steps: int = DEFAULT_MAX_STEPS
    shell_timeout: int = 60
    thinking_mode: str = "medium"
    verbosity: str = "medium"
    show_prompts: bool = False
    show_model_output: bool = False
    auto_confirm_write: bool = True
    auto_confirm_shell: bool = True
    allow_shell: bool = True
    memory_limit: int = 5000
    memory_retrieval_limit: int = 8
    max_parallel_workers: int = 4
    quiet: bool = False


@dataclass
class DiscoveryBudget:
    mode_key: str
    mode_label: str
    max_tool_calls: int
    tool_calls_used: int = 0

    @property
    def remaining_tool_calls(self) -> int:
        return max(0, self.max_tool_calls - self.tool_calls_used)

    @property
    def exhausted(self) -> bool:
        return self.tool_calls_used >= self.max_tool_calls


@dataclass
class ActiveErrorState:
    task: str
    action_type: str
    error_type: Optional[str]
    message: str
    path: str = ""
    step: int = 0
    diagnostic_engine: Optional[str] = None
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    suggested_next_actions: List[Dict[str, Any]] = field(default_factory=list)

    def label(self) -> str:
        suffix = self.path or self.action_type or "unknown"
        return f"active_error::{suffix}"


FactRecord = IssueFactRecord


@dataclass(frozen=True)
class TokenUsageSnapshot:
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    total_tokens: int
    reasoning_tokens: int = 0
    estimated_cost_usd: Optional[float] = None
    input_cost_usd: Optional[float] = None
    output_cost_usd: Optional[float] = None
    pricing_label: str = "unknown"
    input_per_million: Optional[float] = None
    output_per_million: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "estimated_cost_usd": self.estimated_cost_usd,
            "input_cost_usd": self.input_cost_usd,
            "output_cost_usd": self.output_cost_usd,
            "pricing_label": self.pricing_label,
            "input_per_million": self.input_per_million,
            "output_per_million": self.output_per_million,
        }


@dataclass(frozen=True)
class WorkerValidationResult:
    kind: str = "none"
    passed: bool = False
    summary: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "passed": self.passed,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class WorkerRunResult:
    ok: bool
    final_message: str
    task_satisfied: bool
    validation_ran: bool
    validation_passed: bool
    touched_paths: List[str] = field(default_factory=list)
    validation: WorkerValidationResult = field(default_factory=WorkerValidationResult)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "final_message": self.final_message,
            "task_satisfied": self.task_satisfied,
            "validation_ran": self.validation_ran,
            "validation_passed": self.validation_passed,
            "touched_paths": list(self.touched_paths),
            "validation": self.validation.to_dict(),
        }


# -----------------------------
# Agent
# -----------------------------

class WorkingFolderAgent:
    def __init__(self, model_client: BaseModelClient, config: AgentConfig) -> None:
        self.model = model_client
        self.config = config
        self.root = config.root.resolve()
        self.usage_estimator = TokenUsageEstimator()
        self.fact_subagent = FactSubagent(self.model)
        self.on_step_callback: Optional[Callable[["AgentStep"], None]] = None
        self._init_runtime_state()
        self._init_memory()
        self._init_tools()
        self._load_registered_skills()
        self._load_repo_facts_into_map()

    def _shell_access_enabled(self) -> bool:
        return bool(self.config.allow_shell)

    def _shell_access_note(self) -> str:
        if self._shell_access_enabled():
            return ""
        return "SHELL ACCESS POLICY: SHELL_ACCESS=false. Do not emit `run_shell`; use file, git, discovery, diagnostics, or structured mutation actions instead."

    def _init_runtime_state(self) -> None:
        self.history: List[AgentStep] = []
        self._current_task = ""
        self.task_satisfied = False
        self.satisfaction_reason = ""
        self.post_satisfaction_checks = 0
        self.completion_check_pending = False
        self.completion_check_reason = ""
        self.pending_verification = None
        self.edit_batch_mode = False
        self.edit_batch_pending: Dict[str, Dict[str, Any]] = {}
        self.edit_batch_started_step = 0
        self.edit_batch_started_thought = ""
        self.pending_fact_resolution = None
        self.active_error = None
        self.output_format_recovery: Optional[Dict[str, Any]] = None
        self.fact_map = {}
        self.issue_ledger = IssueFactLedger.empty()
        self._run_sequence = 0
        self._active_run_id = 0
        self._current_thought = ""
        self._repo_facts_loaded_count = 0
        self._observability_buffer = []
        self._run_metrics = {}
        self._run_started_at = 0.0
        self.steering_prompt = ""
        self.selected_goal_fact_keys: List[str] = []
        self.discovery_budget = None
        self.pending_patch_recovery = None
        self._repo_snapshot_cache = None
        self._repo_snapshot_cache_ts = 0.0
        self._safe_branch_cache = None
        self._safe_branch_cache_ts = 0.0
        self._last_run_result: Optional[WorkerRunResult] = None
        self.recent_resolution_handoff: Optional[Dict[str, Any]] = None
        self.active_context = {
            "strategy": None,
            "items": [],
            "notes": [],
        }
        self.fulfillment_mode = False
        self._last_strategy = None

    def reconfigure_runtime(
        self,
        *,
        provider: str,
        model: str,
        thinking_mode: Optional[str] = None,
        verbosity: Optional[str] = None,
    ) -> Dict[str, Any]:
        next_provider = str(provider or self.config.provider).strip().lower()
        next_model = str(model or self.config.model).strip()
        next_thinking = str(thinking_mode or self.config.thinking_mode).strip() or self.config.thinking_mode
        next_verbosity = str(verbosity or self.config.verbosity).strip() or self.config.verbosity
        next_client = create_model_client(
            provider=next_provider,
            model=next_model,
            thinking_mode=next_thinking,
            verbosity=next_verbosity,
        )
        old_client = self.model
        self.model = next_client
        # carry backoff strategy from previous client to new one
        old_backoff = getattr(old_client, "backoff", None)
        if old_backoff is not None and isinstance(old_backoff, BackoffStrategy):
            self.model.backoff = BackoffStrategy(enabled=old_backoff.enabled, token_limit_k=old_backoff.token_limit_k)
        self.fact_subagent = FactSubagent(self.model)
        self.config.provider = next_provider
        self.config.model = next_model
        self.config.thinking_mode = next_thinking
        self.config.verbosity = next_verbosity
        return {
            "provider": self.config.provider,
            "model": self.config.model,
            "thinking_mode": self.config.thinking_mode,
            "verbosity": self.config.verbosity,
        }

    def configure_backoff(self, *, enabled: bool, token_limit_k: int = 0) -> Dict[str, Any]:
        backoff = getattr(self.model, "backoff", None)
        if backoff is None or not isinstance(backoff, BackoffStrategy):
            self.model.backoff = BackoffStrategy()
            backoff = self.model.backoff
        backoff.enabled = enabled
        backoff.token_limit_k = max(0, int(token_limit_k))
        return backoff.to_dict()

    def get_backoff_state(self) -> Dict[str, Any]:
        backoff = getattr(self.model, "backoff", None)
        if backoff is not None and isinstance(backoff, BackoffStrategy):
            return backoff.to_dict()
        return {"enabled": False, "token_limit_k": 0, "window_tokens_used": 0}

    def _init_memory(self) -> None:
        try:
            self.memory = MemoryManager(
                root=self.root,
                capacity=self.config.memory_limit,
            )
        except Exception:
            class _NoopMemory:
                def stats(self) -> Dict[str, Any]:
                    return {}

                def lookup(self, q: Any) -> Dict[str, Any]:
                    return {}

                def ingest_step(self, **kwargs: Any) -> None:
                    return None

            self.memory = _NoopMemory()  # type: ignore

    def _init_tools(self) -> None:
        self.tools = ToolbeltRunner(
            tool_script=self.config.tool_script,
            root=self.root,
            timeout=max(self.config.shell_timeout, 60),
        )
        self.path_locks = get_shared_path_lock_manager(self.root)
        self.parallel_tools = ParallelToolRunner(
            self.tools,
            self.path_locks,
            max_workers=self.config.max_parallel_workers,
        )

    def _load_registered_skills(self) -> None:
        bundled_skills_dir = Path(__file__).resolve().parent / "skills"
        workspace_skills_dir = self.root / "skills"
        loaded: Dict[str, MarkdownSkill] = {}
        for directory in [bundled_skills_dir, workspace_skills_dir]:
            for skill in load_markdown_skills_from_dir(directory):
                loaded[skill.name] = skill
        self._registered_skills = loaded

    def _available_skills_payload(self) -> List[Dict[str, Any]]:
        skills: List[Dict[str, Any]] = []
        for skill in self._registered_skills.values():
            skills.append(
                {
                    "name": skill.name,
                    "description": skill.description,
                    "args_schema": dict(skill.args_schema),
                    "tags": list(skill.tags),
                    "category": skill.category,
                    "priority": skill.priority,
                    "modes": list(skill.modes),
                }
            )
        return sorted(skills, key=lambda item: (-int(item.get("priority", 0)), str(item.get("name", ""))))

    def _invalidate_fast_caches(self) -> None:
        self._repo_snapshot_cache = None
        self._repo_snapshot_cache_ts = 0.0
        self._safe_branch_cache = None
        self._safe_branch_cache_ts = 0.0


    def set_steering(self, prompt: str) -> None:
        self.steering_prompt = prompt.strip()

    def clear_steering(self) -> None:
        self.steering_prompt = ""

    def _delete_session_artifacts(self) -> None:
        targets = [self._repo_facts_path(), *self._observability_targets()]
        seen: set[str] = set()
        for target in targets:
            target_str = str(target)
            if not target_str or target_str in seen:
                continue
            seen.add(target_str)
            try:
                target.unlink(missing_ok=True)
            except Exception:
                continue

    def delete_session(self) -> str:
        self._delete_session_artifacts()
        self._init_runtime_state()
        return "Session deleted. Repo facts and observability were cleared."

    def set_goal_fact_keys(self, keys: List[str]) -> None:
        selected: List[str] = []
        for key in keys:
            normalized = str(key or "").strip()
            if normalized and normalized not in selected:
                selected.append(normalized)
        self.selected_goal_fact_keys = selected

    def clear_goal_fact_keys(self) -> None:
        self.selected_goal_fact_keys = []

    def configure_discovery_budget(self, mode_key: str, mode_label: str, max_tool_calls: int) -> None:
        self.discovery_budget = DiscoveryBudget(
            mode_key=mode_key,
            mode_label=mode_label,
            max_tool_calls=max(0, int(max_tool_calls)),
        )

    def clear_discovery_budget(self) -> None:
        self.discovery_budget = None

    def prepare_for_goal(self, preserve_context: bool) -> None:
        self._reset_task_satisfaction()
        self._clear_completion_check(keep_satisfaction=False)
        self._clear_patch_recovery()
        self._clear_edit_batch_state()
        self._clear_pending_verification()
        self._clear_pending_fact_resolution()
        self._clear_active_error()
        self._clear_output_format_recovery()
        self.clear_goal_fact_keys()
        self.clear_discovery_budget()
        if not preserve_context:
            self._clear_active_context()
            self._clear_facts()
        self._current_task = ""

    def get_last_run_metrics(self) -> Dict[str, Any]:
        return dict(self._run_metrics) if isinstance(self._run_metrics, dict) else {}

    def get_last_run_result(self) -> Optional[WorkerRunResult]:
        return self._last_run_result

    def get_last_token_usage_snapshot(self) -> Optional[TokenUsageSnapshot]:
        metrics = self.get_last_run_metrics()
        usage = metrics.get("llm_usage")
        if not isinstance(usage, dict):
            return None
        return self.usage_estimator.estimate(
            provider=str(metrics.get("provider") or self.config.provider),
            model=str(metrics.get("model") or self.config.model),
            usage=usage,
        )

    def render_last_usage_summary(self) -> str:
        snapshot = self.get_last_token_usage_snapshot()
        if snapshot is None:
            return "Usage: unavailable"
        return self.usage_estimator.render_cli_summary(snapshot)

    def export_runtime_state(self) -> Dict[str, Any]:
        previous_run_records, current_run_records = self._fact_records_by_run_scope()
        last_run = self.get_last_run_result()
        active_error = None
        if self.active_error is not None:
            active_error = {
                "task": self.active_error.task,
                "action_type": self.active_error.action_type,
                "error_type": self.active_error.error_type,
                "message": self.active_error.message,
                "path": self.active_error.path,
                "step": self.active_error.step,
                "diagnostic_engine": self.active_error.diagnostic_engine,
                "diagnostics": [dict(item) for item in self.active_error.diagnostics],
                "suggested_next_actions": [dict(item) for item in self.active_error.suggested_next_actions],
            }
        pending_verification = None
        if isinstance(self.pending_verification, dict):
            pending_verification = dict(self.pending_verification)
        pending_patch_recovery = None
        if isinstance(self.pending_patch_recovery, dict):
            pending_patch_recovery = dict(self.pending_patch_recovery)
        output_format_recovery = None
        if isinstance(self.output_format_recovery, dict):
            output_format_recovery = dict(self.output_format_recovery)
        return {
            "runtime_config": {
                "provider": self.config.provider,
                "model": self.config.model,
                "thinking_mode": self.config.thinking_mode,
                "verbosity": self.config.verbosity,
            },
            "current_task": self._current_task,
            "task_satisfied": self.task_satisfied,
            "satisfaction_reason": self.satisfaction_reason,
            "completion_check_pending": self.completion_check_pending,
            "completion_check_reason": self.completion_check_reason,
            "active_error": active_error,
            "output_format_recovery": output_format_recovery,
            "active_mode_strategy": self._active_mode_strategy(),
            "pending_patch_recovery": pending_patch_recovery,
            "pending_verification": pending_verification,
            "edit_batch": {
                "active": self.edit_batch_mode,
                "started_step": self.edit_batch_started_step,
                "started_thought": self.edit_batch_started_thought,
                "pending_paths": sorted(self.edit_batch_pending.keys()),
                "pending_count": len(self.edit_batch_pending),
            },
            "selected_goal_fact_keys": list(self.selected_goal_fact_keys),
            "issue_state": self._issue_state_payload(),
            "current_run_facts": [record.to_dict() for record in current_run_records],
            "previous_turn_facts": [record.to_dict() for record in previous_run_records],
            "repo_facts_status_lines": self.repo_facts_status_lines(),
            "active_context_notes": [str(item) for item in self.active_context.get("notes", []) if str(item).strip()],
            "last_run_result": last_run.to_dict() if last_run is not None else None,
            "last_usage_summary": self.render_last_usage_summary(),
            "run_metrics": self.get_last_run_metrics(),
            "latest_diagnostics": self._latest_diagnostics_state(),
            "latest_review": self._latest_review_state(),
            "available_skills": self._available_skills_payload(),
            "suggested_next_actions": self._runtime_suggested_next_actions(),
            "backoff": self.get_backoff_state(),
        }

    def _issue_state_payload(self) -> Dict[str, Any]:
        active_issue = self.issue_ledger.active_issue()
        return {
            "active_issue_id": active_issue.issue_id if active_issue is not None else "",
            "active_issue": active_issue.summary() if active_issue is not None else None,
            "reopenable_issues": self.issue_ledger.reopenable_issues(),
            "total_fact_count": self.issue_ledger.total_fact_count(),
        }

    def ensure_issue_for_plan(self, *, original_request: str, plan_summary: str, reuse_issue_id: str = "") -> Dict[str, Any]:
        issue = self.issue_ledger.ensure_issue_open(
            request_summary=str(original_request or "").strip(),
            plan_summary=str(plan_summary or "").strip(),
            reuse_issue_id=str(reuse_issue_id or "").strip(),
        )
        self._persist_repo_facts()
        self._clear_facts()
        return issue.summary()

    def close_active_issue(self, *, note: str = "") -> Optional[Dict[str, Any]]:
        issue = self.issue_ledger.close_active_issue(note=note)
        self._persist_repo_facts()
        self._clear_facts()
        return issue.summary() if issue is not None else None

    def close_issue(self, issue_id: str, *, note: str = "") -> Dict[str, Any]:
        issue = self.issue_ledger.close_issue(str(issue_id or "").strip(), note=note)
        self._persist_repo_facts()
        self._clear_facts()
        return issue.summary()

    def reopen_issue(self, issue_id: str) -> Dict[str, Any]:
        issue = self.issue_ledger.reopen_issue(str(issue_id or "").strip())
        self._persist_repo_facts()
        self._clear_facts()
        self._reset_task_satisfaction()
        self._clear_edit_batch_state()
        return issue.summary()

    def _latest_diagnostics_state(self) -> Optional[Dict[str, Any]]:
        if self.active_error is not None and self.active_error.diagnostics:
            return {
                "path": self.active_error.path,
                "message": self.active_error.message,
                "diagnostic_engine": self.active_error.diagnostic_engine,
                "diagnostics": [dict(item) for item in self.active_error.diagnostics],
                "step": self.active_error.step,
                "source": "active_error",
            }

        for step in reversed(self.history):
            if not step.result.ok or step.result.name not in {"host_diagnostics", "diagnose"}:
                continue
            payload = step.result.payload if isinstance(step.result.payload, dict) else {}
            diagnostics = payload.get("diagnostics")
            if not isinstance(diagnostics, list) or not diagnostics:
                continue
            return {
                "path": str(payload.get("path", "") or step.action.get("path", "")),
                "message": str(payload.get("message", "") or payload.get("summary", "") or ""),
                "diagnostic_engine": str(payload.get("diagnostic_engine", "") or ""),
                "diagnostics": [dict(item) for item in diagnostics if isinstance(item, dict)],
                "step": int(step.step),
                "source": "history",
            }
        return None

    def _latest_review_state(self) -> Optional[Dict[str, Any]]:
        for step in reversed(self.history):
            if not step.result.ok:
                continue
            action_type = str(step.action.get("type", "") or "")
            if action_type not in {"show_diff", "git_diff", "review_changes"}:
                continue
            payload = step.result.payload if isinstance(step.result.payload, dict) else {}
            state: Dict[str, Any] = {
                "action_type": action_type,
                "summary": str(payload.get("summary", "") or ""),
                "step": int(step.step),
                "path": str(payload.get("path", "") or step.action.get("path", "") or ""),
            }
            if action_type in {"show_diff", "git_diff"}:
                state["diff"] = str(payload.get("diff", "") or "")
                state["stat"] = str(payload.get("stat", "") or "")
                files = payload.get("files")
                state["files"] = [str(item) for item in files if isinstance(item, str)] if isinstance(files, list) else []
                state["staged"] = bool(payload.get("staged", False))
            if action_type == "review_changes":
                files = payload.get("files")
                state["files"] = [dict(item) for item in files if isinstance(item, dict)] if isinstance(files, list) else []
                state["counts"] = dict(payload.get("counts", {})) if isinstance(payload.get("counts"), dict) else {}
                state["high_risk_paths"] = [str(item) for item in payload.get("high_risk_paths", []) if isinstance(item, str)] if isinstance(payload.get("high_risk_paths"), list) else []
                state["review_summary"] = dict(payload.get("review_summary", {})) if isinstance(payload.get("review_summary"), dict) else {}
            return state
        return None

    def _format_runtime_action_label(self, action: Dict[str, Any]) -> str:
        action_type = str(action.get("type", "") or "action")
        path = str(action.get("path", "") or "").strip()
        if action_type == "read_file":
            return f"Read {path}" if path else "Read File"
        if action_type == "git_diff":
            return f"Diff {path}" if path else "Git Diff"
        if action_type == "show_diff":
            return "Show Diff"
        if action_type == "review_changes":
            return "Review Changes"
        if action_type == "begin_edit_batch":
            return "Begin Edit Batch"
        if action_type == "end_edit_batch":
            return "End Edit Batch"
        if action_type == "finish":
            return "Finish"
        if action_type == "drop_context":
            return "Drop Context"
        if action_type == "find_files":
            return "Find Files"
        if action_type == "search_in_files":
            return "Search In Files"
        if action_type == "grep":
            return "Search"
        if action_type == "list_files":
            return "List Files"
        if action_type == "outline_file":
            return f"Outline {path}" if path else "Outline File"
        if action_type == "read_symbol":
            symbol_name = str(action.get("symbol_name", "") or "").strip()
            return f"Read Symbol {symbol_name}" if symbol_name else "Read Symbol"
        if action_type == "find_symbol_definitions":
            return "Find Symbol Definitions"
        if action_type == "find_symbol_references":
            return "Find Symbol References"
        if action_type == "trace_dependencies":
            return f"Trace Dependencies {path}" if path else "Trace Dependencies"
        if action_type == "find_related_files":
            return f"Find Related Files {path}" if path else "Find Related Files"
        if action_type == "find_related_tests":
            return "Find Related Tests"
        if action_type == "find_related_configs":
            return "Find Related Configs"
        if action_type == "find_canonical_implementation":
            return "Find Canonical Implementation"
        if action_type == "find_similar_code":
            return "Find Similar Code"
        if action_type == "find_entry_points":
            return "Find Entry Points"
        if action_type == "find_ownership":
            return "Find Ownership"
        if action_type == "recent_changes":
            return "Recent Changes"
        if action_type == "get_changed_files":
            return "Get Changed Files"
        if action_type == "semantic_search":
            return "Semantic Search"
        if action_type == "investigate":
            return "Investigate"
        if action_type == "inspect_files":
            return "Inspect Files"
        if action_type == "summarize_files":
            return "Summarize Files"
        if action_type == "symbol_search":
            return "Symbol Search"
        if action_type == "git_status":
            return "Git Status"
        if action_type == "diagnose":
            return f"Diagnose {path}" if path else "Diagnose"
        if action_type == "changed_files_check":
            return "Changed Files Check"
        if action_type == "project_problems":
            mode = str(action.get("mode", "") or "").strip()
            return f"Project Problems {mode}" if mode else "Project Problems"
        if action_type == "skill":
            name = str(action.get("name", "") or "").strip()
            return f"Skill {name}" if name else "List Skills"
        return action_type.replace("_", " ").title()

    def _decorate_runtime_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        decorated = dict(action)
        if not decorated.get("label"):
            decorated["label"] = self._format_runtime_action_label(decorated)
        if not decorated.get("style"):
            decorated["style"] = "ghost" if decorated.get("type") == "drop_context" else "secondary"
        if decorated.get("type") == "drop_context":
            decorated["requires_confirmation"] = True
        return decorated

    def _runtime_suggested_next_actions(self) -> List[Dict[str, Any]]:
        suggestions: List[Dict[str, Any]] = []
        if self.pending_patch_recovery:
            path = str(self.pending_patch_recovery.get("path", "") or "")
            if path:
                suggestions.append({"type": "read_file", "path": path})
                suggestions.append({"type": "git_diff", "path": path})
                suggestions.append({"type": "show_diff"})
                suggestions.append({"type": "review_changes", "path": path, "limit": 20})
            else:
                suggestions.append({"type": "show_diff"})
                suggestions.append({"type": "review_changes", "limit": 20})
            suggestions.append({"type": "drop_context", "reason": f"Reset patch recovery for {path}" if path else "Reset patch recovery"})
        elif self.active_error is not None and self.active_error.suggested_next_actions:
            suggestions.extend(self.active_error.suggested_next_actions)
        elif self.pending_verification:
            suggestions.extend(self._finish_validation_suggestions())
        elif self.edit_batch_mode and self.edit_batch_pending:
            suggestions.append({"type": "end_edit_batch"})
            first_path = sorted(self.edit_batch_pending.keys())[0]
            suggestions.append({"type": "git_diff", "path": first_path})
            suggestions.append({"type": "show_diff"})

        unique: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in suggestions:
            if not isinstance(item, dict):
                continue
            key = json.dumps(item, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            unique.append(self._decorate_runtime_action(item))
        return unique[:6]

    def _active_mode_strategy(self) -> Optional[Dict[str, Any]]:
        if self.pending_patch_recovery:
            path = str(self.pending_patch_recovery.get("path", "") or "")
            return {
                "mode": "patch_recovery",
                "steps": [
                    f"Read {path or 'the affected file'} to refresh exact file state.",
                    f"Inspect diff or review output for {path or 'the affected file'} before attempting another edit.",
                    "If the branch is still correct, make one corrected edit after recovery clears.",
                    "If the branch is wrong, drop context and restart from fresh reads.",
                ],
            }
        if self.pending_verification:
            path = str(self.pending_verification.get("path", "") or "")
            return {
                "mode": "pending_verification",
                "steps": [
                    f"Verify {path or 'the edited path'} with read_file or diff/review output.",
                    "Confirm the landed change matches the intended mutation.",
                    "Finish if complete, or make one final correction only after verification clears.",
                ],
            }
        if self.edit_batch_mode:
            pending_paths = sorted(self.edit_batch_pending.keys())
            return {
                "mode": "edit_batch",
                "steps": [
                    "Keep related writes grouped together; avoid unrelated exploration.",
                    f"End the batch once the related files are done: {', '.join(pending_paths[:4]) if pending_paths else 'pending files' }.",
                    "Use host verification results to decide whether to finish or make one small follow-up.",
                ],
            }
        if self.completion_check_pending:
            return {
                "mode": "completion_check",
                "steps": [
                    "Run one concrete validation action tied to the recent mutation.",
                    "If validation passes, finish immediately.",
                    "If validation contradicts completion, make one corrective edit and let normal execution resume.",
                ],
            }
        return None

    def _bridge_safe_action_types(self) -> set[str]:
        return {
            "begin_edit_batch",
            "drop_context",
            "diagnose",
            "end_edit_batch",
            "find_files",
            "find_canonical_implementation",
            "find_entry_points",
            "find_ownership",
            "find_related_configs",
            "find_related_files",
            "find_related_tests",
            "find_similar_code",
            "find_symbol_definitions",
            "find_symbol_references",
            "finish",
            "get_changed_files",
            "git_diff",
            "git_status",
            "grep",
            "inspect_files",
            "investigate",
            "list_files",
            "outline_file",
            "read_file",
            "read_symbol",
            "recent_changes",
            "review_changes",
            "search_in_files",
            "semantic_search",
            "skill",
            "show_diff",
            "summarize_files",
            "symbol_search",
            "trace_dependencies",
        }

    def execute_operator_action(self, action: Dict[str, Any], *, thought: str = "Operator action from extension UI.") -> ActionResult:
        if not isinstance(action, dict):
            return ActionResult(ok=False, name="operator_action", payload={"error": "Action must be an object."})

        action_type = str(action.get("type", "") or "").strip()
        if not action_type:
            return ActionResult(ok=False, name="operator_action", payload={"error": "Missing action.type"})
        if action_type not in self._bridge_safe_action_types():
            return ActionResult(
                ok=False,
                name=action_type,
                payload={"error": f"Unsupported operator action: {action_type}"},
            )

        if (
            self.completion_check_pending
            and action_type != "finish"
            and self._is_validation_action(action_type)
        ):
            self._clear_completion_check(keep_satisfaction=True)

        self._current_thought = thought.strip()
        started = time.time()
        result = self._execute_action(action)
        error_cleared = self._maybe_clear_active_error(action, result)
        if not result.ok:
            self._set_active_error(action, result)
        step = AgentStep(
            step=len(self.history) + 1,
            thought=self._current_thought,
            action=action,
            result=result,
            elapsed_s=time.time() - started,
            run_id=self._active_run_id,
        )
        self.history.append(step)
        if error_cleared and isinstance(step.result.payload, dict):
            step.result.payload["error_cleared"] = True

        if self._run_metrics:
            self._run_metrics["steps"] = int(self._run_metrics.get("steps", 0)) + 1
            if result.ok:
                self._run_metrics["successful_actions"] = int(self._run_metrics.get("successful_actions", 0)) + 1
            else:
                self._run_metrics["failed_actions"] = int(self._run_metrics.get("failed_actions", 0)) + 1

        task = self._current_task or "Extension operator action"
        self._ingest_memory(
            task=task,
            thought=self._current_thought,
            action=action,
            result=result,
        )
        self._print_step(step)
        self._run_automatic_diagnostics_after_step(step)
        self._run_fact_subagent(task, step)

        if action_type == "finish" and result.ok:
            final_message = str(result.payload.get("message", "Done."))
            self._last_run_result = self._build_run_result(final_message, True)

        entered_completion_check = self._maybe_enter_completion_check(task)
        if entered_completion_check:
            return result

        forced_finish_message = self._should_force_finish_after_satisfaction()
        if forced_finish_message:
            try:
                self._clear_active_context()
                self._clear_patch_recovery()
            except Exception:
                pass
            self._last_run_result = self._build_run_result(forced_finish_message, True)

        return result

    def _steering_block(self) -> str:
        if not self.steering_prompt.strip():
            return "None"
        return self.steering_prompt

    def _has_steering(self) -> bool:
        return bool(self.steering_prompt.strip())

    # -----------------------------
    # Memory / helper methods
    # -----------------------------

    def _safe_branch(self) -> Optional[str]:
        now = time.time()
        if self._safe_branch_cache is not None and (now - self._safe_branch_cache_ts) < 3.0:
            return self._safe_branch_cache
        try:
             payload = self._call_tool("git-branch")
             if payload.get("ok"):
                 data = payload.get("data", {})
                 branch = data.get("branch")
                 if isinstance(branch, str) and branch.strip():
                    self._safe_branch_cache = branch.strip()
                    self._safe_branch_cache_ts = now
                    return branch.strip()
        except Exception:
             pass
        return None

    def _infer_paths_from_task(self, task: str) -> List[str]:
        try:
            return uniq(PATH_RE.findall(task))
        except Exception:
            return []

    def _infer_tags_from_task(self, task: str) -> List[str]:
        t = task.lower()
        tags: List[str] = []

        if any(x in t for x in ["git", "commit", "branch", "diff", "restore", "stage"]):
            tags.extend(["git", "repo_state"])
        if any(x in t for x in ["test", "pytest", "validate", "run"]):
            tags.extend(["validation"])
        if any(x in t for x in ["error", "fail", "broken", "exception", "traceback"]):
            tags.extend(["failure"])
        if any(x in t for x in ["find", "search", "grep", "where"]):
            tags.extend(["search"])
        if any(x in t for x in ["edit", "change", "modify", "patch", "write", "update"]):
            tags.extend(["edit"])
        if any(x in t for x in ["read", "inspect", "understand", "explain"]):
            tags.extend(["inspect"])

        return uniq(tags)

    def _infer_entities_from_task(self, task: str) -> List[str]:
        tokens = tokenize(task)
        stop = {
            "the", "and", "for", "with", "from", "into", "that", "this", "then", "need",
            "make", "agent", "tool", "tools", "file", "files", "use", "using", "would",
            "should", "could", "just", "only", "have", "has", "into", "without", "main",
            "path", "root", "context", "memory", "manager"
        }
        return uniq([tok for tok in tokens if len(tok) >= 3 and tok not in stop])[:12]

    def _infer_error_type_from_history(self) -> Optional[str]:
        if self.active_error is not None and self.active_error.error_type:
            return self.active_error.error_type
        for step in reversed(self.history[-8:]):
            payload = step.result.payload
            stderr = str(payload.get("stderr", "") or "")
            message = str(payload.get("message", "") or "")
            blob = stderr + "\n" + message
            for error_name in [
                "SyntaxError",
                "NameError",
                "ImportError",
                "ModuleNotFoundError",
                "TypeError",
                "ValueError",
                "AssertionError",
                "FileNotFoundError",
                "PermissionError",
            ]:
                if error_name in blob:
                    return error_name
        return None

    def _extract_error_type(self, payload: Any) -> Optional[str]:
        if isinstance(payload, dict):
            direct_error = payload.get("error")
            if isinstance(direct_error, dict):
                code = direct_error.get("code")
                message = direct_error.get("message")
                for candidate in [code, message]:
                    if isinstance(candidate, str):
                        detected = self._extract_error_type(candidate)
                        if detected:
                            return detected
            nested = payload.get("data")
            if isinstance(nested, dict):
                detected = self._extract_error_type(nested)
                if detected:
                    return detected
            blob = "\n".join(
                str(payload.get(key, "") or "")
                for key in ["stderr", "message", "summary", "code", "status"]
            )
        else:
            blob = str(payload or "")

        for error_name in [
            "SyntaxError",
            "NameError",
            "ImportError",
            "ModuleNotFoundError",
            "TypeError",
            "ValueError",
            "AssertionError",
            "FileNotFoundError",
            "PermissionError",
            "PATCH_FAILED",
            "PATCH_LOOP_DETECTED",
        ]:
            if error_name in blob:
                return error_name
        return None

    def _current_memory_bundle(self, task: str, action_type: Optional[str] = None) -> Dict[str, Any]:
        query = MemoryQuery(
            task=task,
            action_type=action_type,
            paths=self._infer_paths_from_task(task),
            tags=self._infer_tags_from_task(task),
            entities=self._infer_entities_from_task(task),
            error_type=self._infer_error_type_from_history(),
            limit=self.config.memory_retrieval_limit,
            current_branch=self._safe_branch(),
            current_step=len(self.history) + 1,
        )
        try:
            return self.memory.lookup(query)
        except Exception:
            return {}

    def _ingest_memory(
        self,
        *,
        task: str,
        thought: str,
        action: Dict[str, Any],
        result: ActionResult,
    ) -> None:
        try:
            self.memory.ingest_step(
                task=task,
                thought=thought,
                action=action,
                result_ok=result.ok,
                result_payload=result.payload,
                step=len(self.history),
                branch=self._safe_branch(),
            )
        except Exception:
            return None

    # Active-context helpers -------------------------------------------------
    def _set_active_context_item(self, item: Dict[str, Any], label: str) -> None:
        try:
            normalized = {"label": label, "item": item}
        except Exception:
            normalized = {"label": label, "item": str(item)}

        items = self.active_context.setdefault("items", [])

        # Replace existing item with same label instead of duplicating.
        replaced = False
        for idx, existing in enumerate(items):
            if isinstance(existing, dict) and existing.get("label") == label:
                items[idx] = normalized
                replaced = True
                break

        if not replaced:
            items.append(normalized)

        # Bound active context size.
        self.active_context["items"] = items[-12:]

    def _remove_active_context_item(self, label: str) -> None:
        items = self.active_context.get("items", [])
        if not isinstance(items, list):
            return
        self.active_context["items"] = [
            item for item in items
            if not (isinstance(item, dict) and item.get("label") == label)
        ]

    def _clear_active_context(self) -> None:
        self.active_context = {
            "strategy": None,
            "items": [],
            "notes": [],
        }
        self.fulfillment_mode = False
        self._last_strategy = None

    def _drop_context(self) -> None:
        self._clear_active_context()
        self._clear_facts()

    def _start_new_run(self) -> None:
        self._run_sequence += 1
        self._active_run_id = self._run_sequence

    def _fact_records_by_run_scope(self) -> Tuple[List[FactRecord], List[FactRecord]]:
        return self.issue_ledger.records_by_run_scope(self._active_run_id)

    def _facts_block(self, records: Optional[List[FactRecord]] = None) -> str:
        try:
            facts = [record.to_dict() for record in (records if records is not None else list(self.fact_map.values()))]
            return json.dumps(facts, indent=2)
        except Exception:
            return "[]"

    def _fact_context_block(self) -> str:
        previous_run_records, current_run_records = self._fact_records_by_run_scope()
        payload = {
            "previous_turn_facts": [record.to_dict() for record in previous_run_records],
            "current_run_facts": [record.to_dict() for record in current_run_records],
        }
        try:
            return json.dumps(payload, indent=2)
        except Exception:
            return '{"previous_turn_facts": [], "current_run_facts": []}'

    def _set_recent_resolution_handoff(self, payload: Dict[str, Any], *, label: str, note: str) -> None:
        enriched = dict(payload)
        enriched["delivery_state"] = "pending_next_turn"
        self.recent_resolution_handoff = enriched
        try:
            self._set_active_context_item(enriched, label)
            self._add_active_note(note)
        except Exception:
            pass

    def _set_recent_fact_handoff(self, record: FactRecord, *, action_type: str) -> None:
        self._set_recent_resolution_handoff(
            {
                "type": action_type,
                "key": record.key,
                "value": record.value,
                "fact_type": record.fact_type,
                "issue_id": record.issue_id,
                "issue_status": record.issue_status,
                "source_action": action_type,
                "updated_step": record.updated_step,
                "updated_run_id": record.updated_run_id,
            },
            label=f"recent_resolution::{record.key}",
            note=f"Fresh fact ready for immediate next-step use: {record.key} = {self._fact_excerpt(record.value, 140)}",
        )

    def _recent_resolution_handoff_block(self) -> str:
        payload = self.recent_resolution_handoff
        if not isinstance(payload, dict):
            return "{}"
        try:
            return json.dumps(payload, indent=2)
        except Exception:
            return "{}"

    def _mark_recent_resolution_handoff_delivered(self) -> None:
        payload = self.recent_resolution_handoff
        if not isinstance(payload, dict):
            return
        key = str(payload.get("key", "") or "").strip()
        label = f"recent_resolution::{key}" if key else "recent_resolution::unknown"
        try:
            self._remove_active_context_item(label)
        except Exception:
            pass
        self.recent_resolution_handoff = None

    def _selected_goal_facts_block(self) -> str:
        selected_keys = list(self.selected_goal_fact_keys or [])
        if not selected_keys:
            return "{}"
        selected_records: List[Dict[str, Any]] = []
        missing_keys: List[str] = []
        available = {record.key: record for record in self.issue_ledger.selected_context_records(selected_keys)}
        for key in selected_keys:
            record = available.get(key)
            if record is None:
                missing_keys.append(key)
                continue
            selected_records.append(record.to_dict())
        payload = {
            "selected_keys": selected_keys,
            "facts": selected_records,
            "missing_keys": missing_keys,
        }
        try:
            return json.dumps(payload, indent=2)
        except Exception:
            return "{}"

    def _repo_facts_path(self) -> Path:
        return self.root / REPO_FACTS_FILENAME

    def _serialize_repo_facts_markdown(self, records: Optional[List[FactRecord]] = None) -> str:
        return self.issue_ledger.to_markdown()

    def _load_repo_facts_records(self) -> List[FactRecord]:
        path = self._repo_facts_path()
        self.issue_ledger = IssueFactLedger.load(path)
        self._repo_facts_loaded_count = self.issue_ledger.total_fact_count()
        return self.issue_ledger.active_context_records()

    def _load_repo_facts_into_map(self) -> None:
        records = self._load_repo_facts_records()
        self.fact_map = {record.key: record for record in records}

    def repo_facts_status_lines(self) -> List[str]:
        path = self._repo_facts_path()
        if self._repo_facts_loaded_count > 0:
            lines = [
                f"repo_facts : loaded {self._repo_facts_loaded_count}",
                f"facts_path  : {path}",
                f"schema      : v{self.issue_ledger.schema_version}",
            ]
            active_issue = self.issue_ledger.active_issue()
            if active_issue is not None:
                lines.append(f"active_issue: {active_issue.issue_id}")
            return lines
        if path.exists():
            return [
                "repo_facts : present but empty/unreadable",
                f"facts_path  : {path}",
            ]
        return [
            "repo_facts : none",
            f"facts_path  : {path}",
        ]

    def _persist_repo_facts(self) -> None:
        try:
            self._repo_facts_path().write_text(
                self._serialize_repo_facts_markdown(),
                encoding="utf-8",
            )
            self._repo_facts_loaded_count = self.issue_ledger.total_fact_count()
        except Exception:
            return

    def _set_fact_record(self, key: str, value: str, *, source_action: str, fact_type: str) -> FactRecord:
        normalized_key = key.strip()
        resolved_fact_type = str(fact_type or "").strip().lower()
        if resolved_fact_type not in {FACT_TYPE_GOAL, FACT_TYPE_ARCHITECTURE}:
            resolved_fact_type = FACT_TYPE_GOAL
        record = self.issue_ledger.upsert_fact(
            key=normalized_key,
            value=value.strip(),
            fact_type=resolved_fact_type,
            source_action=source_action,
            updated_step=len(self.history) + 1,
            updated_run_id=self._active_run_id,
            task_summary=self._current_task or "Ad hoc worker task",
        )
        self.fact_map = {item.key: item for item in self.issue_ledger.active_context_records()}
        self._persist_repo_facts()
        return record

    def _short_sha256(self, text: str, length: int = 12) -> str:
        try:
            return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]
        except Exception:
            return "unknown"

    def _fact_excerpt(self, text: str, limit: int = 72) -> str:
        compact = re.sub(r"\s+", " ", str(text or "").strip())
        if not compact:
            return ""
        return shorten(compact, limit)

    def _first_changed_line_from_diff(self, diff_text: str) -> Optional[int]:
        if not isinstance(diff_text, str) or not diff_text:
            return None
        match = re.search(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", diff_text, flags=re.MULTILINE)
        if not match:
            return None
        try:
            return int(match.group(1))
        except Exception:
            return None

    def _edit_attempt_fact_key(self, action_type: str, path: str, action: Dict[str, Any]) -> str:
        normalized_path = str(path or "").strip()
        if action_type == "patch_file":
            intent = json.dumps(
                {
                    "path": normalized_path,
                    "search": str(action.get("search", "") or ""),
                    "replace": str(action.get("replace", "") or ""),
                    "all": bool(action.get("all", False)),
                },
                sort_keys=True,
                ensure_ascii=True,
            )
            return f"patch_attempt::{normalized_path}::{self._short_sha256(intent, 16)}"

        if action_type == "write_file":
            content = str(action.get("content", "") or "")
            intent = json.dumps(
                {
                    "path": normalized_path,
                    "content_sha256": self._short_sha256(content, 16),
                },
                sort_keys=True,
                ensure_ascii=True,
            )
            return f"write_attempt::{normalized_path}::{self._short_sha256(intent, 16)}"

        if action_type in MUTATION_ACTION_TYPES:
            intent = json.dumps(action, sort_keys=True, ensure_ascii=True)
            return f"edit_attempt::{action_type}::{normalized_path}::{self._short_sha256(intent, 16)}"

        fallback = json.dumps(action, sort_keys=True, ensure_ascii=True)
        return f"edit_attempt::{action_type}::{normalized_path}::{self._short_sha256(fallback, 16)}"

    def _auto_edit_fact_value(
        self,
        *,
        action_type: str,
        path: str,
        action: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> str:
        data = payload.get("data")
        result_payload: Dict[str, Any] = data if isinstance(data, dict) else {}
        path_value = str(result_payload.get("path", path) or path)
        diff_text = str(result_payload.get("diff", "") or "")
        line_hint = self._first_changed_line_from_diff(diff_text)
        parts = [f"path={path_value}"]
        thought_excerpt = self._fact_excerpt(self._current_thought, 120)

        if thought_excerpt:
            parts.append(f"thought={thought_excerpt}")

        if action_type == "patch_file":
            search_text = str(action.get("search", "") or "")
            replace_text = str(action.get("replace", "") or "")
            if payload.get("ok"):
                status = str(result_payload.get("status", "patched") or "patched")
                parts.append(f"status={status}")
                replacements = int(result_payload.get("replacements", 0) or 0)
                parts.append(f"replacements={replacements}")
                sha256_value = str(result_payload.get("sha256", "") or "")
                if sha256_value:
                    parts.append(f"sha256={sha256_value[:12]}")
            else:
                error_blob = payload.get("error")
                error_code = "PATCH_FAILED"
                error_message = ""
                if isinstance(error_blob, dict):
                    error_code = str(error_blob.get("code", error_code) or error_code)
                    error_message = str(error_blob.get("message", "") or "")
                else:
                    error_message = str(error_blob or payload.get("message", "") or "")
                parts.append(f"status=failed:{error_code}")
                if error_message:
                    parts.append(f"error={self._fact_excerpt(error_message, 96)}")
            parts.append(f"search_sha={self._short_sha256(search_text)}")
            parts.append(f"replace_sha={self._short_sha256(replace_text)}")
            search_excerpt = self._fact_excerpt(search_text, 48)
            replace_excerpt = self._fact_excerpt(replace_text, 48)
            if search_excerpt:
                parts.append(f"search_excerpt={search_excerpt}")
            if replace_excerpt:
                parts.append(f"replace_excerpt={replace_excerpt}")
        elif action_type == "write_file":
            content = str(action.get("content", "") or "")
            if payload.get("ok"):
                parts.append("status=written")
                parts.append(f"created={str(bool(result_payload.get('created', False))).lower()}")
                bytes_written = int(result_payload.get("bytes_written", 0) or 0)
                parts.append(f"bytes={bytes_written}")
                sha256_value = str(result_payload.get("sha256", "") or "")
                if sha256_value:
                    parts.append(f"sha256={sha256_value[:12]}")
            else:
                error_blob = payload.get("error")
                error_code = "WRITE_FAILED"
                error_message = ""
                if isinstance(error_blob, dict):
                    error_code = str(error_blob.get("code", error_code) or error_code)
                    error_message = str(error_blob.get("message", "") or "")
                else:
                    error_message = str(error_blob or payload.get("message", "") or "")
                parts.append(f"status=failed:{error_code}")
                if error_message:
                    parts.append(f"error={self._fact_excerpt(error_message, 96)}")
            parts.append(f"content_sha={self._short_sha256(content)}")
            content_excerpt = self._fact_excerpt(content, 64)
            if content_excerpt:
                parts.append(f"content_excerpt={content_excerpt}")
        elif action_type in MUTATION_ACTION_TYPES:
            mutation = result_payload.get("mutation") if isinstance(result_payload.get("mutation"), dict) else result_payload
            mutation_payload = mutation if isinstance(mutation, dict) else {}
            if payload.get("ok"):
                status = "applied" if bool(mutation_payload.get("applied")) else str(mutation_payload.get("reason", "ok") or "ok")
                parts.append(f"status={status}")
                after_hash = str(mutation_payload.get("after_hash", result_payload.get("sha256", "")) or "")
                if after_hash:
                    parts.append(f"sha256={after_hash[:12]}")
                changed_line_count = int(mutation_payload.get("changed_line_count", 0) or 0)
                parts.append(f"changed_lines={changed_line_count}")
            else:
                error_blob = payload.get("error")
                error_code = "MUTATION_FAILED"
                error_message = ""
                if isinstance(error_blob, dict):
                    error_code = str(error_blob.get("code", error_code) or error_code)
                    error_message = str(error_blob.get("message", "") or "")
                else:
                    error_message = str(error_blob or payload.get("message", "") or "")
                parts.append(f"status=failed:{error_code}")
                if error_message:
                    parts.append(f"error={self._fact_excerpt(error_message, 96)}")
            intent_excerpt = self._fact_excerpt(json.dumps(action, sort_keys=True, ensure_ascii=True), 96)
            if intent_excerpt:
                parts.append(f"intent={intent_excerpt}")

        if line_hint is not None:
            parts.append(f"line={line_hint}")

        return " | ".join(parts)

    def _record_auto_edit_attempt_fact(
        self,
        *,
        action_type: str,
        path: str,
        action: Dict[str, Any],
        payload: Dict[str, Any],
    ) -> None:
        if action_type not in MUTATION_ACTION_TYPES:
            return
        normalized_path = str(path or "").strip()
        if not normalized_path:
            return
        try:
            key = self._edit_attempt_fact_key(action_type, normalized_path, action)
            value = self._auto_edit_fact_value(
                action_type=action_type,
                path=normalized_path,
                action=action,
                payload=payload,
            )
            self._set_fact_record(key, value, source_action=f"auto_{action_type}", fact_type=FACT_TYPE_GOAL)
            if not self.edit_batch_mode:
                self._prune_auto_edit_attempt_facts(action_type=action_type, path=normalized_path)
        except Exception:
            return

    def _auto_edit_attempt_fact_prefix(self, action_type: str, path: str) -> str:
        normalized_path = str(path or "").strip()
        if action_type == "patch_file":
            return f"patch_attempt::{normalized_path}::"
        if action_type == "write_file":
            return f"write_attempt::{normalized_path}::"
        return f"edit_attempt::{action_type}::{normalized_path}::"

    def _prune_auto_edit_attempt_facts(self, *, action_type: str, path: str) -> None:
        prefix = self._auto_edit_attempt_fact_prefix(action_type, path)
        if not prefix:
            return

        matching: List[FactRecord] = []
        for key, record in self.fact_map.items():
            if key.startswith(prefix):
                matching.append(record)

        if len(matching) <= AUTO_EDIT_ATTEMPT_FACTS_PER_PATH:
            return

        matching.sort(
            key=lambda record: (
                int(record.updated_run_id or 0),
                int(record.updated_step or 0),
            ),
            reverse=True,
        )
        keep_keys = {record.key for record in matching[:AUTO_EDIT_ATTEMPT_FACTS_PER_PATH]}
        removed_any = False
        for record in matching[AUTO_EDIT_ATTEMPT_FACTS_PER_PATH:]:
            if record.key in keep_keys:
                continue
            if record.key in self.fact_map:
                del self.fact_map[record.key]
                removed_any = True

        if removed_any:
            self._persist_repo_facts()

    def _batch_auto_edit_attempt_records(self, *, action_type: str, path: str) -> List[FactRecord]:
        prefix = self._auto_edit_attempt_fact_prefix(action_type, path)
        if not prefix:
            return []
        batch_start = int(self.edit_batch_started_step or 0)
        records: List[FactRecord] = []
        for key, record in self.fact_map.items():
            if not key.startswith(prefix):
                continue
            if int(record.updated_run_id or 0) != int(self._active_run_id or 0):
                continue
            if int(record.updated_step or 0) <= batch_start:
                continue
            records.append(record)
        return records

    def _prune_batch_auto_edit_attempt_facts_for_path(self, path: str, *, keep_mode: str = "success") -> None:
        normalized_path = str(path or "").strip()
        if not normalized_path:
            return
        all_batch_records: List[FactRecord] = []
        for action_type in sorted(MUTATION_ACTION_TYPES):
            all_batch_records.extend(
                self._batch_auto_edit_attempt_records(action_type=action_type, path=normalized_path)
            )

        keep_record: Optional[FactRecord] = None
        if keep_mode == "success":
            success_candidates = [record for record in all_batch_records if "status=failed:" not in str(record.value or "")]
            if success_candidates:
                success_candidates.sort(
                    key=lambda record: (
                        int(record.updated_run_id or 0),
                        int(record.updated_step or 0),
                    ),
                    reverse=True,
                )
                keep_record = success_candidates[0]
        elif keep_mode == "latest" and all_batch_records:
            all_batch_records.sort(
                key=lambda record: (
                    int(record.updated_run_id or 0),
                    int(record.updated_step or 0),
                ),
                reverse=True,
            )
            keep_record = all_batch_records[0]

        removed_any = False
        for record in all_batch_records:
            if keep_record is not None and record.key == keep_record.key:
                continue
            if record.key in self.fact_map:
                del self.fact_map[record.key]
                removed_any = True

        for action_type in sorted(MUTATION_ACTION_TYPES):
            self._prune_auto_edit_attempt_facts(action_type=action_type, path=normalized_path)
        if removed_any:
            self._persist_repo_facts()

    def _record_edit_batch_summary_fact(
        self,
        *,
        status: str,
        verified_paths: List[str],
        failed_paths: List[str],
        results: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if not verified_paths and not failed_paths:
            return
        start_step = int(self.edit_batch_started_step or 0)
        run_id = int(self._active_run_id or 0)
        thought_excerpt = self._fact_excerpt(self.edit_batch_started_thought or self._current_thought, 160)
        normalized_verified = sorted(uniq([str(path).strip() for path in verified_paths if str(path).strip()]))
        normalized_failed = sorted(uniq([str(path).strip() for path in failed_paths if str(path).strip()]))
        parts = [f"status={status}"]
        if thought_excerpt:
            parts.append(f"thought={thought_excerpt}")
        if normalized_verified:
            parts.append(f"verified_paths={', '.join(normalized_verified)}")
        if normalized_failed:
            parts.append(f"failed_paths={', '.join(normalized_failed)}")
        parts.append(f"verified_count={len(normalized_verified)}")
        parts.append(f"failed_count={len(normalized_failed)}")
        if status == "failed" and results:
            for item in results:
                if not isinstance(item, dict) or bool(item.get("ok")):
                    continue
                raw_payload = item.get("payload")
                payload: Dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
                message = self._fact_excerpt(str(payload.get("message", "") or ""), 140)
                if message:
                    parts.append(f"failure_message={message}")
                break
        key = f"edit_batch::{run_id}::{start_step or len(self.history)}"
        self._set_fact_record(key, " | ".join(parts), source_action="auto_edit_batch", fact_type=FACT_TYPE_GOAL)

    def _latest_successful_edit_step_for_path(self, path: str) -> Optional[AgentStep]:
        normalized_path = str(path or "").strip()
        if not normalized_path:
            return None
        for step in reversed(self._current_run_steps()):
            if not step.result.ok:
                continue
            action_type = str(step.action.get("type", "") or "")
            if action_type not in MUTATION_ACTION_TYPES:
                continue
            action_path = str(step.action.get("path", "") or "").strip()
            if action_path == normalized_path:
                return step
        return None

    def _diagnostic_fact_suffix(
        self,
        *,
        engine: str,
        status: str,
        diagnostics: Optional[List[Dict[str, Any]]] = None,
    ) -> str:
        parts = [f"diagnostic_engine={engine}", f"diagnostic_status={status}"]
        if diagnostics:
            parts.append(f"diagnostic_count={len(diagnostics)}")
            first = diagnostics[0]
            code = str(first.get("code", "") or "").strip()
            line = first.get("line")
            column = first.get("column")
            message = self._fact_excerpt(str(first.get("message", "") or ""), 120)
            if code:
                parts.append(f"diagnostic_code={code}")
            if isinstance(line, int):
                parts.append(f"diagnostic_line={line}")
            if isinstance(column, int):
                parts.append(f"diagnostic_column={column}")
            if message:
                parts.append(f"diagnostic_message={message}")
        return " | ".join(parts)

    def _update_auto_edit_attempt_fact_with_diagnostics(
        self,
        *,
        mutation_step: AgentStep,
        engine: str,
        status: str,
        diagnostics: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        action_type = str(mutation_step.action.get("type", "") or "")
        path = str(mutation_step.action.get("path", "") or "").strip()
        if action_type not in MUTATION_ACTION_TYPES or not path:
            return
        payload = mutation_step.result.payload if isinstance(mutation_step.result.payload, dict) else {}
        try:
            key = self._edit_attempt_fact_key(action_type, path, mutation_step.action)
            value = self._auto_edit_fact_value(
                action_type=action_type,
                path=path,
                action=mutation_step.action,
                payload=payload,
            )
            suffix = self._diagnostic_fact_suffix(
                engine=engine,
                status=status,
                diagnostics=diagnostics,
            )
            if suffix:
                value = f"{value} | {suffix}"
            self._set_fact_record(key, value, source_action=f"auto_{action_type}_diagnostics", fact_type=FACT_TYPE_GOAL)
            if not self.edit_batch_mode:
                self._prune_auto_edit_attempt_facts(action_type=action_type, path=path)
        except Exception:
            return

    def _assess_fact_quality(self, key: str, value: str) -> Dict[str, Any]:
        normalized_key = str(key or "").strip().lower()
        normalized_value = str(value or "").strip()
        lowered_value = normalized_value.lower()
        issues: List[str] = []

        if len(normalized_value) > 360:
            issues.append("value is too long for a durable fact")

        if re.search(r"\blines?\s+\d+(?:\s*-\s*\d+)?\b", lowered_value):
            issues.append("contains line-specific details that will go stale")

        if re.search(r"\b\d+\.\s+\S", normalized_value):
            issues.append("contains a step-by-step plan instead of a durable fact")

        tactical_tokens = [
            "implementation_plan",
            "relevant_files",
            "primary target",
            "why next",
            "delegation",
            "success signals",
            "next steps",
            "risk",
            "risks",
            "currently",
            "to be replaced",
            "for this task",
            "this request",
            "this run",
        ]
        matched_tactical_tokens = [token for token in tactical_tokens if token in lowered_value]
        if matched_tactical_tokens:
            issues.append(
                "contains task-local planning or temporary implementation details: "
                + ", ".join(matched_tactical_tokens[:4])
            )

        if any(token in normalized_key for token in ["summary", "plan", "discovery", "notes"]):
            issues.append("fact key suggests a summary blob instead of a stable repo fact")

        if normalized_value.startswith(("{", "[")) and normalized_value.count(":") >= 4:
            issues.append("packs too many fields into one fact; split or compress it")

        ok = not issues
        guidance = [
            "Record only durable repo knowledge that should remain useful across later unrelated tasks.",
            "Prefer one or two concise sentences about a stable capability, owner, constraint, interface, or reusable pattern.",
            "Do not store line numbers, temporary plans, risks, or broad run summaries in repo facts.",
        ]
        return {
            "ok": ok,
            "issues": issues,
            "guidance": guidance,
            "suggested_shape": {
                "key": normalized_key or "stable_fact_key",
                "value": "Concise durable repo fact with no line numbers or task-local plan details.",
            },
        }

    def _validate_fact_quality(self, action_type: str, key: str, value: str, fact_type: str) -> Optional[ActionResult]:
        resolved_fact_type = str(fact_type or "").strip().lower()
        # Goal facts are issue-local tactical findings — skip the durable-quality gate.
        if resolved_fact_type == FACT_TYPE_GOAL:
            return None
        assessment = self._assess_fact_quality(key, value)
        issues = list(assessment.get("issues", [])) if isinstance(assessment, dict) else []
        lowered_value = str(value or "").strip().lower()
        lowered_key = str(key or "").strip().lower()
        if resolved_fact_type == FACT_TYPE_ARCHITECTURE:
            if lowered_key.startswith(("patch_attempt::", "write_attempt::", "edit_attempt::", "edit_batch::")):
                issues.append("architecture facts cannot use edit-attempt keys; store them as goal facts")
            tactical_markers = [
                "verification",
                "pending",
                "search_excerpt=",
                "replace_excerpt=",
                "content_sha=",
                "status=failed",
            ]
            matched_markers = [token for token in tactical_markers if token in lowered_value]
            if matched_markers:
                issues.append(
                    "architecture facts cannot store tactical execution traces: " + ", ".join(matched_markers[:4])
                )
        if not issues:
            return None
        guidance = list(assessment.get("guidance", [])) if isinstance(assessment, dict) else []
        guidance.append("Use fact_type=goal for issue-local execution findings and fact_type=architecture for cross-issue repo memory.")
        suggested_shape = dict(assessment.get("suggested_shape", {})) if isinstance(assessment, dict) else {}
        suggested_shape["fact_type"] = resolved_fact_type
        return self._error_action_result(
            action_type,
            {
                "error": "Fact failed durable-memory validation.",
                "issues": issues,
                "guidance": guidance,
                "suggested_shape": suggested_shape,
            },
        )

    def _clear_facts(self) -> None:
        self.fact_map = {}
        self._load_repo_facts_into_map()
        self._active_run_id = 0

    def _render_fact_ledger_table(self) -> str:
        previous_run_records, current_run_records = self._fact_records_by_run_scope()
        lines: List[str] = []

        def append_section(title: str, records: List[FactRecord]) -> None:
            lines.append(title + ":")
            if not records:
                lines.append("  (none)")
                return

            for record in records:
                lines.extend(
                    [
                        f"  {record.key}",
                        f"    value: {record.value}",
                        f"    source: {record.source_action or '-'}",
                        f"    step: {record.updated_step}",
                    ]
                )
                if record.updated_run_id:
                    lines.append(f"    run: {record.updated_run_id}")
                lines.append("")

        append_section("previous turn facts", previous_run_records)
        append_section("current run facts", current_run_records)
        while lines and not lines[-1].strip():
            lines.pop()
        return _render_text_panel("Fact Ledger", lines)

    def _render_current_run_facts_panel(self) -> str:
        _, current_run_records = self._fact_records_by_run_scope()
        lines: List[str] = []
        if not current_run_records:
            lines.append("No durable facts were collected during this goal.")
            return _render_text_panel("Facts Collected", lines)

        for record in current_run_records:
            lines.extend(
                [
                    f"{record.key}",
                    f"  value: {record.value}",
                    f"  source: {record.source_action or '-'}",
                    f"  step: {record.updated_step}",
                ]
            )
            lines.append("")

        while lines and not lines[-1].strip():
            lines.pop()
        return _render_text_panel("Facts Collected", lines)

    def _current_run_steps(self) -> List[AgentStep]:
        return [step for step in self.history if step.run_id == self._active_run_id]

    def _collect_touched_paths_for_steps(self, steps: List[AgentStep]) -> List[str]:
        paths: List[str] = []
        for step in steps:
            action_path = step.action.get("path")
            if isinstance(action_path, str) and action_path:
                paths.append(action_path)
            payload = step.result.payload
            if isinstance(payload, dict):
                payload_path = payload.get("path")
                if isinstance(payload_path, str) and payload_path:
                    paths.append(payload_path)
        return uniq(paths)

    def _validation_result_for_run(self, steps: List[AgentStep]) -> WorkerValidationResult:
        successful_mutations = [
            step for step in steps
            if step.result.ok and self._is_mutating_action(str(step.action.get("type", "") or ""))
        ]
        if not successful_mutations:
            return WorkerValidationResult(kind="none", passed=True, summary="No mutating actions required validation.")

        latest_mutation = successful_mutations[-1]
        latest_mutation_index = steps.index(latest_mutation)
        validation_candidates = steps[latest_mutation_index + 1:]
        for step in validation_candidates:
            if not step.result.ok:
                continue
            action_type = str(step.action.get("type", "") or "")
            if action_type == "read_file" and self._validation_confirms_mutation(latest_mutation, step):
                return WorkerValidationResult(
                    kind="read_file",
                    passed=True,
                    summary=f"Confirmed the latest mutation in {latest_mutation.action.get('path', '')} via read_file.",
                )
            if action_type in {"git_diff", "show_diff", "review_changes"}:
                return WorkerValidationResult(
                    kind=action_type,
                    passed=True,
                    summary=f"Reviewed structural changes after the latest mutation using {action_type}.",
                )
            if action_type == "run_shell":
                payload = step.result.payload if isinstance(step.result.payload, dict) else {}
                returncode = payload.get("returncode")
                if isinstance(returncode, int) and returncode == 0:
                    return WorkerValidationResult(
                        kind="run_shell",
                        passed=True,
                        summary="A post-mutation shell validation completed successfully.",
                    )

        return WorkerValidationResult(
            kind="missing",
            passed=False,
            summary="Mutating actions completed without a structural validation step afterward.",
        )

    def _build_run_result(self, final_message: str, ok: bool) -> WorkerRunResult:
        steps = self._current_run_steps()
        touched_paths = self._collect_touched_paths_for_steps(steps)
        validation = self._validation_result_for_run(steps)
        has_mutations = any(
            step.result.ok and self._is_mutating_action(str(step.action.get("type", "") or ""))
            for step in steps
        )
        last_action_type = str(steps[-1].action.get("type", "") or "") if steps else ""
        task_satisfied = self.task_satisfied or (
            ok and last_action_type == "finish" and (validation.passed or not has_mutations)
        )
        validation_passed = validation.passed or not has_mutations
        return WorkerRunResult(
            ok=ok and task_satisfied and validation_passed,
            final_message=final_message,
            task_satisfied=task_satisfied,
            validation_ran=validation.kind not in {"none", "missing"},
            validation_passed=validation_passed,
            touched_paths=touched_paths,
            validation=validation,
        )

    def _render_pending_fact_resolution_table(self) -> str:
        if not self.pending_fact_resolution:
            return ""
        paths = [str(path) for path in self.pending_fact_resolution.get("paths") or [] if str(path)]
        lines = [
            f"source_action: {self.pending_fact_resolution.get('source_action', '-')}",
            f"exploration_count: {self.pending_fact_resolution.get('exploration_count', '-')}",
            f"paths: {', '.join(paths) if paths else '(none)'}",
            f"reason: {str(self.pending_fact_resolution.get('reason', '') or '').replace(chr(10), ' ')}",
            "next: consider set_fact | update_fact",
        ]
        return _render_text_panel("Fact Suggestion", lines)

    def _render_pending_verification_table(self) -> str:
        if not self.pending_verification:
            return ""
        lines = [
            f"path: {self.pending_verification.get('path', '-')}",
            f"mode: {self.pending_verification.get('mode', '-')}",
            "next: read_file on the same path",
        ]
        expected_sha256 = str(self.pending_verification.get("expected_sha256", "") or "")
        if expected_sha256:
            lines.append(f"expect_sha256: {expected_sha256}")
        replace_text = str(self.pending_verification.get("replace", "") or "")
        if replace_text:
            lines.append(f"expect: {replace_text.replace(chr(10), ' ')}")
        return _render_text_panel("Pending Verification", lines)

    def _maybe_reset_context_for_strategy(self, task: str) -> None:
        # Heuristic: if a strategy token is present in the task and differs
        # from the last seen, reset the active context working set.
        try:
            strategy = None
            if isinstance(task, str):
                m = re.search(r"strategy:\s*([a-zA-Z0-9_\-]+)", task)
                if m:
                    strategy = m.group(1)
        except Exception:
            strategy = None
        if strategy is not None and strategy != self._last_strategy:
            try:
                self._clear_active_context()
            except Exception:
                pass
        try:
            if strategy is not None:
                self.active_context["strategy"] = strategy
        except Exception:
            pass
        self._last_strategy = strategy

    def _active_context_block(self) -> str:
        try:
            items = self.active_context.get("items", []) or []
            notes = self.active_context.get("notes", []) or []
            out = {
                "strategy": self.active_context.get("strategy"),
                "items": items,
                "notes": notes,
            }
            return json.dumps(out, indent=2)
        except Exception:
            return ""

    def _add_active_note(self, note: str) -> None:
        notes = self.active_context.setdefault("notes", [])
        notes.append(note)
        self.active_context["notes"] = notes[-12:]

    def _normalize_step_list(self, action: Dict[str, Any]) -> List[int]:
        if "steps" in action and isinstance(action["steps"], list):
            out: List[int] = []
            for value in action["steps"]:
                try:
                    out.append(int(value))
                except Exception:
                    continue
            return out
        if "step" in action:
            try:
                return [int(action["step"])]
            except Exception:
                return []
        return []

    def _normalize_memory_id_list(self, action: Dict[str, Any]) -> List[str]:
        if "ids" in action and isinstance(action["ids"], list):
            out: List[str] = []
            for value in action["ids"]:
                if isinstance(value, str) and value.strip():
                    out.append(value.strip())
            return out
        mem_id = action.get("id") or action.get("memory_id")
        if isinstance(mem_id, str) and mem_id.strip():
            return [mem_id.strip()]
        return []

    def _recent_patch_failures(self, path: str, window: int = 6) -> int:
        count = 0
        for step in reversed(self.history[-window:]):
            if step.action.get("type") != "patch_file":
                continue
            if step.action.get("path") != path:
                continue
            if step.result.ok:
                continue

            payload = step.result.payload
            if not isinstance(payload, dict):
                if "PATCH_FAILED" in str(payload):
                    count += 1
                continue

            if payload.get("code") == "PATCH_FAILED":
                count += 1
            elif payload.get("error") == "PATCH_FAILED":
                count += 1
            elif isinstance(payload.get("error"), dict) and payload["error"].get("code") == "PATCH_FAILED":
                count += 1
            elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("error"), dict) and payload["data"]["error"].get("code") == "PATCH_FAILED":
                count += 1
            elif "PATCH_FAILED" in str(payload):
                count += 1
        return count

    def _is_idempotent_patch_result(self, step: AgentStep) -> bool:
        if step.action.get("type") != "patch_file":
            return False
        if not step.result.ok:
            return False
        payload = step.result.payload
        if not isinstance(payload, dict):
            return False
        if payload.get("status") == "already_applied":
            return True
        data = payload.get("data")
        return isinstance(data, dict) and data.get("status") == "already_applied"

    def _same_patch_attempts(self, action: Dict[str, Any], window: int = 6) -> int:
        count = 0
        path = action.get("path")
        search = action.get("search")
        replace = action.get("replace")
        use_all = bool(action.get("all", False))

        for step in reversed(self.history[-window:]):
            a = step.action
            if a.get("type") != "patch_file":
                continue
            if self._is_idempotent_patch_result(step):
                continue
            if (
                a.get("path") == path
                and a.get("search") == search
                and a.get("replace") == replace
                and bool(a.get("all", False)) == use_all
            ):
                count += 1
        return count

    def _disable_fulfillment_for_recovery(self, note: str) -> None:
        self.fulfillment_mode = False
        try:
            self._add_active_note(note)
        except Exception:
            pass

    def _find_recent_plan_for_path(self, path: str, window: int = 8) -> str:
        for step in reversed(self.history[-window:]):
            action_path = step.action.get("path")
            result_path = None
            try:
                if isinstance(step.result.payload, dict):
                    result_path = step.result.payload.get("path")
            except Exception:
                result_path = None
            if action_path == path or result_path == path:
                if step.thought:
                    return str(step.thought)
        return ""

    def _set_patch_recovery(
        self,
        *,
        task: str,
        path: str,
        failed_action: Dict[str, Any],
        result_payload: Dict[str, Any],
    ) -> None:
        self.pending_patch_recovery = {
            "task": task,
            "path": path,
            "failed_patch_intent": {
                "search": failed_action.get("search"),
                "replace": failed_action.get("replace"),
                "all": bool(failed_action.get("all", False)),
            },
            "recent_plan": self._find_recent_plan_for_path(path),
            "error": result_payload,
            "next_required_action": {"type": "read_file", "path": path},
        }
        try:
            self._set_active_context_item(self.pending_patch_recovery, f"patch_recovery::{path}")
        except Exception:
            pass

    def _clear_patch_recovery(self) -> None:
        if self.pending_patch_recovery is not None:
            path = str(self.pending_patch_recovery.get("path", "") or "")
            if path:
                self._remove_active_context_item(f"patch_recovery::{path}")
        self.pending_patch_recovery = None

    def _set_edit_batch_context_item(self) -> None:
        if not self.edit_batch_mode:
            return
        pending_paths = sorted(self.edit_batch_pending.keys())
        payload = {
            "edit_batch_mode": True,
            "task": self._current_task,
            "pending_paths": pending_paths,
            "pending_count": len(pending_paths),
            "next_required_action": {"type": "end_edit_batch"} if pending_paths else None,
        }
        self._set_active_context_item(payload, "edit_batch_mode")

    def _enter_edit_batch_mode(self) -> None:
        self.edit_batch_mode = True
        self.edit_batch_pending = {}
        self.edit_batch_started_step = len(self.history)
        self.edit_batch_started_thought = str(self._current_thought or "").strip()
        try:
            self._set_edit_batch_context_item()
            self._add_active_note(
                "Edit batch mode is active. Perform related write_file/patch_file actions, then use end_edit_batch to run host verification reads across the touched files."
            )
        except Exception:
            pass

    def _clear_edit_batch_state(self) -> None:
        if self.edit_batch_mode or self.edit_batch_pending:
            try:
                self._remove_active_context_item("edit_batch_mode")
            except Exception:
                pass
        self.edit_batch_mode = False
        self.edit_batch_pending = {}
        self.edit_batch_started_step = 0
        self.edit_batch_started_thought = ""

    def _queue_edit_batch_verification(
        self,
        *,
        path: str,
        search: str,
        replace: str,
        mode: str,
        expected_sha256: Optional[str],
    ) -> None:
        self.edit_batch_pending[path] = {
            "task": self._current_task,
            "path": path,
            "search": search,
            "replace": replace,
            "mode": mode,
            "expected_sha256": expected_sha256,
        }
        try:
            self._set_edit_batch_context_item()
        except Exception:
            pass

    def _render_edit_batch_table(self) -> str:
        if not self.edit_batch_mode:
            return ""
        pending_paths = sorted(self.edit_batch_pending.keys())
        lines = [
            f"active: {str(self.edit_batch_mode).lower()}",
            f"pending_paths: {', '.join(pending_paths) if pending_paths else '(none)'}",
            f"pending_count: {len(pending_paths)}",
            "next: end_edit_batch to run host verification reads",
        ]
        return _render_text_panel("Edit Batch Mode", lines)

    def _set_pending_verification(
        self,
        *,
        path: str,
        search: str,
        replace: str,
        mode: str = "contains",
        expected_sha256: Optional[str] = None,
    ) -> None:
        self.pending_verification = {
            "task": self._current_task,
            "path": path,
            "search": search,
            "replace": replace,
            "mode": mode,
            "expected_sha256": expected_sha256,
        }
        try:
            self._set_active_context_item(
                {
                    "pending_verification": True,
                    "task": self._current_task,
                    "path": path,
                    "search": search,
                    "replace": replace,
                    "mode": mode,
                    "expected_sha256": expected_sha256,
                    "next_required_action": {
                        "type": "read_file",
                        "path": path,
                    },
                },
                f"pending_verification::{path}",
            )
        except Exception:
            pass

    def _clear_pending_verification(self) -> None:
        if self.pending_verification is not None:
            path = str(self.pending_verification.get("path", "") or "")
            if path:
                self._remove_active_context_item(f"pending_verification::{path}")
        self.pending_verification = None

    def _set_pending_fact_resolution(
        self,
        *,
        source_action: str,
        paths: List[str],
        reason: str,
        exploration_count: int,
    ) -> None:
        normalized_paths = [path for path in uniq(paths) if isinstance(path, str) and path]
        label_suffix = ",".join(normalized_paths) if normalized_paths else source_action
        self.pending_fact_resolution = {
            "task": self._current_task,
            "source_action": source_action,
            "paths": normalized_paths,
            "reason": reason.strip(),
            "exploration_count": exploration_count,
            "suggested_actions": [
                {"type": "set_fact"},
                {"type": "update_fact"},

            ],
        }
        try:
            self._set_active_context_item(
                {
                    "pending_fact_resolution": True,
                    **self.pending_fact_resolution,
                },
                f"pending_fact_resolution::{label_suffix}",
            )
        except Exception:
            pass

    def _clear_pending_fact_resolution(self) -> None:
        if self.pending_fact_resolution is not None:
            paths = self.pending_fact_resolution.get("paths") or []
            label_suffix = ",".join(str(path) for path in paths if str(path)) or str(self.pending_fact_resolution.get("source_action", ""))
            if label_suffix:
                self._remove_active_context_item(f"pending_fact_resolution::{label_suffix}")
        self.pending_fact_resolution = None

    def _set_active_error(self, action: Dict[str, Any], result: ActionResult) -> None:
        payload = result.payload if isinstance(result.payload, dict) else {}
        path = str(action.get("path", "") or payload.get("path", "") or "")
        error_type = self._extract_error_type(payload)
        message = ""
        if isinstance(payload.get("error"), dict):
            message = str(payload["error"].get("message", "") or "")
        if not message:
            message = str(payload.get("message", "") or payload.get("summary", "") or result.name or "Action failed")

        self._clear_active_error()
        self.active_error = ActiveErrorState(
            task=self._current_task,
            action_type=str(action.get("type", "") or result.name or "unknown"),
            error_type=error_type,
            message=message.strip(),
            path=path,
            step=len(self.history) + 1,
            diagnostic_engine=str(payload.get("diagnostic_engine", "") or "") or None,
            diagnostics=[dict(item) for item in payload.get("diagnostics", []) if isinstance(item, dict)],
            suggested_next_actions=[dict(item) for item in payload.get("suggested_next_actions", []) if isinstance(item, dict)],
        )
        try:
            self._set_active_context_item(
                {
                    "active_error": True,
                    "task": self.active_error.task,
                    "action_type": self.active_error.action_type,
                    "error_type": self.active_error.error_type,
                    "message": self.active_error.message,
                    "path": self.active_error.path,
                    "step": self.active_error.step,
                    "diagnostic_engine": self.active_error.diagnostic_engine,
                    "diagnostics": self.active_error.diagnostics,
                    "suggested_next_actions": self.active_error.suggested_next_actions,
                },
                self.active_error.label(),
            )
        except Exception:
            pass

    def _clear_active_error(self) -> None:
        if self.active_error is not None:
            try:
                self._remove_active_context_item(self.active_error.label())
            except Exception:
                pass
        self.active_error = None

    def _maybe_clear_active_error(self, action: Dict[str, Any], result: ActionResult) -> bool:
        if self.active_error is None or not result.ok:
            return False

        if self.active_error.task and self.active_error.task != self._current_task:
            self._clear_active_error()
            return True

        action_type = str(action.get("type", "") or result.name or "")
        result_payload = result.payload if isinstance(result.payload, dict) else {}
        path = str(action.get("path", "") or result_payload.get("path", "") or "")
        same_path = bool(self.active_error.path) and bool(path) and self.active_error.path == path
        same_action_type = self.active_error.action_type == action_type
        clears = same_path or same_action_type

        if (
            not clears
            and self.active_error.action_type == "run_shell"
            and action_type in {"run_shell", "read_file", "show_diff", "git_diff"}
        ):
            clears = action_type == "run_shell"

        if not clears:
            return False

        resolved_path = self.active_error.path or path or action_type
        self._clear_active_error()
        try:
            self._add_active_note(f"Resolved prior error context for {resolved_path} after a successful {action_type}.")
        except Exception:
            pass
        return True

    def _verification_blocks_patch(self, action: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        If a patch already succeeded on a file for the current task and has not
        yet been verified, do not allow more patch attempts on that same file.
        """
        if not self.pending_verification:
            return None

        if action.get("type") not in MUTATION_ACTION_TYPES:
            return None

        pending_task = str(self.pending_verification.get("task", ""))
        if pending_task and pending_task != self._current_task:
            return None

        path = str(action.get("path", ""))
        pending_path = str(self.pending_verification.get("path", ""))
        if path != pending_path:
            return None

        return {
            "status": "verification_required",
            "message": (
                f"A previous write on {path} already succeeded and is awaiting verification. "
                "Do not write this file again yet. Verify with read_file first."
            ),
            "suggested_next_actions": [
                {"type": "begin_edit_batch"},
                {"type": "read_file", "path": path},
                {"type": "git_diff", "path": path},
                {"type": "show_diff"},
                {"type": "review_changes", "limit": 20},
            ],
        }

    def _reset_task_satisfaction(self) -> None:
        self.task_satisfied = False
        self.satisfaction_reason = ""
        self.post_satisfaction_checks = 0
        self.completion_check_pending = False
        self.completion_check_reason = ""
        self._clear_pending_verification()
        self._clear_pending_fact_resolution()
        self._clear_active_error()

    def _mark_task_satisfied(self, reason: str) -> None:
        self.task_satisfied = True
        self.satisfaction_reason = reason

    def _enter_completion_check(self, reason: str) -> None:
        self._mark_task_satisfied(reason)
        self.completion_check_pending = True
        self.completion_check_reason = reason
        try:
            self._add_active_note(
                "A successful milestone was reached. Before finishing, decide whether any user-requested subtask remains."
            )
        except Exception:
            pass

    def _clear_completion_check(self, keep_satisfaction: bool = False) -> None:
        self.completion_check_pending = False
        self.completion_check_reason = ""
        if not keep_satisfaction:
            self.task_satisfied = False
            self.satisfaction_reason = ""
            self.post_satisfaction_checks = 0

    def _recent_non_finish_steps(self, window: int = 6) -> List[AgentStep]:
        return [step for step in self.history[-window:] if step.action.get("type") != "finish"]


    def _recent_failures(self, window: int = 4) -> int:
        count = 0
        for step in self.history[-window:]:
            if not step.result.ok:
                count += 1
        return count

    def _recent_exploration_actions(self, window: int = 6) -> List[AgentStep]:
        return [
            step for step in self.history[-window:]
            if str(step.action.get("type", "")) in EXPLORATION_ACTION_TYPES and step.result.ok
        ]

    def _recent_successful_actions(self, window: int = 4) -> List[AgentStep]:
        return [step for step in self.history[-window:] if step.result.ok]

    def _recent_focus_path(self, window: int = 10) -> Optional[str]:
        for step in reversed(self.history[-window:]):
            action_path = step.action.get("path")
            if isinstance(action_path, str) and action_path and step.action.get("type") in ({"read_file"} | MUTATION_ACTION_TYPES):
                return action_path
            payload = step.result.payload
            if isinstance(payload, dict):
                payload_path = payload.get("path")
                if isinstance(payload_path, str) and payload_path and step.action.get("type") in ({"read_file"} | MUTATION_ACTION_TYPES):
                    return payload_path
        return None

    def _recent_list_files_count(self, window: int = 8) -> int:
        return sum(1 for step in self.history[-window:] if step.result.ok and step.action.get("type") == "list_files")

    def _same_list_files_path_count(self, path: str, window: int = 8) -> int:
        normalized = path or "."
        count = 0
        for step in self.history[-window:]:
            if step.action.get("type") != "list_files" or not step.result.ok:
                continue
            step_path = step.action.get("path")
            if not isinstance(step_path, str) or not step_path.strip():
                step_path = "."
            if step_path == normalized:
                count += 1
        return count

    def _exploration_loop_payload(self, path: str) -> Dict[str, Any]:
        focus_path = self._recent_focus_path()
        suggestions: List[Dict[str, Any]] = []
        if focus_path:
            suggestions.append({"type": "read_file", "path": focus_path})
        suggestions.extend(
            [
                {"type": "find_files", "glob": "**/*.css", "limit": 50},
                {"type": "grep", "pattern": "(--[A-Za-z0-9-]+|var\\()", "glob": "**/*.css", "limit": 50},
                {"type": "drop_context", "reason": "Repeated broad exploration"},
            ]
        )
        return {
            "code": "EXPLORATION_LOOP_DETECTED",
            "message": (
                f"Repeated list_files exploration detected for path '{path or '.'}'. "
                "Use a focused read_file, grep, or find_files with a concrete glob instead of listing again."
            ),
            "suggested_next_actions": suggestions,
        }

    def _is_mutating_action(self, action_type: str) -> bool:
        return action_type in MUTATION_ACTION_TYPES | {"batch_mutate", "git_add", "git_restore", "git_commit"}

    def _is_validation_action(self, action_type: str) -> bool:
        return action_type in {"run_shell", "show_diff", "git_diff", "read_file", "diagnose", "changed_files_check", "project_problems"}

    def _collect_recent_touched_paths(self, window: int = 4) -> List[str]:
        paths: List[str] = []
        for step in self.history[-window:]:
            action_path = step.action.get("path")
            if isinstance(action_path, str) and action_path:
                paths.append(action_path)
            payload = step.result.payload
            if isinstance(payload, dict):
                payload_path = payload.get("path")
                if isinstance(payload_path, str) and payload_path:
                    paths.append(payload_path)
        return uniq(paths)

    def _recent_successful_mutations_for_path(self, path: str, window: int = 12) -> List[AgentStep]:
        mutations: List[AgentStep] = []
        current_steps = self._current_run_steps()
        steps = current_steps[-window:] if window > 0 else current_steps
        for step in steps:
            if not step.result.ok:
                continue
            action_type = str(step.action.get("type", "") or "")
            if action_type not in MUTATION_ACTION_TYPES:
                continue
            action_path = str(step.action.get("path", "") or "")
            if action_path == path:
                if action_type == "patch_file" and self._is_idempotent_patch_result(step):
                    continue
                mutations.append(step)
        return mutations

    def _recent_structural_review_for_path(self, path: str, window: int = 8) -> bool:
        current_steps = self._current_run_steps()
        for step in reversed(current_steps[-window:]):
            if not step.result.ok:
                continue
            action_type = str(step.action.get("type", "") or "")
            if action_type in {"git_diff", "show_diff", "review_changes"}:
                step_path = str(step.action.get("path", "") or "")
                if not step_path or step_path == path:
                    return True
        return False

    def _structural_review_after_step_for_path(self, path: str, after_step: int) -> bool:
        for step in self._current_run_steps():
            if step.step <= after_step or not step.result.ok:
                continue
            action_type = str(step.action.get("type", "") or "")
            if action_type not in {"git_diff", "show_diff", "review_changes"}:
                continue
            step_path = str(step.action.get("path", "") or "")
            if not step_path or step_path == path:
                return True
        return False

    def _latest_structural_review_step_for_path(self, path: str) -> int:
        latest_step = 0
        for step in self._current_run_steps():
            if not step.result.ok:
                continue
            action_type = str(step.action.get("type", "") or "")
            if action_type not in {"git_diff", "show_diff", "review_changes"}:
                continue
            step_path = str(step.action.get("path", "") or "")
            if not step_path or step_path == path:
                latest_step = max(latest_step, int(step.step))
        return latest_step

    def _successful_mutations_since_review(self, path: str) -> List[AgentStep]:
        latest_review_step = self._latest_structural_review_step_for_path(path)
        return [step for step in self._recent_successful_mutations_for_path(path) if step.step > latest_review_step]

    def _mutation_thrash_result(self, action_type: str, path: str) -> ActionResult:
        return self._error_action_result(
            action_type,
            {
                "code": "REPEATED_MUTATION_REVIEW_REQUIRED",
                "message": (
                    f"Repeated successful writes detected for {path}. Do not keep patching from memory. "
                    "Inspect a structural diff after the latest edit, then either finish or make one final consolidated patch."
                ),
                "suggested_next_actions": [
                    {"type": "git_diff", "path": path},
                    {"type": "show_diff"},
                    {"type": "review_changes", "limit": 20},
                    {"type": "finish", "message": f"Changes for {path} appear complete."},
                ],
            },
        )

    def _mutation_consolidation_result(self, action_type: str, path: str) -> ActionResult:
        return self._error_action_result(
            action_type,
            {
                "code": "FILE_EDIT_CONSOLIDATION_REQUIRED",
                "message": (
                    f"Too many sequential successful edits were made to {path} in this task. "
                    "Stop incremental patch accretion. Reconcile with diff/review output and then finish or make one final consolidated patch only after resetting strategy."
                ),
                "suggested_next_actions": [
                    {"type": "git_diff", "path": path},
                    {"type": "show_diff"},
                    {"type": "review_changes", "limit": 20},
                    {"type": "finish", "message": f"Changes for {path} appear complete."},
                    {"type": "drop_context", "reason": f"Reconcile repeated edits on {path}"},
                ],
            },
        )

    def _repeated_mutation_guard_result(self, action_type: str, path: str) -> Optional[ActionResult]:
        if self.edit_batch_mode:
            return None

        mutations = self._successful_mutations_since_review(path)
        if len(mutations) < 2:
            return None

        latest_mutation = mutations[-1]
        if len(mutations) >= 3:
            return self._mutation_consolidation_result(action_type, path)

        if self._structural_review_after_step_for_path(path, latest_mutation.step):
            return None

        last_success = mutations[-1]
        recent = self._current_run_steps()[-4:]
        if not recent:
            return None
        if any(step is last_success for step in recent):
            return self._mutation_thrash_result(action_type, path)
        return None

    def _validation_confirms_mutation(self, mutation_step: AgentStep, validation_step: AgentStep) -> bool:
        mutation_type = str(mutation_step.action.get("type", ""))
        validation_type = str(validation_step.action.get("type", ""))

        if validation_type not in {"read_file", "run_shell", "show_diff", "git_diff", "diagnose", "changed_files_check", "project_problems"}:
            return False

        if mutation_type != "patch_file":
            return True

        patch_path = mutation_step.action.get("path")
        replace_text = mutation_step.action.get("replace")
        if not isinstance(patch_path, str) or not patch_path:
            return False
        if not isinstance(replace_text, str) or not replace_text:
            return False

        if validation_type in {"diagnose", "changed_files_check", "project_problems"}:
            return bool(validation_step.result.ok)

        if validation_type != "read_file":
            return False

        validation_path = validation_step.action.get("path")
        if validation_path != patch_path:
            return False

        payload = validation_step.result.payload
        if not isinstance(payload, dict):
            return False
        content = payload.get("content")
        if not isinstance(content, str):
            return False

        return replace_text in content

    def _maybe_enter_completion_check(self, task: str) -> bool:
        """
        Conservative host-side completion checkpoint.

        Instead of finishing immediately, enter a one-turn completion check when:
        - no recent failures
        - a mutating step succeeded recently
        - a validation/inspection step succeeded after it
        - we are in fulfillment mode or readiness is high
        """
        if len(self.history) < 2:
            return False

        if self._recent_failures(window=3) > 0:
            return False

        readiness = self._fulfillment_readiness(task)
        if not (self.fulfillment_mode or readiness.get("ready")):
            return False

        if self.pending_verification is not None:
            return False

        if self.edit_batch_mode or self.edit_batch_pending:
            return False

        if self.completion_check_pending:
            return False

        recent = self._recent_successful_actions(window=4)
        if len(recent) < 2:
            return False

        seen_mutation = False
        seen_validation_after_mutation = False
        latest_mutation: Optional[AgentStep] = None

        for step in recent:
            action_type = str(step.action.get("type", ""))
            if self._is_mutating_action(action_type):
                seen_mutation = True
                latest_mutation = step
                continue
            if (
                seen_mutation
                and latest_mutation is not None
                and self._is_validation_action(action_type)
                and self._validation_confirms_mutation(latest_mutation, step)
            ):
                seen_validation_after_mutation = True

        if not (seen_mutation and seen_validation_after_mutation):
            return False

        touched = self._collect_recent_touched_paths(window=4)
        if not touched:
            return False

        reason = (
            "Successful change followed by successful validation with no recent failures. "
            f"Touched paths: {', '.join(touched[:6])}"
        )
        self._enter_completion_check(reason)
        return True

    def _should_force_finish_after_satisfaction(self) -> Optional[str]:
        """
        Once satisfaction is reached, allow at most one extra non-failing check.
        Then force termination to avoid verification spirals.
        """
        if self.completion_check_pending:
            return None

        if not self.task_satisfied:
            return None

        if self._recent_failures(window=2) > 0:
            return None

        recent = self._recent_non_finish_steps(window=2)
        if not recent:
            return None

        last = recent[-1]
        action_type = str(last.action.get("type", ""))

        if self._is_validation_action(action_type):
            self.post_satisfaction_checks += 1

        if self.post_satisfaction_checks >= 1:
            return f"Auto-finished after task satisfaction: {self.satisfaction_reason}"

        return None

    def _post_satisfaction_block_payload(self, action: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        action_type = str(action.get("type", "") or "")
        if self._recent_failures(window=2) > 0:
            return None

        if self.completion_check_pending:
            if self._is_mutating_action(action_type):
                self._clear_completion_check(keep_satisfaction=False)
                try:
                    path = str(action.get("path", "") or action_type or "the active task")
                    self._add_active_note(
                        f"Completion check cleared because a new concrete mutation on {path} indicates work remains."
                    )
                except Exception:
                    pass
                return None
            if action_type in {"finish"} or self._is_validation_action(action_type):
                return None
            return {
                "code": "COMPLETION_CHECK_ACTIVE",
                "message": (
                    "A completion check is active. Only one concrete validation step or finish is allowed now."
                ),
                "suggested_next_actions": self._finish_validation_suggestions(),
            }

        if not self.task_satisfied:
            return None

        if action_type == "finish":
            return None

        if self.post_satisfaction_checks >= 1:
            return {
                "code": "POST_SATISFACTION_FINISH_REQUIRED",
                "message": (
                    "Task satisfaction is already established and the allowed follow-up validation is complete. "
                    "Finish instead of continuing to explore or mutate."
                ),
                "suggested_next_actions": [
                    {"type": "finish", "message": f"Done. {self.satisfaction_reason}".strip()},
                ],
            }

        if self._is_validation_action(action_type):
            return None

        return {
            "code": "POST_SATISFACTION_VALIDATION_ONLY",
            "message": (
                "Task satisfaction is already established. The only allowed next step is one concrete validation action "
                "or finish."
            ),
            "suggested_next_actions": self._finish_validation_suggestions(),
        }

    def _finish_validation_suggestions(self) -> List[Dict[str, Any]]:
        suggestions: List[Dict[str, Any]] = []
        if self.pending_verification:
            pending_path = str(self.pending_verification.get("path", "") or "")
            if pending_path:
                suggestions.append({"type": "read_file", "path": pending_path})
                suggestions.append({"type": "git_diff", "path": pending_path})

        touched_paths = self._collect_recent_touched_paths(window=6)
        if touched_paths:
            first_path = touched_paths[0]
            if not any(item.get("type") == "read_file" and item.get("path") == first_path for item in suggestions):
                suggestions.append({"type": "read_file", "path": first_path})
            if not any(item.get("type") == "git_diff" and item.get("path") == first_path for item in suggestions):
                suggestions.append({"type": "git_diff", "path": first_path})

        suggestions.append({"type": "show_diff"})
        suggestions.append({"type": "review_changes", "limit": 20})
        suggestions.append({"type": "finish", "message": f"Done. {self.satisfaction_reason}".strip()})

        unique: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in suggestions:
            key = json.dumps(item, sort_keys=True)
            if key in seen:
                continue
            seen.add(key)
            unique.append(item)
        return unique[:5]

    def _finish_block_payload(self) -> Optional[Dict[str, Any]]:
        if self.edit_batch_mode and self.edit_batch_pending:
            pending_paths = sorted(self.edit_batch_pending.keys())
            return {
                "code": "FINISH_BLOCKED_EDIT_BATCH",
                "message": (
                    "Cannot finish while edit batch mode still has unverified files. "
                    "End the batch so the host can verify all touched files first."
                ),
                "pending_paths": pending_paths,
                "suggested_next_actions": [
                    {"type": "end_edit_batch"},
                    {"type": "git_diff", "path": pending_paths[0]} if pending_paths else {"type": "show_diff"},
                    {"type": "show_diff"},
                ],
            }
        if self.pending_verification:
            pending_path = str(self.pending_verification.get("path", "") or "")
            return {
                "code": "FINISH_BLOCKED_PENDING_VERIFICATION",
                "message": (
                    f"Cannot finish while verification is still pending for {pending_path or 'the latest mutation'}."
                ),
                "suggested_next_actions": self._finish_validation_suggestions(),
            }

        steps = self._current_run_steps()
        validation = self._validation_result_for_run(steps)
        has_mutations = any(
            step.result.ok and self._is_mutating_action(str(step.action.get("type", "") or ""))
            for step in steps
        )
        if has_mutations and not validation.passed:
            return {
                "code": "FINISH_BLOCKED_VALIDATION_REQUIRED",
                "message": "Cannot finish after mutating repository state without a concrete successful validation step.",
                "validation": validation.to_dict(),
                "suggested_next_actions": self._finish_validation_suggestions(),
            }

        return None

    def _active_context_items(self) -> List[Dict[str, Any]]:
        items = self.active_context.get("items", [])
        return items if isinstance(items, list) else []

    def _fulfillment_readiness(self, task: str) -> Dict[str, Any]:
        """
        Heuristic gate for switching from exploration to fulfillment.

        We want the model to stop browsing once it has enough evidence:
        - a plausible target file/component
        - a styling/render clue
        - a likely fix location or class/token
        """
        task_l = task.lower()
        items = self._active_context_items()

        paths: List[str] = []
        text_blobs: List[str] = [task_l]

        for wrapper in items:
            if not isinstance(wrapper, dict):
                continue
            data = wrapper.get("item")
            if isinstance(data, dict):
                p = data.get("path")
                if isinstance(p, str) and p:
                    paths.append(p)
                try:
                    text_blobs.append(json.dumps(data, ensure_ascii=False).lower())
                except Exception:
                    text_blobs.append(str(data).lower())
            else:
                text_blobs.append(str(data).lower())

        blob = "\n".join(text_blobs)

        has_target_file = len(set(paths)) >= 1
        has_multiple_related_files = len(set(paths)) >= 2
        has_style_signal = any(
            token in blob
            for token in [
                "classname",
                "text-[var(",
                "--muted-foreground",
                "dark mode",
                "color",
                "foreground",
                "recent activity",
                "todoitem",
                "todolist",
            ]
        )
        has_fix_hint = any(
            token in blob
            for token in [
                "text-[var(--muted-foreground)]",
                "--muted-foreground",
                "unchecked",
                "checked",
                "classname",
                "jsx",
                "tsx",
            ]
        )
        ui_styling_task = any(
            token in task_l
            for token in [
                "color", "dark mode", "theme", "style", "css", "class", "tailwind", "recent activity"
            ]
        )

        score = 0
        score += 2 if has_target_file else 0
        score += 1 if has_multiple_related_files else 0
        score += 2 if has_style_signal else 0
        score += 2 if has_fix_hint else 0
        score += 1 if ui_styling_task else 0

        ready = score >= 5

        return {
            "ready": ready,
            "score": score,
            "signals": {
                "has_target_file": has_target_file,
                "has_multiple_related_files": has_multiple_related_files,
                "has_style_signal": has_style_signal,
                "has_fix_hint": has_fix_hint,
                "ui_styling_task": ui_styling_task,
            },
            "recommended_mode": "fulfillment" if ready else "exploration",
            "recommended_actions": (
                (["patch_file", "write_file", "show_diff", "finish"] if not self._shell_access_enabled() else ["patch_file", "write_file", "run_shell", "show_diff", "finish"])
                if ready
                else ["read_file", "grep", "find_files", "history_expand", "memory_expand"]
            ),
            "known_paths": list(dict.fromkeys(paths))[:8],
        }

    def _should_discourage_exploration(self, task: str) -> bool:
        readiness = self._fulfillment_readiness(task)
        return bool(readiness.get("ready"))

    def _reset_run_observability(self, task: str) -> None:
        self._run_started_at = time.time()
        self._observability_buffer = []
        self._run_metrics = {
            "task": task,
            "provider": self.config.provider,
            "model": self.config.model,
            "root": str(self.root),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._run_started_at)),
            "steps": 0,
            "tool_calls": 0,
            "parallel_batches": 0,
            "parallel_tasks_executed": 0,
            "time_saved_estimate_s": 0.0,
            "model_calls": 0,
            "successful_actions": 0,
            "failed_actions": 0,
            "llm_usage": {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "reasoning_tokens": 0,
                "prompt_token_count": 0,
                "candidates_token_count": 0,
                "total_token_count": 0,
                "thoughts_token_count": 0,
            },
            "model_turns": [],
        }
        self._write_observability_snapshot(final_message="Run in progress.", finished=False)

    def _append_observability_block(self, block: str) -> None:
        self._observability_buffer.append(block)
        self._write_observability_snapshot(final_message="Run in progress.", finished=False)

    def _record_model_turn_metrics(self, *, step_num: int, duration_s: float) -> None:
        metrics = self.model.get_last_metrics() if hasattr(self.model, "get_last_metrics") else {}
        usage = metrics.get("usage") if isinstance(metrics, dict) else {}
        if not isinstance(usage, dict):
            usage = {}

        if self._run_metrics:
            self._run_metrics["model_calls"] = int(self._run_metrics.get("model_calls", 0)) + 1
            turns = self._run_metrics.setdefault("model_turns", [])
            if isinstance(turns, list):
                turns.append(
                    {
                        "step": step_num,
                        "duration_s": round(duration_s, 3),
                        "provider": metrics.get("provider") if isinstance(metrics, dict) else None,
                        "model": metrics.get("model") if isinstance(metrics, dict) else None,
                        "usage": usage,
                    }
                )
            totals = self._run_metrics.setdefault("llm_usage", {})
            if isinstance(totals, dict):
                for key, value in usage.items():
                    if isinstance(value, int):
                        totals[key] = int(totals.get(key, 0)) + value

    def _compacted_observability_blocks(self) -> List[str]:
        blocks = list(self._observability_buffer)
        if len(blocks) <= OBSERVABILITY_TRACE_BLOCK_LIMIT:
            return blocks
        omitted = len(blocks) - OBSERVABILITY_TRACE_BLOCK_LIMIT
        summary = (
            f"> Auto-compacted observability trace: omitted {omitted} earlier block(s); "
            f"showing the most recent {OBSERVABILITY_TRACE_BLOCK_LIMIT}.\n\n"
        )
        return [summary, *blocks[-OBSERVABILITY_TRACE_BLOCK_LIMIT:]]

    def _render_observability_report(self, final_message: str, *, finished: bool = True) -> str:
        finished_at = time.time()
        metrics = dict(self._run_metrics) if isinstance(self._run_metrics, dict) else {}
        if finished:
            metrics["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(finished_at))
        metrics["duration_s"] = round(max(0.0, finished_at - self._run_started_at), 3)
        metrics["final_message"] = final_message
        usage_snapshot = self.get_last_token_usage_snapshot()
        if usage_snapshot is not None:
            metrics["usage_estimate"] = usage_snapshot.to_dict()

        return "".join(
            [
                "# Memory Observability\n\n",
                "## Run Metrics\n```json\n",
                json.dumps(metrics, indent=2, ensure_ascii=False),
                "\n```\n\n",
                "## Trace\n\n",
                *self._compacted_observability_blocks(),
            ]
        )

    def _observability_targets(self) -> List[Path]:
        return [Path(__file__).parent.joinpath("memory_observability.md")]

    def _write_observability_snapshot(self, *, final_message: str, finished: bool) -> None:
        if not self._run_metrics:
            return

        report = self._render_observability_report(final_message, finished=finished)
        targets = self._observability_targets()

        seen: set[str] = set()
        for target in targets:
            target_str = str(target)
            if target_str in seen:
                continue
            seen.add(target_str)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(report, encoding="utf-8")
            except Exception:
                pass

    def _flush_observability(self, final_message: str) -> None:
        self._write_observability_snapshot(final_message=final_message, finished=True)

    def _action_lock_mode(self, action_type: str) -> str:
        if action_type in MUTATION_ACTION_TYPES | {"batch_mutate", "git_add", "git_restore", "git_commit"}:
            return "write"
        if action_type == "run_shell":
            return "write"
        return "read"

    def _action_lock_path(self, action_type: str, action: Optional[Dict[str, Any]] = None) -> str:
        if not isinstance(action, dict):
            return "."
        if action_type == "rename_file":
            new_path = action.get("new_path")
            old_path = action.get("old_path")
            if isinstance(new_path, str) and new_path.strip():
                return new_path.strip()
            if isinstance(old_path, str) and old_path.strip():
                return old_path.strip()
        if action_type == "copy_file":
            destination_path = action.get("destination_path")
            if isinstance(destination_path, str) and destination_path.strip():
                return destination_path.strip()
        direct_path = action.get("path")
        if isinstance(direct_path, str) and direct_path.strip():
            return direct_path.strip()
        if action_type == "inspect_files":
            files = action.get("files")
            if isinstance(files, list):
                paths = [str(item.get("path", "") or "").strip() for item in files if isinstance(item, dict)]
                paths = [path for path in paths if path]
                if len(paths) == 1:
                    return paths[0]
        return "."

    def _record_parallel_batch_metrics(self, records: List[ParallelToolResult], elapsed_s: float) -> None:
        if not self._run_metrics or not records:
            return
        self._run_metrics["parallel_batches"] = int(self._run_metrics.get("parallel_batches", 0)) + 1
        self._run_metrics["parallel_tasks_executed"] = int(self._run_metrics.get("parallel_tasks_executed", 0)) + len(records)
        sequential_estimate = sum(max(0.0, item.duration_s) for item in records)
        saved = max(0.0, sequential_estimate - elapsed_s)
        self._run_metrics["time_saved_estimate_s"] = round(
            float(self._run_metrics.get("time_saved_estimate_s", 0.0)) + saved,
            3,
        )
        self._run_metrics["tool_calls"] = int(self._run_metrics.get("tool_calls", 0)) + len(records)

    def _append_parallel_observability(self, title: str, records: List[ParallelToolResult], elapsed_s: float) -> None:
        lines = [f"## {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())} - {title}\n\n"]
        lines.append(f"elapsed_s={elapsed_s:.3f}\n\n")
        for record in records:
            lines.append(f"### {record.job.label} [{record.job.action_type}]\n")
            lines.append("```json\n")
            try:
                lines.append(
                    json.dumps(
                        {
                            "subcommand": record.job.subcommand,
                            "args": list(record.job.args),
                            "lock_mode": record.job.lock_mode,
                            "lock_path": record.job.lock_path,
                            "duration_s": round(record.duration_s, 3),
                            "result": record.result,
                        },
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            except Exception:
                lines.append(str(record.result))
            lines.append("\n```\n\n")
        lines.append("---\n\n")
        self._append_observability_block("".join(lines))

    def _run_parallel_tool_batch(self, jobs: List[ParallelToolJob], *, title: str) -> List[ParallelToolResult]:
        if not jobs:
            return []
        started = time.time()
        records = self.parallel_tools.run_batch(jobs)
        elapsed_s = time.time() - started
        self._record_parallel_batch_metrics(records, elapsed_s)
        self._append_parallel_observability(title, records, elapsed_s)
        return records

    def _record_host_step(
        self,
        *,
        thought: str,
        action: Dict[str, Any],
        result: ActionResult,
        elapsed_s: float,
    ) -> AgentStep:
        step = AgentStep(
            step=len(self.history) + 1,
            thought=thought,
            action=action,
            result=result,
            elapsed_s=elapsed_s,
            run_id=self._active_run_id,
        )
        self.history.append(step)
        if self._run_metrics:
            self._run_metrics["steps"] = int(self._run_metrics.get("steps", 0)) + 1
            if result.ok:
                self._run_metrics["successful_actions"] = int(self._run_metrics.get("successful_actions", 0)) + 1
            else:
                self._run_metrics["failed_actions"] = int(self._run_metrics.get("failed_actions", 0)) + 1
        self._ingest_memory(task=self._current_task, thought=thought, action=action, result=result)
        self._print_step(step)
        return step

    def _discovery_prefetch_jobs(self, task: str) -> List[ParallelToolJob]:
        if self.discovery_budget is None:
            return []
        remaining = max(0, self.discovery_budget.remaining_tool_calls - 1)
        if remaining <= 0:
            return []
        limit = min(self.config.max_parallel_workers, remaining, 4)
        entity = next((item for item in self._infer_entities_from_task(task) if len(item) >= 3), "")
        jobs: List[ParallelToolJob] = [
            ParallelToolJob(label="repo_meta", subcommand="meta", action_type="meta", lock_mode="read", lock_path="."),
            ParallelToolJob(label="git_status", subcommand="git-status", args=("--limit", "50"), action_type="git_status", lock_mode="read", lock_path="."),
            ParallelToolJob(label="topology_scan", subcommand="ls", args=("--path", ".", "--limit", "120", "--recursive", "--max-depth", "3"), action_type="list_files", lock_mode="read", lock_path="."),
        ]
        if entity:
            jobs.append(
                ParallelToolJob(
                    label="symbol_probe",
                    subcommand="symbols",
                    args=("--path", ".", "--query", entity, "--limit", "20"),
                    action_type="symbol_search",
                    lock_mode="read",
                    lock_path=".",
                )
            )
        return jobs[:limit]

    def _parallel_discovery_prefetch_payload(self, task: str) -> Optional[Dict[str, Any]]:
        jobs = self._discovery_prefetch_jobs(task)
        if not jobs:
            return None
        records = self._run_parallel_tool_batch(jobs, title="parallel_discovery_prefetch")
        if self.discovery_budget is not None:
            self.discovery_budget.tool_calls_used += len(records)

        result_entries: List[Dict[str, Any]] = []
        for record in records:
            tool_result = record.result
            data = tool_result.get("data")
            result_entries.append(
                {
                    "label": record.job.label,
                    "action_type": record.job.action_type,
                    "ok": bool(tool_result.get("ok")),
                    "duration_s": round(record.duration_s, 3),
                    "summary": self._normalize_tool_outcome(
                        action_type=record.job.action_type,
                        tool_result=tool_result,
                        success_summary=f"{record.job.label} completed",
                        failure_summary=f"{record.job.label} failed",
                    ).summary,
                    "data_excerpt": self._fact_subagent_result_excerpt(data if data is not None else tool_result),
                }
            )

        payload = {
            "parallel_discovery_prefetch": True,
            "budget_used": len(records),
            "results": result_entries,
        }
        self._set_active_context_item(payload, f"parallel_discovery_prefetch::{self._active_run_id}")
        try:
            self._add_active_note(
                f"Host prefetched {len(records)} discovery probes in parallel before the first model turn."
            )
        except Exception:
            pass

        # Broadcast a host step so the UI shows the prefetch in the timeline.
        elapsed_s = sum(max(0.0, r.duration_s) for r in records)
        labels = [r.job.label for r in records]
        self._record_host_step(
            thought=f"Parallel prefetch: {', '.join(labels)}",
            action={"type": "inspect_files", "path": ".", "prefetch": True},
            result=ActionResult(
                ok=all(bool(r.result.get("ok")) for r in records),
                name="parallel_discovery_prefetch",
                payload={"summary": f"Prefetched {len(records)} probes", "count": len(records)},
            ),
            elapsed_s=elapsed_s,
        )

        return payload

    def _parallel_validation_jobs(self) -> List[ParallelToolJob]:
        touched_paths = self._collect_recent_touched_paths(window=6)
        jobs: List[ParallelToolJob] = []
        if touched_paths:
            primary = touched_paths[0]
            jobs.append(
                ParallelToolJob(
                    label="git_diff_validation",
                    subcommand="git-diff",
                    args=("--path", primary),
                    action_type="git_diff",
                    lock_mode="read",
                    lock_path=primary,
                )
            )
            jobs.append(
                ParallelToolJob(
                    label="review_changes_validation",
                    subcommand="review",
                    args=("--path", primary, "--limit", "20"),
                    action_type="review_changes",
                    lock_mode="read",
                    lock_path=primary,
                )
            )
        else:
            jobs.append(
                ParallelToolJob(
                    label="git_diff_validation",
                    subcommand="git-diff",
                    action_type="git_diff",
                    lock_mode="read",
                    lock_path=".",
                )
            )
            jobs.append(
                ParallelToolJob(
                    label="review_changes_validation",
                    subcommand="review",
                    args=("--limit", "20"),
                    action_type="review_changes",
                    lock_mode="read",
                    lock_path=".",
                )
            )
        return jobs[: max(1, min(self.config.max_parallel_workers, 2))]

    def _run_parallel_post_write_validation(self) -> bool:
        jobs = self._parallel_validation_jobs()
        if not jobs:
            return False
        records = self._run_parallel_tool_batch(jobs, title="parallel_post_write_validation")
        any_success = False
        for record in records:
            outcome = self._normalize_tool_outcome(
                action_type=record.job.action_type,
                tool_result=record.result,
                success_summary=f"Automatic validation via {record.job.action_type} completed",
                failure_summary=f"Automatic validation via {record.job.action_type} failed",
                next_hint="finish" if bool(record.result.get("ok")) else None,
            )
            action_result = self._action_result_from_outcome(outcome)
            host_action = {
                "type": record.job.action_type,
                "agent": "host_validation",
            }
            if record.job.lock_path and record.job.lock_path != ".":
                host_action["path"] = record.job.lock_path
            self._record_host_step(
                thought=f"Host ran automatic post-write validation using {record.job.action_type} before finish.",
                action=host_action,
                result=action_result,
                elapsed_s=record.duration_s,
            )
            any_success = any_success or action_result.ok
        return any_success

    def _normalize_repo_relative_path(self, raw_path: str) -> str:
        value = str(raw_path or "").strip()
        if not value:
            return ""
        candidate = Path(value)
        if candidate.is_absolute():
            try:
                return str(candidate.resolve().relative_to(self.root)).replace(os.sep, "/")
            except Exception:
                return str(candidate).replace(os.sep, "/")
        return value.replace(os.sep, "/")

    def _repo_command_path(self, name: str) -> Optional[str]:
        local = self.root / "node_modules" / ".bin" / name
        if local.exists() and local.is_file():
            return str(local)
        return shutil.which(name)

    def _typescript_diagnostic_command(self, path: str) -> Optional[List[str]]:
        suffix = Path(path).suffix.lower()
        if suffix not in {".ts", ".tsx", ".js", ".jsx"}:
            return None
        tsconfig = self.root / "tsconfig.json"
        if not tsconfig.exists() or not tsconfig.is_file():
            return None
        tsc_path = self._repo_command_path("tsc")
        if not tsc_path:
            return None
        return [tsc_path, "--noEmit", "--pretty", "false", "-p", str(tsconfig)]

    def _python_diagnostic_command(self, path: str) -> Optional[List[str]]:
        if Path(path).suffix.lower() != ".py":
            return None
        return [sys.executable, "-m", "py_compile", path]

    def _parse_typescript_diagnostics(self, combined_output: str, target_path: str, limit: int = 8) -> List[Dict[str, Any]]:
        diagnostics: List[Dict[str, Any]] = []
        normalized_target = self._normalize_repo_relative_path(target_path)
        rx = re.compile(
            r"^(?P<path>.+?)\((?P<line>\d+),(?P<column>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<message>.+)$",
            flags=re.MULTILINE,
        )
        for match in rx.finditer(combined_output or ""):
            diag_path = self._normalize_repo_relative_path(match.group("path"))
            if diag_path != normalized_target:
                continue
            diagnostics.append(
                {
                    "path": diag_path,
                    "line": int(match.group("line")),
                    "column": int(match.group("column")),
                    "code": match.group("code"),
                    "message": match.group("message").strip(),
                    "source": "tsc",
                }
            )
            if len(diagnostics) >= limit:
                break
        return diagnostics

    def _parse_python_diagnostics(self, combined_output: str, target_path: str) -> List[Dict[str, Any]]:
        diagnostics: List[Dict[str, Any]] = []
        normalized_target = self._normalize_repo_relative_path(target_path)
        line_match = re.search(r'File "(?P<path>.+?)", line (?P<line>\d+)', combined_output or "")
        if not line_match:
            return diagnostics
        diag_path = self._normalize_repo_relative_path(line_match.group("path"))
        if diag_path != normalized_target:
            return diagnostics
        tail = (combined_output or "").strip().splitlines()
        message = tail[-1].strip() if tail else "Python compilation failed"
        diagnostics.append(
            {
                "path": diag_path,
                "line": int(line_match.group("line")),
                "column": 1,
                "code": "PY_COMPILE",
                "message": message,
                "source": "py_compile",
            }
        )
        return diagnostics

    def _diagnostic_probe_for_path(self, path: str) -> Optional[Dict[str, Any]]:
        ts_command = self._typescript_diagnostic_command(path)
        if ts_command is not None:
            return {
                "engine": "tsc",
                "command": ts_command,
                "parser": self._parse_typescript_diagnostics,
            }
        py_command = self._python_diagnostic_command(path)
        if py_command is not None:
            return {
                "engine": "py_compile",
                "command": py_command,
                "parser": self._parse_python_diagnostics,
            }
        return None

    def _run_automatic_diagnostics_for_path(self, *, path: str, trigger_action_type: str) -> Optional[AgentStep]:
        try:
            run = run_backend_diagnostics(
                self.root,
                path=path,
                limit=8,
                timeout=min(self.config.shell_timeout, 20),
            )
        except ValueError:
            return None

        mutation_step = self._latest_successful_edit_step_for_path(path)

        host_action = {
            "type": "diagnose",
            "agent": "host_diagnostics",
            "path": path,
            "command": list(run.command),
        }
        diagnostics = [dict(item) for item in run.diagnostics]
        stdout = run.stdout
        stderr = run.stderr
        returncode = int(run.returncode)

        if returncode == 0:
            if mutation_step is not None:
                self._update_auto_edit_attempt_fact_with_diagnostics(
                    mutation_step=mutation_step,
                    engine=run.engine,
                    status="clean",
                    diagnostics=None,
                )
            return None

        if not diagnostics:
            if mutation_step is not None:
                self._update_auto_edit_attempt_fact_with_diagnostics(
                    mutation_step=mutation_step,
                    engine=run.engine,
                    status="failed_nonmatching",
                    diagnostics=None,
                )
            return None

        if mutation_step is not None:
            self._update_auto_edit_attempt_fact_with_diagnostics(
                mutation_step=mutation_step,
                engine=run.engine,
                status="failed_matching",
                diagnostics=diagnostics,
            )

        if returncode == 0 or not diagnostics:
            return None

        result = ActionResult(
            ok=False,
            name="host_diagnostics",
            payload={
                "code": "POST_MUTATION_DIAGNOSTIC_FAILED",
                "message": (
                    f"Automatic {run.engine} diagnostics found {len(diagnostics)} issue(s) related to {path} after {trigger_action_type}."
                ),
                "path": path,
                "diagnostic_engine": run.engine,
                "diagnostics": diagnostics,
                "returncode": returncode,
                "command": list(run.command),
                "stdout": shorten(stdout, 2000),
                "stderr": shorten(stderr, 2000),
            },
        )
        step = self._record_host_step(
            thought=f"Host ran automatic {run.engine} diagnostics for {path} after successful {trigger_action_type} and found matching issues.",
            action=host_action,
            result=result,
            elapsed_s=0.0,
        )
        self._set_active_error(host_action, result)
        try:
            self._add_active_note(
                f"Automatic {run.engine} diagnostics found {len(diagnostics)} issue(s) for {path}; address them before continuing."
            )
        except Exception:
            pass
        return step

    def _handle_diagnose_action(self, action: Dict[str, Any]) -> ActionResult:
        path = self._normalize_repo_relative_path(str(action.get("path", "") or ""))
        if not path:
            return ActionResult(ok=False, name="diagnose", payload={"error": "diagnose requires a path"})

        try:
            limit = int(action.get("limit", 8) or 8)
        except Exception:
            limit = 8

        try:
            run = run_backend_diagnostics(
                self.root,
                path=path,
                limit=max(1, min(limit, 50)),
                timeout=min(self.config.shell_timeout, 30),
            )
        except ValueError as exc:
            return ActionResult(ok=False, name="diagnose", payload={"error": str(exc), "path": path})

        diagnostics = [dict(item) for item in run.diagnostics]
        payload: Dict[str, Any] = {
            "path": run.path,
            "diagnostic_engine": run.engine,
            "diagnostic_scope": run.scope,
            "diagnostics": diagnostics,
            "returncode": int(run.returncode),
            "command": list(run.command),
            "stdout": shorten(run.stdout, 2000),
            "stderr": shorten(run.stderr, 2000),
        }

        if diagnostics:
            payload.update(
                {
                    "code": "DIAGNOSTICS_FOUND",
                    "message": f"Backend {run.engine} diagnostics found {len(diagnostics)} issue(s) in {run.path}.",
                    "suggested_next_actions": [
                        {"type": "read_file", "path": run.path},
                        {"type": "find_files", "path": ".", "glob": "**/tsconfig*.json", "limit": 20},
                        {"type": "grep", "pattern": "vitest|jest|types", "path": ".", "glob": "**/*.{json,ts,tsx,js,jsx}", "limit": 20},
                    ],
                }
            )
            return ActionResult(ok=False, name="diagnose", payload=payload)

        if run.returncode != 0:
            if not str(run.stdout or "").strip() and not str(run.stderr or "").strip():
                payload["summary"] = (
                    f"Backend {run.engine} diagnostics found no issues in {run.path}."
                )
                return ActionResult(ok=True, name="diagnose", payload=payload)
            payload.update(
                {
                    "code": "DIAGNOSTIC_COMMAND_FAILED",
                    "message": f"Backend {run.engine} diagnostics failed for {run.path} without parseable diagnostics.",
                }
            )
            return ActionResult(ok=False, name="diagnose", payload=payload)

        payload["summary"] = f"Backend {run.engine} diagnostics found no issues in {run.path}."
        return ActionResult(ok=True, name="diagnose", payload=payload)

    def _handle_changed_files_check_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        try:
            result_payload = run_changed_files_check(root=self.root)
        except Exception as exc:
            return ActionResult(ok=False, name=action_type, payload={"error": str(exc)})
        return self._suite_diagnostics_action_result(
            action_type=action_type,
            result_payload=result_payload,
            success_summary="Checked changed files for diagnostics",
            failure_summary="Changed-files diagnostics found blocking issues",
            context_label="changed_files_check",
        )

    def _handle_project_problems_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        mode = str(action.get("mode", "standard") or "standard").strip() or "standard"
        try:
            result_payload = run_project_problems(mode=mode, root=self.root)
        except Exception as exc:
            return ActionResult(ok=False, name=action_type, payload={"error": str(exc), "mode": mode})
        return self._suite_diagnostics_action_result(
            action_type=action_type,
            result_payload={**result_payload, "mode": mode},
            success_summary=f"Collected project diagnostics ({mode})",
            failure_summary=f"Project diagnostics ({mode}) found blocking issues",
            context_label=f"project_problems:{mode}",
        )

    def _suite_diagnostics_action_result(
        self,
        *,
        action_type: str,
        result_payload: Dict[str, Any],
        success_summary: str,
        failure_summary: str,
        context_label: str,
    ) -> ActionResult:
        diagnostics = result_payload.get("diagnostics") if isinstance(result_payload.get("diagnostics"), list) else []
        summary = result_payload.get("summary") if isinstance(result_payload.get("summary"), dict) else {}
        error_count = int(summary.get("error", 0)) if isinstance(summary, dict) else 0
        warning_count = int(summary.get("warning", 0)) if isinstance(summary, dict) else 0
        payload: Dict[str, Any] = {
            **result_payload,
            "diagnostics": diagnostics,
            "diagnostic_count": len(diagnostics),
            "diagnostic_summary": summary,
        }
        if diagnostics:
            first_file = ""
            if isinstance(diagnostics[0], dict):
                first_file = str(diagnostics[0].get("file", "") or "")
            payload["suggested_next_actions"] = [
                {"type": "read_file", "path": first_file} if first_file else {"type": "review_changes", "limit": 20},
                {"type": "review_changes", "limit": 20},
            ]
        if error_count > 0:
            payload.update(
                {
                    "code": "DIAGNOSTICS_FOUND",
                    "message": f"{action_type} found {len(diagnostics)} diagnostic(s), including {error_count} error(s).",
                }
            )
            return ActionResult(ok=False, name=action_type, payload=payload)
        if warning_count > 0:
            payload["message"] = f"{action_type} found {warning_count} warning(s) and no errors."
        payload["summary"] = success_summary if not diagnostics else payload.get("message", success_summary)
        self._try_activate_context(payload, context_label)
        return ActionResult(ok=True, name=action_type, payload=payload)

    def _run_automatic_diagnostics_after_step(self, step: AgentStep) -> None:
        if not step.result.ok:
            return
        action_type = str(step.action.get("type", "") or "")
        if action_type == "patch_file" and self._is_idempotent_patch_result(step):
            return
        candidate_paths: List[str] = []
        if action_type in MUTATION_ACTION_TYPES and not self.edit_batch_mode:
            step_path = str(step.action.get("path", "") or "").strip()
            if step_path:
                candidate_paths.append(step_path)
        elif action_type == "end_edit_batch":
            payload = step.result.payload if isinstance(step.result.payload, dict) else {}
            verified_paths = payload.get("verified_paths")
            if isinstance(verified_paths, list):
                candidate_paths.extend(str(item).strip() for item in verified_paths if str(item).strip())

        for path in uniq(candidate_paths):
            self._run_automatic_diagnostics_for_path(path=path, trigger_action_type=action_type)

    def _edit_batch_verification_jobs(self) -> List[ParallelToolJob]:
        jobs: List[ParallelToolJob] = []
        for path in sorted(self.edit_batch_pending.keys()):
            jobs.append(
                ParallelToolJob(
                    label=f"edit_batch_verify::{path}",
                    subcommand="read",
                    args=("--path", path),
                    action_type="read_file",
                    lock_mode="read",
                    lock_path=path,
                )
            )
        return jobs

    def _run_edit_batch_verification(self, *, reason: str) -> ActionResult:
        if not self.edit_batch_mode:
            return ActionResult(ok=True, name="end_edit_batch", payload={"message": "Edit batch mode is not active."})

        if not self.edit_batch_pending:
            self._clear_edit_batch_state()
            return ActionResult(ok=True, name="end_edit_batch", payload={"message": "Edit batch closed with no pending file verifications."})

        jobs = self._edit_batch_verification_jobs()
        records = self._run_parallel_tool_batch(jobs, title="edit_batch_verification")
        verified_paths: List[str] = []
        failed_paths: List[str] = []
        results: List[Dict[str, Any]] = []

        for record in records:
            path = str(record.job.lock_path or "")
            verification_item = self.edit_batch_pending.get(path)
            if verification_item is None:
                continue
            outcome = self._normalize_tool_outcome(
                action_type="read_file",
                tool_result=record.result,
                success_summary=f"Verified batched edits for {path}",
                failure_summary=f"Could not verify batched edits for {path}",
                next_hint="finish" if bool(record.result.get("ok")) else "read_file",
            )
            action_result = self._action_result_from_outcome(outcome)
            result_payload = action_result.payload if isinstance(action_result.payload, dict) else {}
            if action_result.ok and isinstance(result_payload, dict):
                verified, verification_basis = self._verification_item_matches(verification_item, result_payload)
                if verified:
                    result_payload["verification_read"] = True
                    result_payload["verification_cleared"] = True
                    result_payload["verification_message"] = f"Batched verification for {path} cleared via {verification_basis or 'file confirmation'}."
                    verified_paths.append(path)
                    self.edit_batch_pending.pop(path, None)
                else:
                    action_result = ActionResult(
                        ok=False,
                        name="read_file",
                        payload={
                            "code": "BATCH_VERIFICATION_FAILED",
                            "message": f"Batched verification for {path} did not match the expected post-edit file state.",
                            "path": path,
                            "expected_sha256": verification_item.get("expected_sha256"),
                            "actual_sha256": result_payload.get("sha256"),
                        },
                    )
                    failed_paths.append(path)
            else:
                failed_paths.append(path)

            self._record_host_step(
                thought=f"Host ran batched verification read for {path} after edit batch {reason}.",
                action={"type": "read_file", "path": path, "agent": "host_edit_batch_verification"},
                result=action_result,
                elapsed_s=record.duration_s,
            )
            results.append({
                "path": path,
                "ok": action_result.ok,
                "payload": action_result.payload,
            })

        if failed_paths:
            self._record_edit_batch_summary_fact(
                status="failed",
                verified_paths=verified_paths,
                failed_paths=failed_paths,
                results=results,
            )
            for path in verified_paths:
                self._prune_batch_auto_edit_attempt_facts_for_path(path, keep_mode="success")
            for path in failed_paths:
                self._prune_batch_auto_edit_attempt_facts_for_path(path, keep_mode="latest")
            try:
                self._set_edit_batch_context_item()
                self._add_active_note(
                    f"Edit batch verification still needs attention for: {', '.join(failed_paths)}."
                )
            except Exception:
                pass
            return ActionResult(
                ok=False,
                name="end_edit_batch",
                payload={
                    "code": "EDIT_BATCH_VERIFICATION_INCOMPLETE",
                    "message": "Some batched edit verifications did not match the expected file state.",
                    "verified_paths": verified_paths,
                    "failed_paths": failed_paths,
                    "results": results,
                },
            )

        self._record_edit_batch_summary_fact(
            status="success",
            verified_paths=verified_paths,
            failed_paths=[],
            results=results,
        )
        for path in verified_paths:
            self._prune_batch_auto_edit_attempt_facts_for_path(path, keep_mode="success")

        self._clear_edit_batch_state()
        try:
            self._add_active_note(
                f"Edit batch verification completed for {len(verified_paths)} file(s)."
            )
        except Exception:
            pass
        return ActionResult(
            ok=True,
            name="end_edit_batch",
            payload={
                "message": f"Edit batch verified {len(verified_paths)} file(s).",
                "verified_paths": verified_paths,
                "results": results,
            },
        )

    def _call_tool(self, name: str, *args: str, action: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # Simplified observability: buffer request + result blocks in memory,
        # then persist them once at the end of the run.
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())

        request_parts: List[str] = [f"## {ts} - tool: {name}\n\n"]
        if action is not None:
            request_parts.append("**Action**\n```json\n")
            try:
                request_parts.append(json.dumps(action, indent=2, ensure_ascii=False))
            except Exception:
                request_parts.append(str(action))
            request_parts.append("\n```\n\n")

        if self.steering_prompt.strip():
            request_parts.append("**Operator steering**\n")
            request_parts.append(self.steering_prompt)
            request_parts.append("\n\n")

        request_parts.append("**Args**\n```json\n")
        try:
            request_parts.append(json.dumps(list(args), indent=2, ensure_ascii=False))
        except Exception:
            request_parts.append(str(list(args)))
        request_parts.append("\n```\n\n")

        self._append_observability_block("".join(request_parts))
        if self._run_metrics:
            self._run_metrics["tool_calls"] = int(self._run_metrics.get("tool_calls", 0)) + 1

        action_type = str(action.get("type", name) if isinstance(action, dict) else name)
        lock_mode = self._action_lock_mode(action_type)
        lock_path = self._action_lock_path(action_type, action)
        with self.path_locks.acquire(lock_mode, lock_path):
            result = self.tools.call(name, *args)

        response_parts: List[str] = []
        response_parts.append("**Result**\n```json\n")
        try:
            response_parts.append(json.dumps(result, indent=2, ensure_ascii=False))
        except Exception:
            response_parts.append(str(result))
        response_parts.append("\n```\n\n---\n\n")

        self._append_observability_block("".join(response_parts))
        return result

    def _normalize_tool_outcome(
        self,
        *,
        action_type: str,
        tool_result: Dict[str, Any],
        success_summary: Optional[str] = None,
        failure_summary: Optional[str] = None,
        next_hint: Optional[str] = None,
    ) -> ToolOutcome:
        ok = bool(tool_result.get("ok"))
        data = tool_result.get("data") if isinstance(tool_result.get("data"), dict) else None

        raw_error = tool_result.get("error")
        error: Optional[Dict[str, Any]] = None
        if isinstance(raw_error, dict):
            error = raw_error
        elif raw_error is not None:
            error = {
                "code": "TOOL_ERROR",
                "message": str(raw_error),
            }

        if ok:
            summary = success_summary or f"{action_type} succeeded"
            status = "succeeded"
        else:
            if error is not None:
                message = error.get("message") or str(error)
            else:
                message = "Tool action failed"
            summary = failure_summary or f"{action_type} failed: {message}"
            status = "failed"

        return ToolOutcome(
            ok=ok,
            action_type=action_type,
            status=status,
            summary=summary,
            data=data,
            error=error,
            raw=tool_result,
            next_hint=next_hint,
        )

    def _action_result_from_outcome(self, outcome: ToolOutcome) -> ActionResult:
        payload: Dict[str, Any] = {
            "ok": outcome.ok,
            "action_type": outcome.action_type,
            "status": outcome.status,
            "summary": outcome.summary,
            "data": outcome.data,
            "error": outcome.error,
            "next_hint": outcome.next_hint,
            "raw": outcome.raw,
        }
        if isinstance(outcome.data, dict):
            for key, value in outcome.data.items():
                if key not in payload:
                    payload[key] = value
        return ActionResult(
            ok=outcome.ok,
            name=outcome.action_type,
            payload=payload,
        )

    def _expand_history_step(self, step_number: int) -> Dict[str, Any]:
        for step in self.history:
            if step.step == step_number:
                expanded = {
                    "step": step.step,
                    "thought": step.thought,
                    "action": step.action,
                    "result_ok": step.result.ok,
                    "result_name": step.result.name,
                    "result_payload": step.result.payload,
                    "elapsed_s": step.elapsed_s,
                }
                try:
                    self._set_active_context_item(expanded, f"history_step_{step_number}")
                    self.fulfillment_mode = True
                except Exception:
                    pass
                return expanded
        return {"error": f"History step not found: {step_number}"}

    def _expand_memory_item(self, memory_id: str) -> Dict[str, Any]:
        try:
            getter = getattr(self.memory, "get_item", None)
            if callable(getter):
                item = getter(memory_id)
                if item is None:
                    return {"error": f"Memory item not found: {memory_id}"}
                try:
                    self._set_active_context_item(cast(Dict[str, Any], item), f"memory_{memory_id}")
                    self.fulfillment_mode = True
                except Exception:
                    pass
                return cast(Dict[str, Any], item)
            return {"error": "Memory expansion not available: get_item missing"}
        except Exception as exc:
            return {"error": str(exc)}

    def _expand_history_steps(self, step_numbers: List[int]) -> Dict[str, Any]:
        expanded_items: List[Dict[str, Any]] = []
        missing: List[int] = []

        seen = set()
        for step_number in step_numbers:
            if step_number in seen:
                continue
            seen.add(step_number)
            item = self._expand_history_step(step_number)
            if "error" in item:
                missing.append(step_number)
            else:
                expanded_items.append(item)

        if expanded_items:
            self.fulfillment_mode = True
            self._add_active_note(
                f"Expanded history steps into active context: {', '.join(str(x) for x in [item['step'] for item in expanded_items if 'step' in item])}"
            )

        return {
            "expanded": expanded_items,
            "missing": missing,
            "count": len(expanded_items),
        }

    def _expand_memory_items(self, memory_ids: List[str]) -> Dict[str, Any]:
        expanded_items: List[Dict[str, Any]] = []
        missing: List[str] = []

        seen = set()
        for memory_id in memory_ids:
            if memory_id in seen:
                continue
            seen.add(memory_id)
            item = self._expand_memory_item(memory_id)
            if "error" in item:
                missing.append(memory_id)
            else:
                expanded_items.append(item)

        if expanded_items:
            self.fulfillment_mode = True
            ids = [str(item.get("id")) for item in expanded_items if item.get("id")]
            self._add_active_note(f"Expanded memory items into active context: {', '.join(ids)}")

        return {
            "expanded": expanded_items,
            "missing": missing,
            "count": len(expanded_items),
        }

    def run_task(self, task: str) -> WorkerRunResult:
        # Reset or preserve active context depending on the task's strategy.
        self._current_task = task
        self._reset_task_satisfaction()
        self._reset_run_observability(task)
        self._start_new_run()
        self._maybe_reset_context_for_strategy(task)
        if self.discovery_budget is not None:
            self._parallel_discovery_prefetch_payload(task)
        final_message = "Stopped: max steps reached."
        run_ok = False
        try:
            for _turn in range(1, self.config.max_steps + 1):
                step_num = len(self.history) + 1
                prompt = self._build_prompt(task)
                self._mark_recent_resolution_handoff_delivered()
                if self.config.show_prompts:
                    print("\n--- PROMPT ---\n")
                    print(prompt)

                started = time.time()
                raw = self.model.complete(self._system_prompt(), prompt)
                model_duration = time.time() - started
                self._record_model_turn_metrics(step_num=step_num, duration_s=model_duration)

                if self.config.show_model_output:
                    print("\n--- MODEL RAW OUTPUT ---\n")
                    print(raw)

                try:
                    obj = extract_first_json_object(raw)
                except Exception as exc:
                    self._set_output_format_recovery(
                        error_type="parse_error",
                        message=f"Could not parse JSON: {exc}",
                        raw=raw,
                    )
                    result = ActionResult(
                        ok=False,
                        name="parse_error",
                        payload={"error": f"Could not parse JSON: {exc}", "raw": shorten(raw, 4000)},
                    )
                    error_step = AgentStep(
                        step=len(self.history) + 1,
                        thought="Model returned invalid JSON.",
                        action={"type": "output_format_error", "error_type": "parse_error"},
                        result=result,
                        elapsed_s=time.time() - started,
                        run_id=self._active_run_id,
                    )
                    self.history.append(error_step)
                    self._print_step(error_step)
                    continue

                thought = str(obj.get("thought", "")).strip()

                actions, schema_error = self._extract_actions_from_model_object(obj)
                if schema_error is not None or actions is None:
                    result = schema_error or ActionResult(
                        ok=False,
                        name="schema_error",
                        payload={"error": "Invalid action payload", "object": obj},
                    )
                    self._set_output_format_recovery(
                        error_type="schema_error",
                        message=str(result.payload.get("error", "Invalid action payload")) if isinstance(result.payload, dict) else "Invalid action payload",
                        raw=raw,
                    )
                    error_step = AgentStep(
                        step=len(self.history) + 1,
                        thought=thought,
                        action={"type": "output_format_error", "error_type": "schema_error"},
                        result=result,
                        elapsed_s=time.time() - started,
                        run_id=self._active_run_id,
                    )
                    self.history.append(error_step)
                    self._print_step(error_step)
                    continue

                for action in actions:
                    if (
                        self.completion_check_pending
                        and action.get("type") != "finish"
                        and self._is_validation_action(str(action.get("type", "") or ""))
                    ):
                        self._clear_completion_check(keep_satisfaction=True)

                    self._current_thought = thought
                    result = self._execute_action(action)
                    if result.ok:
                        self._clear_output_format_recovery()
                    error_cleared = self._maybe_clear_active_error(action, result)
                    if not result.ok:
                        self._set_active_error(action, result)
                    step = AgentStep(
                        step=len(self.history) + 1,
                        thought=thought,
                        action=action,
                        result=result,
                        elapsed_s=time.time() - started,
                        run_id=self._active_run_id,
                    )
                    self.history.append(step)
                    if error_cleared and isinstance(step.result.payload, dict):
                        step.result.payload["error_cleared"] = True
                    if self._run_metrics:
                        self._run_metrics["steps"] = int(self._run_metrics.get("steps", 0)) + 1
                        if result.ok:
                            self._run_metrics["successful_actions"] = int(self._run_metrics.get("successful_actions", 0)) + 1
                        else:
                            self._run_metrics["failed_actions"] = int(self._run_metrics.get("failed_actions", 0)) + 1

                    self._ingest_memory(
                        task=task,
                        thought=thought,
                        action=action,
                        result=result,
                    )

                    self._print_step(step)
                    self._run_automatic_diagnostics_after_step(step)
                    self._run_fact_subagent(task, step)

                    if action["type"] == "finish" and result.ok:
                        final_message = str(result.payload.get("message", "Done."))
                        run_ok = True
                        return self._build_run_result(final_message, run_ok)

                    entered_completion_check = self._maybe_enter_completion_check(task)
                    if not result.ok:
                        break
                    if entered_completion_check:
                        continue

                    forced_finish_message = self._should_force_finish_after_satisfaction()
                    if forced_finish_message:
                        try:
                            self._clear_active_context()
                            self._clear_patch_recovery()
                        except Exception:
                            pass
                        final_message = forced_finish_message
                        run_ok = True
                        return self._build_run_result(final_message, run_ok)

            return self._build_run_result(final_message, run_ok)
        finally:
            self._last_run_result = self._build_run_result(final_message, run_ok)
            self._flush_observability(final_message)

    def _print_step(self, step: AgentStep) -> None:
        if self.on_step_callback is not None:
            try:
                self.on_step_callback(step)
            except Exception:
                pass
        if self.config.quiet:
            return
        print(f"\n[{step.step}] action={step.action.get('type')} ok={step.result.ok} elapsed={step.elapsed_s:.2f}s")
        if step.thought:
            print(textwrap.indent(step.thought, "  thought: "))
        if not step.result.ok:
            print(textwrap.indent(shorten(json.dumps(step.result.payload, indent=2), 3000), "  error: "))
            edit_batch_table = self._render_edit_batch_table()
            if edit_batch_table:
                print(textwrap.indent(edit_batch_table, "  "))
            pending_verification_table = self._render_pending_verification_table()
            if pending_verification_table:
                print(textwrap.indent(pending_verification_table, "  "))
            return

        if isinstance(step.result.payload, dict):
            summary = step.result.payload.get("summary")
            if isinstance(summary, str) and summary:
                print(textwrap.indent(f"summary: {summary}", "  "))

        if step.action.get("type") == "read_file":
            print(textwrap.indent(f"read: {step.action.get('path')}", "  "))
        elif step.action.get("type") == "inspect_files":
            files = step.result.payload.get("files", []) if isinstance(step.result.payload, dict) else []
            print(textwrap.indent(f"inspected files: {len(files)}", "  "))
        elif step.action.get("type") == "summarize_files":
            files = step.result.payload.get("files", []) if isinstance(step.result.payload, dict) else []
            print(textwrap.indent(f"summarized files: {len(files)}", "  "))
        elif step.action.get("type") == "write_file":
            if isinstance(step.result.payload, dict) and step.result.payload.get("verification_pending"):
                print(textwrap.indent(f"wrote: {step.action.get('path')} (verification pending)", "  "))
            else:
                print(textwrap.indent(f"wrote: {step.action.get('path')}", "  "))
        elif step.action.get("type") == "list_files":
            print(textwrap.indent(f"listed files under root", "  "))
        elif step.action.get("type") == "run_shell":
            rc = step.result.payload.get("returncode")
            print(textwrap.indent(f"shell rc={rc}: {step.action.get('command')}", "  "))
            stdout = step.result.payload.get("stdout", "")
            stderr = step.result.payload.get("stderr", "")
            if stdout:
                print(textwrap.indent("stdout:\n" + shorten(stdout, 2000), "    "))
            if stderr:
                print(textwrap.indent("stderr:\n" + shorten(stderr, 2000), "    "))
        elif step.action.get("type") == "patch_file":
            status = None
            if isinstance(step.result.payload, dict):
                data = step.result.payload.get("data")
                if isinstance(data, dict):
                    status = data.get("status")
                if status is None:
                    status = step.result.payload.get("status")
            if status == "already_applied":
                print(textwrap.indent(f"patch already applied: {step.action.get('path')}", "  "))
            elif isinstance(step.result.payload, dict) and step.result.payload.get("verification_pending"):
                print(textwrap.indent(f"patched: {step.action.get('path')} (verification pending)", "  "))
            else:
                print(textwrap.indent(f"patched: {step.action.get('path')}", "  "))
            if not step.result.ok:
                print(textwrap.indent(f"patch error: {step.result.payload}", "    "))
        elif step.action.get("type") in (MUTATION_ACTION_TYPES - {"write_file", "patch_file"}):
            action_type = str(step.action.get("type", "") or "")
            path = str(step.action.get("path", "") or step.action.get("new_path", "") or step.action.get("destination_path", "") or "")
            suffix = " (verification pending)" if isinstance(step.result.payload, dict) and step.result.payload.get("verification_pending") else ""
            print(textwrap.indent(f"{action_type}: {path or 'mutation target'}{suffix}", "  "))
        elif step.action.get("type") == "grep":
            matches = step.result.payload.get("matches", [])
            print(textwrap.indent(f"grep matches: {len(matches)}", "  "))
        elif step.action.get("type") == "find_files":
            files = step.result.payload.get("files", [])
            print(textwrap.indent(f"find files: {len(files)}", "  "))
        elif step.action.get("type") == "symbol_search":
            symbols = step.result.payload.get("symbols", []) if isinstance(step.result.payload, dict) else []
            print(textwrap.indent(f"symbols found: {len(symbols)}", "  "))
        elif step.action.get("type") == "show_diff":
            print(textwrap.indent("diff fetched", "  "))
        elif step.action.get("type") == "meta":
            print(textwrap.indent("meta fetched", "  "))
        elif step.action.get("type") == "git_status":
            print(textwrap.indent("git status fetched", "  "))
        elif step.action.get("type") == "git_diff":
            print(textwrap.indent("git diff fetched", "  "))
        elif step.action.get("type") == "review_changes":
            files = step.result.payload.get("files", []) if isinstance(step.result.payload, dict) else []
            print(textwrap.indent(f"reviewed changed files: {len(files)}", "  "))
        elif step.action.get("type") == "changed_files_check":
            diagnostics = step.result.payload.get("diagnostics", []) if isinstance(step.result.payload, dict) else []
            print(textwrap.indent(f"changed-files diagnostics: {len(diagnostics)}", "  "))
        elif step.action.get("type") == "project_problems":
            diagnostics = step.result.payload.get("diagnostics", []) if isinstance(step.result.payload, dict) else []
            print(textwrap.indent(f"project problems: {len(diagnostics)}", "  "))
        elif step.action.get("type") == "git_add":
            paths = step.result.payload.get("paths") if isinstance(step.result.payload, dict) else None
            if isinstance(paths, list) and paths:
                print(textwrap.indent(f"git added: {', '.join(str(p) for p in paths)}", "  "))
            else:
                print(textwrap.indent(f"git added: {step.action.get('path')}", "  "))
        elif step.action.get("type") == "git_restore":
            print(textwrap.indent(f"git restored: {step.action.get('path')}", "  "))
        elif step.action.get("type") == "git_commit":
            print(textwrap.indent(f"git commit: {step.action.get('message')}", "  "))
        elif step.action.get("type") == "git_log":
            commits = step.result.payload.get("commits", [])
            print(textwrap.indent(f"git log commits: {len(commits)}", "  "))
        elif step.action.get("type") == "git_branch":
            print(textwrap.indent(f"git branch: {step.result.payload.get('branch', '')}", "  "))
        elif step.action.get("type") == "finish":
            print(textwrap.indent(f"finish: {step.result.payload.get('message', '')}", "  "))
            print(textwrap.indent(self._render_current_run_facts_panel(), "  "))
        elif step.action.get("type") == "history_expand":
            if step.action.get("steps"):
                print(textwrap.indent(f"expanded history steps: {step.action.get('steps')}", "  "))
            else:
                print(textwrap.indent(f"expanded history step: {step.action.get('step')}", "  "))
        elif step.action.get("type") == "memory_expand":
            if step.action.get("ids"):
                print(textwrap.indent(f"expanded memory items: {step.action.get('ids')}", "  "))
            else:
                print(textwrap.indent(f"expanded memory item: {step.action.get('id')}", "  "))
        elif step.action.get("type") == "set_fact":
            label = "fact subagent set" if step.action.get("agent") == "fact_subagent" else "fact set"
            print(textwrap.indent(f"{label}: {step.action.get('key')} = {step.action.get('value')}", "  "))
        elif step.action.get("type") == "update_fact":
            label = "fact subagent updated" if step.action.get("agent") == "fact_subagent" else "fact updated"
            print(textwrap.indent(f"{label}: {step.action.get('key')}", "  "))
        elif step.action.get("type") == "drop_context":
            print(textwrap.indent("dropped active context", "  "))

        pending_verification_table = self._render_pending_verification_table()
        if pending_verification_table:
            print(textwrap.indent(pending_verification_table, "  "))
        edit_batch_table = self._render_edit_batch_table()
        if edit_batch_table:
            print(textwrap.indent(edit_batch_table, "  "))

    def _system_prompt(self) -> str:
                shell_rule = (
                    "Use shell commands only when useful for validation, tests, formatting, or inspection."
                    if self._shell_access_enabled()
                    else "Shell commands are disabled for this run via SHELL_ACCESS=false. Do not emit run_shell actions."
                )
                shell_note = self._shell_access_note()
                return textwrap.dedent(f"""
                You are a coding agent operating on a real working folder.
                Immutable: Respect your scope and do not over-discover. 
                Collect Facts, correct stale facts. Do not force unstale fact verification, they are there to help you make better decisions, chase goal.
                Do not chase things you've already verified. Mark as finish: done.
                {shell_note}
                                       
                Rules:
                1. You may ONLY act within the root path provided by the user.
                2. All file paths must be relative to that root.
                3. Prefer reading files before editing them, but once enough evidence exists, prefer patching over more exploration.
                4. Keep edits minimal and targeted.
                5. {shell_rule}
                6. Never ask for tools; choose the next action yourself. Use a short ordered batch only when the steps are tightly coupled and clearly higher-yield than waiting for another turn.
                7. Return EXACTLY one JSON object and nothing else.
                7d. Do not answer with prose-only plans, numbered checklists, markdown, or "I will..." summaries. Put any plan in `thought`, then emit an executable `action` or `actions`.
                7a. Prefer the narrowest implementation that directly satisfies the user's request.
                7b. Do not expand admin/editor constraints into public runtime behavior unless the request or concrete code dependency requires it.
                7c. Do not claim performance gains or WCAG compliance unless you measured them, computed them, or implemented an actual validation mechanism.

                8. RELEVANT MEMORY contains compact prior tool results selected for the current task.
                9. Prefer using paths, entities, and tags from RELEVANT MEMORY when choosing your next action.
                10. Do not repeat broad exploration if RELEVANT MEMORY already identifies likely targets.
                11. If more detail is needed, request it with a focused tool call rather than guessing.
                12. Treat each read_file as expensive: before reading, identify the concrete questions you expect that read to answer.
                13. After reading a file, extract multiple relevant signals from it, not just the first thing you notice.
                14. A good read should narrow several possibilities at once: likely edit site, surrounding constraints, validation strategy, and any blockers.
                15. Do not tunnel onto one token, one line, or one branch of logic if the same read supports a broader conclusion.
                15a. If the target looks like a directory or the next question is about repo/file topology, prefer `list_files` before `read_file` so you ground the path structure first.
                16. When choosing another read_file, prefer the highest-yield file for the next decision, not merely the nearest adjacent file.
                16a. If a particular problem anchor has stalled and focused follow-up produced neither goal progress nor a durable fact, stop extending that branch and prefer `finish` with a summary that explicitly tells the goal planner a change of strategy is needed.

                Required JSON schema:
                {{
                    "thought": "brief reasoning summary visible to the operator; for read_file actions, include what you want to learn from the read and any line range referencing avoiding already verified changes and reads. If a problem anchor has gone cold and produced no goal progress or durable facts, say that plainly and finish with a summary that tells the goal planner a change of strategy is needed. If satisfied move to finish: Done.",
                    "action": {{
                        "type": "list_files" | "read_file" | "inspect_files" | "summarize_files" | "find_files" | "search_in_files" | "outline_file" | "read_symbol" | "find_symbol_definitions" | "find_symbol_references" | "trace_dependencies" | "find_related_files" | "find_related_tests" | "find_related_configs" | "find_canonical_implementation" | "find_similar_code" | "find_entry_points" | "find_ownership" | "recent_changes" | "get_changed_files" | "semantic_search" | "investigate" | "write_file" | "patch_file" | "replace_range" | "replace_snippet" | "insert_before" | "insert_after" | "delete_range" | "delete_snippet" | "append_block" | "prepend_block" | "replace_symbol" | "insert_symbol_member" | "rename_symbol" | "move_block" | "create_file" | "delete_file" | "rename_file" | "copy_file" | "fill_template" | "batch_mutate" | "begin_edit_batch" | "end_edit_batch" | "grep" | "symbol_search" | "run_shell" | "diagnose" | "changed_files_check" | "project_problems" | "skill" | "show_diff" | "meta" | "git_status" | "git_diff" | "review_changes" | "git_add" | "git_restore" | "git_commit" | "git_log" | "git_branch" | "history_expand" | "memory_expand" | "set_fact" | "update_fact" | "drop_context" | "finish",
                        "... action-specific fields ..."
                    }}
                }}

                Or, for a short sequential batch:
                {{
                    "thought": "brief reasoning summary",
                    "actions": [
                        {{ "type": "begin_edit_batch" }},
                        {{ "type": "patch_file", "path": "src/main.py", "search": "old text", "replace": "new text" }},
                        {{ "type": "end_edit_batch" }}
                    ]
                }}

                Action formats:
                {{
                    "thought": "...",
                    "action": {{ "type": "list_files", "path": ".", "limit": 200, "recursive": false, "max_depth": 2, "glob": "src/**/*.py" }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "read_file", "path": "src/main.py", "start_line": 120, "end_line": 220 }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "inspect_files", "files": [{{ "path": "src/main.py", "start_line": 120, "end_line": 220 }}, {{ "path": "src/utils.py" }}] }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "summarize_files", "path": "src", "glob": "src/**/*.py", "limit": 25 }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "grep", "pattern": "PlannerAgent", "path": "src", "glob": "src/**/*.py", "limit": 20, "ignore_case": false, "fixed_strings": true }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "find_files", "path": "src", "glob": "src/**/*.tsx", "limit": 50 }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "search_in_files", "path": "src", "query": "PlannerAgent", "limit": 20, "literal": true }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "outline_file", "path": "src/main.py" }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "read_symbol", "path": "src/main.py", "symbol_name": "PlannerAgent", "symbol_kind": "class" }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "find_symbol_definitions", "path": "src", "symbol_name": "PlannerAgent", "limit": 20 }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "find_symbol_references", "path": "src", "symbol_name": "PlannerAgent", "limit": 20 }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "trace_dependencies", "path": "src/main.py", "direction": "both", "depth": 2 }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "find_canonical_implementation", "topic": "planner runtime", "path": ".", "limit": 10 }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "semantic_search", "intent": "where planner state is persisted", "path": ".", "limit": 10 }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "investigate", "topic": "completion check guard", "path": ".", "mode": "standard" }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "symbol_search", "path": "src", "glob": "src/**/*.py", "query": "PlannerAgent", "limit": 20 }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "diagnose", "path": "src/hooks/useTodoOperations.test.ts", "limit": 8 }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "changed_files_check" }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "project_problems", "mode": "standard" }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "skill", "name": "testing_playbook" }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "set_fact", "key": "entrypoint", "value": "planner.py owns discovery mode", "fact_type": "architecture" }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "update_fact", "key": "entrypoint", "value": "confirmed planner.py owns discovery mode", "fact_type": "architecture" }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "review_changes", "limit": 50 }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "begin_edit_batch" }}
                }}

                {{
                    "thought": "...",
                    "action": {{ "type": "end_edit_batch" }}
                }}

                {{
                    "thought": "...",
                    "action": {{
                        "type": "write_file",
                        "path": "src/main.py",
                        "content": "full new file content"
                    }}
                }}

                {{
                    "thought": "...",
                    "action": {{
                        "type": "patch_file",
                        "path": "src/main.py",
                        "search": "old text",
                        "replace": "new text",
                        "all": false
                    }}
                }}

                {{
                    "thought": "...",
                    "action": {{
                        "type": "replace_range",
                        "path": "src/main.py",
                        "start_line": 10,
                        "end_line": 14,
                        "new_text": "def main():\n    return 0"
                    }}
                }}

                {{
                    "thought": "...",
                    "action": {{
                        "type": "batch_mutate",
                        "atomic": true,
                        "operations": [
                            {{ "type": "replace_snippet", "path": "src/main.py", "old_text": "old", "new_text": "new" }},
                            {{ "type": "append_block", "path": "src/main.py", "new_text": "\nprint('done')\n" }}
                        ]
                    }}
                }}

                Prefer this workflow:
                inspect repo state -> use relevant memory -> expand only what is necessary -> once enough evidence exists, patch the most likely target -> validate -> finish

                17. Use memory_expand or history_expand only when you need exact details that are not already present.
                18. Every exploration produces a fact. Use set_fact or update_fact to record what you learned. Use fact_type=goal for task-local findings, fact_type=architecture for cross-task repo knowledge.
                19. If FULFILLMENT READINESS says ready=true, prefer concrete execution or validation over more broad exploration.
                20. Once task satisfaction is reached, prefer finish unless a concrete contradiction remains.
                21. For non-trivial code edits, prefer one real validation step before finish: read_file confirmation, git_diff/show_diff/review_changes, changed_files_check, project_problems, or a targeted build/test/typecheck when available.
                22. Do not accrete tiny same-file patches across many turns. After two successful edits on one file, prefer git_diff/show_diff/review_changes and then make at most one final consolidated patch for the remaining gap.
                23. Prefer the smallest reliable mutation primitive that matches the change: replace_range or replace_snippet before write_file, and structural/file-system mutations when the target is a symbol or file move rather than raw text.
                24. If several related edits are clearly needed, prefer begin_edit_batch, complete the related mutation actions, then call end_edit_batch so the host can verify all touched files in one read batch. While edit batch mode is active, same-file consolidation limits are deferred until batch exit.
                25. The host records mutation attempt facts automatically under keys like patch_attempt::..., write_attempt::..., or edit_attempt::<type>::.... Those auto facts include the originating thought summary and any automatic post-mutation diagnostics status. Reuse them only when that prior attempt matches the current reasoning branch; do not let an unrelated old attempt suppress a materially different edit.
                26. If you use `actions`, keep it to at most 4 ordered actions, stop at the first failure, and only place `finish` as the last item.

                If a task is ambiguous, inspect the repository first.
                """).strip()

    def _repo_snapshot(self) -> str:
        now = time.time()
        if self._repo_snapshot_cache is not None and (now - self._repo_snapshot_cache_ts) < 3.0:
            return self._repo_snapshot_cache

        meta = self._call_tool("meta")
        branch = self._call_tool("git-branch")
        status = self._call_tool("git-status")

        snapshot = {
            "meta": meta,
            "git_branch": branch,
            "git_status": status,
            "memory_stats": self.memory.stats(),
        }
        rendered = json.dumps(snapshot, indent=2)
        self._repo_snapshot_cache = rendered
        self._repo_snapshot_cache_ts = now
        return rendered

    def _history_snapshot(self) -> str:
        current_run_steps: List[AgentStep] = []
        previous_run_steps: List[AgentStep] = []
        for step in self.history:
            if getattr(step, "run_id", 0) == self._active_run_id:
                current_run_steps.append(step)
            else:
                previous_run_steps.append(step)

        snapshot_steps = current_run_steps if current_run_steps else previous_run_steps[-8:]
        data: List[Dict[str, Any]] = []
        for h in snapshot_steps:
            # Build a compact, addressable summary that the model can use
            # to decide whether to expand a particular step.
            paths: List[str] = []
            try:
                p = h.action.get("path")
                if isinstance(p, str):
                    paths.append(p)
            except Exception:
                pass
            try:
                p2 = h.result.payload.get("path")
                if isinstance(p2, str):
                    paths.append(p2)
            except Exception:
                pass

            data.append(
                {
                    "step": h.step,
                    "run_id": getattr(h, "run_id", 0),
                    "thought": h.thought,
                    "action_type": h.action.get("type"),
                    "result_ok": h.result.ok,
                    "result_name": h.result.name,
                    "paths": [p for p in paths if isinstance(p, str)],
                    "result_keys": list(h.result.payload.keys())[:10],
                    "result_summary": h.result.payload.get("summary") if isinstance(h.result.payload, dict) else None,
                    "result_status": h.result.payload.get("status") if isinstance(h.result.payload, dict) else None,
                    "next_hint": h.result.payload.get("next_hint") if isinstance(h.result.payload, dict) else None,
                    "expand_hint": {"action": "history_expand", "step": h.step},
                }
            )
        payload = {
            "scope": "current_run" if current_run_steps else "recent_history_fallback",
            "active_run_id": self._active_run_id,
            "step_count": len(data),
            "steps": data,
        }
        return json.dumps(payload, indent=2)

    def _build_prompt(self, task: str) -> str:
        memory_bundle = self._current_memory_bundle(task)
        # Include any currently active context (from history/memory expansion)
        # and compute readiness to decide whether to push for fulfillment.
        readiness = self._fulfillment_readiness(task)
        active_block = self._active_context_block()
        selected_goal_facts_block = self._selected_goal_facts_block()
        shell_access_note = self._shell_access_note()
        fulfillment_note = ""
        if self.fulfillment_mode and self.active_context.get("items"):
            action_examples = "`write_file`, `patch_file`, or `run_shell`" if self._shell_access_enabled() else "`write_file`, `patch_file`, or `show_diff`"
            fulfillment_note = textwrap.dedent(
                f"""
                IMPORTANT: An ACTIVE CONTEXT has been provided above. You are now
                in FULFILLMENT MODE: choose a concrete action to complete the
                requested change (for example {action_examples}) that uses the ACTIVE CONTEXT. Do NOT request
                further `history_expand` or `memory_expand` unless the ACTIVE
                CONTEXT explicitly indicates more expansion is required.

                Return a single JSON object with `thought` and `action`.
                """
            ).strip()

        steering_note = ""
        if self._has_steering():
            steering_note = textwrap.dedent(f"""
            STEERING PRIORITY:
            The following operator steering is active and should be treated as a high-priority policy overlay for this task:

            {self._steering_block()}

            You should prefer actions that satisfy this steering.
            If you cannot follow it yet, explain the specific blocker in `thought`.
            """).strip()

        readiness_note = ""
        if readiness.get("ready"):
            readiness_note = textwrap.dedent(
                f"""
                FULFILLMENT READINESS:
                {json.dumps(readiness, indent=2)}

                IMPORTANT:
                - Enough evidence exists to attempt a patch now.
                - Prefer editing the most likely target file over reading more adjacent files.
                - Only continue exploration if there is a specific blocker not resolved by ACTIVE CONTEXT or RELEVANT MEMORY.
                """
            ).strip()
        else:
            readiness_note = textwrap.dedent(f"""
            FULFILLMENT READINESS:
            {json.dumps(readiness, indent=2)}
            """
            ).strip()

        satisfaction_note = ""
        if self.task_satisfied:
            satisfaction_note = textwrap.dedent(f"""
            TASK SATISFACTION STATUS:
            {{
              "task_satisfied": true,
              "reason": {json.dumps(self.satisfaction_reason)},
              "post_satisfaction_checks": {self.post_satisfaction_checks}
            }}

            IMPORTANT:
            - The task appears satisfied already.
            - Prefer `finish` unless there is a specific contradictory signal.
            - Do not continue broad or repeated verification without a named blocker.
            """).strip()

        pending_verification_note = ""
        if self.pending_verification:
            pending_verification_note = textwrap.dedent(f"""
            PATCH VERIFICATION STATUS:
            {json.dumps(self.pending_verification, indent=2)}

            Host policy: writes to this path are blocked until verification clears.
            A verification read for that same path is validation-only and does not require a separate fact-resolution step.

            INNER STRATEGY:
            1. Read or diff the edited path to confirm the landed change.
            2. Check that the intended mutation is present and the old state is gone.
            3. Finish if the goal is complete, or make one final correction only after verification clears.
            """).strip()

        patch_recovery_note = ""
        if self.pending_patch_recovery:
            patch_recovery_note = textwrap.dedent(f"""
            PATCH RECOVERY MODE:
            {json.dumps(self.pending_patch_recovery, indent=2)}

            Host policy: do not continue mutating or broad exploration until this failed edit is reconciled.

            INNER STRATEGY:
            1. Read the affected path to refresh exact file state.
            2. Inspect git_diff, show_diff, or review_changes before trying another edit.
            3. If the branch is still valid, make one corrected edit after recovery clears.
            4. If the branch is wrong, use drop_context and restart from fresh reads.
            """).strip()

        edit_batch_note = ""
        if self.edit_batch_mode:
            edit_batch_note = textwrap.dedent(f"""
            EDIT BATCH MODE:
            {{
              "active": true,
              "pending_paths": {json.dumps(sorted(self.edit_batch_pending.keys()))},
              "pending_count": {len(self.edit_batch_pending)}
            }}

            Host policy: related writes may continue without per-write read blockers while this mode is active.
            When the related edits are done, call `end_edit_batch` so the host can verify all queued files in one read batch.

            INNER STRATEGY:
            1. Keep related edits tight and avoid unrelated exploration.
            2. Finish the clustered writes, then call `end_edit_batch`.
            3. Use the host verification batch to decide whether to finish or make one small follow-up.
            """).strip()

        active_error_note = ""
        if self.active_error is not None:
            active_error_note = textwrap.dedent(f"""
            ACTIVE ERROR STATUS:
            {{
              "task": {json.dumps(self.active_error.task)},
              "action_type": {json.dumps(self.active_error.action_type)},
              "error_type": {json.dumps(self.active_error.error_type)},
              "message": {json.dumps(self.active_error.message)},
              "path": {json.dumps(self.active_error.path)},
              "step": {self.active_error.step}
            }}

            IMPORTANT:
            - This error is still considered active until a matching follow-up succeeds.
            - Prefer a concrete resolution or validation action that addresses this specific failure.
            - Once a matching follow-up succeeds, the host will clear this error state.
            """).strip()

        output_format_note = ""
        if isinstance(self.output_format_recovery, dict):
            output_format_note = textwrap.dedent(f"""
            OUTPUT FORMAT RECOVERY:
            {json.dumps(self.output_format_recovery, indent=2)}

            IMPORTANT:
            - The previous model turn was ignored because it was not a valid action object.
            - Do not repeat the prose plan or numbered checklist from that turn.
            - Return exactly one JSON object and nothing else.
            - Put brief reasoning in `thought`, then emit an executable `action` or `actions`.
            - Valid immediate examples:
              {{"thought":"Apply the Dashboard and sidebar integration UI updates.","action":{{"type":"patch_file","path":"src/pages/Dashboard.tsx","search":"old exact text","replace":"new exact text"}}}}
              {{"thought":"Batch the Dashboard and sidebar updates, then verify.","actions":[{{"type":"begin_edit_batch"}},{{"type":"patch_file","path":"src/pages/Dashboard.tsx","search":"old exact text","replace":"new exact text"}},{{"type":"patch_file","path":"src/components/SmartSidebar.tsx","search":"old exact text","replace":"new exact text"}},{{"type":"end_edit_batch"}}]}}
            """).strip()

        fact_discipline_note = ""
        recent_exploration = self._recent_exploration_actions(window=6)
        if recent_exploration:
            fact_discipline_note = textwrap.dedent(f"""
            FACT DISCIPLINE:
            {{
              "recent_exploration_actions": {len(recent_exploration)},
              "fact_count": {len(self.fact_map)}
            }}

            IMPORTANT:
            - Your tool results are private to this goal execution. The planner and other goals CANNOT see what you read or discovered.
            - The ONLY way to pass findings forward is to record them with `set_fact` or `update_fact`.
            - If you discovered something concrete (file locations, config state, presence/absence of a pattern), record it as a fact before doing more exploration or finishing.
            - When a read/search/inspection answers a question, prefer `set_fact` before doing more discovery.
            - When a fact changes or needs correction, prefer `update_fact` and then finish or move on.
            - Repeating the same search conclusion without updating FACT CONTEXT is a failure mode.
            - An empty FACT CONTEXT after several meaningful discovery actions usually means you should record a fact now.
            - Good repo facts are concise and durable across future tasks.
            - Bad repo facts include line numbers, step-by-step plans, risks, and broad run summaries.
            - Every exploration must produce a fact. If the finding refines an existing fact, use update_fact.
            """).strip()

        completion_check_note = ""
        if self.completion_check_pending:
            completion_check_note = textwrap.dedent(f"""
            COMPLETION CHECK:
            {{
              "pending": true,
              "reason": {json.dumps(self.completion_check_reason)}
            }}

                        Host policy: only one concrete validation step or `finish` is allowed while this check is active.

            INNER STRATEGY:
            1. Run one concrete validation action tied to the recent mutation.
            2. Finish immediately if that validation passes.
            3. If it contradicts completion, make one corrective edit and continue normally.
            """).strip()

        discovery_budget_note = ""
        if self.discovery_budget is not None:
            discovery_budget_note = textwrap.dedent(f"""
            DISCOVERY BUDGET:
            {{
              "mode": {json.dumps(self.discovery_budget.mode_label)},
              "tool_calls_used": {self.discovery_budget.tool_calls_used},
              "tool_calls_max": {self.discovery_budget.max_tool_calls},
              "tool_calls_remaining": {self.discovery_budget.remaining_tool_calls},
              "budget_exhausted": {str(self.discovery_budget.exhausted).lower()}
            }}

                        Host policy: discovery is read-only, every tool-backed action counts against this budget, and exhaustion blocks further tool use until `finish`.
            """).strip()

        skills_note = ""
        if self._registered_skills:
            skills_preview = [
                {
                    "name": skill.name,
                    "description": skill.description,
                    "category": skill.category,
                    "priority": skill.priority,
                    "tags": skill.tags,
                }
                for skill in sorted(self._registered_skills.values(), key=lambda item: (-item.priority, item.name))[:12]
            ]
            skills_note = textwrap.dedent(f"""
            AVAILABLE SKILLS:
            {json.dumps(skills_preview, indent=2)}

            Host policy: `skill` is a zero-side-effect read action. Use it to list available skills or load a named skill payload into context when it directly matches the task.
            """).strip()

        return textwrap.dedent(f"""
        TASK:
        {task}

        OPERATOR STEERING:
        {self._steering_block()}

        {steering_note}

        ROOT:
        {self.root}

        REPO SNAPSHOT:
        {self._repo_snapshot()}

        RELEVANT MEMORY:
        {json.dumps(memory_bundle, indent=2)}

        RECENT EXECUTION SUMMARY:
        {self._history_snapshot()}

        ACTIVE CONTEXT:
        {active_block}

        FACT CONTEXT:
        {self._fact_context_block()}

        IMMEDIATE RESOLUTION HANDOFF:
        {self._recent_resolution_handoff_block()}

        SELECTED GOAL FACTS:
        {selected_goal_facts_block}

        {fulfillment_note}

        {readiness_note}

        {satisfaction_note}

        {pending_verification_note}

        {patch_recovery_note}

        {edit_batch_note}

        {active_error_note}

        {output_format_note}

        {fact_discipline_note}

        {completion_check_note}

        {discovery_budget_note}

        {skills_note}

        {shell_access_note}

        Notes:
        - Memory items in RELEVANT MEMORY can be expanded with action type "memory_expand" using their "id".
        - Prior steps in RECENT EXECUTION SUMMARY can be expanded with action type "history_expand" using their "step".
        - FACT CONTEXT is a durable host-side map of facts the model previously recorded with set_fact or update_fact.
        - IMMEDIATE RESOLUTION HANDOFF is the most recent set_fact or update_fact outcome from this run and must be treated as the just-resolved conclusion for the next action.
        - If IMMEDIATE RESOLUTION HANDOFF is present, use it directly instead of re-reading or re-deriving the same conclusion.
        - SELECTED GOAL FACTS is a planner-prioritized subset of durable facts for this goal only.
        - Use SELECTED GOAL FACTS first when it is present, but you may still use other facts from FACT CONTEXT if execution reveals a missing dependency or hidden constraint.
        - previous_turn_facts were last updated in an earlier run and may still be useful background.
        - current_run_facts were updated during this active execution and should drive the immediate next action.
        - A separate fact subagent may record `set_fact` or `update_fact` after repeated exploration bursts using recent tool thoughts and results.
        - Expand first when you need exact details before the next tool call.
        - If a current problem anchor has resolved to no goal progress and no durable fact, stop repeating that branch and prefer `finish` with a summary that explicitly says the goal planner needs a change of strategy.
        - If EDIT BATCH MODE is active, keep the related mutations tight and then call `end_edit_batch` instead of interleaving repeated verification reads after each edit. Same-file mutation budget limits are deferred until batch exit.
        - The host may run automatic language diagnostics after successful mutation actions or batch verification. Treat matching compiler errors surfaced in ACTIVE ERROR as concrete post-mutation feedback, not exploratory noise.
        - Before choosing `read_file`, decide the 2-4 most important questions that read should answer.
        - If the target looks like a directory or the next question is about repo/file topology, prefer `list_files` before `read_file` so you ground the path structure first.
        - After a `read_file`, use the result to update several conclusions at once when possible: likely edit location, constraints, validation path, and whether more exploration is still necessary.
        - Avoid spending a full read on a single narrow detail if the same token budget could answer a broader next-step decision.
        - If the same file already took two successful mutation actions in this run, stop incremental edit accretion and use diff/review output to decide whether one final reconciler edit is justified.
        - Emit `patch_file`, `replace_snippet`, or `replace_range` only when you can fill concrete grounded fields from repo context. If any edit anchor is still uncertain, do a `read_file` first instead of guessing.

        Produce the next best action as JSON. Prefer a single `action`. Use `actions` only for a short ordered batch when the steps are tightly coupled.
        """).strip()

    def _error_action_result(self, name: Any, payload: Dict[str, Any]) -> ActionResult:
        return ActionResult(ok=False, name=str(name), payload=payload)

    def _extract_actions_from_model_object(self, obj: Dict[str, Any]) -> Tuple[Optional[List[Dict[str, Any]]], Optional[ActionResult]]:
        action = obj.get("action")
        actions = obj.get("actions")

        if action is not None and actions is not None:
            return None, self._error_action_result(
                "schema_error",
                {
                    "error": "Provide either action or actions, not both.",
                    "object": obj,
                },
            )

        if actions is not None:
            if not isinstance(actions, list) or not actions:
                return None, self._error_action_result(
                    "schema_error",
                    {
                        "error": "actions must be a non-empty list.",
                        "object": obj,
                    },
                )
            if len(actions) > MAX_ACTION_BATCH_SIZE:
                return None, self._error_action_result(
                    "schema_error",
                    {
                        "error": f"actions may contain at most {MAX_ACTION_BATCH_SIZE} items.",
                        "count": len(actions),
                    },
                )

            normalized_actions: List[Dict[str, Any]] = []
            finish_seen = False
            for index, item in enumerate(actions, start=1):
                if not isinstance(item, dict):
                    return None, self._error_action_result(
                        "schema_error",
                        {
                            "error": f"actions[{index - 1}] must be an object.",
                            "object": item,
                        },
                    )
                action_type = str(item.get("type", "") or "").strip()
                if not action_type:
                    return None, self._error_action_result(
                        "schema_error",
                        {
                            "error": f"actions[{index - 1}] is missing type.",
                            "object": item,
                        },
                    )
                if action_type == "finish":
                    if finish_seen or index != len(actions):
                        return None, self._error_action_result(
                            "schema_error",
                            {
                                "error": "finish may appear at most once and only as the last item in actions.",
                                "count": len(actions),
                            },
                        )
                    finish_seen = True
                normalized_actions.append(dict(item))
            return normalized_actions, None

        if not isinstance(action, dict) or "type" not in action:
            return None, self._error_action_result(
                "schema_error",
                {"error": "Missing action.type", "object": obj},
            )

        return [dict(action)], None

    def _finalize_tool_action(
        self,
        *,
        action_type: str,
        payload: Dict[str, Any],
        success_summary: str,
        failure_summary: str,
        next_hint: Optional[str] = None,
        extra_data: Optional[Dict[str, Any]] = None,
    ) -> ActionResult:
        outcome = self._normalize_tool_outcome(
            action_type=action_type,
            tool_result=payload,
            success_summary=success_summary,
            failure_summary=failure_summary,
            next_hint=next_hint,
        )
        if outcome.ok and outcome.data is not None and extra_data:
            outcome.data.update(extra_data)
        return self._action_result_from_outcome(outcome)

    def _try_activate_context(self, item: Any, label: str) -> None:
        try:
            self._set_active_context_item(cast(Dict[str, Any], item), label)
            self.fulfillment_mode = True
        except Exception:
            pass

    def _clear_execution_context_state(self) -> None:
        try:
            self._clear_active_context()
            self._clear_patch_recovery()
            self._clear_edit_batch_state()
            self._clear_pending_verification()
            self._clear_pending_fact_resolution()
            self._clear_active_error()
        except Exception:
            pass

    def _handle_verification_block(self, action: Dict[str, Any], action_type: Any) -> Optional[ActionResult]:
        verification_block = self._verification_blocks_patch(action)
        if verification_block is None:
            return None
        return self._error_action_result(action_type, verification_block)

    def _patch_recovery_blocks_action(self, action: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.pending_patch_recovery:
            return None

        recovery_task = str(self.pending_patch_recovery.get("task", "") or "")
        if recovery_task and recovery_task != self._current_task:
            self._clear_patch_recovery()
            return None

        action_type = str(action.get("type", "") or "")
        allowed_actions = {"read_file", "git_diff", "show_diff", "review_changes", "drop_context"}
        if action_type in allowed_actions:
            return None

        path = str(self.pending_patch_recovery.get("path", "") or "") or "the affected file"
        return {
            "code": "PATCH_RESOLUTION_ACTIVE",
            "message": (
                f"Patch resolution is active for {path}. Do not continue mutating or exploring yet. "
                "Resolve the failed patch with read_file, git_diff, show_diff, review_changes, or drop_context first."
            ),
            "suggested_next_actions": self._runtime_suggested_next_actions(),
        }

    def _handle_patch_recovery_block(self, action: Dict[str, Any], action_type: Any) -> Optional[ActionResult]:
        recovery_block = self._patch_recovery_blocks_action(action)
        if recovery_block is None:
            return None
        return self._error_action_result(action_type, recovery_block)

    def _fact_resolution_blocks_action(self, action: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        return None

    def _handle_fact_resolution_block(self, action: Dict[str, Any], action_type: Any) -> Optional[ActionResult]:
        resolution_block = self._fact_resolution_blocks_action(action)
        if resolution_block is None:
            return None
        return self._error_action_result(action_type, resolution_block)

    def _set_output_format_recovery(self, *, error_type: str, message: str, raw: str = "") -> None:
        self.output_format_recovery = {
            "error_type": str(error_type or "output_format").strip() or "output_format",
            "message": str(message or "").strip(),
            "raw_excerpt": shorten(str(raw or "").strip(), 1000),
            "step": len(self.history) + 1,
        }

    def _clear_output_format_recovery(self) -> None:
        self.output_format_recovery = None

    def _recent_exploration_burst(self, window: int = 10) -> List[AgentStep]:
        burst: List[AgentStep] = []
        for step in reversed(self.history[-window:]):
            action_type = str(step.action.get("type", "") or "")
            if action_type in FACT_ACTION_TYPES:
                break
            if not step.result.ok:
                break
            if action_type not in EXPLORATION_ACTION_TYPES:
                break
            burst.append(step)
        burst.reverse()
        return burst

    def _fact_subagent_result_excerpt(self, value: Any, depth: int = 0) -> Any:
        if depth >= 3:
            if isinstance(value, str):
                return shorten(value, 400)
            return str(value)
        if isinstance(value, str):
            return shorten(value, 1200)
        if isinstance(value, list):
            items = [self._fact_subagent_result_excerpt(item, depth + 1) for item in value[:6]]
            if len(value) > 6:
                items.append(f"... ({len(value) - 6} more items)")
            return items
        if isinstance(value, dict):
            out: Dict[str, Any] = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 10:
                    out["..."] = f"{len(value) - 10} more fields"
                    break
                out[str(key)] = self._fact_subagent_result_excerpt(item, depth + 1)
            return out
        return value

    def _fact_subagent_recent_events(self, steps: List[AgentStep]) -> List[Dict[str, Any]]:
        events: List[Dict[str, Any]] = []
        for step in steps:
            events.append(
                {
                    "step": step.step,
                    "thought": step.thought,
                    "action": step.action,
                    "result": self._fact_subagent_result_excerpt(step.result.payload),
                }
            )
        return events

    def _normalize_fact_subagent_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(action)
        action_type = str(normalized.get("type", "") or "").strip()
        key = str(normalized.get("key", "") or "").strip()
        value = str(normalized.get("value", normalized.get("resolution", "")) or "").strip()

        if action_type == "set_fact" and key and self.issue_ledger.find_fact(key) is not None:
            normalized["type"] = "update_fact"
        elif action_type == "update_fact" and key and self.issue_ledger.find_fact(key) is None:
            normalized["type"] = "set_fact"

        normalized.setdefault("agent", "fact_subagent")
        normalized.setdefault("auto", True)
        if str(normalized.get("type", "") or "").strip() in {"set_fact", "update_fact"}:
            raw_type = str(normalized.get("fact_type", "") or "").strip().lower()
            if raw_type not in {FACT_TYPE_GOAL, FACT_TYPE_ARCHITECTURE}:
                raw_type = FACT_TYPE_GOAL
            normalized["fact_type"] = raw_type

        normalized_type = str(normalized.get("type", "") or "").strip()
        if normalized_type in {"set_fact", "update_fact"}:
            if not key or not value:
                normalized["type"] = "set_fact"
                normalized["key"] = key or "exploration_finding"
                normalized["value"] = value or "Recent exploration produced context for this task."
                normalized["fact_type"] = FACT_TYPE_GOAL
        return normalized

    def _should_run_fact_subagent_for_step(self, step: AgentStep) -> bool:
        action_type = str(step.action.get("type", "") or "")
        if not step.result.ok or action_type not in EXPLORATION_ACTION_TYPES:
            return False
        payload = step.result.payload if isinstance(step.result.payload, dict) else {}
        if bool(payload.get("verification_read")) or bool(payload.get("verification_cleared")):
            return False
        return len(self._recent_exploration_burst()) >= 4

    def _run_fact_subagent(self, task: str, trigger_step: AgentStep) -> Optional[AgentStep]:
        if not self._should_run_fact_subagent_for_step(trigger_step):
            return None

        recent_steps = self._recent_exploration_burst(window=10)
        recent_events = self._fact_subagent_recent_events(recent_steps)
        started = time.time()
        decision = self.fact_subagent.decide(
            task=task,
            fact_context=self._fact_context_block(),
            selected_goal_facts=self._selected_goal_facts_block(),
            recent_events=recent_events,
        )
        duration = time.time() - started
        self._record_model_turn_metrics(step_num=len(self.history) + 1, duration_s=duration)
        if decision is None:
            try:
                self._add_active_note("Fact subagent could not derive a valid fact action from recent exploration.")
            except Exception:
                pass
            return None

        action = self._normalize_fact_subagent_action(decision.action)
        prior_thought = self._current_thought
        self._current_thought = decision.thought
        result = self._execute_action(action)
        self._current_thought = prior_thought
        if not result.ok:
            self._set_active_error(action, result)

        step = AgentStep(
            step=len(self.history) + 1,
            thought=decision.thought,
            action=action,
            result=result,
            elapsed_s=duration,
            run_id=self._active_run_id,
        )
        self.history.append(step)
        if self._run_metrics:
            self._run_metrics["steps"] = int(self._run_metrics.get("steps", 0)) + 1
            if result.ok:
                self._run_metrics["successful_actions"] = int(self._run_metrics.get("successful_actions", 0)) + 1
            else:
                self._run_metrics["failed_actions"] = int(self._run_metrics.get("failed_actions", 0)) + 1

        self._ingest_memory(
            task=task,
            thought=decision.thought,
            action=action,
            result=result,
        )
        self._print_step(step)
        return step

    def _handle_list_files_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        path = str(action.get("path", "."))
        if self._recent_list_files_count(window=8) >= 4 and self._same_list_files_path_count(path, window=8) >= 2:
            return self._error_action_result(action_type, self._exploration_loop_payload(path))

        limit = str(int(action.get("limit", 200)))
        args = ["--path", path, "--limit", limit]
        if action.get("recursive"):
            args.append("--recursive")
        if action.get("max_depth") is not None:
            args += ["--max-depth", str(int(action["max_depth"]))]
        if action.get("glob"):
            args += ["--glob", str(action["glob"])]
        if action.get("hidden"):
            args.append("--hidden")
        payload = self._call_tool("ls", *args, action=action)
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary=f"Listed files under {path}",
            failure_summary=f"Could not list files under {path}",
        )

    def _maybe_complete_pending_verification(
        self,
        action: Dict[str, Any],
        result_payload: Dict[str, Any],
    ) -> bool:
        if not self.pending_verification:
            return False
        if str(action.get("path", "")) != str(self.pending_verification.get("path", "")):
            return False
        if str(self.pending_verification.get("task", "")) != self._current_task:
            return False

        verified, verification_basis = self._verification_item_matches(self.pending_verification, result_payload)

        if verified:
            self._clear_pending_verification()
            try:
                self._add_active_note(
                    f"Pending verification for {action['path']} is resolved via {verification_basis or 'file confirmation'}."
                )
            except Exception:
                pass
            return True
        return False

    def _maybe_complete_patch_recovery(self, action: Dict[str, Any], result_payload: Dict[str, Any]) -> bool:
        if not self.pending_patch_recovery:
            return False

        recovery_task = str(self.pending_patch_recovery.get("task", "") or "")
        if recovery_task and recovery_task != self._current_task:
            self._clear_patch_recovery()
            return False

        action_type = str(action.get("type", "") or "")
        recovery_path = str(self.pending_patch_recovery.get("path", "") or "")
        action_path = str(action.get("path", "") or "")
        resolved = False
        basis = ""

        if action_type == "read_file":
            resolved = bool(action_path and action_path == recovery_path)
            basis = "file reread"
        elif action_type == "git_diff":
            resolved = not action_path or action_path == recovery_path
            basis = "git diff review"
        elif action_type == "show_diff":
            resolved = True
            basis = "workspace diff review"
        elif action_type == "review_changes":
            resolved = not action_path or action_path == recovery_path
            basis = "change review"

        if not resolved:
            return False

        self._clear_patch_recovery()
        try:
            self._add_active_note(
                f"Patch recovery for {recovery_path or action_path or 'the affected file'} was resolved via {basis}."
            )
        except Exception:
            pass
        return True

    def _verification_item_matches(
        self,
        item: Dict[str, Any],
        result_payload: Dict[str, Any],
    ) -> Tuple[bool, str]:
        content = result_payload.get("content")
        content_sha256 = str(result_payload.get("sha256", "") or "")
        replace_text = item.get("replace")
        search_text = item.get("search")
        expected_sha256 = str(item.get("expected_sha256", "") or "")
        verification_mode = str(item.get("mode", "contains") or "contains")
        verified = False
        verification_basis = ""
        if expected_sha256 and content_sha256:
            verified = content_sha256 == expected_sha256
            verification_basis = "sha256 match"
        if isinstance(content, str) and isinstance(replace_text, str):
            if not verified and verification_mode == "exact_content":
                verified = content == replace_text
                verification_basis = "exact content match"
            elif not verified and verification_mode == "contains":
                if isinstance(search_text, str) and search_text:
                    verified = search_text not in content and replace_text in content
                    verification_basis = "search removed and replacement present"
                elif replace_text:
                    verified = replace_text in content
                    verification_basis = "replacement text present"
        return verified, verification_basis

    def _read_targets_pending_verification(self, action: Dict[str, Any]) -> bool:
        if not self.pending_verification:
            return False
        if str(action.get("type", "") or "") != "read_file":
            return False
        if str(self.pending_verification.get("task", "") or "") != self._current_task:
            return False
        return str(action.get("path", "") or "") == str(self.pending_verification.get("path", "") or "")

    def _handle_read_file_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        path = str(action["path"])
        args = ["--path", path]
        if action.get("start_line") is not None:
            args += ["--start-line", str(int(action["start_line"]))]
        if action.get("end_line") is not None:
            args += ["--end-line", str(int(action["end_line"]))]
        payload = self._call_tool("read", *args, action=action)
        verification_cleared = False
        patch_recovery_cleared = False
        verification_read = self._read_targets_pending_verification(action)
        if payload.get("ok"):
            result_payload = payload.get("data") or {}
            self._try_activate_context(result_payload, f"read:{action.get('path')}")
            if isinstance(result_payload, dict):
                verification_cleared = self._maybe_complete_pending_verification(action, result_payload)
            patch_recovery_cleared = self._maybe_complete_patch_recovery(action, result_payload)

        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary=f"Read file {path}",
            failure_summary=f"Could not read file {path}",
            next_hint="patch_file" if payload.get("ok") else None,
            extra_data={
                "verification_read": verification_read,
                "verification_cleared": verification_cleared,
                "patch_recovery_cleared": patch_recovery_cleared,
                "verification_message": (
                    f"Pending verification for {path} was cleared after deterministic file confirmation."
                    if verification_cleared else None
                ),
                "patch_recovery_message": (
                    f"Patch recovery for {path} was cleared after deterministic file confirmation."
                    if patch_recovery_cleared else None
                ),
            },
        )

    def _inspect_files_payload(self, files: List[Dict[str, Any]], action: Dict[str, Any]) -> Dict[str, Any]:
        tmp_path = self.root / ".agent_tmp_inspect.json"
        tmp_path.write_text(json.dumps({"files": files, "include_content": True}, indent=2), encoding="utf-8")
        try:
            return self._call_tool("inspect", "--spec-file", str(tmp_path), action=action)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _handle_inspect_files_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        files = action.get("files")
        if not isinstance(files, list) or not files:
            return self._error_action_result(
                action_type,
                {
                    "code": "BAD_REQUEST",
                    "message": "inspect_files requires a non-empty files list.",
                    "example": {
                        "type": "inspect_files",
                        "files": [
                            {"path": "src/main.py", "start_line": 120, "end_line": 220},
                            {"path": "src/utils.py"},
                        ],
                    },
                },
            )

        payload = self._inspect_files_payload(cast(List[Dict[str, Any]], files), action)
        if payload.get("ok"):
            self._try_activate_context(payload.get("data") or payload, "inspect_files")
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Inspected multiple files",
            failure_summary="Could not inspect requested files",
            next_hint="patch_file" if payload.get("ok") else None,
        )

    def _handle_summarize_files_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        args: List[str] = []
        if action.get("path"):
            args += ["--path", str(action["path"])]
        if action.get("glob"):
            args += ["--glob", str(action["glob"])]
        if action.get("limit") is not None:
            args += ["--limit", str(int(action["limit"]))]
        if action.get("hidden"):
            args.append("--hidden")
        if action.get("include_content"):
            args.append("--include-content")

        payload = self._call_tool("summarize", *args, action=action)
        if payload.get("ok"):
            self._try_activate_context(payload.get("data") or payload, f"summaries:{action.get('path') or action.get('glob') or '.'}")
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Summarized files",
            failure_summary="Could not summarize files",
            next_hint="read_file" if payload.get("ok") else None,
        )

    def _write_file_payload(self, rel_path: str, content: str, action: Dict[str, Any]) -> Dict[str, Any]:
        tmp_path = self.root / ".agent_tmp_write.txt"
        tmp_path.write_text(content, encoding="utf-8")
        try:
            return self._call_tool("write", "--path", rel_path, "--input-file", str(tmp_path), action=action)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()

    def _invalid_write_request_result(self, action_type: str) -> ActionResult:
        return self._error_action_result(
            action_type,
            {
                "code": "BAD_REQUEST",
                "message": "write_file requires non-empty `path` and `content`.",
                "example": {
                    "type": "write_file",
                    "path": "src/main.py",
                    "content": "full new file content",
                },
            },
        )

    def _invalid_patch_request_result(self, action_type: str) -> ActionResult:
        return self._error_action_result(
            action_type,
            {
                "code": "BAD_REQUEST",
                "message": "patch_file requires non-empty `path`, `search`, and `replace`.",
                "example": {
                    "type": "patch_file",
                    "path": "src/main.py",
                    "search": "old text",
                    "replace": "new text",
                    "all": False,
                },
                "suggested_next_actions": [
                    {"type": "read_file", "path": ".", "label": "Read the target file before patching"},
                    {"type": "write_file", "path": "src/main.py", "content": "full new file content", "label": "Use write_file if you intend to replace the full file"},
                ],
                "next_hint": "read_file",
            },
        )

    def _handle_write_file_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        rel_path = str(action.get("path", "") or "").strip()
        content_value = action.get("content")
        content = content_value if isinstance(content_value, str) else ""
        if not rel_path or not content:
            return self._invalid_write_request_result(action_type)
        repeated_mutation_result = self._repeated_mutation_guard_result(action_type, rel_path)
        if repeated_mutation_result is not None:
            return repeated_mutation_result
        payload = self._write_file_payload(rel_path, content, action)
        raw_data = payload.get("data")
        result_payload: Dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        if payload.get("ok"):
            self._invalidate_fast_caches()
            result_payload = dict(result_payload)
            result_payload["verification_pending"] = True
            result_payload["verification_hint"] = {
                "type": "read_file",
                "path": rel_path,
                "expect_equals": content,
                "expect_sha256": str(result_payload.get("sha256", "") or ""),
            }
            if self.edit_batch_mode:
                self._queue_edit_batch_verification(
                    path=rel_path,
                    search="",
                    replace=content,
                    mode="exact_content",
                    expected_sha256=str(result_payload.get("sha256", "") or "") or None,
                )
            else:
                self._set_pending_verification(
                    path=rel_path,
                    search="",
                    replace=content,
                    mode="exact_content",
                    expected_sha256=str(result_payload.get("sha256", "") or "") or None,
                )
        self._record_auto_edit_attempt_fact(
            action_type=action_type,
            path=rel_path,
            action=action,
            payload=payload,
        )

        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary=f"Wrote file {rel_path}",
            failure_summary=f"Could not write file {rel_path}",
            next_hint="read_file",
            extra_data=result_payload,
        )

    def _patch_loop_detected_result(self, action_type: str, path: str) -> ActionResult:
        self._disable_fulfillment_for_recovery(
            f"Patch loop detected for {path}. Switching out of fulfillment for recovery."
        )
        payload = {
            "code": "PATCH_LOOP_DETECTED",
            "message": (
                f"Repeated identical patch attempts detected for {path}. "
                "Do not retry the same patch. Refresh file state, inspect diff, "
                "or drop context."
            ),
            "suggested_next_actions": [
                {"type": "read_file", "path": path},
                {"type": "git_diff", "path": path},
                {"type": "show_diff"},
                {"type": "drop_context", "reason": "Repeated patch failure"},
            ],
        }
        self._set_patch_recovery(
            task=self._current_task,
            path=path,
            failed_action={"type": action_type},
            result_payload=payload,
        )
        return self._error_action_result(
            action_type,
            payload,
        )

    def _patch_tool_args(self, action: Dict[str, Any], path: str) -> List[str]:
        args = [
            "--path", path,
            "--search", str(action["search"]),
            "--replace", str(action["replace"]),
        ]
        if action.get("all"):
            args.append("--all")
        return args

    def _call_tool_with_temp_files(
        self,
        subcommand: str,
        args: List[str],
        *,
        action: Dict[str, Any],
        text_files: Optional[List[Tuple[str, str, str]]] = None,
        json_payload: Optional[Dict[str, Any]] = None,
        json_flag: str = "--spec-file",
    ) -> Dict[str, Any]:
        temp_paths: List[Path] = []
        final_args = list(args)
        try:
            for flag, content, suffix in text_files or []:
                tmp_path = self.root / f".agent_tmp_{subcommand}_{time.time_ns()}_{len(temp_paths)}{suffix}"
                tmp_path.write_text(content, encoding="utf-8")
                temp_paths.append(tmp_path)
                final_args += [flag, str(tmp_path)]
            if json_payload is not None:
                tmp_path = self.root / f".agent_tmp_{subcommand}_{time.time_ns()}_{len(temp_paths)}.json"
                tmp_path.write_text(json.dumps(json_payload, indent=2), encoding="utf-8")
                temp_paths.append(tmp_path)
                final_args += [json_flag, str(tmp_path)]
            return self._call_tool(subcommand, *final_args, action=action)
        finally:
            for temp_path in temp_paths:
                if temp_path.exists():
                    temp_path.unlink()

    def _mutation_result_data(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw_data = payload.get("data")
        return raw_data if isinstance(raw_data, dict) else {}

    def _mutation_after_hash(self, payload: Dict[str, Any]) -> str:
        data = self._mutation_result_data(payload)
        if isinstance(data.get("sha256"), str) and data.get("sha256"):
            return str(data.get("sha256") or "")
        mutation = data.get("mutation") if isinstance(data.get("mutation"), dict) else data
        if isinstance(mutation, dict):
            return str(mutation.get("after_hash", "") or "")
        return ""

    def _mutation_result_path(self, action: Dict[str, Any], payload: Dict[str, Any]) -> str:
        action_type = str(action.get("type", "") or "")
        if action_type == "rename_file":
            return str(action.get("new_path", "") or "").strip()
        if action_type == "copy_file":
            return str(action.get("destination_path", "") or "").strip()
        data = self._mutation_result_data(payload)
        path = str(data.get("path", "") or data.get("file_path", "") or action.get("path", "") or "").strip()
        mutation = data.get("mutation") if isinstance(data.get("mutation"), dict) else {}
        if not path and isinstance(mutation, dict):
            path = str(mutation.get("file_path", "") or "").strip()
        return path

    def _update_generic_mutation_verification_state(self, action: Dict[str, Any], payload: Dict[str, Any]) -> Dict[str, Any]:
        result_payload = self._mutation_result_data(payload)
        if not payload.get("ok"):
            return result_payload
        action_type = str(action.get("type", "") or "")
        if action_type in {"delete_file", "rename_file", "copy_file", "batch_mutate"}:
            return result_payload
        after_hash = self._mutation_after_hash(payload)
        verify_path = self._mutation_result_path(action, payload)
        if not verify_path or not after_hash:
            return result_payload
        result_payload = dict(result_payload)
        result_payload["verification_pending"] = True
        result_payload["verification_hint"] = {
            "type": "read_file",
            "path": verify_path,
            "expect_sha256": after_hash,
        }
        if self.edit_batch_mode:
            self._queue_edit_batch_verification(
                path=verify_path,
                search="",
                replace="",
                mode="contains",
                expected_sha256=after_hash,
            )
        else:
            self._set_pending_verification(
                path=verify_path,
                search="",
                replace="",
                mode="contains",
                expected_sha256=after_hash,
            )
        return result_payload

    def _finalize_generic_mutation_action(
        self,
        *,
        action: Dict[str, Any],
        payload: Dict[str, Any],
        success_summary: str,
        failure_summary: str,
        record_path: Optional[str] = None,
        enable_patch_recovery: bool = True,
    ) -> ActionResult:
        action_type = str(action.get("type", "") or "")
        path = record_path or self._mutation_result_path(action, payload)
        result_payload = self._update_generic_mutation_verification_state(action, payload)
        if payload.get("ok"):
            self._invalidate_fast_caches()
            self._clear_patch_recovery()
        elif enable_patch_recovery and path:
            self._set_patch_recovery(
                task=self._current_task,
                path=path,
                failed_action=action,
                result_payload=result_payload or payload,
            )
        if path:
            self._record_auto_edit_attempt_fact(
                action_type=action_type,
                path=path,
                action=action,
                payload=payload,
            )
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary=success_summary,
            failure_summary=failure_summary,
            next_hint="read_file",
            extra_data=result_payload,
        )

    def _handle_explicit_mutation_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type", "") or "")
        path = str(action.get("path", "") or "").strip()
        if path:
            repeated_mutation_result = self._repeated_mutation_guard_result(action_type, path)
            if repeated_mutation_result is not None:
                return repeated_mutation_result

        args: List[str] = []
        text_files: List[Tuple[str, str, str]] = []
        subcommand = action_type.replace("_", "-")

        if action_type in {"replace_range", "delete_range"}:
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": f"{action_type} requires a non-empty path."})
            args = ["--path", path, "--start-line", str(int(action.get("start_line", 0))), "--end-line", str(int(action.get("end_line", 0)))]
            if action_type == "replace_range":
                text_files.append(("--input-file", str(action.get("new_text", "") or ""), ".txt"))
        elif action_type in {"replace_snippet", "delete_snippet"}:
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": f"{action_type} requires a non-empty path."})
            args = ["--path", path]
            if action_type == "replace_snippet":
                text_files.append(("--old-file", str(action.get("old_text", "") or ""), ".txt"))
                text_files.append(("--new-file", str(action.get("new_text", "") or ""), ".txt"))
                if action.get("expected_occurrences") is not None:
                    args += ["--expected-occurrences", str(int(action.get("expected_occurrences", 1)))]
                if action.get("all"):
                    args.append("--all")
            else:
                text_files.append(("--input-file", str(action.get("text", "") or ""), ".txt"))
                if action.get("expected_occurrences") is not None:
                    args += ["--expected-occurrences", str(int(action.get("expected_occurrences", 1)))]
        elif action_type in {"insert_before", "insert_after"}:
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": f"{action_type} requires a non-empty path."})
            args = ["--path", path]
            text_files.append(("--anchor-file", str(action.get("anchor_text", "") or ""), ".txt"))
            text_files.append(("--input-file", str(action.get("new_text", "") or ""), ".txt"))
            if action.get("expected_occurrences") is not None:
                args += ["--expected-occurrences", str(int(action.get("expected_occurrences", 1)))]
        elif action_type in {"append_block", "prepend_block"}:
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": f"{action_type} requires a non-empty path."})
            args = ["--path", path]
            text_files.append(("--input-file", str(action.get("new_text", "") or ""), ".txt"))
        elif action_type == "replace_symbol":
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "replace_symbol requires a non-empty path."})
            symbol_name = str(action.get("symbol_name", "") or "").strip()
            symbol_kind = str(action.get("symbol_kind", "") or "").strip()
            if not symbol_name or not symbol_kind:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "replace_symbol requires symbol_name and symbol_kind."})
            args = ["--path", path, "--symbol-name", symbol_name, "--symbol-kind", symbol_kind]
            text_files.append(("--input-file", str(action.get("new_text", "") or ""), ".txt"))
        elif action_type == "insert_symbol_member":
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "insert_symbol_member requires a non-empty path."})
            container_symbol = str(action.get("container_symbol", "") or "").strip()
            if not container_symbol:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "insert_symbol_member requires container_symbol."})
            args = ["--path", path, "--container-symbol", container_symbol, "--position", str(action.get("position", "end") or "end")]
            text_files.append(("--input-file", str(action.get("member_text", "") or ""), ".txt"))
        elif action_type == "rename_symbol":
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "rename_symbol requires a non-empty path."})
            old_name = str(action.get("old_name", "") or "").strip()
            new_name = str(action.get("new_name", "") or "").strip()
            if not old_name or not new_name:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "rename_symbol requires old_name and new_name."})
            args = ["--path", path, "--old-name", old_name, "--new-name", new_name, "--scope", str(action.get("scope", "file") or "file")]
        elif action_type == "move_block":
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "move_block requires a non-empty path."})
            args = [
                "--path", path,
                "--start-line", str(int(action.get("start_line", 0))),
                "--end-line", str(int(action.get("end_line", 0))),
                "--position", str(action.get("position", "after") or "after"),
            ]
            text_files.append(("--anchor-file", str(action.get("destination_anchor", "") or ""), ".txt"))
        elif action_type == "create_file":
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "create_file requires a non-empty path."})
            args = ["--path", path]
            if action.get("overwrite"):
                args.append("--overwrite")
            text_files.append(("--input-file", str(action.get("content", "") or ""), ".txt"))
        elif action_type == "delete_file":
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "delete_file requires a non-empty path."})
            args = ["--path", path]
        elif action_type == "rename_file":
            old_path = str(action.get("old_path", "") or "").strip()
            new_path = str(action.get("new_path", "") or "").strip()
            if not old_path or not new_path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "rename_file requires old_path and new_path."})
            args = ["--old-path", old_path, "--new-path", new_path]
            path = old_path
        elif action_type == "copy_file":
            source_path = str(action.get("source_path", "") or "").strip()
            destination_path = str(action.get("destination_path", "") or "").strip()
            if not source_path or not destination_path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "copy_file requires source_path and destination_path."})
            args = ["--source-path", source_path, "--destination-path", destination_path]
            if action.get("overwrite"):
                args.append("--overwrite")
            path = destination_path
        elif action_type == "fill_template":
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "fill_template requires a non-empty path."})
            slots = action.get("slots")
            if not isinstance(slots, dict):
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "fill_template requires slots as an object."})
            payload = self._call_tool_with_temp_files(
                subcommand,
                ["--path", path],
                action=action,
                json_payload=cast(Dict[str, Any], slots),
                json_flag="--slots-file",
            )
            return self._finalize_generic_mutation_action(
                action=action,
                payload=payload,
                success_summary=f"Filled template {path}",
                failure_summary=f"Could not fill template {path}",
                record_path=path,
            )
        else:
            return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": f"Unsupported mutation action: {action_type}"})

        payload = self._call_tool_with_temp_files(subcommand, args, action=action, text_files=text_files)
        return self._finalize_generic_mutation_action(
            action=action,
            payload=payload,
            success_summary=f"Completed {action_type} for {path or self._mutation_result_path(action, payload) or 'mutation target'}",
            failure_summary=f"Could not complete {action_type}",
            record_path=path,
        )

    def _handle_batch_mutate_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type", "") or "")
        operations = action.get("operations")
        if not isinstance(operations, list) or not operations:
            return self._error_action_result(
                action_type,
                {
                    "code": "BAD_REQUEST",
                    "message": "batch_mutate requires a non-empty operations list.",
                    "example": {
                        "type": "batch_mutate",
                        "atomic": True,
                        "operations": [
                            {"type": "replace_snippet", "path": "src/main.py", "old_text": "old", "new_text": "new"},
                            {"type": "append_block", "path": "src/main.py", "new_text": "\nprint('done')\n"},
                        ],
                    },
                },
            )
        payload = self._call_tool_with_temp_files(
            "batch-mutate",
            ["--atomic"] if bool(action.get("atomic")) else [],
            action=action,
            json_payload={"atomic": bool(action.get("atomic")), "operations": operations},
        )
        if payload.get("ok"):
            self._invalidate_fast_caches()
            self._clear_patch_recovery()
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Completed batch mutate",
            failure_summary="Could not complete batch mutate",
            next_hint="review_changes",
            extra_data=self._mutation_result_data(payload),
        )

    def _update_patch_verification_state(
        self,
        action: Dict[str, Any],
        path: str,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        raw_data = payload.get("data")
        result_payload: Dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        if not payload.get("ok"):
            return result_payload

        if result_payload.get("status") == "already_applied":
            return result_payload
        if int(result_payload.get("replacements", 0) or 0) <= 0:
            return result_payload

        result_payload["verification_pending"] = True
        result_payload["verification_hint"] = {
            "type": "read_file",
            "path": path,
            "expect_contains": str(action.get("replace", "")),
            "expect_sha256": str(result_payload.get("sha256", "") or ""),
        }
        if self.edit_batch_mode:
            self._queue_edit_batch_verification(
                path=path,
                search=str(action.get("search", "")),
                replace=str(action.get("replace", "")),
                mode="contains",
                expected_sha256=str(result_payload.get("sha256", "") or "") or None,
            )
        else:
            self._set_pending_verification(
                path=path,
                search=str(action.get("search", "")),
                replace=str(action.get("replace", "")),
                mode="contains",
                expected_sha256=str(result_payload.get("sha256", "") or "") or None,
            )
        return result_payload

    def _maybe_disable_patch_fulfillment_recovery(self, path: str, payload: Dict[str, Any]) -> None:
        if payload.get("ok"):
            return

        nested_error_code = None
        if isinstance(payload.get("error"), dict):
            nested_error_code = payload["error"].get("code")

        if nested_error_code == "PATCH_FAILED" or "PATCH_FAILED" in str(payload.get("error")):
            if self._recent_patch_failures(path) >= 1:
                self._disable_fulfillment_for_recovery(
                    f"Repeated PATCH_FAILED on {path}; fulfillment mode disabled pending recovery."
                )

    def _handle_patch_file_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        path = str(action.get("path", "") or "").strip()
        search = action.get("search")
        replace = action.get("replace")
        if not path or not isinstance(search, str) or not search or not isinstance(replace, str) or not replace:
            return self._invalid_patch_request_result(action_type)

        repeated_mutation_result = self._repeated_mutation_guard_result(action_type, path)
        if repeated_mutation_result is not None:
            return repeated_mutation_result

        repeated_same_attempt = self._same_patch_attempts(action)
        if repeated_same_attempt >= 3:
            return self._patch_loop_detected_result(action_type, path)

        payload = self._call_tool("patch", *self._patch_tool_args(action, path), action=action)
        result_payload = self._update_patch_verification_state(action, path, payload)
        if payload.get("ok"):
            self._invalidate_fast_caches()
            self._clear_patch_recovery()
        else:
            self._set_patch_recovery(
                task=self._current_task,
                path=path,
                failed_action=action,
                result_payload=result_payload or payload,
            )
        self._record_auto_edit_attempt_fact(
            action_type=action_type,
            path=path,
            action=action,
            payload=payload,
        )
        self._maybe_disable_patch_fulfillment_recovery(path, payload)

        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary=f"Patched file {path}",
            failure_summary=f"Could not patch file {path}",
            next_hint="read_file",
            extra_data=result_payload,
        )

    def _handle_grep_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        args = ["--pattern", str(action["pattern"])]
        if action.get("path"):
            args += ["--path", str(action["path"])]
        if action.get("glob"):
            args += ["--glob", str(action["glob"])]
        if action.get("limit") is not None:
            args += ["--limit", str(int(action["limit"]))]
        if action.get("ignore_case"):
            args.append("--ignore-case")
        if action.get("fixed_strings"):
            args.append("--fixed-strings")
        if action.get("hidden"):
            args.append("--hidden")

        payload = self._call_tool("grep", *args, action=action)
        if payload.get("ok"):
            self._try_activate_context(payload.get("data") or payload, f"grep:{action.get('pattern')}")

        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary=f"Grep completed for pattern {action['pattern']}",
            failure_summary=f"Grep failed for pattern {action['pattern']}",
            next_hint="read_file" if payload.get("ok") else None,
        )

    def _handle_find_files_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        patterns = action.get("patterns")
        if isinstance(patterns, list) and patterns:
            args = []
            if action.get("path"):
                args += ["--path", str(action["path"])]
            for pattern in patterns:
                if isinstance(pattern, str) and pattern.strip():
                    args += ["--pattern", pattern]
            if action.get("limit") is not None:
                args += ["--limit", str(int(action["limit"]))]
            if action.get("hidden"):
                args.append("--hidden")
            payload = self._call_tool("find-files", *args, action=action)
            summary = ", ".join(str(pattern) for pattern in patterns if isinstance(pattern, str))
            return self._finalize_tool_action(
                action_type=action_type,
                payload=payload,
                success_summary=f"Found files matching {summary}",
                failure_summary="Could not find matching files",
                next_hint="read_file" if payload.get("ok") else None,
            )

        if not isinstance(action.get("glob"), str) or not str(action.get("glob", "")).strip():
            return self._error_action_result(
                action_type,
                {
                    "code": "MISSING_GLOB",
                    "message": "find_files requires a non-empty glob or patterns list.",
                    "example": {"type": "find_files", "glob": "**/*.css", "limit": 50},
                },
            )

        args = ["--glob", str(action["glob"])]
        if action.get("path"):
            args += ["--path", str(action["path"])]
        if action.get("limit") is not None:
            args += ["--limit", str(int(action["limit"]))]
        if action.get("hidden"):
            args.append("--hidden")
        payload = self._call_tool("find", *args, action=action)
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary=f"Found files matching {action['glob']}",
            failure_summary=f"Could not find files matching {action['glob']}",
        )

    def _handle_explicit_discovery_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type", "") or "")
        subcommand = action_type.replace("_", "-")
        args: List[str] = []

        if action_type == "search_in_files":
            query = str(action.get("query", "") or "").strip()
            if not query:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "search_in_files requires query."})
            args = ["--query", query]
            if action.get("path"):
                args += ["--path", str(action["path"])]
            if action.get("literal"):
                args.append("--literal")
            if action.get("regex"):
                args.append("--regex")
            if action.get("case_sensitive"):
                args.append("--case-sensitive")
            if action.get("hidden"):
                args.append("--hidden")
            if action.get("limit") is not None:
                args += ["--limit", str(int(action["limit"]))]
        elif action_type == "outline_file":
            path = str(action.get("path", "") or "").strip()
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "outline_file requires path."})
            args = ["--path", path]
        elif action_type == "read_symbol":
            path = str(action.get("path", "") or "").strip()
            symbol_name = str(action.get("symbol_name", "") or "").strip()
            if not path or not symbol_name:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "read_symbol requires path and symbol_name."})
            args = ["--path", path, "--symbol-name", symbol_name]
            if action.get("symbol_kind"):
                args += ["--symbol-kind", str(action["symbol_kind"])]
        elif action_type in {"find_symbol_definitions", "find_symbol_references"}:
            symbol_name = str(action.get("symbol_name", "") or "").strip()
            if not symbol_name:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": f"{action_type} requires symbol_name."})
            args = ["--symbol-name", symbol_name]
            if action.get("path"):
                args += ["--path", str(action["path"])]
            if action.get("symbol_kind") and action_type == "find_symbol_definitions":
                args += ["--symbol-kind", str(action["symbol_kind"])]
            if action.get("hidden"):
                args.append("--hidden")
            if action.get("limit") is not None:
                args += ["--limit", str(int(action["limit"]))]
        elif action_type == "trace_dependencies":
            path = str(action.get("path", "") or "").strip()
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "trace_dependencies requires path."})
            args = ["--path", path, "--direction", str(action.get("direction", "both") or "both"), "--depth", str(int(action.get("depth", 1)))]
        elif action_type == "find_related_files":
            path = str(action.get("path", "") or "").strip()
            if not path:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "find_related_files requires path."})
            args = ["--path", path]
            if action.get("limit") is not None:
                args += ["--limit", str(int(action["limit"]))]
        elif action_type in {"find_related_tests", "find_related_configs", "find_ownership"}:
            target = str(action.get("target", action.get("path", "")) or "").strip()
            if not target:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": f"{action_type} requires target."})
            args = ["--target", target]
            if action.get("path"):
                args += ["--path", str(action["path"])]
            if action.get("limit") is not None and action_type != "find_ownership":
                args += ["--limit", str(int(action["limit"]))]
        elif action_type in {"find_canonical_implementation", "semantic_search", "investigate"}:
            key = "topic" if action_type in {"find_canonical_implementation", "investigate"} else "intent"
            value = str(action.get(key, action.get("query", "")) or "").strip()
            if not value:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": f"{action_type} requires {key}."})
            args = [f"--{key.replace('_', '-')}", value]
            if action.get("path"):
                args += ["--path", str(action["path"])]
            if action.get("limit") is not None and action_type != "investigate":
                args += ["--limit", str(int(action["limit"]))]
            if action_type == "investigate":
                args += ["--mode", str(action.get("mode", "standard") or "standard")]
        elif action_type == "find_similar_code":
            if action.get("query_file"):
                args += ["--query-file", str(action["query_file"])]
            if action.get("snippet"):
                args += ["--snippet", str(action["snippet"])]
            if not args:
                return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": "find_similar_code requires query_file or snippet."})
            if action.get("path"):
                args += ["--path", str(action["path"])]
            if action.get("limit") is not None:
                args += ["--limit", str(int(action["limit"]))]
        elif action_type in {"find_entry_points", "recent_changes"}:
            if action.get("path"):
                args += ["--path", str(action["path"])]
            if action.get("limit") is not None:
                args += ["--limit", str(int(action["limit"]))]
        elif action_type == "get_changed_files":
            args = []
        else:
            return self._error_action_result(action_type, {"code": "BAD_REQUEST", "message": f"Unsupported discovery action: {action_type}"})

        payload = self._call_tool(subcommand, *args, action=action)
        if payload.get("ok"):
            self._try_activate_context(payload.get("data") or payload, f"discovery:{action_type}:{action.get('path') or action.get('target') or action.get('topic') or action.get('intent') or action.get('query') or '.'}")
        label_target = str(action.get("path") or action.get("target") or action.get("topic") or action.get("intent") or action.get("query") or "").strip()
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary=f"Completed {action_type} {label_target}".strip(),
            failure_summary=f"Could not complete {action_type}",
            next_hint="read_file" if payload.get("ok") else None,
        )

    def _handle_symbol_search_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        args: List[str] = []
        if action.get("path"):
            args += ["--path", str(action["path"])]
        if action.get("glob"):
            args += ["--glob", str(action["glob"])]
        if action.get("query"):
            args += ["--query", str(action["query"])]
        if action.get("limit") is not None:
            args += ["--limit", str(int(action["limit"]))]
        if action.get("hidden"):
            args.append("--hidden")

        payload = self._call_tool("symbols", *args, action=action)
        if payload.get("ok"):
            self._try_activate_context(
                payload.get("data") or payload,
                f"symbols:{action.get('query') or action.get('glob') or action.get('path') or '.'}",
            )
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Searched repository symbols",
            failure_summary="Could not search repository symbols",
            next_hint="read_file" if payload.get("ok") else None,
        )

    def _run_shell_command_list(self, action: Dict[str, Any]) -> Tuple[Optional[List[str]], Optional[ActionResult]]:
        action_type = str(action.get("type"))
        command = action["command"]
        if isinstance(command, str):
            return ["bash", "-lc", command], None
        if isinstance(command, (list, tuple)) and all(isinstance(x, str) for x in command):
            return list(command), None
        return None, self._error_action_result(
            action_type,
            {"error": "run_shell.command must be a list of strings or a shell string"},
        )

    def _handle_run_shell_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        if not self._shell_access_enabled():
            return self._error_action_result(
                action_type,
                {"code": "SHELL_DISABLED", "message": "Shell access is disabled by SHELL_ACCESS=false."},
            )
        cmd_list, error_result = self._run_shell_command_list(action)
        if error_result is not None or cmd_list is None:
            return cast(ActionResult, error_result)

        payload = self._call_tool(
            "run",
            "--timeout",
            str(self.config.shell_timeout),
            "--",
            *cmd_list,
            action=action,
        )
        if payload.get("ok"):
            command = action["command"]
            label = command if isinstance(command, str) else " ".join(cmd_list)
            self._try_activate_context(payload.get("data") or payload, f"shell:{label}")
            self._invalidate_fast_caches()

        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Shell command completed",
            failure_summary="Shell command failed",
            next_hint="finish" if payload.get("ok") else None,
        )

    def _handle_show_diff_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        payload = self._call_tool("diff", action=action)
        patch_recovery_cleared = False
        if payload.get("ok"):
            patch_recovery_cleared = self._maybe_complete_patch_recovery(action, payload.get("data") or {})
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Fetched repository diff",
            failure_summary="Could not fetch repository diff",
            next_hint="finish" if payload.get("ok") else None,
            extra_data={"patch_recovery_cleared": patch_recovery_cleared},
        )

    def _handle_meta_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        payload = self._call_tool("meta", action=action)
        if payload.get("ok"):
            self._try_activate_context(payload.get("data") or payload, "meta")
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Fetched repository metadata",
            failure_summary="Could not fetch repository metadata",
        )

    def _handle_skill_action(self, action: Dict[str, Any]) -> ActionResult:
        name = str(action.get("name", "") or "").strip()
        if not name:
            skills = self._available_skills_payload()
            return ActionResult(
                ok=True,
                name="skill",
                payload={
                    "summary": f"Loaded {len(skills)} skill(s).",
                    "skills": skills,
                },
            )
        skill = self._registered_skills.get(name)
        if skill is None:
            return ActionResult(
                ok=False,
                name="skill",
                payload={"error": f"Unknown skill: {name}", "name": name},
            )
        mode = str(action.get("mode", "") or "").strip()
        content = skill.render(mode=mode) if mode else skill.cache
        return ActionResult(
            ok=True,
            name="skill",
            payload={
                "summary": f"Loaded skill {name}.",
                "name": skill.name,
                "description": skill.description,
                "args_schema": dict(skill.args_schema),
                "tags": list(skill.tags),
                "category": skill.category,
                "priority": skill.priority,
                "modes": list(skill.modes),
                "mode": mode,
                "content": content,
            },
        )

    def _handle_git_status_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        args: List[str] = []
        if action.get("limit") is not None:
            args += ["--limit", str(int(action["limit"]))]
        if action.get("ignored"):
            args.append("--ignored")
        payload = self._call_tool("git-status", *args, action=action)
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Fetched git status",
            failure_summary="Could not fetch git status",
            next_hint="finish" if payload.get("ok") else None,
        )

    def _handle_git_diff_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        args: List[str] = []
        if action.get("path"):
            args += ["--path", str(action["path"])]
        if action.get("staged"):
            args.append("--staged")
        if action.get("name_only"):
            args.append("--name-only")
        if action.get("stat"):
            args.append("--stat")
        if action.get("summary_only"):
            args.append("--summary-only")
        if action.get("limit") is not None:
            args += ["--limit", str(int(action["limit"]))]
        payload = self._call_tool("git-diff", *args, action=action)
        raw_data = payload.get("data")
        result_payload: Dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        if payload.get("ok"):
            self._try_activate_context(result_payload, f"git_diff:{action.get('path', 'repo')}")
        patch_recovery_cleared = False
        if payload.get("ok"):
            patch_recovery_cleared = self._maybe_complete_patch_recovery(action, result_payload)
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Fetched git diff",
            failure_summary="Could not fetch git diff",
            next_hint="finish" if payload.get("ok") else None,
            extra_data={**result_payload, "patch_recovery_cleared": patch_recovery_cleared},
        )

    def _handle_review_changes_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        args: List[str] = []
        if action.get("path"):
            args += ["--path", str(action["path"])]
        if action.get("limit") is not None:
            args += ["--limit", str(int(action["limit"]))]
        if action.get("ignored"):
            args.append("--ignored")
        payload = self._call_tool("review", *args, action=action)
        if payload.get("ok"):
            self._try_activate_context(payload.get("data") or payload, f"review_changes:{action.get('path') or 'repo'}")
        patch_recovery_cleared = False
        if payload.get("ok"):
            raw_review_data = payload.get("data")
            review_payload: Dict[str, Any] = raw_review_data if isinstance(raw_review_data, dict) else {}
            patch_recovery_cleared = self._maybe_complete_patch_recovery(action, review_payload)
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Reviewed repository changes",
            failure_summary="Could not review repository changes",
            next_hint="read_file" if payload.get("ok") else None,
            extra_data={"patch_recovery_cleared": patch_recovery_cleared},
        )

    def _git_add_args_or_error(self, action: Dict[str, Any]) -> Tuple[Optional[List[str]], Optional[ActionResult]]:
        action_type = str(action.get("type"))
        paths = action.get("paths")
        if isinstance(paths, list) and paths:
            args: List[str] = []
            for path in paths:
                args.extend(["--path", str(path)])
            return args, None

        single_path = action.get("path")
        if isinstance(single_path, str) and single_path:
            return ["--path", str(single_path)], None

        return None, self._error_action_result(
            action_type,
            {
                "ok": False,
                "action_type": action_type,
                "status": "failed",
                "summary": "git_add requires `path` or `paths`",
                "data": None,
                "error": {
                    "code": "MISSING_PATH",
                    "message": "Provide `path` or `paths` for git_add",
                },
                "next_hint": "use git_add with path or paths",
            },
        )

    def _handle_git_add_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        args, error_result = self._git_add_args_or_error(action)
        if error_result is not None or args is None:
            return cast(ActionResult, error_result)

        payload = self._call_tool("git-add", *args, action=action)
        if payload.get("ok"):
            self._invalidate_fast_caches()
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Staged git paths",
            failure_summary="Could not stage git paths",
            next_hint="git_commit" if payload.get("ok") else "run_shell",
        )

    def _handle_git_restore_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        args = ["--path", str(action["path"])]
        if action.get("staged"):
            args.append("--staged")
        payload = self._call_tool("git-restore", *args, action=action)
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Restored git path",
            failure_summary="Could not restore git path",
        )

    def _handle_git_commit_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        payload = self._call_tool("git-commit", "--message", str(action.get("message", "")), action=action)
        if payload.get("ok"):
            self._invalidate_fast_caches()
            self._mark_task_satisfied("Created git commit successfully.")
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Created git commit",
            failure_summary="Could not create git commit",
            next_hint="finish" if payload.get("ok") else "git_status",
        )

    def _handle_git_log_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        limit = str(int(action.get("limit", 10)))
        payload = self._call_tool("git-log", "--limit", limit, action=action)
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Fetched git log",
            failure_summary="Could not fetch git log",
        )

    def _handle_git_branch_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        payload = self._call_tool("git-branch", action=action)
        return self._finalize_tool_action(
            action_type=action_type,
            payload=payload,
            success_summary="Fetched git branch",
            failure_summary="Could not fetch git branch",
        )

    def _handle_history_expand_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        step_numbers = self._normalize_step_list(action)
        if not step_numbers:
            return self._error_action_result(action_type, {"error": "Invalid or missing step/steps"})

        payload = self._expand_history_step(step_numbers[0]) if len(step_numbers) == 1 else self._expand_history_steps(step_numbers)
        return ActionResult(ok="error" not in payload, name=action_type, payload=payload)

    def _handle_memory_expand_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        mem_ids = self._normalize_memory_id_list(action)
        if not mem_ids:
            return self._error_action_result(action_type, {"error": "Missing memory id/ids"})

        payload = self._expand_memory_item(mem_ids[0]) if len(mem_ids) == 1 else self._expand_memory_items(mem_ids)
        return ActionResult(ok="error" not in payload, name=action_type, payload=payload)

    def _handle_set_fact_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        key = str(action.get("key", "") or "").strip()
        value = str(action.get("value", "") or "").strip()
        fact_type = str(action.get("fact_type", "") or "").strip().lower()
        if fact_type not in {FACT_TYPE_GOAL, FACT_TYPE_ARCHITECTURE}:
            return self._error_action_result(
                action_type,
                {
                    "error": 'set_fact requires fact_type as "goal" or "architecture".',
                    "next_hint": 'Retry set_fact with fact_type "goal" for task-local findings or "architecture" for reusable repo knowledge.',
                },
            )
        if not key or not value:
            return self._error_action_result(
                action_type,
                {
                    "error": "set_fact requires non-empty key and value.",
                    "next_hint": "Retry with a concise durable finding, for example {\"type\":\"set_fact\",\"key\":\"goal/findings/<short_key>\",\"value\":\"<specific finding>\",\"fact_type\":\"goal\"}.",
                },
            )
        existing = self.issue_ledger.find_fact(key)
        if existing is not None:
            return self._error_action_result(
                action_type,
                {
                    "error": f"Fact already exists: {key}.",
                    "next_hint": "Use update_fact with the same key to revise an existing fact, or choose a different key for a distinct finding.",
                },
            )
        quality_error = self._validate_fact_quality(action_type, key, value, fact_type)
        if quality_error is not None:
            return quality_error

        record = self._set_fact_record(key, value, source_action=action_type, fact_type=fact_type)
        self._set_recent_fact_handoff(record, action_type=action_type)
        self._clear_pending_fact_resolution()
        return ActionResult(
            ok=True,
            name=action_type,
            payload={
                "message": f"Fact recorded: {record.key}",
                "fact": record.to_dict(),
            },
        )

    def _handle_update_fact_action(self, action: Dict[str, Any]) -> ActionResult:
        action_type = str(action.get("type"))
        key = str(action.get("key", "") or "").strip()
        if not key:
            return self._error_action_result(
                action_type,
                {
                    "error": "update_fact requires a non-empty key.",
                    "next_hint": "Retry update_fact with the exact existing fact key, or use set_fact with a non-empty key for a new finding.",
                },
            )
        existing = self.issue_ledger.find_fact(key)
        if existing is None:
            return self._error_action_result(
                action_type,
                {
                    "error": f"Fact not found: {key}",
                    "next_hint": "Use set_fact to create a new finding, or retry update_fact with an existing key from the fact context.",
                },
            )

        next_value = str(
            action.get("value", action.get("resolution", "")) or existing.value
        ).strip() or existing.value
        fact_type = str(action.get("fact_type", existing.fact_type) or existing.fact_type).strip().lower()
        if fact_type not in {FACT_TYPE_GOAL, FACT_TYPE_ARCHITECTURE}:
            fact_type = existing.fact_type
        quality_error = self._validate_fact_quality(action_type, key, next_value, fact_type)
        if quality_error is not None:
            return quality_error
        record = self._set_fact_record(key, next_value, source_action=action_type, fact_type=fact_type)
        self._set_recent_fact_handoff(record, action_type=action_type)
        self._clear_pending_fact_resolution()
        return ActionResult(
            ok=True,
            name=action_type,
            payload={
                "message": f"Fact updated: {record.key}",
                "fact": record.to_dict(),
            },
        )

    def _handle_begin_edit_batch_action(self, action: Dict[str, Any]) -> ActionResult:
        if self.edit_batch_mode:
            return ActionResult(
                ok=True,
                name=str(action.get("type")),
                payload={
                    "message": "Edit batch mode is already active.",
                    "pending_paths": sorted(self.edit_batch_pending.keys()),
                },
            )

        self._enter_edit_batch_mode()
        if self.pending_verification is not None:
            pending = dict(self.pending_verification)
            self._clear_pending_verification()
            self._queue_edit_batch_verification(
                path=str(pending.get("path", "") or ""),
                search=str(pending.get("search", "") or ""),
                replace=str(pending.get("replace", "") or ""),
                mode=str(pending.get("mode", "contains") or "contains"),
                expected_sha256=str(pending.get("expected_sha256", "") or "") or None,
            )
        return ActionResult(
            ok=True,
            name=str(action.get("type")),
            payload={
                "message": "Edit batch mode activated.",
                "pending_paths": sorted(self.edit_batch_pending.keys()),
                "next_hint": "patch_file",
            },
        )

    def _handle_end_edit_batch_action(self, action: Dict[str, Any]) -> ActionResult:
        return self._run_edit_batch_verification(reason="exit")

    def _handle_finish_action(self, action: Dict[str, Any]) -> ActionResult:
        if self.edit_batch_mode and self.edit_batch_pending:
            self._run_edit_batch_verification(reason="before finish")
        blocked_payload = self._finish_block_payload()
        if isinstance(blocked_payload, dict) and blocked_payload.get("code") == "FINISH_BLOCKED_VALIDATION_REQUIRED":
            self._run_parallel_post_write_validation()
            blocked_payload = self._finish_block_payload()
        if blocked_payload is not None:
            return self._error_action_result(str(action.get("type")), blocked_payload)
        self._clear_execution_context_state()
        return ActionResult(
            ok=True,
            name=str(action.get("type")),
            payload={"message": str(action.get("message", "Done."))},
        )

    def _handle_drop_context_action(self, action: Dict[str, Any]) -> ActionResult:
        try:
            self._drop_context()
            self._clear_patch_recovery()
            self._clear_edit_batch_state()
            self._clear_pending_verification()
            self._clear_pending_fact_resolution()
        except Exception:
            pass
        return ActionResult(ok=True, name=str(action.get("type")), payload={"message": "context dropped"})

    def _enforce_budget_and_mode(self, action_type: str) -> Optional[ActionResult]:
        if self.discovery_budget is None:
            return None

        if action_type in MUTATION_ACTION_TYPES | {"batch_mutate", "git_add", "git_restore", "git_commit"}:
            return ActionResult(
                ok=False,
                name=action_type or "unknown",
                payload={
                    "error": "Discovery mode is read-only. Finish discovery before mutating repository state.",
                    "discovery_budget": {
                        "mode": self.discovery_budget.mode_label,
                        "tool_calls_used": self.discovery_budget.tool_calls_used,
                        "tool_calls_max": self.discovery_budget.max_tool_calls,
                        "tool_calls_remaining": self.discovery_budget.remaining_tool_calls,
                        "budget_exhausted": self.discovery_budget.exhausted,
                    },
                    "next_hint": "finish",
                },
            )

        if action_type in TOOL_BACKED_ACTION_TYPES:
            if self.discovery_budget.exhausted:
                return ActionResult(
                    ok=False,
                    name=action_type or "unknown",
                    payload={
                        "error": (
                            "Discovery tool-call budget exhausted. "
                            "Use finish to return control to the planner."
                        ),
                        "discovery_budget": {
                            "mode": self.discovery_budget.mode_label,
                            "tool_calls_used": self.discovery_budget.tool_calls_used,
                            "tool_calls_max": self.discovery_budget.max_tool_calls,
                            "tool_calls_remaining": self.discovery_budget.remaining_tool_calls,
                            "budget_exhausted": self.discovery_budget.exhausted,
                        },
                        "next_hint": "finish",
                    },
                )
            self.discovery_budget.tool_calls_used += 1

        return None

    def _execute_action(self, action: Dict[str, Any]) -> ActionResult:
        t = action.get("type")
        action_type = str(t or "")

        try:
            blocked_payload = self._post_satisfaction_block_payload(action)
            if blocked_payload is not None:
                return self._error_action_result(action_type, blocked_payload)

            blocked_result = self._handle_verification_block(action, t)
            if blocked_result is not None:
                return blocked_result

            blocked_result = self._handle_patch_recovery_block(action, t)
            if blocked_result is not None:
                return blocked_result

            blocked_result = self._handle_fact_resolution_block(action, t)
            if blocked_result is not None:
                return blocked_result

            blocked_result = self._enforce_budget_and_mode(action_type)
            if blocked_result is not None:
                return blocked_result

            handlers: Dict[str, Callable[[Dict[str, Any]], ActionResult]] = {
                "list_files": self._handle_list_files_action,
                "read_file": self._handle_read_file_action,
                "inspect_files": self._handle_inspect_files_action,
                "summarize_files": self._handle_summarize_files_action,
                "search_in_files": self._handle_explicit_discovery_action,
                "outline_file": self._handle_explicit_discovery_action,
                "read_symbol": self._handle_explicit_discovery_action,
                "find_symbol_definitions": self._handle_explicit_discovery_action,
                "find_symbol_references": self._handle_explicit_discovery_action,
                "trace_dependencies": self._handle_explicit_discovery_action,
                "find_related_files": self._handle_explicit_discovery_action,
                "find_related_tests": self._handle_explicit_discovery_action,
                "find_related_configs": self._handle_explicit_discovery_action,
                "find_canonical_implementation": self._handle_explicit_discovery_action,
                "find_similar_code": self._handle_explicit_discovery_action,
                "find_entry_points": self._handle_explicit_discovery_action,
                "find_ownership": self._handle_explicit_discovery_action,
                "recent_changes": self._handle_explicit_discovery_action,
                "get_changed_files": self._handle_explicit_discovery_action,
                "semantic_search": self._handle_explicit_discovery_action,
                "investigate": self._handle_explicit_discovery_action,
                "write_file": self._handle_write_file_action,
                "patch_file": self._handle_patch_file_action,
                "replace_range": self._handle_explicit_mutation_action,
                "replace_snippet": self._handle_explicit_mutation_action,
                "insert_before": self._handle_explicit_mutation_action,
                "insert_after": self._handle_explicit_mutation_action,
                "delete_range": self._handle_explicit_mutation_action,
                "delete_snippet": self._handle_explicit_mutation_action,
                "append_block": self._handle_explicit_mutation_action,
                "prepend_block": self._handle_explicit_mutation_action,
                "replace_symbol": self._handle_explicit_mutation_action,
                "insert_symbol_member": self._handle_explicit_mutation_action,
                "rename_symbol": self._handle_explicit_mutation_action,
                "move_block": self._handle_explicit_mutation_action,
                "create_file": self._handle_explicit_mutation_action,
                "delete_file": self._handle_explicit_mutation_action,
                "rename_file": self._handle_explicit_mutation_action,
                "copy_file": self._handle_explicit_mutation_action,
                "fill_template": self._handle_explicit_mutation_action,
                "batch_mutate": self._handle_batch_mutate_action,
                "grep": self._handle_grep_action,
                "find_files": self._handle_find_files_action,
                "symbol_search": self._handle_symbol_search_action,
                "run_shell": self._handle_run_shell_action,
                "diagnose": self._handle_diagnose_action,
                "changed_files_check": self._handle_changed_files_check_action,
                "project_problems": self._handle_project_problems_action,
                "skill": self._handle_skill_action,
                "show_diff": self._handle_show_diff_action,
                "meta": self._handle_meta_action,
                "git_status": self._handle_git_status_action,
                "git_diff": self._handle_git_diff_action,
                "review_changes": self._handle_review_changes_action,
                "git_add": self._handle_git_add_action,
                "git_restore": self._handle_git_restore_action,
                "git_commit": self._handle_git_commit_action,
                "git_log": self._handle_git_log_action,
                "git_branch": self._handle_git_branch_action,
                "history_expand": self._handle_history_expand_action,
                "memory_expand": self._handle_memory_expand_action,
                "set_fact": self._handle_set_fact_action,
                "update_fact": self._handle_update_fact_action,
                "begin_edit_batch": self._handle_begin_edit_batch_action,
                "end_edit_batch": self._handle_end_edit_batch_action,
                "finish": self._handle_finish_action,
                "drop_context": self._handle_drop_context_action,
            }

            handler = handlers.get(str(t))
            if handler is None:
                return ActionResult(ok=False, name="unknown_action", payload={"error": f"Unknown action type: {t}"})
            return handler(action)

        except subprocess.TimeoutExpired:
            return ActionResult(ok=False, name=t or "unknown", payload={"error": "Shell command timed out."})
        except FileNotFoundError as exc:
            return ActionResult(ok=False, name=t or "unknown", payload={"error": str(exc)})
        except Exception as exc:
            return ActionResult(ok=False, name=t or "unknown", payload={"error": str(exc)})


# -----------------------------
# CLI
# -----------------------------

def create_model_client(
    *,
    provider: str,
    model: str,
    thinking_mode: str = "medium",
    verbosity: str = "medium",
) -> BaseModelClient:
    normalized_provider = str(provider or "").strip().lower()
    normalized_model = str(model or "").strip()
    validate_provider_model_selection(normalized_provider, normalized_model)
    if normalized_provider == "openai":
        return OpenAIModelClient(
            model=normalized_model,
            thinking_mode=thinking_mode,
            verbosity=verbosity,
        )
    if normalized_provider == "anthropic":
        return AnthropicModelClient(
            model=normalized_model,
            thinking_mode=thinking_mode,
        )
    if normalized_provider == "gemini":
        return GeminiModelClient(
            model=normalized_model,
            thinking_mode=thinking_mode,
        )
    if normalized_provider == "local":
        return LocalModelClient(
            model=normalized_model,
            thinking_mode=thinking_mode,
            verbosity=verbosity,
        )
    if normalized_provider in {"ollama", "ollama-local"}:
        return OllamaModelClient(
            model=normalized_model,
            thinking_mode=thinking_mode,
            verbosity=verbosity,
            base_url=os.getenv("OLLAMA_LOCAL_BASE_URL") or os.getenv("OLLAMA_BASE_URL") or "http://127.0.0.1:11434/v1",
            api_key=os.getenv("OLLAMA_LOCAL_API_KEY") or os.getenv("OLLAMA_API_KEY") or os.getenv("OPENAI_API_KEY") or "ollama-key",
            provider_name="ollama-local",
        )
    if normalized_provider == "ollama-runpod":
        return OllamaModelClient(
            model=normalized_model,
            thinking_mode=thinking_mode,
            verbosity=verbosity,
            base_url=os.getenv("OLLAMA_RUNPOD_BASE_URL") or "https://zql0xy4x10v0sp-11434.proxy.runpod.net/v1/chat/completions",
            api_key=os.getenv("OLLAMA_RUNPOD_API_KEY") or os.getenv("OLLAMA_API_KEY") or os.getenv("OPENAI_API_KEY") or "ollama-key",
            provider_name="ollama-runpod",
        )
    raise ValueError(f"Unsupported provider: {provider}")


def build_model_client(args: argparse.Namespace) -> BaseModelClient:
    return create_model_client(
        provider=args.provider,
        model=args.model,
        thinking_mode=args.thinking_mode,
        verbosity=args.verbosity,
    )


def print_banner(agent: WorkingFolderAgent) -> None:
    config = agent.config
    print("=" * 72)
    print("Working Folder Agent")
    print(f"provider   : {config.provider}")
    print(f"model      : {config.model}")
    print(f"root       : {config.root}")
    print(f"tools      : {config.tool_script}")
    print(f"max_steps  : {config.max_steps}")
    print(f"thinking   : {config.thinking_mode}")
    if config.provider in {"openai", "local"}:
        print(f"verbosity  : {config.verbosity}")
    for line in agent.repo_facts_status_lines():
        print(line)
    print("=" * 72)
    print("Interactive commands:")
    print("  /task <instruction>   run a task")
    print("  /steer <instruction>  set persistent operator steering")
    print("  /steer-clear          clear persistent steering")
    print("  /steer-show           show current steering")
    print("  /runtime-show         show active provider/model")
    print("  /runtime <p> <m>      switch provider/model without restarting")
    print("  /model <m>            switch model within the current provider")
    print("  /providers            list supported providers")
    print("  /models [provider]    list suggested models for a provider")
    print("  /files                show repo files")
    print("  /read <path>          print a file")
    print("  /shell <command>      run a command in root")
    print("  /history              show recent agent history")
    print("  /quit                 exit")
    print("=" * 72)


def _runtime_panel_lines(config: AgentConfig) -> List[str]:
    lines = [
        f"provider : {config.provider}",
        f"model    : {config.model}",
        f"thinking : {config.thinking_mode}",
    ]
    if config.provider in {"openai", "local"}:
        lines.append(f"verbosity: {config.verbosity}")
    return lines


def _parse_runtime_switch(raw: str, current_provider: str) -> Optional[Tuple[str, str]]:
    if raw.startswith("/runtime "):
        remainder = raw[len("/runtime "):].strip()
        parts = remainder.split(None, 1)
        if len(parts) != 2:
            raise ValueError("Usage: /runtime <provider> <model>")
        return parts[0].strip().lower(), parts[1].strip()
    if raw.startswith("/model "):
        model = raw[len("/model "):].strip()
        if not model:
            raise ValueError("Usage: /model <model>")
        return current_provider.strip().lower(), model
    return None


def _parse_runtime_models_command(raw: str, current_provider: str) -> Optional[str]:
    if raw == "/models":
        return current_provider.strip().lower()
    if raw.startswith("/models "):
        provider = raw[len("/models "):].strip().lower()
        if not provider:
            raise ValueError("Usage: /models [provider]")
        return provider
    return None


def interactive_worker_loop(agent: WorkingFolderAgent) -> None:
    print_banner(agent)

    while True:
        try:
            raw = input("\nagent> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye")
            return

        if not raw:
            continue

        if raw in {"/quit", "quit", "exit"}:
            print("bye")
            return

        if raw.startswith("/task "):
            task = raw[len("/task "):].strip()
            if not task:
                print("Task cannot be empty.")
                continue
            result = agent.run_task(task)
            print(f"\nFINAL: {result.final_message}")
            print(agent.render_last_usage_summary())
            continue

        if raw.startswith("/steer "):
            prompt = raw[len("/steer "):].strip()
            if not prompt:
                print("Steering prompt cannot be empty.")
                continue
            agent.set_steering(prompt)
            print("Steering updated.")
            print(agent._steering_block())
            continue

        if raw == "/steer-clear":
            agent.clear_steering()
            print("Steering cleared.")
            continue

        if raw == "/steer-show":
            print(agent._steering_block())
            continue

        if raw == "/runtime-show":
            print("Runtime:")
            for line in _runtime_panel_lines(agent.config):
                print(line)
            bs = agent.get_backoff_state()
            if bs.get("enabled"):
                print(f"backoff  : on ({bs['token_limit_k']}k input tokens/min, window used: {bs['window_tokens_used']})")
            else:
                print("backoff  : off")
            continue

        if raw.startswith("/backoff"):
            remainder = raw[len("/backoff"):].strip()
            if remainder in {"off", "0", "false", "disable"}:
                result = agent.configure_backoff(enabled=False, token_limit_k=0)
                print("Backoff disabled.")
            elif remainder == "" or remainder == "show":
                bs = agent.get_backoff_state()
                if bs.get("enabled"):
                    print(f"Backoff: on ({bs['token_limit_k']}k input tokens/min, window used: {bs['window_tokens_used']})")
                else:
                    print("Backoff: off")
            else:
                try:
                    limit_k = int(remainder)
                    if limit_k <= 0:
                        print("Token limit must be a positive number (in thousands).")
                    else:
                        result = agent.configure_backoff(enabled=True, token_limit_k=limit_k)
                        print(f"Backoff enabled: {limit_k}k input tokens/min. Will pause 60s at limit.")
                except ValueError:
                    print("Usage: /backoff <tokens_in_thousands>  or  /backoff off")
            continue

        if raw == "/providers":
            print(_render_text_panel("Providers", runtime_provider_lines(agent.config.provider)))
            continue

        try:
            models_provider = _parse_runtime_models_command(raw, agent.config.provider)
        except ValueError as exc:
            print(f"Runtime error: {exc}")
            continue
        if models_provider is not None:
            try:
                print(_render_text_panel("Models", runtime_model_lines(models_provider, agent.config.model)))
            except Exception as exc:
                print(f"Runtime error: {exc}")
            continue

        try:
            runtime_switch = _parse_runtime_switch(raw, agent.config.provider)
        except ValueError as exc:
            print(f"Runtime error: {exc}")
            continue
        if runtime_switch is not None:
            provider, model = runtime_switch
            try:
                updated = agent.reconfigure_runtime(provider=provider, model=model)
                print("Runtime updated without restarting the process.")
                print(f"provider : {updated['provider']}")
                print(f"model    : {updated['model']}")
            except Exception as exc:
                print(f"Runtime update failed: {exc}")
            continue

        if raw == "/files":
            for f in list_files(agent.root, 500):
                print(f)
            continue

        if raw.startswith("/read "):
            path = raw[len("/read "):].strip()
            try:
                target = safe_join(agent.root, path)
                print(read_text_file(target))
            except Exception as exc:
                print(f"error: {exc}")
            continue

        if raw.startswith("/shell "):
            cmd = raw[len("/shell "):].strip()
            try:
                if not agent.config.allow_shell:
                    print("error: shell access is disabled by SHELL_ACCESS=false")
                    continue
                result = run_shell(agent.root, cmd, timeout=agent.config.shell_timeout)
                print(json.dumps(result, indent=2))
            except Exception as exc:
                print(f"error: {exc}")
            continue

        if raw == "/history":
            print(agent._history_snapshot())
            continue

        # Default: treat raw text as a task
        result = agent.run_task(raw)
        print(f"\nFINAL: {result.final_message}")
        print(agent.render_last_usage_summary())


def interactive_loop(agent: WorkingFolderAgent) -> None:
    interactive_worker_loop(agent)


def build_agent_config(args: argparse.Namespace, root: Path, tool_path: Path) -> AgentConfig:
    return AgentConfig(
        provider=args.provider,
        model=args.model,
        root=root,
        tool_script=tool_path,
        max_steps=max(1, args.max_steps),
        shell_timeout=max(1, args.shell_timeout),
        thinking_mode=args.thinking_mode,
        verbosity=args.verbosity,
        show_prompts=args.show_prompts,
        show_model_output=args.show_model_output,
        auto_confirm_write=not args.confirm_writes,
        auto_confirm_shell=not args.confirm_shell,
        allow_shell=_env_flag_enabled("SHELL_ACCESS", True),
        memory_limit=max(100, args.memory_limit),
        memory_retrieval_limit=max(1, args.memory_retrieval_limit),
        max_parallel_workers=max(1, args.max_parallel_workers),
        quiet=bool(getattr(args, "extension_bridge", False)),
    )


def create_working_folder_agent(model_client: BaseModelClient, config: AgentConfig) -> WorkingFolderAgent:
    return WorkingFolderAgent(model_client, config)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Interactive coding agent over a working folder.")
    parser.add_argument("--provider", choices=supported_provider_keys(), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--root", required=True, help="Working folder root. All operations are relative to this path.")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--shell-timeout", type=int, default=60)

    # Shared user-facing thinking knob. Mapped per provider.
    parser.add_argument(
        "--thinking-mode",
        default="medium",
        choices=["auto", "none", "minimal", "low", "medium", "high", "xhigh"],
        help="OpenAI: reasoning effort. Gemini: mapped to thinking_level or thinking_budget.",
    )

    parser.add_argument(
        "--verbosity",
        default="medium",
        choices=["low", "medium", "high"],
        help="OpenAI only.",
    )

    parser.add_argument("--show-prompts", action="store_true")
    parser.add_argument("--show-model-output", action="store_true")
    parser.add_argument("--worker-mode", action="store_true", help="Run the direct worker loop instead of the planner.")
    parser.add_argument("--confirm-writes", action="store_true", help="Ask before each write.")
    parser.add_argument("--confirm-shell", action="store_true", help="Ask before each shell command.")
    parser.add_argument(
        "--tools",
        required=False,
        help="Path to agent_tools.py (default: agent_tools.py next to main.py)",
    )
    parser.add_argument("--memory-limit", type=int, default=5000)
    parser.add_argument("--memory-retrieval-limit", type=int, default=8)
    parser.add_argument("--max-parallel-workers", type=int, default=4)
    parser.add_argument("--extension-bridge", action="store_true", help="Run a JSON bridge for the VS Code extension.")
    parser.add_argument("--delete-session", action="store_true", help="Delete session artifacts (repo_facts.md, memory_observability.md) for the given --root and exit.")
    return parser.parse_args(argv)


def _emit_bridge_message(payload: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_bridge_planner_action(
    *,
    planner: Any,
    transcript: List[Dict[str, str]],
    request: Dict[str, Any],
    add_exchange: Callable[[str, str], None],
    emit_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> str:
    action_name = str(request.get("action", "") or "").strip()
    if action_name == "approve_plan":
        add_exchange("user", "approve")
        message = planner.execute_pending_plan()
        add_exchange("assistant", message)
        return message
    if action_name == "reject_plan":
        add_exchange("user", "reject")
        message = planner.continue_conversation("reject")
        add_exchange("assistant", message)
        return message
    if action_name == "select_discovery_mode" or action_name in {"discovery_quick", "discovery_moderate", "discovery_deep"}:
        mode = str(request.get("mode", "") or "").strip().lower()
        if not mode:
            payload = request.get("payload")
            if isinstance(payload, dict):
                mode = str(payload.get("mode", "") or "").strip().lower()
        if not mode and action_name.startswith("discovery_"):
            mode = action_name.removeprefix("discovery_").strip().lower()
        if mode not in {"quick", "moderate", "deep"}:
            pending = getattr(planner.session, "pending_discovery", None)
            if pending is not None:
                mode = str(getattr(pending, "recommended_mode", "") or "").strip().lower()
        if mode not in {"quick", "moderate", "deep"}:
            raise ValueError("Discovery selection requires mode quick, moderate, or deep")
        add_exchange("user", mode)
        if callable(emit_progress):
            emit_progress(
                {
                    "step": 0,
                    "action_type": "discovery_mode_selected",
                    "path": "",
                    "ok": True,
                    "elapsed_s": 0,
                    "thought": f"Discovery mode selected: {mode}",
                    "summary": "Bridge accepted discovery mode selection and is starting the worker run.",
                    "diff": "",
                    "replacements": 0,
                    "added_lines": 0,
                    "removed_lines": 0,
                    "search_excerpt": "",
                    "replace_excerpt": "",
                    "inspected_file_count": 0,
                    "inspected_files": [],
                }
            )
        message = planner.continue_conversation(mode)
        add_exchange("assistant", message)
        return message
    if action_name == "skip_discovery":
        add_exchange("user", "no")
        message = planner.continue_conversation("no")
        add_exchange("assistant", message)
        return message
    if action_name == "reopen_issue":
        issue_id = str(request.get("issue_id", "") or "").strip()
        if not issue_id:
            payload = request.get("payload")
            if isinstance(payload, dict):
                issue_id = str(payload.get("issue_id", "") or "").strip()
        if not issue_id:
            raise ValueError("reopen_issue requires issue_id")
        message = planner.reopen_issue(issue_id)
        add_exchange("assistant", message)
        return message
    if action_name == "close_issue":
        issue_id = str(request.get("issue_id", "") or "").strip()
        if not issue_id:
            payload = request.get("payload")
            if isinstance(payload, dict):
                issue_id = str(payload.get("issue_id", "") or "").strip()
        if not issue_id:
            raise ValueError("close_issue requires issue_id")
        message = planner.close_issue(issue_id)
        add_exchange("assistant", message)
        return message
    if action_name == "reset_session":
        planner.clear_session()
        transcript.clear()
        message = "Planner session cleared."
        add_exchange("assistant", message)
        return message
    if action_name == "delete_session":
        message = planner.delete_session()
        transcript.clear()
        add_exchange("assistant", message)
        return message
    raise ValueError(f"Unsupported planner_action: {action_name}")


def _run_extension_bridge(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    model_holder: Dict[str, BaseModelClient] = {"client": build_model_client(args)}
    default_tools = Path(__file__).parent / "agent_tools.py"
    tool_path = Path(args.tools).expanduser().resolve() if getattr(args, "tools", None) else default_tools.resolve()
    config = build_agent_config(args, root, tool_path)

    from planner import PlannerAgent
    transcript: List[Dict[str, str]] = []
    last_bridge_state: Dict[str, Any] | None = None

    def bridge_state(last_message: str = "") -> Dict[str, Any]:
        return {
            "planner": planner.export_state(),
            "transcript": transcript[-40:],
            "last_message": last_message,
        }

    def safe_bridge_state(last_message: str = "") -> Dict[str, Any]:
        nonlocal last_bridge_state
        try:
            state = bridge_state(last_message)
            last_bridge_state = state
            return state
        except Exception as exc:
            fallback_planner: Dict[str, Any] = {}
            fallback_transcript: List[Dict[str, str]] = transcript[-40:]
            if isinstance(last_bridge_state, dict):
                cached_planner = last_bridge_state.get("planner")
                cached_transcript = last_bridge_state.get("transcript")
                if isinstance(cached_planner, dict):
                    fallback_planner = dict(cached_planner)
                if isinstance(cached_transcript, list):
                    fallback_transcript = [item for item in cached_transcript if isinstance(item, dict)]
            return {
                "planner": fallback_planner,
                "transcript": fallback_transcript,
                "last_message": last_message,
                "bridge_warning": f"bridge_state failed: {exc}",
            }

    def _make_step_callback(step: AgentStep, domain: str = "") -> None:
        action_type = str(step.action.get("type", "")).strip()
        path = str(step.action.get("path", "") or "").strip()
        thought = str(step.thought or "").strip()
        summary = ""
        skill_name = ""
        skill_mode = ""
        skill_count = 0
        diff = ""
        replacements = 0
        added_lines = 0
        removed_lines = 0
        search_excerpt = ""
        replace_excerpt = ""
        inspected_file_count = 0
        inspected_files: List[Dict[str, Any]] = []
        if isinstance(step.result.payload, dict):
            summary = str(step.result.payload.get("summary", "") or "").strip()
            if action_type == "skill":
                skill_name = str(step.result.payload.get("name", "") or step.action.get("name", "") or "").strip()
                skill_mode = str(step.result.payload.get("mode", "") or step.action.get("mode", "") or "").strip()
                payload_skills = step.result.payload.get("skills", [])
                if isinstance(payload_skills, list):
                    skill_count = len(payload_skills)
                if skill_name:
                    summary = f"Loaded skill {skill_name}" + (f" (mode={skill_mode})" if skill_mode else ".")
                elif skill_count > 0:
                    summary = f"Listed {skill_count} available skill(s)."
            if action_type == "patch_file":
                diff = str(step.result.payload.get("diff", "") or "").strip()
                replacements = int(step.result.payload.get("replacements", 0) or 0)
            if action_type == "inspect_files":
                payload_files = step.result.payload.get("files", [])
                if isinstance(payload_files, list):
                    inspected_file_count = int(step.result.payload.get("count", len(payload_files)) or len(payload_files))
                    for item in payload_files:
                        if not isinstance(item, dict):
                            continue
                        inspected_files.append({
                            "path": str(item.get("path", "") or "").strip(),
                            "start_line": int(item.get("start_line", 0) or 0) or None,
                            "end_line": int(item.get("end_line", 0) or 0) or None,
                            "ok": bool(item.get("ok", False)),
                            "error": str((item.get("error") or {}).get("message", "") or "").strip() if isinstance(item.get("error"), dict) else "",
                        })
                if not inspected_files:
                    action_files = step.action.get("files", [])
                    if isinstance(action_files, list):
                        inspected_file_count = len(action_files)
                        for item in action_files:
                            if not isinstance(item, dict):
                                continue
                            start_line = int(item.get("start_line", 0) or 0) or None
                            end_line = int(item.get("end_line", 0) or 0) or None
                            inspected_files.append({
                                "path": str(item.get("path", "") or "").strip(),
                                "start_line": start_line,
                                "end_line": end_line,
                                "ok": True,
                                "error": "",
                            })
        if action_type == "patch_file":
            search_excerpt = shorten(str(step.action.get("search", "") or "").strip(), 240)
            replace_excerpt = shorten(str(step.action.get("replace", "") or "").strip(), 240)
            if diff:
                for line in diff.splitlines():
                    if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
                        continue
                    if line.startswith("+"):
                        added_lines += 1
                    elif line.startswith("-"):
                        removed_lines += 1
        _emit_bridge_message({
            "type": "progress",
            "domain": domain or "worker",
            "step": step.step,
            "action_type": action_type,
            "path": path,
            "ok": step.result.ok,
            "elapsed_s": round(step.elapsed_s, 2),
            "thought": thought[:200] if thought else "",
            "summary": summary[:200] if summary else "",
            "skill_name": skill_name,
            "skill_mode": skill_mode,
            "skill_count": skill_count,
            "diff": shorten(diff, 4000) if diff else "",
            "replacements": replacements,
            "added_lines": added_lines,
            "removed_lines": removed_lines,
            "search_excerpt": search_excerpt,
            "replace_excerpt": replace_excerpt,
            "inspected_file_count": inspected_file_count,
            "inspected_files": inspected_files,
            "state": safe_bridge_state(),
        })

    def _make_worker() -> "WorkingFolderAgent":
        worker = create_working_folder_agent(model_holder["client"].clone(), config)
        worker.on_step_callback = lambda step, worker=worker: _make_step_callback(
            step,
            str(getattr(worker, "bridge_progress_domain", "") or "worker"),
        )
        return worker

    planner = PlannerAgent(
        model_client=model_holder["client"],
        config=config,
        worker_factory=_make_worker,
        json_loader=extract_first_json_object,
    )
    # Also set callback on the default worker created during __init__
    if hasattr(planner, "worker") and planner.worker is not None:
        planner.worker.on_step_callback = lambda step, worker=planner.worker: _make_step_callback(
            step,
            str(getattr(worker, "bridge_progress_domain", "") or "worker"),
        )

    def _goal_callback(event: str, index: int, goal_id: str, title: str) -> None:
        _emit_bridge_message({
            "type": event,
            "domain": "plan",
            "goal_index": index,
            "goal_id": goal_id,
            "goal_title": title,
            "state": safe_bridge_state(),
        })

    planner.on_goal_callback = _goal_callback

    def _discovery_callback(event: str, mode: str) -> None:
        _emit_bridge_message({
            "type": "progress",
            "domain": "discovery",
            "step": 0,
            "action_type": event,
            "path": "",
            "ok": True,
            "elapsed_s": 0,
            "thought": f"Discovery {event.replace('_', ' ')}: {mode}",
            "summary": f"Discovery {event.replace('_', ' ')} ({mode}).",
            "diff": "",
            "replacements": 0,
            "added_lines": 0,
            "removed_lines": 0,
            "search_excerpt": "",
            "replace_excerpt": "",
            "inspected_file_count": 0,
            "inspected_files": [],
            "state": safe_bridge_state(),
        })

    planner.on_discovery_callback = _discovery_callback

    def _plan_callback(event: str, payload: Dict[str, Any]) -> None:
        summary = str(payload.get("summary", "") or "").strip()
        goal_count = int(payload.get("goal_count", 0) or 0)
        status = str(payload.get("status", "") or "").strip()
        execution_summary = str(payload.get("execution_summary", "") or "").strip()
        meta_bits: List[str] = []
        if goal_count > 0:
            meta_bits.append(f"{goal_count} goal{'s' if goal_count != 1 else ''}")
        if status:
            meta_bits.append(status)
        thought = event.replace("_", " ").strip().title()
        if summary:
            thought = f"{thought}: {summary}"
        _emit_bridge_message({
            "type": "progress",
            "domain": "plan",
            "step": 0,
            "action_type": event,
            "path": "",
            "ok": status != "failed",
            "elapsed_s": 0,
            "thought": thought,
            "summary": execution_summary or " ".join(meta_bits).strip() or thought,
            "diff": "",
            "replacements": 0,
            "added_lines": 0,
            "removed_lines": 0,
            "search_excerpt": "",
            "replace_excerpt": "",
            "inspected_file_count": 0,
            "inspected_files": [],
            "state": safe_bridge_state(),
        })

    planner.on_plan_callback = _plan_callback

    def add_exchange(role: str, content: str) -> None:
        text = str(content or "").strip()
        if not text:
            return
        transcript.append({"role": role, "content": text})

    def summarize_bridge_action_result(action: Dict[str, Any], result: ActionResult) -> str:
        action_type = str(action.get("type", "") or result.name or "action")
        payload = result.payload if isinstance(result.payload, dict) else {}
        prefix = "Action failed" if not result.ok else "Action completed"
        if isinstance(payload.get("error"), dict):
            detail = str(payload["error"].get("message", "") or payload["error"].get("code", "")).strip()
            if detail:
                return f"{prefix}: {action_type}. {detail}"
        for key in ["message", "summary"]:
            detail = str(payload.get(key, "") or "").strip()
            if detail:
                return f"{prefix}: {action_type}. {detail}"
        path = str(action.get("path", "") or payload.get("path", "") or "").strip()
        if path:
            return f"{prefix}: {action_type} on {path}."
        return f"{prefix}: {action_type}."

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        request_id = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("Bridge request must be a JSON object.")
            request_id = request.get("id")
            request_type = str(request.get("type", "") or "").strip()
            message = ""

            if request_type == "initialize":
                _emit_bridge_message({"id": request_id, "ok": True, "state": safe_bridge_state(), "message": "initialized"})
                continue

            if request_type == "reconfigure_runtime":
                provider = str(request.get("provider", "") or config.provider).strip().lower()
                model = str(request.get("model", "") or config.model).strip()
                if not model:
                    raise ValueError("reconfigure_runtime requires a non-empty model")
                next_client = create_model_client(
                    provider=provider,
                    model=model,
                    thinking_mode=config.thinking_mode,
                    verbosity=config.verbosity,
                )
                model_holder["client"] = next_client
                updated = planner.reconfigure_runtime(
                    model_client=next_client,
                    provider=provider,
                    model=model,
                    thinking_mode=config.thinking_mode,
                    verbosity=config.verbosity,
                )
                message = f"Runtime updated to {updated['provider']} / {updated['model']}"
                _emit_bridge_message({"id": request_id, "ok": True, "state": safe_bridge_state(message), "message": message})
                continue

            if request_type == "configure_backoff":
                enabled = bool(request.get("enabled", False))
                token_limit_k = int(request.get("token_limit_k", 0) or 0)
                worker = getattr(planner, "worker", None)
                if worker is not None and hasattr(worker, "configure_backoff"):
                    worker.configure_backoff(enabled=enabled, token_limit_k=token_limit_k)
                # also apply to planner model client
                planner_client = model_holder.get("client")
                if planner_client is not None:
                    planner_backoff = getattr(planner_client, "backoff", None)
                    if planner_backoff is not None and isinstance(planner_backoff, BackoffStrategy):
                        planner_backoff.enabled = enabled
                        planner_backoff.token_limit_k = max(0, token_limit_k)
                bs = worker.get_backoff_state() if worker is not None and hasattr(worker, "get_backoff_state") else {"enabled": enabled, "token_limit_k": token_limit_k, "window_tokens_used": 0}
                message = f"Backoff {'enabled' if enabled else 'disabled'}" + (f" ({token_limit_k}k/min)" if enabled else "")
                _emit_bridge_message({"id": request_id, "ok": True, "state": safe_bridge_state(message), "backoff": bs, "message": message})
                continue

            if request_type == "runtime_options":
                _emit_bridge_message(
                    {
                        "id": request_id,
                        "ok": True,
                        "state": safe_bridge_state(),
                        "message": "runtime options",
                        "runtime_options": runtime_options_payload(
                            current_provider=getattr(planner.config, "provider", config.provider),
                            current_model=getattr(planner.config, "model", config.model),
                        ),
                    }
                )
                continue

            if request_type == "submit":
                text = str(request.get("text", "") or "").strip()
                if not text:
                    raise ValueError("submit requires non-empty text")
                add_exchange("user", text)
                if planner.session.pending_plan is None and not planner.session.intake_messages:
                    message = planner.start_request(text)
                else:
                    message = planner.continue_conversation(text)
                # If a builtin command cleared the session, sync the transcript.
                if text.strip().lower() in {"/reset", "reset"}:
                    transcript.clear()
                add_exchange("assistant", message)
                _emit_bridge_message({"id": request_id, "ok": True, "state": safe_bridge_state(message), "message": message})
                continue

            if request_type == "planner_action":
                def _emit_planner_progress(payload: Dict[str, Any]) -> None:
                    packet = {"type": "progress", **payload}
                    packet["state"] = safe_bridge_state()
                    _emit_bridge_message(packet)

                message = _handle_bridge_planner_action(
                    planner=planner,
                    transcript=transcript,
                    request=request,
                    add_exchange=add_exchange,
                    emit_progress=_emit_planner_progress,
                )
                _emit_bridge_message({"id": request_id, "ok": True, "state": safe_bridge_state(message), "message": message})
                continue

            if request_type == "worker_action":
                action = request.get("action")
                if not isinstance(action, dict):
                    raise ValueError("worker_action requires an action object")
                worker = getattr(planner, "worker", None)
                executor = getattr(worker, "execute_operator_action", None)
                if not callable(executor):
                    raise ValueError("Worker action bridge is unavailable")
                result = executor(action, thought="Operator action from extension UI.")
                if not isinstance(result, ActionResult):
                    raise ValueError("Worker action bridge returned an invalid result")
                message = summarize_bridge_action_result(action, result)
                add_exchange("assistant", message)
                _emit_bridge_message(
                    {
                        "id": request_id,
                        "ok": result.ok,
                        "state": safe_bridge_state(message),
                        "message": message,
                    }
                )
                continue

            raise ValueError(f"Unsupported bridge request type: {request_type}")
        except Exception as exc:
            _emit_bridge_message(
                {
                    "id": request_id,
                    "ok": False,
                    "message": str(exc),
                    "state": safe_bridge_state(),
                }
            )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if not root.exists():
        eprint(f"Root does not exist: {root}")
        return 2
    if not root.is_dir():
        eprint(f"Root is not a directory: {root}")
        return 2

    try:
        refresh_runtime_provider_catalog_once()
        model_client = build_model_client(args)
        default_tools = Path(__file__).parent / "agent_tools.py"
        tool_path = Path(args.tools).expanduser().resolve() if getattr(args, "tools", None) else default_tools.resolve()

        config = build_agent_config(args, root, tool_path)
        if args.extension_bridge:
            return _run_extension_bridge(args)
        if getattr(args, "delete_session", False):
            agent = create_working_folder_agent(model_client, config)
            print(agent.delete_session())
            return 0
        agent = create_working_folder_agent(model_client, config)

        if args.worker_mode:
            interactive_worker_loop(agent)
            return 0

        from planner import PlannerAgent, interactive_planner_loop

        planner_model_holder: Dict[str, BaseModelClient] = {"client": model_client}

        def _make_planner_worker() -> WorkingFolderAgent:
            return create_working_folder_agent(planner_model_holder["client"].clone(), config)

        def _reconfigure_planner_runtime(provider: str, model: str) -> Dict[str, Any]:
            next_client = create_model_client(
                provider=provider,
                model=model,
                thinking_mode=config.thinking_mode,
                verbosity=config.verbosity,
            )
            planner_model_holder["client"] = next_client
            return planner.reconfigure_runtime(
                model_client=next_client,
                provider=provider,
                model=model,
                thinking_mode=config.thinking_mode,
                verbosity=config.verbosity,
            )

        def _configure_backoff(action: str) -> Dict[str, Any]:
            worker = planner.worker
            if action in {"off", "0", "false", "disable"}:
                result = worker.configure_backoff(enabled=False, token_limit_k=0)
                # also apply to planner model client
                planner_backoff = getattr(planner_model_holder["client"], "backoff", None)
                if planner_backoff is not None and isinstance(planner_backoff, BackoffStrategy):
                    planner_backoff.enabled = False
                    planner_backoff.token_limit_k = 0
                return result
            if action in {"", "show"}:
                return worker.get_backoff_state()
            try:
                limit_k = int(action)
                result = worker.configure_backoff(enabled=True, token_limit_k=limit_k)
                # also apply to planner model client
                planner_backoff = getattr(planner_model_holder["client"], "backoff", None)
                if planner_backoff is not None and isinstance(planner_backoff, BackoffStrategy):
                    planner_backoff.enabled = True
                    planner_backoff.token_limit_k = limit_k
                return result
            except (ValueError, TypeError):
                return worker.get_backoff_state()

        planner = PlannerAgent(
            model_client=planner_model_holder["client"],
            config=config,
            worker_factory=_make_planner_worker,
            json_loader=extract_first_json_object,
        )
        interactive_planner_loop(
            planner,
            worker_debug_loop=interactive_worker_loop,
            runtime_reconfigure=_reconfigure_planner_runtime,
            backoff_configure=_configure_backoff,
        )
        return 0
    except Exception as exc:
        eprint(f"fatal: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
