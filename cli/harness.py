"""CLI surface for the governed Harness Runtime."""

from pathlib import Path

import click

from validators.harness import AgentRunner, EchoLLMProvider


@click.group()
def harness():
    """Run the governed worker execution loop."""
    pass


@harness.command('run-once')
@click.argument('agent')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option(
    '--release-target',
    default=None,
    help='Release target used when completing a work-order task',
)
def run_once(agent: str, project_path: str, release_target: str | None):
    """Process one inbox item or assigned work order."""
    from rich.console import Console

    console = Console()
    runner = AgentRunner(
        Path(project_path).resolve(),
        agent,
        llm_provider=EchoLLMProvider(default_release_target=release_target),
    )
    result = runner.run_once()

    console.print(f'status: {result.status}')
    console.print(f'persisted: {result.persisted}')
    if result.task_id:
        console.print(f'task_id: {result.task_id}')
    if result.report_path:
        console.print(f'report_path: {result.report_path}')
    if result.reason:
        console.print(f'reason: {result.reason}')
    if result.meta:
        console.print(f"benchmark_mode: {result.meta.get('benchmark_mode')}")
        console.print(f"retrieval_cap_exhausted: {result.meta.get('retrieval_cap_exhausted')}")

    if result.status in {'blocked', 'deferred'} and not result.persisted:
        raise SystemExit(1)


__all__ = ['harness']
