"""Durable, non-executable agent suggestions and diagnostic deferral records."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import RLock
from typing import Any, Callable
from uuid import uuid4

_LOCK = RLock()
PROPOSALS_FILENAME = ".agent-issue-proposals.json"
PROPOSAL_GUIDANCE = """Scope boundary: unrelated findings must not expand the current goal.
Use propose-issue {"summary":"...","reason":"Why outside this goal","evidence":"Already observed evidence","run_issue_ids":["run-..."]} to persist an agent suggestion and defer the linked diagnostics immediately.
Use paths for findings without run diagnostics. Do not investigate further just to write a suggestion. Do not invent evidence.
Once proposed, those findings are not validation or completion gates, even while awaiting user acceptance or after Ignore. Do not re-read, reclassify, fix, or resolve them for this goal. Continue focused validation of the requested change; do not claim the broader failing check passed.
Only the user can accept or ignore proposals. Proposals are not executable issues."""


def diagnostic_fingerprint(issue: dict) -> str:
    # Ignore transient ids, ingestion source, counts, and timestamps. Changed
    # message/location is new evidence and must not inherit an old deferral.
    fields = {key: str(issue.get(key) or "") for key in ("file", "path", "line", "column", "code", "message", "summary", "classification", "route")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()


class IssueProposalStore:
    def __init__(self, root: Path):
        self.path = Path(root) / PROPOSALS_FILENAME

    def snapshot(self) -> list[dict]:
        if self.path.is_symlink():
            raise ValueError("Issue proposal storage must not be a symlink")
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or not isinstance(payload.get("proposals"), list):
            raise ValueError("Invalid issue proposal storage")
        return payload["proposals"]

    @contextmanager
    def _transaction(self):
        lock_path = self.path.with_suffix(".lock")
        with _LOCK:
            if lock_path.is_symlink():
                raise ValueError("Issue proposal lock must not be a symlink")
            with lock_path.open("a+b") as lock:
                if os.name == "nt":
                    import msvcrt
                    lock.write(b"0")
                    lock.flush()
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    yield self.snapshot()
                finally:
                    if os.name == "nt":
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(lock, fcntl.LOCK_UN)

    def _save(self, proposals: list[dict]) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".issue-proposals-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as output:
                json.dump({"version": 1, "proposals": proposals}, output, indent=2)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def propose(self, action: dict, *, diagnostics: list[dict], scope: str, parent_issue_id: str, goal: str) -> dict:
        text = {}
        for key, limit in (("summary", 500), ("reason", 4000), ("evidence", 12000)):
            value = action.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise ValueError(f"propose_issue requires {key} (1–{limit} characters)")
            text[key] = value.strip()
        ids = action.get("run_issue_ids", [])
        paths = action.get("paths", [])
        if not isinstance(ids, list) or not all(isinstance(item, str) for item in ids) or len(ids) > 30:
            raise ValueError("run_issue_ids must be a list of at most 30 diagnostic ids")
        if not isinstance(paths, list) or not all(isinstance(item, str) for item in paths) or len(paths) > 30:
            raise ValueError("paths must be a list of at most 30 paths")
        indexed = {item["id"]: item for item in diagnostics}
        if any(item not in indexed for item in ids):
            raise ValueError("Unknown run diagnostic id; use the ids in the current diagnostic evidence")
        linked = [deepcopy(indexed[item]) for item in dict.fromkeys(ids)]
        fingerprints = sorted(diagnostic_fingerprint(item) for item in linked)
        identity = fingerprints or [text["summary"], text["evidence"], sorted(paths)]
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        with self._transaction() as proposals:
            existing = next((item for item in proposals if item["fingerprint"] == fingerprint), None)
            if existing:
                if scope not in existing["scopes"]:
                    existing["scopes"].append(scope)
                    self._save(proposals)
                return deepcopy(existing)
            proposal = {
                "proposal_id": "proposal-" + uuid4().hex[:12], "status": "proposed", "author": "agent",
                **text, "paths": list(dict.fromkeys(paths + [str(item.get("file") or item.get("path") or "") for item in linked if item.get("file") or item.get("path")])),
                "parent_issue_id": parent_issue_id, "goal": goal, "scopes": [scope], "diagnostics": linked,
                "diagnostic_fingerprints": fingerprints, "fingerprint": fingerprint,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            proposals.append(proposal)
            self._save(proposals)  # Only a successful durable write authorizes deferral.
            return deepcopy(proposal)

    def decide(self, proposal_id: str, decision: str, creator: Callable[[dict], str]) -> dict:
        if decision not in {"accept", "ignore"}:
            raise ValueError("Choose accept or ignore")
        with self._transaction() as proposals:
            proposal = next((item for item in proposals if item["proposal_id"] == proposal_id), None)
            if proposal is None:
                raise ValueError("Proposal not found")
            target = "accepted" if decision == "accept" else "ignored"
            if proposal["status"] == target:
                return deepcopy(proposal)
            if proposal["status"] != "proposed":
                raise ValueError("This proposal has already been decided")
            if decision == "accept":
                proposal["accepted_issue_id"] = creator(deepcopy(proposal))
            proposal["status"] = target
            proposal["decided_at"] = datetime.now(timezone.utc).isoformat()
            self._save(proposals)
            return deepcopy(proposal)

    def apply_deferrals(self, diagnostics: list[dict], *, scope: str, active_issue_id: str = "") -> list[dict]:
        matches = {}
        for proposal in self.snapshot():
            if scope in proposal["scopes"] and (not active_issue_id or proposal.get("accepted_issue_id") != active_issue_id):
                for fingerprint in proposal["diagnostic_fingerprints"]:
                    matches[fingerprint] = proposal["proposal_id"]
        output = []
        for original in diagnostics:
            item = dict(original)
            proposal_id = matches.get(diagnostic_fingerprint(item))
            if proposal_id and item.get("status") != "resolved":
                item.update(status="deferred", proposal_id=proposal_id)
            output.append(item)
        return output
