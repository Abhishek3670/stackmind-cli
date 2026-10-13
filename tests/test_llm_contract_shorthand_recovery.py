"""Tests for tolerant LLM contract shorthand ingestion and schema fallback in recovery."""

from __future__ import annotations

import json
from pathlib import Path
import pytest
import yaml

from cli.init import ensure_workspace_protocol_scaffolding
from validators.harness.authoring_gate import AuthoringGate
from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.contract import AgentContract as KernelContract, ContractNormalizer
from validators.kernel.identity import AuthorizationPolicy
from validators.kernel.operations import OperationJournal
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace
from validators.knowledge.contract import AgentContract as KnowledgeContract


CLAUDE_REAL_SHORTHAND_CONTRACT_YAML = """schema_version: 1.0
agent_id: gemma
work_order: WO-004
identity:
  role: QA Specialist
  reports_to: Claude
scope:
  allow:
    - tests/**
    - src/**
    - templates/**
    - .sync/inbox/claude/**
    - .sync/decisions/**
    - .sync/decisions/*
    - test_gacche.db
  deny:
    - gacche.db
  write:
    - tests/**
    - test_gacche.db
    - .sync/inbox/claude/**
budget:
  max_files_touched: 10
  max_tokens: 50000
contract_revision: 3
guidelines: |
  - ABSOLUTELY FORBIDDEN: Modification or access to 'gacche.db'.
  - MANDATORY: Use 'test_gacche.db' or an in-memory SQLite database for all integration tests.
"""


def test_authoring_gate_accepts_and_normalizes_llm_shorthand():
    gate = AuthoringGate()
    decision = gate.validate_artifact_content(
        ".sync/contracts/WO-004.yaml",
        CLAUDE_REAL_SHORTHAND_CONTRACT_YAML,
        agent="claude",
    )
    assert decision.passed is True
    assert decision.errors == []
    assert decision.normalized_content is not None

    # Verify normalized content has canonical shape
    norm_data = yaml.safe_load(decision.normalized_content)
    assert norm_data["schema_version"] == 1
    assert norm_data["contract_revision"] == 3
    assert norm_data["scope"]["write"] == "read-write"
    
    # Each item in allow and deny must be a dict with 'module'
    for rule in norm_data["scope"]["allow"]:
        assert isinstance(rule, dict)
        assert "module" in rule
    for rule in norm_data["scope"]["deny"]:
        assert isinstance(rule, dict)
        assert "module" in rule


def test_tool_gateway_write_file_canonicalizes_llm_shorthand_contract(tmp_path: Path):
    ws = ScratchWorkspace.create(tmp_path, attempt_id="test-canonicalize")
    contract = KernelContract(
        agent_id="claude",
        work_order="WO-004",
        allow=("workspace/.sync/contracts/**",),
        deny=(),
        write_mode="read-write",
    )
    policy = AuthorizationPolicy.permit("claude", ("write_file", "read_file"))
    boundary = RuntimeBoundary(OperationJournal())
    gateway = ToolGateway(
        workspace=ws,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="test-session",
        attempt_id="test-attempt",
        actor_id="claude",
        provider_id="mock",
    )

    # Writing LLM shorthand contract succeeds
    gateway.write_file(".sync/contracts/WO-004.yaml", CLAUDE_REAL_SHORTHAND_CONTRACT_YAML)
    written_path = ws.path_for(".sync/contracts/WO-004.yaml")
    assert written_path.exists()

    written_data = yaml.safe_load(written_path.read_text(encoding="utf-8"))
    assert written_data["schema_version"] == 1
    assert written_data["scope"]["write"] == "read-write"
    assert any(r.get("module") == "tests/**" for r in written_data["scope"]["allow"])
    assert any(r.get("module") == "gacche.db" for r in written_data["scope"]["deny"])


def test_tool_gateway_read_file_schema_fallback(tmp_path: Path):
    ws = ScratchWorkspace.create(tmp_path, attempt_id="test-schema-fallback")
    contract = KernelContract(
        agent_id="claude",
        work_order="WO-004",
        allow=("workspace/schemas/**",),
        deny=(),
        write_mode="read-write",
    )
    policy = AuthorizationPolicy.permit("claude", ("read_file",))
    boundary = RuntimeBoundary(OperationJournal())
    gateway = ToolGateway(
        workspace=ws,
        boundary=boundary,
        contract=contract,
        policy=policy,
        session_id="test-session",
        attempt_id="test-attempt",
        actor_id="claude",
        provider_id="mock",
    )

    # Workspace does not have schemas/contract.schema.json on disk
    target = "schemas/contract.schema.json"
    assert not ws.path_for(target).exists()

    # read_file falls back to bundled schema and succeeds
    content = gateway.read_file(target)
    assert content
    schema_json = json.loads(content)
    assert schema_json["title"] == "stackmind Agent Contract"


def test_contract_normalizer_tolerates_write_list():
    raw = {
        "agent_id": "gemma",
        "work_order": "WO-004",
        "scope": {
            "allow": ["tests/**", "src/**"],
            "deny": ["gacche.db"],
            "write": ["tests/**", "test_gacche.db"],
        },
        "budget": {"max_files_touched": 10},
    }
    contract = ContractNormalizer.normalize(raw)
    assert contract.write_mode == "read-write"
    assert "tests/**" in contract.allow
    assert "src/**" in contract.allow
    assert "test_gacche.db" in contract.allow
    assert "gacche.db" in contract.deny


def test_ensure_workspace_protocol_scaffolding_mirrors_schemas(tmp_path: Path):
    ws = tmp_path / "project"
    ws.mkdir(parents=True, exist_ok=True)
    ensure_workspace_protocol_scaffolding(ws)
    
    schema_file = ws / "schemas" / "contract.schema.json"
    assert schema_file.exists()
    assert (ws / "schemas" / "work-order.schema.json").exists()
    data = json.loads(schema_file.read_text(encoding="utf-8"))
    assert data["title"] == "stackmind Agent Contract"
