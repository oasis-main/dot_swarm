"""dot_swarm plan management.

A plan is a named chain of work items with per-step agent assignments and
inspector requirements. Plans are stored as markdown files in
.swarm/plans/<name>.md and run state is tracked in .swarm/plan_runs.jsonl.

Inspector configuration can be set at two levels:
  - Plan level: applies to every step unless overridden
  - Step level: overrides the plan-level config for that step only

When a step's max_retries is exhausted the plan halts with status
NEEDS_HUMAN, writing an escalation note to state.md. No separate watchdog
role is needed.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import SwarmPaths, _now_ts


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class InspectorConfig:
    enabled: bool = True
    max_retries: int = 3
    require_proof: list[str] = field(default_factory=lambda: ["branch", "commit"])

    @classmethod
    def from_dict(cls, d: dict) -> "InspectorConfig":
        return cls(
            enabled=d.get("enabled", True),
            max_retries=int(d.get("max_retries", 3)),
            require_proof=[
                f.strip()
                for f in str(d.get("require_proof", "branch,commit")).split(",")
                if f.strip()
            ],
        )

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "max_retries": self.max_retries,
            "require_proof": ",".join(self.require_proof),
        }


@dataclass
class PlanStep:
    title: str
    item_id: str | None = None
    agent: str = "opencode"
    depends: list[str] = field(default_factory=list)
    inspector: InspectorConfig = field(default_factory=InspectorConfig)
    notes: str = ""
    # Runtime state (not persisted in plan file)
    retries: int = 0
    status: str = "pending"   # pending | running | passed | failed | needs_human


@dataclass
class Plan:
    name: str
    description: str = ""
    pattern: str = "sequential"
    inspector: InspectorConfig = field(default_factory=InspectorConfig)
    steps: list[PlanStep] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Plan file paths
# ---------------------------------------------------------------------------

def _plans_dir(paths: SwarmPaths) -> Path:
    return paths.root / "plans"


def _plan_file(paths: SwarmPaths, name: str) -> Path:
    return _plans_dir(paths) / f"{name}.md"


def _runs_file(paths: SwarmPaths) -> Path:
    return paths.root / "plan_runs.jsonl"


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Extract YAML-lite frontmatter (key: value lines) and body."""
    fm: dict[str, Any] = {}
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return fm, text
    end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
    if end is None:
        return fm, text
    for line in lines[1:end]:
        if ":" in line:
            k, _, v = line.partition(":")
            fm[k.strip()] = v.strip()
    return fm, "\n".join(lines[end + 1:])


def _parse_inspector_from_lines(lines: list[str], prefix: str) -> dict:
    """Extract inspector_* keys from a block of continuation lines."""
    d: dict[str, Any] = {}
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{prefix}max_retries:"):
            d["max_retries"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith(f"{prefix}require_proof:"):
            d["require_proof"] = stripped.split(":", 1)[1].strip()
        elif stripped.startswith(f"{prefix}enabled:"):
            d["enabled"] = stripped.split(":", 1)[1].strip().lower() != "false"
    return d


def load_plan(paths: SwarmPaths, name: str) -> Plan | None:
    f = _plan_file(paths, name)
    if not f.exists():
        return None
    text = f.read_text(encoding='utf-8')
    fm, body = _parse_frontmatter(text)

    plan_inspector = InspectorConfig.from_dict({
        "max_retries": fm.get("inspector_max_retries", 3),
        "require_proof": fm.get("inspector_require_proof", "branch,commit"),
        "enabled": fm.get("inspector_enabled", True),
    })

    plan = Plan(
        name=name,
        description=fm.get("description", ""),
        pattern=fm.get("pattern", "sequential"),
        inspector=plan_inspector,
    )

    # Parse steps — each starts with "### <title>"
    step_blocks = re.split(r"^### ", body, flags=re.MULTILINE)
    for block in step_blocks:
        if not block.strip():
            continue
        block_lines = block.splitlines()
        title = block_lines[0].strip()
        kv: dict[str, str] = {}
        inspector_lines: list[str] = []
        for line in block_lines[1:]:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("inspector_"):
                inspector_lines.append(s)
            elif ":" in s:
                k, _, v = s.partition(":")
                kv[k.strip()] = v.strip()

        step_inspector_d = _parse_inspector_from_lines(inspector_lines, "inspector_")
        # Step-level inspector inherits from plan if not overridden
        merged = {
            "enabled": plan_inspector.enabled,
            "max_retries": plan_inspector.max_retries,
            "require_proof": ",".join(plan_inspector.require_proof),
        }
        merged.update(step_inspector_d)

        step = PlanStep(
            title=title,
            item_id=kv.get("item") or kv.get("item_id"),
            agent=kv.get("agent", "opencode"),
            depends=[d.strip() for d in kv.get("depends", "").split(",") if d.strip()],
            notes=kv.get("notes", ""),
            inspector=InspectorConfig.from_dict(merged),
        )
        plan.steps.append(step)

    return plan


def save_plan(paths: SwarmPaths, plan: Plan) -> Path:
    """Write plan to .swarm/plans/<name>.md (creates directory if needed)."""
    _plans_dir(paths).mkdir(exist_ok=True)
    lines = [
        "---",
        f"description: {plan.description}",
        f"pattern: {plan.pattern}",
        f"inspector_enabled: {str(plan.inspector.enabled).lower()}",
        f"inspector_max_retries: {plan.inspector.max_retries}",
        f"inspector_require_proof: {','.join(plan.inspector.require_proof)}",
        "---",
        "",
        "## Steps",
        "",
    ]
    for step in plan.steps:
        lines += [f"### {step.title}", ""]
        if step.item_id:
            lines.append(f"item: {step.item_id}")
        lines.append(f"agent: {step.agent}")
        if step.depends:
            lines.append(f"depends: {', '.join(step.depends)}")
        if step.notes:
            lines.append(f"notes: {step.notes}")
        # Step-level inspector (only write if different from plan defaults)
        if step.inspector.max_retries != plan.inspector.max_retries:
            lines.append(f"inspector_max_retries: {step.inspector.max_retries}")
        if step.inspector.require_proof != plan.inspector.require_proof:
            lines.append(
                f"inspector_require_proof: {','.join(step.inspector.require_proof)}"
            )
        lines.append("")

    f = _plan_file(paths, plan.name)
    f.write_text("\n".join(lines), encoding='utf-8')
    return f


def list_plans(paths: SwarmPaths) -> list[str]:
    d = _plans_dir(paths)
    if not d.exists():
        return []
    return [f.stem for f in sorted(d.glob("*.md"))]


# ---------------------------------------------------------------------------
# Run tracking
# ---------------------------------------------------------------------------

def _append_run(paths: SwarmPaths, record: dict) -> None:
    with _runs_file(paths).open("a", encoding='utf-8') as fh:
        fh.write(json.dumps(record) + "\n")


def last_run(paths: SwarmPaths, plan_name: str) -> dict | None:
    rf = _runs_file(paths)
    if not rf.exists():
        return None
    last = None
    for line in rf.read_text(encoding='utf-8').splitlines():
        try:
            r = json.loads(line)
            if r.get("plan") == plan_name:
                last = r
        except json.JSONDecodeError:
            pass
    return last


# ---------------------------------------------------------------------------
# Plan runner
# ---------------------------------------------------------------------------

def run_plan(
    paths: SwarmPaths,
    plan: Plan,
    *,
    dry_run: bool = False,
    spawn: bool = False,
    yes: bool = False,
) -> dict:
    """Execute a plan sequentially, honouring inspector retry limits.

    Returns a result dict: {plan, status, steps, message}
    """
    from .operations import read_queue, done_item
    from .roles import validate_proof, load_role

    ts = _now_ts()
    result_steps: list[dict] = []

    for i, step in enumerate(plan.steps):
        step_result: dict = {
            "step": i + 1,
            "title": step.title,
            "item_id": step.item_id,
            "agent": step.agent,
            "status": "skipped",
            "retries": 0,
        }

        if dry_run:
            inspector_note = (
                f"inspector: max_retries={step.inspector.max_retries}, "
                f"proof={','.join(step.inspector.require_proof)}"
            ) if step.inspector.enabled else "inspector: disabled"
            step_result["status"] = "dry_run"
            step_result["inspector"] = inspector_note
            result_steps.append(step_result)
            continue

        if spawn and step.item_id:
            # Delegate to tmux spawn — human/CI monitors the session
            from .spawn import spawn_agent
            try:
                sr = spawn_agent(paths, step.item_id, agent=step.agent)
                step_result["status"] = "spawned"
                step_result["window"] = sr.get("window")
            except RuntimeError as e:
                step_result["status"] = "spawn_failed"
                step_result["error"] = str(e)
            result_steps.append(step_result)
            continue

        # Non-spawn mode: run as subprocess command
        if step.item_id:
            cmd = ["swarm", "claim", step.item_id]
            if not yes:
                pass  # caller confirms interactively
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  cwd=str(paths.root.parent))
            step_result["claim_output"] = proc.stdout.strip()

        # Inspector check (only in non-spawn mode — spawn is async)
        if step.inspector.enabled and step.item_id:
            active, pending, _ = read_queue(paths)
            target = next(
                (it for it in active + pending if it.id == step.item_id), None
            )
            if target:
                from .roles import validate_proof
                missing = validate_proof(target.proof, step.inspector.require_proof)
                retries = target.inspect_fails
                if missing and retries >= step.inspector.max_retries:
                    step_result["status"] = "needs_human"
                    step_result["retries"] = retries
                    step_result["missing_proof"] = missing
                    result_steps.append(step_result)
                    _append_run(paths, {
                        "plan": plan.name, "ts": ts, "status": "needs_human",
                        "halted_at_step": i + 1, "steps": result_steps,
                    })
                    return {
                        "plan": plan.name,
                        "status": "needs_human",
                        "message": (
                            f"Step {i+1} '{step.title}' exhausted {retries} "
                            f"retries on {step.item_id}. Human review required."
                        ),
                        "steps": result_steps,
                    }

        step_result["status"] = "ok"
        result_steps.append(step_result)

    final_status = "complete"
    if any(s["status"] == "spawned" for s in result_steps):
        final_status = "spawned"
    elif any(s["status"] in ("spawn_failed", "needs_human") for s in result_steps):
        final_status = "partial"

    _append_run(paths, {
        "plan": plan.name, "ts": ts, "status": final_status, "steps": result_steps,
    })
    return {"plan": plan.name, "status": final_status, "steps": result_steps,
            "message": f"Plan '{plan.name}' {final_status}."}
