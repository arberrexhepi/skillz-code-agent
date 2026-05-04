from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from .filesystem_ops import copy_file, create_file, delete_file, fill_template, rename_file
from .models import BatchMutationResult, MutationResult, make_mutation_result
from .structural_ops import insert_symbol_member, rename_symbol, replace_symbol
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


def batch_mutate(
    operations: list[dict[str, Any]],
    *,
    atomic: bool = False,
    root: Path | str | None = None,
) -> BatchMutationResult:
    workspace_root = Path(root or Path.cwd()).resolve()
    snapshots: dict[Path, tuple[bool, Optional[str]]] = {}
    results: list[MutationResult] = []
    rolled_back = False
    for index, operation in enumerate(operations):
        op_type = str(operation.get("type", "") or "").strip()
        file_paths = _operation_paths(operation)
        for file_path in file_paths:
            target = (workspace_root / file_path).resolve()
            if target not in snapshots:
                if target.exists() and target.is_file():
                    snapshots[target] = (True, target.read_text(encoding="utf-8"))
                else:
                    snapshots[target] = (False, None)
        result = _dispatch_operation(operation, root=workspace_root, index=index)
        results.append(result)
        if result.get("ok"):
            continue
        if atomic:
            _rollback_snapshots(snapshots)
            rolled_back = True
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
    kwargs = dict(operation)
    kwargs.pop("type", None)
    kwargs["root"] = root
    try:
        return handler(**kwargs)
    except TypeError as exc:
        return make_mutation_result(
            operation_id=f"batch:{index}:{op_type}",
            file_path=str(operation.get("file_path") or operation.get("path") or ""),
            mutation_type=op_type,
            ok=False,
            applied=False,
            reason="bad_request",
            diagnostics=[{"code": "BAD_REQUEST", "message": str(exc)}],
        )


def _handler_map() -> dict[str, OperationHandler]:
    return {
        "replace_range": replace_range,
        "replace_snippet": replace_snippet,
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


def _operation_paths(operation: dict[str, Any]) -> list[str]:
    paths: list[str] = []
    for key in ("file_path", "path", "old_path", "new_path", "source_path", "destination_path"):
        value = operation.get(key)
        if isinstance(value, str) and value.strip():
            paths.append(value.strip())
    unique: list[str] = []
    for path in paths:
        if path not in unique:
            unique.append(path)
    return unique


def _rollback_snapshots(snapshots: dict[Path, tuple[bool, Optional[str]]]) -> None:
    for path, (existed, content) in snapshots.items():
        if existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content or "", encoding="utf-8")
        elif path.exists():
            if path.is_file():
                path.unlink()