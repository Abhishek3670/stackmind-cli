# stackmind-cli

Compiler-backed multi-agent engineering runtime — governed CLI, local JSON-RPC daemon, and deterministic knowledge compiler. One engine repo, many project instances via `.sync/`.

> **Version:** `3.17.0` · Python `>=3.10` · License MIT · See [`STACKMIND-CLI.md`](docs/governance/STACKMIND-CLI.md), [`docs/STACKMIND_ARCHITECTURE.md`](docs/architectures/STACKMIND_ARCHITECTURE.md), [`docs/`](docs/), [`CHANGELOG.md`](CHANGELOG.md)

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

Full reference: [`docs/governance/cli-reference.md`](docs/governance/cli-reference.md), [`docs/roadmap/getting-started.md`](docs/roadmap/getting-started.md), [`docs/governance/protocols.md`](docs/governance/protocols.md).

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

Authority is `CEO → Claude (architect, sole writer of TREE.yaml/boot) → Gemma (QA gate) → Workers`. Work orders are the unit of work (typed YAML + `INDEX.yaml` ledger). Writes are serialized by `.sync/runtime/LOCK` (writes-only, checked acquire). Every layer is validated (`stackmind validate`); drift between `TREE.yaml` and `INDEX.yaml`, lock malformation, and registry/node invariants are all gated. See [`docs/governance/protocols.md`](docs/governance/protocols.md).

## Daemon & lifecycle

`validators/kernel/daemon/` is a local JSON-RPC server (default `127.0.0.1:8765`, state in `.sync/runtime/daemon/`). The `LifecycleSupervisor` drives `INIT → PLANNING → AWAITING_APPROVAL → AUTHORING → DISPATCHING → EXECUTING → INTEGRATION_REVIEW → PRODUCT_READY → GITOPS → COMPLETE` (`BLOCKED`/`FAILED` on unrecoverable gates), with DAG ordering, concurrent sibling dispatch, retry/contention bounds, and crash recovery.

The TUI (`cli/tui/`) is a control surface over daemon+supervisor — `:goal`, `:status`, `:approve`/`:reject`, `:resume`, streamed output — not a second source of truth.
- **Terminal Mouse Interaction & SGR Reporting**: Native terminal mouse wheel scrolling, drag-to-copy, and click navigation via SGR extended mouse reporting (`\x1b[?1000h\x1b[?1002h\x1b[?1006h`) with automatic Windows Console QuickEdit management. Configure via `:mouse [on|off]` or toggle.
- **Collapsible Side Panel**: Press `Ctrl+B`, click the header `[–]` button, click the vertical divider `│`, or type `:collapse` to toggle the right-hand StackMind Runtime sidebar between 2-column split and 100% full-width view.
- **Clean Edge-to-Edge Drag-to-Copy**: Column-isolated mouse selection extracts conversation text without polluting clipboard with sidebar widgets, scrollbars, or trailing spaces. Preserves 100% of bullet hierarchy, leading indentation, and blank separator lines.
- **Pristine Markdown Copying**: `:copy` formats technical contract payloads and copies raw Markdown with original formatting and code backticks directly to the system clipboard.

## Harness & Governance

`AgentRunner` (`validators/harness/runner/`) executes contracted agent turns with multi-layer governance:
- **Contract Change Requests (CCR)**: Worker agents dynamically request scope expansion via `request_contract_change`; strictly the Lead Architect (`claude`) can approve amendments.
- **Plan Binding & Scope Guard**: Binds work orders to `PLAN.md` content hashes and rejects unapproved scope drift.
- **Concurrency Limiting**: Thread-safe `AgentConcurrencyLimiter` caps concurrent running agents (`STACKMIND_MAX_CONCURRENT_AGENTS`, default 2).
- **Observability & Tracing**: `WorkOrderTracer` (`.sync/traces/{wo_id}.json`) and Run Cost Ledgers (`.sync/traces/run_cost_ledger.json`) calculate token consumption and USD costs per agent.
- **Fixed Feature Evals**: Canonical regression test suite (`validators/harness/evals.py`) to verify determinism and compliance across model or prompt updates.
- **Verification Gates**: Staged-diff verification, `D024` QA gates, and `DeliverableVerifier` HTML media asset integrity checks before validated write-back. Default provider is `EchoLLMProvider`; gateway also supports real backends (Ollama etc.).
- **The Evidence-Based Principle ("LLM Proposes, Harness Decides")**:
  - **`QA_NO_TEST_EVIDENCE` & Stack-Appropriate Static Verifiers**: In early work orders before test suites are authored, empty test runs (e.g. pytest exits 4/5) receive a deterministic `NO_TEST_EVIDENCE` outcome backed by stack-native checks (`node --check`, AST verification, `py_compile`, HTML/CSS parsers) instead of triggering false-negative rework or recovery cycles.
  - **Authoritative Harness QA Verdict Synthesis**: The harness authors `qa_verdict.json` using system UTC timestamps, true deliverable SHA-256 hashes, and canonical revisions, eliminating LLM date/hash hallucinations.
  - **Single Deliverable Ownership**: Strict enforcement in `delegate_task` ensures every deliverable path has exactly one owner across `ACTIVE`, `BLOCKED`, and `COMPLETED` work orders. QA reviews of existing orders register review operations rather than duplicate conflicting work orders.
  - **Strict `architect_fix` Recovery Gate**: The Lead Architect cannot close a blocked work order without all prerequisite dependencies being completed, deliverables existing on disk with valid SHA-256 hashes, and static checks passing.
  - **Full Blocker Diagnostics & Typed Failure Codes**: Blocker reports retain full findings (no ~200-char clipping) and supervisor transitions preserve error contexts, eliminating `failure_code: UNKNOWN`.
  - **Model Specialization Guidance**: Architectural governance recommends assigning strong reasoning models (e.g. Claude 3.5 Sonnet / Gemini Pro) to Architecture and QA roles, and dedicated models to Worker roles.
- **Recovery & Repair Overwrite Governance**: Automatic detection of recovery repairs and auto-bumping of revisions (`revision` and `contract_revision`) on active work orders and contracts, preventing deadlocks when the Lead Architect amends tasks.
- **Unquoted YAML Scalar Auto-Repair & Schema Sanitization**: Automatically folds unquoted multi-line descriptions with colons (`>-`) and normalizes `amendments` / `log` schema variations to ensure deterministic authoring writes.
- **Ephemeral Test & Smoke Log Budget Exemption**: Excludes scratch smoke and test logs (`tests/*_smoke_log.*`, `*.tmp`, `*.log`) from file budget checks, preventing artificial gate blocks.
- **Dynamic In-Turn & Verification Auto-Repair for `PLAN.md`**: Validates and auto-repairs checklist structure both in-turn and at verification.
- **Ponytail Durability & Turn Budget Scaling**: Scales initial turn budget dynamically (`200 + 20N`) and expands `:resume` grant to 500 turns (configurable via `STACKMIND_TURN_BUDGET`), raising human resume limits to 100 to eliminate repetitive budget halts.
- **Tool Limits & Non-Intrusive Execution**: Expanded default per-turn tool limits from 20 to 75 calls to avoid premature `ToolLimitExceededError`.
- **Authoring Gate & Report Deliverable Support**: First-class support for `deliverable.type: report` in `authoring_gate.py` and `work-order.schema.json`, plus normalized null path checks to avoid role-mismatch false positives.
- **Autonomous Recovery Loop Resolution**: Immediate archival of consumed recovery decisions and automatic normalization of `SUPERSEDED_DEPENDENCY` / `INVALID_DEPENDENCY` actions from `amend_contract` to `architect_fix`, preventing contention cancellations and infinite recovery loops.
- **Plan Auto-Attestation on Resume**: Automatically defaults `plan_id = "PLAN-001"` on resume when `PLAN.md` exists, eliminating false `FINAL_ACCEPTANCE_BLOCKED` rejections.
- **Retrospective Dependency Graph Validation Exemption**: Exempts completed work orders from retrospective graph validation and prerequisite re-evaluation, preventing circular dependency deadlocks when historical work orders are pruned.
- **Deterministic Work Order Completion in Autonomous Recovery**: Automatically marks verified completed orders on disk as `COMPLETED` during recovery to skip worker redispatch and prevent 40+ turn spinning loops.
- **Dynamic `PYTHONPATH` Workspace Root Injection**: Automatically injects project workspace root into `PYTHONPATH` for sandboxed test runner execution, ensuring local source packages resolve seamlessly.
- **Snapshot State Verification Exemption for Governance Files**: Excludes `.sync/` and `.git/` directories from state snapshot hashing in `verification.py`, allowing governance files to be safely authored without false-positive verification errors.
- **Turn 1 Planning Deliverable Auto-Synthesis**: Auto-synthesizes `PLAN.md` from the turn's planning output when the model omits explicit file write tool calls.
- **Autonomous Uninterrupted Orchestration (Laya Decision Specialist)**: Integrated fallback recovery decision provider (`LayaDecisionAdapter`) in `validators/kernel/daemon/supervisor/laya_adapter.py`. Automatically synthesizes compliant recovery decisions when timeouts or deadlocks occur during architecture repair decisions, eliminating manual `:resume` interruptions and infinite recovery halts.
- **In-Memory Python Syntax Verification**: Replaced external `py_compile` subprocess execution with in-memory `compile()` in `validators/harness/review_gate.py`, eliminating Windows file-locking `[Errno 13] Permission denied` errors when writing `.pyc` files into `__pycache__`.
- **Active Work Order Ownership & Safe Delegation**: In `delegate_task` (`validators/kernel/tools.py`), prevented QA review hijacking for worker agents and added explicit work order binding (`work_order_id` / `WO-xxx`), ensuring delegations route directly to active fix work orders rather than completed predecessor tasks.
- **TUI Cursor Drift Elimination & History Sanitization**: Anchored cursor row coordinates inside composer bounds, drained trailing mouse escape sequences, and sanitized raw LLM transport errors and billing alerts from chat history.

## Docs

- Architecture handbook: [`docs/architectures/STACKMIND_ARCHITECTURE.md`](docs/architectures/STACKMIND_ARCHITECTURE.md)
- RFCs: [`docs/rfcs/`](docs/rfcs/) (RFC-001 identity → RFC-006 harness)
- Harness evaluation: [`docs/governance/HARNESS-ENGINEERING-EVALUATION.md`](docs/governance/HARNESS-ENGINEERING-EVALUATION.md)
- Orchestration deep study: [`reports/STACKMIND_ORCHESTRATION_DEEP_STUDY.md`](reports/STACKMIND_ORCHESTRATION_DEEP_STUDY.md)
- Migration guide: [`docs/roadmap/migration-guide.md`](docs/roadmap/migration-guide.md)

## Versioning

- Package/release: `VERSION` / `pyproject.toml` / `cli/__init__.py` — currently `3.17.0`
- Runtime protocol: `.sync/RUNTIME_VERSION` — currently `3.1.0` (separate from package version)
- `stackmind migrate --check` / `stackmind graph versions` to inspect
