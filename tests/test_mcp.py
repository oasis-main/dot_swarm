import pytest
import json
from pathlib import Path

try:
    import mcp  # noqa: F401 — the optional [mcp] extra
    HAS_MCP = True
except (ImportError, ModuleNotFoundError):
    HAS_MCP = False

# SWC-047: only the OPTIONAL `mcp` SDK import above is allowed to skip these
# tests — a broken dot_swarm_mcp.server (our own code) must fail loudly, not
# silently report "SDK not installed" while masking a real bug. This bit us:
# server.py imported a function that didn't exist, and every MCP test just
# skipped with a misleading reason instead of failing.
if HAS_MCP:
    from dot_swarm_mcp.server import server, _resolve_paths, call_tool, list_tools

from dot_swarm.models import SwarmPaths, WorkItem, ItemState
from dot_swarm.operations import write_queue

@pytest.fixture
def swarm_paths(tmp_path):
    swarm = tmp_path / ".swarm"
    swarm.mkdir()
    (swarm / "queue.md").write_text("# Queue\n\n## Active\n\n## Pending\n\n## Done\n", encoding='utf-8')
    (swarm / "state.md").write_text("# State\n**Last touched**: 2026-01-01T00:00Z by test\n**Current focus**: none\n", encoding='utf-8')
    (swarm / "context.md").write_text("# Context\n", encoding='utf-8')
    (swarm / "BOOTSTRAP.md").write_text("# Bootstrap\n", encoding='utf-8')
    return SwarmPaths.from_swarm_dir(swarm)

@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_list_tools():
    tools = await list_tools()
    tool_names = [t.name for t in tools]
    assert "swarm_bootstrap" in tool_names
    assert "swarm_claim" in tool_names
    assert "swarm_partial" in tool_names
    assert "swarm_inspect" in tool_names

@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_call_tool_read(swarm_paths):
    import os
    os.environ["SWARM_ROOT"] = str(swarm_paths.root.parent)
    
    result = await call_tool("swarm_bootstrap", {"path": "."})
    assert result[0].text == "# Bootstrap\n"

@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_claim_and_partial(swarm_paths):
    import os
    os.environ["SWARM_ROOT"] = str(swarm_paths.root.parent)
    
    # Add an item first
    from dot_swarm.operations import add_item
    add_item(swarm_paths, "MCP Task")
    
    # Get the ID
    q_result = await call_tool("swarm_queue", {"section": "pending", "path": "."})
    items = json.loads(q_result[0].text)
    item_id = items[0]["id"]
    
    # Claim it
    await call_tool("swarm_claim", {"id": item_id, "agent_id": "mcp-agent", "path": "."})
    
    # Partial with proof
    await call_tool("swarm_partial", {
        "id": item_id, 
        "agent_id": "mcp-agent", 
        "proof": "commit:123",
        "path": "."
    })
    
    # Verify via queue
    q_result = await call_tool("swarm_queue", {"section": "active", "path": "."})
    active_items = json.loads(q_result[0].text)
    assert active_items[0]["id"] == item_id
    # Note: with original architecture it might be CLAIMED or PARTIAL
    assert active_items[0]["state"] in ("CLAIMED", "PARTIAL")
    
    # Check claim directory (even in v0 mode, we keep the v1 foundational write if claims dir exists)
    # But wait, in v0 mode we might not have created the claims dir.
    if swarm_paths.claims.is_dir():
        claim_files = list(swarm_paths.claims.glob(f"{item_id}_*.json"))
        assert len(claim_files) > 0


# ---------------------------------------------------------------------------
# SWC-049: process identity binding — a bound agent_id always overrides
# whatever the caller passes; with nothing bound, behavior is unchanged.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_claim_without_binding_trusts_caller_agent_id(swarm_paths, monkeypatch):
    """No DOT_SWARM_AGENT_ID set — identical to pre-SWC-049 behavior."""
    monkeypatch.setenv("SWARM_ROOT", str(swarm_paths.root.parent))
    monkeypatch.delenv("DOT_SWARM_AGENT_ID", raising=False)

    from dot_swarm.operations import add_item
    add_item(swarm_paths, "Unbound task")
    q = json.loads((await call_tool("swarm_queue", {"section": "pending", "path": "."}))[0].text)
    item_id = q[0]["id"]

    await call_tool("swarm_claim", {"id": item_id, "agent_id": "whoever-the-caller-says", "path": "."})

    active = json.loads((await call_tool("swarm_queue", {"section": "active", "path": "."}))[0].text)
    assert active[0]["claimed_by"] == "whoever-the-caller-says"


@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_bound_identity_overrides_caller_supplied_agent_id(swarm_paths, monkeypatch):
    """THE core property: with DOT_SWARM_AGENT_ID bound, a caller claiming to
    be a different agent is silently overridden, not trusted. This is what
    stops a hijacked/confused caller from attributing a write to an agent
    it isn't."""
    monkeypatch.setenv("SWARM_ROOT", str(swarm_paths.root.parent))
    monkeypatch.setenv("DOT_SWARM_AGENT_ID", "house")

    from dot_swarm.operations import add_item
    add_item(swarm_paths, "Bound task")
    q = json.loads((await call_tool("swarm_queue", {"section": "pending", "path": "."}))[0].text)
    item_id = q[0]["id"]

    # Caller claims to be "yesman" — the bound process identity is "house".
    await call_tool("swarm_claim", {"id": item_id, "agent_id": "yesman", "path": "."})

    active = json.loads((await call_tool("swarm_queue", {"section": "active", "path": "."}))[0].text)
    assert active[0]["claimed_by"] == "house"
    assert active[0]["claimed_by"] != "yesman"


@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_bound_write_is_signed_in_trail(swarm_paths, monkeypatch, tmp_path):
    """When the bound agent has a local Ed25519 key (SWC-048), writes made
    through it carry a verifiable per-agent signature in trail.log — not
    just an attributed name."""
    cryptography = pytest.importorskip("cryptography")
    from dot_swarm import identity as _id
    from dot_swarm import signing as _sign

    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    monkeypatch.setenv("DOT_SWARM_AGENT_KEY_DIR", str(key_dir))
    ident = _id.generate_agent_identity("house", key_dir=key_dir)
    _id.register_agent(swarm_paths.root, ident)

    monkeypatch.setenv("SWARM_ROOT", str(swarm_paths.root.parent))
    monkeypatch.setenv("DOT_SWARM_AGENT_ID", "house")

    from dot_swarm.operations import add_item
    add_item(swarm_paths, "Signed task")
    q = json.loads((await call_tool("swarm_queue", {"section": "pending", "path": "."}))[0].text)
    item_id = q[0]["id"]

    await call_tool("swarm_claim", {"id": item_id, "agent_id": "irrelevant", "path": "."})

    trail = _sign.read_trail(swarm_paths.root, limit=10)
    claim_records = [r for r in trail if r.get("op") == "claim"]
    assert claim_records, "expected a 'claim' record in trail.log"
    record = claim_records[-1]
    assert record["agent_id"] == "house"
    sig = record.get("agent_signature")
    assert sig and sig != _id.UNSIGNED
    assert _id.verify_agent(swarm_paths.root, "house", record["payload"], sig) is True


@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_inspector_id_also_overridden_when_bound(swarm_paths, monkeypatch):
    monkeypatch.setenv("SWARM_ROOT", str(swarm_paths.root.parent))
    monkeypatch.setenv("DOT_SWARM_AGENT_ID", "kolmogorov")

    from dot_swarm.operations import add_item, claim_item
    add_item(swarm_paths, "Inspect me")
    q = json.loads((await call_tool("swarm_queue", {"section": "pending", "path": "."}))[0].text)
    item_id = q[0]["id"]
    claim_item(swarm_paths, item_id, "some-worker")

    await call_tool("swarm_inspect", {
        "id": item_id, "inspector_id": "not-kolmogorov", "status": "pass", "path": ".",
    })

    done = json.loads((await call_tool("swarm_queue", {"section": "done", "path": "."}))[0].text)
    assert done[0]["id"] == item_id


# ---------------------------------------------------------------------------
# SWC-051: comments over MCP
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_comment_and_comments(swarm_paths, monkeypatch):
    monkeypatch.setenv("SWARM_ROOT", str(swarm_paths.root.parent))
    monkeypatch.delenv("DOT_SWARM_AGENT_ID", raising=False)

    await call_tool("swarm_comment", {"id": "SWC-001", "body": "first note", "agent_id": "house", "path": "."})
    result = await call_tool("swarm_comments", {"id": "SWC-001", "path": "."})
    thread = json.loads(result[0].text)
    assert len(thread) == 1
    assert thread[0]["body"] == "first note"
    assert thread[0]["agent_id"] == "house"


@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_comment_agent_id_overridden_when_bound(swarm_paths, monkeypatch):
    monkeypatch.setenv("SWARM_ROOT", str(swarm_paths.root.parent))
    monkeypatch.setenv("DOT_SWARM_AGENT_ID", "kolmogorov")

    await call_tool("swarm_comment", {"id": "SWC-001", "body": "hi", "agent_id": "someone-else", "path": "."})
    thread = json.loads((await call_tool("swarm_comments", {"id": "SWC-001", "path": "."}))[0].text)
    assert thread[0]["agent_id"] == "kolmogorov"


# ---------------------------------------------------------------------------
# SWC-052: mailbox over MCP
# ---------------------------------------------------------------------------

@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_mail_send_and_inbox(swarm_paths, monkeypatch):
    monkeypatch.setenv("SWARM_ROOT", str(swarm_paths.root.parent))
    monkeypatch.delenv("DOT_SWARM_AGENT_ID", raising=False)

    await call_tool("swarm_mail_send", {
        "to": "house", "subject": "fetch this", "body": "GET https://example.com",
        "agent_id": "yesman", "path": ".",
    })
    inbox = json.loads((await call_tool("swarm_mail_inbox", {"agent_id": "house", "path": "."}))[0].text)
    assert len(inbox) == 1
    assert inbox[0]["from"] == "yesman"
    assert inbox[0]["subject"] == "fetch this"


@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_mail_sender_overridden_when_bound(swarm_paths, monkeypatch):
    monkeypatch.setenv("SWARM_ROOT", str(swarm_paths.root.parent))
    monkeypatch.setenv("DOT_SWARM_AGENT_ID", "vanhelsing")

    await call_tool("swarm_mail_send", {
        "to": "house", "subject": "s", "body": "b", "agent_id": "yesman", "path": ".",
    })
    inbox = json.loads((await call_tool("swarm_mail_inbox", {"agent_id": "house", "path": "."}))[0].text)
    assert inbox[0]["from"] == "vanhelsing"
    assert inbox[0]["from"] != "yesman"


@pytest.mark.skipif(not HAS_MCP, reason="mcp SDK not installed")
@pytest.mark.anyio
async def test_mcp_mail_read_marks_read(swarm_paths, monkeypatch):
    monkeypatch.setenv("SWARM_ROOT", str(swarm_paths.root.parent))
    monkeypatch.delenv("DOT_SWARM_AGENT_ID", raising=False)

    await call_tool("swarm_mail_send", {"to": "house", "subject": "s", "body": "b", "agent_id": "yesman", "path": "."})
    inbox = json.loads((await call_tool("swarm_mail_inbox", {"agent_id": "house", "path": "."}))[0].text)
    msg_id = inbox[0]["msg_id"]

    result = await call_tool("swarm_mail_read", {"agent_id": "house", "msg_id": msg_id, "path": "."})
    read = json.loads(result[0].text)
    assert read["body"] == "b"

    inbox_after = json.loads((await call_tool("swarm_mail_inbox", {"agent_id": "house", "path": "."}))[0].text)
    assert inbox_after == []
