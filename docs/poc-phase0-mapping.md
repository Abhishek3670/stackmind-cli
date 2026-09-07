# POC Phase 0 Mapping: Current SKC Contracts

Work order: WO-029
Agent: codex
Date: 2026-08-18

## Scope and Method

This is a research-only implementation map for the Code-Graph Intelligence POC.
The Knowledge API was attempted first per KNOW-01:

- `stackmind graph stats -p .` timed out after 20 seconds.
- `stackmind graph context "WO-029 POC phase 0 map SKC contracts EdgeIR birth_key KnowledgeAPI EmbeddingBackend AgentContract sharded JSON" --token-budget 3000 -p .` timed out after 60 seconds.

Because the Knowledge API was unavailable locally, this audit read only the
files allowed by `.sync/contracts/WO-029.yaml` and named by the work order.

## POC Targets Mapped to Current Symbols

| POC target | Current symbol | Current location | Current signature or shape | Notes |
| --- | --- | --- | --- | --- |
| Stable symbol identity | `birth_key` | `validators/knowledge/registry.py:59` | `birth_key(path: str, qualified_name: str) -> str` | Canonical key is normalized repo-relative path plus qualified name. |
| Identity digest | `birth_digest` | `validators/knowledge/registry.py:70` | `birth_digest(key: str) -> str` | Full SHA-256 hex digest of the birth key. |
| NodeID minting | `node_id_for` | `validators/knowledge/registry.py:75` | `node_id_for(kind: str, key: str) -> str` | Current format is `PREFIX-` plus first 16 hex chars, not the older underscore sketch. |
| Kind prefixes | `KIND_PREFIXES` | `validators/knowledge/registry.py:21` | `dict[str, str]` | Includes code, runtime, FastAPI, Django, Pydantic, SQLAlchemy, and analysis symbol kinds. |
| Registry lifecycle | `SymbolRegistry` | `validators/knowledge/registry.py:97` | class | `get_or_create`, `upsert`, `mark_obsolete`, and `alias` implement identity lifecycle. |
| Compiler IR | `SymbolIR` | `validators/knowledge/compiler/ir.py:16` | dataclass | Fields: `node_id`, `kind`, `path`, `qualified_name`, `signature`, `location`, `content_hash`, `owner`. |
| Edge IR | `EdgeIR` | `validators/knowledge/compiler/ir.py:42` | dataclass | Fields: `source_id`, `relation`, `target_id`, `target_name`, `resolution`, `confidence`, `path`, `line`. |
| Diagnostic IR | `DiagnosticIR` | `validators/knowledge/compiler/ir.py:68` | dataclass | Non-fatal compiler diagnostic with path, severity, code, message, optional line. |
| Compiler output | `CompilerIR` | `validators/knowledge/compiler/ir.py:88` | dataclass | Holds `revision_inputs`, `symbols`, `edges`, and `diagnostics`; `to_json()` returns byte-stable canonical JSON. |
| Parser symbols | `ParsedSymbol` | `validators/knowledge/compiler/parse.py:18` | dataclass | Parser-side symbol before registry identity is attached. |
| Parser calls | `ParsedCall` | `validators/knowledge/compiler/parse.py:32` | dataclass | Captures source qualified name, target call name, and line. |
| Parser relations | `ParsedRelation` | `validators/knowledge/compiler/parse.py:42` | dataclass | Generic non-call relation with relation string and confidence. |
| Parser entry point | `parse_project` | `validators/knowledge/compiler/parse.py:87` | `parse_project(root: Path) -> list[ParsedFile]` | Parses Python files only. |
| File parser | `parse_file` | `validators/knowledge/compiler/parse.py:93` | `parse_file(path: Path, root: Path) -> ParsedFile` | Emits diagnostics instead of raising syntax errors. |
| Resolver entry point | `compile_project` | `validators/knowledge/compiler/resolve.py:33` | `compile_project(project_path: Path, *, agent: str = "codex", write_registry: bool = True) -> CompilerIR` | Parse, augment, register symbols, resolve edges, return IR. |
| Symbol registration | `_register_symbols` | `validators/knowledge/compiler/resolve.py:97` | `_register_symbols(parsed_files: list[ParsedFile], registry: SymbolRegistry, *, write_registry: bool) -> list[SymbolIR]` | Uses `birth_key`; disambiguates duplicate path+qualname with line suffix. |
| Edge resolution | `_resolve_edges` | `validators/knowledge/compiler/resolve.py:159` | `_resolve_edges(parsed_files: list[ParsedFile], symbols: list[SymbolIR], project_path: Path) -> list[EdgeIR]` | Resolves calls and generic parsed relations into `EdgeIR`. |
| Call resolver | `_resolve_call` | `validators/knowledge/compiler/resolve.py:203` | `_resolve_call(call: ParsedCall, parsed: ParsedFile, source_symbol: SymbolIR, by_path_qual: dict[tuple[str, str], SymbolIR], by_global_name: dict[str, SymbolIR], module_names: set[str], project_path: Path) -> EdgeIR` | Emits `CALLS` as resolved, external, or unresolved. |
| Generic relation resolver | `_resolve_relation` | `validators/knowledge/compiler/resolve.py:292` | `_resolve_relation(relation: ParsedRelation, source_symbol: SymbolIR, by_path_qual: dict[tuple[str, str], SymbolIR], by_global_name: dict[str, SymbolIR]) -> EdgeIR` | Passes through frontend relation strings; no `EdgeKind` enum. |
| Node JSON persistence | `symbol_document` | `validators/knowledge/storage.py:50` | `symbol_document(symbol: SymbolIR, edges: list[EdgeIR]) -> dict[str, Any]` | Persists deterministic fields and inline outgoing edges under each node. |
| Node document builder | `build_node_documents` | `validators/knowledge/storage.py:69` | `build_node_documents(ir: CompilerIR) -> dict[str, dict[str, Any]]` | Produces one document per symbol keyed by node ID. |
| Revision document builder | `build_revision_document` | `validators/knowledge/storage.py:73` | `build_revision_document(ir: CompilerIR, number: int, parent: int | None, built_at: str) -> dict[str, Any]` | Stores build metadata and diagnostics, not full graph payload. |
| Disk IR reader | `read_ir` | `validators/knowledge/storage.py:90` | `read_ir(project_path: Path) -> CompilerIR` | Rehydrates IR by scanning node documents and inline outgoing edges. |
| Reverse index projection | `build_reverse_index_documents` | `validators/knowledge/projections/reverse_index.py:27` | `build_reverse_index_documents(ir: CompilerIR) -> dict[str, dict[str, Any]]` | Builds target-sharded inbound edge cache. |
| Reverse index lookup | `lookup_reverse_edges` | `validators/knowledge/projections/reverse_index.py:88` | `lookup_reverse_edges(project_path: Path, target_id: str, *, relation: str | None = None) -> list[dict[str, Any]]` | Enables direct inbound lookups without scanning all node files. |
| Query API | `KnowledgeAPI` | `validators/knowledge/api.py:125` | class | Read-only query surface over compiled knowledge. |
| Lookup | `KnowledgeAPI.lookup` | `validators/knowledge/api.py:166` | `lookup(needle: str, *, limit: int = 10, contract: AgentContract | str | Path | None = None) -> KnowledgeEnvelope` | Resolves by NodeID, birth key, current name, or alias. |
| Filter | `KnowledgeAPI.filter` | `validators/knowledge/api.py:199` | `filter(*, query: str = '', kind: str | None = None, path_contains: str | None = None, qualified_name_contains: str | None = None, limit: int = 10, contract: AgentContract | str | Path | None = None) -> KnowledgeEnvelope` | Attribute filter over active nodes. |
| Traverse | `KnowledgeAPI.traverse` | `validators/knowledge/api.py:258` | `traverse(target: str, *, relation: str | None = None, direction: str = 'outbound', depth: int = 1, limit: int = 25, contract: AgentContract | str | Path | None = None) -> KnowledgeEnvelope` | Bounded inbound/outbound graph traversal. |
| Search | `KnowledgeAPI.search` | `validators/knowledge/api.py:342` | `search(query: str, *, limit: int = 10, query_embedding: Sequence[float] | None = None, contract: AgentContract | str | Path | None = None) -> KnowledgeEnvelope` | Uses semantic search when a query embedding is supplied and cached vectors exist; otherwise uses text search. |
| Callers | `KnowledgeAPI.callers` | `validators/knowledge/api.py:390` | `callers(target: str, *, limit: int = 25, contract: AgentContract | str | Path | None = None) -> KnowledgeEnvelope` | Direct inbound `CALLS` traversal. |
| Impact | `KnowledgeAPI.impact` | `validators/knowledge/api.py:407` | `impact(target: str, *, depth: int = 3, limit: int = 50, contract: AgentContract | str | Path | None = None) -> KnowledgeEnvelope` | Transitive inbound `CALLS` traversal. |
| Explain | `KnowledgeAPI.explain` | `validators/knowledge/api.py:425` | `explain(target: str, *, contract: AgentContract | str | Path | None = None) -> dict[str, Any]` | Returns node plus inbound/outbound deterministic facts. |
| Context assembly | `KnowledgeAPI.assemble_context` | `validators/knowledge/api.py:471` | `assemble_context(query: str, *, token_budget: int = 1200, limit: int = 8, query_embedding: Sequence[float] | None = None, contract: AgentContract | str | Path | None = None) -> ContextBundle` | Prompt bundle from lookup/search seeds plus `CALLS` neighbors. |
| Semantic search helper | `KnowledgeAPI._semantic_search` | `validators/knowledge/api.py:733` | `_semantic_search(query_embedding: Sequence[float], *, limit: int, contract: AgentContract | str | Path | None = None) -> tuple[KnowledgeResult, ...]` | Reads embedding cache by deterministic content hash. |
| Embedding request | `EmbeddingRequest` | `validators/knowledge/enricher.py:86` | dataclass | Fields: `node_id`, `content_hash`, `privacy_mode`, `text`. |
| Embedding response | `EmbeddingResponse` | `validators/knowledge/enricher.py:94` | dataclass | Fields: `vector`, `dimensions`, `tokens_used`, `model`. |
| Embedding backend | `EmbeddingBackend` | `validators/knowledge/enricher.py:116` | `class EmbeddingBackend(Protocol): embed(self, request: EmbeddingRequest) -> EmbeddingResponse` | Protocol is in `enricher.py`, not `search.py`. |
| Embedding cache path | `embedding_cache_path` | `validators/knowledge/enricher.py:385` | `embedding_cache_path(project_path: Path, content_hash: str) -> Path` | Current cache is `.sync/knowledge/cache/embeddings/<first2>/<content_hash>.json`. |
| Contract gate | `AgentContract` | `validators/knowledge/contract.py:43` | class | Loads schema-validated contract data and enforces allow/deny rules. |
| Contract loader | `AgentContract.load` | `validators/knowledge/contract.py:59` | `load(path_or_data: Union[str, Path, Dict[str, Any]], project_path: Path) -> AgentContract` | Validates against `schemas/contract.schema.json`. |
| Contract scope check | `AgentContract.is_node_in_scope` | `validators/knowledge/contract.py:125` | `is_node_in_scope(node_id: str, ir: Any) -> bool` | Fail-closed node access check using deny precedence and allow-depth BFS. |
| CBM compiler | `cbm_compiler.py` | `validators/knowledge/compiler/cbm_compiler.py:1` | deleted stub | File contains only a comment: tree-sitter adapter replaced by static AST parsing. |

## Current IR Contracts

`validators/knowledge/compiler/ir.py` defines no edge enum. `EdgeIR.relation`
is a plain `str` at line 46, and relation names are emitted as string constants
by the resolver and domain augmenters.

`SymbolIR` at line 16 is the stable code-node IR shape. The required identity
fields are `node_id`, `kind`, `path`, and `qualified_name`; deterministic
content is represented by `signature`, `location`, and `content_hash`; ownership
is optional through `owner`.

`EdgeIR` at line 42 stores source ID, optional target ID, target display name,
resolution tier, confidence, and source location. `ResolutionTier` is a literal
union at line 9 with values `RESOLVED`, `EXTERNAL`, and `UNRESOLVED`.

`CompilerIR` at line 88 is the canonical in-memory compile artifact. Its
`to_dict()` method sorts symbols, edges, and diagnostics before serialization,
and `to_json()` at line 124 emits canonical JSON for compile-twice comparison.

## Relation Strings Observed

There is no central `EdgeKind` enum. Relation strings are introduced at the
compiler frontend/augmenter layer and passed through `ParsedRelation` into
`EdgeIR`.

Observed relation strings across `validators/knowledge/compiler/**`:

- `CALLS`
- `CONTAINS_SECTION`
- `CONFIG_DEFINES_ENV`
- `CONFIG_DEFINES_SERVICE`
- `CONFIG_REQUIRES_DEP`
- `CIRCULAR_DEPENDENCY`
- `COVERAGE_COVERS`
- `DECLARES_ASSOCIATION_TABLE`
- `DECLARES_CELERY_TASK`
- `DECLARES_COLUMN`
- `DECLARES_DJANGO_FIELD`
- `DECLARES_FIELD`
- `DECLARES_RELATIONSHIP`
- `DECLARES_VALIDATOR`
- `DEPENDENCY_DEPENDS_ON`
- `DEPENDS_ON_MIGRATION`
- `DEPENDS_TARGET`
- `DOC_LINKS_TO`
- `DOCUMENTS_SYMBOL`
- `FOREIGN_KEY`
- `HANDLES`
- `HAS_CONFIG`
- `HAS_DJANGO_META`
- `HAS_HEALTH_METRICS`
- `INCLUDES_URLCONF`
- `INHERITS`
- `IS_DEAD_CODE`
- `JOB_CONTAINS_STEP`
- `JOB_DEPENDS_ON_JOB`
- `MANAGES_MODEL`
- `PART_OF_CYCLE`
- `PIPELINE_CONTAINS_JOB`
- `RECEIVES_SIGNAL`
- `REFACTORING_AFFECTS`
- `RELATES_TO`
- `ROUTE_DEPENDS_ON`
- `SCHEDULES_TASK`
- `SERIALIZES_MODEL`
- `SIGNAL_SENDER`
- `STEP_RUNS_COMMAND`
- `SUITE_CONTAINS_CASE`
- `TESTS_SYMBOL`
- `TRIGGERS_CELERY_TASK`
- `URL_HANDLES`
- `USES_AUTH`
- `USES_MIDDLEWARE`
- `USES_REQUEST_MODEL`
- `USES_RESPONSE_MODEL`
- `VALIDATES`
- `WRAPS_FUNCTION`

Notably absent for the upcoming POC WOs: `FLOWS_TO`.

## Current Sharded JSON Format

The current persistent graph layout differs from the older POC draft that
described `.sync/knowledge/edges/CALLS.json` and `.sync/knowledge/graph-revisions`.
The actual storage helpers write and read this shape:

```text
.sync/knowledge/
  registry/
    <bucket>/<NODE_ID>.json
  nodes/
    <kind>/<bucket>/<NODE_ID>.json
  revisions/
    REV-0000000001.json
  cache/
    reverse_index/
      manifest.json
      <bucket>/<TARGET_NODE_ID>.json
    embeddings/
      <first2>/<content_hash>.json
```

Node bucket selection is `node_id.split("-", 1)[1][:2]`
(`validators/knowledge/storage.py:23`). Node paths are
`.sync/knowledge/nodes/<kind>/<bucket>/<node_id>.json`
(`validators/knowledge/storage.py:27`).

Each node document is produced by `symbol_document()` at
`validators/knowledge/storage.py:50`:

```json
{
  "ai": {},
  "deterministic": {
    "content_hash": "...",
    "location": {"line": 1, "column": 0, "end_line": 1, "end_column": 0},
    "outgoing": [],
    "owner": null,
    "path": "validators/knowledge/api.py",
    "qualified_name": "KnowledgeAPI.lookup",
    "signature": "def lookup(self, needle, *...)"
  },
  "kind": "Function",
  "node_id": "FUNC-...",
  "status": "active"
}
```

Edges are stored inline under `deterministic.outgoing` in the source node file,
not in a global edge-type file. `read_ir()` at
`validators/knowledge/storage.py:90` reconstructs `CompilerIR` by loading
`nodes/*/*/*.json`, rebuilding `SymbolIR` from deterministic node fields, and
expanding each node's `deterministic.outgoing` into `EdgeIR`.

Revision files use `.sync/knowledge/revisions/REV-<10 digit id>.json`
(`validators/knowledge/storage.py:31`) and contain build metadata:
`built_at`, `diagnostics`, `id`, `parent`, `revision_inputs`, and
`schema_version` (`validators/knowledge/storage.py:73`).

The reverse index is a derived cache under
`.sync/knowledge/cache/reverse_index/`. `build_reverse_index_documents()` at
`validators/knowledge/projections/reverse_index.py:27` emits a
`manifest.json` plus one inbound-edge file per target:

```json
{
  "inbound": [
    {
      "confidence": 1.0,
      "line": 10,
      "path": "some/file.py",
      "relation": "CALLS",
      "resolution": "RESOLVED",
      "source_id": "FUNC-...",
      "target_name": "target.qualname"
    }
  ],
  "projector": "reverse_index",
  "target_id": "FUNC-...",
  "version": "reverse-index-1"
}
```

## Identity Chain

The current identity chain is:

1. `birth_key(path, qualified_name)` at `validators/knowledge/registry.py:59`
   normalizes the path and returns `"<path>:<qualified_name>"`.
2. `birth_digest(key)` at `validators/knowledge/registry.py:70` returns the
   SHA-256 hex digest of that key.
3. `node_id_for(kind, key)` at `validators/knowledge/registry.py:75` returns
   `"<kind_prefix>-<first16 digest chars>"`.
4. `SymbolRegistry.get_or_create()` at `validators/knowledge/registry.py:156`
   uses the birth key to reuse an active record or mint a new node ID.
5. `_register_symbols()` at `validators/knowledge/compiler/resolve.py:97`
   binds parsed symbols to registry records, then constructs `SymbolIR`.

The work order note is correct: `birth_key()` is in
`validators/knowledge/registry.py`, not `cbm_models.py`.

## Compiler Pipeline

Current pipeline:

1. `compile_project()` parses all Python files through `parse_project()` at
   `validators/knowledge/compiler/resolve.py:46`.
2. Domain augmenters append additional `ParsedSymbol` and `ParsedRelation`
   objects (`resolve.py:47-60`).
3. Registry writes are wrapped by the runtime lock only during symbol
   registration (`resolve.py:68-79`).
4. `_register_symbols()` maps parser symbols to stable `SymbolIR` objects
   (`resolve.py:97-156`).
5. `_resolve_edges()` resolves `ParsedCall` and `ParsedRelation` records into
   `EdgeIR` (`resolve.py:159-200`).
6. `_resolve_call()` emits `CALLS` edges as:
   - local `RESOLVED` with confidence `1.0`;
   - global/imported repo `RESOLVED` with confidence `0.95`;
   - imported external `EXTERNAL` with confidence `1.0`;
   - otherwise `UNRESOLVED` with confidence `0.0`.
7. `_resolve_relation()` passes through relation strings from domain augmenters,
   resolving local/global targets or marking common framework targets as
   `EXTERNAL`.

The parser currently uses Python `ast`, not LibCST/tree-sitter. `ParsedFile`
captures symbols, calls, relations, imports, and diagnostics.

## Knowledge API Contracts

`KnowledgeAPI` is the current graph API. It lives in
`validators/knowledge/api.py`, not a `validators/knowledge/graph/query.py`
subdirectory. Public methods:

- `lookup()` resolves one symbol by NodeID, birth key, current name, or alias.
- `filter()` scans deterministic node attributes with kind/path/name filters.
- `traverse()` performs bounded inbound or outbound edge traversal.
- `search()` performs semantic search when the caller supplies a query vector
  and cached embeddings exist; otherwise it falls back to text search.
- `callers()` is inbound depth-1 traversal over relation `CALLS`.
- `impact()` is transitive inbound traversal over relation `CALLS`.
- `explain()` returns one node plus deterministic inbound and outbound facts.
- `assemble_context()` builds a token-bounded `ContextBundle` from lookup/search
  seeds plus direct caller/callee neighbors.

All public methods accept an optional `contract` parameter except constructor
setup. The instance can also be initialized with a contract. Enforcement happens
through `_check_expiration()` and `_enforce_node()`.

## Embedding and Semantic Search Contracts

The embedding protocol is in `validators/knowledge/enricher.py`:

- `EmbeddingRequest` at line 86 carries `node_id`, `content_hash`,
  `privacy_mode`, and `text`.
- `EmbeddingResponse` at line 94 carries vector, dimensions, token count, and
  model.
- `EmbeddingBackend` at line 116 is a `Protocol` with
  `embed(self, request: EmbeddingRequest) -> EmbeddingResponse`.

The cache path is implemented by `embedding_cache_path()` at
`validators/knowledge/enricher.py:385` as:

```text
.sync/knowledge/cache/embeddings/<content_hash first 2 chars>/<content_hash>.json
```

`KnowledgeAPI._semantic_search()` at `validators/knowledge/api.py:733` loops
over active registry records, enforces contract scope, loads the node document,
reads the embedding cache for the node `content_hash`, computes cosine
similarity, and returns `KnowledgeResult` values with reason `semantic-search`.

## Contract Gate

`AgentContract` is in `validators/knowledge/contract.py`, not in the harness
layer. It validates YAML/JSON contract data against `schemas/contract.schema.json`
in `AgentContract.load()` at line 59.

Contract enforcement model:

- `ContractAccessDenied` and `ContractExpiredError` are defined at lines 16 and
  20.
- `AgentContract.is_expired()` at line 96 checks optional
  `budget.expires_at`.
- `AgentContract.is_node_in_scope()` at line 125 fails closed if the node is
  missing, applies deny rules first, then applies allow rules using module
  matching plus graph-distance BFS.
- `KnowledgeAPI._enforce_node()` at `validators/knowledge/api.py:151` raises
  contract errors before returning scoped node data.

One implementation detail to verify before relying on it for current v3
contracts: the `AgentContract` class expects schema fields such as `agent_id`
at `contract.py:48`, while `WO-029.yaml` uses `assigned_agent`. That may be a
schema/version mismatch in the contract layer or this specific contract file.

## POC Assumptions vs Actual Code

Confirmed differences between the POC assumptions and the current repository:

- `birth_key()` is in `validators/knowledge/registry.py:59`, not
  `cbm_models.py`.
- `EmbeddingBackend` is in `validators/knowledge/enricher.py:116`, not
  `search.py`.
- The graph API is `KnowledgeAPI` in `validators/knowledge/api.py:125`, not
  `validators/knowledge/graph/query.py`.
- There is no `validators/knowledge/graph/` subdirectory in the current file
  list.
- `EdgeIR.relation` is a raw string field at
  `validators/knowledge/compiler/ir.py:46`; there is no `EdgeKind` enum.
- `validators/knowledge/compiler/cbm_compiler.py` is a one-line deleted stub.
- Current storage keeps outgoing edges inline in node documents and inbound
  edges in a derived reverse-index cache. It does not use the older
  `.sync/knowledge/edges/<RELATION>.json` canonical layout.
- Current embedding cache is under `.sync/knowledge/cache/embeddings/`, while
  older docs mention `.sync/cache/embeddings/`.
- The parser is the built-in Python `ast` frontend. The older LibCST/tree-sitter
  language is not the current implementation contract.
- `FLOWS_TO` does not exist yet among compiler relation strings.
- Runtime artifact node support exists at the identity prefix level
  (`workorder`, `decision`, `review`, `issue`), but this WO did not confirm a
  full runtime event compiler implementation.

## Suggested Integration Map for Follow-on WOs

This section maps likely follow-on POC changes to concrete current extension
points:

- Evidence model: extend node `ai` or metadata schemas and storage contracts
  around `validators/knowledge/storage.py:50` and `schemas/knowledge/*.json`.
- Runtime tracing: add deterministic frontend/augmenter output as
  `ParsedRelation` records, then resolve through `_resolve_relation()` in
  `validators/knowledge/compiler/resolve.py:292`.
- `FLOWS_TO`: introduce it as another relation string emitted by an augmenter;
  no enum update is required, but tests should assert the exact string.
- Embedding provider: implement `EmbeddingBackend` from
  `validators/knowledge/enricher.py:116` and write cache-compatible vectors
  through existing `KnowledgeEnricher` flow.
- Unified RAG/query: build on `KnowledgeAPI.search()` and
  `KnowledgeAPI.assemble_context()` in `validators/knowledge/api.py`.
- Contract-bounded reads: pass `contract=` into `KnowledgeAPI` calls and expect
  `ContractAccessDenied` for out-of-scope nodes.
