# skillz Workbench

Electron desktop shell for the Python planner/worker. The application is intentionally an agent-first workbench rather than a VS Code clone.

## Included vertical slice

- local workspace selection and lazy file explorer
- an Issues tab for saved issue management, suggestions, lifecycle details, and completed-goal history
- a Repo Facts tab for architecture/goal facts and provenance
- Monaco file tabs, dirty-state tracking, save shortcuts, and Git diff views
- real PTY terminal through xterm.js and node-pty
- Git branch/status, stage, unstage, diff, and commit controls
- exact Python bridge parity for planner actions, worker actions, runtime discovery/switching, backoff, and lifecycle progress
- Runtime settings for the additive `codex-subscription` provider, including local ChatGPT authentication, plan, CLI, and live-model status while preserving the existing OpenAI API provider
- workspace-native agent UI: calm conversation rail, pinned lifecycle decisions, durable issue management and repository facts, and continuous-mode status
- bottom workspace dock for Terminal, Activity, Problems, and Review; diagnostics also become Monaco markers
- per-workspace view preferences: hide/show the editor and resize the agent panel without losing tabs, drafts, or terminal sessions
- sandboxed renderer with a narrow, validated preload API

## Windows compatibility improvements

The workbench now discovers Python through the Windows launcher as well as executable paths and repository virtual environments. Codex discovery also checks the Windows desktop application's versioned runtimes. Runtime settings provide a native file picker, a pasted-path fallback, validation before saving, and restart guidance when a running agent still uses the previous CLI selection.

Python bridge streams, toolbelt results, and Codex subprocess pipes explicitly use UTF-8, including streaming and non-streaming model turns, account/login responses, version checks, and app-server traffic. This fixes invalid UTF-8 stdin failures after successful authentication. Git and diagnostic output also use UTF-8, and repository reads and edits preserve Unicode instead of using the Windows code page. The Windows terminal uses native node-pty encoding, avoiding the unsupported encoding warning.

Repository discovery returns forward-slash virtual paths consistently across platforms, and directory scans skip Windows junctions as well as symlinks. Run `py -3 -m unittest tests.test_repository_discovery tests.test_tree_commands tests.test_discovery_remediation` from the repository root for these checks. Only file-symlink cases skip when Windows lacks the necessary privileges; junction and other workspace-boundary checks still run.

Source Control now offers local repository initialization for a new project folder. See [Starting a Git repository](#starting-a-git-repository), [Runtime boundaries](#runtime-boundaries), and [Python resolution](#python-resolution) for the corresponding setup details.

## Workspace views

Use **Hide editor / Show editor** in the title bar to switch between the split layout and an agent-focused view. The terminal/activity dock stays available, and hidden editor tabs retain unsaved changes. Opening a file or diff shows the editor automatically.

Drag the divider to the left of the agent to adjust its width. The divider also supports arrow keys (Shift for larger steps), Home/End for the width limits, and double-click to reset its width. The title-bar reset button restores both editor visibility and the default width. Preferences are saved locally per workspace and adapt to smaller windows without overwriting the preferred width.

Agent chat uses 14px body/composer text, 13px code, and 11px supporting labels at normal zoom; editor and sidebar typography remain independent.

Discovery choices, plan approvals/results, and issue lifecycle actions appear in collapsed workflow report cards, in conversation order. Expand a card for selections, outcomes, and original workflow text. New bridge messages carry explicit presentation boundaries so conversational replies appended to reports remain in the chat. The original bridge transcript is unchanged; older bridge messages use conservative format recognition.

Operator-facing turn thoughts appear above the composer while work runs (collapsible when space is tight). Full thoughts remain readable, and Activity retains tool-level history. The beta loop forwards explicit `>>th` annotations; provider-internal reasoning is not displayed.

## Artifacts

Use **Artifacts** in the header to build visualizations and tools with their own agent conversations and live iframe previews. Each React/Vite/TypeScript + Express project has an independent Git history as a submodule in your chosen library. Nothing is pushed automatically.

1. Start **Docker Desktop with Linux containers** (or a local Linux Docker Engine). Both artifact previews and artifact agents require Docker. An unavailable engine produces a setup error; execution stays stopped.
2. **Choose artifacts folder** selects an empty directory or an existing skillz library. skillz initializes a parent Git repository and remembers the selection.
3. Choose **New artifact**, name it, and describe what to build. **Agent runtime** uses the same provider, model, backend, and Codex sign-in/path controls as workspace chat. Its selection is saved locally and used for the first request and when reopening the artifact.
4. Under **Allowed system directories**, add folders the artifact and its agent may read, such as Documents. **Allow active workbench repository reads** shares the repository open when that session starts. Both options default to no additional access. Source facts and memory can also be shared individually.
5. **Create & ask agent** scaffolds the project and sends the request to its separate agent. Discovery, plans, approvals, and follow-up instructions use the usual workflow. The first start builds a reusable Docker image containing the harness, Node, Python, and Git; allow time for the initial download.
6. **Start preview** installs npm dependencies in Docker and launches Express/Vite. Its local URL loads directly in a sandboxed iframe, with native-resolution rendering, normal scrolling, typing, and paste. The page resizes with the panel and retains its state across artifact and workbench tab switches. **Reload preview** starts a fresh page. **Stop** ends its preview; closing the tab also stops its agent. No Playwright browser download is needed for this view.

### Ready-made artifacts

Ready-made artifacts appear as optional installs in the artifact library and creation screen; none are installed automatically. **Repository issue manager** is the first bundled option. During installation, add one or more repositories under **Allowed system directories** and explicitly enable **Allow changes**. The running artifact can then initialize `repo_facts.md`, create and activate issues, and close or reopen existing schema-version-2 issues without starting an agent. It supports multiple granted repositories and shows read-only grants without mutation controls.

The maintained issue-manager source is a Git submodule at `desktop/prebuilt-artifacts/repo-issue-manager`, and the desktop installer copies that version into the user's artifact library as a new independent artifact repository. Packaged builds include the submodule checkout. Clone the project and its submodules together:

```bash
git clone --recurse-submodules https://github.com/arberrexhepi/skillz-code-agent.git
```

For an existing clone, initialize all registered submodules from the repository root:

```bash
npm --prefix desktop run submodules:init
```

To move submodules to the latest commit on their configured remote branches and expose the updated Git pointers for a parent-repository commit:

```bash
npm --prefix desktop run submodules:update
```

Keep `prebuilt/repo-issue-manager` as the independent artifact source branch. Commit submodule pointer updates through the normal `app` → `dev` → `main` promotion flow; do not merge the artifact source branch into those application branches.

### Install and repair capabilities

The artifact landing and creation screens check Python, Git, the Linux Docker engine, the selected provider SDK and API-key configuration, the artifact runtime image, and the optional Playwright inspection browser. **Install capabilities** downloads only missing SDK/runtime components, with live progress, bounded logs, and retry after failure. Python provider packages installed here use a separate environment in the desktop's per-user settings folder; existing usable provider installations are reused. Missing Python, Git, and Docker get official download buttons and **Recheck**. The OS installer handles system prerequisites and Docker setup.

Checks run again when opening existing artifacts and after agent/preview state changes. A missing component or an outdated runtime image shows **Repair capabilities**. Each artifact's **Setup** tab offers the same checks and installation flow; completed setups collapse to a Ready summary. The runtime image is matched to the current harness content, and the optional browser check launches the version required by the installed Playwright package. Missing Playwright does not block creation or trigger a repair notice. **Install Playwright** in Setup downloads it separately for inspection tooling; the screenshot/input service remains available for future Skillz agent visual inspection.

API-key providers offer **Save API key** and removal directly in setup. Saved keys use [Electron safeStorage](https://www.electronjs.org/docs/latest/api/safe-storage), stay outside artifact repositories, and are passed only to that provider's local artifact model helper. No plaintext fallback is used when secure storage is unavailable. Environment or harness `.env` credentials continue to work; a saved key takes precedence. Codex uses the existing **Agent runtime** sign-in controls. Readiness checks confirm local configuration without making paid model calls or validating credentials with a provider.

### File access and enforcement

The artifact repository is writable at `/repo`. Approved directories are mounted at `/reads/<id>` and default to read only; **Allow changes** is an explicit per-folder write grant. The optional workbench repository is always read only at `/reads/workspace`. Source context snapshots are mounted read-only at `/context`. Other host folders and the Docker socket are not mounted. Containers have a read-only system filesystem, no added capabilities, no privilege escalation, and bounded memory/process counts. Nested host mounts are excluded. Direct filesystem calls, shell commands, dependency install scripts, and validation tools run inside this boundary.

Permission records and source-sharing metadata live in desktop settings outside the generated repositories. Editing `artifact.json` or `.artifact-local.json` cannot grant additional access. Change or revoke permissions in the artifact's **File access** tab. Saving stops both its preview and agent, including containers left by an earlier app crash, before applying changes; their next start uses the updated mounts. Imported repositories start with no external grants.

The agent receives the granted folder names and container paths. Stable and beta read tools accept those paths; structured mutation tools remain repository-bound. Model requests go through a separate, model-only local helper, preserving existing provider credentials and Codex authentication without mounting the host credential directories into the artifact containers. Host Python and the selected provider's SDK/CLI remain required. The Codex model helper disables its own shell, image/file, browser, plugin, and delegation tools so workspace actions stay in the Skillz container. See the [Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference) for these controls.

Artifact agent startup checks the selected provider's local SDK and configuration before starting Docker or accepting a request. Missing SDK errors name the exact host Python executable and its installation command. For Gemini, install `google-genai` in that environment and configure `GEMINI_API_KEY` in the harness `.env` or the environment launching the desktop. Restart the artifact agent, then choose **Send original request** to retry a newly created artifact; there is no need to recreate it. These checks do not make model calls or validate an API key against the provider.

The template provides `src/files.ts`: `files.roots()`, `files.list(id, relativePath)`, `files.readText(id, relativePath)`, `files.read(id, relativePath)`, and `files.writeText(id, relativePath, content, expectedSha256)`. Reads use `/files/:id/list` and `/files/:id/read`; writes use `PUT /files/:id/write` and only work for an explicit write grant. Writes are bounded to 5 MB, remain inside the canonical granted root, use atomic replacement, preserve the existing mode, and require the SHA-256 of the exact loaded content. A concurrent change returns HTTP 409 instead of being overwritten. The `repo` ID reads the artifact itself and remains read only through the browser gateway. Docker mount modes enforce the same grants when generated backend code or shell commands bypass this API.

### Runtime and connections

The desktop allocates the server port dynamically and Docker publishes an available port on host `127.0.0.1`. Vite middleware, frontend routes, API routes, and HMR share that origin. Preserve `SKILLZ_ARTIFACT_PORT` and `SKILLZ_ARTIFACT_HOST` in `server/index.ts`; no project ports are hardcoded. `npm run build` typechecks and builds the frontend inside the artifact agent. Dependencies live in a per-artifact Docker volume, separate from Windows/macOS node_modules. These volumes persist across stops to speed up later starts.

In **API connections**, add an ID, label, HTTP/WebSocket transport, upstream URL, method, and request/response JSON Schemas. The agent can edit `.artifact/apis.json`. HTTP calls use `/api/<id>` and WebSockets use `/ws/<id>`. Unknown IDs, wrong methods, invalid payloads, redirects, and oversized responses are rejected. Connection edits apply to new requests/connections. To reach a service on the host, use `host.docker.internal` in its URL instead of container localhost.

`headerEnv` maps upstream header names to environment-variable names, e.g. `{"Authorization":"SCHEMA_API_AUTH"}`. Set values in the desktop process environment and restart the preview after changing the referenced names or values. Only referenced variables are passed to the preview container. Frontend previews access their own origin and use the gateway for external APIs. Electron enforces that policy on running artifact URLs, including generated and older templates. Artifact frames receive no Node or workbench IPC bridge; only the main workbench frame may invoke privileged IPC. Cross-origin navigation, popups, downloads, nested frames, service workers, and device/filesystem permission requests are blocked. The iframe runs in Electron while the Express backend and agent tools remain in Docker. API definitions are connection contracts; they do not restrict arbitrary backend network traffic.

Selected facts and memory are copied to dedicated desktop-managed context folders, refreshed every two seconds and before startup, then mounted read-only. This works without symlink privileges on Windows and handles atomic source replacement/removal. Read them through `/context/repo-facts` and `/context/memory`. The artifact agent's own facts and memory remain in its repository. Desktop agent sessions use workspace-local observability files; CLI behavior is unchanged unless `SKILLZ_OBSERVABILITY_PATH` is set.

Scaffold commits use a command-local `skillz Workbench` identity. Commit artifact changes in the child repository, then stage its updated submodule pointer in the parent. Configure child and parent remotes before publishing; initial submodule URLs are local placeholders. Source paths, context snapshots, permission records, dependencies, and agent session files are not committed.

For Docker startup errors, check Docker Desktop's engine status first. An `EAI_AGAIN` npm failure indicates container DNS/network configuration; check custom daemon DNS overrides and Docker proxy settings. The workbench does not change system Docker settings or fall back to unrestricted host execution.

`npm run test:artifacts:preview` opens a hidden Electron window to check native iframe input, resizing, retained state, reload, HTTP/WebSocket requests, and the frame security boundary; it needs neither Docker nor Playwright Chromium. `npm run test:artifacts:setup` covers capability checks, installation retry, managed environments, and encrypted-key handling. `npm run test:artifacts:setup:integration` exercises actual provider installation and Docker image preparation without model calls. Run Docker integration suites separately on Windows to avoid competing Docker context metadata locks. `npm run test:artifacts` covers Git submodules, saved runtime selection, permission storage, context snapshots, and separate bridge sessions. `npm run test:artifacts:sandbox` uses real Docker containers to test direct and subprocess write denials, Windows junction escape attempts, stable/beta agent reads and startup, and revocation. `npm run test:artifacts:integration` installs dependencies and Chromium, builds the generated project, tests gateways, and renders independent previews. The browser UI fixture is `/scripts/fixtures/workspace-view.html?artifacts=1`.

## macOS artifact compatibility

Artifact creation, container execution, model helpers, and previews use the same pipeline on macOS and Windows. Host paths are resolved with the platform path API; generated code sees `/repo`, `/reads/<id>`, and `/context`. The Docker image does not force an x86 architecture. macOS containers use the current user's UID/GID and a separate Linux dependency volume.

For launches from Finder, child processes keep the existing PATH precedence and add Intel/Apple Silicon Homebrew directories, `~/.docker/bin`, and Docker application-bundle binaries. This also makes Docker credential helpers discoverable during image builds; no shell startup scripts are executed and the parent environment is unchanged. See [Docker's macOS binary locations](https://docs.docker.com/desktop/setup/install/mac-permission-requirements/). Python discovery requires 3.10 or newer and checks Homebrew locations if PATH resolves to an older system interpreter. Explicit interpreter overrides and repository virtual environments remain authoritative.

Finder's regular `.DS_Store` files are preserved when creating an otherwise empty library or artifact and excluded from new Git histories. Other files, directories, and links still prevent reuse. Tests canonicalize temporary roots so macOS `/var` → `/private/var` aliases do not cause false failures.

Artifact model helpers run in isolated POSIX process groups. Stopping or timing out a helper also stops its CLI descendants, including when the direct helper has already exited. Group termination is restricted to helpers launched this way; Windows retains its process-tree termination. See [Node's process-group behavior](https://nodejs.org/api/child_process.html#optionsdetached).

`npm run test:artifacts` includes Finder metadata, launch-environment, and helper-cleanup regressions; the two real process-group cases run on macOS/Linux and skip on Windows. `npm run test:runtime` covers macOS/Windows interpreter resolution and host model switching. A native macOS smoke run is still required for Finder launch, Keychain-backed credentials, Docker folder sharing, preview startup, and both agent backends. Simulated macOS branches and Linux process tests do not establish native macOS compatibility.

## Issues and repository facts

**Issues** and **Repo Facts** are separate views of the same saved `repo_facts.md` ledger. Both load without starting Python or connecting a model, refresh when workspace files change, and reject late reads after a folder switch.

### Issues

Use **Issues** to manage suggested, open, and closed work. Saved issues remain visible with the agent stopped; the connected runtime supplies current status while it runs. Search and status filters cover the entire saved backlog, including older closed records. Repository-wide architecture records are excluded from the issue list.

**Continue**, **Close**, and **Reopen** remain visible on each issue card. Expand **Details** for the request, plan, lifecycle notes, blocked reason, parent/source information, and completed-goal history with validation results. Creating, closing, and reopening issues update `repo_facts.md` directly while the runtime is stopped or unavailable; they require neither Python nor a model connection. If the agent is already running, the same actions use its structured bridge so planner memory stays synchronized. **Continue** starts the configured runtime because it resumes agent work. Failed actions retain the issue's state and show an error. Lifecycle actions are disabled during execution; adding an issue and accepting/ignoring suggestions can still queue behind the current action.

Pending agent suggestions are loaded from `.agent-issue-proposals.json`. **Accept** creates an issue without replacing the active task; **Ignore** removes the proposal from the pending list. Reading either file does not modify it. A malformed ledger does not hide readable suggestions, and a suggestion-storage error does not hide readable issues.

Run `npm run test:issues` for projection, saved-file, workspace-boundary, IPC, and management tests. `/scripts/fixtures/workspace-view.html?issues=1` previews the complete workbench; `/scripts/fixtures/issues-interactions.html` exercises saved issues, lifecycle actions, proposal decisions, failures, duplicate clicks, and folder switches.

### Repository facts

Use **Repo Facts** to browse architecture and goal facts. Search fact keys, values, and source actions; filter by type or recording scope. Expand **Provenance** for the source action, run, and step. The related issue link opens that issue's management view and details. Counts reflect retained facts; closed issues with no facts do not appear as empty fact cards.

**Open source** opens the original Markdown in the editor. Missing files, invalid JSON, unsupported schemas, and read errors are shown separately. Current schema 2 and legacy flat fact lists are supported; viewing a legacy file does not migrate or modify it.

Run `npm run test:facts` for parser, file-reader, workspace-boundary, IPC, and rendering checks. `/scripts/fixtures/workspace-view.html?facts=1` previews the facts tab; `/scripts/fixtures/repo-facts-interactions.html` checks filtering, refresh races, switching repositories, source opening, and retry.

## File reference chips

File references in conversation, Repo Facts, issue details, plans, reports, activity, diagnostics, and runtime messages share a compact chip with the filename emphasized and the full path in its tooltip. Click or focus a chip and press Enter to open it in the editor. Git file chips open the source; the adjacent **Δ** button opens its diff.

The viewer recognizes common source/configuration filenames, relative paths, `/repo/...` references, absolute paths within the selected workspace, and quoted paths containing spaces or Unicode. `/repo/` means the selected workspace root. References such as `src/app.ts:12:3`, `src/app.ts#L12C3`, and compiler-style `src/app.ts(12,3)` jump to the recorded location. Reopening an editor tab retains unsaved edits. Paths outside the workspace are shown as inactive chips; missing files report an error when opened.

Terminal output has clickable file links and a **Files** strip with up to eight recent references. Terminal and editor source text stay intact, as do fenced code samples and web links in Markdown. New prose surfaces can use `PathText`, and structured file fields can use `PathChip`, with navigation supplied by the workbench context.

Run `npm run test:paths` for detection, Windows/Unix resolution, terminal cell ranges, and editor navigation tests. Preview all panels with `/scripts/fixtures/workspace-view.html?paths=1`; `/scripts/fixtures/path-chip-interactions.html` exercises Markdown, metadata chips, workspace switches, and sanitization.

## Starting a Git repository

For an opened folder without Git, Source Control shows **Start tracking your project** with the folder path and an **Initialize repository** action. Clicking it runs local `git init` using Git's configured default branch, then displays the files ready for staging. Users choose what to stage and create their first commit; initialization does not stage files, commit, or publish a remote.

Existing repositories, parent repositories, and linked worktrees are detected normally. Git installation, permission, and damaged local metadata errors remain visible with a retry action. Initialization is tied to the displayed folder and is rejected if the workspace changes before it starts.

Run `npm run test:git` for Git regressions. Test repositories disable automatic line-ending conversion locally, independent of the user's Git configuration. Directory traversal checks use junctions on Windows; the file-symlink case reports a skip if Windows requires additional symlink privileges. With the fixture server running, `/scripts/fixtures/git-initialize.html` previews setup without touching Git. Add `?failInit=1` to test retry, `?gitError=1` for an unavailable Git executable, or `?slowRefresh=1` for a refresh arriving during initialization.

## Agent-authored issue suggestions

An agent can dispatch `propose-issue {"summary":"…","reason":"Why outside this goal","evidence":"Already observed evidence","run_issue_ids":["run-…"]}` (classic worker: JSON action type `propose_issue`). Optional `paths` capture findings without run diagnostics. No extra investigation is needed just to file a suggestion.

After the proposal is successfully persisted, linked diagnostics immediately stop gating the current goal—even before user review. Identical findings remain deferred after re-ingestion/restart; different findings and different goals do not inherit the deferral. A failed proposal write releases nothing. Previously passing focused validation is preserved, but a proposal cannot substitute for missing validation of the requested change. Raw failing checks remain failures and completion reports disclose separately recorded findings.

The Issues tab has an **Agent suggestions** inbox. **Accept** promotes a suggestion into an open, inactive issue; **Ignore** removes it from the inbox without creating an executable issue. Both controls work while another task runs, with decisions queued through the bridge. Neither action changes the current task. The workspace-local `.agent-issue-proposals.json` retains evidence and fingerprints, including ignored records, to prevent repeated proposals; it and its lock are gitignored. Only user-facing bridge actions can accept or ignore suggestions.

## Development

From this directory:

```bash
npm install
npm run dev
```

The development renderer starts at `http://localhost:55173` instead of Vite's common port 5173, leaving repository applications free to use their usual defaults. If 55173 is already occupied, Vite selects the next available port and Electron follows the resulting URL automatically.

The `predev` check resolves Electron before electron-vite starts. Electron 44 can restore a missing local runtime lazily, while electron-vite otherwise fails early with `Error: Electron uninstall` when `path.txt` is absent.

Layout regression checks: `npm run test:layout`. For browser interaction checks, `npm run test:layout:preview` serves `/scripts/fixtures/workspace-view.html` with the real renderer and a mock bridge (no real files, shell, or Python process). The workspace switcher alternates two fixture repositories for checking preference isolation.

Transcript/turn-thought checks: `npm run test:timeline`; append `?workflow=1` to the fixture URL for a sample discovery, approved goal plan, and live thought.

Issue checks: `npm run test:issues`; append `?suggestions=1&failDecision=1` to test agent suggestions during execution, a failed save, retry, acceptance, and ignore using the real renderer with a mock bridge. Backend regressions: `../.venv/bin/python -m pytest -q ../tests/test_issue_proposals.py` from this directory. Restart the agent bridge to load Python runtime changes.

Plan-review checks: `npm run test:plans`; append `?plan=1` for a full five-goal plan, or `?plan=1&failRevision=1` to test revision failure and retry. The inline card stays compact: a two-line summary, goal count, and review/change controls. The reading dialog shows the complete plan with approval/rejection actions. Suggest plan changes submits explicit revision feedback (not chat commands) and requires a fresh approval afterward.

With the fixture server running, `/scripts/fixtures/plan-review-interactions.html` runs native-dialog interaction regressions against deferred mock actions. It covers dismissing while execution/revision is pending, duplicate-action protection, late responses, and retryable failures. Closing a review never cancels an agent action; approval/resume/rejection dismiss immediately.

Production checks and local packaging:

```bash
npm run typecheck
npm run build
npm run package
```

The local macOS app bundle is written to `release/mac-arm64/skillz Workbench.app` on Apple Silicon.

## Runtime boundaries

The React renderer owns presentation only. It cannot import Node, access arbitrary files, or spawn commands.

```text
React + Monaco + xterm.js
           │ typed IPC
Electron preload
           │ validated IPC
Electron main
  ├── workspace and Git services
  ├── node-pty session manager
  └── Python agent process manager
           │ NDJSON stdin/stdout
Python planner/worker
```

The desktop process reuses the same `--extension-bridge` protocol as the VS Code extension.

For both repository and artifact agents, provider/model/backend selections made while stopped are used automatically by **Start agent**, sending a message, or **Send original request**. Closing Runtime settings keeps the selection for that workspace session. Catalog refreshes cannot replace it with an older provider. For a running agent, the drawer identifies the active provider/model and offers **Apply to running agent**; the backend remains locked until stopped. Artifact provider changes validate host setup and load the selected provider's credentials before applying, preserving the current runtime if the change fails.

The browser fixture `/scripts/fixtures/runtime-selection.html` exercises both independent workspace instances, delayed catalog responses, closing/reopening settings, start/send behavior, and failed live changes. `npm run test:runtime` covers host preflight and broker switching; `py -3 -m unittest tests.test_gemini_messages tests.test_artifact_model_host` covers model routing and Gemini's disabled automatic function calling (the SDK-specific test requires `google-genai`). The harness retains responsibility for tool dispatch and approval.

The Codex subscription controls use a narrow helper boundary: Electron requests account/login state from the local Codex app-server, while Python model turns run ephemerally and read-only through the same local ChatGPT-managed session. No OAuth token is exposed to the renderer or copied into Skillz configuration.

On Windows, the subscription helper also discovers the CLI in `%LOCALAPPDATA%\OpenAI\Codex\bin`, including versioned runtime directories, when it is absent from the app's `PATH`.

If discovery fails, open **Runtime → Codex / ChatGPT subscription → Locate Codex CLI**. Browse for the executable or paste its full path, then choose **Save and check**. The expandable help includes Windows and macOS/Linux lookup instructions. Windows selections must be native `codex.exe` executables; macOS/Linux selections should point to `codex`. The app checks `--version` before saving, and a failed check preserves the previous selection.

The selection is stored in `runtime-settings.json` inside Electron's per-user application-data directory, outside the repository. A saved selection overrides `CODEX_CLI_PATH`; **Use automatic discovery** clears it and restores the environment/PATH/bundle lookup. A missing saved executable is reported rather than silently selecting another installation. Status and sign-in use changes immediately; the dialog requests an agent restart when model turns are still using an older path.

Reopen Runtime settings to refresh status after installing or updating Codex; restart a running agent to load helper changes. With the fixture server running, `/scripts/fixtures/runtime-settings.html?running=1` provides a browser-only recovery preview with a mock picker and CLI status. It covers browse/cancel, pasted paths (`invalid` simulates a failed check), save, reopen, reset, and agent restart guidance.

Agent protocol types and UI derivation live in `src/shared/agentTypes.ts` and `src/shared/agentCore.ts`. The reducer/selectors have no React or Electron dependency, keeping the bridge payload authoritative while allowing the desktop and VS Code shells to present it differently.

## Python resolution

The workbench uses `PYTHON_AGENT_PYTHON` when set, then the repository's `.venv/bin/python` on macOS/Linux or `.venv/Scripts/python.exe` on Windows. Otherwise it checks `python`, `py -3`, then `python3` on Windows, and `python3` then `python` on macOS/Linux. Each candidate must run Python 3.10 or newer; an unusable explicit override or repository virtual environment produces a setup error instead of silently using a different environment. This resolution applies to agent startup, runtime discovery, and subscription status/login.

Override the executable when needed (use an executable path, without command arguments or embedded quotes):

```bash
PYTHON_AGENT_PYTHON=/absolute/path/to/python npm run dev
```

Windows PowerShell setup, starting from the repository root:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install openai google-genai anthropic pytest
cd desktop
npm install
npm run dev
```

For an existing interpreter, set `$env:PYTHON_AGENT_PYTHON = 'C:\Path With Spaces\Python\python.exe'` before `npm run dev`. The `py` launcher also works without a virtual environment. If Python is not installed, install Python 3 with its Windows launcher and restart your terminal before setup. Packaged apps can use the same environment override.

Runtime and terminal regression checks: `npm run test:runtime`. From the repository root, run `py -3 -m unittest tests.test_codex_subscription tests.test_codex_discovery tests.test_codex_utf8 tests.test_windows_text_encoding` (or use the repository virtual environment) for Codex discovery and Unicode subprocess regressions. These checks use local fixtures without authenticating or making model calls.

Packaged builds include the Python agent source, but they do not yet embed a Python interpreter or provider wheels. A distributable release should ship a frozen, platform-specific Python sidecar so end users do not need to configure Python themselves.

## Distribution notes

- `node-pty` is a native dependency and is rebuilt for the target Electron ABI during packaging.
- Public macOS distribution still requires a valid signing identity, hardened-runtime verification, and notarization.
- Add branded application icons before producing installers; electron-builder currently falls back to the Electron icon.

## Next architectural layer

Language Server Protocol support should be added as a separate main-process service rather than embedded into editor components. That keeps Monaco usable without tying workspace, agent, and language intelligence lifecycles together.
