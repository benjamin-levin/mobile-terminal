# Deployment and Fleet Topology

## Release gates

The enforced order is:

1. ph: tests, reviewed local service update, runtime verification, live iPhone acceptance.
2. ps: exact managed target `ps-powerhouse`, followed by explicit live ps acceptance.
3. lat: `lat-ben`, then `lat-bperritt`; verify each exact service and obtain live acceptance before progressing.
4. `mbp-powerhouse` only when explicitly requested.

Do not combine geometry rollout and provider enforcement into one acceptance gate. Deploy code first, verify tmux handoff, run provider mode in shadow, validate prefer with its visible raw-terminal fallback warning, then enable enforcement separately only if fail-closed behavior is required.

`deploy.sh` requires an explicit mode and exact per-user targets. It has no implicit fleet, `--all`, `ps`, or `lat` behavior. A dry run may preflight several exact targets, but each apply accepts exactly one target so live acceptance can occur before progression. Applying any ps or lat target requires `--confirm-ph-accepted`; applying lat additionally requires `--confirm-ps-accepted`. Dry runs require neither acceptance flag.

```bash
./deploy.sh --dry-run ps-powerhouse
./deploy.sh --apply --confirm-ph-accepted ps-powerhouse
./deploy.sh --dry-run lat-ben lat-bperritt
./deploy.sh --apply --confirm-ph-accepted --confirm-ps-accepted lat-ben
./deploy.sh --apply --confirm-ph-accepted --confirm-ps-accepted lat-bperritt
./deploy.sh --dry-run mbp-powerhouse
```

The ph canary is the local checkout and is intentionally not a remote-copy target. Run the test and local service workflow in [`RUNBOOK.md`](RUNBOOK.md), then obtain explicit live ph/iPhone acceptance before applying any ps or lat target.

A clean tracked tree is required for `--apply`; every archived deployment input must itself be tracked. `deployment-manifest.sh` is the single source of truth for the runtime closure and target topology. The tracked closure includes both dependency manifests, and `npm ci --ignore-scripts` materializes only the locked browser vendor assets inside the remote staging tree. Both modes validate every manifest file, Python and JavaScript syntax, imports and requirements, trusted SSH host keys, remote identity, repository ownership, target-specific virtualenv, exact service configuration, current service state, and baseline health. Every selected target must pass preflight before an apply copies anything to any host.

Authentication readiness is part of that preflight. It reads only the named non-secret
`MOBILE_TERMINAL_NO_TOKEN` and `MOBILE_TERMINAL_AUTH_MIGRATION` values; it never reads or
prints `MOBILE_TERMINAL_TOKEN`. Before the first deployment to an existing token-mode install,
run `./install.sh --apply --migrate-token-auth` as the target OS user. The explicit migration
replaces the old token line with a generated bootstrap token, records
`passkey-bootstrap-v1`, and keeps the env file owner-only without exposing either token value.
Existing no-token installs require no token migration.

Apply uploads the complete closure to a fresh remote staging directory, smoke-checks it with that target's repository virtualenv, and only then activates it. Activation retains a timestamped backup under `.mobile-terminal-deploy-backups`, replaces runtime files atomically one at a time, restarts only the allowlisted service, and checks service state plus `/health`. Any activation, restart, or health failure restores the full file set and restarts and health-checks the prior version. Target-owned env, state, credentials, provider authentication, and tmux are never copied or inspected.

## Target matrix

| Gate | OS user | Code path | Service | Notes |
|---|---|---|---|---|
| ph | `powerhouse` | `/home/powerhouse/mobile-terminal` | user `mobile-terminal.service` | Local canary; not a remote-copy target |
| ps | `powerhouse` | `/home/powerhouse/mobile-terminal` | user `mobile-terminal-proxy.service` | Exact target `ps-powerhouse`; proxy serves gen+bh backends |
| lat | `ben` | `/home/ben/mobile-terminal` | system `mobile-terminal@ben.service` | Exact target `lat-ben`; own repository virtualenv |
| lat | `bperritt` | `/home/bperritt/mobile-terminal` | system `mobile-terminal@bperritt.service` | Exact target `lat-bperritt`; own repository virtualenv |
| mbp | `powerhouse` | `/Users/powerhouse/mobile-terminal` | launchd `com.mobile-terminal.server` | Exact target `mbp-powerhouse` |

Never restart lat's Ubuntu hub. Never use a wildcard Mobile Terminal restart. Never restart tmux, SSH, mosh, Tailscale, or the reverse proxy as a side effect of backend deployment.

## ps boundary

`ps-powerhouse` is the only managed ps deployment target. The Behuman profile shown by the proxy is currently a down stub, not a managed Mobile Terminal backend; this repository does not define a Behuman deployment target or service unit. Do not infer one from design/example documents, create an ad hoc target, or claim Behuman acceptance as part of this rollout.

If a Behuman backend is established later, it must retain a separate OS-user, provider-login, env, token, transcript, credential, and service boundary. Nothing owned by `powerhouse` may be copied into it.

The public profile proxy is a separate layer. Updating the powerhouse backend does not authorize restarting or reconfiguring the proxy. See [`profiles-proxy-integration.md`](profiles-proxy-integration.md).

## lat boundary

The SSH staging user is `ubuntu`, but the backend processes are `ben` and `bperritt`. Install files with target-user ownership, compile/import as that target user, and restart only:

```text
mobile-terminal@ben.service
mobile-terminal@bperritt.service
```

Do not cgroup-kill, restart the Ubuntu hub, or touch an unrelated service. A failure for one user blocks progression to the other user until diagnosed.

## Provider files and hooks

The complete closure is defined only in `deployment-manifest.sh` and includes `requirements.txt`, browser assets, runtime modules, and these provider-authority files:

```text
provider_authority.py
provider_binding_hook.py
install_provider_hooks.py
server.py
```

Copying runtime files does not itself prove hooks are installed for every OS user. Install hooks as the target user with the repository virtualenv and the installer's explicit `--apply` flag, then verify only tagged hook counts—never hook payload credentials or provider auth files.

Use `scripts/provider-mode.sh` locally for provider state changes. On fleet users, apply the same `off -> shadow -> prefer -> enforce` validation sequence within that user's env/service boundary. Direct `off -> prefer` activation is supported when shadow validation was completed separately; `enforce` is accepted only from `prefer`.

## Verification

Local/ph:

```bash
scripts/verify-runtime.sh
```

Fleet verification must report only:

- exact deployed revision or file digest;
- service name and active state;
- health status;
- configured provider mode;
- tagged hook counts;
- tmux global policy and informational manual-override count;
- bounded error/reason counts.

Never print tokens, complete environments, provider text, selected text, credentials, or authentication files.

## Failed deployment

- If remote preflight fails, no selected target is staged or activated.
- If staging copy or staged smoke-check fails, the active tree and service are not changed.
- If activation, restart, or health fails, the script restores every manifest file from the retained deployment backup, restarts the prior version, verifies its service health, and stops.
- If rollback health also fails, stop immediately and diagnose from the exact unit; never broaden the restart.
- After two failed attempts, stop and identify the failed assumption before another mutation.
