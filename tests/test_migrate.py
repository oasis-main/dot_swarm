"""Tests for ``swarm migrate`` — legacy .swarm/ → v1.0 layout fix-up."""

from __future__ import annotations

from datetime import datetime

import pytest

from dot_swarm.migrate import migrate_swarm
from dot_swarm.models import ItemState, SwarmPaths, WorkItem
from dot_swarm.operations import read_queue, write_queue


def _legacy_swarm(tmp_path):
    """Build a 0.3-era directory: queue.md present, claims/ missing,
    federation/ present without strangers, gitignore missing the new keys."""
    s = tmp_path / ".swarm"
    s.mkdir()
    # legacy queue.md with one CLAIMED item but no claims/ dir
    (s / "queue.md").write_text(
        "# Queue — legacy (Division Level)\n\n"
        "## Active\n\n"
        "- [>] [LEG-001] [CLAIMED · agent-A · 2026-04-01T10:00Z] Test item\n"
        "      priority: high | project: misc\n\n"
        "## Pending\n\n"
        "## Done\n\n",
        encoding='utf-8',
    )
    # legacy gitignore — only the original signing-key entry
    (s / ".gitignore").write_text(".signing_key\nquarantine/\ntrail.log\n", encoding='utf-8')
    # legacy federation/ — has trusted_peers + inbox + outbox but no strangers/
    fed = s / "federation"
    (fed / "trusted_peers").mkdir(parents=True)
    (fed / "inbox").mkdir(parents=True)
    (fed / "outbox").mkdir(parents=True)
    return s


def test_migrate_creates_missing_layout(tmp_path):
    s = _legacy_swarm(tmp_path)
    report = migrate_swarm(s, dry_run=False)

    assert (s / "claims").is_dir()
    assert (s / "federation" / "strangers").is_dir()
    assert (s / "federation" / "strangers" / "rejected").is_dir()
    assert ".swarm_key" in (s / ".gitignore").read_text(encoding='utf-8')
    assert ".swarm_key.old" in (s / ".gitignore").read_text(encoding='utf-8')

    summary = " | ".join(report.actions)
    assert "claims/" in summary
    assert "strangers" in summary
    assert "gitignore" in summary
    assert report.already_current is False


def test_migrate_backfills_active_claim_records(tmp_path):
    s = _legacy_swarm(tmp_path)
    migrate_swarm(s, dry_run=False)

    # The single CLAIMED item should now have a claim record
    claim_files = list((s / "claims").glob("LEG-001_*.json"))
    assert len(claim_files) == 1

    # Resolver still reports the same active state
    paths = SwarmPaths.from_swarm_dir(s)
    active, pending, done = read_queue(paths)
    target = next(i for i in active if i.id == "LEG-001")
    assert target.state == ItemState.CLAIMED
    assert target.claimed_by == "agent-A"


def test_migrate_dry_run_writes_nothing(tmp_path):
    s = _legacy_swarm(tmp_path)
    report = migrate_swarm(s, dry_run=True)

    # Layout still legacy
    assert not (s / "claims").is_dir()
    assert not (s / "federation" / "strangers").is_dir()
    assert ".swarm_key" not in (s / ".gitignore").read_text(encoding='utf-8')
    # Report says what would happen
    assert report.actions == []
    assert any("claims/" in n for n in report.needed)
    assert any("strangers" in n for n in report.needed)
    assert any("gitignore" in n for n in report.needed)


def test_migrate_is_idempotent(tmp_path):
    s = _legacy_swarm(tmp_path)
    migrate_swarm(s, dry_run=False)
    second = migrate_swarm(s, dry_run=False)

    assert second.actions == []
    assert second.needed == []
    assert second.already_current is True


def test_migrate_does_not_duplicate_existing_claim_records(tmp_path):
    s = _legacy_swarm(tmp_path)
    migrate_swarm(s, dry_run=False)
    claim_files_before = sorted((s / "claims").glob("LEG-001_*.json"))
    migrate_swarm(s, dry_run=False)
    claim_files_after = sorted((s / "claims").glob("LEG-001_*.json"))
    assert claim_files_before == claim_files_after


def test_migrate_creates_gitignore_if_missing(tmp_path):
    s = tmp_path / ".swarm"
    s.mkdir()
    (s / "queue.md").write_text(
        "# Queue — minimal\n\n## Active\n\n## Pending\n\n## Done\n\n",
        encoding='utf-8',
    )
    # No gitignore at all
    report = migrate_swarm(s, dry_run=False)
    assert (s / ".gitignore").exists()
    contents = (s / ".gitignore").read_text(encoding='utf-8')
    for entry in (".signing_key", ".swarm_key", ".swarm_key.old", "trail.log"):
        assert entry in contents
    assert any("gitignore" in a for a in report.actions)
