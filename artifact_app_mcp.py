"""Container-owned MCP client and Streamable HTTP server for an artifact app."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import socket
import sys
import tempfile
from typing import Any
from urllib.parse import unquote, urlsplit

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from mcp_runtime import KINDS, McpRuntime, model_mcp_result
from mcp_settings import McpSettings, read_json, validate_server

TOOL_ORIGIN_ENV = "SKILLZ_ARTIFACT_TOOL_ORIGIN"


class AppMcpSettings(McpSettings):
    def __init__(self, root: Path):
        super().__init__(root, "artifact:app")
        config = read_json(root / ".artifact/app.json").get("mcp", {})
        entries = config.get("servers", [])
        if config.get("enabled") is not True or not isinstance(entries, list) or len(entries) > 24:
            raise ValueError("Artifact MCP is disabled or its server list is invalid.")
        self.configured = {}
        self.chatbot_servers = set()
        for entry in entries:
            server = dict(entry)
            name = server.pop("id")
            if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name) or name in self.configured:
                raise ValueError("MCP server names must be unique artifact IDs.")
            if server.pop("chatbot", False) is True:
                self.chatbot_servers.add(name)
            local_source = server.pop("source", None) == "artifact"
            if local_source:
                location = root / "mcp" / name / "server.py"
                if server["transport"] != "stdio" or (server.get("enabled", True) and (location.resolve() != location or not location.is_file())):
                    raise ValueError(f"Artifact MCP server {name} requires a regular file at mcp/{name}/server.py.")
                server.update(command=sys.executable, args=[str(location)])
            if server["transport"] == "http":
                host = (urlsplit(server["url"]).hostname or "").lower().rstrip(".")
                loopback = host in {"localhost", "127.0.0.1", "::1"}
                if not loopback and (host in {"host.docker.internal", "gateway.docker.internal", "host.containers.internal"}
                                     or re.fullmatch(r"[\d.]+", host) or ":" in host):
                    raise ValueError("MCP endpoints must use container loopback or an HTTPS hostname.")
                server["auth"] = "bearer" if server.get("bearerEnv") else "none"
            variables = [*server.get("env", []), server.get("bearerEnv", "")]
            if any(name.upper().startswith("SKILLZ_") or name.upper() in {"PATH", "HOME"} for name in variables):
                raise ValueError("MCP servers may only reference application environment variables.")
            server = validate_server(name, server)
            if local_source:
                server["env"] = [*server.get("env", []), TOOL_ORIGIN_ENV]
            self.configured[name] = (self.scope, server)

    def servers(self):
        # The app owns its profile. Reading Workbench settings, including its
        # implicit default servers and credential cache, would cross this boundary.
        return self.configured


class AppMcp:
    def __init__(self, settings: AppMcpSettings):
        self.settings = settings
        self.client = McpRuntime(settings)
        self.capacity = asyncio.Semaphore(8)

    def _invoke(self, action: dict, *, chatbot: bool, approved: bool) -> dict:
        name = action.get("server")
        operation = action.get("operation")
        if operation == "servers":
            return {"ok": True, "servers": [
                {"name": key, "transport": config["transport"]}
                for key, (_, config) in self.settings.servers().items()
                if config.get("enabled", True) and (not chatbot or key in self.settings.chatbot_servers)
            ]}
        if not isinstance(name, str) or (chatbot and name not in self.settings.chatbot_servers):
            return {"ok": False, "error": "MCP server is unavailable for this caller."}
        if operation == "call" and not approved:
            description = self.client.invoke({"operation": "describe", "server": name, "kind": "tools", "name": action.get("name")})
            if not description.get("ok"):
                return description
            if (description.get("item", {}).get("annotations") or {}).get("readOnlyHint") is not True:
                return {"ok": False, "error": "This MCP tool requires explicit mutation approval."}
        return self.client.invoke(action)

    async def invoke(self, action: dict, *, chatbot: bool = False, approved: bool = False) -> dict:
        if not isinstance(action, dict):
            return {"ok": False, "error": "An MCP request object is required."}
        async with self.capacity:
            result = await asyncio.to_thread(self._invoke, action, chatbot=chatbot, approved=approved)
        if chatbot:
            result = model_mcp_result(result)
        if len(json.dumps(result, ensure_ascii=False).encode()) > 1_000_000:
            return {"ok": False, "error": "MCP response exceeds 1 MB. Narrow the request."}
        return result


def make_server(backend: AppMcp) -> FastMCP:
    server = FastMCP("Skillz Artifact MCP", stateless_http=True, json_response=True,
                     instructions="Use mcp_request to discover and invoke this artifact's configured MCP tools and resources.")

    @server.tool()
    async def mcp_request(action: dict[str, Any], approve_mutations: bool = False) -> dict[str, Any]:
        """List servers or use list, search, describe, call, read-resource, or get-prompt on a configured server."""
        return await backend.invoke(action, approved=approve_mutations)

    @server.resource("artifact-mcp://servers", mime_type="application/json")
    async def servers() -> str:
        return json.dumps(await backend.invoke({"operation": "servers"}))

    @server.resource("artifact-mcp://catalog/{source}/{kind}/{offset}", mime_type="application/json")
    async def catalog(source: str, kind: str, offset: int) -> str:
        if kind not in KINDS:
            raise ValueError("Unknown MCP catalog category.")
        return json.dumps(await backend.invoke({"operation": "list", "server": source, "kind": kind, "offset": offset}))

    @server.resource("artifact-mcp://resource/{source}/{uri}", mime_type="application/json")
    async def resource(source: str, uri: str) -> str:
        """Read an upstream resource by percent-encoding its URI as one path segment."""
        return json.dumps(await backend.invoke({"operation": "read-resource", "server": source, "uri": unquote(uri)}))

    @server.custom_route("/request", methods=["POST"])
    async def request(request: Request) -> JSONResponse:
        try:
            body = await request.json()
            result = await backend.invoke(body["action"], chatbot=body.get("actor") == "chatbot",
                                          approved=body.get("approveMutations") is True)
        except (TypeError, ValueError, KeyError, AttributeError):
            result = {"ok": False, "error": "Invalid MCP request."}
        return JSONResponse(result)

    return server


class PreviewServer(uvicorn.Server):
    async def startup(self, sockets=None):
        await super().startup(sockets=sockets)
        if self.started:
            print("SKILLZ_APP_MCP_READY " + json.dumps({"port": sockets[0].getsockname()[1]}), flush=True)


def main() -> None:
    root = Path(sys.argv[1]).resolve()
    settings = AppMcpSettings(root)
    app_port = os.environ.get("SKILLZ_ARTIFACT_PORT", "")
    with tempfile.TemporaryDirectory(prefix="skillz-app-mcp-") as home, socket.socket() as listener:
        # Executables run in this container, with an artifact-local home and no
        # host Process Proxy or desktop broker variables in their environment.
        for key in list(os.environ):
            if key.startswith("SKILLZ_"):
                del os.environ[key]
        if app_port.isdigit() and 0 < int(app_port) < 65536:
            os.environ[TOOL_ORIGIN_ENV] = f"http://127.0.0.1:{app_port}"
        os.environ["HOME"] = home
        paths = [entry for entry in os.defpath.split(os.pathsep) + os.environ.get("PATH", "").split(os.pathsep)
                 if entry and entry != "/opt/skillz/bin"]
        os.environ["PATH"] = os.pathsep.join([str(root / "node_modules/.bin"), str(Path(sys.executable).parent), *dict.fromkeys(paths)])
        listener.bind(("127.0.0.1", 0))
        backend = AppMcp(settings)
        try:
            app = make_server(backend).streamable_http_app()
            PreviewServer(uvicorn.Config(app, port=0, access_log=False, log_level="warning")).run(sockets=[listener])
        finally:
            backend.client.close()


if __name__ == "__main__":
    main()
