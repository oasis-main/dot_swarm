"""dot_swarm core file operations.

All reads and writes go through these functions. Write operations are atomic
(write to temp file, then rename). File locking is used for concurrent safety.
"""

from __future__ import annotations

try:
    import fcntl
except ImportError:
    fcntl = None
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .models import (
    Claim, ItemState, Priority, SwarmPaths, SwarmState, WorkItem,
    _now_ts, _parse_ts, PRIORITY_ORDER, utcnow,
)


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------

SECTION_RE = re.compile(r"^## (Active|Pending|Done)$", re.MULTILINE)
FIELD_RE = re.compile(r"^\s{6}(?P<key>priority|project|notes|depends|supersedes|duplicates|refs|proof|inspect_fails|max_retries): (?P<value>.+)$")


def read_queue(paths: SwarmPaths) -> tuple[list[WorkItem], list[WorkItem], list[WorkItem]]:
    """Parse queue.md into (active, pending, done) lists, resolved with dynamic claims."""
    if not paths.queue.exists():
        return [], [], []

    text = paths.queue.read_text()
    sections = _split_sections(text)
    active = _parse_items(sections.get("Active", ""))
    pending = _parse_items(sections.get("Pending", ""))
    done = _parse_items(sections.get("Done", ""))

    # Resolve with dynamic claims
    claims = read_claims(paths)
    active, pending, done = resolve_claims(active, pending, done, claims)

    return active, pending, done


def read_claims(paths: SwarmPaths) -> list[Claim]:
    """Read all claim files from .swarm/claims/ (append-only trail)."""
    claims = []
    if not paths.claims.is_dir():
        return []
    for p in paths.claims.glob("*.json"):
        try:
            data = json.loads(p.read_text())
            claims.append(Claim.from_dict(data))
        except (json.JSONDecodeError, KeyError, ValueError):
            continue
    return claims


def write_claim(paths: SwarmPaths, claim: Claim) -> Path:
    """Append a claim record to .swarm/claims/.

    Append-only: never overwrites. Each (item_id, agent_id, timestamp) triple
    becomes a new immutable record. State transitions (CLAIMED → DONE → ...)
    are recorded as additional records, not in-place mutations.
    """
    paths.claims.mkdir(parents=True, exist_ok=True)
    ts_safe = claim.timestamp.strftime("%Y%m%dT%H%M%SZ")
    base = f"{claim.item_id}_{claim.agent_id}_{ts_safe}"
    p = paths.claims / f"{base}.json"
    suffix = 0
    while p.exists():  # collision under same-second concurrent writes
        suffix += 1
        p = paths.claims / f"{base}_{suffix}.json"
    p.write_text(json.dumps(claim.to_dict(), indent=2))
    return p


def clear_claims(paths: SwarmPaths, item_id: str) -> None:
    """DEPRECATED: destructive helper retained for compaction/tests only.

    Lifecycle operations no longer call this — they append release records
    via supersede_claims() instead, preserving the full append-only trail.
    """
    if not paths.claims.is_dir():
        return
    for p in paths.claims.glob(f"{item_id}_*.json"):
        p.unlink()


_ACTIVE_STATES: frozenset[ItemState] = frozenset({
    ItemState.CLAIMED, ItemState.PARTIAL, ItemState.COMPETING, ItemState.REVIEW,
})
_TERMINAL_STATES: frozenset[ItemState] = frozenset({
    ItemState.DONE, ItemState.BLOCKED, ItemState.CANCELLED,
})


def supersede_claims(
    paths: SwarmPaths,
    item_id: str,
    new_state: ItemState,
    agent_id: str = "system",
    note: str = "",
    proof: str = "",
) -> Path:
    """Append a release record that supersedes prior active claims.

    Use this instead of clear_claims when transitioning an item to a new
    lifecycle state — the prior records remain on disk for audit, and the
    resolver picks the newest record per (item, agent) when reading.
    """
    return write_claim(paths, Claim(
        item_id=item_id,
        agent_id=agent_id,
        state=new_state,
        timestamp=utcnow(),
        proof=proof,
        note=note,
    ))


def competitors_for(paths: SwarmPaths, item_id: str) -> list[Claim]:
    """Return the list of currently-active competitor claims on an item.

    Per-agent newest claim is picked; only those whose latest state is in
    {CLAIMED, COMPETING, PARTIAL, REVIEW} count as still-in-the-running.
    """
    by_agent: dict[str, Claim] = {}
    for c in read_claims(paths):
        if c.item_id != item_id:
            continue
        prev = by_agent.get(c.agent_id)
        if prev is None or c.timestamp > prev.timestamp:
            by_agent[c.agent_id] = c
    return sorted(
        [c for c in by_agent.values() if c.state in _ACTIVE_STATES],
        key=lambda c: c.timestamp,
    )


def promote_competitor(
    paths: SwarmPaths,
    item_id: str,
    winner_agent: str,
    reason: str = "",
) -> tuple[Claim, list[Claim]]:
    """Promote one competitor to CLAIMED winner; record losers as withdrawn.

    Returns (winner_claim, loser_claims). The winner gets a fresh CLAIMED
    record; each other active competitor gets an OPEN release record with
    note 'lost-competition' so the trail records the outcome.
    """
    rivals = competitors_for(paths, item_id)
    winner_present = any(c.agent_id == winner_agent for c in rivals)
    if not winner_present:
        raise ValueError(
            f"Agent {winner_agent!r} has no active claim on {item_id}. "
            f"Active competitors: {[c.agent_id for c in rivals] or 'none'}"
        )

    now = utcnow()
    winner_claim = Claim(
        item_id=item_id, agent_id=winner_agent, state=ItemState.CLAIMED,
        timestamp=now, note=f"promoted-winner: {reason}".strip(": "),
    )
    write_claim(paths, winner_claim)

    losers: list[Claim] = []
    for c in rivals:
        if c.agent_id == winner_agent:
            continue
        loser = Claim(
            item_id=item_id, agent_id=c.agent_id, state=ItemState.OPEN,
            timestamp=utcnow(),
            note=f"lost-competition: winner={winner_agent}".strip(),
        )
        write_claim(paths, loser)
        losers.append(loser)
    return winner_claim, losers


def resolve_claims(
    active: list[WorkItem],
    pending: list[WorkItem],
    done: list[WorkItem],
    claims: list[Claim],
) -> tuple[list[WorkItem], list[WorkItem], list[WorkItem]]:
    """Resolve the queue against the append-only claims trail.

    Algorithm:
      1. For each (item, agent), keep only the newest claim record.
      2. If any agent's newest is terminal (DONE/BLOCKED/CANCELLED) and
         strictly newer than every active record on that item, the terminal
         state wins.
      3. Otherwise, count agents whose newest is "active" (CLAIMED/COMPETING/
         PARTIAL/REVIEW). Zero → OPEN. One → that state. Two or more → COMPETING.
    """
    if not claims:
        return active, pending, done

    all_items = {i.id: i for i in active + pending + done}

    # newest claim per (item_id, agent_id)
    by_item_agent: dict[str, dict[str, Claim]] = {}
    for c in claims:
        agents = by_item_agent.setdefault(c.item_id, {})
        prev = agents.get(c.agent_id)
        if prev is None or c.timestamp > prev.timestamp:
            agents[c.agent_id] = c

    for item_id, agent_claims in by_item_agent.items():
        if item_id not in all_items:
            continue
        item = all_items[item_id]

        # DONE in queue.md always wins over any dynamic claim
        if item.state == ItemState.DONE:
            continue

        per_agent = list(agent_claims.values())
        terminals = [c for c in per_agent if c.state in _TERMINAL_STATES]
        actives = [c for c in per_agent if c.state in _ACTIVE_STATES]

        # Terminal wins if strictly newer than every active record (or no actives).
        if terminals:
            newest_terminal = max(terminals, key=lambda c: c.timestamp)
            if not actives or newest_terminal.timestamp >= max(c.timestamp for c in actives):
                item.state = newest_terminal.state
                item.claimed_by = newest_terminal.agent_id
                item.claimed_at = newest_terminal.timestamp
                if newest_terminal.state == ItemState.DONE:
                    item.done_at = newest_terminal.timestamp
                    if newest_terminal.note:
                        item.notes = (item.notes + " | " + newest_terminal.note).strip(" | ")
                    if item in active:
                        active.remove(item)
                    if item in pending:
                        pending.remove(item)
                    if item not in done:
                        done.append(item)
                continue

        if not actives:
            # All claims released back to OPEN
            item.state = ItemState.OPEN
            item.claimed_by = None
            item.claimed_at = None
            if item in active:
                active.remove(item)
                pending.append(item)
            continue

        if len(actives) >= 2:
            # Multi-agent contention → COMPETING
            sorted_active = sorted(actives, key=lambda c: c.timestamp)
            item.state = ItemState.COMPETING
            item.claimed_by = ", ".join(c.agent_id for c in sorted_active)
            item.claimed_at = sorted_active[0].timestamp
            item.competitors = sorted_active
            for c in sorted_active:
                if c.proof and not item.proof:
                    item.proof = c.proof
        else:
            c = actives[0]
            item.state = c.state
            item.claimed_by = c.agent_id
            item.claimed_at = c.timestamp
            if c.proof:
                item.proof = c.proof
            if c.note:
                item.notes = (item.notes + " | " + c.note).strip(" | ")
            item.competitors = []

        # Re-bucket by resolved state
        if item.state in _ACTIVE_STATES:
            if item in pending:
                pending.remove(item)
                active.append(item)
        elif item.state == ItemState.OPEN:
            if item in active:
                active.remove(item)
                pending.append(item)

    return active, pending, done


def _split_sections(text: str) -> dict[str, str]:
    """Split queue.md text into section name → content."""
    result: dict[str, str] = {}
    current: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^## (Active|Pending|Done)$", line)
        if m:
            if current is not None:
                result[current] = "\n".join(lines)
            current = m.group(1)
            lines = []
        else:
            if current is not None:
                lines.append(line)
    if current:
        result[current] = "\n".join(lines)
    return result


def _parse_items(section_text: str) -> list[WorkItem]:
    """Parse a section of queue.md into WorkItems, attaching continuation fields."""
    items: list[WorkItem] = []
    current_item: WorkItem | None = None
    for line in section_text.splitlines():
        item = WorkItem.parse_line(line)
        if item:
            current_item = item
            items.append(item)
        elif current_item and (fm := FIELD_RE.match(line)):
            key, value = fm.group("key"), fm.group("value").strip()
            if key == "priority":
                # value format: "high | project: cloud-stability"
                for part in value.split("|"):
                    part = part.strip()
                    if part.startswith("project:"):
                        current_item.project = part.split(":", 1)[1].strip()
                    else:
                        try:
                            current_item.priority = Priority(part)
                        except ValueError:
                            pass
            elif key == "notes":
                current_item.notes = value
            elif key == "depends":
                current_item.depends = [d.strip() for d in value.split(",")]
            elif key == "supersedes":
                current_item.supersedes = [d.strip() for d in value.split(",") if d.strip()]
            elif key == "duplicates":
                current_item.duplicates = [d.strip() for d in value.split(",") if d.strip()]
            elif key == "refs":
                current_item.refs = [r.strip() for r in value.split(",")]
            elif key == "proof":
                current_item.proof = value
            elif key == "inspect_fails":
                try:
                    current_item.inspect_fails = int(value)
                except ValueError:
                    pass
            elif key == "max_retries":
                try:
                    current_item.max_retries = int(value)
                except ValueError:
                    pass
    return items


def write_queue(
    paths: SwarmPaths,
    active: list[WorkItem],
    pending: list[WorkItem],
    done: list[WorkItem],
) -> None:
    """Write queue.md atomically from three lists."""
    lines = [
        f"# Queue — {_division_name(paths)} ({_level_label(paths)})",
        "",
        "Items are listed in priority order within each section.",
        "Item IDs: `<DIVISION-CODE>-<3-digit-number>` — assigned sequentially, never reused.",
        "",
        "---",
        "",
        "## Active",
        "",
    ]
    for item in active:
        lines.append(item.to_line())
        lines.append("")

    lines += ["## Pending", ""]
    # Sort pending by priority
    pending_sorted = sorted(pending, key=lambda i: PRIORITY_ORDER.get(i.priority, 99))
    for item in pending_sorted:
        lines.append(item.to_line())
        lines.append("")

    lines += ["## Done", ""]
    for item in done:
        lines.append(item.to_line())
        lines.append("")

    _atomic_write(paths.queue, "\n".join(lines))


def next_item_id(paths: SwarmPaths, division_code: str) -> str:
    """Compute the next available item ID for a division code."""
    active, pending, done = read_queue(paths)
    all_items = active + pending + done
    id_re = re.compile(rf"^{re.escape(division_code)}-(\d+)$")
    max_num = 0
    for item in all_items:
        if m := id_re.match(item.id):
            max_num = max(max_num, int(m.group(1)))
    return f"{division_code}-{max_num + 1:03d}"


def next_hash_id(paths: SwarmPaths, prefix: str = "sw") -> str:
    """Generate a content-free hash-style ID like ``sw-a1b2``.

    Beads-style short IDs help avoid merge collisions when multiple workers
    add items concurrently in separate worktrees. Collisions inside the
    current queue are detected and retried.
    """
    import secrets
    active, pending, done = read_queue(paths)
    taken = {i.id for i in active + pending + done}
    for _ in range(32):
        candidate = f"{prefix}-{secrets.token_hex(2)}"
        if candidate not in taken:
            return candidate
    # 32 collisions in a 65k space means the queue is huge — widen to 6 hex.
    while True:
        candidate = f"{prefix}-{secrets.token_hex(3)}"
        if candidate not in taken:
            return candidate


def claim_item(paths: SwarmPaths, item_id: str, agent_id: str, compete: bool = False) -> WorkItem:
    """Claim an OPEN item, or contend an already-claimed one with compete=True.

    Always appends a record to the .swarm/claims/ trail. The queue.md
    rendering is recomputed from the trail on the next read.
    """
    active, pending, done = read_queue(paths)
    target = _find_item(pending + active, item_id)
    if target is None:
        raise ValueError(f"Item {item_id} not found in active or pending queue.")

    if target.state in (ItemState.CLAIMED, ItemState.PARTIAL, ItemState.COMPETING):
        if not compete:
            raise ValueError(
                f"Item {item_id} is already claimed by {target.claimed_by}. "
                "Use --compete to submit a competing implementation."
            )
        new_state = ItemState.COMPETING
    else:
        new_state = ItemState.CLAIMED

    now = utcnow()
    write_claim(paths, Claim(
        item_id=target.id,
        agent_id=agent_id,
        state=new_state,
        timestamp=now,
    ))

    # Reflect the claim in queue.md for human readers (resolver will recompute)
    target.state = new_state
    target.claimed_by = agent_id
    target.claimed_at = now
    if target in pending:
        pending.remove(target)
        active.append(target)
    write_queue(paths, active, pending, done)

    # Re-resolve so callers see the multi-agent COMPETING aggregation
    active, pending, done = read_queue(paths)
    return _find_item(active + pending + done, item_id) or target


def done_item(paths: SwarmPaths, item_id: str, agent_id: str, note: str = "") -> WorkItem:
    """Mark a claimed item as done; appends a DONE release record to the trail."""
    active, pending, done = read_queue(paths)
    target = _find_item(active + pending, item_id)
    if target is None:
        raise ValueError(f"Item {item_id} not found in active or pending queue.")

    target.state = ItemState.DONE
    target.done_at = utcnow()
    if note:
        target.notes = (target.notes + " | " + note).strip(" | ")

    active = [i for i in active if i.id != item_id]
    pending = [i for i in pending if i.id != item_id]
    if target not in done:
        done.append(target)

    write_queue(paths, active, pending, done)
    supersede_claims(paths, item_id, ItemState.DONE, agent_id=agent_id, note=note)
    return target


def partial_item(paths: SwarmPaths, item_id: str, agent_id: str, note: str = "", proof: str = "") -> WorkItem:
    """Append a PARTIAL claim record (checkpoint without completing)."""
    active, pending, done = read_queue(paths)
    target = _find_item(active + pending, item_id)
    if target is None:
        raise ValueError(f"Item {item_id} not found in active or pending queue.")

    now = utcnow()
    write_claim(paths, Claim(
        item_id=item_id,
        agent_id=agent_id,
        state=ItemState.PARTIAL,
        timestamp=now,
        note=note,
        proof=proof,
    ))

    target.state = ItemState.PARTIAL
    target.claimed_by = agent_id
    target.claimed_at = now
    if note:
        target.notes = (target.notes + " | " + note).strip(" | ")
    if proof:
        target.proof = proof

    pending = [i for i in pending if i.id != item_id]
    if target not in active:
        active.append(target)

    write_queue(paths, active, pending, done)
    return target


def block_item(paths: SwarmPaths, item_id: str, reason: str) -> WorkItem:
    """Mark a work item as BLOCKED; appends a BLOCKED release record."""
    active, pending, done = read_queue(paths)
    target = _find_item(active + pending, item_id)
    if target is None:
        raise ValueError(f"Item {item_id} not found in active or pending queue.")

    target.state = ItemState.BLOCKED
    target.notes = f"BLOCKED: {reason}"

    write_queue(paths, active, pending, done)
    supersede_claims(paths, item_id, ItemState.BLOCKED, agent_id="system", note=reason)
    return target


def add_item(
    paths: SwarmPaths,
    description: str,
    division_code: str | None = None,
    priority: Priority = Priority.MEDIUM,
    project: str = "misc",
    notes: str = "",
    refs: list[str] | None = None,
    depends: list[str] | None = None,
    supersedes: list[str] | None = None,
    duplicates: list[str] | None = None,
    hash_id: bool = False,
) -> WorkItem:
    """Add a new OPEN work item with an auto-assigned ID.

    Set ``hash_id=True`` for a beads-style ``sw-XXXX`` ID that won't collide
    when two worktrees add items in parallel before merging.
    """
    if hash_id:
        item_id = next_hash_id(paths)
    else:
        code = division_code or _division_code_from_paths(paths)
        item_id = next_item_id(paths, code)
    item = WorkItem(
        id=item_id,
        state=ItemState.OPEN,
        description=description,
        priority=priority,
        project=project,
        notes=notes,
        refs=refs or [],
        depends=depends or [],
        supersedes=supersedes or [],
        duplicates=duplicates or [],
    )
    active, pending, done = read_queue(paths)
    pending.append(item)
    write_queue(paths, active, pending, done)
    return item


# ---------------------------------------------------------------------------
# Ready / reopen
# ---------------------------------------------------------------------------

def ready_items(paths: SwarmPaths) -> list[WorkItem]:
    """Return OPEN pending items with all dependencies completed (à la `bd ready`).

    Items are also filtered out if they're known duplicates (``duplicates:``
    points to another item) or if another item supersedes them (some other
    item lists this one in its ``supersedes:`` field).
    """
    active, pending, done = read_queue(paths)
    done_ids = {i.id for i in done}
    superseded: set[str] = set()
    for i in active + pending + done:
        superseded.update(i.supersedes)
    result = []
    for item in pending:
        if item.state != ItemState.OPEN:
            continue
        if item.duplicates:
            continue
        if item.id in superseded:
            continue
        if not item.depends or all(dep in done_ids for dep in item.depends):
            result.append(item)
    return result


def reopen_item(
    paths: SwarmPaths,
    item_id: str,
    inspector_id: str,
    reason: str,
    role_max_iterations: int = 3,
) -> tuple["WorkItem", bool]:
    """Re-open an item after Inspector rejection.

    Returns (item, exhausted) where exhausted=True means retry limit was hit
    and the item has been BLOCKED rather than re-opened.
    """
    active, pending, done = read_queue(paths)
    target = _find_item(active + pending, item_id)
    if target is None:
        raise ValueError(f"Item {item_id} not found in active or pending queue.")

    target.inspect_fails += 1
    target.proof = ""
    fail_note = f"inspector-fail-{target.inspect_fails}: {reason} (by {inspector_id})"
    target.notes = (target.notes + " | " + fail_note).strip(" | ") if target.notes else fail_note

    # Effective limit: task-level overrides role-level when set
    effective_max = target.max_retries if target.max_retries > 0 else role_max_iterations
    exhausted = target.inspect_fails >= effective_max

    if exhausted:
        # Block rather than re-open — surfaces in swarm audit/status automatically
        target.state = ItemState.BLOCKED
        block_reason = (
            f"Max retries exhausted ({target.inspect_fails}/{effective_max}). "
            "Human review required. Use 'swarm unblock --reclaim' to reassign."
        )
        target.notes = (target.notes + " | " + block_reason).strip(" | ")
    else:
        target.state = ItemState.OPEN
        target.claimed_by = None
        target.claimed_at = None

    # Move back to pending (BLOCKED items stay in pending, surfaced by audit)
    active = [i for i in active if i.id != item_id]
    if target not in pending:
        pending.append(target)

    write_queue(paths, active, pending, done)
    final_state = ItemState.BLOCKED if exhausted else ItemState.OPEN
    supersede_claims(
        paths, item_id, final_state,
        agent_id=inspector_id, note=fail_note,
    )
    return target, exhausted


def crawl_directory(
    paths: SwarmPaths,
    root: Path,
    depth: int = 3,
    create_items: bool = False,
    dry_run: bool = False,
) -> list[dict]:
    """Walk *root*, skip subdirs that already have .swarm/, catalog the rest.

    Appends a '## Directory Map' section to context.md and optionally creates
    OPEN queue items for uncatalogued directories.

    Returns a list of dicts describing what was found/created.
    """
    import fnmatch

    IGNORE_PATTERNS = {".git", "__pycache__", "node_modules", ".venv", "venv",
                       "dist", "build", ".tox", ".eggs", "*.egg-info"}

    def _should_ignore(name: str) -> bool:
        return any(fnmatch.fnmatch(name, p) for p in IGNORE_PATTERNS)

    findings: list[dict] = []

    def _walk(directory: Path, current_depth: int) -> None:
        if current_depth > depth:
            return
        try:
            entries = sorted(directory.iterdir())
        except PermissionError:
            return

        for entry in entries:
            if not entry.is_dir() or _should_ignore(entry.name):
                continue
            if (entry / ".swarm").is_dir():
                findings.append({
                    "path": str(entry.relative_to(root)),
                    "type": "swarm_division",
                    "note": "already has .swarm/ — skipped",
                })
                continue  # don't descend into existing swarm divisions

            # Catalog this directory
            try:
                files = [f.name for f in entry.iterdir() if f.is_file()]
            except PermissionError:
                files = []
            ext_counts: dict[str, int] = {}
            for f in files:
                ext = Path(f).suffix.lower() or "(no ext)"
                ext_counts[ext] = ext_counts.get(ext, 0) + 1
            ext_summary = ", ".join(f"{v}×{k}" for k, v in sorted(ext_counts.items(), key=lambda x: -x[1])[:5])

            findings.append({
                "path": str(entry.relative_to(root)),
                "type": "uncatalogued",
                "file_count": len(files),
                "ext_summary": ext_summary,
            })
            _walk(entry, current_depth + 1)

    _walk(root, 1)

    if dry_run:
        return findings

    # Append Directory Map to context.md
    uncatalogued = [f for f in findings if f["type"] == "uncatalogued"]
    divisions = [f for f in findings if f["type"] == "swarm_division"]

    if uncatalogued or divisions:
        map_lines = [
            "",
            "## Directory Map",
            f"*Last crawled: {utcnow().strftime('%Y-%m-%dT%H:%MZ')}*",
            "",
        ]
        if divisions:
            map_lines.append("**Swarm divisions (skipped):**")
            for d in divisions:
                map_lines.append(f"- `{d['path']}/` — {d['note']}")
            map_lines.append("")
        if uncatalogued:
            map_lines.append("**Uncatalogued directories:**")
            for d in uncatalogued:
                summary = f"{d['file_count']} files" + (f" ({d['ext_summary']})" if d["ext_summary"] else "")
                map_lines.append(f"- `{d['path']}/` — {summary}")

        ctx_text = paths.context.read_text(encoding="utf-8") if paths.context.exists() else ""
        # Replace existing Directory Map section if present
        if "## Directory Map" in ctx_text:
            ctx_text = ctx_text[:ctx_text.index("## Directory Map")].rstrip()
        ctx_text += "\n" + "\n".join(map_lines) + "\n"
        _atomic_write(paths.context, ctx_text)

    # Optionally create OPEN queue items for each uncatalogued directory
    if create_items:
        division_code = _division_code_from_paths(paths)
        for d in uncatalogued:
            description = f"Document/review directory: {d['path']}/"
            add_item(paths, description, division_code=division_code,
                     priority=Priority.LOW, project="librarian",
                     notes=f"Crawled: {d['file_count']} files {d['ext_summary']}")

    return findings


# ---------------------------------------------------------------------------
# State operations
# ---------------------------------------------------------------------------

STATE_FIELD_RE = re.compile(r"^\*\*(?P<key>[^*]+)\*\*: (?P<value>.+)$")


def read_state(paths: SwarmPaths) -> dict[str, str]:
    """Parse state.md into a dict of field → value."""
    if not paths.state.exists():
        return {}
    result: dict[str, str] = {}
    handoff_lines: list[str] = []
    in_handoff = False
    for line in paths.state.read_text().splitlines():
        if line.strip() == "## Handoff Note":
            in_handoff = True
            continue
        if in_handoff:
            if line.startswith("## "):
                in_handoff = False
            else:
                handoff_lines.append(line)
            continue
        if m := STATE_FIELD_RE.match(line):
            result[m.group("key")] = m.group("value").strip()
    result["Handoff note"] = "\n".join(handoff_lines).strip()
    return result


def write_state(paths: SwarmPaths, updates: dict[str, str]) -> None:
    """Update specific fields in state.md, preserving all other content."""
    if not paths.state.exists():
        _create_state_template(paths)

    lines = paths.state.read_text().splitlines()
    now = _now_ts()
    updates.setdefault("Last touched", now)

    new_lines: list[str] = []
    in_handoff = False
    handoff_written = False

    for line in lines:
        if line.strip() == "## Handoff Note":
            in_handoff = True
            new_lines.append(line)
            if "Handoff note" in updates:
                new_lines.append("")
                new_lines.append(updates["Handoff note"])
                handoff_written = True
            continue
        if in_handoff:
            if line.startswith("## ") and not line.strip() == "## Handoff Note":
                in_handoff = False
                new_lines.append(line)
            elif not handoff_written:
                new_lines.append(line)
            continue

        if m := STATE_FIELD_RE.match(line):
            key = m.group("key")
            if key in updates:
                # Reconstruct the "last touched" line which includes "by <agent>"
                if key == "Last touched" and "last_agent" in updates:
                    new_lines.append(f"**Last touched**: {updates[key]} by {updates['last_agent']}")
                else:
                    new_lines.append(f"**{key}**: {updates[key]}")
                continue
        new_lines.append(line)

    _atomic_write(paths.state, "\n".join(new_lines) + "\n")


# ---------------------------------------------------------------------------
# Memory operations
# ---------------------------------------------------------------------------

def append_memory(
    paths: SwarmPaths,
    topic: str,
    decision: str,
    why: str,
    tradeoff: str = "",
    agent_id: str = "unknown",
) -> str:
    """Append a formatted entry to memory.md."""
    date = utcnow().strftime("%Y-%m-%d")
    entry = f"\n## {date} — {topic} ({agent_id})\n\n"
    entry += f"**Decision**: {decision}\n\n"
    entry += f"**Why**: {why}\n"
    if tradeoff:
        entry += f"\n**Trade-off accepted**: {tradeoff}\n"

    if paths.memory.exists():
        existing = paths.memory.read_text()
        _atomic_write(paths.memory, existing.rstrip() + "\n" + entry)
    else:
        _atomic_write(paths.memory, f"# Memory — {_division_name(paths)}\n\nAppend-only.\n" + entry)
    return entry


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def audit(paths: SwarmPaths, stale_hours: int = 48) -> list[dict]:
    """Return list of drift findings."""
    findings: list[dict] = []
    active, pending, done = read_queue(paths)
    now = utcnow()
    threshold = timedelta(hours=stale_hours)

    for item in active:
        if item.state == ItemState.CLAIMED and item.claimed_at:
            age = now - item.claimed_at
            if age > threshold:
                findings.append({
                    "severity": "WARN",
                    "type": "stale_claim",
                    "item_id": item.id,
                    "message": f"Claimed {int(age.total_seconds() / 3600)}h ago by {item.claimed_by}",
                    "suggested_action": "Re-evaluate or mark PARTIAL if still in progress.",
                })
        if item.state == ItemState.BLOCKED:
            findings.append({
                "severity": "WARN",
                "type": "blocked_item",
                "item_id": item.id,
                "message": f"Item is BLOCKED: {item.notes}",
                "suggested_action": "Escalate to org level or resolve blocker.",
            })

    # Check state.md freshness
    state = read_state(paths)
    if lt := state.get("Last touched"):
        ts_part = lt.split(" by ")[0].strip()
        if ts := _parse_ts(ts_part):
            age = now - ts
            if age > timedelta(hours=stale_hours * 1.5):
                findings.append({
                    "severity": "WARN",
                    "type": "stale_state",
                    "item_id": None,
                    "message": f"state.md not updated in {int(age.total_seconds() / 3600)}h",
                    "suggested_action": "Run 'swarm status' and update state.md.",
                })

    return findings


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _find_item(items: list[WorkItem], item_id: str) -> WorkItem | None:
    for item in items:
        if item.id == item_id:
            return item
    return None


def _atomic_write(path: Path, content: str) -> None:
    """Write content to path atomically using temp file + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_EX)
            f.write(content)
            if fcntl:
                fcntl.flock(f, fcntl.LOCK_UN)
        os.replace(tmp_path, path)
    except Exception:
        os.unlink(tmp_path)
        raise


def _division_name(paths: SwarmPaths) -> str:
    return paths.root.parent.name


def _level_label(paths: SwarmPaths) -> str:
    return "Organization Level" if paths.is_org_level() else "Division Level"


_DIVISION_CODE_MAP: dict[str, str] = {
    "oasis-x": "ORG",
    "oasis-cloud": "CLD",
    "oasis-cloud-admin": "ADM",
    "oasis-weather": "WTH",
    "oasis-firmware": "FW",
    "oasis-home": "HM",
    "oasis-ui": "UI",
    "oasis-forms": "FRM",
    "oasis-hardware": "HW",
    "oasis-welcome": "WEB",
    "oasis-cloud-wiki": "WIKI",
    "oasis-records": "REC",
    "dot-swarm": "SWC",
}


def _division_code_from_paths(paths: SwarmPaths) -> str:
    """Infer division code from directory name."""
    name = paths.root.parent.name
    return _DIVISION_CODE_MAP.get(name, name.upper()[:4])


def discover_divisions(root_path: Path, depth: int = 2) -> list[tuple[Path, SwarmPaths]]:
    """Recursively find all .swarm/ directories in the subtree."""
    divisions: list[tuple[Path, SwarmPaths]] = []

    # 1. Check root
    root_paths = SwarmPaths.find(root_path)
    if root_paths:
        divisions.append((root_path, root_paths))

    # 2. Check subdirectories up to depth
    patterns = ["*/.swarm"]
    if depth > 1:
        patterns.append("*/*/.swarm")
    if depth > 2:
        patterns.append("*/*/*/.swarm")

    for pattern in patterns:
        for p in root_path.glob(pattern):
            div_path = p.parent
            if div_path == root_path:
                continue
            paths = SwarmPaths.from_swarm_dir(p)
            if paths and (div_path, paths) not in divisions:
                divisions.append((div_path, paths))

    return sorted(divisions, key=lambda x: x[0].name)


def find_parent_paths(current_paths: SwarmPaths) -> SwarmPaths | None:
    """Find the next .swarm/ directory above the current one."""
    # Start looking from the parent of the current division root
    start_search = current_paths.root.parent.parent
    if not start_search or start_search == current_paths.root.parent:
        return None
    return SwarmPaths.find(start_search)


def get_alignment(
    local_paths: SwarmPaths, other_paths: SwarmPaths
) -> list[tuple[WorkItem, WorkItem]]:
    """Find pairs of items that reference each other across divisions."""
    local_active, local_pending, local_done = read_queue(local_paths)
    other_active, other_pending, other_done = read_queue(other_paths)

    local_items = local_active + local_pending + local_done
    other_items = other_active + other_pending + other_done

    local_code = _division_code_from_paths(local_paths)
    other_code = _division_code_from_paths(other_paths)

    aligned: list[tuple[WorkItem, WorkItem]] = []

    for l_item in local_items:
        # Check if local item refs other division
        for ref in l_item.refs + l_item.depends:
            if ref.startswith(other_code + "-"):
                # Find the target item in other division
                target = next((i for i in other_items if i.id == ref), None)
                if target:
                    aligned.append((l_item, target))

    for o_item in other_items:
        # Check if other item refs local division
        for ref in o_item.refs + o_item.depends:
            if ref.startswith(local_code + "-"):
                # Find the target item in local division
                target = next((i for i in local_items if i.id == ref), None)
                if target and (target, o_item) not in aligned:
                    aligned.append((target, o_item))

    return aligned


def get_git_history(path: Path, limit: int = 20) -> list[dict[str, Any]]:
    """Get recent git history for the .swarm/ directory at path."""
    swarm_dir = path / ".swarm"
    if not swarm_dir.exists():
        return []

    try:
        # Get last N commits affecting .swarm/
        cmd = [
            "git", "log", "-n", str(limit),
            "--pretty=format:%H|%at|%an|%s",
            "--", str(swarm_dir)
        ]
        result = subprocess.run(
            cmd,
            cwd=path,
            capture_output=True,
            text=True,
            check=True
        )
        history = []
        for line in result.stdout.splitlines():
            if not line:
                continue
            sha, ts, author, msg = line.split("|", 3)
            history.append({
                "sha": sha,
                "timestamp": datetime.fromtimestamp(int(ts)).isoformat() + "Z",
                "author": author,
                "message": msg
            })
        return history
    except (subprocess.SubprocessError, FileNotFoundError):
        return []


def get_colony_summary(root_path: Path) -> dict[str, Any]:
    """Aggregate all division data into a JSON-serializable dict."""
    divisions = discover_divisions(root_path, depth=3)
    data = {
        "root": str(root_path),
        "timestamp": _now_ts(),
        "divisions": []
    }

    for div_path, paths in divisions:
        try:
            state = read_state(paths)
            active, pending, done = read_queue(paths)
            history = get_git_history(div_path)

            div_data = {
                "name": div_path.name,
                "path": str(div_path),
                "is_org": paths.is_org_level(),
                "state": state,
                "queue": {
                    "active": [i.__dict__ for i in active],
                    "pending": [i.__dict__ for i in pending],
                    "done": [i.__dict__ for i in done],
                },
                "history": history
            }
            # Clean up WorkItem dicts for JSON (enums to strings)
            for section in ["active", "pending", "done"]:
                for item in div_data["queue"][section]:
                    item["state"] = item["state"].value
                    item["priority"] = item["priority"].value
                    if item["claimed_at"]:
                        item["claimed_at"] = _fmt_ts(item["claimed_at"])
                    if item["done_at"]:
                        item["done_at"] = _fmt_ts(item["done_at"])

            data["divisions"].append(div_data)
        except Exception as e:
            data["divisions"].append({
                "name": div_path.name,
                "error": str(e)
            })

    return data


def _create_state_template(paths: SwarmPaths) -> None:
    name = _division_name(paths)
    content = f"""# State — {name}

**Last touched**: {_now_ts()} by unknown
**Current focus**: (not set)
**Active items**: (none)
**Blockers**: None
**Ready for pickup**: (none)

---

## Handoff Note

(no handoff note yet)
"""
    _atomic_write(paths.state, content)
