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

Repository discovery returns forward-slash virtual paths consistently across platforms, and directory scans skip Windows junctions as well as symlinks. Run `py -3 -m unittest test_repository_discovery test_tree_commands test_discovery_remediation` from the repository root for these checks. Only file-symlink cases skip when Windows lacks the necessary privileges; junction and other workspace-boundary checks still run.

Source Control now offers local repository initialization for a new project folder. See [Starting a Git repository](#starting-a-git-repository), [Runtime boundaries](#runtime-boundaries), and [Python resolution](#python-resolution) for the corresponding setup details.

## Workspace views

Use **Hide editor / Show editor** in the title bar to switch between the split layout and an agent-focused view. The terminal/activity dock stays available, and hidden editor tabs retain unsaved changes. Opening a file or diff shows the editor automatically.

Drag the divider to the left of the agent to adjust its width. The divider also supports arrow keys (Shift for larger steps), Home/End for the width limits, and double-click to reset its width. The title-bar reset button restores both editor visibility and the default width. Preferences are saved locally per workspace and adapt to smaller windows without overwriting the preferred width.

Agent chat uses 14px body/composer text, 13px code, and 11px supporting labels at normal zoom; editor and sidebar typography remain independent.

Discovery choices, plan approvals/results, and issue lifecycle actions appear in collapsed workflow report cards, in conversation order. Expand a card for selections, outcomes, and original workflow text. New bridge messages carry explicit presentation boundaries so conversational replies appended to reports remain in the chat. The original bridge transcript is unchanged; older bridge messages use conservative format recognition.

Operator-facing turn thoughts appear above the composer while work runs (collapsible when space is tight). Full thoughts remain readable, and Activity retains tool-level history. The beta loop forwards explicit `>>th` annotations; provider-internal reasoning is not displayed.

## Issues and repository facts

**Issues** and **Repo Facts** are separate views of the same saved `repo_facts.md` ledger. Both load without starting Python or connecting a model, refresh when workspace files change, and reject late reads after a folder switch.

### Issues

Use **Issues** to manage suggested, open, and closed work. Saved issues remain visible with the agent stopped; the connected runtime supplies current status while it runs. Search and status filters cover the entire saved backlog, including older closed records. Repository-wide architecture records are excluded from the issue list.

**Continue**, **Close**, and **Reopen** remain visible on each issue card. Expand **Details** for the request, plan, lifecycle notes, blocked reason, parent/source information, and completed-goal history with validation results. These actions use the existing structured planner commands and start the configured runtime when needed. Failed actions retain the issue's state and show an error. Lifecycle actions are disabled during execution; adding an issue and accepting/ignoring suggestions can still queue behind the current action.

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

The `predev` check resolves Electron before electron-vite starts. Electron 44 can restore a missing local runtime lazily, while electron-vite otherwise fails early with `Error: Electron uninstall` when `path.txt` is absent.

Layout regression checks: `npm run test:layout`. For browser interaction checks, `npm run test:layout:preview` serves `/scripts/fixtures/workspace-view.html` with the real renderer and a mock bridge (no real files, shell, or Python process). The workspace switcher alternates two fixture repositories for checking preference isolation.

Transcript/turn-thought checks: `npm run test:timeline`; append `?workflow=1` to the fixture URL for a sample discovery, approved goal plan, and live thought.

Issue checks: `npm run test:issues`; append `?suggestions=1&failDecision=1` to test agent suggestions during execution, a failed save, retry, acceptance, and ignore using the real renderer with a mock bridge. Backend regressions: `../.venv/bin/python -m pytest -q ../test_issue_proposals.py` from this directory. Restart the agent bridge to load Python runtime changes.

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

The Codex subscription controls use a narrow helper boundary: Electron requests account/login state from the local Codex app-server, while Python model turns run ephemerally and read-only through the same local ChatGPT-managed session. No OAuth token is exposed to the renderer or copied into Skillz configuration.

On Windows, the subscription helper also discovers the CLI in `%LOCALAPPDATA%\OpenAI\Codex\bin`, including versioned runtime directories, when it is absent from the app's `PATH`.

If discovery fails, open **Runtime → Codex / ChatGPT subscription → Locate Codex CLI**. Browse for the executable or paste its full path, then choose **Save and check**. The expandable help includes Windows and macOS/Linux lookup instructions. Windows selections must be native `codex.exe` executables; macOS/Linux selections should point to `codex`. The app checks `--version` before saving, and a failed check preserves the previous selection.

The selection is stored in `runtime-settings.json` inside Electron's per-user application-data directory, outside the repository. A saved selection overrides `CODEX_CLI_PATH`; **Use automatic discovery** clears it and restores the environment/PATH/bundle lookup. A missing saved executable is reported rather than silently selecting another installation. Status and sign-in use changes immediately; the dialog requests an agent restart when model turns are still using an older path.

Reopen Runtime settings to refresh status after installing or updating Codex; restart a running agent to load helper changes. With the fixture server running, `/scripts/fixtures/runtime-settings.html?running=1` provides a browser-only recovery preview with a mock picker and CLI status. It covers browse/cancel, pasted paths (`invalid` simulates a failed check), save, reopen, reset, and agent restart guidance.

Agent protocol types and UI derivation live in `src/shared/agentTypes.ts` and `src/shared/agentCore.ts`. The reducer/selectors have no React or Electron dependency, keeping the bridge payload authoritative while allowing the desktop and VS Code shells to present it differently.

## Python resolution

The workbench uses `PYTHON_AGENT_PYTHON` when set, then the repository's `.venv/bin/python` on macOS/Linux or `.venv/Scripts/python.exe` on Windows. Otherwise it checks `python`, `py -3`, then `python3` on Windows, and `python3` then `python` on macOS/Linux. Each candidate must run Python 3; an unusable explicit override or repository virtual environment produces a setup error instead of silently using a different environment. This resolution applies to agent startup, runtime discovery, and subscription status/login.

Override the executable when needed (use an executable path, without command arguments or embedded quotes):

```bash
PYTHON_AGENT_PYTHON=/absolute/path/to/python npm run dev
```

Windows PowerShell setup, starting from the repository root:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install openai google-genai anthropic
cd desktop
npm install
npm run dev
```

For an existing interpreter, set `$env:PYTHON_AGENT_PYTHON = 'C:\Path With Spaces\Python\python.exe'` before `npm run dev`. The `py` launcher also works without a virtual environment. If Python is not installed, install Python 3 with its Windows launcher and restart your terminal before setup. Packaged apps can use the same environment override.

Runtime and terminal regression checks: `npm run test:runtime`. From the repository root, run `py -3 -m unittest test_codex_subscription test_codex_discovery test_codex_utf8 test_windows_text_encoding` (or use the repository virtual environment) for Codex discovery and Unicode subprocess regressions. These checks use local fixtures without authenticating or making model calls.

Packaged builds include the Python agent source, but they do not yet embed a Python interpreter or provider wheels. A distributable release should ship a frozen, platform-specific Python sidecar so end users do not need to configure Python themselves.

## Distribution notes

- `node-pty` is a native dependency and is rebuilt for the target Electron ABI during packaging.
- Public macOS distribution still requires a valid signing identity, hardened-runtime verification, and notarization.
- Add branded application icons before producing installers; electron-builder currently falls back to the Electron icon.

## Next architectural layer

Language Server Protocol support should be added as a separate main-process service rather than embedded into editor components. That keeps Monaco usable without tying workspace, agent, and language intelligence lifecycles together.
