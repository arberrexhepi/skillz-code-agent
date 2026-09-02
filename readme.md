# Skillz Code Agent

Skillz is a planner-first coding agent for real repositories, available as a standalone Electron/React workbench, a CLI, and a VS Code extension. The planner handles clarification, bounded discovery, goal sequencing, approvals, recovery, and final next steps; the worker performs concrete repository actions and validation.

Model invocation is provider-neutral. API-backed providers remain available, while the additive `codex-subscription` backend can invoke supported Codex models through an existing local ChatGPT subscription session without converting or replacing the OpenAI API path.

## TLDR: Ways To Use The Agent

| Interface | Best for | How to start | How to use |
| --- | --- | --- | --- |
| Desktop Workbench | The complete local workflow: repository browsing, editing, terminal, Git, planner execution, runtime selection, and live goal activity. | `cd desktop && npm install && npm run dev` | Open a repository, configure the provider/model/backend in Runtime settings, start the agent, then work through discovery, approval, execution, and goal reports. |
| Codex subscription runtime | Running either stable or TreeLoop workers with a locally authenticated ChatGPT/Codex subscription instead of API-key billing. | Run `codex login`, then select `codex-subscription` in the desktop Runtime drawer or pass it to the CLI. | Choose a model from the detected local catalog. The existing `openai` provider remains available separately. |
| Planner CLI | Normal repo work where you want discovery, a reviewable plan, then execution. | `python main.py --provider openai --model gpt-5.4 --root /your/project` | Type the request, choose discovery depth if offered, then `approve` to run the plan. |
| Auto CLI | Letting the planner run one or more issue cycles without pausing for plan approval. | `start-auto 3 Build the feature described in PROPOSAL.md` from the planner prompt | The optional text becomes the auto-run prompt; cycles create/close issues and use completed issue context to avoid repeats. |
| Direct Worker CLI | Small, concrete edits when you do not need planner decomposition. | `python main.py --provider openai --model gpt-5.4 --root /your/project --worker-mode` | Give a focused task; the worker reads, edits, validates, and finishes directly. |
| Beta TreeLoop Worker | Fast command-grammar workflow and current-run diagnostics. | `python main_v2.py --provider gemini --model gemini-3-flash-preview --root /your/project --worker-mode` | Use tree commands like `cat`, `replace-lines`, `run-check`, `list-run-issues`, and `show-run-issue`. |
| VS Code Extension | Desktop UI for planner state, Auto mode, issues, diagnostics, diffs, and suggested actions. | Open `vscode-extension/` in VS Code and run `Run Python Agent Extension` | Use the panel to submit prompts, create issues, start Auto cycles, approve plans, inspect diagnostics, and open files/diffs. |

Common planner commands: `/reset`, `/start-auto 3 optional prompt`, `/stop-auto`, `/create-issue details`, `reopen issue-123`, `approve`, `reject`.

> **Roadmap note:** The Electron/React Workbench is the primary desktop direction and is expected to phase out the VS Code extension. The extension remains useful for current development and compatibility, but should not be the sole reason to adopt Skillz or be treated as a long-term product commitment.

## Latest Development Update

The desktop workbench now handles common Windows setup failures, provides a manual Codex location fallback, and helps users start version control in a new project folder:

- **Windows Python discovery:** agent startup, runtime options, and Codex status/login share interpreter detection. The workbench checks an explicit override and the repository virtual environment, then tries `python`, the Windows `py -3` launcher, and `python3`. This fixes `spawn python ENOENT` when Python is installed through the launcher but absent from `PATH`.
- **Codex discovery and manual setup:** Windows discovery recognizes the local Codex desktop application's versioned runtime directories. In Runtime settings, users can browse or paste a CLI executable, validate it with **Save and check**, and keep the selection on their computer. **Use automatic discovery** restores normal lookup; changing a running agent's CLI path displays restart guidance.
- **UTF-8 message handling:** Python bridge streams and Codex subprocess pipes use explicit UTF-8. Prompts, responses, account status, and model discovery handle accented text, multilingual content, and emoji without Windows code-page conversion. This fixes the invalid UTF-8 stdin error that could occur even after successful Codex authentication.
- **Windows terminal compatibility:** the embedded terminal uses node-pty's native Windows encoding behavior, removing the unsupported encoding warning.
- **New repository setup:** Source Control offers **Initialize repository** for folders without Git metadata, then shows files ready for staging and a first commit. It recognizes existing repositories and worktrees, preserves actionable errors, and rejects initialization requests for a folder the user has already switched away from.
- **Regression coverage:** focused Python and desktop tests cover discovery, saved CLI settings, UTF-8 subprocess traffic, terminal options, and Git initialization. Browser fixtures exercise setup, retry, and concurrent refresh behavior; Git fixtures account for Windows line endings and symlink privileges.

See [Windows quick start](#windows-quick-start) and the [desktop guide](desktop/README.md) for setup and verification commands. Packaged builds still require a separately installed Python interpreter and provider dependencies.

### Earlier development highlights

The planner/worker improvements below remain part of the current workbench:

- **Smaller model turns:** stable and beta workers now keep provider-native message transcripts after the first turn instead of rebuilding the full prompt every time. OpenAI prompt-cache keys, cached-token accounting, explicit context drops, and compact fresh-context final retries reduce repeated context without losing current repository state.
- **Resilient provider calls:** transient 408/409/425/429 and 5xx failures receive bounded retries, `Retry-After` is honored, repeated 500s receive a longer cooldown, and request IDs plus retry timing are retained for observability. Terminal provider failures pause execution with partial edits preserved so retry starts by inspecting and repairing the current diff.
- **Reliable plan continuation:** completed goals are persisted as issue checkpoints, continuation plans reconcile dependencies on completed or omitted goals, and unsafe dependency graphs are rejected before execution. Resuming skips completed goals and starts at the failed or next incomplete goal without another approval cycle.
- **Clear issue identity:** durable planner issues use `issue-*` identifiers, while transient validation findings use stable `run-*` identifiers and dedicated list/show commands. This prevents a worker from mistaking an editor diagnostic for the active issue it is implementing.
- **Preemptive output recovery:** annotation-only, prose-only, and malformed command turns are repaired into the beta command grammar before they become terminal `model_output_invalid` failures.
- **Expanded guarded Git support:** the beta worker now supports bounded status, diff, revision-range log, branch, remote, rev-parse, show, blame, add, restore, move, remove, commit, and push operations. Mutating or remote operations remain authorization-gated, paths are explicit, and broad or unsafe revision expressions are rejected.
- **Meta Muse Spark support:** `muse-spark-1.2` is available through Meta's OpenAI-compatible API using `META_AI_API_KEY`, including provider/model selection in the extension. The standard model remains distinct from the opt-in contributor tier whose prompts and completions may be used for Meta training.
- **Local Codex subscription support:** `codex-subscription` invokes models through the locally installed Codex CLI and its ChatGPT-managed session. It remains isolated from the existing `openai` API-key provider and appears with account, plan, and live-model status in the Electron Runtime drawer.
- **Standalone desktop workbench:** the Electron main process hosts workspace, Git, PTY, and Python bridge services behind validated IPC, while the sandboxed React renderer provides Monaco editing/diffs, an xterm terminal, lifecycle-aware agent cards, goal reports, and live model/tool activity.
- **Evidence-based execution recovery:** the beta/live TreeLoop interrupts repeated empty searches or unchanged repository observations, not useful source inspection or skill loading. New evidence resumes execution; failed patch diagnostics and the affected source remain available for repair. Repeated unproductive exploration still ends in an incomplete stop.
- **Lossless mutation text:** quoted patch operands decode once, embedded arrows stay inside source text, and write/replace payloads retain indentation and trailing whitespace through extraction, heredocs, and strategy steps. Patches require one exact match; incomplete heredocs reject the batch without dispatching writes.
- **Workspace-backed discovery:** `/repo` reads, recursive filename/content/symbol searches, and diagnostics snapshots no longer depend on the capped metadata preview or preloaded content. Typed TypeScript component declarations share the discovery symbol scanner; other virtual mounts stay in-memory.
- **Better extension state:** the panel shows provider-specific model choices, per-session and per-issue usage, transient recovery details, durable-versus-run issue context, completed checkpoints, and the exact goal where a paused plan will resume.

These changes are covered by targeted provider, prompt-cache, transcript, recovery, continuation, usage-accounting, beta Git, command-repair, and extension panel tests.

## Architecture

```text
Electron / React workbench                 CLI / VS Code extension
            │ validated IPC                         │
            ▼                                       │
Electron main process                               │
  ├── workspace, Git, PTY                           │
  ├── Codex account/model status                    │
  └── Python process manager ───── NDJSON bridge ───┘
                                      │
                                      ▼
                         Python planner + worker
                           ├── stable runtime
                           ├── beta/live TreeLoop
                           └── provider adapters
                                ├── API providers
                                ├── local providers
                                └── Codex subscription
```

The Python planner/worker is the source of truth for lifecycle and repository actions. Skillz uses Codex only as a model backend: a subscription-backed Codex subprocess cannot directly edit the target repository, and must return Skillz actions for the host to validate and execute.

## Setup

Requirements:

- Python 3.13 is the current development target.
- Git must be available for status, diff, review, and repository mutation flows.
- Node.js and npm are required for the Electron workbench or VS Code extension.
- The Codex CLI, or a discovered desktop-bundled Codex executable on Windows or macOS, is required only for `codex-subscription`.

Install Python provider dependencies:

```bash
pip install openai google-genai anthropic
```

Set an API key with environment variables or a local `.env` file:

```bash
export OPENAI_API_KEY=...
export META_AI_API_KEY=...
export GEMINI_API_KEY=...
export ANTHROPIC_API_KEY=...
```

Only configure credentials for the API-backed providers you use. `codex-subscription` does not require `OPENAI_API_KEY`; it requires a local Codex session authenticated with ChatGPT.

Start the desktop workbench:

```bash
cd desktop
npm install
npm run dev
```

Then open a project folder, open Runtime settings, choose a provider/model and one of the stable, beta, or live backends, and select **Start agent**. If the folder has no Git repository, Source Control offers **Initialize repository**; afterward, choose files to stage and make the first commit.

### Windows quick start

Install Python 3 with the Windows launcher, Git, and Node.js/npm. From the repository root in PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install openai google-genai anthropic
cd desktop
npm install
npm run dev
```

The workbench uses `.venv\Scripts\python.exe` automatically. To use another installation, set `$env:PYTHON_AGENT_PYTHON = 'C:\Path With Spaces\Python\python.exe'` before launching it. Use only the executable path, without arguments or embedded quotes. Without an override or repository virtual environment, Windows lookup tries `python`, `py -3`, then `python3`; macOS/Linux lookup tries `python3`, then `python`. An invalid explicit selection produces a setup error so the workbench does not silently use a different environment.

For a local ChatGPT subscription, select **Codex / ChatGPT subscription** in Runtime settings. If automatic discovery fails, expand **Locate Codex CLI**, browse or paste the native `codex.exe` path, and choose **Save and check**. Use **Sign in with ChatGPT** if needed. After changing the executable for a running agent, stop and start the agent to use it for model calls. See [Codex / ChatGPT subscription runtime](#codex--chatgpt-subscription-runtime) for discovery paths and environment overrides.

Run the focused checks from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest test_codex_subscription test_codex_discovery test_codex_utf8
cd desktop
npm run test:runtime
npm run test:git
npm run build
```

The file-symlink Git test reports a skip on Windows when Developer Mode or symlink privileges are unavailable; the remaining Git checks still run.

## Run

Planner-first mode:

```bash
python main.py --provider openai --model gpt-5.4 --root /your/project
python main.py --provider codex-subscription --model gpt-5.6-terra --root /your/project
python main.py --provider meta --model muse-spark-1.2 --root /your/project
python main.py --provider anthropic --model claude-sonnet-4-6 --root /your/project
python main.py --provider local --model gemma4 --root /your/project
```

Direct worker mode:

```bash
python main.py --provider openai --model gpt-5.4 --root /your/project --worker-mode
python main.py --provider codex-subscription --model gpt-5.6-terra --root /your/project --worker-mode
python main.py --provider meta --model muse-spark-1.2 --root /your/project --worker-mode
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

`/providers` lists supported runtimes. `/models [provider]` shows the current provider by default and prints suggested model names for any supported provider. On startup, the backend does one best-effort live model refresh for providers with installed SDKs and credentials, then falls back to the built-in catalog if a provider cannot be queried. Custom model strings remain available for providers that support them; `codex-subscription` is intentionally limited to its local live/fallback catalog.

### Codex / ChatGPT subscription runtime

The `codex-subscription` provider is an additive alternative to `openai`; it does not replace or modify API-key invocation.

1. Install the Codex CLI, or use a discovered desktop-bundled executable on Windows or macOS.
2. Run `codex login` and complete the browser flow. Confirm the active method with `codex login status`; it must report ChatGPT authentication rather than API-key authentication.
3. In the Electron Workbench Runtime drawer, choose **Codex / ChatGPT subscription**. The drawer shows the detected account, subscription plan, CLI version, and live model catalog. If needed, use **Sign in with ChatGPT**.
4. Select one of the models advertised by the local Codex catalog and apply the runtime.

You can inspect the same integration without starting the desktop app:

```bash
python codex_subscription.py status
python main.py --provider codex-subscription --model gpt-5.6-terra --root /your/project
```

Skillz discovers the executable in this order: `CODEX_CLI_PATH`, `codex` on `PATH`, then the local desktop bundle. On macOS this is `/Applications/ChatGPT.app`. On Windows it checks `%LOCALAPPDATA%\OpenAI\Codex\bin\<runtime>\codex.exe` (newest binary first), then the older `bin\codex.exe` layout. Windows discovery works even when the desktop app was launched without Codex on `PATH`. Override discovery when needed:

```bash
export CODEX_CLI_PATH=/absolute/path/to/codex
```

Desktop users can also choose **Runtime → Locate Codex CLI**, browse or paste the executable path, and select **Save and check**. This validated, per-computer selection takes precedence over `CODEX_CLI_PATH`; **Use automatic discovery** removes it. A missing explicit path is reported instead of silently switching installations.

In Windows PowerShell, set `$env:CODEX_CLI_PATH = 'C:\Path With Spaces\codex.exe'` before launching Skillz. Use the executable path only, without command arguments. Inspect discovery and session status with `py -3 codex_subscription.py status` from the repository root.

Each model turn runs through `codex exec --ephemeral` in a temporary, read-only workspace. Skillz removes `OPENAI_API_KEY`, OpenAI base-URL overrides, and Codex API/access-token variables from the child process, preventing this provider from silently falling back to usage-based API authentication. Repository reads, writes, and validation remain controlled by the Skillz host.

Set `CODEX_SUBSCRIPTION_TIMEOUT_SECONDS` to override the default model-turn timeout. Authentication and model discovery use the local Codex app-server; credentials remain owned by Codex and are never copied into the renderer or Skillz configuration.

OpenAI documents ChatGPT subscription and API-key login as separate Codex authentication paths. API-key invocation continues to use standard API billing, while ChatGPT sign-in uses the permissions and limits of the selected ChatGPT account/workspace. See [Codex authentication](https://learn.chatgpt.com/docs/auth), [Codex app-server](https://learn.chatgpt.com/docs/app-server), and [Codex non-interactive mode](https://learn.chatgpt.com/docs/non-interactive-mode).

Muse Spark uses Meta's OpenAI-compatible Responses API. Add `META_AI_API_KEY=...` to the repository `.env`, then select `--provider meta --model muse-spark-1.2`. The shared startup loader reads `.env` before constructing any provider client, including when the VS Code extension launches the backend. The base URL defaults to `https://api.meta.ai/v1` and can be overridden with `META_MODEL_API_BASE_URL`. Meta does not support `--thinking-mode none`; use `minimal` or higher. The discounted `muse-spark-1.2-contributor` model is also listed, but its prompts and completions may be used by Meta for training, unlike the standard tier.

## VS Code Extension

An initial desktop VS Code extension shell is available under `vscode-extension/`.

The extension is a transitional integration surface. Ongoing product development is centered on the standalone Electron/React Workbench, which is intended to replace the extension rather than maintain permanent feature parity with it.

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
- Set provider credentials in the repository `.env` or the environment seen by VS Code, such as `OPENAI_API_KEY`, `META_AI_API_KEY`, `ANTHROPIC_API_KEY`, or `GEMINI_API_KEY`.
- Keep `git` available on `PATH`; review, diff, and file comparison flows rely on repository commands.
- `skillzAgent.pythonPath` should point at the interpreter or virtual environment you want the extension backend to use.
- The `local` provider targets the existing localhost OpenAI-compatible endpoint at `http://127.0.0.1:5051/v1`, which can be used for models such as Gemma 4.
- Node.js and `npm` are required only for extension development inside `vscode-extension/`, not for the Python backend itself.

The extension currently targets desktop VS Code APIs and uses the Python runtime as the source of truth for planner/worker behavior.

## Desktop Workbench

The standalone Electron application is under `desktop/`. It is an agent-first coding workbench rather than a VS Code clone.

Current workspace capabilities:

- lazy repository tree, Monaco tabs, dirty-state tracking, save shortcuts, and file/Git diffs
- real PTY terminals through xterm.js and `node-pty`
- repository initialization for new project folders, branch/status inspection, staging, unstaging, diff review, and commits
- Activity, Problems, Review, and Terminal dock surfaces, with diagnostics mirrored into Monaco markers
- planner conversation, bounded discovery choices, plan approval, continuous execution, durable issues, run diagnostics, work handoffs, and per-goal reports
- live model-call and tool-action feedback so long subscription-backed turns do not appear frozen
- Runtime settings for provider, live model catalog, stable/beta/live backend, token backoff, agent start/stop, Codex subscription account status, and a saved CLI executable fallback

The renderer is sandboxed and has no direct Node, filesystem, shell, or credential access. Workspace, Git, terminal, Codex status/login, and Python bridge operations run in Electron's main process behind a narrow validated preload API.

Development and packaging commands:

```bash
cd desktop
npm run typecheck
npm run build
npm run package
```

See [`desktop/README.md`](desktop/README.md) for architecture, development commands, packaging, and current distribution constraints.

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
