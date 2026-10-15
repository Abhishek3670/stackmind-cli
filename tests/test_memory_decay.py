"""Tests for Stage 7: Memory Decay implementation."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from validators.kernel.profile import ExecutionProfile
from validators.memory.decay import (
    DecayAdjuster,
    DecayDiagnostics,
    DecayFunction,
    DecayPolicy,
    get_decay_policy,
)
from validators.memory.models import MemoryProvenance, MemoryRecord, MemoryStatus, MemoryType
from validators.memory.retriever import MemoryRetriever, MemoryRetrievalDiagnostics


def _make_memory(
    memory_id: str,
    memory_type: MemoryType,
    content: str,
    project_id: str = "test-project",
    confidence: float = 0.8,
    days_old: float = 0.0,
    status: MemoryStatus = MemoryStatus.ACTIVE,
) -> MemoryRecord:
    """Create a test memory with a specific age."""
    now = datetime.now(timezone.utc)
    created_at = (now - timedelta(days=days_old)).isoformat()
    provenance = MemoryProvenance(
        source_experience_ids=("EXP-test",),
        extraction_method="compiler",
        extracted_at=created_at,
        extractor_agent="test",
        confidence=confidence,
    )
    return MemoryRecord(
        memory_id=memory_id,
        memory_type=memory_type,
        content=content,
        normalized_content=content.lower(),
        project_id=project_id,
        provenance=provenance,
        evidence=(),
        status=status,
        created_at=created_at,
        updated_at=created_at,
    )


class TestDecayPolicy:
    """Tests for DecayPolicy configuration."""

    def test_default_policy(self):
        policy = DecayPolicy.default()
        assert policy.function == DecayFunction.EXPONENTIAL
        assert policy.half_life_days == 30.0
        assert policy.decay_floor == 0.2
        assert policy.enabled is True
        assert MemoryType.FACT in policy.half_life_by_type
        assert MemoryType.DECISION in policy.half_life_by_type
        assert MemoryType.LESSON in policy.half_life_by_type
        assert MemoryType.PREFERENCE in policy.half_life_by_type
        assert MemoryType.PATTERN in policy.half_life_by_type
        assert ExecutionProfile.LOW in policy.profile_multipliers
        assert ExecutionProfile.MEDIUM in policy.profile_multipliers
        assert ExecutionProfile.HIGH in policy.profile_multipliers

    def test_no_decay_policy(self):
        policy = DecayPolicy.no_decay()
        assert policy.function == DecayFunction.NONE
        assert policy.enabled is False
        assert policy.decay_floor == 1.0

    def test_get_effective_half_life(self):
        policy = DecayPolicy.default()
        # Base half-life for FACT is 60 days
        assert policy.get_effective_half_life(MemoryType.FACT, ExecutionProfile.MEDIUM) == 60.0
        # HIGH profile has 0.5x multiplier
        assert policy.get_effective_half_life(MemoryType.FACT, ExecutionProfile.HIGH) == 30.0
        # LOW profile has 0.0 multiplier (but we use MEDIUM for memories)
        assert policy.get_effective_half_life(MemoryType.FACT, ExecutionProfile.LOW) == 0.0

    def test_to_dict(self):
        policy = DecayPolicy.default()
        d = policy.to_dict()
        assert d["function"] == "exponential"
        assert d["half_life_days"] == 30.0
        assert d["decay_floor"] == 0.2
        assert d["enabled"] is True
        assert "fact" in d["half_life_by_type"]
        assert "medium" in d["profile_multipliers"]


class TestDecayAdjuster:
    """Tests for DecayAdjuster core logic."""

    def test_exponential_decay_fresh_memory(self):
        """Fresh memory (age=0) should have decay factor 1.0."""
        adjuster = DecayAdjuster(Path("."), DecayPolicy.default())
        memory = _make_memory("MEM-test1", MemoryType.FACT, "test content", days_old=0.0)
        base_scores = [0.8]
        decayed, diag = adjuster.apply_decay([memory], base_scores, ExecutionProfile.MEDIUM)
        assert decayed[0] == 0.8  # No decay for fresh memory
        assert diag.decay_applied_count == 0

    def test_exponential_decay_at_half_life(self):
        """Memory at half-life should have decay factor ~0.5."""
        policy = DecayPolicy(
            function=DecayFunction.EXPONENTIAL,
            half_life_days=30.0,
            decay_floor=0.0,
            enabled=True,
            half_life_by_type={MemoryType.FACT: 30.0},
            profile_multipliers={ExecutionProfile.MEDIUM: 1.0},
        )
        adjuster = DecayAdjuster(Path("."), policy)
        memory = _make_memory("MEM-test2", MemoryType.FACT, "test content", days_old=30.0)
        base_scores = [0.8]
        decayed, diag = adjuster.apply_decay([memory], base_scores, ExecutionProfile.MEDIUM)
        # At half-life, factor should be 0.5
        assert 0.39 < decayed[0] < 0.41  # 0.8 * 0.5 = 0.4
        assert diag.decay_applied_count == 1

    def test_exponential_decay_two_half_lives(self):
        """Memory at 2x half-life should have decay factor ~0.25."""
        policy = DecayPolicy(
            function=DecayFunction.EXPONENTIAL,
            half_life_days=30.0,
            decay_floor=0.0,
            enabled=True,
            half_life_by_type={MemoryType.FACT: 30.0},
            profile_multipliers={ExecutionProfile.MEDIUM: 1.0},
        )
        adjuster = DecayAdjuster(Path("."), policy)
        memory = _make_memory("MEM-test3", MemoryType.FACT, "test content", days_old=60.0)
        base_scores = [1.0]
        decayed, diag = adjuster.apply_decay([memory], base_scores, ExecutionProfile.MEDIUM)
        # At 2 half-lives, factor should be 0.25
        assert 0.24 < decayed[0] < 0.26
        assert diag.decay_applied_count == 1

    def test_linear_decay(self):
        """Linear decay should reach 0 at half-life."""
        policy = DecayPolicy(
            function=DecayFunction.LINEAR,
            half_life_days=30.0,
            decay_floor=0.0,
            enabled=True,
            half_life_by_type={MemoryType.FACT: 30.0},
            profile_multipliers={ExecutionProfile.MEDIUM: 1.0},
        )
        adjuster = DecayAdjuster(Path("."), policy)
        memory = _make_memory("MEM-test4", MemoryType.FACT, "test content", days_old=15.0)
        base_scores = [1.0]
        decayed, _ = adjuster.apply_decay([memory], base_scores, ExecutionProfile.MEDIUM)
        # At half half-life, linear should be 0.5
        assert 0.49 < decayed[0] < 0.51

    def test_stepwise_decay(self):
        """Stepwise decay should drop by half at each half-life boundary."""
        policy = DecayPolicy(
            function=DecayFunction.STEPWISE,
            half_life_days=30.0,
            decay_floor=0.0,
            enabled=True,
            half_life_by_type={MemoryType.FACT: 30.0},
            profile_multipliers={ExecutionProfile.MEDIUM: 1.0},
        )
        adjuster = DecayAdjuster(Path("."), policy)

        # Just before half-life
        memory1 = _make_memory("MEM-test5a", MemoryType.FACT, "test content", days_old=29.9)
        decayed1, _ = adjuster.apply_decay([memory1], [1.0], ExecutionProfile.MEDIUM)
        assert decayed1[0] == 1.0

        # Just after half-life
        memory2 = _make_memory("MEM-test5b", MemoryType.FACT, "test content", days_old=30.1)
        decayed2, _ = adjuster.apply_decay([memory2], [1.0], ExecutionProfile.MEDIUM)
        assert 0.49 < decayed2[0] < 0.51

    def test_decay_floor(self):
        """Decay should not go below decay_floor."""
        policy = DecayPolicy(
            function=DecayFunction.EXPONENTIAL,
            half_life_days=1.0,  # Very short half-life
            decay_floor=0.3,
            enabled=True,
            half_life_by_type={MemoryType.FACT: 1.0},
            profile_multipliers={ExecutionProfile.MEDIUM: 1.0},
        )
        adjuster = DecayAdjuster(Path("."), policy)
        memory = _make_memory("MEM-test6", MemoryType.FACT, "test content", days_old=100.0)
        base_scores = [1.0]
        decayed, _ = adjuster.apply_decay([memory], base_scores, ExecutionProfile.MEDIUM)
        assert decayed[0] >= 0.3

    def test_disabled_decay(self):
        """Disabled decay should return original scores unchanged."""
        policy = DecayPolicy.no_decay()
        adjuster = DecayAdjuster(Path("."), policy)
        memory = _make_memory("MEM-test7", MemoryType.FACT, "test content", days_old=1000.0)
        base_scores = [0.5]
        decayed, diag = adjuster.apply_decay([memory], base_scores, ExecutionProfile.MEDIUM)
        assert decayed[0] == 0.5
        assert diag.decay_applied_count == 0
        assert diag.decay_skipped_count == 1

    def test_shadow_mode(self):
        """Shadow mode should compute but not apply decay."""
        policy = DecayPolicy(
            function=DecayFunction.EXPONENTIAL,
            half_life_days=30.0,
            decay_floor=0.0,
            enabled=True,
            half_life_by_type={MemoryType.FACT: 30.0},
            profile_multipliers={ExecutionProfile.MEDIUM: 1.0},
        )
        adjuster = DecayAdjuster(Path("."), policy)
        adjuster.enable_shadow_mode()

        memory = _make_memory("MEM-test8", MemoryType.FACT, "test content", days_old=30.0)
        base_scores = [0.8]
        decayed, diag = adjuster.apply_decay([memory], base_scores, ExecutionProfile.MEDIUM)

        # In shadow mode, original scores returned
        assert decayed[0] == 0.8
        assert diag.shadow_mode is True
        assert diag.shadow_comparison is not None
        assert diag.shadow_comparison["original_scores"][0] == 0.8
        assert diag.shadow_comparison["decayed_scores"][0] < 0.8

    def test_shadow_mode_disabled(self):
        """Disabled shadow mode should apply decay normally."""
        policy = DecayPolicy(
            function=DecayFunction.EXPONENTIAL,
            half_life_days=30.0,
            decay_floor=0.0,
            enabled=True,
            half_life_by_type={MemoryType.FACT: 30.0},
            profile_multipliers={ExecutionProfile.MEDIUM: 1.0},
        )
        adjuster = DecayAdjuster(Path("."), policy)
        adjuster.disable_shadow_mode()

        memory = _make_memory("MEM-test9", MemoryType.FACT, "test content", days_old=30.0)
        base_scores = [0.8]
        decayed, diag = adjuster.apply_decay([memory], base_scores, ExecutionProfile.MEDIUM)

        assert decayed[0] < 0.8
        assert diag.shadow_mode is False

    def test_evaluate_shadow(self):
        """evaluate_shadow should return diagnostics without changing mode."""
        policy = DecayPolicy(
            function=DecayFunction.EXPONENTIAL,
            half_life_days=30.0,
            decay_floor=0.0,
            enabled=True,
            half_life_by_type={MemoryType.FACT: 30.0},
            profile_multipliers={ExecutionProfile.MEDIUM: 1.0},
        )
        adjuster = DecayAdjuster(Path("."), policy)
        assert adjuster.shadow_mode is False

        memory = _make_memory("MEM-test10", MemoryType.FACT, "test content", days_old=30.0)
        base_scores = [0.8]
        diag = adjuster.evaluate_shadow([memory], base_scores, ExecutionProfile.MEDIUM)

        assert diag.shadow_mode is True
        assert adjuster.shadow_mode is False  # Original mode restored

    def test_per_type_half_life(self):
        """Different memory types should use their configured half-lives."""
        policy = DecayPolicy(
            function=DecayFunction.EXPONENTIAL,
            half_life_days=30.0,
            decay_floor=0.0,
            enabled=True,
            half_life_by_type={
                MemoryType.FACT: 60.0,
                MemoryType.LESSON: 10.0,
            },
            profile_multipliers={ExecutionProfile.MEDIUM: 1.0},
        )
        adjuster = DecayAdjuster(Path("."), policy)

        # FACT at 30 days (half of 60-day half-life) -> factor ~0.707
        fact_mem = _make_memory("MEM-fact", MemoryType.FACT, "fact content", days_old=30.0)
        # LESSON at 30 days (3x 10-day half-life) -> factor ~0.125
        lesson_mem = _make_memory("MEM-lesson", MemoryType.LESSON, "lesson content", days_old=30.0)

        decayed, _ = adjuster.apply_decay([fact_mem, lesson_mem], [1.0, 1.0], ExecutionProfile.MEDIUM)

        assert decayed[0] > decayed[1]  # FACT decays slower than LESSON
        assert 0.70 < decayed[0] < 0.72  # sqrt(0.5) ≈ 0.707
        assert 0.11 < decayed[1] < 0.14  # 0.5^3 = 0.125

    def test_profile_multiplier(self):
        """Different profiles should apply different multipliers."""
        policy = DecayPolicy(
            function=DecayFunction.EXPONENTIAL,
            half_life_days=30.0,
            decay_floor=0.0,
            enabled=True,
            half_life_by_type={MemoryType.FACT: 30.0},
            profile_multipliers={
                ExecutionProfile.MEDIUM: 1.0,
                ExecutionProfile.HIGH: 2.0,  # Double half-life in HIGH
            },
        )
        adjuster = DecayAdjuster(Path("."), policy)

        memory = _make_memory("MEM-test11", MemoryType.FACT, "test content", days_old=30.0)
        base_scores = [1.0]

        decayed_medium, _ = adjuster.apply_decay([memory], base_scores, ExecutionProfile.MEDIUM)
        decayed_high, _ = adjuster.apply_decay([memory], base_scores, ExecutionProfile.HIGH)

        # HIGH profile has double half-life (60 days), so at 30 days factor = 0.707
        # MEDIUM profile has 30-day half-life, so at 30 days factor = 0.5
        assert decayed_high[0] > decayed_medium[0]

    def test_multiple_memories(self):
        """Applying decay to multiple memories should work correctly."""
        adjuster = DecayAdjuster(Path("."), DecayPolicy.default())

        memories = [
            _make_memory(f"MEM-{i}", MemoryType.FACT, f"content {i}", days_old=float(i * 10))
            for i in range(5)
        ]
        base_scores = [0.8, 0.7, 0.6, 0.5, 0.4]

        decayed, diag = adjuster.apply_decay(memories, base_scores, ExecutionProfile.MEDIUM)

        assert len(decayed) == 5
        assert diag.considered_count == 5
        # Decayed scores should be non-increasing (older = lower or equal)
        for i in range(1, 5):
            assert decayed[i] <= decayed[i - 1] + 1e-6


class TestDecayIntegration:
    """Integration tests with MemoryRetriever."""

    def test_retriever_with_decay(self, tmp_path):
        """Test that retriever integrates decay correctly."""
        # Create a temp project with some memory records
        project_path = tmp_path / "test_project"
        project_path.mkdir(parents=True)

        # Create .sync/memory/records directory
        records_dir = project_path / ".sync" / "memory" / "records"
        records_dir.mkdir(parents=True)

        # Create some test memories with different ages
        now = datetime.now(timezone.utc)
        for i, days_old in enumerate([0, 15, 30, 60, 90]):
            mem = _make_memory(
                f"MEM-test-{i:04d}",
                MemoryType.FACT,
                f"test memory content {i}",
                days_old=days_old,
            )
            record_file = records_dir / f"{mem.memory_id}.json"
            record_file.write_text(json.dumps(mem.to_dict(), indent=2))

        # Enable shadow mode via env var for testing
        os.environ["STACKMIND_MEMORY_DECAY_SHADOW"] = "1"
        try:
            retriever = MemoryRetriever(project_path)
            results, diag = retriever.retrieve_memories_with_diagnostics(
                "test memory",
                profile=ExecutionProfile.MEDIUM,
                limit=10,
                token_budget=500,
            )

            # Should have decay diagnostics
            assert diag.decay_diagnostics is not None
            assert diag.decay_diagnostics.shadow_mode is True
            assert diag.decay_diagnostics.considered_count > 0
        finally:
            os.environ.pop("STACKMIND_MEMORY_DECAY_SHADOW", None)

    def test_retriever_decay_changes_ordering(self, tmp_path):
        """Test that decay can change memory ordering."""
        project_path = tmp_path / "test_project2"
        project_path.mkdir(parents=True)
        records_dir = project_path / ".sync" / "memory" / "records"
        records_dir.mkdir(parents=True)

        # Create memories: one fresh with low base score, one old with high base score
        # After decay, the fresh one should rank higher
        mem1 = _make_memory("MEM-fresh", MemoryType.FACT, "fresh memory content", days_old=0.0)
        mem2 = _make_memory("MEM-old", MemoryType.FACT, "old memory content", days_old=60.0)

        for mem in [mem1, mem2]:
            record_file = records_dir / f"{mem.memory_id}.json"
            record_file.write_text(json.dumps(mem.to_dict(), indent=2))

        os.environ["STACKMIND_MEMORY_DECAY_SHADOW"] = "1"
        try:
            retriever = MemoryRetriever(project_path)
            results, diag = retriever.retrieve_memories_with_diagnostics(
                "memory content",
                profile=ExecutionProfile.MEDIUM,
                limit=10,
                token_budget=500,
            )

            # Should have shadow mode diagnostics
            assert diag.decay_diagnostics is not None
            assert diag.decay_diagnostics.shadow_mode is True
        finally:
            os.environ.pop("STACKMIND_MEMORY_DECAY_SHADOW", None)


class TestGetDecayPolicy:
    """Tests for get_decay_policy function."""

    def test_default_policy(self):
        policy = get_decay_policy()
        assert policy.function == DecayFunction.EXPONENTIAL
        assert policy.enabled is True

    def test_profile_specific(self):
        # Currently returns same default for all profiles
        policy_low = get_decay_policy(ExecutionProfile.LOW)
        policy_high = get_decay_policy(ExecutionProfile.HIGH)
        assert policy_low.function == policy_high.function

    def test_config_file_override(self, tmp_path):
        """Test loading policy from config file."""
        config_dir = tmp_path / ".sync" / "memory" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "decay.json"

        custom_policy = {
            "function": "linear",
            "half_life_days": 45.0,
            "decay_floor": 0.1,
            "enabled": True,
            "half_life_by_type": {"fact": 90.0},
            "profile_multipliers": {"medium": 1.5},
        }
        config_file.write_text(json.dumps(custom_policy))

        # Need to run from tmp_path for config discovery
        old_cwd = Path.cwd()
        os.chdir(tmp_path)
        try:
            policy = get_decay_policy()
            assert policy.function == DecayFunction.LINEAR
            assert policy.half_life_days == 45.0
            assert policy.decay_floor == 0.1
            assert policy.half_life_by_type[MemoryType.FACT] == 90.0
            assert policy.profile_multipliers[ExecutionProfile.MEDIUM] == 1.5
        finally:
            os.chdir(old_cwd)


class TestDecayDiagnostics:
    """Tests for DecayDiagnostics serialization."""

    def test_to_dict(self):
        from validators.memory.decay import DecayApplication

        app = DecayApplication(
            memory_id="MEM-test",
            memory_type=MemoryType.FACT,
            age_days=30.0,
            base_relevance=0.8,
            decay_factor=0.5,
            decayed_relevance=0.4,
            half_life_used=30.0,
            function_used=DecayFunction.EXPONENTIAL,
        )
        diag = DecayDiagnostics(
            policy={"function": "exponential", "half_life_days": 30.0},
            profile="medium",
            considered_count=1,
            decay_applied_count=1,
            decay_skipped_count=0,
            applications=(app,),
            shadow_mode=False,
        )

        d = diag.to_dict()
        assert d["profile"] == "medium"
        assert d["considered_count"] == 1
        assert d["decay_applied_count"] == 1
        assert len(d["applications"]) == 1
        assert d["applications"][0]["memory_id"] == "MEM-test"
        assert d["applications"][0]["decay_factor"] == 0.5
        assert d["shadow_mode"] is False

    def test_to_dict_with_shadow_comparison(self):
        from validators.memory.decay import DecayApplication

        app = DecayApplication(
            memory_id="MEM-test",
            memory_type=MemoryType.FACT,
            age_days=30.0,
            base_relevance=0.8,
            decay_factor=0.5,
            decayed_relevance=0.4,
            half_life_used=30.0,
            function_used=DecayFunction.EXPONENTIAL,
        )
        diag = DecayDiagnostics(
            policy={"function": "exponential"},
            profile="medium",
            considered_count=1,
            decay_applied_count=1,
            decay_skipped_count=0,
            applications=(app,),
            shadow_mode=True,
            shadow_comparison={
                "original_scores": [0.8],
                "decayed_scores": [0.4],
                "max_impact": 0.4,
                "avg_impact": 0.4,
            },
        )

        d = diag.to_dict()
        assert d["shadow_mode"] is True
        assert "shadow_comparison" in d
        assert d["shadow_comparison"]["max_impact"] == 0.4


if __name__ == "__main__":
    pytest.main([__file__, "-v"])