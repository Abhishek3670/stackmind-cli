"""Tests for Memory Promotion Governance (Phase C Stage 5)."""

import hashlib
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from validators.memory.promotion import (
    MemoryPromotionPolicy,
    MemoryPromotionService,
    PromotionDecision,
)
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
    status: MemoryStatus = MemoryStatus.CANDIDATE,
    confidence: float = 0.8,
    content: str = "Test memory content",
    project_id: str = "test-project",
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


def test_promotion_policy_basic_eligible():
    """Test that a valid candidate memory is eligible for promotion."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        policy = MemoryPromotionPolicy(project_path)
        memory = create_test_memory()

        result = policy.evaluate_promotion(memory)
        assert result.decision == PromotionDecision.ELIGIBLE
        assert result.reason_code == "eligible"


def test_promotion_policy_invalid_status():
    """Test that non-CANDIDATE memories cannot be promoted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        policy = MemoryPromotionPolicy(project_path)
        memory = create_test_memory(status=MemoryStatus.ACTIVE)

        result = policy.evaluate_promotion(memory)
        assert result.decision == PromotionDecision.ALREADY_ACTIVE


def test_promotion_policy_expired_memory():
    """Test that expired memories cannot be promoted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        policy = MemoryPromotionPolicy(project_path)
        memory = create_test_memory()
        # Create expired memory by modifying expires_at
        from datetime import datetime, timezone, timedelta
        past = datetime.now(timezone.utc) - timedelta(days=1)
        memory = MemoryRecord(
            memory_id=memory.memory_id,
            memory_type=memory.memory_type,
            content=memory.content,
            normalized_content=memory.normalized_content,
            project_id=memory.project_id,
            provenance=memory.provenance,
            evidence=memory.evidence,
            status=memory.status,
            created_at=memory.created_at,
            updated_at=memory.updated_at,
            expires_at=past.isoformat(),
        )

        result = policy.evaluate_promotion(memory)
        assert result.decision == PromotionDecision.REJECTED
        assert result.reason_code == "expired"


def test_promotion_policy_low_confidence():
    """Test that memories with low provenance confidence are rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        policy = MemoryPromotionPolicy(project_path)
        memory = create_test_memory(confidence=0.5)  # Below FACT minimum of 0.7

        result = policy.evaluate_promotion(memory)
        assert result.decision == PromotionDecision.REJECTED
        assert "provenance_confidence" in result.reason_code


def test_promotion_policy_no_source_experience():
    """Test that memories without source experience IDs are rejected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        policy = MemoryPromotionPolicy(project_path)
        now = datetime.now(timezone.utc).isoformat()
        provenance = MemoryProvenance(
            source_experience_ids=(),  # Empty
            extraction_method="compiler",
            extracted_at=now,
            extractor_agent="test",
            confidence=0.8,
        )
        memory = MemoryRecord(
            memory_id=make_memory_id("test-project", "test"),
            memory_type=MemoryType.FACT,
            content="Test",
            normalized_content="test",
            project_id="test-project",
            provenance=provenance,
            evidence=(),
            status=MemoryStatus.CANDIDATE,
            created_at=now,
            updated_at=now,
        )

        result = policy.evaluate_promotion(memory)
        assert result.decision == PromotionDecision.REJECTED
        assert result.reason_code == "no_source_experience_ids"


def test_promotion_service_preview_and_promote():
    """Test the promotion service preview and promote flow."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)
        memory = create_test_memory()
        store.save_record(memory)

        service = MemoryPromotionService(project_path)

        # Preview
        preview = service.preview_promotion(memory.memory_id)
        assert preview.decision == PromotionDecision.ELIGIBLE

        # Promote
        promoted, result = service.promote(
            memory.memory_id,
            reason="Test promotion",
            actor="test",
            is_human=True,
        )
        assert promoted is not None
        assert promoted.status == MemoryStatus.ACTIVE
        assert result.decision == PromotionDecision.ELIGIBLE

        # Verify it's now active
        loaded = store.load_record(memory.memory_id)
        assert loaded.status == MemoryStatus.ACTIVE


def test_promotion_service_duplicate_detection():
    """Test that duplicate active memories are detected."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)
        # Create an active memory first with specific content
        active_content = "This is a test memory that will be duplicated"
        active_memory = create_test_memory(
            status=MemoryStatus.ACTIVE,
            content=active_content,
        )
        store.save_record(active_memory)

        # Try to promote a candidate with DIFFERENT content that normalizes to the same thing
        # e.g., different casing/whitespace but same normalized content
        candidate_content = "this   is  a   test   memory   that   will   be   duplicated"
        candidate = create_test_memory(
            status=MemoryStatus.CANDIDATE,
            content=candidate_content,
        )
        store.save_record(candidate)

        service = MemoryPromotionService(project_path)
        preview = service.preview_promotion(candidate.memory_id)
        assert preview.decision == PromotionDecision.ALREADY_ACTIVE
        assert preview.reason_code == "duplicate_active_memory"


def test_promotion_contract_scope_enforcement():
    """Test that contract scope rules are enforced."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        # Contract denies access to "forbidden-project"
        contract = create_contract(
            allow_rules=[],
            deny_rules=[{"project": "forbidden-project"}],
        )

        policy = MemoryPromotionPolicy(project_path, contract=contract)
        memory = create_test_memory(project_id="forbidden-project")

        result = policy.evaluate_promotion(memory)
        assert result.decision == PromotionDecision.REJECTED
        assert result.reason_code == "contract_scope_denied"


def test_promotion_allow_rules():
    """Test that allow rules work correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        # Contract only allows "allowed-project"
        contract = create_contract(
            allow_rules=[{"project": "allowed-project"}],
            deny_rules=[],
        )

        policy = MemoryPromotionPolicy(project_path, contract=contract)
        memory = create_test_memory(project_id="allowed-project")

        result = policy.evaluate_promotion(memory)
        assert result.decision == PromotionDecision.ELIGIBLE


def test_promotion_audit_log_created():
    """Test that audit log is created on promotion."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)
        memory = create_test_memory()
        store.save_record(memory)

        service = MemoryPromotionService(project_path)
        promoted, result = service.promote(
            memory.memory_id,
            reason="Test promotion",
            actor="test",
            is_human=True,
        )

        # Check audit log exists
        audit_dir = project_path / ".sync" / "memory" / "audit"
        audit_files = list(audit_dir.glob("promotion_*.jsonl"))
        assert len(audit_files) == 1

        # Verify audit content
        import json
        with open(audit_files[0]) as f:
            entry = json.loads(f.readline())
        assert entry["memory_id"] == memory.memory_id
        assert entry["old_status"] == "candidate"
        assert entry["new_status"] == "active"
        assert entry["decision"] == "eligible"
        assert entry["actor"] == "test"


def test_promotion_dry_run():
    """Test that dry-run mode doesn't mutate state."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        store = MemoryStore(project_path)
        memory = create_test_memory()
        store.save_record(memory)

        service = MemoryPromotionService(project_path)
        preview = service.preview_promotion(memory.memory_id)
        assert preview.decision == PromotionDecision.ELIGIBLE

        # Dry run via policy
        policy = MemoryPromotionPolicy(project_path)
        promoted, result = policy.enforce_promotion(memory, dry_run=True)
        assert promoted is None  # Not promoted in dry run
        assert result.decision == PromotionDecision.ELIGIBLE

        # Verify state unchanged
        loaded = store.load_record(memory.memory_id)
        assert loaded.status == MemoryStatus.CANDIDATE


def test_type_specific_policies():
    """Test that type-specific policies are enforced."""
    with tempfile.TemporaryDirectory() as tmpdir:
        project_path = Path(tmpdir)
        (project_path / "schemas").mkdir()
        schema_src = Path("schemas/memory.schema.json")
        if schema_src.exists():
            import shutil
            shutil.copy(schema_src, project_path / "schemas" / "memory.schema.json")

        policy = MemoryPromotionPolicy(project_path)

        # FACT requires 0.7 confidence
        fact_memory = create_test_memory(
            memory_type=MemoryType.FACT,
            confidence=0.65,
        )
        result = policy.evaluate_promotion(fact_memory)
        assert result.decision == PromotionDecision.REJECTED

        # DECISION requires 0.8 confidence
        decision_memory = create_test_memory(
            memory_type=MemoryType.DECISION,
            confidence=0.75,
        )
        result = policy.evaluate_promotion(decision_memory)
        assert result.decision == PromotionDecision.REJECTED

        # PREFERENCE only requires 0.5
        pref_memory = create_test_memory(
            memory_type=MemoryType.PREFERENCE,
            confidence=0.55,
        )
        result = policy.evaluate_promotion(pref_memory)
        assert result.decision == PromotionDecision.ELIGIBLE


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])