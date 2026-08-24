import json
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import provider_authority as provider_authority_module
from provider_authority import (
    AssistantTextRecord,
    MatchResult,
    MAX_TRANSCRIPT_INDEXES,
    ProviderAuthorityError,
    ProviderBinding,
    RendererProfile,
    STALE_MESSAGE,
    TranscriptIndex,
    _TRANSCRIPT_INDEXES,
    _selection_is_owned,
    _transcript_index,
    authoritative_provider_match,
    bind_claude_pane,
    bind_codex_pane,
    close_transcript_fence,
    exact_plain_row_placements,
    lex_semantic_source,
    match_complete_provider_block,
    normalize_styled_rows,
    open_transcript_fence,
    provider_selection,
    render_semantic_candidate,
    resolve_provider_binding,
    revalidate_transcript_fence,
)


SESSION_ID = "12345678-1234-4123-8123-123456789abc"
OTHER_SESSION_ID = "abcdefab-cdef-4abc-8def-abcdefabcdef"
CODEX_V7_ID = "0198f1a2-3456-7abc-8def-0123456789ab"


def write_jsonl(path, records, *, final_newline=True):
    payload = b"".join(
        json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
        for record in records
    )
    if payload and not final_newline:
        payload = payload[:-1]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


def claude_text(text, *, message_id="msg_1", record_uuid="record-1", content=None):
    return {
        "type": "assistant",
        "uuid": record_uuid,
        "sessionId": SESSION_ID,
        "message": {
            "type": "message",
            "role": "assistant",
            "id": message_id,
            "content": [{"type": "text", "text": text}] if content is None else content,
        },
    }


def codex_meta(*, thread_id=SESSION_ID, session_id=OTHER_SESSION_ID, parent_thread_id=None):
    payload = {"id": thread_id, "session_id": session_id}
    if parent_thread_id is not None:
        payload["parent_thread_id"] = parent_thread_id
    return {"type": "session_meta", "payload": payload}


def codex_text(text, *, item_id="item_1", turn_id="turn_1", content=None):
    return {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "id": item_id,
            "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
            "content": [{"type": "output_text", "text": text}] if content is None else content,
        },
    }


def binding(provider, path, *, version=None, proc_start="77", pane="%9"):
    return ProviderBinding(
        provider=provider,
        pane_id=pane,
        pid=123,
        proc_start=proc_start,
        session_id=SESSION_ID,
        transcript_id=SESSION_ID,
        transcript_path=path,
        version=version or ("2.1.241" if provider == "claude" else "0.147.0"),
        generation=4,
    )


def verified_styled_rows(candidate, *, dot_fg=231, continuation_gutter_fg=None):
    sgr = {
        "assistant-dot": f"38;5;{dot_fg}",
        "heading": "1",
        "strong": "1",
        "emphasis": "3",
        "list-marker": "2",
    }
    rows = []
    for row_index, plain in enumerate(candidate.plain_rows):
        styles = {}
        for start, end, style in candidate.style_rows[row_index]:
            for column in range(start, end):
                styles[column] = style
        parts = []
        for column, character in enumerate(plain):
            style = styles.get(column)
            if continuation_gutter_fg is not None and row_index and column < 2:
                parts.append(f"\x1b[38;5;{continuation_gutter_fg}m{character}\x1b[0m")
            elif style in sgr:
                parts.append(f"\x1b[{sgr[style]}m{character}\x1b[0m")
            else:
                parts.append(character)
        rows.append("".join(parts))
    return tuple(rows)


class TranscriptFenceTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "approved"
        self.path = self.root / "nested" / f"{SESSION_ID}.jsonl"
        write_jsonl(self.path, [claude_text("safe")])
        self.binding = binding("claude", self.path)

    def tearDown(self):
        self.temporary.cleanup()

    def test_fence_records_complete_regular_file_identity(self):
        fence = open_transcript_fence(self.binding, root=self.root)
        try:
            self.assertEqual(fence.size, self.path.stat().st_size)
            self.assertEqual(fence.complete_size, fence.size)
            self.assertEqual((fence.device, fence.inode), (self.path.stat().st_dev, self.path.stat().st_ino))
            revalidate_transcript_fence(fence)
        finally:
            close_transcript_fence(fence)

    def test_rejects_outside_root_and_symlink_components(self):
        outside = Path(self.temporary.name) / "outside.jsonl"
        write_jsonl(outside, [claude_text("secret")])
        with self.assertRaisesRegex(ProviderAuthorityError, "outside-root"):
            open_transcript_fence(binding("claude", outside), root=self.root)

        linked = self.root / "link"
        linked.symlink_to(outside.parent, target_is_directory=True)
        with self.assertRaisesRegex(ProviderAuthorityError, "unsafe-transcript-open"):
            open_transcript_fence(
                binding("claude", linked / outside.name),
                root=self.root,
            )

    def test_rejects_symlink_file_non_regular_owner_and_partial_tail(self):
        target = Path(self.temporary.name) / "target.jsonl"
        write_jsonl(target, [claude_text("secret")])
        symlink = self.root / "linked.jsonl"
        symlink.symlink_to(target)
        with self.assertRaisesRegex(ProviderAuthorityError, "unsafe-transcript-open"):
            open_transcript_fence(binding("claude", symlink), root=self.root)
        with self.assertRaisesRegex(ProviderAuthorityError, "metadata"):
            open_transcript_fence(self.binding, root=self.root, owner_uid=os.geteuid() + 1)
        self.path.write_bytes(b'{"type":"assistant"}')
        with self.assertRaisesRegex(ProviderAuthorityError, "partial-jsonl-record"):
            open_transcript_fence(self.binding, root=self.root)

    def test_final_revalidation_rejects_append_and_inode_rotation(self):
        fence = open_transcript_fence(self.binding, root=self.root)
        try:
            with self.path.open("ab") as output:
                output.write(b"{}\n")
            with self.assertRaisesRegex(ProviderAuthorityError, "transcript-changed"):
                revalidate_transcript_fence(fence)
        finally:
            close_transcript_fence(fence)

        fence = open_transcript_fence(self.binding, root=self.root)
        replacement = self.path.with_suffix(".replacement")
        write_jsonl(replacement, [claude_text("replacement")])
        replacement.replace(self.path)
        try:
            with self.assertRaisesRegex(ProviderAuthorityError, "transcript-changed"):
                revalidate_transcript_fence(fence)
        finally:
            close_transcript_fence(fence)


class ClaudeBindingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        home = Path(self.temporary.name)
        self.registry = home / "sessions"
        self.transcripts = home / "projects"
        self.registry.mkdir()
        self.path = self.transcripts / "project" / f"{SESSION_ID}.jsonl"
        write_jsonl(self.path, [claude_text("hello")])

    def tearDown(self):
        self.temporary.cleanup()

    def write_registry(self, registry_pid, **updates):
        data = {
            "pid": registry_pid,
            "sessionId": SESSION_ID,
            "procStart": "444",
            "version": "2.1.241",
            "tmux": "name:@2.%9",
            "cwd": "/not/authority",
            "name": "not-authority",
        }
        data.update(updates)
        (self.registry / f"{registry_pid}.json").write_text(json.dumps(data), encoding="utf-8")

    def bind(self, **kwargs):
        return bind_claude_pane(
            "%9",
            sessions_root=self.registry,
            transcript_root=self.transcripts,
            expected_pid=kwargs.get("expected_pid"),
            expected_session_id=kwargs.get("expected_session_id"),
            proc_start_reader=kwargs.get("proc_start_reader", lambda pid: "444"),
            proc_environ_reader=kwargs.get("proc_environ_reader", lambda pid: {"TMUX_PANE": "%9"}),
        )

    def test_binds_exact_registry_process_pane_session_and_transcript(self):
        self.write_registry(321)
        result = self.bind()
        self.assertEqual(result.pid, 321)
        self.assertEqual(result.pane_id, "%9")
        self.assertEqual(result.transcript_path, self.path)
        self.assertEqual(result.session_id, SESSION_ID)

    def test_rejects_process_start_and_tmux_pane_mismatch(self):
        self.write_registry(321)
        for readers in (
            {"proc_start_reader": lambda pid: "445"},
            {"proc_environ_reader": lambda pid: {"TMUX_PANE": "%8"}},
        ):
            with self.subTest(readers=tuple(readers)):
                with self.assertRaisesRegex(ProviderAuthorityError, "binding-unavailable"):
                    self.bind(**readers)

    def test_requires_one_live_match_and_does_not_use_names_or_recency(self):
        self.write_registry(321)
        self.write_registry(322, name="newest", cwd=str(self.path.parent))
        with self.assertRaisesRegex(ProviderAuthorityError, "ambiguous"):
            self.bind()
        resolved = self.bind(expected_pid=321, expected_session_id=SESSION_ID)
        self.assertEqual(resolved.pid, 321)
        (self.registry / "322.json").unlink()
        duplicate = self.transcripts / "other" / f"{SESSION_ID}.jsonl"
        write_jsonl(duplicate, [claude_text("newer")])
        with self.assertRaisesRegex(ProviderAuthorityError, "binding-unavailable"):
            self.bind()

    def test_pid_filename_and_session_uuid_are_authoritative(self):
        self.write_registry(321, pid=999)
        with self.assertRaisesRegex(ProviderAuthorityError, "binding-unavailable"):
            self.bind()
        self.write_registry(321, sessionId="not-a-uuid")
        with self.assertRaisesRegex(ProviderAuthorityError, "binding-unavailable"):
            self.bind()


class CodexBindingTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "sessions"
        self.path = self.root / "2026" / f"rollout-{SESSION_ID}.jsonl"
        write_jsonl(self.path, [codex_meta(), codex_text("hello")])
        self.event = {
            "provider": "codex",
            "pane": "%9",
            "pid": 123,
            "procStart": "77",
            "sessionId": SESSION_ID,
            "rolloutPath": str(self.path),
            "version": "0.147.0",
            "generation": 5,
            "cwd": "/not/authority",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_binds_only_explicit_lifecycle_identity(self):
        result = bind_codex_pane(
            self.event,
            transcript_root=self.root,
            proc_start_reader=lambda pid: "77",
            proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
        )
        self.assertEqual(result.transcript_path, self.path)
        self.assertEqual(result.generation, 5)

    def test_accepts_uuidv7_lifecycle_identity(self):
        path = self.root / "2026" / f"rollout-{CODEX_V7_ID}.jsonl"
        write_jsonl(path, [codex_meta(thread_id=CODEX_V7_ID), codex_text("hello")])
        event = dict(self.event, sessionId=CODEX_V7_ID, rolloutPath=str(path))
        result = bind_codex_pane(
            event,
            transcript_root=self.root,
            proc_start_reader=lambda pid: "77",
            proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
        )
        self.assertEqual(result.session_id, CODEX_V7_ID)

    def test_rejects_process_pane_and_outside_path(self):
        with self.assertRaisesRegex(ProviderAuthorityError, "process-mismatch"):
            bind_codex_pane(
                self.event,
                transcript_root=self.root,
                proc_start_reader=lambda pid: "78",
                proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
            )
        outside = dict(self.event, rolloutPath=str(Path(self.temporary.name) / "outside.jsonl"))
        with self.assertRaisesRegex(ProviderAuthorityError, "outside-root"):
            bind_codex_pane(
                outside,
                transcript_root=self.root,
                proc_start_reader=lambda pid: "77",
                proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
            )


class TranscriptIndexTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "root"
        self.path = self.root / f"{SESSION_ID}.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def update(self, index, provider="claude"):
        current = binding(provider, self.path)
        fence = open_transcript_fence(current, root=self.root)
        try:
            return index.update(fence, current)
        finally:
            close_transcript_fence(fence)

    def test_claude_keeps_same_message_chunks_distinct(self):
        write_jsonl(self.path, [claude_text("one", record_uuid="u1")])
        index = TranscriptIndex()
        records = self.update(index)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].text, "one")
        with self.path.open("ab") as output:
            output.write((json.dumps(claude_text(" two", record_uuid="u2")) + "\n").encode())
        records = self.update(index)
        self.assertEqual([record.text for record in records], ["one", " two"])
        self.assertNotEqual(records[0].record_id, records[1].record_id)
        self.assertEqual(index.offset, self.path.stat().st_size)

    def test_claude_keeps_text_content_blocks_distinct(self):
        write_jsonl(
            self.path,
            [
                claude_text(
                    "unused",
                    content=[
                        {"type": "text", "text": "first"},
                        {"type": "text", "text": "second"},
                    ],
                )
            ],
        )
        records = self.update(TranscriptIndex())
        self.assertEqual([record.text for record in records], ["first", "second"])
        self.assertEqual(len({record.source_id for record in records}), 2)

    def test_claude_group_index_evicts_with_bounded_records(self):
        write_jsonl(
            self.path,
            [
                claude_text("one", message_id="one"),
                claude_text("two", message_id="two"),
                claude_text("three", message_id="three"),
            ],
        )
        index = TranscriptIndex(max_records=2)
        records = self.update(index)
        self.assertEqual([record.text for record in records], ["two", "three"])
        self.assertEqual(set(index._claude_groups), {record.record_id for record in records})
        with self.path.open("ab") as output:
            output.write((json.dumps(claude_text("new", message_id="one")) + "\n").encode())
        records = self.update(index)
        self.assertEqual(records[-1].text, "new")
        self.assertNotEqual(records[-1].text, "onenew")

    def test_claude_unknown_content_marks_candidate_unsupported(self):
        write_jsonl(
            self.path,
            [claude_text("visible", content=[{"type": "text", "text": "visible"}, {"type": "tool_use", "id": "x"}])],
        )
        record = self.update(TranscriptIndex())[0]
        self.assertTrue(record.unsupported)
        with self.assertRaisesRegex(ProviderAuthorityError, "unsupported-assistant-record"):
            render_semantic_candidate(record, version="2.1.241", cols=20)

    def test_claude_explicit_schema_rejects_drift(self):
        record = claude_text("visible")
        record["message"]["content"][0]["extra"] = True
        write_jsonl(self.path, [record])
        with self.assertRaisesRegex(ProviderAuthorityError, "schema-drift"):
            self.update(TranscriptIndex())

    def test_codex_indexes_real_metadata_shape_with_physical_record_offset(self):
        metadata = codex_meta(parent_thread_id=CODEX_V7_ID)
        write_jsonl(self.path, [metadata, codex_text("answer")])
        records = self.update(TranscriptIndex(), "codex")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].text, "answer")
        self.assertIn(SESSION_ID, records[0].record_id)
        self.assertIn("item_1", records[0].record_id)
        metadata_bytes = len(json.dumps(metadata, separators=(",", ":")).encode()) + 1
        self.assertIn(f":{metadata_bytes}:item_1", records[0].record_id)
        self.assertTrue(records[0].source_id.endswith(":turn_1"))

    def test_codex_requires_session_metadata_and_exact_session(self):
        write_jsonl(self.path, [codex_text("answer")])
        with self.assertRaisesRegex(ProviderAuthorityError, "missing-codex-session"):
            self.update(TranscriptIndex(), "codex")
        write_jsonl(
            self.path,
            [codex_meta(thread_id=OTHER_SESSION_ID)],
        )
        with self.assertRaisesRegex(ProviderAuthorityError, "session-mismatch"):
            self.update(TranscriptIndex(), "codex")

    def test_rejects_partial_oversized_and_invalid_records(self):
        write_jsonl(self.path, [claude_text("x")], final_newline=False)
        with self.assertRaisesRegex(ProviderAuthorityError, "partial-jsonl-record"):
            self.update(TranscriptIndex())
        write_jsonl(self.path, [claude_text("x" * 100)])
        with self.assertRaisesRegex(ProviderAuthorityError, "oversized"):
            self.update(TranscriptIndex(max_record_bytes=40))
        self.path.write_bytes(b"not-json\n")
        with self.assertRaisesRegex(ProviderAuthorityError, "invalid-jsonl"):
            self.update(TranscriptIndex())

    def test_tail_bootstrap_skips_oversized_irrelevant_records_and_stays_incremental(self):
        oversized_irrelevant = {"type": "progress", "payload": "x" * 4096}
        write_jsonl(
            self.path,
            [
                claude_text("old", message_id="old"),
                oversized_irrelevant,
                {"type": "user"},
                claude_text("recent", message_id="recent"),
            ],
        )
        index = TranscriptIndex(
            max_record_bytes=256,
            max_scan_bytes=512,
            max_irrelevant_record_bytes=8192,
        )
        records = self.update(index)
        self.assertEqual([record.text for record in records if not record.unsupported], ["recent"])
        with self.path.open("ab") as output:
            output.write((json.dumps(oversized_irrelevant) + "\n").encode())
            output.write((json.dumps(claude_text("later", message_id="later")) + "\n").encode())
        records = self.update(index)
        self.assertEqual(records[-1].text, "later")
        self.assertEqual(index.offset, self.path.stat().st_size)

    def test_rejects_relevant_oversize_bootstrap_limit_truncation_and_rotation(self):
        write_jsonl(self.path, [claude_text("x" * 100)])
        with self.assertRaisesRegex(ProviderAuthorityError, "oversized-assistant"):
            self.update(
                TranscriptIndex(
                    max_record_bytes=40,
                    max_scan_bytes=20,
                    max_irrelevant_record_bytes=1000,
                )
            )
        self.path.write_bytes(b"x" * 300 + b"\n")
        with self.assertRaisesRegex(ProviderAuthorityError, "bootstrap-limit"):
            self.update(
                TranscriptIndex(
                    max_record_bytes=40,
                    max_scan_bytes=20,
                    max_irrelevant_record_bytes=100,
                )
            )
        write_jsonl(self.path, [claude_text("long enough")])
        index = TranscriptIndex()
        self.update(index)
        self.path.write_bytes(b"\n")
        with self.assertRaisesRegex(ProviderAuthorityError, "truncated"):
            self.update(index)
        replacement = self.path.with_suffix(".new")
        write_jsonl(replacement, [claude_text("replacement")])
        replacement.replace(self.path)
        with self.assertRaisesRegex(ProviderAuthorityError, "rotated"):
            self.update(index)


class SemanticLexerTest(unittest.TestCase):
    def rendered_and_copied(self, source):
        tokens = lex_semantic_source(source)
        return (
            "".join(token.render_text for token in tokens),
            "".join(token.copy_text for token in tokens),
            tokens,
        )

    def test_common_subset_classifies_every_source_byte(self):
        source = "# Head\nParagraph with *em* and **strong**.\n- item\n12. next\n"
        rendered, copied, tokens = self.rendered_and_copied(source)
        self.assertEqual(rendered, "Head\nParagraph with em and strong.\nitem\nnext\n")
        self.assertEqual(copied, rendered)
        self.assertEqual(tokens[0].source_start, 0)
        self.assertEqual(tokens[-1].source_end, len(source.encode("utf-8")))
        self.assertTrue(any(token.semantic.startswith("heading:") for token in tokens))
        self.assertTrue(any(token.semantic == "unordered-list" for token in tokens))
        self.assertTrue(any(token.semantic == "ordered-list:12" for token in tokens))

    def test_preserves_indentation_tabs_nbsp_repetition_trailing_and_blank_lines(self):
        source = "  gutter-like\n\tindented   \n\n• source bullet  \n"
        rendered, copied, _ = self.rendered_and_copied(source)
        self.assertEqual(rendered, source)
        self.assertEqual(copied, source)

    def test_escaped_markdown_is_visible_without_escape_marker(self):
        rendered, copied, _ = self.rendered_and_copied(r"\*literal\* and \# heading")
        self.assertEqual(rendered, "*literal* and # heading")
        self.assertEqual(copied, rendered)

    def test_rejects_theme_dependent_code_rendering(self):
        for source in ("paragraph with `inline` code", "```python\n  x\n```\n"):
            with self.subTest(source=source):
                with self.assertRaises(ProviderAuthorityError):
                    lex_semantic_source(source)

    def test_rejects_unsupported_and_ambiguous_markdown_without_whitespace_cleanup(self):
        unsupported = (
            "| a | b |\n|---|---|\n",
            "> quote\n",
            "- [ ] widget\n",
            "  - nested\n",
            "[link](https://example.com)\n",
            "**nested *value***\n",
            "```\nunclosed\n",
            "---\n",
        )
        for source in unsupported:
            with self.subTest(source=source):
                with self.assertRaises(ProviderAuthorityError):
                    lex_semantic_source(source)
    def test_rejects_html_entity_syntax(self):
        for source in (
            "left &gt; right",
            "left &#62; right",
            "left &#x3e; right",
            "left &gt right",
            "left &#62 right",
            "left &#x3e right",
        ):
            with self.subTest(source=source):
                with self.assertRaisesRegex(ProviderAuthorityError, "html-entity"):
                    lex_semantic_source(source)


class RenderingAndMatchingTest(unittest.TestCase):
    profile = RendererProfile("claude", "test", "G ", "  ", "- ", "{number}. ")

    def candidate(self, text, *, record_id="one", source_id=None, cols=12):
        record = AssistantTextRecord("claude", record_id, source_id or record_id, text, 1)
        return render_semantic_candidate(record, version="test", cols=cols, profile=self.profile)

    def test_render_keeps_copy_separate_from_gutters_markdown_and_wraps(self):
        candidate = self.candidate("## head\n- alpha beta")
        self.assertEqual(candidate.copy_text, "head\n- alpha beta")
        self.assertTrue(candidate.plain_rows[0].startswith("G "))
        self.assertTrue(candidate.plain_rows[1].startswith("  "))
        self.assertNotIn("#", candidate.copy_text)
        self.assertEqual(len(candidate.plain_rows), 4)
        self.assertTrue(all(len(row) == 12 for row in candidate.plain_rows))
        self.assertTrue(all(cell.copy_start is None for cell in candidate.cells if cell.presentation))

    def test_word_wrap_moves_whole_words_and_does_not_add_copy_newlines(self):
        for width in range(8, 13):
            with self.subTest(width=width):
                candidate = self.candidate("alpha beta gamma", cols=width)
                self.assertEqual(candidate.copy_text, "alpha beta gamma")
                self.assertGreater(len(candidate.plain_rows), 1)
                self.assertTrue(all("alph" not in row or "alpha" in row for row in candidate.plain_rows))

    def test_long_unbreakable_tokens_fail_closed(self):
        with self.assertRaisesRegex(ProviderAuthorityError, "unbreakable"):
            self.candidate("abcdefghijk", cols=8)

    def test_source_hard_breaks_are_preserved(self):
        candidate = self.candidate("abc\ndef", cols=20)
        self.assertEqual(candidate.copy_text, "abc\ndef")
        self.assertEqual(len(candidate.plain_rows), 2)

    def test_unordered_list_uses_dash_and_hanging_continuation(self):
        candidate = self.candidate("- alpha beta gamma", cols=10)
        self.assertEqual(candidate.copy_text, "- alpha beta gamma")
        self.assertEqual(candidate.plain_rows, ("G - alpha ", "    beta  ", "    gamma "))
        marker = [cell for cell in candidate.cells if cell.style == "list-marker"]
        self.assertEqual([(cell.row, cell.column, cell.text) for cell in marker], [(0, 2, "-")])

    def test_partial_selection_crosses_soft_wrap_without_newline(self):
        candidate = self.candidate("alpha beta gamma", cols=10)
        result = match_complete_provider_block(
            (candidate,),
            candidate.plain_rows,
            (0, 2),
            (1, 6),
        )
        self.assertTrue(result.matched)
        self.assertEqual(result.text, "alpha beta")

    def test_partial_selection_crosses_source_newline_and_omits_layout_margin(self):
        candidate = self.candidate("## head\nbody", cols=12)
        result = match_complete_provider_block(
            (candidate,),
            candidate.plain_rows,
            candidate.selection_start,
            candidate.selection_end,
        )
        self.assertTrue(result.matched)
        self.assertEqual(result.text, "head\nbody")
        self.assertEqual(candidate.plain_rows[1], " " * 12)

    def test_gutter_only_and_partial_semantic_markers_fail_closed(self):
        candidate = self.candidate("12. item", cols=12)
        gutter = match_complete_provider_block((candidate,), candidate.plain_rows, (0, 0), (0, 2))
        self.assertFalse(gutter.matched)
        partial_marker = match_complete_provider_block((candidate,), candidate.plain_rows, (0, 2), (0, 3))
        self.assertFalse(partial_marker.matched)
        marker_and_item = match_complete_provider_block((candidate,), candidate.plain_rows, (0, 2), candidate.selection_end)
        self.assertTrue(marker_and_item.matched)
        self.assertEqual(marker_and_item.text, "12. item")

    def test_clipped_and_display_transformed_candidates_fail_closed(self):
        candidate = self.candidate("alpha beta gamma", cols=10)
        clipped = match_complete_provider_block(
            (candidate,),
            candidate.plain_rows[:-1],
            candidate.selection_start,
            (1, 6),
        )
        self.assertFalse(clipped.matched)
        transformed = list(candidate.plain_rows)
        transformed[0] = transformed[0].replace("alpha", "other")
        changed = match_complete_provider_block(
            (candidate,),
            transformed,
            candidate.selection_start,
            candidate.selection_end,
        )
        self.assertFalse(changed.matched)

    def test_exact_plain_rows_require_one_placement_and_allow_partial_selection(self):
        candidate = self.candidate("unique", cols=12)
        rows = ("ordinary    ",) + candidate.plain_rows + ("after       ",)
        self.assertEqual(exact_plain_row_placements(candidate, rows), (1,))
        start = (1 + candidate.selection_start[0], candidate.selection_start[1])
        end = (1 + candidate.selection_end[0], candidate.selection_end[1])
        result = match_complete_provider_block((candidate,), rows, start, end)
        self.assertTrue(result.matched)
        self.assertEqual(result.text, "unique")
        self.assertEqual(result.placement_row, 1)
        partial = match_complete_provider_block((candidate,), rows, start, (end[0], end[1] - 1))
        self.assertTrue(partial.matched)
        self.assertEqual(partial.text, "uniqu")

    def test_repeated_rows_different_sources_and_style_mismatch_fail_closed(self):
        candidate = self.candidate("same", cols=12)
        rows = candidate.plain_rows + candidate.plain_rows
        start = candidate.selection_start
        end = candidate.selection_end
        repeated = match_complete_provider_block((candidate,), rows, start, end)
        self.assertFalse(repeated.matched)
        self.assertEqual(repeated.internal_reason, "ambiguous-provider-block")

        other = self.candidate("**same**", record_id="two", source_id="two", cols=12)
        ambiguous_source = match_complete_provider_block((candidate, other), candidate.plain_rows, start, end)
        self.assertFalse(ambiguous_source.matched)
        mismatched_style = tuple(() for _ in candidate.style_rows)
        style_result = match_complete_provider_block(
            (candidate,), candidate.plain_rows, start, end, style_rows=mismatched_style
        )
        self.assertFalse(style_result.matched)

    def test_runtime_style_normalization_verifies_required_spans(self):
        candidate = self.candidate("**same**", cols=12)
        plain = candidate.plain_rows[0]
        styled = f"{plain[:2]}\x1b[1m{plain[2:6]}\x1b[22m{plain[6:]}"
        normalized = normalize_styled_rows((styled,), candidate.plain_rows)
        matched = match_complete_provider_block(
            (candidate,),
            candidate.plain_rows,
            candidate.selection_start,
            candidate.selection_end,
            style_rows=normalized,
        )
        self.assertTrue(matched.matched)

        wrong = normalize_styled_rows(candidate.plain_rows, candidate.plain_rows)
        rejected = match_complete_provider_block(
            (candidate,),
            candidate.plain_rows,
            candidate.selection_start,
            candidate.selection_end,
            style_rows=wrong,
        )
        self.assertFalse(rejected.matched)
        unsupported = normalize_styled_rows(("G \x1b[4msame\x1b[24m      ",), candidate.plain_rows)
        rejected = match_complete_provider_block(
            (candidate,),
            candidate.plain_rows,
            candidate.selection_start,
            candidate.selection_end,
            style_rows=unsupported,
        )
        self.assertFalse(rejected.matched)

    def test_tab_layout_fails_closed_when_wrap_behavior_is_unverified(self):
        with self.assertRaisesRegex(ProviderAuthorityError, "wrap"):
            self.candidate("a\tb", cols=12)

    def test_claude_dot_requires_indexed_fg231_and_default_reset(self):
        candidate = render_semantic_candidate(
            AssistantTextRecord("claude", "dot", "dot", "alpha beta gamma", 1),
            version="2.1.241",
            cols=10,
        )
        for dot_fg, expected in ((231, True), (114, False), (246, False)):
            with self.subTest(dot_fg=dot_fg):
                normalized = normalize_styled_rows(
                    verified_styled_rows(candidate, dot_fg=dot_fg),
                    candidate.plain_rows,
                )
                result = match_complete_provider_block(
                    (candidate,),
                    candidate.plain_rows,
                    candidate.selection_start,
                    candidate.selection_end,
                    style_rows=normalized,
                )
                self.assertEqual(result.matched, expected)

        first = candidate.plain_rows[0]
        unreset = (
            f"\x1b[38;5;231m{first}" + "\x1b[0m",
            *candidate.plain_rows[1:],
        )
        normalized = normalize_styled_rows(unreset, candidate.plain_rows)
        self.assertFalse(
            match_complete_provider_block(
                (candidate,),
                candidate.plain_rows,
                candidate.selection_start,
                candidate.selection_end,
                style_rows=normalized,
            ).matched
        )

        normalized = normalize_styled_rows(
            verified_styled_rows(candidate, continuation_gutter_fg=246),
            candidate.plain_rows,
        )
        self.assertTrue(
            match_complete_provider_block(
                (candidate,),
                candidate.plain_rows,
                candidate.selection_start,
                candidate.selection_end,
                style_rows=normalized,
            ).matched
        )

    def test_boundary_provenance_controls_exact_and_edge_copy(self):
        candidate = self.candidate("\n  alpha  \n", cols=14)
        exact = match_complete_provider_block(
            (candidate,),
            candidate.plain_rows,
            candidate.selection_start,
            candidate.selection_end,
        )
        expanded = match_complete_provider_block(
            (candidate,), candidate.plain_rows, (0, 0), (0, 14)
        )
        self.assertEqual(exact.text, "\n  alpha  \n")
        self.assertEqual(expanded.text, exact.text)

        leading = match_complete_provider_block(
            (candidate,), candidate.plain_rows, candidate.selection_start, (0, 8)
        )
        middle = match_complete_provider_block(
            (candidate,), candidate.plain_rows, (0, 4), (0, 8)
        )
        trailing = match_complete_provider_block(
            (candidate,), candidate.plain_rows, (0, 4), candidate.selection_end
        )
        self.assertEqual(leading.text, "\n  alph")
        self.assertEqual(middle.text, "alph")
        self.assertEqual(trailing.text, "alpha  \n")
        self.assertEqual(
            {boundary.kind for boundary in candidate.boundaries},
            {"hard-break", "trimmed-whitespace"},
        )

    def test_hard_soft_and_margin_boundaries_have_exact_copy_behavior(self):
        hard = self.candidate("alpha\nbeta", cols=14)
        self.assertEqual(
            match_complete_provider_block(
                (hard,), hard.plain_rows, hard.selection_start, hard.selection_end
            ).text,
            "alpha\nbeta",
        )

        soft = self.candidate("alpha beta", cols=9)
        self.assertIn("soft-wrap-separator", {value.kind for value in soft.boundaries})
        self.assertEqual(
            match_complete_provider_block(
                (soft,), soft.plain_rows, soft.selection_start, soft.selection_end
            ).text,
            "alpha beta",
        )
        unexplained = replace(
            soft,
            boundaries=tuple(
                value
                for value in soft.boundaries
                if value.kind != "soft-wrap-separator"
            ),
        )
        self.assertFalse(
            match_complete_provider_block(
                (unexplained,),
                unexplained.plain_rows,
                unexplained.selection_start,
                unexplained.selection_end,
            ).matched
        )

        margin = self.candidate("P\n\n- I", cols=14)
        self.assertEqual(margin.plain_rows[1], " " * 14)
        self.assertEqual(
            match_complete_provider_block(
                (margin,),
                margin.plain_rows,
                margin.selection_start,
                margin.selection_end,
            ).text,
            "P\n\n- I",
        )
        self.assertFalse(
            match_complete_provider_block(
                (margin,), margin.plain_rows, (1, 0), (1, 14)
            ).matched
        )

    def test_physical_boundary_endpoints_and_starts_are_directional(self):
        cases = (
            ("hard", self.candidate("abc\ndef", cols=12), "abc\n", "def"),
            ("soft", self.candidate("abc def", cols=6), "abc ", "def"),
            (
                "trimmed-hard",
                self.candidate("abc  \ndef", cols=12),
                "abc  \n",
                "def",
            ),
        )
        for name, candidate, prefix, suffix in cases:
            with self.subTest(case=name, anchors=True):
                self.assertTrue(candidate.boundaries)
                self.assertEqual(
                    {(boundary.anchor_row, boundary.anchor_column) for boundary in candidate.boundaries},
                    {(1, 0)},
                )
            previous_cell_end = max(
                cell.column + cell.width
                for cell in candidate.cells
                if cell.row == 0 and cell.copy_start is not None
            )
            for end_column in range(3):
                with self.subTest(
                    case=name,
                    boundary_only=True,
                    end_column=end_column,
                ):
                    result = match_complete_provider_block(
                        (candidate,),
                        candidate.plain_rows,
                        (0, previous_cell_end),
                        (1, end_column),
                    )
                    self.assertTrue(result.matched)
                    self.assertEqual(result.text, prefix[len("abc") :])
            for start in (candidate.selection_start, (0, 0)):
                for end_column in range(3):
                    with self.subTest(
                        case=name,
                        start=start,
                        end_column=end_column,
                    ):
                        result = match_complete_provider_block(
                            (candidate,),
                            candidate.plain_rows,
                            start,
                            (1, end_column),
                        )
                        self.assertTrue(result.matched)
                        self.assertEqual(result.text, prefix)
            for start_column in range(3):
                for end in (candidate.selection_end, (1, len(candidate.plain_rows[1]))):
                    with self.subTest(
                        case=name,
                        start_column=start_column,
                        end=end,
                    ):
                        result = match_complete_provider_block(
                            (candidate,),
                            candidate.plain_rows,
                            (1, start_column),
                            end,
                        )
                        self.assertTrue(result.matched)
                        self.assertEqual(result.text, suffix)

    def test_synthetic_margin_crossing_does_not_invent_copy_bytes(self):
        candidate = self.candidate("## H\nP", cols=12)
        hard_break = next(
            boundary
            for boundary in candidate.boundaries
            if boundary.kind == "hard-break"
        )
        self.assertEqual(
            (hard_break.anchor_row, hard_break.anchor_column),
            (1, 0),
        )
        for end_column in range(3):
            prefix = match_complete_provider_block(
                (candidate,),
                candidate.plain_rows,
                candidate.selection_start,
                (1, end_column),
            )
            self.assertTrue(prefix.matched)
            self.assertEqual(prefix.text, "H\n")
        for start_column in range(3):
            suffix = match_complete_provider_block(
                (candidate,),
                candidate.plain_rows,
                (1, start_column),
                (2, len(candidate.plain_rows[2])),
            )
            self.assertTrue(suffix.matched)
            self.assertEqual(suffix.text, "P")

    def test_block_ir_applies_verified_heading_list_and_margin_rules(self):
        cases = {
            "## H\nP": ("G H         ", "            ", "  P         "),
            "## H\n\nP": ("G H         ", "            ", "  P         "),
            "P\n- I": ("G P         ", "  - I       "),
            "P\n\n- I": ("G P         ", "            ", "  - I       "),
            "- I\nP": ("G - I       ", "    P       "),
            "- I\n\nP": ("G - I       ", "            ", "  P         "),
            "P\n\n\n- I": ("G P         ", "            ", "  - I       "),
        }
        for source, expected_rows in cases.items():
            with self.subTest(source=source):
                candidate = self.candidate(source, cols=12)
                self.assertEqual(candidate.plain_rows, expected_rows)
                result = match_complete_provider_block(
                    (candidate,),
                    candidate.plain_rows,
                    candidate.selection_start,
                    candidate.selection_end,
                    style_rows=normalize_styled_rows(
                        verified_styled_rows(candidate), candidate.plain_rows
                    ),
                )
                self.assertTrue(result.matched)
                self.assertEqual(result.text, candidate.copy_text)

        with self.assertRaisesRegex(ProviderAuthorityError, "heading-style"):
            self.candidate("# H\nP", cols=12)
        for level in range(2, 7):
            candidate = self.candidate(f"{'#' * level} H", cols=12)
            styles = normalize_styled_rows(
                verified_styled_rows(candidate), candidate.plain_rows
            )
            self.assertTrue(
                match_complete_provider_block(
                    (candidate,),
                    candidate.plain_rows,
                    candidate.selection_start,
                    candidate.selection_end,
                    style_rows=styles,
                ).matched
            )

    def test_repeated_spaces_indentation_and_blank_lines_match_byte_exactly(self):
        source = "  alpha  beta  \n\n\n  omega   \n"
        candidate = self.candidate(source, cols=20)
        result = match_complete_provider_block(
            (candidate,),
            candidate.plain_rows,
            candidate.selection_start,
            candidate.selection_end,
        )
        self.assertTrue(result.matched)
        self.assertEqual(result.text, source)

    def test_no_trim_or_common_indent_matching(self):
        candidate = self.candidate("  exact  ", cols=12)
        changed = tuple(row.replace("  exact  ", "exact    ") for row in candidate.plain_rows)
        result = match_complete_provider_block(
            (candidate,), changed, candidate.selection_start, candidate.selection_end
        )
        self.assertFalse(result.matched)

    def test_immutable_result_and_internal_failure_do_not_expose_transcript(self):
        result = MatchResult.failure("schema-drift")
        with self.assertRaises(FrozenInstanceError):
            result.text = "changed"
        self.assertEqual(result.public_error, STALE_MESSAGE)
        self.assertNotIn("secret transcript", repr(result))


class RuntimeBindingCacheTest(unittest.TestCase):
    def write_cache(self, home, *, active=True, **updates):
        transcript = home / ".claude" / "projects" / f"{SESSION_ID}.jsonl"
        cached = {
            "schema": 1,
            "provider": "claude",
            "paneId": "%9",
            "sessionId": SESSION_ID,
            "transcriptPath": str(transcript),
            "pid": 123,
            "procStart": "77",
            "generation": 7,
            "active": active,
            "version": "2.1.241",
        }
        cached.update(updates)
        cache_path = home / ".mobile-terminal" / "provider-bindings" / "9.json"
        cache_path.parent.mkdir(parents=True)
        cache_path.write_text(json.dumps(cached))
        return cached

    def test_claude_cache_filters_ambiguous_registry_processes(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            transcript = home / ".claude" / "projects" / f"{SESSION_ID}.jsonl"
            write_jsonl(transcript, [claude_text("answer")])
            cached = {
                "schema": 1,
                "provider": "claude",
                "paneId": "%9",
                "sessionId": SESSION_ID,
                "transcriptPath": str(transcript),
                "pid": 123,
                "procStart": "77",
                "generation": 7,
                "active": True,
                "version": "2.1.241",
            }
            cache_path = home / ".mobile-terminal" / "provider-bindings" / "9.json"
            cache_path.parent.mkdir(parents=True)
            cache_path.write_text(json.dumps(cached))
            live = binding("claude", transcript)
            with patch("provider_authority.bind_claude_pane", return_value=live) as bind:
                resolved, cache = resolve_provider_binding("%9", home=home)
            self.assertEqual(resolved.generation, 7)
            self.assertEqual(cache, cached)
            self.assertEqual(bind.call_args.kwargs["expected_pid"], 123)
            self.assertEqual(bind.call_args.kwargs["expected_session_id"], SESSION_ID)

    def test_cacheless_discovery_distinguishes_absence_from_ambiguity(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch(
                "provider_authority.bind_claude_pane",
                side_effect=ProviderAuthorityError("claude-binding-unavailable"),
            ):
                self.assertEqual(resolve_provider_binding("%9", home=home), (None, None))
            with patch(
                "provider_authority.bind_claude_pane",
                side_effect=ProviderAuthorityError("ambiguous-claude-binding"),
            ):
                with self.assertRaisesRegex(ProviderAuthorityError, "ambiguous-claude-binding"):
                    resolve_provider_binding("%9", home=home)

    def test_inactive_claude_cache_is_unowned_only_in_normal_buffer(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.write_cache(home, active=False)
            normal = SimpleNamespace(pane_id="%9", alternate=False)
            alternate = SimpleNamespace(pane_id="%9", alternate=True)
            with patch.dict(os.environ, {"MOBILE_TERMINAL_PROVIDER_AUTHORITY": "enforce"}), patch(
                "provider_authority.resolve_provider_binding",
                side_effect=AssertionError("inactive cache was rediscovered"),
            ):
                self.assertFalse(provider_selection(normal, 0, 0, 0, 0, home=home).owned)
                with self.assertRaisesRegex(ProviderAuthorityError, "binding-cache-stale"):
                    provider_selection(alternate, 0, 0, 0, 0, home=home)

    def test_dead_claude_cache_is_unowned_only_in_normal_buffer(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.write_cache(home)
            normal = SimpleNamespace(pane_id="%9", alternate=False)
            alternate = SimpleNamespace(pane_id="%9", alternate=True)
            unavailable = ProviderAuthorityError("claude-binding-unavailable")
            with patch.dict(os.environ, {"MOBILE_TERMINAL_PROVIDER_AUTHORITY": "enforce"}), patch(
                "provider_authority.resolve_provider_binding",
                side_effect=unavailable,
            ):
                self.assertFalse(provider_selection(normal, 0, 0, 0, 0, home=home).owned)
                with self.assertRaisesRegex(ProviderAuthorityError, "claude-binding-unavailable"):
                    provider_selection(alternate, 0, 0, 0, 0, home=home)

    def test_malformed_cache_fails_closed_but_shadow_preserves_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            self.write_cache(home, active="false")
            snapshot = SimpleNamespace(pane_id="%9", alternate=False)
            with patch.dict(os.environ, {"MOBILE_TERMINAL_PROVIDER_AUTHORITY": "enforce"}):
                with self.assertRaisesRegex(ProviderAuthorityError, "binding-cache-invalid"):
                    provider_selection(snapshot, 0, 0, 0, 0, home=home)
            with patch.dict(os.environ, {"MOBILE_TERMINAL_PROVIDER_AUTHORITY": "shadow"}):
                self.assertFalse(provider_selection(snapshot, 0, 0, 0, 0, home=home).owned)

    def test_cacheless_discovery_errors_fail_closed_but_shadow_preserves_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            snapshot = SimpleNamespace(pane_id="%9", alternate=False)
            error = ProviderAuthorityError("ambiguous-claude-binding")
            with patch(
                "provider_authority.resolve_provider_binding",
                side_effect=error,
            ), patch.dict(os.environ, {"MOBILE_TERMINAL_PROVIDER_AUTHORITY": "enforce"}):
                with self.assertRaisesRegex(ProviderAuthorityError, "ambiguous-claude-binding"):
                    provider_selection(snapshot, 0, 0, 0, 0, home=home)
            with patch(
                "provider_authority.resolve_provider_binding",
                side_effect=error,
            ), patch.dict(os.environ, {"MOBILE_TERMINAL_PROVIDER_AUTHORITY": "shadow"}):
                self.assertFalse(provider_selection(snapshot, 0, 0, 0, 0, home=home).owned)

    def test_provider_rejection_is_immediate_before_aggregate_threshold(self):
        with provider_authority_module._PROVIDER_DIAGNOSTIC_LOCK:
            saved = provider_authority_module._PROVIDER_DIAGNOSTICS.copy()
            saved_total = provider_authority_module._PROVIDER_DIAGNOSTIC_TOTAL
            provider_authority_module._PROVIDER_DIAGNOSTICS.clear()
            provider_authority_module._PROVIDER_DIAGNOSTIC_TOTAL = 0
        try:
            provider_authority_module._record_provider_diagnostic(
                "shadow", "matched", "canonical-match"
            )
            with patch("builtins.print") as output:
                provider_authority_module._record_provider_diagnostic(
                    "enforce", "rejected", "binding-cache-stale"
                )
            self.assertEqual(provider_authority_module._PROVIDER_DIAGNOSTIC_TOTAL, 2)
            output.assert_called_once()
            self.assertTrue(output.call_args.kwargs["flush"])
            diagnostic = output.call_args.args[0]
            counters = json.loads(diagnostic.removeprefix("provider authority diagnostics "))
            self.assertEqual(
                counters,
                [
                    {
                        "count": 1,
                        "decision": "rejected",
                        "mode": "enforce",
                        "reason": "binding-cache-stale",
                    }
                ],
            )
        finally:
            with provider_authority_module._PROVIDER_DIAGNOSTIC_LOCK:
                provider_authority_module._PROVIDER_DIAGNOSTICS.clear()
                provider_authority_module._PROVIDER_DIAGNOSTICS.update(saved)
                provider_authority_module._PROVIDER_DIAGNOSTIC_TOTAL = saved_total

    def test_provider_diagnostics_are_bounded_aggregate_reason_codes(self):
        with provider_authority_module._PROVIDER_DIAGNOSTIC_LOCK:
            saved = provider_authority_module._PROVIDER_DIAGNOSTICS.copy()
            saved_total = provider_authority_module._PROVIDER_DIAGNOSTIC_TOTAL
            provider_authority_module._PROVIDER_DIAGNOSTICS.clear()
            provider_authority_module._PROVIDER_DIAGNOSTIC_TOTAL = 0
        try:
            with patch("builtins.print") as output:
                provider_authority_module._record_provider_diagnostic(
                    "shadow",
                    "fallback",
                    "secret/path?credential=value",
                )
                for index in range(MAX_TRANSCRIPT_INDEXES * 3):
                    provider_authority_module._record_provider_diagnostic(
                        "enforce",
                        "rejected",
                        f"reason-{index}",
                    )
                self.assertLessEqual(
                    len(provider_authority_module._PROVIDER_DIAGNOSTICS),
                    provider_authority_module.MAX_PROVIDER_DIAGNOSTIC_REASONS,
                )
                provider_authority_module._flush_provider_diagnostics()
            self.assertTrue(all(call.kwargs["flush"] for call in output.call_args_list))
            diagnostic = output.call_args.args[0]
            self.assertIn("invalid-reason", diagnostic)
            self.assertNotIn("secret", diagnostic)
            self.assertNotIn("path", diagnostic)
            self.assertNotIn("credential", diagnostic)
        finally:
            with provider_authority_module._PROVIDER_DIAGNOSTIC_LOCK:
                provider_authority_module._PROVIDER_DIAGNOSTICS.clear()
                provider_authority_module._PROVIDER_DIAGNOSTICS.update(saved)
                provider_authority_module._PROVIDER_DIAGNOSTIC_TOTAL = saved_total

    def test_transcript_indexes_are_lru_bounded_and_generations_supersede(self):
        _TRANSCRIPT_INDEXES.clear()
        try:
            for index in range(MAX_TRANSCRIPT_INDEXES + 1):
                _transcript_index(("claude", Path(f"/transcript/{index}"), 1))
            self.assertEqual(len(_TRANSCRIPT_INDEXES), MAX_TRANSCRIPT_INDEXES)
            self.assertNotIn(("claude", Path("/transcript/0"), 1), _TRANSCRIPT_INDEXES)
            _transcript_index(("claude", Path("/transcript/1"), 2))
            self.assertNotIn(("claude", Path("/transcript/1"), 1), _TRANSCRIPT_INDEXES)
            self.assertIn(("claude", Path("/transcript/1"), 2), _TRANSCRIPT_INDEXES)
        finally:
            _TRANSCRIPT_INDEXES.clear()

    def test_runtime_provider_selection_requires_normalized_styled_rows(self):
        current = binding("claude", Path("/approved/session.jsonl"))
        snapshot = SimpleNamespace(
            pane_id="%9",
            alternate=True,
            cols=12,
            seed_history=0,
            physical_rows=["● \x1b[1manswer\x1b[22m    "],
            plain_physical_rows=["● answer    "],
        )
        with patch.dict(os.environ, {"MOBILE_TERMINAL_PROVIDER_AUTHORITY": "enforce"}), patch(
            "provider_authority.resolve_provider_binding",
            return_value=(current, None),
        ), patch(
            "provider_authority.authoritative_provider_match",
            return_value=MatchResult(True, text="answer"),
        ) as match:
            result = provider_selection(snapshot, 2, 0, 8, 0)
        self.assertTrue(result.owned)
        styles = match.call_args.kwargs["style_rows"]
        self.assertEqual(styles[0][2], (2, 3, "strong"))


class ProviderSelectionQuarantineTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        self.root = self.home / ".claude" / "projects"
        self.path = self.root / f"{SESSION_ID}.jsonl"
        self.source = "wrapped Claude response exactly"
        write_jsonl(self.path, [claude_text(self.source)])
        self.binding = binding("claude", self.path)
        self.cols = 16
        self.candidate = render_semantic_candidate(
            AssistantTextRecord(
                "claude",
                f"claude:{SESSION_ID}:msg_1",
                f"claude:{SESSION_ID}:msg_1:record-1",
                self.source,
                1,
            ),
            version="2.1.241",
            cols=self.cols,
        )
        self.assertGreater(len(self.candidate.plain_rows), 1)

    def tearDown(self):
        self.temporary.cleanup()

    def select(self, plain_rows, styled_rows, start, end):
        snapshot = SimpleNamespace(
            pane_id="%9",
            alternate=True,
            cols=self.cols,
            seed_history=0,
            physical_rows=list(styled_rows),
            plain_physical_rows=list(plain_rows),
        )

        def match(*args, **kwargs):
            kwargs["proc_start_reader"] = lambda pid: "77"
            kwargs["proc_environ_reader"] = lambda pid: {"TMUX_PANE": "%9"}
            return authoritative_provider_match(*args, **kwargs)

        with patch.dict(
            os.environ,
            {"MOBILE_TERMINAL_PROVIDER_AUTHORITY": "enforce"},
        ), patch(
            "provider_authority.resolve_provider_binding",
            return_value=(self.binding, None),
        ), patch(
            "provider_authority.authoritative_provider_match",
            side_effect=match,
        ):
            return provider_selection(
                snapshot,
                start[1],
                start[0],
                end[1],
                end[0],
                home=self.home,
            )

    def candidate_selection(self, placement):
        return (
            (
                placement + self.candidate.selection_start[0],
                self.candidate.selection_start[1],
            ),
            (
                placement + self.candidate.selection_end[0],
                self.candidate.selection_end[1],
            ),
        )

    def assert_rejected(self, plain, styled, reason):
        rows = (plain, *self.candidate.plain_rows)
        styles = (styled, *verified_styled_rows(self.candidate))
        with self.assertRaises(ProviderAuthorityError) as raised:
            self.select(rows, styles, (0, 0), (0, 1))
        self.assertEqual(raised.exception.reason, reason)

    def test_wrapped_candidate_matches_with_unsafe_rows_before_and_after(self):
        for grapheme in ("✳", "⏺", "😀", "©"):
            for position in ("before", "after"):
                with self.subTest(grapheme=grapheme, position=position):
                    if position == "before":
                        rows = (grapheme, *self.candidate.plain_rows)
                        styles = (grapheme, *verified_styled_rows(self.candidate))
                        placement = 1
                    else:
                        rows = (*self.candidate.plain_rows, grapheme)
                        styles = (*verified_styled_rows(self.candidate), grapheme)
                        placement = 0
                    start, end = self.candidate_selection(placement)
                    result = self.select(rows, styles, start, end)
                    self.assertTrue(result.owned)
                    self.assertEqual(result.text, self.source)
                    self.assertEqual(result.text, self.candidate.copy_text)

    def test_each_captured_row_failure_is_quarantined_off_selection(self):
        ordinary = "ordinary".ljust(self.cols)
        cases = (
            ("✳", "✳", "unsafe-captured-wide-cell"),
            ("́", "́", "invalid-captured-cell"),
            (" " * (self.cols - 1) + "界", " " * (self.cols - 1) + "界", "captured-row-overflow"),
            (ordinary, "\x1b[999m" + ordinary, "unsupported-styled-row"),
            (ordinary, "different".ljust(self.cols), "styled-row-text-mismatch"),
        )
        for plain, styled, reason in cases:
            with self.subTest(reason=reason):
                rows = (plain, *self.candidate.plain_rows)
                styles = (styled, *verified_styled_rows(self.candidate))
                start, end = self.candidate_selection(1)
                result = self.select(rows, styles, start, end)
                self.assertTrue(result.owned)
                self.assertEqual(result.text, self.candidate.copy_text)

    def test_selecting_quarantined_row_propagates_original_reason(self):
        ordinary = "ordinary".ljust(self.cols)
        for plain, styled, reason in (
            ("✳", "✳", "unsafe-captured-wide-cell"),
            ("́", "́", "invalid-captured-cell"),
            (" " * (self.cols - 1) + "界", " " * (self.cols - 1) + "界", "captured-row-overflow"),
            (ordinary, "\x1b[999m" + ordinary, "unsupported-styled-row"),
            (ordinary, "different".ljust(self.cols), "styled-row-text-mismatch"),
        ):
            with self.subTest(reason=reason):
                self.assert_rejected(plain, styled, reason)

    def test_quarantine_inside_candidate_blocks_spanning_placement(self):
        plain_rows = list(self.candidate.plain_rows)
        styled_rows = list(verified_styled_rows(self.candidate))
        plain_rows[1] = "✳"
        styled_rows[1] = "✳"
        with self.assertRaises(ProviderAuthorityError) as raised:
            self.select(
                plain_rows,
                styled_rows,
                (self.candidate.selection_start[0], self.candidate.selection_start[1]),
                (0, self.cols),
            )
        self.assertEqual(
            raised.exception.reason,
            "no-unique-canonical-provider-block",
        )

    def test_duplicate_candidate_remains_ambiguous_with_quarantine(self):
        plain_rows = (
            *self.candidate.plain_rows,
            "✳",
            *self.candidate.plain_rows,
        )
        styled_candidate = verified_styled_rows(self.candidate)
        styled_rows = (*styled_candidate, "✳", *styled_candidate)
        start, end = self.candidate_selection(0)
        with self.assertRaises(ProviderAuthorityError) as raised:
            self.select(plain_rows, styled_rows, start, end)
        self.assertEqual(raised.exception.reason, "ambiguous-provider-block")


class CodexOwnershipTest(unittest.TestCase):
    def setUp(self):
        self.binding = binding("codex", Path("/approved/session.jsonl"))
        self.snapshot = SimpleNamespace(
            alternate=False,
            history=20,
            history_limit=100,
            cursor_y=4,
        )
        self.cache = {
            "ownershipRanges": [
                {
                    "sessionId": SESSION_ID,
                    "startRow": 22,
                    "endRow": None,
                    "historyAtStart": 20,
                    "historyLimit": 100,
                    "alternate": False,
                    "saturated": False,
                }
            ]
        }

    def test_normal_buffer_range_is_deterministic(self):
        self.assertTrue(_selection_is_owned(self.binding, self.cache, self.snapshot, 2, 4))
        self.assertFalse(_selection_is_owned(self.binding, self.cache, self.snapshot, 0, 1))

    def test_missing_stale_and_saturated_ranges_fail_closed(self):
        with self.assertRaisesRegex(ProviderAuthorityError, "ownership-unavailable"):
            _selection_is_owned(self.binding, {}, self.snapshot, 2, 4)
        stale = SimpleNamespace(**{**vars(self.snapshot), "history": 100})
        with self.assertRaisesRegex(ProviderAuthorityError, "ownership-stale"):
            _selection_is_owned(self.binding, self.cache, stale, -78, -76)


class ProviderAuthorityFlowTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "root"
        self.path = self.root / f"{SESSION_ID}.jsonl"
        write_jsonl(self.path, [claude_text("final answer")])
        self.binding = binding("claude", self.path)
        self.record = AssistantTextRecord(
            "claude",
            f"claude:{SESSION_ID}:msg_1",
            f"claude:{SESSION_ID}:msg_1:record-1",
            "final answer",
            1,
        )
        self.candidate = render_semantic_candidate(self.record, version="2.1.241", cols=24)

    def tearDown(self):
        self.temporary.cleanup()

    def match(self, **kwargs):
        return authoritative_provider_match(
            self.binding,
            TranscriptIndex(),
            transcript_root=self.root,
            cols=24,
            plain_rows=self.candidate.plain_rows,
            selection_start=self.candidate.selection_start,
            selection_end=self.candidate.selection_end,
            proc_start_reader=lambda pid: "77",
            proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
            **kwargs,
        )

    def test_complete_match_revalidates_and_returns_only_supported_text(self):
        result = self.match()
        self.assertTrue(result.matched)
        self.assertEqual(result.text, "final answer")

    def test_authoritative_match_enforces_production_dot_style(self):
        correct = normalize_styled_rows(
            verified_styled_rows(self.candidate), self.candidate.plain_rows
        )
        self.assertTrue(self.match(style_rows=correct).matched)
        for foreground in (114, 246):
            wrong = normalize_styled_rows(
                verified_styled_rows(self.candidate, dot_fg=foreground),
                self.candidate.plain_rows,
            )
            self.assertFalse(self.match(style_rows=wrong).matched)

    def test_unsupported_historical_record_does_not_poison_supported_match(self):
        write_jsonl(
            self.path,
            [
                claude_text("```\nunverified\n```", message_id="msg_0", record_uuid="record-0"),
                claude_text("final answer"),
            ],
        )
        result = self.match()
        self.assertTrue(result.matched)
        self.assertEqual(result.text, "final answer")

    def test_inline_code_before_and_after_supported_island_matches_exact_source(self):
        source = "before `code`\nselected  fragment  \nafter `code`"
        write_jsonl(self.path, [claude_text(source)])
        prefix = "before `code`\n"
        island = render_semantic_candidate(
            replace(self.record, text="selected  fragment  "),
            version="2.1.241",
            cols=24,
            source_byte_offset=len(prefix.encode("utf-8")),
            first_record_island=False,
        )
        result = authoritative_provider_match(
            self.binding,
            TranscriptIndex(),
            transcript_root=self.root,
            cols=24,
            plain_rows=island.plain_rows,
            selection_start=island.selection_start,
            selection_end=island.selection_end,
            proc_start_reader=lambda pid: "77",
            proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
        )
        self.assertTrue(result.matched)
        self.assertEqual(result.text, "selected  fragment  ")
        self.assertEqual(result.source_start, len(prefix.encode("utf-8")))
        self.assertEqual(
            result.source_end,
            len((prefix + "selected  fragment  ").encode("utf-8")),
        )

    def test_inline_code_island_touching_and_crossing_fail_closed(self):
        source = "before `code`\nselected fragment\nafter `code`"
        write_jsonl(self.path, [claude_text(source)])
        island = render_semantic_candidate(
            replace(self.record, text="selected fragment"),
            version="2.1.241",
            cols=24,
            source_byte_offset=len("before `code`\n".encode("utf-8")),
            first_record_island=False,
        )
        rows = ("before".ljust(24), *island.plain_rows, "after".ljust(24))

        def match(start, end):
            return authoritative_provider_match(
                self.binding,
                TranscriptIndex(),
                transcript_root=self.root,
                cols=24,
                plain_rows=rows,
                selection_start=start,
                selection_end=end,
                proc_start_reader=lambda pid: "77",
                proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
            )

        touching = match((0, 24), (1, island.selection_end[1]))
        crossing = match((1, island.selection_start[1]), (2, 1))
        self.assertFalse(touching.matched)
        self.assertFalse(crossing.matched)
        self.assertEqual(
            touching.internal_reason,
            "no-unique-canonical-provider-block",
        )
        self.assertEqual(
            crossing.internal_reason,
            "no-unique-canonical-provider-block",
        )

    def test_duplicate_supported_inline_code_islands_are_ambiguous(self):
        source = "`before`\nsame\n`middle`\nsame\n`after`"
        write_jsonl(self.path, [claude_text(source)])
        island = render_semantic_candidate(
            replace(self.record, text="same"),
            version="2.1.241",
            cols=24,
            first_record_island=False,
        )
        rows = (
            "before".ljust(24),
            *island.plain_rows,
            "middle".ljust(24),
            *island.plain_rows,
            "after".ljust(24),
        )
        result = authoritative_provider_match(
            self.binding,
            TranscriptIndex(),
            transcript_root=self.root,
            cols=24,
            plain_rows=rows,
            selection_start=(1, island.selection_start[1]),
            selection_end=(1, island.selection_end[1]),
            proc_start_reader=lambda pid: "77",
            proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
        )
        self.assertFalse(result.matched)
        self.assertEqual(result.internal_reason, "ambiguous-provider-block")

    def test_historical_mixed_width_rows_normalize_to_current_geometry(self):
        candidate = render_semantic_candidate(self.record, version="2.1.241", cols=180)
        rows = ("unrelated".ljust(90), candidate.plain_rows[0][:45])
        styled_rows = (
            rows[0],
            f"\x1b[38;5;231m{rows[1][0]}\x1b[0m{rows[1][1:]}",
        )
        styles = normalize_styled_rows(styled_rows, rows)
        result = authoritative_provider_match(
            self.binding,
            TranscriptIndex(),
            transcript_root=self.root,
            cols=180,
            plain_rows=rows,
            selection_start=(1, candidate.selection_start[1]),
            selection_end=(1, candidate.selection_end[1]),
            style_rows=styles,
            proc_start_reader=lambda pid: "77",
            proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
        )
        self.assertTrue(result.matched)
        self.assertEqual(result.text, "final answer")
        self.assertEqual([len(row) for row in rows], [90, 45])

    def test_inline_code_island_matches_historical_width_with_exact_copy(self):
        source = "before `code`\nselected  fragment  \nafter `code`"
        write_jsonl(self.path, [claude_text(source)])
        island = render_semantic_candidate(
            replace(self.record, text="selected  fragment  "),
            version="2.1.241",
            cols=180,
            source_byte_offset=len("before `code`\n".encode("utf-8")),
            first_record_island=False,
        )
        rows = ("unrelated".ljust(90), island.plain_rows[0][:45])
        result = authoritative_provider_match(
            self.binding,
            TranscriptIndex(),
            transcript_root=self.root,
            cols=180,
            plain_rows=rows,
            selection_start=(1, island.selection_start[1]),
            selection_end=(1, island.selection_end[1]),
            proc_start_reader=lambda pid: "77",
            proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
        )
        self.assertTrue(result.matched)
        self.assertEqual(result.text, "selected  fragment  ")

    def test_historical_width_overflow_and_unsafe_wide_cells_fail_closed(self):
        for rows, reason in (
            (("x" * 181,), "captured-row-overflow"),
            (("😀",), "unsafe-captured-wide-cell"),
        ):
            with self.subTest(reason=reason):
                result = authoritative_provider_match(
                    self.binding,
                    TranscriptIndex(),
                    transcript_root=self.root,
                    cols=180,
                    plain_rows=rows,
                    selection_start=self.candidate.selection_start,
                    selection_end=self.candidate.selection_end,
                    proc_start_reader=lambda pid: "77",
                    proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
                )
                self.assertFalse(result.matched)
                self.assertEqual(result.internal_reason, reason)

    def test_compilable_unsupported_alias_poisons_only_matching_geometry(self):
        write_jsonl(
            self.path,
            [
                claude_text(
                    "unused",
                    message_id="msg_0",
                    record_uuid="record-0",
                    content=[
                        {"type": "text", "text": "final answer"},
                        {"type": "tool_use", "id": "synthetic"},
                    ],
                ),
                claude_text("final answer"),
            ],
        )
        result = self.match()
        self.assertFalse(result.matched)
        self.assertEqual(result.internal_reason, "unsupported-provider-alias")

        write_jsonl(
            self.path,
            [
                claude_text(
                    "unused",
                    message_id="msg_0",
                    record_uuid="record-0",
                    content=[
                        {"type": "text", "text": "unrelated history"},
                        {"type": "tool_use", "id": "synthetic"},
                    ],
                ),
                claude_text("final answer"),
            ],
        )
        result = self.match()
        self.assertTrue(result.matched)
        self.assertEqual(result.text, "final answer")

    def test_render_failing_unsupported_raw_collision_fails_closed(self):
        write_jsonl(
            self.path,
            [
                claude_text(
                    "unused",
                    message_id="msg_0",
                    record_uuid="record-0",
                    content=[
                        {"type": "text", "text": "```\nfinal answer\n```"},
                        {"type": "tool_use", "id": "synthetic"},
                    ],
                ),
                claude_text("final answer"),
            ],
        )
        result = self.match()
        self.assertFalse(result.matched)
        self.assertEqual(result.internal_reason, "unsupported-provider-alias")

        write_jsonl(
            self.path,
            [
                claude_text(
                    "```\nfinal answer\n```",
                    message_id="msg_0",
                    record_uuid="record-0",
                ),
                claude_text("final answer"),
            ],
        )
        result = self.match()
        self.assertFalse(result.matched)
        self.assertEqual(result.internal_reason, "unsupported-provider-alias")

    def test_html_entity_aliases_poison_but_unrelated_entities_do_not(self):
        source = "left > right"
        candidate = render_semantic_candidate(
            AssistantTextRecord("claude", "entity", "entity", source, 1),
            version="2.1.241",
            cols=24,
        )

        def match_candidate():
            return authoritative_provider_match(
                self.binding,
                TranscriptIndex(),
                transcript_root=self.root,
                cols=24,
                plain_rows=candidate.plain_rows,
                selection_start=candidate.selection_start,
                selection_end=candidate.selection_end,
                proc_start_reader=lambda pid: "77",
                proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
            )

        aliases = (
            (
                "named-unsupported",
                claude_text(
                    "unused",
                    message_id="msg_0",
                    record_uuid="record-0",
                    content=[
                        {"type": "text", "text": "left &gt; right"},
                        {"type": "tool_use", "id": "synthetic"},
                    ],
                ),
            ),
            (
                "decimal",
                claude_text(
                    "left &#62; right",
                    message_id="msg_0",
                    record_uuid="record-0",
                ),
            ),
            (
                "hex-inside-markdown",
                claude_text(
                    "```\nleft &#x3e; right\n```",
                    message_id="msg_0",
                    record_uuid="record-0",
                ),
            ),
        )
        for name, alias in aliases:
            with self.subTest(alias=name):
                write_jsonl(self.path, [alias, claude_text(source)])
                result = match_candidate()
                self.assertFalse(result.matched)
                self.assertEqual(
                    result.internal_reason,
                    "unsupported-provider-alias",
                )

        write_jsonl(
            self.path,
            [
                claude_text(
                    "```\nother &amp; value\n```",
                    message_id="msg_0",
                    record_uuid="record-0",
                ),
                claude_text(source),
            ],
        )
        result = match_candidate()
        self.assertTrue(result.matched)
        self.assertEqual(result.text, source)

    def test_multi_content_isolation_and_repeated_ids_fail_closed(self):
        write_jsonl(
            self.path,
            [
                claude_text(
                    "unused",
                    content=[
                        {"type": "text", "text": "other block"},
                        {"type": "text", "text": "final answer"},
                    ],
                )
            ],
        )
        result = self.match()
        self.assertTrue(result.matched)
        self.assertEqual(result.text, "final answer")

        write_jsonl(
            self.path,
            [
                claude_text("final answer", message_id="same", record_uuid="record-a"),
                claude_text("final answer", message_id="same", record_uuid="record-b"),
            ],
        )
        result = self.match()
        self.assertFalse(result.matched)
        self.assertEqual(result.internal_reason, "ambiguous-provider-block")

    def test_append_during_matching_fails_closed(self):
        def append():
            with self.path.open("ab") as output:
                output.write((json.dumps(claude_text("later", message_id="msg_2")) + "\n").encode())

        result = self.match(before_final_revalidation=append)
        self.assertFalse(result.matched)
        self.assertEqual(result.public_error, STALE_MESSAGE)
        self.assertEqual(result.internal_reason, "transcript-changed")

    def test_process_revalidation_and_provider_version_fail_closed(self):
        calls = 0

        def process_start(pid):
            nonlocal calls
            calls += 1
            return "77" if calls == 1 else "reused"

        result = authoritative_provider_match(
            self.binding,
            TranscriptIndex(),
            transcript_root=self.root,
            cols=24,
            plain_rows=self.candidate.plain_rows,
            selection_start=self.candidate.selection_start,
            selection_end=self.candidate.selection_end,
            proc_start_reader=process_start,
            proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
        )
        self.assertFalse(result.matched)
        self.assertEqual(result.internal_reason, "process-start-mismatch")
        unsupported = binding("claude", self.path, version="future")
        result = authoritative_provider_match(
            unsupported,
            TranscriptIndex(),
            transcript_root=self.root,
            cols=24,
            plain_rows=self.candidate.plain_rows,
            selection_start=self.candidate.selection_start,
            selection_end=self.candidate.selection_end,
            proc_start_reader=lambda pid: "77",
            proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
        )
        self.assertFalse(result.matched)
        self.assertEqual(result.internal_reason, "unsupported-provider-version")

    def test_internal_exceptions_never_expose_transcript_text(self):
        result = authoritative_provider_match(
            self.binding,
            TranscriptIndex(),
            transcript_root=self.root,
            cols=24,
            plain_rows=self.candidate.plain_rows,
            selection_start=self.candidate.selection_start,
            selection_end=self.candidate.selection_end,
            proc_start_reader=lambda pid: (_ for _ in ()).throw(ValueError("final answer secret")),
            proc_environ_reader=lambda pid: {"TMUX_PANE": "%9"},
        )
        self.assertFalse(result.matched)
        self.assertEqual(result.public_error, STALE_MESSAGE)
        self.assertNotIn("final answer", repr(result))


if __name__ == "__main__":
    unittest.main()
