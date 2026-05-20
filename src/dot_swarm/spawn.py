"""dot_swarm agent spawning via tmux.

Spawns a named tmux window for a worker or inspector agent, optionally
auto-claiming the target item. Requires tmux >= 3.0 and the chosen agent
CLI (opencode, claude, ollama, gemini) to be installed.

No daemon is started — tmux is used purely as a process container.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .models import SwarmPaths

SUPPORTED_AGENTS = ("opencode", "claude", "ollama", "gemini", "custom")
TMUX_SESSION = "swarm"

# Files/dirs to skip when summarising a directory for agent context
_SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".tox", ".eggs",
}


# ---------------------------------------------------------------------------
# Dependency checks
# ---------------------------------------------------------------------------

def check_tmux() -> tuple[bool, str]:
    """Return (available, version_string)."""
    if not shutil.which("tmux"):
        return False, ""
    result = subprocess.run(["tmux", "-V"], capture_output=True, text=True)
    return True, result.stdout.strip()


def check_agent(agent: str) -> bool:
    if agent == "custom":
        return True
    return bool(shutil.which(agent))


# ---------------------------------------------------------------------------
# tmux helpers
# ---------------------------------------------------------------------------

def _session_exists(session: str) -> bool:
    r = subprocess.run(["tmux", "has-session", "-t", session], capture_output=True)
    return r.returncode == 0


def _ensure_session(session: str) -> None:
    if not _session_exists(session):
        subprocess.run(["tmux", "new-session", "-d", "-s", session], check=True)


def _window_exists(session: str, window: str) -> bool:
    r = subprocess.run(
        ["tmux", "list-windows", "-t", session, "-F", "#{window_name}"],
        capture_output=True, text=True,
    )
    return window in r.stdout.splitlines()


def _send(session: str, window: str, cmd: str) -> None:
    subprocess.run(
        ["tmux", "send-keys", "-t", f"{session}:{window}", cmd, "Enter"],
        check=True,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def spawn_agent(
    paths: SwarmPaths,
    item_id: str,
    *,
    agent: str = "opencode",
    role: str | None = None,
    session: str = TMUX_SESSION,
    window_name: str | None = None,
    auto_claim: bool = True,
    custom_cmd: str | None = None,
) -> dict:
    """Spawn a tmux window for an agent working on *item_id*.

    Returns a result dict with keys: window, agent, item_id, role, success, error.
    """
    ok, tmux_ver = check_tmux()
    if not ok:
        raise RuntimeError(
            "tmux is required for swarm spawn.\n"
            "  macOS:  brew install tmux\n"
            "  Linux:  apt install tmux  or  dnf install tmux"
        )

    if agent not in SUPPORTED_AGENTS:
        raise ValueError(
            f"Unknown agent '{agent}'. Supported: {', '.join(SUPPORTED_AGENTS)}"
        )
    if agent != "custom" and not check_agent(agent):
        raise RuntimeError(
            f"'{agent}' is not installed or not in PATH.\n"
            f"  opencode: npm install -g opencode-ai\n"
            f"  claude:   npm install -g @anthropic-ai/claude-code"
        )

    win = window_name or f"{item_id}"
    repo_root = str(paths.root.parent.resolve())

    _ensure_session(session)

    if _window_exists(session, win):
        return {
            "window": win, "agent": agent, "item_id": item_id, "role": role,
            "success": False, "error": f"Window '{win}' already exists in session '{session}'.",
        }

    # Create the window (stays in background)
    subprocess.run(
        ["tmux", "new-window", "-d", "-n", win, "-t", session],
        check=True,
    )

    agent_id = f"{item_id}-{agent}" + (f"-{role}" if role else "")

    # Set environment
    env_exports = " ".join([
        f"export SWARM_AGENT_ID={agent_id}",
        f"&& export SWARM_PATH={repo_root}",
        f"&& export SWARM_ITEM={item_id}",
    ])
    if role:
        env_exports += f" && export SWARM_ROLE={role}"

    _send(session, win, f"cd {repo_root} && {env_exports}")

    # Auto-claim the item before handing off to the agent
    if auto_claim:
        _send(session, win, f"swarm claim {item_id} --agent {agent_id}")

    # Build the bootstrap prompt injected as the agent's first message
    bootstrap = _build_bootstrap_prompt(item_id, role, repo_root)

    # Launch the agent
    if agent == "custom" and custom_cmd:
        _send(session, win, custom_cmd)
    elif agent == "opencode":
        _send(session, win, f'opencode "{bootstrap}"')
    elif agent == "claude":
        _send(session, win, f'claude --print "{bootstrap}"')
    else:
        # ollama / gemini: just open interactive, bootstrap written to file
        ctx_file = paths.root / f"_spawn_{item_id}.md"
        ctx_file.write_text(bootstrap, encoding='utf-8')
        _send(session, win, f"{agent}  # context: {ctx_file}")

    return {
        "window": win,
        "agent": agent,
        "agent_id": agent_id,
        "item_id": item_id,
        "role": role,
        "session": session,
        "success": True,
        "error": None,
    }


def list_windows(session: str = TMUX_SESSION) -> list[dict]:
    """List all tmux windows in the swarm session."""
    if not _session_exists(session):
        return []
    result = subprocess.run(
        ["tmux", "list-windows", "-t", session,
         "-F", "#{window_index}:#{window_name}:#{window_active}:#{pane_pid}"],
        capture_output=True, text=True,
    )
    windows = []
    for line in result.stdout.splitlines():
        parts = line.split(":", 3)
        if len(parts) == 4:
            windows.append({
                "index": parts[0],
                "name": parts[1],
                "active": parts[2] == "1",
                "pid": parts[3],
            })
    return windows


def attach_window(window: str, session: str = TMUX_SESSION) -> None:
    """Bring a tmux window to the foreground (switch to it)."""
    subprocess.run(["tmux", "select-window", "-t", f"{session}:{window}"], check=True)
    subprocess.run(["tmux", "attach-session", "-t", session])


# ---------------------------------------------------------------------------
# Bootstrap prompt builder
# ---------------------------------------------------------------------------

def _build_bootstrap_prompt(item_id: str, role: str | None, repo_root: str) -> str:
    role_instruction = ""
    if role == "inspector":
        role_instruction = (
            f"Your role is INSPECTOR for item {item_id}. "
            "Review the worker's proof-of-work, then run: "
            f"swarm inspect {item_id} --pass  OR  "
            f"swarm inspect {item_id} --fail --reason \"<reason>\""
        )
    elif role == "supervisor":
        role_instruction = (
            "Your role is SUPERVISOR. Run: swarm explore && swarm report "
            "to build a holistic view, then brief the human director."
        )
    else:
        role_instruction = (
            f"Claim and implement item {item_id}. When done, attach proof: "
            f"swarm partial {item_id} --proof \"branch:<name> commit:<sha> tests:<N/N>\""
        )

    return (
        f"You are a dot_swarm agent in {repo_root}. "
        "First: read .swarm/BOOTSTRAP.md, then .swarm/state.md, then .swarm/queue.md. "
        f"{role_instruction} "
        "All swarm state lives in .swarm/ — no external services needed."
    )
