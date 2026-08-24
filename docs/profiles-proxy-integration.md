# Profiles proxy integration

`MOBILE_TERMINAL_CONFIG` selects the runtime role. With no value, or with a config whose
`mode` is `single` or `backend`, `server.py` follows the existing environment/CLI backend
path. A config with `mode: "proxy"` starts the profile proxy instead.

## ps layout

Start the Powerhouse backend as the `powerhouse` OS user with an environment file like:

```sh
MOBILE_TERMINAL_HOST=127.0.0.1
MOBILE_TERMINAL_PORT=8090
MOBILE_TERMINAL_SESSION=mobile-terminal
MOBILE_TERMINAL_NO_TOKEN=true
MOBILE_TERMINAL_INTERNAL_TOKEN=<powerhouse-only-random-internal-token>
```

The internal token mode refuses to start unless the backend host is loopback. Backend
instances started through the systemd template also refuse to start when the internal token
is missing. The backend rejects WebSocket connections without the token and only accepts
the forwarded principal after both the loopback-source and token checks pass.

The proxy environment is:

```sh
MOBILE_TERMINAL_CONFIG=/absolute/path/to/docs/ps-proxy.example.json
MOBILE_TERMINAL_TOKEN=<external-login-token>
MOBILE_TERMINAL_INTERNAL_TOKEN=<legacy-shared-default-token>
MOBILE_TERMINAL_INTERNAL_TOKEN_POWERHOUSE=<powerhouse-only-random-internal-token>
```

`docs/ps-proxy.example.json` intentionally leaves Behuman as a down/stub profile. It stays
visible in the dropdown and can be selected without dropping the browser connection. This
repository does not define or manage a Behuman backend target or service. Establish and review
that separate OS-user boundary before adding a loopback backend URL and distinct internal token.

Relative `stateDir` values resolve from the directory containing the JSON config. The proxy
stores global UI settings in `stateDir/settings.json`; open terminal tabs and the active
session remain namespaced per profile in the browser and in each isolated backend. Profile
backend URLs and the proxy listen address must be loopback.

The root `internalToken` / `internalTokenEnv` remains the shared default for compatibility.
Set `internalToken` or `internalTokenEnv` on each profile to isolate backend hops; explicit
overrides must differ from the root token and from every other profile override. Each backend's
standard `MOBILE_TERMINAL_INTERNAL_TOKEN` must equal only its profile's effective token. The
example uses a distinct environment variable for the managed Powerhouse backend. If the Behuman
stub is replaced later, its backend must use a different variable and credential so it cannot
silently reuse Powerhouse's token. Tokens can be supplied directly, but environment
references keep secrets out of JSON. If a JSON file does contain direct tokens, the loader reduces
its mode to `0600`; environment files must also be owner-only:

```sh
chmod 600 /path/to/proxy-config.json /path/to/proxy.env /path/to/backend.env
```

Terminal and tmux child processes do not receive access tokens or `MOBILE_TERMINAL_INTERNAL_TOKEN*` values. This applies to newly created sessions; restart any sessions that predate this protection if they may have inherited one.

## systemd template

`systemd/mobile-terminal@.service` is the system-level OS-user template. Replace
`@WORKDIR@`, `@PYTHON@`, and `@ENV_DIR@` when installing it. Each instance requires
`@ENV_DIR@/<user>.env`, forces the backend listener to `127.0.0.1`, and requires a non-empty
`MOBILE_TERMINAL_INTERNAL_TOKEN` before `server.py` starts:

For a separately reviewed managed backend, instantiate only its exact unit, for example:

```sh
systemctl enable --now mobile-terminal@powerhouse.service
```

This repository does not currently define or manage a Behuman instance.

The proxy may use the existing single-process service template with
`MOBILE_TERMINAL_CONFIG` set in its environment file. Tailscale Serve should target the
proxy's loopback listen address, never either backend port.

## Authentication seam

The authenticator contract, failure behavior, custom-loader setting, and identity-header caveats
are documented once in [`INTEGRATION-proxy-auth.md`](../INTEGRATION-proxy-auth.md).
