# Product board

Board updated: {{TODAY}} by the operator (starter board)
Board editor: claude-code
Operator: the operator

This file shows each product, where it stands, and the next step. It is the top
layer above the `queue.md` files (work items). `swarm board sync` writes a static
page to `.swarm/board/index.html`. Open that page from a file:// URL. The page
checks this file every 30 seconds. After an edit, run `swarm board sync`, or keep
`swarm board sync --watch` running. The format is in the dot_swarm document
`docs/PRODUCT_BOARD.md`.

## Rules for every agent

1. Read this file at session start, after `BOOTSTRAP.md`. Work on a P1 product before a P2 product.
2. Claim work in the `queue.md` file that holds the item ID, not in this file.
   The claim protocol in `BOOTSTRAP.md` does not change.
3. A task has one owner tag:
   - `operator`: the human who sets direction. Money, legal, contracts, outreach,
     approvals, physical hardware and product choices go to the operator.
   - `claude-code`: Claude Code sessions on the operator's machine.
   - `fleet:<name>`: one long-running bot. The bot starts a task only under an
     active directive from the operator that covers the task.
   - `harness`: any other coding agent session. Use it for self-contained code work.
4. Only the operator and the board editor (the `Board editor:` line above) edit this
   file. Any other agent writes a change request in its own state file or handoff
   note. The board editor then updates this file.
5. An item ID of `new` means that no queue item exists yet. The agent that starts the
   work creates the queue item first.

## Tiers

- Product tier: `P1` launch focus, `P2` keep moving, `P3` parked.
- Decision tier for the operator, by capacity:
  - `T1`: do in a low-capacity week. Each one blocks a launch step or has a date.
  - `T2`: do when there is more time. Each one is needed in the next few weeks.
  - `T3`: do on a high-capacity day. Nothing waits on it yet.

## Format (`swarm board sync` and the page read this format)

Each product is a `## ` heading with `- key: value` lines (tier, stage, epic, repos,
goal, now, next, updated). Then a `Tasks:` list with
`- [state] ID | owner: X | status: Y | title`, and a `Decisions:` list with
`- T1 | decision | context | ref: path`. The task states are `[ ]` open,
`[>]` claimed, `[/]` in progress, `[!]` blocked, `[x]` done.
The stage is one of: idea, design, building, beta, live.

<!-- board:start -->

## Example notes app
- tier: P1
- stage: building
- epic: EPIC-NOTES
- repos: notes-web, notes-api
- goal: A small team can write, tag and search shared notes in the browser.
- now: The editor and the API for notes are merged. Search is not started.
- next: Add full-text search, then open a private beta for five teams.
- updated: {{TODAY}}

Tasks:
- [x] NOTES-001 | owner: claude-code | status: merged | Notes API: create, read, update, delete
- [/] NOTES-004 | owner: harness | status: draft PR | Full-text search on the note body
- [ ] new | owner: fleet:builder | status: needs a directive | Nightly export of all notes to JSON
- [!] NOTES-007 | owner: operator | status: waits on a decision | Choose the hosting region for the beta

Decisions:
- T1 | Choose the hosting region for the beta | Blocks the beta date | ref: notes-api/.swarm/queue.md
- T2 | Free plan limit: 100 notes or 500 notes | Needed before the public launch | ref: notes-web/.swarm/context.md

## Example command-line tool
- tier: P3
- stage: idea
- goal: A command-line client for the notes app.
- now: Parked until the notes app beta is live.
- next: Write a one-page design.
- updated: {{TODAY}}

<!-- board:end -->
