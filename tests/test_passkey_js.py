import json
from pathlib import Path
import shutil
import subprocess
import unittest


PASSKEY_JS = Path(__file__).parents[1] / "static" / "passkey.js"


@unittest.skipUnless(shutil.which("node"), "node is required for passkey.js tests")
class PasskeyJavaScriptTest(unittest.TestCase):
    def test_registration_and_authentication_messages_are_browser_encoded(self):
        script = f"""
const assert = require("node:assert/strict");
const passkeys = require({json.dumps(str(PASSKEY_JS))});
const bytes = (...values) => new Uint8Array(values).buffer;
let createOptions;
let getOptions;
Object.defineProperty(globalThis, "PublicKeyCredential", {{ value: function () {{}}, configurable: true }});
Object.defineProperty(globalThis, "navigator", {{
  value: {{ credentials: {{
    create: async (options) => {{
      createOptions = options;
      return {{
        id: "registered-id",
        rawId: bytes(1, 2, 3),
        type: "public-key",
        authenticatorAttachment: "platform",
        getClientExtensionResults: () => ({{ credProps: {{ rk: true }} }}),
        response: {{
          clientDataJSON: bytes(4, 5),
          attestationObject: bytes(6, 7),
          getTransports: () => ["internal"],
        }},
      }};
    }},
    get: async (options) => {{
      getOptions = options;
      return {{
        id: "registered-id",
        rawId: bytes(1, 2, 3),
        type: "public-key",
        getClientExtensionResults: () => ({{}}),
        response: {{
          clientDataJSON: bytes(8),
          authenticatorData: bytes(9),
          signature: bytes(10),
          userHandle: bytes(11),
        }},
      }};
    }},
  }} }},
  configurable: true,
}});

(async () => {{
  const sent = [];
  assert.equal(await passkeys.handleMessage({{
    type: "webauthn-register-options",
    challengeId: "register-challenge",
    options: {{
      challenge: "AQI",
      user: {{ id: "AwQ", name: "ben", displayName: "Ben" }},
      rp: {{ id: "term.example.test", name: "term terminal" }},
      pubKeyCredParams: [{{ type: "public-key", alg: -7 }}],
      excludeCredentials: [{{ type: "public-key", id: "BQY" }}],
    }},
  }}, (message) => sent.push(message)), true);
  assert.deepEqual(Array.from(createOptions.publicKey.challenge), [1, 2]);
  assert.deepEqual(Array.from(createOptions.publicKey.user.id), [3, 4]);
  assert.deepEqual(Array.from(createOptions.publicKey.excludeCredentials[0].id), [5, 6]);
  assert.equal(sent[0].type, "webauthn-register");
  assert.equal(sent[0].challengeId, "register-challenge");
  assert.equal(sent[0].attestation.rawId, "AQID");
  assert.deepEqual(sent[0].attestation.response.transports, ["internal"]);

  assert.equal(await passkeys.handleMessage({{
    type: "webauthn-auth-options",
    challengeId: "auth-challenge",
    options: {{
      challenge: "DA0",
      rpId: "term.example.test",
      allowCredentials: [{{ type: "public-key", id: "AQID" }}],
    }},
  }}, (message) => sent.push(message)), true);
  assert.deepEqual(Array.from(getOptions.publicKey.challenge), [12, 13]);
  assert.deepEqual(Array.from(getOptions.publicKey.allowCredentials[0].id), [1, 2, 3]);
  assert.equal(sent[1].type, "webauthn-auth");
  assert.equal(sent[1].challengeId, "auth-challenge");
  assert.equal(sent[1].assertion.response.signature, "Cg");
  assert.equal(sent[1].assertion.response.userHandle, "Cw");
  assert.equal(await passkeys.handleMessage({{ type: "ready" }}, () => {{}}), false);
}})().catch((error) => {{
  console.error(error.stack || error);
  process.exit(1);
}});
"""
        result = subprocess.run(
            ["node", "-e", script],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
