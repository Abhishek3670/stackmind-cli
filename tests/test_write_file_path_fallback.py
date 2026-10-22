from __future__ import annotations

from unittest.mock import MagicMock
from validators.kernel.providers.gateway import ProviderGateway
from validators.kernel.providers.models import Message, ToolCallRequest


def test_write_file_fallback_to_active_messages_target_files():
    mock_tool_gw = MagicMock()
    mock_tool_gw.write_file = MagicMock()

    pg = ProviderGateway(
        adapter=MagicMock(),
        tool_gateway=mock_tool_gw,
    )
    pg._active_messages = [
        Message.user("Please update the landing page.\n\nTarget files:\n- index.html")
    ]

    # Model calls write_file with content only, omitting path
    tc = ToolCallRequest(
        id="call-1",
        name="write_file",
        arguments={"content": "<h1>Crochet Artistry</h1>"},
    )
    res = pg.execute_tool_call(tc)

    # Should not raise ConsecutiveToolFailureError or return missing required argument error
    assert "Error: 'write_file' missing required argument 'path'" not in res
    mock_tool_gw.write_file.assert_called_once_with("index.html", "<h1>Crochet Artistry</h1>")


def test_write_file_fallback_to_html_content_heuristic():
    mock_tool_gw = MagicMock()
    mock_tool_gw.write_file = MagicMock()

    pg = ProviderGateway(
        adapter=MagicMock(),
        tool_gateway=mock_tool_gw,
    )
    pg._active_messages = []

    # Model calls write_file with html content without specifying path
    tc = ToolCallRequest(
        id="call-2",
        name="write_file",
        arguments={"content": "<!DOCTYPE html><html><body><h1>Crochet</h1></body></html>"},
    )
    res = pg.execute_tool_call(tc)

    assert "Error: 'write_file' missing required argument 'path'" not in res
    mock_tool_gw.write_file.assert_called_once_with("index.html", "<!DOCTYPE html><html><body><h1>Crochet</h1></body></html>")


def test_write_file_fallback_to_required_deliverable():
    mock_tool_gw = MagicMock()
    mock_tool_gw.write_file = MagicMock()

    pg = ProviderGateway(
        adapter=MagicMock(),
        tool_gateway=mock_tool_gw,
    )
    pg.required_deliverable = "assets/js/main.js"

    tc = ToolCallRequest(
        id="call-3",
        name="write_file",
        arguments={"content": "console.log('loaded');"},
    )
    res = pg.execute_tool_call(tc)

    assert "Error: 'write_file' missing required argument 'path'" not in res
    mock_tool_gw.write_file.assert_called_once_with("assets/js/main.js", "console.log('loaded');")


def test_write_file_target_files_list():
    mock_tool_gw = MagicMock()
    mock_tool_gw.write_file = MagicMock()

    pg = ProviderGateway(
        adapter=MagicMock(),
        tool_gateway=mock_tool_gw,
    )

    tc = ToolCallRequest(
        id="call-4",
        name="write_file",
        arguments={"target_files": ["style.css"], "content": "body { margin: 0; }"},
    )
    res = pg.execute_tool_call(tc)
    assert "Error: 'write_file' missing required argument 'path'" not in res
    mock_tool_gw.write_file.assert_called_once_with("style.css", "body { margin: 0; }")

