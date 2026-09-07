from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli.init import init
from cli.main import cli
from validators.knowledge.compiler import compile_project
from validators.knowledge.compiler.incremental import incremental_update
from validators.knowledge.projections import build_projections
from validators.knowledge.storage import read_ir
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


def test_compile_project_extracts_django_urls_models_signals_and_middleware(fresh_project):
    put(
        fresh_project,
        'project/urls.py',
        (
            'from django.urls import include, path, re_path\n'
            'from blog import views\n\n'
            'urlpatterns = [\n'
            '    path("health/", views.health, name="health"),\n'
            '    path("blog/", include("blog.urls")),\n'
            '    re_path(r"^legacy/$", views.legacy, name="legacy"),\n'
            ']\n'
        ),
    )
    put(
        fresh_project,
        'project/settings.py',
        (
            'MIDDLEWARE = [\n'
            '    "django.middleware.security.SecurityMiddleware",\n'
            '    "blog.middleware.TraceMiddleware",\n'
            ']\n'
        ),
    )
    put(
        fresh_project,
        'blog/urls.py',
        (
            'from django.urls import path\n'
            'from . import views\n\n'
            'urlpatterns = [path("posts/", views.post_list, name="post-list")]\n'
        ),
    )
    put(
        fresh_project,
        'blog/views.py',
        (
            'def health(request):\n'
            '    return None\n\n'
            'def legacy(request):\n'
            '    return None\n\n'
            'def post_list(request):\n'
            '    return None\n'
        ),
    )
    put(
        fresh_project,
        'blog/models.py',
        (
            'from django.db import models\n\n'
            'class Author(models.Model):\n'
            '    name = models.CharField(max_length=100, unique=True)\n\n'
            'class Post(models.Model):\n'
            '    title = models.CharField(max_length=200)\n'
            '    author = models.ForeignKey(Author, on_delete=models.CASCADE)\n\n'
            '    class Meta:\n'
            '        ordering = ["title"]\n'
        ),
    )
    put(
        fresh_project,
        'blog/signals.py',
        (
            'from django.db.models.signals import post_save, pre_save\n'
            'from django.dispatch import receiver\n'
            'from .models import Post\n\n'
            '@receiver(post_save, sender=Post)\n'
            'def index_post(sender, instance, **kwargs):\n'
            '    return None\n\n'
            'pre_save.connect(index_post, sender=Post)\n'
        ),
    )
    put(
        fresh_project,
        'blog/api.py',
        (
            'from rest_framework import serializers, viewsets\n'
            'from .models import Post\n\n'
            'class PostSerializer(serializers.ModelSerializer):\n'
            '    class Meta:\n'
            '        model = Post\n'
            '        fields = ["id", "title"]\n\n'
            'class PostViewSet(viewsets.ModelViewSet):\n'
            '    queryset = Post.objects.all()\n'
            '    serializer_class = PostSerializer\n'
        ),
    )

    data = json.loads(compile_project(fresh_project).to_json())
    symbols = {item['qualified_name']: item for item in data['symbols']}
    edges = data['edges']

    assert any(item['kind'] == 'DjangoURLPattern' and 'blog/posts/' in item['signature'] for item in data['symbols'])
    assert any(item['kind'] == 'DjangoMiddleware' and 'TraceMiddleware' in item['signature'] for item in data['symbols'])
    assert 'Author.__django_field__.name' in symbols
    assert 'Post.__django_field__.author' in symbols
    assert 'Post.__django_meta__.Meta' in symbols
    assert 'index_post.__django_signal_receiver__.post_save' in symbols
    assert 'PostSerializer.__django_serializer__' in symbols
    assert 'PostViewSet.__django_viewset__' in symbols
    assert any(
        edge['relation'] == 'URL_HANDLES'
        and edge['target_name'].endswith('blog/views.py:post_list')
        for edge in edges
    )
    assert any(
        edge['relation'] == 'RELATES_TO'
        and edge['source_id'] == symbols['Post.__django_field__.author']['node_id']
        and edge['target_id'] == symbols['Author']['node_id']
        for edge in edges
    )
    assert any(
        edge['relation'] == 'SIGNAL_SENDER'
        and edge['source_id'] == symbols['index_post.__django_signal_receiver__.post_save']['node_id']
        and edge['target_id'] == symbols['Post']['node_id']
        for edge in edges
    )
    assert any(
        edge['relation'] == 'SERIALIZES_MODEL'
        and edge['source_id'] == symbols['PostSerializer.__django_serializer__']['node_id']
        and edge['target_id'] == symbols['Post']['node_id']
        for edge in edges
    )


def test_graph_django_cli_reports_urls_and_signals(runner, fresh_project):
    put(
        fresh_project,
        'blog/urls.py',
        (
            'from django.urls import path\n'
            'from . import views\n\n'
            'urlpatterns = [path("posts/", views.post_list, name="post-list")]\n'
        ),
    )
    put(
        fresh_project,
        'blog/views.py',
        (
            'def post_list(request):\n'
            '    return None\n'
        ),
    )
    put(
        fresh_project,
        'blog/models.py',
        (
            'from django.db import models\n\n'
            'class Post(models.Model):\n'
            '    title = models.CharField(max_length=200)\n'
        ),
    )
    put(
        fresh_project,
        'blog/signals.py',
        (
            'from django.db.models.signals import post_save\n'
            'from django.dispatch import receiver\n'
            'from .models import Post\n\n'
            '@receiver(post_save, sender=Post)\n'
            'def index_post(sender, instance, **kwargs):\n'
            '    return None\n'
        ),
    )
    build_graph(fresh_project)

    urls = runner.invoke(cli, ['graph', 'django-urls', '--project', str(fresh_project), '--json-output'])
    signals = runner.invoke(cli, ['graph', 'django-signals', '--project', str(fresh_project), '--json-output'])

    assert urls.exit_code == 0
    assert signals.exit_code == 0
    url_payload = json.loads(urls.output)
    signal_payload = json.loads(signals.output)
    assert url_payload['urls'][0]['route'] == 'posts/'
    assert url_payload['urls'][0]['target'] == 'blog.views.post_list'
    assert signal_payload['signals'][0]['signal'] == 'post_save'
    assert signal_payload['signals'][0]['sender'] == 'Post'


def test_incremental_update_preserves_django_artifacts(fresh_project):
    put(
        fresh_project,
        'blog/models.py',
        (
            'from django.db import models\n\n'
            'class Post(models.Model):\n'
            '    title = models.CharField(max_length=200)\n'
        ),
    )
    build_graph(fresh_project)

    put(
        fresh_project,
        'blog/models.py',
        (
            'from django.db import models\n\n'
            'class Post(models.Model):\n'
            '    title = models.CharField(max_length=200)\n'
            '    slug = models.CharField(max_length=80)\n'
        ),
    )
    result = incremental_update(fresh_project)
    ir = read_ir(fresh_project)

    assert result.changed
    assert any(symbol.qualified_name == 'Post.__django_field__.slug' for symbol in ir.symbols)
