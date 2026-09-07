"""Deterministic source-to-IR compiler frontend."""

from .ir import CompilerIR, DiagnosticIR, EdgeIR, SymbolIR
from .pydantic_compiler import augment_parsed_files as augment_pydantic_files
from .fastapi_compiler import augment_parsed_files as augment_fastapi_files
from .sqlalchemy_compiler import augment_parsed_files as augment_sqlalchemy_files
from .django_compiler import augment_parsed_files as augment_django_files
from .celery_compiler import augment_parsed_files as augment_celery_files
from .alembic_compiler import augment_parsed_files as augment_alembic_files
from .doc_compiler import augment_parsed_files as augment_doc_files
from .config_compiler import augment_parsed_files as augment_config_files
from .cicd_compiler import augment_parsed_files as augment_cicd_files
from .test_compiler import augment_parsed_files as augment_test_files
from .cycle_compiler import augment_parsed_files as augment_cycle_files
from .dead_code_compiler import augment_parsed_files as augment_dead_code_files
from .health_compiler import augment_parsed_files as augment_health_files
from .impact_compiler import augment_parsed_files as augment_impact_files
from .resolve import compile_project

__all__ = [
    'CompilerIR',
    'DiagnosticIR',
    'EdgeIR',
    'SymbolIR',
    'augment_fastapi_files',
    'augment_django_files',
    'augment_pydantic_files',
    'augment_sqlalchemy_files',
    'augment_celery_files',
    'augment_alembic_files',
    'augment_doc_files',
    'augment_config_files',
    'augment_cicd_files',
    'augment_test_files',
    'augment_cycle_files',
    'augment_dead_code_files',
    'augment_health_files',
    'augment_impact_files',
    'compile_project',
]
