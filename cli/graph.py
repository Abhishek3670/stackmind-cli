"""CLI commands for building and inspecting knowledge projections."""

from __future__ import annotations

import json
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import Any

import click

from cli import __version__
from validators.knowledge.api import KnowledgeAPI
from validators.knowledge.compiler import compile_project
from validators.knowledge.compiler.incremental import (
    collect_git_python_changes,
    incremental_update,
)
from validators.knowledge.compiler.ir import COMPILER_VERSION, IR_SCHEMA_VERSION
from validators.knowledge.compiler.watcher import PollingWatcher
from validators.knowledge.enricher import enqueue_stale_nodes
from validators.knowledge.enricher_queue import read_enrichment_status
from validators.knowledge.projections import build_projections, projection_versions
from validators.knowledge.storage import (
    KNOWLEDGE_SCHEMA_VERSION,
    latest_revision_id,
    read_ir,
    revision_path,
)
from validators.knowledge.writer import write_knowledge


@click.group()
def graph():
    """Build and inspect derived knowledge artifacts."""
    pass


@graph.command('build')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option(
    '--agent',
    default='codex',
    show_default=True,
    help='Agent name recorded for registry and knowledge writes',
)
def build(project_path: str, agent: str):
    """Compile source to T1 and rebuild all T2 projections."""
    from rich.console import Console

    console = Console()
    project = Path(project_path).resolve()
    external = not (project / ".sync" / "runtime").exists()

    ir = compile_project(project, agent=agent)
    write_result = write_knowledge(project, ir, agent=agent)
    projection_results = build_projections(project)
    if not external:
        enqueue_stale_nodes(project, agent=agent)
    stats = _graph_stats(project)

    console.print('[bold green][PASS] Knowledge store built[/bold green]')
    console.print(f'revision: {write_result.revision_id}')
    console.print(f'nodes: {stats["nodes"]}')
    console.print(f'edges: {stats["edges"]}')
    console.print(f'revisions: {stats["revisions"]}')
    console.print(
        'projections: '
        + ', '.join(
            f'{result.name}={len(result.written_paths)}'
            for result in projection_results
        )
    )


@graph.command('update')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option(
    '--agent',
    default='codex',
    show_default=True,
    help='Agent name recorded for registry and knowledge writes',
)
@click.option('--path', 'paths', multiple=True, help='Explicit changed Python paths')
@click.option(
    '--deleted-path',
    'deleted_paths',
    multiple=True,
    help='Explicit deleted Python paths',
)
def update(project_path: str, agent: str, paths: tuple[str, ...], deleted_paths: tuple[str, ...]):
    """Incrementally update the knowledge store from changed files."""
    from rich.console import Console

    console = Console()
    project = Path(project_path).resolve()
    changed = paths
    deleted = deleted_paths
    if not changed and not deleted:
        changed, deleted = collect_git_python_changes(project)

    result = incremental_update(
        project,
        agent=agent,
        changed_paths=changed or None,
        deleted_paths=deleted or None,
    )
    if not result.changed:
        console.print('[dim]No changes detected.[/dim]')
        return

    enqueue_stale_nodes(project, agent=agent)
    console.print('[bold green][PASS] Incremental update complete[/bold green]')
    console.print(f'revision: {result.revision_id}')
    console.print(f'dirty_paths: {len(result.dirty_paths)}')
    console.print(f'affected_paths: {len(result.affected_paths)}')
    console.print(f'written_paths: {len(result.written_paths)}')


@graph.command('watch')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option(
    '--agent',
    default='codex',
    show_default=True,
    help='Agent name recorded for registry and knowledge writes',
)
@click.option('--poll-interval', default=0.25, show_default=True, type=float)
@click.option('--debounce-seconds', default=0.5, show_default=True, type=float)
@click.option(
    '--max-batches',
    default=None,
    type=int,
    help='Optional batch limit for the watcher loop',
)
def watch(
    project_path: str,
    agent: str,
    poll_interval: float,
    debounce_seconds: float,
    max_batches: int | None,
):
    """Watch Python files and run incremental graph updates."""
    from rich.console import Console

    console = Console()
    project = Path(project_path).resolve()
    watcher = PollingWatcher(
        project,
        agent=agent,
        poll_interval=poll_interval,
        debounce_seconds=debounce_seconds,
    )
    console.print('[dim]Watching for Python changes...[/dim]')
    try:
        watcher.watch(max_batches=max_batches)
    except KeyboardInterrupt:
        console.print('[dim]Watcher stopped.[/dim]')


@graph.command('query')
@click.argument('query_text', required=False)
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--kind', help='Filter by symbol kind')
@click.option('--path', 'path_contains', help='Filter by repo-relative path substring')
@click.option(
    '--qualified-name',
    'qualified_name_contains',
    help='Filter by qualified-name substring',
)
@click.option('--limit', default=10, show_default=True, type=int)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def query_command(
    query_text: str | None,
    project_path: str,
    kind: str | None,
    path_contains: str | None,
    qualified_name_contains: str | None,
    limit: int,
    json_output: bool,
):
    """Lookup, filter, or search knowledge nodes."""
    from rich.console import Console

    console = Console()
    api = KnowledgeAPI(Path(project_path).resolve())
    if kind or path_contains or qualified_name_contains:
        envelope = api.filter(
            query=query_text or '',
            kind=kind,
            path_contains=path_contains,
            qualified_name_contains=qualified_name_contains,
            limit=limit,
        )
    elif query_text:
        envelope = api.lookup(query_text, limit=limit)
        if not envelope.results:
            envelope = api.search(query_text, limit=limit)
    else:
        raise click.UsageError('Provide a query string or at least one filter option.')

    _emit_envelope(console, envelope, json_output=json_output)


@graph.command('callers')
@click.argument('target')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--limit', default=25, show_default=True, type=int)
@click.option('--evidence-type', multiple=True, help='Filter evidence: runtime or static')
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def callers_command(
    target: str,
    project_path: str,
    limit: int,
    evidence_type: tuple[str, ...],
    json_output: bool,
):
    """Show direct callers of a symbol."""
    from rich.console import Console

    console = Console()
    api = KnowledgeAPI(Path(project_path).resolve())
    envelope = api.callers(target, limit=limit, evidence_type=evidence_type or None)
    _emit_envelope(console, envelope, json_output=json_output)


@graph.command('flows')
@click.argument('source')
@click.argument('sink', required=False)
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--limit', default=25, show_default=True, type=int)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def flows_command(
    source: str,
    sink: str | None,
    project_path: str,
    limit: int,
    json_output: bool,
):
    """Show observed FLOWS_TO paths from SOURCE to optional SINK."""
    from rich.console import Console

    console = Console()
    api = KnowledgeAPI(Path(project_path).resolve())
    envelope = api.flows(source, sink, limit=limit)
    _emit_envelope(console, envelope, json_output=json_output)


@graph.command('impact')
@click.argument('target')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--depth', default=3, show_default=True, type=int)
@click.option('--limit', default=50, show_default=True, type=int)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def impact_command(
    target: str,
    project_path: str,
    depth: int,
    limit: int,
    json_output: bool,
):
    """Show transitive inbound callers impacted by a symbol change."""
    from rich.console import Console

    console = Console()
    api = KnowledgeAPI(Path(project_path).resolve())
    envelope = api.impact(target, depth=depth, limit=limit)
    _emit_envelope(console, envelope, json_output=json_output)


@graph.command('explain')
@click.argument('target')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def explain_command(target: str, project_path: str, json_output: bool):
    """Explain one symbol with callers and callees."""
    from rich.console import Console

    console = Console()
    api = KnowledgeAPI(Path(project_path).resolve())
    payload = api.explain(target)
    if json_output:
        console.print_json(json.dumps(payload))
        return
    node = payload.get('node')
    if not node:
        console.print('[dim]No matching symbol found.[/dim]')
        return
    console.print(f"revision: {payload['revision']}")
    console.print(f"git_commit: {payload['git_commit']}")
    console.print(f"stale: {payload['stale']}")
    console.print(
        f"node: {node['kind']} {node['qualified_name']} ({node['path']})"
    )
    if node.get('signature'):
        console.print(f"signature: {node['signature']}")
    if node.get('summary'):
        console.print(f"summary: {node['summary']}")
    console.print(f"callers: {len(payload.get('inbound', []))}")
    console.print(f"callees: {len(payload.get('outbound', []))}")


@graph.command('context')
@click.argument('query_text')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--token-budget', default=1200, show_default=True, type=int)
@click.option('--limit', default=8, show_default=True, type=int)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def context_command(
    query_text: str,
    project_path: str,
    token_budget: int,
    limit: int,
    json_output: bool,
):
    """Assemble a bounded agent context bundle."""
    from rich.console import Console

    console = Console()
    api = KnowledgeAPI(Path(project_path).resolve())
    bundle = api.assemble_context(
        query_text,
        token_budget=token_budget,
        limit=limit,
    )
    if json_output:
        console.print_json(json.dumps(bundle.to_dict()))
        return
    console.print(f'revision: {bundle.revision}')
    console.print(f'git_commit: {bundle.git_commit}')
    console.print(f'stale: {bundle.stale}')
    console.print(f'semantic: {bundle.semantic}')
    console.print(f'token_budget: {bundle.token_budget}')
    console.print(f'estimated_tokens: {bundle.estimated_tokens}')
    console.print(f'truncated: {bundle.truncated}')
    if bundle.truncation_reason:
        console.print(f'truncation_reason: {bundle.truncation_reason}')
    console.print(bundle.text or '[dim]No context available.[/dim]')


@graph.command('models')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def models_command(project_path: str, json_output: bool):
    """List compiled Pydantic models."""
    from rich.console import Console

    console = Console()
    models = _compiled_models(Path(project_path).resolve())
    if json_output:
        console.print_json(json.dumps({'models': models}))
        return
    console.print(f'models: {len(models)}')
    for model in models:
        bases = ', '.join(model['inherits']) if model['inherits'] else '-'
        console.print(f"- {model['qualified_name']} ({model['path']}) bases={bases}")


@graph.command('model')
@click.argument('name')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def model_command(name: str, project_path: str, json_output: bool):
    """Show compiled model detail."""
    from rich.console import Console

    console = Console()
    detail = _compiled_model_detail(Path(project_path).resolve(), name)
    if detail is None:
        console.print('[dim]No matching compiled model found.[/dim]')
        return
    if json_output:
        console.print_json(json.dumps(detail))
        return

    console.print(f"model: {detail['qualified_name']}")
    console.print(f"path: {detail['path']}")
    console.print(f"inherits: {', '.join(detail['inherits']) if detail['inherits'] else '-'}")
    console.print(f"fields: {len(detail['fields'])}")
    for field in detail['fields']:
        console.print(f"- {field['name']}: {field['signature']}")
    if detail.get('columns'):
        console.print(f"columns: {len(detail['columns'])}")
        for column in detail['columns']:
            console.print(f"- {column['name']}: {column['signature']}")
    if detail.get('relationships'):
        console.print(f"relationships: {len(detail['relationships'])}")
        for relationship in detail['relationships']:
            console.print(f"- {relationship['name']}: target={relationship['target'] or '-'} signature={relationship['signature']}")
    console.print(f"validators: {len(detail['validators'])}")
    for validator in detail['validators']:
        targets = ', '.join(validator['fields']) if validator['fields'] else '-'
        console.print(f"- {validator['name']}: fields={targets} signature={validator['signature']}")
    if detail['config'] is not None:
        console.print(f"config: {detail['config']['signature']}")


@graph.command('relations')
@click.argument('name')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def relations_command(name: str, project_path: str, json_output: bool):
    """Show compiled SQLAlchemy model relationships."""
    from rich.console import Console

    console = Console()
    detail = _compiled_relations(Path(project_path).resolve(), name)
    if detail is None:
        console.print('[dim]No matching compiled SQLAlchemy model found.[/dim]')
        return
    if json_output:
        console.print_json(json.dumps(detail))
        return
    console.print(f"model: {detail['qualified_name']}")
    console.print(f"relationships: {len(detail['relationships'])}")
    for relationship in detail['relationships']:
        console.print(f"- {relationship['name']} -> {relationship['target'] or '-'}")


@graph.command('schema')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--at', 'at_rev', default=None, help='Reconstruct schema at specific migration revision')
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def schema_command(project_path: str, at_rev: str | None, json_output: bool):
    """Show compiled SQLAlchemy schema graph or reconstruct schema at revision."""
    from rich.console import Console

    console = Console()
    if at_rev is not None:
        migs = _compiled_migrations(Path(project_path).resolve())
        if at_rev != 'base' and not any(mig['revision'] == at_rev for mig in migs):
            raise click.BadParameter(f"Revision '{at_rev}' not found in migration history.")
        reconstructed = _reconstruct_schema_at(migs, at_rev)
        if json_output:
            console.print_json(json.dumps({'revision': at_rev, 'schema': reconstructed}))
            return
        console.print(f"schema at revision {at_rev}:")
        for table, cols in sorted(reconstructed.items()):
            console.print(f"- {table}: {', '.join(cols)}")
        return

    schema = _compiled_schema(Path(project_path).resolve())
    if json_output:
        console.print_json(json.dumps(schema))
        return
    console.print(f"models: {len(schema['models'])}")
    for model in schema['models']:
        console.print(
            f"- {model['qualified_name']} columns={len(model['columns'])} "
            f"relationships={len(model['relationships'])}"
        )
    console.print(f"association_tables: {len(schema['association_tables'])}")
    console.print(f"sessions: {len(schema['sessions'])}")
    console.print(f"repositories: {len(schema['repositories'])}")


@graph.command('routes')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def routes_command(project_path: str, json_output: bool):
    """List compiled FastAPI routes."""
    from rich.console import Console

    console = Console()
    routes = _compiled_routes(Path(project_path).resolve())
    if json_output:
        console.print_json(json.dumps({'routes': routes}))
        return
    console.print(f'routes: {len(routes)}')
    for route in routes:
        console.print(
            f"- {route['method']} {route['path']} -> {route['endpoint']} "
            f"response_model={route['response_model'] or '-'}"
        )


@graph.command('endpoint')
@click.argument('path')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def endpoint_command(path: str, project_path: str, json_output: bool):
    """Show compiled FastAPI endpoint detail."""
    from rich.console import Console

    console = Console()
    detail = _compiled_endpoint_detail(Path(project_path).resolve(), path)
    if detail is None:
        console.print('[dim]No matching compiled endpoint found.[/dim]')
        return
    if json_output:
        console.print_json(json.dumps(detail))
        return
    console.print(f"endpoint: {detail['method']} {detail['path']}")
    console.print(f"handler: {detail['endpoint']}")
    console.print(f"response_model: {detail['response_model'] or '-'}")
    console.print(f"request_models: {', '.join(detail['request_models']) if detail['request_models'] else '-'}")
    console.print(f"status_code: {detail['status_code'] or '-'}")
    console.print(f"dependencies: {len(detail['dependencies'])}")
    for dependency in detail['dependencies']:
        console.print(f"- {dependency['target'] or '-'}")
    console.print(f"auth: {', '.join(detail['auth']) if detail['auth'] else '-'}")


@graph.command('auth')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def auth_command(project_path: str, json_output: bool):
    """List compiled FastAPI authentication dependencies."""
    from rich.console import Console

    console = Console()
    auth_items = _compiled_auth(Path(project_path).resolve())
    if json_output:
        console.print_json(json.dumps({'auth': auth_items}))
        return
    console.print(f'auth: {len(auth_items)}')
    for item in auth_items:
        console.print(f"- {item['name']} ({item['path']}): {item['signature']}")


@graph.command('middleware')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def middleware_command(project_path: str, json_output: bool):
    """List compiled FastAPI middleware registrations."""
    from rich.console import Console

    console = Console()
    middleware_items = _compiled_middleware(Path(project_path).resolve())
    if json_output:
        console.print_json(json.dumps({'middleware': middleware_items}))
        return
    console.print(f'middleware: {len(middleware_items)}')
    for item in middleware_items:
        console.print(f"- {item['middleware']} ({item['path']}:{item['line']})")


@graph.command('django-urls')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def django_urls_command(project_path: str, json_output: bool):
    """List compiled Django URL patterns."""
    from rich.console import Console

    console = Console()
    urls = _compiled_django_urls(Path(project_path).resolve())
    if json_output:
        console.print_json(json.dumps({'urls': urls}))
        return
    console.print(f'django_urls: {len(urls)}')
    for item in urls:
        console.print(f"- {item['route']} -> {item['target'] or '-'} name={item['name'] or '-'}")


@graph.command('django-signals')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def django_signals_command(project_path: str, json_output: bool):
    """List compiled Django signal wiring."""
    from rich.console import Console

    console = Console()
    signals = _compiled_django_signals(Path(project_path).resolve())
    if json_output:
        console.print_json(json.dumps({'signals': signals}))
        return
    console.print(f'django_signals: {len(signals)}')
    for item in signals:
        console.print(f"- {item['signal']} sender={item['sender'] or '-'} receiver={item['receiver'] or '-'}")


@graph.command('stats')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
def stats(project_path: str):
    """Report graph and enrichment queue counts."""
    from rich.console import Console

    console = Console()
    values = _graph_stats(Path(project_path))
    console.print(f'nodes: {values["nodes"]}')
    console.print(f'edges: {values["edges"]}')
    console.print(f'resolved_ratio: {values["resolved_ratio"]}')
    console.print(f'diagnostics: {values["diagnostics"]}')
    if values.get('diagnostics_by_code'):
        console.print(f'diagnostics_by_code: {values["diagnostics_by_code"]}')
    console.print(f'revisions: {values["revisions"]}')
    console.print(f'latest_revision: {values["latest_revision"]}')
    for key in (
        'enrichment_jobs',
        'enrichment_parked',
        'enrichment_paused',
        'enrichment_pause_reason',
        'enrichment_processed',
        'enrichment_calls_used',
        'enrichment_tokens_used',
        'enrichment_cache_hits',
        'enrichment_cache_entries',
    ):
        console.print(f'{key}: {values[key]}')


@graph.command('versions')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
def versions(project_path: str):
    """Report CLI, compiler, and projector versions."""
    from rich.console import Console

    console = Console()
    project = Path(project_path).resolve()
    revision_inputs = _latest_revision_inputs(project)

    console.print(f'stackmind_cli: {__version__}')
    console.print(f'ir_schema: {IR_SCHEMA_VERSION}')
    console.print(
        f'compiler: {revision_inputs.get("compiler_version") or COMPILER_VERSION}'
    )
    console.print(f'knowledge_schema: {KNOWLEDGE_SCHEMA_VERSION}')
    console.print(f'latest_revision: {latest_revision_id(project)}')
    for name, version in sorted(projection_versions().items()):
        console.print(f'{name}: {version}')
    for key in ('git_commit', 'registry_version', 'schema_version', 'sync_ref'):
        console.print(f'{key}: {revision_inputs.get(key)}')


def _graph_stats(project_path: Path) -> dict[str, Any]:
    project_path = project_path.resolve()
    revisions_path = project_path / '.sync' / 'knowledge' / 'revisions'
    revisions = len(list(revisions_path.glob('REV-*.json'))) if revisions_path.exists() else 0
    latest = latest_revision_id(project_path)

    summary_path = project_path / '.sync' / 'knowledge' / 'cache' / 'metrics' / 'summary.json'
    summary_data = None
    if summary_path.exists():
        try:
            summary_data = json.loads(summary_path.read_text(encoding='utf-8'))
        except Exception:
            summary_data = None

    if summary_data is not None:
        nodes = summary_data.get('nodes', {}).get('total', 0)
        edges = summary_data.get('edges', {}).get('total', 0)
        resolved_ratio = summary_data.get('edges', {}).get('resolved_ratio', 0.0)
        diagnostics = summary_data.get('diagnostics', {}).get('total', 0)
        diagnostics_by_code = summary_data.get('diagnostics', {}).get('by_code', {})
    else:
        ir = read_ir(project_path)
        nodes = len(ir.symbols)
        edges = len(ir.edges)
        resolved_count = sum(1 for e in ir.edges if e.resolution == 'RESOLVED')
        resolved_ratio = round(resolved_count / edges, 4) if edges else 0.0
        diagnostics = len(ir.diagnostics)
        from collections import Counter
        diagnostics_by_code = dict(sorted(Counter(d.code for d in ir.diagnostics).items()))

    values: dict[str, Any] = {
        'edges': edges,
        'latest_revision': latest,
        'nodes': nodes,
        'resolved_ratio': resolved_ratio,
        'diagnostics': diagnostics,
        'diagnostics_by_code': diagnostics_by_code,
        'revisions': revisions,
    }
    values.update(read_enrichment_status(project_path))
    return values


def _latest_revision_inputs(project_path: Path) -> dict[str, Any]:
    latest = latest_revision_id(project_path)
    if latest == 0:
        return {}
    path = revision_path(project_path, latest)
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    revision_inputs = data.get('revision_inputs', {})
    if not isinstance(revision_inputs, dict):
        return {}
    return revision_inputs


def _compiled_models(project_path: Path) -> list[dict[str, Any]]:
    ir = read_ir(project_path.resolve())
    symbols_by_id = {symbol.node_id: symbol for symbol in ir.symbols}
    outgoing = _outgoing_edges(ir)
    models: list[dict[str, Any]] = []

    for symbol in sorted(ir.symbols, key=lambda item: (item.path, item.qualified_name, item.node_id)):
        if symbol.kind != 'Class':
            continue
        edges = outgoing.get(symbol.node_id, [])
        framework = None
        if any(_marks_pydantic_model(edge, symbols_by_id) for edge in edges):
            framework = 'pydantic'
        if any(edge.relation in {'DECLARES_COLUMN', 'DECLARES_RELATIONSHIP'} for edge in edges):
            framework = 'sqlalchemy'
        if framework is None:
            continue
        models.append(
            {
                'framework': framework,
                'node_id': symbol.node_id,
                'path': symbol.path,
                'qualified_name': symbol.qualified_name,
                'inherits': [
                    _edge_label(edge, symbols_by_id)
                    for edge in edges
                    if edge.relation == 'INHERITS'
                ],
            }
        )
    return models


def _compiled_model_detail(project_path: Path, name: str) -> dict[str, Any] | None:
    ir = read_ir(project_path.resolve())
    symbols_by_id = {symbol.node_id: symbol for symbol in ir.symbols}
    outgoing = _outgoing_edges(ir)
    models = _compiled_models(project_path)

    exact = [item for item in models if item['qualified_name'] == name]
    if exact:
        selected = exact[0]
    else:
        matches = [item for item in models if item['qualified_name'].rsplit('.', 1)[-1] == name]
        if len(matches) != 1:
            return None
        selected = matches[0]

    model_symbol = symbols_by_id[selected['node_id']]
    model_edges = outgoing.get(model_symbol.node_id, [])
    fields = []
    validators = []
    columns = []
    relationships = []
    config = None

    for edge in model_edges:
        target = symbols_by_id.get(edge.target_id or '')
        if edge.relation == 'DECLARES_COLUMN' and target is not None:
            columns.append(
                {
                    'name': _suffix_name(target.qualified_name, '.__column__.'),
                    'qualified_name': target.qualified_name,
                    'signature': target.signature,
                    'foreign_key': next(
                        (_edge_label(item, symbols_by_id) for item in outgoing.get(target.node_id, []) if item.relation == 'FOREIGN_KEY'),
                        None,
                    ),
                }
            )
            continue
        if edge.relation == 'DECLARES_RELATIONSHIP' and target is not None:
            relationships.append(
                {
                    'name': _suffix_name(target.qualified_name, '.__relationship__.'),
                    'qualified_name': target.qualified_name,
                    'signature': target.signature,
                    'target': next(
                        (_edge_label(item, symbols_by_id) for item in outgoing.get(target.node_id, []) if item.relation == 'RELATES_TO'),
                        None,
                    ),
                }
            )
            continue
        if edge.relation == 'DECLARES_FIELD' and target is not None:
            fields.append(
                {
                    'name': _suffix_name(target.qualified_name, '.__field__.'),
                    'qualified_name': target.qualified_name,
                    'signature': target.signature,
                }
            )
            continue
        if edge.relation == 'DECLARES_VALIDATOR' and target is not None:
            validator_edges = outgoing.get(target.node_id, [])
            validators.append(
                {
                    'name': _suffix_name(target.qualified_name, '.__validator__.'),
                    'qualified_name': target.qualified_name,
                    'signature': target.signature,
                    'fields': sorted(
                        _suffix_name(symbols_by_id[item.target_id].qualified_name, '.__field__.')
                        if item.target_id and item.target_id in symbols_by_id
                        else item.target_name.rsplit('.', 1)[-1]
                        for item in validator_edges
                        if item.relation == 'VALIDATES'
                    ),
                }
            )
            continue
        if edge.relation == 'HAS_CONFIG' and target is not None:
            config = {
                'qualified_name': target.qualified_name,
                'signature': target.signature,
            }

    return {
        'node_id': model_symbol.node_id,
        'path': model_symbol.path,
        'qualified_name': model_symbol.qualified_name,
        'framework': selected.get('framework'),
        'inherits': selected['inherits'],
        'columns': sorted(columns, key=lambda item: item['name']),
        'relationships': sorted(relationships, key=lambda item: item['name']),
        'fields': sorted(fields, key=lambda item: item['name']),
        'validators': sorted(validators, key=lambda item: item['name']),
        'config': config,
    }


def _compiled_relations(project_path: Path, name: str) -> dict[str, Any] | None:
    detail = _compiled_model_detail(project_path, name)
    if detail is None or not detail.get('columns') and not detail.get('relationships'):
        return None
    return {
        'node_id': detail['node_id'],
        'path': detail['path'],
        'qualified_name': detail['qualified_name'],
        'relationships': detail['relationships'],
        'foreign_keys': [
            {
                'column': column['name'],
                'target': column['foreign_key'],
            }
            for column in detail['columns']
            if column.get('foreign_key')
        ],
    }


def _compiled_schema(project_path: Path) -> dict[str, Any]:
    ir = read_ir(project_path.resolve())
    models = [
        model
        for model in (_compiled_model_detail(project_path, item['qualified_name']) for item in _compiled_models(project_path))
        if model is not None and (model.get('columns') or model.get('relationships'))
    ]
    association_tables = [
        {
            'name': _suffix_name(symbol.qualified_name, '.__association_table__.'),
            'path': symbol.path,
            'qualified_name': symbol.qualified_name,
            'signature': symbol.signature,
        }
        for symbol in sorted(ir.symbols, key=lambda item: (item.path, item.qualified_name, item.node_id))
        if symbol.kind == 'SQLAlchemyAssociationTable'
    ]
    sessions = [
        {
            'name': _suffix_name(symbol.qualified_name, '.__session__.'),
            'path': symbol.path,
            'qualified_name': symbol.qualified_name,
            'signature': symbol.signature,
        }
        for symbol in sorted(ir.symbols, key=lambda item: (item.path, item.qualified_name, item.node_id))
        if symbol.kind == 'SQLAlchemySession'
    ]
    repositories = [
        {
            'name': _suffix_name(symbol.qualified_name, '.__repository__'),
            'path': symbol.path,
            'qualified_name': symbol.qualified_name,
            'signature': symbol.signature,
        }
        for symbol in sorted(ir.symbols, key=lambda item: (item.path, item.qualified_name, item.node_id))
        if symbol.kind == 'SQLAlchemyRepository'
    ]
    return {
        'association_tables': association_tables,
        'models': sorted(models, key=lambda item: item['qualified_name']),
        'repositories': repositories,
        'sessions': sessions,
    }


def _compiled_routes(project_path: Path) -> list[dict[str, Any]]:
    ir = read_ir(project_path.resolve())
    symbols_by_id = {symbol.node_id: symbol for symbol in ir.symbols}
    outgoing = _outgoing_edges(ir)
    routes: list[dict[str, Any]] = []
    for symbol in sorted(ir.symbols, key=lambda item: (item.path, item.qualified_name, item.node_id)):
        if symbol.kind != 'FastAPIRoute':
            continue
        route = _route_payload(symbol, outgoing.get(symbol.node_id, []), symbols_by_id)
        routes.append(route)
    return routes


def _compiled_endpoint_detail(project_path: Path, path: str) -> dict[str, Any] | None:
    ir = read_ir(project_path.resolve())
    symbols_by_id = {symbol.node_id: symbol for symbol in ir.symbols}
    outgoing = _outgoing_edges(ir)
    routes = [
        symbol
        for symbol in sorted(ir.symbols, key=lambda item: (item.path, item.qualified_name, item.node_id))
        if symbol.kind == 'FastAPIRoute'
    ]
    matches = [
        symbol for symbol in routes
        if _route_parts(symbol.signature)['path'] == path
    ]
    if len(matches) != 1:
        return None
    selected = matches[0]
    payload = _route_payload(selected, outgoing.get(selected.node_id, []), symbols_by_id)
    dependencies = []
    auth = []
    request_models = []
    for edge in outgoing.get(selected.node_id, []):
        target = symbols_by_id.get(edge.target_id or '')
        if edge.relation == 'ROUTE_DEPENDS_ON' and target is not None:
            target_edges = outgoing.get(target.node_id, [])
            dependency_target = next(
                (_edge_label(item, symbols_by_id) for item in target_edges if item.relation == 'DEPENDS_TARGET'),
                None,
            )
            dependencies.append(
                {
                    'qualified_name': target.qualified_name,
                    'signature': target.signature,
                    'target': dependency_target,
                }
            )
        if edge.relation == 'USES_AUTH':
            auth.append(_suffix_name(_edge_label(edge, symbols_by_id), '.__auth__.'))
        if edge.relation == 'USES_REQUEST_MODEL':
            request_models.append(_edge_label(edge, symbols_by_id))
    return {
        **payload,
        'dependencies': sorted(dependencies, key=lambda item: item['qualified_name']),
        'auth': sorted(auth),
        'request_models': sorted(request_models),
    }


def _compiled_auth(project_path: Path) -> list[dict[str, Any]]:
    ir = read_ir(project_path.resolve())
    return [
        {
            'name': symbol.qualified_name.rsplit('.', 1)[-1],
            'path': symbol.path,
            'qualified_name': symbol.qualified_name,
            'signature': symbol.signature,
        }
        for symbol in sorted(ir.symbols, key=lambda item: (item.path, item.qualified_name, item.node_id))
        if symbol.kind == 'FastAPIAuth'
    ]


def _compiled_middleware(project_path: Path) -> list[dict[str, Any]]:
    ir = read_ir(project_path.resolve())
    return [
        {
            'line': symbol.location.get('line', 0),
            'middleware': symbol.signature.replace('middleware ', '', 1),
            'path': symbol.path,
            'qualified_name': symbol.qualified_name,
            'signature': symbol.signature,
        }
        for symbol in sorted(ir.symbols, key=lambda item: (item.path, item.location.get('line', 0), item.qualified_name))
        if symbol.kind == 'FastAPIMiddleware'
    ]


def _compiled_django_urls(project_path: Path) -> list[dict[str, Any]]:
    ir = read_ir(project_path.resolve())
    symbols_by_id = {symbol.node_id: symbol for symbol in ir.symbols}
    outgoing = _outgoing_edges(ir)
    urls = []
    for symbol in sorted(ir.symbols, key=lambda item: (item.path, item.location.get('line', 0), item.qualified_name)):
        if symbol.kind != 'DjangoURLPattern':
            continue
        parts = _django_url_parts(symbol.signature)
        target = parts['target']
        urls.append(
            {
                'kind': parts['kind'],
                'line': symbol.location.get('line', 0),
                'name': parts['name'] if parts['name'] != '-' else None,
                'node_id': symbol.node_id,
                'path': symbol.path,
                'qualified_name': symbol.qualified_name,
                'route': parts['route'],
                'target': target if target != '-' else None,
            }
        )
    return urls


def _compiled_django_signals(project_path: Path) -> list[dict[str, Any]]:
    ir = read_ir(project_path.resolve())
    symbols_by_id = {symbol.node_id: symbol for symbol in ir.symbols}
    outgoing = _outgoing_edges(ir)
    signals = []
    for symbol in sorted(ir.symbols, key=lambda item: (item.path, item.location.get('line', 0), item.qualified_name)):
        if symbol.kind != 'DjangoSignalReceiver':
            continue
        parts = _django_signal_parts(symbol.signature)
        edges = outgoing.get(symbol.node_id, [])
        signal = next((_edge_label(edge, symbols_by_id) for edge in edges if edge.relation == 'RECEIVES_SIGNAL'), parts['signal'])
        sender = next((_edge_label(edge, symbols_by_id) for edge in edges if edge.relation == 'SIGNAL_SENDER'), parts['sender'])
        receiver = next((_edge_label(edge, symbols_by_id) for edge in edges if edge.relation == 'CONNECTS_SIGNAL'), parts['receiver'])
        signals.append(
            {
                'line': symbol.location.get('line', 0),
                'node_id': symbol.node_id,
                'path': symbol.path,
                'qualified_name': symbol.qualified_name,
                'receiver': receiver if receiver != '-' else None,
                'sender': sender if sender != '-' else None,
                'signal': signal,
                'signature': symbol.signature,
            }
        )
    return signals


def _route_payload(symbol: Any, edges: list[Any], symbols_by_id: dict[str, Any]) -> dict[str, Any]:
    parts = _route_parts(symbol.signature)
    endpoint = next(
        (_edge_label(edge, symbols_by_id) for edge in edges if edge.relation == 'HANDLES'),
        parts['endpoint'],
    )
    response_model = next(
        (_edge_label(edge, symbols_by_id) for edge in edges if edge.relation == 'USES_RESPONSE_MODEL'),
        parts['response_model'],
    )
    return {
        'endpoint': endpoint,
        'method': parts['method'],
        'node_id': symbol.node_id,
        'path': parts['path'],
        'qualified_name': symbol.qualified_name,
        'response_model': response_model if response_model != '-' else None,
        'status_code': parts['status_code'] if parts['status_code'] != '-' else None,
    }


def _parse_kv_parts(rest: str) -> dict[str, str]:
    """Parse 'k1=v1; k2=v2' into a dictionary."""
    return dict(item.strip().split('=', 1) for item in rest.split(';') if '=' in item)


def _route_parts(signature: str) -> dict[str, str | None]:
    first, _, rest = signature.partition(';')
    method, _, path = first.partition(' ')
    kv = _parse_kv_parts(rest)
    return {
        'endpoint': kv.get('endpoint'),
        'method': method,
        'path': path,
        'response_model': kv.get('response_model'),
        'status_code': kv.get('status_code'),
    }


def _django_url_parts(signature: str) -> dict[str, str | None]:
    first, _, rest = signature.partition(';')
    kind, _, route = first.partition(' ')
    kv = _parse_kv_parts(rest)
    return {'kind': kind, 'name': kv.get('name'), 'route': route, 'target': kv.get('target')}


def _django_signal_parts(signature: str) -> dict[str, str | None]:
    first, _, rest = signature.partition(';')
    _, _, signal = first.partition(' ')
    kv = _parse_kv_parts(rest)
    return {
        'receiver': kv.get('receiver', kv.get('function')),
        'sender': kv.get('sender'),
        'signal': signal,
    }


def _outgoing_edges(ir: Any) -> dict[str, list[Any]]:
    outgoing: dict[str, list[Any]] = {}
    for edge in ir.edges:
        outgoing.setdefault(edge.source_id, []).append(edge)
    return outgoing


def _marks_pydantic_model(edge: Any, symbols_by_id: dict[str, Any]) -> bool:
    if edge.relation in {'DECLARES_FIELD', 'DECLARES_VALIDATOR', 'HAS_CONFIG'}:
        return True
    if edge.relation != 'INHERITS':
        return False
    if edge.target_name.startswith('pydantic.'):
        return True
    target = symbols_by_id.get(edge.target_id or '')
    return target is not None and target.kind == 'Class'


def _edge_label(edge: Any, symbols_by_id: dict[str, Any]) -> str:
    target = symbols_by_id.get(edge.target_id or '')
    if target is not None:
        return target.qualified_name
    return edge.target_name


def _suffix_name(value: str, marker: str) -> str:
    if marker not in value:
        return value.rsplit('.', 1)[-1]
    return value.split(marker, 1)[1]


def _emit_envelope(console: Any, envelope: Any, *, json_output: bool) -> None:
    if json_output:
        console.print_json(json.dumps(envelope.to_dict()))
        return
    console.print(f'revision: {envelope.revision}')
    console.print(f'git_commit: {envelope.git_commit}')
    console.print(f'stale: {envelope.stale}')
    console.print(f'semantic: {envelope.semantic}')
    console.print(f'results: {len(envelope.results)}')
    if envelope.truncated:
        console.print(f'truncated: {envelope.truncated}')
        if envelope.truncation_reason:
            console.print(f'truncation_reason: {envelope.truncation_reason}')
    for item in envelope.results:
        console.print(
            f"- {item.kind} {item.qualified_name} [{item.node_id}] "
            f"{item.path} confidence={item.confidence}"
        )
        if item.provenance_summary:
            console.print(f"  provenance: {item.provenance_summary}")
        if item.evidence:
            console.print(
                "  evidence: "
                + ", ".join(
                    f"{evidence.provider}/{evidence.evidence_type}"
                    for evidence in item.evidence
                )
            )


@graph.command('tasks')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def tasks_command(project_path: str, json_output: bool):
    """List compiled Celery tasks and beat schedules."""
    from rich.console import Console
    console = Console()
    payload = _compiled_celery_tasks(Path(project_path).resolve())
    if json_output:
        console.print_json(json.dumps(payload))
        return
    console.print(f"tasks: {len(payload['tasks'])}")
    for task in payload['tasks']:
        console.print(f"- {task['name']} (queue={task['queue'] or '-'}, bind={task['bind']})")
    console.print(f"beat_schedules: {len(payload['beat_schedules'])}")
    for beat in payload['beat_schedules']:
        console.print(f"- {beat['entry_name']}: task={beat['task']} schedule={beat['schedule']} options={beat['options'] or '-'}")


@graph.command('task-flow')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def task_flow_command(project_path: str, json_output: bool):
    """Show flow mapping caller code to asynchronous task execution."""
    from rich.console import Console
    console = Console()
    flow = _compiled_task_flow(Path(project_path).resolve())
    if json_output:
        console.print_json(json.dumps({'task_flow': flow}))
        return
    console.print(f"task_flows: {len(flow)}")
    for item in flow:
        console.print(f"- {item['caller']} -> triggers task {item['task']} ({item['path']}:{item['line']})")


@graph.command('migrations')
@click.option(
    '--project',
    '-p',
    'project_path',
    type=click.Path(exists=True),
    default='.',
    help='Project path',
)
@click.option('--json-output', is_flag=True, help='Emit machine-readable JSON')
def migrations_command(project_path: str, json_output: bool):
    """List migration history DAG sequentially."""
    from rich.console import Console
    console = Console()
    migs = _compiled_migrations(Path(project_path).resolve())
    if json_output:
        console.print_json(json.dumps({'migrations': migs}))
        return
    console.print(f"migrations: {len(migs)}")
    for mig in migs:
        down = mig['down_revision']
        down_str = f"<- {down}" if down else "base"
        console.print(f"- {mig['revision']} ({down_str}) {mig['path']}")


def _compiled_celery_tasks(project_path: Path) -> dict[str, Any]:
    ir = read_ir(project_path.resolve())
    tasks = []
    beat_schedules = []
    for symbol in sorted(ir.symbols, key=lambda item: (item.path, item.qualified_name, item.node_id)):
        if symbol.kind == 'CeleryTask':
            parts = _parse_celery_task_signature(symbol.signature)
            tasks.append({
                'node_id': symbol.node_id,
                'path': symbol.path,
                'qualified_name': symbol.qualified_name,
                'name': parts['name'],
                'queue': parts['queue'],
                'bind': parts['bind'] == 'true',
                'options': parts['options'],
            })
        elif symbol.kind == 'CeleryBeatSchedule':
            parts = _parse_celery_beat_signature(symbol.signature)
            beat_schedules.append({
                'node_id': symbol.node_id,
                'path': symbol.path,
                'qualified_name': symbol.qualified_name,
                'entry_name': parts['entry_name'],
                'task': parts['task'],
                'schedule': parts['schedule'],
                'options': parts['options'],
            })
    return {
        'tasks': tasks,
        'beat_schedules': beat_schedules,
    }


def _parse_celery_task_signature(sig: str) -> dict[str, str | None]:
    first, _, rest = sig.partition(';')
    name = first.replace('task ', '', 1).strip()
    kv = _parse_kv_parts(rest)
    return {'name': name, 'queue': kv.get('queue'), 'bind': kv.get('bind', 'false'), 'options': kv.get('options')}


def _parse_celery_beat_signature(sig: str) -> dict[str, str | None]:
    first, _, rest = sig.partition(';')
    entry_name = first.replace('beat ', '', 1).strip()
    kv = _parse_kv_parts(rest)
    return {'entry_name': entry_name, 'task': kv.get('task'), 'schedule': kv.get('schedule'), 'options': kv.get('options')}


def _compiled_task_flow(project_path: Path) -> list[dict[str, Any]]:
    ir = read_ir(project_path.resolve())
    symbols_by_id = {symbol.node_id: symbol for symbol in ir.symbols}
    flow = []
    
    edges = sorted(
        ir.edges,
        key=lambda item: (item.path, item.line, item.source_id, item.target_name)
    )
    
    for edge in edges:
        if edge.relation == 'TRIGGERS_CELERY_TASK':
            caller_sym = symbols_by_id.get(edge.source_id)
            task_sym = symbols_by_id.get(edge.target_id or '')
            
            caller_name = caller_sym.qualified_name if caller_sym else 'Unknown'
            if task_sym:
                parts = _parse_celery_task_signature(task_sym.signature)
                task_name = parts['name']
            else:
                task_name = edge.target_name
                if task_name.endswith('.__celery_task__'):
                    task_name = task_name[:-16]
                    
            flow.append({
                'caller': caller_name,
                'task': task_name,
                'path': edge.path,
                'line': edge.line,
                'resolved': edge.resolution == 'RESOLVED',
            })
    return flow


def _compiled_migrations(project_path: Path) -> list[dict[str, Any]]:
    ir = read_ir(project_path.resolve())
    migrations = []
    for symbol in sorted(ir.symbols, key=lambda item: (item.path, item.qualified_name, item.node_id)):
        if symbol.kind == 'AlembicMigration':
            parts = _parse_alembic_signature(symbol.signature)
            migrations.append({
                'node_id': symbol.node_id,
                'path': symbol.path,
                'qualified_name': symbol.qualified_name,
                'revision': parts['revision'],
                'down_revision': parts['down_revision'],
                'operations': parts['operations'],
            })
    return _topo_sort_migrations(migrations)


def _parse_alembic_signature(sig: str) -> dict[str, Any]:
    first, _, rest = sig.partition(';')
    revision = first.replace('migration ', '', 1).strip()
    down_revision = None
    operations = []
    for part in rest.split(';'):
        k, _, v = part.strip().partition('=')
        if k == 'down_revision':
            if v == '-':
                down_revision = None
            elif ',' in v:
                down_revision = v.split(',')
            else:
                down_revision = v
        elif k == 'operations':
            try:
                operations = json.loads(v)
            except Exception:
                operations = []
    return {
        'revision': revision,
        'down_revision': down_revision,
        'operations': operations,
    }


def _topo_sort_migrations(migrations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_rev = {mig['revision']: mig for mig in migrations}
    ts = TopologicalSorter()
    for mig in migrations:
        rev = mig['revision']
        down = mig.get('down_revision')
        parents = [down] if isinstance(down, str) else (down if isinstance(down, list) else [])
        valid_parents = [p for p in parents if p in by_rev]
        ts.add(rev, *valid_parents)
    try:
        sorted_revs = [r for r in ts.static_order() if r in by_rev]
    except CycleError:
        sorted_revs = sorted(by_rev.keys())
    return [by_rev[rev] for rev in sorted_revs]


def _reconstruct_schema_at(migrations: list[dict[str, Any]], target_rev: str | None) -> dict[str, list[str]]:
    schema = {}
    for mig in migrations:
        for op in mig['operations']:
            op_type = op.get('op')
            if op_type == 'create_table':
                table = op.get('table')
                cols = op.get('columns', [])
                schema[table] = list(cols)
            elif op_type == 'drop_table':
                table = op.get('table')
                if table in schema:
                    del schema[table]
            elif op_type == 'add_column':
                table = op.get('table')
                col = op.get('column')
                if table in schema and col not in schema[table]:
                    schema[table].append(col)
            elif op_type == 'drop_column':
                table = op.get('table')
                col = op.get('column')
                if table in schema and col in schema[table]:
                    schema[table].remove(col)
            elif op_type == 'alter_column':
                pass
                
        if target_rev and mig['revision'] == target_rev:
            break
            
    return schema


from cli.contract import contract_group, explain_denial_command, scope_command
graph.add_command(contract_group, name="contract")
graph.add_command(explain_denial_command, name="explain-denial")
graph.add_command(scope_command, name="scope")

__all__ = ['graph']
