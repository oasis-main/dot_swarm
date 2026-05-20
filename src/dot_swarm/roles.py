"""dot_swarm agent role management.

Roles are opt-in capabilities that extend swarm coordination with structured
agent behaviors. Each role is stored as a JSON config in .swarm/roles/<name>.json.
Toggling a role on/off never touches queue.md or any work items.

Available roles
---------------
inspector   — requires proof-of-work before a worker's item can be marked done;
              re-opens items on failure; escalates to watchdog after max_iterations.
watchdog    — monitors for items stuck in inspector loops or items that need
              human domain knowledge; surfaces escalations to the human director.
supervisor  — maintains a holistic view of all active items and phases; generates
              structured progress briefs for a human director on demand.
librarian   — crawls a directory tree, populates context.md and queue.md with
              undocumented files/modules; flags conflicts to the watchdog.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .models import SwarmPaths

KNOWN_ROLES = {"inspector", "watchdog", "supervisor", "librarian"}


@dataclass
class RoleConfig:
    name: str
    enabled: bool = True
    # Inspector-specific
    max_iterations: int = 3          # reject count before watchdog escalation
    require_proof_fields: list[str] = field(default_factory=lambda: ["branch", "commit"])
    # Assignment
    assigned_agent: str | None = None
    # Extra per-role options
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _roles_dir(paths: SwarmPaths) -> Path:
    return paths.root / "roles"


def _role_file(paths: SwarmPaths, role_name: str) -> Path:
    return _roles_dir(paths) / f"{role_name}.json"


def load_role(paths: SwarmPaths, role_name: str) -> RoleConfig | None:
    """Return RoleConfig for *role_name*, or None if not configured."""
    cfg_file = _role_file(paths, role_name)
    if not cfg_file.exists():
        return None
    data = json.loads(cfg_file.read_text(encoding='utf-8'))
    return RoleConfig(
        name=role_name,
        enabled=data.get("enabled", True),
        max_iterations=data.get("max_iterations", 3),
        require_proof_fields=data.get("require_proof_fields", ["branch", "commit"]),
        assigned_agent=data.get("assigned_agent"),
        extra=data.get("extra", {}),
    )


def is_role_enabled(paths: SwarmPaths, role_name: str) -> bool:
    """Return True iff *role_name* is configured and enabled."""
    role = load_role(paths, role_name)
    return role is not None and role.enabled


def enable_role(
    paths: SwarmPaths,
    role_name: str,
    *,
    max_iterations: int = 3,
    require_proof_fields: list[str] | None = None,
    assigned_agent: str | None = None,
    extra: dict[str, Any] | None = None,
) -> RoleConfig:
    """Enable (or reconfigure) a role. Creates .swarm/roles/ if needed."""
    if role_name not in KNOWN_ROLES:
        raise ValueError(
            f"Unknown role '{role_name}'. Known roles: {', '.join(sorted(KNOWN_ROLES))}"
        )
    _roles_dir(paths).mkdir(exist_ok=True)
    cfg = RoleConfig(
        name=role_name,
        enabled=True,
        max_iterations=max_iterations,
        require_proof_fields=require_proof_fields or ["branch", "commit"],
        assigned_agent=assigned_agent,
        extra=extra or {},
    )
    payload = {
        "enabled": cfg.enabled,
        "max_iterations": cfg.max_iterations,
        "require_proof_fields": cfg.require_proof_fields,
        "assigned_agent": cfg.assigned_agent,
        "extra": cfg.extra,
    }
    _role_file(paths, role_name).write_text(json.dumps(payload, indent=2) + "\n", encoding='utf-8')
    return cfg


def disable_role(paths: SwarmPaths, role_name: str) -> None:
    """Remove the role config file (idempotent — no error if already absent)."""
    cfg_file = _role_file(paths, role_name)
    if cfg_file.exists():
        cfg_file.unlink()


def list_roles(paths: SwarmPaths) -> list[RoleConfig]:
    """Return configs for all roles that have a config file."""
    roles_dir = _roles_dir(paths)
    if not roles_dir.exists():
        return []
    result = []
    for f in sorted(roles_dir.glob("*.json")):
        role = load_role(paths, f.stem)
        if role is not None:
            result.append(role)
    return result


# ---------------------------------------------------------------------------
# Inspector helpers
# ---------------------------------------------------------------------------

def validate_proof(proof: str, required_fields: list[str]) -> list[str]:
    """Return list of missing required proof fields.

    Proof string format: ``key:value key2:value2 ...``
    Example: ``"branch:feature/oauth2 commit:abc1234 tests:42/42"``
    """
    if not proof:
        return list(required_fields)
    present = {part.split(":", 1)[0].strip() for part in proof.split() if ":" in part}
    return [f for f in required_fields if f not in present]


def check_escalation(inspect_fails: int, max_iterations: int) -> bool:
    """Return True if the failure count has reached the escalation threshold."""
    return inspect_fails >= max_iterations
