"""Tests for the colony summary — the data source behind `swarm gui`.

Before these tests, get_colony_summary() had zero coverage and called an
undefined name (_fmt_ts, defined in models.py but never imported into
operations.py). Any division holding a claimed_at or done_at timestamp —
i.e. every division with real history — raised NameError and was rendered
in the GUI as an error card instead of a queue.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dot_swarm.models import Priority, SwarmPaths
from dot_swarm.operations import (
    add_item,
    claim_item,
    discover_divisions,
    done_item,
    get_colony_summary,
    is_division_copy,
)


def _make_division(root: Path, name: str) -> SwarmPaths:
    """Create a minimal, parseable .swarm/ division under root."""
    div_root = root / name
    div_root.mkdir(parents=True, exist_ok=True)
    swarm = div_root / ".swarm"
    swarm.mkdir()
    (swarm / "queue.md").write_text(
        "# Queue\n\n## Active\n\n## Pending\n\n## Done\n",
        encoding="utf-8",
    )
    (swarm / "state.md").write_text(
        "# State\n\n**Last touched**: 2026-01-01T00:00Z by test\n"
        "**Current focus**: testing\n**Active items**: (none)\n**Blockers**: (none)\n",
        encoding="utf-8",
    )
    (swarm / "memory.md").write_text("# Memory\n\n(empty)\n", encoding="utf-8")
    (swarm / "context.md").write_text("# Context\n\nTest.\n", encoding="utf-8")
    return SwarmPaths.find(div_root)


@pytest.fixture()
def colony(tmp_path: Path) -> Path:
    """An org root with its own .swarm/ plus one sub-division."""
    _make_division(tmp_path, ".")  # org root itself
    _make_division(tmp_path, "oasis-cloud")
    return tmp_path


# ---------------------------------------------------------------------------
# Regression: the undefined _fmt_ts
# ---------------------------------------------------------------------------

def test_summary_survives_claimed_and_done_timestamps(colony: Path) -> None:
    """A division with real claim/done history must summarize without error.

    This is the exact shape that used to raise NameError: _fmt_ts.
    """
    paths = SwarmPaths.find(colony / "oasis-cloud")
    claimed = add_item(paths, "still in flight", priority=Priority.HIGH)
    finished = add_item(paths, "already shipped", priority=Priority.LOW)
    claim_item(paths, claimed.id, "agent-a")
    claim_item(paths, finished.id, "agent-b")
    done_item(paths, finished.id, "agent-b")

    data = get_colony_summary(colony)

    div = next(d for d in data["divisions"] if d["name"] == "oasis-cloud")
    assert "error" not in div, div.get("error")

    items = div["queue"]["active"] + div["queue"]["pending"] + div["queue"]["done"]
    stamped = [i for i in items if i["claimed_at"] or i["done_at"]]
    assert stamped, "fixture produced no timestamps — the regression path is untested"
    for item in stamped:
        for field in ("claimed_at", "done_at"):
            if item[field] is not None:
                # _fmt_ts output, not a raw datetime
                assert isinstance(item[field], str)
                assert item[field].endswith("Z")


def test_summary_is_json_serializable(colony: Path) -> None:
    """The GUI serves this dict through json.dumps — enums and datetimes
    must already be flattened to strings by get_colony_summary."""
    paths = SwarmPaths.find(colony / "oasis-cloud")
    item = add_item(paths, "serialize me", priority=Priority.HIGH)
    claim_item(paths, item.id, "agent-a")

    blob = json.dumps(get_colony_summary(colony))

    assert '"priority": "high"' in blob
    assert '"state":' in blob


def test_summary_reports_per_division_errors_without_failing(tmp_path: Path) -> None:
    """One unreadable division must not take down the whole summary."""
    _make_division(tmp_path, ".")
    _make_division(tmp_path, "good")
    broken = _make_division(tmp_path, "broken")
    broken.state.write_text("\x00 not markdown", encoding="utf-8")
    broken.queue.unlink()
    broken.queue.mkdir()  # a directory where a file belongs

    data = get_colony_summary(tmp_path)

    names = {d["name"] for d in data["divisions"]}
    assert "good" in names
    good = next(d for d in data["divisions"] if d["name"] == "good")
    assert "error" not in good


# ---------------------------------------------------------------------------
# Division copies (git worktrees, vendored trees)
# ---------------------------------------------------------------------------

def test_worktree_copy_is_not_a_division(colony: Path) -> None:
    """A git worktree carries a full .swarm/ with it. That copy is not a
    peer division of the repo it was cut from."""
    _make_division(colony / ".claude" / "worktrees", "sleepy-stonebraker-fad760")

    found = {p.name for p, _ in discover_divisions(colony, depth=3)}

    assert "oasis-cloud" in found
    assert "sleepy-stonebraker-fad760" not in found


def test_vendored_copy_is_not_a_division(colony: Path) -> None:
    _make_division(colony / "node_modules", "some-package")

    found = {p.name for p, _ in discover_divisions(colony, depth=3)}

    assert "some-package" not in found


def test_root_under_a_dot_directory_is_not_self_excluded(tmp_path: Path) -> None:
    """Only the components BETWEEN root and division are inspected — a root
    that itself lives under a dot-directory still finds its own children."""
    root = tmp_path / ".config" / "myorg"
    _make_division(root, ".")
    _make_division(root, "child")

    found = {p.name for p, _ in discover_divisions(root, depth=2)}

    assert "child" in found


def test_is_division_copy_ignores_unrelated_paths(tmp_path: Path) -> None:
    """A path outside root is not classified as a copy."""
    assert is_division_copy(tmp_path, tmp_path / "plain") is False
    assert is_division_copy(tmp_path, tmp_path / ".claude" / "wt" / "x") is True
    assert is_division_copy(tmp_path, Path("/somewhere/else")) is False
