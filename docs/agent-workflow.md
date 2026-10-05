# Agent workflow and tools

Skillz keeps discovery, approved plans, repository actions, validation, and durable issue facts in its Python runtime. See the [root README](../readme.md#usage) for a quick example.

## Planner Flow

- Starts in planner mode by default.
- Asks clarification questions when the request is materially underspecified.
- Offers a discovery phase when repo inspection is needed before planning.
- Supports `Quick Scan`, `Moderate Scan`, and `Deep Scan` discovery depths.
- Produces a plan that must be approved before execution.
- Delegates goals one at a time to the worker.
- Can execute dependency-ready read-only or validation-only goals concurrently when the planner marks them safe to parallelize.
- After discovery, pushes discovered files, constraints, and risks into delegation so goals are concrete rather than vague.
- Ends with specific next steps tied to the executed work.
- Opens an issue-scoped execution context when an approved plan starts, closes it on full success, and can explicitly reopen recent issues for follow-up work.
- Pauses on retryable model failures while preserving completed-goal checkpoints and partial repository changes.
- Resumes from the failed or next incomplete goal without rerunning completed goals or requiring plan re-approval.

Planner commands:

- `/approve` executes the pending plan.
- `/reject` rejects the pending plan.
- `/plan` shows the current pending plan.
- `/discover` shows the current discovery offer.
- `/providers` lists supported runtime providers.
- `/models [provider]` lists suggested models for the current or specified provider.
- `/reset` clears planner state.
- `/worker` enters direct worker debug mode.
- `/quit` exits.

## Example Session

Example request:

```text
When opening a routine, do a 10 second countdown with speech and an indicator before the first drill starts.
```

Typical planner-first flow:

```text
planner> When opening a routine, do a 10 second countdown with speech and an indicator before the first drill starts.

Discovery suggested: The request depends on the current routine start flow and UI entrypoints.
Choose a discovery depth:
1. Quick Scan [budget: 6 tool calls]
2. Moderate Scan (recommended) [budget: 12 tool calls]
3. Deep Scan [budget: 15 tool calls]

planner> 2

Discovery complete: Moderate Scan
Worker result: Discovery found the routine entry flow in src/app.py and the immediate start behavior in src/routine.py.
Tool budget: 7/12

Plan summary: Fix routine start flow
Discovery basis: Discovery found the routine entry flow in src/app.py and the immediate start behavior in src/routine.py.
Goals:
1. Implement countdown before first drill [goal-1] - preserve_context=false
	Goal: Update the routine startup flow to show a 10 second countdown, play countdown speech, and begin the first drill only after countdown completion.
	Why next: Discovery already identified the startup flow and the files controlling routine start behavior.
	Delegation: Primary discovered files: src/app.py, src/routine.py; Use the discovery findings directly rather than repeating broad discovery.
	Success signals: The worker reports a concrete completed outcome tied to the discovered flow, not additional broad discovery.

planner> approve

Executing confirmed plan.
Goal 1/1 completed: Implement countdown before first drill
Worker result: Updated the startup flow and added countdown behavior before the first drill begins.

Specific next steps:
1. Validate the countdown timing and speech cadence in the routine UI.
2. Verify the first drill starts only after countdown completion.
```

What this example shows:

- The planner offers discovery when repo structure matters.
- Discovery findings are carried into the plan rather than discarded.
- Goal delegation names concrete files, outcomes, and success signals.
- Approval is explicit before worker execution begins.

## Worker Tooling

The worker supports focused repository actions instead of a generic shell-first workflow.

Core file and search actions:

- `list_files` with recursive listing, max depth, and glob filters.
- `read_file` with optional line windows.
- `inspect_files` for batched multi-file reads.
- `summarize_files` for dependency-aware file summaries.
- `grep` scoped by path and glob, with ripgrep when available.
- `find_files` scoped by path and glob.
- `symbol_search` for Python and JS/TS symbols, including imports/exports and Python methods.

Change and git actions:

- `write_file` and `patch_file` with verification-aware follow-up.
- `git_status` with parsed entries and counts.
- `git_diff` with staged, stat, and name-only modes.
- `review_changes` with risk and validation summaries.
- `git_add`, `git_restore`, `git_commit`, `git_log`, and `git_branch`.

The beta TreeLoop worker exposes a guarded Git command library for `status`, `diff`, revision-range `log`, read-only branch and remote inspection, `rev-parse`, `show`, `blame`, explicit-path staging/restoration/moves/removals, commits, and authorized pushes. Remote writes require explicit task authorization, and repository paths and revisions are validated before execution.

Execution and context actions:

- `diagnose` for backend file-targeted diagnostics on `.ts`, `.tsx`, `.js`, `.jsx`, and `.py` files without relying on VS Code.
- `run_shell` for validation, formatting, or targeted inspection.
- `meta` and `show_diff` for repository context.
- `history_expand` and `memory_expand` for compact context recovery.
- `drop_context` and `finish` for execution control.

Playground OS skills:

- Bundled skills live under `skills/*.md` with front matter for `name`, `description`, optional `args_schema`, optional `tags`, optional `category`, and optional `priority`.
- Both the stable runtime and the beta TreeLoop runtime auto-load bundled skills from this repo and workspace-local skills from `<target-repo>/skills/*.md`.
- In the stable runtime, use the `skill` action to list skills or load a named skill payload.
- Use `skill` to list them and `skill <name>` to invoke a cached Markdown skill payload.

## Issue-Scoped Facts

- Durable facts in `repo_facts.md` are now schema-versioned and stored in an issue-aware ledger instead of a flat list.
- `architecture` facts are cross-issue repo memory and remain available for unrelated future work.
- `goal` facts are issue-local memory and return only while the issue is active or when that issue is explicitly reopened.
- Approved plan execution opens an issue automatically; successful completion closes it.
- The planner and extension can surface recent closed issues as explicit reopen actions instead of silently leaking old goal facts into new requests.

## Notes

- The planner is designed to reduce repeated exploration and push the worker toward concrete execution once enough evidence exists.
- Successful writes and patches require read-based verification before the worker treats them as complete.
- Discovery is intended to improve delegation quality, not become a substitute for execution.
- The host can prefetch discovery probes in parallel and run parallel post-write validation, while repository writes remain serialized behind runtime locks.
- The backend now exposes a structured runtime catalog for supported providers and suggested models, so the CLI and VS Code extension can reuse the same source of truth instead of hardcoding separate lists.
