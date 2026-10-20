"""锁住管理后台规则：没配口令 → 404；口令错 → 401；连续输错锁定 → 429；
删除 = 隐藏（不出现在候选、重启补写预置词也不复活、可恢复）；改字 = 隐藏旧词 + 新词继承计数；
新增/改字的校验与冲突；玩家再填被隐藏的词仍保持隐藏；写操作有日志且日志里没有口令；v3→v4 迁移不丢数据。"""

import logging
import sqlite3

import pytest
from fastapi.testclient import TestClient

from tppe import admin as admin_mod
from tppe.admin import Guard
from tppe.db import Database
from tppe.server import create_app
from tppe.wordbank import WordBank, load_seed

TOKEN = "test-admin-token-123456"
H = {"X-Admin-Token": TOKEN}


@pytest.fixture
def app_db(tmp_path):
    path = tmp_path / "admin.db"
    with TestClient(create_app(path, admin_token=TOKEN)) as c:
        yield c, Database(path), path


def find(c, text, **params):
    items = c.get("/api/admin/words", params={"q": text, **params}, headers=H).json()["items"]
    return next((w for w in items if w["text"] == text), None)


# ---------- 访问控制 ----------
def test_disabled_without_token(tmp_path):
    with TestClient(create_app(tmp_path / "x.db", admin_token="")) as c:
        assert c.get("/admin").status_code == 404
        assert c.get("/api/admin/stats", headers=H).status_code == 404


def test_page_and_auth(app_db):
    c, _, _ = app_db
    assert c.get("/admin").status_code == 200
    assert c.get("/api/admin/stats").status_code == 401
    assert c.get("/api/admin/stats", headers={"X-Admin-Token": "wrong"}).status_code == 401
    r = c.get("/api/admin/stats", headers=H)
    assert r.status_code == 200 and r.json()["words"]["time"] == 15
    assert c.post("/api/admin/login", headers=H).json() == {"ok": True}


def test_lockout_after_10_failures_then_expires():
    t = [0.0]
    g = Guard("secret-token-xyz", clock=lambda: t[0])
    for _ in range(admin_mod.MAX_FAILS - 1):
        with pytest.raises(Exception) as e:
            g.check("1.2.3.4", "bad")
        assert e.value.status_code == 401
    with pytest.raises(Exception) as e:
        g.check("1.2.3.4", "bad")
    assert e.value.status_code == 429
    with pytest.raises(Exception) as e:
        g.check("1.2.3.4", "secret-token-xyz")  # 锁定期间正确口令也不行
    assert e.value.status_code == 429
    g.check("5.6.7.8", "secret-token-xyz")      # 别的 IP 不受影响
    t[0] += admin_mod.LOCK_SECONDS + 1
    g.check("1.2.3.4", "secret-token-xyz")      # 过了锁定期恢复


def test_failures_outside_window_dont_accumulate():
    t = [0.0]
    g = Guard("secret-token-xyz", clock=lambda: t[0])
    for _ in range(20):
        with pytest.raises(Exception) as e:
            g.check("ip", "bad")
        assert e.value.status_code == 401
        t[0] += admin_mod.FAIL_WINDOW / 5  # 每个窗口内最多 5 次，永远到不了 10 次


# ---------- 删除 = 隐藏 ----------
def test_hide_excludes_from_suggestions_and_survives_reseed(app_db):
    c, db, path = app_db
    w = find(c, "除夕夜")
    assert c.post(f"/api/admin/words/{w['id']}/hide", headers=H).json()["hidden"] is True
    bank = WordBank(db)
    for _ in range(30):
        assert "除夕夜" not in [x["text"] for x in bank.suggest("time")]
    Database(path).init_db(load_seed())  # 模拟重启时补写预置词
    assert find(c, "除夕夜", hidden=True)["hidden"] is True
    assert find(c, "除夕夜") is None
    assert c.get("/api/admin/stats", headers=H).json()["hidden"] == 1
    c.post(f"/api/admin/words/{w['id']}/restore", headers=H)
    assert find(c, "除夕夜")["hidden"] is False


def test_player_reentering_hidden_word_keeps_it_hidden(app_db):
    c, db, _ = app_db
    w = find(c, "除夕夜")
    c.post(f"/api/admin/words/{w['id']}/hide", headers=H)
    bank = WordBank(db)
    assert bank.submit("time", "除夕夜", author="玩家") == "除夕夜"  # 本轮照常可用
    again = find(c, "除夕夜", hidden=True)
    assert again["hidden"] is True and again["use_count"] == 1


# ---------- 改字 ----------
def test_edit_hides_old_and_new_inherits_counts(app_db):
    c, db, _ = app_db
    WordBank(db).submit("place", "火新基地", author="小明")
    WordBank(db).submit("place", "火新基地", picked=True)
    old = find(c, "火新基地")
    r = c.patch(f"/api/admin/words/{old['id']}", json={"text": "火锅基地"}, headers=H)
    assert r.status_code == 200
    new = r.json()
    assert new["text"] == "火锅基地" and new["origin"] == "admin"
    assert (new["use_count"], new["pick_count"]) == (old["use_count"], old["pick_count"])
    assert find(c, "火新基地") is None and find(c, "火新基地", hidden=True)
    texts = {x["text"] for x in db.random_words("place", 100)}
    assert "火锅基地" in texts and "火新基地" not in texts


def test_edit_conflict_and_validation(app_db):
    c, _, _ = app_db
    w = find(c, "除夕夜")
    assert c.patch(f"/api/admin/words/{w['id']}", json={"text": "五分钟前"}, headers=H).status_code == 409
    assert c.patch(f"/api/admin/words/{w['id']}", json={"text": "长" * 21}, headers=H).status_code == 400
    assert c.patch(f"/api/admin/words/{w['id']}", json={"text": "  "}, headers=H).status_code == 400
    assert c.patch("/api/admin/words/999999", json={"text": "x"}, headers=H).status_code == 404


# ---------- 新增 ----------
def test_add_word_validation_and_dup(app_db):
    c, db, _ = app_db
    r = c.post("/api/admin/words", json={"element": "event", "text": " 跳了一支 舞 "}, headers=H)
    assert r.status_code == 200 and r.json()["text"] == "跳了一支 舞" and r.json()["origin"] == "admin"
    assert "跳了一支 舞" in {x["text"] for x in db.random_words("event", 100)}
    assert c.post("/api/admin/words", json={"element": "event", "text": "跳了一支 舞"}, headers=H).status_code == 409
    assert c.post("/api/admin/words", json={"element": "event", "text": "x" * 21}, headers=H).status_code == 400
    assert c.post("/api/admin/words", json={"element": "weather", "text": "晴"}, headers=H).status_code == 400
    # 已删除的词再新增 → 409 且提示可恢复
    w = find(c, "除夕夜")
    c.post(f"/api/admin/words/{w['id']}/hide", headers=H)
    r = c.post("/api/admin/words", json={"element": "time", "text": "除夕夜"}, headers=H)
    assert r.status_code == 409 and "恢复" in r.json()["detail"]


# ---------- 列表 ----------
def test_list_filter_sort_page(app_db):
    c, db, _ = app_db
    bank = WordBank(db)
    for i in range(3):
        bank.submit("person", "热门人物", picked=True)
    d = c.get("/api/admin/words", params={"element": "person", "sort": "use"}, headers=H).json()
    assert d["total"] == 16 and d["items"][0]["text"] == "热门人物"
    d = c.get("/api/admin/words", params={"q": "老王"}, headers=H).json()
    assert [w["text"] for w in d["items"]] == ["隔壁老王"]
    d = c.get("/api/admin/words", params={"page": 2}, headers=H).json()
    assert d["page"] == 2 and len(d["items"]) == 61 - 50
    assert c.get("/api/admin/words", params={"element": "x"}, headers=H).status_code == 400


def test_sentences_listing(app_db):
    c, db, _ = app_db
    db.insert_sentence("1234", 1, "春天，老王在食堂跳舞。", round_no=1,
                       answers={"time": {"text": "春天", "by": "甲"}}, meme=None, auto_filled=["time"])
    d = c.get("/api/admin/sentences", headers=H).json()
    assert d["total"] == 1 and d["items"][0]["answers"]["time"]["by"] == "甲"
    assert d["items"][0]["auto_filled"] == ["time"]
    assert c.get("/api/admin/sentences", params={"q": "火星"}, headers=H).json()["total"] == 0


# ---------- 日志 ----------
def test_admin_writes_logged_without_token(app_db, caplog):
    c, _, _ = app_db
    with caplog.at_level(logging.INFO, "tppe"):
        w = find(c, "除夕夜")
        c.post(f"/api/admin/words/{w['id']}/hide", headers=H)
        c.post("/api/admin/words", json={"element": "time", "text": "日志测试"}, headers=H)
        c.get("/api/admin/stats", headers={"X-Admin-Token": "wrong"})
    text = "\n".join(f"[{r.name}] {r.getMessage()}" for r in caplog.records)
    assert "[tppe.admin]" in text and "删除（隐藏）词" in text and "新增词" in text and "口令错误" in text
    assert TOKEN not in text and "wrong" not in text


# ---------- 迁移 ----------
def test_migrate_v3_to_v4_keeps_data(tmp_path):
    path = tmp_path / "v3.db"
    d = Database(path)
    d.init_db({"time": ["旧词"]})
    with sqlite3.connect(path) as conn:  # 退回 v3 结构：去掉 v4 的列
        conn.execute("DROP INDEX IF EXISTS idx_words_element_hidden")
        for col in ("hidden", "hidden_at", "added_by_admin"):
            conn.execute(f"ALTER TABLE words DROP COLUMN {col}")
        conn.execute("PRAGMA user_version = 3")
    Database(path).init_db()
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("SELECT text, hidden FROM words").fetchall() == [("旧词", 0)]
