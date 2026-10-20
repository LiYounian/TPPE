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

SCHEMA_VERSION = 3

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

# v3：词的最早贡献者昵称（预置词为 NULL）；句子的得票（JSON {element: 票数}）与超时自动填的元素
_MIGRATE_V3 = [
    "ALTER TABLE words ADD COLUMN author TEXT",
    "ALTER TABLE sentences ADD COLUMN votes TEXT",
    "ALTER TABLE sentences ADD COLUMN auto_filled TEXT",  # JSON 数组: ["time", ...]
]
_MIGRATIONS = {2: _MIGRATE_V2, 3: _MIGRATE_V3}


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
                for target in range(ver + 1, SCHEMA_VERSION + 1):
                    for sql in _MIGRATIONS.get(target, []):
                        try:
                            conn.execute(sql)
                        except sqlite3.OperationalError as e:  # 列已存在等：迁移可重入
                            log.debug("迁移语句跳过（%s）: %s", e, sql)
                    conn.execute(f"PRAGMA user_version = {target}")
                    log.info("数据库结构迁移 → v%d", target)
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

    def insert_word(self, element: str, text: str, picked: bool = False, author: str | None = None) -> bool:
        """玩家词入库。新词返回 True（记下贡献者 author）；已存在则 use_count+1（点选的再 pick_count+1）并返回 False。"""
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO words (element, text, author, last_used_at) "
                "VALUES (?, ?, ?, datetime('now','localtime'))", (element, text, author))
            if cur.rowcount:
                return True
            conn.execute("UPDATE words SET use_count = use_count + 1, pick_count = pick_count + ?, "
                         "last_used_at = datetime('now','localtime') WHERE element = ? AND text = ?",
                         (1 if picked else 0, element, text))
            return False

    def random_words(self, element: str, k: int, exclude: Iterable[str] = ()) -> list[dict]:
        """从某元素词库随机取至多 k 个不重复的词 [{"text", "by"}]，by 为贡献者（预置词为 None）。"""
        exclude = list(exclude)
        sql = "SELECT text, author FROM words WHERE element = ?"
        if exclude:
            sql += f" AND text NOT IN ({','.join('?' * len(exclude))})"
        sql += " ORDER BY RANDOM() LIMIT ?"
        with self._connect() as conn:
            return [{"text": r["text"], "by": r["author"]} for r in conn.execute(sql, (element, *exclude, k))]

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
                        meme: str | None = None, auto_filled: list[str] | None = None) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO sentences (room_code, template_id, sentence, round_no, answers, meme, auto_filled) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (room_code, template_id, sentence, round_no,
                 json.dumps(answers, ensure_ascii=False) if answers else None, meme,
                 json.dumps(auto_filled) if auto_filled else None))
            return cur.lastrowid

    def set_sentence_votes(self, sentence_id: int, votes: dict[str, int]) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE sentences SET votes = ? WHERE id = ?",
                         (json.dumps(votes, ensure_ascii=False), sentence_id))
