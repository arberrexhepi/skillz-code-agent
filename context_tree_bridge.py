"""
ContextTreeBridge — Integration layer between WorkingFolderAgent and ContextTree.

Usage:
    bridge = ContextTreeBridge(agent)
    bridge.setup()  # Index repo, sync facts/memory/status
    
    # In _build_prompt, replace the 15 scattered sections:
    tree_block = bridge.render_for_prompt()
    
    # In _execute_action, intercept tree commands:
    if bridge.is_tree_command(raw_output):
        results = bridge.execute(raw_output)
        # results with needs_tool=True get dispatched to existing handlers
        # results without needs_tool return immediately (free reads)

This module does NOT modify main.py directly. It provides the hook points
that main.py can call into when ready.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from context_tree import ContextTree
from tree_commands import (
    Annotation,
    CommandResult,
    TreeCommandParser,
    execute_multi,
    execute_strategy,
    format_strategy_results,
    is_strategy,
)


class ContextTreeBridge:
    """Bridge between the existing WorkingFolderAgent and the ContextTree."""

    def __init__(
        self,
        *,
        workspace_root: Path,
        get_fact_records: Callable[[], Sequence[Any]],
        get_memory_items: Callable[[], Sequence[Any]],
        get_status: Callable[[], Dict[str, Any]],
        hot_file_selector: Optional[Callable[[str], List[str]]] = None,
    ) -> None:
        self.tree = ContextTree(workspace_root)
        self._get_fact_records = get_fact_records
        self._get_memory_items = get_memory_items
        self._get_status = get_status
        self._hot_file_selector = hot_file_selector
        self.parser = TreeCommandParser(self.tree, get_issue_state=self._issue_state_for_commands)
        self._indexed = False
        self._last_sync_ts = 0.0
        self._sync_interval = 2.0  # seconds between syncs

    def _issue_state_for_commands(self) -> Dict[str, Any]:
        try:
            status = self._get_status()
        except Exception:
            return {}
        if not isinstance(status, dict):
            return {}
        issue_state = status.get("issue_state")
        return issue_state if isinstance(issue_state, dict) else {}

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------

    def setup(
        self,
        *,
        max_files: int = 5000,
        exclude_dirs: Optional[set[str]] = None,
    ) -> Dict[str, Any]:
        """Index the workspace and perform initial sync."""
        count = self.tree.index_repo(max_files=max_files, exclude_dirs=exclude_dirs)
        self._indexed = True
        self._sync_all()
        return {"files_indexed": count, "fact_count": len(list(self.tree._facts.walk())), "skills": len(self.tree._skills)}

    def _sync_all(self, *, force: bool = False) -> None:
        """Sync facts, memory, and status into the tree."""
        now = time.time()
        if not force and now - self._last_sync_ts < self._sync_interval:
            return
        try:
            records = self._get_fact_records()
            if records:
                self.tree.sync_facts(records)
        except Exception:
            pass
        try:
            self.tree.sync_memory(self._get_memory_items())
        except Exception:
            pass
        try:
            self.tree.sync_status(self._get_status())
        except Exception:
            pass
        self._last_sync_ts = now

    # ------------------------------------------------------------------
    # Prompt rendering
    # ------------------------------------------------------------------

    def render_for_prompt(
        self,
        *,
        repo_depth: int = 2,
        hot_files: Optional[Sequence[str]] = None,
        max_tree_chars: int = 30000,
        max_hot_chars: int = 50000,
    ) -> str:
        """Render the full context tree block for prompt injection.
        
        Replaces these _build_prompt sections:
          - REPO SNAPSHOT (partially — git status/branch still needed separately)
          - RELEVANT MEMORY
          - FACT CONTEXT
          - SELECTED GOAL FACTS (facts are now addressable paths)
          - ACTIVE CONTEXT (hot files replace expanded items)
          - STATUS flags (completion_check, edit_batch, etc.)
        """
        self._sync_all(force=True)

        sections: List[str] = []
        sections.append(self.tree.render_prompt_block(
            repo_depth=repo_depth,
            max_chars=max_tree_chars,
        ))

        if hot_files:
            self.tree.preload_files(hot_files)
            hot_block = self.tree.render_hot_files_block(hot_files, max_chars=max_hot_chars)
            if hot_block != "(no hot files loaded)":
                sections.append("HOT FILES (preloaded):\n" + hot_block)

        return "\n\n".join(sections)

    def render_command_grammar(self) -> str:
        """Render the compact command grammar for the system prompt.
        Replaces the ~2KB JSON action schema."""
        return """
Command grammar (one command per line, multiple reads allowed per turn):

READ (free, no tool call — use these instead of tool actions for exploration):
  ls /repo/src depth=2              List directory entries
  cat /repo/src/main.py             Read full file content
  cat /repo/src/main.py:100-200     Read specific line range
  read-line-range /repo/src/main.py 100-200  Read a numbered line range
  symbols /repo/src/main.py         List functions/classes/variables in a file
  find-symbol /repo/src useTodo     Find a symbol by name in a file or directory
  stat /repo/src/main.py            File metadata (size, lines)
  find /repo *.py limit=50          Glob search for files
  grep /repo/src "pattern"          Search loaded file contents
  grep /facts "keyword"             Search facts
  read-diagnostics [path]           Ingest a local diagnostics snapshot
    diagnose <path> [limit=N]         Run backend diagnostics for one file and ingest issues
  run-route-check <route-or-url> [base=<url>]  Visit a route and ingest browser/runtime errors
  list-issues                       List parsed log issues
  show-issue <id>                   Show one parsed issue
  reopen-issue <id>                 Mark one parsed issue open again

WRITE (dispatches to real tools):
  write <path> <content>            Write file (single line content)
  write <path> <<<                  Write file (multi-line heredoc)
    full file content here...
    multiple lines supported
  >>>                               End of heredoc block
  replace-lines <path>:10-20 text   Replace one bounded line inline
  replace-lines <path>:10-20 <<<    Replace a bounded line range
    replacement lines here
  >>>
  patch <path> <search> -> <replace>  Search/replace edit
    show-diff [path]                  Show git diff for the workspace or one path
    review-changes [path] [limit=N]   Review changed files and risk summary
  shell <command>                   Run shell command
  git status                        Git status
  git diff [path]                   Git diff
  git add <paths...>                Stage files
  git commit <message>              Commit staged changes

HEREDOC (<<< ... >>> works on ANY command or strategy step):
  s2: write /repo/file.tsx <<<      Strategy step with heredoc
    import React from 'react';
    export default function Demo() ...
  >>>
  Content between <<< and >>> is joined and attached to the preceding command.
  IMPORTANT: Put the complete final file content between <<< and >>>.
  Do NOT use variable substitution or string operations inside heredoc.
  Literal braces in CSS, JSX comments, object literals, and similar code are safe.
  Only placeholders that begin with a strategy label such as {s1} are special.

CONTEXT:
  fact <issue>/<type>/<key> <value> Set/update a durable fact
  ingest-log <path>                 Parse a log file into /facts/log-issues
  run-check <build|lint|typecheck|test>  Run a finite project check and ingest issues
  run-route-check <route-or-url> [base=<url>]  Visit a route with Playwright and ingest browser/runtime issues
  expand <step_number>              Expand a history step
  expand <memory_id>                Expand a memory item
  drop                              Clear active context
  batch start                       Begin edit batch mode
  batch end                         End edit batch mode
  resolve-issue <id>                Mark a parsed issue resolved
  reopen-issue <id>                 Mark a parsed issue open again
  finish [message]                  Signal task completion

SKILLS:
  skill                             List available skills
  skill <name> [key=value ...]      Invoke a skill

ANNOTATIONS (>> feed-forward, free):
  >>th: <reasoning>                 Thought — explain your approach
  >>dg: <condition>                 Delegation — "if X then do Y"
  >>pl: <plan>                      Plan — intent for upcoming turns
  >>q:  <question>                  Question — flag a blocker
  >>err: <diagnosis>                Error — diagnose what went wrong
  >>ju: <justification>             Justify why more turns are needed

Notes:
- Multiple read commands can be chained (one per line) in a single turn.
- Read commands resolve from the in-context tree — zero cost, no tool dispatch.
- Write/context commands dispatch to the host and count as tool calls.
- Facts are addressable at /facts/<issue>/<type>/<key> — use cat to read them.
- Memory items live at /memory/recent/<id> — use ls/cat to browse.
- Agent status flags live at /status/ — use cat to check state.
- Skills deliver preloaded context or run handlers — no IO at query time.
- Use >>th for what you believe, >>pl for the concrete next step, >>err when a command or edit went sideways, and >>dg only for a real conditional next move.
- For localized edits, prefer this loop: `read-line-range` -> `replace-lines` -> `read-line-range` on the same region.
- Prefer `replace-lines` for bounded edits. Prefer `write` when you are replacing most of a file or reconstructing a corrupted file wholesale.
- Use inline `replace-lines` only for short single-line replacements. Use heredoc for any multi-line replacement or any long replacement text.
- Avoid `cat` on an entire large file right after a localized edit unless you genuinely need global structure.
- Literal braces in CSS, JSX comments, and object literals are normal code content. Only `{sN...}` forms are placeholder syntax.
- Use `batch start` before a cluster of related file fixes and `batch end` after the cluster is landed and verified.
- When a large trace produces more errors after one fix lands, stay composed: that is normal. Read the next grounded error, fix the next issue, and keep moving.

STRATEGIES (multi-step DAG pipelines):
  s1: cat /repo/main.py, cat /repo/planner.py
  s2: grep /repo "class Worker"
  s1, s2 -> s3: fact demo/arch/overview main.py is worker

  - sN: labels a step. Comma-separated commands run as a parallel group.
  - -> sN, sM at end of a step declares targets that receive output.
  - sN, sM -> sK on the left declares deps that must finish first.
  - Use {sN} in downstream commands to inject upstream output.
  - Supported placeholder transforms are limited text operations only:
    {s1.stdout}, {s1.trim()}, {s1.replace("old", "new")},
        {s1.split('\n').filter(line => !line.includes("React")).join('\n')},
        {s1.split('\n')[0].trim()},
        {s2.match(/needle: (.+)/)[1]}
  - Placeholder expressions are text transforms, not general code execution.
  - Steps with no deps execute in the first tier (parallel).
""".strip()

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def is_tree_command(self, raw: str) -> bool:
        """Check if the model output looks like a tree command or strategy (vs JSON action)."""
        stripped = raw.strip()
        if not stripped:
            return False
        # Strategy blocks start with sN: labels
        if is_strategy(stripped):
            return True
        # Tree commands start with a known verb, not with {
        if stripped.startswith("{"):
            return False
        first_word = stripped.split(None, 1)[0].lower()
        return first_word in {
            "ls", "cat", "read-line-range", "read_line_range", "symbols", "find-symbol", "find_symbol", "stat", "find", "grep",
            "read-diagnostics", "run-route-check", "run_route_check", "ingest-log", "list-issues", "show-issue", "resolve-issue", "reopen-issue", "run-check",
            "write", "replace-lines", "replace_lines", "patch", "show-diff", "show_diff", "review-changes", "review_changes", "shell", "git",
            "fact", "expand", "drop", "batch", "finish",
            "skill", "#",
        }

    def execute(self, raw: str) -> List[CommandResult]:
        """Execute one or more tree commands, or a strategy DAG.
        
        Returns a list of CommandResult. The caller should:
        1. Return read results directly to the model (free).
        2. Dispatch needs_tool results through existing _execute_action handlers.
        3. After writes, call bridge.on_write_complete(path) to invalidate cache.
        
        Annotations (>>tag: lines) are captured and accessible via
        self.last_annotations after each call.
        """
        self.last_annotations: List[Annotation] = []
        if is_strategy(raw):
            return self.execute_strategy(raw)
        results, annotations = execute_multi(self.parser, raw)
        self.last_annotations = annotations
        return results

    def execute_strategy(self, raw: str) -> List[CommandResult]:
        """Execute a strategy DAG and return flattened results."""
        results_by_label = execute_strategy(self.parser, raw)
        # Flatten into a single list for uniform return type
        flat: List[CommandResult] = []
        for label in sorted(results_by_label.keys()):
            flat.extend(results_by_label[label])
        return flat

    def execute_strategy_full(self, raw: str) -> Dict[str, List[CommandResult]]:
        """Execute a strategy DAG and return per-label results dict."""
        return execute_strategy(self.parser, raw)

    def format_strategy(self, results: Dict[str, List[CommandResult]]) -> str:
        """Format strategy results for prompt or display."""
        return format_strategy_results(results)

    def execute_single(self, raw: str) -> CommandResult:
        """Execute a single tree command."""
        return self.parser.parse_and_execute(raw)

    # ------------------------------------------------------------------
    # Post-write hooks
    # ------------------------------------------------------------------

    def on_write_complete(self, rel_path: str) -> None:
        """Call after a successful write/patch to invalidate the file cache."""
        self.tree.invalidate_file(rel_path)

    def on_fact_written(self, issue_id: str, fact_type: str, key: str, value: str) -> None:
        """Call after a fact is persisted to the ledger, to update the tree."""
        self.tree.set_fact(issue_id, fact_type, key, value)

    # ------------------------------------------------------------------
    # Skill registration (delegated)
    # ------------------------------------------------------------------

    def register_skill(
        self,
        name: str,
        description: str,
        *,
        args_schema: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        category: str = "general",
        priority: int = 0,
        modes: Optional[List[str]] = None,
        cache: Optional[str] = None,
        handler: Optional[Callable[..., str]] = None,
    ) -> None:
        self.tree.register_skill(
            name,
            description,
            args_schema=args_schema,
            tags=tags,
            category=category,
            priority=priority,
            modes=modes,
            cache=cache,
            handler=handler,
        )
