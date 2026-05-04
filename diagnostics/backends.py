from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from .common import find_command, normalize_repo_relative_path, run_command, strip_ansi


_TS_RE = re.compile(
    r"^(?P<path>.+?)\((?P<line>\d+),(?P<column>\d+)\):\s+error\s+(?P<code>TS\d+):\s+(?P<message>.+)$",
    flags=re.MULTILINE,
)
_PY_RE = re.compile(r'File "(?P<path>.+?)", line (?P<line>\d+)')
_TS_SUFFIXES = {".ts", ".tsx", ".js", ".jsx"}


@dataclass
class BackendDiagnosticRun:
    engine: str
    path: str
    scope: str
    command: List[str]
    returncode: int
    stdout: str
    stderr: str
    diagnostics: List[Dict[str, Any]]


def run_backend_diagnostics(
    root: Path,
    *,
    path: str,
    limit: int = 8,
    timeout: int = 30,
) -> BackendDiagnosticRun:
    normalized_path = normalize_repo_relative_path(root, path)
    if not normalized_path:
        raise ValueError("diagnose requires a target path")

    abs_path = (root / normalized_path).resolve()
    try:
        abs_path.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {path}") from exc

    if not abs_path.exists() or not abs_path.is_file():
        raise ValueError(f"file not found: {normalized_path}")

    suffix = abs_path.suffix.lower()
    if suffix in _TS_SUFFIXES:
        return _run_typescript_diagnostics(root, abs_path, normalized_path, limit=limit, timeout=timeout)
    if suffix == ".py":
        return _run_python_diagnostics(root, abs_path, normalized_path, timeout=timeout)
    raise ValueError(f"no backend diagnostics available for {normalized_path}")


def _run_typescript_diagnostics(
    root: Path,
    abs_path: Path,
    normalized_path: str,
    *,
    limit: int,
    timeout: int,
) -> BackendDiagnosticRun:
    tsconfig = _find_nearest_tsconfig(root, abs_path)
    tsc_path = find_command("tsc", root=root, start_dir=tsconfig.parent if tsconfig is not None else abs_path.parent)
    if not tsc_path:
        raise ValueError("tsc is not available on PATH or in node_modules/.bin")

    scope = "file"
    if tsconfig is not None:
        with tempfile.TemporaryDirectory(prefix="python-agent-diagnostics-") as tmpdir:
            temp_root = Path(tmpdir)
            temp_tsconfig = temp_root / "tsconfig.python-agent.json"
            payload = {
                "extends": os.path.relpath(tsconfig, temp_root).replace(os.sep, "/"),
                "files": [os.path.relpath(abs_path, temp_root).replace(os.sep, "/")],
            }
            temp_tsconfig.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            command = [tsc_path, "--noEmit", "--pretty", "false", "-p", str(temp_tsconfig)]
            result = _run_checked(command, cwd=root, timeout=timeout)
    else:
        command = [tsc_path, "--noEmit", "--pretty", "false", normalized_path]
        result = _run_checked(command, cwd=root, timeout=timeout)

    combined = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
    diagnostics = _parse_typescript_diagnostics(combined, root=root, target_path=normalized_path, limit=limit)
    return BackendDiagnosticRun(
        engine="tsc",
        path=normalized_path,
        scope=scope,
        command=command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        diagnostics=diagnostics,
    )


def _run_python_diagnostics(root: Path, abs_path: Path, normalized_path: str, *, timeout: int) -> BackendDiagnosticRun:
    command = [sys.executable, "-m", "py_compile", str(abs_path)]
    result = _run_checked(command, cwd=root, timeout=timeout)
    combined = "\n".join(part for part in [result.stdout, result.stderr] if part).strip()
    diagnostics = _parse_python_diagnostics(combined, root=root, target_path=normalized_path)
    return BackendDiagnosticRun(
        engine="py_compile",
        path=normalized_path,
        scope="file",
        command=command,
        returncode=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        diagnostics=diagnostics,
    )


def _parse_typescript_diagnostics(
    combined_output: str,
    *,
    root: Path,
    target_path: str,
    limit: int,
) -> List[Dict[str, Any]]:
    diagnostics: List[Dict[str, Any]] = []
    normalized_target = normalize_repo_relative_path(root, target_path)
    for match in _TS_RE.finditer(strip_ansi(combined_output or "")):
        diag_path = normalize_repo_relative_path(root, match.group("path"))
        if diag_path != normalized_target:
            continue
        line = int(match.group("line"))
        column = int(match.group("column"))
        diagnostics.append(
            {
                "path": diag_path,
                "resource": diag_path,
                "owner": "typescript",
                "source": "tsc",
                "line": line,
                "column": column,
                "startLineNumber": line,
                "startColumn": column,
                "endLineNumber": line,
                "endColumn": column,
                "severity": 8,
                "code": match.group("code"),
                "message": match.group("message").strip(),
            }
        )
        if len(diagnostics) >= limit:
            break
    return diagnostics


def _parse_python_diagnostics(combined_output: str, *, root: Path, target_path: str) -> List[Dict[str, Any]]:
    match = _PY_RE.search(strip_ansi(combined_output or ""))
    if match is None:
        return []
    diag_path = normalize_repo_relative_path(root, match.group("path"))
    normalized_target = normalize_repo_relative_path(root, target_path)
    if diag_path != normalized_target:
        return []
    lines = strip_ansi(combined_output).strip().splitlines()
    message = lines[-1].strip() if lines else "Python compilation failed"
    line_no = int(match.group("line"))
    return [
        {
            "path": diag_path,
            "resource": diag_path,
            "owner": "python",
            "source": "py_compile",
            "line": line_no,
            "column": 1,
            "startLineNumber": line_no,
            "startColumn": 1,
            "endLineNumber": line_no,
            "endColumn": 1,
            "severity": 8,
            "code": "PY_COMPILE",
            "message": message,
        }
    ]


def _find_nearest_tsconfig(root: Path, abs_path: Path) -> Optional[Path]:
    root_resolved = root.resolve()
    current = abs_path.parent.resolve()
    while True:
        candidate = current / "tsconfig.json"
        if candidate.exists() and candidate.is_file():
            return candidate
        if current == root_resolved or root_resolved not in current.parents:
            break
        current = current.parent
    return None


def _run_checked(command: list[str], *, cwd: Path, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return run_command(command, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"diagnostics timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise ValueError(str(exc)) from exc