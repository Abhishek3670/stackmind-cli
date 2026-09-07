"""stackmind promote — Promote a worker draft snapshot to canonical (CLAUDE-01).

Workers write draft snapshots to ``runtime/drafts/<agent>.boot.draft.yaml`` at
shutdown; only the architect promotes a draft to the canonical
``runtime/boot/<agent>.boot.yaml``. CLAUDE-01 requires this promotion to be
gated by validation on both sides:

    validate draft  →  promote  →  validate canonical

If either validation fails the promotion is aborted (and any partial write is
rolled back) and a blocker is written to Claude's inbox. It must not be
possible to promote a draft that fails schema validation, because that would
silently corrupt the canonical boot file for the whole team.
"""

from datetime import datetime, timezone
from pathlib import Path

import yaml
from jsonschema import Draft7Validator
from rich.console import Console

from .validate import _load_schema
from .decisions import build_canonical_change, write_normalization_decision

console = Console()


def draft_path(sync_path: Path, agent: str) -> Path:
    return sync_path / "runtime" / "drafts" / f"{agent}.boot.draft.yaml"


def canonical_path(sync_path: Path, agent: str) -> Path:
    return sync_path / "runtime" / "boot" / f"{agent}.boot.yaml"


def validate_boot_text(text: str) -> list[str]:
    """Validate raw boot-snapshot text against the boot schema.

    Returns a list of human-readable error messages (empty if valid).
    """
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        return [f"Invalid YAML: {e}"]

    if not isinstance(data, dict):
        return [f"Expected YAML mapping, got {type(data).__name__}"]

    schema = _load_schema("boot.schema.json")
    if schema is None:
        return []

    errors: list[str] = []
    for error in Draft7Validator(schema).iter_errors(data):
        json_path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        errors.append(f"{json_path} — {error.message}")
    return errors


def write_promotion_blocker(sync_path: Path, agent: str, reason: str, errors: list[str]) -> Path:
    """Write a blocker notice to Claude's inbox describing a failed promotion."""
    inbox = sync_path / "inbox" / "claude"
    inbox.mkdir(parents=True, exist_ok=True)

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    dest = inbox / f"{ts}_promote-blocked_{agent}.md"

    lines = [
        f"# Promotion blocked: {agent}",
        "",
        f"Reason: {reason}",
        "",
        f"Draft: runtime/drafts/{agent}.boot.draft.yaml",
        f"Canonical target: runtime/boot/{agent}.boot.yaml",
        "",
        "## Validation errors",
        "",
    ]
    if errors:
        lines.extend(f"- {e}" for e in errors)
    else:
        lines.append("- (none recorded)")
    lines.append("")
    lines.append("The canonical boot snapshot was NOT modified.")
    dest.write_text("\n".join(lines), encoding="utf-8")
    return dest


def promote_draft(sync_path: Path, agent: str, authored_by: str = "claude") -> tuple[bool, str]:
    """Promote ``agent``'s draft snapshot to canonical, gated by validation.

    Returns (success, message). On any failure the canonical file is left
    unchanged (or restored) and a blocker is written to Claude's inbox. On
    success a NORMALIZATION decision entry is recorded in ``decisions/``
    (PLAT-04) so the canonical mutation is traceable in the audit trail.
    """
    src = draft_path(sync_path, agent)
    if not src.exists():
        return False, f"No draft found at runtime/drafts/{agent}.boot.draft.yaml"

    draft_text = src.read_text(encoding="utf-8")

    # 1. Validate the draft BEFORE touching the canonical file.
    draft_errors = validate_boot_text(draft_text)
    if draft_errors:
        blocker = write_promotion_blocker(
            sync_path, agent, "Draft failed schema validation", draft_errors
        )
        return (
            False,
            f"Draft validation failed ({len(draft_errors)} error(s)); "
            f"promotion aborted. Blocker: {blocker.relative_to(sync_path).as_posix()}",
        )

    # 2. Promote, preserving the prior canonical content for rollback.
    dest = canonical_path(sync_path, agent)
    previous = dest.read_text(encoding="utf-8") if dest.exists() else None
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(draft_text, encoding="utf-8")

    # 3. Validate the canonical file AFTER promotion; roll back on failure.
    canonical_errors = validate_boot_text(dest.read_text(encoding="utf-8"))
    if canonical_errors:
        if previous is not None:
            dest.write_text(previous, encoding="utf-8")
        else:
            dest.unlink()
        blocker = write_promotion_blocker(
            sync_path, agent, "Canonical failed post-promotion validation", canonical_errors
        )
        return (
            False,
            f"Canonical validation failed after promotion; rolled back. "
            f"Blocker: {blocker.relative_to(sync_path).as_posix()}",
        )

    # 4. PLAT-04: record the canonical mutation in the audit trail.
    prev_data = yaml.safe_load(previous) if previous else None
    new_data = yaml.safe_load(draft_text)
    change = build_canonical_change(
        f"runtime/boot/{agent}.boot.yaml",
        prev_data if isinstance(prev_data, dict) else None,
        new_data if isinstance(new_data, dict) else None,
    )
    session = new_data.get("session_count") if isinstance(new_data, dict) else None
    write_normalization_decision(
        sync_path,
        authored_by=authored_by,
        changes=[change],
        reason=f"Promoted {agent} draft snapshot to canonical boot file.",
        session=session,
    )

    return True, f"Promoted draft to runtime/boot/{agent}.boot.yaml"


def promote(project_path: Path, agent: str) -> bool:
    """CLI entry point for promoting a draft snapshot to canonical."""
    sync_path = project_path / ".sync"
    if not sync_path.exists():
        console.print("[bold red][x] No .sync/ directory found[/bold red]")
        return False

    ok, message = promote_draft(sync_path, agent)
    if ok:
        console.print(f"[green][+] {message}[/green]")
    else:
        console.print(f"[bold red][x] {message}[/bold red]")
    return ok
