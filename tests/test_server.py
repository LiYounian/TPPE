"""端到端：用 TestClient 模拟 4 个玩家连 WebSocket 走完两轮，确认揭晓、入库、候选回流。"""

import pytest
from fastapi.testclient import TestClient

from tppe import config
from tppe.server import create_app


@pytest.fixture
def client(tmp_path):
    # 必须用 with：共享一个事件循环，否则跨连接广播会卡住（生产 uvicorn 本就单循环）
    with TestClient(create_app(tmp_path / "e2e.db")) as c:
        yield c


def drain_until(ws, pred, limit=50):
    """读消息直到满足条件，返回该消息。"""
    for _ in range(limit):
        m = ws.receive_json()
        if pred(m):
            return m
    raise AssertionError("没等到期望的消息")


def latest_task(ws):
    """读到本次广播里属于自己的 your_task（room_state 后紧跟 your_task）。"""
    return drain_until(ws, lambda m: m["type"] == "your_task")


def test_health_and_index(client):
    assert client.get("/health").json()["ok"] is True
    assert client.get("/").status_code == 200


def test_seed_words_loaded(client):
    assert client.get("/health").json()["words"] >= 40


def test_join_missing_room(client):
    assert client.get("/api/rooms/0000").status_code == 404
    with client.websocket_connect("/ws/0000?name=a") as ws:
        assert ws.receive_json()["type"] == "error"


def test_four_players_full_round(client):
    code = client.post("/api/rooms").json()["code"]
    names = ["甲", "乙", "丙", "丁"]
    wss = []
    try:
        for n in names:
            ws = client.websocket_connect(f"/ws/{code}?name={n}").__enter__()
            assert ws.receive_json()["type"] == "welcome"
            wss.append(ws)
        wss[0].send_json({"type": "start"})
        for rnd in range(2):
            # 跳过加入阶段/上一轮残留的广播，只取本轮分到的任务
            tasks = [drain_until(ws, lambda m: m["type"] == "your_task" and m["round"] == rnd + 1
                                 and m["element"]) for ws in wss]
            assert sorted(t["element"] for t in tasks) == sorted(config.ELEMENTS)
            for t in tasks:
                assert len(t["suggestions"]) <= config.SUGGEST_COUNT
            for ws, t in zip(wss, tasks):
                ws.send_json({"type": "submit", "element": t["element"], "text": f"第{rnd}轮{t['label']}"})
            reveal = drain_until(wss[0], lambda m: m["type"] == "room_state" and m["phase"] == "reveal")
            for el, label in config.ELEMENTS.items():
                assert f"第{rnd}轮{label}" in reveal["sentence"]
            for ws in wss[1:]:
                drain_until(ws, lambda m: m["type"] == "room_state" and m["phase"] == "reveal")
            if rnd == 0:
                wss[2].send_json({"type": "next"})
        assert client.get("/health").json()["words"] >= 40 + 8
    finally:
        for ws in wss:
            ws.__exit__(None, None, None)


def test_bad_submit_returns_error(client):
    code = client.post("/api/rooms").json()["code"]
    with client.websocket_connect(f"/ws/{code}?name=独行") as ws:
        ws.receive_json()
        latest_task(ws)
        ws.send_json({"type": "start"})
        t = latest_task(ws)
        ws.send_json({"type": "submit", "element": t["element"], "text": "长" * 21})
        err = drain_until(ws, lambda m: m["type"] == "error")
        assert "20" in err["message"]
