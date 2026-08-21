---
title: CLI Reference
nav_order: 2
---

# CLI Reference

`swarm` is installed as a standalone command via `pip install dot-swarm`.

```bash
pip install dot-swarm           # base CLI
pip install 'dot-swarm[ai]'     # + AWS Bedrock support (boto3)
```

---

## Global Options

| Flag | Default | Description |
|------|---------|-------------|
| `--path PATH` | `.` (cwd) | Root path to search for `.swarm/` directory |
| `--version` | — | Print version and exit |
| `--help` | — | Show help |

All commands inherit `--path`. Example: `swarm --path ../api-service status`

---

## Initialization

### `swarm init`

Initialize a `.swarm/` directory in the current repo.

```bash
swarm init                   # auto-detect org vs division level
swarm init --level org       # force org level (ORG- item IDs)
swarm init --level division  # force division level
swarm init --code CLD        # set division code (default: derived from folder name)
```

Creates: `BOOTSTRAP.md`, `context.md`, `state.md`, `queue.md`, `memory.md`

---

## Situational Awareness

### `swarm status`

Print current state and active/pending queue items for this division.

```bash
swarm status          # active + pending items only
swarm status --all    # include done items
```

### `swarm ls`

List queue items with filtering.

```bash
swarm ls                            # all items
swarm ls --section active           # active only
swarm ls --section pending          # pending only
swarm ls --priority high            # filter by priority
swarm ls --project cloud-stability  # filter by project tag
```

### `swarm explore`

Show the heartbeat of all divisions in the colony. Recursively discovers `.swarm/` directories.

```bash
swarm explore                       # from current directory, depth 2
swarm explore --depth 3             # search deeper
swarm --path ~/org explore          # from org root
```

### `swarm report`

Generate a full markdown report of all divisions. Unlike `explore`, outputs a complete
document suitable for sharing, filing as a GitHub issue, or posting to a wiki.

```bash
swarm report                        # print to stdout
swarm report --out REPORT.md        # write to file
swarm report --only active          # active items only
swarm report --no-done              # skip done sections
```

### `swarm ready`

List OPEN items with all dependencies satisfied — safe to pick up right now.
Equivalent to `bd ready` in the Gastown/Beads ecosystem.

```bash
swarm ready            # human-readable list
swarm ready --json     # machine-readable JSON array (for agent scripts)
```

Only items in the `Pending` section with state `OPEN` whose entire `depends:` chain
appears in the `Done` section are shown. Items with no dependencies are always listed.

### `swarm gui`

Serve the colony as a browsable, editable web page on `127.0.0.1`. Reads every
`.swarm/` division under `--path` and renders four views: a division overview, the
global priority queue sorted across every division, the combined commit trail, and a
per-division detail page.

```bash
swarm gui                          # http://127.0.0.1:8000
swarm gui --port 9000 --open       # different port, open the browser
swarm gui --read-only              # serve the page without write routes
swarm gui --agent alice            # attribute writes to a specific agent
swarm --path ~/org gui             # serve a colony you are not standing in
```

The page is **self-contained** — no CDN, no webfont, no build step. It renders with
no network access at all, which is the point: `swarm gui` is a local operator tool
that has to work on a plane, behind a proxy, and inside an egress-blocked container.

**Writing from the page.** Add, claim, complete, block and comment go through the same
`operations.py` calls the CLI uses. A write from the dashboard produces the same
`queue.md` entry and the same append-only record in `.swarm/claims/` as the equivalent
`swarm` command — there is no separate storage path and no translation layer, so a
human working in the browser and an agent working over MCP are editing one queue.

**What bounds a write:**

| Control | Behaviour |
|---|---|
| Bind address | `127.0.0.1` only — never published to other interfaces |
| Write token | Random per run, embedded in the page at serve time, required in the `X-Swarm-Token` header. A page from another site cannot read it, and setting a custom header forces a CORS preflight this server does not answer. |
| `Origin` check | A request carrying a foreign `Origin` is refused with 403 |
| Division allow-list | The `division_path` in a request must already be a discovered division. A path outside the colony is refused. |
| Body limit | 64 KB |
| Static files | Only `/`, `/api/state.json` and `/logo.png` are served. Every other path is a 404 — the handler never falls through to serving the working directory. |
| `--read-only` | No token is embedded, and every write route answers 403 |

Requests are handled one at a time, so the dashboard never mutates two items
concurrently. `claim` additionally takes the same per-item lock the CLI takes.

---

## Work Item Lifecycle

### `swarm add`

Add a new work item to the Pending queue.

```bash
swarm add "Add request ID tracing to all services"
swarm add "Fix Redis timeout" --priority high --project infra
swarm add "OAuth2 discovery" --notes "See RFC 8414 for discovery spec"
```

```bash
swarm add "Add request ID tracing to all services"
swarm add "Fix Redis timeout" --priority high --project infra
swarm add "OAuth2 discovery" --notes "See RFC 8414 for discovery spec"
swarm add "Run integration tests" --depends API-042,API-043
swarm add "Implement rate limiter" --max-retries 5  # override inspector retry limit for this task
```

Options: `--priority [low|medium|high|critical]`, `--project TEXT`, `--notes TEXT`,
`--depends ITEM-IDs` (comma-separated), `--max-retries N`

**`--max-retries N`** sets a task-level retry override for the inspector role. When
an inspector rejects this item `N` times, it is automatically BLOCKED (surfacing in
`swarm audit` and `swarm status`) rather than re-opened. Set to `0` (default) to
inherit the inspector role's `max_iterations` setting.

### `swarm claim`

Claim an item (move Active, stamp with agent ID + timestamp).

```bash
swarm claim API-042
swarm claim API-042 --agent my-agent-id
```

### `swarm done`

Mark a claimed item as done.

```bash
swarm done API-042
swarm done API-042 --note "Fixed by switching to sliding window counter"
swarm done API-042 --next "Pick up API-043 next"   # update state.md focus
```

{: .warning }
> **Inspector gate:** When the **inspector** role is enabled, `swarm done` is blocked
> unless the item has valid proof attached. Workers must use `swarm partial --proof`
> first, then an inspector agent runs `swarm inspect --pass`. Use `--force` to bypass
> as a human director.

```bash
swarm done API-042 --force    # human director override
```

### `swarm partial`

Checkpoint progress on a claimed item without marking it done. Updates the item's
in-progress note and refreshes the claim timestamp.

```bash
swarm partial API-042 --note "Counter logic done, eviction policy next"
```

**With proof (required when inspector role is enabled):**

```bash
swarm partial API-042 --proof "branch:feature/rate-limiter commit:abc1234 tests:87/87"
```

The `--proof` value is a space-separated list of `key:value` pairs. Required fields
are validated against the inspector role config (`branch` and `commit` by default).
A warning is printed if required fields are missing — the inspector will reject the
item unless they are present.

See [Agent Roles → Inspector](ROLES.md#inspector) for the full proof workflow.

### `swarm block`

Mark a claimed item as blocked.

```bash
swarm block API-042 "Waiting for staging environment credentials from ops"
```

### `swarm unblock`

Clear a blocked item back to Open (or back to Claimed if an agent is specified).

```bash
swarm unblock API-042                  # → OPEN
swarm unblock API-042 --reclaim        # → re-CLAIMED by current agent
```

---

## Memory & Audit

### `swarm audit`

Check for drift: stale claims, blocked items, pending items, security scan, and AI-powered code-vs-docs drift check.

```bash
swarm audit                  # Basic: stale claims + blocked items (always shown)
swarm audit --pending        # Also list all pending items
swarm audit --security       # Add adversarial content scan of .swarm/ files
swarm audit --drift          # Add AI code-vs-docs drift check (requires LLM backend)
swarm audit --trail          # Verify pheromone trail HMAC signatures
swarm audit --full           # All of the above
swarm audit --since 24       # Stale threshold in hours (default: 48)
```

**`--security`** scans `.swarm/` markdown files and platform shims (CLAUDE.md, .windsurfrules, .cursorrules) for:
- `CRITICAL`: Prompt injection, instruction erasure, persona hijacking, jailbreaks, LLM template injection
- `HIGH`: Non-disclosure directives, hidden instructions, control characters, priority manipulation
- `MEDIUM`: Hidden HTML/markdown comments, HTML injection, code injection

**`--drift`** runs the same AI analysis as the GitHub Actions drift-check workflow, locally. Compares the last 5 commits against `.swarm/` state to detect misalignment.

**`--trail`** re-verifies every HMAC-SHA256 signature in `trail.log`. Tampered entries are flagged with the agent fingerprint responsible.

### `swarm heal`

Full health pass: alignment + security scan + trail verification. Runs everything in sequence and logs all security findings to `memory.md` (findings are never silently swallowed).

```bash
swarm heal                   # Read-only health check
swarm heal --fix             # Quarantine adversarial content + block tampered trail signers
swarm heal --depth 2         # Descend two levels into child divisions (default: 1)
```

**Sections run by `swarm heal`:**
1. **Alignment** — ascend (local ↔ parent) + descend (local ↔ children). Identifies orphaned items with no cross-division links.
2. **Queue Health** — stale claims, blocked items, pending item count.
3. **Security Scan** — same 18-pattern scan as `swarm audit --security`, covering all `.swarm/` files and platform shims.
4. **Pheromone Trail Integrity** — HMAC-SHA256 re-verification of `trail.log`.
5. **Summary** — total issues, memory.md log entry, remediation hint if `--fix` not used.

**With `--fix`:**
- Copies flagged files to `.swarm/quarantine/<timestamp>_<file>.bak` for human forensic review. Does **not** auto-delete content — humans must excise injections.
- Blocks HMAC fingerprints of tampered trail entries in `.swarm/blocked_peers.json`.
- Run `swarm heal` again after cleaning to confirm resolution.

### `swarm handoff`

Print a structured handoff note for the current session — what was done, what's in
flight, what's next. Useful at the end of a work session.

```bash
swarm handoff
swarm handoff --format json    # machine-readable output
```

---

## Federation

OGP-lite cross-swarm federation — exchange work items and alignment signals between separate `.swarm/` hierarchies using signed intent messages. Trust is bilateral and explicit; there is no central registry.

**Key design principles (from OGP build learnings):**
- Identity is always derived from a stored key fingerprint, never from a path or claimed header field.
- Peer records are persisted *before* the trust operation returns (avoids the "addPeer lost on restart" bug).
- Every inbound message passes through a three-layer doorman before any queue operation is executed.

### `swarm federation init`

Create the `federation/` directory structure inside `.swarm/`.

```bash
swarm federation init
```

Creates: `federation/trusted_peers/`, `federation/inbox/`, `federation/outbox/`, `federation/policy.md`, `federation/exports.md`.

### `swarm federation export-id`

Print this swarm's public identity — the file to share with federation peers out-of-band (email, Slack, git).

```bash
swarm federation export-id              # print to stdout
swarm federation export-id --out id.json  # write to file
```

This is safe to commit or share. The private `.signing_key` is never exposed.

### `swarm federation trust`

Import a peer's identity file and establish bilateral trust.

```bash
swarm federation trust peer_identity.json
swarm federation trust peer_identity.json --name "Acme Corp" --scopes "work_request,alignment_signal"
```

| Option | Default | Description |
|--------|---------|-------------|
| `--name NAME` | peer's swarm ID | Human-readable label |
| `--scopes SCOPES` | `work_request,alignment_signal` | Comma-separated list of permitted intents |

### `swarm federation revoke`

Remove a peer from trusted peers.

```bash
swarm federation revoke <fingerprint>
```

### `swarm federation peers`

List all trusted federation peers and their permitted scopes.

```bash
swarm federation peers
```

### `swarm federation send`

Create a signed outbound intent message in `outbox/`. Deliver the resulting file to the peer manually (git push, shared directory, email attachment).

```bash
swarm federation send <fingerprint> work_request --desc "Need help with OAuth2 token exchange"
swarm federation send <fingerprint> alignment_signal --context "Completed auth module"
swarm federation send <fingerprint> capability_ad --context "Available for API integration work"
```

**Intent types:**

| Intent | Effect on peer | Notes |
|--------|---------------|-------|
| `work_request` | Adds item to peer's queue | Requires `work_request` scope |
| `alignment_signal` | Informational only | No queue change at peer |
| `capability_ad` | Informational only | Advertise what this swarm can do |

### `swarm federation inbox`

List received messages waiting in `inbox/`.

```bash
swarm federation inbox
```

### `swarm federation apply`

Apply a received inbox message to this swarm's queue after doorman enforcement.

```bash
swarm federation apply inbox/20260406T1400Z_work_request_ab123456.json
swarm federation apply inbox/msg.json --yes   # skip confirmation prompt
```

**Doorman enforcement sequence** (Layer 1 → 2 → 3):
1. Parse the message and read `from_fingerprint` from the message body (never the claimed header)
2. Layer 2: Is this fingerprint in `trusted_peers/`? Does their record permit this intent?
3. Layer 1: Does `federation/policy.md` allow this intent globally?

A `403`-equivalent reason is printed for any failure at any layer.

### Federation Policy

`federation/policy.md` is Layer 1 of the scope model. To disable an intent globally:

```markdown
disabled: work_request
```

Per-peer scopes in `trusted_peers/<fingerprint>.json` are Layer 2. Both must pass for a message to be applied.

### Upgrade Path

The current implementation exchanges identity files out-of-band and signs messages with HMAC-SHA256 for local trail integrity. The natural upgrade path to full OGP:

| Now (OGP-lite) | Full OGP |
|----------------|----------|
| Identity via `swarm federation export-id` (manual) | Ed25519 public key, discoverable via DNS `_ogp.example.com` TXT record |
| Transport: git push / shared dir / manual | HTTP/gRPC OGP gateway |
| HMAC-SHA256 local trail signing | Ed25519 asymmetric signatures (peers can verify independently) |
| `trusted_peers/` files | Bilateral gateway trust handshake |

---

## Scheduling

Schedules are stored in `.swarm/schedules.md`. No daemon required — intended to be called from the system crontab or triggered manually.

### `swarm schedule list`

```bash
swarm schedule list
```

### `swarm schedule add`

```bash
swarm schedule add '0 */6 * * *' 'swarm heal --fix'             # every 6 hours
swarm schedule add '6h' 'swarm audit --security' --name 'Security check'
swarm schedule add 'on:done API-042' 'swarm ai "claim API-043"'  # event-driven
```

**Schedule types:**

| Type | Spec format | Example |
|------|------------|---------|
| `cron` | 5-field cron | `0 9 * * 1` (Mondays 9am) |
| `interval` | `Nm` / `Nh` / `Nd` | `30m`, `6h`, `2d` |
| `on:done` | `on:done ITEM-ID` | `on:done API-042` |
| `on:blocked` | `on:blocked ITEM-ID` | `on:blocked API-042` |

### `swarm schedule remove`

```bash
swarm schedule remove SCHED-001
```

### `swarm schedule run`

Manually trigger a specific schedule regardless of due status.

```bash
swarm schedule run SCHED-001
```

### `swarm schedule run-due`

Run all currently-due cron/interval schedules. Add to system crontab:

```bash
# In crontab (crontab -e):
* * * * *  cd /path/to/repo && swarm schedule run-due

# Or manually:
swarm schedule run-due
```

---

## Workflows

Workflows are markdown files in `.swarm/workflows/*.md` with a YAML frontmatter header. They define multi-step sequences of `swarm` commands or arbitrary shell commands.

**Patterns** (inspired by swarms.ai):
- **`sequential`** — steps run in order; halts on first failure
- **`concurrent`** — all steps run in parallel threads
- **`conditional`** — steps have `if:` guards based on previous step results
- **`mixture`** — concurrent with result aggregation (implement per-step `agent:` assignments)

### `swarm workflow create`

Scaffold a new workflow file.

```bash
swarm workflow create rate-limiter-rollout --pattern sequential --trigger "on:done API-041"
swarm workflow create weekly-report --pattern sequential --trigger "0 9 * * 1"
```

Edit the generated `.swarm/workflows/<name>.md`:

```markdown
---
trigger: on:done API-041
pattern: sequential
description: Rate limiter implementation sequence
---

## Steps

1. swarm claim API-042
   agent: bedrock
   timeout: 30

2. swarm claim API-043
   agent: claude
   depends: API-042
   timeout: 45
   if: step1.ok

3. swarm heal --fix
   agent: auto
   timeout: 5
```

### `swarm workflow list`

```bash
swarm workflow list
```

### `swarm workflow show`

```bash
swarm workflow show rate-limiter-rollout
```

### `swarm workflow run`

```bash
swarm workflow run rate-limiter-rollout              # confirm before running
swarm workflow run rate-limiter-rollout --dry-run    # show steps, no execution
swarm workflow run rate-limiter-rollout --yes        # skip confirmation
```

### `swarm workflow status`

Show last run result from `.swarm/workflow_runs.jsonl`.

```bash
swarm workflow status rate-limiter-rollout
```

---

## AI Interface

### `swarm ai`

Translate a natural-language instruction into `.swarm/` operations using an LLM backend.
Previews proposed changes before executing (unless `--yes`).

```bash
swarm ai "mark API-042 as done, merged the rate limiter PR"
swarm ai "what should I work on next?"
swarm ai "add three items for distributed tracing: design, implement, test"
swarm ai "write a memory entry: chose sliding window over token bucket for burst tolerance"
swarm ai "update focus to auth service hardening" --yes

# With a specific backend:
swarm ai "summarise the queue" --via claude
swarm ai "what needs doing?" --via gemini
swarm ai "mark done" --via bedrock      # explicit Bedrock (default)
```

Options: `--yes / -y`, `--agent TEXT`, `--limit INT` (context token budget), `--via [bedrock|claude|gemini|opencode]`, `--chain`, `--max-steps INT`

**Workflow chaining (`--chain`)**:

With `--chain`, the AI is re-invoked after each successful set of write operations using the refreshed `.swarm/` context. This continues until the AI returns no further write ops (work is complete) or `--max-steps` is reached.

Each batch of chained operations is signed and recorded in `trail.log`.

```bash
swarm ai "run the rate limiter rollout: design, implement, test, deploy" --chain --yes
swarm ai "implement auth hardening, then tracing, then dashboard metrics" --chain --max-steps 9 --yes
swarm ai "process the full pending queue" --chain --max-steps 20 --yes
```

Use `--yes` with `--chain` for fully automated runs; omit it to confirm each step interactively.

### `swarm session`

Launch an interactive LLM session in the division root, seeded with `.swarm/` context.

```bash
swarm session                          # interactive, auto-detect CLI
swarm session --with claude            # prefer Claude Code
swarm session --with gemini            # prefer Gemini CLI
swarm session "what should I pick up?" # single non-interactive turn
```

For **Claude Code**: CLAUDE.md already loads `.swarm/` context automatically.
For **gemini / opencode**: writes `.swarm/CURRENT_SESSION.md` context file first.

---

## Agent Roles

Agent roles extend multi-agent task mode with structured behaviors. See
[Agent Roles](ROLES.md) for the full conceptual guide. All role state is stored in
`.swarm/roles/<name>.json` — enabling or disabling a role never modifies `queue.md`.

### `swarm role list`

Show all known roles and their current status.

```bash
swarm role list
```

### `swarm role enable`

Enable a role (or reconfigure it if already enabled).

```bash
swarm role enable inspector
swarm role enable inspector --max-iterations 3 --require-proof "branch,commit,tests"
swarm role enable inspector --agent inspector-bot-1
swarm role enable watchdog
swarm role enable supervisor
swarm role enable librarian
```

| Option | Default | Description |
|--------|---------|-------------|
| `--max-iterations N` | `3` | (inspector) Fail count before watchdog escalation |
| `--require-proof FIELDS` | `branch,commit` | (inspector) Comma-separated required proof fields |
| `--agent AGENT_ID` | *(any)* | Assign a specific agent ID to this role |

### `swarm role disable`

Remove a role config (idempotent).

```bash
swarm role disable inspector
```

### `swarm role show`

Print full configuration for a role.

```bash
swarm role show inspector
```

---

## Inspector

The `swarm inspect` command is used by the inspector agent to verify a worker's
proof-of-work and either pass (mark done) or fail (re-open) a work item.

The inspector role must be enabled first: `swarm role enable inspector`.

### `swarm inspect`

```bash
swarm inspect API-042 --pass
swarm inspect API-042 --pass --note "Tests pass, memory profile clean"

swarm inspect API-042 --fail --reason "Edge case under burst not handled — see test_rate_limiter.py:98"
```

| Option | Required | Description |
|--------|----------|-------------|
| `--pass` | one of | Accept proof — mark item done, sign in trail |
| `--fail` | one of | Reject proof — reopen item, increment `inspect_fails` |
| `--reason TEXT` | with `--fail` | Explanation written back to the item's notes |
| `--agent TEXT` | no | Inspector agent ID override |

**On `--pass`:** item moves to Done, inspector agent ID is recorded, operation signed
in `trail.log`.

**On `--fail`:** item moves back to OPEN, `proof:` is cleared, `inspect_fails` is
incremented. If `inspect_fails >= max_iterations` and watchdog is enabled, an
escalation alert is printed.

See [Agent Roles → Inspector](ROLES.md#inspector) for the full workflow diagram.

---

---

## Spawn & Crawl

### `swarm spawn`

Launch an agent CLI in a named tmux window for a specific work item or role.
Requires **tmux 3.0+** and the chosen agent CLI on PATH.

```bash
# Worker — claim and open
swarm spawn API-042                          # opencode worker, auto-claims item
swarm spawn API-042 --agent claude           # Claude Code worker
swarm spawn API-042 --agent ollama           # local Ollama worker
swarm spawn API-042 --no-claim               # open window without claiming

# Role agents
swarm spawn --role inspector                  # inspector monitor window
swarm spawn --role supervisor                 # supervisor overview window
swarm spawn --role watchdog                   # watchdog audit loop

# Session control
swarm spawn API-042 --session my-project     # custom tmux session name
swarm spawn API-042 --window-name rate-limiter-fix   # custom window name
swarm spawn API-042 --agent-id my-agent-42   # explicit SWARM_AGENT_ID
```

| Option | Default | Description |
|--------|---------|-------------|
| `--agent` | `opencode` | Agent CLI: `opencode`, `claude`, `ollama`, `bedrock` |
| `--role` | — | Spawn as a role agent (`inspector`, `supervisor`, `watchdog`) |
| `--session` | `swarm` | tmux session name (created if absent) |
| `--window-name` | item_id or role | tmux window name |
| `--no-claim` | false | Skip auto-claiming the item |
| `--agent-id` | `spawn-<id>-<ts>` | Override `SWARM_AGENT_ID` env var |

**Environment set in the tmux window:**

| Variable | Value |
|----------|-------|
| `SWARM_AGENT_ID` | Effective agent ID |
| `SWARM_ITEM_ID` | Item being worked on (if any) |
| `SWARM_ROLE` | `worker`, `inspector`, `supervisor`, or `watchdog` |

**Attach / navigate:**
```bash
tmux attach -t swarm                     # attach to session
tmux select-window -t swarm:API-042      # switch to window
tmux list-windows -t swarm               # list all windows
```

**Supported agents and install:**

| Agent | Install |
|-------|---------|
| `opencode` | `npm install -g opencode-ai` |
| `claude` | [Claude Code CLI](https://claude.ai/code) |
| `ollama` | `brew install ollama` |
| `bedrock` | `pip install 'dot-swarm[ai]'` + `aws configure` |

---

### `swarm crawl`

Walk the current directory tree to build context. Stops descending into any
subdirectory that already has a `.swarm/` directory (those are separate divisions).
Results are written to `.swarm/context.md` under a `## Directory Map` section.

Combined with `swarm heal`, this replaces the need for a separate librarian role agent.

```bash
swarm crawl                  # catalog from cwd, depth 3
swarm crawl --depth 5        # go deeper
swarm crawl --create-items   # also create OPEN queue items for each uncatalogued dir
swarm crawl --dry-run        # preview without writing anything
```

| Option | Default | Description |
|--------|---------|-------------|
| `--depth N` | `3` | Max directory depth to walk |
| `--create-items` | false | Create OPEN queue items (project: librarian) for each uncatalogued dir |
| `--dry-run` | false | Print what would be cataloged, write nothing |

**What gets skipped:**
`.git/`, `__pycache__/`, `node_modules/`, `.venv/`, `venv/`, `dist/`, `build/`

**Example output:**
```
Crawled /Users/me/api-service

  Swarm divisions found (2) — skipped:
    services/auth/
    services/payments/

  Catalogued (4 dirs):
    docs/ — 12 files (8×.md, 3×.png, 1×.svg)
    scripts/ — 5 files (5×.sh)
    config/ — 3 files (2×.toml, 1×.json)
    tests/fixtures/ — 8 files (8×.json)

  Written to: .swarm/context.md

Run 'swarm heal' to verify context alignment after cataloging.
```

---

### `swarm trail`

Manage whether `.swarm/` is visible or hidden in git. The trail is **invisible by
default** — your swarm state stays private unless you explicitly share it.

```bash
swarm trail status     # show current visibility + .gitignore path
swarm trail invisible  # hide .swarm/ (adds .swarm/ to .gitignore)
swarm trail visible    # share .swarm/ (removes .swarm/ from .gitignore)
```

**Why this matters:** sharing a git repo also shares the full swarm trail — every
claim, completion, handoff note, and memory entry. `invisible` is the default so
that decision is always explicit.

**After making visible:**
```bash
swarm trail visible
git add .swarm/
git commit -m "chore: share swarm trail"
```

**Security note:** `trail invisible` only affects git tracking. The `.swarm/`
files remain fully functional locally; the signing key (`.swarm/.signing_key`)
and quarantine dir are always excluded by `.swarm/.gitignore` regardless of
trail visibility.

`swarm init` defaults to invisible. Pass `--visible` to opt in at init time:
```bash
swarm init --visible   # share trail from the start
```

---

### `swarm configure`

Interactive wizard to set your default LLM interface and (if Bedrock) model + region.

```bash
swarm configure
```

Config stored at `~/.config/swarm/config.toml`. Credentials are never stored here —
use `aws configure` or env vars for Bedrock; the respective CLI handles auth for others.

---

## Setup & CI

### `swarm setup-drift-check`

Install the `swarm-drift-check.yml` GitHub Actions workflow into the current repo.
Uses the `gh` CLI to set secrets if needed.

```bash
swarm setup-drift-check           # install workflow file only
swarm setup-drift-check --commit  # also commit + push
```

See [Drift Check Setup](DRIFT_CHECK_SETUP.md) for AWS Bedrock prerequisites.

---

## Item ID Convention

```
<DIVISION-CODE>-<3-digit-number>
```

| Division | Code |
|----------|------|
| Org level | `ORG` |
| api-service | `API` |
| auth-service | `AUTH` |
| dashboard | `DASH` |
| mobile-app | `MOB` |
| firmware | `FW` |
| docs | `DOC` |
| infra | `INF` |
| homelab | `LAB` |
| dot_swarm | `SWC` |

IDs are assigned sequentially and never reused.

---

## Security & Trust Model

### Pheromone Trail Signing

Every `swarm init` generates a per-swarm HMAC-SHA256 signing identity:

| File | Description | Commit? |
|------|-------------|--------|
| `.swarm/identity.json` | Public fingerprint (swarm ID, algorithm, created) | ✅ Yes |
| `.swarm/.signing_key` | 256-bit private HMAC key | ❌ No (.gitignored) |
| `.swarm/trail.log` | Append-only signed operation log | ❌ No (local only) |
| `.swarm/blocked_peers.json` | Blocked fingerprints | ✅ Optional |

Each `swarm ai` batch records a signed entry in `trail.log`:
```json
{"timestamp":"2026-04-06T14:00Z","swarm_id":"a1b2c3d4","fingerprint":"f8e7d6c5","agent_id":"cascade","op":"ai_batch","payload":{"step":1,"ops":["done","write_state"]},"signature":"abc123..."}
```

To verify the trail: `swarm audit --trail` or `swarm heal`.
To block a bad actor: `swarm heal --fix` (auto-blocks tampered fingerprints).

### Adversarial Content Detection

`swarm heal` and `swarm audit --security` scan for 18 patterns across 3 severity levels:

| Severity | Categories |
|----------|------------|
| **CRITICAL** | PROMPT_INJECTION, INSTRUCTION_ERASURE, PERSONA_HIJACK, JAILBREAK, LLM_TEMPLATE_INJECTION, SAFETY_OVERRIDE |
| **HIGH** | NON_DISCLOSURE, CONTROL_CHARACTERS, PRIORITY_OVERRIDE |
| **MEDIUM** | HIDDEN_HTML_COMMENT, HIDDEN_MD_COMMENT, HTML_INJECTION, CODE_INJECTION |

Files scanned: `state.md`, `queue.md`, `memory.md`, `context.md`, `BOOTSTRAP.md`, `workflows/*.md`, `CLAUDE.md`, `.windsurfrules`, `.cursorrules`, `.github/copilot-instructions.md`.

### swarms.ai Integration

For multi-agent frameworks, use the built-in bridge:

```python
from dot_swarm.swarms_provider import DotSwarmStateProvider, StigmergicSwarm

# Inject .swarm/ state into any agent's system prompt
provider = DotSwarmStateProvider(swarm_path="./.swarm")
system_prompt = provider.build_system_prompt(agent_name="Coordinator")

# Read queue state
queue = provider.get_queue()   # {"active": [...], "pending": [...], "done": [...]}

# Apply AI response operations back to .swarm/ files
results = provider.apply_operations(llm_response_json, agent_id="my-agent")

# Stigmergic multi-agent coordination (requires: pip install swarms)
from swarms import Agent
swarm = StigmergicSwarm(swarm_path=".", agents=[agent1, agent2], max_rounds=10)
results = swarm.run("Implement the OAuth2 integration")
```

Agents coordinate *indirectly* through `.swarm/` files (stigmergic protocol) — no direct agent-to-agent message passing required. Full git audit trail maintained automatically.
