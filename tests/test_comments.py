"""Tests for dot_swarm.comments — threaded, signed discussion (SWC-051)."""

from __future__ import annotations

import json

import pytest

from dot_swarm import comments as _comments
from dot_swarm import identity as _identity
from dot_swarm.models import SwarmPaths


@pytest.fixture
def swarm(tmp_path):
    s = tmp_path / ".swarm"
    s.mkdir()
    return SwarmPaths.from_swarm_dir(s)


# ---------------------------------------------------------------------------
# Storage / append semantics
# ---------------------------------------------------------------------------

def test_add_and_read_comment_round_trip(swarm):
    c = _comments.add_comment(swarm, "SWC-001", "house", "This looks solid.")
    thread = _comments.read_comments(swarm, "SWC-001")
    assert len(thread) == 1
    assert thread[0].comment_id == c.comment_id
    assert thread[0].body == "This looks solid."
    assert thread[0].agent_id == "house"


def test_no_comments_returns_empty_list(swarm):
    assert _comments.read_comments(swarm, "SWC-999") == []


def test_comments_append_in_order(swarm):
    _comments.add_comment(swarm, "SWC-001", "house", "first")
    _comments.add_comment(swarm, "SWC-001", "kolmogorov", "second")
    _comments.add_comment(swarm, "SWC-001", "house", "third")
    thread = _comments.read_comments(swarm, "SWC-001")
    assert [c.body for c in thread] == ["first", "second", "third"]


def test_comments_scoped_per_item(swarm):
    _comments.add_comment(swarm, "SWC-001", "house", "on 001")
    _comments.add_comment(swarm, "SWC-002", "house", "on 002")
    assert [c.body for c in _comments.read_comments(swarm, "SWC-001")] == ["on 001"]
    assert [c.body for c in _comments.read_comments(swarm, "SWC-002")] == ["on 002"]


def test_reply_threading(swarm):
    parent = _comments.add_comment(swarm, "SWC-001", "house", "question?")
    reply = _comments.add_comment(swarm, "SWC-001", "kolmogorov", "answer.", in_reply_to=parent.comment_id)
    assert reply.in_reply_to == parent.comment_id


def test_reply_to_unknown_comment_id_still_recorded(swarm):
    """A reply arriving before its parent (e.g. out-of-order federation sync)
    must not be silently dropped."""
    reply = _comments.add_comment(swarm, "SWC-001", "house", "re: something", in_reply_to="deadbeef1234")
    thread = _comments.read_comments(swarm, "SWC-001")
    assert thread[0].in_reply_to == "deadbeef1234"


def test_malformed_line_is_skipped_not_fatal(swarm):
    _comments.add_comment(swarm, "SWC-001", "house", "good comment")
    path = swarm.root / "comments" / "SWC-001.jsonl"
    with path.open("a") as fh:
        fh.write("{not valid json\n")
    _comments.add_comment(swarm, "SWC-001", "house", "another good one")

    thread = _comments.read_comments(swarm, "SWC-001")
    assert [c.body for c in thread] == ["good comment", "another good one"]


def test_comment_ids_are_content_addressed_and_distinct(swarm):
    a = _comments.add_comment(swarm, "SWC-001", "house", "hello")
    b = _comments.add_comment(swarm, "SWC-001", "kolmogorov", "hello")  # same body, different agent
    assert a.comment_id != b.comment_id


# ---------------------------------------------------------------------------
# Signing (mirrors identity.py's own conventions)
# ---------------------------------------------------------------------------

def test_comment_unsigned_when_agent_has_no_local_key(swarm):
    c = _comments.add_comment(swarm, "SWC-001", "never-initialized", "hi")
    assert c.signature == _identity.UNSIGNED
    assert _comments.verify_comment(swarm, c) is False


@pytest.mark.skipif(not _identity.has_crypto(), reason="cryptography not installed")
def test_comment_signed_and_verifies_when_agent_registered(swarm, tmp_path):
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    ident = _identity.generate_agent_identity("house", key_dir=key_dir)
    _identity.register_agent(swarm.root, ident)

    import os
    os.environ["DOT_SWARM_AGENT_KEY_DIR"] = str(key_dir)
    try:
        c = _comments.add_comment(swarm, "SWC-001", "house", "signed comment")
    finally:
        del os.environ["DOT_SWARM_AGENT_KEY_DIR"]

    assert c.signature != _identity.UNSIGNED
    assert _comments.verify_comment(swarm, c) is True


@pytest.mark.skipif(not _identity.has_crypto(), reason="cryptography not installed")
def test_tampered_comment_body_fails_verification(swarm, tmp_path):
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    ident = _identity.generate_agent_identity("house", key_dir=key_dir)
    _identity.register_agent(swarm.root, ident)

    import os
    os.environ["DOT_SWARM_AGENT_KEY_DIR"] = str(key_dir)
    try:
        c = _comments.add_comment(swarm, "SWC-001", "house", "original")
    finally:
        del os.environ["DOT_SWARM_AGENT_KEY_DIR"]

    c.body = "tampered"
    assert _comments.verify_comment(swarm, c) is False


@pytest.mark.skipif(not _identity.has_crypto(), reason="cryptography not installed")
def test_compromised_agent_cannot_forge_a_comment_as_a_peer(swarm, tmp_path):
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    vh = _identity.generate_agent_identity("vanhelsing", key_dir=key_dir)
    ym = _identity.generate_agent_identity("yesman", key_dir=key_dir)
    _identity.register_agent(swarm.root, vh)
    _identity.register_agent(swarm.root, ym)

    import os
    os.environ["DOT_SWARM_AGENT_KEY_DIR"] = str(key_dir)
    try:
        c = _comments.add_comment(swarm, "SWC-001", "vanhelsing", "trust me, I'm yesman")
    finally:
        del os.environ["DOT_SWARM_AGENT_KEY_DIR"]

    # Relabel the recorded comment as if it came from yesman — the signature
    # was produced with vanhelsing's key and must not verify as yesman's.
    forged = _comments.Comment(
        item_id=c.item_id, agent_id="yesman", body=c.body, timestamp=c.timestamp,
        comment_id=c.comment_id, in_reply_to=c.in_reply_to, signature=c.signature,
    )
    assert _comments.verify_comment(swarm, forged) is False
    assert _comments.verify_comment(swarm, c) is True
