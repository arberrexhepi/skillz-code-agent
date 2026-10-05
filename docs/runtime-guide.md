# Runtime guide

Run commands from the repository root unless a block explicitly changes directories. For the short desktop setup, see the [root README](../readme.md#getting-started).

## Setup

Requirements:

- Python 3.13 is the current development target.
- Git must be available for status, diff, review, and repository mutation flows.
- Node.js and npm are required for the Electron workbench or VS Code extension.
- The Codex CLI, or a discovered desktop-bundled Codex executable on Windows or macOS, is required only for `codex-subscription`.

Install Python provider dependencies:

```bash
pip install openai google-genai anthropic pytest
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
.\.venv\Scripts\python.exe -m pip install openai google-genai anthropic pytest
cd desktop
npm install
npm run dev
```

The workbench uses `.venv\Scripts\python.exe` automatically. To use another installation, set `$env:PYTHON_AGENT_PYTHON = 'C:\Path With Spaces\Python\python.exe'` before launching it. Use only the executable path, without arguments or embedded quotes. Without an override or repository virtual environment, Windows lookup tries `python`, `py -3`, then `python3`; macOS/Linux lookup tries `python3`, then `python`. An invalid explicit selection produces a setup error so the workbench does not silently use a different environment.

For a local ChatGPT subscription, select **Codex / ChatGPT subscription** in Runtime settings. If automatic discovery fails, expand **Locate Codex CLI**, browse or paste the native `codex.exe` path, and choose **Save and check**. Use **Sign in with ChatGPT** if needed. After changing the executable for a running agent, stop and start the agent to use it for model calls. See [Codex / ChatGPT subscription runtime](#codex--chatgpt-subscription-runtime) for discovery paths and environment overrides.

Run the focused checks from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\python.exe -m unittest tests.test_codex_subscription tests.test_codex_discovery tests.test_codex_utf8 tests.test_windows_text_encoding
.\.venv\Scripts\python.exe -m unittest tests.test_repository_discovery tests.test_tree_commands tests.test_discovery_remediation
cd desktop
npm run test:runtime
npm run test:git
npm run build
```

File-symlink tests report a skip on Windows when Developer Mode or symlink privileges are unavailable. Directory-junction, excluded-directory, and parent-traversal checks still run without those privileges.

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
