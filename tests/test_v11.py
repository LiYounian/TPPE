"""v1.1 改动：数据库迁移、点选统计、句子存档字段、热梗模板校验、日志与前端上报、端口检查。"""

import json
import logging
import socket
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tppe import config, templates
from tppe.__main__ import port_free
from tppe.db import SCHEMA_VERSION, Database
from tppe.room import REVEAL, RoomManager
from tppe.server import create_app
from tppe.templates import Template


def test_default_port_is_6333():
    assert config.PORT == 6333 or "TPPE_PORT" in __import__("os").environ


def test_migrate_v1_db_keeps_data(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as c:  # 模拟 v1 结构的旧库
        c.executescript("""
          CREATE TABLE words (id INTEGER PRIMARY KEY, element TEXT NOT NULL, text TEXT NOT NULL,
            source TEXT NOT NULL DEFAULT 'player', use_count INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')), UNIQUE (element, text));
          CREATE TABLE sentences (id INTEGER PRIMARY KEY, room_code TEXT, template_id INTEGER,
            sentence TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')));
          INSERT INTO words (element, text) VALUES ('time', '旧词');
          INSERT INTO sentences (sentence) VALUES ('旧句子');""")
    db = Database(path)
    db.init_db()
    db.init_db()  # 再跑一次也不报错
    with sqlite3.connect(path) as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        cols = {r[1] for r in c.execute("PRAGMA table_info(sentences)")}
        assert {"answers", "meme", "round_no"} <= cols
        assert c.execute("SELECT text FROM words").fetchone()[0] == "旧词"
        assert c.execute("SELECT sentence FROM sentences").fetchone()[0] == "旧句子"


def test_picked_word_counts(db, bank):
    bank.submit("time", "春天")
    bank.submit("time", "春天", picked=True)
    bank.submit("time", "春天", picked=False)
    with db._connect() as c:
        row = c.execute("SELECT use_count, pick_count, last_used_at FROM words").fetchone()
    assert (row[0], row[1]) == (3, 1) and row[2]


def test_reveal_archives_answers_fillers_and_meme(db, bank):
    meme_tpl = Template(999, "{时间}{地点}{人物}{事件}，city不city？",
                        {"name": "city不city", "source": "测试", "date": "2024-06"})
    mgr = RoomManager(bank, db, [meme_tpl])
    room = mgr.create()
    a = room.join("小明")
    room.start(a.id)
    for el in config.ELEMENTS:
        room.submit(a.id, el, f"{el}词", picked=False)
    assert room.phase == REVEAL
    st = room.public_state()
    assert st["meme"]["name"] == "city不city"
    with db._connect() as c:
        row = c.execute("SELECT answers, meme, round_no, template_id FROM sentences").fetchone()
    ans = json.loads(row["answers"])
    assert ans["time"] == {"text": "time词", "by": "小明"}
    assert (row["meme"], row["round_no"], row["template_id"]) == ("city不city", 1, 999)


def test_meme_hidden_before_reveal(db, bank):
    meme_tpl = Template(1, "{时间}{地点}{人物}{事件}", {"name": "X", "source": "Y", "date": "2024"})
    room = RoomManager(bank, db, [meme_tpl]).create()
    a = room.join("a")
    room.start(a.id)
    assert "meme" not in room.public_state()


@pytest.mark.parametrize("meme", [{"name": "x", "source": "y"}, {"name": "x", "source": "y", "date": "24年"},
                                  {"name": "", "source": "y", "date": "2024"}])
def test_bad_meme_rejected(meme):
    with pytest.raises(ValueError):
        templates.check_meme(meme, 1)


@pytest.mark.parametrize("date", ["2024", "2024-06", "2024-06-01"])
def test_meme_date_formats(date):
    templates.check_meme({"name": "x", "source": "y", "date": date}, 1)


def test_template_ids_unique_and_memes_present():
    ts = templates.load()
    assert len({t.id for t in ts}) == len(ts)
    memes = [t for t in ts if t.meme]
    assert len(memes) >= 30
    assert len(ts) - len(memes) >= 100  # 普通模板也要够多


def test_duplicate_template_id_rejected(tmp_path):
    p = tmp_path / "t.json"
    item = {"id": 1, "text": "{时间}{地点}{人物}{事件}", "meme": None}
    p.write_text(json.dumps([item, item]), encoding="utf-8")
    with pytest.raises(ValueError):
        templates.load(p)


def test_client_log_endpoint_logs(tmp_path, caplog):
    with TestClient(create_app(tmp_path / "c.db")) as client, caplog.at_level(logging.INFO, "tppe"):
        r = client.post("/api/client-log", json={"kind": "tts_error", "message": "boom", "level": "error",
                                                 "room": "1234", "player": "ab(甲)"})
        assert r.json() == {"ok": True}
        r = client.post("/api/client-log", content=b"not json")
        assert r.status_code == 200
    rec = [x for x in caplog.records if x.name == "tppe.client"]
    assert rec and rec[0].levelno == logging.ERROR and "boom" in rec[0].getMessage()


def test_ws_lifecycle_logged(tmp_path, caplog):
    with TestClient(create_app(tmp_path / "w.db")) as client, caplog.at_level(logging.INFO, "tppe"):
        code = client.post("/api/rooms").json()["code"]
        with client.websocket_connect(f"/ws/{code}?name=甲") as ws:
            ws.receive_json()
            ws.send_text("{bad json")
            assert ws.receive_json()["type"] in ("room_state", "your_task", "error")
    msgs = "\n".join(f"[{r.name}] {r.getMessage()}" for r in caplog.records)
    assert "[tppe.ws] conn=" in msgs and "连接 room=" in msgs and "断开" in msgs
    assert "非法 JSON" in msgs
    assert f"room={code} 创建" in msgs and "已空，销毁" in msgs
    assert "[tppe.http] POST /api/rooms → 200" in msgs


def test_port_free_detects_busy():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen()
        assert port_free("127.0.0.1", s.getsockname()[1]) is False
