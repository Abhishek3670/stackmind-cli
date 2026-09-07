# Migration Guide

This guide covers upgrading stackmind runtimes between versions, handling breaking changes, and rollback procedures.

## Version Management

stackmind follows [Semantic Versioning 2.0.0](https://semver.org/):

```
MAJOR.MINOR.PATCH

MAJOR — Breaking changes to runtime contracts
MINOR — Backward-compatible feature additions
PATCH — Backward-compatible bug fixes
```

### Version File

Every runtime tracks its version in `.sync/RUNTIME_VERSION`:

```yaml
version: "1.0.0"
schema_versions:
  tree: 1
  boot: 1
  work_order: 1
  index: 1
min_cli_version: "1.0.0"
created_at: "2026-05-19T16:51:00+05:30"
upgraded_at: null
upgraded_from: null
migration_history: []
```

## Checking for Updates

### View Current Version

```bash
stackmind doctor ./my-project
```

Output includes:
```
Runtime Version Check
─────────────────────
Runtime version: 1.0.0
CLI version: 1.0.0
Compatibility: ✅ COMPATIBLE
```

### Check for Pending Migrations

```bash
stackmind migrate --check ./my-project
```

Output:
```
Pending migrations:
  1. v1.0.0 → v1.1.0 (reversible)
  2. v1.1.0 → v1.2.0 (reversible)

Run `stackmind migrate ./my-project` to apply.
```

## Upgrading

### Pre-Upgrade Checklist

1. **Commit all changes** — Ensure `.sync/` repo is clean
2. **Verify CLI version** — CLI must be >= runtime version
3. **Review changelog** — Check for breaking changes

```bash
# Check git status
cd ./my-project/.sync
git status

# Verify CLI
stackmind --version
```

### Run Migration

```bash
stackmind migrate ./my-project
```

The migration process:
1. Creates backup at `.sync/.backup/<timestamp>/`
2. Applies migrations sequentially
3. Updates `RUNTIME_VERSION`
4. Validates result

Output:
```
Backing up runtime to .sync/.backup/2026-05-20T10-00-00/
Applying migration v1.0.0 → v1.1.0... ✅
Applying migration v1.1.0 → v1.2.0... ✅
Updating RUNTIME_VERSION...
Migration complete. Runtime is now v1.2.0.
```

### Post-Upgrade Validation

```bash
stackmind validate ./my-project
```

## Breaking Changes

### Version Compatibility Matrix

| CLI Version | Runtime v1.0.x | Runtime v1.1.x | Runtime v2.0.x |
|-------------|----------------|----------------|----------------|
| CLI v1.0.x | ✅ Full | ⚠️ Partial | ❌ Incompatible |
| CLI v1.1.x | ✅ Full | ✅ Full | ❌ Incompatible |
| CLI v2.0.x | ⚠️ Read-only | ⚠️ Read-only | ✅ Full |

- **Full**: All operations supported
- **Partial**: CLI missing some new features
- **Read-only**: Can validate, cannot modify
- **Incompatible**: Upgrade required

### Breaking Change Policy

Before any v2.0.0 release:

1. Announce deprecations in v1.x release notes
2. Provide migration tooling
3. Support v1.x for minimum 6 months after v2.0.0
4. Document all breaking changes

## Rollback

### Automatic Rollback

If a migration fails, stackmind automatically rolls back:

```bash
stackmind migrate ./my-project
```

Output on failure:
```
Applying migration v1.2.0 → v2.0.0... ❌
Error: Schema validation failed
Rolling back to v1.2.0... ✅
Migration aborted. Runtime restored to v1.2.0.
```

### Manual Rollback

For reversible migrations:

```bash
stackmind migrate --rollback ./my-project
```

Output:
```
Rolling back v1.2.0 → v1.1.0... ✅
Runtime restored to v1.1.0.
```

### Restore from Backup

If rollback is unavailable:

```bash
# List backups
ls ./my-project/.sync/.backup/

# Restore from backup
rm -rf ./my-project/.sync/runtime
cp -r ./my-project/.sync/.backup/2026-05-20T10-00-00/runtime ./my-project/.sync/
```

## Migration Examples

### Example 1: Patch Upgrade (v1.0.0 → v1.0.1)

Patch upgrades are safe and require no intervention:

```bash
stackmind migrate ./my-project
```

Patch changes:
- Bug fixes in validation logic
- Documentation updates
- Performance improvements

### Example 2: Minor Upgrade (v1.0.0 → v1.1.0)

Minor upgrades add features without breaking existing schemas:

```bash
# Check what's new
stackmind migrate --check ./my-project

# Apply
stackmind migrate ./my-project
```

Minor changes might include:
- New optional fields in `TREE.yaml`
- New CLI commands
- New work order templates

### Example 3: Major Upgrade (v1.x → v2.0.0)

Major upgrades require careful planning:

1. **Read release notes** — Documented breaking changes
2. **Update CLI first** — `pip install stackmind>=2.0.0`
3. **Test on a copy** — Clone `.sync/` before migrating
4. **Run migration** — `stackmind migrate ./my-project`
5. **Validate thoroughly** — `stackmind validate ./my-project`

### Example 4: Manual Schema Migration

If auto-migration is unavailable:

```yaml
# Before (v1.0.0 TREE.yaml)
quality:
  backend_coverage: "80%"
  frontend_coverage: "80%"
  tests_passing: "all"

# After (v2.0.0 TREE.yaml) - field renamed
quality:
  coverage:
    backend: "80%"
    frontend: "80%"
  tests_passing: "all"
```

Manual steps:
1. Read `.sync/runtime/TREE.yaml`
2. Apply field transformations
3. Update `RUNTIME_VERSION`
4. Run `stackmind validate`

### Example 5: Normalizing Drifted Data (v1.1.0 → v1.2.0)

Long-running runtimes can accumulate field values that were never schema-valid
(e.g. agents writing free-form `phase_status` like
`RELEASED_DEPLOYED_HEALTHY (v1.0.5 ...)` instead of a lifecycle enum value).
The `1.1.0 → 1.2.0` migration repairs this automatically using the
`normalize_enum_field` action:

```yaml
up:
  - action: normalize_enum_field
    glob: "runtime/boot/*.yaml"        # apply to every boot snapshot
    field: phase_status
    allowed: [INIT, PLANNING, ACTIVE, COMPLETE, BLOCKED]
    mapping:
      RELEASED: COMPLETE               # substring → enum value
      DEPLOYED: COMPLETE
    fallback: COMPLETE                 # used when no mapping matches
    preserve_to: phase_status_detail   # original text kept here (lossless)
```

- Values already in `allowed` are left untouched.
- Non-conforming values are mapped to the enum and the **original string is
  preserved** in `preserve_to`, so no information is lost.
- The migration is reversible: its `down` uses `restore_field` to copy the
  preserved value back and drop the detail field.

```bash
stackmind migrate ./my-project        # 1.0.0 → 1.1.0 → 1.2.0
stackmind validate ./my-project       # now clean
```

## Compatibility Validation

### stackmind doctor Output

```
Runtime Version Check
─────────────────────
Runtime version: 1.1.0
CLI version: 1.0.0
Compatibility: ⚠️ PARTIAL

Warning: CLI version (1.0.0) is older than runtime (1.1.0)
Some features may not be available. Update CLI: pip install --upgrade stackmind

Schema Version Check
────────────────────
TREE.yaml schema: v1 ✅
Boot snapshots schema: v1 ✅
Work orders schema: v1 ✅
INDEX.yaml schema: v1 ✅

Migration Status
────────────────
Pending migrations: None
Runtime is up to date.
```

### Version Mismatch Handling

| Scenario | Action |
|----------|--------|
| CLI older than runtime (same major) | Upgrade CLI: `pip install --upgrade stackmind` |
| CLI newer than runtime (same major) | Run migration: `stackmind migrate ./my-project` |
| Different major versions | Follow major upgrade guide |

## Best Practices

### Before Upgrading

1. **Commit all agent work** — Ensure no uncommitted changes
2. **Update CLI first** — Always use latest CLI before migrating
3. **Read changelog** — Understand what changed
4. **Test on non-production** — Validate in a safe environment

### During Upgrade

1. **Don't interrupt** — Let migration complete fully
2. **Watch for errors** — Address any validation failures
3. **Verify backup** — Confirm `.backup/` exists before proceeding

### After Upgrading

1. **Validate** — Run `stackmind validate`
2. **Test boot** — Have agents boot and verify they resume correctly
3. **Commit** — Commit the upgraded runtime state

## Troubleshooting

### Migration Fails: Schema Validation

```
Error: TREE.yaml schema validation failed
  - tree_version: expected integer, got string
```

**Solution:** Fix schema manually or use `--fix`:
```bash
stackmind validate --fix ./my-project
stackmind migrate ./my-project
```

### Migration Fails: Incompatible CLI

```
Error: CLI version (1.0.0) incompatible with runtime (2.0.0)
Upgrade CLI: pip install stackmind>=2.0.0
```

**Solution:** Update CLI:
```bash
pip install --upgrade stackmind
```

### Rollback Unavailable

```
Error: Migration v1.0.0 → v2.0.0 is not reversible
```

**Solution:** Restore from backup:
```bash
ls ./my-project/.sync/.backup/
```

### Backup Missing

If backup is missing, recreate from git:
```bash
cd ./my-project/.sync
git log --oneline  # Find commit before migration
git checkout <commit-hash> -- .
```

## Migration Registry

Available migrations are tracked in the stackmind package:

```python
# stackmind/migrations/registry.py
MIGRATIONS = [
    Migration("1.0.0", "1.1.0", reversible=True),
    Migration("1.1.0", "1.2.0", reversible=True),
    Migration("1.2.0", "2.0.0", reversible=False),  # Major version
]
```

## Support

For migration issues:

1. Run `stackmind doctor ./my-project --verbose`
2. Check `.sync/.backup/` for restore points
3. Review migration logs in output
4. Escalate to Claude for complex issues
