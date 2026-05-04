from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Optional

from .common import (
    file_missing_result,
    line_delta,
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


def replace_symbol(
    file_path: str,
    symbol_name: str,
    symbol_kind: str,
    new_text: str,
    *,
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = "replace_symbol"
    if not target.exists() or not target.is_file():
        return file_missing_result(f"{mutation_type}:{rel_path}", rel_path, mutation_type)
    original = read_text(target)
    hash_result = with_expected_hash(workspace_root, rel_path, original, mutation_type, expected_hash)
    if hash_result is not None:
        return hash_result
    symbol_range = _locate_symbol_range(rel_path, original, symbol_name, symbol_kind)
    if symbol_range is None:
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="symbol_not_found",
            before_hash=sha256_text(original),
            diagnostics=[{"code": "SYMBOL_NOT_FOUND", "message": f"Could not resolve {symbol_kind} {symbol_name}."}],
            preconditions={"symbol_resolves": False},
        )
    start_line, end_line = symbol_range
    lines = original.splitlines()
    replacement_lines = new_text.splitlines()
    updated_lines = lines[: start_line - 1] + replacement_lines + lines[end_line:]
    updated = "\n".join(updated_lines)
    if original.endswith("\n") and updated and not updated.endswith("\n"):
        updated += "\n"
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
            preconditions={"symbol_resolves": True},
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
        preconditions={"symbol_resolves": True},
        diff=unified_diff_text(rel_path, original, updated),
        details={"symbol_name": symbol_name, "symbol_kind": symbol_kind, "start_line": start_line, "end_line": end_line},
    )


def insert_symbol_member(
    file_path: str,
    container_symbol: str,
    member_text: str,
    *,
    position: str = "end",
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = "insert_symbol_member"
    if not target.exists() or not target.is_file():
        return file_missing_result(f"{mutation_type}:{rel_path}", rel_path, mutation_type)
    original = read_text(target)
    hash_result = with_expected_hash(workspace_root, rel_path, original, mutation_type, expected_hash)
    if hash_result is not None:
        return hash_result
    insertion = _insert_member(rel_path, original, container_symbol, member_text, position=position)
    if insertion is None:
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="container_not_found",
            before_hash=sha256_text(original),
            diagnostics=[{"code": "CONTAINER_NOT_FOUND", "message": f"Could not resolve container symbol {container_symbol}."}],
            preconditions={"symbol_resolves": False},
        )
    updated = insertion
    if updated == original:
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=True,
            applied=False,
            reason="already_present",
            before_hash=sha256_text(original),
            after_hash=sha256_text(original),
            preconditions={"symbol_resolves": True},
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
        preconditions={"symbol_resolves": True},
        diff=unified_diff_text(rel_path, original, updated),
        details={"container_symbol": container_symbol, "position": position},
    )


def rename_symbol(
    file_path: str,
    old_name: str,
    new_name: str,
    *,
    scope: str = "file",
    root: Path | str | None = None,
    expected_hash: Optional[str] = None,
) -> MutationResult:
    workspace_root = resolve_root(root)
    rel_path = normalize_relative_path(workspace_root, file_path)
    target = safe_join(workspace_root, rel_path)
    mutation_type = "rename_symbol"
    if not target.exists() or not target.is_file():
        return file_missing_result(f"{mutation_type}:{rel_path}", rel_path, mutation_type)
    original = read_text(target)
    hash_result = with_expected_hash(workspace_root, rel_path, original, mutation_type, expected_hash)
    if hash_result is not None:
        return hash_result
    pattern = re.compile(rf"\b{re.escape(old_name)}\b")
    count = len(pattern.findall(original))
    if count == 0:
        if re.search(rf"\b{re.escape(new_name)}\b", original):
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
        return make_mutation_result(
            operation_id=f"{mutation_type}:{rel_path}",
            file_path=rel_path,
            mutation_type=mutation_type,
            ok=False,
            applied=False,
            reason="symbol_not_found",
            before_hash=sha256_text(original),
            diagnostics=[{"code": "SYMBOL_NOT_FOUND", "message": f"Could not find token {old_name} in {scope} scope."}],
        )
    updated = pattern.sub(new_name, original)
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
        details={"old_name": old_name, "new_name": new_name, "scope": scope, "rename_count": count},
    )


def _locate_symbol_range(file_path: str, content: str, symbol_name: str, symbol_kind: str) -> Optional[tuple[int, int]]:
    suffix = Path(file_path).suffix.lower()
    if suffix == ".py":
        return _locate_python_symbol_range(content, symbol_name, symbol_kind)
    if suffix in {".js", ".jsx", ".ts", ".tsx"}:
        return _locate_script_symbol_range(content, symbol_name, symbol_kind)
    return None


def _locate_python_symbol_range(content: str, symbol_name: str, symbol_kind: str) -> Optional[tuple[int, int]]:
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return None
    target_method = None
    target_class = None
    if symbol_kind == "method" and "." in symbol_name:
        target_class, target_method = symbol_name.split(".", 1)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if symbol_kind == "class" and node.name == symbol_name:
                return node.lineno, int(node.end_lineno or node.lineno)
            if target_class and node.name == target_class:
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == target_method:
                        return child.lineno, int(child.end_lineno or child.lineno)
        if isinstance(node, ast.FunctionDef) and symbol_kind in {"function", "method"} and node.name == symbol_name:
            return node.lineno, int(node.end_lineno or node.lineno)
        if isinstance(node, ast.AsyncFunctionDef) and symbol_kind in {"function", "method"} and node.name == symbol_name:
            return node.lineno, int(node.end_lineno or node.lineno)
        if isinstance(node, ast.Assign) and symbol_kind in {"constant", "variable"}:
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == symbol_name:
                    return node.lineno, int(node.end_lineno or node.lineno)
    return None


def _locate_script_symbol_range(content: str, symbol_name: str, symbol_kind: str) -> Optional[tuple[int, int]]:
    lines = content.splitlines()
    patterns = [
        re.compile(rf"^\s*(?:export\s+default\s+)?function\s+{re.escape(symbol_name)}\b"),
        re.compile(rf"^\s*(?:export\s+)?class\s+{re.escape(symbol_name)}\b"),
        re.compile(rf"^\s*(?:export\s+)?interface\s+{re.escape(symbol_name)}\b"),
        re.compile(rf"^\s*(?:export\s+)?type\s+{re.escape(symbol_name)}\b"),
        re.compile(rf"^\s*(?:export\s+)?(?:const|let|var)\s+{re.escape(symbol_name)}\b"),
    ]
    for index, line in enumerate(lines):
        if not any(pattern.match(line) for pattern in patterns):
            continue
        end_line = _find_block_end(lines, index)
        return index + 1, end_line + 1
    return None


def _find_block_end(lines: list[str], start_index: int) -> int:
    brace_balance = 0
    saw_brace = False
    for index in range(start_index, len(lines)):
        line = lines[index]
        brace_balance += line.count("{")
        if line.count("{"):
            saw_brace = True
        brace_balance -= line.count("}")
        if saw_brace and brace_balance <= 0:
            return index
        if not saw_brace and line.rstrip().endswith(";"):
            return index
    return len(lines) - 1


def _insert_member(file_path: str, content: str, container_symbol: str, member_text: str, *, position: str) -> Optional[str]:
    symbol_range = _locate_symbol_range(file_path, content, container_symbol, "class")
    if symbol_range is None:
        symbol_range = _locate_symbol_range(file_path, content, container_symbol, "interface")
    if symbol_range is None:
        symbol_range = _locate_symbol_range(file_path, content, container_symbol, "constant")
    if symbol_range is None:
        return None
    start_line, end_line = symbol_range
    lines = content.splitlines()
    insert_index = end_line - 1 if position == "end" else start_line
    indent = _infer_member_indent(lines, start_line - 1, end_line - 1)
    normalized_member = member_text
    if normalized_member and not normalized_member.endswith("\n"):
        normalized_member += "\n"
    member_lines = [f"{indent}{line}" if line else "" for line in normalized_member.rstrip("\n").splitlines()]
    updated_lines = lines[:insert_index] + member_lines + lines[insert_index:]
    updated = "\n".join(updated_lines)
    if content.endswith("\n") and not updated.endswith("\n"):
        updated += "\n"
    return updated


def _infer_member_indent(lines: list[str], start_index: int, end_index: int) -> str:
    container_indent = len(lines[start_index]) - len(lines[start_index].lstrip(" "))
    for index in range(start_index + 1, min(end_index, len(lines))):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped == "}":
            continue
        return " " * (len(line) - len(line.lstrip(" ")))
    return " " * (container_indent + 4)