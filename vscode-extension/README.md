# Python Agent VS Code Extension

Local desktop VS Code extension shell for the Python planner/worker runtime in the parent repository.

## What This Extension Does

- launches the Python agent runtime as a background bridge process
- opens a webview panel for planner and worker interaction
- renders planner state, worker runtime state, facts, diagnostics, and review output
- exposes planner and worker suggested actions as panel buttons
- opens files, diagnostic locations, review reports, and working-tree-vs-`HEAD` diffs

## Prerequisites

- VS Code desktop
- Node.js and `npm`
- Python 3.13 recommended
- `git` on `PATH`
- provider credentials in the environment seen by VS Code, such as `OPENAI_API_KEY` or `GEMINI_API_KEY`

For this repository, a practical Python interpreter setting is:

```text
/.venv/bin/python
```

## Setup

From this folder:

```bash
npm install
npm run compile
npm test
npm run test:integration
```

## Launch In Development Mode

1. Open the `vscode-extension/` folder in VS Code.
2. Press `F5`.
3. Choose `Run Python Agent Extension` from [.vscode/launch.json](.vscode/launch.json).
4. A new Extension Development Host window will open.
5. In that new window, open the repository you want the agent to work on.

## Configure The Extension

The extension settings are defined in [package.json](package.json):

- `pythonAgent.provider`
- `pythonAgent.model`
- `pythonAgent.pythonPath`

Recommended local values for this repo:

- `pythonAgent.provider = gemini` or `openai`
- `pythonAgent.model = gemini-3-flash-preview` or your OpenAI model
- `pythonAgent.pythonPath = /.venv/bin/python`

## Use The Extension

1. In the Extension Development Host, open the Command Palette.
2. Run `Python Agent: Open Agent`.
3. Enter a request in the panel.
4. Use the planner buttons to approve or reject plans and choose discovery depth.
5. Use the worker buttons for review, validation, finish, and recovery flows.
6. Click surfaced diagnostics, file paths, and review actions to open files and diffs in the editor.

## Typical Flow

1. Submit a task.
2. If needed, choose a discovery depth.
3. Review the proposed plan.
4. Approve the plan.
5. Follow worker actions and diagnostics until the change is complete.

## Notes

- The extension currently targets stable desktop VS Code APIs only.
- The Python runtime in the parent repository remains the source of truth for planner, worker, safety, and diagnostics behavior.
- If the panel cannot start the backend, first check `pythonAgent.pythonPath`, API keys, and that VS Code was launched from an environment that can see them.