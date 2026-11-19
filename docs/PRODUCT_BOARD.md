---
title: Product Board
nav_order: 8
---

# Product Board

The product board is one markdown file, `.swarm/PRODUCTS.md`, above the
`queue.md` files. The board shows each product, where the product stands, the
next step, the open tasks with one owner each, and the decisions that wait on
the operator (the human who sets direction for the agents).

The queues hold the work items. The board holds the view across products: which
product comes first, who owns each task, and which decision blocks which step.
Several kinds of agent can read one board: Claude Code sessions, long-running
bots, and other coding harnesses.

```bash
swarm board init            # write a starter .swarm/PRODUCTS.md
swarm board sync            # write .swarm/board/index.html and board-data.js
swarm board sync --watch    # write them again each time PRODUCTS.md changes
```

Open `.swarm/board/index.html` in a browser. `swarm board sync` prints the
file:// URL. The page loads `board-data.js` again every 30 seconds and draws the
board again when the text changed.

---

## `swarm board` and `swarm gui`

| | `swarm board` | `swarm gui` |
|---|---|---|
| Shows | `PRODUCTS.md`: products, owners, decisions | Every `queue.md`, `state.md` and the commit trail |
| Writes | Nothing. The page is read-only. | Add, claim, done, block, comment |
| Runs | No process (or `sync --watch`, which only writes files) | An HTTP server on `127.0.0.1` |
| Access control | File permissions | A per-run token on every API route |
| Opens from | A `file://` URL | `http://127.0.0.1:PORT/#t=TOKEN` |

`swarm gui` needs a per-run token because the loopback address is not a trust
boundary: every local process, and a Docker container through
`host.docker.internal`, can reach a loopback port. `swarm board` opens no port.
The page is plain files that a browser reads from disk, so a local process can
read the board only if the process can already read `.swarm/`.

A browser does not let `fetch()` read a `file://` URL. A `<script>` tag can load
a `file://` URL, so `swarm board sync` puts the board text in a small script,
`board-data.js`, that sets `window.BOARD`.

---

## Files

| Path | Written by | Edit it? |
|---|---|---|
| `.swarm/PRODUCTS.md` | `swarm board init` (once), then people and agents | Yes: only the operator and the board editor |
| `.swarm/board/index.html`, `board.css`, `board.js` | `swarm board sync` | No. Each sync writes them again. |
| `.swarm/board/board-data.js` | `swarm board sync` | No. The file holds a copy of `PRODUCTS.md`. |

The files in `.swarm/board/` are generated. Add `.swarm/board/` to `.gitignore`
if the team does not want them in git.

`swarm board sync` writes `board-data.js` even when `PRODUCTS.md` does not parse.
The page then shows the error and keeps the last good board on screen. Without
`--watch`, the command exits with code 1 on a parse error. The command also
prints a warning for an unknown tier, an unknown stage, or a task with no owner.

Each write goes to a temporary file first, then a rename replaces the old file.
So the page never reads a half-written file.

---

## File format

Free text can go above the board. The parser reads three header lines anywhere
in the file:

```markdown
Board updated: 2026-01-05 by claude-code (short note on the change)
Board editor: claude-code
Operator: the operator
```

- `Board updated:` shows at the top of the page.
- `Board editor:` names the one agent tag that edits the board with the operator.
- `Operator:` is the display name in "Decisions for ...". The default is "the operator".

The products go between two markers. The parser ignores every line outside them.

```markdown
<!-- board:start -->

## Example notes app
- tier: P1
- stage: building
- epic: EPIC-NOTES
- repos: notes-web, notes-api
- goal: A small team can write, tag and search shared notes in the browser.
- now: The editor and the API for notes are merged. Search is not started.
- next: Add full-text search, then open a private beta for five teams.
- updated: 2026-01-05

Tasks:
- [/] NOTES-004 | owner: harness | status: draft PR | Full-text search on the note body
- [ ] new | owner: fleet:builder | status: needs a directive | Nightly export of all notes to JSON
- [!] NOTES-007 | owner: operator | status: waits on a decision | Choose the hosting region

Decisions:
- T1 | Choose the hosting region for the beta | Blocks the beta date | ref: notes-api/.swarm/queue.md

<!-- board:end -->
```

### Product

A product starts at a `## ` heading. The heading text is the product name.

Field lines have the form `- key: value`. The page uses these keys:

| Key | Value |
|---|---|
| `tier` | `P1` launch focus, `P2` keep moving, `P3` parked. The page does not show a product with another tier. |
| `stage` | `idea`, `design`, `building`, `beta` or `live`. The first word counts, so `beta (closed)` is `beta`. |
| `goal`, `now`, `next` | One line each. Text in backticks shows as code. |
| `epic`, `repos`, `updated` | Shown in small text under the goal. |

The parser keeps other lowercase keys, but the page does not show them.

### Tasks

After a `Tasks:` line, each task has the form:

```
- [state] ID | owner: TAG | status: TEXT | title
```

| State | Meaning |
|---|---|
| `[ ]` | open |
| `[>]` | claimed |
| `[/]` | in progress |
| `[!]` | blocked |
| `[x]` | done |

- The `ID` is the item ID in a `queue.md` file. The ID `new` means that no queue
  item exists yet.
- `owner:` and `status:` are optional and can come in any order after the ID. A
  task with no `owner:` is "not assigned", and `swarm board sync` warns.
- The other parts make the title. A title that contains ` | ` stays in one piece.

### Owner tags

An owner tag is `group` or `group:name`. The page groups open tasks by the part
before the colon. A suggested set of groups:

| Tag | Owner |
|---|---|
| `operator` | The operator: money, legal, contracts, outreach, approvals, physical hardware, product choices. |
| `claude-code` | Claude Code sessions on the operator's machine. |
| `fleet:<name>` | One long-running bot. The bot starts a task only under an active directive from the operator. |
| `harness` | Any other coding agent session, for self-contained code work. |

Any other tag works too. The page shows the operator first, then the board
editor's group, then the other groups by name.

### Decisions

After a `Decisions:` line, each decision has the form:

```
- T1 | decision | context | ref: path
```

The tier sorts decisions by the capacity that the operator has:

| Tier | Meaning | Shown at capacity |
|---|---|---|
| `T1` | Blocks a launch step or has a date. | low, medium, high |
| `T2` | Needed in the next few weeks. | medium, high |
| `T3` | Nothing waits on it yet. | high |

The page has a capacity switch: low, medium or high. The page keeps the
selection in `localStorage` for that browser.

---

## Rules for agents

The starter board from `swarm board init` puts these rules in the file. The
agent skill in `skills/product-board/SKILL.md` gives the same rules to a Claude
Code session.

1. Read `PRODUCTS.md` at session start, after `BOOTSTRAP.md`. Work on a P1
   product before a P2 product.
2. Claim work in the `queue.md` file that holds the item ID, not in
   `PRODUCTS.md`. The claim protocol does not change.
3. A task has one owner tag. Take only a task with your own tag.
4. Only the operator and the board editor edit `PRODUCTS.md`. Any other agent
   writes a change request in its own `state.md` or handoff note. The board
   editor then updates the board.
5. An item ID of `new` means that no queue item exists yet. The agent that
   starts the work creates the queue item first.

To install the skill for Claude Code, copy the folder into the skills folder of
the repository:

```bash
mkdir -p .claude/skills && cp -R path/to/dot_swarm/skills/product-board .claude/skills/
```

---

## Parser

There are two copies of the parser:

- `dot_swarm/board.py` (`parse_board`) for `swarm board sync` and for scripts.
- `templates/board/board.js` (`parseBoard`) for the page. Node can also load
  this file with `require()`.

`tests/test_board.py` parses one sample with both copies and compares the
results when Node is installed. Change both copies together.
