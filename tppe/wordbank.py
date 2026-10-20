"""词库业务：输入规范化、校验、入库、随机候选。"""

import json
import logging
import re

from tppe import config
from tppe.db import Database

log = logging.getLogger("tppe.wordbank")

SEED_PATH = config.PKG_DIR / "seed_words.json"


def normalize(text: str) -> str:
    """去首尾空白、把连续空白（含换行）合并为一个空格。"""
    return re.sub(r"\s+", " ", text or "").strip()


def validate(text: str) -> tuple[bool, str]:
    """校验已规范化的词。返回 (是否合法, 不合法原因)。"""
    if len(text) < config.WORD_MIN_LEN:
        return False, "不能为空"
    if len(text) > config.WORD_MAX_LEN:
        return False, f"不能超过 {config.WORD_MAX_LEN} 个字符（当前 {len(text)} 个）"
    return True, ""


def load_seed() -> dict[str, list[str]]:
    data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
    for el, words in data.items():
        if el not in config.ELEMENTS:
            raise ValueError(f"seed_words.json 含未知元素: {el}")
        for w in words:
            ok, why = validate(normalize(w))
            if not ok:
                raise ValueError(f"预置词不合法 [{el}] {w!r}: {why}")
    return data


class WordBank:
    def __init__(self, db: Database):
        self.db = db

    def submit(self, element: str, raw: str, picked: bool = False, author: str | None = None) -> str:
        """校验并入库，返回规范化后的词；不合法抛 ValueError（消息可直接给玩家看）。
        picked=True 表示玩家点的是候选词（只用于统计 pick_count 和日志）。"""
        if element not in config.ELEMENTS:
            raise ValueError(f"未知元素: {element}")
        text = normalize(raw)
        ok, why = validate(text)
        if not ok:
            log.info("词被拒 [%s] %r: %s", element, raw[:60], why)
            raise ValueError(why)
        is_new = self.db.insert_word(element, text, picked=picked, author=author)
        log.info("词入库 [%s] %r by %s（%s，%s）", element, text, author, "新词" if is_new else "已有",
                 "点选候选" if picked else "手动输入")
        return text

    def suggest(self, element: str, exclude=()) -> list[dict]:
        """至多 SUGGEST_COUNT 个随机候选 [{"text", "by"}]，空库返回 []。"""
        words = self.db.random_words(element, config.SUGGEST_COUNT, exclude)
        log.debug("候选 [%s] %s", element, words)
        return words
