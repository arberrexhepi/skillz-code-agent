import json
from types import SimpleNamespace as Object
from unittest.mock import Mock, patch

import pytest

import main
from command_blocks import BEGIN_COMMANDS, END_COMMANDS
from live_test_loop import TreeLoopPlannerWorker
from model_output_progress import DraftStrategy, ModelOutputObserver, ModelOutputProgress
from planner import PlannerAgent
from provider_response import CommandWaveBoundary
from structured_command_response import COMMAND_BLOCK_SCHEMA
from tree_loop import TreeLoop


MESSAGES = [{"role": "user", "content": "Inspect the workspace."}]


def test_openai_streams_only_summary_events_and_keeps_final_usage():
    updates = []
    output = []
    response = Object(output_text="finish done", status="completed", usage={"input_tokens": 12})

    class Stream:
        response = Object(headers={"x-request-id": "response-id"})

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def __iter__(self):
            yield Object(type="response.reasoning_text.delta", delta="private reasoning")
            yield Object(type="response.reasoning_summary_text.delta", delta="Checking the workspace.", item_id="r1", summary_index=0)
            assert updates == ["Checking the workspace."]
            yield Object(type="response.reasoning_summary_text.done", text="Checking the workspace.", item_id="r1", summary_index=0)
            yield Object(type="response.output_text.delta", delta="finish ")
            assert output == [7]
            yield Object(type="response.output_text.delta", delta="done")
            yield Object(type="response.completed", response=response)

    create = Mock(return_value=Stream())
    with patch.object(main, "OpenAI", return_value=Object(responses=Object(create=create))), patch.dict("os.environ", {"OPENAI_API_KEY": "fixture"}):
        model = main.OpenAIModelClient("gpt-5.4", thinking_mode="low")
    observer = ModelOutputObserver(lambda count, _draft: output.append(count))
    assert model.complete_messages_with_progress("system", MESSAGES, updates.append, on_output=observer) == "finish done"
    assert updates == ["Checking the workspace."]
    assert output == [7, 11]
    assert create.call_args.kwargs["stream"] is True
    assert "max_output_tokens" not in create.call_args.kwargs
    assert create.call_args.kwargs["reasoning"] == {"effort": "low", "summary": "auto"}
    assert model.get_last_metrics()["usage"]["input_tokens"] == 12
    assert model.get_last_metrics()["response_headers"]["x-request-id"] == "response-id"


def test_gemini_keeps_thoughts_out_of_command_output_and_retains_trailing_usage():
    updates = []
    output = []

    def chunk(text, *, thought=False, finish_reason=None):
        candidate = Object(content=Object(parts=[Object(text=text, thought=thought)]), finish_reason=finish_reason)
        return Object(candidates=[candidate], usage_metadata=None, prompt_feedback=None)

    def generate(**kwargs):
        assert kwargs["config"].thinking_config.include_thoughts
        assert kwargs["config"].thinking_config.thinking_budget == 1024
        yield chunk("Checking the workspace.", thought=True)
        assert updates == ["Checking the workspace."]
        yield chunk("finish ")
        assert output == [7]
        yield chunk("done", finish_reason="STOP")
        yield Object(candidates=[], usage_metadata={"thoughts_token_count": 10}, prompt_feedback=None)

    model = main.GeminiModelClient.__new__(main.GeminiModelClient)
    model.client = Object(models=Object(generate_content_stream=generate))
    model.model = "gemini-2.5-flash"
    model.thinking_mode = "low"
    sdk_types = Object(ThinkingConfig=Object, GenerateContentConfig=Object)
    with patch.object(main, "genai_types", sdk_types):
        observer = ModelOutputObserver(lambda count, _draft: output.append(count))
        assert model.complete_messages_with_progress("system", MESSAGES, updates.append, on_output=observer) == "finish done"
    assert updates == ["Checking the workspace."]
    assert output == [7, 11]
    assert model.get_last_metrics()["usage"]["reasoning_tokens"] == 10
    assert model.get_last_metrics()["response"]["candidates"][0]["finish_reason"] == "STOP"


def test_worker_emits_wait_summary_and_actual_mcp_dispatch_in_order(tmp_path):
    progress = []

    class Model:
        def complete_messages_with_progress(self, system, messages, on_summary, *, on_output):
            on_summary("I need the Blender catalog.")
            assert [item["action_type"] for item in progress] == ["model_call_start", "model_reasoning_summary"]
            assert on_output.preview_strategy
            on_output.report(11, ["mcp"])
            return 'mcp {"operation":"list","server":"Blender"}\nfinish done'

    worker = TreeLoopPlannerWorker(model=Model(), root=tmp_path, max_turns=1, verbose=False,
                                   provider="gemini", model_name="gemini-4-hydrogen", thinking_mode="low")
    worker.on_step_callback = progress.append

    def dispatch(root, action):
        assert progress[-1]["summary"] == "Querying Blender MCP"
        assert progress[-1]["action_type"] == "tool_start"
        assert action["server"] == "Blender"
        return {"ok": True, "tools": []}

    with patch("live_test_loop.invoke_mcp", side_effect=dispatch) as invoke:
        assert worker.run_task("Inspect Blender MCP").ok
    invoke.assert_called_once()
    assert progress[0]["reasoning_effort"] == {"requested": "low", "parameter": "thinking_level", "value": "low"}
    assert worker.usage_accounting_state()["recent_turns"][-1]["reasoning_effort"] == progress[0]["reasoning_effort"]
    assert [item["turn"] for item in progress[:2]] == [1, 1]
    assert progress[1]["thought"] == "I need the Blender catalog."
    events = [item["action_type"] for item in progress]
    assert progress[2]["output_chars"] == 11
    assert progress[2]["draft_strategy"] == ["mcp"]
    assert events[:7] == ["model_call_start", "model_reasoning_summary", "model_output_progress",
                          "model_response_received", "model_call_finish", "tool_start", "tool_finish"]


def test_planner_emits_wait_summary_and_output_before_plan_returns(tmp_path):
    progress = []

    class Model:
        def complete_messages_with_progress(self, system, messages, on_summary, *, on_output):
            assert [item["action_type"] for item in progress] == ["model_call_start"]
            on_summary("Working through the goal order.")
            assert not on_output.preview_strategy
            on_output.report(13, [])
            assert [item["action_type"] for item in progress] == [
                "model_call_start", "model_reasoning_summary", "model_output_progress",
            ]
            return "plan response"

    planner = PlannerAgent(Model(), Object(root=tmp_path, provider="openai", model="gpt-5.4", thinking_mode="auto"),
                           lambda: None, lambda value: {})
    planner.on_model_progress = progress.append
    assert planner._planner_complete("system", "Plan this work.") == "plan response"
    assert progress[-1]["action_type"] == "model_call_finish"
    assert progress[0]["reasoning_effort"] == {"requested": "auto", "parameter": None, "value": None}
    assert progress[1]["thought"] == "Working through the goal order."
    assert progress[2]["output_chars"] == 13
    assert all(item["turn"] == 1 and item["phase"] == "Planning" for item in progress)


def test_response_counts_are_throttled_and_only_change_when_text_arrives():
    counts = []
    with patch("model_output_progress.time.monotonic", return_value=10) as clock:
        output = ModelOutputProgress(ModelOutputObserver(lambda count, _draft: counts.append(count)))
        output.add("abc")
        output.add("de")
        assert counts == [3]
        clock.return_value = 11
        output.add("f")
        assert counts == [3, 6]
        output.add("g")
        output.finish()
        output.finish()
        assert counts == [3, 6, 7]


def test_worker_reports_rejected_output_and_labels_same_turn_retry(tmp_path):
    progress = []
    rejected = "write /repo/draft.txt This is an unaccepted response.\nfinish done"

    class Model:
        calls = 0

        def complete_messages_with_progress(self, system, messages, on_summary, *, on_output):
            self.calls += 1
            output = (rejected, "write /repo/draft.txt Accepted response.", "cat /repo/draft.txt\nfinish done")[self.calls - 1]
            on_output.report(len(output), ["write"] if self.calls <= 2 else ["cat", "finish"])
            return output

        def get_last_metrics(self):
            reason = "MALFORMED_FUNCTION_CALL" if self.calls == 1 else "STOP"
            return {"response": {"candidates": [{"finish_reason": reason}]}}

    worker = TreeLoopPlannerWorker(model=Model(), root=tmp_path, max_turns=2, verbose=False)
    worker.on_step_callback = progress.append
    assert worker.run_task("Test the recovery path").ok
    assert (tmp_path / "draft.txt").read_text() == "Accepted response."
    assert progress[1]["output_chars"] == len(rejected)
    assert progress[2]["action_type"] == "model_call_finish"
    assert progress[2]["ok"] is False
    assert progress[2]["summary"] == f"Turn 1 response rejected · malformed tool call ({len(rejected)} characters)."
    assert progress[3]["action_type"] == "model_call_start"
    assert progress[3]["turn"] == 1
    assert progress[3]["phase"] == "Retrying after malformed tool call"


def test_draft_strategy_projects_incremental_headers_and_skips_operands():
    draft = DraftStrategy()
    assert draft.add(">>t") == []
    assert draft.add("h: secret note\n>>pl: plan note\ns1: cat /repo/private.py, mcp {\"operation\":\"list\"}\n") == [
        ">>th:", ">>pl:", "s1: cat, mcp",
    ]
    assert draft.add("s1 -> s2: write /repo/file <<") == []
    assert draft.add("<\ncat /repo/pretend-command\n>>pl: payload annotation\n") == ["s1 -> s2: write"]
    assert draft.add(">>>\ns3: shell cat <<'PAYLOAD'\n") == ["s3: shell"]
    assert draft.add("s4: patch not-an-action\nPAYLOAD\n```python\ncat('code content')\n```\n") == []
    assert draft.add("finish private summary") == []
    assert draft.finish() == ["finish"]


def test_draft_headers_are_reported_before_the_response_finishes():
    events = []
    output = ModelOutputProgress(ModelOutputObserver(lambda count, draft: events.append((count, draft)), preview_strategy=True))
    output.add("s1: wr")
    assert events[-1] == (6, [])
    output.add("ite /repo/file <<<\n")
    assert events[-1][1] == ["s1: write"]
    output.add("s2: cat this-is-file-content\n>>>\ns2: stat /repo/file")
    assert not any("cat" in line for _, draft in events for line in draft)
    output.finish()
    assert events[-1][1] == ["s2: stat"]


def test_worker_projects_buffered_response_before_dispatch(tmp_path):
    progress = []

    class Model:
        def complete_messages_with_progress(self, system, messages, on_summary, *, on_output):
            return ">>pl: inspect Blender\ns1: mcp {\"operation\":\"list\",\"server\":\"Blender\"}"

    worker = TreeLoopPlannerWorker(model=Model(), root=tmp_path, max_turns=1, verbose=False)
    worker.on_step_callback = progress.append
    with patch("live_test_loop.invoke_mcp", return_value={"ok": True, "tools": []}) as invoke:
        worker.run_task("Inspect Blender MCP")
    invoke.assert_called_once()
    start = next(i for i, event in enumerate(progress) if event["action_type"] == "tool_start")
    assert [entry for item in progress[:start] for entry in item.get("draft_strategy", [])] == [
        ">>pl:", "s1: mcp",
    ]


def test_streamed_read_and_git_headers_do_not_dispatch_until_final_response(tmp_path):
    progress = []
    (tmp_path / "note.txt").write_text("sample\n")

    class Model:
        def complete_messages_with_progress(self, system, messages, on_summary, *, on_output):
            on_output.report(60, [">>th:", ">>pl:", "read-line-range", "git"])
            assert [event["action_type"] for event in progress] == ["model_call_start", "model_output_progress"]
            return "read-line-range /repo/note.txt 1-1\ngit status\nfinish inspected"

    worker = TreeLoopPlannerWorker(model=Model(), root=tmp_path, max_turns=1, verbose=False)
    worker.on_step_callback = progress.append
    with patch.object(worker, "_exec_git_status", return_value="branch=main"):
        assert worker.run_task("Inspect the repository").ok
    received = next(i for i, event in enumerate(progress) if event["action_type"] == "model_response_received")
    assert [event["summary"] for event in progress[received + 1:] if event["action_type"] == "tool_start"] == [
        "Reading /repo/note.txt", "Running git status",
    ]


@pytest.mark.parametrize("annotation", [">>th:", ">>pl:"])
def test_new_thought_or_plan_stops_at_a_command_boundary_and_ignores_heredoc_tags(annotation):
    observer = ModelOutputObserver(lambda *_: None, preview_strategy=True)
    output = ModelOutputProgress(observer)
    wave = ">>th: begin\n>>pl: write\nwrite /repo/sample.txt <<<\n>>th: file content\n>>pl: file content\n>>>"
    output.add(wave + "\n" + annotation[:3])
    with pytest.raises(CommandWaveBoundary) as boundary:
        output.add(annotation[3:])
    assert boundary.value.text == wave


def test_gemini_legacy_loop_executes_the_first_wave_before_next_call(tmp_path):
    (tmp_path / "note.txt").write_text("A real tool result.\n")
    calls = []
    closed = []

    def chunk(text):
        candidate = Object(content=Object(parts=[Object(text=text, thought=False)]), finish_reason=None)
        return Object(candidates=[candidate], usage_metadata=None, prompt_feedback=None)

    def generate(**kwargs):
        calls.append(kwargs)
        assert not hasattr(kwargs["config"], "max_output_tokens")
        if len(calls) == 1:
            try:
                yield chunk(">>th: inspect\n>>pl: read the file\ncat /repo/note.txt\n>>t")
                yield chunk("h:")
                pytest.fail("The harness consumed more than one streamed command wave.")
            finally:
                closed.append(True)
        else:
            assert closed == [True]
            assert "A real tool result." in str(kwargs["contents"])
            yield chunk("finish Inspected the file.")

    model = main.GeminiModelClient.__new__(main.GeminiModelClient)
    model.client = Object(models=Object(generate_content_stream=generate))
    model.model = "gemini-4-hydrogen"
    model.thinking_mode = "high"
    model.command_response_framing = False
    sdk_types = Object(ThinkingConfig=Object, GenerateContentConfig=Object)
    with patch.object(main, "genai_types", sdk_types):
        worker = TreeLoopPlannerWorker(model=model, root=tmp_path, max_turns=2, verbose=False)
        assert worker.run_task("Inspect the file").ok
    assert len(calls) == 2


def test_gemini_protocol_recovery_still_runs_after_the_host_rejects_annotations(tmp_path):
    (tmp_path / "note.txt").write_text("Actual tool result.\n")
    summary = f"{BEGIN_COMMANDS}\n>>th: Public summary\ncat /repo/summary.txt\n{END_COMMANDS}"
    calls = []
    progress = []

    def chunk(text, *, thought=False, finish_reason=None):
        part = Object(text=text, thought=thought)
        return Object(candidates=[Object(content=Object(parts=[part]), finish_reason=finish_reason)],
                      usage_metadata=None, prompt_feedback=None)

    def generate(**kwargs):
        calls.append(kwargs)
        config = kwargs["config"]
        assert config.thinking_config.include_thoughts
        assert config.response_mime_type == "application/json"
        assert config.response_json_schema == COMMAND_BLOCK_SCHEMA
        assert summary not in str(kwargs["contents"])
        yield chunk(summary, thought=True)
        if len(calls) == 1:
            yield chunk(json.dumps({"command_block": f"{BEGIN_COMMANDS}\n>>th: inspect\nwrite /repo/rejected.txt draft\n>>th: reconsider\n"}))
        elif len(calls) == 2:
            assert "COMMAND_ANNOTATION_LIMIT" in config.system_instruction
            yield chunk(json.dumps({"command_block": f"{BEGIN_COMMANDS}\nwrite /repo/rejected.txt retry\n{END_COMMANDS}"}))
            yield chunk("", finish_reason="MALFORMED_RESPONSE")
        elif len(calls) == 3:
            assert "MALFORMED_RESPONSE" in config.system_instruction
            payload = json.dumps({"command_block": f"{BEGIN_COMMANDS}\ncat /repo/note.txt\n{END_COMMANDS}"})
            yield chunk(payload[:payload.index(END_COMMANDS)])
            assert any(event["action_type"] == "model_output_progress" and "cat" in event["draft_strategy"] for event in progress)
            yield chunk(payload[payload.index(END_COMMANDS):], finish_reason="STOP")
        else:
            assert "Actual tool result." in str(kwargs["contents"])
            yield chunk(json.dumps({"command_block": f"{BEGIN_COMMANDS}\nfinish Inspected the file.\n{END_COMMANDS}"}), finish_reason="STOP")

    model = main.GeminiModelClient.__new__(main.GeminiModelClient)
    model.client = Object(models=Object(generate_content_stream=generate))
    model.model = "gemini-4-hydrogen"
    model.thinking_mode = "low"
    with patch.object(main, "genai_types", Object(ThinkingConfig=Object, GenerateContentConfig=Object)):
        worker = TreeLoopPlannerWorker(model=model, root=tmp_path, max_turns=2, verbose=False)
        worker.on_step_callback = progress.append
        assert worker.run_task("Inspect the file").ok

    assert len(calls) == 4
    assert not (tmp_path / "rejected.txt").exists()
    assert [event["thought"] for event in progress if event["action_type"] == "model_reasoning_summary"] == [summary] * 4
    assert worker.usage_accounting_state()["recent_turns"][-1]["command_encoding"] == "json_schema"


def test_invalid_gemini_command_envelope_retries_with_the_same_schema(tmp_path):
    calls = 0
    response = {"command_block": f"{BEGIN_COMMANDS}\nwrite /repo/note.txt intended\nfinish Done\n{END_COMMANDS}"}

    def generate(**kwargs):
        nonlocal calls
        calls += 1
        config = kwargs["config"]
        assert config.response_json_schema == COMMAND_BLOCK_SCHEMA
        assert not hasattr(config.thinking_config, "include_thoughts")  # Summaries are off for this turn.
        text = json.dumps(response)
        if calls == 1:
            text += " trailing output"
        else:
            assert "did not contain a complete JSON command envelope" in config.system_instruction
            assert "A completion message cannot replace it" in config.system_instruction
            assert not (tmp_path / "note.txt").exists()
        candidate = Object(content=Object(parts=[Object(text=text, thought=False)]), finish_reason="STOP")
        yield Object(candidates=[candidate], usage_metadata=None, prompt_feedback=None)

    model = main.GeminiModelClient.__new__(main.GeminiModelClient)
    model.client = Object(models=Object(generate_content_stream=generate))
    model.model = "gemini-4-hydrogen"
    model.thinking_mode = "none"
    with patch.object(main, "genai_types", Object(ThinkingConfig=Object, GenerateContentConfig=Object)):
        loop = TreeLoop(model=model, workspace_root=tmp_path, max_turns=1, verbose=False)
        assert loop.run("Write the note").finished

    assert calls == 2
    assert (tmp_path / "note.txt").read_text() == "intended"


@pytest.mark.parametrize("finish_reason", ["MAX_TOKENS", "CONTINUATION"])
def test_generation_limit_never_dispatches_partial_commands_and_suspends_after_one_retry(tmp_path, finish_reason):
    class Model:
        calls = 0

        def complete_messages(self, system, messages):
            self.calls += 1
            if self.calls == 2:
                assert "No commands from it were executed" in messages[-1]["content"]
                assert f"generation token limit ({finish_reason})" in system
                assert messages[-2] == {"role": "assistant", "content": "write /repo/partial.txt unsafe"}
            return "write /repo/partial.txt unsafe"

        def get_last_metrics(self):
            return {"response": {"candidates": [{"finish_reason": finish_reason}]}}

    model = Model()
    worker = TreeLoopPlannerWorker(model=model, root=tmp_path, max_turns=5, verbose=False)
    outcome = worker.run_task("Write the file")
    assert model.calls == 2
    assert outcome.suspended
    assert not (tmp_path / "partial.txt").exists()


@pytest.mark.parametrize("provider", ["gemini", "openai"])
def test_generation_cap_replays_the_visible_partial_on_the_same_transport(tmp_path, provider):
    (tmp_path / "note.txt").write_text("This result is evidence.\n")
    partial = f"{BEGIN_COMMANDS}\n>>th: editing\n>>pl: write the note\nwrite /repo/rejected.txt <<<\n" + "discarded text\n" * 600
    public_summary = "Provider reasoning summary"
    calls = 0

    def completion(messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            return partial
        if calls == 2:
            assert ("assistant", partial) in messages
            assert public_summary not in str(messages)
            assert "The preceding assistant message is your interrupted response" in messages[-1][1]
            assert not (tmp_path / "rejected.txt").exists()
            assert BEGIN_COMMANDS in messages[-1][1]
            return f"{BEGIN_COMMANDS}\n>>th: use existing evidence\n>>pl: inspect the note\ncat /repo/note.txt\n{END_COMMANDS}"
        assert "This result is evidence." in str(messages)
        assert ("assistant", partial) not in messages
        return f"{BEGIN_COMMANDS}\nfinish The note was inspected.\n{END_COMMANDS}"

    if provider == "gemini":
        def generate(**kwargs):
            config = kwargs["config"]
            assert config.thinking_config.include_thoughts
            assert config.response_json_schema == COMMAND_BLOCK_SCHEMA
            assert not hasattr(config, "max_output_tokens")
            messages = [("assistant" if item.role == "model" else item.role, item.parts[0].text) for item in kwargs["contents"]]
            text = completion(messages)
            if calls == 1:
                thought = Object(content=Object(parts=[Object(text=public_summary, thought=True)]), finish_reason=None)
                yield Object(candidates=[thought], usage_metadata=None, prompt_feedback=None)
            encoded = json.dumps({"command_block": text})
            if calls == 1:
                encoded = encoded[:-2]  # The generation limit arrived inside the JSON string.
            candidate = Object(content=Object(parts=[Object(text=encoded, thought=False)]),
                               finish_reason="MAX_TOKENS" if calls == 1 else "STOP")
            yield Object(candidates=[candidate], usage_metadata=None, prompt_feedback=None)

        model = main.GeminiModelClient.__new__(main.GeminiModelClient)
        model.client = Object(models=Object(generate_content_stream=generate))
        model.model = "gemini-4-hydrogen"
        model.thinking_mode = "high"
    else:
        class Stream:
            response = Object(headers={})

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def __iter__(self):
                yield from self.events

        def generate(**kwargs):
            assert kwargs["stream"] is True
            assert kwargs["reasoning"]["summary"] == "auto"
            assert "max_output_tokens" not in kwargs
            text = completion([(item["role"], item["content"]) for item in kwargs["input"]])
            delta = Object(type="response.output_text.delta", delta=text)
            stream = Stream()
            if calls == 1:
                summary = Object(type="response.reasoning_summary_text.delta", delta=public_summary, item_id="r1", summary_index=0)
                cap = Object(status="incomplete", incomplete_details=Object(reason="max_output_tokens"), usage={})
                stream.events = [summary, delta, Object(type="response.incomplete", response=cap)]
            else:
                reply = Object(status="completed", output_text=text, usage={})
                stream.events = [delta, Object(type="response.completed", response=reply)]
            return stream

        model = main.OpenAIModelClient.__new__(main.OpenAIModelClient)
        model.client = Object(responses=Object(create=generate))
        model.model = "gpt-5.4"
        model.thinking_mode = "low"
        model.verbosity = "medium"
        model.set_prompt_cache_key("")

    sdk_types = Object(ThinkingConfig=Object, GenerateContentConfig=Object, Part=Object, Content=Object)
    with patch.object(main, "genai_types", sdk_types):
        worker = TreeLoopPlannerWorker(model=model, root=tmp_path, max_turns=2, verbose=False)
        assert worker.run_task("Inspect the note").ok
    assert calls == 3
    assert not (tmp_path / "rejected.txt").exists()


def test_capped_retry_requires_the_complete_command_wave(tmp_path):
    class Model:
        calls = 0

        def complete_messages(self, system, messages):
            self.calls += 1
            return "write /repo/missing-annotations.txt contents"

        def get_last_metrics(self):
            return {"response": {"candidates": [{"finish_reason": "MAX_TOKENS" if self.calls == 1 else "STOP"}]}}

    model = Model()
    worker = TreeLoopPlannerWorker(model=model, root=tmp_path, max_turns=5, verbose=False)
    result = worker.run_task("Write the file")
    assert result.suspended
    assert "did not produce a complete thought, plan, and command wave" in result.final_message
    assert model.calls == 2
    assert not (tmp_path / "missing-annotations.txt").exists()
