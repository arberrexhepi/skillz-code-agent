# Agent Observatory

A maintained interactive showcase for the Skillz beta TreeLoop. Thirteen atomic components explain their inputs, outputs, behavioral contracts, implementation locations, and connections. The system map follows a real run or a saved trace. The included sample shows a failed patch and recovery and is always labeled illustrative.

## Use in Workbench

1. Open **Artifacts → New artifact → Install Agent Observatory** in an updated desktop build.
2. Keep **Allow agent runtime** enabled to run tasks from the showcase. Choose your provider and model. The run bridge always uses the beta `main_v2.py` backend.
3. Optionally share source folders. They start read only. Enable changes only for folders the requested work should modify.
4. Start Preview. If the runtime needs setup, choose **Configure runtime** to open this artifact’s **Setup → Agent runtime** controls. Codex uses automatic host discovery and ChatGPT sign-in; you can also browse/paste an executable. Saving a changed provider/model restarts the preview and stops an active recording. New runs require local provider readiness; recordings remain replayable while setup is incomplete. Under **Put the loop in motion**, choose a **Working repository** from the folders granted in **File Access** (including the Workbench repository when granted), or keep **Observatory artifact**. Each run stays pinned to that choice until it finishes or is stopped. Read-only grants allow inspection; write permission is needed for edits. Changing File Access stops the preview/runtime; restart preview to refresh the choices. Enter a task. Review any discovery choice or plan inside the showcase and explicitly continue it.
5. Open a completed, failed, or interrupted run from **Run archive**. Play/pause, step backward or forward, scrub the event slider, select 0.25×–8× speed, or disable **Skip idle gaps** for original event timing. Export saves a JSON trace.

Live playback follows the newest event automatically, including discovery choices and plan approvals. Full decision controls appear above the system map and in the run console, sharing their reply draft and submission state. Scrubbing or stepping pauses follow mode; **Return to live** resumes it and follows the current event in the inspector. The event strip scrolls horizontally as events arrive. Archive playback starts at the first event and never launches execution.

The latest 20 runs are persisted by the desktop under its artifact setup directory in `run-history/<artifact-id>`. Each trace is capped at 6,000 events; reaching the limit stops the run with a visible partial-recording error. Strings and large collections are bounded for storage. A preview reload reconnects to the active recording. Closing the preview runtime or revoking permissions stops its agent; unfinished recordings become interrupted and remain replayable.

## Runtime boundary

The artifact server receives a short-lived `SKILLZ_RUNTIME_URL` and `SKILLZ_RUNTIME_TOKEN` only when the desktop-owned runtime capability is enabled and a runtime selection is saved. Its same-origin `/runtime/v1/*` gateway exposes only status, run retrieval, submit, explicit planner continuation, stop, and a scoped request to open its Workbench Setup tab. It never returns the token to the browser. Calls cannot target another artifact or launch a host shell.

This recording bridge coexists with the updated app chatbot model broker (`SKILLZ_MODEL_BROKER_URL`); chatbot calls do not create beta recordings or share the recorded planner session. The current API Blueprint, command-discovery, optional chatbot, and database gateway modules are preserved.

A dedicated `AgentService` / `ArtifactAgentExecution` session uses the existing Docker sandbox and model-only provider broker. It is separate from the artifact editing conversation; its `/repo` is the selected working repository. A selected shared repository is mounted with its existing read/write grant; other granted repositories and the Observatory source are not mounted in that recording worker. The preview server and artifact editing agent retain their separate artifact mounts. Git operations target the selected repository (including Git worktrees), with the same access permission. The repository identity and access are saved with each recording; legacy traces default to the original Observatory artifact scope. Read-only runs keep worker observability in the container's temporary directory and cannot persist repo facts back to the source. The artifact cannot expand these grants. Credentials stay in the desktop provider broker. A server started outside Workbench has no runtime capability and offers documentation and sample replay.

## Trace semantics

- Model start/finish/error/interruption, discovery, goals, and tool outcomes come from the beta bridge. `diagnose`, `run_check`, `run_route_check`, and transient run-diagnostic actions map to verification.
- Tool progress is emitted after execution. Context preparation, grammar, guard participation, and evidence updates are inferred where the bridge has no distinct event. Dashed node borders and the inspector identify inferred phases; no synthetic timing is presented as measured timing.
- Timestamps record desktop receipt relative to run creation, including user decision time. Idle-gap skipping only affects playback.
- Worker `finish` and individual `goal_finish` events do not mark the entire run complete. The current worker accepts source reads, diff reviews, or checks as verification; verification does not promise a test suite ran.
- A response with `ok: true` is not enough to declare completion. The planner must report `status: completed`. Paused decisions remain awaiting input.
- Playback uses the local event array only and never resubmits a prompt or action. Raw thought fields are excluded from the recording.

## Development

React, TypeScript, Vite, Express. Preserve the existing file and API gateways and dynamic `SKILLZ_ARTIFACT_READY` protocol.

```sh
npm ci
npm run build
npm test
```

Launch through Workbench for container isolation and provider access. The host service and bridge tests are `npm run test:artifacts:runtime` in `desktop`. Run `npm run test:artifacts:runtime:integration` there with Docker Desktop running to check an installed artifact, both model paths, explicit approval, read-only grants, history replay, and permission revocation using a deterministic fixture backend without paid provider calls. Frontend modules separate the catalog, trace projection, polling, playback, and UI components. Styling uses system fonts, restrained mint accents, responsive layouts, keyboard controls, and reduced-motion support.
