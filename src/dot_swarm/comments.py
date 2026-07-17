"""dot_swarm threaded comments (SWC-051).

No structured discussion mechanism existed on a work item before this —
coordination happened either in a `notes:` field (one shot, no thread) or
out-of-band entirely. This gives each item its own append-only, optionally
Ed25519-signed (SWC-048) comment thread.

Storage: ``.swarm/comments/<item-id>.jsonl`` — one JSON object per line,
mirroring the existing ``claims/`` append-only pattern (never rewritten in
place, appended to like ``trail.log``). A repo with no ``comments/``
directory simply has no comments; nothing here touches ``queue.md``,
``state.md``, or the ``claims/`` trail, so adopting this feature — or never
adopting it — is purely additive for every existing ``.swarm/``.

Signing follows identity.py's conventions exactly: an agent with no local
Ed25519 key gets the honest ``UNSIGNED`` sentinel rather than a fabricated
signature, and without the optional ``cryptography`` package installed at
all, comments are still written (unsigned) rather than failing outright —
matching how ``vault.py``/``identity.py`` treat crypto as optional.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from .models import SwarmPaths, utcnow
from . import identity as _identity

COMMENTS_DIR = "comments"


@dataclass
class Comment:
    item_id: str
    agent_id: str
    body: str
    timestamp: str                  # ISO8601 with trailing Z
    comment_id: str = ""
    in_reply_to: str = ""
    signature: str = _identity.UNSIGNED

    def to_dict(self) -> dict:
        return {
            "comment_id": self.comment_id,
            "item_id": self.item_id,
            "agent_id": self.agent_id,
            "body": self.body,
            "timestamp": self.timestamp,
            "in_reply_to": self.in_reply_to,
            "signature": self.signature,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Comment":
        return cls(
            item_id=d["item_id"],
            agent_id=d["agent_id"],
            body=d["body"],
            timestamp=d["timestamp"],
            comment_id=d.get("comment_id", ""),
            in_reply_to=d.get("in_reply_to", ""),
            signature=d.get("signature", _identity.UNSIGNED),
        )

    def _signable_payload(self) -> dict:
        """The exact fields covered by the signature — everything except the
        signature itself. A stable, explicit subset (not to_dict() minus one
        key) so a future field addition to to_dict() can't silently change
        what old signatures were computed over."""
        return {
            "comment_id": self.comment_id,
            "item_id": self.item_id,
            "agent_id": self.agent_id,
            "body": self.body,
            "timestamp": self.timestamp,
            "in_reply_to": self.in_reply_to,
        }


def _comments_path(paths: SwarmPaths, item_id: str) -> Path:
    return paths.root / COMMENTS_DIR / f"{item_id}.jsonl"


def _comment_id(item_id: str, agent_id: str, ts: str, body: str) -> str:
    """Content-addressed, not sequence-numbered — two agents commenting on
    the same item at the same instant never race for an ID the way a
    counter would (see SWC-050 for why that race matters here)."""
    digest = hashlib.sha256(f"{item_id}|{agent_id}|{ts}|{body}".encode("utf-8")).hexdigest()
    return digest[:12]


def add_comment(
    paths: SwarmPaths,
    item_id: str,
    agent_id: str,
    body: str,
    in_reply_to: str = "",
) -> Comment:
    """Append a comment to item_id's thread, signed with agent_id's Ed25519
    key when one is available locally. Returns the recorded Comment.

    A reply whose in_reply_to doesn't (yet) match any comment_id in the
    thread is still recorded rather than rejected — replies can arrive
    out of order (e.g. across a federation sync), and silently dropping
    one would lose an agent's contribution over an ordering quirk. The
    dangling reference is surfaced by the CLI, not enforced here.
    """
    now = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    comment_id = _comment_id(item_id, agent_id, now, body)
    comment = Comment(
        item_id=item_id, agent_id=agent_id, body=body, timestamp=now,
        comment_id=comment_id, in_reply_to=in_reply_to,
    )
    if _identity.has_crypto():
        comment.signature = _identity.sign_agent(agent_id, comment._signable_payload())

    path = _comments_path(paths, item_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(comment.to_dict(), separators=(",", ":"))
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    return comment


def read_comments(paths: SwarmPaths, item_id: str) -> list[Comment]:
    """Read item_id's thread in chronological (append) order. A malformed
    line is skipped, not fatal — one bad line must never take down the
    rest of the thread."""
    path = _comments_path(paths, item_id)
    if not path.exists():
        return []
    out: list[Comment] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(Comment.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def verify_comment(paths: SwarmPaths, comment: Comment) -> bool:
    """True iff comment.signature verifies against agent_id's REGISTERED
    public key (never a key supplied by the comment itself). False — not
    an exception — for an unsigned or unknown-agent comment, so callers
    can treat "not verifiably signed" uniformly."""
    return _identity.verify_agent(
        paths.root, comment.agent_id, comment._signable_payload(), comment.signature
    )
