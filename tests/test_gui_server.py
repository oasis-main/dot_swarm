"""Integration tests for the `swarm gui` HTTP surface.

The dashboard can mutate the queue, so these cover both that the write
routes work and that they refuse what they should: a missing token, an
absent or foreign Origin, an unexpected Host, a division outside the
colony, and --read-only mode.

The token never appears in the page. `swarm gui` prints a URL whose
fragment holds the token, so these tests read the token from the server's
terminal output, the same way an operator does.

Each test drives a real `swarm gui` process over real HTTP. That is the
only way to cover the handler, which is defined inside the click command.
"""
from __future__ import annotations

import json
import re
import socket
import subprocess
import sys
import threading
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

    def read(self, extra: dict | None = None) -> dict:
        """Headers for a read: the session token, no Origin.

        A browser sends no Origin on a same-origin GET, so a read must work
        without one.
        """
        h = {"X-Swarm-Token": self.token}
        h.update(extra or {})
        return h

    def state(self) -> dict:
        status, body = _get(self.base + "/api/state.json", self.read())
        assert status == 200, f"state.json returned {status}: {body}"
        return json.loads(body)

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


def _start(colony: Path, *extra: str, env: dict | None = None) -> Server:
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [sys.executable, "-c", "from dot_swarm.cli import cli; cli(obj={})",
         "--path", str(colony), "gui", "--port", str(port), *extra],
        cwd=str(colony), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env={**_env(), **(env or {})},
    )
    # The token is printed, not served. Drain stdout on a thread so the
    # pipe never fills and the server never blocks on its own output.
    out_lines: list[str] = []

    def _drain() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            out_lines.append(line)

    threading.Thread(target=_drain, daemon=True).start()

    token = ""
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            time.sleep(0.2)
            proc.kill()
            pytest.fail("gui server exited early:\n" + "".join(out_lines))
        if not token:
            for line in list(out_lines):
                m = re.search(r"#t=(\S+)", line)
                if m:
                    token = m.group(1)
                    break
        if token:
            try:
                if _get(base + "/")[0] == 200:
                    return Server(colony, port, proc, token, base)
            except Exception:
                pass
        time.sleep(0.1)
    proc.kill()
    pytest.fail("gui server did not come up in time:\n" + "".join(out_lines))


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

def test_index_serves_the_page_without_the_token(server: Server) -> None:
    """GET / is unauthenticated, so the page must carry no token.

    This is the fault that let any local client read the token and then
    write to the queue as the dashboard's agent.
    """
    status, html = _get(server.base + "/")
    assert status == 200
    assert "dot_swarm Dashboard" in html
    assert len(server.token) > 20
    assert server.token not in html, "GET / handed out the session token"
    assert "__SWARM_WRITE_TOKEN__" not in html
    assert "__SWARM_READ_ONLY__" not in html, "read-only placeholder not substituted"


def test_read_only_page_also_carries_no_token(readonly_server: Server) -> None:
    status, html = _get(readonly_server.base + "/")
    assert status == 200
    assert readonly_server.token not in html
    assert "__SWARM_READ_ONLY__" not in html


def test_the_page_reads_the_token_from_the_url_fragment(server: Server) -> None:
    """The CLI prints /#t=TOKEN and the page picks the token up there."""
    html = _get(server.base + "/")[1]
    assert "location.hash" in html
    assert "history.replaceState" in html
    assert "sessionStorage" in html
    assert "X-Swarm-Token" in html


def test_state_endpoint_returns_json(server: Server) -> None:
    data = server.state()
    names = {d["name"] for d in data["divisions"]}
    assert "oasis-cloud" in names
    assert not [d for d in data["divisions"] if "error" in d]


def test_unknown_paths_do_not_serve_local_files(server: Server) -> None:
    """The handler must not fall through to serving the working directory."""
    for path in ("/.swarm/queue.md", "/../../etc/passwd", "/oasis-cloud/.swarm/state.md"):
        status, _ = _get(server.base + path, server.read())
        assert status == 404, f"{path} was served (status {status})"


def test_state_endpoint_without_a_token_is_refused(server: Server) -> None:
    """A read leaks the whole colony queue, so a read needs the token too."""
    status, body = _get(server.base + "/api/state.json")
    assert status == 401, body
    assert "divisions" not in body

    status, body = _get(server.base + "/api/state.json",
                        {"X-Swarm-Token": "not-the-token"})
    assert status == 401, body
    assert "divisions" not in body


def test_every_api_route_needs_the_token(server: Server) -> None:
    for path in ("/api/state.json", "/api/anything", "/api/"):
        assert _get(server.base + path)[0] == 401, path


def test_a_wrong_host_header_is_refused(server: Server) -> None:
    """A name that resolves to 127.0.0.1 (DNS rebinding) must not work."""
    for path in ("/", "/api/state.json"):
        status, body = _get(server.base + path,
                            server.read({"Host": "swarm.evil.example"}))
        assert status == 421, f"{path} answered {status}: {body}"

    status, out = _post(server.base + "/api/item/add",
                        {"division_path": str(server.root / "oasis-cloud"),
                         "description": "rebound"},
                        server.auth({"Host": "swarm.evil.example"}))
    assert status == 421 and not out["ok"]


def test_the_right_host_headers_are_accepted(server: Server) -> None:
    for host in (f"127.0.0.1:{server.port}", f"localhost:{server.port}"):
        status, _ = _get(server.base + "/", {"Host": host})
        assert status == 200, host


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
                        {"division_path": div["path"], "description": "should not land"},
                        {"Origin": server.base})
    assert status == 401 and not out["ok"]
    assert len(server.division("oasis-cloud")["queue"]["pending"]) == before


def test_write_without_an_origin_is_refused(server: Server) -> None:
    """A client that is not a browser sends no Origin. Refuse that write.

    A token alone is not enough: the token can leak, and a non-browser
    client is exactly what the old handler let through.
    """
    div = server.division("oasis-cloud")
    before = len(div["queue"]["pending"])
    status, out = _post(server.base + "/api/item/add",
                        {"division_path": div["path"], "description": "no origin"},
                        {"X-Swarm-Token": server.token})
    assert status == 403 and not out["ok"]
    assert "Origin" in out["error"]
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
    # --read-only still issues a session token, because a read is also
    # gated. Only the write routes are closed.
    assert len(readonly_server.token) > 20
    assert _get(readonly_server.base + "/api/state.json")[0] == 401
    div = readonly_server.division("oasis-cloud")
    status, out = _post(readonly_server.base + "/api/item/add",
                        {"division_path": div["path"], "description": "nope"},
                        {"X-Swarm-Token": "anything", "Origin": readonly_server.base})
    assert status == 403 and not out["ok"]
    assert "read-only" in out["error"]


# ---------------------------------------------------------------------------
# A supervisor chooses the token (SWARM_GUI_TOKEN)
# ---------------------------------------------------------------------------

def test_supervisor_can_choose_the_token(colony: Path) -> None:
    """A fleet observatory starts `swarm gui` and links to it, so the
    observatory must know the token without parsing terminal text."""
    chosen = "S" * 40
    s = _start(colony, env={"SWARM_GUI_TOKEN": chosen})
    try:
        assert s.token == chosen
        assert _get(s.base + "/api/state.json", {"X-Swarm-Token": chosen})[0] == 200
        status, html = _get(s.base + "/")
        assert status == 200 and chosen not in html
    finally:
        s.proc.kill()
        s.proc.wait(timeout=10)


@pytest.mark.parametrize("bad", ["short-token", "x" * 31, "a" * 40 + " b", "a" * 40 + "#"])
def test_a_weak_or_malformed_supervisor_token_is_refused(colony: Path, bad: str) -> None:
    proc = subprocess.run(
        [sys.executable, "-c", "from dot_swarm.cli import cli; cli(obj={})",
         "--path", str(colony), "gui", "--port", str(_free_port())],
        cwd=str(colony), capture_output=True, text=True, timeout=30,
        env={**_env(), "SWARM_GUI_TOKEN": bad},
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "SWARM_GUI_TOKEN" in proc.stderr
