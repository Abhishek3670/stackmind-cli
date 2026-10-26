"""Tests for thinking text formatting and wrapping (Phase 37)."""

import pytest
from cli.tui.state import AutonomousDeliveryState


class TestThinkingFormatting:
    """Tests ensuring thinking text preserves normal word spacing."""

    def test_thinking_preserves_word_spacing(self):
        """Verify thinking text preserves spaces between words."""
        state = AutonomousDeliveryState()

        # Simulate reasoning.delta with normal prose
        event = {
            "name": "reasoning.delta",
            "payload": {"text": "The user is asking a general question."},
        }

        state.process_event(event)

        # Check that spaces are preserved (not one word per line)
        assert state.current_turn_thinking == "The user is asking a general question."
        # Ensure there are actual spaces between words
        assert " " in state.current_turn_thinking

    def test_thinking_multiple_deltas_combine_without_extra_newlines(self):
        """Verify multiple thinking deltas combine without inserting unwanted newlines."""
        state = AutonomousDeliveryState()

        events = [
            {"name": "reasoning.delta", "payload": {"text": "First delta"}},
            {"name": "reasoning.delta", "payload": {"text": "Second delta"}},
            {"name": "reasoning.delta", "payload": {"text": "Third delta"}},
        ]

        for ev in events:
            state.process_event(ev)

        # Should combine with single spaces, not newlines
        assert "First delta Second delta Third delta" == state.current_turn_thinking
        # Count actual newlines - should be minimal
        newline_count = state.current_turn_thinking.count("\n")
        assert newline_count <= 1

    def test_thinking_preserves_leading_newlines(self):
        """Verify thinking text preserves leading newlines (paragraph breaks)."""
        state = AutonomousDeliveryState()

        # First thinking chunk
        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "First paragraph"},
        })

        # Second chunk with leading newline (preserved as paragraph break)
        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "\nSecond paragraph"},
        })

        # Leading newline should be preserved
        assert "First paragraph\nSecond paragraph" == state.current_turn_thinking

    def test_thinking_accumulation_with_space_separation(self):
        """Verify consecutive deltas accumulate with space separation."""
        state = AutonomousDeliveryState()

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "Hello world"},
        })

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "more text"},
        })

        # Should be separated by space
        assert "Hello world more text" == state.current_turn_thinking
        assert "\n" not in state.current_turn_thinking

    def test_thinking_empty_and_whitespace_handling(self):
        """Verify empty and whitespace-only deltas are handled correctly."""
        state = AutonomousDeliveryState()

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "   "},  # whitespace only
        })

        # Should not create thinking content from whitespace
        assert state.current_turn_thinking == ""

    def test_thinking_delta_without_spaces(self):
        """Verify single word deltas work correctly."""
        state = AutonomousDeliveryState()

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "Thinking"},
        })

        assert state.current_turn_thinking == "Thinking"

    def test_thinking_with_special_characters(self):
        """Verify thinking text preserves special characters."""
        state = AutonomousDeliveryState()

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "Analysis: value = 42, status = 'OK'"},
        })

        assert state.current_turn_thinking == "Analysis: value = 42, status = 'OK'"


class TestThinkingRendering:
    """Tests for thinking text rendering behavior."""

    def test_wrapping_helper_exists(self):
        """Verify text wrapping helper is available for thinking rendering."""
        from cli.tui.app import _wrap_text_to_width

        result = _wrap_text_to_width("This is a long line that should wrap", 20)

        assert len(result) > 0
        # Lines should be <= width + some overhead for newline
        for line in result.split("\n"):
            assert len(line) <= 25

    def test_thinking_preserves_normal_prose_formatting(self):
        """Verify thinking text renders as normal prose."""
        from cli.tui.app import _wrap_text_to_width

        thinking = "The user is asking a general question that requires careful analysis."
        wrapped = _wrap_text_to_width(thinking, 40)

        # Should not break individual words
        for line in wrapped.split("\n"):
            assert line.strip()
            # Words should remain intact (not split by newline)
            words = line.split()
            assert all(words), f"Line should have words: {line}"




class TestDetokenizationAndTimer:
    """Tests for tokenization repairs, punctuation spacing, and thinking timer duration."""

    def test_clean_detokenized_text_repairs_punctuation_and_subwords(self):
        """Verify tokenization glitches from TUI streaming are cleaned."""
        from cli.tui.chat import clean_detokenized_text

        glitches = [
            ("instructions , I need to :", "instructions, I need to:"),
            ("1 . Understand before prescribing - consult and listen", "1. Understand before prescribing - consult and listen"),
            ("2 . Co -design iteratively - don 't dump massive boilerplate", "2. Co-design iteratively - don't dump massive boilerplate"),
            ("3 . Use autonomous multi -agent dispatch when needed", "3. Use autonomous multi-agent dispatch when needed"),
            ('The user has sent ": provider " and ": roles "', 'The user has sent ":provider" and ":roles"'),
            ("- git -guard rails -cl aude -code : for hooks", "- git-guardrails-claude-code: for hooks"),
            ("role /provider", "role/provider"),
            ("system .", "system."),
        ]
        for raw, expected in glitches:
            assert clean_detokenized_text(raw) == expected

    def test_render_thinking_block_duration_calculation(self):
        """Verify thinking timer never displays 0.0s for non-empty thinking."""
        from cli.tui.chat import render_thinking_block_str

        sample_thinking = "Analyzing user prompt and planning response across multiple agent stages."

        # Case 1: Explicit elapsed time provided
        rendered_explicit = render_thinking_block_str(sample_thinking, expanded=True, elapsed=1.5, tokens=20, width=80)
        assert "Thought for 1.5s, 20 tokens" in rendered_explicit

        # Case 2: No elapsed time provided (historical/turn replay) - must compute realistic time > 0.0s
        rendered_fallback = render_thinking_block_str(sample_thinking, expanded=True, elapsed=None, tokens=243, width=80)
        assert "Thought for 6.9s, 243 tokens" in rendered_fallback
        assert "0.0s" not in rendered_fallback

    def test_state_add_message_cleans_thinking_and_records_elapsed(self):
        """Verify state.add_message cleans assistant thinking text and stores elapsed time."""
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()
        raw_think = "instructions , I need to :\n1 . Co -design iteratively"
        msg = state.add_message("assistant", "Hello", thinking=raw_think, thinking_elapsed=2.4, thinking_tokens=15)

        assert "instructions, I need to:" in msg.thinking
        assert "1. Co-design iteratively" in msg.thinking
        assert msg.thinking_elapsed == 2.4
        assert msg.thinking_tokens == 15


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
