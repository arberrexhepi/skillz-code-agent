from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from artifact_preview import preview_guidance
from live_test_loop import TreeLoopPlannerWorker
from execution_progress import (
    ExecutionProgress,
    MAX_RUNTIME_PROGRESS_TURNS,
    READ_ONLY_RECOVERY_TURNS,
    RECOVERY_TURN_ALLOWANCE,
)
from main import AgentConfig, WorkingFolderAgent
from planner import PlannerAgent, PlannerGoal, PlannerPlan
from tests.test_tree_loop_messages import RecordingMessageModel
from tree_commands import CommandResult
from tree_loop import TreeLoop


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is required to exercise package scripts")
def test_beta_runs_declared_scripts_and_manages_dev(tmp_path, monkeypatch):
    monkeypatch.setenv("SHELL_ACCESS", "true")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "dev": 'node -e "console.log(\'fixture-dev-ready\'); setInterval(() => {}, 1000)"',
        "build:fixture": 'node -e "console.log(\'fixture-build-done\')"',
    }}))
    model = RecordingMessageModel(["shell npm run dev"])
    loop = TreeLoop(model=model, workspace_root=tmp_path, max_turns=1, verbose=False)
    result = loop.run("Start the app.").turns[0].results[0]
    assert result.ok, result.output
    process_id = re.search(r"process_id=([a-f0-9]+)", result.output).group(1)
    try:
        assert "process_state=running" in result.output and "fixture-dev-ready" in result.output
        status = loop._exec_run_shell({"command": f"status {process_id}"})
        assert "process_state=running" in status
        assert f"process_id={process_id}" in loop._exec_run_shell({"command": "npm run dev"})
        finite = loop._exec_run_shell({"command": "npm run build:fixture"})
        assert finite.startswith("exit_code=0") and "fixture-build-done" in finite
        assert "does not define npm script" in loop._exec_run_shell({"command": "npm run dev:missing"})
    finally:
        assert "process_state=stopped" in loop._exec_run_shell({"command": f"stop {process_id}"})


@pytest.mark.skipif(shutil.which("npm") is None, reason="npm is required to exercise package scripts")
def test_stable_supports_background_for_a_custom_package_script(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "fixture": 'node -e "console.log(\'custom-ready\'); setInterval(() => {}, 1000)"',
    }}))
    model = RecordingMessageModel([])
    config = AgentConfig("gemini", "fixture", tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
    worker = WorkingFolderAgent(model, config)
    result = worker._handle_run_shell_action({"type": "run_shell", "command": ["npm", "run", "fixture"], "background": True})
    assert result.ok, result.payload
    process_id = result.payload["process_id"]
    try:
        assert result.payload["process_state"] == "running"
        assert "custom-ready" in result.payload["stdout"]
        assert worker._handle_run_shell_action({"type": "run_shell", "operation": "status", "process_id": process_id}).ok
    finally:
        assert worker._handle_run_shell_action({"type": "run_shell", "operation": "stop", "process_id": process_id}).ok


def run_execution_goal(tmp_path, monkeypatch, commands, *, unavailable=False, scope="execute"):
    monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
    worker = TreeLoopPlannerWorker(model=RecordingMessageModel(commands), root=tmp_path,
                                   max_turns=len(commands), verbose=False)
    planner = PlannerAgent(RecordingMessageModel([]), SimpleNamespace(root=tmp_path, provider="codex-subscription"),
                           lambda: worker, json.loads)
    goal = PlannerGoal(goal_id="browser", title="Inspect the page in the browser", goal="Report the rendered heading",
                       reason="The rendered page is the deliverable", estimated_scope=scope)
    plan = PlannerPlan(summary="Inspect browser", goals=[goal], clarification_summary="", original_request="Check the page.")

    def route_check(action):
        if unavailable:
            action["_validation_unavailable"] = True
            return "run_route_check: playwright is not installed in the workspace"
        return "run_route_check: the loaded page failed validation"

    with patch.object(worker.loop, "_exec_run_route_check", side_effect=route_check), patch(
        "live_test_loop.invoke_mcp", return_value={"ok": True, "result": {"content": [{"type": "text", "text": "Heading present"}]}}
    ):
        result = planner._execute_goal_with_worker(worker, plan, goal, 1)
    return result, worker


def test_browser_execute_goal_progresses_without_edits_or_local_playwright(tmp_path, monkeypatch):
    commands = ["run-route-check /"]
    commands += [
        'mcp ' + json.dumps({"operation": "call", "server": "playwright", "name": "browser_navigate",
                             "arguments": {"url": f"http://localhost:3000/view/{index}"}})
        for index in range(12)
    ]
    result, worker = run_execution_goal(tmp_path, monkeypatch, commands + ["finish Heading present"], unavailable=True)
    assert result.status == "completed" and result.validation_passed
    assert result.validation_summary == "Observed successful external execution."
    assert worker._execution_delivery.state()["external_executions"] == 12
    assert not worker._has_mutation


def test_catalog_lookups_alone_cannot_complete_an_execute_goal(tmp_path, monkeypatch):
    result, _ = run_execution_goal(tmp_path, monkeypatch, [
        'mcp {"operation":"list","server":"playwright"}', "finish Done",
    ])
    assert result.status == "failed"
    assert "without a successful task-specific MCP operation" in result.final_message


def test_artifact_preview_url_reaches_the_browser_goal_without_an_edit(tmp_path, monkeypatch):
    url = "http://127.0.0.1:49153"
    with patch("artifact_preview._start", return_value={"ok": True, "status": "running", "url": url}) as start:
        result, worker = run_execution_goal(tmp_path, monkeypatch, [
            "artifact-preview start",
            'mcp ' + json.dumps({"operation": "call", "server": "playwright", "name": "browser_navigate",
                                 "arguments": {"url": url}}),
            "finish Heading present",
        ])
    start.assert_called_once_with()
    assert any(url in message["content"] for message in worker.loop.model.calls[1])
    assert result.status == "completed" and result.validation_passed
    assert worker._execution_delivery.state()["external_executions"] == 2
    assert not worker._has_mutation


def test_artifact_preview_is_scoped_and_available_to_stable_execution(tmp_path):
    model = RecordingMessageModel([])
    config = AgentConfig("gemini", "fixture", tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
    worker = WorkingFolderAgent(model, config)
    action = {"type": "artifact_preview"}
    assert not preview_guidance() and not worker._execute_action(action).ok
    with patch("artifact_preview._start", return_value={"ok": True, "url": "http://127.0.0.1:49153"}) as start:
        assert '{"type":"artifact_preview"}' in preview_guidance()
        assert worker._execute_action(action).payload["url"] == "http://127.0.0.1:49153"
        worker.configure_discovery_budget("quick", "Quick Scan", 6)
        assert not worker._execute_action(action).ok
        start.assert_called_once_with()


def test_artifact_route_check_uses_published_preview_without_workspace_playwright(tmp_path):
    model = RecordingMessageModel([])
    loop = TreeLoop(model=model, workspace_root=tmp_path, verbose=False)
    action = {"type": "run_route_check", "target": "http://127.0.0.1:41981/studio?mode=test"}
    published = "http://127.0.0.1:52413/studio?mode=test"
    with patch("artifact_preview._request", return_value={
        "ok": True, "url": published, "finalUrl": published, "title": "Motion Studio", "route": "/studio?mode=test",
        "pageErrors": [{"message": "Render failed"}],
    }) as route, patch("tree_loop.subprocess.run", side_effect=AssertionError("No workspace browser")):
        result = loop._exec_run_route_check(action)
    route.assert_called_once_with({"operation": "route-check", "route": "/studio?mode=test"})
    assert f"url={published}" in result and "issues=1" in result
    assert loop._tool_message_indicates_failure("run_route_check", result)


def test_artifact_preview_verification_is_external_execution(tmp_path, monkeypatch):
    catalog = {"operation": "list", "server": "game-tools"}
    read = {"operation": "call", "server": "game-tools", "name": "read_board"}
    chat = {"message": "Use game-tools/read_board"}
    actions = [
        {"operation": "mcp", "input": catalog},
        {"operation": "mcp", "input": read},
        {"operation": "chat", "input": chat},
        {"operation": "browser-check", "input": {"route": "/", "steps": [
            {"type": "assert-visible", "selector": "#board", "label": "Game board"},
        ]}},
    ]
    with patch("artifact_preview._request", return_value={"ok": True, "message": "Board verified"}) as request:
        result, worker = run_execution_goal(tmp_path, monkeypatch, [
            f"artifact-preview {action['operation']} {json.dumps(action['input'])}" for action in actions
        ] + ["finish Board verified"])
    assert [call.args[0] for call in request.call_args_list] == actions
    assert result.status == "completed" and result.validation_passed
    assert worker._execution_delivery.state()["external_executions"] == 3
    assert not worker._has_mutation


def test_actual_failed_route_check_still_blocks_execute_goal(tmp_path, monkeypatch):
    result, _ = run_execution_goal(tmp_path, monkeypatch, [
        "run-route-check /",
        'mcp {"operation":"call","server":"playwright","name":"browser_snapshot"}',
        "finish Heading present",
    ])
    assert result.status == "failed"
    assert "the loaded page failed validation" in result.validation_summary


def test_repeat_external_calls_do_not_renew_recovery():
    progress = ExecutionProgress()
    result = CommandResult(True, "page snapshot", "tool",
                           tool_action={"type": "mcp", "operation": "call", "server": "playwright", "name": "browser_snapshot"})
    progress.observe_turn(["mcp snapshot"], [result])
    assert progress.read_only_turns == 0
    for _ in range(11):
        progress.observe_turn(["mcp snapshot"], [result])
    assert progress.exhausted


@pytest.mark.parametrize("scope", ["write", "mixed"])
def test_write_goal_can_reproduce_in_the_browser_before_editing(scope, tmp_path, monkeypatch):
    note = tmp_path / "note.txt"
    note.write_text("original\n")
    catalog = 'mcp {"operation":"list","server":"playwright"}'
    ceiling = READ_ONLY_RECOVERY_TURNS + RECOVERY_TURN_ALLOWANCE
    probes = [
        "mcp " + json.dumps({"operation": "call", "server": "playwright", "name": "browser_evaluate",
                             "arguments": {"function": f"() => document.querySelector('.step-{index}')?.textContent"}})
        for index in range(MAX_RUNTIME_PROGRESS_TURNS)
    ]
    commands = [catalog, *probes, "cat /repo/note.txt", *[catalog] * (ceiling - 3),
                "replace-lines note.txt:1-1 repaired", "cat /repo/note.txt\nfinish Fixed the issue"]
    result, worker = run_execution_goal(tmp_path, monkeypatch, commands, scope=scope)
    assert result.status == "completed", result.final_message
    assert result.validation_passed
    assert note.read_text() == "repaired\n"
    assert worker._execution_delivery.state()["runtime_progress_turns"] == MAX_RUNTIME_PROGRESS_TURNS


def test_runtime_progress_uses_one_bounded_allowance_without_refilling_reads():
    progress = ExecutionProgress(read_only_turns=READ_ONLY_RECOVERY_TURNS, repository_reads=2, focused_reads_used=2)

    def probe(index, *, ok=True, operation="call"):
        return CommandResult(ok, "Observed browser state", "tool",
                             tool_action={"type": "mcp", "operation": operation, "server": "playwright",
                                          "name": "browser_evaluate", "arguments": {"function": f"() => {index}"}})

    first = probe(0)
    progress.observe_turn(["mcp"], [first], goal_scope="write")
    progress.observe_turn(["mcp"], [first], goal_scope="write")
    progress.observe_turn(["mcp"], [probe(-1, ok=False)], goal_scope="write")
    progress.observe_turn(["mcp"], [probe(-2, operation="describe")], goal_scope="write")
    assert progress.runtime_progress_turns == 1
    assert progress.exhausted

    progress = ExecutionProgress(read_only_turns=READ_ONLY_RECOVERY_TURNS, repository_reads=2, focused_reads_used=2)
    for index in range(MAX_RUNTIME_PROGRESS_TURNS):
        progress.observe_turn(["mcp"], [probe(index)], goal_scope="mixed")
    assert progress.read_only_turns == READ_ONLY_RECOVERY_TURNS
    assert progress.repository_reads == 2 and progress.focused_reads_used == 2
    for index in range(MAX_RUNTIME_PROGRESS_TURNS, MAX_RUNTIME_PROGRESS_TURNS + RECOVERY_TURN_ALLOWANCE):
        progress.observe_turn(["mcp"], [probe(index)], goal_scope="mixed")
    assert progress.exhausted
    assert progress.runtime_progress_turns == MAX_RUNTIME_PROGRESS_TURNS
