# CLI Reference

## Installation

```bash
pip install stackmind
```

## Commands Overview

| Command | Description |
|---------|-------------|
| `stackmind init` | Initialize a new runtime |
| `stackmind validate` | Validate runtime health |
| `stackmind doctor` | Check runtime status |
| `stackmind migrate` | Migrate runtime version |
| `stackmind shutdown` | Shutdown an agent session with handoff validation |
| `stackmind promote` | Promote a worker draft snapshot to canonical (gated) |
| `stackmind lock` | Manage the runtime write lock (`acquire`/`release`/`status`) |

---

## stackmind init

Initialize a new stackmind runtime at the specified path.

### Usage

```bash
stackmind init <project_path> [OPTIONS]
```

### Arguments

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `project_path` | PATH | Yes | Path where the runtime will be created |

### Options

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--name` | `-n` | Directory name | Project name |
| `--agents` | `-a` | All agents | Comma-separated agent list |
| `--no-git` | | False | Skip git initialization |

### Examples

```bash
# Basic initialization
stackmind init ./my-project

# With custom name
stackmind init ./my-project --name "My Project"

# Subset of agents
stackmind init ./my-project --agents claude,codex,gemini

# Without git initialization
stackmind init ./my-project --no-git
```

### Output

```
✅ stackmind runtime initialized at ./my-project

Created:
  - AGENTS.md
  - .sync/runtime/
  - .sync/work-orders/
  - .sync/inbox/
  - .sync/outbox/

Next steps:
  1. Review AGENTS.md for agent rules
  2. Run 'stackmind validate ./my-project' to verify
  3. Run 'stackmind doctor ./my-project' for status
```

### Errors

| Error | Cause | Solution |
|-------|-------|----------|
| `Runtime already exists` | `.sync/` directory present | Remove or use different path |
| `Permission denied` | Cannot write to path | Check permissions |
| `Invalid agent name` | Unknown agent specified | Use: claude, codex, gemini, gemma, local-llm |

---

## stackmind validate

Validate runtime health across all four layers.

### Usage

```bash
stackmind validate [project_path] [OPTIONS]
```

### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `project_path` | PATH | No | `.` | Path to runtime |

### Options

| Option | Description |
|--------|-------------|
| `--fix` | Auto-fix minor issues |

### Examples

```bash
# Validate current directory
stackmind validate

# Validate specific project
stackmind validate ./my-project

# Auto-fix minor issues
stackmind validate --fix
```

### Validation Layers

```
┌─────────────────────────────────────────┐
│  Layer 4: Boot Integrity                │
│  - Snapshot consistency                 │
│  - Version alignment                    │
│  - Canonical drift (TREE vs INDEX)      │
│  - Snapshot version lag detection       │
│  - .sync-ref anchoring                  │
│  - Hash verification                    │
├─────────────────────────────────────────┤
│  Layer 3: Protocol Compliance           │
│  - D021-D031 rule enforcement           │
│  - Authority model validation           │
│  - Write-lock (LOCK) integrity          │
│  - Untracked .sync path detection       │
│  - Review-file bundling detection       │
│  - Completion-notice release_target     │
│  - Forbidden action detection           │
├─────────────────────────────────────────┤
│  Layer 2: Structural Validation         │
│  - Directory structure                  │
│  - Required files exist                 │
│  - Git repos initialized                │
├─────────────────────────────────────────┤
│  Layer 1: Schema Validation             │
│  - YAML syntax                          │
│  - JSON Schema compliance               │
│  - Type checking                        │
└─────────────────────────────────────────┘
```

### Runtime Integrity Checks

In addition to the base layers, `validate` enforces the runtime-integrity
rules introduced to prevent silent canonical drift:

| Check | Severity | Description |
|-------|----------|-------------|
| Canonical drift | ERROR | `TREE.yaml` work-order totals must match `INDEX.yaml` (the work-order ledger is the external anchor) |
| Snapshot version lag | WARN | An agent boot snapshot lagging `TREE.yaml` by more than 3 versions signals broken session continuity |
| LOCK integrity | ERROR / WARN | A malformed `runtime/LOCK` is an error; a lock held by an unknown agent is a warning |
| LOCK stolen events | WARN | A forced lock acquisition (stealing the lock) logs a `LOCK_STOLEN` compliance event |
| Untracked `.sync` paths | WARN | Untracked files in the `.sync` git repo (e.g. orphaned review files) are flagged |
| Review-file bundling | ERROR | A review file referencing more than one work order violates D024 (one review per WO) |
| Completion `release_target` | ERROR | A work-order completion notice must declare a `release_target` |
| `.sync-ref` anchoring | WARN | The live `.sync` HEAD must match the `.sync-ref` SHA tracked by the main repo |

### Output (Success)

```
[PASS] Schema validation
[PASS] Structure validation
[PASS] Protocol compliance
[PASS] Boot integrity

Runtime is healthy.
```

### Output (With Issues)

```
[FAIL] Schema validation: TREE.yaml: tree_version must be integer
[WARN] Protocol compliance: codex inbox has 3 unread messages older than 24h

2 error(s), 1 warning(s)

Run `stackmind validate --fix` to auto-fix 1 issue(s).
```

### Auto-fixable Issues

| Issue | Auto-fixable |
|-------|--------------|
| Missing required file | No |
| Invalid YAML syntax | No |
| Schema violation | No |
| tree_version mismatch | Yes |
| Missing `.gitkeep` | Yes |
| Missing `_read/` directory | Yes |

---

## stackmind doctor

Check runtime status and compatibility.

### Usage

```bash
stackmind doctor [project_path]
```

### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `project_path` | PATH | No | `.` | Path to runtime |

### Examples

```bash
# Check current directory
stackmind doctor

# Check specific project
stackmind doctor ./my-project
```

### Output

```
╭─────────────────────────────────────────────────────╮
│ stackmind Runtime Health Report                     │
╰─────────────────────────────────────────────────────╯

Runtime Version Check
─────────────────────
Runtime version: 1.0.0
CLI version: 1.2.0
Compatibility: ✅ COMPATIBLE

Schema Version Check
────────────────────
TREE.yaml schema: v1 ✅
Boot snapshots schema: v1 ✅
Work orders schema: v1 ✅
INDEX.yaml schema: v1 ✅

Agent Status
────────────
Claude:    active  (session 61)
Codex:     idle    (session 67)
Gemini:    idle    (session 34)
Gemma:     idle    (session 47)
Local-LLM: active  (session 50)

Compliance Status
─────────────────
Codex:     COMPLIANT ✅
Gemini:    COMPLIANT ✅
Gemma:     COMPLIANT ✅
Local-LLM: COMPLIANT ✅

Migration Status
────────────────
Pending migrations: None
Runtime is up to date.

Validation Summary
──────────────────
[PASS] All validation checks passed
```

### Compatibility Matrix

| CLI Version | Runtime v1.0.x | Runtime v1.1.x | Runtime v2.0.x |
|-------------|----------------|----------------|----------------|
| CLI v1.0.x | ✅ Full | ⚠️ Partial | ❌ Incompatible |
| CLI v1.1.x | ✅ Full | ✅ Full | ❌ Incompatible |
| CLI v2.0.x | ⚠️ Read-only | ⚠️ Read-only | ✅ Full |

---

## stackmind migrate

Migrate runtime to a new version.

### Usage

```bash
stackmind migrate [project_path] [OPTIONS]
```

### Arguments

| Argument | Type | Required | Default | Description |
|----------|------|----------|---------|-------------|
| `project_path` | PATH | No | `.` | Path to runtime |

### Options

| Option | Description |
|--------|-------------|
| `--to VERSION` | Target version |
| `--check` | Check pending migrations only |
| `--rollback` | Rollback last migration |

### Examples

```bash
# Check pending migrations
stackmind migrate --check

# Migrate to latest
stackmind migrate

# Migrate to specific version
stackmind migrate --to 1.1.0

# Rollback last migration
stackmind migrate --rollback
```

### Output (Check)

```
Pending migrations:
  1. v1.0.0 → v1.1.0 (reversible)
  2. v1.1.0 → v1.2.0 (reversible)

Run `stackmind migrate` to apply.
```

### Output (Migrate)

```
Backing up runtime to .sync/.backup/2026-05-19T16-51-00/
Applying migration v1.0.0 → v1.1.0... ✅
Applying migration v1.1.0 → v1.2.0... ✅
Updating RUNTIME_VERSION...
Migration complete. Runtime is now v1.2.0.
```

---

## stackmind shutdown

Shutdown an agent session with handoff validation. Updates `TREE.yaml`,
persists the agent's boot snapshot, archives the handoff report, and releases
the write lock.

### Usage

```bash
stackmind shutdown <agent> [OPTIONS]
```

### Arguments

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `agent` | TEXT | Yes | Name of the agent to shut down |

### Options

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--project` | `-p` | `.` | Project path |
| `--force` | | False | Skip handoff/inbox gates (not recommended) |
| `--defer` | | False | Defer unprocessed inbox items instead of blocking shutdown |

### Gates enforced

- **Handoff report** must exist in the agent's outbox (unless `--force`).
- **Inbox drain (GEMMA-02)**: the agent's inbox must contain zero unprocessed
  items (top-level files not yet moved to `_read/`) before the session can
  close. If unprocessed items are present, shutdown is blocked unless:
  - `--force` is used (bypasses the gate entirely, not recommended)
  - `--defer` is used (safely moves items to `_deferred/` and writes receipt stubs)

### Snapshot freshness (CODEX-01)

When persisting the boot snapshot, `shutdown` re-reads `TREE.yaml` at write
time and syncs the snapshot's `tree_version`/`graph_version` to the current
canonical values — never a value cached earlier in the session. This prevents
both stale-version writes and silent snapshot version lag.

### Examples

```bash
stackmind shutdown claude
stackmind shutdown codex --project ./my-project
stackmind shutdown gemini --force
```

---

## stackmind promote

Promote a worker's draft snapshot to the canonical boot file, gated by
validation on both sides (CLAUDE-01).

### Usage

```bash
stackmind promote <agent> [OPTIONS]
```

### Arguments

| Argument | Type | Required | Description |
|----------|------|----------|-------------|
| `agent` | TEXT | Yes | Agent whose draft to promote |

### Options

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--project` | `-p` | `.` | Project path |

### Promotion gate

```
validate draft  →  promote  →  validate canonical
```

- The draft at `runtime/drafts/<agent>.boot.draft.yaml` is validated against
  the boot schema **before** the canonical file is touched.
- On success the draft is written to `runtime/boot/<agent>.boot.yaml` and a
  `NORMALIZATION` decision entry is recorded in `decisions/` (PLAT-04).
- If validation fails on either side, the promotion is aborted (or rolled
  back) and a blocker is written to `inbox/claude/`.

### Examples

```bash
stackmind promote codex
stackmind promote gemma --project ./my-project
```

---

## stackmind lock

Manage the runtime write lock (PLAT-03). The lock serializes canonical writes
(`runtime/boot/`, `TREE.yaml`) across agent sessions. It is an advisory lock at
the tooling layer: it lets agents and CI detect contention and refuse to
proceed.

### Subcommands

```bash
stackmind lock acquire <agent> [--session-id ID] [--force] [-p PATH]
stackmind lock release <agent> [--force] [-p PATH]
stackmind lock status [-p PATH]
```

| Subcommand | Description |
|------------|-------------|
| `acquire` | Acquire the lock for an agent; refuses if another agent holds it (using `--force` steals the lock and logs a `LOCK_STOLEN` compliance event) |
| `release` | Release the lock; refuses to release another agent's lock (unless `--force`) |
| `status` | Show the current lock holder, session, and acquisition time |

The lock file lives at `.sync/runtime/LOCK`:

```yaml
held_by: claude
session_id: 30
acquired_at: 2026-06-10T09:14:00+05:30
```

`stackmind shutdown` is the canonical mechanism that clears an agent's lock.

### Examples

```bash
stackmind lock acquire claude --session-id 31
stackmind lock status
stackmind lock release claude
stackmind lock acquire codex --force   # steal a stale lock
```

---

## Version

```bash
stackmind --version
```
Output:
```
stackmind, version 1.2.0
```

## Help

```bash
stackmind --help
stackmind init --help
stackmind validate --help
stackmind doctor --help
stackmind migrate --help
stackmind shutdown --help
stackmind promote --help
stackmind lock --help
```

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `stackmind_DEBUG` | Enable debug output |
| `stackmind_QUIET` | Suppress non-error output |
| `stackmind_NO_COLOR` | Disable colored output |
