"""stackmind validate — Validate runtime health.

Implements four validation layers per D030 §7:
- Layer 1: Schema Validation (YAML syntax, JSON Schema compliance)
- Layer 2: Structural Validation (directory structure, required files, git repos)
- Layer 3: Protocol Compliance (authority model, forbidden action detection)
- Layer 4: Boot Integrity (snapshot consistency, version alignment, hash verification)
"""

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import yaml
from jsonschema import Draft7Validator, ValidationError
from rich.console import Console

console = Console()


class Severity(Enum):
    ERROR = "ERROR"
    WARN = "WARN"


@dataclass
class Issue:
    layer: str
    severity: Severity
    message: str
    path: str = ""
    auto_fixable: bool = False


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == Severity.WARN]

    @property
    def passed(self) -> bool:
        return len(self.errors) == 0


def get_schemas_dir() -> Path:
    return Path(__file__).parent.parent / "schemas"


def _load_schema(name: str) -> dict | None:
    schema_path = get_schemas_dir() / name
    if not schema_path.exists():
        return None
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _load_yaml(path: Path) -> tuple[dict | None, str | None]:
    try:
        content = path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
        if not isinstance(data, dict):
            return None, f"Expected YAML mapping, got {type(data).__name__}"
        return data, None
    except yaml.YAMLError as e:
        return None, f"Invalid YAML: {e}"
    except Exception as e:
        return None, str(e)


WORK_ORDER_STATE_DIRS = ("ACTIVE", "BLOCKED", "COMPLETED")
WORK_ORDER_ALLOWED_STATUSES = {
    "ACTIVE": {"ACTIVE"},
    "BLOCKED": {"BLOCKED"},
    "COMPLETED": {"COMPLETE", "COMPLETED"},
}


def _iter_work_order_files(sync_path: Path):
    work_orders_dir = sync_path / "work-orders"
    for state_dir in WORK_ORDER_STATE_DIRS:
        state_path = work_orders_dir / state_dir
        if not state_path.exists():
            continue
        for work_order_file in sorted(state_path.glob("*.yaml")):
            yield state_dir, work_order_file


def _append_schema_errors(file_rel: str, data: dict, schema: dict, result: ValidationResult) -> None:
    validator = Draft7Validator(schema)
    for error in validator.iter_errors(data):
        json_path = ".".join(str(p) for p in error.absolute_path) or "(root)"
        result.issues.append(Issue(
            layer="Schema",
            severity=Severity.ERROR,
            message=f"{file_rel}: {json_path} — {error.message}",
            path=file_rel,
        ))


def _normalize_work_order_status(status: str | None) -> str | None:
    if not status:
        return status
    status_upper = str(status).upper()
    if status_upper in ("COMPLETE", "COMPLETED"):
        return "COMPLETED"
    return status_upper


def _discover_agents(sync_path: Path) -> list[str]:
    boot_dir = sync_path / "runtime" / "boot"
    if not boot_dir.exists():
        return []
    return sorted(
        f.stem.removesuffix(".boot")
        for f in boot_dir.glob("*.boot.yaml")
    )


# ─── Layer 1: Schema Validation ──────────────────────────────────


def validate_schema(sync_path: Path, result: ValidationResult) -> None:
    schema_checks = [
        ("runtime/TREE.yaml", "tree.schema.json"),
        ("RUNTIME_VERSION", "runtime-version.schema.json"),
        ("work-orders/INDEX.yaml", "index.schema.json"),
    ]

    for file_rel, schema_name in schema_checks:
        file_path = sync_path / file_rel
        if not file_path.exists():
            continue

        data, err = _load_yaml(file_path)
        if err:
            result.issues.append(Issue(
                layer="Schema",
                severity=Severity.ERROR,
                message=f"{file_rel}: {err}",
                path=file_rel,
            ))
            continue

        schema = _load_schema(schema_name)
        if schema is None:
            continue

        _append_schema_errors(file_rel, data, schema, result)

    # Validate boot snapshots
    boot_schema = _load_schema("boot.schema.json")
    boot_dir = sync_path / "runtime" / "boot"
    if boot_dir.exists() and boot_schema:
        for boot_file in boot_dir.iterdir():
            if boot_file.suffix != ".yaml":
                continue
            data, err = _load_yaml(boot_file)
            if err:
                result.issues.append(Issue(
                    layer="Schema",
                    severity=Severity.ERROR,
                    message=f"runtime/boot/{boot_file.name}: {err}",
                    path=f"runtime/boot/{boot_file.name}",
                ))
                continue

            _append_schema_errors(
                f"runtime/boot/{boot_file.name}",
                data,
                boot_schema,
                result,
            )

    work_order_schema = _load_schema("work-order.schema.json")
    if work_order_schema:
        for _, work_order_file in _iter_work_order_files(sync_path):
            file_rel = work_order_file.relative_to(sync_path).as_posix()
            data, err = _load_yaml(work_order_file)
            if err:
                result.issues.append(Issue(
                    layer="Schema",
                    severity=Severity.ERROR,
                    message=f"{file_rel}: {err}",
                    path=file_rel,
                ))
                continue

            _append_schema_errors(file_rel, data, work_order_schema, result)


# ─── Layer 2: Structural Validation ─────────────────────────────


def validate_structure(project_path: Path, sync_path: Path, agents: list[str], result: ValidationResult) -> None:
    if not (project_path / "AGENTS.md").exists():
        result.issues.append(Issue(
            layer="Structure",
            severity=Severity.ERROR,
            message="Missing: AGENTS.md in project root",
            path="AGENTS.md",
        ))

    required_files = [
        "PROTOCOL_DIGEST.md",
        "PROTOCOL_DIGEST.hash",
        "RUNTIME_VERSION",
        "runtime/TREE.yaml",
        "work-orders/INDEX.yaml",
    ]

    for f in required_files:
        if not (sync_path / f).exists():
            result.issues.append(Issue(
                layer="Structure",
                severity=Severity.ERROR,
                message=f"Missing: .sync/{f}",
                path=f,
            ))

    required_dirs = [
        "decisions",
        "reviews",
        "escalations",
        "standup",
        "releases",
        "state",
        "runtime/drafts",
        "runtime/receipts",
        "work-orders/ACTIVE",
        "work-orders/COMPLETED",
        "work-orders/BLOCKED",
        "work-orders/TEMPLATES",
        "inbox/CEO",
    ]

    for d in required_dirs:
        dir_path = sync_path / d
        if not dir_path.is_dir():
            result.issues.append(Issue(
                layer="Structure",
                severity=Severity.ERROR,
                message=f"Missing directory: .sync/{d}",
                path=d,
            ))
        else:
            gitkeep = dir_path / ".gitkeep"
            if not gitkeep.exists() and not any(dir_path.iterdir()):
                result.issues.append(Issue(
                    layer="Structure",
                    severity=Severity.WARN,
                    message=f"Empty directory without .gitkeep: .sync/{d}",
                    path=d,
                    auto_fixable=True,
                ))

    # CEO inbox _read/ folder
    ceo_read_dir = sync_path / "inbox" / "CEO" / "_read"
    if not ceo_read_dir.is_dir():
        result.issues.append(Issue(
            layer="Structure",
            severity=Severity.ERROR,
            message="Missing: .sync/inbox/CEO/_read/",
            path="inbox/CEO/_read",
            auto_fixable=True,
        ))

    for agent in agents:
        boot_file = sync_path / "runtime" / "boot" / f"{agent}.boot.yaml"
        if not boot_file.exists():
            result.issues.append(Issue(
                layer="Structure",
                severity=Severity.ERROR,
                message=f"Missing: .sync/runtime/boot/{agent}.boot.yaml",
                path=f"runtime/boot/{agent}.boot.yaml",
            ))

        agent_contract = sync_path / "agents" / f"{agent}.agent.md"
        if not agent_contract.exists():
            result.issues.append(Issue(
                layer="Structure",
                severity=Severity.WARN,
                message=f"Missing: .sync/agents/{agent}.agent.md",
                path=f"agents/{agent}.agent.md",
            ))

        inbox_dir = sync_path / "inbox" / agent
        if not inbox_dir.is_dir():
            result.issues.append(Issue(
                layer="Structure",
                severity=Severity.ERROR,
                message=f"Missing: .sync/inbox/{agent}/",
                path=f"inbox/{agent}",
            ))
        else:
            read_dir = inbox_dir / "_read"
            if not read_dir.is_dir():
                result.issues.append(Issue(
                    layer="Structure",
                    severity=Severity.ERROR,
                    message=f"Missing: .sync/inbox/{agent}/_read/",
                    path=f"inbox/{agent}/_read",
                    auto_fixable=True,
                ))

        outbox_dir = sync_path / "outbox" / agent
        if not outbox_dir.is_dir():
            result.issues.append(Issue(
                layer="Structure",
                severity=Severity.WARN,
                message=f"Missing: .sync/outbox/{agent}/",
                path=f"outbox/{agent}",
                auto_fixable=True,
            ))

    # Cross-check: TREE.yaml agents should all have boot files
    tree_path = sync_path / "runtime" / "TREE.yaml"
    if tree_path.exists():
        tree_data, _ = _load_yaml(tree_path)
        if tree_data and "agents" in tree_data:
            for agent_name in tree_data["agents"]:
                boot_file = sync_path / "runtime" / "boot" / f"{agent_name}.boot.yaml"
                if not boot_file.exists():
                    result.issues.append(Issue(
                        layer="Structure",
                        severity=Severity.ERROR,
                        message=f"Agent '{agent_name}' in TREE.yaml but missing boot file: {agent_name}.boot.yaml",
                        path=f"runtime/boot/{agent_name}.boot.yaml",
                    ))

    # Git repo checks
    if not (sync_path / ".git").exists():
        result.issues.append(Issue(
            layer="Structure",
            severity=Severity.WARN,
            message=".sync/ is not a git repository",
            path=".git",
        ))

    # Check if .sync is accidentally tracked in the main project git repository
    if (project_path / ".git").exists():
        try:
            completed = subprocess.run(
                ["git", "ls-files", ".sync"],
                cwd=str(project_path),
                capture_output=True,
                check=True,
                text=True,
            )
            if completed.stdout.strip():
                result.issues.append(Issue(
                    layer="Structure",
                    severity=Severity.WARN,
                    message=(
                        ".sync/ directory is tracked in the main project Git repository index. "
                        "Run 'git rm -r --cached .sync' to untrack it."
                    ),
                    path=".sync",
                    auto_fixable=True,
                ))
        except (subprocess.CalledProcessError, FileNotFoundError, OSError):
            pass


# ─── Layer 3: Protocol Compliance ───────────────────────────────


def validate_protocol(sync_path: Path, agents: list[str], result: ValidationResult) -> None:
    # Check PROTOCOL_DIGEST.hash matches PROTOCOL_DIGEST.md
    hash_file = sync_path / "PROTOCOL_DIGEST.hash"
    digest_file = sync_path / "PROTOCOL_DIGEST.md"

    if hash_file.exists() and digest_file.exists():
        stored_hash = hash_file.read_text(encoding="utf-8").strip()
        content = digest_file.read_bytes().replace(b"\r\n", b"\n")
        computed_hash = hashlib.sha256(content).hexdigest().upper()
        if stored_hash != computed_hash:
            result.issues.append(Issue(
                layer="Protocol",
                severity=Severity.ERROR,
                message=(
                    f"PROTOCOL_DIGEST.hash mismatch: "
                    f"stored={stored_hash[:16]}... computed={computed_hash[:16]}..."
                ),
                path="PROTOCOL_DIGEST.hash",
            ))

    # Check TREE.yaml has authority model (agents section must exist)
    tree_path = sync_path / "runtime" / "TREE.yaml"
    data = None
    if tree_path.exists():
        data, _ = _load_yaml(tree_path)
        if data:
            tree_agents = data.get("agents", {})
            for agent in agents:
                if agent not in tree_agents:
                    result.issues.append(Issue(
                        layer="Protocol",
                        severity=Severity.WARN,
                        message=f"Agent '{agent}' has boot snapshot but missing from TREE.yaml agents",
                        path="runtime/TREE.yaml",
                    ))

    # Validate blocked agents have non-empty blockers with valid references
    if data:
        _validate_blocked_agents(sync_path, data, result)
        _validate_work_order_state_files(sync_path, result)

    # Validate the write lock (PLAT-03) integrity
    _validate_lock(sync_path, agents, result)

    # Validate compliance events (LOCK_STOLEN)
    _validate_compliance_events(sync_path, result)

    # CODEX-02: flag untracked paths in the .sync repo as compliance warnings
    _validate_untracked_sync(sync_path, result)

    # GEMINI-03: flag review files that bundle multiple work orders
    _validate_review_files(sync_path, result)

    # GEMINI-04: require a declared release_target on completion notices
    _validate_completion_notices(sync_path, result)

    # Validate handoff reports (GEMINI-02, LOCAL-LLM-01)
    _validate_handoff_reports(sync_path, result)

    # Ungoverned change detection
    _validate_ungoverned_changes(sync_path, result)


# Unanchored work-order reference pattern (for scanning prose/filenames).
WO_REF_PATTERN = re.compile(r"WO-\d{3}")


def _iter_review_files(sync_path: Path):
    """Yield D024 review-*request* files, skipping processed (_read/) items.

    Only true review-request files are considered — those whose name follows
    the D024 convention ``<date>_<agent>_<wo-id>-review.md`` (i.e. the name
    ends in ``-review.md``). This deliberately excludes related-but-different
    artifacts that merely contain the word "review" in their name, such as
    review *plans* (``...-review-plan.md``) or QA *verdicts*
    (``...-verdict.md``), which legitimately reference several work orders.

    Files are discovered under the dedicated ``reviews/`` directory and inside
    reviewer inboxes (``inbox/.../``).
    """
    seen = set()
    for base in (sync_path / "reviews", sync_path / "inbox"):
        if not base.is_dir():
            continue
        for path in base.rglob("*.md"):
            if "_read" in path.parts:
                continue
            if not path.name.lower().endswith("-review.md"):
                continue
            if path in seen:
                continue
            seen.add(path)
            yield path


def _validate_review_files(sync_path: Path, result: ValidationResult) -> None:
    """Flag review-request files that name more than one work order (GEMINI-03).

    D024 requires exactly one review request file per work order. The work
    order a request covers is encoded in its **filename**
    (``<wo-id>-review.md``); a request filed under two or more distinct WO IDs
    breaks per-WO auditability and is a protocol violation (ERROR).

    Only the filename is inspected — not the body — because a review request
    legitimately references related work orders (dependencies, prior reviews)
    in its prose, and scanning content produces false positives.
    """
    for review_path in _iter_review_files(sync_path):
        wo_ids = sorted(set(WO_REF_PATTERN.findall(review_path.name)))
        if len(wo_ids) > 1:
            rel = review_path.relative_to(sync_path).as_posix()
            result.issues.append(Issue(
                layer="Protocol",
                severity=Severity.ERROR,
                message=(
                    f"Review request names multiple work orders "
                    f"({', '.join(wo_ids)}): {rel} "
                    f"— D024 requires one review file per work order (GEMINI-03)"
                ),
                path=rel,
            ))


# Matches a `release_target:` field with a non-empty value (ignoring an empty
# value or a placeholder like "TBD"/"none"/"<...>").
# Matches release_target in various formats:
#   release_target: v1.0.0
#   release_target: "v1.0.0"
#   - **Release Target:** v1.0.0
#   **release_target:** v1.0.0
#   | release_target | v1.0.0 |
RELEASE_TARGET_PATTERN = re.compile(
    r"(?mi)^[ \t\-*|]*release[_ ]?target[ \t*]*:[ \t*|]*[\"']?(.*?)[\"']?[ \t|]*$"
)
_RELEASE_TARGET_PLACEHOLDERS = {"", "tbd", "none", "null", "n/a", "-"}


def _iter_completion_notices(sync_path: Path):
    """Yield work-order completion-notice files, skipping processed items.

    Per D024, a worker writes a completion notice to the manager's inbox as
    ``inbox/claude/<date>_<agent>_<wo-id>-complete.md``. Only files that match
    this shape are considered:

      * located in ``inbox/claude/`` (the manager inbox), and
      * whose filename names a specific work order (``WO-NNN``) and contains
        "complete".

    This excludes release-level notices/decisions addressed elsewhere (e.g.
    ``inbox/CEO/..._v1-release-complete.md``), which are not per-work-order
    completion notices and have no ``release_target`` obligation.
    """
    inbox = sync_path / "inbox" / "claude"
    if not inbox.is_dir():
        return
    for path in inbox.rglob("*.md"):
        if "_read" in path.parts:
            continue
        name = path.name.lower()
        if "complete" not in name:
            continue
        # Exclude commit reports (Local-LLM) — they're not implementation completions
        if "commit" in name:
            continue
        if not WO_REF_PATTERN.search(path.name):
            continue
        yield path


def _validate_completion_notices(sync_path: Path, result: ValidationResult) -> None:
    """Require a declared ``release_target`` on completion notices (GEMINI-04).

    A work order completed after a release tag must declare which release it
    targets so Claude can make the final versioning decision. Shipping a
    completion notice with no declared release target leaves versioning
    undefined for in-flight changes and is a protocol violation (ERROR).
    """
    for notice_path in _iter_completion_notices(sync_path):
        try:
            content = notice_path.read_text(encoding="utf-8")
        except OSError:
            content = ""

        match = RELEASE_TARGET_PATTERN.search(content)
        value = match.group(1).strip().lower() if match else ""

        if value in _RELEASE_TARGET_PLACEHOLDERS:
            rel = notice_path.relative_to(sync_path).as_posix()
            result.issues.append(Issue(
                layer="Protocol",
                severity=Severity.ERROR,
                message=(
                    f"Completion notice missing a declared 'release_target': {rel} "
                    f"— workers should declare a release target (GEMINI-04)"
                ),
                path=rel,
            ))


def _validate_untracked_sync(sync_path: Path, result: ValidationResult) -> None:
    """Flag untracked paths in the .sync git repo (CODEX-02).

    In the .sync repo, an untracked file represents uncommitted work or an
    orphaned artifact — potential non-compliance evidence (e.g. review files
    never dispatched). The D023 shutdown sequence requires relevant paths to be
    committed, so any untracked path is surfaced as a compliance WARNING.

    The check is skipped silently when .sync is not a git repository or git is
    unavailable, so it never blocks validation in non-git environments.
    """
    if not (sync_path / ".git").exists():
        return

    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(sync_path),
            capture_output=True,
            check=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        # git missing or repo unreadable — do not block validation.
        return

    for line in completed.stdout.splitlines():
        # Porcelain v1 untracked entries are prefixed with "?? ".
        if not line.startswith("??"):
            continue
        untracked_path = line[3:].strip().strip('"')
        if not untracked_path:
            continue
        # Knowledge store files are compiler output — not governance state.
        # They don't need to be committed to satisfy protocol compliance.
        if untracked_path.startswith("knowledge/"):
            continue
        result.issues.append(Issue(
            layer="Protocol",
            severity=Severity.WARN,
            message=(
                f"Untracked path in .sync repo: {untracked_path} "
                f"— commit or remove it before assigning new work (CODEX-02)"
            ),
            path=untracked_path,
            auto_fixable=True,
        ))


def _validate_lock(sync_path: Path, agents: list[str], result: ValidationResult) -> None:
    """Validate the .sync/runtime/LOCK write-lock marker (PLAT-03).

    A held, well-formed lock is normal during an active session and is not an
    issue. This check flags only integrity problems: a LOCK file that exists
    but is malformed (ERROR), or one whose holder is not a known agent (WARN).
    """
    from .lock import get_lock_path, lock_is_malformed, read_lock

    lock_path = get_lock_path(sync_path)
    if not lock_path.exists():
        return

    if lock_is_malformed(sync_path):
        result.issues.append(Issue(
            layer="Protocol",
            severity=Severity.ERROR,
            message=(
                "runtime/LOCK exists but is malformed "
                "(must be a YAML mapping with a 'held_by' field)"
            ),
            path="runtime/LOCK",
        ))
        return

    lock_data = read_lock(sync_path)
    holder = lock_data.get("held_by") if lock_data else None

    # Determine the set of known agents (boot snapshots + TREE.yaml agents).
    known_agents = set(agents)
    tree_path = sync_path / "runtime" / "TREE.yaml"
    if tree_path.exists():
        tree_data, _ = _load_yaml(tree_path)
        if tree_data and isinstance(tree_data.get("agents"), dict):
            known_agents.update(tree_data["agents"].keys())

    if holder is not None and holder not in known_agents:
        result.issues.append(Issue(
            layer="Protocol",
            severity=Severity.WARN,
            message=(
                f"runtime/LOCK held by unknown agent '{holder}' "
                f"(not found in boot snapshots or TREE.yaml)"
            ),
            path="runtime/LOCK",
        ))


def _validate_compliance_events(sync_path: Path, result: ValidationResult) -> None:
    """Scan for LOCK_STOLEN compliance events and report them."""
    receipts_dir = sync_path / "runtime" / "receipts"
    if not receipts_dir.is_dir():
        return

    for path in sorted(receipts_dir.glob("LOCK_STOLEN_*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        if not isinstance(data, dict):
            continue

        if data.get("event_type") == "LOCK_STOLEN":
            agent = data.get("agent", "unknown")
            stolen_from = data.get("stolen_from", "unknown")
            timestamp = data.get("timestamp", "unknown")
            result.issues.append(Issue(
                layer="Protocol",
                severity=Severity.WARN,
                message=f"LOCK was stolen by '{agent}' from '{stolen_from}' at {timestamp} (LOCK_STOLEN)",
                path=f"runtime/receipts/{path.name}",
            ))


def _validate_handoff_reports(sync_path: Path, result: ValidationResult) -> None:
    """Scan for agent handoff reports and validate their contents."""
    outbox = sync_path / "outbox"
    if not outbox.is_dir():
        return

    for agent_dir in outbox.iterdir():
        if not agent_dir.is_dir():
            continue
        agent_name = agent_dir.name
        
        # Scan only unarchived handoff files in outbox; archived items in _read/ are immutable history
        for path in sorted(agent_dir.glob("handoff-*.md")):
            _validate_handoff_file(path, agent_name, result, sync_path)
        for path in sorted(agent_dir.glob("*_session-report.md")):
            _validate_handoff_file(path, agent_name, result, sync_path)


def _validate_handoff_file(path: Path, agent: str, result: ValidationResult, sync_path: Path) -> None:
    """Validate a single handoff report's content against protocol rules."""
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return

    completed_text = ""
    next_tasks_text = ""
    
    lines = content.splitlines()
    current_section = None
    
    for line in lines:
        if "COMPLETED THIS SESSION" in line:
            current_section = "completed"
            continue
        elif "MY NEXT TASKS" in line:
            current_section = "next_tasks"
            continue
        elif any(marker in line for marker in ("MESSAGES", "BLOCKERS", "WAITING ON", "DECISION NEEDED", "next_session_id")):
            current_section = None
            continue
            
        if current_section == "completed":
            completed_text += line + "\n"
        elif current_section == "next_tasks":
            next_tasks_text += line + "\n"

    # Relative path for reporting issues
    rel_path = path.relative_to(sync_path).as_posix()

    # GEMINI-02: WO in next tasks must cite assignment source
    for line in next_tasks_text.splitlines():
        wo_match = re.search(r"WO-\d{3}", line)
        if wo_match:
            wo_id = wo_match.group(0)
            lower_line = line.lower()
            # Skip lines that indicate no active assignment (completed, none, awaiting)
            if any(phrase in lower_line for phrase in (
                "none", "complete", "closed", "released", "done",
                "shipped", "finished", "delivered", "delegated",
                "awaiting", "await", "no remaining", "no open",
                "stray", "cleanup",
            )):
                continue
            if "assigned by" not in lower_line:
                result.issues.append(Issue(
                    layer="Protocol",
                    severity=Severity.ERROR,
                    message=(
                        f"Handoff report '{path.name}' lists next task {wo_id} "
                        f"but is missing assignment source (must cite 'assigned by') (GEMINI-02)"
                    ),
                    path=rel_path,
                ))

    # LOCAL-LLM-01: Delegated items in completed section must declare delegating_agent
    if "delegate" in completed_text.lower() or "delegated" in completed_text.lower():
        if "delegating_agent:" not in completed_text:
            result.issues.append(Issue(
                layer="Protocol",
                severity=Severity.ERROR,
                message=(
                    f"Handoff report '{path.name}' contains delegated tasks in COMPLETED section "
                    f"but is missing 'delegating_agent:' declaration (LOCAL-LLM-01)"
                ),
                path=rel_path,
            ))


def _validate_ungoverned_changes(sync_path: Path, result: ValidationResult) -> None:
    """Detect source file modifications with no active work orders.

    When the ACTIVE work-orders directory is empty (no .yaml files) but the
    project has modified or untracked Python source files, this is an
    ungoverned change — code is being written without a governing work order.
    Emits a WARN so agents and developers are reminded to create a WO first.
    """
    active_dir = sync_path / "work-orders" / "ACTIVE"
    if not active_dir.is_dir():
        return

    # Check if there are any .yaml work order files (ignore .gitkeep)
    has_active_wo = any(
        f.suffix == ".yaml" for f in active_dir.iterdir() if f.is_file()
    )
    if has_active_wo:
        return

    # Project root is the parent of .sync/
    project_path = sync_path.parent

    # Must be a git repo
    if not (project_path / ".git").exists():
        return

    # Check git status for modified/untracked .py files in the project
    try:
        completed = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=str(project_path),
            capture_output=True,
            check=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return

    has_py_changes = any(
        line.rstrip().endswith(".py")
        for line in completed.stdout.splitlines()
        if line.strip()
    )

    if has_py_changes:
        result.issues.append(Issue(
            layer="Protocol",
            severity=Severity.WARN,
            message=(
                "Ungoverned changes detected: project source files modified "
                "with no active work orders. Create a work order before implementing."
            ),
            path="work-orders/ACTIVE",
        ))


# Work order ID pattern
WO_PATTERN = re.compile(r"^WO-\d{3}$")


def _validate_blocked_agents(sync_path: Path, tree_data: dict, result: ValidationResult) -> None:
    """Validate that blocked agents have valid blocker references."""
    if not tree_data:
        return

    tree_agents = tree_data.get("agents", {})
    agent_names = set(tree_agents.keys())

    # Load work order IDs from INDEX.yaml
    index_path = sync_path / "work-orders" / "INDEX.yaml"
    wo_ids: set[str] = set()
    if index_path.exists():
        index_data, _ = _load_yaml(index_path)
        if index_data and "orders" in index_data:
            wo_ids = {order["id"] for order in index_data["orders"] if "id" in order}

    for agent_name, agent_data in tree_agents.items():
        if not isinstance(agent_data, dict):
            continue

        status = agent_data.get("status")
        blockers = agent_data.get("blockers", [])

        if status == "blocked":
            # Blocked agents must have non-empty blockers
            if not blockers:
                result.issues.append(Issue(
                    layer="Protocol",
                    severity=Severity.ERROR,
                    message=f"Agent '{agent_name}' has status 'blocked' but empty blockers list",
                    path="runtime/TREE.yaml",
                ))
                continue

            # Validate each blocker reference
            for blocker in blockers:
                if WO_PATTERN.match(blocker):
                    # It's a work order reference
                    if blocker not in wo_ids:
                        result.issues.append(Issue(
                            layer="Protocol",
                            severity=Severity.ERROR,
                            message=f"Agent '{agent_name}' blocked by non-existent work order '{blocker}'",
                            path="runtime/TREE.yaml",
                        ))
                else:
                    # It's an agent reference
                    if blocker not in agent_names:
                        result.issues.append(Issue(
                            layer="Protocol",
                            severity=Severity.ERROR,
                            message=f"Agent '{agent_name}' blocked by non-existent agent '{blocker}'",
                            path="runtime/TREE.yaml",
                        ))

    # Validate work order deliverables
    _validate_work_order_deliverables(sync_path, result)


# Work order types that require a deliverable
DELIVERABLE_REQUIRED_TYPES = {"FEATURE", "BUGFIX", "HOTFIX", "REFACTOR", "FIX"}


def _validate_work_order_deliverables(sync_path: Path, result: ValidationResult) -> None:
    """Validate that work orders requiring deliverables have them.

    Checks INDEX.yaml first; if deliverable is missing there, falls back to
    the actual WO file in ACTIVE/BLOCKED/COMPLETED. The INDEX is a summary
    ledger — deliverable in the WO file is sufficient.
    """
    index_path = sync_path / "work-orders" / "INDEX.yaml"
    if not index_path.exists():
        return

    index_data, _ = _load_yaml(index_path)
    if not index_data or "orders" not in index_data:
        return

    for order in index_data["orders"]:
        wo_id = order.get("id", "unknown")
        wo_type = order.get("type")
        deliverable = order.get("deliverable")

        if wo_type in DELIVERABLE_REQUIRED_TYPES and not deliverable:
            if order.get("deliverables"):
                continue
            # Fallback: check the actual WO file
            wo_found_in_file = False
            for subdir in ("ACTIVE", "BLOCKED", "COMPLETED"):
                wo_file = sync_path / "work-orders" / subdir / f"{wo_id}.yaml"
                if wo_file.exists():
                    wo_data, _ = _load_yaml(wo_file)
                    if wo_data and (wo_data.get("deliverable") or wo_data.get("deliverables") or wo_data.get("files_created")):
                        wo_found_in_file = True
                    break
            if not wo_found_in_file:
                result.issues.append(Issue(
                    layer="Protocol",
                    severity=Severity.ERROR,
                    message=f"Work order '{wo_id}' (type={wo_type}) requires a deliverable field",
                    path="work-orders/INDEX.yaml",
                ))

    _validate_rework_budgets(sync_path, index_data, result)


def _validate_work_order_state_files(sync_path: Path, result: ValidationResult) -> None:
    """Validate work-order placement and status across state directories."""
    index_path = sync_path / "work-orders" / "INDEX.yaml"
    index_data, _ = _load_yaml(index_path) if index_path.exists() else (None, None)
    index_orders: dict[str, dict] = {}
    if index_data and "orders" in index_data:
        index_orders = {
            order["id"]: order
            for order in index_data["orders"]
            if isinstance(order, dict) and "id" in order
        }

    file_locations: dict[str, list[tuple[str, str, dict]]] = {}
    for state_dir, work_order_file in _iter_work_order_files(sync_path):
        work_order_data, err = _load_yaml(work_order_file)
        if err or not work_order_data:
            continue

        file_rel = work_order_file.relative_to(sync_path).as_posix()
        work_order_id = work_order_data.get("id") or work_order_file.stem
        file_locations.setdefault(work_order_id, []).append(
            (state_dir, file_rel, work_order_data)
        )

    for work_order_id, locations in sorted(file_locations.items()):
        state_dirs = sorted({state_dir for state_dir, _, _ in locations})
        if len(state_dirs) > 1:
            result.issues.append(Issue(
                layer="Protocol",
                severity=Severity.ERROR,
                message=(
                    f"Work order '{work_order_id}' exists in multiple state directories: "
                    f"{', '.join(state_dirs)}"
                ),
                path="work-orders",
            ))

        index_order = index_orders.get(work_order_id)
        index_status = _normalize_work_order_status(
            index_order.get("status") if index_order else None
        )

        for state_dir, file_rel, work_order_data in locations:
            file_status = work_order_data.get("status")
            normalized_file_status = _normalize_work_order_status(file_status)
            allowed_statuses = {
                _normalize_work_order_status(status)
                for status in WORK_ORDER_ALLOWED_STATUSES[state_dir]
            }

            if normalized_file_status not in allowed_statuses:
                allowed_display = ", ".join(sorted(WORK_ORDER_ALLOWED_STATUSES[state_dir]))
                result.issues.append(Issue(
                    layer="Protocol",
                    severity=Severity.ERROR,
                    message=(
                        f"{file_rel} has status '{file_status}' but directory '{state_dir}' "
                        f"requires {allowed_display}"
                    ),
                    path=file_rel,
                ))

            if index_order is None:
                result.issues.append(Issue(
                    layer="Protocol",
                    severity=Severity.ERROR,
                    message=f"{file_rel} has no matching entry in work-orders/INDEX.yaml",
                    path=file_rel,
                ))
                continue

            if normalized_file_status != index_status:
                result.issues.append(Issue(
                    layer="Protocol",
                    severity=Severity.ERROR,
                    message=(
                        f"{file_rel} status '{file_status}' does not match INDEX.yaml "
                        f"status '{index_order.get('status')}' for '{work_order_id}'"
                    ),
                    path=file_rel,
                ))


# Default rework budget for actionable work order types
DEFAULT_REWORK_BUDGET = 2


def _validate_rework_budgets(sync_path: Path, index_data: dict, result: ValidationResult) -> None:
    """Validate rework budget constraints on work orders."""
    if not index_data or "orders" not in index_data:
        return

    escalations_dir = sync_path / "escalations"

    for order in index_data["orders"]:
        wo_id = order.get("id", "unknown")
        wo_type = order.get("type")

        if wo_type not in DELIVERABLE_REQUIRED_TYPES:
            continue

        rework_budget = order.get("rework_budget", DEFAULT_REWORK_BUDGET)
        rework_count = order.get("rework_count", 0)
        blocked_by_rework = order.get("blocked_by_rework", False)

        if rework_count >= rework_budget and not blocked_by_rework:
            result.issues.append(Issue(
                layer="Protocol",
                severity=Severity.ERROR,
                message=(
                    f"Work order '{wo_id}' exhausted rework budget "
                    f"({rework_count}/{rework_budget}) but blocked_by_rework is not set"
                ),
                path="work-orders/INDEX.yaml",
            ))

        if blocked_by_rework:
            escalation_file = escalations_dir / f"{wo_id}-rework.yaml"
            if not escalation_file.exists():
                result.issues.append(Issue(
                    layer="Protocol",
                    severity=Severity.ERROR,
                    message=(
                        f"Work order '{wo_id}' blocked_by_rework=true "
                        f"but no escalation file at escalations/{wo_id}-rework.yaml"
                    ),
                    path=f"escalations/{wo_id}-rework.yaml",
                ))


# ─── Layer 4: Boot Integrity ────────────────────────────────────


# Work-order total fields cross-checked between TREE.yaml and INDEX.yaml.
# These are the canonical counters that must agree across the two sources of
# truth; divergence indicates that one file was updated without the other.
CANONICAL_TOTAL_FIELDS = ("total_active", "total_blocked", "total_completed")

# Maximum number of tree_version revisions an agent's boot snapshot may lag
# behind TREE.yaml before it is flagged as a session-continuity risk (GEMMA-01).
SNAPSHOT_VERSION_LAG_THRESHOLD = 3


def _validate_canonical_drift(sync_path: Path, tree_data: dict, result: ValidationResult) -> None:
    """PLAT-01 / CODEX-03: cross-check TREE.yaml against INDEX.yaml.

    The boot-integrity check used to compare boot snapshots only against
    TREE.yaml. When TREE.yaml itself was stale, the comparison passed because
    two stale artifacts agreed with each other rather than with ground truth.

    INDEX.yaml is the work-order ledger and stays current through normal work
    order operations, so its totals are treated as the external anchor. Any
    disagreement between TREE.yaml's ``work_orders`` counters and INDEX.yaml's
    top-level counters is reported as canonical drift (ERROR).
    """
    index_path = sync_path / "work-orders" / "INDEX.yaml"
    if not index_path.exists():
        return

    index_data, err = _load_yaml(index_path)
    if err or not index_data:
        return

    tree_totals = tree_data.get("work_orders")
    if not isinstance(tree_totals, dict):
        return

    for field_name in CANONICAL_TOTAL_FIELDS:
        tree_value = tree_totals.get(field_name)
        index_value = index_data.get(field_name)

        # Only compare when both sources declare the counter.
        if tree_value is None or index_value is None:
            continue

        if tree_value != index_value:
            result.issues.append(Issue(
                layer="Boot Integrity",
                severity=Severity.ERROR,
                message=(
                    f"Canonical drift: TREE.yaml work_orders.{field_name} "
                    f"({tree_value}) != INDEX.yaml {field_name} ({index_value}) "
                    f"— normalize TREE.yaml against the work-order ledger"
                ),
                path="runtime/TREE.yaml",
                auto_fixable=True,
            ))


def validate_boot_integrity(sync_path: Path, agents: list[str], result: ValidationResult) -> None:
    tree_path = sync_path / "runtime" / "TREE.yaml"
    if not tree_path.exists():
        return

    tree_data, err = _load_yaml(tree_path)
    if err or not tree_data:
        return

    # PLAT-01 / CODEX-03: anchor TREE.yaml to the work-order ledger before
    # trusting it as the reference for per-agent boot comparisons.
    _validate_canonical_drift(sync_path, tree_data, result)

    tree_version = tree_data.get("tree_version")
    protocol_hash_file = sync_path / "PROTOCOL_DIGEST.hash"
    stored_protocol_hash = None
    if protocol_hash_file.exists():
        stored_protocol_hash = protocol_hash_file.read_text(encoding="utf-8").strip()

    boot_dir = sync_path / "runtime" / "boot"
    if not boot_dir.exists():
        return

    for agent in agents:
        boot_file = boot_dir / f"{agent}.boot.yaml"
        if not boot_file.exists():
            continue

        boot_data, err = _load_yaml(boot_file)
        if err or not boot_data:
            continue

        # tree_version alignment
        boot_tree_version = boot_data.get("tree_version")
        if boot_tree_version is not None and tree_version is not None:
            if boot_tree_version > tree_version:
                result.issues.append(Issue(
                    layer="Boot Integrity",
                    severity=Severity.ERROR,
                    message=(
                        f"{agent}.boot.yaml tree_version ({boot_tree_version}) "
                        f"exceeds TREE.yaml tree_version ({tree_version})"
                    ),
                    path=f"runtime/boot/{agent}.boot.yaml",
                ))
            else:
                # GEMMA-01: a snapshot that lags TREE.yaml by more than the
                # configured threshold signals broken session continuity — the
                # agent is likely doing full TREE re-reads every boot.
                version_lag = tree_version - boot_tree_version
                if version_lag > SNAPSHOT_VERSION_LAG_THRESHOLD:
                    result.issues.append(Issue(
                        layer="Boot Integrity",
                        severity=Severity.WARN,
                        message=(
                            f"{agent}.boot.yaml snapshot version lag: "
                            f"{version_lag} versions behind current TREE "
                            f"(boot={boot_tree_version}, tree={tree_version}, "
                            f"threshold={SNAPSHOT_VERSION_LAG_THRESHOLD}) "
                            f"— agent may be running without session continuity; "
                            f"recommend promoting a fresh {agent} snapshot"
                        ),
                        path=f"runtime/boot/{agent}.boot.yaml",
                    ))

        # protocol hash alignment
        boot_hash = boot_data.get("protocol_digest_hash")
        if boot_hash and stored_protocol_hash:
            if boot_hash != stored_protocol_hash:
                result.issues.append(Issue(
                    layer="Boot Integrity",
                    severity=Severity.WARN,
                    message=(
                        f"{agent}.boot.yaml protocol_digest_hash stale "
                        f"(boot={boot_hash[:16]}... current={stored_protocol_hash[:16]}...)"
                    ),
                    path=f"runtime/boot/{agent}.boot.yaml",
                ))

        # session_count must not be negative
        session_count = boot_data.get("session_count", 0)
        if session_count < 0:
            result.issues.append(Issue(
                layer="Boot Integrity",
                severity=Severity.ERROR,
                message=f"{agent}.boot.yaml has negative session_count: {session_count}",
                path=f"runtime/boot/{agent}.boot.yaml",
            ))

        # graph_version alignment
        tree_graph_version = tree_data.get("graph_version")
        boot_graph_version = boot_data.get("graph_version")
        if boot_graph_version is not None:
            if tree_graph_version is None:
                result.issues.append(Issue(
                    layer="Boot Integrity",
                    severity=Severity.ERROR,
                    message=(
                        f"{agent}.boot.yaml has graph_version but TREE.yaml graph_version is null"
                    ),
                    path=f"runtime/boot/{agent}.boot.yaml",
                ))
            elif boot_graph_version != tree_graph_version:
                result.issues.append(Issue(
                    layer="Boot Integrity",
                    severity=Severity.ERROR,
                    message=(
                        f"{agent}.boot.yaml graph_version mismatch "
                        f"(boot={boot_graph_version[:16]}... tree={tree_graph_version[:16]}...)"
                    ),
                    path=f"runtime/boot/{agent}.boot.yaml",
                ))


# ─── Layer 5: .sync-ref Anchoring (PLAT-05) ─────────────────────


def _git_head(sync_path: Path) -> str | None:
    """Return the .sync repo HEAD commit SHA, or None if unavailable."""
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(sync_path),
            capture_output=True,
            check=True,
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    return completed.stdout.strip() or None


def validate_sync_ref(project_path: Path, sync_path: Path, result: ValidationResult) -> None:
    """Anchor the main repo to the .sync repo via a tracked .sync-ref (PLAT-05).

    ``.sync/`` is git-ignored by the main repo, so its commit state is invisible
    from ``git status`` in the main project. A ``.sync-ref`` file tracked in the
    main repo records the last-known-good ``.sync`` commit SHA. This check
    compares the live ``.sync`` HEAD against ``.sync-ref`` and warns if they
    diverge — surfacing unexpected (or missing) ``.sync`` commits.

    Skipped silently when ``.sync-ref`` is absent, ``.sync`` is not a git repo,
    or git is unavailable, so it never blocks validation in those cases.
    """
    ref_file = project_path / ".sync-ref"
    if not ref_file.exists():
        return
    if not (sync_path / ".git").exists():
        return

    head = _git_head(sync_path)
    if head is None:
        return

    expected = ref_file.read_text(encoding="utf-8").strip()
    if not expected:
        result.issues.append(Issue(
            layer="Boot Integrity",
            severity=Severity.WARN,
            message=".sync-ref is empty — cannot verify .sync repo commit state",
            path=".sync-ref",
        ))
        return

    if not head.startswith(expected) and not expected.startswith(head):
        result.issues.append(Issue(
            layer="Boot Integrity",
            severity=Severity.WARN,
            message=(
                f".sync HEAD ({head[:12]}) does not match .sync-ref "
                f"({expected[:12]}) — the .sync repo has unexpected or "
                f"uncommitted commits relative to the main repo's anchor"
            ),
            path=".sync-ref",
        ))


# ─── Auto-fix ────────────────────────────────────────────────────


def auto_fix(sync_path: Path, issues: list[Issue]) -> int:
    fixed = 0
    synced_tree_totals = False
    committed_sync_repo = False
    for issue in issues:
        if not issue.auto_fixable:
            continue

        if "without .gitkeep" in issue.message:
            dir_path = sync_path / issue.path
            if dir_path.is_dir():
                (dir_path / ".gitkeep").write_text("", encoding="utf-8")
                fixed += 1

        elif "Missing: .sync/inbox/" in issue.message and "_read" in issue.message:
            read_dir = sync_path / issue.path
            read_dir.mkdir(parents=True, exist_ok=True)
            (read_dir / ".gitkeep").write_text("", encoding="utf-8")
            fixed += 1

        elif "Missing: .sync/outbox/" in issue.message:
            outbox_dir = sync_path / issue.path
            outbox_dir.mkdir(parents=True, exist_ok=True)
            (outbox_dir / ".gitkeep").write_text("", encoding="utf-8")
            fixed += 1

        elif "Canonical drift:" in issue.message:
            if not synced_tree_totals:
                index_path = sync_path / "work-orders" / "INDEX.yaml"
                tree_path = sync_path / "runtime" / "TREE.yaml"
                if index_path.exists() and tree_path.exists():
                    index_data, _ = _load_yaml(index_path)
                    tree_data, _ = _load_yaml(tree_path)
                    if index_data and tree_data and isinstance(tree_data.get("work_orders"), dict):
                        for field_name in CANONICAL_TOTAL_FIELDS:
                            if field_name in index_data:
                                tree_data["work_orders"][field_name] = index_data[field_name]
                        tree_path.write_text(yaml.dump(tree_data, default_flow_style=False), encoding="utf-8")
                        synced_tree_totals = True
            fixed += 1

        elif "Untracked path in .sync repo:" in issue.message:
            if not committed_sync_repo and (sync_path / ".git").exists():
                try:
                    subprocess.run(["git", "add", "."], cwd=str(sync_path), capture_output=True, check=False)
                    subprocess.run(["git", "commit", "-m", "chore: auto-commit untracked governance files"], cwd=str(sync_path), capture_output=True, check=False)
                    committed_sync_repo = True
                except Exception:
                    pass
            fixed += 1

        elif ".sync/ directory is tracked in the main project Git repository index" in issue.message:
            project_path = sync_path.parent
            if (project_path / ".git").exists():
                try:
                    subprocess.run(["git", "rm", "-r", "--cached", ".sync"], cwd=str(project_path), capture_output=True, check=False)
                    subprocess.run(["git", "commit", "-m", "chore: untrack .sync from main git repo (validate --fix)"], cwd=str(project_path), capture_output=True, check=False)
                    fixed += 1
                except Exception:
                    pass

    return fixed


# ─── Main Entry Point ────────────────────────────────────────────


def validate(project_path: Path, fix: bool = False) -> ValidationResult:
    """Run full validation on a stackmind runtime.

    Args:
        project_path: Root of the project containing .sync/.
        fix: If True, auto-fix minor issues.

    Returns:
        ValidationResult with all discovered issues.
    """
    project_path = project_path.resolve()
    sync_path = project_path / ".sync"

    result = ValidationResult()

    if not sync_path.exists():
        result.issues.append(Issue(
            layer="Structure",
            severity=Severity.ERROR,
            message="No .sync/ directory found — not a stackmind runtime",
            path=".sync",
        ))
        return result

    # Discover agents from boot snapshots
    agents = _discover_agents(sync_path)
    if not agents:
        result.issues.append(Issue(
            layer="Structure",
            severity=Severity.ERROR,
            message="No agent boot snapshots found in .sync/runtime/boot/",
            path="runtime/boot",
        ))

    # Run all layers
    validate_schema(sync_path, result)
    validate_structure(project_path, sync_path, agents, result)
    validate_protocol(sync_path, agents, result)
    validate_boot_integrity(sync_path, agents, result)

    # PLAT-05: anchor the main repo to the .sync repo via .sync-ref
    validate_sync_ref(project_path, sync_path, result)

    # Layer 5: knowledge registry identity invariants
    validate_knowledge_layer(project_path, result)

    # Auto-fix if requested
    if fix:
        fixable = [i for i in result.issues if i.auto_fixable]
        if fixable:
            fixed_count = auto_fix(sync_path, fixable)
            for issue in fixable:
                if issue in result.issues:
                    result.issues.remove(issue)
            console.print(f"[bold green][+][/bold green] Auto-fixed {fixed_count} issues")

    return result


def validate_knowledge_layer(project_path: Path, result: ValidationResult) -> None:
    """Validate knowledge registry artifacts when present."""
    from validators.knowledge.validate import validate_knowledge

    knowledge_result = validate_knowledge(project_path)
    for issue in knowledge_result.issues:
        result.issues.append(Issue(
            layer="Knowledge",
            severity=Severity.ERROR,
            message=issue.message,
            path=issue.path,
        ))
