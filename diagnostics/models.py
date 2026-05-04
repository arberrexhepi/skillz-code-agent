from __future__ import annotations

from typing import Dict, Iterable, Literal, Optional, TypedDict, cast


Severity = Literal["error", "warning", "info"]
Category = Literal[
    "syntax",
    "type",
    "lint",
    "format",
    "config",
    "schema",
    "dependency",
    "build",
    "test",
    "runtime",
    "security",
    "dead_code",
    "duplication",
    "policy",
]


class Diagnostic(TypedDict):
    tool: str
    category: Category
    severity: Severity
    file: str
    line: Optional[int]
    column: Optional[int]
    end_line: Optional[int]
    end_column: Optional[int]
    code: Optional[str]
    message: str
    suggestion: Optional[str]
    symbol: Optional[str]
    related_files: list[str]
    confidence: Optional[float]


class DiagnosticsResult(TypedDict):
    ok: bool
    summary: Dict[str, int]
    diagnostics: list[Diagnostic]
    raw_sources: list[str]


def make_diagnostic(
    *,
    tool: str,
    category: Category,
    severity: Severity,
    file: str,
    message: str,
    line: Optional[int] = None,
    column: Optional[int] = None,
    end_line: Optional[int] = None,
    end_column: Optional[int] = None,
    code: Optional[str] = None,
    suggestion: Optional[str] = None,
    symbol: Optional[str] = None,
    related_files: Optional[list[str]] = None,
    confidence: Optional[float] = None,
) -> Diagnostic:
    return {
        "tool": tool,
        "category": category,
        "severity": severity,
        "file": file,
        "line": line,
        "column": column,
        "end_line": end_line,
        "end_column": end_column,
        "code": code,
        "message": message,
        "suggestion": suggestion,
        "symbol": symbol,
        "related_files": list(related_files or []),
        "confidence": confidence,
    }


def summarize_diagnostics(diagnostics: Iterable[Diagnostic]) -> Dict[str, int]:
    summary: Dict[str, int] = {"total": 0, "error": 0, "warning": 0, "info": 0}
    for diagnostic in diagnostics:
        summary["total"] += 1
        severity = diagnostic["severity"]
        summary[severity] = summary.get(severity, 0) + 1
        category_key = f"category:{diagnostic['category']}"
        summary[category_key] = summary.get(category_key, 0) + 1
    return summary


def dedupe_diagnostics(diagnostics: Iterable[Diagnostic]) -> list[Diagnostic]:
    seen: set[tuple[object, ...]] = set()
    unique: list[Diagnostic] = []
    for diagnostic in diagnostics:
        key = (
            diagnostic["tool"],
            diagnostic["category"],
            diagnostic["severity"],
            diagnostic["file"],
            diagnostic["line"],
            diagnostic["column"],
            diagnostic["end_line"],
            diagnostic["end_column"],
            diagnostic["code"],
            diagnostic["message"],
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(diagnostic)
    return unique


def make_result(
    diagnostics: Iterable[Diagnostic],
    *,
    raw_sources: Optional[Iterable[str]] = None,
) -> DiagnosticsResult:
    normalized = dedupe_diagnostics(diagnostics)
    return {
        "ok": not any(item["severity"] == "error" for item in normalized),
        "summary": summarize_diagnostics(normalized),
        "diagnostics": normalized,
        "raw_sources": [str(item) for item in (raw_sources or [])],
    }


def merge_results(*results: DiagnosticsResult) -> DiagnosticsResult:
    diagnostics: list[Diagnostic] = []
    raw_sources: list[str] = []
    for result in results:
        diagnostics.extend(cast(list[Diagnostic], result.get("diagnostics", [])))
        raw_sources.extend(str(item) for item in result.get("raw_sources", []))
    return make_result(diagnostics, raw_sources=raw_sources)