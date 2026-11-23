"""Keep explicit command blocks and multiline shell operands opaque to the agent grammar."""

from __future__ import annotations

from dataclasses import dataclass
import re

from mutation_text import PARSE_ERROR_PREFIX

STRATEGY_LABEL_PATTERN = r"s[0-9]+(?:\.[A-Za-z0-9_]+)*"
BEGIN_COMMANDS = "BEGIN_PLAYGROUND_COMMANDS"
END_COMMANDS = "END_PLAYGROUND_COMMANDS"
COMMAND_FRAME_RULE = f"Wrap the executable response in exact standalone {BEGIN_COMMANDS} and {END_COMMANDS} lines."
_COMMAND_FENCES = {None, "", "text", "plaintext", "playground", "commands"}
_SHELL_HEADER = re.compile(
    rf"^(?:(?:[\w.,\s]+->\s*)?{STRATEGY_LABEL_PATTERN}\s*:[ \t]*)?"
    r"(?:verify[ \t]+[\w,-]+[ \t]+)?shell(?:[ \t]+(?P<script>.*))?$", re.I,
)
_MARKDOWN_PREFIX = re.compile(r"^(?:[-*+]\s+|\d{1,3}[.)]\s+|\[[ xX]\]\s+)")


def normalize_command_header(line: str) -> str:
    candidate = line.lstrip()
    if candidate.startswith("> "):
        candidate = candidate[2:].lstrip()
    candidate = _MARKDOWN_PREFIX.sub("", candidate, count=1).lstrip()
    label = re.match(r"^(?:command|action)\s*:\s*(.+)$", candidate, re.I | re.S)
    if label:
        candidate = label.group(1).lstrip()
    trimmed = candidate.rstrip()
    if len(trimmed) >= 2 and trimmed[0] == "`" and trimmed[-1] == "`" and not trimmed.startswith("```"):
        return trimmed[1:-1].lstrip()
    return candidate


def _delimiter(line: str, offset: int) -> tuple[str, int]:
    while offset < len(line) and line[offset] in " \t":
        offset += 1
    start = offset
    quote = ""
    word = []
    while offset < len(line):
        char = line[offset]
        if quote:
            if char == quote:
                quote = ""
            elif char == "\\" and quote == '"' and offset + 1 < len(line) and line[offset + 1] in '$`"\\':
                offset += 1
                word.append(line[offset])
            else:
                word.append(char)
        elif char in " \t;&|<>()":
            break
        elif char in "'\"":
            quote = char
        elif char == "\\" and offset + 1 < len(line):
            offset += 1
            word.append(line[offset])
        else:
            word.append(char)
        offset += 1
    if quote or offset == start:
        raise ValueError("Invalid or missing shell heredoc delimiter; no commands dispatched")
    return "".join(word), offset


@dataclass
class _ShellScan:
    quote: str = ""
    continuation: bool = False
    arithmetic_depth: int = 0

    @property
    def incomplete(self) -> bool:
        return bool(self.quote or self.continuation or self.arithmetic_depth)

    def feed(self, line: str) -> list[tuple[str, bool]]:
        heredocs = []
        self.continuation = False
        offset = 0
        code_end = len(line)
        while offset < len(line):
            char = line[offset]
            if char == "\\" and self.quote != "'":
                if offset + 1 == len(line):
                    self.continuation = True
                offset += 2
                continue
            if self.quote:
                if char == self.quote:
                    self.quote = ""
                offset += 1
                continue
            if char in "'\"`":
                self.quote = char
            elif char == "#" and (offset == 0 or line[offset - 1].isspace()):
                code_end = offset
                break
            elif self.arithmetic_depth:
                if char == "(":
                    self.arithmetic_depth += 1
                elif char == ")":
                    self.arithmetic_depth -= 1
            elif line.startswith("$((", offset) or line.startswith("((", offset):
                self.arithmetic_depth = 2
                offset += 3 if line.startswith("$", offset) else 2
                continue
            elif line.startswith("<<<", offset):
                offset += 3
                continue
            elif line.startswith("<<", offset):
                offset += 2
                strip_tabs = offset < len(line) and line[offset] == "-"
                if strip_tabs:
                    offset += 1
                delimiter, offset = _delimiter(line, offset)
                heredocs.append((delimiter, strip_tabs))
                continue
            offset += 1
        if not self.quote and line[:code_end].rstrip().endswith(("&&", "||", "|")):
            self.continuation = True
        return heredocs


class CommandBlockScan:
    """Identify command headers while keeping multiline operands opaque."""

    def __init__(self) -> None:
        self._tree_heredoc = False
        self._shell = _ShellScan()
        self._shell_heredocs: list[tuple[str, bool]] = []

    @property
    def incomplete(self) -> bool:
        return self._tree_heredoc or self._shell.incomplete or bool(self._shell_heredocs)

    def feed(self, line: str) -> bool:
        """Return whether the line starts a new block."""
        if self._tree_heredoc:
            if line.strip() == ">>>":
                self._tree_heredoc = False
            return False
        if self._shell.incomplete:
            self._shell_heredocs.extend(self._shell.feed(line))
            return False
        if self._shell_heredocs:
            delimiter, strip_tabs = self._shell_heredocs[0]
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                self._shell_heredocs.pop(0)
            return False

        self._tree_heredoc = line.rstrip().endswith("<<<")
        if not self._tree_heredoc:
            header = _SHELL_HEADER.match(normalize_command_header(line))
            if header:
                self._shell = _ShellScan()
                self._shell_heredocs = self._shell.feed(header.group("script") or "")
        return True

    def require_complete(self) -> None:
        if self._tree_heredoc:
            raise ValueError("Unterminated heredoc: missing >>>; no commands dispatched")
        if self._shell.incomplete:
            raise ValueError("Unterminated multiline shell command; close its quotes or continuation before issuing another agent command")
        if self._shell_heredocs:
            delimiter, _ = self._shell_heredocs[0]
            raise ValueError(f"Unterminated shell heredoc: missing {delimiter!r}; no commands dispatched")


class CommandResponseScan:
    """Recognize a command frame and preserve the grammar's multiline operands."""

    def __init__(self, *, require_frame: bool) -> None:
        self._require_frame = require_frame
        self._opened = False
        self._closed = False
        self._fence: str | None = None
        self._blocks = CommandBlockScan()

    @property
    def incomplete(self) -> bool:
        return self._blocks.incomplete

    def accepts_header(self) -> bool:
        inside_frame = self._opened and not self._closed
        return (not self._require_frame or inside_frame) and self._fence in _COMMAND_FENCES and not self.incomplete

    def feed(self, line: str) -> bool | None:
        """Return True for a header, False for its body, and None for excluded text."""
        if self.incomplete:
            return self._blocks.feed(line)

        normalized = normalize_command_header(line).rstrip()
        if normalized.startswith("```"):
            self._fence = None if self._fence is not None else normalized[3:].lower()
            return None

        if self._require_frame and self._fence is None:
            if line == BEGIN_COMMANDS:
                if self._opened:
                    raise ValueError("Expected one command frame per response")
                self._opened = True
                return None
            if line == END_COMMANDS:
                if not self._opened or self._closed:
                    raise ValueError(f"Unexpected {END_COMMANDS}; expected one complete command frame")
                self._closed = True
                return None

        return self._blocks.feed(line) if self.accepts_header() else None

    def require_complete(self) -> None:
        self._blocks.require_complete()
        if self._require_frame and not self._opened:
            raise ValueError(f"Missing {BEGIN_COMMANDS}; no commands dispatched")
        if self._require_frame and not self._closed:
            raise ValueError(f"Missing {END_COMMANDS}; no commands dispatched")


def group_command_blocks(lines: list[str], *, response_scan: CommandResponseScan | None = None) -> list[str]:
    """Preserve raw delimiters for extraction; only the final parser removes <<< >>>."""
    grouped: list[str] = []
    parts: list[str] = []
    scan = response_scan if response_scan is not None else CommandBlockScan()
    try:
        for line in lines:
            header = scan.feed(line)
            if header is None:
                continue
            if header and parts:
                grouped.append("\n".join(parts))
                parts = []
            parts.append(line)
        scan.require_complete()
        if parts:
            grouped.append("\n".join(parts))
    except ValueError as exc:
        return [PARSE_ERROR_PREFIX + str(exc)]
    return grouped
