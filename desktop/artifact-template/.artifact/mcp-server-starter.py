"""Artifact-local MCP server. Attach additional tools to this server."""
from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Annotated

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

source = Path(__file__).resolve()
server = FastMCP(source.parent.name)
note_file = source.parents[2] / ".artifact-data" / "mcp" / source.parent.name / "note.txt"


@server.tool(annotations=ToolAnnotations(readOnlyHint=True))
def get_note() -> dict[str, str | None]:
    """Read the note saved by this artifact's MCP server."""
    try:
        return {"note": note_file.read_text(encoding="utf-8")}
    except FileNotFoundError:
        return {"note": None}


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))
def save_note(text: Annotated[str, Field(min_length=1, max_length=4000)]) -> dict[str, str]:
    """Save a note in the artifact, replacing its previous value."""
    note_file.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=note_file.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(text)
            file.close()
            os.replace(temporary, note_file)
        finally:
            temporary.unlink(missing_ok=True)
    return {"note": text}


if __name__ == "__main__":
    server.run(transport="stdio")
