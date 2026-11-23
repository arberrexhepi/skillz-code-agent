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
import hashlib
from issue_proposals import IssueProposalStore, PROPOSAL_GUIDANCE
from diagnostic_provenance import DiagnosticProvenance
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Protocol, Sequence, Tuple, TypedDict

from context_tree_bridge import ContextTreeBridge
from artifact_preview import check_artifact_route, preview_guidance, run_artifact_preview
from discovery.dispatch import DISCOVERY_ACTION_TYPES, execute_discovery_action
from mcp_runtime import invoke_mcp, mcp_guidance, model_mcp_result
from mutation_text import PARSE_ERROR_PREFIX, is_text_mutation
from mutation_runtime import EXPLICIT_MUTATION_TYPES, execute_mutation, compact_mutation_result
from mutations.staging import StageConflict, StagedFiles, anchored_range
from execution_evidence import repository_observation
from project_diagnostics import run_backend_diagnostics
from model_result_delivery import ReadWorkingSet, prepare_model_result, render_packet
from model_output_progress import ModelOutputObserver, ModelOutputProgress
from goal_handoff import HANDOFF_READ_CHARS, summarize_handoff
from repository_changes import RepositorySnapshot
from shell_processes import PACKAGE_SHELL_GUIDANCE, npm_script, run_managed_shell
from provider_response import (
    GENERATION_LIMIT_REASONS, PROTOCOL_FAILURES, RESPONSE_BUDGET_FAILURES,
    CommandResponseLimit, CommandWaveBoundary,
    ProviderResponseError, ProviderResponseRecovery,
)
from command_blocks import BEGIN_COMMANDS, COMMAND_FRAME_RULE, END_COMMANDS, CommandResponseScan, group_command_blocks, normalize_command_header
from tree_commands import (
    Annotation,
    COMMAND_VERBS,
    CommandResult,
    has_strategy_placeholders,
    is_annotation,
    is_strategy,
    parse_multi_command,
    parse_annotation,
    parse_strategy,
)


_BATCH_DIAGNOSTIC_INSPECTIONS = frozenset({
    "cat", "read-line-range", "read-context", "read-symbol", "find-regex", "grep", "repo-map", "symbols", "find-symbol",
    "list-issues", "list-run-issues", "show-issue", "show-run-issue", "diagnose",
})
_BATCH_DIAGNOSTICS_LIMIT = 8


def _model_error_metadata(exc: Exception) -> Dict[str, Any]:
    status_code: Optional[int] = None
    for value in (
        getattr(exc, "status_code", None),
        getattr(exc, "status", None),
        getattr(exc, "code", None),
    ):
        try:
            candidate = int(value)
        except (TypeError, ValueError):
            continue
        if 100 <= candidate <= 599:
            status_code = candidate
            break
    if status_code is None:
        match = re.search(r"(?:error code|status(?: code)?|http)\s*[:=]?\s*(\d{3})\b", str(exc), re.IGNORECASE)
        if match:
            status_code = int(match.group(1))

    request_id = str(getattr(exc, "request_id", "") or "").strip()
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if not request_id and headers is not None:
        for name in ("x-request-id", "request-id", "x-meta-request-id"):
            try:
                request_id = str(headers.get(name, "") or "").strip()
            except Exception:
                request_id = ""
            if request_id:
                break
    retry_attempts = int(getattr(exc, "retry_attempts", 0) or 0)
    retry_request_ids = list(getattr(exc, "retry_request_ids", []) or [])
    retry_delays_s = list(getattr(exc, "retry_delays_s", []) or [])
    rate_limit_headers = dict(getattr(exc, "rate_limit_headers", {}) or {})
    retry_after = str(getattr(exc, "retry_after", "") or "").strip()
    fresh_context_retry = bool(getattr(exc, "fresh_context_retry", False))
    return {
        "status_code": status_code,
        "request_id": request_id,
        "retry_attempts": retry_attempts,
        "retry_request_ids": retry_request_ids,
        "retry_delays_s": retry_delays_s,
        "rate_limit_headers": rate_limit_headers,
        "retry_after": retry_after,
        "fresh_context_retry": fresh_context_retry,
    }


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

class ChatMessage(TypedDict):
    role: str
    content: str


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
    # Annotations: >>tag: content. A bare heredoc terminator (>>>) is not a command.
    if is_annotation(line):
        return True
    if is_strategy(line):
        return True
    first = line.split(None, 1)[0].lower() if line else ""
    return first in COMMAND_VERBS


_INLINE_ANNOTATION_RE = re.compile(r">>\w+:\s*")


def _strip_leading_control_preamble(raw: str) -> str:
    """Drop prose attached directly before the first annotation marker.

    Some compatible models prefix an otherwise valid response with a short
    sentence and omit the newline before ``>>th:``. Only text before the first
    annotation is removed; command-like prefixes and heredoc content are left
    untouched so normalization cannot broaden execution authority.
    """
    text = str(raw or "")
    match = _INLINE_ANNOTATION_RE.search(text)
    if match is None or match.start() == 0:
        return text
    preamble = text[:match.start()]
    if "<<<" in preamble:
        return text
    if any(_looks_like_command(_normalize_command_candidate(line)) for line in preamble.splitlines()):
        return text
    return text[match.start():]


def _normalize_command_candidate(line: str) -> str:
    """Recover executable command lines wrapped in common Markdown prose syntax."""
    candidate = normalize_command_header(line)
    return candidate if is_text_mutation(candidate) or is_strategy(candidate) else candidate.rstrip()


def _has_executable_command(command_block: str) -> bool:
    if not str(command_block or "").strip():
        return False
    return any(
        not is_annotation(command.strip())
        for command in parse_multi_command(command_block)
        if command.strip()
    )


def _build_output_repair_instruction(format_error: str = "") -> str:
    if format_error:
        return (
            f"FORMAT REPAIR REQUIRED: {format_error}\n"
            "No commands from that response were executed. Rewrite the rejected command or strategy with its original intent. "
            "Include all intended steps, with every strategy dependency defined. Do not skip the malformed step or replace it with `finish`. "
            "Return only the corrected Playground OS commands and optional >>th: and >>pl: annotations."
        )
    return "\n".join(
        [
            "FORMAT REPAIR REQUIRED: your previous response contained no executable Playground OS command.",
            "Rewrite it as an executable turn using the same intent and current context.",
            "If you proposed a code change, emit that edit as patch, replace-lines, edit-symbol, or edit-batch now. Do not replace the intended edit with another broad read.",
            "Return only this shape:",
            ">>th: <brief reasoning>",
            ">>pl: <one concrete action>",
            "<at least one executable command>",
            "Put each annotation and each executable command on its own line; never attach prose before `>>th:`.",
            "Valid commands include `patch /repo/path \"exact source\" -> \"replacement\"`, "
            "`run-check build`, `read-context /repo/path \"exact anchor\" before=4 after=8 limit=1`, and `finish <summary>`.",
            "Do not return bullets, a numbered plan, a promise to act later, a bare JSON action, or a fenced code block. Command arguments may contain JSON where the grammar permits it.",
        ]
    )


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """\
You are a precise coding agent operating inside a playground OS.
NON-NEGOTIABLE EXECUTION CONTRACT: every response must contain at least one executable Playground OS command.
`>>th:` and `>>pl:` annotations do not count as executable commands. Never return prose-only planning.
Emit one wave per response: one brief `>>th:`, one `>>pl:`, then the commands you can justify from results already received.
End the response after that batch. Plan any work that depends on its results in the next turn after the host executes it.
{command_frame_hint}
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
  - The command parser is line-oriented: put each annotation and each executable command on its own line, with no prose before >>th:.
  - Keep >>pl: operational, not vague. Good: "Read lines 30-60 of DemoTodoContext.tsx, replace addTodo, then verify lines 30-60."
  - Use >>err: after a failed edit so the next turn can avoid repeating the same mistake.
  - Do not use >>dg: as a generic note. Use it only for a real if/then branch.

RULES:
1. READ commands (ls, cat, read-line-range, repo-map, symbols, find-symbol, stat, find, grep, read-diagnostics) run locally without tool dispatch.
    /repo reads and discovery use the real mounted workspace, independent of the bounded metadata preview or loaded file cache.
    Explicit paths can inspect dependency types under /repo/node_modules, including build directories; broad searches omit dependencies.
    Other mounts remain in-memory. Keep all reads focused and bounded even though they do NOT count as tool calls.
    Multi-line shell scripts use `shell <<<` followed by the script and a standalone `>>>` terminator.
    Native shell heredocs (`shell python3 <<'CHECK'` ... `CHECK`) and multi-line quoted shell arguments also stay one command.
    Script bodies are opaque: commands, annotations, commas and strategy labels inside them are source, not agent actions.
    Close all script delimiters and quotes before the next agent command; incomplete blocks dispatch nothing.
1c. For broad or unfamiliar codebase work, start with `repo-map /repo topic="<task topic>" limit=20` before line-by-line reads.
    Treat it as the structural layer of a retrieval funnel: use the ranked files, symbols, imports, and drill-down suggestions to
    choose targeted `symbols`, `find-symbol`, `grep`, `cat`, or `read-line-range` commands.
1a. Skills are part of your capability surface. Use `skill` with no arguments to discover what skills are available, and use
    `skill <name>` to load a skill payload into the run when it would improve task quality, style consistency, testing approach,
    or repair strategy. When skill choice is uncertain or the user asks for skills broadly, prefer an explicit `skill` discovery
    step before any direct `skill <name>` load so you can choose from the actual catalog.
1b. When the user explicitly mentions a skill, playbook, style, or workflow that sounds like a bundled Playground OS skill,
     proactively check available skills and load the best match yourself instead of waiting for the operator to force it.
2. WRITE commands (write, replace-lines, patch, shell, git, diagnose, run-check, run-route-check) dispatch to real tools. Use them
   deliberately after you have enough context from reads.
   Commands in a turn complete in order: a read after a write or shell command sees its changes.
   Strategy dependencies complete before downstream reads or output substitution. A failed command skips the rest of the turn.
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
7. When your task is complete, use `finish <summary message>` as the final command. Its summary can contain punctuation, including commas.
8. You may issue related file edits in one turn. The host dispatches commands in order and stops at the first failure.
   Prefer at most 8 focused reads. Turns with more than 32 commands other than file edits are rejected before dispatch.
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
11a. When line ranges feel unstable or a file has many nearby definitions, use `repo-map`, `symbols`, or `find-symbol` first to anchor on the right function, class, or variable before editing.
12. Prefer `replace-lines` for bounded edits. Prefer `write` when replacing most of a file or rebuilding a corrupted region wholesale.
12a. Use inline `replace-lines` only for short single-line replacements. If the replacement spans multiple lines or is longer than a short import/function call, use heredoc.
12b. Write/replace content is literal. Exactly one separator follows the path/range header; additional spaces are payload indentation. Heredocs preserve first-line indentation, trailing spaces, and blank lines. Always close them with >>>.
12c. For patch, prefer two JSON-quoted string operands separated by ` -> `. Escapes decode exactly once, and arrows within quoted source are not separators. Include enough source context for exactly one match; never copy displayed line numbers into search text. Missing or ambiguous matches make no changes.
12d. Put each text mutation in its own strategy step. Use left-side dependencies such as `s1 -> s2: patch ...`; mutation payload commas and trailing arrows are literal content, not strategy wiring.
13. Avoid rereading an entire large file after a localized edit unless you need whole-file structure. Verify the edited range first.
13a. Context compaction retains at most 48,000 characters of recent read packets. Large scans can evict earlier source; requesting them cannot pin previous ranges. Keep one bounded edit surface in view. Use read-symbol or read-context for a specific missing identifier instead of reloading every declaration before editing. Novel reads and context restoration are not task delivery; heed the execution delivery checkpoint and its focused-read allowance.
14. If a bounded edit fails twice or keeps duplicating content, stop stretching the same range edit. Re-read the surrounding region, widen the inspected range, and either do one clean `replace-lines` pass or rewrite the full file with `write`.
15. Strategy placeholders like `{{s1}}` are raw upstream text plus a few safe text transforms. You may use `.stdout`, `.trim()`, `.replace("old", "new")`, `.split('\n').filter(line => ...).join('\n')`, numeric indexing like `.split('\n')[0]`, and regex capture extraction like `.match(/pattern/)[1]`, but do not invent arbitrary code execution inside placeholders.
16. Use `batch start` before a cluster of related file fixes and `batch end` to validate and publish the staged edits. Edits run in order. Read staged source for an overlapping range changed by a prior edit, or use an exact patch. Run project checks after `batch end` publishes the files.
16a. After editing a `.ts`, `.tsx`, `.js`, `.jsx`, or `.py` file, prefer `diagnose <path>` when you need backend file-targeted diagnostics that do not rely on an editor.
16b. If diagnostics fail at `batch end`, the batch is closed. A queued `diagnose` for the same file can reuse that result. Use the findings and any queued inspection results to plan the repair, then rerun validation after editing.
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
# User prompt (rebuilt each turn, used as a compatibility fallback)
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


def _build_initial_context_message(task: str, os_state: str, *, steering: str = "") -> str:
    sections = [
        "═══ TASK ═══",
        task,
        "═══ PLAYGROUND OS STATE ═══",
        os_state,
    ]
    if steering:
        sections.extend(["═══ OPERATOR STEERING ═══", steering])
    sections.extend([
        "═══ YOUR TURN ═══",
        "Think, then act. Start with >>th: and >>pl:, then issue executable tree commands or one `s1:` strategy block. "
        "Do not return a numbered prose plan.",
    ])
    return "\n\n".join(sections)


def _build_turn_result_message(turn: Turn) -> str:
    return "\n\n".join([
        "═══ LATEST PLAYGROUND OS RESULT ═══",
        f"── Turn {turn.turn_number} ──\n" + "\n\n".join(
            render_packet(command, result) for command, result in zip(turn.commands_issued, turn.results)
            if result.command_type != "annotation"
        ),
        "═══ YOUR TURN ═══",
        "Continue from the latest result. Start with brief >>th: and >>pl:, then emit executable tree commands or `finish <summary>`.",
    ])


def _build_current_steering_message(steering: str) -> str:
    return "\n\n".join([
        "═══ CURRENT STEERING ═══",
        steering,
        "Apply this steering on the next executable command.",
    ])


def _message_chars(messages: Sequence[ChatMessage]) -> int:
    return sum(len(str(item.get("role", ""))) + len(str(item.get("content", ""))) for item in messages)


def _merge_adjacent_messages(messages: Sequence[ChatMessage]) -> List[ChatMessage]:
    merged: List[ChatMessage] = []
    for item in messages:
        role = str(item.get("role", "user") or "user")
        content = str(item.get("content", "") or "")
        if not content:
            continue
        if merged and merged[-1]["role"] == role:
            merged[-1]["content"] = merged[-1]["content"] + "\n\n" + content
        else:
            merged.append({"role": role, "content": content})
    return merged


def _build_context_summary_message(task: str, history: Sequence[Turn], os_state: str, *, steering: str = "", max_chars: int = 10000, retained_reads: str = "") -> str:
    parts: List[str] = [
        "═══ COMPACTED WORK SUMMARY ═══",
        "Older transcript messages were compacted to keep the stable system prompt cacheable.",
        "",
        "Current task:",
        task,
    ]
    if history:
        parts.extend(["", "Prior turns:"])
        budget = max_chars
        compact_parts: List[str] = []
        for turn in reversed(history):
            # Activity is a summary, never evidence of having read omitted code.
            compact = f"── Turn {turn.turn_number} ──\n" + "\n".join(
                f"[{'✓' if result.ok else '✗'} {result.command_type.upper()}] {_truncate_command_for_cli(command)}"
                + (f"\n{result.output[:800]}" if result.command_type in {"finish", "error"} or not result.ok else "")
                for command, result in zip(turn.commands_issued, turn.results)
            )
            if len(compact) > budget:
                compact = compact[:budget] + "\n… (older compacted work omitted)"
                compact_parts.insert(0, compact)
                break
            compact_parts.insert(0, compact)
            budget -= len(compact)
        parts.append("\n\n".join(compact_parts))
    parts.extend(["", "═══ RETAINED REPOSITORY READS ═══",
                  "Only the complete packets below remain read context. Other source may be requested again.",
                  retained_reads or "(none)"])
    parts.extend(["", "═══ PLAYGROUND OS STATE REFRESH ═══", os_state])
    if steering:
        parts.extend(["", "═══ OPERATOR STEERING ═══", steering])
    parts.extend([
        "",
        "═══ YOUR TURN ═══",
        "Continue from the compacted summary and current OS state. Start with brief >>th: and >>pl:, then emit executable tree commands or `finish <summary>`.",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Command extraction from LLM output
# ---------------------------------------------------------------------------

def extract_commands(raw: str, *, require_frame: bool = False) -> str:
    """Extract annotations and actual commands from an LLM response.

    Models often mix explanation with commands, sometimes wrapping the
    actionable lines in code fences. We keep only:
    - annotation lines (>>tag:)
    - lines that look like tree commands or strategy steps
    - heredoc bodies attached to an extracted command

    Free-form prose is ignored rather than being sent to the command parser.
    """
    text = raw if require_frame else _strip_leading_control_preamble(raw)
    lines = group_command_blocks(
        [line.removesuffix("\r") for line in text.split("\n")],
        response_scan=CommandResponseScan(require_frame=require_frame),
    )
    if lines and lines[0].startswith(PARSE_ERROR_PREFIX):
        return lines[0]
    extracted: List[str] = []
    pending_annotations: List[str] = []

    for line in lines:
        stripped = line.strip()

        if not stripped:
            continue

        candidate = _normalize_command_candidate(line)

        if is_annotation(candidate):
            pending_annotations.append(candidate)
            continue

        if _looks_like_command(candidate):
            if pending_annotations:
                extracted.extend(pending_annotations)
                pending_annotations = []
            extracted.append(candidate)
            continue

    if extracted:
        return "\n".join(extracted)
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


MAX_NON_EDIT_COMMANDS_PER_TURN = 32
MAX_CONSECUTIVE_COMMANDLESS_TURNS = 2
MAX_MODEL_OUTPUT_REPAIR_ATTEMPTS = 1


def _is_file_edit_command(command: str) -> bool:
    if is_text_mutation(command):
        return True
    parts = command.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "mutate":
        return False
    try:
        action = json.loads(parts[1])
    except ValueError:
        return False
    return isinstance(action, dict) and str(action.get("type") or "") in (
        EXPLICIT_MUTATION_TYPES | {"write_file", "replace_lines", "patch_file"}
    )


def _executable_commands(command_block: str) -> List[str]:
    """Inspect commands before execution without interpreting text payloads as commands."""
    if is_strategy(command_block):
        plan = parse_strategy(command_block)
        if plan is None:
            return []
        return [command for label, step in plan.steps.items() if label != "error" for command in step.commands]
    return [command for command in parse_multi_command(command_block) if not is_annotation(command)]


def _has_non_finish_command(command_block: str) -> bool:
    return any(command.split(None, 1)[0].lower() != "finish" for command in _executable_commands(command_block))


def _has_complete_recovery_wave(command_block: str) -> bool:
    commands = parse_multi_command(command_block)
    headers = [parse_annotation(command) for command in commands[:2]]
    return [header.tag for header in headers if header and header.content] == ["th", "pl"] and bool(
        _executable_commands(command_block)
    )


def _delivery_attempt_instruction(reason: str) -> str:
    return (
        f"FINAL DELIVERY ATTEMPT: {reason}\n"
        "Use only the retained evidence and the original goal. If you can make a concrete, in-scope file change, "
        "briefly state the edit strategy in `>>pl:`, then emit a grounded file edit such as `patch`, `replace-lines`, "
        "`replace-regex` with an exact match count, `write`, or an `edit-batch` of related mutations. "
        "Strategy steps (`s1:`, `s1 -> s2:`) may contain file edits only. "
        "Use exact anchors for existing code and do not reconstruct an existing file from partial source. "
        "Ignore earlier suggestions to gather more evidence. No reads, checks, shell commands, or MCP calls are allowed on this turn. "
        "If this goal does not call for a file change, or an edit cannot be grounded safely, emit `finish abstain` "
        "as the only command. Abstaining leaves the goal incomplete. Any edits will use the normal mutation and validation path."
    )


def _delivery_attempt_preflight(commands: Sequence[str]) -> Optional[CommandResult]:
    if len(commands) == 1 and re.fullmatch(r"finish\s+abstain", commands[0], re.IGNORECASE):
        return CommandResult(ok=True, output="The model abstained from the final edit attempt.", command_type="delivery_abstained")
    if commands and all(_is_file_edit_command(command) for command in commands):
        return None
    return CommandResult(
        ok=False, command_type="error",
        output="Final delivery attempt rejected. Expected only file edits, or `finish abstain`.",
    )


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
        read_context_observer: Optional[Callable[[Sequence[Tuple[str, CommandResult]]], None]] = None,
        turn_observer: Optional[Callable[[Turn], None]] = None,
        model_event_observer: Optional[Callable[[Dict[str, Any]], None]] = None,
        backend_check_observer: Optional[Callable[[str, bool, str, List[Dict[str, Any]]], None]] = None,
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
        self.read_context_observer = read_context_observer
        self.turn_observer = turn_observer
        self._read_working_set = ReadWorkingSet()
        self._repository_snapshot: Optional[RepositorySnapshot] = None
        self.model_event_observer = model_event_observer
        self.backend_check_observer = backend_check_observer
        self.allow_shell = _env_flag_enabled("SHELL_ACCESS", True) if allow_shell is None else bool(allow_shell)
        self.workspace_root = workspace_root.resolve()
        self._external_status_provider = get_status or (lambda: {})
        self._base_steering = steering
        self._checkpoint_steering = ""
        self._recovery_steering = ""
        self._signal_steering = ""
        self._signal_state: Dict[str, Any] = {
            "active_signal": "",
            "meta_signal": "",
            "signal_version": 0,
            "current_focus_issue_id": "",
            "current_focus_issue_namespace": "",
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
        self._conversation_messages: List[ChatMessage] = []
        self._conversation_task = ""
        self._conversation_history_start = 0
        self._handoff_summary = ""
        self.diagnostic_provenance = DiagnosticProvenance(self.workspace_root)
        self.bridge.tree.diagnostic_disposition = lambda diagnostics: IssueProposalStore(self.workspace_root).apply_deferrals(
            diagnostics, scope='task:' + hashlib.sha256(getattr(self, '_proposal_task', self._conversation_task).encode()).hexdigest(),
            may_defer=self.diagnostic_provenance.may_defer,
        )
        self._conversation_compaction_count = 0
        self._conversation_char_budget = int(os.getenv("TREELOOP_MESSAGE_CONTEXT_CHARS", "120000") or "120000")
        self._total_reads = 0
        self._total_writes = 0
        self._range_reads: dict[str, dict[int, tuple[str, str]]] = {}
        self._same_turn_halt_reason = ""
        self._external_stop_reason = ""
        self._delivery_attempt_reason = ""
        self.delivery_suspended = False
        self._inspected_run_issue_ids: set[str] = set()
        compactor_setter = getattr(self.model, "set_final_retry_message_compactor", None)
        if callable(compactor_setter):
            compactor_setter(self._compact_messages_for_final_retry)

    @property
    def require_command_frame(self) -> bool:
        return getattr(self.model, "command_response_framing", False) is True

    def setup(self, *, max_files: int = 5000) -> Dict[str, Any]:
        """Index workspace and initial sync."""
        return self.bridge.setup(max_files=max_files)

    def register_skill(self, name: str, description: str, **kwargs: Any) -> None:
        self.bridge.register_skill(name, description, **kwargs)

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    def run(self, task: str, *, resume: bool = False, diagnostic_provenance: Optional[DiagnosticProvenance] = None) -> LoopResult:
        """Run the agent loop until finish or max turns."""
        self._proposal_task = str(task or "").strip()
        if not resume:
            self.diagnostic_provenance = diagnostic_provenance or DiagnosticProvenance(self.workspace_root)
        if self._conversation_task and self._proposal_task != self._conversation_task:
            self.begin_goal_handoff()
        if not self.bridge._indexed:
            self.setup()

        start_time = time.time()
        system = self._system_prompt()
        finish_message = ""
        finished = False
        interrupted = False
        commandless_turns = 0
        self._external_stop_reason = ""
        self._delivery_attempt_reason = ""
        self.delivery_suspended = False
        self._refresh_log_issue_signals()

        turn_offset = self.history[-1].turn_number if resume and self.history else 0
        turn_limit = turn_offset + self.max_turns
        # Reserve one final delivery turn even when the read allowance expires on the last regular turn.
        for turn_num in range(turn_offset + 1, turn_limit + 2):
            delivery_attempt_reason = self._delivery_attempt_reason
            self._delivery_attempt_reason = ""
            if turn_num > turn_limit and not delivery_attempt_reason:
                break
            before_turn = getattr(self, "before_turn", None)
            if before_turn is not None:
                before_turn()
            # Both provider APIs consume this one model-visible transcript.
            os_state = self.bridge.render_for_prompt(repo_depth=2)
            prompt_steering = self._compose_steering()
            self._ensure_conversation_task(task, os_state, steering=prompt_steering)
            self._compact_conversation_if_needed(task, os_state, steering=prompt_steering)
            call_messages: List[ChatMessage] = list(self._conversation_messages)
            if prompt_steering and self.history:
                call_messages.append({"role": "user", "content": _build_current_steering_message(prompt_steering)})
            messages = _merge_adjacent_messages(call_messages)
            # Keep the one-turn contract in the system prompt so provider retries can compact the conversation safely.
            turn_system = system + "\n\n" + _delivery_attempt_instruction(delivery_attempt_reason) if delivery_attempt_reason else system

            if self.verbose:
                _log(f"── Turn {turn_num}/{turn_limit} ──")

            # Call LLM
            t0 = time.time()
            active_model_call_started = t0
            repair_attempts = 0
            provider_recovery = ProviderResponseRecovery()
            command_block = ""
            self._emit_live_progress("model_call_start", turn_num, delivery_attempt=bool(delivery_attempt_reason))
            try:
                raw_output, active_model_call_started, messages = self._complete_with_provider_recovery(
                    turn_system, messages, turn_num, active_model_call_started, provider_recovery,
                    delivery_attempt=bool(delivery_attempt_reason),
                )
                command_block = extract_commands(raw_output, require_frame=self.require_command_frame)
                format_error = self._command_format_error(command_block)
                if not delivery_attempt_reason and provider_recovery.discarded_action and not _has_non_finish_command(command_block):
                    format_error = format_error or "A provider-rejected action is still pending. Reissue the action before finishing."
                rejected_format_error = format_error
                if self.require_command_frame and format_error and not _has_non_finish_command(extract_commands(raw_output)):
                    # A framing-only error around a completion can be repaired as a completion.
                    rejected_format_error = ""
                if provider_recovery.budget_retries and not _has_executable_command(command_block):
                    format_error = format_error or "The focused response retry returned no executable command."
                while (
                    not delivery_attempt_reason
                    and not provider_recovery.budget_retries
                    and (format_error or not _has_executable_command(command_block))
                    and repair_attempts < MAX_MODEL_OUTPUT_REPAIR_ATTEMPTS
                ):
                    repair_attempts += 1
                    if self.model_event_observer is not None:
                        try:
                            self.model_event_observer(
                                {
                                    "event": "model_call_finish",
                                    "turn": turn_num,
                                    "elapsed_s": round(time.time() - active_model_call_started, 3),
                                    "output_chars": len(raw_output),
                                    "output_valid": False,
                                    "repair_pending": True,
                                    "repair_attempt": repair_attempts,
                                    "invalid_output_excerpt": raw_output[:8000],
                                }
                            )
                            self.model_event_observer(
                                {
                                    "event": "model_call_start",
                                    "turn": turn_num,
                                    "repair_attempt": repair_attempts,
                                }
                            )
                        except Exception:
                            pass
                    repair_instruction = _build_output_repair_instruction(format_error)
                    if self.require_command_frame:
                        repair_instruction += "\n" + COMMAND_FRAME_RULE
                    repair_output_excerpt = raw_output
                    if len(repair_output_excerpt) > 8000:
                        repair_output_excerpt = repair_output_excerpt[:8000] + "\n… (invalid output truncated)"
                    repair_messages = _merge_adjacent_messages(
                        [
                            *messages,
                            {"role": "assistant", "content": repair_output_excerpt},
                            {"role": "user", "content": repair_instruction},
                        ]
                    )
                    active_model_call_started = time.time()
                    raw_output, active_model_call_started, messages = self._complete_with_provider_recovery(
                        system, repair_messages, turn_num, active_model_call_started, provider_recovery,
                    )
                    command_block = extract_commands(raw_output, require_frame=self.require_command_frame)
                    format_error = self._command_format_error(command_block)
                    if (rejected_format_error or provider_recovery.discarded_action) and not format_error and not _has_non_finish_command(command_block):
                        format_error = rejected_format_error or "A provider-rejected action is still pending. Reissue the action before finishing."
            except ProviderResponseError as exc:
                if exc.budget_exhausted:
                    self.delivery_suspended = True
                finish_message = f"stopped: {exc}"
                break
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
                        metadata = _model_error_metadata(exc)
                        self.model_event_observer(
                            {
                                "event": "model_call_error",
                                "turn": turn_num,
                                "elapsed_s": time.time() - active_model_call_started,
                                "error": str(exc),
                                **metadata,
                            }
                        )
                    except Exception:
                        pass
                raise
            if self._checkpoint_steering:
                self._checkpoint_steering = ""
            if self._recovery_steering:
                self._recovery_steering = ""
            parsed_commands = _executable_commands(command_block) if command_block else []
            delivery_preflight = _delivery_attempt_preflight(parsed_commands) if delivery_attempt_reason else None
            llm_elapsed = time.time() - active_model_call_started
            if self.model_event_observer is not None:
                try:
                    self.model_event_observer(
                        {
                            "event": "model_call_finish",
                            "turn": turn_num,
                            "elapsed_s": round(llm_elapsed, 3),
                            "output_chars": len(raw_output),
                            "output_valid": not format_error and (delivery_preflight.ok if delivery_preflight is not None else _has_executable_command(command_block)),
                            "repair_attempt": repair_attempts,
                        }
                    )
                except Exception:
                    pass

            if self.verbose:
                repair_suffix = f", repaired after {repair_attempts} format retry" if repair_attempts else ""
                _log(f"LLM responded ({time.time() - t0:.1f}s, {len(raw_output)} chars{repair_suffix})")

            # Publish only explicit operator-facing annotations, never provider
            # reasoning or source text inside a write/patch/heredoc payload.
            if self.model_event_observer is not None:
                thoughts = []
                for command in parse_multi_command(command_block or ""):
                    annotation = parse_annotation(command)
                    if annotation is not None and annotation.tag == "th":
                        thoughts.append(annotation.content)
                if thoughts:
                    try:
                        self.model_event_observer({"event": "turn_thought", "turn": turn_num, "thought": "\n".join(thoughts)})
                    except Exception:
                        pass

            # Extract and execute commands
            commands_issued: List[str] = []
            results: List[CommandResult] = []
            turn_annotations: List[Annotation] = []
            batch_executed = False

            if format_error:
                commands_issued = ["command_batch_rejected"]
                results = [CommandResult(False, f"{format_error} No commands from this response were executed.", "error")]
            elif delivery_preflight is not None:
                commands_issued = ["delivery_attempt"]
                results = [delivery_preflight]
            elif command_block:
                executable_command_count = len(parsed_commands)
                has_extension = any(command.split(None, 1)[0].lower() == "request-discovery-extension" for command in parsed_commands)
                if has_extension and executable_command_count != 1:
                    commands_issued = ["command_batch_rejected"]
                    results = [CommandResult(ok=False, command_type="error", output="A discovery extension request must be the only executable command in its turn. No commands were executed.")]
                elif (non_edit_count := sum(not _is_file_edit_command(command) for command in parsed_commands)) > MAX_NON_EDIT_COMMANDS_PER_TURN:
                    commands_issued = ["command_batch_rejected"]
                    results = [CommandResult(
                        ok=False,
                        output=(
                            f"command batch limit exceeded: received {non_edit_count} commands other than file edits; "
                            f"at most {MAX_NON_EDIT_COMMANDS_PER_TURN} are allowed in one turn. "
                            "No commands from this batch were executed. Emit one focused command or a small bounded group."
                        ),
                        command_type="error",
                    )]
                    self._recovery_steering = (
                        "[RECOVERY] Your previous command batch was rejected before execution because it was unbounded. "
                        "Do not enumerate speculative filenames. Use one repo-map, find, grep, cat, or finish command, "
                        "or a small bounded group of directly relevant reads."
                    )
                else:
                    commands_issued, results, turn_annotations = self._execute_command_batch(command_block, turn=turn_num)
                    batch_executed = True
            else:
                if self.verbose:
                    _log("  (no commands extracted)")

            executable_results = [
                candidate
                for candidate in results
                if candidate.command_type not in {"annotation", "planning_only"}
            ]
            if not executable_results:
                commandless_turns += 1
                output_excerpt = " ".join(str(raw_output or "").strip().split())
                if len(output_excerpt) > 180:
                    output_excerpt = output_excerpt[:180] + "..."
                has_annotations = bool(turn_annotations) or any(
                    candidate.command_type == "annotation" for candidate in results
                )
                if has_annotations:
                    guidance = (
                        "Planning-only output accepted. You emitted thoughts/plan but no executable Playground OS command. "
                        "Proceed from that plan on the next turn: keep `>>th:` and `>>pl:` brief, then emit exactly one "
                        "executable command or strategy such as `cat /repo/file`, `repo-map /repo topic=\"...\"`, "
                        "`s1: cat /repo/file`, or `finish <discovery summary>`."
                    )
                    if self._recent_reads:
                        guidance += " Since recent file context is already available, prefer `finish <concise discovery summary>` if discovery is complete."
                    self._recovery_steering = (
                        "[RECOVERY] Your previous turn only emitted thoughts/planning. Continue from that plan now. "
                        "After brief `>>th:` and `>>pl:` annotations, emit an executable Playground OS command. "
                        "If enough context has been gathered, use `finish <concise summary>`."
                    )
                    commands_issued.append("planning_only")
                    results.append(CommandResult(ok=True, output=guidance, command_type="planning_only"))
                else:
                    guidance = (
                        "No executable tree commands extracted from model output. "
                        "Do not respond with numbered prose steps or an `I will...` plan. "
                        "Start the next turn with `>>th:` and `>>pl:`, then emit executable commands such as "
                        "`cat /repo/file`, `s1: cat /repo/file`, or `finish <summary>`."
                    )
                    commands_issued.append("model_output_invalid")
                    results.append(CommandResult(ok=False, output=guidance, command_type="error"))
                if output_excerpt:
                    guidance += f" Output excerpt: {output_excerpt}"
                    results[-1].output = guidance
            else:
                commandless_turns = 0

            if not batch_executed and self.command_observer is not None:
                for command, result in zip(commands_issued, results):
                    prepare_model_result(command, result)
                    try:
                        self.command_observer(command, result)
                    except Exception:
                        pass

            for command, result in zip(commands_issued, results):
                if not batch_executed:
                    prepare_model_result(command, result)
                    self._read_working_set.record(command, result)

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
            self._record_run_issue_inspections(turn)
            self._refresh_log_issue_signals()
            self._conversation_messages.extend([
                {"role": "assistant", "content": raw_output},
                {"role": "user", "content": _build_turn_result_message(turn)},
            ])
            if self.turn_observer is not None and delivery_preflight is None:
                self.turn_observer(turn)

            if self._external_stop_reason:
                finish_message = f"stopped: {self._external_stop_reason}"
                if self.verbose:
                    _log(f"  ■ STOPPED: {finish_message}")
                break

            if delivery_attempt_reason:
                if any((result.tool_action or {}).get("_changed_paths") for result in results):
                    if turn_num >= turn_limit:
                        self.delivery_suspended = True
                        finish_message = "Final delivery edits were applied. The turn limit was reached before the goal could finish."
                        break
                    continue
                self.delivery_suspended = True
                finish_message = f"stopped: {delivery_attempt_reason}"
                detail = next((result.output for result in results if not result.ok), "")
                if detail:
                    finish_message += f" Final delivery attempt: {_truncate_for_cli(detail, 500)}"
                elif delivery_preflight is None:
                    finish_message += " The final delivery attempt made no file changes."
                break

            if format_error:
                if provider_recovery.budget_retries:
                    self.delivery_suspended = True
                finish_message = f"stopped: malformed command response after {repair_attempts} format retry. {format_error}"
                break

            if self._delivery_attempt_reason:
                continue

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
                and (turn_num - turn_offset) % self.checkpoint_interval == 0
                and turn_num < turn_limit
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

        if not finished and not finish_message:
            finish_message = f"Turn limit reached: {self.max_turns} model turns in this attempt."
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

    def request_stop(self, reason: str) -> None:
        """Stop after the current turn without treating the run as completed."""
        self._external_stop_reason = str(reason or "").strip() or "external stop requested"

    def request_delivery_attempt(self, reason: str) -> None:
        """Ask for a final edit strategy using the context retained in this run."""
        self._delivery_attempt_reason = reason

    def request_delivery_pause(self, reason: str) -> None:
        """Suspend delivery while keeping the current goal context available for Resume."""
        self.delivery_suspended = True
        self.request_stop(reason)

    def reset_conversation(self) -> None:
        self._conversation_messages.clear()
        self._conversation_task = ""
        self._conversation_history_start = len(self.history)
        self._handoff_summary = ""
        self._checkpoint_steering = ""
        self._recovery_steering = ""
        self._conversation_compaction_count = 0
        self._inspected_run_issue_ids.clear()
        self._read_working_set = ReadWorkingSet()
        if self.read_context_observer is not None:
            self.read_context_observer([])

    def begin_goal_handoff(self) -> None:
        """Retain bounded evidence and outcomes, never the prior goal's raw dialogue."""
        self.discard_staged_edits()
        self._handoff_summary = summarize_handoff(self.history[self._conversation_history_start:])
        self._conversation_history_start = len(self.history)
        self._conversation_messages.clear()
        self._conversation_task = ""
        self._checkpoint_steering = ""
        self._recovery_steering = ""
        self._inspected_run_issue_ids.clear()
        packets = self._read_working_set.compact(budget=HANDOFF_READ_CHARS)
        self._recent_reads.clear()
        if self.read_context_observer is not None:
            self.read_context_observer(packets)

    def record_external_command(self, command: str, result: CommandResult) -> None:
        """Deliver operator-approved tool work through the same evidence path."""
        prepare_model_result(command, result)
        if self.command_observer is not None:
            self.command_observer(command, result)
        prepare_model_result(command, result)
        turn = Turn(turn_number=0, raw_output="", commands_issued=[command], results=[result], elapsed_s=0)
        self.history.append(turn)
        if self._conversation_task:
            self._conversation_messages.append({"role": "user", "content": "Operator-approved command result:\n" + render_packet(command, result)})
        else:
            self._handoff_summary = summarize_handoff(self.history[self._conversation_history_start:])
        if (result.tool_action or {}).get("_changed_paths"):
            self._total_writes += 1
            if self.turn_observer is not None:
                self.turn_observer(turn)

    def _ensure_conversation_task(self, task: str, os_state: str, *, steering: str = "") -> None:
        normalized = str(task or "").strip()
        if normalized and self._conversation_task and normalized != self._conversation_task:
            self.begin_goal_handoff()
        if not self._conversation_messages:
            self._inspected_run_issue_ids.clear()
            self._conversation_messages.append({
                "role": "user",
                "content": self._compacted_context(normalized, os_state, steering=steering, notify=False)
                           if self._handoff_summary else _build_initial_context_message(normalized, os_state, steering=steering),
            })
            self._conversation_task = normalized
            return

    def _compact_conversation_if_needed(self, task: str, os_state: str, *, steering: str = "") -> None:
        if self._conversation_char_budget <= 0:
            return
        if _message_chars(self._conversation_messages) <= self._conversation_char_budget:
            return
        self._conversation_compaction_count += 1
        self._conversation_messages = [{
            "role": "user",
            "content": self._compacted_context(task, os_state, steering=steering),
        }]
        self._conversation_task = str(task or "").strip()

    def _compacted_context(self, task: str, os_state: str, *, steering: str = "", notify: bool = True) -> str:
        packets = self._read_working_set.compact()
        summary = _build_context_summary_message(
            task, self.history[self._conversation_history_start:], os_state, steering=steering,
            retained_reads=("Host-retained context manifest (exact ranges; additional broad reads may evict these):\n"
                            + json.dumps(self._read_working_set.manifest(), ensure_ascii=False)
                            + "\n\n" + "\n\n".join(render_packet(*packet) for packet in packets)),
        )
        if notify and self.read_context_observer is not None:
            self.read_context_observer(packets)
        return (self._handoff_summary + "\n\n" if self._handoff_summary else "") + summary

    def _compact_messages_for_final_retry(
        self,
        _messages: Sequence[ChatMessage],
    ) -> Sequence[ChatMessage]:
        """Refresh provider input before the cooled-down final 500 retry."""
        os_state = self.bridge.render_for_prompt(repo_depth=2)
        steering = self._compose_steering()
        self._conversation_compaction_count += 1
        self._conversation_messages = [{
            "role": "user",
            "content": self._compacted_context(
                self._conversation_task,
                os_state,
                steering=steering,
            ),
        }]
        return list(self._conversation_messages)

    def _emit_live_progress(self, event: str, turn: int, **payload: Any) -> None:
        if self.model_event_observer is not None:
            try:
                self.model_event_observer({"event": event, "turn": turn, **payload})
            except Exception:
                pass

    def _complete_model(
        self, system: str, messages: Sequence[ChatMessage], *, turn: int, preserve_request_context: bool = False,
    ) -> str:
        complete_with_progress = getattr(self.model, "complete_messages_with_progress", None)
        if callable(complete_with_progress):
            streamed = False

            def on_summary(delta: str) -> None:
                nonlocal streamed
                if not delta:
                    streamed = False
                self._emit_live_progress("model_reasoning_summary", turn, thought=delta)

            def on_output(count: int, draft: list[str]) -> None:
                nonlocal streamed
                streamed = True
                self._emit_live_progress("model_output_progress", turn, output_chars=count, draft_strategy=draft)

            observer = ModelOutputObserver(on_output, preview_strategy=True, preserve_request_context=preserve_request_context,
                                           require_command_frame=self.require_command_frame)
            output = complete_with_progress(
                system, list(messages), on_summary, on_output=observer,
            )
            text = str(output or "")
            if text and not streamed:
                # Some backends deliver the response as a complete message.
                preview = ModelOutputProgress(observer)
                try:
                    preview.add(text)
                    preview.finish()
                except (CommandResponseLimit, CommandWaveBoundary):
                    # The caller checks the provider finish reason before slicing the reply.
                    pass
            return text
        complete_messages = getattr(self.model, "complete_messages", None)
        if callable(complete_messages):
            return str(complete_messages(system, list(messages)) or "")
        # Legacy providers receive exactly the same retained context as chat providers.
        transcript = "\n\n".join(f"═══ {item['role'].upper()} HISTORY ═══\n{item['content']}" for item in messages)
        return str(self.model.complete(system, transcript) or "")

    def _complete_with_provider_recovery(
        self, system: str, messages: Sequence[ChatMessage], turn: int,
        started: float, recovery: ProviderResponseRecovery, *, delivery_attempt: bool = False,
    ) -> Tuple[str, float, Sequence[ChatMessage]]:
        """Retry provider delivery in place without evicting source or spending execution turns."""
        retry_system = system
        base_messages = list(messages)
        last_protocol_reason = ""
        while True:
            limit_metadata: dict[str, Any] = {}
            try:
                output = self._complete_model(
                    retry_system, messages, turn=turn, preserve_request_context=recovery.generation_limit_retry,
                )
            except CommandWaveBoundary as exc:
                output = exc.text
                limit_metadata = {"wave_boundary": True}
            except CommandResponseLimit as exc:
                output = ""
                limit_metadata = exc.metadata
            metrics_getter = getattr(self.model, "get_last_metrics", None)
            try:
                metrics = metrics_getter() if callable(metrics_getter) else {}
            except Exception:
                metrics = {}
            metadata = {**((metrics or {}).get("response") or {}), **limit_metadata}
            reason = recovery.failure_reason(output, metadata)
            if not reason:
                # Check complete replies too, including CLI and non-streaming recovery.
                guard = ModelOutputProgress(ModelOutputObserver(lambda *_: None, preview_strategy=True,
                                                               require_command_frame=self.require_command_frame))
                try:
                    guard.add(output)
                    guard.finish()
                except CommandWaveBoundary as exc:
                    output = exc.text
                    metadata["wave_boundary"] = True
                except CommandResponseLimit as exc:
                    metadata.update(exc.metadata)
                    reason = str(exc)
            if not reason and metadata.get("wave_boundary"):
                wave = extract_commands(output)
                actions = _executable_commands(wave)
                terminal_commands = {"finish", "request-discovery-extension"}
                if not actions or any(command.split(None, 1)[0].lower() in terminal_commands for command in actions):
                    reason = "COMMAND_ANNOTATION_LIMIT"
            if not reason and recovery.generation_limit_retry and not _has_complete_recovery_wave(
                extract_commands(output, require_frame=self.require_command_frame),
            ):
                reason = "INCOMPLETE_RECOVERY_WAVE"
            if not reason:
                self._emit_live_progress(
                    "model_response_received", turn,
                    **({"summary": "Command wave ready to execute"} if metadata.get("wave_boundary") else {}),
                )
                return output, started, messages
            protocol_failure = reason in PROTOCOL_FAILURES
            budget_failure = reason in RESPONSE_BUDGET_FAILURES
            envelope_failure = reason == "INVALID_COMMAND_ENVELOPE"
            if protocol_failure:
                last_protocol_reason = reason
            if protocol_failure or budget_failure or envelope_failure:
                discarded = extract_commands(output)
                recovery.discarded_action |= bool(metadata.get("discarded_action")) or _has_non_finish_command(discarded) or bool(self._command_format_error(discarded))
            retry = recovery.claim_retry(reason)
            if self.model_event_observer is not None:
                try:
                    self.model_event_observer({
                        "event": "model_call_finish", "turn": turn,
                        "elapsed_s": round(time.time() - started, 3), "output_chars": metadata.get("output_chars", len(output)),
                        "output_valid": False, "empty_response": not output.strip() and not (budget_failure or envelope_failure),
                        "provider_response": metadata, "response_budget_failure": budget_failure,
                        "provider_protocol_failure": protocol_failure, "recovery_reason": reason,
                        "rejected_output_excerpt": output[:4000] if protocol_failure else "",
                        "provider_retry_pending": retry, "provider_recovery_attempts": recovery.retries,
                        "provider_recovery_exhausted": not retry, "preserved_source_context": True,
                        "empty_response_retry_pending": retry and reason == "EMPTY_RESPONSE",
                        "empty_response_recovery_exhausted": not retry and reason == "EMPTY_RESPONSE",
                    })
                except Exception:
                    pass
            if not retry:
                detail = f" Provider response {json.dumps(metadata, ensure_ascii=False)}" if metadata else ""
                if reason == "INCOMPLETE_RECOVERY_WAVE":
                    message = "The generation cap retry did not produce a complete thought, plan, and command wave. Current goal and source preserved."
                elif budget_failure:
                    message = f"command response budget exceeded ({reason}). Current goal and source preserved."
                elif recovery.generation_limit_retry:
                    message = f"The generation limit recovery response failed ({reason}). Current goal and source preserved."
                elif protocol_failure:
                    message = (
                        f"provider command delivery failed with {reason} after {recovery.protocol_retries} targeted retries. "
                        "Current goal and source preserved."
                    )
                elif last_protocol_reason:
                    message = (
                        f"provider command delivery failed with {last_protocol_reason} and the last retry returned no usable text. "
                        "Current goal and source preserved."
                    )
                elif envelope_failure:
                    message = "The provider returned an invalid structured command block after recovery. Current goal and source preserved."
                else:
                    message = "provider returned empty text twice after one retry preserving context."
                raise ProviderResponseError(message + detail, budget_exhausted=budget_failure or bool(recovery.budget_retries))
            partial = output if reason in GENERATION_LIMIT_REASONS and output.strip() else ""
            instruction = recovery.instruction(reason, has_partial_text=bool(partial))
            if self.require_command_frame:
                instruction += " " + COMMAND_FRAME_RULE
            if recovery.discarded_action and not delivery_attempt:
                instruction += " Reissue the unfinished action as a text command. A completion message cannot replace it."
            if delivery_attempt:
                instruction += " The final delivery contract still applies: emit only file edits, or use `finish abstain`."
            # Retry the same delivered context. Refreshing the request must not
            # compact the active goal's source merely because the provider erred.
            messages = list(base_messages)
            if partial:
                messages.append({"role": "assistant", "content": partial})
            messages.append({"role": "user", "content": instruction})
            messages = _merge_adjacent_messages(messages)
            retry_system = system
            if protocol_failure or budget_failure or envelope_failure:
                retry_system += "\n\nPROVIDER RECOVERY OVERRIDE FOR THIS REQUEST:\n" + instruction
            started = time.time()
            if self.model_event_observer is not None:
                try:
                    self.model_event_observer({"event": "model_call_start", "turn": turn,
                                               "fresh_context_retry": False, "preserved_source_context": True,
                                               "provider_recovery_attempts": recovery.retries, "recovery_reason": reason})
                except Exception:
                    pass

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

    def _record_run_issue_inspections(self, turn: Turn) -> None:
        run_issues = self.bridge.tree.list_log_issues()
        if not run_issues:
            return
        for command, result in zip(turn.commands_issued, turn.results):
            if not result.ok or result.command_type == "annotation":
                continue
            normalized = self._normalize_recent_command(command)
            if normalized.startswith("show-run-issue "):
                issue_id = normalized.split(None, 1)[1].strip()
                if issue_id:
                    self._inspected_run_issue_ids.add(issue_id)
                continue
            for issue in run_issues:
                issue_id = str(issue.get("id", "") or "").strip()
                file_path = str(issue.get("file", "") or "").strip().removeprefix("/repo/").removeprefix("repo/")
                if issue_id and file_path and f"/repo/{file_path}" in normalized:
                    self._inspected_run_issue_ids.add(issue_id)

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
        self._read_working_set.invalidate(normalized_path)

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

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    def _system_prompt(self) -> str:
        grammar = self.bridge.render_command_grammar()
        frame_hint = ""
        if self.require_command_frame:
            frame_hint = (
                f"\n{COMMAND_FRAME_RULE} Put any >>th: and >>pl: annotations inside the frame. "
                "Keep provider reasoning summaries in their own channel. When ready to act, start the frame directly. "
                "Use `>>th:` for a short public statement of intent. Commands are literal Playground OS text. "
                "Omit native function calls, Markdown fences, and preambles. Output one complete frame, then stop.\n"
                f"{BEGIN_COMMANDS}\n"
                ">>th: I need the current source.\n"
                ">>pl: Read the relevant lines.\n"
                "read-line-range /repo/src/example.ts 20-40\n"
                f"{END_COMMANDS}\n"
            )
        shell_note = "\nSHELL ACCESS POLICY: SHELL_ACCESS=false. Do not emit `shell <command>` or any action that dispatches to `run_shell`. Prefer structured file, git, and diagnostics commands.\n" if not self.allow_shell else ""
        return "\n".join(filter(None, [(_SYSTEM_PROMPT_TEMPLATE + shell_note).format(command_grammar=grammar, command_frame_hint=frame_hint),
                                       PROPOSAL_GUIDANCE, mcp_guidance(self.workspace_root, tree=True), preview_guidance(tree=True),
                                       "For the shell command: " + PACKAGE_SHELL_GUIDANCE if self.allow_shell else ""]))

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
        parts = [
            part
            for part in [self._base_steering, self._signal_steering, self._checkpoint_steering, self._recovery_steering]
            if part
        ]
        return "\n".join(parts)

    def _refresh_log_issue_signals(self) -> None:
        issues = self.bridge.tree.list_log_issues()
        status_map = {str(issue.get("id", "")): str(issue.get("status", "open")) for issue in issues if issue.get("id")}
        previous_map = dict(self._signal_state.get("issue_status_map", {}))
        unresolved = [issue for issue in issues if str(issue.get("status", "open")) not in {"resolved", "deferred"}]
        resolved = [issue for issue in issues if str(issue.get("status", "open")) == "resolved"]

        raw_signals: List[str] = []
        previous_unresolved = [issue_id for issue_id, status in previous_map.items() if status not in {"resolved", "deferred"}]
        changed_to_resolved = [
            issue_id
            for issue_id, status in status_map.items()
            if previous_map.get(issue_id) != "resolved" and status == "resolved"
        ]
        newly_added = [issue_id for issue_id in status_map if issue_id not in previous_map]
        for issue_id in newly_added:
            if status_map.get(issue_id) not in {"resolved", "deferred"}:
                raw_signals.append(f"issue_ingested:{issue_id}")
        for issue_id in changed_to_resolved:
            raw_signals.append(f"issue_resolved:{issue_id}")

        current_focus = str(self._signal_state.get("current_focus_issue_id", ""))
        unresolved_ids = [str(issue.get("id", "")) for issue in unresolved]
        uninspected_ids = [issue_id for issue_id in unresolved_ids if issue_id not in self._inspected_run_issue_ids]
        if current_focus not in unresolved_ids:
            if current_focus:
                raw_signals.append("focus_invalidated")
            current_focus = uninspected_ids[0] if uninspected_ids else (unresolved_ids[0] if unresolved_ids else "")
        elif current_focus in self._inspected_run_issue_ids and uninspected_ids:
            current_focus = uninspected_ids[0]

        meta_signal = ""
        if previous_unresolved and not unresolved:
            meta_signal = "no_blocking_issues" if any(issue.get("status") == "deferred" for issue in issues) else "all_issues_resolved"
        elif len(unresolved) > 1:
            meta_signal = "issue_batch_ready"
        elif len(unresolved) == 1:
            meta_signal = "single_issue_focus_ready"
        elif raw_signals:
            meta_signal = "issue_signal_update"

        self._signal_state["issue_status_map"] = status_map
        self._signal_state["current_focus_issue_id"] = current_focus
        self._signal_state["current_focus_issue_namespace"] = "run" if current_focus else ""
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
            focus_reads = self.bridge.tree.log_issue_read_commands(focus_issue)
            focus_read = focus_reads[0] if focus_reads else ""
            focus_inspected = current_focus in self._inspected_run_issue_ids
            if meta_signal == "issue_batch_ready":
                strategy_reads: List[str] = []
                for issue in unresolved[:4]:
                    reads = self.bridge.tree.log_issue_read_commands(issue)
                    issue_id = str(issue.get("id", "") or "").strip()
                    command = reads[0] if reads else f"show-run-issue {issue_id}"
                    if command not in strategy_reads:
                        strategy_reads.append(command)
                strategy_example = " and ".join(
                    f"`s{index}: {command}`" for index, command in enumerate(strategy_reads, start=1)
                )
                self._signal_steering = "\n".join([
                    f"[SIGNAL {meta_signal}] There are {len(unresolved)} unresolved run diagnostic(s).",
                    f"[RAW SIGNALS] {', '.join(raw_signals[:6]) if raw_signals else 'open_issue_set'}",
                    f"[RUN DIAGNOSTIC FOCUS] Current focus: {current_focus} — {focus_summary}",
                    "Run diagnostics are transient and separate from the active durable planner issue.",
                    f"Inspect the referenced files directly in parallel, for example: {strategy_example}.",
                    "Triage relevance using existing evidence. For unrelated findings, use propose-issue with run_issue_ids; do not investigate them further. Fix and verify only in-scope diagnostics before resolve-run-issue.",
                ])
            else:
                if focus_inspected:
                    inspection_guidance = (
                        f"This exact diagnostic revision is already inspected. Do not call `show-run-issue {current_focus}` again; "
                        "continue with the fix or rerun validation."
                    )
                elif focus_read:
                    inspection_guidance = (
                        f"Inspect its referenced code directly with `{focus_read}`. Use `show-run-issue {current_focus}` only if full diagnostic evidence is needed."
                    )
                else:
                    inspection_guidance = f"Use `show-run-issue {current_focus}` to inspect its evidence."
                self._signal_steering = "\n".join([
                    f"[SIGNAL {meta_signal or 'open_issues_present'}] There are {len(unresolved)} unresolved run diagnostic(s).",
                    f"[RAW SIGNALS] {', '.join(raw_signals[:6]) if raw_signals else 'focus_ready'}",
                    f"[RUN DIAGNOSTIC FOCUS] Current focus: {current_focus} — {focus_summary}",
                    "Run diagnostics are transient and separate from the active durable planner issue.",
                    inspection_guidance,
                    "For unrelated findings, use propose-issue with run_issue_ids and existing evidence; no further inspection is needed. Fix only in-scope diagnostics and use resolve-run-issue after verification.",
                ])
            return

        if issues:
            self._signal_steering = "\n".join([
                f"[SIGNAL {meta_signal or 'no_blocking_issues'}] No blocking run diagnostics remain; findings may be resolved or deferred to agent suggestions.",
                f"[RAW SIGNALS] {', '.join(raw_signals[:6]) if raw_signals else 'resolved_issue_set'}",
                "Validate the current goal only, then finish. Deferred findings must not trigger more reads, validation gates, or remediation; mention them separately without claiming broader checks passed.",
            ])
            self._signal_state["current_focus_issue_id"] = ""
            self._signal_state["current_focus_issue_namespace"] = ""
            return

        self._signal_steering = ""
        self._signal_state["active_signal"] = ""
        self._signal_state["meta_signal"] = ""
        self._signal_state["current_focus_issue_id"] = ""
        self._signal_state["current_focus_issue_namespace"] = ""
        self._signal_state["unresolved_issue_count"] = 0
        self._signal_state["resolved_issue_count"] = 0
        self._signal_state["raw_signals"] = []
        self._signal_state["latest_diagnostics"] = None

    # ------------------------------------------------------------------
    # Built-in tool execution (writes to disk)
    # ------------------------------------------------------------------

    def _command_format_error(self, raw: str) -> str:
        if raw.startswith(PARSE_ERROR_PREFIX):
            return raw.removeprefix(PARSE_ERROR_PREFIX)
        strategy = parse_strategy(raw) if is_strategy(raw) else None
        if strategy is not None and (error := strategy.steps.get("error")) is not None:
            return error.results[0].output
        for command in _executable_commands(raw):
            verb = command.split(None, 1)[0].lower()
            # These parsers only translate arguments. Other commands may inspect or change the workspace while parsing.
            if not (_is_file_edit_command(command) or verb in {"mutate", "discover", "mcp"}):
                continue
            if strategy is not None and has_strategy_placeholders(command):
                continue
            result = self.bridge.parser.parse_and_execute(command)
            if not result.ok:
                return f"Malformed `{verb}` command: {result.output}"
        return ""

    def _execute_command_batch(self, raw: str, *, turn: int = 0) -> Tuple[List[str], List[CommandResult], List[Annotation]]:
        """Complete commands in execution order, including tools and observers."""
        commands: List[str] = []
        results: List[CommandResult] = []
        remaining = len(_executable_commands(raw))
        blocked_reason = ""
        blocked_prefix = "skipped after prior command failure"
        batch_diagnostic_failure = False
        batch_diagnostic_paths: set[str] = set()
        self._same_turn_halt_reason = ""

        def execute(command: str, label: str) -> CommandResult:
            nonlocal remaining, blocked_reason, blocked_prefix, batch_diagnostic_failure
            annotation = parse_annotation(command)
            if annotation is not None:
                result = CommandResult(ok=True, output=annotation.compact(), command_type="annotation")
            else:
                remaining -= 1
                parts = command.split(None, 2)
                verb = parts[0].lower().replace("_", "-") if parts else ""
                if blocked_reason and not (batch_diagnostic_failure and verb in _BATCH_DIAGNOSTIC_INSPECTIONS):
                    result = CommandResult(ok=False, output=f"{blocked_prefix}: {blocked_reason}", command_type="skipped")
                else:
                    local_read = len(parts) >= 2 and verb in {"cat", "read-line-range"}
                    if local_read:
                        self._emit_live_progress("tool_start", turn, summary=f"Reading {parts[1][:180]}")
                    try:
                        result = self.bridge.execute_single(command)
                    finally:
                        if local_read:
                            self._emit_live_progress("tool_finish", turn)
                    if result.command_type == "finish" and remaining > 0:
                        result.ok = False
                        result.output = "finish must be the final executable command in a turn"
                    if result.ok and result.command_type == "read":
                        self._total_reads += 1
                    if result.ok and result.needs_tool:
                        action = result.tool_action or {}
                        action_type = action.get("type")
                        path = str(action.get("path") or "").removeprefix("./")
                        if (batch_diagnostic_failure and action_type == "diagnose" and path in batch_diagnostic_paths
                                and action["limit"] <= _BATCH_DIAGNOSTICS_LIMIT):
                            result.ok = False
                            result.needs_tool = False
                            result.command_type = "reused"
                            result.output = f"Reused the failed batch-end diagnostics above for {path}."
                        else:
                            if result.command_type != "read" and action_type not in {"run_shell", "mcp", "artifact_preview"}:
                                self._total_writes += 1
                            executed = self._dispatch_tool_with_progress(result, turn)
                            if action_type == "run_shell" and action.get("_changed_paths"):
                                self._total_writes += 1
                            if executed:
                                result.output = executed

            preview = f"[{label}] {command}" if label else command
            commands.append(preview)
            results.append(result)
            prepare_model_result(preview, result)
            if self.command_observer is not None:
                try:
                    self.command_observer(preview, result)
                except Exception:
                    pass
            # Observers may replace output with a real failure or a recovery block.
            prepare_model_result(preview, result)
            self._read_working_set.record(preview, result)
            if result.command_type == "read" and (observation := repository_observation(preview, result)) and observation.source_lines:
                self._remember_range(observation.source_path, observation.source_lines)
            if annotation is None and not blocked_reason:
                if not result.ok:
                    blocked_reason = result.output or "command failed"
                    if ((result.tool_action or {}).get("type") == "end_edit_batch"
                            and self._batch_diagnostics_failed(result.output) and not self._same_turn_halt_reason):
                        batch_diagnostic_failure = True
                        batch_diagnostic_paths.update((result.tool_action or {}).get("_batch_diagnostic_paths", ()))
                        blocked_prefix = "held after edit-batch diagnostics"
                        blocked_reason = "repair the reported diagnostics, then rerun validation"
                    elif result.command_type == "skipped":
                        blocked_prefix = "skipped by runtime"
                elif self._same_turn_halt_reason:
                    blocked_reason = self._same_turn_halt_reason
                    blocked_prefix = "skipped by runtime"
            elif batch_diagnostic_failure and self._same_turn_halt_reason:
                batch_diagnostic_failure = False
                blocked_reason = self._same_turn_halt_reason
                blocked_prefix = "skipped by runtime"
            if self.verbose:
                if annotation is not None:
                    _log(f"  {annotation.compact()}")
                else:
                    ok = "✓" if result.ok else "✗"
                    _log(f"  [{ok} {result.command_type.upper()}] {_truncate_command_for_cli(preview)}")
                    _log(f"  ⟶ {_truncate_for_cli(result.output)}")
            return result

        if is_strategy(raw):
            self.bridge.execute_strategy_full(raw, command_executor=execute)
            annotations = [annotation for command in parse_multi_command(raw)
                           if (annotation := parse_annotation(command)) is not None]
        else:
            self.bridge.execute(raw, command_executor=execute)
            annotations = self.bridge.last_annotations
        return commands, results, annotations

    def _dispatch_tool_with_progress(
        self, result: CommandResult, turn: int, *, dispatch: Optional[Callable[[CommandResult], str]] = None,
    ) -> str:
        action = result.tool_action or {}
        action_type = str(action.get("type") or "")
        execute = dispatch or self._execute_tool
        if not action_type or action_type in {"finish", "verify"}:
            return execute(result)

        label = f"Running {action_type.replace('_', ' ')}"
        if action_type == "mcp":
            server = " ".join(str(action.get("server") or "").split())[:80]
            name = " ".join(str(action.get("name") or "").split())[:80]
            if action.get("operation") == "call" and name:
                label = f"Calling {name} on {server} MCP" if server else f"Calling MCP tool {name}"
            else:
                label = f"Querying {server} MCP" if server else "Querying MCP"

        self._emit_live_progress("tool_start", turn, summary=label)
        try:
            provenance = self.diagnostic_provenance
            with provenance.track(action, shell_tracks_changes=True):
                output = execute(result)
            if action_type in {"run_check", "run_shell", "run_route_check"} and output:
                provenance.observe(self.bridge.tree._parse_log_issues(str(output), source_path=label))
            elif action_type == "diagnose" and "diagnostics:\n" in str(output):
                latest = self._signal_state.get("latest_diagnostics") or {}
                provenance.observe(latest.get("diagnostics", []))
            return output
        finally:
            self._emit_live_progress("tool_finish", turn)

    def _execute_tool(self, r: CommandResult) -> str:
        """Execute a tool action. Returns a status string.

        If a custom tool_dispatcher is set, delegates to it.
        Otherwise uses the built-in executor for write_file, replace_lines, patch_file,
        and shell commands.
        """
        if r.tool_action:
            # These fields are host evidence, never model-supplied assertions.
            r.tool_action.pop("_changed_paths", None)
            r.tool_action.pop("_change_accounting", None)
            r.tool_action.pop("_batch_diagnostic_paths", None)
            r.tool_action.pop("_staged_paths", None)
        if self.tool_dispatcher:
            try:
                dispatched = self.tool_dispatcher(r)
                if dispatched is None:
                    return "dispatched to custom handler"
                message = str(dispatched)
                if self._tool_message_indicates_failure(str((r.tool_action or {}).get("type", "")), message):
                    r.ok = False
                return message
            except Exception as e:
                r.ok = False
                return f"dispatch error: {e}"

        action = r.tool_action
        if not action:
            return ""

        action_type = action.get("type", "")

        if action_type == "artifact_preview":
            payload = run_artifact_preview(action)
            r.ok = payload["ok"]
            return json.dumps(payload, ensure_ascii=False)
        if action_type == "mcp":
            payload = model_mcp_result(invoke_mcp(self.workspace_root, action))
            r.ok = payload["ok"]
            return json.dumps(payload, ensure_ascii=False)
        if action_type in DISCOVERY_ACTION_TYPES:
            payload = self._exec_discovery(action)
            r.ok = bool(payload.get("ok", True))
            return json.dumps(payload, indent=2)
        if action_type == "propose_issue":
            store = IssueProposalStore(self.workspace_root)
            scope = 'task:' + hashlib.sha256(self._conversation_task.encode()).hexdigest()
            try:
                diagnostics = self.bridge.tree.list_log_issues()
                self.diagnostic_provenance.observe(diagnostics)
                proposal = store.propose(
                    action, diagnostics=diagnostics, scope=scope, parent_issue_id='', goal=self._conversation_task,
                    may_defer=self.diagnostic_provenance.may_defer,
                )
                self._refresh_log_issue_signals()
                return f"Recorded {proposal['proposal_id']}; linked diagnostics no longer gate this task."
            except Exception as exc:
                r.ok = False
                return f"propose_issue failed: {exc}"
        if action_type == "write_file":
            message = self._exec_write_file(action)
        elif action_type == "replace_lines":
            message = self._exec_replace_lines(action)
        elif action_type == "patch_file":
            message = self._exec_patch_file(action)
        elif action_type in EXPLICIT_MUTATION_TYPES:
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
            message = self._exec_end_edit_batch(action)
        elif action_type == "finish":
            message = "finish: end the active edit batch before finishing" if self.bridge.tree.staged_files else ""
        elif action_type == "set_fact":
            message = ""  # facts are already written to the tree
        else:
            message = f"unhandled action type: {action_type}"

        if self._tool_message_indicates_failure(action_type, message):
            r.ok = False
        return message

    @staticmethod
    def _tool_message_indicates_failure(action_type: str, message: str) -> bool:
        lowered = str(message or "").strip().lower()
        if not lowered:
            return False
        if action_type == "run_shell":
            exit_code = re.match(r"exit_code=(-?\d+)\b", lowered)
            return lowered.startswith("run_shell:") or bool(exit_code and int(exit_code.group(1)) != 0)
        if action_type in {"diagnose", "run_check", "run_route_check"}:
            match = re.search(r"\bissues=(\d+)\b", str(message or ""), re.IGNORECASE)
            exit_code = re.search(r"^exit_code=(-?\d+)\s*$", lowered, re.MULTILINE)
            return lowered.startswith(f"{action_type}:") or bool(match and int(match.group(1)) > 0) or bool(exit_code and int(exit_code.group(1)) != 0)
        if action_type == "end_edit_batch":
            return lowered.startswith("end_edit_batch:") or TreeLoop._batch_diagnostics_failed(lowered)
        return lowered.startswith(f"{action_type}:")

    @staticmethod
    def _batch_diagnostics_failed(message: str) -> bool:
        return bool(re.search(r"\bissues=[1-9]\d*", message)) or "backend_diagnostics_warning" in message

    def _exec_begin_edit_batch(self) -> str:
        if self.bridge.tree.staged_files is None:
            self.bridge.tree.staged_files = StagedFiles(self.workspace_root)
        self._signal_state["edit_batch_mode"] = True
        return "edit batch started; edits are staged until batch end"

    def discard_staged_edits(self) -> None:
        stage = self.bridge.tree.staged_files
        self.bridge.tree.staged_files = None
        if stage:
            paths = list(stage.originals)
            stage.close()
            for path in paths:
                self._range_reads.pop(path, None)
                self._refresh_recent_file_context(path)
        self._signal_state["edit_batch_mode"] = False

    def _exec_end_edit_batch(self, action: Dict[str, Any]) -> str:
        action["_changed_paths"] = []
        stage = self.bridge.tree.staged_files
        if stage:
            try:
                changes = stage.changes()
                errors = stage.syntax_errors(changes)
                if errors:
                    action["_batch_diagnostic_paths"] = sorted({item["path"] for item in errors})
                    findings = "\n".join(f"- {item['path']}:{item['line']}:{item['column']} {item['code']} {item['message']}" for item in errors)
                    return (f"end_edit_batch: staged TSX syntax check failed (issues={len(errors)}). No files were written. "
                            f"The staged batch is still open for repair.\n{findings}")
                action["_changed_paths"] = stage.publish(changes)
            except StageConflict as exc:
                self.discard_staged_edits()
                return f"end_edit_batch: {exc} The staged batch was discarded and the external edit was preserved."
            except (OSError, ValueError) as exc:
                return f"end_edit_batch: {exc} The staged batch is still open."
            self.bridge.tree.staged_files = None
            stage.close()
            self.diagnostic_provenance.changed_paths.update(action["_changed_paths"])
            for path in action["_changed_paths"]:
                self.bridge.on_write_complete(path)
                self._refresh_recent_file_context(path)
        self._signal_state["edit_batch_mode"] = False
        diagnostics = {}
        for path in action["_changed_paths"]:
            if (self.workspace_root / path).is_file():
                diagnostics[path] = self._run_backend_diagnostics_for_path(path, trigger_action="batch end")
            elif self.backend_check_observer is not None:
                self.backend_check_observer(path, True, "File removed from the workspace.", [])
        action["_batch_diagnostic_paths"] = [path for path, message in diagnostics.items() if message.startswith("backend_diagnostics ")]
        return "\n".join(["edit batch ended; deferred diagnostics completed", *filter(None, diagnostics.values())])

    def _edit_target(self, path: str) -> Path:
        stage = self.bridge.tree.staged_files
        return stage.include(path) if stage else self.workspace_root / path

    def _record_text_edit(self, action: dict, path: str) -> None:
        if self.bridge.tree.staged_files:
            action["_changed_paths"] = []
            action["_staged_paths"] = [path]
        else:
            self.bridge.on_write_complete(path)
            self._refresh_recent_file_context(path)

    def _exec_discovery(self, action: dict) -> dict:
        stage = self.bridge.tree.staged_files
        for key in ("path", "file_path"):
            path = action.get(key)
            if stage and isinstance(path, str):
                if staged := stage.lookup(path.removeprefix("/repo/").removeprefix("repo/")):
                    return execute_discovery_action({**action, key: staged.relative_to(stage.directory).as_posix()}, root=stage.directory)
        return execute_discovery_action(action, root=self.workspace_root)

    def _remember_range(self, path: str, lines: tuple[tuple[int, str], ...]) -> None:
        stage = self.bridge.tree.staged_files
        staged = stage.lookup(path) if stage else None
        try:
            target = staged or (self.workspace_root / path).resolve()
            if not staged and not target.is_relative_to(self.workspace_root):
                return
            revision = hashlib.sha256(target.read_text(encoding="utf-8").encode("utf-8")).hexdigest()
        except (OSError, UnicodeError):
            return
        self._range_reads.setdefault(path, {}).update({line: (text, revision) for line, text in lines})

    def _expected_range(self, path: str, first: int, last: int, before: list[str], current: str) -> tuple[list[str], bool]:
        observed = self._range_reads.get(Path(path).as_posix(), {})
        source_end = min(last, max(len(before), max(observed, default=0)))
        revision = hashlib.sha256(current.encode("utf-8")).hexdigest()
        baseline_current = current.splitlines() == before
        expected = []
        allow_position = True
        for line in range(first, source_end + 1):
            if line in observed:
                text, seen_revision = observed[line]
                allow_position &= seen_revision == revision
            elif line <= len(before):
                text = before[line - 1]
                allow_position &= baseline_current
            else:
                return [], False
            expected.append(text)
        return expected, allow_position

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

        target = self._edit_target(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        self._record_text_edit(action, rel_path)
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

        target = self._edit_target(rel_path)
        if not target.exists():
            return f"replace_lines: file not found: {rel_path}"

        original_text = target.read_text(encoding="utf-8")
        lines = original_text.splitlines()
        requested_range = (start_line, end_line)
        if stage := self.bridge.tree.staged_files:
            before = stage.originals[target.relative_to(stage.directory).as_posix()].data.decode("utf-8").splitlines()
            expected, allow_position = self._expected_range(rel_path, start_line, end_line, before, original_text)
            try:
                start_line, end_line = anchored_range(lines, start_line, end_line, expected, allow_position=allow_position)
            except ValueError as exc:
                return f"replace_lines: {rel_path}: {exc}"
        if start_line > len(lines):
            return f"replace_lines: start line out of bounds for {rel_path} ({len(lines)} total lines)"

        replacement_lines = content.splitlines()
        updated_lines = lines[: start_line - 1] + replacement_lines + lines[end_line:]
        updated = "\n".join(updated_lines)
        if updated_lines and original_text.endswith("\n"):
            updated += "\n"

        target.write_text(updated, encoding="utf-8")
        self._record_text_edit(action, rel_path)
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
            f"re-read lines {verify_start}-{verify_end}"
            + (f"; re-anchored from {requested_range[0]}-{requested_range[1]}" if requested_range != (start_line, end_line) else ""),
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

        target = self._edit_target(rel_path)
        if not target.exists():
            return f"patch_file: file not found: {rel_path}"

        original = target.read_text(encoding="utf-8")
        occurrences = original.count(search)
        if occurrences == 0:
            return (
                f"patch_file: search text not found in {rel_path}; no changes made. "
                "Re-read the target and use exact source text (without displayed line numbers). "
                "Use JSON-quoted operands for multiline text, quotes, or backslashes."
            )
        if occurrences > 1:
            return f"patch_file: ambiguous search text in {rel_path}: {occurrences} matches; no changes made. Include unique surrounding context."

        updated = original.replace(search, replace, 1)
        target.write_text(updated, encoding="utf-8")
        self._record_text_edit(action, rel_path)
        return self._append_backend_diagnostics(f"patched {rel_path}", rel_path, trigger_action="patch")

    def _exec_explicit_mutation(self, action: Dict[str, Any]) -> str:
        action_type = str(action.get("type", ""))
        stage = self.bridge.tree.staged_files
        try:
            planned = action
            if stage:
                if action_type == "batch_mutate" and isinstance(action.get("operations"), list):
                    planned = {**action, "operations": [self._anchor_staged_operation(stage, operation) for operation in action["operations"]]}
                else:
                    planned = self._anchor_staged_operation(stage, action)
            result, paths = execute_mutation(planned, self.workspace_root, stage=stage)
        except (ValueError, TypeError, OSError) as exc:
            return f"{action_type}: {exc}"
        action["_changed_paths"] = [] if stage else paths
        for path in paths:
            if not stage:
                self.bridge.on_write_complete(path)
                self._refresh_recent_file_context(path)
        if stage:
            action["_staged_paths"] = paths
        detail = json.dumps(compact_mutation_result(result), ensure_ascii=False)
        if not result["ok"]:
            return f"{action_type}: mutation failed; rolled_back={result['rolled_back']}\n{detail}"
        message = f"applied {action_type.replace('_', ' ')}: {result['applied_count']} operation(s)\n{detail}"
        for path in paths:
            target = stage.lookup(path) if stage else self.workspace_root / path
            if target is not None and target.is_file():
                message = self._append_backend_diagnostics(message, path, trigger_action=action_type)
            elif not stage and self.backend_check_observer is not None and not target.exists():
                self.backend_check_observer(path, True, "File removed from the workspace.", [])
        return message

    def _anchor_staged_operation(self, stage: StagedFiles, operation: Any) -> Any:
        if (not isinstance(operation, dict) or operation.get("type") not in {"replace_range", "delete_range"}
                or "expected_text" in operation):
            return operation
        path = operation.get("path") or operation.get("file_path")
        first, last = operation.get("start_line"), operation.get("end_line")
        if not isinstance(path, str) or not isinstance(first, int) or not isinstance(last, int) or first < 1 or last < first:
            return operation
        path = path.removeprefix("/repo/").removeprefix("repo/")
        target = stage.include(path)
        if not target.is_file():
            return operation
        before = stage.originals[target.relative_to(stage.directory).as_posix()].data.decode("utf-8").splitlines()
        expected, allow_position = self._expected_range(path, first, last, before, target.read_text(encoding="utf-8"))
        return operation if allow_position else {**operation, "expected_text": "\n".join(expected) + "\n" if expected else ""}

    def _append_backend_diagnostics(self, message: str, rel_path: str, *, trigger_action: str) -> str:
        if self._signal_state.get("edit_batch_mode"):
            return f"{message}; staged; diagnostics deferred until batch end"
        diagnostic_message = self._run_backend_diagnostics_for_path(rel_path, trigger_action=trigger_action)
        if not diagnostic_message:
            return message
        return f"{message}\n{diagnostic_message}"

    def _run_backend_diagnostics_for_path(self, rel_path: str, *, trigger_action: str) -> str:
        try:
            run = run_backend_diagnostics(self.workspace_root, path=rel_path, limit=_BATCH_DIAGNOSTICS_LIMIT, timeout=30)
        except ValueError:
            return ""

        diagnostics = [dict(item) for item in run.diagnostics]
        issues: List[Dict[str, Any]] = []
        lines: List[str] = []
        if diagnostics:
            issues = self.bridge.tree.ingest_diagnostic_content(
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
        else:
            self._signal_state["latest_diagnostics"] = None
            if run.scoped_returncode != 0:
                lines.append(f"backend_diagnostics_warning engine={run.engine} path={run.path} exit_code={run.returncode}")
                stdout = _strip_ansi(run.stdout).strip()
                stderr = _strip_ansi(run.stderr).strip()
                if stdout:
                    lines.append(f"stdout:\n{stdout}")
                if stderr:
                    lines.append(f"stderr:\n{stderr}")
        message = "\n".join(lines)
        if self.backend_check_observer is not None:
            self.backend_check_observer(run.path, not lines, message, issues)
        return message

    def _rewrite_repo_shell_paths(self, command: str) -> str:
        if self.workspace_root == Path("/repo"):
            return command

        # Keep shell syntax and quoted scripts intact; rewriting every token
        # through shlex.join turns &&, pipes and redirects into literal arguments.
        words = r'''(?:[^\s|&;()<>\\'"]+|'[^']*'|"(?:\\.|[^"\\])*"|\\.)+'''

        def rewrite(match: re.Match[str]) -> str:
            word = match.group(0)
            for quote in ("", "'", '"'):
                prefix = quote + "/repo"
                if not word.startswith(prefix):
                    continue
                suffix = word[len(prefix):]
                if not suffix or suffix.startswith("/") or (quote and suffix.startswith(quote)):
                    return shlex.quote(str(self.workspace_root)) + quote + suffix
            return word

        return re.sub(words, rewrite, command)

    def _maybe_git_rm_hint(self, command: str, result: subprocess.CompletedProcess[str]) -> str:
        normalized = str(command or "").strip()
        if not normalized.startswith("git rm"):
            return ""
        stderr = str(result.stderr or "")
        lowered = stderr.lower()
        if "pathspec" in lowered or "did not match any files" in lowered or "fatal:" in lowered:
            return "Hint: if the file is already untracked or already gone, use `shell rm -f <path>` instead."
        return ""

    def _record_repository_changes(self, action: Dict[str, Any], before: RepositorySnapshot) -> None:
        after = RepositorySnapshot.capture(self.workspace_root, previous=before)
        self._repository_snapshot = after
        action["_changed_paths"] = after.changed_since(before)
        action["_change_accounting"] = {"complete": not (before.errors or after.errors),
                                        "errors": (before.errors + after.errors)[:5]}
        for path in action["_changed_paths"]:
            self.bridge.on_write_complete(path)
            self._refresh_recent_file_context(path)
        if action.get("type") == "npm_command" or any(
            Path(path).name in {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock"}
            for path in action["_changed_paths"]
        ):
            self._read_working_set.invalidate_dependencies()

    def _exec_run_shell(
        self, action: Dict[str, Any], *, allow_process_changes: bool = True, verification_check: bool = False,
    ) -> str:
        action.pop("_managed_process", None)
        if action.get("check_name"):
            allow_process_changes = False
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
        if not command and action.get("operation") not in ("status", "stop"):
            return "run_shell: missing command"
        if re.search(r"(?:^|(?:&&|\|\||;|\||\n)\s*)(?:/usr/bin/)?git(?:\s|$)", command):
            return (
                "run_shell: direct git commands are disabled; use the typed `git <subcommand>` beta grammar "
                "so path, history-rewrite, and push authorization policies are enforced"
            )
        script_command = command.removeprefix("--background ")
        if _is_long_running_shell_command(command) and npm_script(script_command) is None:
            return (
                "run_shell: refused long-running dev/watch command: "
                f"{command}\n"
                "Invoke a declared npm server script on its own or use a finite diagnostic command such as `npm run build`."
            )

        command = self._rewrite_repo_shell_paths(command)

        before = RepositorySnapshot.capture(self.workspace_root, previous=self._repository_snapshot)
        try:
            managed = run_managed_shell(self.workspace_root, {**action, "command": command}, _subprocess_env_no_color(),
                                        allow_changes=allow_process_changes)
            if managed is not None:
                action["_managed_process"] = managed.get("process_state", "error")
                lines = []
                if managed.get("error"):
                    lines.append(f"run_shell: {managed['error']}")
                if managed.get("process_id"):
                    lines.extend([f"process_id={managed['process_id']}", f"process_state={managed['process_state']}"])
                    if "returncode" in managed:
                        lines.append(f"exit_code={managed['returncode']}")
                if managed.get("stdout"):
                    lines.append("stdout:\n" + _strip_ansi(managed["stdout"]))
                return "\n".join(lines)
            result = subprocess.run(
                command,
                cwd=str(self.workspace_root),
                shell=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_subprocess_env_no_color(),
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            return f"run_shell: timed out after 60s: {command}"
        except Exception as exc:
            return f"run_shell: error executing command: {exc}"
        finally:
            self._record_repository_changes(action, before)

        stdout = _strip_ansi(result.stdout).strip()
        stderr = _strip_ansi(result.stderr).strip()
        if verification_check:
            action["_verification_output"] = {"returncode": result.returncode, "stdout": stdout, "stderr": stderr}
        parts: List[str] = [f"exit_code={result.returncode}"]
        if action["_changed_paths"]:
            parts.append("observed_file_changes=" + json.dumps(action["_changed_paths"], ensure_ascii=False))
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

        before = RepositorySnapshot.capture(self.workspace_root, previous=self._repository_snapshot)
        try:
            result = subprocess.run(
                argv,
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_subprocess_env_no_color(),
                timeout=180,
            )
        except subprocess.TimeoutExpired:
            return f"npm_command: timed out after 180s: {' '.join(argv)}"
        except Exception as exc:
            return f"npm_command: error executing command: {exc}"
        finally:
            self._record_repository_changes(action, before)

        stdout = _strip_ansi(result.stdout).strip()
        stderr = _strip_ansi(result.stderr).strip()
        parts: List[str] = [f"manager={manager}", f"exit_code={result.returncode}"]
        if action["_changed_paths"]:
            parts.append("observed_file_changes=" + json.dumps(action["_changed_paths"], ensure_ascii=False))
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
                package_data = json.loads(package_json.read_text(encoding="utf-8"))
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
                encoding="utf-8",
                errors="replace",
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
            f"exit_code={run.scoped_returncode}",
        ]
        if run.other_file_error_count:
            parts.extend([
                f"scope={run.scope}",
                f"compiler_exit_code={run.returncode}",
                f"other_file_errors={run.other_file_error_count}",
                "The requested file has no diagnostics. The compiler reported errors in other files.",
            ])
        if run.scoped_returncode != 0:
            stdout = _strip_ansi(run.stdout).strip()
            stderr = _strip_ansi(run.stderr).strip()
            if stdout:
                parts.append(f"stdout:\n{stdout}")
            if stderr:
                parts.append(f"stderr:\n{stderr}")
            if not stdout and not stderr:
                parts.append("Diagnostic command failed without output.")
            return "diagnose: " + "\n".join(parts)
        return "\n".join(parts)

    def _exec_run_route_check(self, action: Dict[str, Any]) -> str:
        action.pop("_validation_unavailable", None)
        action.pop("_route_checked", None)
        target = str(action.get("target", "") or "").strip()
        if not target:
            return "run_route_check: missing target route or URL"

        base_url = str(action.get("base_url", "") or "").strip()
        url = self._resolve_route_check_url(target, base_url)
        preview = check_artifact_route(target, base_url)
        if preview is not None:
            if not preview.get("ok"):
                action["_validation_unavailable"] = bool(preview.get("unavailable"))
                return f"run_route_check: {preview.get('error') or 'The artifact preview check failed.'}"
            return self._record_route_check(action, preview, target, url)
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
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".cjs", delete=False) as fh:
                fh.write(script)
                temp_script = fh.name
            result = subprocess.run(
                ["node", temp_script, url, target],
                cwd=str(self.workspace_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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
                action["_validation_unavailable"] = True
                return (
                    f"run_route_check: playwright is not installed in {self.workspace_root}\n"
                    f"For browser execution, use the configured Playwright MCP on {url} if it can reach that URL, "
                    "then inspect the rendered page for the goal's success conditions."
                )
            return f"run_route_check: failed for {url}\nstderr:\n{stderr or '(no stderr)'}"

        try:
            payload = json.loads(result.stdout or "{}")
        except json.JSONDecodeError as exc:
            return f"run_route_check: invalid JSON output: {exc}"
        return self._record_route_check(action, payload, target, url)

    def _record_route_check(self, action: dict, payload: dict, target: str, url: str) -> str:
        if not isinstance(payload, dict) or not payload.get("finalUrl"):
            return "run_route_check: the browser returned no route observation"
        action["_route_checked"] = True
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
    env["PYTHONIOENCODING"] = "utf-8"
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
