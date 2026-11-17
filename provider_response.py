"""Provider response diagnostics without recording hidden reasoning or content."""

from dataclasses import dataclass
from typing import Any


class ProviderResponseError(RuntimeError):
    pass


PROTOCOL_FAILURES = {"MALFORMED_FUNCTION_CALL", "UNEXPECTED_TOOL_CALL"}


@dataclass
class ProviderResponseRecovery:
    """Separate bounded protocol repair from retrying a blank completion."""
    empty_retries: int = 0
    protocol_retries: int = 0

    @property
    def retries(self) -> int:
        return self.empty_retries + self.protocol_retries

    @staticmethod
    def failure_reason(output: str, metadata: dict) -> str:
        for candidate in metadata.get("candidates", []):
            reason = str(candidate.get("finish_reason", "")).rsplit(".", 1)[-1].upper()
            if reason in PROTOCOL_FAILURES:
                return reason
        return "EMPTY_RESPONSE" if not output.strip() else ""

    def claim_retry(self, reason: str) -> bool:
        if self.retries >= 3:
            return False
        if reason in PROTOCOL_FAILURES:
            if self.protocol_retries >= 2:
                return False
            self.protocol_retries += 1
        else:
            if self.empty_retries >= 1:
                return False
            self.empty_retries += 1
        return True

    @staticmethod
    def instruction(reason: str) -> str:
        if reason in PROTOCOL_FAILURES:
            return (
                f"The provider rejected the previous response with {reason}; no commands from it were executed. "
                "Native function calling is disabled. The host executes ordinary text commands. "
                "For this recovery response only, emit one small executable text command using the existing host grammar. "
                "Omit annotations, narration, Markdown fences, and native function-call wrappers. "
                "Continue the current goal from retained source; do not restart discovery. Use finish only when complete."
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
