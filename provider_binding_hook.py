#!/usr/bin/env python3
import argparse
import fcntl
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any


MAX_INPUT_BYTES = 64_000
MAX_OWNERSHIP_RANGES = 16
PROVIDER_EXECUTABLES = {
    "claude": {"claude"},
    "codex": {"codex"},
}
PANE_RE = re.compile(r"^%[0-9]+$")
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
END_EVENTS = {"SessionEnd", "session_end"}


def _proc_fields(pid: int, proc_root: Path = Path("/proc")) -> tuple[int, int]:
    value = (proc_root / str(pid) / "stat").read_text()
    close = value.rfind(")")
    fields = value[close + 2 :].split()
    if close < 0 or len(fields) < 20:
        raise ValueError
    return int(fields[1]), int(fields[19])


def _proc_environment(pid: int, proc_root: Path = Path("/proc")) -> dict[str, str]:
    raw = (proc_root / str(pid) / "environ").read_bytes()
    result: dict[str, str] = {}
    for item in raw.split(b"\0"):
        key, separator, value = item.partition(b"=")
        if separator:
            result[key.decode("ascii", "ignore")] = value.decode("utf-8", "surrogateescape")
    return result


def _provider_process(
    provider: str,
    pane_id: str,
    proc_root: Path = Path("/proc"),
    *,
    start_pid: int | None = None,
) -> tuple[int, int]:
    expected = PROVIDER_EXECUTABLES.get(provider)
    if expected is None:
        raise ValueError
    pid = os.getppid() if start_pid is None else start_pid
    for _ in range(32):
        parent, start = _proc_fields(pid, proc_root)
        environment = _proc_environment(pid, proc_root)
        if environment.get("TMUX_PANE") == pane_id:
            names: set[str] = set()
            try:
                names.add(Path(os.readlink(proc_root / str(pid) / "exe")).name.lower())
            except OSError:
                pass
            try:
                names.add((proc_root / str(pid) / "comm").read_text().strip().lower())
            except OSError:
                pass
            if names & expected:
                return pid, start
        if parent <= 1 or parent == pid:
            break
        pid = parent
    raise ValueError


def _tmux_environment() -> dict[str, str]:
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("MOBILE_TERMINAL_")
    }


def _pane_coordinates(pane_id: str) -> dict[str, int | bool]:
    result = subprocess.run(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            pane_id,
            "#{pane_id}\t#{history_size}\t#{history_limit}\t#{cursor_y}\t#{pane_height}\t#{alternate_on}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=1,
        env=_tmux_environment(),
    )
    fields = result.stdout.rstrip("\n").split("\t")
    if len(fields) != 6 or fields[0] != pane_id:
        raise ValueError
    history, history_limit, cursor_y, rows, alternate = (int(value) for value in fields[1:])
    if min(history, history_limit, cursor_y, rows) < 0 or rows <= 0 or cursor_y >= rows:
        raise ValueError
    return {
        "history": history,
        "historyLimit": history_limit,
        "cursorY": cursor_y,
        "rows": rows,
        "alternate": bool(alternate),
    }


def _claude_registry(home: Path, pane_id: str, session_id: str) -> tuple[int, int, str]:
    matches: list[tuple[int, int, str]] = []
    for path in (home / ".claude" / "sessions").glob("*.json"):
        try:
            data = json.loads(path.read_bytes())
            tmux = data.get("tmux")
            registry_pane = tmux.rsplit(".", 1)[-1] if isinstance(tmux, str) else ""
            if str(data.get("sessionId")) == session_id and registry_pane == pane_id:
                matches.append(
                    (int(data["pid"]), int(data["procStart"]), str(data.get("version", "")))
                )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
    if len(matches) != 1:
        raise ValueError
    return matches[0]


def _safe_transcript(provider: str, transcript: Path, home: Path) -> Path:
    root = home / (".claude/projects" if provider == "claude" else ".codex/sessions")
    if not transcript.is_absolute() or not root.is_dir():
        raise ValueError
    resolved_root = root.resolve(strict=True)
    resolved_parent = transcript.parent.resolve(strict=True)
    resolved_parent.relative_to(resolved_root)
    if transcript.is_symlink():
        raise ValueError
    return transcript


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(data, output, separators=(",", ":"), sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def update_binding(
    provider: str,
    event: dict[str, Any],
    version: str,
    home: Path,
    *,
    pane_coordinates: Any = _pane_coordinates,
) -> None:
    pane_id = os.environ.get("TMUX_PANE", "")
    if not PANE_RE.fullmatch(pane_id):
        raise ValueError
    input_pane = event.get("pane_id", event.get("paneId"))
    if input_pane is not None and input_pane != pane_id:
        raise ValueError
    session_id = str(
        event.get("session_id")
        or event.get("sessionId")
        or event.get("thread_id")
        or event.get("threadId")
        or ""
    )
    if not SESSION_RE.fullmatch(session_id):
        raise ValueError
    transcript_value = event.get("transcript_path") or event.get("transcriptPath") or event.get("rollout_path")
    if not isinstance(transcript_value, str):
        raise ValueError
    transcript = _safe_transcript(provider, Path(transcript_value), home)
    if provider == "claude":
        pid, proc_start, registry_version = _claude_registry(home, pane_id, session_id)
        if registry_version:
            version = registry_version
        coordinates = None
    else:
        pid, proc_start = _provider_process(provider, pane_id)
        try:
            coordinates = pane_coordinates(pane_id)
        except Exception:
            coordinates = None
    event_name = str(event.get("hook_event_name") or event.get("event") or event.get("type") or "")
    active = event_name not in END_EVENTS
    state_dir = home / ".mobile-terminal" / "provider-bindings"
    path = state_dir / f"{pane_id[1:]}.json"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = state_dir / ".lock"
    with lock_path.open("a+") as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        existing: dict[str, Any] = {}
        try:
            existing = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError):
            pass
        if (
            existing.get("active") is False
            and active
            and existing.get("provider") == provider
            and existing.get("sessionId") == session_id
            and int(existing.get("pid", -1)) == pid
            and str(existing.get("procStart", "")) == str(proc_start)
            and existing.get("terminalEvent") == "SessionEnd"
        ):
            return
        event_time = int(event.get("event_time_ns") or event.get("timestamp_ns") or time.time_ns())
        if event_time <= int(existing.get("eventTimeNs", 0)):
            return
        generation = int(existing.get("generation", 0)) + 1
        data: dict[str, Any] = {
            "schema": 1,
            "provider": provider,
            "paneId": pane_id,
            "sessionId": session_id,
            "transcriptPath": str(transcript),
            "pid": pid,
            "procStart": str(proc_start),
            "generation": generation,
            "eventTimeNs": event_time,
            "active": active,
            "terminalEvent": "SessionEnd" if event_name in END_EVENTS else "",
            "version": version,
        }
        if provider == "codex":
            ranges = existing.get("ownershipRanges", [])
            if not isinstance(ranges, list):
                ranges = []
            ranges = [item for item in ranges if isinstance(item, dict)][-MAX_OWNERSHIP_RANGES:]
            if not isinstance(coordinates, dict):
                data["ownershipUnavailable"] = True
                data["ownershipRanges"] = ranges
                _atomic_write(path, data)
                return
            history = int(coordinates["history"])
            history_limit = int(coordinates["historyLimit"])
            cursor_y = int(coordinates["cursorY"])
            rows = int(coordinates["rows"])
            alternate = bool(coordinates["alternate"])
            if min(history, history_limit, cursor_y, rows) < 0 or rows <= 0 or cursor_y >= rows:
                raise ValueError
            same_binding = (
                existing.get("provider") == provider
                and existing.get("sessionId") == session_id
                and int(existing.get("pid", -1)) == pid
                and str(existing.get("procStart", "")) == str(proc_start)
            )
            start_new_range = not same_binding or not ranges or not bool(existing.get("active"))
            if start_new_range:
                if ranges and ranges[-1].get("endRow") is None:
                    ranges[-1]["endRow"] = history + cursor_y
                ranges.append(
                    {
                        "sessionId": session_id,
                        "startRow": history + cursor_y,
                        "endRow": None,
                        "historyAtStart": history,
                        "historyLimit": history_limit,
                        "alternate": alternate,
                        "saturated": bool(history_limit and history >= history_limit),
                        "bindingGeneration": generation,
                    }
                )
            elif event_name in END_EVENTS:
                current = ranges[-1]
                if current.get("sessionId") == session_id and current.get("endRow") is None:
                    current["endRow"] = history + cursor_y
            data["ownershipRanges"] = ranges[-MAX_OWNERSHIP_RANGES:]
            data["ownershipSnapshot"] = {
                "history": history,
                "historyLimit": history_limit,
                "cursorY": cursor_y,
                "rows": rows,
                "alternate": alternate,
            }
        _atomic_write(path, data)


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--provider", choices=("claude", "codex"), required=True)
    parser.add_argument("--version", required=True)
    arguments = parser.parse_args()
    try:
        raw = os.read(0, MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            return 0
        event = json.loads(raw)
        if isinstance(event, dict):
            update_binding(arguments.provider, event, arguments.version, Path.home().resolve())
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
