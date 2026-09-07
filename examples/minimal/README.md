# Minimal stackmind Example

This is a minimal example of a stackmind runtime instance.

## Structure

```
minimal/
├── AGENTS.md              # Authoritative agent rules
└── .sync/                 # Runtime instance
    ├── RUNTIME_VERSION    # Version tracking
    ├── runtime/
    │   ├── TREE.yaml      # Team state
    │   └── boot/          # Agent snapshots
    └── work-orders/
        └── INDEX.yaml     # Work order index
```

## Usage

This example shows the expected output of `stackmind init`.

To use this as a reference:

1. Copy this directory to start a new project
2. Update `AGENTS.md` with your project name
3. Begin agent sessions

## Notes

- All session counts are 0 (fresh runtime)
- No operational history
- Ready for first agent session
