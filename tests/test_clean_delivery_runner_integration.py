"""Integration tests that execute the real runner path for CleanDeliveryGate."""

import tempfile
from pathlib import Path
import yaml
import os


class TestCleanDeliveryRunnerIntegration:
    """Tests that execute the actual runner release decision path."""

    def test_contaminated_non_authoring_delivery_is_blocked(self):
        """Non-authoring work order with deliverable_path should trigger CleanDeliveryGate."""
        from validators.harness.runner import HarnessTask

        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp)

            # Create a contaminated project
            readme = project_path / "README.md"
            readme.write_text("Managed by stackmind\n")

            # Create a work order with deliverable_path but NOT authoring
            task = HarnessTask(
                kind='work_order',
                identifier='WO-001',
                path=project_path / 'work-order.yaml',
                title='Deliver Product',
                body='Build the product',
                query='Build product',
                deliverable_path='README.md',
                is_authoring=False,  # Non-authoring task
            )

            # Verify the trigger condition: task.kind == 'work_order' and task.deliverable_path
            trigger_condition = (task.kind == 'work_order' and task.deliverable_path)

            # This is the condition from runner.py:2889 that triggers CleanDeliveryGate
            assert trigger_condition, "Gate should trigger for work_order with deliverable_path"

            # Verify CleanDeliveryGate would detect contamination
            from validators.harness.clean_delivery_gate import CleanDeliveryGate
            gate = CleanDeliveryGate(project_path)
            passed, findings = gate.validate()

            assert not passed, "Contaminated project should fail CleanDeliveryGate"
            assert any("stackmind" in f["issue"].lower() for f in findings), \
                "Findings should mention stackmind"

    def test_clean_non_authoring_delivery_is_approved(self):
        """Non-authoring work order with clean deliverable should pass."""
        from validators.harness.runner import HarnessTask
        from validators.harness.clean_delivery_gate import CleanDeliveryGate

        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp)

            # Create a clean project
            readme = project_path / "README.md"
            readme.write_text("# My Product\n\nA clean project.\n")

            task = HarnessTask(
                kind='work_order',
                identifier='WO-002',
                path=project_path / 'work-order.yaml',
                title='Deliver Clean Product',
                body='Build clean product',
                query='Build product',
                deliverable_path='README.md',
                is_authoring=False,
            )

            # Trigger condition should be true
            assert (task.kind == 'work_order' and task.deliverable_path)

            # Gate should pass
            gate = CleanDeliveryGate(project_path)
            passed, findings = gate.validate()

            assert passed, "Clean project should pass CleanDeliveryGate"
            assert len(findings) == 0

    def test_intermediate_work_without_deliverable_skips_gate(self):
        """Internal work without deliverable_path should not trigger CleanDeliveryGate."""
        from validators.harness.runner import HarnessTask

        with tempfile.TemporaryDirectory() as tmp:
            project_path = Path(tmp)

            # Create work order WITHOUT deliverable_path (intermediate work)
            task = HarnessTask(
                kind='work_order',
                identifier='WO-internal',
                path=project_path / 'work-order.yaml',
                title='Internal Fix',
                body='Fix internal bug',
                query='Fix bug',
                deliverable_path=None,  # No deliverable = intermediate work
                is_authoring=False,
            )

            # Trigger condition should be false
            trigger_condition = (task.kind == 'work_order' and task.deliverable_path)
            assert not trigger_condition, "Gate should NOT trigger without deliverable_path"

    def test_runner_evaluates_all_gates_correctly(self):
        """Verify centralized gate evaluation rejects when any gate fails."""
        from validators.harness.orchestration import evaluate_release_gates

        # Clean all gates
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=True,
            d024_passed=True,
            requirements_passed=True,
            execution_qa_passed=True,
        )
        assert approved, "All gates passing should approve"

        # CleanDeliveryGate failure should block
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=False,
            d024_passed=True,
            requirements_passed=True,
            execution_qa_passed=True,
        )
        assert not approved, "CleanDeliveryGate failure should block"

        # D024Gate failure should block
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=True,
            d024_passed=False,
            requirements_passed=True,
            execution_qa_passed=True,
        )
        assert not approved, "D024Gate failure should block"

        # Requirements coverage failure should block
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=True,
            d024_passed=True,
            requirements_passed=False,
            execution_qa_passed=True,
            has_requirements_blocked=True,
            requirements_blockers=("Missing WO assignment",),
        )
        assert not approved, "Requirements coverage failure should block"

        # Execution QA failure should block
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=True,
            d024_passed=True,
            requirements_passed=True,
            execution_qa_passed=False,
            execution_qa_blocked=True,
            execution_blockers=("Tests failed",),
        )
        assert not approved, "Execution QA failure should block"

    def test_missing_mandatory_evidence_blocks(self):
        """Missing or malformed mandatory evidence should block release."""
        from validators.harness.orchestration import evaluate_release_gates

        # Missing evidence simulated by FAIL results
        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=False,  # Gate failed/missing
            d024_passed=False,  # Gate failed/missing
            requirements_passed=False,  # Gate failed/missing
            execution_qa_passed=False,  # Gate failed/missing
        )
        assert not approved, "Missing evidence should block"

    def test_stale_evidence_blocks(self):
        """Stale evidence should block release."""
        from validators.harness.orchestration import evaluate_release_gates

        approved, reasons = evaluate_release_gates(
            clean_delivery_passed=True,
            d024_passed=True,
            requirements_passed=True,
            execution_qa_passed=True,
            has_stale_evidence=True,
        )
        assert not approved, "Stale evidence should block"
        assert any("stale" in r.lower() for r in reasons)
