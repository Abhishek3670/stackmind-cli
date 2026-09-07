# StackMind PLANv6: A Graph UI That Shows the Governance Layer, Not Just Structure

**Version:** 6.0 Draft
**Status:** Strategic Roadmap
**Builds on:** existing `KnowledgeAPI` (`validators/knowledge/api.py`), existing
`stackmind graph` CLI surface (`cli/graph.py`), the contract engine
(`validators/knowledge/contract.py`), and the CBM adapter from PLANv5.
**Prompted by:** `codebase-memory-mcp`'s optional `--ui` binary variant — a
localhost-only 3D graph browser at `localhost:9749`. Worth having an
equivalent, but not worth copying: theirs shows structure, which is table
stakes. StackMind's should show contract scope, which nothing else in this
space has.

---

## 0. The decision this plan encodes

**Don't build a generic graph browser. Build a scoped, contract-aware viewer
where the differentiating information — allowed, denied, and unresolved
blind-spot regions of the graph, in the context of a specific agent contract
— is the default view, not an add-on toggle.**

A plain structure browser competing with CBM's UI on visual polish is a fight
you don't need to have. Nobody else can show what a specific agent contract
currently permits, overlaid on the graph, in real time. That's the whole
point of this UI.

---

## 1. Why not reuse CBM's UI binary directly

CBM's `--ui` variant reads its own SQLite schema (`nodes`, `edges`,
`projects`) and has no concept of StackMind's birth-hash NodeIDs, contract
scopes, or `DenyPlaceholder` nodes. Pointing it at StackMind's data would
require translating your IR back into CBM's schema — a pointless round trip
that would also produce a UI blind to the one thing worth seeing. Their
engineering effort (a from-scratch, in-house, localhost-only HTTP transport,
and a binary-search layout algorithm to handle scale) is worth learning from
directly, though — see Phase 6.4.

---

## 2. Backend — new endpoints on the existing Knowledge API

No new server framework, no new storage. This is additive to
`validators/knowledge/api.py`'s `KnowledgeAPI` class and exposed through a new
`stackmind graph ui` CLI command (`cli/graph.py`), following the exact pattern
`impact_command` and `callers_command` already use — `KnowledgeAPI(Path(project_path).resolve())`,
then a method call, then serialize.

### `KnowledgeAPI.export_subgraph(...)`
The core new method. Never returns the full graph — see Scoping Strategy
below for why. Signature:
```python
def export_subgraph(
    self,
    *,
    center: str | None = None,      # a symbol/node_id to radiate from
    depth: int = 2,                 # BFS radius from center, reuses the same
                                     # bounded-BFS machinery impact_command
                                     # already calls through KnowledgeAPI.impact()
    module: str | None = None,      # alternative: scope to one module/path prefix
    contract_path: str | None = None,  # if given, annotate every node/edge
                                        # with in-scope / denied / unresolved,
                                        # using Contract.is_node_in_scope(...)
                                        # from validators/knowledge/contract.py
    limit: int = 500,               # hard cap on nodes returned, enforced
                                     # server-side, not just client-side
) -> SubgraphEnvelope: ...
```

### `SubgraphEnvelope` (new small dataclass, same file)
```python
{
  "nodes": [
    {
      "node_id": "...", "kind": "...", "path": "...", "qualified_name": "...",
      "frontend": "python" | "cbm",         # provenance, from IR source
      "scope_state": "allowed" | "denied" | "unresolved" | null  # null if no
                                                                  # contract given
    }
  ],
  "edges": [...],
  "truncated": bool,   # true if `limit` was hit — UI must show this, not hide it
  "revision": int
}
```

### New CLI command: `stackmind graph ui`
```
stackmind graph ui --project . [--center <symbol>] [--contract <path>] [--port 9750] [--depth 2]
```
Starts a local server (see Phase 6.2), binds `127.0.0.1` only by default —
matching CBM's own security posture exactly, not inventing a weaker one —
and opens the default scoped view (see below) in the browser.

---

## Phase 6.1 — Scoping strategy (the part that actually matters)

The self-hosting test already produced 3,320 nodes / 36,312 edges from
StackMind's own codebase alone. CBM's own team needed real engineering (a
binary-search layout algorithm, replacing an `O(n·e)` approach) to make an
unscoped graph browsable at that scale in C. A naive "send everything to the
browser" approach in a Python-served UI would either choke or render an
unreadable hairball — and would be the wrong default even if it didn't.

**Default view is never the whole graph.** Three scoped entry points, in
order of expected use:
1. **Impact-radius view** (`--center <symbol> --depth N`) — reuses the exact
   BFS the CLI's `impact` command already performs. This is almost certainly
   the most common real use: "what does changing this touch."
2. **Contract view** (`--contract <path>`) — the differentiating one. Shows
   only the nodes an active contract's scope rules resolve against (its
   allow/deny tree plus immediate boundary), colored by `scope_state`. This
   is where `DenyPlaceholder` nodes and CBM-adapter blind spots (from PLANv5)
   become visible as a shape, not a log line.
3. **Module view** (`--module <path prefix>`) — scoped to one subtree,
   for orientation before drilling into 1 or 2.

A global unscoped view is deliberately not a first-class feature. If someone
really wants it, `--depth` set very high plus `--limit` still caps it, and
`truncated: true` is a required, visible UI state — not a silent cutoff.

---

## Phase 6.2 — Local server

- Reuse Python's standard library (`http.server`) or a minimal existing
  dependency already in the project (check `pyproject.toml` before adding
  anything new — this should not be the excuse to add a web framework
  dependency to a tool that currently has none for its core path).
- Bind `127.0.0.1` explicitly, no `0.0.0.0` fallback, no config flag that
  weakens this by default — CBM's own team treats this as a hard security
  property ("Localizing-only by construction... platform-correct socket
  options on every OS"), and StackMind's contract data flowing through this
  endpoint is more sensitive than CBM's plain structure data, not less.
- Serve exactly two things: the `SubgraphEnvelope` JSON from
  `export_subgraph`, and one static frontend bundle (see 6.3). No other
  routes, no write endpoints — this is a read-only viewer, matching the
  Knowledge API's own read-only posture (RFC-004).

---

## Phase 6.3 — Frontend

- A static HTML/JS bundle, not a build pipeline — this should be
  `pip install`-able with zero new native dependencies, consistent with
  PLANv5's packaging discipline for the CBM adapter.
- Graph rendering: Cytoscape.js or d3-force, not a 3D engine. 3D is the
  right call for CBM's "browse everything" use case; StackMind's default
  views are all scoped and small (a `--depth 2` impact radius from one
  symbol is rarely more than a few hundred nodes), so a 2D layout with clear
  edge-type styling and contract-state coloring reads better than a 3D scene
  would for this specific use case.
- Visual encoding, directly reflecting `SubgraphEnvelope`:
  - Node fill: `scope_state` (allowed / denied / unresolved / no-contract-given)
  - Node border or icon: `frontend` provenance (python vs cbm), so polyglot
    coverage gaps are visible without a separate report
  - `DenyPlaceholder` nodes: a distinct shape, not just a color, so they
    can't be missed by someone colorblind to the deny-red

---

## Phase 6.4 — What's actually worth learning from CBM's UI, concretely

Not the 3D visuals — the transport hardening. Before shipping even a
localhost-only server, mirror their documented discipline: strict request
size caps, no keep-alive state machine complexity, one request per
connection. Their `httpd.c` rewrite notes are a good checklist to hold this
against even though the implementation language differs entirely.

---

## Sequencing — where this fits relative to open CBM hardening work

**This plan should follow, not precede, the CBM adapter fixes already in
flight.** Concretely, in order:

1. **PLANv5 Bug #1 and #2 fixes** (db-lookup, determinism) — already
   confirmed fixed and verified by direct execution.
2. **The remaining small gap** — `FileNotFoundError`/`OSError` not caught
   alongside `CalledProcessError` in the CBM adapter's failure handling —
   should close before this plan starts, for one specific reason: Phase 6.1's
   contract view is the whole point of this UI, and it will render
   `DenyPlaceholder` nodes from CBM failures front and center. If the
   fail-closed path itself has a hole, this UI will faithfully and visibly
   render *evidence of that hole* to whoever looks at it — better to close
   the gap first than to ship a UI whose first real use surfaces an old,
   already-known bug as if it were a new one.
3. **PLANv4's still-open items** (scope-violation end-to-end harness test,
   D025 code enforcement) — not a hard blocker for this plan technically,
   since the UI is read-only and doesn't touch the harness. But it's the
   same underlying data (`is_node_in_scope`) this UI's contract view depends
   on, so proving that path end-to-end first means the UI is visualizing a
   verified guarantee, not an assumed one.
4. **PLANv6** (this plan) — once 1-3 above are settled, build in the order:
   `export_subgraph` + impact-radius view first (lowest risk, reuses existing
   BFS) → contract view (the actual differentiator) → module view (cheapest,
   any time).

**Exit gate:** `stackmind graph ui --contract <path>` run against a repo with
at least one CBM-sourced language and at least one intentionally-denied
module, correctly renders that module in deny-red and any known blind spot
as a distinct `DenyPlaceholder` shape — proving the UI surfaces the
governance layer accurately, which was the entire reason to build this
instead of adopting CBM's.
