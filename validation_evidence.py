"""Keep failed checks independent of mutation and source-inspection state."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
import shlex

from shell_processes import npm_script
from tree_commands import CommandResult


def verification_pass(result: CommandResult) -> tuple[str, str] | None:
    """Identify a concrete pass independently of output noise or model claims."""
    action = result.tool_action or {}
    if not result.ok or any(action.get(key) for key in ("_changed_paths", "_managed_process", "_validation_unavailable")):
        return None
    kind = action.get("type")
    if kind in {"run_check", "diagnose", "run_route_check"}:
        scope = action.get({"run_check": "kind", "diagnose": "path", "run_route_check": "target"}[kind], "")
        if scope and re.search(r"(?m)^issues=0\s*$", result.output):
            label = str(scope).removeprefix("/repo/").removeprefix("repo/")
            identity = json.dumps([kind, label, action.get("base_url", "") if kind == "run_route_check" else ""])
            return identity, f"{kind.replace('_', ' ')} {label} passed"
    if kind != "run_shell":
        return None
    # Only the host's exit line and actual stdout can earn a verification turn.
    exit_line, _, output = result.output.partition("\n")
    if exit_line != "exit_code=0":
        return None
    if name := action.get("check_name"):
        return json.dumps(["shell_check", name]), f"shell check {name} passed"
    command = action.get("command", "")
    script = npm_script(command)
    check = script[1] if script and script[1] in {"build", "lint", "typecheck", "test"} else ""
    if not check and isinstance(command, str) and "\n" not in command:
        try:
            argv = shlex.split(command)
        except ValueError:
            argv = []
        if argv in (["npx", "tsc", "--noEmit"], ["tsc", "--noEmit"], ["./node_modules/.bin/tsc", "--noEmit"]):
            check = "typecheck"
    if check:
        return json.dumps(["run_check", check, ""]), f"{check} passed"
    stdout = output.removeprefix("stdout:\n").split("\nstderr:\n", 1)[0] if output.startswith("stdout:\n") else ""
    if re.search(r"(?m)^SMOKE_OK(?:[ \t].*)?$", stdout):
        return "shell-smoke", "shell smoke test passed (SMOKE_OK)"
    return None


@dataclass
class FailedCheck:
    action: dict
    reason: str
    diagnostics: tuple[dict, ...] = ()


@dataclass
class ValidationEvidence:
    failures: dict[str, FailedCheck] = field(default_factory=dict)
    passed: bool = False

    def copy(self) -> ValidationEvidence:
        return ValidationEvidence(dict(self.failures), self.passed)

    def observe(self, result: CommandResult, *, has_mutation: bool = False, diagnostics: tuple[dict, ...] = ()) -> None:
        action = result.tool_action or {}
        if result.command_type in {"skipped", "reused"} or action.get("_managed_process"):
            return
        kind = action.get("type", "")
        scope_field = {"diagnose": "path", "run_check": "kind", "run_route_check": "target", "run_shell": "command"}.get(kind)
        check_name = action.get("check_name") if kind == "run_shell" else None
        if scope_field is None or (kind == "run_shell" and not has_mutation and not check_name):
            return
        scope = str(action.get(scope_field, "")).removeprefix("/repo/").removeprefix("repo/")
        key = json.dumps(["shell_check", check_name]) if check_name else json.dumps([kind, scope, action.get("base_url", "") if kind == "run_route_check" else ""])
        if result.ok:
            self.failures.pop(key, None)
            if not action.get("_changed_paths"):
                self.passed = True
            else:
                self.passed = False
        else:
            self.failures[key] = FailedCheck(dict(action), str(result.output or "check failed")[:1000], diagnostics)

    def clear_deferred(self, apply_deferrals) -> None:
        for key, check in list(self.failures.items()):
            if check.diagnostics and all(item.get("status") == "deferred" for item in apply_deferrals(list(check.diagnostics))):
                del self.failures[key]

    def retry_actions(self) -> list[dict]:
        return [dict(check.action) for check in self.failures.values()]

    def failure_summary(self) -> str:
        return "Validation failed: " + "; ".join(
            f"{check.action.get('type', 'check')} "
            f"{check.action.get('path') or check.action.get('kind') or check.action.get('target') or check.action.get('check_name') or check.action.get('command', '')}: "
            f"{check.reason}" for check in self.failures.values())
