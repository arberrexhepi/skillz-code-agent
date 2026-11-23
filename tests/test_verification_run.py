import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from issue_facts import CompletedGoalCheckpoint
from live_test_loop import TreeLoopPlannerWorker
from main import AgentConfig, WorkingFolderAgent
from planner import PlannerAgent, PlannerGoal, PlannerPlan
from tests.test_tree_loop_messages import RecordingMessageModel
from verification_repair import MAX_INVESTIGATION_TURNS, MAX_REPAIR_TURNS
from verification_run import MAX_VERIFICATION_TOOL_CALLS, VerificationRun, all_required_checks_passed


MCP = {"type": "mcp", "operation": "call", "server": "blender", "name": "execute"}
REQUIREMENTS = ["Rig opens without missing objects", "Animation plays across 120 frames"]
BROWSER_REQUIREMENT = "Playwright MCP confirms Project Home and the Editor Viewport workspace render cleanly"
BROWSER_STEPS = [
    {"type": "assert-visible", "label": "Project Home", "selector": "#home"},
    {"type": "click", "label": "Open project", "selector": "#open-project"},
    {"type": "assert-visible", "label": "Editor Viewport", "selector": "#viewport"},
]


def verify(*ids):
    return f"verify {','.join(ids)} mcp {json.dumps(MCP)}"


def repair_handoff(targets, diagnosis="The failing diagnostic identifies this declaration."):
    return "finish " + json.dumps({"targets": targets, "diagnosis": diagnosis})


def result_payload(verdicts, **payload):
    text = "VERIFICATION_RESULTS: " + json.dumps(verdicts)
    return {"ok": True, "result": {"content": [{"type": "text", "text": json.dumps({
        "status": "ok", "returncode": 0, "stdout": text, **payload,
    })}]}}


def tree_worker(tmp_path, monkeypatch, responses, *, limit=30):
    monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
    model = RecordingMessageModel(responses)
    return TreeLoopPlannerWorker(model=model, root=tmp_path, max_turns=limit, verbose=False)


def planner_goal(worker, tmp_path):
    planner = PlannerAgent(RecordingMessageModel([]), SimpleNamespace(root=tmp_path, provider="gemini"),
                           lambda: worker, json.loads)
    goal = PlannerGoal(goal_id="verify", title="Verify the rig", goal="Verify the shipped rig",
                       reason="Check the Blender deliverable", estimated_scope="validation", success_signals=REQUIREMENTS)
    plan = PlannerPlan(summary="Verify the rig", goals=[goal], clarification_summary="", original_request="Verify the rig.")
    return planner, plan, goal


def test_tree_planner_carries_evidence_through_compaction_and_checkpoints(tmp_path, monkeypatch):
    first = result_payload({"check-1": {"status": "pass", "evidence": "No missing objects observed"}})
    second = result_payload({"check-2": {"status": "pass", "evidence": "Measured 120 animated frames"}})
    worker = tree_worker(tmp_path, monkeypatch, [
        verify("check-1"), "mcp {\"operation\":\"servers\"}", verify("check-2"),
    ])
    worker.loop._conversation_char_budget = 1
    planner, plan, goal = planner_goal(worker, tmp_path)
    with patch("live_test_loop.invoke_mcp", side_effect=[first, {"ok": True, "result": []}, second]):
        result = planner._execute_goal_with_worker(worker, plan, goal, 1)
    assert result.status == "completed", result.final_message
    assert result.validation_passed
    assert result.verification["turns_used"] == 3
    assert all_required_checks_passed(REQUIREMENTS, result.verification)
    assert "No missing objects observed" in json.dumps(worker.model.calls[-1])
    assert worker.loop._conversation_compaction_count > 0
    assert worker._verification is None

    checkpoint = CompletedGoalCheckpoint.from_dict({
        "goal_id": goal.goal_id, "title": goal.title, "goal_signature": planner._goal_signature(goal),
        "verification": result.verification, "validation_summary": result.validation_summary,
    }).to_dict()
    with patch.object(planner, "_issue_completed_goal_checkpoints", return_value=[checkpoint]):
        assert planner._checkpoint_results_for_plan(plan)[0].verification == result.verification
        goal.success_signals = [*REQUIREMENTS, "Retargeted pose matches reference"]
        assert planner._checkpoint_results_for_plan(plan) == []
        goal.success_signals = REQUIREMENTS
        del checkpoint["verification"]
        assert planner._checkpoint_results_for_plan(plan) == []


def test_tree_verification_retries_failure_and_preserves_blocked_check(tmp_path, monkeypatch):
    masked_failure = result_payload({"check-1": {"status": "pass", "evidence": "false pass"}},
                                    stderr="Traceback (most recent call last):\nAttributeError: Missing rig")
    blocked = result_payload({"check-2": {
        "status": "blocked", "evidence": "Source clip download timed out", "next_step": "Attach the source clip.",
    }})
    worker = tree_worker(tmp_path, monkeypatch, [
        verify("check-1"), verify("check-1"), verify("check-1"), verify("check-2"),
    ])
    planner, plan, goal = planner_goal(worker, tmp_path)
    with patch("live_test_loop.invoke_mcp", side_effect=[masked_failure, masked_failure, blocked]) as invoke:
        result = planner._execute_goal_with_worker(worker, plan, goal, 1)
    assert invoke.call_count == 3
    assert result.status == "failed" and result.validation_ran
    first, second = result.verification["checks"]
    assert (first["status"], first["attempts"], first["evidence"]) == ("fail", 2, "AttributeError: Missing rig")
    assert second["status"] == "blocked"
    assert "Attach the source clip." in planner._synthesize_final_summary(plan, [result])
    assert "Attach the source clip." in planner.session.last_next_steps
    planner.session.last_completed_results = [result]
    review = planner._review_completed_plan(plan)
    assert review.decision == "rejected" and not review.retryable
    assert "Attach the source clip." in review.required_actions


def test_strategy_supports_binding_one_tool_to_multiple_checks(tmp_path, monkeypatch):
    worker = tree_worker(tmp_path, monkeypatch, [f"s1: {verify('check-1', 'check-2')}, finish Verified both"])
    worker.configure_verification(REQUIREMENTS)
    outcomes = {f"check-{i}": {"status": "pass", "evidence": requirement} for i, requirement in enumerate(REQUIREMENTS, 1)}
    with patch("live_test_loop.invoke_mcp", return_value=result_payload(outcomes)) as invoke:
        result = worker.run_task("Check both requirements")
    assert invoke.call_count == 1
    assert result.validation_passed, result.final_message


def test_tree_ignores_unwrapped_checks_and_unsupported_completion_claim(tmp_path, monkeypatch):
    worker = tree_worker(tmp_path, monkeypatch, [f"mcp {json.dumps(MCP)}", "finish All checks passed"])
    planner, plan, goal = planner_goal(worker, tmp_path)
    with patch("live_test_loop.invoke_mcp") as invoke:
        result = planner._execute_goal_with_worker(worker, plan, goal, 1)
    invoke.assert_not_called()
    assert result.status == "failed" and not result.validation_ran
    assert result.final_message.startswith("Verification: 0/2 required checks passed.")
    assert [check["status"] for check in result.verification["checks"]] == ["blocked", "blocked"]


def test_tree_preview_setup_preserves_browser_check_attempts(tmp_path, monkeypatch):
    worker = tree_worker(tmp_path, monkeypatch, [
        "artifact-preview start",
        "verify check-1 artifact-preview start",
        "verify check-1 run-route-check /",
    ])
    worker.configure_verification(["The preview route loads without browser errors"])
    with (
        patch("artifact_preview._start", return_value={"ok": True, "url": "http://127.0.0.1:4173"}) as start,
        patch.object(worker, "_exec_run_route_check", return_value="url=http://127.0.0.1:4173\nissues=0"),
    ):
        result = worker.run_task("Start the preview and inspect its home route")

    assert result.ok
    assert start.call_count == 2
    assert result.verification["tool_calls_used"] == 1
    assert result.verification["checks"][0]["attempts"] == 1


def test_stable_preview_setup_preserves_browser_check_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
    config = AgentConfig("gemini", "fixture", tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
    preview = {"type": "artifact_preview"}
    worker = WorkingFolderAgent(RecordingMessageModel([]), config)
    worker.configure_verification(["The home screen renders"])
    with patch("artifact_preview._start", return_value={"ok": True, "url": "http://127.0.0.1:4173"}) as start:
        assert worker._execute_action(preview).ok
        assert worker._execute_action({"type": "verify", "checks": ["check-1"], "action": preview}).ok
    assert start.call_count == 2
    assert worker._verification.snapshot()["checks"][0]["attempts"] == 0


def test_tree_interactive_browser_check_requires_an_observed_workflow(tmp_path, monkeypatch):
    action = {"route": "/", "steps": BROWSER_STEPS}
    worker = tree_worker(tmp_path, monkeypatch, [
        "verify check-1 run-route-check /",
        "verify check-1 artifact-preview browser-check " + json.dumps(action),
        "verify check-1 artifact-preview browser-check " + json.dumps(action),
    ])
    with (
        patch("artifact_preview._request", side_effect=[
            {"ok": True, "observations": [BROWSER_STEPS[0], BROWSER_STEPS[2]]},
            {"ok": True, "observations": BROWSER_STEPS},
        ]) as request,
        patch.object(worker, "_exec_run_route_check") as route_check,
    ):
        worker.configure_verification([BROWSER_REQUIREMENT])
        result = worker.run_task("Open a project and inspect the viewport")

    route_check.assert_not_called()
    request.assert_called_with({"operation": "browser-check", "input": action})
    assert result.ok, result.final_message
    check = result.verification["checks"][0]
    assert check["attempts"] == 2
    assert check["browser_observations"] == BROWSER_STEPS
    assert all_required_checks_passed([BROWSER_REQUIREMENT], result.verification)
    del check["browser_observations"]
    assert not all_required_checks_passed([BROWSER_REQUIREMENT], result.verification)


def test_workspace_playwright_verdict_can_include_observed_steps():
    run = VerificationRun([BROWSER_REQUIREMENT], managed_browser=False)
    action = {"type": "mcp", "operation": "call", "server": "playwright", "name": "browser_run_code"}
    ids = run.begin_checks(["check-1"], action=action)
    output = result_payload({"check-1": {
        "status": "pass", "evidence": "The Editor Viewport appeared after opening a project.",
        "browser_observations": BROWSER_STEPS,
    }})
    run.record(ids, kind="mcp", ok=True, output=output, action=action)
    assert all_required_checks_passed([BROWSER_REQUIREMENT], run.snapshot())


@pytest.mark.parametrize("exit_code", [0, 1])
def test_stable_approved_build_uses_real_shell_result_once_without_stdout_marker(tmp_path, monkeypatch, exit_code):
    monkeypatch.setenv("SHELL_ACCESS", "true")
    config = AgentConfig("gemini", "fixture", tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
    worker = WorkingFolderAgent(RecordingMessageModel([]), config)
    worker.configure_verification(["npx tsc --noEmit and npm run build pass with 0 errors"])
    action = {"type": "verify", "checks": ["check-1"], "action": {"type": "run_shell", "command": "npx tsc --noEmit && npm run build"}}
    with patch.object(worker, "_call_tool", return_value={"ok": True, "data": {"returncode": exit_code, "stdout": "Build output"}}) as invoke:
        first = worker._execute_action(action)
        retry = worker._execute_action(action)
    assert first.payload["verification"]["passed"] is (exit_code == 0)
    assert not retry.ok
    assert invoke.call_count == 1
    assert worker._verification.checks["check-1"].attempts == 1


@pytest.mark.parametrize(("requirement", "command", "exit_code", "passed"), [
    ("npm run build exits with code 0", "npm run build", 0, True),
    ("npm run build exits with code 0", "npm run build", 1, False),
    ("npm run build exits with code 0", "npm run build || true", 0, False),
    ("npm run build exits with code 0", "npm run lint", 0, False),
    ("npm run build emits the required artifacts", "npm run build", 0, False),
])
def test_exit_code_only_proves_an_exact_approved_exit_requirement(requirement, command, exit_code, passed):
    run = VerificationRun([requirement])
    action = {"type": "run_shell", "command": command}
    ids = run.begin_checks(["check-1"], action=action)
    run.record(ids, kind="run_shell", ok=True, output={"returncode": exit_code, "stdout": "Build output"}, action=action)
    assert run.passed is passed


def test_continuous_mode_stops_after_incomplete_verification(tmp_path, monkeypatch):
    worker = tree_worker(tmp_path, monkeypatch, [verify("check-1"), "finish Confirm the connection before playback."])
    planner, plan, _ = planner_goal(worker, tmp_path)
    plan.not_in_scope = ["Implementation changes"]

    def present_plan():
        planner.session.pending_plan = plan
        return "Plan ready"

    issue = {"request_summary": "Verify the rig", "plan_summary": "Verify the rig"}
    unavailable = result_payload({"check-1": {"status": "blocked", "evidence": "Connection timed out"}})
    with (
        patch.object(planner, "_create_or_select_continuous_issue", return_value=issue),
        patch.object(planner, "_handle_intake_turn", side_effect=present_plan) as intake,
        patch("live_test_loop.invoke_mcp", return_value=unavailable) as invoke,
    ):
        report = planner.start_continuous(max_cycles=4)
    assert intake.call_count == invoke.call_count == 1
    assert "Continuous mode stopped: review_rejected." in report
    assert planner.session.execution_paused


def test_new_evidence_resets_idle_budget_but_does_not_extend_turn_budget(tmp_path, monkeypatch):
    requirement_count = 13
    worker = tree_worker(tmp_path, monkeypatch, [verify(f"check-{i}") for i in range(1, requirement_count + 1)])
    worker.configure_verification([f"Verify item {i}" for i in range(1, requirement_count + 1)])
    outcomes = [result_payload({f"check-{i}": {"status": "pass", "evidence": f"Verified item {i}"}})
                for i in range(1, requirement_count + 1)]
    with patch("live_test_loop.invoke_mcp", side_effect=outcomes) as invoke:
        result = worker.run_task("Verify all items")
    assert invoke.call_count == 12
    assert "verification turn budget was exhausted" in result.final_message
    assert result.verification["turns_without_new_evidence"] == 0
    assert result.verification["checks"][-1]["status"] == "blocked"
    assert not result.ok


def test_check_attempt_and_overall_tool_limits():
    run = VerificationRun([f"Check item {i}" for i in range(MAX_VERIFICATION_TOOL_CALLS)])
    calls = ["check-1"] * 3 + [f"check-{i}" for i in range(2, MAX_VERIFICATION_TOOL_CALLS - 1)]
    for index, key in enumerate(calls):
        selected = run.begin_checks([key])
        run.record(selected, kind="mcp", ok=False, output=f"Attempt {index} failed")
    assert "tool-call budget" in run.stop_reason
    with pytest.raises(ValueError, match="tool-call budget"):
        run.begin_checks([f"check-{MAX_VERIFICATION_TOOL_CALLS}"])
    assert run.checks["check-1"].attempts == 3


def test_evidence_turn_resets_idle_counter_and_failed_provider_keeps_last_verdict(tmp_path, monkeypatch):
    worker = tree_worker(tmp_path, monkeypatch, ["cat /repo/notes.txt"] * 4 + [verify("check-1")])
    (tmp_path / "notes.txt").write_text("Check the rig.")
    worker.configure_verification(REQUIREMENTS)
    passed = result_payload({"check-1": {"status": "pass", "evidence": "Opened file and found the rig"}})
    with patch("live_test_loop.invoke_mcp", return_value=passed):
        result = worker.run_task("Verify both requirements")
    assert result.verification["turns_used"] == 6
    assert [check["status"] for check in result.verification["checks"]] == ["pass", "blocked"]
    assert "Worker execution failed" in result.final_message


def test_tree_stops_when_reads_do_not_produce_new_evidence(tmp_path, monkeypatch):
    (tmp_path / "notes.txt").write_text("Check the rig.")
    worker = tree_worker(tmp_path, monkeypatch, ["cat /repo/notes.txt"] * 6)
    worker.configure_verification(REQUIREMENTS)
    result = worker.run_task("Verify both requirements")
    assert len(worker.model.calls) == 5
    assert not result.ok and not result.validation_ran
    assert "five turns without new check evidence" in result.final_message


def test_shell_heredoc_uses_only_stdout_for_verdicts(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL_ACCESS", "true")
    evidence = {"check-1": {"status": "pass", "evidence": "Found frames in fixture"}}
    line = "VERIFICATION_RESULTS: " + json.dumps(evidence)
    worker = tree_worker(tmp_path, monkeypatch, [
        f"s1: verify check-1 shell cat >&2 <<'PROBE'\n{line}\nPROBE",
        f"s1: verify check-1 shell cat <<'PROBE'\n{line}\nPROBE",
    ])
    worker.configure_verification(["Look for frames"])
    result = worker.run_task("Check frames")
    assert result.ok, result.final_message
    assert result.verification["checks"][0]["attempts"] == 2


def test_stable_worker_returns_partial_verdicts_with_model_next_steps(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
    responses = [
        {"action": {"type": "verify", "checks": ["check-1"], "action": MCP}},
        {"action": {"type": "drop_context", "reason": "test compaction"}},
        {"action": {"type": "finish", "message": "Use a local playback session to confirm the frame span."}},
    ]
    model = RecordingMessageModel([json.dumps(response) for response in responses])
    config = AgentConfig("gemini", "fixture", tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
    worker = WorkingFolderAgent(model, config)
    planner, plan, goal = planner_goal(worker, tmp_path)
    passed = result_payload({"check-1": {"status": "pass", "evidence": "Opened file and found the rig"}})
    with patch("main.invoke_mcp", return_value=passed):
        result = planner._execute_goal_with_worker(worker, plan, goal, 1)
    assert result.status == "failed", result.final_message
    assert result.validation_ran
    assert [check["status"] for check in result.verification["checks"]] == ["pass", "blocked"]
    assert "Opened file and found the rig" in json.dumps(model.calls[-1])
    assert "Use a local playback session" in result.final_message


@pytest.mark.parametrize("build_passes_after_repair", [True, False])
def test_resumed_final_verification_dispatches_scoped_repair_and_rechecks_every_requirement(
    tmp_path, monkeypatch, build_passes_after_repair,
):
    app = tmp_path / "App.tsx"
    app.write_text("const bad = ;\n")
    excluded = tmp_path / "Other.tsx"
    excluded.write_text("const neverChange = true;\n")
    verify_both = "verify check-1 run-check typecheck\nverify check-2 run-check build\nfinish Checked both requirements"
    batch = {
        "type": "batch_mutate", "atomic": True,
        "operations": [
            {"type": "replace_regex", "path": app.name, "pattern": "= ;", "replacement": "= 1;"},
            {"type": "replace_regex", "path": excluded.name, "pattern": "true", "replacement": "false"},
        ],
    }
    forbidden = "mutate " + json.dumps(batch)
    batch["operations"].pop()
    allowed = "mutate " + json.dumps(batch)
    responses = [
        verify_both,
        "write /repo/App.tsx must never execute during investigation",
        "cat /repo/App.tsx\n" + repair_handoff(["/repo/App.tsx"]),
        forbidden, allowed, "cat /repo/App.tsx\nfinish Repaired source", verify_both,
    ]
    if not build_passes_after_repair:
        responses.append(repair_handoff([], "The remaining build failure needs a separate change."))
    worker = tree_worker(tmp_path, monkeypatch, responses)
    planner, plan, verifier = planner_goal(worker, tmp_path)
    verifier.success_signals = ["Typecheck passes", "Production build passes"]
    implementation = PlannerGoal("app", "Fix syntax in App.tsx", "Fix App syntax", "Build requires it", estimated_scope="write")
    unrelated = PlannerGoal("docs", "Update docs", "Update docs", "Document the workflow", estimated_scope="write")
    verifier.depends_on = [implementation.goal_id, unrelated.goal_id]
    plan.goals = [implementation, unrelated, verifier]
    plan.not_in_scope = [
        "Changing Other.tsx or UI behavior beyond the syntax repair in App.tsx",
        "Adding new features or refactoring unrelated components in App.tsx",
    ]
    issue = worker.ensure_issue_for_plan(original_request=plan.original_request, plan_summary=plan.summary)
    for index, goal in enumerate([implementation, unrelated], 1):
        worker.record_completed_goal(
            issue_id=issue["issue_id"], plan_summary=plan.summary, original_index=index,
            total_goal_count=3, goal={"goal_id": goal.goal_id, "title": goal.title, "goal_signature": planner._goal_signature(goal)},
            result={"final_message": "Completed earlier."},
        )

    observed = []

    def run_check(action):
        changed = app.read_text() == "const bad = 1;\n"
        observed.append((action["kind"], changed))
        if changed and (action["kind"] == "typecheck" or build_passes_after_repair):
            return "exit_code=0\nissues=0"
        output = "exit_code=1\nissues=1\nstdout:\nApp.tsx(1,13): error TS1128: Declaration or statement expected."
        if action["kind"] == "build":
            output += "\nOther.tsx(1,1): error TS2786: 'App' cannot be used as a JSX component."
        return output

    with (
        patch.object(worker, "_exec_run_check", side_effect=run_check),
        patch.object(planner, "_planner_complete", return_value="{}"),
    ):
        planner._resume_approved_issue(planner._plan_snapshot(plan), issue["issue_id"])

    assert observed == [("typecheck", False), ("build", False), ("typecheck", True), ("build", True)]
    assert app.read_text() == "const bad = 1;\n"
    assert excluded.read_text() == "const neverChange = true;\n"
    assert "App.tsx(1,13): error TS1128" in json.dumps(worker.model.calls[1])
    assert "Other.tsx(1,1): error TS2786" in json.dumps(worker.model.calls[1])
    assert "Verification investigation is read-only" in json.dumps(worker.model.calls[2])
    assert "Use direct edits to these existing repair files only" in json.dumps(worker.model.calls[4])
    previous_app, previous_docs, result = planner.session.last_completed_results
    assert [previous_app.goal_id, previous_docs.goal_id] == ["app", "docs"]
    assert previous_app.final_message == previous_docs.final_message == "Completed earlier."
    assert all_required_checks_passed(verifier.success_signals, result.verification) is build_passes_after_repair
    assert planner.session.execution_paused is (not build_passes_after_repair)
    assert "Repair round 1 edited App.tsx" in result.final_message
    assert result.verification["repair_rounds"][0]["changed_paths"] == ["App.tsx"]
    assert worker._verification_repair_paths is None
    assert not worker.model.responses


@pytest.mark.parametrize("scope_note", ["", "Modifying src/* files", "Modifying src/app.ts"])
def test_repair_uses_checkpointed_mutations_and_has_a_fixed_budget(tmp_path, monkeypatch, scope_note):
    source = tmp_path / "src"
    source.mkdir()
    (source / "app.ts").write_text("const x = ;\n")
    failure = "verify check-1 run-check typecheck\nfinish Typecheck failed"
    worker = tree_worker(tmp_path, monkeypatch, [
        failure, repair_handoff(["src/app.ts"]), *(["cat /repo/src/app.ts"] * MAX_REPAIR_TURNS),
    ])
    planner, plan, verifier = planner_goal(worker, tmp_path)
    verifier.success_signals = ["Typecheck passes"]
    implementation = PlannerGoal("app", "Fix the source", "Restore compilation", "Build requires it", estimated_scope="write")
    plan.goals.insert(0, implementation)
    if scope_note:
        plan.not_in_scope = [scope_note]
    checkpoint = CompletedGoalCheckpoint.from_execution(
        plan_summary=plan.summary, goal={**planner._goal_payload(implementation), "goal_signature": planner._goal_signature(implementation)},
        result={"mutation_paths": ["src/app.ts"]}, original_index=1, total_goal_count=2,
    ).to_dict()
    with (
        patch.object(planner, "_issue_completed_goal_checkpoints", return_value=[checkpoint]),
        patch.object(worker, "_exec_run_check", return_value="exit_code=1\nissues=1\nsrc/app.ts(1,11): error TS1109: Expression expected."),
    ):
        planner._execute_plan(plan, checkpoint_results=planner._checkpoint_results_for_plan(plan), resumed=True)

    assert len(worker.model.calls) == 1 + (0 if scope_note else 1 + MAX_REPAIR_TURNS)
    assert (source / "app.ts").read_text() == "const x = ;\n"
    assert planner.session.execution_paused
    assert ("left the source unchanged" in planner.session.paused_failure_reason) is (not scope_note)


@pytest.mark.parametrize("outcome", ["passed", "exhausted", "repeated", "out_of_scope"])
def test_verification_repair_loop_stops_at_success_repetition_or_two_rounds(tmp_path, monkeypatch, outcome):
    app = tmp_path / "App.tsx"
    app.write_text("const value = 0;\n")
    consumer = tmp_path / "Consumer.tsx"
    consumer.write_text("import App from './App';\n")
    verify_both = "verify check-1 run-check typecheck\nverify check-2 run-check build\nfinish Checks completed"
    responses = [verify_both]
    rounds = {"out_of_scope": 0, "repeated": 1}.get(outcome, 2)
    if outcome == "out_of_scope":
        responses.append(repair_handoff(["Consumer.tsx"]))
    for revision in range(1, rounds + 1):
        responses += [
            "cat /repo/App.tsx\n" + repair_handoff(["App.tsx"], f"The consumer error originates in App, revision {revision}."),
            f"write /repo/App.tsx const value = {revision};\ncat /repo/App.tsx\nfinish Repaired",
            verify_both,
        ]
    worker = tree_worker(tmp_path, monkeypatch, responses)
    planner, plan, verifier = planner_goal(worker, tmp_path)
    verifier.success_signals = ["Typecheck passes", "Build passes"]
    implementation = PlannerGoal("app", "Implement App.tsx", "Implement the component", "Build requires it", estimated_scope="write")
    plan.goals.insert(0, implementation)
    checkpoint = CompletedGoalCheckpoint.from_execution(
        plan_summary=plan.summary, goal={**planner._goal_payload(implementation), "goal_signature": planner._goal_signature(implementation)},
        result={"mutation_paths": ["App.tsx"]}, original_index=1, total_goal_count=2,
    ).to_dict()
    calls = []

    def run_check(action):
        revision = int(app.read_text().split("=")[1].strip().rstrip(";"))
        calls.append((revision, action["kind"]))
        if revision == 2 and outcome == "passed":
            return "exit_code=0\nissues=0"
        evidence = "App returns void." if outcome == "repeated" else f"App return mismatch at revision {revision}."
        return f"exit_code=1\nissues=1\nConsumer.tsx({revision + 1},1): error TS2786: {evidence}"

    with (
        patch.object(planner, "_issue_completed_goal_checkpoints", return_value=[checkpoint]),
        patch.object(planner, "_planner_complete", return_value="{}"),
        patch.object(worker, "_exec_run_check", side_effect=run_check),
    ):
        planner._execute_plan(plan, checkpoint_results=planner._checkpoint_results_for_plan(plan), resumed=True)

    assert calls == [(revision, kind) for revision in range(rounds + 1) for kind in ("typecheck", "build")]
    result = planner.session.last_completed_results[-1]
    assert len(result.verification["repair_rounds"]) == max(rounds, 1)
    assert not worker.model.responses
    assert all_required_checks_passed(verifier.success_signals, result.verification) is (outcome == "passed")
    assert planner.session.execution_paused is (outcome != "passed")
    if outcome == "repeated":
        assert "reproduced the same failures" in result.final_message
    elif outcome == "exhausted":
        assert "paused after 2 repair rounds" in result.final_message
    elif outcome == "out_of_scope":
        assert "outside the approved repair scope" in result.final_message


def test_verification_investigation_obeys_its_read_only_budget(tmp_path, monkeypatch):
    app = tmp_path / "App.tsx"
    app.write_text("const value = 0;\n")
    worker = tree_worker(tmp_path, monkeypatch, ["cat /repo/App.tsx"] * (MAX_INVESTIGATION_TURNS + 1))
    worker.configure_verification_repair(frozenset())

    result = worker.run_task("Investigate the compiler failure")

    assert len(worker.model.calls) == MAX_INVESTIGATION_TURNS
    assert not result.ok
    assert app.read_text() == "const value = 0;\n"


@pytest.mark.parametrize("grounded_handoff", [False, True])
def test_verifier_can_repair_an_approved_html_target_that_was_untouched_in_the_original_goal(tmp_path, monkeypatch, grounded_handoff):
    index = tmp_path / "index.html"
    index.write_text('<link rel="stylesheet" href="https://example.test/fonts.css">\n')
    (tmp_path / "style.css").write_text("body { margin: 0; }\n")
    (tmp_path / "contracts.ts").write_text("export const schema = {}\n")
    verify_route = "verify check-1 run-route-check /\nfinish Inspected preview"
    diagnosis = "The stylesheet request originates in index.html. Remove the external font link."
    initial_check = "verify check-1 run-route-check /\ncat /repo/index.html\nfinish " + json.dumps({
        "check_ids": ["check-1"], "targets": ["index.html" if grounded_handoff else "style.css"], "diagnosis": diagnosis,
    })
    worker = tree_worker(tmp_path, monkeypatch, [
        initial_check,
        *([] if grounded_handoff else ["cat /repo/index.html\n" + repair_handoff(["index.html"], diagnosis)]),
        "write /repo/index.html <!doctype html><title>Studio</title>\nshell npm run build",
        "cat /repo/index.html\nfinish Uses the bundled style",
        verify_route,
    ])
    planner, plan, verifier = planner_goal(worker, tmp_path)
    verifier.success_signals = ["The preview route loads without browser errors"]
    implementation = PlannerGoal(
        "design", "Redesign the studio", "Redesign style.css (and index.html if needed for typography) using contracts.ts.",
        "Polish the studio", estimated_scope="write",
    )
    plan.goals.insert(0, implementation)
    checkpoint = CompletedGoalCheckpoint.from_execution(
        plan_summary=plan.summary, goal={**planner._goal_payload(implementation), "goal_signature": planner._goal_signature(implementation)},
        result={"mutation_paths": ["style.css"]}, original_index=1, total_goal_count=2,
    ).to_dict()

    def route_check(*_args):
        failed = "fonts.css" in index.read_text()
        return f"url=http://127.0.0.1:4173/\nissues={int(failed)}\nrequest_failures={int(failed)}"

    with (
        patch.object(planner, "_issue_completed_goal_checkpoints", return_value=[checkpoint]),
        patch.object(planner, "_planner_complete", return_value="{}"),
        patch.object(worker, "_exec_run_route_check", side_effect=route_check),
    ):
        planner._execute_plan(plan, checkpoint_results=planner._checkpoint_results_for_plan(plan), resumed=True)

    assert not planner.session.execution_paused
    result = planner.session.last_completed_results[-1]
    repair = result.verification["repair_rounds"][0]
    assert repair["changed_paths"] == ["index.html"]
    assert repair["diagnosis_source"] == ("verifier" if grounded_handoff else "investigation")
    assert "Uses the bundled style" in repair["worker_result"]
    assert len(worker.model.calls) == (4 if grounded_handoff else 5)
    if not grounded_handoff:
        assert "Approved repair files: index.html, style.css" in json.dumps(worker.model.calls[1])
    assert "fonts.css" not in index.read_text()


def test_stable_repair_can_finish_after_undispatched_build_and_source_review(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
    (tmp_path / "index.html").write_text("<title>Old title</title>\n")
    actions = [
        {"type": "write_file", "path": "index.html", "content": "<title>Studio</title>\n"},
        {"type": "run_shell", "command": "npm run build"},
        {"type": "read_file", "path": "index.html"},
        {"type": "finish", "message": "Repaired the title."},
    ]
    model = RecordingMessageModel([json.dumps({"action": action}) for action in actions])
    config = AgentConfig("gemini", "fixture", tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
    worker = WorkingFolderAgent(model, config)
    worker.configure_verification_repair(frozenset({"index.html"}))

    with patch.object(worker, "_handle_run_shell_action") as shell:
        result = worker.run_task("Repair index.html and review the edited result")

    shell.assert_not_called()
    assert result.ok, result.final_message
    assert result.final_message == "Repaired the title."
    assert worker.history[-3].result.payload["code"] == "VERIFICATION_REPAIR_ACTION_SKIPPED"
    assert worker.active_error is None


def test_stable_verification_investigator_can_report_after_failed_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
    app = tmp_path / "app.py"
    app.write_text("value =\n")
    handoff = {"targets": ["app.py"], "diagnosis": "The assignment has no value."}
    responses = [
        {"action": {"type": "write_file", "path": "app.py", "content": "value = 1\n"}},
        {"action": {"type": "diagnose", "path": "app.py"}},
        {"action": {"type": "finish", "message": json.dumps(handoff)}},
    ]
    model = RecordingMessageModel([json.dumps(response) for response in responses])
    config = AgentConfig("gemini", "fixture", tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
    worker = WorkingFolderAgent(model, config)
    worker.configure_verification_repair(frozenset())

    result = worker.run_task("Investigate the failure")

    assert json.loads(result.final_message) == handoff
    assert app.read_text() == "value =\n"
    write, diagnose, finish = worker.history[-3:]
    assert write.action["type"] == "write_file" and not write.result.ok
    assert "Verification investigation is read-only" in json.dumps(write.result.payload)
    assert diagnose.action["type"] == "diagnose" and not diagnose.result.ok
    assert finish.result.ok


def test_verification_compiler_locations_survive_bounded_shell_evidence():
    run = VerificationRun(["Typecheck passes"])
    ids = run.begin_checks(["check-1"])
    output = "compiler headers\n" * 70 + "\x1b[31msrc/App.tsx(1895,1): error TS1128: Declaration or statement expected.\x1b[0m"
    check = run.record(ids, kind="run_shell", ok=False, output={"returncode": 1, "stdout": output})["checks"][0]
    assert check["status"] == "fail"
    assert check["source_errors"][0]["path"] == "src/App.tsx"
    assert check["evidence"].startswith("src/App.tsx(1895,1): error TS1128")


def test_long_typescript_diagnostic_keeps_the_actionable_continuation():
    run = VerificationRun(["Build passes"])
    ids = run.begin_checks(["check-1"])
    output = (
        "src/App.tsx(1040,9): error TS2322: Type '{ "
        + ("longProperty: string; " * 40)
        + "}' is not assignable to type 'Take'.\n"
        "  Property 'renderStyle' is missing in the constructed take.\n"
        "src/main.tsx(5,72): error TS2786: 'App' cannot be used as a JSX component."
    )
    check = run.record(ids, kind="run_check", ok=False, output=output)["checks"][0]
    assert [error["path"] for error in check["source_errors"]] == ["src/App.tsx", "src/main.tsx"]
    assert "Property 'renderStyle' is missing" in check["evidence"]
