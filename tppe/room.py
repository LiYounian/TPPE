"""房间与回合状态机（纯逻辑，不碰网络）。

阶段：waiting（等人开始）→ filling（填空）→ reveal（揭晓）→ next → filling …
server 层在每个事件后调用 public_state() / task_for() 把结果推给玩家。
"""

import logging
import random
import uuid
from dataclasses import dataclass, field

from tppe import config, templates
from tppe.db import Database
from tppe.templates import Template
from tppe.wordbank import WordBank, normalize

log = logging.getLogger("tppe.room")

WAITING, FILLING, REVEAL = "waiting", "filling", "reveal"


class GameError(ValueError):
    """玩家操作不合法（阶段不对、不是你的空位等），消息可直接展示给玩家。"""


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

    players: dict[str, Player] = field(default_factory=dict)  # 按加入顺序
    phase: str = WAITING
    round_no: int = 0
    template: Template | None = None
    assignments: dict[str, str] = field(default_factory=dict)  # element → player_id
    answers: dict[str, str] = field(default_factory=dict)      # element → text
    suggestions: dict[str, list[str]] = field(default_factory=dict)
    fillers_by_element: dict[str, str] = field(default_factory=dict)  # 揭晓时记录填写人昵称
    sentence: str = ""
    recent_templates: list[int] = field(default_factory=list)
    last_fillers: set[str] = field(default_factory=set)

    # ---------- 玩家进出 ----------
    def join(self, name: str) -> Player:
        name = normalize(name)[: config.NAME_MAX_LEN] or "无名氏"
        taken = {p.name for p in self.players.values()}
        base, n = name, 2
        while name in taken:
            name, n = f"{base}{n}", n + 1
        p = Player(uuid.uuid4().hex[:8], name)
        self.players[p.id] = p
        log.info("room=%s 加入 player=%s(%s) phase=%s 共 %d 人", self.code, p.id, p.name,
                 self.phase, len(self.players))
        return p

    def leave(self, player_id: str) -> None:
        p = self.players.pop(player_id, None)
        if not p:
            return
        log.info("room=%s 离开 player=%s(%s) phase=%s 剩 %d 人", self.code, p.id, p.name,
                 self.phase, len(self.players))
        if self.phase == FILLING and self.players:
            self._reassign_from(player_id)

    @property
    def empty(self) -> bool:
        return not self.players

    # ---------- 回合 ----------
    def start(self, player_id: str) -> None:
        self._require_member(player_id)
        if self.phase != WAITING:
            raise GameError("游戏已经开始了")
        self._new_round()

    def next_round(self, player_id: str) -> None:
        self._require_member(player_id)
        if self.phase != REVEAL:
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
        try:
            word = self.bank.submit(element, text, picked=picked)  # 提交即入库，不等整句完成
        except ValueError as e:
            raise GameError(str(e)) from e
        self.answers[element] = word
        log.info("room=%s 第%d轮 [%s] ← %r by %s（%s）进度 %d/%d", self.code, self.round_no, element,
                 word, self.players[player_id].name, "点选" if picked else "手填",
                 len(self.answers), config.SLOTS_PER_ROUND)
        if len(self.answers) == config.SLOTS_PER_ROUND:
            self._reveal()

    def _new_round(self) -> None:
        self.round_no += 1
        self.template = templates.pick(self.tpls, self.recent_templates, self.rng)
        self.recent_templates = (self.recent_templates + [self.template.id])[-config.RECENT_TEMPLATE_WINDOW:]
        self.assignments = self._assign()
        self.answers, self.sentence, self.fillers_by_element = {}, "", {}
        self.suggestions = {el: self.bank.suggest(el) for el in config.ELEMENTS}
        self.last_fillers = set(self.assignments.values())
        self.phase = FILLING
        log.info("room=%s 第%d轮开始 模板#%d%s 分配=%s 候选=%s", self.code, self.round_no,
                 self.template.id, f"（梗：{self.template.meme['name']}）" if self.template.meme else "",
                 {el: self.players[pid].name for el, pid in self.assignments.items()},
                 {el: len(w) for el, w in self.suggestions.items()})

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

    def _reassign_from(self, gone_id: str) -> None:
        """掉线玩家没填的空位，分给当前待填最少的在线玩家。"""
        for el, pid in list(self.assignments.items()):
            if pid == gone_id and el not in self.answers:
                load = {i: 0 for i in self.players}
                for e, p in self.assignments.items():
                    if p in load and e not in self.answers:
                        load[p] += 1
                least = min(load.values())
                new = self.rng.choice([i for i, c in load.items() if c == least])
                self.assignments[el] = new
                log.info("room=%s 第%d轮 [%s] 掉线改派 → %s", self.code, self.round_no, el,
                         self.players[new].name)

    def _reveal(self) -> None:
        self.sentence = self.template.fill(self.answers)
        self.fillers_by_element = {
            el: (self.players[pid].name if pid in self.players else "?")
            for el, pid in self.assignments.items()}
        self.phase = REVEAL
        meme = self.template.meme["name"] if self.template.meme else None
        try:
            sid = self.db.insert_sentence(
                self.code, self.template.id, self.sentence, round_no=self.round_no, meme=meme,
                answers={el: {"text": self.answers[el], "by": self.fillers_by_element[el]}
                         for el in config.ELEMENTS})
        except Exception:  # 历史句子只是存档，写失败不影响本轮揭晓
            log.exception("room=%s 历史句子写库失败", self.code)
            sid = None
        log.info("room=%s 第%d轮揭晓 sentence#%s「%s」", self.code, self.round_no, sid, self.sentence)

    def _require_member(self, player_id: str) -> None:
        if player_id not in self.players:
            raise GameError("你不在这个房间里")

    # ---------- 给前端的视图 ----------
    def public_state(self) -> dict:
        """所有人可见的状态。揭晓前绝不包含任何答案内容。"""
        st = {
            "type": "room_state",
            "code": self.code,
            "phase": self.phase,
            "round": self.round_no,
            "players": [{"id": p.id, "name": p.name} for p in self.players.values()],
        }
        if self.template and self.phase in (FILLING, REVEAL):
            st["segments"] = self.template.segments()
            st["slots"] = {
                el: {"label": config.ELEMENTS[el],
                     "filler": self.players[pid].name if pid in self.players else "?",
                     "filled": el in self.answers}
                for el, pid in self.assignments.items()}
        if self.phase == REVEAL:
            st["sentence"] = self.sentence
            st["meme"] = self.template.meme
            st["answers"] = dict(self.answers)
            for el, name in self.fillers_by_element.items():
                st["slots"][el]["filler"] = name
        return st

    def task_for(self, player_id: str) -> dict:
        """该玩家当前要填的空（一人多空时依次给第一个），没有则 element=None。"""
        pending = [el for el in config.ELEMENTS
                   if self.phase == FILLING and self.assignments.get(el) == player_id
                   and el not in self.answers]
        if not pending:
            return {"type": "your_task", "round": self.round_no, "element": None, "remaining": 0}
        el = pending[0]
        return {"type": "your_task", "round": self.round_no, "element": el, "label": config.ELEMENTS[el],
                "suggestions": self.suggestions.get(el, []), "remaining": len(pending),
                "max_len": config.WORD_MAX_LEN}


class RoomManager:
    def __init__(self, bank: WordBank, db: Database, tpls: list[Template], rng=None):
        self.bank, self.db, self.tpls = bank, db, tpls
        self.rng = rng or random.Random()
        self.rooms: dict[str, Room] = {}

    def create(self) -> Room:
        for _ in range(1000):
            code = f"{self.rng.randint(0, 9999):04d}"
            if code not in self.rooms:
                break
        else:
            raise RuntimeError("房间号用完了")
        room = Room(code, self.bank, self.db, self.tpls, rng=random.Random(self.rng.random()))
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
