"""dot_swarm AI operations — context bundling, Bedrock invocation, op dispatch.

This module contains no boto3 imports at the top level so the base CLI
remains importable without the [ai] extra installed.
"""

from __future__ import annotations

import json
from pathlib import Path

from .models import Priority, SwarmPaths, utcnow
from .operations import (
    add_item, append_memory, audit, block_item, claim_item, done_item,
    discover_divisions, find_parent_paths, get_alignment,
    partial_item, read_queue, read_state, write_state,
    _division_code_from_paths, _now_ts,
)

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a dot_swarm assistant. dot_swarm is a git-native agent coordination system that stores
all state in markdown files inside a .swarm/ directory.

You will receive:
1. The current .swarm/ context (state.md, active queue items, context.md header)
2. A natural language instruction from the user

Respond with a SINGLE JSON object — no prose, no markdown fences, just the raw JSON.

Schema:
{
  "commentary": "<one or two sentences: what you're doing and why>",
  "operations": [ <operation objects — see below> ]
}

OPERATION TYPES:

  done:           {"op":"done",          "id":"<ITEM-ID>",  "note":"<completion note>"}
  claim:          {"op":"claim",         "id":"<ITEM-ID>"}
  add:            {"op":"add",           "description":"<text>", "priority":"critical|high|medium|low",
                                         "project":"<project>",  "notes":"<optional>"}
  partial:        {"op":"partial",       "id":"<ITEM-ID>",  "note":"<checkpoint note>"}
  block:          {"op":"block",         "id":"<ITEM-ID>",  "reason":"<why blocked>"}
  write_state:    {"op":"write_state",   "fields":{"Current focus":"...", "Blockers":"..."}}
                    Valid field keys: "Current focus", "Blockers", "Active items",
                                      "Ready for pickup", "Handoff note", "Last touched"
                    Include only fields that are actually changing.
  append_memory:  {"op":"append_memory", "topic":"<short topic>",
                                         "decision":"<what was decided>",
                                         "why":"<rationale — required>",
                                         "tradeoff":"<tradeoff accepted, or empty>"}
  update_context: {"op":"update_context","section":"<## Section Heading>",
                                         "content":"<new content for that section>"}
                    Replaces content under the named ## heading in context.md.
                    If the heading doesn't exist, it is appended as a new section.
  respond:        {"op":"respond",       "message":"<answer to user's question>"}
                    Use for questions with no file changes needed.
                    Can be combined with write_state for focus updates.
                    Do not combine with add/done/claim/etc.

RULES:
- Return ONLY the JSON object. No markdown fences, no preamble.
- For questions, use "respond". For multiple items to add, emit one "add" per item.
- Use item IDs exactly as shown in the queue context. If ambiguous, use "respond" to clarify.
- Keep "note", "notes", and "decision" fields to one sentence each.
- "why" in append_memory is required — never leave it empty.
- If the instruction cannot map to any operation, use "respond" to explain why.
"""

# ---------------------------------------------------------------------------
# Context bundle
# ---------------------------------------------------------------------------

def build_context_bundle(paths: SwarmPaths, context_limit: int = 1200) -> str:
    """Assemble minimal .swarm/ context for the AI prompt.

    The 'context_limit' defines the approximate target token budget for
    the files (state + active queue + pending queue + context header).
    """
    sections: list[str] = []

    # state.md — typically ~400 chars
    if paths.state.exists():
        sections.append("--- STATE ---\n" + paths.state.read_text(encoding='utf-8').strip())
    else:
        sections.append("--- STATE ---\n(state.md not found)")

    # queue.md — active items + pending items up to limit
    try:
        active, pending, _ = read_queue(paths)
    except Exception:
        active, pending = [], []

    if active:
        lines = "\n".join(item.to_line() for item in active)
        sections.append(f"--- QUEUE (active: {len(active)} items) ---\n{lines}")
    else:
        sections.append("--- QUEUE (active) ---\n(none)")

    if pending:
        # Heuristic: 4 chars per token, reserve half for other files
        pending_budget = max(5, context_limit // 2)
        top_n = pending[:(pending_budget // 15)] # approx 15 tokens per line
        lines = "\n".join(item.to_line() for item in top_n)
        suffix = f"\n(+{len(pending)-len(top_n)} more)" if len(pending) > len(top_n) else ""
        sections.append(f"--- QUEUE (pending, top {len(top_n)}) ---\n{lines}{suffix}")

    # context.md — header (charter)
    if paths.context.exists():
        # Read first ~2000 chars or first 40 lines
        header = "\n".join(paths.context.read_text(encoding='utf-8').splitlines()[:40])
        sections.append("--- CONTEXT (charter) ---\n" + header)

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Bedrock invocation
# ---------------------------------------------------------------------------

def invoke_via_cli(cli_name: str, user_message: str) -> dict:
    """Invoke a local LLM CLI (claude, gemini, opencode) and return parsed JSON.

    Passes the same SYSTEM_PROMPT as the Bedrock path so the response
    schema is identical. Requires the chosen CLI to be installed and in PATH.

    Supported interfaces and their non-interactive flags:
      claude    → claude -p "<prompt>"   (Claude Code CLI)
      gemini    → gemini -p "<prompt>"   (Gemini CLI)
      opencode  → opencode run "<prompt>" (OpenCode)

    Raises:
        FileNotFoundError: if the CLI binary is not found in PATH
        RuntimeError: if the CLI exits non-zero
        json.JSONDecodeError: if the response is not valid JSON after cleaning
    """
    import shutil
    import subprocess

    bin_path = shutil.which(cli_name)
    if not bin_path:
        raise FileNotFoundError(
            f"'{cli_name}' not found in PATH. "
            f"Install it and make sure it's on your PATH before using --via {cli_name}."
        )

    full_prompt = (
        f"SYSTEM INSTRUCTIONS — follow exactly:\n{SYSTEM_PROMPT}\n"
        f"---\n\n"
        f"{user_message}"
    )

    if cli_name in ("claude", "gemini"):
        cmd = [bin_path, "-p", full_prompt]
    elif cli_name == "opencode":
        cmd = [bin_path, "run", full_prompt]
    else:
        raise ValueError(f"Unknown CLI interface: {cli_name!r}")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    if result.returncode != 0:
        stderr = (result.stderr or result.stdout)[:400]
        raise RuntimeError(f"{cli_name} exited {result.returncode}: {stderr}")

    raw = result.stdout.strip()
    # Strip accidental markdown fences despite instructions
    clean = raw
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[-1]
    if clean.endswith("```"):
        clean = clean.rsplit("```", 1)[0]
    return json.loads(clean.strip())


def invoke_via_ollama(host: str, model: str, user_message: str) -> dict:
    """Call a local Ollama instance and return the parsed JSON response dict.

    Uses urllib (stdlib only — no extra dependency).
    Ollama must be running: `ollama serve` or the desktop app.

    Raises:
        ConnectionError: if Ollama is not reachable at host
        json.JSONDecodeError: if the response is not valid JSON after cleaning
    """
    import urllib.request
    import urllib.error

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        "stream": False,
    }).encode()

    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw_body = resp.read().decode()
    except urllib.error.URLError as e:
        raise ConnectionError(
            f"Could not reach Ollama at {host}. "
            f"Is it running? Start it with: ollama serve\n  ({e})"
        ) from e

    data = json.loads(raw_body)
    raw = data.get("message", {}).get("content", "")
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[-1]
    if clean.endswith("```"):
        clean = clean.rsplit("```", 1)[0]
    return json.loads(clean.strip())


def invoke_ai(client, model: str, user_message: str) -> dict:
    """Call Bedrock converse API and return the parsed JSON response dict.

    Raises:
        json.JSONDecodeError: if response is not valid JSON after fence stripping
        botocore.exceptions.ClientError: on API / auth errors
    """
    response = client.converse(
        modelId=model,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": user_message}]}],
        inferenceConfig={"maxTokens": 1024, "temperature": 0.0},
    )
    raw = response["output"]["message"]["content"][0]["text"]
    # Strip accidental markdown fences despite instructions
    clean = raw.strip()
    if clean.startswith("```"):
        clean = clean.split("\n", 1)[-1]  # remove opening fence line
    if clean.endswith("```"):
        clean = clean.rsplit("```", 1)[0]
    return json.loads(clean.strip())


# ---------------------------------------------------------------------------
# Preview formatting
# ---------------------------------------------------------------------------

def format_preview(commentary: str, ops: list[dict]) -> str:
    """Return a human-readable preview of proposed operations."""
    lines = [f"AI: {commentary}", "", "Proposed operations:"]
    for i, op in enumerate(ops, 1):
        kind = op.get("op", "?")
        if kind == "done":
            lines.append(f"  {i}. done      [{op['id']}]  note: \"{op.get('note','')}\"")
        elif kind == "claim":
            lines.append(f"  {i}. claim     [{op['id']}]")
        elif kind == "add":
            lines.append(f"  {i}. add       [{op.get('priority','medium')}] {op['description'][:60]}")
        elif kind == "partial":
            lines.append(f"  {i}. partial   [{op['id']}]  note: \"{op.get('note','')}\"")
        elif kind == "block":
            lines.append(f"  {i}. block     [{op['id']}]  reason: \"{op.get('reason','')}\"")
        elif kind == "write_state":
            for k, v in op.get("fields", {}).items():
                lines.append(f"  {i}. state     {k} → \"{v[:50]}\"")
        elif kind == "append_memory":
            lines.append(f"  {i}. memory    topic: {op.get('topic','')} — {op.get('decision','')[:50]}")
        elif kind == "update_context":
            lines.append(f"  {i}. context   section: {op.get('section','')} (rewrite)")
        elif kind == "respond":
            lines.append(f"  {i}. respond   (no file changes)")
        else:
            lines.append(f"  {i}. {kind}  (unknown op)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Operation executor
# ---------------------------------------------------------------------------

def execute_operations(
    paths: SwarmPaths,
    ops: list[dict],
    agent_id: str,
) -> list[str]:
    """Execute each operation against .swarm/ files.

    Returns a list of human-readable result strings.
    Best-effort: errors on individual ops are collected but do not abort the rest.
    """
    results: list[str] = []
    division_code = _division_code_from_paths(paths)

    for op in ops:
        kind = op.get("op", "")
        try:
            if kind == "done":
                item = done_item(paths, op["id"], agent_id, op.get("note", ""))
                write_state(paths, {"last_agent": agent_id, "last_touched": _now_ts()})
                results.append(f"  ✓ done [{item.id}]: {item.description[:60]}")

            elif kind == "claim":
                item = claim_item(paths, op["id"], agent_id)
                write_state(paths, {
                    "last_agent": agent_id,
                    "last_touched": _now_ts(),
                    "current_focus": f"{item.id}: {item.description[:50]}",
                })
                results.append(f"  ✓ claimed [{item.id}]: {item.description[:60]}")

            elif kind == "add":
                priority = Priority(op.get("priority", "medium"))
                item = add_item(
                    paths,
                    description=op["description"],
                    division_code=division_code,
                    priority=priority,
                    project=op.get("project", "misc"),
                    notes=op.get("notes", ""),
                )
                results.append(f"  ✓ added [{item.id}]: {item.description[:60]}")

            elif kind == "partial":
                item = partial_item(paths, op["id"], agent_id, op.get("note", ""))
                results.append(f"  ✓ partial [{item.id}]: {item.description[:60]}")

            elif kind == "block":
                item = block_item(paths, op["id"], op["reason"])
                results.append(f"  ✓ blocked [{item.id}]: {item.description[:60]}")

            elif kind == "write_state":
                write_state(paths, op.get("fields", {}))
                fields_str = ", ".join(f"{k}={v[:30]!r}" for k, v in op.get("fields", {}).items())
                results.append(f"  ✓ state updated: {fields_str}")

            elif kind == "append_memory":
                append_memory(
                    paths,
                    topic=op["topic"],
                    decision=op["decision"],
                    why=op["why"],
                    tradeoff=op.get("tradeoff", ""),
                    agent_id=agent_id,
                )
                results.append(f"  ✓ memory: [{op['topic']}] {op['decision'][:50]}")

            elif kind == "update_context":
                _exec_update_context(paths, op["section"], op["content"])
                results.append(f"  ✓ context: section '{op['section']}' updated")

            elif kind == "respond":
                # Message already printed in cli.py; no file changes
                results.append(f"  (no file changes)")

            else:
                results.append(f"  ✗ skipped unknown op: {kind!r}")

        except (ValueError, KeyError, FileNotFoundError) as e:
            results.append(f"  ✗ error executing {kind}({op.get('id', '')}): {e}")

    return results


# ---------------------------------------------------------------------------
# update_context helper
# ---------------------------------------------------------------------------

def _exec_update_context(paths: SwarmPaths, section: str, content: str) -> None:
    """Replace or append a ## section in context.md."""
    if not paths.context.exists():
        paths.context.write_text(f"{section}\n\n{content}\n", encoding='utf-8')
        return

    lines = paths.context.read_text(encoding='utf-8').splitlines(keepends=True)

    # Find the target heading
    target = section.strip()
    start = None
    for i, line in enumerate(lines):
        if line.strip() == target:
            start = i
            break

    if start is None:
        # Append as new section
        new_text = "".join(lines).rstrip("\n") + f"\n\n{target}\n\n{content}\n"
    else:
        # Find the end of this section (next ## heading or EOF)
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if lines[j].startswith("## "):
                end = j
                break
        new_lines = lines[:start + 1] + ["\n", content.rstrip("\n") + "\n", "\n"] + lines[end:]
        new_text = "".join(new_lines)

    # Atomic write
    tmp = paths.context.with_suffix(".md.tmp")
    tmp.write_text(new_text, encoding='utf-8')
    tmp.replace(paths.context)


# ---------------------------------------------------------------------------
# heal — full health pass (alignment, queue health, security scan, trail
# integrity), extracted from the CLI so both `swarm heal` and the MCP
# `swarm_heal` tool share ONE implementation (SWC-047 — the MCP server
# previously imported `heal` from here and it never existed, so the whole
# server failed to start).
# ---------------------------------------------------------------------------

def _quarantine_findings(paths: SwarmPaths, div_root: Path, findings: list) -> list[str]:
    """Back up files with adversarial findings to .swarm/quarantine/ for human
    review. We do NOT auto-delete content — humans must review and excise
    injections. Returns human-readable messages describing what was quarantined
    (formerly `click.echo`'d directly from cli.py; now returned so both the CLI
    and the MCP tool can report it their own way).
    """
    quarantine_dir = paths.root / "quarantine"
    quarantine_dir.mkdir(exist_ok=True)

    ts = utcnow().strftime("%Y%m%dT%H%MZ")  # filename-safe, no ':' (Windows compat)
    by_source: dict[str, list] = {}
    for f in findings:
        by_source.setdefault(f.source, []).append(f)

    messages: list[str] = []
    for source, src_findings in by_source.items():
        if source.startswith("workflows/"):
            fpath = paths.workflows / source[len("workflows/"):]
        elif source in ("CLAUDE.md", ".windsurfrules", ".cursorrules",
                        ".github/copilot-instructions.md"):
            fpath = div_root / source
        else:
            fpath = paths.root / source
        if not fpath.exists():
            continue

        safe_name = source.replace("/", "_").replace(".", "_")
        backup = quarantine_dir / f"{ts}_{safe_name}.bak"
        backup.write_text(fpath.read_text(encoding="utf-8", errors="replace"))

        categories = ", ".join(sorted({f.category for f in src_findings}))
        messages.append(f"{source} -> quarantine/{backup.name}  [{categories}]")

    return messages


def heal(paths: SwarmPaths, *, fix: bool = False, depth: int = 1) -> dict:
    """Full health pass: alignment, queue health, security scan, and pheromone
    trail integrity. Pure function — returns structured findings, no I/O side
    effects beyond memory.md logging and (with fix=True) quarantine/block_peer.
    Callers that want human-readable output (the CLI) format this themselves;
    callers that want JSON (the MCP tool) can return it as-is.
    """
    from . import security as _sec
    from . import signing as _sign

    div_root = paths.root.parent
    div_name = div_root.name

    result: dict = {"division": div_name, "fix": fix}

    # ── 1. Alignment ─────────────────────────────────────────────────────
    parent_paths = find_parent_paths(paths)
    alignment: dict = {}
    if parent_paths:
        al = get_alignment(paths, parent_paths)
        alignment["parent"] = parent_paths.root.parent.name
        alignment["parent_links"] = len(al)
    else:
        alignment["parent"] = None
        alignment["parent_links"] = 0

    child_divisions = discover_divisions(div_root, depth=depth)
    children = [(p, ps) for p, ps in child_divisions if p != div_root]
    child_links = sum(len(get_alignment(paths, cps)) for _, cps in children)
    orphan_items: list[str] = []
    if children:
        local_active, local_pending, _ = read_queue(paths)
        linked_ids = set()
        if parent_paths:
            for l_item, _ in get_alignment(paths, parent_paths):
                linked_ids.add(l_item.id)
        for _, cps in children:
            for l_item, _ in get_alignment(paths, cps):
                linked_ids.add(l_item.id)
        for item in local_active + local_pending:
            if item.id not in linked_ids:
                orphan_items.append(item.id)
    alignment["children"] = len(children)
    alignment["child_links"] = child_links
    alignment["orphan_items"] = orphan_items
    result["alignment"] = alignment

    # ── 2. Queue health ──────────────────────────────────────────────────
    queue_findings = audit(paths)
    _, pending_q, _ = read_queue(paths)
    result["queue_health"] = {
        "findings": queue_findings,
        "pending_count": len(pending_q),
    }

    # ── 3. Security scan ─────────────────────────────────────────────────
    swarm_sec = _sec.scan_swarm_directory(paths)
    shim_sec = _sec.scan_platform_shims(div_root)
    all_sec = swarm_sec + shim_sec
    counts = _sec.severity_counts(all_sec)
    quarantine_messages: list[str] = []
    if all_sec and fix:
        quarantine_messages = _quarantine_findings(paths, div_root, all_sec)
    result["security"] = {
        "findings": [
            {"source": f.source, "line": f.line, "category": f.category,
             "severity": f.severity, "excerpt": f.excerpt}
            for f in all_sec
        ],
        "counts": counts,
        "quarantined": quarantine_messages,
    }

    # ── 4. Pheromone trail integrity ─────────────────────────────────────
    identity = _sign.load_identity(paths.root)
    tampered: list[dict] = []
    blocked_fingerprints: list[str] = []
    trail_len = 0
    if identity:
        tampered = _sign.verify_trail(paths.root)
        trail_len = len(_sign.read_trail(paths.root))
        if tampered and fix:
            fps = {r.get("fingerprint", "") for r in tampered
                   if r.get("fingerprint", "") not in ("unsigned", "")}
            for fp in fps:
                _sign.block_peer(paths.root, fp)
                blocked_fingerprints.append(fp)
    result["trail"] = {
        "has_identity": identity is not None,
        "length": trail_len,
        "tampered": tampered,
        "blocked_fingerprints": blocked_fingerprints,
    }

    # ── 5. Summary & memory log ──────────────────────────────────────────
    total_issues = len(queue_findings) + len(all_sec) + len(tampered)
    result["total_issues"] = total_issues
    result["healthy"] = total_issues == 0

    if all_sec:
        sources = ", ".join(sorted({f.source for f in all_sec}))
        summary = (
            f"heal found {counts['CRITICAL']} critical / {counts['HIGH']} high / "
            f"{counts['MEDIUM']} medium security issues in: {sources}"
        )
        append_memory(paths, topic="heal-security-scan", decision=summary,
                      why="Automatic heal audit log — findings must not be silently lost",
                      tradeoff="", agent_id="swarm-heal")
        result["memory_logged"] = True
    else:
        result["memory_logged"] = False

    return result
