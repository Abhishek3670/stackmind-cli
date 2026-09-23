# Integrating Code-Graph-RAG into StackMind PKG

**Executive Summary:** We propose using **Code-Graph-RAG** as the code-intelligence engine within StackMind’s knowledge system. Code-Graph-RAG parses a monorepo with Tree‑sitter, builds a unified graph of code structure in **Memgraph**, and supports semantic search via **Qdrant**. It complements StackMind’s **Symbol Knowledge Compiler (SKC)** by supplying a rebuildable projection of code symbols, calls, imports, inheritance, and data flows. In our design, **StackMind’s SKC** remains authoritative (events from `.sync/` → normalization → SKC), and Code-Graph-RAG acts as a *compiled projection* of the codebase. Agents query SKC and/or Code-Graph-RAG through a unified interface. This report details the integration architecture, schema mappings, APIs (with sample Cypher/vector queries), a phased roadmap, CI/test strategy, deployment templates, and security/risk considerations.

## 1. Integration Architecture

```mermaid
flowchart LR
    A[StackMind .sync (Project State)] -->|Events| B[StackMind SKC (Knowledge Compiler)]
    B -->|Trigger parsing| C[Code-Graph-RAG Ingestion]
    C -->|Graph Updates| D[Memgraph (Code Graph DB)]
    C -->|Embeddings| E[Qdrant (Vector DB)]
    B -->|Agent queries| G[Unified Knowledge API]
    D -->|Cypher queries| G
    E -->|Vector queries| G
    G --> H[Agents / RAG]
```

1. **Ingestion Pipeline:** When code changes or new repos arrive via StackMind’s `.sync/`, the SKC normalizes events and invokes the Code-Graph-RAG loader (e.g. via its CLI `cgr start` or Python SDK). This populates/upserts the Memgraph graph and Qdrant index.
2. **Graph Schema:** Code-Graph-RAG uses a unified schema (see Node/Edge tables below) to represent code structure. Each code *symbol* (function, class, file, etc.) is a node; relationships encode calls, imports, inheritance, etc.
3. **Symbol Mapping:** To maintain StackMind’s stable IDs, each code node in Memgraph should include a property for the SKC symbol ID (and project name) so SKC ↔ CGR can be joined. Conversely, SKC can expose a symbol registry API that Code-Graph-RAG queries to link new nodes to existing symbols (or assign new IDs).
4. **Query/Serving:** StackMind’s Agents issue code-intelligence queries via the SKC’s Knowledge API. The SKC delegates code-specific queries to the Code-Graph-RAG engine: either by translating agent requests to Cypher (Memgraph) or by performing vector search (Qdrant) and combining results (RAG). The Cypher interface (Bolt port 7687) and Qdrant REST/GRPC API run on local Docker/K8s services.
5. **Feedback Loops:** Optionally, code edits suggested or made by agents (via SKC workflows) can feed back into SKC and trigger re-indexing. We must ensure SKC remains authoritative; any code changes occur via PRs/commits (reflected back through `.sync/`).

This integration ensures StackMind “knows why/how” (decisions, agents, context) at the SKC level, while Code-Graph-RAG provides “what/where” code-level details.

## 2. Schema Mapping: SKC ↔ Code-Graph-RAG

We assume SKC’s symbol model (unknown exact schema) has concepts like *Project*, *Symbol* (function/class/interface), *File/Module*, *Agent*, *Decision*, etc. We map the relevant code entities:

| **Code-Graph-RAG Node** | **SKC Concept**            | **Notes**                                                    |
|-------------------------|----------------------------|--------------------------------------------------------------|
| *Project*               | Project                    | Code-Graph-RAG `Project` node = repo identifier. Align SKC project names. |
| *Package/Folder*        | Code Namespace / Package   | Logical group; SKC may or may not track separately.         |
| *File/Module*           | Source File / Document     | SKC “document” or file symbol. Include path for identity.  |
| *Class / Interface / Enum / Type / Union* | Symbol (Class/Interface) | SKC symbols for types. Inherit `qualified_name`, decorators. |
| *Function / Method*     | Symbol (Function/Method)   | SKC function/method symbols. Methods have `is_property` flag. |
| *ModuleInterface / ModuleImplementation* | (Advanced DSL) | Rare SKC might ignore. Advanced patterns.               |
| *ExternalModule/Package*| External dependency        | SKC may track external libs differently. CGR marks imports as external. |
| *Resource*              | Environment/Resource       | External I/O targets (files, ENV). SKC could link these to requirements/decisions. |
| *Pattern / CodeSmell*   | Analysis Finding (AST)     | Code-Graph-RAG AST-grep findings (opt-in). SKC could map to issues or code smells. |

**Node Properties:** All CGR nodes carry `qualified_name`, `path`, `absolute_path`, etc.. SKC should use these to identify symbols (e.g. Python function `foo` at `a.py:10-20`).

| **Code-Graph-RAG Relationship**     | **SKC Edge**         | **Notes**                                                   |
|-------------------------------------|----------------------|-------------------------------------------------------------|
| `CONTAINS_*` (Project→Package/Folder/File/Module) | containment hierarchy | SKC may already track files and directories; CGR links project→files. |
| `DEFINES` (Module → Class/Function/Method) | symbol definition  | SKC symbol registry mapping (where is class/method defined). |
| `CALLS` (Function/Method → Function/Method) | calls/call graph    | SKC call edges. CGR marks static calls (with runtime provenance later). |
| `IMPORTS` (Module → Module/ExternalModule) | imports/includes    | SKC import edges; used to find dependencies.                |
| `INHERITS` / `IMPLEMENTS` | inheritance / interface implementation | SKC inheritance edges.                                      |
| `OVERRIDES` | method override         | SKC method override relationships.                          |
| `READS_FROM` / `WRITES_TO` | I/O edges            | SKC capture of external IO. (opt-in CGR)                   |
| `FLOWS_TO`     | data-flow / taint      | SKC data-flow tracking (opt-in CGR).         |
| `REFERENCES`   | references/uses       | Non-call usage of symbols.                                  |
| `DEPENDS_ON_EXTERNAL` | external dependency | SKC dependency on external package.                         |
| Other: `EXPORTS`, `PATTERN`, `SMELL`, etc. | non-core           | Likely no direct SKC analogue; may map to documentation or code analysis features.

This mapping table helps align SKC’s internal graph with Code-Graph-RAG’s. Each SKC edge should correspond to one or more CGR relations (e.g. a SKC “calls” link ↔ `CALLS` edge in Memgraph).

## 3. API Integration and Queries

**API Contracts:** We expose a **StackMind Knowledge API** endpoint (e.g. `/code-query`) that wraps Code-Graph-RAG functions. Under the hood:

- **Graph Query (Cypher):** The API will accept structured queries (or NL converted to Cypher via CGR’s CypherGenerator) and forward to Memgraph. For example, to find callers of a function:
  ```cypher
  MATCH (f:Function {qualified_name: "myapp.service.AuthService.authenticate"})
  <-[:CALLS]- (caller)
  RETURN caller.qualified_name, caller.absolute_path
  LIMIT 10;
  ```
  This returns all functions calling `authenticate()`. The StackMind agent can formulate such queries in NL or DSL, or use CGR’s `CypherGenerator` module.

- **Vector Semantic Search:** For intent-based search (GraphRAG), StackMind agents can use CGR’s `embed_code` or call Qdrant directly. Example: find functions similar to description “validate user token”:
  ```python
  from cgr import embed_code
  client = QdrantClient(host="localhost", port=6333, api_key="READ_ONLY_KEY")
  result = client.search(
      collection_name="code_embeddings",
      query_text="validate JWT token",
      top=5
  )
  ```
  This uses the UniXcoder embeddings CGR built for each function/class. Results return nearest code nodes.

- **Combining Results (RAG):** Agents may use both Cypher and vector results to answer questions. For example, “Show me where authentication happens” could:
  1. Vector-search “authenticate”, get candidate functions (IDs).
  2. For each, do a `MATCH (f) WHERE id(f)=X RETURN f.source_code` via Memgraph.
  3. Or ask CGR’s NLP-to-Cypher to traverse `CALLS` edges from known auth functions.

- **Example Cypher Query (Multihop):**  
  *“Which modules call the payment API?”*  
  ```cypher
  MATCH (mod:Module)-[:CALLS*1..2]->(api:Function {name: "processPayment"})
  RETURN DISTINCT mod.name, mod.absolute_path;
  ```
  
- **Example Qdrant Query (CGR’s Semantic Search):**  
  StackMind agent code (via CGR SDK):  
  ```python
  from cgr import MemgraphIngestor, embed_code
  mg = MemgraphIngestor(host="localhost", port=7687)
  # Semantic rank by title
  scores = embed_code.search("connect to database", top_k=5)  
  # Then retrieve matching code
  for id in scores.ids:
      code = mg.execute(f"MATCH (f) WHERE id(f)={id} RETURN f.source_code").records()
      print(code)
  ```

**API Contracts:** SKC ↔ CGR integration should define:
- HTTP/CLI contract for SKC to trigger `cgr start`, e.g. an SKC event handler calls `cgr start --repo-path ... --update-graph`.
- GRPC/Bolt for queries: SKC uses CGR Python SDK (`MemgraphIngestor`, `CypherGenerator`, `embed_code`) to interact with Memgraph/Qdrant.
- Responses: Return JSON with node properties or code snippets, including provenance (static/dynamic, file/line info). CGR nodes have metadata (start_line, end_line) we should surface.

## 4. Implementation Roadmap

| **Phase** | **Tasks (Milestones)** | **Effort** | **Rollback** |
|---|---|---|---|
| **1. Environments** | - Install/launch Memgraph and Qdrant (Docker or K8s).<br>- Configure Memgraph (user/password) and Qdrant (API key, TLS). | Low | Stop containers; revert config. |
| **2. Initial Parsing** | - Run `cgr start` on a sample project to populate graph. Verify Memgraph has nodes (via `CALL (gql)SHOW LABELS`).<br>- Ingest CGR example code into StackMind’s test workspace. | Medium | Flush Memgraph data; revise config. |
| **3. SKC Integration** | - Extend SKC ingestion pipeline to invoke Code-Graph-RAG: on code push event, call `cgr update` (or use CGR Python `GraphLoader`).<br>- Store resulting node metadata (including CGR node IDs or symbol IDs) in SKC’s normalized event log. | High | Can disable CGR calls; revert to static graph. |
| **4. Symbol Registry Sync** | - Modify SKC to assign stable symbol IDs to CGR nodes: e.g. embed SKC’s `symbol_id` as a property in Memgraph nodes (project + qualified_name as key).<br>- Ensure renames/moves preserve ID by updating CGR node (via `LOAD CSV` or using `MERGE`). | High | If mismatches, isolate SKC→CGR linkage; use name-based fallback. |
| **5. Query API** | - Implement StackMind Knowledge-API endpoints for code queries.<br>- Use CGR SDK in service layer (e.g. `cgr.MemgraphIngestor`, `cgr.CypherGenerator`, `cgr.embed_code`).<br>- Write example queries and validate results (e.g. “find callers of X”). | Medium | Provide documentation; treat as optional feature if unstable. |
| **6. Testing & CI** | - Unit tests for mapping logic (e.g. SKC event → CGR ingest command).<br>- Integration tests: spin up Memgraph/Qdrant in CI (via Docker Compose) and run `cgr start` on a tiny repo. Verify expected nodes/edges.<br>- Security tests: confirm memgraph requires auth, Qdrant rejects unauthorized. | Medium | Disable failing tests; run CGR in a separate pipeline. |
| **7. Documentation & Training** | - Document integration points, API usage, query patterns.<br>- Train team on using GraphRAG tools (e.g. `cgr interactive`). | Low | Use alternate tooling (e.g. Graphify) as fallback. |

Each task should have clear success criteria (graph populated, queries returning correct info) and metrics (indexing time, query latency). A rollback plan is to isolate the code-intel feature (e.g. an SKC feature flag) so that if issues occur, StackMind can function without it, albeit with reduced code intelligence.

## 5. Testing and CI

**CI Environment:** Use Docker Compose with services for Memgraph and Qdrant (see Infra below). In CI:

- **Unit Tests:** Mock SKC events and verify they invoke the correct `cgr` commands or SDK calls. Example: given a dummy repo path, ensure `cgr start` is executed.
- **Integration Tests:**  
  - **Graph Build:** Include a small sample repo (e.g. a few Python files) in test fixtures. Pipeline should run `cgr start` and then run Cypher queries to assert nodes/edges exist.  
  - **Query Tests:** Use CGR’s Python SDK to query the live Memgraph. Example test: ensure function `foo()` was indexed (`MATCH (f:Function {name:"foo"}) RETURN f` yields count >0).  
  - **Vector Search:** Index a few code snippets and test that semantic search returns the correct function.  
- **Security Tests:** Verify that Memgraph rejects connections without credentials (if enabled) and Qdrant rejects unauthorized API keys.  
- **Performance:** Automated tests to measure indexing time for, say, 100 small files (<500 lines) to gauge scale.

Sample **unit test case** (Python/PyTest style):
```python
def test_cycle_graph_update(tmp_path):
    # Setup: create a sample Python file
    repo_dir = tmp_path / "proj"; repo_dir.mkdir()
    file = repo_dir / "a.py"
    file.write_text("def add(x,y): return x+y")
    # Invoke SKC integration point (pseudo-code)
    result = call_stackmind_ingest(repo_path=str(repo_dir))
    assert result.success
    # Verify: query memgraph for the function node
    mg = MemgraphIngestor(host="localhost", port=7687)
    res = mg.execute("MATCH (f:Function {name: 'add'}) RETURN count(f) AS cnt")
    assert int(res.records()[0]['cnt']) == 1
```

If integrating into GitHub Actions or similar, use services:
```yaml
services:
  memgraph:
    image: memgraph/memgraph:latest
    ports: ["7687:7687"]
    environment:
      - MEMGRAPH_USER=test
      - MEMGRAPH_PASSWORD=pass
  qdrant:
    image: qdrant/qdrant:latest
    ports: ["6333:6333"]
    environment:
      - QDRANT_API_KEY=secretkey
```
And run a test script that uses the CGR CLI and SDK.

## 6. Infrastructure Templates

### Docker Compose

```yaml
version: '3.8'
services:
  memgraph:
    image: memgraph/memgraph:latest
    container_name: memgraph
    ports:
      - "7687:7687"  # Bolt (graph queries)
      - "7444:7444"  # HTTP (Memgraph Lab / logs, optional)
    environment:
      - MEMGRAPH_USER=admin
      - MEMGRAPH_PASSWORD=admin_pass
    volumes:
      - mg_db:/var/lib/memgraph
  qdrant:
    image: qdrant/qdrant:latest
    container_name: qdrant
    ports:
      - "6333:6333"  # HTTP API
      - "6334:6334"  # gRPC
    environment:
      - QDRANT_API_KEY=YOUR_API_KEY
      - QDRANT_STORAGE_TYPE=rocksdb
    volumes:
      - qdrant_storage:/qdrant/storage
volumes:
  mg_db:
  qdrant_storage:
```

- **Memgraph:** We bind Bolt port and set `MEMGRAPH_USER`/`MEMGRAPH_PASSWORD` to enforce login. Data persists in `mg_db`. 
- **Qdrant:** We set an API key and persistent volume. (By default, Qdrant open source has no auth; here we configure it via `QDRANT_API_KEY` or later enable JWT).

### Kubernetes (Helm/sketch)

In Kubernetes, run Memgraph and Qdrant as StatefulSets/Deployments with similar env vars. Ensure `readinessProbe` checks. Use `NetworkPolicy` to restrict access to these services to StackMind pods only. If hosted, use managed/cloud options (Memgraph Cloud, Qdrant Cloud) for ease.

## 7. Security & Privacy Checklist

- **Authentication & Access Control:**  
  - **Memgraph:** Enable authentication. Use `MEMGRAPH_USER`/`MEMGRAPH_PASSWORD` (Community) or RBAC (Enterprise). Create a read-only role for query agents if needed.  
  - **Qdrant:** Require API keys/TLS. As [34] warns, default Qdrant is open, so explicitly set an `api-key` and restrict by collection. Use **read-only keys** for agents and admin keys only for ingestion.  
  - **Network Restrictions:** Bind services to internal network interfaces only. For Docker, map to `127.0.0.1` or private subnets (Qdrant [34†L274-L283]). In K8s, use internal ClusterIP.  
  - **TLS/Encryption:** Encrypt Bolt and Qdrant endpoints. Use TLS for Memgraph (supported via bolt-over-SSL) and TLS for Qdrant API.  
- **Data Privacy:**  
  - **Code Sensitivity:** The code graph contains proprietary code structure. Ensure it does not leak outside org. Do not connect Memgraph/Qdrant to the public internet.  
  - **Audit Logging:** Enable Memgraph audit logs and Qdrant audit (if available) to track queries.  
  - **Data Retention:** Define how long to keep old graphs. E.g. purge or archive graphs for inactive projects to reduce risk.  
- **Agent Controls:**  
  - Agents querying code graph must be authenticated in StackMind. Reject unauthenticated API calls.  
  - If enabling AI code editing, require human review (approval workflows) to avoid unwanted code generation.  
- **Compliance:**  
  - If StackMind is used in regulated environments, treat code as confidential data. Follow similar policies (encryption at rest, regular audits).  
- **Dependency Security:**  
  - Code-Graph-RAG, Memgraph, Qdrant should be up-to-date to avoid vulnerabilities. Use official images and check for CVEs.  
- **Backup and Recovery:**  
  - Regularly back up Memgraph and Qdrant data (to secure storage). Have a tested recovery plan.  
- **Third-Party / Open Source Risk:**  
  - Code-Graph-RAG is open-source MIT license. Ensure usage complies with policies. Qdrant and Memgraph similarly.  
- **Per-Governance:**  
  - If projects have export-control or IP restrictions, ensure code graph data (structure, names) is handled accordingly (it’s code structure, so likely not separate IP, but caution if code is secret).

## 8. Risks and Mitigations

| **Risk**                              | **Impact**                 | **Mitigation**                                             |
|---------------------------------------|----------------------------|------------------------------------------------------------|
| *Graph Update Failure*<br>(e.g. parsing error) | Outdated or missing code graph. Agents get wrong answers. | SKC should catch failures and log them. Fallback: use last known graph snapshot. Validate changes before commit (unit tests). |
| *Stale Symbol Links*<br>(IDs mismatch) | SKC symbol registry out of sync with Memgraph. | Maintain stable `qualified_name` keys. On renames, run SKC update to merge nodes. Use CGR’s incremental update mode (`--update-graph`). |
| *Performance Scalability*<br>(large monorepo) | Slow ingestion or query time. | Monitor DB usage. Possibly shard projects (project label) or increase resources. Use CGR’s selective parsing (ignore vendor dirs). |
| *Security Breach*<br>(Memgraph/Qdrant exposure) | Code leaking, data tampering. | Enforce the security checklist above: firewalls, auth, encryption. Perform penetration tests on endpoints. |
| *Inconsistent State*<br>(SKC vs CGR) | Query returns conflicting info. | Decouple: SKC remains source-of-truth; treat CGR as read-only projection. Do not write SKC-critical data into CGR. Refresh CGR graph from SKC events regularly. |
| *Complexity Overhead*<br>(maintenance) | High operational cost. | Containerize and automate as much as possible. Initially treat CGR as an optional module. Gradually refine. Document clearly. |
| *Agent Hallucinations*<br>(CGR AI component) | Incorrect code edits or suggestions. | Limit AI-driven changes to read/analysis only. Review before applying changes. Use CGR’s explicit AST diff generation to audit modifications. |
| *Version Compatibility*<br>(CGR updates) | Integration breaks on CGR release change. | Pin versions. Write adapter layer. Include CGR version in SKC. Maintain automated tests for new CGR releases. |
| *Data Privacy Regulation*<br>(if code contains PII) | Compliance violation. | Code structures likely not PII, but if so, ensure environment encrypts data and access is logged. Possibly exclude sensitive files from indexing. |

Each risk is prioritized (above by impact) and should be included in periodic reviews. The architecture allows isolating Code-Graph-RAG; if a risk becomes reality, the feature can be disabled without losing core StackMind functionality.

## References

- Code-Graph-RAG: architecture, features, and schema (Memgraph-based code knowledge graph).  
- Code-Graph-RAG Quick Start (Memgraph+Qdrant stack).  
- Code-Graph-RAG Python SDK (CypherGenerator, MemgraphIngestor, embed_code).  
- Memgraph Docker auth (USER/PASS environment).  
- Qdrant security (API keys, network binding).  
- Code-Graph-RAG Graph Schema (node/edge types).  

