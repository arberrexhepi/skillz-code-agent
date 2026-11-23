"""Decode Gemini's single-field JSON response for Playground command turns."""

import json
import re

from model_output_progress import MAX_COMMAND_RESPONSE_CHARS
from provider_response import CommandResponseLimit


COMMAND_BLOCK_SCHEMA = {
    "type": "object",
    "properties": {"command_block": {"type": "string"}},
    "required": ["command_block"],
    "additionalProperties": False,
}

COMMAND_BLOCK_INSTRUCTION = (
    "\nResponse encoding: Return one JSON object using the supplied response schema. "
    "Put the complete literal Playground text, including both boundary lines, inside the command_block string. "
    "The contents of that string are ordinary host commands. Format rules for Playground text apply inside the field."
)

_PREFIX = re.compile(r'\A[ \t\r\n]*\{[ \t\r\n]*"command_block"[ \t\r\n]*:[ \t\r\n]*"')
# Defer unfinished escapes, including a UTF-16 pair split across stream chunks.
_STRING_PREFIX = re.compile(
    r'(?:[^"\\\x00-\x1f\ud800-\udfff]+|\\["\\/bfnrt]|'
    r'\\u[dD][89aAbB][0-9a-fA-F]{2}\\u[dD][c-fC-F][0-9a-fA-F]{2}|'
    r'\\u(?![dD][89a-fA-F])[0-9a-fA-F]{4})*'
)
_MAX_WIRE_CHARS = 12 * MAX_COMMAND_RESPONSE_CHARS + 4096


def _command_object(pairs: list[tuple[str, object]]) -> dict:
    if len(pairs) != 1 or pairs[0][0] != "command_block":
        raise ValueError("Expected exactly one command_block field")
    return dict(pairs)


class StructuredCommandResponse:
    """Preview decoded string fragments and validate the entire envelope at EOF."""

    def __init__(self) -> None:
        self._raw: list[str] = []
        self._wire_chars = 0
        self._decoded: list[str] = []
        self._pending = ""
        self._opened = False
        self._closed = False

    def add(self, fragment: str) -> str:
        self._wire_chars += len(fragment)
        if self._wire_chars > _MAX_WIRE_CHARS:
            raise CommandResponseLimit("COMMAND_OUTPUT_LIMIT", self._wire_chars, bool(self._decoded))
        self._raw.append(fragment)
        if self._closed:
            return ""
        self._pending += fragment
        if not self._opened:
            match = _PREFIX.match(self._pending)
            if match is None:
                # An unusually formatted but valid key can still be decoded at EOF.
                if len(self._pending) > 4096:
                    self._closed = True
                    self._pending = ""
                return ""
            self._opened = True
            self._pending = self._pending[match.end():]

        match = _STRING_PREFIX.match(self._pending)
        assert match is not None
        self._pending = self._pending[match.end():]
        if self._pending.startswith('"') or len(self._pending) >= 12:
            self._closed = True
            self._pending = ""
        if not match.group():
            return ""
        decoded = json.loads('"' + match.group() + '"')
        self._decoded.append(decoded)
        return decoded

    def partial_text(self) -> str:
        return "".join(self._decoded)

    @property
    def received_text(self) -> bool:
        return self._wire_chars > 0

    def finish(self) -> str:
        value = json.loads("".join(self._raw), object_pairs_hook=_command_object)
        command_block = value.get("command_block") if isinstance(value, dict) else None
        if not isinstance(command_block, str):
            raise ValueError("Expected command_block to be a string")
        command_block.encode("utf-8")
        return command_block
