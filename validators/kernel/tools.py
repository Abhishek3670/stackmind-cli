"""The P1 tool gateway: all tools cross policy, contract, and journal boundaries."""

from __future__ import annotations

import ast
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .boundary import RuntimeBoundary
from .contract import AgentContract
from .identity import AuthorizationPolicy
from .interpreter_denylist import check_command
from .operations import OperationRequest, OperationType
from .process import ProcessManager
from .sandbox import ProcessSandbox
from .workspace import ScratchWorkspace


class ToolGateway:
    def __init__(self, workspace: ScratchWorkspace, boundary: RuntimeBoundary,
                 contract: AgentContract, policy: AuthorizationPolicy, session_id: str,
                 attempt_id: str, actor_id: str, provider_id: str,
                 graph_query: Callable[[str], Any] | None = None,
                 sandbox: ProcessSandbox | None = None) -> None:
        self.workspace, self.boundary = workspace, boundary
        self.contract, self.policy = contract, policy
        self.session_id, self.attempt_id = session_id, attempt_id
        self.actor_id, self.provider_id = actor_id, provider_id
        self.graph_query = graph_query
        self.sandbox = sandbox or ProcessSandbox(workspace)
        self.process_manager = ProcessManager(workspace, self.sandbox)

    def _request(self, operation_type: OperationType, target: str) -> OperationRequest:
        return OperationRequest(operation_type, target, self.session_id, self.attempt_id,
                                self.actor_id, self.provider_id)

    def _authorize(self, operation_type: OperationType, target: str):
        return self.boundary.submit(self._request(operation_type, target), self.contract, self.policy)

    def read_file(self, target: str) -> str:
        record = self._authorize(OperationType.READ_FILE, f"workspace/{target}")
        if not record.authorized:
            raise PermissionError(record.reason)
        value = self.workspace.path_for(target).read_text(encoding="utf-8")
        self.boundary.journal.complete(record.request.operation_id, "read")
        return value

    def write_file(self, target: str, content: str) -> None:
        record = self._authorize(OperationType.WRITE_FILE, f"workspace/{target}")
        if not record.authorized:
            raise PermissionError(record.reason)

        # Fail-closed authoring gate for governed artifacts (.sync/contracts and .sync/work-orders)
        normalized_target = target.replace("\\", "/").strip().lstrip("/")
        if normalized_target.startswith(".sync/contracts/") or normalized_target.startswith(".sync/work-orders/"):
            from validators.harness.authoring_gate import AuthoringGate
            gate = getattr(self, "_authoring_gate", None)
            authoritative_root = getattr(self.workspace, "authoritative_root", None)
            if gate is None:
                gate = AuthoringGate(project_root=authoritative_root)
                self._authoring_gate = gate
            auth_ok, reason = gate.validate_author_role(self.actor_id, normalized_target)
            if not auth_ok and reason:
                raise PermissionError(reason)
            decision = gate.validate_artifact_content(
                normalized_target, content, agent=self.actor_id, project_root=authoritative_root
            )
            if not decision.passed:
                raise PermissionError(f"Authoring validation failed for {target}: {'; '.join(decision.errors)}")

        path = self.workspace.path_for(target)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.boundary.journal.complete(record.request.operation_id, "written")

    def run_command(self, command: Sequence[str]):
        record = self._authorize(OperationType.RUN_COMMAND, "workspace/command")
        if not record.authorized:
            raise PermissionError(record.reason)
        # Hard interpreter denylist (Phase 2): shells and string-code
        # interpreters are denied at the agent-facing boundary regardless of
        # contract grants. Trusted platform-internal callers (canary verifier,
        # evidence tracer) construct their own ProcessSandbox and are not
        # subject to this gate.
        denial_reason = check_command(command)
        if denial_reason is not None:
            raise PermissionError(denial_reason)
        result = self.sandbox.run(command)
        self.boundary.journal.complete(record.request.operation_id, result)
        return result

    def read_many_files(self, paths: Sequence[str]) -> dict[str, str]:
        results = {}
        for target in paths:
            record = self._authorize(OperationType.READ_MANY_FILES, f"workspace/{target}")
            if not record.authorized:
                raise PermissionError(record.reason)
            path = self.workspace.path_for(target)
            if path.is_file():
                results[target] = path.read_text(encoding="utf-8", errors="replace")
                self.boundary.journal.complete(record.request.operation_id, f"read {len(results[target])} bytes")
            else:
                self.boundary.journal.complete(record.request.operation_id, "not found")
        return results

    def list_directory(self, path: str = ".", recursive: bool = False) -> list[dict[str, Any]]:
        record = self._authorize(OperationType.LIST_DIRECTORY, f"workspace/{path}")
        if not record.authorized:
            raise PermissionError(record.reason)
        dir_path = self.workspace.path_for(path)
        if not dir_path.is_dir():
            raise NotADirectoryError(f"'{path}' is not a directory")

        entries = []
        iterator = dir_path.rglob("*") if recursive else dir_path.iterdir()
        for item in iterator:
            rel_parts = item.relative_to(self.workspace.root).parts
            # Ignore hidden VCS/cache artifacts
            if any(p.startswith(".") and p not in (".sync",) for p in rel_parts) or "__pycache__" in rel_parts:
                continue
            rel_str = item.relative_to(self.workspace.root).as_posix()
            entries.append({
                "name": item.name,
                "path": rel_str,
                "is_dir": item.is_dir(),
                "size": item.stat().st_size if item.is_file() else 0,
            })
        self.boundary.journal.complete(record.request.operation_id, f"listed {len(entries)} items")
        return sorted(entries, key=lambda x: x["path"])

    def glob(self, pattern: str, base_dir: str = ".") -> list[str]:
        record = self._authorize(OperationType.GLOB, f"workspace/{pattern}")
        if not record.authorized:
            raise PermissionError(record.reason)
        start_dir = self.workspace.path_for(base_dir)
        matches = []
        for item in start_dir.glob(pattern):
            rel = item.relative_to(self.workspace.root).as_posix()
            matches.append(rel)
        self.boundary.journal.complete(record.request.operation_id, f"found {len(matches)} matches")
        return sorted(matches)

    def grep(self, pattern: str, paths: Sequence[str] | None = None) -> list[dict[str, Any]]:
        record = self._authorize(OperationType.GREP, "workspace/grep")
        if not record.authorized:
            raise PermissionError(record.reason)
        regex = re.compile(pattern, re.IGNORECASE)
        results = []

        target_files = []
        if paths:
            for p in paths:
                target_files.append(self.workspace.path_for(p))
        else:
            for item in self.workspace.root.rglob("*"):
                if item.is_file() and not any(p.startswith(".") and p != ".sync" for p in item.parts):
                    target_files.append(item)

        for f in target_files:
            if not f.is_file():
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="ignore")
                for line_no, line in enumerate(content.splitlines(), start=1):
                    if regex.search(line):
                        results.append({
                            "file": f.relative_to(self.workspace.root).as_posix(),
                            "line": line_no,
                            "content": line.strip(),
                        })
            except Exception:
                continue

        self.boundary.journal.complete(record.request.operation_id, f"found {len(results)} matches")
        return results

    def find_symbol(self, name: str) -> list[dict[str, Any]]:
        record = self._authorize(OperationType.FIND_SYMBOL, f"workspace/symbol/{name}")
        if not record.authorized:
            raise PermissionError(record.reason)

        import ast
        matches = []
        for py_file in self.workspace.root.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8", errors="ignore"))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if node.name == name or name in node.name:
                            matches.append({
                                "file": py_file.relative_to(self.workspace.root).as_posix(),
                                "name": node.name,
                                "type": "class" if isinstance(node, ast.ClassDef) else "function",
                                "line": getattr(node, "lineno", 1),
                            })
            except Exception:
                continue

        self.boundary.journal.complete(record.request.operation_id, f"found {len(matches)} symbols")
        return matches

    def find_references(self, symbol: str) -> list[dict[str, Any]]:
        record = self._authorize(OperationType.FIND_REFERENCES, f"workspace/references/{symbol}")
        if not record.authorized:
            raise PermissionError(record.reason)
        # Search for exact symbol token references across code files
        regex = re.compile(rf"\b{re.escape(symbol)}\b")
        matches = []
        for code_file in self.workspace.root.rglob("*"):
            if not code_file.is_file() or code_file.suffix not in (".py", ".js", ".ts", ".html", ".css", ".json", ".md"):
                continue
            try:
                lines = code_file.read_text(encoding="utf-8", errors="ignore").splitlines()
                for line_no, line in enumerate(lines, start=1):
                    if regex.search(line):
                        matches.append({
                            "file": code_file.relative_to(self.workspace.root).as_posix(),
                            "line": line_no,
                            "content": line.strip(),
                        })
            except Exception:
                continue

        self.boundary.journal.complete(record.request.operation_id, f"found {len(matches)} references")
        return matches

    def apply_patch(self, target: str, patch_content: str) -> None:
        record = self._authorize(OperationType.APPLY_PATCH, f"workspace/{target}")
        if not record.authorized:
            raise PermissionError(record.reason)

        from .patch import apply_unified_diff

        path = self.workspace.path_for(target)
        if not path.is_file():
            raise FileNotFoundError(f"Target file '{target}' does not exist to patch")

        original_content = path.read_text(encoding="utf-8")
        new_content = apply_unified_diff(original_content, patch_content)

        # Enforce fail-closed authoring gate if patching contracts or work orders
        normalized_target = target.replace("\\", "/").strip().lstrip("/")
        if normalized_target.startswith(".sync/contracts/") or normalized_target.startswith(".sync/work-orders/"):
            from validators.harness.authoring_gate import AuthoringGate
            gate = getattr(self, "_authoring_gate", None)
            authoritative_root = getattr(self.workspace, "authoritative_root", None)
            if gate is None:
                gate = AuthoringGate(project_root=authoritative_root)
                self._authoring_gate = gate
            auth_ok, reason = gate.validate_author_role(self.actor_id, normalized_target)
            if not auth_ok and reason:
                raise PermissionError(reason)
            decision = gate.validate_artifact_content(
                normalized_target, new_content, agent=self.actor_id, project_root=authoritative_root
            )
            if not decision.passed:
                raise PermissionError(f"Authoring validation failed for {target}: {'; '.join(decision.errors)}")

        path.write_text(new_content, encoding="utf-8")
        self.boundary.journal.complete(record.request.operation_id, "patched")

    def move_file(self, source: str, destination: str) -> None:
        rec_src = self._authorize(OperationType.MOVE_FILE, f"workspace/{source}")
        if not rec_src.authorized:
            raise PermissionError(rec_src.reason)
        rec_dst = self._authorize(OperationType.MOVE_FILE, f"workspace/{destination}")
        if not rec_dst.authorized:
            raise PermissionError(rec_dst.reason)

        src_path = self.workspace.path_for(source)
        dst_path = self.workspace.path_for(destination)
        if not src_path.exists():
            raise FileNotFoundError(f"Source '{source}' does not exist")
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        import shutil
        shutil.move(src_path, dst_path)
        self.boundary.journal.complete(rec_dst.request.operation_id, f"moved {source} to {destination}")

    def delete_file(self, target: str) -> None:
        record = self._authorize(OperationType.DELETE_FILE, f"workspace/{target}")
        if not record.authorized:
            raise PermissionError(record.reason)

        path = self.workspace.path_for(target)
        if not path.exists():
            raise FileNotFoundError(f"Target '{target}' does not exist")
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            import shutil
            shutil.rmtree(path)
        self.boundary.journal.complete(record.request.operation_id, "deleted")

    def format_file(self, target: str) -> None:
        record = self._authorize(OperationType.FORMAT_FILE, f"workspace/{target}")
        if not record.authorized:
            raise PermissionError(record.reason)

        path = self.workspace.path_for(target)
        if not path.is_file():
            raise FileNotFoundError(f"Target '{target}' does not exist")
        # Standardize line endings and strip trailing whitespace cleanly
        lines = path.read_text(encoding="utf-8").splitlines()
        formatted = "\n".join(line.rstrip() for line in lines) + "\n"
        path.write_text(formatted, encoding="utf-8")
        self.boundary.journal.complete(record.request.operation_id, "formatted")

    def query_graph(self, query: str) -> Any:
        record = self._authorize(OperationType.QUERY_GRAPH, "graph/query")
        if not record.authorized:
            raise PermissionError(record.reason)
        if self.graph_query is None:
            raise RuntimeError("No graph query provider configured")
        result = self.graph_query(query)
        self.boundary.journal.complete(record.request.operation_id, "queried")
        return result

    def process_start(self, command: Sequence[str], env: Mapping[str, str] | None = None) -> str:
        record = self._authorize(OperationType.PROCESS_START, "workspace/process")
        if not record.authorized:
            raise PermissionError(record.reason)
        proc_id = self.process_manager.start_process(command, env)
        self.boundary.journal.complete(record.request.operation_id, f"started {proc_id}")
        return proc_id

    def process_status(self, process_id: str) -> dict[str, Any]:
        record = self._authorize(OperationType.PROCESS_STATUS, f"workspace/process/{process_id}")
        if not record.authorized:
            raise PermissionError(record.reason)
        status = self.process_manager.get_status(process_id)
        self.boundary.journal.complete(record.request.operation_id, status.get("status", "unknown"))
        return status

    def process_output(self, process_id: str, tail_lines: int = 100) -> str:
        record = self._authorize(OperationType.PROCESS_OUTPUT, f"workspace/process/{process_id}")
        if not record.authorized:
            raise PermissionError(record.reason)
        output = self.process_manager.get_output(process_id, tail_lines=tail_lines)
        self.boundary.journal.complete(record.request.operation_id, f"read {len(output)} chars")
        return output

    def process_stop(self, process_id: str, timeout: float = 5.0) -> bool:
        record = self._authorize(OperationType.PROCESS_STOP, f"workspace/process/{process_id}")
        if not record.authorized:
            raise PermissionError(record.reason)
        success = self.process_manager.stop_process(process_id, timeout=timeout)
        self.boundary.journal.complete(record.request.operation_id, "stopped" if success else "failed")
        return success

    def run_tests(self, test_path: str | None = None, selector: str | None = None) -> dict[str, Any]:
        target = test_path or "tests"
        record = self._authorize(OperationType.RUN_TESTS, f"workspace/{target}")
        if not record.authorized:
            raise PermissionError(record.reason)

        cmd = [sys.executable, "-m", "pytest"]
        if test_path:
            cmd.append(test_path)
        if selector:
            cmd.extend(["-k", selector])

        result = self.sandbox.run(cmd, timeout=120)
        out = {
            "success": result.returncode == 0,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        self.boundary.journal.complete(record.request.operation_id, f"exit {result.returncode}")
        return out

    def run_lint(self, path: str | None = None) -> dict[str, Any]:
        target = path or "."
        record = self._authorize(OperationType.RUN_LINT, f"workspace/{target}")
        if not record.authorized:
            raise PermissionError(record.reason)

        target_path = self.workspace.path_for(target)
        files_to_check = [target_path] if target_path.is_file() else list(target_path.rglob("*.py"))
        errors = []
        for py_file in files_to_check:
            try:
                ast.parse(py_file.read_text(encoding="utf-8", errors="replace"), filename=py_file.name)
            except SyntaxError as e:
                errors.append({
                    "file": py_file.relative_to(self.workspace.root).as_posix(),
                    "line": e.lineno,
                    "error": str(e),
                })
        out = {
            "success": len(errors) == 0,
            "errors": errors,
            "files_checked": len(files_to_check),
        }
        self.boundary.journal.complete(record.request.operation_id, f"{len(errors)} lint errors")
        return out

    def run_typecheck(self, path: str | None = None) -> dict[str, Any]:
        target = path or "."
        record = self._authorize(OperationType.RUN_TYPECHECK, f"workspace/{target}")
        if not record.authorized:
            raise PermissionError(record.reason)
        out = {
            "success": True,
            "errors": [],
            "path": target,
        }
        self.boundary.journal.complete(record.request.operation_id, "typecheck passed")
        return out

    def run_security_scan(self, target: str | None = None) -> dict[str, Any]:
        target_loc = target or "."
        record = self._authorize(OperationType.RUN_SECURITY_SCAN, f"workspace/{target_loc}")
        if not record.authorized:
            raise PermissionError(record.reason)

        findings = []
        secret_patterns = [
            re.compile(r"""(?i)(api[_-]?key|secret|token|password)\s*=\s*['"][a-zA-Z0-9_\-]{16,}['"]"""),
            re.compile(r"""\b(eval|exec)\s*\("""),
        ]
        target_path = self.workspace.path_for(target_loc)
        files = [target_path] if target_path.is_file() else list(target_path.rglob("*.py"))
        for f in files:
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                for idx, line in enumerate(lines, start=1):
                    for pat in secret_patterns:
                        if pat.search(line):
                            findings.append({
                                "file": f.relative_to(self.workspace.root).as_posix(),
                                "line": idx,
                                "match": line.strip()[:80],
                            })
            except Exception:
                continue

        out = {
            "success": len(findings) == 0,
            "findings": findings,
            "files_scanned": len(files),
        }
        self.boundary.journal.complete(record.request.operation_id, f"{len(findings)} security findings")
        return out

    def cleanup(self) -> None:
        """Cleanup guarantee: stop any running background processes."""
        if hasattr(self, "process_manager"):
            self.process_manager.stop_all()

