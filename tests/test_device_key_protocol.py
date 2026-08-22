import base64
import json
import re
import subprocess
import unittest
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from webauthn_auth import (
    DEVICE_ENROLLMENT_PURPOSE,
    PendingDeviceEnrollment,
    device_authentication_transcript,
    device_enrollment_transcript,
    verify_device_key_signature,
)


def public_spki(private_key):
    return base64.b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    ).decode("ascii")


def raw_signature(private_key, transcript):
    der_signature = private_key.sign(transcript, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_signature)
    return base64.b64encode(
        r.to_bytes(32, "big") + s.to_bytes(32, "big")
    ).decode("ascii")


class DeviceKeyProtocolTest(unittest.TestCase):
    def test_node_webcrypto_transcript_and_signature_verify_in_python(self):
        source = (Path(__file__).parents[1] / "static" / "app.js").read_text()
        constant = re.search(r'  const DEVICE_AUTH_PURPOSE = ".*?";', source)
        transcript_function = re.search(
            r"  function deviceTranscript\(.*?\n  \}", source, re.DOTALL
        )
        authentication_function = re.search(
            r"  function deviceAuthenticationTranscript\(.*?\n  \}",
            source,
            re.DOTALL,
        )
        self.assertIsNotNone(constant)
        self.assertIsNotNone(transcript_function)
        self.assertIsNotNone(authentication_function)
        script = "\n".join(
            (constant.group(0), transcript_function.group(0), authentication_function.group(0))
        ) + r'''
const { webcrypto } = require("node:crypto");
(async () => {
  const transcript = deviceAuthenticationTranscript(
    "terminal.example.ts.net",
    "mine",
    "powerhouse",
    "nonce-value",
  );
  const keys = await webcrypto.subtle.generateKey(
    { name: "ECDSA", namedCurve: "P-256" },
    false,
    ["sign", "verify"],
  );
  const publicKey = await webcrypto.subtle.exportKey("spki", keys.publicKey);
  const signature = await webcrypto.subtle.sign(
    { name: "ECDSA", hash: "SHA-256" },
    keys.privateKey,
    transcript,
  );
  process.stdout.write(JSON.stringify({
    transcript: Buffer.from(transcript).toString("base64"),
    publicKey: Buffer.from(publicKey).toString("base64"),
    signature: Buffer.from(signature).toString("base64"),
  }));
})();
'''
        result = subprocess.run(
            ["node", "-e", script],
            check=True,
            capture_output=True,
            text=True,
        )
        proof = json.loads(result.stdout)
        transcript = device_authentication_transcript(
            "terminal.example.ts.net",
            "mine",
            "powerhouse",
            "nonce-value",
        )

        self.assertEqual(base64.b64decode(proof["transcript"]), transcript)
        self.assertTrue(
            verify_device_key_signature(
                proof["publicKey"], transcript, proof["signature"]
            )
        )
        self.assertFalse(
            verify_device_key_signature(
                proof["publicKey"],
                device_authentication_transcript(
                    "terminal.example.ts.net", "other", "powerhouse", "nonce-value"
                ),
                proof["signature"],
            )
        )
        self.assertFalse(
            verify_device_key_signature(
                proof["publicKey"],
                device_enrollment_transcript(
                    "terminal.example.ts.net",
                    "mine",
                    "powerhouse",
                    "enrollment-id",
                    "nonce-value",
                    "device-id",
                    proof["publicKey"],
                ),
                proof["signature"],
            )
        )

    def enrollment_proof(self, ticket):
        private_key = ec.generate_private_key(ec.SECP256R1())
        public_key = public_spki(private_key)
        transcript = device_enrollment_transcript(
            ticket.rp_id,
            ticket.realm,
            ticket.profile,
            ticket.enrollment_id,
            ticket.nonce,
            ticket.device_id,
            public_key,
        )
        return {
            **ticket.message(),
            "type": "register-key",
            "deviceId": ticket.device_id,
            "publicKey": public_key,
            "signature": raw_signature(private_key, transcript),
        }

    def test_enrollment_proof_success_bad_signature_mismatch_and_expiry(self):
        ticket = PendingDeviceEnrollment.issue(
            rp_id="terminal.example.ts.net",
            realm="mine",
            profile="powerhouse",
            device_id="device-id",
            principal="ben",
            now=100.0,
        )
        payload = self.enrollment_proof(ticket)

        self.assertTrue(ticket.verify(payload, now=ticket.expires_at - 0.001))
        self.assertFalse(
            ticket.verify({**payload, "signature": base64.b64encode(b"\0" * 64).decode()}, now=101.0)
        )
        for field, value in (
            ("enrollmentId", "other-id"),
            ("nonce", "other-nonce"),
            ("realm", "other"),
            ("profile", "other"),
            ("deviceId", "other-device"),
        ):
            with self.subTest(field=field):
                self.assertFalse(ticket.verify({**payload, field: value}, now=101.0))
        self.assertFalse(ticket.verify(payload, now=ticket.expires_at))
        self.assertFalse(ticket.verify(payload, now=ticket.expires_at + 0.001))

    def test_transcript_rejects_ambiguous_fields(self):
        with self.assertRaises(ValueError):
            device_authentication_transcript("rp", "mine\0other", "profile", "nonce")
        self.assertNotEqual(
            device_authentication_transcript("rp", "mine", "profile", "nonce"),
            DEVICE_ENROLLMENT_PURPOSE.encode(),
        )


if __name__ == "__main__":
    unittest.main()
