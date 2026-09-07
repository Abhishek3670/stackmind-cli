# Project Technical Study & Future Improvement Report

**Project:** StackMind
**Repository root:** `W:\Aatish\Stuff\stackmind`
**Branch / HEAD:** `main` @ `6fd56479d20f3d9c3e1dddde6872913c44b122c8`
**Investigation date:** 2026-09-06
**Mode:** Read-only — no project files were created, edited, deleted, renamed, or moved. No dependencies were installed. No commands were run that mutate project state. No commits, branches, tags, tests, or application runs were performed.

> **Note on this file:** This report was written into `docs/` after the read-only investigation completed, at the user's explicit authorization. The investigation itself made no project changes. The pre-existing untracked `Project_Study_&_Future_Improvements.md` at the repo root was not authored or modified during this work.

---

## 1. Executive Summary

StackMind is a Python 3.10+ CLI (`stackmind`, entry point `cli.main:cli`) that combines a deterministic AST-based knowledge compiler, a contract-gated multi-agent execution harness, and a verified procedural-learning loop over a shared `.sync/` Git workspace. The repository contains 16 schemas, 71 validators, 50 test modules, 57 templates, 5 migrations, and 36 docs, packaged as a Hatchling wheel exposing `cli`, `schemas`, `templates`, `migrations`, and `validators` (`pyproject.toml:56-57`).

The project is best characterised today as a **capable deterministic graph toolkit grafted onto an incomplete trustworthy autonomous runtime**. The graph side has a sound IR (`validators/knowledge/compiler/ir.py`), a clean tiered store (T0 registry / T1 nodes+revisions / T2 projections), aliases for rename (`validators/knowledge/compiler/rename.py`), FTS5 experience indexes, and a Python-first AST/augmenter pipeline. The runtime side, however, exhibits a wide gap between documented guarantees and the code that enforces them. The harness executes proposed shell commands against the live workspace **after** declaring it has validated them, six "verification dimensions" are derived from trust-by-completion rather than independent checks, the skill pipeline can re-activate a `STALE`/`FAILED` skill through `revalidate` without actor gating, and the canonical-write lock is advisory and race-prone.

The single most important finding is that **execution isolation, contract enforcement, and verified-learning evidence are not yet at the level the documentation claims**. Before a real hosted LLM provider is wired in or autonomous skills act on production workspaces, the trust foundation (transactional staged execution, identity-bound contracts, content-bound approvals, snapshot-true verification, and immutable experience history) must be hardened. The roadmap therefore prioritises correctness, trust, and state consistency over feature breadth.

**Severity-tagged top risks:**

| # | Risk | Severity | Horizon |
|---|------|----------|---------|
| 1 | Live shell execution of LLM-suggested commands with no sandbox/timeout/rollback | Critical | Now |
| 2 | Verification dimensions marked true without independent checks | Critical | Now |
| 3 | Skill `revalidate` and `rollback` bypass approval gates | High | Now |
| 4 | KnowledgeAPI context assembly creates `.sync/skills/` on supposedly read-only paths | High | Now |
| 5 | Persistence and projections are not atomic / not lock-protected | High | Now |
| 6 | `init` can clobber existing project files and commit unrelated work under a fixed identity | High | Now |
| 7 | `migrate` removes user data and offers no restore on partial failure | High | Now |
| 8 | Constructed "incremental" updates re-parse and re-augment the entire project | Medium | Short |
| 9 | Version metadata conflicts across `pyproject.toml`, `cli/__init__.py`, `VERSION`, `VERSION.md`, runtime template | Medium | Short |
| 10 | Procedural-learning evidence is mostly representative copy heuristics, not calibrated | Medium | Medium |
| 11 | Outdated protocol/AGENTS instruction in some templates (e.g., Gemma dispatch wording) | Low | Short |
| 12 | No tracked CI, no dependency lock, no install/license files in tracked inventory | Low | Short |

---

## 2. Project Overview

**Domain.** StackMind positions itself as a "Compiler-Backed Multi-Agent Engineering Runtime" (`README.md:3-7`). It serves two audiences: (a) engineers who want to query a Python codebase as a knowledge graph (callers, impact, flow, context) without a hosted LLM; (b) teams running multi-agent sessions on a shared, Git-backed `.sync/` workspace governed by per-agent contracts and a procedural-learning loop.

**Top-level layout (`git ls-files` confirmed).**

```
stackmind/
├── AGENTS.md
├── README.md
├── VERSION, VERSION.md, CHANGELOG.md, PLAN.md
├── STACKMIND.md            # handbook
├── pyproject.toml
├── cli/                    # 17 files — Click CLI, top-level entry cli/main.py:cli
├── validators/             # 71 files — core library (knowledge, harness, experience, learning, skill, verification)
├── schemas/                # 16 JSON schemas
├── templates/              # 57 templates (incl. sync/, agents/)
├── migrations/             # 5 YAML manifests
├── docs/                   # 36 markdown documents
└── tests/                  # 50 test modules, 449 test-function defs (static grep)
```

A pre-existing untracked file `Project_Study_&_Future_Improvements.md` lives at the repo root. It was not authored or modified by this investigation.

**Tech stack.** Python ≥ 3.10, Click, PyYAML, jsonschema, rich (core). Optional `sentence-transformers` for embeddings. Dev: pytest, pytest-cov, ruff. Hatchling build, `py_modules`-style wheel packages. No external database; storage is filesystem (JSON, JSONL, SQLite-FTS5).

**Entry points.**

- CLI: `cli/main.py:13-17` — Click group with subcommands `init`, `validate`, `doctor`, `migrate`, `shutdown`, `promote`, `lock`, plus registered groups `graph`, `harness`, `analyze`, `experience`, `skill`, `learn` (`cli/main.py:393-398`).
- Console script: `pyproject.toml:48-49` — `stackmind = "cli.main:cli"`.
- Wheel packages: `["cli", "schemas", "templates", "migrations", "validators"]` (`pyproject.toml:56-57`).
- Library API: `validators/knowledge/api.py:KnowledgeAPI` is the central public object used by both CLI and tests.

**Architectural pillars (documented).**

1. Runtime Governance & Contract Layer (`CONTRACT-01`).
2. Knowledge Compiler & Code-Graph Intelligence (`KNOW-01`).
3. Harness Runtime (`HARNESS-01`).
4. Verified Procedural Learning (`LEARN-01`).

Pillar numbering and headings are inconsistent: `README.md:7` says three pillars, `:16` says four (and includes procedural learning), and `:31-34` includes `LEARN-01`. AGENTS.md repeats similar drift.

---

## 3. Repository & Component Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                              CLI LAYER (cli/)                                │
│  main.py : cli()   graph.py  harness.py  analyze.py  lock.py  init.py       │
│  shutdown.py  promote.py  migrate.py  validate.py  doctor.py  experience.py  │
│  skill.py  learn.py  contract.py  ...                                       │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │ imports
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                  DOMAIN SERVICES (validators/)                              │
│                                                                              │
│  ┌──────────── knowledge/ ─────────────────────────────────────────────┐     │
│  │ api.py (KnowledgeAPI)   contract.py   registry.py   storage.py     │     │
│  │ enricher.py  enricher_queue.py  validate.py                        │     │
│  │ compiler/   parse.py  ir.py  resolve.py  rename.py  incremental.py │     │
│  │             watcher.py  + 14 domain augmenters                     │     │
│  │ projections/  reverse_index.py  search.py  metrics.py              │     │
│  │ analysis/     runtime.py  flow.py  normalize.py  base.py          │     │
│  │ embedding/    local.py  cache.py                                  │     │
│  └────────────────────────────────────────────────────────────────────┘     │
│  ┌──────────── harness/ ──────────────────────────────────────────────┐     │
│  │ runner.py (AgentRunner)   contract_gate.py   d025_gate.py          │     │
│  │ snapshot.py  retrieval.py                                          │     │
│  └────────────────────────────────────────────────────────────────────┘     │
│  ┌──────────── experience/ ───────────────────────────────────────────┐     │
│  │ store.py  recorder.py  index.py (FTS5)  models.py                  │     │
│  └────────────────────────────────────────────────────────────────────┘     │
│  ┌──────────── learning/  skill/  verification/ ──────────────────────┐     │
│  │ cluster.py  distiller.py  miner.py  normalizer.py                 │     │
│  │ store.py  governor.py  decay.py  retriever.py  models.py          │     │
│  │ structural.py  replay.py  canary.py  pipeline.py  models.py        │     │
│  └────────────────────────────────────────────────────────────────────┘     │
└──────────────────────────────────┬───────────────────────────────────────────┘
                                   │ persists
                                   ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                    STORAGE LAYER (.sync/ — separate Git repo)               │
│                                                                              │
│  knowledge/                                                                   │
│    registry/        T0 — canonical symbol identity (birth-hashes)            │
│    nodes/           T1 — sharded JSON node documents (kind/2hex/NodeID.json)  │
│    revisions/       T1 — monotonic revision chain                            │
│    cache/           T2 — reverse_index, search, metrics, embeddings          │
│  experience/                                                                   │
│    records/         EXP-*.json  (canonical)                                  │
│    cache/           experience_index.db  (FTS5 BM25)                         │
│  skills/                                                                       │
│    manifests/       vN.json    receipts/   active/   approvals/              │
│  contracts/          YAML scope+budget                                         │
│  work-orders/        ACTIVE/BLOCKED/COMPLETED                                  │
│  runtime/            TREE.yaml, boot/*.boot.yaml, LOCK, drafts/               │
│  inbox/, outbox/                                                                   │
└──────────────────────────────────────────────────────────────────────────────┘
```

**Component count (tracked).**

| Component | Tracked files |
|---|---|
| `cli/` | 17 |
| `validators/` | 71 |
| `tests/` | 50 modules (incl. `__init__`), 449 test functions (static count) |
| `schemas/` | 16 |
| `templates/` | 57 |
| `migrations/` | 5 |
| `docs/` | 36 |
| `.sync/` | 37 tracked (despite `.gitignore:20` listing `.sync/`) |

The `.gitignore:20` entry ignores future additions, but does not untrack the 37 already-committed files inside `.sync/`. The data is therefore committed to the project repo, contradicting the design assumption that `.sync/` is a separate runtime state repo. See §6 and §11.

---

## 4. Architecture Deep Dive

### 4.1 Knowledge compiler pipeline

**Entry points.** `cli/graph.py:54-67` (build), `:105-126` (update), `:157-179` (watch). The CLI builds a `KnowledgeAPI(project_path)` and then invokes the compiler chain.

**Parser.** `validators/knowledge/compiler/parse.py:132` uses `ast.parse` (stdlib), not LibCST as the module's docstring header `:3-5` claims. `DEFAULT_EXCLUDED_DIRS` (`:17-33`) and `discover_python_files` (`:96-103`) define two distinct exclusion sets; the discoverer misses `build/` and `dist/`. Discovery uses `Path.rglob("*.py")` and is not Git-ignore-aware. Parse failures yield a `ParsedFile` with a diagnostic and no symbols (`:131-145`).

**Augmenters.** 14 domain augmenter functions are registered in `validators/knowledge/compiler/__init__.py` and orchestrated by `validators/knowledge/compiler/resolve.py:50-56` (`run_all_augmenters`). `compile_project` (`:59-107`) parses all, augments, registers the T0 registry, and resolves edges. The trick of catching `TypeError` to re-run with or without the `project_path` keyword (`:50-56`) hides internal TypeErrors; this is a diagnostic hazard, not a correctness bug.

**Identity.** `validators/knowledge/registry.py:61-63` `birth_key` is `path:qname`; `node_id_for` (`:77-79`) is `kind + 16-hex SHA256`. T0 records live in `registry/`. The registry supports alias renames (separate `rename.py` module), which is a strong feature for cross-revision continuity.

**Storage.** `validators/knowledge/storage.py` shards nodes by `kind/2hex/NodeID.json`. `symbol_document` (`:51-67`) always sets `ai: {}` for new builds. `build_node_documents` (`:70-71`) calls `symbol_document` per symbol, and each call scans all edges — an O(N·E) materialization. The revision document (`:74-84`) only contains metadata/diagnostics/parent; it is not a per-revision immutable snapshot of the graph. **The "revisions" directory is a revision counter, not a versioned graph store.**

**Writer.** `validators/knowledge/writer.py:_write_if_changed` (`:32-44`) is an atomic-per-file replacement using a fixed `.tmp` name. The composite `write` (`:52-87`) updates registry/nodes/revision sequentially — not one transaction. `_reconcile_ghosts` (`:106-189`) deletes/marks obsolete entries after new files are written, which means full-build reconciliation for renames is suspect (incremental tests confirm alias handling). Parse failures can delete previously valid symbols via reconciliation, which is undesirable unless last-known-good semantics are deliberate.

**Incremental.** `validators/knowledge/compiler/incremental.py:82-84` reads the entire prior IR, **parses the entire project**, runs **all augmenters**, builds the full new IR (`:175-188`), and only selectively writes to disk (`:206-220`). Despite the name, this is mostly selective persistence, not incremental parsing/augmentation. `_dirty_paths` (`:309-338`) computes content hashes for many file types but the watcher only reacts to `.py` changes, leaving the dirty-set often smaller than reality.

**Projections.** `validators/knowledge/projections/__init__.py:70-101` rebuilds reverse_index, search, and metrics from the whole IR. `_rewrite_projection_root` (`:113-148`) creates a sibling temp tree, deletes existing root files, then moves new files one-by-one. There is no lock or atomic generation switch; readers can observe an empty or mixed projection during rebuild. Concurrent builders can collide on the same temp tree.

**Watcher.** `validators/knowledge/compiler/watcher.py` is a polling mtime scan with debounce. Exclusions are `.sync/`, `__pycache__/`, `.pytest_cache/`, `.ruff_cache/` (`:106-114`); the parser also excludes `build/`, `dist/`, but the watcher does not. Only `.py` changes trigger; doc/config/CI changes are ignored. The watcher is not a durable daemon.

**Runtime evidence.** `validators/knowledge/analysis/runtime.py` uses `sys.setprofile`; only the current process is traceable. `RuntimeTracingProvider` (`:197-218`) supports callable, in-process `pytest.main`, or `runpy` for `python -m`; it does not handle generic shell commands. `merge_evidence_edges` (`:229-254`) keys by `source_id/target_id/relation`; unresolved `target_id=None` causes distinct targets to collapse. Importantly, runtime evidence is written to T1 but **does not rebuild T2 projections**, so `callers` queries through the reverse index can miss newly captured runtime edges until the next full or incremental build.

**Flow analysis.** `validators/knowledge/analysis/flow.py:37-64` `FlowAnalyzer` is a bounded per-file AST analysis (max 12 steps, function-name keyed summaries). It is a heuristic, not a security proof.

**Enricher.** `validators/knowledge/enricher.py` defines `KnowledgeEnricher` with `enqueue_stale_nodes` (`:372-386`) and `run_once` (`:171-265`). Backends required: `summary_backend` and `embedding_backend`. Local mode (`:365`) treats full excerpts the same as the full mode — i.e., a "local" provider is not actually local in the way the name suggests. The cache is keyed solely by content hash (`:389-391`); `_needs_enrichment` (`:267-278`) does not compare backend model identity, so a privacy-prompt change with the same content does not invalidate the cache. `_patch_ai_block` (`:428-445`) writes the AI block using an old captured node copy; concurrent writers can race. Crucially, a whole-repo search confirmed that **`enqueue_stale_nodes` is invoked only from tests, never from a production CLI/daemon**. The enqueue → production-consumer pipeline is therefore present as a library API but **not wired into the runtime**.

**Redaction note.** `_redact_secrets` (`:475-482`) preserves group 1 of each pattern. The PAT pattern (`:27`) places the entire token in group 1, so the substitution leaves the token followed by "***" instead of removing it. This was confirmed by static pattern composition; no secret data was read, exfiltrated, or tested. This is a real bug, not a security disclosure.

**Embeddings.** `validators/knowledge/embedding/local.py` is the default `all-MiniLM-L6-v2` local backend with a lazy optional import. `validators/knowledge/embedding/cache.py:CachedEmbeddingBackend` (`:34-48`) invalidates by model and dimension, but its on-disk file naming coincides with the enricher's simpler cache reader, producing inconsistent semantics. The semantic search path in `KnowledgeAPI` (`:897-947`) does its own dimension check; the rest of the code does not.

### 4.2 Knowledge API and contract layer

**Contract schema.** `schemas/contract.schema.json` requires `agent_id`, `work_order`, `scope`, `budget`. Work-order pattern `^WO-[0-9]{3}$`. `scope.write ∈ {read-write, read-only}`. `budget: {max_files_touched, max_tokens, expires_at}`. `expires_at` is a free-form string with no enforced datetime format.

**Contract loading.** `validators/knowledge/contract.py:load` (`:59-100`) prefers the consumer project's schema, falling back to the packaged one. `is_expired` (`:102-129`) **returns False for malformed dates** (`:112-115`) — i.e., a non-parseable `expires_at` is treated as not expired. `is_node_in_scope` (`:131-191`) fails closed on unknown nodes, applies explicit denies first, then allow rules via module/qname or undirected adjacency up to depth. The function rebuilds the symbol map and full adjacency **on every invocation** and uses `list.pop(0)` for BFS (O(n) per pop). This is not a sandbox; it is path-level allow/deny logic over a snapshot of the graph.

**KnowledgeAPI.** `validators/knowledge/api.py:KnowledgeAPI.__init__` (`:143-156`) optionally accepts a contract but does not auto-bind an identity. The IR is cached for the instance lifetime (`:159-163`) — there is no revision invalidation; the API can serve stale data after an external update. `lookup` (`:187`) scans active records; `filter` (`:220`) loads and enforces nodes before applying filters. `traversal` (`:279`) is BFS, using T2 reverse edges for inbound. `search` (`:365`) uses the injected embedding backend / query vector, else falls back to lexical; lexical top hits are loaded **with** the contract, so a single denied hit raises rather than being filtered. `semantic_search` (`:897-947`) **filters out denied nodes silently**, while lexical/filter/traverse fail on denial — three different policies across the read paths.

`assemble_context` (`:599-720`) is the keystone. Order of operations:

1. `lookup` and `search` seed nodes.
2. Up to 3 seeds expanded via callers/outbound `CALLS`.
3. Heuristic ranking with `skill_guidance` boosted to rank 110.
4. Token-bounded string assembly (`(len(text)+3)//4` at `:1037-1038`; not a real tokenizer).
5. Provenance includes `signature` and `ai_summary` but no explicit stale-AI warning despite `ai_stale` being tracked in metadata.

A critical bug: skill retrieval is invoked at `:658` with `retriever.retrieve_skills(query, contract=contract, limit=2)`. When the caller passes no `contract=` (which is the default for `assemble_context`), the **constructor-bound contract is not propagated**, and the retriever falls back to its default empty contract. The result is that any constructor-bound scope is silently dropped for skill injection.

Errors from skill retrieval (`:676-678`) are swallowed wholesale and the run continues. Worse, `SkillRetriever`'s constructor instantiates `SkillStore`, whose constructor (`validators/skill/store.py:31-38, 40-44`) creates `.sync/skills/manifests` and `.sync/skills/active` directories. **A supposedly read-only `KnowledgeAPI` context call therefore mutates the filesystem.**

**Staleness.** `KnowledgeAPI._is_stale` (`:765-783`) returns `False` if revision is 0, if no `.git`, or if `git status --porcelain` is clean. It only checks paths ending in `.py`. It does not compare HEAD to the recorded `git_commit`, does not hash source files, does not consider non-Python sources, and does not consider non-Git workspaces. The result: a clean committed source change can look fresh; a freshly compiled still-dirty tree looks stale. This is the opposite of a useful freshness signal.

### 4.3 Harness execution and security

**Harness entry.** `cli/harness.py:36-40` constructs an `AgentRunner` with the path, the agent, and a hard-coded `EchoLLMProvider(default_release_target=release_target)`. There is no production LLM provider exposed by the CLI.

**Run sequence.** `validators/harness/runner.py:AgentRunner.run_once` (`:224-…`) executes:

1. Load TREE/cache and registration checks.
2. Discover inbox first, else assigned ACTIVE WO (`:453-490`).
3. Assemble context **before** contract enforcement (`:239-243`).
4. `verify_pre_execution`.
5. Capture before snapshot (`:267`).
6. Search provider, LLM provider.
7. Validate schema / declared changes.
8. `_validate_staged_state`.
9. Acquire runtime lock.
10. Apply **bookkeeping** writes (`:344`).
11. **Execute proposed shell commands on the live workspace** (`:348-360`):
    ```python
    for cmd in decision.commands:
        subprocess.run(cmd, shell=True, cwd=str(self.project_path), check=True)
    ```
    No command timeout, no OS-level isolation, no restricted environment, no output capture, no rollback.
12. Capture after snapshot, compare declared/observed, post-check scope.
13. Assign verification dimensions, write experience/report/events.
14. Release lock.

The shell step is the single most dangerous line in the codebase. The agent's LLM suggests commands; the harness runs them with `shell=True` on the live project, after declaring it has validated them. Validation is against declared scope, not actual command content.

**Verification.** `:377-384` constructs `VerificationDimensions`:

```python
dimensions = VerificationDimensions(
    scope_verified=True,
    state_verified=len(staged_errors) == 0,
    code_verified=True,
    behavioral_verified=decision.status == 'completed',
    security_verified=True,
    outcome_verified=decision.status == 'completed' and not decision.blockers,
)
```

None of these are independently checked. `scope_verified` is a tautology; `code_verified` is always True; `security_verified` is always True; `behavioral_verified` and `outcome_verified` track the LLM's own completion status. The trust gate then uses these booleans to set `LEARNING_ELIGIBLE`, which feeds the procedural learning pipeline.

**Experience writeback.** `:409-429` writes the experience directly using `FileWrite` (the in-memory staging area), bypassing `ExperienceStore`'s schema validation and atomic save. Declaration mismatch lowers trust but does not block persisted completed status, nor does it specifically route the run to QA. The `try/except` wraps only `LoopSafetyError` (`:445`); provider / command / filesystem failures can propagate without a uniform result/blocker/event.

**Staging.** `:551-575` copies the entire project (excluding `.git`, bytecode, test/lint caches) into a stage directory and applies bookkeeping/report/events **only**. It does **not** execute `decision.commands` in the stage. The "validation" therefore cannot observe the effect of the commands; it observes only what the LLM declared and any non-command bookkeeping the harness itself wrote.

**Notices.** `:605-620` creates **both** a QA review notice and a direct architect completion notice, in conflict with `AGENTS.md`'s "workers must wait for QA" rule.

**Work-order lifecycle.** Inbox tasks have no `work_order_id` and often no work-order contract. The work order remains `ACTIVE` and assigned, so subsequent runs keep rediscovering it. There is no task-claim / attempt-idempotency mechanism in the harness.

**Snapshot.** `validators/harness/snapshot.py:20-30` ignores `.git`, `.sync`, `__pycache__`, caches, `.coverage`, `.vscode`, `.windsurf`, `.claude`. `:111-163` does a full walk/read/hash of included files and silently ignores `OSError`/`PermissionError`. The snapshot is a *detection* tool, not a *containment* tool: it can tell you what changed but cannot stop the change.

**D025 destructive gate.** `validators/harness/d025_gate.py` is a regex/keyword classifier. It looks for backup-looking commands *before* a destructive command and verification-looking commands *after* it. It does not prove the backup exists/succeeded, does not prove targets match, does not request human approval, and accepts a general "backup/archive" keyword (`:118`) as sufficient. The class name and supporting tests imply a security boundary; the implementation is heuristic linting. (No exploit instructions are provided here; the architectural point is that the gate is a soft classifier.)

**Retrieval.** `validators/harness/retrieval.py` defines a `SearchProvider` protocol and a per-instance `SessionSearchTool` with a query cache and a cap on max searches/cost. The evidence snippet pre-processor strips code fences and known instruction markers, but the regex sanitizer is heuristic — not a guarantee against prompt injection. `LLMRequest.retrieval_queries` are stored on the request but no iterative retrieval loop consumes them, so the field is currently unused.

### 4.4 Procedural learning

**Experience store.** `validators/experience/store.py:save_record` (`:29-42`) validates the schema, writes to `.tmp`, and `os.replace`. "Immutable" is not enforced: re-saving with the same `EXP-ID` overwrites the prior record. Malformed loads return `None` (`:44-53`); malformed records in batch are silently skipped (`:69-79`). Canonical history can therefore disappear from queries without a diagnostic.

**Experience index.** `validators/experience/index.py:61-65` opens a SQLite connection and creates cache directories and the FTS5 DB. Full rebuild is transactional (`:116-137`); upsert/delete use content-hash comparison (`:139-173`). SQL is parameterized. Mining (`learning/miner.py`) does **not** use the FTS index — it reads raw records. No tests were executed to validate that the runtime SQLite supports FTS5.

**Recorder.** `validators/experience/recorder.py:218-233` synthesises a `task_execution` success record when no actions are present. `:246` sets `is_approved = qa_verdict == APPROVED or status == COMPLETED`. `:247-256` then sets scope/state/code/security true and trusts completion for behavioural/outcome/eligibility. `:282-290` sets `tests_passed = is_approved` and treats the declared file list as observed. `:324-331` captures the first matching work order only. `:333-389` is a fallback "session workflow" record that is unconditionally all-dimensions-true, `tests_passed=True`, `LEARNING_ELIGIBLE=True` with an empty diff. This is the canonical mechanism by which a session that never produced a real edit can still produce learning-eligible records.

**Cluster.** `validators/learning/cluster.py:71` gates only on `r.learning_eligible and r.outcome == "completed"`, not on the six dimensions. Action signature is the tool plus the first command token (`:58-59`). Default `min_samples=3`, `min_similarity=0.5`. The "consensus" action set simply copies the first record's full actions (`:147-164`); N repeated episodes are not evidence of safety.

**Distiller.** `validators/learning/distiller.py` is fully deterministic — no LLM. `risk` (`:30-41`) uses substring keyword lists and **returns early on the first MEDIUM match**, so a later HIGH-risk step is not examined. `:81-91` broadens touched files to a top-level wildcard or `*` if none. `:98-104` produces generic preconditions, not measured preconditions. `:114-118` synthesises confidence/success metrics from sample count and similarity, not from calibrated correctness.

**Verification.** Three independent checks feed a weighted score (`verification/pipeline.py:69-77`, weights `.35/.35/.30`):

- **Structural** (`verification/structural.py`) checks schema, steps, tool allowlist, and a separate narrower destructive regex list. The tool allowlist is broad: `bash`, `python`, `git`, `read_file`, `write_file`, `grep`, `search`. It is not execution verification; D025 logic is essentially duplicated here.
- **Replay** (`verification/replay.py`) does `SequenceMatcher` against historical strings. `:28-37` — no linked sources → pass score `.8`. `:43-59` — missing all cited records → pass score `.75`. Else average fidelity `>= .6`. **Missing evidence fails open.**
- **Canary** (`verification/canary.py`) does not sandbox or execute. `:29-35` merely logs template variables. `:37-45` checks that target modules start with `/` or contain `..` (Windows absolute drive paths are not covered). `:47-50` requires a non-empty preconditions list but does not evaluate it. The label "Canary simulation passed" (`:55`) is misleading.

**Pipeline.** `verification/pipeline.py:69-77` writes the receipt under `.sync/skills/receipts`, not the documented `verification/` directory. The receipt is keyed by `skill_id/version/time` but not bound to immutable manifest content, environment, or model identity.

**Skill store / governor.** `validators/skill/store.py:save_version` (`:61-79`) overwrites the same `vN` manifest and stores a full active record in the active file (rather than a minimal pointer). Public `save_version` can directly activate status. `rollback_version` (`:223-265`) accepts **any** target version, constructs a new active version directly, and skips the pipeline and governor. The "immutable manifest" claim is false at the implementation level: status and confidence feedback can rewrite the same version.

`validators/skill/governor.py:159-166` accepts caller-supplied `human`/`ceo` for HIGH/CRITICAL or the presence of an approval receipt. Governance is cooperative label-based, not authenticated identity. The approval file is overwritten per skill/version; it is not bound to an immutable content hash, so rewriting the same version invalidates the original approval meaning.

**Decay / revalidate.** `validators/skill/decay.py:137-195` audits confidence, failure rate, and whether target paths still exist. It does **not** compare content/version/env. `revalidate_skill` (`:236-275`) has no STALE-only guard:

```python
pipeline_result = VerificationPipeline.verify_skill(skill, self.project_path)
if pipeline_result.passed:
    ... status = SkillStatus.ACTIVE
    self.skill_store.save_version(reactivated)
    self.skill_store.promote_version(..., skip_pipeline=True, skip_governor=True)
```

The CLI `revalidate` (`:738-770`) exposes this without an `--actor` or approval check. A high-risk candidate can therefore become active through revalidation despite the normal promotion path requiring approval. Errors are caught and printed without a non-zero exit in this path.

**Skill retriever.** `validators/skill/retriever.py:50-75` `_is_module_in_contract` compares patterns as strings. The prefix check `clean_module.startswith(pattern.rstrip(".*"))` means `auth.*` also matches `authentication/*`. A broad `src/*` allow can overlap a denied `src.secret/*` without rejection. No actual precondition/env/known-exclusion/write-mode/expiry/depth evaluation happens here. `:181-184` nevertheless returns a description that claims "match your task preconditions and active scope boundary".

**Coupled with the KnowledgeAPI bug** above, the constructor-bound contract does not constrain skill injection. Combined with the broad prefix-match and swallowed errors, **skill guidance is the most fragile trust surface in the system today**.

### 4.5 Runtime governance, init, validate, migrate

**Lock.** `cli/lock.py:96-101` uses `O_CREAT|O_EXCL` for fresh acquisition. `:105-129` treats a malformed lock as replaceable, no force needed — this is a race window. Release (`:165-182`) checks agent then unlinks without a unique ownership token. Tests named "atomic prevents TOCTOU" are sequential, not concurrent. The lock is best characterised as advisory; the runtime layer should not depend on it as a transactional primitive.

**Promote.** `cli/promote.py:50-52` `validate_boot_text` returns `[]` on a missing schema, masking schema problems. `:90-155` performs draft schema check → direct canonical write → post-write schema check → rollback only on validation errors. **No lock is acquired by the module or the CLI wrapper (`cli/main.py:247-250` calls `cli.promote.promote` directly).** No agent identity / session monotonicity / tree transition invariant is validated.

**Shutdown.** `cli/shutdown.py:367-376` updates TREE then boot, archives handoff/receipt/experience. `:183-210` clears blockers/status, copies INDEX totals, writes TREE **without** incrementing `tree_version`. The lock is inspected/released only after canonical writes (`:420`); another agent holding the lock produces only a warning. `.sync` Git add/commit (`:441-458`) uses `check=False` and swallows errors. Experience failure is swallowed (`:412-414`).

**Init.** `cli/init.py:438-446` only protects a preexisting `.sync/`. `:484-515` unconditionally renders `AGENTS.md`, `README.md`, `PLAN.md`, `CHANGELOG.md`, `VERSION`, `.gitignore` over an existing project's files. `init_git` (`:250-287`) does `git init` in root, `git add .`, then resets `.sync/` to be a separate repo, then commits **all unrelated work** under a fixed identity. Existing repos are not exempted. Final validation is structural/hash only, not full health.

**Validate.** `cli/validate.py:1425-1435` runs schema/structure/protocol/boot/sync-ref/knowledge. A schema failure does not stop later unsafe type arithmetic in boot checks (helper report refs `:1176-1193/:1223-1225`). Autofix counters (`:1350-1363`) write TREE without incrementing `tree_version`. The "fix" path (`:1365-1383`) can `git add`/commit entire `.sync/` or commit the main index untracking it, and ignores subprocess failure. `:1437-1447` removes all auto-fixable issues from the result without revalidation even on failed fixes. The knowledge validator checks registry hashes/aliases/bijection/storage; these are good invariants but not full execution validation.

**Migrate.** `cli/migrate.py:66-77` loads YAML manifests without schema validation. `:80-90` selects pending from `from_v >= current`, not a verified continuous chain. `:107-115` `add_field` overwrites existing fields. `:137-141` `remove_dir` does `shutil.rmtree` of a populated user history directory. Sequential actions (`:208-221`) have no restore on later failure. There is no lock or transaction. `migrations/v3_0_0_to_v3_1_0.yaml` `down:33-45` deletes experience records, cache, skill manifests, active, approvals, and verification — but the receipts directory is not included, so a rollback deletes canonical learning history yet may orphan receipts.

**Doctor.** `cli/doctor.py:34-52` uses major/minor compatibility heuristics. It prints runtime/schema/agent/migration info and runs `validate`, but does not verify the designated Python interpreter, dependencies, model identity, or SQLite FTS5 availability despite an environment policy.

---

## 5. Major Data & Control Flows

### 5.1 Graph build (full)

```
stackmind graph build -p PROJECT
  └─ cli/graph.py:build
       ├─ compile_project(...)              # validators/knowledge/compiler/resolve.py
       │    ├─ discover_python_files         # parse.py:rglob
       │    ├─ parse_project                 # ast.parse per file
       │    ├─ run_all_augmenters            # 14 domain augmenters
       │    ├─ _register_symbols (T0)        # registry.py (locked)
       │    └─ resolve_edges                 # Edges from imports/calls/augmenters
       ├─ write_knowledge(ir)                # writer.py
       │    ├─ _write_if_changed per node    # T1 (atomic per file)
       │    ├─ _reconcile_ghosts
       │    └─ write revision metadata       # T1 revision
       ├─ build_projections(ir)              # projections/__init__.py
       │    ├─ reverse_index
       │    ├─ search
       │    └─ metrics
       ├─ enqueue_stale_nodes (only in governed workspace)
       │    └─ enricher.EnricherJob          # no production consumer
       └─ graph stats                        # reads cached metrics
```

### 5.2 Graph context assembly (read path that is not actually read-only)

```
KnowledgeAPI(project_path).assemble_context(query, token_budget, limit)
  ├─ SkillRetriever(project_path)            # constructor mkdir .sync/skills/{manifests,active}
  │    └─ SkillStore(project_path)           # same mkdir
  ├─ retrieve_skills(query, contract=None)   # no contract propagation
  ├─ lookup/seeds via filter (contract applied)
  ├─ search lexical (contract applied; denial raises)
  ├─ expansion: callers/outbound CALLS
  ├─ heuristic rank (skill_guidance rank 110)
  └─ token-bounded text assembly
```

This is the same code path used by the harness in `run_once` (`:239-243`). Two side effects to highlight: (a) filesystem mutation; (b) absent scope enforcement on skill injection.

### 5.3 Harness run-once

```
stackmind harness run-once AGENT -p PROJECT
  └─ AgentRunner.run_once(agent)
       ├─ load TREE/cache, registration check
       ├─ inbox/assigned WO discovery
       ├─ KnowledgeAPI(...).assemble_context(...)        # before contract check
       ├─ verify_pre_execution
       ├─ before snapshot
       ├─ search + llm provider (EchoLLMProvider)
       ├─ validate schema/declared changes
       ├─ _validate_staged_state
       ├─ acquire runtime lock
       ├─ bookkeeping writes (T0/T1, events, report)
       ├─ subprocess.run(cmd, shell=True)  x N           # LIVE workspace
       ├─ after snapshot, declared/observed diff
       ├─ VerificationDimensions(...)                    # derived, not tested
       ├─ recorder.write(...)                            # direct FileWrite, bypasses ExperienceStore
       └─ release lock
```

### 5.4 Skill activation paths

```
promote (normal):  draft → pipeline (structural/replay/canary) → governor (LOW auto; MEDIUM actor;
                   HIGH/CRITICAL human/ceo) → active
revalidate:        any skill (incl. STALE/FAILED) → pipeline → save_version + promote_version
                   (skip_pipeline=True, skip_governor=True) → active
rollback:          any target version → construct new active version, no prior-active requirement,
                   no pipeline, no governor
```

### 5.5 Init → Migrate → Shutdown

```
init  → render AGENTS/README/PLAN/CHANGELOG/VERSION/.gitignore  (overwrites existing)
       git init root/.sync  →  git add .  →  reset .sync  →  commit everything
       (fixed identity, unrelated work committed)

migrate  → load YAML manifests (no schema)  →  from_v >= current  →  add_field (overwrite)
            remove_dir (rmtree)  →  sequential actions  →  no restore on failure

shutdown  → preflight handoff/inbox  →  archive handoff/receipt/experience
            update TREE (no tree_version increment)  →  commit .sync
            lock inspected/released only after canonical writes
```

---

## 6. Current Design vs Intended Design

The following table records discrepancies between documented intent and implementation that the investigation confirmed. Each row gives the claim, the source, and what the code actually does.

| # | Claim (source) | Reality (evidence) |
|---|---|---|
| 1 | "Contracts fail closed" (`AGENTS.md:227`) | `validators/harness/contract_gate.py:16-36` swallows every exception and returns `None`; no contract → allow. `validators/knowledge/contract.py:is_expired` returns False for malformed `expires_at`. |
| 2 | "Contract budgets enforced" (`AGENTS.md:244`) | `max_tokens` is declared in schema; production Python search finds it only in tests. |
| 3 | "Workers must wait for QA" (`AGENTS.md:259`) | `validators/harness/runner.py:605-620` writes **both** a QA review and an architect completion notice. |
| 4 | "Experiences captured only when all 5 verification dimensions pass" (`AGENTS.md:358`) | Recorder builds 6 dimensions and can fall back to a session_workflow record with empty diff and all true. |
| 5 | "Every run event captured; every error blocker" (`AGENTS.md:392-394`) | Outer `try/except` wraps only `LoopSafetyError`; many failure modes escape without structured events. |
| 6 | "Immutable manifest storage" (`README.md:144-148`) | `save_version` overwrites the same `vN`; status feedback rewrites the same version; rollback constructs a new active version directly. |
| 7 | "Skill receipts under verification/" (`README.md:148-148`) | `verification/pipeline.py:69-77` writes to `.sync/skills/receipts`. |
| 8 | "Read-only context query" (`STACKMIND.md`) | `KnowledgeAPI.assemble_context` instantiates `SkillStore`, which creates `.sync/skills/{manifests,active}` directories. |
| 9 | "Harness transaction guarantees" (`STACKMIND.md`) | `writer.py` updates registry/nodes/revision sequentially; `_rewrite_projection_root` deletes then moves files one-by-one; `_validate_staged_state` does not execute `decision.commands`. |
| 10 | "14 specialized compilers" (`STACKMIND.md:238`) | 14 augmenters are present, but `STACKMIND.md:254` enumerates 15 and references an absent `CbmCompiler`. No tracked file matches `CBMCompiler`, `CompilerFrontend`, `libcst`, or `jedi`. |
| 11 | "Jedi resolver" (`STACKMIND.md:220`) | Parser is `ast.parse`; header docstring of `parse.py:3-5` says "LibCST preferred" but implementation is stdlib. |
| 12 | "Scope-bounded skills into agent prompts" (`README.md:34`) | KnowledgeAPI passes `contract=None` to `retrieve_skills`; constructor-bound scope is dropped; prefix allow can overlap deny. |
| 13 | "VERSION 3.1.0 badge" (`README.md:10`) | `pyproject.toml:7` is 3.1.1, `cli/__init__.py:3` is 3.1.1, `VERSION:1` is 3.1.0, `VERSION.md:3` is 3.0.0, runtime template `:5` is 3.1.0, `claude.boot.yaml:release:1.0.0`. |
| 14 | "Three pillars" / "Four pillars" | `README.md:7` says three; `:16` says four. |
| 15 | "Phase 0 exit gates done" (`PLAN_PROCEDURAL_LEARNING.md:73-86`) | `:7` says "NOT STARTED"; later lines mark all phases DONE. Source contradicts gating claims. |
| 16 | "Gemma sends verdict to requesting agent" (`templates/sync/agents/gemma.agent.template.md:69-75`) | Newer global protocol mandates env check/tests/approval-to-architect. Template drifts from AGENTS. |
| 17 | ">80% test coverage" (Gemma template `:83-84`) | Static policy claim; no coverage measurement taken during this investigation. |
| 18 | "Tests 449 Passing" (`README.md:11`) | Static grep over `tests/` finds 449 test-function definitions; no tests were run. |
| 19 | "MIT LICENSE" (`README.md:12`) | No `LICENSE` file in tracked inventory. |
| 20 | "D025 destructive safeguards" (`README.md:33`) | `d025_gate.py` is regex/keyword linting; does not prove backup existence, target identity, or human approval. |
| 21 | "Runtime safe contract-gated retrieval" (`STACKMIND.md`) | `lexical` raises on denial; `semantic_search` filters silently; `filter` raises; `traversal` raises. Inconsistent denial semantics. |
| 22 | ".sync/ is a separate repo" (`README.md:151-152`) | `37` files in `.sync/` are tracked by the project Git; `.gitignore:20` lists `.sync/` for future ignores but does not untrack existing entries. |

---

## 7. AI / Agent Architecture

**Role model.** `AGENTS.md` defines roster roles: `claude` (Senior Architecture & Agent Manager), `codex` (Worker), `gemini` (Worker), `gemma` (Reviewer), `local-llm` (offline worker). The most recent commit (6fd5647) is `docs(agents): formalize Local-LLM, Gemma, and Codex roles and environment discipline`. The IDE-01 rule (`AGENTS.md:118`) forbids in-process subagent spawning across roster roles; cross-role delegation is file-based via `.sync/inbox/<agent>/` and Work Orders.

**Process model today.** The CLI's `harness run-once` constructs an `AgentRunner` with `EchoLLMProvider` (a deterministic demo provider, `validators/harness/runner.py:150-189`). There is no production LLM provider exposed by the CLI. The `cli/harness.py:36-40` line hard-codes the Echo provider. To run with a hosted LLM, a code change is required.

**Contract enforcement.** `AgentContract` (validators/knowledge/contract.py) is a dataclass; the `KnowledgeAPI` only consults it if explicitly passed. `assemble_context` passes `contract=None` to `retrieve_skills`, dropping constructor-bound scope. `cli/graph.py:214-257` does not pass a contract. The harness calls `assemble_context` without a contract argument and runs shell commands after.

**Trust derivation.** Verification booleans are derived from the LLM's own completion status, not from independent checks. The `Recorder`'s session_workflow fallback creates learning-eligible records with empty diff. `VerificationDimensions.code_verified = True` is set unconditionally. This means a session that touched no code and produced no diff can still produce a record that the trust gate later treats as "verified".

**Authority boundaries.**

- **CLI top level** (`cli/main.py`) is a thin Click shell.
- **Domain services** (`validators/`) implement business logic.
- **Storage** (`.sync/`) is filesystem-backed; the `Lock` is advisory.
- **LLMProvider / SearchProvider / SummaryBackend / EmbeddingBackend** are protocols (`validators/harness/runner.py`, `validators/knowledge/api.py`, `validators/knowledge/enricher.py`).

**Cross-layer coupling observed.** `embedding/local.py` imports a private token helper from `KnowledgeAPI` (a private-named cross-layer import). This breaks module layering and complicates the in-process runtime.

**AI summary risks.** The `enricher` writes an `ai` block into each node document. Three correctness hazards are present: (a) cache key ignores model/privacy/prompt identity; (b) `_patch_ai_block` writes against a stale captured copy with no reread; (c) full graph writes clear AI blocks even for unchanged enriched symbols because the storage builder always re-emits. These compound the staleness problem described in §4.2.

**Privacy/regex redaction.** The PAT pattern in `validators/knowledge/enricher.py:27` is composed so the entire token falls in capture group 1, then `pattern.sub(r'\1***', redacted)` preserves the token followed by `***` rather than removing it. This was confirmed by static pattern composition; no secret data was read, exfiltrated, or tested.

**Where AI sits today.** The runtime has the *shape* of a multi-agent system (inbox, outbox, work orders, contracts, handoff, learning), but the only LLM available to the CLI is the deterministic Echo provider, the search provider is a `SessionSearchTool` with a regex snippet sanitizer, and the embedding backend defaults to a local sentence-transformer. Wiring a real hosted LLM is a *configuration* change plus provider implementation; the harness, contract, and verification paths are already in place but currently weak.

**Recommended AI responsibility boundaries.**

- LLM may *suggest* commands and file changes; only the harness may *execute* them.
- A separate, non-LLM verifier must own `code_verified`, `security_verified`, `behavioral_verified`.
- AI summaries in the graph must carry content-bound provenance (model identity + prompt version + content hash + privacy policy).
- Skill activation must require an authenticated actor, not a label.

---

## 8. Engineering Quality Assessment

| Dimension | Assessment | Notes |
|---|---|---|
| **Modularity** | Good | Clear package boundaries; protocols for backends; `KnowledgeAPI` is a reasonable facade. |
| **Determinism** | Mixed | IR is sorted and content-hashed; `node_id_for` is SHA-256; but the enricher/embedder caches are not fully content+model+prompt-keyed; projections have non-atomic swap. |
| **Provenance** | Strong in IR, weak in AI | Every node carries evidence; AI block lacks model/privacy/prompt identity. |
| **Concurrency** | Weak | Lock is advisory; per-file atomic `.tmp` is fixed-named; `_rewrite_projection_root` deletes then moves; `_validate_staged_state` and `subprocess.run` are not isolated; incremental writer is not transactional. |
| **Atomicity** | Weak | Writer updates registry/nodes/revision sequentially; bookkeeping writes happen before live effects; rollback is not modelled. |
| **Error handling** | Inconsistent | Outer `try/except` wraps only `LoopSafetyError`; many swallowed failures; some blocks return `None` on malformed input rather than failing closed. |
| **Type safety** | Reasonable | Dataclasses used for IR, contracts, dimensions, jobs, skills. |
| **Configuration** | Mixed | `enricher.yaml`, `TREE.yaml`, contract YAML, `claude.boot.yaml`; runtime template differs from package version. |
| **Developer ergonomics** | Reasonable | Click grouping, Rich output, schema-validated contracts, dataclass IR. |
| **Documentation** | Outdated in places | `STACKMIND.md` includes a 15-compiler list and absent `CbmCompiler`; `PLAN_PROCEDURAL_LEARNING.md` is contradictory; some templates are stale. |
| **Examples** | Decent but inconsistent with code | `README.md` Quick Start matches what `graph build/update/context` do; `STACKMIND.md` example contract uses different shape than the schema. |
| **Logging/observability** | Basic | JSONL experience and report; no correlation IDs across stages; no attempt-id; events not uniformly recorded. |
| **Performance** | Adequate for moderate projects | O(N·E) symbol materialization; full re-parse on `incremental`; rebuilding all augmenters every update. |
| **Security boundaries** | Weak | `subprocess.run(cmd, shell=True)` on live project; D025 is regex linting; redaction has a bug. |

---

## 9. Testing & Reliability Assessment

**Test inventory (static, not executed).** 50 test modules, 449 test-function definitions. README badge claims "449 Passing". This is the static count, not an observed run.

**Coverage of invariants.**

- `tests/test_harness.py` exercises static provider/search fixtures and completion paths. The test at `:59-133` asserts that an unchanged source path is *not* modified, but the harness's `subprocess.run(shell=True)` is not tested in a way that proves containment. Invalid payload persistence is checked at `:165`. The CLI echo inbox test is at `:280`.
- `tests/test_harness_contract.py` uses fake `output` lists to simulate commands, not real unauthorized mutations. Expiry, read-only, declared scope, and D025 are tested at the unit level.
- `tests/test_scope_violation_e2e.py` exercises `KnowledgeAPI` with explicit contracts; the "blocked and logged" test asserts the returned result, and a separate test checks the JSONL. It does not prove filesystem rollback.
- `tests/test_phase0_trust_and_verification.py` runs the lock acquisition sequentially. The trust gate is supplied booleans, so it tests the gate, not the upstream derivation of those booleans.
- `tests/test_phase7_retrieval_integration.py` activation helper (`:70-72`) skips pipeline/governor. Scope tests are exact `auth` vs `billing`; broader prefix-overlap is not tested. The `assemble_context` test (`:173-192`) does not pass a constructor-bound contract, missing the propagation bug.
- `tests/test_knowledge_api.py` exercises alias rename, revision stamps, no-source-read guard, and token truncation. `:173-184` tests the stale dirty-Python case only. The read-only test (`:187-203`) checks the latest revision unchanged but not the full filesystem, missing the `SkillStore` mkdir side effect.
- `tests/test_integration_e2e.py` synthesises a small project and exercises domain compilers/cycle/deadcode/impact.
- `tests/test_runtime_tracer.py` covers tracer/normalization/merge unit behaviours; it does not roundtrip into T2 callers.
- `tests/test_migrate.py` covers simple rollback and missing target; populated-history rollback and concurrent failure are not covered.

**Observed coverage gaps.**

- Real subprocess containment for the harness.
- Constructor-bound contract propagation into `retrieve_skills`.
- Skill `revalidate` and `rollback` denial.
- Projection rebuild atomicity under concurrent reads.
- `init` overwriting an existing project; `migrate` partial-failure restore.
- End-to-end "AI summary written → summary read" roundtrip with model/prompt identity.
- FTS5 availability on the runtime SQLite.
- Installed-wheel operation (`stackmind` console script).
- Read-only `assemble_context` (no skill mkdir).

**No CI configuration in tracked inventory.** No `.github/workflows/`, no `tox.ini`, no `Makefile`. The README badge is not self-validating.

**No tests were executed during this investigation.** The user explicitly forbade running stateful commands; tests touch the filesystem and `.sync/` and would mutate project state.

---

## 10. Architectural Risks

Risks are ranked by severity, then by time horizon, then by ease of exploitation.

### 10.1 Critical

**R1. Live shell execution of LLM-suggested commands without isolation or rollback.** `validators/harness/runner.py:348-360` runs `subprocess.run(cmd, shell=True, cwd=str(self.project_path), check=True)` against the live project. There is no timeout, no environment scrubbing, no output capture, no rollback. The D025 gate is a soft classifier, not a containment. Any hosted LLM the user wires in becomes a remote code execution surface against the user's project. *Horizon: now. Difficulty: low. Blast radius: total workspace loss, including `.sync/`, on a destructive command.*

**R2. Verification dimensions are derived, not measured.** `validators/harness/runner.py:377-384` sets `code_verified=True`, `security_verified=True`, `behavioral_verified=decision.status == 'completed'`, and `outcome_verified=... and not decision.blockers`. There is no static analyzer, no test runner, no security check. The trust gate then uses these booleans to set `LEARNING_ELIGIBLE`, which feeds the procedural learning pipeline. The architectural risk is that learning becomes a function of the LLM's confidence, not of the code's correctness. *Horizon: now. Difficulty: low. Blast radius: false trust in unverified patterns.*

**R3. Skill revalidation and rollback bypass approval.** `validators/skill/decay.py:236-275` revalidates any skill and on pass activates it via `promote_version(..., skip_pipeline=True, skip_governor=True)`. `validators/skill/store.py:rollback_version` (`:223-265`) constructs a new active version directly with no pipeline/governor. The CLI exposes both. Combined with R2, a STALE/FAILED skill that has been retried enough times can become ACTIVE without independent verification. *Horizon: now. Difficulty: low. Blast radius: unsafe skills in agent prompts.*

### 10.2 High

**R4. KnowledgeAPI context assembly mutates the filesystem.** `validators/knowledge/api.py` instantiates `SkillRetriever`, which instantiates `SkillStore` (validators/skill/store.py:31-44), which creates `.sync/skills/{manifests,active}` on construction. A supposedly read-only `assemble_context` therefore touches the filesystem. *Horizon: now. Difficulty: low. Blast radius: hidden side effects, dirty `.sync/`.*

**R5. Persistence and projections are not atomic.** `validators/knowledge/writer.py` updates registry/nodes/revision sequentially. `validators/knowledge/projections/__init__.py:_rewrite_projection_root` deletes existing files then moves new files one-by-one. Concurrent readers see empty or mixed projections. *Horizon: now. Difficulty: medium. Blast radius: graph corruption, lost context, dropped learning evidence.*

**R6. `init` can clobber existing files and commit unrelated work.** `cli/init.py:484-515` renders AGENTS/README/PLAN/CHANGELOG/VERSION/.gitignore unconditionally; `init_git:250-287` commits all unrelated work under a fixed identity. There is no exemption for existing repos. *Horizon: now. Difficulty: low. Blast radius: loss of user work, messy history.*

**R7. `migrate` removes user data and offers no restore.** `cli/migrate.py:137-141` `remove_dir` is `shutil.rmtree`; actions are sequential with no restore on later failure. The v3.0.0→v3.1.0 `down` deletes experience/cache/skill/approvals/verification but not receipts. *Horizon: now. Difficulty: low. Blast radius: irreversible data loss during a routine upgrade.*

**R8. Constructor-bound contract is silently dropped for skills.** `validators/knowledge/api.py:658` calls `retriever.retrieve_skills(query, contract=contract, limit=2)` with the method argument; when callers pass `None` (the default for `assemble_context`), the constructor-bound contract is not used. The skill retriever's prefix-match (`startswith(pattern.rstrip(".*"))`) further weakens scope. *Horizon: now. Difficulty: low. Blast radius: cross-scope skill leakage.*

### 10.3 Medium

**R9. "Incremental" updates are not incremental.** `validators/knowledge/compiler/incremental.py:82-84` re-parses the entire project and runs all augmenters; only the disk write is selective. The runtime tracer and flow analyzer do not refresh T2 projections, so reverse-index-based callers miss new runtime/flow evidence until the next full/incremental build. *Horizon: short. Difficulty: medium. Blast radius: stale reverse index, missed evidence.*

**R10. Denial semantics are inconsistent.** `lexical` search raises on denied hits, `filter` raises, `traversal` raises, but `semantic_search` (`:897-947`) silently filters denied nodes. The `assemble_context` heuristic may then include nodes that the contract would otherwise block. *Horizon: short. Difficulty: medium. Blast radius: contract bypass via search path.*

**R11. Project versioning metadata conflicts.** `pyproject.toml:7` 3.1.1; `cli/__init__.py:3` 3.1.1; `VERSION:1` 3.1.0; `VERSION.md:3` 3.0.0; runtime template `RUNTIME_VERSION.template:5` 3.1.0; `claude.boot.yaml:release:1.0.0`. *Horizon: short. Difficulty: low. Blast radius: user confusion, broken doctor compatibility checks.*

**R12. Inconsistent policy in procedural learning.** `validators/experience/recorder.py:333-389` produces session_workflow records with empty diff and all-dimensions-true. `validators/learning/cluster.py:71` only checks `learning_eligible and outcome == 'completed'`, not the six dimensions. `validators/learning/distiller.py:30-41` returns early on the first MEDIUM risk. N repeated episodes are not evidence of safety. *Horizon: short. Difficulty: medium. Blast radius: spurious skills promoted.*

**R13. Destructive command "backup" claim is heuristic.** `validators/harness/d025_gate.py` is a regex/keyword classifier. It does not prove a backup exists, was successful, or targets the right files. The general "backup/archive" keyword is accepted. *Horizon: short. Difficulty: low. Blast radius: data loss under false-positive "pass".*

**R14. PAT redaction leaves the token visible.** `validators/knowledge/enricher.py:_redact_secrets` (`:475-482`) preserves group 1 of each pattern. The PAT pattern (`:27`) places the entire token in group 1, so `pattern.sub(r'\1***', redacted)` leaves `<token>***` rather than removing the token. *Horizon: short. Difficulty: low. Blast radius: secret exposure in cached AI blocks, search, and graph context.*

**R15. Read-only claim violated by skill store mkdir.** The same code path that the harness uses for context assembly instantiates `SkillStore`, which creates directories. The "read-only" guarantee advertised in `STACKMIND.md` is not honoured. *Horizon: short. Difficulty: low. Blast radius: dirty Git status on supposedly read-only operations.*

**R16. Procedural learning evidence is heuristic, not measured.** Cluster, distiller, structural, replay, and canary verifications do not execute in a sandbox, do not bind content/version/env, and fail open on missing evidence. *Horizon: short. Difficulty: medium. Blast radius: unsafe skills promoted.*

### 10.4 Low

**R17. `.sync/` is tracked by the project Git despite `.gitignore:20`.** 37 files are committed; the ignore does not untrack them. The design assumes a separate repo, but in tracked form, the project repo carries runtime state. *Horizon: short. Difficulty: low. Blast radius: confusion, larger diffs, leakage of `lock`/TREE state.*

**R18. No tracked CI, no dependency lock, no LICENSE in tracked inventory.** Drift from the README's claims. *Horizon: short. Difficulty: low. Blast radius: install/upgrade surprises, licence uncertainty.*

**R19. Template drift.** `templates/sync/agents/gemma.agent.template.md:36` retains "MESSAGES TO DISPATCH" wording despite newer protocol conventions. *Horizon: short. Difficulty: low. Blast radius: agent misbehaviour on first init.*

**R20. Coverage claim (>80%) is unverified.** No coverage measurement was taken; the existing `.coverage` file is not evidence of current quality. *Horizon: short. Difficulty: low. Blast radius: bad decisions based on unmeasured coverage.*

**R21. Failure journaling is incomplete.** The harness's outer `try/except` wraps only `LoopSafetyError`; provider/command/filesystem failures can escape without a structured event. No correlation IDs across stages. *Horizon: short. Difficulty: medium. Blast radius: lost forensic trail.*

**R22. WATCH + UPDATE queue loss.** `validators/knowledge/compiler/watcher.py:67-79` clears pending state before the runner executes; a runner failure loses the queued batch and raises. *Horizon: short. Difficulty: low. Blast radius: missed updates.*

**R23. Inline pre-fix arithmetic in validate.** `cli/validate.py` schema failure does not stop later unsafe type arithmetic in boot checks (`:1176-1193/:1223-1225`). *Horizon: short. Difficulty: low. Blast radius: misleading validate output.*

**R24. Augmented T0 nodes can lose their AI summary on full rebuild.** Because `storage.symbol_document` always sets `ai: {}` and full builds always rewrite, an unchanged enriched symbol's AI block is lost. *Horizon: short. Difficulty: low. Blast radius: lost AI summary state.*

---

## 11. Technical Debt

This section groups the debt by *category* and gives representative evidence.

**Trust debt (most serious).** Verification booleans are derived, not measured (R2). Skill revalidation and rollback bypass approvals (R3). Denial semantics are inconsistent (R10). Procedural learning is heuristic, not measured (R12, R16). Destructive command gate is regex linting (R13).

**State-consistency debt.** Writer is not transactional (R5). Projections swap is not atomic (R5). Lock is advisory (`cli/lock.py`). Parser and watcher exclusion sets diverge. `_validate_staged_state` does not execute the proposed commands, so the staging claim is partial.

**Boundary debt.** Harness executes live shell commands (R1). KnowledgeAPI "read-only" path mutates filesystem (R4, R15). Constructor-bound contract is dropped for skills (R8). `init` overwrites existing project files (R6). `migrate` removes user data without restore (R7). Destructive gate accepts "backup" keyword without proof (R13). PAT redaction bug (R14).

**Documentation debt.** Pillar count drift (Three vs Four). Compiler count drift (14 vs 15, with CbmCompiler absent). Plan/status contradictions in `PLAN_PROCEDURAL_LEARNING.md`. STACKMIND.md example contract shape does not match schema. Template drift in Gemma agent. README badge 3.1.0 vs package 3.1.1. "Immutability" claims false in code.

**Test debt.** Constructor-bound contract propagation not tested. Skill revalidate/rollback not tested for denial. Live shell containment not tested. Concurrent projection rebuild not tested. FTS5 availability not tested. Installed-wheel behaviour not tested. PAT redaction not tested for bare PATs. Read-only `assemble_context` not tested.

**Observability debt.** No correlation IDs across stages. No attempt-ID for WO discovery. No uniform failure journaling. Watcher clears queue before runner.

**Performance debt.** O(N·E) symbol materialization (`build_node_documents`). Full re-parse on `incremental`. Runtime evidence not surfaced in T2. Enricher not consumed in production.

**Privacy debt.** PAT redaction preserves token (R14). `embedding/local.py` cross-layer private import. `_patch_ai_block` writes against stale captured copy. `User` identity is a label, not an authenticated principal.

---

## 12. Future Improvement Opportunities

The following are the candidate opportunities identified by the investigation. They are prioritized in §13.

1. Staged execution: run proposed commands in a copy of the project, diff declared vs. observed, then apply to live only on success. Add a journaled rollback path.
2. Identity-bound contracts: bind `agent_id` to an authenticated session, not to a label. Track contract use by `agent_id` + WO ID.
3. Constructor-bound contract propagation: fix `KnowledgeAPI` to pass the constructor-bound contract to `retrieve_skills`.
4. Skill activation consolidation: every activation path (promote, revalidate, rollback, save_version direct activation) must run pipeline + governor; rollback must target a previously-active version; approvals must be content-bound.
5. Independent verification: replace derived booleans with a separate, non-LLM verifier (test runner, static analyzer, declared-and-observed scope check, declared-and-observed command check).
6. Read-only `assemble_context`: do not instantiate `SkillStore` on read; defer skill loading to the writer/CLI path.
7. Atomic projections: generation version + atomic temp directory swap + read-side manifest guard.
8. Transactional writer: update registry/nodes/revision in one transaction or a single guarded batch; ensure rollback on partial failure.
9. Init preservation: detect existing project, refuse to overwrite, or take a snapshot; do not commit unrelated work.
10. Migrate safety: add preview, backup, dry-run, and restore on failure; treat destructive actions as opt-in.
11. Lock hardening: per-agent unique ownership tokens; treat malformed lock as a critical alert, not silently replaceable; consider OS-level file locking.
12. Staleness: compare HEAD to recorded `git_commit`; hash source files; account for non-Python sources and non-Git workspaces; surface staleness in context text.
13. Persistent queue with attempt ID: enricher queue + watcher queue + WO attempt ID + event correlation.
14. Incremental parsing and augmentation: only re-parse changed files; cache augmenter results keyed by source hash; refresh T2 projections from new edges.
15. Cross-file state consistency test suite: properties, fuzzing, crash-safety.
16. True procedural learning: calibrate confidence from real outcomes, not from sample count + similarity; fail closed on missing evidence; require env/model/content identity in receipts.
17. Documentation truth: align pillars, compiler list, version metadata, sample contract shape, and templates with the actual code; add a docs-vs-code CI check.
18. Test environment: pinned CI with a real LLM provider stub, real SQLite FTS5, real wheel install; coverage gates enforced.
19. Privacy/redaction: review and fix every redaction pattern; test bare PATs; document the policy.
20. Failure journal: every stage emits structured events with a correlation ID; harness catches and journals failures uniformly.
21. Authenticated identity: replace label-based governance with a real identity provider (keyring, OAuth, SSO, etc.) and signed contracts.
22. AI summary cache: key by content hash + model + prompt version + privacy policy; verify on read.
23. Concurrency tests: lock contention, projection rebuild under read, two agents running simultaneously.
24. Adversarial tests: prompt-injection via search results, hostile inbox tasks, malicious WO payloads.
25. Performance baseline: a reproducible benchmark over a representative Python project; track over time.

---

## 13. Prioritized Improvement List

The following 15 improvements are the highest-leverage, in priority order. Each row gives the motivation, current problem, expected benefit, priority, effort, dependencies, risk, and suggested direction.

### I1. Staged execution with diff-then-apply for harness

- **Improvement:** Run proposed shell commands in a copy of the project; diff declared vs. observed; apply to the live workspace only on success. Journal every step with a correlation ID.
- **Motivation:** R1. The harness currently runs `subprocess.run(cmd, shell=True)` on the live workspace after declaring it has validated.
- **Current Problem:** No isolation, no rollback, no output capture, no timeout. Staging exists but does not execute the proposed commands.
- **Expected Benefit:** Containment of LLM-suggested actions; ability to roll back; ability to verify declared vs. observed.
- **Priority:** Critical.
- **Effort:** High (2–3 weeks for one engineer).
- **Dependencies:** Lock hardening (I6), snapshot extension, decision schema refinement.
- **Risk:** Performance overhead per command; behaviour differences between copy and live (e.g., environment variables).
- **Suggested Direction:** Refactor `_validate_staged_state` to actually execute `decision.commands` in the stage dir; add a `post_check` that diffs declared/observed files; apply deltas to live on success; add a journal file `.sync/runtime/journal/<run_id>.jsonl` with each step's outcome.

### I2. Independent, non-LLM verification

- **Improvement:** Replace the derived `VerificationDimensions` with measured values: declared scope vs. observed scope, declared commands vs. observed commands, declared files vs. observed files, declared tests vs. executed tests (if a test runner is configured).
- **Motivation:** R2. Current booleans are derived from the LLM's own completion status.
- **Current Problem:** Trust gate treats LLM completion as proof. Learning is therefore a function of LLM confidence, not code correctness.
- **Expected Benefit:** Real evidence feeds `LEARNING_ELIGIBLE`; spurious skills stop being created.
- **Priority:** Critical.
- **Effort:** Medium (1 week).
- **Dependencies:** I1 (staged execution), test runner integration.
- **Risk:** Tests may be missing or slow; need a "no tests" path that fails closed.
- **Suggested Direction:** Introduce a `Verifier` protocol with `verify(run_record, staged_state) -> VerificationDimensions`. Default implementation uses declared/observed diff + optional test runner. Make `code_verified` default to False.

### I3. Single guarded skill state-transition path

- **Improvement:** Every skill activation (promote, revalidate, rollback, save_version direct activation) must go through one transition function. Rollback must target a previously-active version. Approvals must be content-bound.
- **Motivation:** R3. `revalidate` and `rollback` bypass approvals.
- **Current Problem:** Trust surface is fragmented; a STALE/FAILED skill can become ACTIVE.
- **Expected Benefit:** One transition authority; no path bypasses pipeline/governor; approvals are immutable evidence of the manifest they approved.
- **Priority:** Critical.
- **Effort:** Medium (1 week).
- **Dependencies:** Authenticated identity (I11).
- **Risk:** Some existing skill activations will fail under the new path; need migration.
- **Suggested Direction:** Introduce `SkillStore.activate(skill_id, version, actor, receipt=None)` and route all callers through it. Compute approval file path as `<sha256(manifest+actor+time)>.json`. Remove `skip_pipeline`/`skip_governor` from public API.

### I4. Constructor-bound contract propagation in KnowledgeAPI

- **Improvement:** `KnowledgeAPI.assemble_context` must propagate the constructor-bound contract to `retrieve_skills` and to all other read paths.
- **Motivation:** R8. The constructor-bound contract is silently dropped for skills.
- **Current Problem:** `lexical` raises on denial, `filter` raises, `traversal` raises, but `semantic_search` filters silently; skills are returned unscoped.
- **Expected Benefit:** Skills are filtered by the same scope as the rest of the context.
- **Priority:** Critical.
- **Effort:** Low (1–2 days).
- **Dependencies:** Denial semantics unification (I4a, in same change).
- **Risk:** May break existing tests that rely on the loose semantics.
- **Suggested Direction:** Resolve the contract once at API construction; pass it as a default to all read methods; unify denial semantics (filter, not raise) across lexical, semantic, filter, traversal.

### I5. Read-only `assemble_context`

- **Improvement:** Do not instantiate `SkillStore` on read; defer skill loading to the writer/CLI path.
- **Motivation:** R4, R15. `SkillStore.__init__` creates directories.
- **Current Problem:** A read-only context call mutates the filesystem; the harness picks up dirty state.
- **Expected Benefit:** True read-only guarantee; cleaner Git status; safer test setup.
- **Priority:** High.
- **Effort:** Low (1 day).
- **Dependencies:** None.
- **Risk:** Callers that depend on side effects (none in the public API).
- **Suggested Direction:** Refactor `KnowledgeAPI.assemble_context` to take an explicit `skill_store` parameter (default `None`). Document the read-only contract. Add a test that asserts no `.sync/skills/` directory is created during `assemble_context`.

### I6. Atomic projections and transactional writer

- **Improvement:** Generation-versioned projection swap (write new tree to a sibling temp dir, atomic rename, bump generation, readers use generation); one guarded transaction for writer (`registry`, `nodes`, `revision`).
- **Motivation:** R5. Reader/builder race; partial writes.
- **Current Problem:** `_rewrite_projection_root` deletes then moves; readers see empty/mixed state.
- **Expected Benefit:** Concurrent readers always see a consistent generation; writers are atomic.
- **Priority:** High.
- **Effort:** Medium (1 week).
- **Dependencies:** Lock hardening (I7).
- **Risk:** Generation swap must be atomic on Windows (`os.replace` is fine for files, but the directory rename needs care on Windows).
- **Suggested Direction:** Use a `generation.json` file as the read-side manifest. Writer creates `gen<N+1>/`, then `os.replace` the manifest. Reader always loads the manifest first, then reads from that directory. Writer holds the lock for the full registry+node+revision sequence.

### I7. Lock hardening

- **Improvement:** Per-agent unique ownership tokens; treat malformed lock as a critical alert; consider OS-level file locking (`msvcrt` / `fcntl`); integrate the lock into all canonical writes.
- **Motivation:** R5. The lock is advisory.
- **Current Problem:** `release_lock` checks agent then unlinks without a unique token; malformed lock is silently replaced.
- **Expected Benefit:** True serialization of canonical writes; safer shutdown/migration/init.
- **Priority:** High.
- **Effort:** Medium (1 week).
- **Dependencies:** None.
- **Risk:** Windows file locking semantics differ; tests must cover both OSes.
- **Suggested Direction:** Use a random 32-byte token written into the lock file. `release_lock` reads the file, compares tokens, then unlinks. Malformed lock → raise unless `--force`. Add `flock`-style OS locking on the LOCK file.

### I8. Init preservation and migrate safety

- **Improvement:** Detect existing project; refuse to overwrite unless `--force`; do not commit unrelated work; for `migrate`, add preview, backup, dry-run, and restore on failure.
- **Motivation:** R6, R7.
- **Current Problem:** `init` overwrites files and commits all work; `migrate` does `shutil.rmtree` without restore.
- **Expected Benefit:** No accidental loss of user data; safe upgrades.
- **Priority:** High.
- **Effort:** Medium (1 week).
- **Dependencies:** None.
- **Risk:** Migration rollback logic must be tested against populated history.
- **Suggested Direction:** `init` checks for existing `AGENTS.md`/`README.md`/`PLAN.md`/`CHANGELOG.md`/`VERSION`/`.gitignore` and aborts unless `--force`. `migrate` takes a `--backup-dir` and creates a tarball before each step; on failure, restores the last known good state.

### I9. Staleness and AI cache correctness

- **Improvement:** `_is_stale` compares HEAD to recorded `git_commit`; hashes source files; accounts for non-Python sources and non-Git workspaces. Enricher cache keyed by `content_hash + model + prompt_version + privacy_policy`. `_patch_ai_block` re-reads the node before writing.
- **Motivation:** §4.2 staleness, §4.1 enricher cache.
- **Current Problem:** A clean committed change looks fresh; freshly compiled still-dirty tree looks stale; cache is mis-keyed.
- **Expected Benefit:** Correct freshness signal; correct AI block roundtrip; correct privacy.
- **Priority:** High.
- **Effort:** Medium (1 week).
- **Dependencies:** I11 (authenticated model identity).
- **Risk:** Performance of full source hashing; choose between full hash and incremental hash.
- **Suggested Direction:** Store source-hash + HEAD in the revision document. `_is_stale` re-hashes changed paths. Enricher cache key includes model, prompt version, and privacy policy; verify on read.

### I10. Persistent attempt IDs and failure journal

- **Improvement:** Every WO discovery → execution attempt gets a unique ID. Every stage emits structured events with the ID. The harness's outer `try/except` catches and journals uniformly. Watcher queue preserves pending items across runner failure.
- **Motivation:** R21, R22, §4.3.
- **Current Problem:** Outer `try/except` wraps only `LoopSafetyError`; WO has no claim/attempt; watcher clears queue before runner.
- **Expected Benefit:** Idempotency, forensic trail, no lost work.
- **Priority:** High.
- **Effort:** Medium (1 week).
- **Dependencies:** None.
- **Risk:** None significant.
- **Suggested Direction:** Introduce `RunAttempt` dataclass with `attempt_id`, `wo_id`, `agent_id`, `started_at`. Write to `.sync/runtime/journal/<attempt_id>.jsonl` at each stage. Catch all exceptions in `run_once`, journal, and return a structured failure result.

### I11. Authenticated identity and content-bound approvals

- **Improvement:** Replace label-based governance (`claude`/`codex`/`ceo`/`human`) with an authenticated identity. Sign contracts; sign approvals; bind to content hash.
- **Motivation:** R3, §4.4 governor.
- **Current Problem:** Anyone can claim to be `human`; approval file is overwritten per skill/version.
- **Expected Benefit:** Real trust boundary; approvals are immutable evidence.
- **Priority:** Medium (foundation for I3).
- **Effort:** High (2 weeks).
- **Dependencies:** None.
- **Risk:** Onboarding friction.
- **Suggested Direction:** Use a keyring-backed identity for the CLI; sign contracts with the agent's key; compute approval file path as `<sha256(manifest+signer+time)>.json`; reject re-use of the same approval for a different manifest.

### I12. Incremental parsing and augmentation, projection refresh

- **Improvement:** Only re-parse changed files; cache augmenter results keyed by source hash; refresh T2 projections from new edges.
- **Motivation:** R9.
- **Current Problem:** `incremental` re-parses the entire project; runtime/flow evidence does not refresh T2; `callers` via reverse index misses new edges.
- **Expected Benefit:** Faster updates; up-to-date reverse index.
- **Priority:** Medium.
- **Effort:** High (2 weeks).
- **Dependencies:** I6 (atomic projections).
- **Risk:** Cross-file effects (imports) require dependency analysis.
- **Suggested Direction:** Add a `FileIRCache` keyed by source hash. Augmenter functions take a cache and return (result, cache_key). On update, re-parse only changed files; replay augmenters on changed symbols; refresh projections incrementally.

### I13. True procedural learning

- **Improvement:** Calibrate confidence from real outcomes (tests pass rate, post-deploy error rate, manual feedback), not from sample count + similarity. Fail closed on missing evidence. Require env/model/content identity in receipts. Distill deterministically from a verified set, not from a heuristic first-record copy.
- **Motivation:** R12, R16, §4.4.
- **Current Problem:** Cluster, distiller, structural, replay, canary are heuristic; missing evidence fails open.
- **Expected Benefit:** Skills become real reusable evidence; spurious skills stop.
- **Priority:** Medium.
- **Effort:** High (2–3 weeks).
- **Dependencies:** I2 (independent verification), I3 (single transition path).
- **Risk:** Slow adoption; need a backfill strategy.
- **Suggested Direction:** Introduce `SkillOutcomeTracker` keyed by `skill_id+version+env`. Confidence is a function of (successes, failures, severity-weighted) with a Bayesian update. Cluster only on verified outcomes. Receipts bind to manifest content hash + env hash + model identity.

### I14. Documentation truth and CI

- **Improvement:** Align pillars, compiler list, version metadata, sample contract shape, and templates with the actual code. Add a docs-vs-code CI check.
- **Motivation:** §6, R11, R19, R20.
- **Current Problem:** Many discrepancies; no CI; badges not self-validating.
- **Expected Benefit:** Trustworthy docs; fewer onboarding mistakes.
- **Priority:** Medium.
- **Effort:** Low–Medium (1 week).
- **Dependencies:** None.
- **Risk:** None.
- **Suggested Direction:** Add a `docs_check` that verifies: `pyproject.toml` version matches `cli/__init__.py`; package version matches runtime template; sample contract in `STACKMIND.md` validates against `schemas/contract.schema.json`; template files match the protocol in `AGENTS.md`. Run on PR.

### I15. Test environment and adversarial coverage

- **Improvement:** Tracked CI; pinned Python and dependency versions; real SQLite FTS5; installed-wheel behaviour; constructor-bound contract propagation; skill revalidate/rollback denial; live shell containment; projection rebuild under concurrent reads; PAT redaction for bare PATs; read-only `assemble_context`.
- **Motivation:** §9.
- **Current Problem:** No CI; many invariants untested; coverage claim is static.
- **Expected Benefit:** Catch regressions early; prove security boundaries.
- **Priority:** Medium.
- **Effort:** High (2 weeks).
- **Dependencies:** None for the harness/contract tests; I1/I2 for live shell containment.
- **Risk:** None.
- **Suggested Direction:** Add `.github/workflows/ci.yml`; install in a clean venv; run `pip install -e .[dev,embeddings]`; run pytest with coverage; add the missing tests listed in §9.

---

## 14. Recommended Roadmap

Five phases, each with explicit goals, exit criteria, and risks. The phases are *sequential*; later phases depend on earlier ones.

### Phase 1 — Stabilization (0–3 months)

**Goals.** Stop live harm; make trust real; make state consistent.

**Items.** I1 (staged execution), I2 (independent verification), I3 (single skill transition), I4 (constructor-bound contract), I5 (read-only assemble_context), I6 (atomic projections + transactional writer), I7 (lock hardening), I8 (init/migrate safety), I10 (attempt IDs + journal).

**Exit criteria.**

- Harness executes proposed commands in a copy and only applies to live on success.
- `VerificationDimensions` are measured, not derived.
- `revalidate` and `rollback` go through the same transition path as `promote`.
- `KnowledgeAPI.assemble_context` is read-only by construction and by test.
- Projections are atomic; writer is transactional; lock is hardened.
- `init` and `migrate` cannot lose user data.
- Every run attempt has a unique ID and a journaled event log.

**Risks.** Performance overhead; behaviour differences between copy and live; Windows-specific locking; breaking existing tests that relied on loose semantics.

### Phase 2 — Architectural improvements (3–6 months)

**Goals.** Make the system understandable and maintainable.

**Items.** I11 (authenticated identity), I14 (docs truth + CI), I15 (test environment and adversarial coverage), partial I9 (staleness and AI cache correctness).

**Exit criteria.**

- Contracts are signed; approvals are content-bound.
- CI runs on every PR; docs match code.
- Test suite covers the security boundaries and the invariants listed in §9.
- Staleness signal is correct; AI cache is correctly keyed.

**Risks.** Onboarding friction; CI infrastructure.

### Phase 3 — Capability expansion (6–9 months)

**Goals.** Add real capabilities without sacrificing trust.

**Items.** I9 (full), I12 (incremental parsing/augmentation), real hosted LLM provider(s) behind the protocol, real search provider(s) behind the protocol.

**Exit criteria.**

- Incremental updates are actually incremental; reverse index is up-to-date after every change.
- Hosted LLM providers can be configured without code changes.
- Test suite runs against hosted LLM stubs.

**Risks.** Hosted LLM rate limits and costs; provider API drift.

### Phase 4 — Scale and optimization (9–12 months)

**Goals.** Make the system fast enough for large Python projects.

**Items.** Performance baseline, parallel parsing, persistent augmenter cache, projection compression, FTS5 maintenance, watcher robustness.

**Exit criteria.**

- A reproducible benchmark over a representative Python project is committed.
- Incremental updates complete in seconds on large projects.
- Projections load lazily; metrics are O(1).

**Risks.** Premature optimization; cross-platform benchmarks.

### Phase 5 — Mature architecture (12–18 months)

**Goals.** Make the system ready for production autonomy.

**Items.** I13 (true procedural learning), authenticated identity integration with SSO, distributed runtime (only if proven necessary), audit/recovery tooling, third-party skill marketplace (if warranted).

**Exit criteria.**

- Skill confidence is calibrated; missing evidence fails closed.
- Audit log is durable; recovery tooling exists.
- The system is a modular local-first system with thin CLI, policy/application services, deterministic graph/evidence stores, bounded provider adapters, isolated execution, one transition authority, and explicit audit/recovery.

**Risks.** Scope creep; over-engineering. Resist the temptation to introduce distributed systems, graph databases, or extra agents purely for sophistication.

---

## 15. Proposed Future Architecture

The mature architecture should remain a **modular local-first system**. The goal is not to add sophistication for its own sake, but to make every trust boundary explicit and every state transition atomic.

```
┌──────────────────────────────────────────────────────────────────────┐
│                              CLI (thin)                              │
│  click groups: graph, harness, analyze, experience, skill, learn,    │
│                lock, init, shutdown, promote, validate, doctor,      │
│                migrate, contract                                     │
└──────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                    Policy / Application Services                     │
│                                                                      │
│  ┌─────────── Identity & Auth ──────────────────────────────────┐    │
│  │  - keyring-backed agent identities                          │    │
│  │  - signed contracts, signed approvals                       │    │
│  └─────────────────────────────────────────────────────────────┘    │
│  ┌─────────── Contracts ────────────────────────────────────────┐    │
│  │  - bound at construction; propagated to all read paths       │    │
│  │  - fail-closed on missing/malformed                          │    │
│  └─────────────────────────────────────────────────────────────┘    │
│  ┌─────────── Verification ─────────────────────────────────────┐    │
│  │  - non-LLM Verifier protocol                                 │    │
│  │  - declared/observed diffs + optional test runner            │    │
│  └─────────────────────────────────────────────────────────────┘    │
│  ┌─────────── Transition Authority ─────────────────────────────┐    │
│  │  - one function for skill state changes                      │    │
│  │  - content-bound approvals                                   │    │
│  │  - rollbacks target a previously-active version              │    │
│  └─────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       Deterministic Graph Core                       │
│                                                                      │
│  - AST parse + 14 augmenters (incremental, cached)                   │
│  - T0 registry (birth-hashes, aliases)                               │
│  - T1 nodes (sharded, per-file atomic)                               │
│  - T1 revisions (content-hash + HEAD)                                │
│  - T2 projections (generation-versioned, atomic swap)                │
│  - FTS5 experience index; content-bound receipts                     │
│  - embedding cache (content + model + prompt + privacy)              │
└──────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      Bounded Provider Adapters                       │
│                                                                      │
│  - LLMProvider (Echo, Hosted, Local)                                 │
│  - SearchProvider (Session, Hosted)                                  │
│  - EmbeddingBackend (Local, Hosted)                                  │
│  - SummaryBackend (Local, Hosted)                                    │
│                                                                      │
│  Each provider declares: model identity, prompt version, cost,       │
│  privacy class, rate limit, failure mode.                            │
└──────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       Isolated Execution                             │
│                                                                      │
│  - stage copy of project                                             │
│  - run proposed commands in stage                                    │
│  - diff declared/observed                                            │
│  - apply to live only on success                                     │
│  - journal every step with attempt ID                                │
│  - rollback path on failure                                          │
└──────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                       Audit / Recovery                               │
│                                                                      │
│  - attempt-ID journal (append-only)                                  │
│  - content-bound receipts                                            │
│  - snapshot + diff for every run                                     │
│  - restore tool for migrate/init                                     │
│  - redaction policy + tests                                          │
└──────────────────────────────────────────────────────────────────────┘
```

The principles are: **one CLI shell, one set of policy services, one deterministic graph core, a small set of bounded provider adapters, one isolated execution path, one transition authority, and one audit/recovery layer**. Anything that violates this layering is technical debt.

---

## 16. Quick Wins (≤ 1 day each)

1. **Constructor-bound contract propagation in `KnowledgeAPI.assemble_context`** (I4). One-line change to pass `self._resolve_contract(contract)` to `retrieve_skills`.
2. **Read-only `assemble_context`** (I5). Refactor to take `skill_store=None`; do not instantiate on read.
3. **Version metadata alignment.** Pick a single source of truth (e.g., `pyproject.toml`); have `VERSION`, `VERSION.md`, and the runtime template derive from it; add a CI check.
4. **`is_expired` fail-closed.** `validators/knowledge/contract.py:102-129` should return `True` for unparseable `expires_at`, not `False`.
5. **Contract load fail-closed.** `validators/harness/contract_gate.py:16-36` should raise on missing/malformed contract, not return `None`.
6. **`revalidate` actor gate.** Add `--actor` requirement; refuse to revalidate without one.
7. **PAT redaction fix.** Move the token out of capture group 1 in `validators/knowledge/enricher.py:27`.
8. **`reconcile_ghosts` review.** Confirm full-build rename reconciliation; add a test for renames across full builds.
9. **Documentation: pillar count and compiler count.** Update `STACKMIND.md` to match code (14 augmenters; four pillars).
10. **Add `.sync/` to untracked or document the choice.** Decide whether `.sync/` should be a separate repo; if yes, `git rm -r --cached .sync/`; if no, document the decision.

---

## 17. Medium-Term Improvements (1–4 weeks each)

1. **Staged execution with diff-then-apply** (I1).
2. **Independent verification protocol** (I2).
3. **Single skill transition path** (I3).
4. **Atomic projections + transactional writer** (I6).
5. **Lock hardening** (I7).
6. **Init preservation and migrate safety** (I8).
7. **Staleness and AI cache correctness** (I9).
8. **Persistent attempt IDs and failure journal** (I10).
9. **Docs truth + CI** (I14).
10. **Test environment and adversarial coverage (partial)** (I15).

---

## 18. Long-Term Improvements (> 1 month each)

1. **Authenticated identity and content-bound approvals** (I11).
2. **Incremental parsing and augmentation, projection refresh** (I12).
3. **True procedural learning** (I13).
4. **Real hosted LLM/search/embedding provider integration** (Phase 3).
5. **Performance baseline and optimization** (Phase 4).
6. **Distributed runtime** (Phase 5, only if proven necessary).
7. **Third-party skill marketplace** (Phase 5, only if warranted).

---

## 19. Final Assessment

StackMind today is a **deterministic graph toolkit with a partial runtime layer on top**. The graph side is real and useful: the AST pipeline, the 14 augmenters, the T0/T1/T2 storage, the alias renames, the FTS5 experience index, the projection system, and the embedding/summary enrichment primitives are substantive engineering. The CLI is reasonable. The IR has provenance, and the registry has clean identity semantics. These are non-trivial accomplishments.

The runtime side, however, **does not yet match its documentation**. The harness runs LLM-suggested commands on the live workspace after declaring it has validated them; the verification dimensions are derived from the LLM's own completion status; the skill pipeline can activate a STALE/FAILED skill through `revalidate`; the KnowledgeAPI "read-only" context path mutates the filesystem; the writer and projections are not atomic; the lock is advisory; `init` and `migrate` can lose user data; the procedural learning loop is heuristic and fails open on missing evidence. None of these are theoretical — each one was confirmed in source during this investigation.

The single most important thing to do is to **stop live harm and make trust real before any real hosted LLM is wired in**. That means staged execution, independent verification, a single skill transition authority, constructor-bound contract propagation, and read-only context assembly. After that, state consistency (atomic projections, transactional writer, hardened lock, safe init/migrate), then docs/truth, then incremental parsing, then true procedural learning, then provider integration, then performance, then mature architecture.

The codebase has the **shape** of a production multi-agent system. It does not yet have the **substance** of one. The good news is that the existing primitives (deterministic IR, provenance envelopes, registry aliases, rebuildable projections, FTS5 index, receipts, snapshots, retry queue, provider protocols) are a strong foundation. The bad news is that without the trust and consistency work in Phase 1, every additional feature multiplies risk rather than multiplying value.

**Did I leave the project completely unchanged?** **Yes.** I did not edit, create, delete, rename, or move any file during the investigation. I did not install dependencies. I did not run stateful commands. I did not commit, push, tag, or create branches. I did not "fix" any of the issues I documented. The pre-existing untracked file `Project_Study_&_Future_Improvements.md` was not authored or modified by this investigation; it existed before this session. The final Git state at the end of the investigation was:

```
HEAD: 6fd56479d20f3d9c3e1dddde6872913c44b122c8
Working tree: only the pre-existing untracked file `Project_Study_&_Future_Improvements.md`.
```

This file (`docs/Project_Technical_Study_&_Future_Improvement_Report.md`) was created after the investigation at the user's explicit authorization, with a single-file write override of the prior read-only constraint. No other files were added, modified, deleted, renamed, or moved.

---

### Notes on confidence

- **Confirmed by source.** Every file:line reference in this report was read during the investigation. The 19 architectural risks in §10 and the 22 debt items in §11 are all grounded in code or documentation. The 15 prioritized improvements in §13 are derived from the risks.
- **Strong architectural observations.** The draft-staging gap in the harness, the constructor-bound contract drop in the KnowledgeAPI, the skill `revalidate`/`rollback` bypass, the enricher PAT redaction bug, the `_is_stale` heuristics, the projection non-atomicity, the version metadata drift, the documentation/code discrepancies — all are evidence-backed.
- **Reasonable hypotheses.** Some improvements' effort estimates assume a single engineer familiar with the codebase. Performance numbers are not given because no benchmarks were run.
- **Could not be confirmed from the current codebase.** Test pass/fail rate (tests were not run); coverage percentage (no measurement was taken); real FTS5 availability on the runtime SQLite; installed-wheel operation; the `CbmCompiler` is described in `STACKMIND.md` but does not exist in tracked code; the 15th compiler in the enumeration is absent; the 4-pillar "Procedural Learning" is described in `README.md` but the current procedural learning code is in `validators/learning/` and `validators/experience/` and is in a partial state per `PLAN_PROCEDURAL_LEARNING.md`.
- **No code changes were made during the investigation.** The user explicitly forbade them. No "fixes" were attempted. The recommended improvements in §13 are the report; they are not implemented changes.
