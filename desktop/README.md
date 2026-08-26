# skillz Workbench

Electron desktop shell for the Python planner/worker. The application is intentionally an agent-first workbench rather than a VS Code clone.

## Included vertical slice

- local workspace selection and lazy file explorer
- Monaco file tabs, dirty-state tracking, save shortcuts, and Git diff views
- real PTY terminal through xterm.js and node-pty
- Git branch/status, stage, unstage, diff, and commit controls
- existing Python agent bridge with stable/beta runtime selection, transcript, discovery choices, and plan approval
- sandboxed renderer with a narrow, validated preload API

## Development

From this directory:

```bash
npm install
npm run dev
```

The `predev` check resolves Electron before electron-vite starts. Electron 44 can restore a missing local runtime lazily, while electron-vite otherwise fails early with `Error: Electron uninstall` when `path.txt` is absent.

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
