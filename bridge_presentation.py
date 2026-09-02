"""Lossless UI annotations for the bridge; never alters the model transcript.

Match the planner's own rendered blocks, not arbitrary prose containing words
like 'approve'. Snapshot reports now so later requests cannot replace history.
"""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from copy import deepcopy
import re
from uuid import uuid4
from typing import Any


def _snapshot(value: Any) -> dict:
    snapshot = asdict(value) if is_dataclass(value) else deepcopy(value) if isinstance(value, dict) else {}
    # Reports do not need another copy of full worker history or delegated prompts.
    for key in ("worker_history_summary", "delegated_task", "original_request"):
        snapshot.pop(key, None)
    return snapshot


def _report(category: str, status: str, title: str, content: str, **extras: Any) -> dict:
    return {"kind": "workflow", "category": category, "status": status, "title": title, "content": content, **extras}


def presentation_parts(planner: Any, role: str, text: str) -> list[dict]:
    session = planner.session
    pending_plan = getattr(session, "pending_plan", None)
    pending_discovery = getattr(session, "pending_discovery", None)
    extension = getattr(session, "pending_discovery_extension", None)
    if role == "user":
        if extension is not None and (planner._is_approval(text) or planner._is_rejection(text)):
            return [_report("discovery", "selected", "Discovery extension", text,
                            selection="Extension approved" if planner._is_approval(text) else "Plan with current findings",
                            extension=deepcopy(extension))]
        revision_plan = pending_plan or (getattr(session, "paused_plan", None) if getattr(session, "execution_paused", False) else None)
        if revision_plan is not None and text.startswith("Suggest plan changes:\n"):
            return [_report("plan", "selected", "Goal plan", text, selection="Changes requested", plan=_snapshot(revision_plan))]
        if pending_plan is not None:
            if planner._is_approval(text) or planner._is_rejection(text):
                choice = "Approved" if planner._is_approval(text) else "Rejected"
                return [_report("plan", "selected", "Goal plan", text, selection=choice, plan=_snapshot(pending_plan))]
        elif pending_discovery is not None:
            mode = planner._parse_discovery_selection(text)
            if mode or planner._is_rejection(text):
                return [_report("discovery", "selected", "Discovery", text, selection=mode.title() if mode else "Skipped")]
        if re.fullmatch(r"/?(?:create-issue\s+.+|(?:close-issue|close|reopen)\s+issue-[\w-]+)", text, re.I):
            return [_report("issue", "requested", "Issue action", text)]
        return [{"kind": "message", "content": text}]

    if role != "assistant":
        return [{"kind": "message", "content": text}]
    if re.match(r"^(?:Created|Closed|Reopened|Activated) issue\s+issue-[\w-]+", text):
        return [_report("issue", "complete", text.split(".", 1)[0], text)]
    if text in {"Plan rejected. Describe what should change and I will revise it.", "Paused execution rejected. Describe what should change and I will revise the plan."}:
        return [_report("plan", "rejected", "Goal plan", text, selection="Rejected")]
    if text == "Discovery skipped. Add more detail or let me know what should be assumed instead.":
        return [_report("discovery", "skipped", "Discovery", text, selection="Skipped")]

    blocks: list[tuple[str, dict]] = []
    if extension is not None:
        rendered = planner._render_discovery_extension()
        blocks.append((rendered, _report("discovery", "paused", "Discovery extension", rendered, extension=deepcopy(extension))))
    if pending_discovery is not None:
        rendered = planner._render_discovery_offer(pending_discovery)
        blocks.append((rendered, _report("discovery", "offered", "Discovery", rendered, summary=getattr(pending_discovery, "reason", ""))))
    discovery = getattr(session, "last_discovery", None)
    if discovery is not None:
        rendered = planner._render_discovery_result(discovery)
        blocks.append((rendered, _report("discovery", getattr(discovery, "outcome", "") or ("complete" if discovery.ok else "failed"), "Discovery", rendered, discovery=_snapshot(discovery))))
    plans = [pending_plan, getattr(session, "paused_plan", None), getattr(session, "last_completed_plan", None), getattr(session, "last_presented_plan", None)]
    for plan in plans:
        if plan is not None:
            rendered = planner._render_plan(plan)
            blocks.append((rendered, _report("plan", "offered", "Goal plan", rendered, summary=plan.summary, plan=_snapshot(plan))))
    results = getattr(session, "last_completed_results", None) or getattr(session, "completed_results", [])
    plan = next((candidate for candidate in plans if candidate is not None), None)
    total = len(plan.goals) if plan is not None else len(results)
    for index, result in enumerate(results, 1):
        rendered = planner._render_goal_result(index, total, result)
        if rendered not in text and getattr(result, "commentary_for_next_goal", ""):
            # Guidance is added after the result's text was emitted.
            original_result = deepcopy(result)
            original_result.commentary_for_next_goal = ""
            rendered = planner._render_goal_result(index, total, original_result)
        blocks.append((rendered, _report("plan", "complete" if result.status == "completed" else "failed", "Goal plan", rendered, goals=[_snapshot(result)], plan=_snapshot(plan))))

    # Keep every unrecognized span, including conversational text after reports.
    parts: list[dict] = []
    remaining = text
    while remaining:
        matches = [(remaining.find(block), block, part) for block, part in blocks if block and block in remaining]
        matches = [(index, block, part) for index, block, part in matches
                   if (index == 0 or remaining[:index].endswith("\n\n"))
                   and (index + len(block) == len(remaining) or remaining[index + len(block):].startswith("\n\n"))]
        if not matches:
            parts.append({"kind": "message", "content": remaining})
            break
        index, block, part = min(matches, key=lambda item: (item[0], -len(item[1])))
        if remaining[:index].strip():
            parts.append({"kind": "message", "content": remaining[:index].strip()})
        parts.append(part)
        remaining = remaining[index + len(block):].strip()
    return parts


def bridge_exchange(planner: Any, role: str, content: str) -> dict:
    text = str(content or "").strip()
    entry = {"id": uuid4().hex, "role": role, "content": text}
    try:
        entry["presentation"] = presentation_parts(planner, role, text)
    except Exception:
        # Presentation must never break an agent action or remove its output.
        pass
    return entry
