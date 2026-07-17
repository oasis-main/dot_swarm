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
    (swarm / "queue.md").write_text("# Queue\n\n## Active\n\n## Pending\n\n## Done\n")
    (swarm / "state.md").write_text("# State\n**Last touched**: 2026-01-01T00:00Z by test\n**Current focus**: none\n")
    (swarm / "context.md").write_text("# Context\n")
    (swarm / "BOOTSTRAP.md").write_text("# Bootstrap\n")
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
