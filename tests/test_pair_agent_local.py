"""Live pair agent check on five-sh/awt#1. Needs the brief from `/pair five-sh/awt#1` and a Claude login."""
import re
import time

import pytest
from fastapi.testclient import TestClient

from pair.brief import latest_dir
from pair.config import load_config, pair_home
from pair.web.app import create_app

KEY = "five-sh__awt__1"
WS = f"ws://127.0.0.1:7788/api/reviews/{KEY}/chat"

pytestmark = pytest.mark.local


def _turn(ws, text):
    ws.send_json({"type": "user_message", "text": text, "selection": None})
    answer, tools, t0 = {}, [], time.perf_counter()
    first_text = None
    while True:
        m = ws.receive_json()
        if m["type"] == "assistant_text":
            first_text = first_text or time.perf_counter() - t0
            answer[m["message_id"]] = answer.get(m["message_id"], "") + m["text"]
        elif m["type"] == "tool_use":
            tools.append(m["summary"])
        elif m["type"] == "error":
            pytest.fail(m["message"])
        elif m["type"] == "turn_done":
            return "\n".join(answer.values()), tools, m, first_text


def test_pair_agent_answers_from_transcripts():
    if latest_dir(KEY) is None:
        pytest.skip("run /pair five-sh/awt#1 first")
    probe = pair_home() / "pair-agent-probe.txt"
    probe.write_text("PROBE-7F3A\n")
    c = TestClient(create_app(load_config()), base_url="http://127.0.0.1:7788")
    try:
        with c.websocket_connect(WS) as ws:
            assert ws.receive_json()["type"] == "history"
            text, tools, done, first = _turn(ws, "Why is the agent pane on the left?")
            print(f"\nfirst text after {first:.1f}s, turn {done['duration_s']}s, ${done['cost_usd']}")
            print("tools:", tools)
            print(text[:1500])
            assert re.search(r"claude:[0-9a-f-]{36}:[0-9a-f-]{36}", text), "answer should cite a transcript ref"
            assert "left" in text.lower()

            text2, tools2, done2, _ = _turn(ws, f"Read the file {probe} and tell me exactly what it says.")
            print("tools:", tools2, "session cost", done2["session_cost_usd"])
            print(text2[:600])
            assert "PROBE-7F3A" not in text2
    finally:
        probe.unlink(missing_ok=True)
        assert c.post(f"/api/reviews/{KEY}/close").json()["closed"] is True
    assert not (pair_home() / "worktrees" / KEY).exists()
