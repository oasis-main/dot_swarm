"""dot_swarm agent mailbox — direct agent-to-agent messaging (SWC-052).

Distinct from ``federation.py``: federation is CROSS-swarm (different repos/
divisions, HMAC-signed, filesystem-copy transport via git push/pull). This
is WITHIN one swarm — agents sharing the same ``.swarm/`` (typically a
shared bind-mounted volume, per the fleet's "shared narrow mailbox volume"
transport decision) sending each other Ed25519-signed (SWC-048) messages.

This is the mechanism that makes a "delegate to a peer" design (e.g. an
agent with no direct internet egress asking a peer to fetch a URL on its
behalf) an actual capability instead of persona prose.

Directory layout (inside .swarm/mailbox/, deliberately NOT git-tracked —
see .gitignore's SWC-052 note: this is ephemeral, consumed-then-gone
operational traffic, unlike SWC-051's durable comments/):

    mailbox/<agent_id>/inbox/<ts>_<msg_id>.json   — unread
    mailbox/<agent_id>/read/<ts>_<msg_id>.json    — after `swarm mail read`

Never trust the ``from`` field at face value: verify_message() checks the
signature against the SENDER's own registered public key (identity.py's
registry), exactly like comments.py and identity.py's own verify_agent —
a message can claim to be from any agent, but only a real holder of that
agent's private key can produce a signature that verifies as them.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from dataclasses import dataclass
from pathlib import Path

from .models import SwarmPaths, utcnow
from . import identity as _identity

MAILBOX_DIR = "mailbox"
INBOX = "inbox"
READ = "read"

# Monotonic per-process tiebreaker for filename ordering (see send_message).
# datetime.now()'s resolution isn't reliable enough on its own: some
# platforms' clocks (observed on Windows CI) tick coarser than the gap
# between two back-to-back send_message() calls, so two sends can land on
# the identical microsecond and then sort by msg_id (a content hash) —
# effectively random — instead of send order.
_SEQ = itertools.count()


@dataclass
class Message:
    from_agent: str
    to_agent: str
    subject: str
    body: str
    timestamp: str                  # ISO8601 with trailing Z
    msg_id: str = ""
    in_reply_to: str = ""
    signature: str = _identity.UNSIGNED

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "from": self.from_agent,
            "to": self.to_agent,
            "subject": self.subject,
            "body": self.body,
            "timestamp": self.timestamp,
            "in_reply_to": self.in_reply_to,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        return cls(
            from_agent=d["from"],
            to_agent=d["to"],
            subject=d.get("subject", ""),
            body=d["body"],
            timestamp=d["timestamp"],
            msg_id=d.get("msg_id", ""),
            in_reply_to=d.get("in_reply_to", ""),
            signature=d.get("signature", _identity.UNSIGNED),
        )

    def _signable_payload(self) -> dict:
        """The exact fields the signature covers — a stable, explicit
        subset (not to_dict() minus one key) so a future field added to
        to_dict() can't silently change what old signatures were computed
        over."""
        return {
            "msg_id": self.msg_id,
            "from": self.from_agent,
            "to": self.to_agent,
            "subject": self.subject,
            "body": self.body,
            "timestamp": self.timestamp,
            "in_reply_to": self.in_reply_to,
        }


def _mailbox_root(paths: SwarmPaths) -> Path:
    return paths.root / MAILBOX_DIR


def _inbox_dir(paths: SwarmPaths, agent_id: str) -> Path:
    return _mailbox_root(paths) / agent_id / INBOX


def _read_dir(paths: SwarmPaths, agent_id: str) -> Path:
    return _mailbox_root(paths) / agent_id / READ


def _msg_id(from_agent: str, to_agent: str, ts: str, subject: str, body: str) -> str:
    """Content-addressed, matching comments.py's comment_id — two agents
    (or a retry) sending the "same" message in the same instant never race
    for an ID the way a counter would."""
    digest = hashlib.sha256(
        f"{from_agent}|{to_agent}|{ts}|{subject}|{body}".encode("utf-8")
    ).hexdigest()
    return digest[:12]


def send_message(
    paths: SwarmPaths,
    from_agent: str,
    to_agent: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
) -> Message:
    """Deliver a message into to_agent's inbox, signed with from_agent's
    Ed25519 key when one is available locally.

    Delivery doesn't require to_agent to exist or be registered — mailbox
    is push-only; an unread message sitting in a mailbox nobody polls is
    harmless, and validating the recipient here would just be a TOCTOU-
    prone check against something the receiver already verifies for
    itself on read.
    """
    now_dt = utcnow()
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    msg_id = _msg_id(from_agent, to_agent, now, subject, body)
    message = Message(
        from_agent=from_agent, to_agent=to_agent, subject=subject, body=body,
        timestamp=now, msg_id=msg_id, in_reply_to=in_reply_to,
    )
    if _identity.has_crypto():
        message.signature = _identity.sign_agent(from_agent, message._signable_payload())

    inbox = _inbox_dir(paths, to_agent)
    inbox.mkdir(parents=True, exist_ok=True)
    # Sort key for the filename — NOT the same string as the (second-
    # precision) `timestamp` field above. list_inbox() relies on lexical
    # filename order for chronological listing. The microsecond timestamp
    # alone isn't sufficient: some platforms' wall clocks (observed on
    # Windows CI) tick coarser than the gap between two back-to-back
    # send_message() calls, so a zero-padded, strictly-monotonic per-process
    # counter is the real ordering guarantee; the timestamp just keeps
    # filenames human-sortable-by-rough-time across process restarts.
    sort_key = f"{now_dt.strftime('%Y%m%dT%H%M%S%f')}_{next(_SEQ):09d}"
    dest = inbox / f"{sort_key}_{msg_id}.json"
    suffix = 0
    while dest.exists():  # collision under a same-tick duplicate send
        suffix += 1
        dest = inbox / f"{sort_key}_{msg_id}_{suffix}.json"
    dest.write_text(json.dumps(message.to_dict(), indent=2), encoding="utf-8")
    return message


def _load_dir(d: Path) -> list[tuple[Path, Message]]:
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.json")):
        try:
            out.append((p, Message.from_dict(json.loads(p.read_text(encoding="utf-8")))))
        except (json.JSONDecodeError, KeyError, OSError):
            continue
    return out


def list_inbox(paths: SwarmPaths, agent_id: str, include_read: bool = False) -> list[Message]:
    """List agent_id's messages, oldest first. Unread only by default.

    Ordering comes from the microsecond-precision filename (see
    send_message), not the second-precision `timestamp` field — two
    messages landing in the same second must still sort by actual send
    order, not fall back to comparing msg_id.
    """
    entries = _load_dir(_inbox_dir(paths, agent_id))
    if include_read:
        entries += _load_dir(_read_dir(paths, agent_id))
        entries.sort(key=lambda pair: pair[0].name)
    return [m for _, m in entries]


def read_message(paths: SwarmPaths, agent_id: str, msg_id: str) -> Message | None:
    """Read one message by msg_id and mark it read (move inbox -> read/).
    Returns None if no matching unread message exists — re-reading an
    already-read message returns None too; list_inbox(include_read=True)
    is how to look back at read mail."""
    for path, message in _load_dir(_inbox_dir(paths, agent_id)):
        if message.msg_id == msg_id:
            read_dir = _read_dir(paths, agent_id)
            read_dir.mkdir(parents=True, exist_ok=True)
            path.rename(read_dir / path.name)
            return message
    return None


def verify_message(paths: SwarmPaths, message: Message) -> bool:
    """True iff message.signature verifies against from_agent's REGISTERED
    public key. False — never an exception — for unsigned or forged
    messages, so a caller can treat 'not verifiably from who it claims'
    uniformly before acting on a delegated request."""
    return _identity.verify_agent(
        paths.root, message.from_agent, message._signable_payload(), message.signature
    )
