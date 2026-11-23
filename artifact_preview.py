"""Start an artifact's Workbench preview through its scoped desktop broker."""
from __future__ import annotations

from typing import Callable


_start: Callable[[], dict] | None = None
_request: Callable[[dict], dict] | None = None


def configure_preview_start(start: Callable[[], dict] | None, request: Callable[[dict], dict] | None = None) -> None:
    global _start, _request
    _start = start
    _request = request


def start_artifact_preview() -> dict:
    if _start is None:
        return {"ok": False, "error": "Start Preview is available from an artifact's Workbench agent."}
    return _start()


def run_artifact_preview(action: dict) -> dict:
    operation = action.get("operation", "start")
    if operation == "start":
        return start_artifact_preview()
    if _request is None:
        return {"ok": False, "error": "Application verification is available from an artifact's Workbench agent."}
    if operation == "restart":
        return _request({"operation": operation})
    arguments = action.get("input")
    if operation not in ("mcp", "chat") or not isinstance(arguments, dict):
        return {"ok": False, "error": "Use a Preview mcp or chat action with a JSON object."}
    from mcp_runtime import model_mcp_result
    return model_mcp_result(_request({"operation": operation, "input": arguments}))


def preview_guidance(*, tree: bool = False) -> str:
    if _start is None:
        return ""
    action = "`artifact-preview start`" if tree else '`{"type":"artifact_preview"}`'
    verification = ""
    if _request is not None:
        verification = (
            " During goal execution, use `artifact-preview restart`, `artifact-preview mcp <JSON>`, or `artifact-preview chat <JSON>` for this app."
            if tree else
            ' For app verification, use the artifact_preview action with operation restart, mcp, or chat and put the app request object in input.'
        )
    return (
        f"Use {action} to start this artifact's Workbench preview or retrieve its running URL. "
        "When Playwright MCP is enabled, pass it the returned host URL for browser checks. "
        "The artifact's container loopback is separate from the host browser."
        + verification
    )
