"""Trusted model-only helper. No file, shell, or agent-action RPC endpoints."""
import json
import os
import sys
import shlex
from pathlib import Path
os.environ["SKILLZ_CODEX_MODEL_ONLY"] = "1"
from main import create_model_client


def setup_error(error):
    message = str(error)
    for package in ("google-genai", "openai", "anthropic"):
        if message.startswith(f"{package} package not installed."):
            executable = "& '" + sys.executable.replace("'", "''") + "'" if os.name == "nt" else shlex.quote(sys.executable)
            return (f"Artifact model requests use local Python: {sys.executable}. "
                    f"Install the missing {package} SDK there: {executable} -m pip install {package}. "
                    "Then restart the artifact agent.")
    if "_API_KEY is not set" in message:
        return (f"{message} Configure it in {Path(__file__).with_name('.env')} or the environment "
                "that launches the workbench, then restart the artifact agent. "
                "Provider credentials belong on the host, outside the artifact repository.")
    return message


def capabilities(request):
    import main as agent
    from runtime_catalog import BASE_RUNTIME_PROVIDER_CATALOG, normalize_provider
    provider = normalize_provider(request['provider'])
    info = BASE_RUNTIME_PROVIDER_CATALOG[provider]
    package = info.get('package')
    if provider in {'local', 'ollama', 'ollama-local', 'ollama-runpod'}:
        package = 'openai'
    available = {'openai': agent.OpenAI is not None, 'anthropic': agent.Anthropic is not None,
                 'google-genai': agent.genai is not None and agent.genai_types is not None}
    key = info.get('env_var')
    return {'package': package, 'sdkReady': available.get(package, True), 'keyName': key,
            'keyReady': not key or bool(os.getenv(key)), 'label': info['label']}


def main(check=False, inspect=False):
    request = json.loads(sys.stdin.read(16 * 1024 * 1024))
    if inspect:
        print(json.dumps(capabilities(request)), flush=True)
        return
    client = create_model_client(provider=request["provider"], model=request["model"], thinking_mode=request.get("thinking_mode", "medium"), verbosity=request.get("verbosity", "medium"))
    if check:
        # Construct the selected client to validate its local SDK and configuration.
        # Do not call the provider or consume model usage during startup checks.
        print(json.dumps({"ready": True}), flush=True)
        return
    client.set_prompt_cache_key(request.get("prompt_cache_key", ""))
    progress = getattr(client, "set_progress_callback", None)
    if callable(progress):
        progress(lambda event: print(json.dumps({"progress": event}), flush=True))
    text = client.complete_messages(request["system"], request["messages"])
    print(json.dumps({"text": text, "metrics": client.get_last_metrics()}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    try:
        main(check="--check" in sys.argv[1:], inspect="--capabilities" in sys.argv[1:])
    except Exception as error:
        print(json.dumps({"error": setup_error(error)}), flush=True)
