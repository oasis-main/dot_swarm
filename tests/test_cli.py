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
        "# Queue\n\n## Active\n\n(none)\n\n## Pending\n\n(none)\n\n## Done\n\n(none)\n"
    )
    (swarm / "state.md").write_text(
        "# State\n\n**Last touched**: 2026-01-01T00:00Z by test\n"
        "**Current focus**: testing\n**Active items**: (none)\n**Blockers**: (none)\n"
    )
    (swarm / "memory.md").write_text("# Memory\n\n(empty)\n")
    (swarm / "context.md").write_text("# Context\n")
    (swarm / "BOOTSTRAP.md").write_text("# Bootstrap\n")
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
    queue = (div / ".swarm" / "queue.md").read_text()
    assert "Fix the thing" in queue
    assert "high" in queue


# ---------------------------------------------------------------------------
# swarm claim / done lifecycle
# ---------------------------------------------------------------------------

def test_claim_and_done(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()

    runner.invoke(cli, ["--path", str(div), "add", "Task A"])
    queue = (div / ".swarm" / "queue.md").read_text()
    # Extract item ID (CLD-001)
    import re
    m = re.search(r'\[(\w+-\d+)\]', queue)
    assert m, "No item ID found in queue"
    item_id = m.group(1)

    result = runner.invoke(cli, ["--path", str(div), "claim", item_id, "--agent", "test-agent"])
    assert result.exit_code == 0
    assert "CLAIMED" in (div / ".swarm" / "queue.md").read_text()

    result = runner.invoke(cli, ["--path", str(div), "done", item_id, "--agent", "test-agent"])
    assert result.exit_code == 0
    assert "DONE" in (div / ".swarm" / "queue.md").read_text()


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
# swarm comment / comments (SWC-051)
# ---------------------------------------------------------------------------

def test_comment_and_comments_round_trip(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli, ["--path", str(div), "comment", "SWC-001", "looks good", "--agent", "house"])
    assert result.exit_code == 0
    assert "Comment" in result.output

    result = runner.invoke(cli, ["--path", str(div), "comments", "SWC-001"])
    assert result.exit_code == 0
    assert "looks good" in result.output
    assert "house" in result.output


def test_comments_empty_thread(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli, ["--path", str(div), "comments", "SWC-404"])
    assert result.exit_code == 0
    assert "No comments" in result.output


def test_comment_reply_threading_via_cli(tmp_path: Path) -> None:
    div = _make_swarm(tmp_path)
    runner = CliRunner()

    runner.invoke(cli, ["--path", str(div), "comment", "SWC-001", "question?", "--agent", "house"])
    import re
    thread_file = (div / ".swarm" / "comments" / "SWC-001.jsonl").read_text()
    parent_id = re.search(r'"comment_id":"([a-f0-9]+)"', thread_file).group(1)

    result = runner.invoke(cli, [
        "--path", str(div), "comment", "SWC-001", "answer.",
        "--agent", "kolmogorov", "--reply-to", parent_id,
    ])
    assert result.exit_code == 0

    result = runner.invoke(cli, ["--path", str(div), "comments", "SWC-001"])
    assert f"reply to {parent_id}" in result.output
