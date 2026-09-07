# Repository Layout & Current Capabilities  

StackMind’s codebase today is a **YAML-based runtime coordinator**. All state lives under `.sync/` in Git-tracked YAML files, and the CLI provides commands to manage that state. Key components are:  

- **CLI (`cli/`)**: One Click CLI group (`cli/main.py`) with 7 commands (`init`, `validate`, `doctor`, `migrate`, `shutdown`, `promote`, `lock`). Each command reads/writes the `.sync/` YAML tree.  
- **Schemas (`schemas/`)**: Seven JSON Schema files (`boot.schema.json`, `tree.schema.json`, `work-order.schema.json`, `index.schema.json`, `migration.schema.json`, `escalation.schema.json`, `runtime-version.schema.json`) define the YAML structure. These are enforced via JSON Schema (Draft7) in the validator.  
- **Validation (`cli/validate.py`)**: Implements a **4-layer validation pipeline** (Schema → Structure → Protocol → Boot). The code leverages JSON Schema for layer 1, structural checks for layer 2, protocol rules for layer 3, and final “boot sanity” checks for layer 4. (The `validators/` package exists but contains no logic yet.)  
- **Storage Model**: Everything under `.sync/` is Git-tracked YAML. A single write-lock (`cli/lock.py`) serializes writes to this tree, ensuring transactional updates.  

**Note:** *Currently there is **no graph or knowledge-graph code**. No `stackmind graph` command, no `.sync/knowledge/` directory, no AST or LLM components. All new “Knowledge Engine” functionality will be additive and layered on top of this existing infrastructure.*  

# Identity & Symbol Registry (RFC-001)  

**RFC-001** defines how symbols (functions, classes, modules, etc.) are identified in the graph. We use a **“birth-hash” symbol registry** to get the best of both worlds: *deterministic ID assignment* (for Git merges) and *rename stability* (for refactor resilience). The approach is:  

- **NodeID = Birth-Hash + Type Prefix:**  When a symbol is first seen (by its file path and fully-qualified name), we compute a SHA-256 hash of `"<relative_path>:<qualified_name>"`. Prefix it by the symbol type (e.g. `FUNC_` or `CLASS_`) to form the NodeID. For example:  
  ```
  # First time seeing function `foo` in `src/auth.py`
  input = "src/auth.py:AuthService.login"
  node_id = "FUNC_" + SHA256(input)[0:8]  # e.g. "FUNC_2a9f8b17"
  ```  
  This makes ID assignment **deterministic** across independent builds (Axis 1).  
- **Symbol Registry:** We store a canonical **symbol registry** (`.sync/knowledge/registry/symbols.json`) mapping each symbol’s current path/name to its NodeID, plus any historical aliases. This registry is **Git-tracked and write-locked** (not cache) because it’s canonical state. When files are renamed or moved, we detect the rename (by matching signature patterns or names) and update the registry so the NodeID **never changes** (Axis 2). For example:  
  - *Rename:* If `AuthService.login` is renamed to `AuthService.authenticate`, we leave NodeID `FUNC_2a9f8b17` intact and just record the new name in the registry.  
  - *Move:* If `src/auth.py` moves to `src/services/auth.py`, the same NodeID stays, but the registry notes the new path.  

This hybrid scheme means: a) **Independent builds** produce the same initial NodeIDs (important for Git merging); b) **Renames/moves** do not break identity (important for incremental updates and history). Unmatched symbols (new or deleted) simply add or remove registry entries. As a result, *every symbol has a stable, opaque ID from first sighting onward*.  

# Graph Storage & Sharding  

The **knowledge store** under `.sync/knowledge/` uses a **sharded JSON layout** to avoid giant monolithic files and enable fine-grained updates. Key points:  

- **Nodes by Type:** Each entity is stored in a separate file under `.sync/knowledge/nodes/`. For example, functions go in `nodes/Function/FUNC_XXXXX.json`, classes in `nodes/Class/`, etc. Each JSON file contains both **deterministic fields** (name, signature, location, imports/calls, etc.) and an `ai` block for LLM-generated summaries/notes (with confidence). Example structure:  
  ```jsonc
  // nodes/Function/FUNC_2a9f8b17.json
  {
    "id": "FUNC_2a9f8b17",
    "path": "src/auth.py",
    "qualname": "AuthService.login",
    "kind": "Function",
    "imports": ["AuthService.validate", "token_utils.encrypt"],
    "calls": ["UTILS_91b7c3d4", "FUNC_abc123ef"],
    // ... other static fields ...
    "ai": {
      "summary": "Handles user login by verifying credentials.",
      "confidence": 0.93
    }
  }
  ```
  Updating a function only rewrites its own file, so unrelated nodes aren’t affected.  

- **Outgoing Edges in Node Files:** Instead of a separate global CALLS.json, each node file lists its own outgoing edges (imports, calls, subclass links, etc.). This **eliminates large edge files** and avoids rewriting a whole-repo file on every change. For example, the above `calls` list shows edges from `AuthService.login` to other nodes. (Alternatively, one could store edges under `.sync/knowledge/edges/Type/NodeID.json`; either way, edges are sharded by source node.) This means **one-file edits** lead to **one-file updates**, aligning with an incremental model.  

- **Registry & Versions:** The symbol registry lives in `.sync/knowledge/registry/symbols.json` (Git-tracked, validated). We also keep a `graph-revisions.json` or similar to stamp each build with the Git SHA and schema version, for provenance.  

Overall, the storage is **schema-validated JSON** (draft-07) just like existing stackmind files. New schemas (`schemas/knowledge/node.schema.json`, `edges.schema.json`, `symbol.schema.json`, etc.) are added to define these formats. We extend the validator to a new “Layer 5” that checks graph-specific invariants (no duplicate IDs, all edge targets exist, etc.). This way the graph “can’t drift silently” – any violation fails `stackmind validate` just like the YAML state.  

# Incremental Build & Watch Daemon  

We treat the knowledge builder like an **incremental compiler**. Its workflow is:  

1. **Event-Driven Updates:** The builder watches for source or runtime changes (file edits, new commits, work-order updates, etc.). On each event, we recompute only the delta: parse changed files, update affected nodes/edges/registry entries. This follows the “only the delta runs” principle. For example, editing one function file will only re-parse that file and adjust the nodes it contains.  

2. **File-System Watch:** A background daemon (`stackmind graph watch`) uses `watchdog` or similar to monitor relevant directories (code, `.sync/`, tests, etc.). It batches rapid changes and triggers the graph update process. Because every write to `.sync/knowledge/` would retrigger the watch, the watch must **ignore its own output directories**. Typically we configure it to watch only source directories and `.sync/` (minus `.knowledge/`).  

3. **Write-Lock Interactions:** When updating the graph, the daemon acquires the same `.sync/` write-lock that CLI commands use. It holds the lock only while writing out the new/updated node and edge files (as well as the registry). Then it releases the lock before parsing new events. This ensures no two processes race on `.sync/`. Other CLI operations (e.g. updating work-orders) must either use the same lock or gracefully retry. In practice, the graph builder’s lock usage looks like:  

   - **Acquire lock** (via `stackmind lock`)  
   - *Compute updates:* parse ASTs, update symbol registry, generate new node JSONs or edge lists.  
   - *Write to `.sync/knowledge/`* (this is under the lock).  
   - **Release lock** (via exiting the lock context)  

   This pattern ensures **consistency**: during a build, agents can’t simultaneously write YAML that would conflict, and the graph files are atomically updated.  

4. **Command Support:** We expose CLI commands for the graph: `stackmind graph build` (one-shot build), `graph update` (rebuild changed parts), `graph stats` (report node counts), `graph versions` (list graph revisions), etc. These plug into `cli/main.py` under a new `@cli.group('graph')`. None of the existing commands are touched.  

This design means agents can query the graph (via `stackmind graph query ...`) at any time without triggering a rebuild. The data is always “fresh enough” because the watch daemon will update after source changes, enabling **sub-second or near-real-time indexing** in practice.  

# Write-Lock & Validation Layers  

We integrate the graph fully into StackMind’s validation and locking model:  

- **Write-Lock Usage:** The existing `stackmind lock` mechanism serializes all writes to `.sync/`. We treat `.sync/knowledge/` as part of that namespace. The graph builder **only holds the lock during writes**, as noted. Importantly, we do **not** hold the lock continuously; the watch daemon acquires/releases per update, so normal agent tasks (which don’t write to knowledge directly) aren’t blocked.  

- **Layer-5 Validation:** We add a new **fifth layer** in `cli/validate.py` to catch knowledge graph issues. After the existing Schema/Structure/Protocol/Boot checks, we validate the `.sync/knowledge/` files:  
  - **No duplicate IDs:** Every node ID is unique.  
  - **Edge consistency:** Every edge’s target ID must exist (or be marked unresolved if dynamic).  
  - **Registry integrity:** Symbols map to valid node types.  
  - **Revision stamps:** Graph revisions reference valid Git SHAs and builder versions.  

  Any violation is treated as a validation error (just like bad YAML). This ensures the graph “can’t drift silently” or accumulate orphaned data.  

- **Authoritative YAML:** We emphasize that `.sync/` YAML remains the **single source of truth** (Layer 0). The knowledge graph is a derived cache. If the graph were lost, it would simply rebuild from the latest YAML and source files. Thus the system never writes back into the authoritative layers from the graph.  

# Week 1 Implementation Plan  

With RFC-001 accepted, Week 1 focuses on bootstrapping the knowledge system’s foundation. Tasks include:  

1. **CLI & Schemas Setup**:  
   - Add `cli/graph.py` and register `@cli.group('graph')` with a placeholder `build` command.  
   - Create the `.sync/knowledge/` folder structure. Ensure `.sync/knowledge/registry/` and `.sync/knowledge/nodes/` exist.  
   - Add JSON Schema files under `schemas/knowledge/` for Node, Edge, Symbol, and GraphRevision. Integrate them into `validate.py`.  
   - Update `cli/validate.py` to include a fifth validation layer that loads `.sync/knowledge/` and applies the new schemas.  

2. **Symbol Registry (RFC-001)**:  
   - Implement the **symbol registry** loading. At graph build start, load `registry/symbols.json` (create if missing).  
   - On parsing each source file, compute `birth_key = f"{path}:{qualname}"`. Compute its hash prefix (as NodeID seed).  
   - If `birth_key` not in registry, assign a new NodeID (`TYPE_hash`) and add to registry. If it exists, use existing ID.  
   - Handle deletion: if a registry entry’s symbol no longer appears anywhere, mark it obsolete (or remove after review).  

3. **AST Parsing (Phase 1 Compiler)**:  
   - Use **LibCST** to parse Python source files and extract symbol definitions (functions, classes, modules).  
   - Build a **Symbol Table**: map each definition to its (path, qualname). For now, ignore complex dynamic cases (we’ll revisit later).  
   - Use **Jedi** or static analysis to resolve simple imports and calls within the same file (for future CALLS edges). Emit placeholder edges for unresolved calls.  

4. **Initial Node/Edge Writing**:  
   - For each symbol found, write a JSON file in `.sync/knowledge/nodes/Type/ID.json` with its static fields (name, signature, path, and any detected calls/imports). Include an empty `ai` section.  
   - Embed the outgoing edges in that file (as arrays of target IDs or placeholders). No separate edges files needed yet.  
   - Write/update the `registry/symbols.json` with any new symbols. Validate with the new schemas before committing.  

5. **Write-Lock & Git Integration**:  
   - Wrap all file writes in a `with stackmind.lock:` context (or equivalent) to ensure atomicity.  
   - After building, commit or push the new `.sync/knowledge/` files to Git (optional, depending on usage scenario).  
   - Implement a `stackmind graph build` command that invokes all above steps.  

6. **Testing & Validation**:  
   - Add unit tests or examples: e.g. build the graph on a small sample repo and verify that renaming a function in YAML or code doesn’t change its ID.  
   - Run `stackmind validate` including layer 5 to catch any schema errors.  

By the end of Week 1, we should have a **working skeleton**: a `stackmind graph build` that creates an empty (or simple) knowledge graph from the code, and all new files are schema-validated. This sets the stage for Weeks 2–4, where we’ll flesh out incremental updates, the watch daemon, LLM enrichment, embeddings, and querying. The critical underpinning is having the **correct identity model and basic storage** in place – once that’s solid, the rest of the build is straightforward.  

**Sources:** We follow modern “incremental indexing” practices (rebuilding only deltas) as in frameworks like CocoIndex, and leverage our existing StackMind YAML schema/validation infrastructure for correctness.