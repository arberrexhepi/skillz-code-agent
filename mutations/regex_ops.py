"""Guarded regex replacements using the shared bounded evaluator."""

from pathlib import Path

from .common import (file_missing_result, normalize_relative_path, resolve_root, safe_join,
                     with_expected_hash, expected_hash_mismatch)
from .models import make_mutation_result
from regex_runtime import evaluate_regex, read_source, source_hash


def replace_regex(file_path: str, pattern: str, replacement: str, *, expected_matches: int = 1,
                  flags: str = "", expected_hash: str | None = None, root: Path | str | None = None) -> dict:
    if type(expected_matches) is not int or expected_matches < 1 or not isinstance(replacement, str):
        raise ValueError("replace_regex requires a string replacement and a positive exact expected_matches count")
    workspace_root = resolve_root(root)
    path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, path)
    if not target.is_file():
        return file_missing_result("replace_regex:" + path, path, "replace_regex")
    original = read_source(target)
    before_hash = source_hash(original)
    mismatch = with_expected_hash(workspace_root, path, original, "replace_regex", expected_hash)
    if mismatch is not None:
        return mismatch
    result = evaluate_regex(original, pattern=pattern, replacement=replacement, flags=flags,
                            expected_matches=expected_matches, limit=1, before=0, after=0, file_path=path)
    if not result["ok"]:
        return make_mutation_result(operation_id="replace_regex:" + path, file_path=path, mutation_type="replace_regex",
                                    ok=False, applied=False, reason=result["reason"], before_hash=before_hash,
                                    diagnostics=[{"code": result["reason"].upper(), "message": result["error"]}], details=result)
    # Evaluation runs off-process: detect edits made while it was evaluating too.
    current_hash = source_hash(read_source(target))
    if current_hash != before_hash:
        return expected_hash_mismatch(path, "replace_regex", before_hash, current_hash)
    updated = result.pop("updated")
    applied = updated != original
    if applied:
        with target.open("w", encoding="utf-8", newline="") as handle:
            handle.write(updated)
    return make_mutation_result(operation_id="replace_regex:" + path, file_path=path, mutation_type="replace_regex",
                                ok=True, applied=applied, reason=None if applied else "already_applied",
                                before_hash=before_hash, after_hash=source_hash(updated), diff=result.get("diff", ""),
                                changed_line_count=result.get("changed_line_count", 0),
                                preconditions={"expected_hash": expected_hash, "expected_occurrences": expected_matches},
                                details={"match_count": result["match_count"], "replacements": result["match_count"] if applied else 0,
                                         "diff_truncated": result.get("diff_truncated", False)})
