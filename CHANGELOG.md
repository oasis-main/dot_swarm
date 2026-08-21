# Changelog

All notable changes to dot_swarm are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed — `swarm gui` never worked (three defects, one endpoint)

The dashboard shipped in v0.x and had zero test coverage, so nothing caught
that its only data endpoint returned a 500 to every user on every platform.

- `cli.py`'s gui handler referenced a bare `json`, but `cli.py` has no
  module-level `import json` — every other command in the file does a local
  `import json as _json`. So `GET /api/state.json` raised
  `NameError: name 'json' is not defined` on the first request, always.
- The failure was invisible because the error was raised *after*
  `end_headers()`. `send_error()` writes a complete HTTP response of its
  own, so the client received a 200 whose body began `HTTP/1.0 500 ...`.
  The payload is now serialized before any header is written, so a failure
  produces one clean 500.
- `operations.get_colony_summary()` called `_fmt_ts`, which is defined in
  `models.py` and was never imported into `operations.py`. Any division
  holding a `claimed_at` or `done_at` timestamp — i.e. every division with
  real history — raised `NameError` and was rendered as an error card. On
  the oasis-x colony that was 4 of 29 divisions, including the org root and
  the two largest queues.
- `discover_divisions()` treated a git worktree's `.swarm/` (e.g. under
  `.claude/worktrees/<name>/`) as a peer division of the repo it was cut
  from, and would do the same for a vendored tree. New `is_division_copy()`
  skips any division reached through a dot-directory or a vendored
  directory name; `explore`, `descend`, `report` and the GUI all inherit it.
- `swarm gui --open` passed `encoding='utf-8'` to `webbrowser.open()`,
  which takes no such argument — collateral from the v2.0.0 Windows
  encoding sweep, which rewrote a call that is not file I/O. The browser
  never opened; a traceback printed from the thread.

### Changed — the dashboard is self-contained and can write

- **No external subresources.** The template pulled React, ReactDOM,
  Babel-standalone and the Tailwind JIT from two CDNs, plus a Google
  webfont, and transpiled JSX in the browser on every load. Offline that
  rendered a blank page. Rewritten as plain DOM calls and hand-written CSS
  in one 25 KB file: no CDN, no webfont, no build step, no vendored blobs.
  `tests/test_gui_template.py` fails the build if a CDN reference returns.
- **Write routes.** `POST /api/item/{add,claim,done,block,comment}` call
  the same `operations.py` functions as the CLI, so a dashboard write
  produces the same `queue.md` entry and the same append-only
  `.swarm/claims/` record as the equivalent `swarm` command. This is the
  first surface where a human can drive the protocol without the terminal.
- **Bounded.** The server now binds `127.0.0.1` instead of every interface;
  writes require a per-run token embedded in the page (`X-Swarm-Token`) and
  are refused from a foreign `Origin`; a `division_path` outside the
  discovered colony is refused; bodies are capped at 64 KB. The handler
  moved from `SimpleHTTPRequestHandler` to `BaseHTTPRequestHandler`, which
  removes the fall-through that served the process's working directory for
  any unmatched path. `--read-only` serves the page with no token at all.
- New `--read-only` and `--agent` options on `swarm gui`.

### Added — tests for the GUI surface (308 → 334)

- `tests/test_colony_summary.py` (7): the `_fmt_ts` regression, JSON
  serializability of the payload the GUI serves, per-division error
  isolation, and division-copy exclusion.
- `tests/test_gui_template.py` (6): no external subresources, no known CDN
  host, no JSX left behind.
- `tests/test_gui_server.py` (13): a real `swarm gui` process driven over
  real HTTP — add/claim/done round trip, the write landing in the claim
  trail, and every refusal above.

## [2.0.0] — 2026-07-17

The agent-identity release. Fixes a genuinely broken MCP server, replaces
the swarm-wide HMAC signing key with a real per-agent primitive, and adds
the two coordination surfaces (comments, mailbox) that primitive was
built to secure. No breaking changes to the `.swarm/` directory layout —
every addition here is additive, and a `.swarm/` with none of the new
`agents/`/`comments/`/`mailbox/` directories keeps working exactly as
before. Bumped to 2.0.0 (rather than a strict-SemVer 1.1.0) as a
deliberate signal: this is the release where dot_swarm's coordination
model becomes safe to run with agents that don't all trust each other.

### Added — hash-style IDs, graph edges, JSON output (worktree-friendly queue)
- Borrows three ideas from [beads (bd)](https://github.com/steveyegge/beads)
  without taking on its Dolt-backed storage — the markdown queue +
  append-only claims trail stays the source of truth.
- `swarm add --hash-id` generates beads-style `sw-XXXX` IDs that don't
  collide when two worktrees add items in parallel before merging.
  `ITEM_RE` widened to accept both `DIVISION-NNN` and the hash form.
- New `supersedes:`/`duplicates:` edge fields on work items. `swarm
  ready` now skips items that are duplicates of, or superseded by,
  another item. CLI: `swarm add --supersedes ID,...` / `--duplicates
  ID,...`.
- `--json` on `swarm ls` and `swarm status` (matching the existing
  `swarm ready --json`), all three now surfacing the new edge fields
  so agent scripts can consume the queue without regex-parsing markdown.

### Fixed — Windows file-encoding and clock-resolution gaps
- `operations.py::_atomic_write()` wrote via `os.fdopen(fd, "w")` with
  no explicit encoding — missed by an earlier encoding sweep that only
  covered `Path.read_text()`/`write_text()`/`open()` calls. This was
  the actual cause of Windows CI failures on any queue.md containing
  an em dash: every write went through the process locale (cp1252)
  while every read had already been fixed to utf-8 — a write/read
  mismatch, not a read-only bug. Also closed 9 sites in `identity.py`/
  `mailbox.py` (written before the Windows-encoding convention existed)
  and 2 in test fixtures, found via an AST-based sweep rather than grep
  so multi-line calls couldn't hide a missing `encoding=`.
- `mailbox.py`'s inbox filename ordering assumed microsecond-precision
  timestamps were enough to distinguish two sends — not true on
  platforms with coarser clock resolution (observed on Windows CI: two
  back-to-back `send_message()` calls landed on the identical tick).
  Added a monotonic per-process counter as the real ordering guarantee.
- The SWC-050 claim lock's cleanup could hit `PermissionError` on
  Windows under heavy concurrent create/delete of the same lock
  filename — POSIX allows that unconditionally, Windows doesn't.
  Added a bounded retry; worst case falls back to the lock's existing
  30s stale-reclaim, not a new failure mode.

### Fixed — MCP server was broken end to end (SWC-047)
- `dot_swarm_mcp/server.py` imported a function (`heal`) that didn't
  exist in `ai_ops.py` — every `swarm_heal` MCP call raised `ImportError`.
  `test_mcp.py`'s own except-clause silently reported "SDK not installed"
  instead of failing, masking the bug from the test suite entirely.
- `heal` extracted into a pure `ai_ops.heal()` shared by both the CLI and
  the MCP server; `swarm_handoff` (implemented but never listed) added to
  `list_tools()`; `test_mcp.py` now fails loudly on a real break instead
  of skipping.

### Added — per-agent Ed25519 identity (SWC-048)
- New `dot_swarm.identity`: each agent gets its own Ed25519 keypair
  instead of every writer sharing one HMAC key (`signing.py`) that lets
  any holder forge any agent's signature. Private key never lives in
  `.swarm/` (default `~/.dot_swarm/keys/<agent_id>.key`, override with
  `DOT_SWARM_AGENT_KEY_DIR`); public key publishes to the git-tracked
  `.swarm/agents/<agent_id>.json` registry, which refuses to silently
  overwrite a different key for an already-registered agent.
- New CLI: `swarm agent init/list/show`.
- Fixed a real `.gitignore` bug found while dogfooding this: a blanket
  `.swarm/` ignore rule silently defeats *any* negation for a subpath
  (git can't re-include inside an excluded parent) — changed to
  `.swarm/*` so `.swarm/agents/` can actually be un-ignored.

### Added — MCP server authentication (SWC-049)
- `call_tool()` used to trust whatever `agent_id`/`inspector_id` string
  the *caller* passed per call, with zero verification. Since MCP here
  is stdio (one server process per agent), identity is now bound ONCE
  at startup via `DOT_SWARM_AGENT_ID` — once bound, it always overrides
  a caller-supplied value. Unset env var = unchanged pre-2.0 behavior.
- When the bound agent also has a local Ed25519 key, every write is
  additionally signed and recorded in `trail.log` via a new, optional
  `agent_signature` field — additive, existing readers unaffected.

### Fixed — atomic `claim()` (SWC-050)
- `claim_item()`'s read-decide-write sequence had no lock: concurrent
  claimants could all observe `OPEN` and all be told they'd won an
  uncontested claim. New per-item advisory lock
  (`.swarm/claims/.lock-<item-id>`, `os.open(O_CREAT|O_EXCL)`) closes
  the window, with stale-lock reclaim (30s) and a timeout (5s default).
  `resolve_claims()`/`COMPETING` stays in place as defense in depth.

### Added — signed comment threads (SWC-051)
- New `dot_swarm.comments`: `.swarm/comments/<item-id>.jsonl`,
  Ed25519-signed, content-addressed `comment_id` (no sequence-number
  race between concurrent commenters). Out-of-order replies are
  recorded, not rejected.
- New CLI: `swarm comment <id> <body> [--reply-to] [--agent]`,
  `swarm comments <id> [--verify]`.
- `comments/` is git-tracked (durable shared discussion, like
  `queue.md`/`agents/`) — unlike the mailbox below.

### Added — agent-to-agent mailbox (SWC-052)
- New `dot_swarm.mailbox`: direct, Ed25519-signed messaging between
  agents sharing one `.swarm/` — the mechanism that makes "delegate to
  a peer with different network access" an actual capability instead
  of persona description. Distinct from `federation.py` (cross-swarm/
  cross-repo, HMAC, git-transported); this is within one swarm, over
  the shared mailbox volume.
- `.swarm/mailbox/<agent_id>/{inbox,read}/` — one file per message
  (not JSONL) so marking a message read is a single rename.
- New CLI: `swarm mail send/inbox/read`.
- `mailbox/` stays gitignored — ephemeral, consumed-then-gone traffic,
  unlike `comments/` above.

## [1.0.1] — 2026-05-04

### Fixed
- Replaced all 20 `datetime.utcnow()` calls (deprecated in Python 3.12,
  scheduled for removal in 3.14) with a new `dot_swarm.models.utcnow()`
  helper that returns the same naive UTC datetime via
  `datetime.now(timezone.utc).replace(tzinfo=None)`. Test suite now
  passes cleanly under `-W error::DeprecationWarning`.

## [1.0.0] — 2026-05-02

The first stable release. The protocol is frozen for the v1.x line:
existing `.swarm/` directories created by 1.0 will be readable by every
1.x release, and `swarm migrate` brings any 0.3.x directory up to the
v1.0 layout idempotently.

### Added — append-only claim trail (SWC-033)
- `.swarm/claims/` is now a true immutable log. Every `claim`, `partial`,
  `done`, `block`, `reopen`, and `compete` operation appends one
  JSON record; release transitions write a superseding record rather
  than deleting prior ones.
- New resolver in `operations.resolve_claims`: newest-per-(item, agent),
  with terminal records winning over equally-recent active ones, and
  multiple concurrent active agents aggregating into `COMPETING`.
- New CLI: `swarm trail claims [--item ID]` to view full history.

### Added — competing claimants (SWC-039)
- `claim --compete` now records each rival as an independent claim
  record rather than overwriting the prior claimant.
- New helpers `competitors_for(paths, item_id)` and
  `promote_competitor(paths, item_id, winner, reason)` — the latter
  writes a fresh `CLAIMED` record for the winner and an
  `OPEN`/`lost-competition` record for each loser.
- New CLI: `swarm compete list <id>` and
  `swarm compete winner <id> <agent> --reason ...`.

### Added — hidden-in-plain-sight stigmergic seals
- New module `dot_swarm.seals`. A *seal* is an HMAC tag embedded inline
  as `<!-- 🐝 sw-seal v2 <agent>:<hex16> -->` — looks like ordinary
  markdown to outsiders, authenticates the writer to anyone with the
  swarm signing key, and detects content tampering on read.
- Status enum: `VALID` / `INVALID` / `MISSING` / `UNKEYED`. Foreign-swarm
  seals read as `INVALID` — the digital analogue of a wrong cuticular
  signature.
- v2 (16-hex / 64-bit tag) is the default; v1 (8-hex / 32-bit) seals
  remain accepted for backward compatibility.
- New CLI: `swarm seal sign <file>`, `swarm seal verify`,
  `swarm seal check <file>`.

### Added — collaborative-but-untrusted message bay
- `federation/strangers/` holds inbound messages from peers without a
  matching `trusted_peers/` record (or whose intent is disabled by
  policy). Each quarantined message is paired with a
  `<file>.json.reason.txt` recording the doorman verdict.
- `apply_inbox_message` now quarantines on doorman block by default;
  `triage_inbox` sweeps the inbox in bulk.
- `promote_stranger` trusts the peer with explicit scopes and replays
  the message into `inbox/`. `reject_stranger` archives without trust
  into `strangers/rejected/`.
- New CLI: `swarm federation triage`,
  `swarm federation strangers list/show/promote/reject`.

### Added — swarm key (SWC-046, Phase 1)
- New module `dot_swarm.vault`. `swarm key init` generates a per-swarm
  ChaCha20-Poly1305 key (`.swarm/.swarm_key`, 256-bit, gitignored).
- When the key is present, every `trail.log` entry is written as a
  one-line `swae1:<base64>` envelope (nonce + ciphertext + 128-bit AEAD
  tag) — opaque on disk, transparently decrypted by `read_trail` and
  the CLI.
- `swarm key rotate` generates a new key, retains the prior one as
  `.swarm_key.old`, and re-seals every existing envelope under the new
  key. Mid-rotation reads fall back to the old key.
- `swarm key seal <file>` / `swarm key open <file>` apply the same
  envelope format to any individual coordination file.
- New optional extra: `pip install 'dot-swarm[crypto]'` (pulls
  `cryptography>=42`).
- The CHaCha20-Poly1305 layer composes with the existing HMAC-signed
  trail and the new seals — the medium is shared, but the *language*
  of the medium is not.

### Added — `swarm migrate`
- Brings any 0.3.x `.swarm/` directory up to the v1.0 layout
  idempotently. Creates missing `claims/`, `federation/strangers/`,
  `federation/strangers/rejected/`, ensures `.gitignore` lists the new
  key files, and backfills synthetic claim records for items still
  marked CLAIMED in `queue.md`.
- `--dry-run` previews changes without writing; `--all` runs against
  every `.swarm/` discovered under the current path.

### Changed
- `swarm init` now creates `.swarm/claims/` and includes
  `.swarm_key` / `.swarm_key.old` in the auto-generated `.gitignore`.
- `apply_inbox_message` returns an additional `quarantined` boolean and
  no longer drops untrusted messages silently.
- Default seal format is now v2 (16-hex tag). Existing v1 seals continue
  to verify.

### Tests
- 240+ tests across the suite; full pass on Python 3.11 and 3.12.

## [0.3.x] and earlier

See git history. 0.3.x was the pre-stable iteration line; protocol
shape changed across point releases. Use `swarm migrate` to upgrade.
