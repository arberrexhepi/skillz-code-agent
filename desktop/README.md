# skillz Workbench

Electron desktop shell for the Python planner/worker. The application is intentionally an agent-first workbench rather than a VS Code clone.

## Included vertical slice

- local workspace selection and lazy file explorer
- Monaco file tabs, dirty-state tracking, save shortcuts, and Git diff views
- real PTY terminal through xterm.js and node-pty
- Git branch/status, stage, unstage, diff, and commit controls
- exact Python bridge parity for planner actions, worker actions, runtime discovery/switching, backoff, and lifecycle progress
- Runtime settings for the additive `codex-subscription` provider, including local ChatGPT authentication, plan, CLI, and live-model status while preserving the existing OpenAI API provider
- workspace-native agent UI: calm conversation rail, pinned lifecycle decisions, durable issues/run facts, and continuous-mode status
- bottom workspace dock for Terminal, Activity, Problems, and Review; diagnostics also become Monaco markers
- per-workspace view preferences: hide/show the editor and resize the agent panel without losing tabs, drafts, or terminal sessions
- sandboxed renderer with a narrow, validated preload API

## Workspace views

Use **Hide editor / Show editor** in the title bar to switch between the split layout and an agent-focused view. The terminal/activity dock stays available, and hidden editor tabs retain unsaved changes. Opening a file or diff shows the editor automatically.

Drag the divider to the left of the agent to adjust its width. The divider also supports arrow keys (Shift for larger steps), Home/End for the width limits, and double-click to reset its width. The title-bar reset button restores both editor visibility and the default width. Preferences are saved locally per workspace and adapt to smaller windows without overwriting the preferred width.

Agent chat uses 14px body/composer text, 13px code, and 11px supporting labels at normal zoom; editor and sidebar typography remain independent.

Discovery choices, plan approvals/results, and issue lifecycle actions appear in collapsed workflow report cards, in conversation order. Expand a card for selections, outcomes, and original workflow text. New bridge messages carry explicit presentation boundaries so conversational replies appended to reports remain in the chat. The original bridge transcript is unchanged; older bridge messages use conservative format recognition.

Operator-facing turn thoughts appear above the composer while work runs (collapsible when space is tight). Full thoughts remain readable, and Activity retains tool-level history. The beta loop forwards explicit `>>th` annotations; provider-internal reasoning is not displayed.

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

Agent protocol types and UI derivation live in `src/shared/agentTypes.ts` and `src/shared/agentCore.ts`. The reducer/selectors have no React or Electron dependency, keeping the bridge payload authoritative while allowing the desktop and VS Code shells to present it differently.

## Python resolution

In development, the workbench prefers `../.venv/bin/python` (or the Windows equivalent). Otherwise it uses `python3`/`python` from `PATH`. Override this explicitly when needed:

```bash
PYTHON_AGENT_PYTHON=/absolute/path/to/python npm run dev
```

Packaged builds include the Python agent source, but they do not yet embed a Python interpreter or provider wheels. A distributable release should ship a frozen, platform-specific Python sidecar so end users do not need to configure Python themselves.

## Distribution notes

- `node-pty` is a native dependency and is rebuilt for the target Electron ABI during packaging.
- Public macOS distribution still requires a valid signing identity, hardened-runtime verification, and notarization.
- Add branded application icons before producing installers; electron-builder currently falls back to the Electron icon.

## Next architectural layer

Language Server Protocol support should be added as a separate main-process service rather than embedded into editor components. That keeps Monaco usable without tying workspace, agent, and language intelligence lifecycles together.
