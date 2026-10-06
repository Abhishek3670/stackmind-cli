"""Canonical Authoring Policy — single source of truth for governed authoring requirements.

Every requirement enforced by the Authoring Readiness Gate, injected by the
deterministic contract compiler, rendered into architect authoring prompts, and
surfaced in repair diagnostics derives from this module.  Change a policy value
here and all four surfaces follow; do not re-state these rules in other modules.

Policy values are extracted from the pre-existing gate implementation
(validators/harness/authoring_readiness.py, validators/harness/d024_gate.py) and
preserve their intended semantics.
"""

from __future__ import annotations

POLICY_VERSION = "1.0.0"

# ── QA verdict channel ────────────────────────────────────────────────
# The QA worker (gemma) is instructed by its dispatch prompt to deliver its
# verdict into the architect's inbox; the QA contract must therefore authorize
# that write.  Other roles have their inbox notices written by the harness
# itself, so only QA contracts require this channel.
QA_AGENT = "gemma"
QA_VERDICT_CHANNEL = ".sync/inbox/claude/**"
QA_VERDICT_PROBE_PATH = ".sync/inbox/claude/qa-verdict.md"


def qa_verdict_channel_required(agent_id: str | None) -> bool:
    """True when the contract for this agent must authorize the QA verdict channel."""
    return str(agent_id or "").strip().lower() == QA_AGENT


# ── Milestone identity ────────────────────────────────────────────────
# Work orders link to their approved plan milestone by stable id
# (e.g. "Milestone 1", as assigned by validators/harness/plan.py).  Title
# similarity is a backward-compatible diagnostic, never the primary identity.
MILESTONE_ID_FIELD = "milestone_id"


# ── Test coverage ─────────────────────────────────────────────────────
# Every code deliverable must be covered by an explicitly declared test plan
# entry (per-file or consolidated) or a companion test artifact following the
# tests/test_<stem>.py convention.  Declared test plans are honored by both
# the authoring readiness gate and the D024 gate.
TEST_PLAN_FIELD = "test_plan"
TEST_DIR = "tests"

# ── Rule registry ─────────────────────────────────────────────────────
# Each rule: stable id, one-line requirement (rendered into prompts), and the
# remediation the repair loop surfaces when the rule is violated.
RULES: tuple[dict[str, str], ...] = (
    {
        "id": "milestone.identity",
        "requirement": (
            "Every Work Order MUST carry `milestone_id` matching the approved plan "
            "milestone it implements."
        ),
        "rationale": (
            "Milestone coverage is validated by stable identity; lexical title "
            "similarity is only a fallback diagnostic."
        ),
    },
    {
        "id": "qa.verdict_channel",
        "requirement": (
            "Contracts for gemma (QA) work orders MUST authorize the QA verdict "
            f"channel `{QA_VERDICT_CHANNEL}` in scope.allow."
        ),
        "rationale": (
            "The QA dispatch prompt instructs the QA worker to deliver its verdict "
            "to the architect inbox; an unauthorized write would fail the contract "
            "gate mid-execution."
        ),
    },
    {
        "id": "test.companion_coverage",
        "requirement": (
            "Every code deliverable MUST be covered by an explicit `test_plan` entry "
            f"mapping it to test artifact(s) under `{TEST_DIR}/`, or by a companion "
            "test following the `tests/test_<stem>.py` convention."
        ),
        "rationale": (
            "D024 blocks GitOps progression without an executed companion test; "
            "declaring coverage up front prevents a deterministic dead-end."
        ),
    },
)

_RULES_BY_ID = {rule["id"]: rule for rule in RULES}


def rule(rule_id: str) -> dict[str, str] | None:
    return _RULES_BY_ID.get(rule_id)


def authoring_prompt_digest() -> str:
    """Compact rule digest rendered into architect authoring prompts.

    Deliberately terse: the full policy rationale lives in this module and in
    the readiness diagnostics, not in the prompt.
    """
    lines = [
        f"GOVERNANCE REQUIREMENTS (canonical authoring policy v{POLICY_VERSION} "
        "— enforced fail-closed by the authoring readiness gate):",
    ]
    for index, rule_def in enumerate(RULES, start=1):
        lines.append(f"{index}. [{rule_def['id']}] {rule_def['requirement']}")
    lines.append(
        "The deterministic contract compiler auto-injects the QA verdict channel, "
        "missing milestone_ids, and a consolidated test_plan default (when a single "
        "QA suite is planned) after you finish; semantic decisions (budgets, "
        "deliverables, dependencies, scope) are yours and are never auto-filled."
    )
    return "\n".join(lines)


def repair_action(rule_id: str) -> str:
    """Required-action text surfaced in repair diagnostics for a violated rule."""
    actions = {
        "milestone.identity": (
            "Set `milestone_id` on the work order to the id of the plan milestone "
            "it implements (the compiler injects it automatically when the title "
            "matches exactly one plan milestone)."
        ),
        "qa.verdict_channel": (
            "Add `{\"module\": \""
            + QA_VERDICT_CHANNEL
            + "\"}` to the contract's scope.allow (the compiler injects this "
            "automatically when normalizing)."
        ),
        "test.companion_coverage": (
            "Declare a `test_plan` entry mapping this deliverable to test "
            f"artifact(s) under `{TEST_DIR}/` (e.g. tests/test_<stem>.py, or a "
            "consolidated suite listing the sources it covers)."
        ),
    }
    return actions.get(rule_id, "Follow the canonical authoring policy for this rule.")
