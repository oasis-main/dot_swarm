"""Integration tests for the `swarm gui` HTTP surface.

The dashboard can mutate the queue, so these cover both that the write
routes work and that they refuse what they should: a missing token, a
foreign Origin, a division outside the colony, and --read-only mode.

Each test drives a real `swarm gui` process over real HTTP. That is the
only way to cover the handler, which is defined inside the click command.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_SRC = str(Path(__file__).parent.parent / "src")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _swarm(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", "from dot_swarm.cli import cli; cli(obj={})", *args],
        cwd=str(cwd), capture_output=True, text=True, env=_env(),
    )


def _env() -> dict:
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = REPO_SRC + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["SWARM_AGENT_ID"] = "test-agent"
    return env


def _get(url: str, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")


def _post(url: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8")
    hdrs = {"Content-Type": "application/json"}
    hdrs.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class Server:
    def __init__(self, root: Path, port: int, proc, token: str, base: str):
        self.root, self.port, self.proc = root, port, proc
        self.token, self.base = token, base

    def auth(self, extra: dict | None = None) -> dict:
        h = {"X-Swarm-Token": self.token, "Origin": self.base}
        h.update(extra or {})
        return h

    def state(self) -> dict:
        return json.loads(_get(self.base + "/api/state.json")[1])

    def division(self, name: str) -> dict:
        return next(d for d in self.state()["divisions"] if d["name"] == name)


@pytest.fixture()
def colony(tmp_path: Path) -> Path:
    """Org root + one sub-division, each with a real .swarm/."""
    (tmp_path / "oasis-cloud").mkdir()
    assert _swarm(["init"], tmp_path).returncode == 0
    assert _swarm(["init"], tmp_path / "oasis-cloud").returncode == 0
    _swarm(["--path", str(tmp_path / "oasis-cloud"), "add", "seed item", "--priority", "high"], tmp_path)
    return tmp_path


def _start(colony: Path, *extra: str) -> Server:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-c", "from dot_swarm.cli import cli; cli(obj={})",
         "--path", str(colony), "gui", "--port", str(port), *extra],
        cwd=str(colony), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=_env(),
    )
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail("gui server exited early:\n" + (proc.stdout.read() if proc.stdout else ""))
        try:
            status, html = _get(base + "/")
            if status == 200:
                m = re.search(r'const WRITE_TOKEN = "([^"]*)"', html)
                assert m, "WRITE_TOKEN placeholder was not substituted"
                return Server(colony, port, proc, m.group(1), base)
        except Exception:
            time.sleep(0.1)
    proc.kill()
    pytest.fail("gui server did not come up in time")


@pytest.fixture()
def server(colony: Path):
    s = _start(colony)
    yield s
    s.proc.kill()
    s.proc.wait(timeout=10)


@pytest.fixture()
def readonly_server(colony: Path):
    s = _start(colony, "--read-only")
    yield s
    s.proc.kill()
    s.proc.wait(timeout=10)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def test_index_serves_the_page_with_a_token(server: Server) -> None:
    status, html = _get(server.base + "/")
    assert status == 200
    assert "dot_swarm Dashboard" in html
    assert "__SWARM_WRITE_TOKEN__" not in html
    assert len(server.token) > 20


def test_state_endpoint_returns_json(server: Server) -> None:
    data = server.state()
    names = {d["name"] for d in data["divisions"]}
    assert "oasis-cloud" in names
    assert not [d for d in data["divisions"] if "error" in d]


def test_unknown_paths_do_not_serve_local_files(server: Server) -> None:
    """The handler must not fall through to serving the working directory."""
    for path in ("/.swarm/queue.md", "/../../etc/passwd", "/oasis-cloud/.swarm/state.md"):
        status, _ = _get(server.base + path)
        assert status == 404, f"{path} was served (status {status})"


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def test_add_claim_done_round_trip(server: Server) -> None:
    div = server.division("oasis-cloud")

    status, out = _post(server.base + "/api/item/add",
                        {"division_path": div["path"], "description": "added from the dashboard",
                         "priority": "critical"},
                        server.auth())
    assert status == 200 and out["ok"], out
    item_id = out["item_id"]

    pending = server.division("oasis-cloud")["queue"]["pending"]
    added = next(i for i in pending if i["id"] == item_id)
    assert added["description"] == "added from the dashboard"
    assert added["priority"] == "critical"

    status, out = _post(server.base + "/api/item/claim",
                        {"division_path": div["path"], "item_id": item_id}, server.auth())
    assert status == 200 and out["ok"], out
    active = server.division("oasis-cloud")["queue"]["active"]
    claimed = next(i for i in active if i["id"] == item_id)
    assert claimed["state"] == "CLAIMED"
    assert claimed["claimed_by"] == "test-agent"

    status, out = _post(server.base + "/api/item/done",
                        {"division_path": div["path"], "item_id": item_id}, server.auth())
    assert status == 200 and out["ok"], out
    done = server.division("oasis-cloud")["queue"]["done"]
    assert any(i["id"] == item_id and i["state"] == "DONE" for i in done)


def test_write_lands_in_the_claim_trail(server: Server) -> None:
    """The dashboard must go through the same claim trail as the CLI."""
    div = server.division("oasis-cloud")
    item_id = _post(server.base + "/api/item/add",
                    {"division_path": div["path"], "description": "trail check"},
                    server.auth())[1]["item_id"]
    _post(server.base + "/api/item/claim",
          {"division_path": div["path"], "item_id": item_id}, server.auth())

    claims = list((server.root / "oasis-cloud" / ".swarm" / "claims").glob("*.json"))
    records = [json.loads(c.read_text(encoding="utf-8")) for c in claims]
    mine = [r for r in records if r["item_id"] == item_id]
    assert mine, "no claim record written"
    assert mine[0]["agent_id"] == "test-agent"


def test_block_requires_a_reason(server: Server) -> None:
    div = server.division("oasis-cloud")
    item_id = div["queue"]["pending"][0]["id"]

    status, out = _post(server.base + "/api/item/block",
                        {"division_path": div["path"], "item_id": item_id}, server.auth())
    assert status == 400 and not out["ok"]
    assert "reason" in out["error"]

    status, out = _post(server.base + "/api/item/block",
                        {"division_path": div["path"], "item_id": item_id, "reason": "waiting on infra"},
                        server.auth())
    assert status == 200 and out["ok"], out


def test_comment_is_recorded(server: Server) -> None:
    div = server.division("oasis-cloud")
    item_id = div["queue"]["pending"][0]["id"]
    status, out = _post(server.base + "/api/item/comment",
                        {"division_path": div["path"], "item_id": item_id, "body": "looks right to me"},
                        server.auth())
    assert status == 200 and out["ok"], out
    thread = server.root / "oasis-cloud" / ".swarm" / "comments" / f"{item_id}.jsonl"
    assert thread.exists()
    assert "looks right to me" in thread.read_text(encoding="utf-8")


def test_unknown_item_is_a_clean_400(server: Server) -> None:
    div = server.division("oasis-cloud")
    status, out = _post(server.base + "/api/item/claim",
                        {"division_path": div["path"], "item_id": "CLD-999"}, server.auth())
    assert status == 400 and not out["ok"]
    assert "CLD-999" in out["error"]


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_write_without_the_token_is_refused(server: Server) -> None:
    div = server.division("oasis-cloud")
    before = len(div["queue"]["pending"])
    status, out = _post(server.base + "/api/item/add",
                        {"division_path": div["path"], "description": "should not land"})
    assert status == 403 and not out["ok"]
    assert len(server.division("oasis-cloud")["queue"]["pending"]) == before


def test_write_from_a_foreign_origin_is_refused(server: Server) -> None:
    div = server.division("oasis-cloud")
    status, out = _post(server.base + "/api/item/add",
                        {"division_path": div["path"], "description": "csrf"},
                        server.auth({"Origin": "https://evil.example"}))
    assert status == 403 and not out["ok"]
    assert "cross-origin" in out["error"]


def test_write_outside_the_colony_is_refused(server: Server, tmp_path: Path) -> None:
    outside = tmp_path.parent / "not-a-division"
    for candidate in (str(outside), "/etc", str(server.root / "oasis-cloud" / ".." / ".." / "etc")):
        status, out = _post(server.base + "/api/item/add",
                            {"division_path": candidate, "description": "escape"},
                            server.auth())
        assert status == 400 and not out["ok"], candidate
        assert "unknown division" in out["error"]


def test_oversized_body_is_refused(server: Server) -> None:
    div = server.division("oasis-cloud")
    status, out = _post(server.base + "/api/item/add",
                        {"division_path": div["path"], "description": "x" * 200_000},
                        server.auth())
    assert status == 413 and not out["ok"]


def test_read_only_mode_serves_but_refuses_writes(readonly_server: Server) -> None:
    assert readonly_server.token == "", "read-only page must carry no write token"
    div = readonly_server.division("oasis-cloud")
    status, out = _post(readonly_server.base + "/api/item/add",
                        {"division_path": div["path"], "description": "nope"},
                        {"X-Swarm-Token": "anything", "Origin": readonly_server.base})
    assert status == 403 and not out["ok"]
    assert "read-only" in out["error"]
