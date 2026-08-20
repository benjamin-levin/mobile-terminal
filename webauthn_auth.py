from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers.structs import (
    AuthenticatorAttachment,
    AuthenticatorSelectionCriteria,
    AuthenticatorTransport,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)


REGISTER_OPTIONS_MESSAGE = "webauthn-register-options"
REGISTER_MESSAGE = "webauthn-register"
AUTH_OPTIONS_MESSAGE = "webauthn-auth-options"
AUTH_MESSAGE = "webauthn-auth"
CREDENTIAL_STORE_VERSION = 1
REALM_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class PasskeyError(Exception):
    pass


class PasskeyChallengeError(PasskeyError):
    pass


class PasskeyVerificationError(PasskeyError):
    pass


class PasskeyStoreError(PasskeyError):
    pass


class AuthenticationRejected(PasskeyError):
    pass


@dataclass(frozen=True)
class AuthResolution:
    method: str
    principal: str
    terminal_authorized: bool
    bootstrap_only: bool

    def require_terminal_authorization(self) -> str:
        if not self.terminal_authorized:
            raise AuthenticationRejected("Passkey enrollment is required before terminal access.")
        return self.principal


@dataclass(frozen=True)
class _PendingChallenge:
    purpose: str
    realm: str
    challenge: bytes
    created_at: float
    user_id: bytes = b""
    principal: str = ""
    label: str = ""
    binding: str = ""


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise PasskeyVerificationError("Invalid credential ID.") from exc


def _utc_now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def resolve_auth_method(
    *,
    identity_principal: str | None = None,
    passkey_principal: str | None = None,
    token_principal: str | None = None,
) -> AuthResolution:
    """Resolve an authenticated principal in the required identity/passkey/token order."""
    if identity_principal is not None:
        return AuthResolution("identity", identity_principal, True, False)
    if passkey_principal is not None:
        return AuthResolution("passkey", passkey_principal, True, False)
    if token_principal is not None:
        return AuthResolution("token", token_principal, False, True)
    raise AuthenticationRejected("Authentication required.")


class PasskeyAuth:
    """Per-realm WebAuthn registration, authentication, and credential revocation."""

    def __init__(
        self,
        state_dir: str | Path,
        *,
        rp_id: str,
        rp_name: str,
        expected_origin: str | list[str],
        challenge_ttl: float = 120,
        max_pending_challenges: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not rp_id or "://" in rp_id or "/" in rp_id or ":" in rp_id:
            raise ValueError("rp_id must be a hostname without a scheme or port")
        if not rp_name:
            raise ValueError("rp_name is required")
        if not expected_origin or isinstance(expected_origin, list) and not expected_origin:
            raise ValueError("expected_origin is required")
        if challenge_ttl <= 0:
            raise ValueError("challenge_ttl must be positive")
        if max_pending_challenges <= 0:
            raise ValueError("max_pending_challenges must be positive")

        self.state_dir = Path(state_dir)
        self.rp_id = rp_id
        self.rp_name = rp_name
        self.expected_origin = expected_origin
        self.challenge_ttl = challenge_ttl
        self.max_pending_challenges = max_pending_challenges
        self.clock = clock
        self._challenges: dict[str, _PendingChallenge] = {}
        self._challenge_lock = threading.Lock()
        self._store_lock = threading.Lock()

    def begin_registration(
        self,
        realm: str,
        *,
        principal: str,
        user_name: str | None = None,
        user_display_name: str | None = None,
        label: str = "device",
        binding: str = "",
    ) -> dict[str, Any]:
        realm = self._validate_realm(realm)
        principal = self._validate_principal(principal)
        user_id = self.user_id_for_realm(realm)

        with self._store_lock:
            store = self._load_store_unlocked(realm)
            realm_user = store.get("realmUser")
            if realm_user is not None and realm_user["principal"] != principal:
                raise ValueError("Auth realm principal does not match its enrolled principal.")
            records = store["credentials"]
        exclude_credentials = [
            PublicKeyCredentialDescriptor(id=_b64url_decode(credential_id))
            for credential_id in records
        ]
        options = generate_registration_options(
            rp_id=self.rp_id,
            rp_name=self.rp_name,
            user_id=user_id,
            user_name=user_name or principal,
            user_display_name=user_display_name or principal,
            authenticator_selection=AuthenticatorSelectionCriteria(
                authenticator_attachment=AuthenticatorAttachment.PLATFORM,
                resident_key=ResidentKeyRequirement.REQUIRED,
                user_verification=UserVerificationRequirement.REQUIRED,
            ),
            exclude_credentials=exclude_credentials,
        )
        challenge_id = self._save_challenge(
            _PendingChallenge(
                purpose="register",
                realm=realm,
                challenge=bytes(options.challenge),
                created_at=self.clock(),
                user_id=user_id,
                principal=principal,
                label=self._clean_label(label),
                binding=str(binding),
            )
        )
        return {
            "type": REGISTER_OPTIONS_MESSAGE,
            "challengeId": challenge_id,
            "options": json.loads(options_to_json(options)),
        }

    def finish_registration(
        self,
        realm: str,
        challenge_id: str,
        attestation: dict[str, Any],
        *,
        label: str | None = None,
        binding: str = "",
    ) -> dict[str, Any]:
        realm = self._validate_realm(realm)
        pending = self._take_challenge(challenge_id, "register", realm, str(binding))
        try:
            verified = verify_registration_response(
                credential=attestation,
                expected_challenge=pending.challenge,
                expected_rp_id=self.rp_id,
                expected_origin=self.expected_origin,
                require_user_verification=True,
            )
        except Exception as exc:
            raise PasskeyVerificationError("Passkey registration failed.") from exc

        credential_id = _b64url_encode(bytes(verified.credential_id))
        now = _utc_now()
        record = {
            "credentialId": credential_id,
            "publicKey": _b64url_encode(bytes(verified.credential_public_key)),
            "signCount": int(verified.sign_count),
            "userId": _b64url_encode(pending.user_id),
            "principal": pending.principal,
            "label": self._clean_label(label if label is not None else pending.label),
            "created": now,
            "lastSeen": now,
            "deviceType": verified.credential_device_type.value,
            "backedUp": bool(verified.credential_backed_up),
        }
        transports = self._credential_transports(attestation)
        if transports:
            record["transports"] = transports

        with self._store_lock:
            store = self._load_store_unlocked(realm)
            realm_user = store.get("realmUser")
            expected_realm_user = {
                "id": _b64url_encode(pending.user_id),
                "principal": pending.principal,
            }
            if realm_user is not None and realm_user != expected_realm_user:
                raise PasskeyVerificationError("Auth realm principal changed during registration.")
            if credential_id in store["credentials"]:
                raise PasskeyVerificationError("Passkey is already registered.")
            store["realmUser"] = expected_realm_user
            store["credentials"][credential_id] = record
            self._save_store_unlocked(realm, store)
        return self._public_record(record)

    def begin_authentication(
        self,
        realm: str,
        *,
        principal: str,
        binding: str = "",
    ) -> dict[str, Any]:
        realm = self._validate_realm(realm)
        principal = self._validate_principal(principal)
        with self._store_lock:
            store = self._load_store_unlocked(realm)
            realm_user = store.get("realmUser")
            if realm_user is not None and realm_user["principal"] != principal:
                raise ValueError("Auth realm principal does not match its enrolled principal.")
            records = store["credentials"]
        allow_credentials = [
            PublicKeyCredentialDescriptor(
                id=_b64url_decode(credential_id),
                transports=[AuthenticatorTransport(value) for value in record.get("transports", [])],
            )
            for credential_id, record in records.items()
        ]
        options = generate_authentication_options(
            rp_id=self.rp_id,
            allow_credentials=allow_credentials,
            user_verification=UserVerificationRequirement.REQUIRED,
        )
        challenge_id = self._save_challenge(
            _PendingChallenge(
                purpose="authenticate",
                realm=realm,
                challenge=bytes(options.challenge),
                created_at=self.clock(),
                principal=principal,
                binding=str(binding),
            )
        )
        return {
            "type": AUTH_OPTIONS_MESSAGE,
            "challengeId": challenge_id,
            "options": json.loads(options_to_json(options)),
        }

    def finish_authentication(
        self,
        realm: str,
        challenge_id: str,
        assertion: dict[str, Any],
        *,
        binding: str = "",
    ) -> dict[str, Any]:
        realm = self._validate_realm(realm)
        pending = self._take_challenge(challenge_id, "authenticate", realm, str(binding))
        credential_id = self._assertion_credential_id(assertion)

        with self._store_lock:
            store = self._load_store_unlocked(realm)
            record = store["credentials"].get(credential_id)
            if not isinstance(record, dict):
                raise PasskeyVerificationError("Passkey is not registered in this realm.")
            if record["principal"] != pending.principal:
                raise PasskeyVerificationError("Passkey principal does not match this auth realm.")
            try:
                public_key = _b64url_decode(record["publicKey"])
                sign_count = int(record.get("signCount", 0))
            except (KeyError, TypeError, ValueError) as exc:
                raise PasskeyStoreError("Passkey credential record is invalid.") from exc

            try:
                verified = verify_authentication_response(
                    credential=assertion,
                    expected_challenge=pending.challenge,
                    expected_rp_id=self.rp_id,
                    expected_origin=self.expected_origin,
                    credential_public_key=public_key,
                    credential_current_sign_count=sign_count,
                    require_user_verification=True,
                )
            except Exception as exc:
                raise PasskeyVerificationError("Passkey authentication failed.") from exc

            if _b64url_encode(bytes(verified.credential_id)) != credential_id:
                raise PasskeyVerificationError("Passkey credential ID did not match.")
            record["signCount"] = int(verified.new_sign_count)
            record["lastSeen"] = _utc_now()
            record["deviceType"] = verified.credential_device_type.value
            record["backedUp"] = bool(verified.credential_backed_up)
            self._save_store_unlocked(realm, store)
            return self._public_record(record)

    def list_credentials(self, realm: str) -> list[dict[str, Any]]:
        realm = self._validate_realm(realm)
        with self._store_lock:
            records = self._load_store_unlocked(realm)["credentials"].values()
            return sorted(
                (self._public_record(record) for record in records),
                key=lambda record: record.get("created", ""),
            )

    def revoke_credential(self, realm: str, credential_id: str) -> bool:
        realm = self._validate_realm(realm)
        credential_id = _b64url_encode(_b64url_decode(credential_id))
        with self._store_lock:
            store = self._load_store_unlocked(realm)
            if credential_id not in store["credentials"]:
                return False
            del store["credentials"][credential_id]
            self._save_store_unlocked(realm, store)
            return True

    def revoke_all_credentials(self, realm: str) -> int:
        """Revoke a realm's passkeys, for example as part of token rotation cascade."""
        realm = self._validate_realm(realm)
        with self._store_lock:
            store = self._load_store_unlocked(realm)
            revoked = len(store["credentials"])
            if revoked:
                store["credentials"] = {}
                self._save_store_unlocked(realm, store)
            return revoked

    def _save_challenge(self, pending: _PendingChallenge) -> str:
        with self._challenge_lock:
            self._discard_expired_challenges_unlocked()
            while len(self._challenges) >= self.max_pending_challenges:
                oldest = min(self._challenges, key=lambda key: self._challenges[key].created_at)
                del self._challenges[oldest]
            challenge_id = secrets.token_urlsafe(24)
            self._challenges[challenge_id] = pending
            return challenge_id

    def _take_challenge(
        self,
        challenge_id: str,
        purpose: str,
        realm: str,
        binding: str,
    ) -> _PendingChallenge:
        with self._challenge_lock:
            pending = self._challenges.pop(str(challenge_id), None)
        if pending is None:
            raise PasskeyChallengeError("Passkey challenge is missing or already used.")
        if pending.purpose != purpose or pending.realm != realm or pending.binding != binding:
            raise PasskeyChallengeError("Passkey challenge does not match this request.")
        if self.clock() - pending.created_at > self.challenge_ttl:
            raise PasskeyChallengeError("Passkey challenge expired.")
        return pending

    def _discard_expired_challenges_unlocked(self) -> None:
        cutoff = self.clock() - self.challenge_ttl
        expired = [key for key, value in self._challenges.items() if value.created_at < cutoff]
        for key in expired:
            del self._challenges[key]

    def user_id_for_realm(self, realm: str) -> bytes:
        """Return the stable WebAuthn user.id owned by this RP/auth-realm pair."""
        realm = self._validate_realm(realm)
        material = b"mobile-terminal-passkey-user-v1\0" + self.rp_id.encode("utf-8") + b"\0" + realm.encode("ascii")
        return hashlib.sha256(material).digest()

    def _realm_path(self, realm: str) -> Path:
        return self.state_dir / realm / "device-keys.json"

    @staticmethod
    def _validate_realm(realm: str) -> str:
        realm = str(realm)
        if not REALM_PATTERN.fullmatch(realm):
            raise ValueError("Invalid auth realm.")
        return realm

    @staticmethod
    def _validate_principal(principal: str) -> str:
        if not isinstance(principal, str) or not principal.strip() or len(principal) > 128:
            raise ValueError("principal must be a non-empty string of at most 128 characters")
        if principal != principal.strip():
            raise ValueError("principal must not have leading or trailing whitespace")
        return principal

    @staticmethod
    def _clean_label(label: str | None) -> str:
        return (str(label or "device").strip() or "device")[:80]

    @staticmethod
    def _credential_transports(attestation: dict[str, Any]) -> list[str]:
        response = attestation.get("response") if isinstance(attestation, dict) else None
        transports = response.get("transports") if isinstance(response, dict) else None
        if not isinstance(transports, list):
            return []
        allowed = {"ble", "cable", "hybrid", "internal", "nfc", "smart-card", "usb"}
        return [value for value in transports if isinstance(value, str) and value in allowed]

    @staticmethod
    def _assertion_credential_id(assertion: dict[str, Any]) -> str:
        if not isinstance(assertion, dict):
            raise PasskeyVerificationError("Passkey assertion is invalid.")
        raw_id = assertion.get("rawId") or assertion.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            raise PasskeyVerificationError("Passkey assertion has no credential ID.")
        return _b64url_encode(_b64url_decode(raw_id))

    @staticmethod
    def _public_record(record: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in record.items() if key != "publicKey"}

    def _load_store_unlocked(self, realm: str) -> dict[str, Any]:
        path = self._realm_path(realm)
        if not path.is_file():
            return {"version": CREDENTIAL_STORE_VERSION, "credentials": {}}
        try:
            store = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise PasskeyStoreError(f"Unable to read passkey store for realm '{realm}'.") from exc
        if not isinstance(store, dict):
            raise PasskeyStoreError(f"Invalid passkey store for realm '{realm}'.")
        credentials = store.get("credentials")
        if (
            store.get("version") != CREDENTIAL_STORE_VERSION
            or not isinstance(credentials, dict)
        ):
            raise PasskeyStoreError(f"Invalid passkey store for realm '{realm}'.")
        realm_user = store.get("realmUser")
        try:
            if realm_user is not None:
                expected_user_id = _b64url_encode(self.user_id_for_realm(realm))
                if (
                    not isinstance(realm_user, dict)
                    or realm_user.get("id") != expected_user_id
                    or self._validate_principal(realm_user.get("principal")) != realm_user["principal"]
                ):
                    raise ValueError
            elif credentials:
                raise ValueError
            for credential_id, record in credentials.items():
                if (
                    not isinstance(credential_id, str)
                    or _b64url_encode(_b64url_decode(credential_id)) != credential_id
                    or not isinstance(record, dict)
                    or record.get("credentialId") != credential_id
                    or not isinstance(record.get("publicKey"), str)
                    or realm_user is None
                    or record.get("userId") != realm_user["id"]
                    or record.get("principal") != realm_user["principal"]
                    or int(record.get("signCount", 0)) < 0
                ):
                    raise ValueError
                _b64url_decode(record["publicKey"])
                transports = record.get("transports", [])
                if not isinstance(transports, list):
                    raise ValueError
                for value in transports:
                    AuthenticatorTransport(value)
        except (PasskeyError, TypeError, ValueError) as exc:
            raise PasskeyStoreError(f"Invalid passkey store for realm '{realm}'.") from exc
        return store

    def _save_store_unlocked(self, realm: str, store: dict[str, Any]) -> None:
        path = self._realm_path(realm)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
        try:
            temporary.write_text(json.dumps(store, indent=2) + "\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise PasskeyStoreError(f"Unable to save passkey store for realm '{realm}'.") from exc
