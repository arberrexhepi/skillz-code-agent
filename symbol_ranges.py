"""Exact declaration spans for edits; uncertain or ambiguous targets fail closed."""

from __future__ import annotations

import ast
from contextlib import contextmanager
from contextvars import ContextVar
import json
import subprocess
from pathlib import Path


_COMPILER_ROOT: ContextVar[Path | None] = ContextVar("compiler_root", default=None)


@contextmanager
def typescript_lookup_root(root: Path):
    token = _COMPILER_ROOT.set(root)
    try:
        yield
    finally:
        _COMPILER_ROOT.reset(token)


_TYPESCRIPT_RANGE = r"""
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const p = JSON.parse(fs.readFileSync(0, 'utf8'));
async function main() {
const searchPaths = [p.root, p.compilerRoot, p.harness];
let ts = require(require.resolve('typescript', {paths: searchPaths}));
let sf, api, snapshot, diagnostics;
try {
if (ts.createSourceFile) {
  sf = ts.createSourceFile(p.path, p.text, ts.ScriptTarget.Latest, true);
  diagnostics = sf.parseDiagnostics;
} else {
  // TypeScript 7 exposes syntax trees through its native compiler API.
  const resolve = name => pathToFileURL(require.resolve(name, {paths: searchPaths})).href;
  ts = await import(resolve('typescript/unstable/ast'));
  const {API} = await import(resolve('typescript/unstable/sync'));
  const file = path.resolve(p.root, p.path);
  api = new API({cwd: p.root, fs: {readFile: name => path.resolve(name) === file ? p.text : undefined,
                                  fileExists: name => path.resolve(name) === file ? true : undefined}});
  snapshot = api.updateSnapshot({openFiles: [file]});
  const project = snapshot.getDefaultProjectForFile(file);
  sf = project && project.program.getSourceFile(file);
  if (!sf) throw new Error('TypeScript did not load the target file');
  diagnostics = project.program.getSyntacticDiagnostics(file);
}
if (p.name === undefined) {
  const findings = diagnostics.slice(0, 8).map(d => {
    const pos = sf.getLineAndCharacterOfPosition(d.start ?? d.pos ?? 0);
    return {line: pos.line + 1, column: pos.character + 1, code: `TS${d.code}`,
            message: d.text ?? ts.flattenDiagnosticMessageText(d.messageText, ' ')};
  });
  process.stdout.write(JSON.stringify(findings));
  return;
}
if (diagnostics.length) { process.stdout.write('null'); return; }
const hits = [];
function walk(node, parents = []) {
  let name = node.name && node.name.getText(sf);
  let kind = '';
  let target = node;
  if (ts.isFunctionDeclaration(node)) kind = 'function';
  else if (ts.isClassDeclaration(node)) kind = 'class';
  else if (ts.isInterfaceDeclaration(node)) kind = 'interface';
  else if (ts.isTypeAliasDeclaration(node)) kind = 'type';
  else if (ts.isEnumDeclaration(node)) kind = 'enum';
  else if (ts.isMethodDeclaration(node)) kind = 'method';
  else if (ts.isVariableDeclaration(node)) {
    kind = 'variable';
    if (node.parent.declarations.length !== 1 || !ts.isVariableStatement(node.parent.parent)) name = '';
    else target = node.parent.parent;
  }
  const qualified = [...parents, name].filter(Boolean).join('.');
  const acceptedKind = !p.kind || p.kind === kind ||
    (kind === 'variable' && ['constant', 'function', 'component'].includes(p.kind));
  if (name && kind && acceptedKind && (p.name === name || p.name === qualified)) {
    const start = target.getStart(sf), end = target.getEnd();
    // Replacing whole lines must not consume a neighboring declaration.
    const lineStart = p.text.lastIndexOf('\n', start - 1) + 1;
    const nextBreak = p.text.indexOf('\n', end);
    const tail = p.text.slice(end, nextBreak < 0 ? p.text.length : nextBreak).trim();
    if (!p.text.slice(lineStart, start).trim() && !tail) {
      hits.push([sf.getLineAndCharacterOfPosition(start).line + 1,
                 sf.getLineAndCharacterOfPosition(end - 1).line + 1]);
    }
  }
  const next = name && ['class','function','method'].includes(kind) ? [...parents, name] : parents;
  node.forEachChild(child => walk(child, next));
}
walk(sf);
process.stdout.write(JSON.stringify(hits.length === 1 ? hits[0] : null));
} finally { if (snapshot) snapshot.dispose(); if (api) api.close(); }
}
main().catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
"""


def _typescript_request(path: Path, text: str, root: Path, **parameters):
    result = subprocess.run(["node", "-e", _TYPESCRIPT_RANGE], input=json.dumps({
        "path": str(path), "text": text, "root": str(root), "compilerRoot": str(_COMPILER_ROOT.get() or root),
        "harness": str(Path(__file__).parent / "desktop"), **parameters,
    }), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    if result.returncode != 0:
        raise ValueError(result.stderr.splitlines()[0] if result.stderr else "TypeScript parser failed")
    return json.loads(result.stdout)


def typescript_syntax_errors(path: Path, text: str, *, root: Path) -> list[dict]:
    """Parse proposed text without resolving imports or type-checking the project."""
    try:
        return _typescript_request(path, text, root)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"TypeScript syntax check unavailable for {path.relative_to(root)}. Install the project's TypeScript dependencies. {exc}") from exc


def exact_symbol_range(path: Path, text: str, name: str, kind: str = "", *, root: Path) -> tuple[int, int] | None:
    if path.suffix in {".py", ".pyi"}:
        try:
            tree = ast.parse(text)
        except SyntaxError:
            return None
        hits = []

        def walk(node: ast.AST, parents: tuple[str, ...] = ()) -> None:
            for child in ast.iter_child_nodes(node):
                child_name = getattr(child, "name", "")
                child_kind = "class" if isinstance(child, ast.ClassDef) else "function" if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else ""
                target = child.target if isinstance(child, ast.AnnAssign) else child.targets[0] if isinstance(child, ast.Assign) and len(child.targets) == 1 else None
                if isinstance(target, ast.Name):
                    child_name, child_kind = target.id, "variable"
                if child_kind:
                    qualified = ".".join((*parents, child_name))
                    normalized_kind = kind.removeprefix("async_")
                    accepted_kind = not kind or normalized_kind == child_kind or (normalized_kind == "method" and child_kind == "function") or (kind == "constant" and child_kind == "variable")
                    if name in {child_name, qualified} and accepted_kind:
                        start = min([child.lineno, *(item.lineno for item in getattr(child, "decorator_list", []))])
                        end = child.end_lineno or child.lineno
                        # Avoid deleting a second statement on the same line.
                        tail = text.splitlines()[end - 1][child.end_col_offset:].strip()
                        if not tail or tail.startswith("#"):
                            hits.append((start, end))
                    walk(child, (*parents, child_name))
                else:
                    walk(child, parents)

        walk(tree)
        return hits[0] if len(hits) == 1 else None
    if path.suffix.lower() not in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return None
    try:
        span = _typescript_request(path, text, root, name=name, kind=kind)
        return tuple(span) if isinstance(span, list) and len(span) == 2 else None
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
