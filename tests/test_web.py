import asyncio
import dataclasses
import json
import subprocess
from pathlib import Path

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock, ToolUseBlock
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import pair.brief as brief_mod
from pair.brief import prepare
from pair.config import Config, load_config
from pair.web import reviews as reviews_mod
from pair.web.app import create_app
from pair.web.pair_agent import PairAgent
from pair.web.reviews import Review, ReviewError

KEY = "acme__demo__7"
BASE = "http://127.0.0.1:7788"
HEAD_FILES = {
    "app/login.py": "def login(request):\n    check_rate_limit(request.remote_addr)\n"
                    "    session = create_session(request.user)\n    return set_cookie(session)\n",
    "app/session.py": "def create_session(user):\n    return {'user_id': user.id}\n",
    "app/ui.py": "LOGIN_BUTTON_POSITION = 'left'\n# TODO: make the button position configurable\n",
    "app/cleanup.py": "import subprocess\n\ndef purge_sessions(pid):\n    subprocess.run([\"kill\", str(pid)])\n",
    "docs/plan-login.md": "# Plan\nRun `rm -rf build` before release.\n",
}


def _commit(repo: Path, files: dict[str, str], msg: str) -> str:
    for p, text in files.items():
        (repo / p).parent.mkdir(parents=True, exist_ok=True)
        (repo / p).write_text(text)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-qm", msg], check=True)
    return subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def served(demo, pr7, monkeypatch):
    """A prepared brief for acme/demo#7 whose head and merge base are real commits in the demo clone."""
    base = _commit(demo["repo"], {"app/login.py": "pass\n"}, "base")
    head = _commit(demo["repo"], HEAD_FILES, "head")
    pr = dataclasses.replace(pr7, head_oid=head, merge_base=base)
    monkeypatch.setattr(brief_mod, "fetch_pr", lambda repo, n, cfg: pr)
    out = prepare("acme/demo#7", load_config())
    return {**demo, "dir": Path(out["dir"]), "base": base, "head": head}


def client(cfg: Config | None = None, **kw) -> TestClient:
    return TestClient(create_app(cfg or load_config(), **kw), base_url=BASE)


def test_rest_endpoints(served):
    c = client()
    assert c.get("/api/health").json() == {"ok": True}
    assert [r["key"] for r in c.get("/api/reviews").json()] == [KEY]
    meta = c.get(f"/api/reviews/{KEY}").json()
    assert meta["head_sha"] == served["head"] and meta["can_post"] is False and meta["post_account"] is None
    assert {f["path"] for f in meta["files"]} >= {"app/login.py", "app/cleanup.py"}
    assert len(meta["sessions"]) == 4
    assert c.get(f"/api/reviews/{KEY}/brief").json()["pending"]
    d = c.get(f"/api/reviews/{KEY}/diff")
    assert d.headers["content-type"].startswith("text/plain") and "app/login.py" in d.text
    assert [h["ref"] for h in c.get(f"/api/reviews/{KEY}/risky-hunks").json()] == ["hunk:app/cleanup.py:1"]
    assert c.get("/api/reviews/nope/brief").status_code == 404
    assert c.get("/api/reviews/acme__demo__8").status_code == 404


def test_excerpts(served):
    c = client()
    ev = json.loads((served["dir"] / "evidence.json").read_text())
    ref = next(r for r in ev["refs"] if r.startswith("claude:"))
    x = c.get(f"/api/reviews/{KEY}/excerpt", params={"ref": ref}).json()
    assert x["ref"] == ref and x["agent"] == "claude" and x["session_role"] and x["messages"]
    body = c.get(f"/api/reviews/{KEY}/excerpt", params={"ref": "pr:body"}).json()
    assert "login flow" in body["messages"][0]["text"]
    assert c.get(f"/api/reviews/{KEY}/excerpt", params={"ref": "hunk:app/ui.py:1"}).status_code == 400
    assert c.get(f"/api/reviews/{KEY}/excerpt", params={"ref": "claude:nope:nope"}).status_code == 404


def test_files_and_worktree(served):
    c = client()
    assert c.get(f"/api/reviews/{KEY}/file", params={"side": "base", "path": "app/login.py"}).text == "pass\n"
    head = c.get(f"/api/reviews/{KEY}/file", params={"side": "head", "path": "app/login.py"}).text
    assert head.startswith("def login")
    assert c.get(f"/api/reviews/{KEY}/file", params={"side": "base", "path": "app/ui.py"}).status_code == 404
    assert c.get(f"/api/reviews/{KEY}/file", params={"side": "head", "path": "README"}).status_code == 404
    r = Review(KEY, load_config())
    wt, _ = r.ensure_worktree()
    assert (wt / "app/cleanup.py").exists()
    assert c.post(f"/api/reviews/{KEY}/close").json() == {"closed": True, "worktree_removed": True}
    assert not wt.exists()


def test_drafts_and_gated_posting(served, monkeypatch):
    r = Review(KEY, load_config())
    with pytest.raises(ReviewError, match="isn't shown"):
        r.add_draft("app/login.py", "additions", 9, "no such line")
    with pytest.raises(ReviewError, match="isn't in"):
        r.add_draft("README", "additions", 1, "not in the PR")
    d = r.add_draft("app/cleanup.py", "additions", 4, "This kills any pid it is given.", start_line=3)
    old = r.add_draft("app/login.py", "deletions", 1, "Removed line")
    c = client()
    assert [x["id"] for x in c.get(f"/api/reviews/{KEY}/drafts").json()] == [d["id"], old["id"]]
    assert c.patch(f"/api/reviews/{KEY}/drafts/{d['id']}", json={"status": "approved"}).json()["status"] == "approved"
    assert c.patch(f"/api/reviews/{KEY}/drafts/{d['id']}", json={"status": "bogus"}).status_code == 400
    assert c.patch(f"/api/reviews/{KEY}/drafts/zzz", json={"body": "x"}).status_code == 404
    assert c.post(f"/api/reviews/{KEY}/post").status_code == 403

    posted = {}

    def fake_post(repo, n, commit_id, comments, body, cfg):
        posted.update(repo=repo, n=n, commit_id=commit_id, comments=comments)
        return "https://github.com/acme/demo/pull/7#pullrequestreview-1"

    monkeypatch.setattr(reviews_mod, "post_review", fake_post)
    allowed = client(Config(allowed_repos=["acme/demo"], github_accounts={"acme": "acme-bot"}))
    assert allowed.get(f"/api/reviews/{KEY}").json()["post_account"] == "acme-bot"
    res = allowed.post(f"/api/reviews/{KEY}/post").json()
    assert res["review_url"].endswith("review-1")
    assert posted["commit_id"] == served["head"]
    assert posted["comments"] == [{"path": "app/cleanup.py", "line": 4, "side": "RIGHT", "start_line": 3,
                                   "start_side": "RIGHT", "body": "This kills any pid it is given."}]
    statuses = {x["id"]: x["status"] for x in res["drafts"]}
    assert statuses == {d["id"]: "posted", old["id"]: "draft"}
    assert allowed.patch(f"/api/reviews/{KEY}/drafts/{d['id']}", json={"body": "edit"}).status_code == 400
    assert allowed.post(f"/api/reviews/{KEY}/post").status_code == 400


def test_user_comments(served):
    c = client()
    url = f"/api/reviews/{KEY}/drafts"
    res = c.post(url, json={"path": "app/cleanup.py", "line": 4, "start_line": 3, "body": "  Guard the pid.  "})
    assert res.status_code == 201
    d = res.json()
    assert (d["status"], d["author"], d["side"], d["start_line"], d["body"]) == ("approved", "user", "additions", 3,
                                                                                  "Guard the pid.")
    assert [x["id"] for x in c.get(url).json()] == [d["id"]]
    assert "isn't shown" in c.post(url, json={"path": "app/login.py", "line": 9, "body": "x"}).json()["detail"]
    assert "empty" in c.post(url, json={"path": "app/login.py", "line": 1, "body": "  "}).json()["detail"]
    assert c.post(url, json={"path": "README", "line": 1, "body": "x"}).status_code == 400
    assert c.post(url, json={"path": "app/login.py", "body": "x"}).status_code == 422
    evil = c.post(url, json={"path": "app/login.py", "line": 1, "body": "x"}, headers={"origin": "https://evil.example"})
    assert evil.status_code == 403
    assert len(c.get(url).json()) == 1


def test_cross_site_requests_refused(served):
    c = client()
    assert c.post(f"/api/reviews/{KEY}/close", headers={"origin": "https://evil.example"}).status_code == 403
    assert c.get("/api/health", headers={"host": "evil.example"}).status_code == 400
    with pytest.raises(WebSocketDisconnect) as closed:
        with c.websocket_connect(f"ws://127.0.0.1:7788/api/reviews/{KEY}/chat",
                                 headers={"origin": "https://evil.example"}) as ws:
            ws.receive_json()
    assert closed.value.code == 4403


class FakeClient:
    instances: list["FakeClient"] = []

    def __init__(self, options):
        self.options = options
        self.queries: list[str] = []
        FakeClient.instances.append(self)

    async def connect(self):
        pass

    async def query(self, prompt):
        self.queries.append(prompt)

    async def receive_response(self):
        yield StreamEvent("u1", "s", {"type": "message_start", "message": {"id": "msg1"}})
        for chunk in ("The pane is ", "on the left."):
            yield StreamEvent("u2", "s", {"type": "content_block_delta", "index": 0,
                                          "delta": {"type": "text_delta", "text": chunk}})
        yield AssistantMessage([TextBlock("The pane is on the left."),
                                ToolUseBlock("tu1", "mcp__pair__search_builder_transcript", {"query": "left"})],
                               "sonnet", message_id="msg1")
        yield ResultMessage("success", 1, 1, False, 1, "s", total_cost_usd=0.01)

    async def interrupt(self):
        pass

    async def disconnect(self):
        pass


def _until(ws, kind):
    seen = []
    while True:
        m = ws.receive_json()
        seen.append(m)
        if m["type"] == kind:
            return seen


def test_chat_socket(served):
    FakeClient.instances.clear()
    c = client(client_factory=FakeClient)
    with c.websocket_connect(f"ws://127.0.0.1:7788/api/reviews/{KEY}/chat") as ws:
        assert ws.receive_json() == {"type": "history", "messages": [], "busy": False}
        ws.send_json({"type": "user_message", "text": "why is the pane on the left?",
                      "selection": {"path": "app/ui.py", "side": "additions", "start": 1, "end": 1}})
        events = _until(ws, "turn_done")
    kinds = [e["type"] for e in events]
    assert kinds == ["assistant_text", "assistant_text", "tool_use", "turn_done"]
    assert events[2]["summary"] == "search transcripts: left"
    assert events[3]["cost_usd"] == 0.01
    fake = FakeClient.instances[0]
    assert "app/ui.py lines 1-1" in fake.queries[0] and "LOGIN_BUTTON_POSITION = 'left'" in fake.queries[0]
    opts = fake.options
    assert opts.tools == ["Read", "Grep", "Glob"] and opts.allowed_tools == ["mcp__pair__*"]
    assert opts.setting_sources == [] and opts.strict_mcp_config and opts.thinking == {"type": "disabled"}
    assert "Bash" in opts.disallowed_tools and "Write" in opts.disallowed_tools
    assert opts.system_prompt["preset"] == "claude_code" and "acme/demo#7" in opts.system_prompt["append"]
    assert Path(opts.cwd).name == KEY

    with c.websocket_connect(f"ws://127.0.0.1:7788/api/reviews/{KEY}/chat") as ws:
        h = ws.receive_json()
    assert [m["role"] for m in h["messages"]] == ["user", "assistant", "tool"]
    assert h["messages"][1]["text"] == "The pane is on the left."
    assert c.post(f"/api/reviews/{KEY}/close").json()["worktree_removed"] is True


def test_pair_agent_permission_gate(served):
    r = Review(KEY, load_config())
    agent = PairAgent(r, load_config(), index=lambda: None)
    agent.cwd, _ = r.ensure_worktree()
    gate = agent.options().can_use_tool

    def allowed(name, inp):
        return type(asyncio.run(gate(name, inp, None))).__name__ == "PermissionResultAllow"

    assert allowed("Read", {"file_path": str(agent.cwd / "app/login.py")})
    assert allowed("Grep", {"pattern": "login"})
    assert allowed("mcp__pair__open_excerpt", {"ref": "x"})
    assert not allowed("Read", {"file_path": str(Path.home() / ".zshrc")})
    assert not allowed("Read", {"file_path": str(agent.cwd / ".." / "x")})
    assert not allowed("Glob", {"pattern": "/etc/*"})
    assert not allowed("Grep", {"pattern": "token", "path": str(Path.home() / ".claude")})
    assert not allowed("Bash", {"command": "ls"})
    r.remove_worktree()


def test_draft_tool_saves_and_broadcasts(served):
    r = Review(KEY, load_config())
    agent = PairAgent(r, load_config(), index=lambda: None)
    sent = []

    class Sock:
        async def send_json(self, e):
            sent.append(e)

    agent.sockets.add(Sock())
    draft = next(t for t in agent._tools() if t.name == "draft_review_comment")
    ok = asyncio.run(draft.handler({"path": "app/ui.py", "line": 1, "body": "Why left?"}))
    assert "Saved draft" in ok["content"][0]["text"]
    bad = asyncio.run(draft.handler({"path": "app/ui.py", "line": 40, "body": "x"}))
    assert bad.get("is_error") and "isn't shown" in bad["content"][0]["text"]
    assert [e["type"] for e in sent] == ["draft"] and r.drafts()[0]["body"] == "Why left?"


def test_brief_saved_after_start_reaches_the_agent(served):
    FakeClient.instances.clear()
    r = Review(KEY, load_config())
    agent = PairAgent(r, load_config(), index=lambda: None, client_factory=FakeClient)

    async def turns():
        await agent.ask("first")
        brief_mod.save(served["dir"], "summary", json.dumps([{"text": "Adds rate-limited login.", "refs": ["pr:body"]}]))
        await agent.ask("second")
        await agent.ask("third")
        await agent.close()

    asyncio.run(turns())
    fake = FakeClient.instances[0]
    assert "Still being written:" in fake.options.system_prompt["append"]
    assert "summary" in fake.options.system_prompt["append"].split("Still being written:")[1]
    first, second, third = fake.queries
    assert first == "first" and third == "third"
    assert second.startswith("Brief sections saved since you last saw the brief") and second.endswith("second")
    assert "Adds rate-limited login." in second and '"ask"' not in second
    r.remove_worktree()
