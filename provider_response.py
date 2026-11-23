"""Provider response diagnostics without recording hidden reasoning or content."""

from dataclasses import dataclass
from typing import Any


class ProviderResponseError(RuntimeError):
    def __init__(self, message: str, *, budget_exhausted: bool = False) -> None:
        super().__init__(message)
        self.budget_exhausted = budget_exhausted


PROTOCOL_FAILURES = {"MALFORMED_FUNCTION_CALL", "MALFORMED_RESPONSE", "UNEXPECTED_TOOL_CALL"}
GENERATION_LIMIT_REASONS = {"MAX_TOKENS", "MAX_OUTPUT_TOKENS", "CONTINUATION"}
RESPONSE_BUDGET_FAILURES = GENERATION_LIMIT_REASONS | {
    "COMMAND_ANNOTATION_LIMIT", "COMMAND_OUTPUT_LIMIT", "INCOMPLETE_RECOVERY_WAVE",
}


class CommandResponseLimit(RuntimeError):
    def __init__(self, reason: str, output_chars: int, discarded_action: bool, *, annotation: str = "") -> None:
        super().__init__(reason)
        self.metadata = {"limit_reason": reason, "output_chars": output_chars, "discarded_action": discarded_action}
        if annotation:
            self.metadata["annotation"] = annotation


class CommandWaveBoundary(RuntimeError):
    def __init__(self, text: str) -> None:
        super().__init__("A new thought or plan started after a complete command wave.")
        self.text = text


@dataclass
class ProviderResponseRecovery:
    """Separate bounded protocol repair from retrying a blank completion."""
    empty_retries: int = 0
    protocol_retries: int = 0
    budget_retries: int = 0
    discarded_action: bool = False
    generation_limit_retry: bool = False

    @property
    def retries(self) -> int:
        return self.empty_retries + self.protocol_retries + self.budget_retries

    @staticmethod
    def protocol_failure_reason(metadata: dict) -> str:
        for candidate in (metadata or {}).get("candidates", []):
            reason = str(candidate.get("finish_reason", "")).rsplit(".", 1)[-1].upper()
            if reason in PROTOCOL_FAILURES:
                return reason
        return ""

    @classmethod
    def failure_reason(cls, output: str, metadata: dict) -> str:
        provider_reason = cls.protocol_failure_reason(metadata) or cls.budget_failure_reason(metadata)
        if provider_reason:
            return provider_reason
        if (metadata or {}).get("command_envelope_invalid"):
            return "INVALID_COMMAND_ENVELOPE"
        return "EMPTY_RESPONSE" if not output.strip() else ""

    @staticmethod
    def budget_failure_reason(metadata: dict) -> str:
        reason = str((metadata or {}).get("limit_reason") or "").upper()
        if reason in RESPONSE_BUDGET_FAILURES:
            return reason
        for candidate in (metadata or {}).get("candidates", []):
            reason = str(candidate.get("finish_reason", "")).rsplit(".", 1)[-1].upper()
            if reason in {"MAX_TOKENS", "CONTINUATION"}:
                return reason
        return ""

    def claim_retry(self, reason: str) -> bool:
        if self.retries >= 3:
            return False
        if reason in PROTOCOL_FAILURES:
            # A host guard retry can itself hit a provider protocol failure.
            # A generation-limit retry still gets only one recovery call.
            if self.protocol_retries >= 2 or self.generation_limit_retry:
                return False
            self.protocol_retries += 1
        elif self.budget_retries:
            return False
        elif reason in RESPONSE_BUDGET_FAILURES:
            self.budget_retries += 1
            self.generation_limit_retry = reason in GENERATION_LIMIT_REASONS
        else:
            if self.empty_retries >= 1:
                return False
            self.empty_retries += 1
        return True

    @staticmethod
    def instruction(reason: str, *, has_partial_text: bool = False) -> str:
        if reason in GENERATION_LIMIT_REASONS:
            prior = (
                "The preceding assistant message is your interrupted response. Use it to recover your intent. "
                if has_partial_text else "The provider returned no visible text from the interrupted response. "
            )
            return (
                f"The previous response reached its generation token limit ({reason}). No commands from it were executed. "
                + prior
                + "Start fresh with one short `>>th:` and one short `>>pl:`, then emit a complete, focused Playground OS "
                "command batch. Reissue the first unfinished operation as a self-contained command. "
                "Split large edits into small patches across turns. Wait for tool results before planning another wave. "
                "Use finish only if the results of previously executed commands already satisfy the goal."
            )
        if reason in RESPONSE_BUDGET_FAILURES:
            return (
                f"The previous response exceeded its command or generation budget ({reason}). No commands from it were executed. "
                "Keep the goal and retained source. Emit only the first focused command or independent command batch. "
                "Omit annotations for this retry. End the response and wait for actual results before planning another wave."
            )
        if reason in PROTOCOL_FAILURES:
            return (
                f"The provider rejected the previous response with {reason}; no commands from it were executed. "
                "Native function calling is disabled. The host executes ordinary text commands. "
                "For this recovery response only, emit one small executable text command using the existing host grammar. "
                "Omit annotations, narration, Markdown fences, and native function-call wrappers. "
                "Resume the unfinished operation from retained source. The rejected response did not complete that operation. "
                "Use finish only if previously confirmed results already satisfy the goal."
            )
        if reason == "INVALID_COMMAND_ENVELOPE":
            return (
                "The previous response did not contain a complete JSON command envelope. No commands were executed. "
                "Reissue the next operation using one JSON object with a single command_block string. "
                "Put the complete framed Playground command block inside that string."
            )
        return (
            "The provider returned no executable text; no commands were executed. This is the single empty-response retry. "
            "Keep the current goal and retained source. Return the next bounded text command using the host grammar, "
            "or a completed answer via finish. Do not issue native function calls."
        )


def gemini_response_metadata(response: Any) -> dict:
    def label(value: Any) -> str:
        return str(getattr(value, "value", value) or "")

    candidates = []
    for candidate in (getattr(response, "candidates", None) or []):
        parts = getattr(getattr(candidate, "content", None), "parts", None) or []
        kinds = []
        for part in parts:
            if getattr(part, "thought", False):
                kinds.append("thought")
            else:
                kinds.extend(name for name in ("text", "function_call", "inline_data", "file_data", "executable_code", "code_execution_result")
                             if getattr(part, name, None) is not None)
        candidates.append({"finish_reason": label(getattr(candidate, "finish_reason", None)),
                           "part_types": kinds})
    feedback = getattr(response, "prompt_feedback", None)
    return {"response_id": label(getattr(response, "response_id", None)),
            "block_reason": label(getattr(feedback, "block_reason", None)), "candidates": candidates}
