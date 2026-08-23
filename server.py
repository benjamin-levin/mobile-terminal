#!/usr/bin/env python3
import argparse
import asyncio
import base64
import datetime
import gzip
import hashlib
import hmac
import ipaddress
import json
import mimetypes
import os
import re
import secrets
import signal
import shlex
import subprocess
import time
import unicodedata
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qs, urlsplit

import regex
from wcwidth import wcwidth

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from mobile_terminal_config import (
    default_authentication_settings,
    normalize_authentication_realms,
    normalize_authentication_settings,
)
from provider_authority import provider_selection
from webauthn_auth import (
    PendingDeviceEnrollment,
    PasskeyAuth,
    PasskeyChallengeError,
    PasskeyStoreError,
    PasskeyVerificationError,
    device_authentication_transcript,
    valid_device_public_key,
    verify_device_key_signature,
)


ROOT = Path(__file__).resolve().parent
STATIC_ROOT = ROOT / "static"
NODE_MODULES_ROOT = ROOT / "node_modules"
WS_PATH = "/_ws"
SETTINGS_PATH = ROOT / "mobile-terminal-settings.json"
OPEN_TABS_PATH = Path(
    os.environ.get(
        "MOBILE_TERMINAL_OPEN_TABS_PATH",
        str(ROOT / "mobile-terminal-open-tabs.json"),
    )
).expanduser()
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
COMMAND_PROVENANCE_MAX_CHARS = 65536
COMMAND_PROVENANCE_RECORDS_PER_SESSION = 24
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


INTERNAL_TOKEN_ENV_NAME = "MOBILE_TERMINAL_INTERNAL_TOKEN"
INTERNAL_PROFILE_HEADER = "X-Mobile-Terminal-Profile"


def internal_token_environment_names() -> tuple[str, ...]:
    names = {INTERNAL_TOKEN_ENV_NAME}
    names.update(name for name in os.environ if name.startswith(f"{INTERNAL_TOKEN_ENV_NAME}_"))
    return tuple(sorted(names))


def terminal_child_environment() -> dict[str, str]:
    env = os.environ.copy()
    for name in internal_token_environment_names():
        env.pop(name, None)
    return env


def terminal_command(command: str) -> str:
    unset = " ".join(f"-u {shlex.quote(name)}" for name in internal_token_environment_names())
    return f"env {unset} {command}"


def tmux_capture(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", *args],
        cwd=ROOT,
        capture_output=True,
        check=check,
        text=True,
        env=terminal_child_environment(),
    )


def send_pane_bytes(target: str, data: bytes) -> None:
    if not data:
        return
    buffer_name = f"mobile-terminal-{os.getpid()}-{time.time_ns()}"
    loaded = subprocess.run(
        ["tmux", "load-buffer", "-b", buffer_name, "-"],
        cwd=ROOT,
        input=data,
        capture_output=True,
        check=False,
        env=terminal_child_environment(),
    )
    if loaded.returncode != 0:
        return
    tmux_capture(
        "paste-buffer",
        "-r",
        "-d",
        "-b",
        buffer_name,
        "-t",
        target,
        check=False,
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


def request_origin_matches_host(connection: ServerConnection) -> bool:
    origin = connection.request.headers.get("Origin")
    host = connection.request.headers.get("Host")
    if not origin or not host:
        return False
    try:
        parsed_origin = urlsplit(origin)
        parsed_host = urlsplit(f"//{host}")
        origin_port = parsed_origin.port or (443 if parsed_origin.scheme == "https" else 80)
        host_port = parsed_host.port or (443 if parsed_origin.scheme == "https" else 80)
    except ValueError:
        return False
    return bool(
        parsed_origin.scheme in ("http", "https")
        and parsed_origin.hostname
        and parsed_origin.hostname.lower() == (parsed_host.hostname or "").lower()
        and origin_port == host_port
        and not parsed_origin.username
        and not parsed_origin.password
        and parsed_origin.path in ("", "/")
        and not parsed_origin.query
        and not parsed_origin.fragment
    )


def ensure_session(session_name: str, shell: str, cwd: str) -> None:
    for name in internal_token_environment_names():
        tmux_capture("set-environment", "-g", "-u", name, check=False)
    has_session = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=terminal_child_environment(),
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
            terminal_command(f"{shell} -l"),
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
        env=terminal_child_environment(),
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


# Lines of authoritative scrollback sent in the initial seed. Older normal
# history is fetched by a larger reseed only when the local xterm reaches the
# top of its current window.
CONNECT_HISTORY_LINES = 2000
MAX_HISTORY_SEED_LINES = 20000
TMUX_INPUT_CHUNK_BYTES = 1024


@dataclass(frozen=True)
class CommandProvenance:
    session_name: str
    pane_id: str
    cols: int
    rows: int
    layout_generation: int
    start_row: int
    start_x: int
    draft: str
    revision: int
    source: str
    owner_id: int
    row_epoch: int = 0
    end_row: int | None = None
    end_x: int | None = None
    row_digest: str = ""
    terminal_revision: int | None = None


@dataclass(frozen=True)
class UnavailableCommandProvenance:
    session_name: str
    pane_id: str
    layout_generation: int
    draft: str
    revision: int
    owner_id: int


@dataclass(frozen=True)
class AcceptedCommand:
    session_name: str
    pane_id: str
    cols: int
    rows: int
    layout_generation: int
    start_row: int
    start_x: int
    end_row: int
    end_x: int
    draft: str
    revision: int
    source: str
    row_digest: str
    row_epoch: int = 0


@dataclass(frozen=True)
class SnapshotRowIdentity:
    epoch: int
    first_row: int


@dataclass
class PaneRowTracker:
    epoch: int = 0
    pane_id: str = ""
    history: int = -1
    history_limit: int = 0
    first_absolute_row: int = 0
    first_row: int = 0
    revision: int = -1
    plain_rows: tuple[str, ...] = ()

    def invalidate(self) -> None:
        self.epoch += 1
        self.pane_id = ""
        self.history = -1
        self.history_limit = 0
        self.revision = -1
        self.plain_rows = ()

    def observe(self, snapshot: "PaneSnapshot", revision: int) -> SnapshotRowIdentity:
        first_absolute_row = snapshot.history - snapshot.seed_history
        rows = tuple(snapshot.plain_physical_rows)
        if not self.pane_id or self.pane_id != snapshot.pane_id:
            if self.pane_id:
                self.epoch += 1
            first_row = 0
        elif snapshot.history < self.history:
            self.epoch += 1
            first_row = 0
        elif snapshot.history > self.history:
            first_row = self.first_row + first_absolute_row - self.first_absolute_row
        else:
            expected_delta = first_absolute_row - self.first_absolute_row
            delta = self._matching_delta(
                rows,
                expected_delta,
                revision,
                snapshot.history_limit,
            )
            if delta is None:
                self.epoch += 1
                first_row = 0
            else:
                first_row = self.first_row + delta
        self.pane_id = snapshot.pane_id
        self.history = snapshot.history
        self.history_limit = snapshot.history_limit
        self.first_absolute_row = first_absolute_row
        self.first_row = first_row
        self.revision = revision
        self.plain_rows = rows
        return SnapshotRowIdentity(self.epoch, first_row)

    def _matching_delta(
        self,
        rows: tuple[str, ...],
        expected_delta: int,
        revision: int,
        history_limit: int,
    ) -> int | None:
        if revision == self.revision:
            return expected_delta
        if rows == self.plain_rows:
            return None
        if (
            self.history_limit > 0
            and history_limit > 0
            and self.history < self.history_limit
            and self.history < history_limit
        ):
            return expected_delta
        if not rows or not self.plain_rows:
            return None

        prefix = [0] * len(rows)
        for index in range(1, len(rows)):
            matched = prefix[index - 1]
            while matched and rows[index] != rows[matched]:
                matched = prefix[matched - 1]
            if rows[index] == rows[matched]:
                matched += 1
            prefix[index] = matched

        matched = 0
        for row in self.plain_rows:
            while matched and (matched == len(rows) or row != rows[matched]):
                matched = prefix[matched - 1]
            if row == rows[matched]:
                matched += 1
        deltas = []
        while matched:
            delta = len(self.plain_rows) - matched
            if delta >= expected_delta:
                deltas.append(delta)
            matched = prefix[matched - 1]
        if len(deltas) != 1:
            return None
        return deltas[0]


@dataclass
class CommandProvenanceState:
    layout_generation: int = 0
    tracking_generation: int = 0
    row_tracker: PaneRowTracker = field(default_factory=PaneRowTracker)
    active: CommandProvenance | None = None
    unavailable: UnavailableCommandProvenance | None = None
    accepted_records: deque[AcceptedCommand] = field(
        default_factory=lambda: deque(maxlen=COMMAND_PROVENANCE_RECORDS_PER_SESSION)
    )
    owner_pids: set[int] = field(default_factory=set)
    fence_task: asyncio.Task[None] | None = None

    def cancel_fence(self) -> None:
        task = self.fence_task
        self.fence_task = None
        if task is not None and not task.done():
            task.cancel()

    def mark_unavailable(self, unavailable: UnavailableCommandProvenance) -> None:
        self.cancel_fence()
        self.active = None
        self.unavailable = unavailable

    def invalidate_active(self) -> None:
        self.cancel_fence()
        self.tracking_generation += 1
        self.active = None
        self.unavailable = None

    def invalidate_layout(self) -> None:
        self.layout_generation += 1
        self.row_tracker.invalidate()
        self.invalidate_active()

    def remember(self, record: AcceptedCommand) -> None:
        self.accepted_records.append(record)

    def accepted(self, pane_id: str) -> tuple[AcceptedCommand, ...]:
        return tuple(record for record in self.accepted_records if record.pane_id == pane_id)


@dataclass(frozen=True)
class PaneSnapshot:
    pane_id: str
    history: int
    seed_history: int
    cols: int
    rows: int
    alternate: bool
    cursor_x: int
    cursor_y: int
    cursor_flag: bool
    cursor_blinking: bool
    cursor_shape: str
    insert: bool
    keypad_cursor: bool
    keypad: bool
    origin: bool
    wrap: bool
    mouse_standard: bool
    mouse_button: bool
    mouse_any: bool
    mouse_sgr: bool
    scroll_upper: int
    scroll_lower: int
    tab_stops: tuple[int, ...]
    physical_rows: list[str]
    plain_physical_rows: list[str]
    authored_lines: list[str]
    history_limit: int = 0

    def metadata(self) -> dict[str, Any]:
        return {
            "paneId": self.pane_id,
            "history": self.history,
            "historyLimit": self.history_limit,
            "seedHistory": self.seed_history,
            "cols": self.cols,
            "rows": self.rows,
            "alternate": self.alternate,
            "cursorX": self.cursor_x,
            "cursorY": self.cursor_y,
            "cursorFlag": self.cursor_flag,
            "cursorBlinking": self.cursor_blinking,
            "cursorShape": self.cursor_shape,
            "insert": self.insert,
            "keypadCursor": self.keypad_cursor,
            "keypad": self.keypad,
            "origin": self.origin,
            "wrap": self.wrap,
            "mouseStandard": self.mouse_standard,
            "mouseButton": self.mouse_button,
            "mouseAny": self.mouse_any,
            "mouseSgr": self.mouse_sgr,
            "scrollUpper": self.scroll_upper,
            "scrollLower": self.scroll_lower,
            "tabStops": list(self.tab_stops),
        }


def _capture_lines(output: str) -> list[str]:
    if not output.endswith("\n"):
        raise RuntimeError("tmux capture-pane returned an unframed snapshot")
    return output[:-1].split("\n")


def pane_metadata(session_name: str, pane_id: str | None = None) -> tuple[Any, ...]:
    target = pane_id or session_name
    result = tmux_capture(
        "display-message",
        "-p",
        "-t",
        target,
        "#{pane_id}\t#{history_size}\t#{pane_width}\t#{pane_height}\t#{alternate_on}"
        "\t#{cursor_x}\t#{cursor_y}\t#{cursor_flag}\t#{cursor_blinking}\t#{cursor_shape}"
        "\t#{insert_flag}\t#{keypad_cursor_flag}\t#{keypad_flag}\t#{origin_flag}\t#{wrap_flag}"
        "\t#{mouse_standard_flag}\t#{mouse_button_flag}\t#{mouse_any_flag}\t#{mouse_sgr_flag}"
        "\t#{scroll_region_upper}\t#{scroll_region_lower}\t#{pane_tabs}\t#{history_limit}",
        check=False,
    )
    fields = result.stdout.rstrip("\n").split("\t")
    if result.returncode != 0 or len(fields) != 23 or not fields[0].startswith("%"):
        raise RuntimeError("active tmux pane is unavailable")
    try:
        numeric = {
            index: int(fields[index] or 0)
            for index in (*range(1, 9), *range(10, 21), 22)
        }
        tab_stops = tuple(int(value) for value in fields[21].split(",") if value)
    except ValueError as exc:
        raise RuntimeError("tmux returned invalid pane metadata") from exc
    return (
        fields[0],
        *(numeric[index] for index in range(1, 9)),
        fields[9],
        *(numeric[index] for index in range(10, 21)),
        tab_stops,
        numeric[22],
    )


def capture_pane_snapshot(
    session_name: str,
    pane_id: str | None = None,
    history_lines: int = CONNECT_HISTORY_LINES,
) -> PaneSnapshot:
    before = pane_metadata(session_name, pane_id)
    (
        active_pane,
        history,
        cols,
        rows,
        alternate,
        cursor_x,
        cursor_y,
        cursor_flag,
        cursor_blinking,
        cursor_shape,
        insert,
        keypad_cursor,
        keypad,
        origin,
        wrap,
        mouse_standard,
        mouse_button,
        mouse_any,
        mouse_sgr,
        scroll_upper,
        scroll_lower,
        tab_stops,
        history_limit,
    ) = before
    seed_history = 0 if alternate else min(history, max(0, history_lines))
    start = str(-seed_history)
    end = str(rows - 1)
    styled_physical = tmux_capture(
        "capture-pane", "-p", "-e", "-N", "-S", start, "-E", end, "-t", active_pane, check=False
    )
    plain_physical = tmux_capture(
        "capture-pane", "-p", "-N", "-S", start, "-E", end, "-t", active_pane, check=False
    )
    authored = tmux_capture(
        "capture-pane", "-p", "-J", "-S", start, "-E", end, "-t", active_pane, check=False
    )
    after = pane_metadata(session_name, active_pane)
    if (
        styled_physical.returncode != 0
        or plain_physical.returncode != 0
        or authored.returncode != 0
        or before != after
    ):
        raise RuntimeError("tmux pane changed during capture")
    physical_rows = _capture_lines(styled_physical.stdout)
    plain_physical_rows = _capture_lines(plain_physical.stdout)
    authored_lines = _capture_lines(authored.stdout)
    if len(physical_rows) != len(plain_physical_rows):
        raise RuntimeError("tmux physical-row captures have inconsistent geometry")
    if len(physical_rows) != seed_history + rows:
        raise RuntimeError("tmux physical-row capture has inconsistent geometry")
    return PaneSnapshot(
        pane_id=active_pane,
        history=history,
        seed_history=seed_history,
        cols=cols,
        rows=rows,
        alternate=bool(alternate),
        cursor_x=cursor_x,
        cursor_y=cursor_y,
        cursor_flag=bool(cursor_flag),
        cursor_blinking=bool(cursor_blinking),
        cursor_shape=cursor_shape,
        insert=bool(insert),
        keypad_cursor=bool(keypad_cursor),
        keypad=bool(keypad),
        origin=bool(origin),
        wrap=bool(wrap),
        mouse_standard=bool(mouse_standard),
        mouse_button=bool(mouse_button),
        mouse_any=bool(mouse_any),
        mouse_sgr=bool(mouse_sgr),
        scroll_upper=scroll_upper,
        scroll_lower=scroll_lower,
        tab_stops=tab_stops,
        physical_rows=physical_rows,
        plain_physical_rows=plain_physical_rows,
        authored_lines=authored_lines,
        history_limit=history_limit,
    )


def _command_row_digest(
    snapshot: PaneSnapshot,
    start_row: int,
    end_row: int,
) -> str:
    first_absolute_row = snapshot.history - snapshot.seed_history
    first_index = start_row - first_absolute_row
    last_index = end_row - first_absolute_row
    if (
        start_row > end_row
        or first_index < 0
        or last_index < first_index
        or last_index >= len(snapshot.plain_physical_rows)
    ):
        raise RuntimeError("command rows are outside the authoritative snapshot")
    digest = hashlib.sha256()
    for row in snapshot.plain_physical_rows[first_index : last_index + 1]:
        encoded = row.encode("utf-8", "surrogateescape")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _identity_row(
    snapshot: PaneSnapshot,
    identity: SnapshotRowIdentity,
    absolute_row: int,
) -> int:
    return identity.first_row + absolute_row - (snapshot.history - snapshot.seed_history)


def _absolute_row(
    snapshot: PaneSnapshot,
    identity: SnapshotRowIdentity,
    identity_row: int,
) -> int:
    return snapshot.history - snapshot.seed_history + identity_row - identity.first_row


def _coordinate_intersects(
    selection_start: tuple[int, int],
    selection_end: tuple[int, int],
    command_start: tuple[int, int],
    command_end: tuple[int, int],
) -> bool:
    return selection_start < command_end and command_start < selection_end


def exact_provenance_selection(
    snapshot: PaneSnapshot,
    session_name: str,
    layout_generation: int,
    selection_start: tuple[int, int],
    selection_end: tuple[int, int],
    active: CommandProvenance | None,
    accepted: tuple[AcceptedCommand, ...],
    identity: SnapshotRowIdentity | None = None,
    selected_text: str | None = None,
    terminal_revision: int | None = None,
) -> tuple[bool, str | None]:
    del selected_text
    if identity is None:
        identity = SnapshotRowIdentity(0, snapshot.history - snapshot.seed_history)
    cursor_end = (
        _identity_row(snapshot, identity, snapshot.history + snapshot.cursor_y),
        snapshot.cursor_x,
    )
    candidates: list[tuple[Any, tuple[int, int], tuple[int, int]]] = []
    stale_intersection = False
    if active is not None and active.pane_id == snapshot.pane_id:
        active_start = (active.start_row, active.start_x)
        active_end = (
            (active.end_row, active.end_x)
            if active.end_row is not None and active.end_x is not None
            else cursor_end
        )
        if _coordinate_intersects(selection_start, selection_end, active_start, active_end):
            try:
                absolute_start = _absolute_row(snapshot, identity, active.start_row)
                absolute_end = _absolute_row(snapshot, identity, active_end[0])
                valid_active = bool(
                    active.row_epoch == identity.epoch
                    and active.session_name == session_name
                    and active.cols == snapshot.cols
                    and active.rows == snapshot.rows
                    and active.layout_generation == layout_generation
                    and active.source == "composer-sync"
                    and len(active.draft) <= COMMAND_PROVENANCE_MAX_CHARS
                    and active.end_row is not None
                    and active.end_x is not None
                    and active.row_digest
                    and (
                        terminal_revision is None
                        or active.terminal_revision == terminal_revision
                    )
                    and active_end == cursor_end
                    and _command_row_digest(snapshot, absolute_start, absolute_end)
                    == active.row_digest
                )
            except RuntimeError:
                valid_active = False
            if valid_active:
                candidates.append((active, active_start, active_end))
            else:
                stale_intersection = True
    for record in accepted:
        record_start = (record.start_row, record.start_x)
        record_end = (record.end_row, record.end_x)
        if record.row_epoch != identity.epoch:
            if _coordinate_intersects(selection_start, selection_end, record_start, record_end):
                stale_intersection = True
            continue
        if not _coordinate_intersects(selection_start, selection_end, record_start, record_end):
            continue
        try:
            absolute_start = _absolute_row(snapshot, identity, record.start_row)
            absolute_end = _absolute_row(snapshot, identity, record.end_row)
            valid_record = bool(
                record.session_name == session_name
                and record.pane_id == snapshot.pane_id
                and record.cols == snapshot.cols
                and record.rows == snapshot.rows
                and record.layout_generation == layout_generation
                and record.source == "composer-sync"
                and _command_row_digest(snapshot, absolute_start, absolute_end)
                == record.row_digest
            )
        except RuntimeError:
            valid_record = False
        if valid_record:
            candidates.append((record, record_start, record_end))
        else:
            stale_intersection = True

    if not candidates:
        if stale_intersection:
            raise RuntimeError("visible command does not match tracked composer state")
        return False, None
    if len(candidates) != 1:
        raise RuntimeError("selection intersects ambiguous command provenance")

    record, command_start, command_end = candidates[0]
    if selection_start != command_start or selection_end != command_end:
        raise RuntimeError("selection only partially covers command provenance")
    return True, record.draft


GRAPHEME_RE = regex.compile(r"\X")
REGIONAL_INDICATOR_RE = regex.compile(r"\A\p{Regional_Indicator}\Z")
EXTENDED_PICTOGRAPHIC_RE = regex.compile(r"\p{Extended_Pictographic}")
EMOJI_MODIFIER_RE = regex.compile(r"\p{Emoji_Modifier}")

# These are conservative tmux/xterm agreement classes. wcwidth is used only
# to validate the base and combining marks inside an otherwise allowed class.


def _is_verified_cjk_base(character: str) -> bool:
    value = ord(character)
    return (
        0x3000 <= value <= 0x30FF
        or 0x3400 <= value <= 0x4DBF
        or 0x4E00 <= value <= 0x9FFF
        or 0xAC00 <= value <= 0xD7A3
        or 0xF900 <= value <= 0xFAFF
        or 0xFF01 <= value <= 0xFF60
        or 0xFFE0 <= value <= 0xFFE6
    )


def _verified_grapheme_width(grapheme: str) -> int:
    if grapheme == "♥︎":
        return 1
    if all(REGIONAL_INDICATOR_RE.fullmatch(character) for character in grapheme):
        if len(grapheme) == 2:
            return 2
        raise RuntimeError("terminal grapheme geometry is not renderer-proven")
    if any(REGIONAL_INDICATOR_RE.fullmatch(character) for character in grapheme):
        raise RuntimeError("terminal grapheme geometry is not renderer-proven")
    if (
        "‍" in grapheme
        or "️" in grapheme
        or "⃣" in grapheme
        or any(0xFE00 <= ord(character) <= 0xFE0F for character in grapheme)
        or any(0xE0100 <= ord(character) <= 0xE01EF for character in grapheme)
        or EXTENDED_PICTOGRAPHIC_RE.search(grapheme)
        or EMOJI_MODIFIER_RE.search(grapheme)
    ):
        raise RuntimeError("terminal grapheme geometry is not renderer-proven")

    widths = [wcwidth(character) for character in grapheme]
    base = grapheme[0]
    if widths[0] <= 0 or unicodedata.category(base)[0] in "CM":
        raise RuntimeError("terminal grapheme geometry is not renderer-proven")
    if any(
        width != 0 or unicodedata.category(character)[0] != "M"
        for character, width in zip(grapheme[1:], widths[1:])
    ):
        raise RuntimeError("terminal grapheme geometry is not renderer-proven")

    value = ord(base)
    if 0x20 <= value <= 0x7E:
        return 1
    east_asian_width = unicodedata.east_asian_width(base)
    if east_asian_width == "A":
        raise RuntimeError("terminal grapheme geometry is not renderer-proven")
    if east_asian_width in ("W", "F") and _is_verified_cjk_base(base):
        return 2
    if east_asian_width in ("N", "Na", "H") and unicodedata.category(base)[0] in "LNP":
        return 1
    raise RuntimeError("terminal grapheme geometry is not renderer-proven")


def _display_token_width(
    value: str,
    column: int,
    tab_stops: tuple[int, ...] | None,
    cols: int | None,
) -> int:
    if value != "\t":
        return _verified_grapheme_width(value)
    if tab_stops is None:
        destination = ((column // 8) + 1) * 8
    else:
        destination = next((stop for stop in tab_stops if stop > column), cols - 1 if cols else column)
    if cols is not None:
        destination = min(destination, cols - 1)
    return max(0, destination - column)


def _display_tokens(
    text: str,
    tab_stops: tuple[int, ...] | None = None,
    cols: int | None = None,
) -> tuple[list[tuple[str, int, int]], int]:
    tokens: list[tuple[str, int, int]] = []
    column = 0
    for value in GRAPHEME_RE.findall(text):
        width = _display_token_width(value, column, tab_stops, cols)
        if width:
            tokens.append((value, column, column + width))
            column += width
    return tokens, column


def _authored_physical_map(snapshot: PaneSnapshot) -> list[tuple[int, str]]:
    if len(snapshot.physical_rows) != len(snapshot.plain_physical_rows):
        raise RuntimeError("tmux physical-row captures have inconsistent geometry")
    mapping: list[tuple[int, str]] = []
    physical_index = 0
    for logical_index, line in enumerate(snapshot.authored_lines):
        remainder = line
        consumed_row = False
        while remainder or not consumed_row:
            if physical_index >= len(snapshot.plain_physical_rows):
                raise RuntimeError("tmux authored text does not map to its physical geometry")
            physical_row = snapshot.plain_physical_rows[physical_index]
            prefix_length = min(len(remainder), len(physical_row))
            while prefix_length >= 0:
                if (
                    physical_row[:prefix_length] == remainder[:prefix_length]
                    and not physical_row[prefix_length:].strip(" ")
                ):
                    break
                prefix_length -= 1
            if prefix_length < 0:
                raise RuntimeError("tmux authored text does not map to its physical geometry")
            segment = remainder[:prefix_length]
            mapping.append((logical_index, segment))
            remainder = remainder[prefix_length:]
            physical_index += 1
            consumed_row = True
    if physical_index != len(snapshot.plain_physical_rows):
        raise RuntimeError("tmux authored text does not map to its physical geometry")
    return mapping


def _slice_display_cells(
    text: str,
    start: int,
    end: int,
    tab_stops: tuple[int, ...] | None = None,
    cols: int | None = None,
) -> str:
    if end <= start:
        return ""
    selected: list[str] = []
    column = 0
    for value in GRAPHEME_RE.findall(text):
        if column >= end:
            break
        width = _display_token_width(value, column, tab_stops, cols)
        token_start = column
        token_end = column + width
        column = token_end
        overlap_start = max(start, token_start)
        overlap_end = min(end, token_end)
        if overlap_end <= overlap_start:
            continue
        if value == "\t" and (overlap_start != token_start or overlap_end != token_end):
            selected.append(" " * (overlap_end - overlap_start))
        else:
            selected.append(value)
    return "".join(selected)


def extract_authoritative_selection(
    snapshot: PaneSnapshot,
    start_x: int,
    start_row: int,
    end_x: int,
    end_row: int,
) -> str:
    minimum_row = -snapshot.seed_history
    maximum_row = snapshot.rows - 1
    if (
        start_row < minimum_row
        or start_row > maximum_row
        or end_row < minimum_row
        or end_row > maximum_row
        or (end_row, end_x) < (start_row, start_x)
        or not 0 <= start_x <= snapshot.cols
        or not 0 <= end_x <= snapshot.cols
    ):
        raise ValueError("selection coordinates are outside the authoritative snapshot")

    mapping = _authored_physical_map(snapshot)
    first_index = start_row + snapshot.seed_history
    last_index = end_row + snapshot.seed_history
    pieces: list[str] = []
    for physical_index in range(first_index, last_index + 1):
        logical_index, row_segment = mapping[physical_index]
        row_start = start_x if physical_index == first_index else 0
        row_end = end_x if physical_index == last_index else snapshot.cols
        pieces.append(
            _slice_display_cells(
                row_segment,
                row_start,
                row_end,
                snapshot.tab_stops,
                snapshot.cols,
            )
        )
        if physical_index < last_index and mapping[physical_index + 1][0] != logical_index:
            pieces.append("\n")
    return "".join(pieces)


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
    viewport, so those still scroll via exact wheel or application-arrow input."""
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


def scroll_session_history(pane_id: str, lines: int) -> None:
    count = min(abs(int(lines)), MAX_SCROLL_EVENTS_PER_CALL)
    if count == 0:
        return
    result = tmux_capture(
        "display-message",
        "-p",
        "-t",
        pane_id,
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

    if mouse_tracking:
        # Mouse-enabled TUIs own their transcript. Route exactly the SGR wheel
        # report a terminal would send; never enter tmux copy mode.
        x = max(1, pane_width // 2)
        y = max(1, pane_height // 2)
        button = 64 if lines > 0 else 65
        sequence = f"\x1b[<{button};{x};{y}M" * count
        send_pane_bytes(pane_id, sequence.encode("utf-8"))
        return

    if alternate_on:
        # Alternate-screen applications without mouse tracking receive
        # application arrows. Their history is not a tmux normal-history view.
        key = "Up" if lines > 0 else "Down"
        tmux_capture("send-keys", "-t", pane_id, "-N", str(count), key, check=False)


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


def sessions_by_recent_activity() -> list[str]:
    """Existing tmux session names ordered most-recently-active first."""
    output = tmux_capture(
        "list-sessions",
        "-F",
        "#{session_activity}\t#{session_name}",
        check=False,
    )
    if output.returncode != 0:
        return []
    rows: list[tuple[int, str]] = []
    for line in output.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        activity, name = parts
        try:
            rows.append((int(activity), name))
        except ValueError:
            continue
    rows.sort(reverse=True)
    return [name for _, name in rows]


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
        "authentication": default_authentication_settings(),
        "authenticationByRealm": {},
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
        "authentication": normalize_authentication_settings(raw_settings.get("authentication")),
        "authenticationByRealm": normalize_authentication_realms(
            raw_settings.get("authenticationByRealm")
        ),
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
        "provenanceStartAllowed": True,
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


def load_open_tabs_global() -> list[str]:
    """Single-tenant persisted open-tab set (shared across all devices)."""
    if not OPEN_TABS_PATH.is_file():
        return []
    try:
        raw = json.loads(OPEN_TABS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(raw, list):
        return []
    return [name for name in raw if isinstance(name, str) and name]


def save_open_tabs_global(names: list[str]) -> None:
    OPEN_TABS_PATH.write_text(json.dumps(names, indent=2) + "\n")


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
        USERS_PATH.chmod(0o600)
    except OSError:
        pass
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
    temporary = USERS_PATH.with_name(f".{USERS_PATH.name}.{os.getpid()}.{time.time_ns()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(USERS_PATH)


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
    """Per-user owned-session map, device registry and open-tab set:
    {owned:{name:{label}}, devices:{id:{...}}, openTabs:[name]}."""
    state: dict[str, Any] = {"owned": {}, "devices": {}, "openTabs": []}
    path = user_state_path(user)
    if path.is_file():
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):
                if isinstance(raw.get("owned"), dict):
                    state["owned"] = raw["owned"]
                if isinstance(raw.get("devices"), dict):
                    state["devices"] = raw["devices"]
                if isinstance(raw.get("openTabs"), list):
                    state["openTabs"] = [name for name in raw["openTabs"] if isinstance(name, str) and name]
        except (OSError, json.JSONDecodeError):
            pass
    return state


def save_user_state(user: str, state: dict[str, Any]) -> None:
    path = user_state_path(user)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n")


# --- device-bound key auth ------------------------------------------------
# Each enrolled browser holds a non-extractable ECDSA P-256 private key (in
# IndexedDB) and registers only its PUBLIC key here. To connect it signs a fresh
# server nonce; we verify with the stored public key. Unlike the old deviceId
# "remember-me" string this is not a transferable secret: the private key can't
# be exported by JS and a captured signature is bound to a spent one-time nonce.
DEVICE_KEYS_PATH = ROOT / "state" / "device-keys.json"
PASSKEY_STATE_DIR = ROOT / "state" / "passkeys"
DEVICE_NONCE_TTL = 120  # seconds a signing challenge stays valid


def load_device_keys() -> dict[str, Any]:
    """{user: {deviceId: {pubKey(spki b64), label, created, lastSeen}}}.
    Single-tenant uses the "" user key."""
    if DEVICE_KEYS_PATH.is_file():
        try:
            raw = json.loads(DEVICE_KEYS_PATH.read_text())
            if isinstance(raw, dict):
                return raw
        except (OSError, json.JSONDecodeError):
            pass
    return {}


def save_device_keys(store: dict[str, Any]) -> None:
    DEVICE_KEYS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DEVICE_KEYS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(store, indent=2) + "\n")
    tmp.replace(DEVICE_KEYS_PATH)


def device_pubkey(user: str, device_id: str) -> str | None:
    if not device_id:
        return None
    rec = load_device_keys().get(user or "", {}).get(device_id)
    return rec.get("pubKey") if isinstance(rec, dict) else None


def register_device_key(
    user: str, device_id: str, pub_key_spki_b64: str, label: str
) -> None:
    store = load_device_keys()
    bucket = store.setdefault(user or "", {})
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    existing = bucket.get(device_id) if isinstance(bucket.get(device_id), dict) else {}
    bucket[device_id] = {
        "pubKey": pub_key_spki_b64,
        "label": label or existing.get("label", "device"),
        "created": existing.get("created", now),
        "lastSeen": now,
    }
    save_device_keys(store)


def forget_device_key(user: str, device_id: str) -> None:
    store = load_device_keys()
    bucket = store.get(user or "")
    if isinstance(bucket, dict) and device_id in bucket:
        del bucket[device_id]
        save_device_keys(store)


def verify_device_signature(pub_key_spki_b64: str, message: bytes, signature_b64: str) -> bool:
    return verify_device_key_signature(pub_key_spki_b64, message, signature_b64)


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
# Assembled (body, gzipped_body, etag) of index.html with CSS/JS inlined, per
# label. Built once per process; a deploy restarts the server and rebuilds it.
_INLINED_INDEX_CACHE: dict[str, tuple[bytes, bytes, str]] = {}
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
        connection: ServerConnection,
        session_name: str,
        shell: str,
        cwd: str,
        create_if_missing: bool = True,
        initial_size: tuple[int, int] | None = None,
        epoch_state: dict[str, int] | None = None,
        profile_id: str = "",
        provenance_state: CommandProvenanceState | None = None,
        write_lock: asyncio.Lock | None = None,
    ) -> None:
        self.connection = connection
        self.session_name = session_name
        self.shell = shell
        self.cwd = cwd
        self.create_if_missing = create_if_missing
        self.initial_size = initial_size
        self.profile_id = profile_id
        self.epoch_state = epoch_state if epoch_state is not None else {"epoch": 0, "layout": 0}
        self.provenance_state = provenance_state or CommandProvenanceState()
        self.process: subprocess.Popen[bytes] | None = None
        self.pane_id = ""
        self.offset = 0
        self.bytes_out = 0
        self.cutoff = 0
        self.seed_history = 0
        self.phase = "hold"
        self.held: list[dict[str, Any]] = []
        self.last_output_at = time.monotonic()
        self.line_buffer = b""
        self.read_task: asyncio.Task[Any] | None = None
        self.send_lock = asyncio.Lock()
        self.seed_lock = asyncio.Lock()
        self.write_lock = write_lock or asyncio.Lock()
        self.closing = False
        self.closed = False
        self.command_waiters: deque[asyncio.Future[list[bytes]]] = deque()
        self.command_block: tuple[asyncio.Future[list[bytes]] | None, list[bytes]] | None = None
        self.initial_block_seen = asyncio.Event()
        self.pane_change = asyncio.Event()
        self.seed_start_acks: dict[int, tuple[asyncio.Event, dict[str, Any]]] = {}
        self.seed_acks: dict[int, asyncio.Event] = {}
        self.flush_acks: dict[tuple[int, int], asyncio.Event] = {}
        self.selection_acks: dict[str, tuple[asyncio.Event, dict[str, Any]]] = {}

    @staticmethod
    def unescape_control(value: bytes) -> bytes:
        output = bytearray()
        index = 0
        while index < len(value):
            if (
                value[index] == 0x5C
                and index + 3 < len(value)
                and all(0x30 <= character <= 0x37 for character in value[index + 1 : index + 4])
            ):
                output.append(int(value[index + 1 : index + 4], 8))
                index += 4
            else:
                output.append(value[index])
                index += 1
        return bytes(output)

    async def open(self) -> None:
        try:
            if self.create_if_missing:
                ensure_session(self.session_name, self.shell, self.cwd)
            env = terminal_child_environment()
            env["TERM"] = "xterm-256color"
            env["COLORTERM"] = "truecolor"
            self.process = subprocess.Popen(
                ["tmux", "-C", "attach-session", "-t", self.session_name],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                cwd=self.cwd,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
            self.provenance_state.owner_pids.add(self.process.pid)
            assert self.process.stdout is not None
            os.set_blocking(self.process.stdout.fileno(), False)
            self.read_task = asyncio.create_task(self.read_loop())
            await asyncio.wait_for(self.initial_block_seen.wait(), timeout=3)
            self.pane_id = str(pane_metadata(self.session_name)[0])
            cols, rows = self.initial_size or (140, 40)
            await self.set_size(cols, rows)
            self.last_output_at = time.monotonic()
            await self.quiet(0.1)
        except BaseException:
            await self.close()
            raise

    async def command(self, command: str) -> list[bytes]:
        if not self.process or self.process.poll() is not None or self.process.stdin is None:
            raise RuntimeError("tmux control client is closed")
        future: asyncio.Future[list[bytes]] = asyncio.get_running_loop().create_future()
        self.command_waiters.append(future)
        self.process.stdin.write(command.encode("utf-8") + b"\n")
        self.process.stdin.flush()
        return await asyncio.wait_for(future, timeout=3)

    async def _set_size_locked(self, cols: int, rows: int) -> None:
        try:
            metadata = await asyncio.to_thread(pane_metadata, self.session_name, self.pane_id)
            dimensions_changed = metadata[2:4] != (cols, rows)
        except RuntimeError:
            dimensions_changed = True
        if dimensions_changed:
            self.provenance_state.invalidate_layout()
        await self.command(f"refresh-client -C {cols},{rows}")
        await asyncio.to_thread(
            tmux_capture,
            "resize-window",
            "-t",
            self.pane_id,
            "-x",
            str(cols),
            "-y",
            str(rows),
            check=False,
        )

    async def set_size(self, cols: int, rows: int) -> None:
        async with self.write_lock:
            await self._set_size_locked(cols, rows)

    async def resize(self, cols: int, rows: int) -> None:
        async def mutate() -> None:
            await self.set_size(cols, rows)

        await self.reseed("resize", mutate=mutate)

    async def _send_json(self, payload: dict[str, Any]) -> None:
        await self.connection.send(json.dumps(payload, ensure_ascii=False))

    async def _send_output(self, record: dict[str, Any], kind: str) -> None:
        await self._send_json(
            {
                "type": "terminal-output",
                "paneId": self.pane_id,
                "epoch": self.epoch_state["epoch"],
                "start": record["start"],
                "end": record["end"],
                "kind": kind,
            }
        )
        await self.connection.send(record["data"])
        self.bytes_out += record["end"] - record["start"]

    async def pane_bytes(self, pane_id: str, data: bytes) -> None:
        if not data or pane_id != self.pane_id:
            return
        async with self.send_lock:
            record = {"start": self.offset, "end": self.offset + len(data), "data": data}
            self.offset = record["end"]
            self.last_output_at = time.monotonic()
            if self.phase == "hold":
                self.held.append(record)
            else:
                await self._send_output(record, "live")

    def _finish_command_block(self, failed: bool) -> None:
        if self.command_block is None:
            self.initial_block_seen.set()
            return
        future, output = self.command_block
        self.command_block = None
        if future is None or future.done():
            self.initial_block_seen.set()
        elif failed:
            future.set_exception(RuntimeError(b"\n".join(output).decode("utf-8", "replace")))
        else:
            future.set_result(output)

    async def control_line(self, line: bytes) -> None:
        if self.command_block is not None:
            if line.startswith(b"%end "):
                self._finish_command_block(False)
            elif line.startswith(b"%error "):
                self._finish_command_block(True)
            else:
                self.command_block[1].append(line)
            return
        if line.startswith(b"%begin "):
            future = self.command_waiters.popleft() if self.command_waiters else None
            self.command_block = (future, [])
            return
        if line.startswith(b"%output "):
            parts = line.split(b" ", 2)
            if len(parts) == 3:
                await self.pane_bytes(parts[1].decode("ascii", "ignore"), self.unescape_control(parts[2]))
            return
        if line.startswith(b"%extended-output "):
            prefix, separator, value = line.partition(b" : ")
            parts = prefix.split()
            if separator and len(parts) >= 3:
                await self.pane_bytes(parts[1].decode("ascii", "ignore"), self.unescape_control(value))
            return
        if line.startswith(b"%client-session-changed "):
            parts = line.split(b" ", 2)
            client_name = parts[1].decode("ascii", "ignore") if len(parts) > 1 else ""
            client_pid = -1
            if client_name.startswith("client-"):
                try:
                    client_pid = int(client_name.removeprefix("client-"))
                except ValueError:
                    pass
            if client_pid not in self.provenance_state.owner_pids:
                self.provenance_state.invalidate_active()
            return
        if line.startswith((b"%window-pane-changed ", b"%session-window-changed ")):
            self.pane_change.set()
            return
        if line.startswith(b"%pause "):
            pane_id = line.split(b" ", 1)[1].decode("ascii", "ignore")
            asyncio.create_task(self.command(f"refresh-client -A {pane_id}:continue"))

    async def read_loop(self) -> None:
        assert self.process is not None and self.process.stdout is not None
        descriptor = self.process.stdout.fileno()
        while self.process.poll() is None:
            read_any = False
            while True:
                try:
                    data = os.read(descriptor, 65536)
                except BlockingIOError:
                    break
                except OSError:
                    return
                if not data:
                    return
                read_any = True
                self.line_buffer += data
                while b"\n" in self.line_buffer:
                    line, self.line_buffer = self.line_buffer.split(b"\n", 1)
                    await self.control_line(line.rstrip(b"\r"))
            if not read_any:
                await asyncio.sleep(0.004)

    async def quiet(self, duration: float = 0.14, timeout: float = 3.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if time.monotonic() - self.last_output_at >= duration:
                return
            await asyncio.sleep(0.015)
        raise RuntimeError("tmux pane output did not quiesce")

    async def _write_locked(self, raw: bytes) -> None:
        if self.closing or self.closed:
            raise RuntimeError("tmux control client is closed")
        target = self.pane_id or self.session_name
        for offset in range(0, len(raw), TMUX_INPUT_CHUNK_BYTES):
            chunk = raw[offset : offset + TMUX_INPUT_CHUNK_BYTES]
            await self.command(f"send-keys -t {target} -H {chunk.hex(' ')}")

    async def write(self, data: str | bytes) -> None:
        if isinstance(data, bytes):
            raw = data
        else:
            try:
                raw = data.encode("utf-8", "surrogateescape")
            except UnicodeEncodeError as exc:
                raise ValueError("terminal input contains invalid Unicode") from exc
        if not raw:
            return
        async with self.write_lock:
            await self._write_locked(raw)

    async def settle_active_provenance_fence(self) -> None:
        while True:
            task = self.provenance_state.fence_task
            if task is None:
                return
            try:
                await asyncio.shield(task)
            except RuntimeError:
                pass
            if self.provenance_state.fence_task in (None, task):
                return

    def schedule_active_provenance_fence(self, active: CommandProvenance) -> None:
        self.provenance_state.cancel_fence()
        task = asyncio.create_task(self._capture_active_provenance_fence(active))
        self.provenance_state.fence_task = task

        def clear(completed: asyncio.Task[None]) -> None:
            if self.provenance_state.fence_task is completed:
                self.provenance_state.fence_task = None

        task.add_done_callback(clear)

    async def _capture_active_provenance_fence(self, active: CommandProvenance) -> None:
        try:
            await asyncio.sleep(0.04)
            for attempt in range(3):
                try:
                    await self.quiet(0.14, timeout=0.8)
                    async with self.seed_lock:
                        async with self.write_lock:
                            if self.provenance_state.active is not active:
                                return
                            stable_offset = self.offset
                            # Ordinary tmux clients are allowed to remain attached. Their
                            # pane output crosses this control client's byte stream, so
                            # revision and row-digest fences detect edits after settlement;
                            # attach/detach effects are fenced by pane and geometry identity.
                            # A topology change that leaves bytes, cells, and geometry identical
                            # cannot affect the selected text. Input concurrent with this
                            # pre-settlement capture cannot be attributed to one client; the
                            # settled cells are the precise linearization limit rather than
                            # client attachment ownership.
                            snapshot = await asyncio.to_thread(
                                capture_pane_snapshot,
                                self.session_name,
                                self.pane_id,
                                CONNECT_HISTORY_LINES,
                            )
                            await self.quiet(0.04, timeout=0.2)
                            if self.offset != stable_offset:
                                if attempt < 2:
                                    continue
                                return
                            if (
                                active.owner_id != id(self)
                                or active.session_name != self.session_name
                                or active.pane_id != snapshot.pane_id
                                or active.cols != snapshot.cols
                                or active.rows != snapshot.rows
                                or active.layout_generation
                                != self.provenance_state.layout_generation
                            ):
                                return
                            identity = self.provenance_state.row_tracker.observe(
                                snapshot,
                                stable_offset,
                            )
                            if active.row_epoch != identity.epoch:
                                return
                            end_absolute_row = snapshot.history + snapshot.cursor_y
                            start_absolute_row = _absolute_row(
                                snapshot,
                                identity,
                                active.start_row,
                            )
                            if (end_absolute_row, snapshot.cursor_x) <= (
                                start_absolute_row,
                                active.start_x,
                            ):
                                return
                            self.provenance_state.active = replace(
                                active,
                                end_row=_identity_row(snapshot, identity, end_absolute_row),
                                end_x=snapshot.cursor_x,
                                row_digest=_command_row_digest(
                                    snapshot,
                                    start_absolute_row,
                                    end_absolute_row,
                                ),
                                terminal_revision=stable_offset,
                            )
                            return
                except (RuntimeError, asyncio.TimeoutError):
                    if attempt == 2:
                        return
                await asyncio.sleep(0.03)
        except asyncio.CancelledError:
            return

    async def write_with_command_start(
        self,
        data: str,
        draft: str,
        revision: int,
        tracking_generation: int,
    ) -> CommandProvenance | None:
        raw = data.encode("utf-8", "surrogateescape")
        if not raw:
            return None
        async with self.seed_lock:
            async with self.write_lock:
                start: tuple[str, int, int, int, int, int, int] | None = None
                for attempt in range(3):
                    try:
                        await self.quiet(0.03, timeout=0.12)
                        stable_offset = self.offset
                        snapshot = await asyncio.to_thread(
                            capture_pane_snapshot,
                            self.session_name,
                            self.pane_id,
                            CONNECT_HISTORY_LINES,
                        )
                        if self.offset == stable_offset and not snapshot.alternate:
                            identity = self.provenance_state.row_tracker.observe(
                                snapshot,
                                stable_offset,
                            )
                            start = (
                                snapshot.pane_id,
                                snapshot.cols,
                                snapshot.rows,
                                _identity_row(
                                    snapshot,
                                    identity,
                                    snapshot.history + snapshot.cursor_y,
                                ),
                                snapshot.cursor_x,
                                self.provenance_state.layout_generation,
                                identity.epoch,
                            )
                            break
                    except RuntimeError:
                        pass
                    if attempt < 2:
                        await asyncio.sleep(0.03)
                await self._write_locked(raw)
                unavailable = UnavailableCommandProvenance(
                    session_name=self.session_name,
                    pane_id=self.pane_id,
                    layout_generation=self.provenance_state.layout_generation,
                    draft=draft,
                    revision=revision,
                    owner_id=id(self),
                )
                if (
                    start is None
                    or self.provenance_state.tracking_generation != tracking_generation
                    or self.provenance_state.active is not None
                    or self.provenance_state.unavailable is not None
                ):
                    self.provenance_state.mark_unavailable(unavailable)
                    return None
                pane_id, cols, rows, start_row, start_x, layout_generation, row_epoch = start
                active = CommandProvenance(
                    session_name=self.session_name,
                    pane_id=pane_id,
                    cols=cols,
                    rows=rows,
                    layout_generation=layout_generation,
                    start_row=start_row,
                    start_x=start_x,
                    draft=draft,
                    revision=revision,
                    source="composer-sync",
                    owner_id=id(self),
                    row_epoch=row_epoch,
                )
                self.provenance_state.active = active
                self.provenance_state.unavailable = None
                return active

    async def write_unavailable_command_continuation(
        self,
        data: str,
        unavailable: UnavailableCommandProvenance,
        draft: str,
        revision: int,
        tracking_generation: int,
    ) -> UnavailableCommandProvenance | None:
        raw = data.encode("utf-8", "surrogateescape")
        async with self.write_lock:
            if raw:
                await self._write_locked(raw)
            if (
                self.provenance_state.tracking_generation != tracking_generation
                or self.provenance_state.unavailable is not unavailable
            ):
                return None
            updated = replace(unavailable, draft=draft, revision=revision)
            self.provenance_state.unavailable = updated
            return updated

    async def write_command_continuation(
        self,
        data: str,
        active: CommandProvenance,
        draft: str,
        revision: int,
        tracking_generation: int,
    ) -> CommandProvenance | None:
        raw = data.encode("utf-8", "surrogateescape")
        self.provenance_state.cancel_fence()
        async with self.write_lock:
            if raw:
                await self._write_locked(raw)
            if (
                self.provenance_state.tracking_generation != tracking_generation
                or self.provenance_state.active is not active
            ):
                return None
            updated = replace(
                active,
                draft=draft,
                revision=revision,
                end_row=None,
                end_x=None,
                row_digest="",
                terminal_revision=None,
            )
            self.provenance_state.active = updated
            return updated

    async def write_accepted_enter(
        self,
        active: CommandProvenance | None,
        revision: int,
        tracking_generation: int,
    ) -> AcceptedCommand | None:
        async with self.seed_lock:
            async with self.write_lock:
                record: AcceptedCommand | None = None
                if (
                    active is not None
                    and revision > active.revision
                    and active.end_row is not None
                    and active.end_x is not None
                    and active.row_digest
                ):
                    try:
                        await self.quiet(0.05, timeout=0.35)
                        stable_offset = self.offset
                        stable_epoch = self.epoch_state["epoch"]
                        stable_layout = self.epoch_state["layout"]
                        snapshot = await asyncio.to_thread(
                            capture_pane_snapshot,
                            self.session_name,
                            self.pane_id,
                            MAX_HISTORY_SEED_LINES,
                        )
                        await self.quiet(0.03, timeout=0.15)
                        identity = self.provenance_state.row_tracker.observe(snapshot, stable_offset)
                        start_absolute_row = _absolute_row(snapshot, identity, active.start_row)
                        end_absolute_row = _absolute_row(snapshot, identity, active.end_row)
                        row_digest = _command_row_digest(
                            snapshot,
                            start_absolute_row,
                            end_absolute_row,
                        )
                        cursor_end = (
                            _identity_row(
                                snapshot,
                                identity,
                                snapshot.history + snapshot.cursor_y,
                            ),
                            snapshot.cursor_x,
                        )
                        if (
                            self.offset == stable_offset
                            and self.epoch_state["epoch"] == stable_epoch
                            and self.epoch_state["layout"] == stable_layout
                            and active.terminal_revision == stable_offset
                            and self.provenance_state.tracking_generation
                            == tracking_generation
                            and self.provenance_state.active is active
                            and active.session_name == self.session_name
                            and active.owner_id == id(self)
                            and active.pane_id == snapshot.pane_id
                            and active.cols == snapshot.cols
                            and active.rows == snapshot.rows
                            and active.layout_generation
                            == self.provenance_state.layout_generation
                            and active.source == "composer-sync"
                            and active.row_epoch == identity.epoch
                            and cursor_end == (active.end_row, active.end_x)
                            and row_digest == active.row_digest
                        ):
                            record = AcceptedCommand(
                                session_name=active.session_name,
                                pane_id=active.pane_id,
                                cols=active.cols,
                                rows=active.rows,
                                layout_generation=active.layout_generation,
                                start_row=active.start_row,
                                start_x=active.start_x,
                                end_row=active.end_row,
                                end_x=active.end_x,
                                draft=active.draft,
                                revision=revision,
                                source=active.source,
                                row_digest=row_digest,
                                row_epoch=identity.epoch,
                            )
                    except (RuntimeError, asyncio.TimeoutError):
                        record = None
                await self._write_locked(b"\r")
                if (
                    record is not None
                    and self.provenance_state.tracking_generation == tracking_generation
                    and self.provenance_state.active is active
                ):
                    self.provenance_state.remember(record)
                else:
                    record = None
                if self.provenance_state.active is active:
                    self.provenance_state.active = None
                return record

    def acknowledge(self, payload: dict[str, Any]) -> bool:
        message_type = payload.get("type")
        try:
            epoch = int(payload.get("epoch", -1))
        except (TypeError, ValueError):
            return False
        if message_type == "seed-start-ack":
            pending = self.seed_start_acks.get(epoch)
            if pending:
                pending[1].update(payload)
                pending[0].set()
            return True
        if message_type == "seed-ack":
            event = self.seed_acks.get(epoch)
            if event:
                event.set()
            return True
        if message_type == "post-flush-ack":
            try:
                cycle = int(payload.get("cycle", -1))
            except (TypeError, ValueError):
                return True
            event = self.flush_acks.get((epoch, cycle))
            if event:
                event.set()
            return True
        if message_type == "selection-check-ack":
            request_id = str(payload.get("requestId", ""))
            pending = self.selection_acks.get(request_id)
            if pending:
                pending[1].update(payload)
                pending[0].set()
            return True
        return False

    async def _flush_seed_output(self, epoch: int, cutoff: int) -> None:
        cycle = 0
        while True:
            pending = [record for record in self.held if record["start"] >= cutoff]
            self.held = [record for record in self.held if record["start"] < cutoff]
            for record in pending:
                await self._send_output(record, "postseed")
            cycle += 1
            event = asyncio.Event()
            self.flush_acks[(epoch, cycle)] = event
            through = pending[-1]["end"] if pending else cutoff
            await self._send_json(
                {
                    "type": "post-flush",
                    "epoch": epoch,
                    "cycle": cycle,
                    "through": through,
                    "bytes": sum(record["end"] - record["start"] for record in pending),
                }
            )
            await asyncio.wait_for(event.wait(), timeout=5)
            self.flush_acks.pop((epoch, cycle), None)
            self.last_output_at = time.monotonic()
            await self.quiet(0.1)
            async with self.send_lock:
                if not any(record["start"] >= cutoff for record in self.held):
                    self.held.clear()
                    self.phase = "forward"
                    break
        await self._send_json(
            {
                "type": "seed-open",
                "epoch": epoch,
                "session": self.session_name,
                "paneId": self.pane_id,
                "cutoff": cutoff,
                "layoutGeneration": self.epoch_state["layout"],
            }
        )

    async def reseed(
        self,
        reason: str,
        *,
        mutate: Callable[[], Awaitable[None]] | None = None,
        history_lines: int | None = None,
        scroll_target: int | None = None,
        next_pane_id: str | None = None,
    ) -> None:
        async with self.seed_lock:
            if self.closing or self.closed:
                return
            if reason == "pane-change":
                self.provenance_state.invalidate_layout()
            elif reason in ("history", "initial", "session-switch"):
                self.provenance_state.invalidate_active()
            async with self.send_lock:
                self.phase = "hold"
                self.held = []
                if next_pane_id:
                    self.pane_id = next_pane_id
                self.epoch_state["epoch"] += 1
                self.epoch_state["layout"] += 1
                epoch = self.epoch_state["epoch"]
                start_event = asyncio.Event()
                start_payload: dict[str, Any] = {}
                self.seed_start_acks[epoch] = (start_event, start_payload)
                await self._send_json(
                    {
                        "type": "seed-start",
                        "epoch": epoch,
                        "reason": reason,
                        "session": self.session_name,
                        "paneId": self.pane_id,
                        "invalidFrom": self.offset,
                    }
                )
            await asyncio.wait_for(start_event.wait(), timeout=5)
            self.seed_start_acks.pop(epoch, None)
            if mutate is not None:
                await mutate()
            elif reason == "initial":
                try:
                    cols = int(start_payload.get("cols", 0))
                    rows = int(start_payload.get("rows", 0))
                except (TypeError, ValueError):
                    cols, rows = 0, 0
                if cols > 0 and rows > 0:
                    await self.set_size(max(20, cols), max(6, rows))
            requested_history = history_lines if history_lines is not None else CONNECT_HISTORY_LINES
            snapshot: PaneSnapshot | None = None
            cutoff = self.offset
            for _attempt in range(3):
                self.last_output_at = time.monotonic()
                await self.quiet()
                cutoff = self.offset
                candidate = await asyncio.to_thread(
                    capture_pane_snapshot,
                    self.session_name,
                    self.pane_id,
                    requested_history,
                )
                self.last_output_at = time.monotonic()
                await self.quiet(0.05)
                if self.offset == cutoff:
                    snapshot = candidate
                    break
            if snapshot is None:
                raise RuntimeError("tmux pane did not remain stable for capture")
            self.pane_id = snapshot.pane_id
            self.cutoff = cutoff
            self.seed_history = snapshot.seed_history
            self.held = [record for record in self.held if record["start"] >= cutoff]
            await self._send_json(
                {
                    "type": "seed-data",
                    "epoch": epoch,
                    "session": self.session_name,
                    "paneId": self.pane_id,
                    "cutoff": cutoff,
                    "layoutGeneration": self.epoch_state["layout"],
                    "meta": snapshot.metadata(),
                    "physicalRows": snapshot.physical_rows,
                    "scrollTarget": scroll_target,
                }
            )
            seed_event = asyncio.Event()
            self.seed_acks[epoch] = seed_event
            await self._send_json({"type": "seed-end", "epoch": epoch, "cutoff": cutoff})
            await asyncio.wait_for(seed_event.wait(), timeout=5)
            self.seed_acks.pop(epoch, None)
            await self._flush_seed_output(epoch, cutoff)

    async def _flush_selection_hold(self) -> None:
        while True:
            async with self.send_lock:
                pending = self.held
                self.held = []
                if not pending:
                    self.phase = "forward"
                    return
            for record in pending:
                await self._send_output(record, "selection-held")

    async def authoritative_selection(self, payload: dict[str, Any]) -> tuple[str | None, str | None]:
        request_id = str(payload.get("requestId", ""))
        stale_message = "Terminal changed; select again."
        try:
            epoch = int(payload.get("epoch", -1))
            revision = int(payload.get("revision", -1))
            cutoff = int(payload.get("cutoff", -1))
            layout_generation = int(payload.get("layoutGeneration", -1))
            client_cols = int(payload.get("cols", -1))
            client_rows = int(payload.get("rows", -1))
            base_y = int(payload.get("baseY", -1))
            start = payload["selection"]["start"]
            end = payload["selection"]["end"]
            start_x, start_y = int(start["x"]), int(start["y"])
            end_x, end_y = int(end["x"]), int(end["y"])
        except (KeyError, TypeError, ValueError):
            return None, stale_message
        if (
            payload.get("session") != self.session_name
            or payload.get("profile", "") != self.profile_id
            or payload.get("paneId") != self.pane_id
            or epoch != self.epoch_state["epoch"]
            or cutoff != self.cutoff
            or layout_generation != self.epoch_state["layout"]
        ):
            return None, stale_message

        await self.settle_active_provenance_fence()
        async with self.seed_lock:
            async with self.write_lock:
                async with self.send_lock:
                    if (
                        self.phase != "forward"
                        or revision != self.offset
                        or epoch != self.epoch_state["epoch"]
                        or cutoff != self.cutoff
                        or layout_generation != self.epoch_state["layout"]
                    ):
                        return None, stale_message
                    unavailable = self.provenance_state.unavailable
                    if (
                        unavailable is not None
                        and unavailable.session_name == self.session_name
                        and unavailable.pane_id == self.pane_id
                        and unavailable.layout_generation
                        == self.provenance_state.layout_generation
                    ):
                        return None, stale_message
                    self.phase = "hold"
                    self.held = []
                    stable_offset = self.offset
                    stable_epoch = self.epoch_state["epoch"]
                    stable_layout = self.epoch_state["layout"]
                    event = asyncio.Event()
                    ack_payload: dict[str, Any] = {}
                    self.selection_acks[request_id] = (event, ack_payload)
                    await self._send_json({"type": "selection-check", "requestId": request_id})
                try:
                    await asyncio.wait_for(event.wait(), timeout=5)
                    if ack_payload.get("unchanged") is not True:
                        return None, stale_message
                    self.last_output_at = time.monotonic()
                    await self.quiet(0.1)
                    if self.offset != stable_offset:
                        return None, stale_message
                    snapshot = await asyncio.to_thread(
                        capture_pane_snapshot,
                        self.session_name,
                        self.pane_id,
                        max(base_y, self.seed_history, CONNECT_HISTORY_LINES),
                    )
                    self.last_output_at = time.monotonic()
                    await self.quiet(0.05)
                    if self.offset != stable_offset:
                        return None, stale_message
                    if snapshot.cols != client_cols or snapshot.rows != client_rows:
                        return None, stale_message
                    if snapshot.alternate != (payload.get("bufferType") == "alternate"):
                        return None, stale_message
                    expected_base = 0 if snapshot.alternate else snapshot.seed_history
                    if base_y != expected_base:
                        return None, stale_message
                    relative_start_row = start_y - base_y
                    relative_end_row = end_y - base_y
                    identity = self.provenance_state.row_tracker.observe(snapshot, stable_offset)
                    absolute_start_row = snapshot.history + relative_start_row
                    absolute_end_row = snapshot.history + relative_end_row
                    accepted = self.provenance_state.accepted(self.pane_id)
                    active = self.provenance_state.active
                    if (
                        self.offset != stable_offset
                        or self.epoch_state["epoch"] != stable_epoch
                        or self.epoch_state["layout"] != stable_layout
                        or self.session_name != payload.get("session")
                        or self.pane_id != snapshot.pane_id
                    ):
                        return None, stale_message
                    matched, exact_text = exact_provenance_selection(
                        snapshot,
                        self.session_name,
                        self.provenance_state.layout_generation,
                        (_identity_row(snapshot, identity, absolute_start_row), start_x),
                        (_identity_row(snapshot, identity, absolute_end_row), end_x),
                        active,
                        accepted,
                        identity,
                        terminal_revision=stable_offset,
                    )
                    if matched:
                        return exact_text or "", None
                    provider = await asyncio.to_thread(
                        provider_selection,
                        snapshot,
                        start_x,
                        relative_start_row,
                        end_x,
                        relative_end_row,
                    )
                    verification_snapshot = await asyncio.to_thread(
                        capture_pane_snapshot,
                        self.session_name,
                        self.pane_id,
                        max(base_y, self.seed_history, CONNECT_HISTORY_LINES),
                    )
                    if (
                        verification_snapshot != snapshot
                        or self.offset != stable_offset
                        or self.epoch_state["epoch"] != stable_epoch
                        or self.epoch_state["layout"] != stable_layout
                        or self.session_name != payload.get("session")
                        or self.pane_id != snapshot.pane_id
                    ):
                        return None, stale_message
                    if provider.owned:
                        return provider.text or "", None
                    selected_text = extract_authoritative_selection(
                        snapshot,
                        start_x,
                        relative_start_row,
                        end_x,
                        relative_end_row,
                    )
                    return selected_text, None
                except (RuntimeError, ValueError, asyncio.TimeoutError):
                    return None, stale_message
                finally:
                    self.selection_acks.pop(request_id, None)
                    await self._flush_selection_hold()

    async def close(self) -> None:
        self.closing = True
        if (
            (
                self.provenance_state.active is not None
                and self.provenance_state.active.owner_id == id(self)
            )
            or (
                self.provenance_state.unavailable is not None
                and self.provenance_state.unavailable.owner_id == id(self)
            )
        ):
            self.provenance_state.invalidate_active()
        async with self.seed_lock:
            async with self.write_lock:
                if self.closed:
                    return
                self.closed = True
                process = self.process
                owner_pid = process.pid if process else None
                try:
                    try:
                        if process and process.poll() is None:
                            process.terminate()
                            try:
                                await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=1)
                            except asyncio.TimeoutError:
                                process.kill()
                                await asyncio.to_thread(process.wait)
                    finally:
                        if self.read_task:
                            self.read_task.cancel()
                            try:
                                await self.read_task
                            except (asyncio.CancelledError, Exception):
                                pass
                finally:
                    if owner_pid is not None:
                        self.provenance_state.owner_pids.discard(owner_pid)


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
        internal_token: str | None = None,
        passkeys: PasskeyAuth | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.label = (label or "term").strip()[:12] or "term"
        self.session_name = session_name
        self.shell = shell
        self.cwd = cwd
        self.token = token
        self.internal_token = internal_token
        self.passkeys = passkeys
        self._passkey_managers: dict[tuple[str, str], PasskeyAuth] = {}
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
        self.command_provenance_states: dict[str, CommandProvenanceState] = {}
        self.terminal_write_locks: dict[str, asyncio.Lock] = {}
        self.live_terminal_connections: dict[
            str, list[tuple[TmuxBridge, dict[str, Any]]]
        ] = {}
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

    def queue_scroll_history(self, session_name: str, pane_id: str, lines: int) -> None:
        if lines == 0:
            return
        state = self.scroll_states.setdefault(
            session_name,
            {"pending": 0, "task": None, "paneId": pane_id},
        )
        if state["paneId"] != pane_id:
            state["pending"] = 0
            state["paneId"] = pane_id
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
            await asyncio.to_thread(scroll_session_history, state["paneId"], take)

    async def settle_scroll_history(self, session_name: str) -> None:
        # Scroll commands execute out-of-band; before writing user input to the
        # pane, drop queued deltas and wait out the in-flight tmux call.
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

    def internal_request_principal(self, connection: ServerConnection) -> str | None:
        if not self.internal_token:
            return None
        host = remote_ip(connection.remote_address)
        if host not in ("127.0.0.1", "::1"):
            return None
        candidate = connection.request.headers.get("X-Mobile-Terminal-Internal-Token", "")
        if not hmac.compare_digest(candidate, self.internal_token):
            return None
        principal = connection.request.headers.get("X-Mobile-Terminal-Principal", "").strip()
        return principal or "proxy"

    def internal_request_profile(self, connection: ServerConnection) -> str:
        if self.internal_request_principal(connection) is None:
            return ""
        return connection.request.headers.get(INTERNAL_PROFILE_HEADER, "").strip()[:80]

    def request_is_public(self, connection: ServerConnection) -> bool:
        """True when the request arrived over Tailscale Funnel (public internet)
        rather than tailnet serve. Tailscale marks Funnel requests with a
        Tailscale-Funnel-Request header and never injects identity headers for
        them; we additionally refuse to trust identity on such requests."""
        return connection.request.headers.get("Tailscale-Funnel-Request") is not None

    def proxy_login(self, connection: ServerConnection) -> str | None:
        """The Tailscale identity that `tailscale serve` injected, but only when
        the request actually came through the local proxy (loopback source) on a
        *tailnet* (non-Funnel) request. A direct or public-Funnel connection
        could carry a forged header, so it's ignored there. Tailscale also strips
        client-supplied Tailscale-* headers, so this is defense in depth."""
        host = remote_ip(connection.remote_address)
        if host not in ("127.0.0.1", "::1"):
            return None
        if self.request_is_public(connection):
            return None  # public Funnel: never trust an identity header
        if not request_origin_matches_host(connection):
            return None  # cross-origin browser WebSockets must not inherit proxy identity
        login = connection.request.headers.get("Tailscale-User-Login")
        return login.strip().lower() if login else None

    def auto_auth_user(self, connection: ServerConnection) -> str | None:
        """Map the proxy-provided Tailscale identity to a user for token-less
        login — but ONLY when MOBILE_TERMINAL_TRUST_IDENTITY is set.

        Identity-header trust is off by default because Tailscale Funnel was
        observed to inject the node owner's `Tailscale-User-Login` into public
        internet requests (with no Funnel marker), which would auto-authenticate
        anyone as the owner. Enable it only on a machine that is NOT publicly
        funnel-exposed — e.g. a brief tailnet-only enrollment window — so a
        device can enroll its key with no token, then it's disabled again."""
        if os.environ.get("MOBILE_TERMINAL_TRUST_IDENTITY", "").strip().lower() not in ("1", "true", "yes"):
            return None
        login = self.proxy_login(connection)  # loopback + non-Funnel + identity header
        if not login:
            return None
        if not self.multi_tenant:
            return ""
        for name, meta in self.users.items():
            if meta.get("tailscaleLogin") and meta["tailscaleLogin"] == login:
                return name
        return None

    def rp_id(self, connection: ServerConnection) -> str:
        override = os.environ.get("MOBILE_TERMINAL_RP_ID", "").strip()
        if override:
            return override
        host = connection.request.headers.get("Host", "") or self.host
        try:
            return urlsplit(f"//{host}").hostname or self.host
        except ValueError:
            return self.host

    def expected_origin(self, connection: ServerConnection) -> str:
        override = os.environ.get("MOBILE_TERMINAL_ORIGIN", "").strip()
        if override:
            return override
        host = connection.request.headers.get("Host", "") or self.host
        return f"https://{host}"

    def passkey_auth(self, connection: ServerConnection) -> PasskeyAuth:
        if self.passkeys is not None:
            return self.passkeys
        rp_id = self.rp_id(connection)
        origin = self.expected_origin(connection)
        key = (rp_id, origin)
        manager = self._passkey_managers.get(key)
        if manager is None:
            manager = PasskeyAuth(
                PASSKEY_STATE_DIR / hashlib.sha256(rp_id.encode("utf-8")).hexdigest()[:16],
                rp_id=rp_id,
                rp_name=f"{self.label} terminal",
                expected_origin=origin,
            )
            self._passkey_managers[key] = manager
        return manager

    def passkey_realm(self, user: str) -> str:
        if not self.multi_tenant:
            return "standalone"
        digest = hashlib.sha256(user.encode("utf-8")).hexdigest()[:32]
        return f"user-{digest}"

    def passkey_principal(self, user: str) -> str:
        return user if self.multi_tenant else "standalone"

    async def receive_auth_message(
        self,
        connection: ServerConnection,
        *,
        timeout: float,
    ) -> dict[str, Any] | None:
        try:
            raw = await asyncio.wait_for(connection.recv(), timeout=timeout)
            if not isinstance(raw, str):
                return None
            payload = json.loads(raw)
        except (ConnectionClosed, TimeoutError, ValueError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    async def authenticate_passkey(
        self,
        connection: ServerConnection,
        user: str,
        *,
        binding: str,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        manager = self.passkey_auth(connection)
        realm = self.passkey_realm(user)
        principal = self.passkey_principal(user)
        credentials = manager.list_credentials(realm)
        if not credentials:
            return None, None
        message = manager.begin_authentication(
            realm,
            principal=principal,
            binding=binding,
        )
        await self.send_json(connection, message)
        payload = await self.receive_auth_message(connection, timeout=120)
        if payload is None:
            raise PasskeyChallengeError("Passkey response was not received.")
        if payload.get("type") == "auth":
            return None, payload
        if payload.get("type") != "webauthn-auth":
            raise PasskeyChallengeError("Unexpected passkey response.")
        record = manager.finish_authentication(
            realm,
            payload.get("challengeId", ""),
            payload.get("assertion"),
            binding=binding,
        )
        if record.get("principal") != principal:
            raise PasskeyVerificationError("Passkey principal is invalid.")
        return record, None

    async def register_passkey(
        self,
        connection: ServerConnection,
        user: str,
        *,
        binding: str,
    ) -> dict[str, Any]:
        manager = self.passkey_auth(connection)
        realm = self.passkey_realm(user)
        principal = self.passkey_principal(user)
        message = manager.begin_registration(
            realm,
            principal=principal,
            user_name=user or self.label,
            user_display_name=self.users.get(user, {}).get("label")
            if self.multi_tenant
            else self.label,
            label=device_label(connection.request.headers.get("User-Agent", "")),
            binding=binding,
        )
        await self.send_json(connection, message)
        payload = await self.receive_auth_message(connection, timeout=120)
        if payload is None or payload.get("type") != "webauthn-register":
            raise PasskeyChallengeError("Passkey registration response was not received.")
        record = manager.finish_registration(
            realm,
            payload.get("challengeId", ""),
            payload.get("attestation"),
            binding=binding,
        )
        if record.get("principal") != principal:
            raise PasskeyVerificationError("Passkey principal is invalid.")
        return record

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

    def open_tabs_for(self, user: str) -> list[str]:
        """The user's persisted open-tab set, pruned to sessions that still
        exist and that the user may see, so every entry can be reopened."""
        visible = self.visible_session_names(user)
        stored = load_open_tabs_global() if not self.multi_tenant else load_user_state(user)["openTabs"]
        return [name for name in stored if name in visible]

    def save_open_tabs(self, user: str, names: Any) -> None:
        """Persist the client-reported open-tab set (the user's tabs follow
        them across devices). Entries are deduped and restricted to the user's
        visible sessions so one tenant can never pin another's sessions."""
        if not isinstance(names, list):
            return
        visible = self.visible_session_names(user)
        cleaned: list[str] = []
        for name in names:
            if isinstance(name, str) and name in visible and name not in cleaned:
                cleaned.append(name)
        if not self.multi_tenant:
            if load_open_tabs_global() != cleaned:
                save_open_tabs_global(cleaned)
            return
        state = load_user_state(user)
        if state["openTabs"] != cleaned:
            state["openTabs"] = cleaned
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
            "new-session",
            "-d",
            "-s",
            name,
            "-n",
            "shell",
            "-c",
            path,
            terminal_command(f"{self.shell} -l"),
            check=False,
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
        # No requested/default session exists: resume the most-recently-active
        # real session instead of force-creating a new tab. Only fall through to
        # creating one when there is genuinely nothing to attach to.
        for name in sessions_by_recent_activity():
            if not name.startswith(BTOP_SESSION_PREFIX):
                return name, False
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
                # Silent device-key auth: the client enrolls a non-extractable
                # key when trusted (tailnet) and signs a nonce to reconnect from
                # anywhere.
                "deviceKeyAuth": True,
                "passkeyAuth": True,
                "rpId": self.rp_id(connection),
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
                body, gzipped, etag = self.inlined_index()
            except OSError:
                return http_response(404, b"Not Found", "text/plain; charset=utf-8")
            headers = {"ETag": etag, "Cache-Control": "no-cache"}
            if etag in request.headers.get("If-None-Match", ""):
                return http_response(304, b"", "text/html; charset=utf-8", headers)
            if request.headers.get(":method") == "HEAD":
                return http_response(200, b"", "text/html; charset=utf-8", headers)
            if "gzip" in request.headers.get("Accept-Encoding", ""):
                return http_response(
                    200,
                    gzipped,
                    "text/html; charset=utf-8",
                    {**headers, "Content-Encoding": "gzip", "Vary": "Accept-Encoding"},
                )
            return http_response(200, body, "text/html; charset=utf-8", headers)
        return await process_request(connection, request)

    def inlined_index(self) -> tuple[bytes, bytes, str]:
        """index.html with its CSS and JS embedded inline, so a cold first load
        gets the whole app shell in one response (no separate asset round-trips).
        Returns (body, gzipped_body, etag), cached per label for the process."""
        cached = _INLINED_INDEX_CACHE.get(self.label)
        if cached is not None:
            return cached
        raw = (STATIC_ROOT / "index.html").read_text()
        mtimes = [(STATIC_ROOT / "index.html").stat().st_mtime_ns]

        def read_asset(href: str) -> str | None:
            target, _ = safe_join(href)
            if not target or not target.is_file():
                return None
            mtimes.append(target.stat().st_mtime_ns)
            return target.read_text()

        def css_sub(match: "re.Match[str]") -> str:
            content = read_asset(match.group(1))
            if content is None:
                return match.group(0)
            return "<style>" + content.replace("</style", "<\\/style") + "</style>"

        def js_sub(match: "re.Match[str]") -> str:
            content = read_asset(match.group(1))
            if content is None:
                return match.group(0)
            return "<script>" + content.replace("</script", "<\\/script") + "</script>"

        html = re.sub(r'<link rel="stylesheet" href="([^"]+)">', css_sub, raw)
        html = re.sub(r'<script defer src="([^"]+)"></script>', js_sub, html)
        html = html.replace("__MT_LABEL__", self.label)
        body = html.encode("utf-8")
        etag = f'"idx-{(sum(mtimes) & 0xFFFFFFFFFFFF):x}-{len(body):x}"'
        result = (body, gzip.compress(body, 6), etag)
        _INLINED_INDEX_CACHE[self.label] = result
        return result

    def manifest(self) -> dict[str, Any]:
        # The rendered icon depends only on the label (render_app_icon draws the
        # label text), so key the cache-busting query on the label. Re-labeling a
        # host via MOBILE_TERMINAL_LABEL then auto-busts every device's cached
        # icon with zero code edits — branding is env-config, not a source patch.
        iconver = hashlib.md5(self.label.encode("utf-8")).hexdigest()[:8]
        return {
            "name": f"{self.label} terminal",
            "short_name": self.label,
            "display": "standalone",
            "background_color": "#0b121b",
            "theme_color": "#0b121b",
            "icons": [
                {"src": f"/app-icon-192.png?v={iconver}", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": f"/app-icon-512.png?v={iconver}", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": f"/app-icon-512.png?v={iconver}", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
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

    def command_provenance_state(self, session_name: str) -> CommandProvenanceState:
        if not hasattr(self, "command_provenance_states"):
            self.command_provenance_states = {}
        state = self.command_provenance_states.get(session_name)
        if state is None:
            state = CommandProvenanceState()
            self.command_provenance_states[session_name] = state
        return state

    def terminal_write_lock(self, session_name: str) -> asyncio.Lock:
        if not hasattr(self, "terminal_write_locks"):
            self.terminal_write_locks = {}
        lock = self.terminal_write_locks.get(session_name)
        if lock is None:
            lock = asyncio.Lock()
            self.terminal_write_locks[session_name] = lock
        return lock

    def register_live_terminal(
        self,
        session_name: str,
        bridge: TmuxBridge,
        state: dict[str, Any],
    ) -> None:
        if not hasattr(self, "live_terminal_connections"):
            self.live_terminal_connections = {}
        self.live_terminal_connections.setdefault(session_name, []).append((bridge, state))

    def unregister_live_terminal(self, session_name: str, bridge: TmuxBridge) -> None:
        if not hasattr(self, "live_terminal_connections"):
            return
        connections = self.live_terminal_connections.get(session_name, [])
        connections = [entry for entry in connections if entry[0] is not bridge]
        if connections:
            self.live_terminal_connections[session_name] = connections
        else:
            self.live_terminal_connections.pop(session_name, None)

    async def open_live_terminal(self, bridge: TmuxBridge, state: dict[str, Any]) -> None:
        session_name = getattr(bridge, "session_name", state["session"])
        self.register_live_terminal(session_name, bridge, state)
        try:
            await bridge.open()
        except BaseException:
            session_name = getattr(bridge, "session_name", state["session"])
            self.unregister_live_terminal(session_name, bridge)
            await bridge.close()
            raise

    def transfer_single_tenant_session_state(self, old_name: str, new_name: str) -> None:
        for mapping_name in ("mobile_composer_states", "terminal_sizes", "scroll_states"):
            mapping = getattr(self, mapping_name, None)
            if mapping is not None and old_name in mapping:
                mapping[new_name] = mapping.pop(old_name)
        provenance = self.command_provenance_states.pop(old_name, None)
        if provenance is not None:
            if provenance.active is not None:
                provenance.active = replace(provenance.active, session_name=new_name)
            if provenance.unavailable is not None:
                provenance.unavailable = replace(provenance.unavailable, session_name=new_name)
            provenance.accepted_records = deque(
                (replace(record, session_name=new_name) for record in provenance.accepted_records),
                maxlen=COMMAND_PROVENANCE_RECORDS_PER_SESSION,
            )
            self.command_provenance_states[new_name] = provenance
        lock = self.terminal_write_locks.pop(old_name, None)
        if lock is not None:
            self.terminal_write_locks[new_name] = lock
        connections = self.live_terminal_connections.pop(old_name, [])
        if connections:
            self.live_terminal_connections.setdefault(new_name, []).extend(connections)
            for live_bridge, live_state in connections:
                live_bridge.session_name = new_name
                if provenance is not None:
                    live_bridge.provenance_state = provenance
                if lock is not None:
                    live_bridge.write_lock = lock
                live_state["session"] = new_name

    def invalidate_command_provenance(self, session_name: str) -> None:
        self.command_provenance_state(session_name).invalidate_active()

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

    def reset_mobile_composer_tracking(
        self,
        session_name: str,
        *,
        allow_provenance_start: bool = False,
    ) -> None:
        self.invalidate_command_provenance(session_name)
        state = self.mobile_composer_state(session_name)
        state["draft"] = ""
        state["cursor"] = 0
        state["historyIndex"] = None
        state["pendingDraft"] = ""
        state["tracked"] = False
        state["source"] = "reset"
        state["provenanceStartAllowed"] = allow_provenance_start

    async def force_clear_mobile_composer(
        self,
        bridge: TmuxBridge,
        session_name: str,
        revision: int | None = None,
    ) -> None:
        if pane_in_mode(session_name):
            tmux_capture("send-keys", "-t", session_name, "-X", "cancel", check=False)
        self.invalidate_command_provenance(session_name)
        sequence = CTRL_E + ("\u007f" * MOBILE_COMPOSER_FORCE_CLEAR_BACKSPACES) + CTRL_U
        await bridge.write(sequence)
        self.reset_mobile_composer_tracking(session_name, allow_provenance_start=True)
        state = self.mobile_composer_state(session_name)
        if revision is not None:
            state["revision"] = max(state["revision"], revision)
        state["source"] = "force-clear"

    async def sync_mobile_composer(
        self,
        bridge: TmuxBridge,
        session_name: str,
        value: str,
        cursor: Any,
        *,
        revision: int | None = None,
        reset_history_index: bool = True,
        direct_provenance: bool = True,
    ) -> dict[str, Any]:
        state = self.mobile_composer_state(session_name)
        provenance_state = self.command_provenance_state(session_name)
        next_value = value.replace("\r\n", "\n").replace("\r", "\n")
        sequence, next_cursor = build_composer_sync_sequence(
            state["draft"],
            state["cursor"],
            next_value,
            cursor,
        )
        active = provenance_state.active
        unavailable = provenance_state.unavailable
        can_continue = bool(
            direct_provenance
            and active is not None
            and active.owner_id == id(bridge)
            and active.session_name == session_name
            and active.pane_id == bridge.pane_id
            and active.layout_generation == provenance_state.layout_generation
            and active.draft == state["draft"]
            and active.revision == state["revision"]
            and state["tracked"]
            and state["source"] == "composer-sync"
            and state["cursor"] == len(state["draft"])
            and revision is not None
            and revision > active.revision
            and next_cursor == len(next_value)
            and len(next_value) <= COMMAND_PROVENANCE_MAX_CHARS
        )
        can_continue_unavailable = bool(
            direct_provenance
            and unavailable is not None
            and unavailable.owner_id == id(bridge)
            and unavailable.session_name == session_name
            and unavailable.pane_id == bridge.pane_id
            and unavailable.layout_generation == provenance_state.layout_generation
            and unavailable.draft == state["draft"]
            and unavailable.revision == state["revision"]
            and state["tracked"]
            and state["source"] == "composer-sync"
        )
        can_start = bool(
            direct_provenance
            and active is None
            and unavailable is None
            and state.get("provenanceStartAllowed") is True
            and not state["tracked"]
            and state["draft"] == ""
            and state["cursor"] == 0
            and sequence
            and revision is not None
            and revision > 0
            and next_cursor == len(next_value)
            and 0 < len(next_value) <= COMMAND_PROVENANCE_MAX_CHARS
        )
        if (
            not can_continue
            and not can_continue_unavailable
            and not can_start
            and (sequence or unavailable is None)
        ):
            provenance_state.invalidate_active()

        tracking_generation = provenance_state.tracking_generation
        updated_active: CommandProvenance | None = None
        updated_unavailable: UnavailableCommandProvenance | None = None
        if pane_in_mode(session_name):
            tmux_capture("send-keys", "-t", session_name, "-X", "cancel", check=False)
        try:
            if can_start:
                updated_active = await bridge.write_with_command_start(
                    sequence,
                    next_value,
                    revision or 0,
                    tracking_generation,
                )
            elif can_continue:
                updated_active = await bridge.write_command_continuation(
                    sequence,
                    active,
                    next_value,
                    revision or 0,
                    tracking_generation,
                )
            elif can_continue_unavailable:
                updated_unavailable = await bridge.write_unavailable_command_continuation(
                    sequence,
                    unavailable,
                    next_value,
                    max(unavailable.revision, revision or 0),
                    tracking_generation,
                )
            elif sequence:
                await bridge.write(sequence)
        except (asyncio.CancelledError, Exception):
            if can_start or can_continue or can_continue_unavailable:
                provenance_state.mark_unavailable(
                    UnavailableCommandProvenance(
                        session_name=session_name,
                        pane_id=bridge.pane_id,
                        layout_generation=provenance_state.layout_generation,
                        draft=next_value,
                        revision=revision or 0,
                        owner_id=id(bridge),
                    )
                )
            elif unavailable is not None:
                provenance_state.mark_unavailable(unavailable)
            else:
                provenance_state.invalidate_active()
            raise
        if can_start and updated_active is None:
            provenance_state.mark_unavailable(
                UnavailableCommandProvenance(
                    session_name=session_name,
                    pane_id=bridge.pane_id,
                    layout_generation=provenance_state.layout_generation,
                    draft=next_value,
                    revision=revision or 0,
                    owner_id=id(bridge),
                )
            )
        elif can_continue and updated_active is None:
            provenance_state.mark_unavailable(
                UnavailableCommandProvenance(
                    session_name=session_name,
                    pane_id=bridge.pane_id,
                    layout_generation=provenance_state.layout_generation,
                    draft=next_value,
                    revision=revision or 0,
                    owner_id=id(bridge),
                )
            )
        elif can_continue_unavailable and updated_unavailable is None:
            provenance_state.mark_unavailable(
                replace(
                    unavailable,
                    draft=next_value,
                    revision=max(unavailable.revision, revision or 0),
                )
            )
        if updated_active is not None:
            bridge.schedule_active_provenance_fence(updated_active)

        state["draft"] = next_value
        state["cursor"] = next_cursor
        if revision is not None:
            state["revision"] = max(state["revision"], revision)
        if reset_history_index:
            state["historyIndex"] = None
            state["pendingDraft"] = next_value
        state["tracked"] = True
        state["source"] = "composer-sync"
        state["provenanceStartAllowed"] = False
        return state

    async def commit_mobile_composer(
        self,
        bridge: TmuxBridge,
        session_name: str,
        revision: int | None = None,
    ) -> None:
        await bridge.settle_active_provenance_fence()
        session_name = bridge.session_name
        state = self.mobile_composer_state(session_name)
        provenance_state = self.command_provenance_state(session_name)
        line = state["draft"]
        active = provenance_state.active
        unavailable = provenance_state.unavailable
        if not (
            active is not None
            and active.owner_id == id(bridge)
            and active.draft == line
            and active.revision == state["revision"]
            and state["tracked"]
            and state["source"] == "composer-sync"
            and state["cursor"] == len(line)
            and revision is not None
            and revision > active.revision
        ):
            active = None
            provenance_state.invalidate_active()
        if pane_in_mode(session_name):
            tmux_capture("send-keys", "-t", session_name, "-X", "cancel", check=False)
        try:
            if active is None:
                await bridge.write("\r")
                record = None
            else:
                record = await bridge.write_accepted_enter(
                    active,
                    revision or 0,
                    provenance_state.tracking_generation,
                )
        except (asyncio.CancelledError, Exception):
            if unavailable is not None:
                provenance_state.mark_unavailable(unavailable)
            else:
                provenance_state.invalidate_active()
            raise
        if line:
            history = state["history"]
            history.append(line)
            if len(history) > MOBILE_COMPOSER_HISTORY_LIMIT:
                del history[:-MOBILE_COMPOSER_HISTORY_LIMIT]
        if revision is not None:
            state["revision"] = max(state["revision"], revision)
        self.reset_mobile_composer_tracking(session_name, allow_provenance_start=True)

    async def fallback_mobile_composer_history(
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
        next_state = await self.sync_mobile_composer(
            bridge,
            session_name,
            next_value,
            len(next_value),
            revision=revision,
            reset_history_index=False,
            direct_provenance=False,
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
        self.invalidate_command_provenance(session_name)
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
            state["provenanceStartAllowed"] = False
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

        self.invalidate_command_provenance(session_name)
        state = self.mobile_composer_state(session_name)
        save_pending_draft = direction == "up" and state["historyIndex"] is None
        await bridge.write(arrow)
        if save_pending_draft:
            state["pendingDraft"] = state["draft"]
        next_state = await self.refresh_mobile_composer_from_terminal(session_name, revision=revision)
        if next_state is not None:
            next_state["historyIndex"] = None
            return next_state

        return await self.fallback_mobile_composer_history(
            bridge,
            session_name,
            direction,
            revision=revision,
        )

    async def handle_image_upload(
        self,
        connection: ServerConnection,
        user: str,
        payload: dict[str, Any],
    ) -> None:
        """Save a screenshot pasted into the composer, then hand the client back
        the file's absolute path. The client drops that path into the prompt so
        the claude CLI (or any tool at the prompt) reads the image by path —
        reusing the already-authenticated WebSocket instead of a new HTTP route."""
        data_b64 = payload.get("data", "")
        if not isinstance(data_b64, str) or not data_b64:
            await self.send_json(connection, {"type": "image-uploaded", "error": "Empty image."})
            return
        mime = str(payload.get("mime", "image/png") or "image/png")
        ext = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/jpg": "jpg",
            "image/gif": "gif",
            "image/webp": "webp",
        }.get(mime.lower(), "png")
        try:
            # binascii.Error (raised on malformed base64) subclasses ValueError.
            raw = base64.b64decode(data_b64, validate=True)
        except ValueError:
            await self.send_json(connection, {"type": "image-uploaded", "error": "Bad image data."})
            return
        MAX_IMAGE_BYTES = 16 * 1024 * 1024
        if not raw or len(raw) > MAX_IMAGE_BYTES:
            await self.send_json(connection, {"type": "image-uploaded", "error": "Image too large."})
            return
        uploads = user_dir(user) / "uploads"
        try:
            uploads.mkdir(parents=True, exist_ok=True)
            dest = uploads / f"paste-{time.time_ns()}.{ext}"
            dest.write_bytes(raw)
        except OSError:
            await self.send_json(connection, {"type": "image-uploaded", "error": "Couldn't save image."})
            return
        await self.send_json(connection, {"type": "image-uploaded", "path": str(dest)})

    async def handle_command(
        self,
        connection: ServerConnection,
        bridge: TmuxBridge,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        session_name = state["session"]
        user = state.get("user", "")
        message_type = payload.get("type")
        input_data = ""
        if message_type == "input":
            candidate = payload.get("data", "")
            if not isinstance(candidate, str):
                return
            try:
                candidate.encode("utf-8", "surrogateescape")
            except UnicodeEncodeError:
                return
            input_data = candidate
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
            if message_type != "input" or input_is_user_keystroke(input_data):
                await self.settle_scroll_history(session_name)
            session_name = state["session"]
        if message_type == "composer-sync":
            await self.sync_mobile_composer(
                bridge,
                session_name,
                str(payload.get("value", "")),
                payload.get("cursor"),
                revision=revision,
            )
            return

        if message_type == "composer-semantic-sync":
            self.invalidate_command_provenance(session_name)
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
            composer_state["provenanceStartAllowed"] = False
            await self.send_composer_state(connection, session_name)
            return

        if message_type == "composer-enter":
            await self.commit_mobile_composer(bridge, session_name, revision=revision)
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

        if message_type == "upload-image":
            await self.handle_image_upload(connection, user, payload)
            return

        if message_type == "composer-force-clear":
            await self.force_clear_mobile_composer(bridge, session_name, revision=revision)
            await self.send_composer_state(connection, session_name)
            return

        if message_type == "selection-request":
            text, error = await bridge.authoritative_selection(payload)
            response: dict[str, Any] = {
                "type": "selection-result",
                "requestId": str(payload.get("requestId", "")),
            }
            if error:
                response["error"] = error
            else:
                response["text"] = text or ""
            await self.send_json(connection, response)
            return

        if message_type == "history-reseed":
            try:
                requested = max(
                    CONNECT_HISTORY_LINES,
                    min(MAX_HISTORY_SEED_LINES, int(payload.get("historyLines", CONNECT_HISTORY_LINES))),
                )
                scroll_target = int(payload.get("scrollTarget", 0))
            except (TypeError, ValueError):
                return
            await bridge.reseed(
                "history",
                history_lines=requested,
                scroll_target=scroll_target,
            )
            return

        if message_type == "input":
            data = input_data
            user_keystroke = input_is_user_keystroke(data)
            if user_keystroke and pane_in_mode(session_name):
                tmux_capture("send-keys", "-t", session_name, "-X", "cancel", check=False)
            if data:
                self.invalidate_command_provenance(session_name)
            if data and user_keystroke:
                self.reset_mobile_composer_tracking(session_name)
            await bridge.write(data)
            if data:
                self.invalidate_command_provenance(session_name)
            return

        if message_type == "resize":
            cols = max(20, int(payload.get("cols", 80)))
            rows = max(6, int(payload.get("rows", 24)))
            self.invalidate_command_provenance(session_name)
            self.terminal_sizes[session_name] = (cols, rows)
            await bridge.resize(cols, rows)
            return

        if message_type == "scroll-history":
            lines = int(payload.get("lines", 0))
            self.queue_scroll_history(session_name, bridge.pane_id, lines)
            return

        if message_type == "request-tabs":
            await self.send_tabs(connection, user, session_name)
            return

        if message_type == "request-sessions":
            await self.send_sessions(connection, user, session_name)
            return

        if message_type == "open-tabs":
            self.save_open_tabs(user, payload.get("tabs"))
            return

        if message_type == "register-key":
            self.handle_register_key(connection, state, payload)
            return

        if message_type == "forget-key":
            # Revoke only this connection's authenticated realm and device.
            if (
                payload.get("realm") == self.passkey_principal(user)
                and payload.get("profile") == ""
                and str(payload.get("deviceId", ""))[:64] == state.get("deviceId")
            ):
                forget_device_key(user, state.get("deviceId", ""))
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
                    terminal_command(command),
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
                rename_lock = self.terminal_write_lock(target_name)
                async with rename_lock:
                    if target_name != name and session_exists(name):
                        await self.send_json(
                            connection,
                            {"type": "notice", "message": f"Session '{name}' already exists."},
                        )
                        return
                    renamed = tmux_capture(
                        "rename-session", "-t", target_name, name, check=False
                    )
                    if renamed.returncode != 0:
                        await self.send_json(
                            connection,
                            {"type": "notice", "message": f"Session '{target_name}' could not be renamed."},
                        )
                        return
                    if target_name != name:
                        self.transfer_single_tenant_session_state(target_name, name)
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
            self.command_provenance_states.pop(target_name, None)
            self.terminal_write_locks.pop(target_name, None)
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

    @staticmethod
    def _valid_spki(pub_key_spki_b64: str) -> bool:
        return valid_device_public_key(pub_key_spki_b64)

    async def maybe_enroll_device(self, connection: ServerConnection, state: dict[str, Any]) -> None:
        """Issue one connection-bound proof-of-possession challenge after the
        device has completed a WebAuthn assertion or registration."""
        if not state.get("needsEnroll") or not state.get("deviceId"):
            return
        enrollment = PendingDeviceEnrollment.issue(
            rp_id=self.rp_id(connection),
            realm=self.passkey_principal(state.get("user", "")),
            profile="",
            device_id=state["deviceId"],
            principal=self.passkey_principal(state.get("user", "")),
        )
        state["enrollment"] = enrollment
        await self.send_json(connection, enrollment.message())

    def handle_register_key(
        self,
        connection: ServerConnection,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> None:
        enrollment = state.get("enrollment")
        if (
            not isinstance(enrollment, PendingDeviceEnrollment)
            or payload.get("enrollmentId") != enrollment.enrollment_id
        ):
            return
        state["enrollment"] = None
        state["needsEnroll"] = False
        if not enrollment.verify(payload):
            return
        register_device_key(
            state.get("user", ""),
            enrollment.device_id,
            str(payload.get("publicKey", "")),
            device_label(connection.request.headers.get("User-Agent", "")),
        )

    async def websocket_handler(self, connection: ServerConnection) -> None:
        request_url = urlsplit(connection.request.path)
        if request_url.path != WS_PATH:
            await connection.close(code=1008, reason="invalid path")
            return
        if not self.client_is_allowed(connection.remote_address):
            await connection.close(code=4003, reason="forbidden")
            return

        trusted_client = self.client_is_trusted(connection.remote_address)
        forwarded_principal = self.internal_request_principal(connection)
        if self.internal_token and forwarded_principal is None:
            await connection.close(code=4003, reason="invalid internal credentials")
            return
        # Token-less bootstrap by Tailscale identity behind `tailscale serve`.
        auto_user = self.auto_auth_user(connection)
        user = self.owner if self.multi_tenant else ""
        device_id = ""
        passkey_binding = secrets.token_urlsafe(24)
        require_token_here = self.token_required_for(trusted_client) and auto_user is None
        auth_method = "internal" if forwarded_principal is not None else ""
        needs_enroll = False

        # A proxy-forwarded backend connection is already authenticated by the
        # internal hop. Every direct browser must answer a fresh device-key
        # challenge, even when its network address or Tailscale identity is trusted.
        if forwarded_principal is None:
            nonce = secrets.token_urlsafe(32)
            realm_hint = auto_user if self.multi_tenant and auto_user is not None else (
                "" if self.multi_tenant else "standalone"
            )
            try:
                await self.send_json(
                    connection,
                    {
                        "type": "auth-challenge",
                        "nonce": nonce,
                        "realm": realm_hint,
                        "profile": "",
                        "rpId": self.rp_id(connection),
                    },
                )
                auth_payload = await self.receive_auth_message(connection, timeout=20)
            except ConnectionClosed:
                return
            if auth_payload is None or auth_payload.get("type") != "auth":
                await self.send_json(
                    connection,
                    {"type": "auth-error", "message": "Authentication required."},
                )
                await connection.close(code=4001, reason="auth failed")
                return

            device_id = str(auth_payload.get("deviceId", ""))[:64]
            claimed_user = sanitize_user(auth_payload.get("user", "")) if self.multi_tenant else ""
            user = auto_user if self.multi_tenant and auto_user is not None else claimed_user
            protocol_realm = self.passkey_principal(user)
            if auth_payload.get("realm") != protocol_realm or auth_payload.get("profile") != "":
                await self.send_json(
                    connection,
                    {"type": "auth-error", "message": "Authentication required."},
                )
                await connection.close(code=4001, reason="auth failed")
                return

            # Only a literal JSON false permits silent device-key authorization.
            # Missing, null, strings, numbers, and true all require WebAuthn.
            require_passkey = auth_payload.get("requirePasskey") is not False
            transcript = device_authentication_transcript(
                self.rp_id(connection),
                protocol_realm,
                "",
                nonce,
            )
            spki = device_pubkey(user, device_id)
            device_key_valid = bool(
                not require_passkey
                and spki
                and verify_device_signature(
                    spki,
                    transcript,
                    str(auth_payload.get("signature", "")),
                )
            )

            if device_key_valid:
                auth_method = "device-key"
            else:
                try:
                    passkey_record, fallback_payload = await self.authenticate_passkey(
                        connection,
                        user,
                        binding=passkey_binding,
                    )
                except (PasskeyChallengeError, PasskeyVerificationError, PasskeyStoreError, ValueError) as exc:
                    print(f"passkey authentication failed: {type(exc).__name__}")
                    await self.send_json(
                        connection,
                        {"type": "auth-error", "message": "Passkey verification failed."},
                    )
                    await connection.close(code=4001, reason="auth failed")
                    return

                if passkey_record is not None:
                    auth_method = "passkey"
                elif fallback_payload is not None:
                    # Existing passkeys cannot be bypassed by submitting a token
                    # after a cancelled assertion.
                    await self.send_json(
                        connection,
                        {"type": "auth-error", "message": "Passkey authentication is required."},
                    )
                    await connection.close(code=4001, reason="auth failed")
                    return
                else:
                    # With no passkey yet, trusted identity or a valid token chooses
                    # the bootstrap principal but does not authorize the terminal.
                    if auto_user is not None:
                        token_ok = True
                        err = "Authentication required."
                    elif self.multi_tenant:
                        user_meta = self.users.get(user)
                        token_ok = user_meta is not None and (
                            not require_token_here
                            or hmac.compare_digest(
                                str(auth_payload.get("token", "")),
                                user_meta["token"],
                            )
                        )
                        err = "Invalid user or access token."
                    else:
                        token_ok = not require_token_here or (
                            self.token is not None
                            and hmac.compare_digest(
                                str(auth_payload.get("token", "")),
                                self.token,
                            )
                        )
                        err = "Invalid access token."
                    if not token_ok:
                        await self.send_json(connection, {"type": "auth-error", "message": err})
                        await connection.close(code=4001, reason="auth failed")
                        return
                    try:
                        await self.register_passkey(
                            connection,
                            user,
                            binding=passkey_binding,
                        )
                    except (PasskeyChallengeError, PasskeyVerificationError, PasskeyStoreError, ValueError) as exc:
                        print(f"passkey registration failed: {type(exc).__name__}")
                        await self.send_json(
                            connection,
                            {"type": "auth-error", "message": "Passkey verification failed."},
                        )
                        await connection.close(code=4001, reason="auth failed")
                        return
                    auth_method = "passkey-bootstrap"

                needs_enroll = bool(device_id) and device_pubkey(user, device_id) is None

        if self.multi_tenant:
            self.register_device(user, device_id, device_label(connection.request.headers.get("User-Agent", "")))

        requested_session = parse_qs(request_url.query).get("session", [""])[0].strip()
        session_name, created = self.resolve_user_session(user, requested_session)
        requested_session_missing = bool(requested_session) and requested_session != session_name
        profile_id = self.internal_request_profile(connection)

        state = {
            "session": session_name,
            "user": user,
            "principal": forwarded_principal,
            "profile": profile_id,
            "switching": False,
            "deviceId": device_id,
            # Enrollment context for the post-ready device-key exchange.
            "authMethod": auth_method,
            "needsEnroll": needs_enroll,
            "enrollment": None,
        }
        epoch_state = {"epoch": 0, "layout": 0}
        bridge = TmuxBridge(
            connection,
            session_name,
            self.shell,
            self.cwd,
            create_if_missing=created,
            initial_size=self.terminal_sizes.get(session_name),
            epoch_state=epoch_state,
            profile_id=profile_id,
            provenance_state=self.command_provenance_state(session_name),
            write_lock=self.terminal_write_lock(session_name),
        )
        await self.open_live_terminal(bridge, state)
        session_name = getattr(bridge, "session_name", state["session"])
        if pane_scrolls_locally(session_name) and pane_in_mode(session_name):
            tmux_capture("send-keys", "-t", session_name, "-X", "cancel", check=False)

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
        incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

        async def receive_messages() -> None:
            async for raw_message in connection:
                if not isinstance(raw_message, str):
                    continue
                session_summary["bytesIn"] += len(raw_message)
                try:
                    payload = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                if bridge.acknowledge(payload):
                    continue
                await incoming.put(payload)

        async def watch_tabs() -> None:
            previous = ""
            prev_local = None
            while True:
                if state.get("switching"):
                    await asyncio.sleep(0.05)
                    continue
                tabs = self.tabs_for_user(state["user"], state["session"])
                snapshot = json.dumps(tabs, sort_keys=True)
                if snapshot != previous:
                    previous = snapshot
                    await self.send_json(connection, {"type": "tabs", "tabs": tabs})
                local = pane_scrolls_locally(state["session"])
                if local != prev_local:
                    prev_local = local
                    await self.send_json(connection, {"type": "pane-scroll", "local": local})
                try:
                    current_pane = str(pane_metadata(state["session"])[0])
                except RuntimeError:
                    current_pane = bridge.pane_id
                if current_pane != bridge.pane_id:
                    await bridge.reseed("pane-change", next_pane_id=current_pane)
                bridge.pane_change.clear()
                try:
                    await asyncio.wait_for(bridge.pane_change.wait(), timeout=1)
                except asyncio.TimeoutError:
                    pass

        async def send_ready(*, initial: bool = False) -> None:
            payload: dict[str, Any] = {
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
                "principal": forwarded_principal,
            }
            if initial:
                payload["openTabs"] = self.open_tabs_for(user)
            await self.send_json(connection, payload)

        async def switch_session(target: str) -> None:
            nonlocal bridge
            new_session, new_created = self.resolve_user_session(state["user"], str(target).strip())
            if new_session == state["session"]:
                return
            state["switching"] = True
            try:
                self.invalidate_command_provenance(state["session"])
                self.invalidate_command_provenance(new_session)
                self.unregister_live_terminal(bridge.session_name, bridge)
                await bridge.close()
                session_summary["bytesOut"] += bridge.bytes_out
                bridge = TmuxBridge(
                    connection,
                    new_session,
                    self.shell,
                    self.cwd,
                    create_if_missing=new_created,
                    initial_size=self.terminal_sizes.get(new_session),
                    epoch_state=epoch_state,
                    profile_id=profile_id,
                    provenance_state=self.command_provenance_state(new_session),
                    write_lock=self.terminal_write_lock(new_session),
                )
                state["session"] = new_session
                await self.open_live_terminal(bridge, state)
                new_session = getattr(bridge, "session_name", state["session"])
                state["session"] = new_session
                await send_ready()
                if pane_scrolls_locally(new_session) and pane_in_mode(new_session):
                    tmux_capture("send-keys", "-t", new_session, "-X", "cancel", check=False)
                await bridge.reseed("session-switch")
                await self.send_tabs(connection, state["user"], new_session)
                await self.send_sessions(connection, state["user"], new_session)
                await self.send_composer_state(connection, new_session)
            finally:
                state["switching"] = False

        receive_task = asyncio.create_task(receive_messages())
        tab_task = asyncio.create_task(watch_tabs())
        try:
            await send_ready(initial=True)
            await bridge.reseed("initial")
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
            await self.maybe_enroll_device(connection, state)

            while True:
                command_task = asyncio.create_task(incoming.get())
                done, _ = await asyncio.wait((command_task, receive_task), return_when=asyncio.FIRST_COMPLETED)
                if receive_task in done:
                    command_task.cancel()
                    try:
                        await command_task
                    except asyncio.CancelledError:
                        pass
                    await receive_task
                    break
                payload = command_task.result()
                msg_type = payload.get("type")
                if msg_type == "switch-session":
                    await switch_session(payload.get("session", ""))
                    continue
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
            receive_task.cancel()
            tab_task.cancel()
            for task in (receive_task, tab_task):
                try:
                    await task
                except (asyncio.CancelledError, ConnectionClosed, Exception):
                    pass
            self.unregister_live_terminal(state["session"], bridge)
            await bridge.close()
            session_summary["bytesOut"] += bridge.bytes_out
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
    from mobile_terminal_config import ConfigError, load_runtime_config

    try:
        runtime_config = load_runtime_config()
    except ConfigError as exc:
        raise SystemExit(f"Invalid MOBILE_TERMINAL_CONFIG: {exc}") from exc
    if runtime_config is not None:
        from proxy import ProxyServer

        proxy = ProxyServer(
            runtime_config,
            static_root=STATIC_ROOT,
            node_modules_root=NODE_MODULES_ROOT,
            render_icon=render_app_icon,
        )
        try:
            asyncio.run(proxy.run())
        except KeyboardInterrupt:
            pass
        return

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
    internal_token = os.environ.get("MOBILE_TERMINAL_INTERNAL_TOKEN", "").strip() or None
    require_internal_token = os.environ.get("MOBILE_TERMINAL_REQUIRE_INTERNAL_TOKEN", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if require_internal_token and not internal_token:
        raise SystemExit("MOBILE_TERMINAL_INTERNAL_TOKEN is required for this backend")
    if internal_token and args.host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("MOBILE_TERMINAL_INTERNAL_TOKEN requires a loopback-only host")
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
        internal_token=internal_token,
    )
    try:
        asyncio.run(server.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
