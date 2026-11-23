# MCP connections in Skillz

Skillz can discover and use tools, resources, resource templates, and prompts
from MCP servers. Connections support local stdio processes and Streamable HTTP
endpoints. A hub can expose tools for several providers. Skillz addresses each
one by the configured server name and the original tool name.

## Playwright

The `playwright` server is enabled by default in the CLI and repository
workspaces. It starts on first use with a headless browser and an isolated
profile. It uses the pinned Microsoft Playwright MCP package through `npx`.
Install Node.js and the MCP runtime on the host. The first launch may download
the server package. If Chrome is unavailable, the server provides the
`browser_install` tool.

When creating or installing a prebuilt artifact, select **Enable Playwright
MCP for this artifact** to opt in. You can also enable or disable it later
from the artifact’s **MCP** tab. The CLI can disable the default with
`python main.py mcp disable playwright`, or manage an artifact by using
`--scope artifact:<id>`.

## Workbench

Open **MCP** in the repository sidebar or in an individual artifact’s tabs.
Choose **Install MCP runtime** if prompted, then **Add server**.

- **Streamable HTTP** connects to an MCP URL. Enter the endpoint and set its
  authentication mode in Options.
- **Executable** launches a local MCP server or bridge on the host over
  stdio. Enter its absolute path or command name and put each argument in the
  `args` array. For bridges such as Blender Lab MCP, start the companion app
  and add-on, then enter the bridge executable.

Use **Capabilities** to browse and search the server’s tools, resources,
templates, and prompts. Enable or disable each connection in the current
workspace. Repository profiles are tied to the repository path. Artifacts
have their own profiles and connect through the desktop host. Their server
configuration and OAuth tokens stay on the host.

## Artifact app runtime

Under **App capabilities**, enable **Artifact MCP**, add the app's MCP
connections, save, and start or restart the preview. Each app runs its own
MCP backend and uses the servers configured in `.artifact/app.json`.
Workbench’s separate **MCP** tab configures the development agent.

Choose **Executable** for a program available inside the artifact, such
as a Node dependency installed by its `package.json` or a script in the
artifact repository. **Streamable HTTP** accepts an HTTPS service or HTTP
loopback inside the container. Use a named environment variable for
bearer credentials. Workbench passes only the configured names from the
host environment to the preview.

Use **Run MCP tools and read resources** in App capabilities or the app’s
`artifactMcp()` function in `src/mcp.ts`. MCP clients can connect to
`<preview-url>/_skillz/mcp` and use the `mcp_request` tool or the
`artifact-mcp://` resources. For a resource template, percent-encode the
upstream resource URI as one path segment.

To use a server from the installed **Skillz chatbot**, enable chatbot access
for that server and restart the preview. Tools without a read-only MCP
annotation require explicit approval. The artifact’s chatbot keeps the
usual `approveMutations` option.

### Create an artifact-local server

Select **Create MCP server**, enter a server ID, and save capabilities. This
installs `mcp/<server-id>/server.py` with a read-only note tool and an
approval-gated write tool. Edit the file to provide app-specific tools. The
preview supplies Python and the MCP SDK. Saving again preserves your edits.
For an app-owned HTTP data route, the tool process can use
`SKILLZ_ARTIFACT_TOOL_ORIGIN` to reach the app inside the container.

The artifact agent loads [artifact-mcp-authoring](../skills/artifact-mcp-authoring.md)
and can test the manual runner and chatbot through `artifact-preview mcp` and
`artifact-preview chat`. Both verification actions keep mutation approval
off. Use the manual runner's approval control to exercise a write explicitly.

## CLI

Install the optional Python dependency into the Skillz interpreter.

```sh
python -m pip install -r requirements-mcp.txt
python main.py mcp add calendar '{"transport":"http","url":"https://calendar.example/mcp","auth":"oauth"}'
python main.py mcp login calendar
python main.py mcp list
```

Both the stable and beta CLI accept the `mcp` subcommand. The CLI commands
default to the CLI profile. Set `--root` and `--scope repo` to manage the same
repository profile as Workbench, or use `--scope artifact:<id>` with the
artifact’s root. A standalone agent uses the CLI profile together with its
repository profile. On a name collision the repository entry takes precedence.
Workbench agents use their selected workspace profile.

```sh
python main.py mcp --root /path/to/repo --scope repo add hub \
  '{"transport":"stdio","command":"uvx","args":["my-mcp-hub"],"env":["HUB_API_KEY"],"allowedTools":["calendar_*","docs_find"]}'
python main.py mcp --root /path/to/repo --scope repo request \
  '{"operation":"search","server":"hub","kind":"tools","query":"calendar","limit":10}'
```

An `allowedTools` array limits tool discovery and calls using exact names or
glob patterns. Omit it to enable all tools on the configured server. An empty
array enables zero tools and leaves resources and prompts available. CLI
commands also include `enable`, `disable`, `remove`, and `logout`. For a
config supplied on stdin, pass `-` as the server settings.

## Authentication

Set `"auth":"oauth"` for OAuth. Choose **Sign in** in Workbench or run
`mcp login <name>` to open a browser. Skillz handles the loopback callback and
can refresh expiring tokens. The server can advertise dynamic client
registration or client metadata documents. If a provider requires a
registered client, set `oauth.clientId` and register
`http://127.0.0.1:8765/callback` with the provider. Set
`oauth.callbackPort` to use a different port and register that exact URI.
Use `oauth.clientMetadataUrl` for a provider that supports a hosted client
metadata document.
Set `oauth.scope` to request specific scopes. For a preregistered confidential
client, `oauth.clientSecretEnv` names the host variable with the secret and
`oauth.tokenEndpointAuthMethod` selects its token endpoint authentication.

For a static bearer credential, use `"auth":"bearer","bearerEnv":"HUB_TOKEN"`.
For stdio, `env` names the host variables to pass to the executable. Keep
credentials in those variables or the OAuth flow. User-owned settings and
OAuth state are stored under `~/.skillz/mcp` by default. Set
`SKILLZ_MCP_HOME` to use a different private directory. Sign out to remove a
saved OAuth session from this profile.

## Using capabilities in an agent task

The worker can list enabled servers, search or page through capabilities, and
fetch an exact tool schema when it needs it. It can call tools during goal
execution and read resources or retrieve prompts on demand. You can mention
a server, tool name, resource URI, or prompt by name in a task.

An execution goal can complete with a successful MCP operation and an
observed outcome. In an artifact, use `artifact-preview start` in the beta
worker or `{"type":"artifact_preview"}` in the stable worker. This starts or
reuses the Workbench preview and returns its published host URL. When
Playwright MCP is enabled for the artifact, the starter request includes
preview guidance and the browser can open that URL directly.

Run declared workspace scripts with `shell npm run <script>` in the beta
worker or the `run_shell` action in the stable worker. Server and watch
scripts return a process ID so later tools can run while the server stays up.
Use `shell status <id>` to read logs and `shell stop <id>` to stop it.
Use `--background npm run <script>` for a custom long-running script.

The CLI `mcp request` command returns protocol payloads as JSON. Worker outputs
are bounded for model context. Binary payloads remain available through the
CLI, while the worker sees their metadata.
