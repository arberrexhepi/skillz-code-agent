"""One-shot chatbot process for generated artifacts.

The container owns orchestration and tool execution. Model credentials stay on the
desktop host behind a token-scoped broker.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request


def post(url: str, payload: dict, headers: dict[str, str] | None = None) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1200) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(detail or f"HTTP {error.code}") from error


def get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def structured(text: str) -> dict:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.S)
    if fenced:
        candidate = fenced.group(1)
    try:
        value = json.loads(candidate)
        if isinstance(value, dict):
            return value
    except ValueError:
        pass
    return {"type": "answer", "content": text}


def main() -> int:
    request = json.load(sys.stdin)
    message = str(request.get("message", "")).strip()
    if not message:
        raise ValueError("A message is required.")
    port = os.environ["SKILLZ_ARTIFACT_PORT"]
    local = f"http://127.0.0.1:{port}"
    commands = get(local + "/_skillz/commands").get("commands", [])
    system = (
        "You are the Skillz agent embedded in this artifact. Answer using the artifact's purpose and data. "
        "You may invoke one advertised command at a time. Discovery commands read data. Mutation commands change data and only run when the user explicitly approved mutations. "
        "Return strict JSON: {\"type\":\"answer\",\"content\":\"...\"} or {\"type\":\"tool\",\"name\":\"command\",\"input\":{...}}. "
        "Never invent commands or omit required inputs.\n\nAdvertised commands:\n" + json.dumps(commands, ensure_ascii=False)
    )
    history = request.get("history", [])
    messages = [item for item in history if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str)][-20:]
    messages.append({"role": "user", "content": message})
    broker = os.environ["SKILLZ_MODEL_BROKER_URL"]
    broker_headers = {"X-Skillz-Model-Token": os.environ["SKILLZ_MODEL_BROKER_TOKEN"]}
    for _ in range(8):
        result = post(broker, {"system": system, "messages": messages}, broker_headers)
        action = structured(str(result.get("text", "")))
        if action.get("type") != "tool":
            print(json.dumps({"message": str(action.get("content", result.get("text", ""))), "commands": commands}, ensure_ascii=False))
            return 0
        name = str(action.get("name", ""))
        try:
            tool_result = post(local + "/_skillz/commands/" + urllib.parse.quote(name), action.get("input") or {}, {"X-Skillz-Approve-Mutation": "true" if request.get("approveMutations") is True else "false"})
        except Exception as error:
            tool_result = {"error": str(error)}
        messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
        messages.append({"role": "user", "content": "Tool result for " + name + ": " + json.dumps(tool_result, ensure_ascii=False)})
    raise RuntimeError("The embedded agent exceeded eight tool steps.")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        raise SystemExit(1)
