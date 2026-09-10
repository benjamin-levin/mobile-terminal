"""Check repaired source findings without touching services, tmux, or user data.

Run from the checkout with .venv/bin/python docs/reviews/2026-09-04/source-probes.py.
The original defects are described in the review; assertions now require repairs.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from provider_authority import ProviderAuthorityError, TranscriptIndex, renderer_profile
from server import build_composer_sync_sequence, unwrap_selected_url, utf16_cursor_to_codepoint


results = []
for provider, version in (("codex", "0.153.3"), ("claude", "2.1.261")):
    try:
        renderer_profile(provider, version)
    except ProviderAuthorityError as error:
        assert error.reason == "unsupported-provider-version"
        results.append({"provider": provider, "version": version, "reason": error.reason})
    else:
        results.append({"provider": provider, "version": version, "reason": "supported-renderer"})

TranscriptIndex()._parse_codex(
    {"type": "turn_context", "payload": {"turn_id": "synthetic", "cwd": "/tmp"}},
    SimpleNamespace(session_id="synthetic"),
    0,
)
results.append({"name": "codex-turn-context", "reason": "accepted-metadata"})

source = "https://example.com/docs\nNEXT"
actual = unwrap_selected_url(source)
assert actual == source
results.append({"name": "authored-url-hard-break", "input": source, "actual": actual})

# Browser textarea offsets are UTF-16: end=4, immediately after emoji=2.
# Moving from the end of '😀ab' to after the emoji requires two Left keys.
actual, cursor = build_composer_sync_sequence(
    "😀ab", utf16_cursor_to_codepoint("😀ab", 4),
    "😀ab", utf16_cursor_to_codepoint("😀ab", 2),
)
assert actual == "\x1b[D\x1b[D"
assert cursor == 1
results.append({"name": "utf16-cursor", "actual": actual, "expected": "\x1b[D\x1b[D"})

print(json.dumps(results, indent=2, ensure_ascii=False))
