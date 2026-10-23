"""SDP-FIX-004 - transitive dependency freshness and release eligibility.

Covers the dependency model end to end with temporary workspaces:

* a completed dependent keeps its historical completion record and gains a
  separate current-evidence invalidation marker;
* invalidation follows declared ``dependencies`` transitively and leaves
  unrelated work orders untouched;
* the release gate refuses release while a required dependent is stale;
* a revalidation-only invalidation can be cleared by verified revalidation
  against the current upstream revision, while a rework-class invalidation
  cannot;
* restart / RunState serialization never restores stale evidence.
"""

from __future__ import annotations

from pathlib import Path
import shutil

import pytest
import yaml

from validators.harness.integration_gate import IntegrationGate
from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.contract import AgentContract as KernelContract
from validators.kernel.identity import AuthorizationPolicy
from validators.kernel.operations import OperationJournal
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace

GATEWAY_OPERATIONS = (
    "update_work_order",
    "reopen_work_order",
    "revalidate_work_order",
    "write_file",
    "read_file",
    "inspect_agent",
)


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    for bucket in ("ACTIVE", "BLOCKED", "COMPLETED"):
        (root / ".sync" / "work-orders" / bucket).mkdir(parents=True)
    return root


def _wo(wo_id: str, agent: str = "codex", path: str = "src/api.py", **extra) -> dict:
    record = {
        "id": wo_id,
        "revision": 1,
        "type": "FEATURE",
        "title": f"Deliver {path}",
        "status": "ACTIVE",
        "priority": "P1",
        "assigned_agents": [agent],
        "dependencies": [],
        "deliverable": {"type": "code", "path": path, "description": f"Author {path}"},
        "acceptance_criteria": [f"{path} exists", f"{path} integrates"],
    }
    record.update(extra)
    return record


def _write_wo(root: Path, data: dict, bucket: str = "ACTIVE") -> Path:
    target = root / ".sync" / "work-orders" / bucket / f"{data['id']}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def _wo_path(root: Path, wo_id: str, bucket: str = "ACTIVE") -> Path:
    return root / ".sync" / "work-orders" / bucket / f"{wo_id}.yaml"


def _load(root: Path, wo_id: str, bucket: str = "ACTIVE") -> dict:
    return yaml.safe_load(_wo_path(root, wo_id, bucket).read_text(encoding="utf-8"))


def _completed(root: Path, wo_id: str, revision: int = 2, **extra) -> Path:
    record = _wo(wo_id, revision=revision, status="COMPLETED", **extra)
    record["completed_at"] = "2026-02-01T00:00:00Z"
    record["completion"] = {
        "verified_at_revision": revision,
        "verified_at_contract_revision": 1,
        "summary": "shipped",
    }
    return _write_wo(root, record, bucket="COMPLETED")


def _gateway(root: Path, actor: str = "claude") -> ToolGateway:
    workspace = ScratchWorkspace.create(root, attempt_id=f"test-{actor}")
    contract = KernelContract(
        agent_id=actor,
        work_order="WO-000",
        allow=("workspace/.sync/work-orders/**",),
        deny=(),
        write_mode="read-write",
    ).freeze()
    return ToolGateway(
        workspace=workspace,
        boundary=RuntimeBoundary(OperationJournal()),
        contract=contract,
        policy=AuthorizationPolicy.permit(f"test-{actor}", GATEWAY_OPERATIONS),
        session_id="s1",
        attempt_id="a1",
        actor_id=actor,
        provider_id="mock",
    )


def _contract(root: Path, wo_id: str, revision: int) -> Path:
    data = {
        "schema_version": 1,
        "agent_id": "codex",
        "work_order": wo_id,
        "scope": {"allow": [{"module": "src/**"}], "deny": [], "write": "read-write"},
        "budget": {"max_files_touched": 5, "max_tokens": 10000},
        "contract_revision": revision,
    }
    target = root / ".sync" / "contracts" / f"{wo_id}.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def _deliverable_file(root: Path, rel: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        '"""Module."""\n\n\n'
        "def handle(payload: dict) -> dict:\n"
        '    """Return a deterministic response envelope."""\n'
        '    return {"ok": True, "payload": payload}\n',
        encoding="utf-8",
    )


def _artifacts_check(root: Path):
    return IntegrationGate(root)._check_artifacts_and_freshness(None, None, None)


def _issue_text(issues: list[str]) -> str:
    return "\n".join(issues)


# ─── Historical completion vs current evidence ──────────────────────────


def test_completed_dependent_keeps_history_but_loses_current_evidence(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001", path="src/api.py"))
    _completed(root, "WO-002", revision=2, path="src/client.py", dependencies=["WO-001"])
    before = _load(root, "WO-002", "COMPLETED")

    amended = _gateway(root).update_work_order(
        "WO-001",
        {"acceptance_criteria": ["src/api.py returns 404 for unknown ids"]},
        reason="tighten the acceptance contract",
        expected_revision=1,
    )
    assert amended["revision"] == 2

    after = _load(root, "WO-002", "COMPLETED")

    # Historical completion is byte-for-byte preserved.
    assert after["status"] == "COMPLETED"
    assert after["revision"] == before["revision"]
    assert after["completed_at"] == before["completed_at"]
    assert after["completion"] == before["completion"]

    stale = after["evidence_stale"]
    assert stale["revision"] == 2
    assert stale["requires_revalidation"] is True
    # Acceptance criteria are a requirement statement, not verification
    # metadata: the dependent may need code changes, so rework is required.
    assert stale["requires_rework"] is True
    assert stale["sources"] == [{
        "upstream_work_order": "WO-001",
        "upstream_revision": 2,
        "upstream_contract_revision": None,
        "via": "WO-001",
        "depth": 1,
        "fields": ["acceptance_criteria"],
        "reason": "tighten the acceptance contract",
        "invalidated_at": stale["sources"][0]["invalidated_at"],
        "requires_rework": True,
    }]


def test_invalidation_records_the_upstream_contract_revision(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001"))
    _contract(root, "WO-001", revision=7)
    _completed(root, "WO-002", path="src/client.py", dependencies=["WO-001"])

    _gateway(root).update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="test plan changed", expected_revision=1,
    )

    stale = _load(root, "WO-002", "COMPLETED")["evidence_stale"]
    assert stale["sources"][0]["upstream_contract_revision"] == 7


# ─── Transitive closure ─────────────────────────────────────────────────


def test_transitive_dependents_are_invalidated_and_unrelated_untouched(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001"))
    _write_wo(root, _wo("WO-002", path="src/middle.py", dependencies=["WO-001"]))
    _completed(root, "WO-003", revision=4, path="src/leaf.py", dependencies=["WO-002"])
    _completed(root, "WO-004", revision=2, path="src/unrelated.py", dependencies=[])
    _write_wo(root, _wo("WO-005", path="src/other.py", dependencies=["WO-099"]))
    unrelated_before = _load(root, "WO-004", "COMPLETED")

    _gateway(root).update_work_order(
        "WO-001", {"description": "interface widened"},
        reason="interface change", expected_revision=1,
    )

    middle = _load(root, "WO-002")
    leaf = _load(root, "WO-003", "COMPLETED")
    assert middle["evidence_stale"]["sources"][0]["depth"] == 1
    assert middle["evidence_stale"]["sources"][0]["via"] == "WO-001"
    assert leaf["evidence_stale"]["sources"][0]["depth"] == 2
    assert leaf["evidence_stale"]["sources"][0]["via"] == "WO-002"

    # Not in the dependency closure: no marker, no rewrite.
    assert "evidence_stale" not in _load(root, "WO-004", "COMPLETED")
    assert _load(root, "WO-004", "COMPLETED") == unrelated_before
    assert "evidence_stale" not in _load(root, "WO-005")


def test_accumulated_invalidations_are_not_overwritten(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001"))
    _completed(root, "WO-002", path="src/client.py", dependencies=["WO-001"])

    gateway = _gateway(root)
    gateway.update_work_order(
        "WO-001", {"acceptance_criteria": ["one"]}, reason="first", expected_revision=1,
    )
    gateway.update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="second", expected_revision=2,
    )

    stale = _load(root, "WO-002", "COMPLETED")["evidence_stale"]
    revisions = [
        (source["upstream_revision"], source["reason"]) for source in stale["sources"]
    ]
    assert revisions == [(2, "first"), (3, "second")]


# ─── Release eligibility ────────────────────────────────────────────────


CLIENT_TESTS = [{"source": "src/client.py", "tests": ["tests/test_client.py"]}]


def _test_file(root: Path, rel: str = "tests/test_client.py", body: str | None = None) -> Path:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        body if body is not None else "def test_client_contract():\n    assert True\n",
        encoding="utf-8",
    )
    return target


def _release_workspace(tmp_path: Path, *, with_client: bool = True) -> Path:
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001", path="src/api.py"))
    _deliverable_file(root, "src/api.py")
    if with_client:
        _completed(
            root, "WO-002", revision=2, path="src/client.py",
            dependencies=["WO-001"], test_plan=CLIENT_TESTS,
        )
        _deliverable_file(root, "src/client.py")
        _test_file(root)
    return root


def _archive_upstream(root: Path) -> None:
    from validators.kernel.daemon.supervisor import archive_completed_work_orders

    archive_completed_work_orders(root, ["WO-001"])


NPM_AVAILABLE = shutil.which("npm") is not None and shutil.which("node") is not None


def _build_manifest(
    root: Path, script: str = 'node -e "process.exit(0)"',
) -> Path:
    """Declare a required build the way a real project does: in its manifest."""
    import json

    target = root / "package.json"
    target.write_text(
        json.dumps({
            "name": "fixture-project",
            "version": "0.0.0",
            "private": True,
            "scripts": {"build": script},
        }),
        encoding="utf-8",
    )
    return target


def _build_check(root: Path):
    return IntegrationGate(root)._check_build_execution_integrity(None, None, None)


def _invalidate_dependent(root: Path, gateway: ToolGateway | None = None) -> None:
    (gateway or _gateway(root)).update_work_order(
        "WO-001",
        {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="verification plan changed",
        expected_revision=1,
    )


def test_acceptance_criteria_change_requires_rework(tmp_path: Path):
    root = _release_workspace(tmp_path)
    _gateway(root).update_work_order(
        "WO-001", {"acceptance_criteria": ["src/api.py returns 404"]},
        reason="criteria changed", expected_revision=1,
    )

    stale = _load(root, "WO-002", "COMPLETED")["evidence_stale"]
    assert stale["requires_rework"] is True

    with pytest.raises(PermissionError) as excinfo:
        _gateway(root).revalidate_work_order("WO-002", expected_revision=2)
    assert "reopen_work_order" in str(excinfo.value)
    assert _load(root, "WO-002", "COMPLETED")["evidence_stale"]["revision"] == 2


def test_release_blocked_for_stale_completed_dependent(tmp_path: Path):
    root = _release_workspace(tmp_path)

    check, issues, blockers, _ = _artifacts_check(root)
    assert check.passed is True, issues

    _gateway(root).update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="verification plan changed", expected_revision=1,
    )

    check, issues, blockers, _ = _artifacts_check(root)
    assert check.passed is False
    text = _issue_text(issues)
    assert "Stale evidence for WO-002" in text
    assert "upstream WO-001 r2" in text
    assert any("revalidate_work_order" in b["remediation"] for b in blockers)


def test_release_allowed_after_runtime_revalidation(tmp_path: Path):
    root = _release_workspace(tmp_path)
    gateway = _gateway(root)
    gateway.update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="verification plan changed", expected_revision=1,
    )
    assert _artifacts_check(root)[0].passed is False

    # The upstream's own work is re-verified on its next turn and archived;
    # only the dependent's evidence is still stale.
    _archive_upstream(root)

    revalidated = gateway.revalidate_work_order(
        "WO-002", expected_revision=2, verification={"notes": "re-ran the client suite"},
    )

    assert "evidence_stale" not in revalidated
    assert revalidated["status"] == "COMPLETED"
    assert revalidated["revision"] == 2
    assert revalidated["completion"]["summary"] == "shipped"
    history = revalidated["revalidation_history"][-1]
    assert history["revision"] == 2
    assert history["upstream_revisions"] == {"WO-001": 2}
    assert history["tests"] == ["tests/test_client.py"]
    assert history["returncode"] == 0
    assert "pytest" in history["command"]

    # Artifact hashes are recomputed by the runtime and bound to the record.
    import hashlib

    expected_hash = hashlib.sha256((root / "src/client.py").read_bytes()).hexdigest()
    assert history["artifact_hashes"] == {"src/client.py": expected_hash[:16]}

    check, issues, _, _ = _artifacts_check(root)
    assert check.passed is True, issues

    # The written record still satisfies the authoritative work-order schema.
    import json

    from jsonschema import Draft7Validator

    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "work-order.schema.json")
        .read_text(encoding="utf-8")
    )
    stored = _load(root, "WO-002", "COMPLETED")
    assert not list(Draft7Validator(schema).iter_errors(stored))


def test_artifact_change_after_revalidation_blocks_release(tmp_path: Path):
    """Revalidation is bound to the bytes it verified."""
    root = _release_workspace(tmp_path)
    gateway = _gateway(root)
    gateway.update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="verification plan changed", expected_revision=1,
    )
    _archive_upstream(root)
    gateway.revalidate_work_order(
        "WO-002", expected_revision=2, verification={"notes": "re-ran the client suite"},
    )
    assert _artifacts_check(root)[0].passed is True

    client = root / "src/client.py"
    client.write_text(
        client.read_text(encoding="utf-8") + "\n# edited after revalidation\n",
        encoding="utf-8",
    )

    check, issues, _, _ = _artifacts_check(root)
    assert check.passed is False
    assert "modified after test/review evidence was recorded" in _issue_text(issues)
    assert "src/client.py" in _issue_text(issues)


# ─── Required build evidence ────────────────


def test_build_requirement_is_read_from_the_workspace_manifest(tmp_path: Path):
    """Contracts carry no build command; the manifest is the declaration."""
    import json

    from validators.harness.integration_gate import detect_build_requirement

    root = _project(tmp_path)
    assert detect_build_requirement(root) is None

    (root / "Makefile").write_text(
        "test:\n\tpython -m pytest\n", encoding="utf-8",
    )
    assert detect_build_requirement(root) is None

    (root / "Makefile").write_text(
        "build:\n\tpython -m compileall src\n", encoding="utf-8",
    )
    make = detect_build_requirement(root)
    assert make["manifest"] == "Makefile"
    assert make["command"] == ["make", "build"]

    # A package.json build script takes precedence over the Makefile target.
    (root / "package.json").write_text(
        json.dumps({"scripts": {"build": "vite build"}}), encoding="utf-8",
    )
    node = detect_build_requirement(root)
    assert node["manifest"] == "package.json"
    assert node["command"] == ["npm", "run", "build"]
    assert node["script"] == "vite build"


@pytest.mark.skipif(not NPM_AVAILABLE, reason="npm/node are not installed")
def test_revalidation_executes_the_required_build(tmp_path: Path):
    """A project that declares a build must run it before evidence clears."""
    root = _release_workspace(tmp_path)
    _build_manifest(root)
    gateway = _gateway(root)
    _invalidate_dependent(root, gateway)
    _archive_upstream(root)

    revalidated = gateway.revalidate_work_order(
        "WO-002", expected_revision=2, verification={"notes": "re-ran build and suite"},
    )

    history = revalidated["revalidation_history"][-1]
    build = history["build"]
    import hashlib

    manifest_hash = hashlib.sha256((root / "package.json").read_bytes()).hexdigest()[:16]
    client_hash = hashlib.sha256((root / "src/client.py").read_bytes()).hexdigest()[:16]

    assert build["manifest"] == "package.json"
    assert build["command"] == "npm run build"
    assert build["returncode"] == 0
    assert build["manifest_sha256"] == manifest_hash
    # Build evidence is bound to the bytes the build produced.
    assert build["artifact_hashes"] == {"src/client.py": client_hash}
    assert history["artifact_hashes"] == {"src/client.py": client_hash}
    assert history["returncode"] == 0

    check, issues, _, _ = _build_check(root)
    assert check.passed is True, issues

    import json

    from jsonschema import Draft7Validator

    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas" / "work-order.schema.json")
        .read_text(encoding="utf-8")
    )
    stored = _load(root, "WO-002", "COMPLETED")
    assert not list(Draft7Validator(schema).iter_errors(stored))


@pytest.mark.skipif(not NPM_AVAILABLE, reason="npm/node are not installed")
def test_required_build_failure_leaves_stale_evidence_intact(tmp_path: Path):
    """A failing build is not verification: the marker and history stay untouched."""
    root = _release_workspace(tmp_path)
    _build_manifest(root, script='node -e "process.exit(7)"')
    gateway = _gateway(root)
    _invalidate_dependent(root, gateway)
    _archive_upstream(root)

    with pytest.raises(PermissionError) as excinfo:
        gateway.revalidate_work_order("WO-002", expected_revision=2)
    message = str(excinfo.value)
    assert "required build 'npm run build' failed" in message
    assert "rc=7" in message

    stored = _load(root, "WO-002", "COMPLETED")
    assert stored["evidence_stale"]["revision"] == 2
    assert "revalidation_history" not in stored
    assert _artifacts_check(root)[0].passed is False


def test_missing_build_tool_cannot_clear_stale_evidence(tmp_path: Path, monkeypatch):
    """A declared build that cannot be executed fails closed."""
    root = _release_workspace(tmp_path)
    _build_manifest(root)
    gateway = _gateway(root)
    _invalidate_dependent(root, gateway)
    _archive_upstream(root)

    import validators.kernel.tools as tools_module

    monkeypatch.setattr(tools_module.shutil, "which", lambda name: None)

    with pytest.raises(PermissionError) as excinfo:
        gateway.revalidate_work_order("WO-002", expected_revision=2)
    assert "is not installed" in str(excinfo.value)
    assert "stale evidence was not cleared" in str(excinfo.value)

    stored = _load(root, "WO-002", "COMPLETED")
    assert stored["evidence_stale"]["revision"] == 2
    assert "revalidation_history" not in stored


def test_projects_without_a_build_are_not_blocked(tmp_path: Path):
    """No declared build means no build evidence is required."""
    root = _release_workspace(tmp_path)
    gateway = _gateway(root)
    _invalidate_dependent(root, gateway)
    _archive_upstream(root)

    revalidated = gateway.revalidate_work_order("WO-002", expected_revision=2)

    history = revalidated["revalidation_history"][-1]
    assert "build" not in history
    check, issues, _, _ = _build_check(root)
    assert check.status == "skipped"
    assert not issues
    assert _artifacts_check(root)[0].passed is True


def test_release_requires_build_evidence_from_revalidation(tmp_path: Path):
    """Clearing a marker under no-build policy does not survive a declared build."""
    root = _release_workspace(tmp_path)
    gateway = _gateway(root)
    _invalidate_dependent(root, gateway)
    _archive_upstream(root)
    gateway.revalidate_work_order("WO-002", expected_revision=2)

    # The project now declares a build. The old revalidation never executed one,
    # so its record cannot stand in for build evidence.
    _build_manifest(root)

    check, issues, blockers, _ = _build_check(root)
    assert check.passed is False
    text = _issue_text(issues)
    assert "Revalidation of WO-002 recorded no build evidence" in text
    assert any("revalidate_work_order" in b["remediation"] for b in blockers)


@pytest.mark.skipif(not NPM_AVAILABLE, reason="npm/node are not installed")
def test_build_manifest_change_after_revalidation_blocks_release(tmp_path: Path):
    """Build evidence is bound to the manifest it was produced against."""
    root = _release_workspace(tmp_path)
    _build_manifest(root)
    gateway = _gateway(root)
    _invalidate_dependent(root, gateway)
    _archive_upstream(root)
    gateway.revalidate_work_order("WO-002", expected_revision=2)
    assert _build_check(root)[0].passed is True

    _build_manifest(root, script='node -e "process.exit(0)"; node -e "console.log(1)"')

    check, issues, _, _ = _build_check(root)
    assert check.passed is False
    assert "recorded against a different package.json" in _issue_text(issues)


@pytest.mark.skipif(not NPM_AVAILABLE, reason="npm/node are not installed")
def test_artifact_change_after_build_verification_blocks_release(tmp_path: Path):
    """The build binds the artifact bytes; later edits invalidate that evidence."""
    root = _release_workspace(tmp_path)
    _build_manifest(root)
    gateway = _gateway(root)
    _invalidate_dependent(root, gateway)
    _archive_upstream(root)
    gateway.revalidate_work_order("WO-002", expected_revision=2)
    assert _build_check(root)[0].passed is True

    client = root / "src/client.py"
    client.write_text(
        client.read_text(encoding="utf-8") + "\n# edited after the build\n",
        encoding="utf-8",
    )

    check, issues, _, _ = _build_check(root)
    assert check.passed is False
    text = _issue_text(issues)
    assert "Stale build evidence for 'src/client.py'" in text
    assert "modified after test/review evidence" in _issue_text(_artifacts_check(root)[1])


def test_revalidation_rejects_self_reported_evidence(tmp_path: Path):
    root = _release_workspace(tmp_path)
    gateway = _gateway(root)
    _test_file(root, body="def test_client_contract():\n    assert False, 'broken'\n")
    gateway.update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="verification plan changed", expected_revision=1,
    )

    with pytest.raises(PermissionError) as excinfo:
        gateway.revalidate_work_order(
            "WO-002",
            expected_revision=2,
            verification={"evidence": "all tests pass, verified by hand"},
        )
    assert "Declared verification tests failed" in str(excinfo.value)
    assert _load(root, "WO-002", "COMPLETED")["evidence_stale"]["revision"] == 2
    assert "revalidation_history" not in _load(root, "WO-002", "COMPLETED")

    check, _, _, _ = _artifacts_check(root)
    assert check.passed is False


def test_revalidation_requires_a_declared_test_plan(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001"))
    _completed(root, "WO-002", path="src/client.py", dependencies=["WO-001"])
    _deliverable_file(root, "src/client.py")

    gateway = _gateway(root)
    gateway.update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="verification plan changed", expected_revision=1,
    )

    with pytest.raises(PermissionError) as excinfo:
        gateway.revalidate_work_order("WO-002", expected_revision=2)
    assert "no test_plan" in str(excinfo.value)

    assert _load(root, "WO-002", "COMPLETED")["evidence_stale"]["revision"] == 2


def test_revalidation_rejects_missing_declared_tests(tmp_path: Path):
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001"))
    _completed(
        root, "WO-002", path="src/client.py", dependencies=["WO-001"],
        test_plan=[{"source": "src/client.py", "tests": ["tests/test_missing.py"]}],
    )
    _deliverable_file(root, "src/client.py")

    gateway = _gateway(root)
    gateway.update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="verification plan changed", expected_revision=1,
    )

    with pytest.raises(PermissionError) as excinfo:
        gateway.revalidate_work_order("WO-002", expected_revision=2)
    assert "missing from the workspace" in str(excinfo.value)
    assert _load(root, "WO-002", "COMPLETED")["evidence_stale"]["revision"] == 2


def test_revalidation_rejects_changed_artifacts(tmp_path: Path):
    root = _release_workspace(tmp_path)
    gateway = _gateway(root)
    gateway.update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="verification plan changed", expected_revision=1,
    )

    (root / "src/client.py").unlink()

    with pytest.raises(PermissionError) as excinfo:
        gateway.revalidate_work_order("WO-002", expected_revision=2)
    assert "no longer verify" in str(excinfo.value)
    assert _load(root, "WO-002", "COMPLETED")["evidence_stale"]["revision"] == 2


def test_rework_invalidation_cannot_be_revalidated(tmp_path: Path):
    root = _release_workspace(tmp_path)
    gateway = _gateway(root)
    gateway.update_work_order(
        "WO-001",
        {"deliverable": {"type": "code", "path": "src/api_v2.py", "description": "v2"}},
        reason="interface moved to a new module",
        expected_revision=1,
    )

    stale = _load(root, "WO-002", "COMPLETED")["evidence_stale"]
    assert stale["requires_rework"] is True

    with pytest.raises(PermissionError) as excinfo:
        gateway.revalidate_work_order("WO-002", expected_revision=2)
    assert "reopen_work_order" in str(excinfo.value)

    check, issues, blockers, _ = _artifacts_check(root)
    assert check.passed is False
    assert any("Reopen WO-002" in b["remediation"] for b in blockers)


def test_revalidation_refused_when_an_upstream_advanced_out_of_band(tmp_path: Path):
    """A hand-edited upstream must not be silently revalidated against."""
    root = _release_workspace(tmp_path)
    gateway = _gateway(root)
    gateway.update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="first", expected_revision=1,
    )

    # Simulate a mutation that bypassed the runtime (external editor / crash
    # before dependent marking): the upstream is newer than the marker says.
    upstream = yaml.safe_load(_wo_path(root, "WO-001").read_text(encoding="utf-8"))
    upstream["revision"] = 3
    _write_wo(root, upstream)

    with pytest.raises(PermissionError) as excinfo:
        gateway.revalidate_work_order("WO-002", expected_revision=2)
    assert "advanced from r2 to r3" in str(excinfo.value)
    assert _load(root, "WO-002", "COMPLETED")["evidence_stale"]["revision"] == 2

    # A second recorded amendment re-binds the marker to r3; now the dependent
    # can be revalidated against the current upstream.
    gateway.update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="second", expected_revision=3,
    )
    gateway.revalidate_work_order("WO-002", expected_revision=2)
    assert "evidence_stale" not in _load(root, "WO-002", "COMPLETED")


def test_archiving_cannot_clear_a_rework_invalidation(tmp_path: Path):
    """Archiving a turn is not rework: a rework marker survives the move."""
    root = _project(tmp_path)
    _write_wo(root, _wo("WO-001", path="src/api.py"))
    _write_wo(root, _wo("WO-002", path="src/client.py", dependencies=["WO-001"]))
    _deliverable_file(root, "src/api.py")
    _deliverable_file(root, "src/client.py")

    _gateway(root).update_work_order(
        "WO-001", {"description": "the endpoint narrows its accepted payloads"},
        reason="interface narrowed", expected_revision=1,
    )
    stale = _load(root, "WO-002")["evidence_stale"]
    assert stale["requires_rework"] is True

    # The dependent finishes its turn and is archived, with the upstream still
    # at the revision that caused the invalidation.
    completing = _load(root, "WO-002")
    completing["status"] = "COMPLETED"
    completing["completion"] = {"verified_at_revision": completing["revision"]}
    _write_wo(root, completing)

    from validators.kernel.daemon.supervisor import archive_completed_work_orders

    assert archive_completed_work_orders(root, ["WO-002"]) == ["WO-002"]

    archived = _load(root, "WO-002", "COMPLETED")
    assert archived["evidence_stale"]["requires_rework"] is True
    assert "reopen" in archived["evidence_stale"]["revalidation_blocked_reason"]

    _, issues, _, _ = _artifacts_check(root)
    assert "Stale evidence for WO-002" in _issue_text(issues)


def test_revalidation_refused_when_the_upstream_contract_revision_moves(tmp_path: Path):
    """The marker binds the upstream contract revision, not just its revision."""
    root = _release_workspace(tmp_path)
    _contract(root, "WO-001", revision=1)
    gateway = _gateway(root)

    gateway.update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="test plan changed", expected_revision=1,
    )
    stale = _load(root, "WO-002", "COMPLETED")["evidence_stale"]
    assert stale["sources"][0]["upstream_contract_revision"] == 1

    # The dependent's evidence was produced under contract r1; a contract bump
    # changes the requirements, so rerunning the old checks is not enough.
    _contract(root, "WO-001", revision=2)

    with pytest.raises(PermissionError) as excinfo:
        gateway.revalidate_work_order("WO-002", expected_revision=2)
    assert "contract revision changed from 1 to 2" in str(excinfo.value)
    assert _load(root, "WO-002", "COMPLETED")["evidence_stale"]["revision"] == 2
    assert _artifacts_check(root)[0].passed is False


def test_interrupted_revalidation_never_clears_the_marker(tmp_path: Path, monkeypatch):
    root = _release_workspace(tmp_path)
    gateway = _gateway(root)
    gateway.update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="verification plan changed", expected_revision=1,
    )

    import validators.kernel.tools as tools_module

    def _boom(*args, **kwargs):
        raise OSError("journal unavailable")

    monkeypatch.setattr(tools_module, "record_pending", _boom)

    with pytest.raises(PermissionError) as excinfo:
        gateway.revalidate_work_order("WO-002", expected_revision=2)
    assert "no change applied" in str(excinfo.value)
    assert _load(root, "WO-002", "COMPLETED")["evidence_stale"]["revision"] == 2


def test_failed_audit_after_revalidation_blocks_release_until_reconciled(
    tmp_path: Path, monkeypatch,
):
    root = _release_workspace(tmp_path)
    gateway = _gateway(root)
    gateway.update_work_order(
        "WO-001", {"test_plan": [{"source": "src/api.py", "tests": ["tests/test_api.py"]}]},
        reason="verification plan changed", expected_revision=1,
    )
    _archive_upstream(root)

    import validators.kernel.tools as tools_module

    real_append = tools_module.append_audit

    def _fail_revalidated(root_arg, entry, **kwargs):
        if entry.get("outcome") == "revalidated":
            return False
        return real_append(root_arg, entry, **kwargs)

    monkeypatch.setattr(tools_module, "append_audit", _fail_revalidated)

    result = gateway.revalidate_work_order("WO-002", expected_revision=2)
    assert result["amendment_audit"] == "pending_recovery"
    assert "evidence_stale" not in _load(root, "WO-002", "COMPLETED")

    from validators.kernel import work_order_ops
    from validators.kernel.work_order_ops import load_operations, reconcile_operations

    pending = [r for r in load_operations(root) if r.get("status") == "pending"]
    assert len(pending) == 1
    assert pending[0]["kind"] == "revalidate"

    # While the audit trail cannot be completed at all, release stays blocked.
    real_module_append = work_order_ops.append_audit
    monkeypatch.setattr(work_order_ops, "append_audit", lambda *a, **k: False)
    _, issues, _, _ = _artifacts_check(root)
    assert any("Unreconciled work-order operation" in issue for issue in issues)

    monkeypatch.setattr(work_order_ops, "append_audit", real_module_append)
    monkeypatch.setattr(tools_module, "append_audit", real_append)
    summary = reconcile_operations(root)
    assert summary["unresolved"] == []
    assert len(summary["recovered"]) == 1

    check, issues, _, _ = _artifacts_check(root)
    assert check.passed is True, issues


# ─── Durability across restart / RunState ───────────────────────────────


def test_restart_preserves_invalidation_state(tmp_path: Path):
    root = _release_workspace(tmp_path)
    _gateway(root).update_work_order(
        "WO-001", {"acceptance_criteria": ["one"]}, reason="first", expected_revision=1,
    )

    from validators.kernel.daemon.supervisor import Phase, RunState

    state = RunState(
        run_id="run-1",
        product_goal="goal",
        workspace=str(root),
        session_id="s1",
        phase=Phase.INTEGRATION_REVIEW,
    )
    restored = RunState.from_dict(state.to_dict())
    assert restored.workspace == str(root)

    # A brand-new gateway (process restart equivalent) still sees the marker
    # and refuses release.
    assert _load(root, "WO-002", "COMPLETED")["evidence_stale"]["revision"] == 2
    assert _artifacts_check(root)[0].passed is False


def test_reopening_a_completed_dependent_preserves_the_stale_provenance(tmp_path: Path):
    root = _release_workspace(tmp_path)
    gateway = _gateway(root)
    gateway.update_work_order(
        "WO-001", {"acceptance_criteria": ["one"]}, reason="first", expected_revision=1,
    )
    stale_before = _load(root, "WO-002", "COMPLETED")["evidence_stale"]

    reopened = gateway.reopen_work_order(
        "WO-002", reason="rework required after upstream change", expected_revision=2,
    )

    assert reopened["status"] == "ACTIVE"
    assert reopened["evidence_stale"]["fields"] == ["completion"]
    history = reopened["completion_history"][-1]
    assert history["revision"] == 2
    assert history["completion"]["summary"] == "shipped"

    import json

    audit = [
        json.loads(line)
        for line in (root / ".sync" / "reports" / "work_order_amendments.jsonl")
        .read_text(encoding="utf-8").splitlines()
        if line
    ]
    reopened_entry = [e for e in audit if e["outcome"] == "reopened"][-1]
    assert reopened_entry["previous_record"]["evidence_stale"] == stale_before
