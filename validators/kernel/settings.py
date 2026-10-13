"""Cloud and local provider configuration management (settings.json).

Manages provider credentials, API endpoints, available model catalogs,
and agent role mappings for StackMind CLI and daemon.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


def sanitize_model_name(model: str, provider: str = "") -> str:
    """Normalize model string: strip whitespace and redundant CLI/provider prefixes."""
    m = (model or "").strip()
    if not m:
        return ""
    for prefix in ("ollama run ", "ollama ", "ollama/"):
        if m.lower().startswith(prefix):
            m = m[len(prefix):].strip()
    p_lower = (provider or "").lower()
    if p_lower in ("openai", "openrouter") and m.lower().startswith("openai/"):
        m = m[len("openai/"):].strip()
    return m


DEFAULT_MODELS_BY_PROVIDER: dict[str, list[str]] = {
    "anthropic": [
        "claude-3-7-sonnet-20250219",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-20241022",
        "claude-3-opus-20240229",
    ],
    "openai": [
        "gpt-4o",
        "gpt-4o-mini",
        "o1",
        "o3-mini",
        "gpt-4-turbo",
    ],
    "gemini": [
        "gemini-2.0-flash",
        "gemini-2.0-pro-exp-02-05",
        "gemini-1.5-pro",
        "gemini-1.5-flash",
    ],
    "ollama": [
        "gemma4:31b-cloud",
        "ornith-1.5-16k:latest",
        "qwen2.5-coder:7b",
        "llama3.1:8b",
        "deepseek-r1:8b",
    ],
    "openrouter": [
        "anthropic/claude-3.7-sonnet",
        "deepseek/deepseek-r1",
        "openai/gpt-4o",
        "meta-llama/llama-3.3-70b-instruct",
    ],
    "custom": [
        "default",
        "custom-model",
    ],
}

DEFAULT_BASE_URLS: dict[str, str] = {
    "anthropic": "https://api.anthropic.com",
    "openai": "https://api.openai.com/v1",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "ollama": "http://localhost:11434",
    "openrouter": "https://openrouter.ai/api/v1",
    "custom": "http://localhost:20128/v1",
}

DEFAULT_DISPLAY_NAMES: dict[str, str] = {
    "anthropic": "Anthropic (Claude)",
    "openai": "OpenAI (GPT/Codex)",
    "gemini": "Google Gemini",
    "ollama": "Ollama (Local LLM)",
    "openrouter": "OpenRouter",
    "custom": "Custom (OpenAI-compat)",
}


@dataclass
class ProviderConfig:
    name: str
    display_name: str = ""
    enabled: bool = True
    api_key: str = ""
    base_url: str = ""
    models: list[str] = field(default_factory=list)
    default_model: str = ""

    def __post_init__(self) -> None:
        if not self.display_name:
            self.display_name = DEFAULT_DISPLAY_NAMES.get(self.name, self.name.title())
        if not self.base_url:
            self.base_url = DEFAULT_BASE_URLS.get(self.name, "")
        if not self.models:
            self.models = list(DEFAULT_MODELS_BY_PROVIDER.get(self.name, []))
        # Sanitize model names (strip 'ollama ' prefixes, whitespace, etc.)
        self.models = [sanitize_model_name(m, self.name) for m in self.models if m]
        seen = set()
        self.models = [m for m in self.models if not (m in seen or seen.add(m))]
        if self.default_model:
            self.default_model = sanitize_model_name(self.default_model, self.name)
        elif self.models:
            self.default_model = self.models[0]

    @property
    def masked_api_key(self) -> str:
        """Return masked key for display in UI or logs."""
        key = self.api_key.strip()
        if not key:
            return "(not set)"
        if len(key) <= 8:
            return "***" + key[-3:]
        prefix = key[:7] if key.startswith(("sk-ant-", "sk-or-", "AIza")) else key[:4]
        return f"{prefix}...{key[-4:]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "enabled": self.enabled,
            "api_key": self.api_key,
            "base_url": self.base_url,
            "models": list(self.models),
            "default_model": self.default_model,
        }

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> ProviderConfig:
        return cls(
            name=name,
            display_name=str(data.get("display_name") or DEFAULT_DISPLAY_NAMES.get(name, name.title())),
            enabled=bool(data.get("enabled", True)),
            api_key=str(data.get("api_key") or ""),
            base_url=str(data.get("base_url") or DEFAULT_BASE_URLS.get(name, "")),
            models=list(data.get("models") or DEFAULT_MODELS_BY_PROVIDER.get(name, [])),
            default_model=str(data.get("default_model") or ""),
        )


@dataclass
class StackMindSettings:
    providers: dict[str, ProviderConfig] = field(default_factory=dict)
    roles: dict[str, dict[str, str]] = field(default_factory=dict)
    execution_profile: str = "medium"

    @property
    def effective_profile(self) -> Any:
        from validators.kernel.profile import ExecutionProfile
        return ExecutionProfile.from_str(self.execution_profile)

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_profile": self.execution_profile,
            "providers": {k: v.to_dict() for k, v in self.providers.items()},
            "roles": self.roles,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StackMindSettings:
        from validators.kernel.profile import ExecutionProfile

        raw_profile = data.get("execution_profile")
        try:
            profile_val = ExecutionProfile.from_str(raw_profile).value
        except ValueError:
            # Fallback gracefully to default for corrupted/invalid setting while logging
            profile_val = ExecutionProfile.MEDIUM.value

        providers_data = data.get("providers") or {}
        providers: dict[str, ProviderConfig] = {}
        for p_name, p_val in providers_data.items():
            if isinstance(p_val, dict):
                providers[p_name] = ProviderConfig.from_dict(p_name, p_val)
        roles = dict(data.get("roles") or {})
        if "code" in roles:
            code_cfg = roles.pop("code")
            if "backend" not in roles and isinstance(code_cfg, dict):
                roles["backend"] = dict(code_cfg)
            if "frontend" not in roles and isinstance(code_cfg, dict):
                roles["frontend"] = dict(code_cfg)

        default_roles = {
            "architecture": {"provider": "anthropic", "model": "claude-3-7-sonnet-20250219"},
            "backend": {"provider": "ollama", "model": "ornith-1.5-16k:latest"},
            "frontend": {"provider": "ollama", "model": "ornith-1.5-16k:latest"},
            "qa": {"provider": "ollama", "model": "ornith-1.5-16k:latest"},
            "gitops": {"provider": "ollama", "model": "ornith-1.5-16k:latest"},
        }
        for k, v in default_roles.items():
            if k not in roles:
                roles[k] = dict(v)

        for r_name, r_cfg in roles.items():
            if isinstance(r_cfg, dict):
                prov = str(r_cfg.get("provider", ""))
                mod = str(r_cfg.get("model", ""))
                if mod:
                    r_cfg["model"] = sanitize_model_name(mod, prov)
        return cls(providers=providers, roles=roles, execution_profile=profile_val)


def get_default_settings() -> StackMindSettings:
    """Construct initial default settings populated with detected environment keys."""
    # Check env variables
    anthropic_env = os.environ.get("ANTHROPIC_API_KEY", "")
    openai_env = os.environ.get("OPENAI_API_KEY", "")
    gemini_env = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY", "")
    ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    openrouter_env = os.environ.get("OPENROUTER_API_KEY", "")

    providers = {
        "anthropic": ProviderConfig(
            name="anthropic",
            display_name="Anthropic (Claude)",
            enabled=bool(anthropic_env) or True,
            api_key=anthropic_env,
            base_url=DEFAULT_BASE_URLS["anthropic"],
            models=list(DEFAULT_MODELS_BY_PROVIDER["anthropic"]),
            default_model="claude-3-7-sonnet-20250219",
        ),
        "openai": ProviderConfig(
            name="openai",
            display_name="OpenAI (GPT/Codex)",
            enabled=bool(openai_env),
            api_key=openai_env,
            base_url=DEFAULT_BASE_URLS["openai"],
            models=list(DEFAULT_MODELS_BY_PROVIDER["openai"]),
            default_model="gpt-4o",
        ),
        "gemini": ProviderConfig(
            name="gemini",
            display_name="Google Gemini",
            enabled=bool(gemini_env),
            api_key=gemini_env,
            base_url=DEFAULT_BASE_URLS["gemini"],
            models=list(DEFAULT_MODELS_BY_PROVIDER["gemini"]),
            default_model="gemini-2.0-flash",
        ),
        "ollama": ProviderConfig(
            name="ollama",
            display_name="Ollama (Local LLM)",
            enabled=True,
            api_key="",
            base_url=ollama_host,
            models=list(DEFAULT_MODELS_BY_PROVIDER["ollama"]),
            default_model="ornith-1.5-16k:latest",
        ),
        "openrouter": ProviderConfig(
            name="openrouter",
            display_name="OpenRouter",
            enabled=bool(openrouter_env),
            api_key=openrouter_env,
            base_url=DEFAULT_BASE_URLS["openrouter"],
            models=list(DEFAULT_MODELS_BY_PROVIDER["openrouter"]),
            default_model="anthropic/claude-3.7-sonnet",
        ),
        "custom": ProviderConfig(
            name="custom",
            display_name="Custom (OpenAI-compat)",
            enabled=bool(os.environ.get("CUSTOM_API_KEY") or os.environ.get("CUSTOM_BASE_URL")),
            api_key=os.environ.get("CUSTOM_API_KEY", ""),
            base_url=os.environ.get("CUSTOM_BASE_URL", DEFAULT_BASE_URLS["custom"]),
            models=list(DEFAULT_MODELS_BY_PROVIDER["custom"]),
            default_model="default",
        ),
    }

    # Default role mappings
    roles = {
        "architecture": {
            "provider": "anthropic" if anthropic_env else "ollama",
            "model": "claude-3-7-sonnet-20250219" if anthropic_env else "ornith-1.5-16k:latest",
        },
        "backend": {
            "provider": "ollama",
            "model": "ornith-1.5-16k:latest",
        },
        "frontend": {
            "provider": "ollama",
            "model": "ornith-1.5-16k:latest",
        },
        "qa": {
            "provider": "ollama",
            "model": "ornith-1.5-16k:latest",
        },
        "gitops": {
            "provider": "ollama",
            "model": "ornith-1.5-16k:latest",
        },
    }

    return StackMindSettings(providers=providers, roles=roles)


def find_settings_file(workspace: Path | str | None = None) -> Path | None:
    """Search for existing settings.json or setting.json across known paths."""
    ws = Path(workspace).resolve() if workspace else Path.cwd().resolve()
    from validators.kernel.home import get_global_settings_path, get_stackmind_home
    cli_home = get_stackmind_home()
    candidate_paths = [
        ws / ".sync" / "settings.json",
        ws / ".sync" / "setting.json",
        ws / "settings.json",
        ws / "setting.json",
        get_global_settings_path(),
        cli_home / "setting.json",
        # Legacy fallback
        Path.home() / ".stackmind" / "settings.json",
        Path.home() / ".stackmind" / "setting.json",
    ]
    for p in candidate_paths:
        try:
            if p.is_file():
                return p
        except (PermissionError, OSError):
            continue
    return None


def get_default_settings_path(workspace: Path | str | None = None) -> Path:
    """Return the canonical target path for writing settings.json."""
    ws = Path(workspace).resolve() if workspace else Path.cwd().resolve()
    sync_dir = ws / ".sync"
    if sync_dir.is_dir() or ws.exists():
        sync_dir.mkdir(parents=True, exist_ok=True)
        return sync_dir / "settings.json"
    return ws / "settings.json"


def load_settings(workspace: Path | str | None = None) -> StackMindSettings:
    """Load settings from disk, filling in missing providers and defaults."""
    settings_file = find_settings_file(workspace)
    defaults = get_default_settings()

    if not settings_file:
        # Save default settings template for user convenience
        target = get_default_settings_path(workspace)
        try:
            save_settings(defaults, workspace)
        except Exception:
            pass
        return defaults

    try:
        content = settings_file.read_text(encoding="utf-8")
        data = json.loads(content)
        loaded = StackMindSettings.from_dict(data)

        # Merge with defaults to ensure all standard providers exist
        for p_name, default_cfg in defaults.providers.items():
            if p_name not in loaded.providers:
                loaded.providers[p_name] = default_cfg
            else:
                cfg = loaded.providers[p_name]
                if not cfg.display_name:
                    cfg.display_name = default_cfg.display_name
                if not cfg.base_url:
                    cfg.base_url = default_cfg.base_url
                if not cfg.models:
                    cfg.models = list(default_cfg.models)
                if not cfg.default_model and cfg.models:
                    cfg.default_model = cfg.models[0]

        # Merge roles
        for r_name, r_cfg in defaults.roles.items():
            if r_name not in loaded.roles:
                loaded.roles[r_name] = r_cfg

        return loaded
    except Exception:
        return defaults


def save_settings(settings: StackMindSettings, workspace: Path | str | None = None) -> Path:
    """Save settings to disk in settings.json (and update any mirrored setting.json)."""
    target = find_settings_file(workspace) or get_default_settings_path(workspace)
    target.parent.mkdir(parents=True, exist_ok=True)

    data = settings.to_dict()
    text = json.dumps(data, indent=2)
    target.write_text(text, encoding="utf-8")

    # If workspace has root settings.json or setting.json, sync it too
    ws = Path(workspace).resolve() if workspace else Path.cwd().resolve()
    root_json = ws / "settings.json"
    if root_json.exists() and root_json != target:
        try:
            root_json.write_text(text, encoding="utf-8")
        except Exception:
            pass

    # Mirror setting.json alias in target directory
    try:
        alias = target.parent / "setting.json"
        if alias != target:
            alias.write_text(text, encoding="utf-8")
    except Exception:
        pass

    # If saving to global home, sync both ~/.stackmindcli and ~/.stackmind
    try:
        from validators.kernel.home import STACKMIND_CLI_DIR_NAME, LEGACY_STACKMIND_DIR_NAME
        cli_home = (Path.home() / STACKMIND_CLI_DIR_NAME).resolve()
        legacy_home = (Path.home() / LEGACY_STACKMIND_DIR_NAME).resolve()
        if target.parent.resolve() in (cli_home, legacy_home):
            for base in (cli_home, legacy_home):
                if base != target.parent.resolve():
                    base.mkdir(parents=True, exist_ok=True)
                    (base / "settings.json").write_text(text, encoding="utf-8")
                    (base / "setting.json").write_text(text, encoding="utf-8")
    except Exception:
        pass

    return target


def test_provider_connection(
    provider: ProviderConfig,
    timeout: float = 8.0,
) -> tuple[bool, str, list[str]]:
    """Test live connectivity and fetch available models for a provider.

    Returns (success, message, discovered_models).
    """
    p_name = provider.name.lower()
    base_url = provider.base_url.rstrip("/")
    api_key = provider.api_key.strip()

    if p_name == "ollama":
        url = f"{base_url}/api/tags"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "StackMind-CLI/3.3"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = [m.get("name") for m in data.get("models", []) if isinstance(m, dict) and m.get("name")]
                    return True, f"Ollama online ({len(models)} model(s) installed)", models
                return False, f"Ollama HTTP error {resp.status}", []
        except Exception as exc:
            return False, f"Could not reach Ollama at {base_url}: {exc}", []

    elif p_name in ("openai", "openrouter", "custom"):
        if not api_key and p_name != "custom":
            return False, "API key is required", []
        headers = {"User-Agent": "StackMind-CLI/3.3"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        candidate_urls = [f"{base_url}/models"]
        if not base_url.endswith("/v1"):
            candidate_urls.append(f"{base_url}/v1/models")

        last_err = None
        for cand_url in candidate_urls:
            targets = [cand_url]
            if "localhost" in cand_url:
                targets.append(cand_url.replace("localhost", "127.0.0.1"))
            for target_url in targets:
                try:
                    req = urllib.request.Request(target_url, headers=headers)
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        if resp.status == 200:
                            data = json.loads(resp.read().decode("utf-8"))
                            raw_models = data.get("data") or data.get("models") or []
                            models = [m.get("id") for m in raw_models if isinstance(m, dict) and m.get("id")]
                            count = len(models) if models else "Endpoint reachable"
                            return True, f"Connected to {provider.display_name} ({count} model(s))", models[:100]
                        return False, f"HTTP error {resp.status}", []
                except urllib.error.HTTPError as err:
                    last_err = err
                    if err.code in (401, 403):
                        return False, "Authentication failed: invalid API key", []
                    continue
                except Exception as exc:
                    last_err = exc
                    continue

        # Fallback for custom servers without /models endpoint: probe base URL
        if p_name == "custom":
            root_targets = [base_url]
            if "localhost" in base_url:
                root_targets.append(base_url.replace("localhost", "127.0.0.1"))
            for root_target in root_targets:
                try:
                    req = urllib.request.Request(root_target, headers=headers)
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        if resp.status in (200, 204):
                            return True, f"Connected to {provider.display_name} (Endpoint reachable)", []
                except urllib.error.HTTPError as err:
                    if err.code in (401, 403):
                        return False, "Authentication failed: invalid API key", []
                    if err.code in (404, 405):
                        return True, f"Connected to {provider.display_name} (Endpoint reachable)", []
                except Exception:
                    pass

        if isinstance(last_err, urllib.error.HTTPError):
            return False, f"HTTP error {last_err.code}: {last_err.reason}", []
        if last_err:
            return False, f"Connection error: {last_err}", []
        return False, "Could not reach endpoint", []

    elif p_name == "anthropic":
        if not api_key:
            return False, "Anthropic API key is required", []
        # Anthropic has a /v1/models endpoint or we can probe with minimal headers
        url = f"{base_url}/v1/models"
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "User-Agent": "StackMind-CLI/3.3",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = [m.get("id") for m in data.get("data", []) if isinstance(m, dict) and m.get("id")]
                    return True, f"Connected to Anthropic ({len(models)} model(s))", models
        except urllib.error.HTTPError as err:
            if err.code in (401, 403):
                return False, "Authentication failed: invalid Anthropic API key", []
            # If /v1/models returns 404 or other on older proxies, key format check
            if err.code == 404:
                return True, "Connected to Anthropic API endpoint", list(provider.models)
            return False, f"HTTP error {err.code}: {err.reason}", []
        except Exception as exc:
            return False, f"Connection error: {exc}", []
        return True, "Connected to Anthropic API", list(provider.models)

    elif p_name == "gemini":
        if not api_key:
            return False, "Google Gemini API key is required", []
        url = f"{base_url}/models?key={api_key}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "StackMind-CLI/3.3"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = [
                        m.get("name", "").replace("models/", "")
                        for m in data.get("models", [])
                        if isinstance(m, dict) and m.get("name")
                    ]
                    # Filter to generateContent supported models
                    gen_models = [m for m in models if "gemini" in m.lower()]
                    return True, f"Connected to Gemini ({len(gen_models)} models)", gen_models[:20]
                return False, f"HTTP error {resp.status}", []
        except urllib.error.HTTPError as err:
            if err.code in (400, 401, 403):
                return False, "Authentication failed: invalid Google API key", []
            return False, f"HTTP error {err.code}: {err.reason}", []
        except Exception as exc:
            return False, f"Connection error: {exc}", []

    return True, "Configured", list(provider.models)
