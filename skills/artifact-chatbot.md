---
name: artifact-chatbot
description: Set up, style, or troubleshoot the in-app Skillz chatbot, its generated UI shells, and its Express endpoint in a Workbench artifact.
tags: ["artifact", "chatbot", "react", "preview", "mcp"]
category: artifacts
priority: 50
---

# Artifact chatbot

Use this for the chatbot inside a generated artifact. The Workbench artifact
agent builds the app. The installed chatbot answers its users through the app
server.

## Start with the known entry points

1. Read `.artifact/app.json` and the active React entry point. Check
   `chatbot.enabled`, `chatbot.includeShell`, and `chatbot.shellLayout`.
   Defaults are shell included and floating when the chatbot is enabled.
2. For UI work, inspect the existing mount and the exports of
   `src/skillz-chat/index.tsx` or `src/skillz.ts`. For a server failure, inspect
   `server/index.ts` and `server/agent.ts`. Open more files only to answer a
   specific dependency question.
3. Use **App capabilities** to enable the chatbot and to install missing
   shell files. It can upgrade older artifacts while keeping their app code.
   A hand edit to the settings cannot provision the selected model broker or
   run the installer. Apply capability changes by restarting Preview.

## Mount the shell and fit the layout

Reuse the starter's existing mount when present. In a custom app, import
`{ SkillzChat }` from the generated `src/skillz-chat` module and mount it once.
`<SkillzChat />` uses the configured layout. A custom placement can pass a
`title` and `className`. Style the shell with its `--skillz-chat-*` variables
on that container.

- **Floating** uses a launcher and an expandable panel. For a custom message
  or question surface, register `skillzChat.registerTargets({ message, elicitation })`
  on mount and use the returned function as effect cleanup. The message
  handler receives an entry with `content`. The elicitation handler receives
  an entry with `question` and `choices`, plus `respond(answer)`. Render
  those options or a text field in the custom UI and call the callback once.
  A channel with no target uses the built-in chat.
- **Sidebar** renders broadcasts and questions in its transcript. Put it
  beside the main app in a grid or flex layout with a defined height.
- **Full workspace** fills the width and height of its parent. Give a page
  wrapper `height: 100dvh`, or give a nested container an explicit height
  and `min-height: 0` where needed.

App code can publish an update with `skillzChat.broadcast(text)`. A shell
opt-out keeps the API available. For a requested custom interface, use
`askSkillz(message, { history, approveMutations })` from `src/skillz.ts`,
passing prior user and assistant messages. Mutation approval comes from an
explicit user action for that message. An answer to a question grants no
mutation approval.

## Keep the app server connected

`attachArtifactAgent(app)` registers `POST /_skillz/agent/chat`. In a customized
entry point, attach it after the JSON parser and artifact gateway, before
Vite or the production fallback. Call its returned cleanup on shutdown.
Keep the managed Preview host, dynamic port, and `SKILLZ_ARTIFACT_READY`
handshake. The Python worker runs in Preview and uses the Workbench model
broker. Browser requests use the same-origin chat path.

The shell accepts live NDJSON broadcasts and can read legacy JSON replies.
If Artifact MCP is relevant, check the app's configured server and its
**Allow in Skillz chatbot** switch under **App capabilities**. Use the
artifact-owned MCP runtime for app chat.

## Verify the result

- Use Start Preview to prepare dependencies and get the published URL. Run
  `npm run build` in the artifact after edits. A host browser or Playwright
  MCP uses the published URL when available.
- Open the selected chat layout and send one benign message. Confirm the
  visible reply, the chat request status, and that the shell fits its
  container. When changing target routing, verify one broadcast and one
  question reply through the actual surface.
- A 404 with the chatbot-disabled message points to the capability or
  preview runtime selection. An unhandled 404 or an HTML response from the chat route
  points to a missing handler or fallback ordering. A 502 calls for reading
  the chat response and Preview logs to identify the worker, broker, or tool
  failure. Report the specific missing capability or failed check.
