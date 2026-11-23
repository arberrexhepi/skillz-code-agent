from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import textwrap

import pytest

from mcp_runtime import McpRuntime, model_mcp_result
from mcp_settings import McpSettings, profile_path, read_json
from skillz_mcp import main
from tree_loop import TreeLoop

pytest.importorskip("mcp")


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLZ_MCP_HOME", str(tmp_path / "user-settings"))
    monkeypatch.delenv("SKILLZ_MCP_SCOPE", raising=False)
    root = tmp_path / "workspace"
    root.mkdir()
    return root


@pytest.fixture
def stdio_server(profiles):
    script = profiles.parent / "server.py"
    script.write_text(textwrap.dedent("""
        from mcp.server.fastmcp import FastMCP

        hub = FastMCP("fixture-hub")

        @hub.tool(name="calendar_find")
        def calendar(word: str) -> str:
            return f"calendar found {word}"

        @hub.tool(name="documents_find")
        def documents(word: str) -> str:
            return f"documents found {word}"

        @hub.resource("brief://summary")
        def brief() -> str:
            return "Keep the heading."

        @hub.resource("brief://fixtures/{slug}")
        def fixture(slug: str) -> str:
            return f"Fixture {slug}"

        @hub.prompt(name="review_title")
        def review(title: str) -> str:
            return f"Review title {title}"

        hub.run(transport="stdio")
    """), encoding="utf-8")
    return {"transport": "stdio", "command": sys.executable, "args": [str(script)], "timeout": 5}


def test_cli_installs_scoped_hub_and_lists_tools_without_launching_other_servers(profiles, stdio_server, capsys):
    args = ["--root", str(profiles), "--scope", "repo"]
    assert main([*args, "add", "hub", json.dumps(stdio_server)]) == 0
    assert main([*args, "request", '{"operation":"list","server":"hub","kind":"tools","limit":1}']) == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert result["total"] == 2 and result["next_offset"] == 1
    assert result["items"][0]["name"] == "calendar_find"
    assert "hub" not in McpSettings(profiles, "artifact:a").servers()
    assert "hub" not in McpSettings(profiles.parent, "repo").servers()
    assert "hub" in McpSettings(profiles, "local").servers()
    assert "playwright" not in read_json(profile_path(profiles, "repo"))["servers"]
    if os.name != "nt":
        assert profile_path(profiles, "repo").stat().st_mode & 0o077 == 0


def test_playwright_defaults_and_opt_outs_are_scoped(profiles, capsys):
    def enabled(scope):
        return McpSettings(profiles, scope).servers()["playwright"][1]["enabled"]

    assert enabled("cli") and enabled("repo") and enabled("local")
    assert not enabled("artifact:a") and not enabled("artifact:b")
    assert main(["--root", str(profiles), "disable", "playwright"]) == 0
    assert not enabled("cli") and not enabled("local")
    assert enabled("repo")
    assert main(["--root", str(profiles), "--scope", "artifact:a", "enable", "playwright"]) == 0
    assert enabled("artifact:a") and not enabled("artifact:b")
    assert main(["--root", str(profiles), "--scope", "artifact:a", "remove", "playwright"]) == 0
    assert not enabled("artifact:a")


def test_hub_routes_original_names_and_exposes_resources_templates_prompts(profiles, stdio_server):
    settings = McpSettings(profiles, "artifact:a")
    settings.save("hub", {**stdio_server, "allowedTools": ["documents_*"]})
    runtime = McpRuntime(settings)

    def invoke(operation, **fields):
        return runtime.invoke({"operation": operation, "server": "hub", **fields})

    try:
        assert invoke("search", query="documents")["items"][0]["name"] == "documents_find"
        assert invoke("describe", name="documents_find")["item"]["inputSchema"]["required"] == ["word"]
        assert not invoke("call", name="calendar_find", arguments={"word": "test"})["ok"]
        assert invoke("call", name="documents_find", arguments={"word": "test"})["result"]["content"][0]["text"] == "documents found test"
        assert invoke("list", kind="resources")["items"][0]["uri"] == "brief://summary"
        assert invoke("list", kind="templates")["items"][0]["uriTemplate"] == "brief://fixtures/{slug}"
        assert invoke("read-resource", uri="brief://fixtures/alpha")["result"]["contents"][0]["text"] == "Fixture alpha"
        assert invoke("list", kind="prompts")["items"][0]["name"] == "review_title"
        assert invoke("get-prompt", name="review_title", arguments={"title": "Plan"})["result"]["messages"][0]["content"]["text"] == "Review title Plan"
        other = McpRuntime(McpSettings(profiles, "artifact:b"))
        try:
            assert not other.invoke({"operation": "call", "server": "hub", "name": "documents_find"})["ok"]
        finally:
            other.close()
        settings.save("hub", {**stdio_server, "enabled": False})
        assert not invoke("call", name="documents_find", arguments={"word": "test"})["ok"]
        assert runtime.invoke({"operation": "servers"})["servers"] == []
    finally:
        runtime.close()


def test_tree_worker_calls_mcp_and_sees_result(profiles, stdio_server):
    McpSettings(profiles, "repo").save("hub", stdio_server)
    calls = []

    class Model:
        def complete(self, system, prompt):
            calls.append((system, prompt))
            if len(calls) == 1:
                return 'mcp {"operation":"read-resource","server":"hub","uri":"brief://summary"}'
            return "finish Done."

    loop = TreeLoop(model=Model(), workspace_root=profiles, max_turns=2, verbose=False)
    assert loop.run("Read the configured MCP brief.").finished
    assert "MCP is available" in calls[0][0]
    assert "Keep the heading." in calls[1][1]


def test_model_result_only_filters_protocol_binary_blocks():
    data = {"ok": True, "result": {"structuredContent": {"data": "useful value"},
            "content": [{"type": "image", "data": "YQ==", "mimeType": "image/png"}]}}
    result = model_mcp_result(data)["result"]
    assert result["structuredContent"]["data"] == "useful value"
    assert result["content"][0]["data"] != "YQ=="


@pytest.mark.parametrize("url", ["http://remote.example/mcp", "https://test:secret@example.com/mcp", "file:///tmp/mcp"])
def test_disallows_insecure_transport_endpoints(profiles, url):
    with pytest.raises(ValueError, match="HTTPS"):
        McpSettings(profiles, "repo").save("bad", {"transport": "http", "url": url})
