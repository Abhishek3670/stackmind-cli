# stackmind-cli

A compiler-backed multi-agent engineering runtime — Click CLI, local JSON-RPC daemon, and governance kernel. See [STACKMIND-CLI.md](STACKMIND-CLI.md) and [docs/](docs/).

## Getting Started

1. Install in a virtual environment: `pip install -e .` (Python 3.10+)
2. Run `stackmind --help` — commands: `init`, `validate`, `doctor`, `migrate`, `graph`, `harness`, `analyze`, `experience`, `skill`, `learn`, `daemon`, `tui`, `shutdown`, `promote`, `lock`
3. Scaffold a managed project with `stackmind init` (creates `AGENTS.md`, the `.sync/` runtime layout, work orders, and contracts in the target project)

## Runtime

- **Version:** v3.7.0 (see `VERSION` / `CHANGELOG.md`)
