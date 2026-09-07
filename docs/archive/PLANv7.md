# StackMind PLANv7: A TUI for Watching Agents, Not Just Browsing Graphs

**Version:** 7.0 Draft
**Status:** Strategic Roadmap
**Builds on:** PLANv6's `KnowledgeAPI.export_subgraph(...)`, the existing
harness (`validators/harness/runner.py`), the decisions log (`cli/decisions.py`),
and the single console-script entry point already defined in `pyproject.toml`
(`stackmind = "cli.main:cli"`).
**Depends on:** PLANv6 shipping `export_subgraph` first — this plan is a
second *client* of that API, not a third independent graph implementation.

---

## 0. The decision this plan encodes

**Don't build a TUI whose main feature is browsing the graph — that's the
least differentiated thing it could do, and `stackmind graph` plus PLANv6's
web UI already cover it.** Build a TUI whose main feature is a live,
terminal-native view of an agent actually running under contract — something
neither the CLI nor the web UI does well, and nothing in this space (CBM
included) has any equivalent of at all, because nothing else has a contract
layer to watch.

## 1. Naming and packaging

- Command: `stackmind tui` — a subcommand under the existing single entry
  point, not a second binary. `stackmind-cli` or any separate console-script
  name was considered and rejected: the existing tool already *is* the CLI,
  and a second binary with an unrelated name creates exactly the kind of
  "which one do I run" confusion worth avoiding from day one.
- Dependency: [Textual](https://github.com/Textualize/textual) — same
  maintainers as Rich, which `cli/graph.py` already imports
  (`rich.console.Console`), so this is additive, not a new dependency
  family. Pure Python, no native binary, no packaging story to solve (unlike
  CBM).
- Add a `tui` extra to `pyproject.toml`'s existing
  `[project.optional-dependencies]` block, following the same pattern as the
  existing `dev` extra:
  ```toml
  [project.optional-dependencies]
  tui = ["textual>=0.50"]
  ```
  `pip install stackmind[tui]`, not a base dependency — matching the lazy,
  opt-in discipline already established for CBM in PLANv5.

---

## Phase 7.1 — Live agent monitor (primary, the actual differentiator)

**Data source: tail what already exists, don't invent an event system.**
`AgentRunner.run_once()` already appends a line per run to the harness log
(`f"{now.isoformat()} harness {decision.status}: {decision.summary}"`), and
`cli/decisions.py`'s `decisions_dir(sync_path)` already holds one record per
decision. The live monitor is a `Textual` app that:
1. Tails the harness log file for new lines as they're appended (standard
   file-tail pattern — no new hooks needed in `runner.py`).
2. Cross-references each new line against the decisions directory for the
   full record (work order, contract, scope, blockers).
3. Renders three panels: **active work order + contract scope** (from the
   currently-active `.sync/contracts/*.yaml`), **live decision stream**
   (append-only, most recent at top, color-coded by
   `completed`/`blocked`/`denied`), and **the exact reason** for any
   `blocked` result — surfacing `result.reason` (the same string the harness
   contract tests assert against) directly in the terminal, live, as it
   happens.
4. A denied write (scope violation or D025 trigger) should visually stand
   out immediately — this is the "watch the fail-closed guarantee work in
   real time" view, which is the single most convincing demo this whole
   project has, and currently only exists as a pytest assertion nobody but
   me has actually watched happen.

This is genuinely novel: an `htop`-for-contract-enforcement view. Nothing
else in this space has a contract to watch in the first place.

---

## Phase 7.2 — Graph browse (secondary, intentionally not the headline feature)

A thin TUI client of PLANv6's `export_subgraph(...)` endpoint — same data,
same scoping strategy (impact-radius, contract-view, module-view), rendered
as a tree/list view instead of a rendered graph, since a terminal isn't well
suited to force-directed layouts. This exists mainly so someone on an SSH
session without a browser can still ask "what does this contract currently
allow" without needing PLANv6's web UI. Deliberately not the main pitch for
this plan — if this Phase ends up being the most-used part, that's a signal
the live monitor in 7.1 isn't landing, worth noticing rather than just
shipping past.

---

## Phase 7.3 — Promotion / decision review panel

A `lazygit`-style review screen: list pending promotions
(`cli/promote.py`'s domain), show what an agent actually changed before
promotion, approve/reject from the keyboard. This matches the actual persona
(someone overseeing agents, not browsing source) better than a generic file
diff viewer would. Lower priority than 7.1, roughly equal priority to 7.2 —
sequence based on which you'd actually reach for day to day.

---

## Sequencing

1. **PLANv6's `export_subgraph` ships first.** 7.2 depends on it directly;
   7.1 and 7.3 don't, but building any of this before PLANv6's API exists
   risks a TUI that reimplements graph traversal a third time instead of
   calling the shared endpoint once it's available.
2. **7.1 first within this plan** — it's the differentiated piece, it has
   no dependency on new API surface (the harness log and decisions dir
   already exist), and it's the best possible demo of the whole governance
   thesis this project has been built around.
3. **7.2 and 7.3** — either order, both secondary, both cheap once 7.1's
   Textual scaffolding exists.

**Exit gate:** running `stackmind tui` against a project with an active
contract, then triggering a real scope violation or D025 command through
`AgentRunner.run_once()` in another terminal, visibly shows the denial
appear live in the monitor within the same run — the harness contract tests
already prove this happens; this plan's job is to make it *watchable*.

---

## Appendix A — D025 hardening: blocklist → structural check

Carried over from the last review, not part of the TUI work itself but
worth recording formally now rather than leaving as an implicit gap.

**Current state (verified real and passing):**
`validators/harness/contract_gate.py` blocks destructive commands via a
fixed keyword list:
```python
destructive_keywords = ["rm ", "git reset", "git push", "git filter-repo",
                         "git filter-branch", "docker rm", "docker rmi", "del "]
```
This closed the original "D025 is prose-only" gap — it's real, code-enforced,
and the harness test (`test_harness_post_execution_gate_d025_violation`)
proves it blocks the exact case tested. But it's narrower than PLANv4's
original framing of this item (a `stackmind guard <command>` wrapper
requiring a backup + approval artifact before any destructive git operation
runs), and a fixed keyword list has a known, general weakness: it only
catches phrasings on the list. `git branch -D`, `git clean -fdx`, or a
destructive action taken through a non-shell code path (an agent calling a
file-delete tool directly rather than emitting a `commands` string) would
currently pass through untouched.

**Recommended upgrade path, not urgent, worth scheduling:**
1. Replace keyword matching with a structural check: classify a command by
   what it *does* (does it rewrite history, force-delete a ref, remove
   tracked files outside the working tree) rather than by matching literal
   substrings. Even a small allowlist-of-safe-git-verbs approach (deny by
   default for any `git` subcommand not explicitly known-safe, rather than
   allow by default and deny known-bad phrasings) inverts the failure mode
   from "misses new bad commands" to "occasionally blocks a safe command
   that needs allowlisting" — a much better direction to fail in for a
   safety mechanism named after a real prior incident.
2. Extend enforcement to cover destructive actions taken outside the
   `commands` string entirely — i.e., any file-deletion path a tool call can
   take, not just shell commands the agent reports.
3. Once either lands, add a harness test alongside the existing D025 test
   for at least one bypass case from the list above (`git branch -D` is the
   cheapest one to add first), so the next review has something concrete to
   verify rather than take on description.
