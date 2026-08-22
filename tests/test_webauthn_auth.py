import base64
import hashlib
import json
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import encode_cbor
from webauthn.helpers.structs import CredentialDeviceType

from webauthn_auth import (
    AUTH_MESSAGE,
    AUTH_OPTIONS_MESSAGE,
    REGISTER_MESSAGE,
    REGISTER_OPTIONS_MESSAGE,
    AuthenticationRejected,
    PasskeyAuth,
    PasskeyChallengeError,
    PasskeyStoreError,
    PasskeyVerificationError,
    resolve_auth_method,
)


CREDENTIAL_ID = b"credential-1"
CREDENTIAL_ID_B64 = "Y3JlZGVudGlhbC0x"
PUBLIC_KEY = b"cose-public-key"
ORIGIN = "https://term.example.test"


def b64url(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def client_data(operation, challenge):
    return json.dumps(
        {
            "type": operation,
            "challenge": challenge,
            "origin": ORIGIN,
            "crossOrigin": False,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def synthetic_registration(options, private_key, credential_id, sign_count=0):
    public_numbers = private_key.public_key().public_numbers()
    cose_key = encode_cbor(
        {
            1: 2,
            3: -7,
            -1: 1,
            -2: public_numbers.x.to_bytes(32, "big"),
            -3: public_numbers.y.to_bytes(32, "big"),
        }
    )
    authenticator_data = b"".join(
        (
            hashlib.sha256(options["rp"]["id"].encode("utf-8")).digest(),
            bytes((0x45,)),
            sign_count.to_bytes(4, "big"),
            bytes(16),
            len(credential_id).to_bytes(2, "big"),
            credential_id,
            cose_key,
        )
    )
    attestation_object = encode_cbor(
        {"fmt": "none", "attStmt": {}, "authData": authenticator_data}
    )
    raw_client_data = client_data("webauthn.create", options["challenge"])
    return {
        "id": b64url(credential_id),
        "rawId": b64url(credential_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": b64url(raw_client_data),
            "attestationObject": b64url(attestation_object),
            "transports": ["internal"],
        },
    }


def synthetic_assertion(options, private_key, credential_id, sign_count):
    raw_client_data = client_data("webauthn.get", options["challenge"])
    authenticator_data = b"".join(
        (
            hashlib.sha256(options["rpId"].encode("utf-8")).digest(),
            bytes((0x05,)),
            sign_count.to_bytes(4, "big"),
        )
    )
    signature = private_key.sign(
        authenticator_data + hashlib.sha256(raw_client_data).digest(),
        ec.ECDSA(hashes.SHA256()),
    )
    return {
        "id": b64url(credential_id),
        "rawId": b64url(credential_id),
        "type": "public-key",
        "response": {
            "clientDataJSON": b64url(raw_client_data),
            "authenticatorData": b64url(authenticator_data),
            "signature": b64url(signature),
            "userHandle": None,
        },
    }


def registration_result():
    return SimpleNamespace(
        credential_id=CREDENTIAL_ID,
        credential_public_key=PUBLIC_KEY,
        sign_count=4,
        credential_device_type=CredentialDeviceType.SINGLE_DEVICE,
        credential_backed_up=False,
    )


def authentication_result(sign_count=5):
    return SimpleNamespace(
        credential_id=CREDENTIAL_ID,
        new_sign_count=sign_count,
        credential_device_type=CredentialDeviceType.MULTI_DEVICE,
        credential_backed_up=True,
    )


class PasskeyAuthTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temporary.name)
        self.auth = PasskeyAuth(
            self.state_dir,
            rp_id="term.example.test",
            rp_name="term terminal",
            expected_origin="https://term.example.test",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def register(self, realm="mine", label="Phone"):
        options = self.auth.begin_registration(
            realm,
            principal="ben",
            label=label,
        )
        attestation = {
            "id": CREDENTIAL_ID_B64,
            "rawId": CREDENTIAL_ID_B64,
            "type": "public-key",
            "response": {"transports": ["internal", "hybrid", "invalid"]},
        }
        with mock.patch("webauthn_auth.verify_registration_response", return_value=registration_result()) as verify:
            record = self.auth.finish_registration(
                realm,
                options["challengeId"],
                attestation,
            )
        return options, record, verify

    def register_real(self):
        private_key = ec.generate_private_key(ec.SECP256R1())
        credential_id = b"real-p256-credential"
        options = self.auth.begin_registration("mine", principal="ben")
        attestation = synthetic_registration(options["options"], private_key, credential_id)
        record = self.auth.finish_registration(
            "mine",
            options["challengeId"],
            attestation,
        )
        return private_key, credential_id, record

    def authenticate_real(self, private_key, credential_id, sign_count):
        options = self.auth.begin_authentication("mine", principal="ben")
        assertion = synthetic_assertion(
            options["options"],
            private_key,
            credential_id,
            sign_count,
        )
        return self.auth.finish_authentication(
            "mine",
            options["challengeId"],
            assertion,
        )

    def test_registration_uses_existing_message_names_and_persists_per_realm(self):
        options, record, verify = self.register()

        self.assertEqual(options["type"], REGISTER_OPTIONS_MESSAGE)
        self.assertEqual(REGISTER_MESSAGE, "webauthn-register")
        self.assertEqual(options["options"]["rp"]["id"], "term.example.test")
        self.assertEqual(options["options"]["authenticatorSelection"]["residentKey"], "required")
        self.assertEqual(record["credentialId"], CREDENTIAL_ID_B64)
        self.assertEqual(record["label"], "Phone")
        self.assertNotIn("publicKey", record)
        self.assertTrue(verify.call_args.kwargs["require_user_verification"])

        store_path = self.state_dir / "mine" / "device-keys.json"
        store = json.loads(store_path.read_text())
        self.assertEqual(set(store), {"version", "realmUser", "credentials"})
        self.assertEqual(store["realmUser"]["principal"], "ben")
        self.assertEqual(store["realmUser"]["id"], options["options"]["user"]["id"])
        saved = store["credentials"][CREDENTIAL_ID_B64]
        self.assertEqual(
            set(saved),
            {
                "credentialId",
                "publicKey",
                "signCount",
                "userId",
                "principal",
                "label",
                "created",
                "lastSeen",
                "deviceType",
                "backedUp",
                "transports",
            },
        )
        self.assertEqual(saved["principal"], "ben")
        self.assertEqual(saved["transports"], ["internal", "hybrid"])
        self.assertEqual(saved["publicKey"], "Y29zZS1wdWJsaWMta2V5")
        self.assertEqual(stat.S_IMODE(store_path.stat().st_mode), 0o600)
        self.assertFalse((self.state_dir / "other" / "device-keys.json").exists())

    def test_authentication_is_one_time_and_updates_counter(self):
        self.register()
        options = self.auth.begin_authentication("mine", principal="ben")
        assertion = {
            "id": CREDENTIAL_ID_B64,
            "rawId": CREDENTIAL_ID_B64,
            "type": "public-key",
            "response": {},
        }

        with mock.patch("webauthn_auth.verify_authentication_response", return_value=authentication_result()) as verify:
            record = self.auth.finish_authentication(
                "mine",
                options["challengeId"],
                assertion,
            )

        self.assertEqual(options["type"], AUTH_OPTIONS_MESSAGE)
        self.assertEqual(AUTH_MESSAGE, "webauthn-auth")
        self.assertEqual(options["options"]["allowCredentials"][0]["id"], CREDENTIAL_ID_B64)
        self.assertEqual(record["signCount"], 5)
        self.assertEqual(record["deviceType"], "multi_device")
        self.assertTrue(record["backedUp"])
        self.assertEqual(verify.call_args.kwargs["credential_current_sign_count"], 4)
        self.assertTrue(verify.call_args.kwargs["require_user_verification"])

        with self.assertRaises(PasskeyChallengeError):
            self.auth.finish_authentication("mine", options["challengeId"], assertion)

    def test_failed_verification_also_consumes_challenge(self):
        options = self.auth.begin_registration("mine", principal="ben")
        with mock.patch("webauthn_auth.verify_registration_response", side_effect=ValueError("bad")):
            with self.assertRaises(PasskeyVerificationError):
                self.auth.finish_registration("mine", options["challengeId"], {})
        with self.assertRaises(PasskeyChallengeError):
            self.auth.finish_registration("mine", options["challengeId"], {})

    def test_expired_or_cross_realm_challenges_are_rejected(self):
        current_time = [10.0]
        auth = PasskeyAuth(
            self.state_dir,
            rp_id="term.example.test",
            rp_name="term terminal",
            expected_origin="https://term.example.test",
            challenge_ttl=5,
            clock=lambda: current_time[0],
        )
        options = auth.begin_registration("mine", principal="ben")
        with self.assertRaises(PasskeyChallengeError):
            auth.finish_registration("other", options["challengeId"], {})

        options = auth.begin_registration(
            "mine",
            principal="ben",
            binding="socket-a",
        )
        with self.assertRaises(PasskeyChallengeError):
            auth.finish_registration(
                "mine",
                options["challengeId"],
                {},
                binding="socket-b",
            )

        options = auth.begin_registration("mine", principal="ben")
        current_time[0] = 16.0
        with self.assertRaises(PasskeyChallengeError):
            auth.finish_registration("mine", options["challengeId"], {})

    def test_revocation_is_individual_and_can_cascade(self):
        self.register()
        self.assertEqual(len(self.auth.list_credentials("mine")), 1)
        self.assertFalse(
            self.auth.revoke_credential(
                "other",
                CREDENTIAL_ID_B64,
                principal="ben",
            )
        )
        self.assertTrue(
            self.auth.revoke_credential(
                "mine",
                CREDENTIAL_ID_B64,
                principal="ben",
            )
        )
        self.assertFalse(
            self.auth.revoke_credential(
                "mine",
                CREDENTIAL_ID_B64,
                principal="ben",
            )
        )

        self.register(label="Replacement")
        self.assertEqual(
            self.auth.revoke_all_credentials("mine", principal="ben"),
            1,
        )
        self.assertEqual(
            self.auth.revoke_all_credentials("mine", principal="ben"),
            0,
        )
        self.assertEqual(self.auth.list_credentials("mine"), [])

    def test_revocation_requires_matching_principal(self):
        self.register()
        store_path = self.state_dir / "mine" / "device-keys.json"
        original = store_path.read_text()
        operations = (
            lambda: self.auth.revoke_credential("mine", CREDENTIAL_ID_B64),
            lambda: self.auth.revoke_all_credentials("mine"),
            lambda: self.auth.revoke_credential(
                "mine",
                CREDENTIAL_ID_B64,
                principal="",
            ),
            lambda: self.auth.revoke_all_credentials(
                "mine",
                principal="",
            ),
            lambda: self.auth.revoke_credential(
                "mine",
                CREDENTIAL_ID_B64,
                principal="other-user",
            ),
            lambda: self.auth.revoke_all_credentials(
                "mine",
                principal="other-user",
            ),
        )

        for operation in operations:
            with self.subTest(operation=operation), self.assertRaises(
                PasskeyVerificationError
            ) as raised:
                operation()
            self.assertEqual(str(raised.exception), "Passkey operation failed.")
            self.assertEqual(store_path.read_text(), original)

        self.assertTrue(
            self.auth.revoke_credential(
                "mine",
                CREDENTIAL_ID_B64,
                principal="ben",
            )
        )
        self.register(label="Replacement")
        self.assertEqual(
            self.auth.revoke_all_credentials("mine", principal="ben"),
            1,
        )

    def test_stable_realm_user_id_and_principal_survive_revoke_all(self):
        options, _, _ = self.register()
        original_user_id = options["options"]["user"]["id"]
        self.assertEqual(original_user_id, b64url(self.auth.user_id_for_realm("mine")))

        self.assertEqual(
            self.auth.revoke_all_credentials("mine", principal="ben"),
            1,
        )
        next_options = self.auth.begin_registration("mine", principal="ben")
        self.assertEqual(next_options["options"]["user"]["id"], original_user_id)
        self.assertNotEqual(
            b64url(self.auth.user_id_for_realm("other")),
            original_user_id,
        )
        with self.assertRaises(ValueError):
            self.auth.begin_registration("mine", principal="different-user")
        with self.assertRaises(ValueError):
            self.auth.begin_authentication("mine", principal="different-user")

    def test_real_p256_registration_and_authentication_ceremony(self):
        private_key, credential_id, registered = self.register_real()
        self.assertEqual(registered["credentialId"], b64url(credential_id))
        self.assertEqual(registered["signCount"], 0)

        authenticated = self.authenticate_real(private_key, credential_id, sign_count=1)
        self.assertEqual(authenticated["principal"], "ben")
        self.assertEqual(authenticated["signCount"], 1)

    def test_non_incrementing_sign_count_is_rejected(self):
        private_key, credential_id, _ = self.register_real()
        self.authenticate_real(private_key, credential_id, sign_count=1)
        with self.assertRaises(PasskeyVerificationError):
            self.authenticate_real(private_key, credential_id, sign_count=1)

    def test_authentication_after_revoke_is_rejected(self):
        private_key, credential_id, _ = self.register_real()
        self.assertTrue(
            self.auth.revoke_credential(
                "mine",
                b64url(credential_id),
                principal="ben",
            )
        )
        with self.assertRaises(PasskeyVerificationError):
            self.authenticate_real(private_key, credential_id, sign_count=1)

    def test_realm_paths_cannot_escape_state_directory(self):
        for realm in ("", "../mine", "mine/other", ".hidden"):
            with self.subTest(realm=realm), self.assertRaises(ValueError):
                self.auth.list_credentials(realm)

    def test_corrupt_store_fails_closed(self):
        path = self.state_dir / "mine" / "device-keys.json"
        path.parent.mkdir(parents=True)
        path.write_text("not json")
        with self.assertRaises(PasskeyStoreError):
            self.auth.begin_authentication("mine", principal="ben")


class AuthResolutionTest(unittest.TestCase):
    def test_precedence_is_identity_then_passkey_then_token(self):
        result = resolve_auth_method(
            identity_principal="identity-user",
            passkey_principal="passkey-user",
            token_principal="token-user",
        )
        self.assertEqual((result.method, result.principal), ("identity", "identity-user"))
        self.assertTrue(result.terminal_authorized)
        self.assertFalse(result.bootstrap_only)
        self.assertEqual(result.require_terminal_authorization(), "identity-user")

        result = resolve_auth_method(passkey_principal="", token_principal="token-user")
        self.assertEqual((result.method, result.principal), ("passkey", ""))
        self.assertTrue(result.terminal_authorized)
        self.assertFalse(result.bootstrap_only)
        self.assertEqual(result.require_terminal_authorization(), "")

        result = resolve_auth_method(token_principal="token-user")
        self.assertEqual((result.method, result.principal), ("token", "token-user"))
        self.assertFalse(result.terminal_authorized)
        self.assertTrue(result.bootstrap_only)
        with self.assertRaises(AuthenticationRejected):
            result.require_terminal_authorization()

    def test_missing_auth_is_rejected(self):
        with self.assertRaises(AuthenticationRejected):
            resolve_auth_method()


if __name__ == "__main__":
    unittest.main()
