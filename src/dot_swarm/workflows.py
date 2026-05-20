"""dot_swarm workflow engine.

Executes multi-step workflows defined as markdown files in .swarm/workflows/.
Inspired by swarms.ai patterns (sequential, concurrent, conditional, mixture)
but expressed in markdown-native format and driven by the local dot_swarm CLI.

Workflow file format (.swarm/workflows/<name>.md):
---
trigger: manual          # or "on:done CLD-042" or cron "0 9 * * 1"
pattern: sequential      # sequential | concurrent | conditional | mixture
description: OAuth2 flow
---

## Steps

1. swarm claim CLD-042
   agent: bedrock
   timeout: 30

2. swarm claim CLD-043
   agent: claude
   depends: CLD-042
   timeout: 45
   if: step1.ok

3. swarm heal --fix
"""

from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import SwarmPaths

WORKFLOWS_DIR = "workflows"
RUN_LOG_FILE = "workflow_runs.jsonl"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class WorkflowStep:
    number: int
    command: str            # full shell/swarm command
    agent: str = "auto"     # "bedrock" | "claude" | "human" | "auto"
    depends: list[str] = field(default_factory=list)  # item IDs
    timeout_min: int = 60
    condition: str = ""     # optional "if:" guard: "step1.ok", "not step2.ok"

    def to_md(self) -> str:
        lines = [f"{self.number}. {self.command}"]
        if self.agent != "auto":
            lines.append(f"   agent: {self.agent}")
        if self.depends:
            lines.append(f"   depends: {', '.join(self.depends)}")
        if self.timeout_min != 60:
            lines.append(f"   timeout: {self.timeout_min}")
        if self.condition:
            lines.append(f"   if: {self.condition}")
        return "\n".join(lines)


@dataclass
class Workflow:
    name: str
    trigger: str = "manual"
    pattern: str = "sequential"
    description: str = ""
    steps: list[WorkflowStep] = field(default_factory=list)


@dataclass
class StepResult:
    step_number: int
    command: str
    exit_code: int
    stdout: str
    stderr: str
    ran_at: str
    skipped: bool = False
    skip_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.skipped


@dataclass
class WorkflowRun:
    workflow_name: str
    pattern: str
    step_results: list[StepResult]
    started_at: str
    finished_at: str = ""

    @property
    def ok(self) -> bool:
        return all(r.ok or r.skipped for r in self.step_results)

    @property
    def summary(self) -> str:
        ok = sum(1 for r in self.step_results if r.ok)
        skip = sum(1 for r in self.step_results if r.skipped)
        fail = sum(1 for r in self.step_results if not r.ok and not r.skipped)
        return f"{ok} ok, {fail} failed, {skip} skipped"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def load_workflow(paths: SwarmPaths, name: str) -> Workflow:
    """Load and parse a workflow markdown file by name (with or without .md)."""
    fname = name if name.endswith(".md") else f"{name}.md"
    wfile = paths.workflows / fname
    if not wfile.exists():
        raise FileNotFoundError(f"Workflow not found: {wfile}")
    return _parse_workflow_md(wfile.read_text(encoding="utf-8"), name.removesuffix(".md"))


def list_workflows(paths: SwarmPaths) -> list[str]:
    """Return names of all workflow files in .swarm/workflows/."""
    if not paths.workflows.exists():
        return []
    return sorted(p.stem for p in paths.workflows.glob("*.md"))


def create_workflow(paths: SwarmPaths, wf: Workflow) -> Path:
    """Write a Workflow to .swarm/workflows/<name>.md. Creates dir if needed."""
    paths.workflows.mkdir(parents=True, exist_ok=True)
    wfile = paths.workflows / f"{wf.name}.md"
    wfile.write_text(_render_workflow_md(wf), encoding="utf-8")
    return wfile


def _parse_workflow_md(text: str, name: str) -> Workflow:
    wf = Workflow(name=name)
    in_frontmatter = False
    in_steps = False
    current_step_lines: list[str] = []
    current_step_num: int = 0

    def _flush_step() -> None:
        if current_step_lines and current_step_num:
            step = _parse_step_block(current_step_num, current_step_lines)
            wf.steps.append(step)

    for line in text.splitlines():
        stripped = line.strip()

        # YAML-style frontmatter between --- markers
        if stripped == "---":
            in_frontmatter = not in_frontmatter
            continue

        if in_frontmatter:
            if ":" in stripped:
                k, v = stripped.split(":", 1)
                k, v = k.strip(), v.strip()
                if k == "trigger":
                    wf.trigger = v
                elif k == "pattern":
                    wf.pattern = v
                elif k == "description":
                    wf.description = v
            continue

        if stripped == "## Steps":
            in_steps = True
            continue

        if not in_steps:
            continue

        # Numbered step opener: "1. some command"
        import re
        step_m = re.match(r"^(\d+)\.\s+(.+)$", line)
        if step_m:
            _flush_step()
            current_step_num = int(step_m.group(1))
            current_step_lines = [step_m.group(2).strip()]
            continue

        # Continuation lines (indented or blank)
        if current_step_num and (line.startswith("   ") or stripped == ""):
            if stripped:
                current_step_lines.append(stripped)
            continue

    _flush_step()
    return wf


def _parse_step_block(num: int, lines: list[str]) -> WorkflowStep:
    import re
    step = WorkflowStep(number=num, command=lines[0])
    for line in lines[1:]:
        m = re.match(r"^(\w+):\s*(.+)$", line.strip())
        if not m:
            continue
        k, v = m.group(1), m.group(2).strip()
        if k == "agent":
            step.agent = v
        elif k == "depends":
            step.depends = [x.strip() for x in v.split(",")]
        elif k == "timeout":
            try:
                step.timeout_min = int(v)
            except ValueError:
                pass
        elif k == "if":
            step.condition = v
    return step


def _render_workflow_md(wf: Workflow) -> str:
    lines = [
        "---",
        f"trigger: {wf.trigger}",
        f"pattern: {wf.pattern}",
    ]
    if wf.description:
        lines.append(f"description: {wf.description}")
    lines += ["---", "", "## Steps", ""]
    for step in wf.steps:
        lines.append(step.to_md())
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------

def run_workflow(
    paths: SwarmPaths,
    name: str,
    *,
    dry_run: bool = False,
    cwd: Path | None = None,
) -> WorkflowRun:
    """Execute a named workflow. Returns a WorkflowRun with all step results."""
    wf = load_workflow(paths, name)
    started = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    work_dir = str(cwd or paths.root.parent)

    if wf.pattern == "concurrent":
        results = _run_concurrent(wf, work_dir, dry_run)
    else:
        results = _run_sequential(wf, work_dir, dry_run)

    finished = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    run = WorkflowRun(
        workflow_name=name,
        pattern=wf.pattern,
        step_results=results,
        started_at=started,
        finished_at=finished,
    )
    _log_run(paths, run)
    return run


def _run_sequential(wf: Workflow, cwd: str, dry_run: bool) -> list[StepResult]:
    results: list[StepResult] = []
    context: dict[str, StepResult] = {}

    for step in wf.steps:
        key = f"step{step.number}"

        if step.condition and not _eval_condition(step.condition, context):
            results.append(StepResult(
                step_number=step.number, command=step.command,
                exit_code=0, stdout="", stderr="",
                ran_at=_now(), skipped=True,
                skip_reason=f"condition '{step.condition}' not met",
            ))
            context[key] = results[-1]
            continue

        sr = _exec_step(step, cwd, dry_run)
        results.append(sr)
        context[key] = sr

        if not sr.ok and not dry_run:
            for remaining in wf.steps[wf.steps.index(step) + 1:]:
                results.append(StepResult(
                    step_number=remaining.number,
                    command=remaining.command,
                    exit_code=0, stdout="", stderr="",
                    ran_at=_now(), skipped=True,
                    skip_reason=f"step {step.number} failed",
                ))
            break

    return results


def _run_concurrent(wf: Workflow, cwd: str, dry_run: bool) -> list[StepResult]:
    """Run all steps in parallel threads. Results ordered by step number."""
    results: list[StepResult] = [None] * len(wf.steps)  # type: ignore[list-item]
    threads: list[threading.Thread] = []

    def _worker(idx: int, step: WorkflowStep) -> None:
        results[idx] = _exec_step(step, cwd, dry_run)

    for i, step in enumerate(wf.steps):
        t = threading.Thread(target=_worker, args=(i, step), daemon=True)
        threads.append(t)
        t.start()

    for t in threads:
        t.join(timeout=max(s.timeout_min for s in wf.steps) * 60)

    return [r for r in results if r is not None]


def _exec_step(step: WorkflowStep, cwd: str, dry_run: bool) -> StepResult:
    now = _now()
    if dry_run:
        return StepResult(
            step_number=step.number, command=step.command,
            exit_code=0, stdout=f"[dry-run] would run: {step.command}",
            stderr="", ran_at=now,
        )
    try:
        result = subprocess.run(
            step.command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=step.timeout_min * 60,
            cwd=cwd,
        )
        return StepResult(
            step_number=step.number, command=step.command,
            exit_code=result.returncode,
            stdout=result.stdout.strip(),
            stderr=result.stderr.strip(),
            ran_at=now,
        )
    except subprocess.TimeoutExpired:
        return StepResult(
            step_number=step.number, command=step.command,
            exit_code=124, stdout="", stderr=f"timeout after {step.timeout_min}m",
            ran_at=now,
        )
    except Exception as exc:
        return StepResult(
            step_number=step.number, command=step.command,
            exit_code=1, stdout="", stderr=str(exc), ran_at=now,
        )


def _eval_condition(condition: str, context: dict[str, StepResult]) -> bool:
    """Evaluate a simple condition like 'step1.ok' or 'not step2.ok'."""
    negate = condition.startswith("not ")
    expr = condition.removeprefix("not ").strip()
    parts = expr.split(".")
    if len(parts) == 2:
        step_key, attr = parts[0], parts[1]
        sr = context.get(step_key)
        if sr is None:
            return not negate
        val = getattr(sr, attr, False)
        result = bool(val)
        return (not result) if negate else result
    return True


# ---------------------------------------------------------------------------
# Workflow status
# ---------------------------------------------------------------------------

def workflow_status(paths: SwarmPaths, name: str) -> dict:
    """Return the last run result for a workflow, or {} if never run."""
    import json
    log = paths.root / RUN_LOG_FILE
    if not log.exists():
        return {}
    last: dict = {}
    for line in log.read_text(encoding='utf-8').splitlines():
        try:
            entry = json.loads(line)
            if entry.get("workflow_name") == name:
                last = entry
        except (json.JSONDecodeError, KeyError):
            continue
    return last


def _log_run(paths: SwarmPaths, run: WorkflowRun) -> None:
    """Append a summary JSON line to .swarm/workflow_runs.jsonl."""
    import json
    log = paths.root / RUN_LOG_FILE
    entry = {
        "workflow_name": run.workflow_name,
        "pattern": run.pattern,
        "started_at": run.started_at,
        "finished_at": run.finished_at,
        "ok": run.ok,
        "summary": run.summary,
        "steps": [
            {
                "n": r.step_number,
                "cmd": r.command,
                "exit": r.exit_code,
                "skipped": r.skipped,
            }
            for r in run.step_results
        ],
    }
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
