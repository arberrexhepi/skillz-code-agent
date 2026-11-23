"""User-owned, scoped MCP settings shared by the CLI and desktop host."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
PLAYWRIGHT_SERVER = "playwright"
PLAYWRIGHT_PACKAGE = "@playwright/mcp@0.0.82"


def playwright_server(*, enabled: bool) -> dict:
    return {
        "transport": "stdio",
        "command": "npx",
        "args": ["--yes", PLAYWRIGHT_PACKAGE, "--headless", "--isolated"],
        "enabled": enabled,
        "timeout": 120,
    }


def is_builtin_playwright(name: str, server: dict) -> bool:
    default = playwright_server(enabled=True)
    return name == PLAYWRIGHT_SERVER and all(server.get(key) == default[key] for key in ("transport", "command", "args"))


def settings_home() -> Path:
    return Path(os.environ.get("SKILLZ_MCP_HOME") or Path.home() / ".skillz" / "mcp").expanduser()


def profile_path(root: Path, scope: str) -> Path:
    if scope == "cli":
        name = "cli"
    elif scope == "repo":
        name = "repo-" + hashlib.sha256(os.path.normcase(str(root.resolve())).encode()).hexdigest()
    elif scope.startswith("artifact:") and NAME.fullmatch(scope.removeprefix("artifact:")):
        name = "artifact-" + scope.removeprefix("artifact:")
    else:
        raise ValueError("MCP scope must be cli, repo, or artifact:<id>.")
    return settings_home() / "profiles" / f"{name}.json"


def read_json(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Invalid MCP settings file.")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 4096 or any(ord(char) < 32 for char in value):
        raise ValueError(f"Invalid {label}.")
    return value


def _strings(value, label: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 200 or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{label} must be an array of strings.")
    return value


def _validate_url(value, *, https_only=False) -> str:
    text = _text(value, "server URL")
    url = urlsplit(text)
    loopback = url.hostname in {"localhost", "127.0.0.1", "::1"}
    if (not url.hostname or url.username or url.password or url.fragment
            or not (url.scheme == "https" or (not https_only and url.scheme == "http" and loopback))):
        raise ValueError("MCP HTTP URLs must use HTTPS or loopback HTTP and omit credentials and fragments.")
    if url.port is not None and not 1 <= url.port <= 65535:
        raise ValueError("Invalid MCP server port.")
    return text


def validate_server(name: str, value: dict) -> dict:
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError("MCP server names may contain letters, numbers, dots, hyphens, and underscores.")
    if not isinstance(value, dict):
        raise ValueError("MCP server settings must be an object.")
    transport = value.get("transport")
    allowed = {"transport", "enabled", "allowedTools", "timeout"}
    if transport == "stdio":
        allowed |= {"command", "args", "env"}
        _text(value.get("command"), "stdio command")
        for arg in _strings(value.get("args", []), "stdio args"):
            if len(arg) > 4096 or "\x00" in arg:
                raise ValueError("Invalid stdio argument.")
        for key in _strings(value.get("env", []), "stdio env"):
            if not ENV_NAME.fullmatch(key):
                raise ValueError("stdio env must name host environment variables.")
    elif transport == "http":
        allowed |= {"url", "auth", "bearerEnv", "oauth"}
        _validate_url(value.get("url"))
        auth = value.get("auth", "none")
        if auth not in {"none", "bearer", "oauth"}:
            raise ValueError("MCP HTTP auth must be none, bearer or oauth.")
        if auth == "bearer":
            var = value.get("bearerEnv")
            if not isinstance(var, str) or not ENV_NAME.fullmatch(var):
                raise ValueError("bearerEnv must name a host environment variable.")
        if ("oauth" in value and auth != "oauth") or ("bearerEnv" in value and auth != "bearer"):
            raise ValueError("MCP credential options must match the selected authentication.")
        oauth = value.get("oauth", {})
        if not isinstance(oauth, dict) or set(oauth) - {
            "clientId", "clientMetadataUrl", "scope", "callbackPort", "clientSecretEnv", "tokenEndpointAuthMethod",
        }:
            raise ValueError("Invalid MCP OAuth options.")
        for key in ("clientId", "scope", "clientSecretEnv"):
            if key in oauth:
                _text(oauth[key], f"OAuth {key}")
        if "clientSecretEnv" in oauth and (not oauth.get("clientId") or not ENV_NAME.fullmatch(oauth["clientSecretEnv"])):
            raise ValueError("OAuth clientSecretEnv requires a client ID and a host environment variable name.")
        if "tokenEndpointAuthMethod" in oauth and (
                "clientSecretEnv" not in oauth or oauth["tokenEndpointAuthMethod"] not in ["client_secret_basic", "client_secret_post"]):
            raise ValueError("OAuth tokenEndpointAuthMethod requires clientSecretEnv and must be client_secret_basic or client_secret_post.")
        if "clientMetadataUrl" in oauth:
            _validate_url(oauth["clientMetadataUrl"], https_only=True)
            if "clientId" in oauth:
                raise ValueError("Choose either an OAuth client ID or a client metadata URL.")
        port = oauth.get("callbackPort", 8765)
        if type(port) is not int or not 1024 <= port <= 65535:
            raise ValueError("OAuth callbackPort must be between 1024 and 65535.")
    else:
        raise ValueError("MCP transport must be stdio or http.")
    if set(value) - allowed:
        raise ValueError(f"Unknown MCP settings: {', '.join(sorted(set(value) - allowed))}.")
    if type(value.get("enabled", True)) is not bool:
        raise ValueError("enabled must be a boolean.")
    timeout = value.get("timeout", 60)
    if type(timeout) is not int or not 5 <= timeout <= 300:
        raise ValueError("MCP timeout must be an integer from 5 to 300 seconds.")
    for tool in _strings(value.get("allowedTools", []), "allowedTools"):
        _text(tool, "tool allowlist pattern")
    return dict(value)


class McpSettings:
    def __init__(self, root: Path, scope: str):
        self.root = root.resolve()
        self.scope = scope

    def _profiles(self) -> tuple[str, ...]:
        return ("cli", "repo") if self.scope == "local" else (self.scope,)

    def _saved_servers(self, scope: str) -> dict:
        profile = read_json(profile_path(self.root, scope))
        servers = profile.get("servers", {})
        if not isinstance(servers, dict) or profile.get("version", 1) != 1:
            raise ValueError("Invalid MCP profile version or server list.")
        return {name: validate_server(name, value) for name, value in servers.items()}

    def servers(self) -> dict[str, tuple[str, dict]]:
        profiles = self._profiles()
        result = {PLAYWRIGHT_SERVER: (profiles[0], playwright_server(enabled=not self.scope.startswith("artifact:")))}
        for scope in profiles:
            result.update({name: (scope, value) for name, value in self._saved_servers(scope).items()})
        return result

    def save(self, name: str, config: dict | None) -> None:
        path = profile_path(self.root, self.scope)
        servers = self._saved_servers(self.scope)
        if config is None:
            if name == PLAYWRIGHT_SERVER and not self.scope.startswith("artifact:"):
                servers[name] = playwright_server(enabled=False)
            else:
                servers.pop(name, None)
        else:
            servers[name] = validate_server(name, config)
        write_json(path, {"version": 1, "servers": servers})

    def credential_path(self, scope: str, name: str, server: dict) -> Path:
        identity = json.dumps([profile_path(self.root, scope).stem, name, server["url"], server.get("oauth", {})],
                              sort_keys=True)
        return settings_home() / "credentials" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
