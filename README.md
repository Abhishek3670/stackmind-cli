# stackmind-cli

Compiler-backed multi-agent engineering runtime — governed CLI, local JSON-RPC daemon, and deterministic knowledge compiler. One engine repo, many project instances via `.sync/`.

> **Version:** `3.10.3` · Python `>=3.10` · License MIT · See [`STACKMIND-CLI.md`](STACKMIND-CLI.md), [`docs/STACKMIND_ARCHITECTURE.md`](docs/STACKMIND_ARCHITECTURE.md), [`docs/`](docs/), [`CHANGELOG.md`](CHANGELOG.md)

## Install

```bash
pip install -e .          # from this repo (dev)
# or
pip install stackmind      # published package
```

Requires `click`, `pyyaml`, `jsonschema`, `rich` (see `pyproject.toml`). Optional: `sentence-transformers` for embeddings (`pip install -e ".[embeddings]"`).

## Quick start

```bash
stackmind init ./my-project --name "My Project"   # scaffold .sync/, AGENTS.md, work-orders, contracts
stackmind validate ./my-project                  # 4 layers + knowledge layer-5
stackmind doctor ./my-project                    # version/compat/agent health
stackmind tui --workspace ./my-project           # terminal control plane (auto-starts daemon on :8765)
```

Initialized layout:

```
my-project/
├── AGENTS.md
└── .sync/
    ├── RUNTIME_VERSION
    ├── runtime/{TREE.yaml,boot/,LOCK,daemon/,receipts/}
    ├── work-orders/{ACTIVE/,COMPLETED/,BLOCKED/,INDEX.yaml}
    ├── inbox/ outbox/ decisions/ reviews/
    └── knowledge/{registry/,nodes/,revisions/,cache/}
```

## Commands

```bash
stackmind --help
stackmind <command> --help
```

| Command | What it does |
|---------|--------------|
| `init <path> [-n NAME] [-a claude,codex,…] [--no-git]` | Create a new governed runtime |
| `projects {list|add|remove|switch|current}` | Central multi-project workspace manager (`~/.stackmindcli/`) |
| `validate [path] [--fix]` | Schema / structure / protocol / boot / knowledge checks |
| `doctor [path]` | Compatibility + agent + migration status |
| `migrate [path] [--to VER] [--check] [--rollback]` | Apply YAML migrations in `migrations/` |
| `shutdown <agent> [-p PATH] [--force] [--defer]` | Handoff + inbox-drain gated shutdown |
| `promote <agent> [-p PATH]` | `validate draft → promote → validate canonical` (CLAUDE-01) |
| `lock {acquire|release|status} <agent>` | Advisory write lock at `.sync/runtime/LOCK` (PLAT-03) |
| `daemon {start|stop|status|restart} [-p 8765] [-w PATH]` | Local JSON-RPC daemon (`validators/kernel/daemon/`) |
| `tui [--daemon-url URL] [-a AGENT] [-w PATH] [-s SESSION] [--demo]` | Governed TUI (project prep + daemon attach) |
| `graph {build|update|watch|query|callers|flows|impact|explain|context|stats|versions|…}` | Knowledge compiler & API (see below) |
| `harness run-once <agent> [-p PATH] [--release-target X]` | One governed worker turn |
| `analyze {runtime -- <cmd>|flows <paths>}` | Runtime tracing + bounded static flow (`FLOWS_TO`) |
| `experience {list|show|stats|compile|search|capture}` | Experience records (SQLite/FTS5) |
| `skill {list|show|create|test|promote|…|retrieve|audit}` | Procedural skills with execution profiles & 3-stage verification |
| `learn {clusters|mine|distill}` | Pattern mining → candidate skills |

Full reference: [`docs/cli-reference.md`](docs/cli-reference.md), [`docs/getting-started.md`](docs/getting-started.md), [`docs/protocols.md`](docs/protocols.md).

## Knowledge graph

Deterministic compiler (`validators/knowledge/compiler/`) → IR → sharded node store (T1) + caches (T2: reverse index, search, metrics, embeddings). Symbol identity via birth-hash registry (`TYPE-first16(SHA256(path:qualname))`), stable across renames/moves.

```bash
stackmind graph build              # full compile + projections
stackmind graph update             # incremental (git diff or --path/--deleted-path)
stackmind graph watch              # poll + debounce loop
stackmind graph query "AuthService" --kind Class --limit 10
stackmind graph callers <symbol> --limit 25
stackmind graph impact <symbol> --depth 3
stackmind graph explain <symbol>
stackmind graph context "where is auth handled" --token-budget 1200
stackmind graph stats
stackmind graph versions           # CLI / compiler / projection versions
# framework projections
stackmind graph routes | endpoint <path> | auth | middleware
stackmind graph models | model <Name> | relations <Name> | schema [--at REV]
stackmind graph django-urls | django-signals
stackmind graph tasks | task-flow | migrations
```

## Governance in one paragraph

Authority is `CEO → Claude (architect, sole writer of TREE.yaml/boot) → Gemma (QA gate) → Workers`. Work orders are the unit of work (typed YAML + `INDEX.yaml` ledger). Writes are serialized by `.sync/runtime/LOCK` (writes-only, checked acquire). Every layer is validated (`stackmind validate`); drift between `TREE.yaml` and `INDEX.yaml`, lock malformation, and registry/node invariants are all gated. See [`docs/protocols.md`](docs/protocols.md).

## Daemon & lifecycle

`validators/kernel/daemon/` is a local JSON-RPC server (default `127.0.0.1:8765`, state in `.sync/runtime/daemon/`). The `LifecycleSupervisor` drives `INIT → PLANNING → AWAITING_APPROVAL → AUTHORING → DISPATCHING → EXECUTING → INTEGRATION_REVIEW → PRODUCT_READY → GITOPS → COMPLETE` (`BLOCKED`/`FAILED` on unrecoverable gates), with DAG ordering, concurrent sibling dispatch, retry/contention bounds, and crash recovery.

The TUI (`cli/tui/`) is a control surface over daemon+supervisor — `:goal`, `:status`, `:approve`/`:reject`, `:resume`, streamed output — not a second source of truth.

## Harness

`AgentRunner` (`validators/harness/runner.py`) executes one contracted turn: inbox/WO discovery → contract + citizenship checks → `assemble_context` → scratch workspace → provider tool loop (`read_file`/`write_file`/`run_command`/`query_graph`/`web_search`/`image_search`/`web_fetch` via `validators/kernel/tools.py` + `providers/gateway.py`) → staged-diff verification (incl. no-op, `D024` QA gates, and `DeliverableVerifier` HTML media asset integrity checks) → validated write-back. Default provider is `EchoLLMProvider`; gateway also supports real backends (Ollama etc.).

## Docs

- Architecture handbook: [`docs/STACKMIND_ARCHITECTURE.md`](docs/STACKMIND_ARCHITECTURE.md)
- RFCs: [`docs/rfcs/`](docs/rfcs/) (RFC-001 identity → RFC-006 harness)
- Harness evaluation: [`docs/HARNESS-ENGINEERING-EVALUATION.md`](docs/HARNESS-ENGINEERING-EVALUATION.md)
- Migration guide: [`docs/migration-guide.md`](docs/migration-guide.md)

## Versioning

- Package/release: `VERSION` / `pyproject.toml` / `cli/__init__.py` — currently `3.9.0`
- Runtime protocol: `.sync/RUNTIME_VERSION` — currently `3.1.0` (separate from package version)
- `stackmind migrate --check` / `stackmind graph versions` to inspect
