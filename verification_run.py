"""Required verification checks and a bounded, host-owned record of their evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Callable, Sequence
from urllib.parse import urlsplit
from issue_facts import ISSUE_LOCAL_FACT_TYPES

from verification_repair import compiler_errors, verification_diagnosis
from verification_session import MAX_MCP_VERIFICATION_CALLS, McpCheckBudget, McpVerificationSession, mcp_session_limits


MAX_VERIFICATION_TURNS = 12
MAX_VERIFICATION_TOOL_CALLS = 16
MAX_VERIFICATION_ATTEMPTS = 3
MAX_TURNS_WITHOUT_EVIDENCE = 5
MAX_VERIFICATION_SETUP_CALLS = 6
NATIVE_CHECKS = frozenset({"run_check", "run_route_check", "diagnose"})
VERIFICATION_ACTIONS = NATIVE_CHECKS | {"mcp", "run_shell", "artifact_preview"}
_RESULT_PREFIX = "VERIFICATION_RESULTS:"
_PLAYWRIGHT_SETUP_TOOLS = frozenset({"browser_navigate", "browser_snapshot"})
_PLAYWRIGHT_INPUT_TOOLS = frozenset({"browser_click", "browser_drag", "browser_press_key", "browser_select_option"})
_PLAYWRIGHT_ASSERTION_TOOLS = frozenset({"browser_run_code", "browser_evaluate"})
_MCP_OPERATIONS = frozenset({"call", "read-resource", "get-prompt"})
VERIFICATION_FIXTURE_GUIDANCE = (
    "For browser fixture setup, start and navigate to the preview, then call Playwright MCP browser_evaluate "
    "without a check ID. Create disposable records through the preview UI or relative app API URLs. "
    "Use small stages and inspect the actual response before assuming which child records the API creates. "
    "Use HTTP methods and routes confirmed in the app source or prior successful calls. Check HTTP status and Content-Type "
    "before parsing JSON. If an API URL returns HTML, capture its method, path, status, and a short text excerpt. "
    "A successful HTTP status with HTML can be the frontend fallback. "
    'Return {"ready":false,"milestone":"stable step name","evidence":"observed completed step","projectId":"..."} '
    "when part of the fixture is ready. Reuse returned IDs to complete that fixture. "
    'Return {"ready":true,"evidence":"confirmed all required test data exists","projectId":"..."} when setup is complete. '
    'On a failed stage return {"ready":false,"error":"request, HTTP status and failure","response":{}}, '
    "with a small excerpt of the actual response and any IDs already created. "
    "Bind the subsequent assertions to required check IDs."
)
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


def _playwright_tool(action: dict) -> str:
    if (action.get("type") == "mcp" and action.get("operation") == "call"
            and action.get("server") == "playwright" and isinstance(action.get("name"), str)):
        return action["name"]
    return ""


def is_verification_setup(action: dict, *, unbound: bool = False) -> bool:
    tool = _playwright_tool(action)
    return (
        action.get("type") == "artifact_preview" and action.get("operation", "start") == "start"
        or tool in _PLAYWRIGHT_SETUP_TOOLS
        or tool in _PLAYWRIGHT_INPUT_TOOLS
        or (unbound and tool == "browser_evaluate")
    )


def _loopback_origin(url: Any) -> tuple[str, str, int] | None:
    if not isinstance(url, str) or "\\" in url or any(char.isspace() for char in url):
        return None
    try:
        parsed = urlsplit(url)
        if (parsed.scheme in {"http", "https"} and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and parsed.username is None and parsed.password is None):
            port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
            return parsed.scheme, parsed.hostname, port
    except ValueError:
        pass
    return None


def _mcp_text(output: dict) -> list[str]:
    result = output.get("result")
    blocks = result.get("content") if isinstance(result, dict) else output.get("content")
    return [
        item["text"] for item in blocks
        if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
    ] if isinstance(blocks, list) else []


def _bounded_mcp_text(output: dict) -> str:
    observations = "\n".join(_mcp_text(output))
    limit = 3000
    return observations[:limit] + ("\n[output truncated]" if len(observations) > limit else "")


def _playwright_page_url(output: dict) -> str | None:
    for block in _mcp_text(output):
        _, heading, page = block.partition("### Page\n- Page URL: ")
        if heading:
            return page.split("\n", 1)[0].strip()
    return None


def _playwright_page_origin(output: dict) -> tuple[str, str, int] | None:
    return _loopback_origin(_playwright_page_url(output))


def _playwright_result(output: dict) -> Any:
    for block in _mcp_text(output):
        if block.startswith("### Result\n"):
            body = block.removeprefix("### Result\n").split("\n### ", 1)[0].strip()
            try:
                return json.loads(body)
            except ValueError:
                return body
    return None


def _playwright_partial_progress(body: str) -> dict:
    """Read complete entries of a leading progress map from an incomplete result."""
    header = re.match(r'^\s*\{\s*"verification_results"\s*:\s*\{\s*', body)
    if header is None:
        return {}
    # The omitted suffix may contain authoritative failure metadata.
    progress = {}
    decoder = json.JSONDecoder()
    cursor = header.end()
    while cursor < len(body):
        try:
            key, end = decoder.raw_decode(body, cursor)
            colon = re.match(r"\s*:\s*", body[end:])
            if not isinstance(key, str) or colon is None:
                break
            check, end = decoder.raw_decode(body, end + colon.end())
        except ValueError:
            break
        if isinstance(check, dict) and check.get("status") == "unverified":
            progress[key] = check
        comma = re.match(r"\s*,\s*", body[end:])
        if comma is None:
            break
        cursor = end + comma.end()
    return progress


def is_verification_fact(action: dict) -> bool:
    return (action.get("type") in {"set_fact", "update_fact"}
            and str(action.get("fact_type") or "").strip().lower() in ISSUE_LOCAL_FACT_TYPES)


def _has_playwright_sections(output: dict) -> bool:
    return any(block.startswith(("### Result\n", "### Ran Playwright code\n", "### Page\n", "### Error\n"))
               for block in _mcp_text(output))


def _fixture_setup_action(action: dict, origin: tuple[str, str, int]) -> dict:
    arguments = action.get("arguments")
    function = arguments.get("function") if isinstance(arguments, dict) else None
    if not isinstance(function, str) or not function.strip():
        raise ValueError("Provide a Playwright browser_evaluate function for fixture setup.")
    scheme, host, port = origin
    base = f"{scheme}://{'[' + host + ']' if ':' in host else host}:{port}"
    scoped_function = (
        "async () => {\n"
        f"if (location.origin !== new URL({json.dumps(base)}).origin) "
        "throw new Error('Navigate to the intended local preview before creating a fixture.');\n"
        "const requests = [];\n"
        "const fetch = async (...args) => {\n"
        "  const response = await globalThis.fetch(...args);\n"
        "  if (response.url.startsWith(location.origin + '/')) {\n"
        "    requests.push({ method: args[1]?.method ?? args[0]?.method ?? 'GET',\n"
        "      path: new URL(response.url).pathname, status: response.status,\n"
        "      contentType: response.headers.get('content-type')?.split(';')[0] ?? '' });\n"
        "    if (requests.length > 3) requests.shift();\n"
        "  }\n"
        "  return response;\n"
        "};\n"
        "try {\n"
        f"const fixture = await (\n{function}\n)();\n"
        "return { verification_fixture: fixture };\n"
        "} catch (error) {\n"
        "return { verification_fixture: { ready: false, error: String(error), requests } };\n"
        "}\n}"
    )
    return {**action, "arguments": {"function": scoped_function}}


def needs_verification_check(action: dict) -> bool:
    kind = action.get("type")
    return (
        kind in VERIFICATION_ACTIONS and not is_verification_setup(action)
        and (kind != "mcp" or action.get("operation") in _MCP_OPERATIONS)
    )


def _is_mcp_check(action: dict) -> bool:
    return (
        action.get("type") == "mcp" and action.get("operation") in _MCP_OPERATIONS
        or action.get("type") == "artifact_preview" and action.get("operation") == "mcp"
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
        and 1 <= check["attempts"] <= (MAX_MCP_VERIFICATION_CALLS if check.get("mcp_backed") is True
                                      else MAX_VERIFICATION_ATTEMPTS)
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


def _fixture_details(fixture: dict) -> dict:
    """Keep fixture data usable as JSON, even when a response must be abbreviated."""
    limit = 2600
    if len(json.dumps(fixture, ensure_ascii=False)) <= limit:
        return fixture
    core = {"ready", "milestone", "evidence", "error", "requests", "id"}
    keys = sorted(fixture, key=lambda key: key not in core and not key.endswith(("Id", "_id")))
    details: dict = {"truncated": True}
    for key in keys:
        if key == "truncated":
            continue
        value = fixture[key]
        if key in {"milestone", "evidence", "error"}:
            value = _bounded(value)
        if len(json.dumps({**details, key: value}, ensure_ascii=False)) <= limit:
            details[key] = value
    if "response" in fixture and "response" not in details:
        excerpt = json.dumps(fixture["response"], ensure_ascii=False)[:400]
        if len(json.dumps({**details, "response_excerpt": excerpt}, ensure_ascii=False)) <= limit:
            details["response_excerpt"] = excerpt
    return details


def _fixture_http_context(requests: object) -> str:
    if not isinstance(requests, list):
        return ""
    receipts = []
    for request in requests[-3:]:
        if not isinstance(request, dict) or type(request.get("status")) is not int:
            continue
        method = _bounded(request.get("method"), 12)
        path = _bounded(request.get("path"), 160)
        content_type = _bounded(request.get("contentType"), 80)
        if method and path:
            receipts.append(f"{method} {path} → HTTP {request['status']}" + (f" ({content_type})" if content_type else ""))
    return ", ".join(receipts)


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
    native_attempts: int = 0
    mcp_backed: bool = False
    mcp_budget: McpCheckBudget = field(default_factory=McpCheckBudget)
    verdict: Verdict | None = None
    seen: set[tuple[str, str]] = field(default_factory=set)
    repeated_failure: bool = False
    source_errors: list[dict[str, str]] = field(default_factory=list)
    browser_observations: list[dict[str, str]] = field(default_factory=list)
    progress_evidence: str = ""
    latest_unverified: str = ""
    shell_exit_observed: bool = False
    shell_retry_rejected: bool = False
    reused_from: list[str] = field(default_factory=list)

    @property
    def retryable(self) -> bool:
        return self.can_attempt(mcp=self.mcp_backed)

    def can_attempt(self, *, mcp: bool) -> bool:
        return (
            (not self.mcp_budget.stop_reason if mcp else self.native_attempts < MAX_VERIFICATION_ATTEMPTS)
            and not (self.repeated_failure or self.shell_retry_rejected)
            and (self.verdict is None or self.verdict.status in {"fail", "unverified"})
        )

    @property
    def stop_detail(self) -> str:
        if self.retryable or (self.verdict and self.verdict.status in {"pass", "blocked"}):
            return ""
        if self.repeated_failure:
            return "The same failure was observed again."
        if self.shell_retry_rejected:
            return "An exit status was already recorded for this command."
        return self.mcp_budget.stop_reason or f"The check used all {MAX_VERIFICATION_ATTEMPTS} attempts."


class VerificationRun:
    def __init__(self, requirements: Sequence[str], *, review_deferred: bool = False, managed_browser: bool = True,
                 inherited_checks: dict[str, dict] | None = None) -> None:
        self.checks = {f"check-{index}": VerificationCheck(requirement)
                       for index, requirement in enumerate(dict.fromkeys(requirements), 1)}
        for check in self.checks.values():
            prior = (inherited_checks or {}).get(check.requirement)
            if prior and _browser_observations_match(check.requirement, prior.get("browser_observations")):
                check.verdict = Verdict("pass", prior["evidence"])
                check.attempts = 1
                check.browser_observations = prior.get("browser_observations", [])
                check.reused_from = prior["goals"]
        self.turns = 0
        self.turn_limit = MAX_VERIFICATION_TURNS
        self.max_turns, self.max_calls = mcp_session_limits(len(self.checks))
        self.tool_calls = 0
        self.idle_turns = 0
        self._new_evidence = False
        self.setup_calls = 0
        self.mcp_session: McpVerificationSession | None = None
        self._completed_setup: set[str] = set()
        self._new_setup = False
        self._preview_url = ""
        self._browser_origin: tuple[str, str, int] | None = None
        self._confirmed_input_origin: tuple[str, str, int] | None = None
        self._browser_snapshot = ""
        self._fixture_result: dict = {}
        self._fixture_error = ""
        self._fixture_error_result: dict = {}
        self._inconclusive_output = ""
        self._result_format_issue = ""
        self.review_deferred = review_deferred
        self.managed_browser = managed_browser
        self._reporting = False
        self.observed_source_paths: set[str] = set()
        self.repair_handoff: dict | None = None
        self.proposed_repair_targets: list[str] = []

    def observe_source(self, path: str) -> None:
        if path and path != ".":
            self.observed_source_paths.add(path.removeprefix("/repo/").removeprefix("./"))

    def configure_turn_limit(self, limit: int) -> int:
        self.max_turns = min(limit, mcp_session_limits(len(self.checks))[0])
        self.turn_limit = min(limit, MAX_VERIFICATION_TURNS)
        return self.max_turns

    def _start_mcp_session(self) -> None:
        if self.mcp_session is None:
            self.mcp_session = McpVerificationSession(
                turn_checkpoint=self.turn_limit,
                call_checkpoint=MAX_VERIFICATION_TOOL_CALLS + MAX_VERIFICATION_SETUP_CALLS,
                max_turns=self.max_turns,
                max_calls=self.max_calls,
            )

    def _check_mcp_budget(self, requested_calls: int) -> None:
        if self.mcp_session is not None:
            self.mcp_session.review(turns=self.turns, calls=self.tool_calls + self.setup_calls,
                                    requested_calls=requested_calls, fixture_blocker=self._fixture_error,
                                    undispatched=self._undispatched_checks())
            if self.mcp_session.stop_reason:
                raise ValueError(self.mcp_session.stop_reason)

    def _undispatched_checks(self) -> list[str]:
        return [key for key, check in self.checks.items() if not check.attempts and check.retryable]

    def begin_mcp_discovery(self) -> None:
        if self.mcp_session is not None:
            self._check_mcp_budget(1)
            self.tool_calls += 1

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
        diagnosis = verification_diagnosis(handoff.get("diagnosis"))
        if (not diagnosis or not isinstance(targets, list) or not targets or len(targets) > 8
                or any(not isinstance(path, str) for path in targets)
                or not isinstance(ids, list) or not ids or any(not isinstance(key, str) for key in ids)):
            return notes
        if any(not self.checks.get(key) or not self.checks[key].verdict
               or self.checks[key].verdict.status != "fail" for key in ids):
            return notes
        known_paths = self.observed_source_paths | {
            error["path"].removeprefix("/repo/").removeprefix("./")
            for key in ids for error in self.checks[key].source_errors
        }
        normalized = list(dict.fromkeys(path.removeprefix("/repo/").removeprefix("./") for path in targets))
        self.proposed_repair_targets = normalized
        if set(normalized) <= known_paths:
            self.repair_handoff = {"targets": normalized, "check_ids": list(dict.fromkeys(ids)), "diagnosis": diagnosis}
        return diagnosis

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.verdict and check.verdict.status == "pass" for check in self.checks.values())

    @property
    def stop_reason(self) -> str:
        if self.passed:
            turn_limit = self.max_turns if self.mcp_session is not None else self.turn_limit
            if self.review_deferred and not self._reporting and self.turns < turn_limit:
                return ""
            return "All required verification checks passed."
        if self.mcp_session is not None and self.mcp_session.stop_reason:
            return self.mcp_session.stop_reason
        if not any(check.retryable for check in self.checks.values()):
            checks = list(self.checks.values())
            repeated = any(check.repeated_failure for check in checks)
            exhausted = any(
                check.mcp_budget.stop_reason or (
                    not check.mcp_backed and check.native_attempts >= MAX_VERIFICATION_ATTEMPTS
                    and check.stop_detail and not check.repeated_failure
                ) for check in checks
            )
            if repeated and exhausted:
                return "Verification stopped after repeated failures and per-check attempt limits."
            if repeated:
                return "Verification stopped after repeated failures."
            if exhausted:
                return "Verification reached the per-check attempt limit."
            return "All required checks have a recorded result."
        if self.mcp_session is not None:
            return ""
        if self.tool_calls >= MAX_VERIFICATION_TOOL_CALLS:
            return f"The verification tool-call budget was exhausted ({self.tool_calls}/{MAX_VERIFICATION_TOOL_CALLS} calls)."
        if self.turns >= self.turn_limit:
            return f"The verification turn budget was exhausted ({self.turns}/{self.turn_limit} turns)."
        if self.idle_turns >= MAX_TURNS_WITHOUT_EVIDENCE:
            return "The verification run stopped after five turns without new check evidence or setup progress."
        return ""

    def begin_turn(self) -> None:
        self.turns += 1
        self._reporting = self.review_deferred and self.passed

    def begin_setup(self, action: dict) -> dict:
        tool = _playwright_tool(action)
        if tool in _PLAYWRIGHT_INPUT_TOOLS:
            if self._browser_origin is None:
                raise ValueError("Navigate to the local preview with Playwright before sending browser input.")
            if self.mcp_session is None and self.tool_calls + 2 > MAX_VERIFICATION_TOOL_CALLS:
                raise ValueError("The verification tool-call budget has no room for browser input and its preview check.")
            self._check_mcp_budget(2)
            return action
        if self.mcp_session is None and self.setup_calls >= MAX_VERIFICATION_SETUP_CALLS:
            raise ValueError("The verification setup allowance was exhausted. Run the actual assertions or report the setup blocker.")
        if tool == "browser_navigate":
            args = action.get("arguments")
            origin = _loopback_origin(args.get("url")) if isinstance(args, dict) else None
            if self.managed_browser and (origin is None or origin != _loopback_origin(self._preview_url)):
                raise ValueError("Start the artifact preview and navigate to its published URL before browser verification.")
            if origin is None:
                raise ValueError("Playwright setup navigation requires a local preview URL.")
            if self._browser_origin != origin:
                self._clear_browser_context()
            self._browser_origin = None
            self._browser_snapshot = ""
        elif tool == "browser_snapshot":
            if self._browser_origin is None:
                raise ValueError("Navigate to the local preview with Playwright before requesting a setup snapshot.")
            args = action.get("arguments")
            if isinstance(args, dict) and args.get("filename"):
                raise ValueError("Return the Playwright setup snapshot inline by omitting filename.")
        elif tool == "browser_evaluate":
            if self._browser_origin is None:
                raise ValueError("Navigate to the local preview with Playwright before preparing a browser fixture.")
            action = _fixture_setup_action(action, self._browser_origin)
        self._check_mcp_budget(1)
        self.setup_calls += 1
        return action

    def invoke_setup_mcp(self, action: dict, invoke: Callable[[dict], dict]) -> dict:
        if _playwright_tool(action) not in _PLAYWRIGHT_INPUT_TOOLS:
            return invoke(action)
        self._confirmed_input_origin = None

        def probe_origin() -> tuple[str, str, int] | None:
            self.tool_calls += 1
            page = invoke({
                "type": "mcp", "operation": "call", "server": "playwright", "name": "browser_evaluate",
                "arguments": {"function": "() => location.origin"},
            })
            return _loopback_origin(_playwright_result(page)) if page.get("ok") is True else None

        origin = probe_origin()
        if origin is None or origin != self._browser_origin:
            self._clear_browser_context()
            return {"ok": False, "error": "Playwright is no longer on the expected preview. Navigate there before sending browser input."}
        self.tool_calls += 1
        output = invoke(action)
        if output.get("ok") is True and _playwright_page_url(output) is None:
            try:
                self._check_mcp_budget(1)
            except ValueError as exc:
                self._clear_browser_context()
                return {"ok": False, "error": f"Browser input ran. Preview origin could not be rechecked. {exc}"}
            self._confirmed_input_origin = probe_origin()
        return output

    def record_setup(self, action: dict, output: dict) -> dict:
        if output.get("ok") is not True:
            return output
        tool = _playwright_tool(action)
        if tool == "browser_evaluate":
            returned = _playwright_result(output)
            if not isinstance(returned, dict) or "verification_fixture" not in returned:
                return {"ok": False, "error": "The browser evaluation returned no readable observation. Return a compact result."}
            fixture = returned["verification_fixture"]
            if not isinstance(fixture, dict) or "ready" not in fixture:
                return {"ok": True, "inspection": _fixture_details({"response": fixture})}
            ready = fixture.get("ready")
            evidence = _bounded(fixture.get("evidence"))
            error = _bounded(fixture.get("error"), 500)
            details = _fixture_details(fixture)
            if error:
                if http_context := _fixture_http_context(fixture.get("requests")):
                    error = f"{error} Recent preview responses: {http_context}."
                self._fixture_error = error
                self._fixture_error_result = details
                return {"ok": False, "error": error, "fixture": details}
            if type(ready) is bool and evidence:
                self._fixture_result = details
                self._fixture_error = ""
                self._fixture_error_result = {}
                milestone = _bounded(fixture.get("milestone"), 100) or "initial"
                step = "fixture-ready" if ready else f"fixture-partial/{milestone.casefold()}"
                self._mark_setup_progress(step, evidence=evidence)
                return {"ok": True, "ready": ready, "fixture": details}
            return {"ok": False, "error": VERIFICATION_FIXTURE_GUIDANCE}
        if not tool:
            url = output.get("url")
            origin = _loopback_origin(url)
            self._preview_url = url if origin is not None else ""
            if self.managed_browser and self._browser_origin != origin:
                self._clear_browser_context()
            complete = origin is not None
        else:
            origin = _playwright_page_origin(output)
            if tool in _PLAYWRIGHT_INPUT_TOOLS:
                origin = origin or self._confirmed_input_origin
                self._confirmed_input_origin = None
            expected = (_loopback_origin(action["arguments"]["url"]) if tool == "browser_navigate"
                        else self._browser_origin)
            if origin is None or origin != expected:
                self._clear_browser_context()
                return {"ok": False, "error": "Playwright did not report a page on the expected local preview origin."}
            self._browser_origin = origin
            if tool == "browser_snapshot":
                self._browser_snapshot = _bounded_mcp_text(output)
            elif tool in _PLAYWRIGHT_INPUT_TOOLS:
                self._browser_snapshot = ""
            complete = True
        if complete:
            if tool == "browser_navigate":
                self._start_mcp_session()
            self._mark_setup_progress(tool or "preview-start")
        return output

    def _clear_browser_context(self) -> None:
        self._browser_origin = None
        self._confirmed_input_origin = None
        self._browser_snapshot = ""
        self._fixture_result = {}
        self._fixture_error = ""
        self._fixture_error_result = {}

    def _mark_setup_progress(self, milestone: str, *, evidence: str = "") -> None:
        if milestone not in self._completed_setup:
            self._completed_setup.add(milestone)
            if self.mcp_session is None:
                self._new_setup = True
            else:
                description = evidence or {
                    "preview-start": "Published the preview URL.",
                    "browser_navigate": "Reached the local preview in the browser.",
                    "browser_snapshot": "Inspected the preview page.",
                }.get(milestone, f"Dispatched verified preview input using {milestone}.")
                self._new_setup |= self.mcp_session.observe("Setup", milestone, description)

    def _custom_browser_check_allowed(self, action: dict) -> bool:
        if not self.managed_browser:
            return action.get("type") in {"mcp", "run_shell"}
        return (_playwright_tool(action) in _PLAYWRIGHT_ASSERTION_TOOLS
                and self._browser_origin is not None and self._browser_origin == _loopback_origin(self._preview_url))

    def begin_checks(self, ids: Any, *, action: dict | None = None) -> list[str]:
        mcp = action is not None and _is_mcp_check(action)
        if self.mcp_session is None and not mcp and self.tool_calls >= MAX_VERIFICATION_TOOL_CALLS:
            raise ValueError("The verification tool-call budget was exhausted.")
        if not isinstance(ids, (list, tuple)) or not ids or any(not isinstance(item, str) for item in ids):
            raise ValueError("Provide one or more required check IDs.")
        selected = list(dict.fromkeys(ids))
        for key in selected:
            if key not in self.checks:
                raise ValueError(f"Unknown verification check {key}.")
            check = self.checks[key]
            if not check.can_attempt(mcp=mcp):
                detail = check.stop_detail or "The check used all of its native attempts."
                if check.verdict and check.verdict.status in {"pass", "blocked"}:
                    detail = f"Recorded result: {check.verdict.status}."
                raise ValueError(f"{key} is closed. {detail} Use the recorded evidence.")
            if (action is not None and action.get("type") == "run_shell" and check.shell_exit_observed
                    and _shell_commands(action.get("command")) == _approved_exit_commands(check.requirement)):
                check.shell_retry_rejected = True
                raise ValueError(f"{key} already has an exit status for this command. Use the recorded evidence to diagnose the failure.")
            if action is not None and _browser_assertion_count(check.requirement):
                managed_check = action.get("type") == "artifact_preview" and action.get("operation") == "browser-check"
                if not (managed_check or self._custom_browser_check_allowed(action)):
                    instructions = ("Use artifact-preview browser-check with visible-state assertions, or navigate to the published preview with Playwright and run an assertion." if self.managed_browser
                                    else "Use a Playwright tool or script that reports the observed browser steps.")
                    raise ValueError(f"{key} requires browser interaction evidence. {instructions}")
        if mcp:
            self._start_mcp_session()
        self._check_mcp_budget(1)
        for key in selected:
            check = self.checks[key]
            check.attempts += 1
            if mcp:
                check.mcp_backed = True
                check.mcp_budget.attempts += 1
            else:
                check.native_attempts += 1
        self.tool_calls += 1
        return selected

    def record(self, ids: list[str], *, kind: str, ok: bool, output: Any, unavailable: bool = False, action: dict | None = None) -> dict:
        action = action or {}
        verdicts: dict = {}
        errors: list[str] = []
        playwright = isinstance(output, dict) and _playwright_tool(action) in _PLAYWRIGHT_ASSERTION_TOOLS
        headed_result = playwright and _has_playwright_sections(output)
        self._result_format_issue = ""
        _tool_findings(output, verdicts, errors, accept_verdicts=not headed_result)
        if headed_result:
            result = _playwright_result(output)
            if isinstance(result, str) and result.lstrip().startswith(("{", "[")):
                verdicts.update(_playwright_partial_progress(result))
                self._result_format_issue = (
                    "Playwright returned incomplete or invalid JSON. Return a compact verification_results object "
                    "with only the bound check IDs. Inspect large DOM or diagnostic output in a separate call."
                )
            else:
                _tool_findings(result, verdicts, errors)
            if not ok:
                errors.extend(_bounded(block.removeprefix("### Error\n")) for block in _mcp_text(output)
                              if block.startswith("### Error\n"))
        native_summary = _bounded(output.get("summary") if isinstance(output, dict) else output)
        source_errors = compiler_errors(output) if kind in {"run_shell", "run_check", "diagnose"} else []
        browser = kind == "artifact_preview" and action.get("operation") == "browser-check"
        mcp = _is_mcp_check(action)
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
            custom_browser = self._custom_browser_check_allowed(action) and status == "pass" and bool(evidence)
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
                observed_failure = (
                    bool(status == "fail" and evidence)
                    or bool(source_errors)
                    or (approved_shell and observed_exit and output["returncode"] != 0)
                    or kind in NATIVE_CHECKS
                    or (browser and isinstance(output, dict) and isinstance(output.get("observations"), list))
                )
                verdict = (Verdict("fail", details, next_step) if observed_failure else Verdict(
                    "unverified", f"Verifier error: {details}",
                    "Fix the verifier error and rerun the check.",
                ))
            elif status == "unverified" and evidence:
                verdict = Verdict(status, evidence, next_step or "Resolve the verifier error and retry this check.")
            elif browser and isinstance(output, dict) and output.get("ok") is True and observations and _browser_observations_match(check.requirement, observations):
                observed = [item["label"] for item in observations if isinstance(item, dict) and _bounded(item.get("label"))]
                verdict = Verdict("pass", _bounded("Managed browser observed " + " → ".join(observed)))
            elif browser or _browser_assertion_count(check.requirement):
                if custom_browser and observations and _browser_observations_match(check.requirement, observations):
                    verdict = Verdict("pass", evidence)
                elif status in ("fail", "blocked") and evidence:
                    verdict = Verdict(status, evidence, next_step)
                else:
                    verdict = Verdict("unverified", "The browser result did not establish the required UI states and interactions.",
                                      "Run a browser assertion that covers the required UI states and interactions.")
            elif (approved_shell and observed_exit and output["returncode"] == 0
                  and status not in ("fail", "blocked")):
                verdict = Verdict("pass", _bounded(" && ".join(" ".join(part) for part in commands) + " exited 0."))
            elif status in ("pass", "fail", "blocked") and evidence:
                verdict = Verdict(status, evidence, next_step)
            elif not verdicts and len(ids) == 1 and kind in NATIVE_CHECKS and native_summary:
                verdict = Verdict("pass", native_summary)
            else:
                verdict = Verdict("unverified", "The tool ran without a usable check verdict.",
                                  "Run the required assertion and return a status and concrete evidence for this check.")

            if verdict.status == "unverified":
                if not supplied and self._result_format_issue and ok and not errors and not unavailable:
                    verdict = Verdict("unverified", "Playwright returned incomplete or invalid assertion JSON.",
                                      "Rerun the assertion with a compact result containing only the bound checks.")
                check.latest_unverified = verdict.evidence
                if check.verdict is None or check.verdict.status == "unverified":
                    check.verdict = verdict
                progress = record.get("progress")
                new_progress = False
                if ok and not unavailable and not errors and self.mcp_session is not None and mcp and isinstance(progress, dict):
                    milestone = _bounded(progress.get("milestone"), 100)
                    observation = _bounded(progress.get("evidence"), 300)
                    if milestone and observation and self.mcp_session.observe(key, f"progress/{milestone}", observation):
                        check.progress_evidence = observation
                        self._new_evidence = True
                        new_progress = True
                if mcp:
                    check.mcp_budget.review(new_evidence=new_progress)
                continue

            check.latest_unverified = ""
            check.browser_observations = observations if verdict.status == "pass" else []
            check.source_errors = []
            if verdict.status == "fail" and kind in {"run_shell", "run_check", "diagnose"}:
                check.source_errors = compiler_errors(record.get("evidence")) or source_errors
            identity = (verdict.status, verdict.evidence.casefold())
            new_progress = False
            if identity not in check.seen:
                check.seen.add(identity)
                if self.mcp_session is None:
                    self._new_evidence = True
                else:
                    new_progress = self.mcp_session.observe(key, verdict.status, verdict.evidence)
                    self._new_evidence |= new_progress
            elif verdict.status == "fail":
                check.repeated_failure = True
            check.verdict = verdict
            if mcp and verdict.status == "fail" and not check.repeated_failure:
                # Reporting a different failure still needs a distinct, meaningful observation
                # to earn continuation. The first observed failure permits a focused follow-up.
                check.mcp_budget.review(new_evidence=new_progress)
        state = self.snapshot()
        if self._result_format_issue:
            state["result_format_issue"] = self._result_format_issue
        if kind == "mcp":
            inconclusive = isinstance(output, dict) and any(self.checks[key].latest_unverified for key in ids)
            self._inconclusive_output = _bounded_mcp_text(output) if inconclusive else ""
            if self._inconclusive_output:
                state["inconclusive_tool_output"] = self._inconclusive_output
        return state

    def finish_turn(self) -> None:
        if self._new_evidence or self._new_setup:
            self.idle_turns = 0
        else:
            self.idle_turns += 1
        self._new_evidence = False
        self._new_setup = False
        if self.mcp_session is not None and not self.passed and (
            any(check.retryable for check in self.checks.values())
            or self.turns >= self.mcp_session.max_turns
        ):
            self.mcp_session.review(turns=self.turns, calls=self.tool_calls + self.setup_calls,
                                    stalled=self.idle_turns >= MAX_TURNS_WITHOUT_EVIDENCE,
                                    fixture_blocker=self._fixture_error, undispatched=self._undispatched_checks())

    def snapshot(self, *, final: bool = False, reason: str = "", notes: str = "") -> dict:
        stop_reason = ""
        if final:
            stop_reason = reason or self.stop_reason
            if not stop_reason:
                outstanding = sum(check.verdict is None or check.verdict.status != "pass" for check in self.checks.values())
                if self.repair_handoff:
                    ids = ", ".join(self.repair_handoff["check_ids"])
                    stop_reason = f"Verification handed off {ids} for repair. {outstanding} required checks remain open."
                else:
                    stop_reason = f"The verifier returned a report with {outstanding} required checks still open."
        checks = []
        for key, check in self.checks.items():
            verdict = check.verdict
            status = verdict.status if verdict else ("pending" if not final else "unverified" if check.attempts else "not_run")
            evidence = verdict.evidence if verdict else ""
            if final and not verdict:
                evidence = "The tool started but returned no check result." if check.attempts else "No verification tool was dispatched for this check."
            next_step = verdict.next_step if verdict else ""
            if final and not next_step:
                if status == "fail" and check.mcp_backed and not check.source_errors:
                    next_step = "Confirm the test inputs and observed behavior, then isolate and rerun the failing assertion."
                elif status == "fail":
                    next_step = "Trace the recorded failure to its source, repair it if in scope, and rerun the check."
                elif status == "blocked":
                    next_step = "Resolve the recorded prerequisite and rerun this check."
                elif status in {"not_run", "unverified"}:
                    next_step = "Run this check and record the observed result."
            stop_detail = check.stop_detail or (stop_reason if final and status not in {"pass", "blocked"} else "")
            checks.append({
                "id": key, "requirement": check.requirement, "attempts": check.attempts,
                "status": status, "evidence": evidence,
                "next_step": next_step,
                "retryable": check.retryable and not final,
                **({"mcp_backed": True, "mcp_attempts": check.mcp_budget.attempts,
                    "mcp_attempt_checkpoint": check.mcp_budget.checkpoint} if check.mcp_backed else {}),
                **({"progress_evidence": check.progress_evidence} if check.progress_evidence else {}),
                **({"stop_reason": stop_detail} if stop_detail else {}),
                **({"latest_attempt": check.latest_unverified} if status == "fail" and check.latest_unverified else {}),
                **({"source_errors": check.source_errors} if check.source_errors else {}),
                **({"browser_observations": check.browser_observations} if check.browser_observations else {}),
                **({"reused_from": check.reused_from} if check.reused_from else {}),
            })
        return {
            "passed": self.passed, "turns_used": self.turns,
            "turn_limit": self.max_turns if self.mcp_session is not None else self.turn_limit,
            "tool_calls_used": self.tool_calls,
            "tool_call_limit": self.max_calls if self.mcp_session is not None else MAX_VERIFICATION_TOOL_CALLS,
            "setup_calls_used": self.setup_calls,
            **({"setup_call_limit": MAX_VERIFICATION_SETUP_CALLS} if self.mcp_session is None else {}),
            "turns_without_new_evidence": self.idle_turns, "attempt_limit": MAX_VERIFICATION_ATTEMPTS,
            "checks": checks,
            **({"mcp_session": self.mcp_session.snapshot(calls=self.tool_calls + self.setup_calls)} if self.mcp_session is not None else {}),
            **({"stop_reason": stop_reason} if stop_reason else {}),
            **({"worker_notes": notes.strip()[:2000]} if final and notes.strip() else {}),
            **({"repair_handoff": self.repair_handoff} if final and self.repair_handoff else {}),
            **({"proposed_repair_targets": self.proposed_repair_targets} if final and self.proposed_repair_targets else {}),
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
            "Use verify check-1,check-2 <tool command>, for example verify check-1 run-check test. "
            "Send Playwright JavaScript as a literal block to preserve regex and string escapes:\n"
            'verify check-1 mcp {"operation":"call","server":"playwright","name":"browser_evaluate"} <<<\n'
            '() => ({ verification_results: { "check-1": { status: document.title ? "pass" : "fail", '
            'evidence: "Observed page title: " + document.title } } })\n'
            ">>>"
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
        state = self.snapshot()
        if self._preview_url:
            state["published_preview_url"] = self._preview_url
        if self._browser_snapshot:
            state["latest_setup_snapshot"] = self._browser_snapshot
        if self._fixture_result:
            state["latest_fixture_setup"] = self._fixture_result
        fixture_recovery = ""
        if self._fixture_error:
            state["latest_fixture_error_response"] = self._fixture_error_result
            fixture_recovery = (
                "FIXTURE RECOVERY\n"
                "Use the last response and existing fixture IDs to correct the failed setup step on this turn. "
                "Keep any lookup focused on the failing endpoint or field and report the blocker if setup cannot be corrected.\n"
            )
        if self._inconclusive_output:
            state["inconclusive_tool_output"] = self._inconclusive_output
        if self._result_format_issue:
            state["result_format_issue"] = self._result_format_issue
        coverage_guidance = ""
        if self.mcp_session is not None and (undispatched := self._undispatched_checks()):
            focus = 0 < self.mcp_session.coverage_until_turn and self.turns <= self.mcp_session.coverage_until_turn
            coverage_guidance = (
                f"{'FOCUSED COVERAGE WINDOW' if focus else 'UNDISPATCHED CHECKS'} — {', '.join(undispatched)}. "
                "Use a bound verification call for the next observable precondition on these checks. "
                "Reuse the current fixture and preconditions already established. "
                "If a prerequisite is unavailable, report the exact blocker for that check.\n"
            )
        return (
            "VERIFICATION GOAL\n" + command + "\n" + coverage_guidance + fixture_recovery +
            'For custom scripts print one stdout line VERIFICATION_RESULTS: {"check-1":{"status":"pass","evidence":"observed result"}}. '
            'For MCP, return a result object with a verification_results map or emit the same marked text. '
            'Keep bound Playwright results compact. Return only verdicts or progress for the checks the call actually covers. '
            'Put per-check browser_observations inside its verdict, and use a separate call for large DOM snapshots. '
            'Each MCP-backed check earns more attempts only when it produces new check-specific evidence. '
            'Setup and all checks share a capped session budget. Cover independent checks early, and avoid repeating passed or closed checks. '
            'Checks marked reused_from passed in earlier goals on the current source. Run only outstanding checks. '
            'If a tool completes a check step before '
            'you can give a verdict, return status "unverified" and include progress {"milestone":"stable step name",'
            '"evidence":"concrete observation from this tool call"} on that check. '
            'Use "fail" only when a valid assertion reproduces a product failure. Verify selectors in the source or live DOM, '
            'confirm fixture identity and prerequisites, and use "unverified" with a concrete next_step for an inconclusive probe. '
            'If one interaction is a prerequisite for another check, give the dependent check its own verified fixture and preconditions '
            'or report it as unverified with the missing prerequisite. Use "blocked" for an unavailable prerequisite or environment. '
            'Include evidence and a concrete next_step per outstanding check, especially when nearing a budget limit. A successful tool call alone '
            "does not establish a verdict. Use Playwright MCP browser_navigate and browser_snapshot without verify to explore the local preview. "
            "These setup calls leave check attempts available and share the overall call budget once the MCP session opens. "
            "For real pointer or keyboard input, use Playwright browser_click, browser_drag, or browser_press_key in the preview session. "
            "Use browser_select_option for a native select when the tool is available. "
            "Use target references from the latest browser snapshot or a selector verified against that snapshot. "
            "A drag can partially run before timing out. Inspect the current snapshot and the reported hit target before further input. "
            "These tools use the overall tool-call allowance and do not charge check attempts. "
            "Use them for pointer capture, dragging, or scrubbing. After input, verify preconditions such as the selected clip "
            "and the playhead being inside its range before clicking Split. Use bound browser_evaluate for measurements and assertions. "
            "Verify independent observations before risky interactions, using short separate calls in one turn. "
            "An unbound browser_evaluate can return a compact observation without supplying fixture readiness or a check verdict. "
            "When one script covers several checks, isolate each check with try/catch and return all accumulated results, "
            'including "unverified" with the verifier error for any check needing a retry. '
            "For each assertion return "
            'one result object like {"verification_results":{"check-2":{"status":"pass","evidence":"actual observed result"}}}. '
            'For a browser workflow include "browser_observations" with observed assertion and transition steps. '
            "Check visible state after interactions and after reload when persistence is required. "
            "Bind each check to its actual assertion, and propagate script errors. "
            + browser_guidance + VERIFICATION_FIXTURE_GUIDANCE + " " +
            'Use steps such as {"type":"assert-visible","selector":"...","label":"Project Home"}, '
            '{"type":"click","selector":"...","label":"Open project"}, then assert the workspace in the same browser session. '
            "A plain route load only covers route health. "
            "For API smoke tests, derive payloads from the actual request schema, assert status and response fields, "
            "include the response body on failure, and use temporary test data. "
            "A native run-check, diagnose, or run-route-check can report its own success for one matching check. "
            "Use normal source reads to find exact inputs and diagnose failures. Reuse recorded exit statuses for approved commands and skip checks with a final verdict. "
            "Use issue-local verification facts to record observations. Continue independent checks while budget remains. "
            "Use finish to summarize findings and recommend next steps for failures or blocked checks. "
            'When you have grounded an in-scope repair, use finish with JSON {"check_ids":["check-2"],'
            '"targets":["path/to/file"],"diagnosis":"cause, evidence, and a concrete repair"}. '
            "On the last available turn, favor that report over starting another investigation.\n"
            + shell_guidance
            + json.dumps(state, ensure_ascii=False)
        )

    def report(self, reason: str = "", notes: str = "") -> str:
        snapshot = self.snapshot(final=True, reason=reason, notes=notes)
        checks = snapshot["checks"]
        passed = sum(check["status"] == "pass" for check in checks)
        lines = [f"Verification: {passed}/{len(checks)} required checks passed."]
        lines.append(snapshot["stop_reason"])
        for check in checks:
            label = check["status"].replace("_", " ").upper()
            detail = check.get("stop_reason")
            if detail and detail != snapshot["stop_reason"]:
                detail = f" {detail}"
            else:
                detail = ""
            lines.append(f"- {check['id']} [{label}] {check['requirement']} — {check['evidence']}{detail}")
        unresolved = [check for check in checks if check["status"] != "pass"]
        if unresolved:
            lines.append("\nNext steps")
            for check in unresolved:
                lines.append(f"- {check['id']}: {check['next_step']}")
        if notes:
            lines.append(f"\nWorker notes\n{notes.strip()[:2000]}")
        return "\n".join(lines)
