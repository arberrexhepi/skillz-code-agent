from __future__ import annotations

"""Local Codex/ChatGPT subscription integration.

The module intentionally keeps subscription-backed Codex invocation separate
from the OpenAI API client. Authentication is owned by the locally installed
Codex CLI and its ChatGPT-managed OAuth session; no token is read or copied by
Skillz.
"""

import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


CODEX_SUBSCRIPTION_PROVIDER = "codex-subscription"
_MACOS_BUNDLED_CODEX = Path("/Applications/ChatGPT.app/Contents/Resources/codex")


def resolve_codex_executable() -> str:
    configured = str(os.environ.get("CODEX_CLI_PATH", "") or "").strip()
    candidates = [configured, shutil.which("codex") or ""]
    if sys.platform == "darwin":
        candidates.append(str(_MACOS_BUNDLED_CODEX))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    raise RuntimeError(
        "Codex CLI was not found. Install Codex or set CODEX_CLI_PATH to the local executable."
    )


def _clean_cli_environment() -> Dict[str, str]:
    env = dict(os.environ)
    # Prevent this provider from silently falling back to usage-based API auth.
    for name in (
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_API_BASE",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
    ):
        env.pop(name, None)
    return env


def quick_chatgpt_auth_status(executable: Optional[str] = None) -> Dict[str, Any]:
    cli = executable or resolve_codex_executable()
    result = subprocess.run(
        [cli, "login", "status"],
        capture_output=True,
        text=True,
        timeout=10,
        env=_clean_cli_environment(),
        check=False,
    )
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    normalized = output.lower()
    return {
        "authenticated": result.returncode == 0 and "logged in using chatgpt" in normalized,
        "auth_mode": "chatgpt" if "logged in using chatgpt" in normalized else None,
        "status_text": output,
    }


class CodexAppServerSession:
    """Small synchronous JSON-RPC client for auth and model discovery."""

    def __init__(self, executable: Optional[str] = None, *, timeout: float = 10.0) -> None:
        self.executable = executable or resolve_codex_executable()
        self.timeout = timeout
        self._next_id = 1
        self._lines: "queue.Queue[Optional[str]]" = queue.Queue()
        self._notifications: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._process = subprocess.Popen(
            [self.executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=_clean_cli_environment(),
        )
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "skillz_code_agent",
                    "title": "Skillz Code Agent",
                    "version": "0.1.0",
                }
            },
        )
        self.notify("initialized", {})

    def __enter__(self) -> "CodexAppServerSession":
        return self

    def __exit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        self.close()

    def _read_stdout(self) -> None:
        stream = self._process.stdout
        if stream is None:
            self._lines.put(None)
            return
        for line in stream:
            self._lines.put(line)
        self._lines.put(None)

    def _drain_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        for _line in stream:
            pass

    def _write(self, payload: Mapping[str, Any]) -> None:
        stream = self._process.stdin
        if stream is None or self._process.poll() is not None:
            raise RuntimeError("Codex app-server is not running.")
        stream.write(json.dumps(dict(payload), separators=(",", ":")) + "\n")
        stream.flush()

    def notify(self, method: str, params: Optional[Mapping[str, Any]] = None) -> None:
        self._write({"method": method, "params": dict(params or {})})

    def request(
        self,
        method: str,
        params: Optional[Mapping[str, Any]] = None,
        *,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._write({"method": method, "id": request_id, "params": dict(params or {})})
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for Codex app-server method {method}.")
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError(f"Timed out waiting for Codex app-server method {method}.") from exc
            if line is None:
                raise RuntimeError("Codex app-server exited before returning a response.")
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") == request_id:
                error = message.get("error")
                if error:
                    detail = error.get("message") if isinstance(error, dict) else error
                    raise RuntimeError(f"Codex app-server {method} failed: {detail}")
                result = message.get("result")
                return dict(result) if isinstance(result, dict) else {}
            if isinstance(message.get("method"), str):
                self._notifications.put(message)

    def wait_for_notification(
        self,
        method: str,
        *,
        predicate: Optional[Any] = None,
        timeout: float = 300.0,
    ) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        deferred: List[Dict[str, Any]] = []
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Timed out waiting for Codex notification {method}.")
                try:
                    message = self._notifications.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    line: Optional[str]
                    try:
                        line = self._lines.get_nowait()
                    except queue.Empty:
                        continue
                    if line is None:
                        raise RuntimeError("Codex app-server exited while waiting for authentication.")
                    try:
                        parsed = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict) and isinstance(parsed.get("method"), str):
                        message = parsed
                    else:
                        continue
                params = message.get("params") if isinstance(message.get("params"), dict) else {}
                if message.get("method") == method and (predicate is None or predicate(params)):
                    return dict(params)
                deferred.append(message)
        finally:
            for message in deferred:
                self._notifications.put(message)

    def close(self) -> None:
        if self._process.poll() is None:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=2)


def _model_names(payload: Mapping[str, Any]) -> List[str]:
    models: List[str] = []
    data = payload.get("data")
    if not isinstance(data, list):
        return models
    for item in data:
        if not isinstance(item, dict) or item.get("hidden") is True:
            continue
        value = str(item.get("model") or item.get("id") or "").strip()
        if value and value not in models:
            models.append(value)
    return models


def _cli_version(executable: str) -> str:
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            env=_clean_cli_environment(),
            check=False,
        )
    except Exception:
        return ""
    return str(result.stdout or result.stderr or "").strip().splitlines()[-1]


def codex_subscription_status(*, timeout: float = 10.0) -> Dict[str, Any]:
    try:
        executable = resolve_codex_executable()
    except Exception as exc:
        return {
            "available": False,
            "authenticated": False,
            "provider": CODEX_SUBSCRIPTION_PROVIDER,
            "error": str(exc),
            "models": [],
        }

    status: Dict[str, Any] = {
        "available": True,
        "authenticated": False,
        "provider": CODEX_SUBSCRIPTION_PROVIDER,
        "cli_path": executable,
        "cli_version": _cli_version(executable),
        "models": [],
    }
    try:
        with CodexAppServerSession(executable, timeout=timeout) as session:
            account_result = session.request("account/read", {"refreshToken": False})
            account = account_result.get("account")
            if isinstance(account, dict):
                account_type = str(account.get("type") or "").strip()
                status.update(
                    {
                        "account_type": account_type or None,
                        "auth_mode": "chatgpt" if account_type == "chatgpt" else account_type or None,
                        "authenticated": account_type == "chatgpt",
                        "email": account.get("email"),
                        "plan_type": account.get("planType"),
                    }
                )
            models = session.request("model/list", {"limit": 100, "includeHidden": False})
            status["models"] = _model_names(models)
    except Exception as exc:
        # A quick status still distinguishes a valid ChatGPT session if an older
        # app-server build cannot provide the richer account payload.
        try:
            status.update(quick_chatgpt_auth_status(executable))
        except Exception:
            pass
        status["error"] = str(exc)
    return status


def begin_codex_chatgpt_login(*, timeout: float = 300.0) -> Dict[str, Any]:
    executable = resolve_codex_executable()
    with CodexAppServerSession(executable, timeout=15.0) as session:
        login = session.request(
            "account/login/start",
            {"type": "chatgpt", "useHostedLoginSuccessPage": True, "appBrand": "chatgpt"},
        )
        login_id = str(login.get("loginId") or "").strip()
        auth_url = str(login.get("authUrl") or "").strip()
        if not login_id or not auth_url:
            raise RuntimeError("Codex did not return a ChatGPT sign-in URL.")
        if not webbrowser.open(auth_url):
            raise RuntimeError("Could not open the ChatGPT sign-in page in the default browser.")
        completed = session.wait_for_notification(
            "account/login/completed",
            predicate=lambda params: str(params.get("loginId") or "") == login_id,
            timeout=timeout,
        )
        if not completed.get("success"):
            raise RuntimeError(str(completed.get("error") or "ChatGPT sign-in did not complete."))
    return codex_subscription_status(timeout=15.0)


def _render_backend_prompt(system: str, messages: Sequence[Mapping[str, str]]) -> str:
    transcript: List[str] = []
    for item in messages:
        role = str(item.get("role", "user") or "user").strip().upper()
        content = str(item.get("content", "") or "")
        if content:
            transcript.append(f"[{role}]\n{content}")
    return (
        "You are the language-model backend inside Skillz Code Agent. "
        "Do not inspect files, run commands, call tools, or modify the workspace. "
        "Follow the supplied system instructions and return only the response that "
        "Skillz requested, with no wrapper or commentary about this invocation.\n\n"
        f"[SKILLZ SYSTEM INSTRUCTIONS]\n{system}\n\n"
        "[SKILLZ CONVERSATION]\n"
        + "\n\n".join(transcript)
    )


def _extract_usage(events: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    best: Dict[str, int] = {}
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens"),
        "output_tokens": ("output_tokens", "outputTokens"),
        "total_tokens": ("total_tokens", "totalTokens"),
        "cached_tokens": ("cached_input_tokens", "cachedInputTokens", "cached_tokens"),
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for target, names in aliases.items():
                for name in names:
                    raw = value.get(name)
                    if isinstance(raw, (int, float)) and raw >= best.get(target, -1):
                        best[target] = int(raw)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for event in events:
        visit(event)
    if "total_tokens" not in best and ("input_tokens" in best or "output_tokens" in best):
        best["total_tokens"] = best.get("input_tokens", 0) + best.get("output_tokens", 0)
    return best


def run_codex_subscription_completion(
    *,
    model: str,
    thinking_mode: str,
    system: str,
    messages: Sequence[Mapping[str, str]],
    timeout: Optional[float] = None,
) -> Tuple[str, Dict[str, Any]]:
    executable = resolve_codex_executable()
    auth = quick_chatgpt_auth_status(executable)
    if not auth.get("authenticated"):
        raise RuntimeError(
            "Codex is not signed in with ChatGPT. Open Runtime settings and sign in before using "
            "the codex-subscription provider."
        )

    effective_timeout = timeout
    if effective_timeout is None:
        try:
            effective_timeout = float(os.environ.get("CODEX_SUBSCRIPTION_TIMEOUT_SECONDS", "600"))
        except ValueError:
            effective_timeout = 600.0
    effort = str(thinking_mode or "medium").strip().lower()
    if effort in {"", "auto", "none", "minimal"}:
        effort = "low" if effort in {"none", "minimal"} else "medium"

    prompt = _render_backend_prompt(system, messages)
    with tempfile.TemporaryDirectory(prefix="skillz-codex-") as temp_dir:
        output_path = Path(temp_dir) / "last-message.txt"
        args = [
            executable,
            "exec",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--model",
            model,
            "-c",
            f'model_reasoning_effort="{effort}"',
            "--cd",
            temp_dir,
            "--json",
            "--output-last-message",
            str(output_path),
            "-",
        ]
        result = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            env=_clean_cli_environment(),
            check=False,
        )
        events: List[Dict[str, Any]] = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        text = output_path.read_text(encoding="utf-8").strip() if output_path.exists() else ""
        if result.returncode != 0 or not text:
            detail = str(result.stderr or "").strip()
            if not detail:
                errors = [str(event.get("message") or event.get("error") or "").strip() for event in events]
                detail = next((item for item in reversed(errors) if item), "Codex returned no response.")
            raise RuntimeError(f"Codex subscription invocation failed: {detail}")
        return text, {
            "provider": CODEX_SUBSCRIPTION_PROVIDER,
            "model": model,
            "auth_mode": "chatgpt",
            "billing_mode": "chatgpt_subscription",
            "usage": _extract_usage(events),
        }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv or sys.argv[1:])
    command = args[0] if args else "status"
    try:
        if command == "status":
            payload = codex_subscription_status()
        elif command == "login":
            payload = begin_codex_chatgpt_login()
        else:
            raise ValueError(f"Unknown command: {command}")
        print(json.dumps(payload))
        return 0 if payload.get("available", True) else 1
    except Exception as exc:
        print(json.dumps({"available": False, "authenticated": False, "error": str(exc), "models": []}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
