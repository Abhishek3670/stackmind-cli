"""Unit tests for the canonical authoring policy (single source of truth).

The policy module is consumed by the authoring prompt, the deterministic
contract compiler, the authoring readiness gate, and repair diagnostics.
These tests pin the policy values that those surfaces must agree on.
"""

from __future__ import annotations

from validators.harness.authoring_policy import (
    POLICY_VERSION,
    QA_VERDICT_CHANNEL,
    QA_VERDICT_PROBE_PATH,
    RULES,
    authoring_prompt_digest,
    qa_verdict_channel_required,
    repair_action,
    rule,
)


class TestPolicyValues:
    def test_qa_channel_constants_are_consistent(self) -> None:
        # The probe path the gate tests against must live under the channel glob.
        assert QA_VERDICT_CHANNEL == ".sync/inbox/claude/**"
        assert QA_VERDICT_PROBE_PATH.startswith(".sync/inbox/claude/")

    def test_qa_verdict_channel_scoped_to_gemma_only(self) -> None:
        assert qa_verdict_channel_required("gemma") is True
        assert qa_verdict_channel_required("Gemma") is True  # normalized
        assert qa_verdict_channel_required("codex") is False
        assert qa_verdict_channel_required("gemini") is False
        assert qa_verdict_channel_required(None) is False

    def test_rule_registry_covers_the_three_governed_requirements(self) -> None:
        ids = {r["id"] for r in RULES}
        assert {"milestone.identity", "qa.verdict_channel", "test.companion_coverage"} <= ids

    def test_rule_lookup_unknown_id_returns_none(self) -> None:
        assert rule("no.such.rule") is None


class TestAuthoringPromptDigest:
    def test_digest_names_every_rule_and_the_channel(self) -> None:
        digest = authoring_prompt_digest()
        assert "canonical authoring policy" in digest
        assert POLICY_VERSION in digest
        for rule_def in RULES:
            assert rule_def["id"] in digest
            assert rule_def["requirement"] in digest
        assert QA_VERDICT_CHANNEL in digest

    def test_digest_states_compiler_authority_boundary(self) -> None:
        # The prompt must tell the architect which invariants are auto-injected
        # so it does not guess at system-owned fields.
        digest = authoring_prompt_digest()
        assert "auto-injects" in digest
        assert "never auto-filled" in digest


class TestRepairActions:
    def test_every_registry_rule_has_an_action(self) -> None:
        for rule_def in RULES:
            action = repair_action(rule_def["id"])
            assert action
            assert action != "Follow the canonical authoring policy for this rule."

    def test_qa_channel_action_names_the_exact_fix(self) -> None:
        action = repair_action("qa.verdict_channel")
        assert ".sync/inbox/claude/**" in action
        assert "scope.allow" in action

    def test_unknown_rule_falls_back(self) -> None:
        assert repair_action("no.such.rule")
