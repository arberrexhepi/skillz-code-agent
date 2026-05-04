from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from .common import infer_language
from .models import SymbolRecord


def scan_python_symbols(text: str) -> list[tuple[re.Pattern[str], str]]:
    return [
        (re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b(?:\((.*?)\))?:"), "class"),
        (re.compile(r"^\s*async\s+def\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*\((.*?)\)\s*(?:->\s*([^:]+))?:"), "async_function"),
        (re.compile(r"^\s*def\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*\((.*?)\)\s*(?:->\s*([^:]+))?:"), "function"),
    ]


def scan_frontend_symbols(text: str) -> list[tuple[re.Pattern[str], str]]:
    return [
        (re.compile(r"^\s*export\s+default\s+function\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*\((.*?)\)"), "exported_function"),
        (re.compile(r"^\s*export\s+function\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*\((.*?)\)"), "exported_function"),
        (re.compile(r"^\s*function\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*\((.*?)\)"), "function"),
        (re.compile(r"^\s*export\s+(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*=\s*(?:async\s*)?\((.*?)\)\s*=>"), "exported_variable"),
        (re.compile(r"^\s*(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*=\s*(?:async\s*)?\((.*?)\)\s*=>"), "variable"),
        (re.compile(r"^\s*export\s+class\s+([A-Za-z_][A-Za-z0-9_]*)\b(?:\s+extends\s+([^\s{]+))?"), "exported_class"),
        (re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b(?:\s+extends\s+([^\s{]+))?"), "class"),
        (re.compile(r"^\s*export\s+interface\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "interface"),
        (re.compile(r"^\s*export\s+type\s+([A-Za-z_][A-Za-z0-9_]*)\b\s*="), "type"),
        (re.compile(r"^\s*export\s+enum\s+([A-Za-z_][A-Za-z0-9_]*)\b"), "enum"),
    ]


def extract_python_imports(text: str) -> list[dict[str, Any]]:
    imports: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        import_match = re.match(r"^import\s+(.+)$", stripped)
        if import_match:
            modules = [part.strip() for part in import_match.group(1).split(",") if part.strip()]
            for module in modules:
                imports.append({"line": lineno, "module": module.split(" as ", 1)[0].strip(), "kind": "import"})
            continue
        from_match = re.match(r"^from\s+([A-Za-z0-9_\.]+)\s+import\s+(.+)$", stripped)
        if from_match:
            names = [part.strip() for part in from_match.group(2).split(",") if part.strip()]
            imports.append({"line": lineno, "module": from_match.group(1), "names": names, "kind": "from_import"})
    return imports


def extract_frontend_imports(text: str) -> list[dict[str, Any]]:
    imports: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        import_match = re.match(r"^import\s+(.+?)\s+from\s+[\"']([^\"']+)[\"']", stripped)
        if import_match:
            imports.append({"line": lineno, "module": import_match.group(2), "binding": import_match.group(1).strip(), "kind": "import"})
            continue
        bare_import_match = re.match(r"^import\s+[\"']([^\"']+)[\"']", stripped)
        if bare_import_match:
            imports.append({"line": lineno, "module": bare_import_match.group(1), "kind": "side_effect_import"})
    return imports


def collect_symbols_for_file(path: Path, text: str) -> list[SymbolRecord]:
    language = infer_language(path)
    if language == "python":
        return _collect_python_symbols(text)
    if language in {"javascript", "typescript"}:
        return _collect_script_symbols(text, language)
    return []


def extract_dependencies_for_file(path: Path, text: str, symbols: list[SymbolRecord]) -> dict[str, Any]:
    language = infer_language(path)
    if language == "python":
        imports = extract_python_imports(text)
    elif language in {"javascript", "typescript"}:
        imports = extract_frontend_imports(text)
    else:
        imports = []
    exports = extract_exports_for_file(path, text, symbols)
    return {"imports": imports, "exports": exports}


def extract_exports_for_file(path: Path, text: str, symbols: list[SymbolRecord]) -> list[dict[str, Any]]:
    language = infer_language(path)
    exports: list[dict[str, Any]] = []
    if language == "python":
        all_match = re.search(r"^__all__\s*=\s*\[(.*?)\]", text, flags=re.MULTILINE | re.DOTALL)
        if all_match:
            names = [part.strip().strip("\"'") for part in all_match.group(1).split(",") if part.strip()]
            for name in names:
                exports.append({"name": name, "kind": "explicit_export"})
        else:
            for symbol in symbols:
                name = str(symbol.get("name", ""))
                if name and not name.startswith("_") and str(symbol.get("kind", "")) in {"class", "function", "async_function"}:
                    exports.append({"name": name, "kind": "implicit_export"})
        return exports
    if language in {"javascript", "typescript"}:
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            default_match = re.match(r"^export\s+default\s+(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)?", stripped)
            if default_match:
                exports.append({"line": lineno, "name": default_match.group(1) or "default", "kind": "default_export"})
                continue
            named_match = re.match(r"^export\s+\{(.+)\}", stripped)
            if named_match:
                names = [part.strip().split(" as ", 1)[-1].strip() for part in named_match.group(1).split(",") if part.strip()]
                for name in names:
                    exports.append({"line": lineno, "name": name, "kind": "named_export"})
        for symbol in symbols:
            if str(symbol.get("kind", "")).startswith("exported_"):
                exports.append({"line": symbol.get("line"), "name": symbol.get("name"), "kind": symbol.get("kind")})
    return exports


def locate_symbol_range(path: Path, text: str, symbol_name: str, symbol_kind: Optional[str] = None) -> Optional[tuple[int, int]]:
    language = infer_language(path)
    if language == "python":
        return _locate_python_symbol_range(text, symbol_name, symbol_kind)
    if language in {"javascript", "typescript"}:
        return _locate_script_symbol_range(text, symbol_name, symbol_kind)
    return None


def constants_for_file(path: Path, text: str) -> list[dict[str, Any]]:
    language = infer_language(path)
    constants: list[dict[str, Any]] = []
    if language == "python":
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = re.match(r"^([A-Z][A-Z0-9_]+)\s*=\s*(.+)$", line.strip())
            if match:
                constants.append({"name": match.group(1), "line": lineno, "preview": match.group(0)[:200]})
    elif language in {"javascript", "typescript"}:
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = re.match(r"^(?:export\s+)?const\s+([A-Z][A-Z0-9_]+)\b", line.strip())
            if match:
                constants.append({"name": match.group(1), "line": lineno, "preview": line.strip()[:200]})
    return constants


def sections_for_file(path: Path, text: str) -> list[dict[str, Any]]:
    language = infer_language(path)
    sections: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if language == "markdown" and stripped.startswith("#"):
            sections.append({"line": lineno, "label": stripped.lstrip("# "), "kind": "heading"})
        elif stripped.startswith("# ") or stripped.startswith("// ") or stripped.startswith("/*"):
            if len(stripped) > 4:
                sections.append({"line": lineno, "label": stripped[:120], "kind": "comment_section"})
    return sections[:50]


def _collect_python_symbols(text: str) -> list[SymbolRecord]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _collect_python_symbols_fallback(text)
    symbols: list[SymbolRecord] = []
    for node in tree.body:
        _append_python_symbol(symbols, node, parent=None)
    return symbols


def _append_python_symbol(symbols: list[SymbolRecord], node: ast.AST, parent: Optional[str]) -> None:
    if isinstance(node, ast.ClassDef):
        symbols.append({
            "name": node.name,
            "kind": "class",
            "line": int(node.lineno),
            "end_line": int(node.end_lineno or node.lineno),
            "signature": f"class {node.name}",
            "language": "python",
            **({"parent": parent} if parent else {}),
        })
        for child in node.body:
            _append_python_symbol(symbols, child, parent=node.name)
    elif isinstance(node, ast.FunctionDef):
        kind = "method" if parent else "function"
        record: SymbolRecord = {
            "name": node.name,
            "kind": kind,
            "line": int(node.lineno),
            "end_line": int(node.end_lineno or node.lineno),
            "signature": f"def {node.name}{ast.unparse(node.args) if hasattr(ast, 'unparse') else ''}",
            "language": "python",
        }
        if parent:
            record["parent"] = parent
            record["qualified_name"] = f"{parent}.{node.name}"
        symbols.append(record)
    elif isinstance(node, ast.AsyncFunctionDef):
        kind = "async_method" if parent else "async_function"
        record = {
            "name": node.name,
            "kind": kind,
            "line": int(node.lineno),
            "end_line": int(node.end_lineno or node.lineno),
            "signature": f"async def {node.name}",
            "language": "python",
        }
        if parent:
            record["parent"] = parent
            record["qualified_name"] = f"{parent}.{node.name}"
        symbols.append(record)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                symbols.append({
                    "name": target.id,
                    "kind": "constant" if target.id.isupper() else "variable",
                    "line": int(node.lineno),
                    "end_line": int(node.end_lineno or node.lineno),
                    "signature": target.id,
                    "language": "python",
                })


def _collect_python_symbols_fallback(text: str) -> list[SymbolRecord]:
    patterns = scan_python_symbols(text)
    symbols: list[SymbolRecord] = []
    class_stack: list[tuple[int, str]] = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        while class_stack and indent <= class_stack[-1][0]:
            class_stack.pop()
        for pattern, kind in patterns:
            match = pattern.match(line)
            if not match:
                continue
            name = match.group(1)
            record: SymbolRecord = {
                "name": name,
                "kind": kind,
                "line": lineno,
                "end_line": _find_indented_block_end(lines, lineno - 1, indent),
                "signature": stripped,
                "language": "python",
            }
            if kind == "class":
                class_stack.append((indent, name))
            elif class_stack:
                record["parent"] = class_stack[-1][1]
                record["qualified_name"] = f"{class_stack[-1][1]}.{name}"
                record["kind"] = "method" if kind == "function" else "async_method"
            symbols.append(record)
            break
    return symbols


def _collect_script_symbols(text: str, language: str) -> list[SymbolRecord]:
    patterns = scan_frontend_symbols(text)
    symbols: list[SymbolRecord] = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for pattern, kind in patterns:
            match = pattern.match(line)
            if not match:
                continue
            end_line = _find_script_block_end(lines, lineno - 1)
            symbols.append({
                "name": match.group(1),
                "kind": kind,
                "line": lineno,
                "end_line": end_line,
                "signature": line.strip(),
                "language": language,
            })
            break
    return symbols


def _locate_python_symbol_range(text: str, symbol_name: str, symbol_kind: Optional[str]) -> Optional[tuple[int, int]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    target_class = None
    target_method = None
    if symbol_kind in {"method", "async_method"} and "." in symbol_name:
        target_class, target_method = symbol_name.split(".", 1)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if (symbol_kind in {None, "class"}) and node.name == symbol_name:
                return int(node.lineno), int(node.end_lineno or node.lineno)
            if target_class and node.name == target_class:
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == target_method:
                        return int(child.lineno), int(child.end_lineno or child.lineno)
        if isinstance(node, ast.FunctionDef) and node.name == symbol_name and symbol_kind in {None, "function", "method"}:
            return int(node.lineno), int(node.end_lineno or node.lineno)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == symbol_name and symbol_kind in {None, "async_function", "async_method", "function"}:
            return int(node.lineno), int(node.end_lineno or node.lineno)
    return None


def _locate_script_symbol_range(text: str, symbol_name: str, symbol_kind: Optional[str]) -> Optional[tuple[int, int]]:
    lines = text.splitlines()
    patterns = scan_frontend_symbols(text)
    for lineno, line in enumerate(lines, start=1):
        for pattern, kind in patterns:
            match = pattern.match(line)
            if not match or match.group(1) != symbol_name:
                continue
            if symbol_kind and symbol_kind not in {kind, kind.removeprefix("exported_")}: 
                continue
            return lineno, _find_script_block_end(lines, lineno - 1)
    return None


def _find_script_block_end(lines: list[str], start_index: int) -> int:
    brace_balance = 0
    saw_brace = False
    for index in range(start_index, len(lines)):
        line = lines[index]
        brace_balance += line.count("{")
        if line.count("{"):
            saw_brace = True
        brace_balance -= line.count("}")
        if saw_brace and brace_balance <= 0:
            return index + 1
        if not saw_brace and line.rstrip().endswith(";"):
            return index + 1
    return len(lines)


def _find_indented_block_end(lines: list[str], start_index: int, base_indent: int) -> int:
    end = start_index + 1
    for index in range(start_index + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            end = index + 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= base_indent:
            break
        end = index + 1
    return end


def extract_python_imports(text: str) -> list[dict[str, Any]]:
    imports: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        import_match = re.match(r"^import\s+(.+)$", stripped)
        if import_match:
            modules = [part.strip() for part in import_match.group(1).split(",") if part.strip()]
            for module in modules:
                imports.append({"line": lineno, "module": module.split(" as ", 1)[0].strip(), "kind": "import"})
            continue
        from_match = re.match(r"^from\s+([A-Za-z0-9_\.]+)\s+import\s+(.+)$", stripped)
        if from_match:
            names = [part.strip() for part in from_match.group(2).split(",") if part.strip()]
            imports.append({"line": lineno, "module": from_match.group(1), "names": names, "kind": "from_import"})
    return imports


def extract_frontend_imports(text: str) -> list[dict[str, Any]]:
    imports: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        import_match = re.match(r"^import\s+(.+?)\s+from\s+[\"']([^\"']+)[\"']", stripped)
        if import_match:
            imports.append({"line": lineno, "module": import_match.group(2), "binding": import_match.group(1).strip(), "kind": "import"})
            continue
        bare_import_match = re.match(r"^import\s+[\"']([^\"']+)[\"']", stripped)
        if bare_import_match:
            imports.append({"line": lineno, "module": bare_import_match.group(1), "kind": "side_effect_import"})
    return imports


def collect_symbols_for_file(path: Path, text: str) -> list[SymbolRecord]:
    language = infer_language(path)
    if language == "python":
        return _collect_python_symbols(text)
    if language in {"javascript", "typescript"}:
        return _collect_script_symbols(text, language)
    return []


def extract_dependencies_for_file(path: Path, text: str, symbols: list[SymbolRecord]) -> dict[str, Any]:
    language = infer_language(path)
    if language == "python":
        imports = extract_python_imports(text)
    elif language in {"javascript", "typescript"}:
        imports = extract_frontend_imports(text)
    else:
        imports = []
    exports = extract_exports_for_file(path, text, symbols)
    return {"imports": imports, "exports": exports}


def extract_exports_for_file(path: Path, text: str, symbols: list[SymbolRecord]) -> list[dict[str, Any]]:
    language = infer_language(path)
    exports: list[dict[str, Any]] = []
    if language == "python":
        all_match = re.search(r"^__all__\s*=\s*\[(.*?)\]", text, flags=re.MULTILINE | re.DOTALL)
        if all_match:
            names = [part.strip().strip("\"'") for part in all_match.group(1).split(",") if part.strip()]
            for name in names:
                exports.append({"name": name, "kind": "explicit_export"})
        else:
            for symbol in symbols:
                name = str(symbol.get("name", ""))
                if name and not name.startswith("_") and str(symbol.get("kind", "")) in {"class", "function", "async_function"}:
                    exports.append({"name": name, "kind": "implicit_export"})
        return exports
    if language in {"javascript", "typescript"}:
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            default_match = re.match(r"^export\s+default\s+(?:function\s+)?([A-Za-z_][A-Za-z0-9_]*)?", stripped)
            if default_match:
                exports.append({"line": lineno, "name": default_match.group(1) or "default", "kind": "default_export"})
                continue
            named_match = re.match(r"^export\s+\{(.+)\}", stripped)
            if named_match:
                names = [part.strip().split(" as ", 1)[-1].strip() for part in named_match.group(1).split(",") if part.strip()]
                for name in names:
                    exports.append({"line": lineno, "name": name, "kind": "named_export"})
        for symbol in symbols:
            if str(symbol.get("kind", "")).startswith("exported_"):
                exports.append({"line": symbol.get("line"), "name": symbol.get("name"), "kind": symbol.get("kind")})
    return exports


def locate_symbol_range(path: Path, text: str, symbol_name: str, symbol_kind: Optional[str] = None) -> Optional[tuple[int, int]]:
    language = infer_language(path)
    if language == "python":
        return _locate_python_symbol_range(text, symbol_name, symbol_kind)
    if language in {"javascript", "typescript"}:
        return _locate_script_symbol_range(text, symbol_name, symbol_kind)
    return None


def constants_for_file(path: Path, text: str) -> list[dict[str, Any]]:
    language = infer_language(path)
    constants: list[dict[str, Any]] = []
    if language == "python":
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = re.match(r"^([A-Z][A-Z0-9_]+)\s*=\s*(.+)$", line.strip())
            if match:
                constants.append({"name": match.group(1), "line": lineno, "preview": match.group(0)[:200]})
    elif language in {"javascript", "typescript"}:
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = re.match(r"^(?:export\s+)?const\s+([A-Z][A-Z0-9_]+)\b", line.strip())
            if match:
                constants.append({"name": match.group(1), "line": lineno, "preview": line.strip()[:200]})
    return constants


def sections_for_file(path: Path, text: str) -> list[dict[str, Any]]:
    language = infer_language(path)
    sections: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if language == "markdown" and stripped.startswith("#"):
            sections.append({"line": lineno, "label": stripped.lstrip("# "), "kind": "heading"})
        elif stripped.startswith("# ") or stripped.startswith("// ") or stripped.startswith("/*"):
            if len(stripped) > 4:
                sections.append({"line": lineno, "label": stripped[:120], "kind": "comment_section"})
    return sections[:50]


def _collect_python_symbols(text: str) -> list[SymbolRecord]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _collect_python_symbols_fallback(text)
    symbols: list[SymbolRecord] = []
    for node in tree.body:
        _append_python_symbol(symbols, node, parent=None)
    return symbols


def _append_python_symbol(symbols: list[SymbolRecord], node: ast.AST, parent: Optional[str]) -> None:
    if isinstance(node, ast.ClassDef):
        symbols.append({
            "name": node.name,
            "kind": "class",
            "line": int(node.lineno),
            "end_line": int(node.end_lineno or node.lineno),
            "signature": f"class {node.name}",
            "language": "python",
            **({"parent": parent} if parent else {}),
        })
        for child in node.body:
            _append_python_symbol(symbols, child, parent=node.name)
    elif isinstance(node, ast.FunctionDef):
        kind = "method" if parent else "function"
        record: SymbolRecord = {
            "name": node.name,
            "kind": kind,
            "line": int(node.lineno),
            "end_line": int(node.end_lineno or node.lineno),
            "signature": f"def {node.name}{ast.unparse(node.args) if hasattr(ast, 'unparse') else ''}",
            "language": "python",
        }
        if parent:
            record["parent"] = parent
            record["qualified_name"] = f"{parent}.{node.name}"
        symbols.append(record)
    elif isinstance(node, ast.AsyncFunctionDef):
        kind = "async_method" if parent else "async_function"
        record = {
            "name": node.name,
            "kind": kind,
            "line": int(node.lineno),
            "end_line": int(node.end_lineno or node.lineno),
            "signature": f"async def {node.name}",
            "language": "python",
        }
        if parent:
            record["parent"] = parent
            record["qualified_name"] = f"{parent}.{node.name}"
        symbols.append(record)
    elif isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name):
                symbols.append({
                    "name": target.id,
                    "kind": "constant" if target.id.isupper() else "variable",
                    "line": int(node.lineno),
                    "end_line": int(node.end_lineno or node.lineno),
                    "signature": target.id,
                    "language": "python",
                })


def _collect_python_symbols_fallback(text: str) -> list[SymbolRecord]:
    patterns = scan_python_symbols(text)
    symbols: list[SymbolRecord] = []
    class_stack: list[tuple[int, str]] = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue
        indent = len(line) - len(line.lstrip(" "))
        while class_stack and indent <= class_stack[-1][0]:
            class_stack.pop()
        for pattern, kind in patterns:
            match = pattern.match(line)
            if not match:
                continue
            name = match.group(1)
            record: SymbolRecord = {
                "name": name,
                "kind": kind,
                "line": lineno,
                "end_line": _find_indented_block_end(lines, lineno - 1, indent),
                "signature": stripped,
                "language": "python",
            }
            if kind == "class":
                class_stack.append((indent, name))
            elif class_stack:
                record["parent"] = class_stack[-1][1]
                record["qualified_name"] = f"{class_stack[-1][1]}.{name}"
                record["kind"] = "method" if kind == "function" else "async_method"
            symbols.append(record)
            break
    return symbols


def _collect_script_symbols(text: str, language: str) -> list[SymbolRecord]:
    patterns = scan_frontend_symbols(text)
    symbols: list[SymbolRecord] = []
    lines = text.splitlines()
    for lineno, line in enumerate(lines, start=1):
        for pattern, kind in patterns:
            match = pattern.match(line)
            if not match:
                continue
            end_line = _find_script_block_end(lines, lineno - 1)
            symbols.append({
                "name": match.group(1),
                "kind": kind,
                "line": lineno,
                "end_line": end_line,
                "signature": line.strip(),
                "language": language,
            })
            break
    return symbols


def _locate_python_symbol_range(text: str, symbol_name: str, symbol_kind: Optional[str]) -> Optional[tuple[int, int]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    target_class = None
    target_method = None
    if symbol_kind in {"method", "async_method"} and "." in symbol_name:
        target_class, target_method = symbol_name.split(".", 1)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if (symbol_kind in {None, "class"}) and node.name == symbol_name:
                return int(node.lineno), int(node.end_lineno or node.lineno)
            if target_class and node.name == target_class:
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == target_method:
                        return int(child.lineno), int(child.end_lineno or child.lineno)
        if isinstance(node, ast.FunctionDef) and node.name == symbol_name and symbol_kind in {None, "function", "method"}:
            return int(node.lineno), int(node.end_lineno or node.lineno)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == symbol_name and symbol_kind in {None, "async_function", "async_method", "function"}:
            return int(node.lineno), int(node.end_lineno or node.lineno)
    return None


def _locate_script_symbol_range(text: str, symbol_name: str, symbol_kind: Optional[str]) -> Optional[tuple[int, int]]:
    lines = text.splitlines()
    patterns = scan_frontend_symbols(text)
    for lineno, line in enumerate(lines, start=1):
        for pattern, kind in patterns:
            match = pattern.match(line)
            if not match or match.group(1) != symbol_name:
                continue
            if symbol_kind and symbol_kind not in {kind, kind.removeprefix("exported_")}: 
                continue
            return lineno, _find_script_block_end(lines, lineno - 1)
    return None


def _find_script_block_end(lines: list[str], start_index: int) -> int:
    brace_balance = 0
    saw_brace = False
    for index in range(start_index, len(lines)):
        line = lines[index]
        brace_balance += line.count("{")
        if line.count("{"):
            saw_brace = True
        brace_balance -= line.count("}")
        if saw_brace and brace_balance <= 0:
            return index + 1
        if not saw_brace and line.rstrip().endswith(";"):
            return index + 1
    return len(lines)


def _find_indented_block_end(lines: list[str], start_index: int, base_indent: int) -> int:
    end = start_index + 1
    for index in range(start_index + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            end = index + 1
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= base_indent:
            break
        end = index + 1
    return end