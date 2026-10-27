"""Filesystem-backed versioned storage and lifecycle manager for StackMind skills.

Implements Phase 3 (Skill Storage & Versioning) of Verified Procedural Learning.
Organizes skills under:
  .sync/skills/manifests/<name>/v<version>.json  (Immutable historical versions)
  .sync/skills/active/<name>.json               (Active promoted pointer)
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from validators.skill.models import (
    RiskTier,
    SkillApplicability,
    SkillMetrics,
    SkillProvenance,
    SkillRecord,
    SkillStatus,
    SkillStep,
)


class SkillStore:
    """Manages versioned skill storage, lifecycle promotion, and rollback."""

    def __init__(self, project_path: Path | str | None = None) -> None:
        self.project_path = Path(project_path if project_path is not None else Path.cwd()).resolve()
        self.skills_dir = self.project_path / ".sync" / "skills"
        self.manifests_dir = self.skills_dir / "manifests"
        self.active_dir = self.skills_dir / "active"

        try:
            self.manifests_dir.mkdir(parents=True, exist_ok=True)
            self.active_dir.mkdir(parents=True, exist_ok=True)
            self.local_agent_skills_dir = self.skills_dir / "agents-skills"
            self.local_agent_skills_dir.mkdir(parents=True, exist_ok=True)
        except (PermissionError, OSError):
            self.local_agent_skills_dir = self.skills_dir / "agents-skills"

        try:
            from validators.kernel.home import get_global_agent_skills_dir, get_global_skills_dir
            self.global_skills_dir = get_global_skills_dir()
            self.global_active_dir = self.global_skills_dir / "global"
            self.global_active_dir.mkdir(parents=True, exist_ok=True)
            self.global_agent_skills_dir = get_global_agent_skills_dir()
            self.global_agent_skills_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            self.global_skills_dir = None
            self.global_active_dir = None
            self.global_agent_skills_dir = None

    def _skill_manifest_dir(self, name: str) -> Path:
        slug = name.strip().lower()
        d = self.manifests_dir / slug
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _manifest_path(self, name: str, version: int) -> Path:
        return self._skill_manifest_dir(name) / f"v{version}.json"

    def _active_path(self, name: str) -> Path:
        slug = name.strip().lower()
        return self.active_dir / f"{slug}.json"

    def _write_atomic(self, target_path: Path, data: dict[str, Any]) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = target_path.with_suffix(".tmp")
        payload = json.dumps(data, indent=2, sort_keys=True) + "\n"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, target_path)

    def save_version(self, skill: SkillRecord) -> Path:
        """Persist a versioned skill manifest and update active pointer if ACTIVE."""
        manifest_path = self._manifest_path(skill.name, skill.version)
        self._write_atomic(manifest_path, skill.to_dict())

        active_path = self._active_path(skill.name)
        if skill.status == SkillStatus.ACTIVE:
            self._write_atomic(active_path, skill.to_dict())
        elif active_path.exists():
            # If current active was this version and is no longer active, remove active pointer
            try:
                with open(active_path, "r", encoding="utf-8") as f:
                    curr_active = json.load(f)
                if curr_active.get("version") == skill.version:
                    active_path.unlink()
            except Exception:
                pass

        return manifest_path

    def get_skill(self, name: str, version: int | None = None, role: str | None = None) -> SkillRecord | None:
        """Load a skill by name, optional version, and optional role."""
        slug = name.strip().lower()

        if role:
            clean_role = role.strip().lower()
            # Check local role dir (.sync/skills/agents-skills/<role>/<slug>.json)
            local_r = self.local_agent_skills_dir / clean_role / f"{slug}.json"
            if local_r.is_file():
                try:
                    with open(local_r, "r", encoding="utf-8") as f:
                        return SkillRecord.from_dict(json.load(f))
                except Exception:
                    pass
            # Check global role dir (~/.stackmindcli/skills/agents-skills/<role>/<slug>.json)
            if self.global_agent_skills_dir and self.global_agent_skills_dir.is_dir():
                glob_r = self.global_agent_skills_dir / clean_role / f"{slug}.json"
                if glob_r.is_file():
                    try:
                        with open(glob_r, "r", encoding="utf-8") as f:
                            return SkillRecord.from_dict(json.load(f))
                    except Exception:
                        pass

        if version is not None:
            path = self._manifest_path(slug, version)
            if not path.exists():
                return None
            with open(path, "r", encoding="utf-8") as f:
                return SkillRecord.from_dict(json.load(f))

        # Check active first
        active_path = self._active_path(slug)
        if active_path.exists():
            with open(active_path, "r", encoding="utf-8") as f:
                return SkillRecord.from_dict(json.load(f))

        # Fallback to highest version manifest
        latest_ver = self.get_latest_version_number(slug)
        if latest_ver > 0:
            return self.get_skill(slug, latest_ver)

        # Fallback to centralized global skills (~/.stackmindcli/skills/global)
        if self.global_active_dir and self.global_active_dir.is_dir():
            g_path = self.global_active_dir / f"{slug}.json"
            if g_path.is_file():
                try:
                    with open(g_path, "r", encoding="utf-8") as f:
                        return SkillRecord.from_dict(json.load(f))
                except Exception:
                    pass

        # Check all role dirs if role was not explicitly specified
        if not role:
            # Check local agent-skills/*/<slug>.json
            if self.local_agent_skills_dir.is_dir():
                for r_dir in self.local_agent_skills_dir.iterdir():
                    if r_dir.is_dir():
                        cand = r_dir / f"{slug}.json"
                        if cand.is_file():
                            try:
                                with open(cand, "r", encoding="utf-8") as f:
                                    return SkillRecord.from_dict(json.load(f))
                            except Exception:
                                pass
            # Check global agent-skills/*/<slug>.json
            if self.global_agent_skills_dir and self.global_agent_skills_dir.is_dir():
                for r_dir in self.global_agent_skills_dir.iterdir():
                    if r_dir.is_dir():
                        cand = r_dir / f"{slug}.json"
                        if cand.is_file():
                            try:
                                with open(cand, "r", encoding="utf-8") as f:
                                    return SkillRecord.from_dict(json.load(f))
                            except Exception:
                                pass

        return None

    def get_latest_version_number(self, name: str) -> int:
        """Find the highest version number for a skill."""
        skill_dir = self._skill_manifest_dir(name)
        versions = []
        for p in skill_dir.glob("v*.json"):
            try:
                ver_num = int(p.stem.replace("v", ""))
                versions.append(ver_num)
            except ValueError:
                pass
        return max(versions, default=0)

    def list_skills(
        self,
        *,
        status: SkillStatus | None = None,
        active_only: bool = False,
        include_all_versions: bool = False,
        include_global: bool = True,
        role: str | None = None,
    ) -> list[SkillRecord]:
        """List skills in the repository and global library, optionally filtered by agent role."""
        records: list[SkillRecord] = []
        seen_slugs: set[str] = set()

        if role:
            clean_role = role.strip().lower()

            # 1. Local role-specific skills (.sync/skills/agents-skills/<role>/*.json)
            local_rdir = self.local_agent_skills_dir / clean_role
            if local_rdir.is_dir():
                for p in local_rdir.glob("*.json"):
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            r = SkillRecord.from_dict(json.load(f))
                            if status is None or r.status == status:
                                records.append(r)
                                seen_slugs.add(r.name.strip().lower())
                    except Exception:
                        continue

            # 2. Global role-specific skills (~/.stackmindcli/skills/agents-skills/<role>/*.json)
            if include_global and self.global_agent_skills_dir and self.global_agent_skills_dir.is_dir():
                glob_rdir = self.global_agent_skills_dir / clean_role
                if glob_rdir.is_dir():
                    for p in glob_rdir.glob("*.json"):
                        slug = p.stem.strip().lower()
                        if slug in seen_slugs:
                            continue
                        try:
                            with open(p, "r", encoding="utf-8") as f:
                                r = SkillRecord.from_dict(json.load(f))
                                if status is None or r.status == status:
                                    records.append(r)
                                    seen_slugs.add(slug)
                        except Exception:
                            continue

            # 3. Universal cross-cutting skills (role is None or role == clean_role)
            # Local active
            for p in self.active_dir.glob("*.json"):
                slug = p.stem.strip().lower()
                if slug in seen_slugs:
                    continue
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        r = SkillRecord.from_dict(json.load(f))
                        if r.role and r.role.strip().lower() != clean_role:
                            continue
                        if status is None or r.status == status:
                            records.append(r)
                            seen_slugs.add(slug)
                except Exception:
                    continue

            # Global active
            if include_global and self.global_active_dir and self.global_active_dir.is_dir():
                for p in self.global_active_dir.glob("*.json"):
                    slug = p.stem.strip().lower()
                    if slug in seen_slugs:
                        continue
                    try:
                        with open(p, "r", encoding="utf-8") as f:
                            r = SkillRecord.from_dict(json.load(f))
                            if r.role and r.role.strip().lower() != clean_role:
                                continue
                            if status is None or r.status == status:
                                records.append(r)
                                seen_slugs.add(slug)
                    except Exception:
                        continue

            return sorted(records, key=lambda s: s.name)

        # When role is None, list all skills across standard active/manifests, global, and all role folders
        if active_only:
            for p in self.active_dir.glob("*.json"):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        r = SkillRecord.from_dict(json.load(f))
                        records.append(r)
                        seen_slugs.add(r.name.strip().lower())
                except Exception:
                    continue
        else:
            # Iterate over all skill directories
            for skill_dir in self.manifests_dir.iterdir():
                if not skill_dir.is_dir():
                    continue
                if include_all_versions:
                    for p in skill_dir.glob("v*.json"):
                        try:
                            with open(p, "r", encoding="utf-8") as f:
                                r = SkillRecord.from_dict(json.load(f))
                                if status is None or r.status == status:
                                    records.append(r)
                                    seen_slugs.add(r.name.strip().lower())
                        except Exception:
                            continue
                else:
                    latest = self.get_skill(skill_dir.name)
                    if latest and (status is None or latest.status == status):
                        records.append(latest)
                        seen_slugs.add(latest.name.strip().lower())

        # Include local role-specific skills (.sync/skills/agents-skills/*/*.json)
        if self.local_agent_skills_dir.is_dir():
            for r_dir in self.local_agent_skills_dir.iterdir():
                if r_dir.is_dir():
                    for p in r_dir.glob("*.json"):
                        slug = p.stem.strip().lower()
                        if slug in seen_slugs:
                            continue
                        try:
                            with open(p, "r", encoding="utf-8") as f:
                                r = SkillRecord.from_dict(json.load(f))
                                if status is None or r.status == status:
                                    records.append(r)
                                    seen_slugs.add(slug)
                        except Exception:
                            continue

        # Merge in global skills from ~/.stackmindcli/skills/global
        if include_global and self.global_active_dir and self.global_active_dir.is_dir():
            for p in self.global_active_dir.glob("*.json"):
                slug = p.stem.strip().lower()
                if slug in seen_slugs:
                    continue
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        g_rec = SkillRecord.from_dict(json.load(f))
                        if status is None or g_rec.status == status:
                            records.append(g_rec)
                            seen_slugs.add(slug)
                except Exception:
                    continue

        # Merge in global role skills from ~/.stackmindcli/skills/agents-skills/*/*.json
        if include_global and self.global_agent_skills_dir and self.global_agent_skills_dir.is_dir():
            for r_dir in self.global_agent_skills_dir.iterdir():
                if r_dir.is_dir():
                    for p in r_dir.glob("*.json"):
                        slug = p.stem.strip().lower()
                        if slug in seen_slugs:
                            continue
                        try:
                            with open(p, "r", encoding="utf-8") as f:
                                g_rec = SkillRecord.from_dict(json.load(f))
                                if status is None or g_rec.status == status:
                                    records.append(g_rec)
                                    seen_slugs.add(slug)
                        except Exception:
                            continue

        return sorted(records, key=lambda s: s.name)

    def save_global_skill(self, skill: SkillRecord) -> Path:
        """Persist a procedural skill to the centralized ~/.stackmindcli/skills/global library."""
        if not self.global_active_dir:
            from validators.kernel.home import get_global_skills_dir
            self.global_skills_dir = get_global_skills_dir()
            self.global_active_dir = self.global_skills_dir / "global"
            self.global_active_dir.mkdir(parents=True, exist_ok=True)
        slug = skill.name.strip().lower()
        target_path = self.global_active_dir / f"{slug}.json"
        self._write_atomic(target_path, skill.to_dict())
        return target_path

    def save_agent_skill(self, skill: SkillRecord, role: str, is_global: bool = True) -> Path:
        """Persist a procedural skill to the dedicated agent role library (~/.stackmindcli/skills/agents-skills/<role> or .sync/skills/agents-skills/<role>)."""
        clean_role = role.strip().lower()
        if skill.role != clean_role:
            skill = SkillRecord(
                skill_id=skill.skill_id,
                name=skill.name,
                version=skill.version,
                status=skill.status,
                risk_tier=skill.risk_tier,
                description=skill.description,
                applicability=skill.applicability,
                steps=skill.steps,
                provenance=skill.provenance,
                metrics=skill.metrics,
                constraints=skill.constraints,
                fallback_procedure=skill.fallback_procedure,
                role=clean_role,
                schema_version=skill.schema_version,
            )
        slug = skill.name.strip().lower()
        if is_global:
            if not self.global_agent_skills_dir:
                from validators.kernel.home import get_global_agent_skills_dir
                self.global_agent_skills_dir = get_global_agent_skills_dir()
            target_dir = self.global_agent_skills_dir / clean_role
        else:
            target_dir = self.local_agent_skills_dir / clean_role

        target_dir.mkdir(parents=True, exist_ok=True)
        target_path = target_dir / f"{slug}.json"
        self._write_atomic(target_path, skill.to_dict())
        return target_path

    def promote_version(
        self,
        name: str,
        version: int,
        *,
        reason: str = "Passed verification and canary checks",
        author_agent: str = "claude",
        is_human: bool = False,
        allow_medium_auto: bool = False,
        skip_pipeline: bool = False,
        skip_governor: bool = False,
    ) -> SkillRecord:
        """Promote a specific version of a skill to ACTIVE status after passing verification pipeline and risk governance."""
        current = self.get_skill(name, version)
        if current is None:
            raise ValueError(f"Skill '{name}' version {version} does not exist.")

        if not skip_pipeline:
            from validators.verification.pipeline import VerificationPipeline
            pipeline_result = VerificationPipeline.verify_skill(current, self.project_path)
            if not pipeline_result.passed:
                failed_stages = [s.stage_name for s in pipeline_result.stage_results if not s.passed]
                stage_errors = "; ".join(
                    f"{s.stage_name}: {', '.join(s.messages)}"
                    for s in pipeline_result.stage_results
                    if not s.passed
                )
                raise ValueError(
                    f"Promotion rejected: Skill '{name}' v{version} failed verification pipeline "
                    f"in stages {failed_stages}. Details: {stage_errors}"
                )

        if not skip_governor:
            from validators.skill.governor import PromotionGovernor
            governor = PromotionGovernor(self.project_path)
            governor.enforce_promotion_governance(
                current,
                actor=author_agent,
                is_human=is_human,
                allow_medium_auto=allow_medium_auto,
            )

        prov = SkillProvenance(
            source_experience_ids=current.provenance.source_experience_ids,
            created_at=current.provenance.created_at,
            author_agent=author_agent,
            promotion_reason=reason,
            previous_version_id=current.provenance.previous_version_id,
        )

        promoted = SkillRecord(
            skill_id=current.skill_id,
            name=current.name,
            version=current.version,
            status=SkillStatus.ACTIVE,
            risk_tier=current.risk_tier,
            description=current.description,
            applicability=current.applicability,
            steps=current.steps,
            constraints=current.constraints,
            fallback_procedure=current.fallback_procedure,
            provenance=prov,
            metrics=current.metrics,
            schema_version=current.schema_version,
        )
        self.save_version(promoted)
        return promoted

    def rollback_version(
        self,
        name: str,
        target_version: int,
        *,
        reason: str = "Rolled back to previous stable version",
        author_agent: str = "claude",
    ) -> SkillRecord:
        """Roll back a skill by creating a new active version copying target version procedure."""
        target = self.get_skill(name, target_version)
        if target is None:
            raise ValueError(f"Target rollback version {target_version} does not exist for skill '{name}'.")

        current_active = self.get_skill(name)
        new_version_num = self.get_latest_version_number(name) + 1
        new_skill_id = SkillRecord.mint_id(name, new_version_num)
        now_iso = datetime.now(timezone.utc).isoformat()

        prov = SkillProvenance(
            source_experience_ids=target.provenance.source_experience_ids,
            created_at=now_iso,
            author_agent=author_agent,
            promotion_reason=f"Rollback to v{target_version}: {reason}",
            previous_version_id=current_active.skill_id if current_active else None,
        )

        new_record = SkillRecord(
            skill_id=new_skill_id,
            name=name,
            version=new_version_num,
            status=SkillStatus.ACTIVE,
            risk_tier=target.risk_tier,
            description=f"{target.description} (Rolled back to v{target_version})",
            applicability=target.applicability,
            steps=target.steps,
            constraints=target.constraints,
            fallback_procedure=target.fallback_procedure,
            provenance=prov,
            metrics=SkillMetrics(confidence_score=target.metrics.confidence_score),
            schema_version=target.schema_version,
        )
        self.save_version(new_record)
        return new_record

    def deprecate_skill(self, name: str, *, reason: str = "Deprecated", version: int | None = None) -> SkillRecord:
        """Transition a skill to DEPRECATED status and remove active pointer."""
        target = self.get_skill(name, version)
        if target is None:
            raise ValueError(f"Skill '{name}' does not exist.")

        deprecated = SkillRecord(
            skill_id=target.skill_id,
            name=target.name,
            version=target.version,
            status=SkillStatus.DEPRECATED,
            risk_tier=target.risk_tier,
            description=target.description,
            applicability=target.applicability,
            steps=target.steps,
            constraints=target.constraints,
            fallback_procedure=target.fallback_procedure,
            provenance=target.provenance,
            metrics=target.metrics,
            schema_version=target.schema_version,
        )
        self.save_version(deprecated)
        return deprecated

    def stats(self) -> dict[str, Any]:
        """Aggregate skill store metrics."""
        all_manifests = self.list_skills(include_all_versions=True)
        active_skills = self.list_skills(active_only=True)
        distinct_names = {s.name for s in all_manifests}

        status_counts = {
            SkillStatus.CANDIDATE: 0,
            SkillStatus.EXPERIMENTAL: 0,
            SkillStatus.ACTIVE: 0,
            SkillStatus.STALE: 0,
            SkillStatus.DEPRECATED: 0,
            SkillStatus.ARCHIVED: 0,
        }
        for s in all_manifests:
            status_counts[s.status] = status_counts.get(s.status, 0) + 1

        return {
            "active_skills": len(active_skills),
            "candidates": status_counts[SkillStatus.CANDIDATE],
            "deprecated": status_counts[SkillStatus.DEPRECATED],
            "distinct_skills": len(distinct_names),
            "experimental": status_counts[SkillStatus.EXPERIMENTAL],
            "stale": status_counts[SkillStatus.STALE],
            "total_versions": len(all_manifests),
        }
