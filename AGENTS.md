# Maintained Server Manager artifact

Build the user's requested artifact in this repository using React, Vite, TypeScript, and Express. Read artifact.json for the original request. The existing starter is only a scaffold: implement the actual requested experience and verify it.

This artifact is distributed as the `server-manager` prebuilt. Preserve its primary server-launch action, secondary package-script overflow, visible process failures, and dynamic repository inventory. Only show scripts allowed by each grant's Process Proxy allowlist. Prefer `dev`, then `start`, `serve`, and `preview`; label preview as a production-build preview.

Keep the runtime protocol intact: server/index.ts prints SKILLZ_ARTIFACT_READY followed by a JSON object with its dynamically assigned URL. Use SKILLZ_ARTIFACT_HOST and SKILLZ_ARTIFACT_PORT from the desktop (default 127.0.0.1 and port 0 outside Docker) and let Vite middleware/HMR use that server. Do not hardcode ports or remove the configured gateway.

Add or modify API definitions in .artifact/apis.json. Each has a unique id, transport (http or websocket), title, url, HTTP method, requestSchema, responseSchema, and headerEnv. Frontend HTTP calls use /api/<id>, WebSocket calls use /ws/<id>. No credentials in frontend code or tracked files. Validate API shapes and keep response errors visible.

Optional source context is mounted read-only at /context; never expose other source-workspace files. This artifact's own repo_facts.md and memory_observability.md are separate. Do not change or commit the parent library or other artifacts. Run npm run build after edits; use the desktop Preview to view the result.

## File access

The desktop runs this repository in a Docker container. `/repo` is the writable artifact repository. `/context` contains the selected, read-only source context snapshots. `SKILLZ_READ_ROOTS` lists additional approved folders and their access mode at `/reads/<id>`, including `/reads/workspace` only when the user enables workbench repository reads. `SKILLZ_WRITE_ROOTS` lists the explicit write grants. Folders remain read only unless the user enables Allow changes. No other host directories are available. Directory grants belong to desktop settings; never try to change them from generated code.

Use `list_files` and `read_file` with `/reads/<id>/relative/path` to inspect granted files. The browser client `src/files.ts` exposes `files.roots()`, `files.list(id, path)`, `files.readText(id, path)`, `files.read(id, path)`, and `files.writeText(id, path, content, expectedSha256)`. Write only when a root reports `access: 'write'`; the SHA-256 precondition prevents overwriting a concurrent change. The artifact's own `repo` root stays read only through this gateway. Use folder IDs and relative paths in the UI, never native host paths.

Preserve the file gateway and the dynamic port/host environment settings in `server/index.ts`. Docker mounts enforce read-only grants even when backend code uses `node:fs`, spawns a subprocess, or runs build scripts. Use `npm run build` inside the agent session. Container dependencies live in a separate Docker volume; native Windows/macOS node_modules are not used. Reach services on the host through `host.docker.internal`, not container localhost.
