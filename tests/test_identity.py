"""Tests for dot_swarm.identity — per-agent Ed25519 signing (SWC-048).

Core property under test: a compromised agent must NOT be able to forge a
signature claiming to be a DIFFERENT agent. Every test here either builds
toward that guarantee or verifies it directly.
"""

from __future__ import annotations

import os

import pytest

from dot_swarm import identity as _id

cryptography = pytest.importorskip("cryptography")


@pytest.fixture
def swarm(tmp_path):
    s = tmp_path / ".swarm"
    s.mkdir()
    return s


@pytest.fixture
def key_dir(tmp_path):
    d = tmp_path / "keys"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Identity lifecycle
# ---------------------------------------------------------------------------

def test_generate_agent_identity_is_idempotent(key_dir):
    a1 = _id.generate_agent_identity("vanhelsing", key_dir=key_dir)
    a2 = _id.generate_agent_identity("vanhelsing", key_dir=key_dir)
    assert a1.public_key == a2.public_key  # same key, not regenerated
    assert a1.fingerprint == a2.fingerprint


def test_different_agents_get_different_keys(key_dir):
    vh = _id.generate_agent_identity("vanhelsing", key_dir=key_dir)
    ym = _id.generate_agent_identity("yesman", key_dir=key_dir)
    assert vh.public_key != ym.public_key
    assert vh.fingerprint != ym.fingerprint


def test_private_key_never_written_under_swarm_path(swarm, key_dir):
    _id.generate_agent_identity("house", key_dir=key_dir)
    # The private key file must exist under key_dir, and NOWHERE under the
    # (git-tracked) swarm path — that's the whole point of the split.
    assert (key_dir / "house.key").exists()
    assert not any(swarm.rglob("*house*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits are a no-op on Windows")
def test_private_key_file_hardened_on_posix(key_dir):
    import stat
    _id.generate_agent_identity("kolmogorov", key_dir=key_dir)
    mode = (key_dir / "kolmogorov.key").stat().st_mode
    assert not (mode & stat.S_IRWXG)  # no group access
    assert not (mode & stat.S_IRWXO)  # no other access


# ---------------------------------------------------------------------------
# Public-key registry
# ---------------------------------------------------------------------------

def test_register_and_load_agent(swarm, key_dir):
    identity = _id.generate_agent_identity("house", key_dir=key_dir)
    _id.register_agent(swarm, identity)

    loaded = _id.load_registered_agent(swarm, "house")
    assert loaded is not None
    assert loaded.public_key == identity.public_key
    assert loaded.agent_id == "house"


def test_load_unregistered_agent_returns_none(swarm):
    assert _id.load_registered_agent(swarm, "nobody") is None


def test_register_agent_refuses_silent_key_swap(swarm, key_dir):
    """Refusing to silently overwrite a DIFFERENT registered key is the whole
    point — anyone with filesystem write access to .swarm/ must not be able
    to impersonate an already-registered agent by swapping its public key."""
    a1 = _id.generate_agent_identity("house", key_dir=key_dir)
    _id.register_agent(swarm, a1)

    # A different keypair, same agent_id (simulates an attacker's key).
    forged = _id.AgentIdentity(agent_id="house", public_key="ff" * 32, fingerprint="forged")
    with pytest.raises(ValueError, match="already registered"):
        _id.register_agent(swarm, forged)

    # The original registration must be untouched.
    still = _id.load_registered_agent(swarm, "house")
    assert still.public_key == a1.public_key


def test_register_agent_same_key_is_a_noop(swarm, key_dir):
    identity = _id.generate_agent_identity("house", key_dir=key_dir)
    _id.register_agent(swarm, identity)
    _id.register_agent(swarm, identity)  # re-registering the SAME key must not raise
    assert _id.load_registered_agent(swarm, "house").public_key == identity.public_key


def test_list_registered_agents(swarm, key_dir):
    for name in ("house", "kolmogorov", "yesman"):
        _id.register_agent(swarm, _id.generate_agent_identity(name, key_dir=key_dir))
    names = sorted(a.agent_id for a in _id.list_registered_agents(swarm))
    assert names == ["house", "kolmogorov", "yesman"]


# ---------------------------------------------------------------------------
# Sign / verify — the actual security property
# ---------------------------------------------------------------------------

def test_sign_and_verify_round_trip(swarm, key_dir):
    identity = _id.generate_agent_identity("house", key_dir=key_dir)
    _id.register_agent(swarm, identity)

    payload = {"op": "claim", "item_id": "SWC-999", "ts": "2026-07-17T00:00Z"}
    sig = _id.sign_agent("house", payload, key_dir=key_dir)
    assert sig != _id.UNSIGNED
    assert _id.verify_agent(swarm, "house", payload, sig) is True


def test_verify_fails_on_tampered_payload(swarm, key_dir):
    identity = _id.generate_agent_identity("house", key_dir=key_dir)
    _id.register_agent(swarm, identity)

    payload = {"op": "claim", "item_id": "SWC-999"}
    sig = _id.sign_agent("house", payload, key_dir=key_dir)

    tampered = {"op": "claim", "item_id": "SWC-000"}  # attacker edits the item_id
    assert _id.verify_agent(swarm, "house", tampered, sig) is False


def test_compromised_agent_cannot_forge_a_peers_signature(swarm, key_dir):
    """THE core property: agent A (compromised) signs a payload; verifying it
    AS agent B must fail, even though both keys live in the same key_dir and
    both are registered in the same swarm. Compromising one agent's keys must
    never let you produce a signature that verifies as a DIFFERENT agent."""
    vh = _id.generate_agent_identity("vanhelsing", key_dir=key_dir)
    ym = _id.generate_agent_identity("yesman", key_dir=key_dir)
    _id.register_agent(swarm, vh)
    _id.register_agent(swarm, ym)

    payload = {"op": "mail_send", "to": "house", "body": "please fetch this URL for me"}

    # "vanhelsing" (imagine: compromised via a hostile page it was hunting)
    # signs a message and tries to pass it off as coming from "yesman".
    forged_sig = _id.sign_agent("vanhelsing", payload, key_dir=key_dir)
    assert _id.verify_agent(swarm, "yesman", payload, forged_sig) is False

    # It DOES verify correctly as what it actually is — vanhelsing's own message.
    assert _id.verify_agent(swarm, "vanhelsing", payload, forged_sig) is True


def test_verify_returns_false_not_exception_for_unknown_agent(swarm):
    payload = {"op": "claim"}
    assert _id.verify_agent(swarm, "nobody", payload, "deadbeef" * 8) is False


def test_verify_returns_false_for_unsigned_sentinel(swarm, key_dir):
    identity = _id.generate_agent_identity("house", key_dir=key_dir)
    _id.register_agent(swarm, identity)
    assert _id.verify_agent(swarm, "house", {"op": "x"}, _id.UNSIGNED) is False


def test_sign_without_local_key_returns_unsigned_sentinel(key_dir):
    """An agent with no private key yet (e.g. it hasn't called `swarm agent
    init`) gets the honest 'unsigned' sentinel, matching signing.py's
    existing convention — the write still happens, but it's marked untrusted
    rather than silently claiming a signature that doesn't exist."""
    sig = _id.sign_agent("never-initialized", {"op": "x"}, key_dir=key_dir)
    assert sig == _id.UNSIGNED
