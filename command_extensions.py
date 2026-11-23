"""Compact commands for bounded inspection and coordinated structural edits."""

from __future__ import annotations

import json
import shlex


EXTENSION_VERBS = frozenset({"inspect-file", "read-symbol", "references", "read-context", "edit-symbol", "edit-batch", "find-regex", "replace-regex"})

ADVANCED_COMMAND_GRAMMAR = '''
STRUCTURED INSPECTION (read-only, bounded host discovery actions):
  inspect-file <path> [limit=80]       Imports, exports, symbols, sections, and file_hash
  read-symbol <path> <name> [kind=...] [limit=200] [offset=0]
                                     Read a declaration; next_offset pages large symbols
  references <name> [path=/repo/src] [limit=40]  Token references with definition markers
  read-context <path> "literal anchor" [before=8] [after=12] [limit=8]
                                     Merge matching context windows; return file_hash
  Prefer a symbol or anchor over repeatedly shifting overlapping line ranges.

STRUCTURED EDITS (real mutation dispatch):
  edit-symbol <path> <name> [kind=...] [hash=<file_hash>] <<<
    complete replacement declaration
  >>>
  edit-batch {"atomic":true,"operations":[{"type":"replace_snippet","path":"src/App.tsx","old_text":"exact source","new_text":"replacement","expected_hash":"..."}]}
  edit-batch dispatches operations in order and defaults to atomic rollback on failure. A batch that edits TSX checks the final staged syntax before publishing.
  Numeric replace_range/delete_range use the batch-start source by default. Supply expected_text for a later range based on text produced by an earlier operation, or use replace_snippet.
  Operations also support insert_before/insert_after with anchor_text/new_text,
  replace_symbol, create_file, and the existing `mutate <json-tool-action>` schema.
  Python symbol edits use AST; JS/TS/JSX/TSX edits require the local TypeScript parser.
  Ambiguous, stale-hash, or unparseable symbol edits fail without changing source.
  Use `batch start` / `batch end` to stage related commands across turns. Read the staged source when a preceding edit changed the range you intend to replace.
'''

ADVANCED_COMMAND_GRAMMAR += r'''
REGEX FIND / REPLACE (Python regex, JSON preserves backslashes and newlines):
  find-regex /repo/src/App.tsx {"pattern":"\\bcomposerMode\\b","flags":"m","limit":20}
  find-regex /repo/file.ts {"pattern":"(?P<name>oldName)","replacement":"newName","flags":"m"}
                                     Read-only matches/captures, line windows, file_hash, optional diff preview
  replace-regex /repo/file.ts {"pattern":"\\boldName\\b","replacement":"newName","expected_matches":3,"expected_hash":"<file_hash>"}
                                     Apply only if the exact match count and optional hash agree
  flags: i (ignore case), m (line anchors), s (dot matches newline), x (verbose), a (ASCII).
  Replacement capture references use Python syntax: \\1 or \\g<name> in JSON, not $1.
  Find supports offset/limit paging and before/after context. Each command targets one file.
  replace-regex applies to every match in the file. Use the exact match_count from find-regex as expected_matches for a replace-all; the default is 1.
  Prefer previewing with find-regex and reuse its file_hash as expected_hash.
  replace_regex is also an edit-batch operation; atomic rollback and normal verification apply.
  Evaluation is limited to 2 seconds, 2 MiB source, 10000 matches, and 4 MiB output.
'''


def extension_action(verb: str, rest: str) -> dict:
    if verb in {"find-regex", "replace-regex"}:
        raw = rest.lstrip()
        if raw.startswith('"'):
            path, end = json.JSONDecoder().raw_decode(raw)
            payload = json.loads(raw[end:].lstrip())
        else:
            path, encoded = raw.split(None, 1)
            payload = json.loads(encoded)
        if not isinstance(path, str) or not path or not isinstance(payload, dict):
            raise ValueError(f"Usage: {verb} <path> {{\"pattern\":\"...\"}}")
        allowed = {"pattern", "replacement", "flags"} | ({"limit", "offset", "before", "after"} if verb == "find-regex" else {"expected_matches", "expected_hash"})
        if set(payload) - allowed or "pattern" not in payload or (verb == "replace-regex" and "replacement" not in payload):
            raise ValueError(f"{verb} requires pattern and supported JSON options; replacement is required for edits")
        return {"type": verb.replace("-", "_"), "path": path, **payload}
    if verb == "edit-batch":
        payload = json.loads(rest)
        if not isinstance(payload, dict) or not isinstance(payload.get("operations"), list):
            raise ValueError('Usage: edit-batch {"atomic":true,"operations":[...]}')
        if set(payload) - {"atomic", "operations"}:
            raise ValueError("edit-batch accepts only atomic and operations")
        if not payload["operations"]:
            raise ValueError("edit-batch requires at least one operation")
        return {"type": "batch_mutate", "atomic": payload.get("atomic", True), "operations": payload["operations"]}
    if verb == "edit-symbol":
        header, separator, content = rest.partition("\n")
        if not separator:
            raise ValueError("Usage: edit-symbol <path> <symbol> [kind=...] [hash=...] <<< followed by replacement source and >>>")
        args, options = _arguments(header, 2, {"kind", "hash"})
        return {"type": "replace_symbol", "path": args[0], "symbol_name": args[1],
                "symbol_kind": options.get("kind", ""), "new_text": content,
                **({"expected_hash": options["hash"]} if "hash" in options else {})}
    count = 2 if verb in {"read-symbol", "read-context"} else 1
    allowed = {
        "inspect-file": {"limit"}, "read-symbol": {"kind", "limit", "offset"},
        "references": {"path", "limit"}, "read-context": {"before", "after", "limit"},
    }[verb]
    args, options = _arguments(rest, count, allowed)
    for key in {"before", "after", "limit", "offset"} & options.keys():
        options[key] = int(options[key])
    if verb == "references":
        return {"type": "find_symbol_references", "symbol_name": args[0], "limit": 40, **options}
    action = {"type": verb.replace("-", "_"), "path": args[0], **options}
    if verb == "read-symbol":
        action["symbol_name"] = args[1]
        if "kind" in action:
            action["symbol_kind"] = action.pop("kind")
    elif verb == "read-context":
        action["query"] = args[1]
    return action


def _arguments(rest: str, count: int, allowed: set[str]) -> tuple[list[str], dict]:
    tokens = shlex.split(rest)
    if len(tokens) < count:
        raise ValueError(f"Expected {count} positional argument(s), then named options")
    options = {}
    for token in tokens[count:]:
        key, separator, value = token.partition("=")
        if not separator or key not in allowed or key in options:
            raise ValueError(f"Unexpected or duplicate option: {token}")
        options[key] = value
    return tokens[:count], options
