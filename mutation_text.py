"""Lossless text operands for the beta mutation command grammar."""

from __future__ import annotations

import ast
import json
import re


TEXT_MUTATION_VERBS = {"write", "replace-lines", "replace_lines", "patch"}
PARSE_ERROR_PREFIX = "__command_parse_error__ "


def is_text_mutation(command: str) -> bool:
    parts = command.lstrip().split(None, 1)
    return bool(parts and parts[0].lower() in TEXT_MUTATION_VERBS)


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


def parse_patch_payload(text: str) -> tuple[str, str, str]:
    path, payload = split_path_payload(text)
    arrows = unquoted_arrows(payload)
    if len(arrows) != 1:
        raise ValueError("patch requires exactly one unquoted ' -> ' separator; quote operands containing arrows")
    offset = arrows[0]
    search = _decode_patch_operand(payload[:offset])
    replacement = _decode_patch_operand(payload[offset + 4:])
    if not search:
        raise ValueError("patch requires non-empty search text")
    return path, search, replacement
