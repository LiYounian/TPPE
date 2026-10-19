"""锁住模板规则：每条四占位各一次、拼句正确、挖空片段不泄露答案、抽取避开最近用过的。"""

import random

import pytest

from tppe import config, templates
from tppe.templates import Template


def test_all_templates_valid_and_plenty():
    ts = templates.load()
    assert len(ts) >= 60
    assert len({t.text for t in ts}) == len(ts)


@pytest.mark.parametrize("bad", ["{时间}{地点}{人物}", "{时间}{地点}{人物}{事件}{事件}", "{时间}{地点}{人物}{天气}"])
def test_invalid_template_rejected(bad):
    with pytest.raises(ValueError):
        templates.check(bad)


def test_fill():
    t = Template(1, "{时间}，{人物}在{地点}{事件}。")
    s = t.fill({"time": "春天", "person": "老王", "place": "食堂", "event": "跳舞"})
    assert s == "春天，老王在食堂跳舞。"


def test_fill_missing_element_raises():
    with pytest.raises(ValueError):
        Template(1, "{时间}{人物}{地点}{事件}").fill({"time": "a"})


def test_segments_cover_each_slot_once():
    for t in templates.load():
        slots = [s["slot"] for s in t.segments() if "slot" in s]
        assert sorted(slots) == sorted(config.ELEMENTS)


def test_pick_avoids_recent():
    ts = templates.load()
    recent = [t.id for t in ts[:-1]]
    rng = random.Random(0)
    for _ in range(10):
        assert templates.pick(ts, recent, rng).id == ts[-1].id


def test_pick_when_all_recent_still_returns():
    ts = templates.load()[:3]
    assert templates.pick(ts, [t.id for t in ts]) in ts
