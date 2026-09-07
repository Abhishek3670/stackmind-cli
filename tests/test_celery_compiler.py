from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.knowledge.compiler import compile_project
from validators.knowledge.projections import build_projections
from validators.knowledge.writer import write_knowledge


@pytest.fixture
def fresh_project(tmp_path):
    project = tmp_path / 'project'
    init(project, name='Project', no_git=True)
    return project


@pytest.fixture
def runner():
    return CliRunner()


def put(project: Path, name: str, text: str) -> None:
    path = project / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')


def build_graph(project: Path) -> None:
    write_knowledge(project, compile_project(project), built_at='fixed')
    build_projections(project)


def test_compile_celery_apps_tasks_beat_and_invocations(fresh_project):
    put(
        fresh_project,
        'app/worker.py',
        (
            'from celery import Celery, shared_task\n'
            'app = Celery("my_app", broker="redis://localhost", backend="rpc://")\n\n'
            '@app.task(name="add_tasks", queue="high_priority", bind=True)\n'
            'def add(self, x, y):\n'
            '    return x + y\n\n'
            '@shared_task\n'
            'def mul(x, y):\n'
            '    return x * y\n'
        ),
    )
    put(
        fresh_project,
        'app/trigger.py',
        (
            'from .worker import add, mul\n\n'
            'def run_tasks():\n'
            '    add.delay(10, 20)\n'
            '    mul.apply_async(args=(5, 5), queue="low")\n'
        ),
    )
    put(
        fresh_project,
        'app/config.py',
        (
            'CELERY_BEAT_SCHEDULE = {\n'
            '    "run-add-every-hour": {\n'
            '        "task": "add_tasks",\n'
            '        "schedule": 3600.0,\n'
            '        "args": (1, 2)\n'
            '    }\n'
            '}\n'
        ),
    )

    ir = compile_project(fresh_project)
    data = json.loads(ir.to_json())
    symbols = {item['qualified_name']: item for item in data['symbols']}
    edges = data['edges']

    # 1. Assert CeleryApp
    assert '__celery_app__.app' in symbols
    assert 'broker=redis://localhost' in symbols['__celery_app__.app']['signature']

    # 2. Assert CeleryTasks
    assert 'add.__celery_task__' in symbols
    assert 'task add_tasks; queue=high_priority; bind=true' in symbols['add.__celery_task__']['signature']
    
    assert 'mul.__celery_task__' in symbols
    assert 'task app.worker.mul; queue=-; bind=false' in symbols['mul.__celery_task__']['signature']

    # 3. Assert CeleryBeatSchedule
    assert '__celery_beat__.run_add_every_hour' in symbols
    assert 'beat run-add-every-hour; task=add_tasks; schedule=3600.0' in symbols['__celery_beat__.run_add_every_hour']['signature']

    # 4. Assert Relations
    # DECLARES_CELERY_TASK
    assert any(
        e['relation'] == 'DECLARES_CELERY_TASK'
        and e['target_id'] == symbols['add.__celery_task__']['node_id']
        for e in edges
    )
    # WRAPS_FUNCTION
    assert any(
        e['relation'] == 'WRAPS_FUNCTION'
        and e['source_id'] == symbols['add.__celery_task__']['node_id']
        and e['target_id'] == symbols['add']['node_id']
        for e in edges
    )
    # SCHEDULES_TASK
    assert any(
        e['relation'] == 'SCHEDULES_TASK'
        and e['source_id'] == symbols['__celery_beat__.run_add_every_hour']['node_id']
        and e['target_id'] == symbols['add.__celery_task__']['node_id']
        for e in edges
    )
    # TRIGGERS_CELERY_TASK
    assert any(
        e['relation'] == 'TRIGGERS_CELERY_TASK'
        and e['source_id'] == symbols['run_tasks']['node_id']
        and e['target_id'] == symbols['add.__celery_task__']['node_id']
        for e in edges
    )
    assert any(
        e['relation'] == 'TRIGGERS_CELERY_TASK'
        and e['source_id'] == symbols['run_tasks']['node_id']
        and e['target_id'] == symbols['mul.__celery_task__']['node_id']
        for e in edges
    )


def test_celery_cli_commands(runner, fresh_project):
    put(
        fresh_project,
        'app/worker.py',
        (
            'from celery import Celery\n'
            'app = Celery("app")\n'
            '@app.task(name="my_task", queue="test_q")\n'
            'def my_task():\n'
            '    pass\n'
        ),
    )
    put(
        fresh_project,
        'app/trigger.py',
        (
            'from .worker import my_task\n'
            'def trigger_it():\n'
            '    my_task.delay()\n'
        ),
    )
    put(
        fresh_project,
        'app/config.py',
        (
            'CELERY_BEAT_SCHEDULE = {\n'
            '    "beat_entry": {\n'
            '        "task": "my_task",\n'
            '        "schedule": 60.0\n'
            '    }\n'
            '}\n'
        ),
    )
    build_graph(fresh_project)

    # test tasks command
    res_tasks = runner.invoke(cli, ['graph', 'tasks', '--project', str(fresh_project), '--json-output'])
    assert res_tasks.exit_code == 0
    data_tasks = json.loads(res_tasks.output)
    assert len(data_tasks['tasks']) == 1
    assert data_tasks['tasks'][0]['name'] == 'my_task'
    assert data_tasks['tasks'][0]['queue'] == 'test_q'
    assert len(data_tasks['beat_schedules']) == 1
    assert data_tasks['beat_schedules'][0]['entry_name'] == 'beat_entry'
    assert data_tasks['beat_schedules'][0]['schedule'] == '60.0'

    # test tasks command text output
    res_tasks_text = runner.invoke(cli, ['graph', 'tasks', '--project', str(fresh_project)])
    assert res_tasks_text.exit_code == 0
    assert 'tasks: 1' in res_tasks_text.output
    assert 'my_task' in res_tasks_text.output

    # test task-flow command
    res_flow = runner.invoke(cli, ['graph', 'task-flow', '--project', str(fresh_project), '--json-output'])
    assert res_flow.exit_code == 0
    data_flow = json.loads(res_flow.output)
    assert len(data_flow['task_flow']) == 1
    assert data_flow['task_flow'][0]['caller'] == 'trigger_it'
    assert data_flow['task_flow'][0]['task'] == 'my_task'

    # test task-flow command text output
    res_flow_text = runner.invoke(cli, ['graph', 'task-flow', '--project', str(fresh_project)])
    assert res_flow_text.exit_code == 0
    assert 'task_flows: 1' in res_flow_text.output
    assert 'trigger_it -> triggers task my_task' in res_flow_text.output
