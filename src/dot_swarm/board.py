"""Product board — one markdown file above the queues, plus a static page.

The board lives in ``.swarm/PRODUCTS.md``. Each product is a ``## `` heading
with ``- key: value`` fields, a ``Tasks:`` list and a ``Decisions:`` list,
between the ``<!-- board:start -->`` and ``<!-- board:end -->`` markers.
See ``docs/PRODUCT_BOARD.md`` for the format and the agent rules.

``swarm board sync`` copies a static page (``index.html``, ``board.css``,
``board.js``) into ``.swarm/board/`` and writes ``board-data.js``, which holds
the board text. The page loads ``board-data.js`` again every 30 seconds. A
``<script>`` tag works from a ``file://`` URL and ``fetch()`` does not, so the
board text goes into a small script file and no server is necessary.

The parser here is a port of ``parseBoard`` in ``templates/board/board.js``.
The two must read the format the same way; ``tests/test_board.py`` checks
both against one sample when Node is installed.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

START_MARKER = "<!-- board:start -->"
END_MARKER = "<!-- board:end -->"
BOARD_FILE = "PRODUCTS.md"
BOARD_DIR = "board"
DATA_FILE = "board-data.js"
STATIC_FILES = ("index.html", "board.css", "board.js")

#: Task state character -> state name. Keep in step with STATE in board.js.
STATE = {" ": "open", ">": "claimed", "/": "in progress", "!": "blocked", "x": "done", "X": "done"}
PRODUCT_TIERS = ("P1", "P2", "P3")
DECISION_TIERS = ("T1", "T2", "T3")
STAGES = ("idea", "design", "building", "beta", "live")

_FIELD_RE = re.compile(r"^- ([a-z][a-z0-9_-]*):\s*(.*)$")
_TASK_RE = re.compile(r"^- \[(.)\]\s*(.*)$")
_TASK_KV_RE = re.compile(r"^(owner|status):\s*(.*)$")
_DECISION_RE = re.compile(r"^- (T[123])\s*\|\s*(.*)$")
_REF_RE = re.compile(r"^ref:\s*(.*)$")
_HEADER_RE = {
    "updated": re.compile(r"^Board updated:\s*(.+)$", re.M),
    "editor": re.compile(r"^Board editor:\s*(.+)$", re.M),
    "operator": re.compile(r"^Operator:\s*(.+)$", re.M),
}

TEMPLATE_DIR = Path(__file__).parent / "templates" / "board"


class BoardError(ValueError):
    """The board file cannot be parsed."""


@dataclass
class Task:
    state: str
    id: str
    owner: str = "unassigned"
    status: str = ""
    title: str = ""


@dataclass
class Decision:
    tier: str
    title: str
    context: str = ""
    ref: str = ""


@dataclass
class Product:
    name: str
    fields: dict[str, str] = field(default_factory=dict)
    tasks: list[Task] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)


@dataclass
class Board:
    updated: str = ""
    editor: str = ""
    operator: str = ""
    products: list[Product] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _split_parts(text: str) -> list[str]:
    return [p.strip() for p in text.split(" | ")]


def parse_board(text: str) -> Board:
    """Parse the part of PRODUCTS.md between the board markers."""
    text = text.replace("\r\n", "\n")
    s = text.find(START_MARKER)
    e = text.find(END_MARKER)
    if s < 0 or e < s:
        raise BoardError("PRODUCTS.md: board markers not found")
    body = text[s:e]

    board = Board()
    for key, rx in _HEADER_RE.items():
        m = rx.search(text)
        if m:
            setattr(board, key, m.group(1).strip())

    for chunk in re.split(r"^## ", body, flags=re.M)[1:]:
        lines = chunk.split("\n")
        product = Product(name=lines[0].strip())
        mode = "fields"
        for raw in lines[1:]:
            line = raw.rstrip()
            if re.fullmatch(r"Tasks:\s*", line):
                mode = "tasks"
                continue
            if re.fullmatch(r"Decisions:\s*", line):
                mode = "decisions"
                continue
            if not line.strip() or line.startswith("<!--"):
                continue
            if mode == "fields":
                m = _FIELD_RE.match(line)
                if m:
                    product.fields[m.group(1)] = m.group(2)
            elif mode == "tasks":
                m = _TASK_RE.match(line)
                if not m:
                    continue
                parts = _split_parts(m.group(2))
                task = Task(state=STATE.get(m.group(1), "open"), id=parts[0])
                for part in parts[1:]:
                    kv = _TASK_KV_RE.match(part)
                    if kv:
                        setattr(task, kv.group(1), kv.group(2))
                    else:
                        task.title = f"{task.title} | {part}" if task.title else part
                product.tasks.append(task)
            else:
                m = _DECISION_RE.match(line)
                if not m:
                    continue
                parts = _split_parts(m.group(2))
                decision = Decision(tier=m.group(1), title=parts[0])
                for part in parts[1:]:
                    kv = _REF_RE.match(part)
                    if kv:
                        decision.ref = kv.group(1)
                    else:
                        decision.context = f"{decision.context} {part}" if decision.context else part
                product.decisions.append(decision)
        board.products.append(product)
    return board


def check_board(board: Board) -> list[str]:
    """Return warnings for values the page cannot place. An empty list is clean."""
    warnings: list[str] = []
    for p in board.products:
        tier = p.fields.get("tier", "")
        if tier not in PRODUCT_TIERS:
            warnings.append(f"{p.name}: tier {tier!r} is not one of {', '.join(PRODUCT_TIERS)}; the page does not show this product")
        stage = (p.fields.get("stage", "").split() or [""])[0].lower()
        if stage not in STAGES:
            warnings.append(f"{p.name}: stage {p.fields.get('stage', '')!r} is not one of {', '.join(STAGES)}")
        for t in p.tasks:
            if not t.title:
                warnings.append(f"{p.name}: task {t.id} has no title")
            if t.owner == "unassigned":
                warnings.append(f"{p.name}: task {t.id} has no owner")
    return warnings


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------

def board_path(swarm_root: Path) -> Path:
    return swarm_root / BOARD_FILE


def page_dir(swarm_root: Path) -> Path:
    return swarm_root / BOARD_DIR


def template_text() -> str:
    """The starter PRODUCTS.md that ``swarm board init`` writes."""
    return (TEMPLATE_DIR / "PRODUCTS.md").read_text(encoding="utf-8")


def init_board(swarm_root: Path, *, force: bool = False) -> Path:
    """Write the starter PRODUCTS.md. Refuse to replace a board unless forced."""
    target = board_path(swarm_root)
    if target.exists() and not force:
        raise FileExistsError(target)
    today = datetime.now(timezone.utc).date().isoformat()
    target.write_text(template_text().replace("{{TODAY}}", today), encoding="utf-8")
    return target


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class SyncResult:
    data_file: Path
    board: Board | None
    error: str = ""
    warnings: list[str] = field(default_factory=list)


def sync_board(swarm_root: Path) -> SyncResult:
    """Copy the static page into .swarm/board/ and write board-data.js.

    board-data.js is written even when the board does not parse: the page
    then shows the error and keeps the last good board on screen.
    """
    source = board_path(swarm_root)
    md = source.read_text(encoding="utf-8")  # FileNotFoundError goes to the caller
    out = page_dir(swarm_root)
    out.mkdir(parents=True, exist_ok=True)

    for name in STATIC_FILES:
        text = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
        dest = out / name
        if not dest.exists() or dest.read_text(encoding="utf-8") != text:
            _write_atomic(dest, text)

    board: Board | None = None
    error = ""
    warnings: list[str] = []
    try:
        board = parse_board(md)
        warnings = check_board(board)
    except BoardError as exc:
        error = str(exc)

    data = {
        "md": md,
        "synced": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source.name,
    }
    data_file = out / DATA_FILE
    _write_atomic(
        data_file,
        f"// Written by `swarm board sync`. Do not edit this file. Edit {source.name}.\n"
        f"window.BOARD = {json.dumps(data)};\n",
    )
    return SyncResult(data_file=data_file, board=board, error=error, warnings=warnings)


def _stamp(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size)


def watch_board(
    swarm_root: Path,
    on_sync: Callable[[SyncResult], None],
    *,
    interval: float = 1.0,
    cycles: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Sync again each time PRODUCTS.md changes. Polls; needs no extra package.

    A poll of the file stamp also follows an editor that saves by replacing
    the file. ``cycles`` stops the loop after that many polls (for tests).
    """
    source = board_path(swarm_root)
    last = _stamp(source)
    n = 0
    while cycles is None or n < cycles:
        sleep(interval)
        n += 1
        now = _stamp(source)
        if now is not None and now != last:
            last = now
            on_sync(sync_board(swarm_root))
