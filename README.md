# Server Manager

A maintained Skillz artifact for discovering approved repository scripts, launching them through Process Proxy, tracking lifecycle output, and opening detected local ports.

React + Vite + TypeScript with an Express server and configured HTTP/WebSocket gateways.

Run this artifact through the desktop **Artifacts** panel. The preview and agent execute in Docker, with `/repo` writable, approved folders read-only at `/reads/<id>`, and selected facts/memory read-only at `/context`. The desktop supplies dynamic host/port settings and publishes the preview on host loopback. Start Docker Desktop with Linux containers first.

- Edit `src/App.tsx` to implement the requested artifact. Run `npm run build` in the artifact agent to typecheck and build. Dependencies are kept in a Docker volume per artifact.
- Use `src/files.ts` to list shared folders and read files: `files.roots()`, `files.list(id, path)`, `files.readText(id, path)`, and `files.read(id, path)`. Paths are relative to a granted folder; the `repo` ID reads this artifact. All file endpoints accept GET only. Reads are limited to 5 MB and directory listings to 500 entries.
- Configure grants in **File access** in the desktop. Saving stops the preview and agent; restart them to apply new mounts. Repository files cannot grant themselves access. Native host paths are never required in artifact code.
- Configure named API connections in `.artifact/apis.json` or **API connections**. Use `/api/<id>` for HTTP and `/ws/<id>` for WebSocket. JSON Schema validates request and response data. Use `host.docker.internal` for services running on the host.
- `headerEnv` maps upstream header names to environment variable names, e.g. `{"Authorization":"SCHEMA_API_AUTH"}`. Set values before starting the desktop; restart the preview after changes. Keep credentials out of frontend configuration.
- Read shared source context via `/context/repo-facts` and `/context/memory`. The desktop refreshes selected snapshots automatically; they are separate from this artifact's own agent memory.
- Commit changes inside this child repository, then update the parent's submodule pointer. Configure remotes before publishing; nothing is pushed automatically.

Docker enforces host filesystem grants even for direct backend filesystem calls and child processes. Model calls use the desktop's local provider configuration through a model-only bridge. Running this project's scripts directly on the host bypasses that container boundary; use the workbench to run with its permissions.
