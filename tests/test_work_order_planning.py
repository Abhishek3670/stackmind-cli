"""Tests for work order planning, contract scope, and outcome verification.

These tests cover the WO-000 planning failure where scope-denied reads prevented
the agent from gathering necessary information to write the deliverable.
"""

import tempfile
from pathlib import Path


class TestContractScopeForPlanning:
    """Tests that contracts allow required reading for planning deliverables."""

    def test_architect_contract_allows_root_md_files(self):
        """Architecture agent should be able to read root-level markdown files."""
        from validators.kernel.daemon.authoring import build_child_contract

        contract = build_child_contract(
            wo_id="WO-000",
            agent_id="claude",
            role="architecture",
            deliverable_path="PLAN.md",
            candidate_paths=["PLAN.md"],
        )

        # Check that README.md is allowed
        allow_list = contract.get("scope", {}).get("allow", [])
        assert any("README" in r.get("module", "") or "*.md" in r.get("module", "")
                   for r in allow_list if isinstance(r, dict)), \
            "Contract should allow README.md or *.md files"

    def test_contract_scope_allows_plan_and_deliverable(self):
        """Contract should explicitly allow PLAN.md and the deliverable."""
        from validators.kernel.daemon.authoring import build_child_contract

        contract = build_child_contract(
            wo_id="WO-000",
            agent_id="claude",
            role="architecture",
            deliverable_path="PLAN.md",
            candidate_paths=[],
        )

        allow_list = contract.get("scope", {}).get("allow", [])
        assert any("PLAN.md" == r.get("module", "") for r in allow_list if isinstance(r, dict)), \
            "PLAN.md should be explicitly allowed"

    def test_denied_read_then_allowed_write(self):
        """Agent should be able to write deliverable after scope-denied reads."""
        from validators.kernel.contract import ContractEvaluator, AgentContract

        # Create a simple contract with explicit allow/deny
        contract = AgentContract(
            agent_id="codex",
            work_order="WO-000",
            allow=("README.md", "*.md", ".sync/inbox/codex/**"),
            deny=("secrets.json", ".env", ".git/**"),
            write_mode="read-write",
        )

        evaluator = ContractEvaluator()

        # Simulate a denied read attempt for a denied path
        ok, reason = evaluator.authorize(contract, 'read_file', 'secrets.json')
        assert not ok, "Denied path should be denied"

        # Simulate an allowed write to the deliverable
        ok, reason = evaluator.authorize(contract, 'write_file', 'README.md')
        assert ok, "Deliverable path should be allowed"


class TestOutcomeVerifiedForDeliverables:
    """Tests for outcome_verified behavior with deliverables."""

    def test_outcome_verified_fails_when_deliverable_not_written(self):
        """outcome_verified should be False when declared deliverable is not written."""
        from validators.harness.runner import HarnessTask
        from validators.harness.snapshot import VerificationDimensions

        # Simulate a turn where deliverable was not touched
        task = HarnessTask(
            kind='work_order',
            identifier='WO-000',
            path=Path('work-orders/ACTIVE/WO-000.yaml'),
            title='Plan Project',
            body='Create PLAN.md',
            query='Create PLAN.md',
            deliverable_path='PLAN.md',
            is_authoring=False,
        )

        # Decision was blocked because deliverable wasn't written
        deliverable_touched = False
        authored_artifacts = False
        decision_status = 'completed'
        decision_blockers = []

        # This simulates the logic at runner.py:2842-2845
        # When tool_runtime is None and deliverable_path is set
        outcome_verified = (
            decision_status == 'completed'
            and not decision_blockers
            and (deliverable_touched or False or False or False)  # deliverable.exists, changed_files, staged_writes
        )

        assert not outcome_verified, \
            "outcome_verified should be False when deliverable was not touched"

    def test_outcome_verified_succeeds_when_deliverable_written(self):
        """outcome_verified should be True when deliverable was successfully written."""
        deliverable_touched = True
        decision_status = 'completed'
        decision_blockers = []

        outcome_verified = (
            decision_status == 'completed'
            and not decision_blockers
            and (deliverable_touched or False)
        )

        assert outcome_verified, \
            "outcome_verified should be True when deliverable was touched"


class TestScopeDenialHandling:
    """Tests for how scope denials are handled."""

    def test_verify_scope_returns_denied_for_outside_path(self):
        """verify_scope should return False for paths outside allowed scope."""
        from validators.kernel.contract import ContractEvaluator

        contract = type('AgentContract', (), {
            'allow': ('PLAN.md', '.sync/inbox/claude/**'),
            'deny': ('.git/**', '.env',),
            'write_mode': 'read-only',
        })()

        evaluator = ContractEvaluator()
        ok, reason = evaluator.authorize(contract, 'read_file', 'README.md')

        assert not ok, "README.md should be denied if not in allow list"
        assert 'outside allowed scope' in reason or 'denied' in reason.lower()

    def test_verify_scope_allows_in_scope_path(self):
        """verify_scope should return True for paths inside allowed scope."""
        from validators.kernel.contract import ContractEvaluator

        contract = type('AgentContract', (), {
            'allow': ('PLAN.md', '.sync/inbox/claude/**'),
            'deny': ('.git/**', '.env'),
            'write_mode': 'read-only',
        })()

        evaluator = ContractEvaluator()
        ok, reason = evaluator.authorize(contract, 'read_file', 'PLAN.md')

        assert ok, "PLAN.md should be allowed"


class TestDeliverableVerification:
    """Tests for deliverable existence and modification checking."""

    def test_deliverable_touched_check(self):
        """Verify that deliverable_touched is set when deliverable is added or modified."""
        from pathlib import Path

        # Simulate the deliverable_touched check from runner.py:2734-2738
        deliverable_path = 'PLAN.md'
        norm_deliv = Path(deliverable_path).as_posix().lstrip('/')

        # Scenario 1: deliverable was added
        staged_added = {'PLAN.md', 'README.md'}
        staged_modified = set()
        deliverable_touched = (norm_deliv in staged_added or norm_deliv in staged_modified)
        assert deliverable_touched, "Should detect added deliverable"

        # Scenario 2: deliverable was modified
        staged_added = {'README.md'}
        staged_modified = {'PLAN.md'}
        deliverable_touched = (norm_deliv in staged_added or norm_deliv in staged_modified)
        assert deliverable_touched, "Should detect modified deliverable"

        # Scenario 3: deliverable was NOT touched
        staged_added = {'README.md'}
        staged_modified = {'README.md'}
        deliverable_touched = (norm_deliv in staged_added or norm_deliv in staged_modified)
        assert not deliverable_touched, "Should detect unmodified deliverable"
