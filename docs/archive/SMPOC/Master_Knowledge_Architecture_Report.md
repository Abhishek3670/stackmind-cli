# STACKMIND KNOWLEDGE COMPILER: ARCHITECTURE & EXECUTION REPORT

**Status:** Master Plan v1.0 (Synthesis of POC Artifacts)
**Directive:** Transition StackMind from a mere workflow orchestrator to an **inferentially-augmented, compiler-backed knowledge engine**. The source of truth remains the canonical Git/YAML state (`.sync/`), while derived intelligence is compiled and stored in `.sync/knowledge/`.

## 1. Core Thesis & Governance Model

**Problem:** Current agent sessions re-discover project structure (redundant parsing) which leads to high latency, resource drain, and unmanaged context fragmentation.
**Solution:** Implement a persistent, queryable **Knowledge Graph** (`Projs Knowledge`) populated by an incremental compiler layer (SKC). The Query API replaces file reads as the primary method of accessing structured system knowledge across agents.

### Core Principles (Invariant)
1.  **Canonical Source:** StackMind’s `.sync/` YAML is the Single Source of Truth (SOT). All generated data (`.sync/knowledge/`) must be *derived* from SOT and source code; they cannot overwrite authoritative layers.
2.  **Deterministic Core:** Code entities (Function, Class, Module) use AST-based parsing to generate IDs and relationships. These core reads must be byte-identical across identical rebuilds.
3.  **Stable Identity:** Symbols must possess a stable `NodeID` derived from a consistent hash of their fully qualified name + module path ($\text{HASH}_{\text{Symbol}}$). This ID **must never change** unless the symbol genuinely moves/renames.

## 2. Architecture Components

The system is separated into three logical, decoupled stacks:

### A. Source Layer (Deterministic Core)
*   **Process:** AST Parsing $\rightarrow$ Symbol Registry $\rightarrow$ Graph Builder
*   **Input:** Source Code Files (`src/*.py`), StackMind State (`.sync/`).
*   **Output:** Sharded JSON files in `.sync/knowledge/*`.
*   **Key Function (RFC-001):** The **Symbol Registry** maps `(ModulePath:QualifiedName)` $\rightarrow$ `NodeID`. This registry is the system's master reference.

### B. Compiler Layer (The Engine)
*   **Process:** Incremental Update Loop (`stackmind graph watch`).
*   **Functionality:** On file change detection, re-runs only affected parsing components ($\Delta\text{AST}$).
    1.  **Parsing:** Use $\text{LibCST}$ or $\text{AST}$ to extract Module/Class/Function definitions and compute **deterministic edges** (CALLS, IMPORTS).
    2.  **Graph Builder:** Writes structured nodes/edges to their respective sharded files (`nodes/`, `edges/`).
    3.  **Validation Gate:** Executes a new **Layer-5 Validator** on all knowledge JSON, ensuring schema conformity and preventing dangling edges/IDs.

### C. Inference Layer (Knowledge Augmentation)
*   **Process:** Background Enricher Queue $\rightarrow$ LLM Calls + Search API $\rightarrow$ Projection Storage.
*   **Functionality:** Adds non-deterministic intelligence to the graph structure without compromising determinism.
    1.  **Summarization/Embs:** Asynchronously processes new nodes (e.g., Function Body) via external APIs to generate `ai.summary` and compute embeddings. These are stored with a *low confidence* default.
    2.  **Harness Runtime:** This is the Web Search component. When an agent needs current facts (e.g., "Latest market trends"), it triggers a search provider, receives snippets, and adds them to the prompt context for the LLM.

## 3. Data Model & Provenance ($\text{.sync/knowledge/}$)

| Element | Definition | Location Example | Deterministic vs AI | Key Fields (Minimum) |
| :--- | :--- | :--- | :--- | :--- |
| **Node** | A single entity instance (Class, WorkOrder). | `nodes/Function/FUNC-ID.json` | Both (Code core; AI summary addon) | stable ID, name, path, determined fields $\text{AND}$ optional `ai.{summary, confidence}` block. |
| **Edge** | A directed relationship between two NodeIDs. | `edges/CALLS.json` | Deterministic only | SourceID, TargetID, Type (e.g., CALLS), Relation (calls). |
| **Graph Revision** | Full snapshot of the graph's state at a point in time. | `graph-revisions/REV-HASH.json` | Deterministic | Git SHA, Compiler Version, Node Count, Edge Count. *Mandatory for provenance.* |

## 4. Workflow Diagram & Execution Flow

The process is event-driven:

$\text{Code Change / State Update} \xrightarrow{\text{File Watcher / Event}} \text{AST/Parser} \xrightarrow{\text{Symbol Registry}} \text{Graph Builder Action}$
$$\downarrow$$
$$\mathbf{Write\ Lock\ Acquired \rightarrow Write\; Knowledge\; Files} \xrightarrow{\text{Schema/Protocol/K-Validator}} \text{.sync/knowledge/*}$$

**On Read (Query):** Agent calls $\texttt{stackmind graph query}$. This invokes the **Query API**, which builds a context from live JSON files, then optionally forwards this to the LLM for final answer generation.

## 5. Command Surface Adaptation

| Existing StackMind Cmd | New Status/Enhancement | Description |
| :--- | :--- | :--- |
| $\texttt{validate}$ | **New Layer 5 Integration** | Must validate all `.sync/knowledge/` files against graph schemas, checking IDs, edge existence, and structural integrity. Failure here blocks commits. |
| **(N/A)** | `graph build` | Builds the entire knowledge graph from scratch (initialization). |
| **(N/A)** | `graph watch` | Long-running daemon that monitors code sources for file changes ($\text{watchdog}$) and triggers incremental updates, acquiring/releasing the write lock per update. |
| **(N/A)** | `graph query <query>` | The primary API surface. Allows querying by criteria (e.g., "Find all WOs related to Module X") or free-text search over enriched knowledge. |

## 6. Conclusion & Required Action Items Checklist

The architecture is mature and feasible, relying on a powerful combination of version control primitives (diffs/SHA), structured JSON storage (sharding), and incremental computation (watches/watchdog).

### ⭐ ACTION PRIORITY (Immediate Next Steps)
1.  **Accept RFC-002 & RFC-003:** Define the full schema for Node/Edge structure, especially how $\text{CALLS}$ edges are represented in the JSON.
2.  **Implement $\texttt{graph build}$ Stub:** Create a working program that parses an empty sample project and successfully writes basic (empty) nodes and an initial `graph-revisions` marker file to `.sync/knowledge/`. This validates the ID formula, sharding, and write lock mechanism.

### 💡 Open Questions/Decisions (Requires User Input)
1.  **Preferred Node Storage Pattern:** Should we use a library like $\text{NetworkX}$ in memory during query time for complex traversal, or rely purely on structured file iteration over the JSON files?
2.  **Symbol ID Resolution Priority:** Which is most critical to stabilize first: `import` path tracking (imports) or functional call resolution (AST/CALLS)?