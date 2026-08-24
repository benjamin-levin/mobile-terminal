import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from install_provider_hooks import BACKUP_SUFFIX, SOURCE_TAG, install_provider_hooks
from provider_binding_hook import _pane_coordinates, _provider_process, update_binding


SESSION_ID = "12345678-1234-4123-8123-123456789abc"


class ProviderHookInstallerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name) / "home"
        self.root = Path(self.temporary.name) / "mobile terminal"
        self.home.mkdir()
        self.root.mkdir()
        (self.root / "provider_binding_hook.py").write_text("#!/usr/bin/env python3\n")

    def tearDown(self):
        self.temporary.cleanup()

    def test_preserves_foreign_hooks_and_codex_toml_and_is_idempotent(self):
        claude_path = self.home / ".claude" / "settings.json"
        claude_path.parent.mkdir()
        gstack = {
            "_gstack_source": "gstack-timeline-stop",
            "hooks": [{"type": "command", "command": "/foreign/stop"}],
        }
        stale_tagged = {
            "_mobile_terminal_source": SOURCE_TAG,
            "hooks": [{"type": "command", "command": "/old/subagent-hook"}],
        }
        claude_original = json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "Stop": [gstack],
                    "SubagentStart": [stale_tagged],
                    "SubagentStop": [stale_tagged],
                },
            }
        )
        claude_path.write_text(claude_original)
        codex_config = self.home / ".codex" / "config.toml"
        codex_config.parent.mkdir()
        original_toml = 'model = "gpt"\n[projects."/work"]\ntrust_level = "trusted"\n'
        codex_config.write_text(original_toml)
        codex_hooks = self.home / ".codex" / "hooks" / "hooks.json"
        codex_hooks.parent.mkdir()
        foreign = {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "foreign"}]}],
                "SubagentStart": [stale_tagged],
                "SubagentStop": [stale_tagged],
            }
        }
        codex_original = json.dumps(foreign)
        codex_hooks.write_text(codex_original)

        install_provider_hooks(self.home, self.root, "2.1.241", "0.147.0")
        claude_backup = claude_path.with_name(f"{claude_path.name}{BACKUP_SUFFIX}")
        codex_backup = codex_hooks.with_name(f"{codex_hooks.name}{BACKUP_SUFFIX}")
        self.assertEqual(claude_backup.read_text(), claude_original)
        self.assertEqual(codex_backup.read_text(), codex_original)
        self.assertEqual(oct(claude_backup.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(codex_backup.stat().st_mode & 0o777), "0o600")
        claude_backup.write_text("stable backup")
        os.chmod(claude_backup, 0o600)
        changed = json.loads(claude_path.read_text())
        changed["statusLine"] = {"type": "command", "command": "foreign-status"}
        changed["hooks"]["SessionStart"] = []
        claude_path.write_text(json.dumps(changed))
        install_provider_hooks(self.home, self.root, "2.1.241", "0.147.0")
        self.assertEqual(claude_backup.read_text(), "stable backup")

        claude = json.loads(claude_path.read_text())
        self.assertEqual(claude["statusLine"], changed["statusLine"])
        self.assertEqual(claude["theme"], "dark")
        self.assertEqual(claude["hooks"]["Stop"], [gstack])
        for event in ("SessionStart", "SessionEnd", "PreCompact", "PostCompact", "CwdChanged"):
            tagged = [
                entry
                for entry in claude["hooks"][event]
                if entry.get("_mobile_terminal_source") == SOURCE_TAG
            ]
            self.assertEqual(len(tagged), 1)
            self.assertIn("'", tagged[0]["hooks"][0]["command"])
        self.assertNotIn("SubagentStart", claude["hooks"])
        self.assertNotIn("SubagentStop", claude["hooks"])
        installed_codex = json.loads(codex_hooks.read_text())
        self.assertNotIn("SubagentStart", installed_codex["hooks"])
        self.assertNotIn("SubagentStop", installed_codex["hooks"])
        self.assertEqual(installed_codex["hooks"]["Stop"], foreign["hooks"]["Stop"])
        self.assertEqual(codex_config.read_text(), original_toml)
        self.assertEqual(oct(claude_path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct(codex_hooks.stat().st_mode & 0o777), "0o600")

    def test_new_hook_files_do_not_create_backups(self):
        install_provider_hooks(self.home, self.root, "2.1.241", "0.147.0")
        claude = self.home / ".claude" / "settings.json"
        codex = self.home / ".codex" / "hooks" / "hooks.json"
        self.assertTrue(claude.exists())
        self.assertTrue(codex.exists())
        self.assertFalse(claude.with_name(f"{claude.name}{BACKUP_SUFFIX}").exists())
        self.assertFalse(codex.with_name(f"{codex.name}{BACKUP_SUFFIX}").exists())

    def test_missing_provider_clis_are_fail_open(self):
        install_provider_hooks(self.home, self.root, None, None)
        self.assertFalse((self.home / ".claude" / "settings.json").exists())
        self.assertFalse((self.home / ".codex" / "hooks" / "hooks.json").exists())

    def test_rejects_missing_or_unsafe_installation_root(self):
        missing = Path(self.temporary.name) / "missing"
        with self.assertRaises(FileNotFoundError):
            install_provider_hooks(self.home, missing, "2.1.241", "0.147.0")


class InstallScriptProviderHooksTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for command in ("python3", "tmux", "node", "npm"):
            path = self.bin / command
            path.write_text("#!/bin/sh\nexit 0\n")
            path.chmod(0o755)
        self.run_number = 0

    def tearDown(self):
        self.temporary.cleanup()

    def run_install(self, *arguments, hook_exit=0):
        self.run_number += 1
        root = self.base / f"install-{self.run_number}"
        root.mkdir()
        source = Path(__file__).resolve().parents[1] / "install.sh"
        shutil.copy2(source, root / "install.sh")
        (root / "provider_binding_hook.py").write_text("#!/usr/bin/env python3\n")
        python = root / ".venv" / "bin" / "python"
        python.parent.mkdir(parents=True)
        python.write_text(
            "#!/bin/sh\n"
            'case "$*" in\n'
            '  *install_provider_hooks.py*)\n'
            '    printf "%s\\n" "$*" >>"$HOME/provider-hook-calls"\n'
            '    exit "${PROVIDER_HOOK_EXIT:-0}"\n'
            '    ;;\n'
            '  *token_urlsafe*) printf "test-bootstrap-secret\\n" ;;\n'
            'esac\n'
        )
        python.chmod(0o755)
        environment = {
            **os.environ,
            "HOME": str(self.home),
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "PROVIDER_HOOK_EXIT": str(hook_exit),
        }
        return subprocess.run(
            ["bash", str(root / "install.sh"), "--apply", *arguments, "--service", "none"],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def test_default_auto_continues_and_logs_hook_failure(self):
        result = self.run_install(hook_exit=9)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hook installation failed; continuing", result.stdout)
        self.assertTrue((self.home / "provider-hook-calls").exists())

    def test_off_skips_and_required_propagates_hook_failure(self):
        calls = self.home / "provider-hook-calls"
        result = self.run_install("--provider-hooks", "off", hook_exit=9)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Skipping provider lifecycle hooks", result.stdout)
        self.assertFalse(calls.exists())

        result = self.run_install("--provider-hooks", "required", hook_exit=9)
        self.assertEqual(result.returncode, 9)
        self.assertTrue(calls.exists())

    def test_invalid_mode_is_rejected_before_installation(self):
        result = self.run_install("--provider-hooks", "sometimes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Invalid --provider-hooks mode: sometimes", result.stderr)
        self.assertFalse((self.home / "provider-hook-calls").exists())


class ProviderBindingHookTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.transcript = self.home / ".claude" / "projects" / "project" / f"{SESSION_ID}.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text("{}\n")
        self.event = {
            "session_id": SESSION_ID,
            "transcript_path": str(self.transcript),
            "hook_event_name": "SessionStart",
            "event_time_ns": 100,
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_generation_replay_and_end_are_pane_local(self):
        with patch.dict(os.environ, {"TMUX_PANE": "%7"}, clear=False), patch(
            "provider_binding_hook._claude_registry",
            return_value=(321, 444, "2.1.241"),
        ):
            update_binding("claude", self.event, "ignored", self.home)
            path = self.home / ".mobile-terminal" / "provider-bindings" / "7.json"
            first = json.loads(path.read_text())
            self.assertEqual(first["generation"], 1)
            self.assertTrue(first["active"])
            self.assertEqual(first["procStart"], "444")

            update_binding("claude", dict(self.event, event_time_ns=99), "ignored", self.home)
            self.assertEqual(json.loads(path.read_text()), first)

            update_binding(
                "claude",
                dict(self.event, event_time_ns=101, hook_event_name="SubagentStop"),
                "ignored",
                self.home,
            )
            subagent = json.loads(path.read_text())
            self.assertEqual(subagent["generation"], 2)
            self.assertTrue(subagent["active"])

            update_binding(
                "claude",
                dict(self.event, event_time_ns=102, hook_event_name="SessionEnd"),
                "ignored",
                self.home,
            )
            ended = json.loads(path.read_text())
            self.assertEqual(ended["generation"], 3)
            self.assertFalse(ended["active"])

            update_binding(
                "claude",
                dict(self.event, event_time_ns=103, hook_event_name="SessionStart"),
                "ignored",
                self.home,
            )
            self.assertEqual(json.loads(path.read_text()), ended)

    def test_codex_records_normal_buffer_boundaries_and_saturation(self):
        transcript = self.home / ".codex" / "sessions" / "2026" / f"{SESSION_ID}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")
        event = {
            "session_id": SESSION_ID,
            "rollout_path": str(transcript),
            "hook_event_name": "SessionStart",
            "event_time_ns": 200,
        }
        start = {
            "history": 12,
            "historyLimit": 100,
            "cursorY": 3,
            "rows": 24,
            "alternate": False,
        }
        end = dict(start, history=18, cursorY=5)
        coordinates = iter((start, end))
        with patch.dict(os.environ, {"TMUX_PANE": "%7"}, clear=False), patch(
            "provider_binding_hook._provider_process",
            return_value=(654, 987),
        ):
            update_binding(
                "codex",
                event,
                "0.147.0",
                self.home,
                pane_coordinates=lambda pane: next(coordinates),
            )
            path = self.home / ".mobile-terminal" / "provider-bindings" / "7.json"
            started = json.loads(path.read_text())
            ownership = started["ownershipRanges"][-1]
            self.assertEqual(ownership["startRow"], 15)
            self.assertIsNone(ownership["endRow"])
            self.assertFalse(ownership["saturated"])

            update_binding(
                "codex",
                dict(event, event_time_ns=201, hook_event_name="SessionEnd"),
                "0.147.0",
                self.home,
                pane_coordinates=lambda pane: next(coordinates),
            )
            ended = json.loads(path.read_text())
            self.assertFalse(ended["active"])
            self.assertEqual(ended["ownershipRanges"][-1]["endRow"], 23)

    def test_codex_coordinate_failure_is_cached_for_fail_closed_selection(self):
        transcript = self.home / ".codex" / "sessions" / f"{SESSION_ID}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")
        event = {
            "session_id": SESSION_ID,
            "rollout_path": str(transcript),
            "hook_event_name": "SessionStart",
            "event_time_ns": 300,
        }
        with patch.dict(os.environ, {"TMUX_PANE": "%7"}, clear=False), patch(
            "provider_binding_hook._provider_process",
            return_value=(654, 987),
        ):
            update_binding(
                "codex",
                event,
                "0.147.0",
                self.home,
                pane_coordinates=lambda pane: (_ for _ in ()).throw(RuntimeError()),
            )
        path = self.home / ".mobile-terminal" / "provider-bindings" / "7.json"
        cached = json.loads(path.read_text())
        self.assertTrue(cached["active"])
        self.assertTrue(cached["ownershipUnavailable"])
        self.assertEqual(cached["ownershipRanges"], [])

    def test_provider_process_skips_shell_and_python_wrappers(self):
        proc_root = Path(self.temporary.name) / "proc"

        def process(pid, parent, start, executable, command):
            path = proc_root / str(pid)
            path.mkdir(parents=True)
            fields = ["S", str(parent), *("0" for _ in range(17)), str(start)]
            (path / "stat").write_text(f"{pid} ({command}) {' '.join(fields)}\n")
            (path / "environ").write_bytes(b"TMUX_PANE=%7\0")
            (path / "comm").write_text(f"{command}\n")
            (path / "cmdline").write_bytes(f"{executable}\0/path/containing/codex\0".encode())
            (path / "exe").symlink_to(executable)

        process(100, 99, 1000, "/usr/bin/python3", "python3")
        process(99, 98, 900, "/bin/sh", "sh")
        process(98, 1, 800, "/opt/codex/bin/codex", "codex")
        self.assertEqual(
            _provider_process("codex", "%7", proc_root, start_pid=100),
            (98, 800),
        )
        (proc_root / "98" / "exe").unlink()
        (proc_root / "98" / "exe").symlink_to("/bin/sh")
        (proc_root / "98" / "comm").write_text("sh\n")
        with self.assertRaises(ValueError):
            _provider_process("codex", "%7", proc_root, start_pid=100)

    def test_tmux_probe_subprocess_removes_mobile_terminal_secrets(self):
        result = subprocess.CompletedProcess(
            [],
            0,
            "%7\t12\t100\t3\t24\t0\n",
            "",
        )
        with patch.dict(
            os.environ,
            {
                "TMUX": "/private/socket,1,0",
                "TMUX_PANE": "%7",
                "MOBILE_TERMINAL_TOKEN": "external-secret",
                "MOBILE_TERMINAL_INTERNAL_TOKEN_POWERHOUSE": "internal-secret",
                "MOBILE_TERMINAL_CONFIG": "/real/config.json",
            },
            clear=False,
        ), patch("provider_binding_hook.subprocess.run", return_value=result) as run:
            self.assertEqual(_pane_coordinates("%7")["history"], 12)

        child_environment = run.call_args.kwargs["env"]
        self.assertEqual(child_environment["TMUX_PANE"], "%7")
        self.assertNotIn("MOBILE_TERMINAL_TOKEN", child_environment)
        self.assertNotIn(
            "MOBILE_TERMINAL_INTERNAL_TOKEN_POWERHOUSE",
            child_environment,
        )
        self.assertNotIn("MOBILE_TERMINAL_CONFIG", child_environment)

    def test_malformed_hook_input_is_provider_safe(self):
        script = Path(__file__).resolve().parents[1] / "provider_binding_hook.py"
        result = subprocess.run(
            [sys.executable, str(script), "--provider", "claude", "--version", "2.1.241"],
            input=b"not-json",
            env={
                **{
                    name: value
                    for name, value in os.environ.items()
                    if not name.startswith("MOBILE_TERMINAL_")
                },
                "HOME": str(self.home),
                "TMUX_PANE": "%7",
            },
            capture_output=True,
            timeout=3,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")


if __name__ == "__main__":
    unittest.main()
