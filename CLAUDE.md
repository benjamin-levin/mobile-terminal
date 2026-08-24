# Mobile Terminal repository instructions

## Start here

Before changing a running installation, read [`docs/RUNBOOK.md`](docs/RUNBOOK.md). For tmux geometry, provider-backed copy, or fleet deployment, also read the matching document under `docs/`.

## Hard safety rules

- Never restart, stop, reconfigure, or kill `sshd`, mosh, a tmux server, Tailscale, or an unrelated service while deploying Mobile Terminal.
- Restart only the exact Mobile Terminal unit named for the target in `docs/deployment.md`. Never restart lat's Ubuntu hub.
- Never use `tmux resize-window` in production code or operational scripts. It changes the window to persistent `window-size=manual`. Keep global `window-size latest` and use the sequence documented in `docs/tmux-sizing.md`.
- Never run tests against the user's live tmux socket. Use `scripts/test.sh`; every integration test must create an explicit private `tmux -S <socket>` server and remove it afterward.
- Never print, copy, log, or inspect `MOBILE_TERMINAL_TOKEN`, provider credentials, authentication files, or complete service environments. Read only named non-secret keys from `mobile-terminal.env`. Do not use `/proc/<pid>/environ` for verification.
- Do not copy one OS user's provider credentials to another user. Provider login is performed interactively by that OS user.
- Do not touch pre-existing untracked files. Temporary work belongs in the session scratchpad, not the repository or `/tmp`.

## Change and rollout rules

- Keep tmux sizing, provider authority, authentication, hooks, and deployment changes in separate commits.
- Run focused tests, JavaScript/Python/shell syntax checks, and the full suite through `scripts/test.sh`, then run `git diff --check` before committing.
- Push only the feature branch requested by the user; never push `master` without explicit permission.
- Provider rollout is `off -> shadow -> enforce`. Do not jump directly to enforcement. Shadow diagnostics and live ph/iPhone acceptance are required first.
- Fleet rollout order is ph, the exact managed ps target in `deployment-manifest.sh`, then both lat users. Behuman is not a managed deployment target. Stop at each acceptance gate. Do not interpret an automated notification or a passing test as live user acceptance.
- After two failed operational attempts, stop retrying. Diagnose the failed assumption before issuing another mutation.
- Use checked-in scripts under `scripts/`; do not create ad hoc deployment scripts unless the checked-in path cannot represent the operation.
