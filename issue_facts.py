from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = 2
FACT_TYPE_GOAL = "goal"
FACT_TYPE_ARCHITECTURE = "architecture"
ISSUE_STATUS_OPEN = "open"
ISSUE_STATUS_CLOSED = "closed"
AUTO_COMPACT_CLOSED_ISSUE_DETAIL_LIMIT = 3
AUTO_COMPACT_LIFECYCLE_NOTE_LIMIT = 2
LEGACY_ISSUE_ID = "legacy-architecture"
GLOBAL_ARCHITECTURE_ISSUE_ID = "global-architecture"
REPO_FACTS_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _normalize_fact_type(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == FACT_TYPE_GOAL:
        return FACT_TYPE_GOAL
    return FACT_TYPE_ARCHITECTURE


def _normalize_issue_status(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == ISSUE_STATUS_OPEN:
        return ISSUE_STATUS_OPEN
    return ISSUE_STATUS_CLOSED


def _record_sort_key(record: "IssueFactRecord") -> tuple[int, int, str, str, str]:
    return (
        int(record.updated_run_id or 0),
        int(record.updated_step or 0),
        str(record.issue_id or ""),
        str(record.fact_type or ""),
        str(record.key or ""),
    )


@dataclass
class IssueFactRecord:
    key: str
    value: str
    fact_type: str = FACT_TYPE_ARCHITECTURE
    issue_id: str = ""
    source_action: str = ""
    updated_step: int = 0
    updated_run_id: int = 0
    issue_status: str = ""

    def clone(self, *, issue_status: Optional[str] = None) -> "IssueFactRecord":
        return IssueFactRecord(
            key=self.key,
            value=self.value,
            fact_type=self.fact_type,
            issue_id=self.issue_id,
            source_action=self.source_action,
            updated_step=self.updated_step,
            updated_run_id=self.updated_run_id,
            issue_status=self.issue_status if issue_status is None else issue_status,
        )

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "key": self.key,
            "value": self.value,
            "fact_type": self.fact_type,
            "issue_id": self.issue_id,
            "source_action": self.source_action,
            "updated_step": self.updated_step,
            "updated_run_id": self.updated_run_id,
        }
        if self.issue_status:
            payload["issue_status"] = self.issue_status
        return payload

    def to_persisted_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "value": self.value,
            "fact_type": self.fact_type,
            "source_action": self.source_action,
            "updated_step": self.updated_step,
            "updated_run_id": self.updated_run_id,
        }

    @classmethod
    def from_dict(cls, item: Dict[str, Any], *, issue_id: str, issue_status: str = "") -> Optional["IssueFactRecord"]:
        key = str(item.get("key", "") or "").strip()
        value = str(item.get("value", "") or "").strip()
        if not key or not value:
            return None
        return cls(
            key=key,
            value=value,
            fact_type=_normalize_fact_type(item.get("fact_type")),
            issue_id=issue_id,
            source_action=str(item.get("source_action", "") or ""),
            updated_step=int(item.get("updated_step", 0) or 0),
            updated_run_id=int(item.get("updated_run_id", 0) or 0),
            issue_status=issue_status,
        )


@dataclass
class IssueRecord:
    issue_id: str
    request_summary: str = ""
    plan_summary: str = ""
    status: str = ISSUE_STATUS_CLOSED
    opened_at: str = ""
    closed_at: str = ""
    reopen_count: int = 0
    source: str = ""
    parent_issue_id: str = ""
    source_excerpt: str = ""
    priority: int = 0
    blocked_reason: str = ""
    last_review_decision: str = ""
    lifecycle_notes: List[str] = field(default_factory=list)
    facts: List[IssueFactRecord] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "issue_id": self.issue_id,
            "request_summary": self.request_summary,
            "plan_summary": self.plan_summary,
            "status": self.status,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "reopen_count": self.reopen_count,
            "source": self.source,
            "parent_issue_id": self.parent_issue_id,
            "source_excerpt": self.source_excerpt,
            "priority": self.priority,
            "blocked_reason": self.blocked_reason,
            "last_review_decision": self.last_review_decision,
            "lifecycle_notes": list(self.lifecycle_notes),
            "facts": [record.to_persisted_dict() for record in sorted(self.facts, key=lambda item: (item.fact_type, item.key))],
        }

    def summary(self) -> Dict[str, Any]:
        architecture_count = len([record for record in self.facts if record.fact_type == FACT_TYPE_ARCHITECTURE])
        goal_count = len([record for record in self.facts if record.fact_type == FACT_TYPE_GOAL])
        return {
            "issue_id": self.issue_id,
            "request_summary": self.request_summary,
            "plan_summary": self.plan_summary,
            "status": self.status,
            "opened_at": self.opened_at,
            "closed_at": self.closed_at,
            "reopen_count": self.reopen_count,
            "source": self.source,
            "parent_issue_id": self.parent_issue_id,
            "source_excerpt": self.source_excerpt,
            "priority": self.priority,
            "blocked_reason": self.blocked_reason,
            "last_review_decision": self.last_review_decision,
            "lifecycle_notes": list(self.lifecycle_notes),
            "fact_count": len(self.facts),
            "architecture_fact_count": architecture_count,
            "goal_fact_count": goal_count,
        }

    @classmethod
    def from_dict(cls, item: Dict[str, Any]) -> Optional["IssueRecord"]:
        issue_id = str(item.get("issue_id", "") or "").strip()
        if not issue_id:
            return None
        status = _normalize_issue_status(item.get("status"))
        facts: List[IssueFactRecord] = []
        for fact_item in item.get("facts", []) or []:
            if not isinstance(fact_item, dict):
                continue
            fact = IssueFactRecord.from_dict(fact_item, issue_id=issue_id, issue_status=status)
            if fact is not None:
                facts.append(fact)
        return cls(
            issue_id=issue_id,
            request_summary=str(item.get("request_summary", "") or ""),
            plan_summary=str(item.get("plan_summary", "") or ""),
            status=status,
            opened_at=str(item.get("opened_at", "") or ""),
            closed_at=str(item.get("closed_at", "") or ""),
            reopen_count=int(item.get("reopen_count", 0) or 0),
            source=str(item.get("source", "") or ""),
            parent_issue_id=str(item.get("parent_issue_id", "") or ""),
            source_excerpt=str(item.get("source_excerpt", "") or ""),
            priority=int(item.get("priority", 0) or 0),
            blocked_reason=str(item.get("blocked_reason", "") or ""),
            last_review_decision=str(item.get("last_review_decision", "") or ""),
            lifecycle_notes=[str(note) for note in item.get("lifecycle_notes", []) or [] if str(note).strip()],
            facts=facts,
        )


@dataclass
class IssueFactLedger:
    schema_version: int = SCHEMA_VERSION
    active_issue_id: str = ""
    issues: List[IssueRecord] = field(default_factory=list)
    migration: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "IssueFactLedger":
        return cls()

    @classmethod
    def load(cls, path: Path) -> "IssueFactLedger":
        if not path.exists() or not path.is_file():
            return cls.empty()
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return cls.empty()
        return cls.from_markdown(text)

    @classmethod
    def from_markdown(cls, text: str) -> "IssueFactLedger":
        if not str(text or "").strip():
            return cls.empty()
        match = REPO_FACTS_JSON_BLOCK_RE.search(text)
        candidate = match.group(1) if match else text.strip()
        try:
            payload = json.loads(candidate)
        except Exception:
            return cls.empty()
        return cls.from_payload(payload)

    @classmethod
    def from_payload(cls, payload: Any) -> "IssueFactLedger":
        if isinstance(payload, list):
            return cls._migrate_legacy_flat_facts(payload)
        if not isinstance(payload, dict):
            return cls.empty()
        if isinstance(payload.get("facts"), list):
            return cls._migrate_legacy_flat_facts(payload.get("facts") or [])
        schema_version = int(payload.get("schema_version", 0) or 0)
        if schema_version not in {SCHEMA_VERSION}:
            return cls.empty()
        issues: List[IssueRecord] = []
        for item in payload.get("issues", []) or []:
            if not isinstance(item, dict):
                continue
            issue = IssueRecord.from_dict(item)
            if issue is not None:
                issues.append(issue)
        ledger = cls(
            schema_version=SCHEMA_VERSION,
            active_issue_id=str(payload.get("active_issue_id", "") or "").strip(),
            issues=issues,
            migration=dict(payload.get("migration", {})) if isinstance(payload.get("migration"), dict) else {},
        )
        if ledger.active_issue_id and ledger.get_issue(ledger.active_issue_id) is None:
            ledger.active_issue_id = ""
        return ledger

    @classmethod
    def _migrate_legacy_flat_facts(cls, payload: List[Any]) -> "IssueFactLedger":
        issue = IssueRecord(
            issue_id=LEGACY_ISSUE_ID,
            request_summary="Migrated legacy repo facts",
            plan_summary="Legacy flat repo_facts migration",
            status=ISSUE_STATUS_CLOSED,
            opened_at="legacy",
            closed_at=_utc_timestamp(),
        )
        for item in payload:
            if not isinstance(item, dict):
                continue
            fact = IssueFactRecord.from_dict(
                {
                    "key": item.get("key"),
                    "value": item.get("value"),
                    "fact_type": FACT_TYPE_ARCHITECTURE,
                    "source_action": item.get("source_action", ""),
                    "updated_step": item.get("updated_step", 0),
                    "updated_run_id": item.get("updated_run_id", 0),
                },
                issue_id=LEGACY_ISSUE_ID,
                issue_status=ISSUE_STATUS_CLOSED,
            )
            if fact is not None:
                issue.facts.append(fact)
        issues = [issue] if issue.facts else []
        return cls(
            schema_version=SCHEMA_VERSION,
            active_issue_id="",
            issues=issues,
            migration={"legacy_flat_list_migrated": bool(issues), "migrated_at": _utc_timestamp()},
        )

    def _closed_issue_ids_to_keep_detailed(self, detail_limit: int = AUTO_COMPACT_CLOSED_ISSUE_DETAIL_LIMIT) -> set[str]:
        retained: set[str] = set()
        eligible = [
            issue
            for issue in self.issues
            if issue.status == ISSUE_STATUS_CLOSED and issue.issue_id not in {LEGACY_ISSUE_ID, GLOBAL_ARCHITECTURE_ISSUE_ID}
        ]
        eligible.sort(
            key=lambda issue: (
                str(issue.closed_at or issue.opened_at or ""),
                str(issue.issue_id or ""),
            ),
            reverse=True,
        )
        for issue in eligible[: max(0, int(detail_limit))]:
            retained.add(issue.issue_id)
        return retained

    def _issue_to_persisted_dict(self, issue: IssueRecord, *, keep_detailed_ids: set[str]) -> Dict[str, Any]:
        payload = issue.to_dict()
        if issue.status != ISSUE_STATUS_CLOSED or issue.issue_id in keep_detailed_ids or issue.issue_id in {LEGACY_ISSUE_ID, GLOBAL_ARCHITECTURE_ISSUE_ID}:
            return payload

        lifecycle_notes = [str(note) for note in payload.get("lifecycle_notes", []) if str(note).strip()]
        payload["lifecycle_notes"] = lifecycle_notes[-AUTO_COMPACT_LIFECYCLE_NOTE_LIMIT:]
        payload["facts"] = []
        return payload

    def to_payload(self) -> Dict[str, Any]:
        issues = sorted(self.issues, key=lambda issue: (issue.opened_at or "", issue.issue_id))
        keep_detailed_ids = self._closed_issue_ids_to_keep_detailed()
        return {
            "schema_version": SCHEMA_VERSION,
            "active_issue_id": self.active_issue_id,
            "migration": dict(self.migration),
            "issues": [self._issue_to_persisted_dict(issue, keep_detailed_ids=keep_detailed_ids) for issue in issues],
        }

    def to_markdown(self) -> str:
        return "\n".join(
            [
                "# Repo Facts",
                "",
                "Issue-scoped durable facts recorded by the agent.",
                "",
                "```json",
                json.dumps(self.to_payload(), indent=2),
                "```",
                "",
            ]
        )

    def total_fact_count(self) -> int:
        return sum(len(issue.facts) for issue in self.issues)

    def get_issue(self, issue_id: str) -> Optional[IssueRecord]:
        normalized = str(issue_id or "").strip()
        if not normalized:
            return None
        for issue in self.issues:
            if issue.issue_id == normalized:
                return issue
        return None

    def active_issue(self) -> Optional[IssueRecord]:
        issue = self.get_issue(self.active_issue_id)
        if issue is None:
            return None
        if issue.status != ISSUE_STATUS_OPEN:
            return None
        return issue

    def reopenable_issues(self, limit: int = 5) -> List[Dict[str, Any]]:
        closed = [
            issue.summary()
            for issue in self.issues
            if issue.status == ISSUE_STATUS_CLOSED and issue.issue_id not in {LEGACY_ISSUE_ID, GLOBAL_ARCHITECTURE_ISSUE_ID}
        ]
        closed.sort(key=lambda item: (str(item.get("closed_at", "") or ""), str(item.get("issue_id", "") or "")), reverse=True)
        return closed[: max(0, int(limit))]

    def planner_payload(self, *, path: str = "") -> Dict[str, Any]:
        active_issue = self.active_issue()
        context_facts = [record.to_dict() for record in self.active_context_records()]
        active_issue_goal_facts = []
        if active_issue is not None:
            active_issue_goal_facts = [
                record.clone(issue_status=active_issue.status).to_dict()
                for record in sorted(active_issue.facts, key=lambda item: (item.fact_type, item.key))
                if record.fact_type == FACT_TYPE_GOAL
            ]
        return {
            "path": path,
            "schema_version": self.schema_version,
            "active_issue": active_issue.summary() if active_issue is not None else None,
            "context_facts": context_facts,
            "active_issue_goal_facts": active_issue_goal_facts,
            "reopenable_issues": self.reopenable_issues(),
            "issues": [issue.summary() for issue in sorted(self.issues, key=lambda item: (item.opened_at or "", item.issue_id))],
            "total_fact_count": self.total_fact_count(),
        }

    def available_fact_keys(self) -> List[str]:
        keys: List[str] = []
        for record in self.active_context_records():
            if record.key not in keys:
                keys.append(record.key)
        return keys

    def _next_issue_id(self) -> str:
        highest = 0
        for issue in self.issues:
            match = re.fullmatch(r"issue-(\d+)", issue.issue_id)
            if match:
                highest = max(highest, int(match.group(1)))
        return f"issue-{highest + 1:03d}"

    def _ensure_global_architecture_issue(self) -> IssueRecord:
        issue = self.get_issue(GLOBAL_ARCHITECTURE_ISSUE_ID)
        if issue is not None:
            return issue
        issue = IssueRecord(
            issue_id=GLOBAL_ARCHITECTURE_ISSUE_ID,
            request_summary="Cross-issue architecture memory",
            plan_summary="Architecture fact bucket",
            status=ISSUE_STATUS_CLOSED,
            opened_at=_utc_timestamp(),
            closed_at=_utc_timestamp(),
        )
        self.issues.append(issue)
        return issue

    def ensure_issue_open(
        self,
        *,
        request_summary: str = "",
        plan_summary: str = "",
        reuse_issue_id: str = "",
        source: str = "",
        parent_issue_id: str = "",
        source_excerpt: str = "",
        priority: int = 0,
    ) -> IssueRecord:
        if reuse_issue_id:
            issue = self.get_issue(reuse_issue_id)
            if issue is not None:
                issue.status = ISSUE_STATUS_OPEN
                issue.closed_at = ""
                if request_summary:
                    issue.request_summary = request_summary
                if plan_summary:
                    issue.plan_summary = plan_summary
                if source:
                    issue.source = source
                if parent_issue_id:
                    issue.parent_issue_id = parent_issue_id
                if source_excerpt:
                    issue.source_excerpt = source_excerpt
                if priority:
                    issue.priority = int(priority)
                self.active_issue_id = issue.issue_id
                return issue
        active_issue = self.active_issue()
        if active_issue is not None:
            if request_summary:
                active_issue.request_summary = request_summary
            if plan_summary:
                active_issue.plan_summary = plan_summary
            return active_issue
        issue = IssueRecord(
            issue_id=self._next_issue_id(),
            request_summary=request_summary,
            plan_summary=plan_summary,
            status=ISSUE_STATUS_OPEN,
            opened_at=_utc_timestamp(),
            source=source,
            parent_issue_id=parent_issue_id,
            source_excerpt=source_excerpt,
            priority=int(priority or 0),
        )
        self.issues.append(issue)
        self.active_issue_id = issue.issue_id
        return issue

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
    ) -> IssueRecord:
        issue = IssueRecord(
            issue_id=self._next_issue_id(),
            request_summary=str(request_summary or "").strip(),
            plan_summary=str(plan_summary or request_summary or "").strip(),
            status=ISSUE_STATUS_OPEN,
            opened_at=_utc_timestamp(),
            source=str(source or "").strip(),
            parent_issue_id=str(parent_issue_id or "").strip(),
            source_excerpt=str(source_excerpt or "").strip(),
            priority=int(priority or 0),
        )
        self.issues.append(issue)
        if activate:
            self.active_issue_id = issue.issue_id
        return issue

    def find_duplicate_issue(self, *, request_summary: str, source: str = "", parent_issue_id: str = "") -> Optional[IssueRecord]:
        normalized = re.sub(r"\s+", " ", str(request_summary or "").strip().lower())
        if not normalized:
            return None
        for issue in self.issues:
            candidate = re.sub(r"\s+", " ", str(issue.request_summary or issue.plan_summary or "").strip().lower())
            if candidate != normalized:
                continue
            if source and issue.source and issue.source != source:
                continue
            if parent_issue_id and issue.parent_issue_id and issue.parent_issue_id != parent_issue_id:
                continue
            return issue
        return None

    def ensure_goal_issue(self, *, task_summary: str = "") -> IssueRecord:
        active_issue = self.active_issue()
        if active_issue is not None:
            return active_issue
        return self.ensure_issue_open(request_summary=task_summary, plan_summary=task_summary)

    def close_active_issue(self, *, note: str = "") -> Optional[IssueRecord]:
        issue = self.active_issue()
        if issue is None:
            return None
        return self.close_issue(issue.issue_id, note=note)

    def close_issue(self, issue_id: str, *, note: str = "") -> IssueRecord:
        issue = self.get_issue(issue_id)
        if issue is None:
            raise KeyError(issue_id)
        issue.status = ISSUE_STATUS_CLOSED
        if not str(issue.closed_at or "").strip():
            issue.closed_at = _utc_timestamp()
        note_text = str(note or "").strip()
        if note_text:
            issue.lifecycle_notes.append(note_text)
        if self.active_issue_id == issue.issue_id:
            self.active_issue_id = ""
        return issue

    def reopen_issue(self, issue_id: str) -> IssueRecord:
        issue = self.get_issue(issue_id)
        if issue is None:
            raise KeyError(issue_id)
        issue.status = ISSUE_STATUS_OPEN
        issue.closed_at = ""
        issue.reopen_count = int(issue.reopen_count or 0) + 1
        self.active_issue_id = issue.issue_id
        return issue

    def _find_fact_index(self, issue: IssueRecord, *, key: str, fact_type: str) -> int:
        for index, record in enumerate(issue.facts):
            if record.key == key and record.fact_type == fact_type:
                return index
        return -1

    def _remove_architecture_fact(self, key: str) -> None:
        for issue in self.issues:
            issue.facts = [record for record in issue.facts if not (record.fact_type == FACT_TYPE_ARCHITECTURE and record.key == key)]

    def find_fact(self, key: str, *, fact_type: Optional[str] = None, issue_id: str = "") -> Optional[IssueFactRecord]:
        normalized_key = str(key or "").strip()
        if not normalized_key:
            return None
        normalized_fact_type = _normalize_fact_type(fact_type) if fact_type else ""
        candidate: Optional[IssueFactRecord] = None
        for issue in self.issues:
            if issue_id and issue.issue_id != issue_id:
                continue
            for record in issue.facts:
                if record.key != normalized_key:
                    continue
                if normalized_fact_type and record.fact_type != normalized_fact_type:
                    continue
                clone = record.clone(issue_status=issue.status)
                if candidate is None or _record_sort_key(clone) >= _record_sort_key(candidate):
                    candidate = clone
        return candidate

    def upsert_fact(
        self,
        *,
        key: str,
        value: str,
        fact_type: str,
        source_action: str,
        updated_step: int,
        updated_run_id: int,
        issue_id: str = "",
        task_summary: str = "",
    ) -> IssueFactRecord:
        normalized_key = str(key or "").strip()
        normalized_value = str(value or "").strip()
        normalized_type = _normalize_fact_type(fact_type)
        if normalized_type == FACT_TYPE_GOAL:
            target_issue = self.get_issue(issue_id) if issue_id else None
            if target_issue is None:
                target_issue = self.ensure_goal_issue(task_summary=task_summary)
        else:
            target_issue = self.get_issue(issue_id) if issue_id else None
            if target_issue is None:
                target_issue = self.active_issue() or self._ensure_global_architecture_issue()
            self._remove_architecture_fact(normalized_key)

        record = IssueFactRecord(
            key=normalized_key,
            value=normalized_value,
            fact_type=normalized_type,
            issue_id=target_issue.issue_id,
            source_action=source_action,
            updated_step=int(updated_step or 0),
            updated_run_id=int(updated_run_id or 0),
            issue_status=target_issue.status,
        )
        existing_index = self._find_fact_index(target_issue, key=normalized_key, fact_type=normalized_type)
        if existing_index >= 0:
            target_issue.facts[existing_index] = record
        else:
            target_issue.facts.append(record)
        return record.clone(issue_status=target_issue.status)

    def active_context_records(self) -> List[IssueFactRecord]:
        active_issue = self.active_issue()
        architecture_records: List[IssueFactRecord] = []
        latest_architecture_by_key: Dict[str, IssueFactRecord] = {}
        for issue in self.issues:
            if active_issue is None and issue.status != ISSUE_STATUS_CLOSED:
                continue
            for record in issue.facts:
                if record.fact_type != FACT_TYPE_ARCHITECTURE:
                    continue
                candidate = record.clone(issue_status=issue.status)
                existing = latest_architecture_by_key.get(candidate.key)
                if existing is None or _record_sort_key(candidate) >= _record_sort_key(existing):
                    latest_architecture_by_key[candidate.key] = candidate
        architecture_records = sorted(latest_architecture_by_key.values(), key=lambda item: item.key)
        if active_issue is None:
            return architecture_records
        goal_records = [
            record.clone(issue_status=active_issue.status)
            for record in active_issue.facts
            if record.fact_type == FACT_TYPE_GOAL
        ]
        return architecture_records + sorted(goal_records, key=lambda item: item.key)

    def selected_context_records(self, keys: List[str]) -> List[IssueFactRecord]:
        selected = []
        allowed = {str(key or "").strip() for key in keys if str(key or "").strip()}
        if not allowed:
            return selected
        for record in self.active_context_records():
            if record.key in allowed:
                selected.append(record)
        return selected

    def records_by_run_scope(self, active_run_id: int) -> tuple[List[IssueFactRecord], List[IssueFactRecord]]:
        previous_run_records: List[IssueFactRecord] = []
        current_run_records: List[IssueFactRecord] = []
        for record in self.active_context_records():
            if int(record.updated_run_id or 0) == int(active_run_id or 0) and int(active_run_id or 0) > 0:
                current_run_records.append(record)
            else:
                previous_run_records.append(record)
        return previous_run_records, current_run_records
