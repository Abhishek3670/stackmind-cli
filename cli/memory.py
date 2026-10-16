"""CLI commands for StackMind Durable Memory management."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import click
import yaml

from cli.init import init
from validators.memory.models import MemoryRecord, MemoryStatus, MemoryType
from validators.memory.store import MemoryCompiler, MemoryStore
from validators.memory.promotion import MemoryPromotionService, PromotionDecision
from validators.memory.contradiction import (
    ContradictionDetector,
    ContradictionResolutionService,
    ContradictionFinding,
    ContradictionCategory,
    ContradictionSeverity,
    ContradictionStatus,
)
from validators.memory.decay import DecayPolicy, DecayAdjuster, DecayFunction, get_decay_policy
from validators.kernel.profile import ExecutionProfile
from validators.experience.store import ExperienceStore


@click.group(name="memory")
def memory_group():
    """Durable memory management: compile, list, search, promote, and delete."""
    pass


@memory_group.command("list")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--status", type=click.Choice([s.value for s in MemoryStatus]), help="Filter by status")
@click.option("--type", "memory_type", type=click.Choice([t.value for t in MemoryType]), help="Filter by memory type")
@click.option("--project-id", help="Filter by project ID")
@click.option("--limit", type=int, default=20, help="Maximum results")
@click.option("--format", "fmt", type=click.Choice(["table", "json", "yaml"]), default="table", help="Output format")
@click.option("--include-expired/--no-expired", default=False, help="Include EXPIRED memories")
@click.option("--include-deleted/--no-deleted", default=False, help="Include DELETED memories")
def memory_list(project, status, memory_type, project_id, limit, fmt, include_expired, include_deleted):
    """List memory records."""
    project_path = Path(project).resolve()
    store = MemoryStore(project_path)

    status_enum = MemoryStatus(status) if status else None
    type_enum = MemoryType(memory_type) if memory_type else None

    records = store.list_records(
        project_id=project_id,
        status=status_enum,
        memory_type=type_enum,
        limit=limit,
        include_expired=include_expired,
        include_deleted=include_deleted,
    )

    if fmt == "json":
        click.echo(json.dumps([r.to_dict() for r in records], indent=2))
    elif fmt == "yaml":
        click.echo(yaml.safe_dump([r.to_dict() for r in records], sort_keys=False))
    else:
        if not records:
            click.echo("No memory records found.")
            return

        click.echo(f"{'ID':<22} {'Type':<10} {'Status':<12} {'Project':<15} {'Confidence':<10} {'Content'}")
        click.echo("-" * 120)
        for r in records:
            content_preview = r.content[:60] + "..." if len(r.content) > 60 else r.content
            click.echo(f"{r.memory_id:<22} {r.memory_type.value:<10} {r.status.value:<12} {r.project_id:<15} {r.provenance.confidence:.2f}      {content_preview}")


@memory_group.command("show")
@click.argument("memory_id")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--format", "fmt", type=click.Choice(["json", "yaml", "text"]), default="text", help="Output format")
def memory_show(memory_id, project, fmt):
    """Show a specific memory record by ID."""
    project_path = Path(project).resolve()
    store = MemoryStore(project_path)

    record = store.load_record(memory_id)
    if not record:
        click.echo(f"Memory record '{memory_id}' not found.", err=True)
        raise click.Abort()

    if fmt == "json":
        click.echo(json.dumps(record.to_dict(), indent=2))
    elif fmt == "yaml":
        click.echo(yaml.safe_dump(record.to_dict(), sort_keys=False))
    else:
        click.echo(f"Memory ID:     {record.memory_id}")
        click.echo(f"Type:          {record.memory_type.value}")
        click.echo(f"Status:        {record.status.value}")
        click.echo(f"Project:       {record.project_id}")
        click.echo(f"Content:       {record.content}")
        click.echo(f"Normalized:    {record.normalized_content}")
        click.echo(f"Confidence:    {record.provenance.confidence:.2f}")
        click.echo(f"Created:       {record.created_at}")
        click.echo(f"Updated:       {record.updated_at}")
        if record.last_validated_at:
            click.echo(f"Validated:     {record.last_validated_at}")
        if record.expires_at:
            click.echo(f"Expires:       {record.expires_at}")
        if record.superseded_by:
            click.echo(f"Superseded by: {record.superseded_by}")
        if record.supersedes:
            click.echo(f"Supersedes:    {', '.join(record.supersedes)}")
        if record.tags:
            click.echo(f"Tags:          {', '.join(record.tags)}")
        click.echo(f"Provenance:")
        click.echo(f"  Extracted at:  {record.provenance.extracted_at}")
        click.echo(f"  Extractor:     {record.provenance.extractor_agent}")
        click.echo(f"  Method:        {record.provenance.extraction_method}")
        click.echo(f"  Source EXPs:   {', '.join(record.provenance.source_experience_ids)}")
        if record.evidence:
            click.echo(f"Evidence:")
            for ev in record.evidence:
                click.echo(f"  [{ev.evidence_type}] {ev.excerpt} (exp: {ev.experience_id})")


@memory_group.command("stats")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
def memory_stats(project):
    """Show memory store statistics."""
    project_path = Path(project).resolve()
    store = MemoryStore(project_path)

    stats = store.stats()
    click.echo("Memory Store Statistics:")
    click.echo(f"  Total:        {stats['total']}")
    click.echo(f"  Active:       {stats['active']}")
    click.echo(f"  Candidate:    {stats['candidate']}")
    click.echo(f"  Superseded:   {stats['superseded']}")
    click.echo(f"  Expired:      {stats['expired']}")
    click.echo(f"  Deleted:      {stats['deleted']}")
    click.echo("  By Type:")
    for t, count in stats["by_type"].items():
        click.echo(f"    {t}: {count}")


@memory_group.command("search")
@click.argument("query")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--project-id", help="Filter by project ID")
@click.option("--status", type=click.Choice([s.value for s in MemoryStatus]), default="active", help="Filter by status")
@click.option("--limit", type=int, default=10, help="Maximum results")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table", help="Output format")
@click.option("--include-expired/--no-expired", default=False, help="Include EXPIRED memories")
@click.option("--include-deleted/--no-deleted", default=False, help="Include DELETED memories")
def memory_search(query, project, project_id, status, limit, fmt, include_expired, include_deleted):
    """Full-text search over memory records."""
    project_path = Path(project).resolve()
    store = MemoryStore(project_path)

    status_enum = MemoryStatus(status) if status else None

    results = store.search(
        query=query,
        project_id=project_id,
        status=status_enum,
        limit=limit,
        include_expired=include_expired,
        include_deleted=include_deleted,
    )

    if fmt == "json":
        click.echo(json.dumps([r.to_dict() for r in results], indent=2))
    else:
        if not results:
            click.echo(f"No memories found for query: '{query}'")
            return

        click.echo(f"{'ID':<22} {'Type':<10} {'Score':<8} {'Confidence':<10} {'Project':<15} {'Content'}")
        click.echo("-" * 120)
        for r in results:
            content_preview = r.content[:60] + "..." if len(r.content) > 60 else r.content
            click.echo(f"{r.memory_id:<22} {r.memory_type.value:<10} {r.rank:.3f}   {r.provenance_confidence:.2f}      {r.project_id:<15} {content_preview}")


@memory_group.command("compile")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--experience-id", help="Compile from specific experience ID (default: all learning-eligible)")
@click.option("--save/--no-save", default=True, help="Save candidate memories to store")
@click.option("--promote/--no-promote", default=False, help="Auto-promote high-confidence candidates")
def memory_compile(project, experience_id, save, promote):
    """Compile candidate memories from verified experience records."""
    project_path = Path(project).resolve()

    # Ensure schemas are available
    schemas_src = Path(__file__).parent.parent / "schemas"
    if schemas_src.exists():
        shutil.copytree(schemas_src, project_path / "schemas", dirs_exist_ok=True)

    compiler = MemoryCompiler(project_path)
    exp_store = ExperienceStore(project_path)

    if experience_id:
        experiences = [exp_store.load_record(experience_id)]
        if not experiences[0]:
            click.echo(f"Experience '{experience_id}' not found.", err=True)
            raise click.Abort()
    else:
        experiences = exp_store.list_records(only_learning_eligible=True)

    if not experiences:
        click.echo("No learning-eligible experiences found.")
        return

    total_candidates = 0
    total_promoted = 0

    for exp in experiences:
        click.echo(f"Compiling from {exp.experience_id} ({exp.task_signature})...")
        candidates = compiler.compile_memories_from_experience(exp)

        for candidate in candidates:
            total_candidates += 1
            if save:
                compiler.memory_store.save_record(candidate)
                click.echo(f"  Created: {candidate.memory_id} [{candidate.memory_type.value}] - {candidate.content[:80]}")
            else:
                click.echo(f"  Candidate: {candidate.memory_id} [{candidate.memory_type.value}] - {candidate.content[:80]}")

            if promote and candidate.provenance.confidence >= 0.75:
                promoted = compiler.promote_candidate(candidate.memory_id)
                if promoted:
                    total_promoted += 1
                    click.echo(f"  -> Promoted to ACTIVE")

    click.echo(f"\nTotal candidates: {total_candidates}")
    if promote:
        click.echo(f"Auto-promoted: {total_promoted}")


@memory_group.command("promote")
@click.argument("memory_id")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--reason", default="Validated by user", help="Promotion reason")
@click.option("--actor", default="user", help="Actor performing promotion")
@click.option("--dry-run/--no-dry-run", default=False, help="Preview promotion without applying")
def memory_promote(memory_id, project, reason, actor, dry_run):
    """Promote a candidate memory to ACTIVE status via governance gate."""
    project_path = Path(project).resolve()
    service = MemoryPromotionService(project_path)

    if dry_run:
        result = service.preview_promotion(memory_id)
        click.echo(f"Promotion Preview for {memory_id}:")
        click.echo(f"  Decision: {result.decision.value}")
        click.echo(f"  Reason: {result.reason_code}")
        click.echo(f"  Message: {result.message}")
        if result.requires_approval:
            click.echo(f"  Requires Approval: Yes ({result.approval_type})")
        if result.metadata:
            click.echo(f"  Metadata: {result.metadata}")
        if result.decision == PromotionDecision.ELIGIBLE:
            click.echo("  -> Would promote to ACTIVE")
        return

    promoted, result = service.promote(
        memory_id,
        reason=reason,
        actor=actor,
        is_human=False,
    )
    if promoted:
        click.echo(f"Promoted {memory_id} to ACTIVE")
        click.echo(f"  Decision: {result.decision.value}")
        click.echo(f"  Reason: {result.reason_code}")
    else:
        click.echo(f"Promotion failed: {result.message} ({result.reason_code})", err=True)
        if result.requires_approval:
            click.echo(f"  This promotion requires {result.approval_type} approval", err=True)
        raise click.Abort()


@memory_group.command("supersede")
@click.argument("old_memory_id")
@click.argument("new_memory_id")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
def memory_supersede(old_memory_id, new_memory_id, project):
    """Supersede an existing memory with a new version."""
    project_path = Path(project).resolve()
    compiler = MemoryCompiler(project_path)

    old = compiler.memory_store.load_record(old_memory_id)
    new = compiler.memory_store.load_record(new_memory_id)

    if not old:
        click.echo(f"Old memory '{old_memory_id}' not found.", err=True)
        raise click.Abort()
    if not new:
        click.echo(f"New memory '{new_memory_id}' not found.", err=True)
        raise click.Abort()

    result = compiler.supersede_memory(old_memory_id, new)
    if result:
        click.echo(f"Superseded {old_memory_id} with {new_memory_id}")
    else:
        click.echo("Supersession failed.", err=True)
        raise click.Abort()


@memory_group.command("delete")
@click.argument("memory_id")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--hard", is_flag=True, help="Permanently delete (not just mark as DELETED)")
@click.option("--confirm", is_flag=True, help="Confirm deletion without prompt")
def memory_delete(memory_id, project, hard, confirm):
    """Delete a memory record (soft delete by default)."""
    project_path = Path(project).resolve()
    compiler = MemoryCompiler(project_path)

    record = compiler.memory_store.load_record(memory_id)
    if not record:
        click.echo(f"Memory '{memory_id}' not found.", err=True)
        raise click.Abort()

    if not confirm:
        action = "permanently delete" if hard else "mark as DELETED"
        if not click.confirm(f"Really {action} '{memory_id}'?"):
            click.echo("Aborted.")
            return

    if compiler.delete_memory(memory_id, hard=hard):
        action = "permanently deleted" if hard else "marked as DELETED"
        click.echo(f"Memory '{memory_id}' {action}")
    else:
        click.echo("Deletion failed.", err=True)
        raise click.Abort()


@memory_group.command("rebuild-index")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--clean", is_flag=True, help="Full rebuild (delete existing index first)")
def memory_rebuild_index(project, clean):
    """Rebuild the memory search index from raw records."""
    project_path = Path(project).resolve()
    store = MemoryStore(project_path)

    click.echo("Rebuilding memory index...")
    count = store.build_index(clean=clean)
    click.echo(f"Indexed {count} memory records.")


# ===== Contradiction Detection Commands =====

@memory_group.command("contradictions")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--project-id", help="Filter by project ID")
@click.option("--status", type=click.Choice([s.value for s in ContradictionStatus]), help="Filter by resolution status")
@click.option("--category", type=click.Choice([c.value for c in ContradictionCategory]), help="Filter by contradiction category")
@click.option("--memory-id", help="Filter by memory ID (either side)")
@click.option("--severity", type=click.Choice([s.value for s in ContradictionSeverity]), help="Filter by severity")
@click.option("--limit", type=int, default=20, help="Maximum results")
@click.option("--format", "fmt", type=click.Choice(["table", "json", "yaml"]), default="table", help="Output format")
def memory_contradictions(project, project_id, status, category, memory_id, severity, limit, fmt):
    """List contradiction findings."""
    project_path = Path(project).resolve()
    detector = ContradictionDetector(project_path)

    status_enum = ContradictionStatus(status) if status else None
    category_enum = ContradictionCategory(category) if category else None
    severity_enum = ContradictionSeverity(severity) if severity else None

    findings = detector.findings_store.list_findings(
        project_id=project_id,
        status=status_enum,
        category=category_enum,
        memory_id=memory_id,
        limit=limit,
    )

    # Filter by severity if specified
    if severity_enum:
        findings = [f for f in findings if f.severity == severity_enum]

    if fmt == "json":
        click.echo(json.dumps([f.to_dict() for f in findings], indent=2))
    elif fmt == "yaml":
        click.echo(yaml.safe_dump([f.to_dict() for f in findings], sort_keys=False))
    else:
        if not findings:
            click.echo("No contradiction findings found.")
            return

        click.echo(f"{'Finding ID':<22} {'Category':<30} {'Severity':<10} {'Status':<20} {'Memory A':<22} {'Memory B':<22}")
        click.echo("-" * 140)
        for f in findings:
            mem_a = f.memory_id_a[:20] + ".." if len(f.memory_id_a) > 22 else f.memory_id_a
            mem_b = f.memory_id_b[:20] + ".." if len(f.memory_id_b) > 22 else f.memory_id_b
            cat = f.category.value[:28] + ".." if len(f.category.value) > 30 else f.category.value
            click.echo(f"{f.finding_id:<22} {cat:<30} {f.severity.value:<10} {f.status.value:<20} {mem_a:<22} {mem_b:<22}")


@memory_group.command("contradiction-show")
@click.argument("finding_id")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--format", "fmt", type=click.Choice(["json", "yaml", "text"]), default="text", help="Output format")
def memory_contradiction_show(finding_id, project, fmt):
    """Show a specific contradiction finding by ID."""
    project_path = Path(project).resolve()
    detector = ContradictionDetector(project_path)

    finding = detector.findings_store.load_finding(finding_id)
    if not finding:
        click.echo(f"Contradiction finding '{finding_id}' not found.", err=True)
        raise click.Abort()

    if fmt == "json":
        click.echo(json.dumps(finding.to_dict(), indent=2))
    elif fmt == "yaml":
        click.echo(yaml.safe_dump(finding.to_dict(), sort_keys=False))
    else:
        click.echo(f"Finding ID:        {finding.finding_id}")
        click.echo(f"Category:          {finding.category.value}")
        click.echo(f"Severity:          {finding.severity.value}")
        click.echo(f"Status:            {finding.status.value}")
        click.echo(f"Memory A:          {finding.memory_id_a}")
        click.echo(f"Memory B:          {finding.memory_id_b}")
        click.echo(f"Project:           {finding.project_id}")
        click.echo(f"Detection Method:  {finding.detection_method}")
        if finding.confidence is not None:
            click.echo(f"Confidence:        {finding.confidence:.2f}")
        click.echo(f"Explanation:       {finding.explanation}")
        click.echo(f"Evidence A:        {', '.join(finding.evidence_refs_a) if finding.evidence_refs_a else 'none'}")
        click.echo(f"Evidence B:        {', '.join(finding.evidence_refs_b) if finding.evidence_refs_b else 'none'}")
        click.echo(f"Detected At:       {finding.detected_at}")
        if finding.resolved_at:
            click.echo(f"Resolved At:       {finding.resolved_at}")
            click.echo(f"Resolution:        {finding.resolution_action}")
            click.echo(f"Rationale:         {finding.resolution_rationale}")


@memory_group.command("contradiction-resolve")
@click.argument("finding_id")
@click.argument("resolution", type=click.Choice([
    "not_contradiction",
    "superseded",
    "both_valid",
    "rejected",
]))
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--rationale", required=True, help="Reason for resolution")
@click.option("--actor", default="user", help="Actor performing resolution")
def memory_contradiction_resolve(finding_id, resolution, project, rationale, actor):
    """Resolve a contradiction finding."""
    project_path = Path(project).resolve()
    service = ContradictionResolutionService(project_path)

    resolution_map = {
        "not_contradiction": ContradictionStatus.RESOLVED_NOT_CONTRADICTION,
        "superseded": ContradictionStatus.RESOLVED_SUPERSEDED,
        "both_valid": ContradictionStatus.RESOLVED_BOTH_VALID,
        "rejected": ContradictionStatus.RESOLVED_REJECTED,
    }
    resolution_enum = resolution_map[resolution]

    try:
        finding = service.resolve_finding(
            finding_id,
            resolution_enum,
            rationale,
            actor,
        )
        if finding:
            click.echo(f"Resolved finding {finding_id} as {resolution}")
            click.echo(f"  Status: {finding.status.value}")
            click.echo(f"  Rationale: {finding.resolution_rationale}")
        else:
            click.echo(f"Finding {finding_id} not found.", err=True)
            raise click.Abort()
    except ValueError as e:
        click.echo(f"Resolution failed: {e}", err=True)
        raise click.Abort()


@memory_group.command("contradiction-supersede")
@click.argument("finding_id")
@click.argument("superseding_memory_id")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--rationale", required=True, help="Reason for supersession")
@click.option("--actor", default="user", help="Actor performing supersession")
def memory_contradiction_supersede(finding_id, superseding_memory_id, project, rationale, actor):
    """Resolve a contradiction by superseding one memory with another."""
    project_path = Path(project).resolve()
    service = ContradictionResolutionService(project_path)

    # Load the superseding memory
    store = MemoryStore(project_path)
    superseding = store.load_record(superseding_memory_id)
    if not superseding:
        click.echo(f"Superseding memory '{superseding_memory_id}' not found.", err=True)
        raise click.Abort()

    try:
        superseded, finding = service.supersede_and_resolve(
            finding_id,
            superseding,
            rationale,
            actor,
        )
        if superseded and finding:
            click.echo(f"Superseded and resolved finding {finding_id}")
            click.echo(f"  Superseding memory: {superseding_memory_id}")
            click.echo(f"  Superseded memory status: {superseded.status.value}")
            click.echo(f"  Finding resolution: {finding.status.value}")
        else:
            click.echo("Supersession failed.", err=True)
            raise click.Abort()
    except ValueError as e:
        click.echo(f"Supersession failed: {e}", err=True)
        raise click.Abort()


@memory_group.command("contradiction-detect")
@click.argument("memory_id")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table", help="Output format")
def memory_contradiction_detect(memory_id, project, fmt):
    """Run contradiction detection for a specific memory against existing active memories."""
    project_path = Path(project).resolve()
    store = MemoryStore(project_path)
    detector = ContradictionDetector(project_path)

    record = store.load_record(memory_id)
    if not record:
        click.echo(f"Memory '{memory_id}' not found.", err=True)
        raise click.Abort()

    findings = detector.detect_contradictions(record)

    if fmt == "json":
        click.echo(json.dumps([f.to_dict() for f in findings], indent=2))
    else:
        if not findings:
            click.echo(f"No contradictions detected for {memory_id}")
            return

        click.echo(f"Found {len(findings)} contradiction(s) for {memory_id}:")
        click.echo()
        for f in findings:
            click.echo(f"  Finding ID: {f.finding_id}")
            click.echo(f"  Category:   {f.category.value}")
            click.echo(f"  Severity:   {f.severity.value}")
            click.echo(f"  Status:     {f.status.value}")
            click.echo(f"  Other Mem:  {f.memory_id_b if f.memory_id_a == memory_id else f.memory_id_a}")
            click.echo(f"  Explanation: {f.explanation}")
            click.echo()


# ===== Memory Decay Commands (Stage 7) =====

@memory_group.command("decay-config")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--function", type=click.Choice([f.value for f in DecayFunction]), help="Decay function")
@click.option("--half-life", type=float, help="Base half-life in days")
@click.option("--decay-floor", type=float, help="Minimum decay multiplier (0.0-1.0)")
@click.option("--enabled/--disabled", default=None, help="Enable/disable decay")
@click.option("--show/--no-show", default=True, help="Show current config")
def memory_decay_config(project, function, half_life, decay_floor, enabled, show):
    """Configure or display memory decay policy."""
    project_path = Path(project).resolve()
    policy = get_decay_policy(project_path=project_path)

    if function is not None or half_life is not None or decay_floor is not None or enabled is not None:
        # Build updated policy
        half_life_by_type = dict(policy.half_life_by_type)
        profile_multipliers = dict(policy.profile_multipliers)

        new_policy = DecayPolicy(
            function=DecayFunction(function) if function else policy.function,
            half_life_days=half_life if half_life is not None else policy.half_life_days,
            decay_floor=decay_floor if decay_floor is not None else policy.decay_floor,
            enabled=enabled if enabled is not None else policy.enabled,
            half_life_by_type=half_life_by_type,
            profile_multipliers=profile_multipliers,
        )

        # Save to config file
        config_dir = project_path / ".sync" / "memory" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "decay.json"
        with config_file.open("w", encoding="utf-8") as f:
            json.dump(new_policy.to_dict(), f, indent=2)
        click.echo(f"Decay policy saved to {config_file}")
        policy = new_policy

    if show:
        click.echo("Current Decay Policy:")
        click.echo(f"  Function:       {policy.function.value}")
        click.echo(f"  Half-Life:      {policy.half_life_days} days")
        click.echo(f"  Decay Floor:    {policy.decay_floor}")
        click.echo(f"  Enabled:        {policy.enabled}")
        click.echo("  Half-Life by Type:")
        for mt, hl in sorted(policy.half_life_by_type.items(), key=lambda x: x[0].value):
            click.echo(f"    {mt.value}: {hl} days")
        click.echo("  Profile Multipliers:")
        for prof, mult in sorted(policy.profile_multipliers.items(), key=lambda x: x[0].value):
            click.echo(f"    {prof.value}: {mult}x")


@memory_group.command("decay-evaluate")
@click.argument("query")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--profile", type=click.Choice([p.value for p in ExecutionProfile]), default="medium", help="Execution profile")
@click.option("--limit", type=int, default=10, help="Max memories to consider")
@click.option("--token-budget", type=int, default=500, help="Token budget for retrieval")
@click.option("--shadow/--no-shadow", default=False, help="Run in shadow mode (compare without applying)")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table", help="Output format")
def memory_decay_evaluate(query, project, profile, limit, token_budget, shadow, fmt):
    """Evaluate decay impact on memory retrieval for a query (shadow mode by default)."""
    project_path = Path(project).resolve()
    from validators.memory.retriever import MemoryRetriever
    from validators.kernel.profile import ExecutionProfile

    profile_enum = ExecutionProfile(profile)
    retriever = MemoryRetriever(project_path)

    # Run retrieval with diagnostics
    results, diag = retriever.retrieve_memories_with_diagnostics(
        query,
        profile=profile_enum,
        limit=limit,
        token_budget=token_budget,
    )

    # Get decay diagnostics
    decay_diag = diag.decay_diagnostics
    if decay_diag is None:
        click.echo("No decay diagnostics available (decay may be disabled or no memories matched).")
        return

    if fmt == "json":
        click.echo(json.dumps(decay_diag.to_dict(), indent=2))
        return

    click.echo(f"Decay Evaluation for query: '{query}'")
    click.echo(f"Profile: {profile} | Shadow Mode: {decay_diag.shadow_mode}")
    click.echo()
    click.echo(f"Considered:  {decay_diag.considered_count}")
    click.echo(f"Decay Applied: {decay_diag.decay_applied_count}")
    click.echo(f"Decay Skipped: {decay_diag.decay_skipped_count}")
    click.echo()

    if decay_diag.applications:
        click.echo(f"{'Memory ID':<22} {'Type':<10} {'Age (d)':<8} {'Base':<8} {'Factor':<8} {'Decayed':<8} {'Half-Life':<10}")
        click.echo("-" * 100)
        for app in decay_diag.applications:
            click.echo(f"{app.memory_id:<22} {app.memory_type.value:<10} {app.age_days:<8.1f} {app.base_relevance:<8.3f} {app.decay_factor:<8.3f} {app.decayed_relevance:<8.3f} {app.half_life_used:<10.1f}")

    if decay_diag.shadow_comparison:
        sc = decay_diag.shadow_comparison
        click.echo()
        click.echo("Shadow Comparison:")
        click.echo(f"  Max Impact:  {sc['max_impact']:.4f}")
        click.echo(f"  Avg Impact:  {sc['avg_impact']:.4f}")
        click.echo(f"  Original:    {[f'{s:.3f}' for s in sc['original_scores']]}")
        click.echo(f"  Decayed:     {[f'{s:.3f}' for s in sc['decayed_scores']]}")


@memory_group.command("decay-audit")
@click.option("--project", "-p", type=click.Path(exists=True, file_okay=False), default=".", help="Project path")
@click.option("--days", type=int, default=7, help="Number of days of audit logs to show")
@click.option("--limit", type=int, default=50, help="Maximum entries to show")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table", help="Output format")
def memory_decay_audit(project, days, limit, fmt):
    """Show decay audit log entries."""
    project_path = Path(project).resolve()
    audit_dir = project_path / ".sync" / "memory" / "audit"

    if not audit_dir.exists():
        click.echo("No decay audit directory found.")
        return

    from datetime import datetime, timezone, timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    entries = []
    for audit_file in sorted(audit_dir.glob("decay_*.jsonl"), reverse=True):
        try:
            file_date_str = audit_file.stem.replace("decay_", "")
            file_date = datetime.strptime(file_date_str, "%Y%m%d").replace(tzinfo=timezone.utc)
            if file_date < cutoff:
                continue
        except Exception:
            continue

        with audit_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entry_time = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                    if entry_time >= cutoff:
                        entries.append(entry)
                except Exception:
                    continue
                if len(entries) >= limit:
                    break
        if len(entries) >= limit:
            break

    entries.sort(key=lambda e: e["timestamp"], reverse=True)

    if fmt == "json":
        click.echo(json.dumps(entries, indent=2))
        return

    if not entries:
        click.echo(f"No decay audit entries in last {days} days.")
        return

    click.echo(f"Decay Audit Log (last {days} days, showing {len(entries)} entries):")
    click.echo()
    for entry in entries:
        diag = entry.get("diagnostics", {})
        click.echo(f"  {entry['timestamp']} | Query: {entry.get('query', '(none)')[:60]}")
        click.echo(f"    Profile: {diag.get('profile', '?')} | Considered: {diag.get('considered_count', 0)} | Applied: {diag.get('decay_applied_count', 0)} | Skipped: {diag.get('decay_skipped_count', 0)} | Shadow: {diag.get('shadow_mode', False)}")
        if diag.get("shadow_comparison"):
            sc = diag["shadow_comparison"]
            click.echo(f"    Shadow: max_impact={sc['max_impact']:.4f} avg_impact={sc['avg_impact']:.4f}")
        click.echo()


# Export for main CLI
__all__ = ["memory_group"]