from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import shutil
import sys
import textwrap
import time
from dataclasses import dataclass, field, replace as _dataclass_replace
from context_compaction import serialize_prompt
from issue_facts import IssueFactLedger
from pathlib import Path
from planner_control import (
    final_summary_instructions,
    next_goal_guidance_instructions,
    parse_final_summary_response,
    parse_next_goal_guidance_response,
    parse_planner_intake_response,
    planner_intake_format_instructions,
    use_tagged_planner_control,
)
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple
from runtime_catalog import runtime_model_lines, runtime_provider_lines


class JsonLoader(Protocol):
    def __call__(self, text: str) -> Dict[str, Any]:
        ...


class PlannerWorker(Protocol):
    history: List[Any]
    root: Path
    on_step_callback: Optional[Callable[[Any], None]]

    def set_steering(self, prompt: str) -> None:
        ...

    def clear_steering(self) -> None:
        ...

    def set_goal_fact_keys(self, keys: List[str]) -> None:
        ...

    def clear_goal_fact_keys(self) -> None:
        ...

    def configure_discovery_budget(self, mode_key: str, mode_label: str, max_tool_calls: int) -> None:
        ...

    def clear_discovery_budget(self) -> None:
        ...

    def configure_backoff(self, *, enabled: bool, token_limit_k: int = 0) -> Dict[str, Any]:
        ...

    def get_backoff_state(self) -> Dict[str, Any]:
        ...

    def render_last_usage_summary(self) -> str:
        ...

    def run_task(self, task: str) -> Any:
        ...

    def prepare_for_goal(self, preserve_context: bool) -> None:
        ...

    def ensure_issue_for_plan(self, *, original_request: str, plan_summary: str, reuse_issue_id: str = "") -> Dict[str, Any]:
        ...

    def close_active_issue(self, *, note: str = "") -> Optional[Dict[str, Any]]:
        ...

    def close_issue(self, issue_id: str, *, note: str = "") -> Dict[str, Any]:
        ...

    def reopen_issue(self, issue_id: str) -> Dict[str, Any]:
        ...

    def create_issue(
        self,
        *,
        request_summary: str,
        plan_summary: str = "",
        source: str = "",
        parent_issue_id: str = "",
        source_excerpt: str = "",
        priority: int = 0,
        activate: bool = True,
    ) -> Dict[str, Any]:
        ...

    def delete_session(self) -> str:
        ...


@dataclass(frozen=True)
class DiscoveryMode:
    key: str
    label: str
    description: str
    scan_expectation: str
    max_tool_calls: int


DISCOVERY_MODES: Dict[str, DiscoveryMode] = {
    "quick": DiscoveryMode(
        key="quick",
        label="Quick Scan",
        description="Fast pass over the most likely files and repo structure.",
        scan_expectation="Inspect only the highest-yield files and likely entrypoints. Stay under 6 tool calls, then finish with a concise summary for the planner.",
        max_tool_calls=6,
    ),
    "moderate": DiscoveryMode(
        key="moderate",
        label="Moderate Scan",
        description="Broader inspection of relevant files, flows, and likely constraints.",
        scan_expectation="Inspect the main flow, adjacent supporting files, and likely implementation constraints. Stay under 12 tool calls, then finish with a concise summary for the planner.",
        max_tool_calls=12,
    ),
    "deep": DiscoveryMode(
        key="deep",
        label="Deep Scan",
        description="Thorough repo investigation before planning or execution.",
        scan_expectation="Do a thorough exploration of the relevant architecture, supporting files, risks, and edge conditions. Stay under 15 tool calls, then finish with a concise summary for the planner.",
        max_tool_calls=15,
    ),
}
REPO_FACTS_FILENAME = "repo_facts.md"
REPO_FACTS_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


@dataclass
class PlannerGoal:
    goal_id: str
    title: str
    goal: str
    reason: str
    depends_on: List[str] = field(default_factory=list)
    preserve_context: bool = False
    parallelizable: bool = False
    estimated_scope: str = "mixed"
    delegation_notes: List[str] = field(default_factory=list)
    success_signals: List[str] = field(default_factory=list)
    relevant_fact_keys: List[str] = field(default_factory=list)


@dataclass
class PlannerPlan:
    original_request: str
    summary: str
    assumptions: List[str] = field(default_factory=list)
    clarification_summary: str = ""
    goals: List[PlannerGoal] = field(default_factory=list)
    not_in_scope: List[str] = field(default_factory=list)
    next_steps_preview: List[str] = field(default_factory=list)
    confirmation_prompt: str = "Approve this plan to start execution."


@dataclass
class GoalExecutionResult:
    goal_id: str
    title: str
    delegated_task: str
    final_message: str
    worker_history_summary: List[Dict[str, Any]] = field(default_factory=list)
    touched_paths: List[str] = field(default_factory=list)
    preserve_context_used: bool = False
    duration_s: float = 0.0
    commentary_for_next_goal: str = ""
    status: str = "completed"
    usage_summary: str = ""
    task_satisfied: bool = False
    validation_ran: bool = False
    validation_passed: bool = False
    validation_summary: str = ""


@dataclass
class DiscoveryRequest:
    reason: str
    prompt: str
    recommended_mode: str = "moderate"


@dataclass
class DiscoveryResult:
    mode: str
    delegated_task: str
    final_message: str
    reason: str = ""
    prompt: str = ""
    tool_calls_used: int = 0
    tool_calls_max: int = 0
    usage_summary: str = ""
    worker_history_summary: List[Dict[str, Any]] = field(default_factory=list)
    touched_paths: List[str] = field(default_factory=list)
    duration_s: float = 0.0
    ok: bool = False
    task_satisfied: bool = False
    validation_ran: bool = False
    validation_passed: bool = False
    validation_summary: str = ""


@dataclass
class ProjectIntent:
    path: str
    content: str = ""
    sha256: str = ""
    present: bool = False


@dataclass
class ContinuousModeConfig:
    enabled: bool = False
    max_cycles: int = 1
    max_consecutive_failures: int = 1
    auto_approve: bool = True
    review_required: bool = True


@dataclass
class ContinuousRunState:
    enabled: bool = False
    status: str = "idle"
    cycle: int = 0
    max_cycles: int = 0
    active_issue_id: str = ""
    selected_discovery_mode: str = ""
    latest_review_decision: str = ""
    stop_reason: str = ""
    created_followup_issue_ids: List[str] = field(default_factory=list)


@dataclass
class PlanApprovalDecision:
    approved: bool
    reasons: List[str] = field(default_factory=list)
    blocking_reasons: List[str] = field(default_factory=list)


@dataclass
class ReviewModeResult:
    decision: str
    evidence: List[str] = field(default_factory=list)
    required_actions: List[str] = field(default_factory=list)
    followup_issue_ids: List[str] = field(default_factory=list)


def _discovery_findings_lines(result: Optional[DiscoveryResult], *, limit: int = 6) -> List[str]:
    if result is None:
        return []
    findings: List[str] = []
    seen: Dict[str, None] = {}
    summary = str(result.final_message or "").strip()
    if summary:
        findings.append(summary)
        seen[summary] = None
    for item in result.worker_history_summary[:limit]:
        action_type = str(item.get("action_type") or "").strip()
        summary_text = str(item.get("summary") or "").strip()
        path = str(item.get("path") or "").strip()
        parts = [part for part in [action_type, path, summary_text] if part]
        if not parts:
            continue
        line = " | ".join(parts)
        if line in seen:
            continue
        findings.append(line)
        seen[line] = None
    return findings[:limit]


@dataclass
class PlannerSession:
    intake_messages: List[Dict[str, str]] = field(default_factory=list)
    pending_plan: Optional[PlannerPlan] = None
    last_presented_plan: Optional[PlannerPlan] = None
    last_completed_plan: Optional[PlannerPlan] = None
    last_completed_results: List[GoalExecutionResult] = field(default_factory=list)
    last_execution_summary: str = ""
    awaiting_plan_revision: bool = False
    plan_revision_feedback: List[str] = field(default_factory=list)
    pending_discovery: Optional[DiscoveryRequest] = None
    last_discovery: Optional[DiscoveryResult] = None
    completed_results: List[GoalExecutionResult] = field(default_factory=list)
    latest_request: str = ""
    planner_usage_totals: Dict[str, int] = field(default_factory=dict)
    executing: bool = False
    executing_goal_index: int = -1
    executing_goal_id: str = ""
    executing_goal_title: str = ""
    executing_goal_count: int = 0
    active_issue_id: str = ""
    defer_issue_close_for_review: bool = False


class PlannerAgent:
    def __init__(
        self,
        model_client: Any,
        config: Any,
        worker_factory: Callable[[], PlannerWorker],
        json_loader: JsonLoader,
    ) -> None:
        self.model = model_client
        self.config = config
        self.root = Path(config.root).resolve()
        self.worker_factory = worker_factory
        self.worker = worker_factory()
        self.json_loader = json_loader
        self.session = PlannerSession()
        self.on_goal_callback: Optional[Callable[[str, int, str, str], None]] = None
        self.on_discovery_callback: Optional[Callable[[str, str], None]] = None
        self.on_plan_callback: Optional[Callable[[str, Dict[str, Any]], None]] = None
        self.continuous_config = ContinuousModeConfig()
        self.continuous_state = ContinuousRunState()

    def reconfigure_runtime(
        self,
        *,
        model_client: Any,
        provider: str,
        model: str,
        thinking_mode: Optional[str] = None,
        verbosity: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.model = model_client
        self.config.provider = str(provider or self.config.provider).strip().lower()
        self.config.model = str(model or self.config.model).strip() or self.config.model
        if thinking_mode is not None:
            self.config.thinking_mode = str(thinking_mode).strip() or self.config.thinking_mode
        if verbosity is not None:
            self.config.verbosity = str(verbosity).strip() or self.config.verbosity
        worker_reconfigure = getattr(self.worker, "reconfigure_runtime", None)
        if callable(worker_reconfigure):
            worker_reconfigure(
                provider=self.config.provider,
                model=self.config.model,
                thinking_mode=self.config.thinking_mode,
                verbosity=self.config.verbosity,
            )
        return {
            "provider": self.config.provider,
            "model": self.config.model,
            "thinking_mode": self.config.thinking_mode,
            "verbosity": self.config.verbosity,
        }

    def _load_repo_facts_payload(self) -> Optional[Dict[str, Any]]:
        path = self.root / REPO_FACTS_FILENAME
        if not path.exists() or not path.is_file():
            return None
        ledger = IssueFactLedger.load(path)
        return ledger.planner_payload(path=str(path))

    def _load_project_intent(self) -> ProjectIntent:
        path = self.root / "INTENT.md"
        if not path.exists() or not path.is_file():
            return ProjectIntent(path=str(path), present=False)
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            return ProjectIntent(path=str(path), present=False)
        import hashlib
        return ProjectIntent(
            path=str(path),
            content=content,
            sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            present=True,
        )

    def _load_project_intent_payload(self) -> Dict[str, Any]:
        intent = self._load_project_intent()
        return {
            "path": intent.path,
            "present": intent.present,
            "sha256": intent.sha256,
            "content": intent.content,
            "immutable": True,
        }

    def _available_repo_fact_keys(self) -> List[str]:
        payload = self._load_repo_facts_payload() or {}
        facts = payload.get("context_facts") if isinstance(payload, dict) else None
        if not isinstance(facts, list):
            return []
        keys: List[str] = []
        for item in facts:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key", "") or "").strip()
            if key and key not in keys:
                keys.append(key)
        return keys

    def _normalize_relevant_fact_keys(self, values: Any) -> List[str]:
        available = set(self._available_repo_fact_keys())
        selected: List[str] = []
        for value in values or []:
            key = str(value or "").strip()
            if not key or key in selected:
                continue
            if available and key not in available:
                continue
            selected.append(key)
        return selected

    def repo_facts_status_lines(self) -> List[str]:
        path = self.root / REPO_FACTS_FILENAME
        if not path.exists() or not path.is_file():
            return ["repo_facts : none"]
        payload = self._load_repo_facts_payload()
        if payload is None:
            return [
                "repo_facts : present but unreadable",
                f"facts_path  : {path}",
            ]
        count = int(payload.get("total_fact_count", 0) or 0)
        lines = [
            f"repo_facts : loaded {count}",
            f"facts_path  : {payload.get('path', str(path))}",
            f"schema      : v{payload.get('schema_version', '?')}",
        ]
        active_issue = payload.get("active_issue") if isinstance(payload, dict) else None
        if isinstance(active_issue, dict) and str(active_issue.get("issue_id", "") or "").strip():
            lines.append(f"active_issue: {active_issue.get('issue_id')}")
        return lines

    def _active_issue_id_from_repo_facts(self) -> str:
        payload = self._load_repo_facts_payload() or {}
        if not isinstance(payload, dict):
            return ""
        active_issue = payload.get("active_issue")
        if not isinstance(active_issue, dict):
            return ""
        return str(active_issue.get("issue_id", "") or "").strip()

    def _rehydrate_active_issue_context(self) -> None:
        active_issue_id = self._active_issue_id_from_repo_facts()
        self.session.active_issue_id = active_issue_id

    def try_builtin_command(self, text: str) -> Optional[str]:
        """Recognise built-in slash-commands (``/reset``, ``/reopen <id>``,
        ``reopen <id>``) that can come from either the CLI or the extension
        submit path.  Returns the response string when matched, or ``None``
        so the caller can fall through to normal request handling."""
        stripped = text.strip()
        lower = stripped.lower()
        if lower in {"/reset", "reset"}:
            self.clear_session()
            return "Planner session cleared."
        if lower in {"/delete-session", "delete-session"}:
            return self.delete_session()
        if lower.startswith("reopen "):
            issue_id = stripped[len("reopen "):].strip()
            if issue_id:
                return self.reopen_issue(issue_id)
        if lower.startswith("/reopen "):
            issue_id = stripped[len("/reopen "):].strip()
            if issue_id:
                return self.reopen_issue(issue_id)
        if lower.startswith("close-issue "):
            issue_id = stripped[len("close-issue "):].strip()
            if issue_id:
                return self.close_issue(issue_id)
        if lower.startswith("/close-issue "):
            issue_id = stripped[len("/close-issue "):].strip()
            if issue_id:
                return self.close_issue(issue_id)
        if lower.startswith("close "):
            issue_id = stripped[len("close "):].strip()
            if issue_id:
                return self.close_issue(issue_id)
        if lower.startswith("/close "):
            issue_id = stripped[len("/close "):].strip()
            if issue_id:
                return self.close_issue(issue_id)
        return None

    def start_request(self, user_request: str) -> str:
        builtin = self.try_builtin_command(user_request)
        if builtin is not None:
            return builtin
        self.session.latest_request = user_request.strip()
        self.session.intake_messages = [{"role": "user", "content": user_request.strip()}]
        self.session.pending_plan = None
        self.session.last_presented_plan = None
        self.session.last_completed_plan = None
        self.session.last_completed_results = []
        self.session.last_execution_summary = ""
        self.session.awaiting_plan_revision = False
        self.session.plan_revision_feedback = []
        self.session.pending_discovery = None
        self.session.last_discovery = None
        self.session.completed_results = []
        self.session.planner_usage_totals = {}
        self.session.defer_issue_close_for_review = False
        self._rehydrate_active_issue_context()
        return self._handle_intake_turn()

    def continue_conversation(self, user_message: str) -> str:
        text = user_message.strip()
        if not text:
            return "Input cannot be empty."

        builtin = self.try_builtin_command(text)
        if builtin is not None:
            return builtin

        if self.session.pending_plan is not None:
            if self._is_approval(text):
                pending = self.session.pending_plan
                self._fire_plan_callback(
                    "plan_approved",
                    {
                        "summary": str(getattr(pending, "summary", "") or ""),
                        "goal_count": len(getattr(pending, "goals", []) or []),
                    },
                )
                return self.execute_pending_plan()
            if self._is_rejection(text):
                self.session.intake_messages.append({"role": "user", "content": text})
                pending = self.session.pending_plan
                self.session.last_presented_plan = self.session.pending_plan
                self.session.pending_plan = None
                self.session.awaiting_plan_revision = True
                self._fire_plan_callback(
                    "plan_rejected",
                    {
                        "summary": str(getattr(pending, "summary", "") or ""),
                        "feedback": text,
                    },
                )
                return "Plan rejected. Describe what should change and I will revise it."
            self.session.intake_messages.append({"role": "user", "content": text})
            pending = self.session.pending_plan
            self.session.last_presented_plan = self.session.pending_plan
            self.session.pending_plan = None
            self.session.awaiting_plan_revision = True
            self.session.plan_revision_feedback.append(text)
            self._fire_plan_callback(
                "plan_revision_requested",
                {
                    "summary": str(getattr(pending, "summary", "") or ""),
                    "feedback": text,
                },
            )
            return self._handle_intake_turn()

        if self.session.pending_discovery is not None:
            mode = self._parse_discovery_selection(text)
            if mode is not None:
                self.session.intake_messages.append({"role": "user", "content": text})
                return self.execute_discovery(mode)
            if self._is_rejection(text):
                self.session.intake_messages.append({"role": "user", "content": text})
                self.session.pending_discovery = None
                return "Discovery skipped. Add more detail or let me know what should be assumed instead."
            self.session.intake_messages.append({"role": "user", "content": text})
            self.session.pending_discovery = None
            return self._handle_intake_turn()

        if self.session.awaiting_plan_revision and self.session.last_presented_plan is not None:
            self.session.intake_messages.append({"role": "user", "content": text})
            self.session.plan_revision_feedback.append(text)
            return self._handle_intake_turn()

        if self.session.last_completed_plan is not None:
            self._start_follow_up_request(text)
            return self._handle_intake_turn()

        self.session.intake_messages.append({"role": "user", "content": text})
        return self._handle_intake_turn()

    def show_pending_plan(self) -> str:
        if self.session.pending_plan is None:
            return "No pending plan."
        return self._render_plan(self.session.pending_plan)

    def show_pending_discovery(self) -> str:
        if self.session.pending_discovery is None:
            return "No pending discovery offer."
        return self._render_discovery_offer(self.session.pending_discovery)

    def clear_session(self) -> None:
        self.session = PlannerSession()
        try:
            self.worker.clear_steering()
        except Exception:
            pass

    def delete_session(self) -> str:
        delete_session = getattr(self.worker, "delete_session", None)
        if callable(delete_session):
            try:
                message = str(delete_session() or "Session deleted.").strip()
            except Exception as exc:
                return f"Delete session failed: {exc}"
        else:
            message = "Session deleted."
        self.clear_session()
        return message or "Session deleted."

    def reopen_issue(self, issue_id: str) -> str:
        normalized_issue_id = str(issue_id or "").strip()
        if not normalized_issue_id:
            return "Issue id is required to reopen issue context."
        reopen = getattr(self.worker, "reopen_issue", None)
        if not callable(reopen):
            return "Worker does not support issue reopening."
        try:
            issue = reopen(normalized_issue_id)
        except Exception as exc:
            return f"Issue reopen failed: {exc}"
        self.session.intake_messages = []
        self.session.latest_request = ""
        self.session.pending_plan = None
        self.session.last_presented_plan = None
        self.session.awaiting_plan_revision = False
        self.session.plan_revision_feedback = []
        self.session.pending_discovery = None
        self.session.last_discovery = None
        self.session.executing = False
        self.session.executing_goal_index = -1
        self.session.executing_goal_id = ""
        self.session.executing_goal_title = ""
        self.session.executing_goal_count = 0
        self.session.last_execution_summary = ""
        self.session.last_completed_plan = None
        self.session.last_completed_results = []
        self.session.completed_results = []
        self.session.planner_usage_totals = {}
        self.session.active_issue_id = str((issue or {}).get("issue_id", normalized_issue_id) or normalized_issue_id)
        summary = str((issue or {}).get("plan_summary", "") or "").strip()
        if summary:
            return f"Reopened issue {self.session.active_issue_id}: {summary}. Send the follow-up request for this issue."
        return f"Reopened issue {self.session.active_issue_id}. Send the follow-up request for this issue."

    def close_issue(self, issue_id: str) -> str:
        normalized_issue_id = str(issue_id or "").strip()
        if not normalized_issue_id:
            return "Issue id is required to close issue context."
        close_issue = getattr(self.worker, "close_issue", None)
        close_active_issue = getattr(self.worker, "close_active_issue", None)
        try:
            if callable(close_issue):
                issue = close_issue(normalized_issue_id, note="Closed manually from the VS Code Issues panel.")
            elif callable(close_active_issue):
                active_issue_id = str(self.session.active_issue_id or "").strip()
                if active_issue_id and active_issue_id != normalized_issue_id:
                    return f"Issue close failed: active issue is {active_issue_id}, not {normalized_issue_id}."
                issue = close_active_issue(note="Closed manually from the VS Code Issues panel.")
            else:
                return "Worker does not support issue closing."
        except Exception as exc:
            return f"Issue close failed: {exc}"
        issue_id_closed = str((issue or {}).get("issue_id", normalized_issue_id) or normalized_issue_id)
        self.session.intake_messages = []
        self.session.latest_request = ""
        self.session.pending_plan = None
        self.session.last_presented_plan = None
        self.session.awaiting_plan_revision = False
        self.session.plan_revision_feedback = []
        self.session.pending_discovery = None
        self.session.last_discovery = None
        self.session.executing = False
        self.session.executing_goal_index = -1
        self.session.executing_goal_id = ""
        self.session.executing_goal_title = ""
        self.session.executing_goal_count = 0
        self.session.last_execution_summary = ""
        self.session.last_completed_plan = None
        self.session.last_completed_results = []
        self.session.completed_results = []
        self.session.planner_usage_totals = {}
        self.session.active_issue_id = ""
        return f"Closed issue {issue_id_closed}. It will stay out of active context until reopened."

    def _start_follow_up_request(self, user_request: str) -> None:
        self.session.latest_request = user_request.strip()
        self.session.intake_messages = [{"role": "user", "content": user_request.strip()}]
        self.session.pending_plan = None
        self.session.last_presented_plan = None
        self.session.last_completed_plan = None
        self.session.last_completed_results = []
        self.session.last_execution_summary = ""
        self.session.awaiting_plan_revision = False
        self.session.plan_revision_feedback = []
        self.session.pending_discovery = None
        self.session.last_discovery = None
        self.session.completed_results = []
        self.session.planner_usage_totals = {}
        self._rehydrate_active_issue_context()

    def _planner_complete(self, system: str, prompt: str) -> str:
        raw = self.model.complete(system, prompt)
        self._record_planner_usage()
        return raw

    def _record_planner_usage(self) -> None:
        if not hasattr(self.model, "get_last_metrics"):
            return
        metrics = self.model.get_last_metrics()
        if not isinstance(metrics, dict):
            return
        usage = metrics.get("usage")
        if not isinstance(usage, dict):
            return
        for key, value in usage.items():
            if isinstance(value, int):
                self.session.planner_usage_totals[key] = int(self.session.planner_usage_totals.get(key, 0)) + value

    def _render_planner_usage_summary(self) -> str:
        totals = self.session.planner_usage_totals
        if not totals:
            return "Planner usage: unavailable"

        estimator = getattr(self.worker, "usage_estimator", None)
        if estimator is not None and hasattr(estimator, "estimate") and hasattr(estimator, "render_cli_summary"):
            try:
                snapshot = estimator.estimate(
                    provider=str(self.config.provider),
                    model=str(self.config.model),
                    usage=totals,
                )
                rendered = str(estimator.render_cli_summary(snapshot)).strip()
                if rendered.startswith("Usage:"):
                    return "Planner " + rendered
                return "Planner usage: " + rendered
            except Exception:
                pass

        parts: List[str] = []
        for key in ["input_tokens", "output_tokens", "total_tokens", "prompt_token_count", "candidates_token_count", "total_token_count"]:
            value = totals.get(key)
            if isinstance(value, int) and value > 0:
                parts.append(f"{key}={value:,}")
        return "Planner usage: " + (" | ".join(parts) if parts else "unavailable")

    def _run_worker_task(
        self,
        *,
        worker: Optional[PlannerWorker] = None,
        task: str,
        steering: str,
        goal_fact_keys: List[str],
        preserve_context: bool,
        discovery_budget: Optional[DiscoveryMode] = None,
    ) -> Dict[str, Any]:
        target_worker = worker or self.worker
        progress_domain = "discovery" if discovery_budget is not None else "plan"
        try:
            setattr(target_worker, "bridge_progress_domain", progress_domain)
        except Exception:
            pass
        target_worker.prepare_for_goal(preserve_context=preserve_context)
        target_worker.set_goal_fact_keys(goal_fact_keys)
        target_worker.set_steering(steering)
        if discovery_budget is not None:
            target_worker.configure_discovery_budget(
                discovery_budget.key,
                discovery_budget.label,
                discovery_budget.max_tool_calls,
            )

        history_start = len(getattr(target_worker, "history", []))
        started = time.time()
        try:
            run_result = target_worker.run_task(task)
            elapsed = time.time() - started
            history_slice = list(getattr(target_worker, "history", [])[history_start:])
            budget_state = getattr(target_worker, "discovery_budget", None)
            return {
                "run_result": run_result,
                "elapsed": elapsed,
                "history_slice": history_slice,
                "budget_state": {
                    "tool_calls_used": int(getattr(budget_state, "tool_calls_used", 0) or 0),
                    "max_tool_calls": int(getattr(budget_state, "max_tool_calls", 0) or 0),
                },
            }
        finally:
            try:
                target_worker.clear_steering()
            finally:
                target_worker.clear_goal_fact_keys()
                target_worker.clear_discovery_budget()
                try:
                    setattr(target_worker, "bridge_progress_domain", "")
                except Exception:
                    pass
                # Clear stale blocker state (active_error, pending_verification,
                # etc.) so it doesn't leak into the bridge state between runs.
                for _cleanup_name in (
                    "_clear_patch_recovery",
                    "_clear_edit_batch_state",
                    "_clear_pending_verification",
                    "_clear_pending_fact_resolution",
                    "_clear_active_error",
                ):
                    _cleanup_fn = getattr(target_worker, _cleanup_name, None)
                    if callable(_cleanup_fn):
                        try:
                            _cleanup_fn()
                        except Exception:
                            pass

    def _coerce_worker_run_result(self, run_result: Any, history_slice: List[Dict[str, Any]] | List[Any]) -> Dict[str, Any]:
        if hasattr(run_result, "final_message") and hasattr(run_result, "ok"):
            validation = getattr(run_result, "validation", None)
            return {
                "ok": bool(getattr(run_result, "ok", False)),
                "final_message": str(getattr(run_result, "final_message", "") or ""),
                "task_satisfied": bool(getattr(run_result, "task_satisfied", False)),
                "validation_ran": bool(getattr(run_result, "validation_ran", False)),
                "validation_passed": bool(getattr(run_result, "validation_passed", False)),
                "touched_paths": [str(item) for item in getattr(run_result, "touched_paths", []) or [] if str(item)],
                "validation_summary": str(getattr(validation, "summary", "") or ""),
            }
        return {
            "ok": False,
            "final_message": str(run_result or "Worker returned no structured result."),
            "task_satisfied": False,
            "validation_ran": False,
            "validation_passed": False,
            "touched_paths": [],
            "validation_summary": "Worker returned an unstructured result.",
        }

    def _goal_is_parallel_safe(self, goal: PlannerGoal) -> bool:
        scope = str(goal.estimated_scope or "mixed").strip().lower()
        return bool(goal.parallelizable) and not goal.preserve_context and scope in {"read", "validation"}

    def _next_goal_batch(
        self,
        plan: PlannerPlan,
        completed_ids: set[str],
        failed: bool,
    ) -> List[Tuple[int, PlannerGoal]]:
        if failed:
            return []
        ready: List[Tuple[int, PlannerGoal]] = []
        for index, goal in enumerate(plan.goals, start=1):
            if goal.goal_id in completed_ids:
                continue
            if any(dep not in completed_ids for dep in goal.depends_on):
                continue
            ready.append((index, goal))
        if not ready:
            return []
        first_goal = ready[0][1]
        if not self._goal_is_parallel_safe(first_goal):
            return [ready[0]]
        return [item for item in ready if self._goal_is_parallel_safe(item[1])]

    def _fire_goal_callback(self, event: str, index: int, goal_id: str, title: str) -> None:
        if self.on_goal_callback is not None:
            try:
                self.on_goal_callback(event, index, goal_id, title)
            except Exception:
                pass

    def _fire_discovery_callback(self, event: str, mode: str) -> None:
        if self.on_discovery_callback is not None:
            try:
                self.on_discovery_callback(event, mode)
            except Exception:
                pass

    def _fire_plan_callback(self, event: str, payload: Optional[Dict[str, Any]] = None) -> None:
        if self.on_plan_callback is not None:
            try:
                self.on_plan_callback(event, dict(payload or {}))
            except Exception:
                pass

    def _execute_goal_with_worker(
        self,
        worker: PlannerWorker,
        plan: PlannerPlan,
        goal: PlannerGoal,
        index: int,
    ) -> GoalExecutionResult:
        steering = self._build_goal_steering(plan, goal, index)
        task = self._build_goal_task(plan, goal, index)
        try:
            execution = self._run_worker_task(
                worker=worker,
                task=task,
                steering=steering,
                goal_fact_keys=goal.relevant_fact_keys,
                preserve_context=goal.preserve_context,
            )
            worker_result = self._coerce_worker_run_result(execution["run_result"], execution["history_slice"])
            return GoalExecutionResult(
                goal_id=goal.goal_id,
                title=goal.title,
                delegated_task=task,
                final_message=worker_result["final_message"],
                usage_summary=str(worker.render_last_usage_summary() or "").strip(),
                worker_history_summary=self._summarize_worker_history(execution["history_slice"]),
                touched_paths=worker_result["touched_paths"] or self._collect_touched_paths(execution["history_slice"]),
                preserve_context_used=goal.preserve_context,
                duration_s=round(float(execution["elapsed"]), 3),
                status="completed" if worker_result["ok"] and worker_result["task_satisfied"] else "failed",
                task_satisfied=bool(worker_result["task_satisfied"]),
                validation_ran=bool(worker_result["validation_ran"]),
                validation_passed=bool(worker_result["validation_passed"]),
                validation_summary=str(worker_result["validation_summary"]),
            )
        except Exception as exc:
            return GoalExecutionResult(
                goal_id=goal.goal_id,
                title=goal.title,
                delegated_task=task,
                final_message=f"Worker execution failed: {exc}",
                usage_summary=str(worker.render_last_usage_summary() or "").strip(),
                preserve_context_used=goal.preserve_context,
                status="failed",
            )

    def execute_discovery(self, mode_key: str) -> str:
        request = self.session.pending_discovery
        mode = DISCOVERY_MODES.get(mode_key)
        if request is None or mode is None:
            return "No pending discovery to execute."

        steering = self._build_discovery_steering(request, mode)
        task = self._build_discovery_task(request, mode)
        self._fire_discovery_callback("discovery_start", mode.key)
        try:
            execution = self._run_worker_task(
                task=task,
                steering=steering,
                goal_fact_keys=[],
                preserve_context=False,
                discovery_budget=mode,
            )
            worker_result = self._coerce_worker_run_result(execution["run_result"], execution["history_slice"])
            # Models often finish discovery with a terse "Done." — recover the
            # substantive analysis from the last finish-step thought instead.
            raw_msg = worker_result["final_message"].strip()
            if len(raw_msg) < 40:
                thought = self._extract_last_finish_thought(execution["history_slice"])
                if thought and len(thought) > len(raw_msg):
                    worker_result["final_message"] = thought
            result = DiscoveryResult(
                mode=mode.key,
                delegated_task=task,
                final_message=worker_result["final_message"],
                reason=request.reason if request else "",
                prompt=request.prompt if request else "",
                tool_calls_used=int(execution.get("budget_state", {}).get("tool_calls_used", 0) or 0),
                tool_calls_max=int(execution.get("budget_state", {}).get("max_tool_calls", mode.max_tool_calls) or mode.max_tool_calls),
                usage_summary=str(self.worker.render_last_usage_summary() or "").strip(),
                worker_history_summary=self._summarize_worker_history(execution["history_slice"]),
                touched_paths=worker_result["touched_paths"] or self._collect_touched_paths(execution["history_slice"]),
                duration_s=round(float(execution["elapsed"]), 3),
                ok=bool(worker_result["ok"]),
                task_satisfied=bool(worker_result["task_satisfied"]),
                validation_ran=bool(worker_result["validation_ran"]),
                validation_passed=bool(worker_result["validation_passed"]),
                validation_summary=str(worker_result["validation_summary"]),
            )
        except Exception as exc:
            self.session.last_discovery = DiscoveryResult(
                mode=mode_key,
                delegated_task=task,
                final_message=f"Discovery failed: {exc}",
                reason=request.reason if request else "",
                ok=False,
            )
            self.session.pending_discovery = None
            self._fire_discovery_callback("discovery_finish", mode_key)
            return f"Discovery failed before planning could continue: {exc}"

        if not result.ok:
            self.session.last_discovery = result
            self.session.pending_discovery = None
            self._fire_discovery_callback("discovery_finish", result.mode)
            return self._render_discovery_result(result)

        self.session.last_discovery = result
        self.session.pending_discovery = None
        self._fire_discovery_callback("discovery_finish", result.mode)
        self._rehydrate_active_issue_context()

        plan_response = self._handle_intake_turn()
        discovery_summary = self._render_discovery_result(result)
        return "\n\n".join([discovery_summary, plan_response])

    def execute_pending_plan(self) -> str:
        plan = self.session.pending_plan
        if plan is None:
            return "No pending plan to execute."

        self._fire_plan_callback(
            "plan_execution_start",
            {
                "summary": plan.summary,
                "goal_count": len(plan.goals),
            },
        )
        self.session.completed_results = []
        self.session.executing = True
        self.session.executing_goal_count = len(plan.goals)
        output: List[str] = ["Executing confirmed plan."]
        completed_ids: set[str] = set()
        failed = False

        opener = getattr(self.worker, "ensure_issue_for_plan", None)
        if callable(opener):
            issue = opener(
                original_request=plan.original_request,
                plan_summary=plan.summary,
                reuse_issue_id=self.session.active_issue_id,
            )
            issue_id = str((issue or {}).get("issue_id", "") or "").strip()
            if issue_id:
                self.session.active_issue_id = issue_id
                output[0] = f"Executing confirmed plan under {issue_id}."

        try:
            while len(completed_ids) < len(plan.goals) and not failed:
                batch = self._next_goal_batch(plan, completed_ids, failed)
                if not batch:
                    output.append("Planner execution stopped: no dependency-ready goals were available.")
                    failed = True
                    break

                if len(batch) == 1:
                    index, goal = batch[0]
                    self.session.executing_goal_index = index
                    self.session.executing_goal_id = goal.goal_id
                    self.session.executing_goal_title = goal.title
                    self._fire_goal_callback("goal_start", index, goal.goal_id, goal.title)
                    # On the main worker, preserve facts from earlier goals in
                    # the same plan so context accumulates across the sequence.
                    effective_preserve = goal.preserve_context or bool(completed_ids)
                    result = self._execute_goal_with_worker(
                        self.worker, plan,
                        _dataclass_replace(goal, preserve_context=effective_preserve),
                        index,
                    )
                    self._fire_goal_callback("goal_finish", index, goal.goal_id, goal.title)
                    if result.status == "completed" and index < len(plan.goals):
                        result.commentary_for_next_goal = self._plan_next_goal_guidance(plan, goal, result)
                        self._apply_guidance_to_next_goal(plan, index, result.commentary_for_next_goal)
                    self.session.completed_results.append(result)
                    completed_ids.add(goal.goal_id)
                    output.append(self._render_goal_result(index, len(plan.goals), result))
                    if result.status != "completed":
                        failed = True
                    continue

                workers: Dict[str, PlannerWorker] = {
                    goal.goal_id: self.worker_factory()
                    for _, goal in batch
                }
                max_workers = min(len(batch), max(1, int(getattr(self.config, "max_parallel_workers", 4))))
                results_by_goal: Dict[str, GoalExecutionResult] = {}
                with ThreadPoolExecutor(max_workers=max_workers) as executor:
                    futures = {
                        executor.submit(self._execute_goal_with_worker, workers[goal.goal_id], plan, goal, index): (index, goal)
                        for index, goal in batch
                    }
                    for future in as_completed(futures):
                        index, goal = futures[future]
                        results_by_goal[goal.goal_id] = future.result()

                for index, goal in batch:
                    result = results_by_goal[goal.goal_id]
                    self.session.completed_results.append(result)
                    completed_ids.add(goal.goal_id)
                    output.append(self._render_goal_result(index, len(plan.goals), result))
                    if result.status != "completed":
                        failed = True
        finally:
            self.session.executing = False
            self.session.executing_goal_index = -1
            self.session.executing_goal_id = ""
            self.session.executing_goal_title = ""

        plan_completed = (
            len(self.session.completed_results) == len(plan.goals)
            and all(result.status == "completed" for result in self.session.completed_results)
        )
        final_summary = self._synthesize_final_summary(plan, self.session.completed_results)
        self.session.last_completed_results = list(self.session.completed_results)
        self.session.last_execution_summary = final_summary
        if plan_completed:
            if not self.session.defer_issue_close_for_review:
                closer = getattr(self.worker, "close_active_issue", None)
                if callable(closer):
                    closer()
                self.session.active_issue_id = ""
            self.session.last_completed_plan = plan
            self.session.pending_plan = None
            self.session.last_presented_plan = None
            self.session.awaiting_plan_revision = False
            self.session.plan_revision_feedback = []
        else:
            self.session.last_completed_plan = None
            self.session.pending_plan = plan
            self.session.last_presented_plan = plan
            self.session.awaiting_plan_revision = False
            self.session.plan_revision_feedback = []
        self._fire_plan_callback(
            "plan_execution_finish",
            {
                "summary": plan.summary,
                "goal_count": len(plan.goals),
                "status": "completed" if plan_completed else "failed",
                "execution_summary": final_summary,
            },
        )
        self.session.completed_results = []
        return "\n\n".join(output + [final_summary])

    def _intent_issue_summary(self) -> str:
        intent = self._load_project_intent()
        if not intent.present or not intent.content.strip():
            return ""
        fallback = ""
        for line in intent.content.splitlines():
            raw = line.strip()
            stripped = raw.strip(" #\t")
            if not stripped:
                continue
            if not fallback:
                fallback = stripped[:180]
            if raw.startswith("#"):
                continue
            return stripped[:180]
        if fallback:
            return fallback
        return "Work from project intent"

    def _continuous_request_for_issue(self, issue: Dict[str, Any]) -> str:
        source = str(issue.get("source", "") or "").strip()
        summary = str(issue.get("plan_summary") or issue.get("request_summary") or "").strip()
        if source != "intent":
            return summary
        intent_summary = self._intent_issue_summary()
        candidate = intent_summary or summary
        if not candidate:
            return ""
        return (
            "Auto run: choose and implement the next bounded project improvement "
            f"from immutable project direction. Current candidate: {candidate}. "
            "Use that source only as read-only guidance; do not edit it."
        )

    def _create_or_select_continuous_issue(self) -> Optional[Dict[str, Any]]:
        payload = self._load_repo_facts_payload() or {}
        if isinstance(payload, dict):
            active = payload.get("active_issue")
            if isinstance(active, dict) and active.get("issue_id"):
                return active
            issues = payload.get("issues")
            if isinstance(issues, list):
                open_issues = [
                    issue for issue in issues
                    if isinstance(issue, dict) and str(issue.get("status", "") or "") == "open"
                ]
                if open_issues:
                    open_issues.sort(key=lambda issue: int(issue.get("priority", 0) or 0), reverse=True)
                    return open_issues[0]

        summary = self._intent_issue_summary()
        if not summary:
            return None
        creator = getattr(self.worker, "create_issue", None)
        if not callable(creator):
            return {
                "issue_id": "",
                "request_summary": summary,
                "plan_summary": summary,
                "source": "intent",
                "source_excerpt": summary,
            }
        return creator(
            request_summary=summary,
            plan_summary=summary,
            source="intent",
            source_excerpt=summary,
            priority=50,
            activate=True,
        )

    def _select_discovery_mode_for_issue(self, issue: Dict[str, Any]) -> str:
        text = " ".join(
            str(issue.get(key, "") or "")
            for key in ["request_summary", "plan_summary", "source_excerpt", "blocked_reason", "last_review_decision"]
        ).lower()
        if any(token in text for token in ["architecture", "cross-module", "unknown", "unclear", "failed", "review"]):
            return "deep"
        if any(token in text for token in [".py", ".ts", ".tsx", "/", "file:", "path:"]):
            return "quick"
        return "moderate"

    def _auto_approve_plan(self, plan: PlannerPlan) -> PlanApprovalDecision:
        blockers: List[str] = []
        reasons: List[str] = []
        if not self.continuous_config.auto_approve:
            blockers.append("auto approval is disabled")
        if not 1 <= len(plan.goals) <= 5:
            blockers.append("plan must contain 1 to 5 goals")
        if not plan.not_in_scope:
            blockers.append("plan must define concrete not_in_scope boundaries")
        protected_text = " ".join(
            [plan.summary]
            + [goal.title + " " + goal.goal + " " + " ".join(goal.delegation_notes) for goal in plan.goals]
        )
        protected_edit_pattern = re.compile(r"\b(?:edit|modify|update|rewrite|change|write|patch|delete|rename)\b.{0,80}\bINTENT\.md\b|\bINTENT\.md\b.{0,80}\b(?:edit|modify|update|rewrite|change|write|patch|delete|rename)\b", re.IGNORECASE)
        if protected_edit_pattern.search(protected_text):
            blockers.append("plan references INTENT.md as an edit target or scope item")
        for goal in plan.goals:
            if goal.estimated_scope in {"write", "mixed"} and not goal.success_signals:
                blockers.append(f"{goal.goal_id} is missing success_signals")
        if not blockers:
            reasons.append("plan passed continuous-mode approval gates")
        return PlanApprovalDecision(approved=not blockers, reasons=reasons, blocking_reasons=blockers)

    def _review_completed_plan(self, plan: PlannerPlan) -> ReviewModeResult:
        results = list(self.session.last_completed_results or [])
        if not results:
            return ReviewModeResult(decision="blocked", required_actions=["No completed goal results were available for review."])
        failed = [result for result in results if result.status != "completed" or not result.task_satisfied]
        if failed:
            return ReviewModeResult(
                decision="rejected",
                evidence=[f"{item.goal_id}: {item.final_message}" for item in failed],
                required_actions=["Retry or revise the failed goal before closing the issue."],
            )
        invalid_validation = [
            result for result in results
            if result.validation_ran and not result.validation_passed
        ]
        if invalid_validation:
            return ReviewModeResult(
                decision="rejected",
                evidence=[f"{item.goal_id}: {item.validation_summary}" for item in invalid_validation],
                required_actions=["Resolve validation failures before closing the issue."],
            )
        return ReviewModeResult(
            decision="accepted",
            evidence=[f"{item.goal_id}: {item.final_message}" for item in results],
        )

    def _extract_next_steps_from_summary(self, summary: str) -> List[str]:
        lines = str(summary or "").splitlines()
        in_next_steps = False
        steps: List[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.lower().startswith("next steps"):
                in_next_steps = True
                continue
            if in_next_steps and re.match(r"^[A-Z][A-Za-z ]+$", stripped) and not re.match(r"^[-*\d]", stripped):
                break
            if in_next_steps:
                cleaned = re.sub(r"^(?:[-*]|\d+[.)])\s*", "", stripped).strip()
                if cleaned and cleaned.lower() != "none." and cleaned.lower() != "none":
                    steps.append(cleaned)
        return self._filter_completed_next_steps(
            self.session.last_completed_plan or PlannerPlan(original_request="", summary="", goals=[]),
            self.session.last_completed_results,
            steps,
        )

    def _create_followup_issues_from_next_steps(self, parent_issue_id: str) -> List[str]:
        created: List[str] = []
        creator = getattr(self.worker, "create_issue", None)
        if not callable(creator):
            return created
        for step in self._extract_next_steps_from_summary(self.session.last_execution_summary):
            issue = creator(
                request_summary=step,
                plan_summary=step,
                source="next_step",
                parent_issue_id=parent_issue_id,
                source_excerpt=step,
                priority=40,
                activate=False,
            )
            issue_id = str((issue or {}).get("issue_id", "") or "")
            if issue_id and issue_id not in created:
                created.append(issue_id)
        return created

    def start_continuous(self, *, max_cycles: int = 1) -> str:
        self.continuous_config = ContinuousModeConfig(enabled=True, max_cycles=max(1, int(max_cycles or 1)))
        self.continuous_state = ContinuousRunState(enabled=True, status="selecting_issue", max_cycles=self.continuous_config.max_cycles)
        output: List[str] = ["Continuous mode started."]
        failures = 0
        try:
            for cycle in range(1, self.continuous_config.max_cycles + 1):
                self.continuous_state.cycle = cycle
                self.continuous_state.status = "selecting_issue"
                issue = self._create_or_select_continuous_issue()
                if not issue:
                    self.continuous_state.stop_reason = "no_issue_candidate"
                    break
                issue_id = str(issue.get("issue_id", "") or "")
                self.session.active_issue_id = issue_id
                self.continuous_state.active_issue_id = issue_id
                request = self._continuous_request_for_issue(issue)
                if not request:
                    self.continuous_state.stop_reason = "selected_issue_missing_summary"
                    break

                self.session.latest_request = request
                self.session.intake_messages = [{"role": "user", "content": request}]
                self.session.pending_plan = None
                self.session.pending_discovery = None
                self.session.last_completed_plan = None
                self.session.last_completed_results = []
                self.session.last_execution_summary = ""
                self.session.defer_issue_close_for_review = True

                self.continuous_state.status = "planning"
                plan_response = self._handle_intake_turn()
                if self.session.pending_discovery is not None:
                    self.continuous_state.status = "discovering"
                    mode = self._select_discovery_mode_for_issue(issue)
                    self.continuous_state.selected_discovery_mode = mode
                    output.append(f"Cycle {cycle}: selected {mode} discovery for {issue_id or 'intent issue'}.")
                    plan_response = self.execute_discovery(mode)
                if self.session.pending_plan is None:
                    failures += 1
                    output.append(f"Cycle {cycle}: stopped before execution. {plan_response}")
                    if failures >= self.continuous_config.max_consecutive_failures:
                        self.continuous_state.stop_reason = "planning_failed"
                        break
                    continue

                decision = self._auto_approve_plan(self.session.pending_plan)
                if not decision.approved:
                    self.continuous_state.status = "stopped"
                    self.continuous_state.stop_reason = "auto_approval_blocked: " + "; ".join(decision.blocking_reasons)
                    output.append(f"Cycle {cycle}: auto approval blocked. " + "; ".join(decision.blocking_reasons))
                    break

                self.continuous_state.status = "executing"
                output.append(f"Cycle {cycle}: auto-approved plan for {issue_id or 'intent issue'}.")
                execution_message = self.execute_pending_plan()
                output.append(execution_message)

                self.continuous_state.status = "reviewing"
                plan = self.session.last_completed_plan or self.session.last_presented_plan or self.session.pending_plan
                if plan is None:
                    self.continuous_state.stop_reason = "review_missing_plan"
                    break
                review = self._review_completed_plan(plan)
                self.continuous_state.latest_review_decision = review.decision
                if review.decision != "accepted":
                    failures += 1
                    self.continuous_state.stop_reason = f"review_{review.decision}"
                    output.append(f"Cycle {cycle}: review {review.decision}. " + "; ".join(review.required_actions))
                    if failures >= self.continuous_config.max_consecutive_failures:
                        break
                    continue

                self.continuous_state.status = "closing_issue"
                closer = getattr(self.worker, "close_active_issue", None)
                if callable(closer):
                    closer(note="Closed after continuous-mode review accepted completed work.")
                self.session.active_issue_id = ""
                followups = self._create_followup_issues_from_next_steps(issue_id)
                self.continuous_state.created_followup_issue_ids.extend(followups)
                if followups:
                    output.append(f"Cycle {cycle}: created follow-up issues: {', '.join(followups)}.")
        finally:
            self.session.defer_issue_close_for_review = False
            self.continuous_state.status = "stopped"
            self.continuous_config.enabled = False
            self.continuous_state.enabled = False
        if not self.continuous_state.stop_reason:
            self.continuous_state.stop_reason = "max_cycles_reached"
        output.append(f"Continuous mode stopped: {self.continuous_state.stop_reason}.")
        return "\n\n".join(output)

    def stop_continuous(self, reason: str = "stopped_by_user") -> str:
        self.continuous_config.enabled = False
        self.continuous_state.enabled = False
        self.continuous_state.status = "stopped"
        self.continuous_state.stop_reason = str(reason or "stopped_by_user")
        return f"Continuous mode stopped: {self.continuous_state.stop_reason}."

    def _handle_intake_turn(self) -> str:
        prompt = self._build_planner_prompt()
        system = self._planner_system_prompt()
        parsed = None
        last_error = ""
        last_raw = ""
        use_tags = self._use_tagged_planner_control()
        for attempt in range(2):
            current_prompt = prompt if attempt == 0 else (
                prompt + (
                    "\n\nIMPORTANT: Your previous response could not be parsed. Respond with tags only, matching the required planner tag format exactly. No JSON, no markdown, no prose outside tags."
                    if use_tags
                    else "\n\nIMPORTANT: Your previous response could not be parsed as JSON. Respond with a single valid JSON object using double-quoted keys and string values only. No markdown, no prose, no single quotes."
                )
            )
            raw = self._planner_complete(system, current_prompt)
            last_raw = raw
            try:
                parsed = self._load_planner_intake_payload(raw)
                action = parsed.get("action")
                if not isinstance(action, dict):
                    raise ValueError("Planner JSON has no 'action' dict.")
                break
            except Exception as exc:
                last_error = str(exc)
                parsed = None
                raw_preview = (raw or "")[:300].strip()
                print(f"[planner] intake attempt {attempt + 1} failed: {last_error} | raw: {raw_preview!r}", file=sys.stderr)
                if attempt == 0:
                    continue

        if parsed is None or not isinstance(parsed.get("action"), dict):
            snippet = (last_raw or "")[:200].strip()
            return (
                "The planner produced an invalid control response after 2 attempts. "
                f"Error: {last_error}. "
                f"Response preview: {snippet!r}"
            )

        action = parsed["action"]

        action_type = str(action.get("type", "")).strip()
        if action_type == "ask_clarification":
            question = str(action.get("question", "Please clarify the request.")).strip()
            return question

        if action_type == "offer_discovery":
            request = DiscoveryRequest(
                reason=str(action.get("reason") or "The request needs repository discovery before planning.").strip(),
                prompt=str(action.get("prompt") or "Choose a discovery depth.").strip(),
                recommended_mode=self._normalize_discovery_mode(str(action.get("recommended_mode") or "moderate")),
            )
            self.session.pending_discovery = request
            return self._render_discovery_offer(request)

        if action_type == "present_plan":
            plan = self._plan_from_action(action)
            self._apply_discovery_findings_to_plan(plan)
            self.session.pending_plan = plan
            self.session.last_presented_plan = plan
            self.session.awaiting_plan_revision = False
            self.session.plan_revision_feedback = []
            self.session.pending_discovery = None
            self._fire_plan_callback(
                "plan_presented",
                {
                    "summary": plan.summary,
                    "goal_count": len(plan.goals),
                    "confirmation_prompt": plan.confirmation_prompt,
                },
            )
            return self._render_plan(plan)

        if action_type == "respond":
            return str(action.get("message", "No plan generated."))

        return (
            "The planner produced an unsupported control response. "
            "Retry the request, or switch to discovery/worker mode."
        )

    def _use_tagged_planner_control(self) -> bool:
        return use_tagged_planner_control(str(getattr(self.config, "provider", "") or ""))

    def _load_planner_intake_payload(self, raw: str) -> Dict[str, Any]:
        if self._use_tagged_planner_control():
            try:
                return parse_planner_intake_response(raw)
            except Exception:
                return self.json_loader(raw)
        return self.json_loader(raw)

    def _load_next_goal_guidance_payload(self, raw: str) -> Dict[str, Any]:
        if self._use_tagged_planner_control():
            try:
                return parse_next_goal_guidance_response(raw)
            except Exception:
                return self.json_loader(raw)
        return self.json_loader(raw)

    def _load_final_summary_payload(self, raw: str) -> Dict[str, Any]:
        if self._use_tagged_planner_control():
            try:
                payload = parse_final_summary_response(raw)
                if payload.get("summary") or payload.get("next_steps"):
                    return payload
            except Exception:
                pass
            return self.json_loader(raw)
        return self.json_loader(raw)

    def _planner_system_prompt(self) -> str:
        format_instructions = planner_intake_format_instructions(use_tags=self._use_tagged_planner_control())
        return textwrap.dedent(
            f"""
            You are a planner agent orchestrating a coding worker agent, who must be given specific scope and success signals.

            Your job:
            1. Clarify vague requests before execution.
            2. Produce a concrete multi-goal plan that the user must approve.
            3. Keep goals coarse enough for a strong worker agent to execute, but specific enough to sequence safely.
            4. Decide per goal whether worker context should be preserved.
            5. Produce final steps that are specific to the direction of the app, not generic cleanup advice.

                        {format_instructions}

            Rules:
            - Ask clarification when the request is materially underspecified.
            - Once the user answers a clarification, absorb that answer into the plan. Do not ask a second approval-style question that merely restates your preferred implementation.
            - Prefer the narrowest implementation that directly satisfies the user's wording.
            - Do not make product or UX design leaps when a more literal implementation path exists.
            - Keep assumptions conservative and clearly marked; do not turn speculation into scope.
            - Do not claim performance improvements or accessibility compliance unless the plan includes a concrete way to validate them.
            - If the missing context depends on repo inspection, architecture discovery, or implementation ambiguity, prefer offer_discovery.
            - If a discovery offer was pending and the user replies with added detail instead of selecting 1/2/3 or skipping, treat that detail as new planning context and reassess whether discovery is still needed.
            - If last_discovery is present, treat it as evidence you must use, not background noise.
            - If repo_facts is present, treat it as durable repo memory from prior work and use it to avoid redundant rediscovery.
            - When repo_facts materially help a goal, include a short relevant_fact_keys list naming the most relevant fact keys for that goal.
            - relevant_fact_keys should prioritize execution; they are not an exclusive allowlist.
            - After discovery, convert discovered files, flows, constraints, and risks into stronger goals and delegation notes.
            - After discovery, do not propose goals whose main purpose is to inspect, investigate, explore, understand, or discover unless the discovery result explicitly says a critical ambiguity remains unresolved.
            - After discovery, each goal should name a concrete outcome and should be delegable without requiring another broad discovery pass.
            - After discovery, reasons and delegation_notes should reference the discovered architecture, files, constraints, or risks.
            - If discovery identified likely files or entrypoints, use them in delegation_notes.
            - If pending_plan or revision_context is present and the user asks for changes, revise that plan instead of restating it unchanged.
            - If revision_context.feedback contains concrete change requests, either incorporate them into the new plan or ask a clarification question about the conflict.
            - Do not ignore revision feedback and do not simply re-emit the prior plan unless you explicitly explain that the requested change is already reflected.
            - If follow_up_context is present, the current request is a NEW user request that comes after a completed plan; use the prior plan and results as context, but plan for the new request.
            - Do not treat follow_up_context.prior_plan as the active request. The active request is always the top-level request field.
            - If project_intent.present is true, treat project_intent.content as immutable project direction. Use it for scope and issue creation, but never propose edits to INTENT.md.
            - Do not start execution yourself.
            - Prefer 1 to 5 goals.
            - Each goal should be substantial, not a tiny task list.
            - Mark a goal `parallelizable=true` only if it is safely independent, read-only or validation-only, and does not need preserved worker context.
            - Use `estimated_scope=write` for goals expected to mutate files or git state.
            - preserve_context=true only when the next goal genuinely benefits from worker continuity.
            - delegation_notes should contain useful commentary for the worker, not generic filler.
            - Strong delegation_notes usually include concrete targets, constraints, and validation direction.
            - not_in_scope is required and must be non-empty. List every area of the codebase, behavior, or UX that this plan deliberately does not touch.
            - not_in_scope entries should be concrete (e.g. 'authentication flow', 'database schema', 'unrelated components') not vague ('other code').
            - If the request is narrowly scoped, use not_in_scope to make the boundary explicit: name the files, modules, or behaviors the worker must leave untouched.
            - offer_discovery must use one of: quick, moderate, deep.

            File and symbol grounding rules (strictly enforced):
            - NEVER invent, guess, or assume file paths, directory names, function names, class names, or symbol names.
            - Only name a file, path, or symbol in a goal or delegation_notes if it appears verbatim in: last_discovery, repo_facts, completed_results, or the user's own message.
            - If you are unsure of the exact path or symbol name, use offer_discovery instead of guessing.
            - Do not extrapolate naming conventions (e.g. do not assume a file named "fooService.ts" exists because "foo" was mentioned).
            - Do not generate plausible-sounding but unverified identifiers. A wrong file name in delegation_notes will cause the worker to create or corrupt the wrong file.
            - When referencing a discovered file, copy its path exactly as it appears in last_discovery or repo_facts — do not paraphrase or abbreviate it.
            - If multiple discovered files are candidates, list all of them in delegation_notes and let the worker confirm which applies.
            """
        ).strip()

    def _build_planner_prompt(self) -> str:
        discovery_findings = _discovery_findings_lines(self.session.last_discovery)
        payload = {
            "root": str(self.root),
            "request": self.session.latest_request,
            "project_intent": self._load_project_intent_payload(),
            "repo_facts": self._load_repo_facts_payload(),
            "conversation": self.session.intake_messages[-10:],
            "follow_up_context": self._follow_up_context_payload(),
            "revision_context": self._revision_context_payload(),
            "pending_discovery": self._discovery_request_payload(self.session.pending_discovery),
            "last_discovery": self._discovery_result_payload(self.session.last_discovery),
            "last_discovery_findings": discovery_findings,
            "completed_results": [self._result_payload(result) for result in self.session.completed_results[-5:]],
            "pending_plan": self._plan_payload(self.session.pending_plan) if self.session.pending_plan else None,
        }
        return serialize_prompt(payload)

    def _normalize_confirmation_prompt(self, text: str) -> str:
        normalized = str(text or "").strip()
        if not normalized:
            return "Approve this plan to start execution."
        lowered = normalized.lower()
        if "?" in normalized or lowered.startswith(("does this", "would you like", "does the", "is this", "should we")):
            return "Approve this plan to start execution, or describe what should change."
        return normalized

    def _plan_from_action(self, action: Dict[str, Any]) -> PlannerPlan:
        goals_raw = action.get("goals") or []
        goals: List[PlannerGoal] = []
        for index, item in enumerate(goals_raw, start=1):
            if not isinstance(item, dict):
                continue
            goals.append(
                PlannerGoal(
                    goal_id=str(item.get("goal_id") or f"goal-{index}"),
                    title=str(item.get("title") or f"Goal {index}"),
                    goal=str(item.get("goal") or "").strip(),
                    reason=str(item.get("reason") or "").strip(),
                    depends_on=[str(value) for value in item.get("depends_on") or [] if str(value).strip()],
                    preserve_context=bool(item.get("preserve_context", False)),
                    parallelizable=bool(item.get("parallelizable", False)),
                    estimated_scope=str(item.get("estimated_scope") or "mixed").strip().lower() or "mixed",
                    delegation_notes=[str(value) for value in item.get("delegation_notes") or [] if str(value).strip()],
                    success_signals=[str(value) for value in item.get("success_signals") or [] if str(value).strip()],
                    relevant_fact_keys=self._normalize_relevant_fact_keys(item.get("relevant_fact_keys") or []),
                )
            )

        if not goals:
            raise ValueError("Planner returned no goals.")

        return PlannerPlan(
            original_request=self.session.latest_request,
            summary=str(action.get("summary") or "").strip(),
            assumptions=[str(value) for value in action.get("assumptions") or [] if str(value).strip()],
            clarification_summary=str(action.get("clarification_summary") or "").strip(),
            goals=goals,
            not_in_scope=[str(value) for value in action.get("not_in_scope") or [] if str(value).strip()],
            next_steps_preview=[str(value) for value in action.get("next_steps_preview") or [] if str(value).strip()],
            confirmation_prompt=self._normalize_confirmation_prompt(str(action.get("confirmation_prompt") or "Approve this plan to start execution.")),
        )

    def _render_plan(self, plan: PlannerPlan) -> str:
        lines = [
            "Plan Summary",
            f"- {plan.summary}",
        ]
        if plan.clarification_summary:
            lines.extend([
                "",
                "Clarified Scope",
                f"- {plan.clarification_summary}",
            ])
        if self.session.last_discovery is not None:
            lines.extend(["", "Discovery Basis"])
            for item in _discovery_findings_lines(self.session.last_discovery)[:4]:
                lines.append(f"- {item}")
            if self.session.last_discovery.touched_paths:
                lines.append("- Touched: " + ", ".join(self.session.last_discovery.touched_paths[:8]))
        if plan.assumptions:
            lines.extend(["", "Assumptions"])
            lines.extend(f"- {item}" for item in plan.assumptions)
        if plan.not_in_scope:
            lines.extend(["", "Not In Scope"])
            lines.extend(f"- {item}" for item in plan.not_in_scope)
        lines.extend(["", "Goals"])
        for index, goal in enumerate(plan.goals, start=1):
            lines.extend(
                [
                    "",
                    f"{index}. {goal.title}",
                    f"   id: {goal.goal_id}",
                    f"   preserve_context: {str(goal.preserve_context).lower()}",
                    f"   parallelizable: {str(goal.parallelizable).lower()}",
                    f"   estimated_scope: {goal.estimated_scope}",
                    f"   outcome: {goal.goal}",
                    f"   why_next: {goal.reason}",
                ]
            )
            if goal.depends_on:
                lines.append(f"   depends_on: {', '.join(goal.depends_on)}")
            if goal.delegation_notes:
                lines.append("   delegation_notes:")
                lines.extend(f"   - {note}" for note in goal.delegation_notes)
            if goal.success_signals:
                lines.append("   success_signals:")
                lines.extend(f"   - {signal}" for signal in goal.success_signals)
            if goal.relevant_fact_keys:
                lines.append(f"   relevant_fact_keys: {', '.join(goal.relevant_fact_keys)}")
        if plan.next_steps_preview:
            lines.extend(["", "Expected Follow-Through"])
            lines.extend(f"- {item}" for item in plan.next_steps_preview)
        lines.extend([
            "",
            "Approval",
            f"- {plan.confirmation_prompt}",
            "- Reply with 'approve' to execute, or describe what should change.",
        ])
        return "\n".join(lines)

    def _render_discovery_offer(self, request: DiscoveryRequest) -> str:
        lines = [
            "Discovery Suggested",
            f"- Reason: {request.reason}",
            f"- Prompt: {request.prompt}",
            "",
            "Choose a Discovery Depth",
        ]
        ordered_modes = [DISCOVERY_MODES["quick"], DISCOVERY_MODES["moderate"], DISCOVERY_MODES["deep"]]
        for index, mode in enumerate(ordered_modes, start=1):
            recommended = " (recommended)" if mode.key == request.recommended_mode else ""
            lines.append(
                f"{index}. {mode.label}{recommended}"
            )
            lines.append(f"   scope: {mode.description}")
            lines.append(f"   budget: {mode.max_tool_calls} tool calls")
        lines.extend([
            "",
            "Response Options",
            "- Reply with 1, 2, or 3 to run discovery.",
            "- Say 'no' to skip it.",
            "- Send more detail and the planner will reassess.",
        ])
        return "\n".join(lines)

    def _render_discovery_result(self, result: DiscoveryResult) -> str:
        mode = DISCOVERY_MODES[result.mode]
        lines = [
            "Discovery Complete" if result.ok else "Discovery Failed",
            f"- Mode: {mode.label}",
            f"- Worker result: {result.final_message}",
            f"- Status: {'completed' if result.ok else 'failed'}",
            f"- Tool budget: {result.tool_calls_used}/{result.tool_calls_max}",
            f"- Duration: {result.duration_s:.3f}s",
        ]
        if result.validation_summary:
            lines.append(f"- Validation: {result.validation_summary}")
        if result.usage_summary:
            lines.extend(["", "Usage", f"- {result.usage_summary}"])
        if result.touched_paths:
            lines.extend(["", "Touched Paths"])
            lines.extend(f"- {path}" for path in result.touched_paths[:8])
        return "\n".join(lines)

    def _build_discovery_steering(self, request: DiscoveryRequest, mode: DiscoveryMode) -> str:
        return "\n".join(
            [
                "Planner-controlled discovery phase.",
                f"Discovery mode: {mode.label}",
                f"Reason: {request.reason}",
                f"Expectation: {mode.scan_expectation}",
                f"Discovery budget: at most {mode.max_tool_calls} tool-backed actions, then finish manually.",
                "Before normal repo exploration, inspect available skills once and load any directly relevant skill or skill mode.",
                "Prefer starting with `skill` to inspect the catalog, then use `skill <name>` or `skill <name> mode=<mode>` when the contract matches the discovery job.",
                "Do not modify files. Focus on discovery, constraints, likely edit locations, and planning risks.",
            ]
        )

    def _build_discovery_task(self, request: DiscoveryRequest, mode: DiscoveryMode) -> str:
        sections = [
            f"Original request: {self.session.latest_request}",
            f"Discovery mode: {mode.label}",
            f"Why discovery is needed: {request.reason}",
            f"Discovery expectation: {mode.scan_expectation}",
            f"Discovery budget: You may use at most {mode.max_tool_calls} tool-backed actions in this discovery run.",
            "Perform a discovery phase only. Do not modify files.",
            "First, check whether a bundled or workspace skill contract is relevant to this discovery pass.",
            "Use `skill` to inspect the catalog, then load a matching skill with `skill <name>` or `skill <name> mode=<mode>` before broad repo exploration when it would sharpen the search.",
            "Use read/search/meta/git inspection to answer these questions:",
            "- what are the most relevant files and entrypoints",
            "- what constraints or ambiguities materially affect planning",
            "- what implementation shape is most likely",
            "- what risks or dependencies the planner should account for",
            "Keep a running budget in mind and finish manually with a concise discovery summary for the planner before or when you hit the limit.",
        ]
        if self.session.last_discovery is not None:
            sections.append("Previous discovery summary:\n- " + self.session.last_discovery.final_message)
        return "\n\n".join(sections)

    def _build_goal_steering(self, plan: PlannerPlan, goal: PlannerGoal, index: int) -> str:
        completed = self.session.completed_results
        completed_summary = [
            f"{item.goal_id}: {item.final_message}"
            for item in completed[-3:]
        ]
        not_in_scope_note = (
            "Not in scope for this plan: " + "; ".join(plan.not_in_scope)
            if plan.not_in_scope
            else ""
        )
        lines = [
            f"Planner-controlled execution for goal {index}/{len(plan.goals)}.",
            f"Current goal: {goal.goal}",
            f"Why this goal is next: {goal.reason}",
            f"Preserve worker context: {str(goal.preserve_context).lower()}",
            "Scope guardrail: prefer the narrowest implementation that directly satisfies this goal; do not broaden product behavior without a concrete dependency.",
        ]
        if not_in_scope_note:
            lines.append(not_in_scope_note)
        if goal.relevant_fact_keys:
            lines.append("Prioritize repo fact keys: " + " | ".join(goal.relevant_fact_keys))
        if goal.delegation_notes:
            lines.append("Delegation notes: " + " | ".join(goal.delegation_notes))
        if self.session.last_discovery is not None:
            lines.append("Discovery summary: " + self.session.last_discovery.final_message)
            if self.session.last_discovery.touched_paths:
                lines.append("Discovery paths: " + " | ".join(self.session.last_discovery.touched_paths[:8]))
        if completed_summary:
            lines.append("Prior goal outcomes: " + " | ".join(completed_summary))
        return "\n".join(lines)

    def _build_goal_task(self, plan: PlannerPlan, goal: PlannerGoal, index: int) -> str:
        result_lines: List[str] = []
        for item in self.session.completed_results[-3:]:
            result_lines.append(f"- {item.goal_id}: {item.final_message}")
            if item.commentary_for_next_goal:
                result_lines.append(f"  planner commentary: {item.commentary_for_next_goal}")

        not_in_scope_items = plan.not_in_scope
        sections = [
            f"Original request: {plan.original_request}",
            f"Planner goal {index}/{len(plan.goals)}: {goal.title}",
            f"Goal: {goal.goal}",
            f"Why this goal is next: {goal.reason}",
            "Context policy: " + ("Reuse previous worker context when it materially helps this goal." if goal.preserve_context else "Ignore prior worker context unless the repository state itself requires it."),
            "Execution guardrail: implement the narrowest change that satisfies this goal, and avoid speculative compliance/performance claims unless you actually validate them.",
        ]
        if not_in_scope_items:
            sections.append("Do not touch (not in scope for this plan):\n- " + "\n- ".join(not_in_scope_items))
        if goal.relevant_fact_keys:
            sections.append("Relevant repo fact keys to prioritize:\n- " + "\n- ".join(goal.relevant_fact_keys))
        if goal.delegation_notes:
            sections.append("Delegation notes:\n- " + "\n- ".join(goal.delegation_notes))
        if goal.success_signals:
            sections.append("Success signals:\n- " + "\n- ".join(goal.success_signals))
        sections.append("Validation expectation:\n- Prefer one real validation step before finish: diff review, targeted build/typecheck/test, or another concrete integrity check if available.")
        if self.session.last_discovery is not None:
            discovery_findings = _discovery_findings_lines(self.session.last_discovery)
            sections.append("Discovery findings:\n- " + "\n- ".join(discovery_findings or [self.session.last_discovery.final_message]))
            if self.session.last_discovery.touched_paths:
                sections.append("Discovery-touched files:\n- " + "\n- ".join(self.session.last_discovery.touched_paths[:8]))
            if len(discovery_findings) > 1:
                sections.append("Discovery evidence:\n- " + "\n- ".join(discovery_findings[1:7]))
        if result_lines:
            sections.append("Prior goal results:\n" + "\n".join(result_lines))
        sections.append(
            "Use the discovery findings as the starting point for execution. Do not spend this goal on another broad discovery pass unless a named blocker remains unresolved."
        )
        sections.append("Complete this goal end-to-end, then finish with a concise summary of what changed and what remains, if anything.")
        return "\n\n".join(sections)

    def _goal_needs_stronger_delegation(self, goal: PlannerGoal) -> bool:
        vague_terms = {
            "investigate",
            "investigation",
            "explore",
            "exploration",
            "inspect",
            "inspection",
            "understand",
            "analyze",
            "analysis",
            "review",
            "look into",
            "discover",
            "discovery",
        }
        text = " ".join([goal.title, goal.goal, goal.reason]).lower()
        return any(term in text for term in vague_terms)

    def _apply_discovery_findings_to_plan(self, plan: PlannerPlan) -> None:
        discovery = self.session.last_discovery
        if discovery is None:
            return

        discovery_findings = _discovery_findings_lines(discovery)
        discovery_summary = discovery.final_message.strip()
        evidence_summary = " ".join(discovery_findings).strip() or discovery_summary
        touched_paths = discovery.touched_paths[:8]
        if evidence_summary:
            if plan.clarification_summary:
                if evidence_summary not in plan.clarification_summary:
                    plan.clarification_summary = f"{plan.clarification_summary} Discovery established: {evidence_summary}".strip()
            else:
                plan.clarification_summary = f"Discovery established: {evidence_summary}"

        for goal in plan.goals:
            if touched_paths and not any("/" in note or "." in note for note in goal.delegation_notes):
                goal.delegation_notes.append("Primary discovered files: " + ", ".join(touched_paths))
            if discovery_findings:
                joined_findings = "; ".join(discovery_findings[:3])
                if not any(joined_findings in note for note in goal.delegation_notes):
                    goal.delegation_notes.append("Discovery findings to carry forward: " + joined_findings)
            if discovery_summary and not any("Use the discovery findings directly" in note for note in goal.delegation_notes):
                goal.delegation_notes.append("Use the discovery findings directly rather than repeating broad discovery.")
            if self._goal_needs_stronger_delegation(goal):
                goal.delegation_notes.append(
                    "Translate the discovered architecture into a concrete implementation outcome, not another exploration pass."
                )
                if evidence_summary and "Discovery already identified the relevant flow and constraints." not in goal.reason:
                    goal.reason = (goal.reason + " Discovery already identified the relevant flow and constraints.").strip()
            if not goal.success_signals:
                goal.success_signals.append("The worker reports a concrete completed outcome tied to the discovered flow, not additional broad discovery.")

    def _summarize_worker_history(self, steps: List[Any]) -> List[Dict[str, Any]]:
        summary: List[Dict[str, Any]] = []
        for step in steps[-10:]:
            try:
                action = getattr(step, "action", {})
                result = getattr(step, "result", None)
                payload = getattr(result, "payload", {}) if result is not None else {}
                summary.append(
                    {
                        "step": getattr(step, "step", None),
                        "action_type": action.get("type") if isinstance(action, dict) else None,
                        "ok": getattr(result, "ok", None),
                        "summary": payload.get("summary") if isinstance(payload, dict) else None,
                        "path": self._extract_step_path(step),
                    }
                )
            except Exception:
                continue
        return summary

    def _collect_touched_paths(self, steps: List[Any]) -> List[str]:
        seen: Dict[str, None] = {}
        for step in steps:
            path = self._extract_step_path(step)
            if path:
                seen[path] = None
        return list(seen.keys())

    def _extract_step_path(self, step: Any) -> str:
        try:
            action = getattr(step, "action", {})
            if isinstance(action, dict):
                path = action.get("path")
                if isinstance(path, str) and path:
                    return path
            result = getattr(step, "result", None)
            payload = getattr(result, "payload", {}) if result is not None else {}
            if isinstance(payload, dict):
                direct_path = payload.get("path")
                if isinstance(direct_path, str) and direct_path:
                    return direct_path
                data = payload.get("data")
                if isinstance(data, dict):
                    nested_path = data.get("path")
                    if isinstance(nested_path, str) and nested_path:
                        return nested_path
        except Exception:
            return ""
        return ""

    def _extract_last_finish_thought(self, steps: List[Any]) -> str:
        """Return the thought from the last finish step, falling back to the
        last step with a substantial thought.  Used to recover the actual
        analysis when the finish action message is terse (e.g. 'Done.')."""
        # Walk backwards, prefer the finish step's thought.
        for step in reversed(steps):
            try:
                action = getattr(step, "action", {})
                if isinstance(action, dict) and action.get("type") == "finish":
                    thought = str(getattr(step, "thought", "") or "").strip()
                    if thought:
                        return thought
            except Exception:
                continue
        # Fallback: last step with a long thought.
        for step in reversed(steps):
            try:
                thought = str(getattr(step, "thought", "") or "").strip()
                if len(thought) > 60:
                    return thought
            except Exception:
                continue
        return ""

    def _render_goal_result(self, index: int, total: int, result: GoalExecutionResult) -> str:
        status_label = "Completed" if result.status == "completed" else "Failed"
        lines = [
            f"Goal {index}/{total} {status_label}",
            f"- Title: {result.title}",
            f"- Worker result: {result.final_message}",
            f"- Status: {result.status}",
            f"- Task satisfied: {str(result.task_satisfied).lower()}",
            f"- Context preserved: {str(result.preserve_context_used).lower()}",
            f"- Duration: {result.duration_s:.3f}s",
        ]
        if result.validation_summary:
            lines.append(f"- Validation: {result.validation_summary}")
        if result.usage_summary:
            lines.extend(["", "Usage", f"- {result.usage_summary}"])
        if result.touched_paths:
            lines.extend(["", "Touched Paths"])
            lines.extend(f"- {path}" for path in result.touched_paths[:8])
        if result.commentary_for_next_goal:
            lines.extend(["", "Planner Commentary", f"- {result.commentary_for_next_goal}"])
        return "\n".join(lines)

    def _plan_next_goal_guidance(
        self,
        plan: PlannerPlan,
        completed_goal: PlannerGoal,
        result: GoalExecutionResult,
    ) -> str:
        remaining_goals = [goal for goal in plan.goals if goal.goal_id != completed_goal.goal_id]
        if not remaining_goals:
            return ""

        prompt = json.dumps(
            {
                "original_request": plan.original_request,
                "completed_goal": {
                    "goal_id": completed_goal.goal_id,
                    "title": completed_goal.title,
                    "goal": completed_goal.goal,
                    "result": self._result_payload(result),
                },
                "next_goal": self._goal_payload(remaining_goals[0]),
            },
            indent=2,
        )
        raw = self._planner_complete(
            next_goal_guidance_instructions(use_tags=self._use_tagged_planner_control()),
            prompt,
        )
        payload = self._load_next_goal_guidance_payload(raw)
        commentary = str(payload.get("commentary") or "").strip()
        preserve_context = payload.get("preserve_context")
        extra_notes = payload.get("extra_notes") or []
        note_text = "; ".join(str(item) for item in extra_notes if str(item).strip())
        if remaining_goals and isinstance(preserve_context, bool):
            remaining_goals[0].preserve_context = preserve_context
        if note_text:
            commentary = (commentary + " | " + note_text).strip(" |")
        return commentary

    def _apply_guidance_to_next_goal(self, plan: PlannerPlan, completed_index: int, commentary: str) -> None:
        if not commentary:
            return
        if completed_index >= len(plan.goals):
            return
        plan.goals[completed_index].delegation_notes.append(commentary)

    def _synthesize_final_summary(
        self,
        plan: PlannerPlan,
        results: List[GoalExecutionResult],
    ) -> str:
        if len(results) < len(plan.goals):
            remaining = [goal.title for goal in plan.goals if goal.goal_id not in {result.goal_id for result in results}]
            lines = [
                "Execution Summary",
                "- Plan execution stopped before all goals became dependency-ready.",
                "- The current plan remains pending so it can be revised or retried after dependency fixes.",
                "",
                "Next Steps",
                "1. Inspect goal dependencies and revise any goals that form an unresolved chain.",
                f"2. Remaining goals: {', '.join(remaining[:5]) or '(none)'}.",
            ]
            return "\n".join(lines)

        failed_results = [result for result in results if result.status != "completed"]
        if failed_results:
            lines = [
                "Execution Summary",
                f"- Plan execution stopped after a failed goal: {failed_results[0].title}.",
                "- The current plan remains pending so it can be retried or revised.",
                "",
                "Next Steps",
                f"1. Inspect the failed goal output and validation summary for {failed_results[0].title}.",
                "2. Revise the plan or retry it once the blocking issue is resolved.",
            ]
            return "\n".join(lines)

        completed_titles = [f"- {r.title}: {r.final_message}" for r in results if r.final_message]
        completed_block = "\n".join(completed_titles) if completed_titles else "(none)"
        prompt = json.dumps(
            {
                "original_request": plan.original_request,
                "plan_summary": plan.summary,
                "goals": [self._goal_payload(goal) for goal in plan.goals],
                "results": [self._result_payload(result) for result in results],
            },
            indent=2,
        )
        raw = self._planner_complete(
                        final_summary_instructions(use_tags=self._use_tagged_planner_control())
                        + "\n\nALREADY COMPLETED (do NOT suggest any of this as next steps):"
            + "\n"
            + completed_block
            + "\n\n"
            + textwrap.dedent(
                """
                Requirements:
                - next_steps must be NEW work not covered by any completed goal above
                - a step is NOT new if any completed goal already implemented, integrated, or added what the step describes
                - next_steps must be strictly remaining work, optional follow-up, or explicit validation that is not already completed
                - avoid generic suggestions like add tests unless tied to the result
                - if the requested work is already complete and no concrete follow-up remains, return an empty next_steps list
                - prefer 0 to 4 next steps, not forced suggestions
                """
            ).strip(),
            prompt,
        )
        payload = self._load_final_summary_payload(raw)
        summary = str(payload.get("summary") or "Plan execution finished.").strip()
        next_steps = [str(item) for item in payload.get("next_steps") or [] if str(item).strip()]
        next_steps = self._filter_completed_next_steps(plan, results, next_steps)
        lines = [
            "Execution Summary",
            f"- {summary}",
        ]
        if next_steps:
            lines.extend(["", "Next Steps"])
            lines.extend(f"{index}. {item}" for index, item in enumerate(next_steps, start=1))
        else:
            lines.extend(["", "Next Steps", "- None."])
        return "\n".join(lines)

    def _meaningful_tokens(self, text: str) -> List[str]:
        stop_words = {
            "the", "and", "for", "with", "that", "this", "from", "into", "after", "before",
            "then", "than", "when", "where", "while", "will", "would", "could", "should",
            "about", "have", "has", "had", "already", "user", "users", "app", "flow",
            "work", "goal", "result", "complete", "completed", "update", "updated", "implement",
            "implemented", "verify", "validation", "specific", "next", "steps", "step",
        }
        return [
            token for token in re.findall(r"[a-z0-9_]+", text.lower())
            if len(token) >= 3 and token not in stop_words
        ]

    def _completed_scope_texts(self, plan: PlannerPlan, results: List[GoalExecutionResult]) -> List[str]:
        texts: List[str] = [plan.original_request, plan.summary, plan.clarification_summary]
        for goal in plan.goals:
            texts.extend([goal.title, goal.goal, goal.reason])
            texts.extend(goal.delegation_notes)
            texts.extend(goal.success_signals)
        for result in results:
            texts.extend([result.title, result.final_message, result.delegated_task, result.commentary_for_next_goal])
        return [text.strip() for text in texts if text and text.strip()]

    def _next_step_is_probably_completed(self, next_step: str, completed_texts: List[str]) -> bool:
        next_step_normalized = " ".join(self._meaningful_tokens(next_step))
        next_tokens = set(self._meaningful_tokens(next_step))
        if not next_tokens:
            return False

        # Per-text check: any single completed text covers most of the step
        for completed in completed_texts:
            completed_tokens = set(self._meaningful_tokens(completed))
            if not completed_tokens:
                continue
            completed_normalized = " ".join(self._meaningful_tokens(completed))
            if next_step_normalized and next_step_normalized in completed_normalized:
                return True
            overlap = len(next_tokens & completed_tokens)
            if overlap == 0:
                continue
            coverage = overlap / max(1, len(next_tokens))
            if coverage >= 0.6:
                return True

        # Aggregate pool check: the entire body of completed work collectively
        # covers the step's vocabulary, meaning the step introduces no new concepts
        all_completed_tokens: set = set()
        for completed in completed_texts:
            all_completed_tokens.update(self._meaningful_tokens(completed))
        if all_completed_tokens and next_tokens:
            aggregate_coverage = len(next_tokens & all_completed_tokens) / max(1, len(next_tokens))
            if aggregate_coverage >= 0.65:
                return True
        return False

    def _filter_completed_next_steps(
        self,
        plan: PlannerPlan,
        results: List[GoalExecutionResult],
        next_steps: List[str],
    ) -> List[str]:
        completed_texts = self._completed_scope_texts(plan, results)
        filtered: List[str] = []
        seen: Dict[str, None] = {}
        for step in next_steps:
            normalized = step.strip()
            if not normalized or normalized in seen:
                continue
            seen[normalized] = None
            if self._next_step_is_probably_completed(normalized, completed_texts):
                continue
            filtered.append(normalized)
        return filtered

    def _goal_payload(self, goal: PlannerGoal) -> Dict[str, Any]:
        return {
            "goal_id": goal.goal_id,
            "title": goal.title,
            "goal": goal.goal,
            "reason": goal.reason,
            "depends_on": goal.depends_on,
            "preserve_context": goal.preserve_context,
            "parallelizable": goal.parallelizable,
            "estimated_scope": goal.estimated_scope,
            "delegation_notes": goal.delegation_notes,
            "success_signals": goal.success_signals,
            "relevant_fact_keys": goal.relevant_fact_keys,
        }

    def _discovery_request_payload(self, request: Optional[DiscoveryRequest]) -> Optional[Dict[str, Any]]:
        if request is None:
            return None
        return {
            "reason": request.reason,
            "prompt": request.prompt,
            "recommended_mode": request.recommended_mode,
        }

    def _discovery_result_payload(self, result: Optional[DiscoveryResult]) -> Optional[Dict[str, Any]]:
        if result is None:
            return None
        return {
            "mode": result.mode,
            "reason": result.reason,
            "prompt": result.prompt,
            "delegated_task": result.delegated_task,
            "final_message": result.final_message,
            "ok": result.ok,
            "task_satisfied": result.task_satisfied,
            "validation_ran": result.validation_ran,
            "validation_passed": result.validation_passed,
            "validation_summary": result.validation_summary,
            "detailed_findings": _discovery_findings_lines(result),
            "worker_history_summary": result.worker_history_summary,
            "touched_paths": result.touched_paths,
            "duration_s": result.duration_s,
            "tool_calls_used": result.tool_calls_used,
            "tool_calls_max": result.tool_calls_max,
            "usage_summary": result.usage_summary,
        }

    def _plan_payload(self, plan: Optional[PlannerPlan]) -> Optional[Dict[str, Any]]:
        if plan is None:
            return None
        return {
            "original_request": plan.original_request,
            "summary": plan.summary,
            "assumptions": plan.assumptions,
            "clarification_summary": plan.clarification_summary,
            "goals": [self._goal_payload(goal) for goal in plan.goals],
            "next_steps_preview": plan.next_steps_preview,
            "confirmation_prompt": plan.confirmation_prompt,
        }

    def _result_payload(self, result: GoalExecutionResult) -> Dict[str, Any]:
        return {
            "goal_id": result.goal_id,
            "title": result.title,
            "final_message": result.final_message,
            "task_satisfied": result.task_satisfied,
            "validation_ran": result.validation_ran,
            "validation_passed": result.validation_passed,
            "validation_summary": result.validation_summary,
            "worker_history_summary": result.worker_history_summary,
            "touched_paths": result.touched_paths,
            "preserve_context_used": result.preserve_context_used,
            "duration_s": result.duration_s,
            "commentary_for_next_goal": result.commentary_for_next_goal,
            "status": result.status,
            "usage_summary": result.usage_summary,
        }

    def _revision_context_payload(self) -> Optional[Dict[str, Any]]:
        if self.session.last_presented_plan is None:
            return None
        if not self.session.awaiting_plan_revision and self.session.pending_plan is None:
            return None
        return {
            "status": "awaiting_revision" if self.session.awaiting_plan_revision else "pending_plan_revision",
            "prior_plan": self._plan_payload(self.session.last_presented_plan),
            "feedback": self.session.plan_revision_feedback[-5:],
        }

    def _follow_up_context_payload(self) -> Optional[Dict[str, Any]]:
        if self.session.last_completed_plan is None:
            return None
        return {
            "prior_plan": self._plan_payload(self.session.last_completed_plan),
            "prior_results": [self._result_payload(result) for result in self.session.last_completed_results[-5:]],
            "prior_execution_summary": self.session.last_execution_summary,
            "prior_discovery": self._discovery_result_payload(self.session.last_discovery),
        }

    def _normalize_discovery_mode(self, value: str) -> str:
        normalized = value.strip().lower()
        if normalized in DISCOVERY_MODES:
            return normalized
        return "moderate"

    def _parse_discovery_selection(self, text: str) -> Optional[str]:
        normalized = text.strip().lower()
        mapping = {
            "1": "quick",
            "quick": "quick",
            "quick scan": "quick",
            "2": "moderate",
            "moderate": "moderate",
            "moderate scan": "moderate",
            "3": "deep",
            "deep": "deep",
            "deep scan": "deep",
        }
        return mapping.get(normalized)

    def _is_approval(self, text: str) -> bool:
        return text.lower() in {"approve", "approved", "yes", "y", "/approve", "/run"}

    def _is_rejection(self, text: str) -> bool:
        return text.lower() in {"reject", "rejected", "no", "n", "/reject", "/cancel"}

    def _session_status(self) -> str:
        if self.session.executing:
            return "executing"
        if self.session.pending_plan is not None:
            return "awaiting_plan_approval"
        if self.session.pending_discovery is not None:
            return "awaiting_discovery_selection"
        if self.session.awaiting_plan_revision:
            return "awaiting_plan_revision"
        if self.session.last_completed_plan is not None:
            return "completed"
        if self.session.intake_messages:
            return "planning"
        return "idle"

    def _suggested_next_actions_payload(self) -> List[Dict[str, Any]]:
        actions: List[Dict[str, Any]] = []
        if self.session.pending_plan is not None:
            actions.extend(
                [
                    {"type": "approve_plan", "label": "Approve Plan", "style": "primary"},
                    {"type": "reject_plan", "label": "Reject Plan", "style": "secondary"},
                ]
            )
        elif self.session.pending_discovery is not None:
            for mode in [DISCOVERY_MODES["quick"], DISCOVERY_MODES["moderate"], DISCOVERY_MODES["deep"]]:
                actions.append(
                    {
                        "type": "select_discovery_mode",
                        "label": mode.label,
                        "style": "primary" if mode.key == self.session.pending_discovery.recommended_mode else "secondary",
                        "mode": mode.key,
                        "budget": mode.max_tool_calls,
                    }
                )
            actions.append({"type": "skip_discovery", "label": "Skip Discovery", "style": "secondary"})
        elif not self.session.executing:
            actions.append({"type": "start_continuous", "label": "Start Continuous", "style": "primary"})
            payload = self._load_repo_facts_payload() or {}
            for issue in payload.get("reopenable_issues", [])[:3] if isinstance(payload, dict) else []:
                if not isinstance(issue, dict):
                    continue
                issue_id = str(issue.get("issue_id", "") or "").strip()
                if not issue_id:
                    continue
                label = str(issue.get("plan_summary", "") or issue.get("request_summary", "") or issue_id).strip()
                actions.append(
                    {
                        "type": "reopen_issue",
                        "issue_id": issue_id,
                        "label": f"Reopen {issue_id}: {label[:36]}",
                        "style": "secondary",
                    }
                )

        payload = self._load_repo_facts_payload() or {}
        has_issue_state = False
        if isinstance(payload, dict):
            has_issue_state = bool(payload.get("active_issue") or payload.get("reopenable_issues") or payload.get("issues"))
        if self.session.intake_messages or self.session.last_completed_plan is not None:
            actions.append({"type": "reset_session", "label": "Reset Session", "style": "ghost"})
        if self.continuous_state.status not in {"idle", "stopped"}:
            actions.append({"type": "stop_continuous", "label": "Stop Continuous", "style": "secondary"})
        actions.append(
            {
                "type": "delete_session",
                "label": "Delete Session",
                "style": "ghost",
                "requires_confirmation": True,
                "confirmation_prompt": "Delete this session completely? This clears live planner state, repo facts, and observability for the current workspace.",
            }
        )
        return actions

    def export_state(self) -> Dict[str, Any]:
        worker_state_getter = getattr(self.worker, "export_runtime_state", None)
        worker_state = None
        if callable(worker_state_getter):
            try:
                worker_state = worker_state_getter()
            except Exception as exc:
                worker_state = {"bridge_warning": f"worker export_runtime_state failed: {exc}"}
        try:
            issue_state = self._load_repo_facts_payload() or {}
        except Exception as exc:
            issue_state = {"bridge_warning": f"planner issue_state load failed: {exc}"}
        try:
            repo_facts_status_lines = self.repo_facts_status_lines()
        except Exception as exc:
            repo_facts_status_lines = [f"repo_facts : unavailable ({exc})"]
        return {
            "runtime_config": {
                "provider": self.config.provider,
                "model": self.config.model,
                "thinking_mode": self.config.thinking_mode,
                "verbosity": self.config.verbosity,
            },
            "status": self._session_status(),
            "latest_request": self.session.latest_request,
            "project_intent": self._load_project_intent_payload(),
            "continuous_mode": {
                "enabled": self.continuous_state.enabled,
                "status": self.continuous_state.status,
                "cycle": self.continuous_state.cycle,
                "max_cycles": self.continuous_state.max_cycles,
                "active_issue_id": self.continuous_state.active_issue_id,
                "selected_discovery_mode": self.continuous_state.selected_discovery_mode,
                "latest_review_decision": self.continuous_state.latest_review_decision,
                "stop_reason": self.continuous_state.stop_reason,
                "created_followup_issue_ids": list(self.continuous_state.created_followup_issue_ids),
            },
            "pending_plan": self._plan_payload(self.session.pending_plan),
            "last_presented_plan": self._plan_payload(self.session.last_presented_plan),
            "pending_discovery": self._discovery_request_payload(self.session.pending_discovery),
            "last_discovery": self._discovery_result_payload(self.session.last_discovery),
            "last_completed_plan": self._plan_payload(self.session.last_completed_plan),
            "last_completed_results": [self._result_payload(result) for result in self.session.last_completed_results[-5:]],
            "completed_results": [self._result_payload(result) for result in self.session.completed_results[-5:]],
            "last_execution_summary": self.session.last_execution_summary,
            "executing": self.session.executing,
            "executing_goal_index": self.session.executing_goal_index,
            "executing_goal_id": self.session.executing_goal_id,
            "executing_goal_title": self.session.executing_goal_title,
            "executing_goal_count": self.session.executing_goal_count,
            "awaiting_plan_revision": self.session.awaiting_plan_revision,
            "plan_revision_feedback": self.session.plan_revision_feedback[-5:],
            "active_issue_id": self.session.active_issue_id,
            "issue_state": issue_state,
            "repo_facts_status_lines": repo_facts_status_lines,
            "planner_usage_summary": self._render_planner_usage_summary(),
            "suggested_next_actions": self._suggested_next_actions_payload(),
            "worker_state": worker_state,
        }


def print_planner_banner(planner: PlannerAgent) -> None:
    config = planner.config
    details = [
        f"provider  : {config.provider}",
        f"model     : {config.model}",
        f"root      : {config.root}",
        f"tools     : {config.tool_script}",
        f"max_steps : {config.max_steps}",
        f"thinking  : {config.thinking_mode}",
    ]
    if config.provider == "openai":
        details.append(f"verbosity : {config.verbosity}")

    commands = [
        "/approve   execute the pending plan",
        "/discover  show the current pending discovery offer",
        "/runtime-show  show active provider/model",
        "/runtime <p> <m>  switch provider/model without restarting",
        "/model <m>  switch model within the current provider",
        "/providers  list supported providers",
        "/models [provider]  list suggested models for a provider",
        "/backoff <k>  set backoff token limit in thousands (e.g. 30 = 30k/min)",
        "/backoff off  disable backoff strategy",
        "/reject    reject the pending plan",
        "/plan      show the current pending plan",
        "/reset     clear the planner session",
        "/worker    enter direct worker debug mode",
        "/quit      exit",
    ]

    print(_cli_panel("Planner Agent", details))
    print(_cli_panel("Repo Facts", planner.repo_facts_status_lines()))
    print(_cli_panel("Commands", commands))


def _terminal_width(default: int = 88) -> int:
    try:
        return max(60, min(shutil.get_terminal_size((default, 20)).columns, 120))
    except Exception:
        return default


def _cli_supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") in {None, "", "dumb"}:
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _ansi(text: str, *codes: str) -> str:
    if not _cli_supports_color() or not codes:
        return text
    return f"\033[{';'.join(codes)}m{text}\033[0m"


def _panel_palette(title: str) -> Dict[str, str]:
    normalized = title.strip().lower()
    if normalized in {"plan", "planner agent"}:
        return {"border": "36", "title": "1;36", "body": "0", "footer": "2;37"}
    if normalized in {"execution", "goal result"}:
        return {"border": "32", "title": "1;32", "body": "0", "footer": "2;37"}
    if normalized in {"discovery offer", "discovery result", "commands"}:
        return {"border": "34", "title": "1;34", "body": "0", "footer": "2;37"}
    if normalized in {"clarification"}:
        return {"border": "33", "title": "1;33", "body": "0", "footer": "2;37"}
    return {"border": "37", "title": "1;37", "body": "0", "footer": "2;37"}


def _wrap_cli_line(text: str, width: int) -> List[str]:
    if not text:
        return [""]
    stripped = text.lstrip()
    indent = text[: len(text) - len(stripped)]
    bullet_match = re.match(r"([-*]|\d+\.)\s+", stripped)
    if bullet_match:
        bullet = indent + bullet_match.group(0)
        rest = stripped[bullet_match.end():].strip()
        wrapped = textwrap.wrap(
            rest,
            width=max(20, width - len(bullet)),
            initial_indent=bullet,
            subsequent_indent=" " * len(bullet),
            break_long_words=False,
            break_on_hyphens=False,
        )
        return wrapped or [bullet.rstrip()]
    if ": " in stripped and not stripped.endswith(":"):
        key, value = stripped.split(": ", 1)
        prefix = indent + key + ": "
        wrapped = textwrap.wrap(
            value,
            width=max(20, width - len(prefix)),
            initial_indent=prefix,
            subsequent_indent=" " * len(prefix),
            break_long_words=False,
            break_on_hyphens=False,
        )
        return wrapped or [prefix.rstrip()]
    return textwrap.wrap(
        text,
        width=width,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    ) or [text]


def _cli_panel(title: str, body_lines: List[str], footer_lines: Optional[List[str]] = None) -> str:
    width = _terminal_width()
    inner_width = width - 4
    top = "+" + "-" * max(10, width - 2) + "+"
    title_line = f"| {title[:inner_width].ljust(inner_width)} |"
    palette = _panel_palette(title)
    lines = [
        _ansi(top, palette["border"]),
        _ansi(title_line, palette["title"]),
        _ansi(top, palette["border"]),
    ]
    for raw_line in body_lines:
        for wrapped in _wrap_cli_line(raw_line, inner_width):
            lines.append(_ansi(f"| {wrapped[:inner_width].ljust(inner_width)} |", palette["body"]))
    if footer_lines:
        lines.append(_ansi(top, palette["border"]))
        for raw_line in footer_lines:
            for wrapped in _wrap_cli_line(raw_line, inner_width):
                lines.append(_ansi(f"| {wrapped[:inner_width].ljust(inner_width)} |", palette["footer"]))
    lines.append(_ansi(top, palette["border"]))
    return "\n".join(lines)


def _detect_cli_title(message: str) -> str:
    first_line = next((line.strip() for line in message.splitlines() if line.strip()), "Planner")
    if first_line.startswith("Plan summary:"):
        return "Plan"
    if first_line.startswith("Discovery suggested:"):
        return "Discovery Offer"
    if first_line.startswith("Discovery complete:"):
        return "Discovery Result"
    if first_line.startswith("Executing confirmed plan."):
        return "Execution"
    if first_line.startswith("Goal "):
        return "Goal Result"
    if first_line.endswith("?"):
        return "Clarification"
    return "Planner"


def _format_cli_message(message: str, usage_line: Optional[str] = None) -> str:
    blocks = [block.strip("\n") for block in message.strip().split("\n\n") if block.strip()]
    if not blocks:
        blocks = [message.strip()]
    title = _detect_cli_title(blocks[0] if blocks else message)
    body_lines: List[str] = []
    for index, block in enumerate(blocks):
        if index > 0:
            body_lines.append("")
        body_lines.extend(block.splitlines())
    footer = [usage_line] if usage_line else None
    return _cli_panel(title, body_lines, footer_lines=footer)


def interactive_planner_loop(
    planner: PlannerAgent,
    worker_debug_loop: Optional[Callable[[Any], None]] = None,
    runtime_reconfigure: Optional[Callable[[str, str], Dict[str, Any]]] = None,
    backoff_configure: Optional[Callable[[str], Dict[str, Any]]] = None,
) -> None:
    def parse_models_command(raw: str, current_provider: str) -> Optional[str]:
        if raw == "/models":
            return current_provider.strip().lower()
        if raw.startswith("/models "):
            provider = raw[len("/models "):].strip().lower()
            if not provider:
                raise ValueError("Usage: /models [provider]")
            return provider
        return None

    def print_with_usage(message: str, *, include_usage: bool = True) -> None:
        usage_line = planner._render_planner_usage_summary() if include_usage else None
        print(_format_cli_message(message, usage_line=usage_line))

    print_planner_banner(planner)

    while True:
        try:
            raw = input("\nplanner> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n" + _cli_panel("Planner", ["bye"]))
            return

        if not raw:
            continue

        if raw in {"/quit", "quit", "exit"}:
            print(_cli_panel("Planner", ["bye"]))
            return

        if raw == "/plan":
            print(_format_cli_message(planner.show_pending_plan()))
            continue

        if raw == "/discover":
            print(_format_cli_message(planner.show_pending_discovery()))
            continue

        if raw == "/runtime-show":
            runtime_lines = [
                f"provider : {planner.config.provider}",
                f"model    : {planner.config.model}",
                f"thinking : {planner.config.thinking_mode}",
            ]
            if planner.config.provider in {"openai", "local"}:
                runtime_lines.append(f"verbosity: {planner.config.verbosity}")
            if backoff_configure is not None:
                bs = backoff_configure("show")
                if bs.get("enabled"):
                    runtime_lines.append(f"backoff  : on ({bs['token_limit_k']}k/min, window used: {bs['window_tokens_used']})")
                else:
                    runtime_lines.append("backoff  : off")
            print(_cli_panel("Runtime", runtime_lines))
            continue

        if raw.startswith("/backoff") and backoff_configure is not None:
            remainder = raw[len("/backoff"):].strip()
            if remainder in {"off", "0", "false", "disable"}:
                backoff_configure("off")
                print(_cli_panel("Backoff", ["Backoff disabled."]))
            elif remainder in {"", "show"}:
                bs = backoff_configure("show")
                if bs.get("enabled"):
                    print(_cli_panel("Backoff", [f"on ({bs['token_limit_k']}k input tokens/min, window used: {bs['window_tokens_used']})"]))
                else:
                    print(_cli_panel("Backoff", ["off"]))
            else:
                try:
                    limit_k = int(remainder)
                    if limit_k <= 0:
                        print(_cli_panel("Backoff", ["Token limit must be a positive number (in thousands)."]))
                    else:
                        backoff_configure(str(limit_k))
                        print(_cli_panel("Backoff", [f"Enabled: {limit_k}k input tokens/min. Will pause 60s at limit."]))
                except ValueError:
                    print(_cli_panel("Backoff", ["Usage: /backoff <tokens_in_thousands>  or  /backoff off"]))
            continue

        if raw == "/providers":
            print(_cli_panel("Providers", runtime_provider_lines(planner.config.provider)))
            continue

        try:
            models_provider = parse_models_command(raw, planner.config.provider)
        except ValueError as exc:
            print(_cli_panel("Runtime", [str(exc)]))
            continue
        if models_provider is not None:
            try:
                print(_cli_panel("Models", runtime_model_lines(models_provider, planner.config.model)))
            except Exception as exc:
                print(_cli_panel("Runtime", [f"Failed to list models: {exc}"]))
            continue

        if raw.startswith("/runtime ") or raw.startswith("/model "):
            if runtime_reconfigure is None:
                print(_cli_panel("Runtime", ["Runtime switching is not available in this entrypoint."]))
                continue
            try:
                if raw.startswith("/runtime "):
                    remainder = raw[len("/runtime "):].strip()
                    parts = remainder.split(None, 1)
                    if len(parts) != 2:
                        raise ValueError("Usage: /runtime <provider> <model>")
                    provider, model = parts[0].strip().lower(), parts[1].strip()
                else:
                    provider = str(planner.config.provider).strip().lower()
                    model = raw[len("/model "):].strip()
                    if not model:
                        raise ValueError("Usage: /model <model>")
                updated = runtime_reconfigure(provider, model)
                print(_cli_panel("Runtime", [
                    "Runtime updated without restarting the process.",
                    f"provider : {updated['provider']}",
                    f"model    : {updated['model']}",
                ]))
            except Exception as exc:
                print(_cli_panel("Runtime", [f"Failed to update runtime: {exc}"]))
            continue

        if raw == "/reset":
            planner.clear_session()
            print(_cli_panel("Planner", ["Planner session cleared."]))
            continue

        if raw == "/approve":
            print_with_usage(planner.execute_pending_plan())
            continue

        if raw == "/reject":
            if planner.session.pending_plan is None:
                print(_cli_panel("Planner", ["No pending plan to reject."]))
            else:
                planner.session.last_presented_plan = planner.session.pending_plan
                planner.session.pending_plan = None
                planner.session.awaiting_plan_revision = True
                print(_cli_panel("Planner", ["Plan rejected. Describe what should change."]))
            continue

        if raw == "/worker":
            if worker_debug_loop is None:
                print(_cli_panel("Planner", ["Worker debug loop is not available."]))
            else:
                worker_debug_loop(planner.worker)
            continue

        if planner.session.pending_plan is None and not planner.session.intake_messages:
            print_with_usage(planner.start_request(raw))
            continue

        print_with_usage(planner.continue_conversation(raw))
