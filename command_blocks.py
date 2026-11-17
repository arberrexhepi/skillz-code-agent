"""Keep explicit command blocks and multiline shell operands opaque to the agent grammar."""

from __future__ import annotations

from dataclasses import dataclass
import re

from mutation_text import PARSE_ERROR_PREFIX

STRATEGY_LABEL_PATTERN = r"s\w+(?:\.\w+)*"
_SHELL_HEADER = re.compile(
    rf"^(?:(?:[\w.,\s]+->\s*)?{STRATEGY_LABEL_PATTERN}\s*:[ \t]*)?shell(?:[ \t]+(?P<script>.*))?$", re.I,
)
_MARKDOWN_PREFIX = re.compile(r"^(?:[-*+]\s+|\d{1,3}[.)]\s+|\[[ xX]\]\s+)")


def _header(line: str) -> str:
    candidate = line.lstrip()
    if candidate.startswith("> "):
        candidate = candidate[2:].lstrip()
    candidate = _MARKDOWN_PREFIX.sub("", candidate, count=1).lstrip()
    label = re.match(r"^(?:command|action)\s*:\s*(.+)$", candidate, re.I)
    return label.group(1).lstrip() if label else candidate


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


def group_command_blocks(lines: list[str]) -> list[str]:
    """Preserve raw delimiters for extraction; only the final parser removes <<< >>>."""
    grouped = []
    index = 0
    try:
        while index < len(lines):
            first = lines[index]
            index += 1
            parts = [first]
            if first.rstrip().endswith("<<<"):
                while index < len(lines) and lines[index].strip() != ">>>":
                    parts.append(lines[index])
                    index += 1
                if index == len(lines):
                    raise ValueError("Unterminated heredoc: missing >>>; no commands dispatched")
                parts.append(lines[index])
                index += 1
            else:
                header = _SHELL_HEADER.match(_header(first))
                if header:
                    scan = _ShellScan()
                    pending = scan.feed(header.group("script") or "")
                    while pending or scan.incomplete:
                        if scan.incomplete:
                            if index == len(lines):
                                raise ValueError("Unterminated multiline shell command; close its quotes or continuation before issuing another agent command")
                            parts.append(lines[index])
                            pending.extend(scan.feed(lines[index]))
                            index += 1
                        else:
                            delimiter, strip_tabs = pending.pop(0)
                            while index < len(lines):
                                line = lines[index]
                                parts.append(line)
                                index += 1
                                if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                                    break
                            else:
                                raise ValueError(f"Unterminated shell heredoc: missing {delimiter!r}; no commands dispatched")
            grouped.append("\n".join(parts))
    except ValueError as exc:
        return [PARSE_ERROR_PREFIX + str(exc)]
    return grouped
