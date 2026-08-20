from __future__ import annotations

import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit


PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ACCENT_PATTERN = re.compile(r"^#[0-9A-Fa-f]{6}$")
LOOPBACK_NAMES = {"localhost", "localhost.localdomain"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class AuthRealmConfig:
    id: str
    token: str | None = field(default=None, repr=False)
    principal: str = ""
    device_key_auth: bool = False
    trust_identity: bool = False


@dataclass(frozen=True)
class ProfileConfig:
    id: str
    label: str
    auth_realm: str
    backend: str | None
    os_user: str = ""
    accent: str = "#ffd166"
    status: str = "up"
    status_message: str = ""
    internal_token: str | None = field(default=None, repr=False)

    @property
    def available(self) -> bool:
        return self.backend is not None and self.status != "down"


@dataclass(frozen=True)
class ProxyConfig:
    path: Path
    host: str
    port: int
    state_dir: Path
    label: str
    auth_realms: dict[str, AuthRealmConfig]
    profiles: tuple[ProfileConfig, ...]
    active_profile: str
    internal_token: str = field(repr=False)
    rp_id: str = ""
    expected_origin: str = ""

    def profile(self, profile_id: str) -> ProfileConfig | None:
        return next((profile for profile in self.profiles if profile.id == profile_id), None)

    def internal_token_for(self, profile: ProfileConfig) -> str:
        return profile.internal_token or self.internal_token


def _string(raw: Mapping[str, Any], key: str, *, required: bool = False) -> str:
    value = raw.get(key)
    if value is None:
        if required:
            raise ConfigError(f"'{key}' is required")
        return ""
    if not isinstance(value, str):
        raise ConfigError(f"'{key}' must be a string")
    value = value.strip()
    if required and not value:
        raise ConfigError(f"'{key}' must not be empty")
    return value


def _boolean(raw: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"'{key}' must be true or false")
    return value


def _secret(
    raw: Mapping[str, Any],
    key: str,
    env_key: str,
    environ: Mapping[str, str],
    *,
    required: bool,
) -> str | None:
    direct = _string(raw, key)
    env_name = _string(raw, env_key)
    if env_name and not ENV_NAME_PATTERN.fullmatch(env_name):
        raise ConfigError(f"'{env_key}' must name a valid environment variable")
    if direct and env_name:
        raise ConfigError(f"set only one of '{key}' or '{env_key}'")
    if env_name:
        direct = environ.get(env_name, "").strip()
        if not direct:
            raise ConfigError(f"environment variable '{env_name}' is empty or unset")
    if required and not direct:
        raise ConfigError(f"'{key}' or '{env_key}' is required")
    return direct or None


def _secure_secret_config(path: Path, raw: Mapping[str, Any]) -> None:
    realms = raw.get("authRealms")
    profiles = raw.get("profiles")
    contains_secret = bool(_string(raw, "internalToken"))
    if isinstance(realms, dict):
        contains_secret = contains_secret or any(
            isinstance(realm, dict) and bool(_string(realm, "token"))
            for realm in realms.values()
        )
    if isinstance(profiles, list):
        contains_secret = contains_secret or any(
            isinstance(profile, dict) and bool(_string(profile, "internalToken"))
            for profile in profiles
        )
    if not contains_secret:
        return
    try:
        if path.stat().st_mode & 0o077:
            path.chmod(0o600)
    except OSError as exc:
        raise ConfigError(f"secret-bearing config file '{path}' must be owner-only (mode 0600)") from exc


def _parse_listen(value: str) -> tuple[str, int]:
    if not value:
        value = "127.0.0.1:8085"
    if value.startswith("["):
        match = re.fullmatch(r"\[([^]]+)]:(\d+)", value)
        if not match:
            raise ConfigError("'listen' must be HOST:PORT")
        host, port_text = match.groups()
    else:
        try:
            host, port_text = value.rsplit(":", 1)
        except ValueError as exc:
            raise ConfigError("'listen' must be HOST:PORT") from exc
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ConfigError("'listen' port must be an integer") from exc
    if not host or not 1 <= port <= 65535:
        raise ConfigError("'listen' must contain a host and a port from 1 to 65535")
    return host, port


def _is_loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_backend(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname or parsed.port is None:
        raise ConfigError("profile 'backend' must be a ws:// or wss:// URL with an explicit port")
    if parsed.username or parsed.password or parsed.fragment:
        raise ConfigError("profile 'backend' must not contain credentials or a fragment")
    if not _is_loopback(parsed.hostname):
        raise ConfigError("profile 'backend' must use a loopback host")
    if parsed.path not in ("", "/", "/_ws"):
        raise ConfigError("profile 'backend' path must be empty or /_ws")
    return value.rstrip("/")


def _passkey_config(raw: Mapping[str, Any], *, required: bool) -> tuple[str, str]:
    rp_id = _string(raw, "rpId").lower()
    expected_origin = _string(raw, "origin")
    if bool(rp_id) != bool(expected_origin):
        raise ConfigError("'rpId' and 'origin' must be configured together")
    if required and not rp_id:
        raise ConfigError("'rpId' and 'origin' are required when deviceKeyAuth is enabled")
    if not rp_id:
        return "", ""
    labels = rp_id.split(".")
    if (
        len(rp_id) > 253
        or any(
            not label
            or len(label) > 63
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label)
            for label in labels
        )
    ):
        raise ConfigError("'rpId' must be a hostname without a scheme or port")
    try:
        parsed = urlsplit(expected_origin)
        parsed.port
    except ValueError as exc:
        raise ConfigError("'origin' must be an http:// or https:// origin") from exc
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError("'origin' must be an http:// or https:// origin without a path")
    origin_host = parsed.hostname.lower()
    if parsed.scheme == "http" and not _is_loopback(origin_host):
        raise ConfigError("'origin' must use https except for loopback development")
    if origin_host != rp_id and not origin_host.endswith(f".{rp_id}"):
        raise ConfigError("'origin' hostname must equal or be a subdomain of 'rpId'")
    return rp_id, expected_origin


def load_runtime_config(environ: Mapping[str, str] | None = None) -> ProxyConfig | None:
    environ = os.environ if environ is None else environ
    configured_path = environ.get("MOBILE_TERMINAL_CONFIG", "").strip()
    if not configured_path:
        return None

    path = Path(configured_path).expanduser().resolve()
    try:
        raw = json.loads(path.read_text())
    except OSError as exc:
        raise ConfigError(f"unable to read config file '{path}'") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"invalid JSON in config file '{path}': {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be an object")

    mode = _string(raw, "mode", required=True)
    if mode in ("backend", "single"):
        return None
    if mode != "proxy":
        raise ConfigError("'mode' must be 'proxy', 'backend', or 'single'")

    _secure_secret_config(path, raw)
    host, port = _parse_listen(_string(raw, "listen"))
    if not _is_loopback(host):
        raise ConfigError("proxy 'listen' host must be loopback")

    state_dir_value = _string(raw, "stateDir") or "state/proxy"
    state_dir = Path(state_dir_value).expanduser()
    if not state_dir.is_absolute():
        state_dir = path.parent / state_dir
    state_dir = state_dir.resolve()

    internal_token = _secret(
        raw,
        "internalToken",
        "internalTokenEnv",
        environ,
        required=True,
    )
    assert internal_token is not None

    raw_realms = raw.get("authRealms")
    if not isinstance(raw_realms, dict) or not raw_realms:
        raise ConfigError("'authRealms' must be a non-empty object")
    auth_realms: dict[str, AuthRealmConfig] = {}
    for realm_id, realm_raw in raw_realms.items():
        if not isinstance(realm_id, str) or not PROFILE_ID_PATTERN.fullmatch(realm_id):
            raise ConfigError(f"invalid auth realm id '{realm_id}'")
        if not isinstance(realm_raw, dict):
            raise ConfigError(f"auth realm '{realm_id}' must be an object")
        trust_identity = _boolean(realm_raw, "trustIdentity")
        token = _secret(
            realm_raw,
            "token",
            "tokenEnv",
            environ,
            required=not trust_identity,
        )
        auth_realms[realm_id] = AuthRealmConfig(
            id=realm_id,
            token=token,
            principal=_string(realm_raw, "principal") or realm_id,
            device_key_auth=_boolean(realm_raw, "deviceKeyAuth"),
            trust_identity=trust_identity,
        )

    rp_id, expected_origin = _passkey_config(
        raw,
        required=any(realm.device_key_auth for realm in auth_realms.values()),
    )

    raw_profiles = raw.get("profiles")
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ConfigError("'profiles' must be a non-empty array")
    profiles: list[ProfileConfig] = []
    seen_ids: set[str] = set()
    internal_token_owners: dict[str, str] = {}
    for index, profile_raw in enumerate(raw_profiles):
        if not isinstance(profile_raw, dict):
            raise ConfigError(f"profile at index {index} must be an object")
        profile_id = _string(profile_raw, "id", required=True)
        if not PROFILE_ID_PATTERN.fullmatch(profile_id):
            raise ConfigError(f"invalid profile id '{profile_id}'")
        if profile_id in seen_ids:
            raise ConfigError(f"duplicate profile id '{profile_id}'")
        seen_ids.add(profile_id)
        auth_realm = _string(profile_raw, "authRealm", required=True)
        if auth_realm not in auth_realms:
            raise ConfigError(f"profile '{profile_id}' references unknown auth realm '{auth_realm}'")
        backend_value = profile_raw.get("backend")
        if backend_value is not None and not isinstance(backend_value, str):
            raise ConfigError(f"profile '{profile_id}' backend must be a string or null")
        backend = _validate_backend(backend_value.strip()) if isinstance(backend_value, str) and backend_value.strip() else None
        profile_internal_token = _secret(
            profile_raw,
            "internalToken",
            "internalTokenEnv",
            environ,
            required=False,
        )
        if profile_internal_token:
            if profile_internal_token == internal_token:
                raise ConfigError(
                    f"profile '{profile_id}' internal token override must differ from the shared root token"
                )
            previous_owner = internal_token_owners.get(profile_internal_token)
            if previous_owner is not None:
                raise ConfigError(
                    f"profiles '{previous_owner}' and '{profile_id}' must not share an internal token override"
                )
            internal_token_owners[profile_internal_token] = profile_id
        status = _string(profile_raw, "status") or ("up" if backend else "down")
        if status not in ("up", "down"):
            raise ConfigError(f"profile '{profile_id}' status must be 'up' or 'down'")
        accent = _string(profile_raw, "accent") or "#ffd166"
        if not ACCENT_PATTERN.fullmatch(accent):
            raise ConfigError(f"profile '{profile_id}' accent must be a six-digit hex color")
        profiles.append(
            ProfileConfig(
                id=profile_id,
                label=_string(profile_raw, "label") or profile_id,
                auth_realm=auth_realm,
                backend=backend,
                os_user=_string(profile_raw, "osUser"),
                accent=accent.lower(),
                status=status,
                status_message=_string(profile_raw, "statusMessage") or ("Backend unavailable" if status == "down" else ""),
                internal_token=profile_internal_token,
            )
        )

    active_profile = _string(raw, "activeProfile") or profiles[0].id
    if active_profile not in seen_ids:
        raise ConfigError(f"active profile '{active_profile}' does not exist")

    return ProxyConfig(
        path=path,
        host=host,
        port=port,
        state_dir=state_dir,
        label=(_string(raw, "label") or "term")[:12],
        auth_realms=auth_realms,
        profiles=tuple(profiles),
        active_profile=active_profile,
        internal_token=internal_token,
        rp_id=rp_id,
        expected_origin=expected_origin,
    )
