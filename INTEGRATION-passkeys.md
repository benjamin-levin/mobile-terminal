# Passkey integration

`webauthn_auth.py` and `static/passkey.js` are integration-ready seams for the proxy auth work in `docs/unified-config-design.md` §5. They do not change the current standalone server: the constrained implementation intentionally does not modify `server.py`, `static/app.js`, `static/sw.js`, or the running service.

## What is implemented

- Passkey registration and direct passkey authentication with required user verification.
- One-use, 120-second WebAuthn challenges, optionally bound to a WebSocket connection ID.
- Per-realm credential stores at `stateDir/<realm>/device-keys.json`.
- Individual credential revocation and optional revoke-all support for token rotation.
- Auth precedence resolution: trusted tailnet identity, then passkey, then bootstrap token.
- Browser conversion between py_webauthn's base64url JSON and WebAuthn `ArrayBuffer` values.
- Existing registration message names are preserved: `webauthn-register-options` and `webauthn-register`.

The dependency is `webauthn>=3.0`. It is already present in the working-tree `requirements.txt`; this change does not overwrite that parallel edit.

## Proxy setup

Create one manager for the proxy process. Use configured values rather than deriving the RP ID or origin from untrusted forwarded headers.

```python
from webauthn_auth import PasskeyAuth

passkeys = PasskeyAuth(
    config["stateDir"],
    rp_id=config["rpId"],
    rp_name=f'{config.get("label", "term")} terminal',
    expected_origin=config["origin"],
)
```

`rpId` is a hostname without scheme or port. `origin` includes scheme and port when non-default. Production WebAuthn requires HTTPS; loopback HTTP is only suitable for local development where the browser permits it.

For every begin/finish pair, pass the same unpredictable per-connection identifier as `binding`. This prevents a challenge created on one WebSocket from being completed on another.

## Wire protocol

| Direction | Type | Required payload |
|---|---|---|
| server → client | `webauthn-auth-options` | `challengeId`, `options` |
| client → server | `webauthn-auth` | `challengeId`, `assertion` |
| server → client | `webauthn-register-options` | `challengeId`, `options` |
| client → server | `webauthn-register` | `challengeId`, `attestation` |

Authentication:

```python
message = passkeys.begin_authentication(
    realm,
    principal=principal,
    binding=connection_id,
)
await send_json(connection, message)

record = passkeys.finish_authentication(
    realm,
    payload["challengeId"],
    payload["assertion"],
    binding=connection_id,
)
principal = record["principal"]
```

Registration after a bootstrap token has been validated:

```python
message = passkeys.begin_registration(
    realm,
    principal=principal,
    user_display_name=display_name,
    label=device_label,
    binding=connection_id,
)
await send_json(connection, message)

record = passkeys.finish_registration(
    realm,
    payload["challengeId"],
    payload["attestation"],
    binding=connection_id,
)
```

The module derives WebAuthn `user.id` deterministically from the RP ID and realm, so callers cannot accidentally change it and `revoke_all_credentials()` does not rotate it. The first successful registration also persists the realm's configured principal. Later registration and authentication calls must supply that same principal or fail closed. WebAuthn `user.name` is this configured principal; it is not taken from browser input. Returned credential records expose `principal`, omit the stored public key, and are safe for device-list responses.

Map `PasskeyChallengeError`, `PasskeyVerificationError`, and `PasskeyStoreError` to a generic auth failure on the wire. Log the exception class server-side, but do not send verification details to the browser.

## Auth resolution

The proxy should resolve a connection in this order, matching the existing auth block in `server.py`:

1. A trusted tailnet identity, only for loopback proxy traffic, without a Funnel marker, and only when `trustIdentity` is enabled.
2. A verified passkey from the selected auth realm.
3. A valid shared token used only to bootstrap passkey registration.

`resolve_auth_method()` codifies that ordering. An empty string remains a valid single-tenant principal; `None` means that method did not authenticate. Identity and passkey results have `terminal_authorized=True` and `bootstrap_only=False`. A token result has `terminal_authorized=False` and `bootstrap_only=True`; calling `require_terminal_authorization()` on it raises `AuthenticationRejected`. Terminal attachment code should always call that method rather than treating any resolved principal as authorization.

For a realm with credentials, send `webauthn-auth-options` first. If the realm has no credential usable by this browser, show the token bootstrap UI. Once the token is valid, hold terminal authorization until `finish_registration()` succeeds; then continue the current connection and use passkeys on later connections. This is what makes the shared token bootstrap-only rather than a durable terminal credential.

Do not auto-register the legacy WebCrypto ECDSA key after token auth in proxy mode. The current `enroll-key` / `register-key` exchange remains available only for unchanged standalone deployments.

## Browser wiring

Load `static/passkey.js` before `static/app.js` in the proxy-served app shell. Route WebAuthn messages before the normal message dispatch:

```javascript
if (await window.MobileTerminalPasskeys.handleMessage(payload, sendMessage)) {
  return;
}
```

`sendMessage` must accept the plain object produced by the helper. Handle a rejected promise by returning to the bootstrap/sign-in UI; do not silently fall through to token terminal auth. The helper deliberately does not read or persist the shared token.

The proxy app shell must include `passkey.js` in its cache version when service-worker precaching is added. No change is needed to the current standalone service worker until the proxy begins serving this script.

## Revocation and rotation

List credentials with:

```python
credentials = passkeys.list_credentials(realm)
```

Revoke exactly one credential using its base64url `credentialId`:

```python
removed = passkeys.revoke_credential(realm, credential_id)
```

Token rotation does not revoke passkeys by default. When the rotation action requests a cascade, call:

```python
revoked_count = passkeys.revoke_all_credentials(realm)
```

This closes the existing gap where token rotation clears legacy remembered devices but leaves `state/device-keys.json` keys valid.

## Existing state

The legacy store contains browser-generated SPKI keys and cannot be converted into WebAuthn credentials. Do not copy it into a realm store. Leave standalone state untouched, create realm stores on first successful registration, and require one token bootstrap per browser when proxy passkeys are enabled.

Credential files are atomically replaced with mode `0600`. Invalid or corrupt stores fail closed instead of being treated as empty. Realm names are restricted to letters, numbers, `_`, `.`, and `-`, cannot begin with punctuation, and cannot escape `stateDir`.
