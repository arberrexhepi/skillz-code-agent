"""Bounded Python regex evaluation in a text-only subprocess; never writes files."""

from __future__ import annotations

import bisect
import difflib
import hashlib
import itertools
import json
from pathlib import Path
import re
import subprocess
import sys

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_CHARS = 4 * 1024 * 1024
MAX_MATCHES = 10000
REGEX_TIMEOUT_SECONDS = 2
_FLAGS = {"i": re.I, "m": re.M, "s": re.S, "x": re.X, "a": re.A}


def read_source(path: Path) -> str:
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Regex operations support files up to 2 MiB")
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_BYTES:
        raise ValueError("Regex operations support files up to 2 MiB")
    return raw.decode("utf-8")


def source_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evaluate_regex(text: str, **options) -> dict:
    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        return {"ok": False, "reason": "file_too_large", "error": "Regex operations support files up to 2 MiB"}
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve())],
                                input=json.dumps({"text": text, **options}), capture_output=True,
                                text=True, encoding="utf-8", timeout=REGEX_TIMEOUT_SECONDS)
        if result.returncode:
            return {"ok": False, "reason": "regex_worker_failed", "error": "Regex evaluation failed; no changes made"}
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "regex_timeout", "error": f"Regex evaluation exceeded {REGEX_TIMEOUT_SECONDS}s; no changes made"}


def _evaluate(text: str, pattern: str, *, replacement: str | None = None, flags: str = "", limit: int = 20,
              offset: int = 0, before: int = 2, after: int = 2, expected_matches: int | None = None,
              file_path: str = "source") -> dict:
    if not isinstance(pattern, str) or not pattern or len(pattern) > 4096:
        raise ValueError("pattern must be a nonempty string of at most 4096 characters")
    if not isinstance(flags, str) or set(flags) - _FLAGS.keys():
        raise ValueError("Supported regex flags: i, m, s, x, a (Python syntax)")
    if replacement is not None and (not isinstance(replacement, str) or len(replacement) > 4096):
        raise ValueError("replacement must be a string of at most 4096 characters")
    if expected_matches is not None and (type(expected_matches) is not int or expected_matches < 1):
        raise ValueError("expected_matches must be a positive integer")
    for name, value, maximum in (("limit", limit, 200), ("offset", offset, MAX_MATCHES), ("before", before, 20), ("after", after, 20)):
        if type(value) is not int or value < (1 if name == "limit" else 0) or value > maximum:
            raise ValueError(f"{name} is outside its supported range")
    mask = 0
    for flag in flags:
        mask |= _FLAGS[flag]
    regex = re.compile(pattern, mask)
    if replacement is not None:
        regex.sub(replacement, "", count=1)  # Validate capture references even when the file has no matches.
    found = list(itertools.islice(regex.finditer(text), MAX_MATCHES + 1))
    if len(found) > MAX_MATCHES:
        return {"ok": False, "reason": "too_many_matches", "error": "More than 10000 matches; narrow the pattern"}
    count = len(found)
    if expected_matches is not None and count != expected_matches:
        return {"ok": False, "reason": "occurrence_mismatch", "match_count": count,
                "error": f"Expected {expected_matches} match(es), found {count}; no changes made"}
    starts = [0] + [index + 1 for index, character in enumerate(text) if character == "\n"]
    lines = text.split("\n")
    matches, blocks = [], []
    for match in found[offset:offset + limit]:
        start_line = bisect.bisect_right(starts, match.start())
        end_line = bisect.bisect_right(starts, max(match.start(), match.end() - 1))
        matches.append({"start": match.start(), "end": match.end(), "start_line": start_line, "end_line": end_line,
                        "column": match.start() - starts[start_line - 1] + 1,
                        "text": match.group()[:600], "text_truncated": len(match.group()) > 600,
                        "groups": [(value[:200] if value is not None else None) for value in match.groups()[:8]],
                        "named_groups": {key: value[:200] if value is not None else None for key, value in list(match.groupdict().items())[:8]}})
        begin, end = max(0, start_line - 1 - before), min(len(lines), end_line + after)
        # Skip an oversized source line rather than misrepresenting partial code as inspected.
        selected, size = [], 0
        for line in lines[begin:end]:
            if size + len(line) + 1 > 4000:
                break
            selected.append(line.rstrip("\r")); size += len(line) + 1
        if selected:
            block = {"start_line": begin + 1, "end_line": begin + len(selected), "content": "\n".join(selected)}
            if block not in blocks:
                blocks.append(block)
    response = {"ok": True, "match_count": count, "matches": matches, "blocks": blocks,
                "truncated": offset + len(matches) < count,
                "next_offset": offset + len(matches) if offset + len(matches) < count else None}
    if replacement is not None:
        pieces, cursor, size = [], 0, 0
        for match in found:
            if match.end() - match.start() > 65536:
                raise ValueError("Replacement match exceeds 65536 characters; narrow the edit surface")
            expanded = match.expand(replacement)
            size += match.start() - cursor + len(expanded)
            if size + len(text) - match.end() > MAX_OUTPUT_CHARS:
                raise ValueError("Replacement output exceeds 4 MiB")
            pieces.extend((text[cursor:match.start()], expanded)); cursor = match.end()
        pieces.append(text[cursor:])
        updated = "".join(pieces)
        diff_lines = list(difflib.unified_diff(text.splitlines(keepends=True), updated.splitlines(keepends=True),
                                             fromfile="a/" + file_path, tofile="b/" + file_path))
        changed = sum(index >= 2 and line.startswith(("+", "-")) for index, line in enumerate(diff_lines))
        diff = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff_lines)
        response.update(updated=updated, diff=diff[:6000], diff_truncated=len(diff) > 6000, changed_line_count=changed)
    # Page complete match/context records; never emit a broken JSON preview.
    while len(json.dumps({key: value for key, value in response.items() if key != "updated"})) > 24000 and response["matches"]:
        response["matches"].pop()
        response["blocks"] = []
        response["truncated"] = True
        response["next_offset"] = offset + len(response["matches"])
    return response


if __name__ == "__main__":
    try:
        print(json.dumps(_evaluate(**json.load(sys.stdin)), ensure_ascii=False))
    except (ValueError, TypeError, re.error, IndexError) as error:
        print(json.dumps({"ok": False, "reason": "invalid_regex_request", "error": str(error)}))
