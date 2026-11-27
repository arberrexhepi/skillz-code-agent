"""Lossless text operands for the beta mutation command grammar."""

from __future__ import annotations

import ast
import json
import re


TEXT_MUTATION_VERBS = {"write", "replace-lines", "replace_lines", "patch", "edit-symbol", "edit-batch", "replace-regex"}
PARSE_ERROR_PREFIX = "__command_parse_error__ "
_LITERAL_PATCH_HEADER = re.compile(r"(?P<path>\S+)[ \t]+--literal[ \t]+<<<[ \t]*", re.I)
_PATCH_REPLACE_LINE = "=== REPLACE ==="
_LITERAL_PATCH_HINT = (
    "Use `patch <path> --literal <<<`, then exact search text, a standalone "
    "`=== REPLACE ===` line, exact replacement text, and a standalone `>>>` line."
)


def is_text_mutation(command: str) -> bool:
    parts = command.lstrip().split(None, 1)
    return bool(parts and parts[0].lower() in TEXT_MUTATION_VERBS)


def is_literal_patch(command: str) -> bool:
    header = command.partition("\n")[0].split(None, 1)
    return len(header) == 2 and header[0].lower() == "patch" and bool(_LITERAL_PATCH_HEADER.fullmatch(header[1]))


def split_path_payload(text: str) -> tuple[str, str]:
    """Consume exactly one header separator; everything after it is payload."""
    match = re.fullmatch(r"(?P<path>\S+)[ \t\n](?P<payload>.*)", text.lstrip(), re.DOTALL)
    if match is None:
        raise ValueError("Expected <path> followed by one separator and a text payload")
    return match.group("path"), match.group("payload")


def parse_range_payload(text: str) -> tuple[str, int, int, str]:
    for header in (
        r"(?P<path>\S+):(?P<start>\d+)-(?P<end>\d+)",
        r"(?P<path>\S+)[ \t]+(?P<start>\d+)-(?P<end>\d+)",
        r"(?P<path>\S+)[ \t]+(?P<start>\d+)[ \t]+(?P<end>\d+)",
    ):
        match = re.fullmatch(header + r"(?:[ \t\n](?P<content>.*))?", text.lstrip(), re.DOTALL)
        if match is not None:
            return (
                match.group("path"), int(match.group("start")), int(match.group("end")),
                match.group("content") or "",
            )
    raise ValueError("replace-lines requires a <start>-<end> range")


def unquoted_arrows(text: str) -> list[int]:
    """Locate separators, excluding arrows inside quoted source or templates."""
    positions: list[int] = []
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = ""
        elif char in {'"', "'", "`"}:
            quote = char
        elif text.startswith(" -> ", index):
            positions.append(index)
    if quote:
        raise ValueError("Unterminated quoted patch operand; use a complete JSON string for arbitrary text")
    return positions


def _decode_patch_operand(text: str) -> str:
    stripped = text.strip()
    if not stripped or stripped[0] not in {'"', "'"}:
        return text
    quote = stripped[0]
    escaped = False
    closing = -1
    for index, char in enumerate(stripped[1:], start=1):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            closing = index
            break
    if closing != len(stripped) - 1:
        return text  # A raw expression beginning with a string, not a quoted operand.
    try:
        value = json.loads(stripped) if quote == '"' else ast.literal_eval(stripped)
    except (ValueError, SyntaxError) as exc:
        raise ValueError("Invalid quoted patch operand; encode newlines and backslashes as a JSON string") from exc
    if not isinstance(value, str):
        raise ValueError("Patch operands must be strings")
    return value


def _parse_inline_patch(payload: str) -> tuple[str, str]:
    arrows = unquoted_arrows(payload)
    if len(arrows) != 1:
        raise ValueError("Inline patch requires exactly one unquoted ' -> ' separator.")
    offset = arrows[0]
    return _decode_patch_operand(payload[:offset]), _decode_patch_operand(payload[offset + 4:])


def _parse_literal_patch(block: str) -> tuple[str, str]:
    lines = block.removesuffix("\n").split("\n")
    if lines[-1].strip() != ">>>":
        raise ValueError("Literal patch requires a closing '>>>' line.")
    body = lines[:-1]
    separators = [index for index, line in enumerate(body) if line.strip() == _PATCH_REPLACE_LINE]
    if len(separators) != 1:
        raise ValueError(f"Literal patch requires exactly one standalone '{_PATCH_REPLACE_LINE}' line.")
    split_at = separators[0]
    return "\n".join(body[:split_at]), "\n".join(body[split_at + 1:])


def parse_patch_payload(text: str) -> tuple[str, str, str]:
    header, _, block = text.lstrip().partition("\n")
    try:
        if literal := _LITERAL_PATCH_HEADER.fullmatch(header):
            path = literal.group("path")
            search, replacement = _parse_literal_patch(block)
        else:
            path, payload = split_path_payload(text)
            search, replacement = _parse_inline_patch(payload)
        if not search:
            raise ValueError("Patch requires non-empty search text.")
    except ValueError as exc:
        raise ValueError(f"{exc} {_LITERAL_PATCH_HINT}") from exc
    return path, search, replacement
