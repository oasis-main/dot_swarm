"""Tests for the federation strangers bay (collaborative-but-untrusted)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dot_swarm import federation as _fed
from dot_swarm.signing import generate_identity


@pytest.fixture
def home_swarm(tmp_path):
    swarm = tmp_path / "home" / ".swarm"
    swarm.mkdir(parents=True)
    generate_identity(swarm)
    _fed.init_federation(swarm)
    return swarm


@pytest.fixture
def stranger_message(tmp_path):
    """A signed-looking message from an unrecognized fingerprint."""
    msg = {
        "version": "1",
        "timestamp": "2026-05-01T10:00Z",
        "from_swarm": "alien-swarm",
        "from_fingerprint": "deadbeefcafebabe",
        "to_fingerprint": "anyone",
        "intent": "work_request",
        "payload": {"description": "Please help with X"},
        "agent_id": "alien-1",
        "signature": "fake",
    }
    f = tmp_path / "msg_from_alien.json"
    f.write_text(json.dumps(msg), encoding='utf-8')
    return f


def test_apply_inbox_quarantines_unknown_peer(home_swarm, stranger_message):
    inbox = home_swarm / _fed.INBOX_DIR
    inbox_msg = inbox / stranger_message.name
    inbox_msg.write_bytes(stranger_message.read_bytes())

    def fake_add(*args, **kwargs):  # should never be called
        raise AssertionError("doorman must block unknown peer")

    result = _fed.apply_inbox_message(home_swarm, inbox_msg, fake_add, object())

    assert result["ok"] is False
    assert result["quarantined"] is True
    assert "strangers/" in result["reason"]
    # The original inbox file is moved out, into strangers/
    assert not inbox_msg.exists()
    bay = list((home_swarm / _fed.STRANGERS_DIR).glob("*.json"))
    assert len(bay) == 1
    reason_file = bay[0].with_suffix(bay[0].suffix + ".reason.txt")
    assert reason_file.exists()


def test_list_strangers_returns_metadata(home_swarm, stranger_message):
    _fed.quarantine_to_strangers(home_swarm, stranger_message, reason="unknown fingerprint")

    entries = _fed.list_strangers(home_swarm)
    assert len(entries) == 1
    e = entries[0]
    assert e["from_fingerprint"] == "deadbeefcafebabe"
    assert e["intent"] == "work_request"
    assert "unknown fingerprint" in e["reason"]


def test_promote_stranger_trusts_peer_and_replays(home_swarm, stranger_message):
    quarantined = _fed.quarantine_to_strangers(
        home_swarm, stranger_message, reason="unknown fingerprint"
    )

    peer, dest = _fed.promote_stranger(
        home_swarm,
        quarantined.name,
        scopes=["work_request"],
        display_name="alien-collaborator",
    )

    assert peer.fingerprint == "deadbeefcafebabe"
    assert peer.scopes == ["work_request"]
    # Peer file persisted
    peer_file = home_swarm / _fed.TRUSTED_PEERS_DIR / f"{peer.fingerprint}.json"
    assert peer_file.exists()
    # Message moved back to inbox/, no longer in strangers/
    assert dest.parent.name == "inbox"
    assert dest.exists()
    assert not quarantined.exists()


def test_promoted_stranger_passes_doorman(home_swarm, stranger_message):
    quarantined = _fed.quarantine_to_strangers(
        home_swarm, stranger_message, reason="unknown fingerprint"
    )
    _fed.promote_stranger(home_swarm, quarantined.name, scopes=["work_request"])

    allowed, _ = _fed.doorman_check(home_swarm, "deadbeefcafebabe", "work_request")
    assert allowed is True


def test_reject_stranger_archives_without_trust(home_swarm, stranger_message):
    quarantined = _fed.quarantine_to_strangers(
        home_swarm, stranger_message, reason="unknown fingerprint"
    )

    archived = _fed.reject_stranger(home_swarm, quarantined.name, reason="suspicious")
    assert archived.parent.name == "rejected"
    assert archived.exists()
    assert not quarantined.exists()

    # Peer is NOT trusted
    allowed, reason = _fed.doorman_check(home_swarm, "deadbeefcafebabe", "work_request")
    assert allowed is False
    assert "trusted peers" in reason


def test_triage_inbox_routes_unknown_peers(home_swarm, stranger_message):
    inbox = home_swarm / _fed.INBOX_DIR
    target = inbox / "msg.json"
    target.write_bytes(stranger_message.read_bytes())

    counts = _fed.triage_inbox(home_swarm)
    assert counts["quarantined"] == 1
    assert counts["trusted"] == 0
    assert not target.exists()
    assert list((home_swarm / _fed.STRANGERS_DIR).glob("*.json"))
