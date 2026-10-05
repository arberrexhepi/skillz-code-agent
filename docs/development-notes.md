# Development notes

Implementation updates, compatibility details, and runtime behavior. Installation and model configuration are in the [runtime guide](runtime-guide.md); operational desktop details are in the [workbench guide](../desktop/README.md).

## Latest Development Update

The desktop workbench now includes **Artifacts**, an app factory for ad hoc visualizations, code explorers, dashboards, chatbots, and tools. New apps can opt into a Docker-hosted Python Skillz chatbot and a Sequelize database using SQLite, PostgreSQL, MySQL, MariaDB, or SQL Server. API Blueprints provide reusable service quickstarts, typed request collections, declarative scripts and tests, OpenAPI conversion, artifact-specific discovery/mutation commands, and a constrained Blueprint agent for validated CRUD without entering the app-building loop. A workspace-level Vault keeps encrypted provider keys outside artifact repositories. Each React/Vite/TypeScript + Express project keeps an independent Git history, selectable agent runtime, Docker-enforced folder grants, dynamic ports, and live iframe preview. The optional repository issue manager remains available as a user-installed prebuilt. Artifact agents and previews require a running Linux Docker engine. Clone with `--recurse-submodules`, or run `npm --prefix desktop run submodules:init` in an existing checkout. See the [Artifacts guide](../desktop/README.md#artifacts) and the [prebuilt clone and publish workflow](prebuilt-artifacts.md).

File paths throughout the desktop workbench now render as clickable chips, with full-path tooltips and editor navigation to optional line/column references. This includes chat, facts, issues, plans, reports, activity, diagnostics, Git, and terminal shortcuts. Windows paths and quoted Unicode filenames are supported. See [file reference chips](../desktop/README.md#file-reference-chips).

The desktop **Issues** and **Repo Facts** tabs extract different views from the saved ledger. **Issues** keeps saved issues and pending suggestions visible with the agent stopped; creating, closing, and reopening issues works directly against the saved ledger without starting Python or a model. Continue resumes work through the agent. **Repo Facts** focuses on architecture/goal facts and provenance, with links back to the related issue. Both refresh on file changes. See the [issues and repository facts guide](../desktop/README.md#issues-and-repository-facts).

Discovery can now pause at its action limit or on its last allotted turn to request **1–10 additional turns** when material ambiguity remains. The request shows the turn count, reason, a short investigation proposal, unresolved questions, and findings collected so far.

- **Allow more turns** resumes the same discovery conversation, retaining its history, context, and cumulative budget. Each approved turn also adds one tool-action slot; discovery stays read-only.
- **Plan with current findings** ends discovery and passes the unresolved questions and unperformed checks to the goal planner, which must state assumptions, risks, and necessary validation rather than treat ambiguity as resolved.
- Stable and beta workers share validation and budget accounting. Continuous/Auto mode also pauses for this decision; it never grants an extension automatically. Once discovery finishes, the plan returns for review.
- In the CLI, use `approve` or `no` for the pending extension (`/discovery` shows it again). An additional extension requires a new explicit decision. Paused worker context is held in the running session; stopping the agent or switching folders ends that session.


The desktop workbench now handles common Windows setup failures, provides a manual Codex location fallback, and helps users start version control in a new project folder:

- **Windows Python discovery:** agent startup, runtime options, and Codex status/login share interpreter detection. The workbench checks an explicit override and the repository virtual environment, then tries `python`, the Windows `py -3` launcher, and `python3`. This fixes `spawn python ENOENT` when Python is installed through the launcher but absent from `PATH`.
- **Codex discovery and manual setup:** Windows discovery recognizes the local Codex desktop application's versioned runtime directories. In Runtime settings, users can browse or paste a CLI executable, validate it with **Save and check**, and keep the selection on their computer. **Use automatic discovery** restores normal lookup; changing a running agent's CLI path displays restart guidance.
- **UTF-8 message handling:** Python bridge streams, toolbelt results, Codex subprocess pipes, Git output, and diagnostic commands use explicit UTF-8. Repository text reads and edits also specify UTF-8. Prompts, responses, account status, and model discovery handle accented text, multilingual content, and emoji without Windows code-page conversion. This fixes the invalid UTF-8 stdin error that could occur even after successful Codex authentication.
- **Windows terminal compatibility:** the embedded terminal uses node-pty's native Windows encoding behavior, removing the unsupported encoding warning.
- **New repository setup:** Source Control offers **Initialize repository** for folders without Git metadata, then shows files ready for staging and a first commit. It recognizes existing repositories and worktrees, preserves actionable errors, and rejects initialization requests for a folder the user has already switched away from.
- **Consistent repository discovery:** file, directory, and symbol searches return forward-slash virtual paths on Windows, macOS, and Linux. Directory scans skip symlinks and Windows junctions, keeping searches within the workspace and avoiding linked-directory traversal errors.
- **Regression coverage:** focused Python and desktop tests cover discovery, saved CLI settings, UTF-8 subprocess traffic, terminal options, and Git initialization. Browser fixtures exercise setup, retry, and concurrent refresh behavior; Git fixtures account for Windows line endings and symlink privileges.

See [Windows quick start](runtime-guide.md#windows-quick-start) and the [desktop guide](../desktop/README.md) for setup and verification commands. Packaged builds still require a separately installed Python interpreter and provider dependencies.

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
