from __future__ import annotations

import ast
import json
import re
import tomllib
from pathlib import Path
from typing import Any, Optional

from .backends import run_backend_diagnostics
from .common import (
    JS_TS_SUFFIXES,
    PYTHON_SUFFIXES,
    command_failure_message,
    combine_command_output,
    detect_changed_files,
    find_command,
    iter_project_files,
    normalize_input_paths,
    normalize_repo_relative_path,
    read_text_file,
    resolve_root,
    run_command,
    safe_tail,
    try_import_yaml,
)
from .models import Diagnostic, DiagnosticsResult, make_diagnostic, make_result


_MYPY_RE = re.compile(
    r"^(?P<path>[^:\n]+):(?P<line>\d+)(?::(?P<column>\d+))?:\s+(?P<severity>error|note|warning):\s+(?P<message>.+?)(?:\s+\[(?P<code>[^\]]+)\])?$",
    flags=re.MULTILINE,
)
_PYRIGHT_RE = re.compile(
    r"^(?P<path>[^:\n]+):(?P<line>\d+):(?P<column>\d+)\s+-\s+(?P<severity>error|warning|information):\s+(?P<message>.+?)(?:\s+\((?P<code>[^)]+)\))?$",
    flags=re.MULTILINE,
)
_NODE_CHECK_RE = re.compile(r"^(?P<path>.+?):(?P<line>\d+)\n", flags=re.MULTILINE)
_ESLINT_RE = re.compile(r"^(?P<path>.+?): line (?P<line>\d+), col (?P<column>\d+), (?P<message>.+)$")
_SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS_ACCESS_KEY"),
    (re.compile(r"-----BEGIN (?:RSA|DSA|EC|OPENSSH|PGP) PRIVATE KEY-----"), "PRIVATE_KEY"),
    (re.compile(r"(?i)(api[_-]?key|secret|token)\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}['\"]"), "SECRET_LITERAL"),
]
_DANGEROUS_PATTERNS = [
    (re.compile(r"subprocess\.(?:run|Popen)\([^\n]*shell\s*=\s*True"), "SHELL_TRUE"),
    (re.compile(r"\beval\s*\("), "EVAL_CALL"),
    (re.compile(r"\bexec\s*\("), "EXEC_CALL"),
]


def syntax_check(paths: list[str], *, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    diagnostics: list[Diagnostic] = []
    raw_sources: list[str] = []
    yaml_module = try_import_yaml()
    for rel_path in normalize_input_paths(workspace_root, paths):
        path = workspace_root / rel_path
        if not path.exists() or not path.is_file():
            diagnostics.append(
                make_diagnostic(
                    tool="syntax_check",
                    category="syntax",
                    severity="error",
                    file=rel_path,
                    code="FILE_NOT_FOUND",
                    message="File does not exist.",
                    suggestion="Pass an existing file path to syntax_check.",
                )
            )
            continue
        suffix = path.suffix.lower()
        try:
            if suffix in PYTHON_SUFFIXES:
                compile(read_text_file(path), rel_path, "exec")
            elif suffix == ".json":
                json.loads(read_text_file(path))
            elif suffix == ".toml":
                tomllib.loads(read_text_file(path))
            elif suffix in {".yaml", ".yml"}:
                if yaml_module is None:
                    raw_sources.append(f"syntax_check: skipped YAML parse for {rel_path} because PyYAML is unavailable")
                    continue
                yaml_module.safe_load(read_text_file(path))
            elif suffix == ".md":
                _parse_markdown_frontmatter(path, yaml_module=yaml_module)
            elif suffix in {".js", ".mjs", ".cjs"}:
                diagnostics.extend(_node_syntax_diagnostics(workspace_root, rel_path))
            elif suffix in {".ts", ".tsx"}:
                diagnostics.extend(_typescript_syntax_diagnostics(workspace_root, rel_path))
        except SyntaxError as exc:
            diagnostics.append(
                make_diagnostic(
                    tool="syntax_check",
                    category="syntax",
                    severity="error",
                    file=rel_path,
                    line=exc.lineno,
                    column=exc.offset,
                    end_line=exc.lineno,
                    end_column=exc.offset,
                    code="SYNTAX_ERROR",
                    message=exc.msg,
                    suggestion="Fix the syntax before running higher-level diagnostics.",
                    confidence=0.99,
                )
            )
        except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            diagnostics.append(
                make_diagnostic(
                    tool="syntax_check",
                    category="syntax",
                    severity="error",
                    file=rel_path,
                    line=getattr(exc, "lineno", None),
                    column=getattr(exc, "colno", None),
                    end_line=getattr(exc, "lineno", None),
                    end_column=getattr(exc, "colno", None),
                    code="PARSE_ERROR",
                    message=str(exc),
                    suggestion="Fix the malformed structured data.",
                    confidence=0.99,
                )
            )
        except ValueError as exc:
            diagnostics.append(
                make_diagnostic(
                    tool="syntax_check",
                    category="syntax",
                    severity="error",
                    file=rel_path,
                    code="FRONTMATTER_PARSE_ERROR",
                    message=str(exc),
                    suggestion="Fix the Markdown frontmatter block.",
                    confidence=0.95,
                )
            )
    return make_result(diagnostics, raw_sources=raw_sources)


def type_check(paths: list[str], scope: str = "changed", *, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    diagnostics: list[Diagnostic] = []
    raw_sources: list[str] = [f"type_check scope={scope}"]
    normalized = normalize_input_paths(workspace_root, paths)
    ts_paths = [path for path in normalized if Path(path).suffix.lower() in JS_TS_SUFFIXES]
    py_paths = [path for path in normalized if Path(path).suffix.lower() in PYTHON_SUFFIXES]

    for rel_path in ts_paths:
        try:
            run = run_backend_diagnostics(workspace_root, path=rel_path, limit=20, timeout=30)
        except ValueError as exc:
            raw_sources.append(f"type_check skipped {rel_path}: {exc}")
            continue
        for item in run.diagnostics:
            diagnostics.append(
                make_diagnostic(
                    tool=run.engine,
                    category="type",
                    severity="error",
                    file=str(item.get("path") or rel_path),
                    line=_coerce_int(item.get("line")),
                    column=_coerce_int(item.get("column")),
                    end_line=_coerce_int(item.get("endLineNumber")),
                    end_column=_coerce_int(item.get("endColumn")),
                    code=str(item.get("code") or "TYPE_ERROR"),
                    message=str(item.get("message") or "Type diagnostics failed."),
                    suggestion="Update types or imports to satisfy the compiler.",
                    confidence=0.95,
                )
            )

    diagnostics.extend(_python_type_diagnostics(workspace_root, py_paths, raw_sources))
    return make_result(diagnostics, raw_sources=raw_sources)


def lint_check(paths: list[str], scope: str = "changed", *, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    diagnostics: list[Diagnostic] = []
    raw_sources: list[str] = [f"lint_check scope={scope}"]
    normalized = normalize_input_paths(workspace_root, paths)
    py_paths = [path for path in normalized if Path(path).suffix.lower() in PYTHON_SUFFIXES]
    js_paths = [path for path in normalized if Path(path).suffix.lower() in JS_TS_SUFFIXES]

    if py_paths:
        ruff = find_command("ruff", root=workspace_root)
        if ruff:
            result = run_command([ruff, "check", "--output-format", "json", *py_paths], cwd=workspace_root, timeout=30)
            raw_sources.append(f"ruff check returncode={result.returncode}")
            try:
                payload = json.loads(result.stdout or "[]")
            except json.JSONDecodeError:
                payload = []
            for item in payload:
                diagnostics.append(
                    make_diagnostic(
                        tool="ruff",
                        category="lint",
                        severity="warning" if str(item.get("code", "")).startswith("I") else "error",
                        file=normalize_repo_relative_path(workspace_root, str(item.get("filename") or "")),
                        line=_coerce_int(item.get("location", {}).get("row")),
                        column=_coerce_int(item.get("location", {}).get("column")),
                        end_line=_coerce_int(item.get("end_location", {}).get("row")),
                        end_column=_coerce_int(item.get("end_location", {}).get("column")),
                        code=str(item.get("code") or "RUFF"),
                        message=str(item.get("message") or "Lint issue."),
                        suggestion=_ruff_fix_suggestion(item),
                        confidence=0.9,
                    )
                )
        else:
            raw_sources.append("lint_check: skipped Python linting because ruff is unavailable")

    if js_paths:
        eslint = find_command("eslint", root=workspace_root)
        if eslint:
            result = run_command([eslint, "-f", "json", *js_paths], cwd=workspace_root, timeout=30)
            raw_sources.append(f"eslint returncode={result.returncode}")
            try:
                payload = json.loads(result.stdout or "[]")
            except json.JSONDecodeError:
                payload = []
            for file_payload in payload:
                file_name = normalize_repo_relative_path(workspace_root, str(file_payload.get("filePath") or ""))
                for message in file_payload.get("messages", []):
                    severity = "error" if int(message.get("severity", 2)) >= 2 else "warning"
                    diagnostics.append(
                        make_diagnostic(
                            tool="eslint",
                            category="lint",
                            severity=severity,
                            file=file_name,
                            line=_coerce_int(message.get("line")),
                            column=_coerce_int(message.get("column")),
                            end_line=_coerce_int(message.get("endLine")),
                            end_column=_coerce_int(message.get("endColumn")),
                            code=str(message.get("ruleId") or "ESLINT"),
                            message=str(message.get("message") or "Lint issue."),
                            confidence=0.9,
                        )
                    )
        else:
            raw_sources.append("lint_check: skipped JS/TS linting because eslint is unavailable")

    return make_result(diagnostics, raw_sources=raw_sources)


def format_check(paths: list[str], *, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    diagnostics: list[Diagnostic] = []
    raw_sources: list[str] = []
    normalized = normalize_input_paths(workspace_root, paths)
    py_paths = [path for path in normalized if Path(path).suffix.lower() in PYTHON_SUFFIXES]
    other_paths = [path for path in normalized if Path(path).suffix.lower() not in PYTHON_SUFFIXES]

    if py_paths:
        ruff = find_command("ruff", root=workspace_root)
        if ruff:
            result = run_command([ruff, "format", "--check", *py_paths], cwd=workspace_root, timeout=30)
            raw_sources.append(f"ruff format --check returncode={result.returncode}")
            if result.returncode != 0:
                for rel_path in py_paths:
                    diagnostics.append(
                        make_diagnostic(
                            tool="ruff-format",
                            category="format",
                            severity="warning",
                            file=rel_path,
                            code="FORMAT_CHECK_FAILED",
                            message="File is not formatted according to ruff format.",
                            suggestion="Run `ruff format` on the file.",
                            confidence=0.8,
                        )
                    )
        else:
            raw_sources.append("format_check: skipped Python formatter check because ruff is unavailable")

    if other_paths:
        prettier = find_command("prettier", root=workspace_root)
        if prettier:
            result = run_command([prettier, "--check", *other_paths], cwd=workspace_root, timeout=30)
            raw_sources.append(f"prettier --check returncode={result.returncode}")
            if result.returncode != 0:
                for rel_path in other_paths:
                    diagnostics.append(
                        make_diagnostic(
                            tool="prettier",
                            category="format",
                            severity="warning",
                            file=rel_path,
                            code="FORMAT_CHECK_FAILED",
                            message="File is not formatted according to Prettier.",
                            suggestion="Run `prettier --write` on the file.",
                            confidence=0.8,
                        )
                    )
        else:
            raw_sources.append("format_check: skipped non-Python formatter check because prettier is unavailable")

    return make_result(diagnostics, raw_sources=raw_sources)


def config_validate(paths: list[str], *, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    diagnostics: list[Diagnostic] = []
    raw_sources: list[str] = []
    yaml_module = try_import_yaml()
    for rel_path in normalize_input_paths(workspace_root, paths):
        path = workspace_root / rel_path
        if not path.exists() or not path.is_file():
            continue
        suffix = path.suffix.lower()
        try:
            if path.name == "package.json":
                payload = json.loads(read_text_file(path))
                if "scripts" in payload and not isinstance(payload["scripts"], dict):
                    diagnostics.append(_config_error(rel_path, "package.json `scripts` must be an object."))
                if "dependencies" in payload and not isinstance(payload["dependencies"], dict):
                    diagnostics.append(_config_error(rel_path, "package.json `dependencies` must be an object."))
            elif path.name.startswith("tsconfig") and suffix == ".json":
                payload = json.loads(read_text_file(path))
                if "compilerOptions" in payload and not isinstance(payload["compilerOptions"], dict):
                    diagnostics.append(_config_error(rel_path, "tsconfig compilerOptions must be an object."))
                if "extends" in payload and not isinstance(payload["extends"], str):
                    diagnostics.append(_config_error(rel_path, "tsconfig extends must be a string."))
            elif suffix == ".json":
                json.loads(read_text_file(path))
            elif suffix == ".toml":
                tomllib.loads(read_text_file(path))
            elif suffix in {".yaml", ".yml"}:
                if yaml_module is None:
                    raw_sources.append(f"config_validate: skipped YAML validation for {rel_path} because PyYAML is unavailable")
                    continue
                yaml_module.safe_load(read_text_file(path))
            elif path.name.startswith(".env") or suffix == ".env":
                for index, raw_line in enumerate(read_text_file(path).splitlines(), start=1):
                    stripped = raw_line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    if "=" not in raw_line:
                        diagnostics.append(
                            make_diagnostic(
                                tool="config_validate",
                                category="config",
                                severity="error",
                                file=rel_path,
                                line=index,
                                column=1,
                                end_line=index,
                                end_column=len(raw_line),
                                code="ENV_PARSE_ERROR",
                                message="Environment variable lines must contain '='.",
                                suggestion="Use KEY=value syntax.",
                                confidence=0.95,
                            )
                        )
        except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            diagnostics.append(
                make_diagnostic(
                    tool="config_validate",
                    category="config",
                    severity="error",
                    file=rel_path,
                    line=getattr(exc, "lineno", None),
                    column=getattr(exc, "colno", None),
                    end_line=getattr(exc, "lineno", None),
                    end_column=getattr(exc, "colno", None),
                    code="CONFIG_PARSE_ERROR",
                    message=str(exc),
                    suggestion="Fix the malformed configuration file.",
                    confidence=0.98,
                )
            )
    return make_result(diagnostics, raw_sources=raw_sources)


def schema_validate(
    files: list[str],
    schema_refs: Optional[list[str]] = None,
    *,
    root: Optional[Path | str] = None,
) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    diagnostics: list[Diagnostic] = []
    raw_sources: list[str] = []
    schema_module = _try_import_jsonschema()
    normalized = normalize_input_paths(workspace_root, files)
    refs = normalize_input_paths(workspace_root, schema_refs or [])
    if refs and schema_module is None:
        raw_sources.append("schema_validate: skipped JSON Schema validation because jsonschema is unavailable")
    for rel_path in normalized:
        path = workspace_root / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            payload = _load_structured_payload(path)
        except Exception as exc:
            diagnostics.append(
                make_diagnostic(
                    tool="schema_validate",
                    category="schema",
                    severity="error",
                    file=rel_path,
                    code="SCHEMA_INPUT_INVALID",
                    message=str(exc),
                    suggestion="Fix the file before schema validation.",
                )
            )
            continue
        if refs and schema_module is not None:
            for schema_ref in refs:
                schema_path = workspace_root / schema_ref
                schema = _load_structured_payload(schema_path)
                try:
                    schema_module.validate(payload, schema)
                except Exception as exc:
                    diagnostics.append(
                        make_diagnostic(
                            tool="jsonschema",
                            category="schema",
                            severity="error",
                            file=rel_path,
                            code="SCHEMA_VALIDATION_FAILED",
                            message=str(exc),
                            suggestion=f"Update {rel_path} to satisfy {schema_ref}.",
                            related_files=[schema_ref],
                            confidence=0.95,
                        )
                    )
        elif isinstance(payload, dict) and "openapi" in payload:
            if not isinstance(payload.get("paths"), dict):
                diagnostics.append(
                    make_diagnostic(
                        tool="schema_validate",
                        category="schema",
                        severity="error",
                        file=rel_path,
                        code="OPENAPI_PATHS_MISSING",
                        message="OpenAPI documents must include a `paths` object.",
                        suggestion="Add a `paths` object to the OpenAPI document.",
                        confidence=0.9,
                    )
                )
    return make_result(diagnostics, raw_sources=raw_sources)


def dependency_check(paths: list[str], *, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    diagnostics: list[Diagnostic] = []
    for rel_path in normalize_input_paths(workspace_root, paths):
        path = workspace_root / rel_path
        if not path.exists() or not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix in PYTHON_SUFFIXES:
            diagnostics.extend(_python_dependency_diagnostics(workspace_root, rel_path))
        elif suffix in JS_TS_SUFFIXES:
            diagnostics.extend(_javascript_dependency_diagnostics(workspace_root, rel_path))
    return make_result(diagnostics)


def security_check(paths: Optional[list[str]] = None, *, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    target_paths = normalize_input_paths(workspace_root, paths or detect_changed_files(workspace_root))
    if not target_paths:
        target_paths = iter_project_files(workspace_root)
    diagnostics: list[Diagnostic] = []
    for rel_path in target_paths:
        path = workspace_root / rel_path
        if not path.exists() or not path.is_file():
            continue
        try:
            content = read_text_file(path)
        except Exception:
            continue
        for pattern, code in _SECRET_PATTERNS:
            match = pattern.search(content)
            if match is None:
                continue
            line, column = _offset_to_line_column(content, match.start())
            diagnostics.append(
                make_diagnostic(
                    tool="security_check",
                    category="security",
                    severity="error",
                    file=rel_path,
                    line=line,
                    column=column,
                    end_line=line,
                    end_column=column + len(match.group(0)),
                    code=code,
                    message="Potential secret material detected in source-controlled content.",
                    suggestion="Move secrets to environment variables or a secret manager.",
                    confidence=0.92,
                )
            )
        for pattern, code in _DANGEROUS_PATTERNS:
            match = pattern.search(content)
            if match is None:
                continue
            line, column = _offset_to_line_column(content, match.start())
            diagnostics.append(
                make_diagnostic(
                    tool="security_check",
                    category="security",
                    severity="warning",
                    file=rel_path,
                    line=line,
                    column=column,
                    end_line=line,
                    end_column=column + len(match.group(0)),
                    code=code,
                    message="Potentially dangerous dynamic execution pattern detected.",
                    suggestion="Prefer safer structured APIs or explicit allowlists.",
                    confidence=0.8,
                )
            )
    return make_result(diagnostics)


def policy_check(paths: list[str], *, root: Optional[Path | str] = None) -> DiagnosticsResult:
    workspace_root = resolve_root(root)
    diagnostics: list[Diagnostic] = []
    for rel_path in normalize_input_paths(workspace_root, paths):
        path = workspace_root / rel_path
        if not path.exists() or not path.is_file():
            continue
        line_count = _count_lines(path)
        if line_count > 1200:
            diagnostics.append(
                make_diagnostic(
                    tool="policy_check",
                    category="policy",
                    severity="warning",
                    file=rel_path,
                    code="FILE_TOO_LARGE",
                    message=f"File has {line_count} lines; prefer smaller focused modules.",
                    suggestion="Split the file into importable modules.",
                    confidence=0.85,
                )
            )
        if any(part in {"dist", "build", "__pycache__"} for part in Path(rel_path).parts):
            diagnostics.append(
                make_diagnostic(
                    tool="policy_check",
                    category="policy",
                    severity="warning",
                    file=rel_path,
                    code="GENERATED_PATH_EDIT",
                    message="File is inside a generated or build output directory.",
                    suggestion="Prefer editing source files instead of generated output.",
                    confidence=0.9,
                )
            )
        if path.name == ".env":
            diagnostics.append(
                make_diagnostic(
                    tool="policy_check",
                    category="policy",
                    severity="warning",
                    file=rel_path,
                    code="ENV_FILE_TRACKED",
                    message="Tracked .env files often contain environment-specific values.",
                    suggestion="Consider keeping secrets outside version control.",
                    confidence=0.75,
                )
            )
    return make_result(diagnostics)


def _node_syntax_diagnostics(root: Path, rel_path: str) -> list[Diagnostic]:
    node = find_command("node", root=root)
    if not node:
        return []
    result = run_command([node, "--check", rel_path], cwd=root, timeout=20)
    if result.returncode == 0:
        return []
    combined = combine_command_output(result.stdout, result.stderr)
    match = _NODE_CHECK_RE.search(combined)
    line = int(match.group("line")) if match else None
    return [
        make_diagnostic(
            tool="node",
            category="syntax",
            severity="error",
            file=rel_path,
            line=line,
            column=1,
            end_line=line,
            end_column=1,
            code="NODE_SYNTAX",
            message=command_failure_message(
                result.stdout,
                result.stderr,
                fallback=f"Node syntax check failed for {rel_path} with exit code {result.returncode}.",
                max_lines=4,
            ),
            suggestion="Fix the JavaScript syntax error.",
            confidence=0.95,
        )
    ]


def _typescript_syntax_diagnostics(root: Path, rel_path: str) -> list[Diagnostic]:
    try:
        run = run_backend_diagnostics(root, path=rel_path, limit=20, timeout=20)
    except ValueError:
        return []
    diagnostics: list[Diagnostic] = []
    for item in run.diagnostics:
        diagnostics.append(
            make_diagnostic(
                tool="tsc",
                category="syntax",
                severity="error",
                file=str(item.get("path") or rel_path),
                line=_coerce_int(item.get("line")),
                column=_coerce_int(item.get("column")),
                end_line=_coerce_int(item.get("endLineNumber")),
                end_column=_coerce_int(item.get("endColumn")),
                code=str(item.get("code") or "TSC"),
                message=str(item.get("message") or "TypeScript compilation issue."),
                suggestion="Fix the TypeScript parser or compiler error.",
                confidence=0.9,
            )
        )
    return diagnostics


def _parse_markdown_frontmatter(path: Path, *, yaml_module: Any | None) -> None:
    content = read_text_file(path)
    if not content.startswith("---\n"):
        return
    parts = content.split("\n---\n", 1)
    if len(parts) != 2:
        raise ValueError("Frontmatter block is not terminated with a closing '---'.")
    if yaml_module is None:
        raise ValueError("PyYAML is unavailable for Markdown frontmatter parsing.")
    yaml_module.safe_load(parts[0][4:])


def _python_type_diagnostics(root: Path, rel_paths: list[str], raw_sources: list[str]) -> list[Diagnostic]:
    if not rel_paths:
        return []
    diagnostics: list[Diagnostic] = []
    mypy = find_command("mypy", root=root)
    pyright = find_command("pyright", root=root) or find_command("basedpyright", root=root)
    if mypy:
        result = run_command([mypy, *rel_paths], cwd=root, timeout=45)
        raw_sources.append(f"mypy returncode={result.returncode}")
        for match in _MYPY_RE.finditer(result.stdout + "\n" + result.stderr):
            severity = "warning" if match.group("severity") == "warning" else "error"
            diagnostics.append(
                make_diagnostic(
                    tool="mypy",
                    category="type",
                    severity=severity,
                    file=normalize_repo_relative_path(root, match.group("path")),
                    line=_coerce_int(match.group("line")),
                    column=_coerce_int(match.group("column")),
                    end_line=_coerce_int(match.group("line")),
                    end_column=_coerce_int(match.group("column")),
                    code=match.group("code") or "MYPY",
                    message=match.group("message").strip(),
                    suggestion="Adjust types or imports so the checker can resolve the symbol.",
                    confidence=0.92,
                )
            )
        return diagnostics
    if pyright:
        result = run_command([pyright, "--outputjson", *rel_paths], cwd=root, timeout=45)
        raw_sources.append(f"pyright returncode={result.returncode}")
        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            payload = {}
        for item in payload.get("generalDiagnostics", []):
            diag_file = normalize_repo_relative_path(root, str(item.get("file") or ""))
            start = item.get("range", {}).get("start", {})
            end = item.get("range", {}).get("end", {})
            severity = "warning" if str(item.get("severity")) == "warning" else "error"
            diagnostics.append(
                make_diagnostic(
                    tool="pyright",
                    category="type",
                    severity=severity,
                    file=diag_file,
                    line=_coerce_int(start.get("line"), offset=1),
                    column=_coerce_int(start.get("character"), offset=1),
                    end_line=_coerce_int(end.get("line"), offset=1),
                    end_column=_coerce_int(end.get("character"), offset=1),
                    code=str(item.get("rule") or "PYRIGHT"),
                    message=str(item.get("message") or "Type checking issue."),
                    confidence=0.92,
                )
            )
        return diagnostics
    raw_sources.append("type_check: skipped Python type diagnostics because mypy/pyright are unavailable")
    return diagnostics


def _config_error(rel_path: str, message: str) -> Diagnostic:
    return make_diagnostic(
        tool="config_validate",
        category="config",
        severity="error",
        file=rel_path,
        code="CONFIG_SCHEMA_ERROR",
        message=message,
        suggestion="Adjust the config structure to the expected schema.",
        confidence=0.95,
    )


def _load_structured_payload(path: Path) -> Any:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return json.loads(read_text_file(path))
    if suffix == ".toml":
        return tomllib.loads(read_text_file(path))
    if suffix in {".yaml", ".yml"}:
        yaml_module = try_import_yaml()
        if yaml_module is None:
            raise ValueError("PyYAML is unavailable")
        return yaml_module.safe_load(read_text_file(path))
    raise ValueError(f"Unsupported schema input: {path.name}")


def _try_import_jsonschema() -> Any | None:
    try:
        import jsonschema  # type: ignore
    except Exception:
        return None
    return jsonschema


def _python_dependency_diagnostics(root: Path, rel_path: str) -> list[Diagnostic]:
    path = root / rel_path
    try:
        tree = ast.parse(read_text_file(path), filename=rel_path)
    except SyntaxError:
        return []
    diagnostics: list[Diagnostic] = []
    module_dir = path.parent
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            target = _resolve_python_relative_import(module_dir, node.module, node.level)
            if target is None:
                diagnostics.append(
                    make_diagnostic(
                        tool="dependency_check",
                        category="dependency",
                        severity="error",
                        file=rel_path,
                        line=node.lineno,
                        column=node.col_offset + 1,
                        end_line=node.lineno,
                        end_column=node.col_offset + 1,
                        code="MISSING_IMPORT",
                        message="Relative Python import does not resolve to a local module.",
                        suggestion="Fix the import path or add the missing module.",
                        confidence=0.9,
                    )
                )
    return diagnostics


def _javascript_dependency_diagnostics(root: Path, rel_path: str) -> list[Diagnostic]:
    path = root / rel_path
    content = read_text_file(path)
    diagnostics: list[Diagnostic] = []
    pattern = re.compile(r"(?:from|require\()\s*[(']?['\"](?P<spec>[^'\"]+)['\"]")
    for match in pattern.finditer(content):
        spec = match.group("spec")
        if not spec.startswith("."):
            continue
        if _resolve_javascript_import(path, spec) is not None:
            continue
        line, column = _offset_to_line_column(content, match.start("spec"))
        diagnostics.append(
            make_diagnostic(
                tool="dependency_check",
                category="dependency",
                severity="error",
                file=rel_path,
                line=line,
                column=column,
                end_line=line,
                end_column=column + len(spec),
                code="MISSING_IMPORT",
                message=f"Relative import `{spec}` does not resolve to a local module.",
                suggestion="Fix the relative import path or create the missing module.",
                confidence=0.9,
            )
        )
    return diagnostics


def _resolve_python_relative_import(module_dir: Path, module: Optional[str], level: int) -> Optional[Path]:
    base = module_dir
    for _ in range(max(0, level - 1)):
        base = base.parent
    module_path = base if not module else base / Path(*module.split("."))
    candidates = [module_path.with_suffix(".py"), module_path / "__init__.py"]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _resolve_javascript_import(path: Path, spec: str) -> Optional[Path]:
    base = (path.parent / spec).resolve()
    candidates = [
        base,
        base.with_suffix(".ts"),
        base.with_suffix(".tsx"),
        base.with_suffix(".js"),
        base.with_suffix(".jsx"),
        base / "index.ts",
        base / "index.tsx",
        base / "index.js",
        base / "index.jsx",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _coerce_int(value: Any, *, offset: int = 0) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value) + offset
    except Exception:
        return None


def _ruff_fix_suggestion(item: dict[str, Any]) -> Optional[str]:
    fix = item.get("fix")
    if isinstance(fix, dict) and fix.get("message"):
        return str(fix["message"])
    return "Apply the suggested Ruff fix or refactor the code."


def _offset_to_line_column(text: str, offset: int) -> tuple[int, int]:
    prefix = text[:offset]
    line = prefix.count("\n") + 1
    column = len(prefix.rsplit("\n", 1)[-1]) + 1
    return line, column


def _count_lines(path: Path) -> int:
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for _ in handle)