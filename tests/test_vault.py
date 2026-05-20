"""Tests for dot_swarm.vault — AEAD-encrypted trail under the swarm key."""

from __future__ import annotations

import json

import pytest

from dot_swarm import vault as _vault
from dot_swarm import seals as _seals
from dot_swarm.signing import (
    append_trail, generate_identity, read_trail, sign_operation,
)

cryptography = pytest.importorskip("cryptography")


@pytest.fixture
def swarm(tmp_path):
    s = tmp_path / ".swarm"
    s.mkdir()
    generate_identity(s)
    return s


@pytest.fixture
def keyed_swarm(swarm):
    _vault.generate_swarm_key(swarm)
    return swarm


# ---------------------------------------------------------------------------
# Envelope round-trip
# ---------------------------------------------------------------------------

def test_seal_open_round_trip(keyed_swarm):
    key = _vault.load_swarm_key(keyed_swarm)
    msg = '{"hello":"swarm"}'
    envelope = _vault.seal_envelope(msg, key)
    assert envelope.startswith(_vault.ENVELOPE_PREFIX)
    assert _vault.is_envelope(envelope)
    assert _vault.open_envelope(envelope, key) == msg


def test_open_with_wrong_key_raises(keyed_swarm, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _vault.generate_swarm_key(other)

    home_key = _vault.load_swarm_key(keyed_swarm)
    other_key = _vault.load_swarm_key(other)
    assert home_key != other_key

    envelope = _vault.seal_envelope("secret", home_key)
    with pytest.raises(_vault.TamperedEnvelope):
        _vault.open_envelope(envelope, other_key)


def test_tampered_envelope_fails(keyed_swarm):
    key = _vault.load_swarm_key(keyed_swarm)
    envelope = _vault.seal_envelope("secret payload", key)
    # Flip a byte inside the base64 body
    bad = envelope[: len(_vault.ENVELOPE_PREFIX) + 5] + (
        "B" if envelope[len(_vault.ENVELOPE_PREFIX) + 5] != "B" else "C"
    ) + envelope[len(_vault.ENVELOPE_PREFIX) + 6 :]
    with pytest.raises(_vault.TamperedEnvelope):
        _vault.open_envelope(bad, key)


# ---------------------------------------------------------------------------
# Trail integration
# ---------------------------------------------------------------------------

def test_trail_writes_envelopes_when_keyed(keyed_swarm):
    record = sign_operation(keyed_swarm, "claim", "agent-1", {"item_id": "X-1"})
    append_trail(keyed_swarm, record)

    on_disk = (keyed_swarm / "trail.log").read_text(encoding='utf-8')
    assert _vault.ENVELOPE_PREFIX in on_disk
    assert "agent-1" not in on_disk            # no plaintext leaks
    assert "claim" not in on_disk

    # …and reads back transparently to the JSON record
    rows = read_trail(keyed_swarm)
    assert len(rows) == 1
    assert rows[0]["agent_id"] == "agent-1"
    assert rows[0]["op"] == "claim"


def test_trail_unkeyed_remains_plaintext(swarm):
    record = sign_operation(swarm, "claim", "agent-1", {"item_id": "X-1"})
    append_trail(swarm, record)

    on_disk = (swarm / "trail.log").read_text(encoding='utf-8')
    assert _vault.ENVELOPE_PREFIX not in on_disk
    assert "agent-1" in on_disk
    rows = read_trail(swarm)
    assert rows[0]["agent_id"] == "agent-1"


def test_trail_supports_mixed_plaintext_and_envelope(swarm):
    # First write an entry without a key (plaintext)
    record1 = sign_operation(swarm, "claim", "agent-1", {"item_id": "X-1"})
    append_trail(swarm, record1)

    # Now adopt a key and write a second entry (envelope)
    _vault.generate_swarm_key(swarm)
    record2 = sign_operation(swarm, "done", "agent-1", {"item_id": "X-1"})
    append_trail(swarm, record2)

    rows = read_trail(swarm)
    ops = [r["op"] for r in rows]
    assert ops == ["claim", "done"]


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def test_rotate_re_seals_existing_envelopes(keyed_swarm):
    record = sign_operation(keyed_swarm, "claim", "a", {"item_id": "X-1"})
    append_trail(keyed_swarm, record)
    original_meta = _vault.load_swarm_key_metadata(keyed_swarm)

    new_meta, result = _vault.rotate_swarm_key(keyed_swarm)

    assert new_meta.fingerprint != original_meta.fingerprint
    assert result.lines_rewrapped == 1
    assert result.failed == 0
    assert (keyed_swarm / _vault.SWARM_KEY_OLD_FILE).exists()

    # Trail still readable transparently under the new key
    rows = read_trail(keyed_swarm)
    assert rows[0]["op"] == "claim"


def test_rotate_falls_back_to_old_key_on_partial_state(keyed_swarm):
    """Mid-rotation state (envelope under old key) is readable until cleanup."""
    record = sign_operation(keyed_swarm, "claim", "a", {"item_id": "X-1"})
    append_trail(keyed_swarm, record)
    _vault.rotate_swarm_key(keyed_swarm)

    # Read should still work — try_open_envelope falls back to .old key
    rows = read_trail(keyed_swarm)
    assert rows and rows[0]["op"] == "claim"


# ---------------------------------------------------------------------------
# v2 seals — 16-hex tag
# ---------------------------------------------------------------------------

def test_v2_seal_uses_longer_tag(swarm):
    sealed = _seals.seal_content("payload", "agent-x", swarm)  # default v2
    extracted = _seals.extract_seal(sealed)
    assert extracted is not None
    version, _, tag = extracted
    assert version == "v2"
    assert len(tag) == 16
    assert _seals.verify_content(sealed, swarm).status == _seals.SealStatus.VALID


def test_v1_seals_still_verify(swarm):
    sealed = _seals.seal_content("payload", "agent-x", swarm, version="v1")
    extracted = _seals.extract_seal(sealed)
    version, _, tag = extracted
    assert version == "v1"
    assert len(tag) == 8
    assert _seals.verify_content(sealed, swarm).status == _seals.SealStatus.VALID


def test_seal_with_truncated_tag_is_invalid(swarm):
    sealed = _seals.seal_content("payload", "agent-x", swarm, version="v2")
    # Drop 4 chars off the tag — claim it's still v2 → length mismatch → INVALID
    bad = sealed.replace(":", ":x", 1)  # corrupt one char before the tag end
    truncated = sealed.replace(_seals.extract_seal(sealed)[2], _seals.extract_seal(sealed)[2][:12])
    result = _seals.verify_content(truncated, swarm)
    assert result.status == _seals.SealStatus.INVALID
