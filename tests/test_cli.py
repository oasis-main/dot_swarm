"""Tests for the swarm CLI commands using Click's test runner."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from dot_swarm.cli import cli


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_swarm(tmp_path: Path, name: str = "oasis-cloud") -> Path:
    """Create a minimal division with .swarm/ inside tmp_path."""
    div = tmp_path / name
    div.mkdir()
    swarm = div / ".swarm"
    swarm.mkdir()
    (swarm / "queue.md").write_text(
        "# Queue\n\n## Active\n\n(none)\n\n## Pending\n\n(none)\n\n## Done\n\n(none)\n",
        encoding='utf-8',
    )
    (swarm / "state.md").write_text(
        "# State\n\n**Last touched**: 2026-01-01T00:00Z by test\n"
        "**Current focus**: testing\n**Active items**: (none)\n**Blockers**: (none)\n",
        encoding='utf-8',
    )
    (swarm / "memory.md").write_text("# Memory\n\n(empty)\n", encoding='utf-8')
    (swarm / "context.md").write_text("# Context\n", encoding='utf-8')
    (swarm / "BOOTSTRAP.md").write_text("# Bootstrap\n", encoding='utf-8')
    return div


# ---------------------------------------------------------------------------
# swarm status
# ---------------------------------------------------------------------------

def test_status_empty_queue(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["--path", str(div), "status"])
    assert result.exit_code == 0
    assert "testing" in result.output  # current focus


# ---------------------------------------------------------------------------
# swarm add
# ---------------------------------------------------------------------------

def test_add_creates_item(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, [
        "--path", str(div), "add", "Fix the thing",
        "--priority", "high", "--project", "infra",
    ])
    assert result.exit_code == 0
    queue = (div / ".swarm" / "queue.md").read_text(encoding='utf-8')
    assert "Fix the thing" in queue
    assert "high" in queue


# ---------------------------------------------------------------------------
# swarm claim / done lifecycle
# ---------------------------------------------------------------------------

def test_claim_and_done(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()

    runner.invoke(cli, ["--path", str(div), "add", "Task A"])
    queue = (div / ".swarm" / "queue.md").read_text(encoding='utf-8')
    # Extract item ID (CLD-001)
    import re
    m = re.search(r'\[(\w+-\d+)\]', queue)
    assert m, "No item ID found in queue"
    item_id = m.group(1)

    result = runner.invoke(cli, ["--path", str(div), "claim", item_id, "--agent", "test-agent"])
    assert result.exit_code == 0
    assert "CLAIMED" in (div / ".swarm" / "queue.md").read_text(encoding='utf-8')

    result = runner.invoke(cli, ["--path", str(div), "done", item_id, "--agent", "test-agent"])
    assert result.exit_code == 0
    assert "DONE" in (div / ".swarm" / "queue.md").read_text(encoding='utf-8')


# ---------------------------------------------------------------------------
# swarm ls
# ---------------------------------------------------------------------------

def test_ls_shows_items(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["--path", str(div), "add", "Item one"])
    runner.invoke(cli, ["--path", str(div), "add", "Item two", "--priority", "high"])

    result = runner.invoke(cli, ["--path", str(div), "ls", "--section", "pending"])
    assert result.exit_code == 0
    assert "Item one" in result.output
    assert "Item two" in result.output


def test_ls_priority_filter(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["--path", str(div), "add", "Low item", "--priority", "low"])
    runner.invoke(cli, ["--path", str(div), "add", "High item", "--priority", "high"])

    result = runner.invoke(cli, ["--path", str(div), "ls", "--priority", "high"])
    assert result.exit_code == 0
    assert "High item" in result.output
    assert "Low item" not in result.output


# ---------------------------------------------------------------------------
# swarm report
# ---------------------------------------------------------------------------

def test_report_stdout(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["--path", str(div), "add", "Report test item"])

    result = runner.invoke(cli, ["--path", str(tmp_path), "report"])
    assert result.exit_code == 0
    assert "dot_swarm Colony Report" in result.output
    assert "oasis-cloud" in result.output


def test_report_to_file(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    out_file = tmp_path / "REPORT.md"
    runner = CliRunner()
    result = runner.invoke(cli, ["--path", str(tmp_path), "report", "--out", str(out_file)])
    assert result.exit_code == 0
    assert out_file.exists()
    assert "dot_swarm Colony Report" in out_file.read_text(encoding='utf-8')


# ---------------------------------------------------------------------------
# swarm explore
# ---------------------------------------------------------------------------

def test_explore_finds_divisions(tmp_path: Path) -> None:
    _make_swarm(tmp_path, "oasis-cloud")
    _make_swarm(tmp_path, "oasis-firmware")
    runner = CliRunner()
    result = runner.invoke(cli, ["--path", str(tmp_path), "explore"])
    assert result.exit_code == 0
    assert "oasis-cloud" in result.output
    assert "oasis-firmware" in result.output


# ---------------------------------------------------------------------------
# swarm handoff
# ---------------------------------------------------------------------------

def test_handoff_produces_output(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["--path", str(div), "handoff"])
    assert result.exit_code == 0
    assert len(result.output) > 20


# ---------------------------------------------------------------------------
# Hash IDs + new edge flags
# ---------------------------------------------------------------------------

def test_add_with_hash_id(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["--path", str(div), "add", "hashy", "--hash-id"])
    assert result.exit_code == 0
    queue = (div / ".swarm" / "queue.md").read_text(encoding='utf-8')
    import re
    assert re.search(r"\[sw-[a-f0-9]+\]", queue), queue


def test_add_with_supersedes_and_duplicates(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["--path", str(div), "add", "old"])
    queue = (div / ".swarm" / "queue.md").read_text(encoding='utf-8')
    import re
    old_id = re.search(r"\[(\w+-\d+)\]", queue).group(1)
    result = runner.invoke(cli, [
        "--path", str(div), "add", "new",
        "--supersedes", old_id,
    ])
    assert result.exit_code == 0
    queue = (div / ".swarm" / "queue.md").read_text(encoding='utf-8')
    assert f"supersedes: {old_id}" in queue


def test_ready_json_includes_edge_fields(tmp_path: Path) -> None:
    import json
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["--path", str(div), "add", "alpha"])
    result = runner.invoke(cli, ["--path", str(div), "ready", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert isinstance(data, list) and data
    assert "supersedes" in data[0] and "duplicates" in data[0]


def test_ls_json_output(tmp_path: Path) -> None:
    import json
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["--path", str(div), "add", "alpha"])
    result = runner.invoke(cli, ["--path", str(div), "ls", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert "pending" in data
    assert data["pending"][0]["description"] == "alpha"


def test_status_json_output(tmp_path: Path) -> None:
    import json
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["--path", str(div), "add", "alpha"])
    result = runner.invoke(cli, ["--path", str(div), "status", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["counts"]["pending"] >= 1


def test_ready_hides_superseded_via_cli(tmp_path: Path) -> None:
    import json
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    runner.invoke(cli, ["--path", str(div), "add", "old"])
    queue = (div / ".swarm" / "queue.md").read_text(encoding='utf-8')
    import re
    old_id = re.search(r"\[(\w+-\d+)\]", queue).group(1)
    runner.invoke(cli, ["--path", str(div), "add", "new", "--supersedes", old_id])

    result = runner.invoke(cli, ["--path", str(div), "ready", "--json"])
    data = json.loads(result.output)
    descs = {i["description"] for i in data}
    assert "old" not in descs
    assert "new" in descs
