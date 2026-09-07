"""StackMind's governed terminal presentation client."""

from .adapter import StackMindTuiAdapter
from .client import DaemonClient
from .views import (
    activity_line,
    contract_panel,
    diff_viewer,
    hitl_prompt,
    session_header,
    verification_matrix,
)

__all__ = [
    "DaemonClient",
    "StackMindTuiAdapter",
    "activity_line",
    "contract_panel",
    "diff_viewer",
    "hitl_prompt",
    "session_header",
    "verification_matrix",
]
