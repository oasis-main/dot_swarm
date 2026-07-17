import os
import threading
import time

import pytest
from pathlib import Path
from datetime import datetime
from dot_swarm.models import SwarmPaths, WorkItem, ItemState, Claim
from dot_swarm.operations import (
    read_queue, write_queue, claim_item, done_item, read_claims,
    resolve_claims, ClaimLockTimeout, _item_lock,
)

@pytest.fixture
def swarm_paths(tmp_path):
    swarm = tmp_path / ".swarm"
    swarm.mkdir()
    paths = SwarmPaths.from_swarm_dir(swarm)
    return paths

def test_claim_creates_file(swarm_paths):
    # Setup initial queue
    item = WorkItem(id="SWC-001", description="Test item", state=ItemState.OPEN)
    write_queue(swarm_paths, [], [item], [])
    
    # Create claims dir to trigger forward-compatible write
    swarm_paths.claims.mkdir(parents=True, exist_ok=True)
    
    # Claim it
    claim_item(swarm_paths, "SWC-001", "agent-1")
    
    # Verify claim file exists
    claims_dir = swarm_paths.claims
    assert claims_dir.is_dir()
    claim_files = list(claims_dir.glob("SWC-001_agent-1_*.json"))
    assert len(claim_files) == 1
    
    # Verify read_queue resolves it to CLAIMED
    active, pending, done = read_queue(swarm_paths)
    assert len(active) == 1
    assert active[0].id == "SWC-001"
    assert active[0].state == ItemState.CLAIMED
    assert active[0].claimed_by == "agent-1"

def test_done_supersedes_claims_append_only(swarm_paths):
    """done_item appends a DONE release record without deleting prior claims."""
    item = WorkItem(id="SWC-001", description="Test item", state=ItemState.OPEN)
    write_queue(swarm_paths, [], [item], [])

    swarm_paths.claims.mkdir(parents=True, exist_ok=True)

    claim_item(swarm_paths, "SWC-001", "agent-1")
    assert len(list(swarm_paths.claims.glob("SWC-001_*.json"))) == 1

    done_item(swarm_paths, "SWC-001", "agent-1")

    # Append-only: original CLAIMED record persists alongside the new DONE record.
    files = list(swarm_paths.claims.glob("SWC-001_*.json"))
    assert len(files) == 2

    states = sorted(
        __import__("json").loads(f.read_text())["state"] for f in files
    )
    assert states == ["CLAIMED", "DONE"]

    # Resolution still picks DONE
    active, pending, done = read_queue(swarm_paths)
    assert len(done) == 1
    assert done[0].id == "SWC-001"
    assert done[0].state == ItemState.DONE

def test_two_active_claims_resolve_to_competing(swarm_paths):
    """Two distinct agents both holding active claims → COMPETING with both names."""
    item = WorkItem(id="SWC-001", description="Test item", state=ItemState.OPEN)
    write_queue(swarm_paths, [], [item], [])

    from dot_swarm.operations import write_claim

    c1 = Claim(item_id="SWC-001", agent_id="agent-1", state=ItemState.CLAIMED,
               timestamp=datetime(2026, 4, 20, 10, 0))
    c2 = Claim(item_id="SWC-001", agent_id="agent-2", state=ItemState.COMPETING,
               timestamp=datetime(2026, 4, 20, 11, 0))
    write_claim(swarm_paths, c1)
    write_claim(swarm_paths, c2)

    active, pending, done = read_queue(swarm_paths)
    assert len(active) == 1
    item = active[0]
    assert item.state == ItemState.COMPETING
    assert "agent-1" in item.claimed_by and "agent-2" in item.claimed_by
    assert len(item.competitors) == 2


def test_done_release_overrides_active_claims(swarm_paths):
    """A DONE record newer than every active claim wins resolution."""
    item = WorkItem(id="SWC-001", description="Test item", state=ItemState.OPEN)
    write_queue(swarm_paths, [], [item], [])

    from dot_swarm.operations import write_claim

    write_claim(swarm_paths, Claim(
        item_id="SWC-001", agent_id="agent-1",
        state=ItemState.CLAIMED, timestamp=datetime(2026, 4, 20, 10, 0),
    ))
    write_claim(swarm_paths, Claim(
        item_id="SWC-001", agent_id="agent-1",
        state=ItemState.DONE, timestamp=datetime(2026, 4, 20, 11, 0),
    ))

    active, pending, done = read_queue(swarm_paths)
    assert active == [] or all(i.id != "SWC-001" for i in active)
    assert any(i.id == "SWC-001" and i.state == ItemState.DONE for i in done)


def test_promote_competitor_marks_losers_withdrawn(swarm_paths):
    from dot_swarm.operations import (
        claim_item, competitors_for, promote_competitor,
    )
    item = WorkItem(id="SWC-001", description="Race", state=ItemState.OPEN)
    write_queue(swarm_paths, [], [item], [])

    swarm_paths.claims.mkdir(parents=True, exist_ok=True)
    claim_item(swarm_paths, "SWC-001", "agent-1")
    claim_item(swarm_paths, "SWC-001", "agent-2", compete=True)

    rivals = competitors_for(swarm_paths, "SWC-001")
    assert sorted(c.agent_id for c in rivals) == ["agent-1", "agent-2"]

    winner, losers = promote_competitor(
        swarm_paths, "SWC-001", "agent-2", reason="cleaner solution"
    )
    assert winner.agent_id == "agent-2"
    assert [l.agent_id for l in losers] == ["agent-1"]

    # After promotion: only the winner is an active competitor
    rivals2 = competitors_for(swarm_paths, "SWC-001")
    assert len(rivals2) == 1
    assert rivals2[0].agent_id == "agent-2"

    active, pending, done = read_queue(swarm_paths)
    target = next(i for i in active if i.id == "SWC-001")
    assert target.state == ItemState.CLAIMED
    assert target.claimed_by == "agent-2"


# ---------------------------------------------------------------------------
# SWC-050: per-item claim lock closes the read-decide-write TOCTOU window.
# ---------------------------------------------------------------------------

def test_concurrent_claim_item_exactly_one_winner(swarm_paths):
    """THE core property: N threads racing to claim the SAME OPEN item with
    compete=False must produce exactly ONE winner and N-1 ValueErrors —
    never N silent 'successes' that only get reconciled into COMPETING
    after the fact. Without the SWC-050 lock this was flaky: concurrent
    callers could all observe OPEN before any of them wrote."""
    item = WorkItem(id="SWC-001", description="Race", state=ItemState.OPEN)
    write_queue(swarm_paths, [], [item], [])
    swarm_paths.claims.mkdir(parents=True, exist_ok=True)

    n = 8
    barrier = threading.Barrier(n)
    results: list[tuple[str, object]] = []
    lock = threading.Lock()

    def worker(agent_id: str) -> None:
        barrier.wait()  # maximize actual contention
        try:
            claim_item(swarm_paths, "SWC-001", agent_id, compete=False)
            outcome = ("ok", agent_id)
        except ValueError as e:
            outcome = ("error", str(e))
        with lock:
            results.append(outcome)

    threads = [threading.Thread(target=worker, args=(f"agent-{i}",)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == n
    winners = [r for r in results if r[0] == "ok"]
    losers = [r for r in results if r[0] == "error"]
    assert len(winners) == 1, f"expected exactly 1 winner, got {winners}"
    assert len(losers) == n - 1
    assert all("already claimed" in msg for _, msg in losers)

    # No leftover lock file after every claimant has finished.
    assert not (swarm_paths.claims / ".lock-SWC-001").exists()

    active, pending, done = read_queue(swarm_paths)
    target = next(i for i in active if i.id == "SWC-001")
    assert target.state == ItemState.CLAIMED
    assert target.claimed_by == winners[0][1]


def test_item_lock_releases_after_use(swarm_paths):
    with _item_lock(swarm_paths, "SWC-999"):
        assert (swarm_paths.claims / ".lock-SWC-999").exists()
    assert not (swarm_paths.claims / ".lock-SWC-999").exists()


def test_item_lock_scoped_per_item_not_swarm_wide(swarm_paths):
    """Two DIFFERENT items must never contend — only same-item callers should
    serialize."""
    entered = threading.Event()
    release = threading.Event()

    def hold_a():
        with _item_lock(swarm_paths, "SWC-A"):
            entered.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold_a)
    t.start()
    assert entered.wait(timeout=5)

    # A lock on a DIFFERENT item must acquire immediately, not wait on SWC-A.
    start = time.monotonic()
    with _item_lock(swarm_paths, "SWC-B"):
        pass
    assert time.monotonic() - start < 1.0

    release.set()
    t.join(timeout=5)


def test_item_lock_times_out_when_genuinely_held(swarm_paths):
    with _item_lock(swarm_paths, "SWC-001"):
        # Held (not stale — mtime is now) by "this" call; a nested/second
        # attempt with a short timeout must give up rather than hang.
        with pytest.raises(ClaimLockTimeout):
            with _item_lock(swarm_paths, "SWC-001", timeout=0.15):
                pass  # pragma: no cover — must never be reached


def test_item_lock_reclaims_stale_lock(swarm_paths):
    """A lock file left behind by a crashed holder must not block forever —
    once older than the staleness threshold it's reclaimed."""
    from dot_swarm import operations as _ops

    swarm_paths.claims.mkdir(parents=True, exist_ok=True)
    stale = swarm_paths.claims / ".lock-SWC-001"
    stale.touch()
    old = time.time() - (_ops._LOCK_STALE_SECONDS + 5)
    os.utime(stale, (old, old))

    start = time.monotonic()
    with _item_lock(swarm_paths, "SWC-001", timeout=2.0):
        pass
    # Reclaimed promptly, not after waiting out the full 2s timeout.
    assert time.monotonic() - start < 1.0
