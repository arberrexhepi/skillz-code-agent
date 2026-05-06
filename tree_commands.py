"""
TreeCommandParser — Compact command grammar for the agent.

Instead of emitting verbose JSON action blobs, the agent can use
short tree-native commands:

READ (free, in-context — no tool call):
  ls /repo/src                     List directory
  ls /repo/src depth=3             List with depth
  cat /repo/src/main.py            Read full file
  cat /repo/src/main.py:100-200    Read line range
  read-line-range /repo/src/main.py 100-200  Read line range with numbering
  symbols /repo/src/main.py        List functions/classes/variables in a file
  find-symbol /repo/src useTodo    Find a symbol by name in a file or directory
  stat /repo/src/main.py           File metadata
  find /repo *.py                  Glob search
  grep /facts "pattern"            Search content
  grep /repo/src "TODO"            Search loaded files
  read-diagnostics [path]          Ingest a local diagnostics snapshot
    diagnose <path> [limit=N]        Run backend diagnostics for one file and ingest issues
  run-route-check <route-or-url> [base=<url>]  Visit a route with Playwright and ingest runtime errors
  list-run-issues                  List parsed run diagnostic issues
  show-run-issue <id>              Show one parsed run diagnostic issue
  list-issues                      Compatibility alias: list run + durable issues
  show-issue <id>                  Compatibility alias: show run or durable issue

WRITE (real tool dispatch):
  write <path> <content>           Write full file
  replace-lines <path>:10-20 <content>  Replace a bounded line range
  patch <path> <search> -> <replace>  Search/replace edit
    npm <args>                       Run a package-manager dependency command behind operator approval
    show-diff [path]                 Show git diff for the workspace or one path
    review-changes [path] [limit=N]  Review changed files and surface risk summary
  shell <command>                  Run shell command
  git status                       Git status
  git diff                         Git diff
  git add <path>                   Stage file
  git commit <message>             Commit

TREE MUTATIONS (in-process):
  fact <issue>/<type>/<key> <value>   Set/update fact
  ingest-log <path>                Parse a log file into /facts/log-issues
  run-check <build|lint|typecheck|test>  Run a finite project check and ingest issues
  expand <step_or_id>              Expand history/memory
  drop                             Clear active context
  batch start                      Begin edit batch
  batch end                        End edit batch
    approve-npm                      Approve the pending npm command
    reject-npm                       Reject the pending npm command
  resolve-run-issue <id>           Mark a parsed run issue resolved
  reopen-run-issue <id>            Mark a parsed run issue open again
  finish [message]                 Signal completion

SKILLS (preloaded cache or handler):
  skill <name> [args]              Invoke a registered skill
"""

from __future__ import annotations

import ast
import json
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from context_tree import ContextTree
from issue_facts import (
    format_issue_not_found,
    format_issue_summary_detail,
    format_issue_summary_list,
    issue_summaries_from_payload,
)


# ---------------------------------------------------------------------------
# Command result
# ---------------------------------------------------------------------------

@dataclass
class CommandResult:
    ok: bool
    output: str
    command_type: str  # "read" | "write" | "mutation" | "skill" | "error" | "finish" | "annotation"
    needs_tool: bool = False  # True if this must dispatch to ToolbeltRunner
    tool_action: Optional[Dict[str, Any]] = None  # Translated action dict for ToolbeltRunner

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"ok": self.ok, "output": self.output, "command_type": self.command_type}
        if self.tool_action:
            d["tool_action"] = self.tool_action
        return d


# ---------------------------------------------------------------------------
# Annotations — structured feed-forward metadata (>>tag: content)
# ---------------------------------------------------------------------------
#
# The model emits >>tag: lines to annotate its output with structured metadata.
# These are NOT commands — they're captured, stored in the turn, and fed back
# in history so the model (and the human) can see the reasoning chain.
#
# Tags:
#   >>th: <reasoning>       Thought — why the model is doing what it's doing
#   >>dg: <condition>       Delegation — conditional: "if output X, then do Y"
#   >>pl: <plan>            Plan — what the model intends to do next
#   >>q:  <question>        Question — something the model needs clarified
#   >>err: <problem>        Error note — something went wrong, model's diagnosis
#   >>ju:  <justification>  Justify — why the task needs more turns to complete
#
# Annotations flow forward: they appear in history and inform future turns.
# They cost nothing — no tool call, no tree traversal.

_ANNOTATION_RE = re.compile(r"^>>(\w+):\s*(.*)$")

ANNOTATION_TAGS = {
    "th": "thought",
    "dg": "delegation",
    "pl": "plan",
    "q": "question",
    "err": "error_note",
    "ju": "justify",
}


@dataclass
class Annotation:
    """A structured feed-forward annotation from the model."""
    tag: str        # short tag: th, dg, pl, q, err
    tag_name: str   # expanded: thought, delegation, plan, question, error_note
    content: str    # the annotation body

    def compact(self) -> str:
        return f">>{self.tag}: {self.content}"


def parse_annotation(line: str) -> Optional[Annotation]:
    """Parse a >>tag: line into an Annotation, or None if not an annotation."""
    m = _ANNOTATION_RE.match(line.strip())
    if m is None:
        return None
    tag = m.group(1).lower()
    content = m.group(2).strip()
    tag_name = ANNOTATION_TAGS.get(tag, tag)
    return Annotation(tag=tag, tag_name=tag_name, content=content)


def is_annotation(line: str) -> bool:
    """Quick check if a line is a >>tag: annotation."""
    return line.strip().startswith(">>") and _ANNOTATION_RE.match(line.strip()) is not None


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

_LINE_RANGE_RE = re.compile(r"^(.+):(\d+)-(\d+)$")
_RANGE_ONLY_RE = re.compile(r"^(\d+)-(\d+)$")


class TreeCommandParser:
    """Parse and execute compact tree commands."""

    def __init__(self, tree: ContextTree, get_issue_state: Optional[Callable[[], Dict[str, Any]]] = None) -> None:
        self.tree = tree
        self._get_issue_state = get_issue_state or (lambda: {})

    def _durable_issue_state(self) -> Dict[str, Any]:
        try:
            payload = self._get_issue_state()
        except Exception:
            return {}
        return payload if isinstance(payload, dict) else {}

    def parse_and_execute(self, raw: str) -> CommandResult:
        """Parse a command string and execute it.
        Returns a CommandResult. If needs_tool is True, the caller
        must dispatch tool_action to ToolbeltRunner."""

        raw = raw.strip()
        if not raw:
            return CommandResult(ok=False, output="Empty command", command_type="error")

        # Split on first space to get verb
        parts = raw.split(None, 1)
        verb = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        dispatch = {
            "ls": self._cmd_ls,
            "cat": self._cmd_cat,
            "read-line-range": self._cmd_read_line_range,
            "read_line_range": self._cmd_read_line_range,
            "symbols": self._cmd_symbols,
            "find-symbol": self._cmd_find_symbol,
            "find_symbol": self._cmd_find_symbol,
            "stat": self._cmd_stat,
            "find": self._cmd_find,
            "grep": self._cmd_grep,
            "read-diagnostics": self._cmd_read_diagnostics,
            "diagnose": self._cmd_diagnose,
            "run-route-check": self._cmd_run_route_check,
            "run_route_check": self._cmd_run_route_check,
            "ingest-log": self._cmd_ingest_log,
            "list-run-issues": self._cmd_list_issues,
            "list_run_issues": self._cmd_list_issues,
            "list-issues": self._cmd_list_issues,
            "show-run-issue": self._cmd_show_issue,
            "show_run_issue": self._cmd_show_issue,
            "show-issue": self._cmd_show_issue,
            "resolve-run-issue": self._cmd_resolve_issue,
            "resolve_run_issue": self._cmd_resolve_issue,
            "resolve-issue": self._cmd_resolve_issue,
            "reopen-run-issue": self._cmd_reopen_issue,
            "reopen_run_issue": self._cmd_reopen_issue,
            "reopen-issue": self._cmd_reopen_issue,
            "run-check": self._cmd_run_check,
            "write": self._cmd_write,
            "replace-lines": self._cmd_replace_lines,
            "replace_lines": self._cmd_replace_lines,
            "patch": self._cmd_patch,
            "discover": self._cmd_discover,
            "mutate": self._cmd_mutate,
            "show-diff": self._cmd_show_diff,
            "show_diff": self._cmd_show_diff,
            "review-changes": self._cmd_review_changes,
            "review_changes": self._cmd_review_changes,
            "npm": self._cmd_npm,
            "shell": self._cmd_shell,
            "git": self._cmd_git,
            "fact": self._cmd_fact,
            "expand": self._cmd_expand,
            "drop": self._cmd_drop,
            "batch": self._cmd_batch,
            "approve-npm": self._cmd_approve_npm,
            "reject-npm": self._cmd_reject_npm,
            "finish": self._cmd_finish,
            "skill": self._cmd_skill,
        }

        handler = dispatch.get(verb)
        if handler is None:
            return CommandResult(ok=False, output=f"Unknown command: {verb}", command_type="error")

        try:
            return handler(rest)
        except Exception as exc:
            return CommandResult(ok=False, output=f"Command error: {exc}", command_type="error")

    # ------------------------------------------------------------------
    # READ commands (free, in-context)
    # ------------------------------------------------------------------

    def _cmd_ls(self, rest: str) -> CommandResult:
        parts = rest.split()
        path = parts[0] if parts else "/"
        depth = 1
        for p in parts[1:]:
            if p.startswith("depth="):
                try:
                    depth = int(p.split("=", 1)[1])
                except ValueError:
                    pass

        entries = self.tree.ls(path, depth=depth)
        output = json.dumps(entries, indent=2) if entries else "(empty)"
        return CommandResult(ok=True, output=output, command_type="read")

    def _cmd_cat(self, rest: str) -> CommandResult:
        rest = rest.strip()
        start_line = 0
        end_line = 0

        m = _LINE_RANGE_RE.match(rest)
        if m:
            rest = m.group(1)
            start_line = int(m.group(2))
            end_line = int(m.group(3))

        content = self.tree.cat(rest, start_line=start_line, end_line=end_line)
        return CommandResult(ok=True, output=content, command_type="read")

    def _cmd_read_line_range(self, rest: str) -> CommandResult:
        parts = rest.split(None, 1)
        if len(parts) != 2:
            return CommandResult(
                ok=False,
                output="Usage: read-line-range <path> <start>-<end>",
                command_type="error",
            )
        path = parts[0].strip()
        range_part = parts[1].strip()
        m = _RANGE_ONLY_RE.match(range_part)
        if m is None:
            return CommandResult(
                ok=False,
                output="Usage: read-line-range <path> <start>-<end>",
                command_type="error",
            )
        start_line = int(m.group(1))
        end_line = int(m.group(2))
        content = self.tree.read_line_range(path, start_line, end_line, include_line_numbers=True)
        return CommandResult(ok=True, output=content, command_type="read")

    def _cmd_symbols(self, rest: str) -> CommandResult:
        path = rest.strip()
        if not path:
            return CommandResult(ok=False, output="Usage: symbols <path>", command_type="error")
        symbols = self.tree.extract_symbols(path)
        if not symbols:
            return CommandResult(ok=True, output="(no symbols found)", command_type="read")
        return CommandResult(ok=True, output=json.dumps(symbols, indent=2), command_type="read")

    def _cmd_find_symbol(self, rest: str) -> CommandResult:
        parts = rest.split()
        if len(parts) < 2:
            return CommandResult(
                ok=False,
                output="Usage: find-symbol <path> <symbol_name> [limit=N]",
                command_type="error",
            )
        path = parts[0]
        name = parts[1]
        limit = 20
        for part in parts[2:]:
            if part.startswith("limit="):
                try:
                    limit = int(part.split("=", 1)[1])
                except ValueError:
                    pass
        matches = self.tree.find_symbols(path, name, limit=limit)
        if not matches:
            return CommandResult(ok=True, output="(no symbol matches)", command_type="read")
        return CommandResult(ok=True, output=json.dumps(matches, indent=2), command_type="read")

    def _cmd_stat(self, rest: str) -> CommandResult:
        path = rest.strip() or "/"
        info = self.tree.stat(path)
        return CommandResult(ok=True, output=json.dumps(info, indent=2), command_type="read")

    def _cmd_find(self, rest: str) -> CommandResult:
        parts = rest.split()
        path = parts[0] if parts else "/"
        pattern = parts[1] if len(parts) > 1 else "*"
        limit = 100
        for p in parts[2:]:
            if p.startswith("limit="):
                try:
                    limit = int(p.split("=", 1)[1])
                except ValueError:
                    pass

        results = self.tree.find(path, glob_pattern=pattern, limit=limit)
        output = "\n".join(results) if results else "(no matches)"
        return CommandResult(ok=True, output=output, command_type="read")

    def _cmd_grep(self, rest: str) -> CommandResult:
        # grep /path "pattern" [limit=N]
        parts = rest.split(None, 1)
        path = parts[0] if parts else "/"
        remainder = parts[1] if len(parts) > 1 else ""

        # Extract quoted pattern
        pattern = remainder
        limit = 50
        quote_match = re.match(r'"([^"]*)"(.*)', remainder)
        if quote_match:
            pattern = quote_match.group(1)
            for token in quote_match.group(2).split():
                if token.startswith("limit="):
                    try:
                        limit = int(token.split("=", 1)[1])
                    except ValueError:
                        pass
        else:
            tokens = remainder.split()
            pattern = tokens[0] if tokens else ""
            for t in tokens[1:]:
                if t.startswith("limit="):
                    try:
                        limit = int(t.split("=", 1)[1])
                    except ValueError:
                        pass

        results = self.tree.grep(path, pattern, limit=limit)
        if results:
            lines = [f"  {r['path']}:{r['line']}: {r['text']}" for r in results]
            output = f"{len(results)} matches:\n" + "\n".join(lines)
        else:
            output = "(no matches)"
        return CommandResult(ok=True, output=output, command_type="read")

    def _cmd_list_issues(self, rest: str) -> CommandResult:
        issues = self.tree.list_log_issues()
        durable_state = self._durable_issue_state()
        parts: List[str] = []
        if issues:
            parts.append(self.tree.format_log_issue_list(issues))
        if issue_summaries_from_payload(durable_state):
            parts.append(format_issue_summary_list(durable_state))
        if not parts:
            parts.append("(no run issues and no durable repo_facts issues)")
        return CommandResult(ok=True, output="\n\n".join(parts), command_type="read")

    def _cmd_show_issue(self, rest: str) -> CommandResult:
        issue_id = rest.strip()
        if not issue_id:
            return CommandResult(ok=False, output="Usage: show-run-issue <id>", command_type="error")
        issue = self.tree.show_log_issue(issue_id)
        if issue is None:
            durable_state = self._durable_issue_state()
            for durable_issue in issue_summaries_from_payload(durable_state):
                if str(durable_issue.get("issue_id", "") or "").strip() == issue_id:
                    return CommandResult(ok=True, output=format_issue_summary_detail(durable_issue), command_type="read")
            return CommandResult(ok=True, output=format_issue_not_found(issue_id, durable_state), command_type="read")
        return CommandResult(ok=True, output=self.tree.format_log_issue_detail(issue), command_type="read")

    def _cmd_read_diagnostics(self, rest: str) -> CommandResult:
        path = rest.strip()
        candidates = [path] if path else [
            "/repo/live_trace.md",
            "/repo/diagnostics.json",
            "/repo/.diagnostics.json",
            "/repo/.vscode/diagnostics.json",
            "/repo/ts-errors.json",
            "/repo/tsc-errors.txt",
        ]
        searched: List[str] = []
        for candidate in candidates:
            if not candidate:
                continue
            searched.append(candidate)
            issues = self.tree.ingest_log_issues(candidate)
            if issues:
                return CommandResult(
                    ok=True,
                    output=f"Read diagnostics from {candidate} and ingested {len(issues)} issue(s) into /facts/{self.tree.LOG_ISSUES_ROOT}",
                    command_type="mutation",
                )
        return CommandResult(
            ok=False,
            output="No diagnostics snapshot found or parsed from: " + ", ".join(searched),
            command_type="error",
        )

    def _cmd_diagnose(self, rest: str) -> CommandResult:
        parts = rest.split()
        if not parts:
            return CommandResult(
                ok=False,
                output="Usage: diagnose <path> [limit=N]",
                command_type="error",
            )
        path = parts[0].strip()
        rel_path = path.removeprefix("/repo/").removeprefix("repo/")
        limit = 8
        for token in parts[1:]:
            if token.startswith("limit="):
                try:
                    limit = int(token.split("=", 1)[1])
                except ValueError:
                    pass
        return CommandResult(
            ok=True,
            output=f"[dispatch: diagnose {rel_path}]",
            command_type="write",
            needs_tool=True,
            tool_action={"type": "diagnose", "path": rel_path, "limit": limit},
        )

    # ------------------------------------------------------------------
    # WRITE commands (needs_tool=True, dispatches to ToolbeltRunner)
    # ------------------------------------------------------------------

    def _cmd_ingest_log(self, rest: str) -> CommandResult:
        path = rest.strip()
        if not path:
            return CommandResult(ok=False, output="Usage: ingest-log <path>", command_type="error")
        issues = self.tree.ingest_log_issues(path)
        if not issues:
            return CommandResult(ok=False, output=f"No issues parsed from {path}", command_type="error")
        return CommandResult(
            ok=True,
            output=f"Ingested {len(issues)} issue(s) from {path} into /facts/{self.tree.LOG_ISSUES_ROOT}",
            command_type="mutation",
        )

    def _cmd_run_check(self, rest: str) -> CommandResult:
        kind = rest.strip().lower()
        if kind not in {"build", "lint", "typecheck", "test"}:
            return CommandResult(
                ok=False,
                output="Usage: run-check <build|lint|typecheck|test>",
                command_type="error",
            )
        return CommandResult(
            ok=True,
            output=f"[dispatch: run_check {kind}]",
            command_type="write",
            needs_tool=True,
            tool_action={"type": "run_check", "kind": kind},
        )

    def _cmd_run_route_check(self, rest: str) -> CommandResult:
        parts = rest.split()
        if not parts:
            return CommandResult(
                ok=False,
                output="Usage: run-route-check <route-or-url> [base=<url>]",
                command_type="error",
            )
        target = parts[0].strip()
        base_url = ""
        for token in parts[1:]:
            if token.startswith("base="):
                base_url = token.split("=", 1)[1].strip()
        return CommandResult(
            ok=True,
            output=f"[dispatch: run_route_check {target}]",
            command_type="write",
            needs_tool=True,
            tool_action={"type": "run_route_check", "target": target, "base_url": base_url},
        )

    def _cmd_write(self, rest: str) -> CommandResult:
        # write <path> <content>
        parts = rest.split(None, 1)
        if len(parts) < 2:
            return CommandResult(ok=False, output="Usage: write <path> <content>", command_type="error")
        path, content = parts
        # Strip /repo/ prefix if present for real tool dispatch
        rel_path = path.removeprefix("/repo/").removeprefix("repo/")
        return CommandResult(
            ok=True,
            output=f"[dispatch: write {rel_path}]",
            command_type="write",
            needs_tool=True,
            tool_action={"type": "write_file", "path": rel_path, "content": content},
        )

    def _cmd_replace_lines(self, rest: str) -> CommandResult:
        rest = rest.lstrip()
        if not rest:
            return CommandResult(
                ok=False,
                output="Usage: replace-lines <path>:<start>-<end> <content> or replace-lines <path> <start>-<end> <content>",
                command_type="error",
            )

        match = re.match(
            r"^(?P<path>\S+):(?P<start>\d+)-(?P<end>\d+)(?:\s+(?P<content>.*))?$",
            rest,
            re.DOTALL,
        )
        if match is not None:
            path = match.group("path")
            start_line = int(match.group("start"))
            end_line = int(match.group("end"))
            content = match.group("content") or ""
        else:
            match = re.match(
                r"^(?P<path>\S+)\s+(?P<start>\d+)-(?P<end>\d+)(?:\s+(?P<content>.*))?$",
                rest,
                re.DOTALL,
            )
            if match is not None:
                path = match.group("path")
                start_line = int(match.group("start"))
                end_line = int(match.group("end"))
                content = match.group("content") or ""
            else:
                match = re.match(
                    r"^(?P<path>\S+)\s+(?P<start>\d+)\s+(?P<end>\d+)(?:\s+(?P<content>.*))?$",
                    rest,
                    re.DOTALL,
                )
                if match is None:
                    return CommandResult(
                        ok=False,
                        output="replace-lines requires a <start>-<end> range",
                        command_type="error",
                    )
                path = match.group("path")
                start_line = int(match.group("start"))
                end_line = int(match.group("end"))
                content = match.group("content") or ""

        rel_path = path.removeprefix("/repo/").removeprefix("repo/")
        return CommandResult(
            ok=True,
            output=f"[dispatch: replace-lines {rel_path}:{start_line}-{end_line}]",
            command_type="write",
            needs_tool=True,
            tool_action={
                "type": "replace_lines",
                "path": rel_path,
                "start_line": start_line,
                "end_line": end_line,
                "content": content,
            },
        )

    def _cmd_patch(self, rest: str) -> CommandResult:
        # patch <path> <search> -> <replace>
        # Find the path (first token), then split on ' -> '
        parts = rest.split(None, 1)
        if len(parts) < 2:
            return CommandResult(ok=False, output="Usage: patch <path> <search> -> <replace>", command_type="error")
        path = parts[0]
        remainder = parts[1]

        arrow_split = remainder.split(" -> ", 1)
        if len(arrow_split) < 2:
            return CommandResult(ok=False, output="Missing ' -> ' separator", command_type="error")

        search, replace = arrow_split
        rel_path = path.removeprefix("/repo/").removeprefix("repo/")
        return CommandResult(
            ok=True,
            output=f"[dispatch: patch {rel_path}]",
            command_type="write",
            needs_tool=True,
            tool_action={"type": "patch_file", "path": rel_path, "search": search, "replace": replace},
        )

    def _cmd_mutate(self, rest: str) -> CommandResult:
        raw = rest.strip()
        if not raw:
            return CommandResult(
                ok=False,
                output="Usage: mutate <json-tool-action>",
                command_type="error",
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return CommandResult(
                ok=False,
                output=f"mutate requires valid JSON: {exc}",
                command_type="error",
            )
        if not isinstance(payload, dict):
            return CommandResult(
                ok=False,
                output="mutate requires a JSON object tool action",
                command_type="error",
            )
        action_type = str(payload.get("type", "") or "").strip()
        if not action_type:
            return CommandResult(
                ok=False,
                output="mutate payload requires a non-empty type",
                command_type="error",
            )
        return CommandResult(
            ok=True,
            output=f"[dispatch: mutate {action_type}]",
            command_type="write",
            needs_tool=True,
            tool_action=payload,
        )

    def _cmd_discover(self, rest: str) -> CommandResult:
        raw = rest.strip()
        if not raw:
            return CommandResult(
                ok=False,
                output="Usage: discover <json-tool-action>",
                command_type="error",
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            return CommandResult(
                ok=False,
                output=f"discover requires valid JSON: {exc}",
                command_type="error",
            )
        if not isinstance(payload, dict):
            return CommandResult(
                ok=False,
                output="discover requires a JSON object tool action",
                command_type="error",
            )
        action_type = str(payload.get("type", "") or "").strip()
        if not action_type:
            return CommandResult(
                ok=False,
                output="discover payload requires a non-empty type",
                command_type="error",
            )
        return CommandResult(
            ok=True,
            output=f"[dispatch: discover {action_type}]",
            command_type="read",
            needs_tool=True,
            tool_action=payload,
        )

    def _cmd_shell(self, rest: str) -> CommandResult:
        return CommandResult(
            ok=True,
            output=f"[dispatch: shell]",
            command_type="write",
            needs_tool=True,
            tool_action={"type": "run_shell", "command": rest},
        )

    def _cmd_npm(self, rest: str) -> CommandResult:
        command = rest.strip()
        if not command:
            return CommandResult(ok=False, output="Usage: npm <install|uninstall|ci> [args...]", command_type="error")
        return CommandResult(
            ok=True,
            output="[dispatch: npm_command]",
            command_type="write",
            needs_tool=True,
            tool_action={"type": "npm_command", "command": command, "path": "package.json"},
        )

    def _cmd_approve_npm(self, rest: str) -> CommandResult:
        return CommandResult(
            ok=True,
            output="[dispatch: approve_npm_command]",
            command_type="mutation",
            needs_tool=True,
            tool_action={"type": "approve_npm_command"},
        )

    def _cmd_reject_npm(self, rest: str) -> CommandResult:
        return CommandResult(
            ok=True,
            output="[dispatch: reject_npm_command]",
            command_type="mutation",
            needs_tool=True,
            tool_action={"type": "reject_npm_command"},
        )

    def _cmd_show_diff(self, rest: str) -> CommandResult:
        path = rest.strip()
        tool_action: Dict[str, Any] = {"type": "show_diff"}
        if path:
            tool_action["path"] = path.removeprefix("/repo/").removeprefix("repo/")
        return CommandResult(
            ok=True,
            output="[dispatch: show_diff]",
            command_type="write",
            needs_tool=True,
            tool_action=tool_action,
        )

    def _cmd_review_changes(self, rest: str) -> CommandResult:
        parts = [part for part in rest.split() if part]
        path = ""
        limit = 20
        for part in parts:
            if part.startswith("limit="):
                try:
                    limit = int(part.split("=", 1)[1])
                except ValueError:
                    pass
            elif not path:
                path = part
        tool_action: Dict[str, Any] = {"type": "review_changes", "limit": limit}
        if path:
            tool_action["path"] = path.removeprefix("/repo/").removeprefix("repo/")
        return CommandResult(
            ok=True,
            output="[dispatch: review_changes]",
            command_type="write",
            needs_tool=True,
            tool_action=tool_action,
        )

    def _cmd_git(self, rest: str) -> CommandResult:
        parts = rest.split(None, 1)
        subcmd = parts[0] if parts else ""
        args = parts[1] if len(parts) > 1 else ""

        git_map = {
            "status": "git_status",
            "diff": "git_diff",
            "add": "git_add",
            "rm": "git_rm",
            "restore": "git_restore",
            "commit": "git_commit",
            "log": "git_log",
            "branch": "git_branch",
        }

        action_type = git_map.get(subcmd)
        if action_type is None:
            return CommandResult(ok=False, output=f"Unknown git subcommand: {subcmd}", command_type="error")

        tool_action: Dict[str, Any] = {"type": action_type}
        if subcmd == "add" and args:
            tool_action["path"] = [p.strip() for p in args.split()]
        elif subcmd == "rm" and args:
            path_tokens = [p.strip().removeprefix("/repo/").removeprefix("repo/") for p in args.split() if p.strip()]
            if not path_tokens:
                return CommandResult(ok=False, output="Usage: git rm <path>", command_type="error")
            shell_command = "git rm -- " + " ".join(shlex.quote(token) for token in path_tokens)
            return CommandResult(
                ok=True,
                output="[dispatch: git_rm]",
                command_type="write",
                needs_tool=True,
                tool_action={"type": "run_shell", "command": shell_command},
            )
        elif subcmd == "commit" and args:
            tool_action["message"] = args
        elif subcmd == "diff" and args:
            tool_action["path"] = args.strip()

        return CommandResult(
            ok=True,
            output=f"[dispatch: {action_type}]",
            command_type="write",
            needs_tool=True,
            tool_action=tool_action,
        )

    # ------------------------------------------------------------------
    # TREE MUTATIONS (in-process)
    # ------------------------------------------------------------------

    def _cmd_fact(self, rest: str) -> CommandResult:
        # fact <issue>/<type>/<key> <value>
        parts = rest.split(None, 1)
        if len(parts) < 2:
            return CommandResult(ok=False, output="Usage: fact <issue>/<type>/<key> <value>", command_type="error")
        fact_path, value = parts
        segments = fact_path.strip("/").split("/")
        if len(segments) < 3:
            return CommandResult(ok=False, output="Fact path must be <issue>/<type>/<key>", command_type="error")

        issue_id = segments[0]
        fact_type = segments[1]
        key = "/".join(segments[2:])  # Allow nested keys

        self.tree.set_fact(issue_id, fact_type, key, value)

        # Also return as tool_action so the host can persist to ledger
        return CommandResult(
            ok=True,
            output=f"Fact set: /facts/{issue_id}/{fact_type}/{key}",
            command_type="mutation",
            needs_tool=True,
            tool_action={"type": "set_fact", "key": key, "value": value, "fact_type": fact_type, "issue_id": issue_id},
        )

    def _cmd_expand(self, rest: str) -> CommandResult:
        target = rest.strip()
        if not target:
            return CommandResult(ok=False, output="Usage: expand <step_number|memory_id>", command_type="error")

        # Determine if it's a step number or memory ID
        try:
            step = int(target)
            return CommandResult(
                ok=True,
                output=f"[dispatch: history_expand step {step}]",
                command_type="mutation",
                needs_tool=True,
                tool_action={"type": "history_expand", "step": step},
            )
        except ValueError:
            return CommandResult(
                ok=True,
                output=f"[dispatch: memory_expand {target}]",
                command_type="mutation",
                needs_tool=True,
                tool_action={"type": "memory_expand", "id": target},
            )

    def _cmd_drop(self, rest: str) -> CommandResult:
        return CommandResult(
            ok=True,
            output="[dispatch: drop_context]",
            command_type="mutation",
            needs_tool=True,
            tool_action={"type": "drop_context"},
        )

    def _cmd_batch(self, rest: str) -> CommandResult:
        subcmd = rest.strip().lower()
        if subcmd == "start":
            return CommandResult(
                ok=True,
                output="[dispatch: begin_edit_batch]",
                command_type="mutation",
                needs_tool=True,
                tool_action={"type": "begin_edit_batch"},
            )
        elif subcmd == "end":
            return CommandResult(
                ok=True,
                output="[dispatch: end_edit_batch]",
                command_type="mutation",
                needs_tool=True,
                tool_action={"type": "end_edit_batch"},
            )
        return CommandResult(ok=False, output="Usage: batch start|end", command_type="error")

    def _cmd_finish(self, rest: str) -> CommandResult:
        message = rest.strip() or "Done."
        return CommandResult(
            ok=True,
            output=f"[finish: {message}]",
            command_type="finish",
            needs_tool=True,
            tool_action={"type": "finish", "message": message},
        )

    def _cmd_resolve_issue(self, rest: str) -> CommandResult:
        issue_id = rest.strip()
        if not issue_id:
            return CommandResult(ok=False, output="Usage: resolve-run-issue <id>", command_type="error")
        resolved = self.tree.resolve_log_issue(issue_id)
        if not resolved:
            return CommandResult(ok=False, output=f"Issue not found: {issue_id}", command_type="error")
        return CommandResult(ok=True, output=f"Issue resolved: {issue_id}", command_type="mutation")

    def _cmd_reopen_issue(self, rest: str) -> CommandResult:
        issue_id = rest.strip()
        if not issue_id:
            return CommandResult(ok=False, output="Usage: reopen-run-issue <id>", command_type="error")
        reopened = self.tree.reopen_log_issue(issue_id)
        if not reopened:
            return CommandResult(ok=False, output=f"Issue not found: {issue_id}", command_type="error")
        return CommandResult(ok=True, output=f"Issue reopened: {issue_id}", command_type="mutation")

    # ------------------------------------------------------------------
    # SKILLS (preloaded cache or handler)
    # ------------------------------------------------------------------

    def _cmd_skill(self, rest: str) -> CommandResult:
        parts = rest.split(None, 1)
        if not parts:
            # List available skills
            skills = self.tree.list_skills()
            if not skills:
                return CommandResult(ok=True, output="No skills registered.", command_type="read")
            lines = []
            for skill in skills:
                category = str(skill.get("category", "") or "").strip()
                priority = str(skill.get("priority", "") or "").strip()
                tags = str(skill.get("tags", "") or "").strip()
                modes = str(skill.get("modes", "") or "").strip()
                suffix_parts = [part for part in [f"category={category}" if category else "", f"priority={priority}" if priority else "", f"tags={tags}" if tags else "", f"modes={modes}" if modes else ""] if part]
                suffix = f" [{'; '.join(suffix_parts)}]" if suffix_parts else ""
                lines.append(f"  {skill['name']} — {skill['description']}{suffix}")
            return CommandResult(ok=True, output="Available skills:\n" + "\n".join(lines), command_type="read")

        name = parts[0]
        args_str = parts[1] if len(parts) > 1 else ""

        # Parse args as key=value pairs or JSON
        kwargs: Dict[str, Any] = {}
        if args_str:
            if args_str.strip().startswith("{"):
                try:
                    kwargs = json.loads(args_str)
                except json.JSONDecodeError:
                    kwargs = {"input": args_str}
            else:
                for token in args_str.split():
                    if "=" in token:
                        k, v = token.split("=", 1)
                        kwargs[k] = v
                    else:
                        kwargs["input"] = kwargs.get("input", "") + " " + token
                        kwargs["input"] = kwargs["input"].strip()

        result = self.tree.invoke_skill(name, **kwargs)
        return CommandResult(ok=True, output=result, command_type="skill")


# ---------------------------------------------------------------------------
# Multi-command support — agent can chain reads in one turn
# ---------------------------------------------------------------------------

def collapse_heredocs(lines: List[str]) -> List[str]:
    """Pre-process lines: collapse any ``<<< ... >>>`` heredoc blocks.

    Any line whose *stripped* form ends with ``<<<`` begins a heredoc.
    Content is collected until a line whose stripped form is ``>>>``.
    The ``<<<`` is replaced with the joined content, producing one
    logical line no matter how many physical lines the block spans.

    Works for **every** prefix — ``write``, ``s2: write``, ``>>th:``, etc.
    """
    result: List[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].rstrip()
        if stripped.rstrip().endswith("<<<"):
            prefix = stripped[:-3].rstrip()
            content_parts: List[str] = []
            i += 1
            while i < len(lines):
                if lines[i].strip() == ">>>":
                    i += 1
                    break
                content_parts.append(lines[i])
                i += 1
            content = "\n".join(content_parts)
            result.append(f"{prefix} {content}" if prefix else content)
        else:
            result.append(lines[i])
            i += 1
    return result


def parse_multi_command(raw: str) -> List[str]:
    """Split newline-separated commands. Lines starting with # are comments.
    Annotation lines (>>tag:) are preserved as-is for separate handling.

    Supports heredoc-style multi-line blocks:
        write /repo/file.md <<<
        line 1
        line 2
        >>>
    These are collapsed into a single command with the content joined.
    Heredoc works on ANY prefix (write, s2: write, >>th:, etc.).
    """
    logical_lines = collapse_heredocs(raw.strip().splitlines())
    commands: List[str] = []
    i = 0
    while i < len(logical_lines):
        line = logical_lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue
        if _starts_inline_multiline_command(stripped):
            content_lines: List[str] = [stripped]
            i += 1
            while i < len(logical_lines):
                continuation = logical_lines[i]
                continuation_stripped = continuation.strip()
                if not continuation_stripped:
                    content_lines.append("")
                    i += 1
                    continue
                if continuation_stripped.startswith("#") or _starts_new_command_boundary(continuation_stripped):
                    break
                content_lines.append(continuation)
                i += 1
            commands.append("\n".join(content_lines).strip())
            continue
        commands.append(stripped)
        i += 1
    return commands


def _starts_inline_multiline_command(line: str) -> bool:
    lowered = str(line or "").lstrip().lower()
    return lowered.startswith("write ") or lowered.startswith("replace-lines ") or lowered.startswith("replace_lines ")


def _starts_new_command_boundary(line: str) -> bool:
    if not line:
        return False
    if line.startswith(">>"):
        return True
    if is_strategy(line):
        return True
    first = line.split(None, 1)[0].lower() if line else ""
    return first in {
        "ls", "cat", "read-line-range", "read_line_range", "symbols", "find-symbol", "find_symbol", "stat", "find", "grep",
        "read-diagnostics", "diagnose", "run-route-check", "run_route_check", "ingest-log", "list-run-issues", "list_run_issues", "list-issues", "show-run-issue", "show_run_issue", "show-issue", "resolve-run-issue", "resolve_run_issue", "resolve-issue", "reopen-run-issue", "reopen_run_issue", "reopen-issue", "run-check",
        "write", "replace-lines", "replace_lines", "patch", "discover", "mutate", "show-diff", "show_diff", "review-changes", "review_changes", "shell", "git",
        "fact", "expand", "drop", "batch", "finish", "skill",
    }


def execute_multi(parser: TreeCommandParser, raw: str) -> Tuple[List[CommandResult], List[Annotation]]:
    """Execute multiple commands, returning results and annotations.
    Read commands execute immediately. Write/mutation commands
    are collected for dispatch. Annotation lines are captured separately."""
    commands = parse_multi_command(raw)
    results: List[CommandResult] = []
    annotations: List[Annotation] = []
    for cmd in commands:
        ann = parse_annotation(cmd)
        if ann is not None:
            annotations.append(ann)
            results.append(CommandResult(
                ok=True,
                output=ann.compact(),
                command_type="annotation",
            ))
            continue
        result = parser.parse_and_execute(cmd)
        results.append(result)
    return results, annotations


# ---------------------------------------------------------------------------
# Command Strategies — DAG pipelines with parallel groups and dependency flow
# ---------------------------------------------------------------------------
#
# Syntax:
#   s1: cat /repo/main.py, cat /repo/planner.py
#   s2: grep /repo "class Worker" -> s3
#   s3: fact demo/arch/worker Found in main.py
#
# Rules:
#   - "sN:" labels a strategy step. Commands after the colon are the group.
#   - Comma-separated commands within a step run in parallel.
#   - "-> sN, sM" at the end of a step declares dependents that receive output.
#   - A step won't execute until all steps that point to it have finished.
#   - The accumulated output of upstream steps is available to downstream
#     commands via the {sN} placeholder (expands to that step's output).
#   - Placeholders support a small text transform chain, e.g.
#       {s1.stdout.split('\n').filter(line => !line.includes("React")).join('\n')}
#       {s1.stdout.split('\n')[0].trim()}
#       {s2.stdout.match(/proximity \* (\d+\.?\d*)/)[1]}
#   - Steps with no inbound arrows and no explicit deps run first.
#
# Example:
#   s1: cat /repo/main.py:1-50, cat /repo/planner.py:1-50
#   s2: cat /repo/issue_facts.py:1-30
#   s1, s2 -> s3: fact demo/arch/overview main.py is worker, planner.py orchestrates
#
# This means: s1 and s2 run in parallel (all reads are free), then s3 runs
# after both complete. s3's command can reference {s1} and {s2} to use their
# output.

_STRATEGY_LINE_RE = re.compile(
    r"^(?:(?P<deps>[\w\s,]+)->\s*)?(?P<label>s\w+)\s*:\s*(?P<body>.+?)(?:\s*->\s*(?P<targets>[\w\s,]+))?\Z",
    re.IGNORECASE | re.DOTALL,
)


def _split_strategy_commands(body: str) -> List[str]:
    commands: List[str] = []
    current: List[str] = []
    quote: str = ""
    escaped = False
    paren_depth = 0
    brace_depth = 0
    bracket_depth = 0

    for char in body:
        current.append(char)

        if quote:
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == quote:
                quote = ""
            continue

        if char in {'"', "'"}:
            quote = char
            continue
        if char == "(":
            paren_depth += 1
            continue
        if char == ")":
            paren_depth = max(0, paren_depth - 1)
            continue
        if char == "{":
            brace_depth += 1
            continue
        if char == "}":
            brace_depth = max(0, brace_depth - 1)
            continue
        if char == "[":
            bracket_depth += 1
            continue
        if char == "]":
            bracket_depth = max(0, bracket_depth - 1)
            continue
        if char == "," and not (quote or paren_depth or brace_depth or bracket_depth):
            current.pop()
            command = "".join(current).strip()
            if command:
                commands.append(command)
            current = []

    tail = "".join(current).strip()
    if tail:
        commands.append(tail)
    return commands


@dataclass
class StrategyStep:
    """One step in a command strategy DAG."""
    label: str
    commands: List[str]  # parallel commands in this step
    depends_on: List[str] = field(default_factory=list)  # labels this step waits for
    targets: List[str] = field(default_factory=list)  # labels that receive this step's output
    results: List[CommandResult] = field(default_factory=list)

    @property
    def output(self) -> str:
        return "\n".join(r.output for r in self.results if r.ok)


@dataclass
class StrategyPlan:
    """A parsed strategy DAG ready for execution."""
    steps: Dict[str, StrategyStep]
    execution_order: List[List[str]]  # tiers of parallelizable labels

    def summary(self) -> str:
        lines: List[str] = []
        for tier_idx, tier in enumerate(self.execution_order):
            tier_labels = ", ".join(tier)
            lines.append(f"  tier {tier_idx}: [{tier_labels}]")
        return "Strategy plan:\n" + "\n".join(lines)


def parse_strategy(raw: str) -> Optional[StrategyPlan]:
    """Parse a strategy block into a StrategyPlan.

    Returns None if the input isn't a strategy (no sN: labels found).
    Heredoc blocks (<<<...>>>) are collapsed before parsing so that
    multi-line file content appears as a single command body.
    """
    # Pre-collapse any heredoc blocks before strategy parsing
    raw_lines = [l for l in raw.strip().splitlines()]
    logical_lines = collapse_heredocs(raw_lines)
    lines = [l.strip() for l in logical_lines if l.strip() and not l.strip().startswith("#")]
    if not lines:
        return None

    steps: Dict[str, StrategyStep] = {}
    # Track forward-declared targets for wiring deps
    forward_targets: Dict[str, List[str]] = {}  # source_label -> [target_labels]

    for line in lines:
        m = _STRATEGY_LINE_RE.match(line)
        if m is None:
            continue

        label = m.group("label").strip().lower()
        body = m.group("body").strip()
        deps_str = (m.group("deps") or "").strip()
        targets_str = (m.group("targets") or "").strip()

        # Parse top-level comma-separated commands, but keep commas that are
        # part of inline code payloads, strings, or structured literals.
        # Embedded newlines from collapsed heredocs stay single-command.
        if "\n" in body:
            commands = [body]
        else:
            commands = _split_strategy_commands(body)

        # Parse explicit deps (left side: "s1, s2 -> sN: ...")
        deps = [d.strip().lower() for d in deps_str.split(",") if d.strip()] if deps_str else []

        # Parse targets (right side: "... -> s4, s5")
        targets = [t.strip().lower() for t in targets_str.split(",") if t.strip()] if targets_str else []

        if label in steps:
            return StrategyPlan(
                steps={
                    "error": StrategyStep(
                        label="error",
                        commands=[],
                        results=[
                            CommandResult(
                                ok=False,
                                output=(
                                    f"Invalid strategy: duplicate label `{label}`. "
                                    "Emit exactly one executable strategy block with unique labels like `s1:`, `s2:`."
                                ),
                                command_type="error",
                            )
                        ],
                    )
                },
                execution_order=[["error"]],
            )

        steps[label] = StrategyStep(label=label, commands=commands, depends_on=deps, targets=targets)
        if targets:
            forward_targets[label] = targets

    if not steps:
        return None

    # Wire forward targets into deps: if s1 -> s3, then s3.depends_on includes s1
    for source, targets in forward_targets.items():
        for target in targets:
            if target in steps and source not in steps[target].depends_on:
                steps[target].depends_on.append(source)

    # Topological sort into execution tiers
    execution_order = _topological_tiers(steps)

    return StrategyPlan(steps=steps, execution_order=execution_order)


def _topological_tiers(steps: Dict[str, StrategyStep]) -> List[List[str]]:
    """Sort steps into tiers where each tier can run in parallel."""
    in_degree: Dict[str, int] = {label: 0 for label in steps}
    for step in steps.values():
        for dep in step.depends_on:
            if dep in steps:
                in_degree[step.label] = in_degree.get(step.label, 0) + 1

    # BFS by tier
    tiers: List[List[str]] = []
    remaining = set(steps.keys())

    while remaining:
        # Find all steps with no unresolved deps
        tier = [label for label in remaining if in_degree.get(label, 0) == 0]
        if not tier:
            # Cycle detected — just dump remaining
            tiers.append(sorted(remaining))
            break
        tier.sort()  # deterministic ordering
        tiers.append(tier)
        for label in tier:
            remaining.discard(label)
            # Reduce in-degree for dependents
            for other_label, other_step in steps.items():
                if label in other_step.depends_on and other_label in remaining:
                    in_degree[other_label] = max(0, in_degree.get(other_label, 1) - 1)

    return tiers


_PLACEHOLDER_RE = re.compile(r"\{(s\w+[^{}]*)\}", re.IGNORECASE)
_QUOTED_ARG_RE = r'(?:"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\')'


def _decode_placeholder_string(token: str) -> str:
    return str(ast.literal_eval(token))


def _consume_chained_call(tail: str, name: str) -> Optional[Tuple[str, str]]:
    prefix = f".{name}("
    if not tail.startswith(prefix):
        return None
    depth = 0
    quote = ""
    in_regex = False
    escaped = False
    for index, char in enumerate(tail[len(prefix):], start=len(prefix)):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote:
            if char == quote:
                quote = ""
            continue
        if in_regex:
            if char == "/":
                in_regex = False
            continue
        if char in {"'", '"'}:
            quote = char
            continue
        if char == "/" and name == "match":
            in_regex = True
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                return tail[len(prefix):index], tail[index + 1:]
            depth -= 1
    raise ValueError(f"unterminated .{name}(...) placeholder transform")


def _apply_filter_transform(value: Any, predicate: str) -> List[str]:
    if not isinstance(value, list):
        raise ValueError(".filter(...) requires a list value")

    match = re.match(
        rf"^\s*(?P<var>\w+)\s*=>\s*(?P<negate>!)?\s*(?P=var)\.includes\((?P<needle>{_QUOTED_ARG_RE})\)\s*$",
        predicate,
    )
    if match is None:
        raise ValueError(f"unsupported filter predicate: {predicate}")

    needle = _decode_placeholder_string(match.group("needle"))
    negate = bool(match.group("negate"))
    filtered: List[str] = []
    for item in value:
        text = str(item)
        condition = needle in text
        if negate:
            condition = not condition
        if condition:
            filtered.append(text)
    return filtered


def _consume_index_access(tail: str) -> Optional[Tuple[int, str]]:
    if not tail.startswith("["):
        return None
    end = tail.find("]")
    if end == -1:
        raise ValueError("unterminated placeholder index access")
    raw_index = tail[1:end].strip()
    if not re.fullmatch(r"-?\d+", raw_index):
        raise ValueError(f"unsupported placeholder index access: [{raw_index}]")
    return int(raw_index), tail[end + 1:]


def _decode_placeholder_regex(token: str) -> Tuple[str, int]:
    if not token.startswith("/"):
        raise ValueError(".match(...) requires a regex literal like /pattern/")

    escaped = False
    closing_index = -1
    for index in range(1, len(token)):
        char = token[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "/":
            closing_index = index
            break

    if closing_index == -1:
        raise ValueError(f"unterminated regex literal: {token}")

    pattern = token[1:closing_index]
    flags_token = token[closing_index + 1:].strip()
    flags = 0
    for flag in flags_token:
        if flag == "i":
            flags |= re.IGNORECASE
            continue
        if flag == "m":
            flags |= re.MULTILINE
            continue
        if flag == "s":
            flags |= re.DOTALL
            continue
        raise ValueError(f"unsupported regex flag: {flag}")
    return pattern, flags


def _evaluate_placeholder_expression(expr: str, steps: Dict[str, StrategyStep]) -> str:
    expr = expr.strip()
    base_match = re.match(r"^(?P<label>s\w+)(?P<tail>.*)$", expr, re.IGNORECASE)
    if base_match is None:
        raise ValueError(f"placeholder must start with a strategy label: {expr}")

    label = base_match.group("label").lower()
    step = steps.get(label)
    if step is None:
        raise ValueError(f"unknown strategy placeholder: {label}")

    value: Any = step.output
    tail = base_match.group("tail")

    while tail:
        if tail.startswith(".stdout") or tail.startswith(".output"):
            tail = tail[7:]
            continue
        if tail.startswith(".text"):
            tail = tail[5:]
            continue
        if tail.startswith(".trim()"):
            value = str(value).strip()
            tail = tail[len(".trim()"):]
            continue

        split_call = _consume_chained_call(tail, "split")
        if split_call is not None:
            arg, tail = split_call
            value = str(value).split(_decode_placeholder_string(arg.strip()))
            continue

        join_call = _consume_chained_call(tail, "join")
        if join_call is not None:
            arg, tail = join_call
            if not isinstance(value, list):
                raise ValueError(".join(...) requires a list value")
            value = _decode_placeholder_string(arg.strip()).join(str(item) for item in value)
            continue

        replace_call = _consume_chained_call(tail, "replace")
        if replace_call is not None:
            args, tail = replace_call
            parts = [part.strip() for part in args.split(",", 1)]
            if len(parts) != 2:
                raise ValueError(".replace(...) requires two quoted string arguments")
            value = str(value).replace(
                _decode_placeholder_string(parts[0]),
                _decode_placeholder_string(parts[1]),
            )
            continue

        filter_call = _consume_chained_call(tail, "filter")
        if filter_call is not None:
            predicate, tail = filter_call
            value = _apply_filter_transform(value, predicate.strip())
            continue

        match_call = _consume_chained_call(tail, "match")
        if match_call is not None:
            arg, tail = match_call
            pattern, flags = _decode_placeholder_regex(arg.strip())
            match = re.search(pattern, str(value), flags)
            if match is None:
                value = []
            else:
                value = [match.group(0), *match.groups()]
            continue

        index_access = _consume_index_access(tail)
        if index_access is not None:
            index, tail = index_access
            if not isinstance(value, list):
                raise ValueError("index access requires a list value")
            try:
                value = value[index]
            except IndexError as exc:
                raise ValueError(f"placeholder index out of range: [{index}]") from exc
            continue

        raise ValueError(f"unsupported placeholder transform chain: {expr}")

    if isinstance(value, list):
        return "\n".join(str(item) for item in value)
    return str(value)


def _resolve_strategy_placeholders(command: str, steps: Dict[str, StrategyStep]) -> str:
    def _replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        return _evaluate_placeholder_expression(expr, steps)

    return _PLACEHOLDER_RE.sub(_replace, command)


def is_strategy(raw: str) -> bool:
    """Quick check if the input looks like a strategy block."""
    logical_lines = collapse_heredocs(raw.strip().splitlines())
    for line in logical_lines:
        line = line.strip()
        if line and not line.startswith("#") and _STRATEGY_LINE_RE.match(line):
            return True
    return False


def execute_strategy(parser: TreeCommandParser, raw: str) -> Dict[str, List[CommandResult]]:
    """Parse and execute a strategy DAG.

    Returns a dict of label -> results. Commands within a tier execute
    in sequence (since Python is single-threaded here), but the tier
    structure tells the caller what *could* be parallelized.

    Upstream outputs are injected into downstream commands via {sN} placeholders.
    """
    plan = parse_strategy(raw)
    if plan is None:
        return {"error": [CommandResult(ok=False, output="Not a valid strategy", command_type="error")]}

    all_results: Dict[str, List[CommandResult]] = {}

    for tier in plan.execution_order:
        for label in tier:
            step = plan.steps[label]
            if step.results and all(result.command_type == "error" for result in step.results):
                all_results[label] = step.results
                continue

            # Substitute upstream outputs into commands
            resolved_commands: List[str] = []
            for cmd in step.commands:
                try:
                    resolved = _resolve_strategy_placeholders(cmd, plan.steps)
                except ValueError as exc:
                    step_results = [CommandResult(ok=False, output=f"placeholder error: {exc}", command_type="error")]
                    step.results = step_results
                    all_results[label] = step_results
                    resolved_commands = []
                    break
                resolved_commands.append(resolved)

            if label in all_results and all_results[label] and not all_results[label][0].ok:
                continue

            # Execute all commands in this step
            step_results: List[CommandResult] = []
            for cmd in resolved_commands:
                result = parser.parse_and_execute(cmd)
                step_results.append(result)

            step.results = step_results
            all_results[label] = step_results

    return all_results


def format_strategy_results(
    results: Dict[str, List[CommandResult]],
    *,
    max_output_chars: int = 1200,
    max_dispatch_chars: int = 240,
) -> str:
    """Format strategy results for display.

    This is intentionally CLI-oriented: stored command results remain full-fidelity,
    but terminal output is truncated so a large file read or heredoc write does not
    drown the surrounding turn trace.
    """
    lines: List[str] = []
    for label in sorted(results.keys()):
        step_results = results[label]
        for r in step_results:
            ok_mark = "✓" if r.ok else "✗"
            tag = "READ" if r.command_type == "read" else "TOOL" if r.needs_tool else r.command_type.upper()
            header = f"[{ok_mark} {label} {tag}]"
            if r.needs_tool:
                dispatch = str(r.tool_action)
                if len(dispatch) > max_dispatch_chars:
                    dispatch = dispatch[:max_dispatch_chars] + f" … ({len(dispatch) - max_dispatch_chars} chars truncated)"
                header += f" → dispatch: {dispatch}"
            output = r.output
            if len(output) > max_output_chars:
                output = output[:max_output_chars] + f"\n… ({len(r.output) - max_output_chars} chars truncated)"
            lines.append(f"{header}\n{output}")
    return "\n\n".join(lines)
