#!/usr/bin/env python3
import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from provider_binding_hook import update_binding


SCENARIOS = {
    "mixed": (
        "Reply with exactly this structure and no commentary. First write "
        "FIXTURE-MIXED-BEGIN on its own line. Then write this prose paragraph: "
        "Reliable terminal copying preserves authored words while removing visual "
        "wraps, so a paragraph selected on a narrow screen still returns the "
        "original sentence without invented newlines. Then a blank line. Then two "
        "markdown bullets: - first bullet wraps naturally across the terminal "
        "width without changing its source; - second bullet remains distinct from "
        "the first item. Then a blank line and exactly: Status ✅ 東京 漢字 "
        "complete. Then a blank line and exactly: Inline `code_value()` remains "
        "here. End with FIXTURE-MIXED-END on its own line."
    ),
    "prose": (
        "Reply with exactly three lines and no commentary. Line one is "
        "FIXTURE-PROSE-BEGIN. Line two is: Reliable terminal copying preserves "
        "authored words while removing visual wraps, so a paragraph selected on "
        "a narrow screen still returns the original sentence without invented "
        "newlines. Line three is FIXTURE-PROSE-END."
    ),
    "code": (
        "Reply with exactly this markdown and no commentary: a line containing "
        "FIXTURE-CODE-BEGIN, then a line containing Inline `code_value()` remains "
        "here., then a fenced python code block containing only print(\"東京 ✅\"), "
        "then a line containing FIXTURE-CODE-END."
    ),
    "richtext": (
        "Reply with exactly this markdown and no commentary. First line "
        "FIXTURE-RICHTEXT-BEGIN. Then a markdown heading: ## Deployment summary. "
        "Then a numbered list with three items: 1. preflight every manifest file "
        "before any remote copy so partial trees never activate; 2. stage into a "
        "fresh directory and smoke-test the import closure there; 3. roll back "
        "files and service health together on any failure. Then a paragraph with "
        "emphasis: The rollout order stays **ph first**, then *both ps users*, "
        "then lat, and no gate may be skipped even when every earlier stage "
        "passed cleanly. Last line FIXTURE-RICHTEXT-END."
    ),
    "history": (
        "Reply with exactly this structure and no commentary. First line "
        "FIXTURE-HISTORY-BEGIN. Then forty short lines, each of the form "
        "item NN: retained history row followed by that number spelled in "
        "words, for NN from 01 to 40. Then one paragraph: Selections that "
        "start above the visible viewport must still resolve to their exact "
        "authored source once the pane scrolls, because retained rows keep "
        "their original width forever. Last line FIXTURE-HISTORY-END."
    ),
}


@dataclass(frozen=True)
class Geometry:
    name: str
    cols: int
    rows: int
    scenarios: tuple[str, ...]


GEOMETRIES = (
    Geometry("wide", 100, 30, ("mixed", "code")),
    Geometry("narrow", 60, 30, ("prose",)),
)


class RecorderError(RuntimeError):
    pass


def run(
    command: list[str],
    *,
    env: dict[str, str],
    timeout: float = 15,
    cwd: Path | None = None,
) -> str:
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=timeout,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise RecorderError(f"command failed ({command[0]} exit {result.returncode})")
    return result.stdout


def wait_for(description: str, probe: Callable[[], object | None], timeout: float) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value is not None:
            return value
        time.sleep(0.02)
    raise RecorderError(f"timed out waiting for {description}")


class PrivateTmux:
    def __init__(self, root: Path, geometry: Geometry, env: dict[str, str]):
        self.root = root
        self.geometry = geometry
        self.env = dict(env, TMUX_TMPDIR=str(root / "tmux-tmp"))
        self.tmux_tmp = root / "tmux-tmp"
        self.socket = "fixture.sock"
        self.session = f"provider-fixture-{geometry.name}"
        self.pane = ""
        self.tmux_tmp.mkdir(mode=0o700, parents=True)

    def command(self, *arguments: str, timeout: float = 15) -> str:
        return run(
            ["tmux", "-S", self.socket, *arguments],
            env=self.env,
            timeout=timeout,
            cwd=self.tmux_tmp,
        )

    def start(self, cwd: Path) -> None:
        self.command(
            "new-session",
            "-d",
            "-x",
            str(self.geometry.cols),
            "-y",
            str(self.geometry.rows),
            "-s",
            self.session,
            "-c",
            str(cwd),
            shlex.join(["bash", "--noprofile", "--norc"]),
        )
        self.pane = self.command(
            "display-message",
            "-p",
            "-t",
            self.session,
            "#{pane_id}",
        ).strip()
        if not self.pane.startswith("%"):
            raise RecorderError("private pane identity unavailable")

    def send(self, text: str) -> None:
        self.command("send-keys", "-l", "-t", self.pane, text)
        self.command("send-keys", "-t", self.pane, "Enter")

    def submit(self, text: str) -> None:
        self.command("send-keys", "-l", "-t", self.pane, text)
        time.sleep(0.2)
        self.command("send-keys", "-t", self.pane, "C-m")

    def capture_visible(self, *, styled: bool = False) -> str:
        arguments = ["capture-pane", "-p"]
        if styled:
            arguments.append("-e")
        arguments.extend(("-t", self.pane))
        return self.command(*arguments)

    def capture(self, *, styled: bool) -> str:
        arguments = ["capture-pane", "-p"]
        if styled:
            arguments.append("-e")
        arguments.append("-N")
        arguments.extend(("-t", self.pane))
        return self.command(*arguments)

    def geometry_metadata(self) -> dict[str, int]:
        value = self.command(
            "display-message",
            "-p",
            "-t",
            self.pane,
            "#{pane_width} #{pane_height} #{pane_pid}",
        ).strip()
        cols, rows, pid = (int(item) for item in value.split())
        return {"cols": cols, "rows": rows, "shellPid": pid}

    def close(self) -> None:
        try:
            self.command("kill-server", timeout=5)
        except (RecorderError, subprocess.TimeoutExpired):
            pass


def binding_cache(state_root: Path, pane: str) -> dict[str, object] | None:
    path = state_root / ".mobile-terminal" / "provider-bindings" / f"{pane[1:]}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def pane_registry_metadata(
    tmux: PrivateTmux,
    session_id: str,
    version: str,
) -> dict[str, object] | None:
    shell_pid = tmux.geometry_metadata()["shellPid"]
    pending = [shell_pid]
    descendants = []
    while pending:
        parent = pending.pop()
        try:
            children = [
                int(value)
                for value in Path(
                    f"/proc/{parent}/task/{parent}/children"
                ).read_text(encoding="ascii").split()
            ]
        except (FileNotFoundError, OSError, UnicodeError, ValueError):
            continue
        pending.extend(children)
        descendants.extend(children)
    claude = []
    for pid in descendants:
        try:
            command = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
        except (FileNotFoundError, OSError, UnicodeError):
            continue
        if command == "claude":
            claude.append(pid)
    if len(claude) != 1:
        return None
    pid = claude[0]
    try:
        stat_value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    close = stat_value.rfind(")")
    fields = stat_value[close + 2 :].split() if close >= 0 else []
    if len(fields) <= 19:
        return None
    return {
        "pid": pid,
        "sessionId": session_id,
        "procStart": fields[19],
        "version": version,
        "tmux": f"fixture:{tmux.pane}",
    }


def transcript_path(session_id: str) -> Path | None:
    matches = tuple((Path.home() / ".claude" / "projects").rglob(f"{session_id}.jsonl"))
    return matches[0] if len(matches) == 1 and matches[0].is_file() else None


def transcript_has_assistant_marker(path: Path, marker: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError, UnicodeError):
        return False
    for line in reversed(lines):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict) or value.get("type") != "assistant":
            continue
        message = value.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        if any(
            isinstance(block, dict)
            and block.get("type") == "text"
            and marker in str(block.get("text", ""))
            for block in content
        ):
            return True
    return False


def record_binding(
    state_root: Path,
    pane: str,
    session_id: str,
    transcript: Path,
    version: str,
    registry: dict[str, object],
) -> dict[str, object]:
    event = {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "hook_event_name": "SessionStart",
    }
    previous_pane = os.environ.get("TMUX_PANE")
    os.environ["TMUX_PANE"] = pane
    try:
        update_binding(
            "claude",
            event,
            version,
            Path.home().resolve(),
            state_root=state_root,
            claude_registry=lambda home, pane_id, event_session: (
                int(registry["pid"]),
                str(registry["procStart"]),
                str(registry["version"]),
            ),
        )
    finally:
        if previous_pane is None:
            os.environ.pop("TMUX_PANE", None)
        else:
            os.environ["TMUX_PANE"] = previous_pane
    value = binding_cache(state_root, pane)
    if value is None:
        raise RecorderError("provider binding hook produced no isolated metadata")
    return value


def copy_snapshot(
    output: Path,
    name: str,
    tmux: PrivateTmux,
    session_id: str,
    model: str,
    version: str,
    invocation: int,
    transcript: Path,
    registry: dict[str, object],
    cache: dict[str, object],
    state: str,
) -> dict[str, object]:
    plain_name = f"{name}.plain.txt"
    styled_name = f"{name}.styled.txt"
    transcript_name = f"{name}.transcript.jsonl"
    metadata_name = f"{name}.json"
    (output / plain_name).write_text(tmux.capture(styled=False), encoding="utf-8")
    (output / styled_name).write_text(tmux.capture(styled=True), encoding="utf-8")
    shutil.copyfile(transcript, output / transcript_name)
    actual_geometry = tmux.geometry_metadata()
    metadata = {
        "schema": 1,
        "scenario": name,
        "state": state,
        "geometry": {
            "requested": {"cols": tmux.geometry.cols, "rows": tmux.geometry.rows},
            "actual": {"cols": actual_geometry["cols"], "rows": actual_geometry["rows"]},
        },
        "pane": {"id": tmux.pane, "shellPid": actual_geometry["shellPid"]},
        "claude": {
            "version": version,
            "model": model,
            "sessionId": session_id,
            "invocation": invocation,
        },
        "transcript": {"path": str(transcript), "copy": transcript_name},
        "registry": registry,
        "binding": cache,
        "files": {"plain": plain_name, "styled": styled_name},
    }
    (output / metadata_name).write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata


def save_failure_snapshot(root: Path, phase: str, tmux: PrivateTmux) -> None:
    for suffix, styled in (("plain.txt", False), ("styled.txt", True)):
        try:
            value = tmux.capture(styled=styled)
            (root / f"failure-{phase}.{suffix}").write_text(value, encoding="utf-8")
        except (OSError, RecorderError, subprocess.TimeoutExpired):
            pass


def record_geometry(
    output: Path,
    scratch: Path,
    geometry: Geometry,
    model: str,
    version: str,
    invocation_start: int,
) -> tuple[list[dict[str, object]], int]:
    root = scratch / geometry.name
    root.mkdir(mode=0o700, parents=True)
    work = root / "work"
    state_root = root / "state"
    work.mkdir(mode=0o700)
    state_root.mkdir(mode=0o700)
    session_id = str(uuid.uuid4())
    env = os.environ.copy()
    env.pop("TMUX", None)
    tmux = PrivateTmux(root, geometry, env)
    metadata: list[dict[str, object]] = []
    invocation = invocation_start
    failure_phase = "startup"
    try:
        tmux.start(work)
        command = shlex.join(
            [
                "env",
                "-u",
                "CLAUDECODE",
                "-u",
                "CLAUDE_CODE_ENTRYPOINT",
                "-u",
                "CLAUDE_CODE_CHILD_SESSION",
                "-u",
                "CLAUDE_CODE_MESSAGING_SOCKET",
                "-u",
                "CLAUDE_CODE_MESSAGING_TOKEN",
                "-u",
                "CLAUDE_CODE_SESSION_ID",
                "-u",
                "CLAUDE_PID",
                f"MOBILE_TERMINAL_PROVIDER_BINDING_STATE_ROOT={state_root}",
                "claude",
                "--model",
                model,
                "--effort",
                "low",
                "--permission-mode",
                "dontAsk",
                "--tools",
                "",
                "--no-chrome",
                "--disable-slash-commands",
                "--setting-sources",
                "user",
                "--session-id",
                session_id,
            ]
        )
        tmux.send(command)

        def ready() -> object | None:
            value = tmux.capture_visible()
            if "Yes, I trust this folder" in value:
                tmux.command("send-keys", "-t", tmux.pane, "Enter")
                time.sleep(0.5)
                return None
            if "Bypass Permissions mode" in value:
                raise RecorderError("Claude requested bypass-permissions confirmation")
            return True if "❯" in value else None

        wait_for("Claude input prompt", ready, 30)
        registry: dict[str, object] | None = None
        transcript: Path | None = None
        cache: dict[str, object] | None = None

        streaming_saved = False
        for scenario in geometry.scenarios:
            failure_phase = f"{geometry.name}-{scenario}"
            invocation += 1
            tmux.submit(SCENARIOS[scenario])
            end = f"FIXTURE-{scenario.upper()}-END"
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                plain = tmux.capture_visible()
                if any(
                    marker in plain
                    for marker in (
                        "API Error",
                        "Invalid model",
                        "Not logged in",
                        "authentication failed",
                        "rate limit",
                        "usage limit",
                    )
                ):
                    raise RecorderError("Claude reported an interactive request failure")
                if registry is None:
                    registry = pane_registry_metadata(tmux, session_id, version)
                if transcript is None:
                    transcript = transcript_path(session_id)
                if cache is None:
                    cache = binding_cache(state_root, tmux.pane)
                if cache is None and registry is not None and transcript is not None:
                    cache = record_binding(
                        state_root,
                        tmux.pane,
                        session_id,
                        transcript,
                        version,
                        registry,
                    )
                if (
                    geometry.name == "wide"
                    and not streaming_saved
                    and registry is not None
                    and transcript is not None
                    and not transcript_has_assistant_marker(transcript, end)
                    and cache is not None
                    and any(
                        marker in plain
                        for marker in ("esc to interrupt", "✳", "✶", "✻", "⏺")
                    )
                ):
                    streaming_name = "wide-streaming"
                    metadata.append(
                        copy_snapshot(
                            output,
                            streaming_name,
                            tmux,
                            session_id,
                            model,
                            version,
                            invocation,
                            transcript,
                            registry,
                            cache,
                            "streaming",
                        )
                    )
                    streaming_saved = True
                if transcript is not None and transcript_has_assistant_marker(transcript, end):
                    break
                time.sleep(0.02)
            else:
                raise RecorderError(f"timed out waiting for {geometry.name} {scenario}")
            if registry is None or transcript is None or cache is None:
                raise RecorderError("Claude binding metadata unavailable")
            time.sleep(0.5)
            final_name = f"{geometry.name}-{scenario}-complete"
            metadata.append(
                copy_snapshot(
                    output,
                    final_name,
                    tmux,
                    session_id,
                    model,
                    version,
                    invocation,
                    transcript,
                    registry,
                    cache,
                    "complete",
                )
            )
        if geometry.name == "wide" and not streaming_saved:
            raise RecorderError("no spinner-bearing streaming snapshot was captured")
        tmux.submit("/exit")
        return metadata, invocation
    except (RecorderError, subprocess.TimeoutExpired):
        save_failure_snapshot(root, failure_phase, tmux)
        raise
    finally:
        tmux.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "tests" / "fixtures" / "provider_live",
    )
    parser.add_argument("--scratch-root", type=Path, required=True)
    parser.add_argument("--model", default="claude-haiku-4-5")
    parser.add_argument("--prior-invocations", type=int, default=0)
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    scratch = arguments.scratch_root.resolve()
    if arguments.prior_invocations < 0:
        raise RecorderError("prior invocation count must be non-negative")
    output.mkdir(mode=0o755, parents=True, exist_ok=True)
    scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise RecorderError("fixture output directory must be empty")
    version = run(["claude", "--version"], env=os.environ.copy()).split()[0]
    all_metadata = []
    invocation = arguments.prior_invocations
    for geometry in GEOMETRIES:
        recorded, invocation = record_geometry(
            output,
            scratch,
            geometry,
            arguments.model,
            version,
            invocation,
        )
        all_metadata.extend(recorded)
    manifest = {
        "schema": 1,
        "claudeVersion": version,
        "model": arguments.model,
        "claudeInvocations": invocation,
        "fixtures": [item["scenario"] for item in all_metadata],
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"recorded {len(all_metadata)} fixtures with {invocation} Claude invocations")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
