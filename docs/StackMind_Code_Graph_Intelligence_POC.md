# StackMind — Code-Graph Intelligence POC

**Status:** Proposed Proof of Concept  
**Target:** `feat/v2.0.0`  
**Decision:** Implement selected Code-Graph-RAG capabilities inside SKC; do **not** adopt Code-Graph-RAG as a parallel graph/vector infrastructure.

## 1. Executive Decision

StackMind should **not integrate Code-Graph-RAG (CGR) wholesale**.

The POC should instead prove that StackMind can absorb the two most valuable CGR-style capabilities that are not already represented in its current SKC model:

1. **Runtime-confirmed call relationships**
2. **Data-flow / taint relationships (`FLOWS_TO`)**

A third workstream should productionize StackMind's existing semantic retrieval path by introducing a real `EmbeddingBackend`.

The architectural rule is:

> **SKC remains the single authoritative knowledge representation, identity system, storage layer, and access-control boundary.**

External tools such as `codebase-memory-mcp` and, later, a CGR-derived runtime/data-flow analyzer are treated as **analysis providers / data feeds**. Their identifiers are discarded at ingestion time and relationships are re-minted against SKC identities.

### Target architecture

```text
                         STACKMIND
                             |
                    +--------+--------+
                    |                 |
                .sync / SKC       External analyzers
                AUTHORITATIVE          |
                    |          +-------+---------+
                    |          |                 |
                    |         CBM        Runtime/Data-flow
                    |       Tree-sitter      Provider
                    |          |                 |
                    +----------+-----------------+
                               |
                         Normalized evidence
                               |
                         SKC Knowledge IR
                               |
          +--------------------+---------------------+
          |                    |                     |
       Static              Runtime              Data-flow
        graph                graph                graph
       CALLS/etc.          CALLS/etc.            FLOWS_TO
          |                    |                     |
          +--------------------+---------------------+
                               |
                     Search / Query / RAG
                               |
                             Agents
```

---

## 2. Why This POC Exists

The current StackMind direction already provides a persistent, queryable code knowledge graph and project-level semantics. The repository describes StackMind as compiling source into a persistent knowledge graph and exposes graph-oriented queries for callers, impact, and context.

Current branch:

- `feat/v2.0.0`
- Repository: https://github.com/Abhishek3670/stackmind
- CGR reference: https://github.com/vitali87/code-graph-rag

The POC is therefore **not** a replacement project. It answers a narrower question:

> Can StackMind gain the highest-value Code-Graph-RAG capabilities without creating a second source of truth, second identity space, or mandatory graph/vector infrastructure?

The answer this POC is designed to prove is **yes**.

---

# 3. Scope

## In scope

### A. Runtime call tracing

Capture calls observed during controlled execution of tests or selected commands and normalize them into SKC edges.

Example:

```text
STATIC

A ──CALLS──> B


RUNTIME

A ──CALLS──> B
      provenance = runtime
      confidence = 1.0
```

The runtime result must not overwrite static evidence.

---

### B. Data-flow / taint relationships

Introduce a new edge family:

```text
A ──FLOWS_TO──> B
```

Example:

```text
request
   |
   v
validate_input()
   |
   v
build_query()
   |
   v
execute_sql()
```

This relationship becomes queryable through the same SKC mechanisms as other graph edges.

---

### C. Real embedding backend

Implement a concrete `EmbeddingBackend` behind the existing retrieval interface.

Required properties:

- deterministic model/configuration
- content-hash caching
- batch generation
- configurable model/provider
- offline/local option
- graceful lexical fallback
- no mandatory Qdrant dependency for the POC

---

### D. Unified provenance

Every imported relationship must preserve:

- source provider
- evidence type
- confidence
- observation/run identifier where applicable
- source location if available
- timestamp
- analyzer version

Example:

```json
{
  "edge_kind": "CALLS",
  "src": "skc:...",
  "dst": "skc:...",
  "provenance": {
    "provider": "runtime-tracer",
    "evidence": "runtime-observed",
    "confidence": 1.0,
    "run_id": "run-2026-08-18-001",
    "analyzer_version": "poc-0.1"
  }
}
```

---

# 4. Explicit Non-Goals

The POC will **not**:

- add Memgraph as a required StackMind service
- add Qdrant as a required StackMind service
- maintain a CGR graph alongside SKC
- maintain CGR node IDs as authoritative IDs
- create a CGR↔SKC synchronization layer
- replace the current SKC graph
- replace `birth_key()` identity
- replace existing Contract-based access control
- duplicate the current multi-language parser effort
- make StackMind depend on the CGR repository at runtime

If a later benchmark proves that an external graph database is necessary at a very large scale, that should be a separate architecture decision.

---

# 5. Current StackMind Components to Reuse

The POC should reuse the current SKC architecture rather than introduce new parallel infrastructure.

Known integration points from the current v2.0.0 work include:

```text
validators/
  knowledge/
    compiler/
      cbm_compiler.py
```

and the existing SKC concepts around:

- `birth_key()`
- IR generation
- graph edges
- sharded JSON persistence
- graph query APIs
- Contract-based access control
- content-hash-based semantic caching

The exact file/function signatures should be confirmed against the working tree before implementation. This document intentionally treats those items as integration targets rather than pretending every symbol name has been independently verified.

---

# 6. Proposed Provider Architecture

Introduce a small internal provider boundary.

```python
from dataclasses import dataclass
from typing import Iterable, Protocol


@dataclass(frozen=True)
class AnalysisEvidence:
    provider: str
    evidence_type: str
    confidence: float
    run_id: str | None = None
    analyzer_version: str | None = None
    metadata: dict | None = None


@dataclass(frozen=True)
class ObservedRelationship:
    source_external_id: str
    target_external_id: str
    edge_kind: str
    evidence: AnalysisEvidence


class AnalysisProvider(Protocol):
    name: str

    def analyze(self, request) -> Iterable[ObservedRelationship]:
        ...
```

The provider returns **external observations**, never authoritative SKC IDs.

The ingestion layer then performs:

```text
external symbol
      |
      v
symbol resolution
      |
      v
SKC canonical identity
      |
      v
edge normalization
      |
      v
SKC IR
```

---

# 7. Identity Rule

This is the most important POC invariant.

External analyzers may produce:

```text
python:function:foo
file.py:17
memgraph-node-123
tree-sitter-node-9988
```

StackMind must never persist these as canonical graph identities.

Instead:

```text
External ID
    |
    v
resolve to source/symbol
    |
    v
birth_key()
    |
    v
SKC canonical node ID
```

Therefore:

> **CGR can observe. SKC decides identity.**

This preserves the existing rename/alias semantics and prevents a second identity universe.

---

# 8. Runtime Call Tracing POC

## 8.1 Goal

Run a selected test command and capture:

```text
caller -> callee
```

relationships that are actually observed.

## 8.2 Minimal implementation

Use Python runtime instrumentation for the first POC.

Possible mechanism:

```text
sys.setprofile()
```

or an equivalent supported tracing mechanism.

Capture:

```text
CALL event
RETURN event
```

and resolve frames to:

- module
- qualified function
- file
- line

Then map them to SKC symbols.

## 8.3 Runtime record

```json
{
  "caller": {
    "module": "app.auth",
    "qualname": "AuthService.login"
  },
  "callee": {
    "module": "app.db",
    "qualname": "UserRepository.find"
  },
  "run_id": "pytest-001",
  "test": "tests/test_auth.py::test_login"
}
```

## 8.4 Merge semantics

If a static edge already exists:

```text
A ──CALLS──> B [STATIC]
```

do not create a duplicate logical edge.

Instead merge evidence:

```text
A ──CALLS──> B

evidence:
  - static
  - runtime
```

Conceptually:

```json
{
  "kind": "CALLS",
  "src": "skc:A",
  "dst": "skc:B",
  "evidence": [
    {"type": "static", "confidence": 0.85},
    {"type": "runtime", "confidence": 1.0}
  ]
}
```

## 8.5 Failure semantics

Tracing failure must be **fail-open for analysis, fail-closed for authority**:

- tracing crashes → existing SKC graph remains valid
- partial trace → partial evidence is marked partial
- unresolved symbol → observation is dropped/quarantined, not guessed
- malformed analyzer output → ingestion rejects it
- runtime graph never replaces static graph

---

# 9. FLOWS_TO / Taint POC

## 9.1 Goal

Represent data movement through a program.

The first POC should avoid claiming complete taint analysis. It should implement a bounded analysis that can prove useful paths.

Example:

```text
SOURCE
  |
  v
ASSIGNMENT
  |
  v
FUNCTION ARGUMENT
  |
  v
CALL
  |
  v
SINK
```

## 9.2 Initial edge type

```text
FLOWS_TO
```

Optional metadata:

```json
{
  "kind": "FLOWS_TO",
  "src": "skc:A",
  "dst": "skc:B",
  "flow": {
    "origin": "request.args",
    "sink": "db.execute",
    "confidence": 0.92,
    "path_length": 4
  }
}
```

## 9.3 POC analysis boundary

Start with:

- assignments
- local variables
- function arguments
- return values
- selected known sources
- selected known sinks

Do **not** initially promise:

- whole-language soundness
- complete interprocedural taint analysis
- framework-independent vulnerability detection

The first milestone is:

> Produce useful, inspectable data-flow paths with explicit confidence and provenance.

---

# 10. EmbeddingBackend POC

## 10.1 Goal

Make the existing semantic-search interface actually use a production-quality embedding implementation.

Recommended interface:

```python
class EmbeddingBackend(Protocol):
    model_name: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...
```

Optional methods:

```python
def embed_query(self, text: str) -> list[float]: ...
def embed_document(self, text: str) -> list[float]: ...
```

## 10.2 Requirements

The implementation must:

1. batch efficiently
2. use content hashes for cache keys
3. persist model/version metadata
4. detect dimension/model changes
5. remain replaceable
6. fall back safely to lexical search if embedding is unavailable

## 10.3 Storage

For the POC:

```text
SKC
 |
 +-- sharded graph storage
 |
 +-- embedding cache
```

No Qdrant.

If later profiling proves in-process search insufficient, introduce an optional vector backend behind the same interface.

---

# 11. Query Model

The existing graph query layer should expose provenance-aware answers.

Example:

```text
graph callers AuthService.login
```

could return:

```text
UserRepository.find
  CALLS
  evidence: runtime-confirmed + static
```

A future query could be:

```text
graph flows request -> db.execute
```

and return:

```text
request
  -> validate_input
  -> build_query
  -> execute_sql

evidence:
  static
confidence:
  0.91
```

The critical property is that agents receive **evidence**, not just relationships.

---

# 12. RAG Integration

The final retrieval stack should become:

```text
                     User task
                         |
          +--------------+--------------+
          |              |              |
       lexical         semantic       graph
        search          search        search
          |              |              |
          +--------------+--------------+
                         |
                    reranking
                         |
                provenance filtering
                         |
                  Contract gate
                         |
                  agent context
```

Graph retrieval should be a first-class retrieval signal, not a separate application.

A context item should be able to say:

```text
Symbol: AuthService.login

Why retrieved:
- lexical match
- semantic similarity
- caller relationship
- runtime-confirmed dependency

Access:
- allowed by Contract scope
```

---

# 13. Contract / Security Rules

The analysis provider must never bypass existing StackMind authorization.

Correct flow:

```text
raw analyzer result
       |
       v
normalize
       |
       v
SKC IDs
       |
       v
Contract-gated query
       |
       v
agent
```

Never:

```text
agent -> analyzer -> raw source
```

unless that source retrieval is separately authorized.

Runtime tracing also requires:

- explicit command scope
- test/environment isolation
- no production execution by default
- redaction policy for captured arguments
- no persistence of secrets
- bounded observation volume

---

# 14. Performance / Resource Isolation

Runtime tracing can be much more expensive than static indexing.

The POC should therefore:

- run tracing only on explicit commands
- default to tests, not arbitrary applications
- support include/exclude patterns
- cap event count
- batch normalization
- persist after the run, not on every event
- keep runtime evidence in a separate ingestion buffer until validated

Suggested command shape:

```text
stackmind analyze runtime -- pytest tests/
```

and later:

```text
stackmind analyze flows -- path/to/module.py
```

The exact CLI names are part of the POC and may be changed to match the existing CLI conventions.

---

# 15. Suggested Storage Extension

Do not redesign the graph format.

Extend the existing edge representation.

Conceptually:

```json
{
  "edge_id": "...",
  "src": "...",
  "dst": "...",
  "kind": "CALLS",
  "evidence": [
    {
      "type": "static",
      "provider": "skc",
      "confidence": 0.8
    },
    {
      "type": "runtime",
      "provider": "runtime-tracer",
      "confidence": 1.0,
      "run_id": "..."
    }
  ]
}
```

For data flow:

```json
{
  "kind": "FLOWS_TO",
  "evidence": [
    {
      "type": "static",
      "provider": "flow-analyzer",
      "confidence": 0.9
    }
  ]
}
```

The exact schema should follow the existing SKC serialization contract rather than introduce an incompatible parallel schema.

---

# 16. POC File Layout

A reasonable initial layout is:

```text
validators/
└── knowledge/
    ├── compiler/
    │   ├── cbm_compiler.py
    │   └── ...
    │
    ├── analysis/
    │   ├── __init__.py
    │   ├── base.py
    │   ├── runtime.py
    │   ├── flow.py
    │   └── normalize.py
    │
    ├── embedding/
    │   ├── __init__.py
    │   ├── base.py
    │   ├── local.py
    │   └── cache.py
    │
    └── ...
```

Again, these are **POC targets**, not claims that these exact files already exist.

---

# 17. Testing Strategy

## 17.1 Identity tests

Given the same symbol before and after:

- module relocation
- rename
- parser-provider change

verify canonical identity behavior remains governed by SKC rules.

### Acceptance

No external analyzer ID appears as the canonical node ID.

---

## 17.2 Runtime tracing tests

Given:

```python
def a():
    b()

def b():
    pass
```

and a test calling `a()`:

Expected:

```text
a CALLS b
evidence.type = runtime
```

### Acceptance

Runtime relationship is queryable and coexists with static evidence.

---

## 17.3 Data-flow tests

Given:

```python
def sink(value):
    db.execute(value)

def handler(request):
    value = request.args["id"]
    sink(value)
```

Expected:

```text
request.args
  FLOWS_TO
db.execute
```

### Acceptance

The system produces a bounded, inspectable flow path with provenance.

---

## 17.4 Failure tests

Simulate:

- tracer crash
- malformed analyzer output
- unresolved symbol
- embedding backend unavailable
- embedding dimension mismatch

### Acceptance

Existing SKC remains usable and no invalid relationships are committed.

---

## 17.5 Contract tests

Verify an agent with restricted module scope cannot retrieve:

- forbidden nodes
- forbidden edges
- forbidden flow paths
- runtime evidence belonging to an inaccessible symbol

### Acceptance

Same access semantics before and after the POC.

---

# 18. Evaluation Metrics

The POC should be judged on measurable outcomes.

## Runtime tracing

Measure:

- unique runtime calls discovered
- static-only calls
- runtime-confirmed calls
- unresolved calls
- tracing overhead

Target:

> Demonstrate useful new edges with bounded execution overhead on representative tests.

## Data flow

Measure:

- valid flow paths
- false positives
- false negatives on a hand-built corpus
- analysis time

Target:

> Demonstrate useful flows on a controlled corpus without silently claiming completeness.

## Semantic retrieval

Measure:

- top-k relevance
- embedding cache hit rate
- latency
- lexical-only vs semantic retrieval

Target:

> Prove that real embeddings materially improve retrieval quality over lexical fallback.

---

# 19. Phased Implementation Roadmap

## Phase 0 — Verify Current Contracts

Before coding:

- inspect current SKC IR types
- inspect current edge serialization
- inspect `birth_key()`
- inspect query/API contracts
- inspect current embedding protocol
- inspect Contract gate
- inspect CBM normalization path

### Exit criterion

A short implementation note maps POC changes to confirmed code symbols.

---

## Phase 1 — EmbeddingBackend

Implement:

```text
EmbeddingBackend
    |
    +-- local/remote implementation
    +-- cache
    +-- model metadata
```

### Exit criterion

Semantic search uses a real backend and tests prove meaningful retrieval improvement.

---

## Phase 2 — Runtime Calls

Implement:

```text
runtime tracer
      ↓
observation buffer
      ↓
symbol resolver
      ↓
SKC edge normalizer
      ↓
CALLS evidence
```

### Exit criterion

A controlled test suite produces runtime-confirmed calls in SKC without duplicate logical edges.

---

## Phase 3 — FLOWS_TO

Implement bounded data-flow analysis.

### Exit criterion

Controlled fixtures produce correct flow paths and provenance.

---

## Phase 4 — Unified RAG

Combine:

```text
lexical
+
semantic
+
graph
+
runtime evidence
+
flow evidence
```

### Exit criterion

An agent context query returns a compact, provenance-aware code context using the existing Contract gate.

---

## Phase 5 — Evaluation

Run the system against a representative StackMind repository slice and record:

- build time
- graph size
- retrieval quality
- runtime tracing overhead
- flow-analysis coverage
- token/context reduction
- false-positive behavior

### Exit criterion

A measured decision is made on whether each capability graduates beyond POC.

---

# 20. Architecture Decision Record

## Decision

**Adopt CGR-inspired capabilities inside SKC; do not adopt CGR as a parallel infrastructure stack.**

## Reasons

1. SKC already owns graph identity and persistence.
2. SKC already owns access control.
3. StackMind already has an external multi-language parsing strategy.
4. A second graph database creates synchronization and identity problems.
5. The most valuable missing CGR capabilities are runtime/data-flow intelligence, not Memgraph/Qdrant themselves.
6. The provider boundary leaves room to use CGR algorithms or tooling later without coupling StackMind to its storage model.

---

# 21. Alternative Options

| Option | Result |
|---|---|
| Full CGR stack | Reject |
| Memgraph + Qdrant beside SKC | Reject |
| CGR only as external RAG service | Reject for current scope |
| Continue SKC + CBM only | Acceptable baseline |
| Implement runtime + flow directly in SKC | **Preferred** |
| Use CGR analyzer as temporary provider | Acceptable fallback |
| Port selected CGR algorithms | Consider after POC benchmarks |

---

# 22. What Should Be Reused From CGR

The project should be treated as a **reference implementation and research source**, not as StackMind's persistence architecture.

Potentially reusable ideas:

- runtime call tracing
- data-flow/taint analysis
- graph-aware retrieval
- MCP exposure patterns
- multi-language analysis techniques

Not reusable as StackMind authorities:

- CGR node IDs
- CGR graph storage
- CGR vector store
- CGR authorization model
- CGR persistence lifecycle

---

# 23. Final POC Success Definition

The POC is successful when StackMind can answer all of the following from its own SKC-backed knowledge layer:

### Structural

> Who calls `AuthService.login`?

### Runtime

> Which callers of `AuthService.login` were actually observed during tests?

### Data flow

> Can request input reach the SQL execution sink?

### Semantic

> What code is semantically relevant to "authentication token refresh"?

### Provenance

> Why does StackMind believe each relationship exists?

### Security

> Can the current agent Contract prevent unauthorized graph/flow retrieval?

And all of those answers must come from:

```text
                 ONE
          authoritative SKC
                 |
       +---------+---------+
       |         |         |
     static    runtime    flow
       |         |         |
       +---------+---------+
                 |
              semantic
                 |
                 v
              Agent
```

**That is the Code-Graph-RAG concept we want in StackMind.**

Not "CGR inside StackMind."

**StackMind becomes its own code-graph-RAG system, with SKC as the foundation.**
