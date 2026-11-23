"""Compiler evidence and file scope for the planner's bounded repair pass."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from diagnostics.common import strip_ansi
from mutation_runtime import EXPLICIT_MUTATION_TYPES


MAX_REPAIR_ROUNDS = 2
MAX_INVESTIGATION_TURNS = 4
MAX_REPAIR_TURNS = 8
_SOURCE_PATH = r"(?:[\w.@-]+/)*[\w.@-]+\.(?:[cm]?[jt]s|[jt]sx|py|html?|css)"
_COMPILER_ERROR = re.compile(
    rf"(?<![\w./@-])(?P<path>/?{_SOURCE_PATH})"
    r"(?:\(\d+,\s*\d+\):\s*error\s+|:\d+:\d+\s*(?:-\s*error\s+|\s+))"
    r"(?:TS\d+|PY_COMPILE)\b[^\n]*(?:\n[ \t]+[^\n]*){0,6}"
)
_PYTHON_ERROR = re.compile(
    r'File "(?P<path>[^"\n]+\.py)", line \d+[^\n]*\n'
    r'(?:[ \t]+[^\n]*\n){0,4}(?:SyntaxError|IndentationError|TabError):[^\n]{0,300}'
)
_SCOPE_PATH = re.compile(rf"(?<![\w./@-])(?:\./)?{_SOURCE_PATH}(?![\w./@-])")
_TARGET_PATH = rf"[`'\"]?{_SCOPE_PATH.pattern}[`'\"]?"
_APPROVED_WRITE_TARGETS = re.compile(
    rf"^\s*(?:adjust|change|edit|fix|modify|redesign|refactor|repair|update)\s+"
    rf"{_TARGET_PATH}(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+|\s*\(\s*and\s+){_TARGET_PATH})*",
    flags=re.IGNORECASE,
)
_SCOPE_DIRECTORY = re.compile(r"(?<![\w./@-])(?:\./)?(?:[\w.-]+/)+(?=\*|[\s,`)]|$)")
_BEHAVIOR_SCOPE = re.compile(
    r"\b(?:new features|unrelated (?:behavior|code|components|features|logic|modules))"
    r"\s+(?:in|within)\s*$", re.IGNORECASE,
)
_REPAIR_EDITS = (EXPLICIT_MUTATION_TYPES - {"batch_mutate", "create_file", "delete_file", "rename_file", "copy_file", "fill_template"}) | {
    "write_file", "replace_lines", "patch_file",
}
_UNSCOPED_ACTIONS = {
    "run_shell", "run_check", "run_route_check", "artifact_preview", "mcp",
    "npm_command", "approve_npm_command", "git_add", "git_restore", "git_commit",
    "git_mv", "git_rm", "git_push",
}
_COMPILER_LOCATION = re.compile(r'\(\d+,\s*\d+\)(?=:)|:\d+:\d+(?=\s)|(?<=\.py"), line \d+')


def verification_failure_identity(checks: list[dict]) -> tuple:
    """Treat moved compiler locations and reordered diagnostics as the same evidence."""
    failures = []
    for check in checks:
        if check["status"] == "pass":
            continue
        details = {
            _COMPILER_LOCATION.sub("", error["evidence"])
            for error in check.get("source_errors", [])
        } or {check.get("evidence", "")}
        failures.append((check["id"], check["status"], tuple(sorted(details))))
    return tuple(failures)


def compiler_errors(output: Any) -> list[dict[str, str]]:
    """Read source diagnostics from concrete check output or bound failure evidence."""
    if isinstance(output, str):
        clean = "\n".join(line for line in strip_ansi(output).splitlines() if not line.lstrip().startswith("VERIFICATION_RESULTS:"))
        return [
            {"path": match["path"], "evidence": _compact_compiler_evidence(match[0])}
            for pattern in (_COMPILER_ERROR, _PYTHON_ERROR)
            for match in pattern.finditer(clean) if not match["path"].startswith("//")
        ][:8]
    if isinstance(output, dict):
        errors = []
        for key in ("stdout", "stderr", "result", "data"):
            errors.extend(compiler_errors(output.get(key)))
        return errors[:8]
    return []


def _compact_compiler_evidence(text: str) -> str:
    # TypeScript often puts the actionable mismatch after a long type display.
    evidence = " ".join(text.split())
    if len(evidence) <= 500:
        return evidence
    return f"{evidence[:200].rstrip()} … {evidence[-297:].lstrip()}"


def existing_source_path(root: Path, value: str) -> str:
    path = root / value
    try:
        relative = path.resolve().relative_to(root.resolve()).as_posix()
        if re.fullmatch(_SOURCE_PATH, relative) and path.is_file():
            return relative
    except (OSError, RuntimeError, ValueError):
        pass
    return ""


def named_source_paths(text: str) -> list[str]:
    return _SCOPE_PATH.findall(text)


def approved_write_paths(goal: str) -> list[str]:
    """Read explicit path operands of edits in the approved goal description."""
    match = _APPROVED_WRITE_TARGETS.match(goal)
    return named_source_paths(match[0]) if match else []


def excluded_source_paths(scope: list[str]) -> tuple[set[str], set[str]]:
    exclusions = [re.split(r"\b(?:except|beyond|unless|other than)\b", item, maxsplit=1, flags=re.I)[0] for item in scope]

    def whole_targets(pattern: re.Pattern[str]) -> set[str]:
        return {
            match[0].removeprefix("./")
            for item in exclusions for match in pattern.finditer(item)
            if not _BEHAVIOR_SCOPE.search(item[:match.start()].rstrip("`'\""))
        }

    return whole_targets(_SCOPE_PATH), whole_targets(_SCOPE_DIRECTORY)


def repair_action_error(action: dict, *, root: Path, paths: frozenset[str]) -> str:
    """Constrain the automatic repair to direct edits of the identified files."""
    kind = action.get("type")
    if not paths and kind in _UNSCOPED_ACTIONS | _REPAIR_EDITS | EXPLICIT_MUTATION_TYPES | {
        "begin_edit_batch", "end_edit_batch", "propose_issue", "set_fact", "update_fact",
    }:
        return "Verification investigation is read-only. Inspect the failure and return findings to the planner."
    if kind in _UNSCOPED_ACTIONS:
        return "The repair can inspect source, edit the listed files, and run file diagnostics. The verifier runs the required checks."
    if kind not in EXPLICIT_MUTATION_TYPES | _REPAIR_EDITS:
        return ""
    operations = action.get("operations") if kind == "batch_mutate" else [action]
    if isinstance(operations, list) and operations and all(
        isinstance(item, dict)
        and item.get("type") in _REPAIR_EDITS
        and "root" not in item
        and (targets := [item[key] for key in ("path", "file_path") if key in item])
        and all(
            isinstance(path, str) and not path.startswith("repo/")
            and path in paths and existing_source_path(root, path) == path
            for path in targets
        )
        for item in operations
    ):
        return ""
    return f"Use direct edits to these existing repair files only: {', '.join(sorted(paths))}."
