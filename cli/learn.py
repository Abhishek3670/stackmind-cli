"""CLI commands for Pattern Mining and Skill Distillation.

Implements Phase 4 (Pattern Mining) CLI tooling.
"""

from __future__ import annotations

from pathlib import Path
import click
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from validators.learning.distiller import SkillDistiller
from validators.learning.miner import PatternMiner
from validators.skill.store import SkillStore


@click.group("learn")
def learn_group():
    """Discover patterns and distill reusable procedural skills."""
    pass


@learn_group.command("clusters")
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
@click.option(
    "--min-samples",
    "-n",
    default=3,
    type=int,
    help="Minimum verified episodes required to form a cluster (default: 3)",
)
@click.option(
    "--min-similarity",
    "-s",
    default=0.5,
    type=float,
    help="Minimum trajectory similarity ratio (default: 0.5)",
)
def clusters_command(project_path: str, min_samples: int, min_similarity: float):
    """List discovered clusters of verified experience episodes."""
    console = Console()
    miner = PatternMiner(project_path)
    clusters = miner.mine_clusters(min_samples=min_samples, min_similarity=min_similarity)

    if not clusters:
        console.print("[dim]No experience clusters found matching criteria.[/dim]")
        return

    table = Table(title=f"Discovered Pattern Clusters ({len(clusters)} shown)")
    table.add_column("Cluster ID", style="bold cyan", no_wrap=True)
    table.add_column("Normalized Intent", style="bold green")
    table.add_column("Episodes", justify="center", style="magenta")
    table.add_column("Similarity", justify="center", style="cyan")
    table.add_column("Eligible (N >= 3)", justify="center")
    table.add_column("Action Summary", style="italic")

    for c in clusters:
        elig_style = "[bold green]YES[/bold green]" if c.is_distillation_eligible else "[dim red]NO (N < 3)[/dim red]"
        actions_str = ", ".join(f"{a['tool']}:{a['command_template'] or 'action'}" for a in c.common_actions[:2])
        if len(c.common_actions) > 2:
            actions_str += f" (+{len(c.common_actions) - 2} more)"

        table.add_row(
            c.cluster_id,
            c.intent_slug,
            str(c.sample_count),
            f"{c.similarity_score:.2f}",
            elig_style,
            actions_str or "-",
        )

    console.print(table)


@learn_group.command("mine")
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
@click.option(
    "--min-samples",
    "-n",
    default=3,
    type=int,
    help="Minimum verified episodes required for distillation (default: 3)",
)
@click.option(
    "--min-similarity",
    "-s",
    default=0.5,
    type=float,
    help="Minimum trajectory similarity ratio (default: 0.5)",
)
@click.option(
    "--save/--no-save",
    default=True,
    help="Automatically persist distilled candidates to skill store",
)
def mine_command(project_path: str, min_samples: int, min_similarity: float, save: bool):
    """Mine verified patterns and distill eligible clusters into Skill Candidates."""
    console = Console()
    miner = PatternMiner(project_path)
    candidates = miner.distill_candidates(
        min_samples=min_samples,
        min_similarity=min_similarity,
        save=save,
    )

    if not candidates:
        console.print("[yellow]Pattern mining complete. No new clusters met the N ≥ 3 distillation threshold.[/yellow]")
        return

    console.print(f"[bold green][SUCCESS][/bold green] Distilled {len(candidates)} candidate skill(s):")
    for cand in candidates:
        console.print(
            f"  • [bold cyan]{cand.name}[/bold cyan] v{cand.version} ({cand.skill_id}): "
            f"{len(cand.steps)} step(s), risk={cand.risk_tier.value.upper()}, confidence={cand.metrics.confidence_score:.2f}"
        )


@learn_group.command("distill")
@click.argument("cluster_id")
@click.option(
    "--name",
    "-n",
    default=None,
    help="Override distilled skill name",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory",
)
def distill_command(cluster_id: str, name: str | None, project_path: str):
    """Distill a specific pattern cluster into a Candidate Skill."""
    console = Console()
    miner = PatternMiner(project_path)
    skill_store = SkillStore(project_path)

    clusters = miner.mine_clusters(min_samples=1)
    target_cluster = next((c for c in clusters if c.cluster_id == cluster_id), None)

    if not target_cluster:
        console.print(f"[bold red]Error:[/bold red] Cluster '{cluster_id}' not found.")
        return

    try:
        candidate = SkillDistiller.distill_candidate_skill(target_cluster, skill_name=name)
        skill_store.save_version(candidate)
        console.print(
            f"[bold green][SUCCESS][/bold green] Distilled cluster '{cluster_id}' into "
            f"candidate skill '{candidate.name}' v1 ({candidate.skill_id})."
        )
    except Exception as exc:
        console.print(f"[bold red]Distillation failed:[/bold red] {exc}")
