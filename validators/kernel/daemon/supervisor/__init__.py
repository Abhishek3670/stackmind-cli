"""Deterministic lifecycle Supervisor for StackMind product delivery.

Drives the complete multi-agent lifecycle from CEO product request through
GitOps release commit.  This is *deterministic orchestration logic*, NOT an
autonomous LLM agent - it reads disk/session state after each operation
completes and dispatches the next valid action.

Layout
------
``models``    phases/results/errors/exit codes/RunState
``core``      LifecycleSupervisor, assembled from the mixins below
``phases``    linear phase handlers
``execution`` dispatch, retries, QA gates, operation tracking
``review``    integration/final review and readiness gates
``recovery``  architect repair and recovery decisions
``gitops``    release phase
``controls``  human/operator controls
``status``    delivery status rendering
``release``   release-commit and archiving helpers

The names re-exported here preserve the historical module import surface.
"""

# Re-export shim for the historical module-level surface.
# ruff: noqa: F401

from .core import LifecycleSupervisor
from .models import (
    _AGENT_ROLE_KEYWORDS,
    _OPERATION_TERMINAL,
    _ROLE_TO_PRIMARY_AGENT,
    DEFAULT_TIMEOUTS,
    EXIT_BUDGET_EXHAUSTED,
    EXIT_FAILURE,
    EXIT_HEADLESS_TIMEOUT,
    EXIT_INTEGRITY_VIOLATION,
    EXIT_PLAN_REJECTED,
    EXIT_RETRIES_EXHAUSTED,
    EXIT_SUCCESS,
    GOVERNANCE_FAILURE_CODES,
    TRANSIENT_RETRYABLE_CODES,
    AdvanceResult,
    BudgetExhaustedError,
    DaemonRestartFailClosedError,
    OperationContentionError,
    Phase,
    ReplayAttackError,
    RunState,
    StateIntegrityError,
    SupervisorStateManager,
    _extract_op_error,
    _strip_code_fences,
)
from .release import (
    archive_blocked_work_orders,
    archive_completed_work_orders,
    create_gitops_commit,
    format_release_commit_message,
)

__all__ = [
    "AdvanceResult",
    "LifecycleSupervisor",
    "Phase",
    "RunState",
    "archive_completed_work_orders",
    "archive_blocked_work_orders",
    "create_gitops_commit",
    "format_release_commit_message",
]
