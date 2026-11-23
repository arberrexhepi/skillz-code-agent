---
name: artifact-mcp-authoring
description: Author or repair custom artifact-local MCP tools, classify their side effects, and verify them in Preview and the Skillz chatbot.
tags: ["artifact", "mcp", "tools", "fastmcp", "chatbot"]
category: artifacts
priority: 50
---

# Author MCP tools inside an artifact

## Locate and edit the server

1. Read `.artifact/app.json` and the relevant `mcp/<server-id>/server.py`.
   **App Capabilities → Artifact MCP → Create MCP server** stages a new
   artifact-local server. **Save capabilities** writes the starter and registers
   it with `transport: "stdio"` and `source: "artifact"`. Preserve existing
   server code when the file already exists.
2. Edit the generated FastMCP server in place. The preview supplies its Python
   environment and MCP dependency. Add a function to the existing `server`
   and keep `server.run(transport="stdio")` at the entry point. Write
   diagnostics to stderr so stdout stays available to the MCP protocol.
3. Read the app's actual state from its persisted data or an existing app
   route. The tool process cannot read the browser's React memory. When
   calling an app-owned data route from a tool, use
   `os.environ["SKILLZ_ARTIFACT_TOOL_ORIGIN"]` as the preview's in-container
   origin. Use a fixed route path and a bounded timeout. For state that
   lives in the browser, move or synchronize the minimum required data to
   the app server before claiming the tool can see it.

## Define the tool contract

- Give each tool a task-specific name, typed parameters, a concise docstring,
  and a bounded result. Validate input and use the app's existing rules.
- Set `ToolAnnotations(readOnlyHint=True)` only if the entire operation
  leaves app and external state unchanged. Use `readOnlyHint=False` for a
  write and `destructiveHint=True` for deletion or replacement. The app
  runner requires explicit mutation approval unless `readOnlyHint` is true.
- Keep mutations behind the existing approval path. Every chatbot question
  and every tool description is separate from the user's approval.
- The starter includes `get_note` and `save_note`. Use them to check wiring
  when helpful, then replace them with the requested app capabilities.

## Verify in Preview

Start Preview. If it was already running when the registration or tool source
changed, restart it once to reload the executable and capability catalog.
After edits, run `npm run build` inside the artifact.

The artifact agent can run these Beta TreeLoop commands during goal execution.
Replace the example IDs and names with the ones you authored.

```text
artifact-preview restart
artifact-preview mcp {"operation":"list","server":"game-tools","kind":"tools"}
artifact-preview mcp {"operation":"describe","server":"game-tools","name":"get_board_state"}
artifact-preview mcp {"operation":"call","server":"game-tools","name":"get_board_state","arguments":{}}
artifact-preview chat {"message":"Call game-tools/get_board_state and report the board state you observed."}
```

Stable uses `{"type":"artifact_preview","operation":"mcp","input":{...}}`
or `operation:"chat"` with the same input object. Restart uses
`{"type":"artifact_preview","operation":"restart"}`.

The Preview actions call the same app routes as the manual runner and chatbot,
with mutation approval disabled. Verify a read returns the expected data and
a mutation attempt asks for approval without changing state. Exercise an
authorized mutation on disposable data using **Approve changes for this tool
call** in the manual runner or the chatbot's user approval control.

For chatbot verification, enable **Skillz chatbot** and **Allow in Skillz
chatbot** for this server, then restart Preview. Ask it to use the exact tool
and confirm its `mcpCalls` receipt shows that tool with `ok: true`. Compare
the chatbot's answer with the data from the manual call. Report separately
when the app is reachable, manual calls succeed, or chatbot verification is
blocked by a disabled capability or model setup error.
