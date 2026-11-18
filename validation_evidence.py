"""Keep failed checks independent of mutation and source-inspection state."""

from __future__ import annotations

from dataclasses import dataclass, field
import json

from tree_commands import CommandResult


@dataclass
class FailedCheck:
    action: dict
    reason: str


@dataclass
class ValidationEvidence:
    failures: dict[str, FailedCheck] = field(default_factory=dict)
    passed: bool = False

    def copy(self) -> ValidationEvidence:
        return ValidationEvidence(dict(self.failures), self.passed)

    def observe(self, result: CommandResult, *, has_mutation: bool = False) -> None:
        action = result.tool_action or {}
        if action.get("_managed_process"):
            return
        kind = action.get("type", "")
        scope_field = {"diagnose": "path", "run_check": "kind", "run_route_check": "target", "run_shell": "command"}.get(kind)
        if scope_field is None or (kind == "run_shell" and not has_mutation):
            return
        scope = str(action.get(scope_field, "")).removeprefix("/repo/").removeprefix("repo/")
        key = json.dumps([kind, scope, action.get("base_url", "") if kind == "run_route_check" else ""])
        if result.ok:
            self.failures.pop(key, None)
            if not action.get("_changed_paths"):
                self.passed = True
            else:
                self.passed = False
        else:
            self.failures[key] = FailedCheck(dict(action), str(result.output or "check failed")[:1000])

    def retry_actions(self) -> list[dict]:
        return [dict(check.action) for check in self.failures.values()]

    def failure_summary(self) -> str:
        return "Validation failed: " + "; ".join(
            f"{check.action.get('type', 'check')} "
            f"{check.action.get('path') or check.action.get('kind') or check.action.get('target') or check.action.get('command', '')}: "
            f"{check.reason}" for check in self.failures.values())
