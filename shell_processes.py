"""Run workspace npm servers without blocking the agent's tool loop."""
from __future__ import annotations

import atexit
from collections import deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import threading
import uuid


STARTUP_WAIT = 1.5
SERVER_SCRIPT_NAMES = {"dev", "start", "serve", "preview", "watch"}
PACKAGE_SHELL_GUIDANCE = (
    "Run package.json scripts with npm run <script>. Declared dev, start, serve, preview, and watch scripts "
    "start as managed processes when invoked on their own. Prefix another npm script with --background "
    "to start it the same way. Use status <process_id> for logs and stop <process_id> to stop it. "
    "Check the app separately before claiming it is ready."
)


def npm_script(command: str | list[str]) -> tuple[list[str], str] | None:
    if isinstance(command, str):
        if "\n" in command:
            return None
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
            lexer.whitespace_split = True
            argv = list(lexer)
        except ValueError:
            return None
        if any(token and all(char in ";&|<>()" for char in token) for token in argv):
            return None
    elif isinstance(command, (list, tuple)) and all(isinstance(token, str) for token in command):
        argv = list(command)
    else:
        return None
    if not argv or argv[0] != "npm" or len(argv) < 2:
        return None
    if argv[1] in {"run", "run-script"} and len(argv) > 2 and not argv[2].startswith("-"):
        return argv, argv[2]
    if argv[1] == "start":
        return argv, "start"
    return None


@dataclass
class ManagedScript:
    root: Path
    argv: tuple[str, ...]
    child: subprocess.Popen[bytes]
    process_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    logs: deque[bytes] = field(default_factory=lambda: deque(maxlen=12))
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader: threading.Thread | None = None
    stopped: bool = False

    def read_logs(self) -> None:
        if self.child.stdout is not None:
            with self.child.stdout as stream:
                while chunk := stream.read1(1024):
                    with self.lock:
                        self.logs.append(chunk)

    def result(self) -> dict:
        returncode = self.child.poll()
        if returncode is not None and self.reader is not None:
            self.reader.join(timeout=0.15)
        with self.lock:
            output = b"".join(self.logs).decode("utf-8", errors="replace").strip()
        return {"process_id": self.process_id, "command": list(self.argv), "stdout": output,
                "process_state": "stopped" if self.stopped else "running" if returncode is None else "exited",
                **({"returncode": returncode} if returncode is not None else {})}

    def stop(self) -> None:
        if self.child.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(self.child.pid), "/T", "/F"],
                           capture_output=True, timeout=5, check=True)
        else:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            self.child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name != "nt":
                try:
                    os.killpg(self.child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            self.child.wait(timeout=2)
        self.stopped = True


_processes: dict[str, ManagedScript] = {}
_lock = threading.Lock()


def _start(root: Path, argv: list[str], env: dict[str, str]) -> dict:
    with _lock:
        for process in _processes.values():
            if process.root == root and process.argv == tuple(argv) and process.child.poll() is None:
                return process.result()
        for key, process in list(_processes.items())[:-16]:
            if process.child.poll() is not None:
                del _processes[key]
        executable = shutil.which("npm", path=env.get("PATH"))
        if executable is None:
            raise ValueError("npm was not found on the shell PATH.")
        child = subprocess.Popen(
            [executable, *argv[1:]], cwd=root, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
        )
        process = ManagedScript(root, tuple(argv), child)
        _processes[process.process_id] = process
        process.reader = threading.Thread(target=process.read_logs, daemon=True)
        process.reader.start()
    try:
        child.wait(timeout=STARTUP_WAIT)
    except subprocess.TimeoutExpired:
        pass
    result = process.result()
    if result.get("returncode") not in {None, 0}:
        result["error"] = f"Package script exited with code {result['returncode']}."
    return result


def run_managed_shell(root: Path, action: dict, env: dict[str, str], *, allow_changes: bool = True) -> dict | None:
    """Handle process control and background package scripts, leaving finite shell commands to their normal path."""
    root = root.resolve()
    command = action.get("command", "")
    operation, process_id = action.get("operation"), action.get("process_id")
    background = action.get("background") is True
    if isinstance(command, str):
        control = command.strip().split()
        if len(control) == 2 and control[0] in {"status", "stop"}:
            operation, process_id = control
        if command.startswith("--background "):
            background = True
            command = command.removeprefix("--background ")
    if operation in ("status", "stop"):
        if operation == "stop" and not allow_changes:
            return {"error": "Stopping a shell process is available during goal execution."}
        with _lock:
            process = _processes.get(process_id) if isinstance(process_id, str) else None
        if process is None or process.root != root:
            return {"error": "Unknown shell process ID for this workspace."}
        try:
            if operation == "stop":
                process.stop()
            return process.result()
        except (OSError, subprocess.SubprocessError) as exc:
            return {"error": f"Could not stop shell process: {exc}"}

    script = npm_script(command)
    if script is None:
        return {"error": "Use --background with a single npm run <script> command."} if background else None
    argv, name = script
    if not background and name.split(":", 1)[0] not in SERVER_SCRIPT_NAMES:
        return None
    if not allow_changes:
        return {"error": "Starting a package script is available during goal execution."}
    try:
        manifest = json.loads((root / "package.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {"error": "A readable package.json is required to start a managed npm script."}
    scripts = manifest.get("scripts") if isinstance(manifest, dict) else None
    if not isinstance(scripts, dict) or not isinstance(scripts.get(name), str):
        return {"error": f"package.json does not define npm script {name!r}."}
    try:
        return _start(root, argv, env)
    except OSError as exc:
        return {"error": f"Could not start npm script: {exc}"}


@atexit.register
def stop_shell_processes() -> None:
    for process in list(_processes.values()):
        if process.child.poll() is None:
            try:
                process.stop()
            except (OSError, subprocess.SubprocessError):
                pass


def install_shell_process_cleanup() -> None:
    if threading.current_thread() is threading.main_thread():
        def terminate(_signum, _frame):
            raise SystemExit(128 + signal.SIGTERM)

        signal.signal(signal.SIGTERM, terminate)
