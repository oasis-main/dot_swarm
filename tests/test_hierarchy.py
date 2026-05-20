"""Tests for hierarchical alignment (swarm ascend / descend)."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from dot_swarm.cli import cli
from dot_swarm.operations import add_item


@pytest.fixture()
def hierarchy(tmp_path: Path):
    """
    Create a hierarchical structure:
    /org/.swarm/
    /org/division-a/.swarm/
    """
    org_root = tmp_path / "oasis-x"
    org_root.mkdir()
    org_swarm = org_root / ".swarm"
    org_swarm.mkdir()
    
    # Init files
    for f in ["queue.md", "state.md", "context.md", "BOOTSTRAP.md"]:
        (org_swarm / f).write_text(f"# Org {f}\n", encoding='utf-8')
    
    # Org state template
    (org_swarm / "state.md").write_text(
        "# State - Org\n\n**Last touched**: 2026-03-31T00:00Z by test\n"
        "**Current focus**: Org focus\n**Active items**: (none)\n**Blockers**: None\n",
        encoding='utf-8',
    )

    div_root = org_root / "oasis-cloud"
    div_root.mkdir()
    div_swarm = div_root / ".swarm"
    div_swarm.mkdir()
    
    for f in ["queue.md", "state.md", "context.md", "BOOTSTRAP.md"]:
        (div_swarm / f).write_text(f"# Div {f}\n", encoding='utf-8')

    # Div state template
    (div_swarm / "state.md").write_text(
        "# State - Div\n\n**Last touched**: 2026-03-31T00:00Z by test\n"
        "**Current focus**: Div focus\n**Active items**: (none)\n**Blockers**: None\n",
        encoding='utf-8',
    )

    return org_root, div_root


def test_up_alignment(hierarchy):
    org_root, div_root = hierarchy
    from dot_swarm.models import SwarmPaths
    
    org_paths = SwarmPaths.find(org_root)
    div_paths = SwarmPaths.find(div_root)
    
    # Add items
    org_item = add_item(org_paths, "Org initiative", division_code="ORG")
    div_item = add_item(div_paths, "Local task", division_code="CLD", depends=[org_item.id])
    
    runner = CliRunner()
    # Run from division
    result = runner.invoke(cli, ["--path", str(div_root), "ascend"])
    
    assert result.exit_code == 0
    assert "Checking alignment: oasis-cloud → oasis-x" in result.output
    assert "CLD-001" in result.output
    assert "depends on" in result.output
    assert "ORG-001" in result.output


def test_down_alignment(hierarchy):
    org_root, div_root = hierarchy
    from dot_swarm.models import SwarmPaths
    
    org_paths = SwarmPaths.find(org_root)
    div_paths = SwarmPaths.find(div_root)
    
    # Add items
    org_item = add_item(org_paths, "Org initiative", division_code="ORG")
    add_item(div_paths, "Local task", division_code="CLD", refs=[org_item.id])
    
    runner = CliRunner()
    # Run from org
    result = runner.invoke(cli, ["--path", str(org_root), "descend"])
    
    assert result.exit_code == 0
    assert "Checking alignment: oasis-x ↴ 1 children" in result.output
    assert "Sub-division: oasis-cloud" in result.output
    assert "ORG-001" in result.output
    assert "referenced by" in result.output
    assert "CLD-001" in result.output


def test_up_no_parent(tmp_path):
    # No .swarm at all
    runner = CliRunner()
    result = runner.invoke(cli, ["--path", str(tmp_path), "ascend"])
    assert result.exit_code != 0
    assert "No .swarm/ directory found" in result.output


def test_up_at_top_level(hierarchy):
    org_root, _ = hierarchy
    runner = CliRunner()
    # Run from org (which has no parent)
    result = runner.invoke(cli, ["--path", str(org_root), "ascend"])
    assert result.exit_code == 0
    assert "No parent division found" in result.output


def test_down_no_children(hierarchy):
    _, div_root = hierarchy
    runner = CliRunner()
    # Run from division (which has no children)
    result = runner.invoke(cli, ["--path", str(div_root), "descend"])
    assert result.exit_code == 0
    assert "No sub-divisions found" in result.output
