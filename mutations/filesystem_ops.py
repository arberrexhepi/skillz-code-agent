from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from .common import (
    file_missing_result,
    normalize_relative_path,
    read_text,
    resolve_root,
    safe_join,
    sha256_text,
    unified_diff_text,
    with_expected_hash,
    write_text,
)
from .models import MutationResult, make_mutation_result


def create_file(
    file_path: str,
    content: str,
    *,
    overwrite: bool = False,
    root: Path | str | None = None,
) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = "create_file"
    if target.exists() and not overwrite:
        existing = read_text(target) if target.is_file() else ""
        if target.is_file() and existing == content:
            return make_mutation_result(
                operation_id=f"{mutation_type}:{rel_path}",
                file_path=rel_path,
                mutation_type=mutation_type,
                ok=True,
                applied=False,
                reason="already_present",
                before_hash=sha256_text(existing) if target.is_file() else None,
                after_hash=sha256_text(existing) if target.is_file() else None,
            )
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="path_exists",
            diagnostics=[{"code": "PATH_EXISTS", "message": f"File already exists: {rel_path}"}],
        )
    before = read_text(target) if target.exists() and target.is_file() else ""
    write_text(target, content)
    return make_mutation_result(
        operation_id=f"{mutation_type}:{rel_path}",
        file_path=rel_path,
        mutation_type=mutation_type,
        ok=True,
        applied=True,
        before_hash=sha256_text(before) if before else None,
        after_hash=sha256_text(content),
        changed_line_count=abs(len(content.splitlines()) - len(before.splitlines())),
        diff=unified_diff_text(rel_path, before, content) if before else "",
        details={"created": not bool(before), "overwrite": overwrite},
    )


def delete_file(file_path: str, *, root: Path | str | None = None) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = "delete_file"
    if not target.exists():
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=True,
            applied=False,
            reason="already_absent",
        )
    if not target.is_file():
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="not_a_file",
            diagnostics=[{"code": "NOT_A_FILE", "message": f"Path is not a file: {rel_path}"}],
        )
    before = read_text(target)
    target.unlink()
    return make_mutation_result(
        operation_id=f"{mutation_type}:{rel_path}",
        file_path=rel_path,
        mutation_type=mutation_type,
        ok=True,
        applied=True,
        before_hash=sha256_text(before),
        changed_line_count=len(before.splitlines()),
        details={"deleted": True},
    )


def rename_file(old_path: str, new_path: str, *, root: Path | str | None = None) -> MutationResult:
    workspace_root = resolve_root(root)
    old_rel = normalize_relative_path(workspace_root, old_path)
    new_rel = normalize_relative_path(workspace_root, new_path)
    source = safe_join(workspace_root, old_rel)
    destination = safe_join(workspace_root, new_rel)
    mutation_type = "rename_file"
    if not source.exists() or not source.is_file():
        return file_missing_result(f"{mutation_type}:{old_rel}", old_rel, mutation_type)
    if destination.exists():
        return make_mutation_result(
            operation_id=f"{mutation_type}:{old_rel}",
            file_path=old_rel,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="destination_exists",
            diagnostics=[{"code": "DESTINATION_EXISTS", "message": f"Destination already exists: {new_rel}"}],
        )
    before = read_text(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.rename(destination)
    return make_mutation_result(
        operation_id=f"{mutation_type}:{old_rel}",
        file_path=old_rel,
        mutation_type=mutation_type,
        ok=True,
        applied=True,
        before_hash=sha256_text(before),
        after_hash=sha256_text(before),
        details={"new_path": new_rel},
    )


def copy_file(
    source_path: str,
    destination_path: str,
    *,
    overwrite: bool = False,
    root: Path | str | None = None,
) -> MutationResult:
    workspace_root = resolve_root(root)
    source_rel = normalize_relative_path(workspace_root, source_path)
    destination_rel = normalize_relative_path(workspace_root, destination_path)
    source = safe_join(workspace_root, source_rel)
    destination = safe_join(workspace_root, destination_rel)
    mutation_type = "copy_file"
    if not source.exists() or not source.is_file():
        return file_missing_result(f"{mutation_type}:{source_rel}", source_rel, mutation_type)
    if destination.exists() and not overwrite:
        return make_mutation_result(
            operation_id=f"{mutation_type}:{destination_rel}",
            file_path=destination_rel,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="destination_exists",
            diagnostics=[{"code": "DESTINATION_EXISTS", "message": f"Destination already exists: {destination_rel}"}],
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    content = read_text(source)
    return make_mutation_result(
        operation_id=f"{mutation_type}:{destination_rel}",
        file_path=destination_rel,
        mutation_type=mutation_type,
        ok=True,
        applied=True,
        after_hash=sha256_text(content),
        changed_line_count=len(content.splitlines()),
        details={"source_path": source_rel, "overwrite": overwrite},
    )


def fill_template(
    file_path: str,
    slots: dict[str, str],
    *,
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = "fill_template"
    if not target.exists() or not target.is_file():
        return file_missing_result(f"{mutation_type}:{rel_path}", rel_path, mutation_type)
    original = read_text(target)
    hash_result = with_expected_hash(workspace_root, rel_path, original, mutation_type, expected_hash)
    if hash_result is not None:
        return hash_result
    updated = original
    replacements = 0
    for key, value in slots.items():
        for pattern in (f"{{{{{key}}}}}", f"{{{{ {key} }}}}"):
            if pattern in updated:
                updated = updated.replace(pattern, value)
                replacements += 1
    if replacements == 0:
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="no_slots_replaced",
            before_hash=sha256_text(original),
            diagnostics=[{"code": "NO_SLOTS_REPLACED", "message": "No template slots matched the provided keys."}],
        )
    if updated == original:
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=True,
            applied=False,
            reason="already_applied",
            before_hash=sha256_text(original),
            after_hash=sha256_text(original),
        )
    write_text(target, updated)
    return make_mutation_result(
        operation_id=f"{mutation_type}:{rel_path}",
        file_path=rel_path,
        mutation_type=mutation_type,
        ok=True,
        applied=True,
        before_hash=sha256_text(original),
        after_hash=sha256_text(updated),
        changed_line_count=abs(len(updated.splitlines()) - len(original.splitlines())),
        diff=unified_diff_text(rel_path, original, updated),
        details={"slots": sorted(slots.keys()), "replacements": replacements},
    )