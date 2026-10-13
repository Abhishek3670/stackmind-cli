"""Skill Consumer and Importer for StackMind.

Ingests procedural skills from:
1. Markdown files (SKILL.md, *.md) with YAML frontmatter or structured steps.
2. JSON manifests (*.json) in native SkillRecord format or simplified schema.
3. YAML manifests (*.yaml, *.yml).
4. Directories containing SKILL.md packages or multiple skill definitions.
5. In-memory markdown/JSON strings.

Supports installing into:
- Centralized global library: ~/.stackmindcli/skills/global/ (default)
- Local workspace repository: <project>/.sync/skills/active/ and manifests/ (--local)
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

import yaml

from validators.kernel.home import get_global_skills_dir, get_stackmind_home
from validators.skill.models import (
    RiskTier,
    SkillApplicability,
    SkillMetrics,
    SkillProvenance,
    SkillRecord,
    SkillStatus,
    SkillStep,
)
from validators.skill.store import SkillStore


def slugify_skill_name(raw: str) -> str:
    """Slugify a skill name to conform strictly to ^[a-z0-9_-]+$."""
    raw = raw.strip()
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw).strip("_-").lower()
    if not slug:
        return "unnamed_skill"
    return slug


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """Parse YAML frontmatter delimited by `---` from markdown content."""
    stripped = content.strip()
    if not stripped.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    raw_yaml = parts[1].strip()
    body = parts[2].strip()

    try:
        data = yaml.safe_load(raw_yaml)
        if isinstance(data, dict):
            return data, body
    except Exception:
        pass

    return {}, body


def _extract_code_block(text: str) -> tuple[str | None, str | None]:
    """Extract language and command from the first fenced code block if present."""
    match = re.search(r"```([a-zA-Z0-9_-]*)\s*\n(.*?)```", text, re.DOTALL)
    if match:
        lang = match.group(1).strip().lower() or "bash"
        cmd = match.group(2).strip()
        return lang, cmd
    return None, None


def _clean_rationale(text: str) -> str:
    """Clean markdown formatting for clean rationale text."""
    # Remove code blocks for cleaner text rationale
    clean = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    # Strip leading markers like 1., *, -
    clean = re.sub(r"^\s*(\d+[\.\)]|[\*\-])\s*", "", clean)
    return " ".join(clean.split()).strip()


def _extract_steps_from_markdown(body: str) -> list[SkillStep]:
    """Extract procedural steps from markdown body.
    
    Looks for:
    1. Numbered items: 1. ... 2. ...
    2. Headings: ## Step 1, ### Action
    3. Code blocks with commands
    4. Fallback: single guided instruction step
    """
    steps: list[SkillStep] = []

    # 1. Look for numbered list blocks: e.g. "1. **Action**: details..."
    # Match items starting with a number and dot/parenthesis at line start
    lines = body.splitlines()
    numbered_blocks: list[tuple[int, list[str]]] = []
    current_block: list[str] = []
    current_num = 0

    num_pattern = re.compile(r"^\s*(\d+)[\.\)]\s+(.*)")
    heading_pattern = re.compile(r"^\s*#{1,6}\s+(.*)")

    for line in lines:
        match = num_pattern.match(line)
        if match:
            if current_block:
                numbered_blocks.append((current_num, current_block))
            current_num = int(match.group(1))
            current_block = [match.group(2)]
        elif heading_pattern.match(line):
            if current_block:
                numbered_blocks.append((current_num, current_block))
                current_block = []
                current_num = 0
        else:
            if current_block:
                current_block.append(line)

    if current_block:
        numbered_blocks.append((current_num, current_block))

    if numbered_blocks:
        step_idx = 1
        for _, b_lines in numbered_blocks:
            block_text = "\n".join(b_lines).strip()
            if not block_text:
                continue

            lang, cmd = _extract_code_block(block_text)
            tool = "bash" if (cmd or lang in ("bash", "sh", "zsh", "shell", "powershell")) else "agent"

            # Extract action name from bold prefix or first sentence
            first_line = b_lines[0].strip()
            bold_match = re.match(r"^\*\*([^\*]+)\*\*[\s:\-–—]*(.*)", first_line)
            if bold_match:
                action = bold_match.group(1).strip()
                remainder = bold_match.group(2).strip()
                rationale_raw = remainder + "\n" + "\n".join(b_lines[1:])
            else:
                # First sentence or up to 80 chars
                first_sent = first_line.split(". ")[0].rstrip(".")
                action = first_sent[:80] if len(first_sent) > 80 else first_sent
                rationale_raw = block_text

            rationale = _clean_rationale(rationale_raw) or action

            steps.append(
                SkillStep(
                    step_index=step_idx,
                    action=action or f"Step {step_idx}",
                    tool=tool,
                    command_template=cmd,
                    rationale=rationale[:300] if rationale else None,
                    expected_outcome=None,
                )
            )
            step_idx += 1

        if steps:
            return steps

    # 2. Look for section headings: ## Heading or ### Heading
    heading_sections: list[tuple[str, list[str]]] = []
    cur_head = ""
    cur_content: list[str] = []

    for line in lines:
        h_match = re.match(r"^#{2,4}\s+(.+)", line)
        if h_match:
            if cur_head and cur_content:
                heading_sections.append((cur_head, cur_content))
            cur_head = h_match.group(1).strip()
            cur_content = []
        elif cur_head:
            cur_content.append(line)

    if cur_head and cur_content:
        heading_sections.append((cur_head, cur_content))

    # Filter out generic documentation headers
    ignored_headers = {
        "license",
        "author",
        "authors",
        "contributing",
        "table of contents",
        "overview",
        "introduction",
    }
    filtered_sections = [
        (h, c) for h, c in heading_sections if h.lower() not in ignored_headers and "\n".join(c).strip()
    ]

    if filtered_sections:
        for idx, (h_title, c_lines) in enumerate(filtered_sections, 1):
            sec_text = "\n".join(c_lines).strip()
            lang, cmd = _extract_code_block(sec_text)
            tool = "bash" if cmd else "agent"
            rationale = _clean_rationale(sec_text)
            steps.append(
                SkillStep(
                    step_index=idx,
                    action=h_title[:80],
                    tool=tool,
                    command_template=cmd,
                    rationale=rationale[:300] if rationale else None,
                    expected_outcome=None,
                )
            )
        if steps:
            return steps

    # 3. Fallback: single guided instruction step
    lang, cmd = _extract_code_block(body)
    tool = "bash" if cmd else "agent"
    rationale = _clean_rationale(body)
    return [
        SkillStep(
            step_index=1,
            action="Execute skill procedure",
            tool=tool,
            command_template=cmd,
            rationale=rationale[:300] if rationale else "Follow governed skill instructions",
            expected_outcome="Successfully completed procedural execution",
        )
    ]


def parse_skill_markdown(content: str, filename_or_path: str | Path | None = None) -> SkillRecord:
    """Parse a Markdown skill (e.g., SKILL.md) into a validated SkillRecord."""
    frontmatter, body = parse_frontmatter(content)

    # 1. Resolve name
    raw_name = frontmatter.get("name")
    if not raw_name:
        # Check first H1 heading
        h1_match = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        if h1_match:
            raw_name = h1_match.group(1).strip()
    if not raw_name and filename_or_path:
        p = Path(filename_or_path)
        if p.stem.lower() in ("skill", "readme") and p.parent.name:
            raw_name = p.parent.name
        else:
            raw_name = p.stem
    if not raw_name:
        raw_name = "unnamed_skill"

    name = slugify_skill_name(str(raw_name))

    # 2. Resolve description
    desc = frontmatter.get("description")
    if not desc:
        # First non-header paragraph
        paragraphs = [p.strip() for p in body.split("\n\n") if p.strip() and not p.strip().startswith("#")]
        if paragraphs:
            desc = paragraphs[0].replace("\n", " ")
        else:
            desc = f"Procedural skill for {name}"
    desc = str(desc).strip()

    # 3. Resolve version & risk tier
    version = int(frontmatter.get("version", 1))
    if version < 1:
        version = 1

    risk_raw = str(frontmatter.get("risk_tier") or frontmatter.get("risk") or "low").lower()
    try:
        risk_tier = RiskTier(risk_raw)
    except ValueError:
        risk_tier = RiskTier.LOW

    # 4. Resolve applicability
    app_data = frontmatter.get("applicability")
    if isinstance(app_data, dict):
        preconditions = tuple(str(x) for x in app_data.get("preconditions", ()))
        target_modules = tuple(str(x) for x in app_data.get("target_modules", ("*",)))
        known_exclusions = tuple(str(x) for x in app_data.get("known_exclusions", ()))
        environment = {str(k): str(v) for k, v in app_data.get("environment", {}).items()}
    else:
        preconditions = tuple(str(x) for x in frontmatter.get("preconditions", ()))
        target_modules = tuple(str(x) for x in frontmatter.get("target_modules", ("*",)))
        known_exclusions = tuple(str(x) for x in frontmatter.get("known_exclusions", ()))
        environment = {str(k): str(v) for k, v in frontmatter.get("environment", {}).items()}

    if not target_modules:
        target_modules = ("*",)

    applicability = SkillApplicability(
        preconditions=preconditions,
        environment=environment,
        known_exclusions=known_exclusions,
        target_modules=target_modules,
    )

    # 5. Extract steps
    steps = tuple(_extract_steps_from_markdown(body))

    # 6. Resolve provenance
    author_agent = str(frontmatter.get("author_agent") or frontmatter.get("author") or "skill-consumer")
    created_at = datetime.now(timezone.utc).isoformat()
    provenance = SkillProvenance(
        source_experience_ids=(),
        created_at=created_at,
        author_agent=author_agent,
        promotion_reason="Imported via :consume-skills",
    )

    # 7. Metrics
    metrics = SkillMetrics(
        success_count=1,
        failure_count=0,
        success_rate=1.0,
        confidence_score=0.85,
    )

    constraints = tuple(str(c) for c in frontmatter.get("constraints", ()))
    fallback = frontmatter.get("fallback") or frontmatter.get("fallback_procedure")

    skill_id = SkillRecord.mint_id(name, version)

    return SkillRecord(
        skill_id=skill_id,
        name=name,
        version=version,
        status=SkillStatus.ACTIVE,
        risk_tier=risk_tier,
        description=desc,
        applicability=applicability,
        steps=steps,
        provenance=provenance,
        metrics=metrics,
        constraints=constraints,
        fallback_procedure=str(fallback) if fallback else None,
        schema_version=1,
    )


def parse_skill_json(content: str | dict[str, Any], filename_or_path: str | Path | None = None) -> SkillRecord:
    """Parse JSON skill data into a SkillRecord."""
    if isinstance(content, str):
        data = json.loads(content)
    else:
        data = dict(content)

    # Check if this is already a full SkillRecord dictionary
    if "procedure" in data and "skill_id" in data and "status" in data:
        # Standardize name slug
        data["name"] = slugify_skill_name(data["name"])
        data["skill_id"] = SkillRecord.mint_id(data["name"], int(data.get("version", 1)))
        return SkillRecord.from_dict(data)

    # Simplified format
    raw_name = data.get("name")
    if not raw_name and filename_or_path:
        raw_name = Path(filename_or_path).stem
    name = slugify_skill_name(str(raw_name or "unnamed_skill"))

    version = int(data.get("version", 1))
    risk_raw = str(data.get("risk_tier", "low")).lower()
    try:
        risk_tier = RiskTier(risk_raw)
    except ValueError:
        risk_tier = RiskTier.LOW

    desc = str(data.get("description", f"Procedural skill for {name}"))

    # Applicability
    app_data = data.get("applicability", {})
    target_modules = tuple(app_data.get("target_modules", ("*",)))
    if not target_modules:
        target_modules = ("*",)
    applicability = SkillApplicability(
        preconditions=tuple(app_data.get("preconditions", ())),
        environment=dict(app_data.get("environment", {})),
        known_exclusions=tuple(app_data.get("known_exclusions", ())),
        target_modules=target_modules,
    )

    # Steps
    raw_steps = data.get("steps") or data.get("procedure", {}).get("steps", [])
    parsed_steps: list[SkillStep] = []
    for idx, s in enumerate(raw_steps, 1):
        if isinstance(s, dict):
            action = str(s.get("action", f"Step {idx}"))
            tool = str(s.get("tool", "bash" if s.get("command") or s.get("command_template") else "agent"))
            cmd = s.get("command_template") or s.get("command")
            rationale = s.get("rationale")
            parsed_steps.append(
                SkillStep(
                    step_index=idx,
                    action=action,
                    tool=tool,
                    command_template=str(cmd) if cmd else None,
                    rationale=str(rationale) if rationale else None,
                )
            )
        elif isinstance(s, str):
            parsed_steps.append(
                SkillStep(
                    step_index=idx,
                    action=s[:80],
                    tool="agent",
                    rationale=s,
                )
            )

    if not parsed_steps:
        parsed_steps = [
            SkillStep(
                step_index=1,
                action="Execute procedure",
                tool="agent",
                rationale=desc,
            )
        ]

    created_at = datetime.now(timezone.utc).isoformat()
    provenance = SkillProvenance(
        source_experience_ids=(),
        created_at=created_at,
        author_agent=str(data.get("author", "skill-consumer")),
        promotion_reason="Imported via JSON skill consumer",
    )

    metrics = SkillMetrics(
        success_count=1,
        failure_count=0,
        success_rate=1.0,
        confidence_score=0.85,
    )

    return SkillRecord(
        skill_id=SkillRecord.mint_id(name, version),
        name=name,
        version=version,
        status=SkillStatus.ACTIVE,
        risk_tier=risk_tier,
        description=desc,
        applicability=applicability,
        steps=tuple(parsed_steps),
        provenance=provenance,
        metrics=metrics,
        schema_version=1,
    )


def parse_skill_yaml(content: str | dict[str, Any], filename_or_path: str | Path | None = None) -> SkillRecord:
    """Parse YAML skill data into a SkillRecord."""
    if isinstance(content, str):
        data = yaml.safe_load(content)
    else:
        data = content

    if not isinstance(data, dict):
        raise ValueError("Invalid YAML skill manifest: must be an associative mapping/dict.")

    return parse_skill_json(data, filename_or_path=filename_or_path)


def parse_skill_file(file_path: Path | str) -> SkillRecord:
    """Parse any skill file (.md, .json, .yaml, .yml) into a SkillRecord."""
    p = Path(file_path).resolve()
    if not p.is_file():
        raise FileNotFoundError(f"Skill file not found: {p}")

    ext = p.suffix.lower()
    raw = p.read_text(encoding="utf-8")

    if ext in (".md", ".markdown"):
        return parse_skill_markdown(raw, filename_or_path=p)
    elif ext == ".json":
        return parse_skill_json(raw, filename_or_path=p)
    elif ext in (".yaml", ".yml"):
        return parse_skill_yaml(raw, filename_or_path=p)
    else:
        # Try markdown then JSON
        try:
            return parse_skill_markdown(raw, filename_or_path=p)
        except Exception:
            return parse_skill_json(raw, filename_or_path=p)


def scan_skill_directory(dir_path: Path | str) -> list[SkillRecord]:
    """Recursively scan a directory for skill files (SKILL.md, *.skill.json, *.md)."""
    root = Path(dir_path).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Directory not found: {root}")

    records: list[SkillRecord] = []
    seen_names: set[str] = set()

    # Priority 1: Direct or nested SKILL.md files
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        # Skip internal version manifests and VCS
        parts = p.parts
        if any(part in (".git", ".sync", "node_modules", "__pycache__", ".pytest_cache") for part in parts):
            continue

        if p.name.lower() == "skill.md":
            try:
                rec = parse_skill_file(p)
                if rec.name not in seen_names:
                    records.append(rec)
                    seen_names.add(rec.name)
            except Exception:
                continue

    # Priority 2: Other skill manifests (*.skill.json, *.skill.yaml) or top-level *.md if none found
    if not records:
        for p in root.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() in (".md", ".json", ".yaml", ".yml"):
                try:
                    rec = parse_skill_file(p)
                    if rec.name not in seen_names:
                        records.append(rec)
                        seen_names.add(rec.name)
                except Exception:
                    continue

    return records


def is_url(source: Path | str) -> bool:
    """Check if the source string represents a remote URL (HTTP, HTTPS, Git)."""
    src = str(source).strip()
    return bool(
        src.startswith(("http://", "https://", "git://", "git@", "ssh://"))
        or src.startswith("github.com/")
    )


def fetch_skills_from_url(url: str) -> list[SkillRecord]:
    """Fetch and parse skills from a remote URL (GitHub repo, raw file, or HTTP URL)."""
    import subprocess
    import tempfile
    import urllib.request
    from urllib.parse import urlparse

    url = url.strip()
    if url.startswith("github.com/"):
        url = "https://" + url

    parsed = urlparse(url)
    netloc = parsed.netloc.lower()
    path = parsed.path

    # Check if URL directly targets a file
    is_direct_file = any(
        path.lower().endswith(ext)
        for ext in (".md", ".markdown", ".json", ".yaml", ".yml")
    )

    if "github.com" in netloc and "/blob/" in path:
        # Convert GitHub web blob URL to raw content URL
        # e.g. https://github.com/owner/repo/blob/branch/skills/SKILL.md
        #   -> https://raw.githubusercontent.com/owner/repo/branch/skills/SKILL.md
        raw_path = re.sub(r"^/([^/]+)/([^/]+)/blob/(.+)$", r"/\1/\2/\3", path)
        url = f"https://raw.githubusercontent.com{raw_path}"
        is_direct_file = True

    if is_direct_file or "raw.githubusercontent.com" in netloc:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "stackmind-cli"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                content_bytes = resp.read()
                text = content_bytes.decode("utf-8")
        except Exception as exc:
            raise RuntimeError(f"Failed to download skill file from '{url}': {exc}") from exc

        filename = Path(path).name or "SKILL.md"
        ext = Path(filename).suffix.lower()
        if ext == ".json":
            return [parse_skill_json(text, filename_or_path=filename)]
        elif ext in (".yaml", ".yml"):
            return [parse_skill_yaml(text, filename_or_path=filename)]
        else:
            return [parse_skill_markdown(text, filename_or_path=filename)]

    # Otherwise, treat as git repository (clone and scan)
    clone_url = url
    subpath = ""

    if "github.com" in netloc:
        match_tree = re.match(r"^/([^/]+)/([^/]+)/tree/([^/]+)(?:/(.+))?$", path)
        if match_tree:
            owner, repo, branch, sub = match_tree.groups()
            clone_url = f"https://github.com/{owner}/{repo}.git"
            subpath = sub or ""
        else:
            match_repo = re.match(r"^/([^/]+)/([^/]+?)(?:\.git)?/?$", path)
            if match_repo:
                owner, repo = match_repo.groups()
                clone_url = f"https://github.com/{owner}/{repo}.git"

    with tempfile.TemporaryDirectory(prefix="stackmind_skill_") as tmpdir:
        tmp_path = Path(tmpdir)
        cmd = ["git", "clone", "--depth", "1", clone_url, str(tmp_path)]
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
                check=True,
            )
        except subprocess.CalledProcessError as err:
            err_msg = (err.stderr or err.stdout or str(err)).strip()
            raise RuntimeError(f"Failed to clone git repository '{clone_url}': {err_msg}") from err
        except FileNotFoundError as err:
            raise RuntimeError("'git' command not found. Please install git or use a raw file URL directly.") from err

        scan_target = tmp_path / subpath if subpath else tmp_path
        if not scan_target.exists():
            raise FileNotFoundError(f"Subpath '{subpath}' not found in cloned repository {clone_url}")

        if scan_target.is_file():
            return [parse_skill_file(scan_target)]
        else:
            records = scan_skill_directory(scan_target)
            if not records:
                # Fallback: scan any top-level *.md files
                for md in scan_target.glob("*.md"):
                    try:
                        records.append(parse_skill_file(md))
                    except Exception:
                        pass
            return records


def consume_skills(
    source: Path | str,
    project_path: Path | str | None = None,
    is_global: bool = True,
    status: SkillStatus = SkillStatus.ACTIVE,
) -> list[SkillRecord]:
    """Ingest and register procedural skills into StackMind.
    
    Args:
        source: File path, directory path, remote URL (GitHub repo or raw file), or raw markdown/JSON string.
        project_path: Workspace project path (used for local installation or resolution).
        is_global: If True, install into ~/.stackmindcli/skills/global/ (shared across all projects).
                   If False, install into project_path/.sync/skills/active/.
        status: Initial lifecycle state (defaults to SkillStatus.ACTIVE).

    Returns:
        List of ingested and saved SkillRecord instances.
    """
    store = SkillStore(project_path)
    candidates: list[SkillRecord] = []

    src_str = str(source).strip()

    # 1. Check if source is a remote URL (GitHub repository, raw URL, or HTTP endpoint)
    if is_url(src_str):
        candidates = fetch_skills_from_url(src_str)
    else:
        # Check if source is an existing filesystem path
        p = Path(source)
        if not p.is_absolute() and project_path and not p.exists():
            cand = Path(project_path) / source
            if cand.exists():
                p = cand

        if p.is_file():
            rec = parse_skill_file(p)
            candidates.append(rec)
        elif p.is_dir():
            candidates = scan_skill_directory(p)
        else:
            # Check if source string is inline markdown or JSON content
            if src_str.startswith("{"):
                candidates.append(parse_skill_json(src_str))
            elif src_str.startswith("---") or src_str.startswith("#"):
                candidates.append(parse_skill_markdown(src_str))
            else:
                raise FileNotFoundError(f"Skill source path not found: {source}")

    if not candidates:
        raise ValueError(f"No valid procedural skills could be parsed from: {source}")

    saved_records: list[SkillRecord] = []
    for cand in candidates:
        # Update status if needed
        if cand.status != status:
            cand = SkillRecord(
                skill_id=cand.skill_id,
                name=cand.name,
                version=cand.version,
                status=status,
                risk_tier=cand.risk_tier,
                description=cand.description,
                applicability=cand.applicability,
                steps=cand.steps,
                provenance=cand.provenance,
                metrics=cand.metrics,
                constraints=cand.constraints,
                fallback_procedure=cand.fallback_procedure,
                schema_version=cand.schema_version,
            )

        # Validate schema before persisting
        validation_errors = cand.validate_schema()
        if validation_errors:
            # Raise or log if invalid schema
            err_msg = "; ".join(validation_errors[:3])
            raise ValueError(f"Skill '{cand.name}' failed schema validation: {err_msg}")

        # Persist to destination
        if is_global:
            store.save_global_skill(cand)
        else:
            store.save_version(cand)

        saved_records.append(cand)

    return saved_records


__all__ = [
    "consume_skills",
    "fetch_skills_from_url",
    "is_url",
    "parse_frontmatter",
    "parse_skill_file",
    "parse_skill_json",
    "parse_skill_markdown",
    "parse_skill_yaml",
    "scan_skill_directory",
    "slugify_skill_name",
]
