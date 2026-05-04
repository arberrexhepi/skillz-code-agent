from __future__ import annotations

from copy import deepcopy
import importlib
import os
import threading
from typing import Any, Dict, Iterable, List, Optional


BASE_RUNTIME_PROVIDER_CATALOG: Dict[str, Dict[str, Any]] = {
    "openai": {
        "label": "OpenAI",
        "package": "openai",
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-5.4",
        "models": [
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.4-nano",
            "gpt-4.1",
            "gpt-4.1-mini",
            "o4-mini",
        ],
        "notes": "Uses the Responses API. Any compatible OpenAI model string is allowed.",
    },
    "anthropic": {
        "label": "Anthropic",
        "package": "anthropic",
        "env_var": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
        "models": [
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "claude-haiku-4-5-20251001",
        ],
        "notes": "Any valid Anthropic model string is allowed.",
    },
    "gemini": {
        "label": "Gemini",
        "package": "google-genai",
        "env_var": "GEMINI_API_KEY",
        "default_model": "gemini-3.1-pro-preview",
        "models": [
            "gemini-3.1-pro-preview",
            "gemini-3-flash-preview",
            "gemini-3.1-flash-lite-preview",
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
        ],
        "notes": "Any valid Gemini model string is allowed.",
    },
    "local": {
        "label": "Local OpenAI-compatible",
        "package": None,
        "env_var": None,
        "default_model": "gemma4",
        "models": [
            "gemma4",
        ],
        "notes": "Targets the localhost OpenAI-compatible endpoint at http://127.0.0.1:5051/v1.",
    },
    "ollama": {
        "label": "Ollama (Legacy Alias)",
        "package": None,
        "env_var": None,
        "default_model": "gemma4:e2b",
        "models": [
            "gemma4:e2b",
        ],
        "notes": "Legacy alias for ollama-local.",
        "hidden": True,
    },
    "ollama-local": {
        "label": "Ollama Local",
        "package": None,
        "env_var": None,
        "default_model": "gemma4:e2b",
        "models": [
            "gemma4:e2b",
        ],
        "notes": "Targets the localhost Ollama OpenAI-compatible endpoint at http://127.0.0.1:11434/v1.",
    },
    "ollama-runpod": {
        "label": "Ollama Runpod",
        "package": None,
        "env_var": None,
        "default_model": "gemma4:latest",
        "models": [
            "gemma4:latest",
            "gemma4:e2b",
        ],
        "notes": "Targets the Runpod Ollama endpoint at https://zql0xy4x10v0sp-11434.proxy.runpod.net/v1/chat/completions.",
    },
}

RUNTIME_PROVIDER_CATALOG: Dict[str, Dict[str, Any]] = deepcopy(BASE_RUNTIME_PROVIDER_CATALOG)
_RUNTIME_CATALOG_REFRESH_LOCK = threading.Lock()
_RUNTIME_CATALOG_REFRESH_DONE = False


def _dedupe_preserve_order(items: Iterable[str]) -> List[str]:
    seen: Dict[str, None] = {}
    ordered: List[str] = []
    for raw in items:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen[value] = None
        ordered.append(value)
    return ordered


def _extract_model_name(value: Any) -> str:
    for attr in ("id", "name", "model", "display_name"):
        candidate = getattr(value, attr, None)
        if isinstance(candidate, str) and candidate.strip():
            text = candidate.strip()
            if text.startswith("models/"):
                return text.split("/", 1)[1]
            return text
    if isinstance(value, dict):
        for key in ("id", "name", "model", "display_name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                text = candidate.strip()
                if text.startswith("models/"):
                    return text.split("/", 1)[1]
                return text
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("models/"):
            return text.split("/", 1)[1]
        return text
    return ""


def _model_supports_runtime_generation(provider: str, value: Any) -> bool:
    if provider == "gemini":
        methods = getattr(value, "supported_actions", None)
        if methods is None:
            methods = getattr(value, "supported_generation_methods", None)
        if isinstance(methods, list):
            normalized = {str(item).strip() for item in methods if str(item).strip()}
            if normalized and "generateContent" not in normalized:
                return False
    return True


def _looks_like_supported_runtime_model(provider: str, model_name: str) -> bool:
    normalized = str(model_name or "").strip().lower()
    if not normalized:
        return False
    if provider == "openai":
        allowed_prefix = normalized.startswith(("gpt-", "o1", "o3", "o4", "chatgpt-"))
        blocked_terms = ("image", "realtime", "tts", "transcribe", "embedding", "moderation")
        return allowed_prefix and not any(term in normalized for term in blocked_terms)
    if provider == "anthropic":
        return normalized.startswith("claude-")
    if provider == "gemini":
        if not normalized.startswith("gemini-"):
            return False
        blocked_terms = ("embedding", "image", "veo", "lyria", "robotics", "tts", "live", "audio", "computer-use", "deep-research")
        return not any(term in normalized for term in blocked_terms)
    if provider == "local":
        return True
    if provider in {"ollama", "ollama-local", "ollama-runpod"}:
        return True
    return False


def _merge_live_models(provider: str, live_models: List[str]) -> None:
    if provider not in RUNTIME_PROVIDER_CATALOG or not live_models:
        return
    fallback_models = [str(item) for item in RUNTIME_PROVIDER_CATALOG[provider].get("models", []) if str(item).strip()]
    RUNTIME_PROVIDER_CATALOG[provider]["models"] = _dedupe_preserve_order(list(live_models) + fallback_models)


def _refresh_openai_models() -> List[str]:
    api_key = str(os.environ.get("OPENAI_API_KEY", "")).strip()
    if not api_key:
        return []
    OpenAI = getattr(importlib.import_module("openai"), "OpenAI")
    client = OpenAI(api_key=api_key, timeout=5.0, max_retries=0)
    return _dedupe_preserve_order(
        _extract_model_name(item)
        for item in client.models.list()
        if _looks_like_supported_runtime_model("openai", _extract_model_name(item))
    )


def _refresh_anthropic_models() -> List[str]:
    api_key = str(os.environ.get("ANTHROPIC_API_KEY", "")).strip()
    if not api_key:
        return []
    Anthropic = getattr(importlib.import_module("anthropic"), "Anthropic")
    client = Anthropic(api_key=api_key, timeout=5.0, max_retries=0)
    result = client.models.list()
    items = result.data if hasattr(result, "data") else result
    return _dedupe_preserve_order(
        _extract_model_name(item)
        for item in items
        if _looks_like_supported_runtime_model("anthropic", _extract_model_name(item))
    )


def _refresh_gemini_models() -> List[str]:
    api_key = str(os.environ.get("GEMINI_API_KEY", "")).strip()
    if not api_key:
        return []
    genai = importlib.import_module("google.genai")
    client = genai.Client(api_key=api_key, http_options={"timeout": 5000})
    pager = client.models.list(config={"page_size": 100})
    models: List[str] = []
    for item in pager:
        model_name = _extract_model_name(item)
        if not _looks_like_supported_runtime_model("gemini", model_name):
            continue
        if not _model_supports_runtime_generation("gemini", item):
            continue
        models.append(model_name)
    return _dedupe_preserve_order(models)


def _refresh_openai_compatible_models(base_url: str, api_key: str, provider_key: str) -> List[str]:
    try:
        OpenAI = getattr(importlib.import_module("openai"), "OpenAI")
    except Exception:
        return []
    try:
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=5.0, max_retries=0)
        return _dedupe_preserve_order(
            _extract_model_name(item)
            for item in client.models.list()
            if _looks_like_supported_runtime_model(provider_key, _extract_model_name(item))
        )
    except Exception:
        return []


def _refresh_ollama_local_models() -> List[str]:
    base_url = str(os.environ.get("OLLAMA_LOCAL_BASE_URL") or os.environ.get("OLLAMA_BASE_URL") or "http://127.0.0.1:11434/v1").strip()
    api_key = str(os.environ.get("OLLAMA_LOCAL_API_KEY") or os.environ.get("OLLAMA_API_KEY") or "ollama-key").strip()
    return _refresh_openai_compatible_models(base_url, api_key, "ollama-local")


def refresh_runtime_provider_catalog_once() -> Dict[str, Any]:
    global _RUNTIME_CATALOG_REFRESH_DONE
    with _RUNTIME_CATALOG_REFRESH_LOCK:
        if _RUNTIME_CATALOG_REFRESH_DONE:
            return {"refreshed": False, "providers": {}}
        providers: Dict[str, Dict[str, Any]] = {}
        for key in BASE_RUNTIME_PROVIDER_CATALOG:
            RUNTIME_PROVIDER_CATALOG[key] = deepcopy(BASE_RUNTIME_PROVIDER_CATALOG[key])
        refreshers = {
            "openai": _refresh_openai_models,
            "anthropic": _refresh_anthropic_models,
            "gemini": _refresh_gemini_models,
            "ollama": _refresh_ollama_local_models,
            "ollama-local": _refresh_ollama_local_models,
        }
        for provider, refresher in refreshers.items():
            try:
                models = refresher()
                if models:
                    _merge_live_models(provider, models)
                    providers[provider] = {"live": True, "count": len(models)}
                else:
                    providers[provider] = {"live": False, "count": 0}
            except Exception as exc:
                providers[provider] = {"live": False, "count": 0, "error": str(exc)}
        _RUNTIME_CATALOG_REFRESH_DONE = True
        return {"refreshed": True, "providers": providers}


def supported_provider_keys() -> List[str]:
    return list(RUNTIME_PROVIDER_CATALOG.keys())


def normalize_provider(provider: Optional[str]) -> str:
    key = str(provider or "").strip().lower()
    if key == "ollama":
        return "ollama-local"
    if key not in RUNTIME_PROVIDER_CATALOG:
        raise ValueError(
            "Unknown provider '"
            + str(provider or "")
            + "'. Available providers: "
            + ", ".join(supported_provider_keys())
        )
    return key


def validate_provider_model_selection(provider: Optional[str], model: Optional[str]) -> None:
    key = normalize_provider(provider)
    normalized_model = str(model or "").strip().lower()
    if not normalized_model:
        raise ValueError("Model cannot be empty.")

    provider_named_model = normalized_model in RUNTIME_PROVIDER_CATALOG
    known_provider_prefixes = {
        "openai": ("gpt-", "o1", "o3", "o4", "chatgpt-"),
        "anthropic": ("claude-",),
        "gemini": ("gemini-",),
        "local": tuple(),
        "ollama": tuple(),
        "ollama-local": tuple(),
        "ollama-runpod": tuple(),
    }
    obvious_mismatch_prefixes = {
        provider_name
        for provider_name, prefixes in known_provider_prefixes.items()
        if provider_name != key and any(normalized_model.startswith(prefix) for prefix in prefixes)
    }

    if provider_named_model and normalized_model != key:
        raise ValueError(
            "Model '"
            + str(model)
            + "' looks like a provider name, not a model name for provider '"
            + key
            + "'. Use /models"
            + (" " + key if key else "")
            + " to see suggested model names."
        )

    if obvious_mismatch_prefixes:
        mismatch = sorted(obvious_mismatch_prefixes)[0]
        raise ValueError(
            "Model '"
            + str(model)
            + "' does not look compatible with provider '"
            + key
            + "'"
            + "; it looks like a "
            + mismatch
            + " model. Use /models "
            + key
            + " to see suggested names."
        )


def runtime_options_payload(
    *,
    current_provider: Optional[str] = None,
    current_model: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_current_provider = str(current_provider or "").strip().lower()
    normalized_current_model = str(current_model or "").strip()
    providers: List[Dict[str, Any]] = []
    for key, info in RUNTIME_PROVIDER_CATALOG.items():
        suggested_models = [str(item) for item in info.get("models", []) if str(item).strip()]
        providers.append(
            {
                "key": key,
                "label": info.get("label", key),
                "package": info.get("package"),
                "env_var": info.get("env_var"),
                "default_model": info.get("default_model"),
                "suggested_models": suggested_models,
                "notes": info.get("notes", ""),
                "active": key == normalized_current_provider,
                "active_model": normalized_current_model if key == normalized_current_provider else "",
                "accepts_custom_model": True,
                "hidden": bool(info.get("hidden", False)),
            }
        )
    return {
        "providers": providers,
        "provider_keys": supported_provider_keys(),
        "current_provider": normalized_current_provider,
        "current_model": normalized_current_model,
    }


def runtime_provider_lines(current_provider: Optional[str] = None) -> List[str]:
    active = str(current_provider or "").strip().lower()
    lines: List[str] = []
    for key, info in RUNTIME_PROVIDER_CATALOG.items():
        if bool(info.get("hidden", False)):
            continue
        label = str(info.get("label", key)).strip() or key
        parts = [key]
        if key == active:
            parts.append("[active]")
        parts.append("- " + label)
        env_var = info.get("env_var")
        package = info.get("package")
        if env_var:
            parts.append("- env " + str(env_var))
        if package:
            parts.append("- package " + str(package))
        lines.append(" ".join(parts))
    lines.append("")
    lines.append("Use /models <provider> to see suggested models.")
    lines.append("Custom model strings are still allowed even if not listed here.")
    return lines


def runtime_model_lines(provider: Optional[str], current_model: Optional[str] = None) -> List[str]:
    key = normalize_provider(provider)
    info = RUNTIME_PROVIDER_CATALOG[key]
    active_model = str(current_model or "").strip()
    lines = [
        "provider : " + key,
        "default  : " + str(info.get("default_model", "")),
    ]
    env_var = info.get("env_var")
    package = info.get("package")
    if env_var:
        lines.append("env     : " + str(env_var))
    if package:
        lines.append("package : " + str(package))
    lines.append("")
    lines.append("Suggested models:")
    for model in info.get("models", []):
        marker = " [active]" if active_model and str(model) == active_model else ""
        lines.append("- " + str(model) + marker)
    notes = str(info.get("notes", "")).strip()
    if notes:
        lines.append("")
        lines.append("Notes:")
        lines.append("- " + notes)
    lines.append("- Custom model strings are allowed.")
    return lines