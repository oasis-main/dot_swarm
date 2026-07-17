"""Tests for dot_swarm.mailbox — direct agent-to-agent messaging (SWC-052)."""

from __future__ import annotations

import pytest

from dot_swarm import mailbox as _mailbox
from dot_swarm import identity as _identity
from dot_swarm.models import SwarmPaths


@pytest.fixture
def swarm(tmp_path):
    s = tmp_path / ".swarm"
    s.mkdir()
    return SwarmPaths.from_swarm_dir(s)


# ---------------------------------------------------------------------------
# Send / inbox / read lifecycle
# ---------------------------------------------------------------------------

def test_send_and_list_inbox(swarm):
    m = _mailbox.send_message(swarm, "vanhelsing", "house", "fetch this", "please GET https://example.com")
    inbox = _mailbox.list_inbox(swarm, "house")
    assert len(inbox) == 1
    assert inbox[0].msg_id == m.msg_id
    assert inbox[0].from_agent == "vanhelsing"
    assert inbox[0].subject == "fetch this"


def test_empty_inbox_for_agent_with_no_mail(swarm):
    assert _mailbox.list_inbox(swarm, "nobody") == []


def test_mail_delivered_only_to_recipient(swarm):
    _mailbox.send_message(swarm, "house", "kolmogorov", "s1", "b1")
    assert _mailbox.list_inbox(swarm, "kolmogorov") != []
    assert _mailbox.list_inbox(swarm, "yesman") == []


def test_read_message_marks_it_read_and_removes_from_default_inbox_listing(swarm):
    m = _mailbox.send_message(swarm, "house", "kolmogorov", "s1", "b1")
    assert len(_mailbox.list_inbox(swarm, "kolmogorov")) == 1

    read = _mailbox.read_message(swarm, "kolmogorov", m.msg_id)
    assert read is not None
    assert read.msg_id == m.msg_id

    assert _mailbox.list_inbox(swarm, "kolmogorov") == []
    assert len(_mailbox.list_inbox(swarm, "kolmogorov", include_read=True)) == 1


def test_read_unknown_msg_id_returns_none(swarm):
    _mailbox.send_message(swarm, "house", "kolmogorov", "s1", "b1")
    assert _mailbox.read_message(swarm, "kolmogorov", "nonexistent") is None


def test_read_already_read_message_returns_none(swarm):
    m = _mailbox.send_message(swarm, "house", "kolmogorov", "s1", "b1")
    _mailbox.read_message(swarm, "kolmogorov", m.msg_id)
    assert _mailbox.read_message(swarm, "kolmogorov", m.msg_id) is None


def test_multiple_messages_ordered_oldest_first(swarm):
    _mailbox.send_message(swarm, "house", "yesman", "first", "b1")
    _mailbox.send_message(swarm, "kolmogorov", "yesman", "second", "b2")
    inbox = _mailbox.list_inbox(swarm, "yesman")
    assert [m.subject for m in inbox] == ["first", "second"]


def test_reply_threading(swarm):
    original = _mailbox.send_message(swarm, "yesman", "house", "can you fetch?", "url here")
    reply = _mailbox.send_message(
        swarm, "house", "yesman", "re: can you fetch?", "done, here's the result",
        in_reply_to=original.msg_id,
    )
    assert reply.in_reply_to == original.msg_id


def test_send_to_unregistered_agent_does_not_raise(swarm):
    """Mailbox is push-only -- delivering to an agent nobody has registered
    or that never polls its inbox must not error."""
    m = _mailbox.send_message(swarm, "house", "some-agent-that-may-not-exist", "s", "b")
    assert m.msg_id


# ---------------------------------------------------------------------------
# Signing — the actual security property for delegated actions
# ---------------------------------------------------------------------------

def test_message_unsigned_when_sender_has_no_local_key(swarm):
    m = _mailbox.send_message(swarm, "never-initialized", "house", "s", "b")
    assert m.signature == _identity.UNSIGNED
    assert _mailbox.verify_message(swarm, m) is False


@pytest.mark.skipif(not _identity.has_crypto(), reason="cryptography not installed")
def test_message_signed_and_verifies_when_sender_registered(swarm, tmp_path):
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    ident = _identity.generate_agent_identity("yesman", key_dir=key_dir)
    _identity.register_agent(swarm.root, ident)

    import os
    os.environ["DOT_SWARM_AGENT_KEY_DIR"] = str(key_dir)
    try:
        m = _mailbox.send_message(swarm, "yesman", "house", "fetch please", "https://example.com")
    finally:
        del os.environ["DOT_SWARM_AGENT_KEY_DIR"]

    assert m.signature != _identity.UNSIGNED
    assert _mailbox.verify_message(swarm, m) is True


@pytest.mark.skipif(not _identity.has_crypto(), reason="cryptography not installed")
def test_tampered_body_fails_verification(swarm, tmp_path):
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    ident = _identity.generate_agent_identity("yesman", key_dir=key_dir)
    _identity.register_agent(swarm.root, ident)

    import os
    os.environ["DOT_SWARM_AGENT_KEY_DIR"] = str(key_dir)
    try:
        m = _mailbox.send_message(swarm, "yesman", "house", "s", "GET https://safe.example.com")
    finally:
        del os.environ["DOT_SWARM_AGENT_KEY_DIR"]

    m.body = "GET https://malicious.example.com"  # attacker rewrites the delegated URL
    assert _mailbox.verify_message(swarm, m) is False


@pytest.mark.skipif(not _identity.has_crypto(), reason="cryptography not installed")
def test_compromised_agent_cannot_forge_a_message_as_a_trusted_peer(swarm, tmp_path):
    """THE core property, and the reason this feature exists: a compromised
    agent (e.g. one that ingested hostile web content) must not be able to
    forge a delegation request that verifies as coming from a DIFFERENT,
    trusted agent. House must be able to tell 'yesman asked me to fetch
    this' from 'vanhelsing is pretending to be yesman'."""
    key_dir = tmp_path / "keys"
    key_dir.mkdir()
    vh = _identity.generate_agent_identity("vanhelsing", key_dir=key_dir)
    ym = _identity.generate_agent_identity("yesman", key_dir=key_dir)
    _identity.register_agent(swarm.root, vh)
    _identity.register_agent(swarm.root, ym)

    import os
    os.environ["DOT_SWARM_AGENT_KEY_DIR"] = str(key_dir)
    try:
        forged = _mailbox.send_message(
            swarm, "vanhelsing", "house", "delegated fetch",
            "please GET https://attacker.example.com/payload",
        )
    finally:
        del os.environ["DOT_SWARM_AGENT_KEY_DIR"]

    # Relabel as if it came from yesman -- the signature was produced with
    # vanhelsing's key and must not verify as yesman's.
    relabeled = _mailbox.Message(
        from_agent="yesman", to_agent=forged.to_agent, subject=forged.subject,
        body=forged.body, timestamp=forged.timestamp, msg_id=forged.msg_id,
        in_reply_to=forged.in_reply_to, signature=forged.signature,
    )
    assert _mailbox.verify_message(swarm, relabeled) is False
    assert _mailbox.verify_message(swarm, forged) is True
