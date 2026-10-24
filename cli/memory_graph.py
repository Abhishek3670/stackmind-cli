"""Live interactive browser graph visualizer for StackMind Durable Memory.

Provides an in-browser, real-time force-directed topology view of StackMind's
memory records, agent attributions, experience provenance, and contradiction links.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# Color theme matching StackMind TUI & runtime standards
THEME = {
    "fact": {"color": "#a855f7", "glow": "rgba(168, 85, 247, 0.4)", "label": "Fact"},
    "decision": {"color": "#10b981", "glow": "rgba(16, 185, 129, 0.4)", "label": "Decision"},
    "lesson": {"color": "#f59e0b", "glow": "rgba(245, 158, 11, 0.4)", "label": "Lesson"},
    "pattern": {"color": "#06b6d4", "glow": "rgba(6, 182, 212, 0.4)", "label": "Pattern"},
    "preference": {"color": "#f43f5e", "glow": "rgba(244, 63, 94, 0.4)", "label": "Preference"},
    "agent": {"color": "#38bdf8", "glow": "rgba(56, 189, 248, 0.4)", "label": "Agent"},
    "experience": {"color": "#64748b", "glow": "rgba(100, 116, 139, 0.3)", "label": "Experience"},
    "work_order": {"color": "#60a5fa", "glow": "rgba(96, 165, 250, 0.3)", "label": "Work Order"},
}


def get_demo_memory_graph() -> dict[str, Any]:
    """Provide a rich, realistic multi-agent memory topology for immediate visualization."""
    nodes = [
        # Agents
        {"id": "agent:architecture", "label": "Architecture", "category": "agent", "type": "agent", "agent": "claude", "role": "architecture", "model": "gemma4:31b-cloud"},
        {"id": "agent:backend", "label": "Backend", "category": "agent", "type": "agent", "agent": "codex", "role": "backend", "model": "gemma4:31b-cloud"},
        {"id": "agent:frontend", "label": "Frontend", "category": "agent", "type": "agent", "agent": "gemini", "role": "frontend", "model": "gemma4:31b-cloud"},
        {"id": "agent:qa", "label": "Q/A Lead", "category": "agent", "type": "agent", "agent": "gemma", "role": "qa", "model": "gemma4:31b-cloud"},
        {"id": "agent:gitops", "label": "GitOps", "category": "agent", "type": "agent", "agent": "local-llm", "role": "gitops", "model": "gemma4:31b-cloud"},

        # Work Orders / Experiences
        {"id": "wo:WO-001", "label": "WO-001: Scaffolding", "category": "work_order", "type": "work_order", "status": "completed"},
        {"id": "wo:WO-002", "label": "WO-002: SQLite Core", "category": "work_order", "type": "work_order", "status": "completed"},
        {"id": "wo:WO-004", "label": "WO-004: Auth Routes", "category": "work_order", "type": "work_order", "status": "completed"},
        {"id": "wo:WO-006", "label": "WO-006: DB E2E Tests", "category": "work_order", "type": "work_order", "status": "completed"},
        {"id": "wo:WO-011", "label": "WO-011: Canvas Sync", "category": "work_order", "type": "work_order", "status": "completed"},
        {"id": "wo:WO-027", "label": "WO-027: Swarm Optimization", "category": "work_order", "type": "work_order", "status": "completed"},

        # Facts
        {
            "id": "MEM-f001",
            "label": "SQLite WAL Mode Required",
            "category": "memory",
            "type": "fact",
            "status": "active",
            "confidence": 0.96,
            "agent": "Backend",
            "content": "SQLite concurrent read/write transactions require PRAGMA journal_mode=WAL to avoid database lock contention under agent load.",
            "normalized_content": "sqlite wal pragma concurrent read write",
            "created_at": "2026-10-23T18:30:00Z",
            "tags": ["sqlite", "database", "concurrency"],
            "evidence": ["EXP-sqlite-wal: database_locked errors dropped to 0 after WAL pragma."],
            "dimensions": {"state_verified": True, "behavioral_verified": True, "outcome_verified": True},
        },
        {
            "id": "MEM-f002",
            "label": "JWT Bearer Token Header",
            "category": "memory",
            "type": "fact",
            "status": "active",
            "confidence": 0.98,
            "agent": "Frontend",
            "content": "All protected API requests must supply 'Authorization: Bearer <token>' matching Express passport middleware expectations.",
            "normalized_content": "jwt auth bearer token express passport header",
            "created_at": "2026-10-23T19:15:00Z",
            "tags": ["auth", "jwt", "api"],
            "evidence": ["EXP-jwt-header: 401 unauthorized resolved when Authorization prefix normalized to Bearer."],
            "dimensions": {"security_verified": True, "outcome_verified": True},
        },
        {
            "id": "MEM-f003",
            "label": "Canvas Max Particle Threshold",
            "category": "memory",
            "type": "fact",
            "status": "active",
            "confidence": 0.92,
            "agent": "QA",
            "content": "Single-thread HTML5 2D canvas frame rate drops below 55 FPS when active particle count exceeds 350 without spatial hashing.",
            "normalized_content": "canvas particle fps 55 350 threshold spatial hash",
            "created_at": "2026-10-24T02:10:00Z",
            "tags": ["performance", "canvas", "particles"],
            "evidence": ["EXP-canvas-perf: benchmark_fps dropped to 48fps at N=400 dots."],
            "dimensions": {"behavioral_verified": True, "code_verified": True},
        },

        # Decisions
        {
            "id": "MEM-d001",
            "label": "Zero-Dependency SQLite Selection",
            "category": "memory",
            "type": "decision",
            "status": "active",
            "confidence": 0.95,
            "agent": "Architecture",
            "content": "Chose embedded SQLite over external Postgres for dev bootstrap to allow immediate 'npm install && npm start' without external docker containers.",
            "normalized_content": "embedded sqlite zero dependency bootstrap dev setup",
            "created_at": "2026-10-23T16:00:00Z",
            "tags": ["architecture", "database", "bootstrap"],
            "evidence": ["EXP-arch-bootstrap: clean clone test passed in 14.2s without docker daemon."],
            "dimensions": {"scope_verified": True, "outcome_verified": True},
        },
        {
            "id": "MEM-d002",
            "label": "Spatial Grid Neighborhood Lookup",
            "category": "memory",
            "type": "decision",
            "status": "active",
            "confidence": 0.90,
            "agent": "Backend",
            "content": "Replaced O(N^2) pairwise proximity calculation with a uniform 32px spatial binning grid, reducing distance checks from 45,000 to ~1,200 per tick.",
            "normalized_content": "spatial grid uniform binning o(n^2) optimization",
            "created_at": "2026-10-24T01:50:00Z",
            "tags": ["physics", "algorithm", "performance"],
            "evidence": ["EXP-spatial-bin: tick execution time reduced from 22ms to 2.4ms."],
            "dimensions": {"code_verified": True, "outcome_verified": True},
        },

        # Lessons
        {
            "id": "MEM-l001",
            "label": "Never Edit Binary SQLite File",
            "category": "memory",
            "type": "lesson",
            "status": "active",
            "confidence": 0.99,
            "agent": "QA",
            "content": "Test workers must never author or rewrite database.sqlite directly; modifying binary database files causes contract scope violation. Populate test fixtures via API seed endpoints.",
            "normalized_content": "never modify database.sqlite binary scope violation seed api",
            "created_at": "2026-10-24T08:15:00Z",
            "tags": ["testing", "sqlite", "contract-scope"],
            "evidence": ["EXP-scope-fail: WO-006 failed with CONTRACT_SCOPE_VIOLATION when test script touched sqlite binary directly."],
            "dimensions": {"security_verified": True, "behavioral_verified": True},
        },
        {
            "id": "MEM-l002",
            "label": "Route Path Uniformity Requirement",
            "category": "memory",
            "type": "lesson",
            "status": "active",
            "confidence": 0.97,
            "agent": "Architecture",
            "content": "Frontend component fetch requests must use normalized '/api/...' paths matching Express route mount namespaces. Route contract mismatches break integration review.",
            "normalized_content": "route contract mismatch express mount frontend fetch api",
            "created_at": "2026-10-24T13:30:00Z",
            "tags": ["routing", "integration", "api"],
            "evidence": ["EXP-integration-review: 3 route contract mismatches halted delivery pipeline."],
            "dimensions": {"scope_verified": True, "code_verified": True},
        },

        # Superseded Memory (Evolution example)
        {
            "id": "MEM-old-001",
            "label": "Direct SQLite File Mocking (Obsolete)",
            "category": "memory",
            "type": "lesson",
            "status": "superseded",
            "superseded_by": "MEM-l001",
            "confidence": 0.60,
            "agent": "QA",
            "content": "Initial assumption: QA test fixture can touch database.sqlite directly to reset user credentials.",
            "normalized_content": "touch database.sqlite directly test fixture",
            "created_at": "2026-10-23T21:00:00Z",
            "tags": ["testing", "legacy"],
            "evidence": ["EXP-legacy: Initial draft approach."],
            "dimensions": {},
        },

        # Contradiction Example
        {
            "id": "MEM-c001",
            "label": "Direct DB Truncate Assumption",
            "category": "memory",
            "type": "preference",
            "status": "candidate",
            "confidence": 0.65,
            "agent": "Backend",
            "content": "Workers may directly purge database.sqlite before test runs to ensure deterministic clean state.",
            "normalized_content": "workers purge database.sqlite directly",
            "created_at": "2026-10-24T06:30:00Z",
            "tags": ["testing", "sqlite"],
            "evidence": ["EXP-cont: Experimental worker convention."],
            "dimensions": {},
        },

        # Patterns
        {
            "id": "MEM-p001",
            "label": "Dynamic-First, Coordinate-Fallback",
            "category": "memory",
            "type": "pattern",
            "status": "active",
            "confidence": 0.94,
            "agent": "QA",
            "content": "Author UI tests with dynamic IDs and text selectors first, with absolute coordinates as verified fallback for headless environments.",
            "normalized_content": "dynamic first coordinate fallback ui tests selector",
            "created_at": "2026-10-23T22:00:00Z",
            "tags": ["testing", "patterns", "ui"],
            "evidence": ["EXP-ui-tests: Resilient locator pattern survived resolution shifts."],
            "dimensions": {"behavioral_verified": True, "outcome_verified": True},
        },
    ]

    links = [
        # Memory -> Agent (Authored/Learned)
        {"source": "MEM-f001", "target": "agent:backend", "type": "authored_by", "label": "learned by"},
        {"source": "MEM-f002", "target": "agent:frontend", "type": "authored_by", "label": "learned by"},
        {"source": "MEM-f003", "target": "agent:qa", "type": "authored_by", "label": "learned by"},
        {"source": "MEM-d001", "target": "agent:architecture", "type": "authored_by", "label": "decided by"},
        {"source": "MEM-d002", "target": "agent:backend", "type": "authored_by", "label": "decided by"},
        {"source": "MEM-l001", "target": "agent:qa", "type": "authored_by", "label": "learned by"},
        {"source": "MEM-l002", "target": "agent:architecture", "type": "authored_by", "label": "learned by"},
        {"source": "MEM-old-001", "target": "agent:qa", "type": "authored_by", "label": "authored by"},
        {"source": "MEM-c001", "target": "agent:backend", "type": "authored_by", "label": "authored by"},
        {"source": "MEM-p001", "target": "agent:qa", "type": "authored_by", "label": "standardized by"},

        # Memory -> Work Order (Extracted From)
        {"source": "MEM-f001", "target": "wo:WO-002", "type": "extracted_from", "label": "provenance"},
        {"source": "MEM-d001", "target": "wo:WO-001", "type": "extracted_from", "label": "provenance"},
        {"source": "MEM-f002", "target": "wo:WO-004", "type": "extracted_from", "label": "provenance"},
        {"source": "MEM-l001", "target": "wo:WO-006", "type": "extracted_from", "label": "provenance"},
        {"source": "MEM-f003", "target": "wo:WO-027", "type": "extracted_from", "label": "provenance"},
        {"source": "MEM-d002", "target": "wo:WO-011", "type": "extracted_from", "label": "provenance"},

        # Supersedes (Memory evolution)
        {"source": "MEM-l001", "target": "MEM-old-001", "type": "supersedes", "label": "supersedes"},

        # Contradiction (Red glowing warning line)
        {"source": "MEM-c001", "target": "MEM-l001", "type": "contradicts", "label": "CONTRADICTION"},
    ]

    return {
        "nodes": nodes,
        "links": links,
        "meta": {
            "mode": "demo",
            "total_memories": 9,
            "active_memories": 7,
            "contradictions_count": 1,
            "project_name": "StackMind Multi-Agent Demo Graph",
            "workspace": "demo",
            "timestamp": time.time(),
        },
    }


def collect_project_memory_graph(project_path: Path | str) -> dict[str, Any]:
    """Inspect a project workspace and assemble live memory nodes and relationships."""
    ws = Path(project_path).resolve()
    memory_dir = ws / ".sync" / "memory" / "records"
    exp_dir = ws / ".sync" / "experience" / "records"
    wo_dir = ws / ".sync" / "work-orders"

    nodes: list[dict[str, Any]] = []
    links: list[list[str] | dict[str, Any]] = []
    seen_nodes: set[str] = set()

    memory_files = list(memory_dir.glob("*.json")) if memory_dir.is_dir() else []
    if not memory_files:
        exp_files = list(exp_dir.glob("EXP-*.json")) if exp_dir.is_dir() else []
        if exp_files:
            seen_summaries: set[str] = set()
            agent_roles: set[str] = set()
            wo_refs: set[str] = set()
            sorted_exps = sorted(exp_files, key=lambda p: p.stat().st_mtime, reverse=True)
            for ef in sorted_exps:
                try:
                    ed = json.loads(ef.read_text(encoding="utf-8"))
                    summary = (ed.get("final_state") or {}).get("summary")
                    if not summary or summary in seen_summaries:
                        continue
                    if len(summary) < 10 or summary.startswith("Completed task via"):
                        continue
                    seen_summaries.add(summary)

                    wo_id = (ed.get("initial_state") or {}).get("work_order_id") or "Core"
                    agent_raw = str(ed.get("agent_id") or "Agent")
                    agent_norm = agent_raw.replace("memory-", "").replace("-compiler", "").title()
                    agent_roles.add(agent_norm)
                    wo_refs.add(wo_id)

                    mem_id = f"MEM-{ef.stem.replace('EXP-', '')[:10]}"
                    summ_low = summary.lower()
                    if any(k in summ_low for k in ("decid", "approv", "architect")):
                        m_type = "decision"
                    elif any(k in summ_low for k in ("lesson", "fix", "defect", "fail", "error", "block")):
                        m_type = "lesson"
                    elif any(k in summ_low for k in ("pattern", "structur", "scaffold")):
                        m_type = "pattern"
                    else:
                        m_type = "fact"

                    nodes.append({
                        "id": mem_id,
                        "label": summary[:48] + "…" if len(summary) > 48 else summary,
                        "category": "memory",
                        "type": m_type,
                        "status": "active",
                        "confidence": 0.95 if ed.get("outcome") == "completed" else 0.75,
                        "agent": agent_norm,
                        "content": summary,
                        "normalized_content": f"{wo_id} {agent_norm} {summary[:40]}".lower(),
                        "created_at": ed.get("recorded_at") or "",
                        "tags": [wo_id.lower()],
                        "evidence": [f"{ef.stem}: {summary}"],
                        "dimensions": {"behavioral_verified": True, "outcome_verified": True},
                    })
                    seen_nodes.add(mem_id)

                    links.append({
                        "source": mem_id,
                        "target": f"agent:{agent_norm.lower()}",
                        "type": "authored_by",
                        "label": "learned by",
                    })
                    links.append({
                        "source": mem_id,
                        "target": f"wo:{wo_id}",
                        "type": "extracted_from",
                        "label": "provenance",
                    })
                    if len(nodes) >= 25:
                        break
                except Exception:
                    continue

            for a in agent_roles:
                a_id = f"agent:{a.lower()}"
                if a_id not in seen_nodes:
                    nodes.append({"id": a_id, "label": a, "category": "agent", "type": "agent", "role": a.lower()})
                    seen_nodes.add(a_id)

            for wo in wo_refs:
                wo_node_id = f"wo:{wo}"
                if wo_node_id not in seen_nodes:
                    nodes.append({"id": wo_node_id, "label": wo, "category": "work_order", "type": "work_order"})
                    seen_nodes.add(wo_node_id)

            return {
                "nodes": nodes,
                "links": links,
                "meta": {
                    "mode": "live_experiences",
                    "total_memories": len([n for n in nodes if n.get("category") == "memory"]),
                    "workspace": str(ws),
                    "timestamp": time.time(),
                },
            }

        return {
            "nodes": [],
            "links": [],
            "meta": {
                "mode": "empty",
                "total_memories": 0,
                "workspace": str(ws),
                "timestamp": time.time(),
            },
        }

    agent_roles: set[str] = set()
    wo_refs: set[str] = set()

    # 1. Parse all memory records
    for f in sorted(memory_files):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            mem_id = data.get("memory_id") or f.stem
            m_type = str(data.get("memory_type", "fact")).lower()
            status = str(data.get("status", "active")).lower()
            prov = data.get("provenance", {}) or {}
            agent_raw = str(prov.get("extractor_agent") or "Architecture")
            agent_norm = agent_raw.replace("memory-", "").replace("-compiler", "").title()
            agent_roles.add(agent_norm)

            content = data.get("content", "")
            preview = content[:48] + "…" if len(content) > 48 else content

            evidence_items = []
            for ev in data.get("evidence", []):
                if isinstance(ev, dict) and ev.get("excerpt"):
                    evidence_items.append(ev.get("excerpt"))

            nodes.append({
                "id": mem_id,
                "label": preview or mem_id,
                "category": "memory",
                "type": m_type,
                "status": status,
                "confidence": float(prov.get("confidence", 0.8)),
                "agent": agent_norm,
                "content": content,
                "normalized_content": data.get("normalized_content", ""),
                "created_at": data.get("created_at", ""),
                "tags": data.get("tags", []),
                "evidence": evidence_items,
                "dimensions": {},
            })
            seen_nodes.add(mem_id)

            # Link to extractor agent
            links.append({
                "source": mem_id,
                "target": f"agent:{agent_norm.lower()}",
                "type": "authored_by",
                "label": "learned by",
            })

            # Check source experience/WO references
            for src_exp in prov.get("source_experience_ids", []):
                links.append({
                    "source": mem_id,
                    "target": f"exp:{src_exp}",
                    "type": "extracted_from",
                    "label": "provenance",
                })
                wo_refs.add(src_exp)

            # Superseded links
            for sup in data.get("supersedes", []):
                links.append({
                    "source": mem_id,
                    "target": sup,
                    "type": "supersedes",
                    "label": "supersedes",
                })
        except Exception:
            continue

    # 2. Add Agent nodes
    for a in agent_roles:
        a_id = f"agent:{a.lower()}"
        if a_id not in seen_nodes:
            nodes.append({
                "id": a_id,
                "label": a,
                "category": "agent",
                "type": "agent",
                "role": a.lower(),
            })
            seen_nodes.add(a_id)

    # 3. Add Experience / Work Order nodes referenced by memories
    for ref in wo_refs:
        ref_id = f"exp:{ref}"
        if ref_id not in seen_nodes:
            nodes.append({
                "id": ref_id,
                "label": ref[:12],
                "category": "experience",
                "type": "experience",
            })
            seen_nodes.add(ref_id)

    return {
        "nodes": nodes,
        "links": links,
        "meta": {
            "mode": "live",
            "total_memories": len(memory_files),
            "workspace": str(ws),
            "timestamp": time.time(),
        },
    }


HTML_APP = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>StackMind // Live Memory Graph</title>
  <style>
    :root {
      --bg: #090d16;
      --card-bg: rgba(15, 23, 42, 0.85);
      --card-border: #334155;
      --text: #f8fafc;
      --text-dim: #94a3b8;
      --text-muted: #64748b;
      --fact: #a855f7;
      --decision: #10b981;
      --lesson: #f59e0b;
      --pattern: #06b6d4;
      --preference: #f43f5e;
      --agent: #38bdf8;
      --workorder: #60a5fa;
      --contradict: #ef4444;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "JetBrains Mono", monospace;
      overflow: hidden;
      width: 100vw;
      height: 100vh;
      display: flex;
      flex-direction: column;
    }

    /* Top HUD */
    header {
      height: 54px;
      background: rgba(15, 23, 42, 0.95);
      border-bottom: 1px solid var(--card-border);
      backdrop-filter: blur(12px);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 20px;
      z-index: 20;
    }

    .brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .brand-logo {
      width: 28px;
      height: 28px;
      background: linear-gradient(135deg, #38bdf8, #a855f7);
      border-radius: 6px;
      display: grid;
      place-content: center;
      font-weight: 800;
      font-size: 14px;
      color: #000;
    }
    .brand-title {
      font-weight: 700;
      font-size: 15px;
      letter-spacing: 0.5px;
    }
    .brand-sub {
      color: var(--text-dim);
      font-size: 12px;
      font-weight: 400;
      margin-left: 6px;
    }

    .project-selector-wrapper {
      display: flex;
      align-items: center;
      gap: 8px;
      margin-left: 16px;
      background: rgba(30, 41, 59, 0.75);
      border: 1px solid var(--card-border);
      padding: 4px 12px;
      border-radius: 8px;
    }
    .project-label {
      font-size: 10px;
      color: var(--text-muted);
      font-weight: 700;
      letter-spacing: 0.8px;
    }
    .project-select {
      background: transparent;
      border: none;
      color: #38bdf8;
      font-weight: 600;
      font-size: 13px;
      outline: none;
      cursor: pointer;
      max-width: 220px;
    }
    .project-select option {
      background: #0f172a;
      color: #f8fafc;
    }

    .hud-metrics {
      display: flex;
      gap: 10px;
      align-items: center;
    }
    .pill {
      background: rgba(30, 41, 59, 0.7);
      border: 1px solid var(--card-border);
      border-radius: 9999px;
      padding: 4px 12px;
      font-size: 12px;
      display: flex;
      align-items: center;
      gap: 6px;
    }
    .dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: #10b981;
    }
    .dot.live {
      box-shadow: 0 0 8px #10b981;
      animation: pulse 2s infinite;
    }
    @keyframes pulse {
      0% { opacity: 0.6; }
      50% { opacity: 1; transform: scale(1.1); }
      100% { opacity: 0.6; }
    }

    .btn {
      background: #1e293b;
      border: 1px solid #475569;
      color: var(--text);
      padding: 5px 12px;
      border-radius: 6px;
      font-size: 12px;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .btn:hover { background: #334155; border-color: #64748b; }
    .btn.active { background: #0284c7; border-color: #38bdf8; }

    /* Main Canvas area */
    #workspace {
      position: relative;
      flex: 1;
      width: 100%;
      height: calc(100vh - 54px);
    }
    canvas {
      width: 100%;
      height: 100%;
      display: block;
      cursor: grab;
    }
    canvas:active { cursor: grabbing; }

    /* Floating Controls Bar */
    .controls-overlay {
      position: absolute;
      top: 16px;
      left: 16px;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 12px;
      backdrop-filter: blur(10px);
      z-index: 10;
      display: flex;
      flex-direction: column;
      gap: 10px;
      width: 270px;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
    }
    .search-box {
      width: 100%;
      background: #0f172a;
      border: 1px solid #334155;
      padding: 6px 10px;
      border-radius: 6px;
      color: #fff;
      font-size: 12px;
      outline: none;
    }
    .search-box:focus { border-color: #38bdf8; }

    .filter-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .filter-title {
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
      color: var(--text-muted);
      font-weight: 700;
    }
    .chips {
      display: flex;
      flex-wrap: wrap;
      gap: 4px;
    }
    .chip {
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 4px;
      background: #1e293b;
      border: 1px solid #334155;
      color: var(--text-dim);
      cursor: pointer;
      user-select: none;
    }
    .chip.active {
      color: #fff;
      font-weight: 600;
    }
    .chip[data-type="fact"].active { background: rgba(168, 85, 247, 0.2); border-color: var(--fact); color: var(--fact); }
    .chip[data-type="decision"].active { background: rgba(16, 185, 129, 0.2); border-color: var(--decision); color: var(--decision); }
    .chip[data-type="lesson"].active { background: rgba(245, 158, 11, 0.2); border-color: var(--lesson); color: var(--lesson); }
    .chip[data-type="pattern"].active { background: rgba(6, 182, 212, 0.2); border-color: var(--pattern); color: var(--pattern); }
    .chip[data-type="agent"].active { background: rgba(56, 189, 248, 0.2); border-color: var(--agent); color: var(--agent); }

    .slider-row {
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 11px;
      color: var(--text-dim);
    }
    input[type=range] {
      width: 100%;
      accent-color: #38bdf8;
      cursor: pointer;
    }

    /* Right Detail Drawer */
    .drawer {
      position: absolute;
      top: 16px;
      right: 16px;
      width: 360px;
      max-height: calc(100vh - 86px);
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 8px;
      padding: 16px;
      backdrop-filter: blur(12px);
      z-index: 15;
      display: none;
      flex-direction: column;
      gap: 12px;
      overflow-y: auto;
      box-shadow: 0 10px 30px -5px rgba(0, 0, 0, 0.6);
      animation: slideIn 0.2s ease-out;
    }
    @keyframes slideIn {
      from { transform: translateX(20px); opacity: 0; }
      to { transform: translateX(0); opacity: 1; }
    }
    .drawer-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
    }
    .drawer-close {
      cursor: pointer;
      color: var(--text-muted);
      font-size: 16px;
    }
    .drawer-close:hover { color: #fff; }
    .badge {
      font-size: 10px;
      text-transform: uppercase;
      padding: 2px 8px;
      border-radius: 4px;
      font-weight: 700;
      letter-spacing: 0.5px;
    }
    .drawer-title {
      font-size: 14px;
      font-weight: 600;
      line-height: 1.4;
      margin-top: 4px;
    }
    .drawer-card {
      background: #0f172a;
      border: 1px solid #1e293b;
      border-radius: 6px;
      padding: 10px;
      font-size: 12px;
    }
    .drawer-meta-row {
      display: flex;
      justify-content: space-between;
      padding: 4px 0;
      border-bottom: 1px solid #1e293b;
      font-size: 11px;
    }
    .drawer-meta-label { color: var(--text-muted); }
    .drawer-meta-val { color: var(--text-dim); font-weight: 500; }
    .meter-bar {
      height: 6px;
      background: #1e293b;
      border-radius: 3px;
      overflow: hidden;
      margin-top: 4px;
    }
    .meter-fill {
      height: 100%;
      background: linear-gradient(90deg, #38bdf8, #10b981);
      width: 80%;
      border-radius: 3px;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="brand-logo">SM</div>
      <div>
        <span class="brand-title">StackMind // Memory Graph</span>
        <span class="brand-sub" id="workspaceLabel">live runtime topology</span>
      </div>
    </div>
    <div class="project-selector-wrapper">
      <span class="project-label">WORKSPACE:</span>
      <select id="projectSelect" class="project-select" title="Switch active workspace">
        <option value="">Scanning workspaces...</option>
      </select>
    </div>
    <div class="hud-metrics">
      <div class="pill">
        <div class="dot live"></div>
        <span id="liveStatus">Live Sync: Active</span>
      </div>
      <div class="pill">
        <span style="color: var(--fact)">●</span> Facts: <strong id="factCount">0</strong>
        <span style="color: var(--decision); margin-left:6px;">●</span> Decisions: <strong id="decisionCount">0</strong>
        <span style="color: var(--lesson); margin-left:6px;">●</span> Lessons: <strong id="lessonCount">0</strong>
      </div>
      <button class="btn" id="toggleDemoBtn">Toggle Demo Overlay</button>
      <button class="btn" id="resetZoomBtn">Reset View</button>
    </div>
  </header>

  <div id="workspace">
    <canvas id="graphCanvas"></canvas>

    <!-- Left Controls -->
    <div class="controls-overlay">
      <input type="text" class="search-box" id="searchBox" placeholder="Search memories, facts, agents...">
      <div class="filter-group">
        <span class="filter-title">Type Filters</span>
        <div class="chips">
          <div class="chip active" data-type="fact">Facts</div>
          <div class="chip active" data-type="decision">Decisions</div>
          <div class="chip active" data-type="lesson">Lessons</div>
          <div class="chip active" data-type="pattern">Patterns</div>
          <div class="chip active" data-type="agent">Agents</div>
          <div class="chip active" data-type="work_order">Orders</div>
        </div>
      </div>
      <div class="filter-group">
        <div class="slider-row">
          <span class="filter-title">Min Confidence</span>
          <span id="confVal">0.00</span>
        </div>
        <input type="range" id="confSlider" min="0" max="1" step="0.05" value="0.0">
      </div>
    </div>

    <!-- Right Inspector Drawer -->
    <div class="drawer" id="inspector">
      <div class="drawer-header">
        <span class="badge" id="inspBadge">FACT</span>
        <span class="drawer-close" id="drawerClose">&times;</span>
      </div>
      <div class="drawer-title" id="inspTitle">Memory Title</div>
      <div class="drawer-card" id="inspContent" style="color: #cbd5e1; line-height: 1.5;"></div>
      
      <div>
        <div class="slider-row">
          <span>Confidence Score</span>
          <strong id="inspConf">95%</strong>
        </div>
        <div class="meter-bar">
          <div class="meter-fill" id="inspConfFill"></div>
        </div>
      </div>

      <div class="drawer-card">
        <div class="drawer-meta-row">
          <span class="drawer-meta-label">Memory ID</span>
          <span class="drawer-meta-val" id="inspId">MEM-000</span>
        </div>
        <div class="drawer-meta-row">
          <span class="drawer-meta-label">Learned By</span>
          <span class="drawer-meta-val" id="inspAgent">Architecture</span>
        </div>
        <div class="drawer-meta-row">
          <span class="drawer-meta-label">Status</span>
          <span class="drawer-meta-val" id="inspStatus">ACTIVE</span>
        </div>
        <div class="drawer-meta-row">
          <span class="drawer-meta-label">Created</span>
          <span class="drawer-meta-val" id="inspDate">—</span>
        </div>
      </div>

      <div class="filter-title">Provenance & Verification Evidence</div>
      <div class="drawer-card" id="inspEvidence" style="font-size:11px; color: #94a3b8;">
        Verified via test assertion and scope boundary checks.
      </div>
    </div>
  </div>

  <script>
    const canvas = document.getElementById('graphCanvas');
    const ctx = canvas.getContext('2d');

    const COLOR_MAP = {
      fact: '#a855f7',
      decision: '#10b981',
      lesson: '#f59e0b',
      pattern: '#06b6d4',
      preference: '#f43f5e',
      agent: '#38bdf8',
      work_order: '#60a5fa',
      experience: '#64748b'
    };

    window.FALLBACK_DEMO_DATA = null;
    let nodes = [];
    let links = [];
    let activeFilters = new Set(['fact', 'decision', 'lesson', 'pattern', 'preference', 'agent', 'work_order', 'experience']);
    let minConfidence = 0.0;
    let searchQuery = '';
    let selectedNode = null;
    let hoveredNode = null;
    let transform = { x: 0, y: 0, k: 1 };
    let isDragging = false;
    let dragNode = null;
    let lastMouse = { x: 0, y: 0 };
    let useDemo = false;

    function resize() {
      canvas.width = canvas.parentElement.clientWidth * window.devicePixelRatio;
      canvas.height = canvas.parentElement.clientHeight * window.devicePixelRatio;
      ctx.scale(window.devicePixelRatio, window.devicePixelRatio);
    }
    window.addEventListener('resize', resize);
    resize();

    // Reset center
    transform.x = canvas.parentElement.clientWidth / 2;
    transform.y = canvas.parentElement.clientHeight / 2;

    let currentProject = "";

    async function loadProjects() {
      try {
        const res = await fetch('/api/projects');
        const projectList = await res.json();
        const sel = document.getElementById('projectSelect');
        if (!sel || !Array.isArray(projectList) || projectList.length === 0) return;
        sel.innerHTML = '';
        projectList.forEach(p => {
          const opt = document.createElement('option');
          opt.value = p.path;
          const count = p.experiences || p.memories || 0;
          const countLabel = count ? ` (${count} items)` : '';
          opt.textContent = `${p.name}${countLabel}`;
          if (p.selected) {
            opt.selected = true;
            currentProject = p.path;
          }
          sel.appendChild(opt);
        });
        sel.onchange = (e) => {
          currentProject = e.target.value;
          useDemo = false;
          document.getElementById('toggleDemoBtn').classList.remove('active');
          fetchGraphData();
        };
      } catch (err) {
        console.warn("Could not load projects list:", err);
      }
    }

    async function fetchGraphData() {
      try {
        let url = useDemo ? '/api/graph?demo=1' : '/api/graph';
        if (!useDemo && currentProject) {
          url += (url.includes('?') ? '&' : '?') + 'project=' + encodeURIComponent(currentProject);
        }
        const res = await fetch(url);
        const data = await res.json();
        processData(data);
      } catch (e) {
        console.warn("Polling error:", e);
      }
    }

    function processData(data) {
      document.getElementById('workspaceLabel').textContent = data.meta.workspace || 'live runtime topology';
      
      const existingMap = new Map(nodes.map(n => [n.id, n]));
      nodes = (data.nodes || []).map(n => {
        const prev = existingMap.get(n.id);
        const radius = n.category === 'agent' ? 24 : (n.category === 'work_order' ? 18 : 14 + (n.confidence || 0.8) * 8);
        return {
          ...n,
          radius: radius,
          x: prev ? prev.x : (transform.x + (Math.random() - 0.5) * 300),
          y: prev ? prev.y : (transform.y + (Math.random() - 0.5) * 300),
          vx: prev ? prev.vx : 0,
          vy: prev ? prev.vy : 0,
        };
      });

      const nodeMap = new Map(nodes.map(n => [n.id, n]));
      links = (data.links || []).map(l => {
        return {
          ...l,
          sourceNode: nodeMap.get(typeof l.source === 'object' ? l.source.id : l.source),
          targetNode: nodeMap.get(typeof l.target === 'object' ? l.target.id : l.target)
        };
      }).filter(l => l.sourceNode && l.targetNode);

      // Counts
      let facts = 0, decisions = 0, lessons = 0;
      nodes.forEach(n => {
        if (n.type === 'fact') facts++;
        if (n.type === 'decision') decisions++;
        if (n.type === 'lesson') lessons++;
      });
      document.getElementById('factCount').textContent = facts;
      document.getElementById('decisionCount').textContent = decisions;
      document.getElementById('lessonCount').textContent = lessons;
    }

    // Physics Simulation Loop
    function tickPhysics() {
      const kRepel = 1200;
      const kLink = 0.04;
      const friction = 0.85;

      // Repulsion between nodes
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i];
          const b = nodes[j];
          let dx = b.x - a.x;
          let dy = b.y - a.y;
          let distSq = dx * dx + dy * dy || 1;
          let dist = Math.sqrt(distSq);
          if (dist < 400) {
            let force = kRepel / distSq;
            let fx = (dx / dist) * force;
            let fy = (dy / dist) * force;
            a.vx -= fx;
            a.vy -= fy;
            b.vx += fx;
            b.vy += fy;
          }
        }
      }

      // Link attraction
      for (let l of links) {
        let a = l.sourceNode;
        let b = l.targetNode;
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let dist = Math.sqrt(dx * dx + dy * dy) || 1;
        let targetDist = l.type === 'contradicts' ? 180 : 100;
        let diff = dist - targetDist;
        let force = diff * kLink;
        let fx = (dx / dist) * force;
        let fy = (dy / dist) * force;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }

      // Center gravity & damping
      const cx = canvas.parentElement.clientWidth / 2;
      const cy = canvas.parentElement.clientHeight / 2;
      for (let n of nodes) {
        if (n === dragNode) continue;
        n.vx += (cx - n.x) * 0.001;
        n.vy += (cy - n.y) * 0.001;
        n.vx *= friction;
        n.vy *= friction;
        n.x += n.vx;
        n.y += n.vy;
      }
    }

    function isNodeVisible(n) {
      const t = n.type || n.category;
      if (!activeFilters.has(t) && !activeFilters.has(n.category)) return false;
      if (n.category === 'memory' && (n.confidence || 0) < minConfidence) return false;
      if (searchQuery) {
        const text = (n.label + ' ' + (n.content || '') + ' ' + (n.agent || '')).toLowerCase();
        if (!text.includes(searchQuery)) return false;
      }
      return true;
    }

    // Render loop
    function render() {
      tickPhysics();

      const w = canvas.parentElement.clientWidth;
      const h = canvas.parentElement.clientHeight;
      ctx.save();
      ctx.clearRect(0, 0, w, h);

      if (nodes.length === 0) {
        ctx.restore();
        ctx.save();
        ctx.textAlign = 'center';
        ctx.fillStyle = '#94a3b8';
        ctx.font = '600 16px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
        ctx.fillText('🌱 Clean Workspace (No Records Yet)', w / 2, h / 2 - 16);
        ctx.fillStyle = '#64748b';
        ctx.font = '13px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
        ctx.fillText('As StackMind agents execute work orders, learned facts and decisions will appear here.', w / 2, h / 2 + 12);
        ctx.fillStyle = '#38bdf8';
        ctx.font = '12px -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
        ctx.fillText('Tip: Click [Toggle Demo Overlay] above to preview an interactive sample graph.', w / 2, h / 2 + 38);
        ctx.restore();
        requestAnimationFrame(render);
        return;
      }

      // Pan & Zoom transform
      ctx.translate(transform.x, transform.y);
      ctx.scale(transform.k, transform.k);
      ctx.translate(-w / 2, -h / 2);

      // 1. Draw Links
      for (let l of links) {
        const a = l.sourceNode;
        const b = l.targetNode;
        if (!isNodeVisible(a) || !isNodeVisible(b)) continue;

        const isContradiction = l.type === 'contradicts';
        const isHovered = hoveredNode && (hoveredNode === a || hoveredNode === b);

        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);

        if (isContradiction) {
          ctx.strokeStyle = '#ef4444';
          ctx.lineWidth = isHovered ? 3 : 2;
          ctx.setLineDash([6, 4]);
        } else if (l.type === 'supersedes') {
          ctx.strokeStyle = '#38bdf8';
          ctx.lineWidth = 2;
          ctx.setLineDash([3, 3]);
        } else {
          ctx.strokeStyle = isHovered ? '#38bdf8' : 'rgba(51, 65, 85, 0.6)';
          ctx.lineWidth = isHovered ? 2 : 1;
          ctx.setLineDash([]);
        }
        ctx.stroke();
        ctx.setLineDash([]);
      }

      // 2. Draw Nodes
      for (let n of nodes) {
        if (!isNodeVisible(n)) continue;

        const color = COLOR_MAP[n.type] || COLOR_MAP[n.category] || '#94a3b8';
        const isSel = selectedNode === n;
        const isHov = hoveredNode === n;

        // Outer glow
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.radius + (isSel ? 6 : (isHov ? 4 : 0)), 0, Math.PI * 2);
        ctx.fillStyle = isSel ? 'rgba(56, 189, 248, 0.4)' : (isHov ? 'rgba(255, 255, 255, 0.15)' : 'rgba(15, 23, 42, 0.8)');
        ctx.fill();

        // Base node circle
        ctx.beginPath();
        ctx.arc(n.x, n.y, n.radius, 0, Math.PI * 2);
        ctx.fillStyle = '#0f172a';
        ctx.fill();
        ctx.strokeStyle = color;
        ctx.lineWidth = isSel ? 3 : 2;
        ctx.stroke();

        // Inner icon / symbol
        ctx.fillStyle = color;
        ctx.font = `${Math.round(n.radius * 0.7)}px sans-serif`;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        let sym = '●';
        if (n.category === 'agent') sym = '⚡';
        else if (n.category === 'work_order') sym = 'WO';
        else if (n.type === 'fact') sym = 'F';
        else if (n.type === 'decision') sym = 'D';
        else if (n.type === 'lesson') sym = 'L';
        else if (n.type === 'pattern') sym = 'P';
        ctx.fillText(sym, n.x, n.y);

        // Label below
        ctx.fillStyle = isSel ? '#38bdf8' : (isHov ? '#ffffff' : '#cbd5e1');
        ctx.font = isSel ? 'bold 11px sans-serif' : '10px sans-serif';
        ctx.fillText(n.label, n.x, n.y + n.radius + 12);
      }

      ctx.restore();
      requestAnimationFrame(render);
    }

    // Screen to world coordinates
    function toWorld(clientX, clientY) {
      const rect = canvas.getBoundingClientRect();
      const sx = clientX - rect.left;
      const sy = clientY - rect.top;
      const w = canvas.parentElement.clientWidth;
      const h = canvas.parentElement.clientHeight;
      const wx = (sx - transform.x) / transform.k + (w / 2);
      const wy = (sy - transform.y) / transform.k + (h / 2);
      return { x: wx, y: wy };
    }

    // Node hit testing
    function hitNode(clientX, clientY) {
      const pt = toWorld(clientX, clientY);
      for (let i = nodes.length - 1; i >= 0; i--) {
        const n = nodes[i];
        if (!isNodeVisible(n)) continue;
        const dx = pt.x - n.x;
        const dy = pt.y - n.y;
        if (dx * dx + dy * dy <= (n.radius + 4) * (n.radius + 4)) {
          return n;
        }
      }
      return null;
    }

    // Mouse Interactions
    canvas.addEventListener('mousedown', e => {
      const hit = hitNode(e.clientX, e.clientY);
      if (hit) {
        dragNode = hit;
        selectedNode = hit;
        showInspector(hit);
      } else {
        isDragging = true;
      }
      lastMouse = { x: e.clientX, y: e.clientY };
    });

    window.addEventListener('mousemove', e => {
      if (dragNode) {
        const pt = toWorld(e.clientX, e.clientY);
        dragNode.x = pt.x;
        dragNode.y = pt.y;
        dragNode.vx = 0;
        dragNode.vy = 0;
      } else if (isDragging) {
        transform.x += (e.clientX - lastMouse.x);
        transform.y += (e.clientY - lastMouse.y);
      } else {
        hoveredNode = hitNode(e.clientX, e.clientY);
      }
      lastMouse = { x: e.clientX, y: e.clientY };
    });

    window.addEventListener('mouseup', () => {
      isDragging = false;
      dragNode = null;
    });

    canvas.addEventListener('wheel', e => {
      e.preventDefault();
      const zoomFactor = e.deltaY < 0 ? 1.1 : 0.9;
      transform.k = Math.max(0.3, Math.min(3.0, transform.k * zoomFactor));
    });

    // Node Inspector details
    function showInspector(n) {
      const insp = document.getElementById('inspector');
      insp.style.display = 'flex';
      
      const badge = document.getElementById('inspBadge');
      badge.textContent = (n.type || n.category).toUpperCase();
      badge.style.background = (COLOR_MAP[n.type] || '#38bdf8') + '22';
      badge.style.color = COLOR_MAP[n.type] || '#38bdf8';

      document.getElementById('inspTitle').textContent = n.label;
      document.getElementById('inspContent').textContent = n.content || 'System node representing execution participant or work package.';
      
      const conf = Math.round((n.confidence || 0.8) * 100);
      document.getElementById('inspConf').textContent = conf + '%';
      document.getElementById('inspConfFill').style.width = conf + '%';

      document.getElementById('inspId').textContent = n.id;
      document.getElementById('inspAgent').textContent = n.agent || n.role || '—';
      document.getElementById('inspStatus').textContent = (n.status || 'ACTIVE').toUpperCase();
      document.getElementById('inspDate').textContent = n.created_at ? n.created_at.slice(0, 19).replace('T', ' ') : 'Live session';

      const evDiv = document.getElementById('inspEvidence');
      if (n.evidence && n.evidence.length > 0) {
        evDiv.innerHTML = n.evidence.map(e => `<div style="margin-bottom:4px;">• ${e}</div>`).join('');
      } else {
        evDiv.textContent = 'Standard verified experience telemetry.';
      }
    }

    document.getElementById('drawerClose').onclick = () => {
      document.getElementById('inspector').style.display = 'none';
      selectedNode = null;
    };

    // Filter Chips
    document.querySelectorAll('.chip').forEach(chip => {
      chip.onclick = () => {
        const type = chip.getAttribute('data-type');
        if (activeFilters.has(type)) {
          activeFilters.delete(type);
          chip.classList.remove('active');
        } else {
          activeFilters.add(type);
          chip.classList.add('active');
        }
      };
    });

    // Slider & Search
    document.getElementById('confSlider').oninput = e => {
      minConfidence = parseFloat(e.target.value);
      document.getElementById('confVal').textContent = minConfidence.toFixed(2);
    };

    document.getElementById('searchBox').oninput = e => {
      searchQuery = e.target.value.trim().toLowerCase();
    };

    document.getElementById('resetZoomBtn').onclick = () => {
      transform = {
        x: canvas.parentElement.clientWidth / 2,
        y: canvas.parentElement.clientHeight / 2,
        k: 1
      };
    };

    document.getElementById('toggleDemoBtn').onclick = () => {
      useDemo = !useDemo;
      document.getElementById('toggleDemoBtn').classList.toggle('active', useDemo);
      fetchGraphData();
    };

    // Auto-polling every 2.5 seconds
    setInterval(fetchGraphData, 2500);
    loadProjects().then(() => fetchGraphData());
    render();
  </script>
</body>
</html>
"""


def discover_projects(current_path: Path) -> list[dict[str, Any]]:
    """Discover active daemon sessions, registered workspaces, and sibling projects."""
    projects: dict[str, dict[str, Any]] = {}

    def add_project(p: str | Path, source: str = "local") -> None:
        try:
            resolved = Path(p).resolve()
            if not resolved.is_dir():
                return
            key = str(resolved)
            # Filter out internal test runs and temporary directories
            low = key.lower()
            if source != "current" and any(t in low for t in ("pytest", "\\temp\\", "/temp/", "\\tmp\\", "/tmp/")):
                return
            if key not in projects:
                mem_cnt = len(list((resolved / ".sync" / "memory" / "records").glob("*.json"))) if (resolved / ".sync" / "memory" / "records").is_dir() else 0
                exp_cnt = len(list((resolved / ".sync" / "experience" / "records").glob("*.json"))) if (resolved / ".sync" / "experience" / "records").is_dir() else 0
                projects[key] = {
                    "name": resolved.name or str(resolved),
                    "path": key,
                    "source": source,
                    "memories": mem_cnt,
                    "experiences": exp_cnt,
                    "selected": (resolved == current_path.resolve()),
                }
        except Exception:
            pass

    # 1. Current workspace
    add_project(current_path, source="current")

    # 2. Query Daemon active sessions
    try:
        rpc_req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session.list", "params": {}}).encode("utf-8")
        req = urllib.request.Request("http://127.0.0.1:8765/rpc", data=rpc_req, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=0.8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for s in data.get("result", []):
                ws = s.get("workspace")
                if ws and not any(t in ws for t in ("pytest", "Temp", "tmp")):
                    add_project(ws, source="active-daemon")
    except Exception:
        pass

    # 3. Check ~/.stackmindcli/projects.json
    try:
        reg_file = Path.home() / ".stackmindcli" / "projects.json"
        if reg_file.is_file():
            reg_data = json.loads(reg_file.read_text(encoding="utf-8"))
            for p_info in reg_data.get("projects", {}).values():
                p_path = p_info.get("path")
                if p_path:
                    add_project(p_path, source="registry")
    except Exception:
        pass

    # 4. Check sibling folders
    try:
        parent = current_path.resolve().parent
        for sib in parent.iterdir():
            if sib.is_dir() and (sib / ".sync").is_dir():
                add_project(sib, source="workspace-sibling")
    except Exception:
        pass

    result = list(projects.values())
    result.sort(key=lambda x: (not x.get("selected", False), x.get("name", "").lower()))
    return result


class MemoryGraphRequestHandler(BaseHTTPRequestHandler):
    """Serve the interactive single-page graph app and JSON memory API."""

    project_path: Path = Path(".")

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            demo_json = json.dumps(get_demo_memory_graph())
            html = HTML_APP.replace("window.FALLBACK_DEMO_DATA = null;", f"window.FALLBACK_DEMO_DATA = {demo_json};")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))
            return

        if path == "/api/projects":
            projects = discover_projects(self.project_path)
            blob = json.dumps(projects, indent=2).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(blob)
            return

        if path == "/api/graph":
            use_demo = query.get("demo", ["0"])[0] in ("1", "true")
            req_project = query.get("project", [None])[0]
            target_ws = Path(req_project).resolve() if req_project else self.project_path
            if use_demo:
                payload = get_demo_memory_graph()
            else:
                payload = collect_project_memory_graph(target_ws)

            blob = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(blob)
            return

        if path == "/api/status":
            req_project = query.get("project", [None])[0]
            target_ws = Path(req_project).resolve() if req_project else self.project_path
            mem_dir = target_ws / ".sync" / "memory" / "records"
            exp_dir = target_ws / ".sync" / "experience" / "records"
            cnt = len(list(mem_dir.glob("*.json"))) if mem_dir.is_dir() else 0
            exp_cnt = len(list(exp_dir.glob("*.json"))) if exp_dir.is_dir() else 0
            blob = json.dumps({"records_count": cnt + exp_cnt, "timestamp": time.time()}).encode("utf-8")
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(blob)
            return

        self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress noisy HTTP access logs
        return


def is_port_in_use(host: str, port: int) -> bool:
    """Check if a port is already actively listening."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex((host, port)) == 0


def find_available_port(host: str = "127.0.0.1", start_port: int = 8766, max_attempts: int = 30) -> int:
    """Find a port that is free and not listening."""
    for port in range(start_port, start_port + max_attempts):
        if not is_port_in_use(host, port):
            return port
    return start_port


def serve_memory_graph(
    project_path: Path | str = ".",
    port: int = 8766,
    open_browser: bool = True,
) -> None:
    """Launch the live memory graph server on the specified port."""
    ws = Path(project_path).resolve()
    handler = MemoryGraphRequestHandler
    handler.project_path = ws

    if is_port_in_use("127.0.0.1", port):
        free_port = find_available_port("127.0.0.1", max(8766, port + 1))
        print(
            f"\n[StackMind] Note: Port {port} is already in use (e.g. by StackMind Daemon). Switching to port {free_port}.",
            flush=True,
        )
        port = free_port

    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    url = f"http://127.0.0.1:{port}"

    print(f"\n[StackMind] Live Memory Graph Visualizer active at:", flush=True)
    print(f"  --> {url}\n", flush=True)
    print(f"Connected to workspace: {ws}", flush=True)
    print(f"Press Ctrl+C to stop.\n", flush=True)

    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Memory Graph Visualizer...", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="StackMind Live In-Browser Memory Graph Server")
    parser.add_argument("project", nargs="?", default=".", help="Project workspace path")
    parser.add_argument("--port", "-p", type=int, default=8766, help="Port to serve (default: 8766)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open browser automatically")
    args = parser.parse_args()

    serve_memory_graph(args.project, port=args.port, open_browser=not args.no_browser)
