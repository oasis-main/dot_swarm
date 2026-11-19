---
name: product-board
description: Read and respect the dot_swarm product board (.swarm/PRODUCTS.md) at the start of a session in a repository that has one. Use it to pick the next task by product tier, to claim work in queue.md, and to file a board change request. Only the operator and the board editor change the board.
---

# Product board

A repository or organization that uses dot_swarm can keep a product board in
`.swarm/PRODUCTS.md`. The board is the top layer above the `queue.md` files. It
lists each product, its tier and stage, its next step, its tasks with one owner
each, and the decisions that wait on the operator (the human who sets direction).
The format is in the dot_swarm document `docs/PRODUCT_BOARD.md`.

## At session start

1. Read `.swarm/BOOTSTRAP.md` first, as usual.
2. Find the board. Look for `.swarm/PRODUCTS.md` in this directory, then in each
   parent directory up to the organization root. If there is no board, stop here.
3. Read the header lines:
   - `Board editor:` names the one agent tag that may edit the board with the operator.
   - `Operator:` is the display name of the operator.
4. Read the products between `<!-- board:start -->` and `<!-- board:end -->`.
   Note each product tier: `P1` launch focus, `P2` keep moving, `P3` parked.

## Choosing work

1. Work on a P1 product before a P2 product. Do not start work on a P3 product
   unless the operator asks for it.
2. Take only a task whose owner tag is yours:
   - `operator`: the operator does this task. Do not do it. You may prepare
     material for it when the operator asks.
   - `claude-code`: a Claude Code session on the operator's machine.
   - `fleet:<name>`: the named long-running bot. That bot starts the task only
     under an active directive from the operator that covers the task.
   - `harness`: any other coding agent session, for self-contained code work.
3. Do not take a task that waits on an open decision for the operator.

## Claiming work

1. Claim the task in the `queue.md` file that holds its item ID, not in
   `PRODUCTS.md`. Use `swarm claim <ID>` or the claim protocol in `BOOTSTRAP.md`.
2. If the item ID is `new`, no queue item exists yet. Create the queue item first
   with `swarm add`, then claim the new ID.
3. Record progress in the queue item and in `state.md`, as usual.

## Changing the board

1. Only the operator and the board editor edit `PRODUCTS.md`.
2. If you are the board editor:
   - Keep the format exactly. The page and `swarm board sync` parse it.
   - Update the `Board updated:` line with the date and your tag.
   - Update the `updated:` field of each product that you change.
   - Run `swarm board sync`. Fix every error and warning that it prints.
3. If you are not the board editor, do not edit `PRODUCTS.md`. Write a change
   request in your own `state.md` or in your handoff note. Give:
   - the product name,
   - the line to add, change or remove, in the board format,
   - the reason, with a reference (a queue item ID, a PR, a file path).
4. Put a question for the operator on the board as a `Decisions:` line with a
   tier (`T1`, `T2` or `T3`). Do not put the question only in a chat message.

## Do not

- Do not run a server to show the board. The page in `.swarm/board/index.html`
  works from a file:// URL.
- Do not edit the files in `.swarm/board/`. `swarm board sync` writes them again.
