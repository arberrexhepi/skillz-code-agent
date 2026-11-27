import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from issue_facts import CompletedGoalCheckpoint, IssueFactLedger
from live_test_loop import TreeLoopPlannerWorker
from main import ActionResult, AgentConfig, WorkingFolderAgent, _handle_bridge_planner_action
from planner import GoalExecutionResult, PlannerAgent, PlannerGoal, PlannerPlan
from tests.test_tree_loop_messages import RecordingMessageModel
from verification_repair import MAX_INVESTIGATION_TURNS, MAX_REPAIR_TURNS
from verification_handoff import GoalVerificationFacts, inherited_verification_checks
from verification_run import MAX_VERIFICATION_SETUP_CALLS, MAX_VERIFICATION_TOOL_CALLS, VerificationRun, all_required_checks_passed
from verification_session import MAX_MCP_VERIFICATION_CALLS, MAX_MCP_VERIFICATION_TURNS


MCP = {"type": "mcp", "operation": "call", "server": "blender", "name": "execute"}
REQUIREMENTS = ["Rig opens without missing objects", "Animation plays across 120 frames"]
PREVIEW_URL = "http://127.0.0.1:4173/"
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


def playwright(name, **arguments):
    return {"type": "mcp", "operation": "call", "server": "playwright", "name": name, "arguments": arguments}


def playwright_output(text):
    return {"ok": True, "result": {"content": [{"type": "text", "text": text}]}}


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


@pytest.mark.parametrize("backend", ["tree", "stable"])
def test_verifier_reuses_tool_backed_facts_and_checks_only_remaining_requirements(backend, tmp_path, monkeypatch):
    requirements = ["npm run build exits with code 0", "Animation plays across 120 frames"]
    (tmp_path / "src.ts").write_text("export const duration = 120;\n")
    finish = "Build passed and the desktop preview opened."
    finding = "Preview opened; the full animation still needs to be measured."
    assertion = result_payload({"check-2": {"status": "pass", "evidence": "Observed all 120 frames."}})
    if backend == "tree":
        worker = tree_worker(tmp_path, monkeypatch, [
            "shell npm run build", f"fact issue-001/verification/preview-coverage {finding}",
            "finish " + finish, verify("check-2"),
        ])
        build = patch.object(worker, "_exec_run_shell", return_value="exit_code=0\nstdout:\nBuilt")
        mcp_target = "live_test_loop.invoke_mcp"
    else:
        monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
        actions = [
            {"type": "run_shell", "command": ["npm", "run", "build"]},
            {"type": "set_fact", "fact_type": "verification", "key": "preview-coverage", "value": finding},
            {"type": "finish", "message": finish},
            {"type": "verify", "checks": ["check-2"], "action": MCP},
        ]
        model = RecordingMessageModel([json.dumps({"action": action}) for action in actions])
        config = AgentConfig("gemini", "fixture", tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
        worker = WorkingFolderAgent(model, config)
        build = patch.object(worker, "_handle_run_shell_action",
                             return_value=ActionResult(True, "run_shell", {"returncode": 0, "stdout": "Built"}))
        mcp_target = "main.invoke_mcp"
    planner, plan, verifier = planner_goal(worker, tmp_path)
    verifier.success_signals = requirements
    plan.goals.insert(0, PlannerGoal("build", "Build the app", "Run the production build", "Prepare preview", estimated_scope="execute"))
    verifier.depends_on = ["build"]
    with (
        build as build_tool,
        patch(mcp_target, return_value=assertion) as mcp,
        patch.object(planner, "_planner_complete", return_value='{"summary":"Checks passed.","next_steps":[]}'),
    ):
        planner._execute_plan(plan, checkpoint_results=[], resumed=False)
    assert build_tool.call_count == mcp.call_count == 1
    result = planner.session.last_completed_results[-1]
    assert result.status == "completed", result.final_message
    assert result.verification["checks"][0]["reused_from"] == ["Build the app"]
    assert result.verification["tool_calls_used"] == 1
    assert finding in json.dumps(worker.model.calls[-1])
    ledger = IssueFactLedger.load(tmp_path / "repo_facts.md")
    facts = ledger.get_issue("issue-001").facts
    assert any(fact.fact_type == "verification" and fact.key == "preview-coverage" for fact in facts)
    assert any(fact.fact_type == "verification" and fact.source_action == "verification_tool" for fact in facts)
    inherited = inherited_verification_checks(tmp_path, requirements[:1], [fact.to_dict() for fact in facts])
    worker.configure_verification(requirements[:1], inherited_checks=inherited)
    prior_calls = len(worker.model.calls)
    reused = worker.run_task("Verify the production build.")
    assert reused.ok and reused.verification["tool_calls_used"] == 0
    assert len(worker.model.calls) == prior_calls


def test_inherited_verification_requires_current_source_and_tool_evidence(tmp_path):
    source = tmp_path / "app.ts"
    source.write_text("export const current = 1;\n")
    requirement = "npm run build exits with code 0"
    recorder = GoalVerificationFacts(tmp_path, [requirement], "Build")
    action = {"type": "run_shell", "command": "npm run build"}
    recorder.observe(action, True, "exit_code=0", recorder.before(action))
    fact, = recorder.facts.values()
    assert inherited_verification_checks(tmp_path, [requirement], [dict(fact, source_action="set_fact")]) == {}
    saved = [dict(fact, source_action="verification_tool")]
    assert inherited_verification_checks(tmp_path, [requirement], saved)[requirement]["goals"] == ["Build"]
    source.write_text("export const current = 2;\n")
    assert inherited_verification_checks(tmp_path, [requirement], saved) == {}


def test_inherited_browser_check_uses_exact_requirement_and_observations(tmp_path):
    recorder = GoalVerificationFacts(tmp_path, [BROWSER_REQUIREMENT], "Exercise browser")
    action = playwright("browser_evaluate", function="() => checkWorkspace()")
    payload = {"verification_results": {"check-1": {
        "status": "pass", "evidence": "Home and viewport rendered.", "browser_observations": BROWSER_STEPS,
    }}}
    output = playwright_output(f"### Result\n{json.dumps(payload)}\n### Page\n- Page URL: {PREVIEW_URL}")
    recorder.observe(action, True, output, recorder.before(action))
    facts = [dict(fact, source_action="verification_tool") for fact in recorder.facts.values()]
    assert inherited_verification_checks(tmp_path, [BROWSER_REQUIREMENT + " after reload"], facts) == {}
    inherited = inherited_verification_checks(tmp_path, [BROWSER_REQUIREMENT], facts)
    run = VerificationRun([BROWSER_REQUIREMENT], inherited_checks=inherited)
    assert all_required_checks_passed([BROWSER_REQUIREMENT], run.snapshot(final=True))
    assert run.snapshot()["tool_calls_used"] == 0


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


def test_tree_verification_stops_at_repeated_observed_failure_and_preserves_blocker(tmp_path, monkeypatch):
    failure = result_payload({"check-1": {"status": "fail", "evidence": "Missing rig"}})
    blocked = result_payload({"check-2": {
        "status": "blocked", "evidence": "Source clip download timed out", "next_step": "Attach the source clip.",
    }})
    worker = tree_worker(tmp_path, monkeypatch, [
        verify("check-1"), verify("check-1"), verify("check-1"), verify("check-2"),
    ])
    planner, plan, goal = planner_goal(worker, tmp_path)
    with patch("live_test_loop.invoke_mcp", side_effect=[failure, failure, blocked]) as invoke:
        result = planner._execute_goal_with_worker(worker, plan, goal, 1)
    assert invoke.call_count == 3
    assert result.status == "failed" and result.validation_ran
    first, second = result.verification["checks"]
    assert (first["status"], first["attempts"], first["evidence"]) == ("fail", 2, "Missing rig")
    assert first["stop_reason"] == "The same failure was observed again."
    assert result.verification["stop_reason"] == "Verification stopped after repeated failures."
    assert second["status"] == "blocked"
    assert "Attach the source clip." in planner._synthesize_final_summary(plan, [result])
    assert "Attach the source clip." in planner.session.last_next_steps
    planner.session.last_completed_results = [result]
    review = planner._review_completed_plan(plan)
    assert review.decision == "rejected" and not review.retryable
    assert "Attach the source clip." in review.required_actions


def test_custom_script_error_cannot_be_reported_as_pass():
    run = VerificationRun(REQUIREMENTS)
    selected = run.begin_checks(["check-1"])
    output = result_payload({"check-1": {"status": "pass", "evidence": "false pass"}},
                            stderr="Traceback (most recent call last):\nAttributeError: Missing rig")
    run.record(selected, kind="mcp", ok=True, output=output)
    check = run.snapshot()["checks"][0]
    assert check["status"] == "unverified"
    assert check["evidence"] == "Verifier error: AttributeError: Missing rig"


@pytest.mark.parametrize("tail", [', "diagnostics": "very large browser output', ', "check-3": {"status": "unver'])
def test_truncated_playwright_result_retains_progress_without_crediting_a_pass(tail):
    action = playwright("browser_evaluate", function="() => reportProgress()")
    run = VerificationRun(REQUIREMENTS, managed_browser=False)
    ids = run.begin_checks(["check-1", "check-2"], action=action)
    complete_map = json.dumps({
        "check-1": {"status": "unverified", "evidence": "Fixture has a six-second timeline.",
                    "progress": {"milestone": "fixture created", "evidence": "Six-second shot available."},
                    "next_step": "Sample animation during playback."},
        "check-2": {"status": "pass", "evidence": "Partial response also emitted a pass."},
    })
    if tail.startswith(', "check-3"'):
        complete_map = complete_map.removesuffix("}")
    output = playwright_output('### Result\n{"verification_results": ' + complete_map + tail)
    state = run.record(ids, kind="mcp", ok=True, output=output, action=action)
    first, second = state["checks"]
    assert first["progress_evidence"] == "Six-second shot available."
    assert first["next_step"] == "Sample animation during playback."
    assert second["status"] == "unverified"
    assert "incomplete or invalid assertion JSON" in second["evidence"]
    assert "incomplete or invalid JSON" in state["result_format_issue"]


@pytest.mark.parametrize("backend", ["tree", "stable"])
def test_verifier_can_record_issue_facts_and_ground_repair_handoff(backend, tmp_path, monkeypatch):
    (tmp_path / "App.tsx").write_text("const clock = 0;\n")
    failure = result_payload({"check-1": {"status": "fail", "evidence": "Viewport reset to frame zero."}})
    handoff = {"check_ids": ["check-1"], "targets": ["App.tsx"], "diagnosis": "Restore the authoritative clock."}
    if backend == "tree":
        worker = tree_worker(tmp_path, monkeypatch, [
            verify("check-1") + "\n"
            "fact issue-001/verification/viewport-clock The mode switch reset playback.\n"
            'read-context /repo/App.tsx "const clock" before=0 after=0 limit=1\n'
            "finish " + json.dumps(handoff)
        ])
        mcp_target = "live_test_loop.invoke_mcp"
    else:
        monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
        actions = [
            {"type": "verify", "checks": ["check-1"], "action": MCP},
            {"type": "set_fact", "fact_type": "verification", "key": "viewport-clock",
             "value": "The mode switch reset playback."},
            {"type": "read_file", "path": "App.tsx"},
            {"type": "finish", "message": json.dumps(handoff)},
        ]
        config = AgentConfig("gemini", "fixture", tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
        worker = WorkingFolderAgent(RecordingMessageModel([json.dumps({"action": action}) for action in actions]), config)
        mcp_target = "main.invoke_mcp"
    worker.ensure_issue_for_plan(original_request="Test viewport clock", plan_summary="Inspect transport")
    worker.configure_verification(["Viewport retains transport time", "Animation renders while playing"])
    with patch(mcp_target, return_value=failure):
        result = worker.run_task("Verify the viewport.")
    assert not result.ok
    assert result.verification["repair_handoff"] == handoff
    assert result.verification["checks"][1]["status"] == "not_run"
    assert "handed off check-1" in result.verification["stop_reason"]
    ledger = IssueFactLedger.load(tmp_path / "repo_facts.md")
    assert ledger.find_fact("viewport-clock", fact_type="verification", issue_id="issue-001")


def test_tree_missing_browser_verdicts_do_not_trigger_source_repairs(tmp_path, monkeypatch):
    browser = json.dumps({"operation": "call", "server": "playwright", "name": "browser_evaluate"})
    worker = tree_worker(tmp_path, monkeypatch, [
        verify("check-1"),
        *[f"verify check-2 mcp {browser}"] * 5,
    ])
    planner, plan, goal = planner_goal(worker, tmp_path)
    pass_result = result_payload({"check-1": {"status": "pass", "evidence": "Rig opened"}})
    raw_probe = {"ok": True, "result": {"content": [{"type": "text", "text": '{"clipCount":2}'}]}}
    with patch("live_test_loop.invoke_mcp", side_effect=[pass_result, *[raw_probe] * 5]):
        result = planner._execute_goal_with_worker(worker, plan, goal, 1)
    with patch.object(planner, "_verification_repair_scope", return_value={"src/App.tsx"}), \
         patch.object(planner, "_run_verification_work") as repair:
        planner._repair_failed_verification(plan, goal, 1, result)
    repair.assert_not_called()
    assert "per-check attempt limit" in result.verification["stop_reason"]
    passed, unresolved = result.verification["checks"]
    assert passed["status"] == "pass"
    assert unresolved["status"] == "unverified"
    assert unresolved["attempts"] == 3
    assert "MCP checkpoint without new evidence" in unresolved["stop_reason"]
    assert result.verification["turns_without_new_evidence"] == 3


def test_malformed_nested_mcp_retries_as_literal_javascript_before_dispatch(tmp_path, monkeypatch):
    script = r"() => /matrix\([^,]+,\s*[^0]/.test(document.body.style.transform)"
    header = '{"operation":"call","server":"playwright","name":"browser_evaluate"}'
    sibling = f"s2: {verify('check-2')}"
    malformed = f"s1: verify check-1 mcp {header[:-1]},\"arguments\":{{\"function\":\"{script}\"}}}}\n{sibling}"
    corrected = (
        ">>th: Run both checks with literal script text.\n>>pl: Probe the UI and measure the frames.\n"
        "s1: cat /repo/notes.txt\n"
        f"s1 -> s1.1: verify check-1 mcp {header} <<<\n{script}\n>>>\n{sibling}"
    )
    (tmp_path / "notes.txt").write_text("UI probe prerequisites.")
    worker = tree_worker(tmp_path, monkeypatch, [malformed, corrected])
    worker.configure_verification(REQUIREMENTS)
    outputs = {
        "browser_evaluate": result_payload({"check-1": {"status": "pass", "evidence": "Observed UI state."}}),
        "execute": result_payload({"check-2": {"status": "pass", "evidence": "Measured 120 frames."}}),
    }
    with patch("live_test_loop.invoke_mcp", side_effect=lambda root, action: outputs[action["name"]]) as invoke:
        result = worker.run_task("Verify both checks.")
    assert result.ok, result.final_message
    assert invoke.call_count == 2
    assert next(call.args[1]["arguments"]["function"] for call in invoke.call_args_list
                if call.args[1]["name"] == "browser_evaluate") == script
    assert "escape backslashes" in json.dumps(worker.model.calls[1])


def test_inconclusive_retry_keeps_observed_failure_and_its_evidence():
    run = VerificationRun(REQUIREMENTS)
    check_id = "check-1"
    failed = result_payload({check_id: {"status": "fail", "evidence": "The rig is missing from the scene."}})
    run.record(run.begin_checks([check_id]), kind="mcp", ok=True, output=failed)
    run.finish_turn()
    run.record(run.begin_checks([check_id]), kind="mcp", ok=True, output={"ok": True})
    run.finish_turn()
    snapshot = run.snapshot(final=True, reason="Stopped by operator.")
    failure, not_run = snapshot["checks"]
    assert run.idle_turns == 1
    assert failure["status"] == "fail"
    assert failure["evidence"] == "The rig is missing from the scene."
    assert failure["latest_attempt"] == "The tool ran without a usable check verdict."
    assert not_run["status"] == "not_run"
    assert not_run["stop_reason"] == snapshot["stop_reason"] == "Stopped by operator."


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
    assert [check["status"] for check in result.verification["checks"]] == ["not_run", "not_run"]
    assert "2 required checks still open" in result.verification["stop_reason"]


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


@pytest.mark.parametrize("backend", ["tree", "stable"])
def test_browser_setup_leaves_attempts_available_and_script_proves_workflow(backend, tmp_path, monkeypatch):
    artifact_start = {"type": "artifact_preview", "operation": "start"}
    navigate = playwright("browser_navigate", url=PREVIEW_URL)
    snapshot = playwright("browser_snapshot")
    seed = playwright("browser_evaluate", function="async () => createFixture('/api/projects')")
    click = playwright("browser_click", target="#open-project")
    probe = playwright("browser_evaluate", function="() => ({ clips: 0 })")
    workflow = playwright("browser_evaluate", function="async () => openFixtureAndAssertViewport(document)")
    build = {"type": "verify", "checks": ["check-1"], "action": MCP}
    check = {"type": "verify", "checks": ["check-2"]}
    read = {"type": "read_file", "path": "/repo/notes.txt"}
    shell = "node -e 'prepareFixture()'"
    (tmp_path / "notes.txt").write_text("Prepare a disposable project fixture.")
    if backend == "tree":
        responses = [
            "artifact-preview start", verify("check-1"),
            "verify check-2 mcp " + json.dumps(navigate), "mcp " + json.dumps(snapshot),
            *["cat /repo/notes.txt"] * 2, f"shell {shell}",
            *["mcp " + json.dumps(seed)] * 2,
            "mcp " + json.dumps(click),
            "verify check-2 mcp " + json.dumps(probe), "verify check-2 mcp " + json.dumps(workflow),
        ]
        worker = tree_worker(tmp_path, monkeypatch, responses)
        mcp_target = "live_test_loop.invoke_mcp"
    else:
        actions = [artifact_start, build, {**check, "action": navigate}, snapshot, *[read] * 2,
                   {"type": "run_shell", "command": shell}, seed, seed, click,
                   {**check, "action": probe}, {**check, "action": workflow}]
        model = RecordingMessageModel([json.dumps({"action": action}) for action in actions])
        config = AgentConfig("gemini", "fixture", tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
        worker = WorkingFolderAgent(model, config)
        mcp_target = "main.invoke_mcp"
    pass_workflow = {
        "verification_results": {"check-2": {
            "status": "pass", "evidence": "The editor opened and showed its viewport.",
            "browser_observations": BROWSER_STEPS,
        }},
    }
    cases = [
        result_payload({"check-1": {"status": "pass", "evidence": "The rig opened."}}),
        playwright_output(f"### Ran Playwright code\nawait page.goto('...')\n### Page\n- Page URL: {PREVIEW_URL}"),
        playwright_output(f'### Page\n- Page URL: {PREVIEW_URL}\n### Snapshot\n- heading "Project Home"'),
        playwright_output('### Result\n{"verification_fixture":{"ready":false,"error":"POST /api/projects returned 422: title required"}}'),
        playwright_output('### Result\n{"verification_fixture":{"ready":true,"evidence":"Created project with clips","projectId":"fixture-123"}}'),
        playwright_output("### Result\n" + json.dumps(PREVIEW_URL.rstrip("/"))),
        playwright_output(f'### Page\n- Page URL: {PREVIEW_URL}\n### Snapshot\n- heading "Editor Viewport"'),
        playwright_output('### Result\n{"clipCount": 0, "view": "Project Home"}'),
        playwright_output("### Result\n" + json.dumps(pass_workflow, indent=2)
                          + "\n### Ran Playwright code\n```js\n// script echo\n```"),
    ]
    requirements = [REQUIREMENTS[0], BROWSER_REQUIREMENT]
    with (
        patch("artifact_preview._start", return_value={"ok": True, "url": PREVIEW_URL}),
        patch("artifact_preview._request"),
        patch(mcp_target, side_effect=cases) as invoke,
    ):
        worker.configure_verification(requirements)
        result = worker.run_task("Verify the rig and open the actual editor")
    assert invoke.call_count == 9
    assert result.ok, result.final_message
    assert all_required_checks_passed(requirements, result.verification)
    assert result.verification["setup_calls_used"] == 5
    assert result.verification["tool_calls_used"] == 5
    assert result.verification["checks"][1]["attempts"] == 2
    for call in invoke.call_args_list[3:5]:
        function = call.args[1]["arguments"]["function"]
        assert "location.origin" in function and "verification_fixture" in function
    assert invoke.call_args_list[5].args[1]["arguments"] == {"function": "() => location.origin"}
    assert invoke.call_args_list[6].args[1] == click
    assert invoke.call_args_list[-1].args[1]["arguments"] == workflow["arguments"]
    assert "POST /api/projects returned 422: title required" in json.dumps(worker.model.calls[8])
    assert "fixture-123" in json.dumps(worker.model.calls[-1])
    assert "clipCount" in json.dumps(worker.model.calls[-1])


def test_browser_setup_is_scoped_and_only_new_milestones_keep_it_active():
    run = VerificationRun([BROWSER_REQUIREMENT], managed_browser=True)
    navigate = playwright("browser_navigate", url=PREVIEW_URL)
    snapshot = playwright("browser_snapshot")
    start = {"type": "artifact_preview"}
    with pytest.raises(ValueError, match="Navigate to the local preview"):
        run.begin_setup(snapshot)
    with pytest.raises(ValueError, match="published URL"):
        run.begin_setup(navigate)
    run.begin_turn()
    run.begin_setup(start)
    run.record_setup(start, {"ok": True, "url": PREVIEW_URL})
    with pytest.raises(ValueError, match="published URL"):
        run.begin_setup(playwright("browser_navigate", url="http://127.0.0.1:9999/"))
    run.begin_setup(navigate)
    response = playwright_output(f"### Page\n- Page URL: {PREVIEW_URL}")
    assert run.record_setup(navigate, response)["ok"]
    with pytest.raises(ValueError, match="snapshot inline"):
        run.begin_setup(playwright("browser_snapshot", filename="snapshot.yml"))
    run.finish_turn()
    assert run.setup_calls == 2 and run.idle_turns == 0
    for turn in range(1, MAX_VERIFICATION_SETUP_CALLS - 1):
        run.begin_turn()
        run.begin_setup(navigate)
        run.record_setup(navigate, response)
        run.finish_turn()
        assert run.idle_turns == turn
    run.begin_turn()
    run.begin_setup(snapshot)
    run.record_setup(snapshot, playwright_output(f"### Page\n- Page URL: {PREVIEW_URL}\n### Snapshot\n- heading Preview"))
    run.finish_turn()
    assert run.setup_calls == MAX_VERIFICATION_SETUP_CALLS + 1
    assert run.idle_turns == 0
    assert run.tool_calls == 0
    assert run.snapshot(final=True)["checks"][0]["status"] == "not_run"


@pytest.mark.parametrize("backend", ["tree", "stable"])
@pytest.mark.parametrize("initial_stage", [
    {"ready": False, "evidence": "Created a project, scene, and shot. Take and clip are pending.",
     "projectId": "fixture-existing", "sceneId": "scene-existing", "shotId": "shot-existing"},
    {"ready": False, "error": "Default hierarchy missing scene/shot/take",
     "tree": {"project": {"id": "fixture-existing"}, "shots": [{"id": "shot-existing"}], "takes": []}},
])
def test_fixture_replays_resume_setup_and_reach_assertions(backend, initial_stage, tmp_path, monkeypatch):
    start = {"type": "artifact_preview", "operation": "start"}
    navigate = playwright("browser_navigate", url=PREVIEW_URL)
    read = {"type": "read_file", "path": "/repo/notes.txt"}
    seed = playwright("browser_evaluate", function="async () => createFixture()")
    complete = playwright("browser_evaluate", function="async () => completeFixture('shot-existing')")
    assertion = playwright("browser_evaluate", function="async () => checkWorkspace()")
    (tmp_path / "notes.txt").write_text("Look up the application API contract.")
    if backend == "tree":
        worker = tree_worker(tmp_path, monkeypatch, [
            "artifact-preview start", "mcp " + json.dumps(navigate), *["cat /repo/notes.txt"] * 4,
            "mcp " + json.dumps(seed), "mcp " + json.dumps(complete),
            "verify check-1 mcp " + json.dumps(assertion),
        ])
        mcp_target = "live_test_loop.invoke_mcp"
    else:
        monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
        actions = [start, navigate, *[read] * 4, seed, complete,
                   {"type": "verify", "checks": ["check-1"], "action": assertion}]
        provider = "codex-subscription" if initial_stage.get("evidence") else "gemini"
        model = RecordingMessageModel([json.dumps({"action": action}) for action in actions])
        config = AgentConfig(provider, "fixture", tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
        worker = WorkingFolderAgent(model, config)
        mcp_target = "main.invoke_mcp"
    passes = {"check-1": {"status": "pass", "evidence": "Observed Project Home and the editor after opening the fixture.",
                          "browser_observations": BROWSER_STEPS}}
    results = [
        playwright_output(f"### Page\n- Page URL: {PREVIEW_URL}"),
        playwright_output("### Result\n" + json.dumps({"verification_fixture": initial_stage})),
        playwright_output('### Result\n{"verification_fixture":{"ready":true,"evidence":"Added the missing take and clip to the existing shot","projectId":"fixture-existing"}}'),
        playwright_output("### Result\n" + json.dumps({"verification_results": passes})),
    ]
    with (
        patch("artifact_preview._start", return_value={"ok": True, "url": PREVIEW_URL}),
        patch("artifact_preview._request"),
        patch(mcp_target, side_effect=results) as invoke,
    ):
        worker.configure_verification([BROWSER_REQUIREMENT])
        result = worker.run_task("Continue the fixture setup and verify the editor")
    assert invoke.call_count == 4
    assert result.ok and all_required_checks_passed([BROWSER_REQUIREMENT], result.verification)
    assert result.verification["checks"][0]["attempts"] == 1
    assert "fixture-existing" in json.dumps(worker.model.calls[7])
    assert "shot-existing" in json.dumps(worker.model.calls[7])
    decisions = result.verification["mcp_session"]["decisions"]
    if "error" in initial_stage:
        assert "FIXTURE RECOVERY" in json.dumps(worker.model.calls[7])
        assert len(decisions) == 1 and decisions[0]["turn"] == 7
        assert decisions[0]["evidence"] == [initial_stage["error"]]
    else:
        assert not decisions


def test_repeated_fixture_errors_earn_only_one_recovery_turn():
    run = VerificationRun([BROWSER_REQUIREMENT], managed_browser=False)
    navigate = playwright("browser_navigate", url=PREVIEW_URL)
    fixture_step = playwright("browser_evaluate", function="async () => seedFixture()")
    run.begin_turn()
    run.begin_setup(navigate)
    run.record_setup(navigate, playwright_output(f"### Page\n- Page URL: {PREVIEW_URL}"))
    run.finish_turn()
    error = playwright_output('### Result\n{"verification_fixture":{"ready":false,"error":"Missing required take"}}')
    for _ in range(6):
        run.begin_turn()
        run.begin_setup(fixture_step)
        output = run.record_setup(fixture_step, error)
        assert not output["ok"]
        run.finish_turn()
    decisions = run.snapshot(final=True)["mcp_session"]["decisions"]
    assert [decision["decision"] for decision in decisions] == ["continue", "stop"]
    assert run.checks["check-1"].attempts == 0
    assert "stalled" in run.stop_reason


def test_fixture_parse_error_retains_the_preview_response_context():
    run = VerificationRun([BROWSER_REQUIREMENT], managed_browser=False)
    navigate = playwright("browser_navigate", url=PREVIEW_URL)
    run.begin_turn()
    run.begin_setup(navigate)
    run.record_setup(navigate, playwright_output(f"### Page\n- Page URL: {PREVIEW_URL}"))
    action = run.begin_setup(playwright("browser_evaluate", function="() => fetch('/api/shots').then(r => r.json())"))
    fixture = {
        "ready": False,
        "error": 'SyntaxError: Unexpected token \'<\', "<!doctype "... is not valid JSON',
        "requests": [{"method": "GET", "path": "/api/shots", "status": 200, "contentType": "text/html"}],
    }
    result = run.record_setup(action, playwright_output("### Result\n" + json.dumps({"verification_fixture": fixture})))
    assert not result["ok"]
    assert "GET /api/shots → HTTP 200 (text/html)" in result["error"]
    assert result["fixture"] == fixture
    assert "Content-Type" in run.guidance(tree_commands=True)


def test_oversized_fixture_results_keep_structured_evidence_and_ids():
    run = VerificationRun([BROWSER_REQUIREMENT], managed_browser=False)
    navigate = playwright("browser_navigate", url=PREVIEW_URL)
    run.begin_setup(navigate)
    run.record_setup(navigate, playwright_output(f"### Page\n- Page URL: {PREVIEW_URL}"))
    action = run.begin_setup(playwright("browser_evaluate", function="() => readFixture()"))
    fixture = {"ready": True, "evidence": "Found the disposable project.",
               "response": {"tree": ["A long response with quotes and backslashes \"\\"] * 120},
               "projectId": "project-after-large-field", "shotId": "shot-after-large-field"}
    result = run.record_setup(action, playwright_output("### Result\n" + json.dumps({"verification_fixture": fixture})))
    assert result["ok"]
    encoded = json.dumps(result["fixture"], ensure_ascii=False)
    assert len(encoded) <= 2600
    assert json.loads(encoded)["projectId"] == fixture["projectId"]
    assert json.loads(encoded)["shotId"] == fixture["shotId"]
    assert result["fixture"]["truncated"] is True
    assert "response" not in result["fixture"]
    assert result["fixture"]["response_excerpt"]


def test_navigation_that_leaves_the_preview_does_not_open_a_setup_session():
    run = VerificationRun([BROWSER_REQUIREMENT], managed_browser=False)
    navigate = playwright("browser_navigate", url=PREVIEW_URL)
    run.begin_setup(navigate)
    response = run.record_setup(navigate, playwright_output("### Page\n- Page URL: https://other.example/"))
    assert response["ok"] is False
    with pytest.raises(ValueError, match="Navigate to the local preview"):
        run.begin_setup(playwright("browser_snapshot"))
    with pytest.raises(ValueError, match="before preparing a browser fixture"):
        run.begin_setup(playwright("browser_evaluate", function="() => seedFixture()"))


def test_browser_input_uses_overall_budget_and_rechecks_the_preview_before_dispatch():
    run = VerificationRun([BROWSER_REQUIREMENT], managed_browser=False)
    navigate = playwright("browser_navigate", url=PREVIEW_URL)
    click = playwright("browser_click", target=".timeline-ruler")
    with pytest.raises(ValueError, match="before sending browser input"):
        run.begin_setup(click)
    run.begin_setup(navigate)
    run.record_setup(navigate, playwright_output(f"### Page\n- Page URL: {PREVIEW_URL}"))
    run.setup_calls = MAX_VERIFICATION_SETUP_CALLS
    assert run.begin_setup(click) == click
    seen = []
    def dispatch(action):
        seen.append(action)
        return playwright_output('### Result\n"http://127.0.0.1:9999"')

    result = run.invoke_setup_mcp(click, dispatch)
    assert not result["ok"] and "expected preview" in result["error"]
    assert [action["name"] for action in seen] == ["browser_evaluate"]
    assert run.tool_calls == 1
    assert run.checks["check-1"].attempts == 0


def test_unbound_browser_observations_leave_fixture_progress_and_checks_untouched():
    run = VerificationRun([BROWSER_REQUIREMENT], managed_browser=False)
    navigate = playwright("browser_navigate", url=PREVIEW_URL)
    run.begin_setup(navigate)
    run.record_setup(navigate, playwright_output(f"### Page\n- Page URL: {PREVIEW_URL}"))
    action = run.begin_setup(playwright("browser_evaluate", function="() => ({ clips: 2 })"))
    result = run.record_setup(action, playwright_output('### Result\n{"verification_fixture":{"clips":2}}'))
    assert result == {"ok": True, "inspection": {"response": {"clips": 2}}}
    assert run.snapshot()["checks"][0]["attempts"] == 0
    assert "latest_fixture_setup" not in run.guidance(tree_commands=True)


@pytest.mark.parametrize("post_origin", [PREVIEW_URL.rstrip("/"), "http://127.0.0.1:9999"])
def test_keyboard_result_without_page_metadata_checks_current_origin(post_origin):
    run = VerificationRun([BROWSER_REQUIREMENT], managed_browser=False)
    navigate = playwright("browser_navigate", url=PREVIEW_URL)
    run.begin_setup(navigate)
    run.record_setup(navigate, playwright_output(f"### Page\n- Page URL: {PREVIEW_URL}"))
    press = run.begin_setup(playwright("browser_press_key", key="Home"))
    input_reply = playwright_output("### Ran Playwright code\nawait page.keyboard.press('Home');")
    dispatch = iter([
        playwright_output("### Result\n" + json.dumps(PREVIEW_URL.rstrip("/"))),
        input_reply,
        playwright_output("### Result\n" + json.dumps(post_origin)),
    ])
    output = run.invoke_setup_mcp(press, lambda _action: next(dispatch))
    result = run.record_setup(press, output)
    assert result["ok"] is (post_origin == PREVIEW_URL.rstrip("/"))
    assert run.tool_calls == 3
    if result["ok"]:
        run.begin_setup(playwright("browser_snapshot"))
    else:
        assert "expected local preview origin" in result["error"]


def test_verification_repair_preserves_structured_scope_blocker(tmp_path, monkeypatch):
    worker = tree_worker(tmp_path, monkeypatch, [])
    planner, plan, goal = planner_goal(worker, tmp_path)
    run = VerificationRun(REQUIREMENTS)
    ids = run.begin_checks(["check-1"], action=MCP)
    run.record(ids, kind="mcp", ok=True, output=result_payload({
        "check-1": {"status": "fail", "evidence": "Mode switch reset the transport."},
    }), action=MCP)
    result = GoalExecutionResult(goal.goal_id, goal.title, goal.goal, run.report(), status="failed",
                                 verification=run.snapshot(final=True))
    findings = json.dumps({"targets": [], "diagnosis": {
        "cause": "The caller does not restore the clock on mount.",
        "blocker": "src/App.tsx is outside the approved repair scope.",
        "evidence": ["Playback advanced while the toolbar was paused."],
        "repair_steps": ["Restore the authoritative time on mount."],
    }})
    with (
        patch.object(planner, "_verification_repair_scope", return_value=frozenset({"src/Viewport.tsx"})),
        patch.object(planner, "_run_verification_work", return_value=findings) as followup,
    ):
        planner._repair_failed_verification(plan, goal, 1, result)
    assert followup.call_count == 1
    assert "Investigation found no in-scope repair" in result.final_message
    assert "src/App.tsx is outside the approved repair scope" in result.final_message
    assert result.verification["checks"][0]["next_step"] == "src/App.tsx is outside the approved repair scope."


def test_inconclusive_pointer_check_preserves_independent_browser_verdicts():
    requirements = ["Event markers have flags", "Cutting at the playhead trims both halves", "Canvas fills the viewport"]
    run = VerificationRun(requirements)
    action = playwright("browser_evaluate")
    ids = run.begin_checks(["check-1", "check-2", "check-3"], action=action)
    verdicts = {
        "check-1": {"status": "pass", "evidence": "Two markers rendered with flags."},
        "check-2": {"status": "unverified", "evidence": "Playhead did not move after the pointer interaction."},
        "check-3": {"status": "pass", "evidence": "Canvas and panel were both 720×502 pixels."},
    }
    run.record(ids, kind="mcp", ok=True,
               output=playwright_output("### Result\n" + json.dumps({"verification_results": verdicts})), action=action)
    first, middle, last = run.snapshot()["checks"]
    assert [first["status"], middle["status"], last["status"]] == ["pass", "unverified", "pass"]
    assert middle["evidence"] == verdicts["check-2"]["evidence"]
    assert middle["retryable"]
    run.record(run.begin_checks(["check-2"], action=action), kind="mcp", ok=True, output=result_payload({
        "check-2": {"status": "pass", "evidence": "After a native click, both trim boundaries met at 3 seconds."},
    }), action=action)
    assert all_required_checks_passed(requirements, run.snapshot(final=True))
    assert [check.attempts for check in run.checks.values()] == [1, 2, 1]


def test_playwright_code_echo_cannot_supply_verdicts():
    run = VerificationRun(["The timeline marker is visible"])
    action = playwright("browser_run_code")
    ids = run.begin_checks(["check-1"], action=action)
    fake = 'VERIFICATION_RESULTS: {"check-1":{"status":"pass","evidence":"copied input"}}'
    old_probe = "### Result\n{\"markerCount\":0}\n### Ran Playwright code\n```js\n" + fake + "\n" + "x" * 3500
    state = run.record(ids, kind="mcp", ok=True, output=playwright_output(old_probe), action=action)
    assert state["checks"][0]["status"] == "unverified"
    assert "markerCount" in state["inconclusive_tool_output"]
    assert len(state["inconclusive_tool_output"]) < 3050
    assert "inconclusive_tool_output" not in run.snapshot(final=True)


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


@pytest.mark.parametrize("entry", ["button", "artifact_prompt"])
def test_proceed_with_verification_next_steps_reuses_the_plan_and_completed_goals(entry, tmp_path, monkeypatch):
    worker = tree_worker(tmp_path, monkeypatch, [verify("check-2"), verify("check-1")])
    planner, plan, goal = planner_goal(worker, tmp_path)
    first = PlannerGoal("setup", "Set up rig", "Build the test rig", "Prepare the deliverable", estimated_scope="analysis")
    plan.goals.insert(0, first)
    goal.depends_on = [first.goal_id]
    completed = GoalExecutionResult(first.goal_id, first.title, first.goal, "Ready.", task_satisfied=True)
    prior = VerificationRun(REQUIREMENTS)
    prior.record(prior.begin_checks(["check-2"], action=MCP), kind="mcp", action=MCP, ok=True, output=result_payload({
        "check-2": {"status": "unverified", "evidence": "The fixture used the wrong clip selector.",
                    "next_step": "Select the rendered timeline clip before testing Split."},
    }))
    prior_report = prior.snapshot(final=True)
    prior_report["follow_up"] = ["The previous investigation identified a fixture issue."]
    planner.session.last_completed_results = [
        completed,
        GoalExecutionResult(goal.goal_id, goal.title, goal.goal, prior.report(), status="failed",
                            verification=prior_report),
    ]
    planner.session.execution_paused = True
    planner.session.paused_plan = plan
    planner.session.paused_failed_goal_id = goal.goal_id
    planner.session.paused_completed_results = [completed]
    passed = [result_payload({check: {"status": "pass", "evidence": "Verified."}}) for check in ["check-2", "check-1"]]
    with (
        patch("live_test_loop.invoke_mcp", side_effect=passed),
        patch.object(planner, "_planner_complete", return_value='{"summary":"Verification completed.","next_steps":[]}'),
    ):
        if entry == "button":
            exchanges = []
            _handle_bridge_planner_action(planner=planner, transcript=[], request={
                "action": "proceed_verification_next_steps", "expected_plan": planner._plan_payload(plan),
            }, add_exchange=lambda role, body: exchanges.append((role, body)))
            assert exchanges[0] == ("user", "Proceed with next steps")
        else:
            planner.continue_conversation(
                "You are working in an independent skillz artifact repository: /repo.\n\n"
                "User request:\nProceed with next steps."
            )
    messages = json.dumps(worker.model.calls[0])
    assert "The fixture used the wrong clip selector." in messages
    assert "Select the rendered timeline clip before testing Split." in messages
    assert "The previous investigation identified a fixture issue." in messages
    assert "Select the rendered timeline clip before testing Split." not in goal.delegation_notes
    assert not planner.session.execution_paused
    assert planner.session.last_completed_results[0] is completed


def test_continuous_mode_applies_actionable_verification_next_steps_once(tmp_path, monkeypatch):
    commands = [verify("check-1", "check-2"), "finish Verify the fixture selection."]
    worker = tree_worker(tmp_path, monkeypatch, commands * 2)
    planner, plan, _ = planner_goal(worker, tmp_path)
    plan.not_in_scope = ["Implementation changes"]
    issue = {"request_summary": "Verify the rig", "plan_summary": "Verify the rig"}

    def present_plan():
        planner.session.pending_plan = plan
        return "Plan ready"

    response = result_payload({
        "check-1": {"status": "pass", "evidence": "The rig opened."},
        "check-2": {"status": "unverified", "evidence": "The clip selection was inconclusive.",
                    "next_step": "Confirm the clip selection in the live preview."},
    })
    with (
        patch.object(planner, "_create_or_select_continuous_issue", return_value=issue),
        patch.object(planner, "_handle_intake_turn", side_effect=present_plan) as intake,
        patch("live_test_loop.invoke_mcp", return_value=response) as invoke,
    ):
        report = planner.start_continuous(max_cycles=4)
    assert intake.call_count == 1 and invoke.call_count == 2
    assert report.count("continuing with the verification next steps once") == 1
    assert "Confirm the clip selection in the live preview." in json.dumps(worker.model.calls[2])
    assert planner.session.execution_paused
    assert not planner.continuous_state.completed_issue_ids


def test_native_checks_keep_the_original_turn_budget(tmp_path, monkeypatch):
    requirement_count = 13
    worker = tree_worker(tmp_path, monkeypatch, [f"verify check-{i} run-route-check /{i}" for i in range(1, requirement_count + 1)])
    worker.configure_verification([f"Verify item {i}" for i in range(1, requirement_count + 1)])
    with patch.object(worker, "_exec_run_route_check", return_value="issues=0") as invoke:
        result = worker.run_task("Verify all items")
    assert invoke.call_count == 12
    assert "verification turn budget was exhausted" in result.final_message
    assert result.verification["turns_without_new_evidence"] == 0
    assert result.verification["checks"][-1]["status"] == "not_run"
    assert result.verification["checks"][-1]["stop_reason"] == result.verification["stop_reason"]
    assert not result.ok


@pytest.mark.parametrize("backend", ["tree", "stable"])
def test_non_browser_mcp_progress_earns_continuation_in_both_workers(backend, tmp_path, monkeypatch):
    requirement = "Verify that the rig and animation load"
    partial = [
        result_payload({"check-1": {
            "status": "unverified", "evidence": "Verification is ongoing.",
            "progress": {"milestone": f"validated-rig-part-{i}", "evidence": f"Inspected rig part {i}."},
        }})
        for i in range(1, 13)
    ]
    passed = result_payload({"check-1": {"status": "pass", "evidence": "The rig loaded and all the animation frames played."}})
    if backend == "tree":
        worker = tree_worker(tmp_path, monkeypatch, [verify("check-1")] * 13)
        mcp_target = "live_test_loop.invoke_mcp"
    else:
        monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
        response = json.dumps({"action": {"type": "verify", "checks": ["check-1"], "action": MCP}})
        model = RecordingMessageModel([response] * 13)
        config = AgentConfig("gemini", "fixture", tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
        worker = WorkingFolderAgent(model, config)
        mcp_target = "main.invoke_mcp"
    worker.configure_verification([requirement])
    with patch(mcp_target, side_effect=[*partial, passed]) as invoke:
        result = worker.run_task(requirement)
    assert invoke.call_count == 13
    assert result.ok and all_required_checks_passed([requirement], result.verification)
    assert result.verification["checks"][0]["attempts"] == 13
    decision, = result.verification["mcp_session"]["decisions"]
    assert decision["turn"] == 12 and decision["decision"] == "continue"
    assert decision["evidence"][-1].endswith("Inspected rig part 12.")


def test_mcp_call_checkpoint_stops_when_a_batch_repeats_the_same_observation():
    run = VerificationRun(["Verify the remote application"])
    run.begin_turn()
    first_checkpoint = MAX_VERIFICATION_TOOL_CALLS + MAX_VERIFICATION_SETUP_CALLS

    def probe(label):
        ids = run.begin_checks(["check-1"], action=MCP)
        run.record(ids, kind="mcp", ok=True, action=MCP, output=result_payload({"check-1": {
            "status": "unverified", "evidence": "Work remains.",
            "progress": {"milestone": label, "evidence": "The target opened."},
        }}))

    probe("open-first")
    for _ in range(first_checkpoint - 1):
        run.begin_mcp_discovery()
    run.finish_turn()
    assert not run.stop_reason
    session = run.snapshot()["mcp_session"]
    assert len(session["decisions"][0]["evidence"]) == 1
    run.begin_turn()
    probe("open-again")
    for _ in range(first_checkpoint + 1, session["call_checkpoint"]):
        run.begin_mcp_discovery()
    with pytest.raises(ValueError, match="checkpoint without new check evidence"):
        probe("too-late")
    assert run.tool_calls + run.setup_calls == session["call_checkpoint"]
    assert run.snapshot(final=True)["checks"][0]["status"] == "unverified"


def test_mcp_coverage_window_allows_pending_input_and_undispatched_assertions():
    run = VerificationRun(["Marker styling", "Split playback", "Persisted Undo"], managed_browser=False)
    navigate = playwright("browser_navigate", url=PREVIEW_URL)
    page = playwright_output(f"### Page\n- Page URL: {PREVIEW_URL}")

    def click(selector):
        action = run.begin_setup(playwright("browser_click", target=selector))
        output = run.invoke_setup_mcp(action, lambda candidate: (
            playwright_output('### Result\n"http://127.0.0.1:4173"')
            if candidate["name"] == "browser_evaluate" else page
        ))
        assert run.record_setup(action, output)["ok"]

    def pass_checks(keys):
        ids = run.begin_checks(keys, action=MCP)
        run.record(ids, kind="mcp", ok=True, action=MCP, output=result_payload({
            key: {"status": "pass", "evidence": f"Observed {key} in the live fixture."} for key in keys
        }))

    run.begin_turn()
    run.begin_setup(navigate)
    run.record_setup(navigate, page)
    click(".open-workspace")
    pass_checks(["check-1"])
    session = run.mcp_session
    while run.tool_calls + run.setup_calls < session.call_checkpoint:
        run.begin_mcp_discovery()
    run.finish_turn()
    run.begin_turn()
    while run.tool_calls + run.setup_calls < session.call_checkpoint - 3:
        run.begin_mcp_discovery()
    click(".timeline-clip-block")
    assert run.tool_calls + run.setup_calls == session.call_checkpoint - 1

    click(".event-marker")
    assert "FOCUSED COVERAGE WINDOW — check-2, check-3" in run.guidance(tree_commands=True)
    pass_checks(["check-2", "check-3"])
    run.finish_turn()

    assert run.passed
    assert session.decisions[-1]["reason"] == "One focused window to start undispatched checks (check-2, check-3)."
    assert all(check["attempts"] == 1 for check in run.snapshot()["checks"])


def test_mcp_coverage_window_does_not_keep_extending_an_idle_session():
    run = VerificationRun(REQUIREMENTS, managed_browser=False)
    navigate = playwright("browser_navigate", url=PREVIEW_URL)
    run.begin_turn()
    run.begin_setup(navigate)
    run.record_setup(navigate, playwright_output(f"### Page\n- Page URL: {PREVIEW_URL}"))
    run.finish_turn()
    while not run.stop_reason and run.turns < run.max_turns:
        run.begin_turn()
        run.finish_turn()
    session = run.snapshot(final=True)["mcp_session"]
    assert [decision["decision"] for decision in session["decisions"]] == ["continue", "stop"]
    assert "undispatched checks" in session["decisions"][0]["reason"]
    assert "stalled" in run.stop_reason
    assert run.turns < run.max_turns


def test_mcp_progress_is_scoped_to_the_check_that_produced_it():
    run = VerificationRun(REQUIREMENTS)
    for count in range(3):
        ids = run.begin_checks(["check-1", "check-2"], action=MCP)
        run.record(ids, kind="mcp", ok=True, action=MCP, output=result_payload({
            "check-1": {"status": "unverified", "evidence": "Waiting for the tool to return a usable result."},
            "check-2": {"status": "unverified", "evidence": "Verification continues.",
                        "progress": {"milestone": f"frames-{count}", "evidence": f"Measured group {count}."}},
        }))
    first, second = run.snapshot()["checks"]
    assert not first["retryable"] and first["mcp_attempt_checkpoint"] == 3
    assert second["retryable"] and second["mcp_attempt_checkpoint"] == 6
    assert not run.stop_reason
    ids = run.begin_checks(["check-2"], action=MCP)
    run.record(ids, kind="mcp", ok=True, action=MCP, output=result_payload({
        "check-2": {"status": "pass", "evidence": "Measured the entire range."},
    }))
    assert not run.passed
    assert run.snapshot()["mcp_session"]["max_turns"] == MAX_MCP_VERIFICATION_TURNS + 4
    assert run.snapshot()["mcp_session"]["max_calls"] == MAX_MCP_VERIFICATION_CALLS + 8


def test_mcp_session_has_an_overall_turn_ceiling_even_with_continuing_progress():
    run = VerificationRun(["Verify the remote application"])
    for turn in range(1, MAX_MCP_VERIFICATION_TURNS + 1):
        run.begin_turn()
        ids = run.begin_checks(["check-1"], action=MCP)
        run.record(ids, kind="mcp", ok=True, action=MCP, output=result_payload({"check-1": {
            "status": "unverified", "evidence": "Work remains.",
            "progress": {"milestone": f"part-{turn}", "evidence": f"Measured part {turn}."},
        }}))
        run.finish_turn()
    assert f"overall turn limit ({MAX_MCP_VERIFICATION_TURNS}/{MAX_MCP_VERIFICATION_TURNS})" in run.stop_reason
    assert run.snapshot()["mcp_session"]["max_calls"] == MAX_MCP_VERIFICATION_CALLS


def test_mcp_session_counts_check_and_discovery_calls_towards_its_overall_ceiling():
    run = VerificationRun(["Verify the remote application"])
    run.begin_turn()
    for count in range(0, MAX_MCP_VERIFICATION_CALLS, 2):
        ids = run.begin_checks(["check-1"], action=MCP)
        run.record(ids, kind="mcp", ok=True, action=MCP, output=result_payload({"check-1": {
            "status": "unverified", "evidence": "Work remains.",
            "progress": {"milestone": f"part-{count}", "evidence": f"Measured part {count}."},
        }}))
        run.begin_mcp_discovery()
    with pytest.raises(ValueError, match="overall tool-call limit"):
        run.begin_mcp_discovery()
    assert run.checks["check-1"].attempts == MAX_MCP_VERIFICATION_CALLS // 2
    assert run.snapshot()["mcp_session"]["calls_used"] == MAX_MCP_VERIFICATION_CALLS


def test_check_attempt_and_overall_tool_limits():
    run = VerificationRun([f"Check item {i}" for i in range(MAX_VERIFICATION_TOOL_CALLS)])
    calls = ["check-1"] * 3 + [f"check-{i}" for i in range(2, MAX_VERIFICATION_TOOL_CALLS - 1)]
    for index, key in enumerate(calls):
        selected = run.begin_checks([key])
        run.record(selected, kind="mcp", ok=True, output=result_payload({
            key: {"status": "fail", "evidence": f"Assertion {index} failed"},
        }))
    assert "tool-call budget" in run.stop_reason
    with pytest.raises(ValueError, match="tool-call budget"):
        run.begin_checks([f"check-{MAX_VERIFICATION_TOOL_CALLS}"])
    assert run.checks["check-1"].attempts == 3
    attempted, *_, not_run = run.snapshot(final=True)["checks"]
    assert attempted["status"] == "fail"
    assert attempted["stop_reason"] == "The check used all 3 attempts."
    assert not_run["status"] == "not_run"
    assert "tool-call budget" in not_run["stop_reason"]


def test_evidence_turn_resets_idle_counter_and_failed_provider_keeps_last_verdict(tmp_path, monkeypatch):
    worker = tree_worker(tmp_path, monkeypatch, ["cat /repo/notes.txt"] * 4 + [verify("check-1")])
    (tmp_path / "notes.txt").write_text("Check the rig.")
    worker.configure_verification(REQUIREMENTS)
    passed = result_payload({"check-1": {"status": "pass", "evidence": "Opened file and found the rig"}})
    with patch("live_test_loop.invoke_mcp", return_value=passed):
        result = worker.run_task("Verify both requirements")
    assert result.verification["turns_used"] == 6
    assert [check["status"] for check in result.verification["checks"]] == ["pass", "not_run"]
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
    assert [check["status"] for check in result.verification["checks"]] == ["pass", "not_run"]
    assert result.verification["worker_notes"] == "Use a local playback session to confirm the frame span."
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
    assert "Final reporting turn (4/4)" in json.dumps(worker.model.calls[-1])
    assert "GOAL-START SKILL MODE" not in json.dumps(worker.model.calls[0])
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
