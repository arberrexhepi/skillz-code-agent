from __future__ import annotations

from typing import Any, NotRequired, TypedDict


class DiscoveryHit(TypedDict, total=False):
    file_path: str
    start_line: int | None
    end_line: int | None
    symbol_name: str | None
    symbol_kind: str | None
    match_type: str
    preview: str | None
    score: float | None
    is_definition: bool | None
    is_reference: bool | None
    is_canonical_candidate: bool | None
    details: dict[str, Any]


class DiscoveryResult(TypedDict, total=False):
    ok: bool
    query: str
    result_type: str
    hits: list[DiscoveryHit]
    summary: dict[str, Any]
    path: str
    details: dict[str, Any]


class InvestigationResult(TypedDict, total=False):
    ok: bool
    topic: str
    path: str
    mode: str
    canonical_candidates: list[str]
    likely_edit_targets: list[str]
    related_tests: list[str]
    related_configs: list[str]
    relevant_symbols: list[dict[str, Any]]
    dependency_edges: list[dict[str, Any]]
    recommended_read_order: list[str]
    notes: list[str]
    summary: dict[str, Any]
    details: dict[str, Any]


class SymbolRecord(TypedDict, total=False):
    name: str
    kind: str
    line: int
    end_line: int | None
    signature: str
    language: str
    parent: str
    qualified_name: str
    exported: bool
    details: dict[str, Any]


class FileOutline(TypedDict, total=False):
    ok: bool
    file_path: str
    language: str
    line_count: int
    imports: list[dict[str, Any]]
    exports: list[dict[str, Any]]
    symbols: list[SymbolRecord]
    constants: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    summary: dict[str, Any]


class DependencyTrace(TypedDict, total=False):
    ok: bool
    file_path: str
    direction: str
    depth: int
    imports: list[dict[str, Any]]
    imported_by: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    summary: dict[str, Any]


class FileOutline(TypedDict, total=False):
    ok: bool
    file_path: str
    language: str
    line_count: int
    imports: list[dict[str, Any]]
    exports: list[dict[str, Any]]
    symbols: list[SymbolRecord]
    constants: list[dict[str, Any]]
    sections: list[dict[str, Any]]
    summary: dict[str, Any]


class DependencyTrace(TypedDict, total=False):
    ok: bool
    file_path: str
    direction: str
    depth: int
    imports: list[dict[str, Any]]
    imported_by: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    summary: dict[str, Any]
