#!/usr/bin/env python3
import argparse
import asyncio
import base64
import datetime
import fcntl
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import signal
import shlex
import struct
import subprocess
import termios
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
NODE_MODULES_ROOT = ROOT / "node_modules"
WS_PATH = "/_ws"
SETTINGS_PATH = ROOT / "mobile-terminal-settings.json"
USAGE_PATH = ROOT / "mobile-terminal-usage.json"
USAGE_RETENTION_DAYS = 365
USAGE_VERSION = 2
USAGE_DAY_FIELDS = (
    "sessions",
    "durationSeconds",
    "inputEvents",
    "commandsRun",
    "bytesIn",
    "bytesOut",
)
USAGE_HOUR_KEY_FORMAT = "%Y-%m-%dT%H"
MOBILE_COMPOSER_HISTORY_LIMIT = 200
COMPOSER_CAPTURE_CONTEXT_ROWS = 12
COMPOSER_CAPTURE_LOGICAL_LINES = 48
COMPOSER_CAPTURE_MAX_CHARS = 12000
COMPOSER_REFRESH_DELAYS = (0.02, 0.05, 0.09, 0.14, 0.2, 0.28, 0.38, 0.5)
MOBILE_COMPOSER_FORCE_CLEAR_BACKSPACES = 1024
FILE_TREE_MAX_ENTRIES = 600
FILE_READ_MAX_BYTES = 2_000_000
FILE_WRITE_MAX_BYTES = 2_000_000
FILE_BOOKMARK_MAX_ITEMS = 40
FILE_BOOKMARK_MAX_PATH_CHARS = 512
FILE_BOOKMARK_MAX_NAME_CHARS = 80
SSH_FS_TIMEOUT_SECONDS = 7
SSH_FS_OPTIONS = (
    "-o",
    "BatchMode=yes",
    "-o",
    "ConnectTimeout=5",
    "-o",
    "NumberOfPasswordPrompts=0",
    "-o",
    "StrictHostKeyChecking=accept-new",
)
LEFT_ARROW = "\u001b[D"
RIGHT_ARROW = "\u001b[C"
UP_ARROW = "\u001b[A"
DOWN_ARROW = "\u001b[B"
CTRL_E = "\u0005"
CTRL_U = "\u0015"
BRACKETED_PASTE_START = "\u001b[200~"
BRACKETED_PASTE_END = "\u001b[201~"
SSH_PATH_PATTERN = re.compile(r"^([A-Za-z0-9_][A-Za-z0-9_.@%+-]*):(.*)$")
SSH_FS_SCRIPT = r'''
import base64
import json
import os
import sys


def emit(payload):
    sys.stdout.write(json.dumps(payload))


try:
    operation = sys.argv[1]
    raw_path = base64.b64decode(sys.argv[2].encode("ascii")).decode("utf-8", "surrogateescape")
    max_entries = int(sys.argv[3])
    max_read = int(sys.argv[4])
    max_write = int(sys.argv[5])

    path = os.path.expandvars(os.path.expanduser(raw_path or "~"))
    path = os.path.abspath(path)

    if operation == "list":
        if not os.path.isdir(path):
            raise ValueError("Selected path is not a directory.")
        entries = []
        truncated = False
        with os.scandir(path) as iterator:
            for index, child in enumerate(iterator):
                if index >= max_entries:
                    truncated = True
                    break
                try:
                    is_dir = child.is_dir(follow_symlinks=True)
                    is_file = child.is_file(follow_symlinks=True)
                    stat_result = child.stat(follow_symlinks=True) if is_file else None
                except OSError:
                    is_dir = False
                    is_file = False
                    stat_result = None
                entries.append(
                    {
                        "name": child.name,
                        "path": os.path.abspath(child.path),
                        "type": "directory" if is_dir else "file" if is_file else "other",
                        "size": stat_result.st_size if stat_result and is_file else None,
                    }
                )
        entries.sort(key=lambda entry: (entry["type"] != "directory", entry["name"].lower()))
        emit({"path": path, "entries": entries, "truncated": truncated})
    elif operation == "read":
        if not os.path.isfile(path):
            raise ValueError("Selected path is not a file.")
        size = os.path.getsize(path)
        if size > max_read:
            raise ValueError(f"File is larger than {max_read // 1_000_000} MB.")
        with open(path, "rb") as handle:
            data = handle.read()
        if b"\x00" in data[:4096]:
            raise ValueError("Binary files are not editable here.")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Only UTF-8 text files are editable here.") from exc
        emit({"path": path, "name": os.path.basename(path) or path, "content": content})
    elif operation == "write":
        data = sys.stdin.buffer.read(max_write + 1)
        if len(data) > max_write:
            raise ValueError(f"File is larger than {max_write // 1_000_000} MB.")
        if os.path.exists(path) and not os.path.isfile(path):
            raise ValueError("Selected path is not a file.")
        parent = os.path.dirname(path) or "."
        if not os.path.isdir(parent):
            raise ValueError("Parent directory does not exist.")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Only UTF-8 text files are editable here.") from exc
        with open(path, "wb") as handle:
            handle.write(data)
        emit({"path": path, "name": os.path.basename(path) or path})
    else:
        raise ValueError("Unsupported file operation.")
except Exception as exc:
    emit({"error": str(exc)})
'''


def tmux_capture(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        cwd=ROOT,
        capture_output=True,
        check=check,
        text=True,
    )


def tailscale_capture(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tailscale", *args],
        cwd=ROOT,
        capture_output=True,
        check=check,
        text=True,
    )


def resolve_tailscale_host() -> str:
    result = tailscale_capture("ip", "-4", check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip() or "tailscale ip -4 failed"
        raise SystemExit(f"Unable to resolve Tailscale IPv4 address: {stderr}")

    for line in result.stdout.splitlines():
        candidate = line.strip()
        if candidate:
            return candidate

    raise SystemExit("Unable to resolve Tailscale IPv4 address: no IPv4 address returned")


def normalize_allowed_clients(raw_values: list[str]) -> list[str]:
    allowed: list[str] = []
    for value in raw_values:
        for piece in value.split(","):
            candidate = piece.strip()
            if not candidate:
                continue
            try:
                ipaddress.ip_address(candidate)
            except ValueError as exc:
                raise SystemExit(f"Invalid --allow-client address '{candidate}': {exc}") from exc
            allowed.append(candidate)
    return sorted(set(allowed))


def remote_ip(remote_address: Any) -> str | None:
    if isinstance(remote_address, tuple) and remote_address:
        host = remote_address[0]
    elif isinstance(remote_address, str):
        host = remote_address
    else:
        return None
    if isinstance(host, str) and host.startswith("::ffff:"):
        return host[7:]
    return host


def ensure_session(session_name: str, shell: str, cwd: str) -> None:
    has_session = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if has_session.returncode != 0:
        tmux_capture(
            "new-session",
            "-d",
            "-s",
            session_name,
            "-n",
            "shell",
            "-c",
            cwd,
            f"{shell} -l",
        )
    tmux_capture("set-option", "-g", "-p", "allow-passthrough", "on", check=False)
    for option, value in (("status", "off"), ("mouse", "on")):
        tmux_capture("set-option", "-t", session_name, option, value, check=False)


def session_exists(session_name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def list_sessions() -> list[dict[str, Any]]:
    output = tmux_capture(
        "list-sessions",
        "-F",
        "#{session_name}\t#{session_attached}\t#{session_windows}",
        check=False,
    )
    if output.returncode != 0:
        return []

    sessions: list[dict[str, Any]] = []
    for line in output.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        name, attached, windows = parts
        sessions.append(
            {
                "name": name,
                "attached": int(attached) if attached.isdigit() else 0,
                "windows": int(windows) if windows.isdigit() else 0,
            }
        )
    return sessions


def current_path(session_name: str, fallback: str) -> str:
    result = tmux_capture(
        "display-message",
        "-p",
        "-t",
        session_name,
        "#{pane_current_path}",
        check=False,
    )
    path = result.stdout.strip()
    return path or fallback


def capture_history(session_name: str, lines: int = 2000) -> str:
    result = tmux_capture(
        "capture-pane",
        "-e",
        "-J",
        "-p",
        "-S",
        f"-{max(0, lines)}",
        "-t",
        session_name,
        check=False,
    )
    if result.returncode != 0:
        return ""
    return result.stdout


def pane_in_mode(session_name: str) -> bool:
    result = tmux_capture(
        "display-message",
        "-p",
        "-t",
        session_name,
        "#{pane_in_mode}",
        check=False,
    )
    return result.stdout.strip() == "1"


def scroll_session_history(session_name: str, lines: int) -> None:
    count = abs(int(lines))
    if count == 0:
        return
    if not pane_in_mode(session_name):
        tmux_capture("copy-mode", "-e", "-t", session_name, check=False)
    command = "scroll-up" if lines > 0 else "scroll-down"
    tmux_capture("send-keys", "-t", session_name, "-X", "-N", str(count), command, check=False)


def list_session_clients(session_name: str) -> list[dict[str, str]]:
    result = tmux_capture(
        "list-clients",
        "-t",
        session_name,
        "-F",
        "#{client_tty}\t#{client_pid}",
        check=False,
    )
    if result.returncode != 0:
        return []

    clients: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        tty, _, pid = line.partition("\t")
        tty = tty.strip()
        pid = pid.strip()
        if tty:
            clients.append({"tty": tty, "pid": pid})
    return clients


def detach_other_clients(session_name: str, keep_pid: int | None = None) -> int:
    detached = 0
    keep_pid_str = str(keep_pid) if keep_pid else ""
    for client in list_session_clients(session_name):
        if keep_pid_str and client["pid"] == keep_pid_str:
            continue
        tmux_capture("detach-client", "-t", client["tty"], check=False)
        detached += 1
    return detached


def session_tabs(active_session: str) -> list[dict[str, Any]]:
    tabs: list[dict[str, Any]] = []
    for session in list_sessions():
        tabs.append(
            {
                "name": session["name"],
                "active": session["name"] == active_session,
                "attached": session["attached"],
                "windows": session["windows"],
            }
        )
    return tabs


def next_session_name(existing: set[str] | None = None) -> str:
    existing = existing if existing is not None else {session["name"] for session in list_sessions()}
    counter = 1
    while True:
        candidate = str(counter)
        if candidate not in existing:
            return candidate
        counter += 1


def default_settings() -> dict[str, Any]:
    return {
        "shortcuts": [
            {"label": "Esc", "sequence": "{ESC}", "visible": True},
            {"label": "📋", "sequence": "{PASTE}", "visible": True},
            {"label": "Copy", "sequence": "{COPY}", "visible": True},
            {"label": "Tab", "sequence": "{TAB}", "visible": True},
            {"label": "⬆️", "sequence": "{UP}", "visible": True},
            {"label": "⬇️", "sequence": "{DOWN}", "visible": True},
            {"label": "⬅️", "sequence": "{LEFT}", "visible": False},
            {"label": "➡️", "sequence": "{RIGHT}", "visible": False},
            {"label": "^+C", "sequence": "{CTRL+C}", "visible": True},
            {"label": "Ctrl+L", "sequence": "{CTRL+L}", "visible": False},
            {"label": "Ctrl+R", "sequence": "{CTRL+R}", "visible": False},
            {"label": "Ctrl+X Tab", "sequence": "{CTRL+X}{TAB}", "visible": False},
            {"label": "↩️", "sequence": "{ENTER}", "visible": True},
            {"label": "▶️", "sequence": "{TEXT:/resume}{ENTER}", "visible": True},
        ],
        "uiScale": 0.85,
        "terminalFontSize": 10,
        "fileBookmarks": [],
    }


def normalize_shortcuts(raw_shortcuts: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_shortcuts, list):
        return default_settings()["shortcuts"]
    normalized: list[dict[str, Any]] = []
    for item in raw_shortcuts:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).strip()
        sequence = str(item.get("sequence", "")).strip()
        if not label or not sequence:
            continue
        normalized.append(
            {
                "label": label[:40],
                "sequence": sequence[:120],
                "visible": item.get("visible", True) is not False,
            }
        )
    return normalized or default_settings()["shortcuts"]


def normalize_file_bookmarks(raw_bookmarks: Any) -> list[dict[str, str]]:
    if not isinstance(raw_bookmarks, list):
        return []
    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_bookmarks:
        if isinstance(item, str):
            path = item.strip()
            name = ""
        elif isinstance(item, dict):
            path = str(item.get("path", "")).strip()
            name = str(item.get("name", "")).strip()
        else:
            continue
        if not path:
            continue
        path = path[:FILE_BOOKMARK_MAX_PATH_CHARS]
        key = path.replace("\\", "/").rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        normalized.append(
            {
                "path": path,
                "name": name[:FILE_BOOKMARK_MAX_NAME_CHARS],
            }
        )
        if len(normalized) >= FILE_BOOKMARK_MAX_ITEMS:
            break
    return normalized


def normalize_settings(raw_settings: Any) -> dict[str, Any]:
    defaults = default_settings()
    if not isinstance(raw_settings, dict):
        return defaults

    try:
        ui_scale = float(raw_settings.get("uiScale", defaults["uiScale"]))
    except (TypeError, ValueError):
        ui_scale = defaults["uiScale"]
    ui_scale = min(1.4, max(0.5, ui_scale))

    try:
        terminal_font_size = int(raw_settings.get("terminalFontSize", defaults["terminalFontSize"]))
    except (TypeError, ValueError):
        terminal_font_size = defaults["terminalFontSize"]
    terminal_font_size = min(24, max(5, terminal_font_size))

    return {
        "shortcuts": normalize_shortcuts(raw_settings.get("shortcuts")),
        "uiScale": ui_scale,
        "terminalFontSize": terminal_font_size,
        "fileBookmarks": normalize_file_bookmarks(raw_settings.get("fileBookmarks")),
    }


def default_mobile_composer_state() -> dict[str, Any]:
    return {
        "history": [],
        "draft": "",
        "cursor": 0,
        "historyIndex": None,
        "pendingDraft": "",
        "tracked": False,
        "revision": 0,
        "source": "reset",
    }


def clamp_cursor(value: str, cursor: Any) -> int:
    try:
        position = int(cursor)
    except (TypeError, ValueError):
        position = len(value)
    return max(0, min(len(value), position))


def build_composer_sync_sequence(
    previous_value: str,
    previous_cursor: int,
    next_value: str,
    next_cursor: int,
) -> tuple[str, int]:
    current_value = previous_value or ""
    target_value = next_value or ""
    current_cursor = clamp_cursor(current_value, previous_cursor)
    target_cursor = clamp_cursor(target_value, next_cursor)

    common_prefix = 0
    max_prefix = min(len(current_value), len(target_value))
    while common_prefix < max_prefix and current_value[common_prefix] == target_value[common_prefix]:
        common_prefix += 1

    common_suffix = 0
    max_suffix = min(len(current_value), len(target_value)) - common_prefix
    while (
        common_suffix < max_suffix
        and current_value[len(current_value) - common_suffix - 1]
        == target_value[len(target_value) - common_suffix - 1]
    ):
        common_suffix += 1

    delete_start = common_prefix
    delete_end = len(current_value) - common_suffix
    insert_text = target_value[delete_start : len(target_value) - common_suffix]
    edit_cursor = delete_start + len(insert_text)

    sequence = ""
    move_to_delete_end = delete_end - current_cursor
    if move_to_delete_end > 0:
        sequence += RIGHT_ARROW * move_to_delete_end
    elif move_to_delete_end < 0:
        sequence += LEFT_ARROW * abs(move_to_delete_end)

    delete_count = delete_end - delete_start
    if delete_count > 0:
        sequence += "\u007f" * delete_count
    if insert_text:
        if "\n" in insert_text:
            sequence += BRACKETED_PASTE_START + insert_text + BRACKETED_PASTE_END
        else:
            sequence += insert_text

    move_to_target = target_cursor - edit_cursor
    if move_to_target > 0:
        sequence += RIGHT_ARROW * move_to_target
    elif move_to_target < 0:
        sequence += LEFT_ARROW * abs(move_to_target)
    return sequence, target_cursor


def unique_non_empty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique_values: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        unique_values.append(value)
    return unique_values


def strip_prompt_prefix(line: str) -> str | None:
    clean_line = line.replace("\r", "").rstrip()
    if not clean_line:
        return None
    for pattern in (
        r"^\s*[›❯]\s?(.*)$",
        r"^.*(?:^|\s)[$#>]\s?(.*)$",
    ):
        match = re.match(pattern, clean_line)
        if match:
            return match.group(1)
    return None


def load_settings() -> dict[str, Any]:
    if not SETTINGS_PATH.is_file():
        return default_settings()
    try:
        return normalize_settings(json.loads(SETTINGS_PATH.read_text()))
    except (OSError, json.JSONDecodeError):
        return default_settings()


def save_settings(settings: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_settings(settings)
    SETTINGS_PATH.write_text(json.dumps(normalized, indent=2) + "\n")
    return normalized


def empty_usage_bucket() -> dict[str, int]:
    return {field: 0 for field in USAGE_DAY_FIELDS}


def default_usage() -> dict[str, Any]:
    return {
        "version": USAGE_VERSION,
        "createdAt": datetime.datetime.now().isoformat(timespec="seconds"),
        "totals": empty_usage_bucket(),
        "days": {},
        "hours": {},
    }


def _read_bucket(raw: dict[str, Any]) -> dict[str, int]:
    bucket = empty_usage_bucket()
    for field in USAGE_DAY_FIELDS:
        try:
            bucket[field] = max(0, int(raw.get(field, 0)))
        except (TypeError, ValueError):
            pass
    return bucket


def normalize_usage(raw: Any) -> dict[str, Any]:
    base = default_usage()
    if not isinstance(raw, dict):
        return base
    created_at = raw.get("createdAt")
    if isinstance(created_at, str) and created_at:
        base["createdAt"] = created_at
    totals = raw.get("totals")
    if isinstance(totals, dict):
        base["totals"] = _read_bucket(totals)
    days = raw.get("days")
    if isinstance(days, dict):
        for key, value in days.items():
            if isinstance(key, str) and isinstance(value, dict):
                base["days"][key] = _read_bucket(value)
    hours = raw.get("hours")
    if isinstance(hours, dict):
        for key, value in hours.items():
            if isinstance(key, str) and isinstance(value, dict):
                base["hours"][key] = _read_bucket(value)
    return base


def load_usage() -> dict[str, Any]:
    if not USAGE_PATH.is_file():
        return default_usage()
    try:
        return normalize_usage(json.loads(USAGE_PATH.read_text()))
    except (OSError, json.JSONDecodeError):
        return default_usage()


def save_usage(usage: dict[str, Any]) -> None:
    try:
        USAGE_PATH.write_text(json.dumps(usage, indent=2, sort_keys=True) + "\n")
    except OSError:
        pass


def trim_usage_history(
    usage: dict[str, Any],
    today: datetime.date,
    retention_days: int = USAGE_RETENTION_DAYS,
) -> None:
    cutoff_date = today - datetime.timedelta(days=retention_days)
    cutoff_day = cutoff_date.isoformat()
    days = usage.get("days", {})
    for key in list(days.keys()):
        if key < cutoff_day:
            days.pop(key, None)
    hours = usage.get("hours", {})
    for key in list(hours.keys()):
        if key[:10] < cutoff_day:
            hours.pop(key, None)


def _hour_bucket(usage: dict[str, Any], moment: datetime.datetime) -> dict[str, int]:
    key = moment.strftime(USAGE_HOUR_KEY_FORMAT)
    bucket = usage["hours"].get(key)
    if bucket is None:
        bucket = empty_usage_bucket()
        usage["hours"][key] = bucket
    return bucket


def record_session_usage(
    usage: dict[str, Any],
    summary: dict[str, int],
    start_at: datetime.datetime,
    end_at: datetime.datetime | None = None,
) -> None:
    if end_at is None:
        end_at = start_at + datetime.timedelta(seconds=int(summary.get("durationSeconds", 0)))

    day_key = start_at.date().isoformat()
    day_bucket = usage["days"].get(day_key)
    if day_bucket is None:
        day_bucket = empty_usage_bucket()
        usage["days"][day_key] = day_bucket
    for field in USAGE_DAY_FIELDS:
        try:
            value = max(0, int(summary.get(field, 0)))
        except (TypeError, ValueError):
            value = 0
        day_bucket[field] += value
        usage["totals"][field] += value

    start_hour_bucket = _hour_bucket(usage, start_at)
    for field in ("sessions", "inputEvents", "commandsRun", "bytesIn", "bytesOut"):
        try:
            value = max(0, int(summary.get(field, 0)))
        except (TypeError, ValueError):
            value = 0
        start_hour_bucket[field] += value

    remaining = max(0, int(summary.get("durationSeconds", 0)))
    cursor = start_at
    while remaining > 0:
        hour_floor = cursor.replace(minute=0, second=0, microsecond=0)
        next_hour = hour_floor + datetime.timedelta(hours=1)
        seconds_until_next = int((next_hour - cursor).total_seconds())
        seconds_in_hour = min(remaining, max(1, seconds_until_next))
        bucket = _hour_bucket(usage, hour_floor)
        bucket["durationSeconds"] += seconds_in_hour
        remaining -= seconds_in_hour
        cursor = next_hour

    trim_usage_history(usage, start_at.date())


def resolve_user_path(raw_path: Any, base_path: str) -> Path:
    value = str(raw_path or "").strip()
    if not value:
        value = base_path
    expanded = os.path.expandvars(os.path.expanduser(value))
    candidate = Path(expanded)
    if not candidate.is_absolute():
        candidate = Path(base_path) / candidate
    return candidate.resolve()


def split_ssh_path(raw_path: Any) -> tuple[str, str] | None:
    value = str(raw_path or "").strip()
    if not value:
        return None
    match = SSH_PATH_PATTERN.match(value)
    if not match:
        return None
    host = match.group(1)
    remote_path = match.group(2).strip() or "~"
    return host, remote_path


def ssh_display_path(host: str, remote_path: str) -> str:
    return f"{host}:{remote_path}"


def file_entry(path: Path) -> dict[str, Any]:
    is_dir = path.is_dir()
    is_file = path.is_file()
    try:
        stat_result = path.stat()
    except OSError:
        stat_result = None
    return {
        "name": path.name or str(path),
        "path": str(path),
        "type": "directory" if is_dir else "file" if is_file else "other",
        "size": stat_result.st_size if stat_result and is_file else None,
    }


def list_file_entries(path: Path) -> tuple[list[dict[str, Any]], bool]:
    entries: list[dict[str, Any]] = []
    truncated = False
    for index, child in enumerate(path.iterdir()):
        if index >= FILE_TREE_MAX_ENTRIES:
            truncated = True
            break
        entries.append(file_entry(child))
    entries.sort(key=lambda entry: (entry["type"] != "directory", entry["name"].lower()))
    return entries, truncated


def read_text_file(path: Path) -> str:
    if not path.is_file():
        raise ValueError("Selected path is not a file.")
    size = path.stat().st_size
    if size > FILE_READ_MAX_BYTES:
        raise ValueError(f"File is larger than {FILE_READ_MAX_BYTES // 1_000_000} MB.")
    data = path.read_bytes()
    if b"\x00" in data[:4096]:
        raise ValueError("Binary files are not editable here.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Only UTF-8 text files are editable here.") from exc


def write_text_file(path: Path, content: Any) -> None:
    if path.exists() and not path.is_file():
        raise ValueError("Selected path is not a file.")
    text = str(content)
    if len(text.encode("utf-8")) > FILE_WRITE_MAX_BYTES:
        raise ValueError(f"File is larger than {FILE_WRITE_MAX_BYTES // 1_000_000} MB.")
    if not path.parent.is_dir():
        raise ValueError("Parent directory does not exist.")
    path.write_text(text, encoding="utf-8")


def ssh_file_payload(host: str, operation: str, remote_path: str, content: Any = None) -> dict[str, Any]:
    encoded_script = base64.b64encode(SSH_FS_SCRIPT.encode("utf-8")).decode("ascii")
    encoded_path = base64.b64encode(remote_path.encode("utf-8", "surrogateescape")).decode("ascii")
    runner = f'import base64;exec(base64.b64decode("{encoded_script}"))'
    command = " ".join(
        (
            "python3",
            "-c",
            shlex.quote(runner),
            shlex.quote(operation),
            shlex.quote(encoded_path),
            str(FILE_TREE_MAX_ENTRIES),
            str(FILE_READ_MAX_BYTES),
            str(FILE_WRITE_MAX_BYTES),
        )
    )
    input_bytes = None
    if operation == "write":
        input_bytes = str(content).encode("utf-8")
        if len(input_bytes) > FILE_WRITE_MAX_BYTES:
            raise ValueError(f"File is larger than {FILE_WRITE_MAX_BYTES // 1_000_000} MB.")

    def run_ssh(options: tuple[str, ...]) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            ["ssh", *options, host, command],
            cwd=ROOT,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=SSH_FS_TIMEOUT_SECONDS,
        )

    try:
        result = run_ssh(SSH_FS_OPTIONS)
    except FileNotFoundError as exc:
        raise ValueError("ssh is not installed on the server.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"SSH operation timed out for {host}.") from exc

    stdout = result.stdout.decode("utf-8", "replace")
    stderr = result.stderr.decode("utf-8", "replace").strip()
    if result.returncode != 0 and "Bad owner or permissions" in stderr and "ssh_config" in stderr:
        try:
            result = run_ssh(("-F", "none", *SSH_FS_OPTIONS))
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"SSH operation timed out for {host}.") from exc
        stdout = result.stdout.decode("utf-8", "replace")
        stderr = result.stderr.decode("utf-8", "replace").strip()
    output_lines = [line for line in stdout.splitlines() if line.strip()]
    output = output_lines[-1] if output_lines else ""
    if result.returncode != 0:
        message = stderr or output or f"ssh exited with status {result.returncode}"
        raise ValueError(f"{host}: {message}")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        message = stderr or "SSH returned an invalid file response."
        raise ValueError(f"{host}: {message}") from exc
    if payload.get("error"):
        raise ValueError(str(payload["error"]))
    return payload


def list_ssh_file_entries(host: str, remote_path: str) -> tuple[str, list[dict[str, Any]], bool]:
    payload = ssh_file_payload(host, "list", remote_path)
    resolved_path = str(payload.get("path") or remote_path)
    entries = []
    for entry in payload.get("entries", []):
        if not isinstance(entry, dict):
            continue
        child_path = str(entry.get("path") or "")
        entries.append(
            {
                "name": str(entry.get("name") or child_path or "item"),
                "path": ssh_display_path(host, child_path),
                "type": entry.get("type") if entry.get("type") in {"directory", "file", "other"} else "other",
                "size": entry.get("size") if isinstance(entry.get("size"), int) else None,
            }
        )
    return ssh_display_path(host, resolved_path), entries, payload.get("truncated") is True


def read_ssh_text_file(host: str, remote_path: str) -> tuple[str, str, str]:
    payload = ssh_file_payload(host, "read", remote_path)
    resolved_path = str(payload.get("path") or remote_path)
    name = str(payload.get("name") or Path(resolved_path).name or resolved_path)
    content = str(payload.get("content") or "")
    return ssh_display_path(host, resolved_path), name, content


def write_ssh_text_file(host: str, remote_path: str, content: Any) -> tuple[str, str]:
    payload = ssh_file_payload(host, "write", remote_path, content)
    resolved_path = str(payload.get("path") or remote_path)
    name = str(payload.get("name") or Path(resolved_path).name or resolved_path)
    return ssh_display_path(host, resolved_path), name


def safe_join(path: str) -> tuple[Path | None, str | None]:
    clean_path = urlsplit(path).path
    if clean_path == "/":
        clean_path = "/index.html"

    if clean_path.startswith("/static/"):
        root = STATIC_ROOT
        relative = clean_path.removeprefix("/static/")
    elif clean_path.startswith("/vendor/"):
        root = NODE_MODULES_ROOT
        relative = clean_path.removeprefix("/vendor/")
    else:
        root = STATIC_ROOT
        relative = clean_path.removeprefix("/")

    candidate = (root / relative).resolve()
    if not str(candidate).startswith(str(root.resolve())):
        return None, None
    content_type, _ = mimetypes.guess_type(candidate.name)
    return candidate, content_type


def http_response(status: int, body: bytes, content_type: str) -> Response:
    headers = Headers(
        {
            "Content-Type": content_type,
            "Content-Length": str(len(body)),
            "Cache-Control": "no-cache",
        }
    )
    reason = {
        200: "OK",
        403: "Forbidden",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
    }.get(status, "OK")
    return Response(status, reason, headers, body)


async def process_request(connection: ServerConnection, request: Request) -> Response | None:
    del connection
    path = urlsplit(request.path).path
    if path == WS_PATH:
        return None
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return None
    if request.headers.get(":method", "GET") not in ("GET", "HEAD"):
        return http_response(405, b"Method Not Allowed", "text/plain; charset=utf-8")
    if path == "/health":
        return http_response(200, b"ok\n", "text/plain; charset=utf-8")

    target, content_type = safe_join(path)
    if not target or not target.is_file():
        return http_response(404, b"Not Found", "text/plain; charset=utf-8")

    body = b"" if request.headers.get(":method") == "HEAD" else target.read_bytes()
    return http_response(200, body, content_type or "application/octet-stream")


class TmuxBridge:
    def __init__(self, session_name: str, shell: str, cwd: str, create_if_missing: bool = True) -> None:
        self.session_name = session_name
        self.shell = shell
        self.cwd = cwd
        self.create_if_missing = create_if_missing
        self.master_fd: int | None = None
        self.process: subprocess.Popen[bytes] | None = None

    def open(self) -> None:
        if self.create_if_missing:
            ensure_session(self.session_name, self.shell, self.cwd)
        master_fd, slave_fd = os.openpty()
        self.master_fd = master_fd
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["COLORTERM"] = "truecolor"
        self.process = subprocess.Popen(
            ["tmux", "attach-session", "-t", self.session_name],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=self.cwd,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        self.resize(140, 40)

    def resize(self, cols: int, rows: int) -> None:
        if self.master_fd is None:
            return
        packed = struct.pack("HHHH", rows, cols, 0, 0)
        fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, packed)
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGWINCH)

    async def read(self) -> bytes:
        if self.master_fd is None:
            return b""

        def _read() -> bytes:
            try:
                return os.read(self.master_fd, 65536)
            except OSError:
                return b""

        return await asyncio.to_thread(_read)

    def write(self, data: str) -> None:
        if self.master_fd is None:
            return
        os.write(self.master_fd, data.encode("utf-8", "surrogateescape"))

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None


class AppServer:
    def __init__(
        self,
        host: str,
        port: int,
        session_name: str,
        shell: str,
        cwd: str,
        token: str | None,
        require_token: bool,
        allowed_clients: list[str],
        tailscale_mode: bool,
    ) -> None:
        self.host = host
        self.port = port
        self.session_name = session_name
        self.shell = shell
        self.cwd = cwd
        self.token = token
        self.require_token = require_token
        self.allowed_clients = allowed_clients
        self.tailscale_mode = tailscale_mode
        self.settings = load_settings()
        self.mobile_composer_states: dict[str, dict[str, Any]] = {}
        self.usage = load_usage()
        self.active_sessions = 0

    async def send_json(self, connection: ServerConnection, payload: dict[str, Any]) -> None:
        await connection.send(json.dumps(payload))

    def client_is_allowed(self, remote_address_value: Any) -> bool:
        if not self.allowed_clients:
            return True
        host = remote_ip(remote_address_value)
        return host in self.allowed_clients

    def client_is_trusted(self, remote_address_value: Any) -> bool:
        if not self.allowed_clients:
            return False
        return self.client_is_allowed(remote_address_value)

    async def process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        path = urlsplit(request.path).path
        if not self.client_is_allowed(connection.remote_address):
            return http_response(403, b"Forbidden\n", "text/plain; charset=utf-8")
        if path == "/config":
            trusted_client = self.client_is_trusted(connection.remote_address)
            payload = {
                "requireToken": self.require_token and not trusted_client,
                "tailscaleMode": self.tailscale_mode,
                "allowedClients": self.allowed_clients,
                "host": self.host,
                "port": self.port,
            }
            body = json.dumps(payload).encode("utf-8")
            return http_response(200, body, "application/json; charset=utf-8")
        if path == "/stats":
            body = json.dumps(self.usage_payload()).encode("utf-8")
            return http_response(200, body, "application/json; charset=utf-8")
        return await process_request(connection, request)

    def usage_payload(self) -> dict[str, Any]:
        return {
            "usage": self.usage,
            "today": datetime.date.today().isoformat(),
            "retentionDays": USAGE_RETENTION_DAYS,
            "activeSessions": self.active_sessions,
            "serverStartedAt": getattr(self, "started_at", None),
        }

    def record_session(
        self,
        summary: dict[str, int],
        start_at: datetime.datetime,
        end_at: datetime.datetime,
    ) -> None:
        record_session_usage(self.usage, summary, start_at, end_at)
        save_usage(self.usage)

    async def send_stats(self, connection: ServerConnection) -> None:
        await self.send_json(connection, {"type": "stats", **self.usage_payload()})

    async def send_tabs(self, connection: ServerConnection, session_name: str) -> list[dict[str, Any]]:
        tabs = session_tabs(session_name)
        await self.send_json(connection, {"type": "tabs", "tabs": tabs})
        return tabs

    async def send_sessions(self, connection: ServerConnection, active_session: str) -> list[dict[str, Any]]:
        sessions = list_sessions()
        await self.send_json(
            connection,
            {"type": "sessions", "sessions": sessions, "activeSession": active_session},
        )
        return sessions

    async def send_settings(self, connection: ServerConnection) -> dict[str, Any]:
        await self.send_json(
            connection,
            {
                "type": "settings",
                "settings": self.settings,
                "persisted": SETTINGS_PATH.is_file(),
            },
        )
        return self.settings

    def mobile_composer_state(self, session_name: str) -> dict[str, Any]:
        state = self.mobile_composer_states.get(session_name)
        if state is None:
            state = default_mobile_composer_state()
            self.mobile_composer_states[session_name] = state
        return state

    async def send_composer_state(self, connection: ServerConnection, session_name: str) -> None:
        state = self.mobile_composer_state(session_name)
        await self.send_json(
            connection,
            {
                "type": "composer-state",
                "value": state["draft"],
                "cursor": state["cursor"],
                "tracked": state["tracked"],
                "revision": state["revision"],
                "source": state["source"],
            },
        )

    def reset_mobile_composer_tracking(self, session_name: str) -> None:
        state = self.mobile_composer_state(session_name)
        state["draft"] = ""
        state["cursor"] = 0
        state["historyIndex"] = None
        state["pendingDraft"] = ""
        state["tracked"] = False
        state["source"] = "reset"

    def force_clear_mobile_composer(self, bridge: TmuxBridge, session_name: str, revision: int | None = None) -> None:
        if pane_in_mode(session_name):
            tmux_capture("send-keys", "-t", session_name, "-X", "cancel", check=False)
        sequence = CTRL_E + ("\u007f" * MOBILE_COMPOSER_FORCE_CLEAR_BACKSPACES) + CTRL_U
        bridge.write(sequence)
        self.reset_mobile_composer_tracking(session_name)
        state = self.mobile_composer_state(session_name)
        if revision is not None:
            state["revision"] = max(state["revision"], revision)
        state["source"] = "force-clear"

    def sync_mobile_composer(
        self,
        bridge: TmuxBridge,
        session_name: str,
        value: str,
        cursor: Any,
        *,
        revision: int | None = None,
        reset_history_index: bool = True,
    ) -> dict[str, Any]:
        state = self.mobile_composer_state(session_name)
        next_value = value.replace("\r\n", "\n").replace("\r", "\n")
        sequence, next_cursor = build_composer_sync_sequence(
            state["draft"],
            state["cursor"],
            next_value,
            cursor,
        )
        if pane_in_mode(session_name):
            tmux_capture("send-keys", "-t", session_name, "-X", "cancel", check=False)
        if sequence:
            bridge.write(sequence)
        state["draft"] = next_value
        state["cursor"] = next_cursor
        if revision is not None:
            state["revision"] = max(state["revision"], revision)
        if reset_history_index:
            state["historyIndex"] = None
            state["pendingDraft"] = next_value
        state["tracked"] = True
        state["source"] = "composer-sync"
        return state

    def commit_mobile_composer(self, bridge: TmuxBridge, session_name: str, revision: int | None = None) -> None:
        state = self.mobile_composer_state(session_name)
        line = state["draft"]
        if line:
            history = state["history"]
            history.append(line)
            if len(history) > MOBILE_COMPOSER_HISTORY_LIMIT:
                del history[:-MOBILE_COMPOSER_HISTORY_LIMIT]
        if revision is not None:
            state["revision"] = max(state["revision"], revision)
        self.reset_mobile_composer_tracking(session_name)
        if pane_in_mode(session_name):
            tmux_capture("send-keys", "-t", session_name, "-X", "cancel", check=False)
        bridge.write("\r")

    def fallback_mobile_composer_history(
        self,
        bridge: TmuxBridge,
        session_name: str,
        direction: str,
        revision: int | None = None,
    ) -> dict[str, Any] | None:
        state = self.mobile_composer_state(session_name)
        history = state["history"]
        if not history:
            return None

        history_index = state["historyIndex"]
        if direction == "up":
            if history_index is None:
                state["pendingDraft"] = state["draft"] if state["tracked"] else ""
                history_index = len(history) - 1
            elif history_index > 0:
                history_index -= 1
        elif direction == "down":
            if history_index is None:
                return None
            if history_index < len(history) - 1:
                history_index += 1
            else:
                history_index = None
        else:
            return None

        next_value = state["pendingDraft"] if history_index is None else history[history_index]
        next_state = self.sync_mobile_composer(
            bridge,
            session_name,
            next_value,
            len(next_value),
            revision=revision,
            reset_history_index=False,
        )
        next_state["historyIndex"] = history_index
        next_state["source"] = "history-fallback"
        return next_state

    def extract_terminal_composer_text(self, session_name: str) -> tuple[str, str] | None:
        capture_result = tmux_capture(
            "capture-pane",
            "-p",
            "-J",
            "-N",
            "-t",
            session_name,
            check=False,
        )
        if capture_result.returncode != 0:
            return None

        logical_lines = [line.replace("\r", "") for line in capture_result.stdout.split("\n")]
        while logical_lines and logical_lines[-1] == "":
            logical_lines.pop()
        if not logical_lines:
            return None

        candidate_lines = logical_lines[-COMPOSER_CAPTURE_LOGICAL_LINES:]
        for index in range(len(candidate_lines) - 1, -1, -1):
            stripped_line = strip_prompt_prefix(candidate_lines[index])
            if stripped_line is None:
                continue
            block_lines = [stripped_line, *candidate_lines[index + 1 :]]
            block = "\n".join(line.rstrip() for line in block_lines).rstrip()
            if not block:
                return None
            if len(block) > COMPOSER_CAPTURE_MAX_CHARS:
                block = block[-COMPOSER_CAPTURE_MAX_CHARS :]
            return block, "terminal-extract"

        return None

    def capture_visible_mobile_composer_text(self, session_name: str) -> tuple[str, str] | None:
        extracted = self.extract_terminal_composer_text(session_name)
        if extracted is not None:
            return extracted

        state = self.mobile_composer_state(session_name)
        candidates = unique_non_empty(
            [
                state["draft"],
                state["pendingDraft"],
                *reversed(state["history"][-MOBILE_COMPOSER_HISTORY_LIMIT:]),
            ]
        )
        if not candidates:
            return None

        cursor_result = tmux_capture(
            "display-message",
            "-p",
            "-t",
            session_name,
            "#{cursor_y}\t#{pane_height}",
            check=False,
        )
        capture_result = tmux_capture(
            "capture-pane",
            "-p",
            "-N",
            "-t",
            session_name,
            check=False,
        )
        if cursor_result.returncode != 0 or capture_result.returncode != 0:
            return None

        try:
            cursor_y_raw, _pane_height_raw = cursor_result.stdout.strip().split("\t", 1)
            cursor_y = max(0, int(cursor_y_raw))
        except (TypeError, ValueError):
            return None

        rows = capture_result.stdout.replace("\r", "").split("\n")
        if rows and rows[-1] == "":
            rows.pop()
        if not rows:
            return None

        clamped_cursor_y = min(cursor_y, len(rows) - 1)
        context_end = clamped_cursor_y + 1
        context_start = max(0, context_end - COMPOSER_CAPTURE_CONTEXT_ROWS)
        context_text = "".join(rows[context_start:context_end])
        if not context_text:
            return None

        best_match = ""
        for candidate in candidates:
            if candidate in context_text and len(candidate) > len(best_match):
                best_match = candidate
        if not best_match:
            return None
        return best_match, "history-substring"

    async def refresh_mobile_composer_from_terminal(
        self,
        session_name: str,
        revision: int | None = None,
    ) -> dict[str, Any] | None:
        state = self.mobile_composer_state(session_name)
        baseline_draft = state["draft"]
        previous_delay = 0.0
        for index, delay in enumerate(COMPOSER_REFRESH_DELAYS):
            await asyncio.sleep(max(0.0, delay - previous_delay))
            previous_delay = delay
            visible_state = self.capture_visible_mobile_composer_text(session_name)
            if visible_state is None:
                continue
            visible_text, source = visible_state
            if visible_text == baseline_draft and index < len(COMPOSER_REFRESH_DELAYS) - 1:
                continue
            state["draft"] = visible_text
            state["cursor"] = len(visible_text)
            state["tracked"] = True
            if revision is not None:
                state["revision"] = max(state["revision"], revision)
            state["source"] = source
            return state
        return None

    async def navigate_mobile_composer_history(
        self,
        bridge: TmuxBridge,
        session_name: str,
        direction: str,
        revision: int | None = None,
    ) -> dict[str, Any] | None:
        arrow = {"up": UP_ARROW, "down": DOWN_ARROW}.get(direction)
        if not arrow:
            return None

        state = self.mobile_composer_state(session_name)
        if direction == "up" and state["historyIndex"] is None:
            state["pendingDraft"] = state["draft"]
        bridge.write(arrow)
        next_state = await self.refresh_mobile_composer_from_terminal(session_name, revision=revision)
        if next_state is not None:
            next_state["historyIndex"] = None
            return next_state

        return self.fallback_mobile_composer_history(
            bridge,
            session_name,
            direction,
            revision=revision,
        )

    async def handle_command(
        self,
        connection: ServerConnection,
        bridge: TmuxBridge,
        state: dict[str, str],
        payload: dict[str, Any],
    ) -> None:
        session_name = state["session"]
        message_type = payload.get("type")
        try:
            revision = int(payload.get("revision", 0))
        except (TypeError, ValueError):
            revision = 0
        if message_type == "composer-sync":
            self.sync_mobile_composer(
                bridge,
                session_name,
                str(payload.get("value", "")),
                payload.get("cursor"),
                revision=revision,
            )
            return

        if message_type == "composer-semantic-sync":
            composer_state = self.mobile_composer_state(session_name)
            composer_state["revision"] = max(composer_state["revision"], revision)
            source = str(payload.get("source", "semantic-osc133") or "semantic-osc133")
            tracked = payload.get("tracked", True) is not False
            if not tracked:
                self.reset_mobile_composer_tracking(session_name)
                composer_state = self.mobile_composer_state(session_name)
                composer_state["revision"] = max(composer_state["revision"], revision)
                composer_state["source"] = source
                await self.send_composer_state(connection, session_name)
                return

            next_value = str(payload.get("value", "")).replace("\r\n", "\n").replace("\r", "\n")
            composer_state["draft"] = next_value
            composer_state["cursor"] = clamp_cursor(next_value, payload.get("cursor"))
            composer_state["historyIndex"] = None
            composer_state["pendingDraft"] = next_value
            composer_state["tracked"] = True
            composer_state["source"] = source
            await self.send_composer_state(connection, session_name)
            return

        if message_type == "composer-enter":
            self.commit_mobile_composer(bridge, session_name, revision=revision)
            await self.send_composer_state(connection, session_name)
            return

        if message_type == "composer-history":
            next_state = await self.navigate_mobile_composer_history(
                bridge,
                session_name,
                str(payload.get("direction", "")).lower(),
                revision=revision,
            )
            if next_state is not None:
                await self.send_composer_state(connection, session_name)
            return

        if message_type == "composer-refresh":
            next_state = await self.refresh_mobile_composer_from_terminal(
                session_name,
                revision=revision,
            )
            if next_state is not None:
                await self.send_composer_state(connection, session_name)
            return

        if message_type == "composer-reset":
            composer_state = self.mobile_composer_state(session_name)
            composer_state["revision"] = max(composer_state["revision"], revision)
            self.reset_mobile_composer_tracking(session_name)
            await self.send_composer_state(connection, session_name)
            return

        if message_type == "composer-force-clear":
            self.force_clear_mobile_composer(bridge, session_name, revision=revision)
            await self.send_composer_state(connection, session_name)
            return

        if message_type == "input":
            if pane_in_mode(session_name):
                tmux_capture("send-keys", "-t", session_name, "-X", "cancel", check=False)
            bridge.write(payload.get("data", ""))
            if payload.get("data"):
                self.reset_mobile_composer_tracking(session_name)
            return

        if message_type == "resize":
            cols = max(20, int(payload.get("cols", 80)))
            rows = max(6, int(payload.get("rows", 24)))
            bridge.resize(cols, rows)
            return

        if message_type == "scroll-history":
            lines = int(payload.get("lines", 0))
            scroll_session_history(session_name, lines)
            return

        if message_type == "request-tabs":
            await self.send_tabs(connection, session_name)
            return

        if message_type == "request-sessions":
            await self.send_sessions(connection, session_name)
            return

        if message_type == "request-settings":
            await self.send_settings(connection)
            return

        if message_type == "request-stats":
            await self.send_stats(connection)
            return

        if message_type == "save-settings":
            self.settings = save_settings(payload.get("settings", {}))
            await self.send_settings(connection)
            return

        if message_type == "fs-default-root":
            await self.send_json(
                connection,
                {
                    "type": "fs-default-root",
                    "requestId": str(payload.get("requestId", "")),
                    "path": current_path(session_name, self.cwd),
                    "home": str(Path.home()),
                },
            )
            return

        if message_type == "fs-list":
            request_id = str(payload.get("requestId", ""))
            try:
                ssh_target = split_ssh_path(payload.get("path"))
                if ssh_target:
                    host, remote_path = ssh_target
                    display_path, entries, truncated = await asyncio.to_thread(
                        list_ssh_file_entries,
                        host,
                        remote_path,
                    )
                    path_value = display_path
                else:
                    path = resolve_user_path(payload.get("path"), current_path(session_name, self.cwd))
                    if not path.is_dir():
                        raise ValueError("Selected path is not a directory.")
                    entries, truncated = list_file_entries(path)
                    path_value = str(path)
                await self.send_json(
                    connection,
                    {
                        "type": "fs-list",
                        "requestId": request_id,
                        "path": path_value,
                        "entries": entries,
                        "truncated": truncated,
                    },
                )
            except (OSError, ValueError) as exc:
                await self.send_json(
                    connection,
                    {
                        "type": "fs-error",
                        "requestId": request_id,
                        "operation": "list",
                        "message": str(exc),
                    },
                )
            return

        if message_type == "fs-read":
            request_id = str(payload.get("requestId", ""))
            try:
                ssh_target = split_ssh_path(payload.get("path"))
                if ssh_target:
                    host, remote_path = ssh_target
                    path_value, name, content = await asyncio.to_thread(read_ssh_text_file, host, remote_path)
                else:
                    path = resolve_user_path(payload.get("path"), current_path(session_name, self.cwd))
                    content = read_text_file(path)
                    path_value = str(path)
                    name = path.name
                await self.send_json(
                    connection,
                    {
                        "type": "fs-read",
                        "requestId": request_id,
                        "path": path_value,
                        "name": name,
                        "content": content,
                    },
                )
            except (OSError, ValueError) as exc:
                await self.send_json(
                    connection,
                    {
                        "type": "fs-error",
                        "requestId": request_id,
                        "operation": "read",
                        "message": str(exc),
                    },
                )
            return

        if message_type == "fs-write":
            request_id = str(payload.get("requestId", ""))
            try:
                ssh_target = split_ssh_path(payload.get("path"))
                if ssh_target:
                    host, remote_path = ssh_target
                    path_value, name = await asyncio.to_thread(
                        write_ssh_text_file,
                        host,
                        remote_path,
                        payload.get("content", ""),
                    )
                else:
                    path = resolve_user_path(payload.get("path"), current_path(session_name, self.cwd))
                    write_text_file(path, payload.get("content", ""))
                    path_value = str(path)
                    name = path.name
                await self.send_json(
                    connection,
                    {
                        "type": "fs-write",
                        "requestId": request_id,
                        "path": path_value,
                        "name": name,
                    },
                )
            except (OSError, ValueError) as exc:
                await self.send_json(
                    connection,
                    {
                        "type": "fs-error",
                        "requestId": request_id,
                        "operation": "write",
                        "message": str(exc),
                    },
                )
            return

        if message_type == "new-tab":
            path = current_path(session_name, self.cwd)
            next_name = next_session_name()
            tmux_capture(
                "new-session",
                "-d",
                "-s",
                next_name,
                "-n",
                "shell",
                "-c",
                path,
                f"{self.shell} -l",
                check=False,
            )
            for option, value in (("status", "off"), ("mouse", "on")):
                tmux_capture("set-option", "-t", next_name, option, value, check=False)
            await self.send_tabs(connection, session_name)
            await self.send_sessions(connection, session_name)
            await self.send_json(connection, {"type": "session-created", "session": next_name})
            return

        if message_type == "rename-tab":
            name = str(payload.get("name", "")).strip()[:40]
            target_name = str(payload.get("session", session_name)).strip() or session_name
            if name:
                if target_name != name and session_exists(name):
                    await self.send_json(
                        connection,
                        {"type": "notice", "message": f"Session '{name}' already exists."},
                    )
                    return
                tmux_capture("rename-session", "-t", target_name, name, check=False)
                if target_name in self.mobile_composer_states:
                    self.mobile_composer_states[name] = self.mobile_composer_states.pop(target_name)
                if target_name == session_name:
                    state["session"] = name
                await self.send_json(
                    connection,
                    {"type": "session-renamed", "oldSession": target_name, "session": name},
                )
                await self.send_tabs(connection, state["session"])
                await self.send_sessions(connection, state["session"])
            return

        if message_type == "close-tab":
            await self.send_json(
                connection,
                {
                    "type": "notice",
                    "message": "Close Tab only hides the session in this browser.",
                },
            )
            return

        if message_type == "kill-session":
            target_name = str(payload.get("session", session_name)).strip() or session_name
            sessions = list_sessions()
            if not any(session["name"] == target_name for session in sessions):
                await self.send_json(
                    connection,
                    {"type": "notice", "message": f"Session '{target_name}' is not running."},
                )
                await self.send_tabs(connection, state["session"])
                await self.send_sessions(connection, state["session"])
                return
            if target_name == session_name:
                remaining = [session["name"] for session in sessions if session["name"] != target_name]
                remaining_names = {session["name"] for session in sessions if session["name"] != target_name}
                fallback = remaining[0] if remaining else next_session_name(remaining_names)
                await self.send_json(
                    connection,
                    {
                        "type": "session-closing",
                        "closedSession": target_name,
                        "nextSession": fallback,
                    },
                )
            tmux_capture("kill-session", "-t", target_name, check=False)
            if target_name == session_name:
                await connection.close(code=1012, reason="session killed")
                return
            await self.send_tabs(connection, session_name)
            await self.send_sessions(connection, session_name)
            return

        if message_type == "detach-other-clients":
            target_name = str(payload.get("session", session_name)).strip() or session_name
            keep_pid = bridge.process.pid if target_name == session_name and bridge.process else None
            detached = detach_other_clients(target_name, keep_pid=keep_pid)
            if detached:
                await self.send_json(
                    connection,
                    {
                        "type": "notice",
                        "message": f"Detached {detached} other tmux client(s) from {target_name}.",
                    },
                )
            else:
                await self.send_json(
                    connection,
                    {
                        "type": "notice",
                        "message": f"No other tmux clients were attached to {target_name}.",
                    },
                )
            await self.send_tabs(connection, session_name)
            await self.send_sessions(connection, session_name)
            return

    async def websocket_handler(self, connection: ServerConnection) -> None:
        request_url = urlsplit(connection.request.path)
        if request_url.path != WS_PATH:
            await connection.close(code=1008, reason="invalid path")
            return
        if not self.client_is_allowed(connection.remote_address):
            await connection.close(code=4003, reason="forbidden")
            return

        trusted_client = self.client_is_trusted(connection.remote_address)
        if not trusted_client:
            try:
                raw_auth = await asyncio.wait_for(connection.recv(), timeout=20)
            except TimeoutError:
                await connection.close(code=4001, reason="auth timeout")
                return

            if not isinstance(raw_auth, str):
                await connection.close(code=4001, reason="auth required")
                return

            try:
                auth_payload = json.loads(raw_auth)
            except json.JSONDecodeError:
                await connection.close(code=4001, reason="auth required")
                return

            token_ok = True
            if self.require_token:
                token_ok = self.token is not None and hmac.compare_digest(
                    str(auth_payload.get("token", "")),
                    self.token,
                )

            if auth_payload.get("type") != "auth" or not token_ok:
                await self.send_json(connection, {"type": "auth-error", "message": "Invalid access token."})
                await connection.close(code=4001, reason="auth failed")
                return

        requested_session = parse_qs(request_url.query).get("session", [""])[0].strip()
        skip_history = parse_qs(request_url.query).get("skip_history", [""])[0].strip().lower() in (
            "1",
            "true",
            "yes",
        )
        session_name = self.session_name
        create_if_missing = True
        requested_session_missing = False
        if requested_session:
            if session_exists(requested_session):
                session_name = requested_session
                create_if_missing = False
            else:
                requested_session_missing = True
                session_name = next_session_name()
        elif not session_exists(session_name):
            session_name = next_session_name()

        state = {"session": session_name}
        bridge = TmuxBridge(session_name, self.shell, self.cwd, create_if_missing=create_if_missing)
        bridge.open()
        history = "" if skip_history else capture_history(state["session"])

        session_summary: dict[str, int] = {
            "sessions": 1,
            "durationSeconds": 0,
            "inputEvents": 0,
            "commandsRun": 0,
            "bytesIn": 0,
            "bytesOut": 0,
        }
        session_start = time.monotonic()
        session_start_at = datetime.datetime.now()
        self.active_sessions += 1

        async def relay_output() -> None:
            while True:
                chunk = await bridge.read()
                if not chunk:
                    break
                session_summary["bytesOut"] += len(chunk)
                await connection.send(chunk)

        async def watch_tabs() -> None:
            previous = ""
            while True:
                tabs = session_tabs(state["session"])
                snapshot = json.dumps(tabs, sort_keys=True)
                if snapshot != previous:
                    previous = snapshot
                    await self.send_json(connection, {"type": "tabs", "tabs": tabs})
                await asyncio.sleep(1)

        output_task = asyncio.create_task(relay_output())
        tab_task = asyncio.create_task(watch_tabs())

        try:
            await self.send_json(
                connection,
                {
                    "type": "ready",
                    "session": state["session"],
                    "shell": self.shell,
                    "cwd": self.cwd,
                    "requireToken": self.require_token and not trusted_client,
                    "tailscaleMode": self.tailscale_mode,
                    "allowedClients": self.allowed_clients,
                },
            )
            if history:
                await connection.send(history.encode("utf-8", "surrogateescape"))
            if requested_session_missing:
                await self.send_json(
                    connection,
                    {
                        "type": "notice",
                        "message": f"Session '{requested_session}' is not running. Attached to {session_name}.",
                    },
                )
            await self.send_tabs(connection, state["session"])
            await self.send_sessions(connection, state["session"])
            await self.send_settings(connection)
            await self.send_composer_state(connection, state["session"])

            async for raw_message in connection:
                if not isinstance(raw_message, str):
                    continue
                session_summary["bytesIn"] += len(raw_message)
                try:
                    payload = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                msg_type = payload.get("type")
                if msg_type and msg_type != "request-stats":
                    session_summary["inputEvents"] += 1
                if msg_type == "composer-enter":
                    session_summary["commandsRun"] += 1
                elif msg_type == "input":
                    data = payload.get("data", "")
                    if isinstance(data, str) and ("\r" in data or "\n" in data):
                        session_summary["commandsRun"] += data.count("\r") + data.count("\n")
                await self.handle_command(connection, bridge, state, payload)
        except ConnectionClosed:
            pass
        finally:
            output_task.cancel()
            tab_task.cancel()
            bridge.close()
            session_summary["durationSeconds"] = int(time.monotonic() - session_start)
            self.active_sessions = max(0, self.active_sessions - 1)
            self.record_session(session_summary, session_start_at, datetime.datetime.now())

    async def run(self) -> None:
        self.started_at = datetime.datetime.now().isoformat(timespec="seconds")
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(signal_name, stop_event.set)
            except (NotImplementedError, RuntimeError):
                pass

        async with serve(
            self.websocket_handler,
            self.host,
            self.port,
            process_request=self.process_request,
            ping_interval=20,
            ping_timeout=20,
            max_size=2**20,
        ):
            print("")
            print(f"mobile-terminal listening on http://{self.host}:{self.port}")
            print(f"tmux session: {self.session_name}")
            print(f"login shell: {self.shell}")
            if self.tailscale_mode:
                print("network mode: tailscale-only")
            if self.allowed_clients:
                print(f"allowed clients: {', '.join(self.allowed_clients)}")
            if self.require_token:
                print(f"access token: {self.token}")
            else:
                print("access token: disabled")
            print("")
            await stop_event.wait()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mobile-friendly browser terminal for tmux.")
    parser.add_argument("--host", default=os.environ.get("MOBILE_TERMINAL_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOBILE_TERMINAL_PORT", "8085")))
    parser.add_argument("--session", default=os.environ.get("MOBILE_TERMINAL_SESSION", "mobile-terminal"))
    parser.add_argument("--cwd", default=os.environ.get("MOBILE_TERMINAL_CWD", str(Path.home())))
    parser.add_argument("--shell", default=os.environ.get("MOBILE_TERMINAL_SHELL", os.environ.get("SHELL", "/bin/bash")))
    parser.add_argument("--token", default=os.environ.get("MOBILE_TERMINAL_TOKEN"))
    parser.add_argument(
        "--tailscale",
        action="store_true",
        default=os.environ.get("MOBILE_TERMINAL_TAILSCALE", "").lower() in ("1", "true", "yes"),
        help="Bind only to the local Tailscale IPv4 address.",
    )
    parser.add_argument(
        "--no-token",
        action="store_true",
        default=os.environ.get("MOBILE_TERMINAL_NO_TOKEN", "").lower() in ("1", "true", "yes"),
        help="Disable access-token auth. Use this only with Tailscale or another trusted network boundary.",
    )
    parser.add_argument(
        "--allow-client",
        action="append",
        default=[],
        help="Allow only these remote IPs to connect. Repeat or pass a comma-separated list.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.tailscale:
        args.host = resolve_tailscale_host()

    allowed_clients = normalize_allowed_clients(
        [os.environ.get("MOBILE_TERMINAL_ALLOW_CLIENTS", ""), *args.allow_client]
    )
    if args.no_token and not args.tailscale and not allowed_clients and args.host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("--no-token requires --tailscale, --allow-client, or a loopback-only host")
    require_token = not args.no_token
    token = (args.token or secrets.token_urlsafe(16)) if require_token else None
    server = AppServer(
        host=args.host,
        port=args.port,
        session_name=args.session,
        shell=args.shell,
        cwd=args.cwd,
        token=token,
        require_token=require_token,
        allowed_clients=allowed_clients,
        tailscale_mode=args.tailscale,
    )
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
