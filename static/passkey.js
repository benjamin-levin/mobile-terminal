(function (root, factory) {
  const api = factory(root);
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  }
  root.MobileTerminalPasskeys = api;
})(typeof window !== "undefined" ? window : globalThis, function (root) {
  "use strict";

  const REGISTER_OPTIONS_MESSAGE = "webauthn-register-options";
  const REGISTER_MESSAGE = "webauthn-register";
  const AUTH_OPTIONS_MESSAGE = "webauthn-auth-options";
  const AUTH_MESSAGE = "webauthn-auth";

  function base64urlOf(value) {
    const bytes = value instanceof Uint8Array ? value : new Uint8Array(value);
    let binary = "";
    for (let offset = 0; offset < bytes.length; offset += 0x8000) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000));
    }
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function base64urlToBytes(value) {
    if (typeof value !== "string" || !value) {
      throw new TypeError("Expected a base64url string.");
    }
    const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
    const binary = atob(normalized + "=".repeat((4 - (normalized.length % 4)) % 4));
    return Uint8Array.from(binary, (character) => character.charCodeAt(0));
  }

  function decodeDescriptors(descriptors) {
    if (!Array.isArray(descriptors)) {
      return descriptors;
    }
    return descriptors.map((descriptor) => ({
      ...descriptor,
      id: base64urlToBytes(descriptor.id),
    }));
  }

  function decodeCreationOptions(options) {
    if (!options || !options.challenge || !options.user || !options.user.id) {
      throw new TypeError("Invalid passkey registration options.");
    }
    return {
      ...options,
      challenge: base64urlToBytes(options.challenge),
      user: { ...options.user, id: base64urlToBytes(options.user.id) },
      excludeCredentials: decodeDescriptors(options.excludeCredentials),
    };
  }

  function decodeRequestOptions(options) {
    if (!options || !options.challenge) {
      throw new TypeError("Invalid passkey authentication options.");
    }
    return {
      ...options,
      challenge: base64urlToBytes(options.challenge),
      allowCredentials: decodeDescriptors(options.allowCredentials),
    };
  }

  function credentialBase(credential) {
    return {
      id: credential.id,
      rawId: base64urlOf(credential.rawId),
      type: credential.type,
      clientExtensionResults: credential.getClientExtensionResults
        ? credential.getClientExtensionResults()
        : {},
      authenticatorAttachment: credential.authenticatorAttachment || null,
    };
  }

  function registrationCredentialToJSON(credential) {
    const response = credential.response;
    const transports = response.getTransports ? response.getTransports() : [];
    return {
      ...credentialBase(credential),
      response: {
        clientDataJSON: base64urlOf(response.clientDataJSON),
        attestationObject: base64urlOf(response.attestationObject),
        transports,
      },
    };
  }

  function authenticationCredentialToJSON(credential) {
    const response = credential.response;
    return {
      ...credentialBase(credential),
      response: {
        clientDataJSON: base64urlOf(response.clientDataJSON),
        authenticatorData: base64urlOf(response.authenticatorData),
        signature: base64urlOf(response.signature),
        userHandle: response.userHandle ? base64urlOf(response.userHandle) : null,
      },
    };
  }

  function available() {
    return Boolean(
      root.PublicKeyCredential &&
      root.navigator &&
      root.navigator.credentials &&
      root.isSecureContext !== false,
    );
  }

  async function register(options) {
    if (!available()) {
      throw new Error("Passkeys are unavailable in this browser or origin.");
    }
    const credential = await root.navigator.credentials.create({
      publicKey: decodeCreationOptions(options),
    });
    if (!credential) {
      throw new Error("Passkey registration was cancelled.");
    }
    return registrationCredentialToJSON(credential);
  }

  async function authenticate(options) {
    if (!available()) {
      throw new Error("Passkeys are unavailable in this browser or origin.");
    }
    const credential = await root.navigator.credentials.get({
      publicKey: decodeRequestOptions(options),
    });
    if (!credential) {
      throw new Error("Passkey authentication was cancelled.");
    }
    return authenticationCredentialToJSON(credential);
  }

  async function handleMessage(payload, send) {
    if (!payload || typeof send !== "function") {
      throw new TypeError("Passkey message handling requires a payload and send function.");
    }
    if (payload.type === REGISTER_OPTIONS_MESSAGE) {
      const attestation = await register(payload.options);
      await send({
        type: REGISTER_MESSAGE,
        challengeId: payload.challengeId,
        attestation,
      });
      return true;
    }
    if (payload.type === AUTH_OPTIONS_MESSAGE) {
      const assertion = await authenticate(payload.options);
      await send({
        type: AUTH_MESSAGE,
        challengeId: payload.challengeId,
        assertion,
      });
      return true;
    }
    return false;
  }

  return Object.freeze({
    REGISTER_OPTIONS_MESSAGE,
    REGISTER_MESSAGE,
    AUTH_OPTIONS_MESSAGE,
    AUTH_MESSAGE,
    available,
    register,
    authenticate,
    handleMessage,
    decodeCreationOptions,
    decodeRequestOptions,
    registrationCredentialToJSON,
    authenticationCredentialToJSON,
  });
});
