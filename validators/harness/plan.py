"""PLAN.md generation, structural validation, and downstream usability parsing (Phase C)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class PlanValidationError(ValueError):
    """Raised when a generated PLAN.md fails structural validation."""
    pass


# Trailing "(Agent: <id>)" annotation on a milestone title — metadata, not title text.
_MILESTONE_AGENT_ANNOTATION_RE = re.compile(r"\(\s*(?:Agent|agent)\s*:\s*([^)]+)\)\s*$")
_MILESTONE_SKILLS_ANNOTATION_RE = re.compile(r"\(\s*(?:Skills?|skills?)\s*:\s*([^)]+)\)")


PLAN_GENERATION_INSTRUCTIONS = (
    "When generating or updating PLAN.md, you MUST strictly structure it as follows:\n\n"
    "# Project Plan: <project-name>\n\n"
    "## Current Architecture\n"
    "<overview of system components, boundaries, and dependencies>\n"
    "Stack & Dependencies: Explicitly specify runtime stack and all third-party dependencies.\n\n"
    "## Milestones & Roadmap\n"
    "- [ ] Milestone 1: <Milestone Name> (Agent: <codex|gemini|gemma|local-llm>) (Skills: <skill-name>)\n"
    "  - [ ] Task 1.1: <actionable task description>\n"
    "  - [ ] Task 1.2: <actionable task description>\n"
    "- [ ] Milestone 2: <Milestone Name> (Agent: <codex|gemini|gemma|local-llm>) (Skills: <skill-name>)\n"
    "  - [ ] Task 2.1: <actionable task description>\n\n"
    "Structural requirements:\n"
    "1. Top-level '# ' heading must contain 'Plan' (e.g. '# Project Plan: <name>').\n"
    "2. Must include a '## Current Architecture' (or '## Architecture') section.\n"
    "3. Must include a '## Milestones & Roadmap' (or '## Roadmap') section.\n"
    "4. Milestones and tasks must use markdown checklist syntax ('- [ ]' or '- [x]').\n"
    "5. Agent & Skill Assignment Decision: As Senior Architect, you decide the best specialist agent and applicable procedural skills for each milestone:\n"
    "   - `(Agent: codex)` (Role: backend): Backend implementation, algorithms, modules, and unit tests.\n"
    "   - `(Agent: gemini)` (Role: frontend): Web views, UI components, HTML/CSS/templates, frontend client state.\n"
    "   - `(Agent: gemma)` (Role: qa): Test suite execution, verification, coverage analysis, and quality audits.\n"
    "   - `(Agent: local-llm)` (Role: gitops): Release commits, version bumping, changelog updates, and git hygiene.\n"
    "   - `(Skills: <skill>)`: Assign active procedural skills from the library (e.g. `(Skills: ponytail)` for lazy/minimal/clean code or `(Skills: database_migration)` for schema changes).\n"
    "   Always append the chosen agent and skills in parentheses to the milestone title, e.g. `- [ ] Milestone 1: Implementation of Rate Limiter (Agent: codex) (Skills: ponytail)`.\n"
    "6. Architecture must explicitly decide third-party dependencies and sequence an explicit scaffolding Work Order before dependent feature Work Orders.\n"
)


ARCHITECTURE_RESEARCH_INSTRUCTIONS = (
    "## Architecture Research & Workflow Requirements\n\n"
    "You are Claude, the Senior Architect. Before drafting an architecture plan or authoring tasks, "
    "you MUST follow this strict multi-step sequence:\n\n"
    "### Step 1: Mandatory Codebase Research (FIRST ACTION)\n"
    "- In your FIRST action, you MUST call the `query_graph` tool (e.g. `query_graph(query='architecture')` "
    "or `query_graph(query='auth')`) to discover existing modules, patterns, and codebase conventions.\n"
    "- Do NOT call `write_file` until you have first called `query_graph` and reviewed the response.\n"
    "- Dynamic Tool Activation: If you need additional specialized tools to understand the codebase "
    "(such as `git_log` or `git_diff` to see how architecture evolved, `find_callers` or `impact_analysis` "
    "for dependency structure, or linters to identify existing constraints), call `request_tools(query='git')` "
    "or `request_tools(query='<tool_name>')` to dynamically activate them.\n"
    "- Web Research & Documentation: You have access to `web_search(query='...')` and `web_fetch(url='...')` "
    "to look up framework documentation, library APIs, and latest technical specifications when designing the architecture stack.\n\n"
    "### Step 2: Write PLAN.md (AFTER RESEARCH)\n"
    "- Once research results are returned, use `write_file` to write the complete project plan to `PLAN.md`.\n"
    "- The file MUST strictly conform to the PLAN.md formatting specification below.\n"
    "- Agent Allocation: For each milestone in 'Milestones & Roadmap', decide which agent is best suited:\n"
    "  * Codex (Agent: codex) for backend implementation, business logic, and code modules.\n"
    "  * Gemini (Agent: gemini) for frontend UI, client state, and templates.\n"
    "  * Gemma (Agent: gemma) for QA verification, test execution, and test coverage.\n"
    "  * Local-LLM (Agent: local-llm) for GitOps, version tags, changelog, and release commits.\n"
    "  Append `(Agent: <agent>)` to each milestone title.\n"
    "- Dependency & Stack Decision: Explicitly specify all required third-party packages in PLAN.md. "
    "When external dependencies are needed, plan an explicit scaffolding Work Order to create or update "
    "the dependency manifest (requirements.txt or pyproject.toml) BEFORE any implementation Work Orders.\n\n"
    "### Step 3: Complete the Turn\n"
    "- After writing `PLAN.md`, conclude the turn by emitting the final JSON decision payload "
    "declaring status 'completed' and modified_files ['PLAN.md'].\n"
)


WORK_ORDER_SCHEMA_TEMPLATE = (
    "### Work Order Schema Specification (.sync/work-orders/ACTIVE/WO-xxx.yaml)\n"
    "When authoring work orders for downstream worker agents, each file MUST be valid YAML "
    "conforming to schemas/work-order.schema.json.\n\n"
    "#### Dependency Scaffolding Work Order Pattern:\n"
    "When introducing third-party dependencies, author an explicit scaffolding Work Order FIRST:\n"
    "```yaml\n"
    "id: \"WO-001\"                         # Required: regex ^WO-[0-9]{3}$ matching filename stem\n"
    "milestone_id: \"Milestone 1\"           # Matching plan milestone id from PLAN.md (e.g. \"Milestone 1\")\n"
    "type: \"FEATURE\"                      # Scaffolding task type\n"
    "title: \"Configure Project Dependencies\"\n"
    "status: \"ACTIVE\"                     # Required: ACTIVE\n"
    "priority: \"P0\"                       # Scaffolding runs first\n"
    "assigned_agents:\n"
    "  - \"codex\"\n"
    "dependencies: []                     # No prior dependencies\n"
    "deliverable:\n"
    "  type: \"config\"                     # config deliverable type\n"
    "  path: \"requirements.txt\"            # or pyproject.toml\n"
    "  description: \"Project dependency manifest declaring required packages\"\n"
    "description: >\n"
    "  Create requirements.txt declaring required third-party packages.\n"
    "```\n\n"
    "#### Implementation Work Order Pattern (Dependent on Scaffolding):\n"
    "```yaml\n"
    "id: \"WO-002\"                         # Required: regex ^WO-[0-9]{3}$ matching filename stem\n"
    "milestone_id: \"Milestone 2\"           # Matching plan milestone id from PLAN.md (e.g. \"Milestone 2\")\n"
    "type: \"FEATURE\"                      # Required: PHASE, FEATURE, BUGFIX, HOTFIX, REFACTOR, RESEARCH, AUDIT, VALIDATION, FIX\n"
    "title: \"Implement Login API Endpoint\" # Required: concise task title\n"
    "status: \"ACTIVE\"                     # Required: READY, PENDING, ACTIVE, BLOCKED, COMPLETE, COMPLETED\n"
    "priority: \"P1\"                       # Required: P0, P1, P2, P3\n"
    "assigned_agents:                     # Required: list of worker agent IDs\n"
    "  - \"codex\"                          # e.g. \"codex\" for backend, \"gemini\" for frontend\n"
    "dependencies: [\"WO-001\"]             # Required: depends on scaffolding WO\n"
    "deliverable:                         # Recommended deliverable specification\n"
    "  type: \"code\"                       # code, doc, config, module\n"
    "  path: \"src/api/auth.py\"\n"
    "  description: \"Login authentication endpoint handler\"\n"
    "description: >                       # Detailed implementation requirements for the worker\n"
    "  Implement the POST /login endpoint with credential validation.\n"
    "acceptance_criteria:                 # Concrete verifiable acceptance criteria (MUST specify interface contracts)\n"
    "  - \"Handler validates username and password credentials against database\"\n"
    "  - \"Returns 200 with JWT token on success and 401 on invalid credentials\"\n"
    "```\n\n"
    "#### Cross-Component Integration Guidance:\n"
    "When decomposing a multi-file feature (e.g. HTML, CSS, JS), the parent Work Order (e.g. index.html) MUST define "
    "explicit `acceptance_criteria` specifying the container markup, classes, and element IDs (e.g. #animated-name) "
    "that downstream scripts and styles hook into. Downstream Work Orders must reference those exact IDs. "
    "Never leave parent deliverables as empty placeholder scaffolding.\n"
)


CONTRACT_SCHEMA_TEMPLATE = (
    "### Contract Schema Specification (.sync/contracts/WO-xxx.yaml)\n"
    "Every work order MUST have a corresponding governing contract conforming to schemas/contract.schema.json.\n\n"
    "#### Scaffolding Contract (Authorizes Dependency Manifest Authoring):\n"
    "```yaml\n"
    "schema_version: 1                    # Schema version integer\n"
    "agent_id: \"codex\"                    # Required: worker agent ID\n"
    "work_order: \"WO-001\"                 # Matching scaffolding work order ID\n"
    "identity:\n"
    "  role: \"backend\"\n"
    "  reports_to: \"claude\"\n"
    "scope:\n"
    "  allow:                             # Explicitly authorizes manifest paths\n"
    "    - module: \"requirements.txt\"\n"
    "  deny:\n"
    "    - module: \".git/**\"\n"
    "  write: \"read-write\"\n"
    "budget:\n"
    "  max_files_touched: 5\n"
    "  max_tokens: 30000\n"
    "```\n\n"
    "#### Implementation Contract (Confined Scope - Manifest Denied/Excluded):\n"
    "```yaml\n"
    "schema_version: 1                    # Schema version integer\n"
    "agent_id: \"codex\"                    # Required: worker agent ID (e.g. codex, gemini)\n"
    "work_order: \"WO-002\"                 # Required: matching work order ID (regex ^WO-[0-9]{3}$)\n"
    "identity:\n"
    "  role: \"backend\"                    # Worker role\n"
    "  reports_to: \"claude\"\n"
    "scope:\n"
    "  allow:                             # Confined strictly to application deliverables\n"
    "    - module: \"src/**\"\n"
    "  deny:                              # Manifests excluded from implementation worker scope\n"
    "    - module: \".git/**\"\n"
    "  write: \"read-write\"                # Required: read-write or read-only\n"
    "budget:\n"
    "  max_files_touched: 10\n"
    "  max_tokens: 30000\n"
    "```\n"
)


@dataclass
class TechStack:
    backend_language: str | None = None   # "javascript", "typescript", "python", "go", "rust"
    backend_framework: str | None = None  # "express", "flask", "fastapi", "django", "nest"
    frontend_framework: str | None = None # "react", "vue", "angular", "svelte", "html"
    database: str | None = None           # "postgresql", "mysql", "sqlite", "mongodb"
    dependency_manifest: str | None = None # "package.json", "requirements.txt", "pyproject.toml", "Cargo.toml", "go.mod"
    is_ambiguous: bool = False
    ambiguity_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_language": self.backend_language,
            "backend_framework": self.backend_framework,
            "frontend_framework": self.frontend_framework,
            "database": self.database,
            "dependency_manifest": self.dependency_manifest,
            "is_ambiguous": self.is_ambiguous,
            "ambiguity_reason": self.ambiguity_reason,
        }


@dataclass
class PlanMilestone:
    id: str
    title: str
    status: str  # "COMPLETED" or "PENDING"
    tasks: list[str] = field(default_factory=list)
    agent: str | None = None  # semantic agent metadata parsed from "(Agent: <id>)"
    skills: list[str] = field(default_factory=list)  # semantic skills chosen by architect "(Skills: <id>)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status,
            "tasks": list(self.tasks),
            "agent": self.agent,
            "skills": list(self.skills),
        }


@dataclass
class PlanStructure:
    project_name: str
    title: str
    architecture_overview: str
    milestones: list[PlanMilestone] = field(default_factory=list)
    raw_content: str = ""
    tech_stack: TechStack | None = None

    def to_dict(self) -> dict[str, Any]:
        data = {
            "project_name": self.project_name,
            "title": self.title,
            "architecture_overview": self.architecture_overview,
            "milestones": [m.to_dict() for m in self.milestones],
        }
        if self.tech_stack:
            data["tech_stack"] = self.tech_stack.to_dict()
        return data

    @property
    def milestone_ids(self) -> list[str]:
        return [m.id for m in self.milestones]

    def get_milestone(self, milestone_id: str) -> PlanMilestone | None:
        for m in self.milestones:
            if m.id.lower() == milestone_id.lower() or m.title.lower() == milestone_id.lower():
                return m
        return None


def validate_plan_structure(content: str) -> tuple[bool, list[str]]:
    """Validate that PLAN.md conforms to required architectural and milestone sections.

    Returns:
        tuple of (is_valid, list of error messages)
    """
    errors: list[str] = []
    if not content or not content.strip():
        return False, ["PLAN.md content is empty"]

    # 1. Heading 1 (Title)
    title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    if not title_match:
        errors.append("PLAN.md is missing a top-level '# ' heading (e.g. '# Project Plan: <name>')")
    else:
        title_text = title_match.group(1).lower()
        if "plan" not in title_text:
            errors.append("Top-level heading must reference 'Plan' (e.g. '# Project Plan: <name>')")

    # 2. Architecture Section
    arch_match = re.search(r"^##\s+.*(?:Architecture|Technical Design|Core Technologies).*$", content, re.MULTILINE | re.IGNORECASE)
    if not arch_match:
        errors.append("PLAN.md is missing a '## Current Architecture' (or '## Architecture') section")

    # 3. Milestones & Roadmap Section
    milestones_match = re.search(r"^##\s+.*(?:Milestones|Roadmap).*$", content, re.MULTILINE | re.IGNORECASE)
    if not milestones_match:
        errors.append("PLAN.md is missing a '## Milestones & Roadmap' (or '## Roadmap') section")

    # 4. Actionable Tasks / Milestones Checklists
    checklist_match = re.search(r"^\s*-\s*\[([ xX])\]\s+(.+)$", content, re.MULTILINE)
    if not checklist_match:
        errors.append("PLAN.md must contain at least one checklist item (e.g. '- [ ] Milestone 1: ...')")

    return len(errors) == 0, errors


def parse_tech_stack(content: str, project_path: Path | str | None = None) -> TechStack:
    """Parse technology stack, frameworks, and manifests from PLAN.md and existing project config."""
    # 1. Search for architecture/tech stack sections
    arch_match = re.search(
        r"^##\s+.*(?:Architecture|Technical Design|Core Technologies|Stack)[^\n]*\n(.*?)(?=^##|\Z)",
        content,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    arch_text = arch_match.group(1).strip() if arch_match else ""
    full_text = f"{arch_text}\n{content}"

    # Extract explicit key-value fields if present
    backend_val = ""
    frontend_val = ""
    db_val = ""

    b_match = re.search(r"(?:^|\n)\s*[-*]?\s*\**backend(?:\s+stack)?\**\s*:\s*([^\n]+)", full_text, re.IGNORECASE)
    if b_match:
        backend_val = b_match.group(1).strip()

    f_match = re.search(r"(?:^|\n)\s*[-*]?\s*\**frontend(?:\s+stack)?\**\s*:\s*([^\n]+)", full_text, re.IGNORECASE)
    if f_match:
        frontend_val = f_match.group(1).strip()

    d_match = re.search(r"(?:^|\n)\s*[-*]?\s*\**database\**\s*:\s*([^\n]+)", full_text, re.IGNORECASE)
    if d_match:
        db_val = d_match.group(1).strip()

    search_scope = arch_text or content

    # Check for explicit ambiguity/conflict in backend_val
    if backend_val:
        b_lower = backend_val.lower()
        b_node = bool(re.search(r"\b(node(?:\.js)?|express(?:\.js)?|javascript)\b", b_lower))
        b_py = bool(re.search(r"\b(python(?:3)?|flask|fastapi|django)\b", b_lower))
        b_go = bool(re.search(r"\b(go|golang)\b", b_lower))
        b_rust = bool(re.search(r"\b(rust)\b", b_lower))

        competing = [k for k, v in [("node", b_node), ("python", b_py), ("go", b_go), ("rust", b_rust)] if v]
        if len(competing) > 1:
            return TechStack(
                is_ambiguous=True,
                ambiguity_reason=f"Conflicting backend technologies explicitly declared: {', '.join(competing)}",
            )

    backend_lang = None
    backend_fw = None
    manifest = None

    if backend_val:
        b_lower = backend_val.lower()
        if re.search(r"\b(node(?:\.js)?|express(?:\.js)?|javascript)\b", b_lower):
            backend_lang = "typescript" if re.search(r"\btypescript\b", b_lower) else "javascript"
            if re.search(r"\bexpress(?:\.js)?\b", b_lower):
                backend_fw = "express"
            elif re.search(r"\bnest(?:\.js)?\b", b_lower):
                backend_fw = "nest"
            manifest = "package.json"
        elif re.search(r"\b(python(?:3)?|flask|fastapi|django)\b", b_lower):
            backend_lang = "python"
            if re.search(r"\bflask\b", b_lower):
                backend_fw = "flask"
            elif re.search(r"\bfastapi\b", b_lower):
                backend_fw = "fastapi"
            elif re.search(r"\bdjango\b", b_lower):
                backend_fw = "django"
            manifest = "requirements.txt"
        elif re.search(r"\b(go|golang)\b", b_lower):
            backend_lang = "go"
            manifest = "go.mod"
        elif re.search(r"\brust\b", b_lower):
            backend_lang = "rust"
            manifest = "Cargo.toml"

    # If backend not determined from explicit line, check project on disk
    if not backend_lang and project_path:
        pp = Path(project_path).resolve()
        if (pp / "package.json").is_file():
            backend_lang = "javascript"
            manifest = "package.json"
        elif (pp / "requirements.txt").is_file() or (pp / "pyproject.toml").is_file():
            backend_lang = "python"
            manifest = "requirements.txt" if (pp / "requirements.txt").is_file() else "pyproject.toml"
        elif (pp / "Cargo.toml").is_file():
            backend_lang = "rust"
            manifest = "Cargo.toml"
        elif (pp / "go.mod").is_file():
            backend_lang = "go"
            manifest = "go.mod"

    # If still not determined, check architecture overview
    if not backend_lang:
        scope_lower = search_scope.lower()
        has_node = bool(re.search(r"\b(node(?:\.js)?|express(?:\.js)?)\b", scope_lower))
        has_python = bool(re.search(r"\b(python(?:3)?|flask|fastapi|django)\b", scope_lower))
        has_go = bool(re.search(r"\b(golang|go\.mod)\b", scope_lower))
        has_rust = bool(re.search(r"\b(rust|cargo\.toml)\b", scope_lower))

        active = [k for k, v in [("node", has_node), ("python", has_python), ("go", has_go), ("rust", has_rust)] if v]
        if len(active) == 1:
            cand = active[0]
            if cand == "node":
                backend_lang = "typescript" if re.search(r"\btypescript\b", scope_lower) else "javascript"
                if re.search(r"\bexpress(?:\.js)?\b", scope_lower):
                    backend_fw = "express"
                manifest = "package.json"
            elif cand == "python":
                backend_lang = "python"
                if re.search(r"\bflask\b", scope_lower):
                    backend_fw = "flask"
                elif re.search(r"\bfastapi\b", scope_lower):
                    backend_fw = "fastapi"
                elif re.search(r"\bdjango\b", scope_lower):
                    backend_fw = "django"
                manifest = "requirements.txt"
            elif cand == "go":
                backend_lang = "go"
                manifest = "go.mod"
            elif cand == "rust":
                backend_lang = "rust"
                manifest = "Cargo.toml"
        elif len(active) > 1:
            arch_b_node = bool(re.search(r"\b(node(?:\.js)?|express(?:\.js)?)\b", arch_text.lower()))
            arch_b_py = bool(re.search(r"\b(python(?:3)?|flask|fastapi|django)\b", arch_text.lower()))
            if arch_b_node and arch_b_py:
                return TechStack(
                    is_ambiguous=True,
                    ambiguity_reason="Contradictory backend stack specified: both Node.js and Python referenced in architecture section",
                )

    # Detect frontend framework
    target_frontend_text = f"{frontend_val} {search_scope}".lower()
    frontend_fw = None
    if re.search(r"\breact(?:\.js)?\b", target_frontend_text):
        frontend_fw = "react"
    elif re.search(r"\bvue(?:\.js)?\b", target_frontend_text):
        frontend_fw = "vue"
    elif re.search(r"\bsvelte\b", target_frontend_text):
        frontend_fw = "svelte"
    elif re.search(r"\bhtml\b", target_frontend_text):
        frontend_fw = "html"

    # Database
    database = None
    target_db_text = f"{db_val} {search_scope}".lower()
    if re.search(r"\bpostgres(?:ql)?\b", target_db_text):
        database = "postgresql"
    elif re.search(r"\bsqlite\b", target_db_text):
        database = "sqlite"
    elif re.search(r"\bmongo(?:db)?\b", target_db_text):
        database = "mongodb"
    elif re.search(r"\bmysql\b", target_db_text):
        database = "mysql"

    return TechStack(
        backend_language=backend_lang,
        backend_framework=backend_fw,
        frontend_framework=frontend_fw,
        database=database,
        dependency_manifest=manifest,
        is_ambiguous=False,
    )


def parse_plan(content: str, project_path: Path | str | None = None) -> PlanStructure:
    """Parse PLAN.md into structured data usable downstream by Architect and TUI."""
    content = content.replace("\r\n", "\n")
    is_valid, errors = validate_plan_structure(content)
    if not is_valid:
        raise PlanValidationError(f"Invalid PLAN.md structure: {'; '.join(errors)}")

    # Extract title and project name
    title_match = re.search(r"^#\s+(.+)$", content, re.MULTILINE)
    title = title_match.group(1).strip() if title_match else "Project Plan"
    project_name = ""
    if ":" in title:
        project_name = title.split(":", 1)[1].strip()
    else:
        project_name = title

    # Extract Architecture overview
    arch_match = re.search(
        r"^##\s+.*(?:Architecture|Technical Design|Core Technologies)[^\n]*\n(.*?)(?=^##|\Z)",
        content,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    architecture_overview = arch_match.group(1).strip() if arch_match else ""

    # Extract Milestones
    milestones: list[PlanMilestone] = []
    milestone_section_match = re.search(
        r"^##\s+.*(?:Milestones|Roadmap)[^\n]*\n(.*?)(?=^##|\Z)",
        content,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    )
    if milestone_section_match:
        section_text = milestone_section_match.group(1)
        current_milestone: PlanMilestone | None = None
        for line in section_text.splitlines():
            line_str = line.rstrip()
            if not line_str.strip():
                continue
            # Top-level milestone: starts at column 0 with '- ['
            if line_str.startswith("- ["):
                m_match = re.match(r"^-\s*\[([ xX])\]\s+([^:]+)(?::\s*(.*))?$", line_str)
                if m_match:
                    checked = m_match.group(1).lower() == "x"
                    m_id_or_title = m_match.group(2).strip()
                    m_extra = (m_match.group(3) or "").strip()
                    if m_extra:
                        m_id = m_id_or_title
                        m_title = m_extra
                    else:
                        m_id = f"M{len(milestones) + 1}"
                        m_title = m_id_or_title

                    # Separate skills and agent metadata from the semantic title:
                    # "(Skills: <id>)" and "(Agent: <id>)" are metadata annotations,
                    # not part of the semantic milestone title.
                    m_skills: list[str] = []
                    skills_match = _MILESTONE_SKILLS_ANNOTATION_RE.search(m_title)
                    if skills_match:
                        raw_skills = skills_match.group(1).split(",")
                        m_skills = [s.strip().lower() for s in raw_skills if s.strip()]
                        m_title = (m_title[: skills_match.start()] + m_title[skills_match.end() :]).strip()
                        m_title = m_title.rstrip("-–—,").strip()

                    m_agent: str | None = None
                    agent_match = _MILESTONE_AGENT_ANNOTATION_RE.search(m_title)
                    if agent_match:
                        m_agent = agent_match.group(1).strip().lower()
                        m_title = (m_title[: agent_match.start()] + m_title[agent_match.end() :]).strip()
                        m_title = m_title.rstrip("-–—,").strip()

                    current_milestone = PlanMilestone(
                        id=m_id,
                        title=m_title,
                        status="COMPLETED" if checked else "PENDING",
                        tasks=[],
                        agent=m_agent,
                        skills=m_skills,
                    )
                    milestones.append(current_milestone)
            elif current_milestone and re.match(r"^\s{2,}-\s+(?:\[[ xX]\]\s+)?(.*)$", line_str):
                task_text = re.match(r"^\s{2,}-\s+(?:\[[ xX]\]\s+)?(.*)$", line_str).group(1).strip()
                current_milestone.tasks.append(task_text)

    tech_stack = parse_tech_stack(content, project_path=project_path)

    return PlanStructure(
        project_name=project_name,
        title=title,
        architecture_overview=architecture_overview,
        milestones=milestones,
        raw_content=content,
        tech_stack=tech_stack,
    )


def validate_plan_file(path: Path) -> tuple[bool, list[str]]:
    """Validate a PLAN.md file on disk."""
    if not path.exists():
        return False, [f"File not found: {path}"]
    try:
        content = path.read_text(encoding="utf-8")
        return validate_plan_structure(content)
    except Exception as exc:
        return False, [f"Failed to read {path}: {exc}"]


def extract_plan_from_text(raw_text: str, default_title: str = "System Architecture") -> str | None:
    """Extract or normalize a valid PLAN.md markdown string from raw model text.

    Supports:
    - Fenced markdown blocks (```markdown ... ``` or ```plan ... ```)
    - Raw markdown starting with '# Project Plan' (or '# ... Plan')
    - Body text that has '## Architecture' and '## Milestones' sections with tasks,
      automatically prepending '# Project Plan: <default_title>'.
    """
    if not raw_text or not raw_text.strip():
        return None

    text = raw_text.strip()

    # 1. Check for fenced markdown block containing a plan
    fenced_blocks = re.findall(r"```(?:markdown|md|plan)?\s*(\n#[^`]+)```", text, re.DOTALL | re.IGNORECASE)
    for block in fenced_blocks:
        b = block.strip()
        is_val, _ = validate_plan_structure(b)
        if is_val:
            return b

    # 2. Check if text contains a top-level heading '# ... Plan'
    plan_heading_match = re.search(r"(^#\s+.*Plan.*$)", text, re.MULTILINE | re.IGNORECASE)
    if plan_heading_match:
        start_idx = plan_heading_match.start()
        candidate = text[start_idx:].strip()
        # strip trailing json code block if present
        candidate = re.sub(r"```(?:json)?\s*\{.*?\}\s*```\s*$", "", candidate, flags=re.DOTALL).strip()
        is_val, _ = validate_plan_structure(candidate)
        if is_val:
            return candidate

    # 3. If text contains '## Architecture' and '## Milestones' but lacks a top-level '# Project Plan:'
    has_arch = bool(re.search(r"^##\s+.*(?:Architecture|Technical Design|Core Technologies).*$", text, re.MULTILINE | re.IGNORECASE))
    has_milestones = bool(re.search(r"^##\s+.*(?:Milestones|Roadmap).*$", text, re.MULTILINE | re.IGNORECASE))
    has_checklist = bool(re.search(r"^\s*-\s*\[([ xX])\]\s+(.+)$", text, re.MULTILINE))

    if has_arch and has_milestones and has_checklist:
        first_section = re.search(r"^##\s+", text, re.MULTILINE)
        if first_section:
            body = text[first_section.start():].strip()
            body = re.sub(r"```(?:json)?\s*\{.*?\}\s*```\s*$", "", body, flags=re.DOTALL).strip()
            candidate = f"# Project Plan: {default_title}\n\n{body}"
            is_val, _ = validate_plan_structure(candidate)
            if is_val:
                return candidate

    return None
