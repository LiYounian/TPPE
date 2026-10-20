"""短句模板：加载、校验、随机抽取、挖空展示、拼句。

templates.json 是对象数组，每条：
  {"id": 整数且全局唯一, "text": "含 1–4 个不同元素占位（{时间}{地点}{人物}{事件}，每个最多一次）的句子",
   "meme": null 或 {"name": 梗名, "source": 出处（和什么有关）, "date": "YYYY[-MM[-DD]]"}}
id 一经发布不要改动或复用（历史句子表按 id 关联）；新模板用新 id 追加。
"""

import json
import logging
import random
import re
from dataclasses import dataclass

from tppe import config

log = logging.getLogger("tppe.templates")

TEMPLATE_PATH = config.PKG_DIR / "templates.json"
_PH_RE = re.compile(r"\{([^{}]+)\}")
_DATE_RE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_NAME_TO_KEY = {v: k for k, v in config.ELEMENTS.items()}


@dataclass(frozen=True)
class Template:
    id: int
    text: str
    meme: dict | None = None

    def segments(self) -> list[dict]:
        """把模板拆成片段，前端用来渲染挖空句：[{"text": "..."}, {"slot": "time"}, ...]"""
        out, pos = [], 0
        for m in _PH_RE.finditer(self.text):
            if m.start() > pos:
                out.append({"text": self.text[pos:m.start()]})
            out.append({"slot": _NAME_TO_KEY[m.group(1)]})
            pos = m.end()
        if pos < len(self.text):
            out.append({"text": self.text[pos:]})
        return out

    @property
    def elements(self) -> list[str]:
        """模板含有的元素（按在句中出现的顺序），如 ["time", "person"]。"""
        return [_NAME_TO_KEY[n] for n in _PH_RE.findall(self.text)]

    @property
    def slots(self) -> int:
        return len(self.elements)

    def fill(self, answers: dict[str, str]) -> str:
        missing = set(self.elements) - set(answers)
        if missing:
            raise ValueError(f"缺少元素: {missing}")
        return _PH_RE.sub(lambda m: answers[_NAME_TO_KEY[m.group(1)]], self.text)


def check(text: str) -> None:
    """模板含 1–4 个占位，只能是四个元素之一，且同一元素最多出现一次。"""
    names = _PH_RE.findall(text)
    unknown = set(names) - set(_NAME_TO_KEY)
    if unknown:
        raise ValueError(f"模板含未知占位 {unknown}: {text}")
    if not names:
        raise ValueError(f"模板至少要有一个空: {text}")
    for name in set(names):
        if names.count(name) > 1:
            raise ValueError(f"模板中 {{{name}}} 出现 {names.count(name)} 次（每个元素最多一次）: {text}")


def check_meme(meme: dict | None, tid: int) -> None:
    if meme is None:
        return
    for k in ("name", "source", "date"):
        if not isinstance(meme.get(k), str) or not meme[k].strip():
            raise ValueError(f"模板 #{tid} 的 meme 缺少字段 {k}")
    if not _DATE_RE.match(meme["date"]):
        raise ValueError(f"模板 #{tid} 的 meme.date 格式应为 YYYY[-MM[-DD]]: {meme['date']}")


def load(path=TEMPLATE_PATH) -> list[Template]:
    items = json.loads(path.read_text(encoding="utf-8"))
    out, seen = [], set()
    for it in items:
        tid = it.get("id")
        if not isinstance(tid, int) or tid <= 0 or tid in seen:
            raise ValueError(f"模板 id 非法或重复: {tid}")
        seen.add(tid)
        check(it["text"])
        check_meme(it.get("meme"), tid)
        out.append(Template(tid, it["text"], it.get("meme")))
    log.debug("加载模板 %d 条", len(out))
    return out


def pick(templates: list[Template], recent: list[int] = (), rng=random, min_slots: int = 1) -> Template:
    """随机抽一条空位数 ≥ min_slots 的模板（保证每个玩家至少一个空），尽量避开最近用过的。
    没有够格的模板时退回空位最多的那些；够格的都用过了就放开"避开最近"的限制。"""
    eligible = [t for t in templates if t.slots >= min_slots]
    if not eligible:
        most = max(t.slots for t in templates)
        eligible = [t for t in templates if t.slots == most]
    pool = [t for t in eligible if t.id not in set(recent)] or eligible
    return rng.choice(pool)
