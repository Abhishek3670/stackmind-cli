"""Execution Profile Calibration and Benchmark Harness.

Executes a comparative benchmark across Low, Medium, and High execution profiles
measuring:
1. Skills considered, selected, loaded, and injected.
2. Estimated vs. measured token consumption.
3. Policy limits and token budget enforcement.
4. Latency and verification depth.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# Ensure repo root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cli.init import init
from validators.kernel.profile import ExecutionProfile, get_profile_policy
from validators.skill.models import (
    RiskTier,
    SkillApplicability,
    SkillMetrics,
    SkillProvenance,
    SkillRecord,
    SkillStatus,
    SkillStep,
)
from validators.skill.retriever import SkillRetriever
from validators.skill.store import SkillStore


@dataclass
class BenchmarkResult:
    profile: str
    task: str
    considered: int
    selected: int
    loaded: int
    injected: int
    estimated_tokens: int
    budget: int
    budget_exceeded: bool
    verification_depth: str
    elapsed_ms: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def setup_benchmark_skills(workspace: Path) -> None:
    store = SkillStore(workspace)

    skills_data = [
        (
            "backend-rest-endpoints",
            "FastAPI and Python REST endpoint design, validation, and error handling",
            ("src/api/*", "src/endpoints/*"),
            [
                SkillStep(1, "Define Pydantic schema", "bash", "cat schema.py"),
                SkillStep(2, "Implement router endpoint", "bash", "cat router.py"),
                SkillStep(3, "Add status code assertions", "bash", "pytest tests/api"),
            ],
            0.95,
        ),
        (
            "pytest-assertions-and-fixtures",
            "Pytest test suite structuring, mock fixtures, and coverage verification",
            ("tests/*", "src/*"),
            [
                SkillStep(1, "Create isolated fixture", "bash", "pytest -k fixtures"),
                SkillStep(2, "Run assertions with coverage", "bash", "pytest --cov"),
            ],
            0.90,
        ),
        (
            "gitops-hygiene-and-release",
            "Semantic release tagging, changelog generation, and branch hygiene",
            ("release/*", "docs/*"),
            [
                SkillStep(1, "Verify git tree clean", "bash", "git status --porcelain"),
                SkillStep(2, "Tag semantic version", "bash", "git tag -a v1.0.0"),
            ],
            0.85,
        ),
        (
            "frontend-component-view",
            "React and Tailwind UI components, client state, and responsive layout",
            ("ui/*", "src/components/*"),
            [
                SkillStep(1, "Scaffold component", "bash", "npm run build"),
            ],
            0.80,
        ),
        (
            "architecture-plan-decomposition",
            "System architecture planning, work order dependency DAG decomposition",
            ("docs/*", "plans/*"),
            [
                SkillStep(1, "Draft PLAN.md milestones", "bash", "cat PLAN.md"),
                SkillStep(2, "Assign agent work orders", "bash", "stackmind wo list"),
            ],
            0.92,
        ),
    ]

    for name, desc, modules, steps, conf in skills_data:
        rec = SkillRecord(
            skill_id=SkillRecord.mint_id(name, 1),
            name=name,
            version=1,
            status=SkillStatus.ACTIVE,
            risk_tier=RiskTier.LOW,
            description=desc,
            applicability=SkillApplicability(preconditions=("Workspace ready",), target_modules=modules),
            steps=tuple(steps),
            provenance=SkillProvenance(
                source_experience_ids=(),
                created_at="2026-10-12T12:00:00Z",
                author_agent="claude",
            ),
            metrics=SkillMetrics(confidence_score=conf),
        )
        store.save_version(rec)
        store.promote_version(name, 1, skip_pipeline=True, skip_governor=True)


def run_profile_calibration() -> list[BenchmarkResult]:
    temp_dir = Path(tempfile.mkdtemp(prefix="stackmind-calibration-"))
    try:
        init(temp_dir, name="Calibration Project", no_git=True)
        schemas_src = Path(__file__).parent.parent / "schemas"
        if schemas_src.exists():
            shutil.copytree(schemas_src, temp_dir / "schemas", dirs_exist_ok=True)
        setup_benchmark_skills(temp_dir)

        retriever = SkillRetriever(temp_dir)

        tasks = [
            "Implement FastAPI REST endpoints with validation",
            "Write comprehensive pytest unit tests and assertions",
            "Execute complete architecture planning, endpoint refactoring, and gitops release",
        ]

        profiles = [ExecutionProfile.LOW, ExecutionProfile.MEDIUM, ExecutionProfile.HIGH]
        results: list[BenchmarkResult] = []

        for task in tasks:
            for prof in profiles:
                policy = get_profile_policy(prof)
                t0 = time.monotonic()
                injected, diag = retriever.retrieve_skills_with_diagnostics(task, policy=policy)
                elapsed_ms = (time.monotonic() - t0) * 1000

                res = BenchmarkResult(
                    profile=prof.value.upper(),
                    task=task[:45] + ("..." if len(task) > 45 else ""),
                    considered=diag.considered_count,
                    selected=diag.selected_count,
                    loaded=diag.loaded_count,
                    injected=diag.injected_count,
                    estimated_tokens=diag.estimated_skill_tokens,
                    budget=policy.skill_token_budget,
                    budget_exceeded=diag.budget_exceeded,
                    verification_depth=policy.verification_depth.value,
                    elapsed_ms=round(elapsed_ms, 2),
                )
                results.append(res)

        return results
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def print_calibration_report(results: list[BenchmarkResult]) -> None:
    header = (
        f"{'Profile':<8} | {'Task':<48} | {'Cons':<4} | {'Sel':<3} | {'Load':<4} | "
        f"{'Inj':<3} | {'Tokens (Est)':<12} | {'Budget':<6} | {'Verif Depth':<11} | {'Latency':<8}"
    )
    print("=" * len(header))
    print("STACKMIND SKILLS EXECUTION PROFILE CALIBRATION REPORT")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for r in results:
        print(
            f"{r.profile:<8} | {r.task:<48} | {r.considered:<4} | {r.selected:<3} | {r.loaded:<4} | "
            f"{r.injected:<3} | {r.estimated_tokens:<12} | {r.budget:<6} | {r.verification_depth:<11} | {r.elapsed_ms:.2f}ms"
        )
    print("=" * len(header))

    # Summary by Profile
    print("\n--- Summary by Profile ---")
    for p_name in ["LOW", "MEDIUM", "HIGH"]:
        p_res = [r for r in results if r.profile == p_name]
        avg_tokens = sum(r.estimated_tokens for r in p_res) / len(p_res) if p_res else 0
        avg_injected = sum(r.injected for r in p_res) / len(p_res) if p_res else 0
        avg_lat = sum(r.elapsed_ms for r in p_res) / len(p_res) if p_res else 0
        print(
            f"{p_name:<8}: Avg Injected = {avg_injected:.1f} skills | "
            f"Avg Tokens = {avg_tokens:.1f} (est) | "
            f"Avg Retrieval Latency = {avg_lat:.2f}ms"
        )


if __name__ == "__main__":
    benchmark_results = run_profile_calibration()
    print_calibration_report(benchmark_results)
