---
title: Agent Roles
nav_order: 3
---

# Agent Roles

Agent roles extend dot_swarm coordination with structured behaviors for multi-agent
task mode. Every role is **opt-in** — enabling one never affects queue.md or any
work items until that role's commands are explicitly invoked. Roles are stored as
JSON configs in `.swarm/roles/<name>.json` and can be toggled at any time.

```bash
swarm role list                        # see all roles + status
swarm role enable inspector            # turn on
swarm role disable inspector           # turn off (removes config file)
swarm role show inspector              # full config
```

---

## Available Roles

| Role | Purpose | Key command |
|------|---------|-------------|
| **inspector** | Requires proof-of-work before an item can be marked done | `swarm inspect` |
| **supervisor** | Holistic progress view across all active items and phases | `swarm spawn --role supervisor` |

{: .note }
> **Watchdog and Librarian are now built-in.**
> The **watchdog** escalation pattern is now intrinsic to the inspector retry loop —
> items auto-BLOCK when retries are exhausted, surfacing in `swarm audit` and
> `swarm status` without a separate role agent. Use `swarm spawn --role watchdog`
> to open a dedicated monitoring window if you want a live operator.
>
> The **librarian** catalog function is now `swarm crawl`. Run it alongside
> `swarm heal` for a full context + alignment pass — no separate role required.

---

## Inspector

The inspector role solves the **"fake done" problem** — an LLM worker marking an item
complete without actually finishing the work. When enabled, workers cannot call
`swarm done` directly. Instead they must attach proof of work and let a designated
inspector agent verify it.

### Enforcement

The gate applies to every write path: `swarm done`, MCP `swarm_done`, the
`swarm gui` dashboard, and `swarm ai` operations all refuse a direct done
while the role is enabled. Proof alone does not unlock done.

- `swarm inspect` / MCP `swarm_inspect` refuse an inspector that holds a
  claim on the item (no self-inspection).
- When the role has `--agent`, only that agent may inspect.
- `swarm done <id> --force` (CLI only) is the human override.

Agent IDs are not authenticated unless agents sign with per-agent
identities, so the gate stops accidental self-completion, not a worker
that deliberately runs under a different `--agent` value.

### Enabling

```bash
swarm role enable inspector
swarm role enable inspector --max-iterations 3 --require-proof "branch,commit,tests"
swarm role enable inspector --agent inspector-bot-1
```

| Option | Default | Description |
|--------|---------|-------------|
| `--max-iterations` | `3` | Fail count before watchdog escalation |
| `--require-proof` | `branch,commit` | Comma-separated required proof fields |
| `--agent` | *(any)* | Assign a specific agent ID as inspector |

### Worker flow

```bash
# 1. Claim and do the work normally
swarm claim CLD-042

# 2. Instead of swarm done, mark partial and attach proof
swarm partial CLD-042 --proof "branch:feature/oauth2 commit:abc1234 tests:42/42"
#   ^ dot_swarm validates that all required proof fields are present and warns if not
```

The `proof:` field accepts a space-separated list of `key:value` pairs. Required fields
are checked against `require_proof_fields` in the role config. Common fields:

| Field | Example |
|-------|---------|
| `branch` | `branch:feature/oauth2` |
| `commit` | `commit:abc1234` |
| `tests` | `tests:42/42` |
| `diff` | `diff:+187/-23` |
| `pr` | `pr:47` |

### Inspector flow

```bash
# Inspector agent (or human) reviews the proof, then passes or fails

swarm inspect CLD-042 --pass
swarm inspect CLD-042 --pass --note "Tests pass, code reviewed, merging"

swarm inspect CLD-042 --fail --reason "Edge case X not handled — see test_auth.py:142"
```

**On `--pass`:** the item is marked Done, the inspector's agent ID is recorded in
the trail, and the operation is signed in `trail.log`.

**On `--fail`:** the item is re-opened to `OPEN` state, `proof:` is cleared,
`inspect_fails` is incremented, and a failure note is appended. The worker will see
the item in `swarm ready` again and can re-claim it.

### Iteration limit and escalation

After `max_iterations` consecutive failures:

- If the **watchdog** role is also enabled: a watchdog alert is printed and the item
  is flagged for human review.
- If watchdog is **not** enabled: a plain warning is shown with the command to enable it.

```
Watchdog alert: CLD-042 has failed inspection 3/3 times. Human review required.
```

### Bypassing (human override)

```bash
swarm done CLD-042 --force    # skip inspector gate; for human directors only
```

### How the proof is stored

The `proof:` and `inspect_fails:` fields are stored inline in `queue.md`, just like
`priority:` and `notes:` — fully human-readable and git-diffable:

```markdown
- [>] [CLD-042] [CLAIMED · worker-1 · 2026-04-18T10:00Z · PARTIAL] Implement OAuth2
      priority: high | project: auth
      proof: branch:feature/oauth2 commit:abc1234 tests:42/42
      inspect_fails: 1
```

---

## Watchdog (built-in)

Watchdog escalation is now intrinsic to the inspector retry loop — **no separate role
agent is required**. When an item's `inspect_fails` reaches its effective retry limit,
it is automatically set to `BLOCKED` with a clear reason. Blocked items surface
immediately in `swarm audit`, `swarm status`, and `swarm heal`.

**Retry limit precedence:**

1. **Task level** — `max_retries:` field on the queue item (set via `swarm add --max-retries N`)
2. **Role level** — `max_iterations` in `.swarm/roles/inspector.json` (set via `swarm role enable inspector --max-iterations N`)
3. **Default** — 3

```bash
# Task-level override (this item gets 5 tries, not the role default)
swarm add "Implement OAuth2" --max-retries 5

# Role-level default
swarm role enable inspector --max-iterations 3
```

When exhausted:
```
Max retries exhausted: [SWC-042] is now BLOCKED (3/3 fails).
  Use 'swarm unblock --reclaim' to manually reassign, or 'swarm done --force' to override.
```

To run a **live watchdog monitoring window** (optional):
```bash
swarm spawn --role watchdog    # opens a tmux window running swarm audit --full in a loop
```

---

## Supervisor

The supervisor provides a holistic view across all active items and phases. Launch
one as a named tmux window using `swarm spawn`:

```bash
swarm spawn --role supervisor    # opens tmux window with SWARM_ROLE=supervisor set
```

Inside the supervisor window the agent has access to all `swarm` commands. Useful
starting points:

```bash
swarm explore --depth 3    # colony heartbeat across all divisions
swarm report --only active # active items only, all divisions
swarm audit --full         # full health pass
```

**Roadmap:**
- `swarm supervisor brief --format md` — structured progress brief for a human director
- Hierarchy roll-up via `swarm ascend / descend`

---

## Librarian (built-in via `swarm crawl`)

Directory cataloging is now a first-class command rather than a role agent:

```bash
swarm crawl                   # catalog directory tree → context.md
swarm crawl --create-items    # also create queue items for undocumented dirs
swarm heal                    # verify alignment after crawl
```

See [CLI Reference → swarm crawl](CLI_REFERENCE.md#swarm-crawl) for full options.

To run a **live librarian window** that monitors a directory for new uncatalogued
content:
```bash
swarm spawn --role watchdog    # reuse watchdog window for periodic crawl + heal
```

---

## Role Interaction Map

```
Worker agent  (swarm spawn SWC-042 --agent opencode)
    │
    ├── swarm claim <id>
    ├── ... do work ...
    └── swarm partial <id> --proof "branch:X commit:Y tests:N/N"
                │
                ▼
        Inspector agent  (swarm spawn --role inspector)
            │
            ├── swarm inspect <id> --pass
            │       └──► DONE  (signed in trail.log)
            │
            └── swarm inspect <id> --fail --reason "..."
                        └──► re-OPEN  (inspect_fails++)
                                │
                                │  (if inspect_fails >= effective max_retries)
                                ▼
                        BLOCKED  ←── automatic, no external agent needed
                        (surfaces in swarm audit / swarm status)
                                │
                                ▼
                    human: swarm unblock --reclaim
                        or: swarm done --force
```

---

## Comparison with Gastown Roles

| dot_swarm | Gastown | Notes |
|-----------|---------|-------|
| inspector | Witness (partial) | dot_swarm inspector actively gates done; Witness only monitors health |
| *(auto-block on retries)* | — | Watchdog escalation is intrinsic; no separate role agent |
| supervisor | Mayor (partial) | Mayor orchestrates; supervisor observes and reports |
| `swarm crawl` | — | Librarian function is a built-in command, not a role |
| `swarm spawn` | Colony bootstrap | Gastown has fixed colony setup; dot_swarm spawns on demand |
| *(worker)* | Polecats | Ephemeral workers; any agent with `$SWARM_AGENT_ID` |
| *(merge gate)* | Refinery | SWC-024 roadmap item |

dot_swarm roles are **advisory and toggleable** — you enable only what your workflow
needs. Gastown's roles are baked into the fixed colony architecture.
