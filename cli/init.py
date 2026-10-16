"""stackmind init — Initialize a new stackmind runtime.

Handles:
- Template rendering ({{PLACEHOLDER}} substitution)
- Directory structure creation from templates/sync/
- Git initialization for both project and .sync repos
- PROTOCOL_DIGEST.hash generation (SHA-256)
- RUNTIME_VERSION file creation
- Result validation
"""

import hashlib
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.panel import Panel

console = Console()

# Default agents in the runtime
DEFAULT_AGENTS = ["claude", "codex", "gemini", "gemma", "local-llm"]

# Placeholder pattern: {{VARIABLE_NAME}}
PLACEHOLDER_PATTERN = re.compile(r"\{\{(\w+)\}\}")

# Runtime version for fresh installs
RUNTIME_VERSION = "3.1.0"


def get_templates_dir() -> Path:
    """Return the path to the templates directory shipped with stackmind."""
    return Path(__file__).parent.parent / "templates"


def compute_protocol_hash(protocol_digest_path: Path) -> str:
    """Compute SHA-256 hash of the PROTOCOL_DIGEST.md file.

    Normalizes line endings to LF before hashing to ensure
    deterministic output across platforms (Windows CRLF vs Unix LF).

    Args:
        protocol_digest_path: Path to the PROTOCOL_DIGEST.md file.

    Returns:
        Uppercase hex SHA-256 hash string (64 chars).
    """
    content = protocol_digest_path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(content).hexdigest().upper()


def render_template_string(content: str, context: dict[str, str]) -> str:
    """Render {{PLACEHOLDER}} variables in a template string.

    Args:
        content: Template string with {{PLACEHOLDER}} markers.
        context: Dictionary mapping placeholder names to values.

    Returns:
        Rendered string with all placeholders substituted.
    """
    def replacer(match: re.Match) -> str:
        key = match.group(1)
        if key in context:
            return context[key]
        # Leave unrecognized placeholders as-is
        return match.group(0)

    return PLACEHOLDER_PATTERN.sub(replacer, content)


def render_template_file(
    src: Path,
    dst: Path,
    context: dict[str, str],
) -> None:
    """Render a single template file to a destination path.

    Reads source, substitutes placeholders, writes to destination.
    For non-template files (no .template. in name), copies directly
    unless they contain placeholder markers.

    Args:
        src: Source template file.
        dst: Destination output file.
        context: Placeholder substitution context.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)

    try:
        content = src.read_text(encoding="utf-8")
        rendered = render_template_string(content, context)
        dst.write_text(rendered, encoding="utf-8", newline="\n")
    except UnicodeDecodeError:
        # Binary file — copy as-is
        shutil.copy2(src, dst)


def _strip_template_suffix(name: str) -> str:
    """Remove .template from filename.

    Examples:
        TREE.template.yaml → TREE.yaml
        README.template.md → README.md
        PROTOCOL_DIGEST.md → PROTOCOL_DIGEST.md (unchanged)
    """
    return name.replace(".template", "")


def create_sync_structure(
    templates_sync_dir: Path,
    sync_path: Path,
    context: dict[str, str],
    agents: list[str],
) -> list[Path]:
    """Create the .sync/ directory structure from templates.

    Walks the templates/sync/ directory tree, rendering template files
    and creating empty directories as needed.

    Args:
        templates_sync_dir: Path to templates/sync/ source.
        sync_path: Destination .sync/ path.
        context: Placeholder substitution context.
        agents: List of agent names to include.

    Returns:
        List of all created file paths.
    """
    created_files: list[Path] = []

    for root, dirs, files in os.walk(templates_sync_dir):
        rel_root = Path(root).relative_to(templates_sync_dir)
        dst_root = sync_path / _strip_template_suffix(str(rel_root))

        # Filter agent-specific files/dirs
        for f in files:
            src_file = Path(root) / f
            dst_name = _strip_template_suffix(f)

            # Skip agent templates that aren't in our agent list
            if _is_agent_specific(f, agents):
                continue

            dst_file = dst_root / dst_name
            render_template_file(src_file, dst_file, context)
            created_files.append(dst_file)

        # Create empty directories (for dirs with no files)
        dst_root.mkdir(parents=True, exist_ok=True)

    # Create additional empty directories with .gitkeep
    _create_empty_dirs(sync_path, agents)

    return created_files


def _is_agent_specific(filename: str, agents: list[str]) -> bool:
    """Check if a file is agent-specific and should be filtered.

    Returns True if the file is for an agent NOT in the agents list.
    """
    for agent in DEFAULT_AGENTS:
        # Match patterns like claude.boot.template.yaml, claude.agent.template.md
        agent_prefix = agent + "."
        if filename.startswith(agent_prefix) and agent not in agents:
            return True
    return False


def _create_empty_dirs(sync_path: Path, agents: list[str]) -> None:
    """Create empty directories that need .gitkeep files.

    These directories start empty but need to exist in the repo.
    """
    # Directories that get .gitkeep
    gitkeep_dirs = [
        "decisions",
        "reviews",
        "escalations",
        "standup",
        "releases",
        "state",
        "runtime/drafts",
        "runtime/receipts",
        "runtime/boot",
        "runtime/supervisor",
        "runtime/daemon",
        "experience/records",
        "experience/cache",
        "skills/manifests",
        "skills/active",
        "skills/approvals",
        "skills/verification",
    ]

    for dir_name in gitkeep_dirs:
        dir_path = sync_path / dir_name
        dir_path.mkdir(parents=True, exist_ok=True)
        gitkeep = dir_path / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")

    # Agent inbox directories with _read/ subdirectory
    for agent in agents:
        inbox_dir = sync_path / "inbox" / agent
        inbox_dir.mkdir(parents=True, exist_ok=True)
        (inbox_dir / ".gitkeep").write_text("", encoding="utf-8")
        read_dir = inbox_dir / "_read"
        read_dir.mkdir(parents=True, exist_ok=True)
        (read_dir / ".gitkeep").write_text("", encoding="utf-8")

    # CEO inbox
    ceo_dir = sync_path / "inbox" / "CEO"
    ceo_dir.mkdir(parents=True, exist_ok=True)
    (ceo_dir / ".gitkeep").write_text("", encoding="utf-8")
    ceo_read_dir = ceo_dir / "_read"
    ceo_read_dir.mkdir(parents=True, exist_ok=True)
    (ceo_read_dir / ".gitkeep").write_text("", encoding="utf-8")

    # Agent outbox directories
    for agent in agents:
        outbox_dir = sync_path / "outbox" / agent
        outbox_dir.mkdir(parents=True, exist_ok=True)
        (outbox_dir / ".gitkeep").write_text("", encoding="utf-8")

    # Work order subdirectories
    for wo_dir in ["ACTIVE", "COMPLETED", "BLOCKED", "TEMPLATES"]:
        dir_path = sync_path / "work-orders" / wo_dir
        dir_path.mkdir(parents=True, exist_ok=True)
        gitkeep = dir_path / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")


def ensure_workspace_protocol_scaffolding(workspace: Path | str) -> None:
    """Ensure baseline .sync/ directories and required protocol governance files exist.

    Idempotent and safe to invoke on any active workspace. Creates missing directories
    and populates PROTOCOL_DIGEST.md, PROTOCOL_DIGEST.hash, and RUNTIME_VERSION if missing.
    Also repairs schema-invalid work-order status 'FAILED' to 'BLOCKED'.
    """
    try:
        ws_path = Path(workspace).resolve()
        sync_path = ws_path / ".sync"
        sync_path.mkdir(parents=True, exist_ok=True)

        templates_dir = get_templates_dir()
        templates_sync_dir = templates_dir / "sync"

        # 1. PROTOCOL_DIGEST.md & .hash
        digest_path = sync_path / "PROTOCOL_DIGEST.md"
        if not digest_path.exists():
            src_digest = templates_sync_dir / "PROTOCOL_DIGEST.md"
            if src_digest.exists():
                shutil.copy2(src_digest, digest_path)
            else:
                digest_path.write_text("# StackMind Protocol Digest\n\nProtocol rules active.\n", encoding="utf-8")

        hash_path = sync_path / "PROTOCOL_DIGEST.hash"
        if not hash_path.exists() or hash_path.stat().st_size == 0:
            hash_val = compute_protocol_hash(digest_path)
            hash_path.write_text(f"{hash_val}\n", encoding="utf-8", newline="\n")

        # 2. RUNTIME_VERSION
        version_path = sync_path / "RUNTIME_VERSION"
        needs_version_write = False
        if not version_path.exists() or version_path.stat().st_size == 0:
            needs_version_write = True
        else:
            try:
                import yaml
                v_data = yaml.safe_load(version_path.read_text(encoding="utf-8"))
                if not isinstance(v_data, dict):
                    needs_version_write = True
            except Exception:
                needs_version_write = True

        if needs_version_write:
            src_template = templates_sync_dir / "RUNTIME_VERSION.template"
            now_iso = datetime.now(timezone.utc).isoformat()
            if src_template.exists():
                tmpl_text = src_template.read_text(encoding="utf-8")
                content = tmpl_text.replace("{{INIT_TIMESTAMP}}", now_iso).replace("{{RUNTIME_VERSION}}", RUNTIME_VERSION)
                version_path.write_text(content, encoding="utf-8")
            else:
                default_content = (
                    f'version: "{RUNTIME_VERSION}"\n\n'
                    'schema_versions:\n'
                    '  tree: 1\n'
                    '  boot: 1\n'
                    '  work_order: 1\n'
                    '  index: 1\n\n'
                    f'min_cli_version: "{RUNTIME_VERSION}"\n\n'
                    f'created_at: "{now_iso}"\n'
                    'created_by: "stackmind-init"\n\n'
                    'upgraded_at: null\n'
                    'upgraded_from: null\n'
                    'upgraded_by: null\n\n'
                    'migration_history: []\n'
                )
                version_path.write_text(default_content, encoding="utf-8")

        # 3. Create all baseline directories with .gitkeep
        _create_empty_dirs(sync_path, DEFAULT_AGENTS)

        # 4. Repair schema-invalid FAILED status on work orders
        import yaml
        wos_dir = sync_path / "work-orders"
        if wos_dir.is_dir():
            for sub in ("ACTIVE", "BLOCKED"):
                sub_dir = wos_dir / sub
                if sub_dir.is_dir():
                    for f in sub_dir.glob("*.yaml"):
                        try:
                            data = yaml.safe_load(f.read_text(encoding="utf-8"))
                            if isinstance(data, dict) and str(data.get("status", "")).upper() == "FAILED":
                                data["status"] = "BLOCKED"
                                f.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
                        except Exception:
                            pass
            idx_file = wos_dir / "INDEX.yaml"
            idx_data = None
            if idx_file.is_file() and idx_file.stat().st_size > 0:
                try:
                    loaded = yaml.safe_load(idx_file.read_text(encoding="utf-8"))
                    if isinstance(loaded, dict) and isinstance(loaded.get("orders"), list):
                        idx_data = loaded
                except Exception:
                    idx_data = None
            if idx_data is None:
                from datetime import datetime, timezone
                now_iso = datetime.now(timezone.utc).isoformat()
                idx_data = {
                    "schema_version": 1,
                    "last_updated": now_iso,
                    "updated_by": "stackmind-init",
                    "orders": [],
                    "next_id": 1,
                    "total_active": 0,
                    "total_completed": 0,
                    "total_blocked": 0,
                }
                tmp_idx = idx_file.with_name(f"{idx_file.name}.tmp.{int(time.time() * 1000)}")
                tmp_idx.write_text(yaml.safe_dump(idx_data, sort_keys=False), encoding="utf-8")
                tmp_idx.replace(idx_file)
            else:
                changed = False
                for o in idx_data["orders"]:
                    if isinstance(o, dict) and str(o.get("status", "")).upper() == "FAILED":
                        o["status"] = "BLOCKED"
                        changed = True
                if changed:
                    tmp_idx = idx_file.with_name(f"{idx_file.name}.tmp.{int(time.time() * 1000)}")
                    tmp_idx.write_text(yaml.safe_dump(idx_data, sort_keys=False), encoding="utf-8")
                    tmp_idx.replace(idx_file)

        # 5. Mirror bundled schemas into .sync/schemas/ directory (not workspace root)
        sync_schemas_dir = sync_path / "schemas"
        sync_schemas_dir.mkdir(parents=True, exist_ok=True)
        try:
            bundled_schemas = Path(__file__).resolve().parents[1] / "schemas"
            if bundled_schemas.is_dir():
                for s_file in bundled_schemas.glob("*.json"):
                    dest_file = sync_schemas_dir / s_file.name
                    if not dest_file.exists():
                        shutil.copy2(s_file, dest_file)
        except Exception:
            pass
    except Exception:
        pass


STACKMIND_ROOT_FILES = (
    ".gitignore",
    "README.md",
    "PLAN.md",
    "CHANGELOG.md",
    "VERSION",
)


def init_git(path: Path, initial_commit: bool = True) -> bool:
    """Initialize a git repository at the given path.

    Args:
        path: Directory to initialize as a git repo.
        initial_commit: If True, create an initial commit of the StackMind
            files only, and only when the repository has no commits yet.
            Existing user files and history are never staged or rewritten.

    Returns:
        True if git init succeeded, False otherwise.
    """
    try:
        subprocess.run(
            ["git", "init"],
            cwd=str(path),
            capture_output=True,
            check=True,
            text=True,
        )

        if initial_commit:
            # Only the very first commit is created here; repositories with
            # existing history are left untouched.
            has_commits = subprocess.run(
                ["git", "rev-parse", "--verify", "HEAD"],
                cwd=str(path),
                capture_output=True,
                check=False,
                text=True,
            ).returncode == 0
            if has_commits:
                return True
            if path.name != ".sync":
                # Stage only the StackMind-rendered root files so pre-existing
                # user files are never swept into the initialization commit.
                # (.sync/ is ignored via .gitignore; the .sync repo is separate.)
                # Clear any pre-existing staged changes so only stackmind files are committed.
                subprocess.run(
                    ["git", "reset"],
                    cwd=str(path),
                    capture_output=True,
                    check=False,
                    text=True,
                )
                existing = [f for f in STACKMIND_ROOT_FILES if (path / f).is_file()]
                if not existing:
                    return True
                subprocess.run(
                    ["git", "add", "--", *existing],
                    cwd=str(path),
                    capture_output=True,
                    check=True,
                    text=True,
                )
            else:
                # Clear any pre-existing staged changes so only stackmind files are committed.
                subprocess.run(["git", "reset"], cwd=str(path), capture_output=True, check=False, text=True)
                subprocess.run(
                    ["git", "add", "."],
                    cwd=str(path),
                    capture_output=True,
                    check=True,
                    text=True,
                )
            subprocess.run(
                ["git", "commit", "-m", "Initial stackmind runtime"],
                cwd=str(path),
                capture_output=True,
                check=True,
                text=True,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": "stackmind-init",
                    "GIT_AUTHOR_EMAIL": "init@stackmind.dev",
                    "GIT_COMMITTER_NAME": "stackmind-init",
                    "GIT_COMMITTER_EMAIL": "init@stackmind.dev",
                },
            )

        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def validate_result(project_path: Path, agents: list[str]) -> list[str]:
    """Validate the generated runtime structure.

    Checks that all required files and directories exist.

    Args:
        project_path: Root of the initialized project.
        agents: List of agent names that should exist.

    Returns:
        List of validation error messages. Empty = valid.
    """
    errors: list[str] = []
    sync_path = project_path / ".sync"

    # Required .sync files
    required_sync_files = [
        "README.md",
        "PROTOCOL_DIGEST.md",
        "PROTOCOL_DIGEST.hash",
        "SYSTEM_CONTEXT.md",
        "RUNTIME_VERSION",
        "runtime/TREE.yaml",
        "work-orders/INDEX.yaml",
    ]

    for f in required_sync_files:
        if not (sync_path / f).exists():
            errors.append(f"Missing: .sync/{f}")

    # Required agent files
    for agent in agents:
        boot_file = sync_path / "runtime" / "boot" / f"{agent}.boot.yaml"
        if not boot_file.exists():
            errors.append(f"Missing: .sync/runtime/boot/{agent}.boot.yaml")

        agent_contract = sync_path / "agents" / f"{agent}.agent.md"
        if not agent_contract.exists():
            errors.append(f"Missing: .sync/agents/{agent}.agent.md")

        inbox_dir = sync_path / "inbox" / agent
        if not inbox_dir.exists():
            errors.append(f"Missing: .sync/inbox/{agent}/")

        read_dir = inbox_dir / "_read"
        if not read_dir.exists():
            errors.append(f"Missing: .sync/inbox/{agent}/_read/")

        outbox_dir = sync_path / "outbox" / agent
        if not outbox_dir.exists():
            errors.append(f"Missing: .sync/outbox/{agent}/")

    # Required empty directories
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
        "inbox/CEO/_read",
    ]

    for d in required_dirs:
        if not (sync_path / d).is_dir():
            errors.append(f"Missing directory: .sync/{d}")

    # Validate PROTOCOL_DIGEST.hash matches PROTOCOL_DIGEST.md
    hash_file = sync_path / "PROTOCOL_DIGEST.hash"
    digest_file = sync_path / "PROTOCOL_DIGEST.md"
    if hash_file.exists() and digest_file.exists():
        stored_hash = hash_file.read_text(encoding="utf-8").strip()
        computed_hash = compute_protocol_hash(digest_file)
        if stored_hash != computed_hash:
            errors.append(
                f"PROTOCOL_DIGEST.hash mismatch: stored={stored_hash[:16]}... "
                f"computed={computed_hash[:16]}..."
            )

    return errors


def _detect_os() -> tuple[str, str]:
    """Detect the current OS and shell type."""
    import platform

    system = platform.system()
    if system == "Windows":
        return "Windows 10/11", "PowerShell"
    if system == "Darwin":
        return "macOS", "Bash/Zsh"
    return "Linux", "Bash"


def init(
    project_path: Path,
    name: Optional[str] = None,
    agents: Optional[list[str]] = None,
    no_git: bool = False,
    preserve_existing: bool = False,
) -> bool:
    """Initialize a new stackmind runtime at the given path.

    This is the main entry point for `stackmind init`.

    Args:
        project_path: Path where the project will be created.
        name: Project name. Defaults to directory name.
        agents: List of agent names. Defaults to all 5 agents.
        no_git: If True, skip git initialization.
        preserve_existing: If True, create only missing project-root files and
            merge StackMind ignore entries into an existing .gitignore. This is
            intended for adoption by ``stackmind tui``; .sync must still be absent.

    Returns:
        True if initialization succeeded, False otherwise.

    Raises:
        RuntimeError: If .sync/ already exists at project_path.
        FileNotFoundError: If templates directory is missing.
    """
    project_path = project_path.resolve()
    sync_path = project_path / ".sync"
    templates_dir = get_templates_dir()

    # Resolve defaults
    if name is None:
        name = project_path.name
    if agents is None:
        agents = DEFAULT_AGENTS.copy()

    # ── Step 1: Validate ──────────────────────────────────────
    console.print(f"\n[bold cyan][*] Initializing stackmind runtime...[/bold cyan]")
    console.print(f"   Path: [dim]{project_path}[/dim]")
    console.print(f"   Name: [dim]{name}[/dim]")
    console.print(f"   Agents: [dim]{', '.join(agents)}[/dim]\n")

    if sync_path.exists():
        console.print(
            "[bold red][x][/bold red] Runtime already exists at "
            f"[dim]{sync_path}[/dim]"
        )
        console.print(
            "  Use [bold]stackmind validate[/bold] to check existing runtime."
        )
        raise RuntimeError(f"Runtime already exists at {sync_path}")

    if not templates_dir.exists():
        raise FileNotFoundError(
            f"Templates directory not found: {templates_dir}. "
            "Is stackmind installed correctly?"
        )

    # ── Step 2: Create project root ───────────────────────────
    project_path.mkdir(parents=True, exist_ok=True)
    console.print("[bold green][+][/bold green] Created project directory")

    # ── Step 3: Build template context ────────────────────────
    templates_sync_dir = templates_dir / "sync"
    protocol_digest_path = templates_sync_dir / "PROTOCOL_DIGEST.md"
    protocol_hash = compute_protocol_hash(protocol_digest_path)

    os_type, shell_type = _detect_os()
    now = datetime.now(timezone.utc).astimezone()
    init_timestamp = now.isoformat()

    import sys
    stackmind_bin = shutil.which("stackmind")
    if not stackmind_bin:
        stackmind_bin = f"{sys.executable} -m cli.main"

    context: dict[str, str] = {
        "PROJECT_NAME": name,
        "WORKSPACE_ROOT": str(project_path).replace("\\", "/"),
        "SYNC_ROOT": str(sync_path).replace("\\", "/"),
        "INIT_TIMESTAMP": init_timestamp,
        "PROTOCOL_HASH": protocol_hash,
        "RUNTIME_VERSION": RUNTIME_VERSION,
        "OS_TYPE": os_type,
        "SHELL_TYPE": shell_type,
        "STACKMIND_BIN_PATH": stackmind_bin.replace("\\", "/"),
    }

    # ── Step 4: Render product-facing documentation ─
    # AGENTS.md is internal runtime metadata, stays in .sync/ only
    readme_template = templates_dir / "README.template.md"
    readme_path = project_path / "README.md"
    if readme_template.exists() and (not preserve_existing or not readme_path.exists()):
        render_template_file(
            readme_template,
            readme_path,
            context,
        )

    plan_template = templates_dir / "PLAN.template.md"
    if plan_template.exists() and (
        not preserve_existing or not (project_path / "PLAN.md").exists()
    ):
        render_template_file(plan_template, project_path / "PLAN.md", context)

    changelog_template = templates_dir / "CHANGELOG.template.md"
    if changelog_template.exists() and (
        not preserve_existing or not (project_path / "CHANGELOG.md").exists()
    ):
        render_template_file(changelog_template, project_path / "CHANGELOG.md", context)

    version_template = templates_dir / "VERSION.template"
    if version_template.exists() and (
        not preserve_existing or not (project_path / "VERSION").exists()
    ):
        render_template_file(version_template, project_path / "VERSION", context)

    gitignore_template = templates_dir / ".gitignore.template"
    gitignore_path = project_path / ".gitignore"
    if gitignore_template.exists() and (not preserve_existing or not gitignore_path.exists()):
        render_template_file(gitignore_template, gitignore_path, context)
    elif preserve_existing and gitignore_path.exists():
        _merge_stackmind_gitignore(gitignore_path)
    elif not gitignore_path.exists():
        gitignore_path.write_text(".sync/\ngraphify-out/\n", encoding="utf-8", newline="\n")
    else:
        current_gi = gitignore_path.read_text(encoding="utf-8")
        if ".sync/" not in current_gi:
            gitignore_path.write_text(current_gi.rstrip() + "\n\n# Stackmind runtime\n.sync/\ngraphify-out/\n", encoding="utf-8", newline="\n")

    console.print("[bold green][+][/bold green] Rendered PLAN.md, CHANGELOG.md, VERSION, .gitignore")

    # ── Step 5: Create .sync/ structure ───────────────────────
    sync_path.mkdir(parents=True, exist_ok=True)
    create_sync_structure(templates_sync_dir, sync_path, context, agents)

    # Mirror bundled schemas into .sync/schemas/ (not polluting workspace root)
    sync_schemas_dir = sync_path / "schemas"
    sync_schemas_dir.mkdir(parents=True, exist_ok=True)
    try:
        bundled_schemas = Path(__file__).resolve().parents[1] / "schemas"
        if bundled_schemas.is_dir():
            for s_file in bundled_schemas.glob("*.json"):
                dest_file = sync_schemas_dir / s_file.name
                if not dest_file.exists():
                    shutil.copy2(s_file, dest_file)
    except Exception:
        pass

    console.print("[bold green][+][/bold green] Created .sync/ directory structure")

    # ── Step 6: Generate PROTOCOL_DIGEST.hash ─────────────────
    hash_file = sync_path / "PROTOCOL_DIGEST.hash"
    hash_file.write_text(protocol_hash, encoding="utf-8", newline="\n")
    console.print("[bold green][+][/bold green] Generated PROTOCOL_DIGEST.hash")

    # ── Step 7: Initialize git repos ──────────────────────────
    if not no_git:
        project_git_ok = init_git(project_path)
        sync_git_ok = init_git(sync_path)

        if project_git_ok and sync_git_ok:
            console.print("[bold green][+][/bold green] Initialized git repositories")
        elif project_git_ok:
            console.print(
                "[bold yellow][!][/bold yellow] Project git OK, "
                ".sync git failed (git may not be installed)"
            )
        else:
            console.print(
                "[bold yellow][!][/bold yellow] Git initialization skipped "
                "(git may not be installed)"
            )
    else:
        console.print("[dim][-] Skipped git initialization (--no-git)[/dim]")

    # ── Step 8: Validate result ───────────────────────────────
    errors = validate_result(project_path, agents)

    if errors:
        console.print(f"\n[bold red][x] Validation failed ({len(errors)} errors):[/bold red]")
        for err in errors:
            console.print(f"  [red]• {err}[/red]")
        return False

    console.print("[bold green][+][/bold green] Validation passed")

    # ── Success ───────────────────────────────────────────────
    console.print(
        Panel(
            f"[bold green][SUCCESS] stackmind runtime initialized![/bold green]\n\n"
            f"  Project: [bold]{name}[/bold]\n"
            f"  Path:    [dim]{project_path}[/dim]\n"
            f"  Runtime: [dim]{sync_path}[/dim]\n"
            f"  Version: [dim]v{RUNTIME_VERSION}[/dim]\n\n"
            f"[dim]Next steps:[/dim]\n"
            f"  1. Customize agent contracts in [bold].sync/agents/[/bold]\n"
            f"  2. Run [bold]stackmind validate {project_path}[/bold] to verify",
            title="[bold] stackmind [/bold]",
            border_style="green",
        )
    )
    # Register project in centralized ~/.stackmindcli/projects.json and seed initial PLAN.v1.md
    try:
        from validators.kernel.home import register_project, save_authoritative_plan
        register_project(project_path, name=name, agents=agents)
        plan_f = project_path / "PLAN.md"
        if plan_f.is_file():
            save_authoritative_plan(project_path, plan_f.read_text(encoding="utf-8"), version=1)
    except Exception:
        pass

    return True


def _append_stackmind_readme_section(path: Path) -> None:
    """Add a small managed adoption note once, preserving existing README text."""
    marker = "<!-- stackmind-runtime:start -->"
    content, encoding = _read_preserving_text(path)
    if marker in content:
        return
    section = (
        "## StackMind Runtime\n\n"
        "This project uses StackMind for its governed agent runtime. Run "
        "`stackmind tui` to validate the runtime, refresh the project knowledge "
        "graph when needed, and start the terminal interface.\n"
    )
    newline = "\r\n" if "\r\n" in content else "\n"
    section = section.replace("\n", newline)
    separator = "" if not content or content.endswith(("\n", "\r")) else newline * 2
    block = marker + newline + section + "<!-- stackmind-runtime:end -->" + newline
    path.write_text(content + separator + block, encoding=encoding, newline="")


def _merge_stackmind_gitignore(path: Path) -> None:
    """Append only missing runtime ignore entries without removing user patterns."""
    content, encoding = _read_preserving_text(path)
    lines = content.splitlines()
    additions = [entry for entry in (".sync/", "graphify-out/") if entry not in lines]
    if not additions:
        return
    newline = "\r\n" if "\r\n" in content else "\n"
    separator = "" if not content or content.endswith(("\n", "\r")) else newline * 2
    merged = content + separator + newline.join(additions) + newline
    path.write_text(merged, encoding=encoding, newline="")


def _read_preserving_text(path: Path) -> tuple[str, str]:
    """Read common Unicode encodings while retaining BOM/encoding on write."""
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    elif raw.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    else:
        encoding = "utf-8"
    return raw.decode(encoding), encoding
