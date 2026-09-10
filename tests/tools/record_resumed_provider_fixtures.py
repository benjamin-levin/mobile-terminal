"""Record installed provider renderers without making a model request.

Only synthetic saved messages, a disposable home, and private tmux are used.
The fake API key points to an unused loopback port; no prompt is submitted.
"""

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import uuid


PROSE = ("Reliable terminal copying preserves authored words while removing visual "
         "wraps added by the provider renderer without inventing line breaks.")
CASES = {
    "prose": (f"FIXTURE-BEGIN\n{PROSE}\nNEXT\n\nFIXTURE-END",
              f"FIXTURE-BEGIN\n{PROSE}\nNEXT\n\nFIXTURE-END"),
    "rich": ("FIXTURE-BEGIN\n\n## Strong heading\n\nPlain **bold** and *italic* words.\n\n"
             "- first bullet wraps across a narrow terminal while retaining its own authored identity\n"
             "- second bullet\n\nStatus ✅ 東京\n\nFIXTURE-END",
             "FIXTURE-BEGIN\n\nStrong heading\n\nPlain bold and italic words.\n\n"
             "- first bullet wraps across a narrow terminal while retaining its own authored identity\n"
             "- second bullet\n\nStatus ✅ 東京\n\nFIXTURE-END"),
    "code": ("FIXTURE-BEGIN\n\nInline `code_value()` remains here.\n\n```python\n"
             "print(\"東京 ✅\")\n```\n\nFIXTURE-END",
             "FIXTURE-BEGIN\n\nInline code_value() remains here.\n\n"
             "print(\"東京 ✅\")\n\nFIXTURE-END"),
}


def record(output, scratch, width, case, version, executable, provider):
    root = scratch / f"{case}-{width}"
    home, work = root / "home", root / "work"
    home.mkdir(parents=True)
    work.mkdir()
    environment = {
        "HOME": str(home), "PATH": os.environ["PATH"], "TERM": "xterm-256color",
        "LANG": "C.UTF-8", "SHELL": "/bin/sh", "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "ANTHROPIC_API_KEY": "synthetic-offline-fixture", "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
        "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
    session = str(uuid.uuid4())
    project = home / ".claude/projects" / str(work).replace("/", "-")
    project.mkdir(parents=True)
    source, expected = CASES[case]
    common = {"sessionId": session, "cwd": str(work), "version": version, "isSidechain": False}
    user_id = str(uuid.uuid4())
    records = [
        dict(common, type="user", uuid=user_id, parentUuid=None,
             timestamp="2026-09-04T12:00:00.000Z",
             message={"role": "user", "content": "Synthetic offline renderer fixture"}),
        dict(common, type="assistant", uuid=str(uuid.uuid4()), parentUuid=user_id,
             timestamp="2026-09-04T12:00:01.000Z",
             message={"id": "fixture-message", "model": "claude-haiku-4-5-20251001",
                      "role": "assistant", "type": "message", "content": [{"type": "text", "text": source}],
                      "stop_reason": "end_turn", "stop_sequence": None,
                      "usage": {"input_tokens": 1, "output_tokens": 1}}),
    ]
    transcript = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    (project / f"{session}.jsonl").write_text(transcript)
    (home / ".claude/.claude.json").write_text(json.dumps({
        "hasCompletedOnboarding": True, "theme": "dark",
        "projects": {str(work): {"hasTrustDialogAccepted": True}},
    }))
    if provider == "codex":
        environment.pop("CLAUDE_CONFIG_DIR")
        sessions = home / ".codex/sessions/2026/09/04"
        sessions.mkdir(parents=True)
        def event(kind, payload):
            return {"timestamp": "2026-09-04T12:00:00.000Z", "type": kind, "payload": payload}
        codex_records = [
            event("session_meta", {"id": session, "timestamp": "2026-09-04T12:00:00.000Z",
                  "cwd": str(work), "originator": "codex_cli_rs", "cli_version": version,
                  "source": "cli", "model_provider": "fixture"}),
            event("turn_context", {"turn_id": "fixture-turn", "cwd": str(work), "model": "fixture-model"}),
            event("response_item", {"type": "message", "role": "assistant", "id": "fixture-message",
                  "internal_chat_message_metadata_passthrough": {"turn_id": "fixture-turn"},
                  "content": [{"type": "output_text", "text": source}]}),
            event("event_msg", {"type": "user_message", "message": "Synthetic offline renderer fixture",
                  "images": [], "local_images": [], "text_elements": []}),
            event("event_msg", {"type": "agent_message", "message": source, "phase": "final_answer"}),
            event("event_msg", {"type": "task_complete", "turn_id": "fixture-turn", "last_agent_message": source}),
        ]
        (sessions / f"rollout-2026-09-04T12-00-00-{session}.jsonl").write_text(
            "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in codex_records))
        (home / ".codex/config.toml").write_text(
            'model = "fixture-model"\nmodel_provider = "fixture"\n'
            '[model_providers.fixture]\nname = "Offline Fixture"\n'
            'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\nrequires_openai_auth = false\n'
            f'[projects.{json.dumps(str(work))}]\ntrust_level = "trusted"\n')
    def tmux(*args):
        return subprocess.run(["tmux", "-S", str(root / "socket"), *args], env=environment,
                              text=True, capture_output=True, check=True, timeout=5).stdout
    try:
        command = shlex.join([executable, "--resume", session, "--permission-mode", "dontAsk",
                              "--setting-sources", "user"])
        if provider == "codex":
            command = shlex.join([executable, "--no-alt-screen", "resume", session])
        tmux("new-session", "-d", "-x", str(width), "-y", "40", "-c", str(work), "-s", "fixture", command)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            plain = tmux("capture-pane", "-p", "-t", "fixture", "-S", "-200")
            if "Detected a custom API key" in plain:
                tmux("send-keys", "-t", "fixture", "Up", "Enter")
            if "FIXTURE-END" in plain:
                break
            time.sleep(.1)
        else:
            raise RuntimeError(f"Synthetic resume did not render: {plain}")
        styled = tmux("capture-pane", "-p", "-e", "-t", "fixture", "-S", "-200")
        # Store only the assistant block, excluding CLI chrome and temp paths.
        rows, styles = plain.splitlines(), styled.splitlines()
        first = next(i for i, row in enumerate(rows) if "FIXTURE-BEGIN" in row)
        last = next(i for i, row in enumerate(rows) if "FIXTURE-END" in row)
        name = f"{case}-{width}"
        (output / f"{name}.json").write_text(json.dumps({
            "provider": provider, "version": version, "cols": width,
            "method": "synthetic-transcript-resume", "modelRequests": 0,
            "source": source, "expected": expected,
            "plain": rows[first:last + 1], "styled": styles[first:last + 1],
        }, ensure_ascii=False, indent=2) + "\n")
    finally:
        tmux("kill-server")
        if provider == "codex":
            # A current Codex TUI can leave a daemon behind. Never target a
            # process unless its HOME is exactly this disposable fixture home.
            for proc in Path("/proc").iterdir():
                if not proc.name.isdigit():
                    continue
                try:
                    if ("HOME=" + str(home)).encode() in (proc / "environ").read_bytes().split(b"\0"):
                        os.kill(int(proc.name), signal.SIGTERM)
                except OSError:
                    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provider", choices=("claude", "codex"), default="claude")
    args = parser.parse_args()
    executable = shutil.which(args.provider)
    version = re.search(r"\d+\.\d+\.\d+", subprocess.check_output([executable, "--version"], text=True)).group()
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mt-resume-", dir="/var/tmp") as temporary:
        for width in (60, 100):
            for case in CASES:
                record(args.output, Path(temporary), width, case, version, executable, args.provider)
