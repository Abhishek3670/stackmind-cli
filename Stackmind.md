# StackMind CLI — One Source Truth

**Project**: StackMind CLI (stackmind-cli), a Python-based multi-agent engineering runtime
**Repository**: https://github.com/Abhishek3670/stackmind
**Package**: `stackmind` on PyPI; install via `pip install stackmind` or `pip install -e .` from this repo

## Core Identity
- **Language**: Python ≥3.10
- **Build system**: Hatchling
- **License**: MIT (Copyright © 2026 Abhishek Sharma); `pyproject.toml` declares `license = "MIT"`
- **Development status**: `5 - Production/Stable` (per `pyproject.toml` classifiers)
- **Entry point**: `stackmind = "cli.main:cli"` (console script)
- **Primary maintainer**: Abhishek Sharma (434 of 414 commits; git shortlog confirms)

## CLI Commands (17 subcommands)
| Command | Description |
|---|---|
| `init <path> [-n NAME] [-a claude,codex,…] [--no-git]` | Scaffold a new governed runtime (`.sync/`, `AGENTS.md`, work-orders, contracts) |
| `projects {list|add|remove|switch|current}` | Manage registered projects in `~/.stackmindcli/` |
| `validate [path] [--fix]` | 4-layer + knowledge validation (schema, structure, protocol, boot, knowledge) |
| `doctor [path]` | Version/compatibility/agent health check |
| `migrate [path] [--to VER] [--check] [--rollback]` | Apply YAML migrations |
| `shutdown <agent> [-p PATH] [--force] [--defer]` | Handoff + inbox-drain gated shutdown |
| `promote <agent> [-p PATH]` | Validate draft → promote → validate canonical (CLAUDE-01) |
| `lock {acquire|release|status} <agent>` | Advisory write lock at `.sync/runtime/LOCK` (PLAT-03) |
| `daemon {start|stop|status|restart} [-p 8765] [-w PATH]` | Local JSON-RPC daemon |
| `tui [--daemon-url URL] [-a AGENT] [-w PATH] [-s SESSION] [--demo]` | Governed TUI control surface |
| `graph {build|update|watch|query|callers|flows|impact|explain|context|stats|versions|…}` | Knowledge compiler & API |
| `harness run-once <agent> [-p PATH] [--release-target X]` | One governed worker turn |
| `analyze {runtime -- <cmd>|flows <paths>}` | Runtime tracing + bounded static flow |
| `experience {list|show|stats|compile|search|capture}` | Experience records (SQLite/FTS5) |
| `skill {list|show|create|test|promote|…|retrieve|audit}` | Procedural skills with execution profiles |
| `learn {clusters|mine|distill}` | Pattern mining → candidate skills |
| `memory {create|get|search|delete|list}` | Durable memory management |

## Architecture
- **Runtime**: Local-first, offline. State persisted under each project's `.sync/` directory.
- **Daemon**: JSON-RPC 2.0 server on `127.0.0.1:8765` (configurable); `validators/kernel/daemon/` implements the protocol and lifecycle supervisor.
- **Governance layers**: `INIT → PLANNING → AWAITING_APPROVAL → AUTHORING → DISPATCHING → EXECUTING → INTEGRATION_REVIEW → PRODUCT_READY → GITOPS → COMPLETE` with DAG ordering, concurrency limiting (default 2 agents via `STACKMIND_MAX_CONCURRENT_AGENTS`), and crash recovery.
- **Knowledge compiler**: Deterministic (`validators/knowledge/compiler/`); IR → sharded node store (T1) + caches (T2: reverse index, search, metrics, embeddings). Symbol identity via birth-hash registry (`TYPE-first16(SHA256(path:qualname))`), stable across renames/moves.
- **Evidence-based principle**: "LLM proposes, harness decides" — static verifiers (AST checks, `py_compile`, HTML/CSS parsers, node --check) back outcomes before rework triggers.
- **Agent concurrency**: Thread-safe `AgentConcurrencyLimiter`; per-turn tool limit expanded from 20 to 75.
- **Recovery & repair**: Automatic detection of recovery repairs, auto-bumping of `revision`/`contract_revision`; deterministic work-order completion during recovery; plan auto-attestation on resume.
- **Tool limits & sandboxing**: Dynamic `PYTHONPATH` workspace root injection for sandboxed test runners; ephemeral test/smoke log budget exemptions.
- **Graph API**: `stackmind graph query`, callers, impact, explain, context, routes, models, Django URLs, task-flow, migrations.

## Documentation
- **README.md**: 142 lines — version, requirements, install, quick-start command table, architecture overview, full command reference, knowledge graph section.
- **114 Markdown files** under `docs/`:
  - Governance handbook (`docs/governance/`)
  - CLI reference (`docs/governance/cli-reference.md`)
  - RFCs (`docs/rfcs/`, RFC-001 through RFC-006)
  - Architecture handbook (`docs/architectures/STACKMIND_ARCHITECTURE.md`)
  - Migration guide (`docs/roadmap/migration-guide.md`)
  - CHANGELOG, roadmap summaries, reports
- **No README/sparsity claim is valid** — documentation is comprehensive and published.

## Testing
- **276 test files** under `tests/`
- **3,884 tests collected** by `pytest` in ~3.75s (verified via `python -m pytest --collect-only -q`)
- One pre-existing test failure: `test_harness_validation_task_fails_closed_when_unstructured` (CONTRACT_READ_ONLY_VIOLATION; 14/15 tests in that file pass)
- Test paths configured in `pyproject.toml`: `[tool.pytest.ini_options] testpaths = ["tests"]`

## CI/CD
- **No CI configuration checked in**. No `.github/workflows`, `.gitlab-ci.yml`, `azure-pipelines.yml`, `Jenkinsfile`, or `.pre-commit-config.yaml` exists in the repository root.

## Cloud dependencies
- **None in core runtime**. The daemon is local JSON-RPC; state lives in `.sync/`. Mentions of Supabase, Qdrant, EdgeStore, or Anthropic appear only in archived `docs/archive/` and `graphify-out/` generated cache — **not** in runtime code or `pyproject.toml` dependencies.
- Optional embeddings extra: `sentence-transformers>=2.2.0` (via `pip install -e ".[embeddings]"`).

## Project metrics (as of today, 2026-11-10)
- **Commits**: 414 (first: 2026-09-07, last: 2026-11-10)
- **Contributors**: Abhishek Sharma (434 commits); 2 `stackmind-init` entries
- **Tags/versions**: `3.16.0` (package), `3.1.0` (runtime protocol, `.sync/RUNTIME_VERSION`)