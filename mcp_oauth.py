"""Interactive loopback OAuth and private credential storage for MCP hosts."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
import threading
import time
from urllib.parse import parse_qs, urlsplit

from mcp_settings import read_json, write_json


class OAuthRequired(RuntimeError):
    pass


class OAuthStorage:
    def __init__(self, path, metadata, options):
        self.path = path
        self.metadata = metadata
        self.options = options
        self.provider = None

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken

        tokens = read_json(self.path).get("tokens")
        try:
            return OAuthToken.model_validate(tokens) if tokens else None
        except ValueError as exc:
            raise OAuthRequired("Stored MCP credentials are invalid. Sign out and sign in again.") from exc

    async def set_tokens(self, tokens):
        state = read_json(self.path)
        context = self.provider.context
        prior_refresh = state.get("tokens", {}).get("refresh_token")
        if not tokens.refresh_token and prior_refresh:
            # OAuth refresh responses can omit a replacement refresh token.
            tokens = tokens.model_copy(update={"refresh_token": prior_refresh})
            context.current_tokens = tokens
        write_json(self.path, {**state, "tokens": tokens.model_dump(mode="json"),
                               "expires_at": time.time() + tokens.expires_in if tokens.expires_in is not None else None,
                               "authority": context.oauth_metadata.model_dump(mode="json") if context.oauth_metadata else None,
                               "resource": context.protected_resource_metadata.model_dump(mode="json") if context.protected_resource_metadata else None})

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull

        if client_id := self.options.get("clientId"):
            secret_var = self.options.get("clientSecretEnv")
            secret = os.environ.get(secret_var, "") if secret_var else None
            if secret_var and not secret:
                raise OAuthRequired(f"Set the host environment variable {secret_var} for MCP OAuth.")
            return OAuthClientInformationFull(**self.metadata.model_dump(
                exclude={"token_endpoint_auth_method"}), client_id=client_id, client_secret=secret,
                token_endpoint_auth_method=self.options.get("tokenEndpointAuthMethod", "client_secret_post") if secret else "none")
        client = read_json(self.path).get("client")
        try:
            return OAuthClientInformationFull.model_validate(client) if client else None
        except ValueError as exc:
            raise OAuthRequired("Stored MCP registration is invalid. Sign out and sign in again.") from exc

    async def set_client_info(self, client):
        write_json(self.path, {**read_json(self.path), "client": client.model_dump(mode="json")})


@asynccontextmanager
async def oauth_provider(name, server, credential_path, open_browser):
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata, OAuthMetadata, ProtectedResourceMetadata

    class PersistentProvider(OAuthClientProvider):
        async def _perform_authorization(self):
            if "scope" in options:
                self.context.client_metadata.scope = options["scope"]
            return await super()._perform_authorization()

        async def _initialize(self):
            # The v1 provider stores tokens but does not restore absolute
            # expiry or metadata for refreshes in a new process.
            await super()._initialize()
            state = read_json(credential_path)
            if isinstance(state.get("expires_at"), (int, float)):
                self.context.token_expiry_time = state["expires_at"]
            try:
                if state.get("authority"):
                    self.context.oauth_metadata = OAuthMetadata.model_validate(state["authority"])
                if state.get("resource"):
                    self.context.protected_resource_metadata = ProtectedResourceMetadata.model_validate(state["resource"])
            except ValueError as exc:
                raise OAuthRequired("Stored MCP authorization metadata is invalid. Sign in again.") from exc

    options = server.get("oauth", {})
    port = options.get("callbackPort", 8765)
    metadata = OAuthClientMetadata(client_name="Skillz", redirect_uris=[f"http://127.0.0.1:{port}/callback"],
                                   token_endpoint_auth_method="none", scope=options.get("scope"))
    storage = OAuthStorage(credential_path, metadata, options)
    loop = asyncio.get_running_loop()
    result = loop.create_future()
    listener = None
    thread = None

    async def redirect(url):
        nonlocal listener, thread
        if open_browser is None:
            raise OAuthRequired(f"Sign in to MCP server {name} from MCP settings or the Skillz MCP CLI.")
        parsed = urlsplit(url)
        if (not parsed.hostname or parsed.username or parsed.password
                or (parsed.scheme != "https" and not (parsed.scheme == "http"
                    and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise OAuthRequired("MCP authorization server did not supply a secure browser URL.")
        expected_state = parse_qs(parsed.query).get("state", [""])[0]

        class Callback(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                callback = urlsplit(self.path)
                params = parse_qs(callback.query)
                state = params.get("state", [""])[0]
                if callback.path != "/callback" or not expected_state or state != expected_state:
                    self.send_error(400, "Invalid Skillz OAuth callback.")
                    return
                code = params.get("code", [""])[0]

                def finish():
                    if not result.done():
                        if code:
                            result.set_result((code, state))
                        else:
                            result.set_exception(OAuthRequired("MCP authorization was declined or failed."))

                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b"You can return to Skillz.")
                loop.call_soon_threadsafe(finish)

        try:
            listener = ThreadingHTTPServer(("127.0.0.1", port), Callback)
        except OSError as exc:
            raise OAuthRequired(f"Skillz OAuth callback port {port} is unavailable.") from exc
        thread = threading.Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        await asyncio.to_thread(open_browser, url)

    async def callback():
        return await asyncio.wait_for(result, timeout=240)

    try:
        provider = PersistentProvider(server["url"], metadata, storage, redirect_handler=redirect,
                                      callback_handler=callback, client_metadata_url=options.get("clientMetadataUrl"))
        storage.provider = provider
        yield provider
    finally:
        if listener is not None:
            await asyncio.to_thread(listener.shutdown)
            listener.server_close()
            if thread is not None:
                await asyncio.to_thread(thread.join)
