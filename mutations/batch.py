from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .filesystem_ops import copy_file, create_file, delete_file, fill_template, rename_file
from .models import BatchMutationResult, MutationResult, make_mutation_result
from .batch_state import BatchState
from .structural_ops import insert_symbol_member, rename_symbol, replace_symbol
from .regex_ops import replace_regex
from .text_ops import (
    append_block,
    delete_range,
    delete_snippet,
    insert_after,
    insert_before,
    move_block,
    prepend_block,
    replace_range,
    replace_snippet,
)


OperationHandler = Callable[..., MutationResult]


_SINGLE_PATH_OPERATIONS = {
    "replace_range",
    "replace_snippet",
    "replace_regex",
    "insert_before",
    "insert_after",
    "delete_range",
    "delete_snippet",
    "append_block",
    "prepend_block",
    "replace_symbol",
    "insert_symbol_member",
    "rename_symbol",
    "move_block",
    "create_file",
    "delete_file",
    "fill_template",
}


def batch_mutate(
    operations: list[dict[str, Any]],
    *,
    atomic: bool = False,
    root: Path | str | None = None,
) -> BatchMutationResult:
    workspace_root = Path(root or Path.cwd()).resolve()
    results: list[MutationResult] = []
    rolled_back = False
    try:
        state = BatchState(workspace_root, operations, _SINGLE_PATH_OPERATIONS - {"create_file", "delete_file"})
        preflight_error = state.validate_hashes(operations)
    except (ValueError, OSError, RuntimeError) as exc:
        error = make_mutation_result(operation_id="batch:preflight", file_path="", mutation_type="batch_mutate",
                                     ok=False, applied=False, reason="invalid_path", diagnostics=[{"code": "INVALID_PATH", "message": str(exc)}])
        return {"ok": False, "atomic": atomic, "operations": [error], "applied_count": 0, "failed_count": 1, "rolled_back": False}
    if preflight_error:
        return {"ok": False, "atomic": atomic, "operations": [preflight_error], "applied_count": 0, "failed_count": 1, "rolled_back": False}
    for index, operation in enumerate(operations):
        try:
            result = state.guard(operation, index)
            if result is None:
                result = _dispatch_operation(state.prepare(operation), root=workspace_root, index=index)
                post_error = state.record(operation, result, index)
            else:
                post_error = None
        except (TypeError, ValueError, OSError) as exc:
            result = make_mutation_result(operation_id=f"batch:{index}:guard", file_path=str(operation.get("file_path") or operation.get("path") or ""),
                mutation_type="batch_mutate", ok=False, applied=False, reason="batch_state_unavailable",
                diagnostics=[{"code": "BATCH_STATE_UNAVAILABLE", "message": str(exc)}], details={"operation_index": index + 1})
            post_error = None
        results.append(result)
        if post_error:
            results.append(post_error)
        if result.get("ok") and not post_error and index == len(operations) - 1:
            for name in state.paths:
                post_error = state.guard({"file_path": name}, index, phase="batch_completion")
                if post_error:
                    results.append(post_error)
                    break
        if result.get("ok") and not post_error:
            continue
        if atomic:
            conflicts = state.rollback()
            rolled_back = not conflicts
            if conflicts:
                results.append(make_mutation_result(operation_id="batch:rollback", file_path="", mutation_type="batch_mutate",
                    ok=False, applied=False, reason="rollback_conflict", diagnostics=[
                        {"code": "ROLLBACK_CONFLICT", "message": f"External change preserved during rollback: {path.relative_to(workspace_root)}"}
                        for path in conflicts]))
        break
    applied_count = sum(1 for result in results if result.get("applied"))
    failed_count = sum(1 for result in results if not result.get("ok"))
    return {
        "ok": failed_count == 0,
        "atomic": atomic,
        "operations": results,
        "applied_count": applied_count,
        "failed_count": failed_count,
        "rolled_back": rolled_back,
    }


def _dispatch_operation(operation: dict[str, Any], *, root: Path, index: int) -> MutationResult:
    op_type = str(operation.get("type", "") or "").strip()
    handler = _handler_map().get(op_type)
    if handler is None:
        return make_mutation_result(
            operation_id=f"batch:{index}:{op_type or 'unknown'}",
            file_path=str(operation.get("file_path") or operation.get("path") or ""),
            mutation_type=op_type or "unknown",
            ok=False,
            applied=False,
            reason="unknown_operation",
            diagnostics=[{"code": "UNKNOWN_OPERATION", "message": f"Unknown mutation operation: {op_type}"}],
        )
    kwargs = _operation_kwargs(op_type, operation)
    kwargs["root"] = root
    try:
        return handler(**kwargs)
    except (TypeError, ValueError, OSError) as exc:
        return make_mutation_result(
            operation_id=f"batch:{index}:{op_type}",
            file_path=str(operation.get("file_path") or operation.get("path") or ""),
            mutation_type=op_type,
            ok=False,
            applied=False,
            reason="bad_request",
            diagnostics=[{"code": "BAD_REQUEST", "message": str(exc)}],
        )


def _operation_kwargs(op_type: str, operation: dict[str, Any]) -> dict[str, Any]:
    """Translate the public action schema into mutation-handler arguments."""
    kwargs = dict(operation)
    kwargs.pop("type", None)
    if op_type in _SINGLE_PATH_OPERATIONS and "file_path" not in kwargs and "path" in kwargs:
        kwargs["file_path"] = kwargs.pop("path")
    if op_type == "replace_snippet" and "replace_all" not in kwargs and "all" in kwargs:
        kwargs["replace_all"] = kwargs.pop("all")
    return kwargs


def _handler_map() -> dict[str, OperationHandler]:
    return {
        "replace_range": replace_range,
        "replace_snippet": replace_snippet,
        "replace_regex": replace_regex,
        "insert_before": insert_before,
        "insert_after": insert_after,
        "delete_range": delete_range,
        "delete_snippet": delete_snippet,
        "append_block": append_block,
        "prepend_block": prepend_block,
        "replace_symbol": replace_symbol,
        "insert_symbol_member": insert_symbol_member,
        "rename_symbol": rename_symbol,
        "move_block": move_block,
        "create_file": create_file,
        "delete_file": delete_file,
        "rename_file": rename_file,
        "copy_file": copy_file,
        "fill_template": fill_template,
    }
