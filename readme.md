# Skillz Code Agent

Turn a coding request into a reviewable plan, verified repository changes, and a clear record of the work.

[![Prebuilt checks](https://github.com/arberrexhepi/skillz-code-agent/actions/workflows/prebuilt-artifacts.yml/badge.svg?branch=main)](.github/workflows/prebuilt-artifacts.yml)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![Node.js](https://img.shields.io/badge/Node.js-22.12%2B-green)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

[Getting started](#getting-started) · [Usage](#usage) · [Artifacts](#artifacts-and-git-portability) · [Development](#development) · [Documentation](#documentation)

## Overview

Skillz is a planner-first coding agent for developers working in real Git repositories. It clarifies a request, performs bounded discovery, proposes a plan for review, then runs repository actions and validation through its Python host.

The **Electron/React workbench** is the primary desktop interface: an editor, terminal, source control, agent conversation, and issue history in one application. The CLI shares the same backend. A transitional VS Code extension remains available, while ongoing desktop development centers on the standalone workbench.

## Key features

- **Review before execution:** bounded discovery, explicit plan approval, stable and beta/live workers, and recovery from interrupted goals.
- **Choice of model provider:** API providers, local endpoints, and an optional local Codex/ChatGPT subscription runtime.
- **A complete repository workspace:** Monaco editing and diffs, a native terminal, Git controls, clickable file references, diagnostics, issues, and repository facts.
- **Independent artifacts:** React/Express apps with their own Git history, Docker previews and agents, explicit folder grants, and optional chatbots or databases.
- **Reusable service setup:** API Blueprints, a constrained configuration agent, and a Vault that keeps provider keys outside artifact repositories.
- **Portable prebuilt sources:** a publish command that uploads and checks prebuilt histories on the selected remote before publishing the application.

## Getting started

### Prerequisites

| Requirement | When needed |
| --- | --- |
| Python **3.10+**; **3.13** is the documented development target | Python backend and desktop agents |
| Git on `PATH` | Repository operations and prebuilt submodules |
| Node.js **22.12+** and npm | Workbench or extension development |
| Credentials for the chosen provider | API-backed model calls |
| Local Codex CLI and an authenticated session | Only for `codex-subscription` |
| A running **Linux Docker engine** | Artifact previews and artifact agents |

### 1. Clone with the prebuilt sources

```bash
git clone --recurse-submodules https://github.com/arberrexhepi/skillz-code-agent.git
cd skillz-code-agent
```

Already cloned without submodules? Run `npm --prefix desktop run submodules:init` from the repository root.

### 2. Create the Python environment

**macOS / Linux**

```bash
python3 -m venv .venv
.venv/bin/python -m pip install openai google-genai anthropic pytest
cp .env-example .env
```

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install openai google-genai anthropic pytest
Copy-Item .env-example .env
```

Edit `.env` and set the key for the provider you will use: `OPENAI_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, or `META_AI_API_KEY`. The file is ignored by Git. For `codex-subscription`, select that provider in Runtime settings and complete its sign-in instead of configuring an API key.

### 3. Start the workbench

```bash
npm --prefix desktop install
npm --prefix desktop run dev
```

Open a project folder, open **Runtime**, select the provider, model, and backend, then choose **Start agent**. The workbench discovers the repository virtual environment automatically. Interpreter overrides and Windows/macOS troubleshooting are covered in the [runtime guide](docs/runtime-guide.md#setup) and [desktop guide](desktop/README.md#python-resolution).

## Usage

### Workbench

Submit a concrete request, such as “Add a ten-second countdown before the first drill.” Choose the discovery depth if prompted, review the plan, and approve execution. Follow the resulting changes, diagnostics, and validation report; pause or resume through the agent controls.

The beta agent can inspect files with `inspect-file`, read declarations with `read-symbol`, search token usages with `references`, and read merged windows around a literal anchor with `read-context`. Inspection returns file hashes for guarded edits. `edit-symbol` replaces an unambiguous declaration; `edit-batch` applies up to 32 coordinated operations and defaults to rollback on failure. Python symbol edits use AST, and JS/TS/JSX/TSX symbol edits use the installed TypeScript compiler API.

`find-regex <path> {"pattern":"...","replacement":"..."}` returns bounded Python regex matches, capture groups, file hashes and an optional replacement diff without editing. `replace-regex <path> {"pattern":"...","replacement":"...","expected_matches":1,"expected_hash":"..."}` applies an exact expected number of matches (default one) with an optional file-hash guard. JSON preserves multiline patterns and replacement escapes; capture references use `\\1` or `\\g<name>`, and flags are `imsxa`. Each command targets one file; multi-file changes use atomic `edit-batch` operations of type `replace_regex`. Evaluation runs in a text-only subprocess with a two-second deadline and file/output bounds.

Related edits between `batch start` and `batch end` defer automatic diagnostics until completion. Repeated overlapping reads of unchanged source trigger recovery. A paused plan shows its actual stop reason and validation blocker; completed goals remain checkpointed. The compact live trace stays in `memory_observability.md`, while completed attempt reports with full traces are preserved in `.context/observability/` and cleared when the agent session is deleted.

In `edit-batch`, each supplied `expected_hash` refers to the file at batch start, so multiple edits to one file can reuse the same inspected hash. Every declared hash is validated before any edits. Operations still run sequentially against the evolving source, with host guards advanced after internal writes; external edits are rejected and preserved during rollback. Prefer anchors, symbols or descending line ranges when earlier edits shift line numbers.

Multiline shell programs can use a `shell <<<` / `>>>` block, native POSIX heredocs, or a quoted multiline argument such as `shell npx tsx -e '…'`. The body is delivered as one shell command; its lines are never separate agent actions. Unterminated blocks reject the command batch before dispatch. Existing shell permissions, execution timeouts and failure handling still apply. For example:

```text
shell <<<
set -eu
node -e '
  console.log("finite verification");
'
>>>
```

Model results are separate from CLI previews: reads up to 30,000 characters are delivered whole, and larger outputs have explicit continuation guidance (structured results remain valid JSON). Compaction retains complete recent read packets within a 48,000-character budget and rebuilds reread coverage from that retained context. Recovery directs the agent toward the grounded repair and validation, with exact reads available for missing context. Failed checks remain validation failures even when the attempt made no edits; rerunning the same check successfully or explicitly deferring unrelated findings clears that evidence. Attempt traces record delivery size and include the model-visible output when it differs from the full tool result.

Execution also has a delivery checkpoint independent of read freshness: after four turns without an edit attempt the host warns; after eight it permits only two focused reads of at most 120 lines and 6,000 characters each. Three further turns without an edit attempt or completed answer stop with an explicit delivery-stall reason. New ranges, restored context and passing checks do not renew this allowance. Discovery uses its existing planner budget. Compacted prompts and attempt metrics show the exact retained ranges and retention budget so the agent can work on a bounded edit surface instead of repeatedly scanning a large file.

A newly surfaced source diagnostic opens one bounded repair window, even when exploration reads are exhausted. Remediation recommends a small window around the diagnostic line and keeps that source packet through compaction; source already retained satisfies the inspection gate. Repeating the same diagnostic does not renew recovery. Build, diagnose and route-check results treat nonzero exit codes or reported issues as failures in both raw and planner-backed execution. Format repair preserves the model's intended edit and archives the rejected response excerpt for inspection.

Beta goal and phase transitions start a fresh conversation with at most 4,000 characters of outcomes and 32,000 characters of complete retained read packets. Durable facts and full audit history remain available; later compaction only summarizes the current goal's turns. Shell and structured dependency commands record content changes (including already-dirty files, additions and deletions), invalidate cached source, and require verification when files changed. Dependency/generated directories are excluded from this repository change accounting. Explicit `cat`, `read-line-range`, and `ls` can inspect dependency files under `/repo/node_modules/`; broad repository searches still skip dependencies, and workspace containment and `.git` protections still apply.

Provider recovery preserves the current conversation and retained source without triggering compaction. Blank replies receive one retry. Gemini `MALFORMED_FUNCTION_CALL`, `MALFORMED_RESPONSE`, and `UNEXPECTED_TOOL_CALL` share a separate allowance of up to two targeted retries requesting one small text command. The total ceiling is three recovery requests per execution turn. These requests retain usage accounting without spending execution turns or restarting discovery. Gemini uses host text commands with native function calling disabled (`mode=NONE`). Persistent failure pauses the current goal with its provider reason, including when the last retry has no usable text. Traces record response IDs, finish/block reasons, part types, and rejected protocol output while excluding hidden reasoning content.

An illustrative CLI exchange using the same planner workflow:

```text
planner> Add a ten-second countdown before the first drill.
[Skillz discovers the entry flow and presents a proposed plan.]
planner> approve
[Skillz edits the relevant files, validates the change, and reports the outcome.]
```

### CLI

From the repository root, use the virtual environment for planner mode. Replace `/path/to/project` with the repository to work on, and choose a model available to your provider:

```bash
.venv/bin/python main.py --provider openai --model gpt-5.4 --root /path/to/project
```

On Windows, use `.\.venv\Scripts\python.exe` instead of `.venv/bin/python`. Add `--worker-mode` for direct worker execution, or `--confirm-writes --confirm-shell` for per-action confirmation. `main_v2.py` selects the beta TreeLoop backend; `live_test_loop.py` exposes its live entrypoint.

| Command | Purpose |
| --- | --- |
| `/discover` | Show the pending discovery choice |
| `/plan`, `approve`, `reject` | Inspect or decide on the proposed plan |
| `/providers`, `/models` | Inspect the runtime catalog |
| `/start-auto 3 <request>`, `/stop-auto` | Start or stop continuous issue cycles |
| `/create-issue <details>`, `reopen issue-123` | Create or resume durable work |

See [runtime configuration](docs/runtime-guide.md#run), [agent workflow and tools](docs/agent-workflow.md), and the [VS Code guide](vscode-extension/README.md) for the complete interfaces.

## Architecture and tech stack

```mermaid
flowchart TB
    Desktop["Electron / React workbench"] --> Bridge["Validated IPC and Python bridge"]
    Bridge --> Host["Python planner / worker"]
    CLI["CLI / VS Code extension"] --> Host
    Host --> Models["Provider adapters: API, local, subscription"]
    Host --> Tools["Skillz tools: repository actions and validation"]
```

| Layer | Main technologies |
| --- | --- |
| Workbench | Electron, React, TypeScript, Vite, Monaco |
| Host integration | Validated IPC, xterm.js, node-pty, Git |
| Agent runtime | Python, planner/worker protocols, provider adapters |
| Artifact apps | React/Vite/TypeScript, Express, Docker |
| Optional artifact data | Sequelize with SQLite, PostgreSQL, MySQL, MariaDB, or SQL Server |

### Runtime boundaries

- The Python host controls repository actions and validation. Model backends return actions for Skillz to validate; the subscription model process does not directly edit the target repository.
- The renderer has no direct Node, filesystem, or process access. Discovery is read-only; mutations and remote writes follow the host’s authorization rules.
- Artifact agents and previews run in Docker with explicit folder grants. **Allow changes** and **Allow Process Proxy** are separate capabilities; approved Process Proxy scripts run with the user’s host privileges.
- Durable `issue-*` records differ from transient `run-*` validation findings. Architecture facts persist across issues; goal facts return with the active or reopened issue.

Read the [runtime boundaries](desktop/README.md#runtime-boundaries), [artifact access model](desktop/README.md#file-access-and-enforcement), and [issue facts guide](docs/agent-workflow.md#issue-scoped-facts) before extending those surfaces.

## Artifacts and Git portability

Use **Artifacts** to build an app or install Server Manager or Repository issue manager. Choose an artifact library, grant only the folders and scripts it needs, and start its agent or preview. Database, chatbot, Blueprint, Vault, dependency repair, and Docker details are in the [Artifacts guide](desktop/README.md#artifacts).

### Cloning and publishing the prebuilt artifacts

The bundled prebuilts are submodules on `prebuilt/server-manager` and `prebuilt/repo-issue-manager`. Their `./` URLs resolve to the cloned repository; the application records exact source commits. Installed copies in a user’s artifact library have independent repositories and publishing configuration.

After pulling, restore the recorded versions. To publish the application and its required prebuilt histories to a configured remote:

```bash
npm --prefix desktop run submodules:init
npm --prefix desktop run repo:publish -- origin
```

Commit child changes and updated parent pointers before publishing. A plain application `git push` bypasses the publisher. The [full prebuilt guide](docs/prebuilt-artifacts.md) covers independent GitHub copies, detached submodule checkouts, second-machine pulls, and destination verification. The [Prebuilt artifacts workflow](.github/workflows/prebuilt-artifacts.yml) should be a required check on protected application branches.

## Development

Run the checks relevant to the code you changed. From the repository root:

```bash
.venv/bin/python -m pytest -q tests
npm --prefix desktop run typecheck
npm --prefix desktop run test:runtime
npm --prefix desktop run test:git
npm --prefix desktop run test:prebuilts
npm --prefix desktop run build
```

On Windows, replace the Python executable with `.\.venv\Scripts\python.exe`. Artifact Docker/browser integration suites are separate; see the [desktop development guide](desktop/README.md#development). Packaging uses `npm --prefix desktop run package`.

## Roadmap and current limitations

- [x] Planner/worker CLI and standalone Electron workbench.
- [x] Independent artifacts, Docker access boundaries, and prebuilt publishing checks.
- [ ] Bundle a platform-specific Python interpreter and provider dependencies with distributable builds.
- [ ] Complete branded installer icons and macOS signing/notarization.
- [ ] Add Language Server Protocol support as a separate main-process service.

Packaged builds currently include the agent source but still require host Python and provider dependencies. The VS Code extension is transitional. See [distribution notes](desktop/README.md#distribution-notes) for the remaining release work.

## Documentation

| Topic | Guide |
| --- | --- |
| Desktop UI, artifacts, permissions, and packaging | [Workbench](desktop/README.md) |
| Setup, providers, model selection, and executable discovery | [Runtime guide](docs/runtime-guide.md) |
| Discovery, approvals, recovery, tools, and issue facts | [Agent workflow](docs/agent-workflow.md) |
| Clones, remotes, submodule changes, and publishing | [Prebuilt artifacts](docs/prebuilt-artifacts.md) |
| Generated dependency notices and release verification | [Third-party licenses](docs/third-party-licenses.md) |
| Detailed implementation updates and compatibility behavior | [Development notes](docs/development-notes.md) |
| Bundled and workspace-local skills | [Skills](skills/README.md) |
| CLI, repository, and artifact MCP connections | [MCP](docs/mcp.md) |
| Existing VS Code integration | [Extension](vscode-extension/README.md) |

## License and acknowledgments

Licensed under the [Apache License 2.0](LICENSE).

Copyright 2026 arbër inc. See [NOTICE](NOTICE) for the copyright notice.

Desktop releases include generated third-party license notices and verify them during packaging. See the [third-party license guide](docs/third-party-licenses.md) for coverage and verification commands.

Skillz builds on Electron, React, Vite, TypeScript, Monaco, xterm.js, node-pty, Zod, Express, Sequelize, and the supported provider SDKs. See the [desktop dependencies](desktop/package.json) and [artifact template](desktop/artifact-template/package.json) for their declarations.
