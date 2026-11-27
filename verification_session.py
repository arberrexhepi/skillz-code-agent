"""Progress checkpoints for MCP-backed verification."""

from dataclasses import dataclass
from typing import Sequence

MAX_MCP_VERIFICATION_TURNS = 24
MAX_MCP_VERIFICATION_CALLS = 44
_ADDITIONAL_TURNS = 4
_ADDITIONAL_CALLS = 8
_MAX_ADDITIONAL_CHECKS = 6
_CHECK_ATTEMPT_WINDOW = 3
MAX_MCP_CHECK_ATTEMPTS = 24


def mcp_session_limits(check_count: int) -> tuple[int, int]:
    additional = min(_MAX_ADDITIONAL_CHECKS, max(0, check_count - 1))
    return (
        MAX_MCP_VERIFICATION_TURNS + additional * _ADDITIONAL_TURNS,
        MAX_MCP_VERIFICATION_CALLS + additional * _ADDITIONAL_CALLS,
    )


@dataclass
class McpCheckBudget:
    attempts: int = 0
    checkpoint: int = _CHECK_ATTEMPT_WINDOW
    stop_reason: str = ""
    _new_evidence: bool = False

    def review(self, *, new_evidence: bool) -> None:
        self._new_evidence |= new_evidence
        if self.attempts < self.checkpoint:
            return
        if self.attempts >= MAX_MCP_CHECK_ATTEMPTS:
            self.stop_reason = f"This check reached its MCP attempt limit ({self.attempts}/{MAX_MCP_CHECK_ATTEMPTS})."
        elif not self._new_evidence:
            self.stop_reason = f"This check reached its MCP checkpoint without new evidence ({self.attempts} attempts)."
        else:
            self.checkpoint = min(MAX_MCP_CHECK_ATTEMPTS, self.attempts + _CHECK_ATTEMPT_WINDOW)
            self._new_evidence = False


class McpVerificationSession:
    def __init__(self, *, turn_checkpoint: int, call_checkpoint: int, max_turns: int, max_calls: int) -> None:
        self.turn_checkpoint = turn_checkpoint
        self.call_checkpoint = call_checkpoint
        self.max_turns = max_turns
        self.max_calls = max_calls
        self.stop_reason = ""
        self.fixture_retry_offered = False
        self.coverage_until_turn = 0
        self._seen_milestones: set[tuple[str, str]] = set()
        self._seen_observations: set[tuple[str, str]] = set()
        self._new_evidence: list[str] = []
        self.decisions: list[dict] = []

    def observe(self, scope: str, milestone: str, evidence: str) -> bool:
        key = (scope, milestone.casefold())
        observation = (scope, evidence.casefold())
        if key in self._seen_milestones or observation in self._seen_observations:
            return False
        self._seen_milestones.add(key)
        self._seen_observations.add(observation)
        self._new_evidence.append(f"{scope} — {evidence}")
        return True

    def review(self, *, turns: int, calls: int, stalled: bool = False, requested_calls: int = 0,
               fixture_blocker: str = "", undispatched: Sequence[str] = ()) -> None:
        if self.stop_reason:
            return
        stalled = stalled and turns >= self.coverage_until_turn
        call_checkpoint = calls + requested_calls > self.call_checkpoint if requested_calls else calls >= self.call_checkpoint
        turn_checkpoint = not requested_calls and turns >= self.turn_checkpoint
        if not (turn_checkpoint or call_checkpoint or stalled):
            return

        if not requested_calls and turns >= self.max_turns:
            reason = f"The MCP verification session reached its overall turn limit ({turns}/{self.max_turns})."
        elif calls >= self.max_calls or calls + requested_calls > self.max_calls:
            reason = (
                f"The MCP verification session reached its overall tool-call limit ({calls}/{self.max_calls})."
                if calls >= self.max_calls
                else f"The next {requested_calls} calls would exceed the MCP verification session limit ({calls}/{self.max_calls} used)."
            )
        elif (stalled or not self._new_evidence) and fixture_blocker and not requested_calls and not self.fixture_retry_offered:
            self.fixture_retry_offered = True
            self.call_checkpoint = max(self.call_checkpoint, calls + 1)
            self.decisions.append({
                "turn": turns, "calls": calls, "decision": "continue",
                "reason": "One focused retry for the observed fixture blocker.",
                "evidence": [fixture_blocker],
            })
            return
        elif undispatched and not self.coverage_until_turn and not fixture_blocker and (stalled or not self._new_evidence):
            # Leave room for the pending setup and assertion without opening another session.
            self.coverage_until_turn = min(self.max_turns, turns + _ADDITIONAL_TURNS)
            self.turn_checkpoint = self.coverage_until_turn
            self.call_checkpoint = min(self.max_calls, calls + _ADDITIONAL_CALLS)
            self.decisions.append({
                "turn": turns, "calls": calls, "decision": "continue",
                "reason": f"One focused window to start undispatched checks ({', '.join(undispatched)}).",
            })
            self._new_evidence.clear()
            return
        elif stalled:
            reason = "The MCP verification session stalled after five turns without new check evidence or setup progress."
        elif not self._new_evidence:
            reason = "The MCP verification session stopped at a progress checkpoint without new check evidence or setup progress."
        else:
            reason = "New verification evidence earned another window."
            self.turn_checkpoint = min(self.max_turns, max(self.turn_checkpoint, turns + _ADDITIONAL_TURNS))
            self.call_checkpoint = min(self.max_calls, max(self.call_checkpoint, calls + _ADDITIONAL_CALLS))
            self.decisions.append({
                "turn": turns, "calls": calls, "decision": "continue", "reason": reason,
                "evidence": self._new_evidence[-3:],
            })
            self._new_evidence.clear()
            return
        if requested_calls and call_checkpoint and calls + requested_calls <= self.max_calls:
            reason += f" The next operation needed {requested_calls} tool calls with {calls}/{self.call_checkpoint} used at the checkpoint."
        self.stop_reason = reason
        self.decisions.append({"turn": turns, "calls": calls, "decision": "stop", "reason": reason})

    def snapshot(self, *, calls: int) -> dict:
        return {
            "turn_checkpoint": self.turn_checkpoint, "call_checkpoint": self.call_checkpoint,
            "max_turns": self.max_turns, "max_calls": self.max_calls, "calls_used": calls,
            "decisions": list(self.decisions),
        }
