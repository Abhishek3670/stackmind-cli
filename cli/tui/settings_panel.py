"""Interactive Cloud Providers & Models Configuration Panel for StackMind TUI.

Allows live interactive inspection, configuration, testing, and persistence
of cloud and local LLM providers, their API keys, base URLs, model catalogs,
and agent role mappings into settings.json.
"""

from __future__ import annotations

import getpass
import io
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterator, Mapping

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cli.tui.keyboard import Key, read_next_key
from cli.tui.views import render_roles_panel

from validators.kernel.settings import (
    DEFAULT_MODELS_BY_PROVIDER,
    ProviderConfig,
    StackMindSettings,
    load_settings,
    sanitize_model_name,
    save_settings,
    test_provider_connection,
)


def render_settings_overview_str(
    settings: StackMindSettings,
    width: int = 80,
    active_nav_hint: str = "[1-5] Configure Provider | [6] Assign Roles | [S] Save | [Esc/Q] Close",
) -> str:
    """Render the main cloud providers configuration overview box."""
    box_w = max(40, min(width - 2, 88))
    table = Table(box=None, show_header=True, header_style="bold #38bdf8", expand=True, padding=(0, 1))
    table.add_column("#", style="bold cyan", width=3, justify="right")
    table.add_column("Provider", style="bold white", width=18)
    table.add_column("Status", width=10)
    table.add_column("API Key / Host", style="dim", width=22)
    table.add_column("Active Model", style="#a7f3d0", ratio=1)

    provider_keys = ["anthropic", "openai", "gemini", "ollama", "openrouter", "custom"]
    for k in settings.providers:
        if k not in provider_keys:
            provider_keys.append(k)

    for i, p_name in enumerate(provider_keys, 1):
        prov = settings.providers.get(p_name)
        if not prov:
            continue
        status_text = Text("[ENABLED]", style="bold green") if prov.enabled else Text("[DISABLED]", style="dim #64748b")
        if p_name == "ollama":
            cred_str = prov.base_url or "localhost:11434"
        elif p_name == "custom" and not prov.api_key:
            cred_str = prov.base_url or "localhost:20128/v1"
        else:
            cred_str = prov.masked_api_key
        table.add_row(
            f"[{i}]",
            prov.display_name,
            status_text,
            cred_str,
            prov.default_model or (prov.models[0] if prov.models else "(none)"),
        )

    content = Table.grid(expand=True)
    content.add_row(Text("Configured Providers & Endpoints:", style="bold #e2e8f0"))
    content.add_row(table)

    prof_name = getattr(settings, "effective_profile", "medium").upper()
    gov_mode = getattr(settings, "govern_verbosity", "tree").upper()
    content.add_row(Text(f"\nExecution Profile: [{prof_name}] (Low | Medium | High)", style="bold #38bdf8"))
    content.add_row(Text(f"Governance Verbosity: [{gov_mode}] (Tree | Flat | Compact)  (Switch via :govern)", style="dim #a5b4fc"))
    content.add_row(Text("Agent Role Bindings: Configure & rebind via :roles", style="dim #94a3b8"))

    nav_hint = active_nav_hint or "[↑/↓] Navigate  [←/→] Toggle  [Enter] Configure  [Esc] Close"
    panel = Panel(
        content,
        title="[bold #60a5fa] Cloud Providers & Models Configuration (settings.json) [/]",
        subtitle=f"[dim #94a3b8] {nav_hint} [/]",
        border_style="#3b82f6",
        width=box_w,
    )

    console = Console(record=True, width=box_w)
    console.print(panel)
    return console.export_text().rstrip()


def render_provider_detail_str(
    prov: ProviderConfig,
    width: int = 80,
    status_msg: str | None = None,
) -> str:
    """Render details and configuration menu for a specific provider."""
    box_w = max(40, min(width - 2, 88))
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(style="bold #94a3b8", width=16)
    grid.add_column(style="white")

    grid.add_row("Provider ID:", prov.name)
    status_txt = Text("ENABLED", style="bold green") if prov.enabled else Text("DISABLED", style="dim #ef4444")
    grid.add_row("Status:", Text.assemble(status_txt, Text("  (Press [E] to toggle)", style="dim")))
    grid.add_row("API Key:", Text.assemble(Text(prov.masked_api_key, style="#fde047"), Text("  (Press [K] to set)", style="dim")))
    grid.add_row("Base URL:", Text.assemble(Text(prov.base_url or "(default)", style="#93c5fd"), Text("  (Press [U] to set)", style="dim")))
    grid.add_row("Default Model:", Text.assemble(Text(prov.default_model or "(none)", style="bold #34d399"), Text("  (Press [M] to choose)", style="dim")))

    models_list = Text("\nAvailable Models:\n", style="bold #e2e8f0")
    for i, m in enumerate(prov.models, 1):
        is_active = (m == prov.default_model)
        marker = " [CURRENT]" if is_active else ""
        style = "bold #34d399" if is_active else "#cbd5e1"
        models_list.append(f"  ({i}) {m}{marker}\n", style=style)

    outer = Table.grid(expand=True)
    outer.add_row(grid)
    outer.add_row(models_list)
    if status_msg:
        outer.add_row(Text(f"\n>> {status_msg}", style="bold #f59e0b"))

    panel = Panel(
        outer,
        title=f"[bold #60a5fa] Configure {prov.display_name} [/]",
        subtitle="[dim #94a3b8] [E] Toggle | [K] Key | [U] URL | [M] Model | [A] Add Model | [T] Test | [B] Back [/]",
        border_style="#60a5fa",
        width=box_w,
    )

    console = Console(record=True, width=box_w)
    console.print(panel)
    return console.export_text().rstrip()


def render_roles_menu_str(
    settings: StackMindSettings,
    width: int = 80,
    status_msg: str | None = None,
) -> str:
    """Render the interactive role mapping configuration view."""
    box_w = max(40, min(width - 2, 88))
    table = Table(box=None, show_header=True, header_style="bold #38bdf8", expand=True, padding=(0, 1))
    table.add_column("#", style="bold cyan", width=3)
    table.add_column("Agent Role", style="bold white", width=18)
    table.add_column("Provider", style="#93c5fd", width=14)
    table.add_column("Assigned Model", style="#34d399")

    roles_order = ["architecture", "backend", "frontend", "qa", "gitops"]
    for i, r in enumerate(roles_order, 1):
        r_cfg = settings.roles.get(r, {})
        p = r_cfg.get("provider", "ollama")
        m = r_cfg.get("model", "ornith-1.5-16k:latest")
        display_name = "Q/A" if r.lower() in ("qa", "q/a") else r.title()
        table.add_row(f"[{i}]", display_name, p, m)

    grid = Table.grid(expand=True)
    grid.add_row(table)
    if status_msg:
        grid.add_row(Text(f"\n>> {status_msg}", style="bold #f59e0b"))

    panel = Panel(
        grid,
        title="[bold #60a5fa] Agent Role Assignments [/]",
        subtitle="[dim #94a3b8] [1-5] Select Role to Rebind | [B] Back to Main Menu [/]",
        border_style="#60a5fa",
        width=box_w,
    )
    console = Console(record=True, width=box_w)
    console.print(panel)
    return console.export_text().rstrip()


def render_stackmind_settings_str(
    settings: StackMindSettings | None = None,
    width: int = 80,
    *,
    status_msg: str | None = None,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render StackMind system settings overview in tabular layout without lines."""
    if settings is None:
        settings = load_settings()

    box_w = max(40, min(width - 2, 88))
    table = Table(
        box=None,
        show_header=False,
        show_edge=False,
        pad_edge=False,
        collapse_padding=True,
        expand=True,
        padding=(0, 1),
    )
    table.add_column("name", style="bold white", width=25, no_wrap=True)
    table.add_column("value", width=18, no_wrap=True)
    table.add_column("desc", style="dim #94a3b8", ratio=1, no_wrap=True)

    # 1. Execution Profile
    prof_val = getattr(settings, "effective_profile", "medium").upper()
    prof_color = "#38bdf8" if prof_val == "LOW" else ("#4ade80" if prof_val == "MEDIUM" else "#c084fc")
    if prof_val == "LOW":
        prof_desc = "Fast: 0 optional skills, strict safety"
    elif prof_val == "MEDIUM":
        prof_desc = "Balanced: 2 skills, 600 tok budget"
    else:
        prof_desc = "Deep: 5 skills, 1500 tok budget"
    table.add_row(
        Text("Execution Profile", style="bold white"),
        Text(f"[{prof_val}]", style=f"bold {prof_color}"),
        Text(prof_desc, style="dim #94a3b8"),
    )

    # 1b. Governance Verbosity
    gov_val = getattr(settings, "govern_verbosity", "tree").upper()
    gov_color = "#38bdf8" if gov_val == "TREE" else ("#4ade80" if gov_val == "FLAT" else "#f59e0b")
    if gov_val == "TREE":
        gov_desc = "Hierarchical tree: Run harness with indented sub-actions"
    elif gov_val == "FLAT":
        gov_desc = "Linear stream: direct individual sub-actions"
    else:
        gov_desc = "Compact: folded harness summary only"
    table.add_row(
        Text("Governance Verbosity", style="bold white"),
        Text(f"[{gov_val}]", style=f"bold {gov_color}"),
        Text(f"{gov_desc} (:govern)", style="dim #94a3b8"),
    )

    # 2. Client Turn Timeout
    to_val = getattr(settings, "client_timeout", 0)
    to_str = f"[{to_val}s]" if to_val and to_val > 0 else "[DISABLED]"
    table.add_row(
        Text("Client Turn Timeout", style="bold white"),
        Text(to_str, style="bold #f59e0b" if to_val else "dim #94a3b8"),
        Text("Governance response timeout threshold", style="dim #94a3b8"),
    )

    # 3. Decision Engine
    table.add_row(
        Text("Decision Engine (Laya)", style="bold white"),
        Text("[DETERMINISTIC]", style="dim #60a5fa"),
        Text("Offline standby; contracts enforced", style="dim #94a3b8"),
    )

    # 4. LLM Providers & Models
    enabled_count = sum(1 for p in settings.providers.values() if p.enabled)
    total_count = len(settings.providers)
    table.add_row(
        Text("LLM Providers & Models", style="bold white"),
        Text(f"[{enabled_count}/{total_count} ENABLED]", style="bold #4ade80"),
        Text("Configure with :providers", style="dim #94a3b8"),
    )

    # 5. Agent Role Bindings
    table.add_row(
        Text("Agent Role Bindings", style="bold white"),
        Text("[CONFIGURED]", style="#38bdf8"),
        Text("Configure & rebind with :roles", style="dim #94a3b8"),
    )

    content = Table.grid(expand=True)
    content.add_row(Text("StackMind System Settings & Governance\n", style="bold white"))
    content.add_row(table)

    if status_msg:
        content.add_row(Text(f"\n>> {status_msg}", style="bold #f59e0b"))

    content.add_row(
        Text(
            "\nCommands:\n"
            "  :config      Open interactive StackMind configuration panel\n"
            "  :providers   Configure LLM providers, API keys & base URLs\n"
            "  :roles       Interactive Agent Roles & Execution Backend bindings",
            style="dim #64748b",
        )
    )

    panel = Panel(
        content,
        title="[bold #60a5fa] StackMind System Settings & Configuration (:config) [/]",
        subtitle="[dim #94a3b8] [↑/↓] Navigate  [←/→] Adjust  [Enter] Select  [Esc] Close [/]",
        border_style="#3b82f6",
        width=box_w,
        padding=(0, 1),
    )

    console = Console(record=True, width=box_w, force_terminal=force_color, no_color=no_color)
    console.print(panel)
    return console.export_text().rstrip()


render_config_str = render_stackmind_settings_str


def run_interactive_settings_panel(
    workspace: Path | None = None,
    client: Any | None = None,
    stream: Any = None,
    width: int = 80,
    initial_subcmd: str | None = None,
) -> str:
    """Run interactive configuration session or execute direct subcommands."""
    out = stream or sys.stdout
    settings = load_settings(workspace)

    # 1. Direct non-interactive subcommand execution
    if initial_subcmd:
        parts = initial_subcmd.strip().split()
        cmd = parts[0].lower() if parts else ""
        if cmd in ("list", "show", "get"):
            return render_settings_overview_str(settings, width=width)

        if cmd in ("profile", "execution-profile", "exec-profile"):
            if len(parts) >= 2:
                prof_val = parts[1].lower()
                if prof_val in ("low", "medium", "high"):
                    settings.execution_profile = prof_val
                    save_settings(settings, workspace)
                    if client and hasattr(client, "settings_update"):
                        try:
                            client.settings_update(settings.to_dict())
                        except Exception:
                            pass
                    return f"[SUCCESS] Execution profile set to {prof_val.upper()} in settings.json"
                else:
                    return f"[ERROR] Invalid profile '{prof_val}'. Available: low, medium, high"
            return f"Current execution profile: {settings.effective_profile.upper()}"

        if cmd in ("govern", "govern-verbosity", "verbosity"):
            if len(parts) >= 2:
                g_val = parts[1].lower()
                if g_val in ("tree", "flat", "compact"):
                    settings.govern_verbosity = g_val
                    save_settings(settings, workspace)
                    return f"[SUCCESS] Governance verbosity set to {g_val.upper()} in settings.json"
                else:
                    return f"[ERROR] Invalid verbosity '{g_val}'. Available: tree, flat, compact"
            return f"Current governance verbosity: {getattr(settings, 'govern_verbosity', 'tree').upper()}"

        if cmd == "set" and len(parts) >= 4:
            p_name = parts[1].lower()
            field_name = parts[2].lower()
            val = " ".join(parts[3:]).strip()
            if p_name not in settings.providers:
                return f"[ERROR] Unknown provider '{p_name}'. Available: {list(settings.providers.keys())}"
            prov = settings.providers[p_name]
            if field_name in ("key", "api_key", "apikey"):
                prov.api_key = val
                prov.enabled = True
            elif field_name in ("model", "default_model"):
                val = sanitize_model_name(val, p_name)
                prov.default_model = val
                if val not in prov.models:
                    prov.models.append(val)
            elif field_name in ("url", "base_url", "endpoint"):
                prov.base_url = val
            elif field_name in ("enabled", "enable"):
                prov.enabled = val.lower() in ("true", "1", "yes", "on")
            save_settings(settings, workspace)
            if client and hasattr(client, "settings_update"):
                try:
                    client.settings_update(settings.to_dict())
                except Exception:
                    pass
            return f"[SUCCESS] Updated {p_name}.{field_name} in settings.json"

        if cmd == "role" and len(parts) >= 3:
            role_name = parts[1].lower()
            prov_name = parts[2].lower()
            model_name = parts[3] if len(parts) >= 4 else None
            if prov_name in settings.providers and not model_name:
                model_name = settings.providers[prov_name].default_model
            if model_name:
                model_name = sanitize_model_name(model_name, prov_name)
            settings.roles[role_name] = {
                "provider": prov_name,
                "model": model_name or "default",
            }
            save_settings(settings, workspace)
            if client and hasattr(client, "settings_update"):
                try:
                    client.settings_update(settings.to_dict())
                except Exception:
                    pass
            return f"[SUCCESS] Bound role '{role_name}' to {prov_name} ({model_name}) in settings.json"

        if cmd == "test" and len(parts) >= 2:
            p_name = parts[1].lower()
            prov = settings.providers.get(p_name)
            if not prov:
                return f"[ERROR] Unknown provider '{p_name}'"
            ok, msg, discovered = test_provider_connection(prov)
            res_str = f"[{'SUCCESS' if ok else 'FAILURE'}] {msg}"
            if discovered:
                res_str += f"\nDiscovered models: {', '.join(discovered[:6])}"
            return res_str

    is_tty = False
    try:
        is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        pass

    if not is_tty:
        return render_settings_overview_str(settings, width=width)

    # 2. Interactive TTY loop
    provider_keys = ["anthropic", "openai", "gemini", "ollama", "openrouter", "custom"]
    for k in settings.providers:
        if k not in provider_keys:
            provider_keys.append(k)
    current_view = "main"  # "main", "provider:<name>", "roles"
    status_msg = ""

    while True:
        term_width = shutil.get_terminal_size(fallback=(80, 24)).columns
        use_width = min(term_width, width)

        if current_view == "main":
            overview_text = render_settings_overview_str(settings, width=use_width)
            out.write(f"\n{overview_text}\n")
            if status_msg:
                out.write(f"\x1b[1;33m>> {status_msg}\x1b[0m\n")
                status_msg = ""
            roles_idx = len(provider_keys) + 1
            out.write(f"\x1b[1;36mSelect Option [1-{len(provider_keys)} Provider, {roles_idx} Roles, S Save, Q/Esc Exit]: \x1b[0m")
            out.flush()

            try:
                choice = input().strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not choice or choice.lower() in ("q", "quit", "exit", "esc"):
                break

            if choice.isdigit() and 1 <= int(choice) <= len(provider_keys):
                idx = int(choice) - 1
                current_view = f"provider:{provider_keys[idx]}"
            elif choice == str(roles_idx):
                current_view = "roles"
            elif choice.lower() in ("p", "profile"):
                curr = getattr(settings, "effective_profile", "medium").lower()
                nxt = {"low": "medium", "medium": "high", "high": "low"}.get(curr, "medium")
                settings.execution_profile = nxt
                status_msg = f"Execution profile set to {nxt.upper()}. Press [S] to save."
            elif choice.lower() in ("s", "save"):
                save_settings(settings, workspace)
                if client and hasattr(client, "settings_update"):
                    try:
                        client.settings_update(settings.to_dict())
                    except Exception:
                        pass
                status_msg = "Settings saved to settings.json and applied to runtime."

        elif current_view.startswith("provider:"):
            p_name = current_view.split(":", 1)[1]
            prov = settings.providers.get(p_name)
            if not prov:
                current_view = "main"
                continue

            detail_text = render_provider_detail_str(prov, width=use_width, status_msg=status_msg)
            status_msg = ""
            out.write(f"\n{detail_text}\n")
            out.write("\x1b[1;36mSelect Action [E/K/U/M/A/T/B]: \x1b[0m")
            out.flush()

            try:
                act = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                current_view = "main"
                continue

            if act in ("b", "back", "q", "exit", "esc"):
                current_view = "main"
            elif act == "e":
                prov.enabled = not prov.enabled
                status_msg = f"{prov.display_name} is now {'ENABLED' if prov.enabled else 'DISABLED'}."
            elif act == "k":
                out.write(f"\nEnter new API Key for {prov.display_name} (press Enter to keep existing): ")
                out.flush()
                new_key = input().strip()
                if new_key:
                    prov.api_key = new_key
                    prov.enabled = True
                    status_msg = f"Updated API Key for {prov.display_name}."
            elif act == "u":
                out.write(f"\nEnter Base URL for {prov.display_name} [{prov.base_url}]: ")
                out.flush()
                new_url = input().strip()
                if new_url:
                    prov.base_url = new_url
                    status_msg = f"Updated Base URL for {prov.display_name}."
            elif act == "m":
                out.write("\nChoose model number (or press Enter to cancel):\n")
                for i, m in enumerate(prov.models, 1):
                    out.write(f"  [{i}] {m}\n")
                out.write("Selection: ")
                out.flush()
                m_choice = input().strip()
                if m_choice.isdigit():
                    m_idx = int(m_choice) - 1
                    if 0 <= m_idx < len(prov.models):
                        prov.default_model = prov.models[m_idx]
                        status_msg = f"Active model set to '{prov.default_model}'."
            elif act == "a":
                out.write(f"\nEnter model name to add to {prov.display_name}: ")
                out.flush()
                new_m = input().strip()
                if new_m and new_m not in prov.models:
                    prov.models.append(new_m)
                    prov.default_model = new_m
                    status_msg = f"Added model '{new_m}' and set as active."
            elif act == "t":
                out.write(f"\nTesting connection to {prov.display_name}...\n")
                out.flush()
                ok, msg, discovered = test_provider_connection(prov)
                if discovered:
                    # Update available models if new ones discovered
                    for m in discovered:
                        if m not in prov.models:
                            prov.models.append(m)
                status_msg = f"{'SUCCESS' if ok else 'FAILED'}: {msg}"

        elif current_view == "roles":
            roles_text = render_roles_menu_str(settings, width=use_width, status_msg=status_msg)
            status_msg = ""
            out.write(f"\n{roles_text}\n")
            out.write("\x1b[1;36mSelect Role to Rebind [1 Architecture, 2 Backend, 3 Frontend, 4 QA, 5 GitOps, B Back]: \x1b[0m")
            out.flush()

            try:
                r_act = input().strip().lower()
            except (EOFError, KeyboardInterrupt):
                current_view = "main"
                continue

            roles_map = {"1": "architecture", "2": "backend", "3": "frontend", "4": "qa", "5": "gitops"}
            if r_act in roles_map:
                target_role = roles_map[r_act]
                out.write(f"\nAssign provider for role '{target_role.title()}':\n")
                for idx, p in enumerate(provider_keys, 1):
                    out.write(f"  [{idx}] {p}\n")
                out.write("Select provider: ")
                out.flush()
                p_choice = input().strip()
                if p_choice.isdigit() and 1 <= int(p_choice) <= len(provider_keys):
                    chosen_p = provider_keys[int(p_choice) - 1]
                    prov_obj = settings.providers.get(chosen_p)
                    chosen_m = prov_obj.default_model if prov_obj else "default"
                    settings.roles[target_role] = {"provider": chosen_p, "model": chosen_m}
                    status_msg = f"Assigned role '{target_role}' -> {chosen_p} ({chosen_m})."
            elif r_act in ("b", "back", "q", "exit", "esc"):
                current_view = "main"

    # Save on exit if modified
    save_settings(settings, workspace)
    if client and hasattr(client, "settings_update"):
        try:
            client.settings_update(settings.to_dict())
        except Exception:
            pass

    return "[SUCCESS] Settings updated in settings.json"


handle_system_settings_command = run_interactive_settings_panel


def render_anchored_settings_panel_str(
    state: Any,
    width: int = 80,
    height: int = 11,
    *,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render the anchored interactive settings panel below the composer.

    Guarantees exactly `height` lines (default 11) so the terminal screen stays
    anchored in-place with zero viewport scroll.
    Renders arrow navigation indicator '▸' on `state.settings_selected_row`.
    """
    settings: StackMindSettings = getattr(state, "settings_obj", None)
    if settings is None:
        settings = load_settings()
        if hasattr(state, "settings_obj"):
            state.settings_obj = settings

    view = getattr(state, "settings_current_view", "main")
    sel = getattr(state, "settings_selected_row", 0)
    status_msg = getattr(state, "settings_status_msg", "")
    box_w = max(40, width)

    grid = Table.grid(expand=True, padding=(0, 0))

    if view in ("settings", "stackmind", "config"):
        title = " StackMind System Settings & Configuration (:config) "
        subtitle = (
            f" >> {status_msg} "
            if status_msg
            else " [↑/↓] Navigate  [←/→] Adjust  [Enter] Select  [Esc] Close "
        )

        # Top padding
        grid.add_row(Text(" ", style="dim"))

        table = Table(
            box=None,
            show_header=False,
            show_edge=False,
            pad_edge=False,
            collapse_padding=True,
            expand=True,
            padding=(0, 1),
        )
        table.add_column("ptr", width=2, justify="left", no_wrap=True)
        table.add_column("name", width=25, no_wrap=True)
        table.add_column("value", width=18, no_wrap=True)
        table.add_column("desc", ratio=1, no_wrap=True)

        # Row 0: Execution Profile (merged with skills policy)
        is_sel_0 = (sel == 0)
        prof_val = getattr(settings, "effective_profile", "medium").upper()
        prof_color = "#38bdf8" if prof_val == "LOW" else ("#4ade80" if prof_val == "MEDIUM" else "#c084fc")
        if prof_val == "LOW":
            prof_desc = "Fast: 0 optional skills, strict safety"
        elif prof_val == "MEDIUM":
            prof_desc = "Balanced: 2 skills, 600 tok budget"
        else:
            prof_desc = "Deep: 5 skills, 1500 tok budget"
        table.add_row(
            Text("▸", style="bold #38bdf8") if is_sel_0 else Text(" ", style="dim"),
            Text("Execution Profile", style="bold white" if is_sel_0 else "white"),
            Text(f"[{prof_val}]", style=f"bold {prof_color}"),
            Text(prof_desc, style="#93c5fd" if is_sel_0 else "dim #94a3b8"),
        )

        # Row 1: Client Turn Timeout
        is_sel_1 = (sel == 1)
        to_val = getattr(state, "turn_timeout", None)
        if to_val is None:
            to_val = getattr(settings, "client_timeout", 0)
        to_str = f"[{to_val}s]" if to_val and to_val > 0 else "[DISABLED]"
        table.add_row(
            Text("▸", style="bold #38bdf8") if is_sel_1 else Text(" ", style="dim"),
            Text("Client Turn Timeout", style="bold white" if is_sel_1 else "white"),
            Text(to_str, style="bold #f59e0b" if to_val else "dim #94a3b8"),
            Text("Governance turn response timeout threshold", style="#93c5fd" if is_sel_1 else "dim #94a3b8"),
        )

        # Row 2: Decision Engine (Laya)
        is_sel_2 = (sel == 2)
        table.add_row(
            Text("▸", style="bold #38bdf8") if is_sel_2 else Text(" ", style="dim"),
            Text("Decision Engine (Laya)", style="bold white" if is_sel_2 else "white"),
            Text("[DETERMINISTIC]", style="bold #38bdf8" if is_sel_2 else "dim #60a5fa"),
            Text("Offline standby; contracts enforced", style="#93c5fd" if is_sel_2 else "dim #94a3b8"),
        )

        # Row 3: LLM Providers & Models
        is_sel_3 = (sel == 3)
        enabled_count = sum(1 for p in settings.providers.values() if p.enabled)
        total_count = len(settings.providers)
        table.add_row(
            Text("▸", style="bold #38bdf8") if is_sel_3 else Text(" ", style="dim"),
            Text("LLM Providers & Models", style="bold white" if is_sel_3 else "white"),
            Text(f"[{enabled_count}/{total_count} ENABLED]", style="bold #4ade80" if is_sel_3 else "dim #22c55e"),
            Text("Configure endpoints, API keys & models", style="#93c5fd" if is_sel_3 else "dim #94a3b8"),
        )

        # Row 4: Agent Role Bindings
        is_sel_4 = (sel == 4)
        table.add_row(
            Text("▸", style="bold #38bdf8") if is_sel_4 else Text(" ", style="dim"),
            Text("Agent Role Bindings", style="bold white" if is_sel_4 else "white"),
            Text("[CONFIGURED]", style="bold #38bdf8" if is_sel_4 else "dim #60a5fa"),
            Text("Configure agent contracts & role mappings", style="#93c5fd" if is_sel_4 else "dim #94a3b8"),
        )

        # Row 5: Save Settings
        is_sel_5 = (sel == 5)
        table.add_row(
            Text("▸", style="bold #38bdf8") if is_sel_5 else Text(" ", style="dim"),
            Text("Save Settings to Disk", style="bold #f59e0b" if is_sel_5 else "white"),
            Text("[SAVE & APPLY]", style="bold #f59e0b" if is_sel_5 else "dim #fbbf24"),
            Text("Persist configuration to settings.json", style="#fde047" if is_sel_5 else "dim #94a3b8"),
        )

        grid.add_row(table)

        # Row 7: Empty separator or status message
        if status_msg:
            grid.add_row(Text(f"  >> {status_msg}", style="bold #f59e0b", no_wrap=True))
        else:
            grid.add_row(Text(" ", style="dim"))

        # Row 8: Shifted hint at bottom
        hints_by_row = {
            0: "Hint: Press [←/→] to cycle execution profile (Low / Medium / High).",
            1: "Hint: Press [←/→] to adjust turn timeout (0s / 30s / 60s / 120s / 300s).",
            2: "Hint: Laya decision engine runs offline in deterministic fallback mode.",
            3: "Hint: Press [Enter] to open :providers (configure API keys & models).",
            4: "Hint: Press [Enter] to open :roles (configure agent role bindings).",
            5: "Hint: Press [Enter] to save settings to disk and apply changes.",
        }
        current_hint = hints_by_row.get(sel, "Hint: Use arrow keys to navigate. Press Enter to select or save.")
        grid.add_row(Text(f"  {current_hint}", style="dim #94a3b8", no_wrap=True))

    elif view in ("main", "providers"):
        title = " Cloud Providers & Models Configuration (settings.json) "
        subtitle = (
            f" >> {status_msg} "
            if status_msg
            else " [↑/↓] Navigate  [←/→] Toggle  [Enter] Configure  [Esc] Close "
        )

        provider_keys = ["anthropic", "openai", "gemini", "ollama", "openrouter", "custom"]
        for k in settings.providers:
            if k not in provider_keys:
                provider_keys.append(k)

        num_prov = len(provider_keys)
        for i in range(num_prov):
            is_sel = (i == sel)
            ptr = "▸ " if is_sel else "  "
            p_key = provider_keys[i]
            prov = settings.providers.get(p_key)
            row_txt = Text(no_wrap=True)
            row_txt.append(ptr, style="bold #38bdf8" if is_sel else "dim")
            if prov:
                row_txt.append(f"{prov.display_name:<18} ", style="bold white" if is_sel else "white")
                if prov.enabled:
                    row_txt.append("[ENABLED]  ", style="bold #22c55e" if not is_sel else "bold #4ade80")
                else:
                    row_txt.append("[DISABLED] ", style="dim #ef4444" if not is_sel else "dim #f87171")
                cred = (prov.base_url or "localhost:11434") if p_key == "ollama" else ((prov.base_url or "localhost:20128/v1") if p_key == "custom" and not prov.api_key else prov.masked_api_key)
                row_txt.append(f"{cred:<18} ", style="dim #94a3b8")
                model_str = prov.default_model or (prov.models[0] if prov.models else "(none)")
                row_txt.append(f"{model_str}", style="#a7f3d0" if is_sel else "#34d399")
            else:
                row_txt.append("(empty)", style="dim")
            grid.add_row(row_txt)

        if view == "providers":
            # Item num_prov: Return to StackMind Settings
            is_sel_ret = (sel == num_prov)
            ptr_ret = "▸ " if is_sel_ret else "  "
            ret_txt = Text(no_wrap=True)
            ret_txt.append(ptr_ret, style="bold #38bdf8" if is_sel_ret else "dim")
            ret_txt.append("◄ Return to StackMind Settings (:config)", style="bold #60a5fa" if is_sel_ret else "#38bdf8")
            grid.add_row(ret_txt)

            # Item num_prov + 1: Save & Exit
            is_sel_save = (sel == num_prov + 1)
            ptr_save = "▸ " if is_sel_save else "  "
            save_txt = Text(no_wrap=True)
            save_txt.append(ptr_save, style="bold #38bdf8" if is_sel_save else "dim")
            save_txt.append("Save Settings to Disk & Apply to Runtime", style="bold #f59e0b" if is_sel_save else "dim #fbbf24")
            grid.add_row(save_txt)

            # Row 8: Hint or status
            if status_msg:
                grid.add_row(Text(f" >> {status_msg}", style="bold #f59e0b", no_wrap=True))
            else:
                grid.add_row(Text("  Hint: Use arrow keys to navigate. Press Enter to configure provider.", style="dim #64748b"))

        else:
            # view == "main" (legacy fallback for tests)
            # Item num_prov: Execution Profile
            is_sel_prof = (sel == num_prov)
            ptr_prof = "▸ " if is_sel_prof else "  "
            prof_txt = Text(no_wrap=True)
            prof_txt.append(ptr_prof, style="bold #38bdf8" if is_sel_prof else "dim")
            prof_val = getattr(settings, "effective_profile", "medium").upper()
            prof_txt.append(f"[{num_prov + 1}] Execution Profile ", style="bold white" if is_sel_prof else "white")
            prof_color = "#38bdf8" if prof_val == "LOW" else ("#4ade80" if prof_val == "MEDIUM" else "#c084fc")
            prof_txt.append(f"[{prof_val}] ", style=f"bold {prof_color}")
            if prof_val == "LOW":
                short_desc = "(Fast, 0 optional skills, strict safety)"
            elif prof_val == "MEDIUM":
                short_desc = "(Balanced: 2 skills, 600 tok budget)"
            else:
                short_desc = "(Deep: 5 skills, 1500 tok budget, extended)"
            prof_txt.append(short_desc, style="#93c5fd" if is_sel_prof else "dim #64748b")
            grid.add_row(prof_txt)

            # Item num_prov + 1: Roles Mapping
            is_sel_roles = (sel == num_prov + 1)
            ptr_roles = "▸ " if is_sel_roles else "  "
            roles_txt = Text(no_wrap=True)
            roles_txt.append(ptr_roles, style="bold #38bdf8" if is_sel_roles else "dim")
            roles_txt.append(f"[{num_prov + 2}] Agent Role Assignments ", style="bold white" if is_sel_roles else "white")
            arch_p = settings.roles.get("architecture", {}).get("provider", "anthropic")
            back_p = settings.roles.get("backend", {}).get("provider", "ollama")
            front_p = settings.roles.get("frontend", {}).get("provider", "ollama")
            qa_p = settings.roles.get("qa", {}).get("provider", "ollama")
            roles_txt.append(f"(Arch: {arch_p} | Back: {back_p} | Front: {front_p} | QA: {qa_p})", style="#93c5fd" if is_sel_roles else "dim #64748b")
            grid.add_row(roles_txt)

            # Item num_prov + 2: Save & Exit
            is_sel_save = (sel == num_prov + 2)
            ptr_save = "▸ " if is_sel_save else "  "
            save_txt = Text(no_wrap=True)
            save_txt.append(ptr_save, style="bold #38bdf8" if is_sel_save else "dim")
            save_txt.append("[S] Save Settings to Disk & Apply to Runtime", style="bold #f59e0b" if is_sel_save else "dim #fbbf24")
            grid.add_row(save_txt)

    elif view == "profile":
        title = " Execution Profile Selection "
        is_selector_mode = getattr(state, "settings_profile_mode", False)
        subtitle = (
            f" >> {status_msg} "
            if status_msg
            else (
                " [↑/↓] Select Profile  [Enter] Apply & Save  [Esc/Q] Cancel "
                if is_selector_mode
                else " [↑/↓] Select Profile  [Enter] Apply & Save  [←/→] Toggle  [Esc/B] Back "
            )
        )

        cur_prof = getattr(settings, "effective_profile", "medium").lower()
        profiles = [
            (
                "low",
                "LOW",
                "Fast & Minimal Context",
                "0 optional skills, 0 token budget. Preserves contracts & D024 verification gates.",
                "#38bdf8",
            ),
            (
                "medium",
                "MEDIUM",
                "Balanced (Default)",
                "Up to 2 relevant skills, 600-token budget, progressive compact summaries.",
                "#4ade80",
            ),
            (
                "high",
                "HIGH",
                "Deep Guidance & Verification",
                "Up to 5 skills, 1500-token budget, full runbooks, extended verification.",
                "#c084fc",
            ),
        ]

        for i, (p_id, p_label, p_summary, p_detail, p_color) in enumerate(profiles):
            is_sel = (i == sel)
            is_active = (p_id == cur_prof)
            ptr = "▸ " if is_sel else "  "
            active_badge = "[ACTIVE] " if is_active else "         "
            row_txt = Text(no_wrap=True)
            row_txt.append(ptr, style="bold #38bdf8" if is_sel else "dim")
            row_txt.append(f"{p_label:<10} ", style=f"bold {p_color}")
            row_txt.append(active_badge, style="bold green" if is_active else "dim")
            row_txt.append(f"— {p_summary}", style="bold white" if is_sel else "white")
            grid.add_row(row_txt)

            detail_txt = Text(no_wrap=True)
            detail_txt.append("      " + p_detail, style="#93c5fd" if is_sel else "dim #64748b")
            grid.add_row(detail_txt)

        # Back / Cancel row (item 3)
        is_sel_back = (sel == 3)
        ptr_back = "▸ " if is_sel_back else "  "
        back_txt = Text(no_wrap=True)
        back_txt.append(ptr_back, style="bold #38bdf8" if is_sel_back else "dim")
        back_label = "◄ Cancel (keep current profile)" if is_selector_mode else "◄ Return to Configuration (:config)"
        back_txt.append(back_label, style="bold white" if is_sel_back else "dim white")
        grid.add_row(back_txt)

        grid.add_row(Text("  Hint: Use arrow keys to select profile and press Enter to apply.", style="dim #64748b"))
        if status_msg:
            grid.add_row(Text(f" >> {status_msg}", style="bold #f59e0b", no_wrap=True))
        else:
            grid.add_row(Text(" ", style="dim"))

    elif view.startswith("provider:"):
        p_name = view.split(":", 1)[1]
        prov = settings.providers.get(p_name)
        if not prov:
            prov = ProviderConfig(name=p_name, display_name=p_name.title())
        title = f" Configure Provider: {prov.display_name} "
        subtitle = " [↑/↓] Navigate  [←/→] Toggle/Cycle  [Enter] Select  [Esc] Back "

        fields = [
            ("Status", "[ENABLED]" if prov.enabled else "[DISABLED]", "Press ←/→ to toggle"),
            ("API Key", prov.masked_api_key, "Press Enter to set"),
            ("Base URL", prov.base_url or "(default)", "Press Enter to set"),
            ("Default Model", prov.default_model or "(none)", "Press ←/→ to cycle"),
            ("Model Catalog", f"{len(prov.models)} models available", "Press Enter to add model"),
            ("Health Check", "Test API connection", "Press Enter to test"),
            ("◄ Back", "Return to Main Menu", "Press Enter or Esc"),
        ]

        for i, (fname, fval, fhint) in enumerate(fields):
            is_sel = (i == sel)
            ptr = "▸ " if is_sel else "  "
            row_txt = Text(no_wrap=True)
            row_txt.append(ptr, style="bold #38bdf8" if is_sel else "dim")
            row_txt.append(f"{fname:<16} ", style="bold white" if is_sel else "dim #94a3b8")
            val_style = "bold #34d399" if fname in ("Status", "Default Model") else "#fde047" if fname == "API Key" else "white"
            row_txt.append(f"{fval:<26} ", style=val_style)
            row_txt.append(f"({fhint})", style="dim #64748b")
            grid.add_row(row_txt)

    elif view == "roles":
        title = " Agent Role Assignments (:roles) "
        subtitle = " [↑/↓] Select Role  [←/→] Provider  [Enter] Model  [Esc] Close "

        roles_list = ["architecture", "backend", "frontend", "qa", "gitops"]
        for i, r in enumerate(roles_list):
            is_sel = (i == sel)
            ptr = "▸ " if is_sel else "  "
            r_cfg = settings.roles.get(r, {})
            p_val = r_cfg.get("provider", "ollama")
            m_val = r_cfg.get("model", "default")
            display_name = "Q/A" if r.lower() in ("qa", "q/a") else r.title()
            row_txt = Text(no_wrap=True)
            row_txt.append(ptr, style="bold #38bdf8" if is_sel else "dim")
            row_txt.append(f"• {display_name:<14} ", style="bold white" if is_sel else "#94a3b8")
            row_txt.append(f"{p_val:<12} ", style="bold #93c5fd" if is_sel else "#60a5fa")
            row_txt.append("-> ", style="dim")
            row_txt.append(f"{m_val:<22} ", style="#34d399")
            row_txt.append("(←/→ Provider | Enter Model)", style="dim #64748b")
            grid.add_row(row_txt)

        # Save row (item 5)
        is_sel_5 = (sel == 5)
        ptr_5 = "▸ " if is_sel_5 else "  "
        save_txt = Text(no_wrap=True)
        save_txt.append(ptr_5, style="bold #38bdf8" if is_sel_5 else "dim")
        save_txt.append("Save Role Bindings to Disk & Apply to Runtime", style="bold #f59e0b" if is_sel_5 else "dim #fbbf24")
        grid.add_row(save_txt)

        # Hint & status rows to keep height exactly 11 lines
        grid.add_row(Text("  Navigation: [↑/↓] Select Role  [←/→] Cycle Provider  [Enter] Cycle Model", style="dim #64748b", no_wrap=True))
        grid.add_row(Text("  Select Save row and press Enter to save to settings.json.", style="dim #64748b", no_wrap=True))
        if status_msg:
            grid.add_row(Text(f" >> {status_msg}", style="bold #f59e0b", no_wrap=True))
        else:
            grid.add_row(Text(" ", style="dim"))

    # Status message or hint (only in subviews to keep main, providers, settings, roles, and profile exactly 11 lines)
    if status_msg and view not in ("main", "providers", "settings", "stackmind", "config", "profile", "roles"):
        grid.add_row(Text(f" >> {status_msg}", style="bold #f59e0b", no_wrap=True))
    elif view not in ("main", "providers", "settings", "stackmind", "config", "profile", "roles"):
        grid.add_row(Text(" ", style="dim"))

    panel = Panel(
        grid,
        title=f"[bold #60a5fa]{title}[/]",
        subtitle=f"[dim #94a3b8]{subtitle}[/]",
        border_style="#3b82f6",
        width=box_w,
        padding=(0, 1),
        expand=True,
    )

    sio = io.StringIO()
    console = Console(file=sio, record=True, width=box_w)
    console.print(panel)
    raw = console.export_text()
    lines = [l.rstrip("\r") for l in raw.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()

    if len(lines) < height:
        lines.extend([""] * (height - len(lines)))
    elif len(lines) > height:
        lines = lines[:height]
    return "\n".join(lines)


def _read_field_input(prompt_text: str, key_stream: Iterator[str] | None = None) -> str | None:
    """Read a field value safely from key_stream or interactive prompt."""
    if key_stream is not None:
        try:
            val = next(key_stream)
            return val.strip() if val and val not in (Key.ESCAPE, "q") else None
        except StopIteration:
            return None
    try:
        sys.stdout.write(f"\n\x1b[1;36m{prompt_text}\x1b[0m")
        sys.stdout.flush()
        res = input().strip()
        return res if res else None
    except Exception:
        return None


def run_arrow_navigation_settings_loop(
    session: Mapping[str, Any],
    state: Any,
    client: Any | None = None,
    live_manager: Any | None = None,
    key_stream: Iterator[str] | None = None,
    workspace: Path | None = None,
    width: int | None = None,
    stream: Any = None,
    initial_view: str = "main",
    is_profile_selector_mode: bool = False,
) -> str:
    """Run interactive configuration session with arrow keys navigation anchored below composer.

    Keys:
    - [↑/↓]: Navigate cursor '▸' smoothly between rows
    - [←/→]: Toggle enabled/disabled or cycle default models/role backends/profiles
    - [Enter]: Open provider submenu, profile selector, trigger test, edit key/url, or save
    - [Esc/Q]: Exit panel and resume normal TUI workspace view
    - [P]: Direct jump to execution profile selector
    - [S]: Instant save to settings.json and runtime sync
    """
    settings = load_settings(workspace)
    state.settings_profile_mode = is_profile_selector_mode
    state.open_settings(settings, initial_view=initial_view)

    provider_keys = ["anthropic", "openai", "gemini", "ollama", "openrouter", "custom"]
    for k in settings.providers:
        if k not in provider_keys:
            provider_keys.append(k)

    role_keys = ["architecture", "backend", "frontend", "qa", "gitops"]
    profile_keys = ["low", "medium", "high"]
    num_prov = len(provider_keys)

    if initial_view == "profile":
        cur_prof = getattr(settings, "effective_profile", "medium").lower()
        if cur_prof in profile_keys:
            state.settings_selected_row = profile_keys.index(cur_prof)

    def _redraw():
        if live_manager is not None and getattr(live_manager, "is_active", False):
            try:
                live_manager.update()
            except Exception:
                pass
        else:
            try:
                from cli.tui.app import redraw_full_screen

                redraw_full_screen(
                    session=session,
                    state=state,
                    clear=False,
                    include_composer=True,
                    composer_is_active=False,
                    stream=stream,
                )
            except Exception:
                pass

    _redraw()

    while True:
        try:
            if key_stream is not None:
                key = next(key_stream)
            else:
                key = read_next_key()
        except (StopIteration, KeyboardInterrupt, EOFError):
            break

        if not key:
            continue

        view = getattr(state, "settings_current_view", "main")
        sel = getattr(state, "settings_selected_row", 0)

        # Max rows per view
        if view in ("settings", "stackmind", "config"):
            max_rows = 6  # 0: Profile, 1: Timeout, 2: Laya, 3: Providers, 4: Roles, 5: Save
        elif view == "providers":
            max_rows = num_prov + 2  # 0..num_prov-1: Providers, num_prov: Return, num_prov+1: Save
        elif view == "main":
            max_rows = num_prov + 3  # providers (num_prov), profile (1), roles (1), save (1)
        elif view == "profile":
            max_rows = 4  # 0: Low, 1: Medium, 2: High, 3: Back/Cancel
        elif view.startswith("provider:"):
            max_rows = 7  # 0..6 fields
        elif view == "roles":
            max_rows = len(role_keys) + 1  # roles + 1 back row
        else:
            max_rows = num_prov + 3

        if key == Key.UP:
            state.settings_selected_row = max(0, sel - 1)
            state.settings_status_msg = ""
            _redraw()
            continue

        if key == Key.DOWN:
            state.settings_selected_row = min(max_rows - 1, sel + 1)
            state.settings_status_msg = ""
            _redraw()
            continue

        if key in (Key.LEFT, Key.RIGHT):
            step = 1 if key == Key.RIGHT else -1
            if view in ("settings", "stackmind", "config"):
                if sel == 0:  # Execution Profile
                    curr_p = getattr(settings, "effective_profile", "medium").lower()
                    curr_idx = profile_keys.index(curr_p) if curr_p in profile_keys else 1
                    next_idx = (curr_idx + step) % len(profile_keys)
                    new_p = profile_keys[next_idx]
                    settings.execution_profile = new_p
                    state.execution_profile = new_p
                    state.settings_status_msg = f"Execution profile set to {new_p.upper()}."
                    _redraw()
                elif sel == 1:  # Client turn timeout cycle
                    timeouts = [0, 30, 60, 120, 300]
                    curr_to = getattr(state, "turn_timeout", None)
                    if curr_to is None:
                        curr_to = getattr(settings, "client_timeout", 0)
                    curr_idx = timeouts.index(curr_to) if curr_to in timeouts else 0
                    next_idx = (curr_idx + step) % len(timeouts)
                    new_to = timeouts[next_idx]
                    state.turn_timeout = new_to
                    settings.client_timeout = new_to
                    state.settings_status_msg = f"Turn timeout set to {new_to}s." if new_to else "Turn timeout disabled."
                    _redraw()
            elif view == "providers":
                if 0 <= sel < num_prov:
                    p_name = provider_keys[sel]
                    prov = settings.providers.get(p_name)
                    if prov:
                        prov.enabled = not prov.enabled
                        state.settings_status_msg = f"{prov.display_name} is now {'ENABLED' if prov.enabled else 'DISABLED'}."
                        _redraw()
            elif view == "main":
                if 0 <= sel < num_prov:
                    p_name = provider_keys[sel]
                    prov = settings.providers.get(p_name)
                    if prov:
                        prov.enabled = not prov.enabled
                        state.settings_status_msg = f"{prov.display_name} is now {'ENABLED' if prov.enabled else 'DISABLED'}."
                        _redraw()
                elif sel == num_prov:
                    curr_p = getattr(settings, "effective_profile", "medium").lower()
                    curr_idx = profile_keys.index(curr_p) if curr_p in profile_keys else 1
                    next_idx = (curr_idx + step) % len(profile_keys)
                    new_p = profile_keys[next_idx]
                    settings.execution_profile = new_p
                    state.execution_profile = new_p
                    state.settings_status_msg = f"Execution profile set to {new_p.upper()}."
                    _redraw()
            elif view == "profile":
                if 0 <= sel < len(profile_keys):
                    next_sel = (sel + step) % len(profile_keys)
                    state.settings_selected_row = next_sel
                    _redraw()
            elif view.startswith("provider:"):
                p_name = view.split(":", 1)[1]
                prov = settings.providers.get(p_name)
                if prov:
                    if sel == 0:  # Status toggle
                        prov.enabled = not prov.enabled
                        state.settings_status_msg = f"{prov.display_name} is now {'ENABLED' if prov.enabled else 'DISABLED'}."
                        _redraw()
                    elif sel == 3:  # Cycle default model
                        if prov.models:
                            curr_idx = prov.models.index(prov.default_model) if prov.default_model in prov.models else 0
                            next_idx = (curr_idx + step) % len(prov.models)
                            prov.default_model = prov.models[next_idx]
                            state.settings_status_msg = f"Default model set to '{prov.default_model}'."
                            _redraw()
            elif view == "roles":
                if 0 <= sel < len(role_keys):
                    r_name = role_keys[sel]
                    curr_p = settings.roles.get(r_name, {}).get("provider", "ollama")
                    curr_idx = provider_keys.index(curr_p) if curr_p in provider_keys else 0
                    next_idx = (curr_idx + step) % len(provider_keys)
                    new_p = provider_keys[next_idx]
                    prov_obj = settings.providers.get(new_p)
                    new_m = prov_obj.default_model if prov_obj and prov_obj.default_model else (prov_obj.models[0] if prov_obj and prov_obj.models else "default")
                    new_m = sanitize_model_name(new_m, new_p)
                    settings.roles[r_name] = {"provider": new_p, "model": new_m}
                    display_name = "Q/A" if r_name.lower() in ("qa", "q/a") else r_name.title()
                    if hasattr(state, "roles") and display_name in state.roles:
                        state.roles[display_name].backend = new_p
                        state.roles[display_name].model = new_m
                    state.settings_status_msg = f"Bound '{display_name}' to {new_p} ({new_m})."
                    _redraw()
            continue

        if key in (Key.ENTER, Key.NEWLINE):
            if view in ("settings", "stackmind", "config"):
                if sel == 0:  # Profile: cycle next profile
                    curr_p = getattr(settings, "effective_profile", "medium").lower()
                    curr_idx = profile_keys.index(curr_p) if curr_p in profile_keys else 1
                    next_idx = (curr_idx + 1) % len(profile_keys)
                    new_p = profile_keys[next_idx]
                    settings.execution_profile = new_p
                    state.execution_profile = new_p
                    state.settings_status_msg = f"Execution profile set to {new_p.upper()}."
                    _redraw()
                elif sel == 1:  # Client turn timeout cycle
                    timeouts = [0, 30, 60, 120, 300]
                    curr_to = getattr(state, "turn_timeout", None)
                    if curr_to is None:
                        curr_to = getattr(settings, "client_timeout", 0)
                    curr_idx = timeouts.index(curr_to) if curr_to in timeouts else 0
                    next_idx = (curr_idx + 1) % len(timeouts)
                    new_to = timeouts[next_idx]
                    state.turn_timeout = new_to
                    settings.client_timeout = new_to
                    state.settings_status_msg = f"Turn timeout set to {new_to}s." if new_to else "Turn timeout disabled."
                    _redraw()
                elif sel == 2:  # Decision engine info
                    state.settings_status_msg = "Laya decision engine in deterministic fallback mode."
                    _redraw()
                elif sel == 3:  # Switch to LLM providers view
                    state.settings_current_view = "providers"
                    state.settings_selected_row = 0
                    state.settings_status_msg = ""
                    _redraw()
                elif sel == 4:  # Switch to Roles view
                    state.settings_current_view = "roles"
                    state.settings_selected_row = 0
                    state.settings_status_msg = ""
                    _redraw()
                elif sel == 5:  # Save & Exit
                    save_settings(settings, workspace)
                    if client and hasattr(client, "settings_update"):
                        try:
                            client.settings_update(settings.to_dict())
                        except Exception:
                            pass
                    state.close_settings()
                    _redraw()
                    return "[SUCCESS] Settings saved to settings.json"
            elif view == "providers":
                if 0 <= sel < num_prov:
                    state.settings_current_view = f"provider:{provider_keys[sel]}"
                    state.settings_selected_row = 0
                    state.settings_status_msg = ""
                    _redraw()
                elif sel == num_prov:  # ◄ Return to StackMind Settings
                    state.settings_current_view = "config" if initial_view in ("settings", "stackmind", "config") else "settings"
                    state.settings_selected_row = 3
                    state.settings_status_msg = ""
                    _redraw()
                elif sel == num_prov + 1:  # Save & Exit
                    save_settings(settings, workspace)
                    if client and hasattr(client, "settings_update"):
                        try:
                            client.settings_update(settings.to_dict())
                        except Exception:
                            pass
                    state.close_settings()
                    _redraw()
                    return "[SUCCESS] Settings saved to settings.json"
            elif view == "main":
                if 0 <= sel < num_prov:
                    state.settings_current_view = f"provider:{provider_keys[sel]}"
                    state.settings_selected_row = 0
                    state.settings_status_msg = ""
                    _redraw()
                elif sel == num_prov:
                    state.settings_current_view = "profile"
                    cur_prof = getattr(settings, "effective_profile", "medium").lower()
                    state.settings_selected_row = profile_keys.index(cur_prof) if cur_prof in profile_keys else 1
                    state.settings_status_msg = ""
                    _redraw()
                elif sel == num_prov + 1:
                    state.settings_current_view = "roles"
                    state.settings_selected_row = 0
                    state.settings_status_msg = ""
                    _redraw()
                elif sel == num_prov + 2:  # Save & Exit
                    save_settings(settings, workspace)
                    if client and hasattr(client, "settings_update"):
                        try:
                            client.settings_update(settings.to_dict())
                        except Exception:
                            pass
                    state.close_settings()
                    _redraw()
                    return "[SUCCESS] Settings saved to settings.json"
            elif view == "profile":
                if 0 <= sel < len(profile_keys):
                    chosen_prof = profile_keys[sel]
                    settings.execution_profile = chosen_prof
                    state.execution_profile = chosen_prof
                    save_settings(settings, workspace)
                    if client and hasattr(client, "settings_update"):
                        try:
                            client.settings_update(settings.to_dict())
                        except Exception:
                            pass
                    if is_profile_selector_mode:
                        state.close_settings()
                        _redraw()
                        return (
                            f"[SUCCESS] Execution profile updated to: {chosen_prof.upper()}\n"
                            f"Profile saved to settings.json and applied to runtime."
                        )
                    else:
                        if initial_view in ("settings", "stackmind"):
                            state.settings_current_view = "settings"
                            state.settings_selected_row = 0
                        else:
                            state.settings_current_view = "main"
                            state.settings_selected_row = num_prov
                        state.settings_status_msg = f"Profile set to {chosen_prof.upper()} and saved."
                        _redraw()
                elif sel == 3:  # Back or Cancel
                    if is_profile_selector_mode:
                        state.close_settings()
                        _redraw()
                        return "[INFO] Profile selection cancelled (no changes)."
                    else:
                        if initial_view in ("settings", "stackmind"):
                            state.settings_current_view = "settings"
                            state.settings_selected_row = 0
                        else:
                            state.settings_current_view = "main"
                            state.settings_selected_row = num_prov
                        state.settings_status_msg = ""
                        _redraw()
            elif view.startswith("provider:"):
                p_name = view.split(":", 1)[1]
                prov = settings.providers.get(p_name)
                if prov:
                    if sel == 0:
                        prov.enabled = not prov.enabled
                        state.settings_status_msg = f"{prov.display_name} is now {'ENABLED' if prov.enabled else 'DISABLED'}."
                        _redraw()
                    elif sel == 1:
                        val = _read_field_input(f"Enter API Key for {prov.display_name}: ", key_stream)
                        if val:
                            prov.api_key = val
                            prov.enabled = True
                            state.settings_status_msg = f"Updated API Key for {prov.display_name}."
                        _redraw()
                    elif sel == 2:
                        val = _read_field_input(f"Enter Base URL for {prov.display_name} [{prov.base_url}]: ", key_stream)
                        if val:
                            prov.base_url = val
                            state.settings_status_msg = f"Updated Base URL for {prov.display_name}."
                        _redraw()
                    elif sel == 3:
                        if prov.models:
                            curr_idx = prov.models.index(prov.default_model) if prov.default_model in prov.models else 0
                            next_idx = (curr_idx + 1) % len(prov.models)
                            prov.default_model = prov.models[next_idx]
                            state.settings_status_msg = f"Default model set to '{prov.default_model}'."
                        _redraw()
                    elif sel == 4:
                        val = _read_field_input(f"Enter model name to add to {prov.display_name}: ", key_stream)
                        if val:
                            clean_m = sanitize_model_name(val, prov.name)
                            if clean_m and clean_m not in prov.models:
                                prov.models.append(clean_m)
                            prov.default_model = clean_m
                            state.settings_status_msg = f"Added model '{clean_m}' and set as active."
                        _redraw()
                    elif sel == 5:
                        state.settings_status_msg = f"Testing connection to {prov.display_name}..."
                        _redraw()
                        ok, msg, disc = test_provider_connection(prov, timeout=10.0)
                        if disc:
                            for dm in disc:
                                clean_dm = sanitize_model_name(dm, prov.name)
                                if clean_dm and clean_dm not in prov.models:
                                    prov.models.append(clean_dm)
                        if ok and not prov.enabled:
                            prov.enabled = True
                        state.settings_status_msg = f"{'SUCCESS' if ok else 'FAILED'}: {msg}"
                        _redraw()
                    elif sel == 6:  # Back
                        if initial_view == "providers":
                            state.settings_current_view = "providers"
                        else:
                            state.settings_current_view = "main"
                        state.settings_selected_row = provider_keys.index(p_name) if p_name in provider_keys else 0
                        state.settings_status_msg = ""
                        _redraw()
            elif view == "roles":
                if sel == len(role_keys):  # Save & Exit (row 5)
                    save_settings(settings, workspace)
                    if client and hasattr(client, "settings_update"):
                        try:
                            client.settings_update(settings.to_dict())
                        except Exception:
                            pass
                    for r_name in role_keys:
                        r_cfg = settings.roles.get(r_name, {})
                        d_name = "Q/A" if r_name.lower() in ("qa", "q/a") else r_name.title()
                        if hasattr(state, "roles") and d_name in state.roles:
                            state.roles[d_name].backend = r_cfg.get("provider", state.roles[d_name].backend)
                            state.roles[d_name].model = r_cfg.get("model", state.roles[d_name].model)
                    if initial_view == "roles":
                        state.close_settings()
                        _redraw()
                        return "[SUCCESS] Agent role bindings saved to settings.json and applied to runtime."
                    elif initial_view in ("settings", "stackmind", "config"):
                        state.settings_current_view = "config" if initial_view in ("settings", "stackmind", "config") else "settings"
                        state.settings_selected_row = 4
                        state.settings_status_msg = "Role bindings saved to settings.json."
                        _redraw()
                    else:
                        state.settings_current_view = "main"
                        state.settings_selected_row = num_prov + 1
                        state.settings_status_msg = "Role bindings saved to settings.json."
                        _redraw()
                elif 0 <= sel < len(role_keys):
                    # Enter cycles through models of current provider
                    r_name = role_keys[sel]
                    r_cfg = settings.roles.get(r_name, {})
                    p_val = r_cfg.get("provider", "ollama")
                    m_val = r_cfg.get("model", "")
                    prov_obj = settings.providers.get(p_val)
                    m_list = prov_obj.models if prov_obj and prov_obj.models else []
                    display_name = "Q/A" if r_name.lower() in ("qa", "q/a") else r_name.title()
                    if m_list:
                        curr_idx = m_list.index(m_val) if m_val in m_list else -1
                        next_idx = (curr_idx + 1) % len(m_list)
                        new_m = sanitize_model_name(m_list[next_idx], p_val)
                        settings.roles[r_name] = {"provider": p_val, "model": new_m}
                        if hasattr(state, "roles") and display_name in state.roles:
                            state.roles[display_name].model = new_m
                        state.settings_status_msg = f"Role '{display_name}' model set to '{new_m}'."
                    else:
                        state.settings_status_msg = f"No catalog models for {p_val}."
                    _redraw()
            continue

        if key == Key.CTRL_C:
            raise KeyboardInterrupt()

        if key == Key.ESCAPE:
            if is_profile_selector_mode:
                state.close_settings()
                _redraw()
                return "[INFO] Profile selection cancelled (no changes)."
            elif initial_view == "roles":
                state.close_settings()
                _redraw()
                return "[INFO] Agent role bindings closed."
            elif view in ("settings", "stackmind", "config"):
                break
            elif view == "providers":
                if initial_view == "providers":
                    break
                else:
                    state.settings_current_view = "config" if initial_view in ("settings", "stackmind", "config") else "settings"
                    state.settings_selected_row = 3
                    state.settings_status_msg = ""
                    _redraw()
            elif view == "roles":
                if initial_view in ("settings", "stackmind", "config"):
                    state.settings_current_view = "config" if initial_view in ("settings", "stackmind", "config") else "settings"
                    state.settings_selected_row = 4
                    state.settings_status_msg = ""
                    _redraw()
                else:
                    state.settings_current_view = "main"
                    state.settings_selected_row = num_prov + 1
                    state.settings_status_msg = ""
                    _redraw()
            elif view != "main":
                if view == "profile":
                    state.settings_current_view = "config" if initial_view in ("settings", "stackmind", "config") else "main"
                    state.settings_selected_row = 0 if initial_view in ("settings", "stackmind", "config") else num_prov
                elif view.startswith("provider:"):
                    p_name = view.split(":", 1)[1]
                    state.settings_current_view = "providers" if initial_view == "providers" else "main"
                    state.settings_selected_row = provider_keys.index(p_name) if p_name in provider_keys else 0
                state.settings_status_msg = ""
                _redraw()
            else:
                break
            continue

    # Save and clean up on exit
    save_settings(settings, workspace)
    if client and hasattr(client, "settings_update"):
        try:
            client.settings_update(settings.to_dict())
        except Exception:
            pass
    state.close_settings()
    _redraw()
    if is_profile_selector_mode:
        return f"[INFO] Profile selector closed. Active profile: {getattr(settings, 'effective_profile', 'medium').upper()}."
    return "[SUCCESS] Settings closed and persisted to settings.json"


def render_profile_overview_str(
    settings: StackMindSettings | None = None,
    width: int = 80,
    *,
    force_color: bool | None = None,
    no_color: bool | None = None,
) -> str:
    """Render a clean text panel displaying the execution profiles and policy limits."""
    if settings is None:
        settings = load_settings()

    cur_prof = getattr(settings, "effective_profile", "medium").lower()
    box_w = max(40, min(width - 2, 88))

    content = Table.grid(expand=True, padding=(0, 0))
    content.add_row(Text("StackMind Execution Profiles & Skill Budgets\n", style="bold white"))

    profiles_data = [
        (
            "low",
            "LOW",
            "Fast & Minimal Context",
            "• Skills: 0 optional skills retrieved or injected.\n"
            "• Tokens: 0 token budget for optional skill context.\n"
            "• Policy: Direct execution; mandatory contracts & D024 verification gates active.",
            "#38bdf8",
        ),
        (
            "medium",
            "MEDIUM",
            "Balanced Quality & Speed (Default)",
            "• Skills: Up to 2 relevant eligible skills.\n"
            "• Tokens: 600-token budget with progressive compact summaries.\n"
            "• Policy: Balanced trade-off between latency, token cost, and accuracy.",
            "#4ade80",
        ),
        (
            "high",
            "HIGH",
            "Deep Guidance & Verification",
            "• Skills: Up to 5 relevant skills loaded progressively.\n"
            "• Tokens: 1500-token budget for full runbooks and reference patterns.\n"
            "• Policy: Extended verification depth and multi-angle validation.",
            "#c084fc",
        ),
    ]

    for p_id, p_label, p_summary, p_detail, p_color in profiles_data:
        is_active = (p_id == cur_prof)
        badge = " [ACTIVE]" if is_active else ""
        header = Text()
        header.append(f"[{p_label}]", style=f"bold {p_color}")
        header.append(badge, style="bold green" if is_active else "dim")
        header.append(f" — {p_summary}", style="bold white")
        content.add_row(header)
        content.add_row(Text(p_detail, style="dim #94a3b8"))
        content.add_row(Text(" "))

    content.add_row(
        Text(
            "Commands:\n"
            "  :config                Open interactive configuration panel\n"
            "  :config profile low|medium|high Set profile directly\n"
            "  :providers             Configure LLM providers and models",
            style="dim #64748b",
        )
    )

    panel = Panel(
        content,
        title="[bold #60a5fa] StackMind Execution Profiles [/]",
        subtitle=f"[dim #94a3b8] Current: {cur_prof.upper()} | Profile takes effect on next cycle [/]",
        border_style="#3b82f6",
        width=box_w,
        padding=(0, 1),
    )

    console = Console(record=True, width=box_w, force_terminal=force_color, no_color=no_color)
    console.print(panel)
    return console.export_text().rstrip()


def run_interactive_profile_selector(
    session: Mapping[str, Any],
    state: Any,
    client: Any | None = None,
    live_manager: Any | None = None,
    key_stream: Iterator[str] | None = None,
    workspace: Path | None = None,
    width: int | None = None,
    stream: Any = None,
) -> str:
    """Run interactive execution profile selection anchored below composer."""
    return run_arrow_navigation_settings_loop(
        session=session,
        state=state,
        client=client,
        live_manager=live_manager,
        key_stream=key_stream,
        workspace=workspace,
        width=width,
        stream=stream,
        initial_view="profile",
        is_profile_selector_mode=True,
    )


def run_interactive_config_panel(
    session: Mapping[str, Any],
    state: Any,
    client: Any | None = None,
    live_manager: Any | None = None,
    key_stream: Iterator[str] | None = None,
    workspace: Path | None = None,
    width: int | None = None,
    stream: Any = None,
) -> str:
    """Run interactive StackMind configuration panel anchored below composer (:config)."""
    return run_arrow_navigation_settings_loop(
        session=session,
        state=state,
        client=client,
        live_manager=live_manager,
        key_stream=key_stream,
        workspace=workspace,
        width=width,
        stream=stream,
        initial_view="config",
    )


run_interactive_stackmind_settings = run_interactive_config_panel


def run_interactive_providers_panel(
    session: Mapping[str, Any],
    state: Any,
    client: Any | None = None,
    live_manager: Any | None = None,
    key_stream: Iterator[str] | None = None,
    workspace: Path | None = None,
    width: int | None = None,
    stream: Any = None,
) -> str:
    """Run interactive Cloud & Local LLM providers configuration anchored below composer."""
    return run_arrow_navigation_settings_loop(
        session=session,
        state=state,
        client=client,
        live_manager=live_manager,
        key_stream=key_stream,
        workspace=workspace,
        width=width,
        stream=stream,
        initial_view="providers",
    )


def run_interactive_roles_panel(
    session: Mapping[str, Any],
    state: Any,
    client: Any | None = None,
    live_manager: Any | None = None,
    key_stream: Iterator[str] | None = None,
    workspace: Path | None = None,
    width: int | None = None,
    stream: Any = None,
) -> str:
    """Run interactive Agent Roles & Execution Backend Bindings panel anchored below composer (:roles)."""
    return run_arrow_navigation_settings_loop(
        session=session,
        state=state,
        client=client,
        live_manager=live_manager,
        key_stream=key_stream,
        workspace=workspace,
        width=width,
        stream=stream,
        initial_view="roles",
    )


