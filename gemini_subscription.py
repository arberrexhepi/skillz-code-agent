from __future__ import annotations

"""Local Gemini CLI Google OAuth subscription integration.

The module intentionally keeps Google OAuth subscription-backed Gemini CLI
invocation separate from the Google GenAI API-key client. Authentication is
owned by the locally installed official Gemini CLI and its Google OAuth
session; no OAuth token is ever read or copied by Skillz.

Gemini CLI stdio is UTF-8. Subprocess pipe encodings must be explicit: setting
PYTHONIOENCODING only configures this Python process's own standard streams.
"""

import json
import os
import queue
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


GEMINI_SUBSCRIPTION_PROVIDER = "gemini-subscription"
_GEMINI_AUTH_DIRNAME = ".gemini"
_GEMINI_OAUTH_CREDS_FILE = "oauth_creds.json"
_GEMINI_ACCOUNTS_FILE = "google_accounts.json"
_GEMINI_SETTINGS_FILE = "settings.json"


def _posix_gemini_candidates() -> List[str]:
    home = Path.home()
    candidates = [
        str(home / ".local" / "bin" / "gemini"),
        str(home / ".npm-global" / "bin" / "gemini"),
        str(home / ".volta" / "bin" / "gemini"),
        str(home / ".asdf" / "shims" / "gemini"),
    ]
    if sys.platform == "darwin":
        candidates.extend(["/opt/homebrew/bin/gemini", "/usr/local/bin/gemini"])
    else:
        candidates.extend(["/usr/local/bin/gemini", "/usr/bin/gemini"])
    return candidates


def _windows_gemini_candidates() -> List[str]:
    roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    npm_dir = roaming / "npm"
    pkg_dir = npm_dir / "node_modules" / "@google" / "gemini-cli"
    return [
        str(pkg_dir / "bundle" / "gemini.js"),
        str(pkg_dir / "dist" / "index.js"),
        str(npm_dir / "gemini.cmd"),
        str(npm_dir / "gemini.exe"),
        str(local_app_data / "Programs" / "gemini-cli" / "gemini.exe"),
    ]


def resolve_gemini_executable() -> str:
    configured = str(os.environ.get("GEMINI_CLI_PATH", "") or "").strip()
    if configured:
        if Path(configured).is_file():
            return configured
        raise RuntimeError(
            "The configured Gemini CLI was not found at "
            + configured
            + ". Choose another executable or use automatic discovery."
        )
    on_path = shutil.which("gemini") or ""
    candidates = [on_path]
    install_dir = os.environ.get("GEMINI_INSTALL_DIR")
    if install_dir:
        if sys.platform == "win32":
            candidates.extend(
                [
                    str(Path(install_dir) / "gemini.exe"),
                    str(Path(install_dir) / "gemini.cmd"),
                    str(Path(install_dir) / "gemini.js"),
                ]
            )
        else:
            candidates.append(str(Path(install_dir) / "gemini"))
    if sys.platform == "win32":
        candidates.extend(_windows_gemini_candidates())
    else:
        candidates.extend(_posix_gemini_candidates())
    for candidate in candidates:
        try:
            if candidate and Path(candidate).is_file():
                return candidate
        except OSError:
            continue
    raise RuntimeError(
        "Gemini CLI was not found. Install @google/gemini-cli or set GEMINI_CLI_PATH to the local executable."
    )


def _build_cli_command(executable: str, cli_args: Sequence[str]) -> List[str]:
    suffix = Path(executable).suffix.lower()
    if suffix in {".js", ".mjs", ".cjs"}:
        node = shutil.which("node") or "node"
        return [node, executable, *cli_args]
    if sys.platform == "win32" and suffix in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        return [comspec, "/d", "/s", "/c", executable, *cli_args]
    return [executable, *cli_args]


def _clean_cli_environment() -> Dict[str, str]:
    env = dict(os.environ)
    # Prevent this provider from silently falling back to usage-based API or Vertex billing.
    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "GOOGLE_CLOUD_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GOOGLE_GENAI_USE_VERTEXAI",
    ):
        env.pop(name, None)
    return env


def _gemini_config_dir() -> Path:
    custom_home = str(os.environ.get("GEMINI_CLI_HOME", "") or "").strip()
    base = Path(custom_home) if custom_home else Path.home()
    return base / _GEMINI_AUTH_DIRNAME


def _read_selected_auth_type(config_dir: Path) -> str:
    settings_path = config_dir / _GEMINI_SETTINGS_FILE
    if not settings_path.is_file():
        return ""
    try:
        payload = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    security = payload.get("security")
    if isinstance(security, dict):
        auth = security.get("auth")
        if isinstance(auth, dict):
            selected = str(auth.get("selectedType") or "").strip()
            if selected:
                return selected
    return str(payload.get("selectedAuthType") or "").strip()


def _read_active_google_email(config_dir: Path) -> Optional[str]:
    accounts_path = config_dir / _GEMINI_ACCOUNTS_FILE
    if not accounts_path.is_file():
        return None
    try:
        payload = json.loads(accounts_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if isinstance(payload, dict):
        active = str(payload.get("active") or "").strip()
        if active and "@" in active:
            return active
    return None


def quick_gemini_auth_status(executable: Optional[str] = None) -> Dict[str, Any]:
    """Check whether Gemini CLI has a local Google OAuth session configured.

    Never reads or copies OAuth token secrets — only checks credential file
    presence and non-secret metadata.
    """
    _ = executable or resolve_gemini_executable()
    config_dir = _gemini_config_dir()
    oauth_path = config_dir / _GEMINI_OAUTH_CREDS_FILE
    selected_type = _read_selected_auth_type(config_dir)
    email = _read_active_google_email(config_dir)

    has_oauth_creds = False
    try:
        has_oauth_creds = oauth_path.is_file() and oauth_path.stat().st_size > 2
    except OSError:
        has_oauth_creds = False

    non_oauth_selected = selected_type.lower() in {"gemini-api-key", "vertex-ai"}
    authenticated = has_oauth_creds and not non_oauth_selected
    if authenticated:
        status_text = (
            f"Signed in with Google ({email})." if email else "Signed in with Google OAuth."
        )
    elif non_oauth_selected:
        status_text = (
            f"Gemini CLI is currently configured for '{selected_type}'. "
            "Run `gemini` in a terminal and switch authentication to Login with Google."
        )
    else:
        status_text = (
            "No local Gemini CLI Google OAuth session found. "
            "Run `gemini` in a terminal and sign in with Google."
        )
    return {
        "authenticated": authenticated,
        "auth_mode": "google_oauth" if authenticated else None,
        "account_type": selected_type or ("oauth-personal" if authenticated else None),
        "email": email if authenticated else None,
        "status_text": status_text,
    }


def _cli_version(executable: str) -> str:
    try:
        result = subprocess.run(
            _build_cli_command(executable, ["--version"]),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            env=_clean_cli_environment(),
            check=False,
        )
    except Exception:
        return ""
    lines = [line.strip() for line in str(result.stdout or result.stderr or "").splitlines() if line.strip()]
    return lines[-1] if lines else ""


def gemini_subscription_status(*, timeout: float = 10.0) -> Dict[str, Any]:
    _ = timeout
    try:
        executable = resolve_gemini_executable()
    except Exception as exc:
        return {
            "available": False,
            "authenticated": False,
            "provider": GEMINI_SUBSCRIPTION_PROVIDER,
            "error": str(exc),
            "models": [],
        }

    status: Dict[str, Any] = {
        "available": True,
        "authenticated": False,
        "provider": GEMINI_SUBSCRIPTION_PROVIDER,
        "cli_path": executable,
        "cli_version": _cli_version(executable),
        "models": [],
    }
    try:
        status.update(quick_gemini_auth_status(executable))
    except Exception as exc:
        status["error"] = str(exc)
    return status


def _launch_interactive_login_terminal(executable: str) -> None:
    """Open an interactive terminal running Gemini CLI with API keys cleared."""
    if sys.platform == "darwin":
        cmd = (
            "unset GEMINI_API_KEY GOOGLE_API_KEY GOOGLE_CLOUD_API_KEY "
            "GOOGLE_APPLICATION_CREDENTIALS GOOGLE_GENAI_USE_VERTEXAI; "
            f"{shlex.quote(executable)}"
        )
        script = f'tell application "Terminal" to do script {json.dumps(cmd)}\ntell application "Terminal" to activate'
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                "Could not open Terminal for Gemini CLI sign-in. Run `gemini` in your terminal and choose Login with Google."
            )
        return
    if sys.platform == "win32":
        cli_cmd = _build_cli_command(executable, [])
        subprocess.Popen(
            cli_cmd,
            env=_clean_cli_environment(),
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        return
    for terminal_cmd in (
        ["x-terminal-emulator", "-e", executable],
        ["gnome-terminal", "--", executable],
        ["konsole", "-e", executable],
    ):
        if shutil.which(terminal_cmd[0]):
            subprocess.Popen(terminal_cmd, env=_clean_cli_environment())
            return
    raise RuntimeError(
        "Could not launch an interactive terminal automatically. Run `gemini` in a terminal and choose Login with Google."
    )


def begin_gemini_google_login(*, timeout: float = 180.0, poll_interval: float = 1.5) -> Dict[str, Any]:
    executable = resolve_gemini_executable()
    current = quick_gemini_auth_status(executable)
    if current.get("authenticated"):
        return gemini_subscription_status()
    _launch_interactive_login_terminal(executable)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() < deadline:
        status = quick_gemini_auth_status(executable)
        if status.get("authenticated"):
            return gemini_subscription_status()
        time.sleep(max(0.1, float(poll_interval)))
    result = gemini_subscription_status()
    if not result.get("authenticated"):
        result["error"] = (
            "Timed out waiting for Gemini CLI Google sign-in. Finish signing in inside the opened terminal, then click Refresh."
        )
    return result


def sign_out_gemini_google() -> Dict[str, Any]:
    """Clear the local Gemini CLI Google OAuth session so another account can sign in."""
    config_dir = _gemini_config_dir()
    for filename in (_GEMINI_OAUTH_CREDS_FILE, _GEMINI_ACCOUNTS_FILE):
        target = config_dir / filename
        try:
            if target.is_file() or target.is_symlink():
                target.unlink()
        except OSError as exc:
            raise RuntimeError(f"Could not remove {target}: {exc}") from exc
    return gemini_subscription_status()


def _render_backend_prompt(system: str, messages: Sequence[Mapping[str, str]]) -> str:
    transcript: List[str] = []
    for item in messages:
        role = str(item.get("role", "user") or "user").strip().upper()
        content = str(item.get("content", "") or "")
        if content:
            transcript.append(f"[{role}]\n{content}")
    return (
        "You are the language-model backend inside Skillz Code Agent. "
        "Skillz—not Gemini CLI—owns repository access and executes workspace tools. "
        "Do not use Gemini CLI's built-in shell, file, web, or MCP tools directly. "
        "Instead, follow the Skillz system protocol and return the requested Skillz JSON action; "
        "Skillz will execute that action and provide its result on the next turn. "
        "Repository inspection, diagnostics, and mutations requested through the Skillz action schema "
        "are permitted unless the current Skillz host state explicitly blocks that specific action. "
        "Gemini CLI's isolated working directory does not prohibit returning Skillz read or mutation actions. "
        "Do not claim workspace actions are unavailable merely because this Gemini CLI subprocess cannot "
        "perform them directly. Follow the current planner goal and operator steering; treat quoted "
        "original requests, prior discovery-only instructions, discovery findings, and prior goal "
        "results as context rather than current execution restrictions unless the current goal repeats them. "
        "Return only the response Skillz requested, with no wrapper or commentary about this invocation.\n\n"
        f"[SKILLZ SYSTEM INSTRUCTIONS]\n{system}\n\n"
        "[SKILLZ CONVERSATION]\n"
        + "\n\n".join(transcript)
    )


def _skillz_permission_blocked_finish(text: str) -> str:
    """Return a false permission-block message from a Skillz finish action."""
    decoder = json.JSONDecoder()
    payload: Optional[Dict[str, Any]] = None
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            candidate, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            payload = candidate
            break
    if payload is None:
        return ""
    action = payload.get("action")
    if not isinstance(action, dict) or str(action.get("type", "") or "").strip() != "finish":
        return ""
    message = str(action.get("message", "") or "").strip()
    normalized = message.lower()
    permission_markers = (
        "no workspace actions permitted",
        "workspace actions are not permitted",
        "workspace inspection and edits were not permitted",
        "repository inspection and/or diagnostics, which are prohibited",
        "repository inspection or diagnostics, which are prohibited",
        "cannot inspect or modify the workspace",
    )
    if normalized.startswith("blocked:") and any(
        term in normalized for term in ("workspace", "repository", "inspection", "mutation", "diagnostic")
    ):
        return message
    return message if any(marker in normalized for marker in permission_markers) else ""


def _extract_usage(events: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    best: Dict[str, int] = {}
    aliases = {
        "input_tokens": ("input_tokens", "inputTokens", "prompt_tokens", "promptTokenCount"),
        "output_tokens": ("output_tokens", "outputTokens", "completion_tokens", "candidatesTokenCount"),
        "total_tokens": ("total_tokens", "totalTokens", "totalTokenCount"),
        "cached_tokens": ("cached", "cached_tokens", "cached_input_tokens", "cachedInputTokens", "cachedContentTokenCount"),
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for target, names in aliases.items():
                for name in names:
                    raw = value.get(name)
                    if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw >= best.get(target, -1):
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


def _extract_response_text(events: Sequence[Mapping[str, Any]], raw_stdout: str) -> str:
    chunks: List[str] = []
    fallback_result = ""
    for event in events:
        event_type = str(event.get("type") or "").strip().lower()
        if event_type == "message":
            role = str(event.get("role") or "").strip().lower()
            if role in {"", "assistant", "model"}:
                content = event.get("content")
                if isinstance(content, str) and content:
                    chunks.append(content)
        elif event_type == "result":
            for key in ("response", "text", "content"):
                candidate = event.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    fallback_result = candidate.strip()
                    break
    if chunks:
        return "".join(chunks).strip()
    if fallback_result:
        return fallback_result
    # If Gemini CLI emitted a single JSON object (non-streaming) with "response"
    stripped = raw_stdout.strip()
    if stripped.startswith("{"):
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict) and isinstance(parsed.get("response"), str):
                return parsed["response"].strip()
        except json.JSONDecodeError:
            pass
    return ""


def _run_gemini_exec_streaming(
    args: Sequence[str],
    *,
    prompt: str,
    cwd: str,
    timeout: float,
    env: Mapping[str, str],
    progress_callback: Callable[[Dict[str, Any]], None],
) -> subprocess.CompletedProcess[str]:
    """Run ``gemini --output-format stream-json`` while forwarding JSONL events."""
    process = subprocess.Popen(
        list(args),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        bufsize=1,
        env=dict(env),
    )
    stderr_parts: List[str] = []

    def drain_stderr() -> None:
        if process.stderr is None:
            return
        for line in process.stderr:
            stderr_parts.append(line)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()
    timed_out = threading.Event()

    def stop_after_timeout() -> None:
        if process.poll() is None:
            timed_out.set()
            process.kill()

    timer = threading.Timer(max(0.1, timeout), stop_after_timeout)
    timer.daemon = True
    timer.start()
    stdout_parts: List[str] = []
    started = time.monotonic()

    def write_stdin() -> None:
        if process.stdin is None:
            return
        try:
            process.stdin.write(prompt)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            # The CLI may exit early (e.g. auth/flag/service error) before reading stdin.
            pass
        finally:
            try:
                process.stdin.close()
            except OSError:
                pass

    stdin_thread = threading.Thread(target=write_stdin, daemon=True)
    try:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("Gemini subscription process did not expose stdio streams.")
        stdin_thread.start()
        for line in process.stdout:
            stdout_parts.append(line)
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            event = dict(event)
            event["skillz_elapsed_s"] = round(time.monotonic() - started, 1)
            try:
                progress_callback(event)
            except Exception:
                # UI feedback must never make a model invocation fail.
                pass
        returncode = process.wait()
    except BaseException:
        if process.poll() is None:
            process.kill()
            process.wait()
        raise
    finally:
        timer.cancel()
        stdin_thread.join(timeout=1)
        stderr_thread.join(timeout=1)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    if timed_out.is_set():
        raise subprocess.TimeoutExpired(list(args), timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(list(args), returncode, stdout=stdout, stderr=stderr)


def run_gemini_subscription_completion(
    *,
    model: str,
    thinking_mode: str,
    system: str,
    messages: Sequence[Mapping[str, str]],
    timeout: Optional[float] = None,
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
    on_summary: Optional[Callable[[str], None]] = None,
) -> Tuple[str, Dict[str, Any]]:
    _ = thinking_mode
    executable = resolve_gemini_executable()
    auth = quick_gemini_auth_status(executable)
    if not auth.get("authenticated"):
        raise RuntimeError(
            "Gemini CLI is not signed in with Google. Open Runtime settings and sign in before using "
            "the gemini-subscription provider."
        )

    effective_timeout = timeout
    if effective_timeout is None:
        try:
            effective_timeout = float(os.environ.get("GEMINI_SUBSCRIPTION_TIMEOUT_SECONDS", "600"))
        except ValueError:
            effective_timeout = 600.0
    effective_timeout = max(0.1, float(effective_timeout))

    prompt = _render_backend_prompt(system, messages)
    with tempfile.TemporaryDirectory(prefix="skillz-gemini-") as temp_dir:
        cli_args = [
            "--prompt",
            "",
            "--output-format",
            "stream-json",
            "--model",
            model,
            "--skip-trust",
            "-e",
            "none",
        ]
        args = _build_cli_command(executable, cli_args)

        def forward(event: Dict[str, Any]) -> None:
            event_type = str(event.get("type") or "").strip().lower()
            if on_summary is not None and event_type in {"thought", "reasoning"}:
                summary = event.get("content") or event.get("text")
                if isinstance(summary, str) and summary.strip():
                    on_summary(summary.strip() + "\n\n")
            if progress_callback is not None:
                progress_callback(event)

        cli_env = _clean_cli_environment()
        if progress_callback is None and on_summary is None:
            result = subprocess.run(
                args,
                input=prompt,
                cwd=temp_dir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=effective_timeout,
                env=cli_env,
                check=False,
            )
        else:
            result = _run_gemini_exec_streaming(
                args,
                prompt=prompt,
                cwd=temp_dir,
                timeout=effective_timeout,
                env=cli_env,
                progress_callback=forward,
            )

        events: List[Dict[str, Any]] = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        text = _extract_response_text(events, result.stdout)
        if result.returncode != 0 or not text:
            detail = str(result.stderr or "").strip()
            if not detail:
                errors = [
                    str(event.get("message") or event.get("error") or "").strip()
                    for event in events
                    if str(event.get("type") or "").lower() == "error" or event.get("error")
                ]
                detail = next((item for item in reversed(errors) if item), "Gemini CLI returned no response.")
            raise RuntimeError(f"Gemini subscription invocation failed: {detail}")
        false_block = _skillz_permission_blocked_finish(text)
        if false_block:
            raise RuntimeError(
                "Gemini subscription returned a false workspace-permission block instead of a Skillz action: "
                f"{false_block}"
            )
        return text, {
            "provider": GEMINI_SUBSCRIPTION_PROVIDER,
            "model": model,
            "auth_mode": "google_oauth",
            "billing_mode": "google_oauth_subscription",
            "usage": _extract_usage(events),
        }


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(argv or sys.argv[1:])
    command = args[0] if args else "status"
    try:
        if command == "status":
            payload = gemini_subscription_status()
        elif command == "login":
            payload = begin_gemini_google_login()
        elif command == "logout":
            payload = sign_out_gemini_google()
        else:
            raise ValueError(f"Unknown command: {command}")
        print(json.dumps(payload))
        return 0 if payload.get("available", True) else 1
    except Exception as exc:
        print(json.dumps({"available": False, "authenticated": False, "error": str(exc), "models": []}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
