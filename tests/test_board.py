"""Tests for the product board: parser, `swarm board init`, `swarm board sync`."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from dot_swarm import board as B
from dot_swarm.cli import cli

SAMPLE = """\
# Product board

Board updated: 2026-01-05 by claude-code
Board editor: claude-code
Operator: Sam

Text above the markers is free. A `## ` heading here is not a product.

## Not a product

<!-- board:start -->

## Garden planner
- tier: P1
- stage: beta (closed)
- epic: EPIC-GDN
- repos: garden-web
- goal: Plan a vegetable bed in `ten` minutes.
- now: Beta with 12 users.
- next: Open the beta.
- updated: 2026-01-04
- unknown line that is not a field

Tasks:
- [ ] new | owner: operator | status: needs a call | Pick the beta price
- [>] GDN-12 | owner: claude-code | Frost-date lookup
- [/] GDN-13 | owner: fleet:builder | status: half done | Seed import | CSV and JSON
- [!] GDN-14 | status: waits on GDN-13 | Planting calendar
- [x] GDN-01 | owner: harness | status: merged | Login
- [?] GDN-15 | owner: harness | Unknown state is open
not a task line

Decisions:
- T1 | Beta price | Blocks the launch | Needs a number | ref: garden-web/.swarm/queue.md
- T3 | Mobile app later? | ref: none
- T4 | not a tier

## Recipe box
- tier: P3
- stage: idea

<!-- board:end -->

## After the end marker
- tier: P1
"""


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def test_parse_header_lines():
    b = B.parse_board(SAMPLE)
    assert b.updated == "2026-01-05 by claude-code"
    assert b.editor == "claude-code"
    assert b.operator == "Sam"


def test_parse_only_reads_between_markers():
    names = [p.name for p in B.parse_board(SAMPLE).products]
    assert names == ["Garden planner", "Recipe box"]


def test_parse_fields():
    p = B.parse_board(SAMPLE).products[0]
    assert p.fields["tier"] == "P1"
    assert p.fields["stage"] == "beta (closed)"
    assert p.fields["goal"] == "Plan a vegetable bed in `ten` minutes."
    assert "unknown" not in p.fields


def test_parse_tasks():
    tasks = B.parse_board(SAMPLE).products[0].tasks
    assert [t.state for t in tasks] == ["open", "claimed", "in progress", "blocked", "done", "open"]
    assert tasks[0].id == "new" and tasks[0].owner == "operator" and tasks[0].status == "needs a call"
    assert tasks[1].status == "" and tasks[1].title == "Frost-date lookup"
    # Extra " | " parts stay in the title.
    assert tasks[2].owner == "fleet:builder" and tasks[2].title == "Seed import | CSV and JSON"
    # A task with no owner tag is unassigned.
    assert tasks[3].owner == "unassigned"


def test_parse_decisions():
    ds = B.parse_board(SAMPLE).products[0].decisions
    assert [d.tier for d in ds] == ["T1", "T3"]  # T4 is ignored
    assert ds[0].title == "Beta price"
    assert ds[0].context == "Blocks the launch Needs a number"
    assert ds[0].ref == "garden-web/.swarm/queue.md"
    assert ds[1].context == "" and ds[1].ref == "none"


def test_parse_crlf_same_as_lf():
    assert B.parse_board(SAMPLE.replace("\n", "\r\n")).to_dict() == B.parse_board(SAMPLE).to_dict()


@pytest.mark.parametrize("text", ["no markers", "<!-- board:end -->\n<!-- board:start -->\n"])
def test_parse_without_markers_raises(text):
    with pytest.raises(B.BoardError):
        B.parse_board(text)


def test_check_board_warnings():
    warnings = B.check_board(B.parse_board(SAMPLE))
    assert any("GDN-14 has no owner" in w for w in warnings)
    assert not any("Garden planner: stage" in w for w in warnings)  # "beta (closed)" reads as beta
    bad = B.parse_board("<!-- board:start -->\n## X\n- tier: P9\n- stage: done\n<!-- board:end -->")
    warnings = B.check_board(bad)
    assert any("tier 'P9'" in w for w in warnings)
    assert any("stage 'done'" in w for w in warnings)


def test_template_parses_clean():
    b = B.parse_board(B.template_text())
    assert b.products, "the starter board needs at least one example product"
    assert b.editor and b.operator
    assert B.check_board(b) == []


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")
def test_js_parser_matches_python_parser(tmp_path):
    """board.js (the page) and board.py (the CLI) must read the format the same way."""
    sample = tmp_path / "sample.md"
    sample.write_text(SAMPLE, encoding="utf-8")
    script = (
        "const { parseBoard } = require(process.argv[1]);"
        "const fs = require('fs');"
        "process.stdout.write(JSON.stringify(parseBoard(fs.readFileSync(process.argv[2], 'utf8'))));"
    )
    for text in (SAMPLE, B.template_text()):
        sample.write_text(text, encoding="utf-8")
        out = subprocess.run(
            ["node", "-e", script, str(B.TEMPLATE_DIR / "board.js"), str(sample)],
            capture_output=True, text=True, check=True,
        ).stdout
        assert json.loads(out) == B.parse_board(text).to_dict()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def div(tmp_path: Path) -> Path:
    (tmp_path / ".swarm").mkdir()
    return tmp_path


def _run(div: Path, *args: str):
    return CliRunner().invoke(cli, ["--path", str(div), "board", *args])


def test_board_needs_a_swarm_dir(tmp_path):
    r = _run(tmp_path, "init")
    assert r.exit_code == 1
    assert "swarm init" in r.output


def test_board_init_writes_template(div):
    r = _run(div, "init")
    assert r.exit_code == 0, r.output
    text = (div / ".swarm" / "PRODUCTS.md").read_text(encoding="utf-8")
    assert "{{TODAY}}" not in text
    assert re.search(r"^Board updated: \d{4}-\d{2}-\d{2} ", text, re.M)
    assert B.START_MARKER in text and B.END_MARKER in text


def test_board_init_refuses_to_replace(div):
    board = div / ".swarm" / "PRODUCTS.md"
    board.write_text("mine", encoding="utf-8")
    r = _run(div, "init")
    assert r.exit_code == 1
    assert "--force" in r.output
    assert board.read_text(encoding="utf-8") == "mine"
    assert _run(div, "init", "--force").exit_code == 0
    assert board.read_text(encoding="utf-8") != "mine"


def test_board_sync_without_board(div):
    r = _run(div, "sync")
    assert r.exit_code == 1
    assert "swarm board init" in r.output


def _read_data(div: Path) -> dict:
    js = (div / ".swarm" / "board" / "board-data.js").read_text(encoding="utf-8")
    m = re.search(r"^window\.BOARD = (.*);$", js, re.M)
    assert m, js
    return json.loads(m.group(1))


def test_board_sync_writes_page(div):
    (div / ".swarm" / "PRODUCTS.md").write_text(SAMPLE, encoding="utf-8")
    r = _run(div, "sync")
    assert r.exit_code == 0, r.output
    out = div / ".swarm" / "board"
    for name in (*B.STATIC_FILES, B.DATA_FILE):
        assert (out / name).is_file(), name
    assert (out / "board.js").read_text(encoding="utf-8") == (B.TEMPLATE_DIR / "board.js").read_text(encoding="utf-8")
    data = _read_data(div)
    assert data["md"] == SAMPLE
    assert data["source"] == "PRODUCTS.md"
    assert data["synced"]
    assert "2 products" in r.output
    assert "file://" in r.output
    assert "GDN-14 has no owner" in r.output
    assert not list(out.glob("*.tmp"))


def test_board_sync_replaces_edited_static_file(div):
    (div / ".swarm" / "PRODUCTS.md").write_text(SAMPLE, encoding="utf-8")
    _run(div, "sync")
    page = div / ".swarm" / "board" / "index.html"
    page.write_text("edited", encoding="utf-8")
    _run(div, "sync")
    assert page.read_text(encoding="utf-8") == (B.TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")


def test_board_sync_parse_error_still_writes_data(div):
    (div / ".swarm" / "PRODUCTS.md").write_text("# no markers\n", encoding="utf-8")
    r = _run(div, "sync")
    assert r.exit_code == 1
    assert "board markers not found" in r.output
    assert _read_data(div)["md"] == "# no markers\n"


def test_init_then_sync(div):
    assert _run(div, "init").exit_code == 0
    r = _run(div, "sync")
    assert r.exit_code == 0, r.output
    assert "⚠" not in r.output  # the starter board has no warnings


def test_watch_syncs_on_change(div):
    board = div / ".swarm" / "PRODUCTS.md"
    board.write_text(SAMPLE, encoding="utf-8")
    B.sync_board(div / ".swarm")
    results = []
    edits = iter([
        lambda: None,                                                   # poll 1: no change
        lambda: board.write_text(SAMPLE + "\nmore\n", encoding="utf-8"),  # poll 2: change
        lambda: None,                                                   # poll 3: no change
    ])

    def fake_sleep(_seconds):
        next(edits)()

    B.watch_board(div / ".swarm", results.append, interval=0, cycles=3, sleep=fake_sleep)
    assert len(results) == 1
    assert _read_data(div)["md"].endswith("more\n")


def test_skill_file_has_frontmatter():
    skill = Path(__file__).resolve().parents[1] / "skills" / "product-board" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md needs YAML frontmatter"
    keys = dict(line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)
    assert keys.get("name", "").strip() == "product-board"
    assert keys.get("description", "").strip()
