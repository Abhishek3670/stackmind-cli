# Protocol Digest v1 (stackmind Runtime)

> **Hash-gated.** Agents compare `protocol_digest_hash` in their boot snapshot
> against `PROTOCOL_DIGEST.hash`. If match → skip this file entirely.
> Read only when the hash changes (protocol was updated).

---

## Boot Sequence (D023+)

```
0. READ  AGENTS.md (project root)              ← Universal rules (FIRST)
1. READ  runtime/boot/<self>.boot.yaml          ← Resume point (~2KB)
2. PEEK  runtime/TREE.yaml tree_version         ← if matches snapshot → skip
3. CHECK PROTOCOL_DIGEST.hash                   ← if matches snapshot → skip
4. CHECK unread_inbox_count                     ← if 0 → skip inbox
5. CHECK last_seen_decision vs latest_decision  ← if same → skip decisions
6. READ  assigned work orders from ACTIVE/      ← read-only for workers
7. RESUME from next_action in boot snapshot
```

Claude adds: step 6b (validate worker drafts), step 7 (INDEX.yaml + PLAN.md check).

## Shutdown Sequence (D023+)

> Universal shutdown rules are in AGENTS.md § Shutdown Rules.
> Agent-specific steps are in each agent's contract.

```
1. WRITE  draft snapshot → runtime/drafts/<self>.boot.draft.yaml
         (Workers NEVER touch runtime/boot/ — Claude publishes canonical)
2. WRITE  session report → outbox/<self>/<date>_session-report.md
3. UPDATE state/<self>.checkpoint.md
4. ARCHIVE read inbox → move to inbox/<self>/_read/
5. OUTPUT Handoff Report block (mandatory)
6. COMMIT .sync/ repo
```

Local LLM adds: step 7 (commit project repo — sole committer).
Claude adds: steps for TREE.yaml rebuild, INDEX.yaml sync, draft validation.

## Key Rules

### Commit Rules
- **Project repo**: Only Local LLM commits
- **Sync repo** (`.sync/`): All agents commit
- Handoff Report is mandatory at session end — no silent stops

### Snapshot Isolation
- Workers write drafts to `runtime/drafts/` — NEVER touch `runtime/boot/`
- Claude validates drafts and publishes canonical snapshots to `runtime/boot/`
- Workers propose. Claude commits.

### TREE.yaml
- Written ONLY by Claude (sole updater)
- Agents read it ONLY when `tree_version` in their snapshot doesn't match
- Contains: agent status, dependencies, unread counts, latest decisions, quality metrics

### Coordination
- Check dependencies before starting dependent groups
- Notify downstream agents when completing dependencies (write to their inbox)
- Don't mock dependencies; wait or work on unblocked tasks
- Respond to inbox messages before starting work

### Inbox
- Agents scan `inbox/<self>/` top-level only (NOT `_read/`)
- After processing a message, move it to `inbox/<self>/_read/`
- Never delete processed messages — `_read/` is the audit trail

### Escalation
- Ambiguous decisions → escalate to Top Manager with pros/cons
- Protocol violations → Claude flags and sends corrective message
- Workers write facts; Claude writes orchestration state

### AGENTS.md Supremacy
- AGENTS.md is the canonical source for universal runtime rules
- If agent contracts conflict with AGENTS.md, AGENTS.md wins
- Agent contracts contain ONLY role-specific instructions

## Destructive Operations Safeguard (D025)

**Before ANY irreversible command (git filter-repo, git reset --hard, rm -rf, force push, etc.):**

```
1. BACKUP  — cp -r .git .git-backup-<timestamp> (or archive targets)
2. VERIFY  — git status clean, record commit count, confirm scope
3. ESCALATE — CEO approval required (write to inbox/CEO/)
4. EXECUTE — single command only
5. VALIDATE — commit count matches, working tree intact
6. ROLLBACK — if validation fails, restore backup IMMEDIATELY
```

**Rules:**
- NEVER retry a failed destructive op — restore backup first
- NEVER chain multiple destructive commands without verification between each
- Unexpected output (e.g., "Parsed 2 commits" on 100+ commit repo) → STOP, restore
- Violation = CRITICAL breach → NON_COMPLIANT, no further work

## Runtime Version

Check `.sync/RUNTIME_VERSION` for version compatibility.
Use `stackmind doctor` to validate runtime health.
