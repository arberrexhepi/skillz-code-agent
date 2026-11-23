"""Use an artifact's Workbench preview through its scoped desktop broker."""
from __future__ import annotations

from typing import Callable
from urllib.parse import urlsplit


_start: Callable[[], dict] | None = None
_request: Callable[[dict], dict] | None = None


def configure_preview_start(start: Callable[[], dict] | None, request: Callable[[dict], dict] | None = None) -> None:
    global _start, _request
    _start = start
    _request = request


def has_preview_broker() -> bool:
    return _request is not None


def start_artifact_preview() -> dict:
    if _start is None:
        return {"ok": False, "error": "Start Preview is available from an artifact's Workbench agent."}
    return _start()


def check_artifact_route(target: str, base_url: str) -> dict | None:
    if _request is None:
        return None
    resolved_target = urlsplit(target)
    for parsed in (resolved_target, urlsplit(base_url)):
        if parsed.scheme and (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}):
            return None  # Explicit non-artifact destinations use the existing workspace route checker.
        if parsed.netloc and (not parsed.scheme or parsed.username or parsed.password):
            return {"ok": False, "error": "Expected an artifact route or a loopback URL without credentials."}
    path = "/" + resolved_target.path.lstrip("/")
    if resolved_target.query:
        path += "?" + resolved_target.query
    if resolved_target.fragment:
        path += "#" + resolved_target.fragment
    return _request({"operation": "route-check", "route": path})


def run_artifact_preview(action: dict) -> dict:
    operation = action.get("operation", "start")
    if operation == "start":
        return start_artifact_preview()
    if _request is None:
        return {"ok": False, "unavailable": True, "error": "Application verification is available from an artifact's Workbench agent."}
    if operation == "restart":
        return _request({"operation": operation})
    arguments = action.get("input")
    if operation == "browser-check" and isinstance(arguments, dict):
        return _request({"operation": operation, "input": arguments})
    if operation not in ("mcp", "chat") or not isinstance(arguments, dict):
        return {"ok": False, "error": "Use a Preview mcp, chat, or browser-check action with a JSON object."}
    from mcp_runtime import model_mcp_result
    return model_mcp_result(_request({"operation": operation, "input": arguments}))


def preview_guidance(*, tree: bool = False) -> str:
    if _start is None:
        return ""
    action = "`artifact-preview start`" if tree else '`{"type":"artifact_preview"}`'
    verification = ""
    if _request is not None:
        verification = (
            " Use `artifact-preview browser-check <JSON>` for managed Playwright interactions and visible-state assertions. "
            "During goal execution, use `artifact-preview restart`, `artifact-preview mcp <JSON>`, or `artifact-preview chat <JSON>` for this app."
            if tree else
            ' For app verification, use artifact_preview with operation browser-check and input for managed Playwright interactions, or use restart, mcp, or chat.'
        )
    route_check = "`run-route-check /path` checks the managed preview using the Workbench inspection browser. " if tree and _request is not None else ""
    return (
        f"Use {action} to start this artifact's Workbench preview or retrieve its running URL. {route_check}"
        "When Playwright MCP is enabled, pass it the returned host URL for browser checks. "
        "The artifact's container loopback is separate from the host browser."
        + verification
    )
