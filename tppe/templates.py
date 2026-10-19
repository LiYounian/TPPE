"""短句模板：加载、校验、随机抽取、挖空展示、拼句。

模板 id = 在 templates.json 里的下标 + 1，加新模板只往末尾追加，保证旧 id 不变。
"""

import json
import random
import re
from dataclasses import dataclass

from tppe import config

TEMPLATE_PATH = config.PKG_DIR / "templates.json"
_PH_RE = re.compile(r"\{([^{}]+)\}")
_NAME_TO_KEY = {v: k for k, v in config.ELEMENTS.items()}


@dataclass(frozen=True)
class Template:
    id: int
    text: str

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

    def fill(self, answers: dict[str, str]) -> str:
        missing = set(config.ELEMENTS) - set(answers)
        if missing:
            raise ValueError(f"缺少元素: {missing}")
        return _PH_RE.sub(lambda m: answers[_NAME_TO_KEY[m.group(1)]], self.text)


def check(text: str) -> None:
    """每条模板必须四个占位各恰好出现一次、且没有未知占位。"""
    names = _PH_RE.findall(text)
    unknown = set(names) - set(_NAME_TO_KEY)
    if unknown:
        raise ValueError(f"模板含未知占位 {unknown}: {text}")
    for name in _NAME_TO_KEY:
        if names.count(name) != 1:
            raise ValueError(f"模板中 {{{name}}} 出现 {names.count(name)} 次: {text}")


def load(path=TEMPLATE_PATH) -> list[Template]:
    texts = json.loads(path.read_text(encoding="utf-8"))
    for t in texts:
        check(t)
    return [Template(i + 1, t) for i, t in enumerate(texts)]


def pick(templates: list[Template], recent: list[int] = (), rng=random) -> Template:
    """随机抽一条，尽量避开最近用过的；全用过就放开限制。"""
    pool = [t for t in templates if t.id not in set(recent)] or templates
    return rng.choice(pool)
