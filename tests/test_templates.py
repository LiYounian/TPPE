"""锁住模板规则：每条四占位各一次、拼句正确、挖空片段不泄露答案、抽取避开最近用过的。"""

import random

import pytest

from tppe import config, templates
from tppe.templates import Template


def test_all_templates_valid_and_plenty():
    ts = templates.load()
    assert len(ts) >= 60
    assert len({t.text for t in ts}) == len(ts)


@pytest.mark.parametrize("bad", ["一个空都没有", "{时间}{地点}{人物}{事件}{事件}", "{人物}和{人物}", "{时间}{地点}{人物}{天气}"])
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


@pytest.mark.parametrize("ok", ["{人物}真是个显眼包。", "{时间}，{人物}治好了我的精神内耗。", "{地点}{人物}{事件}"])
def test_fewer_than_four_slots_is_valid(ok):
    templates.check(ok)


def test_segments_match_elements_and_slot_count():
    for t in templates.load():
        slots = [s["slot"] for s in t.segments() if "slot" in s]
        assert slots == t.elements
        assert 1 <= t.slots <= config.MAX_SLOTS and len(set(slots)) == len(slots)


def test_enough_four_slot_templates_for_big_rooms():
    """4 人及以上只能抽 4 个空的模板，数量要够，否则人多时翻来覆去就那几句。"""
    ts = templates.load()
    assert sum(1 for t in ts if t.slots == 4) >= 60
    assert all(sum(1 for t in ts if t.slots == n) > 0 for n in (1, 2, 3))


def test_fill_only_needs_template_elements():
    t = Template(1, "{时间}，{人物}治好了我的精神内耗。")
    assert t.elements == ["time", "person"]
    assert t.fill({"time": "今天", "person": "二舅"}) == "今天，二舅治好了我的精神内耗。"


@pytest.mark.parametrize("need", [1, 2, 3, 4])
def test_pick_respects_min_slots(need):
    ts = templates.load()
    rng = random.Random(need)
    for _ in range(200):
        assert templates.pick(ts, [], rng, min_slots=need).slots >= need


def test_pick_falls_back_to_largest_when_none_eligible():
    ts = [Template(1, "{人物}"), Template(2, "{人物}{地点}")]
    assert templates.pick(ts, [], random.Random(0), min_slots=4).id == 2


def test_pick_avoids_recent():
    ts = templates.load()
    recent = [t.id for t in ts[:-1]]
    rng = random.Random(0)
    for _ in range(10):
        assert templates.pick(ts, recent, rng).id == ts[-1].id


def test_pick_when_all_recent_still_returns():
    ts = templates.load()[:3]
    assert templates.pick(ts, [t.id for t in ts]) in ts
