"""Translate Playground's reasoning setting at the provider boundary."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
from typing import Iterator, TypedDict


REASONING_EFFORTS = ("auto", "none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
REASONING_PREFERENCES_FILE = ".agent-reasoning.json"
_PHASE_EFFORT: ContextVar[str | None] = ContextVar("phase_reasoning_effort", default=None)
_STANDARD_LEVELS = ("low", "medium", "high")


def available_reasoning_efforts(provider: str, model: str) -> tuple[str, ...]:
    """Advertise documented levels on transports without live effort metadata."""
    tag = model.lower().rsplit("/", 1)[-1]
    levels: tuple[str, ...] = ()
    if provider == "gemini":
        if tag.startswith(("gemini-4-hydrogen", "gemini-4-neon")):
            levels = (*_STANDARD_LEVELS, "xhigh")
        elif tag.startswith(("gemini-3-flash", "gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemini-3.6-flash", "gemini-robotics-er-2")):
            levels = ("minimal", *_STANDARD_LEVELS)
        elif tag.startswith("gemini-3-pro"):
            levels = ("low", "high")
        elif tag.startswith(("gemini-4-argon", "gemini-4-carbon", "gemini-3.8-flash", "gemini-3.1-pro", "gemini-2.5")):
            levels = _STANDARD_LEVELS
    elif provider in {"openai", "codex-subscription"}:
        if tag.startswith(("gpt-6.1-terra", "gpt-6-luna", "gpt-5.6")):
            levels = ("none", *_STANDARD_LEVELS, "xhigh", "max")
        elif tag.startswith(("gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-sol")):
            levels = (*_STANDARD_LEVELS, "xhigh", "max")
        elif tag.startswith(("gpt-6-astra", "gpt-6-sol", "gpt-5.3-codex")):
            levels = (*_STANDARD_LEVELS, "xhigh")
        elif tag.startswith(("gpt-5.4-pro", "gpt-5.5-pro", "gpt-5.2-pro")):
            levels = ("medium", "high", "xhigh")
        elif tag.startswith(("gpt-5.4", "gpt-5.5", "gpt-5.2")):
            levels = ("none", *_STANDARD_LEVELS, "xhigh")
        elif tag.startswith("gpt-5.1"):
            levels = ("none", *_STANDARD_LEVELS)
        elif tag.startswith("gpt-5"):
            levels = ("minimal", *_STANDARD_LEVELS)
        elif tag.startswith(("o3", "o4-mini", "o1")):
            levels = _STANDARD_LEVELS
        if provider == "codex-subscription":
            levels = tuple(level for level in levels if level not in {"none", "minimal"})
    return ("auto", *levels)


def active_reasoning_effort(default: str) -> str:
    return _PHASE_EFFORT.get() or default


@contextmanager
def repo_reasoning_effort(root: Path, provider: str, model: str, phase: str) -> Iterator[None]:
    """Pin a repo's phase setting during a dispatch without mutating shared clients."""
    effort = None
    try:
        file = root / REASONING_PREFERENCES_FILE
        if file.is_file() and not file.is_symlink():
            with file.open(encoding="utf-8") as source:
                data = json.load(source)
            if isinstance(data, dict) and data.get("version") == 1 and isinstance(data.get("profiles"), list):
                allowed = REASONING_EFFORTS if provider == "codex-subscription" else available_reasoning_efforts(provider, model)
                for profile in data["profiles"]:
                    if not isinstance(profile, dict) or (profile.get("provider"), profile.get("model")) != (provider, model):
                        continue
                    preferences = profile.get("efforts")
                    if isinstance(preferences, dict):
                        effort = next((preferences[key] for key in (phase, "all") if preferences.get(key) in allowed), None)
                    break
    except (OSError, ValueError, RecursionError):
        pass
    token = _PHASE_EFFORT.set(effort)
    try:
        yield
    finally:
        _PHASE_EFFORT.reset(token)


class ReasoningEffort(TypedDict):
    requested: str
    parameter: str | None
    value: str | int | None


def gemini_thinking_api(model: str) -> str | None:
    tag = model.lower().rsplit("/", 1)[-1]
    if tag.startswith("gemini-2.5"):
        return "thinking_budget"
    if tag.startswith(("gemini-1", "gemini-2.0")):
        return None
    # Current and future GenerateContent thinking models use a level.
    return "thinking_level"


def resolve_reasoning_effort(provider: str, model: str, requested: str | None) -> ReasoningEffort:
    effort = str(requested or "auto").strip().lower() or "auto"
    settings: ReasoningEffort = {"requested": effort, "parameter": None, "value": None}
    if effort == "auto":
        return settings

    if provider == "gemini":
        parameter = gemini_thinking_api(model)
        if parameter == "thinking_budget":
            budgets = {"none": 0, "minimal": 256, "low": 1024, "medium": 4096, "high": 8192, "xhigh": 8192}
            settings.update(parameter=parameter, value=budgets.get(effort, -1))
        elif parameter:
            settings.update(parameter=parameter, value="low" if effort == "none" else effort)
    elif provider in {"openai", "meta", "local"}:
        settings.update(parameter="reasoning.effort", value=effort)
    elif provider == "codex-subscription":
        settings.update(parameter="model_reasoning_effort", value=effort)
    return settings
