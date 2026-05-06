#!/usr/bin/env python3
from __future__ import annotations

"""Beta planner entrypoint backed by TreeLoop.

This module keeps the fast TreeLoop command grammar and context tree, but it
wraps the loop in the existing planner contract so discovery, goal execution,
and per-goal completion gating can run through the same PlannerAgent flow used
by the main harness.
"""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# Load .env without overriding existing variables.
env_path = Path(__file__).parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

from main import (  # noqa: E402
    ActionResult,
    FACT_ACTION_TYPES,
    WorkerRunResult,
    WorkerValidationResult,
    _emit_bridge_message,
    _handle_bridge_planner_action,
    create_model_client,
    extract_first_json_object,
    refresh_runtime_provider_catalog_once,
)
from issue_facts import (  # noqa: E402
    FACT_TYPE_ARCHITECTURE,
    FACT_TYPE_GOAL,
    IssueFactLedger,
    IssueFactRecord,
    format_issue_not_found,
    format_issue_record_detail,
    format_issue_summary_list,
    issue_summaries_from_payload,
)
from planner import PlannerAgent, interactive_planner_loop  # noqa: E402
from runtime_catalog import runtime_options_payload  # noqa: E402
from skill_loader import load_markdown_skills_from_dir  # noqa: E402
from tree_commands import CommandResult  # noqa: E402
from tree_loop import TreeLoop, Turn  # noqa: E402


REPO_FACTS_FILENAME = "repo_facts.md"
OBSERVABILITY_TRACE_BLOCK_LIMIT = 24


def _normalize_cli_argv(argv: Sequence[str]) -> List[str]:
    normalized: List[str] = []
    for token in argv:
        if token.startswith("—") or token.startswith("–"):
            normalized.append("--" + token[1:])
        else:
            normalized.append(token)
    return normalized


def print_result(result: Any) -> Any:
    print("\n" + "=" * 60)
    print(result.summary())
    print("=" * 60)
    return result


def print_worker_status(worker: "TreeLoopPlannerWorker") -> None:
    print("Repo Facts:")
    for line in worker.repo_facts_status_lines():
        print(f"  {line}")
    patch_resolution = worker.patch_resolution_state()
    if patch_resolution is not None:
        print("Patch Resolution:")
        print(f"  path       : {str(patch_resolution.get('path', '') or '').strip() or '(unknown)'}")
        print(f"  failed     : {str(patch_resolution.get('failed_action_type', '') or 'patch').strip()}")
        reason = str(patch_resolution.get("reason", "") or "").strip()
        if reason:
            print(f"  reason     : {reason}")
    latest_review = worker.latest_review_state()
    if latest_review is not None:
        summary = str(latest_review.get("summary", "") or "").strip()
        action_type = str(latest_review.get("action_type", "") or "host_validation")
        path = str(latest_review.get("path", "") or "").strip()
        print("Latest Review:")
        print(f"  action     : {action_type}")
        if path:
            print(f"  path       : {path}")
        if summary:
            print(f"  summary    : {summary}")
    suggested_actions = worker.runtime_suggested_next_actions()
    if suggested_actions:
        print("Suggested Next Actions:")
        for item in suggested_actions[:6]:
            label = str(item.get("label", item.get("type", "action")) or "action")
            print(f"  {label}")


def print_history(result: Any) -> None:
    for turn in result.turns:
        print(f"\n── Turn {turn.turn_number} ({turn.elapsed_s:.1f}s) ──")
        if turn.thought:
            suffix = "…" if len(turn.thought) > 300 else ""
            print(f"THOUGHT: {turn.thought[:300]}{suffix}")
        print(f"COMMANDS: {len(turn.commands_issued)}")
        for cmd, command_result in zip(turn.commands_issued, turn.results):
            ok = "✓" if command_result.ok else "✗"
            out_preview = command_result.output[:150].replace("\n", "\\n")
            print(f"  [{ok}] {cmd[:80]} → {out_preview}")
def _checkpoint_prompt(loop: TreeLoop, turn_num: int) -> bool:
    print()
    print("━" * 60)
    print(f"  CHECKPOINT — {turn_num} turns used")
    print(f"  reads: {loop._total_reads}  writes: {loop._total_writes}")
    if loop.history:
        last = loop.history[-1]
        justifications = [annotation.content for annotation in last.annotations if annotation.tag == "ju"]
        if justifications:
            for justification in justifications:
                print(f"  >>ju: {justification}")
        if last.thought:
            preview = last.thought[:200] + ("…" if len(last.thought) > 200 else "")
            print(f"  Last thought: {preview}")
    print("━" * 60)
    while True:
        try:
            answer = input("  proceed / stop? ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if answer in ("proceed", "p", "yes", "y", ""):
            return True
        if answer in ("stop", "s", "no", "n"):
            return False
        print("  (type 'proceed' or 'stop')")


@dataclass
class TreeLoopDiscoveryBudget:
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


class TreeLoopPlannerWorker:
    def __init__(
        self,
        *,
        model: Any,
        root: Path,
        max_turns: int = 100,
        checkpoint_interval: int = 20,
        thinking_mode: str = "low",
        provider: str = "gemini",
        model_name: str = "gemini-2.5-flash",
        verbosity: str = "medium",
        verbose: bool = True,
    ) -> None:
        self.model = model
        self.root = root.resolve()
        self.max_turns = max_turns
        self.checkpoint_interval = checkpoint_interval
        self.thinking_mode = thinking_mode
        self.provider = provider
        self.model_name = model_name
        self.verbosity = verbosity
        self.verbose = verbose
        self.on_step_callback: Optional[Callable[[Any], None]] = None
        self.bridge_progress_domain = ""
        self._bridge_step_counter = 0
        self.history: List[Turn] = []
        self.discovery_budget: Optional[TreeLoopDiscoveryBudget] = None
        self.goal_fact_keys: List[str] = []
        self._goal_skill_mode_pending = False
        self._goal_skill_mode_used = False
        self.fact_map: Dict[str, IssueFactRecord] = {}
        self.steering_prompt = ""
        self.issue_ledger = IssueFactLedger.load(self._repo_facts_path())
        self._repo_facts_loaded_count = self.issue_ledger.total_fact_count()
        active_issue = self.issue_ledger.active_issue()
        self.active_issue_id = active_issue.issue_id if active_issue is not None else ""
        self._current_task = ""
        self._task_satisfied = False
        self._completion_check_pending = False
        self._completion_check_reason = ""
        self._pending_verification: Optional[Dict[str, Any]] = None
        self._patch_resolution: Optional[Dict[str, Any]] = None
        self._discovery_remediation: Optional[Dict[str, Any]] = None
        self._pending_npm_command: Optional[Dict[str, Any]] = None
        self._edit_batch_mode = False
        self._edit_batch_pending: Dict[str, Dict[str, Any]] = {}
        self._edit_batch_last_failure: Optional[Dict[str, Any]] = None
        self._has_mutation = False
        self._validation_after_mutation = False
        self._run_sequence = 0
        self._active_run_id = 0
        self._last_validation = WorkerValidationResult(kind="none", passed=True, summary="No mutating actions required validation.")
        self._latest_review: Optional[Dict[str, Any]] = None
        self._observability_buffer: List[str] = []
        self._observability_started_at = 0.0
        self._observability_metrics: Dict[str, Any] = {}
        self._llm_activity: Dict[str, Any] = {
            "in_flight": False,
            "turn": 0,
            "last_event": "idle",
            "elapsed_s": 0.0,
            "output_chars": 0,
            "error": "",
        }
        self.loop = self._build_loop(model)
        self.history = self.loop.history
        self._sync_fact_map()

    def _build_loop(self, model: Any) -> TreeLoop:
        loop = TreeLoop(
            model=model,
            workspace_root=self.root,
            max_turns=self.max_turns,
            checkpoint_interval=self.checkpoint_interval,
            checkpoint_callback=_checkpoint_prompt,
            steering=self._compose_steering(),
            get_fact_records=self._get_fact_records,
            get_status=self._status_payload,
            verbose=self.verbose,
            tool_dispatcher=self._dispatch_tool_action,
            command_observer=self._observe_command_result,
            model_event_observer=self._observe_model_event,
        )
        loop.register_skill(
            "count_py",
            "Count Python files in the repo",
            handler=lambda: str(len(loop.bridge.tree.find("/repo", "*.py"))) + " Python files found",
        )
        bundled_skills_dir = Path(__file__).resolve().parent / "skills"
        workspace_skills_dir = self.root / "skills"
        for directory in [bundled_skills_dir, workspace_skills_dir]:
            for skill in load_markdown_skills_from_dir(directory):
                loop.register_skill(
                    skill.name,
                    skill.description,
                    args_schema=skill.args_schema,
                    tags=skill.tags,
                    category=skill.category,
                    priority=skill.priority,
                    modes=skill.modes,
                    cache=skill.cache,
                    handler=skill.render,
                )
        loop.bridge.tree.set_fact("demo", "architecture", "entrypoint", "live_test_loop.py is the beta TreeLoop entrypoint")
        loop.bridge.tree.set_fact("demo", "architecture", "planner", "planner.py orchestrates discovery and goals")
        loop.setup()
        return loop

    def reconfigure_runtime(
        self,
        *,
        provider: str,
        model: str,
        thinking_mode: Optional[str] = None,
        verbosity: Optional[str] = None,
    ) -> Dict[str, Any]:
        self.provider = str(provider or self.provider).strip().lower()
        self.model_name = str(model or self.model_name).strip() or self.model_name
        if thinking_mode is not None:
            self.thinking_mode = str(thinking_mode or self.thinking_mode).strip() or self.thinking_mode
        if verbosity is not None:
            self.verbosity = str(verbosity or self.verbosity).strip() or self.verbosity
        self.model = create_model_client(
            provider=self.provider,
            model=self.model_name,
            thinking_mode=self.thinking_mode,
            verbosity=self.verbosity,
        )
        self.loop = self._build_loop(self.model)
        self.history = self.loop.history
        self._sync_fact_map()
        return {
            "provider": self.provider,
            "model": self.model_name,
            "thinking_mode": self.thinking_mode,
            "verbosity": self.verbosity,
        }

    def set_steering(self, prompt: str) -> None:
        self.steering_prompt = str(prompt or "").strip()
        self._refresh_loop_steering()

    def clear_steering(self) -> None:
        self.steering_prompt = ""
        self._refresh_loop_steering()

    def _delete_session_artifacts(self) -> None:
        targets = [self._repo_facts_path(), self._observability_path()]
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
        self.loop.history.clear()
        self.loop._recent_reads.clear()
        self.loop._total_reads = 0
        self.loop._total_writes = 0
        self.history = self.loop.history
        self.discovery_budget = None
        self.goal_fact_keys = []
        self._goal_skill_mode_pending = False
        self._goal_skill_mode_used = False
        self.fact_map = {}
        self.steering_prompt = ""
        self.issue_ledger = IssueFactLedger.empty()
        self._repo_facts_loaded_count = 0
        self.active_issue_id = ""
        self._current_task = ""
        self._task_satisfied = False
        self._completion_check_pending = False
        self._completion_check_reason = ""
        self._pending_verification = None
        self._patch_resolution = None
        self._discovery_remediation = None
        self._pending_npm_command = None
        self._edit_batch_mode = False
        self._edit_batch_pending = {}
        self._edit_batch_last_failure = None
        self._has_mutation = False
        self._validation_after_mutation = False
        self._run_sequence = 0
        self._active_run_id = 0
        self._last_validation = WorkerValidationResult(kind="none", passed=True, summary="No mutating actions required validation.")
        self._latest_review = None
        self._observability_buffer = []
        self._observability_started_at = 0.0
        self._observability_metrics = {}
        self._bridge_step_counter = 0
        self._refresh_loop_steering()
        return "Session deleted. Repo facts and observability were cleared."

    def set_goal_fact_keys(self, keys: List[str]) -> None:
        seen: List[str] = []
        for key in keys:
            normalized = str(key or "").strip()
            if normalized and normalized not in seen:
                seen.append(normalized)
        self.goal_fact_keys = seen
        self._refresh_loop_steering()

    def clear_goal_fact_keys(self) -> None:
        self.goal_fact_keys = []
        self._refresh_loop_steering()

    def configure_discovery_budget(self, mode_key: str, mode_label: str, max_tool_calls: int) -> None:
        self.discovery_budget = TreeLoopDiscoveryBudget(
            mode_key=str(mode_key or "discovery").strip() or "discovery",
            mode_label=str(mode_label or "Discovery").strip() or "Discovery",
            max_tool_calls=max(0, int(max_tool_calls)),
        )
        self._refresh_loop_steering()

    def clear_discovery_budget(self) -> None:
        self.discovery_budget = None
        self._refresh_loop_steering()

    def prepare_for_goal(self, preserve_context: bool) -> None:
        self._reset_guard_state()
        self._goal_skill_mode_pending = True
        self._goal_skill_mode_used = False
        if not preserve_context:
            self.loop.history.clear()
            self.loop._recent_reads.clear()
            self.loop._total_reads = 0
            self.loop._total_writes = 0
        self.history = self.loop.history
        self._current_task = ""

    def render_last_usage_summary(self) -> str:
        metrics = getattr(self.model, "get_last_metrics", None)
        if not callable(metrics):
            return "Usage: unavailable"
        try:
            payload = metrics() or {}
        except Exception:
            return "Usage: unavailable"
        usage = payload.get("usage") if isinstance(payload, dict) else None
        if not isinstance(usage, dict):
            return "Usage: unavailable"
        input_tokens = int(usage.get("input_tokens", 0) or 0)
        output_tokens = int(usage.get("output_tokens", 0) or 0)
        return f"Usage: input={input_tokens:,} output={output_tokens:,}"

    def configure_backoff(self, *, enabled: bool, token_limit_k: int = 0) -> Dict[str, Any]:
        backoff = getattr(self.model, "backoff", None)
        if backoff is not None:
            try:
                backoff.enabled = bool(enabled)
                backoff.token_limit_k = max(0, int(token_limit_k))
            except Exception:
                pass
        return self.get_backoff_state()

    def get_backoff_state(self) -> Dict[str, Any]:
        backoff = getattr(self.model, "backoff", None)
        return {
            "enabled": bool(getattr(backoff, "enabled", False)),
            "token_limit_k": int(getattr(backoff, "token_limit_k", 0) or 0),
        }

    def latest_review_state(self) -> Optional[Dict[str, Any]]:
        return dict(self._latest_review) if isinstance(self._latest_review, dict) else None

    def patch_resolution_state(self) -> Optional[Dict[str, Any]]:
        return dict(self._patch_resolution) if isinstance(self._patch_resolution, dict) else None

    def discovery_remediation_state(self) -> Optional[Dict[str, Any]]:
        return dict(self._discovery_remediation) if isinstance(self._discovery_remediation, dict) else None

    def edit_batch_state(self) -> Dict[str, Any]:
        queued_paths = sorted(self._edit_batch_pending.keys())
        return {
            "active": self._edit_batch_mode,
            "queued_paths": queued_paths,
            "queued_count": len(queued_paths),
            "last_failure": dict(self._edit_batch_last_failure) if isinstance(self._edit_batch_last_failure, dict) else None,
        }

    def _active_mode_strategy(self) -> Optional[Dict[str, Any]]:
        if self._patch_resolution is not None:
            path = str(self._patch_resolution.get("path", "") or "").strip()
            repo_path = f"/repo/{path}" if path else "/repo"
            return {
                "mode": "patch_resolution",
                "steps": [
                    f"s1: cat {repo_path}",
                    f"s2: show-diff {path}" if path else "s2: show-diff",
                    f"s3: review-changes {path} limit=20" if path else "s3: review-changes limit=20",
                    "s1: show-diff",
                    f"s2: review-changes {path} limit=20" if path else "s2: review-changes limit=20",
                    f"s3: cat {repo_path}",
                    "s1: drop",
                ],
                "strategy_blocks": [
                    [
                        f"s1: cat {repo_path}",
                        f"s2: show-diff {path}" if path else "s2: show-diff",
                        f"s3: review-changes {path} limit=20" if path else "s3: review-changes limit=20",
                    ],
                    [
                        "s1: show-diff",
                        f"s2: review-changes {path} limit=20" if path else "s2: review-changes limit=20",
                        f"s3: cat {repo_path}",
                    ],
                    [
                        "s1: drop",
                    ],
                ],
            }
        if self._discovery_remediation is not None:
            path = str(self._discovery_remediation.get("path", "") or "").strip()
            issue_id = str(self._discovery_remediation.get("issue_id", "") or "").strip()
            repo_path = f"/repo/{path}" if path else ""
            focused_block: List[str] = []
            if path:
                focused_block.append(f"s{len(focused_block) + 1}: cat {repo_path}")
            if issue_id:
                focused_block.append(f"s{len(focused_block) + 1}: show-issue {issue_id}")
            if not focused_block:
                focused_block.append("s1: list-issues")
            issue_block: List[str] = ["s1: list-issues"]
            if issue_id:
                issue_block.append(f"s2: show-issue {issue_id}")
            return {
                "mode": "discovery_remediation",
                "steps": [line for block in [focused_block, issue_block] for line in block],
                "strategy_blocks": [focused_block, issue_block],
            }
        if self._edit_batch_mode or self._edit_batch_pending:
            queued_paths = sorted(self._edit_batch_pending.keys())
            summary = ", ".join(queued_paths[:3]) if queued_paths else "the current batch"
            return {
                "mode": "edit_batch",
                "steps": [
                    f"Keep related edits grouped while the batch is active for {summary}.",
                    "End the batch to trigger one host verification pass across queued files.",
                    "Do not finish until the batch exits cleanly and validation state clears.",
                ],
            }
        if self._pending_verification is not None:
            path = str(self._pending_verification.get("path", "") or "").strip()
            repo_path = f"/repo/{path}" if path else "/repo"
            return {
                "mode": "pending_verification",
                "steps": [
                    f"s1: cat {repo_path}",
                    f"s2: show-diff {path}" if path else "s2: show-diff",
                    f"s3: review-changes {path} limit=20" if path else "s3: review-changes limit=20",
                    f"s1: show-diff {path}" if path else "s1: show-diff",
                    f"s2: review-changes {path} limit=20" if path else "s2: review-changes limit=20",
                ],
                "strategy_blocks": [
                    [
                        f"s1: cat {repo_path}",
                        f"s2: show-diff {path}" if path else "s2: show-diff",
                        f"s3: review-changes {path} limit=20" if path else "s3: review-changes limit=20",
                    ],
                    [
                        f"s1: show-diff {path}" if path else "s1: show-diff",
                        f"s2: review-changes {path} limit=20" if path else "s2: review-changes limit=20",
                    ],
                ],
            }
        if self._completion_check_pending:
            return {
                "mode": "completion_check",
                "steps": [
                    "Run one concrete validation action tied to the recent mutation.",
                    "If validation passes, finish immediately.",
                    "If validation contradicts completion, make one corrective edit and let the host reopen normal execution.",
                ],
            }
        return None

    def _format_strategy_blocks(self, strategy: Dict[str, Any]) -> str:
        blocks = strategy.get("strategy_blocks")
        if not isinstance(blocks, list):
            steps = [str(item) for item in strategy.get("steps", []) if str(item).strip()]
            return "\n".join(steps)

        rendered: List[str] = []
        for index, block in enumerate(blocks, start=1):
            lines = [str(item) for item in block if str(item).strip()] if isinstance(block, list) else []
            if not lines:
                continue
            rendered.append(f"Executable option {index}; emit only this block if it matches:")
            rendered.extend(lines)
        return "\n".join(rendered)

    def _bridge_safe_action_types(self) -> set[str]:
        return {
            "approve_npm_command",
            "close_active_issue",
            "drop_context",
            "finish",
            "git_diff",
            "list_issues",
            "read_file",
            "reject_npm_command",
            "review_changes",
            "show_issue",
            "show_diff",
        }

    def export_runtime_state(self) -> Dict[str, Any]:
        return {
            "runtime_config": {
                "provider": self.provider,
                "model": self.model_name,
                "thinking_mode": self.thinking_mode,
                "verbosity": self.verbosity,
            },
            "runtime_capabilities": {
                "ordered_mutation_batches": True,
                "max_mutation_commands_per_turn": 4,
                "host_enforced_edit_batch": True,
                "discovery_remediation": True,
                "deterministic_pending_verification": True,
                "goal_start_skill_mode": True,
                "approved_npm_commands": True,
            },
            "current_task": self._current_task,
            "task_satisfied": self._task_satisfied,
            "completion_check_pending": self._completion_check_pending,
            "completion_check_reason": self._completion_check_reason,
            "goal_start_skill_mode": {
                "pending": self._goal_skill_mode_pending,
                "used": self._goal_skill_mode_used,
            },
            "llm_activity": dict(self._llm_activity),
            "patch_resolution": self.patch_resolution_state(),
            "discovery_remediation": self.discovery_remediation_state(),
            "pending_npm_command": dict(self._pending_npm_command) if isinstance(self._pending_npm_command, dict) else None,
            "active_mode_strategy": self._active_mode_strategy(),
            "pending_verification": dict(self._pending_verification) if isinstance(self._pending_verification, dict) else None,
            "edit_batch": self.edit_batch_state(),
            "latest_review": self.latest_review_state(),
            "repo_facts_status_lines": self.repo_facts_status_lines(),
            "available_skills": self._available_skills_payload(),
            "suggested_next_actions": self.runtime_suggested_next_actions(),
            "issue_state": self.issue_ledger.planner_payload(path=str(self._repo_facts_path())),
        }

    def execute_operator_action(self, action: Dict[str, Any], *, thought: str = "Operator action from extension UI.") -> ActionResult:
        if not isinstance(action, dict):
            return ActionResult(ok=False, name="operator_action", payload={"error": "Action must be an object."})

        action_type = str(action.get("type", "") or "").strip()
        if not action_type:
            return ActionResult(ok=False, name="operator_action", payload={"error": "Missing action.type"})
        if action_type not in self._bridge_safe_action_types():
            return ActionResult(ok=False, name=action_type, payload={"error": f"Unsupported operator action: {action_type}"})

        if action_type == "drop_context":
            message = self._exec_drop_context(action)
            return ActionResult(ok=True, name=action_type, payload={"message": message})

        if action_type == "read_file":
            path = str(action.get("path", "") or "").strip()
            if not path:
                return ActionResult(ok=False, name=action_type, payload={"error": "read_file requires path"})
            try:
                target = self._resolve_repo_path(path)
                content = target.read_text(encoding="utf-8")
            except Exception as exc:
                return ActionResult(ok=False, name=action_type, payload={"error": str(exc), "path": path})
            self._mark_path_validated(path)
            self._maybe_resolve_patch_resolution(action_type, path)
            self._maybe_resolve_discovery_remediation(action_type, path)
            return ActionResult(
                ok=True,
                name=action_type,
                payload={
                    "message": f"Read {path}.",
                    "path": path,
                    "content": content,
                    "line_count": len(content.splitlines()),
                },
            )

        if action_type == "list_issues":
            issues = self.loop.bridge.tree.list_log_issues()
            durable_state = self.issue_ledger.planner_payload(path=str(self._repo_facts_path()))
            summary_parts: List[str] = []
            if issues:
                summary_parts.append(self.loop.bridge.tree.format_log_issue_list(issues))
            if issue_summaries_from_payload(durable_state):
                summary_parts.append(format_issue_summary_list(durable_state))
            if not summary_parts:
                summary_parts.append("(no parsed log issues and no durable repo_facts issues)")
            summary = "\n\n".join(summary_parts)
            return ActionResult(
                ok=True,
                name=action_type,
                payload={
                    "message": f"Listed {len(issues)} parsed issue(s) and {len(issue_summaries_from_payload(durable_state))} durable issue(s).",
                    "summary": summary,
                    "issues": issues,
                    "durable_issues": issue_summaries_from_payload(durable_state),
                },
            )

        if action_type == "show_issue":
            issue_id = str(action.get("issue_id", "") or action.get("id", "") or "").strip()
            if not issue_id:
                return ActionResult(ok=False, name=action_type, payload={"error": "show_issue requires issue_id"})
            issue = self.loop.bridge.tree.show_log_issue(issue_id)
            durable_issue = self.issue_ledger.get_issue(issue_id)
            durable_state = self.issue_ledger.planner_payload(path=str(self._repo_facts_path()))
            found = issue is not None or durable_issue is not None
            if found:
                self._maybe_resolve_discovery_remediation(action_type, issue_id=issue_id)
            else:
                remediation_issue_id = str((self._discovery_remediation or {}).get("issue_id", "") or "").strip()
                if remediation_issue_id and remediation_issue_id == issue_id:
                    self._discovery_remediation = None
                    self._refresh_loop_steering()
            if issue is not None:
                summary = self.loop.bridge.tree.format_log_issue_detail(issue)
            elif durable_issue is not None:
                summary = format_issue_record_detail(durable_issue)
            else:
                summary = format_issue_not_found(issue_id, durable_state)
            return ActionResult(
                ok=True,
                name=action_type,
                payload={
                    "message": f"Loaded {issue_id}." if found else f"Issue not found: {issue_id}; returned available durable issues for recovery.",
                    "summary": summary,
                    "issue": issue,
                    "durable_issue": durable_issue.summary() if durable_issue is not None else None,
                    "next_reads": self.loop.bridge.tree.log_issue_read_commands(issue) if issue else [],
                    "issue_id": issue_id,
                    "available_issue_ids": [
                        str(item.get("issue_id", "") or "")
                        for item in issue_summaries_from_payload(durable_state)
                        if str(item.get("issue_id", "") or "").strip()
                    ][:20],
                },
            )

        if action_type == "close_active_issue":
            issue = self.close_active_issue(note="Closed manually from the VS Code Issues panel.")
            if issue is None:
                return ActionResult(ok=False, name=action_type, payload={"error": "No active issue to close."})
            issue_id = str(issue.get("issue_id", "") or "").strip()
            return ActionResult(
                ok=True,
                name=action_type,
                payload={
                    "message": f"Closed {issue_id}." if issue_id else "Closed active issue.",
                    "issue": issue,
                    "issue_id": issue_id,
                },
            )

        if action_type in {"approve_npm_command", "reject_npm_command"}:
            executor = getattr(self, f"_exec_{action_type}", None)
            if not callable(executor):
                return ActionResult(ok=False, name=action_type, payload={"error": f"Unsupported operator action: {action_type}"})
            message = str(executor(action) or "")
            ok = not self._message_indicates_failure(action_type, message)
            payload: Dict[str, Any] = {"message": message}
            if not ok:
                payload["error"] = message or f"{action_type} failed"
            return ActionResult(ok=ok, name=action_type, payload=payload)

        if action_type == "finish":
            command_result = CommandResult(
                ok=True,
                output="",
                command_type="finish",
                needs_tool=True,
                tool_action={"type": "finish", "message": str(action.get("message", "Done."))},
            )
            message = str(self._dispatch_tool_action(command_result) or str(action.get("message", "Done.")))
            payload = {"message": message or str(action.get("message", "Done."))}
            if not command_result.ok:
                payload["error"] = message or "finish blocked"
            return ActionResult(ok=command_result.ok, name=action_type, payload=payload)

        executor = getattr(self, f"_exec_{action_type}", None)
        if not callable(executor):
            return ActionResult(ok=False, name=action_type, payload={"error": f"Unsupported operator action: {action_type}"})
        message = str(executor(action) or "")
        ok = not self._message_indicates_failure(action_type, message)
        if ok:
            self._maybe_resolve_patch_resolution(action_type, str(action.get("path", "") or "").strip())
        payload: Dict[str, Any] = {
            "message": message or self._format_runtime_action_label(action),
            "path": str(action.get("path", "") or "").strip(),
        }
        latest_review = self.latest_review_state()
        if latest_review is not None and action_type in {"git_diff", "show_diff", "review_changes"}:
            payload["summary"] = str(latest_review.get("summary", "") or "")
            payload["latest_review"] = latest_review
        if not ok:
            payload["error"] = message or f"{action_type} failed"
        return ActionResult(ok=ok, name=action_type, payload=payload)

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

    def ensure_issue_for_plan(self, *, original_request: str, plan_summary: str, reuse_issue_id: str = "") -> Dict[str, Any]:
        issue = self.issue_ledger.ensure_issue_open(
            request_summary=str(original_request or "").strip(),
            plan_summary=str(plan_summary or "").strip(),
            reuse_issue_id=str(reuse_issue_id or self.active_issue_id).strip(),
        )
        self.active_issue_id = issue.issue_id
        self._persist_repo_facts()
        self._sync_fact_map()
        return issue.summary()

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
        duplicate = self.issue_ledger.find_duplicate_issue(
            request_summary=request_summary,
            source=source,
            parent_issue_id=parent_issue_id,
        )
        issue = duplicate or self.issue_ledger.create_issue(
            request_summary=request_summary,
            plan_summary=plan_summary,
            source=source,
            parent_issue_id=parent_issue_id,
            source_excerpt=source_excerpt,
            priority=priority,
            activate=activate,
        )
        if activate:
            issue.status = "open"
            issue.closed_at = ""
            self.issue_ledger.active_issue_id = issue.issue_id
            self.active_issue_id = issue.issue_id
        self._persist_repo_facts()
        self._sync_fact_map()
        return issue.summary()

    def close_active_issue(self, *, note: str = "") -> Optional[Dict[str, Any]]:
        issue = self.issue_ledger.close_active_issue(note=note)
        self.active_issue_id = ""
        self._persist_repo_facts()
        self._sync_fact_map()
        return issue.summary() if issue is not None else None

    def close_issue(self, issue_id: str, *, note: str = "") -> Dict[str, Any]:
        issue = self.issue_ledger.close_issue(str(issue_id or "").strip(), note=note)
        if self.active_issue_id == issue.issue_id:
            self.active_issue_id = ""
        self._persist_repo_facts()
        self._sync_fact_map()
        return issue.summary()

    def reopen_issue(self, issue_id: str) -> Dict[str, Any]:
        issue = self.issue_ledger.reopen_issue(str(issue_id or "").strip())
        self.active_issue_id = issue.issue_id
        self._persist_repo_facts()
        self._sync_fact_map()
        self._task_satisfied = False
        self._completion_check_pending = False
        self._completion_check_reason = ""
        self._pending_verification = None
        self._patch_resolution = None
        self._edit_batch_mode = False
        self._edit_batch_pending = {}
        self._edit_batch_last_failure = None
        return issue.summary()

    def run_task(self, task: str) -> WorkerRunResult:
        self._current_task = str(task or "").strip()
        self._reset_guard_state(clear_budget_usage=False)
        self._run_sequence += 1
        self._active_run_id = self._run_sequence
        self._bridge_step_counter = 0
        self._reset_run_observability(self._current_task)
        self._refresh_loop_steering()
        loop_result = self.loop.run(self._current_task)
        self.history = self.loop.history
        touched_paths = self._collect_touched_paths(self.loop.history)
        validation = self._finalize_validation(loop_result)
        task_satisfied = bool(loop_result.finished and validation.passed)
        self._task_satisfied = task_satisfied
        final_message = str(loop_result.finish_message or loop_result.summary())
        if loop_result.finished and not validation.passed:
            final_message = f"{final_message} Validation required: {validation.summary}"
        self._flush_observability(final_message)
        return WorkerRunResult(
            ok=bool(loop_result.finished and task_satisfied),
            final_message=final_message,
            task_satisfied=task_satisfied,
            validation_ran=validation.kind not in {"none", "missing"},
            validation_passed=validation.passed,
            touched_paths=touched_paths,
            validation=validation,
        )

    def _command_progress_payload(self, command: str, result: CommandResult) -> Optional[Dict[str, Any]]:
        if result.command_type == "annotation":
            return None

        command_preview = str(command or "").strip()
        action = result.tool_action if isinstance(result.tool_action, dict) else {}
        action_type = str(action.get("type", "") or "").strip()
        if not action_type:
            parts = shlex.split(command_preview) if command_preview else []
            action_type = str(parts[0]).strip().lower().replace("-", "_") if parts else (result.command_type or "step")

        path_value = action.get("path", "")
        if isinstance(path_value, list):
            path = str(path_value[0] or "").strip() if path_value else ""
        else:
            path = str(path_value or "").strip()

        if not path and command_preview:
            parts = shlex.split(command_preview)
            if len(parts) >= 2 and parts[0].lower() not in {"finish", "skill", "git", "shell", "batch"}:
                candidate = str(parts[1] or "").strip().removeprefix("/repo/").removeprefix("repo/")
                path = candidate.split(":", 1)[0]

        skill_name = ""
        skill_mode = ""
        skill_count = 0
        summary = str(result.output or "").strip()
        if action_type == "skill":
            parts = shlex.split(command_preview) if command_preview else []
            if len(parts) >= 2:
                skill_name = str(parts[1] or "").strip()
            for token in parts[2:]:
                if token.startswith("mode="):
                    skill_mode = token.split("=", 1)[1].strip()
                    break
            if skill_name:
                summary = f"Loaded skill {skill_name}" + (f" (mode={skill_mode})" if skill_mode else ".")
            else:
                skill_count = len(self._available_skills_payload())
                if skill_count > 0:
                    summary = f"Listed {skill_count} available skill(s)."

        self._bridge_step_counter += 1
        return {
            "step": self._bridge_step_counter,
            "action_type": action_type or "step",
            "path": path,
            "ok": bool(result.ok),
            "elapsed_s": 0,
            "thought": "",
            "summary": summary[:200] if summary else "",
            "skill_name": skill_name,
            "skill_mode": skill_mode,
            "skill_count": skill_count,
            "diff": "",
            "replacements": 0,
            "added_lines": 0,
            "removed_lines": 0,
            "search_excerpt": "",
            "replace_excerpt": "",
            "inspected_file_count": 0,
            "inspected_files": [],
        }

    def _emit_step_progress(self, command: str, result: CommandResult) -> None:
        self._append_observability_command(command, result)
        if not callable(self.on_step_callback):
            return
        payload = self._command_progress_payload(command, result)
        if payload is None:
            return
        try:
            self.on_step_callback(payload)
        except Exception:
            pass

    def _emit_model_progress(self, event: str, *, turn: int = 0, elapsed_s: float = 0.0, output_chars: int = 0, error: str = "") -> None:
        if not callable(self.on_step_callback):
            return
        self._bridge_step_counter += 1
        ok = True
        if event == "model_call_start":
            summary = f"Waiting on {self.provider}/{self.model_name} for turn {turn}."
        elif event == "model_call_finish":
            summary = f"Model responded on turn {turn} in {elapsed_s:.2f}s ({output_chars} chars)."
        elif event == "model_call_interrupted":
            ok = False
            summary = f"Model call interrupted on turn {turn}."
        elif event == "model_call_error":
            ok = False
            summary = f"Model call failed on turn {turn}: {error or 'unknown error'}"
        else:
            summary = event.replace("_", " ")
        try:
            self.on_step_callback(
                {
                    "step": self._bridge_step_counter,
                    "action_type": event,
                    "path": "",
                    "ok": ok,
                    "elapsed_s": round(float(elapsed_s or 0.0), 3),
                    "thought": "",
                    "summary": summary[:200],
                    "skill_name": "",
                    "skill_mode": "",
                    "skill_count": 0,
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
        except Exception:
            pass

    def _observe_model_event(self, payload: Dict[str, Any]) -> None:
        event = str((payload or {}).get("event", "") or "").strip()
        if not event:
            return
        turn = int((payload or {}).get("turn", 0) or 0)
        elapsed_s = float((payload or {}).get("elapsed_s", 0.0) or 0.0)
        output_chars = int((payload or {}).get("output_chars", 0) or 0)
        error = str((payload or {}).get("error", "") or "").strip()
        self._llm_activity = {
            "in_flight": event == "model_call_start",
            "turn": turn,
            "last_event": event,
            "elapsed_s": round(elapsed_s, 3),
            "output_chars": output_chars,
            "error": error,
        }
        self._emit_model_progress(event, turn=turn, elapsed_s=elapsed_s, output_chars=output_chars, error=error)

    def _compose_steering(self) -> str:
        parts: List[str] = []
        if self.steering_prompt:
            parts.append(self.steering_prompt)
        if self._goal_skill_mode_pending and not self._goal_skill_mode_used:
            parts.append(
                "GOAL-START SKILL MODE: Before normal repo work, discover and load relevant Playground OS skills once, if any would improve this goal. "
                "Prefer starting with `skill` to inspect the catalog before any direct `skill <name>` load, then load the best matches. "
                "Do this exactly once per planner goal, especially when the task hints at testing, style, diagnostics, repair playbooks, or named skills, then move on to execution."
            )
        if self.goal_fact_keys:
            parts.append("Prioritize these repo fact keys when relevant: " + ", ".join(self.goal_fact_keys))
        if self.discovery_budget is not None:
            parts.append(
                "\n".join(
                    [
                        "Planner-controlled discovery phase.",
                        f"Discovery mode: {self.discovery_budget.mode_label}.",
                        f"Discovery budget: at most {self.discovery_budget.max_tool_calls} tool-backed actions.",
                        "Do not modify files during discovery. Finish with a concise discovery summary.",
                        "Record useful findings with executable fact commands, for example: `fact demo/goal/entrypoint planner.py owns discovery mode`.",
                        "Fact commands are allowed after the tool-call budget is exhausted, but they must include a non-empty key and value.",
                    ]
                )
            )
        if self._patch_resolution is not None:
            path = str(self._patch_resolution.get("path", "") or "").strip()
            strategy = self._active_mode_strategy() or {}
            parts.append(
                f"Patch resolution mode is active for {path or 'the failed edit'}. "
                "Do not keep editing. First resolve the failed patch with read_file, git_diff, show_diff, review_changes, or drop_context."
            )
            strategy_text = self._format_strategy_blocks(strategy)
            if strategy_text:
                parts.append(
                    "Choose the recovery strategy path that best matches what just failed. "
                    "Do not mechanically run every option. Do not return numbered prose steps. "
                    "After >>th: and >>pl:, emit exactly one executable strategy block such as:\n" + strategy_text
                )
            parts.append("If inspection shows the branch is wrong, emit `drop` on the next turn instead of another edit.")
        elif self._discovery_remediation is not None:
            path = str(self._discovery_remediation.get("path", "") or "").strip()
            issue_id = str(self._discovery_remediation.get("issue_id", "") or "").strip()
            focus_target = path or issue_id or "the surfaced issue"
            strategy = self._active_mode_strategy() or {}
            parts.append(
                f"Discovery remediation mode is active for {focus_target}. "
                "Do not mutate or finish until you inspect the focused file or issue record."
            )
            strategy_text = self._format_strategy_blocks(strategy)
            if strategy_text:
                parts.append(
                    "Choose the remediation strategy path that matches the surfaced issue. "
                    "Do not return numbered prose steps. After >>th: and >>pl:, emit exactly one executable strategy block such as:\n" + strategy_text
                )
            parts.append("After inspection clears remediation focus, make one targeted fix on the next turn and rerun the concrete check that surfaced the issue.")
        elif self._edit_batch_mode or self._edit_batch_pending:
            queued_paths = sorted(self._edit_batch_pending.keys())
            parts.append(
                f"Host-managed edit batch is active. Queued files: {', '.join(queued_paths[:4]) or 'none yet'}. "
                "Keep related writes grouped, end the batch to trigger host verification, and only finish after the batch closes cleanly."
            )
        elif self._pending_verification is not None:
            path = str(self._pending_verification.get("path", "") or "").strip()
            strategy = self._active_mode_strategy() or {}
            parts.append(
                f"Pending verification is active for {path or 'the latest mutation'}. "
                "Confirm the landed change before any final correction or finish."
            )
            strategy_text = self._format_strategy_blocks(strategy)
            if strategy_text:
                parts.append(
                    "Choose the verification strategy path that best fits the current diff surface. "
                    "Do not return numbered prose steps. After >>th: and >>pl:, emit exactly one executable strategy block such as:\n" + strategy_text
                )
        if self._completion_check_pending and self._completion_check_reason:
            parts.append(self._completion_check_reason)
            parts.append(
                "COMPLETION CHECK FORMAT: Do not return a numbered prose plan. After >>th: and >>pl:, emit one executable validation command such as `run-check typecheck`, `show-diff`, or `review-changes`; use `finish <summary>` only after validation passes."
            )
        return "\n".join(part for part in parts if part)

    def _refresh_loop_steering(self) -> None:
        if hasattr(self.loop, "_base_steering"):
            self.loop._base_steering = self._compose_steering()

    def _observability_path(self) -> Path:
        return Path(__file__).parent / "memory_observability.md"

    def _reset_run_observability(self, task: str) -> None:
        self._observability_started_at = time.time()
        self._observability_buffer = []
        self._observability_metrics = {
            "task": str(task or ""),
            "provider": self.provider,
            "model": self.model_name,
            "root": str(self.root),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._observability_started_at)),
            "steps": 0,
            "successful_actions": 0,
            "failed_actions": 0,
        }
        self._write_observability_snapshot(final_message="Run in progress.", finished=False)

    def _append_observability_block(self, block: str) -> None:
        self._observability_buffer.append(block)
        self._write_observability_snapshot(final_message="Run in progress.", finished=False)

    def _append_observability_command(self, command: str, result: CommandResult) -> None:
        if not self._observability_metrics:
            return
        self._observability_metrics["steps"] = int(self._observability_metrics.get("steps", 0)) + 1
        key = "successful_actions" if result.ok else "failed_actions"
        self._observability_metrics[key] = int(self._observability_metrics.get(key, 0)) + 1
        block = "".join(
            [
                f"## {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())} - beta command\n\n",
                "**Command**\n```text\n",
                str(command or "").strip(),
                "\n```\n\n",
                "**Result Meta**\n```json\n",
                json.dumps(
                    {
                        "step": int(self._observability_metrics.get("steps", 0)),
                        "command": str(command or "").strip(),
                        "command_type": str(result.command_type or ""),
                        "ok": bool(result.ok),
                        "needs_tool": bool(result.needs_tool),
                        "tool_action": result.tool_action,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                "\n```\n\n",
                "**Output**\n```text\n",
                str(result.output or ""),
                "\n```\n\n---\n\n",
            ]
        )
        self._append_observability_block(block)

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

    def _render_observability_report(self, final_message: str, *, finished: bool) -> str:
        metrics = dict(self._observability_metrics) if isinstance(self._observability_metrics, dict) else {}
        now = time.time()
        if finished:
            metrics["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        metrics["duration_s"] = round(max(0.0, now - self._observability_started_at), 3)
        metrics["final_message"] = final_message
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

    def _write_observability_snapshot(self, *, final_message: str, finished: bool) -> None:
        if not self._observability_metrics:
            return
        try:
            target = self._observability_path()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(self._render_observability_report(final_message, finished=finished), encoding="utf-8")
        except Exception:
            return

    def _flush_observability(self, final_message: str) -> None:
        self._write_observability_snapshot(final_message=final_message, finished=True)

    def _status_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "task_satisfied": self._task_satisfied,
            "completion_check_pending": self._completion_check_pending,
            "completion_check_reason": self._completion_check_reason,
            "patch_resolution": self.patch_resolution_state(),
            "discovery_remediation": self.discovery_remediation_state(),
            "active_mode_strategy": self._active_mode_strategy(),
            "pending_verification": dict(self._pending_verification) if isinstance(self._pending_verification, dict) else None,
            "edit_batch": self.edit_batch_state(),
            "runtime_capabilities": {
                "ordered_mutation_batches": True,
                "max_mutation_commands_per_turn": 4,
                "host_enforced_edit_batch": True,
                "discovery_remediation": True,
                "deterministic_pending_verification": True,
                "goal_start_skill_mode": True,
            },
            "goal_start_skill_mode": {
                "pending": self._goal_skill_mode_pending,
                "used": self._goal_skill_mode_used,
            },
            "llm_activity": dict(self._llm_activity),
            "selected_goal_fact_keys": list(self.goal_fact_keys),
            "repo_facts_status_lines": self.repo_facts_status_lines(),
            "available_skills": self._available_skills_payload(),
            "suggested_next_actions": self.runtime_suggested_next_actions(),
        }
        if self._latest_review is not None:
            payload["latest_review"] = dict(self._latest_review)
        if self.discovery_budget is not None:
            payload["discovery_budget"] = {
                "mode": self.discovery_budget.mode_label,
                "tool_calls_used": self.discovery_budget.tool_calls_used,
                "tool_calls_max": self.discovery_budget.max_tool_calls,
                "tool_calls_remaining": self.discovery_budget.remaining_tool_calls,
                "budget_exhausted": self.discovery_budget.exhausted,
            }
        return payload

    def _available_skills_payload(self) -> List[Dict[str, Any]]:
        skills = self.loop.bridge.tree.list_skills_payload()
        return sorted(skills, key=lambda item: (-int(item.get("priority", 0)), str(item.get("name", ""))))

    def _reset_guard_state(self, *, clear_budget_usage: bool = False) -> None:
        self._task_satisfied = False
        self._completion_check_pending = False
        self._completion_check_reason = ""
        self._pending_verification = None
        self._patch_resolution = None
        self._discovery_remediation = None
        self._pending_npm_command = None
        self._edit_batch_mode = False
        self._edit_batch_pending = {}
        self._edit_batch_last_failure = None
        self._has_mutation = False
        self._validation_after_mutation = False
        self._last_validation = WorkerValidationResult(kind="none", passed=True, summary="No mutating actions required validation.")
        self._latest_review = None
        self._llm_activity = {
            "in_flight": False,
            "turn": 0,
            "last_event": "idle",
            "elapsed_s": 0.0,
            "output_chars": 0,
            "error": "",
        }
        if hasattr(self.loop, "_same_turn_halt_reason"):
            self.loop._same_turn_halt_reason = ""
        if clear_budget_usage and self.discovery_budget is not None:
            self.discovery_budget.tool_calls_used = 0

    def _patch_resolution_suggestions(self) -> List[Dict[str, Any]]:
        path = str((self._patch_resolution or {}).get("path", "") or "").strip()
        suggestions: List[Dict[str, Any]] = []
        if path:
            suggestions.append({"type": "read_file", "path": path})
            suggestions.append({"type": "git_diff", "path": path})
            suggestions.append({"type": "show_diff", "path": path})
            suggestions.append({"type": "review_changes", "path": path, "limit": 20})
        suggestions.append({"type": "show_diff"})
        suggestions.append({"type": "drop_context", "reason": f"Reset patch resolution for {path}" if path else "Reset patch resolution"})

        unique: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in suggestions:
            key = str(sorted(item.items()))
            if key in seen:
                continue
            seen.add(key)
            unique.append(self._decorate_runtime_action(item))
        return unique[:6]

    def _npm_command_suggestions(self) -> List[Dict[str, Any]]:
        pending = dict(self._pending_npm_command) if isinstance(self._pending_npm_command, dict) else {}
        command = str(pending.get("command", "") or "").strip()
        prompt = str(pending.get("confirmation_prompt", "") or "").strip()
        suggestions = [
            {
                "type": "approve_npm_command",
                "label": f"Approve {command}" if command else "Approve NPM Command",
                "style": "primary",
                "requires_confirmation": True,
                "confirmation_prompt": prompt or "Approve the pending npm command?",
            },
            {
                "type": "reject_npm_command",
                "label": "Reject NPM Command",
                "style": "ghost",
            },
        ]
        return [self._decorate_runtime_action(item) for item in suggestions][:6]

    def _queue_npm_command_approval(self, action: Dict[str, Any]) -> str:
        command = str(action.get("command", "") or "").strip()
        if not command:
            return "npm_command: missing command"
        try:
            argv, manager = self.loop._normalize_npm_command(command)
        except ValueError as exc:
            return str(exc)
        self._pending_npm_command = {
            "command": command,
            "argv": list(argv),
            "package_manager": manager,
            "path": str(action.get("path", "package.json") or "package.json").strip() or "package.json",
            "confirmation_prompt": f"Allow {manager} command `{ ' '.join(argv) }` in this workspace?",
        }
        self._refresh_loop_steering()
        self._request_same_turn_halt(f"npm command approval required for {' '.join(argv)}")
        return (
            f"npm_command: approval required before running {' '.join(argv)}\n"
            "Use Approve NPM Command to continue or Reject NPM Command to cancel."
        )

    def _enter_patch_resolution(self, *, action_type: str, path: str, reason: str) -> None:
        normalized_path = str(path or "").strip().removeprefix("/repo/").removeprefix("repo/")
        if not normalized_path:
            return
        self._patch_resolution = {
            "task": self._current_task,
            "path": normalized_path,
            "failed_action_type": str(action_type or "patch_file"),
            "reason": str(reason or "").strip(),
            "next_required_action": {"type": "read_file", "path": normalized_path},
        }
        self._discovery_remediation = None
        self._task_satisfied = False
        self._completion_check_pending = False
        self._completion_check_reason = ""
        self._refresh_loop_steering()

    def _is_patch_resolution_active(self) -> bool:
        if not isinstance(self._patch_resolution, dict):
            return False
        task = str(self._patch_resolution.get("task", "") or "")
        return not task or task == self._current_task

    def _patch_resolution_blocks_action(self, action_type: str) -> Optional[str]:
        if not self._is_patch_resolution_active():
            return None

        if action_type in {"drop_context", "read_file", "git_diff", "show_diff", "review_changes"}:
            return None

        path = str((self._patch_resolution or {}).get("path", "") or "").strip() or "the affected file"
        return (
            f"patch resolution active: resolve the failed edit on {path} with read_file, git_diff, show_diff, "
            "review_changes, or drop_context before continuing"
        )

    def _maybe_resolve_patch_resolution(self, action_type: str, path: str = "") -> bool:
        if not self._is_patch_resolution_active():
            return False

        resolution_path = str((self._patch_resolution or {}).get("path", "") or "").strip()
        normalized_path = str(path or "").strip().removeprefix("/repo/").removeprefix("repo/")
        resolves = False
        if action_type == "read_file":
            resolves = bool(normalized_path and normalized_path == resolution_path)
        elif action_type == "git_diff":
            resolves = not normalized_path or normalized_path == resolution_path
        elif action_type in {"show_diff", "review_changes"}:
            resolves = not normalized_path or normalized_path == resolution_path

        if not resolves:
            return False

        self._patch_resolution = None
        self._refresh_loop_steering()
        return True

    def _is_discovery_remediation_active(self) -> bool:
        if not isinstance(self._discovery_remediation, dict):
            return False
        task = str(self._discovery_remediation.get("task", "") or "")
        return not task or task == self._current_task

    def _enter_discovery_remediation(self, *, source_action: str) -> None:
        issues = [
            issue
            for issue in self.loop.bridge.tree.list_log_issues()
            if str(issue.get("status", "open")) != "resolved"
        ]
        if not issues:
            return
        focus = issues[0]
        path = str(focus.get("file", "") or "").strip().removeprefix("/repo/").removeprefix("repo/")
        issue_id = str(focus.get("id", "") or "").strip()
        next_required_action: Dict[str, Any]
        if path:
            next_required_action = {"type": "read_file", "path": path}
        else:
            next_required_action = {"type": "show_issue", "issue_id": issue_id}
        self._discovery_remediation = {
            "task": self._current_task,
            "source_action_type": str(source_action or "").strip(),
            "issue_id": issue_id,
            "path": path,
            "classification": str(focus.get("classification", "") or "").strip(),
            "summary": str(focus.get("summary", "") or focus.get("message", "") or "").strip(),
            "next_required_action": next_required_action,
        }
        self._patch_resolution = None
        self._task_satisfied = False
        self._completion_check_pending = False
        self._completion_check_reason = ""
        self._refresh_loop_steering()

    def _discovery_remediation_blocks_action(self, action_type: str) -> Optional[str]:
        if not self._is_discovery_remediation_active():
            return None
        if action_type in {"drop_context", "git_diff", "show_diff", "review_changes", "run_check", "run_route_check"}:
            return None
        target = str((self._discovery_remediation or {}).get("path", "") or (self._discovery_remediation or {}).get("issue_id", "") or "the surfaced issue")
        return (
            f"discovery remediation active: inspect {target} with read_file or show_issue before mutating or finishing"
        )

    def _maybe_resolve_discovery_remediation(self, action_type: str, path: str = "", issue_id: str = "") -> bool:
        if not self._is_discovery_remediation_active():
            return False

        resolution_path = str((self._discovery_remediation or {}).get("path", "") or "").strip()
        resolution_issue_id = str((self._discovery_remediation or {}).get("issue_id", "") or "").strip()
        normalized_path = str(path or "").strip().removeprefix("/repo/").removeprefix("repo/")
        resolved = False
        if action_type == "read_file":
            resolved = bool(normalized_path and resolution_path and normalized_path == resolution_path)
        elif action_type == "show_issue":
            resolved = bool(issue_id and resolution_issue_id and issue_id == resolution_issue_id)

        if not resolved:
            return False

        self._discovery_remediation = None
        self._refresh_loop_steering()
        return True

    def _request_same_turn_halt(self, reason: str) -> None:
        if hasattr(self.loop, "_same_turn_halt_reason"):
            self.loop._same_turn_halt_reason = str(reason or "").strip()

    def _current_file_sha256(self, path: str) -> str:
        target = self._resolve_repo_path(path)
        content = target.read_text(encoding="utf-8")
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _build_pending_verification(self, path: str, *, mode: str, source: str) -> Dict[str, Any]:
        normalized_path = str(path or "").strip().removeprefix("/repo/").removeprefix("repo/")
        payload: Dict[str, Any] = {
            "path": normalized_path,
            "mode": mode,
            "source": source,
        }
        if normalized_path:
            try:
                payload["expected_sha256"] = self._current_file_sha256(normalized_path)
            except Exception:
                payload["expected_sha256"] = ""
        return payload

    def _queue_edit_batch_verification(self, path: str) -> None:
        normalized_path = str(path or "").strip().removeprefix("/repo/").removeprefix("repo/")
        if not normalized_path:
            return
        self._edit_batch_pending[normalized_path] = self._build_pending_verification(
            normalized_path,
            mode="batch",
            source="edit_batch",
        )
        self._edit_batch_last_failure = None

    def _verify_and_close_edit_batch(self, *, source: str) -> Tuple[bool, str]:
        queued_paths = sorted(self._edit_batch_pending.keys())
        if not queued_paths:
            self._edit_batch_mode = False
            self._edit_batch_last_failure = None
            self._refresh_loop_steering()
            return True, "edit batch ended; no queued edits required verification"

        failed_paths: List[str] = []
        for path in queued_paths:
            item = self._edit_batch_pending.get(path, {})
            expected_sha = str(item.get("expected_sha256", "") or "")
            try:
                actual_sha = self._current_file_sha256(path)
            except Exception:
                actual_sha = ""
            if expected_sha and actual_sha != expected_sha:
                failed_paths.append(path)

        if failed_paths:
            first_failed = failed_paths[0]
            self._edit_batch_mode = False
            self._edit_batch_last_failure = {
                "source": source,
                "failed_paths": failed_paths,
                "summary": f"Host verification failed for {', '.join(failed_paths)}.",
            }
            self._pending_verification = self._build_pending_verification(
                first_failed,
                mode="read",
                source="edit_batch_failure",
            )
            self._completion_check_pending = True
            self._completion_check_reason = f"Edit batch verification failed for {first_failed}. Inspect the file before continuing."
            self._refresh_loop_steering()
            return False, f"edit batch verification failed for {first_failed}"

        self._edit_batch_mode = False
        self._edit_batch_pending = {}
        self._edit_batch_last_failure = None
        self._pending_verification = None
        self._validation_after_mutation = True
        self._completion_check_pending = True
        self._completion_check_reason = "Host verified the queued edit batch. Finish if the goal is complete or run one more concrete check."
        self._run_post_write_validation()
        self._refresh_loop_steering()
        return True, f"edit batch ended; host verified {len(queued_paths)} file(s)"

    def _command_reported_issue_count(self, result: CommandResult) -> int:
        match = re.search(r"issues=(\d+)", str(result.output or ""))
        if match is not None:
            return int(match.group(1))
        match = re.search(r"(?:ingested|read diagnostics from .* and ingested)\s+(\d+)\s+issue", str(result.output or ""), re.IGNORECASE)
        if match is not None:
            return int(match.group(1))
        return 0

    def _dispatch_tool_action(self, result: CommandResult) -> str:
        action = result.tool_action or {}
        action_type = str(action.get("type", "") or "")
        fact_action = action_type in FACT_ACTION_TYPES

        if self.discovery_budget is not None and result.needs_tool:
            if action_type in {"write_file", "replace_lines", "patch_file", "git_add", "git_restore", "git_commit", "npm_command"}:
                result.ok = False
                return "Discovery mode is read-only. Finish discovery before mutating repository state."
            if self.discovery_budget.exhausted and action_type != "finish" and not fact_action:
                result.ok = False
                return "Discovery tool-call budget exhausted. Use finish to return control to the planner."
            if action_type != "finish" and not fact_action:
                self.discovery_budget.tool_calls_used += 1

        patch_resolution_block = self._patch_resolution_blocks_action(action_type)
        if patch_resolution_block is not None:
            result.ok = False
            self._refresh_loop_steering()
            return patch_resolution_block

        discovery_remediation_block = self._discovery_remediation_blocks_action(action_type)
        if discovery_remediation_block is not None:
            result.ok = False
            self._refresh_loop_steering()
            return discovery_remediation_block

        if action_type == "npm_command":
            result.ok = False
            return self._queue_npm_command_approval(action)

        if action_type == "finish":
            if self._edit_batch_mode:
                result.ok = False
                self._completion_check_pending = True
                self._completion_check_reason = "An edit batch is still open. End the batch before finish."
                self._refresh_loop_steering()
                return "finish blocked: end the active edit batch before finish"
            if self._is_discovery_remediation_active():
                result.ok = False
                self._completion_check_pending = True
                self._completion_check_reason = "Discovery remediation is active. Inspect the focused issue before finish."
                self._refresh_loop_steering()
                return "finish blocked: discovery remediation still active"
            if (self._pending_verification is not None or (self._has_mutation and not self._validation_after_mutation)) and self._run_post_write_validation():
                self._pending_verification = None
                self._validation_after_mutation = True
            if self._pending_verification is not None:
                result.ok = False
                path = str(self._pending_verification.get("path", "") or "")
                self._completion_check_pending = True
                self._completion_check_reason = f"Validation still pending for {path}. Read the edited path or run a concrete validation step before finish."
                self._refresh_loop_steering()
                return f"finish blocked: pending verification for {path}"
            if self._has_mutation and not self._validation_after_mutation:
                result.ok = False
                self._completion_check_pending = True
                self._completion_check_reason = "A mutation landed without a successful validation step. Verify the change before finish."
                self._refresh_loop_steering()
                return "finish blocked: validation required after mutation"
            self._task_satisfied = True
            self._completion_check_pending = False
            self._completion_check_reason = ""
            self._refresh_loop_steering()
            return ""

        executor = getattr(self, f"_exec_{action_type}", None)
        if not callable(executor):
            result.ok = False
            return f"unsupported tool action: {action_type}"

        message = str(executor(action) or "")
        if action_type == "npm_command" and self._message_indicates_failure(action_type, message):
            result.ok = False
        if self._message_indicates_failure(action_type, message):
            result.ok = False
        return message

    def _observe_command_result(self, command: str, result: CommandResult) -> None:
        if result.command_type != "annotation" and self._goal_skill_mode_pending and not self._goal_skill_mode_used:
            self._goal_skill_mode_pending = False
            self._goal_skill_mode_used = True
            self._refresh_loop_steering()

        if not result.ok:
            action = result.tool_action or {}
            action_type = str(action.get("type", "") or "")
            if action_type in {"patch_file", "replace_lines"}:
                path = str(action.get("path", "") or "").strip()
                self._enter_patch_resolution(action_type=action_type, path=path, reason=str(result.output or "").strip())
                self._request_same_turn_halt(str(result.output or "patch resolution required"))
            self._emit_step_progress(command, result)
            return

        action = result.tool_action or {}
        action_type = str(action.get("type", "") or "")
        if action_type in {"begin_edit_batch", "end_edit_batch"}:
            self._refresh_loop_steering()
            self._emit_step_progress(command, result)
            return
        if action_type in {"write_file", "replace_lines", "patch_file", "git_add", "git_restore", "git_commit", "npm_command"}:
            path_value = action.get("path")
            pending_path = ""
            if isinstance(path_value, list):
                pending_path = str(path_value[0] or "") if path_value else ""
            else:
                pending_path = str(path_value or "")
            self._has_mutation = True
            self._validation_after_mutation = False
            self._task_satisfied = False
            self._completion_check_pending = False
            self._completion_check_reason = ""
            if pending_path and self._edit_batch_mode:
                self._queue_edit_batch_verification(pending_path)
                self._pending_verification = None
            elif pending_path:
                self._pending_verification = self._build_pending_verification(
                    pending_path,
                    mode="read",
                    source=action_type,
                )
            else:
                self._pending_verification = {"path": "", "mode": "validation", "source": action_type}
            self._refresh_loop_steering()
            if not self._edit_batch_mode:
                focus_path = pending_path or "the latest mutation"
                self._request_same_turn_halt(f"pending verification active after mutation on {focus_path}")
            self._emit_step_progress(command, result)
            return

        if result.command_type == "read":
            read_path = self._extract_read_path(command)
            shown_issue_id = self._extract_shown_issue_id(command)
            pending_path = str((self._pending_verification or {}).get("path", "") or "")
            if read_path and pending_path and read_path == pending_path:
                self._mark_path_validated(read_path)
            self._maybe_resolve_patch_resolution("read_file", read_path)
            self._maybe_resolve_discovery_remediation("read_file", read_path)
            if shown_issue_id:
                self._maybe_resolve_discovery_remediation("show_issue", issue_id=shown_issue_id)
            self._emit_step_progress(command, result)
            return

        if action_type in {"run_check", "git_diff", "show_diff", "review_changes", "run_shell"} and self._has_mutation:
            self._pending_verification = None
            self._validation_after_mutation = True
            self._completion_check_pending = True
            self._completion_check_reason = "A concrete validation step succeeded after mutation. Finish if the goal is complete."
            self._refresh_loop_steering()

        if action_type in {"git_diff", "show_diff", "review_changes"}:
            self._maybe_resolve_patch_resolution(action_type, str(action.get("path", "") or "").strip())

        if action_type in {"run_check", "run_route_check"} and self._command_reported_issue_count(result) > 0:
            self._enter_discovery_remediation(source_action=action_type)
            focus = str((self._discovery_remediation or {}).get("path", "") or (self._discovery_remediation or {}).get("issue_id", "") or "the surfaced issue")
            self._request_same_turn_halt(f"discovery remediation active for {focus}")
            self._emit_step_progress(command, result)
            return

        if result.command_type == "mutation" and command.strip().startswith(("read-diagnostics", "ingest-log")) and self._command_reported_issue_count(result) > 0:
            self._enter_discovery_remediation(source_action=command.strip().split()[0])
            focus = str((self._discovery_remediation or {}).get("path", "") or (self._discovery_remediation or {}).get("issue_id", "") or "the surfaced issue")
            self._request_same_turn_halt(f"discovery remediation active for {focus}")

        self._emit_step_progress(command, result)

    def _finalize_validation(self, loop_result: Any) -> WorkerValidationResult:
        if self._edit_batch_mode:
            validation = WorkerValidationResult(kind="missing", passed=False, summary="An edit batch is still open and must be ended before completion.")
        elif self._is_discovery_remediation_active():
            target = str((self._discovery_remediation or {}).get("path", "") or (self._discovery_remediation or {}).get("issue_id", "") or "the surfaced issue")
            validation = WorkerValidationResult(kind="missing", passed=False, summary=f"Discovery remediation is still active for {target}.")
        elif not self._has_mutation:
            validation = WorkerValidationResult(kind="none", passed=True, summary="No mutating actions required validation.")
        elif self._validation_after_mutation and self._pending_verification is None:
            validation = WorkerValidationResult(kind="read_or_check", passed=True, summary="A concrete validation step succeeded after mutation.")
        else:
            pending_path = str((self._pending_verification or {}).get("path", "") or "")
            suffix = f" for {pending_path}" if pending_path else ""
            validation = WorkerValidationResult(kind="missing", passed=False, summary=f"Mutation completed without a concrete validation step{suffix}.")
        self._last_validation = validation
        if not loop_result.finished and validation.passed:
            self._task_satisfied = False
        return validation

    def _repo_facts_path(self) -> Path:
        return self.root / REPO_FACTS_FILENAME

    def _persist_repo_facts(self) -> None:
        try:
            self._repo_facts_path().write_text(self.issue_ledger.to_markdown(), encoding="utf-8")
            self._repo_facts_loaded_count = self.issue_ledger.total_fact_count()
        except Exception:
            return

    def _sync_fact_map(self) -> None:
        self.fact_map = {record.key: record for record in self.issue_ledger.active_context_records()}

    def _get_fact_records(self) -> Sequence[IssueFactRecord]:
        return list(self.issue_ledger.active_context_records())

    def _set_fact_record(self, key: str, value: str, *, source_action: str, fact_type: str, issue_id: str = "") -> IssueFactRecord:
        resolved_fact_type = str(fact_type or "").strip().lower()
        if resolved_fact_type not in {FACT_TYPE_GOAL, FACT_TYPE_ARCHITECTURE}:
            resolved_fact_type = FACT_TYPE_GOAL
        record = self.issue_ledger.upsert_fact(
            key=str(key or "").strip(),
            value=str(value or "").strip(),
            fact_type=resolved_fact_type,
            source_action=source_action,
            updated_step=len(self.history) + 1,
            updated_run_id=self._active_run_id,
            issue_id=str(issue_id or "").strip(),
            task_summary=self._current_task or "Ad hoc worker task",
        )
        active_issue = self.issue_ledger.active_issue()
        if active_issue is not None:
            self.active_issue_id = active_issue.issue_id
        self._persist_repo_facts()
        self._sync_fact_map()
        return record

    def _run_toolbelt_command(self, subcommand: str, *args: str) -> Dict[str, Any]:
        tool_script = Path(__file__).with_name("agent_tools.py")
        completed = subprocess.run(
            [sys.executable, str(tool_script), subcommand, "--root", str(self.root), *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        raw = completed.stdout.strip() or completed.stderr.strip()
        if not raw:
            return {"ok": False, "error": {"code": "EMPTY_TOOL_OUTPUT", "message": f"{subcommand} produced no output"}}
        try:
            return extract_first_json_object(raw)
        except Exception:
            return {
                "ok": False,
                "error": {"code": "BAD_TOOL_OUTPUT", "message": raw[:4000]},
            }

    def _run_post_write_validation(self) -> bool:
        touched_paths = self._collect_touched_paths(self.loop.history)
        primary = touched_paths[0] if touched_paths else ""
        review_args = ["--limit", "20"]
        diff_args: List[str] = []
        if primary:
            review_args = ["--path", primary, "--limit", "20"]
            diff_args = ["--path", primary]

        diff_payload = self._run_toolbelt_command("git-diff", *diff_args)
        review_payload = self._run_toolbelt_command("review", *review_args)
        if diff_payload.get("ok") or review_payload.get("ok"):
            review_data = review_payload.get("data") if isinstance(review_payload.get("data"), dict) else {}
            diff_data = diff_payload.get("data") if isinstance(diff_payload.get("data"), dict) else {}
            changed_files = review_data.get("files") if isinstance(review_data, dict) else []
            review_summary = review_data.get("review_summary") if isinstance(review_data, dict) else {}
            changed_count = 0
            if isinstance(review_summary, dict):
                changed_count = int(review_summary.get("changed_file_count", 0) or 0)
            elif isinstance(changed_files, list):
                changed_count = len(changed_files)
            self._latest_review = {
                "action_type": "host_validation",
                "path": primary,
                "diff": str(diff_data.get("diff", "") or "") if isinstance(diff_data, dict) else "",
                "stat": str(diff_data.get("stat", "") or "") if isinstance(diff_data, dict) else "",
                "files": changed_files if isinstance(changed_files, list) else [],
                "review_summary": review_summary if isinstance(review_summary, dict) else {},
                "summary": f"Host validation completed across {changed_count} changed file(s).",
            }
            self._completion_check_pending = True
            self._completion_check_reason = "Host ran automatic post-write validation. Finish if the goal is complete."
            self._refresh_loop_steering()
            return True
        return False

    def _collect_touched_paths(self, turns: Sequence[Turn]) -> List[str]:
        seen: List[str] = []
        for turn in turns:
            for command, result in zip(turn.commands_issued, turn.results):
                path = self._extract_result_path(command, result)
                if path and path not in seen:
                    seen.append(path)
        return seen

    def _extract_result_path(self, command: str, result: CommandResult) -> str:
        action = result.tool_action or {}
        path = action.get("path")
        if isinstance(path, str) and path.strip():
            return path.strip()
        if isinstance(path, list) and path:
            first = str(path[0] or "").strip()
            if first:
                return first
        return self._extract_read_path(command)

    def _extract_read_path(self, command: str) -> str:
        normalized = str(command or "").strip()
        if normalized.startswith("[") and "] " in normalized:
            normalized = normalized.split("] ", 1)[1].strip()
        if normalized.startswith("cat /repo/"):
            target = normalized[len("cat "):].strip()
            path_part = target
            last_segment = target.rsplit("/", 1)[-1]
            if ":" in last_segment:
                path_part = target.rsplit(":", 1)[0]
            return path_part.removeprefix("/repo/").removeprefix("repo/").strip()
        if normalized.startswith("read-line-range /repo/") or normalized.startswith("read_line_range /repo/"):
            parts = normalized.split()
            if len(parts) >= 2:
                return parts[1].removeprefix("/repo/").removeprefix("repo/").strip()
        return ""

    def _extract_shown_issue_id(self, command: str) -> str:
        normalized = str(command or "").strip()
        if normalized.startswith("[") and "] " in normalized:
            normalized = normalized.split("] ", 1)[1].strip()
        if normalized.startswith("show-issue "):
            parts = normalized.split()
            if len(parts) >= 2:
                return str(parts[1] or "").strip()
        return ""

    def _message_indicates_failure(self, action_type: str, message: str) -> bool:
        lowered = str(message or "").strip().lower()
        if not lowered:
            return False
        if action_type == "run_shell":
            return lowered.startswith("run_shell: timed out") or lowered.startswith("run_shell: error executing")
        if action_type == "run_check":
            return lowered.startswith("run_check:")
        if action_type.startswith("git_"):
            return lowered.startswith("git ") and "failed" in lowered
        return lowered.startswith(f"{action_type}:")

    def _resolve_repo_path(self, path: str) -> Path:
        normalized = str(path or "").strip().removeprefix("/repo/").removeprefix("repo/")
        target = (self.root / normalized).resolve()
        if self.root != target and self.root not in target.parents:
            raise ValueError(f"Path escapes workspace: {path}")
        return target

    def _mark_path_validated(self, path: str) -> None:
        normalized = str(path or "").strip().removeprefix("/repo/").removeprefix("repo/")
        pending = self._pending_verification or {}
        pending_path = str(pending.get("path", "") or "")
        expected_sha = str(pending.get("expected_sha256", "") or "")
        if not (normalized and pending_path and normalized == pending_path):
            return
        if expected_sha:
            try:
                actual_sha = self._current_file_sha256(normalized)
            except Exception:
                actual_sha = ""
            if actual_sha != expected_sha:
                self._completion_check_pending = True
                self._completion_check_reason = f"Verification still pending for {normalized}; file contents changed since the last mutation."
                self._refresh_loop_steering()
                return
        self._pending_verification = None
        self._edit_batch_pending.pop(normalized, None)
        if self._edit_batch_last_failure is not None:
            failed_paths = [str(item) for item in list(self._edit_batch_last_failure.get("failed_paths", []) or []) if str(item)]
            remaining = [item for item in failed_paths if item != normalized]
            if remaining:
                self._edit_batch_last_failure["failed_paths"] = remaining
                self._edit_batch_last_failure["summary"] = f"Host verification still requires inspection for {', '.join(remaining)}."
            else:
                self._edit_batch_last_failure = None
        self._validation_after_mutation = True
        self._completion_check_pending = True
        self._completion_check_reason = f"Validation satisfied for {normalized}. Finish if the goal is complete."
        self._refresh_loop_steering()

    def _format_runtime_action_label(self, action: Dict[str, Any]) -> str:
        action_type = str(action.get("type", "") or "action")
        path = str(action.get("path", "") or "").strip()
        if action_type == "read_file":
            return f"Read {path}" if path else "Read File"
        if action_type == "show_issue":
            issue_id = str(action.get("issue_id", "") or action.get("id", "") or "").strip()
            return f"Show Issue {issue_id}" if issue_id else "Show Issue"
        if action_type == "close_active_issue":
            return "Close Active Issue"
        if action_type == "list_issues":
            return "List Issues"
        if action_type == "approve_npm_command":
            return "Approve NPM Command"
        if action_type == "reject_npm_command":
            return "Reject NPM Command"
        if action_type == "git_diff":
            return f"Diff {path}" if path else "Git Diff"
        if action_type == "show_diff":
            return f"Show Diff {path}" if path else "Show Diff"
        if action_type == "review_changes":
            return f"Review Changes {path}" if path else "Review Changes"
        if action_type == "finish":
            return "Finish"
        if action_type == "drop_context":
            return "Drop Context"
        return action_type.replace("_", " ").title()

    def _decorate_runtime_action(self, action: Dict[str, Any]) -> Dict[str, Any]:
        decorated = dict(action)
        if not decorated.get("label"):
            decorated["label"] = self._format_runtime_action_label(decorated)
        if not decorated.get("style"):
            decorated["style"] = "ghost" if decorated.get("type") == "drop_context" else "secondary"
        return decorated

    def _finish_validation_suggestions(self) -> List[Dict[str, Any]]:
        suggestions: List[Dict[str, Any]] = []
        pending_path = str((self._pending_verification or {}).get("path", "") or "")
        if pending_path:
            suggestions.append({"type": "read_file", "path": pending_path})
            suggestions.append({"type": "git_diff", "path": pending_path})
            suggestions.append({"type": "show_diff", "path": pending_path})
            suggestions.append({"type": "review_changes", "path": pending_path, "limit": 20})

        touched_paths = self._collect_touched_paths(self.loop.history)
        if touched_paths:
            first_path = touched_paths[0]
            if not any(item.get("type") == "read_file" and item.get("path") == first_path for item in suggestions):
                suggestions.append({"type": "read_file", "path": first_path})
            if not any(item.get("type") == "git_diff" and item.get("path") == first_path for item in suggestions):
                suggestions.append({"type": "git_diff", "path": first_path})
            if not any(item.get("type") == "show_diff" and item.get("path") == first_path for item in suggestions):
                suggestions.append({"type": "show_diff", "path": first_path})
            if not any(item.get("type") == "review_changes" and item.get("path") == first_path for item in suggestions):
                suggestions.append({"type": "review_changes", "path": first_path, "limit": 20})

        suggestions.append({"type": "show_diff"})
        suggestions.append({"type": "review_changes", "limit": 20})
        suggestions.append({"type": "finish", "message": "Done.", "style": "primary", "label": "Finish"})

        unique: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in suggestions:
            key = str(sorted(item.items()))
            if key in seen:
                continue
            seen.add(key)
            unique.append(self._decorate_runtime_action(item))
        return unique[:6]

    def _discovery_remediation_suggestions(self) -> List[Dict[str, Any]]:
        suggestions: List[Dict[str, Any]] = []
        path = str((self._discovery_remediation or {}).get("path", "") or "").strip()
        issue_id = str((self._discovery_remediation or {}).get("issue_id", "") or "").strip()
        if path:
            suggestions.append({"type": "read_file", "path": path})
            suggestions.append({"type": "show_diff", "path": path})
            suggestions.append({"type": "review_changes", "path": path, "limit": 20})
        if issue_id:
            suggestions.append({"type": "show_issue", "issue_id": issue_id})
        suggestions.append({"type": "list_issues"})
        suggestions.append({"type": "drop_context", "reason": "Reset discovery remediation"})

        unique: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in suggestions:
            key = str(sorted(item.items()))
            if key in seen:
                continue
            seen.add(key)
            unique.append(self._decorate_runtime_action(item))
        return unique[:6]

    def _edit_batch_suggestions(self) -> List[Dict[str, Any]]:
        suggestions: List[Dict[str, Any]] = []
        for path in sorted(self._edit_batch_pending.keys())[:4]:
            suggestions.append({"type": "read_file", "path": path})
            suggestions.append({"type": "show_diff", "path": path})
        if not suggestions:
            last_failure = self._edit_batch_last_failure or {}
            for path in list(last_failure.get("failed_paths", []) or [])[:2]:
                suggestions.append({"type": "read_file", "path": str(path)})
                suggestions.append({"type": "show_diff", "path": str(path)})
        suggestions.append({"type": "drop_context", "reason": "Reset edit batch state"})

        unique: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in suggestions:
            key = str(sorted(item.items()))
            if key in seen:
                continue
            seen.add(key)
            unique.append(self._decorate_runtime_action(item))
        return unique[:6]

    def runtime_suggested_next_actions(self) -> List[Dict[str, Any]]:
        suggestions: List[Dict[str, Any]] = []
        if self._pending_npm_command is not None:
            suggestions.extend(self._npm_command_suggestions())
        elif self._patch_resolution is not None:
            suggestions.extend(self._patch_resolution_suggestions())
        elif self._discovery_remediation is not None:
            suggestions.extend(self._discovery_remediation_suggestions())
        elif self._edit_batch_mode or self._edit_batch_pending or self._edit_batch_last_failure is not None:
            suggestions.extend(self._edit_batch_suggestions())
        elif self._pending_verification or (self._has_mutation and not self._validation_after_mutation):
            suggestions.extend(self._finish_validation_suggestions())
        elif self._latest_review is not None:
            suggestions.append(self._decorate_runtime_action({"type": "finish", "message": "Done.", "style": "primary", "label": "Finish"}))

        unique: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in suggestions:
            key = str(sorted(item.items()))
            if key in seen:
                continue
            seen.add(key)
            unique.append(dict(item))
        return unique[:6]

    def _exec_write_file(self, action: Dict[str, Any]) -> str:
        return self.loop._exec_write_file(action)

    def _exec_replace_lines(self, action: Dict[str, Any]) -> str:
        return self.loop._exec_replace_lines(action)

    def _exec_patch_file(self, action: Dict[str, Any]) -> str:
        return self.loop._exec_patch_file(action)

    def _exec_run_shell(self, action: Dict[str, Any]) -> str:
        return self.loop._exec_run_shell(action)

    def _exec_npm_command(self, action: Dict[str, Any]) -> str:
        return self.loop._exec_npm_command(action)

    def _exec_approve_npm_command(self, action: Dict[str, Any]) -> str:
        pending = dict(self._pending_npm_command) if isinstance(self._pending_npm_command, dict) else {}
        if not pending:
            return "approve_npm_command: no pending npm command"
        self._pending_npm_command = None
        run_action = {
            "type": "npm_command",
            "command": str(pending.get("command", "") or ""),
            "path": str(pending.get("path", "package.json") or "package.json"),
        }
        return self._exec_npm_command(run_action)

    def _exec_reject_npm_command(self, action: Dict[str, Any]) -> str:
        pending = dict(self._pending_npm_command) if isinstance(self._pending_npm_command, dict) else {}
        if not pending:
            return "reject_npm_command: no pending npm command"
        command = str(pending.get("command", "") or "").strip()
        self._pending_npm_command = None
        self._refresh_loop_steering()
        return f"Rejected pending npm command: {command}" if command else "Rejected pending npm command"

    def _exec_diagnose(self, action: Dict[str, Any]) -> str:
        return self.loop._exec_diagnose(action)

    def _exec_run_check(self, action: Dict[str, Any]) -> str:
        return self.loop._exec_run_check(action)

    def _exec_run_route_check(self, action: Dict[str, Any]) -> str:
        return self.loop._exec_run_route_check(action)

    def _exec_begin_edit_batch(self, action: Dict[str, Any]) -> str:
        self._edit_batch_mode = True
        self._edit_batch_pending = {}
        self._edit_batch_last_failure = None
        self._pending_verification = None
        self._completion_check_pending = False
        self._completion_check_reason = ""
        message = self.loop._exec_begin_edit_batch()
        self._refresh_loop_steering()
        return message

    def _exec_end_edit_batch(self, action: Dict[str, Any]) -> str:
        self.loop._exec_end_edit_batch()
        ok, message = self._verify_and_close_edit_batch(source="end_edit_batch")
        if not ok:
            return f"end_edit_batch: {message}"
        return message

    def _exec_set_fact(self, action: Dict[str, Any]) -> str:
        key = str(action.get("key", "") or "").strip()
        value = str(action.get("value", "") or "").strip()
        if not key or not value:
            return "set_fact: missing key or value; emit `fact demo/goal/<key> <value>` with a concise non-empty finding"
        fact_type = str(action.get("fact_type", FACT_TYPE_ARCHITECTURE) or FACT_TYPE_ARCHITECTURE).strip().lower()
        issue_id = str(action.get("issue_id", "") or "").strip()
        record = self._set_fact_record(
            key,
            value,
            source_action="set_fact",
            fact_type=fact_type,
            issue_id=issue_id,
        )
        self.loop.bridge.on_fact_written(record.issue_id, record.fact_type, record.key, record.value)
        return f"fact recorded: {record.key}"

    def _exec_drop_context(self, action: Dict[str, Any]) -> str:
        self._reset_guard_state()
        return "context dropped"

    def _run_git(self, args: List[str]) -> Tuple[int, str, str]:
        completed = subprocess.run(
            ["git", *args],
            cwd=str(self.root),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return completed.returncode, completed.stdout.strip(), completed.stderr.strip()

    def _exec_git_status(self, action: Dict[str, Any]) -> str:
        code, stdout, stderr = self._run_git(["status", "--short"])
        if code != 0:
            return f"git status failed: {stderr or stdout or code}"
        return stdout or "working tree clean"

    def _exec_git_diff(self, action: Dict[str, Any]) -> str:
        path = str(action.get("path", "") or "").strip()
        args = ["diff", "--", path] if path else ["diff"]
        code, stdout, stderr = self._run_git(args)
        if code != 0:
            return f"git diff failed: {stderr or stdout or code}"
        self._latest_review = {
            "action_type": "git_diff",
            "path": path,
            "diff": stdout or "",
            "summary": f"Git diff rendered for {path or 'workspace'}.",
        }
        return stdout or "no diff"

    def _exec_show_diff(self, action: Dict[str, Any]) -> str:
        path = str(action.get("path", "") or "").strip()
        args = ["--path", path] if path else []
        payload = self._run_toolbelt_command("git-diff", *args)
        if not payload.get("ok"):
            raw_error = payload.get("error")
            error: Dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
            return f"show_diff failed: {error.get('message', 'git diff failed')}"
        raw_data = payload.get("data")
        data: Dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        diff = str(data.get("diff", "") or "")
        stat = str(data.get("stat", "") or "")
        self._latest_review = {
            "action_type": "show_diff",
            "path": str(data.get("path", path) or path),
            "diff": diff,
            "stat": stat,
            "files": list(data.get("files", [])) if isinstance(data.get("files"), list) else [],
            "summary": f"Show diff completed for {path or 'workspace'}.",
        }
        if stat and diff:
            return f"{stat}\n\n{diff}"
        return diff or stat or "no diff"

    def _exec_review_changes(self, action: Dict[str, Any]) -> str:
        path = str(action.get("path", "") or "").strip()
        limit = int(action.get("limit", 20) or 20)
        args = ["--limit", str(limit)]
        if path:
            args = ["--path", path, "--limit", str(limit)]
        payload = self._run_toolbelt_command("review", *args)
        if not payload.get("ok"):
            raw_error = payload.get("error")
            error: Dict[str, Any] = raw_error if isinstance(raw_error, dict) else {}
            return f"review_changes failed: {error.get('message', 'review failed')}"
        raw_data = payload.get("data")
        data: Dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        raw_review_summary = data.get("review_summary")
        review_summary: Dict[str, Any] = raw_review_summary if isinstance(raw_review_summary, dict) else {}
        changed_count = int(review_summary.get("changed_file_count", 0) or 0) if isinstance(review_summary, dict) else 0
        high_risk_count = int(review_summary.get("high_risk_count", 0) or 0) if isinstance(review_summary, dict) else 0
        self._latest_review = {
            "action_type": "review_changes",
            "path": path,
            "files": list(data.get("files", [])) if isinstance(data.get("files"), list) else [],
            "review_summary": review_summary,
            "high_risk_paths": list(data.get("high_risk_paths", [])) if isinstance(data.get("high_risk_paths"), list) else [],
            "summary": f"Reviewed {changed_count} changed file(s); {high_risk_count} high-risk.",
        }
        return self._latest_review["summary"]

    def _exec_git_add(self, action: Dict[str, Any]) -> str:
        raw_paths = action.get("path")
        paths = raw_paths if isinstance(raw_paths, list) else [raw_paths]
        clean_paths = [str(item or "").strip() for item in paths if str(item or "").strip()]
        code, stdout, stderr = self._run_git(["add", *clean_paths])
        if code != 0:
            return f"git add failed: {stderr or stdout or code}"
        return "staged: " + ", ".join(clean_paths)

    def _exec_git_restore(self, action: Dict[str, Any]) -> str:
        raw_paths = action.get("path")
        paths = raw_paths if isinstance(raw_paths, list) else [raw_paths]
        clean_paths = [str(item or "").strip() for item in paths if str(item or "").strip()]
        code, stdout, stderr = self._run_git(["restore", *clean_paths])
        if code != 0:
            return f"git restore failed: {stderr or stdout or code}"
        return "restored: " + ", ".join(clean_paths)

    def _exec_git_commit(self, action: Dict[str, Any]) -> str:
        message = str(action.get("message", "") or "").strip()
        if not message:
            return "git commit failed: missing message"
        code, stdout, stderr = self._run_git(["commit", "-m", message])
        if code != 0:
            return f"git commit failed: {stderr or stdout or code}"
        return stdout or "commit created"

    def _exec_git_log(self, action: Dict[str, Any]) -> str:
        code, stdout, stderr = self._run_git(["log", "--oneline", "-5"])
        if code != 0:
            return f"git log failed: {stderr or stdout or code}"
        return stdout or "no commits"

    def _exec_git_branch(self, action: Dict[str, Any]) -> str:
        code, stdout, stderr = self._run_git(["branch", "--show-current"])
        if code != 0:
            return f"git branch failed: {stderr or stdout or code}"
        return stdout or "detached"


def make_loop(
    *,
    root: Path,
    provider: str = "gemini",
    model: str = "gemini-2.5-flash",
    thinking_mode: str = "low",
    max_turns: int = 100,
    checkpoint_interval: int = 20,
    verbose: bool = True,
) -> TreeLoopPlannerWorker:
    llm = create_model_client(
        provider=provider,
        model=model,
        thinking_mode=thinking_mode,
    )
    return TreeLoopPlannerWorker(
        model=llm,
        root=root,
        max_turns=max_turns,
        checkpoint_interval=checkpoint_interval,
        thinking_mode=thinking_mode,
        provider=provider,
        model_name=model,
        verbose=verbose,
    )


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="TreeLoop beta planner entrypoint")
    parser.add_argument("--root", type=Path, default=Path("."), help="Workspace root directory")
    parser.add_argument("--provider", default="gemini", help="LLM provider")
    parser.add_argument("--model", default="gemini-2.5-flash", help="Model name")
    parser.add_argument("--thinking-mode", default="low", help="Thinking mode")
    parser.add_argument("--verbosity", default="medium", help="Model verbosity when supported")
    parser.add_argument("--turns", type=int, default=100, help="Max turns per task")
    parser.add_argument("--checkpoint", type=int, default=20, help="Pause every N turns for proceed/stop (0 disables checkpoints)")
    parser.add_argument("--task", help="Run this task immediately")
    parser.add_argument("--tools", help="Compatibility flag for extension launches; beta runtime resolves agent_tools.py internally")
    parser.add_argument("--worker-mode", action="store_true", help="Run the raw TreeLoop worker loop instead of the planner")
    parser.add_argument("--extension-bridge", action="store_true", help="Run a JSON bridge for the VS Code extension")
    normalized = _normalize_cli_argv(list(argv) if argv is not None else sys.argv[1:])
    return parser.parse_args(normalized)


def _run_extension_bridge(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    tool_script = Path(__file__).with_name("agent_tools.py").resolve()
    model_holder: Dict[str, Any] = {
        "client": create_model_client(
            provider=args.provider,
            model=args.model,
            thinking_mode=args.thinking_mode,
            verbosity=args.verbosity,
        )
    }
    transcript: List[Dict[str, str]] = []
    last_bridge_state: Optional[Dict[str, Any]] = None

    config = SimpleNamespace(
        root=root,
        provider=args.provider,
        model=args.model,
        tool_script=tool_script,
        max_steps=args.turns,
        thinking_mode=args.thinking_mode,
        verbosity=args.verbosity,
        max_parallel_workers=4,
    )

    safe_bridge_state_fn: Callable[[str], Dict[str, Any]] = lambda last_message="": {
        "planner": {},
        "transcript": transcript[-40:],
        "last_message": last_message,
    }

    def emit_progress(payload: Dict[str, Any], *, domain: str = "worker") -> None:
        _emit_bridge_message(
            {
                "type": "progress",
                "domain": domain or "worker",
                **payload,
                "state": safe_bridge_state_fn(""),
            }
        )

    def attach_bridge_progress(worker: Any) -> Any:
        worker.on_step_callback = lambda payload, worker=worker: emit_progress(
            dict(payload) if isinstance(payload, dict) else {},
            domain=str(getattr(worker, "bridge_progress_domain", "") or "worker"),
        )
        return worker

    def worker_factory() -> TreeLoopPlannerWorker:
        return attach_bridge_progress(TreeLoopPlannerWorker(
            model=model_holder["client"].clone(),
            root=root,
            max_turns=args.turns,
            checkpoint_interval=0,  # disabled in bridge mode — stdin is JSON protocol, not interactive
            thinking_mode=args.thinking_mode,
            provider=config.provider,
            model_name=config.model,
            verbosity=config.verbosity,
            verbose=False,
        ))

    planner = PlannerAgent(
        model_client=model_holder["client"],
        config=config,
        worker_factory=worker_factory,
        json_loader=extract_first_json_object,
    )

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

    safe_bridge_state_fn = safe_bridge_state

    def add_exchange(role: str, content: str) -> None:
        text = str(content or "").strip()
        if not text:
            return
        transcript.append({"role": role, "content": text})

    def summarize_bridge_action_result(action: Dict[str, Any], result: ActionResult) -> str:
        action_type = str(action.get("type", "") or result.name or "action")
        payload = result.payload if isinstance(result.payload, dict) else {}
        prefix = "Action failed" if not result.ok else "Action completed"
        error = payload.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message", "") or error.get("code", "")).strip()
            if detail:
                return f"{prefix}: {action_type}. {detail}"
        if isinstance(error, str) and error.strip():
            return f"{prefix}: {action_type}. {error.strip()}"
        detail_keys = ["summary", "message"] if action_type in {"list_issues", "show_issue"} else ["message", "summary"]
        for key in detail_keys:
            detail = str(payload.get(key, "") or "").strip()
            if detail:
                return f"{prefix}: {action_type}. {detail}"
        return f"{prefix}: {action_type}."

    def _goal_callback(event: str, index: int, goal_id: str, title: str) -> None:
        _emit_bridge_message(
            {
                "type": event,
                "domain": "plan",
                "goal_index": index,
                "goal_id": goal_id,
                "goal_title": title,
                "state": safe_bridge_state(),
            }
        )

    planner.on_goal_callback = _goal_callback
    planner.on_discovery_callback = lambda event, mode: emit_progress(
        {
            "step": 0,
            "action_type": event,
            "path": "",
            "ok": True,
            "elapsed_s": 0,
            "thought": "",
            "summary": f"Discovery {event.removeprefix('discovery_')} ({mode}).",
            "skill_name": "",
            "skill_mode": "",
            "skill_count": 0,
            "diff": "",
            "replacements": 0,
            "added_lines": 0,
            "removed_lines": 0,
            "search_excerpt": "",
            "replace_excerpt": "",
            "inspected_file_count": 0,
            "inspected_files": [],
        },
        domain="discovery",
    )
    planner.worker = attach_bridge_progress(planner.worker)

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
                message = "Backoff is not available in the beta TreeLoop bridge."
                _emit_bridge_message({"id": request_id, "ok": True, "state": safe_bridge_state(message), "message": message, "backoff": {"enabled": False, "token_limit_k": 0, "window_tokens_used": 0}})
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
                if text.strip().lower() in {"/reset", "reset"}:
                    transcript.clear()
                add_exchange("assistant", message)
                _emit_bridge_message({"id": request_id, "ok": True, "state": safe_bridge_state(message), "message": message})
                continue

            if request_type == "planner_action":
                message = _handle_bridge_planner_action(
                    planner=planner,
                    transcript=transcript,
                    request=request,
                    add_exchange=add_exchange,
                    emit_progress=lambda payload: emit_progress(payload, domain="discovery"),
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
                raw_result = executor(action, thought="Operator action from extension UI.")
                if not isinstance(raw_result, ActionResult):
                    raise ValueError("Worker action bridge returned an invalid result")
                result = raw_result
                message = summarize_bridge_action_result(action, result)
                add_exchange("assistant", message)
                _emit_bridge_message({"id": request_id, "ok": result.ok, "state": safe_bridge_state(message), "message": message})
                continue

            raise ValueError(f"Unsupported bridge request type: {request_type}")
        except Exception as exc:
            _emit_bridge_message({"id": request_id, "ok": False, "message": str(exc), "state": safe_bridge_state()})
    return 0


def interactive_treeloop_worker_loop(worker: TreeLoopPlannerWorker) -> None:
    loop = worker.loop
    last_result = None
    print("=" * 60)
    print("TreeLoop Beta Worker")
    print(f"Workspace: {worker.root}")
    print(f"Provider:  {worker.provider} / {worker.model_name}")
    print(f"Files indexed: {len(list(loop.bridge.tree._repo.walk()))}")
    ckpt = f" (checkpoint every {worker.checkpoint_interval})" if worker.checkpoint_interval > 0 else ""
    print(f"Max turns: {worker.max_turns}{ckpt}")
    print("=" * 60)
    print_worker_status(worker)
    print()
    print("Commands:")
    print("  <task>       Run a task")
    print("  /facts       Show recorded facts")
    print("  /history     Show last run history")
    print("  /status      Show repo facts and latest review state")
    print("  /tree <cmd>  Run a raw tree command")
    print("  /turns N     Set max turns")
    print("  /reset       Reset loop history")
    print("  /quit        Exit")
    print()

    while True:
        try:
            raw = input("task> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBye.")
            return

        if not raw:
            continue
        if raw == "/quit":
            print("Bye.")
            return
        if raw == "/facts":
            facts = loop.bridge.tree.ls("/facts", depth=4)
            if not facts:
                print("  (no facts recorded)")
            for entry in facts:
                path = entry.get("path", entry.get("name", "?"))
                print(f"  /{path}")
            continue
        if raw == "/history":
            if last_result is None:
                print("  (no runs yet)")
            else:
                print_history(last_result)
            continue
        if raw == "/status":
            print_worker_status(worker)
            continue
        if raw.startswith("/tree "):
            cmd = raw[6:].strip()
            results = loop.bridge.execute(cmd)
            for command_result in results:
                ok = "✓" if command_result.ok else "✗"
                print(f"[{ok}] {command_result.output}")
            continue
        if raw.startswith("/turns "):
            try:
                turns = int(raw.split()[1])
            except ValueError:
                print("Usage: /turns N")
                continue
            worker.max_turns = turns
            loop.max_turns = turns
            print(f"Max turns set to {turns}")
            continue
        if raw == "/reset":
            worker.prepare_for_goal(preserve_context=False)
            last_result = None
            print("Loop reset.")
            continue

        run_result = worker.run_task(raw)
        last_result = SimpleNamespace(turns=list(worker.loop.history), summary=lambda: run_result.final_message)
        print(f"\nFINAL: {run_result.final_message}")
        print(worker.render_last_usage_summary())
        print_worker_status(worker)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    root = args.root.expanduser().resolve()
    if not root.exists():
        print(f"Error: {root} does not exist", file=sys.stderr)
        return 2
    if not root.is_dir():
        print(f"Error: {root} is not a directory", file=sys.stderr)
        return 2

    refresh_runtime_provider_catalog_once()
    if args.extension_bridge:
        return _run_extension_bridge(args)
    tool_script = Path(__file__).with_name("agent_tools.py").resolve()
    model_holder: Dict[str, Any] = {
        "client": create_model_client(
            provider=args.provider,
            model=args.model,
            thinking_mode=args.thinking_mode,
            verbosity=args.verbosity,
        )
    }

    config = SimpleNamespace(
        root=root,
        provider=args.provider,
        model=args.model,
        tool_script=tool_script,
        max_steps=args.turns,
        thinking_mode=args.thinking_mode,
        verbosity=args.verbosity,
        max_parallel_workers=4,
    )

    if args.worker_mode:
        worker = TreeLoopPlannerWorker(
            model=model_holder["client"],
            root=root,
            max_turns=args.turns,
            checkpoint_interval=args.checkpoint,
            thinking_mode=args.thinking_mode,
            provider=args.provider,
            model_name=args.model,
            verbosity=args.verbosity,
            verbose=True,
        )
        if args.task:
            result = worker.run_task(args.task)
            print(f"FINAL: {result.final_message}")
            print(worker.render_last_usage_summary())
            return 0 if result.ok else 1
        interactive_treeloop_worker_loop(worker)
        return 0

    def worker_factory() -> TreeLoopPlannerWorker:
        return TreeLoopPlannerWorker(
            model=model_holder["client"].clone(),
            root=root,
            max_turns=args.turns,
            checkpoint_interval=args.checkpoint,
            thinking_mode=args.thinking_mode,
            provider=config.provider,
            model_name=config.model,
            verbosity=config.verbosity,
            verbose=True,
        )

    planner = PlannerAgent(
        model_client=model_holder["client"],
        config=config,
        worker_factory=worker_factory,
        json_loader=extract_first_json_object,
    )

    def runtime_reconfigure(provider: str, model: str) -> Dict[str, Any]:
        next_client = create_model_client(
            provider=provider,
            model=model,
            thinking_mode=config.thinking_mode,
            verbosity=config.verbosity,
        )
        model_holder["client"] = next_client
        return planner.reconfigure_runtime(
            model_client=next_client,
            provider=provider,
            model=model,
            thinking_mode=config.thinking_mode,
            verbosity=config.verbosity,
        )

    if args.task:
        message = planner.start_request(args.task)
        print(message)
        return 0

    interactive_planner_loop(
        planner,
        worker_debug_loop=interactive_treeloop_worker_loop,
        runtime_reconfigure=runtime_reconfigure,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
