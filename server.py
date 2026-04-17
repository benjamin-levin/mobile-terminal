#!/usr/bin/env python3
import argparse
import asyncio
import fcntl
import hmac
import ipaddress
import json
import mimetypes
import os
import secrets
import signal
import struct
import subprocess
import termios
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
MOBILE_COMPOSER_HISTORY_LIMIT = 200
COMPOSER_CAPTURE_CONTEXT_ROWS = 12
LEFT_ARROW = "\u001b[D"
RIGHT_ARROW = "\u001b[C"
UP_ARROW = "\u001b[A"
DOWN_ARROW = "\u001b[B"


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
    }


def default_mobile_composer_state() -> dict[str, Any]:
    return {
        "history": [],
        "draft": "",
        "cursor": 0,
        "historyIndex": None,
        "pendingDraft": "",
        "tracked": False,
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

    move_right = max(0, len(current_value) - current_cursor)
    move_left = max(0, len(target_value) - target_cursor)
    sequence = ""
    if move_right:
        sequence += RIGHT_ARROW * move_right
    if current_value:
        sequence += "\u007f" * len(current_value)
    if target_value:
        sequence += target_value
    if move_left:
        sequence += LEFT_ARROW * move_left
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

    async def send_json(self, connection: ServerConnection, payload: dict[str, Any]) -> None:
        await connection.send(json.dumps(payload))

    def client_is_allowed(self, remote_address_value: Any) -> bool:
        if not self.allowed_clients:
            return True
        host = remote_ip(remote_address_value)
        return host in self.allowed_clients

    async def process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        path = urlsplit(request.path).path
        if not self.client_is_allowed(connection.remote_address):
            return http_response(403, b"Forbidden\n", "text/plain; charset=utf-8")
        if path == "/config":
            payload = {
                "requireToken": self.require_token,
                "tailscaleMode": self.tailscale_mode,
                "allowedClients": self.allowed_clients,
                "host": self.host,
                "port": self.port,
            }
            body = json.dumps(payload).encode("utf-8")
            return http_response(200, body, "application/json; charset=utf-8")
        return await process_request(connection, request)

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
            },
        )

    def reset_mobile_composer_tracking(self, session_name: str) -> None:
        state = self.mobile_composer_state(session_name)
        state["draft"] = ""
        state["cursor"] = 0
        state["historyIndex"] = None
        state["pendingDraft"] = ""
        state["tracked"] = False

    def sync_mobile_composer(
        self,
        bridge: TmuxBridge,
        session_name: str,
        value: str,
        cursor: Any,
        *,
        reset_history_index: bool = True,
    ) -> dict[str, Any]:
        state = self.mobile_composer_state(session_name)
        next_value = value.replace("\r", "").replace("\n", "")
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
        if reset_history_index:
            state["historyIndex"] = None
            state["pendingDraft"] = next_value
        state["tracked"] = True
        return state

    def commit_mobile_composer(self, bridge: TmuxBridge, session_name: str) -> None:
        state = self.mobile_composer_state(session_name)
        line = state["draft"]
        if line:
            history = state["history"]
            history.append(line)
            if len(history) > MOBILE_COMPOSER_HISTORY_LIMIT:
                del history[:-MOBILE_COMPOSER_HISTORY_LIMIT]
        self.reset_mobile_composer_tracking(session_name)
        if pane_in_mode(session_name):
            tmux_capture("send-keys", "-t", session_name, "-X", "cancel", check=False)
        bridge.write("\r")

    def fallback_mobile_composer_history(
        self,
        bridge: TmuxBridge,
        session_name: str,
        direction: str,
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
            reset_history_index=False,
        )
        next_state["historyIndex"] = history_index
        return next_state

    def capture_visible_mobile_composer_text(self, session_name: str) -> str | None:
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
        return best_match or None

    async def navigate_mobile_composer_history(
        self,
        bridge: TmuxBridge,
        session_name: str,
        direction: str,
    ) -> dict[str, Any] | None:
        arrow = {"up": UP_ARROW, "down": DOWN_ARROW}.get(direction)
        if not arrow:
            return None

        bridge.write(arrow)

        state = self.mobile_composer_state(session_name)
        for delay in (0.05, 0.15, 0.3):
            await asyncio.sleep(delay)
            visible_text = self.capture_visible_mobile_composer_text(session_name)
            if visible_text is None:
                continue
            state["draft"] = visible_text
            state["cursor"] = len(visible_text)
            state["tracked"] = True
            state["pendingDraft"] = visible_text
            state["historyIndex"] = None
            return state

        return self.fallback_mobile_composer_history(bridge, session_name, direction)

    async def handle_command(
        self,
        connection: ServerConnection,
        bridge: TmuxBridge,
        state: dict[str, str],
        payload: dict[str, Any],
    ) -> None:
        session_name = state["session"]
        message_type = payload.get("type")
        if message_type == "composer-sync":
            self.sync_mobile_composer(
                bridge,
                session_name,
                str(payload.get("value", "")),
                payload.get("cursor"),
            )
            return

        if message_type == "composer-enter":
            self.commit_mobile_composer(bridge, session_name)
            await self.send_composer_state(connection, session_name)
            return

        if message_type == "composer-history":
            next_state = await self.navigate_mobile_composer_history(
                bridge,
                session_name,
                str(payload.get("direction", "")).lower(),
            )
            if next_state is not None:
                await self.send_composer_state(connection, session_name)
            return

        if message_type == "composer-reset":
            self.reset_mobile_composer_tracking(session_name)
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

        if message_type == "save-settings":
            self.settings = save_settings(payload.get("settings", {}))
            await self.send_settings(connection)
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
        history = capture_history(state["session"])

        async def relay_output() -> None:
            while True:
                chunk = await bridge.read()
                if not chunk:
                    break
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
            if history:
                await connection.send(history.encode("utf-8", "surrogateescape"))
            await self.send_json(
                connection,
                {
                    "type": "ready",
                    "session": state["session"],
                    "shell": self.shell,
                    "cwd": self.cwd,
                    "requireToken": self.require_token,
                    "tailscaleMode": self.tailscale_mode,
                    "allowedClients": self.allowed_clients,
                },
            )
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
                try:
                    payload = json.loads(raw_message)
                except json.JSONDecodeError:
                    continue
                await self.handle_command(connection, bridge, state, payload)
        except ConnectionClosed:
            pass
        finally:
            output_task.cancel()
            tab_task.cancel()
            bridge.close()

    async def run(self) -> None:
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
            await asyncio.Future()


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
