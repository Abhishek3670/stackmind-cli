# StackMind Code-Graph Intelligence POC Evaluation

Work order: WO-035  
Agent: Codex  
Measured at: 2026-08-19  
Repository commit reported by Knowledge API: `fa107cee06e91f5e6b7a764b2633dd8d5dcd679c`  
Knowledge revision: 23  

## Executive Summary

The POC is usable for deterministic graph lookup, provenance-aware retrieval, runtime evidence modeling, static data-flow observations, semantic retrieval when embeddings are present, and contract-gated access checks. It should graduate the deterministic graph/API, evidence/provenance, flow analyzer, runtime tracer, and contract layer as POC-complete.

The semantic layer should graduate only as an optional capability. The current repository graph reports `semantic: False`, zero enrichment cache entries, and `stale: True`; semantic retrieval works in the controlled corpus when cached embeddings are present, but the production repository does not yet have semantic enrichment populated.

## Evaluation Corpus

The corpus covers the representative code paths requested by WO-035:

| Area | Files / APIs | Hand-labeled expectations |
|---|---|---|
| Runtime tracing | `validators/knowledge/analysis/runtime.py`, `tests/test_runtime_tracer.py` | `helper_a` calls `helper_b`; runtime evidence provider is `runtime-tracer`; normalization succeeds only when registry identities exist. |
| Data flow | `validators/knowledge/analysis/flow.py`, `tests/test_flow_analyzer.py` | `request.args -> db.execute` is a valid flow; multistep assignments remain valid; constants and unsupported sources are not flows; `request.args -> sanitize` cross-function call is a valid flow. |
| Semantic retrieval | `validators/knowledge/api.py`, `validators/knowledge/embedding/cache.py`, `tests/test_embedding_backend.py` | With cached embeddings, query `authentication token refresh` ranks `authentication_token_refresh` first. Without cached embeddings, API falls back to text search. |
| Provenance / unified RAG | `validators/knowledge/api.py`, `tests/test_unified_rag.py` | Results expose evidence, `provenance_summary`, and `why_retrieved` metadata where available. |
| Contract enforcement | `.sync/contracts/WO-035.yaml`, `validators/knowledge/contract.py` | WO-035 allows `validators.knowledge.*`, `cli.*`, `tests.*`, `docs.*`; direct edit validation rejects `app.py` and `schemas/contract.schema.json`. |

## Graph State

`stackmind graph stats -p .`:

| Metric | Value |
|---|---:|
| Nodes | 4,344 |
| Edges | 39,888 |
| Revisions | 23 |
| Latest revision | 23 |
| Enrichment jobs | 4,481 |
| Enrichment paused | `False` |
| Enrichment cache hits | 0 |
| Enrichment cache entries | 0 |

`stackmind graph versions -p .` reports CLI `3.0.0`, IR schema `1`, compiler `frontend-1`, knowledge schema `knowledge-1`, metrics `metrics-1`, reverse index `reverse-index-1`, search `search-1`.

Important negative result: graph queries in this run reported `stale: True` and `semantic: False`.

## Runtime Tracing Metrics

Probe: 100 calls to the existing `tests.test_runtime_tracer.helper_a()` target, traced with `RuntimeTracer(include=["*test_runtime_tracer*"])`.

| Metric | Value |
|---|---:|
| Baseline wall time | 0.0000168 s |
| Traced wall time | 0.182364 s |
| Tracing overhead ratio | 10,854.996x |
| Runtime event count | 202 |
| Runtime call observations | 100 |
| Unique runtime calls discovered | 1 |
| Runtime-confirmed static relationships | 1 |
| Static-only calls in corpus | 0 |
| Normalized edges | 1 |
| Partial trace | `False` |

Observed runtime call:

`tests/test_runtime_tracer.py:helper_a -> tests/test_runtime_tracer.py:helper_b`

Graduation recommendation: graduate with caution. The tracer captures and normalizes the expected relationship, but per-call tracing overhead is high enough that it should remain an explicit diagnostic/evaluation mode rather than a default hot-path behavior.

## Data-Flow Metrics

Probe: five hand-labeled snippets executed through `FlowAnalyzer.analyze_file()`.

| Case | Expected paths | Found paths | Latency |
|---|---:|---:|---:|
| direct `request.args -> db.execute` | 1 | 1 | 0.003453 s |
| multistep assignment path | 1 | 1 | 0.004955 s |
| constant assigned to SQL sink | 0 | 0 | 0.003922 s |
| unsupported `request.cookies` source | 0 | 0 | 0.004638 s |
| cross-function `request.args -> sanitize` | 1 | 1 | 0.003288 s |

| Metric | Value |
|---|---:|
| Valid flow paths found | 3 |
| False positives | 0 |
| False negatives | 0 |
| Total analysis time | 0.020256 s |

Graduation recommendation: graduate for bounded Python AST source-to-sink analysis. The current analyzer is intentionally narrow; it is useful for defined sources/sinks and should not be presented as a general taint engine.

## Semantic Retrieval Metrics

Controlled corpus: three synthetic function nodes with cached vectors for `authentication_token_refresh`, `database_commit`, and `payments_flow`.

| Metric | Value |
|---|---:|
| Semantic enabled in controlled corpus | `True` |
| Top-3 result order | `authentication_token_refresh`, `database_commit`, `payments_flow` |
| Precision@1 | 1.000 |
| Precision@3 | 0.333 |
| Text search avg latency | 0.005189 s |
| Semantic search avg latency | 0.007408 s |
| Cache hits / requests during cache probe | 3 / 6 |
| Cache hit rate during cache probe | 0.500 |
| Backend calls during cache probe | 3 |

Repository graph result: `semantic: False`, enrichment cache entries `0`. This means semantic retrieval is implemented and testable, but repository-wide semantic enrichment is not populated in the current graph.

Graduation recommendation: graduate as optional/beta. Do not make semantic retrieval a required production path until enrichment jobs produce durable repository vectors and stats report non-zero cache entries/hits.

## CLI/API Latency Baseline

| Command | Return code | Latency | First output line |
|---|---:|---:|---|
| `stackmind graph stats -p .` | 0 | 11.786335 s | `nodes: 4344` |
| `stackmind graph query KnowledgeAPI.flows -p .` | 0 | 3.012016 s | `revision: 23` |
| `stackmind graph context AgentContract KnowledgeAPI flows semantic --token-budget 1200 -p .` | 0 | 4.573290 s | `revision: 23` |

Graduation recommendation: acceptable for local agent context assembly, not yet optimized enough for very frequent interactive calls.

## POC Success Questions

### 1. Structural: Who calls `AuthService.login`?

`stackmind graph query "AuthService.login" -p .` found no `AuthService.login` symbol. The nearest results were FastAPI auth compiler helpers, CLI auth functions, and a documentation section about a `login_form` cycle. Therefore the honest answer for this repository is: no structural callers can be reported because the target symbol does not exist in the current graph.

### 2. Runtime: Which callers were observed during test execution?

In the runtime corpus, `helper_a` was observed calling `helper_b` 100 times, producing one unique runtime call relationship and one normalized edge after registry identities were present.

### 3. Data Flow: Can request input reach SQL / execution sinks?

Yes, in the hand-labeled corpus. `request.args` reaches `db.execute` directly and through multistep assignments. The analyzer also correctly rejected constant input to `db.execute` and unsupported `request.cookies` as configured negative cases.

### 4. Semantic: What code is relevant to "authentication token refresh"?

In the controlled semantic corpus, semantic retrieval ranked `authentication_token_refresh` first for `authentication token refresh`. In the repository graph, semantic mode is currently disabled, so production repository queries fall back to text/lexical retrieval.

### 5. Provenance: Why does StackMind believe each relationship exists?

StackMind exposes provenance through:

- `provenance_summary`, such as `static` or provider names.
- Evidence payloads from providers such as `runtime-tracer` and `flow-analyzer`.
- Context metadata such as `why_retrieved`, including lexical match, caller relationship, and semantic similarity when semantic search is active.

Runtime call evidence records provider `runtime-tracer`, evidence type `runtime-observed`, confidence `1.0`, run ID, analyzer version, and caller/callee metadata. Flow evidence records provider `flow-analyzer`, evidence type `static-flow`, origin, sink, path length, and intermediate steps.

### 6. Security: Does `AgentContract` enforce access control on retrieval paths?

Partially yes, with an important caveat. Direct contract validation rejected out-of-scope edit operations:

- `stackmind graph contract validate WO-035 --op "edit app.py"` -> rejected.
- `stackmind graph contract validate WO-035 --op "edit schemas/contract.schema.json"` -> rejected.

The contract loader also validates WO-035 successfully and query outputs pass through the `KnowledgeAPI` contract hooks. However, `stackmind graph explain-denial WO-035 --node validators.harness.runner` reported the node as allowed because scope expansion from `validators.knowledge.*` reaches `validators.harness.runner` through graph depth. That may be intended dependency-depth behavior, but it should be reviewed if contracts are expected to be module-prefix strict.

Graduation recommendation: graduate direct allow/deny enforcement, but add a follow-up review for depth expansion semantics and retrieval-path coverage.

## Overall Graduation Recommendations

| Capability | Recommendation | Reason |
|---|---|---|
| Deterministic graph/API | Graduate | Repository graph has 4,344 nodes and 39,888 edges with versioned metadata and working CLI/API queries. |
| Runtime tracing | Graduate with caution | Correct evidence capture and normalization; high tracing overhead limits hot-path use. |
| Data-flow analysis | Graduate | 3/3 positive paths found, 0 false positives, 0 false negatives in the bounded corpus. |
| Semantic retrieval | Beta | Works with cached embeddings; current repository graph has no populated semantic cache. |
| Provenance-aware retrieval | Graduate | Runtime, flow, and unified context paths expose evidence/provenance metadata. |
| Contract enforcement | Graduate with follow-up | Direct out-of-scope edits are rejected; graph-depth scope expansion needs policy confirmation. |

## Reproducibility

Commands used:

```powershell
stackmind graph stats -p .
stackmind graph versions -p .
stackmind graph context "WO-035 POC evaluation benchmarking runtime tracing data flow semantic retrieval AuthService.login SQL sink authentication token refresh AgentContract provenance" --token-budget 4000 -p .
stackmind graph query "AuthService.login" -p .
stackmind graph flows "request" "SQL" -p . --json-output
stackmind graph contract show WO-035
stackmind graph contract validate WO-035 --op "edit app.py"
stackmind graph contract validate WO-035 --op "edit schemas/contract.schema.json"
pytest -q tests/test_runtime_tracer.py tests/test_flow_analyzer.py tests/test_embedding_backend.py tests/test_unified_rag.py
stackmind validate .
```

The runtime, flow, and semantic micro-benchmarks used temporary projects initialized with `stackmind init --no-git` through the in-process `cli.init.init()` helper. They do not modify the repository knowledge store.
