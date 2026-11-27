"""Capture tool-backed verification facts for the final goal."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Sequence

from repository_changes import RepositorySnapshot
from verification_run import (
    _approved_exit_commands, _bounded, _browser_observations_match, _loopback_origin,
    _playwright_page_origin, _playwright_result, _tool_findings,
)


def _source_digest(root: Path, snapshot: RepositorySnapshot) -> str:
    if snapshot.errors:
        return ""
    runtime_files = {"repo_facts.md", "memory_observability.md", ".agent-issue-proposals.json",
                     ".agent-issue-proposals.lock"}
    if trace := os.environ.get("SKILLZ_OBSERVABILITY_PATH"):
        try:
            runtime_files.add(Path(trace).resolve().relative_to(root.resolve()).as_posix())
        except ValueError:
            pass
    contents = [(path, entry[1]) for path, entry in sorted(snapshot.files.items())
                if path not in runtime_files and not path.endswith(".tsbuildinfo")
                and Path(path).parts[0] not in {".playwright-mcp", ".artifact-data"}]
    return hashlib.sha256(json.dumps(contents).encode()).hexdigest()


def _commands(value: object) -> list[tuple[str, ...]]:
    if isinstance(value, str):
        return [tuple(part.split()) for part in value.split("&&")]
    if isinstance(value, list) and all(isinstance(part, str) for part in value):
        return [tuple(value)]
    return []


class GoalVerificationFacts:
    def __init__(self, root: Path, requirements: Sequence[str], goal: str):
        self.root = root
        self.requirements = {f"check-{i}": check for i, check in enumerate(dict.fromkeys(requirements), 1)}
        self.commands = {tuple(command) for requirement in requirements for command in _approved_exit_commands(requirement)}
        self.goal = goal
        self.facts: dict[str, dict] = {}
        self._snapshot: RepositorySnapshot | None = None
        self._preview_origin: tuple[str, str, int] | None = None

    def _stamp(self) -> str:
        self._snapshot = RepositorySnapshot.capture(self.root, previous=self._snapshot)
        return _source_digest(self.root, self._snapshot)

    def before(self, action: dict) -> str:
        kind = action.get("type")
        if kind == "run_shell":
            commands = _commands(action.get("command"))
            if commands and all(command in self.commands for command in commands):
                return self._stamp()
        elif kind == "mcp" and action.get("operation") == "call":
            if action.get("server") != "playwright" or action.get("name") in {
                "browser_evaluate", "browser_run_code", "browser_run_code_unsafe",
            }:
                return self._stamp()
        elif (kind == "run_check" and self.commands) or (
            kind == "artifact_preview" and action.get("operation") in {"mcp", "browser-check"}
        ):
            return self._stamp()
        return ""

    def observe(self, action: dict, ok: bool, output: object, source: str) -> None:
        kind = action.get("type")
        if kind == "artifact_preview" and action.get("operation", "start") in {"start", "restart"}:
            payload = _output_object(output)
            self._preview_origin = _loopback_origin(payload.get("url")) if ok else None
            return
        if not source or any(action.get(key) for key in ("_managed_process", "_validation_unavailable")):
            return
        if kind in {"run_shell", "run_check"}:
            command = action.get("command") if kind == "run_shell" else None
            exit_code = None
            if isinstance(output, dict) and not output.get("managed_process"):
                exit_code = output.get("returncode")
            elif isinstance(output, str):
                if kind == "run_check":
                    header = re.match(r"command=([^\n]+)\nexit_code=(-?\d+)\nissues=\d+\b", output)
                    if header:
                        command, exit_code = header[1], int(header[2])
                elif header := re.match(r"exit_code=(-?\d+)(?:\n|$)", output):
                    exit_code = int(header[1])
            commands = _commands(command)
            if (type(exit_code) is int and commands and all(item in self.commands for item in commands)
                    and source == self._stamp()):
                for item in commands:
                    self._put(source, {"command": list(item)}, "pass" if ok and exit_code == 0 else "fail",
                              f"{' '.join(item)} exited 0." if ok and exit_code == 0 else "The command did not pass.")
            return
        output = _output_object(output)
        if not output:
            return
        managed_browser = kind == "artifact_preview" and action.get("operation") == "browser-check"
        tool = action.get("input") if kind == "artifact_preview" else action
        browser = isinstance(tool, dict) and tool.get("server") == "playwright"
        if browser:
            origin = _playwright_page_origin(output)
            if origin is None or (self._preview_origin is not None and origin != self._preview_origin):
                return
        result = _playwright_result(output) if browser else output
        verdicts: dict = {}
        errors: list[str] = []
        if browser:
            _tool_findings(output, {}, errors, accept_verdicts=False)
        _tool_findings(result, verdicts, errors)
        if not verdicts or source != self._stamp():
            return
        fallback_observations = output.get("observations") if managed_browser else None
        for key, record in verdicts.items():
            requirement = self.requirements.get(key)
            if not requirement or _approved_exit_commands(requirement) or not isinstance(record, dict):
                continue
            status = record.get("status")
            evidence = _bounded(record.get("evidence"))
            raw_observations = record.get("browser_observations") if record.get("browser_observations") is not None else fallback_observations
            observations = [
                {field: _bounded(item.get(field)) for field in ("type", "label", "selector") if item.get(field) is not None or field != "selector"}
                for item in raw_observations[:24] if isinstance(item, dict) and _bounded(item.get("type")) and _bounded(item.get("label"))
            ] if isinstance(raw_observations, list) else []
            if status not in ("pass", "fail") or not evidence:
                continue
            if status == "pass" and (not ok or errors or ((browser or managed_browser) and not observations)
                                     or not _browser_observations_match(requirement, observations)):
                continue
            self._put(source, {"requirement": requirement}, status, evidence, observations)

    def _put(self, source: str, identity: dict, status: str, evidence: str, observations: list[dict] | None = None) -> None:
        key = "tool/" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]
        subject = identity["requirement"] if "requirement" in identity else " ".join(identity["command"])
        self.facts[key] = {
            "key": key,
            "value": f"{self.goal} · {status.upper()} · {subject} — {evidence}",
            "verification_proof": {**identity, "status": status, "source": source, "goal": self.goal, "evidence": evidence,
                                   **({"browser_observations": observations} if observations else {})},
        }


def inherited_verification_checks(root: Path, requirements: Sequence[str], facts: Sequence[dict]) -> dict[str, dict]:
    proofs = [fact["verification_proof"] for fact in facts
              if fact.get("source_action") == "verification_tool" and isinstance(fact.get("verification_proof"), dict)]
    if not proofs or not (source := _source_digest(root, RepositorySnapshot.capture(root))):
        return {}
    exact, commands = {}, {}
    for proof in proofs:
        if proof.get("source") != source or not _bounded(proof.get("evidence")) or not _bounded(proof.get("goal")):
            continue
        if isinstance(proof.get("requirement"), str):
            exact[proof["requirement"]] = proof
        elif isinstance(proof.get("command"), list) and all(isinstance(word, str) for word in proof["command"]):
            commands[tuple(proof["command"])] = proof
    inherited = {}
    for requirement in requirements:
        if match := exact.get(requirement):
            if match.get("status") == "pass":
                inherited[requirement] = {**match, "goals": [match["goal"]]}
        else:
            expected = [commands.get(tuple(command)) for command in _approved_exit_commands(requirement)]
            if expected and all(item and item.get("status") == "pass" for item in expected):
                inherited[requirement] = {
                    "evidence": " ".join(item["evidence"] for item in expected),
                    "goals": list(dict.fromkeys(item["goal"] for item in expected)),
                }
    return inherited


def _output_object(output: object) -> dict:
    if isinstance(output, str):
        try:
            output = json.loads(output)
        except ValueError:
            return {}
    return output if isinstance(output, dict) else {}
