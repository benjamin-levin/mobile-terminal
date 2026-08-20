from __future__ import annotations

import asyncio
import gzip
import hashlib
import inspect
import json
import mimetypes
import os
import re
import secrets
import signal
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from mobile_terminal_config import ProfileConfig, ProxyConfig
from proxy_auth import AuthenticationRequest, Authenticator, load_authenticator
from webauthn_auth import (
    PasskeyAuth,
    PasskeyChallengeError,
    PasskeyStoreError,
    PasskeyVerificationError,
    resolve_auth_method,
)


WS_PATH = "/_ws"
INTERNAL_TOKEN_HEADER = "X-Mobile-Terminal-Internal-Token"
PRINCIPAL_HEADER = "X-Mobile-Terminal-Principal"


class BackendAuthenticationError(Exception):
    pass


class PasskeyAuthenticationError(Exception):
    pass


def _remote_host(remote_address: Any) -> str | None:
    if isinstance(remote_address, tuple) and remote_address:
        return str(remote_address[0])
    if isinstance(remote_address, str):
        return remote_address
    return None


def _is_loopback(remote_address: Any) -> bool:
    host = _remote_host(remote_address)
    return host in ("127.0.0.1", "::1") or bool(host and host.startswith("127."))


def _origin_matches_host(headers: Any) -> bool:
    origin = headers.get("Origin")
    host = headers.get("Host")
    if not origin or not host:
        return False
    try:
        parsed_origin = urlsplit(origin)
        parsed_host = urlsplit(f"//{host}")
        origin_port = parsed_origin.port or (443 if parsed_origin.scheme == "https" else 80)
        host_port = parsed_host.port or (443 if parsed_origin.scheme == "https" else 80)
    except ValueError:
        return False
    return bool(
        parsed_origin.scheme in ("http", "https")
        and parsed_origin.hostname
        and parsed_origin.hostname.lower() == (parsed_host.hostname or "").lower()
        and origin_port == host_port
        and not parsed_origin.username
        and not parsed_origin.password
        and parsed_origin.path in ("", "/")
        and not parsed_origin.query
        and not parsed_origin.fragment
    )


def _http_response(
    status: int,
    body: bytes,
    content_type: str,
    extra_headers: dict[str, str] | None = None,
) -> Response:
    fields = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Cache-Control": "no-cache",
    }
    if extra_headers:
        fields.update(extra_headers)
    reason = {
        200: "OK",
        304: "Not Modified",
        400: "Bad Request",
        401: "Unauthorized",
        403: "Forbidden",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
        503: "Service Unavailable",
    }.get(status, "OK")
    return Response(status, reason, Headers(fields), body)


class ProxyServer:
    def __init__(
        self,
        config: ProxyConfig,
        *,
        static_root: Path,
        node_modules_root: Path,
        render_icon: Callable[[str, int], bytes],
        authenticate: Authenticator | None = None,
        passkeys: PasskeyAuth | None = None,
    ) -> None:
        self.config = config
        self.static_root = static_root.resolve()
        self.node_modules_root = node_modules_root.resolve()
        self.render_icon = render_icon
        configured_auth = os.environ.get("MOBILE_TERMINAL_AUTHENTICATOR", "").strip()
        self.authenticate = authenticate or load_authenticator(config.auth_realms, configured_auth)
        self.passkeys = passkeys
        if self.passkeys is None and config.rp_id and config.expected_origin:
            self.passkeys = PasskeyAuth(
                config.state_dir,
                rp_id=config.rp_id,
                rp_name=f"{config.label} terminal",
                expected_origin=config.expected_origin,
            )
        self.profile_status = {profile.id: profile.status for profile in config.profiles}
        self.profile_messages = {profile.id: profile.status_message for profile in config.profiles}
        self.started_at = ""
        self._inlined_index: tuple[bytes, bytes, str] | None = None

    def load_settings(self) -> dict[str, Any] | None:
        path = self.config.state_dir / "settings.json"
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def save_settings(self, settings: Any) -> dict[str, Any]:
        payload = settings if isinstance(settings, dict) else {}
        path = self.config.state_dir / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        return payload

    async def send_settings(
        self,
        connection: ServerConnection,
        settings: Any = None,
        *,
        persisted: bool = False,
    ) -> None:
        current = self.load_settings()
        if current is None and isinstance(settings, dict):
            if persisted:
                current = self.save_settings(settings)
            else:
                await self.send_json(
                    connection,
                    {"type": "settings", "settings": settings, "persisted": False},
                )
                return
        if current is not None:
            await self.send_json(
                connection,
                {"type": "settings", "settings": current, "persisted": True},
            )

    def public_profiles(self) -> list[dict[str, Any]]:
        return [
            {
                "id": profile.id,
                "label": profile.label,
                "accent": profile.accent,
                "authRealm": profile.auth_realm,
                "requireToken": bool(
                    self.config.auth_realms[profile.auth_realm].token
                    and not self.config.auth_realms[profile.auth_realm].trust_identity
                ),
                "deviceKeyAuth": bool(
                    self.passkeys
                    and self.config.auth_realms[profile.auth_realm].device_key_auth
                ),
                "available": profile.available and self.profile_status.get(profile.id) != "down",
                "status": self.profile_status.get(profile.id, profile.status),
                "statusMessage": self.profile_messages.get(profile.id, profile.status_message),
            }
            for profile in self.config.profiles
        ]

    def config_payload(self) -> dict[str, Any]:
        active = self.config.profile(self.config.active_profile)
        realm = self.config.auth_realms[active.auth_realm] if active else None
        return {
            "requireToken": bool(realm and realm.token and not realm.trust_identity),
            "tailscaleMode": True,
            "allowedClients": ["127.0.0.1", "::1"],
            "multiTenant": False,
            "profileMode": True,
            "profiles": self.public_profiles(),
            "activeProfile": self.config.active_profile,
            "label": self.config.label,
            "deviceKeyAuth": bool(self.passkeys and realm and realm.device_key_auth),
        }

    async def send_json(self, connection: ServerConnection, payload: dict[str, Any]) -> None:
        await connection.send(json.dumps(payload))

    def _identity_principal(self, connection: ServerConnection, profile: ProfileConfig) -> str | None:
        realm = self.config.auth_realms[profile.auth_realm]
        if (
            not realm.trust_identity
            or not _is_loopback(connection.remote_address)
            or not _origin_matches_host(connection.request.headers)
        ):
            return None
        if connection.request.headers.get("Tailscale-Funnel-Request") is not None:
            return None
        login = connection.request.headers.get("Tailscale-User-Login")
        return login.strip().lower() if login else None

    async def _authenticate_credentials(
        self,
        connection: ServerConnection,
        profile: ProfileConfig,
        credentials: dict[str, Any],
    ) -> str | None:
        request = AuthenticationRequest(
            realm=profile.auth_realm,
            credentials=credentials,
            headers=connection.request.headers,
            remote_address=connection.remote_address,
        )
        try:
            principal = self.authenticate(request)
            if inspect.isawaitable(principal):
                principal = await principal
        except Exception:
            return None
        return principal if isinstance(principal, str) and principal else None

    async def _receive_auth_message(
        self,
        connection: ServerConnection,
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        try:
            raw = await asyncio.wait_for(connection.recv(), timeout=timeout)
            if not isinstance(raw, str):
                return None
            payload = json.loads(raw)
        except (ConnectionClosed, TimeoutError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _passkey_failure(operation: str, exc: Exception) -> PasskeyAuthenticationError:
        print(f"passkey {operation} failed: {type(exc).__name__}")
        return PasskeyAuthenticationError("Passkey verification failed.")

    async def authenticate_realm(
        self,
        connection: ServerConnection,
        profile: ProfileConfig,
        *,
        binding: str = "",
    ) -> str | None:
        identity = self._identity_principal(connection, profile)
        if identity is not None:
            return resolve_auth_method(identity_principal=identity).require_terminal_authorization()

        realm = self.config.auth_realms[profile.auth_realm]
        passkey_enabled = bool(self.passkeys and realm.device_key_auth)
        if not passkey_enabled:
            await self.send_json(
                connection,
                {
                    "type": "auth-challenge",
                    "nonce": "",
                    "realm": profile.auth_realm,
                    "profile": profile.id,
                },
            )
            credentials = await self._receive_auth_message(connection, timeout=20)
            if credentials is None or credentials.get("type") != "auth":
                return None
            return await self._authenticate_credentials(connection, profile, credentials)

        assert self.passkeys is not None
        binding = binding or secrets.token_urlsafe(24)
        try:
            credentials = self.passkeys.list_credentials(profile.auth_realm)
            if credentials:
                passkey_principal = credentials[0].get("principal")
                if not isinstance(passkey_principal, str) or not passkey_principal:
                    raise PasskeyStoreError("Passkey principal is invalid.")
                message = self.passkeys.begin_authentication(
                    profile.auth_realm,
                    principal=passkey_principal,
                    binding=binding,
                )
            else:
                message = {
                    "type": "auth-challenge",
                    "nonce": "",
                }
        except (PasskeyChallengeError, PasskeyVerificationError, PasskeyStoreError, ValueError) as exc:
            raise self._passkey_failure("setup", exc) from None
        message.update({"realm": profile.auth_realm, "profile": profile.id})
        await self.send_json(connection, message)

        bootstrap_principal: str | None = None
        while True:
            payload = await self._receive_auth_message(connection, timeout=120)
            if payload is None:
                return None
            message_type = payload.get("type")
            if message_type == "webauthn-auth":
                try:
                    record = self.passkeys.finish_authentication(
                        profile.auth_realm,
                        payload.get("challengeId", ""),
                        payload.get("assertion"),
                        binding=binding,
                    )
                    passkey_principal = record.get("principal")
                    if not isinstance(passkey_principal, str) or not passkey_principal:
                        raise PasskeyVerificationError("Passkey principal is invalid.")
                except (PasskeyChallengeError, PasskeyVerificationError, PasskeyStoreError, ValueError) as exc:
                    raise self._passkey_failure("authentication", exc) from None
                return resolve_auth_method(
                    passkey_principal=passkey_principal
                ).require_terminal_authorization()
            if message_type == "auth":
                token_principal = await self._authenticate_credentials(connection, profile, payload)
                if token_principal is None:
                    return None
                if bootstrap_principal is not None and token_principal != bootstrap_principal:
                    return None
                resolution = resolve_auth_method(token_principal=token_principal)
                bootstrap_principal = resolution.principal
                try:
                    message = self.passkeys.begin_registration(
                        profile.auth_realm,
                        principal=bootstrap_principal,
                        user_name=self.config.label,
                        user_display_name=self.config.label,
                        label=connection.request.headers.get("User-Agent", "device"),
                        binding=binding,
                    )
                except (PasskeyChallengeError, PasskeyVerificationError, PasskeyStoreError, ValueError) as exc:
                    raise self._passkey_failure("registration setup", exc) from None
                message.update({"realm": profile.auth_realm, "profile": profile.id})
                await self.send_json(connection, message)
                continue
            if message_type == "webauthn-register" and bootstrap_principal is not None:
                try:
                    record = self.passkeys.finish_registration(
                        profile.auth_realm,
                        payload.get("challengeId", ""),
                        payload.get("attestation"),
                        binding=binding,
                    )
                    passkey_principal = record.get("principal")
                    if passkey_principal != bootstrap_principal:
                        raise PasskeyVerificationError("Passkey principal is invalid.")
                except (PasskeyChallengeError, PasskeyVerificationError, PasskeyStoreError, ValueError) as exc:
                    raise self._passkey_failure("registration", exc) from None
                return resolve_auth_method(
                    passkey_principal=passkey_principal
                ).require_terminal_authorization()
            return None

    def _safe_static_target(self, request_path: str) -> tuple[Path | None, str | None]:
        clean_path = urlsplit(request_path).path
        if clean_path == "/":
            clean_path = "/index.html"
        if clean_path.startswith("/static/"):
            root = self.static_root
            relative = clean_path.removeprefix("/static/")
        elif clean_path.startswith("/vendor/"):
            root = self.node_modules_root
            relative = clean_path.removeprefix("/vendor/")
        else:
            root = self.static_root
            relative = clean_path.removeprefix("/")
        candidate = (root / relative).resolve()
        try:
            if os.path.commonpath((str(root), str(candidate))) != str(root):
                return None, None
        except ValueError:
            return None, None
        content_type, _ = mimetypes.guess_type(candidate.name)
        return candidate, content_type

    def _static_response(self, target: Path, content_type: str | None, request: Request) -> Response:
        try:
            stat = target.stat()
        except OSError:
            return _http_response(404, b"Not Found", "text/plain; charset=utf-8")
        content_type = content_type or "application/octet-stream"
        etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
        is_vendor = urlsplit(request.path).path.startswith("/vendor/")
        cache_control = "public, max-age=86400" if is_vendor else "no-cache"
        if etag in request.headers.get("If-None-Match", ""):
            return _http_response(304, b"", content_type, {"ETag": etag, "Cache-Control": cache_control})
        if request.headers.get(":method") == "HEAD":
            return _http_response(200, b"", content_type, {"ETag": etag, "Cache-Control": cache_control})
        body = target.read_bytes()
        headers = {"ETag": etag, "Cache-Control": cache_control}
        compressible = content_type.startswith("text/") or content_type.startswith(
            ("application/javascript", "application/json", "application/manifest", "image/svg")
        )
        if "gzip" in request.headers.get("Accept-Encoding", "") and len(body) > 1024 and compressible:
            body = gzip.compress(body, 6)
            headers.update({"Content-Encoding": "gzip", "Vary": "Accept-Encoding"})
        return _http_response(200, body, content_type, headers)

    def _inlined_index_response(self, request: Request) -> Response:
        if self._inlined_index is None:
            raw = (self.static_root / "index.html").read_text()
            raw = raw.replace(
                '''          var active = "";
          try { active = localStorage.getItem("mobile-terminal.active-session") || ""; } catch (e) {}
          var proto = location.protocol === "https:" ? "wss:" : "ws:";
          var url = proto + "//" + location.host + "/_ws" + (active ? "?session=" + encodeURIComponent(active) : "");
''',
                '''          var active = "";
          var profile = "";
          try {
            profile = localStorage.getItem("mobile-terminal.active-profile") || "";
            active = profile
              ? localStorage.getItem("mobile-terminal.profile." + profile + ".active-session") || ""
              : localStorage.getItem("mobile-terminal.active-session") || "";
          } catch (e) {}
          var proto = location.protocol === "https:" ? "wss:" : "ws:";
          var query = [];
          if (profile) query.push("profile=" + encodeURIComponent(profile));
          if (active) query.push("session=" + encodeURIComponent(active));
          var url = proto + "//" + location.host + "/_ws" + (query.length ? "?" + query.join("&") : "");
''',
            )
            raw = raw.replace(
                '<script defer src="/static/app.js"></script>',
                '<script defer src="/static/passkey.js"></script>\n'
                '    <script defer src="/static/app.js"></script>',
            )
            mtimes = [(self.static_root / "index.html").stat().st_mtime_ns]

            def read_asset(href: str) -> str | None:
                target, _ = self._safe_static_target(href)
                if target is None or not target.is_file():
                    return None
                mtimes.append(target.stat().st_mtime_ns)
                return target.read_text()

            def css_sub(match: re.Match[str]) -> str:
                content = read_asset(match.group(1))
                return match.group(0) if content is None else "<style>" + content.replace("</style", "<\\/style") + "</style>"

            def js_sub(match: re.Match[str]) -> str:
                content = read_asset(match.group(1))
                return match.group(0) if content is None else "<script>" + content.replace("</script", "<\\/script") + "</script>"

            html = re.sub(r'<link rel="stylesheet" href="([^"]+)">', css_sub, raw)
            html = re.sub(r'<script defer src="([^"]+)"></script>', js_sub, html)
            html = html.replace("__MT_LABEL__", self.config.label)
            body = html.encode("utf-8")
            etag = f'"idx-{(sum(mtimes) & 0xFFFFFFFFFFFF):x}-{len(body):x}"'
            self._inlined_index = body, gzip.compress(body, 6), etag
        body, gzipped, etag = self._inlined_index
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if etag in request.headers.get("If-None-Match", ""):
            return _http_response(304, b"", "text/html; charset=utf-8", headers)
        if request.headers.get(":method") == "HEAD":
            return _http_response(200, b"", "text/html; charset=utf-8", headers)
        if "gzip" in request.headers.get("Accept-Encoding", ""):
            return _http_response(
                200,
                gzipped,
                "text/html; charset=utf-8",
                {**headers, "Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
            )
        return _http_response(200, body, "text/html; charset=utf-8", headers)

    def _proxy_service_worker_response(self, request: Request) -> Response:
        source = (self.static_root / "sw.js").read_text()
        source = source.replace(
            'const CACHE = "mobile-terminal-v8";',
            'const CACHE = "mobile-terminal-proxy-v2";',
        )
        body = source.encode("utf-8")
        etag = f'"proxy-sw-{hashlib.sha256(body).hexdigest()[:12]}"'
        headers = {
            "Cache-Control": "no-cache",
            "ETag": etag,
            "Service-Worker-Allowed": "/",
        }
        if etag in request.headers.get("If-None-Match", ""):
            return _http_response(304, b"", "application/javascript; charset=utf-8", headers)
        if request.headers.get(":method") == "HEAD":
            return _http_response(200, b"", "application/javascript; charset=utf-8", headers)
        return _http_response(200, body, "application/javascript; charset=utf-8", headers)

    async def process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        if not _is_loopback(connection.remote_address):
            return _http_response(403, b"Forbidden\n", "text/plain; charset=utf-8")
        path = urlsplit(request.path).path
        if path == WS_PATH:
            return None
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        if request.headers.get(":method", "GET") not in ("GET", "HEAD"):
            return _http_response(405, b"Method Not Allowed", "text/plain; charset=utf-8")
        if path == "/health":
            return _http_response(200, b"ok\n", "text/plain; charset=utf-8")
        if path == "/config":
            body = json.dumps(self.config_payload()).encode("utf-8")
            return _http_response(200, body, "application/json; charset=utf-8")
        if path == "/sw.js":
            try:
                return self._proxy_service_worker_response(request)
            except OSError:
                return _http_response(404, b"Not Found", "text/plain; charset=utf-8")
        if path == "/manifest.webmanifest":
            icon_version = hashlib.md5(self.config.label.encode("utf-8")).hexdigest()[:8]
            body = json.dumps(
                {
                    "name": f"{self.config.label} terminal",
                    "short_name": self.config.label,
                    "display": "standalone",
                    "background_color": "#0b121b",
                    "theme_color": "#0b121b",
                    "icons": [
                        {"src": f"/app-icon-192.png?v={icon_version}", "sizes": "192x192", "type": "image/png"},
                        {"src": f"/app-icon-512.png?v={icon_version}", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
                    ],
                }
            ).encode("utf-8")
            return _http_response(200, body, "application/manifest+json; charset=utf-8")
        if path.startswith("/app-icon"):
            match = re.search(r"(\d{2,4})", path)
            size = min(1024, max(48, int(match.group(1)))) if match else 180
            try:
                return _http_response(200, self.render_icon(self.config.label, size), "image/png")
            except Exception:
                return _http_response(404, b"", "text/plain; charset=utf-8")
        if path in ("/", "/index.html"):
            try:
                return self._inlined_index_response(request)
            except OSError:
                return _http_response(404, b"Not Found", "text/plain; charset=utf-8")
        target, content_type = self._safe_static_target(path)
        if target is None or not target.is_file():
            return _http_response(404, b"Not Found", "text/plain; charset=utf-8")
        return self._static_response(target, content_type, request)

    def _backend_url(self, profile: ProfileConfig, session: str) -> str:
        assert profile.backend is not None
        parsed = urlsplit(profile.backend)
        path = parsed.path if parsed.path not in ("", "/") else WS_PATH
        query = urlencode({"session": session}) if session else ""
        return urlunsplit((parsed.scheme, parsed.netloc, path, query, ""))

    def _mark_profile(self, profile: ProfileConfig, status: str, message: str = "") -> None:
        self.profile_status[profile.id] = status
        self.profile_messages[profile.id] = message or profile.status_message

    def _profile_passkeys_enabled(self, profile: ProfileConfig) -> bool:
        realm = self.config.auth_realms[profile.auth_realm]
        return bool(self.passkeys and realm.device_key_auth)

    async def _send_credentials(
        self,
        connection: ServerConnection,
        profile: ProfileConfig,
    ) -> None:
        assert self.passkeys is not None
        try:
            credentials = self.passkeys.list_credentials(profile.auth_realm)
        except (PasskeyChallengeError, PasskeyVerificationError, PasskeyStoreError, ValueError) as exc:
            print(f"passkey credential listing failed: {type(exc).__name__}")
            await self.send_json(
                connection,
                {"type": "notice", "message": "Passkey operation failed."},
            )
            return
        await self.send_json(connection, {"type": "devices", "devices": credentials})

    async def _handle_credential_message(
        self,
        connection: ServerConnection,
        profile: ProfileConfig,
        payload: dict[str, Any],
    ) -> bool:
        if not self._profile_passkeys_enabled(profile):
            return False
        assert self.passkeys is not None
        message_type = payload.get("type")
        if message_type == "request-devices":
            await self._send_credentials(connection, profile)
            return True
        if message_type not in ("revoke-credential", "revoke-all-credentials"):
            return False
        try:
            if message_type == "revoke-credential":
                removed = self.passkeys.revoke_credential(
                    profile.auth_realm,
                    payload.get("credentialId", ""),
                )
                message = "Passkey revoked." if removed else "Passkey was not found."
            else:
                revoked = self.passkeys.revoke_all_credentials(profile.auth_realm)
                message = f"Revoked {revoked} passkey{'s' if revoked != 1 else ''}."
        except (PasskeyChallengeError, PasskeyVerificationError, PasskeyStoreError, ValueError) as exc:
            print(f"passkey credential revocation failed: {type(exc).__name__}")
            await self.send_json(
                connection,
                {"type": "notice", "message": "Passkey operation failed."},
            )
            return True
        await self.send_json(connection, {"type": "notice", "message": message})
        await self._send_credentials(connection, profile)
        return True

    async def _cascade_passkeys_for_rotation(
        self,
        connection: ServerConnection,
        profile: ProfileConfig,
        payload: dict[str, Any],
    ) -> bool:
        if payload.get("cascadePasskeys") is not True or not self._profile_passkeys_enabled(profile):
            return True
        assert self.passkeys is not None
        try:
            self.passkeys.revoke_all_credentials(profile.auth_realm)
        except (PasskeyChallengeError, PasskeyVerificationError, PasskeyStoreError, ValueError) as exc:
            print(f"passkey rotation cascade failed: {type(exc).__name__}")
            await self.send_json(
                connection,
                {"type": "notice", "message": "Passkey operation failed."},
            )
            return False
        return True

    async def _send_profile_state(
        self,
        connection: ServerConnection,
        profile: ProfileConfig,
        *,
        available: bool,
        message: str = "",
    ) -> None:
        await self.send_json(
            connection,
            {
                "type": "profiles",
                "profiles": self.public_profiles(),
                "activeProfile": profile.id,
            },
        )
        await self.send_json(
            connection,
            {
                "type": "profile-status",
                "profile": profile.id,
                "available": available,
                "message": message,
            },
        )

    async def _unavailable_profile(
        self,
        connection: ServerConnection,
        profile: ProfileConfig,
        message: str,
    ) -> tuple[str, str] | None:
        self._mark_profile(profile, "down", message)
        await self._send_profile_state(connection, profile, available=False, message=message)
        await self.send_json(
            connection,
            {
                "type": "ready",
                "session": "",
                "profiles": self.public_profiles(),
                "activeProfile": profile.id,
                "profileAvailable": False,
                "openTabs": [],
                "multiTenant": False,
            },
        )
        while True:
            try:
                raw = await connection.recv()
            except ConnectionClosed:
                return None
            if not isinstance(raw, str):
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if await self._handle_credential_message(connection, profile, payload):
                continue
            if payload.get("type") == "request-profiles":
                await self.send_json(
                    connection,
                    {"type": "profiles", "profiles": self.public_profiles(), "activeProfile": profile.id},
                )
            elif payload.get("type") in ("switch-profile", "retry-profile"):
                target = str(payload.get("profile", profile.id))
                session = str(payload.get("session", ""))
                return target, session

    async def _relay_profile(
        self,
        connection: ServerConnection,
        profile: ProfileConfig,
        principal: str,
        requested_session: str,
    ) -> tuple[str, str] | None:
        if not profile.available:
            return await self._unavailable_profile(
                connection,
                profile,
                profile.status_message or "This profile is not running yet.",
            )

        try:
            backend = await connect(
                self._backend_url(profile, requested_session),
                additional_headers={
                    INTERNAL_TOKEN_HEADER: self.config.internal_token_for(profile),
                    PRINCIPAL_HEADER: principal,
                },
                proxy=None,
                open_timeout=5,
                ping_interval=20,
                ping_timeout=20,
                max_size=2**20,
            )
        except Exception:
            return await self._unavailable_profile(
                connection,
                profile,
                "Backend is unavailable. Choose another profile or retry.",
            )

        self._mark_profile(profile, "up", "")
        await self._send_profile_state(connection, profile, available=True)

        async def backend_to_client() -> None:
            async for message in backend:
                if isinstance(message, str):
                    try:
                        payload = json.loads(message)
                    except json.JSONDecodeError:
                        payload = None
                    if isinstance(payload, dict) and payload.get("type") == "auth-challenge":
                        raise BackendAuthenticationError("backend rejected internal authentication")
                    if isinstance(payload, dict) and payload.get("type") == "settings":
                        await self.send_settings(
                            connection,
                            payload.get("settings"),
                            persisted=payload.get("persisted") is True,
                        )
                        continue
                    if isinstance(payload, dict) and payload.get("type") == "ready":
                        payload.update(
                            {
                                "profiles": self.public_profiles(),
                                "activeProfile": profile.id,
                                "profileAvailable": True,
                                "principal": principal,
                            }
                        )
                        message = json.dumps(payload)
                await connection.send(message)

        backend_task = asyncio.create_task(backend_to_client())
        try:
            while True:
                client_task = asyncio.create_task(connection.recv())
                done, _ = await asyncio.wait((client_task, backend_task), return_when=asyncio.FIRST_COMPLETED)
                if backend_task in done:
                    client_task.cancel()
                    try:
                        await backend_task
                    except (ConnectionClosed, BackendAuthenticationError, Exception):
                        pass
                    return await self._unavailable_profile(
                        connection,
                        profile,
                        "Backend connection closed. Choose another profile or retry.",
                    )
                try:
                    raw = client_task.result()
                except ConnectionClosed:
                    return None
                if not isinstance(raw, str):
                    await backend.send(raw)
                    continue
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    await backend.send(raw)
                    continue
                message_type = payload.get("type")
                if message_type == "request-profiles":
                    await self.send_json(
                        connection,
                        {"type": "profiles", "profiles": self.public_profiles(), "activeProfile": profile.id},
                    )
                    continue
                if message_type == "switch-profile":
                    return str(payload.get("profile", "")), str(payload.get("session", ""))
                if await self._handle_credential_message(connection, profile, payload):
                    continue
                if (
                    message_type == "rotate-token"
                    and not await self._cascade_passkeys_for_rotation(
                        connection,
                        profile,
                        payload,
                    )
                ):
                    continue
                if message_type == "request-settings":
                    settings = self.load_settings()
                    if settings is not None:
                        await self.send_settings(connection)
                        continue
                if message_type == "save-settings":
                    settings = self.save_settings(payload.get("settings"))
                    await self.send_settings(connection, settings)
                    continue
                await backend.send(raw)
        finally:
            backend_task.cancel()
            await backend.close()

    async def websocket_handler(self, connection: ServerConnection) -> None:
        request_url = urlsplit(connection.request.path)
        if request_url.path != WS_PATH:
            await connection.close(code=1008, reason="invalid path")
            return
        if not _is_loopback(connection.remote_address):
            await connection.close(code=4003, reason="forbidden")
            return

        query = parse_qs(request_url.query)
        profile_id = query.get("profile", [self.config.active_profile])[0]
        requested_sessions = {profile_id: query.get("session", [""])[0]}
        authorized_realms: dict[str, str] = {}
        previous_profile_id = ""
        connection_binding = secrets.token_urlsafe(24)

        while True:
            profile = self.config.profile(profile_id)
            if profile is None:
                profile = self.config.profile(self.config.active_profile)
            assert profile is not None
            principal = authorized_realms.get(profile.auth_realm)
            if principal is None:
                passkey_failure = False
                try:
                    principal = await self.authenticate_realm(
                        connection,
                        profile,
                        binding=connection_binding,
                    )
                except PasskeyAuthenticationError:
                    principal = None
                    passkey_failure = True
                if principal is None:
                    await self.send_json(
                        connection,
                        {
                            "type": "auth-error",
                            "message": (
                                "Passkey verification failed."
                                if passkey_failure
                                else "Authentication failed for this profile."
                            ),
                            "realm": profile.auth_realm,
                            "profile": profile.id,
                        },
                    )
                    if not authorized_realms:
                        await connection.close(code=4001, reason="auth failed")
                        return
                    profile_id = previous_profile_id
                    continue
                authorized_realms[profile.auth_realm] = principal

            previous_profile_id = profile.id
            next_profile = await self._relay_profile(
                connection,
                profile,
                principal,
                requested_sessions.get(profile.id, ""),
            )
            if next_profile is None:
                return
            profile_id, session = next_profile
            if session:
                requested_sessions[profile_id] = session

    async def run(self) -> None:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_name, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass
        async with serve(
            self.websocket_handler,
            self.config.host,
            self.config.port,
            process_request=self.process_request,
            ping_interval=20,
            ping_timeout=20,
            max_size=2**20,
        ):
            print("")
            print(f"mobile-terminal proxy listening on http://{self.config.host}:{self.config.port}")
            print(f"profiles: {', '.join(profile.id for profile in self.config.profiles)}")
            print("internal hop authentication: enabled")
            print("")
            await stop_event.wait()
