"""SQLite 访问层：词库 words + 历史句子 sentences。

每次操作开一个短连接，避免跨线程共享连接；数据量小，开销可忽略。
"""

import logging
import sqlite3
from pathlib import Path
from typing import Iterable

from tppe import config

log = logging.getLogger("tppe.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS words (
  id INTEGER PRIMARY KEY,
  element TEXT NOT NULL CHECK (element IN ('time','place','person','event')),
  text TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'player' CHECK (source IN ('seed','player')),
  use_count INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE (element, text)
);
CREATE TABLE IF NOT EXISTS sentences (
  id INTEGER PRIMARY KEY,
  room_code TEXT,
  template_id INTEGER,
  sentence TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""


class Database:
    def __init__(self, path: Path | str = config.DB_PATH):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self, seed: dict[str, Iterable[str]] | None = None) -> None:
        """建表（幂等）；给了 seed 就用 INSERT OR IGNORE 写入预置词。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            if seed:
                rows = [(el, w) for el, words in seed.items() for w in words]
                cur = conn.executemany(
                    "INSERT OR IGNORE INTO words (element, text, source, use_count) "
                    "VALUES (?, ?, 'seed', 0)", rows)
                log.info("预置词写入 %d 条（已存在的跳过）", cur.rowcount)
        log.info("数据库就绪: %s", self.path)

    def insert_word(self, element: str, text: str) -> bool:
        """玩家词入库。新词返回 True；已存在则 use_count+1 并返回 False。"""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO words (element, text) VALUES (?, ?)", (element, text))
            if cur.rowcount:
                return True
            conn.execute("UPDATE words SET use_count = use_count + 1 "
                         "WHERE element = ? AND text = ?", (element, text))
            return False

    def random_words(self, element: str, k: int, exclude: Iterable[str] = ()) -> list[str]:
        """从某元素词库随机取至多 k 个不重复的词；库不足时有几个返回几个。"""
        exclude = list(exclude)
        sql = "SELECT text FROM words WHERE element = ?"
        if exclude:
            sql += f" AND text NOT IN ({','.join('?' * len(exclude))})"
        sql += " ORDER BY RANDOM() LIMIT ?"
        with self._connect() as conn:
            return [r["text"] for r in conn.execute(sql, (element, *exclude, k))]

    def count_words(self, element: str | None = None) -> int:
        with self._connect() as conn:
            if element:
                return conn.execute("SELECT COUNT(*) FROM words WHERE element = ?",
                                    (element,)).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM words").fetchone()[0]

    def insert_sentence(self, room_code: str, template_id: int, sentence: str) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sentences (room_code, template_id, sentence) VALUES (?, ?, ?)",
                (room_code, template_id, sentence))
            return cur.lastrowid
