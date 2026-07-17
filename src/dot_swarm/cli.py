"""dot_swarm CLI — `swarm` command."""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import click

from .models import ItemState, Priority, SwarmPaths, utcnow
from .operations import (
    add_item, append_memory, audit, block_item, claim_item, crawl_directory,
    done_item, next_item_id, partial_item, ready_items, reopen_item,
    read_queue, read_state, write_queue, write_state, _division_code_from_paths,
    discover_divisions, find_parent_paths, get_alignment, get_colony_summary,
)


def _get_paths(path: str) -> SwarmPaths:
    paths = SwarmPaths.find(path)
    if paths is None:
        click.echo(
            "Error: No .swarm/ directory found. Run 'swarm init' first.", err=True
        )
        sys.exit(1)
    return paths


def _default_agent() -> str:
    return os.environ.get("SWARM_AGENT_ID") or f"human-{os.environ.get('USER', 'unknown')}"


# ---------------------------------------------------------------------------
# CLI root
# ---------------------------------------------------------------------------

@click.group()
@click.option("--path", default=".", help="Path to operate on (default: cwd)")
@click.version_option("1.0.0")
@click.pass_context
def cli(ctx: click.Context, path: str) -> None:
    """dot_swarm — markdown-native agent orchestration.

    Reads and writes .swarm/ directories in the current working directory (or
    specified --path). All state lives in plain markdown files — git is the
    audit trail.

    Quick start:
      swarm init        Initialize .swarm/ here
      swarm status      Show current state
      swarm claim ID    Claim a work item
      swarm done ID     Mark item complete
      swarm ascend      Check alignment with parent division
      swarm descend     Check alignment with sub-divisions
    """
    ctx.ensure_object(dict)
    ctx.obj["path"] = path


# ---------------------------------------------------------------------------
# swarm init
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--level", type=click.Choice(["org", "div"]), default=None,
              help="Override level detection (org|div)")
@click.option("--division-code", default=None, help="Division code e.g. CLD, FW")
@click.option("--division-name", default=None, help="Human-readable division name")
@click.option("--visible", is_flag=True, default=False,
              help="Make .swarm/ visible in git (default: invisible)")
@click.option("--crawl", "run_crawl", is_flag=True, default=False,
              help="Run 'swarm crawl' immediately after init to populate context.md")
@click.pass_context
def init(ctx: click.Context, level: str | None, division_code: str | None,
         division_name: str | None, visible: bool, run_crawl: bool) -> None:
    """Initialize .swarm/ in the current directory.

    Creates BOOTSTRAP.md, context.md, state.md, queue.md, memory.md
    with appropriate templates. At org level, also creates workflows/.

    At division level, also creates platform shims (CLAUDE.md, .windsurfrules,
    .cursorrules) if they don't already exist.
    """
    p = Path(ctx.obj["path"]).resolve()
    swarm_dir = p / ".swarm"

    # Detect level
    is_div = (p / ".git").exists()
    if level == "org":
        is_div = False
    elif level == "div":
        is_div = True

    level_str = "division" if is_div else "org"
    click.echo(f"Initializing .swarm/ at {level_str} level in {p}")

    swarm_dir.mkdir(exist_ok=True)
    code = division_code or _division_code_from_paths(
        SwarmPaths.from_swarm_dir(swarm_dir)
    )
    name = division_name or p.name

    # BOOTSTRAP.md
    _create_if_missing(swarm_dir / "BOOTSTRAP.md",
        f"# dot_swarm Bootstrap — {name}\n\n"
        "See `oasis-x/.swarm/BOOTSTRAP.md` for the full protocol.\n\n"
        "Quick reference:\n"
        "1. Read context.md → state.md → queue.md\n"
        "2. Claim an OPEN item\n"
        "3. Update state.md on start and finish\n"
    )

    # context.md
    _create_if_missing(swarm_dir / "context.md",
        f"# Context — {name}\n\n"
        f"**Level**: {'Organization' if not is_div else 'Division'}\n"
        f"**Division code**: {code}\n"
        f"**Last updated**: {utcnow().strftime('%Y-%m-%d')}\n\n"
        "## What This Division Is\n\n"
        "(fill in)\n\n"
        "## Architecture Constraints\n\n"
        "1. (fill in)\n\n"
        "## Current Focus Areas\n\n"
        "1. (fill in)\n"
    )

    # state.md
    _create_if_missing(swarm_dir / "state.md",
        f"# State — {name}\n\n"
        f"**Last touched**: {utcnow().strftime('%Y-%m-%dT%H:%MZ')} by unknown\n"
        "**Current focus**: (not set)\n"
        "**Active items**: (none)\n"
        "**Blockers**: None\n"
        "**Ready for pickup**: (none)\n\n"
        "---\n\n"
        "## Handoff Note\n\n"
        "(no handoff note yet)\n"
    )

    # queue.md
    _create_if_missing(swarm_dir / "queue.md",
        f"# Queue — {name} ({'Organization' if not is_div else 'Division'} Level)\n\n"
        "Items are listed in priority order within each section.\n"
        f"Item IDs: `{code}-<3-digit-number>` — assigned sequentially, never reused.\n\n"
        "---\n\n"
        "## Active\n\n"
        "(no active items)\n\n"
        "## Pending\n\n"
        f"- [ ] [{code}-001] [OPEN] First work item (replace this)\n"
        "      priority: medium | project: misc\n\n"
        "## Done\n\n"
        "(none yet)\n"
    )

    # memory.md
    _create_if_missing(swarm_dir / "memory.md",
        f"# Memory — {name}\n\n"
        "Append-only. Non-obvious decisions, constraints, and rationale.\n"
        "Format: `## <ISO8601-date> — <topic> (<agent-id>)`\n\n"
        "---\n\n"
        "(no entries yet)\n"
    )

    if not is_div:
        (swarm_dir / "workflows").mkdir(exist_ok=True)

    # Append-only claim trail directory (created up front so all
    # lifecycle operations append rather than fall back to queue.md).
    (swarm_dir / "claims").mkdir(exist_ok=True)

    # Generate signing identity (idempotent — skipped if already exists)
    from . import signing as _sign
    _sign.generate_identity(swarm_dir)
    _ensure_gitignore(swarm_dir)

    click.echo(f"Created .swarm/ with {len(list(swarm_dir.iterdir()))} files/dirs.")

    # Trail visibility (default: invisible)
    gi = _repo_gitignore(p)
    if gi is not None and not visible:
        result = _set_trail_visibility(gi, invisible=True)
        click.echo(f"Trail: {result}  (use 'swarm trail visible' to share)")
    elif gi is not None and visible:
        click.echo("Trail: visible — .swarm/ will be committed with the repo.")

    if is_div:
        _create_platform_shims(p)
        _install_drift_check_workflow(p)

    if run_crawl:
        click.echo("\nRunning swarm crawl...")
        paths = SwarmPaths.find(str(p))
        if paths:
            findings = crawl_directory(paths, p, depth=3, create_items=False, dry_run=False)
            uncatalogued = [f for f in findings if f["type"] == "uncatalogued"]
            divisions = [f for f in findings if f["type"] == "swarm_division"]
            if divisions:
                click.echo(f"  Swarm divisions found ({len(divisions)}) — skipped")
            if uncatalogued:
                click.echo(f"  Catalogued {len(uncatalogued)} dirs → context.md")
            else:
                click.echo("  No uncatalogued directories found.")

    click.echo("\nNext steps:")
    click.echo(f"  1. Edit .swarm/context.md — describe what {name} is")
    click.echo("  2. Run 'swarm crawl' to catalogue subdirectories into context.md")
    click.echo("  3. Run 'swarm status' to verify")
    click.echo("  4. Run 'swarm add \"first task\"' to add a work item")
    if is_div:
        click.echo("  5. Add GEMINI_API_KEY to GitHub secrets to enable drift checks")


# ---------------------------------------------------------------------------
# swarm migrate — bring legacy .swarm/ directories up to the v1.0 layout
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--dry-run", is_flag=True, default=False,
              help="Report what would change without writing anything")
@click.option("--all", "all_divisions", is_flag=True, default=False,
              help="Migrate every .swarm/ found under the current path")
@click.option("--depth", default=3, show_default=True,
              help="Discovery depth when used with --all")
@click.pass_context
def migrate(ctx: click.Context, dry_run: bool, all_divisions: bool, depth: int) -> None:
    """Bring a legacy .swarm/ directory up to the v1.0 protocol layout.

    Idempotent and content-preserving. Creates missing structural pieces
    that v1.0 expects:

    \b
      claims/                          — append-only claim trail (SWC-033)
      federation/strangers/[rejected/] — untrusted message bay
      .gitignore                       — .swarm_key / .swarm_key.old entries

    Also backfills a synthetic claim record for any item currently
    marked CLAIMED in queue.md that has no record in claims/, so the
    resolver renders the same state before and after.

    \b
    swarm migrate                 # one division (cwd)
    swarm migrate --dry-run       # preview without writing
    swarm migrate --all           # every .swarm/ under cwd
    """
    from . import migrate as _mig

    root = Path(ctx.obj["path"]).resolve()
    if all_divisions:
        reports = _mig.migrate_tree(root, dry_run=dry_run, depth=depth)
        if not reports:
            click.echo(f"No .swarm/ directories found under {root}.")
            return
    else:
        paths = _get_paths(ctx.obj["path"])
        reports = [_mig.migrate_swarm(paths.root, dry_run=dry_run)]

    label = "Would apply" if dry_run else "Applied"
    total_actions = total_needed = 0
    for r in reports:
        if r.already_current:
            click.echo(f"  ✓ {r.swarm_path} — already at v1.0 layout.")
            continue
        click.echo(f"\n  {r.swarm_path}")
        for line in (r.needed if dry_run else r.actions):
            click.echo(f"    · {line}")
        total_actions += len(r.actions)
        total_needed += len(r.needed)

    click.echo("")
    if dry_run:
        click.echo(f"Dry run: {total_needed} change(s) would be made across "
                   f"{len(reports)} division(s). Re-run without --dry-run to apply.")
    else:
        click.echo(f"{label} {total_actions} change(s) across {len(reports)} division(s).")


# ---------------------------------------------------------------------------
# swarm status
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--all", "show_all", is_flag=True, help="Show full queue")
@click.pass_context
def status(ctx: click.Context, show_all: bool) -> None:
    """Show current state and next available work items."""
    paths = _get_paths(ctx.obj["path"])
    state = read_state(paths)
    active, pending, done = read_queue(paths)

    name = paths.root.parent.name
    click.echo(f"\n{'─' * 60}")
    click.echo(f"  {name}")
    click.echo(f"{'─' * 60}")

    for key in ["Last touched", "Current focus", "Active items", "Blockers"]:
        val = state.get(key, "(not set)")
        click.echo(f"  {key}: {val}")

    if state.get("Handoff note"):
        click.echo(f"\n  Handoff: {state['Handoff note'][:120]}")

    click.echo(f"\n  Active ({len(active)}):")
    for item in active[:5]:
        click.echo(f"    [{item.id}] {item.description[:60]}  [{item.state.value}]")

    click.echo(f"\n  Pending ({len(pending)}):")
    for item in (pending if show_all else pending[:5]):
        click.echo(f"    [{item.id}] [{item.priority.value.upper()}] {item.description[:60]}")
    if not show_all and len(pending) > 5:
        click.echo(f"    ... and {len(pending) - 5} more (use --all)")

    click.echo(f"\n  Done: {len(done)} items\n")


# ---------------------------------------------------------------------------
# swarm ready  (like `bd ready` — dependency-aware work discovery)
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON array")
@click.pass_context
def ready(ctx: click.Context, as_json: bool) -> None:
    """List OPEN items with all dependencies satisfied — safe to pick up now.

    Equivalent to `bd ready` in the Beads/Gastown ecosystem: only items whose
    entire depends: chain is in the Done section are shown.

    Examples:
      swarm ready            # human-readable list
      swarm ready --json     # machine-readable for agent scripts
    """
    import json as _json
    paths = _get_paths(ctx.obj["path"])
    items = ready_items(paths)
    if as_json:
        click.echo(_json.dumps([
            {"id": i.id, "description": i.description,
             "priority": i.priority.value, "project": i.project}
            for i in items
        ], indent=2))
        return
    if not items:
        click.echo("No items ready — all open items have unresolved dependencies.")
        return
    click.echo(f"Ready for pickup ({len(items)}):")
    for item in items:
        dep_str = f"  depends: {', '.join(item.depends)}" if item.depends else ""
        click.echo(
            f"  [{item.id}] [{item.priority.value.upper()}] {item.description}"
            + (f"\n{dep_str}" if dep_str else "")
        )


# ---------------------------------------------------------------------------
# swarm claim
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("item_id")
@click.option("--agent", default=None, help="Agent ID (default: $SWARM_AGENT_ID or human-$USER)")
@click.option("--compete", is_flag=True, help="Claim an already claimed item for a competing implementation")
@click.pass_context
def claim(ctx: click.Context, item_id: str, agent: str | None, compete: bool) -> None:
    """Claim a work item. Updates queue.md and state.md."""
    paths = _get_paths(ctx.obj["path"])
    agent_id = agent or _default_agent()
    try:
        item = claim_item(paths, item_id, agent_id, compete=compete)
        write_state(paths, {
            "Current focus": item.description[:100],
            "Active items": item_id,
            "last_agent": agent_id,
        })
        if item.state == ItemState.COMPETING:
            click.echo(f"COMPETING claim recorded for [{item_id}] by {agent_id}.")
        else:
            click.echo(f"Claimed [{item_id}] for {agent_id}: {item.description}")
        click.echo("Remember to update state.md and run 'swarm done' when complete.")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# swarm review
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("item_id")
@click.option("--agent", default=None, help="Agent ID (default: $SWARM_AGENT_ID or human-$USER)")
@click.pass_context
def review(ctx: click.Context, item_id: str, agent: str | None) -> None:
    """Move a COMPETING item into REVIEW state."""
    paths = _get_paths(ctx.obj["path"])
    agent_id = agent or _default_agent()
    active, pending, done = read_queue(paths)
    target = next((i for i in active + pending if i.id == item_id), None)
    if not target:
        click.echo(f"Error: Item {item_id} not found.", err=True)
        sys.exit(1)
    
    target.state = ItemState.REVIEW
    target.claimed_by = agent_id
    target.claimed_at = utcnow()
    
    write_queue(paths, active, pending, done)
    click.echo(f"Item [{item_id}] is now in REVIEW by {agent_id}.")

# ---------------------------------------------------------------------------
# swarm done
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("item_id")
@click.option("--agent", default=None)
@click.option("--note", default="", help="Brief completion note")
@click.option("--next", "next_focus", default=None, help="Set state.md next focus")
@click.option("--force", is_flag=True, default=False,
              help="Bypass Inspector proof requirement (human override)")
@click.pass_context
def done(ctx: click.Context, item_id: str, agent: str | None, note: str,
         next_focus: str | None, force: bool) -> None:
    """Mark a work item as done.

    When the Inspector role is enabled, workers cannot mark items done directly —
    they must use 'swarm partial --proof ...' and let the inspector verify via
    'swarm inspect --pass'. Use --force to override as a human director.
    """
    paths = _get_paths(ctx.obj["path"])
    agent_id = agent or _default_agent()

    # Inspector gate: workers must go through swarm inspect, not swarm done
    if not force:
        from . import roles as _roles
        if _roles.is_role_enabled(paths, "inspector"):
            active, pending, _ = read_queue(paths)
            target = next((i for i in active + pending if i.id == item_id), None)
            if target:
                role = _roles.load_role(paths, "inspector")
                missing = _roles.validate_proof(target.proof, role.require_proof_fields)
                if missing:
                    click.echo(
                        f"Inspector role is enabled — '{item_id}' requires proof before done.\n"
                        f"  Missing fields: {', '.join(missing)}\n"
                        f"  Run: swarm partial {item_id} --proof "
                        f"\"branch:<name> commit:<sha> tests:<N/N>\"\n"
                        f"  Then an inspector agent runs: swarm inspect {item_id} --pass\n"
                        f"  Or bypass with: swarm done {item_id} --force",
                        err=True,
                    )
                    sys.exit(1)

    try:
        item = done_item(paths, item_id, agent_id, note)
        updates: dict = {"last_agent": agent_id}
        if next_focus:
            updates["Current focus"] = next_focus
            updates["Handoff note"] = next_focus
        write_state(paths, updates)
        click.echo(f"Done: [{item_id}] {item.description}")
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# swarm add
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("description")
@click.option("--priority", type=click.Choice(["critical", "high", "medium", "low"]),
              default="medium")
@click.option("--project", default="misc")
@click.option("--notes", default="")
@click.option("--refs", default="", help="Comma-separated refs e.g. 'oasis-x/.swarm/queue.md#ORG-001'")
@click.option("--depends", default="", help="Comma-separated item IDs")
@click.option("--code", default=None, help="Division code override")
@click.pass_context
def add(ctx: click.Context, description: str, priority: str, project: str,
        notes: str, refs: str, depends: str, code: str | None) -> None:
    """Add a new work item to the queue."""
    paths = _get_paths(ctx.obj["path"])
    division_code = code or _division_code_from_paths(paths)
    item = add_item(
        paths=paths,
        description=description,
        division_code=division_code,
        priority=Priority(priority),
        project=project,
        notes=notes,
        refs=[r.strip() for r in refs.split(",") if r.strip()],
        depends=[d.strip() for d in depends.split(",") if d.strip()],
    )
    click.echo(f"Added [{item.id}] ({priority}) {description}")


# ---------------------------------------------------------------------------
# swarm audit
# ---------------------------------------------------------------------------

@cli.command(name="audit")
@click.option("--since", default=48, help="Stale threshold in hours (default: 48)")
@click.option("--pending", "show_pending", is_flag=True, default=False,
              help="List all pending items")
@click.option("--security", "run_security", is_flag=True, default=False,
              help="Run adversarial/injection content scan")
@click.option("--drift", "run_drift", is_flag=True, default=False,
              help="Run AI code-vs-docs drift check (requires LLM backend)")
@click.option("--trail", "check_trail", is_flag=True, default=False,
              help="Verify pheromone trail HMAC signatures")
@click.option("--full", is_flag=True, default=False,
              help="Run all checks (--pending --security --drift --trail)")
@click.pass_context
def audit_cmd(
    ctx: click.Context,
    since: int,
    show_pending: bool,
    run_security: bool,
    run_drift: bool,
    check_trail: bool,
    full: bool,
) -> None:
    """Check for drift: stale claims, blocked items, and queue health.

    With --full, also runs security scan, AI drift check, and trail
    signature verification.

    Examples:
      swarm audit                # Basic stale/blocked check
      swarm audit --pending      # Include all pending items
      swarm audit --security     # Add adversarial content scan
      swarm audit --drift        # Add AI code-vs-docs drift check
      swarm audit --trail        # Verify pheromone trail integrity
      swarm audit --full         # All of the above
    """
    if full:
        show_pending = run_security = run_drift = check_trail = True

    paths = _get_paths(ctx.obj["path"])

    # --- Basic queue audit ---------------------------------------------------
    findings = audit(paths, stale_hours=since)
    if not findings:
        click.echo("✓ No stale claims or state drift.")
    else:
        for f in findings:
            icon = "⚠️ " if f["severity"] == "WARN" else "🚨"
            id_str = f"[{f['item_id']}] " if f["item_id"] else ""
            click.echo(f"{icon} {id_str}{f['message']}")
            click.echo(f"   → {f['suggested_action']}")

    # --- Blockers (always shown) ---------------------------------------------
    active, pending, _ = read_queue(paths)
    blocked = [i for i in active if i.state == ItemState.BLOCKED]
    if blocked:
        click.echo(f"\nBlockers ({len(blocked)}):")
        for item in blocked:
            click.echo(f"  🚫 {item.id}: {item.notes}")

    # --- Pending items -------------------------------------------------------
    if show_pending:
        click.echo(f"\nPending Items ({len(pending)}):")
        if pending:
            for item in pending:
                click.echo(
                    f"  [ ] {item.id}: {item.description[:60]}"
                    f"  [{item.priority.value.upper()}]"
                )
        else:
            click.echo("  (none)")

    # --- Security scan -------------------------------------------------------
    if run_security:
        from . import security as _sec
        div_root = paths.root.parent
        sec_findings = _sec.scan_swarm_directory(paths) + _sec.scan_platform_shims(div_root)
        counts = _sec.severity_counts(sec_findings)
        click.echo(
            f"\nSecurity Scan — "
            f"🚨 {counts['CRITICAL']} critical  "
            f"⚠️  {counts['HIGH']} high  "
            f"ℹ️  {counts['MEDIUM']} medium"
        )
        for line in _sec.format_findings(sec_findings):
            click.echo(line)

    # --- Trail integrity -----------------------------------------------------
    if check_trail:
        from . import signing as _sign
        click.echo("\nPheromone Trail:")
        identity = _sign.load_identity(paths.root)
        if not identity:
            click.echo("  ℹ  No signing identity found — run 'swarm init' to enable.")
        else:
            tampered = _sign.verify_trail(paths.root)
            trail_len = len(_sign.read_trail(paths.root))
            if tampered:
                click.echo(f"  🚨 {len(tampered)} tampered entry(ies) detected!")
                for rec in tampered[:5]:
                    click.echo(f"     Agent: {rec['agent_id']}  Op: {rec['op']}  At: {rec['timestamp']}")
            else:
                click.echo(f"  ✓ Trail verified ({trail_len} entries, all valid).")

    # --- AI drift check ------------------------------------------------------
    if run_drift:
        click.echo("\nDrift Check:")
        _run_local_drift_check(ctx, paths)


# ---------------------------------------------------------------------------
# swarm handoff
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--format", "fmt", type=click.Choice(["md", "text"]), default="md")
@click.pass_context
def handoff(ctx: click.Context, fmt: str) -> None:
    """Generate a handoff summary for the next agent or developer."""
    paths = _get_paths(ctx.obj["path"])
    state = read_state(paths)
    active, pending, done = read_queue(paths)
    name = paths.root.parent.name
    now = utcnow().strftime("%Y-%m-%dT%H:%MZ")

    lines = [
        f"# dot_swarm Handoff — {name} — {now}",
        "",
        "## Current State",
        f"Focus: {state.get('Current focus', '(not set)')}",
        f"Agent: {state.get('Last touched', 'unknown').split(' by ')[-1]}",
    ]
    if active:
        lines.append(f"Active: {', '.join(i.id for i in active)}")
    if state.get("Blockers") and state["Blockers"] != "None":
        lines.append(f"Blockers: {state['Blockers']}")
    if state.get("Handoff note"):
        lines.append(f"\n{state['Handoff note']}")

    lines += ["", "## Ready for Pickup"]
    for item in pending[:5]:
        lines.append(f"- {item.id}: {item.description} [{item.priority.value.upper()}]")

    lines += [
        "",
        "## Context Files to Load",
        f"- @{paths.root}/BOOTSTRAP.md",
        f"- @{paths.root}/context.md",
        f"- @{paths.root}/queue.md",
    ]

    click.echo("\n".join(lines))


# ---------------------------------------------------------------------------
# swarm explore
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--depth", default=2, help="Directory depth for division discovery (default: 2)")
@click.pass_context
def explore(ctx: click.Context, depth: int) -> None:
    """Show the heartbeat of all divisions in the colony.

    Recursively finds .swarm/ directories starting from the current directory
    (or --path) and displays their current focus and health.
    """
    root_path = Path(ctx.obj["path"]).resolve()
    click.echo(f"\nExploring colony heartbeat in: {root_path}\n")

    divisions = discover_divisions(root_path, depth=depth)

    if not divisions:
        click.echo("No .swarm/ directories found in this subtree.")
        return

    # Header
    click.echo(f"{'Division':20} | {'C':1} | {'Last Touched':18} | {'Current Focus'}")
    click.echo(f"{'─' * 20}─┼─{'─'}─┼─{'─' * 18}─┼─{'─' * 40}")

    for path, paths in divisions:
        try:
            state = read_state(paths)
            active, pending, _ = read_queue(paths)

            name = path.name
            if paths.is_org_level():
                name = f"★ {name}"

            lt = state.get("Last touched", "unknown")
            lt_short = lt.split(" by ")[0].replace("T", " ").split(".")[0][:16]

            focus = state.get("Current focus", "(not set)")
            if len(focus) > 50:
                focus = focus[:47] + "..."

            # Crawl coverage: context.md contains a Directory Map?
            crawled = "✓" if paths.context.exists() and "## Directory Map" in paths.context.read_text() else "·"

            count = f"({len(active)} active, {len(pending)} pending)"

            click.echo(f"{name:20} | {crawled} | {lt_short:18} | {focus}")
            if active or pending:
                click.echo(f"{' ':20} | {' ':1} | {' ':18} |   └─ {count}")

        except Exception as e:
            click.echo(f"{path.name:20} | ? | Error: {str(e)[:50]}")

    # Detect uninitialised sibling git repos
    uninitialised = [
        d for d in root_path.iterdir()
        if d.is_dir() and (d / ".git").is_dir() and not (d / ".swarm").is_dir()
    ]
    if uninitialised:
        click.echo(f"\n  ⚠  Uninitialised repos ({len(uninitialised)}) — no .swarm/ found:")
        for d in uninitialised:
            click.echo(f"    {d.name}/  →  cd {d.name} && swarm init")

    click.echo(f"\nFound {len(divisions)} divisions.  C = crawled (swarm crawl run)")
    click.echo("Run 'swarm status --path <division>' for deep dive into any node.\n")


# ---------------------------------------------------------------------------
# swarm ascend / descend (alignment)
# ---------------------------------------------------------------------------

@cli.command(name="ascend")
@click.pass_context
def ascend(ctx: click.Context) -> None:
    """Check alignment with the parent division."""
    local_paths = _get_paths(ctx.obj["path"])
    parent_paths = find_parent_paths(local_paths)

    if not parent_paths:
        click.echo("No parent division found above this directory.")
        return

    local_name = local_paths.root.parent.name
    parent_name = parent_paths.root.parent.name
    click.echo(f"\nChecking alignment: {local_name} → {parent_name}\n")

    alignment = get_alignment(local_paths, parent_paths)

    if not alignment:
        click.echo("No explicit work item relations found with parent division.")
    else:
        click.echo(f"{'Local Item':20} | {'Relationship':15} | {'Parent Item'}")
        click.echo(f"{'─' * 20}─┼─{'─' * 15}─┼─{'─' * 40}")
        for local, parent in alignment:
            rel = "depends on" if parent.id in local.depends else "references"
            click.echo(f"{local.id:20} | {rel:15} | {parent.id}: {parent.description[:35]}")

    click.echo("")


@cli.command(name="descend")
@click.pass_context
def descend(ctx: click.Context) -> None:
    """Check alignment with sub-divisions."""
    local_paths = _get_paths(ctx.obj["path"])
    root_path = local_paths.root.parent
    
    # Depth 1 to see immediate children
    divisions = discover_divisions(root_path, depth=1)
    # Filter out local_paths itself
    children = [(p, ps) for p, ps in divisions if p != root_path]

    if not children:
        click.echo("No sub-divisions found in immediate subdirectories.")
        return

    click.echo(f"\nChecking alignment: {root_path.name} ↴ {len(children)} children\n")

    found_any = False
    for child_path, child_paths in children:
        alignment = get_alignment(local_paths, child_paths)
        if alignment:
            found_any = True
            click.echo(f"Sub-division: {child_path.name}")
            click.echo(f"{'Local Item':20} | {'Relationship':15} | {'Child Item'}")
            click.echo(f"{'─' * 20}─┼─{'─' * 15}─┼─{'─' * 40}")
            for local, child in alignment:
                rel = "referenced by" if local.id in child.refs else "parent of"
                if local.id in child.depends:
                    rel = "required by"
                click.echo(f"{local.id:20} | {rel:15} | {child.id}: {child.description[:35]}")
            click.echo("")

    if not found_any:
        click.echo("No explicit work item relations found with sub-divisions.")
        click.echo("Sub-divisions present: " + ", ".join(p.name for p, _ in children))

    click.echo("")


# ---------------------------------------------------------------------------
# swarm heal
# ---------------------------------------------------------------------------

@cli.command(name="heal")
@click.option("--fix", is_flag=True, default=False,
              help="Quarantine adversarial content and block tampered trail signers")
@click.option("--depth", default=1,
              help="Child-division scan depth for descend alignment (default: 1)")
@click.pass_context
def heal(ctx: click.Context, fix: bool, depth: int) -> None:
    """Full health pass: alignment, security scan, and trail verification.

    Runs ascend + descend alignment checks, scans .swarm/ files and
    platform shims for adversarial content (prompt injections, hidden
    instructions, non-disclosure directives), and verifies the
    cryptographic integrity of the pheromone trail.

    Any security findings are logged to memory.md and printed at the
    end of the pass so they are never silently swallowed.

    With --fix:
      - Backs up flagged files to .swarm/quarantine/ for human review
      - Blocks the HMAC fingerprints of tampered trail entries

    Examples:
      swarm heal              # Read-only health check
      swarm heal --fix        # Attempt automatic remediation
      swarm heal --depth 2    # Descend two levels into child divisions
    """
    from . import ai_ops as _ai_ops
    from . import security as _sec

    paths    = _get_paths(ctx.obj["path"])
    div_name = paths.root.parent.name

    click.echo(f"\n⟳  Healing {div_name}…\n")

    # SWC-047: heal's actual logic lives in ai_ops.heal() (a pure function)
    # so the CLI and the MCP `swarm_heal` tool share ONE implementation —
    # this command just formats the result the way it always has.
    result = _ai_ops.heal(paths, fix=fix, depth=depth)

    # ── 1. Alignment ────────────────────────────────────────────────────────
    click.echo("── Alignment ──────────────────────────────────────────")
    al = result["alignment"]
    if al["parent"]:
        if al["parent_links"]:
            click.echo(f"  ↑ {al['parent_links']} link(s) → parent: {al['parent']}")
        else:
            click.echo(f"  ↑ No explicit links to parent ({al['parent']})")
    else:
        click.echo("  ↑ No parent division found.")

    if al["children"]:
        click.echo(f"  ↓ {al['child_links']} link(s) across {al['children']} child division(s)")
        if al["orphan_items"]:
            click.echo(f"  ℹ  {len(al['orphan_items'])} orphaned item(s) (no cross-division links):")
            for oid in al["orphan_items"][:5]:
                click.echo(f"     {oid}")
    else:
        click.echo("  ↓ No child divisions found.")

    # ── 2. Queue health ─────────────────────────────────────────────────────
    click.echo("\n── Queue Health ────────────────────────────────────────")
    queue_findings = result["queue_health"]["findings"]
    if queue_findings:
        for f in queue_findings:
            icon = "⚠️ " if f["severity"] == "WARN" else "🚨"
            id_str = f"[{f['item_id']}] " if f["item_id"] else ""
            click.echo(f"  {icon} {id_str}{f['message']}")
    else:
        click.echo("  ✓ No stale claims or blocked items.")
    if result["queue_health"]["pending_count"]:
        click.echo(f"  ℹ  {result['queue_health']['pending_count']} pending item(s) awaiting pickup.")

    # ── 3. Security scan ────────────────────────────────────────────────────
    click.echo("\n── Security Scan ───────────────────────────────────────")
    sec = result["security"]
    counts = sec["counts"]
    if sec["findings"]:
        click.echo(
            f"  🚨 {counts['CRITICAL']} critical  "
            f"⚠️  {counts['HIGH']} high  "
            f"ℹ️  {counts['MEDIUM']} medium"
        )
        findings_objs = [_sec.SecurityFinding(**f) for f in sec["findings"]]
        for line in _sec.format_findings(findings_objs):
            click.echo(line)
        if fix and sec["quarantined"]:
            click.echo("\n  [--fix] Backing up flagged files to .swarm/quarantine/…")
            for msg in sec["quarantined"]:
                click.echo(f"     {msg}")
            click.echo("  ⚠  Flagged files backed up. Review quarantine/ and remove injections manually.")
            click.echo("     Run 'swarm heal' again after cleaning to confirm resolution.")
    else:
        click.echo("  ✓ No adversarial content detected.")

    # ── 4. Pheromone trail integrity ─────────────────────────────────────────
    click.echo("\n── Pheromone Trail Integrity ───────────────────────────")
    trail = result["trail"]
    if not trail["has_identity"]:
        click.echo("  ℹ  No signing identity. Run 'swarm init' to enable trail signing.")
    elif trail["tampered"]:
        click.echo(f"  🚨 {len(trail['tampered'])} tampered trail entry(ies) detected!")
        for rec in trail["tampered"][:5]:
            click.echo(
                f"     Fingerprint: {rec.get('fingerprint','?')}  "
                f"Agent: {rec['agent_id']}  Op: {rec['op']}"
            )
        for fp in trail["blocked_fingerprints"]:
            click.echo(f"  🔒 Blocked fingerprint: {fp}")
    else:
        click.echo(f"  ✓ Trail verified ({trail['length']} entries, all signatures valid).")

    # ── 5. Summary ───────────────────────────────────────────────────────────
    click.echo("\n── Summary ─────────────────────────────────────────────")
    if result["healthy"]:
        click.echo(f"  ✓ {div_name} is healthy.\n")
    else:
        click.echo(f"  {result['total_issues']} issue(s) found.")
        if sec["findings"] and not fix:
            click.echo("  Run 'swarm heal --fix' to quarantine adversarial content.\n")

    if result["memory_logged"]:
        click.echo(f"  Findings logged to memory.md.")

    click.echo("")


# ---------------------------------------------------------------------------
# swarm federation
# ---------------------------------------------------------------------------

@cli.group(name="federation")
@click.pass_context
def federation_group(ctx: click.Context) -> None:
    """OGP-lite cross-swarm federation commands.

    Federation lets separate .swarm/ hierarchies exchange work items and
    alignment signals using signed intent messages. Trust is bilateral and
    explicit — no central registry.

    Typical flow:
      swarm federation init                  # create federation dirs
      swarm federation export-id             # share identity.json with peer
      swarm federation trust peer_id.json    # import peer's identity
      swarm federation peers                 # confirm trust established
      swarm federation send <fp> work_request --desc "Help with X"
      swarm federation inbox                 # check received messages
      swarm federation apply inbox/msg.json  # apply message to queue
    """


@federation_group.command(name="init")
@click.pass_context
def federation_init(ctx: click.Context) -> None:
    """Create federation/ directory structure inside .swarm/."""
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    _fed.init_federation(paths.root)
    click.echo(f"✓ Federation directories initialised at {paths.root}/federation/")
    click.echo("  Share your identity with peers: swarm federation export-id")


@federation_group.command(name="export-id")
@click.option("--out", default=None, metavar="FILE",
              help="Write identity to file (default: print to stdout)")
@click.pass_context
def federation_export_id(ctx: click.Context, out: str | None) -> None:
    """Print this swarm's public identity (safe to share with peers)."""
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    try:
        identity = _fed.export_identity(paths.root)
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    text = __import__("json").dumps(identity, indent=2)
    if out:
        Path(out).write_text(text)
        click.echo(f"✓ Identity written to {out}")
    else:
        click.echo(text)


@federation_group.command(name="trust")
@click.argument("peer_identity_file", metavar="PEER_IDENTITY_JSON")
@click.option("--name", default="", metavar="NAME",
              help="Human-readable name for this peer")
@click.option("--scopes", default="work_request,alignment_signal", metavar="SCOPES",
              help="Comma-separated list of permitted intents (default: work_request,alignment_signal)")
@click.pass_context
def federation_trust(
    ctx: click.Context, peer_identity_file: str, name: str, scopes: str
) -> None:
    """Import a peer's identity.json and establish bilateral trust.

    PEER_IDENTITY_JSON is the path to the identity file your peer shared.
    """
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    peer_path = Path(peer_identity_file)
    if not peer_path.exists():
        click.echo(f"Error: {peer_path} not found.", err=True)
        raise SystemExit(1)
    _fed.init_federation(paths.root)
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    try:
        peer = _fed.trust_peer(paths.root, peer_path, display_name=name, scopes=scope_list)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    click.echo(f"✓ Trusted: {peer.display_name} [{peer.fingerprint}]")
    click.echo(f"  Scopes: {', '.join(peer.scopes)}")


@federation_group.command(name="revoke")
@click.argument("fingerprint")
@click.pass_context
def federation_revoke(ctx: click.Context, fingerprint: str) -> None:
    """Remove a peer from trusted peers by their fingerprint."""
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    removed = _fed.revoke_peer(paths.root, fingerprint)
    if removed:
        click.echo(f"✓ Revoked: {fingerprint}")
    else:
        click.echo(f"Not found: {fingerprint} (already removed?)")


@federation_group.command(name="peers")
@click.pass_context
def federation_peers(ctx: click.Context) -> None:
    """List all trusted federation peers."""
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    peers = _fed.list_peers(paths.root)
    if not peers:
        click.echo("No trusted peers. Run: swarm federation trust <peer_identity.json>")
        return
    click.echo(f"{'Fingerprint':<18}  {'Name':<24}  {'Scopes'}")
    click.echo("-" * 70)
    for p in peers:
        click.echo(f"{p.fingerprint:<18}  {p.display_name:<24}  {', '.join(p.scopes)}")


@federation_group.command(name="send")
@click.argument("to_fingerprint")
@click.argument("intent", type=click.Choice(["work_request", "alignment_signal", "capability_ad"]))
@click.option("--desc", default="", metavar="TEXT", help="Description (for work_request)")
@click.option("--context", "ctx_note", default="", metavar="TEXT",
              help="Extra context to include in the payload")
@click.option("--agent", default=None, help="Agent ID to record in the message")
@click.pass_context
def federation_send(
    ctx: click.Context,
    to_fingerprint: str,
    intent: str,
    desc: str,
    ctx_note: str,
    agent: str | None,
) -> None:
    """Create a signed outbound intent message in outbox/.

    TO_FINGERPRINT is the 16-char fingerprint from 'swarm federation peers'.
    INTENT is the type of message to send.

    Deliver the resulting file to the peer via git, shared directory,
    or any other transport — then they run 'swarm federation apply'.
    """
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    agent_id = agent or _default_agent()
    payload: dict = {}
    if desc:
        payload["description"] = desc
    if ctx_note:
        payload["context"] = ctx_note
    try:
        out_path = _fed.write_outbox(paths.root, to_fingerprint, intent, payload, agent_id)
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    click.echo(f"✓ Message written: {out_path}")
    click.echo("  Deliver this file to the peer; they run: swarm federation apply <file>")


@federation_group.command(name="inbox")
@click.pass_context
def federation_inbox(ctx: click.Context) -> None:
    """List messages received in inbox/."""
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    messages = _fed.read_inbox(paths.root)
    if not messages:
        click.echo("Inbox is empty.")
        return
    click.echo(f"{'Timestamp':<18}  {'From':<10}  {'Intent':<20}  File")
    click.echo("-" * 75)
    for m in messages:
        fname = m.source_file.name if m.source_file else "?"
        click.echo(
            f"{m.timestamp:<18}  {m.from_fingerprint[:8]:<10}  {m.intent:<20}  {fname}"
        )
    click.echo(f"\n{len(messages)} message(s). Apply: swarm federation apply <file>")


@federation_group.command(name="apply")
@click.argument("message_file", metavar="MESSAGE_JSON")
@click.option("--yes", "-y", is_flag=True, help="Apply without confirmation prompt")
@click.pass_context
def federation_apply(ctx: click.Context, message_file: str, yes: bool) -> None:
    """Apply an inbox message to this swarm's queue.

    MESSAGE_JSON is the path to the received message file
    (see 'swarm federation inbox' for the filename).
    """
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    msg_path = Path(message_file)
    if not msg_path.exists():
        click.echo(f"Error: {msg_path} not found.", err=True)
        raise SystemExit(1)

    try:
        import json as _json
        data = _json.loads(msg_path.read_text())
    except Exception as exc:
        click.echo(f"Error reading message: {exc}", err=True)
        raise SystemExit(1)

    intent = data.get("intent", "?")
    from_fp = data.get("from_fingerprint", "?")[:8]
    payload = data.get("payload", {})

    click.echo(f"\nMessage: {intent} from {from_fp}")
    if payload:
        for k, v in payload.items():
            click.echo(f"  {k}: {v}")
    click.echo("")

    if not yes:
        click.confirm("Apply this message?", abort=True)

    result = _fed.apply_inbox_message(paths.root, msg_path, add_item, paths)
    if result["ok"]:
        click.echo(f"✓ {result['reason']}")
    else:
        click.echo(f"✗ {result['reason']}", err=True)
        if result.get("quarantined"):
            click.echo(
                f"  Held in strangers/. Review with 'swarm federation strangers list'.",
                err=True,
            )
        raise SystemExit(1)


@federation_group.command(name="triage")
@click.pass_context
def federation_triage(ctx: click.Context) -> None:
    """Sweep inbox/ — quarantine messages from unknown peers into strangers/.

    Trusted messages remain in inbox/ awaiting explicit ``swarm federation
    apply``. Untrusted messages are moved to strangers/ for human review
    via ``swarm federation strangers``.
    """
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    counts = _fed.triage_inbox(paths.root)
    click.echo(
        f"Triage: {counts['trusted']} trusted (kept in inbox/), "
        f"{counts['quarantined']} quarantined to strangers/, "
        f"{counts['errors']} errors"
    )


@federation_group.group(name="strangers")
@click.pass_context
def federation_strangers(ctx: click.Context) -> None:
    """Untrusted message bay — collaborative-but-untrusted holding area.

    Messages from peers we do not yet trust (no matching fingerprint in
    trusted_peers/) or from disabled intents land here instead of being
    silently dropped. The bay preserves the message for review while
    granting the sender no write access to the queue.

    \b
    swarm federation strangers list           list quarantined messages
    swarm federation strangers show <file>    pretty-print one message
    swarm federation strangers promote <file> trust the peer + replay
    swarm federation strangers reject <file>  archive without trust
    """


@federation_strangers.command(name="list")
@click.pass_context
def federation_strangers_list(ctx: click.Context) -> None:
    """List every message currently held in the strangers bay."""
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    entries = _fed.list_strangers(paths.root)
    if not entries:
        click.echo("Strangers bay is empty.")
        return
    click.echo(f"{'Timestamp':<18}  {'From fp':<10}  {'Intent':<18}  File")
    click.echo("-" * 75)
    for e in entries:
        click.echo(
            f"{e['timestamp']:<18}  {(e['from_fingerprint'] or '?')[:8]:<10}  "
            f"{e['intent'] or '?':<18}  {e['file']}"
        )
    click.echo(f"\n{len(entries)} stranger(s) held for review.")


@federation_strangers.command(name="show")
@click.argument("filename")
@click.pass_context
def federation_strangers_show(ctx: click.Context, filename: str) -> None:
    """Pretty-print a single stranger message + the doorman reason."""
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    data = _fed.read_stranger(paths.root, filename)
    if data is None:
        click.echo(f"Error: no stranger message at {filename}", err=True)
        raise SystemExit(1)
    import json as _json
    click.echo(_json.dumps(data, indent=2))
    reason_path = paths.root / _fed.STRANGERS_DIR / f"{filename}.reason.txt"
    if reason_path.exists():
        click.echo("\nQuarantine reason:")
        click.echo(reason_path.read_text(encoding="utf-8"))


@federation_strangers.command(name="promote")
@click.argument("filename")
@click.option("--name", default="", help="Display name for this peer")
@click.option("--scopes", default="work_request,alignment_signal",
              help="Comma-separated list of permitted intents")
@click.pass_context
def federation_strangers_promote(ctx: click.Context, filename: str, name: str, scopes: str) -> None:
    """Trust the peer behind this stranger message and move it back to inbox/."""
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    scope_list = [s.strip() for s in scopes.split(",") if s.strip()]
    try:
        peer, dest = _fed.promote_stranger(paths.root, filename,
                                           scopes=scope_list, display_name=name)
    except (FileNotFoundError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    click.echo(f"✓ Trusted: {peer.display_name} [{peer.fingerprint}]")
    click.echo(f"  Scopes: {', '.join(peer.scopes)}")
    click.echo(f"  Message moved to inbox: {dest.name}")
    click.echo(f"  Apply: swarm federation apply {dest}")


@federation_strangers.command(name="reject")
@click.argument("filename")
@click.option("--reason", default="", help="Why this stranger was rejected")
@click.pass_context
def federation_strangers_reject(ctx: click.Context, filename: str, reason: str) -> None:
    """Archive a stranger message into strangers/rejected/ without trust."""
    from . import federation as _fed
    paths = _get_paths(ctx.obj["path"])
    try:
        dest = _fed.reject_stranger(paths.root, filename, reason=reason)
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(1)
    click.echo(f"✓ Rejected: {dest}")


# ---------------------------------------------------------------------------
# swarm schedule
# ---------------------------------------------------------------------------

@cli.group(name="schedule")
@click.pass_context
def schedule_group(ctx: click.Context) -> None:
    """Cron and event-driven schedule management.

    Schedules are stored in .swarm/schedules.md. Run 'swarm schedule run-due'
    from your system crontab to execute due schedules automatically.

    Types: cron (5-field expr), interval (30m/6h/2d), on:done, on:blocked.
    """


@schedule_group.command(name="list")
@click.pass_context
def schedule_list(ctx: click.Context) -> None:
    """List all configured schedules."""
    from . import scheduler as _sched
    paths = _get_paths(ctx.obj["path"])
    schedules = _sched.load_schedules(paths.root)
    if not schedules:
        click.echo("No schedules configured. Use 'swarm schedule add' to create one.")
        return
    click.echo(f"\n{'ID':<12} {'TYPE':<10} {'SPEC':<20} {'LAST RUN':<20} COMMAND")
    click.echo("─" * 80)
    for s in schedules:
        last = s.last_run[:16] if s.last_run else "never"
        status = "" if s.enabled else " [disabled]"
        click.echo(f"{s.id:<12} {s.stype:<10} {s.spec:<20} {last:<20} {s.command}{status}")
    click.echo("")


@schedule_group.command(name="add")
@click.argument("spec")
@click.argument("command")
@click.option("--name", default="", help="Human-readable label")
@click.option("--notes", default="", help="Optional description")
@click.pass_context
def schedule_add(ctx: click.Context, spec: str, command: str, name: str, notes: str) -> None:
    """Add a new schedule.

    SPEC is a cron expression ('0 */6 * * *'), interval ('6h'), or
    event ('on:done CLD-042').

    COMMAND is the shell command to run (e.g. 'swarm heal --fix').

    Examples:

      swarm schedule add '0 */6 * * *' 'swarm heal --fix'

      swarm schedule add '6h' 'swarm audit --security' --name 'Security check'

      swarm schedule add 'on:done CLD-042' 'swarm ai "claim CLD-043"'
    """
    from . import scheduler as _sched
    paths = _get_paths(ctx.obj["path"])
    stype = _sched._infer_stype(spec)
    label = name or spec
    s = _sched.add_schedule(paths.root, label, stype, spec, command, notes)
    click.echo(f"✓ Added {s.id}: {s.stype} '{s.spec}' → {s.command}")


@schedule_group.command(name="remove")
@click.argument("schedule_id")
@click.pass_context
def schedule_remove(ctx: click.Context, schedule_id: str) -> None:
    """Remove a schedule by ID (e.g. SCHED-001)."""
    from . import scheduler as _sched
    paths = _get_paths(ctx.obj["path"])
    if _sched.remove_schedule(paths.root, schedule_id):
        click.echo(f"✓ Removed {schedule_id}")
    else:
        click.echo(f"Schedule {schedule_id} not found.", err=True)
        raise SystemExit(1)


@schedule_group.command(name="run")
@click.argument("schedule_id")
@click.pass_context
def schedule_run(ctx: click.Context, schedule_id: str) -> None:
    """Manually trigger a specific schedule by ID."""
    from . import scheduler as _sched
    paths = _get_paths(ctx.obj["path"])
    schedules = _sched.load_schedules(paths.root)
    target = next((s for s in schedules if s.id == schedule_id), None)
    if target is None:
        click.echo(f"Schedule {schedule_id} not found.", err=True)
        raise SystemExit(1)
    click.echo(f"Running {schedule_id}: {target.command}")
    result = _sched.run_schedule(paths.root, target)
    if result.ok:
        click.echo(f"✓ Exited 0")
    else:
        click.echo(f"✗ Exited {result.exit_code}", err=True)
    if result.stdout:
        click.echo(result.stdout)
    if result.stderr:
        click.echo(result.stderr, err=True)


@schedule_group.command(name="run-due")
@click.pass_context
def schedule_run_due(ctx: click.Context) -> None:
    """Run all currently due cron/interval schedules.

    Add to system crontab for automatic execution:

      * * * * *  cd /path/to/repo && swarm schedule run-due
    """
    from . import scheduler as _sched
    paths = _get_paths(ctx.obj["path"])
    results = _sched.run_due(paths.root)
    if not results:
        click.echo("No schedules due.")
        return
    for r in results:
        status = "✓" if r.ok else "✗"
        click.echo(f"  {status} {r.schedule_id} (exit {r.exit_code}): {r.command}")


# ---------------------------------------------------------------------------
# swarm workflow
# ---------------------------------------------------------------------------

@cli.group(name="workflow")
@click.pass_context
def workflow_group(ctx: click.Context) -> None:
    """Multi-step workflow execution from .swarm/workflows/*.md.

    Workflows define sequences of swarm commands and are expressed
    in markdown with a YAML frontmatter header.

    Patterns: sequential (default), concurrent, conditional, mixture.
    """


@workflow_group.command(name="list")
@click.pass_context
def workflow_list(ctx: click.Context) -> None:
    """List available workflows in .swarm/workflows/."""
    from . import workflows as _wf
    paths = _get_paths(ctx.obj["path"])
    names = _wf.list_workflows(paths)
    if not names:
        click.echo("No workflows found in .swarm/workflows/. Create a .md file to define one.")
        return
    click.echo(f"\nWorkflows in {paths.workflows}:\n")
    for name in names:
        try:
            wf = _wf.load_workflow(paths, name)
            trigger = wf.trigger
            steps = len(wf.steps)
            click.echo(f"  {name:<30} [{wf.pattern}]  trigger: {trigger}  ({steps} steps)")
        except Exception:
            click.echo(f"  {name:<30} [parse error]")
    click.echo("")


@workflow_group.command(name="show")
@click.argument("name")
@click.pass_context
def workflow_show(ctx: click.Context, name: str) -> None:
    """Show steps for a specific workflow."""
    from . import workflows as _wf
    paths = _get_paths(ctx.obj["path"])
    try:
        wf = _wf.load_workflow(paths, name)
    except FileNotFoundError:
        click.echo(f"Workflow '{name}' not found.", err=True)
        raise SystemExit(1)
    click.echo(f"\n{wf.name}  [{wf.pattern}]  trigger: {wf.trigger}")
    if wf.description:
        click.echo(f"  {wf.description}")
    click.echo(f"\n  {'#':<4} {'COMMAND':<40} {'AGENT':<10} {'TIMEOUT'}")
    click.echo("  " + "─" * 65)
    for step in wf.steps:
        deps = f"  depends: {','.join(step.depends)}" if step.depends else ""
        cond = f"  if: {step.condition}" if step.condition else ""
        click.echo(f"  {step.number:<4} {step.command:<40} {step.agent:<10} {step.timeout_min}m{deps}{cond}")
    click.echo("")


@workflow_group.command(name="run")
@click.argument("name")
@click.option("--dry-run", is_flag=True, default=False, help="Show what would run without executing")
@click.option("-y", "--yes", is_flag=True, default=False, help="Skip confirmation")
@click.pass_context
def workflow_run(ctx: click.Context, name: str, dry_run: bool, yes: bool) -> None:
    """Execute a workflow by name.

    NAME is the workflow filename without .md extension.

    Examples:

      swarm workflow run oauth2-flow

      swarm workflow run deploy --dry-run

      swarm workflow run weekly-report --yes
    """
    from . import workflows as _wf
    paths = _get_paths(ctx.obj["path"])

    try:
        wf = _wf.load_workflow(paths, name)
    except FileNotFoundError:
        click.echo(f"Workflow '{name}' not found.", err=True)
        raise SystemExit(1)

    prefix = "[dry-run] " if dry_run else ""
    click.echo(f"\n{prefix}Workflow: {wf.name}  [{wf.pattern}]  {len(wf.steps)} steps\n")
    for step in wf.steps:
        click.echo(f"  {step.number}. {step.command}  (agent: {step.agent})")
    click.echo("")

    if not yes and not dry_run:
        click.confirm("Execute workflow?", abort=True)

    run = _wf.run_workflow(paths, name, dry_run=dry_run)

    click.echo(f"\nResults ({run.summary}):\n")
    for sr in run.step_results:
        icon = "⊘" if sr.skipped else ("✓" if sr.ok else "✗")
        note = f" — {sr.skip_reason}" if sr.skipped else f" (exit {sr.exit_code})"
        click.echo(f"  {icon} step {sr.step_number}: {sr.command}{note}")
        if sr.stdout:
            for line in sr.stdout.splitlines()[:5]:
                click.echo(f"    {line}")
    click.echo("")

    if not run.ok:
        raise SystemExit(1)


@workflow_group.command(name="status")
@click.argument("name")
@click.pass_context
def workflow_status_cmd(ctx: click.Context, name: str) -> None:
    """Show last run status for a workflow."""
    from . import workflows as _wf
    paths = _get_paths(ctx.obj["path"])
    status = _wf.workflow_status(paths, name)
    if not status:
        click.echo(f"No run history for workflow '{name}'.")
        return
    ok_icon = "✓" if status.get("ok") else "✗"
    click.echo(f"\n{ok_icon} {name}  last ran: {status.get('started_at', '?')}  →  {status.get('summary', '?')}\n")
    for step in status.get("steps", []):
        skip_note = " [skipped]" if step.get("skipped") else f" (exit {step.get('exit', '?')})"
        click.echo(f"  step {step['n']}: {step['cmd']}{skip_note}")
    click.echo("")


@workflow_group.command(name="create")
@click.argument("name")
@click.option("--pattern", default="sequential",
              type=click.Choice(["sequential", "concurrent", "conditional", "mixture"]),
              help="Execution pattern")
@click.option("--trigger", default="manual", help="Trigger: manual, on:done ITEM-ID, or cron expr")
@click.option("--description", default="", help="Short description")
@click.pass_context
def workflow_create(ctx: click.Context, name: str, pattern: str, trigger: str, description: str) -> None:
    """Scaffold a new workflow file in .swarm/workflows/.

    Edit the generated file to fill in steps.
    """
    from . import workflows as _wf
    from .workflows import Workflow, WorkflowStep
    paths = _get_paths(ctx.obj["path"])
    wf = Workflow(
        name=name,
        trigger=trigger,
        pattern=pattern,
        description=description or f"{name} workflow",
        steps=[
            WorkflowStep(number=1, command="swarm heal", agent="auto", timeout_min=5),
            WorkflowStep(number=2, command="# replace with your command", agent="auto"),
        ],
    )
    out = _wf.create_workflow(paths, wf)
    click.echo(f"✓ Created {out}")
    click.echo(f"  Edit {out} to define steps, then run: swarm workflow run {name}")


# ---------------------------------------------------------------------------
# swarm gui
# ---------------------------------------------------------------------------

@cli.command(name="gui")
@click.option("--port", default=8000, help="Port to run the dashboard on (default: 8000)")
@click.option("--open", "open_browser", is_flag=True, default=False, help="Open browser automatically")
@click.pass_context
def gui(ctx: click.Context, port: int, open_browser: bool) -> None:
    """Start the visual Swarm Trail dashboard."""
    import http.server
    import socketserver
    import webbrowser
    from threading import Thread

    root_path = Path(ctx.obj["path"]).resolve()
    template_path = Path(__file__).parent / "templates" / "gui.html"
    
    if not template_path.exists():
        click.echo(f"Error: GUI template not found at {template_path}", err=True)
        return

    class SwarmHandler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/api/state.json":
                try:
                    data = get_colony_summary(root_path)
                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(data).encode())
                except Exception as e:
                    self.send_error(500, str(e))
            elif self.path == "/" or self.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-type", "text/html")
                self.end_headers()
                self.wfile.write(template_path.read_bytes())
            elif self.path == "/logo.png":
                logo = root_path / "logo.png"
                if not logo.exists():
                    logo = Path(__file__).parent.parent.parent / "logo.png"
                if logo.exists():
                    self.send_response(200)
                    self.send_header("Content-type", "image/png")
                    self.end_headers()
                    self.wfile.write(logo.read_bytes())
                else:
                    self.send_error(404)
            else:
                super().do_GET()

        def log_message(self, format, *args):
            # Silence standard logging to keep CLI clean
            pass

    click.echo(f"\nStarting dot_swarm GUI on http://localhost:{port}")
    click.echo("Press Ctrl+C to stop.\n")
    
    if open_browser:
        Thread(target=lambda: webbrowser.open(f"http://localhost:{port}")).start()

    with socketserver.TCPServer(("", port), SwarmHandler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            click.echo("\nStopping GUI...")
            httpd.shutdown()


# ---------------------------------------------------------------------------
# swarm report
# ---------------------------------------------------------------------------

@cli.command(name="report")
@click.option("--out", "out_path", default=None, metavar="FILE",
              help="Write report to FILE instead of stdout")
@click.option("--only", "only_section",
              type=click.Choice(["active", "pending", "all"]), default="all",
              help="Limit to this section (default: all, excludes done)")
@click.option("--no-done", "no_done", is_flag=True, default=False,
              help="Omit done sections from each division (default: omit)")
@click.option("--depth", default=2, help="Directory depth for division discovery (default: 2)")
@click.pass_context
def report_cmd(ctx: click.Context, out_path: str | None, only_section: str,
               no_done: bool, depth: int) -> None:
    """Generate a full markdown report across all divisions.

    Recursively discovers .swarm/ directories and produces a shareable
    snapshot of the whole colony — suitable for filing as a GitHub issue,
    pasting into a wiki, or archiving as REPORT.md.

    Examples:
      swarm report                       # print to stdout
      swarm report --out REPORT.md       # write to file
      swarm report --only active         # active items only
      swarm --path ~/org report          # run from org root
    """
    from datetime import datetime, timezone

    root_path = Path(ctx.obj["path"]).resolve()

    # Discover all divisions (same logic as explore)
    divisions: list[tuple[Path, SwarmPaths]] = []
    root_paths = SwarmPaths.find(root_path)
    if root_paths:
        divisions.append((root_path, root_paths))
    for p in root_path.glob("*/.swarm"):
        div_path = p.parent
        if div_path == root_path:
            continue
        paths_obj = SwarmPaths.find(div_path)
        if paths_obj:
            divisions.append((div_path, paths_obj))
    if depth > 1:
        for p in root_path.glob("*/*/.swarm"):
            div_path = p.parent
            paths_obj = SwarmPaths.find(div_path)
            if paths_obj and (div_path, paths_obj) not in divisions:
                divisions.append((div_path, paths_obj))

    divisions = sorted(divisions, key=lambda x: (not x[1].is_org_level(), x[0].name))

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines: list[str] = [
        f"# dot_swarm Colony Report",
        f"",
        f"**Generated**: {now}  ",
        f"**Root**: `{root_path}`  ",
        f"**Divisions found**: {len(divisions)}",
        f"",
        f"---",
        f"",
    ]

    total_active = total_pending = 0

    for div_path, paths in divisions:
        try:
            state        = read_state(paths)
            active, pending, done_items = read_queue(paths)
            total_active  += len(active)
            total_pending += len(pending)

            label = f"★ {div_path.name} (org)" if paths.is_org_level() else div_path.name
            focus = state.get("Current focus", "(not set)")
            touched = state.get("Last touched", "unknown")

            lines += [f"## {label}", f"", f"**Focus**: {focus}  ",
                      f"**Last touched**: {touched}  ",
                      f"**Items**: {len(active)} active · {len(pending)} pending", f""]

            def _fmt(item) -> str:
                pri = f"`{item.priority.value}`" if item.priority else ""
                proj = f"_{item.project}_" if item.project else ""
                meta = " · ".join(x for x in [pri, proj] if x)
                meta_str = f" — {meta}" if meta else ""
                return f"- **[{item.id}]** {item.description}{meta_str}"

            if only_section in ("active", "all") and active:
                lines += ["**Active**", ""]
                lines += [_fmt(i) for i in active]
                lines.append("")

            if only_section in ("pending", "all") and pending:
                lines += ["**Pending**", ""]
                lines += [_fmt(i) for i in pending]
                lines.append("")

            if not no_done and only_section == "all" and done_items:
                lines += [f"<details><summary>Done ({len(done_items)})</summary>", ""]
                lines += [_fmt(i) for i in done_items[-10:]]  # last 10
                if len(done_items) > 10:
                    lines.append(f"_…and {len(done_items) - 10} more_")
                lines += ["", "</details>", ""]

        except Exception as e:
            lines += [f"## {div_path.name}", f"", f"_(error reading state: {e})_", ""]

        lines += ["---", ""]

    lines += [
        f"**Colony totals**: {total_active} active · {total_pending} pending",
        f"",
        f"_Generated by [dot_swarm](https://github.com/MikeHLee/dot_swarm)_",
    ]

    output = "\n".join(lines)

    if out_path:
        Path(out_path).write_text(output, encoding="utf-8")
        click.echo(f"Report written to {out_path}  ({total_active} active, {total_pending} pending)")
    else:
        click.echo(output)


# ---------------------------------------------------------------------------
# swarm unblock
# ---------------------------------------------------------------------------

@cli.command(name="unblock")
@click.argument("item_id")
@click.option("--reclaim", is_flag=True, default=False,
              help="Re-claim the item for this agent instead of returning to OPEN")
@click.option("--agent", "agent_id", default=None, help="Agent ID override")
@click.pass_context
def unblock_cmd(ctx: click.Context, item_id: str, reclaim: bool, agent_id: str | None) -> None:
    """Clear a blocked item back to Open (or re-claim it).

    Examples:
      swarm unblock CLD-042           # → [OPEN]
      swarm unblock CLD-042 --reclaim # → [CLAIMED · agent · now]
    """
    from .operations import block_item, claim_item
    from .models import WorkItem

    paths = _get_paths(ctx.obj["path"])
    agent = agent_id or _default_agent()

    active, pending, done = read_queue(paths)
    all_items = active + pending + done
    target = next((i for i in all_items if i.id.upper() == item_id.upper()), None)

    if target is None:
        click.echo(f"Item {item_id} not found.", err=True)
        raise SystemExit(1)

    if "BLOCKED" not in (target.status or "").upper() and not reclaim:
        click.echo(f"{item_id} is not blocked (status: {target.status}). "
                   f"Use --reclaim to re-claim an already-open item.", err=True)
        raise SystemExit(1)

    # Read raw queue, replace the status stamp
    raw = paths.queue.read_text()

    # Replace [BLOCKED · ...] with [OPEN] or claim stamp
    import re
    if reclaim:
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
        new_stamp = f"[CLAIMED · {agent} · {ts}]"
        new_section = "Active"
    else:
        new_stamp = "[OPEN]"
        new_section = "Pending"

    # Replace the status line
    old_pattern = re.compile(
        r'\[BLOCKED[^\]]*\]',
        re.IGNORECASE
    )
    new_raw = old_pattern.sub(new_stamp, raw, count=1)

    if new_raw == raw:
        # Fallback: just write the item back with OPEN status
        click.echo(f"Could not find BLOCKED stamp for {item_id} — item may already be unblocked.")
        return

    from .operations import _atomic_write
    _atomic_write(paths.queue, new_raw)

    action = f"re-claimed by {agent}" if reclaim else "returned to OPEN"
    click.echo(f"  [{item_id}] unblocked — {action}")


# ---------------------------------------------------------------------------
# swarm ls
# ---------------------------------------------------------------------------

@cli.command(name="ls")
@click.option("--section", type=click.Choice(["active", "pending", "done", "all"]),
              default="all", help="Section to show (default: all)")
@click.option("--priority", default=None,
              type=click.Choice(["critical", "high", "medium", "low"]),
              help="Filter by priority")
@click.option("--project", default=None, help="Filter by project")
@click.pass_context
def ls_cmd(ctx: click.Context, section: str, priority: str | None, project: str | None) -> None:
    """List work items from the queue."""
    paths = _get_paths(ctx.obj["path"])
    active, pending, done = read_queue(paths)

    sections: list[tuple[str, list]] = []
    if section in ("active", "all"):
        sections.append(("Active", active))
    if section in ("pending", "all"):
        sections.append(("Pending", pending))
    if section in ("done", "all"):
        sections.append(("Done", done))

    STATE_ICON = {
        "OPEN": "[ ]", "CLAIMED": "[>]", "PARTIAL": "[~]",
        "BLOCKED": "[!]", "DONE": "[x]", "CANCELLED": "[-]",
    }

    for label, items in sections:
        filtered = items
        if priority:
            filtered = [i for i in filtered if i.priority.value == priority]
        if project:
            filtered = [i for i in filtered if i.project == project]
        if filtered:
            click.echo(f"\n## {label}")
            for item in filtered:
                icon = STATE_ICON.get(item.state.value, "[ ]")
                pri = f"[{item.priority.value.upper()}]"
                claim = f" ← {item.claimed_by}" if item.claimed_by else ""
                click.echo(f"  {icon} [{item.id}] {pri} {item.description}{claim}")


# ---------------------------------------------------------------------------
# swarm partial
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("item_id")
@click.option("--note", default="", help="Checkpoint note")
@click.option("--agent", "agent_id", default=None, help="Agent ID override")
@click.option(
    "--proof", default="",
    help=(
        "Evidence for Inspector verification. "
        "Format: 'branch:<name> commit:<sha> tests:<N/N>'. "
        "Required when the Inspector role is enabled."
    ),
)
@click.pass_context
def partial(ctx: click.Context, item_id: str, note: str, agent_id: str | None,
            proof: str) -> None:
    """Mark a claimed item as partially done (checkpoint).

    When the Inspector role is enabled, supply --proof so the inspector
    agent can verify completion before the item is marked done.

    Example:
      swarm partial SWC-042 --proof "branch:feature/oauth2 commit:abc1234 tests:42/42"
    """
    paths = _get_paths(ctx.obj["path"])
    agent = agent_id or _default_agent()
    try:
        item = partial_item(paths, item_id, agent, note)
        if proof:
            from . import roles as _roles
            # Re-read after partial_item wrote the queue, then attach proof
            active2, pending2, done2 = read_queue(paths)
            target = next((i for i in active2 + pending2 if i.id == item_id), None)
            if target:
                target.proof = proof
                write_queue(paths, active2, pending2, done2)
            click.echo(f"Partial [{item.id}]: proof attached.")
            # Validate against inspector requirements
            role = _roles.load_role(paths, "inspector")
            if role:
                missing = _roles.validate_proof(proof, role.require_proof_fields)
                if missing:
                    click.echo(
                        f"  Warning: proof missing required fields: {', '.join(missing)}. "
                        "Inspector may reject."
                    )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)
    click.echo(f"Partial [{item.id}]: {item.description} (re-claimed by {agent})")


# ---------------------------------------------------------------------------
# swarm block
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("item_id")
@click.argument("reason")
@click.pass_context
def block(ctx: click.Context, item_id: str, reason: str) -> None:
    """Mark a work item as blocked with a reason."""
    paths = _get_paths(ctx.obj["path"])
    try:
        item = block_item(paths, item_id, reason)
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)
    click.echo(f"Blocked [{item.id}]: {item.description}")
    click.echo(f"  Reason: {reason}")


# ---------------------------------------------------------------------------
# swarm inspect  (Inspector role — verify worker proof, pass or fail)
# ---------------------------------------------------------------------------

@cli.command()
@click.argument("item_id")
@click.option("--pass", "verdict", flag_value="pass", help="Accept proof — mark item done")
@click.option("--fail", "verdict", flag_value="fail", help="Reject proof — reopen to worker")
@click.option("--reason", default="", help="Reason for failure (required with --fail)")
@click.option("--agent", default=None, help="Inspector agent ID override")
@click.pass_context
def inspect(ctx: click.Context, item_id: str, verdict: str | None,
            reason: str, agent: str | None) -> None:
    """Verify a worker's proof-of-work and pass or fail the item.

    Inspector role must be enabled ('swarm role enable inspector').
    Workers supply proof via 'swarm partial <id> --proof "..."'.

    Examples:
      swarm inspect SWC-042 --pass
      swarm inspect SWC-042 --fail --reason "Tests failed on edge case X"
    """
    from . import roles as _roles
    from . import signing as _sign

    paths = _get_paths(ctx.obj["path"])
    inspector_id = agent or _default_agent()

    if not _roles.is_role_enabled(paths, "inspector"):
        click.echo(
            "Inspector role is not enabled. Run: swarm role enable inspector", err=True
        )
        sys.exit(1)

    if verdict is None:
        click.echo("Specify --pass or --fail.", err=True)
        sys.exit(1)

    role = _roles.load_role(paths, "inspector")

    active, pending, _ = read_queue(paths)
    target = next((i for i in active + pending if i.id == item_id), None)
    if target is None:
        click.echo(f"Error: {item_id} not found in active or pending queue.", err=True)
        sys.exit(1)

    if verdict == "pass":
        missing = _roles.validate_proof(target.proof, role.require_proof_fields)
        if missing:
            click.echo(
                f"Warning: proof is missing fields {missing}. Passing anyway (inspector override)."
            )
        item = done_item(paths, item_id, inspector_id,
                         note=f"inspector-pass by {inspector_id}")
        write_state(paths, {"last_agent": inspector_id})
        record = _sign.sign_operation(paths.root, "inspect_pass", inspector_id,
                                      {"item_id": item_id})
        _sign.append_trail(paths.root, record)
        click.echo(f"Passed: [{item_id}] {item.description}")

    else:  # fail
        if not reason:
            click.echo("--reason is required with --fail.", err=True)
            sys.exit(1)
        item, exhausted = reopen_item(paths, item_id, inspector_id, reason,
                                      role_max_iterations=role.max_iterations)
        record = _sign.sign_operation(paths.root, "inspect_fail", inspector_id,
                                      {"item_id": item_id, "reason": reason})
        _sign.append_trail(paths.root, record)

        effective_max = item.max_retries if item.max_retries > 0 else role.max_iterations
        if exhausted:
            click.echo(
                f"Max retries exhausted: [{item_id}] is now BLOCKED "
                f"({item.inspect_fails}/{effective_max} fails)."
            )
            click.echo(f"  Reason: {reason}")
            click.echo("  Use 'swarm unblock --reclaim' to manually reassign, or 'swarm done --force' to override.")
        else:
            click.echo(f"Rejected: [{item_id}] re-opened. Fail #{item.inspect_fails}/{effective_max}.")
            click.echo(f"  Reason: {reason}")


# ---------------------------------------------------------------------------
# swarm role  (role management)
# ---------------------------------------------------------------------------

@cli.group()
@click.pass_context
def role(ctx: click.Context) -> None:
    """Manage agent roles (inspector, watchdog, supervisor, librarian).

    Roles extend swarm coordination with structured behaviors without
    touching queue.md. Toggle them on/off at any time.

    Examples:
      swarm role list
      swarm role enable inspector --max-iterations 3
      swarm role show inspector
      swarm role disable inspector
    """


@role.command(name="list")
@click.pass_context
def role_list(ctx: click.Context) -> None:
    """List configured roles and their status."""
    from . import roles as _roles
    paths = _get_paths(ctx.obj["path"])
    configured = _roles.list_roles(paths)
    all_names = sorted(_roles.KNOWN_ROLES)
    click.echo("Roles:")
    for name in all_names:
        cfg = next((r for r in configured if r.name == name), None)
        if cfg:
            status = "enabled" if cfg.enabled else "disabled"
            agent_str = f"  assigned: {cfg.assigned_agent}" if cfg.assigned_agent else ""
            click.echo(f"  {name:<12} [{status}]{agent_str}")
        else:
            click.echo(f"  {name:<12} [not configured]")


@role.command(name="enable")
@click.argument("role_name")
@click.option("--max-iterations", default=3, show_default=True,
              help="(inspector) Fail count before watchdog escalation")
@click.option("--require-proof", default="branch,commit",
              help="(inspector) Comma-separated required proof fields")
@click.option("--agent", default=None, help="Assign a specific agent ID to this role")
@click.pass_context
def role_enable(ctx: click.Context, role_name: str, max_iterations: int,
                require_proof: str, agent: str | None) -> None:
    """Enable a role (or reconfigure it if already enabled)."""
    from . import roles as _roles
    paths = _get_paths(ctx.obj["path"])
    try:
        cfg = _roles.enable_role(
            paths, role_name,
            max_iterations=max_iterations,
            require_proof_fields=[f.strip() for f in require_proof.split(",") if f.strip()],
            assigned_agent=agent,
        )
    except ValueError as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    click.echo(f"Enabled role: {cfg.name}")
    if role_name == "inspector":
        click.echo(f"  max_iterations: {cfg.max_iterations}")
        click.echo(f"  require_proof:  {', '.join(cfg.require_proof_fields)}")
        click.echo("Workers must now run 'swarm partial <id> --proof \"...\"' before done.")
    if cfg.assigned_agent:
        click.echo(f"  assigned_agent: {cfg.assigned_agent}")


@role.command(name="disable")
@click.argument("role_name")
@click.pass_context
def role_disable(ctx: click.Context, role_name: str) -> None:
    """Disable a role by removing its config."""
    from . import roles as _roles
    paths = _get_paths(ctx.obj["path"])
    _roles.disable_role(paths, role_name)
    click.echo(f"Disabled role: {role_name}")


@role.command(name="show")
@click.argument("role_name")
@click.pass_context
def role_show(ctx: click.Context, role_name: str) -> None:
    """Show full configuration for a role."""
    import json as _json
    from . import roles as _roles
    paths = _get_paths(ctx.obj["path"])
    cfg = _roles.load_role(paths, role_name)
    if cfg is None:
        click.echo(f"Role '{role_name}' is not configured.")
        return
    click.echo(f"Role: {cfg.name}")
    click.echo(f"  enabled:              {cfg.enabled}")
    click.echo(f"  max_iterations:       {cfg.max_iterations}")
    click.echo(f"  require_proof_fields: {', '.join(cfg.require_proof_fields)}")
    click.echo(f"  assigned_agent:       {cfg.assigned_agent or '(any)'}")
    if cfg.extra:
        click.echo(f"  extra:                {_json.dumps(cfg.extra)}")


# ---------------------------------------------------------------------------
# swarm spawn  (tmux + agent CLI worker/role launcher)
# ---------------------------------------------------------------------------

_AGENT_CMDS = {
    "opencode": "opencode",
    "claude":   "claude",
    "ollama":   "ollama run llama3",
    "bedrock":  "swarm ai --chain",
}

_ROLE_PROMPTS = {
    "inspector":  "You are an inspector agent. Use 'swarm inspect' to verify worker proof.",
    "supervisor": "You are a supervisor agent. Use 'swarm report' and 'swarm explore'.",
    "watchdog":   "You are a watchdog agent. Monitor blocked items with 'swarm audit --full'.",
}


@cli.command()
@click.argument("item_id", required=False)
@click.option("--agent", "agent_name",
              type=click.Choice(["opencode", "claude", "ollama", "bedrock"]),
              default="opencode", show_default=True,
              help="Agent CLI to launch in the tmux window")
@click.option("--role", default=None,
              type=click.Choice(["inspector", "supervisor", "watchdog"]),
              help="Spawn as a role agent rather than a worker")
@click.option("--session", default="swarm", show_default=True,
              help="tmux session name (created if absent)")
@click.option("--window-name", default=None,
              help="tmux window name (default: item_id or role name)")
@click.option("--no-claim", is_flag=True, default=False,
              help="Do not auto-claim the item — just open the window")
@click.option("--agent-id", default=None,
              help="Override SWARM_AGENT_ID (default: spawn-<role|item>-<ts>)")
@click.pass_context
def spawn(ctx: click.Context, item_id: str | None, agent_name: str, role: str | None,
          session: str, window_name: str | None, no_claim: bool,
          agent_id: str | None) -> None:
    """Launch an agent in a named tmux window for a work item or role.

    Requires tmux 3.0+ and the chosen agent CLI (opencode, claude, etc.)
    to be on PATH. Creates the tmux session if it doesn't exist.

    Worker examples:
      swarm spawn SWC-042                          # opencode worker, auto-claims
      swarm spawn SWC-042 --agent claude           # Claude Code worker
      swarm spawn SWC-042 --agent ollama           # local Ollama worker
      swarm spawn SWC-042 --no-claim               # open window without claiming

    Role examples:
      swarm spawn --role inspector                  # inspector monitor window
      swarm spawn --role supervisor                 # supervisor overview window
      swarm spawn --role watchdog                   # watchdog audit loop
    """
    import shutil
    import subprocess

    paths = _get_paths(ctx.obj["path"])

    # --- Dependency checks ---
    if not shutil.which("tmux"):
        click.echo("Error: tmux not found. Install tmux 3.0+: brew install tmux", err=True)
        sys.exit(1)

    agent_cmd = _AGENT_CMDS[agent_name]
    agent_bin = agent_cmd.split()[0]
    if not shutil.which(agent_bin):
        click.echo(f"Error: '{agent_bin}' not found on PATH. Install it first.", err=True)
        sys.exit(1)

    ts = utcnow().strftime("%H%M%S")
    target_name = role if role else item_id

    if not item_id and not role:
        click.echo("Error: provide an ITEM_ID or --role.", err=True)
        sys.exit(1)

    effective_agent_id = agent_id or f"spawn-{target_name}-{ts}"
    win_name = window_name or target_name

    # --- Auto-claim item ---
    if item_id and not no_claim and not role:
        try:
            claim_item(paths, item_id, effective_agent_id)
            click.echo(f"Claimed [{item_id}] as {effective_agent_id}")
        except ValueError as e:
            click.echo(f"Warning: could not auto-claim: {e}", err=True)

    # --- Build env + prompt context ---
    swarm_root = str(paths.root.parent)
    env_prefix = (
        f"SWARM_AGENT_ID={effective_agent_id} "
        f"SWARM_ITEM_ID={item_id or ''} "
        f"SWARM_ROLE={role or 'worker'} "
    )

    role_hint = ""
    if role and role in _ROLE_PROMPTS:
        role_hint = f" # {_ROLE_PROMPTS[role]}"

    cmd = f"cd {swarm_root!r} && {env_prefix}{agent_cmd}{role_hint}"

    # --- Ensure tmux session exists ---
    check = subprocess.run(
        ["tmux", "has-session", "-t", session],
        capture_output=True,
    )
    if check.returncode != 0:
        subprocess.run(["tmux", "new-session", "-d", "-s", session], check=True)
        click.echo(f"Created tmux session: {session}")

    # --- Open window ---
    subprocess.run(
        ["tmux", "new-window", "-t", session, "-n", win_name, cmd],
        check=True,
    )
    click.echo(f"Spawned [{win_name}] in tmux session '{session}' ({agent_name})")
    click.echo(f"  Attach: tmux attach -t {session}")
    click.echo(f"  Switch: tmux select-window -t {session}:{win_name}")


# ---------------------------------------------------------------------------
# swarm crawl  (directory cataloging → context.md + optional queue items)
# ---------------------------------------------------------------------------

@cli.command()
@click.option("--depth", default=3, show_default=True,
              help="Max directory depth to walk")
@click.option("--create-items", is_flag=True, default=False,
              help="Create OPEN queue items for uncatalogued directories")
@click.option("--dry-run", is_flag=True, default=False,
              help="Print what would be cataloged without writing anything")
@click.pass_context
def crawl(ctx: click.Context, depth: int, create_items: bool, dry_run: bool) -> None:
    """Crawl the current directory tree to build context.

    Walks subdirectories up to --depth levels. Stops descending into any
    subdirectory that already has a .swarm/ directory — those are tracked
    as separate swarm divisions.

    Results are written to .swarm/context.md under a '## Directory Map'
    section. Use --create-items to also add OPEN queue items for each
    uncatalogued directory, ready for documentation or review.

    Combined with 'swarm heal', this replaces the need for a separate
    librarian role agent.

    Examples:
      swarm crawl                  # catalog to context.md
      swarm crawl --depth 5        # go deeper
      swarm crawl --create-items   # also add queue items
      swarm crawl --dry-run        # preview without writing
    """
    paths = _get_paths(ctx.obj["path"])
    root = paths.root.parent

    findings = crawl_directory(paths, root, depth=depth,
                                create_items=create_items, dry_run=dry_run)

    uncatalogued = [f for f in findings if f["type"] == "uncatalogued"]
    divisions    = [f for f in findings if f["type"] == "swarm_division"]

    if dry_run:
        click.echo(f"Crawl preview (dry run) — root: {root}")
    else:
        click.echo(f"Crawled {root}")

    if divisions:
        click.echo(f"\n  Swarm divisions found ({len(divisions)}) — skipped:")
        for d in divisions:
            click.echo(f"    {d['path']}/")

    if uncatalogued:
        label = "Would catalog" if dry_run else "Catalogued"
        click.echo(f"\n  {label} ({len(uncatalogued)} dirs):")
        for d in uncatalogued:
            summary = f"{d['file_count']} files"
            if d.get("ext_summary"):
                summary += f" ({d['ext_summary']})"
            click.echo(f"    {d['path']}/ — {summary}")
        if not dry_run:
            click.echo(f"\n  Written to: {paths.context}")
            if create_items:
                click.echo(f"  Created {len(uncatalogued)} OPEN queue items (project: librarian)")
    else:
        click.echo("  No uncatalogued directories found.")

    # Detect sibling git repos that haven't been initialised yet
    uninitialised = [
        d for d in root.iterdir()
        if d.is_dir() and (d / ".git").is_dir() and not (d / ".swarm").is_dir()
    ]
    if uninitialised:
        click.echo(f"\n  ⚠  Uninitialised git repos ({len(uninitialised)}) — no .swarm/ found:")
        for d in uninitialised:
            click.echo(f"    {d.name}/  →  cd {d.name} && swarm init")

    if not dry_run and uncatalogued:
        click.echo("\nRun 'swarm heal' to verify context alignment after cataloging.")


# ---------------------------------------------------------------------------
# swarm trail — gitignore-based visibility toggle
# ---------------------------------------------------------------------------

def _repo_gitignore(start: Path) -> Path | None:
    """Return the .gitignore at the nearest git root, or None."""
    p = start.resolve()
    for _ in range(8):
        if (p / ".git").is_dir():
            return p / ".gitignore"
        if p.parent == p:
            break
        p = p.parent
    return None


def _trail_is_invisible(gitignore: Path) -> bool:
    if not gitignore.exists():
        return False
    lines = [l.strip() for l in gitignore.read_text().splitlines()]
    return ".swarm/" in lines or ".swarm" in lines


def _set_trail_visibility(gitignore: Path, invisible: bool) -> str:
    """Add or remove .swarm/ from .gitignore. Returns a human-readable action."""
    lines: list[str] = []
    if gitignore.exists():
        lines = gitignore.read_text().splitlines()

    entries = {".swarm/", ".swarm"}
    if invisible:
        if any(l.strip() in entries for l in lines):
            return "already invisible"
        lines.append("")
        lines.append("# dot_swarm trail — remove to make visible (swarm trail visible)")
        lines.append(".swarm/")
        gitignore.write_text("\n".join(lines) + "\n")
        return "trail hidden (.swarm/ added to .gitignore)"
    else:
        new_lines = []
        removed = False
        skip_next_comment = False
        for line in lines:
            stripped = line.strip()
            if stripped == "# dot_swarm trail — remove to make visible (swarm trail visible)":
                skip_next_comment = True
                continue
            if skip_next_comment and stripped in entries:
                removed = True
                skip_next_comment = False
                continue
            if stripped in entries:
                removed = True
                continue
            skip_next_comment = False
            new_lines.append(line)
        if not removed:
            return "already visible (no .swarm/ entry found)"
        gitignore.write_text("\n".join(new_lines) + "\n")
        return "trail visible (.swarm/ removed from .gitignore)"


@cli.group(name="trail")
@click.pass_context
def trail(ctx: click.Context) -> None:
    """Manage .swarm/ trail visibility in git.

    The trail is invisible by default — your swarm state stays private
    unless you explicitly share it.

    \b
    swarm trail status     show current visibility
    swarm trail invisible  hide .swarm/ from git (add to .gitignore)
    swarm trail visible    share .swarm/ with git (remove from .gitignore)
    """


@trail.command(name="status")
@click.pass_context
def trail_status(ctx: click.Context) -> None:
    """Show whether the .swarm/ trail is visible or hidden in git."""
    path = Path(ctx.obj["path"]).resolve()
    gi = _repo_gitignore(path)
    if gi is None:
        click.echo("⚠  No git repository found — trail visibility not applicable.")
        return
    hidden = _trail_is_invisible(gi)
    state = "invisible (private)" if hidden else "visible (shared with git)"
    click.echo(f"Trail: {state}")
    click.echo(f"  .gitignore: {gi}")
    if not hidden:
        click.echo("  Run 'swarm trail invisible' to hide .swarm/ from git.")


@trail.command(name="invisible")
@click.pass_context
def trail_invisible(ctx: click.Context) -> None:
    """Hide .swarm/ from git by adding it to .gitignore."""
    path = Path(ctx.obj["path"]).resolve()
    gi = _repo_gitignore(path)
    if gi is None:
        click.echo("⚠  No git repository found. Create .gitignore manually.", err=True)
        return
    result = _set_trail_visibility(gi, invisible=True)
    click.echo(f"✓ {result}")
    click.echo("  Your .swarm/ trail will not be committed unless you run 'swarm trail visible'.")


@trail.command(name="visible")
@click.pass_context
def trail_visible(ctx: click.Context) -> None:
    """Share .swarm/ with git by removing it from .gitignore."""
    path = Path(ctx.obj["path"]).resolve()
    gi = _repo_gitignore(path)
    if gi is None:
        click.echo("⚠  No git repository found.", err=True)
        return
    result = _set_trail_visibility(gi, invisible=False)
    click.echo(f"✓ {result}")
    click.echo("  Run 'git add .swarm/' to stage the trail for your next commit.")


@trail.command(name="claims")
@click.option("--item", "item_id", default=None,
              help="Filter to one work item ID (default: all)")
@click.pass_context
def trail_claims(ctx: click.Context, item_id: str | None) -> None:
    """Show the append-only claim-trail history for an item (or all items).

    The .swarm/claims/ directory holds one immutable JSON record per
    state transition. Listing them in chronological order reconstructs
    every claim, partial, competition, and release that has ever
    happened to a work item — without ever mutating prior records.
    """
    from .operations import read_claims
    paths = _get_paths(ctx.obj["path"])
    claims = read_claims(paths)
    if item_id:
        claims = [c for c in claims if c.item_id == item_id]
    if not claims:
        target = item_id or "any item"
        click.echo(f"No claim records found for {target}.")
        return
    claims.sort(key=lambda c: c.timestamp)
    click.echo(f"{'Timestamp':<18}  {'Item':<10}  {'Agent':<20}  State    Note")
    click.echo("-" * 80)
    for c in claims:
        ts = c.timestamp.strftime("%Y-%m-%dT%H:%MZ")
        note = (c.note[:32] + "…") if len(c.note) > 32 else c.note
        click.echo(f"{ts:<18}  {c.item_id:<10}  {c.agent_id[:20]:<20}  "
                   f"{c.state.value:<8} {note}")
    click.echo(f"\n{len(claims)} record(s).")


# ---------------------------------------------------------------------------
# swarm compete — multi-claimant management
# ---------------------------------------------------------------------------

@cli.group(name="compete")
@click.pass_context
def compete(ctx: click.Context) -> None:
    """Manage competing claims on a single work item.

    Two or more agents may simultaneously hold an active claim on the same
    item (state COMPETING). The trail records every claimant; once the
    work has been compared, a supervisor or inspector picks a winner and
    the losing claims are recorded as withdrawn.

    \b
    swarm compete list <id>            show every active competitor
    swarm compete winner <id> <agent>  promote one competitor to CLAIMED
    """


@compete.command(name="list")
@click.argument("item_id")
@click.pass_context
def compete_list(ctx: click.Context, item_id: str) -> None:
    """Show every agent currently holding an active competing claim."""
    from .operations import competitors_for
    paths = _get_paths(ctx.obj["path"])
    rivals = competitors_for(paths, item_id)
    if not rivals:
        click.echo(f"No active competitors on [{item_id}].")
        return
    click.echo(f"Active competitors on [{item_id}] ({len(rivals)}):")
    for c in rivals:
        ts = c.timestamp.strftime("%Y-%m-%dT%H:%MZ")
        proof = f"  proof: {c.proof}" if c.proof else ""
        click.echo(f"  · {c.agent_id}  [{c.state.value}]  {ts}{proof}")


@compete.command(name="winner")
@click.argument("item_id")
@click.argument("winner_agent")
@click.option("--reason", default="", help="Why this competitor won")
@click.pass_context
def compete_winner(ctx: click.Context, item_id: str, winner_agent: str, reason: str) -> None:
    """Promote one competitor to CLAIMED winner; mark others as withdrawn."""
    from .operations import promote_competitor
    paths = _get_paths(ctx.obj["path"])
    try:
        winner, losers = promote_competitor(paths, item_id, winner_agent, reason=reason)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"✓ Promoted: {winner.agent_id} on [{item_id}] (state CLAIMED)")
    if losers:
        click.echo(f"  Withdrawn: {', '.join(l.agent_id for l in losers)}")
    if reason:
        click.echo(f"  Reason: {reason}")


# ---------------------------------------------------------------------------
# swarm seal — hidden-in-plain-sight stigmergic authentication
# ---------------------------------------------------------------------------

@cli.group(name="seal")
@click.pass_context
def seal_group(ctx: click.Context) -> None:
    """Sign and verify HMAC seals embedded inline in coordination files.

    A seal is a short HTML comment appended to coordination text — it
    looks like ordinary markdown to anyone reading the file, but
    cryptographically authenticates the writer to anyone holding the
    swarm signing key. Foreign or forged content fails verification.

    \b
    swarm seal sign <file> --agent <id>   stamp a file with a seal
    swarm seal verify [--path PATH]       verify every sealed file
    swarm seal check <file>               verify one file
    """


@seal_group.command(name="sign")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False))
@click.option("--agent", default=None, help="Agent ID (default: $SWARM_AGENT_ID or human-$USER)")
@click.pass_context
def seal_sign(ctx: click.Context, file_path: str, agent: str | None) -> None:
    """Append (or refresh) a seal on the given coordination file."""
    from . import seals as _seals
    paths = _get_paths(ctx.obj["path"])
    agent_id = agent or _default_agent()
    p = Path(file_path)
    text = p.read_text(encoding="utf-8")
    try:
        sealed = _seals.seal_content(text, agent_id, paths.root)
    except FileNotFoundError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    p.write_text(sealed, encoding="utf-8")
    click.echo(f"✓ Sealed {p} for agent {agent_id}")


@seal_group.command(name="check")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def seal_check(ctx: click.Context, file_path: str) -> None:
    """Verify a single file's seal status."""
    from . import seals as _seals
    paths = _get_paths(ctx.obj["path"])
    text = Path(file_path).read_text(encoding="utf-8")
    result = _seals.verify_content(text, paths.root)
    icon = {"VALID": "✓", "INVALID": "✗", "MISSING": "·", "UNKEYED": "?"}[result.status.value]
    click.echo(f"  {icon} {file_path} — {result.status.value}"
               + (f" (agent={result.agent})" if result.agent else ""))
    if result.status.value == "INVALID":
        sys.exit(2)


@seal_group.command(name="verify")
@click.pass_context
def seal_verify(ctx: click.Context) -> None:
    """Sweep .swarm/ for seals and report status of every coordination file."""
    from . import seals as _seals
    paths = _get_paths(ctx.obj["path"])
    reports = _seals.scan_swarm_seals(paths.root)
    if not reports:
        click.echo("No coordination files found under .swarm/.")
        return

    counts = {"VALID": 0, "INVALID": 0, "MISSING": 0, "UNKEYED": 0}
    for r in reports:
        counts[r.result.status.value] = counts.get(r.result.status.value, 0) + 1
        icon = {"VALID": "✓", "INVALID": "✗", "MISSING": "·", "UNKEYED": "?"}[r.result.status.value]
        agent = f" (agent={r.result.agent})" if r.result.agent else ""
        click.echo(f"  {icon} {r.label} — {r.result.status.value}{agent}")

    click.echo(
        f"\n  {counts['VALID']} valid, {counts['INVALID']} invalid, "
        f"{counts['MISSING']} unsealed, {counts['UNKEYED']} unkeyed"
    )
    if counts["INVALID"]:
        click.echo("\n  ⚠  Invalid seals detected — run 'swarm heal' to quarantine.")
        sys.exit(2)


# ---------------------------------------------------------------------------
# swarm key — AEAD-encrypted trail under the per-swarm symmetric key (SWC-046)
# ---------------------------------------------------------------------------

@cli.group(name="key")
@click.pass_context
def key_group(ctx: click.Context) -> None:
    """Manage the swarm key — confidentiality for the coordination trail.

    The swarm key is a per-swarm symmetric AEAD key (ChaCha20-Poly1305).
    When present, trail.log entries are written as opaque envelopes
    rather than plaintext JSON — the medium becomes shared but the
    *language* of the medium is not.

    Requires the optional 'cryptography' package:
      pip install 'dot-swarm[crypto]'

    \b
    swarm key init                 generate a swarm key (idempotent)
    swarm key status               show key fingerprint, algorithm, age
    swarm key rotate               new key + re-seal every existing envelope
    swarm key seal <file>          encrypt one coordination file in-place
    swarm key open <file>          decrypt one coordination file in-place
    """


@key_group.command(name="init")
@click.pass_context
def key_init(ctx: click.Context) -> None:
    """Generate a swarm key (idempotent — does nothing if one already exists)."""
    from . import vault as _vault
    paths = _get_paths(ctx.obj["path"])
    try:
        meta = _vault.generate_swarm_key(paths.root)
    except _vault.CryptoUnavailable as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"✓ Swarm key ready  [fingerprint {meta.fingerprint}]")
    click.echo(f"  Algorithm: {meta.algorithm}")
    click.echo(f"  Created:   {meta.created}")
    click.echo("  Trail entries from now on will be sealed under this key.")
    click.echo("  Back up .swarm/.swarm_key — losing it makes the trail unreadable.")


@key_group.command(name="status")
@click.pass_context
def key_status(ctx: click.Context) -> None:
    """Show whether a swarm key is present and its public metadata."""
    from . import vault as _vault
    paths = _get_paths(ctx.obj["path"])
    if not _vault.has_swarm_key(paths.root):
        click.echo("No swarm key. Run 'swarm key init' to generate one.")
        return
    try:
        meta = _vault.load_swarm_key_metadata(paths.root)
    except FileNotFoundError:
        click.echo("Swarm key file present but metadata missing — run 'swarm key init'.", err=True)
        sys.exit(1)
    click.echo(f"Algorithm:   {meta.algorithm}")
    click.echo(f"Fingerprint: {meta.fingerprint}")
    click.echo(f"Created:     {meta.created}")
    if meta.rotated_at:
        click.echo(f"Last rotate: {meta.rotated_at}")
    if (paths.root / _vault.SWARM_KEY_OLD_FILE).exists():
        click.echo("Previous key still on disk (.swarm_key.old). "
                   "Delete once you've confirmed the rotation.")


@key_group.command(name="rotate")
@click.confirmation_option(
    prompt="Rotate the swarm key? This re-seals every existing envelope."
)
@click.pass_context
def key_rotate(ctx: click.Context) -> None:
    """Generate a new swarm key and re-seal every existing envelope under it."""
    from . import vault as _vault
    paths = _get_paths(ctx.obj["path"])
    try:
        meta, result = _vault.rotate_swarm_key(paths.root)
    except (FileNotFoundError, _vault.CryptoUnavailable) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"✓ Rotated. New fingerprint: {meta.fingerprint}")
    click.echo(
        f"  Re-sealed {result.lines_rewrapped} envelope(s) "
        f"across {result.files_rewrapped} file(s); "
        f"{result.failed} failed; "
        f"{result.skipped_plaintext} plaintext line(s) untouched."
    )
    click.echo("  Previous key retained at .swarm_key.old — delete after verifying.")


@key_group.command(name="seal")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def key_seal(ctx: click.Context, file_path: str) -> None:
    """Encrypt a single coordination file in-place under the swarm key."""
    from . import vault as _vault
    paths = _get_paths(ctx.obj["path"])
    try:
        _vault.seal_file(Path(file_path), paths.root)
    except (FileNotFoundError, _vault.CryptoUnavailable) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"✓ Sealed {file_path} (envelope on disk).")


@key_group.command(name="open")
@click.argument("file_path", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def key_open(ctx: click.Context, file_path: str) -> None:
    """Decrypt a single sealed file in-place to its original plaintext."""
    from . import vault as _vault
    paths = _get_paths(ctx.obj["path"])
    try:
        _vault.open_file(Path(file_path), paths.root)
    except (FileNotFoundError, _vault.CryptoUnavailable, _vault.WrongKey) as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"✓ Opened {file_path} (plaintext restored).")


# ---------------------------------------------------------------------------
# swarm agent — per-agent Ed25519 identity (SWC-048)
# ---------------------------------------------------------------------------

@cli.group(name="agent")
@click.pass_context
def agent_group(ctx: click.Context) -> None:
    """Manage per-agent Ed25519 identities — who can sign as whom.

    The swarm-wide .signing_key (see 'swarm key') is ONE shared secret —
    any holder can sign as ANY agent_id. Per-agent identity fixes that:
    each agent gets its OWN keypair, so compromising one agent's key can
    never forge another agent's signature.

    The PRIVATE key never lives in .swarm/ — it stays local to the agent
    (default ~/.dot_swarm/keys/<agent_id>.key, override with
    DOT_SWARM_AGENT_KEY_DIR). Only the PUBLIC key is published into the
    shared, git-tracked .swarm/agents/ registry.

    Requires the optional 'cryptography' package:
      pip install 'dot-swarm[crypto]'

    \b
    swarm agent init <id>          generate + register this agent's keypair
    swarm agent list               show every agent registered in this swarm
    swarm agent show <id>          show one agent's public key + fingerprint
    """


@agent_group.command(name="init")
@click.argument("agent_id")
@click.pass_context
def agent_init(ctx: click.Context, agent_id: str) -> None:
    """Generate (if needed) and register AGENT_ID's Ed25519 keypair.

    Idempotent — re-running for an agent that already has a key just
    re-publishes the same public key (never rotates silently).
    """
    from . import identity as _identity
    paths = _get_paths(ctx.obj["path"])
    try:
        ident = _identity.generate_agent_identity(agent_id)
        dest = _identity.register_agent(paths.root, ident)
    except _identity.CryptoUnavailable as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    click.echo(f"✓ Identity ready for '{agent_id}'  [fingerprint {ident.fingerprint}]")
    click.echo(f"  Public key registered: {dest.relative_to(paths.root.parent)}")
    click.echo(f"  Private key: {_identity.default_key_dir() / (agent_id + '.key')}")
    click.echo("  Back this up and NEVER commit it — losing it means re-registering")
    click.echo("  under a new key, and everyone else's signature checks stay unaffected.")


@agent_group.command(name="list")
@click.pass_context
def agent_list(ctx: click.Context) -> None:
    """List every agent with a registered public key in this swarm."""
    from . import identity as _identity
    paths = _get_paths(ctx.obj["path"])
    agents = _identity.list_registered_agents(paths.root)
    if not agents:
        click.echo("No agents registered. Run 'swarm agent init <id>' to add one.")
        return
    for a in agents:
        click.echo(f"  {a.agent_id:20} {a.algorithm}  fingerprint={a.fingerprint}  created={a.created}")


@agent_group.command(name="show")
@click.argument("agent_id")
@click.pass_context
def agent_show(ctx: click.Context, agent_id: str) -> None:
    """Show one agent's registered public key and fingerprint."""
    from . import identity as _identity
    paths = _get_paths(ctx.obj["path"])
    ident = _identity.load_registered_agent(paths.root, agent_id)
    if ident is None:
        click.echo(f"No registered identity for '{agent_id}'.", err=True)
        sys.exit(1)
    click.echo(f"agent_id:    {ident.agent_id}")
    click.echo(f"algorithm:   {ident.algorithm}")
    click.echo(f"fingerprint: {ident.fingerprint}")
    click.echo(f"public_key:  {ident.public_key}")
    click.echo(f"created:     {ident.created}")


# ---------------------------------------------------------------------------
# swarm comment — threaded, signed discussion on a work item (SWC-051)
# ---------------------------------------------------------------------------

@cli.command(name="comment")
@click.argument("item_id")
@click.argument("body")
@click.option("--reply-to", "reply_to", default="", help="comment_id this replies to")
@click.option("--agent", default=None, help="Agent ID override")
@click.pass_context
def comment_cmd(ctx: click.Context, item_id: str, body: str, reply_to: str, agent: str | None) -> None:
    """Add a comment to ITEM_ID's discussion thread.

    Signed with the commenting agent's Ed25519 key (see 'swarm agent init')
    when one is registered locally; recorded unsigned otherwise.
    """
    from . import comments as _comments
    from . import identity as _identity
    paths = _get_paths(ctx.obj["path"])
    agent_id = agent or _default_agent()
    c = _comments.add_comment(paths, item_id, agent_id, body, in_reply_to=reply_to)
    signed_note = "" if c.signature == _identity.UNSIGNED else "  [signed]"
    click.echo(f"✓ Comment {c.comment_id} on [{item_id}] by {agent_id}{signed_note}")


@cli.command(name="comments")
@click.argument("item_id")
@click.option("--verify", is_flag=True, help="Show signature verification status per comment")
@click.pass_context
def comments_cmd(ctx: click.Context, item_id: str, verify: bool) -> None:
    """Show ITEM_ID's discussion thread in chronological order."""
    from . import comments as _comments
    from . import identity as _identity
    paths = _get_paths(ctx.obj["path"])
    thread = _comments.read_comments(paths, item_id)
    if not thread:
        click.echo(f"No comments on [{item_id}].")
        return
    known_ids = {c.comment_id for c in thread}
    for c in thread:
        reply = ""
        if c.in_reply_to:
            reply = f"  (reply to {c.in_reply_to}{'' if c.in_reply_to in known_ids else ', orphaned'})"
        status = ""
        if verify:
            if c.signature == _identity.UNSIGNED:
                status = "  [unsigned]"
            else:
                ok = _comments.verify_comment(paths, c)
                status = "  [✓ verified]" if ok else "  [✗ signature does not verify]"
        click.echo(f"[{c.comment_id}] {c.agent_id} @ {c.timestamp}{reply}{status}")
        click.echo(f"    {c.body}")


# ---------------------------------------------------------------------------
# swarm configure
# ---------------------------------------------------------------------------

@cli.command()
def configure() -> None:
    """Interactive wizard: choose an LLM interface and configure it.

    Supported interfaces:
      bedrock   — AWS Bedrock (default, Amazon Nova Micro). Requires boto3 + AWS creds.
      claude    — Claude Code CLI  (https://claude.ai/code)
      gemini    — Gemini CLI       (https://github.com/google-gemini/gemini-cli)
      opencode  — OpenCode         (https://opencode.ai)

    For claude/gemini/opencode, no API keys are managed here — the CLI tools
    handle their own auth. Just install them and they work.
    """
    from . import bedrock as _bedrock

    _INTERFACES = ["bedrock", "ollama", "claude", "gemini", "opencode"]

    click.echo("dot_swarm AI interface configuration\n")
    click.echo("Available interfaces:")
    for iface in _INTERFACES:
        detected = " (detected)" if shutil.which(iface) or iface in ("bedrock", "ollama") else ""
        click.echo(f"  {iface}{detected}")
    click.echo()

    cfg = _bedrock.load_config()
    current = cfg.get("interface", "bedrock")
    interface = click.prompt("  Default interface", default=current,
                             type=click.Choice(_INTERFACES))

    if interface == "ollama":
        host  = click.prompt("  Ollama host",  default=cfg.get("ollama_host",  _bedrock.DEFAULT_OLLAMA_HOST))
        model = click.prompt("  Ollama model", default=cfg.get("ollama_model", _bedrock.DEFAULT_OLLAMA_MODEL))

        if click.confirm("\n  Test connectivity now?", default=True):
            click.echo("  Connecting...", nl=False)
            try:
                import urllib.request
                urllib.request.urlopen(f"{host.rstrip('/')}/api/tags", timeout=5)
                click.echo(" ✓ OK")
            except Exception as e:
                click.echo(f" ✗\n  Could not reach Ollama at {host}.")
                click.echo("  Make sure Ollama is running: ollama serve")
                click.echo(f"  Pull a model first:          ollama pull {model}")

        _bedrock.save_config(cfg["model"], cfg["region"], interface=interface,
                             ollama_host=host, ollama_model=model)

    elif interface == "bedrock":
        try:
            import boto3  # noqa: F401
        except ImportError:
            click.echo("\nError: boto3 not installed. Run: pip install 'dot-swarm[ai]'", err=True)
            raise SystemExit(1)

        click.echo("\nBedrock configuration")
        click.echo("  Credentials: boto3 chain (env vars → ~/.aws/credentials → IAM role)\n")
        model  = click.prompt("  Bedrock model",  default=cfg["model"])
        region = click.prompt("  AWS region",     default=cfg["region"])

        if click.confirm("\n  Test connectivity now?", default=True):
            click.echo("  Connecting...", nl=False)
            try:
                client = _bedrock.get_bedrock_client(region)
                ok, msg = _bedrock.test_connectivity(client, model)
            except Exception as e:
                ok, msg = False, str(e)

            if ok:
                click.echo(" ✓ OK")
            else:
                click.echo(" ✗\n")
                if "NoCredentialsError" in msg or "CredentialRetrievalError" in msg:
                    click.echo("  No AWS credentials found. Options:")
                    click.echo("    aws configure                (interactive)")
                    click.echo("    export AWS_ACCESS_KEY_ID=... (env var)")
                    click.echo("    export AWS_SECRET_ACCESS_KEY=...")
                elif "AccessDeniedException" in msg or "AuthorizationError" in msg:
                    click.echo("  Access denied. Check:")
                    click.echo("    1. IAM policy: bedrock:InvokeModel on the model ARN")
                    click.echo("    2. AWS Console → Bedrock → Model access → enable Amazon Nova Micro")
                elif "EndpointResolution" in msg or "Connection" in msg:
                    click.echo(f"  Connection failed — is region '{region}' correct?")
                else:
                    click.echo(f"  Error: {msg[:200]}")

        _bedrock.save_config(model, region, interface=interface)
    else:
        if not shutil.which(interface):
            click.echo(f"\n  Warning: '{interface}' not found in PATH.")
            if interface == "claude":
                click.echo("  Install: https://claude.ai/code")
            elif interface == "gemini":
                click.echo("  Install: https://github.com/google-gemini/gemini-cli")
            elif interface == "opencode":
                click.echo("  Install: https://opencode.ai")
        else:
            click.echo(f"\n  '{interface}' found at {shutil.which(interface)} ✓")
        _bedrock.save_config(cfg["model"], cfg["region"], interface=interface)

    click.echo(f"\n  Config saved to {_bedrock.CONFIG_PATH}")
    click.echo(f"  Run 'swarm ai \"what should I work on next?\"' to try it out.")
    click.echo(f"  Run 'swarm session' to launch an interactive {interface} session.")


# ---------------------------------------------------------------------------
# swarm ai
# ---------------------------------------------------------------------------

@cli.command(name="ai")
@click.argument("instruction")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation and execute immediately")
@click.option("--agent", "agent_id", default=None, help="Agent ID override")
@click.option("--limit", "context_limit", default=1200, help="Approx token limit for context bundle (default: 1200)")
@click.option("--via", "interface", default=None,
              type=click.Choice(["bedrock", "ollama", "claude", "gemini", "opencode"]),
              help="LLM backend to use (default: from swarm configure, or bedrock)")
@click.option("--chain", is_flag=True, default=False,
              help="Auto-chain: keep invoking AI until no more write operations are proposed")
@click.option("--max-steps", default=5,
              help="Maximum chain steps when --chain is active (default: 5)")
@click.pass_context
def ai_cmd(ctx: click.Context, instruction: str, yes: bool, agent_id: str | None,
           context_limit: int, interface: str | None,
           chain: bool, max_steps: int) -> None:
    """Translate a natural language instruction into .swarm/ operations.

    With --chain, the AI is re-invoked after each set of operations using
    the refreshed .swarm/ context until it produces no further write ops
    (i.e. the workflow is complete) or --max-steps is reached.

    Examples:
      swarm ai "mark ORG-009 as done, blog service is fixed"
      swarm ai "add three items for OAuth2: discovery, token exchange, refresh"
      swarm ai "what should I work on next?"
      swarm ai "write a memory entry: chose NATS over Kafka for native async"
      swarm ai "update my focus to the markets ASGI fix" --yes
      swarm ai "what should I work on?" --via claude
      swarm ai "add a queue item for OAuth" --via gemini
      swarm ai "run the OAuth2 workflow end to end" --chain --yes
      swarm ai "implement auth, then markets, then geo" --chain --max-steps 9 --yes
    """
    from . import bedrock as _bedrock
    from . import ai_ops as _ai

    paths    = _get_paths(ctx.obj["path"])
    agent    = agent_id or _default_agent()
    cfg      = _bedrock.load_config()
    resolved = interface or cfg.get("interface", "bedrock")

    def _invoke(user_msg: str) -> dict:
        if resolved == "bedrock":
            try:
                import boto3  # noqa: F401
            except ImportError:
                click.echo("Error: boto3 not installed. Run: pip install 'dot-swarm[ai]'", err=True)
                raise SystemExit(1)
            client = _bedrock.get_bedrock_client(cfg["region"])
            return _ai.invoke_ai(client, cfg["model"], user_msg)
        if resolved == "ollama":
            return _ai.invoke_via_ollama(
                cfg.get("ollama_host",  _bedrock.DEFAULT_OLLAMA_HOST),
                cfg.get("ollama_model", _bedrock.DEFAULT_OLLAMA_MODEL),
                user_msg,
            )
        return _ai.invoke_via_cli(resolved, user_msg)

    step = 0
    current_instruction = instruction
    total_ops_executed = 0

    while True:
        step += 1
        if chain and step > 1:
            click.echo(f"\n── Chain step {step} ──────────────────────────────────────")

        context  = _ai.build_context_bundle(paths, context_limit=context_limit)
        user_msg = f"Instruction: {current_instruction}\n\n{context}"

        try:
            result = _invoke(user_msg)
        except FileNotFoundError as e:
            click.echo(f"Error: {e}", err=True)
            raise SystemExit(1)
        except Exception as e:
            kind = type(e).__name__
            if "NoCredentials" in kind or "CredentialRetrieval" in kind:
                click.echo("Error: No AWS credentials. Run 'swarm configure' or set AWS_* env vars.", err=True)
            elif "AccessDenied" in str(e) or "Authorization" in str(e):
                click.echo("Error: Bedrock access denied. Run 'swarm configure' to troubleshoot.", err=True)
            else:
                click.echo(f"Error: {kind}: {e}", err=True)
            raise SystemExit(1)

        commentary = result.get("commentary", "")
        ops        = result.get("operations", [])

        if not ops:
            click.echo(f"\n{commentary}")
            break

        respond_ops = [o for o in ops if o.get("op") == "respond"]
        write_ops   = [o for o in ops if o.get("op") != "respond"]

        for op in respond_ops:
            click.echo(f"\n{op['message']}")

        if not write_ops:
            break

        click.echo("\n" + _ai.format_preview(commentary, write_ops))

        if not yes:
            click.echo()
            if not click.confirm("  Execute these operations?", default=False):
                click.echo("  Aborted.")
                break

        click.echo()
        exec_results = _ai.execute_operations(paths, write_ops, agent)
        for r in exec_results:
            click.echo(r)
        total_ops_executed += len(write_ops)

        # Sign and record the batch in the pheromone trail
        try:
            from . import signing as _sign
            record = _sign.sign_operation(
                paths.root, "ai_batch", agent,
                {"step": step, "ops": [o.get("op") for o in write_ops]},
            )
            _sign.append_trail(paths.root, record)
        except Exception:
            pass

        if not chain:
            break

        if step >= max_steps:
            click.echo(f"\n  ⚠  Reached --max-steps ({max_steps}). Stopping chain.")
            break

        # For next chain step, ask AI what to do next given the updated state
        current_instruction = (
            f"Continue: {instruction}. "
            f"Previous step executed {len(write_ops)} operation(s). "
            "What should happen next? If the work is complete, respond with a summary."
        )

    if chain and total_ops_executed > 0:
        click.echo(f"\n  ✓ Chain complete — {total_ops_executed} operation(s) across {step} step(s).")


# ---------------------------------------------------------------------------
# swarm session
# ---------------------------------------------------------------------------

_CLI_CANDIDATES = ["claude", "gemini", "opencode"]

_CLI_INSTALL_HINTS = {
    "claude":   "https://claude.ai/code",
    "gemini":   "https://github.com/google-gemini/gemini-cli",
    "opencode": "https://opencode.ai",
}

_CLI_NONINTERACTIVE_FLAGS = {
    "claude":   ["-p"],
    "gemini":   ["-p"],
    "opencode": ["run"],
}


@cli.command(name="session")
@click.option("--with", "interface",
              type=click.Choice(["auto", "claude", "gemini", "opencode"]),
              default="auto",
              help="LLM CLI to launch (default: auto-detect from PATH)")
@click.argument("prompt", required=False, default=None)
@click.pass_context
def session_cmd(ctx: click.Context, interface: str, prompt: str | None) -> None:
    """Launch an interactive LLM session seeded with .swarm/ context.

    With no PROMPT, opens an interactive session in the division root.
    CLAUDE.md in the root automatically loads .swarm/ context for Claude Code.
    For gemini and opencode a CURRENT_SESSION.md context file is written first.

    With a PROMPT argument, runs a single non-interactive turn and prints output.

    Auto-detection order: claude → gemini → opencode.

    Examples:
      swarm session                          # interactive, auto-detect CLI
      swarm session --with gemini            # interactive, force gemini
      swarm session "what should I pick up?" # seeded single-turn
      swarm session --with claude "summarise the active queue"
    """
    from . import ai_ops as _ai

    paths    = _get_paths(ctx.obj["path"])
    div_root = paths.root.parent

    # Resolve interface
    if interface == "auto":
        from . import bedrock as _bedrock
        cfg = _bedrock.load_config()
        preferred = cfg.get("interface", "bedrock")
        # If configured interface is a CLI tool, prefer it; else auto-detect
        if preferred in _CLI_CANDIDATES and shutil.which(preferred):
            interface = preferred
        else:
            interface = next((c for c in _CLI_CANDIDATES if shutil.which(c)), None)
        if not interface:
            click.echo(
                "No LLM CLI found in PATH. Install one of:\n"
                + "\n".join(f"  {k}: {v}" for k, v in _CLI_INSTALL_HINTS.items()),
                err=True,
            )
            raise SystemExit(1)
    elif not shutil.which(interface):
        hint = _CLI_INSTALL_HINTS.get(interface, "")
        click.echo(f"'{interface}' not found in PATH.  {hint}", err=True)
        raise SystemExit(1)

    bin_path = shutil.which(interface)

    # Session banner
    state_text  = paths.state.read_text().strip() if paths.state.exists() else ""
    focus_line  = next((l for l in state_text.splitlines() if "Current focus" in l), "")
    click.echo(f"  Division : {div_root.name}")
    if focus_line:
        click.echo(f"  {focus_line.strip()}")
    click.echo(f"  Interface: {interface}  ({bin_path})")
    click.echo()

    if prompt:
        # Single non-interactive turn: inject context + prompt
        context = _ai.build_context_bundle(paths)
        seeded  = (
            f"dot_swarm context for {div_root.name}:\n\n{context}"
            f"\n\n---\n\nUser: {prompt}"
        )
        flags = _CLI_NONINTERACTIVE_FLAGS.get(interface, ["-p"])
        os.execv(bin_path, [interface] + flags + [seeded])
    else:
        # Interactive: for non-claude tools write a context file first
        if interface != "claude":
            context   = _ai.build_context_bundle(paths)
            ctx_file  = paths.root / "CURRENT_SESSION.md"
            ctx_file.write_text(
                f"# dot_swarm Session Context — {div_root.name}\n\n"
                f"Read this file to understand the current state, then assist the user.\n\n"
                f"{context}\n"
            )
            rel = ctx_file.relative_to(div_root)
            click.echo(f"  Context written to {rel}")
            click.echo(f"  (Delete {rel} when done to keep the repo clean.)")
            click.echo()

        os.chdir(div_root)
        os.execv(bin_path, [interface])


# ---------------------------------------------------------------------------
# swarm setup-drift-check
# ---------------------------------------------------------------------------

@cli.command(name="setup-drift-check")
@click.option("--repo", default=None, metavar="OWNER/REPO",
              help="GitHub repo (default: detected from git remote)")
@click.option("--region", default=None, metavar="REGION",
              help="AWS region for Bedrock (default: us-east-1)")
@click.option("--commit", is_flag=True,
              help="Commit and push the workflow file after creating it")
@click.option("--model", default=None, metavar="MODEL_ID",
              help="Override Bedrock model ID (sets repo variable SWARM_BEDROCK_MODEL)")
@click.pass_context
def setup_drift_check(
    ctx: click.Context,
    repo: str | None,
    region: str | None,
    commit: bool,
    model: str | None,
) -> None:
    """Install the dot_swarm drift-check GitHub Actions workflow.

    Copies swarm-drift-check.yml to .github/workflows/, verifies AWS secrets
    exist via gh CLI, and optionally commits + pushes.

    Requires: gh CLI authenticated (run `gh auth login` if needed).
    """
    import shutil, subprocess

    repo_root = _find_git_root()
    if repo_root is None:
        click.echo("Error: not inside a git repository.", err=True)
        raise SystemExit(1)

    # --- Check gh CLI -------------------------------------------------------
    if not shutil.which("gh"):
        click.echo("Error: gh CLI not found. Install from https://cli.github.com/", err=True)
        raise SystemExit(1)

    # --- Detect repo if not provided ----------------------------------------
    if not repo:
        result = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            capture_output=True, text=True, cwd=repo_root,
        )
        if result.returncode != 0:
            click.echo("Error: could not detect repo. Pass --repo OWNER/REPO.", err=True)
            raise SystemExit(1)
        repo = result.stdout.strip()

    click.echo(f"Setting up drift check for: {repo}")

    # --- Check / set AWS secrets --------------------------------------------
    secrets_result = subprocess.run(
        ["gh", "secret", "list", "--repo", repo],
        capture_output=True, text=True,
    )
    existing_secrets = secrets_result.stdout

    needed = {
        "AWS_ACCESS_KEY_ID": "AWS access key ID",
        "AWS_SECRET_ACCESS_KEY": "AWS secret access key",
        "AWS_DEFAULT_REGION": f"AWS region (e.g. {region or 'us-east-1'})",
    }
    for secret_name, description in needed.items():
        if secret_name in existing_secrets:
            click.echo(f"  ✓ {secret_name} already set")
        else:
            value = click.prompt(f"  Enter {description} (or press Enter to skip)", default="", show_default=False)
            if value:
                subprocess.run(
                    ["gh", "secret", "set", secret_name, "--repo", repo, "--body", value],
                    check=True,
                )
                click.echo(f"  ✓ {secret_name} set")
            else:
                click.echo(f"  ⚠ {secret_name} skipped — add manually if needed")

    # --- Set model variable if requested ------------------------------------
    if model:
        subprocess.run(
            ["gh", "variable", "set", "SWARM_BEDROCK_MODEL", "--repo", repo, "--body", model],
            check=True,
        )
        click.echo(f"  ✓ SWARM_BEDROCK_MODEL set to {model}")

    # --- Copy workflow file -------------------------------------------------
    workflow_dest = repo_root / ".github" / "workflows" / "swarm-drift-check.yml"
    if workflow_dest.exists():
        click.echo(f"\n  ✓ {workflow_dest.relative_to(repo_root)} already exists")
    else:
        _install_drift_check_workflow(repo_root)

    # --- Commit and push if requested ---------------------------------------
    if commit:
        wf_rel = str(workflow_dest.relative_to(repo_root))
        subprocess.run(["git", "add", wf_rel], cwd=repo_root, check=True)
        subprocess.run(
            ["git", "commit", "-m", "chore: add dot_swarm drift-check workflow\n\nAuto-installed via `swarm setup-drift-check`"],
            cwd=repo_root, check=True,
        )
        subprocess.run(["git", "push"], cwd=repo_root, check=True)
        click.echo("  ✓ Committed and pushed")
    else:
        click.echo(f"\n  Workflow written. To activate:\n")
        click.echo(f"    git add .github/workflows/swarm-drift-check.yml")
        click.echo(f"    git commit -m 'chore: add dot_swarm drift-check workflow'")
        click.echo(f"    git push")

    click.echo(f"\nDone. The drift check will run on every merge to dev/prod in {repo}.")
    click.echo("See docs/DRIFT_CHECK_SETUP.md for Bedrock model access setup.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _install_drift_check_workflow(repo_root: Path) -> None:
    """Install swarm-drift-check.yml into .github/workflows/ if not already present."""
    import importlib.resources

    dest = repo_root / ".github" / "workflows" / "swarm-drift-check.yml"
    if dest.exists():
        return

    # Try to load the bundled template
    try:
        template_path = (
            Path(__file__).parent.parent.parent.parent.parent  # dot_swarm root
            / ".github" / "workflows" / "swarm-drift-check.yml"
        )
        if template_path.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(template_path.read_text())
            click.echo("  Created .github/workflows/swarm-drift-check.yml")
            return
    except Exception:
        pass

    click.echo(
        "  Note: drift-check workflow not found at package root. "
        "Copy dot_swarm/.github/workflows/swarm-drift-check.yml manually."
    )


def _find_git_root() -> Path | None:
    """Walk up from cwd to find the nearest .git/ directory."""
    p = Path.cwd()
    for _ in range(8):
        if (p / ".git").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def _create_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content)
        click.echo(f"  Created {path.name}")
    else:
        click.echo(f"  Skipped {path.name} (already exists)")


def _create_platform_shims(repo_root: Path) -> None:
    """Create CLAUDE.md, .windsurfrules, .cursorrules if they don't exist."""
    shims = {
        "CLAUDE.md": (
            "Before starting any work, read @.swarm/BOOTSTRAP.md and follow the protocol exactly.\n"
            "Active context: @.swarm/context.md | State: @.swarm/state.md | Queue: @.swarm/queue.md\n"
        ),
        ".windsurfrules": (
            "Before starting any task, read the file .swarm/BOOTSTRAP.md and follow its protocol.\n"
            "Do not begin work without claiming an item in .swarm/queue.md.\n"
        ),
        ".cursorrules": (
            "Always begin every session by reading .swarm/BOOTSTRAP.md.\n"
            "Follow the On Start, During Work, and On Stop sections exactly.\n"
        ),
    }
    for filename, content in shims.items():
        path = repo_root / filename
        _create_if_missing(path, content)

def _ensure_gitignore(swarm_dir: Path) -> None:
    """Ensure .swarm/.gitignore excludes private keys and quarantine dir."""
    gitignore = swarm_dir / ".gitignore"
    needed = [".signing_key", ".swarm_key", ".swarm_key.old", "quarantine/", "trail.log"]
    if gitignore.exists():
        existing = gitignore.read_text()
        missing = [line for line in needed if line not in existing]
        if missing:
            with gitignore.open("a") as fh:
                fh.write("\n" + "\n".join(missing) + "\n")
    else:
        gitignore.write_text("\n".join(needed) + "\n")


def _run_local_drift_check(ctx: click.Context, paths: "SwarmPaths") -> None:
    """Run a local AI-powered code-vs-docs drift check (same logic as GHA workflow)."""
    import subprocess
    from . import bedrock as _bedrock
    from . import ai_ops as _ai

    git_root = _find_git_root()
    if not git_root:
        click.echo("  ⚠  Not inside a git repository — skipping diff.")
        return

    try:
        diff_result = subprocess.run(
            ["git", "diff", "--stat", "HEAD~5..HEAD", "--", "."],
            capture_output=True, text=True, cwd=git_root, timeout=15,
        )
        diff_text = diff_result.stdout[:2000] if diff_result.returncode == 0 else "(no diff available)"
    except Exception:
        diff_text = "(could not retrieve git diff)"

    context = _ai.build_context_bundle(paths, context_limit=800)
    drift_prompt = (
        "You are a drift-check agent. Check whether the .swarm/ project state is "
        "aligned with recent code changes.\n\n"
        f"Recent git changes (last 5 commits):\n{diff_text}\n\n"
        f"Current .swarm/ state:\n{context}\n\n"
        "Report:\n"
        "1. Work items in queue.md that look stale given these code changes.\n"
        "2. Code changes not reflected in any queue item.\n"
        "3. State.md fields that appear out of date.\n"
        "Reply with a brief bullet list, or 'No drift detected.' if everything is aligned."
    )

    cfg = _bedrock.load_config()
    resolved = cfg.get("interface", "bedrock")

    try:
        if resolved == "bedrock":
            try:
                import boto3  # noqa: F401
                client = _bedrock.get_bedrock_client(cfg["region"])
                response = client.converse(
                    modelId=cfg["model"],
                    messages=[{"role": "user", "content": [{"text": drift_prompt}]}],
                    inferenceConfig={"maxTokens": 512, "temperature": 0.0},
                )
                result_text = response["output"]["message"]["content"][0]["text"]
            except Exception as e:
                click.echo(f"  ⚠  Bedrock drift check failed: {e}")
                return
        else:
            result = _ai.invoke_via_cli(resolved, drift_prompt)
            result_text = result.get("commentary", "") or str(result)
        click.echo(result_text)
    except Exception as e:
        click.echo(f"  ⚠  Drift check error: {e}")


if __name__ == "__main__":
    cli(obj={})
