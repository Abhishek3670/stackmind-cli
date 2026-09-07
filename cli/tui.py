"""Optional CLI entrypoint helpers for the governed TUI presentation client."""

from __future__ import annotations

from validators.kernel.tui import DaemonClient, StackMindTuiAdapter


def create_tui_adapter(daemon_url: str) -> StackMindTuiAdapter:
    """Create a presentation adapter; starting a daemon remains an explicit runtime concern."""
    return StackMindTuiAdapter(DaemonClient(daemon_url))
