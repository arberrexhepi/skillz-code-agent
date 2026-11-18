"""Start an artifact's Workbench preview through its scoped desktop broker."""
from __future__ import annotations

from typing import Callable


_start: Callable[[], dict] | None = None


def configure_preview_start(start: Callable[[], dict] | None) -> None:
    global _start
    _start = start


def start_artifact_preview() -> dict:
    if _start is None:
        return {"ok": False, "error": "Start Preview is available from an artifact's Workbench agent."}
    return _start()


def preview_guidance(*, tree: bool = False) -> str:
    if _start is None:
        return ""
    action = "`artifact-preview start`" if tree else '`{"type":"artifact_preview"}`'
    return (
        f"Use {action} to start this artifact's Workbench preview or retrieve its running URL. "
        "When Playwright MCP is enabled, pass it the returned host URL for browser checks. "
        "The artifact's container loopback is separate from the host browser."
    )
