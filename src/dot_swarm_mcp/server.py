"""dot_swarm MCP Server.

Exposes .swarm/ directory operations as MCP tools. Agents on any MCP-compatible
platform (Claude Code, Windsurf, Cursor, etc.) can call these tools to read and
write coordination state without manually editing markdown files.

Transport: stdio (default) — one server PROCESS per agent connection. This
matters for SWC-049 (see "Process identity" below): stdio gives each agent
its own process, so identity can be bound once at startup instead of trusted
per-call.

Configure in Claude Code (~/.claude/settings.json):
    {
      "mcpServers": {
        "dot-swarm": {
          "command": "python",
          "args": ["-m", "dot_swarm_mcp"],
          "env": {
            "SWARM_ROOT": "/path/to/oasis-x",
            "DOT_SWARM_AGENT_ID": "house"
          }
        }
      }
    }

Process identity (SWC-049)
---------------------------
Before this, every write tool (swarm_claim, swarm_done, ...) trusted whatever
``agent_id`` string the CALLER passed in the tool arguments — zero
verification. A confused or hostile caller (e.g. a hijacked prompt) could
simply pass ``agent_id: "yesman"`` and have the write attributed to an agent
it isn't.

Setting ``DOT_SWARM_AGENT_ID`` binds this process to one identity for its
whole lifetime. Once bound, that identity ALWAYS wins for write attribution —
any ``agent_id``/``inspector_id`` argument the caller supplies is ignored in
favor of the bound one (see ``_effective_agent_id``). If the bound agent has
also run ``swarm agent init`` (SWC-048, Ed25519), every write is additionally
signed with its private key and the signature is recorded in trail.log —
a caller can no longer just *say* it's a given agent; the process has to
cryptographically prove it.

Leaving ``DOT_SWARM_AGENT_ID`` unset preserves the exact old behavior
(caller-supplied ``agent_id``, unsigned) — existing configs and the local
CLI's own claim/done/etc. commands are unaffected.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import mcp.server.stdio
import mcp.types as types
from mcp.server import Server

from dot_swarm.models import Priority, SwarmPaths, ItemState, utcnow
from dot_swarm.operations import (
    add_item,
    append_memory,
    audit,
    block_item,
    claim_item,
    done_item,
    partial_item,
    read_queue,
    read_state,
    ready_items,
    reopen_item,
    write_state,
    _division_code_from_paths,
)
from dot_swarm.ai_ops import heal as _heal
from dot_swarm import identity as _identity
from dot_swarm import signing as _sign
from dot_swarm import comments as _comments

server = Server("dot-swarm")

# ---------------------------------------------------------------------------
# Process identity (SWC-049) — bound once, never trusted from a call argument
# ---------------------------------------------------------------------------

def _bound_agent_id() -> str | None:
    """The identity this server PROCESS is bound to, if any.

    Read live (not cached at import) so a single long-lived Python process
    running multiple test cases — or, in principle, a supervisor that
    re-execs with a new env — always sees the current binding. In normal
    stdio deployment this is set once before the process starts and never
    changes for its lifetime, which is what makes it trustworthy: the
    caller sends tool arguments over stdio, but never controls this
    process's environment.
    """
    return os.environ.get("DOT_SWARM_AGENT_ID", "").strip() or None


def _effective_agent_id(arguments: dict, field: str = "agent_id") -> str:
    """Resolve who a write operation is attributed to.

    A bound process identity always overrides whatever the caller passed —
    that's the whole fix. With no binding, falls back to the caller-supplied
    value, identical to pre-SWC-049 behavior.
    """
    bound = _bound_agent_id()
    if bound:
        return bound
    return arguments.get(field, "")


def _audit_write(paths: SwarmPaths, op: str, agent_id: str, payload: dict) -> None:
    """Best-effort signed trail entry for an MCP write. Never raises — a
    trail-logging failure must never block the actual operation it's
    recording. Layers a per-agent Ed25519 signature (SWC-048) on top of the
    existing swarm-wide HMAC trail record when this process has a bound,
    locally-keyed identity; falls back to the plain (unsigned-by-agent)
    record otherwise, so trail.log's existing schema and readers are
    unaffected either way."""
    try:
        record = _sign.sign_operation(paths.root, op, agent_id, payload)
        bound = _bound_agent_id()
        if bound and _identity.has_crypto():
            record["agent_signature"] = _identity.sign_agent(bound, payload)
        _sign.append_trail(paths.root, record)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def _resolve_paths(path: str = ".") -> SwarmPaths:
    """Find .swarm/ starting from the given path or SWARM_ROOT env var."""
    root = os.environ.get("SWARM_ROOT", ".")
    start = path if path != "." else root
    paths = SwarmPaths.find(start)
    if paths is None:
        raise ValueError(
            f"No .swarm/ directory found starting from {start}. "
            "Run 'swarm init' to initialize."
        )
    return paths


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="swarm_bootstrap",
            description=(
                "Read BOOTSTRAP.md — the universal agent protocol. Call this first "
                "in any session to get the current coordination protocol."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Working directory (default: SWARM_ROOT)"}
                },
            },
        ),
        types.Tool(
            name="swarm_context",
            description="Read context.md — the stable charter for this division or org.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Working directory"}
                },
            },
        ),
        types.Tool(
            name="swarm_state",
            description=(
                "Read or update state.md — the pheromone trail. "
                "Pass write=true with fields dict to update specific fields."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "write": {"type": "boolean", "description": "True to update state"},
                    "fields": {
                        "type": "object",
                        "description": (
                            "Fields to update. Keys: current_focus, active_items, "
                            "blockers, ready_for_pickup, handoff_note"
                        ),
                    },
                },
            },
        ),
        types.Tool(
            name="swarm_queue",
            description="Read work items from queue.md with optional filters.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "section": {
                        "type": "string",
                        "enum": ["active", "pending", "done", "all"],
                        "description": "Which section to read (default: all)",
                    },
                    "priority": {"type": "string", "description": "Filter by priority"},
                    "project": {"type": "string", "description": "Filter by project name"},
                },
            },
        ),
        types.Tool(
            name="swarm_claim",
            description="Atomically claim an OPEN work item. Updates queue.md and state.md.",
            inputSchema={
                "type": "object",
                "required": ["id", "agent_id"],
                "properties": {
                    "id": {"type": "string", "description": "Item ID e.g. ORG-002"},
                    "agent_id": {"type": "string", "description": "Your agent ID e.g. claude-code"},
                    "path": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="swarm_done",
            description="Mark a claimed work item as done. Updates queue.md and state.md.",
            inputSchema={
                "type": "object",
                "required": ["id", "agent_id"],
                "properties": {
                    "id": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "note": {"type": "string", "description": "Brief completion note"},
                    "next_focus": {"type": "string", "description": "What to set as next focus in state.md"},
                    "path": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="swarm_add",
            description="Add a new OPEN work item to queue.md with an auto-assigned ID.",
            inputSchema={
                "type": "object",
                "required": ["description"],
                "properties": {
                    "description": {"type": "string"},
                    "priority": {
                        "type": "string",
                        "enum": ["critical", "high", "medium", "low"],
                        "default": "medium",
                    },
                    "project": {"type": "string", "default": "misc"},
                    "notes": {"type": "string"},
                    "refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Reference pointers e.g. ['oasis-x/.swarm/queue.md#ORG-001']",
                    },
                    "depends": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Item IDs this depends on",
                    },
                    "division_code": {"type": "string", "description": "Override division code for ID"},
                    "path": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="swarm_append_memory",
            description="Append a decision entry to memory.md. Use for non-obvious decisions.",
            inputSchema={
                "type": "object",
                "required": ["topic", "decision", "why"],
                "properties": {
                    "topic": {"type": "string", "description": "Short topic label"},
                    "decision": {"type": "string"},
                    "why": {"type": "string"},
                    "tradeoff": {"type": "string", "description": "Trade-off accepted (optional)"},
                    "agent_id": {"type": "string"},
                    "path": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="swarm_audit",
            description="Check for drift: stale claims, blocked items, state.md staleness.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "since_hours": {
                        "type": "integer",
                        "description": "Stale threshold in hours (default: 48)",
                        "default": 48,
                    },
                },
            },
        ),
        types.Tool(
            name="swarm_partial",
            description="Re-claim an item or attach proof without completing it. Updates queue.md.",
            inputSchema={
                "type": "object",
                "required": ["id", "agent_id"],
                "properties": {
                    "id": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "note": {"type": "string"},
                    "proof": {"type": "string", "description": "Worker-supplied evidence (branch, commit, etc.)"},
                    "path": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="swarm_block",
            description="Mark an item as BLOCKED with a reason.",
            inputSchema={
                "type": "object",
                "required": ["id", "reason"],
                "properties": {
                    "id": {"type": "string"},
                    "reason": {"type": "string"},
                    "path": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="swarm_ready",
            description="List items that are OPEN and have all dependencies completed.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="swarm_inspect",
            description="Inspector role: verify worker proof and mark item as --pass (done) or --fail (reopen).",
            inputSchema={
                "type": "object",
                "required": ["id", "inspector_id", "status"],
                "properties": {
                    "id": {"type": "string"},
                    "inspector_id": {"type": "string"},
                    "status": {"type": "string", "enum": ["pass", "fail"]},
                    "reason": {"type": "string", "description": "Required if status is fail"},
                    "path": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="swarm_comment",
            description=(
                "Add a signed comment to a work item's discussion thread "
                "(SWC-051). Set reply_to to a comment_id to thread a reply."
            ),
            inputSchema={
                "type": "object",
                "required": ["id", "body"],
                "properties": {
                    "id": {"type": "string", "description": "Item ID e.g. ORG-002"},
                    "body": {"type": "string"},
                    "reply_to": {"type": "string", "description": "comment_id this replies to"},
                    "agent_id": {"type": "string", "description": "Your agent ID (ignored if this process has a bound identity — see SWC-049)"},
                    "path": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="swarm_comments",
            description="Read a work item's discussion thread in chronological order.",
            inputSchema={
                "type": "object",
                "required": ["id"],
                "properties": {
                    "id": {"type": "string"},
                    "path": {"type": "string"},
                },
            },
        ),
        types.Tool(
            name="swarm_heal",
            description="Run a full security scan, alignment check, and trail verification.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "fix": {"type": "boolean", "description": "True to quarantine high-risk files (use with caution)"},
                },
            },
        ),
        types.Tool(
            name="swarm_handoff",
            description=(
                "Render a handoff summary (current focus, active items, ready-for-pickup "
                "queue, context pointers) for the next agent picking up this swarm."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                },
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    path = arguments.get("path", ".")

    try:
        if name == "swarm_bootstrap":
            paths = _resolve_paths(path)
            content = paths.bootstrap.read_text() if paths.bootstrap.exists() else "(no BOOTSTRAP.md found)"
            return [types.TextContent(type="text", text=content)]

        elif name == "swarm_context":
            paths = _resolve_paths(path)
            content = paths.context.read_text() if paths.context.exists() else "(no context.md found)"
            return [types.TextContent(type="text", text=content)]

        elif name == "swarm_state":
            paths = _resolve_paths(path)
            if arguments.get("write") and arguments.get("fields"):
                fields = arguments["fields"]
                # Map camelCase / snake_case keys to display keys
                key_map = {
                    "current_focus": "Current focus",
                    "active_items": "Active items",
                    "blockers": "Blockers",
                    "ready_for_pickup": "Ready for pickup",
                    "handoff_note": "Handoff note",
                    "last_agent": "last_agent",
                }
                mapped = {key_map.get(k, k): v for k, v in fields.items()}
                write_state(paths, mapped)
            content = paths.state.read_text() if paths.state.exists() else "(no state.md found)"
            return [types.TextContent(type="text", text=content)]

        elif name == "swarm_queue":
            paths = _resolve_paths(path)
            active, pending, done = read_queue(paths)
            section = arguments.get("section", "all")
            items = []
            if section in ("active", "all"):
                items.extend(active)
            if section in ("pending", "all"):
                items.extend(pending)
            if section in ("done", "all"):
                items.extend(done)
            # Apply filters
            if prio := arguments.get("priority"):
                items = [i for i in items if i.priority.value == prio]
            if proj := arguments.get("project"):
                items = [i for i in items if i.project == proj]
            result = [
                {
                    "id": i.id,
                    "state": i.state.value,
                    "description": i.description,
                    "priority": i.priority.value,
                    "project": i.project,
                    "claimed_by": i.claimed_by,
                    "claimed_at": i.claimed_at.isoformat() if i.claimed_at else None,
                    "refs": i.refs,
                    "depends": i.depends,
                }
                for i in items
            ]
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "swarm_claim":
            paths = _resolve_paths(path)
            agent_id = _effective_agent_id(arguments)
            item = claim_item(paths, arguments["id"], agent_id)
            write_state(paths, {
                "Current focus": item.description[:100],
                "Active items": item.id,
                "last_agent": agent_id,
            })
            _audit_write(paths, "claim", agent_id, {"item_id": item.id})
            return [types.TextContent(type="text", text=f"Claimed [{item.id}]: {item.description}")]

        elif name == "swarm_done":
            paths = _resolve_paths(path)
            agent_id = _effective_agent_id(arguments)
            item = done_item(
                paths, arguments["id"], agent_id,
                arguments.get("note", "")
            )
            updates: dict = {"last_agent": agent_id}
            if nf := arguments.get("next_focus"):
                updates["Current focus"] = nf
                updates["Handoff note"] = nf
            write_state(paths, updates)
            _audit_write(paths, "done", agent_id, {"item_id": item.id, "note": arguments.get("note", "")})
            return [types.TextContent(type="text", text=f"Done [{item.id}]: {item.description}")]

        elif name == "swarm_add":
            paths = _resolve_paths(path)
            agent_id = _effective_agent_id(arguments)
            code = arguments.get("division_code") or _division_code_from_paths(paths)
            item = add_item(
                paths=paths,
                description=arguments["description"],
                division_code=code,
                priority=Priority(arguments.get("priority", "medium")),
                project=arguments.get("project", "misc"),
                notes=arguments.get("notes", ""),
                refs=arguments.get("refs"),
                depends=arguments.get("depends"),
            )
            _audit_write(paths, "add", agent_id or "unknown", {"item_id": item.id})
            return [types.TextContent(
                type="text",
                text=f"Added [{item.id}] ({item.priority.value}): {item.description}"
            )]

        elif name == "swarm_append_memory":
            paths = _resolve_paths(path)
            agent_id = _effective_agent_id(arguments) or "unknown"
            entry = append_memory(
                paths=paths,
                topic=arguments["topic"],
                decision=arguments["decision"],
                why=arguments["why"],
                tradeoff=arguments.get("tradeoff", ""),
                agent_id=agent_id,
            )
            _audit_write(paths, "append_memory", agent_id, {"topic": arguments["topic"]})
            return [types.TextContent(type="text", text=f"Appended to memory.md:\n{entry}")]

        elif name == "swarm_audit":
            paths = _resolve_paths(path)
            findings = audit(paths, stale_hours=arguments.get("since_hours", 48))
            if not findings:
                return [types.TextContent(type="text", text="No drift detected.")]
            return [types.TextContent(type="text", text=json.dumps(findings, indent=2))]

        elif name == "swarm_partial":
            paths = _resolve_paths(path)
            agent_id = _effective_agent_id(arguments)
            item = partial_item(
                paths, arguments["id"], agent_id,
                arguments.get("note", "")
            )
            if proof := arguments.get("proof"):
                item.proof = proof
                # Partial doesn't write queue.md by default in the new Claim model,
                # but we want to persist the proof if provided via MCP.
                # Since we are using the new claims logic, partial_item already wrote a claim.
                # If we need to update the claim with proof, we should handle that.
                from dot_swarm.models import Claim, ItemState
                from dot_swarm.operations import write_claim
                from datetime import datetime
                write_claim(paths, Claim(
                    item_id=item.id,
                    agent_id=agent_id,
                    state=ItemState.PARTIAL,
                    timestamp=utcnow(),
                    proof=proof,
                    note=arguments.get("note", "")
                ))

            _audit_write(paths, "partial", agent_id, {"item_id": item.id, "proof": arguments.get("proof", "")})
            return [types.TextContent(type="text", text=f"Updated [{item.id}] (PARTIAL): {item.description}")]

        elif name == "swarm_block":
            paths = _resolve_paths(path)
            agent_id = _bound_agent_id() or "unknown"
            item = block_item(paths, arguments["id"], arguments["reason"])
            _audit_write(paths, "block", agent_id, {"item_id": item.id, "reason": arguments["reason"]})
            return [types.TextContent(type="text", text=f"Blocked [{item.id}]: {arguments['reason']}")]

        elif name == "swarm_ready":
            paths = _resolve_paths(path)
            items = ready_items(paths)
            result = [
                {"id": i.id, "description": i.description, "priority": i.priority.value}
                for i in items
            ]
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "swarm_inspect":
            paths = _resolve_paths(path)
            status = arguments["status"]
            item_id = arguments["id"]
            inspector_id = _effective_agent_id(arguments, field="inspector_id")

            if status == "pass":
                item = done_item(paths, item_id, inspector_id, "Inspection PASSED")
                _audit_write(paths, "inspect_pass", inspector_id, {"item_id": item_id})
                return [types.TextContent(type="text", text=f"Inspection PASSED for [{item_id}]")]
            else:
                reason = arguments.get("reason", "No reason provided")
                item, exhausted = reopen_item(paths, item_id, inspector_id, reason)
                _audit_write(paths, "inspect_fail", inspector_id, {"item_id": item_id, "reason": reason})
                msg = f"Inspection FAILED for [{item_id}]: {reason}"
                if exhausted:
                    msg += " (Max retries exhausted, item BLOCKED)"
                return [types.TextContent(type="text", text=msg)]

        elif name == "swarm_comment":
            paths = _resolve_paths(path)
            agent_id = _effective_agent_id(arguments) or "unknown"
            c = _comments.add_comment(
                paths, arguments["id"], agent_id, arguments["body"],
                in_reply_to=arguments.get("reply_to", ""),
            )
            _audit_write(paths, "comment", agent_id, {"item_id": arguments["id"], "comment_id": c.comment_id})
            return [types.TextContent(type="text", text=f"Comment {c.comment_id} on [{arguments['id']}] by {agent_id}")]

        elif name == "swarm_comments":
            paths = _resolve_paths(path)
            thread = _comments.read_comments(paths, arguments["id"])
            result = [c.to_dict() for c in thread]
            return [types.TextContent(type="text", text=json.dumps(result, indent=2))]

        elif name == "swarm_heal":
            paths = _resolve_paths(path)
            findings = _heal(paths, fix=arguments.get("fix", False))
            return [types.TextContent(type="text", text=json.dumps(findings, indent=2))]

        elif name == "swarm_handoff":
            paths = _resolve_paths(path)
            state = read_state(paths)
            active, pending, _ = read_queue(paths)
            from datetime import datetime
            name_str = paths.root.parent.name
            now = utcnow().strftime("%Y-%m-%dT%H:%MZ")
            lines = [
                f"# dot_swarm Handoff — {name_str} — {now}", "",
                "## Current State",
                f"Focus: {state.get('Current focus', '(not set)')}",
            ]
            if active:
                lines.append(f"Active: {', '.join(i.id for i in active)}")
            if state.get("Handoff note"):
                lines.append(f"\n{state['Handoff note']}")
            lines += ["", "## Ready for Pickup"]
            for item in pending[:5]:
                lines.append(f"- {item.id}: {item.description} [{item.priority.value.upper()}]")
            lines += [
                "", "## Context",
                f"@{paths.root}/BOOTSTRAP.md",
                f"@{paths.root}/context.md",
            ]
            return [types.TextContent(type="text", text="\n".join(lines))]

        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]

    except Exception as e:
        return [types.TextContent(type="text", text=f"Error: {e}")]


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import asyncio
    asyncio.run(mcp.server.stdio.run(server))


if __name__ == "__main__":
    main()
