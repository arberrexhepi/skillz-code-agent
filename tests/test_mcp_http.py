"""Loopback MCP transport and OAuth integration tests."""
from __future__ import annotations

from base64 import urlsafe_b64encode
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import socket
import threading
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import urlopen

import pytest

from mcp_runtime import McpRuntime
from mcp_settings import McpSettings, read_json, write_json
from skillz_mcp import authorize

pytest.importorskip("mcp")


@pytest.fixture
def http_hub(tmp_path, monkeypatch):
    state = {"token": "first-access-token", "refreshes": 0, "authorization": {}, "resource": "", "challenge": ""}

    class Hub(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, value=None, **headers):
            body = json.dumps(value).encode() if value is not None else b""
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, header in headers.items():
                self.send_header(key.replace("_", "-"), header)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = urlsplit(self.path)
            if path.path == "/authorize":
                query = {key: value[0] for key, value in parse_qs(path.query).items()}
                state["authorization"] = query
                url = query["redirect_uri"] + "?" + urlencode({"code": "skillz-code", "state": query["state"]})
                self.reply(302, Location=url)
            elif path.path.startswith("/.well-known/oauth-protected-resource"):
                self.reply(200, {"resource": self.origin + "/mcp", "authorization_servers": [self.origin],
                                 "scopes_supported": ["hub.read"]})
            elif path.path == "/.well-known/oauth-authorization-server":
                self.reply(200, {"issuer": self.origin, "authorization_endpoint": self.origin + "/authorize",
                                 "token_endpoint": self.origin + "/token", "registration_endpoint": self.origin + "/register",
                                 "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"]})
            else:
                self.reply(405)

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/register":
                self.reply(201, {**json.loads(raw), "client_id": "dynamic-client"})
                return
            if self.path == "/token":
                form = {key: values[0] for key, values in parse_qs(raw.decode()).items()}
                if form["grant_type"] == "authorization_code":
                    state["resource"] = form.get("resource")
                    state["challenge"] = urlsafe_b64encode(sha256(form["code_verifier"].encode()).digest()).decode().rstrip("=")
                    if (form.get("code") != "skillz-code" or state["challenge"] != state["authorization"]["code_challenge"]
                            or form.get("client_id") != "dynamic-client"):
                        self.reply(400, {"error": "invalid_grant"})
                        return
                else:
                    if form.get("refresh_token") != "persistent-refresh-token":
                        self.reply(400, {"error": "invalid_grant"})
                        return
                    state["refreshes"] += 1
                self.reply(200, {"access_token": state["token"], "token_type": "Bearer",
                                 "refresh_token": "persistent-refresh-token", "expires_in": 600})
                return
            if self.path != "/public" and self.headers.get("Authorization") != "Bearer " + state["token"]:
                self.reply(401, WWW_Authenticate='Bearer resource_metadata="' + self.origin
                           + '/.well-known/oauth-protected-resource/mcp", scope="hub.read"')
                return
            request = json.loads(raw)
            method = request["method"]
            if "id" not in request:
                self.reply(202)
                return
            if method == "initialize":
                result = {"protocolVersion": request["params"]["protocolVersion"], "capabilities": {"tools": {}},
                          "serverInfo": {"name": "multi-provider-fixture", "version": "1"}}
            elif method == "tools/list":
                if request.get("params", {}).get("cursor") == "next":
                    result = {"tools": [{"name": "docs_find", "inputSchema": {"type": "object"}}]}
                else:
                    result = {"tools": [{"name": "calendar_find", "inputSchema": {"type": "object"}}], "nextCursor": "next"}
            else:
                result = {"content": [{"type": "text", "text": "Checked " + request["params"]["name"]}]}
            self.reply(200, {"jsonrpc": "2.0", "id": request["id"], "result": result})

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Hub)
    except PermissionError:
        pytest.skip("This sandbox does not permit loopback listeners.")
    Hub.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("SKILLZ_MCP_HOME", str(tmp_path / "profiles"))
    try:
        yield Hub.origin, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_streamable_http_paginates_hub_catalog(http_hub, tmp_path):
    origin, _ = http_hub
    settings = McpSettings(tmp_path, "repo")
    settings.save("hub", {"transport": "http", "url": origin + "/public", "timeout": 5})
    runtime = McpRuntime(settings)
    try:
        result = runtime.invoke({"operation": "search", "server": "hub", "query": "docs"})
        assert [item["name"] for item in result["items"]] == ["docs_find"]
        result = runtime.invoke({"operation": "call", "server": "hub", "name": "docs_find"})
        assert result["result"]["content"][0]["text"] == "Checked docs_find"
    finally:
        runtime.close()


def test_mcp_oauth_browser_pkce_registration_refresh_and_logout(http_hub, tmp_path, monkeypatch):
    origin, state = http_hub
    with socket.socket() as callback:
        callback.bind(("127.0.0.1", 0))
        port = callback.getsockname()[1]

    def browser(url):
        with urlopen(url, timeout=10) as response:
            assert response.read() == b"You can return to Skillz."
        return True

    monkeypatch.setattr("skillz_mcp.webbrowser.open", browser)
    settings = McpSettings(tmp_path, "artifact:demo")
    config = {"transport": "http", "url": origin + "/mcp", "auth": "oauth", "timeout": 5,
              "oauth": {"callbackPort": port, "scope": "hub.read"}}
    settings.save("hub", config)
    runtime = McpRuntime(settings)
    action = {"operation": "list", "server": "hub", "kind": "tools"}
    try:
        assert "Sign in to MCP server hub" in runtime.invoke(action)["error"]
        authorize(settings, "hub")
        assert state["challenge"] == state["authorization"]["code_challenge"]
        assert state["resource"].rstrip("/") == config["url"]
        credential_file = settings.credential_path("artifact:demo", "hub", config)
        assert credential_file.is_file()
        if os.name != "nt":
            assert credential_file.stat().st_mode & 0o077 == 0
        assert runtime.invoke(action)["total"] == 2
        state["token"] = "new-access-token"
        write_json(credential_file, {**read_json(credential_file), "expires_at": 1})
        assert runtime.invoke({"operation": "call", "server": "hub", "name": "docs_find"})["ok"]
        assert state["refreshes"] == 1
        credential_file.unlink()
        assert "Sign in to MCP server hub" in runtime.invoke(action)["error"]
    finally:
        runtime.close()
