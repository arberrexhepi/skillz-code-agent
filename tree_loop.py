"""
tree_loop — LLM agent loop over the ContextTree playground OS.

The model sees:
  1. System prompt: identity + command grammar + strategy syntax + rules
  2. User prompt: OS state (tree block) + task + history of prior turns
  3. It emits free-form thought + tree commands / strategies
  4. We execute, append results to history, and loop

Loop terminates on `finish` command or max turns.

Usage:
    from tree_loop import TreeLoop
    from main import create_model_client

    model = create_model_client(provider="anthropic", model="claude-sonnet-4-20250514")
    loop = TreeLoop(model=model, workspace_root=Path("."))
    result = loop.run("Find all TODO comments and record them as facts")
"""

from __future__ import annotations

import shlex
import os
import re
import sys
import time
import subprocess
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple

from context_tree_bridge import ContextTreeBridge
from mutations import (
    append_block,
    batch_mutate,
    copy_file,
    create_file,
    delete_file,
    delete_range,
    delete_snippet,
    fill_template,
    insert_after,
    insert_before,
    insert_symbol_member,
    move_block,
    prepend_block,
    rename_file,
    rename_symbol,
    replace_range,
    replace_snippet,
    replace_symbol,
)
from project_diagnostics import run_backend_diagnostics
from tree_commands import (
    Annotation,
    CommandResult,
    format_strategy_results,
    is_annotation,
    is_strategy,
    parse_multi_command,
    parse_strategy,
)


def _env_flag_enabled(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    return default


# ---------------------------------------------------------------------------
# Protocol — any object with .complete(system, prompt) -> str works
# ---------------------------------------------------------------------------

class ModelClient(Protocol):
    def complete(self, system: str, prompt: str) -> str: ...


# ---------------------------------------------------------------------------
# Turn history
# ---------------------------------------------------------------------------

@dataclass
class Turn:
    """One loop iteration: what the model said and what happened."""
    turn_number: int
    raw_output: str          # full LLM output (annotations + commands)
    commands_issued: List[str]
    results: List[CommandResult]
    annotations: List[Annotation] = field(default_factory=list)
    elapsed_s: float = 0.0

    @property
    def thought(self) -> str:
        """Structured thought from >>th: annotations."""
        return "\n".join(a.content for a in self.annotations if a.tag == "th")

    @property
    def delegations(self) -> List[str]:
        """Delegation conditions from >>dg: annotations."""
        return [a.content for a in self.annotations if a.tag == "dg"]

    @property
    def plan(self) -> str:
        """Plan from >>pl: annotations."""
        return "\n".join(a.content for a in self.annotations if a.tag == "pl")

    @property
    def has_finish(self) -> bool:
        return any(r.command_type == "finish" and r.ok for r in self.results)

    def compact(self, max_result_chars: int = 2000) -> str:
        """Compact representation for history injection."""
        parts: List[str] = []
        parts.append(f"── Turn {self.turn_number} ({self.elapsed_s:.1f}s) ──")

        # Thoughts trace — prominent section so the model sees its own reasoning
        thoughts = [a for a in self.annotations if a.tag in ("th", "pl", "ju")]
        other_anns = [a for a in self.annotations if a.tag not in ("th", "pl", "ju")]
        if thoughts:
            parts.append("[THOUGHTS]")
            for ann in thoughts:
                parts.append(f"  {ann.compact()}")
        if other_anns:
            for ann in other_anns:
                parts.append(ann.compact())

        # Then command results
        for cmd, result in zip(self.commands_issued, self.results):
            if result.command_type == "annotation":
                continue  # already shown above
            ok = "✓" if result.ok else "✗"
            tag = "READ" if result.command_type == "read" else (
                "TOOL" if result.needs_tool else result.command_type.upper()
            )
            parts.append(f"[{ok} {tag}] {_truncate_command_for_cli(cmd)}")
            output = result.output
            if len(output) > max_result_chars:
                output = output[:max_result_chars] + f"\n… ({len(result.output) - max_result_chars} chars truncated)"
            parts.append(output)
        return "\n".join(parts)


def _looks_like_command(line: str) -> bool:
    """Quick check if a line looks like a tree command, strategy label, or annotation."""
    if not line:
        return False
    # Annotations: >>tag: content
    if line.startswith(">>"):
        return True
    if is_strategy(line):
        return True
    first = line.split(None, 1)[0].lower() if line else ""
    return first in {
        "ls", "cat", "read-line-range", "read_line_range", "symbols", "find-symbol", "find_symbol", "stat", "find", "grep",
        "read-diagnostics", "diagnose", "run-route-check", "run_route_check", "ingest-log", "list-issues", "show-issue", "resolve-issue", "reopen-issue", "run-check",
        "write", "replace-lines", "replace_lines", "patch", "show-diff", "show_diff", "review-changes", "review_changes", "shell", "git",
        "fact", "expand", "drop", "batch", "finish",
        "skill",
    }


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """\
You are a precise coding agent operating inside a playground OS.
Your workspace is mounted as a virtual filesystem with 5 mounts:
  /repo    — source files (lazy-loaded)
  /facts   — durable facts you record (persist across turns)
  /memory  — addressable memory items
  /status  — agent state flags
  /skills  — registered skill definitions + caches

{command_grammar}

ANNOTATIONS (>> feed-forward metadata — free, no tool call):
  >>th: <reasoning>        Thought — why you're doing what you're doing
  >>dg: <condition>        Delegation — "if output shows X then do Y next"
  >>pl: <plan>             Plan — what you intend to do in upcoming turns
  >>q:  <question>         Question — something you need clarified
  >>err: <diagnosis>       Error — something went wrong, your diagnosis
  >>ju:  <justification>   Justify — why you need more turns to complete the task

  Annotations flow forward in history. Use them to:
  - Explain your reasoning (>>th:) so you can refer back to it
  - Declare the concrete next action (>>pl:) before issuing commands
  - Record what failed and why (>>err:) when a command or edit goes wrong
  - Set up a genuine conditional next step (>>dg:) before reading output
  - Flag blockers (>>q:) when you need human input
  - Justify continued work (>>ju:) when the operator pauses you at a checkpoint

  Tag discipline:
  - Start every turn with >>th: and >>pl:
  - Keep >>pl: operational, not vague. Good: "Read lines 30-60 of DemoTodoContext.tsx, replace addTodo, then verify lines 30-60."
  - Use >>err: after a failed edit so the next turn can avoid repeating the same mistake.
  - Do not use >>dg: as a generic note. Use it only for a real if/then branch.

RULES:
1. READ commands (ls, cat, read-line-range, symbols, find-symbol, stat, find, grep, read-diagnostics) are FREE — they resolve instantly
    from the in-context tree. Use them liberally. They do NOT count as tool calls.
1a. Skills are part of your capability surface. Use `skill` with no arguments to discover what skills are available, and use
    `skill <name>` to load a skill payload into the run when it would improve task quality, style consistency, testing approach,
    or repair strategy. When skill choice is uncertain or the user asks for skills broadly, prefer an explicit `skill` discovery
    step before any direct `skill <name>` load so you can choose from the actual catalog.
1b. When the user explicitly mentions a skill, playbook, style, or workflow that sounds like a bundled Playground OS skill,
     proactively check available skills and load the best match yourself instead of waiting for the operator to force it.
2. WRITE commands (write, replace-lines, patch, shell, git, diagnose, run-check, run-route-check) dispatch to real tools. Use them
   deliberately after you have enough context from reads.
3. HEREDOC (<<< ... >>>) works on ANY line. End the line with <<<, put the
   content on the next lines, and close with >>> on its own line:
     write /repo/path/file.md <<<
     full file content here
     as many lines as needed
     >>>
   This also works inside strategies:
     s2: write /repo/file.tsx <<<
     import React from 'react';
     export default function Demo() ...
     >>>
   Everything between <<< and >>> becomes part of that command.
4. Record discoveries as facts (fact <issue>/<type>/<key> <value>).
   Facts persist across turns and are visible at /facts/.
5. Use strategies (s1:, s2:, ...) to batch multiple reads into parallel
   pipelines. This is the fastest way to gather broad context.
6. Annotate your output with >> lines. They cost nothing and make your
   reasoning visible. Always start with >>th: to explain your approach.
7. When your task is complete, use: finish <summary message>
8. You may issue multiple commands per turn (one per line) or a strategy block.
9. Do NOT wrap commands in code fences or JSON. Just emit them directly.
10. IMPORTANT: When writing files with heredoc, the ENTIRE file content goes
    between <<< and >>>. Do NOT try to use shell-style variable substitution
    or string replacement inside heredoc. Write the complete final content.
10a. Literal braces in code are safe. CSS blocks, JSX comments, object literals, and template code like `{{ color: red; }}` or `{{/* note */}}` are ordinary file content. Only placeholders that begin with a strategy label such as `{{s1}}` or `{{s2.stdout}}` are special.
11. For localized file repairs, prefer this workflow:
    a) `read-line-range /repo/path/file.ts 40-80`
    b) `replace-lines /repo/path/file.ts:52-68 <<< ... >>>`
    c) `read-line-range /repo/path/file.ts 48-72`
    This keeps edits anchored and reduces accidental duplication.
11a. When line ranges feel unstable or a file has many nearby definitions, use `symbols` or `find-symbol` first to anchor on the right function, class, or variable before editing.
12. Prefer `replace-lines` for bounded edits. Prefer `write` when replacing most of a file or rebuilding a corrupted region wholesale.
12a. Use inline `replace-lines` only for short single-line replacements. If the replacement spans multiple lines or is longer than a short import/function call, use heredoc.
13. Avoid rereading an entire large file after a localized edit unless you need whole-file structure. Verify the edited range first.
14. If a bounded edit fails twice or keeps duplicating content, stop stretching the same range edit. Re-read the surrounding region, widen the inspected range, and either do one clean `replace-lines` pass or rewrite the full file with `write`.
15. Strategy placeholders like `{{s1}}` are raw upstream text plus a few safe text transforms. You may use `.stdout`, `.trim()`, `.replace("old", "new")`, `.split('\n').filter(line => ...).join('\n')`, numeric indexing like `.split('\n')[0]`, and regex capture extraction like `.match(/pattern/)[1]`, but do not invent arbitrary code execution inside placeholders.
16. Use `batch start` before a cluster of related file fixes and `batch end` after the cluster is landed and verified. Batch related edits together, then run one finite verification step such as `diagnose /repo/src/file.tsx`, `run-check typecheck`, or `run-check build`.
16a. After editing a `.ts`, `.tsx`, `.js`, `.jsx`, or `.py` file, prefer `diagnose <path>` when you need backend file-targeted diagnostics that do not rely on an editor.
17. When working from a large trace or many diagnostics, maintain composure. It is normal for new downstream errors to appear after one fix lands. Do not panic, do not restart from scratch, and do not treat each new error as proof the last fix was wrong. Read the next grounded error, fix the next issue, and keep moving.

FORMAT (every turn):
>>th: <your reasoning about what to do>
>>pl: <what you plan to accomplish this turn>

<commands, strategies, or annotations>

Do not answer with a prose checklist, numbered plan, or "I will..." summary.
After the required >>th:/>>pl: tags, emit executable tree commands directly.
When using a strategy, the executable lines must be `s1:`, `s2:`, ... labels,
not numbered bullets.
"""


# ---------------------------------------------------------------------------
# User prompt (rebuilt each turn)
# ---------------------------------------------------------------------------

def _build_user_prompt(
    task: str,
    os_state: str,
    history: List[Turn],
    *,
    recent_reads: Optional[List["RecentRead"]] = None,
    max_history_chars: int = 36000,
    max_recent_read_chars: int = 12000,
    steering: str = "",
) -> str:
    sections: List[str] = []

    # OS state (tree prompt block)
    sections.append("═══ PLAYGROUND OS STATE ═══")
    sections.append(os_state)

    # Task
    sections.append("═══ TASK ═══")
    sections.append(task)

    # Steering (optional)
    if steering:
        sections.append("═══ OPERATOR STEERING ═══")
        sections.append(steering)

    if recent_reads:
        sections.append("═══ RECENT FILE-CONTEXT COMMANDS ═══")
        recent_parts: List[str] = []
        budget = max_recent_read_chars
        for item in recent_reads:
            block = f"[COMMAND] {_truncate_command_for_cli(item.command)}\n{_truncate_for_cli(item.output, limit=1200)}"
            if len(block) > budget:
                block = block[:budget] + "\n… (recent command context truncated)"
                recent_parts.append(block)
                break
            recent_parts.append(block)
            budget -= len(block)
        sections.append("\n\n".join(recent_parts))

    # History (most recent turns, budget-limited)
    if history:
        sections.append("═══ HISTORY (previous turns) ═══")
        history_parts: List[str] = []
        budget = max_history_chars
        for turn in reversed(history):
            compact = turn.compact()
            if len(compact) > budget:
                # Truncate this turn's output to fit
                compact = compact[:budget] + "\n… (older history truncated)"
                history_parts.insert(0, compact)
                break
            history_parts.insert(0, compact)
            budget -= len(compact)
        sections.append("\n\n".join(history_parts))

    sections.append("═══ YOUR TURN ═══")
    sections.append(
        "Think, then act. Start with >>th: and >>pl:, then issue executable tree commands or one `s1:` strategy block. "
        "Do not return a numbered prose plan."
    )

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Command extraction from LLM output
# ---------------------------------------------------------------------------

def extract_commands(raw: str) -> str:
    """Extract annotations and actual commands from an LLM response.

    Models often mix explanation with commands, sometimes wrapping the
    actionable lines in code fences. We keep only:
    - annotation lines (>>tag:)
    - lines that look like tree commands or strategy steps
    - heredoc bodies attached to an extracted command

    Free-form prose is ignored rather than being sent to the command parser.
    """
    lines = raw.strip().splitlines()
    extracted: List[str] = []
    pending_annotations: List[str] = []
    collecting_heredoc = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            continue

        if collecting_heredoc:
            extracted.append(line)
            if stripped == ">>>":
                collecting_heredoc = False
            continue

        if not stripped:
            continue

        if is_annotation(stripped):
            pending_annotations.append(stripped)
            continue

        if _looks_like_command(stripped):
            if pending_annotations:
                extracted.extend(pending_annotations)
                pending_annotations = []
            extracted.append(stripped)
            if stripped.endswith("<<<"):
                collecting_heredoc = True
            continue

    if extracted:
        return "\n".join(extracted).strip()
    if pending_annotations:
        return "\n".join(pending_annotations).strip()
    return ""


# ---------------------------------------------------------------------------
# TreeLoop — the main agent loop
# ---------------------------------------------------------------------------

@dataclass
class LoopResult:
    """Result of a complete loop run."""
    turns: List[Turn]
    finished: bool
    finish_message: str
    total_elapsed_s: float
    reads: int = 0      # free read commands executed
    writes: int = 0     # tool-dispatching commands issued

    def summary(self) -> str:
        status = "FINISHED" if self.finished else "MAX TURNS"
        return (
            f"[{status}] {len(self.turns)} turns, "
            f"{self.reads} reads (free), {self.writes} writes (tool), "
            f"{self.total_elapsed_s:.1f}s total"
        )


@dataclass
class RecentRead:
    command: str
    output: str


MAX_MUTATION_COMMANDS_PER_TURN = 4
MAX_CONSECUTIVE_COMMANDLESS_TURNS = 2


class TreeLoop:
    """LLM agent loop over the ContextTree playground OS."""

    def __init__(
        self,
        *,
        model: ModelClient,
        workspace_root: Path,
        max_turns: int = 30,
        checkpoint_interval: int = 0,
        checkpoint_callback: Optional[Callable[["TreeLoop", int], bool]] = None,
        get_fact_records: Optional[Callable[[], Sequence[Any]]] = None,
        get_memory_items: Optional[Callable[[], Sequence[Any]]] = None,
        get_status: Optional[Callable[[], Dict[str, Any]]] = None,
        steering: str = "",
        verbose: bool = True,
        tool_dispatcher: Optional[Callable[[CommandResult], Any]] = None,
        command_observer: Optional[Callable[[str, CommandResult], None]] = None,
        model_event_observer: Optional[Callable[[Dict[str, Any]], None]] = None,
        allow_shell: Optional[bool] = None,
    ) -> None:
        self.model = model
        self.max_turns = max_turns
        self.checkpoint_interval = checkpoint_interval
        self.checkpoint_callback = checkpoint_callback
        self.steering = steering
        self.verbose = verbose
        self.tool_dispatcher = tool_dispatcher
        self.command_observer = command_observer
        self.model_event_observer = model_event_observer
        self.allow_shell = _env_flag_enabled("SHELL_ACCESS", True) if allow_shell is None else bool(allow_shell)
        self.workspace_root = workspace_root.resolve()
        self._external_status_provider = get_status or (lambda: {})
        self._base_steering = steering
        self._checkpoint_steering = ""
        self._signal_steering = ""
        self._signal_state: Dict[str, Any] = {
            "active_signal": "",
            "meta_signal": "",
            "signal_version": 0,
            "current_focus_issue_id": "",
            "unresolved_issue_count": 0,
            "resolved_issue_count": 0,
            "issue_status_map": {},
            "raw_signals": [],
            "latest_diagnostics": None,
        }

        self.bridge = ContextTreeBridge(
            workspace_root=workspace_root,
            get_fact_records=get_fact_records or (lambda: []),
            get_memory_items=get_memory_items or (lambda: []),
            get_status=self._combined_status,
        )
        self.history: List[Turn] = []
        self._recent_reads: List[RecentRead] = []
        self._total_reads = 0
        self._total_writes = 0
        self._same_turn_halt_reason = ""

    def setup(self, *, max_files: int = 5000) -> Dict[str, Any]:
        """Index workspace and initial sync."""
        return self.bridge.setup(max_files=max_files)

    def register_skill(self, name: str, description: str, **kwargs: Any) -> None:
        self.bridge.register_skill(name, description, **kwargs)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, task: str) -> LoopResult:
        """Run the agent loop until finish or max turns."""
        if not self.bridge._indexed:
            self.setup()

        start_time = time.time()
        system = self._system_prompt()
        finish_message = ""
        finished = False
        interrupted = False
        commandless_turns = 0
        self._refresh_log_issue_signals()

        for turn_num in range(1, self.max_turns + 1):
            # Build prompt with current OS state + history
            os_state = self.bridge.render_for_prompt(repo_depth=2)
            prompt_steering = self._compose_steering()
            prompt = _build_user_prompt(
                task=task,
                os_state=os_state,
                history=self.history,
                recent_reads=self._recent_reads,
                steering=prompt_steering,
            )

            if self.verbose:
                _log(f"── Turn {turn_num}/{self.max_turns} ──")

            # Call LLM
            t0 = time.time()
            if self.model_event_observer is not None:
                try:
                    self.model_event_observer({"event": "model_call_start", "turn": turn_num})
                except Exception:
                    pass
            try:
                raw_output = self.model.complete(system, prompt)
            except KeyboardInterrupt:
                if self.model_event_observer is not None:
                    try:
                        self.model_event_observer({"event": "model_call_interrupted", "turn": turn_num})
                    except Exception:
                        pass
                interrupted = True
                finish_message = f"interrupted by operator during model call (turn {turn_num})"
                if self.verbose:
                    _log(f"  ■ INTERRUPTED during model call (turn {turn_num})")
                break
            except Exception as exc:
                if self.model_event_observer is not None:
                    try:
                        self.model_event_observer(
                            {
                                "event": "model_call_error",
                                "turn": turn_num,
                                "error": str(exc),
                            }
                        )
                    except Exception:
                        pass
                raise
            if self._checkpoint_steering:
                self._checkpoint_steering = ""
            llm_elapsed = time.time() - t0
            if self.model_event_observer is not None:
                try:
                    self.model_event_observer(
                        {
                            "event": "model_call_finish",
                            "turn": turn_num,
                            "elapsed_s": round(llm_elapsed, 3),
                            "output_chars": len(raw_output),
                        }
                    )
                except Exception:
                    pass

            if self.verbose:
                _log(f"LLM responded ({llm_elapsed:.1f}s, {len(raw_output)} chars)")

            # Extract and execute commands
            command_block = extract_commands(raw_output)
            commands_issued: List[str] = []
            results: List[CommandResult] = []
            turn_annotations: List[Annotation] = []

            if command_block:
                if is_strategy(command_block):
                    # Execute as strategy DAG
                    # First extract any annotations from the block
                    strategy_plan = parse_strategy(command_block)
                    for line in command_block.splitlines():
                        stripped = line.strip()
                        if is_annotation(stripped):
                            from tree_commands import parse_annotation
                            ann = parse_annotation(stripped)
                            if ann:
                                turn_annotations.append(ann)
                    by_label = self.bridge.execute_strategy_full(command_block)
                    for label in sorted(by_label.keys()):
                        step_commands = strategy_plan.steps[label].commands if strategy_plan and label in strategy_plan.steps else []
                        for index, r in enumerate(by_label[label]):
                            command_preview = step_commands[index] if index < len(step_commands) else r.command_type
                            commands_issued.append(f"[{label}] {command_preview}")
                            results.append(r)
                    if self.verbose:
                        for ann in turn_annotations:
                            _log(f"  {ann.compact()}")
                        _log(format_strategy_results(by_label))
                else:
                    # Execute as plain commands (annotations extracted automatically)
                    results = self.bridge.execute(command_block)
                    turn_annotations = self.bridge.last_annotations
                    commands_issued = parse_multi_command(command_block)
                    if self.verbose:
                        for ann in turn_annotations:
                            _log(f"  {ann.compact()}")
                        for cmd, r in zip(commands_issued, results):
                            if r.command_type == "annotation":
                                continue
                            ok = "✓" if r.ok else "✗"
                            tag = "READ" if r.command_type == "read" else (
                                "TOOL" if r.needs_tool else r.command_type.upper()
                            )
                            _log(f"  [{ok} {tag}] {_truncate_command_for_cli(cmd)}")
            else:
                if self.verbose:
                    _log("  (no commands extracted)")

            executable_results = [candidate for candidate in results if candidate.command_type != "annotation"]
            if not executable_results:
                commandless_turns += 1
                output_excerpt = " ".join(str(raw_output or "").strip().split())
                if len(output_excerpt) > 180:
                    output_excerpt = output_excerpt[:180] + "..."
                guidance = (
                    "No executable tree commands extracted from model output. "
                    "Do not respond with numbered prose steps or an `I will...` plan. "
                    "Start the next turn with `>>th:` and `>>pl:`, then emit executable commands such as "
                    "`cat /repo/file`, `s1: cat /repo/file`, or `finish <summary>`."
                )
                if output_excerpt:
                    guidance += f" Output excerpt: {output_excerpt}"
                commands_issued.append("model_output_invalid")
                results.append(CommandResult(ok=False, output=guidance, command_type="error"))
            else:
                commandless_turns = 0

            # Dispatch tool-requiring results and surface every command result.
            self._same_turn_halt_reason = ""
            blocked_reason = ""
            bounded_mutation_count = 0
            last_executable_index = -1
            for idx, candidate in enumerate(results):
                if candidate.command_type != "annotation":
                    last_executable_index = idx

            for index, r in enumerate(results):
                if r.command_type == "annotation":
                    if self.command_observer is not None:
                        try:
                            command_preview = commands_issued[index] if index < len(commands_issued) else ""
                            self.command_observer(command_preview, r)
                        except Exception:
                            pass
                    continue

                if blocked_reason:
                    r.ok = False
                    r.output = f"skipped after prior command failure: {blocked_reason}"
                    continue

                if r.command_type == "finish" and index != last_executable_index:
                    r.ok = False
                    r.output = "finish must be the final executable command in a turn"

                if not r.ok:
                    if self.command_observer is not None:
                        try:
                            command_preview = commands_issued[index] if index < len(commands_issued) else ""
                            self.command_observer(command_preview, r)
                        except Exception:
                            pass
                    blocked_reason = r.output or "command failed"
                    continue

                if r.command_type == "read":
                    self._total_reads += 1
                elif r.needs_tool:
                    if self._is_bounded_mutation_result(r):
                        bounded_mutation_count += 1
                        if bounded_mutation_count > MAX_MUTATION_COMMANDS_PER_TURN:
                            r.ok = False
                            r.output = (
                                f"mutation batch limit exceeded: at most {MAX_MUTATION_COMMANDS_PER_TURN} "
                                "mutating commands are allowed in one turn"
                            )
                            blocked_reason = r.output
                            continue
                    self._total_writes += 1
                    executed = self._execute_tool(r)
                    if executed:
                        r.output = executed  # feed back into history
                        if self.verbose:
                            _log(f"  ⟶ {_truncate_for_cli(executed)}")
                if self.command_observer is not None:
                    try:
                        command_preview = commands_issued[index] if index < len(commands_issued) else ""
                        self.command_observer(command_preview, r)
                    except Exception:
                        pass
                if not r.ok and not blocked_reason:
                    blocked_reason = r.output or "command failed"
                if self._same_turn_halt_reason and not blocked_reason:
                    blocked_reason = self._same_turn_halt_reason

            elapsed = time.time() - t0
            turn = Turn(
                turn_number=turn_num,
                raw_output=raw_output,
                commands_issued=commands_issued,
                results=results,
                annotations=turn_annotations,
                elapsed_s=elapsed,
            )
            self.history.append(turn)
            self._capture_recent_reads(turn)
            self._refresh_log_issue_signals()

            if commandless_turns >= MAX_CONSECUTIVE_COMMANDLESS_TURNS:
                finish_message = (
                    "stopped: model produced no executable tree commands for "
                    f"{commandless_turns} consecutive turns"
                )
                if self.verbose:
                    _log(f"  ■ STOPPED: {finish_message}")
                break

            # Check for finish
            if turn.has_finish:
                finished = True
                for r in results:
                    if r.command_type == "finish":
                        finish_message = r.output
                        break
                if self.verbose:
                    _log(f"  ✓ FINISHED: {finish_message}")
                break

            # Show thought for verbose mode
            if self.verbose and turn.thought:
                thought_preview = turn.thought[:200]
                if len(turn.thought) > 200:
                    thought_preview += "…"
                _log(f"  Thought: {thought_preview}")

            # Checkpoint: pause every N turns and ask whether to continue
            if (
                self.checkpoint_interval > 0
                and self.checkpoint_callback is not None
                and turn_num % self.checkpoint_interval == 0
                and turn_num < self.max_turns
            ):
                # Inject a >>ju: request into steering for the next turn
                self._checkpoint_steering = (
                    "[CHECKPOINT] You have used "
                    + str(turn_num)
                    + " turns. Emit >>ju: explaining what remains and why you need more turns."
                )
                proceed = self.checkpoint_callback(self, turn_num)
                if not proceed:
                    finished = False
                    finish_message = f"stopped by operator at checkpoint (turn {turn_num})"
                    if self.verbose:
                        _log(f"  ■ STOPPED at checkpoint (turn {turn_num})")
                    break

        if interrupted:
            finished = False

        total_elapsed = time.time() - start_time
        result = LoopResult(
            turns=self.history,
            finished=finished,
            finish_message=finish_message,
            total_elapsed_s=total_elapsed,
            reads=self._total_reads,
            writes=self._total_writes,
        )

        if self.verbose:
            _log(result.summary())

        return result

    def _capture_recent_reads(self, turn: Turn, *, max_items: int = 8) -> None:
        captured: List[RecentRead] = []
        for cmd, result in zip(turn.commands_issued, turn.results):
            if not self._is_file_context_command(cmd, result):
                continue
            captured.append(RecentRead(command=cmd, output=result.output))
        if not captured:
            return
        self._recent_reads.extend(captured)
        self._recent_reads = self._recent_reads[-max_items:]

    def _normalize_recent_command(self, command: str) -> str:
        stripped = command.strip()
        if stripped.startswith("[s") and "] " in stripped:
            return stripped.split("] ", 1)[1].strip()
        return stripped

    def _parse_recent_file_command(self, command: str) -> Optional[Tuple[str, str, int, int]]:
        normalized = self._normalize_recent_command(command)
        if normalized.startswith("cat /repo/"):
            target = normalized[len("cat "):].strip()
            path_part = target
            start_line = 0
            end_line = 0
            last_segment = target.rsplit("/", 1)[-1]
            if ":" in last_segment:
                path_part, range_part = target.rsplit(":", 1)
                if "-" not in range_part:
                    return None
                start_text, end_text = range_part.split("-", 1)
                try:
                    start_line = int(start_text)
                    end_line = int(end_text)
                except ValueError:
                    return None
            rel_path = path_part.removeprefix("/repo/").strip()
            return ("cat", rel_path, start_line, end_line)

        if normalized.startswith("read-line-range /repo/") or normalized.startswith("read_line_range /repo/"):
            parts = normalized.split()
            if len(parts) < 3:
                return None
            path_part = parts[1]
            range_part = parts[2]
            if "-" not in range_part:
                return None
            start_text, end_text = range_part.split("-", 1)
            try:
                start_line = int(start_text)
                end_line = int(end_text)
            except ValueError:
                return None
            rel_path = path_part.removeprefix("/repo/").strip()
            return ("read-line-range", rel_path, start_line, end_line)

        return None

    def _refresh_recent_file_context(self, rel_path: str) -> None:
        normalized_path = str(rel_path or "").strip().removeprefix("/repo/").removeprefix("repo/")
        if not normalized_path:
            return

        refreshed: List[RecentRead] = []
        for item in self._recent_reads:
            parsed = self._parse_recent_file_command(item.command)
            if not parsed:
                refreshed.append(item)
                continue

            command_type, command_path, start_line, end_line = parsed
            if command_path != normalized_path:
                refreshed.append(item)
                continue

            repo_path = f"/repo/{normalized_path}"
            if command_type == "cat":
                output = self.bridge.tree.cat(repo_path, start_line=start_line, end_line=end_line)
            else:
                output = self.bridge.tree.read_line_range(repo_path, start_line, end_line, include_line_numbers=True)
            refreshed.append(RecentRead(command=item.command, output=output))

        self._recent_reads = refreshed

    def _is_file_context_command(self, command: str, result: CommandResult) -> bool:
        if not result.ok or result.command_type != "read":
            return False
        lowered = command.lower()
        return (
            "cat /repo/" in lowered
            or "read-line-range /repo/" in lowered
            or "read_line_range /repo/" in lowered
            or "grep /repo/" in lowered
            or "symbols /repo/" in lowered
            or "find-symbol /repo/" in lowered
            or "find_symbol /repo/" in lowered
            or "show-issue " in lowered
            or lowered.startswith("[s") and (
                "cat /repo/" in lowered
                or "read-line-range /repo/" in lowered
                or "read_line_range /repo/" in lowered
                or "grep /repo/" in lowered
                or "symbols /repo/" in lowered
                or "find-symbol /repo/" in lowered
                or "find_symbol /repo/" in lowered
                or "show-issue " in lowered
            )
        )

    def _is_bounded_mutation_result(self, result: CommandResult) -> bool:
        action = result.tool_action or {}
        action_type = str(action.get("type", "") or "")
        return action_type in {
            "write_file",
            "replace_lines",
            "patch_file",
            "git_add",
            "git_restore",
            "git_commit",
        }

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _system_prompt(self) -> str:
        grammar = self.bridge.render_command_grammar()
        shell_note = "\nSHELL ACCESS POLICY: SHELL_ACCESS=false. Do not emit `shell <command>` or any action that dispatches to `run_shell`. Prefer structured file, git, and diagnostics commands.\n" if not self.allow_shell else ""
        return (_SYSTEM_PROMPT_TEMPLATE + shell_note).format(command_grammar=grammar)

    def _combined_status(self) -> Dict[str, Any]:
        status = {
            "task_satisfied": False,
            "edit_batch_mode": False,
            "step": len(self.history),
        }
        try:
            external = self._external_status_provider()
            if isinstance(external, dict):
                status.update(external)
        except Exception:
            pass
        status.update(self._signal_state)
        return status

    def _compose_steering(self) -> str:
        parts = [part for part in [self._base_steering, self._signal_steering, self._checkpoint_steering] if part]
        return "\n".join(parts)

    def _refresh_log_issue_signals(self) -> None:
        issues = self.bridge.tree.list_log_issues()
        status_map = {str(issue.get("id", "")): str(issue.get("status", "open")) for issue in issues if issue.get("id")}
        previous_map = dict(self._signal_state.get("issue_status_map", {}))
        unresolved = [issue for issue in issues if str(issue.get("status", "open")) != "resolved"]
        resolved = [issue for issue in issues if str(issue.get("status", "open")) == "resolved"]

        raw_signals: List[str] = []
        previous_unresolved = [issue_id for issue_id, status in previous_map.items() if status != "resolved"]
        changed_to_resolved = [
            issue_id
            for issue_id, status in status_map.items()
            if previous_map.get(issue_id) != "resolved" and status == "resolved"
        ]
        newly_added = [issue_id for issue_id in status_map if issue_id not in previous_map]
        for issue_id in newly_added:
            if status_map.get(issue_id) != "resolved":
                raw_signals.append(f"issue_ingested:{issue_id}")
        for issue_id in changed_to_resolved:
            raw_signals.append(f"issue_resolved:{issue_id}")

        current_focus = str(self._signal_state.get("current_focus_issue_id", ""))
        unresolved_ids = [str(issue.get("id", "")) for issue in unresolved]
        if current_focus not in unresolved_ids:
            if current_focus:
                raw_signals.append("focus_invalidated")
            current_focus = unresolved_ids[0] if unresolved_ids else ""

        meta_signal = ""
        if previous_unresolved and not unresolved:
            meta_signal = "all_issues_resolved"
        elif len(unresolved) > 1:
            meta_signal = "issue_batch_ready"
        elif len(unresolved) == 1:
            meta_signal = "single_issue_focus_ready"
        elif raw_signals:
            meta_signal = "issue_signal_update"

        self._signal_state["issue_status_map"] = status_map
        self._signal_state["current_focus_issue_id"] = current_focus
        self._signal_state["unresolved_issue_count"] = len(unresolved)
        self._signal_state["resolved_issue_count"] = len(resolved)
        self._signal_state["raw_signals"] = raw_signals
        if raw_signals:
            self._signal_state["active_signal"] = raw_signals[0]
            self._signal_state["signal_version"] = int(self._signal_state.get("signal_version", 0)) + 1
        elif not unresolved and not issues:
            self._signal_state["active_signal"] = ""
        self._signal_state["meta_signal"] = meta_signal

        if unresolved:
            focus_issue = next((issue for issue in unresolved if str(issue.get("id", "")) == current_focus), unresolved[0])
            focus_summary = str(focus_issue.get("summary", focus_issue.get("message", current_focus)))
            if meta_signal == "issue_batch_ready":
                issue_ids = ", ".join(unresolved_ids[:5])
                self._signal_steering = "\n".join([
                    f"[SIGNAL {meta_signal}] There are {len(unresolved)} unresolved parsed issue(s).",
                    f"[RAW SIGNALS] {', '.join(raw_signals[:6]) if raw_signals else 'open_issue_set'}",
                    f"[ISSUE FOCUS] Current focus: {current_focus} — {focus_summary}",
                    f"Read the issue set before acting. Prefer a strategy block that inspects multiple issues in parallel, for example: `s1: show-issue {issue_ids.split(', ')[0]}` and sibling `show-issue` steps for the other issue ids.",
                    "After the strategy read, pick one issue to fix, verify it, and only then use `resolve-issue <id>`.",
                ])
            else:
                self._signal_steering = "\n".join([
                    f"[SIGNAL {meta_signal or 'open_issues_present'}] There are {len(unresolved)} unresolved parsed issue(s).",
                    f"[RAW SIGNALS] {', '.join(raw_signals[:6]) if raw_signals else 'focus_ready'}",
                    f"[ISSUE FOCUS] Current focus: {current_focus} — {focus_summary}",
                    "Use `show-issue <id>` to inspect the focused issue, read the referenced repo files, fix one issue at a time, and only use `resolve-issue <id>` after verification.",
                ])
            return

        if issues:
            self._signal_steering = "\n".join([
                f"[SIGNAL {meta_signal or 'all_issues_resolved'}] All parsed log issues are resolved.",
                f"[RAW SIGNALS] {', '.join(raw_signals[:6]) if raw_signals else 'resolved_issue_set'}",
                "Verify the workspace end-to-end, then use `finish` with a concise summary if the trace-driven work is complete.",
            ])
            self._signal_state["current_focus_issue_id"] = ""
            return

        self._signal_steering = ""
        self._signal_state["active_signal"] = ""
        self._signal_state["meta_signal"] = ""
        self._signal_state["current_focus_issue_id"] = ""
        self._signal_state["unresolved_issue_count"] = 0
        self._signal_state["resolved_issue_count"] = 0
        self._signal_state["raw_signals"] = []
        self._signal_state["latest_diagnostics"] = None

    # ------------------------------------------------------------------
    # Built-in tool execution (writes to disk)
    # ------------------------------------------------------------------

    def _execute_tool(self, r: CommandResult) -> str:
        """Execute a tool action. Returns a status string.

        If a custom tool_dispatcher is set, delegates to it.
        Otherwise uses the built-in executor for write_file, replace_lines, patch_file,
        and shell commands.
        """
        if self.tool_dispatcher:
            try:
                dispatched = self.tool_dispatcher(r)
                if dispatched is None:
                    return "dispatched to custom handler"
                return str(dispatched)
            except Exception as e:
                return f"dispatch error: {e}"

        action = r.tool_action
        if not action:
            return ""

        action_type = action.get("type", "")

        if action_type == "write_file":
            message = self._exec_write_file(action)
        elif action_type == "replace_lines":
            message = self._exec_replace_lines(action)
        elif action_type == "patch_file":
            message = self._exec_patch_file(action)
        elif action_type in {
            "replace_range",
            "replace_snippet",
            "insert_before",
            "insert_after",
            "delete_range",
            "delete_snippet",
            "append_block",
            "prepend_block",
            "replace_symbol",
            "insert_symbol_member",
            "rename_symbol",
            "move_block",
            "create_file",
            "delete_file",
            "rename_file",
            "copy_file",
            "fill_template",
            "batch_mutate",
        }:
            message = self._exec_explicit_mutation(action)
        elif action_type == "run_shell":
            message = self._exec_run_shell(action)
        elif action_type == "diagnose":
            message = self._exec_diagnose(action)
        elif action_type == "run_check":
            message = self._exec_run_check(action)
        elif action_type == "run_route_check":
            message = self._exec_run_route_check(action)
        elif action_type == "begin_edit_batch":
            message = self._exec_begin_edit_batch()
        elif action_type == "end_edit_batch":
            message = self._exec_end_edit_batch()
        elif action_type == "finish":
            message = ""  # finish is handled by the loop
        elif action_type == "set_fact":
            message = ""  # facts are already written to the tree
        else:
            message = f"unhandled action type: {action_type}"

        if self._tool_message_indicates_failure(action_type, message):
            r.ok = False
        return message

    def _tool_message_indicates_failure(self, action_type: str, message: str) -> bool:
        lowered = str(message or "").strip().lower()
        if not lowered:
            return False
        if action_type == "run_shell":
            return lowered.startswith("run_shell: timed out") or lowered.startswith("run_shell: error executing")
        if action_type == "diagnose":
            return lowered.startswith("diagnose:")
        if action_type == "run_check":
            return lowered.startswith("run_check:")
        return lowered.startswith(f"{action_type}:")

    def _exec_begin_edit_batch(self) -> str:
        self._signal_state["edit_batch_mode"] = True
        return "edit batch started; group related fixes, then verify before ending the batch"

    def _exec_end_edit_batch(self) -> str:
        self._signal_state["edit_batch_mode"] = False
        return "edit batch ended; run a finite verification step if the related fixes are ready"

    def _exec_write_file(self, action: Dict[str, Any]) -> str:
        rel_path = action.get("path", "")
        content = action.get("content", "")
        if not rel_path:
            return "write_file: missing path"

        # Resolve against workspace root
        target = self.workspace_root / rel_path
        # Safety: must stay inside workspace
        try:
            target.resolve().relative_to(self.workspace_root)
        except ValueError:
            return f"write_file: path escapes workspace: {rel_path}"

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        # Invalidate tree cache so next cat sees the new content
        self.bridge.on_write_complete(rel_path)
        self._refresh_recent_file_context(rel_path)
        size = len(content.encode("utf-8"))
        return self._append_backend_diagnostics(
            f"wrote {rel_path} ({size} bytes)",
            rel_path,
            trigger_action="write",
        )

    def _exec_replace_lines(self, action: Dict[str, Any]) -> str:
        rel_path = str(action.get("path", "") or "")
        content = str(action.get("content", ""))
        try:
            start_line = int(action.get("start_line", 0))
            end_line = int(action.get("end_line", 0))
        except Exception:
            return "replace_lines: invalid start/end line"

        if not rel_path:
            return "replace_lines: missing path"
        if start_line <= 0 or end_line <= 0 or end_line < start_line:
            return "replace_lines: invalid line range"

        target = self.workspace_root / rel_path
        try:
            target.resolve().relative_to(self.workspace_root)
        except ValueError:
            return f"replace_lines: path escapes workspace: {rel_path}"

        if not target.exists():
            return f"replace_lines: file not found: {rel_path}"

        original_text = target.read_text()
        lines = original_text.splitlines()
        if start_line > len(lines):
            return f"replace_lines: start line out of bounds for {rel_path} ({len(lines)} total lines)"

        replacement_lines = content.splitlines()
        updated_lines = lines[: start_line - 1] + replacement_lines + lines[end_line:]
        updated = "\n".join(updated_lines)
        if updated_lines and original_text.endswith("\n"):
            updated += "\n"

        target.write_text(updated)
        self.bridge.on_write_complete(rel_path)
        self._refresh_recent_file_context(rel_path)
        replaced_end = min(end_line, len(lines))
        original_span = max(0, replaced_end - start_line + 1)
        replacement_span = len(replacement_lines)
        line_delta = replacement_span - original_span
        verify_start = max(1, start_line - 2)
        verify_end = max(verify_start, start_line + replacement_span + 2)
        return self._append_backend_diagnostics(
            f"replaced lines {start_line}-{replaced_end} in {rel_path} "
            f"with {replacement_span} line(s); "
            f"line delta {line_delta:+d}; "
            f"re-read lines {verify_start}-{verify_end}",
            rel_path,
            trigger_action="replace-lines",
        )

    def _exec_patch_file(self, action: Dict[str, Any]) -> str:
        rel_path = action.get("path", "")
        search = action.get("search", "")
        replace = action.get("replace", "")
        if not rel_path or not search:
            return "patch_file: missing path or search"

        target = self.workspace_root / rel_path
        try:
            target.resolve().relative_to(self.workspace_root)
        except ValueError:
            return f"patch_file: path escapes workspace: {rel_path}"

        if not target.exists():
            return f"patch_file: file not found: {rel_path}"

        original = target.read_text()
        if search not in original:
            return f"patch_file: search text not found in {rel_path}"

        updated = original.replace(search, replace, 1)
        target.write_text(updated)
        self.bridge.on_write_complete(rel_path)
        self._refresh_recent_file_context(rel_path)
        return self._append_backend_diagnostics(f"patched {rel_path}", rel_path, trigger_action="patch")

    def _exec_explicit_mutation(self, action: Dict[str, Any]) -> str:
        action_type = str(action.get("type", "") or "")
        root = self.workspace_root
        result: Dict[str, Any]

        if action_type == "replace_range":
            result = replace_range(str(action.get("path", "")), int(action.get("start_line", 0)), int(action.get("end_line", 0)), str(action.get("new_text", "") or ""), root=root)
        elif action_type == "replace_snippet":
            result = replace_snippet(str(action.get("path", "")), str(action.get("old_text", "") or ""), str(action.get("new_text", "") or ""), expected_occurrences=int(action.get("expected_occurrences", 1) or 1), replace_all=bool(action.get("all")), root=root)
        elif action_type == "insert_before":
            result = insert_before(str(action.get("path", "")), str(action.get("anchor_text", "") or ""), str(action.get("new_text", "") or ""), expected_occurrences=int(action.get("expected_occurrences", 1) or 1), root=root)
        elif action_type == "insert_after":
            result = insert_after(str(action.get("path", "")), str(action.get("anchor_text", "") or ""), str(action.get("new_text", "") or ""), expected_occurrences=int(action.get("expected_occurrences", 1) or 1), root=root)
        elif action_type == "delete_range":
            result = delete_range(str(action.get("path", "")), int(action.get("start_line", 0)), int(action.get("end_line", 0)), root=root)
        elif action_type == "delete_snippet":
            result = delete_snippet(str(action.get("path", "")), str(action.get("text", "") or ""), expected_occurrences=int(action.get("expected_occurrences", 1) or 1), root=root)
        elif action_type == "append_block":
            result = append_block(str(action.get("path", "")), str(action.get("new_text", "") or ""), root=root)
        elif action_type == "prepend_block":
            result = prepend_block(str(action.get("path", "")), str(action.get("new_text", "") or ""), root=root)
        elif action_type == "replace_symbol":
            result = replace_symbol(str(action.get("path", "")), str(action.get("symbol_name", "") or ""), str(action.get("symbol_kind", "") or ""), str(action.get("new_text", "") or ""), root=root)
        elif action_type == "insert_symbol_member":
            result = insert_symbol_member(str(action.get("path", "")), str(action.get("container_symbol", "") or ""), str(action.get("member_text", "") or ""), position=str(action.get("position", "end") or "end"), root=root)
        elif action_type == "rename_symbol":
            result = rename_symbol(str(action.get("path", "")), str(action.get("old_name", "") or ""), str(action.get("new_name", "") or ""), scope=str(action.get("scope", "file") or "file"), root=root)
        elif action_type == "move_block":
            result = move_block(str(action.get("path", "")), int(action.get("start_line", 0)), int(action.get("end_line", 0)), str(action.get("destination_anchor", "") or ""), position=str(action.get("position", "after") or "after"), root=root)
        elif action_type == "create_file":
            result = create_file(str(action.get("path", "")), str(action.get("content", "") or ""), overwrite=bool(action.get("overwrite")), root=root)
        elif action_type == "delete_file":
            result = delete_file(str(action.get("path", "")), root=root)
        elif action_type == "rename_file":
            result = rename_file(str(action.get("old_path", "") or ""), str(action.get("new_path", "") or ""), root=root)
        elif action_type == "copy_file":
            result = copy_file(str(action.get("source_path", "") or ""), str(action.get("destination_path", "") or ""), overwrite=bool(action.get("overwrite")), root=root)
        elif action_type == "fill_template":
            slots = action.get("slots")
            result = fill_template(str(action.get("path", "")), slots if isinstance(slots, dict) else {}, root=root)
        elif action_type == "batch_mutate":
            operations = action.get("operations")
            result = batch_mutate(operations if isinstance(operations, list) else [], atomic=bool(action.get("atomic")), root=root)
        else:
            return f"{action_type}: unsupported mutation action"

        if not bool(result.get("ok")):
            diagnostics = result.get("diagnostics") or []
            if isinstance(diagnostics, list) and diagnostics and isinstance(diagnostics[0], dict):
                message = str(diagnostics[0].get("message", result.get("reason", "mutation failed")) or "mutation failed")
            else:
                message = str(result.get("reason", "mutation failed") or "mutation failed")
            return f"{action_type}: {message}"

        if action_type == "batch_mutate":
            applied_count = int(result.get("applied_count", 0) or 0)
            return f"batch_mutate: applied {applied_count} operation(s)"

        rel_path = str(result.get("file_path", "") or action.get("path", "") or action.get("new_path", "") or action.get("destination_path", "") or "")
        if rel_path:
            self.bridge.on_write_complete(rel_path)
            self._refresh_recent_file_context(rel_path)

        summary = action_type.replace("_", " ")
        if action_type == "delete_file":
            return f"deleted {rel_path}"
        if action_type == "rename_file":
            return f"renamed {action.get('old_path')} -> {action.get('new_path')}"
        if action_type == "copy_file":
            destination = str(action.get("destination_path", "") or rel_path)
            return self._append_backend_diagnostics(f"copied {action.get('source_path')} -> {destination}", destination, trigger_action="copy")
        if action_type == "create_file":
            return self._append_backend_diagnostics(f"created {rel_path}", rel_path, trigger_action="create")
        if rel_path:
            return self._append_backend_diagnostics(f"{summary} {rel_path}", rel_path, trigger_action=action_type)
        return f"{summary} completed"

    def _append_backend_diagnostics(self, message: str, rel_path: str, *, trigger_action: str) -> str:
        diagnostic_message = self._run_backend_diagnostics_for_path(rel_path, trigger_action=trigger_action)
        if not diagnostic_message:
            return message
        return f"{message}\n{diagnostic_message}"

    def _run_backend_diagnostics_for_path(self, rel_path: str, *, trigger_action: str) -> str:
        try:
            run = run_backend_diagnostics(self.workspace_root, path=rel_path, limit=8, timeout=30)
        except ValueError:
            return ""

        diagnostics = [dict(item) for item in run.diagnostics]
        if diagnostics:
            self.bridge.tree.ingest_diagnostic_content(
                json.dumps(diagnostics, indent=2),
                source_path=f"[diagnose:{run.path}]",
            )
            self._signal_state["latest_diagnostics"] = {
                "path": run.path,
                "message": f"Backend {run.engine} diagnostics found {len(diagnostics)} issue(s) in {run.path} after {trigger_action}.",
                "diagnostic_engine": run.engine,
                "diagnostics": diagnostics,
                "source": "backend",
            }
            lines = [f"backend_diagnostics engine={run.engine} path={run.path} issues={len(diagnostics)}"]
            for item in diagnostics[:8]:
                lines.append(
                    f"- {item.get('path', run.path)}:{item.get('line', '')}:{item.get('column', '')} {item.get('code', '')} {item.get('message', '')}".rstrip()
                )
            return "\n".join(lines)

        self._signal_state["latest_diagnostics"] = None
        if run.returncode == 0:
            return ""

        lines = [f"backend_diagnostics_warning engine={run.engine} path={run.path} exit_code={run.returncode}"]
        stdout = _strip_ansi(run.stdout).strip()
        stderr = _strip_ansi(run.stderr).strip()
        if stdout:
            lines.append(f"stdout:\n{stdout}")
        if stderr:
            lines.append(f"stderr:\n{stderr}")
        return "\n".join(lines)

    def _rewrite_repo_shell_paths(self, command: str) -> str:
        try:
            tokens = shlex.split(command, posix=True)
        except Exception:
            return command

        rewritten: List[str] = []
        for token in tokens:
            if token == "/repo":
                rewritten.append(str(self.workspace_root))
                continue
            if token.startswith("/repo/"):
                repo_rel = token.removeprefix("/repo/")
                rewritten.append(str((self.workspace_root / repo_rel).resolve()))
                continue
            rewritten.append(token)
        return shlex.join(rewritten)

    def _maybe_git_rm_hint(self, command: str, result: subprocess.CompletedProcess[str]) -> str:
        normalized = str(command or "").strip()
        if not normalized.startswith("git rm"):
            return ""
        stderr = str(result.stderr or "")
        lowered = stderr.lower()
        if "pathspec" in lowered or "did not match any files" in lowered or "fatal:" in lowered:
            return "Hint: if the file is already untracked or already gone, use `shell rm -f <path>` instead."
        return ""

    def _exec_run_shell(self, action: Dict[str, Any]) -> str:
        if not self.allow_shell:
            command = str(action.get("command", "") or "").strip().lower()
            if command.startswith(_PACKAGE_MANAGER_INSTALL_HINT_PREFIXES):
                return (
                    "run_shell: disabled by SHELL_ACCESS=false\n"
                    "Use the structured `npm <args>` command instead, for example `npm install react`, "
                    "which runs behind explicit operator approval."
                )
            return "run_shell: disabled by SHELL_ACCESS=false"
        command = str(action.get("command", "") or "").strip()
        if not command:
            return "run_shell: missing command"
        if _is_long_running_shell_command(command):
            return (
                "run_shell: refused long-running dev/watch command: "
                f"{command}\n"
                "Use a finite diagnostic command instead, such as `npm run build`, "
                "`npm run lint`, or a targeted test command."
            )

        command = self._rewrite_repo_shell_paths(command)

        try:
            result = subprocess.run(
                command,
                cwd=str(self.workspace_root),
                shell=True,
                capture_output=True,
                text=True,
                env=_subprocess_env_no_color(),
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return f"run_shell: timed out after 60s: {command}"
        except Exception as exc:
            return f"run_shell: error executing command: {exc}"

        stdout = _strip_ansi(result.stdout).strip()
        stderr = _strip_ansi(result.stderr).strip()
        parts: List[str] = [f"exit_code={result.returncode}"]
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        git_rm_hint = self._maybe_git_rm_hint(command, result)
        if git_rm_hint:
            parts.append(git_rm_hint)
        if len(parts) == 1:
            parts.append("(no output)")
        return "\n".join(parts)

    def _detect_package_manager(self) -> str:
        if (self.workspace_root / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (self.workspace_root / "yarn.lock").exists():
            return "yarn"
        return "npm"

    def _normalize_npm_command(self, command: str) -> Tuple[List[str], str]:
        raw = str(command or "").strip()
        if not raw:
            raise ValueError("npm_command: missing command")
        tokens = shlex.split(raw)
        if not tokens:
            raise ValueError("npm_command: missing command")

        manager = self._detect_package_manager()
        subcommand = str(tokens[0] or "").strip().lower()
        args = tokens[1:]

        if subcommand == "install":
            if manager == "pnpm":
                return [manager, "add", *args], manager
            if manager == "yarn":
                return [manager, "add", *args], manager
            return [manager, "install", *args], manager
        if subcommand in {"uninstall", "remove"}:
            if manager == "npm":
                return [manager, "uninstall", *args], manager
            return [manager, "remove", *args], manager
        if subcommand == "ci":
            if manager != "npm":
                raise ValueError(f"npm_command: `ci` is only supported for npm workspaces, not {manager}")
            return [manager, "ci", *args], manager

        raise ValueError(
            "npm_command: only dependency-management commands are supported. "
            "Use `npm install ...`, `npm uninstall ...`, or `npm ci`."
        )

    def _exec_npm_command(self, action: Dict[str, Any]) -> str:
        try:
            argv, manager = self._normalize_npm_command(str(action.get("command", "") or ""))
        except ValueError as exc:
            return str(exc)

        try:
            result = subprocess.run(
                argv,
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                env=_subprocess_env_no_color(),
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return f"npm_command: timed out after 180s: {' '.join(argv)}"
        except Exception as exc:
            return f"npm_command: error executing command: {exc}"

        stdout = _strip_ansi(result.stdout).strip()
        stderr = _strip_ansi(result.stderr).strip()
        parts: List[str] = [f"manager={manager}", f"exit_code={result.returncode}"]
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        if len(parts) == 2:
            parts.append("(no output)")
        if result.returncode != 0:
            return "npm_command: command failed\n" + "\n".join(parts)
        return "\n".join(parts)

    def _detect_check_command(self, kind: str) -> Optional[str]:
        package_json = self.workspace_root / "package.json"
        scripts: Dict[str, Any] = {}
        if package_json.exists():
            try:
                package_data = json.loads(package_json.read_text())
                if isinstance(package_data, dict) and isinstance(package_data.get("scripts"), dict):
                    scripts = package_data["scripts"]
            except Exception:
                scripts = {}

        package_manager = "npm"
        if (self.workspace_root / "pnpm-lock.yaml").exists():
            package_manager = "pnpm"
        elif (self.workspace_root / "yarn.lock").exists():
            package_manager = "yarn"

        def run_script(script_name: str) -> str:
            if package_manager == "yarn":
                return f"yarn {script_name}"
            return f"{package_manager} run {script_name}"

        if kind == "build":
            if "build" in scripts:
                return run_script("build")
            return None
        if kind == "lint":
            if "lint" in scripts:
                return run_script("lint")
            return None
        if kind == "typecheck":
            for script_name in ("typecheck", "check-types", "check:types", "types"):
                if script_name in scripts:
                    return run_script(script_name)
            if "lint" in scripts and "tsc" in str(scripts["lint"]):
                return run_script("lint")
            if (self.workspace_root / "tsconfig.json").exists():
                return "npx tsc --noEmit"
            return None
        if kind == "test":
            if "test" in scripts:
                return run_script("test")
            return None
        return None

    def _resolve_route_check_url(self, target: str, base_url: str = "") -> str:
        target = str(target or "").strip()
        base_url = str(base_url or "").strip() or "http://127.0.0.1:3000"
        if target.startswith("http://") or target.startswith("https://"):
            return target
        if not target.startswith("/"):
            target = "/" + target
        return base_url.rstrip("/") + target

    def _exec_run_check(self, action: Dict[str, Any]) -> str:
        kind = str(action.get("kind", "") or "").strip().lower()
        if kind not in {"build", "lint", "typecheck", "test"}:
            return f"run_check: unsupported kind: {kind}"

        command = self._detect_check_command(kind)
        if not command:
            return f"run_check: no suitable {kind} command found"

        try:
            result = subprocess.run(
                command,
                cwd=str(self.workspace_root),
                shell=True,
                capture_output=True,
                text=True,
                env=_subprocess_env_no_color(),
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            return f"run_check: timed out after 90s: {command}"
        except Exception as exc:
            return f"run_check: error executing {command}: {exc}"

        stdout = _strip_ansi(result.stdout).strip()
        stderr = _strip_ansi(result.stderr).strip()
        combined = "\n".join(part for part in [stdout, stderr] if part).strip()
        issues = self.bridge.tree.ingest_diagnostic_content(combined, source_path=f"[run-check:{kind}]")

        parts = [f"command={command}", f"exit_code={result.returncode}", f"issues={len(issues)}"]
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        if not stdout and not stderr:
            parts.append("(no output)")
        return "\n".join(parts)

    def _exec_diagnose(self, action: Dict[str, Any]) -> str:
        path = str(action.get("path", "") or "").strip()
        if not path:
            return "diagnose: missing path"
        try:
            limit = int(action.get("limit", 8) or 8)
        except Exception:
            limit = 8

        try:
            run = run_backend_diagnostics(
                self.workspace_root,
                path=path,
                limit=max(1, min(limit, 50)),
                timeout=30,
            )
        except ValueError as exc:
            return f"diagnose: {exc}"

        diagnostics = [dict(item) for item in run.diagnostics]
        if diagnostics:
            issues = self.bridge.tree.ingest_diagnostic_content(
                json.dumps(diagnostics, indent=2),
                source_path=f"[diagnose:{run.path}]",
            )
            self._signal_state["latest_diagnostics"] = {
                "path": run.path,
                "message": f"Backend {run.engine} diagnostics found {len(diagnostics)} issue(s) in {run.path}.",
                "diagnostic_engine": run.engine,
                "diagnostics": diagnostics,
                "source": "backend",
            }
            parts = [
                f"engine={run.engine}",
                f"path={run.path}",
                f"issues={len(issues)}",
                f"exit_code={run.returncode}",
                "diagnostics:\n" + "\n".join(
                    f"- {item.get('path', run.path)}:{item.get('line', '')}:{item.get('column', '')} {item.get('code', '')} {item.get('message', '')}".rstrip()
                    for item in diagnostics[:8]
                ),
            ]
            return "\n".join(parts)

        self._signal_state["latest_diagnostics"] = None
        parts = [
            f"engine={run.engine}",
            f"path={run.path}",
            "issues=0",
        ]
        stdout = _strip_ansi(run.stdout).strip()
        stderr = _strip_ansi(run.stderr).strip()
        if run.returncode != 0 and not stdout and not stderr:
            parts.append("exit_code=0")
            return "\n".join(parts)
        parts.append(f"exit_code={run.returncode}")
        if run.returncode != 0:
            if stdout:
                parts.append(f"stdout:\n{stdout}")
            if stderr:
                parts.append(f"stderr:\n{stderr}")
            return "diagnose: " + "\n".join(parts)
        return "\n".join(parts)

    def _exec_run_route_check(self, action: Dict[str, Any]) -> str:
        target = str(action.get("target", "") or "").strip()
        if not target:
            return "run_route_check: missing target route or URL"

        base_url = str(action.get("base_url", "") or "").strip()
        url = self._resolve_route_check_url(target, base_url)
        script = """
const fs = require('fs');
const { chromium } = require('playwright');

async function main() {
  const url = process.argv[2];
  const target = process.argv[3];
  const result = {
    url,
    route: target,
    finalUrl: url,
    consoleMessages: [],
    pageErrors: [],
    requestFailures: [],
    responseErrors: [],
    title: "",
  };

  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();

  page.on('console', (msg) => {
    const type = msg.type();
    if (type === 'error' || type === 'warning') {
      result.consoleMessages.push({
        type,
        text: msg.text(),
        location: msg.location ? msg.location() : {},
      });
    }
  });

  page.on('pageerror', (error) => {
    result.pageErrors.push({
      message: error && error.message ? error.message : String(error),
      stack: error && error.stack ? error.stack : "",
    });
  });

  page.on('requestfailed', (request) => {
    const failure = request.failure();
    result.requestFailures.push({
      url: request.url(),
      method: request.method(),
      errorText: failure && failure.errorText ? failure.errorText : 'request failed',
    });
  });

  page.on('response', async (response) => {
    if (response.status() >= 400) {
      result.responseErrors.push({
        url: response.url(),
        status: response.status(),
        statusText: response.statusText(),
      });
    }
  });

  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 15000 });
    await page.waitForTimeout(1000);
    result.finalUrl = page.url();
    result.title = await page.title();
  } finally {
    await browser.close();
  }

  process.stdout.write(JSON.stringify(result));
}

main().catch((error) => {
  process.stderr.write(error && error.stack ? error.stack : String(error));
  process.exit(1);
});
""".strip()
        temp_script: Optional[str] = None
        try:
            with tempfile.NamedTemporaryFile("w", suffix=".cjs", delete=False) as fh:
                fh.write(script)
                temp_script = fh.name
            result = subprocess.run(
                ["node", temp_script, url, target],
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                timeout=45,
            )
        except subprocess.TimeoutExpired:
            return f"run_route_check: timed out after 45s: {url}"
        except Exception as exc:
            return f"run_route_check: error executing browser check: {exc}"
        finally:
            if temp_script:
                try:
                    Path(temp_script).unlink(missing_ok=True)
                except Exception:
                    pass

        if result.returncode != 0:
            stderr = result.stderr.strip()
            if "Cannot find module 'playwright'" in stderr or "Cannot find package 'playwright'" in stderr:
                return (
                    f"run_route_check: playwright is not installed in {self.workspace_root}\n"
                    "Install Playwright in the target app workspace or provide a workspace that already has it."
                )
            return f"run_route_check: failed for {url}\nstderr:\n{stderr or '(no stderr)'}"

        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            return f"run_route_check: invalid JSON output: {exc}"

        issues = self.bridge.tree.ingest_browser_diagnostics(payload, source_path=f"[run-route-check:{target}]")
        parts = [
            f"url={payload.get('url', url)}",
            f"final_url={payload.get('finalUrl', url)}",
            f"title={payload.get('title', '')}",
            f"issues={len(issues)}",
            f"console={len(payload.get('consoleMessages', []) or [])}",
            f"page_errors={len(payload.get('pageErrors', []) or [])}",
            f"request_failures={len(payload.get('requestFailures', []) or [])}",
            f"http_errors={len(payload.get('responseErrors', []) or [])}",
        ]
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(msg, file=sys.stderr)


_LONG_RUNNING_SHELL_PATTERNS = (
    ("npm", "run", "dev"),
    ("npm", "run", "start"),
    ("pnpm", "dev"),
    ("pnpm", "start"),
    ("yarn", "dev"),
    ("yarn", "start"),
    ("vite",),
    ("next", "dev"),
    ("webpack", "serve"),
)

_PACKAGE_MANAGER_INSTALL_HINT_PREFIXES = (
    "npm install",
    "npm uninstall",
    "npm remove",
    "pnpm add",
    "pnpm remove",
    "yarn add",
    "yarn remove",
)

_CLI_LOG_PREVIEW_CHARS = 600
_CLI_COMMAND_PREVIEW_CHARS = 220
_ANSI_ESCAPE_RE = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")


def _truncate_for_cli(text: str, limit: int = _CLI_LOG_PREVIEW_CHARS) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… ({len(text) - limit} chars truncated)"


def _strip_ansi(text: str) -> str:
    return _ANSI_ESCAPE_RE.sub("", str(text or ""))


def _subprocess_env_no_color() -> Dict[str, str]:
    env = dict(os.environ)
    env["NO_COLOR"] = "1"
    env["FORCE_COLOR"] = "0"
    env["CLICOLOR"] = "0"
    env["CLICOLOR_FORCE"] = "0"
    return env


def _truncate_command_for_cli(command: str, limit: int = _CLI_COMMAND_PREVIEW_CHARS) -> str:
    command = " ".join(str(command).split())
    replace_prefixes = ("replace-lines ", "replace_lines ", "write ")
    if command.startswith(replace_prefixes) and len(command) > limit:
        parts = command.split(None, 2)
        if len(parts) >= 2:
            verb = parts[0]
            target = parts[1]
            content_len = len(parts[2]) if len(parts) >= 3 else 0
            return f"{verb} {target} [content omitted in history; {content_len} chars]"
    if len(command) <= limit:
        return command
    return command[:limit] + f" … ({len(command) - limit} chars truncated)"


def _shell_command_tokens(command: str) -> List[str]:
    try:
        return [token.lower() for token in shlex.split(command) if token.strip()]
    except Exception:
        return [token.lower() for token in str(command).split() if token.strip()]


def _has_token_sequence(tokens: Sequence[str], sequence: Sequence[str]) -> bool:
    if not sequence or len(tokens) < len(sequence):
        return False
    width = len(sequence)
    for index in range(len(tokens) - width + 1):
        if tuple(tokens[index : index + width]) == tuple(sequence):
            return True
    return False


def _is_long_running_shell_command(command: str) -> bool:
    tokens = _shell_command_tokens(command)
    if not tokens:
        return False
    return any(_has_token_sequence(tokens, sequence) for sequence in _LONG_RUNNING_SHELL_PATTERNS)
