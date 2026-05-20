"""Tests for dot_swarm.workflows — multi-step workflow execution."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dot_swarm.models import SwarmPaths
from dot_swarm.workflows import (
    RUN_LOG_FILE,
    Workflow,
    WorkflowRun,
    WorkflowStep,
    StepResult,
    create_workflow,
    list_workflows,
    load_workflow,
    run_workflow,
    workflow_status,
    _eval_condition,
    _parse_workflow_md,
    _render_workflow_md,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def swarm_dir(tmp_path: Path) -> Path:
    swarm = tmp_path / ".swarm"
    swarm.mkdir()
    (swarm / "workflows").mkdir()
    return swarm


@pytest.fixture()
def paths(swarm_dir: Path) -> SwarmPaths:
    return SwarmPaths.from_swarm_dir(swarm_dir)


def _make_workflow(paths: SwarmPaths, name: str, pattern: str = "sequential",
                   steps: list[tuple] | None = None) -> Path:
    default_steps = [
        WorkflowStep(1, "echo step1"),
        WorkflowStep(2, "echo step2"),
    ]
    wf = Workflow(
        name=name,
        pattern=pattern,
        trigger="manual",
        description=f"{name} description",
        steps=[WorkflowStep(i + 1, cmd) for i, (cmd, *_) in enumerate(steps or [])]
        if steps else default_steps,
    )
    return create_workflow(paths, wf)


# ---------------------------------------------------------------------------
# Parsing round-trip
# ---------------------------------------------------------------------------

def test_render_and_parse_roundtrip() -> None:
    original = Workflow(
        name="test-flow",
        trigger="manual",
        pattern="sequential",
        description="A test workflow",
        steps=[
            WorkflowStep(number=1, command="swarm heal", agent="auto", timeout_min=5),
            WorkflowStep(number=2, command="swarm audit", agent="bedrock", timeout_min=10),
        ],
    )
    md = _render_workflow_md(original)
    parsed = _parse_workflow_md(md, "test-flow")
    assert parsed.name == "test-flow"
    assert parsed.trigger == "manual"
    assert parsed.pattern == "sequential"
    assert len(parsed.steps) == 2
    assert parsed.steps[0].command == "swarm heal"
    assert parsed.steps[1].agent == "bedrock"


def test_parse_step_fields() -> None:
    md = """\
---
trigger: manual
pattern: sequential
---

## Steps

1. swarm claim CLD-042
   agent: bedrock
   depends: CLD-041
   timeout: 30
   if: step0.ok
"""
    wf = _parse_workflow_md(md, "test")
    assert len(wf.steps) == 1
    s = wf.steps[0]
    assert s.command == "swarm claim CLD-042"
    assert s.agent == "bedrock"
    assert s.depends == ["CLD-041"]
    assert s.timeout_min == 30
    assert s.condition == "step0.ok"


def test_parse_workflow_defaults() -> None:
    md = """\
---
trigger: manual
---

## Steps

1. echo hello
"""
    wf = _parse_workflow_md(md, "minimal")
    assert wf.pattern == "sequential"  # default
    assert wf.trigger == "manual"
    assert len(wf.steps) == 1


def test_parse_workflow_on_done_trigger() -> None:
    md = """\
---
trigger: on:done CLD-042
pattern: sequential
---

## Steps

1. swarm ai "claim CLD-043"
"""
    wf = _parse_workflow_md(md, "chain")
    assert wf.trigger == "on:done CLD-042"


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def test_create_workflow_writes_file(paths: SwarmPaths) -> None:
    _make_workflow(paths, "deploy")
    assert (paths.workflows / "deploy.md").exists()


def test_list_workflows_empty(paths: SwarmPaths) -> None:
    assert list_workflows(paths) == []


def test_list_workflows_returns_stems(paths: SwarmPaths) -> None:
    _make_workflow(paths, "alpha")
    _make_workflow(paths, "beta")
    names = list_workflows(paths)
    assert names == ["alpha", "beta"]


def test_load_workflow_by_name(paths: SwarmPaths) -> None:
    _make_workflow(paths, "my-flow")
    wf = load_workflow(paths, "my-flow")
    assert wf.name == "my-flow"
    assert len(wf.steps) == 2


def test_load_workflow_by_name_with_extension(paths: SwarmPaths) -> None:
    _make_workflow(paths, "my-flow")
    wf = load_workflow(paths, "my-flow.md")
    assert wf.name == "my-flow"


def test_load_workflow_raises_when_absent(paths: SwarmPaths) -> None:
    with pytest.raises(FileNotFoundError):
        load_workflow(paths, "nonexistent")


def test_list_workflows_no_workflows_dir(tmp_path: Path) -> None:
    swarm = tmp_path / ".swarm"
    swarm.mkdir()
    p = SwarmPaths.from_swarm_dir(swarm)
    assert list_workflows(p) == []


# ---------------------------------------------------------------------------
# _eval_condition
# ---------------------------------------------------------------------------

def _make_result(ok: bool, num: int = 1) -> StepResult:
    return StepResult(
        step_number=num, command="test",
        exit_code=0 if ok else 1, stdout="", stderr="",
        ran_at="2026-04-06T12:00Z",
    )


def test_eval_condition_true() -> None:
    ctx = {"step1": _make_result(True)}
    assert _eval_condition("step1.ok", ctx) is True


def test_eval_condition_false() -> None:
    ctx = {"step1": _make_result(False)}
    assert _eval_condition("step1.ok", ctx) is False


def test_eval_condition_not_true() -> None:
    ctx = {"step1": _make_result(False)}
    assert _eval_condition("not step1.ok", ctx) is True


def test_eval_condition_not_false() -> None:
    ctx = {"step1": _make_result(True)}
    assert _eval_condition("not step1.ok", ctx) is False


def test_eval_condition_missing_step_returns_true() -> None:
    assert _eval_condition("step9.ok", {}) is True


# ---------------------------------------------------------------------------
# run_workflow — sequential
# ---------------------------------------------------------------------------

def test_sequential_all_ok(paths: SwarmPaths) -> None:
    _make_workflow(paths, "ok-flow", steps=[
        ("echo a",), ("echo b",), ("echo c",)
    ])
    result = run_workflow(paths, "ok-flow")
    assert result.ok is True
    assert len(result.step_results) == 3
    assert all(r.exit_code == 0 for r in result.step_results)


def test_sequential_stops_on_failure(paths: SwarmPaths) -> None:
    wf = Workflow(
        name="fail-flow",
        pattern="sequential",
        trigger="manual",
        steps=[
            WorkflowStep(1, "echo ok"),
            WorkflowStep(2, "false"),         # exits 1
            WorkflowStep(3, "echo skipped"),
        ],
    )
    create_workflow(paths, wf)
    result = run_workflow(paths, "fail-flow")
    assert result.ok is False
    assert result.step_results[1].exit_code == 1
    assert result.step_results[2].skipped is True
    assert "step 2 failed" in result.step_results[2].skip_reason


def test_sequential_condition_skip(paths: SwarmPaths) -> None:
    wf = Workflow(
        name="cond-flow",
        pattern="sequential",
        trigger="manual",
        steps=[
            WorkflowStep(1, "false"),                  # fails → ok=False
            WorkflowStep(2, "echo only-if-step1-ok", condition="step1.ok"),
        ],
    )
    create_workflow(paths, wf)
    result = run_workflow(paths, "cond-flow")
    # step2 is skipped because step1 failed (sequential stop)
    assert result.step_results[1].skipped is True


def test_dry_run_does_not_execute(paths: SwarmPaths) -> None:
    wf = Workflow(
        name="dry-flow",
        pattern="sequential",
        trigger="manual",
        steps=[WorkflowStep(1, "rm -rf /important")],
    )
    create_workflow(paths, wf)
    result = run_workflow(paths, "dry-flow", dry_run=True)
    assert result.ok is True
    assert "[dry-run]" in result.step_results[0].stdout


# ---------------------------------------------------------------------------
# run_workflow — concurrent
# ---------------------------------------------------------------------------

def test_concurrent_runs_all_steps(paths: SwarmPaths) -> None:
    wf = Workflow(
        name="conc-flow",
        pattern="concurrent",
        trigger="manual",
        steps=[
            WorkflowStep(1, "echo a"),
            WorkflowStep(2, "echo b"),
            WorkflowStep(3, "echo c"),
        ],
    )
    create_workflow(paths, wf)
    result = run_workflow(paths, "conc-flow")
    assert result.ok is True
    assert len(result.step_results) == 3


# ---------------------------------------------------------------------------
# WorkflowRun.summary
# ---------------------------------------------------------------------------

def test_workflow_run_summary() -> None:
    run = WorkflowRun(
        workflow_name="test",
        pattern="sequential",
        step_results=[
            StepResult(1, "a", 0, "", "", "2026-04-06T12:00Z"),
            StepResult(2, "b", 1, "", "", "2026-04-06T12:00Z"),
            StepResult(3, "c", 0, "", "", "2026-04-06T12:00Z", skipped=True),
        ],
        started_at="2026-04-06T12:00Z",
        finished_at="2026-04-06T12:01Z",
    )
    assert run.ok is False
    assert "1 ok" in run.summary
    assert "1 failed" in run.summary
    assert "1 skipped" in run.summary


# ---------------------------------------------------------------------------
# Run log and workflow_status
# ---------------------------------------------------------------------------

def test_run_log_written_after_run(paths: SwarmPaths) -> None:
    _make_workflow(paths, "log-flow")
    run_workflow(paths, "log-flow")
    log = paths.root / RUN_LOG_FILE
    assert log.exists()
    lines = log.read_text(encoding='utf-8').splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["workflow_name"] == "log-flow"
    assert "ok" in entry
    assert "summary" in entry


def test_workflow_status_returns_last_run(paths: SwarmPaths) -> None:
    _make_workflow(paths, "stat-flow")
    run_workflow(paths, "stat-flow")
    status = workflow_status(paths, "stat-flow")
    assert status["workflow_name"] == "stat-flow"
    assert "started_at" in status


def test_workflow_status_unknown_returns_empty(paths: SwarmPaths) -> None:
    assert workflow_status(paths, "nobody") == {}


def test_workflow_status_returns_latest(paths: SwarmPaths) -> None:
    _make_workflow(paths, "multi-run")
    run_workflow(paths, "multi-run")
    run_workflow(paths, "multi-run")
    status = workflow_status(paths, "multi-run")
    # Should still work — returns most recently logged entry
    assert status["workflow_name"] == "multi-run"


# ---------------------------------------------------------------------------
# WorkflowStep.to_md
# ---------------------------------------------------------------------------

def test_step_to_md_minimal() -> None:
    s = WorkflowStep(number=1, command="swarm heal")
    md = s.to_md()
    assert md.startswith("1. swarm heal")
    assert "agent" not in md  # default "auto" not written


def test_step_to_md_full() -> None:
    s = WorkflowStep(number=2, command="swarm claim CLD-042",
                     agent="bedrock", depends=["CLD-041"],
                     timeout_min=45, condition="step1.ok")
    md = s.to_md()
    assert "agent: bedrock" in md
    assert "depends: CLD-041" in md
    assert "timeout: 45" in md
    assert "if: step1.ok" in md
