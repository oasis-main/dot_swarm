"""Tests for dot_swarm.seals — hidden-in-plain-sight stigmergic authentication."""

from __future__ import annotations

import pytest

from dot_swarm import seals as _seals
from dot_swarm.signing import generate_identity


@pytest.fixture
def keyed_swarm(tmp_path):
    swarm = tmp_path / ".swarm"
    swarm.mkdir()
    generate_identity(swarm)
    return swarm


@pytest.fixture
def unkeyed_swarm(tmp_path):
    swarm = tmp_path / ".swarm"
    swarm.mkdir()
    return swarm


def test_seal_then_verify_round_trip(keyed_swarm):
    text = "## Handoff Note\n\nWatch for race in claim_item.\n"
    sealed = _seals.seal_content(text, "claude-code", keyed_swarm)
    assert "<!-- 🐝 sw-seal v2 " in sealed
    assert _seals.verify_content(sealed, keyed_swarm).status == _seals.SealStatus.VALID


def test_seal_is_idempotent(keyed_swarm):
    text = "decision: x"
    sealed1 = _seals.seal_content(text, "claude-code", keyed_swarm)
    sealed2 = _seals.seal_content(sealed1, "claude-code", keyed_swarm)
    assert sealed1 == sealed2
    assert _seals.verify_content(sealed2, keyed_swarm).status == _seals.SealStatus.VALID


def test_unsealed_content_reports_missing(keyed_swarm):
    plain = "no seal here at all"
    assert _seals.verify_content(plain, keyed_swarm).status == _seals.SealStatus.MISSING


def test_tampered_seal_is_invalid(keyed_swarm):
    sealed = _seals.seal_content("hello swarm", "claude-code", keyed_swarm)
    tampered = sealed.replace("hello swarm", "GOODBYE swarm")
    result = _seals.verify_content(tampered, keyed_swarm)
    assert result.status == _seals.SealStatus.INVALID
    assert result.agent == "claude-code"


def test_foreign_swarm_seal_is_invalid(keyed_swarm, tmp_path):
    other_swarm = tmp_path / "other.swarm"
    other_swarm.mkdir()
    generate_identity(other_swarm)

    sealed_by_other = _seals.seal_content("foreign signal", "alien", other_swarm)
    # The seal verifies under its own key
    assert _seals.verify_content(sealed_by_other, other_swarm).status == _seals.SealStatus.VALID
    # …but reads as INVALID under the home swarm's key — wrong "cuticular signature"
    assert _seals.verify_content(sealed_by_other, keyed_swarm).status == _seals.SealStatus.INVALID


def test_unkeyed_swarm_reports_unkeyed(unkeyed_swarm):
    fake_seal = "content\n<!-- 🐝 sw-seal v1 agent:deadbeef -->\n"
    assert _seals.verify_content(fake_seal, unkeyed_swarm).status == _seals.SealStatus.UNKEYED


def test_seal_sweep_reports_per_file(keyed_swarm):
    (keyed_swarm / "queue.md").write_text("queue content", encoding='utf-8')
    (keyed_swarm / "state.md").write_text(
        _seals.seal_content("state content", "claude", keyed_swarm),
        encoding='utf-8',
    )

    reports = _seals.scan_swarm_seals(keyed_swarm)
    by_label = {r.label: r.result.status for r in reports}
    assert by_label["queue.md"] == _seals.SealStatus.MISSING
    assert by_label["state.md"] == _seals.SealStatus.VALID


def test_strip_seal_removes_marker(keyed_swarm):
    sealed = _seals.seal_content("some text", "agent-x", keyed_swarm)
    assert "🐝" not in _seals.strip_seal(sealed)
