"""Bound execution-phase reconnaissance independently of context freshness.

Restoring evicted source is legitimate, but it is not progress toward delivering
the task. Neither new ranges nor compaction can renew this turn allowance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Sequence

from execution_evidence import RepositoryObservation, external_execution_identity, repository_observation
from tree_commands import CommandResult
from issue_proposals import diagnostic_fingerprint

READ_ONLY_WARNING_TURNS = 4
READ_ONLY_RECOVERY_TURNS = 8
RECOVERY_TURN_ALLOWANCE = 3
RECOVERY_READ_ALLOWANCE = 2
RECOVERY_READ_LINES = 120
RECOVERY_READ_CHARS = 6000


@dataclass
class DiagnosticRepair:
    fingerprint: str
    path: str
    line: int
    issue_id: str
    deadline: int
    reads_used: int = 0


@dataclass
class ExecutionProgress:
    read_only_turns: int = 0
    repository_reads: int = 0
    read_chars: int = 0
    context_replacements: int = 0
    focused_reads_used: int = 0
    source_lines: dict[str, set[int]] = field(default_factory=dict)
    targets: list[str] = field(default_factory=list)
    diagnostic_repair: DiagnosticRepair | None = None
    diagnostic_fingerprints: set[str] = field(default_factory=set)
    external_executions: set[str] = field(default_factory=set)

    @property
    def recovering(self) -> bool:
        return self.read_only_turns >= READ_ONLY_RECOVERY_TURNS

    @property
    def exhausted(self) -> bool:
        deadline = READ_ONLY_RECOVERY_TURNS + RECOVERY_TURN_ALLOWANCE
        if self.diagnostic_repair is not None:
            deadline = max(deadline, self.diagnostic_repair.deadline)
        return self.read_only_turns >= deadline

    def focus_diagnostic(self, issue: dict) -> bool:
        """A new concrete check failure earns one bounded repair window, not more reconnaissance."""
        fingerprint = diagnostic_fingerprint(issue)
        path = str(issue.get("file") or issue.get("path") or "").removeprefix("/repo/").removeprefix("repo/")
        line_match = re.search(r"\d+", str(issue.get("line") or ""))
        if not path or path.startswith(("/", "..")) or "://" in path or not line_match or fingerprint in self.diagnostic_fingerprints:
            return False
        self.diagnostic_fingerprints.add(fingerprint)
        self.diagnostic_repair = DiagnosticRepair(
            fingerprint, path, int(line_match[0]), str(issue.get("id", "")),
            self.read_only_turns + 1 + RECOVERY_TURN_ALLOWANCE,
        )
        return True

    def observe_turn(self, commands: Sequence[str], results: Sequence[CommandResult], *,
                     edit_attempted: bool = False, allow_external_progress: bool = True) -> None:
        executions = {identity for result in results if result.ok
                      and (identity := external_execution_identity(result.tool_action or {})) is not None}
        novel_execution = allow_external_progress and bool(executions - self.external_executions)
        self.external_executions.update(executions)
        if edit_attempted or novel_execution:
            self.read_only_turns = self.repository_reads = self.read_chars = self.context_replacements = self.focused_reads_used = 0
            self.source_lines.clear()
            self.targets.clear()
            self.diagnostic_repair = None
            self.diagnostic_fingerprints.clear()
            return
        self.read_only_turns += 1
        for command, result in zip(commands, results):
            observation = repository_observation(command, result)
            if observation is None:
                continue
            self.repository_reads += 1
            self.read_chars += len(result.output if result.model_output is None else result.model_output)
            if observation.source_path:
                path = observation.source_path
                self.source_lines.setdefault(path, set()).update(number for number, _ in observation.source_lines)
                self.targets = [item for item in self.targets if item != path][-3:] + [path]

    def allow_focused_read(self, command: str, observation: RepositoryObservation, result: CommandResult) -> bool:
        repair = self.diagnostic_repair
        repairing = repair is not None and self.read_only_turns < repair.deadline
        used = repair.reads_used if repairing else self.focused_reads_used
        if used >= RECOVERY_READ_ALLOWANCE or observation.empty_search:
            return False
        action_type = str((result.tool_action or {}).get("type", ""))
        command = command.split("] ", 1)[-1] if command.startswith("[") else command
        verb = command.split(None, 1)[0] if command else ""
        structured = action_type in {"outline_file", "inspect_file"} or verb in {"symbols", "inspect-file"}
        output = result.output if result.model_output is None else result.model_output
        # Source or a bounded outline is useful; broad searches cannot renew the lease.
        if not observation.source_path and not structured:
            return False
        line_count = len(observation.source_lines) if observation.source_lines else len(output.splitlines())
        if len(output) > RECOVERY_READ_CHARS or line_count > RECOVERY_READ_LINES:
            return False
        if repairing:
            repair.reads_used += 1
        else:
            self.focused_reads_used += 1
        return True

    def state(self) -> dict:
        return {
            "read_only_turns": self.read_only_turns,
            "repository_reads": self.repository_reads,
            "read_chars": self.read_chars,
            "source_lines_returned": {path: len(lines) for path, lines in self.source_lines.items()},
            "context_replacements": self.context_replacements,
            "external_executions": len(self.external_executions),
            "focused_reads_remaining": max(0, RECOVERY_READ_ALLOWANCE - self.focused_reads_used),
            "recovery": self.recovering,
            "recent_targets": list(self.targets),
            "diagnostic_repair": ({"path": self.diagnostic_repair.path, "line": self.diagnostic_repair.line,
                                   "issue_id": self.diagnostic_repair.issue_id,
                                   "turns_remaining": max(0, self.diagnostic_repair.deadline - self.read_only_turns),
                                   "focused_reads_remaining": max(0, RECOVERY_READ_ALLOWANCE - self.diagnostic_repair.reads_used)}
                                  if self.diagnostic_repair is not None else None),
        }

    def guidance(self) -> str:
        if self.read_only_turns < READ_ONLY_WARNING_TURNS:
            return ""
        status = "EXECUTION DELIVERY RECOVERY" if self.recovering else "EXECUTION DELIVERY CHECKPOINT"
        repair = self.diagnostic_repair
        if repair is not None and self.read_only_turns < repair.deadline:
            return (
                f"DIAGNOSTIC REPAIR: the failed check identified {repair.path}:{repair.line}. "
                f"{repair.deadline - self.read_only_turns} repair turns and {max(0, RECOVERY_READ_ALLOWANCE - repair.reads_used)} "
                f"focused reads remain (at most {RECOVERY_READ_LINES} lines / {RECOVERY_READ_CHARS} characters each). "
                "Use the retained diagnostic source to make the targeted fix; if exact context is missing, read the small suggested window or declaration. "
                "A whole-file reread is unnecessary. Rerunning the same failed check does not renew this repair window. "
                "After the fix, rerun the failed check. If repair is unavailable, report the specific blocker."
            )
        return (
            f"{status}: {self.read_only_turns} completed execution turns, {self.repository_reads} repository reads "
            f"and {self.context_replacements} context replacements since the last edit attempt or new external execution. "
            "Reading new or evicted source does not reset this allowance. Use the current bounded edit surface to make the "
            "requested change; you do not need the entire file in context. If context is missing, name the exact target "
            "and missing identifier/anchor, then request a small read-symbol or read-context window. "
            "Do not promise an edit after one more broad scan or reload every declaration before editing. "
            + (f"Only {max(0, RECOVERY_READ_ALLOWANCE - self.focused_reads_used)} focused reads remain, each at most "
               f"{RECOVERY_READ_LINES} source lines and {RECOVERY_READ_CHARS} characters. " if self.recovering else "")
            + "For an execution goal, run the next task-specific MCP operation or package script and report its result. "
              "For a read-only task, deliver the findings. If the required action is unavailable, report the concrete blocker."
        )

    def stop_reason(self) -> str:
        if self.diagnostic_repair is not None:
            repair = self.diagnostic_repair
            return (f"diagnostic repair stalled at {repair.path}:{repair.line}: the bounded inspection/repair window expired "
                    f"without an edit attempt or completed answer ({self.read_only_turns} execution turns, {self.repository_reads} repository reads).")
        return (
            f"execution delivery stalled: {self.read_only_turns} execution turns without an edit attempt, new external execution, or completed answer; "
            f"{self.repository_reads} repository reads and {self.context_replacements} context replacements. "
            "The bounded recovery allowance was exhausted."
        )
