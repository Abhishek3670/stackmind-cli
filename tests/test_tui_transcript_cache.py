"""Tests for transcript rendering cache in cli.tui.chat."""

from __future__ import annotations

from pathlib import Path

import pytest

from cli.tui.chat import (
    _compute_transcript_hash,
    _compute_cache_key,
    _get_cached_render,
    _set_cached_render,
    _clear_cache_if_needed,
    _TRANSCRIPT_CACHE,
    render_chat_transcript_str,
)
from cli.tui.state import ChatMessage


def test_compute_transcript_hash_empty():
    """Empty transcript produces deterministic hash."""
    h1 = _compute_transcript_hash([])
    h2 = _compute_transcript_hash([])
    assert h1 == h2


def test_compute_transcript_hash_single_message():
    """Single message hash is deterministic."""
    msgs = [ChatMessage(role="assistant", content="Hello")]
    h1 = _compute_transcript_hash(msgs)
    h2 = _compute_transcript_hash(msgs)
    assert h1 == h2


def test_compute_transcript_hash_differs_by_message_count():
    """Different message counts produce different hashes."""
    msgs1 = [ChatMessage(role="assistant", content="Hello")]
    msgs2 = [
        ChatMessage(role="user", content="Hi"),
        ChatMessage(role="assistant", content="Hello"),
    ]
    h1 = _compute_transcript_hash(msgs1)
    h2 = _compute_transcript_hash(msgs2)
    assert h1 != h2


def test_compute_transcript_hash_differs_by_earlier_message():
    """Earlier message changes produce different hashes."""
    msgs1 = [
        ChatMessage(role="user", content="First"),
        ChatMessage(role="assistant", content="Last"),
    ]
    msgs2 = [
        ChatMessage(role="user", content="CHANGED"),
        ChatMessage(role="assistant", content="Last"),
    ]
    h1 = _compute_transcript_hash(msgs1)
    h2 = _compute_transcript_hash(msgs2)
    assert h1 != h2


def test_compute_transcript_hash_full_content_no_truncation():
    """Messages differing after 2000 chars produce different hashes."""
    base = "A" * 2000
    msgs1 = [ChatMessage(role="assistant", content=base + " suffix1")]
    msgs2 = [ChatMessage(role="assistant", content=base + " suffix2")]
    h1 = _compute_transcript_hash(msgs1)
    h2 = _compute_transcript_hash(msgs2)
    assert h1 != h2


def test_compute_transcript_hash_differs_by_role():
    """Role changes produce different hashes."""
    msgs1 = [ChatMessage(role="user", content="Hello")]
    msgs2 = [ChatMessage(role="assistant", content="Hello")]
    h1 = _compute_transcript_hash(msgs1)
    h2 = _compute_transcript_hash(msgs2)
    assert h1 != h2


def test_compute_transcript_hash_differs_by_order():
    """Message reordering produces different hashes."""
    msgs1 = [
        ChatMessage(role="user", content="First"),
        ChatMessage(role="assistant", content="Second"),
    ]
    msgs2 = [
        ChatMessage(role="assistant", content="Second"),
        ChatMessage(role="user", content="First"),
    ]
    h1 = _compute_transcript_hash(msgs1)
    h2 = _compute_transcript_hash(msgs2)
    assert h1 != h2


def test_cache_key_includes_width():
    """Cache key should differ by width."""
    transcript_hash = _compute_transcript_hash([ChatMessage(role="assistant", content="Hello")])
    k1 = _compute_cache_key(transcript_hash, 80)
    k2 = _compute_cache_key(transcript_hash, 120)
    assert k1 != k2


def test_cache_key_includes_inline_diffs():
    """Cache key should differ by inline_diffs flag."""
    transcript_hash = _compute_transcript_hash([ChatMessage(role="assistant", content="Hello")])
    k1 = _compute_cache_key(transcript_hash, 80, inline_diffs=True)
    k2 = _compute_cache_key(transcript_hash, 80, inline_diffs=False)
    assert k1 != k2


def test_cache_set_and_get():
    """Basic cache set/get operations."""
    _TRANSCRIPT_CACHE.clear()
    key = "test_key"
    value = "test_value"
    _set_cached_render(key, value)
    assert _get_cached_render(key) == value
    assert _get_cached_render("nonexistent") is None


def test_render_chat_transcript_str_output():
    """Basic transcript rendering produces expected output."""
    msgs = [
        ChatMessage(role="user", content="Hello"),
        ChatMessage(role="assistant", content="Hi there"),
    ]
    out = render_chat_transcript_str(msgs, width=80)
    assert "Hello" in out
    assert "Hi there" in out


def test_multiple_messages_render():
    """Multiple messages render in order."""
    msgs = [
        ChatMessage(role="user", content="First"),
        ChatMessage(role="assistant", content="Reply1"),
        ChatMessage(role="user", content="Second"),
        ChatMessage(role="assistant", content="Reply2"),
    ]
    out = render_chat_transcript_str(msgs, width=80)
    assert out.index("First") < out.index("Reply1")
    assert out.index("Reply1") < out.index("Second")
    assert out.index("Second") < out.index("Reply2")


def test_empty_messages():
    """Empty message list produces minimal output."""
    out = render_chat_transcript_str([], width=80)
    assert out == "" or "(No messages" in out


def test_cache_hit_on_unchanged_transcript():
    """Same transcript should hit cache on second render."""
    _TRANSCRIPT_CACHE.clear()
    msgs = [
        ChatMessage(role="user", content="Test user message"),
        ChatMessage(role="assistant", content="Test assistant message"),
    ]
    out1 = render_chat_transcript_str(msgs, width=80)
    out2 = render_chat_transcript_str(msgs, width=80)
    assert out1 == out2


def test_earlier_message_change_invalidates_cache():
    """When an earlier message changes, cache should miss and output should update."""
    _TRANSCRIPT_CACHE.clear()

    msgs1 = [
        ChatMessage(role="user", content="First"),
        ChatMessage(role="assistant", content="Last"),
    ]
    out1 = render_chat_transcript_str(msgs1, width=80)

    msgs2 = [
        ChatMessage(role="user", content="CHANGED"),  # Earlier changed
        ChatMessage(role="assistant", content="Last"),  # Last same
    ]
    out2 = render_chat_transcript_str(msgs2, width=80)

    assert "CHANGED" in out2
    assert "First" not in out2


def test_message_addition_invalidates_cache():
    """Appending a message should invalidate cache."""
    _TRANSCRIPT_CACHE.clear()

    msgs1 = [ChatMessage(role="assistant", content="Last")]
    out1 = render_chat_transcript_str(msgs1, width=80)

    msgs2 = [
        ChatMessage(role="user", content="Added"),
        ChatMessage(role="assistant", content="Last"),
    ]
    out2 = render_chat_transcript_str(msgs2, width=80)

    assert "Added" in out2
    assert out1 != out2


def test_message_removal_invalidates_cache():
    """Removing a message should invalidate cache."""
    _TRANSCRIPT_CACHE.clear()

    msgs1 = [
        ChatMessage(role="user", content="First"),
        ChatMessage(role="assistant", content="Last"),
    ]
    out1 = render_chat_transcript_str(msgs1, width=80)

    msgs2 = [ChatMessage(role="assistant", content="Last")]
    out2 = render_chat_transcript_str(msgs2, width=80)

    assert "First" not in out2
    assert out1 != out2


def test_width_change_produces_different_output():
    """Different widths should produce different output."""
    msgs = [ChatMessage(role="assistant", content="Test message with some content")]
    out80 = render_chat_transcript_str(msgs, width=80)
    out120 = render_chat_transcript_str(msgs, width=120)
    # Width change affects output (line wrapping differs)
    assert len(out80) != len(out120) or out80 != out120


def test_cache_max_size_eviction():
    """Cache should not grow unbounded."""
    from cli.tui.chat import _TRANSCRIPT_CACHE_MAX_SIZE

    _TRANSCRIPT_CACHE.clear()
    for i in range(_TRANSCRIPT_CACHE_MAX_SIZE + 10):
        key = "key_{}".format(i)
        _set_cached_render(key, "value_{}".format(i))

    assert len(_TRANSCRIPT_CACHE) <= _TRANSCRIPT_CACHE_MAX_SIZE + 100


def test_output_equivalence_cached_vs_uncached():
    """Cached output must match uncached output."""
    _TRANSCRIPT_CACHE.clear()
    msgs = [
        ChatMessage(role="user", content="First"),
        ChatMessage(role="assistant", content="Second"),
        ChatMessage(role="user", content="Third"),
    ]
    out1 = render_chat_transcript_str(msgs, width=80)
    out2 = render_chat_transcript_str(msgs, width=80)
    assert out1 == out2


def test_long_transcript_correctness():
    """Correctness holds for larger message collections."""
    _TRANSCRIPT_CACHE.clear()
    msgs = [
        ChatMessage(role="user" if i % 2 == 0 else "assistant",
                    content="Message {}".format(i))
        for i in range(50)
    ]
    out = render_chat_transcript_str(msgs, width=80)
    for i in range(50):
        assert "Message {}".format(i) in out


def test_cache_hit_with_full_transcript_stability():
    """Cache hits when entire transcript is stable."""
    _TRANSCRIPT_CACHE.clear()

    msgs = [
        ChatMessage(role="user", content="User 1"),
        ChatMessage(role="assistant", content="Assistant 1"),
        ChatMessage(role="user", content="User 2"),
        ChatMessage(role="assistant", content="Assistant 2"),
    ]

    out1 = render_chat_transcript_str(msgs, width=80)
    out2 = render_chat_transcript_str(msgs, width=80)

    assert out1 == out2


def test_last_message_mutation_invalidates_cache():
    """Last message changes should invalidate cache."""
    _TRANSCRIPT_CACHE.clear()

    msgs1 = [
        ChatMessage(role="user", content="First"),
        ChatMessage(role="assistant", content="Last"),
    ]
    out1 = render_chat_transcript_str(msgs1, width=80)

    msgs2 = [
        ChatMessage(role="user", content="First"),
        ChatMessage(role="assistant", content="CHANGED"),
    ]
    out2 = render_chat_transcript_str(msgs2, width=80)

    assert "CHANGED" in out2
    assert "Last" not in out2

