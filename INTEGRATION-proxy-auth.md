# Proxy authenticator integration

The profile proxy has one authentication boundary:

```python
def authenticate(request: AuthenticationRequest) -> str | None:
    ...
```

Return a non-empty principal string to accept the request. Return `None` to reject it. The
callable may be synchronous or return an awaitable with the same result. Exceptions, empty
strings, and non-string results fail closed.

`AuthenticationRequest` is defined in `proxy_auth.py` and contains:

- `realm`: the selected profile's configured `authRealm`.
- `credentials`: the decoded client `auth` frame. Treat every value as untrusted input.
- `headers`: the WebSocket upgrade request headers.
- `remote_address`: the proxy's direct peer address.

The built-in `TokenAuthenticator` compares the submitted token in constant time and returns the
realm's configured `principal`. Configure another implementation with:

```sh
MOBILE_TERMINAL_AUTHENTICATOR=package.module:callable
```

The module must be importable by the service's Python interpreter. Do not log credentials,
tokens, authenticator return values, or internal-hop headers. Keep authorization policy inside
the authenticator: a principal identifies the accepted caller but does not grant access to a
different auth realm.

After authentication, the proxy sends the principal to only the selected loopback backend. That
hop is authenticated with the profile's effective internal token. Use a distinct per-profile
`internalToken` or `internalTokenEnv`; the root token remains only a compatibility default.

## Forwarded identity headers

`trustIdentity` is off by default. When enabled, the built-in proxy path accepts
`Tailscale-User-Login` only for a loopback, non-Funnel request whose browser `Origin` matches the
request `Host`. The origin check blocks cross-site browser WebSocket use of an identity-bearing
Tailscale Serve endpoint.

This does not make a TCP loopback header cryptographically authentic. Another local process can
forge the source address, `Host`, `Origin`, and `Tailscale-*` headers. Mobile Terminal cannot
distinguish that process from the local reverse proxy over the current TCP integration. Enable
`trustIdentity` only when local processes and users are inside the trust boundary, and use token
or device-key authentication when that assumption is not acceptable. A custom authenticator must
not trust forwarded identity headers unless it independently authenticates the proxy-to-app hop.

`deviceKeyAuth` is currently a schema-compatible capability flag; the profile proxy advertises
token authentication. Adding passkey or device-key support should be done behind this same
`authenticate(request) -> principal | None` contract so profile routing and backend isolation do
not change.
