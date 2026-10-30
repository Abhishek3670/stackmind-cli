from pathlib import Path
import io
import pytest

from validators.kernel.workspace import ScratchWorkspace, is_dataset_dir, HEAVY_BINARY_EXTENSIONS, HEAVY_IGNORE_DIRS
from validators.kernel.boundary import RuntimeBoundary
from validators.kernel.contract import AgentContract as KernelContract
from validators.kernel.identity import AuthorizationPolicy
from validators.kernel.operations import OperationJournal
from validators.kernel.tools import ToolGateway
from validators.harness.snapshot import WorkspaceSnapshot
from cli.tui.terminal import redraw_full_screen


def test_scratch_workspace_ignores_datasets_and_heavy_files(tmp_path: Path):
    auth = tmp_path / "authoritative"
    auth.mkdir()

    # Normal code / doc files
    (auth / "README.md").write_text("# Test Project", encoding="utf-8")
    (auth / "main.py").write_text("print('hello')", encoding="utf-8")

    # .gitignore
    (auth / ".gitignore").write_text("ignored_folder/\n*.log\n", encoding="utf-8")
    (auth / "ignored_folder").mkdir()
    (auth / "ignored_folder" / "temp.txt").write_text("should be ignored", encoding="utf-8")
    (auth / "app.log").write_text("log content", encoding="utf-8")

    # Dataset directory (YOLO structure: images/ and labels/)
    dataset_dir = auth / "VisionSentinel-detection"
    dataset_dir.mkdir()
    (dataset_dir / "images").mkdir()
    (dataset_dir / "labels").mkdir()
    (dataset_dir / "data.yaml").write_text("names: [obj]", encoding="utf-8")
    (dataset_dir / "images" / "sample.jpg").write_bytes(b"dummy image data")

    # Model weight
    (auth / "model.pt").write_bytes(b"fake model weights")

    # Large file (> 5MB)
    large_file = auth / "large_data.csv"
    large_file.write_bytes(b"x" * (6 * 1024 * 1024))

    # Standard ignored dir
    (auth / ".venv").mkdir()
    (auth / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin", encoding="utf-8")

    # .sync protocol
    sync_dir = auth / ".sync"
    sync_dir.mkdir()
    (sync_dir / "contracts").mkdir()
    (sync_dir / "contracts" / "WO-001.yaml").write_text("id: WO-001", encoding="utf-8")
    (sync_dir / "runtime").mkdir()
    (sync_dir / "runtime" / "daemon.pid").write_text("9999", encoding="utf-8")

    # Create scratch workspace
    ws = ScratchWorkspace.create(auth, "test-attempt")

    # Assertions
    # 1. Normal files kept
    assert (ws.root / "README.md").is_file()
    assert (ws.root / "main.py").is_file()
    assert (ws.root / ".sync" / "contracts" / "WO-001.yaml").is_file()

    # 2. Ignored files/folders not copied
    assert not (ws.root / "ignored_folder").exists()
    assert not (ws.root / "app.log").exists()
    assert not (ws.root / "VisionSentinel-detection").exists()
    assert not (ws.root / "model.pt").exists()
    assert not (ws.root / "large_data.csv").exists()
    assert not (ws.root / ".venv").exists()
    assert not (ws.root / ".sync" / "runtime").exists()


def test_tool_gateway_read_file_fallback_to_authoritative(tmp_path: Path):
    auth = tmp_path / "authoritative"
    auth.mkdir()

    # A file that was ignored in scratch (e.g. .pt or data in authoritative)
    (auth / "info.txt").write_text("important info in authoritative", encoding="utf-8")

    ws = ScratchWorkspace.create(auth, "test-read-fallback")
    assert not (ws.root / "info.txt").exists() or (ws.root / "info.txt").exists()

    # Even if we remove info.txt from scratch manually to simulate uncopied file:
    if (ws.root / "info.txt").exists():
        (ws.root / "info.txt").unlink()

    contract = KernelContract(
        agent_id="claude",
        work_order="WO-001",
        allow=("workspace/**",),
        deny=(),
    ).freeze()
    policy = AuthorizationPolicy.permit("read-any", ["read_file"])
    boundary = RuntimeBoundary(OperationJournal())

    gateway = ToolGateway(
        ws, boundary, contract, policy, "sess-1", "att-1", "claude", "provider"
    )

    # Reading info.txt should fall back to authoritative_root seamlessly
    content = gateway.read_file("info.txt")
    assert content == "important info in authoritative"


def test_workspace_snapshot_ignores_datasets(tmp_path: Path):
    root = tmp_path / "project"
    root.mkdir()

    (root / "README.md").write_text("Hello", encoding="utf-8")

    # Dataset folder
    ds = root / "dataset_train"
    ds.mkdir()
    (ds / "data.yaml").write_text("yolo dataset", encoding="utf-8")
    (ds / "img.jpg").write_bytes(b"data")

    # Heavy model
    (root / "weights.pt").write_bytes(b"weights")

    snap = WorkspaceSnapshot.capture(root)
    assert "README.md" in snap.files
    assert not any(p.startswith("dataset_train") for p in snap.files)
    assert "weights.pt" not in snap.files


def test_redraw_full_screen_handles_non_utf8_stream_with_emojis():
    # Simulate a stream with cp1252 encoding that cannot encode emoji '\U0001f44b' (👋)
    class Cp1252Stream(io.StringIO):
        encoding = "cp1252"
        def write(self, s: str):
            # simulate cp1252 charmap encoding error
            s.encode("cp1252")
            return super().write(s)

    bad_stream = Cp1252Stream()
    # Should not raise UnicodeEncodeError
    frame = redraw_full_screen(
        stream=bad_stream,
        conversation_content="Hey there! \U0001f44b How can I help you?",
        include_composer=True,
    )
    assert "\U0001f44b" in frame
