"""Proposal persistence is the scope boundary, not the user's later decision."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from issue_facts import IssueFactLedger
from issue_proposals import IssueProposalStore
from live_test_loop import TreeLoopPlannerWorker
from main import ActionResult, WorkingFolderAgent, _handle_bridge_planner_action
from tree_commands import CommandResult
from tree_loop import TreeLoop, extract_commands


DIAGNOSTIC = {"id": "run-old", "status": "open", "file": "legacy.ts", "line": "4", "code": "TS2339", "message": "Property missing"}
ACTION = {"summary": "Legacy type error", "reason": "Outside the requested panel change", "evidence": "Typecheck reports TS2339 in legacy.ts:4", "run_issue_ids": ["run-old"]}


def propose(store, action=None, scope="issue-1", diagnostics=None):
    return store.propose(action or ACTION, diagnostics=diagnostics or [DIAGNOSTIC], scope=scope, parent_issue_id="issue-1", goal="Improve panel")


def test_durable_immediate_deferral_and_scope_isolation(tmp_path):
    proposal = propose(IssueProposalStore(tmp_path))
    store = IssueProposalStore(tmp_path)
    diagnostics = [{**DIAGNOSTIC, "id": "run-reingested", "source": "another check"}, {**DIAGNOSTIC, "message": "Different error"}]
    values = store.apply_deferrals(diagnostics, scope="issue-1")
    assert values[0]["status"] == "deferred"
    assert values[0]["proposal_id"] == proposal["proposal_id"]
    assert values[1]["status"] == "open"
    assert store.apply_deferrals(diagnostics, scope="another-task")[0]["status"] == "open"
    assert diagnostics[0]["status"] == "open"  # projection does not rewrite evidence


@pytest.mark.parametrize("decision", ["accept", "ignore"])
def test_decisions_are_idempotent_and_do_not_reopen_gates(tmp_path, decision):
    store = IssueProposalStore(tmp_path)
    proposal = propose(store)
    creator = Mock(return_value="issue-2")
    decided = store.decide(proposal["proposal_id"], decision, creator)
    assert store.decide(proposal["proposal_id"], decision, creator) == decided
    assert creator.call_count == (1 if decision == "accept" else 0)
    assert store.apply_deferrals([DIAGNOSTIC], scope="issue-1")[0]["status"] == "deferred"
    assert propose(store)["proposal_id"] == proposal["proposal_id"]
    assert len(store.snapshot()) == 1
    if decision == "accept":
        assert store.apply_deferrals([DIAGNOSTIC], scope="issue-1", active_issue_id="issue-2")[0]["status"] == "open"


def test_invalid_ids_or_failed_writes_never_authorize_deferral(tmp_path):
    store = IssueProposalStore(tmp_path)
    with pytest.raises(ValueError, match="Unknown run"):
        propose(store, {**ACTION, "run_issue_ids": ["invented"]})
    with patch.object(store, "_save", side_effect=OSError("disk full")), pytest.raises(OSError):
        propose(store)
    assert store.snapshot() == []
    assert store.apply_deferrals([DIAGNOSTIC], scope="issue-1")[0]["status"] == "open"


def test_corrupt_storage_is_not_overwritten(tmp_path):
    store = IssueProposalStore(tmp_path)
    store.path.write_text("broken")
    with pytest.raises(ValueError):
        propose(store)
    assert store.path.read_text() == "broken"


def test_concurrent_proposals_do_not_lose_records(tmp_path):
    def add(index):
        return propose(IssueProposalStore(tmp_path), {**ACTION, "summary": f"Finding {index}", "run_issue_ids": []})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(add, range(12)))
    assert len(IssueProposalStore(tmp_path).snapshot()) == 12


def worker_with_diagnostics(tmp_path, *, validated=True, mixed=False):
    worker = TreeLoopPlannerWorker(model=Mock(), root=tmp_path, verbose=False)
    worker._current_task = "Improve panel"
    worker._has_mutation = True
    worker._validation_after_mutation = validated
    worker._emit_step_progress = Mock()
    worker._run_post_write_validation = Mock(return_value=False)
    content = "legacy.ts(4,1): error TS2339: Property missing"
    if mixed:
        content += "\npanel.ts(10,1): error TS2322: Current change is broken"
    worker.loop.bridge.tree.ingest_diagnostic_content(content, source_path="[run-check:typecheck]")
    return worker


def observe_failure(worker):
    result = CommandResult(ok=False, output=f"issues={len(worker._proposal_diagnostics())}\nexit_code=2", command_type="mutation", needs_tool=True, tool_action={"type": "run_check"})
    worker._observe_command_result("run-check typecheck", result)
    return result


def dispatch_proposal(worker, diagnostics=None):
    ids = [item["id"] for item in (diagnostics or worker._proposal_diagnostics())]
    command = "propose-issue " + json.dumps({**ACTION, "run_issue_ids": ids})
    assert worker.loop.bridge.is_tree_command(command)
    assert command in extract_commands(command)
    result = worker.loop.bridge.execute(command)[0]
    assert result.needs_tool
    result.output = worker._dispatch_tool_action(result)
    worker._observe_command_result(command, result)
    return result


def finish(worker):
    result = CommandResult(ok=True, output="", command_type="finish", needs_tool=True, tool_action={"type": "finish", "message": "Done"})
    result.output = worker._dispatch_tool_action(result)
    return result


def test_official_proposal_restores_focused_validation_before_user_decision(tmp_path):
    worker = worker_with_diagnostics(tmp_path)
    observe_failure(worker)
    assert not worker._validation_after_mutation
    assert worker._discovery_remediation
    assert dispatch_proposal(worker).ok
    assert worker.proposal_state()["proposals"][0]["status"] == "proposed"
    assert worker._discovery_remediation is None
    assert worker._validation_after_mutation
    assert worker._pending_verification is None
    assert not worker._completion_check_pending
    assert "COMPLETION CHECK FORMAT" not in worker._compose_steering()
    assert worker.loop._signal_state["unresolved_issue_count"] == 0
    assert finish(worker).ok
    # A clean later check may replace transient diagnostics; the final report
    # must still disclose the outstanding, separately recorded finding.
    worker.loop.bridge.tree.ingest_diagnostic_content("", source_path="focused check")
    validation = worker._finalize_validation(SimpleNamespace(finished=True))
    assert validation.passed
    assert "Broader checks may still fail" in validation.summary


def test_missing_current_change_validation_is_not_waived(tmp_path):
    worker = worker_with_diagnostics(tmp_path, validated=False)
    observe_failure(worker)
    assert dispatch_proposal(worker).ok
    assert worker._discovery_remediation is None
    assert not worker._validation_after_mutation
    assert not finish(worker).ok


def test_identical_failure_never_reopens_gate_but_changed_failure_does(tmp_path):
    worker = worker_with_diagnostics(tmp_path)
    observe_failure(worker)
    assert dispatch_proposal(worker).ok
    assert not observe_failure(worker).ok  # raw check did NOT pass
    assert worker._validation_after_mutation
    assert worker._pending_verification is None
    worker.loop.bridge.tree.ingest_diagnostic_content("legacy.ts(4,1): error TS2339: New failure", source_path="new check")
    observe_failure(worker)
    assert not worker._validation_after_mutation
    assert worker._pending_verification


def test_mixed_diagnostics_keep_in_scope_failure_blocking(tmp_path):
    worker = worker_with_diagnostics(tmp_path, mixed=True)
    observe_failure(worker)
    unrelated = [item for item in worker._proposal_diagnostics() if item["file"] == "legacy.ts"]
    assert dispatch_proposal(worker, unrelated).ok
    assert not worker._validation_after_mutation
    assert worker._pending_verification
    assert not finish(worker).ok
    assert worker.loop._signal_state["unresolved_issue_count"] == 1


def test_failed_proposal_dispatch_does_not_clear_validation_or_remediation(tmp_path):
    worker = worker_with_diagnostics(tmp_path)
    observe_failure(worker)
    with patch.object(IssueProposalStore, "_save", side_effect=OSError("disk full")):
        assert not dispatch_proposal(worker).ok
    assert worker._discovery_remediation
    assert not worker._validation_after_mutation


def test_later_unstructured_failure_cannot_be_cleared_by_an_earlier_proposal(tmp_path):
    worker = worker_with_diagnostics(tmp_path)
    observe_failure(worker)
    failure = CommandResult(ok=False, output="process crashed", command_type="mutation", tool_action={"type": "run_shell"})
    worker._observe_command_result("shell focused check", failure)
    assert dispatch_proposal(worker).ok
    assert not worker._validation_after_mutation
    assert worker._pending_verification["source"] == "run_shell"


def test_deferral_does_not_hide_same_diagnostic_in_a_different_goal(tmp_path):
    worker = worker_with_diagnostics(tmp_path)
    assert dispatch_proposal(worker).ok
    assert worker._proposal_diagnostics()[0]["status"] == "deferred"
    worker._current_task = "Repair the legacy type error"
    assert worker._proposal_diagnostics()[0]["status"] == "open"


@pytest.mark.parametrize("worker_type", [WorkingFolderAgent, TreeLoopPlannerWorker])
def test_user_acceptance_promotes_inactive_issue_and_ignore_removes_suggestion(tmp_path, worker_type):
    worker = worker_type.__new__(worker_type)
    worker.root = tmp_path
    worker.issue_ledger = IssueFactLedger.empty()
    active = worker.issue_ledger.create_issue(request_summary="Current work", activate=True)
    worker._current_task = "Improve panel"
    worker._active_run_id = 8
    worker._repo_facts_loaded_count = 0
    worker._persist_repo_facts()
    # Classic worker intentionally has no active_issue_id attribute.
    assert worker._proposal_scope().startswith("issue:" + active.issue_id + ":task:")
    proposal = propose(worker._proposal_store(), scope=worker._proposal_scope())
    planner = SimpleNamespace(worker=worker)
    exchange = Mock()
    for _ in range(2):
        message = _handle_bridge_planner_action(planner=planner, request={"action": "accept_issue_proposal", "proposal_id": proposal["proposal_id"]}, transcript=[], add_exchange=exchange)
        assert "without changing the active task" in message
    assert worker.issue_ledger.active_issue_id == active.issue_id
    assert len(worker.issue_ledger.issues) == 2
    added = worker.issue_ledger.issues[1]
    assert added.source == "model_proposal:" + proposal["proposal_id"]
    assert added.status == "open"
    assert "Outside the requested" in added.source_excerpt
    assert worker.proposal_state()["proposals"] == []
    exchange.assert_not_called()


def test_standalone_loop_restores_deferred_status_after_restart(tmp_path):
    loop = TreeLoop(model=Mock(), workspace_root=tmp_path, verbose=False)
    loop._conversation_task = "Improve panel"
    loop.setup()
    loop.bridge.tree.ingest_diagnostic_content("legacy.ts(4,1): error TS2339: Property missing", source_path="first")
    command = "propose-issue " + json.dumps({**ACTION, "run_issue_ids": [item["id"] for item in loop.bridge.tree.list_log_issues()]})
    result = loop.bridge.execute(command)[0]
    assert "Recorded" in loop._execute_tool(result)
    fresh = TreeLoop(model=Mock(), workspace_root=tmp_path, verbose=False)
    fresh._conversation_task = "Improve panel"
    fresh.setup()
    fresh.bridge.tree.ingest_diagnostic_content("legacy.ts(4,1): error TS2339: Property missing", source_path="restarted")
    assert fresh.bridge.tree.list_log_issues()[0]["status"] == "deferred"
    detail = fresh.bridge.tree.format_log_issue_detail(fresh.bridge.tree.list_log_issues()[0])
    assert "next_reads" not in detail


def test_classic_worker_drops_deferred_error_context_and_does_not_reopen_it(tmp_path):
    worker = WorkingFolderAgent.__new__(WorkingFolderAgent)
    worker.root = tmp_path
    worker._current_task = "Improve panel"
    worker.active_error = None
    worker.history = []
    worker._set_active_context_item = Mock()
    worker._remove_active_context_item = Mock()
    worker._add_active_note = Mock()
    action = {"type": "diagnose", "path": "legacy.ts"}
    result = ActionResult(ok=False, name="diagnose", payload={"diagnostics": [DIAGNOSTIC], "message": "Existing type error"})
    worker._set_active_error(action, result)
    ids = [item["id"] for item in worker._proposal_diagnostics()]
    worker.propose_issue({**ACTION, "run_issue_ids": ids})
    assert worker.active_error is None
    worker._set_active_error(action, result)
    assert worker.active_error is None
    assert not result.ok
    changed = ActionResult(ok=False, name="diagnose", payload={"diagnostics": [{**DIAGNOSTIC, "message": "New error"}]})
    worker._set_active_error(action, changed)
    assert worker.active_error is not None


def test_rejected_acceptance_leaves_pending_suggestion_and_deferral(tmp_path):
    store = IssueProposalStore(tmp_path)
    proposal = propose(store)
    with pytest.raises(OSError):
        store.decide(proposal["proposal_id"], "accept", Mock(side_effect=OSError("read-only facts")))
    assert store.snapshot()[0]["status"] == "proposed"
    assert store.apply_deferrals([DIAGNOSTIC], scope="issue-1")[0]["status"] == "deferred"
