"""Configure and use MCP in a local Skillz profile."""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import webbrowser

from mcp_runtime import McpRuntime, _error, model_mcp_result, open_session
from mcp_settings import McpSettings, is_builtin_playwright


def json_object(text: str) -> dict:
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return value


def authorize(settings, name):
    source, server = settings.servers().get(name, (None, {}))
    if server.get("auth") != "oauth":
        raise ValueError("Select an MCP server configured for OAuth.")

    def open_browser(url):
        if not webbrowser.open(url):
            print(f"Open this sign-in URL in a browser:\n{url}", file=sys.stderr, flush=True)

    async def login():
        async with open_session(settings, source, name, server, open_browser=open_browser):
            pass

    asyncio.run(login())


def profile_status(settings):
    servers = {}
    for name, (scope, server) in settings.servers().items():
        status = None
        if server.get("auth") == "oauth":
            status = "signed-in" if settings.credential_path(scope, name, server).is_file() and (
                bool(_stored_tokens(settings, scope, name, server))) else "sign-in-required"
        servers[name] = {**server, **({"authStatus": status} if status else {}),
                         **({"builtIn": True} if is_builtin_playwright(name, server) else {})}
    return {"ok": True, "servers": servers}


def _stored_tokens(settings, scope, name, server):
    from mcp_settings import read_json

    return read_json(settings.credential_path(scope, name, server)).get("tokens")


def _broker(settings) -> int:
    runtime = McpRuntime(settings)
    output = threading.Lock()

    def respond(line):
        request_id = None
        try:
            message = json_object(line)
            request_id = message.get("id")
            action = message.get("action")
            if not isinstance(action, dict):
                raise ValueError("MCP broker requires an action object.")
            payload = model_mcp_result(runtime.invoke(action))
        except (ValueError, TypeError, OSError) as exc:
            payload = {"ok": False, "error": _error(exc)}
        with output:
            print(json.dumps({"id": request_id, "payload": payload}, ensure_ascii=False), flush=True)

    pool = ThreadPoolExecutor(max_workers=12)
    try:
        for line in sys.stdin:
            pool.submit(respond, line)
    finally:
        runtime.close()
        pool.shutdown(cancel_futures=True)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Manage Skillz MCP servers.")
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="Repository or artifact directory")
    parser.add_argument("--scope", default="cli", help="cli, repo, or artifact:<id>")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list")
    add = commands.add_parser("add", help="Add or update a server using its JSON settings")
    add.add_argument("name")
    add.add_argument("config", help="JSON settings or - to read settings from stdin")
    for command in ("enable", "disable", "remove", "login", "logout"):
        commands.add_parser(command).add_argument("name")
    commands.add_parser("request").add_argument("action", help="JSON MCP operation")
    commands.add_parser("broker", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    settings = McpSettings(args.root, args.scope)
    runtime = None
    try:
        if not settings.root.is_dir():
            raise ValueError("The configured workspace directory does not exist.")
        if args.command == "broker":
            return _broker(settings)
        payload = {"ok": True}
        if args.command == "list":
            payload = profile_status(settings)
        elif args.command == "add":
            settings.save(args.name, json_object(sys.stdin.read() if args.config == "-" else args.config))
            payload = profile_status(settings)
        elif args.command == "request":
            runtime = McpRuntime(settings)
            payload = runtime.invoke(json_object(args.action))
        else:
            entry = settings.servers().get(args.name)
            if entry is None:
                raise ValueError("Unknown MCP server in this profile.")
            source, server = entry
            if args.command == "login":
                authorize(settings, args.name)
            elif args.command == "logout":
                if server.get("auth") == "oauth":
                    settings.credential_path(source, args.name, server).unlink(missing_ok=True)
            elif args.command == "remove":
                settings.save(args.name, None)
                if server.get("auth") == "oauth":
                    settings.credential_path(source, args.name, server).unlink(missing_ok=True)
            else:
                settings.save(args.name, {**server, "enabled": args.command == "enable"})
            payload = profile_status(settings)
    except (Exception, KeyboardInterrupt) as exc:
        payload = {"ok": False, "error": _error(exc)}
    finally:
        if runtime is not None:
            runtime.close()
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
