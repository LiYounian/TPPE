"""锁住回合规则：分配与轮换、掉线改派、答案揭晓前不外泄、30 秒超时自动选词、朗读后 20 秒投票、
每人每轮 1 票可改票、不能投自己、全员投完提前结束、每票 +1 分、昵称 ≤10 字符。"""

import random
from collections import Counter

import pytest

from tppe import config, templates
from tppe.room import FILLING, RESULT, REVEAL, VOTING, WAITING, GameError, RoomManager, Timing


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def mgr(db, bank, clock):
    return RoomManager(bank, db, templates.load(), rng=random.Random(42), clock=clock)


def fill_all(room):
    """让每个被分配的人把自己的空填完。"""
    while room.phase == FILLING:
        for pid in list(room.players):
            t = room.task_for(pid)
            if t["element"]:
                room.submit(pid, t["element"], f"{room.players[pid].name}的{t['label']}")


def to_result(room, clock):
    """填完 → 朗读结束 → 投票超时 → 结算。"""
    fill_all(room)
    clock.advance(config.READ_MAX_SECONDS + 0.1)
    room.tick()
    clock.advance(config.VOTE_SECONDS + 0.1)
    room.tick()
    assert room.phase == RESULT


def make_room(mgr, n):
    room = mgr.create()
    ps = [room.join(f"玩家{i}") for i in range(n)]
    return room, ps


# ---------- 分配 ----------
@pytest.mark.parametrize("n", [1, 2, 3, 4, 5, 8])
def test_slots_ge_players_and_everyone_gets_one(mgr, clock, n):
    """空位数 ≥ min(人数, 4)：≤4 人时每人至少一个空；≥4 人时只抽 4 个空的模板，选 4 人各填一个。"""
    room, ps = make_room(mgr, n)
    room.start(ps[0].id)
    for _ in range(25):
        t = room.template
        assert t.slots >= min(n, config.MAX_SLOTS)
        assert set(room.assignments) == set(t.elements)       # 只分配模板里有的元素
        per_player = Counter(room.assignments.values())
        if n <= config.MAX_SLOTS:
            assert set(per_player) == set(room.players)        # 每人至少一个
        else:
            assert t.slots == 4 and len(per_player) == 4 and set(per_player.values()) == {1}
        to_result(room, clock)
        room.next_round(ps[0].id)


def test_partial_template_reveals_and_archives_only_its_elements(db, bank, clock):
    import json
    from tppe.templates import Template
    t = Template(9, "{时间}，{人物}治好了我的精神内耗。", {"name": "二舅", "source": "测试", "date": "2022-07"})
    room = RoomManager(bank, db, [t], rng=random.Random(1), clock=clock).create()
    a, b = room.join("甲"), room.join("乙")
    room.start(a.id)
    assert set(room.suggestions) == {"time", "person"}
    assert room.task_for(a.id)["element"] and room.task_for(b.id)["element"]  # 两人各一个
    fill_all(room)
    assert room.phase == REVEAL
    assert room.sentence.endswith("治好了我的精神内耗。")
    assert set(room.public_state()["slots"]) == {"time", "person"}
    with db._connect() as c:
        assert set(json.loads(c.execute("SELECT answers FROM sentences").fetchone()[0])) == {"time", "person"}
    clock.advance(config.READ_MAX_SECONDS)
    room.tick()
    room.vote(a.id, next(el for el, by in room.answered_by.items() if by != a.id))
    room.vote(b.id, next(el for el, by in room.answered_by.items() if by != b.id))
    assert room.phase == RESULT and room.scores[a.id] == room.scores[b.id] == 1


def test_one_slot_template_solo_and_timeout(db, bank, clock):
    from tppe.templates import Template
    room = RoomManager(bank, db, [Template(5, "{人物}真是个显眼包。")], clock=clock).create()
    a = room.join("独行")
    room.start(a.id)
    clock.advance(config.FILL_SECONDS)
    room.tick()                                   # 唯一的空超时 → 自动选词 → 揭晓
    assert room.phase == REVEAL and room.sentence.endswith("真是个显眼包。")
    assert room.auto_filled == {"person"}


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


def test_spectators_get_priority_next_round(mgr, clock):
    room, ps = make_room(mgr, 6)
    room.start(ps[0].id)
    benched = set(room.players) - set(room.assignments.values())
    assert len(benched) == 2
    to_result(room, clock)
    room.next_round(ps[0].id)
    assert benched <= set(room.assignments.values())


def test_answers_hidden_until_reveal(mgr):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    owner = room.assignments["time"]
    room.submit(owner, "time", "绝密答案")
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


def test_suggestions_at_most_3_with_author(mgr, bank):
    for w in ["甲", "乙", "丙", "丁", "戊"]:
        bank.submit("place", w, author="老王")
    room, ps = make_room(mgr, 1)
    room.start(ps[0].id)
    assert len(room.suggestions["place"]) == 3
    assert all(w["by"] == "老王" for w in room.suggestions["place"])
    assert room.suggestions["time"] == []  # 空库：只能手填


def test_typed_word_appears_as_candidate_with_author(db, bank, clock):
    from tppe.templates import Template
    mgr = RoomManager(bank, db, [Template(1, "{时间}{地点}{人物}{事件}")], clock=clock)
    room, (a,) = make_room(mgr, 1)
    room.start(a.id)
    to_result(room, clock)
    room.next_round(a.id)
    assert room.suggestions["time"] == [{"text": "玩家0的时间", "by": "玩家0"}]


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


def test_join_mid_round_spectates_then_plays(mgr, clock):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    late = room.join("迟到")
    assert room.task_for(late.id)["element"] is None
    to_result(room, clock)
    room.next_round(late.id)
    assert late.id in room.assignments.values()


def test_phase_guards(mgr, clock):
    room, (a,) = make_room(mgr, 1)
    with pytest.raises(GameError):
        room.next_round(a.id)
    room.start(a.id)
    with pytest.raises(GameError):
        room.start(a.id)
    with pytest.raises(GameError):
        room.next_round(a.id)
    fill_all(room)
    with pytest.raises(GameError):
        room.next_round(a.id)  # 朗读中也不能跳


def test_many_rounds_vary_templates(mgr, clock):
    room, (a, b) = make_room(mgr, 2)
    room.start(a.id)
    ids = []
    for _ in range(10):
        ids.append(room.template.id)
        to_result(room, clock)
        room.next_round(b.id)
    assert len(set(ids)) == 10


# ---------- 昵称 ----------
def test_nickname_max_10_chars(mgr):
    room = mgr.create()
    assert room.join("一二三四五六七八九十十一").name == "一二三四五六七八九十"
    assert room.join("小明").name == "小明"
    assert room.join("小明").name == "小明2"
    assert len(room.join("一二三四五六七八九十").name) <= config.NAME_MAX_LEN
    assert room.join("   ").name == "无名氏"


def test_empty_room_discarded(mgr):
    room, (a,) = make_room(mgr, 1)
    room.leave(a.id)
    mgr.discard_if_empty(room.code)
    assert mgr.get(room.code) is None
    assert room.phase == WAITING


# ---------- 30 秒倒计时 ----------
def test_each_current_slot_has_30s_deadline(mgr, clock):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    for pid in room.players:
        assert room.task_for(pid)["time_left"] == config.FILL_SECONDS
    clock.advance(10)
    assert room.task_for(ps[0].id)["time_left"] == config.FILL_SECONDS - 10


def test_timeout_picks_random_candidate(mgr, bank, clock):
    for w in ["甲地", "乙地", "丙地"]:
        bank.submit("place", w, author="老王")
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    shown = {w["text"] for w in room.suggestions["place"]}
    clock.advance(config.FILL_SECONDS - 0.1)
    assert not room.tick() or "place" not in room.answers
    clock.advance(0.2)
    assert room.tick()
    assert room.answers["place"] in shown
    assert "place" in room.auto_filled
    assert room.phase == REVEAL  # 4 个空同时超时 → 全部自动填 → 揭晓


def test_timeout_with_empty_bank_uses_fallback(mgr, clock):
    room, (a,) = make_room(mgr, 1)
    room.start(a.id)
    first = room.task_for(a.id)["element"]
    clock.advance(config.FILL_SECONDS)
    room.tick()
    assert room.answers[first] == config.FALLBACK_WORDS[first]


def test_next_slot_gets_fresh_30s_after_previous(mgr, clock):
    room, (a,) = make_room(mgr, 1)  # 一人四空，依次计时
    room.start(a.id)
    clock.advance(25)
    t = room.task_for(a.id)
    room.submit(a.id, t["element"], "x")
    assert room.task_for(a.id)["time_left"] == config.FILL_SECONDS
    clock.advance(25)
    room.tick()
    assert len(room.answers) == 1  # 第二个空还没超时


def test_reassigned_slot_gets_fresh_deadline(mgr, clock):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    clock.advance(20)
    gone = room.assignments["event"]
    room.leave(gone)
    new = room.assignments["event"]
    # 接手的人先填完自己原来的空，接手的空才开始计时，且是完整 30 秒
    own = room.task_for(new)["element"]
    if own != "event":
        room.submit(new, own, "x")
    assert room.task_for(new)["element"] == "event"
    assert room.task_for(new)["time_left"] == config.FILL_SECONDS


# ---------- 朗读与投票 ----------
def test_reading_then_voting_then_result(mgr, clock):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    fill_all(room)
    assert room.phase == REVEAL
    read = room.timing.read_secs(room.sentence)
    assert config.READ_MIN_SECONDS <= read <= config.READ_MAX_SECONDS
    clock.advance(read - 0.1)
    room.tick()
    assert room.phase == REVEAL
    with pytest.raises(GameError):
        room.vote(ps[0].id, "time")  # 朗读期间不能投
    clock.advance(0.2)
    room.tick()
    assert room.phase == VOTING
    assert room.public_state()["vote_remaining"] == config.VOTE_SECONDS
    clock.advance(config.VOTE_SECONDS)
    room.tick()
    assert room.phase == RESULT


def to_voting(room, clock):
    fill_all(room)
    clock.advance(config.READ_MAX_SECONDS)
    room.tick()
    assert room.phase == VOTING


def test_vote_scores_one_point_per_vote(mgr, clock):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    to_voting(room, clock)
    target_el = "event"
    author = room.answered_by[target_el]
    voters = [p for p in room.players if p != author]
    for v in voters:
        room.vote(v, target_el)
    assert room.phase == VOTING  # 作者自己还没投，不提前结束
    other_el = next(el for el, by in room.answered_by.items() if by != author)
    room.vote(author, other_el)
    assert room.phase == RESULT  # 全员投完，提前结束
    assert room.scores[author] == 3
    assert room.scores[room.answered_by[other_el]] == 1
    assert room.public_state()["tally"] == {target_el: 3, other_el: 1}


def test_cannot_vote_own_word(mgr, clock):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    to_voting(room, clock)
    author = room.answered_by["time"]
    with pytest.raises(GameError):
        room.vote(author, "time")


def test_one_vote_per_player_can_change(mgr, clock):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    to_voting(room, clock)
    voter = ps[0].id
    others = [el for el, by in room.answered_by.items() if by != voter]
    room.vote(voter, others[0])
    room.vote(voter, others[1])
    assert room.votes[voter] == others[1]
    assert room.task_for(voter)["my_vote"] == others[1]
    clock.advance(config.VOTE_SECONDS)
    room.tick()
    assert room.tally == {others[1]: 1}
    assert room.scores[room.answered_by[others[1]]] == 1
    assert room.scores[room.answered_by[others[0]]] == 0


def test_all_voted_finishes_early_and_scores_accumulate(mgr, clock, db):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    total = Counter()
    for _ in range(2):
        to_voting(room, clock)
        for v in list(room.players):
            el = next(el for el, by in room.answered_by.items() if by != v)
            room.vote(v, el)
            total[room.answered_by[el]] += 1
        assert room.phase == RESULT
        room.next_round(ps[0].id)
    assert dict(room.scores) == {p: total.get(p, 0) for p in room.players}
    assert sum(room.scores.values()) == 8
    with db._connect() as c:
        assert c.execute("SELECT COUNT(*) FROM sentences WHERE votes IS NOT NULL").fetchone()[0] == 2


def test_solo_room_skips_voting(mgr, clock):
    room, (a,) = make_room(mgr, 1)
    room.start(a.id)
    fill_all(room)
    clock.advance(config.READ_MAX_SECONDS)
    room.tick()
    assert room.phase == RESULT


def test_auto_filled_word_still_votable_credit_to_assignee(mgr, clock):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    lazy = room.assignments["time"]
    for el, pid in room.assignments.items():
        if el != "time":
            room.submit(pid, el, f"{el}词")
    clock.advance(config.FILL_SECONDS)
    room.tick()
    assert "time" in room.auto_filled and room.answered_by["time"] == lazy
    clock.advance(config.READ_MAX_SECONDS)
    room.tick()
    voter = next(p for p in room.players if p != lazy)
    room.vote(voter, "time")
    clock.advance(config.VOTE_SECONDS)
    room.tick()
    assert room.scores[lazy] == 1


def test_voter_leaving_mid_vote_can_finish_early(mgr, clock):
    room, ps = make_room(mgr, 4)
    room.start(ps[0].id)
    to_voting(room, clock)
    ids = list(room.players)
    for v in ids[:3]:
        room.vote(v, next(el for el, by in room.answered_by.items() if by != v))
    assert room.phase == VOTING
    room.leave(ids[3])  # 最后一个没投的人走了 → 剩下的都投完了
    assert room.phase == RESULT


def test_timing_injection():
    t = Timing(fill=1, vote=1, read_min=0, read_max=0)
    assert t.read_secs("很长的句子" * 20) == 0
