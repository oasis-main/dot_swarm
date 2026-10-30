"""Regression tests for the 2.0.0 lifecycle and queue-format bugs.

Each test reproduces one reported defect through the real CLI (or the MCP
handler) and checks the fixed behaviour.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from dot_swarm.cli import cli
from dot_swarm.models import ItemState, SwarmPaths
from dot_swarm.operations import (
    _division_code_from_paths, add_item, claim_item, merge_active_items,
    partial_item, read_queue, read_state,
)


def _make_swarm(tmp_path: Path, name: str = "oasis-cloud", context: str = "# Context\n") -> Path:
    div = tmp_path / name
    swarm = div / ".swarm"
    swarm.mkdir(parents=True)
    (swarm / "queue.md").write_text(
        "# Queue\n\n## Active\n\n## Pending\n\n## Done\n", encoding="utf-8")
    (swarm / "state.md").write_text(
        "# State\n\n**Last touched**: 2026-01-01T00:00Z by test\n"
        "**Current focus**: testing\n**Active items**: (none)\n**Blockers**: None\n",
        encoding="utf-8")
    (swarm / "context.md").write_text(context, encoding="utf-8")
    return div


def _run(div: Path, *args: str, agent: str = "worker"):
    return CliRunner().invoke(cli, ["--path", str(div), *args],
                              env={"SWARM_AGENT_ID": agent})


def _item(div: Path, item_id: str):
    active, pending, done = read_queue(SwarmPaths.find(div))
    return next((i for i in active + pending + done if i.id == item_id), None)


# ---------------------------------------------------------------------------
# swarm unblock
# ---------------------------------------------------------------------------

def test_unblock_returns_item_to_open(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    _run(div, "add", "Task A")
    _run(div, "claim", "CLD-001")
    _run(div, "block", "CLD-001", "waiting on vendor")

    result = _run(div, "unblock", "CLD-001")
    assert result.exit_code == 0, result.output
    # The resolver re-reads the claims trail: the item must stay OPEN, not
    # fall back to BLOCKED or to the pre-block CLAIMED record.
    item = _item(div, "CLD-001")
    assert item.state == ItemState.OPEN
    assert item.claimed_by is None
    assert "CLD-001" in [i.id for i in read_queue(SwarmPaths.find(div))[1]]


def test_unblock_reclaim_claims_for_agent(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    _run(div, "add", "Task A")
    _run(div, "claim", "CLD-001", agent="first")
    _run(div, "block", "CLD-001", "stuck")

    result = _run(div, "unblock", "CLD-001", "--reclaim", agent="second")
    assert result.exit_code == 0, result.output
    item = _item(div, "CLD-001")
    assert item.state == ItemState.CLAIMED
    assert item.claimed_by == "second"


def test_unblock_only_touches_named_item(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    _run(div, "add", "Task A")
    _run(div, "add", "Task B")
    _run(div, "block", "CLD-001", "a")
    _run(div, "block", "CLD-002", "b")

    assert _run(div, "unblock", "CLD-002").exit_code == 0
    assert _item(div, "CLD-001").state == ItemState.BLOCKED
    assert _item(div, "CLD-002").state == ItemState.OPEN


def test_unblock_refuses_item_that_is_not_blocked(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    _run(div, "add", "Task A")
    result = _run(div, "unblock", "CLD-001")
    assert result.exit_code == 1
    assert "not blocked" in result.output


def test_block_reason_with_brackets_keeps_item_parseable(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    _run(div, "add", "Task A")
    _run(div, "block", "CLD-001", "see [ticket 9]")
    _run(div, "add", "Task B")        # a later write must not drop CLD-001
    # On 2.0.0 the unparseable line vanished, "Task B" reused CLD-001, and
    # the old BLOCKED claim record then attached to "Task B".
    blocked = _item(div, "CLD-001")
    assert blocked.description == "Task A"
    assert blocked.state == ItemState.BLOCKED
    assert _item(div, "CLD-002").description == "Task B"


# ---------------------------------------------------------------------------
# Inspector gate
# ---------------------------------------------------------------------------

def _gated(tmp_path: Path) -> Path:
    div = _make_swarm(tmp_path)
    _run(div, "add", "Task A")
    _run(div, "role", "enable", "inspector")
    _run(div, "claim", "CLD-001")
    _run(div, "partial", "CLD-001", "--proof", "branch:b commit:abc1234")
    return div


def test_worker_with_proof_cannot_done_own_item(tmp_path: Path) -> None:
    div = _gated(tmp_path)
    result = _run(div, "done", "CLD-001")
    assert result.exit_code == 1
    assert "inspect" in result.output
    assert _item(div, "CLD-001").state != ItemState.DONE


def test_done_force_is_the_human_override(tmp_path: Path) -> None:
    div = _gated(tmp_path)
    assert _run(div, "done", "CLD-001", "--force").exit_code == 0
    assert _item(div, "CLD-001").state == ItemState.DONE


def test_worker_cannot_inspect_own_item(tmp_path: Path) -> None:
    div = _gated(tmp_path)
    result = _run(div, "inspect", "CLD-001", "--pass", agent="worker")
    assert result.exit_code == 1
    assert "own work" in result.output
    assert _item(div, "CLD-001").state != ItemState.DONE


def test_other_agent_inspect_pass_completes_item(tmp_path: Path) -> None:
    div = _gated(tmp_path)
    result = _run(div, "inspect", "CLD-001", "--pass", agent="inspector-1")
    assert result.exit_code == 0, result.output
    assert _item(div, "CLD-001").state == ItemState.DONE


def test_assigned_inspector_is_enforced(tmp_path: Path) -> None:
    div = _gated(tmp_path)
    _run(div, "role", "enable", "inspector", "--agent", "inspector-1")
    assert _run(div, "inspect", "CLD-001", "--pass", agent="someone-else").exit_code == 1
    assert _run(div, "inspect", "CLD-001", "--pass", agent="inspector-1").exit_code == 0


try:
    from dot_swarm_mcp.server import call_tool
    HAS_MCP = True
except Exception:  # mcp SDK missing or incompatible
    HAS_MCP = False


@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_done_and_inspect_respect_gate(tmp_path: Path, monkeypatch) -> None:
    div = _gated(tmp_path)
    monkeypatch.setenv("SWARM_ROOT", str(div))
    monkeypatch.delenv("DOT_SWARM_AGENT_ID", raising=False)

    out = await call_tool("swarm_done", {"id": "CLD-001", "agent_id": "worker", "path": "."})
    assert "Inspector role is enabled" in out[0].text
    out = await call_tool("swarm_inspect", {
        "id": "CLD-001", "inspector_id": "worker", "status": "pass", "path": "."})
    assert "own work" in out[0].text
    out = await call_tool("swarm_inspect", {
        "id": "CLD-001", "inspector_id": "inspector-1", "status": "fail", "path": "."})
    assert "reason" in out[0].text
    assert _item(div, "CLD-001").state != ItemState.DONE

    out = await call_tool("swarm_inspect", {
        "id": "CLD-001", "inspector_id": "inspector-1", "status": "pass", "path": "."})
    assert "PASSED" in out[0].text
    assert _item(div, "CLD-001").state == ItemState.DONE


def test_ai_ops_done_respects_gate(tmp_path: Path) -> None:
    from dot_swarm.ai_ops import execute_operations
    div = _gated(tmp_path)
    out = execute_operations(SwarmPaths.find(div), [{"op": "done", "id": "CLD-001"}], "worker")
    assert "inspect" in " ".join(out)
    assert _item(div, "CLD-001").state != ItemState.DONE


# ---------------------------------------------------------------------------
# Division codes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("folder, code", [
    ("my-api", "MYAP"), ("dot_swarm", "SWC"), ("web_app", "WEBA"), ("2fa", "D2FA"),
])
def test_folder_derived_code_is_parseable(tmp_path: Path, folder: str, code: str) -> None:
    div = _make_swarm(tmp_path, name=folder)
    _run(div, "add", "first")
    _run(div, "add", "second")
    ids = [i.id for i in read_queue(SwarmPaths.find(div))[1]]
    assert ids == [f"{code}-001", f"{code}-002"]


def test_context_division_code_wins_over_folder(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path, name="web-app",
                      context="# Context\n\n**Division code**: WEB\n")
    assert _division_code_from_paths(SwarmPaths.find(div)) == "WEB"
    _run(div, "add", "first")
    assert _item(div, "WEB-001") is not None


def test_add_rejects_unparseable_code(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    result = _run(div, "add", "x", "--code", "MY-A")
    assert result.exit_code == 1
    assert "Invalid division code" in result.output


def test_init_rejects_unparseable_code(tmp_path: Path) -> None:
    result = CliRunner().invoke(cli, ["--path", str(tmp_path), "init",
                                      "--division-code", "my_api"])
    assert result.exit_code == 1
    assert not (tmp_path / ".swarm").exists()


# ---------------------------------------------------------------------------
# queue.md format
# ---------------------------------------------------------------------------

def test_multiline_notes_survive_a_write(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    _run(div, "add", "multi", "--notes", "line one\nline two")
    _run(div, "add", "another")       # rewrites queue.md
    assert _item(div, "CLD-001").notes == "line one\nline two"


def test_hand_wrapped_notes_and_standalone_project_survive(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    (div / ".swarm" / "queue.md").write_text(
        "# Queue\n\n## Active\n\n## Pending\n\n"
        "- [ ] [CLD-030] [OPEN] Security docs\n"
        "      project: docs\n"
        "      priority: high\n"
        "      notes: Expand with:\n"
        "             BENEFITS: signed trail\n"
        "             refs: this line is wrapped text, not a field\n"
        "      depends: CLD-001\n"
        "\n## Done\n", encoding="utf-8")
    _run(div, "add", "another")
    item = _item(div, "CLD-030")
    assert item.project == "docs"
    assert item.notes == ("Expand with:\nBENEFITS: signed trail\n"
                          "refs: this line is wrapped text, not a field")
    assert item.refs == []
    assert item.depends == ["CLD-001"]


def test_project_with_pipe_survives_rewrite(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    (div / ".swarm" / "queue.md").write_text(
        "# Queue\n\n## Active\n\n## Pending\n\n"
        "- [ ] [CLD-007] [OPEN] Cache tier\n"
        "      priority: low\n"
        "      project: truecast | division: CLD + ADM\n"
        "\n## Done\n", encoding="utf-8")
    _run(div, "add", "another")
    _run(div, "add", "and another")
    assert _item(div, "CLD-007").project == "truecast | division: CLD + ADM"


def test_partial_note_is_not_duplicated_on_rewrite(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    _run(div, "add", "Task A")
    _run(div, "claim", "CLD-001")
    _run(div, "partial", "CLD-001", "--note", "half way")
    _run(div, "add", "Task B")
    _run(div, "add", "Task C")
    assert _item(div, "CLD-001").notes.count("half way") == 1


# ---------------------------------------------------------------------------
# claim
# ---------------------------------------------------------------------------

def test_claim_appends_to_active_items(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    _run(div, "add", "A")
    _run(div, "add", "B")
    _run(div, "claim", "CLD-001")
    _run(div, "claim", "CLD-002")
    paths = SwarmPaths.find(div)
    assert read_state(paths)["Active items"] == "CLD-001, CLD-002"
    _run(div, "done", "CLD-001")
    assert read_state(paths)["Active items"] == "CLD-002"


def test_merge_active_items_keeps_free_text() -> None:
    assert merge_active_items("(none)", add="X-1") == "X-1"
    assert merge_active_items("X-1 (auth), X-2", add="X-1") == "X-1 (auth), X-2"
    assert merge_active_items("X-1 (auth), X-2", remove="X-1") == "X-2"
    assert merge_active_items("X-2", remove="X-2") == "(none)"


def test_agent_can_reclaim_own_partial(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    paths = SwarmPaths.find(div)
    add_item(paths, "Task A")
    claim_item(paths, "CLD-001", "worker")
    partial_item(paths, "CLD-001", "worker", note="checkpoint")
    item = claim_item(paths, "CLD-001", "worker")
    assert item.state == ItemState.CLAIMED
    assert item.claimed_by == "worker"


def test_other_agent_still_needs_compete(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    paths = SwarmPaths.find(div)
    add_item(paths, "Task A")
    claim_item(paths, "CLD-001", "worker")
    partial_item(paths, "CLD-001", "worker")
    with pytest.raises(ValueError, match="already claimed"):
        claim_item(paths, "CLD-001", "rival")


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------

def test_swarm_stale_hours_env_is_read(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    _run(div, "add", "A")
    _run(div, "claim", "CLD-001")
    claims = div / ".swarm" / "claims"
    # Back-date the claim 10 hours, in queue.md and in the claims trail.
    from datetime import timedelta
    from dot_swarm.models import utcnow
    old = utcnow() - timedelta(hours=10)
    for p in claims.glob("*.json"):
        d = json.loads(p.read_text(encoding="utf-8"))
        d["timestamp"] = old.isoformat() + "Z"
        p.write_text(json.dumps(d), encoding="utf-8")

    quiet = CliRunner().invoke(cli, ["--path", str(div), "audit"])
    assert "CLD-001" not in quiet.output          # 10 h < default 48 h
    loud = CliRunner().invoke(cli, ["--path", str(div), "audit"],
                              env={"SWARM_STALE_HOURS": "5"})
    assert "CLD-001" in loud.output


def test_audit_reports_blocked_item_in_pending(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    _run(div, "add", "A")
    _run(div, "block", "CLD-001", "vendor")
    result = CliRunner().invoke(cli, ["--path", str(div), "audit"])
    assert "BLOCKED" in result.output
