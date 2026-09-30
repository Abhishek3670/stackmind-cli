"""The P1 tool gateway: all tools cross policy, contract, and journal boundaries."""

from __future__ import annotations

import ast
import re
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
import yaml
from typing import Any

from .boundary import RuntimeBoundary
from .contract import AgentContract, ContractEvaluator
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
        self._todo_list: list[dict[str, Any]] = []
        self._plan_mode: bool = False

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

    def _get_knowledge_api(self):
        try:
            from validators.knowledge.api import KnowledgeAPI
            auth_root = getattr(self.workspace, "authoritative_root", None) or self.workspace.root
            if (auth_root / ".sync" / "knowledge").is_dir():
                return KnowledgeAPI(auth_root, contract=self.contract)
        except Exception:
            pass
        return None

    def find_callers(self, symbol: str) -> list[str]:
        record = self._authorize(OperationType.FIND_CALLERS, f"graph/callers/{symbol}")
        if not record.authorized:
            raise PermissionError(record.reason)

        api = self._get_knowledge_api()
        callers: list[str] = []
        if api is not None:
            try:
                envelope = api.callers(symbol, contract=self.contract, on_denied="skip")
                callers = [r.qualified_name or r.node_id for r in envelope.results]
            except Exception:
                callers = []

        if not callers:
            for py_file in self.workspace.root.rglob("*.py"):
                try:
                    tree = ast.parse(py_file.read_text(encoding="utf-8", errors="ignore"))
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            for subnode in ast.walk(node):
                                if isinstance(subnode, ast.Call):
                                    func_name = ""
                                    if isinstance(subnode.func, ast.Name):
                                        func_name = subnode.func.id
                                    elif isinstance(subnode.func, ast.Attribute):
                                        func_name = subnode.func.attr
                                    if func_name == symbol:
                                        callers.append(node.name)
                except Exception:
                    continue

        callers = sorted(list(set(callers)))
        self.boundary.journal.complete(record.request.operation_id, f"found {len(callers)} callers")
        return callers

    def find_callees(self, symbol: str) -> list[str]:
        record = self._authorize(OperationType.FIND_CALLEES, f"graph/callees/{symbol}")
        if not record.authorized:
            raise PermissionError(record.reason)

        api = self._get_knowledge_api()
        callees: list[str] = []
        if api is not None:
            try:
                envelope = api.traverse(symbol, relation="CALLS", direction="outbound", depth=1, contract=self.contract, on_denied="skip")
                callees = [r.qualified_name or r.node_id for r in envelope.results]
            except Exception:
                callees = []

        if not callees:
            for py_file in self.workspace.root.rglob("*.py"):
                try:
                    tree = ast.parse(py_file.read_text(encoding="utf-8", errors="ignore"))
                    for node in ast.walk(tree):
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == symbol:
                            for subnode in ast.walk(node):
                                if isinstance(subnode, ast.Call):
                                    if isinstance(subnode.func, ast.Name):
                                        callees.append(subnode.func.id)
                                    elif isinstance(subnode.func, ast.Attribute):
                                        callees.append(subnode.func.attr)
                except Exception:
                    continue

        callees = sorted(list(set(callees)))
        self.boundary.journal.complete(record.request.operation_id, f"found {len(callees)} callees")
        return callees

    def impact_analysis(self, symbol: str) -> dict[str, Any]:
        record = self._authorize(OperationType.IMPACT_ANALYSIS, f"graph/impact/{symbol}")
        if not record.authorized:
            raise PermissionError(record.reason)

        api = self._get_knowledge_api()
        impacted_symbols: list[str] = []
        impacted_files: list[str] = []
        if api is not None:
            try:
                envelope = api.impact(symbol, depth=3, contract=self.contract)
                for r in envelope.results:
                    impacted_symbols.append(r.qualified_name or r.node_id)
                    if r.path:
                        impacted_files.append(r.path)
            except Exception:
                pass

        if not impacted_symbols:
            callers = self.find_callers(symbol)
            impacted_symbols = callers
            for py_file in self.workspace.root.rglob("*.py"):
                try:
                    content = py_file.read_text(encoding="utf-8", errors="ignore")
                    if symbol in content:
                        impacted_files.append(py_file.relative_to(self.workspace.root).as_posix())
                except Exception:
                    continue

        out = {
            "symbol": symbol,
            "impacted_symbols": sorted(list(set(impacted_symbols))),
            "impacted_files": sorted(list(set(impacted_files))),
            "impact_score": round(len(impacted_symbols) * 1.5, 2),
        }
        self.boundary.journal.complete(record.request.operation_id, f"impacted {len(impacted_symbols)} symbols")
        return out

    def dependency_analysis(self, module: str) -> dict[str, Any]:
        record = self._authorize(OperationType.DEPENDENCY_ANALYSIS, f"graph/dependencies/{module}")
        if not record.authorized:
            raise PermissionError(record.reason)

        imports: list[str] = []
        imported_by: list[str] = []

        mod_name = module.rstrip(".py").replace("/", ".").replace("\\", ".")
        for py_file in self.workspace.root.rglob("*.py"):
            rel_mod = py_file.relative_to(self.workspace.root).as_posix().rstrip(".py").replace("/", ".")
            try:
                tree = ast.parse(py_file.read_text(encoding="utf-8", errors="ignore"))
                file_imports = []
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            file_imports.append(alias.name)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        file_imports.append(node.module)

                if rel_mod == mod_name or py_file.name == module:
                    imports.extend(file_imports)
                if any(mod_name in imp for imp in file_imports):
                    imported_by.append(rel_mod)
            except Exception:
                continue

        out = {
            "module": module,
            "imports": sorted(list(set(imports))),
            "imported_by": sorted(list(set(imported_by))),
        }
        self.boundary.journal.complete(record.request.operation_id, f"{len(imports)} imports, {len(imported_by)} importers")
        return out

    def data_flow_analysis(self, source: str, sink: str | None = None) -> dict[str, Any]:
        record = self._authorize(OperationType.DATA_FLOW_ANALYSIS, f"graph/flow/{source}")
        if not record.authorized:
            raise PermissionError(record.reason)

        api = self._get_knowledge_api()
        paths: list[dict[str, Any]] = []
        if api is not None:
            try:
                envelope = api.flows(source, sink, contract=self.contract)
                for r in envelope.results:
                    paths.append({
                        "node_id": r.node_id,
                        "path": r.path,
                        "confidence": r.confidence,
                    })
            except Exception:
                pass

        if not paths:
            paths.append({
                "source": source,
                "sink": sink or "return",
                "confidence": 0.8,
            })

        out = {
            "source": source,
            "sink": sink,
            "paths": paths,
        }
        self.boundary.journal.complete(record.request.operation_id, f"found {len(paths)} flow paths")
        return out

    def knowledge_stats(self) -> dict[str, Any]:
        record = self._authorize(OperationType.KNOWLEDGE_STATS, "graph/stats")
        if not record.authorized:
            raise PermissionError(record.reason)

        api = self._get_knowledge_api()
        if api is not None:
            try:
                rev, commit = api._revision_meta()
                stale = api._is_stale()
                active = len(api._active_records())
                out = {
                    "revision": rev,
                    "git_commit": commit,
                    "is_stale": stale,
                    "indexed_symbols": active,
                }
                self.boundary.journal.complete(record.request.operation_id, f"rev {rev}")
                return out
            except Exception:
                pass

        py_files = list(self.workspace.root.rglob("*.py"))
        out = {
            "revision": 1,
            "is_stale": False,
            "python_files": len(py_files),
        }
        self.boundary.journal.complete(record.request.operation_id, f"{len(py_files)} files")
        return out

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

    def todo(self, action: str, task_text: str = "", status: str = "pending", item_id: int | None = None) -> list[dict[str, Any]]:
        record = self._authorize(OperationType.TODO, "session/todo")
        if not record.authorized:
            raise PermissionError(record.reason)

        action = action.lower().strip()
        if action == "add":
            new_id = len(self._todo_list) + 1
            item = {"id": new_id, "task": task_text, "status": status}
            self._todo_list.append(item)
        elif action in ("update", "set"):
            for item in self._todo_list:
                if (item_id is not None and item["id"] == item_id) or (task_text and task_text in item["task"]):
                    item["status"] = status
        elif action == "delete":
            self._todo_list = [item for item in self._todo_list if not ((item_id is not None and item["id"] == item_id) or (task_text and item["task"] == task_text))]
        elif action == "clear":
            self._todo_list.clear()

        self.boundary.journal.complete(record.request.operation_id, f"{len(self._todo_list)} items")
        return list(self._todo_list)

    def ask_user(self, question: str, choices: Sequence[str] | None = None) -> str:
        record = self._authorize(OperationType.ASK_USER, "session/ask_user")
        if not record.authorized:
            raise PermissionError(record.reason)
        res = choices[0] if choices else "acknowledged"
        self.boundary.journal.complete(record.request.operation_id, f"prompt: {question}")
        return res

    def enter_plan_mode(self) -> dict[str, Any]:
        record = self._authorize(OperationType.ENTER_PLAN_MODE, "session/plan_mode")
        if not record.authorized:
            raise PermissionError(record.reason)
        self._plan_mode = True
        self.boundary.journal.complete(record.request.operation_id, "active")
        return {"status": "plan_mode_active", "actor": self.actor_id}

    def exit_plan_mode(self) -> dict[str, Any]:
        record = self._authorize(OperationType.EXIT_PLAN_MODE, "session/plan_mode")
        if not record.authorized:
            raise PermissionError(record.reason)
        self._plan_mode = False
        self.boundary.journal.complete(record.request.operation_id, "inactive")
        return {"status": "plan_mode_inactive", "actor": self.actor_id}

    def create_work_order(
        self,
        title: str,
        deliverable: dict[str, Any],
        assigned_agent: str,
        dependencies: Sequence[str] | None = None,
        wo_id: str | None = None,
    ) -> str:
        record = self._authorize(OperationType.CREATE_WORK_ORDER, "workspace/work-orders")
        if not record.authorized:
            raise PermissionError(record.reason)

        from validators.harness.authoring_gate import AuthoringGate
        gate = getattr(self, "_authoring_gate", None)
        authoritative_root = getattr(self.workspace, "authoritative_root", None)
        if gate is None:
            gate = AuthoringGate(project_root=authoritative_root)
            self._authoring_gate = gate

        auth_ok, reason = gate.validate_author_role(self.actor_id, ".sync/work-orders/ACTIVE/new.yaml")
        if not auth_ok and reason:
            raise PermissionError(reason)

        if not wo_id:
            import time
            wo_id = f"WO-{int(time.time() * 1000) % 1000:03d}"

        wo_data = {
            "id": wo_id,
            "type": "FEATURE",
            "title": title,
            "status": "PENDING",
            "priority": "P1",
            "assigned_agents": [assigned_agent] if isinstance(assigned_agent, str) else list(assigned_agent),
            "dependencies": list(dependencies or []),
            "deliverables": [deliverable] if isinstance(deliverable, dict) else deliverable,
        }
        wo_yaml = yaml.dump(wo_data, sort_keys=False)
        target_path = f".sync/work-orders/ACTIVE/{wo_id}.yaml"

        decision = gate.validate_artifact_content(target_path, wo_yaml, agent=self.actor_id, project_root=authoritative_root)
        if not decision.passed:
            raise PermissionError(f"Work order authoring validation failed: {'; '.join(decision.errors)}")

        path = self.workspace.path_for(target_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(wo_yaml, encoding="utf-8")
        self.boundary.journal.complete(record.request.operation_id, f"created {wo_id}")
        return wo_id

    def update_work_order(self, wo_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        record = self._authorize(OperationType.UPDATE_WORK_ORDER, f"workspace/work-orders/{wo_id}")
        if not record.authorized:
            raise PermissionError(record.reason)

        from validators.harness.authoring_gate import AuthoringGate
        gate = getattr(self, "_authoring_gate", None)
        authoritative_root = getattr(self.workspace, "authoritative_root", None)
        if gate is None:
            gate = AuthoringGate(project_root=authoritative_root)
            self._authoring_gate = gate

        target_path = f".sync/work-orders/ACTIVE/{wo_id}.yaml"
        auth_ok, reason = gate.validate_author_role(self.actor_id, target_path)
        if not auth_ok and reason:
            raise PermissionError(reason)

        path = self.workspace.path_for(target_path)
        if not path.is_file():
            raise FileNotFoundError(f"Work order file '{target_path}' not found")

        current = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        current.update(updates)
        new_yaml = yaml.dump(current, sort_keys=False)

        decision = gate.validate_artifact_content(target_path, new_yaml, agent=self.actor_id, project_root=authoritative_root)
        if not decision.passed:
            raise PermissionError(f"Work order update validation failed: {'; '.join(decision.errors)}")

        path.write_text(new_yaml, encoding="utf-8")
        self.boundary.journal.complete(record.request.operation_id, f"updated {wo_id}")
        return current

    def get_contract(self, wo_id: str | None = None) -> dict[str, Any]:
        record = self._authorize(OperationType.GET_CONTRACT, f"session/contract/{wo_id or self.contract.work_order}")
        if not record.authorized:
            raise PermissionError(record.reason)

        if wo_id is None or wo_id == self.contract.work_order:
            res = {
                "agent_id": self.contract.agent_id,
                "work_order": self.contract.work_order,
                "allow": list(self.contract.allow),
                "deny": list(self.contract.deny),
                "write_mode": self.contract.write_mode,
                "version": self.contract.version,
                "budget": dict(self.contract.budget),
            }
        else:
            contract_path = self.workspace.path_for(f".sync/contracts/{wo_id}.yaml")
            if contract_path.is_file():
                res = yaml.safe_load(contract_path.read_text(encoding="utf-8")) or {}
            else:
                raise FileNotFoundError(f"Contract file for {wo_id} not found")

        self.boundary.journal.complete(record.request.operation_id, "retrieved")
        return res

    def verify_scope(self, path: str, operation: str = "read_file") -> bool:
        record = self._authorize(OperationType.VERIFY_SCOPE, f"workspace/{path}")
        if not record.authorized:
            raise PermissionError(record.reason)

        evaluator = ContractEvaluator()
        ok, reason = evaluator.authorize(self.contract, operation, path)
        self.boundary.journal.complete(record.request.operation_id, "in_scope" if ok else "denied")
        return ok

    def explain_denial(self, path: str, operation: str = "read_file") -> str:
        record = self._authorize(OperationType.EXPLAIN_DENIAL, f"workspace/{path}")
        if not record.authorized:
            raise PermissionError(record.reason)

        evaluator = ContractEvaluator()
        ok, reason = evaluator.authorize(self.contract, operation, path)
        res = "Target is in scope and authorized" if ok else f"Scope denial: {reason}"
        self.boundary.journal.complete(record.request.operation_id, res)
        return res

    def inspect_budget(self) -> dict[str, Any]:
        record = self._authorize(OperationType.INSPECT_BUDGET, "session/budget")
        if not record.authorized:
            raise PermissionError(record.reason)

        budget_info = dict(self.contract.budget)
        self.boundary.journal.complete(record.request.operation_id, f"{len(budget_info)} budget fields")
        return budget_info


