"""Report response size and a draft outline of TreeLoop command headers."""

from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Callable

from command_blocks import CommandResponseScan, STRATEGY_LABEL_PATTERN, normalize_command_header
from provider_response import CommandResponseLimit, CommandWaveBoundary
from tree_commands import ANNOTATION_TAGS, COMMAND_VERBS, parse_annotation, parse_strategy_step

MAX_COMMAND_RESPONSE_CHARS = 96_000
WAVE_ANNOTATIONS = frozenset({">>th:", ">>pl:"})


class DraftStrategy:
    """Project complete command headers without forwarding their operands."""

    def __init__(self, *, require_frame: bool = False) -> None:
        self._scan = CommandResponseScan(require_frame=require_frame)
        self._parts: list[str] = []
        self._lost_boundary = False
        self._offset = 0
        self.header_offsets: list[int] = []

    def add(self, text: str) -> list[str]:
        self.header_offsets.clear()
        entries: list[str] = []
        lines = text.split("\n")
        for line in lines[:-1]:
            self._parts.append(line)
            entry = self._take_line()
            if entry:
                entries.append(entry)
        self._parts.append(lines[-1])
        return entries

    def finish(self) -> list[str]:
        self.header_offsets.clear()
        entry = self._take_line()
        return [entry] if entry else []

    def _take_line(self) -> str:
        line = "".join(self._parts)
        self._parts.clear()
        entry = self._project_line(line.removesuffix("\r"))
        if entry:
            self.header_offsets.append(self._offset)
        self._offset += len(line) + 1
        return entry

    def pending_wave_annotation_offset(self) -> int | None:
        if self._lost_boundary or not self._scan.accepts_header():
            return None
        remaining = 128
        prefix = []
        for part in self._parts:
            prefix.append(part[:remaining])
            remaining -= len(prefix[-1])
            if remaining == 0:
                break
        line = normalize_command_header("".join(prefix))
        return self._offset if line[:5].lower() in WAVE_ANNOTATIONS else None

    def _project_line(self, line: str) -> str:
        if self._lost_boundary:
            return ""
        try:
            if not self._scan.feed(line):
                return ""
        except ValueError:
            self._lost_boundary = True
            return ""

        line = normalize_command_header(line).rstrip()
        annotation = parse_annotation(line)
        if annotation is not None and annotation.tag in ANNOTATION_TAGS:
            return f">>{annotation.tag}:"

        step = parse_strategy_step(line)
        commands = step.commands if step else [line]
        if self._scan.incomplete:
            commands = commands[:1]
        names = [parts[0].lower() for command in commands if (parts := command.split(None, 1))
                 and parts[0].lower() in COMMAND_VERBS]
        if not names:
            return ""
        if step is None:
            return names[0]

        def is_label(label: str) -> bool:
            return len(label) <= 32 and re.fullmatch(STRATEGY_LABEL_PATTERN, label) is not None

        if not is_label(step.label):
            return ", ".join(names)
        dependencies = ", ".join(label for label in step.depends_on if is_label(label))
        targets = ", ".join(label for label in step.targets if is_label(label))
        heading = f"{dependencies} -> {step.label}" if dependencies else step.label
        names_text = ", ".join(names)
        return f"{heading}: {names_text}" + (f" -> {targets}" if targets else "")


@dataclass(frozen=True)
class ModelOutputObserver:
    report: Callable[[int, list[str]], None]
    preview_strategy: bool = False
    preserve_request_context: bool = False
    require_command_frame: bool = False


class ModelOutputProgress:
    """Publish new headers immediately and coalesce count-only updates."""

    def __init__(self, observer: ModelOutputObserver | None) -> None:
        self._observer = observer
        self._require_frame = bool(observer and observer.require_command_frame)
        self._draft = DraftStrategy(require_frame=self._require_frame) if observer and observer.preview_strategy else None
        self._received = 0
        self._reported = 0
        self._reported_at = float("-inf")
        self._annotations: set[str] = set()
        self._has_action = False
        self._has_wave_commands = False
        self._fragments: list[str] = []

    def add(self, text: str) -> None:
        if self._observer is None:
            return
        self._received += len(text)
        entries = []
        if self._draft is not None:
            retained = text[:max(0, MAX_COMMAND_RESPONSE_CHARS - (self._received - len(text)))]
            self._fragments.append(retained)
            entries = self._draft.add(retained)
            self._check_headers(entries)
            if not self._require_frame and self._has_wave_commands and (offset := self._draft.pending_wave_annotation_offset()) is not None:
                self._end_wave(offset)
            if self._received > MAX_COMMAND_RESPONSE_CHARS:
                raise CommandResponseLimit("COMMAND_OUTPUT_LIMIT", self._received, self._has_action)
        if entries or time.monotonic() - self._reported_at >= 1:
            self._publish(entries)

    def finish(self) -> None:
        entries = self._draft.finish() if self._draft is not None else []
        self._check_headers(entries)
        self._publish(entries)

    def _end_wave(self, offset: int) -> None:
        raise CommandWaveBoundary("".join(self._fragments)[:offset].strip())

    def _check_headers(self, entries: list[str]) -> None:
        for index, entry in enumerate(entries):
            if entry in WAVE_ANNOTATIONS:
                if not self._require_frame and self._has_wave_commands:
                    assert self._draft is not None
                    self._end_wave(self._draft.header_offsets[index])
                if entry in self._annotations:
                    raise CommandResponseLimit("COMMAND_ANNOTATION_LIMIT", self._received, self._has_action, annotation=entry)
                self._annotations.add(entry)
            elif not entry.startswith(">>") and entry != "finish":
                self._has_action = True
                self._has_wave_commands |= self._annotations == WAVE_ANNOTATIONS

    def _publish(self, entries: list[str]) -> None:
        if self._observer is not None and (entries or self._received > self._reported):
            self._reported = self._received
            self._reported_at = time.monotonic()
            self._observer.report(self._reported, entries)
