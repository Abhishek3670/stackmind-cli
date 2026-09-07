# Getting Started

## Installation

```bash
pip install stackmind
```

## Quick Start

### 1. Initialize a New Project

```bash
stackmind init ./my-project --name "My Project"
```

This creates a complete multi-agent runtime with:
- Agent coordination infrastructure (`.sync/`)
- Work order management system
- Boot snapshot system for session continuity
- Protocol enforcement rules

### 2. Validate Runtime Health

```bash
stackmind validate ./my-project
```

### 3. Check Runtime Status

```bash
stackmind doctor ./my-project
```

## Your First Project

After running `stackmind init`, your project will have this structure:

```
my-project/
├── AGENTS.md              # Authoritative agent rules
└── .sync/                 # Runtime instance
    ├── RUNTIME_VERSION    # Version tracking
    ├── runtime/
    │   ├── TREE.yaml      # Team state index
    │   └── boot/          # Agent boot snapshots
    ├── work-orders/       # Task management
    ├── inbox/             # Agent messages
    ├── outbox/            # Session reports
    └── decisions/         # Decision log
```

### What Happens Next

1. **Agents boot from snapshots** — Each agent reads their `boot.yaml` to resume work
2. **Work flows through work orders** — Tasks are tracked in `.sync/work-orders/`
3. **Communication via inbox/outbox** — Agents send messages and reports
4. **Decisions are logged** — All architectural decisions tracked in `.sync/decisions/`

## CLI Commands

| Command | Description |
|---------|-------------|
| `stackmind init <path>` | Initialize new runtime |
| `stackmind validate <path>` | Check runtime health |
| `stackmind doctor <path>` | Detailed status report |
| `stackmind migrate <path>` | Upgrade runtime version |
| `stackmind shutdown <agent>` | End a session with handoff + inbox-drain validation |
| `stackmind promote <agent>` | Promote a worker draft snapshot to canonical (gated) |
| `stackmind lock <acquire\|release\|status>` | Manage the runtime write lock |

## Next Steps

- [Architecture](architecture.md) — Understand runtime engine vs instance
- [Protocols](protocols.md) — Learn the boot/shutdown sequences
- [CLI Reference](cli-reference.md) — Full command documentation
