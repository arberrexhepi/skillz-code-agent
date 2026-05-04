from __future__ import annotations

from pathlib import Path
from typing import Optional

from .common import (
    already_present_adjacent,
    ensure_trailing_newline_like,
    file_missing_result,
    join_lines,
    line_delta,
    normalize_relative_path,
    read_text,
    resolve_root,
    safe_join,
    sha256_text,
    split_lines,
    unified_diff_text,
    with_expected_hash,
    write_text,
)
from .models import MutationResult, make_mutation_result


def replace_range(
    file_path: str,
    start_line: int,
    end_line: int,
    new_text: str,
    *,
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = "replace_range"
    if not target.exists() or not target.is_file():
        return file_missing_result(f"{mutation_type}:{rel_path}", rel_path, mutation_type)
    original = read_text(target)
    hash_result = with_expected_hash(workspace_root, rel_path, original, mutation_type, expected_hash)
    if hash_result is not None:
        return hash_result
    lines = split_lines(original)
    if start_line <= 0 or end_line < start_line or start_line > len(lines):
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="invalid_line_range",
            before_hash=sha256_text(original),
            diagnostics=[{"code": "INVALID_LINE_RANGE", "message": "Requested line range is invalid for the current file."}],
            preconditions={"line_range_stable": False},
        )
    replacement_lines = split_lines(new_text)
    updated_lines = lines[: start_line - 1] + replacement_lines + lines[end_line:]
    updated = join_lines(updated_lines, trailing_newline=original.endswith("\n") or new_text.endswith("\n"))
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
            preconditions={"line_range_stable": True},
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
        changed_line_count=line_delta(original, updated),
        preconditions={"line_range_stable": True},
        diff=unified_diff_text(rel_path, original, updated),
        details={"start_line": start_line, "end_line": end_line},
    )


def replace_snippet(
    file_path: str,
    old_text: str,
    new_text: str,
    *,
    expected_occurrences: int = 1,
    replace_all: bool = False,
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = "replace_snippet"
    if not target.exists() or not target.is_file():
        return file_missing_result(f"{mutation_type}:{rel_path}", rel_path, mutation_type)
    original = read_text(target)
    hash_result = with_expected_hash(workspace_root, rel_path, original, mutation_type, expected_hash)
    if hash_result is not None:
        return hash_result
    count = original.count(old_text)
    if count == 0:
        if new_text and new_text in original:
            return make_mutation_result(
                operation_id=f"{mutation_type}:{rel_path}",
                file_path=rel_path,
                mutation_type=mutation_type,
                ok=True,
                applied=False,
                reason="already_applied",
                before_hash=sha256_text(original),
                after_hash=sha256_text(original),
                preconditions={"expected_occurrences": expected_occurrences, "anchor_present": False},
            )
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="snippet_not_found",
            before_hash=sha256_text(original),
            diagnostics=[{"code": "SNIPPET_NOT_FOUND", "message": "Target snippet was not found."}],
            preconditions={"expected_occurrences": expected_occurrences, "anchor_present": False},
        )
    if not replace_all and expected_occurrences > 0 and count != expected_occurrences:
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="occurrence_mismatch",
            before_hash=sha256_text(original),
            drift_detected=count != expected_occurrences,
            diagnostics=[{"code": "OCCURRENCE_MISMATCH", "message": f"Expected {expected_occurrences} occurrence(s) but found {count}."}],
            preconditions={"expected_occurrences": expected_occurrences, "anchor_present": True},
        )
    updated = original.replace(old_text, new_text if new_text is not None else "", count if replace_all else 1)
    updated = ensure_trailing_newline_like(original, updated)
    write_text(target, updated)
    return make_mutation_result(
        operation_id=f"{mutation_type}:{rel_path}",
        file_path=rel_path,
        mutation_type=mutation_type,
        ok=True,
        applied=True,
        before_hash=sha256_text(original),
        after_hash=sha256_text(updated),
        changed_line_count=line_delta(original, updated),
        preconditions={"expected_occurrences": expected_occurrences, "anchor_present": True},
        diff=unified_diff_text(rel_path, original, updated),
        details={"replacements": count if replace_all else 1},
    )


def insert_before(
    file_path: str,
    anchor_text: str,
    new_text: str,
    *,
    expected_occurrences: int = 1,
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    return _insert_relative(
        file_path=file_path,
        anchor_text=anchor_text,
        new_text=new_text,
        expected_occurrences=expected_occurrences,
        position="before",
        root=root,
        expected_hash=expected_hash,
    )


def insert_after(
    file_path: str,
    anchor_text: str,
    new_text: str,
    *,
    expected_occurrences: int = 1,
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    return _insert_relative(
        file_path=file_path,
        anchor_text=anchor_text,
        new_text=new_text,
        expected_occurrences=expected_occurrences,
        position="after",
        root=root,
        expected_hash=expected_hash,
    )


def _insert_relative(
    *,
    file_path: str,
    anchor_text: str,
    new_text: str,
    expected_occurrences: int,
    position: str,
    root: Path | str | None,
    expected_hash: Optional[str],
) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = f"insert_{position}"
    if not target.exists() or not target.is_file():
        return file_missing_result(f"{mutation_type}:{rel_path}", rel_path, mutation_type)
    original = read_text(target)
    hash_result = with_expected_hash(workspace_root, rel_path, original, mutation_type, expected_hash)
    if hash_result is not None:
        return hash_result
    count = original.count(anchor_text)
    if count == 0:
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="anchor_not_found",
            before_hash=sha256_text(original),
            diagnostics=[{"code": "ANCHOR_NOT_FOUND", "message": "Anchor text was not found."}],
            preconditions={"anchor_present": False, "expected_occurrences": expected_occurrences},
        )
    if count != expected_occurrences:
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="anchor_ambiguous",
            before_hash=sha256_text(original),
            drift_detected=True,
            diagnostics=[{"code": "ANCHOR_AMBIGUOUS", "message": f"Expected {expected_occurrences} anchor occurrence(s) but found {count}."}],
            preconditions={"anchor_present": True, "expected_occurrences": expected_occurrences},
        )
    if already_present_adjacent(original, anchor_text, new_text, position=position):
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=True,
            applied=False,
            reason="already_present",
            before_hash=sha256_text(original),
            after_hash=sha256_text(original),
            preconditions={"anchor_present": True, "expected_occurrences": expected_occurrences},
        )
    replacement = f"{new_text}{anchor_text}" if position == "before" else f"{anchor_text}{new_text}"
    updated = original.replace(anchor_text, replacement, 1)
    updated = ensure_trailing_newline_like(original, updated)
    write_text(target, updated)
    return make_mutation_result(
        operation_id=f"{mutation_type}:{rel_path}",
        file_path=rel_path,
        mutation_type=mutation_type,
        ok=True,
        applied=True,
        before_hash=sha256_text(original),
        after_hash=sha256_text(updated),
        changed_line_count=line_delta(original, updated),
        preconditions={"anchor_present": True, "expected_occurrences": expected_occurrences},
        diff=unified_diff_text(rel_path, original, updated),
    )


def delete_range(
    file_path: str,
    start_line: int,
    end_line: int,
    *,
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    return replace_range(file_path, start_line, end_line, "", root=root, expected_hash=expected_hash)


def delete_snippet(
    file_path: str,
    text: str,
    *,
    expected_occurrences: int = 1,
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = "delete_snippet"
    if not target.exists() or not target.is_file():
        return file_missing_result(f"{mutation_type}:{rel_path}", rel_path, mutation_type)
    original = read_text(target)
    hash_result = with_expected_hash(workspace_root, rel_path, original, mutation_type, expected_hash)
    if hash_result is not None:
        return hash_result
    count = original.count(text)
    if count == 0:
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=True,
            applied=False,
            reason="already_absent",
            before_hash=sha256_text(original),
            after_hash=sha256_text(original),
            preconditions={"expected_occurrences": expected_occurrences, "anchor_present": False},
        )
    if expected_occurrences > 0 and count != expected_occurrences:
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="occurrence_mismatch",
            before_hash=sha256_text(original),
            drift_detected=True,
            diagnostics=[{"code": "OCCURRENCE_MISMATCH", "message": f"Expected {expected_occurrences} occurrence(s) but found {count}."}],
            preconditions={"expected_occurrences": expected_occurrences, "anchor_present": True},
        )
    updated = original.replace(text, "", 1)
    updated = ensure_trailing_newline_like(original, updated)
    write_text(target, updated)
    return make_mutation_result(
        operation_id=f"{mutation_type}:{rel_path}",
        file_path=rel_path,
        mutation_type=mutation_type,
        ok=True,
        applied=True,
        before_hash=sha256_text(original),
        after_hash=sha256_text(updated),
        changed_line_count=line_delta(original, updated),
        preconditions={"expected_occurrences": expected_occurrences, "anchor_present": True},
        diff=unified_diff_text(rel_path, original, updated),
    )


def append_block(
    file_path: str,
    new_text: str,
    *,
    ensure_newline: bool = True,
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = "append_block"
    if not target.exists() or not target.is_file():
        return file_missing_result(f"{mutation_type}:{rel_path}", rel_path, mutation_type)
    original = read_text(target)
    hash_result = with_expected_hash(workspace_root, rel_path, original, mutation_type, expected_hash)
    if hash_result is not None:
        return hash_result
    normalized_new_text = new_text
    if ensure_newline and normalized_new_text and not normalized_new_text.endswith("\n"):
        normalized_new_text += "\n"
    if original.endswith(normalized_new_text):
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=True,
            applied=False,
            reason="already_present",
            before_hash=sha256_text(original),
            after_hash=sha256_text(original),
        )
    updated = original
    if ensure_newline and updated and not updated.endswith("\n"):
        updated += "\n"
    updated += normalized_new_text
    write_text(target, updated)
    return make_mutation_result(
        operation_id=f"{mutation_type}:{rel_path}",
        file_path=rel_path,
        mutation_type=mutation_type,
        ok=True,
        applied=True,
        before_hash=sha256_text(original),
        after_hash=sha256_text(updated),
        changed_line_count=line_delta(original, updated),
        diff=unified_diff_text(rel_path, original, updated),
    )


def prepend_block(
    file_path: str,
    new_text: str,
    *,
    ensure_newline: bool = True,
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = "prepend_block"
    if not target.exists() or not target.is_file():
        return file_missing_result(f"{mutation_type}:{rel_path}", rel_path, mutation_type)
    original = read_text(target)
    hash_result = with_expected_hash(workspace_root, rel_path, original, mutation_type, expected_hash)
    if hash_result is not None:
        return hash_result
    normalized_new_text = new_text
    if ensure_newline and normalized_new_text and not normalized_new_text.endswith("\n"):
        normalized_new_text += "\n"
    if original.startswith(normalized_new_text):
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=True,
            applied=False,
            reason="already_present",
            before_hash=sha256_text(original),
            after_hash=sha256_text(original),
        )
    updated = normalized_new_text + original
    write_text(target, updated)
    return make_mutation_result(
        operation_id=f"{mutation_type}:{rel_path}",
        file_path=rel_path,
        mutation_type=mutation_type,
        ok=True,
        applied=True,
        before_hash=sha256_text(original),
        after_hash=sha256_text(updated),
        changed_line_count=line_delta(original, updated),
        diff=unified_diff_text(rel_path, original, updated),
    )


def move_block(
    file_path: str,
    start_line: int,
    end_line: int,
    destination_anchor: str,
    *,
    position: str = "after",
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = "move_block"
    if not target.exists() or not target.is_file():
        return file_missing_result(f"{mutation_type}:{rel_path}", rel_path, mutation_type)
    original = read_text(target)
    hash_result = with_expected_hash(workspace_root, rel_path, original, mutation_type, expected_hash)
    if hash_result is not None:
        return hash_result
    lines = split_lines(original)
    if start_line <= 0 or end_line < start_line or start_line > len(lines):
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="invalid_line_range",
            before_hash=sha256_text(original),
            diagnostics=[{"code": "INVALID_LINE_RANGE", "message": "Requested move range is invalid."}],
            preconditions={"line_range_stable": False},
        )
    block = lines[start_line - 1:end_line]
    remaining = lines[: start_line - 1] + lines[end_line:]
    anchor_text = destination_anchor
    remaining_text = join_lines(remaining, trailing_newline=original.endswith("\n"))
    if anchor_text not in remaining_text:
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="anchor_not_found",
            before_hash=sha256_text(original),
            diagnostics=[{"code": "ANCHOR_NOT_FOUND", "message": "Destination anchor was not found after removing the moved block."}],
            preconditions={"anchor_present": False},
        )
    block_text = join_lines(block, trailing_newline=True)
    replacement = f"{anchor_text}{block_text}" if position == "after" else f"{block_text}{anchor_text}"
    updated = remaining_text.replace(anchor_text, replacement, 1)
    updated = ensure_trailing_newline_like(original, updated)
    write_text(target, updated)
    return make_mutation_result(
        operation_id=f"{mutation_type}:{rel_path}",
        file_path=rel_path,
        mutation_type=mutation_type,
        ok=True,
        applied=True,
        before_hash=sha256_text(original),
        after_hash=sha256_text(updated),
        changed_line_count=line_delta(original, updated),
        diff=unified_diff_text(rel_path, original, updated),
        details={"start_line": start_line, "end_line": end_line, "position": position},
    )