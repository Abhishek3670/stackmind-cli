"""Agent Contract verification and enforcement schema/engine (PLANv3 §1.1)."""

from __future__ import annotations

import json
import fnmatch
from pathlib import Path
from typing import Any, Union, Dict, List
import yaml
from jsonschema import Draft7Validator

class ContractValidationError(Exception):
    """Exception raised when contract validation fails."""
    pass

class ContractAccessDenied(PermissionError):
    """Exception raised when an agent attempts to access a node outside its contract."""
    pass

class ContractExpiredError(Exception):
    """Exception raised when a contract has expired."""
    pass

def path_to_module(path: str) -> str:
    """Convert a file path to a dot-separated module name."""
    p = path.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    elif p.startswith("/"):
        p = p[1:]
    if p.endswith(".py"):
        p = p[:-3]
    if p.endswith("/__init__"):
        p = p[:-9]
    return p.replace("/", ".")

def module_matches(module_name: str, pattern: str) -> bool:
    """Check if module_name matches the pattern (supporting wildcards like *)."""
    if "*" in pattern or "?" in pattern:
        return fnmatch.fnmatch(module_name, pattern)
    return module_name == pattern or module_name.startswith(pattern + ".")

class AgentContract:
    """Represents a validated agent contract enforcing identity, scope, and budget."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data
        self.agent_id: str = data["agent_id"]
        self.work_order: str = data["work_order"]
        self.identity: Dict[str, str] = data.get("identity", {})
        self.scope: Dict[str, Any] = data["scope"]
        self.budget: Dict[str, Any] = data["budget"]
        
        self.allow_rules: List[Dict[str, Any]] = self.scope.get("allow", [])
        self.deny_rules: List[Dict[str, Any]] = self.scope.get("deny", [])
        self.write_mode: str = self.scope.get("write", "read-only")

    @classmethod
    def load(cls, path_or_data: Union[str, Path, Dict[str, Any]], project_path: Path) -> AgentContract:
        """Load, validate, and initialize an AgentContract."""
        if isinstance(path_or_data, (str, Path)):
            path = Path(path_or_data)
            if not path.exists():
                raise FileNotFoundError(f"Contract file not found: {path}")
            with open(path, "r", encoding="utf-8") as f:
                raw_data = yaml.safe_load(f)
        else:
            raw_data = path_or_data

        if not isinstance(raw_data, dict):
            raise ContractValidationError("Contract data must be a dictionary")

        if "contract" in raw_data and isinstance(raw_data["contract"], dict):
            contract_data = raw_data["contract"]
        else:
            contract_data = raw_data

        # Load schema: check project override first, then package schema
        schema_path = None
        if project_path:
            cand = project_path / "schemas" / "contract.schema.json"
            if cand.exists():
                schema_path = cand
        if not schema_path:
            schema_path = Path(__file__).resolve().parents[2] / "schemas" / "contract.schema.json"
        if not schema_path.exists():
            raise FileNotFoundError(f"Contract schema not found: {schema_path}")
        with open(schema_path, "r", encoding="utf-8") as f:
            schema = json.load(f)

        validator = Draft7Validator(schema)
        errors = list(validator.iter_errors(contract_data))
        if errors:
            msgs = []
            for err in errors:
                json_path = ".".join(str(p) for p in err.absolute_path) or "(root)"
                msgs.append(f"{json_path}: {err.message}")
            raise ContractValidationError("Contract validation failed:\n" + "\n".join(msgs))

        return cls(contract_data)

    def is_expired(self, current_time_str: str | None = None) -> bool:
        """Check if the contract has expired based on expires_at budget limit."""
        from datetime import datetime, timezone
        expires_str = self.budget.get("expires_at")
        if not expires_str:
            return False
        
        # Normalize 'Z' to '+00:00' for compatibility across Python versions
        if expires_str.endswith("Z"):
            expires_str = expires_str[:-1] + "+00:00"
        try:
            expires_dt = datetime.fromisoformat(expires_str)
        except ValueError:
            return False

        if current_time_str:
            if current_time_str.endswith("Z"):
                current_time_str = current_time_str[:-1] + "+00:00"
            current_dt = datetime.fromisoformat(current_time_str)
        else:
            current_dt = datetime.now(timezone.utc)
            
        if expires_dt.tzinfo is not None and current_dt.tzinfo is None:
            current_dt = current_dt.replace(tzinfo=timezone.utc)
        elif expires_dt.tzinfo is None and current_dt.tzinfo is not None:
            current_dt = current_dt.replace(tzinfo=None)

        return current_dt > expires_dt

    def is_node_in_scope(self, node_id: str, ir: Any) -> bool:
        """Determine if a specific graph node is allowed under this contract's scope rules."""
        symbols_by_id = {s.node_id: s for s in ir.symbols}
        if node_id not in symbols_by_id:
            return False  # Node not found in graph, fail-closed
        
        target_symbol = symbols_by_id[node_id]
        target_module = path_to_module(target_symbol.path)
        target_qname = target_symbol.qualified_name
        
        # 0. Inherently denied nodes
        if target_symbol.kind == "DenyPlaceholder":
            return False
        
        # 1. Deny rules (precedence)
        for deny_rule in self.deny_rules:
            pattern = deny_rule.get("module")
            if pattern and (module_matches(target_module, pattern) or module_matches(target_qname, pattern)):
                return False
                
        # 2. Allow rules
        adj: Dict[str, set[str]] = {}
        for edge in ir.edges:
            if edge.source_id not in adj:
                adj[edge.source_id] = set()
            if edge.target_id not in adj:
                adj[edge.target_id] = set()
            adj[edge.source_id].add(edge.target_id)
            adj[edge.target_id].add(edge.source_id)
            
        for allow_rule in self.allow_rules:
            pattern = allow_rule.get("module")
            if not pattern:
                continue
            max_depth = allow_rule.get("depth", 0)
            
            # BFS up to depth
            queue = [(node_id, 0)]
            visited = {node_id}
            found = False
            
            while queue:
                curr_id, dist = queue.pop(0)
                curr_symbol = symbols_by_id.get(curr_id)
                if curr_symbol:
                    curr_module = path_to_module(curr_symbol.path)
                    curr_qname = curr_symbol.qualified_name
                    if module_matches(curr_module, pattern) or module_matches(curr_qname, pattern):
                        found = True
                        break
                
                if dist < max_depth:
                    for neighbor in adj.get(curr_id, []):
                        if neighbor not in visited:
                            visited.add(neighbor)
                            queue.append((neighbor, dist + 1))
                            
            if found:
                return True
                
        return False
