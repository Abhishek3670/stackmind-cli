"""Autonomous synthesis and validation of child Work Orders and Contracts (CONTRACT-01 & LEARN-01).

Derives governed, schema-validated Work Orders and Contracts from approved PLAN.md
milestones, ensuring fail-closed compliance with AuthoringGate and schemas across
both tool-using cloud models and conversational local models (Ollama, vLLM).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import yaml

from validators.harness.authoring_gate import AuthoringGate
from validators.harness.plan import parse_plan, PlanStructure, TechStack, PlanValidationError, parse_tech_stack, PlanMilestone
from validators.skill.selector import select_skills_for_task


from validators.clock import now as _now


def _is_test_path(path: str) -> bool:
    """True when a path denotes a test artifact (tests/ dir or test_ naming)."""
    norm = str(path).replace("\\", "/").strip().strip("`\"'").lstrip("./")
    if not norm:
        return False
    parts = norm.lower().split("/")
    if parts[0] in ("tests", "test"):
        return True
    stem = Path(norm).stem.lower()
    return stem.startswith("test_") or stem.endswith(("_test", ".test", ".spec"))


def _slugify(title: str, max_len: int = 48) -> str:
    words = re.sub(r"[^a-zA-Z0-9]+", "_", str(title or "")).strip("_").lower()
    words = re.sub(r"_+", "_", words)
    if len(words) > max_len:
        words = words[:max_len].rstrip("_")
    return words or "qa_suite"


def extract_candidate_paths(text: str) -> list[str]:
    """Extract candidate file paths from text, ignoring VCS and internal system files."""
    if not text:
        return []
    file_pattern = r"(?:[`\"'\s(\[{<]|^)([a-zA-Z0-9_\-\.\/]+\.[a-zA-Z0-9]{1,6})(?=[`\"'\s)\]}>,;:]|$)"
    matches = re.findall(file_pattern, text)
    candidate_paths: list[str] = []
    seen = set()
    for m in matches:
        clean = m.strip().strip("`\"'.,()")
        if clean in seen:
            continue
        if any(clean.endswith(ext) for ext in (
            ".txt", ".py", ".html", ".js", ".jsx", ".ts", ".tsx", ".css", ".md",
            ".json", ".yaml", ".yml", ".sql", ".db", ".sqlite", ".go", ".rs", ".vue", ".svelte",
            ".svg", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot"
        )):
            if not clean.startswith(".sync") and not clean.startswith(".git") and clean not in ("PLAN.md", ".env"):
                candidate_paths.append(clean)
                seen.add(clean)

    # Check for dependency manifests mentioned by name without explicit quotes
    text_lower = text.lower()
    for mf in ("package.json", "requirements.txt", "pyproject.toml", "cargo.toml", "go.mod"):
        if mf in text_lower and mf not in seen:
            candidate_paths.append(mf)
            seen.add(mf)

    return candidate_paths


def extract_deliverable_spec(
    milestone_title: str,
    tasks: list[str],
    role: str,
    stack: TechStack | None = None,
    project_path: Path | str | None = None,
) -> dict[str, Any]:
    """Extract deliverable type, path, and description from milestone and tasks, respecting tech stack precedence."""
    if stack and stack.is_ambiguous:
        return {
            "type": "blocked",
            "path": None,
            "description": f"Blocked by stack ambiguity: {stack.ambiguity_reason}",
            "error": f"Stack ambiguity: {stack.ambiguity_reason}",
        }

    combined_text = f"{milestone_title}\n" + "\n".join(tasks)
    combined_lower = combined_text.lower()

    # 1. Search for explicitly mentioned file paths in tasks
    candidate_paths = extract_candidate_paths(combined_text)

    role_norm = role.lower().strip()

    # Precedence 1: Explicit Scaffolding / Dependency Manifest
    is_scaffold_task = any(kw in combined_lower for kw in (
        "scaffolding", "dependency setup", "dependencies setup", "setup dependencies",
        "install dependencies", "initialize dependencies", "configure dependencies",
        "package.json", "requirements.txt", "pyproject.toml", "cargo.toml", "go.mod"
    ))
    manifest_candidates = [
        p for p in candidate_paths
        if p in ("package.json", "requirements.txt", "pyproject.toml", "Cargo.toml", "go.mod")
        or p.endswith(("/package.json", "/requirements.txt", "/pyproject.toml", "/Cargo.toml", "/go.mod"))
    ]

    if (is_scaffold_task or manifest_candidates) and role_norm in ("backend", "codex", "gitops", "local-llm"):
        if manifest_candidates:
            return {
                "type": "config",
                "path": manifest_candidates[0],
                "description": f"Project dependency manifest declaring required packages for {milestone_title}",
            }
        if stack and stack.dependency_manifest:
            return {
                "type": "config",
                "path": stack.dependency_manifest,
                "description": f"Project dependency manifest declaring required packages for {milestone_title}",
            }
        if project_path:
            pp = Path(project_path).resolve()
            if (pp / "package.json").is_file():
                return {"type": "config", "path": "package.json", "description": f"Project dependency manifest for {milestone_title}"}
            if (pp / "requirements.txt").is_file():
                return {"type": "config", "path": "requirements.txt", "description": f"Project dependency manifest for {milestone_title}"}
            if (pp / "pyproject.toml").is_file():
                return {"type": "config", "path": "pyproject.toml", "description": f"Project dependency manifest for {milestone_title}"}

        # Check stack language for scaffolding fallback
        is_node_scaffold = (
            (stack and stack.backend_language in ("javascript", "typescript"))
            or (stack and stack.backend_framework in ("express", "nest"))
            or any(kw in combined_lower for kw in ("node", "express", "npm", "javascript"))
        )
        if is_node_scaffold:
            return {
                "type": "config",
                "path": "package.json",
                "description": f"Project dependency manifest declaring required packages for {milestone_title}",
            }
        return {
            "type": "config",
            "path": "requirements.txt",
            "description": f"Project dependency manifest declaring required packages for {milestone_title}",
        }

    # Role-based extraction:
    # ── Frontend (Gemini) ──
    if role_norm in ("gemini", "frontend"):
        target_path = next(
            (p for p in candidate_paths if any(p.endswith(ext) for ext in (".html", ".jsx", ".tsx", ".vue", ".svelte", ".js", ".ts", ".css")) and not _is_test_path(p)),
            None,
        )
        if not target_path:
            # Check tech stack
            is_react = (stack and stack.frontend_framework == "react") or ("react" in combined_lower)
            is_vue = (stack and stack.frontend_framework == "vue") or ("vue" in combined_lower)
            is_svelte = (stack and stack.frontend_framework == "svelte") or ("svelte" in combined_lower)

            if is_react:
                if "landing" in combined_lower:
                    target_path = "src/components/Landing.jsx"
                elif any(kw in combined_lower for kw in ("theme", "design system", "design")):
                    target_path = "src/theme.js"
                elif any(kw in combined_lower for kw in ("login", "auth", "register")):
                    target_path = "src/components/Login.jsx"
                elif "dashboard" in combined_lower:
                    target_path = "src/components/Dashboard.jsx"
                else:
                    target_path = "src/App.jsx"
            elif is_vue:
                if "landing" in combined_lower:
                    target_path = "src/components/Landing.vue"
                else:
                    target_path = "src/App.vue"
            elif is_svelte:
                target_path = "src/App.svelte"
            else:
                if any(kw in combined_lower for kw in ("style", "css", "theme", "stylesheet", "design system")):
                    target_path = "css/style.css"
                elif any(kw in combined_lower for kw in ("script", "javascript", "interactivity")):
                    target_path = "js/main.js"
                elif any(kw in combined_lower for kw in ("login", "auth")):
                    target_path = "src/login.html"
                else:
                    target_path = "index.html"

        return {
            "type": "code",
            "path": target_path,
            "description": f"Frontend interface and UI components for {milestone_title}",
        }

    # ── QA (Gemma) ──
    if role_norm in ("gemma", "qa"):
        test_path = next((p for p in candidate_paths if _is_test_path(p)), None)
        if not test_path:
            is_js_qa = (
                (stack and stack.backend_language in ("javascript", "typescript"))
                or (stack and stack.frontend_framework in ("react", "vue", "svelte"))
                or any(kw in combined_lower for kw in ("jest", "mocha", "vitest", "playwright", "cypress"))
            )
            if is_js_qa:
                test_path = f"tests/test_{_slugify(milestone_title)}.test.js"
            elif stack and stack.backend_language == "go":
                test_path = f"tests/{_slugify(milestone_title)}_test.go"
            else:
                test_path = f"tests/test_{_slugify(milestone_title)}.py"

        return {
            "type": "code",
            "path": test_path,
            "description": f"Executable end-to-end test suite and QA sign-off for {milestone_title}",
        }

    # ── GitOps (Local-LLM) ──
    if role_norm in ("local-llm", "gitops"):
        target_path = next(
            (p for p in candidate_paths if p in ("VERSION.md", "CHANGELOG.md", "package.json")),
            "VERSION.md",
        )
        return {
            "type": "doc",
            "path": target_path,
            "description": f"Release metadata and version bump for {milestone_title}",
        }

    # ── Backend (Codex) ──
    # 1. Explicit code paths in candidate_paths take precedence
    code_path = next(
        (p for p in candidate_paths if any(p.endswith(ext) for ext in (".py", ".js", ".ts", ".mjs", ".go", ".rs")) and not _is_test_path(p)),
        None,
    )
    if code_path:
        return {
            "type": "code",
            "path": code_path,
            "description": f"Backend API and logic implementation for {milestone_title}",
        }

    # 2. Derive path based on approved technology stack precedence
    is_node = (
        (stack and stack.backend_language in ("javascript", "typescript"))
        or (stack and stack.backend_framework in ("express", "nest"))
        or any(kw in combined_lower for kw in ("node", "express", "npm", "javascript"))
    )
    is_python = (
        (stack and stack.backend_language == "python")
        or (stack and stack.backend_framework in ("flask", "fastapi", "django"))
        or any(kw in combined_lower for kw in ("python", "flask", "fastapi", "django", "pytest"))
    )
    is_go = (stack and stack.backend_language == "go") or any(kw in combined_lower for kw in ("go", "golang"))
    is_rust = (stack and stack.backend_language == "rust") or any(kw in combined_lower for kw in ("rust", "cargo"))

    # Check project structure on disk if stack was not explicit
    if not (is_node or is_python or is_go or is_rust) and project_path:
        pp = Path(project_path).resolve()
        if (pp / "package.json").is_file():
            is_node = True
        elif (pp / "requirements.txt").is_file() or (pp / "pyproject.toml").is_file():
            is_python = True
        elif (pp / "Cargo.toml").is_file():
            is_rust = True
        elif (pp / "go.mod").is_file():
            is_go = True

    if is_node:
        is_ts = (stack and stack.backend_language == "typescript") or ("typescript" in combined_lower)
        ext = ".ts" if is_ts else ".js"
        if any(kw in combined_lower for kw in ("auth", "login", "user")):
            target_path = f"src/routes/auth{ext}"
        elif "api" in combined_lower or "server" in combined_lower:
            target_path = f"src/server{ext}"
        else:
            target_path = f"src/server{ext}"
    elif is_go:
        target_path = "main.go"
    elif is_rust:
        target_path = "src/main.rs"
    else:
        # Default Python backend convention
        target_path = "src/backend.py"

    return {
        "type": "code",
        "path": target_path,
        "description": f"Backend API and logic implementation for {milestone_title}",
    }


def determine_assigned_agent(
    title: str, tasks: list[str], agent_hint: str | None = None
) -> tuple[str, str]:
    """Determine (agent_id, role) from milestone title and tasks.

    The Architect's explicit designation in PLAN.md takes top priority — either
    the structured `agent_hint` (parsed from the "(Agent: <id>)" annotation by
    the plan parser) or an inline designation in the title/tasks text.
    """
    role_map = {
        "codex": "backend",
        "gemini": "frontend",
        "gemma": "qa",
        "local-llm": "gitops",
        "claude": "architecture",
    }
    hint = str(agent_hint or "").strip().lower()
    if hint in role_map:
        return hint, role_map[hint]

    combined = (f"{title} " + " ".join(tasks)).lower()
    title_lower = title.lower()

    # 1. Top priority: Explicit agent designation by the Architect in title or tasks
    # Patterns: (Agent: codex), [Agent: codex], Agent: codex, Assigned: codex, Owner: codex,
    #           (codex), [codex], @codex
    target_header = f"{title} " + (tasks[0] if tasks else "")

    agent_match = (
        re.search(r"(?:agent|assigned|owner)\s*[:=]\s*([a-zA-Z0-9_\-]+)", target_header, re.IGNORECASE)
        or re.search(r"[\(\[]\s*(?:agent:\s*)?(codex|gemini|gemma|local-llm|claude)\s*[\)\]]", target_header, re.IGNORECASE)
        or re.search(r"@([a-zA-Z0-9_\-]+)", target_header)
    )
    if agent_match:
        explicit_agent = agent_match.group(1).lower().strip()
        if explicit_agent in role_map:
            return explicit_agent, role_map[explicit_agent]

    # 2. Explicit role designation by the Architect
    # Patterns: (Role: backend), [Role: frontend], Role: qa, Role: gitops
    role_match = re.search(r"\brole\s*[:=]\s*([a-zA-Z0-9_\-]+)", target_header, re.IGNORECASE)
    if role_match:
        explicit_role = role_match.group(1).lower().strip()
        agent_role_map = {
            "backend": ("codex", "backend"),
            "frontend": ("gemini", "frontend"),
            "qa": ("gemma", "qa"),
            "gitops": ("local-llm", "gitops"),
            "release": ("local-llm", "gitops"),
            "architecture": ("claude", "architecture"),
        }
        if explicit_role in agent_role_map:
            return agent_role_map[explicit_role]

    # 3. Fallback: Semantic heuristics based on title and task keywords
    # Frontend checks: explicit UI files or keywords
    if re.search(r"\b[a-zA-Z0-9_\-\.\/]+\.(?:html|css|jsx|tsx)\b", combined) or re.search(
        r"\b(?:ui|frontend|views?|components?|screens?|css|html|tailwind|react|client|landing page|login page)\b", combined
    ):
        return "gemini", "frontend"

    # GitOps checks
    if re.search(r"\b(?:release|git|deploy|packaging|version|gitops|changelog)\b", combined):
        return "local-llm", "gitops"

    # Implementation / backend logic belongs to Codex (even if tasks include unit tests)
    if re.search(r"\b(?:implement|implementation|backend|feature|logic|module|service|core|tokenbucket|token_bucket|api)\b", title_lower):
        return "codex", "backend"

    # QA checks: pure verification, test suite execution, audit, or review
    if re.search(r"\b(?:qa|verification|audit|validation|review)\b", title_lower) or re.search(
        r"\b(?:qa|tests?|testing|verification|audit|validation|review)\b", combined
    ):
        return "gemma", "qa"

    # Scaffolding / dependencies specifically for backend
    if any(kw in combined for kw in ("package.json", "requirements.txt", "pyproject.toml", "cargo.toml", "go.mod", "dependency manifest", "dependencies manifest", "setup dependencies", "scaffold", "scaffolding")):
        return "codex", "backend"

    return "codex", "backend"


def build_child_work_order(
    wo_id: str,
    title: str,
    description: str,
    assigned_agents: list[str],
    dependencies: list[str],
    priority: str,
    deliverable: dict[str, str],
    implementation_estimate: dict[str, Any] | None = None,
    milestone_id: str | None = None,
    skills: list[str] | None = None,
    deliverables: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Construct a schema-conforming Work Order record."""
    now = _now()
    deliv_spec: dict[str, Any] = {
        "type": deliverable.get("type", "code"),
        "description": deliverable.get("description", f"Deliverables for {title}"),
    }
    if deliverable.get("path"):
        deliv_spec["path"] = deliverable["path"]

    record: dict[str, Any] = {
        "id": wo_id,
        "type": "FEATURE",
        "title": title.strip(),
        "status": "ACTIVE",
        "priority": priority,
        "assigned_agents": list(assigned_agents),
        "dependencies": list(dependencies),
        "deliverable": deliv_spec,
        "description": description.strip() or title.strip(),
        "created": now,
        "updated": now,
    }
    if milestone_id:
        record["milestone_id"] = str(milestone_id).strip()
    if implementation_estimate:
        record["implementation_estimate"] = dict(implementation_estimate)
    if skills:
        record["skills"] = list(skills)
    if deliverables:
        record["deliverables"] = list(deliverables)
    return record


def derive_implementation_estimate(
    title: str,
    description: str,
    deliverable_path: str | None,
    candidate_paths: list[str] | None = None,
    deliverable_type: str | None = None,
    stack: TechStack | None = None,
) -> dict[str, Any] | None:
    """Derive an explicit implementation estimate from the task's expected file set.

    The estimate drives the contract file budget so a scaffold task is never
    silently blocked by an arbitrary fixed limit, and so a task whose expected
    footprint grows beyond a small bounded set is flagged for splitting instead
    of receiving an unbounded budget.

    Every code deliverable also plans its D024 companion test
    (``tests/test_<stem>.py`` or language equivalent) so test authoring is part of the
    work order's declared footprint instead of being discovered missing at GitOps time.
    """
    expected: list[str] = []
    if deliverable_path:
        expected.append(str(deliverable_path).replace("\\", "/").strip().lstrip("/"))
    for cp in candidate_paths or []:
        clean = str(cp).replace("\\", "/").strip().lstrip("/")
        if clean and clean not in expected:
            expected.append(clean)
    if not expected:
        return None

    deliv_norm = (deliverable_path or "").replace("\\", "/").strip().lstrip("/")
    code_exts = (".py", ".ts", ".js", ".jsx", ".tsx", ".mjs", ".go", ".rs", ".dart")
    is_code_deliverable = (
        str(deliverable_type or "").lower() == "code"
        or (bool(deliv_norm) and deliv_norm.endswith(code_exts))
    )
    if (
        is_code_deliverable
        and deliv_norm
        and not _is_test_path(deliv_norm)
        and not any(_is_test_path(f) for f in expected)
    ):
        stem = Path(deliv_norm).stem
        ext = Path(deliv_norm).suffix.lower()
        if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs") or (stack and stack.backend_language in ("javascript", "typescript")):
            test_ext = ".test.ts" if (ext in (".ts", ".tsx") or (stack and stack.backend_language == "typescript")) else ".test.js"
            expected.append(f"tests/test_{stem}{test_ext}")
        elif ext == ".go" or (stack and stack.backend_language == "go"):
            expected.append(f"tests/{stem}_test.go")
        elif ext == ".rs" or (stack and stack.backend_language == "rust"):
            expected.append(f"tests/test_{stem}.rs")
        else:
            expected.append(f"tests/test_{stem}.py")

    return {
        "expected_files": expected,
        "max_files_touched": len(expected),
        "rationale": f"Plan-derived estimate of the files this task creates or modifies: {', '.join(expected)}",
    }


def build_child_contract(
    wo_id: str,
    agent_id: str,
    role: str,
    deliverable_path: str | None = None,
    candidate_paths: list[str] | None = None,
    max_files_touched: int | None = None,
    skills: list[str] | None = None,
    title: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Construct a schema-conforming Contract record bound strictly to the assigned scope.

    ``max_files_touched`` should come from the work order's plan-derived
    implementation estimate.  The conservative default is retained only for
    explicitly designated bootstrap/internal work orders — it must never be
    used to paper over a failed Architect authoring turn.
    """
    role_norm = role.lower().strip()
    allow_rules: list[dict[str, Any]] = [
        {"module": "PLAN.md"},
        {"module": f".sync/inbox/{agent_id}/**"},
    ]

    if deliverable_path:
        allow_rules.append({"module": deliverable_path})

    if candidate_paths:
        for cp in candidate_paths:
            if cp and isinstance(cp, str) and not cp.startswith(".git") and not cp.startswith(".sync") and cp != ".env":
                allow_rules.append({"module": cp})
                clean_cp = cp.replace("\\", "/").strip().lstrip("/")
                if "/" in clean_cp:
                    top_dir = clean_cp.split("/")[0]
                    if top_dir not in (".git", ".sync", ".env"):
                        allow_rules.append({"module": f"{top_dir}/**"})

    # Authorize command execution for QA/testing and build/install/setup tasks
    needs_commands = (
        role_norm in ("qa", "gemma")
        or any(s in ("test", "build", "diagnosing-bugs", "exec", "pr") for s in (skills or []))
        or any(
            kw in f"{title or ''} {description or ''} {wo_id}".lower()
            for kw in ("build", "test", "install", "setup", "dependency", "dependencies", "compile", "exec", "npm", "pip", "pytest", "vite")
        )
    )
    if needs_commands:
        allow_rules.append({"module": "workspace/command"})

    # Common repository root documentation and config files allowed across roles:
    allow_rules.extend([
        {"module": "*.md"},
        {"module": "*.txt"},
        {"module": "*.toml"},
        {"module": "*.json"},
        {"module": "*.yaml"},
        {"module": "*.yml"},
        {"module": "README*"},
        {"module": "CHANGELOG*"},
        {"module": "VERSION*"},
        {"module": "LICENSE*"},
        {"module": ".gitignore"},
        {"module": "*ignore"},
        {"module": "Makefile*"},
        # Governance bookkeeping channel: machine-readable recovery decisions
        {"module": ".sync/decisions/**"},
    ])

    if role_norm in ("backend", "codex"):
        allow_rules.extend([
            {"module": "src/**"},
            {"module": "app/**"},
            {"module": "api/**"},
            {"module": "backend/**"},
            {"module": "db/**"},
            {"module": "models/**"},
            {"module": "services/**"},
            {"module": "routes/**"},
            {"module": "controllers/**"},
            {"module": "tests/**"},
            {"module": "test/**"},
            {"module": "requirements.txt"},
            {"module": "requirements*.txt"},
            {"module": "pyproject.toml"},
            {"module": "setup.py"},
            {"module": "setup.cfg"},
            {"module": "Makefile"},
            {"module": "*.py"},
            {"module": "*.sql"},
            {"module": "*.db"},
            {"module": "*.sqlite"},
            {"module": "*.json"},
            {"module": "*.yaml"},
            {"module": "*.yml"},
        ])
    elif role_norm in ("frontend", "gemini"):
        allow_rules.extend([
            {"module": "src/**"},
            {"module": "app/**"},
            {"module": "public/**"},
            {"module": "frontend/**"},
            {"module": "static/**"},
            {"module": "templates/**"},
            {"module": "views/**"},
            {"module": "components/**"},
            {"module": "assets/**"},
            {"module": "css/**"},
            {"module": "js/**"},
            {"module": "styles/**"},
            {"module": "images/**"},
            {"module": "img/**"},
            {"module": "fonts/**"},
            {"module": "index.html"},
            {"module": "package.json"},
            {"module": "*.html"},
            {"module": "*.js"},
            {"module": "*.ts"},
            {"module": "*.tsx"},
            {"module": "*.jsx"},
            {"module": "*.css"},
            {"module": "*.svg"},
            {"module": "*.png"},
            {"module": "*.jpg"},
            {"module": "*.jpeg"},
            {"module": "*.webp"},
            {"module": "*.gif"},
            {"module": "*.ico"},
        ])
    elif role_norm in ("qa", "gemma"):
        allow_rules.extend([
            # QA verdict channel: gemma delivers verdicts to the Architect
            {"module": ".sync/inbox/claude/**"},
            {"module": "tests/**"},
            {"module": "test/**"},
            {"module": "src/**"},
            {"module": "app/**"},
            {"module": "*.py"},
            {"module": "*.db"},
            {"module": "*.sqlite"},
            {"module": "pytest.ini"},
            # QA must also inspect and audit frontend deliverables and assets
            {"module": "index.html"},
            {"module": "css/**"},
            {"module": "styles/**"},
            {"module": "js/**"},
            {"module": "assets/**"},
            {"module": "images/**"},
            {"module": "img/**"},
            {"module": "fonts/**"},
            {"module": "*.html"},
            {"module": "*.css"},
            {"module": "*.js"},
            {"module": "*.jsx"},
            {"module": "*.tsx"},
            {"module": "*.ts"},
            {"module": "*.svg"},
            {"module": "*.png"},
            {"module": "*.jpg"},
            {"module": "*.jpeg"},
            {"module": "*.webp"},
            {"module": "*.gif"},
            {"module": "*.ico"},
        ])
    elif role_norm in ("gitops", "local-llm"):
        allow_rules.extend([
            {"module": "VERSION.md"},
            {"module": "CHANGELOG.md"},
            {"module": "README.md"},
            {"module": "README*"},
            {"module": "Makefile"},
            {"module": "pyproject.toml"},
            {"module": "package.json"},
            {"module": "setup.py"},
            {"module": "setup.cfg"},
            {"module": "src/**"},
            {"module": "app/**"},
            {"module": "tests/**"},
            {"module": "test/**"},
            {"module": "*.py"},
        ])
    else:
        allow_rules.extend([
            {"module": "src/**"},
            {"module": "app/**"},
            {"module": "*.py"},
        ])

    # Deduplicate allow rules preserving structure
    seen = set()
    unique_allow = []
    for r in allow_rules:
        mod = r.get("module")
        if mod and mod not in seen:
            seen.add(mod)
            unique_allow.append(r)

    deny_rules = [
        {"module": ".git/**"},
        {"module": ".env"},
        {"module": ".env.*"},
        {"module": ".sync/runtime/**"},
        {"module": ".sync/knowledge/**"},
        {"module": ".sync/snapshots/**"},
        {"module": ".sync/outbox/**"},
        {"module": ".sync/agents/**"},
        {"module": "__pycache__/**"},
        {"module": ".venv/**"},
        {"module": "node_modules/**"},
    ]
    # Protect executive inbox from unauthorized worker access
    if agent_id != "CEO":
        deny_rules.append({"module": ".sync/inbox/CEO/**"})
    # Preserve protocol message routing channels (AGENTS.md):
    # - Workers need to drop review requests to .sync/inbox/gemma/
    # - Gemma needs to drop verdicts to .sync/inbox/claude/ and .sync/inbox/codex/
    # - Claude needs to dispatch to worker inboxes
    for other_agent in ("claude", "codex", "gemini", "gemma", "local-llm"):
        if other_agent == agent_id:
            continue
        # Exclude permitted protocol routing channels
        if role_norm in ("qa", "gemma") and other_agent in ("claude", "codex", "gemini"):
            continue
        if role_norm in ("backend", "frontend", "codex", "gemini") and other_agent in ("gemma", "claude"):
            continue
        if role_norm in ("architecture", "claude"):
            continue
        deny_rules.append({"module": f".sync/inbox/{other_agent}/**"})

    ret: dict[str, Any] = {
        "schema_version": 1,
        "agent_id": agent_id,
        "work_order": wo_id,
        "identity": {
            "role": role_norm,
            "reports_to": "claude",
        },
        "scope": {
            "allow": unique_allow,
            "deny": deny_rules,
            "write": "read-write",
        },
        "budget": {
            "max_files_touched": max_files_touched if max_files_touched and max_files_touched > 0 else 10,
            "max_tokens": 0,
        },
    }
    if skills:
        ret["skills"] = list(skills)
    return ret


def extract_declared_deliverable_paths(wo_data: dict[str, Any]) -> set[str]:
    """Extract normalized paths of all declared deliverables from a work order."""
    paths: set[str] = set()
    deliv = wo_data.get("deliverable")
    if isinstance(deliv, dict) and deliv.get("path"):
        paths.add(str(deliv["path"]).replace("\\", "/").strip().lstrip("/"))
    for extra in wo_data.get("deliverables") or []:
        if isinstance(extra, dict) and extra.get("path"):
            paths.add(str(extra["path"]).replace("\\", "/").strip().lstrip("/"))
        elif isinstance(extra, str) and extra.strip():
            paths.add(extra.replace("\\", "/").strip().lstrip("/"))
    return paths


def validate_work_orders_ownership(wos: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """Validate that parallel work orders do not claim conflicting file ownership.

    Returns:
        (is_valid, list of error messages)
    """
    errors: list[str] = []

    deps_map: dict[str, set[str]] = {}
    for wo in wos:
        wo_id = str(wo.get("id") or "")
        deps = set()
        for d in wo.get("dependencies") or []:
            if d and str(d).strip():
                deps.add(str(d).strip())
        deps_map[wo_id] = deps

    def _is_descendant(target: str, source: str) -> bool:
        visited = set()
        queue = list(deps_map.get(source, set()))
        while queue:
            curr = queue.pop(0)
            if curr == target:
                return True
            if curr not in visited:
                visited.add(curr)
                queue.extend(deps_map.get(curr, set()))
        return False

    def are_serialized(wo_a: str, wo_b: str) -> bool:
        return _is_descendant(wo_b, wo_a) or _is_descendant(wo_a, wo_b)

    file_to_wos: dict[str, list[str]] = {}
    for wo in wos:
        wo_id = str(wo.get("id") or "")
        paths = extract_declared_deliverable_paths(wo)
        for p in paths:
            file_to_wos.setdefault(p, []).append(wo_id)

    for file_path, claiming_wos in sorted(file_to_wos.items()):
        if len(claiming_wos) > 1:
            for i in range(len(claiming_wos)):
                for j in range(i + 1, len(claiming_wos)):
                    wo_a = claiming_wos[i]
                    wo_b = claiming_wos[j]
                    if not are_serialized(wo_a, wo_b):
                        errors.append(
                            f"Overlapping file ownership detected for '{file_path}': "
                            f"work orders '{wo_a}' and '{wo_b}' claim the same deliverable concurrently "
                            f"without dependency ordering"
                        )

    return len(errors) == 0, errors


def decompose_milestone(
    m: PlanMilestone,
    start_idx: int,
    stack: TechStack | None = None,
    project_path: Path | str | None = None,
    prev_wo_id: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Decompose a milestone into one or more validated child work orders.

    If the milestone contains multiple subtasks that declare distinct non-test
    deliverable files (e.g., landing.html, app.js, style.css), decompose them
    into sequential child work orders linked by explicit dependencies and sharing
    the milestone_id.

    If the milestone is cohesive or subtasks target the same deliverable/companion
    tests, generate a single work order covering all declared files.

    Returns:
        (list of work order dictionaries, last_wo_id)
    """
    agent, role = determine_assigned_agent(m.title, m.tasks, agent_hint=m.agent)

    # Analyze tasks for distinct non-test deliverables
    task_deliverables: list[tuple[str, str, dict[str, Any]]] = []
    if len(m.tasks) > 1:
        for t in m.tasks:
            t_clean = t.strip()
            if not t_clean:
                continue
            cands = extract_candidate_paths(t_clean)
            non_test_cands = [p for p in cands if not _is_test_path(p)]
            if non_test_cands:
                t_deliv = extract_deliverable_spec(t_clean, [t_clean], role, stack=stack, project_path=project_path)
                if t_deliv.get("path") and not _is_test_path(str(t_deliv.get("path"))):
                    task_deliverables.append((t_clean, t_clean, t_deliv))

    distinct_paths = [td[2]["path"] for td in task_deliverables if td[2].get("path")]
    should_decompose = len(task_deliverables) > 1 and len(set(distinct_paths)) == len(task_deliverables)

    wos: list[dict[str, Any]] = []
    current_prev = prev_wo_id

    if should_decompose:
        for offset, (t_text, t_title, t_deliv) in enumerate(task_deliverables):
            wo_idx = start_idx + offset
            wo_id = f"WO-{wo_idx:03d}"
            deps = [current_prev] if current_prev else []
            clean_title = re.sub(r"^Task\s+[0-9]+(?:\.[0-9]+)?\s*:\s*", "", t_title, flags=re.IGNORECASE).strip()
            clean_title = re.sub(r"^-\s*\[[ xX]\]\s*", "", clean_title).strip()
            wo_title = f"{m.title}: {clean_title}" if len(clean_title) < 50 else clean_title[:80]

            wo = build_child_work_order(
                wo_id=wo_id,
                title=wo_title,
                description=t_text,
                assigned_agents=[agent],
                dependencies=deps,
                priority="P0" if wo_idx == 1 else "P1",
                deliverable=t_deliv,
                milestone_id=m.id,
                skills=m.skills if m.skills else None,
            )
            wos.append(wo)
            current_prev = wo_id
        return wos, current_prev
    else:
        wo_id = f"WO-{start_idx:03d}"
        deps = [current_prev] if current_prev else []
        deliv = extract_deliverable_spec(m.title, m.tasks, role, stack=stack, project_path=project_path)

        all_cands = extract_candidate_paths(f"{m.title}\n" + "\n".join(m.tasks))
        non_test_cands = [p for p in all_cands if not _is_test_path(p)]

        deliverables_list: list[dict[str, Any]] | None = None
        if len(non_test_cands) > 1:
            deliverables_list = []
            for p in non_test_cands:
                p_type = "config" if p in ("package.json", "requirements.txt", "pyproject.toml") else "code"
                deliverables_list.append({
                    "type": p_type,
                    "path": p,
                    "description": f"Deliverable {p} for {m.title}",
                })

        wo = build_child_work_order(
            wo_id=wo_id,
            title=m.title,
            description="\n".join(m.tasks) if m.tasks else m.title,
            assigned_agents=[agent],
            dependencies=deps,
            priority="P0" if start_idx == 1 else "P1",
            deliverable=deliv,
            milestone_id=m.id,
            skills=m.skills if m.skills else None,
            deliverables=deliverables_list,
        )
        wos.append(wo)
        return wos, wo_id


def decompose_plan_milestones(
    milestones: list[PlanMilestone],
    start_idx: int,
    stack: TechStack | None = None,
    project_path: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Decompose parsed plan milestones into child work order dictionaries."""
    all_wos: list[dict[str, Any]] = []
    current_idx = start_idx
    prev_wo_id: str | None = None

    for m in milestones:
        child_wos, last_id = decompose_milestone(
            m,
            start_idx=current_idx,
            stack=stack,
            project_path=project_path,
            prev_wo_id=prev_wo_id,
        )
        all_wos.extend(child_wos)
        current_idx += len(child_wos)
        prev_wo_id = last_id

    return all_wos


def get_next_work_order_int(workspace: Path | str) -> int:
    """Find the next monotonically increasing integer ID for work orders.

    Scans:
    1. .sync/work-orders/ACTIVE/
    2. .sync/work-orders/COMPLETED/
    3. .sync/work-orders/INDEX.yaml

    Returns max(existing_ids) + 1, starting at 1 if no existing worker WOs.
    """
    ws = Path(workspace).resolve()
    max_id = 0
    pattern = re.compile(r"^WO-([0-9]{3,})$")

    # 1. Check INDEX.yaml
    index_file = ws / ".sync" / "work-orders" / "INDEX.yaml"
    if index_file.is_file():
        try:
            data = yaml.safe_load(index_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                next_id = data.get("next_id")
                if isinstance(next_id, int) and next_id > max_id:
                    max_id = next_id - 1
                for o in data.get("orders", []):
                    if isinstance(o, dict) and o.get("id"):
                        m = pattern.match(str(o["id"]))
                        if m:
                            val = int(m.group(1))
                            if val > max_id:
                                max_id = val
        except Exception:
            pass

    # 2. Check ACTIVE, COMPLETED, and BLOCKED directories
    for sub in ("ACTIVE", "COMPLETED", "BLOCKED"):
        d = ws / ".sync" / "work-orders" / sub
        if d.is_dir():
            for f in d.glob("*.yaml"):
                m = pattern.match(f.stem)
                if m:
                    val = int(m.group(1))
                    if val > max_id:
                        max_id = val

    return max_id + 1


def synthesize_child_work_orders(
    workspace: Path | str,
    plan: dict[str, Any] | str,
    session_id: str | None = None,
) -> list[dict[str, Any]]:
    """Synthesize validated child Work Orders and Contracts on disk from an approved plan.

    All artifacts are strictly verified fail-closed against AuthoringGate and schemas.
    Updates .sync/work-orders/ACTIVE/, .sync/contracts/, INDEX.yaml, and TREE.yaml.
    """
    ws = Path(workspace).resolve()
    active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    contracts_dir = ws / ".sync" / "contracts"
    active_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)

    gate = AuthoringGate(project_root=ws)
    start_idx = get_next_work_order_int(ws)

    # 1. Extract plan structure, technology stack, and proposed work orders
    proposed_wos: list[dict[str, Any]] = []

    tech_stack: TechStack | None = None
    plan_raw_text = ""
    if isinstance(plan, dict):
        plan_raw_text = plan.get("content") or ""
    elif isinstance(plan, str):
        plan_raw_text = plan

    if not plan_raw_text:
        plan_file = ws / "PLAN.md"
        if plan_file.is_file():
            try:
                plan_raw_text = plan_file.read_text(encoding="utf-8")
            except Exception:
                pass

    if plan_raw_text:
        try:
            parsed_plan = parse_plan(plan_raw_text, project_path=ws)
            tech_stack = parsed_plan.tech_stack
        except Exception:
            pass
    elif ws:
        tech_stack = parse_tech_stack("", project_path=ws)

    if tech_stack and tech_stack.is_ambiguous:
        raise PlanValidationError(f"Cannot synthesize child work orders from ambiguous technology stack: {tech_stack.ambiguity_reason}")

    if isinstance(plan, dict):
        # Check if pre-parsed work orders exist in plan metadata
        metadata = plan.get("metadata", {})
        if metadata.get("work_orders"):
            proposed_wos = list(metadata["work_orders"])
        elif plan.get("created_work_orders"):
            proposed_wos = list(plan["created_work_orders"])
        elif plan.get("content"):
            parsed = parse_plan(plan["content"], project_path=ws)
            proposed_wos = decompose_plan_milestones(parsed.milestones, start_idx, stack=tech_stack, project_path=ws)
    elif isinstance(plan, str):
        parsed = parse_plan(plan, project_path=ws)
        proposed_wos = decompose_plan_milestones(parsed.milestones, start_idx, stack=tech_stack, project_path=ws)

    # Fallback to reading PLAN.md directly if still empty
    if not proposed_wos:
        plan_file = ws / "PLAN.md"
        if plan_file.is_file():
            parsed = parse_plan(plan_file.read_text(encoding="utf-8"), project_path=ws)
            proposed_wos = decompose_plan_milestones(parsed.milestones, start_idx, stack=tech_stack, project_path=ws)

    if not proposed_wos:
        raise ValueError("Cannot synthesize child work orders: no milestones or tasks could be parsed from plan")

    # Anti-collision check: if any proposed WO conflicts with existing on-disk work order, remap IDs sequentially
    existing_on_disk = set()
    for sub in ("ACTIVE", "COMPLETED", "BLOCKED"):
        d = ws / ".sync" / "work-orders" / sub
        if d.is_dir():
            for f in d.glob("*.yaml"):
                existing_on_disk.add(f.stem)

    if any(w.get("id") in existing_on_disk for w in proposed_wos):
        id_map: dict[str, str] = {}
        renumbered_wos: list[dict[str, Any]] = []
        for offset, w in enumerate(proposed_wos):
            old_id = str(w.get("id", f"WO-{offset+1:03d}"))
            new_id = f"WO-{start_idx + offset:03d}"
            id_map[old_id] = new_id
            w_copy = dict(w)
            w_copy["id"] = new_id
            new_deps = [id_map.get(d, d) for d in w.get("dependencies", [])]
            w_copy["dependencies"] = new_deps
            renumbered_wos.append(w_copy)
        proposed_wos = renumbered_wos
    # Anti-collision and parallel deliverable ownership check
    valid_ownership, ownership_errors = validate_work_orders_ownership(proposed_wos)
    if not valid_ownership:
        raise PlanValidationError(
            f"Cannot synthesize child work orders due to ownership conflict: {'; '.join(ownership_errors)}"
        )

    created_records: list[dict[str, Any]] = []

    # 2. Validate and write each Work Order and Contract
    for wo in proposed_wos:
        wo_id = str(wo.get("id"))
        if not re.match(r"^WO-[0-9]{3}$", wo_id):
            continue

        assigned = wo.get("assigned_agents", ["codex"])
        has_explicit_assigned = bool(wo.get("assigned_agents"))
        primary_agent = assigned[0] if assigned else "codex"
        title = wo.get("title", f"Work order {wo_id}")
        desc = wo.get("description", title)
        deps = wo.get("dependencies", [])
        prio = wo.get("priority", "P1")
        deliv = wo.get("deliverable") or {}

        # Refine agent assignment only if not explicitly designated by the plan
        if not has_explicit_assigned:
            detected_agent, detected_role = determine_assigned_agent(title, [desc], agent_hint=primary_agent)
            if primary_agent == "codex" and detected_agent in ("gemini", "gemma", "local-llm"):
                primary_agent = detected_agent

        # Safety rule: QA worker (gemma) CANNOT deliver non-test code (violates AuthoringReadiness)
        deliv_path = str(deliv.get("path") or "")
        is_test_path = deliv_path.startswith("tests/") or "test" in Path(deliv_path).name
        if primary_agent == "gemma" and deliv_path and not is_test_path:
            if any(deliv_path.endswith(ext) for ext in (".html", ".css", ".js", ".jsx", ".tsx")):
                primary_agent = "gemini"
            else:
                primary_agent = "codex"

        role = "backend"
        if primary_agent == "gemini":
            role = "frontend"
        elif primary_agent == "gemma":
            role = "qa"
        elif primary_agent in ("local-llm", "gitops"):
            role = "gitops"

        # Ensure deliverable has explicit path matching role
        if not deliv.get("path") or (primary_agent == "gemini" and str(deliv.get("path")).endswith(".txt")):
            deliv = extract_deliverable_spec(title, [desc], role, stack=tech_stack, project_path=ws)

        if deliv.get("type") == "blocked":
            raise PlanValidationError(
                f"Cannot synthesize work order {wo_id}: {deliv.get('error') or deliv.get('description')}"
            )

        # Extract candidate paths from title and description
        combined_task_text = f"{title}\n{desc}"
        task_candidates = extract_candidate_paths(combined_task_text)

        # Ensure declared deliverable paths are in task_candidates
        if deliv.get("path"):
            deliv_p = str(deliv["path"]).replace("\\", "/").strip().lstrip("/")
            if deliv_p and deliv_p not in task_candidates:
                task_candidates.append(deliv_p)
        for extra in wo.get("deliverables") or []:
            if isinstance(extra, dict) and extra.get("path"):
                ep = str(extra["path"]).replace("\\", "/").strip().lstrip("/")
                if ep and ep not in task_candidates:
                    task_candidates.append(ep)
            elif isinstance(extra, str) and extra.strip():
                ep = extra.replace("\\", "/").strip().lstrip("/")
                if ep and ep not in task_candidates:
                    task_candidates.append(ep)

        # Plan-derived file budget: the contract covers exactly the files the
        # task is expected to create/modify (including its D024 companion test)
        # instead of a fixed limit.
        implementation_estimate = wo.get("implementation_estimate")
        if not implementation_estimate:
            implementation_estimate = derive_implementation_estimate(
                title, desc, deliv.get("path"), task_candidates,
                deliverable_type=deliv.get("type"),
                stack=tech_stack,
            )
        else:
            implementation_estimate = dict(implementation_estimate)
        if primary_agent in ("local-llm", "gitops") and implementation_estimate:
            # The release flow always writes the release documentation pair.
            for rel in ("VERSION.md", "CHANGELOG.md"):
                if rel not in implementation_estimate["expected_files"]:
                    implementation_estimate["expected_files"].append(rel)
            implementation_estimate["max_files_touched"] = len(
                implementation_estimate["expected_files"]
            )

        # Resolve skills: preserve Architect's explicit designation if provided,
        # otherwise match intelligently based on role, deliverable, and keywords.
        explicit_wo_skills = wo.get("skills") or []
        resolved_skills = select_skills_for_task(
            title=title,
            description=desc,
            agent_id=primary_agent,
            role=role,
            deliverable_path=deliv.get("path"),
            explicit_skills=explicit_wo_skills,
            project_path=ws,
        )

        contract_rec = build_child_contract(
            wo_id=wo_id,
            agent_id=primary_agent,
            role=role,
            deliverable_path=deliv.get("path"),
            candidate_paths=task_candidates,
            max_files_touched=(
                implementation_estimate["max_files_touched"] if implementation_estimate else None
            ),
            skills=resolved_skills if resolved_skills else None,
            title=title,
            description=desc,
        )

        wo_rec = build_child_work_order(
            wo_id=wo_id,
            title=title,
            description=desc,
            assigned_agents=[primary_agent],
            dependencies=deps,
            priority=prio,
            deliverable=deliv,
            implementation_estimate=implementation_estimate,
            milestone_id=wo.get("milestone_id"),
            skills=resolved_skills if resolved_skills else None,
            deliverables=wo.get("deliverables"),
        )

        # Provenance marker: bootstrap-synthesized artifacts are scaffolding an
        # authorized author may supersede via the authoring gate.
        wo_rec["synthesized_by"] = "bootstrap"
        contract_rec["synthesized_by"] = "bootstrap"

        wo_yaml = yaml.safe_dump(wo_rec, sort_keys=False)
        contract_yaml = yaml.safe_dump(contract_rec, sort_keys=False)

        rel_wo_path = f".sync/work-orders/ACTIVE/{wo_id}.yaml"
        rel_contract_path = f".sync/contracts/{wo_id}.yaml"

        # Validate against AuthoringGate (acting on behalf of architecture)
        wo_dec = gate.validate_artifact_content(rel_wo_path, wo_yaml, agent="architecture", project_root=ws)
        if not wo_dec.passed:
            raise ValueError(f"Synthesized work order '{wo_id}' failed authoring gate: {wo_dec.summary}")

        contract_dec = gate.validate_artifact_content(rel_contract_path, contract_yaml, agent="architecture", project_root=ws)
        if not contract_dec.passed:
            raise ValueError(f"Synthesized contract for '{wo_id}' failed authoring gate: {contract_dec.summary}")

        # Write to disk
        (active_dir / f"{wo_id}.yaml").write_text(wo_yaml, encoding="utf-8")
        (contracts_dir / f"{wo_id}.yaml").write_text(contract_yaml, encoding="utf-8")
        created_records.append(wo_rec)

    # 3. Update .sync/work-orders/INDEX.yaml
    index_path = ws / ".sync" / "work-orders" / "INDEX.yaml"
    index_data: dict[str, Any] = {"schema_version": 1, "next_id": len(created_records) + 1, "orders": []}
    if index_path.exists():
        try:
            loaded_index = yaml.safe_load(index_path.read_text(encoding="utf-8"))
            if isinstance(loaded_index, dict) and "orders" in loaded_index:
                index_data = loaded_index
        except Exception:
            pass

    existing_order_ids = {
        item.get("id") for item in index_data.get("orders", []) if isinstance(item, dict)
    }
    for rec in created_records:
        rec_id = rec["id"]
        if rec_id not in existing_order_ids:
            order_entry: dict[str, Any] = {
                "id": rec_id,
                "type": rec["type"],
                "title": rec["title"],
                "status": rec["status"],
                "priority": rec["priority"],
                "assigned_agents": rec["assigned_agents"],
                "dependencies": rec["dependencies"],
                "deliverable": rec["deliverable"],
                "created": rec["created"],
                "updated": rec["updated"],
                "file": f"work-orders/ACTIVE/{rec_id}.yaml",
            }
            if rec.get("deliverables"):
                order_entry["deliverables"] = rec["deliverables"]
            index_data.setdefault("orders", []).append(order_entry)
            existing_order_ids.add(rec_id)

    max_id_num = 0
    pattern = re.compile(r"^WO-([0-9]{3,})$")
    for item in index_data.get("orders", []):
        if isinstance(item, dict) and item.get("id"):
            m = pattern.match(str(item["id"]))
            if m:
                max_id_num = max(max_id_num, int(m.group(1)))
    index_data["next_id"] = max_id_num + 1

    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(yaml.safe_dump(index_data, sort_keys=False), encoding="utf-8")

    # 4. Update .sync/runtime/TREE.yaml
    tree_path = ws / ".sync" / "runtime" / "TREE.yaml"
    tree_data: dict[str, Any] = {}
    if tree_path.exists():
        try:
            loaded_tree = yaml.safe_load(tree_path.read_text(encoding="utf-8"))
            if isinstance(loaded_tree, dict):
                tree_data = loaded_tree
        except Exception:
            pass

    tree_data.setdefault("schema_version", 1)
    tree_data.setdefault("tree_version", 1)
    tree_data.setdefault("release", "3.1.0")
    agents_map = tree_data.setdefault("agents", {})
    if not isinstance(agents_map, dict):
        agents_map = {}
        tree_data["agents"] = agents_map

    for rec in created_records:
        rec_id = rec["id"]
        for agent in rec.get("assigned_agents", []):
            agent_info = agents_map.setdefault(agent, {
                "session_count": 0,
                "status": "idle",
                "assigned_work_orders": [],
            })
            assigned_list = agent_info.setdefault("assigned_work_orders", [])
            if rec_id not in assigned_list:
                assigned_list.append(rec_id)

    tree_path.parent.mkdir(parents=True, exist_ok=True)
    tree_path.write_text(yaml.safe_dump(tree_data, sort_keys=False), encoding="utf-8")

    return created_records


def reconcile_active_work_orders_with_plan(workspace: Path | str) -> list[str]:
    """Reconcile active work orders and contracts against PLAN.md milestones.

    Aligns agent assignment mismatches (e.g., when keyword-based synthesis
    assigned 'gemma' to a backend task whose plan milestone specifies 'codex').
    Updates:
    - .sync/work-orders/ACTIVE/<WO-ID>.yaml
    - .sync/contracts/<WO-ID>.yaml
    - .sync/work-orders/INDEX.yaml
    - .sync/runtime/TREE.yaml

    Returns:
        List of work order IDs that were reconciled.
    """
    ws = Path(workspace).resolve()
    active_dir = ws / ".sync" / "work-orders" / "ACTIVE"
    if not active_dir.is_dir():
        return []

    from validators.harness.authoring_readiness import (
        load_plan_milestones,
        _wo_matches_milestone,
        _is_meta_work_order,
        _parse_wo_index,
    )
    from validators.kernel.io import durable_write_yaml
    from cli.init import ensure_canonical_tree

    plan_milestones = load_plan_milestones(ws)
    if not plan_milestones:
        return []

    reconciled_ids: list[str] = []
    superseded_map: dict[str, str | None] = {}
    role_map = {
        "codex": "backend",
        "gemini": "frontend",
        "gemma": "qa",
        "local-llm": "gitops",
        "claude": "architecture",
    }

    def _sync_index_status(wo_id: str, new_status: str, ts: str) -> None:
        index_path = ws / ".sync" / "work-orders" / "INDEX.yaml"
        if not index_path.is_file():
            return
        try:
            index_data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
            if isinstance(index_data, dict) and "orders" in index_data:
                for ord_entry in index_data["orders"]:
                    if isinstance(ord_entry, dict) and ord_entry.get("id") == wo_id:
                        ord_entry["status"] = new_status
                        ord_entry["updated"] = ts
                index_data["total_active"] = sum(
                    1 for o in index_data["orders"]
                    if isinstance(o, dict) and str(o.get("status", "")).upper() == "ACTIVE"
                )
                index_data["total_completed"] = sum(
                    1 for o in index_data["orders"]
                    if isinstance(o, dict) and str(o.get("status", "")).upper() == "COMPLETED"
                )
                index_data["total_blocked"] = sum(
                    1 for o in index_data["orders"]
                    if isinstance(o, dict) and str(o.get("status", "")).upper() == "BLOCKED"
                )
                durable_write_yaml(index_path, index_data)
        except Exception:
            pass

    def _sync_tree_remove(wo_id: str) -> None:
        try:
            tree_data = ensure_canonical_tree(ws)
            if "agents" in tree_data and isinstance(tree_data["agents"], dict):
                for ag_info in tree_data["agents"].values():
                    if isinstance(ag_info, dict):
                        orders = ag_info.get("assigned_work_orders", [])
                        if wo_id in orders:
                            orders.remove(wo_id)
                durable_write_yaml(ws / ".sync" / "runtime" / "TREE.yaml", tree_data)
        except Exception:
            pass

    def _cancel_wo(wo_file: Path, wo_data: dict[str, Any], wo_id: str, reason: str) -> None:
        now_str = _now()
        wo_data["status"] = "CANCELLED"
        wo_data["error"] = reason
        wo_data["cancellation_reason"] = reason
        wo_data["updated"] = now_str
        durable_write_yaml(wo_file, wo_data)
        _sync_index_status(wo_id, "CANCELLED", now_str)
        _sync_tree_remove(wo_id)
        if wo_id not in reconciled_ids:
            reconciled_ids.append(wo_id)

    def _wo_precedence(cand: tuple[Path, str, dict[str, Any], Any]) -> tuple[int, int, str]:
        _file, _id, _data, _ = cand
        has_plan_ref = 1 if _data.get("plan_ref") else 0
        is_bootstrap = 1 if str(_data.get("synthesized_by", "")).lower() == "bootstrap" else 0
        type_score = 2 if has_plan_ref else (0 if is_bootstrap else 1)
        idx = _parse_wo_index(_id) or 0
        ts = str(_data.get("updated") or _data.get("created") or "")
        return (type_score, idx, ts)

    active_files = sorted(active_dir.glob("*.yaml"))

    # Pass 1: Parse and categorize all active work orders
    matched_by_milestone: dict[str, list[tuple[Path, str, dict[str, Any], Any]]] = {}

    for wo_file in active_files:
        try:
            wo_data = yaml.safe_load(wo_file.read_text(encoding="utf-8"))
            if not isinstance(wo_data, dict):
                continue
            wo_id = str(wo_data.get("id") or wo_file.stem)
            current_status = str(wo_data.get("status", "")).upper()
            if current_status == "CANCELLED":
                # Check if this work order is actually an approved task in PLAN.md
                # that was erroneously cancelled by prior deduplication
                is_in_plan = (
                    bool(plan_raw_text and f"({wo_id})" in plan_raw_text)
                    or any(wo_id == r.get("id") for r in plan_milestones)
                )
                if is_in_plan:
                    current_status = "ACTIVE"
                    wo_data["status"] = "ACTIVE"
                    wo_data.pop("error", None)
                    wo_data.pop("cancellation_reason", None)
                    durable_write_yaml(wo_file, wo_data)
                    _sync_index_status(wo_id, "ACTIVE", _now())
                else:
                    continue
            elif current_status in ("COMPLETED", "FAILED"):
                continue
            if _is_meta_work_order(wo_data):
                continue

            matched_ref = next((r for r in plan_milestones if _wo_matches_milestone(wo_data, r)), None)
            if matched_ref is None:
                # Rule 1: Orphaned active work order not present in current PLAN.md -> CANCEL
                _cancel_wo(wo_file, wo_data, wo_id, "Orphaned work order not present in current PLAN.md")
                superseded_map[wo_id] = None
                continue

            m_key = str(matched_ref.get("id") or matched_ref.get("title") or "")
            matched_by_milestone.setdefault(m_key, []).append((wo_file, wo_id, wo_data, matched_ref))
        except Exception:
            continue

    # Pass 2: Milestone component and duplicate resolution (Rule 1b)
    # A milestone can contain multiple distinct component work orders (e.g. package.json,
    # styles.css, main.js). Group and deduplicate only work orders that claim the SAME
    # deliverable path or identical title within the milestone.
    surviving_entries: list[tuple[Path, str, dict[str, Any], Any]] = []
    for m_key, cands in matched_by_milestone.items():
        task_groups: dict[str, list[tuple[Path, str, dict[str, Any], Any]]] = {}
        for cand in cands:
            d_spec = cand[2].get("deliverable")
            p_val = (
                str(d_spec.get("path", "")).replace("\\", "/").strip().lstrip("/").lower()
                if isinstance(d_spec, dict)
                else ""
            )
            if p_val:
                t_key = f"path:{p_val}"
            else:
                t_key = f"title:{str(cand[2].get('title', '')).strip().lower()}"
            task_groups.setdefault(t_key, []).append(cand)

        for t_key, group in task_groups.items():
            if len(group) > 1:
                group.sort(key=_wo_precedence, reverse=True)
                canonical = group[0]
                surviving_entries.append(canonical)
                for s_file, s_id, s_data, _ in group[1:]:
                    _cancel_wo(s_file, s_data, s_id, f"Superseded by {canonical[1]} for milestone {m_key} ({t_key})")
                    superseded_map[s_id] = canonical[1]
            elif group:
                surviving_entries.append(group[0])

    # Pass 3: Parallel Deliverable Collision Resolution (Rule 1c)
    # If surviving parallel work orders claim the same deliverable path without dependency,
    # keep the higher precedence one and cancel the colliding predecessor.
    deliv_buckets: dict[str, list[tuple[Path, str, dict[str, Any], Any]]] = {}
    for entry in surviving_entries:
        d_spec = entry[2].get("deliverable")
        if isinstance(d_spec, dict) and d_spec.get("path"):
            norm_p = str(d_spec["path"]).replace("\\", "/").strip().lstrip("/").lower()
            if norm_p:
                deliv_buckets.setdefault(norm_p, []).append(entry)

    final_survivors: list[tuple[Path, str, dict[str, Any], Any]] = []
    for entry in surviving_entries:
        d_spec = entry[2].get("deliverable")
        norm_p = (
            str(d_spec.get("path", "")).replace("\\", "/").strip().lstrip("/").lower()
            if isinstance(d_spec, dict)
            else ""
        )
        bucket = deliv_buckets.get(norm_p, [])
        if len(bucket) > 1:
            bucket.sort(key=_wo_precedence, reverse=True)
            if entry != bucket[0]:
                _cancel_wo(entry[0], entry[2], entry[1], f"Superseded by {bucket[0][1]} (duplicate deliverable '{norm_p}')")
                superseded_map[entry[1]] = bucket[0][1]
                continue
        final_survivors.append(entry)

    # Pass 3b: Reconcile dependencies among surviving work orders
    surviving_ids = {entry[1] for entry in final_survivors}
    for wo_file, wo_id, wo_data, _ in final_survivors:
        deps = wo_data.get("dependencies")
        if isinstance(deps, list) and deps:
            new_deps: list[str] = []
            deps_changed = False
            for d in deps:
                d_str = str(d).strip()
                target_d = superseded_map.get(d_str, d_str)
                if target_d is None or target_d == wo_id:
                    deps_changed = True
                    continue
                if target_d in surviving_ids:
                    if target_d not in new_deps:
                        new_deps.append(target_d)
                    if target_d != d_str:
                        deps_changed = True
                else:
                    deps_changed = True
            if deps_changed:
                wo_data["dependencies"] = new_deps
                durable_write_yaml(wo_file, wo_data)
                if wo_id not in reconciled_ids:
                    reconciled_ids.append(wo_id)

    # Pass 3c: Ensure contracts exist for all surviving work orders
    contracts_dir = ws / ".sync" / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    for wo_file, wo_id, wo_data, _ in final_survivors:
        c_file = contracts_dir / f"{wo_id}.yaml"
        c_agents = wo_data.get("assigned_agents") or []
        c_agent = str(c_agents[0]) if c_agents else "backend"
        c_role = "QA" if c_agent == "gemma" else ("Frontend" if c_agent == "gemini" else ("GitOps" if c_agent == "local-llm" else "Backend"))
        c_deliv = wo_data.get("deliverable") or {}
        c_deliv_path = c_deliv.get("path") if isinstance(c_deliv, dict) else None
        if not c_file.is_file():
            c_rec = build_child_contract(
                wo_id=wo_id,
                agent_id=c_agent,
                role=c_role,
                deliverable_path=c_deliv_path,
                candidate_paths=[c_deliv_path] if c_deliv_path else None,
                max_files_touched=wo_data.get("implementation_estimate", {}).get("max_files_touched") if isinstance(wo_data.get("implementation_estimate"), dict) else 5,
                skills=wo_data.get("skills"),
                title=wo_data.get("title", ""),
                description=wo_data.get("description", ""),
            )
            c_rec["synthesized_by"] = "bootstrap"
            durable_write_yaml(c_file, c_rec)
        else:
            try:
                c_data = yaml.safe_load(c_file.read_text(encoding="utf-8"))
                if isinstance(c_data, dict):
                    scope = c_data.setdefault("scope", {})
                    allow = scope.setdefault("allow", [])
                    c_changed = False
                    if c_deliv_path and not any((r.get("module") == c_deliv_path if isinstance(r, dict) else r == c_deliv_path) for r in allow):
                        allow.append({"module": c_deliv_path})
                        c_changed = True
                    if c_agent == "gemma" and not any(("tests" in str(r.get("module") if isinstance(r, dict) else r)) for r in allow):
                        allow.append({"module": "tests/**"})
                        c_changed = True
                    if c_changed:
                        durable_write_yaml(c_file, c_data)
            except Exception:
                pass

    # Pass 4: Unblock canonical survivors that were blocked solely by gate collisions (Rule 1d)
    for wo_file, wo_id, wo_data, matched_ref in final_survivors:
        c_status = str(wo_data.get("status", "")).upper()
        err_msg = str(wo_data.get("error") or wo_data.get("blocked_reason") or "")
        if c_status == "BLOCKED" and (
            "Authoring readiness gate" in err_msg
            or "OVERLAPPING_FILE_OWNERSHIP" in err_msg
            or "exhausted" in err_msg.lower()
        ):
            now_str = _now()
            wo_data["status"] = "ACTIVE"
            wo_data.pop("error", None)
            wo_data.pop("blocked_reason", None)
            wo_data["updated"] = now_str
            durable_write_yaml(wo_file, wo_data)
            _sync_index_status(wo_id, "ACTIVE", now_str)
            if wo_id not in reconciled_ids:
                reconciled_ids.append(wo_id)

    # Pass 5: Reconcile agent assignments on active survivors
    for wo_file, wo_id, wo_data, matched_ref in final_survivors:
        try:
            current_assigned = wo_data.get("assigned_agents", [])
            primary_agent = current_assigned[0] if current_assigned else ""
            deliv = wo_data.get("deliverable", {})
            deliv_path = str(deliv.get("path", "") if isinstance(deliv, dict) else "")
            is_test_path = deliv_path.startswith("tests/") or "test" in Path(deliv_path).name

            # QA deliverable normalization: if a QA work order delivers a doc (.md)
            # or non-executable test report, normalize it to an executable test suite
            # so QA_DELIVERABLE_NOT_EXECUTABLE cannot fail the gate.
            is_qa_wo = (
                primary_agent in ("gemma", "qa", "q/a")
                or str(wo_data.get("type", "")).upper() in ("VALIDATION", "TEST")
                or bool(matched_ref and str(matched_ref.get("agent", "")).lower() == "gemma")
            )
            if is_qa_wo:
                deliv = wo_data.get("deliverable")
                if not isinstance(deliv, dict):
                    deliv = {}
                    wo_data["deliverable"] = deliv
                cur_type = str(deliv.get("type", "")).lower()
                cur_path = str(deliv.get("path", "")).strip().replace("\\", "/")
                if cur_type != "code" or cur_path.endswith(".md") or not cur_path.endswith(".py"):
                    desc = str(wo_data.get("description", ""))
                    m_py = re.search(r"tests/[A-Za-z0-9_\-]+\.py", desc)
                    test_script = m_py.group(0) if m_py else "tests/test_qa_verification.py"
                    deliv["type"] = "code"
                    deliv["path"] = test_script
                    if not deliv.get("description"):
                        deliv["description"] = f"Automated QA test suite ({test_script})."
                    durable_write_yaml(wo_file, wo_data)
                    if wo_id not in reconciled_ids:
                        reconciled_ids.append(wo_id)
                    # Ensure companion contract allows this test script
                    c_file = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
                    if c_file.is_file():
                        try:
                            c_data = yaml.safe_load(c_file.read_text(encoding="utf-8"))
                            if isinstance(c_data, dict):
                                scope = c_data.setdefault("scope", {})
                                allow = scope.setdefault("allow", [])
                                if not any((r.get("module") == test_script if isinstance(r, dict) else r == test_script) for r in allow):
                                    allow.append({"module": test_script})
                                    durable_write_yaml(c_file, c_data)
                        except Exception:
                            pass

            target_agent = None
            if matched_ref and matched_ref.get("agent"):
                target_agent = matched_ref["agent"].strip().lower()
            elif primary_agent == "gemma" and deliv_path and not is_test_path:
                # Non-test code assigned to gemma -> route to gemini or codex
                if any(deliv_path.endswith(ext) for ext in (".html", ".css", ".js", ".jsx", ".tsx")):
                    target_agent = "gemini"
                else:
                    target_agent = "codex"

            if target_agent and target_agent != primary_agent:
                wo_data["assigned_agents"] = [target_agent]
                durable_write_yaml(wo_file, wo_data)

                # Reconcile matching contract
                contract_file = ws / ".sync" / "contracts" / f"{wo_id}.yaml"
                if contract_file.is_file():
                    try:
                        c_data = yaml.safe_load(contract_file.read_text(encoding="utf-8"))
                        if isinstance(c_data, dict):
                            c_data["agent_id"] = target_agent
                            ident = c_data.setdefault("identity", {})
                            if isinstance(ident, dict):
                                ident["role"] = role_map.get(target_agent, "backend")
                                ident["reports_to"] = "claude"
                            durable_write_yaml(contract_file, c_data)
                    except Exception:
                        pass

                # Update INDEX.yaml
                index_path = ws / ".sync" / "work-orders" / "INDEX.yaml"
                if index_path.is_file():
                    try:
                        index_data = yaml.safe_load(index_path.read_text(encoding="utf-8"))
                        if isinstance(index_data, dict) and "orders" in index_data:
                            for ord_entry in index_data["orders"]:
                                if isinstance(ord_entry, dict) and ord_entry.get("id") == wo_id:
                                    ord_entry["assigned_agents"] = [target_agent]
                            durable_write_yaml(index_path, index_data)
                    except Exception:
                        pass

                # Update TREE.yaml
                try:
                    tree_data = ensure_canonical_tree(ws)
                    if "agents" in tree_data and isinstance(tree_data["agents"], dict):
                        agents_map = tree_data["agents"]
                        if primary_agent in agents_map:
                            p_orders = agents_map[primary_agent].get("assigned_work_orders", [])
                            if wo_id in p_orders:
                                p_orders.remove(wo_id)
                        t_info = agents_map.setdefault(target_agent, {"assigned_work_orders": [], "status": "idle", "session_count": 0})
                        t_orders = t_info.setdefault("assigned_work_orders", [])
                        if wo_id not in t_orders:
                            t_orders.append(wo_id)
                        durable_write_yaml(ws / ".sync" / "runtime" / "TREE.yaml", tree_data)
                except Exception:
                    pass

                reconciled_ids.append(wo_id)
        except Exception:
            continue

    # Phase 2: Check for uncovered plan milestones and synthesize missing work orders
    try:
        existing_wos = []
        for sub in ("ACTIVE", "COMPLETED"):
            d = ws / ".sync" / "work-orders" / sub
            if d.is_dir():
                for f in d.glob("*.yaml"):
                    if f.name == ".gitkeep":
                        continue
                    try:
                        data = yaml.safe_load(f.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            if str(data.get("status", "")).upper() in ("CANCELLED", "FAILED"):
                                continue
                            existing_wos.append(data)
                    except Exception:
                        pass

        plan_file = ws / "PLAN.md"
        parsed_plan = None
        if plan_file.is_file():
            try:
                from validators.harness.plan import parse_plan
                parsed_plan = parse_plan(plan_file.read_text(encoding="utf-8"), project_path=ws)
            except Exception:
                pass

        uncovered_milestones = [
            m for m in plan_milestones
            if not any(_wo_matches_milestone(w, m) for w in existing_wos)
        ]

        if uncovered_milestones:
            gate = AuthoringGate(project_root=ws)
            contracts_dir = ws / ".sync" / "contracts"
            contracts_dir.mkdir(parents=True, exist_ok=True)

            tech_stack = parsed_plan.tech_stack if parsed_plan else None

            for m in uncovered_milestones:
                m_title = m.get("title", "")
                m_obj = None
                if parsed_plan and parsed_plan.milestones:
                    m_obj = next((pm for pm in parsed_plan.milestones if pm.title == m_title), None)
                if m_obj is None:
                    from validators.harness.plan import PlanMilestone
                    m_obj = PlanMilestone(
                        id=m.get("id", ""),
                        title=m_title,
                        tasks=[],
                        agent_hint=m.get("agent"),
                    )

                start_idx = get_next_work_order_int(ws)
                prev_wo_id = f"WO-{start_idx - 1:03d}" if start_idx > 1 else None

                child_wos, _ = decompose_milestone(
                    m_obj,
                    start_idx=start_idx,
                    stack=tech_stack,
                    project_path=ws,
                    prev_wo_id=prev_wo_id,
                )

                for wo in child_wos:
                    wo_id = str(wo.get("id"))
                    assigned = wo.get("assigned_agents", ["codex"])
                    primary_agent = assigned[0] if assigned else "codex"
                    w_title = wo.get("title", m_title)
                    w_desc = wo.get("description", w_title)
                    w_deps = wo.get("dependencies", [])
                    w_prio = wo.get("priority", "P1")
                    w_deliv = wo.get("deliverable") or {}

                    w_role = "backend"
                    if primary_agent == "gemini":
                        w_role = "frontend"
                    elif primary_agent == "gemma":
                        w_role = "qa"
                    elif primary_agent in ("local-llm", "gitops"):
                        w_role = "gitops"

                    task_candidates = extract_candidate_paths(f"{w_title}\n{w_desc}")
                    if w_deliv.get("path"):
                        deliv_p = str(w_deliv["path"]).replace("\\", "/").strip().lstrip("/")
                        if deliv_p and deliv_p not in task_candidates:
                            task_candidates.append(deliv_p)

                    impl_est = derive_implementation_estimate(
                        w_title, w_desc, w_deliv.get("path"), task_candidates,
                        deliverable_type=w_deliv.get("type"),
                        stack=tech_stack,
                    )
                    if primary_agent in ("local-llm", "gitops") and impl_est:
                        for rel in ("VERSION.md", "CHANGELOG.md"):
                            if rel not in impl_est["expected_files"]:
                                impl_est["expected_files"].append(rel)
                        impl_est["max_files_touched"] = len(impl_est["expected_files"])

                    resolved_skills = select_skills_for_task(
                        title=w_title,
                        description=w_desc,
                        agent_id=primary_agent,
                        role=w_role,
                        deliverable_path=w_deliv.get("path"),
                        explicit_skills=wo.get("skills") or [],
                        project_path=ws,
                    )

                    contract_rec = build_child_contract(
                        wo_id=wo_id,
                        agent_id=primary_agent,
                        role=w_role,
                        deliverable_path=w_deliv.get("path"),
                        candidate_paths=task_candidates,
                        max_files_touched=impl_est["max_files_touched"] if impl_est else None,
                        skills=resolved_skills if resolved_skills else None,
                        title=w_title,
                        description=w_desc,
                    )

                    wo_rec = build_child_work_order(
                        wo_id=wo_id,
                        title=w_title,
                        description=w_desc,
                        assigned_agents=[primary_agent],
                        dependencies=w_deps,
                        priority=w_prio,
                        deliverable=w_deliv,
                        implementation_estimate=impl_est,
                        milestone_id=wo.get("milestone_id"),
                        skills=resolved_skills if resolved_skills else None,
                    )
                    wo_rec["synthesized_by"] = "reconcile"
                    contract_rec["synthesized_by"] = "reconcile"

                    from validators.kernel.io import durable_write_yaml
                    wo_file_path = active_dir / f"{wo_id}.yaml"
                    contract_file_path = contracts_dir / f"{wo_id}.yaml"

                    durable_write_yaml(wo_file_path, wo_rec)
                    durable_write_yaml(contract_file_path, contract_rec)
                    existing_wos.append(wo_rec)
                    reconciled_ids.append(wo_id)

                    index_path = ws / ".sync" / "work-orders" / "INDEX.yaml"
                    if index_path.is_file():
                        try:
                            index_data = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
                            orders = index_data.setdefault("orders", [])
                            if not any(o.get("id") == wo_id for o in orders if isinstance(o, dict)):
                                orders.append({
                                    "id": wo_id,
                                    "type": wo_rec.get("type", "FEATURE"),
                                    "title": w_title,
                                    "status": "ACTIVE",
                                    "priority": w_prio,
                                    "assigned_agents": [primary_agent],
                                    "dependencies": w_deps,
                                    "deliverable": w_deliv,
                                    "created": wo_rec.get("created"),
                                    "updated": wo_rec.get("updated"),
                                    "file": f"work-orders/ACTIVE/{wo_id}.yaml",
                                })
                                index_data["next_id"] = start_idx + 1
                                durable_write_yaml(index_path, index_data)
                        except Exception:
                            pass

                    try:
                        from cli.init import ensure_canonical_tree
                        tree_data = ensure_canonical_tree(ws)
                        agents_map = tree_data.setdefault("agents", {})
                        a_info = agents_map.setdefault(primary_agent, {"assigned_work_orders": [], "status": "idle", "session_count": 0})
                        a_orders = a_info.setdefault("assigned_work_orders", [])
                        if wo_id not in a_orders:
                            a_orders.append(wo_id)
                        durable_write_yaml(ws / ".sync" / "runtime" / "TREE.yaml", tree_data)
                    except Exception:
                        pass
    except Exception:
        pass

    try:
        from validators.harness.runner import HarnessRunner
        HarnessRunner._sync_index_and_tree_yaml(ws)
    except Exception:
        pass

    return reconciled_ids
