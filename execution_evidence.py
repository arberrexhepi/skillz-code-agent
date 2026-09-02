"""Classify repository observations without confusing reads with stagnation."""

from __future__ import annotations

import hashlib
import json
import posixpath
import shlex
from dataclasses import dataclass

from discovery.dispatch import DISCOVERY_ACTION_TYPES
from tree_commands import CommandResult


REPOSITORY_READ_COMMANDS = {
    "cat", "read-line-range", "symbols", "find-symbol", "find", "grep", "ls", "stat", "repo-map",
}
SOURCE_READ_COMMANDS = {"cat", "read-line-range", "read_file", "read_symbol"}
EMPTY_SEARCH_OUTPUTS = {"", "(empty)", "(no matches)", "(no symbol matches)", "(no symbols found)"}


@dataclass(frozen=True)
class RepositoryObservation:
    fingerprint: str
    empty_search: bool
    source_path: str = ""


def repository_observation(command: str, result: CommandResult) -> RepositoryObservation | None:
    """Fingerprint returned evidence, not query wording or strategy step labels."""
    if not result.ok:
        return None
    normalized = str(command or "").strip()
    if normalized.startswith("[") and "] " in normalized:
        normalized = normalized.split("] ", 1)[1]
    parts = normalized.split(None, 1)
    verb = parts[0] if parts else ""
    rest = parts[1] if len(parts) > 1 else ""
    verb = verb.replace("_", "-")
    action = result.tool_action or {}
    action_type = str(action.get("type", ""))
    if action_type in DISCOVERY_ACTION_TYPES:
        kind = action_type
        path = str(action.get("file_path") or action.get("path") or ".")
    elif result.command_type == "read" and verb in REPOSITORY_READ_COMMANDS:
        kind = verb
        try:
            path = shlex.split(rest)[0]
        except (ValueError, IndexError):
            return None
        if path.startswith("/") and path != "/repo" and not path.startswith("/repo/"):
            return None  # Facts and other virtual mounts are not repository exploration.
    else:
        return None
    if kind == "cat":
        path = path.split(":", 1)[0]
    path = posixpath.normpath(path.removeprefix("/repo/").removeprefix("repo/"))
    if path in {"/repo", "repo"}:
        path = "."
    source_path = path if kind in SOURCE_READ_COMMANDS else ""
    output = str(result.output or "")
    if output.startswith(("[not found:", "[directory:", "[line range out of bounds:")):
        return None
    evidence: object = output
    empty = not source_path and output.strip().lower() in EMPTY_SEARCH_OUTPUTS
    # Raw source can itself be JSON (including an empty list); it is still a read.
    if not source_path or action_type in DISCOVERY_ACTION_TYPES:
        try:
            payload = json.loads(output)
        except (ValueError, TypeError):
            payload = None
        if isinstance(payload, dict):
            if payload.get("ok") is False or "error" in payload:
                return None
            if "hits" in payload:
                evidence = payload["hits"]
                empty = not evidence
            elif "files" in payload:
                evidence = payload["files"]
                empty = not evidence
            else:
                evidence = {key: value for key, value in payload.items() if key not in {"query", "topic", "summary"}}
        elif isinstance(payload, list):
            evidence = payload
            empty = not payload
    serialized = json.dumps([path, evidence], sort_keys=True, ensure_ascii=False)
    return RepositoryObservation(hashlib.sha256(serialized.encode()).hexdigest(), empty, source_path)
