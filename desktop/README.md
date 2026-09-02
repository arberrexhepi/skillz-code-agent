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

## Development

From this directory:

```bash
npm install
npm run dev
```

The `predev` check resolves Electron before electron-vite starts. Electron 44 can restore a missing local runtime lazily, while electron-vite otherwise fails early with `Error: Electron uninstall` when `path.txt` is absent.

Layout regression checks: `npm run test:layout`. For browser interaction checks, `npm run test:layout:preview` serves `/scripts/fixtures/workspace-view.html` with the real renderer and a mock bridge (no real files, shell, or Python process). The workspace switcher alternates two fixture repositories for checking preference isolation.

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
