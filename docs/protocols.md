# Protocols

stackmind enforces a set of protocols that govern agent behavior, runtime operations, and team coordination.

## Protocol Summary

| Protocol | Title | Purpose |
|----------|-------|---------|
| **D021** | Agent Boot/Resume Optimization | Snapshot-based resume system |
| **D022** | Work Orders Architecture | Persistent task management |
| **D023.x** | Protocol Enforcement Patches | Compliance, receipts, graph awareness |
| **D024** | Mandatory Review Handoff | Quality gate enforcement |
| **D025** | Destructive Operations Safeguard | Backup-verify-approve before irreversible ops |
| **D031** | Runtime Compatibility & Migration | Version management |

## Boot Sequence (D021+)

All agents must follow this boot sequence:

```
0. READ  AGENTS.md (project root)              ← Universal rules (FIRST)
1. READ  runtime/boot/<self>.boot.yaml          ← Resume point (~2KB)
2. PEEK  runtime/TREE.yaml tree_version (also referred to as TREE_SIG) ← Sequential integer; skip if matches snapshot
3. CHECK PROTOCOL_DIGEST.hash                   ← Skip if matches snapshot
4. CHECK graph_version (also referred to as GRAPH_SIG, D023.3)      ← SHA-256 of graphify-out/GRAPH_REPORT.md; skip if matches snapshot
   IF matches snapshot → skip graph
   IF mismatch → read relevant sections only
5. CHECK unread_inbox_count                     ← Skip if 0
6. CHECK last_seen_decision vs latest_decision  ← Skip if same
7. READ  assigned work orders from ACTIVE/      ← Read-only for workers
8. RESUME from next_action in boot snapshot
```

> **Note:** `tree_version` (TREE_SIG) and `graph_version` (GRAPH_SIG) are two
> distinct fields that coexist by design — not a naming inconsistency.
> - `tree_version` (TREE_SIG) is the sequential integer counter in `TREE.yaml` (present since v1.0.0) incremented only on structural/schema changes of the TREE.
> - `graph_version` (GRAPH_SIG) is the SHA-256 hash of `graphify-out/GRAPH_REPORT.md` (present since v1.1.0 per D023.3) tracking graph schema/state changes.
> The boot sequence checks both independently because they track different artifacts at different granularities.

### Boot Optimization Impact

| Metric | Before D021 | After D021 |
|--------|-------------|------------|
| Boot cost | ~500KB | ~2-8KB |
| Token cost | ~150K-180K | ~1K-3K tokens |
| Resume | Scanning | Explicit next_action |

## Shutdown Sequence (D023+)

Every session must end with this sequence:

```
1. WRITE  draft snapshot → runtime/drafts/<self>.boot.draft.yaml
           (Workers NEVER touch runtime/boot/)
2. WRITE  session report → outbox/<self>/<date>_session-report.md
3. WRITE  shutdown receipt → runtime/receipts/<self>.receipt.yaml
4. UPDATE state/<self>.checkpoint.md
5. ARCHIVE inbox → move to inbox/<self>/_read/
6. OUTPUT Handoff Report block (mandatory)
7. COMMIT .sync/ repo
```

### Handoff Report Format

Every session MUST end with this exact block:

```
═══════════════════════════════════════════════════════
📤 HANDOFF REPORT — <Agent Name>
═══════════════════════════════════════════════════════
│✅ COMPLETED THIS SESSION (session_completed: N):
│- [what you did; delegated actions must cite delegating_agent]
│
│📋 MY NEXT TASKS (when I resume):
│- WO-XXX (assigned by <Agent>, <source>) — [description]
│- Only currently assigned WOs or explicit inbox directives
│
│📨 MESSAGES WRITTEN THIS SESSION (PENDING READ BY RECIPIENT):
│- → [Agent]: [what you need / sending]
│  unread_inbox_count from TREE.yaml: [N]
│
│🚫 BLOCKERS:
│- [blocking issues, or "None"]
│
│⏳ WAITING ON:
│- [dependencies on others, or "Nothing"]
│
│💡 DECISION NEEDED (from Claude or Top Manager):
│- [decisions needed, or "None"]
│
│next_session_id: N+1
═══════════════════════════════════════════════════════
```

## Work Orders (D022)

### Structure

```
.sync/work-orders/
├── INDEX.yaml          ← Canonical registry (Claude-owned)
├── ACTIVE/             ← Active work order files
├── COMPLETED/          ← Archived completed work orders
├── BLOCKED/            ← Blocked work orders
└── TEMPLATES/          ← Schema templates
    ├── PHASE.yaml
    ├── FEATURE.yaml
    ├── BUGFIX.yaml
    ├── HOTFIX.yaml
    ├── AUDIT.yaml
    ├── REFACTOR.yaml
    └── RESEARCH.yaml
```

### Ownership Model

| Action | Owner |
|--------|-------|
| Create work orders | Claude |
| Update work orders | Claude |
| Assign agents | Claude |
| Mark complete | Claude |
| Read assigned work order | Any agent (read-only) |
| Edit work order files | NEVER (workers) |
| Self-assign WOs | NEVER (workers) — see GEMINI-02 in AGENTS.md |

### Work Order Completion Rules (D024)

When a worker finishes an assigned work order:

1. Write code + tests
2. Send review request to Gemma inbox:
   `.sync/inbox/gemma/<date>_<agent>_<wo-id>-review.md`
3. Send completion notice to Claude inbox:
   `.sync/inbox/claude/<date>_<agent>_<wo-id>-complete.md`
4. **Do NOT mark WO as complete** (Claude commits state changes)

**Skipping step 2 is a protocol violation.**

## Compliance & Enforcement (D023.2)

### Compliance Check

For each agent:
1. Check `receipts/<agent>.receipt.yaml` exists
2. Verify `report_written = true`
3. Check inbox for directives older than 1 session cycle
4. If any fail → `TREE.compliance[agent] = NON_COMPLIANT`
5. Do NOT assign new work to NON_COMPLIANT agents
6. Escalate to CEO

### Receipt Schema

```yaml
session_id: <number>
report_written: true|false
draft_written: true|false
handoffs_written: true|false
commit_written: true|false
timestamp: <ISO 8601>
```

### Non-Compliance Handling

- Missing receipt/report/ack → `NON_COMPLIANT`
- Non-compliant agents receive no new work
- CEO is notified automatically

### Environment Blocker Classification (GEMINI-01)

A broken local test environment MUST be classified as BLOCKED with a formal
BUGFIX work order. Continuing to ship implementation changes without local
verification is prohibited. CI-only verification (when local tests are
unavailable) requires an explicit Decision entry from the architect role
documenting the exception. "Doesn't block the build" is not sufficient.

## Inbox SLA Rules

- Directives must be acknowledged and started within the same session
- Processed messages are moved to `_read/` (never deleted)
- Agents scan only top-level inbox items (skip `_read/`)

## Runtime Integrity Enforcement

The CLI enforces a set of runtime-integrity rules that close the canonical-drift
gaps where an agent could pass its own boot checks while being silently wrong
about shared state. The single root principle: **every canonical write must be
anchored to an external source of truth and serialized.**

### Write Lock & Serialization (PLAT-03)

Canonical writes (`runtime/boot/`, `TREE.yaml`) are serialized by an advisory
write lock at `.sync/runtime/LOCK`:

```yaml
held_by: claude
session_id: 30
acquired_at: 2026-06-10T09:14:00+05:30
```

- `stackmind lock acquire <agent>` — refuses if another agent holds the lock.
- `stackmind lock release <agent>` / `stackmind shutdown` — clears the lock.
- `stackmind validate` flags a malformed lock (ERROR) or an unknown holder (WARN).
- **Lock Theft Alert**: A forced lock acquisition (using `--force` to steal a lock held by another agent) immediately writes a `LOCK_STOLEN` compliance event under `runtime/receipts/`. The validator scans for these events and flags them as warnings to notify operators of potential agent collisions or stuck processes.

### Fresh Snapshot Writes (CODEX-01)

The shutdown sequence re-reads `runtime/TREE.yaml` immediately before writing a
snapshot and syncs `tree_version`/`graph_version` to the current canonical
values — never a value cached earlier in the session. This prevents stale
version writes and keeps snapshots from silently lagging TREE (GEMMA-01).

### Inbox Drain & Deferral Gate (GEMMA-02)

`stackmind shutdown` refuses to close a session while the agent's inbox has
unprocessed items (top-level files not yet moved to `_read/`). This ensures
critical reviews (D024) or directives are not ignored.
- **Production Exemption (Deferral)**: If unprocessed items arrive late or cannot be addressed in the current session, the agent can use `stackmind shutdown --defer`. This moves the pending items to `_deferred/` and writes a signed receipt stub, allowing shutdown to complete safely without resorting to a brute-force bypass.

### CI/CD Pipeline Integration (Production Check)

In production repositories, `stackmind validate` should be integrated as a mandatory pre-commit hook or Pull Request check:
1. **Canonical Alignment**: Ensure `.sync-ref` matches the live `.sync` HEAD to detect uncommitted or out-of-order mutations before merging code.
2. **Structural Health**: Blocks PR merges if any schema violations, total mismatches between `TREE.yaml` and `INDEX.yaml`, or unreviewed code requests exist in the sync repository.

### Infinite Loop & Resource Safeguards (Ethical Containment)

To prevent resource exhaustion (e.g. infinite re-read loops or token-burning cycles due to version mismatch):
- Agents must monitor their re-read count. If an agent detects more than 3 consecutive boot re-reads or validation failures in a single session block, it must immediately abort its loop, write a blocker report to its outbox, and halt execution for human review.

### Promotion Gate (CLAUDE-01)

Promoting a worker draft to canonical follows
`validate draft → promote → validate canonical`. A draft that fails schema
validation is never promoted; a canonical that fails post-promotion validation
is rolled back. Failures write a blocker to Claude's inbox.

### Audit Trail (PLAT-04)

A successful promotion auto-generates a `NORMALIZATION` decision in
`decisions/` capturing the file changed and its from/to values, so canonical
mutations are always traceable:

```yaml
id: D-047
type: NORMALIZATION
authored_by: claude
session: 30
timestamp: 2026-06-10T10:00:00+05:30
changes:
  - file: runtime/boot/claude.boot.yaml
    session_count_from: 29
    session_count_to: 30
reason: Promoted claude draft snapshot to canonical boot file.
```

### Canonical Drift & Anchoring (PLAT-01, CODEX-03, PLAT-05)

- `TREE.yaml` work-order totals must match the `INDEX.yaml` ledger (the
  external ground truth); a mismatch is canonical drift (ERROR).
- A `.sync-ref` file tracked in the main repo records the last-known-good
  `.sync` commit SHA; `validate` compares it against the live `.sync` HEAD.

## Version Management (D031)
### Semantic Versioning

```
MAJOR.MINOR.PATCH

MAJOR — Breaking changes to runtime contracts
MINOR — Backward-compatible feature additions
PATCH — Backward-compatible bug fixes
```

### Compatibility Rules

| Change Type | Version Bump | Migration Required |
|-------------|--------------|-------------------|
| New optional field | MINOR | No |
| New required field | MAJOR | Yes |
| Field type change | MAJOR | Yes |
| Field removal | MAJOR | Yes |
| Protocol rule change | MAJOR | Yes |
| New CLI command | MINOR | No |
| CLI command removal | MAJOR | Yes |

### Version File

Location: `.sync/RUNTIME_VERSION`

```yaml
version: "1.0.0"
schema_versions:
  tree: 1
  boot: 1
  work_order: 1
  index: 1
min_cli_version: "1.0.0"
created_at: "2026-05-19T16:51:00+05:30"
```

## Forbidden Actions

Agents must NEVER:

- Use legacy boot
- Scan all checkpoints
- Scan all inboxes
- Scan all work orders
- Modify `TREE.yaml`
- Modify canonical snapshots (`runtime/boot/`)
- Modify another agent's files
- Change architecture without Claude approval
- Change product scope without CEO approval

## Authority Model

```
CEO:
- Product scope
- Priorities
- Releases

Claude:
- Architecture
- Planning
- Work orders
- Runtime normalization

Gemma:
- Quality gates
- Approvals
- Blocks

Workers:
- Implementation only
```

## Destructive Operations Safeguard (D025)

Added after the 2026-05-20 source code loss incident where `git filter-repo`
wiped all repository history and source files.

### Scope

Any command that rewrites git history, deletes files en masse, or cannot be
undone with a simple `git checkout` or `Ctrl+Z`.

### Mandatory Steps (ALL required, in order)

1. **BACKUP** — Copy `.git/` or archive target files
2. **VERIFY** — `git status` clean, record commit count, confirm targets
3. **ESCALATE** — CEO approval required (representing the **Human Operator / Custodian** oversight gate; or Claude with documented P0 architectural rationale if CEO is unavailable)
4. **EXECUTE** — Run the single command
5. **VALIDATE** — Commit count matches, working tree intact
6. **ROLLBACK** (if validation fails) — Restore from backup immediately

### Ethical Custody & Human Oversight

To protect the integrity of the codebase and prevent catastrophic source code loss, autonomous agents are prohibited from executing destructive commands unilaterally. The CEO escalation step acts as a mandatory human verification check, ensuring that no historical rewrite or bulk deletion is initiated without explicit developer consent and verification of backups.

### Covered Commands

- `git filter-repo`, `git filter-branch`
- `git reset --hard`, `git push --force`
- `git clean -fd`, `git checkout -- .` (on uncommitted work)
- `rm -rf` / `del /s` on source directories
- `docker system prune`, `docker rmi` (production images)
- Any `--force` flag on shared/remote state

### Rules

- NEVER retry a failed destructive command — restore from backup first
- NEVER run multiple destructive commands in sequence without verification between each
- If output shows unexpected counts (e.g., "Parsed 2 commits" on a 100+ commit repo), STOP and restore
- Backup is removed ONLY after final verification succeeds

### Violation

D025 non-compliance is a CRITICAL protocol breach. Agent is marked NON_COMPLIANT
and receives no further work until CEO review.

---

## Escalation Rules

**Escalate to Claude if:**
- Dependency blocked
- Ambiguity exists
- API contract changes
- Architecture mismatch

**Escalate to CEO if:**
- Scope changes
- Deadline changes
- Budget decisions

**Never guess. Escalate.**
