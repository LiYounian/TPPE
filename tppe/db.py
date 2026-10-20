"""SQLite 访问层：词库 words + 历史句子 sentences。

每次操作开一个短连接，避免跨线程共享连接；数据量小，开销可忽略。
表结构版本记在 PRAGMA user_version，init_db() 启动时按版本顺序做增量迁移（只加列，不丢数据）。
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Iterable

from tppe import config

log = logging.getLogger("tppe.db")

SCHEMA_VERSION = 2

_SCHEMA_V1 = """
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

# v2：词被候选点选的次数 / 最后使用时间；句子记录各元素答案与填写人、所用热梗、轮次
_MIGRATE_V2 = [
    "ALTER TABLE words ADD COLUMN pick_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE words ADD COLUMN last_used_at TEXT",
    "ALTER TABLE sentences ADD COLUMN round_no INTEGER",
    "ALTER TABLE sentences ADD COLUMN answers TEXT",   # JSON: {element: {"text":..., "by":...}}
    "ALTER TABLE sentences ADD COLUMN meme TEXT",      # 热梗名，普通模板为 NULL
    "CREATE INDEX IF NOT EXISTS idx_sentences_created ON sentences(created_at)",
]


class Database:
    def __init__(self, path: Path | str = config.DB_PATH):
        self.path = Path(path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self, seed: dict[str, Iterable[str]] | None = None) -> None:
        """建表 + 迁移到最新版本（幂等）；给了 seed 就用 INSERT OR IGNORE 写入预置词。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._connect() as conn:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA_V1)
                ver = conn.execute("PRAGMA user_version").fetchone()[0]
                if ver < 2:
                    for sql in _MIGRATE_V2:
                        try:
                            conn.execute(sql)
                        except sqlite3.OperationalError as e:  # 列已存在等：迁移可重入
                            log.debug("迁移语句跳过（%s）: %s", e, sql)
                    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                    log.info("数据库结构迁移 v%d → v%d", ver, SCHEMA_VERSION)
                if seed:
                    rows = [(el, w) for el, words in seed.items() for w in words]
                    before = conn.total_changes
                    conn.executemany(
                        "INSERT OR IGNORE INTO words (element, text, source, use_count) "
                        "VALUES (?, ?, 'seed', 0)", rows)
                    log.info("预置词：共 %d 条，新写入 %d 条（已存在的跳过）",
                             len(rows), conn.total_changes - before)
        except sqlite3.Error:
            log.exception("数据库初始化失败: %s", self.path)
            raise
        log.info("数据库就绪: %s（schema v%d）", self.path.resolve(), SCHEMA_VERSION)

    def insert_word(self, element: str, text: str, picked: bool = False) -> bool:
        """玩家词入库。新词返回 True；已存在则 use_count+1（点选的再 pick_count+1）并返回 False。"""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO words (element, text, last_used_at) "
                "VALUES (?, ?, datetime('now','localtime'))", (element, text))
            if cur.rowcount:
                return True
            conn.execute("UPDATE words SET use_count = use_count + 1, pick_count = pick_count + ?, "
                         "last_used_at = datetime('now','localtime') WHERE element = ? AND text = ?",
                         (1 if picked else 0, element, text))
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

    def word_stats(self) -> dict[str, int]:
        """各元素词数，启动日志用。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT element, COUNT(*) FROM words GROUP BY element").fetchall()
        return {el: n for el, n in rows}

    def insert_sentence(self, room_code: str, template_id: int, sentence: str,
                        round_no: int | None = None, answers: dict | None = None,
                        meme: str | None = None) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sentences (room_code, template_id, sentence, round_no, answers, meme) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (room_code, template_id, sentence, round_no,
                 json.dumps(answers, ensure_ascii=False) if answers else None, meme))
            return cur.lastrowid
