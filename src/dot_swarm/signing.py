"""dot_swarm pheromone trail signing.

Uses HMAC-SHA256 (stdlib hashlib + hmac) for per-swarm identity and
operation signing. Each .swarm/ directory holds a signing identity;
state-changing operations are recorded in trail.log with their HMAC
signatures so tampering, spoofing, or bad-actor injections can be
detected and their fingerprint blocked from the swarm.

Files managed here:
  .swarm/identity.json      — public fingerprint (safe to commit)
  .swarm/.signing_key       — private HMAC key  (add to .gitignore!)
  .swarm/trail.log          — append-only signed operation log
  .swarm/blocked_peers.json — fingerprints banned from this swarm
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

IDENTITY_FILE = "identity.json"
SIGNING_KEY_FILE = ".signing_key"
TRAIL_FILE = "trail.log"
BLOCKED_PEERS_FILE = "blocked_peers.json"


# ---------------------------------------------------------------------------
# Identity management
# ---------------------------------------------------------------------------

def generate_identity(swarm_path: Path) -> dict:
    """Generate a new HMAC signing identity for this .swarm/ directory.

    Creates identity.json (commit-safe fingerprint) and .signing_key
    (private key — must be gitignored). Idempotent if called twice:
    existing files are not overwritten.
    """
    key_file = swarm_path / SIGNING_KEY_FILE
    id_file = swarm_path / IDENTITY_FILE

    if id_file.exists() and key_file.exists():
        return json.loads(id_file.read_text(encoding='utf-8'))

    key_hex = secrets.token_hex(32)                                       # 256-bit
    fingerprint = hashlib.sha256(key_hex.encode()).hexdigest()[:16]
    swarm_id = secrets.token_hex(8)

    identity = {
        "id": swarm_id,
        "fingerprint": fingerprint,
        "algorithm": "hmac-sha256",
        "created": datetime.now(timezone.utc).isoformat(),
    }

    id_file.write_text(json.dumps(identity, indent=2), encoding='utf-8')
    key_file.write_text(key_hex, encoding='utf-8')
    return identity


def load_identity(swarm_path: Path) -> dict | None:
    """Load the public identity record for this swarm, or None if absent."""
    id_file = swarm_path / IDENTITY_FILE
    if not id_file.exists():
        return None
    try:
        return json.loads(id_file.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return None


def _load_key(swarm_path: Path) -> bytes | None:
    key_file = swarm_path / SIGNING_KEY_FILE
    if not key_file.exists():
        return None
    return key_file.read_text(encoding='utf-8').strip().encode()


# ---------------------------------------------------------------------------
# Signing / verification
# ---------------------------------------------------------------------------

def _sign(key: bytes, message: str) -> str:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()


def _verify(key: bytes, message: str, signature: str) -> bool:
    expected = hmac.new(key, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _body_for_record(record: dict) -> str:
    """Reconstruct the canonical body string that was signed."""
    return json.dumps(
        {
            "timestamp": record["timestamp"],
            "swarm_id": record["swarm_id"],
            "agent_id": record["agent_id"],
            "op": record["op"],
            "payload": record["payload"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )


# ---------------------------------------------------------------------------
# Trail operations
# ---------------------------------------------------------------------------

def sign_operation(
    swarm_path: Path,
    op_type: str,
    agent_id: str,
    payload: dict,
) -> dict:
    """Build a signed pheromone trail record for one operation.

    If no signing key exists the record is still written but marked
    'unsigned' so the trail remains useful even in unsigned mode.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    identity = load_identity(swarm_path)
    swarm_id = identity["id"] if identity else "anonymous"
    fingerprint = identity["fingerprint"] if identity else "unsigned"

    body = json.dumps(
        {
            "timestamp": ts,
            "swarm_id": swarm_id,
            "agent_id": agent_id,
            "op": op_type,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    key = _load_key(swarm_path)
    sig = _sign(key, body) if key else "unsigned"

    return {
        "timestamp": ts,
        "swarm_id": swarm_id,
        "fingerprint": fingerprint,
        "agent_id": agent_id,
        "op": op_type,
        "payload": payload,
        "signature": sig,
    }


def append_trail(swarm_path: Path, record: dict) -> None:
    """Append a signed record to trail.log (one entry per line).

    If a swarm key is present (``.swarm/.swarm_key``), the JSON line is
    sealed with ChaCha20-Poly1305 before being written — the file on
    disk becomes a sequence of ``swae1:<base64>`` envelopes opaque to
    anyone without the key. Without the key, behaviour is unchanged
    (plaintext JSON-per-line), and existing trails keep working.
    """
    from . import vault as _vault  # local import to keep crypto optional

    trail = swarm_path / TRAIL_FILE
    payload = json.dumps(record, separators=(",", ":"))
    key = _vault.load_swarm_key(swarm_path)
    line = (_vault.seal_envelope(payload, key) if key else payload) + "\n"
    with trail.open("a", encoding="utf-8") as fh:
        fh.write(line)


def read_trail(swarm_path: Path, limit: int = 100) -> list[dict]:
    """Read the last *limit* entries from trail.log.

    Transparently decrypts envelope-formatted lines using the swarm key
    (and the rotated-out previous key if present). Lines that fail
    decryption are skipped, matching the prior behaviour for malformed
    plaintext JSON.
    """
    from . import vault as _vault

    trail = swarm_path / TRAIL_FILE
    if not trail.exists():
        return []
    lines = trail.read_text(encoding="utf-8").splitlines()
    records: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        if _vault.is_envelope(line):
            try:
                line = _vault.try_open_envelope(line, swarm_path)
            except (FileNotFoundError, _vault.WrongKey, _vault.TamperedEnvelope):
                continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return records


def verify_trail(swarm_path: Path) -> list[dict]:
    """Re-verify every signed entry in trail.log.

    Returns a list of records whose signatures do not match — these
    represent potential tampering, replayed operations, or spoofed
    agent identities.
    """
    key = _load_key(swarm_path)
    if not key:
        return []

    tampered: list[dict] = []
    for record in read_trail(swarm_path, limit=100_000):
        sig = record.get("signature", "")
        if sig in ("unsigned", ""):
            continue
        body = _body_for_record(record)
        if not _verify(key, body, sig):
            tampered.append(record)
    return tampered


# ---------------------------------------------------------------------------
# Blocked peers
# ---------------------------------------------------------------------------

def load_blocked_peers(swarm_path: Path) -> list[str]:
    blocked_file = swarm_path / BLOCKED_PEERS_FILE
    if not blocked_file.exists():
        return []
    try:
        return json.loads(blocked_file.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return []


def block_peer(swarm_path: Path, fingerprint: str) -> None:
    """Permanently block a peer fingerprint from this swarm."""
    blocked = load_blocked_peers(swarm_path)
    if fingerprint not in blocked:
        blocked.append(fingerprint)
    (swarm_path / BLOCKED_PEERS_FILE).write_text(json.dumps(blocked, indent=2), encoding='utf-8')


def is_blocked(swarm_path: Path, fingerprint: str) -> bool:
    return fingerprint in load_blocked_peers(swarm_path)
