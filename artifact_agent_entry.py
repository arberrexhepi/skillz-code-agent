"""Container-only entry point: isolate every agent tool; broker model calls over stdio."""
from __future__ import annotations
import importlib
import json
import queue
import sys
import threading
import uuid

requests: queue.Queue[str | None] = queue.Queue()
pending: dict[str, queue.Queue[dict]] = {}
lock = threading.Lock()
original_stdin = sys.stdin


def receive() -> None:
    try:
        for line in original_stdin:
            try:
                message = json.loads(line)
            except ValueError:
                requests.put(line)
                continue
            if message.get("type") == "artifact_model_response":
                with lock:
                    target = pending.get(message.get("id", ""))
                if target is not None:
                    target.put(message)
            else:
                requests.put(line)
    finally:
        requests.put(None)
        with lock:
            for target in pending.values():
                target.put({"error": "Desktop connection closed."})


class BridgeInput:
    def __iter__(self):
        while True:
            line = requests.get()
            if line is None:
                return
            yield line


def main() -> int:
    import main as agent

    class BrokerClient(agent.BaseModelClient):
        def __init__(self, **selection):
            self.selection = selection
            self.model = selection["model"]
            self.provider_name = selection["provider"]
            self.thinking_mode = selection.get("thinking_mode", "medium")
            self.verbosity = selection.get("verbosity", "medium")
            self.backoff = agent.BackoffStrategy()
            self.progress_callback = None

        def set_progress_callback(self, callback):
            self.progress_callback = callback

        def complete(self, system, prompt):
            return self.complete_messages(system, [{"role": "user", "content": prompt}])

        def complete_messages(self, system, messages):
            request_id = str(uuid.uuid4())
            response_queue: queue.Queue[dict] = queue.Queue()
            with lock:
                pending[request_id] = response_queue
            try:
                payload = {"type": "artifact_model_request", "id": request_id, **self.selection, "system": system, "messages": list(messages), "prompt_cache_key": getattr(self, "prompt_cache_key", "")}
                print(json.dumps(payload, ensure_ascii=False), flush=True)
                while True:
                    response = response_queue.get(timeout=1200)
                    if "progress" not in response:
                        break
                    if self.progress_callback:
                        self.progress_callback(response["progress"])
                if response.get("error"):
                    raise RuntimeError(response["error"])
                self._set_last_metrics(response.get("metrics", {}))
                return str(response.get("text", ""))
            finally:
                with lock:
                    pending.pop(request_id, None)

        def clone(self):
            clone = BrokerClient(**self.selection)
            clone.backoff = agent.BackoffStrategy(enabled=self.backoff.enabled, token_limit_k=self.backoff.token_limit_k)
            clone.progress_callback = self.progress_callback
            return clone

    agent.create_model_client = lambda **selection: BrokerClient(**selection)
    sys.stdin = BridgeInput()
    threading.Thread(target=receive, daemon=True).start()
    module_name = sys.argv.pop(1).removesuffix(".py")
    if module_name not in {"main", "main_v2", "live_test_loop"}:
        raise ValueError("Unsupported agent backend")
    module = agent if module_name == "main" else importlib.import_module(module_name)
    return module.main()


if __name__ == "__main__":
    raise SystemExit(main())
