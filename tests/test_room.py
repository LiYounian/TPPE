"""锁住回合规则：四空各分配一次、人数不足/超员的分配、旁观者轮换、掉线改派、答案揭晓前不外泄、多轮循环、入库。"""

import random
from collections import Counter

import pytest

from tppe import config, templates
from tppe.room import FILLING, REVEAL, WAITING, GameError, RoomManager


@pytest.fixture
def mgr(db, bank):
    return RoomManager(bank, db, templates.load(), rng=random.Random(42))


def fill_all(room):
    """让每个被分配的人把自己的空填完。"""
    while room.phase == FILLING:
        for pid in list(room.players):
            t = room.task_for(pid)
            if t["element"]:
                room.submit(pid, t["element"], f"{room.players[pid].name}的{t['label']}")


def make_room(mgr, n):
    room = mgr.create()
    ps = [room.join(f"玩家{i}") for i in range(n)]
    return room, ps


@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 8])
def test_each_slot_assigned_exactly_once(mgr, n):
    room, ps = make_room(mgr, n)
    room.start(ps[0].id)
    assert set(room.assignments) == set(config.ELEMENTS)
    per_player = Counter(room.assignments.values())
    if n >= 4:
        assert len(per_player) == 4 and set(per_player.values()) == {1}
    else:
        assert len(per_player) == n  # 每人至少一个


def test_fewer_than_4_players_fill_multiple_in_turn(mgr):
    room, (a,) = make_room(mgr, 1)
    room.start(a.id)
    seen = []
    while room.phase == FILLING:
        t = room.task_for(a.id)
        seen.append(t["element"])
        room.submit(a.id, t["element"], "x")
    assert sorted(seen) == sorted(config.ELEMENTS)
    assert room.phase == REVEAL


def test_spectators_get_priority_next_round(mgr):
    room, ps = make_room(mgr, 6)
    room.start(ps[0].id)
    benched = set(room.players) - set(room.assignments.values())
    assert len(benched) == 2
    fill_all(room)
    room.next_round(ps[0].id)
    assert benched <= set(room.assignments.values())


def test_answers_hidden_until_reveal(mgr):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    el = room.task_for(room.assignments["time"])["element"]
    room.submit(room.assignments["time"], el, "绝密答案")
    st = room.public_state()
    assert "绝密答案" not in repr(st)
    assert st["slots"]["time"]["filled"] is True
    for pid in room.players:
        assert "绝密答案" not in repr(room.task_for(pid))


def test_reveal_builds_sentence_and_stores_everything(mgr, db):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    fill_all(room)
    st = room.public_state()
    assert st["phase"] == REVEAL
    assert st["sentence"] == room.template.fill(room.answers)
    assert db.count_words() == 4
    with db._connect() as c:
        assert c.execute("SELECT sentence FROM sentences").fetchone()[0] == st["sentence"]


def test_submit_rules(mgr):
    room, (a, b, c, d) = make_room(mgr, 4)
    with pytest.raises(GameError):
        room.submit(a.id, "time", "x")  # 还没开始
    room.start(a.id)
    owner = room.assignments["time"]
    other = next(p for p in room.players if p != owner)
    with pytest.raises(GameError):
        room.submit(other, "time", "x")  # 不是你的
    with pytest.raises(GameError):
        room.submit(owner, "time", "x" * 21)  # 超长
    room.submit(owner, "time", "春天")
    with pytest.raises(GameError):
        room.submit(owner, "time", "夏天")  # 已填


def test_suggestions_at_most_3_and_come_from_bank(mgr, bank):
    for w in ["甲", "乙", "丙", "丁", "戊"]:
        bank.submit("place", w)
    room, ps = make_room(mgr, 1)
    room.start(ps[0].id)
    assert len(room.suggestions["place"]) == 3
    assert room.suggestions["time"] == []  # 空库：只能手填


def test_typed_word_appears_as_candidate_later(mgr, bank):
    room, (a,) = make_room(mgr, 1)
    room.start(a.id)
    fill_all(room)
    room.next_round(a.id)
    assert room.suggestions["time"] == ["玩家0的时间"]


def test_disconnect_reassigns_unfilled_slots(mgr):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    gone = room.assignments["event"]
    room.leave(gone)
    assert room.assignments["event"] in room.players
    fill_all(room)
    assert room.phase == REVEAL


def test_disconnect_keeps_already_filled_answer(mgr):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    gone = room.assignments["time"]
    room.submit(gone, "time", "早上")
    room.leave(gone)
    fill_all(room)
    assert room.answers["time"] == "早上"


def test_join_mid_round_spectates_then_plays(mgr):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    late = room.join("迟到")
    assert room.task_for(late.id)["element"] is None
    fill_all(room)
    room.next_round(late.id)
    assert late.id in room.assignments.values()


def test_phase_guards(mgr):
    room, (a,) = make_room(mgr, 1)
    with pytest.raises(GameError):
        room.next_round(a.id)
    room.start(a.id)
    with pytest.raises(GameError):
        room.start(a.id)
    with pytest.raises(GameError):
        room.next_round(a.id)


def test_many_rounds_vary_templates(mgr):
    room, (a, b) = make_room(mgr, 2)
    room.start(a.id)
    ids = []
    for _ in range(10):
        ids.append(room.template.id)
        fill_all(room)
        room.next_round(b.id)
    assert len(set(ids)) == 10  # 最近 10 条不重复


def test_duplicate_names_get_suffix(mgr):
    room = mgr.create()
    assert room.join("小明").name == "小明"
    assert room.join("小明").name == "小明2"


def test_empty_room_discarded(mgr):
    room, (a,) = make_room(mgr, 1)
    room.leave(a.id)
    mgr.discard_if_empty(room.code)
    assert mgr.get(room.code) is None
    assert room.phase == WAITING
