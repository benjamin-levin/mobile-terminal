from __future__ import annotations

import hmac
import importlib
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from mobile_terminal_config import AuthRealmConfig


@dataclass(frozen=True)
class AuthenticationRequest:
    realm: str
    credentials: Mapping[str, Any]
    headers: Mapping[str, str]
    remote_address: Any


class Authenticator(Protocol):
    def __call__(self, request: AuthenticationRequest) -> str | None: ...


class TokenAuthenticator:
    def __init__(self, realms: Mapping[str, AuthRealmConfig]) -> None:
        self.realms = realms

    def authenticate(self, request: AuthenticationRequest) -> str | None:
        realm = self.realms.get(request.realm)
        if realm is None or realm.token is None:
            return None
        candidate = request.credentials.get("token", "")
        if not isinstance(candidate, str):
            return None
        if not hmac.compare_digest(candidate, realm.token):
            return None
        return realm.principal

    def __call__(self, request: AuthenticationRequest) -> str | None:
        return self.authenticate(request)


def load_authenticator(
    realms: Mapping[str, AuthRealmConfig],
    configured: str = "",
) -> Authenticator:
    if not configured:
        return TokenAuthenticator(realms)
    try:
        module_name, attribute = configured.rsplit(":", 1)
    except ValueError as exc:
        raise ValueError("MOBILE_TERMINAL_AUTHENTICATOR must be 'module:callable'") from exc
    implementation = getattr(importlib.import_module(module_name), attribute, None)
    if not callable(implementation):
        raise ValueError(f"configured authenticator '{configured}' is not callable")
    return implementation
