"""Tests for thinking delta formatting with preserved whitespace and punctuation (Phase 38)."""

import pytest
from cli.tui.state import AutonomousDeliveryState


class TestThinkingDeltaFormatting:
    """Tests ensuring thinking deltas preserve original whitespace and punctuation."""

    def test_normal_sentence_preserves_spacing(self):
        """Verify normal sentences preserve word spacing."""
        state = AutonomousDeliveryState()

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "The user is asking a general question."},
        })

        assert state.current_turn_thinking == "The user is asking a general question."

    def test_partial_word_deltas(self):
        """Verify partial word chunks combine properly."""
        state = AutonomousDeliveryState()

        # Simulate Ollama emitting partial chunks
        chunks = ["Ex", "plain", " how", " are", " you", "?"]
        for chunk in chunks:
            state.process_event({
                "name": "reasoning.delta",
                "payload": {"text": chunk},
            })

        # Should combine with space before chunks that don't start with space
        assert "Ex plain how are you ?" == state.current_turn_thinking

    def test_leading_space_chunks(self):
        """Verify chunks with leading space preserve that spacing."""
        state = AutonomousDeliveryState()

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "Hello"},
        })

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": " world"},  # leading space
        })

        # " " should be preserved
        assert "Hello world" == state.current_turn_thinking

    def test_punctuation_with_leading_space(self):
        """Verify chunks like ' ,' preserve the leading space."""
        state = AutonomousDeliveryState()

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "Hello"},
        })

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": ","},
        })

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": " world"},
        })

        # Comma without leading space gets space added before it
        assert "Hello , world" == state.current_turn_thinking

    def test_whitespace_only_chunks_ignored(self):
        """Verify whitespace-only chunks don't corrupt text."""
        state = AutonomousDeliveryState()

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "Hello"},
        })

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "   "},  # whitespace only
        })

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "world"},
        })

        assert "Hello world" == state.current_turn_thinking

    def test_paragraph_breaks_preserved(self):
        """Verify chunks starting with newline preserve paragraph breaks."""
        state = AutonomousDeliveryState()

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "First paragraph"},
        })

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "\n\nSecond paragraph"},
        })

        # Newline at start should be preserved
        assert "First paragraph\n\nSecond paragraph" == state.current_turn_thinking

    def test_multiple_normal_chunks(self):
        """Verify multiple normal chunks combine properly."""
        state = AutonomousDeliveryState()

        for chunk in ["Hello", "world", "this", "is", "test"]:
            state.process_event({
                "name": "reasoning.delta",
                "payload": {"text": chunk},
            })

        assert "Hello world this is test" == state.current_turn_thinking

    def test_trailing_space_preserved(self):
        """Verify trailing spaces in chunks are preserved."""
        state = AutonomousDeliveryState()

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "Hello "},
        })

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "world"},
        })

        assert "Hello world" == state.current_turn_thinking

    def test_empty_chunks_ignored(self):
        """Verify empty chunks don't corrupt text."""
        state = AutonomousDeliveryState()

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "Hello"},
        })

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": ""},
        })

        state.process_event({
            "name": "reasoning.delta",
            "payload": {"text": "world"},
        })

        assert "Hello world" == state.current_turn_thinking


class TestSidebarRemoved:
    """Tests verifying Thinking section was removed from sidebar."""

    def test_runtime_panel_no_thinking_section(self):
        """Verify sidebar doesn't contain THINKING section."""
        from cli.tui.runtime_panel import render_runtime_panel_str
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()
        state.current_turn_thinking = "Some thinking that should not appear in sidebar"

        content = render_runtime_panel_str(
            width=40, height=20, state=state,
        )

        # Thinking section should not be present
        assert "THINKING" not in content.upper()

    def test_runtime_panel_preserves_other_sections(self):
        """Verify sidebar still shows other sections."""
        from cli.tui.runtime_panel import render_runtime_panel_str
        from cli.tui.state import AutonomousDeliveryState

        state = AutonomousDeliveryState()

        content = render_runtime_panel_str(
            width=40, height=20, state=state,
        )

        # Other sections should still be present
        assert "AGENTS" in content.upper() or "WORK ORDERS" in content.upper()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
