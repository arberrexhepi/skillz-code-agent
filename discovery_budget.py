"""Bounded discovery and explicit, user-approved continuation shared by both workers."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any
from uuid import uuid4
import json

MAX_EXTENSION_TURNS = 10


@dataclass
class DiscoveryBudget:
    mode_key: str
    mode_label: str
    max_tool_calls: int
    tool_calls_used: int = 0
    max_turns: int = 0
    turns_used: int = 0
    pending_extension: dict[str, Any] | None = None
    resuming: bool = False

    @property
    def remaining_tool_calls(self) -> int:
        return max(0, self.max_tool_calls - self.tool_calls_used)

    @property
    def exhausted(self) -> bool:
        return self.tool_calls_used >= self.max_tool_calls

    @property
    def remaining_turns(self) -> int:
        return max(0, self.max_turns - self.turns_used)

    def snapshot(self) -> dict[str, Any]:
        return {
            "mode": self.mode_key,
            "tool_calls_used": self.tool_calls_used,
            "max_tool_calls": self.max_tool_calls,
            "turns_used": self.turns_used,
            "max_turns": self.max_turns,
            "pending_extension": deepcopy(self.pending_extension),
        }

    def request_extension(self, action: dict[str, Any]) -> dict[str, Any]:
        if self.pending_extension is not None:
            raise ValueError("A discovery extension is already waiting for the user.")
        if not self.exhausted and not (self.max_turns > 0 and self.turns_used >= self.max_turns):
            raise ValueError("Use the existing discovery budget first; request an extension at the action limit or on the last turn.")
        turns = action.get("additional_turns")
        if type(turns) is not int or not 1 <= turns <= MAX_EXTENSION_TURNS:
            raise ValueError(f"additional_turns must be an integer from 1 to {MAX_EXTENSION_TURNS}.")
        details: dict[str, Any] = {}
        for key in ("reason", "proposal", "findings"):
            value = action.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > 8000:
                raise ValueError(f"{key} must be non-empty text of at most 8000 characters.")
            details[key] = value.strip()
        ambiguities = action.get("ambiguities")
        if (not isinstance(ambiguities, list) or not 1 <= len(ambiguities) <= 10
                or any(not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in ambiguities)):
            raise ValueError("ambiguities must contain 1 to 10 non-empty questions, each at most 2000 characters.")
        self.pending_extension = {
            **details, "request_id": uuid4().hex, "additional_turns": turns,
            "additional_tool_calls": turns, "ambiguities": [item.strip() for item in ambiguities],
            "mode": self.mode_key, "turns_used": self.turns_used, "turns_max": self.max_turns,
            "tool_calls_used": self.tool_calls_used, "tool_calls_max": self.max_tool_calls,
        }
        return deepcopy(self.pending_extension)

    def accept_extension(self, request_id: str) -> None:
        if self.pending_extension is None or self.pending_extension["request_id"] != request_id:
            raise ValueError("This discovery extension is no longer pending. Review the current request.")
        turns = self.pending_extension["additional_turns"]
        self.max_turns = self.turns_used + turns
        self.max_tool_calls += turns
        self.pending_extension = None
        self.resuming = True

    def guidance(self, *, tree_commands: bool = False) -> str:
        example = {"additional_turns": 2, "reason": "Why the ambiguity affects the plan",
                   "proposal": "Which focused checks the extra turns will perform",
                   "ambiguities": ["The unresolved question"], "findings": "Findings collected so far"}
        command = ("request-discovery-extension " + json.dumps(example) if tree_commands else
                   json.dumps({"type": "request_discovery_extension", **example}))
        return (
            f"Discovery budget: {self.turns_used}/{self.max_turns} model turns; "
            f"{self.tool_calls_used}/{self.max_tool_calls} tool-backed actions used. "
            "Discovery remains read-only. On the LAST allotted turn, finish with your findings or request an extension. "
            "At the action limit, finish or request an extension instead of attempting more tools. "
            "Only request extra turns for material unresolved ambiguity with a concrete, bounded investigation proposal. "
            f"Request 1-{MAX_EXTENSION_TURNS} turns; each approved turn also adds one tool-action slot. "
            "The user must decide; never assume approval. Submit the request as the only action/command in its turn: "
            + command
        )
