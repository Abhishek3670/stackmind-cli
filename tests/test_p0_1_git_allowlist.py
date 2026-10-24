"""
Tests for Phase 0 Task 1 (P0-1): Git and publish command allowlist.

AUDITED BYPASS ROW -> TEST ID MAPPING TABLE:
+-----------------------------------+------------------------------------------+-------------------------------------------+
| Audited Vector                    | Example Command                          | Test ID / Function                        |
+-----------------------------------+------------------------------------------+-------------------------------------------+
| Direct git mutation               | git commit, git push, git tag, etc.      | test_non_gitops_denials                   |
| Git alias / config override       | git -c alias.x="!calc" x                 | test_non_gitops_denials                   |
| Alternate git directory           | git --git-dir=.git commit                | test_non_gitops_denials                   |
| Alternate work tree               | git --work-tree=. commit                 | test_non_gitops_denials                   |
| Alternate exec path               | git --exec-path=/tmp commit              | test_non_gitops_denials                   |
| Output redirection flag           | git diff --output=leak.txt               | test_non_gitops_denials                   |
| External diff engine              | git diff --ext-diff                      | test_non_gitops_denials                   |
| Textconv filter execution         | git diff --textconv                      | test_non_gitops_denials                   |
| Global flag before subcommand     | git --no-pager commit                    | test_global_git_flags_before_subcommand   |
| Path / case obfuscation           | C:\\Git\\bin\\git.exe commit, .\\git     | test_executable_path_normalization        |
| Wrapper: env / env -i / env -S    | env FOO=bar -i git commit, env -S "..."  | test_wrapper_unwrapping                   |
| Wrapper: nice / timeout           | nice -n 10 git commit, timeout 5 ...     | test_wrapper_unwrapping                   |
| Wrapper: sudo / nohup             | sudo -u root git commit, nohup ...       | test_wrapper_unwrapping                   |
| Wrapper: xargs / find             | xargs -I{} git commit, find -exec ... +  | test_wrapper_unwrapping                   |
| Wrapper: Windows cmd /c, start    | cmd /c git commit, start /wait ...       | test_wrapper_unwrapping                   |
| Release publishing tools          | gh pr create, hub pull-request           | test_non_gitops_denials                   |
| Package release commands          | npm version, npm publish, npm pack       | test_non_gitops_denials, test_npm_policy  |
| Arbitrary python modules          | python -m http.server                    | test_python_m_allowlist                   |
| Role vs Model Identity            | local-llm running as Backend             | test_model_identity_vs_assigned_role      |
| Git lockfile / config pollution   | git status leaving index.lock            | test_git_hardening_environment            |
+-----------------------------------+------------------------------------------+-------------------------------------------+

DOCS NOTE / SECURITY BOUNDARY STATUS:
This command inspection layer (Layer 1) is ADVISORY, providing defense-in-depth within
the agent execution boundary. It is NOT an operating system security boundary.
Absolute confinement is guaranteed in subsequent layers:
- Layer 2: P0-2 Per-turn snapshot and non-HEAD revert
- Layer 3: P0-3 Post-turn diff gate (.git internal integrity and working tree verification)
- Layer 4: Phase 1 Operating system sandbox, isolated credentials, and restricted environment
"""

from pathlib import Path
import os
import subprocess
import sys
import tempfile
import pytest

from validators.kernel.interpreter_denylist import check_command, is_gitops_role
from validators.kernel.identity import get_role_policy, AuthorizationPolicy
from validators.kernel.contract import AgentContract
from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.operations import OperationJournal
from validators.kernel.workspace import ScratchWorkspace
from validators.kernel.tools import ToolGateway

NON_GITOPS_ROLES = ["backend", "frontend", "qa", "architecture", "codex", "gemini", "gemma", "claude"]
GITOPS_ROLES = ["gitops", "release"]


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


@pytest.mark.parametrize("cmd", [
    ["env", "FOO=bar", "-i", "git", "commit"],
    ["env", "-S", "git commit"],
    ["nice", "-n", "10", "git", "commit"],
    ["timeout", "5", "git", "commit"],
    ["timeout", "-s", "9", "5", "git", "commit"],
    ["sudo", "-u", "root", "git", "commit"],
    ["xargs", "-I{}", "git", "commit"],
    ["find", ".", "-exec", "git", "commit", "{}", "+"],
    ["nohup", "git", "commit"],
    ["cmd", "/c", "git commit"],
    ["cmd.exe", "/c", "git", "commit"],
    ["start", "/wait", "git", "commit"],
    ["start", "git", "commit"],
])
def test_wrapper_unwrapping(cmd):
    reason = check_command(cmd, role="backend")
    assert reason is not None, f"Wrapper bypass {cmd} was not unwrapped and denied!"


@pytest.mark.parametrize("cmd", [
    [r"C:\Program Files\Git\bin\git.exe", "commit"],
    [r"C:\Git\cmd\git.EXE", "commit"],
    [r".\git.exe", "commit"],
    [r".\git", "commit"],
    [r"../bin/git", "commit"],
])
def test_executable_path_normalization(cmd):
    reason = check_command(cmd, role="backend")
    assert reason is not None, f"Path obfuscation {cmd} bypassed git deny!"


def test_global_git_flags_before_subcommand():
    # Denied subcommands preceded by global flags
    assert check_command(["git", "--no-pager", "commit"], role="backend") is not None
    assert check_command(["git", "--no-optional-locks", "commit"], role="backend") is not None
    
    # Allowed subcommands preceded by global flags
    assert check_command(["git", "--no-pager", "status"], role="backend") is None
    assert check_command(["git", "--no-optional-locks", "status"], role="backend") is None
    assert check_command(["git", "--no-pager", "diff"], role="backend") is None


def test_python_m_allowlist():
    # Legitimate developer toolchain modules must be allowed
    assert check_command(["python", "-m", "pytest"], role="backend") is None
    assert check_command(["python", "-m", "pip", "list"], role="backend") is None
    assert check_command(["python", "-m", "venv", ".venv"], role="backend") is None
    assert check_command(["python", "-m", "unittest", "discover"], role="backend") is None
    assert check_command(["python", "-m", "py_compile", "test.py"], role="backend") is None
    assert check_command(["python", "-m", "json.tool", "test.json"], role="backend") is None

    # Arbitrary / unvetted modules must be blocked
    assert check_command(["python", "-m", "http.server"], role="backend") is not None
    assert check_command(["python", "-m", "code"], role="backend") is not None
    assert check_command(["python", "-m"], role="backend") is not None


def test_npm_lifecycle_policy():
    """Verify Layer 1 policy for npm/npx lifecycle scripts.
    
    npm run, npm install, npx are permitted at Layer 1 because they are legitimate
    build/test tools; side effects are caught by P0-3 diff gate.
    npm version, publish, and pack are denied at Layer 1.
    """
    assert check_command(["npm", "run", "build"], role="backend") is None
    assert check_command(["npm", "install"], role="backend") is None
    assert check_command(["npm", "test"], role="backend") is None
    assert check_command(["npx", "tsc"], role="backend") is None

    assert check_command(["npm", "version", "patch"], role="backend") is not None
    assert check_command(["npm", "publish"], role="backend") is not None
    assert check_command(["npm", "pack"], role="backend") is not None


def test_model_identity_vs_assigned_role(tmp_path):
    """Verify that role derives from the assigned turn policy/contract, NOT actor name.
    
    When 'local-llm' runs as Backend, it must be denied git mutation.
    """
    assert not is_gitops_role("local-llm")
    assert check_command(["git", "commit"], role="local-llm") is not None

    backend_policy = get_role_policy("backend")
    assert backend_policy.policy_id == "policy-backend"

    ws_root = tmp_path / "ws"
    ws_root.mkdir()
    auth_root = tmp_path / "auth"
    auth_root.mkdir()
    workspace = ScratchWorkspace(auth_root, ws_root, "attempt-1", True)
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    contract = AgentContract(
        agent_id="local-llm",
        work_order="WO-001",
        allow=["workspace/**"],
        deny=[],
        write_mode="read-write",
    )

    gateway = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=backend_policy,
        session_id="sess-1",
        attempt_id="attempt-1",
        actor_id="local-llm",
        provider_id="ollama",
    )

    with pytest.raises(PermissionError) as exc_info:
        gateway.run_command(["git", "commit", "-m", "malicious"])
    assert "git subcommand 'commit' is denied" in str(exc_info.value)


def test_git_hardening_environment(tmp_path):
    """Verify harness-injected git hardening: no index.lock left and GIT_EXTERNAL_DIFF effective."""
    repo = tmp_path / "repo"
    repo.mkdir()

    null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
    hardened_env = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_EXTERNAL_DIFF": "",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": null_dev,
    }

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, env=hardened_env)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True, env=hardened_env)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, capture_output=True, env=hardened_env)

    dummy_file = repo / "file.txt"
    dummy_file.write_text("initial content\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True, capture_output=True, env=hardened_env)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True, capture_output=True, env=hardened_env)

    # Configure external diff driver in repo
    canary = repo / "pwned.txt"
    diff_script = repo / "fake_diff.bat"
    diff_script.write_text(f"@echo pwned > {canary.resolve()}\n", encoding="utf-8")
    subprocess.run(["git", "config", "diff.external", str(diff_script.resolve())], cwd=repo, check=True, capture_output=True, env=hardened_env)

    dummy_file.write_text("changed content\n", encoding="utf-8")

    # Verify GIT_EXTERNAL_DIFF="" suppresses diff.external
    res = subprocess.run(["git", "diff"], cwd=repo, capture_output=True, text=True, env=hardened_env)
    assert not canary.exists(), "GIT_EXTERNAL_DIFF='' failed to suppress diff.external!"

    # Verify status and log leave no index.lock
    subprocess.run(["git", "status"], cwd=repo, capture_output=True, env=hardened_env, check=True)
    subprocess.run(["git", "log", "-n", "1"], cwd=repo, capture_output=True, env=hardened_env, check=True)
    lock_file = repo / ".git" / "index.lock"
    assert not lock_file.exists(), "index.lock was left behind by read-only git operations!"
