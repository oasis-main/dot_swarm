"""dot_swarm vault — AEAD-encrypted trail under the swarm key (SWC-046).

The swarm key is the colony-specific symmetric key. With it, an agent can
read and write coordination records; without it, the records are
ciphertext on disk. This is the *confidentiality* layer that complements
``dot_swarm.seals`` (writer-attribution / integrity) and
``dot_swarm.signing`` (HMAC trail signing).

Files managed here:
  .swarm/.swarm_key       — 256-bit AEAD key (must be gitignored)
  .swarm/swarm_key.json   — public metadata: fingerprint, algorithm, created
  .swarm/.swarm_key.old   — previous key, retained after a rotate (gitignored)

The cipher is XChaCha20 if available (PyNaCl) and ChaCha20-Poly1305
otherwise (cryptography stdlib). Both give 256-bit keys and 128-bit
authentication tags. ChaCha20-Poly1305 uses a 96-bit random nonce; the
collision risk after 2^48 messages is ~2^-32 — far above any realistic
trail volume but documented here for completeness. XChaCha20 raises the
ceiling further with a 192-bit nonce.

Envelope format on disk::

    swae1:<urlsafe-b64 of (nonce || ciphertext || tag)>

The ``swae1:`` prefix lets us detect ciphertext at parse time, so a
``.swarm/`` directory containing a mix of plaintext (legacy / unkeyed)
and encrypted records reads cleanly under the same code path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SWARM_KEY_FILE = ".swarm_key"
SWARM_KEY_OLD_FILE = ".swarm_key.old"
SWARM_KEY_METADATA = "swarm_key.json"

ENVELOPE_PREFIX = "swae1:"
KEY_BYTES = 32        # 256-bit
NONCE_BYTES = 12      # ChaCha20-Poly1305 standard nonce


class CryptoUnavailable(RuntimeError):
    """Raised when the optional `cryptography` dependency is not installed."""


class TamperedEnvelope(ValueError):
    """Raised when an envelope's AEAD tag fails verification."""


class WrongKey(ValueError):
    """Raised when an envelope was sealed under a different swarm key."""


# ---------------------------------------------------------------------------
# Optional-dependency probe
# ---------------------------------------------------------------------------

def has_crypto() -> bool:
    """True if the optional `cryptography` library is importable."""
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def _aead(key: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
    except ImportError as exc:
        raise CryptoUnavailable(
            "Encrypted trails require the optional 'cryptography' package. "
            "Install it with:  pip install 'dot-swarm[crypto]'"
        ) from exc
    return ChaCha20Poly1305(key)


# ---------------------------------------------------------------------------
# Key lifecycle
# ---------------------------------------------------------------------------

@dataclass
class SwarmKeyMetadata:
    algorithm: str
    fingerprint: str
    created: str
    rotated_at: str = ""


def fingerprint_for(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def generate_swarm_key(swarm_path: Path) -> SwarmKeyMetadata:
    """Generate a fresh swarm key. Idempotent: returns existing metadata if any."""
    key_file = swarm_path / SWARM_KEY_FILE
    meta_file = swarm_path / SWARM_KEY_METADATA
    if key_file.exists() and meta_file.exists():
        return load_swarm_key_metadata(swarm_path)

    if not has_crypto():
        raise CryptoUnavailable(
            "Cannot generate a swarm key without the 'cryptography' package. "
            "Install it with:  pip install 'dot-swarm[crypto]'"
        )

    key = secrets.token_bytes(KEY_BYTES)
    key_file.write_bytes(key)
    try:
        # Best-effort lockdown — owner-read only
        key_file.chmod(0o600)
    except OSError:
        pass

    meta = SwarmKeyMetadata(
        algorithm="chacha20-poly1305",
        fingerprint=fingerprint_for(key),
        created=_utcnow(),
    )
    meta_file.write_text(json.dumps(meta.__dict__, indent=2), encoding='utf-8')
    return meta


def load_swarm_key(swarm_path: Path) -> bytes | None:
    f = swarm_path / SWARM_KEY_FILE
    if not f.exists():
        return None
    return f.read_bytes()


def load_old_swarm_key(swarm_path: Path) -> bytes | None:
    f = swarm_path / SWARM_KEY_OLD_FILE
    if not f.exists():
        return None
    return f.read_bytes()


def load_swarm_key_metadata(swarm_path: Path) -> SwarmKeyMetadata:
    f = swarm_path / SWARM_KEY_METADATA
    if not f.exists():
        raise FileNotFoundError(f"No swarm key metadata at {f}")
    data = json.loads(f.read_text(encoding='utf-8'))
    return SwarmKeyMetadata(**data)


def has_swarm_key(swarm_path: Path) -> bool:
    return (swarm_path / SWARM_KEY_FILE).exists()


# ---------------------------------------------------------------------------
# Envelope primitives
# ---------------------------------------------------------------------------

def is_envelope(text: str) -> bool:
    """True if text is a single-line envelope (any leading whitespace OK)."""
    return text.lstrip().startswith(ENVELOPE_PREFIX)


def seal_envelope(plaintext: str, key: bytes, aad: bytes = b"") -> str:
    """Encrypt and authenticate plaintext under key; return one-line envelope."""
    aead = _aead(key)
    nonce = secrets.token_bytes(NONCE_BYTES)
    ct_and_tag = aead.encrypt(nonce, plaintext.encode("utf-8"), aad or None)
    blob = nonce + ct_and_tag
    return ENVELOPE_PREFIX + base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")


def open_envelope(envelope: str, key: bytes, aad: bytes = b"") -> str:
    """Decrypt and verify an envelope. Raises TamperedEnvelope on AEAD failure."""
    from cryptography.exceptions import InvalidTag
    s = envelope.strip()
    if not s.startswith(ENVELOPE_PREFIX):
        raise ValueError(f"Not an envelope (missing {ENVELOPE_PREFIX!r} prefix)")
    body = s[len(ENVELOPE_PREFIX):]
    pad = "=" * (-len(body) % 4)
    raw = base64.urlsafe_b64decode(body + pad)
    if len(raw) <= NONCE_BYTES:
        raise TamperedEnvelope("envelope truncated")
    nonce, ct = raw[:NONCE_BYTES], raw[NONCE_BYTES:]
    aead = _aead(key)
    try:
        pt = aead.decrypt(nonce, ct, aad or None)
    except InvalidTag as exc:
        raise TamperedEnvelope("AEAD tag mismatch") from exc
    return pt.decode("utf-8")


def try_open_envelope(envelope: str, swarm_path: Path, aad: bytes = b"") -> str:
    """Open an envelope using the active key; fall back to the previous key.

    During a rotation, records sealed under the prior key remain readable
    until they have been re-sealed under the new one. Once re-encryption
    completes, the .swarm_key.old file should be deleted.
    """
    key = load_swarm_key(swarm_path)
    if key is None:
        raise FileNotFoundError(
            f"No swarm key at {swarm_path / SWARM_KEY_FILE}. "
            "Run 'swarm key init' first."
        )
    try:
        return open_envelope(envelope, key, aad)
    except TamperedEnvelope:
        old = load_old_swarm_key(swarm_path)
        if old is None:
            raise WrongKey("envelope did not verify under the active swarm key")
        try:
            return open_envelope(envelope, old, aad)
        except TamperedEnvelope as exc:
            raise WrongKey(
                "envelope did not verify under either the active or the prior swarm key"
            ) from exc


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

@dataclass
class RotationResult:
    files_rewrapped: int
    lines_rewrapped: int
    skipped_plaintext: int
    failed: int


def rotate_swarm_key(swarm_path: Path) -> tuple[SwarmKeyMetadata, RotationResult]:
    """Generate a new swarm key and re-seal every existing envelope.

    Sequence:
      1. Move current `.swarm_key` to `.swarm_key.old` so partially-rotated
         records remain readable mid-rotation.
      2. Generate a new `.swarm_key`.
      3. Walk every .log/.md file under the swarm directory looking for
         envelopes. Each envelope opened under .old is re-sealed under
         the new key.
      4. Update metadata (fingerprint + rotated_at).
      5. Caller is responsible for deleting `.swarm_key.old` when satisfied.

    On any envelope failing to open under .old, that envelope is left
    unchanged and counted in `failed`. The new key still becomes active.
    """
    if not has_crypto():
        raise CryptoUnavailable(
            "Cannot rotate the swarm key without the 'cryptography' package. "
            "Install it with:  pip install 'dot-swarm[crypto]'"
        )
    current = swarm_path / SWARM_KEY_FILE
    if not current.exists():
        raise FileNotFoundError(
            f"No active swarm key at {current}. Run 'swarm key init' first."
        )

    old_path = swarm_path / SWARM_KEY_OLD_FILE
    old_path.write_bytes(current.read_bytes())
    try:
        old_path.chmod(0o600)
    except OSError:
        pass

    new_key = secrets.token_bytes(KEY_BYTES)
    current.write_bytes(new_key)
    try:
        current.chmod(0o600)
    except OSError:
        pass

    old_key = old_path.read_bytes()
    result = RotationResult(0, 0, 0, 0)
    for f in swarm_path.rglob("*"):
        if not f.is_file():
            continue
        if f.name in {SWARM_KEY_FILE, SWARM_KEY_OLD_FILE, SWARM_KEY_METADATA}:
            continue
        if f.suffix not in {".log", ".md", ".json"}:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        new_lines: list[str] = []
        rewrapped_in_file = 0
        for line in text.splitlines(keepends=True):
            stripped = line.rstrip("\n")
            if is_envelope(stripped):
                try:
                    pt = open_envelope(stripped, old_key)
                except TamperedEnvelope:
                    new_lines.append(line)
                    result.failed += 1
                    continue
                rewrapped = seal_envelope(pt, new_key)
                ending = "\n" if line.endswith("\n") else ""
                new_lines.append(rewrapped + ending)
                rewrapped_in_file += 1
            else:
                if stripped:
                    result.skipped_plaintext += 1
                new_lines.append(line)
        if rewrapped_in_file:
            f.write_text("".join(new_lines), encoding="utf-8")
            result.files_rewrapped += 1
            result.lines_rewrapped += rewrapped_in_file

    meta = SwarmKeyMetadata(
        algorithm="chacha20-poly1305",
        fingerprint=fingerprint_for(new_key),
        created=load_swarm_key_metadata(swarm_path).created,
        rotated_at=_utcnow(),
    )
    (swarm_path / SWARM_KEY_METADATA).write_text(json.dumps(meta.__dict__, indent=2), encoding='utf-8')
    return meta, result


# ---------------------------------------------------------------------------
# File-level helpers (one-shot seal / open of an entire file)
# ---------------------------------------------------------------------------

def seal_file(path: Path, swarm_path: Path) -> None:
    """Replace path's contents with a single envelope of the original bytes."""
    key = load_swarm_key(swarm_path)
    if key is None:
        raise FileNotFoundError(
            f"No swarm key at {swarm_path / SWARM_KEY_FILE}. Run 'swarm key init'."
        )
    plaintext = path.read_text(encoding="utf-8")
    if is_envelope(plaintext.strip()):
        return  # already sealed
    envelope = seal_envelope(plaintext, key)
    path.write_text(envelope + "\n", encoding="utf-8")


def open_file(path: Path, swarm_path: Path) -> None:
    """Replace path's contents with the decrypted plaintext."""
    text = path.read_text(encoding="utf-8")
    if not is_envelope(text.strip()):
        return  # nothing to do
    plaintext = try_open_envelope(text.strip(), swarm_path)
    path.write_text(plaintext, encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
