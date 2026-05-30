"""Tests for the compaction pre-pass (G7 / Rip 18) + multimodal token estimation.

Covers the two deferred-from-commit gaps the verification audit flagged:
1. estimate_tokens must size content-block (multimodal) content by characters +
   a flat per-image cost — the old `len(content)` counted blocks, hiding both
   vision context and the media-strip's saving (also the plan's #35b fix).
2. dedicated unit tests for _dedup_tool_results / _strip_historical_media.
"""

from __future__ import annotations

from robothor.engine.compaction import _dedup_tool_results, _strip_historical_media
from robothor.engine.context import _IMAGE_CHARS_EQUIV, estimate_tokens


class TestEstimateTokensMultimodal:
    def test_plain_string_unchanged(self):
        assert estimate_tokens([{"role": "user", "content": "x" * 40}]) == 10  # 40//4

    def test_text_blocks_in_list_are_counted(self):
        msgs = [{"role": "user", "content": [{"type": "text", "text": "x" * 40}]}]
        assert estimate_tokens(msgs) == 10  # old code returned len(list)=1 → 0

    def test_image_block_has_flat_cost(self):
        msgs = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "..."}}]}]
        assert estimate_tokens(msgs) == _IMAGE_CHARS_EQUIV // 4  # ~1500 tokens

    def test_stripping_an_image_reduces_the_estimate(self):
        with_image = [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}
        ]
        stripped = [
            {"role": "user", "content": [{"type": "text", "text": "[image omitted in compaction]"}]}
        ]
        assert estimate_tokens(stripped) < estimate_tokens(with_image)


def _tool(text: str) -> dict:
    return {"role": "tool", "tool_call_id": "c", "content": text}


class TestDedupToolResults:
    def test_earlier_identical_result_elided_newer_kept(self):
        big = "RESULT " * 60  # > 200 chars
        msgs = [_tool(big), _tool(big), _tool("other " * 60), {"role": "user", "content": "hi"}]
        out, elided = _dedup_tool_results(msgs, protect_tail=1)
        assert elided == 1
        assert "duplicate" in out[0]["content"].lower()  # earlier copy elided
        assert out[1]["content"] == big  # last occurrence kept full

    def test_recent_tail_protected(self):
        big = "RESULT " * 60
        msgs = [_tool(big), _tool(big)]  # both within protect_tail
        out, elided = _dedup_tool_results(msgs, protect_tail=2)
        assert elided == 0
        assert all(m["content"] == big for m in out)

    def test_short_results_not_deduped(self):
        msgs = [_tool("short"), _tool("short"), {"role": "user", "content": "hi"}]
        _out, elided = _dedup_tool_results(msgs, protect_tail=1)
        assert elided == 0  # < 200 chars → left alone


class TestStripHistoricalMedia:
    def test_old_image_replaced_with_placeholder(self):
        msgs = [
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "recent"},
        ]
        out, stripped = _strip_historical_media(msgs, protect_tail=1)
        assert stripped == 1
        blocks = out[0]["content"]
        assert blocks[0]["type"] == "text" and "omitted" in blocks[0]["text"]

    def test_recent_media_preserved(self):
        msgs = [
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
        ]
        out, stripped = _strip_historical_media(msgs, protect_tail=1)
        assert stripped == 0  # image is in the protected tail
        assert out[1]["content"][0]["type"] == "image_url"
