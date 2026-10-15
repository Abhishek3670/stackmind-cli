"""Tests for Memory Contradiction Detection (Phase C Stage 6)."""

import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from validators.memory.contradiction import (
    ContradictionDetector,
    ContradictionResolutionService,
    ContradictionFinding,
    ContradictionCategory,
    ContradictionSeverity,
    ContradictionStatus,
    ContradictionStore,
)
from validators.memory.promotion import MemoryPromotionPolicy, MemoryApprovalReceipt
from validators.memory.models import MemoryRecord, MemoryType, MemoryStatus, MemoryProvenance, MemoryEvidence
from validators.knowledge.contract import AgentContract
from validators.memory.store import MemoryStore
from validators.knowledge.registry import node_id_for


def make_memory_id(project_id: str, content: str) -> str:
    """Generate a valid memory ID using the node_id_for function."""
    content_hash = hashlib.sha256(f"{project_id}:{content.lower()}".encode()).hexdigest()[:16]
    return node_id_for("memory", f"{project_id}:{content_hash}")


def create_test_memory(
    memory_type: MemoryType = MemoryType.FACT,
    status: MemoryStatus = MemoryStatus.ACTIVE,
    confidence: float = 0.8,
    content: str = "Test memory content",
    project_id: str = "test-project",
    supersedes: tuple = (),
    superseded_by: str | None = None,
) -> MemoryRecord:
    """Create a test memory record with valid ID."""
    memory_id = make_memory_id(project_id, content)
    now = datetime.now(timezone.utc).isoformat()
    provenance = MemoryProvenance(
        source_experience_ids=("EXP-test1234567890ab",),
        extraction_method="compiler",
        extracted_at=now,
        extractor_agent="test",
        confidence=confidence,
    )
    evidence = (
        MemoryEvidence(
            experience_id="EXP-test1234567890ab",
            evidence_type="verification_dimension",
            excerpt="test verified",
            dimension_scores={"code_verified": True},
        ),
    )
    return MemoryRecord(
        memory_id=memory_id,
        memory_type=memory_type,
        content=content,
        normalized_content=content.lower(),
        project_id=project_id,
        provenance=provenance,
        evidence=evidence,
        status=status,
        created_at=now,
        updated_at=now,
        supersedes=supersedes,
        superseded_by=superseded_by,
    )


def create_contract(allow_rules=None, deny_rules=None):
    """Create a test AgentContract with proper structure."""
    if allow_rules is None:
        allow_rules = []
    if deny_rules is None:
        deny_rules = []
    contract_data = {
        "agent_id": "test-agent",
        "work_order": "test-wo",
        "identity": {"role": "test"},
        "scope": {
            "allow": allow_rules,
            "deny": deny_rules,
            "write": "read-only",
        },
        "budget": {
            "max_tokens": 10000,
            "expires_at": "2099-12-31T23:59:59+00:00",
        },
        "skills": [],
    }
    return AgentContract(contract_data)


def test_contradiction_store_basic():
    """Test basic contradiction store operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        store = ContradictionStore(project_path)

        finding = ContradictionFinding(
            finding_id="CONTR-test1234567890ab",
            category=ContradictionCategory.INCOMPATIBLE_VALUES,
            severity=ContradictionSeverity.BLOCKING,
            status=ContradictionStatus.UNRESOLVED,
            memory_id_a="MEM-test1234567890ab",
            memory_id_b="MEM-test1234567890cd",
            project_id="test-project",
            detection_method="deterministic",
            explanation="Test contradiction",
        )

        store.save_finding(finding)

        loaded = store.load_finding("CONTR-test1234567890ab")
        assert loaded is not None
        assert loaded.finding_id == finding.finding_id
        assert loaded.category == ContradictionCategory.INCOMPATIBLE_VALUES

        # Test find_by_memory_pair (order independent)
        found = store.find_by_memory_pair("MEM-test1234567890cd", "MEM-test1234567890ab")
        assert found is not None
        assert found.finding_id == finding.finding_id


def test_contradiction_detector_incompatible_values():
    """Test detection of incompatible values for same subject (FACT type)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Create active memory saying tests PASSED
        active_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Tests passed for WO-123",
            project_id="test-project",
        )
        store.save_record(active_memory)

        # Create candidate saying tests FAILED for same work order
        candidate = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Tests failed for WO-123",
            project_id="test-project",
        )
        store.save_record(candidate)

        detector = ContradictionDetector(project_path, memory_store=store)
        findings = detector.detect_contradictions(candidate)

        assert len(findings) >= 1
        blocking = [f for f in findings if f.severity == ContradictionSeverity.BLOCKING]
        assert len(blocking) >= 1
        assert blocking[0].category == ContradictionCategory.INCOMPATIBLE_VALUES


def test_contradiction_detector_conflicting_decisions():
    """Test detection of conflicting decisions for same work order."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Active decision: approved WO-456
        active_memory = create_test_memory(
            memory_type=MemoryType.DECISION,
            status=MemoryStatus.ACTIVE,
            content="Approved work order WO-456: Implementation complete",
            project_id="test-project",
        )
        store.save_record(active_memory)

        # Candidate decision: rejected WO-456
        candidate = create_test_memory(
            memory_type=MemoryType.DECISION,
            status=MemoryStatus.CANDIDATE,
            content="Rejected work order WO-456: Requirements not met",
            project_id="test-project",
        )
        store.save_record(candidate)

        detector = ContradictionDetector(project_path, memory_store=store)
        findings = detector.detect_contradictions(candidate)

        assert len(findings) >= 1
        blocking = [f for f in findings if f.severity == ContradictionSeverity.BLOCKING]
        assert len(blocking) >= 1
        assert blocking[0].category == ContradictionCategory.CONFLICTING_DECISIONS


def test_contradiction_detector_legitimate_supersession():
    """Test detection of legitimate supersession (not a conflict).

    Legitimate supersession is when a candidate supersedes a SUPERSEDED memory,
    and there's already an ACTIVE memory that supersedes that same SUPERSEDED memory.
    This means the candidate is just repeating an already-validated supersession.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Old memory (now superseded)
        old_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.SUPERSEDED,
            content="Old fact about WO-789",
            project_id="test-project",
        )
        store.save_record(old_memory)

        # New memory (active) that supersedes old
        new_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Updated fact about WO-789",
            project_id="test-project",
            supersedes=(old_memory.memory_id,),
        )
        # Update old memory with correct superseded_by
        old_memory = MemoryRecord(
            memory_id=old_memory.memory_id,
            memory_type=old_memory.memory_type,
            content=old_memory.content,
            normalized_content=old_memory.normalized_content,
            project_id=old_memory.project_id,
            provenance=old_memory.provenance,
            evidence=old_memory.evidence,
            status=MemoryStatus.SUPERSEDED,
            created_at=old_memory.created_at,
            updated_at=old_memory.updated_at,
            superseded_by=new_memory.memory_id,
            supersedes=old_memory.supersedes,
            tags=old_memory.tags,
            schema_version=old_memory.schema_version,
        )
        store.save_record(old_memory)
        store.save_record(new_memory)

        # Candidate that also supersedes the same old memory
        candidate = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Another update about WO-789",
            project_id="test-project",
        )
        candidate = MemoryRecord(
            memory_id=candidate.memory_id,
            memory_type=candidate.memory_type,
            content=candidate.content,
            normalized_content=candidate.normalized_content,
            project_id=candidate.project_id,
            provenance=candidate.provenance,
            evidence=candidate.evidence,
            status=MemoryStatus.CANDIDATE,
            created_at=candidate.created_at,
            updated_at=candidate.updated_at,
            supersedes=(old_memory.memory_id,),
            superseded_by=candidate.superseded_by,
            tags=candidate.tags,
            schema_version=candidate.schema_version,
        )

        detector = ContradictionDetector(project_path, memory_store=store)

        # Test detection: candidate against existing active (new_memory)
        # The candidate supersedes old_memory, and new_memory also supersedes old_memory
        # This should be detected as legitimate supersession (both point to same superseded)
        findings = detector.detect_contradictions(candidate, existing_records=[new_memory])

        # Should find legitimate supersession (info, resolved)
        info_findings = [f for f in findings if f.severity == ContradictionSeverity.INFO]
        legit = [f for f in info_findings if f.category == ContradictionCategory.LEGITIMATE_SUPERSESSION]
        assert len(legit) >= 1
        assert legit[0].status == ContradictionStatus.RESOLVED_SUPERSEDED


def test_contradiction_detector_mutual_supersession():
    """Test detection of mutual supersession (invalid chain) - candidate and active each supersede the other."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Active memory A
        mem_a = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Memory A",
            project_id="test-project",
        )
        store.save_record(mem_a)

        # Candidate B that supersedes A
        candidate_b = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Memory B",
            project_id="test-project",
            supersedes=(mem_a.memory_id,),
        )
        store.save_record(candidate_b)

        # Now update A to also supersede B (creating mutual supersession between A and B)
        mem_a_updated = MemoryRecord(
            memory_id=mem_a.memory_id,
            memory_type=mem_a.memory_type,
            content=mem_a.content,
            normalized_content=mem_a.normalized_content,
            project_id=mem_a.project_id,
            provenance=mem_a.provenance,
            evidence=mem_a.evidence,
            status=MemoryStatus.ACTIVE,
            created_at=mem_a.created_at,
            updated_at=datetime.now(timezone.utc).isoformat(),
            supersedes=(candidate_b.memory_id,),  # A now supersedes B
            superseded_by=mem_a.superseded_by,
            tags=mem_a.tags,
            schema_version=mem_a.schema_version,
        )
        store.save_record(mem_a_updated)

        # Test with candidate B (which supersedes A) against active A (which supersedes B)
        detector = ContradictionDetector(project_path, memory_store=store)
        findings = detector.detect_contradictions(candidate_b, existing_records=[mem_a_updated])

        # Should detect mutual supersession between candidate_b and mem_a_updated
        chain_findings = [f for f in findings if f.category == ContradictionCategory.SUPERSESSION_CHAIN_INVALID]
        assert len(chain_findings) >= 1
        assert chain_findings[0].severity == ContradictionSeverity.BLOCKING


def test_contradiction_detector_scope_mismatch():
    """Test that different projects don't trigger contradictions."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Active memory in project A
        active_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Tests passed for WO-123",
            project_id="project-a",
        )
        store.save_record(active_memory)

        # Candidate in project B with conflicting content
        candidate = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Tests failed for WO-123",
            project_id="project-b",
        )
        store.save_record(candidate)

        detector = ContradictionDetector(project_path, memory_store=store)
        findings = detector.detect_contradictions(candidate, existing_records=[active_memory])

        # Should find scope mismatch (resolved, not a contradiction)
        scope_findings = [f for f in findings if f.category == ContradictionCategory.SCOPE_MISMATCH]
        assert len(scope_findings) >= 1
        assert scope_findings[0].status == ContradictionStatus.RESOLVED_NOT_CONTRADICTION


def test_contradiction_detector_contract_scope():
    """Test that contract scope rules filter memories for detection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Contract denies access to "forbidden-project"
        contract = create_contract(
            allow_rules=[],
            deny_rules=[{"project": "forbidden-project"}],
        )

        # Active memory in forbidden project
        active_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Tests passed",
            project_id="forbidden-project",
        )
        store.save_record(active_memory)

        # Candidate in allowed project
        candidate = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Tests failed",
            project_id="allowed-project",
        )
        store.save_record(candidate)

        detector = ContradictionDetector(project_path, contract=contract, memory_store=store)
        findings = detector.detect_contradictions(candidate, existing_records=[active_memory])

        # Should find scope mismatch because contract filters forbidden project
        scope_findings = [f for f in findings if f.category == ContradictionCategory.SCOPE_MISMATCH]
        assert len(scope_findings) >= 1


def test_contradiction_detector_ineligible_status():
    """Test that non-ACTIVE memories are excluded from detection."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Memory in SUPERSEDED status
        superseded_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.SUPERSEDED,
            content="Old fact",
            project_id="test-project",
        )
        store.save_record(superseded_memory)

        # Memory in DELETED status
        deleted_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.DELETED,
            content="Deleted fact",
            project_id="test-project",
        )
        store.save_record(deleted_memory)

        # Candidate
        candidate = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="New fact",
            project_id="test-project",
        )
        store.save_record(candidate)

        detector = ContradictionDetector(project_path, memory_store=store)
        findings = detector.detect_contradictions(candidate, existing_records=[superseded_memory, deleted_memory])

        # Should not find contradictions with superseded/deleted memories
        # (they may find scope mismatch or nothing)
        blocking = [f for f in findings if f.severity == ContradictionSeverity.BLOCKING]
        assert len(blocking) == 0


def test_contradiction_resolution_service():
    """Test resolving contradiction findings."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Create active memories with contradiction
        mem_a = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Tests passed for WO-123",
            project_id="test-project",
        )
        store.save_record(mem_a)

        mem_b = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Tests failed for WO-123",
            project_id="test-project",
        )
        store.save_record(mem_b)

        detector = ContradictionDetector(project_path, memory_store=store)
        findings = detector.detect_contradictions(mem_b)
        blocking = [f for f in findings if f.severity == ContradictionSeverity.BLOCKING]
        assert len(blocking) >= 1

        finding_id = blocking[0].finding_id

        # Resolve as "not a contradiction"
        service = ContradictionResolutionService(project_path, memory_store=store)
        resolved = service.resolve_finding(
            finding_id,
            ContradictionStatus.RESOLVED_NOT_CONTRADICTION,
            "Different test runs, both valid in context",
            "test-user",
        )

        assert resolved is not None
        assert resolved.status == ContradictionStatus.RESOLVED_NOT_CONTRADICTION
        assert resolved.resolution_rationale == "Different test runs, both valid in context"
        assert resolved.resolved_at is not None


def test_contradiction_resolution_supersede():
    """Test resolving contradiction via supersession."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Create memories with contradiction
        mem_a = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Tests passed for WO-123",
            project_id="test-project",
        )
        store.save_record(mem_a)

        mem_b = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Tests failed for WO-123",
            project_id="test-project",
        )
        store.save_record(mem_b)

        detector = ContradictionDetector(project_path, memory_store=store)
        findings = detector.detect_contradictions(mem_b, existing_records=[mem_a])
        blocking = [f for f in findings if f.severity == ContradictionSeverity.BLOCKING]
        assert len(blocking) >= 1

        finding_id = blocking[0].finding_id

        # Create superseding memory
        superseding = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Tests passed for WO-123 after flaky test fix",
            project_id="test-project",
        )
        store.save_record(superseding)

        # Grant approval for all memories involved (both conflicting memories + superseding memory)
        promo_policy = MemoryPromotionPolicy(project_path)
        promo_policy.grant_approval(mem_a, "test-user", "Resolving contradiction")
        promo_policy.grant_approval(mem_b, "test-user", "Resolving contradiction")
        promo_policy.grant_approval(superseding, "test-user", "Superseding to resolve contradiction")

        # Resolve via supersession
        service = ContradictionResolutionService(project_path, memory_store=store)
        superseded, resolved = service.supersede_and_resolve(
            finding_id,
            superseding,
            "New run with flaky test fix supersedes both",
            "test-user",
        )

        assert superseded is not None
        assert resolved is not None
        assert resolved.status == ContradictionStatus.RESOLVED_SUPERSEDED
        # The superseded memory should be the older one
        assert superseded.status == MemoryStatus.SUPERSEDED
        assert superseded.superseded_by == superseding.memory_id


def test_promotion_gate_blocks_on_contradiction():
    """Test that promotion policy blocks when contradiction detected."""
    from validators.memory.promotion import MemoryPromotionPolicy, PromotionDecision

    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Active memory saying tests PASSED
        active_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Tests passed for WO-123",
            project_id="test-project",
        )
        store.save_record(active_memory)

        # Candidate saying tests FAILED
        candidate = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Tests failed for WO-123",
            project_id="test-project",
        )
        store.save_record(candidate)

        policy = MemoryPromotionPolicy(project_path)
        result = policy.evaluate_promotion(candidate)

        assert result.decision == PromotionDecision.CONTRADICTION_DETECTED
        assert "contradiction" in result.reason_code


def test_contradiction_audit_log():
    """Test that contradiction detection and resolution create audit logs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Create contradiction
        mem_a = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Tests passed",
            project_id="test-project",
        )
        store.save_record(mem_a)

        mem_b = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Tests failed",
            project_id="test-project",
        )
        store.save_record(mem_b)

        detector = ContradictionDetector(project_path, memory_store=store)
        findings = detector.detect_contradictions(mem_b)

        # Resolve it
        service = ContradictionResolutionService(project_path, memory_store=store)
        if findings:
            finding_id = findings[0].finding_id
            service.resolve_finding(
                finding_id,
                ContradictionStatus.RESOLVED_NOT_CONTRADICTION,
                "Test resolution",
                "test-user",
            )

        # Check audit log exists
        audit_dir = project_path / ".sync" / "memory" / "audit"
        audit_files = list(audit_dir.glob("contradiction_*.jsonl"))
        assert len(audit_files) >= 1


def test_contradiction_cli_integration():
    """Test CLI commands for contradiction detection."""
    from cli.memory import memory_contradiction_detect, memory_contradictions, memory_contradiction_show
    from click.testing import CliRunner

    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Create active memory
        active_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Tests passed for WO-123",
            project_id="test-project",
        )
        store.save_record(active_memory)

        # Candidate with contradiction
        candidate = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Tests failed for WO-123",
            project_id="test-project",
        )
        store.save_record(candidate)

        runner = CliRunner()

        # Test contradiction-detect
        result = runner.invoke(memory_contradiction_detect, [candidate.memory_id, "--project", str(project_path)])
        assert result.exit_code == 0
        assert "contradiction" in result.output.lower() or "conflict" in result.output.lower()

        # Test contradictions list
        result = runner.invoke(memory_contradictions, ["--project", str(project_path)])
        assert result.exit_code == 0


def test_duplicate_finding_detection():
    """Test that duplicate findings are not created for same memory pair."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        active_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Tests passed for WO-123",
            project_id="test-project",
        )
        store.save_record(active_memory)

        candidate = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Tests failed for WO-123",
            project_id="test-project",
        )
        store.save_record(candidate)

        detector = ContradictionDetector(project_path, memory_store=store)

        # Run detection twice
        findings1 = detector.detect_contradictions(candidate)
        findings2 = detector.detect_contradictions(candidate)

        # Should not create duplicate findings
        blocking1 = [f for f in findings1 if f.severity == ContradictionSeverity.BLOCKING]
        blocking2 = [f for f in findings2 if f.severity == ContradictionSeverity.BLOCKING]

        assert len(blocking1) == len(blocking2)
        if blocking1:
            assert blocking1[0].finding_id == blocking2[0].finding_id


def test_contradiction_with_verified_not_verified():
    """Test detection of verified vs not verified conflict."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Active: verified
        active_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Code changes verified for module auth",
            project_id="test-project",
        )
        store.save_record(active_memory)

        # Candidate: not verified
        candidate = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Code changes not verified for module auth",
            project_id="test-project",
        )
        store.save_record(candidate)

        detector = ContradictionDetector(project_path, memory_store=store)
        findings = detector.detect_contradictions(candidate)

        blocking = [f for f in findings if f.severity == ContradictionSeverity.BLOCKING]
        assert len(blocking) >= 1
        assert blocking[0].category == ContradictionCategory.INCOMPATIBLE_VALUES


def test_promotion_service_with_contradiction():
    """Test that MemoryPromotionService properly handles contradictions."""
    from validators.memory.promotion import MemoryPromotionService, PromotionDecision

    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)

        # Active memory
        active_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.ACTIVE,
            content="Tests passed for WO-123",
            project_id="test-project",
        )
        store.save_record(active_memory)

        # Candidate with contradiction
        candidate = create_test_memory(
            memory_type=MemoryType.FACT,
            status=MemoryStatus.CANDIDATE,
            content="Tests failed for WO-123",
            project_id="test-project",
        )
        store.save_record(candidate)

        service = MemoryPromotionService(project_path)

        # Preview should show contradiction
        preview = service.preview_promotion(candidate.memory_id)
        assert preview.decision == PromotionDecision.CONTRADICTION_DETECTED

        # Actual promotion should fail
        promoted, result = service.promote(candidate.memory_id, actor="test")
        assert promoted is None
        assert result.decision == PromotionDecision.CONTRADICTION_DETECTED


def test_contradiction_finding_serialization():
    """Test ContradictionFinding serialization round-trip."""
    finding = ContradictionFinding(
        finding_id="CONTR-test1234567890ab",
        category=ContradictionCategory.INCOMPATIBLE_VALUES,
        severity=ContradictionSeverity.BLOCKING,
        status=ContradictionStatus.UNRESOLVED,
        memory_id_a="MEM-test1234567890ab",
        memory_id_b="MEM-test1234567890cd",
        project_id="test-project",
        detection_method="deterministic",
        confidence=0.95,
        explanation="Test explanation",
        evidence_refs_a=("EXP-1", "EXP-2"),
        evidence_refs_b=("EXP-3",),
    )

    data = finding.to_dict()
    loaded = ContradictionFinding.from_dict(data)

    assert loaded.finding_id == finding.finding_id
    assert loaded.category == finding.category
    assert loaded.severity == finding.severity
    assert loaded.status == finding.status
    assert loaded.memory_id_a == finding.memory_id_a
    assert loaded.memory_id_b == finding.memory_id_b
    assert loaded.confidence == finding.confidence
    assert loaded.explanation == finding.explanation
    assert loaded.evidence_refs_a == finding.evidence_refs_a
    assert loaded.evidence_refs_b == finding.evidence_refs_b


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])