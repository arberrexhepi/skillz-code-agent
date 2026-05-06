# Python Agent

Planner-first coding agent for real repositories. The planner handles clarification, discovery, goal sequencing, and final next steps. The worker executes concrete repository actions.

## TLDR: Ways To Use The Agent

| Interface | Best for | How to start | How to use |
| --- | --- | --- | --- |
| Planner CLI | Normal repo work where you want discovery, a reviewable plan, then execution. | `python main.py --provider openai --model gpt-5.4 --root /your/project` | Type the request, choose discovery depth if offered, then `approve` to run the plan. |
| Auto CLI | Letting the planner run one or more issue cycles without pausing for plan approval. | `start-auto 3 Build the feature described in PROPOSAL.md` from the planner prompt | The optional text becomes the auto-run prompt; cycles create/close issues and use completed issue context to avoid repeats. |
| Direct Worker CLI | Small, concrete edits when you do not need planner decomposition. | `python main.py --provider openai --model gpt-5.4 --root /your/project --worker-mode` | Give a focused task; the worker reads, edits, validates, and finishes directly. |
| Beta TreeLoop Worker | Fast command-grammar workflow and current-run diagnostics. | `python main_v2.py --provider gemini --model gemini-3-flash-preview --root /your/project --worker-mode` | Use tree commands like `cat`, `replace-lines`, `run-check`, `list-run-issues`, and `show-run-issue`. |
| VS Code Extension | Desktop UI for planner state, Auto mode, issues, diagnostics, diffs, and suggested actions. | Open `vscode-extension/` in VS Code and run `Run Python Agent Extension` | Use the panel to submit prompts, create issues, start Auto cycles, approve plans, inspect diagnostics, and open files/diffs. |

Common planner commands: `/reset`, `/start-auto 3 optional prompt`, `/stop-auto`, `/create-issue details`, `reopen issue-123`, `approve`, `reject`.

## Setup

Install dependencies:

```bash
pip install openai google-genai anthropic
```

Set an API key with environment variables or a local `.env` file:

```bash
export OPENAI_API_KEY=...
export GEMINI_API_KEY=...
export ANTHROPIC_API_KEY=...
```

## Run

Planner-first mode:

```bash
python main.py --provider openai --model gpt-5.4 --root /your/project
python main.py --provider anthropic --model claude-sonnet-4-6 --root /your/project
python main.py --provider local --model gemma4 --root /your/project
```

Direct worker mode:

```bash
python main.py --provider openai --model gpt-5.4 --root /your/project --worker-mode
python main.py --provider anthropic --model claude-sonnet-4-6 --root /your/project --worker-mode
python main_v2.py --provider gemini --model gemini-3-flash-preview --root /your/project --worker-mode
python main.py --provider local --model gemma4 --root /your/project --worker-mode
```

Optional runtime tuning:

```bash
python main.py --provider openai --model gpt-5.4 --root /your/project --max-parallel-workers 6
```

Live runtime switching in the CLI:

```text
/runtime anthropic claude-sonnet-4-6
/model claude-sonnet-4-6
/runtime-show
/providers
/models
/models gemini
```

`/providers` lists supported runtimes. `/models [provider]` shows the current provider by default and prints suggested model names for any supported provider. On startup, the backend now does one best-effort live model refresh for providers with installed SDKs and credentials, then falls back to the built-in April 2026 catalog if a provider cannot be queried. Custom model strings are still allowed.

## VS Code Extension

An initial desktop VS Code extension shell is available under `vscode-extension/`.

What it currently provides:

- launches the Python planner/worker runtime as a background bridge process
- renders planner state, worker runtime state, transcript history, and current-run facts in a webview panel
- turns planner and worker `suggested_next_actions` into clickable buttons for plan approval, rejection, discovery selection, validation, review, and recovery flows
- surfaces backend-generated diagnostics in the panel and mirrors them into the VS Code Problems view, including file-targeted checks that also work in pure CLI mode
- opens file paths surfaced from runtime state directly in the editor and can open review reports plus working-tree-vs-HEAD file diffs

Extension development setup:

```bash
cd vscode-extension
npm install
npm run compile
npm test
npm run test:integration
```

Then open `vscode-extension/` as the extension development workspace and run the `Run Python Agent Extension` launch configuration.

Extension settings:

- `skillzAgent.provider`
- `skillzAgent.model`
- `skillzAgent.pythonPath`
- `skillzAgent.backendScript`

To launch the beta TreeLoop planner bridge from the extension, set `skillzAgent.backendScript` to `main_v2.py`. Leave it as `main.py` to keep using the stable planner/worker backend.

Changing `skillzAgent.provider` or `skillzAgent.model` while the extension backend is running now hot-updates the active runtime without killing the process.

Backend requirements:

- Python 3.13 is the current development target; the extension will also work with a compatible Python interpreter that can run `main.py` and `agent_tools.py`.
- Install Python dependencies for the selected provider before launching the extension: `openai` for OpenAI mode, `anthropic` for Anthropic mode, `google-genai` for Gemini mode.
- Set provider credentials in the environment seen by VS Code, such as `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `GEMINI_API_KEY`.
- Keep `git` available on `PATH`; review, diff, and file comparison flows rely on repository commands.
- `skillzAgent.pythonPath` should point at the interpreter or virtual environment you want the extension backend to use.
- The `local` provider targets the existing localhost OpenAI-compatible endpoint at `http://127.0.0.1:5051/v1`, which can be used for models such as Gemma 4.
- Node.js and `npm` are required only for extension development inside `vscode-extension/`, not for the Python backend itself.

The extension currently targets desktop VS Code APIs and uses the Python runtime as the source of truth for planner/worker behavior.

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
