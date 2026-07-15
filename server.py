#!/usr/bin/env python3
import argparse
import asyncio
import base64
import datetime
import fcntl
import gzip
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


# Lines of scrollback sent on connect. Normal-buffer panes scroll this locally
# in the client's xterm buffer (no round-trip), so we send enough to cover the
# full tmux history range. It's gzip-compressed over the socket, and xterm paints
# the visible screen immediately, so first paint stays fast.
CONNECT_HISTORY_LINES = 2000


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


# Sequences xterm.js emits on its own, without a user keystroke: SGR mouse
# reports (claude enables any-motion tracking, so mere pointer movement emits
# these), DA/DSR-style CSI query replies, OSC replies (color queries), and DCS
# replies (XTVERSION). These must not cancel copy-mode or drop queued scrolls.
NON_KEYSTROKE_INPUT_RE = re.compile(
    r"(?:\x1b\[<\d+;\d+;\d+[Mm]"  # SGR mouse report
    r"|\x1b\[\??\d+(?:;\d+)*[cRn]"  # DA / DSR / cursor position replies
    r"|\x1b\[>\d+(?:;\d+)*c"  # secondary DA reply
    r"|\x1b\[[IO]"  # focus in/out report
    r"|\x1b\[\??\d+(?:;\d+)*\$y"  # DECRQM reply
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC reply
    r"|\x1bP[^\x1b]*\x1b\\"  # DCS reply
    r")+"
)


def input_is_user_keystroke(data: str) -> bool:
    if not data:
        return False
    return NON_KEYSTROKE_INPUT_RE.fullmatch(data) is None


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


def pane_scrolls_locally(session_name: str) -> bool:
    """True when the pane is a normal-buffer app (shell, codex, ...): not on the
    alternate screen and not mouse-tracking. Such panes keep their transcript in
    the client's xterm buffer, so the client can scroll it locally with no server
    round-trip. Alt-screen / mouse-tracking TUIs (claude, pagers) redraw their own
    viewport, so those still scroll via the server (copy-mode / wheel / arrows)."""
    result = tmux_capture(
        "display-message",
        "-p",
        "-t",
        session_name,
        "#{alternate_on} #{mouse_any_flag} #{mouse_sgr_flag}",
        check=False,
    )
    parts = result.stdout.split()
    alternate_on = len(parts) > 0 and parts[0] == "1"
    mouse_tracking = len(parts) > 2 and parts[1] == "1" and parts[2] == "1"
    return not alternate_on and not mouse_tracking


MAX_SCROLL_EVENTS_PER_CALL = 200


def scroll_session_history(session_name: str, lines: int) -> None:
    count = min(abs(int(lines)), MAX_SCROLL_EVENTS_PER_CALL)
    if count == 0:
        return
    result = tmux_capture(
        "display-message",
        "-p",
        "-t",
        session_name,
        "#{alternate_on} #{mouse_any_flag} #{mouse_sgr_flag} #{pane_in_mode} #{pane_width} #{pane_height}",
        check=False,
    )
    parts = result.stdout.split()
    alternate_on = len(parts) > 0 and parts[0] == "1"
    # Only forward wheel events when the pane speaks SGR encoding (mode 1006);
    # a tracking-but-not-SGR pane would misparse them, so it falls through to
    # the arrow-key path instead.
    mouse_tracking = len(parts) > 2 and parts[1] == "1" and parts[2] == "1"
    in_mode = len(parts) > 3 and parts[3] == "1"
    try:
        pane_width = max(1, int(parts[4]))
        pane_height = max(1, int(parts[5]))
    except (IndexError, ValueError):
        pane_width, pane_height = 80, 24

    if (mouse_tracking or alternate_on) and in_mode:
        # A pane stuck in copy-mode (entered before scrolling switched to key
        # forwarding) would swallow forwarded keys as copy-mode commands.
        tmux_capture("send-keys", "-t", session_name, "-X", "cancel", check=False)

    if mouse_tracking:
        # Mouse-enabled TUIs (claude, codex) keep their transcript in-app: it
        # never enters tmux history, so copy-mode has almost nothing to scroll
        # into and paints stale pre-launch shell output over a frozen frame.
        # Do what a real terminal does instead: forward SGR wheel events so the
        # app scrolls its own buffer, one event per line delta (apps applying
        # their own lines-per-event multiplier just make flicks travel
        # farther). Coordinates land mid-pane; embedded semicolons are safe in
        # a send-keys -l argument (only a trailing one would be parsed as a
        # command separator).
        x = max(1, pane_width // 2)
        y = max(1, pane_height // 2)
        button = 64 if lines > 0 else 65
        sequence = f"\x1b[<{button};{x};{y}M" * count
        tmux_capture("send-keys", "-t", session_name, "-l", sequence, check=False)
        return

    if alternate_on:
        # Alternate screen without mouse tracking (pagers, etc.): tmux history
        # is unreachable and copy-mode would show pre-launch content, so fall
        # back to arrow keys the app can interpret as scrolling.
        key = "Up" if lines > 0 else "Down"
        tmux_capture("send-keys", "-t", session_name, "-N", str(count), key, check=False)
        return

    command = "scroll-up" if lines > 0 else "scroll-down"
    # Chained into one tmux invocation: copy-mode is a no-op when the pane is
    # already in a mode, and the ";" separator makes enter+scroll atomic so the
    # mode can't exit between a separate check and the scroll keys.
    tmux_capture(
        "copy-mode",
        "-e",
        "-t",
        session_name,
        ";",
        "send-keys",
        "-t",
        session_name,
        "-X",
        "-N",
        str(count),
        command,
        check=False,
    )


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


BTOP_SESSION_PREFIX = "btop-"
# Only simple, tmux-safe target ids are allowed so the target round-trips
# through the session name (btop-<target>) and survives a server restart.
BTOP_TARGET_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def ssh_config_hosts() -> list[dict[str, str]]:
    """[{alias, hostname}] for each non-pattern Host entry in ~/.ssh/config.

    The alias is the tmux/session-safe target id; hostname is the address used
    for the reachability ping (falls back to the alias when no HostName is set).
    """
    path = Path.home() / ".ssh" / "config"
    hosts: list[dict[str, str]] = []
    seen: set[str] = set()
    current: dict[str, str] | None = None
    try:
        lines = path.read_text().splitlines()
    except (FileNotFoundError, PermissionError, OSError):
        return hosts
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"host\s+(.+)", stripped, re.IGNORECASE)
        if match:
            current = None
            for token in match.group(1).split():
                # Skip wildcard/negated patterns like "*" or "!prod".
                if any(ch in token for ch in "*?!"):
                    continue
                # Keep only the first usable, tmux/session-safe alias per line.
                if BTOP_TARGET_PATTERN.match(token) and token not in seen:
                    seen.add(token)
                    current = {"alias": token, "hostname": token}
                    hosts.append(current)
                break
            continue
        if current is not None:
            hostname = re.match(r"hostname\s+(\S+)", stripped, re.IGNORECASE)
            if hostname:
                current["hostname"] = hostname.group(1)
    return hosts


def ssh_host_aliases() -> list[str]:
    return [host["alias"] for host in ssh_config_hosts()]


def btop_targets(reachable: set[str] | None = None) -> list[dict[str, str]]:
    """Local plus every SSH host that is currently reachable.

    When reachable is None (reachability not yet probed) no remotes are listed,
    so a host only appears once it has actually pinged.
    """
    targets = [{"id": "local", "label": "Local (this computer)"}]
    reachable = reachable or set()
    for host in ssh_config_hosts():
        if host["alias"] in reachable:
            targets.append({"id": host["alias"], "label": host["alias"]})
    return targets


def btop_command_for_target(target: str) -> str | None:
    """Shell command tmux should run in the pane for a given btop target.

    Uses a login shell (`bash -lc`) so btop is found even when it lives in
    ~/.local/bin or behind a version manager that only sets PATH on login.
    Returns None for an unknown/invalid target.
    """
    if target == "local":
        return "bash -lc btop"
    if not BTOP_TARGET_PATTERN.match(target) or target not in ssh_host_aliases():
        return None
    return f"ssh -t {shlex.quote(target)} 'bash -lc btop'"


async def ping_host(hostname: str, timeout: float = 3.0) -> bool:
    """True if the host answers a single ICMP echo within ~1s."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            "ping",
            "-c",
            "1",
            "-W",
            "1",
            hostname,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        returncode = await asyncio.wait_for(proc.wait(), timeout=timeout)
        return returncode == 0
    except (asyncio.TimeoutError, OSError):
        if proc is not None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        return False


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
            {"label": "Shift+Tab", "sequence": "{SHIFT+TAB}", "visible": False},
            {"label": "↩️", "sequence": "{ENTER}", "visible": True},
            {"label": "▶️", "sequence": "{TEXT:/resume}{ENTER}", "visible": True},
        ],
        "uiScale": 0.85,
        "terminalFontSize": 10,
        "fileBookmarks": [],
        "gestures": {},
    }


def normalize_gestures(raw_gestures: Any) -> dict[str, dict[str, Any]]:
    """Multi-touch gesture bindings: { gestureId: {sequence, enabled} }.

    Stored leniently — the client owns the gesture catalog and merges these
    over its defaults, so unknown ids are harmless and simply preserved.
    """
    if not isinstance(raw_gestures, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for key, value in raw_gestures.items():
        if not isinstance(key, str) or not isinstance(value, dict):
            continue
        normalized[key[:40]] = {
            "sequence": str(value.get("sequence", ""))[:200],
            "enabled": value.get("enabled", True) is not False,
        }
    return normalized


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
        "gestures": normalize_gestures(raw_settings.get("gestures")),
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


# ---------------------------------------------------------------------------
# Multi-tenancy (optional)
#
# When mobile-terminal-users.json exists, the server runs multi-tenant: each
# user has a shared token, their own set of tmux sessions (tabs), their own
# settings, and a device registry. Tenancy is enforced only in-app under the one
# OS account — it is an organizational boundary, not a security boundary (every
# tab is a real shell as this user). Absent the file, the server is single-tenant
# exactly as before.
# ---------------------------------------------------------------------------

USERS_PATH = Path(os.environ.get("MOBILE_TERMINAL_USERS", str(ROOT / "mobile-terminal-users.json")))
STATE_ROOT = ROOT / "state" / "users"
USER_NAME_RE = re.compile(r"[^a-z0-9_-]+")
# New per-user sessions are named "mt_<user>__<n>" so they're globally unique
# under the one tmux server and cheaply attributable to a tenant.
USER_SESSION_SEP = "__"


def sanitize_user(name: Any) -> str:
    return USER_NAME_RE.sub("-", str(name).strip().lower())[:32]


def device_label(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    for needle, label in (
        ("iphone", "iPhone"),
        ("ipad", "iPad"),
        ("android", "Android"),
        ("macintosh", "Mac"),
        ("mac os", "Mac"),
        ("windows", "Windows"),
        ("cros", "ChromeOS"),
        ("linux", "Linux"),
    ):
        if needle in ua:
            return label
    return "device"


def load_users_config() -> dict[str, Any] | None:
    """Parse mobile-terminal-users.json → {owner, users:{name:{token,label}}} or None."""
    if not USERS_PATH.is_file():
        return None
    try:
        raw = json.loads(USERS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    raw_users = raw.get("users") if isinstance(raw, dict) else None
    if not isinstance(raw_users, dict) or not raw_users:
        return None
    users: dict[str, dict[str, Any]] = {}
    for name, value in raw_users.items():
        user = sanitize_user(name)
        if not user or not isinstance(value, dict):
            continue
        token = str(value.get("token", "")).strip()
        if not token:
            continue
        users[user] = {
            "token": token,
            "label": (str(value.get("label", name)).strip() or user),
            # Optional: the Tailscale identity (e.g. "alice@github") that maps to
            # this user, for token-less auto-login behind `tailscale serve`.
            "tailscaleLogin": str(value.get("tailscaleLogin", "")).strip().lower(),
        }
    if not users:
        return None
    owner = sanitize_user(raw.get("owner", "")) if isinstance(raw, dict) else ""
    if owner not in users:
        owner = next(iter(users))
    return {"owner": owner, "users": users}


def persist_users_config(users: dict[str, dict[str, Any]], owner: str) -> None:
    payload = {
        "owner": owner,
        "users": {
            name: {
                "token": meta["token"],
                "label": meta.get("label", name),
                **({"tailscaleLogin": meta["tailscaleLogin"]} if meta.get("tailscaleLogin") else {}),
            }
            for name, meta in users.items()
        },
    }
    USERS_PATH.write_text(json.dumps(payload, indent=2) + "\n")


def user_dir(user: str) -> Path:
    return STATE_ROOT / sanitize_user(user)


def user_settings_path(user: str) -> Path:
    return user_dir(user) / "settings.json"


def load_user_settings(user: str) -> dict[str, Any]:
    path = user_settings_path(user)
    if not path.is_file():
        return default_settings()
    try:
        return normalize_settings(json.loads(path.read_text()))
    except (OSError, json.JSONDecodeError):
        return default_settings()


def save_user_settings(user: str, settings: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_settings(settings)
    path = user_settings_path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(normalized, indent=2) + "\n")
    return normalized


def user_state_path(user: str) -> Path:
    return user_dir(user) / "sessions.json"


def load_user_state(user: str) -> dict[str, Any]:
    """Per-user owned-session map + device registry: {owned:{name:{label}}, devices:{id:{...}}}."""
    state: dict[str, Any] = {"owned": {}, "devices": {}}
    path = user_state_path(user)
    if path.is_file():
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                if isinstance(raw.get("owned"), dict):
                    state["owned"] = raw["owned"]
                if isinstance(raw.get("devices"), dict):
                    state["devices"] = raw["devices"]
        except (OSError, json.JSONDecodeError):
            pass
    return state


def save_user_state(user: str, state: dict[str, Any]) -> None:
    path = user_state_path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


def migrate_to_multitenant(owner: str) -> None:
    """One-time seeding: owner inherits the global settings file and every
    tmux session currently running, so the machine owner keeps their setup."""
    owner_dir = user_dir(owner)
    owner_dir.mkdir(parents=True, exist_ok=True)
    settings_dest = user_settings_path(owner)
    if not settings_dest.is_file() and SETTINGS_PATH.is_file():
        try:
            save_user_settings(owner, json.loads(SETTINGS_PATH.read_text()))
        except (OSError, json.JSONDecodeError):
            pass
    state_dest = user_state_path(owner)
    if not state_dest.is_file():
        owned = {session["name"]: {"label": session["name"]} for session in list_sessions()}
        save_user_state(owner, {"owned": owned, "devices": {}})


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


# Add-to-home-screen icon: a terminal window with the machine's label baked in
# (e.g. "ph", "lat"), so each deployment's home-screen icon is distinguishable.
# Rendered on demand with Pillow and cached per (label, size).
_APP_ICON_CACHE: dict[tuple[str, int], bytes] = {}
_ICON_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
)


def render_app_icon(label: str, size: int) -> bytes:
    key = (label, size)
    cached = _APP_ICON_CACHE.get(key)
    if cached is not None:
        return cached
    import io

    from PIL import Image, ImageDraw, ImageFont

    bg = (11, 18, 27)
    prompt_color = (255, 183, 3)
    text_color = (130, 207, 255)
    img = Image.new("RGB", (size, size), bg)
    draw = ImageDraw.Draw(img)

    def load_font(px: int) -> Any:
        for path in _ICON_FONTS:
            try:
                return ImageFont.truetype(path, px)
            except OSError:
                continue
        return ImageFont.load_default()

    # Title-bar traffic-light dots.
    dot_r = max(3, int(size * 0.03))
    dot_y = int(size * 0.2)
    for i, color in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        cx = int(size * 0.16) + i * int(size * 0.1)
        draw.ellipse([cx - dot_r, dot_y - dot_r, cx + dot_r, dot_y + dot_r], fill=color)

    # ">_" prompt above the label.
    prompt_font = load_font(int(size * 0.17))
    draw.text((int(size * 0.15), int(size * 0.31)), ">_", font=prompt_font, fill=prompt_color)

    # Label, shrunk to fit the width.
    text = label or "term"
    max_w = int(size * 0.6)
    font_px = int(size * 0.38)
    while font_px > 10:
        font = load_font(font_px)
        if draw.textlength(text, font=font) <= max_w:
            break
        font_px -= 2
    font = load_font(font_px)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (size - tw) / 2 - bbox[0]
    ty = int(size * 0.5) - bbox[1]
    draw.text((tx, ty), text, font=font, fill=text_color)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = buf.getvalue()
    _APP_ICON_CACHE[key] = data
    return data


def http_response(
    status: int, body: bytes, content_type: str, extra_headers: dict[str, str] | None = None
) -> Response:
    fields = {
        "Content-Type": content_type,
        "Content-Length": str(len(body)),
        "Cache-Control": "no-cache",
    }
    if extra_headers:
        fields.update(extra_headers)
    reason = {
        200: "OK",
        304: "Not Modified",
        403: "Forbidden",
        401: "Unauthorized",
        404: "Not Found",
        405: "Method Not Allowed",
        500: "Internal Server Error",
    }.get(status, "OK")
    return Response(status, reason, Headers(fields), body)


_COMPRESSIBLE = ("text/", "application/javascript", "application/json", "application/manifest", "image/svg")


def static_file_response(target: Path, content_type: str | None, request: Request) -> Response:
    """Serve a static file with an ETag (so unchanged assets 304 with no body)
    and gzip for text. Versioned /vendor/ assets are cached long + immutable;
    everything else must revalidate via the ETag."""
    ctype = content_type or "application/octet-stream"
    try:
        stat = target.stat()
    except OSError:
        return http_response(404, b"Not Found", "text/plain; charset=utf-8")
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    # Vendor assets (xterm) rarely change: cache a day, then revalidate via ETag.
    # Everything else must revalidate every load (ETag -> 304 when unchanged).
    is_vendor = urlsplit(request.path).path.startswith("/vendor/")
    cache_control = "public, max-age=86400" if is_vendor else "no-cache"
    if etag in request.headers.get("If-None-Match", ""):
        return http_response(304, b"", ctype, {"ETag": etag, "Cache-Control": cache_control})
    if request.headers.get(":method") == "HEAD":
        return http_response(200, b"", ctype, {"ETag": etag, "Cache-Control": cache_control})
    body = target.read_bytes()
    extra = {"ETag": etag, "Cache-Control": cache_control}
    accepts_gzip = "gzip" in request.headers.get("Accept-Encoding", "")
    if accepts_gzip and len(body) > 1024 and ctype.startswith(_COMPRESSIBLE):
        body = gzip.compress(body, 6)
        extra["Content-Encoding"] = "gzip"
        extra["Vary"] = "Accept-Encoding"
    return http_response(200, body, ctype, extra)


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
    return static_file_response(target, content_type, request)


class TmuxBridge:
    def __init__(
        self,
        session_name: str,
        shell: str,
        cwd: str,
        create_if_missing: bool = True,
        initial_size: tuple[int, int] | None = None,
    ) -> None:
        self.session_name = session_name
        self.shell = shell
        self.cwd = cwd
        self.create_if_missing = create_if_missing
        self.initial_size = initial_size
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
        # A wrong-size attach flaps the tmux window width (window-size=latest),
        # which rewraps the pane and can destroy its history; prefer the last
        # size this session's client reported over the generic default.
        cols, rows = self.initial_size or (140, 40)
        self.resize(cols, rows)

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
        users_config: dict[str, Any] | None = None,
        label: str = "term",
    ) -> None:
        self.host = host
        self.port = port
        self.label = (label or "term").strip()[:12] or "term"
        self.session_name = session_name
        self.shell = shell
        self.cwd = cwd
        self.token = token
        self.require_token = require_token
        self.allowed_clients = allowed_clients
        self.tailscale_mode = tailscale_mode
        self.multi_tenant = users_config is not None
        self.users: dict[str, dict[str, Any]] = (users_config or {}).get("users", {})
        self.owner = (users_config or {}).get("owner", "")
        if self.multi_tenant:
            migrate_to_multitenant(self.owner)
        self.settings = load_settings()
        self.mobile_composer_states: dict[str, dict[str, Any]] = {}
        self.usage = load_usage()
        self.active_sessions = 0
        self.scroll_states: dict[str, dict[str, Any]] = {}
        self.terminal_sizes: dict[str, tuple[int, int]] = {}
        # Aliases of SSH hosts that answered the last ping sweep. Starts empty so
        # a remote target only appears in the btop picker once it's reachable.
        self.btop_reachable: set[str] = set()

    async def btop_ping_loop(self, interval: float = 30.0) -> None:
        """Refresh the reachable-SSH-host set every `interval` seconds."""
        while True:
            hosts = ssh_config_hosts()
            reachable: set[str] = set()
            if hosts:
                results = await asyncio.gather(
                    *(ping_host(host["hostname"]) for host in hosts),
                    return_exceptions=True,
                )
                for host, ok in zip(hosts, results):
                    if ok is True:
                        reachable.add(host["alias"])
            self.btop_reachable = reachable
            await asyncio.sleep(interval)

    def queue_scroll_history(self, session_name: str, lines: int) -> None:
        if lines == 0:
            return
        state = self.scroll_states.setdefault(session_name, {"pending": 0, "task": None})
        state["pending"] += lines
        task = state["task"]
        if task is None or task.done():
            state["task"] = asyncio.create_task(self.drain_scroll_history(session_name, state))

    async def drain_scroll_history(self, session_name: str, state: dict[str, Any]) -> None:
        # Deltas that arrive while a tmux call is in flight accumulate in
        # "pending" and get merged into one call on the next iteration, so the
        # event loop is never blocked and tmux sees at most one process at a
        # time per session.
        while state["pending"] != 0:
            lines = state["pending"]
            # Consume at most one call's worth; the remainder stays pending so
            # a burst-merged flick is deferred to the next iteration, not lost.
            take = max(-MAX_SCROLL_EVENTS_PER_CALL, min(MAX_SCROLL_EVENTS_PER_CALL, lines))
            state["pending"] = lines - take
            await asyncio.to_thread(scroll_session_history, session_name, take)

    async def settle_scroll_history(self, session_name: str) -> None:
        # Scroll commands execute out-of-band; before any handler cancels
        # copy-mode and writes keys to the pane, drop queued deltas and wait
        # out the in-flight tmux call so a stale scroll can't re-enter
        # copy-mode and swallow the keystrokes.
        state = self.scroll_states.get(session_name)
        if state is None:
            return
        state["pending"] = 0
        task = state["task"]
        if task is not None and not task.done():
            try:
                await task
            except Exception:
                pass

    async def send_json(self, connection: ServerConnection, payload: dict[str, Any]) -> None:
        await connection.send(json.dumps(payload))

    def client_is_allowed(self, remote_address_value: Any) -> bool:
        host = remote_ip(remote_address_value)
        # Loopback = a reverse proxy on this host (tailscale serve), which
        # terminates TLS and forwards the real, tailnet-authenticated client.
        # Let it past the network gate; the token still gates access (see
        # client_is_trusted, which deliberately does NOT trust loopback).
        if host in ("127.0.0.1", "::1"):
            return True
        if not self.allowed_clients:
            return True
        return host in self.allowed_clients

    def client_is_trusted(self, remote_address_value: Any) -> bool:
        if not self.allowed_clients:
            return False
        host = remote_ip(remote_address_value)
        # A proxied (loopback) connection carries no client IP, so never treat it
        # as token-exempt — require the token for anything coming via the proxy.
        if host in ("127.0.0.1", "::1"):
            return False
        return host in self.allowed_clients

    def proxy_login(self, connection: ServerConnection) -> str | None:
        """The Tailscale identity that `tailscale serve` injected, but only when
        the request actually came through the local proxy (loopback source). A
        direct connection could forge the header, so it's ignored there."""
        host = remote_ip(connection.remote_address)
        if host not in ("127.0.0.1", "::1"):
            return None
        login = connection.request.headers.get("Tailscale-User-Login")
        return login.strip().lower() if login else None

    def auto_auth_user(self, connection: ServerConnection) -> str | None:
        """Map the proxy-provided Tailscale identity to a user for token-less
        login. Returns the username (multi-tenant), "" for the single implicit
        user (single-tenant), or None when no auto-login applies."""
        login = self.proxy_login(connection)
        if not login:
            return None
        if not self.multi_tenant:
            return ""  # single-tenant: any tailnet identity via serve skips the token
        for name, meta in self.users.items():
            if meta.get("tailscaleLogin") and meta["tailscaleLogin"] == login:
                return name
        return None

    def token_required_for(self, trusted_client: bool) -> bool:
        """Whether the client must supply a token. In single-tenant mode an
        allow-listed (trusted) IP is token-exempt for convenience. In
        multi-tenant mode the per-user token is always required (the IP
        allow-list is only a network gate) so users are genuinely authenticated."""
        if not self.require_token:
            return False
        if self.multi_tenant:
            return True
        return not trusted_client

    # --- multi-tenancy: scoping, settings, ownership, devices ---------------

    def owned_names(self, user: str) -> set[str]:
        return set(load_user_state(user)["owned"].keys())

    def claimed_by_others(self, user: str) -> set[str]:
        claimed: set[str] = set()
        for other in self.users:
            if other == user:
                continue
            claimed |= set(load_user_state(other)["owned"].keys())
        return claimed

    def visible_session_names(self, user: str) -> set[str]:
        """Sessions a user may see/act on. Non-owner: only their owned set.
        Owner: everything not claimed by another user (so ad-hoc tmux sessions
        started outside the app still appear)."""
        names = {session["name"] for session in list_sessions()}
        if not self.multi_tenant:
            return names
        if user == self.owner:
            return names - self.claimed_by_others(user)
        return names & self.owned_names(user)

    def tabs_for_user(self, user: str, active_session: str) -> list[dict[str, Any]]:
        visible = self.visible_session_names(user)
        owned = load_user_state(user)["owned"] if self.multi_tenant else {}
        tabs: list[dict[str, Any]] = []
        for session in list_sessions():
            if session["name"] not in visible:
                continue
            meta = owned.get(session["name"])
            label = meta.get("label") if isinstance(meta, dict) else None
            tabs.append(
                {
                    "name": session["name"],
                    "label": label or session["name"],
                    "active": session["name"] == active_session,
                    "attached": session["attached"],
                    "windows": session["windows"],
                }
            )
        return tabs

    def sessions_for_user(self, user: str) -> list[dict[str, Any]]:
        visible = self.visible_session_names(user)
        owned = load_user_state(user)["owned"] if self.multi_tenant else {}
        result: list[dict[str, Any]] = []
        for session in list_sessions():
            if session["name"] not in visible:
                continue
            meta = owned.get(session["name"])
            label = meta.get("label") if isinstance(meta, dict) else None
            result.append({**session, "label": label or session["name"]})
        return result

    def can_access(self, user: str, session_name: str) -> bool:
        if not self.multi_tenant:
            return True
        return session_name in self.visible_session_names(user)

    def claim_session(self, user: str, name: str, label: str) -> None:
        if not self.multi_tenant:
            return
        state = load_user_state(user)
        state["owned"][name] = {"label": label}
        save_user_state(user, state)

    def release_session(self, user: str, name: str) -> None:
        if not self.multi_tenant:
            return
        state = load_user_state(user)
        if name in state["owned"]:
            del state["owned"][name]
            save_user_state(user, state)

    def create_user_session(self, user: str, cwd: str | None = None) -> str:
        existing = {session["name"] for session in list_sessions()}
        if self.multi_tenant:
            base = f"mt_{sanitize_user(user)}{USER_SESSION_SEP}"
            counter = 1
            while f"{base}{counter}" in existing:
                counter += 1
            name = f"{base}{counter}"
            label = str(counter)
        else:
            name = next_session_name(existing)
            label = name
        path = cwd or self.cwd
        tmux_capture(
            "new-session", "-d", "-s", name, "-n", "shell", "-c", path, f"{self.shell} -l", check=False
        )
        for option, value in (("status", "off"), ("mouse", "on")):
            tmux_capture("set-option", "-t", name, option, value, check=False)
        self.claim_session(user, name, label)
        return name

    def resolve_user_session(self, user: str, requested: str) -> tuple[str, bool]:
        """Return (session_name, created). A user may only attach to a session in
        their visible set; anything else falls back to an existing owned session
        or a freshly created one."""
        if requested and self.can_access(user, requested) and session_exists(requested):
            return requested, False
        if self.multi_tenant:
            for name in load_user_state(user)["owned"]:
                if session_exists(name):
                    return name, False
            return self.create_user_session(user), True
        # Legacy single-tenant behaviour.
        if session_exists(self.session_name):
            return self.session_name, False
        return next_session_name(), True

    def settings_for(self, user: str) -> dict[str, Any]:
        return load_user_settings(user) if self.multi_tenant else self.settings

    def save_settings_for(self, user: str, raw: Any) -> dict[str, Any]:
        if self.multi_tenant:
            return save_user_settings(user, raw or {})
        self.settings = save_settings(raw or {})
        return self.settings

    def settings_persisted(self, user: str) -> bool:
        return user_settings_path(user).is_file() if self.multi_tenant else SETTINGS_PATH.is_file()

    def device_remembered(self, user: str, device_id: str) -> bool:
        if not device_id:
            return False
        return device_id in load_user_state(user).get("devices", {})

    def register_device(self, user: str, device_id: str, label: str) -> None:
        if not self.multi_tenant or not device_id:
            return
        state = load_user_state(user)
        devices = state["devices"]
        now = datetime.datetime.now().isoformat(timespec="seconds")
        entry = devices.get(device_id) if isinstance(devices.get(device_id), dict) else {"firstSeen": now}
        entry["label"] = (label or entry.get("label") or "device")[:60]
        entry["lastSeen"] = now
        devices[device_id] = entry
        save_user_state(user, state)

    async def process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        path = urlsplit(request.path).path
        if not self.client_is_allowed(connection.remote_address):
            return http_response(403, b"Forbidden\n", "text/plain; charset=utf-8")
        if path == "/config":
            trusted_client = self.client_is_trusted(connection.remote_address)
            # Auto-login by Tailscale identity behind `tailscale serve`: when the
            # proxy identifies a mapped user, no token/username prompt is needed.
            auto_user = self.auto_auth_user(connection)
            auto = auto_user is not None
            payload = {
                "requireToken": self.token_required_for(trusted_client) and not auto,
                "tailscaleMode": self.tailscale_mode,
                "allowedClients": self.allowed_clients,
                "multiTenant": self.multi_tenant,
                "autoUser": (auto_user if self.multi_tenant else None) if auto else None,
                "label": self.label,
                "host": self.host,
                "port": self.port,
            }
            body = json.dumps(payload).encode("utf-8")
            return http_response(200, body, "application/json; charset=utf-8")
        if path == "/stats":
            body = json.dumps(self.usage_payload()).encode("utf-8")
            return http_response(200, body, "application/json; charset=utf-8")
        if path == "/manifest.webmanifest":
            body = json.dumps(self.manifest()).encode("utf-8")
            return http_response(200, body, "application/manifest+json; charset=utf-8")
        if path.startswith("/app-icon"):
            match = re.search(r"(\d{2,4})", path)
            size = min(1024, max(48, int(match.group(1)))) if match else 180
            try:
                return http_response(200, render_app_icon(self.label, size), "image/png")
            except Exception:
                return http_response(404, b"", "text/plain; charset=utf-8")
        # Serve index.html with the machine label injected so the home-screen
        # name + title match this deployment (icon carries the label too).
        if path in ("/", "/index.html"):
            try:
                html = (STATIC_ROOT / "index.html").read_text().replace("__MT_LABEL__", self.label)
                return http_response(200, html.encode("utf-8"), "text/html; charset=utf-8")
            except OSError:
                return http_response(404, b"Not Found", "text/plain; charset=utf-8")
        return await process_request(connection, request)

    def manifest(self) -> dict[str, Any]:
        return {
            "name": f"{self.label} terminal",
            "short_name": self.label,
            "display": "standalone",
            "background_color": "#0b121b",
            "theme_color": "#0b121b",
            "icons": [
                {"src": "/app-icon-192.png?v=2", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": "/app-icon-512.png?v=2", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": "/app-icon-512.png?v=2", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
            ],
        }

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

    async def send_tabs(
        self, connection: ServerConnection, user: str, session_name: str
    ) -> list[dict[str, Any]]:
        tabs = self.tabs_for_user(user, session_name)
        await self.send_json(connection, {"type": "tabs", "tabs": tabs})
        return tabs

    async def send_sessions(
        self, connection: ServerConnection, user: str, active_session: str
    ) -> list[dict[str, Any]]:
        sessions = self.sessions_for_user(user)
        await self.send_json(
            connection,
            {"type": "sessions", "sessions": sessions, "activeSession": active_session},
        )
        return sessions

    async def send_settings(self, connection: ServerConnection, user: str) -> dict[str, Any]:
        settings = self.settings_for(user)
        await self.send_json(
            connection,
            {
                "type": "settings",
                "settings": settings,
                "persisted": self.settings_persisted(user),
            },
        )
        return settings

    async def send_devices(self, connection: ServerConnection, user: str) -> None:
        devices: list[dict[str, Any]] = []
        if self.multi_tenant:
            for device_id, meta in load_user_state(user)["devices"].items():
                if not isinstance(meta, dict):
                    continue
                devices.append(
                    {
                        "id": device_id[:8],
                        "label": meta.get("label", ""),
                        "firstSeen": meta.get("firstSeen", ""),
                        "lastSeen": meta.get("lastSeen", ""),
                    }
                )
        devices.sort(key=lambda entry: entry.get("lastSeen", ""), reverse=True)
        await self.send_json(
            connection,
            {"type": "devices", "devices": devices, "multiTenant": self.multi_tenant},
        )

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
        user = state.get("user", "")
        message_type = payload.get("type")
        try:
            revision = int(payload.get("revision", 0))
        except (TypeError, ValueError):
            revision = 0
        if message_type in (
            "input",
            "composer-sync",
            "composer-enter",
            "composer-history",
            "composer-force-clear",
        ):
            # Auto-emitted terminal replies (mouse reports, DA/OSC/DCS query
            # responses) are not user intent; letting them settle the queue
            # would silently drop in-flight scrolling.
            if message_type != "input" or input_is_user_keystroke(str(payload.get("data", ""))):
                await self.settle_scroll_history(session_name)
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
            data = str(payload.get("data", ""))
            user_keystroke = input_is_user_keystroke(data)
            if user_keystroke and pane_in_mode(session_name):
                tmux_capture("send-keys", "-t", session_name, "-X", "cancel", check=False)
            bridge.write(payload.get("data", ""))
            if data and user_keystroke:
                self.reset_mobile_composer_tracking(session_name)
            return

        if message_type == "resize":
            cols = max(20, int(payload.get("cols", 80)))
            rows = max(6, int(payload.get("rows", 24)))
            self.terminal_sizes[session_name] = (cols, rows)
            bridge.resize(cols, rows)
            return

        if message_type == "scroll-history":
            lines = int(payload.get("lines", 0))
            self.queue_scroll_history(session_name, lines)
            return

        if message_type == "request-tabs":
            await self.send_tabs(connection, user, session_name)
            return

        if message_type == "request-sessions":
            await self.send_sessions(connection, user, session_name)
            return

        if message_type == "request-settings":
            await self.send_settings(connection, user)
            return

        if message_type == "request-devices":
            await self.send_devices(connection, user)
            return

        if message_type == "rotate-token":
            if not self.multi_tenant or user not in self.users:
                await self.send_json(
                    connection,
                    {"type": "notice", "message": "Token rotation is unavailable."},
                )
                return
            new_token = secrets.token_urlsafe(16)
            self.users[user]["token"] = new_token
            persist_users_config(self.users, self.owner)
            cleared = load_user_state(user)
            cleared["devices"] = {}
            save_user_state(user, cleared)
            await self.send_json(connection, {"type": "token-rotated", "token": new_token})
            return

        if message_type == "request-stats":
            await self.send_stats(connection)
            return

        if message_type == "save-settings":
            self.save_settings_for(user, payload.get("settings", {}))
            await self.send_settings(connection, user)
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
            next_name = self.create_user_session(user, current_path(session_name, self.cwd))
            await self.send_tabs(connection, user, session_name)
            await self.send_sessions(connection, user, session_name)
            await self.send_json(connection, {"type": "session-created", "session": next_name})
            return

        if message_type == "request-btop-targets":
            await self.send_json(
                connection,
                {"type": "btop-targets", "targets": btop_targets(self.btop_reachable)},
            )
            return

        if message_type == "new-btop-tab":
            target = str(payload.get("target", "")).strip()
            command = btop_command_for_target(target)
            if command is None:
                await self.send_json(
                    connection,
                    {"type": "notice", "message": f"Unknown btop target '{target}'."},
                )
                return
            # Namespace the btop session per user so tenants don't collide on or
            # reuse each other's monitor tabs. The "btop-" prefix is preserved so
            # the client still recognises it as a btop tab.
            btop_session = (
                f"{BTOP_SESSION_PREFIX}{sanitize_user(user)}{USER_SESSION_SEP}{target}"
                if self.multi_tenant
                else f"{BTOP_SESSION_PREFIX}{target}"
            )
            # Reuse an existing btop tab for this target instead of stacking
            # duplicates; otherwise spawn a dedicated session running btop.
            if not session_exists(btop_session):
                path = current_path(session_name, self.cwd)
                tmux_capture(
                    "new-session",
                    "-d",
                    "-s",
                    btop_session,
                    "-n",
                    "btop",
                    "-c",
                    path,
                    command,
                    check=False,
                )
                for option, value in (("status", "off"), ("mouse", "on")):
                    tmux_capture("set-option", "-t", btop_session, option, value, check=False)
            self.claim_session(user, btop_session, f"btop {target}")
            await self.send_tabs(connection, user, session_name)
            await self.send_sessions(connection, user, session_name)
            await self.send_json(
                connection,
                {
                    "type": "session-created",
                    "session": btop_session,
                    "kind": "btop",
                    "target": target,
                },
            )
            return

        if message_type == "rename-tab":
            name = str(payload.get("name", "")).strip()[:40]
            target_name = str(payload.get("session", session_name)).strip() or session_name
            if name and self.multi_tenant:
                # Per-user tabs keep a namespaced tmux name; renaming only updates
                # the stored display label, and only for a session the user owns.
                if not self.can_access(user, target_name):
                    await self.send_json(
                        connection,
                        {"type": "notice", "message": "You can only rename your own tabs."},
                    )
                    return
                tenant_state = load_user_state(user)
                meta = tenant_state["owned"].get(target_name)
                meta = meta if isinstance(meta, dict) else {}
                meta["label"] = name
                tenant_state["owned"][target_name] = meta
                save_user_state(user, tenant_state)
                await self.send_tabs(connection, user, state["session"])
                await self.send_sessions(connection, user, state["session"])
                return
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
                if target_name in self.terminal_sizes:
                    self.terminal_sizes[name] = self.terminal_sizes.pop(target_name)
                if target_name in self.scroll_states:
                    self.scroll_states[name] = self.scroll_states.pop(target_name)
                if target_name == session_name:
                    state["session"] = name
                await self.send_json(
                    connection,
                    {"type": "session-renamed", "oldSession": target_name, "session": name},
                )
                await self.send_tabs(connection, user, state["session"])
                await self.send_sessions(connection, user, state["session"])
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
            if not self.can_access(user, target_name):
                await self.send_json(
                    connection,
                    {"type": "notice", "message": "You can only close your own tabs."},
                )
                await self.send_tabs(connection, user, state["session"])
                await self.send_sessions(connection, user, state["session"])
                return
            sessions = list_sessions()
            if not any(session["name"] == target_name for session in sessions):
                await self.send_json(
                    connection,
                    {"type": "notice", "message": f"Session '{target_name}' is not running."},
                )
                await self.send_tabs(connection, user, state["session"])
                await self.send_sessions(connection, user, state["session"])
                return
            if target_name == session_name:
                visible = self.visible_session_names(user)
                remaining = [
                    session["name"]
                    for session in sessions
                    if session["name"] != target_name and session["name"] in visible
                ]
                fallback = remaining[0] if remaining else self.create_user_session(user)
                await self.send_json(
                    connection,
                    {
                        "type": "session-closing",
                        "closedSession": target_name,
                        "nextSession": fallback,
                    },
                )
            tmux_capture("kill-session", "-t", target_name, check=False)
            self.mobile_composer_states.pop(target_name, None)
            self.terminal_sizes.pop(target_name, None)
            self.scroll_states.pop(target_name, None)
            self.release_session(user, target_name)
            if target_name == session_name:
                await connection.close(code=1012, reason="session killed")
                return
            await self.send_tabs(connection, user, session_name)
            await self.send_sessions(connection, user, session_name)
            return

        if message_type == "detach-other-clients":
            target_name = str(payload.get("session", session_name)).strip() or session_name
            if not self.can_access(user, target_name):
                await self.send_json(
                    connection,
                    {"type": "notice", "message": "You can only manage your own tabs."},
                )
                return
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
            await self.send_tabs(connection, user, session_name)
            await self.send_sessions(connection, user, session_name)
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
        # Token-less auto-login by Tailscale identity behind `tailscale serve`.
        auto_user = self.auto_auth_user(connection)
        user = self.owner if self.multi_tenant else ""
        device_id = ""
        require_token_here = self.token_required_for(trusted_client) and auto_user is None
        # Read the client's auth frame when auth is needed, or (when the proxy
        # identity already authed us) just to capture the deviceId.
        if self.multi_tenant or require_token_here or auto_user is not None:
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
            if auth_payload.get("type") != "auth":
                await self.send_json(connection, {"type": "auth-error", "message": "Authentication required."})
                await connection.close(code=4001, reason="auth failed")
                return
            device_id = str(auth_payload.get("deviceId", ""))[:64]
            if auto_user is not None:
                # Authenticated by the Tailscale identity the serve proxy injected;
                # the token is not consulted, and the identity is authoritative for
                # the user (overrides any client-claimed username).
                user = auto_user if self.multi_tenant else ""
            elif self.multi_tenant:
                user = sanitize_user(auth_payload.get("user", ""))
                user_meta = self.users.get(user)
                # A device that has authenticated with the token once is
                # remembered (its deviceId is in the user's registry), so it can
                # reconnect without the token. The deviceId is a 122-bit random
                # secret held only by that browser, so it acts as a persistent
                # per-device credential (like a "remember this device" session).
                remembered = user_meta is not None and self.device_remembered(user, device_id)
                token_ok = user_meta is not None and (
                    not require_token_here
                    or remembered
                    or hmac.compare_digest(str(auth_payload.get("token", "")), user_meta["token"])
                )
                if not token_ok:
                    await self.send_json(connection, {"type": "auth-error", "message": "Invalid user or access token."})
                    await connection.close(code=4001, reason="auth failed")
                    return
            else:
                token_ok = not require_token_here or (
                    self.token is not None
                    and hmac.compare_digest(str(auth_payload.get("token", "")), self.token)
                )
                if not token_ok:
                    await self.send_json(connection, {"type": "auth-error", "message": "Invalid access token."})
                    await connection.close(code=4001, reason="auth failed")
                    return

        if self.multi_tenant:
            self.register_device(user, device_id, device_label(connection.request.headers.get("User-Agent", "")))

        requested_session = parse_qs(request_url.query).get("session", [""])[0].strip()
        skip_history = parse_qs(request_url.query).get("skip_history", [""])[0].strip().lower() in (
            "1",
            "true",
            "yes",
        )
        session_name, created = self.resolve_user_session(user, requested_session)
        create_if_missing = created
        requested_session_missing = bool(requested_session) and requested_session != session_name

        state = {"session": session_name, "user": user}
        bridge = TmuxBridge(
            session_name,
            self.shell,
            self.cwd,
            create_if_missing=create_if_missing,
            initial_size=self.terminal_sizes.get(session_name),
        )
        bridge.open()
        history = "" if skip_history else capture_history(state["session"], CONNECT_HISTORY_LINES)

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
            prev_local = None
            while True:
                tabs = self.tabs_for_user(state["user"], state["session"])
                snapshot = json.dumps(tabs, sort_keys=True)
                if snapshot != previous:
                    previous = snapshot
                    await self.send_json(connection, {"type": "tabs", "tabs": tabs})
                # Tell the client whether the active pane can scroll locally
                # (normal buffer) or must scroll via the server (alt-screen/mouse).
                local = pane_scrolls_locally(state["session"])
                if local != prev_local:
                    prev_local = local
                    await self.send_json(connection, {"type": "pane-scroll", "local": local})
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
                    "requireToken": self.token_required_for(trusted_client),
                    "tailscaleMode": self.tailscale_mode,
                    "allowedClients": self.allowed_clients,
                    "multiTenant": self.multi_tenant,
                    "user": user if self.multi_tenant else None,
                    "userLabel": self.users.get(user, {}).get("label") if self.multi_tenant else None,
                },
            )
            if history:
                await connection.send(history.encode("utf-8", "surrogateescape"))
            if requested_session_missing:
                await self.send_json(
                    connection,
                    {
                        "type": "notice",
                        "message": f"Session '{requested_session}' is not available. Attached to {session_name}.",
                    },
                )
            await self.send_tabs(connection, state["user"], state["session"])
            await self.send_sessions(connection, state["user"], state["session"])
            await self.send_settings(connection, state["user"])
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
            if self.multi_tenant:
                print(f"multi-tenant: {len(self.users)} users (owner: {self.owner})")
                print(f"users: {', '.join(sorted(self.users))}")
                print(f"access token: {'per-user' if self.require_token else 'disabled'}")
            elif self.require_token:
                print(f"access token: {self.token}")
            else:
                print("access token: disabled")
            print("")
            ping_task = asyncio.create_task(self.btop_ping_loop())
            try:
                await stop_event.wait()
            finally:
                ping_task.cancel()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mobile-friendly browser terminal for tmux.")
    parser.add_argument("--host", default=os.environ.get("MOBILE_TERMINAL_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("MOBILE_TERMINAL_PORT", "8085")))
    parser.add_argument("--session", default=os.environ.get("MOBILE_TERMINAL_SESSION", "mobile-terminal"))
    parser.add_argument(
        "--label",
        default=os.environ.get("MOBILE_TERMINAL_LABEL", ""),
        help="Short machine label shown in the add-to-home-screen icon (e.g. 'ph', 'lat').",
    )
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
    users_config = load_users_config()
    import socket

    label = args.label.strip() or socket.gethostname().split(".")[0][:12] or "term"
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
        users_config=users_config,
        label=label,
    )
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
