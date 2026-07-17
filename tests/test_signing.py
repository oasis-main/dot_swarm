"""Tests for dot_swarm.signing — HMAC-SHA256 identity + pheromone trail."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from dot_swarm.models import SwarmPaths
from dot_swarm.signing import (
    BLOCKED_PEERS_FILE,
    IDENTITY_FILE,
    SIGNING_KEY_FILE,
    TRAIL_FILE,
    append_trail,
    block_peer,
    generate_identity,
    is_blocked,
    load_blocked_peers,
    load_identity,
    read_trail,
    sign_operation,
    verify_trail,
    _load_key,
    _sign,
    _verify,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def swarm_dir(tmp_path: Path) -> Path:
    """Return an initialised .swarm/ directory."""
    swarm = tmp_path / ".swarm"
    swarm.mkdir()
    return swarm


# ---------------------------------------------------------------------------
# Identity generation
# ---------------------------------------------------------------------------

def test_generate_identity_creates_files(swarm_dir: Path) -> None:
    identity = generate_identity(swarm_dir)
    assert (swarm_dir / IDENTITY_FILE).exists()
    assert (swarm_dir / SIGNING_KEY_FILE).exists()
    assert identity["fingerprint"]
    assert identity["algorithm"] == "hmac-sha256"


def test_generate_identity_idempotent(swarm_dir: Path) -> None:
    id1 = generate_identity(swarm_dir)
    id2 = generate_identity(swarm_dir)
    assert id1["fingerprint"] == id2["fingerprint"]
    assert id1["id"] == id2["id"]


def test_load_identity_returns_none_when_absent(tmp_path: Path) -> None:
    assert load_identity(tmp_path / "no_swarm") is None


def test_load_identity_roundtrip(swarm_dir: Path) -> None:
    generate_identity(swarm_dir)
    identity = load_identity(swarm_dir)
    assert identity is not None
    assert "fingerprint" in identity


def test_fingerprint_derived_from_key(swarm_dir: Path) -> None:
    """Fingerprint must be stable and derived from the key, not random."""
    import hashlib
    generate_identity(swarm_dir)
    key_hex = (swarm_dir / SIGNING_KEY_FILE).read_text(encoding='utf-8').strip()
    expected_fp = hashlib.sha256(key_hex.encode()).hexdigest()[:16]
    identity = load_identity(swarm_dir)
    assert identity["fingerprint"] == expected_fp


# ---------------------------------------------------------------------------
# Signing primitives
# ---------------------------------------------------------------------------

def test_sign_verify_roundtrip() -> None:
    key = b"test-key-32-bytes-exactly-padded"
    msg = "hello world"
    sig = _sign(key, msg)
    assert _verify(key, msg, sig)


def test_verify_rejects_tampered_message() -> None:
    key = b"test-key-32-bytes-exactly-padded"
    sig = _sign(key, "original")
    assert not _verify(key, "tampered", sig)


def test_verify_rejects_wrong_key() -> None:
    sig = _sign(b"key-a-32-bytes-exactly-padded!!!", "msg")
    assert not _verify(b"key-b-32-bytes-exactly-padded!!!", "msg", sig)


def test_load_key_returns_bytes(swarm_dir: Path) -> None:
    generate_identity(swarm_dir)
    key = _load_key(swarm_dir)
    assert isinstance(key, bytes)
    assert len(key) > 0


def test_load_key_none_when_absent(tmp_path: Path) -> None:
    assert _load_key(tmp_path) is None


# ---------------------------------------------------------------------------
# Trail operations
# ---------------------------------------------------------------------------

def test_sign_operation_produces_record(swarm_dir: Path) -> None:
    generate_identity(swarm_dir)
    rec = sign_operation(swarm_dir, "done", "test-agent", {"item_id": "CLD-001"})
    assert rec["op"] == "done"
    assert rec["agent_id"] == "test-agent"
    assert rec["signature"] not in ("", "unsigned")


def test_sign_operation_unsigned_without_key(tmp_path: Path) -> None:
    swarm = tmp_path / ".swarm"
    swarm.mkdir()
    rec = sign_operation(swarm, "done", "agent", {})
    assert rec["signature"] == "unsigned"
    assert rec["swarm_id"] == "anonymous"


def test_append_and_read_trail(swarm_dir: Path) -> None:
    generate_identity(swarm_dir)
    rec = sign_operation(swarm_dir, "claim", "agent-x", {"item_id": "CLD-002"})
    append_trail(swarm_dir, rec)
    trail = read_trail(swarm_dir)
    assert len(trail) == 1
    assert trail[0]["op"] == "claim"


def test_read_trail_empty_when_no_file(tmp_path: Path) -> None:
    assert read_trail(tmp_path) == []


def test_verify_trail_clean(swarm_dir: Path) -> None:
    generate_identity(swarm_dir)
    for i in range(3):
        rec = sign_operation(swarm_dir, "add", "agent", {"i": i})
        append_trail(swarm_dir, rec)
    tampered = verify_trail(swarm_dir)
    assert tampered == []


def test_verify_trail_detects_tamper(swarm_dir: Path) -> None:
    generate_identity(swarm_dir)
    rec = sign_operation(swarm_dir, "done", "agent", {"item_id": "X"})
    append_trail(swarm_dir, rec)

    # Manually corrupt the trail
    trail_file = swarm_dir / TRAIL_FILE
    line = json.loads(trail_file.read_text(encoding='utf-8').strip())
    line["agent_id"] = "evil-agent"           # tamper
    trail_file.write_text(json.dumps(line) + "\n", encoding='utf-8')

    tampered = verify_trail(swarm_dir)
    assert len(tampered) == 1


def test_verify_trail_skips_unsigned(swarm_dir: Path) -> None:
    """Unsigned records (no key) should not raise false positives."""
    rec = sign_operation(swarm_dir, "add", "agent", {})  # no key → unsigned
    append_trail(swarm_dir, rec)
    # verify_trail with no key returns [] immediately
    assert verify_trail(swarm_dir) == []


# ---------------------------------------------------------------------------
# Blocked peers
# ---------------------------------------------------------------------------

def test_block_peer_and_query(swarm_dir: Path) -> None:
    block_peer(swarm_dir, "deadbeef12345678")
    assert is_blocked(swarm_dir, "deadbeef12345678")
    assert not is_blocked(swarm_dir, "00000000ffffffff")


def test_block_peer_idempotent(swarm_dir: Path) -> None:
    block_peer(swarm_dir, "aabbccdd11223344")
    block_peer(swarm_dir, "aabbccdd11223344")
    blocked = load_blocked_peers(swarm_dir)
    assert blocked.count("aabbccdd11223344") == 1


def test_load_blocked_peers_empty(swarm_dir: Path) -> None:
    assert load_blocked_peers(swarm_dir) == []
