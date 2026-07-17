"""dot_swarm per-agent identity — Ed25519 signing (SWC-048).

``signing.py`` gives every ``.swarm/`` directory ONE shared HMAC key: any
process holding it can produce a valid signature for ANY ``agent_id`` (the
agent name is just a field in the signed payload, not bound to distinct key
material). That's fine when every writer is equally trusted. It stops being
fine once agents can be independently compromised — e.g. one agent ingests
hostile web content and gets prompt-injected — because a compromised agent
holding the shared swarm key can then forge a signature claiming to be a
DIFFERENT, uncompromised agent.

This module gives each AGENT (not each swarm) its own Ed25519 keypair:

  - The PRIVATE key never lives inside ``.swarm/`` and is never committed —
    it lives in the agent's own local storage (default
    ``~/.dot_swarm/keys/<agent_id>.key``, override via
    ``DOT_SWARM_AGENT_KEY_DIR``), matching the CONTRIBUTING.md norm that
    ``.swarm/`` itself is the shared, git-tracked coordination surface.
  - The PUBLIC key is safe to commit and lives in the shared swarm at
    ``.swarm/agents/<agent_id>.json`` — the same "public fingerprint, safe
    to commit" split ``signing.py`` already documents for ``identity.json``.

Compromising agent A's container never lets you forge agent B's signature:
verification only ever needs A's PUBLIC key, and A signing something can
only ever be checked against A's own registered key.

This does NOT replace ``signing.py``'s swarm-wide HMAC trail (still useful
for "did this swarm-scoped operation happen at all"). It's a second,
per-agent-verifiable layer for anything crossing an agent-trust boundary:
claims, comments (SWC-051), mailbox messages (SWC-052).

Confidentiality is a SEPARATE concern (see ``vault.py``) — signing proves
who wrote something and that it wasn't altered; it does not hide the
content. A payload needing confidentiality should go through
``vault.seal_envelope``, not be assumed protected by a signature.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

AGENTS_DIR = "agents"          # .swarm/agents/<agent_id>.json — public, committed
KEY_FILENAME_SUFFIX = ".key"   # <key_dir>/<agent_id>.key — private, never committed

UNSIGNED = "unsigned"          # sentinel signature value, mirrors signing.py's convention


class CryptoUnavailable(RuntimeError):
    """Raised when the optional `cryptography` package is not installed."""


class UnknownAgent(ValueError):
    """Raised when verifying against an agent with no registered public key."""


# ---------------------------------------------------------------------------
# Optional-dependency probe (same pattern as vault.py)
# ---------------------------------------------------------------------------

def has_crypto() -> bool:
    """True if the optional `cryptography` library is importable."""
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def _ed25519():
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey, Ed25519PublicKey,
        )
        from cryptography.hazmat.primitives import serialization
        from cryptography.exceptions import InvalidSignature
    except ImportError as exc:
        raise CryptoUnavailable(
            "Per-agent signing requires the optional 'cryptography' package. "
            "Install it with:  pip install 'dot-swarm[crypto]'"
        ) from exc
    return Ed25519PrivateKey, Ed25519PublicKey, serialization, InvalidSignature


# ---------------------------------------------------------------------------
# Private key storage (local to the agent, NEVER inside .swarm/)
# ---------------------------------------------------------------------------

def default_key_dir() -> Path:
    override = os.environ.get("DOT_SWARM_AGENT_KEY_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".dot_swarm" / "keys"


def _key_path(agent_id: str, key_dir: Path | None) -> Path:
    return (key_dir or default_key_dir()) / f"{agent_id}{KEY_FILENAME_SUFFIX}"


def _harden_private_key_file(path: Path) -> None:
    """Best-effort 0600 on the private key file. POSIX permission bits are
    largely a no-op on Windows filesystems — this is defense-in-depth, not a
    hard requirement, so failures are swallowed rather than raised."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Identity lifecycle
# ---------------------------------------------------------------------------

@dataclass
class AgentIdentity:
    agent_id: str
    public_key: str          # hex-encoded 32-byte Ed25519 public key
    algorithm: str = "ed25519"
    fingerprint: str = ""
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "public_key": self.public_key,
            "algorithm": self.algorithm,
            "fingerprint": self.fingerprint,
            "created": self.created,
        }


def generate_agent_identity(agent_id: str, key_dir: Path | None = None) -> AgentIdentity:
    """Generate (or load, if one already exists) this agent's Ed25519 keypair.

    Idempotent — an existing private key is never overwritten, matching
    ``signing.generate_identity``'s behavior for the swarm-wide key. Returns
    the PUBLIC identity only; the private key never leaves ``key_dir``.
    """
    Ed25519PrivateKey, _, serialization, _ = _ed25519()

    kdir = key_dir or default_key_dir()
    kdir.mkdir(parents=True, exist_ok=True)
    key_path = _key_path(agent_id, kdir)

    if key_path.exists():
        private_key = Ed25519PrivateKey.from_private_bytes(
            bytes.fromhex(key_path.read_text(encoding="utf-8").strip())
        )
    else:
        private_key = Ed25519PrivateKey.generate()
        raw = private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key_path.write_text(raw.hex(), encoding="utf-8")
        _harden_private_key_file(key_path)

    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    public_hex = public_bytes.hex()
    fingerprint = hashlib.sha256(public_bytes).hexdigest()[:16]
    return AgentIdentity(agent_id=agent_id, public_key=public_hex, fingerprint=fingerprint)


def _load_private_key(agent_id: str, key_dir: Path | None = None):
    Ed25519PrivateKey, _, _, _ = _ed25519()
    key_path = _key_path(agent_id, key_dir)
    if not key_path.exists():
        return None
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(key_path.read_text(encoding="utf-8").strip()))


# ---------------------------------------------------------------------------
# Public-key registry (shared, committed — lives in the swarm, not the agent)
# ---------------------------------------------------------------------------

def _agents_dir(swarm_path: Path) -> Path:
    return swarm_path / AGENTS_DIR


def register_agent(swarm_path: Path, identity: AgentIdentity) -> Path:
    """Publish an agent's PUBLIC key into the shared, git-trackable registry.

    Never overwrites a DIFFERENT key for the same agent_id silently — that
    would let anyone with filesystem write access to .swarm/ impersonate an
    already-registered agent by swapping its public key. A caller that
    genuinely needs to rotate a key must remove the old registration first
    (an explicit, auditable act, not a side effect of registering).
    """
    d = _agents_dir(swarm_path)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{identity.agent_id}.json"
    if dest.exists():
        existing = json.loads(dest.read_text(encoding="utf-8"))
        if existing.get("public_key") != identity.public_key:
            raise ValueError(
                f"agent '{identity.agent_id}' is already registered with a "
                f"DIFFERENT public key — refusing to silently overwrite. "
                f"Remove {dest} first if this is an intentional key rotation."
            )
        return dest
    dest.write_text(json.dumps(identity.to_dict(), indent=2), encoding="utf-8")
    return dest


def load_registered_agent(swarm_path: Path, agent_id: str) -> AgentIdentity | None:
    p = _agents_dir(swarm_path) / f"{agent_id}.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return AgentIdentity(
        agent_id=data.get("agent_id", agent_id),
        public_key=data.get("public_key", ""),
        algorithm=data.get("algorithm", "ed25519"),
        fingerprint=data.get("fingerprint", ""),
        created=data.get("created", ""),
    )


def list_registered_agents(swarm_path: Path) -> list[AgentIdentity]:
    d = _agents_dir(swarm_path)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        out.append(AgentIdentity(
            agent_id=data.get("agent_id", f.stem),
            public_key=data.get("public_key", ""),
            algorithm=data.get("algorithm", "ed25519"),
            fingerprint=data.get("fingerprint", ""),
            created=data.get("created", ""),
        ))
    return out


# ---------------------------------------------------------------------------
# Sign / verify
# ---------------------------------------------------------------------------

def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_agent(agent_id: str, payload: dict, key_dir: Path | None = None) -> str:
    """Sign payload with agent_id's own private key. Returns a hex signature,
    or the UNSIGNED sentinel if the agent has no local private key yet (the
    write still happens — trail-style honesty — but downstream verification
    must treat 'unsigned' as untrusted, same convention as signing.py."""
    private_key = _load_private_key(agent_id, key_dir)
    if private_key is None:
        return UNSIGNED
    return private_key.sign(_canonical(payload)).hex()


def verify_agent(swarm_path: Path, agent_id: str, payload: dict, signature: str) -> bool:
    """Verify a signature against agent_id's REGISTERED public key (never
    trusts a key supplied by the caller — only the swarm's own registry).
    Returns False (not an exception) for the UNSIGNED sentinel or an unknown
    agent, so callers can treat "not verifiably signed" uniformly without a
    try/except at every call site."""
    if signature == UNSIGNED or not signature:
        return False
    identity = load_registered_agent(swarm_path, agent_id)
    if identity is None or not identity.public_key:
        return False
    _, Ed25519PublicKey, _, InvalidSignature = _ed25519()
    try:
        public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(identity.public_key))
        public_key.verify(bytes.fromhex(signature), _canonical(payload))
        return True
    except (InvalidSignature, ValueError):
        return False
