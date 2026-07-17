"""Tests for dot_swarm.swarms_provider — DotSwarmStateProvider, DotSwarmWorkflow, DotSwarmTool.

All tests are standalone (no swarms.ai package required).
StigmergicSwarm integration tests are omitted here; they require `pip install swarms`
and belong in an integration test suite run against swarms.ai CI.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from dot_swarm.swarms_provider import (
    DotSwarmStateProvider,
    DotSwarmTool,
    DotSwarmWorkflow,
)
from dot_swarm.workflows import Workflow, WorkflowStep, create_workflow
from dot_swarm.models import SwarmPaths


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

MINIMAL_QUEUE = """\
# Queue

## Active

## Pending

- [ ] [ORG-001] [OPEN] Implement feature A
      priority: high | project: core

## Done

"""

MINIMAL_STATE = """\
# State — Test

**Last touched**: 2026-04-06T12:00Z by cascade
**Current focus**: testing
**Active items**: (none)
**Blockers**: None
**Ready for pickup**: ORG-001
"""


@pytest.fixture()
def swarm_dir(tmp_path: Path) -> Path:
    swarm = tmp_path / ".swarm"
    swarm.mkdir()
    (swarm / "queue.md").write_text(MINIMAL_QUEUE, encoding='utf-8')
    (swarm / "state.md").write_text(MINIMAL_STATE, encoding='utf-8')
    (swarm / "memory.md").write_text("# Memory\n\n", encoding='utf-8')
    (swarm / "context.md").write_text("# Context\n\nTest project.\n", encoding='utf-8')
    (swarm / "BOOTSTRAP.md").write_text("# Bootstrap\n\n", encoding='utf-8')
    (swarm / "workflows").mkdir()
    return swarm


@pytest.fixture()
def provider(swarm_dir: Path) -> DotSwarmStateProvider:
    return DotSwarmStateProvider(swarm_path=swarm_dir)


@pytest.fixture()
def tool(swarm_dir: Path) -> DotSwarmTool:
    return DotSwarmTool(swarm_path=swarm_dir)


@pytest.fixture()
def wf_file(swarm_dir: Path) -> Path:
    wf = Workflow(
        name="test-flow",
        trigger="manual",
        pattern="sequential",
        steps=[
            WorkflowStep(1, "echo step1"),
            WorkflowStep(2, "echo step2"),
        ],
    )
    paths = SwarmPaths.from_swarm_dir(swarm_dir)
    return create_workflow(paths, wf)


# ---------------------------------------------------------------------------
# DotSwarmStateProvider
# ---------------------------------------------------------------------------

class TestDotSwarmStateProvider:

    def test_init_from_swarm_dir(self, swarm_dir: Path) -> None:
        p = DotSwarmStateProvider(swarm_path=swarm_dir)
        assert p.division_name == swarm_dir.parent.name

    def test_init_from_parent_dir(self, swarm_dir: Path) -> None:
        p = DotSwarmStateProvider(swarm_path=swarm_dir.parent)
        assert p.division_name == swarm_dir.parent.name

    def test_init_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="No .swarm/"):
            DotSwarmStateProvider(swarm_path=tmp_path / "no-such-dir")

    def test_get_queue_returns_dict(self, provider: DotSwarmStateProvider) -> None:
        q = provider.get_queue()
        assert "active" in q
        assert "pending" in q
        assert "done" in q

    def test_get_queue_pending_item(self, provider: DotSwarmStateProvider) -> None:
        q = provider.get_queue()
        pending_ids = [i["id"] for i in q["pending"]]
        assert "ORG-001" in pending_ids

    def test_get_state_returns_dict(self, provider: DotSwarmStateProvider) -> None:
        s = provider.get_state()
        assert isinstance(s, dict)

    def test_build_context_bundle_returns_string(self, provider: DotSwarmStateProvider) -> None:
        ctx = provider.build_context_bundle()
        assert isinstance(ctx, str)
        assert len(ctx) > 0

    def test_build_system_prompt_contains_agent_name(self, provider: DotSwarmStateProvider) -> None:
        prompt = provider.build_system_prompt(agent_name="TestAgent")
        assert "TestAgent" in prompt

    def test_build_system_prompt_contains_context(self, provider: DotSwarmStateProvider) -> None:
        prompt = provider.build_system_prompt()
        assert "CURRENT STATE" in prompt or "queue" in prompt.lower()

    def test_apply_operations_invalid_json(self, provider: DotSwarmStateProvider) -> None:
        results = provider.apply_operations("this is not json")
        assert any("Could not parse" in r or "parse" in r.lower() for r in results)

    def test_apply_operations_empty_ops(self, provider: DotSwarmStateProvider) -> None:
        results = provider.apply_operations({"operations": []})
        assert isinstance(results, list)

    def test_apply_operations_respond_op(self, provider: DotSwarmStateProvider) -> None:
        results = provider.apply_operations({
            "operations": [{"op": "respond", "message": "Hello"}],
            "commentary": "Test commentary",
        })
        assert isinstance(results, list)
        assert any("Hello" in r or "Test commentary" in r for r in results)

    def test_apply_operations_add(self, provider: DotSwarmStateProvider) -> None:
        results = provider.apply_operations({
            "operations": [{"op": "add", "description": "New item from test",
                            "priority": "medium", "project": "test"}]
        }, agent_id="test-agent")
        assert isinstance(results, list)


# ---------------------------------------------------------------------------
# DotSwarmWorkflow
# ---------------------------------------------------------------------------

class TestDotSwarmWorkflow:

    def test_load_from_markdown(self, wf_file: Path) -> None:
        wf = DotSwarmWorkflow.from_markdown(wf_file)
        assert wf.name == "test-flow"
        assert wf.pattern == "sequential"
        assert len(wf.steps) == 2

    def test_load_missing_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            DotSwarmWorkflow.from_markdown(tmp_path / "no-such.md")

    def test_from_swarm_dir(self, swarm_dir: Path, wf_file: Path) -> None:
        wf = DotSwarmWorkflow.from_swarm_dir(swarm_dir, "test-flow")
        assert wf.name == "test-flow"

    def test_from_swarm_dir_with_extension(self, swarm_dir: Path, wf_file: Path) -> None:
        wf = DotSwarmWorkflow.from_swarm_dir(swarm_dir, "test-flow.md")
        assert wf.name == "test-flow"

    def test_dry_run(self, wf_file: Path) -> None:
        wf = DotSwarmWorkflow.from_markdown(wf_file)
        result = wf.run(dry_run=True)
        assert result["ok"] is True
        assert all("[dry-run]" in s["stdout"] for s in result["steps"])

    def test_run_success(self, wf_file: Path) -> None:
        wf = DotSwarmWorkflow.from_markdown(wf_file)
        result = wf.run()
        assert result["ok"] is True
        assert result["workflow"] == "test-flow"
        assert len(result["steps"]) == 2

    def test_run_failure_skips_downstream(self, swarm_dir: Path) -> None:
        paths = SwarmPaths.from_swarm_dir(swarm_dir)
        wf = Workflow(
            name="fail-flow",
            pattern="sequential",
            trigger="manual",
            steps=[
                WorkflowStep(1, "false"),
                WorkflowStep(2, "echo downstream"),
            ],
        )
        wf_path = create_workflow(paths, wf)
        adapter = DotSwarmWorkflow.from_markdown(wf_path)
        result = adapter.run()
        assert result["ok"] is False
        assert result["steps"][1]["skipped"] is True

    def test_add_agent_routes_step(self, swarm_dir: Path) -> None:
        class FakeAgent:
            def run(self, cmd: str) -> str:
                return f"agent ran: {cmd}"

        paths = SwarmPaths.from_swarm_dir(swarm_dir)
        wf = Workflow(
            name="agent-flow",
            pattern="sequential",
            trigger="manual",
            steps=[WorkflowStep(1, "swarm heal", agent="bedrock")],
        )
        wf_path = create_workflow(paths, wf)
        adapter = DotSwarmWorkflow.from_markdown(wf_path)
        adapter.add_agent("bedrock", FakeAgent())
        result = adapter.run()
        assert result["ok"] is True
        assert "agent ran" in result["steps"][0]["stdout"]

    def test_add_agent_fluent_interface(self, wf_file: Path) -> None:
        class FakeAgent:
            def run(self, cmd: str) -> str:
                return "ok"
        wf = DotSwarmWorkflow.from_markdown(wf_file)
        ret = wf.add_agent("bedrock", FakeAgent())
        assert ret is wf

    def test_result_summary_format(self, wf_file: Path) -> None:
        wf = DotSwarmWorkflow.from_markdown(wf_file)
        result = wf.run()
        assert "ok" in result["summary"]
        assert "failed" in result["summary"]
        assert "skipped" in result["summary"]

    def test_properties(self, wf_file: Path) -> None:
        wf = DotSwarmWorkflow.from_markdown(wf_file)
        assert wf.name == "test-flow"
        assert wf.pattern == "sequential"
        assert wf.trigger == "manual"
        assert len(wf.steps) == 2


# ---------------------------------------------------------------------------
# DotSwarmTool
# ---------------------------------------------------------------------------

class TestDotSwarmTool:

    def test_tool_name_and_description(self, tool: DotSwarmTool) -> None:
        assert tool.name == "dot_swarm"
        assert len(tool.description) > 10

    def test_status_op(self, tool: DotSwarmTool) -> None:
        result = tool(operation="status")
        assert result["ok"] is True
        assert "message" in result
        assert "data" in result

    def test_status_contains_pending(self, tool: DotSwarmTool) -> None:
        result = tool(operation="status")
        assert "ORG-001" in result["message"] or "ORG-001" in str(result["data"])

    def test_unknown_operation(self, tool: DotSwarmTool) -> None:
        result = tool(operation="frobnicate")
        assert result["ok"] is False
        assert "Unknown operation" in result["message"]

    def test_add_op(self, tool: DotSwarmTool) -> None:
        result = tool(operation="add", description="Test item from tool",
                      priority="low", project="test")
        assert isinstance(result["ok"], bool)
        assert "message" in result

    def test_memory_op(self, tool: DotSwarmTool) -> None:
        result = tool(operation="memory",
                      topic="test-decision",
                      decision="chose X over Y",
                      why="X is simpler",
                      agent_id="test")
        assert isinstance(result["ok"], bool)

    def test_heal_op(self, tool: DotSwarmTool) -> None:
        result = tool(operation="heal")
        assert isinstance(result["ok"], bool)
        assert "Security scan" in result["message"]
        assert "Trail" in result["message"]
        assert "data" in result

    def test_call_exception_handled(self, tool: DotSwarmTool) -> None:
        result = tool(operation="claim", item_id="NONEXISTENT-999")
        assert isinstance(result, dict)
        assert "ok" in result

    def test_tool_is_callable(self, tool: DotSwarmTool) -> None:
        assert callable(tool)
