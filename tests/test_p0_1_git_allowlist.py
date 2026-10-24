"""
Tests for Phase 0 Task 1 (P0-1): Git and publish command allowlist.

Acceptance criteria:
- Non-GitOps roles (Backend, Frontend, QA, Architecture) are blocked from:
  - git mutation commands (commit, push, tag, checkout, branch, etc.)
  - dangerous git flags (-c, -C, --git-dir, --work-tree, --exec-path, --output, --ext-diff, --textconv)
  - execution wrappers (env, command, xargs, find -exec, etc.)
  - publishing tools (gh, hub, npm version|publish|pack, yarn publish, pnpm publish)
  - arbitrary script runners (python -m)
- Read-only git commands (status, diff, log, show, blame) with allowlisted flags are permitted for non-GitOps.
- GitOps roles (local-llm, gitops) retain full access to git commands.
"""

import pytest
from validators.kernel.interpreter_denylist import check_command

NON_GITOPS_ROLES = ["backend", "frontend", "qa", "architecture", "codex", "gemini", "gemma", "claude"]
GITOPS_ROLES = ["gitops", "local-llm", "release"]


@pytest.mark.parametrize("role", NON_GITOPS_ROLES)
@pytest.mark.parametrize("cmd", [
    ["git", "commit", "-m", "bypassed"],
    ["git", "push", "origin", "main"],
    ["git", "tag", "v1.0.0"],
    ["git", "checkout", "-b", "new-branch"],
    ["git", "branch", "-D", "main"],
    ["git", "-c", "alias.x=!calc", "x"],
    ["git", "--git-dir=.git", "commit"],
    ["git", "--exec-path=/tmp", "commit"],
    ["env", "git", "commit"],
    ["command", "git", "commit"],
    ["xargs", "git", "commit"],
    ["find", ".", "-exec", "git", "commit", "{}", ";"],
    ["gh", "pr", "create"],
    ["hub", "pull-request"],
    ["npm", "version", "patch"],
    ["npm", "publish"],
    ["npm", "pack"],
    ["yarn", "publish"],
    ["pnpm", "publish"],
    ["python", "-m", "http.server"],
    ["git", "diff", "--output=malicious.txt"],
    ["git", "diff", "--ext-diff"],
    ["git", "diff", "--textconv"],
    ["git", "log", "--output=leak.txt"],
    ["git", "-c", "core.editor=calc", "status"],
    ["git", "-C", "sub", "status"],
    ["git", "--exec-path=bin", "status"],
])
def test_non_gitops_denials(role, cmd):
    reason = check_command(cmd, role=role)
    assert reason is not None, f"Expected {cmd} to be DENIED for role '{role}', but was ALLOWED"


@pytest.mark.parametrize("role", NON_GITOPS_ROLES)
@pytest.mark.parametrize("cmd", [
    ["git", "status"],
    ["git", "status", "--no-optional-locks"],
    ["git", "diff"],
    ["git", "diff", "--stat"],
    ["git", "diff", "--name-only"],
    ["git", "diff", "-p"],
    ["git", "log", "--oneline"],
    ["git", "log", "-n", "5"],
    ["git", "show", "HEAD"],
    ["git", "blame", "README.md"],
])
def test_non_gitops_read_only_allowed(role, cmd):
    reason = check_command(cmd, role=role)
    assert reason is None, f"Expected read-only {cmd} to be ALLOWED for role '{role}', but was denied: {reason}"


@pytest.mark.parametrize("role", GITOPS_ROLES)
@pytest.mark.parametrize("cmd", [
    ["git", "commit", "-m", "release 1.0.0"],
    ["git", "tag", "v1.0.0"],
    ["git", "push", "origin", "main"],
    ["git", "status"],
    ["git", "diff"],
    ["npm", "version", "patch"],
])
def test_gitops_allowed(role, cmd):
    reason = check_command(cmd, role=role)
    assert reason is None, f"Expected {cmd} to be ALLOWED for GitOps role '{role}', but was denied: {reason}"
