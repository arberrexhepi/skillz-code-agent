"""context_compaction.py — heuristic compaction of planner prompt payloads.

Reduces token count before any LLM-based summarization by applying cheap,
lossless-or-low-loss structural transforms:

  - Drop null / empty-list / empty-dict values from top-level prompt fields
    (follow_up_context, revision_context, pending_discovery, etc.)
  - Drop null / empty-list values from top-level repo_facts fields
    (active_issue, context_facts, active_issue_goal_facts when all empty)
  - Strip zero-value fields from every issue summary object
    (reopen_count=0, fact_count=0, lifecycle_notes=[], …)
  - Drop timestamps (opened_at / closed_at) from zero-fact closed issues
  - Truncate request_summary / plan_summary for zero-fact closed issues
  - Cap the issues[] history list and emit a prior_issues_omitted counter

None of these transforms alter semantics for a zero-facts / no-active-issue
turn.  Fields that carry real information are never truncated.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Tuneable constants
# ---------------------------------------------------------------------------

#: Maximum number of issues kept in the issues[] history array.
#: Older entries are replaced with a ``prior_issues_omitted`` integer counter.
DEFAULT_ISSUES_HISTORY_LIMIT = 5

#: Maximum chars for request_summary / plan_summary on zero-fact closed issues.
#: Entries beyond this length get a trailing "…" appended.
DEFAULT_ZERO_FACT_SUMMARY_CHARS = 60


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_empty(value: Any) -> bool:
    """Return True if value should be omitted from a compacted payload."""
    if value is None:
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _has_facts(issue: Dict[str, Any]) -> bool:
    """Return True if any fact count field is non-zero."""
    return (
        int(issue.get("fact_count", 0) or 0) > 0
        or int(issue.get("architecture_fact_count", 0) or 0) > 0
        or int(issue.get("goal_fact_count", 0) or 0) > 0
    )


def _compact_issue_summary(
    issue: Dict[str, Any],
    *,
    summary_char_limit: int = DEFAULT_ZERO_FACT_SUMMARY_CHARS,
) -> Dict[str, Any]:
    """Return a compacted copy of a single issue summary dict.

    For zero-fact closed issues:
      - Truncate request_summary / plan_summary to summary_char_limit chars
      - Drop opened_at / closed_at (timestamps carry no planning value)
      - Drop all zero-value / empty fields

    For issues that have facts, only zero-value fields are dropped.
    """
    issue = dict(issue)

    if not _has_facts(issue):
        # Truncate verbose summaries — the LLM only needs enough to let the
        # user recognise an issue for reopening.
        for field in ("request_summary", "plan_summary"):
            val = str(issue.get(field, "") or "")
            if len(val) > summary_char_limit:
                issue[field] = val[:summary_char_limit] + "…"
        # Timestamps add nothing for issues that left no durable knowledge.
        issue.pop("opened_at", None)
        issue.pop("closed_at", None)

    # Strip every zero / empty field regardless of fact status.
    return {k: v for k, v in issue.items() if not _is_empty(v) and v != 0}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compact_repo_facts(
    payload: Optional[Dict[str, Any]],
    *,
    issues_history_limit: int = DEFAULT_ISSUES_HISTORY_LIMIT,
    summary_char_limit: int = DEFAULT_ZERO_FACT_SUMMARY_CHARS,
) -> Optional[Dict[str, Any]]:
    """Compact the output of ``IssueFactLedger.planner_payload()``.

    Args:
        payload: The raw dict returned by ``planner_payload()``.  May be
            ``None`` (when no repo_facts file exists); returns ``None``
            unchanged.
        issues_history_limit: Maximum number of issues to keep in the
            ``issues[]`` array.  Excess older entries are dropped and
            summarised by a ``prior_issues_omitted`` integer field.
        summary_char_limit: Maximum characters for ``request_summary`` /
            ``plan_summary`` on zero-fact closed issues.

    Returns:
        A new dict with the same semantics but fewer tokens, or ``None``.
    """
    if not isinstance(payload, dict):
        return payload

    # 1. Drop null / empty top-level fields (active_issue=null, context_facts=[], …)
    out: Dict[str, Any] = {k: v for k, v in payload.items() if not _is_empty(v)}

    # 2. Compact issue summaries in reopenable_issues.
    if "reopenable_issues" in out:
        out["reopenable_issues"] = [
            _compact_issue_summary(i, summary_char_limit=summary_char_limit)
            for i in out["reopenable_issues"]
            if isinstance(i, dict)
        ]

    # 3. Cap issues[] history and compact each entry.
    if "issues" in out:
        all_issues: List[Dict[str, Any]] = [i for i in out["issues"] if isinstance(i, dict)]
        omitted = max(0, len(all_issues) - issues_history_limit)
        retained = all_issues[omitted:]   # most-recent N

        out["issues"] = [
            _compact_issue_summary(i, summary_char_limit=summary_char_limit)
            for i in retained
        ]
        if omitted > 0:
            out["prior_issues_omitted"] = omitted

    return out


def compact_prompt_payload(
    payload: Dict[str, Any],
    *,
    issues_history_limit: int = DEFAULT_ISSUES_HISTORY_LIMIT,
    summary_char_limit: int = DEFAULT_ZERO_FACT_SUMMARY_CHARS,
) -> Dict[str, Any]:
    """Compact a full planner prompt payload dict before ``json.dumps``.

    Drops null / empty optional fields at the top level
    (``follow_up_context``, ``revision_context``, ``pending_discovery``,
    ``last_discovery``, ``last_discovery_findings``, ``completed_results``,
    ``pending_plan``, …) and delegates ``repo_facts`` compaction to
    :func:`compact_repo_facts`.

    ``root`` and ``request`` are always kept even if empty.

    Args:
        payload: The raw dict assembled by the planner before serialisation.
        issues_history_limit: Forwarded to :func:`compact_repo_facts`.
        summary_char_limit: Forwarded to :func:`compact_repo_facts`.

    Returns:
        A new dict ready for ``json.dumps``.
    """
    # Always keep identity fields regardless of emptiness.
    always_keep = {"root", "request"}

    out: Dict[str, Any] = {}
    for key, value in payload.items():
        if key in always_keep or not _is_empty(value):
            out[key] = value

    if "repo_facts" in out:
        out["repo_facts"] = compact_repo_facts(
            out["repo_facts"],
            issues_history_limit=issues_history_limit,
            summary_char_limit=summary_char_limit,
        )

    return out


def serialize_prompt(
    payload: Dict[str, Any],
    *,
    issues_history_limit: int = DEFAULT_ISSUES_HISTORY_LIMIT,
    summary_char_limit: int = DEFAULT_ZERO_FACT_SUMMARY_CHARS,
    indent: int = 2,
) -> str:
    """Compact and serialise a planner prompt payload to JSON.

    Convenience wrapper around :func:`compact_prompt_payload` + ``json.dumps``.

    Args:
        payload: Raw prompt payload dict.
        issues_history_limit: Forwarded to :func:`compact_repo_facts`.
        summary_char_limit: Forwarded to :func:`compact_repo_facts`.
        indent: JSON indentation level (default 2).

    Returns:
        Compacted JSON string.
    """
    return json.dumps(
        compact_prompt_payload(
            payload,
            issues_history_limit=issues_history_limit,
            summary_char_limit=summary_char_limit,
        ),
        indent=indent,
    )
