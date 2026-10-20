"""管理后台：词库查看 / 新增 / 改字 / 删除（隐藏）/ 恢复，历史句子，概览。

访问控制：管理口令来自环境变量 TPPE_ADMIN_TOKEN（服务器上由 /etc/tppe/admin.env 注入，不进仓库）。
  - 没配置口令 → 整个后台（页面 + 接口）返回 404
  - 请求头 X-Admin-Token 与口令常数时间比对；错误 → 401
  - 同一 IP 在 FAIL_WINDOW 秒内输错 MAX_FAILS 次 → 锁定 LOCK_SECONDS 秒（429）
口令本身绝不写进日志。
"""

import hmac
import logging
import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel

from tppe import config
from tppe.db import Database, DuplicateWord
from tppe.wordbank import normalize, validate

log = logging.getLogger("tppe.admin")

ADMIN_HTML = config.PKG_DIR / "static" / "admin.html"
MAX_FAILS, FAIL_WINDOW, LOCK_SECONDS = 10, 60, 300
WORDS_PAGE_SIZE, SENTENCES_PAGE_SIZE = 50, 30


class Guard:
    def __init__(self, token: str, clock: Callable[[], float] = time.monotonic):
        self.token = (token or "").strip()
        self.clock = clock
        self.fails: dict[str, deque] = defaultdict(deque)
        self.locked_until: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def check(self, ip: str, provided: str) -> None:
        if not self.enabled:
            raise HTTPException(404, "Not Found")
        now = self.clock()
        if self.locked_until.get(ip, 0) > now:
            raise HTTPException(429, f"口令错误次数过多，请 {int(self.locked_until[ip] - now) + 1} 秒后再试")
        if hmac.compare_digest((provided or "").encode(), self.token.encode()):
            self.fails.pop(ip, None)
            return
        q = self.fails[ip]
        q.append(now)
        while q and q[0] < now - FAIL_WINDOW:
            q.popleft()
        if len(q) >= MAX_FAILS:
            self.locked_until[ip] = now + LOCK_SECONDS
            q.clear()
            log.warning("ip=%s 管理口令连续输错 %d 次，锁定 %ds", ip, MAX_FAILS, LOCK_SECONDS)
            raise HTTPException(429, f"口令错误次数过多，已锁定 {LOCK_SECONDS // 60} 分钟")
        log.warning("ip=%s 管理口令错误（%d 分钟内第 %d 次）", ip, FAIL_WINDOW // 60, len(q))
        raise HTTPException(401, "管理口令不对")


class WordIn(BaseModel):
    element: str
    text: str


class TextIn(BaseModel):
    text: str


def _clean(text: str) -> str:
    t = normalize(text)
    ok, why = validate(t)
    if not ok:
        raise HTTPException(400, why)
    return t


def create_admin_router(db: Database, manager, token: str, client_ip: Callable,
                        clock: Callable[[], float] = time.monotonic) -> APIRouter:
    guard = Guard(token, clock)
    r = APIRouter()
    if guard.enabled:
        if len(guard.token) < 12:
            log.warning("管理口令过短（%d 位），建议至少 12 位随机字符", len(guard.token))
        log.info("管理后台已启用：/admin")
    else:
        log.info("管理后台未启用（没有配置 TPPE_ADMIN_TOKEN）")

    def auth(request: Request) -> str:
        ip = client_ip(request.headers, request.client)
        guard.check(ip, request.headers.get("x-admin-token", ""))
        return ip

    def element_ok(el: str | None) -> str | None:
        if el and el not in config.ELEMENTS:
            raise HTTPException(400, f"未知元素: {el}")
        return el or None

    @r.get("/admin")
    async def admin_page():
        if not guard.enabled:
            raise HTTPException(404, "Not Found")
        return FileResponse(ADMIN_HTML, media_type="text/html")

    @r.post("/api/admin/login")
    async def login(ip: str = Depends(auth)):
        log.info("ip=%s 管理员登录", ip)
        return {"ok": True}

    @r.get("/api/admin/stats")
    async def stats(_: str = Depends(auth)):
        return {"elements": config.ELEMENTS,
                "words": db.word_stats(),
                "hidden": db.count_words(include_hidden=True) - db.count_words(),
                "sentences": db.count_sentences(),
                "rooms": len(manager.rooms),
                "players": sum(len(rm.players) for rm in manager.rooms.values())}

    @r.get("/api/admin/words")
    async def words(element: str = "", q: str = "", hidden: bool = False, sort: str = "new",
                    page: int = 1, _: str = Depends(auth)):
        items, total = db.list_words(element_ok(element), q.strip()[:40], hidden, sort,
                                     max(page, 1), WORDS_PAGE_SIZE)
        return {"items": items, "total": total, "page": max(page, 1), "page_size": WORDS_PAGE_SIZE}

    @r.post("/api/admin/words")
    async def add_word(body: WordIn, ip: str = Depends(auth)):
        el = element_ok(body.element)
        if not el:
            raise HTTPException(400, "请选择元素")
        text = _clean(body.text)
        try:
            wid = db.admin_add_word(el, text)
        except DuplicateWord as e:
            raise HTTPException(409, str(e))
        log.info("ip=%s 新增词 #%d [%s] %r", ip, wid, el, text)
        return db.get_word(wid)

    @r.patch("/api/admin/words/{word_id}")
    async def edit_word(word_id: int, body: TextIn, ip: str = Depends(auth)):
        old = db.get_word(word_id)
        if not old:
            raise HTTPException(404, "词不存在")
        text = _clean(body.text)
        if text == old["text"]:
            return old
        try:
            new_id = db.admin_edit_word(word_id, text)
        except DuplicateWord as e:
            raise HTTPException(409, str(e))
        log.info("ip=%s 改字 [%s] #%d %r → #%d %r（原词已隐藏）", ip, old["element"], word_id, old["text"],
                 new_id, text)
        return db.get_word(new_id)

    async def _set_hidden(word_id: int, hidden: bool, ip: str):
        w = db.get_word(word_id)
        if not w:
            raise HTTPException(404, "词不存在")
        db.set_hidden(word_id, hidden)
        log.info("ip=%s %s词 #%d [%s] %r", ip, "删除（隐藏）" if hidden else "恢复", word_id, w["element"], w["text"])
        return db.get_word(word_id)

    @r.post("/api/admin/words/{word_id}/hide")
    async def hide_word(word_id: int, ip: str = Depends(auth)):
        return await _set_hidden(word_id, True, ip)

    @r.post("/api/admin/words/{word_id}/restore")
    async def restore_word(word_id: int, ip: str = Depends(auth)):
        return await _set_hidden(word_id, False, ip)

    @r.get("/api/admin/sentences")
    async def sentences(q: str = "", page: int = 1, _: str = Depends(auth)):
        items, total = db.list_sentences(q.strip()[:40], max(page, 1), SENTENCES_PAGE_SIZE)
        return {"items": items, "total": total, "page": max(page, 1), "page_size": SENTENCES_PAGE_SIZE}

    return r
