from __future__ import annotations

from typing import Any, Optional, TypedDict


class MutationPrecondition(TypedDict, total=False):
    file_exists: Optional[bool]
    expected_hash: Optional[str]
    anchor_present: Optional[bool]
    expected_occurrences: Optional[int]
    line_range_stable: Optional[bool]
    symbol_resolves: Optional[bool]


class MutationResult(TypedDict, total=False):
    ok: bool
    operation_id: str
    file_path: str
    mutation_type: str
    applied: bool
    reason: Optional[str]
    before_hash: Optional[str]
    after_hash: Optional[str]
    changed_line_count: int
    drift_detected: bool
    diagnostics: list[dict[str, Any]]
    preconditions: MutationPrecondition
    diff: str
    details: dict[str, Any]


class BatchMutationResult(TypedDict):
    ok: bool
    atomic: bool
    operations: list[MutationResult]
    applied_count: int
    failed_count: int
    rolled_back: bool


def make_mutation_result(
    *,
    operation_id: str,
    file_path: str,
    mutation_type: str,
    ok: bool,
    applied: bool,
    reason: Optional[str] = None,
    before_hash: Optional[str] = None,
    after_hash: Optional[str] = None,
    changed_line_count: int = 0,
    drift_detected: bool = False,
    diagnostics: Optional[list[dict[str, Any]]] = None,
    preconditions: Optional[MutationPrecondition] = None,
    diff: str = "",
    details: Optional[dict[str, Any]] = None,
) -> MutationResult:
    return {
        "ok": ok,
        "operation_id": operation_id,
        "file_path": file_path,
        "mutation_type": mutation_type,
        "applied": applied,
        "reason": reason,
        "before_hash": before_hash,
        "after_hash": after_hash,
        "changed_line_count": changed_line_count,
        "drift_detected": drift_detected,
        "diagnostics": list(diagnostics or []),
        "preconditions": dict(preconditions or {}),
        "diff": diff,
        "details": dict(details or {}),
    }