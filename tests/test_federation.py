"""Tests for dot_swarm.federation — OGP-lite trust, doorman, inbox/outbox."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from dot_swarm.federation import (
    ALL_INTENTS,
    DEFAULT_SCOPES,
    EXPORTS_FILE,
    INBOX_DIR,
    INTENT_ALIGNMENT_SIGNAL,
    INTENT_CAPABILITY_AD,
    INTENT_WORK_REQUEST,
    OUTBOX_DIR,
    POLICY_FILE,
    TRUSTED_PEERS_DIR,
    FederationMessage,
    FederationPeer,
    apply_inbox_message,
    deliver_to_inbox,
    doorman_check,
    export_identity,
    get_peer,
    init_federation,
    list_peers,
    read_inbox,
    revoke_peer,
    sign_federation_message,
    trust_peer,
    verify_federation_message,
    write_outbox,
    _canonical_json,
    _policy_allows,
)
from dot_swarm.signing import generate_identity


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def swarm_a(tmp_path: Path) -> Path:
    """A fully initialised .swarm/ directory with signing identity."""
    swarm = tmp_path / "org-a" / ".swarm"
    swarm.mkdir(parents=True)
    generate_identity(swarm)
    init_federation(swarm)
    return swarm


@pytest.fixture()
def swarm_b(tmp_path: Path) -> Path:
    """A second .swarm/ directory (peer)."""
    swarm = tmp_path / "org-b" / ".swarm"
    swarm.mkdir(parents=True)
    generate_identity(swarm)
    init_federation(swarm)
    return swarm


@pytest.fixture()
def peer_identity_file(swarm_b: Path, tmp_path: Path) -> Path:
    """Export swarm_b's identity.json to a temp file, as if shared out-of-band."""
    identity = export_identity(swarm_b)
    out = tmp_path / "peer_b_identity.json"
    out.write_text(json.dumps(identity, indent=2), encoding='utf-8')
    return out


# ---------------------------------------------------------------------------
# init_federation
# ---------------------------------------------------------------------------

def test_init_federation_creates_dirs(tmp_path: Path) -> None:
    swarm = tmp_path / ".swarm"
    swarm.mkdir()
    init_federation(swarm)
    assert (swarm / TRUSTED_PEERS_DIR).is_dir()
    assert (swarm / INBOX_DIR).is_dir()
    assert (swarm / OUTBOX_DIR).is_dir()
    assert (swarm / POLICY_FILE).exists()
    assert (swarm / EXPORTS_FILE).exists()


def test_init_federation_is_idempotent(swarm_a: Path) -> None:
    init_federation(swarm_a)
    init_federation(swarm_a)
    assert (swarm_a / TRUSTED_PEERS_DIR).is_dir()


# ---------------------------------------------------------------------------
# Identity export
# ---------------------------------------------------------------------------

def test_export_identity_has_fingerprint(swarm_a: Path) -> None:
    identity = export_identity(swarm_a)
    assert "fingerprint" in identity
    assert "id" in identity


def test_export_identity_raises_without_init(tmp_path: Path) -> None:
    swarm = tmp_path / ".swarm"
    swarm.mkdir()
    with pytest.raises(FileNotFoundError):
        export_identity(swarm)


# ---------------------------------------------------------------------------
# trust_peer / get_peer / list_peers / revoke_peer
# ---------------------------------------------------------------------------

def test_trust_peer_persists_record(swarm_a: Path, peer_identity_file: Path) -> None:
    peer = trust_peer(swarm_a, peer_identity_file, display_name="Org B")
    assert peer.display_name == "Org B"
    peer_file = swarm_a / TRUSTED_PEERS_DIR / f"{peer.fingerprint}.json"
    assert peer_file.exists()  # OGP lesson: persisted before return


def test_trust_peer_default_scopes(swarm_a: Path, peer_identity_file: Path) -> None:
    peer = trust_peer(swarm_a, peer_identity_file)
    assert set(DEFAULT_SCOPES).issubset(set(peer.scopes))


def test_trust_peer_custom_scopes(swarm_a: Path, peer_identity_file: Path) -> None:
    peer = trust_peer(swarm_a, peer_identity_file, scopes=["capability_ad"])
    assert peer.scopes == ["capability_ad"]


def test_get_peer_returns_none_for_unknown(swarm_a: Path) -> None:
    assert get_peer(swarm_a, "nonexistent0000000") is None


def test_get_peer_roundtrip(swarm_a: Path, peer_identity_file: Path) -> None:
    original = trust_peer(swarm_a, peer_identity_file, display_name="B")
    loaded = get_peer(swarm_a, original.fingerprint)
    assert loaded is not None
    assert loaded.fingerprint == original.fingerprint
    assert loaded.display_name == "B"


def test_list_peers_empty(swarm_a: Path) -> None:
    assert list_peers(swarm_a) == []


def test_list_peers_returns_trusted(swarm_a: Path, peer_identity_file: Path) -> None:
    trust_peer(swarm_a, peer_identity_file, display_name="B")
    peers = list_peers(swarm_a)
    assert len(peers) == 1
    assert peers[0].display_name == "B"


def test_revoke_peer_removes_record(swarm_a: Path, peer_identity_file: Path) -> None:
    peer = trust_peer(swarm_a, peer_identity_file)
    assert revoke_peer(swarm_a, peer.fingerprint) is True
    assert get_peer(swarm_a, peer.fingerprint) is None


def test_revoke_peer_returns_false_when_absent(swarm_a: Path) -> None:
    assert revoke_peer(swarm_a, "nope0000000000000") is False


def test_trust_peer_raises_on_missing_fingerprint(swarm_a: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"id": "x", "algorithm": "hmac-sha256"}), encoding='utf-8')
    with pytest.raises(ValueError, match="fingerprint"):
        trust_peer(swarm_a, bad)


# ---------------------------------------------------------------------------
# Doorman — the OGP identity-never-trusted-from-header lesson
# ---------------------------------------------------------------------------

def test_doorman_allows_trusted_peer_with_correct_scope(
    swarm_a: Path, peer_identity_file: Path, swarm_b: Path
) -> None:
    peer = trust_peer(swarm_a, peer_identity_file, scopes=[INTENT_WORK_REQUEST])
    allowed, reason = doorman_check(swarm_a, peer.fingerprint, INTENT_WORK_REQUEST)
    assert allowed is True
    assert reason == "ok"


def test_doorman_blocks_unknown_fingerprint(swarm_a: Path) -> None:
    allowed, reason = doorman_check(swarm_a, "deadbeef00000000", INTENT_WORK_REQUEST)
    assert allowed is False
    assert "not in trusted peers" in reason


def test_doorman_blocks_out_of_scope_intent(
    swarm_a: Path, peer_identity_file: Path
) -> None:
    peer = trust_peer(swarm_a, peer_identity_file, scopes=[INTENT_ALIGNMENT_SIGNAL])
    allowed, reason = doorman_check(swarm_a, peer.fingerprint, INTENT_WORK_REQUEST)
    assert allowed is False
    assert "scope" in reason


def test_doorman_blocks_policy_disabled_intent(
    swarm_a: Path, peer_identity_file: Path
) -> None:
    peer = trust_peer(swarm_a, peer_identity_file, scopes=list(ALL_INTENTS))
    # Disable work_request in policy.md
    policy = swarm_a / POLICY_FILE
    policy.write_text(policy.read_text(encoding='utf-8') + "\ndisabled: work_request\n")
    allowed, reason = doorman_check(swarm_a, peer.fingerprint, INTENT_WORK_REQUEST)
    assert allowed is False
    assert "policy" in reason


def test_doorman_never_trusts_claimed_identity(
    swarm_a: Path, peer_identity_file: Path, swarm_b: Path
) -> None:
    """Doorman must look up from stored records, not from what a message claims."""
    peer = trust_peer(swarm_a, peer_identity_file, scopes=[INTENT_WORK_REQUEST])
    # Attacker claims to be the trusted peer but uses a different fingerprint
    attacker_fp = "aaaa0000bbbb1111"
    allowed, _ = doorman_check(swarm_a, attacker_fp, INTENT_WORK_REQUEST)
    assert allowed is False  # not in trusted peers


# ---------------------------------------------------------------------------
# Message signing
# ---------------------------------------------------------------------------

def test_sign_and_verify_roundtrip() -> None:
    key = b"test-federation-key-bytes-padded"
    msg = {"from_fingerprint": "abc", "intent": "work_request", "payload": {}}
    sig = sign_federation_message(key, msg)
    assert verify_federation_message(key, msg, sig)


def test_verify_rejects_tampered_message() -> None:
    key = b"test-federation-key-bytes-padded"
    msg = {"from_fingerprint": "abc", "intent": "work_request", "payload": {}}
    sig = sign_federation_message(key, msg)
    msg["intent"] = "capability_ad"  # tamper
    assert not verify_federation_message(key, msg, sig)


def test_canonical_json_excludes_signature() -> None:
    d = {"a": 1, "signature": "should-be-excluded", "b": 2}
    canonical = _canonical_json(d)
    assert "signature" not in canonical
    assert "should-be-excluded" not in canonical


def test_canonical_json_is_deterministic() -> None:
    d1 = {"b": 2, "a": 1}
    d2 = {"a": 1, "b": 2}
    assert _canonical_json(d1) == _canonical_json(d2)


# ---------------------------------------------------------------------------
# Outbox
# ---------------------------------------------------------------------------

def test_write_outbox_creates_file(swarm_a: Path, peer_identity_file: Path) -> None:
    peer = trust_peer(swarm_a, peer_identity_file)
    out = write_outbox(swarm_a, peer.fingerprint, INTENT_WORK_REQUEST, {"description": "Help!"})
    assert out.exists()
    data = json.loads(out.read_text(encoding='utf-8'))
    assert data["intent"] == INTENT_WORK_REQUEST
    assert data["signature"] != ""


def test_write_outbox_includes_from_fingerprint(swarm_a: Path, peer_identity_file: Path) -> None:
    peer = trust_peer(swarm_a, peer_identity_file)
    out = write_outbox(swarm_a, peer.fingerprint, INTENT_ALIGNMENT_SIGNAL, {})
    data = json.loads(out.read_text(encoding='utf-8'))
    assert "from_fingerprint" in data
    assert len(data["from_fingerprint"]) > 0


def test_write_outbox_raises_without_identity(tmp_path: Path) -> None:
    swarm = tmp_path / ".swarm"
    swarm.mkdir()
    init_federation(swarm)
    with pytest.raises(FileNotFoundError):
        write_outbox(swarm, "fp0000001111", INTENT_WORK_REQUEST, {})


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

def test_read_inbox_empty(swarm_a: Path) -> None:
    assert read_inbox(swarm_a) == []


def test_deliver_and_read_inbox(swarm_a: Path, swarm_b: Path, peer_identity_file: Path) -> None:
    # B sends to A
    # A trusts B
    trust_peer(swarm_a, peer_identity_file, scopes=[INTENT_WORK_REQUEST])
    out_file = write_outbox(swarm_b, "anyfp000", INTENT_WORK_REQUEST, {"description": "X"})
    delivered = deliver_to_inbox(swarm_a, out_file)
    assert delivered.exists()
    messages = read_inbox(swarm_a)
    assert len(messages) == 1
    assert messages[0].intent == INTENT_WORK_REQUEST


# ---------------------------------------------------------------------------
# apply_inbox_message
# ---------------------------------------------------------------------------

def _mock_add_item(paths, description, notes="", priority="medium"):
    return f"added: {description}"


def test_apply_inbox_work_request(swarm_a: Path, swarm_b: Path, peer_identity_file: Path) -> None:
    trust_peer(swarm_a, peer_identity_file, scopes=[INTENT_WORK_REQUEST])
    b_identity = export_identity(swarm_b)
    # Write a fake message from B with B's real fingerprint
    msg = {
        "version": "1",
        "timestamp": "2026-04-06T14:00Z",
        "from_swarm": b_identity["id"],
        "from_fingerprint": b_identity["fingerprint"],
        "to_fingerprint": "anyvalue",
        "intent": INTENT_WORK_REQUEST,
        "payload": {"description": "Port the auth module", "context": "FastAPI"},
        "agent_id": "test",
        "signature": "dummy",
    }
    inbox_dir = swarm_a / INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)
    msg_file = inbox_dir / "test_msg.json"
    msg_file.write_text(json.dumps(msg), encoding='utf-8')

    result = apply_inbox_message(swarm_a, msg_file, _mock_add_item, object())
    assert result["ok"] is True
    assert "work_request" in result["reason"]


def test_apply_inbox_blocked_by_doorman(swarm_a: Path, tmp_path: Path) -> None:
    """Doorman must block messages from untrusted fingerprints."""
    msg = {
        "version": "1",
        "timestamp": "2026-04-06T14:00Z",
        "from_swarm": "unknown",
        "from_fingerprint": "deadbeef00000000",
        "to_fingerprint": "any",
        "intent": INTENT_WORK_REQUEST,
        "payload": {"description": "Sneaky request"},
        "agent_id": "attacker",
        "signature": "fake",
    }
    inbox_dir = swarm_a / INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)
    msg_file = inbox_dir / "attacker_msg.json"
    msg_file.write_text(json.dumps(msg), encoding='utf-8')

    result = apply_inbox_message(swarm_a, msg_file, _mock_add_item, object())
    assert result["ok"] is False
    assert "doorman" in result["reason"]


def test_apply_inbox_alignment_signal_ok(swarm_a: Path, swarm_b: Path, peer_identity_file: Path) -> None:
    trust_peer(swarm_a, peer_identity_file, scopes=[INTENT_ALIGNMENT_SIGNAL])
    b_identity = export_identity(swarm_b)
    msg = {
        "version": "1",
        "timestamp": "2026-04-06T14:00Z",
        "from_swarm": b_identity["id"],
        "from_fingerprint": b_identity["fingerprint"],
        "to_fingerprint": "any",
        "intent": INTENT_ALIGNMENT_SIGNAL,
        "payload": {"status": "aligned"},
        "agent_id": "test",
        "signature": "dummy",
    }
    inbox_dir = swarm_a / INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)
    msg_file = inbox_dir / "align_msg.json"
    msg_file.write_text(json.dumps(msg), encoding='utf-8')

    result = apply_inbox_message(swarm_a, msg_file, _mock_add_item, object())
    assert result["ok"] is True


def test_apply_inbox_handles_corrupt_file(swarm_a: Path) -> None:
    inbox_dir = swarm_a / INBOX_DIR
    inbox_dir.mkdir(parents=True, exist_ok=True)
    bad = inbox_dir / "bad.json"
    bad.write_text("not valid json {{{{", encoding='utf-8')
    result = apply_inbox_message(swarm_a, bad, _mock_add_item, object())
    assert result["ok"] is False
    assert "parse error" in result["reason"]
