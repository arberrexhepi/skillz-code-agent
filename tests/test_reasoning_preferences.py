import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_subscription import _model_reasoning_efforts
from live_test_loop import TreeLoopPlannerWorker
from main import AgentConfig, BaseModelClient, WorkingFolderAgent
from planner import DISCOVERY_MODES, PlannerAgent
from reasoning_effort import REASONING_PREFERENCES_FILE, available_reasoning_efforts, repo_reasoning_effort


def save(root, efforts, *, provider="gemini", model="gemini-4-hydrogen"):
    (root / REASONING_PREFERENCES_FILE).write_text(json.dumps({
        "version": 1, "profiles": [{"provider": provider, "model": model, "efforts": efforts}],
    }))


@pytest.mark.parametrize("tree", [True, False])
def test_planner_dispatch_applies_the_repo_effort_to_each_phase(tmp_path, monkeypatch, tree):
    monkeypatch.setenv("SKILLZ_OBSERVABILITY_PATH", str(tmp_path / "trace.md"))
    provider, model_name = "gemini", "gemini-4-hydrogen"
    save(tmp_path, {"all": "medium", "planning": "high", "discovery": "low", "verification": "xhigh"})

    class Model(BaseModelClient):
        command_response_framing = False
        complete_messages_with_progress = None

        def __init__(self):
            self.thinking_mode = "auto"
            self.efforts = []

        def complete(self, system, prompt):
            self.efforts.append(self.thinking_mode)
            return "finish Done" if tree else '{"action":{"type":"finish","message":"Done"}}'

        def complete_messages(self, system, messages):
            return self.complete(system, "")

    model = Model()
    config = AgentConfig(provider, model_name, tmp_path, Path(__file__).resolve().parents[1] / "agent_tools.py", quiet=True)
    worker = (TreeLoopPlannerWorker(model=model, root=tmp_path, max_turns=2, verbose=False, provider=provider, model_name=model_name)
              if tree else WorkingFolderAgent(model, config))
    planner = PlannerAgent(model, config, lambda: worker, json.loads)
    planner._planner_complete("System", "Plan the task")
    parameters = {"task": "Report findings", "steering": "", "goal_fact_keys": [], "preserve_context": False}
    planner._run_worker_task(**parameters, discovery_budget=DISCOVERY_MODES["quick"])
    planner._run_worker_task(**parameters)
    planner._run_worker_task(**parameters, verification_checks=["The fixture loads"])

    assert model.efforts == ["high", "low", "medium", "xhigh"]
    assert model.thinking_mode == "auto"

    save(tmp_path, {"all": "low", "goals": "high"})
    planner._run_worker_task(**parameters)
    assert model.efforts[-1] == "high"


def test_repo_scope_matches_provider_and_model_and_drops_an_unsupported_override(tmp_path):
    save(tmp_path, {"all": "medium", "discovery": "minimal"})
    model = BaseModelClient()
    model.thinking_mode = "low"
    with repo_reasoning_effort(tmp_path, "gemini", "gemini-4-hydrogen", "discovery"):
        assert model.thinking_mode == "medium"
    with repo_reasoning_effort(tmp_path, "gemini", "gemini-4-carbon", "discovery"):
        assert model.thinking_mode == "low"
    with repo_reasoning_effort(tmp_path, "openai", "gemini-4-hydrogen", "discovery"):
        assert model.thinking_mode == "low"


def test_model_catalog_and_codex_advertised_efforts():
    assert available_reasoning_efforts("gemini", "gemini-4-hydrogen") == ("auto", "low", "medium", "high", "xhigh")
    assert available_reasoning_efforts("gemini", "gemini-4-carbon") == ("auto", "low", "medium", "high")
    assert available_reasoning_efforts("gemini", "gemini-2.0-flash") == ("auto",)
    assert available_reasoning_efforts("openai", "gpt-6.2-sol") == ("auto", "low", "medium", "high", "xhigh", "max")
    assert _model_reasoning_efforts({"data": [
        {"model": "codex-model", "supportedReasoningEfforts": [{"reasoningEffort": "none"}, {"reasoningEffort": "max"}]},
        {"model": "hidden", "hidden": True, "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
    ]}) == {"codex-model": ["auto", "none", "max"]}
