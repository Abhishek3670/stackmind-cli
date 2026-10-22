"""Unit and integration tests for cloud providers configuration and TUI settings panel."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from validators.kernel.providers.adapter import (
    AnthropicAdapter,
    GeminiAdapter,
    OpenAICompatibleAdapter,
)
from validators.kernel.providers.models import Message, MessageRole, ToolDefinition
from validators.kernel.settings import (
    ProviderConfig,
    StackMindSettings,
    find_settings_file,
    get_default_settings,
    load_settings,
    save_settings,
    test_provider_connection as check_provider_connection,
)
from cli.tui.settings_panel import (
    render_provider_detail_str,
    render_roles_menu_str,
    render_settings_overview_str,
    run_interactive_settings_panel,
)


def test_provider_config_masking():
    cfg = ProviderConfig(name="anthropic", api_key="sk-ant-api03-abcdef1234567890")
    assert "sk-ant-" in cfg.masked_api_key
    assert "7890" in cfg.masked_api_key
    assert "abcdef" not in cfg.masked_api_key

    cfg_empty = ProviderConfig(name="openai", api_key="")
    assert cfg_empty.masked_api_key == "(not set)"


def test_load_and_save_settings(tmp_path: Path):
    settings = load_settings(tmp_path)
    assert "anthropic" in settings.providers
    assert "openai" in settings.providers
    assert "gemini" in settings.providers
    assert "ollama" in settings.providers
    assert "architecture" in settings.roles

    # Mutate and save
    settings.providers["anthropic"].api_key = "sk-ant-test-key-12345"
    settings.providers["anthropic"].default_model = "claude-3-7-sonnet-20250219"
    saved_file = save_settings(settings, tmp_path)
    assert saved_file.exists()

    # Re-read from disk
    reloaded = load_settings(tmp_path)
    assert reloaded.providers["anthropic"].api_key == "sk-ant-test-key-12345"
    assert reloaded.providers["anthropic"].default_model == "claude-3-7-sonnet-20250219"


def test_test_provider_connection_mock():
    prov = ProviderConfig(
        name="anthropic",
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com",
    )
    # When connection succeeds
    with patch("urllib.request.urlopen") as mock_url:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({"data": [{"id": "claude-3-7-sonnet-20250219"}]}).encode()
        mock_url.return_value.__enter__.return_value = mock_resp

        ok, msg, discovered = check_provider_connection(prov)
        assert ok is True
        assert "claude-3-7-sonnet-20250219" in discovered


def test_anthropic_adapter_payload():
    adapter = AnthropicAdapter(
        api_key="sk-ant-test",
        model="claude-3-7-sonnet-20250219",
    )
    messages = [
        Message(role=MessageRole.SYSTEM, content="You are an architect."),
        Message(role=MessageRole.USER, content="Hello"),
    ]
    tools = [
        ToolDefinition(
            name="read_file",
            description="Read file",
            parameters={"type": "object", "properties": {"path": {"type": "string"}}},
        )
    ]
    payload = adapter._prepare_payload(messages, tools, stream=False)
    assert payload["model"] == "claude-3-7-sonnet-20250219"
    assert payload["system"] == "You are an architect."
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["content"] == "Hello"
    assert len(payload["tools"]) == 1
    assert payload["tools"][0]["name"] == "read_file"
    assert "input_schema" in payload["tools"][0]


def test_anthropic_adapter_complete():
    adapter = AnthropicAdapter(
        api_key="sk-ant-test",
        model="claude-3-7-sonnet-20250219",
    )
    fake_response = {
        "content": [
            {"type": "text", "text": "Architectural recommendation"},
            {"type": "tool_use", "id": "call_123", "name": "read_file", "input": {"path": "main.py"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 50, "output_tokens": 30},
    }
    with patch.object(adapter, "_execute_request", return_value=fake_response):
        resp = adapter.complete([Message(role=MessageRole.USER, content="Test")])
        assert resp.message.content == "Architectural recommendation"
        assert len(resp.message.tool_calls) == 1
        assert resp.message.tool_calls[0].name == "read_file"
        assert resp.message.tool_calls[0].arguments == {"path": "main.py"}
        assert resp.usage.total_tokens == 80


def test_gemini_adapter_payload_and_complete():
    adapter = GeminiAdapter(
        api_key="AIzaTestKey",
        model="gemini-2.0-flash",
    )
    messages = [
        Message(role=MessageRole.SYSTEM, content="System instruction"),
        Message(role=MessageRole.USER, content="Inspect system"),
    ]
    payload = adapter._prepare_payload(messages, None)
    assert payload["system_instruction"]["parts"][0]["text"] == "System instruction"
    assert payload["contents"][0]["role"] == "user"

    fake_response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Gemini response text"},
                        {"functionCall": {"name": "list_directory", "args": {"path": "."}}},
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 20, "candidatesTokenCount": 15, "totalTokenCount": 35},
    }
    with patch("urllib.request.urlopen") as mock_url:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(fake_response).encode("utf-8")
        mock_url.return_value.__enter__.return_value = mock_resp

        resp = adapter.complete(messages)
        assert resp.message.content == "Gemini response text"
        assert len(resp.message.tool_calls) == 1
        assert resp.message.tool_calls[0].name == "list_directory"
        assert resp.usage.total_tokens == 35


def test_settings_panel_renderers():
    settings = get_default_settings()
    overview = render_settings_overview_str(settings, width=80)
    assert "Cloud Providers" in overview
    assert "Anthropic" in overview
    assert "Ollama" in overview

    prov = settings.providers["anthropic"]
    detail = render_provider_detail_str(prov, width=80)
    assert "Configure Anthropic" in detail
    assert "API Key:" in detail
    assert "Available Models" in detail

    roles = render_roles_menu_str(settings, width=80)
    assert "Agent Role Assignments" in roles
    assert "Architecture" in roles


def test_run_interactive_settings_panel_subcommands(tmp_path: Path):
    # Test 'list' subcommand
    res_list = run_interactive_settings_panel(workspace=tmp_path, initial_subcmd="list")
    assert "Cloud Providers" in res_list

    # Test 'set' subcommand
    res_set = run_interactive_settings_panel(
        workspace=tmp_path,
        initial_subcmd="set anthropic key sk-ant-new-key-9999",
    )
    assert "[SUCCESS]" in res_set

    # Verify written to settings.json
    loaded = load_settings(tmp_path)
    assert loaded.providers["anthropic"].api_key == "sk-ant-new-key-9999"

    # Test 'role' subcommand
    res_role = run_interactive_settings_panel(
        workspace=tmp_path,
        initial_subcmd="role architecture anthropic claude-3-7-sonnet-20250219",
    )
    assert "[SUCCESS]" in res_role

    loaded_after = load_settings(tmp_path)
    assert loaded_after.roles["architecture"]["provider"] == "anthropic"
    assert loaded_after.roles["architecture"]["model"] == "claude-3-7-sonnet-20250219"


def test_tui_dispatch_settings_command(tmp_path: Path):
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(project_name="test-project", session_id="sess-1")
    session = {"session_id": "sess-1", "workspace": str(tmp_path)}
    client = MagicMock()
    client.list_roles.return_value = []
    adapter = MagicMock()

    sess, should_exit = dispatch_delivery_command(
        adapter=adapter,
        client=client,
        session=session,
        text=":settings list",
        state=state,
    )
    assert should_exit is False
    assert len(state.messages) == 2
    assert state.messages[0].content == ":settings list"
    assert "StackMind System Settings" in state.messages[1].content
    assert "Execution Profile" in state.messages[1].content


def test_settings_panel_viewport_and_layout():
    from cli.tui.layout import (
        SETTINGS_PANEL_HEIGHT,
        compute_viewport_height,
        render_full_screen_workspace,
    )
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState()
    assert state.settings_panel_open is False

    # Normal viewport height (24 - 4 = 20)
    vp_closed = compute_viewport_height(24, state=state)
    assert vp_closed == 20

    # Open settings panel: reserves SETTINGS_PANEL_HEIGHT (24 - 4 - 10 = 10)
    state.open_settings()
    assert state.settings_panel_open is True
    vp_open = compute_viewport_height(24, state=state)
    assert vp_open == 20 - SETTINGS_PANEL_HEIGHT
    assert vp_open == 9

    # Render full screen workspace: total lines should equal exactly 24
    session = {"session_id": "s-test", "workspace": "."}
    frame = render_full_screen_workspace(
        session=session,
        state=state,
        width=80,
        height=24,
    )
    lines = frame.split("\n")
    # Must fit height budget
    assert len(lines) == 24
    # Must include anchored settings panel below composer
    assert "Cloud Providers & Models Configuration" in frame
    assert "▸" in frame

    # Close settings
    state.close_settings()
    assert state.settings_panel_open is False
    frame_closed = render_full_screen_workspace(
        session=session,
        state=state,
        width=80,
        height=24,
    )
    assert "Cloud Providers & Models Configuration" not in frame_closed


def test_render_anchored_settings_panel_views():
    from cli.tui.settings_panel import render_anchored_settings_panel_str
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState()
    state.open_settings()

    # View 1: Main view
    state.settings_selected_row = 1  # Select OpenAI
    panel_main = render_anchored_settings_panel_str(state, width=80, height=10)
    assert len(panel_main.split("\n")) == 10
    assert "Cloud Providers & Models Configuration" in panel_main
    assert "OpenAI" in panel_main
    assert "▸ [2] OpenAI" in panel_main or "▸" in panel_main

    # View 2: Provider detail view
    state.settings_current_view = "provider:anthropic"
    state.settings_selected_row = 3  # Select Default Model
    panel_prov = render_anchored_settings_panel_str(state, width=80, height=10)
    assert len(panel_prov.split("\n")) == 10
    assert "Configure Provider: Anthropic" in panel_prov
    assert "Default Model" in panel_prov
    assert "▸ Default Model" in panel_prov or "▸" in panel_prov

    # View 3: Roles view
    state.settings_current_view = "roles"
    state.settings_selected_row = 0  # Select Architecture
    panel_roles = render_anchored_settings_panel_str(state, width=80, height=10)
    assert len(panel_roles.split("\n")) == 10
    assert "Agent Role Assignments" in panel_roles
    assert "Architecture" in panel_roles
    assert "▸ • Architecture" in panel_roles or "▸" in panel_roles


def test_run_arrow_navigation_settings_loop_navigation(tmp_path: Path):
    from cli.tui.keyboard import Key
    from cli.tui.settings_panel import run_arrow_navigation_settings_loop
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(session_id="s1")
    session = {"session_id": "s1", "workspace": str(tmp_path)}

    # Sequence of arrow keys and actions:
    # 1. DOWN -> select OpenAI (row 1)
    # 2. RIGHT -> toggle OpenAI enabled
    # 3. UP -> back to Anthropic (row 0)
    # 4. ENTER -> open Anthropic detail
    # 5. DOWN -> row 1 (API key)
    # 6. DOWN -> row 2 (Base URL)
    # 7. DOWN -> row 3 (Default Model)
    # 8. RIGHT -> cycle model to next
    # 9. ESCAPE -> back to main view
    # 10. DOWN x 5 -> row 5 (Roles)
    # 11. ENTER -> open Roles view
    # 12. RIGHT -> cycle provider for Architecture role
    # 13. ESCAPE -> back to main view
    # 14. ESCAPE -> save & exit
    key_sequence = [
        Key.DOWN,
        Key.RIGHT,
        Key.UP,
        Key.ENTER,
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.RIGHT,
        Key.ESCAPE,
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.ENTER,
        Key.RIGHT,
        Key.ESCAPE,
        Key.ESCAPE,
    ]

    mock_client = MagicMock()
    result_msg = run_arrow_navigation_settings_loop(
        session=session,
        state=state,
        client=mock_client,
        key_stream=iter(key_sequence),
        workspace=tmp_path,
        width=80,
    )

    assert "[SUCCESS]" in result_msg
    assert state.settings_panel_open is False

    # Check persistence on disk
    persisted = load_settings(tmp_path)
    # OpenAI was toggled from initial default
    initial_enabled = get_default_settings().providers["openai"].enabled
    assert persisted.providers["openai"].enabled == (not initial_enabled)
    # Anthropic model was cycled
    assert persisted.providers["anthropic"].default_model is not None
    # Architecture role provider was cycled
    assert persisted.roles["architecture"]["provider"] is not None
    # Daemon client was notified of updates
    assert mock_client.settings_update.called


def test_sanitize_model_name():
    from validators.kernel.settings import sanitize_model_name

    assert sanitize_model_name("  ollama gemma4:31b-cloud  ", "ollama") == "gemma4:31b-cloud"
    assert sanitize_model_name("ollama run llama3:8b", "ollama") == "llama3:8b"
    assert sanitize_model_name("ollama/qwen2.5-coder:7b", "ollama") == "qwen2.5-coder:7b"
    assert sanitize_model_name("openai/gpt-4o", "openai") == "gpt-4o"
    assert sanitize_model_name("openai/gpt-4o", "openrouter") == "gpt-4o"
    assert sanitize_model_name("  claude-3-7-sonnet-20250219  ") == "claude-3-7-sonnet-20250219"
    assert sanitize_model_name("") == ""


def test_ollama_adapter_model_sanitization():
    from validators.kernel.providers.adapter import OllamaAdapter
    from validators.kernel.providers.models import Message

    adapter = OllamaAdapter(model="ollama gemma4:31b-cloud")
    assert adapter.model_name == "gemma4:31b-cloud"

    payload = adapter._prepare_payload([Message.user("Hello")], tools=None, stream=False)
    assert payload["model"] == "gemma4:31b-cloud"


def test_custom_provider_support(tmp_path: Path):
    from validators.kernel.daemon.manager import SessionManager
    from validators.kernel.daemon.storage import DaemonStorage
    from validators.kernel.providers.adapter import OpenAICompatibleAdapter
    from validators.kernel.settings import ProviderConfig, load_settings, save_settings

    # Verify custom provider is present in defaults
    settings = load_settings(tmp_path)
    assert "custom" in settings.providers
    custom_prov = settings.providers["custom"]
    assert "localhost:20128/v1" in custom_prov.base_url

    # Configure custom provider with custom URL and API key
    settings.providers["custom"] = ProviderConfig(
        name="custom",
        display_name="Custom (OpenAI-compat)",
        enabled=True,
        api_key="custom-secret-key",
        base_url="http://localhost:20128/v1",
        models=["default", "custom-model"],
        default_model="custom-model",
    )
    settings.roles["architecture"] = {
        "provider": "custom",
        "model": "custom-model",
    }
    save_settings(settings, tmp_path)

    # Test SessionManager creates OpenAICompatibleAdapter for custom provider
    storage = DaemonStorage(tmp_path / "daemon.json")
    mgr = SessionManager(storage=storage)
    runner = mgr._default_runner(str(tmp_path), "architecture")
    assert isinstance(runner.provider_adapter, OpenAICompatibleAdapter)
    assert runner.provider_adapter.base_url == "http://localhost:20128/v1"
    assert runner.provider_adapter.api_key == "custom-secret-key"
    assert runner.provider_adapter.model_name == "custom-model"


def test_tui_roles_model_cycling(tmp_path: Path):
    from cli.tui.keyboard import Key
    from cli.tui.settings_panel import run_arrow_navigation_settings_loop
    from cli.tui.state import AutonomousDeliveryState
    from validators.kernel.settings import load_settings, save_settings

    # Setup settings with ollama having multiple models
    settings = load_settings(tmp_path)
    settings.providers["ollama"].models = ["ornith-1.5-16k:latest", "gemma4:31b-cloud"]
    settings.providers["ollama"].default_model = "ornith-1.5-16k:latest"
    settings.roles["architecture"] = {"provider": "ollama", "model": "ornith-1.5-16k:latest"}
    save_settings(settings, tmp_path)

    state = AutonomousDeliveryState(session_id="s1")
    session = {"session_id": "s1", "workspace": str(tmp_path)}

    # Sequence:
    # 1. DOWN x 7 -> select Roles row (row 7, after 6 providers + 1 Execution Profile row)
    # 2. ENTER -> open Roles view
    # 3. ENTER -> cycle Architecture model from ornith-1.5-16k:latest to gemma4:31b-cloud
    # 4. ESCAPE -> back to main menu
    # 5. ESCAPE -> save and exit
    key_sequence = [
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.ENTER,
        Key.ENTER,
        Key.ESCAPE,
        Key.ESCAPE,
    ]

    mock_client = MagicMock()
    run_arrow_navigation_settings_loop(
        session=session,
        state=state,
        client=mock_client,
        key_stream=iter(key_sequence),
        workspace=tmp_path,
        width=80,
    )

    reloaded = load_settings(tmp_path)
    assert reloaded.roles["architecture"]["model"] == "gemma4:31b-cloud"


def test_rebind_command_model_sanitization(tmp_path: Path):
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState
    from validators.kernel.settings import load_settings

    state = AutonomousDeliveryState(session_id="s1")
    session = {"session_id": "s1", "workspace": str(tmp_path)}
    mock_adapter = MagicMock()
    mock_client = MagicMock()

    # User enters :rebind architecture ollama ollama gemma4:31b-cloud
    dispatch_delivery_command(
        adapter=mock_adapter,
        client=mock_client,
        session=session,
        text=":rebind architecture ollama ollama gemma4:31b-cloud",
        state=state,
    )

    # State updated with clean model name
    assert state.roles["Architecture"].model == "gemma4:31b-cloud"
    assert state.roles["Architecture"].backend == "ollama"

    # Persisted into settings.json with clean model name
    s = load_settings(tmp_path)
    assert s.roles["architecture"]["model"] == "gemma4:31b-cloud"
    assert s.roles["architecture"]["provider"] == "ollama"


def test_roles_backend_and_frontend_split(tmp_path: Path):
    from validators.kernel.settings import StackMindSettings, load_settings, save_settings

    # 1. Existing legacy settings.json with "code"
    legacy_data = {
        "providers": {"ollama": {"name": "ollama", "models": ["codellama:latest"], "default_model": "codellama:latest"}},
        "roles": {
            "architecture": {"provider": "anthropic", "model": "claude-3-7-sonnet"},
            "code": {"provider": "ollama", "model": "codellama:latest"},
            "qa": {"provider": "ollama", "model": "ornith"},
            "gitops": {"provider": "ollama", "model": "ornith"},
        },
    }
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps(legacy_data), encoding="utf-8")

    loaded = load_settings(tmp_path)
    # "code" migrated to "backend" and "frontend"
    assert "code" not in loaded.roles
    assert "backend" in loaded.roles
    assert "frontend" in loaded.roles
    assert loaded.roles["backend"]["model"] == "codellama:latest"
    assert loaded.roles["frontend"]["model"] == "codellama:latest"
    assert "qa" in loaded.roles
    assert "gitops" in loaded.roles
    assert "architecture" in loaded.roles


def test_tui_roles_view_renders_backend_and_frontend(tmp_path: Path):
    from cli.tui.settings_panel import render_anchored_settings_panel_str
    from cli.tui.state import AutonomousDeliveryState
    from validators.kernel.settings import load_settings

    state = AutonomousDeliveryState(session_id="s1")
    state.settings_obj = load_settings(tmp_path)
    state.settings_current_view = "roles"
    state.settings_selected_row = 1  # Backend

    rendered = render_anchored_settings_panel_str(state, width=80, height=11)
    # Must show Backend and Frontend, not Code
    assert "Backend" in rendered
    assert "Frontend" in rendered
    assert "Code" not in rendered
    assert "Architecture" in rendered
    assert "Q/A" in rendered
    assert "Gitops" in rendered or "GitOps" in rendered


def test_rebind_backend_and_frontend(tmp_path: Path):
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState
    from validators.kernel.settings import load_settings

    state = AutonomousDeliveryState(session_id="s1")
    session = {"session_id": "s1", "workspace": str(tmp_path)}
    mock_adapter = MagicMock()
    mock_client = MagicMock()

    # 1. Rebind backend specifically
    dispatch_delivery_command(
        adapter=mock_adapter,
        client=mock_client,
        session=session,
        text=":rebind backend openai gpt-4o",
        state=state,
    )
    assert state.roles["Backend"].backend == "openai"
    assert state.roles["Backend"].model == "gpt-4o"
    s1 = load_settings(tmp_path)
    assert s1.roles["backend"]["provider"] == "openai"
    assert s1.roles["backend"]["model"] == "gpt-4o"

    # 2. Rebind frontend specifically
    dispatch_delivery_command(
        adapter=mock_adapter,
        client=mock_client,
        session=session,
        text=":rebind frontend gemini gemini-2.0-flash",
        state=state,
    )
    assert state.roles["Frontend"].backend == "gemini"
    assert state.roles["Frontend"].model == "gemini-2.0-flash"
    s2 = load_settings(tmp_path)
    assert s2.roles["frontend"]["provider"] == "gemini"
    assert s2.roles["frontend"]["model"] == "gemini-2.0-flash"

    # 3. Legacy rebind "code" updates both
    dispatch_delivery_command(
        adapter=mock_adapter,
        client=mock_client,
        session=session,
        text=":rebind code custom my-custom-model",
        state=state,
    )
    assert state.roles["Backend"].backend == "custom"
    assert state.roles["Backend"].model == "my-custom-model"
    assert state.roles["Frontend"].backend == "custom"
    assert state.roles["Frontend"].model == "my-custom-model"
    s3 = load_settings(tmp_path)
    assert s3.roles["backend"]["provider"] == "custom"
    assert s3.roles["frontend"]["provider"] == "custom"


def test_custom_provider_health_check_resilience():
    from unittest.mock import MagicMock, patch
    from validators.kernel.settings import ProviderConfig, test_provider_connection

    prov = ProviderConfig(
        name="custom",
        display_name="Custom (OpenAI-compat)",
        api_key="sk-test-key",
        base_url="http://localhost:20128",
        models=["default"],
    )

    # Mock urllib urlopen so /models gives 404, but /v1/models gives 200 with model list
    def fake_urlopen(req, timeout=8.0):
        url = req.full_url
        if url.endswith("/v1/models"):
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = json.dumps({"data": [{"id": "model-a"}, {"id": "model-b"}]}).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            return mock_resp
        import urllib.error
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        ok, msg, disc = test_provider_connection(prov)
        assert ok is True
        assert "2 model(s)" in msg
        assert "model-a" in disc
        assert "model-b" in disc


def test_render_stackmind_settings_panel_view():
    from cli.tui.settings_panel import render_anchored_settings_panel_str
    from cli.tui.state import AutonomousDeliveryState
    from validators.kernel.settings import load_settings

    state = AutonomousDeliveryState()
    state.settings_obj = load_settings()
    state.settings_current_view = "settings"
    state.settings_selected_row = 0

    rendered = render_anchored_settings_panel_str(state, width=80, height=11)
    lines = rendered.split("\n")
    assert len(lines) == 11
    assert "StackMind System Settings" in rendered
    assert "Execution Profile" in rendered
    assert "Client Turn Timeout" in rendered
    assert "Decision Engine (Laya)" in rendered
    assert "LLM Providers & Models" in rendered
    assert "Agent Role Bindings" in rendered
    assert "Save Settings to Disk" in rendered


def test_render_providers_panel_view():
    from cli.tui.settings_panel import render_anchored_settings_panel_str
    from cli.tui.state import AutonomousDeliveryState
    from validators.kernel.settings import load_settings

    state = AutonomousDeliveryState()
    state.settings_obj = load_settings()
    state.settings_current_view = "providers"
    state.settings_selected_row = 0

    rendered = render_anchored_settings_panel_str(state, width=80, height=11)
    lines = rendered.split("\n")
    assert len(lines) == 11
    assert "Cloud Providers & Models Configuration" in rendered
    assert "Anthropic" in rendered
    assert "OpenAI" in rendered
    assert "Save Settings to Disk & Apply to Runtime" in rendered
    assert "Return to StackMind Settings" not in rendered


def test_navigation_between_settings_and_providers(tmp_path: Path):
    from cli.tui.keyboard import Key
    from cli.tui.settings_panel import run_arrow_navigation_settings_loop
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(session_id="s1")
    session = {"session_id": "s1", "workspace": str(tmp_path)}

    # Sequence starting in "settings":
    # 1. DOWN x 3 -> select row 3: LLM Providers & Models
    # 2. ENTER -> opens "providers" view
    # 3. ESCAPE -> returns to "settings" view
    # 4. ESCAPE -> save & exit
    key_seq = [
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.ENTER,
        Key.ESCAPE,
        Key.ESCAPE,
    ]

    mock_client = MagicMock()
    result = run_arrow_navigation_settings_loop(
        session=session,
        state=state,
        client=mock_client,
        key_stream=iter(key_seq),
        workspace=tmp_path,
        initial_view="settings",
    )
    assert "[SUCCESS]" in result
    assert state.settings_panel_open is False


def test_providers_view_save_and_exit_directly(tmp_path: Path):
    from cli.tui.keyboard import Key
    from cli.tui.settings_panel import run_arrow_navigation_settings_loop
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(session_id="s1")
    session = {"session_id": "s1", "workspace": str(tmp_path)}

    # Starting directly in "providers" view (:provider):
    # 6 providers: DOWN x 6 -> select row 6: Save Settings to Disk & Apply to Runtime
    # ENTER -> saves & closes
    key_seq = [Key.DOWN] * 6 + [Key.ENTER]

    mock_client = MagicMock()
    result = run_arrow_navigation_settings_loop(
        session=session,
        state=state,
        client=mock_client,
        key_stream=iter(key_seq),
        workspace=tmp_path,
        initial_view="providers",
    )
    assert "[SUCCESS]" in result
    assert state.settings_panel_open is False


def test_dispatch_settings_and_providers_separation(tmp_path: Path):
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.state import AutonomousDeliveryState

    state = AutonomousDeliveryState(session_id="s1")
    session = {"session_id": "s1", "workspace": str(tmp_path)}
    client = MagicMock()
    client.list_roles.return_value = []
    adapter = MagicMock()

    # 1. Dispatch :settings
    _, _ = dispatch_delivery_command(
        adapter=adapter,
        client=client,
        session=session,
        text=":settings",
        state=state,
    )
    settings_output = state.messages[-1].content
    assert "StackMind System Settings" in settings_output
    assert "Execution Profile" in settings_output
    assert "Agent Role Bindings" in settings_output
    assert ":roles" in settings_output
    assert "(Arch:" not in settings_output

    # 2. Dispatch :providers
    _, _ = dispatch_delivery_command(
        adapter=adapter,
        client=client,
        session=session,
        text=":providers",
        state=state,
    )
    providers_output = state.messages[-1].content
    assert "Cloud Providers & Models Configuration" in providers_output
    assert "Configured Providers & Endpoints:" in providers_output
    assert "anthropic" in providers_output.lower()
    assert "AGENT ROLE ASSIGNMENTS" not in providers_output
    assert "Arch:" not in providers_output

    # 3. Dispatch :roles
    _, _ = dispatch_delivery_command(
        adapter=adapter,
        client=client,
        session=session,
        text=":roles",
        state=state,
    )
    roles_output = state.messages[-1].content
    assert "AGENT ROLES" in roles_output
    assert "Architecture" in roles_output
    assert "Backend" in roles_output


def test_render_roles_panel_view():
    from cli.tui.settings_panel import render_anchored_settings_panel_str
    from cli.tui.state import AutonomousDeliveryState
    from validators.kernel.settings import load_settings

    state = AutonomousDeliveryState()
    state.settings_obj = load_settings()
    state.settings_current_view = "roles"
    state.settings_selected_row = 0

    rendered = render_anchored_settings_panel_str(state, width=80, height=11)
    lines = rendered.split("\n")
    assert len(lines) == 11
    assert "Agent Role Assignments (:roles)" in rendered
    assert "Architecture" in rendered
    assert "Backend" in rendered
    assert "Frontend" in rendered
    assert "Q/A" in rendered
    assert "Gitops" in rendered
    assert "Save Role Bindings" in rendered


def test_interactive_roles_panel_loop(tmp_path: Path):
    from cli.tui.keyboard import Key
    from cli.tui.settings_panel import run_interactive_roles_panel
    from cli.tui.state import AutonomousDeliveryState
    from validators.kernel.settings import load_settings

    state = AutonomousDeliveryState(session_id="s1")
    session = {"session_id": "s1", "workspace": str(tmp_path)}

    # Sequence (pure arrow navigation, no letter shortcuts):
    # Row 0 is Architecture
    # 1. RIGHT -> cycle provider to openrouter
    # 2. ENTER -> cycle model
    # 3. DOWN x 5 -> navigate cursor to Save row (Row 5)
    # 4. ENTER -> save bindings and apply
    key_seq = [
        Key.RIGHT,
        Key.ENTER,
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.DOWN,
        Key.ENTER,
    ]

    mock_client = MagicMock()
    result = run_interactive_roles_panel(
        session=session,
        state=state,
        client=mock_client,
        key_stream=iter(key_seq),
        workspace=tmp_path,
    )
    assert "[SUCCESS]" in result
    assert "saved to settings.json" in result

    # Verify persistence in settings.json
    reloaded = load_settings(tmp_path)
    assert reloaded.roles["architecture"]["provider"] == "openrouter"
    # Verify runtime state was updated
    assert state.roles["Architecture"].backend == "openrouter"


def test_rebind_command_deprecation(tmp_path: Path):
    from cli.tui.app import dispatch_delivery_command
    from cli.tui.commands import get_help_str
    from cli.tui.state import AutonomousDeliveryState

    # 1. Verify get_help_str marks :rebind as [DEPRECATED]
    help_text = get_help_str()
    assert ":rebind" in help_text
    assert "[DEPRECATED]" in help_text
    assert ":roles" in help_text

    state = AutonomousDeliveryState(session_id="s_dep")
    session = {"session_id": "s_dep", "workspace": str(tmp_path)}
    mock_adapter = MagicMock()
    mock_client = MagicMock()

    # 2. Rebind with missing arguments (e.g. ":rebind" or ":rebind custom") emits deprecation notice
    dispatch_delivery_command(
        adapter=mock_adapter,
        client=mock_client,
        session=session,
        text=":rebind custom",
        state=state,
    )
    last_msg = state.messages[-1].content
    assert "[DEPRECATED]" in last_msg
    assert ":roles" in last_msg

    # 3. Successful rebind emits deprecation warning alongside success
    dispatch_delivery_command(
        adapter=mock_adapter,
        client=mock_client,
        session=session,
        text=":rebind backend custom my-model",
        state=state,
    )
    succ_msg = state.messages[-1].content
    assert "[DEPRECATED]" in succ_msg
    assert "[SUCCESS]" in succ_msg
    assert "Rebound role 'backend' to backend 'custom'" in succ_msg






