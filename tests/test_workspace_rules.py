from pathlib import Path
from unittest.mock import MagicMock
from validators.harness.runner import AgentRunner
from validators.kernel.contract import AgentContract as KernelContract
from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.operations import OperationJournal
from validators.kernel.identity import get_role_policy
from validators.kernel.tools import ToolGateway
from validators.kernel.workspace import ScratchWorkspace
import pytest

def test_workspace_rules_preserves_web_and_code_file_extensions(tmp_path):
    runner = AgentRunner(tmp_path, "gemini")
    
    contract_mock = MagicMock()
    contract_mock.agent_id = "gemini"
    contract_mock.work_order = "WO-001"
    contract_mock.write_mode = "read-write"
    contract_mock.budget = {}
    contract_mock.allow_rules = [
        {"module": "index.html"},
        {"module": "styles.css"},
        {"module": "script.js"},
        {"module": "src/app.tsx"},
        {"module": "package.json"},
        {"module": ".sync/decisions/**"},
    ]
    contract_mock.deny_rules = [{"module": ".git/**"}]
    
    knowledge_api_mock = MagicMock()
    task_mock = MagicMock()
    task_mock.identifier = "WO-001"
    
    # Set provider_adapter so _build_tool_runtime executes
    runner.provider_adapter = MagicMock()
    
    tool_runtime = runner._build_tool_runtime(contract_mock, task_mock, knowledge_api_mock)
    assert tool_runtime is not None
    
    kc = tool_runtime.gateway.contract
    # Ensure all exact file targets are in allowed scope
    assert "workspace/index.html" in kc.allow
    assert "workspace/styles.css" in kc.allow
    assert "workspace/script.js" in kc.allow
    assert "workspace/src/app.tsx" in kc.allow
    assert "workspace/package.json" in kc.allow
    assert "workspace/.sync/decisions/**" in kc.allow
    
    # Verify write_file authorizes cleanly through ToolGateway without PermissionError
    tool_runtime.gateway.write_file("index.html", "<!DOCTYPE html><html></html>")
    written = tool_runtime.workspace.path_for("index.html")
    assert written.exists()
    assert written.read_text() == "<!DOCTYPE html><html></html>"

    tool_runtime.gateway.write_file("styles.css", "body { margin: 0; }")
    assert tool_runtime.workspace.path_for("styles.css").exists()

    tool_runtime.gateway.write_file("script.js", "console.log('test');")
    assert tool_runtime.workspace.path_for("script.js").exists()
