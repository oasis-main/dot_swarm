"""dot_swarm data models.

All models are plain dataclasses — no ORM, no database. State lives on disk as markdown.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


def utcnow() -> datetime:
    """Naive UTC datetime — replacement for the deprecated stdlib `datetime.utcnow()`.

    Returns a naive datetime (tzinfo stripped) so it stays comparable with the
    naive datetimes parsed back from `state.md` / `claims/` markdown timestamps.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ItemState(str, Enum):
    OPEN = "OPEN"
    CLAIMED = "CLAIMED"
    PARTIAL = "PARTIAL"
    COMPETING = "COMPETING"
    REVIEW = "REVIEW"
    BLOCKED = "BLOCKED"
    DONE = "DONE"
    CANCELLED = "CANCELLED"


class Priority(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


PRIORITY_ORDER = {Priority.CRITICAL: 0, Priority.HIGH: 1, Priority.MEDIUM: 2, Priority.LOW: 3}


@dataclass
class WorkItem:
    """A single entry in a queue.md file."""

    id: str                              # e.g. "ORG-002", "CLD-042"
    state: ItemState = ItemState.OPEN
    description: str = ""
    priority: Priority = Priority.MEDIUM
    project: str = "misc"
    notes: str = ""
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    done_at: datetime | None = None
    refs: list[str] = field(default_factory=list)
    depends: list[str] = field(default_factory=list)
    supersedes: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    proof: str = ""              # worker-supplied evidence for Inspector verification
    inspect_fails: int = 0      # times Inspector has rejected this item
    max_retries: int = 0        # 0 = use role default; >0 overrides at task level
    # Transient (resolution-time only — not persisted in queue.md):
    competitors: list["Claim"] = field(default_factory=list)

    # Regex patterns for parsing queue.md lines
    # Matches uppercase-prefix IDs like ORG-002 or hash IDs like sw-a1b2.
    ITEM_RE = re.compile(
        r"^- \[(?P<checkbox>.)\] "
        r"\[(?P<id>[A-Za-z][A-Za-z0-9]*-[A-Za-z0-9]+)\] "
        r"\[(?P<stamp>[^\]]+)\] "
        r"(?P<description>.+)$"
    )
    CLAIM_STAMP_RE = re.compile(
        r"(?P<state>CLAIMED|COMPETING|REVIEW) · (?P<agent>[^ ·]+) · (?P<ts>[0-9T:Z-]+)"
        r"(?: · (?P<modifier>PARTIAL))?"
    )
    DONE_STAMP_RE = re.compile(r"DONE · (?P<ts>[0-9T:Z-]+)")
    BLOCKED_STAMP_RE = re.compile(r"BLOCKED · (?P<reason>.+)")

    FIELD_RE = re.compile(
        r"^\s+(?P<key>priority|project|notes|depends|supersedes|duplicates|refs|proof|inspect_fails|max_retries): (?P<value>.+)$"
    )

    @classmethod
    def parse_line(cls, line: str) -> "WorkItem | None":
        """Parse a single queue.md item line into a WorkItem. Returns None if not an item."""
        m = cls.ITEM_RE.match(line.rstrip())
        if not m:
            return None

        item = cls(id=m.group("id"), description=m.group("description").strip())
        stamp = m.group("stamp").strip()

        if stamp == "OPEN":
            item.state = ItemState.OPEN
        elif cm := cls.CLAIM_STAMP_RE.match(stamp):
            item.claimed_by = cm.group("agent")
            item.claimed_at = _parse_ts(cm.group("ts"))
            st = cm.group("state")
            if cm.group("modifier") == "PARTIAL":
                item.state = ItemState.PARTIAL
            else:
                item.state = ItemState(st)
        elif dm := cls.DONE_STAMP_RE.match(stamp):
            item.done_at = _parse_ts(dm.group("ts"))
            item.state = ItemState.DONE
        elif bm := cls.BLOCKED_STAMP_RE.match(stamp):
            item.state = ItemState.BLOCKED
            item.notes = f"BLOCKED: {bm.group('reason')}"
        elif stamp == "CANCELLED":
            item.state = ItemState.CANCELLED

        return item

    def to_line(self) -> str:
        """Render item back to queue.md line format."""
        checkbox = {"OPEN": " ", "CLAIMED": ">", "PARTIAL": ">",
                    "COMPETING": "?", "REVIEW": "?",
                    "BLOCKED": " ", "DONE": "x", "CANCELLED": "x"}[self.state.value]
        stamp = self._render_stamp()
        line = f"- [{checkbox}] [{self.id}] [{stamp}] {self.description}"
        fields = []
        fields.append(f"priority: {self.priority.value} | project: {self.project}")
        if self.notes:
            fields.append(f"notes: {self.notes}")
        if self.depends:
            fields.append(f"depends: {', '.join(self.depends)}")
        if self.supersedes:
            fields.append(f"supersedes: {', '.join(self.supersedes)}")
        if self.duplicates:
            fields.append(f"duplicates: {', '.join(self.duplicates)}")
        if self.refs:
            fields.append(f"refs: {', '.join(self.refs)}")
        if self.proof:
            fields.append(f"proof: {self.proof}")
        if self.inspect_fails:
            fields.append(f"inspect_fails: {self.inspect_fails}")
        if self.max_retries:
            fields.append(f"max_retries: {self.max_retries}")
        if fields:
            line += "\n      " + "\n      ".join(fields)
        return line

    def _render_stamp(self) -> str:
        now = _now_ts()
        if self.state == ItemState.OPEN:
            return "OPEN"
        elif self.state == ItemState.CLAIMED:
            return f"CLAIMED · {self.claimed_by} · {_fmt_ts(self.claimed_at)}"
        elif self.state == ItemState.PARTIAL:
            return f"CLAIMED · {self.claimed_by} · {_fmt_ts(self.claimed_at)} · PARTIAL"
        elif self.state == ItemState.COMPETING:
            return f"COMPETING · {self.claimed_by} · {_fmt_ts(self.claimed_at)}"
        elif self.state == ItemState.REVIEW:
            return f"REVIEW · {self.claimed_by} · {_fmt_ts(self.claimed_at)}"
        elif self.state == ItemState.BLOCKED:
            reason = self.notes.removeprefix("BLOCKED: ") if self.notes else "unknown"
            return f"BLOCKED · {reason}"
        elif self.state == ItemState.DONE:
            return f"DONE · {_fmt_ts(self.done_at or utcnow())}"
        elif self.state == ItemState.CANCELLED:
            return "CANCELLED"
        return "OPEN"


@dataclass
class SwarmState:
    """Parsed representation of state.md."""

    last_touched: datetime | None = None
    last_agent: str = "unknown"
    current_focus: str = ""
    active_items: list[str] = field(default_factory=list)
    blockers: str = "None"
    ready_for_pickup: list[str] = field(default_factory=list)
    handoff_note: str = ""

    STATE_FIELDS = {
        "Last touched": "last_touched_raw",
        "Current focus": "current_focus",
        "Active items": "active_items_raw",
        "Blockers": "blockers",
        "Ready for pickup": "ready_for_pickup_raw",
    }


@dataclass
class Claim:
    """A claim file in .swarm/claims/."""
    item_id: str
    agent_id: str
    state: ItemState
    timestamp: datetime
    proof: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, d: dict) -> "Claim":
        return cls(
            item_id=d["item_id"],
            agent_id=d["agent_id"],
            state=ItemState(d["state"]),
            timestamp=datetime.fromisoformat(d["timestamp"].rstrip("Z")),
            proof=d.get("proof", ""),
            note=d.get("note", ""),
        )

    def to_dict(self) -> dict:
        return {
            "item_id": self.item_id,
            "agent_id": self.agent_id,
            "state": self.state.value,
            "timestamp": self.timestamp.isoformat() + "Z",
            "proof": self.proof,
            "note": self.note,
        }


@dataclass
class SwarmPaths:
    """Resolved paths for a .swarm/ directory."""

    root: Path           # the .swarm/ directory
    bootstrap: Path
    context: Path
    state: Path
    queue: Path
    memory: Path
    workflows: Path
    claims: Path

    @classmethod
    def from_swarm_dir(cls, swarm: Path) -> "SwarmPaths":
        """Construct SwarmPaths directly from a .swarm/ directory path."""
        return cls(
            root=swarm,
            bootstrap=swarm / "BOOTSTRAP.md",
            context=swarm / "context.md",
            state=swarm / "state.md",
            queue=swarm / "queue.md",
            memory=swarm / "memory.md",
            workflows=swarm / "workflows",
            claims=swarm / "claims",
        )

    @classmethod
    def find(cls, start: Path | str = ".") -> "SwarmPaths | None":
        """Walk up from start until .swarm/ is found (max 5 levels)."""
        p = Path(start).resolve()
        for _ in range(5):
            swarm = p / ".swarm"
            if swarm.is_dir():
                return cls(
                    root=swarm,
                    bootstrap=swarm / "BOOTSTRAP.md",
                    context=swarm / "context.md",
                    state=swarm / "state.md",
                    queue=swarm / "queue.md",
                    memory=swarm / "memory.md",
                    workflows=swarm / "workflows",
                    claims=swarm / "claims",
                )
            parent = p.parent
            if parent == p:
                break
            p = parent
        return None

    def is_org_level(self) -> bool:
        """True if this .swarm/ is at org level (no .git/ in parent)."""
        return not (self.root.parent / ".git").exists()


# --- Helpers ---

def _parse_ts(s: str) -> datetime | None:
    try:
        return datetime.strptime(s.rstrip("Z"), "%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def _fmt_ts(dt: datetime | None) -> str:
    if dt is None:
        return _now_ts()
    return dt.strftime("%Y-%m-%dT%H:%MZ")


def _now_ts() -> str:
    return utcnow().strftime("%Y-%m-%dT%H:%MZ")
