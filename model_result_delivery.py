"""Model-facing results and the repository evidence retained across compaction.

CLI previews are deliberately short. They must never define what the model has
read: delivery, continuation hints and the reread guard share these packets.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import replace
from typing import Sequence

from execution_evidence import repository_observation
from tree_commands import CommandResult

MODEL_RESULT_CHARS = 30000
READ_CONTEXT_CHARS = 48000
ReadPacket = tuple[str, CommandResult]


def delivered_output(result: CommandResult) -> str:
    return result.output if result.model_output is None else result.model_output


def _continuation(command: str, result: CommandResult, last_line: int | None) -> str:
    observation = repository_observation(command, replace(result, model_output=None))
    if observation is not None and observation.source_path and last_line is not None:
        path = shlex.quote('/repo/' + observation.source_path)
        return f"read-line-range {path} {last_line + 1}-{last_line + 100}"
    return "Repeat with a smaller limit or an exact symbol/line range."


def prepare_model_result(command: str, result: CommandResult, *, limit: int = MODEL_RESULT_CHARS) -> None:
    """Keep normal reads whole, and bound oversized outputs without broken JSON."""
    output = str(result.output or "")
    result.model_output = output
    if len(output) <= limit:
        return
    try:
        payload = json.loads(output)
    except (ValueError, TypeError):
        payload = None
    # Use a separate metadata field so continuation text is never source code.
    if isinstance(payload, (dict, list)):
        if isinstance(payload, list):
            payload = {"items": payload}
        metadata = {"truncated": True, "guidance": "Some result content was omitted. Request a smaller limit or exact line range."}
        payload["model_delivery"] = metadata
        while len(json.dumps(payload, ensure_ascii=False)) > max(200, limit - 600):
            leaves: list[tuple[dict | list, str | int, str]] = []

            def collect(value: object) -> None:
                if isinstance(value, (dict, list)):
                    entries = value.items() if isinstance(value, dict) else enumerate(value)
                    for key, item in entries:
                        if key == "model_delivery":
                            continue
                        if isinstance(item, str) and len(item) > 100:
                            leaves.append((value, key, item))
                        else:
                            collect(item)

            collect(payload)
            if not leaves:
                # Huge metadata/collections: deliver no partial source packet.
                payload = {"model_delivery": metadata}
                break
            parent, key, value = max(leaves, key=lambda item: len(item[2]))
            excess = len(json.dumps(payload, ensure_ascii=False)) - limit
            cutoff = max(0, len(value) - max(excess, len(value) // 4))
            # Retain only complete source lines; long single lines are omitted.
            parent[key] = value[:cutoff].rsplit("\n", 1)[0] if "\n" in value[:cutoff] else ""
        # Report the actual delivered source span, not the original end_line.
        blocks = payload.get("blocks", [])
        if "content" in payload:
            blocks = [payload]
        elif payload.get("hits"):
            blocks = payload["hits"]
        for block in blocks:
            details = block.get("details", block)
            content = str(details.get("content", ""))
            if "content" in details:
                end = int(block.get("start_line", 1)) + len(content.splitlines()) - 1
                block["end_line"] = end
                if "symbol_start_line" in details:
                    details["next_offset"] = end - int(details["symbol_start_line"]) + 1
                metadata["continue_with"] = _continuation(command, result, end)
        result.model_output = json.dumps(payload, ensure_ascii=False)
        return
    # Plain text source: cut at a complete line, never halfway through an edit anchor.
    prefix = output[:max(0, limit - 600)]
    prefix = prefix.rsplit("\n", 1)[0] if "\n" in prefix else ""
    numbered = re.findall(r"^\s*(\d+)\s*[|:]", prefix, re.MULTILINE)
    next_command = _continuation(command, result, int(numbered[-1]) if numbered else None)
    result.model_output = prefix + f"\n[Model delivery truncated; remaining content has NOT been read. Continue: {next_command}]"


def render_packet(command: str, result: CommandResult) -> str:
    return f"[{'✓' if result.ok else '✗'} {result.command_type.upper()}] {command}\n{delivered_output(result)}"


class ReadWorkingSet:
    def __init__(self) -> None:
        self.packets: list[ReadPacket] = []
        self.focus: tuple[str, int] | None = None

    def focus_diagnostic(self, path: str, line: int) -> None:
        self.focus = (path, line)

    def record(self, command: str, result: CommandResult) -> None:
        if repository_observation(command, result) is None:
            return
        # A repeated packet replaces its old position; compaction keeps recent context.
        output = delivered_output(result)
        self.packets = [(cmd, prior) for cmd, prior in self.packets
                        if cmd != command or delivered_output(prior) != output]
        self.packets.append((command, replace(result, tool_action=dict(result.tool_action or {}))))

    def compact(self, *, budget: int = READ_CONTEXT_CHARS) -> Sequence[ReadPacket]:
        kept: list[ReadPacket] = []
        candidates = list(reversed(self.packets))
        if self.focus is not None:
            path, line = self.focus
            for index, packet in enumerate(candidates):
                observation = repository_observation(*packet)
                if observation is not None and observation.source_path == path and any(number == line for number, _ in observation.source_lines):
                    candidates.insert(0, candidates.pop(index))
                    break
        for packet in candidates:
            size = len(render_packet(*packet)) + 2
            if size <= budget:
                kept.append(packet)
                budget -= size
        kept_ids = {id(result) for _, result in kept}
        self.packets = [(cmd, result) for cmd, result in self.packets if id(result) in kept_ids]
        return list(self.packets)

    def manifest(self) -> dict:
        packets = []
        for command, result in self.packets:
            observation = repository_observation(command, result)
            numbers = sorted({number for number, _ in observation.source_lines}) if observation else []
            ranges: list[list[int]] = []
            for number in numbers:
                if ranges and number == ranges[-1][1] + 1:
                    ranges[-1][1] = number
                else:
                    ranges.append([number, number])
            packets.append({"command": command, "source_path": observation.source_path if observation else "",
                            "line_ranges": ranges, "chars": len(render_packet(command, result))})
        return {"budget_chars": READ_CONTEXT_CHARS, "budget_applies_at": "compaction",
                "used_chars": sum(item["chars"] + 2 for item in packets),
                "packet_count": len(packets), "diagnostic_focus": self.focus, "packets": packets[-24:]}

    def invalidate_dependencies(self) -> None:
        if self.focus is not None and "node_modules" in self.focus[0].split("/"):
            self.focus = None
        self.packets = [(command, result) for command, result in self.packets
                        if (obs := repository_observation(command, result)) is None
                        or "node_modules" not in obs.source_path.split("/")]

    def invalidate(self, path: str) -> None:
        if self.focus is not None and self.focus[0] == path:
            self.focus = None
        self.packets = [(command, result) for command, result in self.packets
                        if (obs := repository_observation(command, result)) is None or obs.source_path != path]
