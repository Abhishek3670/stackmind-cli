"""CLI commands for inspecting, validating, and explaining agent contracts (PLANv3 §1.5)."""

from __future__ import annotations

from pathlib import Path
import click
from validators.knowledge.contract import AgentContract, path_to_module, module_matches

def find_contract(contract_or_id: str, project_path: Path) -> Path:
    """Resolve a contract or work order ID to a file path."""
    p = Path(contract_or_id)
    if p.exists():
        return p
    p_abs = project_path / contract_or_id
    if p_abs.exists():
        return p_abs
        
    search_dirs = [
        project_path / ".sync" / "contracts",
        project_path / ".sync" / "work-orders" / "ACTIVE",
        project_path / ".sync" / "work-orders" / "COMPLETED",
        project_path,
    ]
    for dir_path in search_dirs:
        if not dir_path.exists():
            continue
        for filename in [contract_or_id, f"{contract_or_id}.yaml", f"{contract_or_id}.json"]:
            candidate = dir_path / filename
            if candidate.exists():
                return candidate
                
    raise FileNotFoundError(f"Could not find contract file for target: {contract_or_id}")

@click.group("contract")
def contract_group():
    """Inspect and validate agent contracts."""
    pass

@contract_group.command("show")
@click.argument("contract_or_wo_id")
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project path",
)
def show_command(contract_or_wo_id: str, project_path: str):
    """Show details and validation status of a contract."""
    from rich.console import Console
    from rich.table import Table

    console = Console()
    project = Path(project_path).resolve()
    try:
        path = find_contract(contract_or_wo_id, project)
        contract = AgentContract.load(path, project)
        
        console.print(f"[bold green][PASS] Contract is valid[/bold green] (loaded from {path.name})")
        console.print(f"[bold]Agent ID:[/bold] {contract.agent_id}")
        console.print(f"[bold]Work Order ID:[/bold] {contract.work_order}")
        console.print(f"[bold]Write Mode:[/bold] {contract.write_mode}")
        
        if contract.identity:
            console.print("[bold]Identity:[/bold]")
            for k, v in contract.identity.items():
                console.print(f"  {k}: {v}")
                
        if contract.budget:
            console.print("[bold]Budget:[/bold]")
            for k, v in contract.budget.items():
                console.print(f"  {k}: {v}")
                
        if contract.allow_rules:
            table = Table(title="Allow Rules")
            table.add_column("Module Pattern")
            table.add_column("Depth")
            for rule in contract.allow_rules:
                table.add_row(rule.get("module"), str(rule.get("depth", 0)))
            console.print(table)
            
        if contract.deny_rules:
            deny_table = Table(title="Deny Rules")
            deny_table.add_column("Module Pattern")
            for rule in contract.deny_rules:
                deny_table.add_row(rule.get("module"))
            console.print(deny_table)
    except Exception as e:
        console.print(f"[bold red][FAIL] Contract error: {e}[/bold red]")
        raise SystemExit(1)

@contract_group.command("validate")
@click.argument("contract_or_wo_id")
@click.option(
    "--op",
    required=True,
    help="Operation to validate (e.g., 'edit billing/invoices.py', 'read auth.session')",
)
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project path",
)
def validate_command(contract_or_wo_id: str, op: str, project_path: str):
    """Validate if an operation is allowed by the contract."""
    from rich.console import Console
    console = Console()
    project = Path(project_path).resolve()
    try:
        path = find_contract(contract_or_wo_id, project)
        contract = AgentContract.load(path, project)
        
        if contract.is_expired():
            console.print(f"[bold red][REJECTED] Contract {contract.work_order} has expired.[/bold red]")
            raise SystemExit(1)
            
        op = op.strip()
        parts = op.split(" ", 1)
        action = parts[0].lower()
        target = parts[1] if len(parts) > 1 else ""
        
        if action in ("edit", "write"):
            if contract.write_mode == "read-only":
                console.print(f"[bold red][REJECTED] Contract {contract.work_order} is read-only.[/bold red]")
                raise SystemExit(1)
            
            from validators.knowledge.api import KnowledgeAPI
            api = KnowledgeAPI(project)
            mod = path_to_module(target)
            
            denied = False
            for deny_rule in contract.deny_rules:
                pattern = deny_rule.get("module")
                if pattern and module_matches(mod, pattern):
                    denied = True
                    break
            if denied:
                console.print(f"[bold red][REJECTED] Module {mod} matches deny rules.[/bold red]")
                raise SystemExit(1)
                
            allowed = False
            module_nodes = [s for s in api.ir.symbols if path_to_module(s.path) == mod]
            if module_nodes:
                allowed = any(contract.is_node_in_scope(s.node_id, api.ir) for s in module_nodes)
            else:
                for allow_rule in contract.allow_rules:
                    pattern = allow_rule.get("module")
                    if pattern and module_matches(mod, pattern):
                        allowed = True
                        break
            if allowed:
                console.print(f"[bold green][ALLOWED] Edit {target} is allowed under contract {contract.work_order}.[/bold green]")
            else:
                console.print(f"[bold red][REJECTED] Module {mod} is outside of allow scope.[/bold red]")
                raise SystemExit(1)
        elif action in ("read", "query"):
            from validators.knowledge.api import KnowledgeAPI
            api = KnowledgeAPI(project)
            from validators.knowledge.contract import ContractAccessDenied
            try:
                envelope = api.lookup(target)
                if not envelope.results:
                    envelope = api.search(target, limit=1)
                if not envelope.results:
                    console.print(f"[bold yellow][WARNING] Symbol '{target}' not found in graph. Checking raw module name...[/bold yellow]")
                    mod = path_to_module(target)
                    allowed = False
                    for allow_rule in contract.allow_rules:
                        pattern = allow_rule.get("module")
                        if pattern and module_matches(mod, pattern):
                            allowed = True
                            break
                    if allowed:
                        console.print(f"[bold green][ALLOWED] Read {target} is allowed under contract {contract.work_order}.[/bold green]")
                    else:
                        console.print(f"[bold red][REJECTED] Module {mod} is outside of allow scope.[/bold red]")
                        raise SystemExit(1)
                    return
                    
                node_id = envelope.results[0].node_id
                if contract.is_node_in_scope(node_id, api.ir):
                    console.print(f"[bold green][ALLOWED] Read {target} ({node_id}) is allowed under contract {contract.work_order}.[/bold green]")
                else:
                    console.print(f"[bold red][REJECTED] Read {target} ({node_id}) is outside of allow scope.[/bold red]")
                    raise SystemExit(1)
            except ContractAccessDenied:
                console.print(f"[bold red][REJECTED] Read {target} is denied by contract.[/bold red]")
                raise SystemExit(1)
        else:
            console.print(f"[bold red]Unknown action: {action}. Supported: edit, read[/bold red]")
            raise SystemExit(1)
    except Exception as e:
        console.print(f"[bold red][FAIL] Validation error: {e}[/bold red]")
        raise SystemExit(1)

@click.command("explain-denial")
@click.argument("contract_or_wo_id")
@click.option("--node", required=True, help="Node ID or qualified name to explain")
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project path",
)
def explain_denial_command(contract_or_wo_id: str, node: str, project_path: str):
    """Explain why a specific node was denied access by the contract."""
    from rich.console import Console
    console = Console()
    project = Path(project_path).resolve()
    try:
        path = find_contract(contract_or_wo_id, project)
        contract = AgentContract.load(path, project)
        
        from validators.knowledge.api import KnowledgeAPI
        api = KnowledgeAPI(project)
        
        envelope = api.lookup(node)
        if not envelope.results:
            envelope = api.search(node, limit=1)
        if not envelope.results:
            console.print(f"[bold red]Node '{node}' not found in the knowledge base.[/bold red]")
            raise SystemExit(1)
            
        node_id = envelope.results[0].node_id
        symbol = next(s for s in api.ir.symbols if s.node_id == node_id)
        mod = path_to_module(symbol.path)
        qname = symbol.qualified_name
        
        console.print(f"[bold]Target Symbol:[/bold] {qname} ({node_id})")
        console.print(f"[bold]Resolved Module:[/bold] {mod}")
        
        denied_by = []
        for deny_rule in contract.deny_rules:
            pattern = deny_rule.get("module")
            if pattern and (module_matches(mod, pattern) or module_matches(qname, pattern)):
                denied_by.append(deny_rule)
                
        if denied_by:
            console.print("[bold red][DENIED] Access is explicitly denied by rule(s):[/bold red]")
            for rule in denied_by:
                console.print(f"  - deny: {rule.get('module')}")
            return
            
        in_scope = contract.is_node_in_scope(node_id, api.ir)
        if in_scope:
            console.print("[bold green][ALLOWED] Node is in scope under allow rules.[/bold green]")
        else:
            console.print("[bold red][DENIED] Fail-closed: Node matches no allow rules.[/bold red]")
            console.print("Active allow rules:")
            for rule in contract.allow_rules:
                console.print(f"  - module: {rule.get('module')} (depth: {rule.get('depth', 0)})")
    except Exception as e:
        console.print(f"[bold red][FAIL] Explanation error: {e}[/bold red]")
        raise SystemExit(1)

@click.command("scope")
@click.argument("agent_id_or_contract")
@click.option(
    "--project",
    "-p",
    "project_path",
    default=".",
    type=click.Path(exists=True),
    help="Project path",
)
def scope_command(agent_id_or_contract: str, project_path: str):
    """Show the full scope boundary (allowed modules) for an agent or contract."""
    from rich.console import Console
    from rich.table import Table
    console = Console()
    project = Path(project_path).resolve()
    try:
        try:
            path = find_contract(agent_id_or_contract, project)
        except FileNotFoundError:
            found_path = None
            search_dirs = [
                project / ".sync" / "contracts",
                project / ".sync" / "work-orders" / "ACTIVE",
            ]
            for dir_path in search_dirs:
                if not dir_path.exists():
                    continue
                for file_path in dir_path.glob("*.yaml"):
                    try:
                        c = AgentContract.load(file_path, project)
                        if c.agent_id == agent_id_or_contract:
                            found_path = file_path
                            break
                    except Exception:
                        pass
                if found_path:
                    break
            if found_path:
                path = found_path
            else:
                raise FileNotFoundError(f"Could not resolve scope for: {agent_id_or_contract}")

        contract = AgentContract.load(path, project)
        console.print(f"[bold]Agent ID:[/bold] {contract.agent_id}")
        console.print(f"[bold]Work Order:[/bold] {contract.work_order}")
        console.print(f"[bold]Write Mode:[/bold] {contract.write_mode}")
        
        table = Table(title="Allowed Module Boundaries")
        table.add_column("Module Pattern")
        table.add_column("Depth")
        for rule in contract.allow_rules:
            table.add_row(rule.get("module"), str(rule.get("depth", 0)))
        console.print(table)
        
        if contract.deny_rules:
            deny_table = Table(title="Denied Module Boundaries")
            deny_table.add_column("Module Pattern")
            for rule in contract.deny_rules:
                deny_table.add_row(rule.get("module"))
            console.print(deny_table)
    except Exception as e:
        console.print(f"[bold red][FAIL] Scope query error: {e}[/bold red]")
        raise SystemExit(1)
