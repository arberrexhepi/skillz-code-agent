"""Small outcome handoffs between goals; raw transcripts remain audit history."""

import json
from typing import Any, Sequence

HANDOFF_READ_CHARS = 32000
HANDOFF_SUMMARY_CHARS = 4000


def summarize_handoff(history: Sequence[Any]) -> str:
    outcomes = []
    for turn in history[-3:]:
        for result in turn.results:
            paths = (result.tool_action or {}).get("_changed_paths", [])
            if paths:
                outcomes.append("Observed file changes: " + json.dumps(paths, ensure_ascii=False))
            if result.command_type == "finish":
                outcomes.append("Previous outcome: " + result.output[:1000])
            elif not result.ok and result.command_type != "skipped":
                outcomes.append("Previous tool failure (current diagnostics take precedence): " + result.output[:400])
    return ("Bounded goal handoff. Follow the current goal; previous plans and promises are omitted.\n"
            + "\n".join(outcomes))[:HANDOFF_SUMMARY_CHARS]
