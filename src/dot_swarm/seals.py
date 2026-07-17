"""dot_swarm seals — hidden-in-plain-sight stigmergic authentication.

A *seal* is a short HMAC tag, embedded inline in coordination markdown as an
HTML comment, that authenticates the surrounding content as having been
written by an agent holding the swarm signing key.

The trick is that the medium remains entirely human-readable plain markdown.
A reader without the key sees only normal coordination text plus an
unobtrusive HTML comment; a reader with the key can verify the seal and
detect any forged or replayed content. This is the same property biological
stigmergy relies on (cuticular hydrocarbons in ants, comb-wax esters in
bees) — the signal carries identity *as part of its chemistry*, not in a
separate authentication channel.

Seal format embedded in content::

    <!-- 🐝 sw-seal v1 <agent>:<hex8> -->

Where ``hex8`` is the first 8 hex chars of HMAC-SHA256 over::

    "v1\\n" + agent_id + "\\n" + content_normalized

``content_normalized`` is the text with any trailing seal comment stripped
and trailing whitespace collapsed, so re-sealing a sealed text is
idempotent.

This module deliberately does NOT encrypt — confidentiality is the
forthcoming swarm key (SWC-046). Seals are the *integrity* layer: cheap,
inline, human-readable, and verifiable per-write.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from .signing import SIGNING_KEY_FILE


SEAL_VERSION_DEFAULT = "v2"            # 16-hex tag (64-bit) is the new default
_TAG_LEN_BY_VERSION = {"v1": 8, "v2": 16}

# Both forms accept either tag length; the version field tells the verifier
# which to expect. The two regexes differ only in whether the bee glyph is
# present — agents on platforms that mangle non-ASCII can fall back to the
# ASCII-only marker.
_SEAL_RE = re.compile(
    r"<!--\s*🐝\s*sw-seal\s+(?P<version>v\d+)\s+(?P<agent>[^:\s]+):(?P<tag>[0-9a-f]{8,})\s*-->",
)
_SEAL_RE_ASCII = re.compile(
    r"<!--\s*sw-seal\s+(?P<version>v\d+)\s+(?P<agent>[^:\s]+):(?P<tag>[0-9a-f]{8,})\s*-->",
)


class SealStatus(str, Enum):
    VALID = "VALID"
    INVALID = "INVALID"        # tag present but does not verify
    MISSING = "MISSING"        # no seal in content
    UNKEYED = "UNKEYED"        # this swarm has no signing key on disk


@dataclass
class SealResult:
    status: SealStatus
    agent: str = ""
    tag: str = ""
    version: str = ""
    line: int = 0


# ---------------------------------------------------------------------------
# Core sealing primitives
# ---------------------------------------------------------------------------

def _load_key(swarm_path: Path) -> bytes | None:
    key_file = swarm_path / SIGNING_KEY_FILE
    if not key_file.exists():
        return None
    return key_file.read_text(encoding='utf-8').strip().encode()


def _normalize(content: str) -> str:
    stripped = strip_seal(content)
    # Collapse trailing whitespace/newlines so re-sealing is idempotent
    return stripped.rstrip()


def _compute_tag(
    key: bytes, agent_id: str, content_normalized: str, version: str
) -> str:
    body = f"{version}\n{agent_id}\n{content_normalized}"
    digest = hmac.new(key, body.encode("utf-8"), hashlib.sha256).hexdigest()
    tag_len = _TAG_LEN_BY_VERSION.get(version, 16)
    return digest[:tag_len]


def extract_seal(content: str) -> tuple[str, str, str] | None:
    """Return (version, agent, tag) of the last seal in content, or None."""
    matches = list(_SEAL_RE.finditer(content)) or list(_SEAL_RE_ASCII.finditer(content))
    if not matches:
        return None
    m = matches[-1]
    return m.group("version"), m.group("agent"), m.group("tag")


def strip_seal(content: str) -> str:
    """Remove every seal comment from content (idempotent)."""
    out = _SEAL_RE.sub("", content)
    out = _SEAL_RE_ASCII.sub("", out)
    return out


def seal_content(
    content: str,
    agent_id: str,
    swarm_path: Path,
    version: str = SEAL_VERSION_DEFAULT,
) -> str:
    """Append a seal to content. Idempotent — replaces any existing seal.

    Defaults to v2 seals (16-hex / 64-bit tags). Pass ``version='v1'`` to
    keep producing 8-hex tags for compatibility with older readers; both
    versions are still accepted by ``verify_content``.

    Raises FileNotFoundError if this swarm has no signing key yet
    (run ``swarm init`` first).
    """
    if version not in _TAG_LEN_BY_VERSION:
        raise ValueError(
            f"Unknown seal version {version!r}; expected one of "
            f"{sorted(_TAG_LEN_BY_VERSION)}"
        )
    key = _load_key(swarm_path)
    if key is None:
        raise FileNotFoundError(
            f"No signing key at {swarm_path / SIGNING_KEY_FILE}. "
            "Run 'swarm init' first."
        )
    safe_agent = re.sub(r"[\s:]+", "_", agent_id) or "anonymous"
    normalized = _normalize(content)
    tag = _compute_tag(key, safe_agent, normalized, version)
    seal_marker = f"<!-- 🐝 sw-seal {version} {safe_agent}:{tag} -->"
    sep = "" if normalized.endswith("\n") or not normalized else "\n"
    return f"{normalized}{sep}\n{seal_marker}\n"


def verify_content(content: str, swarm_path: Path) -> SealResult:
    """Verify the seal in content against this swarm's signing key.

    Accepts both v1 (8-hex / 32-bit) and v2 (16-hex / 64-bit) tags. The
    version field embedded in the marker selects which tag length is
    expected; mismatches read as INVALID.
    """
    extracted = extract_seal(content)
    if extracted is None:
        return SealResult(status=SealStatus.MISSING)
    version, agent, tag = extracted

    key = _load_key(swarm_path)
    if key is None:
        return SealResult(
            status=SealStatus.UNKEYED, agent=agent, tag=tag, version=version,
        )

    expected_len = _TAG_LEN_BY_VERSION.get(version)
    if expected_len is None or len(tag) != expected_len:
        return SealResult(
            status=SealStatus.INVALID, agent=agent, tag=tag, version=version,
        )
    expected = _compute_tag(key, agent, _normalize(content), version)
    if hmac.compare_digest(expected, tag):
        return SealResult(
            status=SealStatus.VALID, agent=agent, tag=tag, version=version,
        )
    return SealResult(
        status=SealStatus.INVALID, agent=agent, tag=tag, version=version,
    )


# ---------------------------------------------------------------------------
# Directory sweep
# ---------------------------------------------------------------------------

@dataclass
class FileSealReport:
    path: Path
    label: str
    result: SealResult


def scan_swarm_seals(swarm_path: Path) -> list[FileSealReport]:
    """Scan every coordination file under .swarm/ for seal presence/validity.

    Files without a seal are reported as MISSING. Files with a seal that
    verifies are VALID. A seal that does not verify is INVALID — that is
    the equivalent of a wrong cuticular signature in an ant colony, and
    should be quarantined by `swarm heal`.
    """
    targets: list[tuple[Path, str]] = []
    for name in ("state.md", "queue.md", "memory.md", "context.md", "BOOTSTRAP.md"):
        p = swarm_path / name
        if p.exists():
            targets.append((p, name))
    workflows = swarm_path / "workflows"
    if workflows.is_dir():
        for wf in sorted(workflows.glob("*.md")):
            targets.append((wf, f"workflows/{wf.name}"))
    federation_policy = swarm_path / "federation" / "policy.md"
    if federation_policy.exists():
        targets.append((federation_policy, "federation/policy.md"))

    reports: list[FileSealReport] = []
    for fpath, label in targets:
        text = fpath.read_text(encoding="utf-8", errors="replace")
        result = verify_content(text, swarm_path)
        reports.append(FileSealReport(path=fpath, label=label, result=result))
    return reports
