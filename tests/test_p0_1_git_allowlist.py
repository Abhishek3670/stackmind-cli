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
| Output redirection flag           | git diff --output=leak.txt               | test_non_gitops_denials, test_subcmd_flags|
| External diff engine              | git diff --ext-diff                      | test_non_gitops_denials, test_subcmd_flags|
| Textconv filter execution         | git diff --textconv                      | test_non_gitops_denials, test_subcmd_flags|
| Global flag before subcommand     | git --namespace status commit            | test_global_git_flag_strict_parsing       |
| Global flag before subcommand     | git --super-prefix foo commit            | test_global_git_flag_strict_parsing       |
| Valueless global flags allowed    | git --no-pager status                    | test_global_git_flag_strict_parsing       |
| Per-subcmd flags (-C / -c)        | git blame -C, git log -c, git show -c    | test_per_subcommand_flags_allowed         |
| Path / case obfuscation           | C:\\Git\\bin\\git.exe commit, .\\git     | test_executable_path_normalization        |
| Wrapper: env / env -i / env -S    | env FOO=bar -i git commit, env -S "..."  | test_wrapper_unwrapping                   |
| Wrapper: nice / timeout           | nice -n 10 git commit, timeout 5 ...     | test_wrapper_unwrapping                   |
| Wrapper: sudo / nohup             | sudo -u root git commit, nohup ...       | test_wrapper_unwrapping                   |
| Wrapper: xargs / find             | xargs -I{} git commit, find -exec ... +  | test_wrapper_unwrapping                   |
| Wrapper: Windows cmd /c, start    | cmd /c git commit, start /wait ...       | test_wrapper_unwrapping                   |
| Release publishing tools          | gh pr create, hub pull-request           | test_non_gitops_denials                   |
| Package release commands          | npm version, npm publish, npm pack       | test_non_gitops_denials, test_npm_policy  |
| Arbitrary python modules          | python -m http.server, python -mhttp...  | test_python_m_allowlist                   |
| Python versions & Windows launcher| python3.11 -m, py -3 -m, py -3.11 -m     | test_python_versions_and_py_launcher      |
| Node execution flags              | node -e, node -p, node -r, node --require| test_node_execution_flags                 |
| Role vs Model Identity            | local-llm running as Backend             | test_model_identity_vs_assigned_role      |
| Git diff external handling        | diff.external set; --no-ext-diff used    | test_git_diff_external_handling           |
+-----------------------------------+------------------------------------------+-------------------------------------------+

DOCS NOTE / SECURITY BOUNDARY STATUS:
This command inspection layer (Layer 1) is ADVISORY, providing defense-in-depth within
the agent execution boundary. It is NOT an operating system security boundary.
Absolute confinement is guaranteed in subsequent layers:
- Layer 2: P0-2 Per-turn snapshot and non-HEAD revert (supervisor-owned object store)
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
from validators.kernel.identity import get_role_policy
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


def test_global_git_flag_strict_parsing():
    """Verify strict parsing of flags before git subcommand.
    
    Only fixed valueless global flags (--no-pager, --no-optional-locks) are allowed before subcommand.
    All other dash tokens (like --namespace, --super-prefix, -C, -c) are strictly denied before subcommand.
    """
    # Potential bypass: --namespace with separate value token
    assert check_command(["git", "--namespace", "status", "commit", "-m", "x"], role="backend") is not None
    assert check_command(["git", "--super-prefix", "foo", "commit"], role="backend") is not None
    assert check_command(["git", "--attr-source", "foo", "commit"], role="backend") is not None
    assert check_command(["git", "-C", "sub", "status"], role="backend") is not None
    assert check_command(["git", "-c", "core.editor=calc", "status"], role="backend") is not None

    # Allowed valueless global flags before subcommand
    assert check_command(["git", "--no-pager", "status"], role="backend") is None
    assert check_command(["git", "--no-optional-locks", "status"], role="backend") is None
    assert check_command(["git", "--no-pager", "--no-optional-locks", "diff"], role="backend") is None

    # Global flags preceding a denied subcommand remain denied
    assert check_command(["git", "--no-pager", "commit"], role="backend") is not None
    assert check_command(["git", "--no-optional-locks", "commit"], role="backend") is not None


def test_per_subcommand_flags_allowed():
    """Verify that scoped subcommand flags work for read-only git operations.
    
    git blame -C, git log -c, git show -c must be permitted; dangerous flags stay denied.
    """
    # git blame -C must be allowed
    assert check_command(["git", "blame", "-C", "README.md"], role="backend") is None
    assert check_command(["git", "blame", "-w", "README.md"], role="backend") is None
    assert check_command(["git", "blame", "-L", "1,10", "README.md"], role="backend") is None

    # git log -c and git show -c must be allowed
    assert check_command(["git", "log", "-c"], role="backend") is None
    assert check_command(["git", "log", "--cc"], role="backend") is None
    assert check_command(["git", "log", "-n", "5"], role="backend") is None
    assert check_command(["git", "log", "-5"], role="backend") is None
    assert check_command(["git", "show", "-c"], role="backend") is None
    assert check_command(["git", "show", "--cc"], role="backend") is None

    # git diff flags
    assert check_command(["git", "diff", "--stat"], role="backend") is None
    assert check_command(["git", "diff", "--no-ext-diff"], role="backend") is None
    assert check_command(["git", "diff", "--no-textconv"], role="backend") is None
    assert check_command(["git", "diff", "-w"], role="backend") is None
    assert check_command(["git", "diff", "-U3"], role="backend") is None

    # git log and show flags
    assert check_command(["git", "log", "--no-textconv"], role="backend") is None
    assert check_command(["git", "show", "--no-textconv"], role="backend") is None

    # Dangerous output / exec flags stay denied under any subcommand
    assert check_command(["git", "diff", "--output=leak.txt"], role="backend") is not None
    assert check_command(["git", "diff", "--ext-diff"], role="backend") is not None
    assert check_command(["git", "diff", "--textconv"], role="backend") is not None
    assert check_command(["git", "log", "--output=leak.txt"], role="backend") is not None
    assert check_command(["git", "show", "--output=leak.txt"], role="backend") is not None


def test_option_abbreviations_rejected():
    """Verify that git option abbreviations (--out=x, --ext, --text) are rejected by the allowlist."""
    for flag in ["--out=leak.txt", "--ext", "--text", "--out", "--ext-dif", "--textcon"]:
        res = check_command(["git", "diff", flag], role="backend")
        assert res is not None, f"Option abbreviation '{flag}' bypassed check_command allowlist!"


def test_python_m_allowlist():
    # Legitimate developer toolchain modules must be allowed
    assert check_command(["python", "-m", "pytest"], role="backend") is None
    assert check_command(["python", "-mpytest"], role="backend") is None
    assert check_command(["python", "-m", "pip", "list"], role="backend") is None
    assert check_command(["python", "-m", "venv", ".venv"], role="backend") is None
    assert check_command(["python", "-m", "unittest", "discover"], role="backend") is None
    assert check_command(["python", "-m", "py_compile", "test.py"], role="backend") is None
    assert check_command(["python", "-m", "json.tool", "test.json"], role="backend") is None

    # Arbitrary / unvetted modules must be blocked (with and without space)
    assert check_command(["python", "-m", "http.server"], role="backend") is not None
    assert check_command(["python", "-mhttp.server"], role="backend") is not None
    assert check_command(["python", "-m", "code"], role="backend") is not None
    assert check_command(["python", "-m"], role="backend") is not None


def test_python_versions_and_py_launcher():
    """Verify that versioned python binaries and Windows py launcher are governed."""
    # Versioned binaries (python3.11, python3.12, etc.)
    assert check_command(["python3.11", "-m", "pytest"], role="backend") is None
    assert check_command(["python3.11", "-m", "http.server"], role="backend") is not None
    assert check_command(["python3.11", "-mhttp.server"], role="backend") is not None
    assert check_command(["python3.11", "-c", "import os"], role="backend") is not None

    # Windows py launcher (py -3, py -3.11, py -2)
    assert check_command(["py", "-3", "-m", "pytest"], role="backend") is None
    assert check_command(["py", "-3.11", "-m", "pytest"], role="backend") is None
    assert check_command(["py", "-3", "-m", "http.server"], role="backend") is not None
    assert check_command(["py", "-3", "-mhttp.server"], role="backend") is not None
    assert check_command(["py", "-3", "-c", "import os"], role="backend") is not None


def test_node_execution_flags():
    """Verify node -e, -p, and -r / --require are denied at the boundary."""
    assert check_command(["node", "-e", "console.log(1)"], role="backend") is not None
    assert check_command(["node", "--eval", "console.log(1)"], role="backend") is not None
    assert check_command(["node", "-p", "process.env"], role="backend") is not None
    assert check_command(["node", "-r", "child_process", "app.js"], role="backend") is not None
    assert check_command(["node", "-r./exploit.js", "app.js"], role="backend") is not None
    assert check_command(["node", "--require", "./exploit.js", "app.js"], role="backend") is not None

    # Running script files remains permitted (subject to down-stream post-turn diff gate)
    assert check_command(["node", "app.js"], role="backend") is None
    assert check_command(["node", "test/run.js"], role="backend") is None


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


def test_git_diff_external_handling(tmp_path):
    """Verify that diff.external configuration does not break git diff and does not spawn external driver."""
    repo = tmp_path / "repo"
    repo.mkdir()

    null_dev = "NUL" if sys.platform == "win32" else "/dev/null"
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": null_dev,
        "GIT_CONFIG_NOSYSTEM": "1",
    }

    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, env=env)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo, check=True, env=env)

    dummy_file = repo / "file.txt"
    dummy_file.write_text("initial content\n", encoding="utf-8")
    subprocess.run(["git", "add", "file.txt"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=repo, check=True, env=env)

    # Configure external diff driver in repo
    canary = repo / "pwned.txt"
    diff_script = repo / "fake_diff.bat"
    diff_script.write_text(f"@echo pwned > {canary.resolve()}\n", encoding="utf-8")
    subprocess.run(["git", "config", "diff.external", str(diff_script.resolve())], cwd=repo, check=True, env=env)

    dummy_file.write_text("changed content\n", encoding="utf-8")

    # Build ToolGateway for Backend role
    auth_root = tmp_path / "auth"
    auth_root.mkdir()
    workspace = ScratchWorkspace(auth_root, repo, "attempt-diff", True)
    journal = OperationJournal()
    boundary = RuntimeBoundary(journal)
    backend_policy = get_role_policy("backend")
    contract = AgentContract(agent_id="backend-agent", work_order="WO-001", allow=["workspace/**"], deny=[], write_mode="read-write")

    gateway = ToolGateway(
        workspace=workspace,
        boundary=boundary,
        contract=contract,
        policy=backend_policy,
        session_id="sess-diff",
        attempt_id="attempt-diff",
        actor_id="codex",
        provider_id="mock",
    )

    # Execute git diff through ToolGateway
    res = gateway.run_command(["git", "diff"])
    # Must exit successfully without exit code 128
    assert res.returncode == 0, f"git diff failed with returncode {res.returncode}: {res.stderr}"
    assert "changed content" in res.stdout
    # External diff driver must NOT have executed
    assert not canary.exists(), "External diff driver was executed despite harness hardening!"
    # index.lock must not exist
    lock_file = repo / ".git" / "index.lock"
    assert not lock_file.exists(), "index.lock was left behind by git diff!"
