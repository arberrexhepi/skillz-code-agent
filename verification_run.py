"""Required verification checks and a bounded, host-owned record of their evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Sequence

from verification_repair import compiler_errors


MAX_VERIFICATION_TURNS = 12
MAX_VERIFICATION_TOOL_CALLS = 16
MAX_VERIFICATION_ATTEMPTS = 3
MAX_TURNS_WITHOUT_EVIDENCE = 5
NATIVE_CHECKS = frozenset({"run_check", "run_route_check", "diagnose"})
VERIFICATION_ACTIONS = NATIVE_CHECKS | {"mcp", "run_shell", "artifact_preview"}
_RESULT_PREFIX = "VERIFICATION_RESULTS:"
_PROJECT_COMMAND = r"(?:(?:npx\s+)?tsc\s+--noEmit|npm\s+run\s+(?:build|typecheck))"
_APPROVED_EXIT_CHECK = re.compile(
    rf"^(?:run\s+)?(?P<commands>{_PROJECT_COMMAND}(?:\s+(?:and|&&)\s+{_PROJECT_COMMAND})*)"
    r"\s+(?:exit(?:s)? (?:with )?(?:code |status )?0|pass(?:es)?(?: with (?:0|zero) errors)?"
    r"|(?:complete|succeed)s? (?:with (?:0|zero) errors|successfully))\.?$",
    re.IGNORECASE,
)
_BROWSER_INTERACTION = re.compile(r"\b(?:click\w*|interact\w*|navigat\w*|workflow|open(?:s|ing)? a project)\b", re.I)


def _approved_exit_commands(requirement: str) -> list[list[str]]:
    match = _APPROVED_EXIT_CHECK.fullmatch(requirement.replace("`", "").strip())
    return [part.split() for part in re.split(r"\s+(?:and|&&)\s+", match["commands"], flags=re.I)] if match else []


def _shell_commands(command: Any) -> list[list[str]]:
    if isinstance(command, str):
        return [part.split() for part in command.split("&&")]
    return [command] if isinstance(command, list) else []


def _browser_assertion_count(requirement: str) -> int:
    if not re.search(r"\b(?:browser|playwright)\b", requirement, re.I):
        return 0
    if re.search(r"\b(?:home|landing)\b", requirement, re.I) and re.search(r"\b(?:viewport|workspace)\b", requirement, re.I):
        return 2
    return int(bool(_BROWSER_INTERACTION.search(requirement) or re.search(r"\b(?:viewport|workspace)\b", requirement, re.I)))


def _browser_observations_match(requirement: str, observations: Any) -> bool:
    count = _browser_assertion_count(requirement)
    if not count:
        return True
    if not isinstance(observations, list) or any(not isinstance(item, dict) for item in observations):
        return False
    assertions = [
        index for index, item in enumerate(observations)
        if item.get("type") == "assert-visible" and _bounded(item.get("label")) and _bounded(item.get("selector"))
    ]
    transition_types = ("click",) if count == 2 or re.search(r"\bclick\w*\b", requirement, re.I) else ("click", "goto")
    transitions = [index for index, item in enumerate(observations) if item.get("type") in transition_types and _bounded(item.get("label"))]
    if count == 2:
        return any(
            before < position < after and observations[before]["selector"] != observations[after]["selector"]
            for position in transitions for before in assertions for after in assertions
        )
    return bool(assertions and (not _BROWSER_INTERACTION.search(requirement) or any(position < assertions[-1] for position in transitions)))


def is_verification_setup(action: dict) -> bool:
    return action.get("type") == "artifact_preview" and action.get("operation", "start") == "start"


def needs_verification_check(action: dict) -> bool:
    kind = action.get("type")
    return (
        kind in VERIFICATION_ACTIONS and not is_verification_setup(action)
        and (kind != "mcp" or action.get("operation") in {"call", "read-resource", "get-prompt"})
    )


def all_required_checks_passed(requirements: Sequence[str], report: Any) -> bool:
    if not isinstance(report, dict) or report.get("passed") is not True:
        return False
    checks = report.get("checks")
    required = list(dict.fromkeys(requirements))
    if not required or not isinstance(checks, list) or len(checks) != len(required):
        return False
    return all(
        isinstance(check, dict) and check.get("id") == f"check-{index}"
        and check.get("requirement") == requirement and check.get("status") == "pass"
        and bool(_bounded(check.get("evidence"))) and type(check.get("attempts")) is int
        and 1 <= check["attempts"] <= MAX_VERIFICATION_ATTEMPTS
        and _browser_observations_match(requirement, check.get("browser_observations"))
        for index, (requirement, check) in enumerate(zip(required, checks), 1)
    )


@dataclass(frozen=True)
class Verdict:
    status: str
    evidence: str
    next_step: str = ""


def _bounded(value: object, limit: int = 500) -> str:
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _browser_failure(output: dict) -> str:
    for field, label, fields in (
        ("requestFailures", "Request failed", ("url", "errorText")),
        ("pageErrors", "Page error", ("message",)),
        ("consoleMessages", "Console", ("type", "text")),
        ("responseErrors", "HTTP", ("status", "url", "statusText")),
    ):
        incidents = output.get(field)
        if isinstance(incidents, list) and incidents and isinstance(incidents[0], dict):
            detail = " ".join(str(incidents[0].get(key) or "") for key in fields)
            if detail.strip():
                return _bounded(f"{label} {detail}", 300)
    return ""


def _tool_findings(value: Any, verdicts: dict, errors: list[str], *, depth: int = 0, accept_verdicts: bool = True) -> None:
    """Look through known tool envelopes and marked stdout, without treating prose as an assertion."""
    if depth > 8:
        return
    if isinstance(value, dict):
        if accept_verdicts and isinstance(value.get("verification_results"), dict):
            verdicts.update(value["verification_results"])
        if value.get("isError") or value.get("ok") is False or value.get("status") in ("error", "failed"):
            error = value.get("error")
            detail = error.get("message") if isinstance(error, dict) else error
            errors.append(_bounded(detail or value.get("message") or value.get("summary")))
        if type(value.get("returncode")) is int and value["returncode"] != 0:
            errors.append(f"Process exited with code {value['returncode']}.")
        for key in ("data", "result", "structuredContent", "content", "stdout", "text"):
            _tool_findings(value.get(key), verdicts, errors, depth=depth + 1, accept_verdicts=accept_verdicts)
        for key in ("stderr", "stderr_tail", "error", "error_lines", "error_or_warning_lines", "warn_or_error_lines"):
            _tool_findings(value.get(key), verdicts, errors, depth=depth + 1, accept_verdicts=False)
    elif isinstance(value, list):
        for item in value:
            _tool_findings(item, verdicts, errors, depth=depth + 1, accept_verdicts=accept_verdicts)
    elif isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith(("{", "[")):
            try:
                decoded = json.loads(stripped)
            except ValueError:
                pass
            else:
                _tool_findings(decoded, verdicts, errors, depth=depth + 1, accept_verdicts=accept_verdicts)
                return
        if "Traceback (most recent call last)" in value:
            exceptions = re.findall(r"(?m)^\s*\w*(?:Error|Exception): .+$", value)
            errors.append(_bounded(exceptions[-1]) if exceptions else "The verifier produced a Python traceback.")
        for line in stripped.splitlines() if accept_verdicts else ():
            if line.lstrip().startswith(_RESULT_PREFIX):
                try:
                    decoded = json.loads(line.lstrip().removeprefix(_RESULT_PREFIX))
                except ValueError:
                    continue
                if isinstance(decoded, dict):
                    verdicts.update(decoded)


@dataclass
class VerificationCheck:
    requirement: str
    attempts: int = 0
    verdict: Verdict | None = None
    seen: set[tuple[str, str]] = field(default_factory=set)
    repeated_failure: bool = False
    source_errors: list[dict[str, str]] = field(default_factory=list)
    browser_observations: list[dict[str, str]] = field(default_factory=list)
    failure_observed: bool = False
    shell_exit_observed: bool = False

    @property
    def retryable(self) -> bool:
        return (
            self.attempts < MAX_VERIFICATION_ATTEMPTS
            and not self.repeated_failure
            and (self.verdict is None or self.verdict.status == "fail")
        )


class VerificationRun:
    def __init__(self, requirements: Sequence[str], *, review_deferred: bool = False, managed_browser: bool = True) -> None:
        self.checks = {f"check-{index}": VerificationCheck(requirement)
                       for index, requirement in enumerate(dict.fromkeys(requirements), 1)}
        self.turns = 0
        self.turn_limit = MAX_VERIFICATION_TURNS
        self.tool_calls = 0
        self.idle_turns = 0
        self._new_evidence = False
        self.review_deferred = review_deferred
        self.managed_browser = managed_browser
        self._reporting = False
        self.observed_source_paths: set[str] = set()
        self.repair_handoff: dict | None = None

    def observe_source(self, path: str) -> None:
        if path and path != ".":
            self.observed_source_paths.add(path.removeprefix("/repo/").removeprefix("./"))

    def record_handoff(self, notes: str) -> str:
        """Accept a diagnosis only when it refers to failed checks and evidenced targets."""
        if self.passed:
            return notes
        try:
            handoff = json.loads(notes)
        except ValueError:
            return notes
        if not isinstance(handoff, dict):
            return notes
        targets = handoff.get("targets")
        ids = handoff.get("check_ids")
        diagnosis = _bounded(handoff.get("diagnosis"), 2000)
        if (not diagnosis or not isinstance(targets, list) or not targets or len(targets) > 8
                or any(not isinstance(path, str) for path in targets)
                or not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids)):
            return notes
        if any(
            key not in self.checks or not self.checks[key].failure_observed for key in ids
        ):
            return notes
        known_paths = self.observed_source_paths | {
            error["path"].removeprefix("/repo/").removeprefix("./")
            for key in ids for error in self.checks[key].source_errors
        }
        normalized = list(dict.fromkeys(path.removeprefix("/repo/").removeprefix("./") for path in targets))
        if set(normalized) <= known_paths:
            self.repair_handoff = {"targets": normalized, "check_ids": list(dict.fromkeys(ids)), "diagnosis": diagnosis}
        return diagnosis

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.verdict and check.verdict.status == "pass" for check in self.checks.values())

    @property
    def stop_reason(self) -> str:
        if self.passed:
            if self.review_deferred and not self._reporting and self.turns < self.turn_limit:
                return ""
            return "All required verification checks passed."
        if not any(check.retryable for check in self.checks.values()):
            return "Required checks reached their final verdicts or retry limits."
        if self.tool_calls >= MAX_VERIFICATION_TOOL_CALLS:
            return "The verification tool-call budget was exhausted."
        if self.turns >= self.turn_limit:
            return "The verification turn budget was exhausted."
        if self.idle_turns >= MAX_TURNS_WITHOUT_EVIDENCE:
            return "The verification run stopped after five turns without new check evidence."
        return ""

    def begin_turn(self) -> None:
        self.turns += 1
        self._reporting = self.review_deferred and self.passed

    def begin_checks(self, ids: Any, *, action: dict | None = None) -> list[str]:
        if self.tool_calls >= MAX_VERIFICATION_TOOL_CALLS:
            raise ValueError("The verification tool-call budget was exhausted.")
        if not isinstance(ids, (list, tuple)) or not ids or any(not isinstance(item, str) for item in ids):
            raise ValueError("Provide one or more required check IDs.")
        selected = list(dict.fromkeys(ids))
        for key in selected:
            if key not in self.checks:
                raise ValueError(f"Unknown verification check {key}.")
            check = self.checks[key]
            if not check.retryable:
                raise ValueError(f"{key} has a final verdict or reached its retry limit. Use the recorded evidence.")
            if (action is not None and action.get("type") == "run_shell" and check.shell_exit_observed
                    and _shell_commands(action.get("command")) == _approved_exit_commands(check.requirement)):
                check.repeated_failure = True
                raise ValueError(f"{key} already has an exit status for this command. Use the recorded evidence to diagnose the failure.")
            if action is not None and _browser_assertion_count(check.requirement):
                managed_check = action.get("type") == "artifact_preview" and action.get("operation") == "browser-check"
                custom_check = not self.managed_browser and action.get("type") in {"mcp", "run_shell"}
                if not (managed_check or custom_check):
                    instructions = ("Use artifact-preview browser-check with visible-state assertions." if self.managed_browser
                                    else "Use a Playwright tool or script that reports the observed browser steps.")
                    raise ValueError(f"{key} requires browser interaction evidence. {instructions}")
        for key in selected:
            self.checks[key].attempts += 1
        self.tool_calls += 1
        return selected

    def record(self, ids: list[str], *, kind: str, ok: bool, output: Any, unavailable: bool = False, action: dict | None = None) -> dict:
        verdicts: dict = {}
        errors: list[str] = []
        _tool_findings(output, verdicts, errors)
        native_summary = _bounded(output.get("summary") if isinstance(output, dict) else output)
        source_errors = compiler_errors(output) if kind in {"run_shell", "run_check", "diagnose"} else []
        action = action or {}
        browser = kind == "artifact_preview" and action.get("operation") == "browser-check"
        for key in ids:
            check = self.checks[key]
            supplied = verdicts.get(key)
            record = supplied if isinstance(supplied, dict) else {}
            status = record.get("status")
            evidence = _bounded(record.get("evidence"))
            next_step = _bounded(record.get("next_step"))
            commands = _approved_exit_commands(check.requirement) if kind == "run_shell" else []
            approved_shell = bool(commands and _shell_commands(action.get("command")) == commands)
            observed_exit = isinstance(output, dict) and type(output.get("returncode")) is int
            if approved_shell and observed_exit and not unavailable:
                check.shell_exit_observed = True
            observations = output.get("observations") if browser and isinstance(output, dict) else None
            custom_browser = not self.managed_browser and kind in {"mcp", "run_shell"} and status == "pass" and bool(evidence)
            if custom_browser:
                observations = record.get("browser_observations")
            if not isinstance(observations, list) or len(observations) > 8 or any(not isinstance(item, dict) for item in observations):
                observations = []
            else:
                observations = [{field: _bounded(item.get(field)) for field in ("type", "label", "selector")} for item in observations]
            if unavailable:
                verdict = Verdict("blocked", next((error for error in errors if error), "") or _bounded(output)
                                  or "The verification tool is unavailable.", next_step)
            elif errors or not ok:
                details = (
                    (source_errors[0]["evidence"] if source_errors else "")
                    or next((error for error in reversed(errors) if error), "")
                    or _bounded(output) or "The verification tool failed."
                )
                if browser and isinstance(output, dict) and (incident := _browser_failure(output)):
                    details = _bounded(f"{incident} — {details}")
                if status == "fail" and evidence and evidence != details:
                    details = _bounded(f"{evidence} ({details})")
                verdict = Verdict("fail", details, next_step)
            elif browser and isinstance(output, dict) and output.get("ok") is True and observations and _browser_observations_match(check.requirement, observations):
                observed = [item["label"] for item in observations if isinstance(item, dict) and _bounded(item.get("label"))]
                verdict = Verdict("pass", _bounded("Managed browser observed " + " → ".join(observed)))
            elif browser or _browser_assertion_count(check.requirement):
                if custom_browser and observations and _browser_observations_match(check.requirement, observations):
                    verdict = Verdict("pass", evidence)
                else:
                    verdict = Verdict("fail", "The browser result did not establish the required UI states and interactions.")
            elif (approved_shell and observed_exit and output["returncode"] == 0
                  and status not in ("fail", "blocked")):
                verdict = Verdict("pass", _bounded(" && ".join(" ".join(part) for part in commands) + " exited 0."))
            elif status in ("pass", "fail", "blocked") and evidence:
                verdict = Verdict(status, evidence, next_step)
            elif not verdicts and len(ids) == 1 and kind in NATIVE_CHECKS and native_summary:
                verdict = Verdict("pass", native_summary)
            else:
                verdict = Verdict("fail", f"The tool returned no usable verification verdict for {key}.",
                                  "Make the verifier emit a status and concrete evidence for this check.")

            check.browser_observations = observations if verdict.status == "pass" else []
            check.failure_observed = verdict.status == "fail" and bool(errors or not ok or (status == "fail" and evidence))
            check.source_errors = []
            if verdict.status == "fail" and kind in {"run_shell", "run_check", "diagnose"}:
                check.source_errors = compiler_errors(record.get("evidence")) or source_errors
            identity = (verdict.status, verdict.evidence.casefold())
            if identity not in check.seen:
                check.seen.add(identity)
                self._new_evidence = True
            elif verdict.status == "fail":
                check.repeated_failure = True
            check.verdict = verdict
        return self.snapshot()

    def finish_turn(self) -> None:
        self.idle_turns = 0 if self._new_evidence else self.idle_turns + 1
        self._new_evidence = False

    def snapshot(self, *, final: bool = False) -> dict:
        checks = []
        for key, check in self.checks.items():
            verdict = check.verdict
            next_step = verdict.next_step if verdict else ""
            if final and not next_step and (not verdict or verdict.status != "pass"):
                next_step = f"Resolve the recorded blocker and verify: {check.requirement}" if verdict else f"Run the required check: {check.requirement}"
            checks.append({
                "id": key, "requirement": check.requirement, "attempts": check.attempts,
                "status": verdict.status if verdict else ("blocked" if final else "pending"),
                "evidence": verdict.evidence if verdict else ("No verdict was recorded." if final else ""),
                "next_step": next_step,
                "retryable": check.retryable and not final,
                **({"source_errors": check.source_errors} if check.source_errors else {}),
                **({"browser_observations": check.browser_observations} if check.browser_observations else {}),
            })
        return {
            "passed": self.passed, "turns_used": self.turns, "turn_limit": self.turn_limit,
            "tool_calls_used": self.tool_calls, "tool_call_limit": MAX_VERIFICATION_TOOL_CALLS,
            "turns_without_new_evidence": self.idle_turns, "checks": checks,
            **({"repair_handoff": self.repair_handoff} if final and self.repair_handoff else {}),
        }

    def guidance(self, *, tree_commands: bool) -> str:
        if self._reporting:
            return (
                "VERIFICATION REPORT\nAll required checks passed. Use finish on this turn. "
                "Review the deferred findings in the goal notes against the original issue request. "
                "Identify findings that are irrelevant to that intent and state any useful possible next steps as optional. "
                "Identify any findings still needing follow-up without claiming they were repaired.\n"
                + json.dumps(self.snapshot(), ensure_ascii=False)
            )
        command = (
            "Use verify check-1,check-2 <tool command>, for example verify check-1 run-check test."
            if tree_commands else
            'Use an action with type "verify", a "checks" array of check IDs, and the existing tool action under "action".'
        )
        browser_guidance = (
            "Start a managed artifact preview as a separate setup action. Starting it publishes a URL and consumes no check attempt. "
            + ("For browser interaction requirements, use artifact-preview browser-check with JSON containing route and steps. "
               if tree_commands else
               'For browser interaction requirements, use artifact_preview with operation "browser-check" and an input containing route and steps. ')
            if self.managed_browser else
            'For browser interaction requirements, use a Playwright tool or script and include observed steps in the check verdict as "browser_observations". '
        )
        shell_guidance = "".join(
            f"{key}: Use shell command `{' && '.join(' '.join(part) for part in commands)}` and rely on the host exit status.\n"
            for key, check in self.checks.items() if check.retryable and not check.shell_exit_observed
            and (commands := _approved_exit_commands(check.requirement))
        )
        return (
            "VERIFICATION GOAL\n" + command + "\n"
            'For custom tools print one stdout line VERIFICATION_RESULTS: {"check-1":{"status":"pass","evidence":"observed result"}}. '
            'Report fail or blocked with evidence and a concrete next_step where appropriate. A successful tool call alone '
            "does not establish a pass. Bind each check to its actual assertion, and propagate script errors. "
            + browser_guidance +
            'Use steps such as {"type":"assert-visible","selector":"...","label":"Project Home"}, '
            '{"type":"click","selector":"...","label":"Open project"}, then assert the workspace in the same browser session. '
            "A plain route load only covers route health. "
            "For API smoke tests, derive payloads from the actual request schema, assert status and response fields, "
            "include the response body on failure, and use temporary test data. "
            "A native run-check, diagnose, or run-route-check can report its own success for one matching check. "
            "Use normal source reads to find exact inputs and diagnose failures. Reuse recorded exit statuses for approved commands and skip checks with a final verdict. "
            "Use finish to summarize findings and recommend next steps for failures or blocked checks. "
            'When you have grounded an in-scope repair, use finish with JSON {"check_ids":["check-2"],'
            '"targets":["path/to/file"],"diagnosis":"cause, evidence, and a concrete repair"}. '
            "On the last available turn, favor that report over starting another investigation.\n"
            + shell_guidance
            + json.dumps(self.snapshot(), ensure_ascii=False)
        )

    def report(self, reason: str = "", notes: str = "") -> str:
        checks = self.snapshot(final=True)["checks"]
        passed = sum(check["status"] == "pass" for check in checks)
        lines = [f"Verification: {passed}/{len(checks)} required checks passed."]
        if reason:
            lines.append(reason)
        for check in checks:
            lines.append(f"- {check['id']} [{check['status'].upper()}] {check['requirement']} — {check['evidence']}")
        unresolved = [check for check in checks if check["status"] != "pass"]
        if unresolved:
            lines.append("\nNext steps")
            for check in unresolved:
                lines.append(f"- {check['id']}: {check['next_step']}")
        if notes:
            lines.append(f"\nWorker notes\n{notes.strip()[:2000]}")
        return "\n".join(lines)
