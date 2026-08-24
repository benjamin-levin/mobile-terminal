import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class OperationalGuardrailsTest(unittest.TestCase):
    def source(self, relative):
        return (ROOT / relative).read_text()

    def test_shell_entrypoints_parse(self):
        for relative in (
            "collect-access.sh",
            "deploy.sh",
            "deployment-manifest.sh",
            "install.sh",
            "ps-proxy-up.sh",
            "run.sh",
            "scripts/deploy-remote.sh",
            "scripts/provider-mode.sh",
            "scripts/test.sh",
            "scripts/verify-runtime.sh",
        ):
            with self.subTest(relative=relative):
                subprocess.run(["bash", "-n", ROOT / relative], check=True)

    def fake_deploy_environment(self, temporary):
        fake_bin = Path(temporary) / "bin"
        fake_bin.mkdir()
        log = Path(temporary) / "commands.log"
        for name, source in {
            "git": "#!/usr/bin/env bash\nexit 0\n",
            "scp": """#!/usr/bin/env bash
printf 'scp %s\\n' "$*" >>"$FAKE_COMMAND_LOG"
[[ "${FAKE_SCP_FAIL:-}" != 1 ]]
""",
            "ssh": """#!/usr/bin/env bash
printf 'ssh %s\\n' "$*" >>"$FAKE_COMMAND_LOG"
case " $* " in
  *" preflight "*)
    [[ " $* " != *" ${FAKE_PREFLIGHT_FAIL:-__none__} "* ]]
    ;;
  *" prepare "*) printf '%s\\n' "$HOME/.mobile-terminal-deploy.fake" ;;
  *" smoke "*) [[ "${FAKE_SMOKE_FAIL:-}" != 1 ]] ;;
  *" activate "*) [[ "${FAKE_ACTIVATE_FAIL:-}" != 1 ]] ;;
  *" cleanup "*) ;;
esac
""",
        }.items():
            path = fake_bin / name
            path.write_text(source)
            path.chmod(0o755)
        env = os.environ.copy()
        env["PATH"] = f"{fake_bin}:{env['PATH']}"
        env["FAKE_COMMAND_LOG"] = str(log)
        return env, log

    def test_deploy_requires_explicit_mode_target_and_ph_gate(self):
        missing_mode = subprocess.run(
            ["bash", ROOT / "deploy.sh", "ps-powerhouse"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(missing_mode.returncode, 2)
        self.assertIn("use --dry-run or --apply", missing_mode.stderr)

        missing_target = subprocess.run(
            ["bash", ROOT / "deploy.sh", "--dry-run"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(missing_target.returncode, 2)
        self.assertIn("Refusing implicit fleet deployment", missing_target.stderr)

        ambiguous = subprocess.run(
            ["bash", ROOT / "deploy.sh", "--dry-run", "lat"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(ambiguous.returncode, 2)
        self.assertIn("Unknown exact target: lat", ambiguous.stderr)

        ungated = subprocess.run(
            ["bash", ROOT / "deploy.sh", "--apply", "ps-powerhouse"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(ungated.returncode, 1)
        self.assertIn("--confirm-ph-accepted", ungated.stderr)

        lat_without_ps_gate = subprocess.run(
            [
                "bash",
                ROOT / "deploy.sh",
                "--apply",
                "--confirm-ph-accepted",
                "lat-ben",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(lat_without_ps_gate.returncode, 1)
        self.assertIn("--confirm-ps-accepted", lat_without_ps_gate.stderr)

    def test_dry_run_performs_remote_preflight_without_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            env, log = self.fake_deploy_environment(temporary)
            result = subprocess.run(
                ["bash", ROOT / "deploy.sh", "--dry-run", "lat-ben"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = log.read_text()
            self.assertIn("StrictHostKeyChecking=yes", commands)
            self.assertIn("preflight ubuntu ben /home/ben/mobile-terminal", commands)
            self.assertIn("/home/ben/mobile-terminal/.venv/bin/python", commands)
            self.assertNotIn("scp ", commands)
            self.assertNotIn(" activate ", commands)

    def test_apply_refuses_to_cross_live_acceptance_gates(self):
        with tempfile.TemporaryDirectory() as temporary:
            env, log = self.fake_deploy_environment(temporary)
            result = subprocess.run(
                [
                    "bash",
                    ROOT / "deploy.sh",
                    "--apply",
                    "--confirm-ph-accepted",
                    "--confirm-ps-accepted",
                    "ps-powerhouse",
                    "lat-ben",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("Refusing multi-target apply", result.stderr)
            self.assertFalse(log.exists())

    def test_apply_preflights_before_staging_exact_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            env, log = self.fake_deploy_environment(temporary)
            result = subprocess.run(
                [
                    "bash",
                    ROOT / "deploy.sh",
                    "--apply",
                    "--confirm-ph-accepted",
                    "--confirm-ps-accepted",
                    "lat-bperritt",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            commands = log.read_text().splitlines()
            preflight = next(i for i, command in enumerate(commands) if " preflight " in command)
            copy = next(i for i, command in enumerate(commands) if command.startswith("scp "))
            self.assertLess(preflight, copy)
            self.assertTrue(any(" prepare --apply" in command for command in commands))
            self.assertTrue(any(" smoke --apply" in command for command in commands))
            self.assertTrue(any(" activate --apply" in command for command in commands))
            activation = next(command for command in commands if " activate " in command)
            self.assertIn("mobile-terminal@bperritt.service", activation)

    def test_unreachable_requested_target_fails_before_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            env, log = self.fake_deploy_environment(temporary)
            env["FAKE_PREFLIGHT_FAIL"] = "mobile-terminal@ben.service"
            result = subprocess.run(
                ["bash", ROOT / "deploy.sh", "--dry-run", "lat-ben"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("no target was staged or activated", result.stderr)
            self.assertNotIn("scp ", log.read_text())

    def test_remote_helper_refuses_lat_ubuntu_hub_service(self):
        result = subprocess.run(
            [
                "bash",
                ROOT / "scripts/deploy-remote.sh",
                "preflight",
                "ubuntu",
                "ubuntu",
                "/home/ubuntu/mobile-terminal",
                "/home/ubuntu/mobile-terminal/.venv/bin/python",
                "system-systemd",
                "mobile-terminal.service",
                "8085",
                "websockets",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("service is not allowlisted", result.stderr)

    def test_remote_preflight_uses_named_redacted_readiness_and_service_queries(self):
        helper = self.source("scripts/deploy-remote.sh")
        self.assertIn(
            'systemctl show --property=LoadState --value "$service"',
            helper,
        )
        self.assertNotIn('systemctl show "$service"', helper)
        self.assertIn("MOBILE_TERMINAL_AUTH_MIGRATION", helper)
        self.assertIn("MOBILE_TERMINAL_NO_TOKEN", helper)
        self.assertNotIn("MOBILE_TERMINAL_TOKEN", helper)
        self.assertIn("--migrate-token-auth", helper)

    def test_remote_mutations_require_apply_and_revalidate_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            env = os.environ.copy()
            env["HOME"] = temporary
            missing_apply = subprocess.run(
                ["bash", ROOT / "scripts/deploy-remote.sh", "prepare"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(missing_apply.returncode, 1)
            self.assertIn("requires explicit --apply", missing_apply.stderr)
            self.assertEqual(list(Path(temporary).iterdir()), [])

            bad_service = subprocess.run(
                [
                    "bash",
                    ROOT / "scripts/deploy-remote.sh",
                    "activate",
                    "--apply",
                    f"{temporary}/.mobile-terminal-deploy.fake",
                    "powerhouse",
                    f"{temporary}/mobile-terminal",
                    f"{temporary}/mobile-terminal/.venv/bin/python",
                    "user-systemd",
                    "ssh.service",
                    "8085",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(bad_service.returncode, 1)
            self.assertIn("service is not allowlisted", bad_service.stderr)

    def test_remote_activation_rolls_back_files_and_service_health(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "home"
            repo = home / "mobile-terminal"
            stage = home / ".mobile-terminal-deploy.test"
            fake_bin = root / "bin"
            repo.mkdir(parents=True)
            (stage / "tree").mkdir(parents=True)
            fake_bin.mkdir()
            (repo / "server.py").write_text("old server\n")
            (repo / "requirements.txt").write_text("old requirements\n")
            (stage / "tree" / "server.py").write_text("new server\n")
            (stage / "tree" / "requirements.txt").write_text("new requirements\n")
            (stage / "tree" / "deployment-manifest.sh").write_text(
                "DEPLOY_FILES=(requirements.txt server.py)\n"
                "DEPLOY_GENERATED_FILES=()\n"
            )
            for name, source in {
                "systemctl": "#!/usr/bin/env bash\nexit 0\n",
                "sleep": "#!/usr/bin/env bash\nexit 0\n",
                "curl": """#!/usr/bin/env bash
case "$(<"$FAKE_REPO/server.py")" in
  *new*) printf '500' ;;
  *) printf '200' ;;
esac
""",
            }.items():
                path = fake_bin / name
                path.write_text(source)
                path.chmod(0o755)
            env = os.environ.copy()
            env["HOME"] = str(home)
            env["FAKE_REPO"] = str(repo)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            result = subprocess.run(
                [
                    "bash",
                    ROOT / "scripts/deploy-remote.sh",
                    "activate",
                    "--apply",
                    stage,
                    "powerhouse",
                    repo,
                    repo / ".venv/bin/python",
                    "user-systemd",
                    "mobile-terminal.service",
                    "8085",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual((repo / "server.py").read_text(), "old server\n")
            self.assertEqual(
                (repo / "requirements.txt").read_text(), "old requirements\n"
            )
            self.assertIn("rollback restored files and service health", result.stderr)
            self.assertFalse(stage.exists())

    def test_deploy_manifest_includes_complete_provider_runtime(self):
        manifest = self.source("deployment-manifest.sh")
        for relative in (
            "requirements.txt",
            "provider_authority.py",
            "provider_binding_hook.py",
            "install_provider_hooks.py",
        ):
            self.assertIn(relative, manifest)
        self.assertIn("mobile-terminal@ben.service", manifest)
        self.assertIn("mobile-terminal@bperritt.service", manifest)
        self.assertNotIn("StrictHostKeyChecking=accept-new", self.source("deploy.sh"))

    def test_run_refuses_system_python_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "run.sh"
            shutil.copy2(ROOT / "run.sh", script)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            marker = root / "python3-ran"
            python3 = fake_bin / "python3"
            python3.write_text(
                "#!/usr/bin/env bash\nprintf ran >\"$PYTHON3_MARKER\"\n"
            )
            python3.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["PYTHON3_MARKER"] = str(marker)

            result = subprocess.run(
                ["bash", script],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Required repository interpreter is missing", result.stderr)
            self.assertFalse(marker.exists())
            self.assertNotIn("python3", self.source("run.sh"))

    def test_mutating_helpers_require_explicit_apply_flags(self):
        for relative, message in (
            ("install.sh", "without --apply"),
            ("ps-proxy-up.sh", "without explicit --apply"),
        ):
            with self.subTest(relative=relative):
                result = subprocess.run(
                    ["bash", ROOT / relative],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertIn(message, result.stderr)

        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / "home"
            home.mkdir()
            hook_installer = subprocess.run(
                [
                    ROOT / ".venv/bin/python",
                    ROOT / "install_provider_hooks.py",
                    "--home",
                    home,
                    "--claude-version",
                    "2.1.241",
                    "--codex-version",
                    "0.147.0",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(hook_installer.returncode, 2)
            self.assertIn("without explicit --apply", hook_installer.stderr)
            self.assertEqual(list(home.iterdir()), [])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts/provider-mode.sh", scripts / "provider-mode.sh")
            (root / ".venv/bin").mkdir(parents=True)
            os.symlink(ROOT / ".venv/bin/python", root / ".venv/bin/python")
            env_file = root / "mobile-terminal.env"
            env_file.write_text(
                'MOBILE_TERMINAL_PROVIDER_AUTHORITY=off\nMOBILE_TERMINAL_PORT="8085"\n'
            )
            env_file.chmod(0o644)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            log = root / "systemctl.log"
            systemctl = fake_bin / "systemctl"
            systemctl.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ $2 == restart ]]; then printf 'restart %s\\n' \"$3\" >>\"$SYSTEMCTL_LOG\"; fi\n"
                "if [[ $2 == is-active ]]; then printf 'active\\n'; fi\n"
            )
            systemctl.chmod(0o755)
            for name, source in {
                "curl": "#!/usr/bin/env bash\nprintf 200\n",
                "sleep": "#!/usr/bin/env bash\nexit 0\n",
            }.items():
                path = fake_bin / name
                path.write_text(source)
                path.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["SYSTEMCTL_LOG"] = str(log)
            script = scripts / "provider-mode.sh"

            preview = subprocess.run(
                ["bash", script, "shadow"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn("dry_run=yes", preview.stdout)
            self.assertIn("PROVIDER_AUTHORITY=off", env_file.read_text())
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o644)
            self.assertFalse(log.exists())

            applied = subprocess.run(
                ["bash", script, "shadow", "--apply"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(applied.returncode, 0, applied.stderr)
            self.assertIn("health=200", applied.stdout)
            self.assertIn("PROVIDER_AUTHORITY=shadow", env_file.read_text())
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
            restart_count = len(log.read_text().splitlines())

            refused_from_shadow = subprocess.run(
                ["bash", script, "enforce", "--apply", "--confirm-enforce"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(refused_from_shadow.returncode, 1)
            self.assertIn("enable and verify prefer first", refused_from_shadow.stderr)
            self.assertEqual(len(log.read_text().splitlines()), restart_count)
            self.assertIn("PROVIDER_AUTHORITY=shadow", env_file.read_text())

            preferred = subprocess.run(
                ["bash", script, "prefer", "--apply"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(preferred.returncode, 0, preferred.stderr)
            self.assertIn("PROVIDER_AUTHORITY=prefer", env_file.read_text())
            restart_count = len(log.read_text().splitlines())

            unconfirmed = subprocess.run(
                ["bash", script, "enforce", "--apply"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(unconfirmed.returncode, 1)
            self.assertIn("--confirm-enforce", unconfirmed.stderr)
            self.assertEqual(len(log.read_text().splitlines()), restart_count)
            self.assertIn("PROVIDER_AUTHORITY=prefer", env_file.read_text())

            enforced = subprocess.run(
                ["bash", script, "enforce", "--apply", "--confirm-enforce"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(enforced.returncode, 0, enforced.stderr)
            self.assertIn("PROVIDER_AUTHORITY=enforce", env_file.read_text())

            rolled_back = subprocess.run(
                ["bash", script, "off", "--apply"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(rolled_back.returncode, 0, rolled_back.stderr)
            self.assertIn("PROVIDER_AUTHORITY=off", env_file.read_text())

            direct_prefer = subprocess.run(
                ["bash", script, "prefer", "--apply"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(direct_prefer.returncode, 0, direct_prefer.stderr)
            self.assertIn("PROVIDER_AUTHORITY=prefer", env_file.read_text())

    def test_provider_mode_rollback_repairs_owner_only_env_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts/provider-mode.sh", scripts / "provider-mode.sh")
            (root / ".venv/bin").mkdir(parents=True)
            os.symlink(ROOT / ".venv/bin/python", root / ".venv/bin/python")
            env_file = root / "mobile-terminal.env"
            env_file.write_text(
                'MOBILE_TERMINAL_PROVIDER_AUTHORITY=off\nMOBILE_TERMINAL_PORT="8085"\n'
            )
            env_file.chmod(0o666)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            for name, source in {
                "systemctl": "#!/usr/bin/env bash\nif [[ $2 == is-active ]]; then printf 'active\\n'; fi\n",
                "curl": """#!/usr/bin/env bash
if grep -q 'PROVIDER_AUTHORITY=off' "$ROLLBACK_ENV_FILE"; then printf 200; else printf 500; fi
""",
                "sleep": "#!/usr/bin/env bash\nexit 0\n",
            }.items():
                path = fake_bin / name
                path.write_text(source)
                path.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["ROLLBACK_ENV_FILE"] = str(env_file)

            result = subprocess.run(
                ["bash", scripts / "provider-mode.sh", "shadow", "--apply"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("restoring the previous env file", result.stderr)
            self.assertIn("PROVIDER_AUTHORITY=off", env_file.read_text())
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)

    def test_install_requires_explicit_redacted_token_auth_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(ROOT / "install.sh", root / "install.sh")
            (root / ".venv/bin").mkdir(parents=True)
            os.symlink(ROOT / ".venv/bin/python", root / ".venv/bin/python")
            env_file = root / "mobile-terminal.env"
            env_file.write_text("MOBILE_TERMINAL_PORT=8085\n")
            env_file.chmod(0o644)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            for name in ("node", "npm", "python3", "tmux"):
                path = fake_bin / name
                path.write_text("#!/usr/bin/env bash\nexit 0\n")
                path.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"

            blocked = subprocess.run(
                [
                    "bash",
                    root / "install.sh",
                    "--apply",
                    "--service",
                    "none",
                    "--provider-hooks",
                    "off",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(blocked.returncode, 1)
            self.assertIn("--migrate-token-auth", blocked.stderr)
            self.assertEqual(env_file.read_text(), "MOBILE_TERMINAL_PORT=8085\n")

            migrated = subprocess.run(
                [
                    "bash",
                    root / "install.sh",
                    "--apply",
                    "--migrate-token-auth",
                    "--service",
                    "none",
                    "--provider-hooks",
                    "off",
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(migrated.returncode, 0, migrated.stderr)
            self.assertIn("value hidden", migrated.stdout)
            self.assertNotIn("MOBILE_TERMINAL_TOKEN=", migrated.stdout + migrated.stderr)
            self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
            contents = env_file.read_text()
            self.assertRegex(contents, r'(?m)^MOBILE_TERMINAL_TOKEN=".+"$')
            self.assertIn(
                'MOBILE_TERMINAL_AUTH_MIGRATION="passkey-bootstrap-v1"',
                contents,
            )

    def test_installer_quotes_environment_values_for_shell_and_systemd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copy2(ROOT / "install.sh", root / "install.sh")
            (root / ".venv/bin").mkdir(parents=True)
            os.symlink(ROOT / ".venv/bin/python", root / ".venv/bin/python")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            for name in ("node", "npm", "python3", "tmux"):
                path = fake_bin / name
                path.write_text("#!/usr/bin/env bash\nexit 0\n")
                path.chmod(0o755)
            injected = root / "must-not-exist"
            cwd = f'{root}/path with spaces/$HOME;$(touch {injected});`touch {injected}`;"quote";\\tail'
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["EXPECTED_CWD"] = cwd

            result = subprocess.run(
                [
                    "bash",
                    root / "install.sh",
                    "--apply",
                    "--service",
                    "none",
                    "--provider-hooks",
                    "off",
                    "--cwd",
                    cwd,
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            env_file = root / "mobile-terminal.env"
            loaded = subprocess.run(
                [
                    "bash",
                    "-c",
                    'set -a; source "$1"; [[ "$MOBILE_TERMINAL_CWD" == "$EXPECTED_CWD" ]]',
                    "_",
                    env_file,
                ],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(loaded.returncode, 0, loaded.stderr)
            self.assertFalse(injected.exists())
            contents = env_file.read_text()
            self.assertIn('MOBILE_TERMINAL_CWD="', contents)
            self.assertIn(r"\$HOME", contents)
            self.assertIn(r"\`touch", contents)
            self.assertNotIn("$'", contents)

    def test_proxy_backend_starts_from_allowlisted_environment(self):
        proxy_script = self.source("ps-proxy-up.sh")
        backend = proxy_script.split("# 4. Launch the powerhouse backend:", 1)[1].split(
            "BACKEND_PID=$!", 1
        )[0]
        self.assertIn("BACKEND_ENV=(env -i)", backend)
        self.assertIn('"${BACKEND_ENV[@]}"', backend)
        self.assertNotIn("MOBILE_TERMINAL_TOKEN", backend)

    def test_startup_and_access_status_never_print_token_values(self):
        server = self.source("server.py")
        collector = self.source("collect-access.sh")
        installer = self.source("install.sh")
        self.assertNotIn('print(f"access token: {self.token}")', server)
        self.assertIn("access token: configured (value hidden)", server)
        self.assertIn("startup never prints generated secrets", server)
        self.assertIn("secrets.token_urlsafe(32)", installer)
        self.assertIn('chmod 600 "$ENV_FILE"', installer)
        self.assertNotIn("MOBILE_TERMINAL_TOKEN", collector)
        self.assertNotIn("mobile-terminal.env", collector)
        self.assertNotIn('cat "$f"', collector)
        self.assertIn("StrictHostKeyChecking=yes", collector)
        self.assertNotIn("StrictHostKeyChecking=accept-new", collector)
        self.assertIn('"ENTRY" "URL"', collector)

    def test_verify_runtime_is_redacted_bounded_and_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            scripts = root / "scripts"
            scripts.mkdir()
            shutil.copy2(ROOT / "scripts/verify-runtime.sh", scripts / "verify-runtime.sh")
            (root / ".venv/bin").mkdir(parents=True)
            os.symlink(ROOT / ".venv/bin/python", root / ".venv/bin/python")
            secret = "never-print-this-bootstrap-token"
            (root / "mobile-terminal.env").write_text(
                "MOBILE_TERMINAL_PROVIDER_AUTHORITY=prefer\n"
                'MOBILE_TERMINAL_PORT="8085"\n'
                f"MOBILE_TERMINAL_TOKEN={secret}\n"
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            journal_args = root / "journal.args"
            commands = {
                "systemctl": "#!/usr/bin/env bash\nprintf active\n",
                "git": """#!/usr/bin/env bash
if [[ $3 == rev-parse ]]; then printf 'abc1234\n'; fi
exit 0
""",
                "tmux": """#!/usr/bin/env bash
if [[ $1 == show-options ]]; then printf 'latest\n'; else printf 'manual\n'; fi
""",
                "curl": "#!/usr/bin/env bash\nprintf 200\n",
                "journalctl": """#!/usr/bin/env bash
printf '%s\n' "$*" >"$JOURNAL_ARGS"
printf 'INFO never-print-this-bootstrap-token\n'
""",
            }
            for name, source in commands.items():
                path = fake_bin / name
                path.write_text(source)
                path.chmod(0o755)
            env = os.environ.copy()
            home = root / "home"
            (home / ".claude").mkdir(parents=True)
            (home / ".codex/hooks").mkdir(parents=True)
            (home / ".claude/settings.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionStart": [
                                {
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "/repo/provider_binding_hook.py --provider claude",
                                        }
                                    ]
                                }
                            ],
                            "SessionEnd": [
                                {
                                    "_mobile_terminal_source": "moved-tag",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "/repo/provider_binding_hook.py --provider claude",
                                        }
                                    ],
                                }
                            ],
                        }
                    }
                )
            )
            (home / ".codex/hooks/hooks.json").write_text(
                json.dumps(
                    {
                        "hooks": {
                            "SessionStart": [
                                {
                                    "type": "command",
                                    "command": "/repo/provider_binding_hook.py --provider codex",
                                }
                            ]
                        }
                    }
                )
            )
            env["HOME"] = str(home)
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["JOURNAL_ARGS"] = str(journal_args)

            result = subprocess.run(
                ["bash", scripts / "verify-runtime.sh"],
                capture_output=True,
                text=True,
                env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("manual_windows=1", result.stdout)
            self.assertIn("claude_hook_events=2", result.stdout)
            self.assertIn("codex_hook_events=1", result.stdout)
            self.assertIn("recent_error_count=0", result.stdout)
            self.assertNotIn(secret, result.stdout + result.stderr)
            self.assertIn("--lines=200", journal_args.read_text())
            source = self.source("scripts/verify-runtime.sh")
            self.assertNotIn("python3", source)
            self.assertNotIn("/proc/", source)

    def test_production_never_uses_resize_window(self):
        self.assertNotIn("resize-window", self.source("server.py"))

    def test_canonical_test_runner_is_private_and_package_scripts_delegate(self):
        runner = self.source("scripts/test.sh")
        package = self.source("package.json")
        self.assertIn('ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."', runner)
        self.assertIn('PYTHON="$ROOT/.venv/bin/python"', runner)
        self.assertNotIn("python3", runner)
        self.assertIn("env -i", runner)
        self.assertIn('TMUX_TMPDIR=$TEST_ROOT/tmux', runner)
        self.assertIn("trap cleanup EXIT HUP INT TERM", runner)
        self.assertIn('cd "$TEST_ROOT/work"', runner)
        self.assertIn('"PYTHONPATH=$ROOT"', runner)
        self.assertIn('"start": "./run.sh"', package)
        self.assertIn('"test": "scripts/test.sh"', package)
        self.assertIn('"check": "scripts/test.sh --syntax"', package)

    def test_live_tmux_integration_tests_are_private_socket_only(self):
        integration = self.source("tests/test_terminal_authority.py")
        harness = self.source("tests/tmux_harness.py")
        server = self.source("server.py")
        self.assertGreaterEqual(integration.count("TmuxHarness(self"), 3)
        for class_name in (
            "LiveComposerHandlerIntegrationTest",
            "TmuxHarnessLifecycleIntegrationTest",
            "IsolatedTmuxGeometryAuthorityIntegrationTest",
        ):
            start = integration.index(f"class {class_name}")
            end = integration.find("\nclass ", start + 1)
            fixture = integration[start : end if end >= 0 else len(integration)]
            self.assertIn("TmuxHarness(self", fixture)
            self.assertNotIn("subprocess.run(", fixture)
            self.assertNotIn("subprocess.Popen(", fixture)
        self.assertNotIn('["tmux"', integration)
        self.assertNotIn(
            'mock.patch("server.subprocess.Popen", side_effect=',
            integration,
        )
        self.assertIn('self.socket_args = ("-S", self.socket_path)', harness)
        self.assertIn('return ["tmux", *self.socket_args, *args]', harness)
        self.assertIn("test_case.addCleanup(self.close)", harness)
        self.assertIn("test_case.addAsyncCleanup(resource.close)", harness)
        self.assertIn("self.test_case.addAsyncCleanup(self._stop_process, process)", harness)
        self.assertIn("def tmux_client_options()", server)
        self.assertIn('["tmux", *tmux_client_options(), *args]', server)

    def test_provider_fixture_recorder_uses_private_visible_control_probes(self):
        recorder = self.source("tests/tools/record_provider_fixtures.py")
        visible_start = recorder.index("    def capture_visible(")
        saved_start = recorder.index("    def capture(", visible_start)
        geometry_start = recorder.index("def record_geometry(", saved_start)
        visible = recorder[visible_start:saved_start]
        saved = recorder[saved_start:geometry_start]
        ready_start = recorder.index("        def ready()", geometry_start)
        ready_end = recorder.index("        wait_for(", ready_start)
        ready = recorder[ready_start:ready_end]

        self.assertIn('self.env = dict(env, TMUX_TMPDIR=str(root / "tmux-tmp"))', recorder)
        self.assertIn('["tmux", "-S", self.socket, *arguments]', recorder)
        self.assertNotIn("resize-window", recorder)
        self.assertNotIn('"-N"', visible)
        self.assertIn('arguments = ["capture-pane", "-p"]', saved)
        self.assertIn('arguments.append("-e")', saved)
        self.assertIn('arguments.append("-N")', saved)
        self.assertIn("value = tmux.capture_visible()", ready)
        self.assertIn('return True if "❯" in value else None', ready)
        self.assertNotIn("Claude Code v", ready)
        self.assertIn("save_failure_snapshot(root, failure_phase, tmux)", recorder)
        self.assertNotIn('"bullets":', recorder)
        self.assertNotIn('"unicode":', recorder)

    def test_test_state_and_provider_subprocesses_are_sanitized(self):
        proxy_tests = self.source("tests/test_proxy_integration.py")
        hooks = self.source("provider_binding_hook.py")
        hook_tests = self.source("tests/test_provider_hooks.py")
        self.assertIn("self.test_root = Path(self.temporary.name).resolve()", proxy_tests)
        self.assertIn("ignores_repo_sentinel", proxy_tests)
        runner = self.source("scripts/test.sh")
        self.assertIn('cd "$TEST_ROOT/work"', runner)
        self.assertIn('"PYTHONPATH=$ROOT"', runner)
        self.assertNotEqual(Path.cwd().resolve(), ROOT)
        self.assertFalse((Path.cwd() / "state").exists())
        self.assertIn('if not name.startswith("MOBILE_TERMINAL_")', hooks)
        self.assertIn("env=_tmux_environment()", hooks)
        self.assertIn('if not name.startswith("MOBILE_TERMINAL_")', hook_tests)

    def test_documentation_links_and_claims_are_current(self):
        for relative in (
            "docs/RUNBOOK.md",
            "docs/deployment.md",
            "docs/provider-authority.md",
            "docs/tmux-sizing.md",
        ):
            self.assertTrue((ROOT / relative).is_file(), relative)
        self.assertNotIn("BH-DEPLOY-README.md", self.source("ps-proxy-up.sh"))
        self.assertNotIn("does not modify `server.py`", self.source("INTEGRATION-passkeys.md"))
        self.assertNotIn("server prints an access token", self.source("README.md"))

        runbook = self.source("docs/RUNBOOK.md")
        deployment = self.source("docs/deployment.md")
        provider = self.source("docs/provider-authority.md")
        readme = self.source("README.md")
        for command in (
            "scripts/provider-mode.sh shadow --apply",
            "scripts/provider-mode.sh prefer --apply",
            "scripts/provider-mode.sh enforce --apply --confirm-enforce",
            "scripts/provider-mode.sh off --apply",
        ):
            self.assertIn(command, runbook)
            self.assertIn(command, provider)
        self.assertIn("scripts/test.sh --all", runbook)
        self.assertIn("each apply accepts exactly one target", deployment)
        self.assertIn("off -> shadow -> prefer -> enforce", deployment)
        self.assertIn("`ps-powerhouse` is the only managed ps deployment target", deployment)
        for target, service in (
            ("ps-powerhouse", "mobile-terminal.service"),
            ("lat-ben", "mobile-terminal@ben.service"),
            ("lat-bperritt", "mobile-terminal@bperritt.service"),
        ):
            self.assertIn(target, deployment)
            self.assertIn(service, deployment)
        self.assertNotIn("mobile-terminal@behuman.service", deployment)
        self.assertNotIn("both ps users", provider)
        self.assertIn("./install.sh --apply", readme)

        proxy_example = json.loads(self.source("docs/ps-proxy.example.json"))
        behuman = next(profile for profile in proxy_example["profiles"] if profile["id"] == "behuman")
        self.assertEqual(behuman["status"], "down")
        self.assertNotIn("backend", behuman)
        self.assertNotIn("mobile-terminal@behuman.service", self.source("ps-proxy-up.sh"))

        unified_design = self.source("docs/unified-config-design.md")
        self.assertIn("Historical design snapshot, not current deployment topology", unified_design)
        self.assertIn("Behuman is currently an", unified_design)
        self.assertIn("[`deployment.md`](deployment.md)", unified_design)
        self.assertIn(
            "[`profiles-proxy-integration.md`](profiles-proxy-integration.md)",
            unified_design,
        )


if __name__ == "__main__":
    unittest.main()
