"""房间与回合状态机（纯逻辑，不碰网络）。

阶段：waiting（等人开始）
   → filling（每个空 30 秒，超时从候选里随机选）
   → reveal（拼句并朗读，时长按字数估算）
   → voting（20 秒，投别人填的词，每人每轮 1 票、可改票，全员投完提前结束）
   → result（显示得票与累计积分，任何人点「下一轮」）→ filling …
时间相关的推进都在 tick() 里做；server 层定时调用 tick()，状态有变化就广播。
时钟可注入（clock），测试里用假时钟。
"""

import logging
import random
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from tppe import config, templates
from tppe.db import Database
from tppe.templates import Template
from tppe.wordbank import WordBank, normalize

log = logging.getLogger("tppe.room")

WAITING, FILLING, REVEAL, VOTING, RESULT = "waiting", "filling", "reveal", "voting", "result"


class GameError(ValueError):
    """玩家操作不合法（阶段不对、不是你的空位等），消息可直接展示给玩家。"""


@dataclass
class Timing:
    fill: float = config.FILL_SECONDS
    vote: float = config.VOTE_SECONDS
    read_per_char: float = config.READ_SECS_PER_CHAR
    read_min: float = config.READ_MIN_SECONDS
    read_max: float = config.READ_MAX_SECONDS

    def read_secs(self, sentence: str) -> float:
        return min(self.read_max, max(self.read_min, len(sentence) * self.read_per_char))


@dataclass
class Player:
    id: str
    name: str


@dataclass
class Room:
    code: str
    bank: WordBank
    db: Database
    tpls: list[Template]
    rng: random.Random = field(default_factory=random.Random)
    clock: Callable[[], float] = time.monotonic
    timing: Timing = field(default_factory=Timing)

    players: dict[str, Player] = field(default_factory=dict)  # 按加入顺序
    scores: dict[str, int] = field(default_factory=dict)      # player_id → 累计得分
    phase: str = WAITING
    round_no: int = 0
    template: Template | None = None
    assignments: dict[str, str] = field(default_factory=dict)  # element → 被分配的 player_id
    answers: dict[str, str] = field(default_factory=dict)      # element → 词
    answered_by: dict[str, str] = field(default_factory=dict)  # element → 实际填写（得分）的 player_id
    auto_filled: set[str] = field(default_factory=set)         # 超时自动选词的元素
    suggestions: dict[str, list[dict]] = field(default_factory=dict)
    deadlines: dict[str, float] = field(default_factory=dict)  # element → 截止时刻（只给"当前正在填"的空）
    fillers_by_element: dict[str, str] = field(default_factory=dict)  # 揭晓时记录填写人昵称
    sentence: str = ""
    sentence_id: int | None = None
    read_until: float = 0.0
    vote_until: float = 0.0
    votes: dict[str, str] = field(default_factory=dict)        # voter_id → element
    tally: dict[str, int] = field(default_factory=dict)        # element → 得票（result 阶段）
    recent_templates: list[int] = field(default_factory=list)
    last_fillers: set[str] = field(default_factory=set)

    # ---------- 玩家进出 ----------
    def join(self, name: str) -> Player:
        name = normalize(name)[: config.NAME_MAX_LEN] or "无名氏"
        taken = {p.name for p in self.players.values()}
        base, n = name, 2
        while name in taken:
            name, n = f"{base[: config.NAME_MAX_LEN - len(str(n))]}{n}", n + 1
        p = Player(uuid.uuid4().hex[:8], name)
        self.players[p.id] = p
        self.scores[p.id] = 0
        log.info("room=%s 加入 player=%s(%s) phase=%s 共 %d 人", self.code, p.id, p.name,
                 self.phase, len(self.players))
        return p

    def leave(self, player_id: str) -> None:
        p = self.players.pop(player_id, None)
        if not p:
            return
        score = self.scores.pop(player_id, 0)
        log.info("room=%s 离开 player=%s(%s) phase=%s 得分=%d 剩 %d 人", self.code, p.id, p.name,
                 self.phase, score, len(self.players))
        if not self.players:
            return
        if self.phase == FILLING:
            self._reassign_from(player_id)
            self._arm_deadlines()
        elif self.phase == VOTING:
            self.votes.pop(player_id, None)
            self._maybe_finish_votes()

    @property
    def empty(self) -> bool:
        return not self.players

    # ---------- 玩家操作 ----------
    def start(self, player_id: str) -> None:
        self._require_member(player_id)
        if self.phase != WAITING:
            raise GameError("游戏已经开始了")
        self._new_round()

    def next_round(self, player_id: str) -> None:
        self._require_member(player_id)
        if self.phase != RESULT:
            raise GameError("这一轮还没结束")
        self._new_round()

    def submit(self, player_id: str, element: str, text: str, picked: bool = False) -> None:
        self._require_member(player_id)
        if self.phase != FILLING:
            raise GameError("现在不是填写阶段")
        if self.assignments.get(element) != player_id:
            raise GameError("这个空不是分给你的")
        if element in self.answers:
            raise GameError("这个空已经填过了")
        try:  # 提交即入库，不等整句完成
            word = self.bank.submit(element, text, picked=picked, author=self.players[player_id].name)
        except ValueError as e:
            raise GameError(str(e)) from e
        self._record_answer(element, word, player_id, auto=False,
                            how="点选" if picked else "手填")

    def vote(self, player_id: str, element: str) -> None:
        self._require_member(player_id)
        if self.phase != VOTING:
            raise GameError("现在不是投票时间")
        if element not in self.answers:
            raise GameError("没有这个词条")
        if self.answered_by.get(element) == player_id:
            raise GameError("不能给自己投票")
        prev = self.votes.get(player_id)
        self.votes[player_id] = element
        log.info("room=%s 第%d轮 投票 %s → [%s]%r（%s）已投 %d/%d", self.code, self.round_no,
                 self.players[player_id].name, element, self.answers[element],
                 f"改票，原 [{prev}]" if prev else "首次", len(self.votes), len(self._eligible_voters()))
        self._maybe_finish_votes()

    # ---------- 时间推进 ----------
    def tick(self) -> bool:
        """检查超时并推进阶段。状态有变化返回 True（server 据此广播）。"""
        now = self.clock()
        changed = False
        if self.phase == FILLING:
            for el, dl in list(self.deadlines.items()):
                if self.phase == FILLING and el not in self.answers and now >= dl:
                    self._auto_fill(el)
                    changed = True
        if self.phase == REVEAL and now >= self.read_until:
            if self._eligible_voters():
                self.phase = VOTING
                self.vote_until = now + self.timing.vote
                log.info("room=%s 第%d轮 开始投票（%ds，可投票 %d 人）", self.code, self.round_no,
                         self.timing.vote, len(self._eligible_voters()))
            else:
                self._finish_votes(reason="无人可投票")
            changed = True
        if self.phase == VOTING and now >= self.vote_until:
            self._finish_votes(reason="投票时间到")
            changed = True
        return changed

    # ---------- 内部 ----------
    def _new_round(self) -> None:
        self.round_no += 1
        self.template = templates.pick(self.tpls, self.recent_templates, self.rng)
        self.recent_templates = (self.recent_templates + [self.template.id])[-config.RECENT_TEMPLATE_WINDOW:]
        self.assignments = self._assign()
        self.answers, self.answered_by, self.auto_filled = {}, {}, set()
        self.sentence, self.sentence_id, self.fillers_by_element = "", None, {}
        self.votes, self.tally, self.deadlines = {}, {}, {}
        self.suggestions = {el: self.bank.suggest(el) for el in config.ELEMENTS}
        self.last_fillers = set(self.assignments.values())
        self.phase = FILLING
        self._arm_deadlines()
        log.info("room=%s 第%d轮开始 模板#%d%s 分配=%s 候选=%s", self.code, self.round_no,
                 self.template.id, f"（梗：{self.template.meme['name']}）" if self.template.meme else "",
                 {el: self.players[pid].name for el, pid in self.assignments.items()},
                 {el: [w["text"] for w in ws] for el, ws in self.suggestions.items()})

    def _assign(self) -> dict[str, str]:
        """4 个空位随机分配。≥4 人：选 4 人，上轮旁观的优先；<4 人：轮流多分。"""
        ids = list(self.players)
        benched = [i for i in ids if i not in self.last_fillers]
        others = [i for i in ids if i in self.last_fillers]
        self.rng.shuffle(benched)
        self.rng.shuffle(others)
        chosen = (benched + others)[: config.SLOTS_PER_ROUND]
        elements = list(config.ELEMENTS)
        self.rng.shuffle(elements)
        return {el: chosen[i % len(chosen)] for i, el in enumerate(elements)}

    def _pending_of(self, player_id: str) -> list[str]:
        return [el for el in config.ELEMENTS
                if self.assignments.get(el) == player_id and el not in self.answers]

    def _arm_deadlines(self) -> None:
        """给每个被分配玩家"当前正在填"的空设置 30 秒截止（已设的不动）。"""
        now = self.clock()
        for pid in set(self.assignments.values()):
            pending = self._pending_of(pid)
            if pending and pending[0] not in self.deadlines:
                self.deadlines[pending[0]] = now + self.timing.fill

    def _record_answer(self, element: str, word: str, player_id: str, auto: bool, how: str) -> None:
        self.answers[element] = word
        self.answered_by[element] = player_id
        if auto:
            self.auto_filled.add(element)
        self.deadlines.pop(element, None)
        log.info("room=%s 第%d轮 [%s] ← %r by %s（%s）进度 %d/%d", self.code, self.round_no, element,
                 word, self.players[player_id].name if player_id in self.players else player_id, how,
                 len(self.answers), config.SLOTS_PER_ROUND)
        if len(self.answers) == config.SLOTS_PER_ROUND:
            self._reveal()
        else:
            self._arm_deadlines()

    def _auto_fill(self, element: str) -> None:
        """超时：优先从展示过的候选里随机选，没有就从整个词库随机，再没有用兜底词。"""
        pool = self.suggestions.get(element) or self.bank.suggest(element)
        word = self.rng.choice(pool)["text"] if pool else config.FALLBACK_WORDS[element]
        pid = self.assignments[element]
        log.info("room=%s 第%d轮 [%s] 超时 %ds，自动选词", self.code, self.round_no, element, self.timing.fill)
        self._record_answer(element, word, pid, auto=True, how="超时随机")

    def _reassign_from(self, gone_id: str) -> None:
        """掉线玩家没填的空，分给当前待填最少的在线玩家（截止时间重新计）。"""
        for el, pid in list(self.assignments.items()):
            if pid == gone_id and el not in self.answers:
                load = {i: len(self._pending_of(i)) for i in self.players}
                least = min(load.values())
                new = self.rng.choice([i for i, c in load.items() if c == least])
                self.assignments[el] = new
                self.deadlines.pop(el, None)
                log.info("room=%s 第%d轮 [%s] 掉线改派 → %s", self.code, self.round_no, el,
                         self.players[new].name)

    def _reveal(self) -> None:
        now = self.clock()
        self.sentence = self.template.fill(self.answers)
        self.fillers_by_element = {
            el: (self.players[pid].name if pid in self.players else "?")
            for el, pid in self.answered_by.items()}
        self.phase = REVEAL
        self.deadlines = {}
        read = self.timing.read_secs(self.sentence)
        self.read_until = now + read
        meme = self.template.meme["name"] if self.template.meme else None
        try:
            self.sentence_id = self.db.insert_sentence(
                self.code, self.template.id, self.sentence, round_no=self.round_no, meme=meme,
                answers={el: {"text": self.answers[el], "by": self.fillers_by_element[el]}
                         for el in config.ELEMENTS},
                auto_filled=sorted(self.auto_filled))
        except Exception:  # 历史句子只是存档，写失败不影响本轮
            log.exception("room=%s 历史句子写库失败", self.code)
        log.info("room=%s 第%d轮揭晓 sentence#%s「%s」朗读约 %.1fs 超时自动=%s", self.code, self.round_no,
                 self.sentence_id, self.sentence, read, sorted(self.auto_filled) or "无")

    def _eligible_voters(self) -> list[str]:
        """有资格投票的人：至少有一个词不是自己填的。"""
        return [p for p in self.players if any(by != p for by in self.answered_by.values())]

    def _maybe_finish_votes(self) -> None:
        if self.phase != VOTING:
            return
        eligible = self._eligible_voters()
        if eligible and all(p in self.votes for p in eligible):
            self._finish_votes(reason="全员已投")
        elif not eligible:
            self._finish_votes(reason="无人可投票")

    def _finish_votes(self, reason: str) -> None:
        self.tally = dict(Counter(el for voter, el in self.votes.items() if voter in self.players))
        gained: dict[str, int] = {}
        for el, n in self.tally.items():
            pid = self.answered_by.get(el)
            if pid in self.scores:
                self.scores[pid] += n
                gained[self.players[pid].name] = gained.get(self.players[pid].name, 0) + n
        self.phase = RESULT
        if self.sentence_id:
            try:
                self.db.set_sentence_votes(self.sentence_id, self.tally)
            except Exception:
                log.exception("room=%s 得票写库失败", self.code)
        log.info("room=%s 第%d轮 投票结束（%s）得票=%s 加分=%s 积分=%s", self.code, self.round_no, reason,
                 self.tally, gained, {self.players[p].name: s for p, s in self.scores.items()})

    def _require_member(self, player_id: str) -> None:
        if player_id not in self.players:
            raise GameError("你不在这个房间里")

    # ---------- 给前端的视图 ----------
    def _remaining(self, until: float) -> float:
        return round(max(0.0, until - self.clock()), 1)

    def public_state(self) -> dict:
        """所有人可见的状态。揭晓前绝不包含任何答案内容。剩余时间用"还剩几秒"表示，避免客户端时钟误差。"""
        st = {
            "type": "room_state",
            "code": self.code,
            "phase": self.phase,
            "round": self.round_no,
            "players": [{"id": p.id, "name": p.name, "score": self.scores.get(p.id, 0)}
                        for p in self.players.values()],
        }
        if not self.template or self.phase == WAITING:
            return st
        st["segments"] = self.template.segments()
        st["slots"] = {}
        for el, pid in self.assignments.items():
            slot = {"label": config.ELEMENTS[el], "filled": el in self.answers,
                    "filler": self.players[pid].name if pid in self.players else "?", "filler_id": pid}
            if self.phase == FILLING and el in self.deadlines and el not in self.answers:
                slot["remaining"] = self._remaining(self.deadlines[el])
            st["slots"][el] = slot
        if self.phase in (REVEAL, VOTING, RESULT):
            st["sentence"] = self.sentence
            st["answers"] = dict(self.answers)
            st["meme"] = self.template.meme
            for el in self.answers:
                st["slots"][el]["filler"] = self.fillers_by_element.get(el, "?")
                st["slots"][el]["filler_id"] = self.answered_by.get(el)
                st["slots"][el]["auto"] = el in self.auto_filled
        if self.phase == REVEAL:
            st["read_remaining"] = self._remaining(self.read_until)
        if self.phase == VOTING:
            st["vote_remaining"] = self._remaining(self.vote_until)
            st["voted_count"] = len(self.votes)
            st["voter_count"] = len(self._eligible_voters())
        if self.phase == RESULT:
            st["tally"] = dict(self.tally)
        return st

    def task_for(self, player_id: str) -> dict:
        """该玩家的个人视图：当前要填的空（一人多空时依次给），以及本轮投给了谁。"""
        base = {"type": "your_task", "round": self.round_no, "my_vote": self.votes.get(player_id)}
        pending = self._pending_of(player_id) if self.phase == FILLING else []
        if not pending:
            return {**base, "element": None, "remaining": 0}
        el = pending[0]
        return {**base, "element": el, "label": config.ELEMENTS[el],
                "suggestions": self.suggestions.get(el, []), "remaining": len(pending),
                "max_len": config.WORD_MAX_LEN,
                "time_left": self._remaining(self.deadlines[el]) if el in self.deadlines else None}


class RoomManager:
    def __init__(self, bank: WordBank, db: Database, tpls: list[Template], rng=None,
                 clock: Callable[[], float] = time.monotonic, timing: Timing | None = None):
        self.bank, self.db, self.tpls = bank, db, tpls
        self.rng = rng or random.Random()
        self.clock = clock
        self.timing = timing or Timing()
        self.rooms: dict[str, Room] = {}

    def create(self) -> Room:
        for _ in range(1000):
            code = f"{self.rng.randint(0, 9999):04d}"
            if code not in self.rooms:
                break
        else:
            raise RuntimeError("房间号用完了")
        room = Room(code, self.bank, self.db, self.tpls, rng=random.Random(self.rng.random()),
                    clock=self.clock, timing=self.timing)
        self.rooms[code] = room
        log.info("room=%s 创建（当前 %d 个房间）", code, len(self.rooms))
        return room

    def get(self, code: str) -> Room | None:
        return self.rooms.get(code)

    def discard_if_empty(self, code: str) -> None:
        room = self.rooms.get(code)
        if room and room.empty:
            del self.rooms[code]
            log.info("room=%s 已空，销毁（剩 %d 个房间，本房共玩 %d 轮）", code, len(self.rooms),
                     room.round_no)
