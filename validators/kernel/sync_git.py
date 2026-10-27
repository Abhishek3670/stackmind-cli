"""Automated Git Commit Manager for the .sync Governance Repository.

Under StackMind multi-agent architecture, the .sync directory is a dedicated,
isolated Git repository tracking immutable governance artifacts (work orders,
contracts, outbox reports, experiences, decisions, and verification records).

Individual specialist agents (Claude, Gemini, Codex, Gemma) do not run git
commands directly; the StackMind runtime and supervisor auto-commit verified
state transitions and agent receipts into the .sync git repo.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence


def commit_sync_governance(
    project_path: Path | str,
    message: str,
    *,
    author: str = "supervisor",
    paths: Sequence[str | Path] | None = None,
) -> str | None:
    """Stage and commit governance records to the .sync git repository.

    Returns the commit SHA if changes were committed, or None if clean/no-op.
    """
    if not project_path:
        return None

    base_path = Path(project_path).resolve()
    sync_path = base_path / ".sync" if base_path.name != ".sync" else base_path
    git_dir = sync_path / ".git"

    if not git_dir.exists():
        return None

    try:
        # 1. Ensure .sync/.gitignore ignores ephemeral daemon runtime pids
        sync_gitignore = sync_path / ".gitignore"
        if not sync_gitignore.exists():
            try:
                sync_gitignore.write_text(
                    "# Ephemeral daemon runtime files\nruntime/daemon/*.pid\nruntime/daemon/*.tmp\n",
                    encoding="utf-8",
                )
            except Exception:
                pass

        # 2. Stage changes (specific paths or all)
        if paths:
            rel_paths: list[str] = []
            for p in paths:
                try:
                    p_path = Path(p)
                    if p_path.is_absolute():
                        rel_paths.append(str(p_path.relative_to(sync_path)))
                    else:
                        rel_paths.append(str(p))
                except Exception:
                    rel_paths.append(str(p))
            subprocess.run(
                ["git", "add", "--", *rel_paths],
                cwd=str(sync_path),
                check=False,
                capture_output=True,
            )
        else:
            subprocess.run(
                ["git", "add", "."],
                cwd=str(sync_path),
                check=False,
                capture_output=True,
            )

        # 3. Check if anything is staged
        status_proc = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(sync_path),
            check=False,
            capture_output=True,
        )
        if status_proc.returncode == 0:
            # Working tree in .sync is clean relative to index
            return None

        # 4. Commit with governance provenance attribution
        author_clean = str(author).strip().lower() or "supervisor"
        env = dict(os.environ)
        env["GIT_AUTHOR_NAME"] = f"StackMind [{author_clean}]"
        env["GIT_AUTHOR_EMAIL"] = f"{author_clean}@stackmind.dev"
        env["GIT_COMMITTER_NAME"] = "StackMind Governance"
        env["GIT_COMMITTER_EMAIL"] = "governance@stackmind.dev"

        commit_proc = subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(sync_path),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if commit_proc.returncode != 0:
            return None

        # 5. Rev-parse HEAD
        sha_proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(sync_path),
            check=False,
            capture_output=True,
            text=True,
        )
        return sha_proc.stdout.strip() if sha_proc.returncode == 0 else None
    except Exception:
        return None


__all__ = ["commit_sync_governance"]
