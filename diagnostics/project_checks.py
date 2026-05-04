from __future__ import annotations

import json
import re
import shlex
import subprocess
from hashlib import sha1
from pathlib import Path
from typing import Optional

from .common import (
    command_failure_message,
    combine_command_output,
    detect_changed_files,
    find_command,
    iter_project_files,
    normalize_input_paths,
    resolve_root,
    run_command,
    safe_tail,
)
from .file_checks import (
    config_validate,
    dependency_check,
    lint_check,
    policy_check,
    security_check,
    syntax_check,
    type_check,
)
from .models import Diagnostic, DiagnosticsResult, make_diagnostic, make_result, merge_results


_PYTEST_FAILURE_RE = re.compile(r"(?P<path>[^\s:]+\.py):(?P<line>\d+):\s+(?P<message>.+)$", flags=re.MULTILINE)
_TAP_FAILURE_RE = re.compile(r"not ok\s+\d+\s+-\s+(?P<message>.+)")


def build_check(targets: Optional[list[str]] = None, *, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    raw_sources: list[str] = []
    diagnostics: list[Diagnostic] = []
    candidate_dirs = _candidate_project_dirs(workspace_root, targets)
    npm = find_command("npm", root=workspace_root)
    for directory in candidate_dirs:
        package_json = directory / "package.json"
        if not package_json.exists() or npm is None:
            continue
        scripts = _read_package_scripts(package_json)
        command: list[str] | None = None
        if "test:compile" in scripts:
            command = [npm, "run", "test:compile"]
        elif "build" in scripts:
            command = [npm, "run", "build"]
        if command is None:
            continue
        result = run_command(command, cwd=directory, timeout=120)
        rel_dir = str(directory.relative_to(workspace_root)).replace("\\", "/") or "."
        raw_sources.append(f"build_check ran {' '.join(command)} in {rel_dir} returncode={result.returncode}")
        if result.returncode != 0:
            diagnostics.append(
                make_diagnostic(
                    tool="build_check",
                    category="build",
                    severity="error",
                    file=rel_dir,
                    code="BUILD_FAILED",
                    message=command_failure_message(
                        result.stdout,
                        result.stderr,
                        fallback=f"Build command failed with exit code {result.returncode}.",
                    ),
                    suggestion="Fix the build or compile errors and rerun build_check.",
                    confidence=0.9,
                )
            )
    return make_result(diagnostics, raw_sources=raw_sources)


def test_check(
    targets: Optional[list[str]] = None,
    mode: str = "related",
    *,
    root: Optional[Path | str] = None,
) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    diagnostics: list[Diagnostic] = []
    raw_sources: list[str] = [f"test_check mode={mode}"]

    pytest_args = _select_pytest_targets(workspace_root, targets, mode)
    if pytest_args:
        command = [str(_python_executable(workspace_root)), "-m", "pytest", *pytest_args]
        try:
            result = run_command(command, cwd=workspace_root, timeout=180)
            raw_sources.append(f"pytest returncode={result.returncode}")
        except subprocess.TimeoutExpired:
            diagnostics.append(
                make_diagnostic(
                    tool="pytest",
                    category="test",
                    severity="error",
                    file="tests",
                    code="TEST_TIMEOUT",
                    message="Pytest exceeded the diagnostics timeout.",
                    suggestion="Run a narrower test target or increase the timeout for this diagnostics pass.",
                    confidence=0.9,
                )
            )
        else:
            if result.returncode != 0:
                combined = combine_command_output(result.stdout, result.stderr)
                matches = list(_PYTEST_FAILURE_RE.finditer(combined))
                if matches:
                    for match in matches:
                        diagnostics.append(
                            make_diagnostic(
                                tool="pytest",
                                category="test",
                                severity="error",
                                file=match.group("path"),
                                line=int(match.group("line")),
                                column=1,
                                end_line=int(match.group("line")),
                                end_column=1,
                                code="TEST_FAILED",
                                message=_pytest_failure_message(combined, match.group("message").strip()),
                                suggestion="Inspect the failing assertion or test setup.",
                                confidence=0.9,
                            )
                        )
                else:
                    diagnostics.append(
                        make_diagnostic(
                            tool="pytest",
                            category="test",
                            severity="error",
                            file="tests",
                            code="TEST_FAILED",
                            message=command_failure_message(
                                result.stdout,
                                result.stderr,
                                fallback=f"Pytest reported failures with exit code {result.returncode}.",
                            ),
                            suggestion="Inspect the failing test output.",
                            confidence=0.85,
                        )
                    )

    if mode in {"full", "deep"}:
        diagnostics.extend(_extension_test_diagnostics(workspace_root, raw_sources))

    return make_result(diagnostics, raw_sources=raw_sources)


test_check.__test__ = False


def runtime_smoke_check(target: Optional[str] = None, *, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    raw_sources: list[str] = []
    diagnostics: list[Diagnostic] = []
    if not target:
        return make_result([], raw_sources=["runtime_smoke_check: no target provided or inferred"])
    command = shlex.split(target)
    result = run_command(command, cwd=workspace_root, timeout=45)
    raw_sources.append(f"runtime_smoke_check ran {' '.join(command)} returncode={result.returncode}")
    if result.returncode != 0:
        diagnostics.append(
            make_diagnostic(
                tool="runtime_smoke_check",
                category="runtime",
                severity="error",
                file=".",
                code="RUNTIME_SMOKE_FAILED",
                message=command_failure_message(
                    result.stdout,
                    result.stderr,
                    fallback=f"Runtime smoke check failed with exit code {result.returncode}.",
                ),
                suggestion="Inspect the startup command and required environment.",
                confidence=0.9,
            )
        )
    return make_result(diagnostics, raw_sources=raw_sources)


def dead_code_check(paths: list[str], scope: str = "project", *, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    vulture = find_command("vulture", root=workspace_root)
    raw_sources = [f"dead_code_check scope={scope}"]
    if not vulture:
        return make_result([], raw_sources=raw_sources + ["dead_code_check: skipped because vulture is unavailable"])
    normalized = normalize_input_paths(workspace_root, paths)
    if not normalized:
        return make_result([], raw_sources=raw_sources)
    result = run_command([vulture, *normalized], cwd=workspace_root, timeout=90)
    diagnostics: list[Diagnostic] = []
    pattern = re.compile(r"^(?P<path>[^:]+):(?P<line>\d+):\s+(?P<message>.+)$", flags=re.MULTILINE)
    for match in pattern.finditer(result.stdout):
        diagnostics.append(
            make_diagnostic(
                tool="vulture",
                category="dead_code",
                severity="warning",
                file=match.group("path"),
                line=int(match.group("line")),
                column=1,
                end_line=int(match.group("line")),
                end_column=1,
                code="DEAD_CODE",
                message=match.group("message").strip(),
                suggestion="Remove dead code or add an allowlist if the symbol is intentionally dynamic.",
                confidence=0.8,
            )
        )
    raw_sources.append(f"vulture returncode={result.returncode}")
    return make_result(diagnostics, raw_sources=raw_sources)


def duplication_check(
    paths: list[str],
    threshold: int = 30,
    *,
    root: Optional[Path | str] = None,
) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    diagnostics: list[Diagnostic] = []
    windows: dict[str, tuple[str, int, list[str]]] = {}
    for rel_path in normalize_input_paths(workspace_root, paths):
        path = workspace_root / rel_path
        if not path.exists() or not path.is_file():
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) < threshold:
            continue
        for index in range(0, len(lines) - threshold + 1):
            block = [line.rstrip() for line in lines[index : index + threshold]]
            if not any(part.strip() for part in block):
                continue
            digest = sha1("\n".join(block).encode("utf-8")).hexdigest()
            existing = windows.get(digest)
            if existing is None:
                windows[digest] = (rel_path, index + 1, block)
                continue
            other_file, other_line, other_block = existing
            if other_file == rel_path:
                continue
            diagnostics.append(
                make_diagnostic(
                    tool="duplication_check",
                    category="duplication",
                    severity="warning",
                    file=rel_path,
                    line=index + 1,
                    column=1,
                    end_line=index + threshold,
                    end_column=1,
                    code="DUPLICATE_BLOCK",
                    message=f"Duplicate block of {threshold} lines also appears in {other_file}:{other_line}.",
                    suggestion="Extract the shared logic into a common helper.",
                    related_files=[other_file],
                    confidence=0.88,
                )
            )
    return make_result(diagnostics)


def changed_files_check(*, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    changed = detect_changed_files(workspace_root)
    if not changed:
        return make_result([], raw_sources=["changed_files_check: no modified or untracked files"])
    return merge_results(
        syntax_check(changed, root=workspace_root),
        lint_check(changed, scope="changed", root=workspace_root),
        type_check(changed, scope="changed", root=workspace_root),
        config_validate(changed, root=workspace_root),
        policy_check(changed, root=workspace_root),
    )


def project_problems(mode: str = "standard", *, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    mode_value = str(mode or "standard").lower()
    all_files = iter_project_files(workspace_root)
    fast = changed_files_check(root=workspace_root)
    if mode_value == "fast":
        return fast

    standard = merge_results(
        fast,
        syntax_check(all_files, root=workspace_root),
        config_validate(all_files, root=workspace_root),
        type_check(all_files, scope="project", root=workspace_root),
        build_check(root=workspace_root),
        test_check(mode="related", root=workspace_root),
        dependency_check(all_files, root=workspace_root),
    )
    if mode_value == "standard":
        return standard

    return merge_results(
        standard,
        security_check(all_files, root=workspace_root),
        dead_code_check(all_files, scope="project", root=workspace_root),
        duplication_check(all_files, threshold=30, root=workspace_root),
        test_check(mode="deep", root=workspace_root),
    )


def _candidate_project_dirs(root: Path, targets: Optional[list[str]]) -> list[Path]:
    if not targets:
        candidates = [root]
        if (root / "vscode-extension").exists():
            candidates.append(root / "vscode-extension")
        return candidates
    directories: list[Path] = []
    for target in normalize_input_paths(root, targets):
        candidate = (root / target).resolve()
        if candidate.is_file():
            candidate = candidate.parent
        if candidate.exists() and candidate not in directories:
            directories.append(candidate)
    return directories or [root]


def _read_package_scripts(path: Path) -> dict[str, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    scripts = payload.get("scripts")
    if not isinstance(scripts, dict):
        return {}
    return {str(key): str(value) for key, value in scripts.items()}


def _python_executable(root: Path) -> Path:
    venv_python = root / ".venv" / "bin" / "python"
    return venv_python if venv_python.exists() else Path(__import__("sys").executable)


def _select_pytest_targets(root: Path, targets: Optional[list[str]], mode: str) -> list[str]:
    if targets:
        normalized = normalize_input_paths(root, targets)
        pytest_targets = [path for path in normalized if path.endswith(".py") or path.startswith("tests/")]
        if pytest_targets:
            return pytest_targets
    if (root / "tests").exists():
        return ["tests"]
    return []


def _extension_test_diagnostics(root: Path, raw_sources: list[str]) -> list[Diagnostic]:
    extension_dir = root / "vscode-extension"
    package_json = extension_dir / "package.json"
    npm = find_command("npm", root=root, start_dir=extension_dir)
    if not package_json.exists() or npm is None:
        raw_sources.append("test_check: skipped extension tests because npm or vscode-extension/package.json is unavailable")
        return []
    scripts = _read_package_scripts(package_json)
    command = None
    if "test:integration" in scripts:
        command = [npm, "run", "test:integration"]
    elif "test" in scripts:
        command = [npm, "run", "test"]
    if command is None:
        return []
    result = run_command(command, cwd=extension_dir, timeout=180)
    raw_sources.append(f"extension tests returncode={result.returncode}")
    if result.returncode == 0:
        return []
    diagnostics: list[Diagnostic] = []
    combined = combine_command_output(result.stdout, result.stderr)
    tap_match = _TAP_FAILURE_RE.search(combined)
    diagnostics.append(
        make_diagnostic(
            tool="npm",
            category="test",
            severity="error",
            file="vscode-extension",
            code="INTEGRATION_TEST_FAILED",
            message=(
                tap_match.group("message").strip()
                if tap_match
                else command_failure_message(
                    result.stdout,
                    result.stderr,
                    fallback=f"Extension tests failed with exit code {result.returncode}.",
                )
            ),
            suggestion="Inspect the failing extension or integration test output.",
            confidence=0.85,
        )
    )
    return diagnostics


def _pytest_failure_message(combined: str, fallback: str) -> str:
    lines = combined.splitlines()
    assertion_line = next(
        (
            line.strip()
            for line in lines
            if "assert " in line and (line.lstrip().startswith("E") or line.lstrip().startswith(">"))
        ),
        None,
    )
    if assertion_line:
        return assertion_line
    return fallback