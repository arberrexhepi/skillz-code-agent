import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from issue_facts import CompletedGoalCheckpoint, IssueFactLedger
from live_test_loop import TreeLoopPlannerWorker
from main import AgentConfig, WorkingFolderAgent, _handle_bridge_planner_action
from planner import DiscoveryResult, GoalExecutionResult, PlannerAgent


def make_planner(root, backend, *summaries):
    model = Mock(spec=["complete", "get_last_metrics"])
    model.get_last_metrics.return_value = {}
    model.complete.side_effect = [
        json.dumps({"action": {
            "type": "present_plan",
            "summary": summary,
            "goals": [
                {"goal_id": "prepare", "title": "Prepare the fixture", "goal": "Create the fixture"},
                {"goal_id": "verify", "title": "Verify the fixture", "goal": "Check the result",
                 "estimated_scope": "validation", "depends_on": ["prepare"],
                 "success_signals": ["The fixture opens successfully"]},
            ],
        }})
        for summary in summaries
    ]
    config = AgentConfig("gemini", "fixture", root, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
    factory = (
        lambda: TreeLoopPlannerWorker(model=model, root=root, verbose=False)
        if backend == "tree" else WorkingFolderAgent(model, config)
    )
    return PlannerAgent(model, config, factory, json.loads)


def action(planner, name, **fields):
    return _handle_bridge_planner_action(
        planner=planner, transcript=[], request={"action": name, **fields}, add_exchange=lambda *_: None,
    )


@pytest.mark.parametrize("backend", ["tree", "stable"])
def test_continue_after_restart_restores_exact_plan_and_awaits_approval(tmp_path, backend):
    planner = make_planner(tmp_path, backend, "Plan before stopping")
    issue_id = planner.worker.create_issue(request_summary="Build a fixture")["issue_id"]
    request = "Build the fixture described here.\n```json\n{\"name\": \"sample\"}\n```"
    planner.start_request(request)
    expected = planner.export_state()["pending_plan"]
    assert expected["original_request"] == request
    assert expected["goals"][1]["depends_on"] == ["prepare"]

    restarted = make_planner(tmp_path, backend)
    with patch.object(restarted, "_execute_plan", return_value="Executed saved plan") as execute:
        message = action(restarted, "continue_issue", issue_id=issue_id)
        assert "Plan before stopping" in message
        assert restarted.export_state()["pending_plan"] == expected
        restarted.model.complete.assert_not_called()
        execute.assert_not_called()

        assert action(restarted, "approve_plan", expected_plan=expected) == "Executed saved plan"
        execute.assert_called_once()
        assert restarted._plan_payload(execute.call_args.args[0]) == expected
        assert make_planner(tmp_path, backend).export_state()["pending_plan"] is None


def test_workspace_draft_can_be_reviewed_after_restart_and_rejection_consumes_it(tmp_path):
    planner = make_planner(tmp_path, "tree", "Unassigned draft")
    planner.start_request("Request before creating an issue")
    restarted = make_planner(tmp_path, "tree")
    assert restarted.export_state()["pending_plan"] == planner.export_state()["pending_plan"]
    assert not restarted.export_state()["active_issue_id"]
    action(restarted, "reject_plan", expected_plan=restarted.export_state()["pending_plan"])
    assert make_planner(tmp_path, "tree").export_state()["pending_plan"] is None


def test_each_issue_restores_its_own_plan_and_discovery(tmp_path):
    planner = make_planner(tmp_path, "tree", "First issue plan", "Second issue plan")
    first = planner.worker.create_issue(request_summary="First")["issue_id"]
    planner.session.active_issue_id = first
    planner.session.latest_request = "First"
    planner.session.last_discovery = DiscoveryResult(
        mode="quick", delegated_task="Find the fixture", final_message="Found fixtures/sample.json", ok=True,
    )
    planner._handle_intake_turn()
    first_plan = planner.export_state()["pending_plan"]
    second = planner.worker.create_issue(request_summary="Second", activate=False)["issue_id"]
    planner.continue_issue(second)
    second_plan = planner.export_state()["pending_plan"]

    restarted = make_planner(tmp_path, "tree")
    restarted.continue_issue(first)
    assert restarted.export_state()["pending_plan"] == first_plan
    assert restarted.session.last_discovery.final_message == "Found fixtures/sample.json"
    restarted.continue_issue(second)
    assert restarted.export_state()["pending_plan"] == second_plan
    restarted.model.complete.assert_not_called()


def test_stale_approval_cannot_consume_a_revised_draft(tmp_path):
    first = make_planner(tmp_path, "stable", "Old plan")
    issue_id = first.worker.create_issue(request_summary="Check the fixture")["issue_id"]
    first.start_request("Check the fixture")
    reviewed = first.export_state()["pending_plan"]
    second = make_planner(tmp_path, "stable", "Revised plan")
    action(second, "revise_plan", feedback="Also verify the schema", expected_plan=reviewed)
    with patch.object(first, "_execute_plan") as execute, pytest.raises(ValueError, match="saved plan changed"):
        action(first, "approve_plan", expected_plan=reviewed)
    execute.assert_not_called()
    assert "Revised plan" in action(first, "continue_issue", issue_id=issue_id)
    assert first.export_state()["pending_plan"] == second.export_state()["pending_plan"]
    first.model.complete.assert_called_once()


def test_closed_and_reopened_issue_does_not_resurrect_draft(tmp_path):
    planner = make_planner(tmp_path, "tree", "Draft to close")
    issue_id = planner.worker.create_issue(request_summary="Old work")["issue_id"]
    planner.start_request("Old work")
    planner.close_issue(issue_id)
    planner.reopen_issue(issue_id)
    assert IssueFactLedger.load(tmp_path / "repo_facts.md").pending_approval_for(issue_id) is None
    restarted = make_planner(tmp_path, "tree", "New plan after reopening")
    assert "New plan after reopening" in restarted.continue_issue(issue_id)


def test_saving_a_draft_does_not_overwrite_an_invalid_issue_ledger(tmp_path):
    ledger = tmp_path / "repo_facts.md"
    ledger.write_text("{incomplete JSON", encoding="utf-8")
    planner = make_planner(tmp_path, "tree", "A new draft")
    with pytest.raises(ValueError, match="repo_facts.md contains invalid JSON"):
        planner.start_request("Check the fixture")
    assert ledger.read_text(encoding="utf-8") == "{incomplete JSON"


@pytest.mark.parametrize("backend", ["tree", "stable"])
def test_issue_panel_continue_uses_approved_plan_after_runtime_restart(tmp_path, backend):
    planner = make_planner(tmp_path, backend, "Approved fixture plan")
    issue_id = planner.worker.create_issue(request_summary="Build a fixture")["issue_id"]
    planner.start_request("Create and verify a fixture")
    expected = planner.export_state()["pending_plan"]

    def execute_until_stopped(worker, plan, goal, index):
        if goal.goal_id == "verify":
            raise InterruptedError("Runtime stopped")
        return GoalExecutionResult(goal.goal_id, goal.title, goal.goal, "Fixture created", task_satisfied=True)

    with (
        patch.object(planner, "_execute_goal_with_worker", side_effect=execute_until_stopped),
        patch.object(planner, "_plan_next_goal_guidance", return_value=""),
        pytest.raises(InterruptedError, match="Runtime stopped"),
    ):
        action(planner, "approve_plan", expected_plan=expected)

    disk = tmp_path / "repo_facts.md"
    saved = IssueFactLedger.load(disk).get_issue(issue_id)
    assert saved.approved_plan["plan"] == expected
    assert saved.pending_approval is None
    assert [item.goal_id for item in saved.completed_goals] == ["prepare"]

    restarted = make_planner(tmp_path, backend)
    restarted.worker.create_issue(request_summary="An unrelated issue")

    def finish_remaining(worker, plan, goal, index):
        assert plan.summary == "Approved fixture plan"
        assert index == 2
        assert goal.goal_id == "verify"
        assert goal.depends_on == ["prepare"]
        assert goal.success_signals == ["The fixture opens successfully"]
        assert "Fixture created" in restarted._build_goal_task(plan, goal, index)
        return GoalExecutionResult(goal.goal_id, goal.title, goal.goal, "Fixture verified", task_satisfied=True)

    with (
        patch.object(restarted, "_execute_goal_with_worker", side_effect=finish_remaining) as execute,
        patch.object(restarted, "_synthesize_final_summary", return_value="Fixture complete"),
    ):
        assert f"Resuming checkpointed plan under {issue_id}" in action(restarted, "continue_issue", issue_id=issue_id)
    execute.assert_called_once()
    restarted.model.complete.assert_not_called()
    finished = IssueFactLedger.load(disk).get_issue(issue_id)
    assert finished.status == "closed"
    assert finished.approved_plan is None
    assert [item.goal_id for item in finished.completed_goals] == ["prepare", "verify"]


def test_legacy_issue_continuation_excludes_completed_goals_from_the_new_plan(tmp_path):
    planner = make_planner(tmp_path, "tree", "Original fixture plan")
    issue_id = planner.worker.create_issue(request_summary="Create and verify a fixture")["issue_id"]
    disk = tmp_path / "repo_facts.md"
    ledger = IssueFactLedger.load(disk)
    ledger.record_completed_goal(issue_id=issue_id, checkpoint=CompletedGoalCheckpoint(
        goal_id="prepare", title="Prepare the fixture", plan_summary="Original fixture plan",
        final_message="Fixture created on disk.", validation_summary="Confirmed sample.json exists",
    ))
    ledger.save(disk)

    message = action(planner, "continue_issue", issue_id=issue_id)
    assert "Carried forward 1 completed goal" in message
    plan = planner.session.pending_plan
    assert [goal.goal_id for goal in plan.goals] == ["verify"]
    assert plan.goals[0].depends_on == []
    assert "Fixture created on disk" in planner._build_goal_task(plan, plan.goals[0], 1)
    _, prompt = planner.model.complete.call_args.args
    assert "Plan only the unfinished outcomes" in prompt
    assert "Confirmed sample.json exists" in prompt


def test_issue_continuation_reconciles_uncheckpointed_changes_before_planning(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "sample.json").write_text('{"name": "sample"}', encoding="utf-8")
    planner = make_planner(tmp_path, "tree", "Finish remaining fixture work")
    issue_id = planner.worker.create_issue(request_summary="Create and verify a fixture")["issue_id"]
    worker_result = SimpleNamespace(ok=True, final_message="sample.json already contains the requested fixture. Verification still needs to run.")

    with patch.object(planner, "_run_worker_task", return_value={
        "run_result": worker_result, "history_slice": [], "elapsed": 0,
    }) as discover:
        message = action(planner, "continue_issue", issue_id=issue_id)

    assert "Finish remaining fixture work" in message
    assert "?? sample.json" in discover.call_args.kwargs["task"]
    assert "staged and unstaged diff" in discover.call_args.kwargs["task"]
    assert "?? repo_facts.md" not in discover.call_args.kwargs["task"]
    assert "Verification still needs to run" in planner.model.complete.call_args.args[1]
