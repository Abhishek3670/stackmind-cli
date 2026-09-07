"""stackmind init — Initialize a new stackmind runtime.

Handles:
- Template rendering ({{PLACEHOLDER}} substitution)
- Directory structure creation from templates/sync/
- AGENTS.md rendering to project root
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
    for wo_dir in ["ACTIVE", "COMPLETED", "BLOCKED"]:
        dir_path = sync_path / "work-orders" / wo_dir
        dir_path.mkdir(parents=True, exist_ok=True)
        gitkeep = dir_path / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")


def init_git(path: Path, initial_commit: bool = True) -> bool:
    """Initialize a git repository at the given path.

    Args:
        path: Directory to initialize as a git repo.
        initial_commit: If True, create an initial commit.

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
            subprocess.run(
                ["git", "add", "."],
                cwd=str(path),
                capture_output=True,
                check=True,
                text=True,
            )
            # Ensure .sync is never staged into the outer repository
            if (path / ".sync").exists() and path.name != ".sync":
                subprocess.run(
                    ["git", "reset", ".sync"],
                    cwd=str(path),
                    capture_output=True,
                    check=False,
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

    # Required root files
    if not (project_path / "AGENTS.md").exists():
        errors.append("Missing: AGENTS.md in project root")

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
) -> bool:
    """Initialize a new stackmind runtime at the given path.

    This is the main entry point for `stackmind init`.

    Args:
        project_path: Path where the project will be created.
        name: Project name. Defaults to directory name.
        agents: List of agent names. Defaults to all 5 agents.
        no_git: If True, skip git initialization.

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

    # ── Step 4: Render AGENTS.md and README.md to project root ─
    agents_template = templates_dir / "AGENTS.template.md"
    render_template_file(
        agents_template,
        project_path / "AGENTS.md",
        context,
    )

    readme_template = templates_dir / "README.template.md"
    if readme_template.exists():
        render_template_file(
            readme_template,
            project_path / "README.md",
            context,
        )
        
    plan_template = templates_dir / "PLAN.template.md"
    if plan_template.exists():
        render_template_file(plan_template, project_path / "PLAN.md", context)
        
    changelog_template = templates_dir / "CHANGELOG.template.md"
    if changelog_template.exists():
        render_template_file(changelog_template, project_path / "CHANGELOG.md", context)
        
    version_template = templates_dir / "VERSION.template"
    if version_template.exists():
        render_template_file(version_template, project_path / "VERSION", context)

    gitignore_template = templates_dir / ".gitignore.template"
    gitignore_path = project_path / ".gitignore"
    if gitignore_template.exists():
        render_template_file(gitignore_template, gitignore_path, context)
    elif not gitignore_path.exists():
        gitignore_path.write_text(".sync/\ngraphify-out/\n", encoding="utf-8", newline="\n")
    else:
        current_gi = gitignore_path.read_text(encoding="utf-8")
        if ".sync/" not in current_gi:
            gitignore_path.write_text(current_gi.rstrip() + "\n\n# Stackmind runtime\n.sync/\ngraphify-out/\n", encoding="utf-8", newline="\n")

    console.print("[bold green][+][/bold green] Rendered AGENTS.md, PLAN.md, CHANGELOG.md, VERSION, .gitignore")

    # ── Step 5: Create .sync/ structure ───────────────────────
    sync_path.mkdir(parents=True, exist_ok=True)
    create_sync_structure(templates_sync_dir, sync_path, context, agents)
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
            f"  1. Review [bold]AGENTS.md[/bold] in project root\n"
            f"  2. Customize agent contracts in [bold].sync/agents/[/bold]\n"
            f"  3. Run [bold]stackmind validate {project_path}[/bold] to verify",
            title="[bold] stackmind [/bold]",
            border_style="green",
        )
    )

    return True
