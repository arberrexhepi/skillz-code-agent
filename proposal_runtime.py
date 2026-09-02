"""Shared suggestion handling for classic and beta planner workers."""
import hashlib
import json

from issue_facts import IssueFactLedger
from issue_proposals import IssueProposalStore, diagnostic_fingerprint


class ProposalRuntimeMixin:
    def _proposal_store(self):
        return IssueProposalStore(self.root)

    def _proposal_active_issue_id(self):
        ledger = getattr(self, "issue_ledger", None)
        return str(getattr(ledger, "active_issue_id", "") or getattr(self, "active_issue_id", "") or "")

    def _proposal_scope(self):
        issue_id = self._proposal_active_issue_id()
        task = str(getattr(self, "_current_task", "") or "")
        task_scope = "task:" + hashlib.sha256(task.encode()).hexdigest()
        return "issue:" + issue_id + ":" + task_scope if issue_id else task_scope

    def proposal_state(self):
        try:
            return {"proposals": [item for item in self._proposal_store().snapshot() if item["status"] == "proposed"]}
        except Exception as exc:
            return {"proposals": [], "error": str(exc)}

    def _proposal_diagnostics(self):
        loop = getattr(self, "loop", None)
        if loop is not None:
            return loop.bridge.tree.list_log_issues()
        error = getattr(self, "active_error", None)
        return [{**item, "id": "run-" + diagnostic_fingerprint(item)[:16]} for item in (getattr(error, "diagnostics", None) or [])]

    def _apply_proposal_deferrals(self, diagnostics):
        return self._proposal_store().apply_deferrals(diagnostics, scope=self._proposal_scope(), active_issue_id=self._proposal_active_issue_id())

    def _proposal_deferral_summary(self):
        proposals = [item for item in self._proposal_store().snapshot()
                     if self._proposal_scope() in item["scopes"] and item.get("accepted_issue_id") != self._proposal_active_issue_id()]
        if not proposals:
            return ""
        return f" {len(proposals)} unrelated finding(s) recorded separately ({', '.join(item['proposal_id'] for item in proposals)}); not current-goal gates. Broader checks may still fail."

    def propose_issue(self, action):
        proposal = self._proposal_store().propose(action, diagnostics=self._proposal_diagnostics(), scope=self._proposal_scope(),
            parent_issue_id=self._proposal_active_issue_id(), goal=str(getattr(self, "_current_task", "") or ""))
        refresh = getattr(self, "_refresh_proposal_deferrals", None)
        if callable(refresh):
            refresh()
        return proposal

    def decide_issue_proposal(self, proposal_id, decision):
        def create(proposal):
            reload_facts = getattr(self, "_reload_repo_facts", None) or getattr(self, "_load_repo_facts_into_map", None)
            if callable(reload_facts):
                reload_facts()
            issue = self.create_issue(request_summary=proposal["summary"], plan_summary=proposal["summary"],
                source="model_proposal:" + proposal["proposal_id"], parent_issue_id=proposal["parent_issue_id"],
                source_excerpt=json.dumps({key: proposal[key] for key in ("reason", "evidence", "paths", "goal")}), activate=False)
            issue_id = str(issue.get("issue_id") or "")
            # Existing fact writers can swallow I/O errors. Do not acknowledge an
            # acceptance until the promoted issue is actually on disk.
            saved = IssueFactLedger.load(self._repo_facts_path()).get_issue(issue_id) if issue_id else None
            if saved is None or saved.source != "model_proposal:" + proposal["proposal_id"]:
                raise OSError("Could not persist the accepted issue. The suggestion remains pending.")
            return issue_id
        return self._proposal_store().decide(proposal_id, decision, create)
