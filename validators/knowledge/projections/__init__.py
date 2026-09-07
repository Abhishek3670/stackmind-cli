"""Projection engine for disposable knowledge cache artifacts."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from validators.knowledge.compiler.ir import CompilerIR
from validators.knowledge.storage import canonical_json, read_ir

from .metrics import (
    PROJECTOR_INPUTS as METRICS_INPUTS,
)
from .metrics import (
    PROJECTOR_NAME as METRICS_NAME,
)
from .metrics import (
    PROJECTOR_VERSION as METRICS_VERSION,
)
from .metrics import (
    build_metrics_documents,
)
from .reverse_index import (
    PROJECTOR_INPUTS as REVERSE_INDEX_INPUTS,
)
from .reverse_index import (
    PROJECTOR_NAME as REVERSE_INDEX_NAME,
)
from .reverse_index import (
    PROJECTOR_VERSION as REVERSE_INDEX_VERSION,
)
from .reverse_index import (
    build_reverse_index_documents,
)
from .search import (
    PROJECTOR_INPUTS as SEARCH_INPUTS,
)
from .search import (
    PROJECTOR_NAME as SEARCH_NAME,
)
from .search import (
    PROJECTOR_VERSION as SEARCH_VERSION,
)
from .search import (
    build_search_documents,
)

CACHE_ROOT_REL = Path('.sync') / 'knowledge' / 'cache'


@dataclass(frozen=True)
class ProjectionResult:
    """Summary of one projector materialization pass."""

    name: str
    version: str
    declared_inputs: tuple[str, ...]
    root: Path
    written_paths: tuple[Path, ...]


def cache_root(project_path: Path) -> Path:
    """Return the disposable T2 cache root."""
    return project_path.resolve() / CACHE_ROOT_REL


def build_projections(project_path: Path) -> tuple[ProjectionResult, ...]:
    """Materialize all T2 projections from the stored T1 knowledge graph."""
    project_path = project_path.resolve()
    ir = read_ir(project_path)
    specs: tuple[
        tuple[str, str, tuple[str, ...], Callable[[CompilerIR], dict[str, dict]]],
        ...,
    ] = (
        (
            REVERSE_INDEX_NAME,
            REVERSE_INDEX_VERSION,
            REVERSE_INDEX_INPUTS,
            build_reverse_index_documents,
        ),
        (SEARCH_NAME, SEARCH_VERSION, SEARCH_INPUTS, build_search_documents),
        (METRICS_NAME, METRICS_VERSION, METRICS_INPUTS, build_metrics_documents),
    )

    results: list[ProjectionResult] = []
    for name, version, declared_inputs, builder in specs:
        root = cache_root(project_path) / name
        written_paths = _rewrite_projection_root(root, builder(ir))
        results.append(
            ProjectionResult(
                name=name,
                version=version,
                declared_inputs=declared_inputs,
                root=root,
                written_paths=written_paths,
            )
        )
    return tuple(results)


def projection_versions() -> dict[str, str]:
    """Return the current version stamp for each projector."""
    return {
        REVERSE_INDEX_NAME: REVERSE_INDEX_VERSION,
        SEARCH_NAME: SEARCH_VERSION,
        METRICS_NAME: METRICS_VERSION,
    }


def _rewrite_projection_root(root: Path, documents: dict[str, dict]) -> tuple[Path, ...]:
    temp_root = root.parent / f'.{root.name}.tmp'
    if temp_root.exists():
        shutil.rmtree(temp_root)
    temp_root.mkdir(parents=True, exist_ok=True)

    for relative, document in sorted(documents.items()):
        path = temp_root / Path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(canonical_json(document), encoding='utf-8', newline='\n')

    root.mkdir(parents=True, exist_ok=True)
    # Delete files in existing root
    for p in list(root.glob("**/*")):
        if p.is_file():
            try:
                p.unlink()
            except Exception:
                pass

    # Move files from temp_root to root
    for p in sorted(temp_root.glob("**/*")):
        if p.is_file():
            rel = p.relative_to(temp_root)
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                try:
                    dest.unlink()
                except Exception:
                    pass
            shutil.move(str(p), str(dest))

    if temp_root.exists():
        shutil.rmtree(temp_root)
    return tuple(sorted(root.rglob('*.json')))


__all__ = [
    'CACHE_ROOT_REL',
    'ProjectionResult',
    'build_projections',
    'cache_root',
    'projection_versions',
]
