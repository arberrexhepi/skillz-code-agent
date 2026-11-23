"""One adapter for explicit mutations, hashes, path normalization and results."""

from __future__ import annotations

from pathlib import Path
import os

from mutations import batch_mutate


EXPLICIT_MUTATION_TYPES = frozenset({
    "replace_range", "replace_snippet", "replace_regex", "insert_before", "insert_after", "delete_range", "delete_snippet",
    "append_block", "prepend_block", "replace_symbol", "insert_symbol_member", "rename_symbol", "move_block",
    "create_file", "delete_file", "rename_file", "copy_file", "fill_template", "batch_mutate",
})
_PATH_KEYS = {"path", "file_path", "old_path", "new_path", "source_path", "destination_path"}


def execute_mutation(action: dict, root: Path) -> tuple[dict, list[str]]:
    if "root" in action:
        raise ValueError("Mutations cannot override the mounted workspace root")
    source = action.get("operations") if action.get("type") == "batch_mutate" else [action]
    if not isinstance(source, list) or not source:
        raise ValueError("A mutation batch requires at least one operation")
    operations = []
    for item in source:
        if not isinstance(item, dict) or "root" in item:
            raise ValueError("Mutation operations must be objects without root overrides")
        operation = {key: value for key, value in item.items() if not key.startswith("_")}
        if operation.get("type") not in EXPLICIT_MUTATION_TYPES - {"batch_mutate"}:
            raise ValueError(f"Unsupported mutation: {operation.get('type')}")
        for key in _PATH_KEYS & operation.keys():
            path = str(operation[key]).strip()
            normalized = os.path.normpath(path.removeprefix("/repo/").removeprefix("repo/"))
            if Path(normalized).is_absolute():
                try:
                    normalized = str(Path(normalized).resolve().relative_to(root.resolve()))
                except ValueError:
                    pass  # Batch preflight rejects paths outside the workspace.
            operation[key] = Path(normalized).as_posix()
        if operation.get("type") == "replace_symbol":
            operation.setdefault("symbol_kind", "")
        operations.append(operation)
    result = batch_mutate(operations, root=root, atomic=bool(action.get("atomic", False)))
    changed = set()
    if not result.get("rolled_back"):
        for operation, outcome in zip(operations, result["operations"]):
            if outcome.get("applied"):
                changed.update(str(operation[key]) for key in _PATH_KEYS & operation.keys())
    return result, sorted(changed)


def compact_mutation_result(result: dict) -> dict:
    return {**result, "operations": [
        {**outcome, "diff": str(outcome.get("diff", ""))[:6000]}
        for outcome in result.get("operations", [])
    ]}
