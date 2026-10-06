"""Focused tests for the deterministic security scanner (report remediation).

Covers the findings a model reviewer routinely misses: hardcoded secret
fallbacks and enabled debug flags — enforced through the shared scanner in the
ToolGateway tool and the D024 deliverable validation.
"""

from __future__ import annotations

from pathlib import Path

from validators.harness.security_scan import scan_file, scan_files

REPORT_BACKEND = '''import os

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-12345")
DEBUG = True

def create_app():
    app = Flask(__name__)
    app.run(debug=True)
'''


def test_detects_hardcoded_env_fallback_secret():
    findings = scan_file("src/backend.py", REPORT_BACKEND)
    codes = [(f.code, f.line) for f in findings]
    assert ("HARDCODED_SECRET_FALLBACK", 3) in codes


def test_detects_direct_secret_literal():
    content = "API_KEY = \"sk-prod-1234567890abcdef\"\n"
    findings = scan_file("src/config.py", content)
    assert [f.code for f in findings] == ["HARDCODED_SECRET_FALLBACK"]


def test_detects_debug_flags_in_both_styles():
    content = "DEBUG = True\napp.run(debug=True)\n"
    findings = scan_file("src/app.py", content)
    assert [f.code for f in findings] == ["DEBUG_MODE_ENABLED", "DEBUG_MODE_ENABLED"]
    assert {f.line for f in findings} == {1, 2}


def test_clean_code_passes():
    content = (
        "import os\n"
        "SECRET_KEY = os.environ[\"SECRET_KEY\"]\n"
        "DEBUG = bool(int(os.environ.get(\"DEBUG\", \"0\")))\n"
        "PASSWORD_POLICY = \"min-8-chars\"\n"
        "token_url = \"https://example.com/token\"\n"
        "# SECRET_KEY = 'ignored-comment'\n"
        "def handler(event, context):\n"
        "    return {'ok': True}\n"
    )
    assert scan_file("src/app.py", content) == []


def test_dynamic_exec_flagged():
    findings = scan_file("src/legacy.py", "result = eval(user_input)\n")
    assert [f.code for f in findings] == ["DANGEROUS_DYNAMIC_EXEC"]
    assert findings[0].severity == "medium"


def test_findings_sorted_and_format_stable():
    findings = scan_files([
        ("src/b.py", "DEBUG = True\n"),
        ("src/a.py", "PASSWORD = \"super-secret-password\"\n"),
    ])
    assert [f.path for f in findings] == ["src/a.py", "src/b.py"]
    formatted = findings[0].format()
    assert formatted.startswith("security finding HARDCODED_SECRET_FALLBACK (critical): ")
    assert "(src/a.py:1)" in formatted
    assert "super-secret-password" in formatted  # matched line carried as evidence


def test_gateway_run_security_scan_reports_finding_codes(tmp_path: Path):
    """The ToolGateway tool reports stable codes for the report-class findings."""
    from tests.test_tools_process_lifecycle import _setup_gateway

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "backend.py").write_text(REPORT_BACKEND, encoding="utf-8")
    gateway, _ = _setup_gateway(tmp_path, "codex")

    result = gateway.run_security_scan("src")
    assert result["success"] is False
    codes = {f["code"] for f in result["findings"]}
    assert "HARDCODED_SECRET_FALLBACK" in codes
    assert "DEBUG_MODE_ENABLED" in codes
    assert all("severity" in f and "message" in f for f in result["findings"])

    # Legacy detection preserved: direct literal + dynamic exec
    (tmp_path / "danger.py").write_text(
        "token = 'secret_key_1234567890123456'\neval('foo')\n", encoding="utf-8"
    )
    legacy = gateway.run_security_scan("danger.py")
    assert legacy["success"] is False
    assert len(legacy["findings"]) >= 1
