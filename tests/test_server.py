"""端到端：用 TestClient 模拟玩家连 WebSocket，走完 填写 → 朗读 → 投票 → 结算 → 下一轮；
另测超时自动选词由服务端定时任务推进。计时参数调小以便快速跑完。"""

import pytest
from fastapi.testclient import TestClient

from tppe import config
from tppe.room import Timing
from tppe.server import create_app

FAST = Timing(fill=30, vote=0.4, read_min=0.1, read_max=0.1)


@pytest.fixture
def client(tmp_path):
    # 必须用 with：共享一个事件循环，否则跨连接广播会卡住（生产 uvicorn 本就单循环）
    with TestClient(create_app(tmp_path / "e2e.db", timing=FAST, tick_seconds=0.05)) as c:
        yield c


def drain_until(ws, pred, limit=200):
    """读消息直到满足条件，返回该消息。"""
    for _ in range(limit):
        m = ws.receive_json()
        if pred(m):
            return m
    raise AssertionError("没等到期望的消息")


def phase_is(p):
    return lambda m: m["type"] == "room_state" and m["phase"] == p


def test_health_and_index(client):
    h = client.get("/health").json()
    assert h["ok"] is True and "version" in h
    assert client.get("/").status_code == 200


def test_seed_words_loaded(client):
    assert client.get("/health").json()["words"] >= 40


def test_join_missing_room(client):
    assert client.get("/api/rooms/0000").status_code == 404
    with client.websocket_connect("/ws/0000?name=a") as ws:
        assert ws.receive_json()["type"] == "error"


def test_four_players_full_round_with_votes(client):
    code = client.post("/api/rooms").json()["code"]
    names = ["甲", "乙", "丙", "丁"]
    wss, ids = [], []
    try:
        for n in names:
            ws = client.websocket_connect(f"/ws/{code}?name={n}").__enter__()
            ids.append(ws.receive_json()["player_id"])
            wss.append(ws)
        wss[0].send_json({"type": "start"})
        for rnd in range(2):
            tasks = [drain_until(ws, lambda m: m["type"] == "your_task" and m["round"] == rnd + 1
                                 and m["element"]) for ws in wss]
            assert sorted(t["element"] for t in tasks) == sorted(config.ELEMENTS)
            for t in tasks:
                assert len(t["suggestions"]) <= config.SUGGEST_COUNT
                assert all(set(w) == {"text", "by"} for w in t["suggestions"])
                assert t["time_left"] == FAST.fill
            for ws, t in zip(wss, tasks):
                ws.send_json({"type": "submit", "element": t["element"], "text": f"第{rnd}轮{t['label']}"})
            reveal = drain_until(wss[0], phase_is("reveal"))
            for label in config.ELEMENTS.values():
                assert f"第{rnd}轮{label}" in reveal["sentence"]
            # 朗读结束 → 投票：每人投给下一个人填的词
            voting = drain_until(wss[0], phase_is("voting"))
            slot_by = {el: s["filler_id"] for el, s in voting["slots"].items()}
            for i, ws in enumerate(wss):
                target = next(el for el, by in slot_by.items() if by == ids[(i + 1) % 4])
                ws.send_json({"type": "vote", "element": target})
            result = drain_until(wss[0], phase_is("result"))
            assert sum(result["tally"].values()) == 4
            assert sorted(p["score"] for p in result["players"]) == [rnd + 1] * 4
            if rnd == 0:
                wss[2].send_json({"type": "next"})
        assert client.get("/health").json()["words"] >= 40 + 8
    finally:
        for ws in wss:
            ws.__exit__(None, None, None)


def test_vote_own_word_rejected(tmp_path):
    # 不提交任何词，让所有空超时自动填（每人至少分到一个空），然后投自己的词应被拒
    t = Timing(fill=0.3, vote=10, read_min=0.1, read_max=0.1)
    with TestClient(create_app(tmp_path / "v.db", timing=t, tick_seconds=0.05)) as client:
        code = client.post("/api/rooms").json()["code"]
        with client.websocket_connect(f"/ws/{code}?name=甲") as a, \
                client.websocket_connect(f"/ws/{code}?name=乙") as b:
            me = a.receive_json()["player_id"]
            b.receive_json()
            a.send_json({"type": "start"})
            voting = drain_until(a, phase_is("voting"))
            own = next(el for el, s in voting["slots"].items() if s["filler_id"] == me)
            a.send_json({"type": "vote", "element": own})
            assert "自己" in drain_until(a, lambda m: m["type"] == "error")["message"]


def test_timeout_autofill_driven_by_server_ticker(tmp_path):
    fast_fill = Timing(fill=0.3, vote=0.3, read_min=0.1, read_max=0.1)
    with TestClient(create_app(tmp_path / "t.db", timing=fast_fill, tick_seconds=0.05)) as client:
        code = client.post("/api/rooms").json()["code"]
        with client.websocket_connect(f"/ws/{code}?name=发呆的人") as ws:
            ws.receive_json()
            ws.send_json({"type": "start"})
            st = drain_until(ws, phase_is("reveal"))  # 一个字都不填，四个空依次超时
            assert all(s["auto"] for s in st["slots"].values())
            drain_until(ws, phase_is("result"))  # 单人局没人能投票，直接结算


def test_bad_submit_returns_error(client):
    code = client.post("/api/rooms").json()["code"]
    with client.websocket_connect(f"/ws/{code}?name=独行") as ws:
        ws.receive_json()
        ws.send_json({"type": "start"})
        t = drain_until(ws, lambda m: m["type"] == "your_task" and m["element"])
        ws.send_json({"type": "submit", "element": t["element"], "text": "长" * 21})
        err = drain_until(ws, lambda m: m["type"] == "error")
        assert "20" in err["message"]
