#!/usr/bin/env python3
import argparse
import copy
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


SOURCE_TAG = "mobile-terminal-provider-authority"
BACKUP_SUFFIX = ".mobile-terminal-provider-hooks.bak"
CLAUDE_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "PreCompact",
    "PostCompact",
    "CwdChanged",
)
CODEX_EVENTS = (
    "SessionStart",
    "SessionEnd",
    "PreCompact",
    "PostCompact",
)
VERSION_RE = re.compile(r"(?<![0-9])([0-9]+\.[0-9]+\.[0-9]+)(?![0-9])")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, indent=2, sort_keys=True)
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


def _backup_existing(path: Path) -> None:
    try:
        payload = path.read_bytes()
    except FileNotFoundError:
        return
    backup = path.with_name(f"{path.name}{BACKUP_SUFFIX}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(backup, flags, 0o600)
    except FileExistsError:
        return
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            backup.unlink()
        except FileNotFoundError:
            pass
        raise


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_bytes())
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _entry(command: str) -> dict[str, Any]:
    return {
        "_mobile_terminal_source": SOURCE_TAG,
        "hooks": [
            {
                "type": "command",
                "command": command,
                "timeout": 2,
            }
        ],
    }


def _merge_hooks(
    document: dict[str, Any],
    events: tuple[str, ...],
    command: str,
) -> dict[str, Any]:
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be a JSON object")
    for event in events:
        configured = hooks.setdefault(event, [])
        if not isinstance(configured, list):
            raise ValueError(f"hooks.{event} must be a JSON array")
        foreign = [
            item
            for item in configured
            if not (
                isinstance(item, dict)
                and item.get("_mobile_terminal_source") == SOURCE_TAG
            )
        ]
        hooks[event] = [*foreign, _entry(command)]
    return document


def _remove_tagged_hooks(document: dict[str, Any], events: tuple[str, ...]) -> dict[str, Any]:
    hooks = document.get("hooks")
    if hooks is None:
        return document
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be a JSON object")
    for event in events:
        configured = hooks.get(event)
        if configured is None:
            continue
        if not isinstance(configured, list):
            raise ValueError(f"hooks.{event} must be a JSON array")
        foreign = [
            item
            for item in configured
            if not (
                isinstance(item, dict)
                and item.get("_mobile_terminal_source") == SOURCE_TAG
            )
        ]
        if foreign:
            hooks[event] = foreign
        else:
            hooks.pop(event)
    return document


def _install_hooks(path: Path, events: tuple[str, ...], command: str) -> None:
    document = _read_json(path)
    updated = copy.deepcopy(document)
    updated = _remove_tagged_hooks(updated, ("SubagentStart", "SubagentStop"))
    updated = _merge_hooks(updated, events, command)
    if updated == document:
        return
    _backup_existing(path)
    _atomic_json(path, updated)


def _version(binary: str) -> str | None:
    try:
        result = subprocess.run(
            [binary, "--version"],
            check=True,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    match = VERSION_RE.search(result.stdout)
    return match.group(1) if match is not None else None


def install_provider_hooks(
    home: Path,
    root: Path,
    claude_version: str | None,
    codex_version: str | None,
) -> None:
    hook = (root / "provider_binding_hook.py").resolve(strict=True)
    if hook.parent != root.resolve(strict=True):
        raise ValueError("provider hook resolves outside the installation root")
    python = shlex.quote(sys.executable)
    hook_command = shlex.quote(str(hook))
    if claude_version is not None:
        claude_command = (
            f"{python} {hook_command} --provider claude --version {shlex.quote(claude_version)}"
        )
        claude_path = home / ".claude" / "settings.json"
        _install_hooks(claude_path, CLAUDE_EVENTS, claude_command)

    if codex_version is not None:
        codex_command = (
            f"{python} {hook_command} --provider codex --version {shlex.quote(codex_version)}"
        )
        codex_path = home / ".codex" / "hooks" / "hooks.json"
        _install_hooks(codex_path, CODEX_EVENTS, codex_command)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--claude-version")
    parser.add_argument("--codex-version")
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    if not arguments.apply:
        parser.error("refusing hook installation without explicit --apply confirmation")
    install_provider_hooks(
        arguments.home.resolve(),
        arguments.root.resolve(),
        arguments.claude_version or _version("claude"),
        arguments.codex_version or _version("codex"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
