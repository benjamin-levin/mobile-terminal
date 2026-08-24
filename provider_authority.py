import atexit
import html
import json
import os
import re
import stat
import threading
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import regex
from wcwidth import wcwidth


STALE_MESSAGE = "Terminal changed; select again."
DEFAULT_MAX_RECORD_BYTES = 256 * 1024
DEFAULT_MAX_SCAN_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_IRRELEVANT_RECORD_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_INDEX_RECORDS = 128
MAX_TRANSCRIPT_INDEXES = 32
MAX_PROVIDER_DIAGNOSTIC_REASONS = 64
PROVIDER_DIAGNOSTIC_FLUSH_EVERY = 128
UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-57][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
PANE_RE = re.compile(r"^%[0-9]+$")
GRAPHEME_RE = regex.compile(r"\X")
REGIONAL_INDICATOR_RE = regex.compile(r"\A\p{Regional_Indicator}\Z")
EXTENDED_PICTOGRAPHIC_RE = regex.compile(r"\p{Extended_Pictographic}")
EMOJI_MODIFIER_RE = regex.compile(r"\p{Emoji_Modifier}")
HTML_ENTITY_RE = re.compile(
    r"&(?:#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);"
)


def _contains_html_entity_syntax(value: str) -> bool:
    return HTML_ENTITY_RE.search(value) is not None or html.unescape(value) != value


class ProviderAuthorityError(RuntimeError):
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"provider authority unavailable ({reason})")


@dataclass(frozen=True)
class ProviderBinding:
    provider: str
    pane_id: str
    pid: int
    proc_start: str
    session_id: str
    transcript_id: str
    transcript_path: Path
    version: str
    generation: int = 0


@dataclass(frozen=True)
class TranscriptFence:
    provider: str
    root: Path
    path: Path
    realpath: Path
    fd: int
    device: int
    inode: int
    size: int
    mtime_ns: int
    ctime_ns: int
    complete_size: int


@dataclass(frozen=True)
class SemanticToken:
    kind: str
    source_start: int
    source_end: int
    source: str
    render_text: str
    copy_text: str
    semantic: str = ""
    style: str = "plain"


@dataclass(frozen=True)
class RenderedCell:
    row: int
    column: int
    text: str
    width: int
    copy_start: int | None
    copy_end: int | None
    style: str = "plain"
    presentation: bool = False


@dataclass(frozen=True)
class BoundaryProvenance:
    kind: str
    copy_start: int
    copy_end: int
    source_start: int
    source_end: int
    anchor_row: int | None = None
    anchor_column: int | None = None


@dataclass(frozen=True)
class RenderCandidate:
    provider: str
    version: str
    record_id: str
    source_id: str
    source_text: str
    copy_text: str
    plain_rows: tuple[str, ...]
    style_rows: tuple[tuple[tuple[int, int, str], ...], ...]
    cells: tuple[RenderedCell, ...]
    boundaries: tuple[BoundaryProvenance, ...]
    selection_start: tuple[int, int]
    selection_end: tuple[int, int]
    unsupported: bool = False
    source_start: int = 0
    source_end: int = 0


@dataclass(frozen=True)
class OwnershipRange:
    provider: str
    pane_id: str
    start_row: int
    end_row: int
    binding_generation: int
    record_id: str = ""


@dataclass(frozen=True)
class MatchResult:
    matched: bool
    text: str | None = None
    provider: str | None = None
    record_id: str | None = None
    placement_row: int | None = None
    source_start: int | None = None
    source_end: int | None = None
    public_error: str | None = None
    internal_reason: str | None = None

    @classmethod
    def failure(cls, reason: str) -> "MatchResult":
        return cls(False, public_error=STALE_MESSAGE, internal_reason=reason)


@dataclass(frozen=True)
class AssistantTextRecord:
    provider: str
    record_id: str
    source_id: str
    text: str
    ordinal: int
    unsupported: bool = False


@dataclass(frozen=True)
class RendererProfile:
    provider: str
    version: str
    first_gutter: str
    continuation_gutter: str
    unordered_marker: str
    ordered_marker: str
    text_style: str = "assistant"
    marker_style: str = "list-marker"
    first_gutter_styles: tuple[str | None, ...] = ()
    source_newlines_are_hard: bool = True


CLAUDE_PROFILES = {
    "2.1.241": RendererProfile(
        "claude",
        "2.1.241",
        "● ",
        "  ",
        "- ",
        "{number}. ",
        first_gutter_styles=("assistant-dot", "assistant"),
    ),
}
CODEX_PROFILES = {
    "0.147.0": RendererProfile("codex", "0.147.0", "• ", "  ", "• ", "{number}. "),
}


def renderer_profile(provider: str, version: str) -> RendererProfile:
    profiles = CLAUDE_PROFILES if provider == "claude" else CODEX_PROFILES if provider == "codex" else {}
    try:
        return profiles[version]
    except KeyError as exc:
        raise ProviderAuthorityError("unsupported-provider-version") from exc


def _read_proc_start(pid: int) -> str:
    try:
        value = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ProviderAuthorityError("process-unavailable") from exc
    close = value.rfind(")")
    fields = value[close + 2 :].split() if close >= 0 else []
    if len(fields) <= 19:
        raise ProviderAuthorityError("invalid-process-stat")
    return fields[19]


def _read_proc_environ(pid: int) -> Mapping[str, str]:
    try:
        raw = Path(f"/proc/{pid}/environ").read_bytes()
    except OSError as exc:
        raise ProviderAuthorityError("process-unavailable") from exc
    result = {}
    for item in raw.split(b"\0"):
        if b"=" not in item:
            continue
        key, value = item.split(b"=", 1)
        try:
            result[key.decode("ascii")] = value.decode("utf-8")
        except UnicodeError:
            continue
    return result


def _registry_pane(value: Any) -> str | None:
    if not isinstance(value, str) or "." not in value:
        return None
    pane = value.rsplit(".", 1)[1]
    return pane if PANE_RE.fullmatch(pane) else None


def _default_transcript_paths(root: Path, transcript_id: str) -> tuple[Path, ...]:
    return tuple(root.rglob(f"{transcript_id}.jsonl"))


def bind_claude_pane(
    pane_id: str,
    *,
    sessions_root: Path | str | None = None,
    transcript_root: Path | str | None = None,
    expected_pid: int | None = None,
    expected_session_id: str | None = None,
    proc_start_reader: Callable[[int], str] = _read_proc_start,
    proc_environ_reader: Callable[[int], Mapping[str, str]] = _read_proc_environ,
    transcript_paths: Callable[[Path, str], Iterable[Path]] = _default_transcript_paths,
) -> ProviderBinding:
    if not PANE_RE.fullmatch(pane_id):
        raise ProviderAuthorityError("invalid-pane")
    home = Path.home()
    registry_root = Path(sessions_root) if sessions_root is not None else home / ".claude" / "sessions"
    approved_root = Path(transcript_root) if transcript_root is not None else home / ".claude" / "projects"
    matches: list[ProviderBinding] = []
    try:
        registry_files = tuple(registry_root.glob("*.json"))
    except OSError as exc:
        raise ProviderAuthorityError("registry-unavailable") from exc
    for registry_path in registry_files:
        try:
            pid = int(registry_path.stem)
            data = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict) or data.get("pid") != pid or _registry_pane(data.get("tmux")) != pane_id:
            continue
        session_id = data.get("sessionId")
        proc_start = data.get("procStart")
        version = data.get("version")
        if expected_pid is not None and pid != expected_pid:
            continue
        if expected_session_id is not None and session_id != expected_session_id:
            continue
        if not all(isinstance(value, str) and value for value in (session_id, proc_start, version)):
            continue
        if not UUID_RE.fullmatch(session_id):
            continue
        try:
            if proc_start_reader(pid) != proc_start:
                continue
            if proc_environ_reader(pid).get("TMUX_PANE") != pane_id:
                continue
            paths = tuple(transcript_paths(approved_root, session_id))
        except (OSError, ProviderAuthorityError):
            continue
        if len(paths) != 1 or paths[0].name != f"{session_id}.jsonl":
            continue
        matches.append(
            ProviderBinding(
                provider="claude",
                pane_id=pane_id,
                pid=pid,
                proc_start=proc_start,
                session_id=session_id,
                transcript_id=session_id,
                transcript_path=paths[0],
                version=version,
            )
        )
    if len(matches) != 1:
        raise ProviderAuthorityError("ambiguous-claude-binding" if matches else "claude-binding-unavailable")
    return matches[0]


def _event_string(event: Mapping[str, Any], key: str) -> str:
    value = event.get(key)
    if not isinstance(value, str) or not value:
        raise ProviderAuthorityError("invalid-codex-binding-event")
    return value


def bind_codex_pane(
    event: Mapping[str, Any],
    *,
    transcript_root: Path | str | None = None,
    proc_start_reader: Callable[[int], str] = _read_proc_start,
    proc_environ_reader: Callable[[int], Mapping[str, str]] = _read_proc_environ,
) -> ProviderBinding:
    if event.get("provider") != "codex":
        raise ProviderAuthorityError("invalid-codex-binding-event")
    pane_id = _event_string(event, "pane")
    session_id = _event_string(event, "sessionId")
    proc_start = _event_string(event, "procStart")
    version = _event_string(event, "version")
    path = Path(_event_string(event, "rolloutPath"))
    try:
        pid = int(event.get("pid"))
        generation = int(event.get("generation", 0))
    except (TypeError, ValueError) as exc:
        raise ProviderAuthorityError("invalid-codex-binding-event") from exc
    if not PANE_RE.fullmatch(pane_id) or not UUID_RE.fullmatch(session_id) or generation < 0:
        raise ProviderAuthorityError("invalid-codex-binding-event")
    if proc_start_reader(pid) != proc_start or proc_environ_reader(pid).get("TMUX_PANE") != pane_id:
        raise ProviderAuthorityError("codex-process-mismatch")
    approved_root = Path(transcript_root) if transcript_root is not None else Path.home() / ".codex" / "sessions"
    try:
        path.relative_to(approved_root)
    except ValueError as exc:
        raise ProviderAuthorityError("transcript-outside-root") from exc
    return ProviderBinding(
        provider="codex",
        pane_id=pane_id,
        pid=pid,
        proc_start=proc_start,
        session_id=session_id,
        transcript_id=session_id,
        transcript_path=path,
        version=version,
        generation=generation,
    )


def _relative_transcript_path(root: Path, path: Path) -> tuple[str, ...]:
    root = root.absolute()
    path = path.absolute()
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ProviderAuthorityError("transcript-outside-root") from exc
    if not relative.parts or any(part in ("", ".", "..") for part in relative.parts):
        raise ProviderAuthorityError("unsafe-transcript-path")
    return relative.parts


def open_transcript_fence(
    binding: ProviderBinding,
    *,
    root: Path | str,
    owner_uid: int | None = None,
) -> TranscriptFence:
    approved_root = Path(root)
    parts = _relative_transcript_path(approved_root, binding.transcript_path)
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        descriptors.append(os.open(approved_root, directory_flags))
        for part in parts[:-1]:
            descriptors.append(os.open(part, directory_flags, dir_fd=descriptors[-1]))
        fd = os.open(parts[-1], file_flags, dir_fd=descriptors[-1])
        file_stat = os.fstat(fd)
    except OSError as exc:
        raise ProviderAuthorityError("unsafe-transcript-open") from exc
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    expected_uid = os.geteuid() if owner_uid is None else owner_uid
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_uid != expected_uid:
        os.close(fd)
        raise ProviderAuthorityError("unsafe-transcript-metadata")
    if file_stat.st_size:
        try:
            final_byte = os.pread(fd, 1, file_stat.st_size - 1)
        except OSError as exc:
            os.close(fd)
            raise ProviderAuthorityError("transcript-read-failed") from exc
        if final_byte != b"\n":
            os.close(fd)
            raise ProviderAuthorityError("partial-jsonl-record")
    realpath = Path(os.readlink(f"/proc/self/fd/{fd}"))
    return TranscriptFence(
        provider=binding.provider,
        root=approved_root.absolute(),
        path=binding.transcript_path.absolute(),
        realpath=realpath,
        fd=fd,
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
        ctime_ns=file_stat.st_ctime_ns,
        complete_size=file_stat.st_size,
    )


def close_transcript_fence(fence: TranscriptFence) -> None:
    try:
        os.close(fence.fd)
    except OSError:
        pass


def revalidate_transcript_fence(fence: TranscriptFence) -> None:
    try:
        current = os.fstat(fence.fd)
        path_stat = os.stat(fence.path, follow_symlinks=False)
    except OSError as exc:
        raise ProviderAuthorityError("transcript-changed") from exc
    identity = (
        current.st_dev,
        current.st_ino,
        current.st_size,
        current.st_mtime_ns,
        current.st_ctime_ns,
    )
    expected = (fence.device, fence.inode, fence.size, fence.mtime_ns, fence.ctime_ns)
    if identity != expected or not stat.S_ISREG(path_stat.st_mode) or (path_stat.st_dev, path_stat.st_ino) != (fence.device, fence.inode):
        raise ProviderAuthorityError("transcript-changed")


def validate_binding_process(
    binding: ProviderBinding,
    *,
    proc_start_reader: Callable[[int], str] = _read_proc_start,
    proc_environ_reader: Callable[[int], Mapping[str, str]] = _read_proc_environ,
) -> None:
    if proc_start_reader(binding.pid) != binding.proc_start:
        raise ProviderAuthorityError("process-start-mismatch")
    if proc_environ_reader(binding.pid).get("TMUX_PANE") != binding.pane_id:
        raise ProviderAuthorityError("process-pane-mismatch")


class TranscriptIndex:
    def __init__(
        self,
        *,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
        max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES,
        max_irrelevant_record_bytes: int = DEFAULT_MAX_IRRELEVANT_RECORD_BYTES,
        max_records: int = DEFAULT_MAX_INDEX_RECORDS,
    ):
        if min(max_record_bytes, max_scan_bytes, max_irrelevant_record_bytes, max_records) <= 0:
            raise ValueError("transcript index limits must be positive")
        if max_irrelevant_record_bytes < max_record_bytes:
            raise ValueError("irrelevant record limit must cover assistant record limit")
        self.max_record_bytes = max_record_bytes
        self.max_scan_bytes = max_scan_bytes
        self.max_irrelevant_record_bytes = max_irrelevant_record_bytes
        self.records: deque[AssistantTextRecord] = deque(maxlen=max_records)
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._ordinal = 0
        self._session_id: str | None = None
        self._provider: str | None = None
        self._claude_groups: dict[str, AssistantTextRecord] = {}
        self._codex_thread_id: str | None = None

    @property
    def offset(self) -> int:
        return self._offset

    def _bootstrap_offset(self, fence: TranscriptFence) -> int:
        if fence.complete_size <= self.max_scan_bytes:
            return 0
        target = fence.complete_size - self.max_scan_bytes
        search_start = max(0, target - self.max_irrelevant_record_bytes)
        try:
            prefix = os.pread(fence.fd, target - search_start, search_start)
        except OSError as exc:
            raise ProviderAuthorityError("transcript-read-failed") from exc
        if len(prefix) != target - search_start:
            raise ProviderAuthorityError("transcript-changed")
        boundary = prefix.rfind(b"\n")
        if boundary < 0:
            if search_start:
                raise ProviderAuthorityError("transcript-bootstrap-limit")
            return 0
        return search_start + boundary + 1

    def _read_range(self, fence: TranscriptFence, start: int) -> bytes:
        length = fence.complete_size - start
        if length > self.max_scan_bytes + self.max_irrelevant_record_bytes:
            raise ProviderAuthorityError("transcript-scan-limit")
        try:
            raw = os.pread(fence.fd, length, start)
        except OSError as exc:
            raise ProviderAuthorityError("transcript-read-failed") from exc
        if len(raw) != length or (raw and not raw.endswith(b"\n")):
            raise ProviderAuthorityError("transcript-changed")
        return raw

    @staticmethod
    def _is_assistant_record(data: Mapping[str, Any], provider: str) -> bool:
        if provider == "claude":
            return data.get("type") == "assistant"
        if provider != "codex" or data.get("type") != "response_item":
            return False
        payload = data.get("payload")
        return isinstance(payload, dict) and payload.get("type") == "message" and payload.get("role") == "assistant"

    def _bootstrap_codex_metadata(self, fence: TranscriptFence, binding: ProviderBinding) -> None:
        limit = min(fence.complete_size, self.max_irrelevant_record_bytes)
        try:
            prefix = os.pread(fence.fd, limit, 0)
        except OSError as exc:
            raise ProviderAuthorityError("transcript-read-failed") from exc
        newline = prefix.find(b"\n")
        if newline < 0:
            raise ProviderAuthorityError("missing-codex-session-metadata")
        try:
            data = json.loads(prefix[:newline].decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ProviderAuthorityError("invalid-jsonl-record") from exc
        if not isinstance(data, dict) or data.get("type") != "session_meta":
            raise ProviderAuthorityError("missing-codex-session-metadata")
        self._parse_codex(data, binding, 0)

    def update(self, fence: TranscriptFence, binding: ProviderBinding) -> tuple[AssistantTextRecord, ...]:
        state = (
            deque(self.records, maxlen=self.records.maxlen),
            self._identity,
            self._offset,
            self._ordinal,
            self._session_id,
            self._provider,
            dict(self._claude_groups),
            self._codex_thread_id,
        )
        try:
            if fence.provider != binding.provider:
                raise ProviderAuthorityError("provider-mismatch")
            identity = (fence.device, fence.inode)
            bootstrap = self._identity is None
            if bootstrap:
                self._identity = identity
                self._session_id = binding.session_id
                self._provider = binding.provider
                self._offset = self._bootstrap_offset(fence)
                if binding.provider == "codex" and self._offset:
                    self._bootstrap_codex_metadata(fence, binding)
            elif identity != self._identity:
                raise ProviderAuthorityError("transcript-rotated")
            elif binding.session_id != self._session_id or binding.provider != self._provider:
                raise ProviderAuthorityError("transcript-session-changed")
            if fence.complete_size < self._offset:
                raise ProviderAuthorityError("transcript-truncated")
            raw = self._read_range(fence, self._offset)
            cursor = self._offset
            bootstrap_boundary_seen = not bootstrap or self._offset == 0 or binding.provider == "codex"
            for line in raw.splitlines(keepends=True):
                if not line.endswith(b"\n"):
                    raise ProviderAuthorityError("partial-jsonl-record")
                if len(line) > self.max_irrelevant_record_bytes:
                    raise ProviderAuthorityError("oversized-jsonl-record")
                try:
                    data = json.loads(line[:-1].decode("utf-8"))
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise ProviderAuthorityError("invalid-jsonl-record") from exc
                if not isinstance(data, dict):
                    raise ProviderAuthorityError("schema-drift")
                assistant = self._is_assistant_record(data, binding.provider)
                if assistant and len(line) > self.max_record_bytes:
                    raise ProviderAuthorityError("oversized-assistant-record")
                if binding.provider == "claude" and data.get("type") == "user":
                    bootstrap_boundary_seen = True
                self._ordinal += 1
                parsed = self._parse_record(data, binding, cursor)
                for record in parsed:
                    if bootstrap and not bootstrap_boundary_seen:
                        record = replace(record, unsupported=True)
                    self._remember(record)
                cursor += len(line)
            self._offset = cursor
            revalidate_transcript_fence(fence)
        except Exception:
            (
                self.records,
                self._identity,
                self._offset,
                self._ordinal,
                self._session_id,
                self._provider,
                self._claude_groups,
                self._codex_thread_id,
            ) = state
            raise
        return tuple(self.records)

    def _remember(self, record: AssistantTextRecord) -> None:
        if self.records.maxlen is not None and len(self.records) == self.records.maxlen:
            evicted = self.records[0]
            if evicted.provider == "claude":
                self._claude_groups.pop(evicted.record_id, None)
        if record.provider == "claude":
            self._claude_groups[record.record_id] = record
        self.records.append(record)

    def _parse_record(
        self,
        data: Mapping[str, Any],
        binding: ProviderBinding,
        ordinal: int,
    ) -> tuple[AssistantTextRecord, ...]:
        if binding.provider == "claude":
            return self._parse_claude(data, binding, ordinal)
        if binding.provider == "codex":
            return self._parse_codex(data, binding, ordinal)
        raise ProviderAuthorityError("unsupported-provider")

    def _parse_claude(
        self,
        data: Mapping[str, Any],
        binding: ProviderBinding,
        ordinal: int,
    ) -> tuple[AssistantTextRecord, ...]:
        record_type = data.get("type")
        if record_type != "assistant":
            return ()
        if data.get("sessionId") != binding.session_id or not isinstance(data.get("uuid"), str):
            raise ProviderAuthorityError("schema-drift")
        message = data.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant" or message.get("type") != "message":
            raise ProviderAuthorityError("schema-drift")
        message_id = message.get("id")
        content = message.get("content")
        if not isinstance(message_id, str) or not message_id or not isinstance(content, list):
            raise ProviderAuthorityError("schema-drift")
        text_blocks: list[tuple[int, str]] = []
        unsupported = False
        for block_index, block in enumerate(content):
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise ProviderAuthorityError("schema-drift")
            if block["type"] == "text":
                if set(block) != {"type", "text"} or not isinstance(block.get("text"), str):
                    raise ProviderAuthorityError("schema-drift")
                text_blocks.append((block_index, block["text"]))
            elif block["type"] == "thinking":
                if not isinstance(block.get("thinking"), str):
                    raise ProviderAuthorityError("schema-drift")
            else:
                unsupported = True
        return tuple(
            AssistantTextRecord(
                "claude",
                f"claude:{binding.session_id}:{message_id}:{data['uuid']}:{block_index}:text",
                f"claude:{binding.session_id}:{message_id}:{data['uuid']}:{block_index}:text",
                text,
                ordinal,
                unsupported,
            )
            for block_index, text in text_blocks
        )

    def _parse_codex(
        self,
        data: Mapping[str, Any],
        binding: ProviderBinding,
        ordinal: int,
    ) -> tuple[AssistantTextRecord, ...]:
        if data.get("type") not in ("session_meta", "response_item", "event_msg", "turn_context"):
            return ()
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise ProviderAuthorityError("schema-drift")
        if data["type"] == "session_meta":
            thread_id = payload.get("id")
            session_id = payload.get("session_id")
            parent_thread_id = payload.get("parent_thread_id")
            if (
                not isinstance(thread_id, str)
                or thread_id != binding.session_id
                or (session_id is not None and not isinstance(session_id, str))
                or (parent_thread_id is not None and not isinstance(parent_thread_id, str))
            ):
                raise ProviderAuthorityError("transcript-session-mismatch")
            self._codex_thread_id = thread_id
            return ()
        if not isinstance(payload.get("type"), str):
            raise ProviderAuthorityError("schema-drift")
        if payload.get("type") != "message" or payload.get("role") != "assistant":
            return ()
        if self._codex_thread_id is None:
            raise ProviderAuthorityError("missing-codex-session-metadata")
        item_id = payload.get("id")
        content = payload.get("content")
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        turn_id = metadata.get("turn_id") if isinstance(metadata, dict) else None
        if not all(isinstance(value, str) and value for value in (item_id, turn_id)) or not isinstance(content, list):
            raise ProviderAuthorityError("schema-drift")
        text_parts = []
        unsupported = False
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise ProviderAuthorityError("schema-drift")
            if block["type"] == "output_text":
                if set(block) != {"type", "text"} or not isinstance(block.get("text"), str):
                    raise ProviderAuthorityError("schema-drift")
                text_parts.append(block["text"])
            else:
                unsupported = True
        if not text_parts:
            return ()
        record_id = f"codex:{self._codex_thread_id}:{ordinal}:{item_id}"
        source_id = f"{record_id}:{turn_id}"
        return (AssistantTextRecord("codex", record_id, source_id, "".join(text_parts), ordinal, unsupported),)


class _SemanticLexer:
    def __init__(self, source: str, *, source_byte_offset: int = 0):
        self.source = source
        self.source_byte_offset = source_byte_offset
        self.byte_offsets = [source_byte_offset]
        for character in source:
            self.byte_offsets.append(self.byte_offsets[-1] + len(character.encode("utf-8")))
        self.tokens: list[SemanticToken] = []

    def emit(
        self,
        kind: str,
        start: int,
        end: int,
        *,
        render_text: str | None = None,
        copy_text: str | None = None,
        semantic: str = "",
        style: str = "plain",
    ) -> None:
        value = self.source[start:end]
        self.tokens.append(
            SemanticToken(
                kind,
                self.byte_offsets[start],
                self.byte_offsets[end],
                value,
                value if render_text is None else render_text,
                value if copy_text is None else copy_text,
                semantic,
                style,
            )
        )

    def lex(self) -> tuple[SemanticToken, ...]:
        lines = self.source.splitlines(keepends=True)
        if not lines and self.source == "":
            return ()
        offset = 0
        for line in lines:
            body = line[:-1] if line.endswith("\n") else line
            newline = line.endswith("\n")
            if "\r" in line or "\x00" in line:
                raise ProviderAuthorityError("unsupported-source-control")
            if _contains_html_entity_syntax(body):
                raise ProviderAuthorityError("unsupported-html-entity")
            fence_match = re.fullmatch(r"(```|~~~)([A-Za-z0-9_+.-]*)", body)
            if fence_match:
                raise ProviderAuthorityError("unsupported-code-fence")
            if re.match(r"^\s*(?:>|[-*_]{3,}|\[[ xX]\]|\|)", body):
                raise ProviderAuthorityError("unsupported-markdown")
            if body.startswith("#") and not re.match(r"^#{1,6} ", body):
                raise ProviderAuthorityError("unsupported-markdown")
            if re.match(r"^(?:[-+*]|[0-9]+[.)]) \[[ xX]\](?: |$)", body):
                raise ProviderAuthorityError("unsupported-widget")
            if re.search(r"!?(?:\[[^\]]*\]\([^)]*\)|<[^>]+>)", body):
                raise ProviderAuthorityError("unsupported-markdown")
            if re.search(r"\|?\s*:?-{3,}:?\s*\|", body):
                raise ProviderAuthorityError("unsupported-table")
            prefix_end = 0
            heading = re.match(r"^(#{1,6}) (.*)$", body)
            list_match = re.match(r"^([-+*]) (.*)$", body)
            ordered = re.match(r"^([0-9]+)([.)]) (.*)$", body)
            if heading:
                prefix_end = len(heading.group(1)) + 1
                self.emit(
                    "consumed_marker",
                    offset,
                    offset + prefix_end,
                    render_text="",
                    copy_text="",
                    semantic=f"heading:{len(heading.group(1))}",
                    style="heading",
                )
            elif list_match:
                prefix_end = 2
                self.emit("semantic_replacement", offset, offset + prefix_end, render_text="", copy_text="", semantic="unordered-list", style="list-marker")
            elif ordered:
                prefix_end = len(ordered.group(1)) + 2
                self.emit(
                    "semantic_replacement",
                    offset,
                    offset + prefix_end,
                    render_text="",
                    copy_text="",
                    semantic=f"ordered-list:{ordered.group(1)}",
                    style="list-marker",
                )
            elif re.match(r"^[ \t]+(?:[-+*]|[0-9]+[.)]) ", body):
                raise ProviderAuthorityError("unsupported-nested-list")
            self._lex_inline(offset + prefix_end, offset + len(body), "heading" if heading else "plain")
            if newline:
                self.emit("source_hard_break", offset + len(body), offset + len(line), render_text="\n", copy_text="\n")
            offset += len(line)
        self._validate_coverage()
        return tuple(self.tokens)

    def _lex_inline(self, start: int, end: int, base_style: str) -> None:
        cursor = start
        visible_start = cursor

        def flush(position: int, style: str = base_style) -> None:
            nonlocal visible_start
            if position > visible_start:
                self.emit("authored_visible", visible_start, position, style=style)
            visible_start = position

        active: tuple[str, str] | None = None
        while cursor < end:
            character = self.source[cursor]
            if character == "\\":
                if cursor + 1 >= end or self.source[cursor + 1] not in r"\\`*_{}[]()#+-.!|>":
                    raise ProviderAuthorityError("unsupported-markdown-escape")
                flush(cursor)
                self.emit("consumed_marker", cursor, cursor + 1, render_text="", copy_text="", semantic="escape")
                self.emit("authored_visible", cursor + 1, cursor + 2, style=base_style)
                cursor += 2
                visible_start = cursor
                continue
            if character == "`":
                raise ProviderAuthorityError("unsupported-inline-code")
            marker = None
            for candidate, candidate_style in (("**", "strong"), ("__", "strong"), ("*", "emphasis"), ("_", "emphasis")):
                if self.source.startswith(candidate, cursor):
                    marker, style = candidate, candidate_style
                    break
            if marker is None:
                cursor += 1
                continue
            flush(cursor, active[1] if active else base_style)
            if active is None:
                closing = self.source.find(marker, cursor + len(marker), end)
                if closing <= cursor + len(marker):
                    raise ProviderAuthorityError("unsupported-markdown")
                active = (marker, style)
            elif active[0] == marker:
                active = None
            else:
                raise ProviderAuthorityError("unsupported-nested-markdown")
            self.emit("consumed_marker", cursor, cursor + len(marker), render_text="", copy_text="", semantic=style, style=style)
            cursor += len(marker)
            visible_start = cursor
        flush(end, active[1] if active else base_style)
        if active is not None:
            raise ProviderAuthorityError("unsupported-markdown")

    def _validate_coverage(self) -> None:
        expected = self.source_byte_offset
        for token in self.tokens:
            if token.source_start != expected or token.source_end <= token.source_start:
                raise ProviderAuthorityError("source-byte-coverage")
            expected = token.source_end
        if expected != self.source_byte_offset + len(self.source.encode("utf-8")):
            raise ProviderAuthorityError("source-byte-coverage")


def lex_semantic_source(source: str, *, source_byte_offset: int = 0) -> tuple[SemanticToken, ...]:
    if not isinstance(source, str):
        raise TypeError("source must be text")
    if source_byte_offset < 0:
        raise ValueError("source byte offset must be non-negative")
    return _SemanticLexer(source, source_byte_offset=source_byte_offset).lex()


def _grapheme_width(value: str) -> int:
    widths = [wcwidth(character) for character in value]
    if not widths or widths[0] <= 0 or any(width < 0 for width in widths):
        raise ProviderAuthorityError("unsupported-grapheme-width")
    width = sum(widths)
    if width not in (1, 2):
        raise ProviderAuthorityError("unsupported-grapheme-width")
    return width


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


def _captured_grapheme_width(grapheme: str) -> int:
    if grapheme == "♥︎":
        return 1
    if all(REGIONAL_INDICATOR_RE.fullmatch(character) for character in grapheme):
        if len(grapheme) == 2:
            return 2
        raise ProviderAuthorityError("unsafe-captured-wide-cell")
    if any(REGIONAL_INDICATOR_RE.fullmatch(character) for character in grapheme):
        raise ProviderAuthorityError("unsafe-captured-wide-cell")
    if (
        "‍" in grapheme
        or "️" in grapheme
        or "⃣" in grapheme
        or any(0xFE00 <= ord(character) <= 0xFE0F for character in grapheme)
        or any(0xE0100 <= ord(character) <= 0xE01EF for character in grapheme)
        or EXTENDED_PICTOGRAPHIC_RE.search(grapheme)
        or EMOJI_MODIFIER_RE.search(grapheme)
    ):
        raise ProviderAuthorityError("unsafe-captured-wide-cell")
    widths = [wcwidth(character) for character in grapheme]
    if (
        not widths
        or widths[0] <= 0
        or unicodedata.category(grapheme[0])[0] in "CM"
        or any(
            width != 0 or unicodedata.category(character)[0] != "M"
            for character, width in zip(grapheme[1:], widths[1:])
        )
    ):
        raise ProviderAuthorityError("invalid-captured-cell")
    width = widths[0]
    if width == 2 and not _is_verified_cjk_base(grapheme[0]):
        raise ProviderAuthorityError("unsafe-captured-wide-cell")
    if width not in (1, 2):
        raise ProviderAuthorityError("invalid-captured-cell")
    return width


@dataclass(frozen=True)
class _RenderUnit:
    grapheme: str
    copy_start: int
    copy_end: int
    source_start: int
    source_end: int
    style: str


@dataclass
class _CompiledLine:
    units: list[_RenderUnit]
    marker: tuple[str, int, int, str] | None = None
    heading_level: int | None = None
    hard_break: BoundaryProvenance | None = None


def _compile_render_lines(
    tokens: Sequence[SemanticToken],
    profile: RendererProfile,
) -> tuple[str, list[_CompiledLine], list[BoundaryProvenance]]:
    copy_parts: list[str] = []
    lines = [_CompiledLine([])]
    boundaries: list[BoundaryProvenance] = []
    copy_offset = 0
    for token in tokens:
        style = token.style
        if style == "plain":
            style = profile.text_style
        elif style == "list-marker":
            style = profile.marker_style
        if token.semantic.startswith("heading:"):
            try:
                lines[-1].heading_level = int(token.semantic.split(":", 1)[1])
            except ValueError as exc:
                raise ProviderAuthorityError("unsupported-heading") from exc
        if token.kind == "source_hard_break":
            start = copy_offset
            copy_parts.append(token.copy_text)
            copy_offset += len(token.copy_text)
            boundary = BoundaryProvenance(
                "hard-break",
                start,
                copy_offset,
                token.source_start,
                token.source_end,
            )
            lines[-1].hard_break = boundary
            boundaries.append(boundary)
            lines.append(_CompiledLine([]))
            continue
        if token.kind == "semantic_replacement":
            if lines[-1].units or lines[-1].marker is not None:
                raise ProviderAuthorityError("unsupported-list-layout")
            if token.semantic == "unordered-list":
                replacement = profile.unordered_marker
            elif token.semantic.startswith("ordered-list:"):
                number = token.semantic.split(":", 1)[1]
                replacement = profile.ordered_marker.format(number=number)
            else:
                raise ProviderAuthorityError("unknown-semantic-replacement")
            start = copy_offset
            copy_parts.append(replacement)
            copy_offset += len(replacement)
            marker = replacement.rstrip(" ")
            if not marker or replacement != f"{marker} ":
                raise ProviderAuthorityError("unsupported-list-marker")
            lines[-1].marker = (marker, start, copy_offset, style)
            continue
        if not token.render_text and not token.copy_text:
            continue
        if token.render_text != token.copy_text:
            raise ProviderAuthorityError("unmapped-semantic-text")
        copy_parts.append(token.copy_text)
        source_offset = token.source_start
        for grapheme in GRAPHEME_RE.findall(token.render_text):
            copy_end = copy_offset + len(grapheme)
            source_end = source_offset + len(grapheme.encode("utf-8"))
            lines[-1].units.append(
                _RenderUnit(
                    grapheme,
                    copy_offset,
                    copy_end,
                    source_offset,
                    source_end,
                    style,
                )
            )
            copy_offset = copy_end
            source_offset = source_end
    return "".join(copy_parts), lines, boundaries


def _unit_width(grapheme: str) -> int:
    if grapheme == "\t":
        raise ProviderAuthorityError("unsupported-wrap-tab")
    width = _grapheme_width(grapheme)
    if width != 1:
        raise ProviderAuthorityError("unsupported-wrap-width")
    return width


def render_semantic_candidate(
    record: AssistantTextRecord,
    *,
    version: str,
    cols: int,
    profile: RendererProfile | None = None,
    allow_unsupported: bool = False,
    source_byte_offset: int = 0,
    first_record_island: bool = True,
) -> RenderCandidate:
    if record.unsupported and not allow_unsupported:
        raise ProviderAuthorityError("unsupported-assistant-record")
    selected_profile = profile or renderer_profile(record.provider, version)
    if selected_profile.provider != record.provider or selected_profile.version != version or cols <= 0:
        raise ProviderAuthorityError("renderer-profile-mismatch")
    if not first_record_island:
        selected_profile = replace(
            selected_profile,
            first_gutter=selected_profile.continuation_gutter,
            first_gutter_styles=(),
        )
    copy_text, lines, boundaries = _compile_render_lines(
        lex_semantic_source(record.text, source_byte_offset=source_byte_offset),
        selected_profile,
    )
    if any(line.heading_level == 1 for line in lines):
        raise ProviderAuthorityError("unsupported-heading-style")

    for line in lines:
        while line.units and line.units[-1].grapheme == " ":
            unit = line.units.pop()
            boundaries.append(
                BoundaryProvenance(
                    "trimmed-whitespace",
                    unit.copy_start,
                    unit.copy_end,
                    unit.source_start,
                    unit.source_end,
                )
            )

    content_lines = [
        index for index, line in enumerate(lines) if line.units or line.marker is not None
    ]
    if not content_lines:
        raise ProviderAuthorityError("empty-render-candidate")

    rows: list[list[str]] = []
    cells: list[RenderedCell] = []
    style_rows: list[list[tuple[int, int, str]]] = []
    boundary_anchors: dict[int, tuple[int, int]] = {}
    row = -1
    column = 0

    def put(
        grapheme: str,
        copy_start: int | None,
        copy_end: int | None,
        style: str,
        presentation: bool = False,
        verify_style: bool = False,
    ) -> None:
        nonlocal column
        width = _unit_width(grapheme)
        if column + width > cols:
            raise ProviderAuthorityError("renderer-width-too-small")
        rows[row][column] = grapheme
        for filler in range(column + 1, column + width):
            rows[row][filler] = ""
        cells.append(
            RenderedCell(
                row,
                column,
                grapheme,
                width,
                copy_start,
                copy_end,
                style,
                presentation,
            )
        )
        if not presentation or verify_style:
            style_rows[row].append((column, column + width, style))
        column += width

    def begin_row(padding: int = 0) -> None:
        nonlocal row, column
        row += 1
        rows.append([" "] * cols)
        style_rows.append([])
        gutter = (
            selected_profile.first_gutter
            if row == 0
            else selected_profile.continuation_gutter
        )
        gutter_styles = (
            selected_profile.first_gutter_styles
            if row == 0
            else ()
        )
        column = 0
        for gutter_index, grapheme in enumerate(GRAPHEME_RE.findall(gutter)):
            expected_style = (
                gutter_styles[gutter_index]
                if gutter_index < len(gutter_styles)
                else None
            )
            put(
                grapheme,
                None,
                None,
                expected_style or "presentation",
                True,
                expected_style is not None,
            )
        if column + padding > cols:
            raise ProviderAuthorityError("renderer-width-too-small")
        column += padding

    def place_units(units: Sequence[_RenderUnit], continuation_padding: int) -> None:
        nonlocal column
        groups: list[tuple[bool, list[_RenderUnit]]] = []
        for unit in units:
            grapheme = unit.grapheme
            if grapheme != " " and grapheme.isspace():
                raise ProviderAuthorityError("unsupported-wrap-whitespace")
            whitespace = grapheme == " "
            if groups and groups[-1][0] == whitespace:
                groups[-1][1].append(unit)
            else:
                groups.append((whitespace, [unit]))
        index = 0
        while index < len(groups):
            whitespace, group = groups[index]
            width = sum(_unit_width(unit.grapheme) for unit in group)
            if whitespace and index + 1 < len(groups):
                next_group = groups[index + 1][1]
                next_width = sum(_unit_width(unit.grapheme) for unit in next_group)
                if len(group) == 1 and column + width + next_width > cols:
                    if next_width > cols - len(selected_profile.continuation_gutter) - continuation_padding:
                        raise ProviderAuthorityError("unsupported-unbreakable-token")
                    unit = group[0]
                    begin_row(continuation_padding)
                    boundary = BoundaryProvenance(
                        "soft-wrap-separator",
                        unit.copy_start,
                        unit.copy_end,
                        unit.source_start,
                        unit.source_end,
                        row,
                        0,
                    )
                    boundaries.append(boundary)
                    boundary_anchors[id(boundary)] = (row, 0)
                    index += 1
                    continue
                if column + width + next_width > cols:
                    raise ProviderAuthorityError("unsupported-wrap-whitespace")
            elif not whitespace and width > cols - len(selected_profile.continuation_gutter) - continuation_padding:
                raise ProviderAuthorityError("unsupported-unbreakable-token")
            if column + width > cols:
                raise ProviderAuthorityError("unsupported-wrap-whitespace")
            for unit in group:
                put(
                    unit.grapheme,
                    unit.copy_start,
                    unit.copy_end,
                    unit.style,
                )
            index += 1

    active_list_padding = 0
    previous_line_index: int | None = None
    previous_line: _CompiledLine | None = None
    for line_index in content_lines:
        line = lines[line_index]
        lazy_continuation = False
        if previous_line_index is None:
            begin_row()
        else:
            separated = line_index - previous_line_index > 1
            margin = previous_line.heading_level is not None or separated
            crossed_rows = []
            if margin:
                begin_row()
                crossed_rows.append(row)
            lazy_continuation = (
                active_list_padding > 0
                and not separated
                and line.marker is None
                and line.heading_level is None
            )
            if not lazy_continuation and line.marker is None:
                active_list_padding = 0
            begin_row(active_list_padding if lazy_continuation else 0)
            crossed_rows.append(row)
            hard_breaks = [
                compiled.hard_break
                for compiled in lines[previous_line_index:line_index]
                if compiled.hard_break is not None
            ]
            for boundary_index, boundary in enumerate(hard_breaks):
                boundary_anchors[id(boundary)] = (
                    crossed_rows[min(boundary_index, len(crossed_rows) - 1)],
                    0,
                )

        continuation_padding = active_list_padding if lazy_continuation else 0
        if line.marker is not None:
            marker, copy_start, copy_end, style = line.marker
            marker_width = sum(
                _unit_width(grapheme) for grapheme in GRAPHEME_RE.findall(marker)
            )
            continuation_padding = marker_width + 1
            active_list_padding = continuation_padding
            for grapheme in GRAPHEME_RE.findall(marker):
                put(grapheme, copy_start, copy_end, style)
            column += 1
            if column > cols:
                raise ProviderAuthorityError("renderer-width-too-small")
        place_units(line.units, continuation_padding)
        previous_line_index = line_index
        previous_line = line

    copy_cells = [cell for cell in cells if cell.copy_start is not None]
    if not copy_cells:
        raise ProviderAuthorityError("empty-render-candidate")
    first = copy_cells[0]
    last_cell = copy_cells[-1]
    for hard_break in (value for value in boundaries if value.kind == "hard-break"):
        anchor = boundary_anchors.get(id(hard_break))
        if anchor is None:
            continue
        copy_cursor = hard_break.copy_start
        for boundary in sorted(boundaries, key=lambda value: value.copy_start, reverse=True):
            if boundary.kind != "trimmed-whitespace" or boundary.copy_end != copy_cursor:
                continue
            boundary_anchors[id(boundary)] = anchor
            copy_cursor = boundary.copy_start

    anchored_boundaries = []
    for boundary in boundaries:
        anchor = boundary_anchors.get(id(boundary))
        if anchor is None and boundary.anchor_row is not None and boundary.anchor_column is not None:
            anchor = (boundary.anchor_row, boundary.anchor_column)
        if anchor is None:
            if boundary.copy_end <= first.copy_start:
                anchor = (first.row, first.column)
            elif boundary.copy_start >= last_cell.copy_end:
                anchor = (
                    last_cell.row,
                    last_cell.column + last_cell.width,
                )
            else:
                raise ProviderAuthorityError("unsupported-boundary-layout")
        anchored_boundaries.append(
            replace(
                boundary,
                anchor_row=anchor[0],
                anchor_column=anchor[1],
            )
        )

    return RenderCandidate(
        provider=record.provider,
        version=version,
        record_id=record.record_id,
        source_id=record.source_id,
        source_text=record.text,
        copy_text=copy_text,
        plain_rows=tuple("".join(parts) for parts in rows),
        style_rows=tuple(tuple(spans) for spans in style_rows),
        cells=tuple(cells),
        boundaries=tuple(
            sorted(
                anchored_boundaries,
                key=lambda value: (value.copy_start, value.copy_end, value.kind),
            )
        ),
        selection_start=(first.row, first.column),
        selection_end=(last_cell.row, last_cell.column + last_cell.width),
        unsupported=record.unsupported,
        source_start=source_byte_offset,
        source_end=source_byte_offset + len(record.text.encode("utf-8")),
    )


def _inline_code_spans(body: str) -> tuple[tuple[int, int], ...] | None:
    runs: list[tuple[int, int]] = []
    cursor = 0
    while cursor < len(body):
        if body[cursor] != "`":
            cursor += 1
            continue
        escaped = 0
        backslash = cursor - 1
        while backslash >= 0 and body[backslash] == "\\":
            escaped += 1
            backslash -= 1
        end = cursor + 1
        while end < len(body) and body[end] == "`":
            end += 1
        if escaped % 2 == 0:
            runs.append((cursor, end))
        cursor = end
    if not runs:
        return ()
    spans = []
    cursor = 0
    while cursor < len(runs):
        opening = runs[cursor]
        width = opening[1] - opening[0]
        closing_index = next(
            (
                index
                for index in range(cursor + 1, len(runs))
                if runs[index][1] - runs[index][0] == width
            ),
            None,
        )
        if closing_index is None:
            return None
        spans.append((opening[0], runs[closing_index][1]))
        cursor = closing_index + 1
    return tuple(spans)


def _inline_code_islands(source: str) -> tuple[tuple[int, int], ...]:
    lines: list[tuple[int, int, int, str]] = []
    cursor = 0
    for line in source.splitlines(keepends=True):
        body_end = cursor + len(line) - (1 if line.endswith("\n") else 0)
        lines.append((cursor, cursor + len(line), body_end, source[cursor:body_end]))
        cursor += len(line)
    if not lines and source == "":
        return ()

    unsupported: set[int] = set()
    for line_index, (_, _, _, body) in enumerate(lines):
        spans = _inline_code_spans(body)
        if spans is None:
            raise ProviderAuthorityError("unsupported-inline-code")
        if not spans:
            lex_semantic_source(body)
            continue
        masked = list(body)
        for start, end in spans:
            masked[start:end] = "x" * (end - start)
        lex_semantic_source("".join(masked))
        unsupported.add(line_index)
    if not unsupported:
        raise ProviderAuthorityError("unsupported-inline-code")

    for line_index in tuple(unsupported):
        before = line_index - 1
        while before >= 0 and not lines[before][3].strip(" \t"):
            unsupported.add(before)
            before -= 1
        after = line_index + 1
        while after < len(lines) and not lines[after][3].strip(" \t"):
            unsupported.add(after)
            after += 1

    islands = []
    line_index = 0
    while line_index < len(lines):
        if line_index in unsupported:
            line_index += 1
            continue
        first = line_index
        while line_index + 1 < len(lines) and line_index + 1 not in unsupported:
            line_index += 1
        last = line_index
        start = lines[first][0]
        end = lines[last][1]
        if last + 1 < len(lines):
            end = lines[last][2]
        if start < end and source[start:end].strip(" \t\n"):
            islands.append((start, end))
        line_index += 1
    return tuple(islands)


def _render_record_candidates(
    record: AssistantTextRecord,
    *,
    version: str,
    cols: int,
    profile: RendererProfile,
) -> tuple[tuple[RenderCandidate, ...], tuple[str, ...]]:
    try:
        return (
            (
                render_semantic_candidate(
                    record,
                    version=version,
                    cols=cols,
                    profile=profile,
                    allow_unsupported=record.unsupported,
                ),
            ),
            (),
        )
    except ProviderAuthorityError as exc:
        if exc.reason != "unsupported-inline-code":
            raise

    islands = _inline_code_islands(record.text)
    if not islands:
        raise ProviderAuthorityError("unsupported-inline-code")
    candidates = []
    unsupported_texts = []
    cursor = 0
    byte_offsets = [0]
    for character in record.text:
        byte_offsets.append(byte_offsets[-1] + len(character.encode("utf-8")))
    for start, end in islands:
        if cursor < start:
            unsupported_texts.append(record.text[cursor:start])
        island_record = replace(record, text=record.text[start:end])
        candidates.append(
            render_semantic_candidate(
                island_record,
                version=version,
                cols=cols,
                profile=profile,
                allow_unsupported=record.unsupported,
                source_byte_offset=byte_offsets[start],
                first_record_island=start == 0,
            )
        )
        cursor = end
    if cursor < len(record.text):
        unsupported_texts.append(record.text[cursor:])
    return tuple(candidates), tuple(unsupported_texts)


def normalize_styled_rows(
    styled_rows: Sequence[str],
    plain_rows: Sequence[str],
) -> tuple[tuple[tuple[int, int, str], ...], ...]:
    if len(styled_rows) != len(plain_rows):
        raise ProviderAuthorityError("styled-row-geometry")
    normalized = []
    for styled, plain in zip(styled_rows, plain_rows):
        spans: list[tuple[int, int, str]] = []
        visible: list[str] = []
        column = 0
        bold = dim = italic = underline = reverse = strike = False
        background = False
        foreground: str | None = None
        cursor = 0
        while cursor < len(styled):
            if styled[cursor] == "\x1b":
                match = re.match(r"\x1b\[([0-9;:]*)m", styled[cursor:])
                if match is None:
                    raise ProviderAuthorityError("unsupported-styled-row")
                raw_parameters = match.group(1).replace(":", ";")
                try:
                    parameters = [int(value) if value else 0 for value in raw_parameters.split(";")]
                except ValueError as exc:
                    raise ProviderAuthorityError("unsupported-styled-row") from exc
                if not parameters:
                    parameters = [0]
                index = 0
                while index < len(parameters):
                    parameter = parameters[index]
                    if parameter == 0:
                        bold = dim = italic = underline = reverse = strike = background = False
                        foreground = None
                    elif parameter == 1:
                        bold = True
                    elif parameter == 2:
                        dim = True
                    elif parameter == 3:
                        italic = True
                    elif parameter == 4:
                        underline = True
                    elif parameter == 7:
                        reverse = True
                    elif parameter == 9:
                        strike = True
                    elif parameter == 22:
                        bold = dim = False
                    elif parameter == 23:
                        italic = False
                    elif parameter == 24:
                        underline = False
                    elif parameter == 27:
                        reverse = False
                    elif parameter == 29:
                        strike = False
                    elif 40 <= parameter <= 48 or 100 <= parameter <= 107:
                        background = parameter != 49
                        if parameter == 48:
                            if index + 1 >= len(parameters) or parameters[index + 1] not in (2, 5):
                                raise ProviderAuthorityError("unsupported-styled-row")
                            count = 2 if parameters[index + 1] == 5 else 4
                            if index + count >= len(parameters):
                                raise ProviderAuthorityError("unsupported-styled-row")
                            index += count
                    elif parameter == 49:
                        background = False
                    elif parameter == 38:
                        if index + 1 >= len(parameters) or parameters[index + 1] not in (2, 5):
                            raise ProviderAuthorityError("unsupported-styled-row")
                        mode = parameters[index + 1]
                        count = 2 if mode == 5 else 4
                        if index + count >= len(parameters):
                            raise ProviderAuthorityError("unsupported-styled-row")
                        if mode == 5:
                            value = parameters[index + 2]
                            if not 0 <= value <= 255:
                                raise ProviderAuthorityError("unsupported-styled-row")
                            foreground = f"indexed-{value}"
                        else:
                            red, green, blue = parameters[index + 2 : index + 5]
                            if any(not 0 <= value <= 255 for value in (red, green, blue)):
                                raise ProviderAuthorityError("unsupported-styled-row")
                            foreground = f"rgb-{red}-{green}-{blue}"
                        index += count
                    elif parameter == 39:
                        foreground = None
                    elif 30 <= parameter <= 37 or 90 <= parameter <= 97:
                        foreground = f"sgr-{parameter}"
                    else:
                        raise ProviderAuthorityError("unsupported-styled-row")
                    index += 1
                cursor += match.end()
                continue
            grapheme_match = GRAPHEME_RE.match(styled, cursor)
            if grapheme_match is None:
                raise ProviderAuthorityError("unsupported-styled-row")
            grapheme = grapheme_match.group(0)
            if any(ord(character) < 32 for character in grapheme):
                raise ProviderAuthorityError("unsupported-styled-row")
            width = _captured_grapheme_width(grapheme)
            if underline or strike or (bold and italic):
                style = "unsupported"
            elif reverse or background:
                style = "code-inline"
            elif bold:
                style = "strong"
            elif italic:
                style = "emphasis"
            elif dim:
                style = "list-marker"
            else:
                style = "plain"
            if foreground is not None:
                style = f"{style};fg-{foreground}"
            spans.append((column, column + width, style))
            visible.append(grapheme)
            column += width
            cursor = grapheme_match.end()
        if "".join(visible) != plain:
            raise ProviderAuthorityError("styled-row-text-mismatch")
        normalized.append(tuple(spans))
    return tuple(normalized)


def normalize_plain_rows(
    plain_rows: Sequence[str],
    cols: int,
) -> tuple[str, ...]:
    if cols <= 0:
        raise ProviderAuthorityError("invalid-captured-geometry")
    normalized = []
    for row in plain_rows:
        if not isinstance(row, str):
            raise ProviderAuthorityError("invalid-captured-row")
        width = 0
        for grapheme in GRAPHEME_RE.findall(row):
            if any(ord(character) < 32 for character in grapheme):
                raise ProviderAuthorityError("invalid-captured-cell")
            width += _captured_grapheme_width(grapheme)
            if width > cols:
                raise ProviderAuthorityError("captured-row-overflow")
        normalized.append(row + " " * (cols - width))
    return tuple(normalized)


def _style_satisfies(expected: str, actual: str) -> bool:
    if expected == "assistant-dot":
        return actual == "plain;fg-indexed-231"
    if expected == actual:
        return True
    return (expected, actual) in {
        ("assistant", "plain"),
        ("heading", "strong"),
        ("code", "code-inline"),
    }


def _candidate_styles_match(
    candidate: RenderCandidate,
    actual_rows: Sequence[Sequence[tuple[int, int, str]]],
    placement: int,
) -> bool:
    if placement + len(candidate.style_rows) > len(actual_rows):
        return False
    for row_index, expected_spans in enumerate(candidate.style_rows):
        actual_cells: dict[int, str] = {}
        for start, end, style in actual_rows[placement + row_index]:
            if start < 0 or end <= start:
                return False
            for column in range(start, end):
                if column in actual_cells:
                    return False
                actual_cells[column] = style
        for start, end, expected in expected_spans:
            if any(not _style_satisfies(expected, actual_cells.get(column, "unsupported")) for column in range(start, end)):
                return False
    return True


def exact_plain_row_placements(candidate: RenderCandidate, plain_rows: Sequence[str]) -> tuple[int, ...]:
    height = len(candidate.plain_rows)
    if not height or height > len(plain_rows):
        return ()
    return tuple(
        start
        for start in range(len(plain_rows) - height + 1)
        if tuple(plain_rows[start : start + height]) == candidate.plain_rows
    )


def _candidate_selection_text(
    candidate: RenderCandidate,
    placement: int,
    selection_start: tuple[int, int],
    selection_end: tuple[int, int],
) -> str:
    first_row = placement
    last_row = placement + len(candidate.plain_rows) - 1
    if (
        selection_start >= selection_end
        or selection_start[0] < first_row
        or selection_end[0] > last_row
        or not 0 <= selection_start[1] <= len(candidate.plain_rows[0])
        or not 0 <= selection_end[1] <= len(candidate.plain_rows[0])
    ):
        raise ProviderAuthorityError("selection-outside-provider-block")

    selected: list[RenderedCell] = []
    selected_ids: set[int] = set()
    for cell_index, cell in enumerate(candidate.cells):
        if cell.copy_start is None or cell.copy_end is None:
            continue
        absolute_row = placement + cell.row
        row_start = selection_start[1] if absolute_row == selection_start[0] else 0
        row_end = (
            selection_end[1]
            if absolute_row == selection_end[0]
            else len(candidate.plain_rows[cell.row])
        )
        if absolute_row < selection_start[0] or absolute_row > selection_end[0]:
            continue
        overlap_start = max(row_start, cell.column)
        overlap_end = min(row_end, cell.column + cell.width)
        if overlap_end <= overlap_start:
            continue
        if overlap_start != cell.column or overlap_end != cell.column + cell.width:
            raise ProviderAuthorityError("partial-provider-cell")
        selected.append(cell)
        selected_ids.add(cell_index)

    selected_boundaries = []
    for boundary in candidate.boundaries:
        if boundary.anchor_row is None or boundary.anchor_column is None:
            raise ProviderAuthorityError("unanchored-provider-boundary")
        absolute_anchor = (
            placement + boundary.anchor_row,
            boundary.anchor_column,
        )
        if selection_start < absolute_anchor <= selection_end:
            selected_boundaries.append(boundary)

    if not selected and not selected_boundaries:
        raise ProviderAuthorityError("provider-presentation-only-selection")
    if any(cell.style == "list-marker" for cell in selected) and not any(
        cell.style != "list-marker" for cell in selected
    ):
        raise ProviderAuthorityError("partial-semantic-marker")

    selected_ranges = [
        (cell.copy_start, cell.copy_end)
        for cell in selected
        if cell.copy_start is not None and cell.copy_end is not None
    ]
    selected_ranges.extend(
        (boundary.copy_start, boundary.copy_end)
        for boundary in selected_boundaries
    )
    selected_start = min(start for start, _ in selected_ranges)
    selected_end = max(end for _, end in selected_ranges)
    expected_start = (
        placement + candidate.selection_start[0],
        candidate.selection_start[1],
    )
    expected_end = (
        placement + candidate.selection_end[0],
        candidate.selection_end[1],
    )
    reaches_leading_edge = (
        selection_start[0] == expected_start[0]
        and selection_start[1] <= expected_start[1]
    )
    reaches_trailing_edge = (
        selection_end[0] == expected_end[0]
        and selection_end[1] >= expected_end[1]
    )

    output_start = 0 if reaches_leading_edge else selected_start
    output_end = len(candidate.copy_text) if reaches_trailing_edge else selected_end
    for cell_index, cell in enumerate(candidate.cells):
        if cell.copy_start is None or cell.copy_end is None:
            continue
        if (
            cell.copy_start < output_end
            and cell.copy_end > output_start
            and cell_index not in selected_ids
        ):
            raise ProviderAuthorityError("discontinuous-provider-selection")

    coverage = list(selected_ranges)
    copy_cells = [
        cell
        for cell in candidate.cells
        if cell.copy_start is not None and cell.copy_end is not None
    ]
    if reaches_leading_edge:
        coverage.extend(
            (boundary.copy_start, boundary.copy_end)
            for boundary in candidate.boundaries
            if boundary.copy_end <= copy_cells[0].copy_start
        )
    if reaches_trailing_edge:
        coverage.extend(
            (boundary.copy_start, boundary.copy_end)
            for boundary in candidate.boundaries
            if boundary.copy_start >= copy_cells[-1].copy_end
        )
    coverage.sort()
    covered = output_start
    for start, end in coverage:
        if end <= covered:
            continue
        if start > covered:
            raise ProviderAuthorityError("unexplained-provider-gap")
        covered = end
    if covered < output_end:
        raise ProviderAuthorityError("unexplained-provider-gap")
    return candidate.copy_text[output_start:output_end]


def match_complete_provider_block(
    candidates: Sequence[RenderCandidate],
    plain_rows: Sequence[str],
    selection_start: tuple[int, int],
    selection_end: tuple[int, int],
    *,
    style_rows: Sequence[Sequence[tuple[int, int, str]]] | None = None,
) -> MatchResult:
    try:
        matches: list[tuple[RenderCandidate, int, str]] = []
        shadows: list[tuple[RenderCandidate, int]] = []
        repeated_selection = False
        for candidate in candidates:
            placements = exact_plain_row_placements(candidate, plain_rows)
            for placement in placements:
                try:
                    text = _candidate_selection_text(
                        candidate,
                        placement,
                        selection_start,
                        selection_end,
                    )
                except ProviderAuthorityError:
                    continue
                if len(placements) != 1:
                    if not candidate.unsupported:
                        repeated_selection = True
                    continue
                if style_rows is not None and not _candidate_styles_match(candidate, style_rows, placement):
                    continue
                if candidate.unsupported:
                    shadows.append((candidate, placement))
                else:
                    matches.append((candidate, placement, text))
        if repeated_selection:
            return MatchResult.failure("ambiguous-provider-block")
        identities = {(candidate.source_id, placement) for candidate, placement, _ in matches}
        if len(matches) != 1 or len(identities) != 1:
            return MatchResult.failure("no-unique-canonical-provider-block" if not matches else "ambiguous-provider-block")
        candidate, placement, text = matches[0]
        if any(
            shadow_placement == placement
            and shadow.plain_rows == candidate.plain_rows
            and shadow.style_rows == candidate.style_rows
            for shadow, shadow_placement in shadows
        ):
            return MatchResult.failure("unsupported-provider-alias")
        return MatchResult(
            True,
            text=text,
            provider=candidate.provider,
            record_id=candidate.record_id,
            placement_row=placement,
            source_start=candidate.source_start,
            source_end=candidate.source_end,
        )
    except Exception:
        return MatchResult.failure("provider-match-failed")


def _unsupported_text_collision_cannot_be_excluded(
    unsupported_text: str,
    candidate: RenderCandidate,
) -> bool:
    if not unsupported_text:
        return False
    candidate_values = (candidate.source_text, candidate.copy_text)
    unsupported_values = (unsupported_text, html.unescape(unsupported_text))
    return any(
        candidate_value
        and unsupported_value
        and (
            candidate_value in unsupported_value
            or unsupported_value in candidate_value
        )
        for candidate_value in candidate_values
        for unsupported_value in unsupported_values
    )


def authoritative_provider_match(
    binding: ProviderBinding,
    index: TranscriptIndex,
    *,
    transcript_root: Path | str,
    cols: int,
    plain_rows: Sequence[str],
    selection_start: tuple[int, int],
    selection_end: tuple[int, int],
    style_rows: Sequence[Sequence[tuple[int, int, str]]] | None = None,
    owner_uid: int | None = None,
    proc_start_reader: Callable[[int], str] = _read_proc_start,
    proc_environ_reader: Callable[[int], Mapping[str, str]] = _read_proc_environ,
    before_final_revalidation: Callable[[], None] | None = None,
) -> MatchResult:
    fence: TranscriptFence | None = None
    try:
        validate_binding_process(
            binding,
            proc_start_reader=proc_start_reader,
            proc_environ_reader=proc_environ_reader,
        )
        fence = open_transcript_fence(binding, root=transcript_root, owner_uid=owner_uid)
        records = index.update(fence, binding)
        profile = renderer_profile(binding.provider, binding.version)
        normalized_plain_rows = normalize_plain_rows(plain_rows, cols)
        if style_rows is not None:
            if len(style_rows) != len(normalized_plain_rows) or any(
                start < 0 or end <= start or end > cols
                for row in style_rows
                for start, end, _ in row
            ):
                raise ProviderAuthorityError("styled-row-geometry")
        candidates = []
        render_failed_texts: list[str] = []
        for record in records:
            try:
                rendered, unsupported_texts = _render_record_candidates(
                    record,
                    version=binding.version,
                    cols=cols,
                    profile=profile,
                )
                candidates.extend(rendered)
                render_failed_texts.extend(unsupported_texts)
            except ProviderAuthorityError:
                render_failed_texts.append(record.text)
        result = match_complete_provider_block(
            tuple(candidates),
            normalized_plain_rows,
            selection_start,
            selection_end,
            style_rows=style_rows,
        )
        if not result.matched:
            return result
        matched_candidate = next(
            (
                candidate
                for candidate in candidates
                if not candidate.unsupported
                and candidate.record_id == result.record_id
                and candidate.source_start == result.source_start
                and candidate.source_end == result.source_end
            ),
            None,
        )
        if matched_candidate is None or any(
            _unsupported_text_collision_cannot_be_excluded(text, matched_candidate)
            for text in render_failed_texts
        ):
            return MatchResult.failure("unsupported-provider-alias")
        if before_final_revalidation is not None:
            before_final_revalidation()
        validate_binding_process(
            binding,
            proc_start_reader=proc_start_reader,
            proc_environ_reader=proc_environ_reader,
        )
        revalidate_transcript_fence(fence)
        return result
    except ProviderAuthorityError as exc:
        return MatchResult.failure(exc.reason)
    except Exception:
        return MatchResult.failure("provider-internal-failure")
    finally:
        if fence is not None:
            close_transcript_fence(fence)


@dataclass(frozen=True)
class ProviderSelectionResult:
    owned: bool
    text: str | None = None


_TRANSCRIPT_INDEXES: OrderedDict[tuple[str, Path, int], TranscriptIndex] = OrderedDict()
_PROVIDER_SELECTION_LOCK = threading.Lock()
_PROVIDER_DIAGNOSTIC_LOCK = threading.Lock()
_PROVIDER_DIAGNOSTICS: OrderedDict[tuple[str, str, str], int] = OrderedDict()
_PROVIDER_DIAGNOSTIC_TOTAL = 0


def _flush_provider_diagnostics() -> None:
    global _PROVIDER_DIAGNOSTIC_TOTAL
    with _PROVIDER_DIAGNOSTIC_LOCK:
        if not _PROVIDER_DIAGNOSTICS:
            return
        counters = [
            {
                "mode": mode,
                "decision": decision,
                "reason": reason,
                "count": count,
            }
            for (mode, decision, reason), count in _PROVIDER_DIAGNOSTICS.items()
        ]
        _PROVIDER_DIAGNOSTICS.clear()
        _PROVIDER_DIAGNOSTIC_TOTAL = 0
    try:
        print(
            "provider authority diagnostics "
            + json.dumps(counters, separators=(",", ":"), sort_keys=True)
        )
    except Exception:
        pass


def _record_provider_diagnostic(mode: str, decision: str, reason: str) -> None:
    global _PROVIDER_DIAGNOSTIC_TOTAL
    if not re.fullmatch(r"[a-z0-9-]{1,64}", reason):
        reason = "invalid-reason"
    key = (mode, decision, reason)
    flush = False
    with _PROVIDER_DIAGNOSTIC_LOCK:
        if (
            key not in _PROVIDER_DIAGNOSTICS
            and len(_PROVIDER_DIAGNOSTICS) >= MAX_PROVIDER_DIAGNOSTIC_REASONS - 1
        ):
            key = ("aggregate", "overflow", "other-reason")
        _PROVIDER_DIAGNOSTICS[key] = _PROVIDER_DIAGNOSTICS.get(key, 0) + 1
        _PROVIDER_DIAGNOSTIC_TOTAL += 1
        flush = _PROVIDER_DIAGNOSTIC_TOTAL >= PROVIDER_DIAGNOSTIC_FLUSH_EVERY
    if flush:
        _flush_provider_diagnostics()


atexit.register(_flush_provider_diagnostics)


def _transcript_index(key: tuple[str, Path, int]) -> TranscriptIndex:
    provider, path, generation = key
    for existing in tuple(_TRANSCRIPT_INDEXES):
        if existing[0] == provider and existing[1] == path and existing[2] != generation:
            _TRANSCRIPT_INDEXES.pop(existing, None)
    index = _TRANSCRIPT_INDEXES.pop(key, None)
    if index is None:
        index = TranscriptIndex()
    _TRANSCRIPT_INDEXES[key] = index
    while len(_TRANSCRIPT_INDEXES) > MAX_TRANSCRIPT_INDEXES:
        _TRANSCRIPT_INDEXES.popitem(last=False)
    return index


def provider_authority_mode() -> str:
    value = os.environ.get("MOBILE_TERMINAL_PROVIDER_AUTHORITY", "off").lower()
    return value if value in ("off", "shadow", "enforce") else "off"


def _binding_cache_path(home: Path, pane_id: str) -> Path:
    if not PANE_RE.fullmatch(pane_id):
        raise ProviderAuthorityError("invalid-pane")
    return home / ".mobile-terminal" / "provider-bindings" / f"{pane_id[1:]}.json"


def _load_binding_cache(home: Path, pane_id: str) -> Mapping[str, Any] | None:
    path = _binding_cache_path(home, pane_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ProviderAuthorityError("binding-cache-unavailable") from exc
    if len(raw) > 64 * 1024:
        raise ProviderAuthorityError("binding-cache-oversized")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ProviderAuthorityError("binding-cache-invalid") from exc
    if not isinstance(data, dict) or data.get("schema") != 1 or data.get("paneId") != pane_id:
        raise ProviderAuthorityError("binding-cache-invalid")
    return data


def _cache_binding(data: Mapping[str, Any], *, require_active: bool = True) -> ProviderBinding:
    try:
        provider = data["provider"]
        pane_id = data["paneId"]
        session_id = data["sessionId"]
        transcript_value = data["transcriptPath"]
        pid_value = data["pid"]
        proc_start = data["procStart"]
        version = data["version"]
        generation_value = data["generation"]
        active = data["active"]
    except KeyError as exc:
        raise ProviderAuthorityError("binding-cache-invalid") from exc
    if (
        provider not in ("claude", "codex")
        or not isinstance(pane_id, str)
        or not PANE_RE.fullmatch(pane_id)
        or not isinstance(session_id, str)
        or not UUID_RE.fullmatch(session_id)
        or not isinstance(transcript_value, str)
        or not transcript_value
        or isinstance(pid_value, bool)
        or not isinstance(pid_value, int)
        or pid_value <= 0
        or not isinstance(proc_start, str)
        or not proc_start
        or not isinstance(version, str)
        or not version
        or isinstance(generation_value, bool)
        or not isinstance(generation_value, int)
        or generation_value <= 0
        or not isinstance(active, bool)
    ):
        raise ProviderAuthorityError("binding-cache-invalid")
    transcript_path = Path(transcript_value)
    if not transcript_path.is_absolute():
        raise ProviderAuthorityError("binding-cache-invalid")
    if require_active and not active:
        raise ProviderAuthorityError("binding-cache-stale")
    return ProviderBinding(
        provider,
        pane_id,
        pid_value,
        proc_start,
        session_id,
        session_id,
        transcript_path,
        version,
        generation_value,
    )


def resolve_provider_binding(
    pane_id: str,
    *,
    home: Path | str | None = None,
) -> tuple[ProviderBinding | None, Mapping[str, Any] | None]:
    provider_home = Path(home) if home is not None else Path.home()
    cache = _load_binding_cache(provider_home, pane_id)
    if cache is None:
        try:
            binding = bind_claude_pane(
                pane_id,
                sessions_root=provider_home / ".claude" / "sessions",
                transcript_root=provider_home / ".claude" / "projects",
            )
        except ProviderAuthorityError as exc:
            if exc.reason == "claude-binding-unavailable":
                return None, None
            raise
        return binding, None
    cached = _cache_binding(cache)
    if cached.provider == "claude":
        live = bind_claude_pane(
            pane_id,
            sessions_root=provider_home / ".claude" / "sessions",
            transcript_root=provider_home / ".claude" / "projects",
            expected_pid=cached.pid,
            expected_session_id=cached.session_id,
        )
        if (
            live.pid != cached.pid
            or live.proc_start != cached.proc_start
            or live.session_id != cached.session_id
            or live.transcript_path.absolute() != cached.transcript_path.absolute()
            or live.version != cached.version
        ):
            raise ProviderAuthorityError("registry-hook-disagreement")
        return replace(live, generation=cached.generation), cache
    event = {
        "provider": "codex",
        "pane": cached.pane_id,
        "sessionId": cached.session_id,
        "procStart": cached.proc_start,
        "version": cached.version,
        "rolloutPath": str(cached.transcript_path),
        "pid": cached.pid,
        "generation": cached.generation,
    }
    return bind_codex_pane(
        event,
        transcript_root=provider_home / ".codex" / "sessions",
    ), cache


def _selection_is_owned(
    binding: ProviderBinding,
    cache: Mapping[str, Any] | None,
    snapshot: Any,
    start_row: int,
    end_row: int,
) -> bool:
    if binding.provider == "claude":
        return bool(snapshot.alternate)
    if cache is None or cache.get("ownershipUnavailable") is True:
        raise ProviderAuthorityError("codex-ownership-unavailable")
    ranges = cache.get("ownershipRanges")
    if not isinstance(ranges, list) or not ranges:
        raise ProviderAuthorityError("codex-ownership-unavailable")
    try:
        absolute_start = int(snapshot.history) + start_row
        absolute_end = int(snapshot.history) + end_row
        current_history = int(snapshot.history)
        current_history_limit = int(snapshot.history_limit)
        current_end = int(snapshot.history) + int(snapshot.cursor_y)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ProviderAuthorityError("codex-ownership-stale") from exc
    owned = False
    for value in ranges:
        if not isinstance(value, dict):
            raise ProviderAuthorityError("codex-ownership-stale")
        try:
            ownership_start = int(value["startRow"])
            history_at_start = int(value["historyAtStart"])
            history_limit = int(value["historyLimit"])
            range_alternate = bool(value["alternate"])
            saturated = bool(value["saturated"])
            ownership_end_value = value.get("endRow")
            ownership_end = current_end if ownership_end_value is None else int(ownership_end_value)
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderAuthorityError("codex-ownership-stale") from exc
        if (
            range_alternate
            or bool(snapshot.alternate)
            or saturated
            or history_limit != current_history_limit
            or current_history < history_at_start
            or (history_limit and current_history >= history_limit)
            or ownership_end < ownership_start
        ):
            raise ProviderAuthorityError("codex-ownership-stale")
        if absolute_start <= ownership_end and absolute_end >= ownership_start:
            owned = True
    return owned


def _transcript_root(binding: ProviderBinding, home: Path) -> Path:
    if binding.provider == "claude":
        return home / ".claude" / "projects"
    return home / ".codex" / "sessions"


def _provider_selection_locked(
    snapshot: Any,
    start_x: int,
    start_row: int,
    end_x: int,
    end_row: int,
    *,
    home: Path | str | None = None,
) -> ProviderSelectionResult:
    mode = provider_authority_mode()
    if mode == "off":
        return ProviderSelectionResult(False)

    def decision(owned: bool, reason: str, text: str | None = None) -> ProviderSelectionResult:
        _record_provider_diagnostic(mode, "matched" if owned else "unowned", reason)
        return ProviderSelectionResult(owned, text)

    provider_home = Path(home) if home is not None else Path.home()
    cache: Mapping[str, Any] | None = None
    cached: ProviderBinding | None = None
    owned = False
    try:
        cache = _load_binding_cache(provider_home, snapshot.pane_id)
        if cache is not None:
            cached = _cache_binding(cache, require_active=False)
            if cached.provider == "codex":
                owned = _selection_is_owned(cached, cache, snapshot, start_row, end_row)
                if not cache["active"]:
                    if owned:
                        raise ProviderAuthorityError("binding-cache-stale")
                    return decision(False, "inactive-unowned")
            elif not cache["active"]:
                if bool(snapshot.alternate):
                    raise ProviderAuthorityError("binding-cache-stale")
                return decision(False, "inactive-unowned")
        try:
            binding, cache = resolve_provider_binding(snapshot.pane_id, home=provider_home)
        except ProviderAuthorityError:
            if cached is not None and cached.provider == "claude" and not bool(snapshot.alternate):
                return decision(False, "inactive-unowned")
            raise
        if binding is None:
            return decision(False, "binding-unavailable")
        owned = _selection_is_owned(binding, cache, snapshot, start_row, end_row)
        if not owned:
            return decision(False, "selection-unowned")
        style_rows = normalize_styled_rows(snapshot.physical_rows, snapshot.plain_physical_rows)
        key = (binding.provider, binding.transcript_path.absolute(), binding.generation)
        index = _transcript_index(key)
        result = authoritative_provider_match(
            binding,
            index,
            transcript_root=_transcript_root(binding, provider_home),
            cols=snapshot.cols,
            plain_rows=snapshot.plain_physical_rows,
            selection_start=(start_row + snapshot.seed_history, start_x),
            selection_end=(end_row + snapshot.seed_history, end_x),
            style_rows=style_rows,
            before_final_revalidation=lambda: _revalidate_runtime_binding(
                binding, cache, provider_home
            ),
        )
        if not result.matched:
            raise ProviderAuthorityError(result.internal_reason or "provider-match-failed")
        return decision(True, "canonical-match", result.text)
    except ProviderAuthorityError as exc:
        if mode == "shadow":
            _record_provider_diagnostic(mode, "fallback", exc.reason)
            return ProviderSelectionResult(False)
        _record_provider_diagnostic(mode, "rejected", exc.reason)
        raise
    except Exception as exc:
        if mode == "shadow":
            _record_provider_diagnostic(mode, "fallback", "provider-internal-failure")
            return ProviderSelectionResult(False)
        _record_provider_diagnostic(mode, "rejected", "provider-internal-failure")
        raise ProviderAuthorityError("provider-internal-failure") from exc


def provider_selection(
    snapshot: Any,
    start_x: int,
    start_row: int,
    end_x: int,
    end_row: int,
    *,
    home: Path | str | None = None,
) -> ProviderSelectionResult:
    with _PROVIDER_SELECTION_LOCK:
        return _provider_selection_locked(
            snapshot,
            start_x,
            start_row,
            end_x,
            end_row,
            home=home,
        )


def _revalidate_runtime_binding(
    binding: ProviderBinding,
    cache: Mapping[str, Any] | None,
    home: Path,
) -> None:
    current, current_cache = resolve_provider_binding(binding.pane_id, home=home)
    if current != binding or current_cache != cache:
        raise ProviderAuthorityError("binding-changed")
