# Operations Runbook

This is the entry point for changing or recovering a running Mobile Terminal installation. It is deliberately strict: the terminal is an access path, so an unnecessary restart can disconnect the operator who is trying to repair it.

## Operational boundaries

Mobile Terminal consists of four independent layers:

1. The Mobile Terminal Python service and browser assets.
2. A tmux server owned by the target OS user.
3. SSH/mosh/Tailscale transport, which is not managed by this repository.
4. Optional Claude/Codex lifecycle hooks and transcript-backed copy authority.

A Mobile Terminal deployment may update layer 1 and, when explicitly requested, layer 4. It must not restart or reconfigure layers 2 or 3.

## Safe local workflow

1. Confirm the branch and tracked state:
   ```bash
   git branch --show-current
   git status --short
   git diff --check
   ```
2. Run focused tests through the isolated repository runner, for example:
   ```bash
   scripts/test.sh tests.test_terminal_authority.GeometryAuthorityTest
   ```
3. Run syntax checks and the complete suite through the same runner:
   ```bash
   scripts/test.sh --all
   ```
   The runner requires `.venv/bin/python`, removes ambient Mobile Terminal secrets and tmux identity, and gives every test run a private tmux socket root.
4. Commit one subsystem at a time.
5. Treat the local ph checkout as the canary. Restart only its user service when the reviewed change is ready; `deploy.sh` does not remote-copy onto ph.
6. Run `scripts/verify-runtime.sh`.
7. Obtain explicit live ph/iPhone acceptance before the next rollout gate.
8. Preflight the next exact target or targets with `./deploy.sh --dry-run TARGET [...]`. Apply exactly one ps target with `--apply --confirm-ph-accepted`; after explicit ps acceptance, apply each lat target separately with both `--confirm-ph-accepted` and `--confirm-ps-accepted`, stopping for live acceptance between targets.

A clean tracked tree is required for deployment. Pre-existing untracked local files are not deployment inputs and must remain untouched.

`scripts/verify-runtime.sh` is read-only, but it does query the live local user service, loopback health endpoint, current tmux server's global policy and window metadata, up to 200 recent service journal lines for aggregate error counts, the two named non-secret env keys used in its report, and tagged hook counts. It never attaches a tmux client, changes tmux state, or prints journal text, tokens, provider content, credentials, or complete environments. Manual-window count is informational because unrelated intentional manual windows must not be cleared.

## Safe service operations

Use only the service listed for the target in [`deployment.md`](deployment.md). Never use broad patterns such as `systemctl restart 'mobile-terminal*'`, `pkill`, or a cgroup-wide kill.

Local ph restart:

```bash
systemctl --user restart mobile-terminal.service
systemctl --user is-active mobile-terminal.service
```

Do not print `systemctl show ... Environment`, source the complete env file for diagnostics, or read `/proc/<pid>/environ`. Those paths can expose secrets or fail under procfs hardening. `scripts/verify-runtime.sh` reads only named non-secret values.

## Provider authority rollout

Use [`scripts/provider-mode.sh`](../scripts/provider-mode.sh), not an ad hoc env editor.

```bash
scripts/provider-mode.sh status
scripts/provider-mode.sh shadow --apply
scripts/provider-mode.sh enforce --apply --confirm-enforce
scripts/provider-mode.sh off --apply
```

Rules:

- Start in `shadow`.
- Validate reason counters and a real ph/iPhone selection.
- Enable `enforce` only after shadow passes.
- If valid Copy and To-tab requests fail closed, immediately return to `shadow` while diagnosing.
- Copy and To-tab intentionally use the same authority request; a shared failure is one backend problem, not two frontend bugs.

Without `--apply`, a requested mode change is a non-mutating preview. With `--apply`, the script backs up the env file in place, atomically updates only `MOBILE_TERMINAL_PROVIDER_AUTHORITY`, restarts only `mobile-terminal.service`, waits for both active service state and loopback `/health`, and restores the old env if verification fails.

## tmux geometry recovery

Check policy without changing the live server:

```bash
tmux show-options -gv window-size
tmux list-windows -a -F '#{session_name}:#{window_index} #{window_size}'
```

The global value must be `latest`. A blank per-window value means it inherits the global policy. `manual` is a persistent override, usually left by `resize-window`; see [`tmux-sizing.md`](tmux-sizing.md).

Do not clear every manual window globally. Mobile Terminal clears only its current target when that client claims geometry. An unrelated intentionally manual window must remain manual.

## Failure discipline

After the second failed operational attempt:

1. Stop mutating the system.
2. Record the exact command and error without secrets.
3. Identify the assumption that failed.
4. Add a regression test or script guard before retrying.

Common examples:

- `/proc/<pid>/environ` permission denied: verify the named config key from the owner-only env file instead.
- Provider `Terminal changed; select again.`: collect a bounded reason code, do not weaken fail-closed fallback.
- tmux remains phone-sized on desktop: look for `window-size=manual`; do not issue another `resize-window`.
- Full suite attaches to a live tmux session: stop it and isolate the test socket before rerunning.

## Rollback

Code rollback is a reviewed prior commit for ph. Remote `deploy.sh` targets retain timestamped copies of the complete manifest closure under `.mobile-terminal-deploy-backups` and automatically restore files plus service health when activation fails. Provider-mode rollback is:

```bash
scripts/provider-mode.sh shadow --apply
```

A rollback must restart only the Mobile Terminal service and must be followed by:

```bash
scripts/verify-runtime.sh
```
