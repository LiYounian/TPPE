"""FastAPI：静态页面 + 建房 HTTP 接口 + 房间 WebSocket + 前端错误上报。

WebSocket 消息（JSON）：
  客户端→服务端  {"type": "start"} / {"type": "submit", "element", "text", "picked"} /
                 {"type": "vote", "element"} / {"type": "next"} / {"type": "ping"}
  服务端→客户端  welcome / room_state / your_task / error
每个事件处理完后，向房间内所有人推一次 room_state + 各自的 your_task。
后台定时任务每 TICK_SECONDS 调一次 room.tick()（填写超时、朗读结束、投票截止），有变化同样广播。
"""

import asyncio
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from tppe import __version__, config, templates
from tppe.db import Database
from tppe.room import GameError, Room, RoomManager, Timing
from tppe.wordbank import WordBank, load_seed

log = logging.getLogger("tppe.app")
http_log = logging.getLogger("tppe.http")
ws_log = logging.getLogger("tppe.ws")
client_log = logging.getLogger("tppe.client")

INDEX_HTML = config.PKG_DIR / "static" / "index.html"
CLIENT_LOG_MAX = 2000  # 前端上报单条最多保留的字符数


def _clip(s, n: int = 300) -> str:
    s = s if isinstance(s, str) else json.dumps(s, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + f"…(+{len(s) - n})"


def _client_ip(headers, client) -> str:
    fwd = headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return client.host if client else "?"


def create_app(db_path: Path | str = config.DB_PATH, timing: Timing | None = None,
               tick_seconds: float = config.TICK_SECONDS) -> FastAPI:
    db = Database(db_path)
    db.init_db(load_seed())
    tpls = templates.load()
    timing = timing or Timing()
    manager = RoomManager(WordBank(db), db, tpls, timing=timing)
    sockets: dict[str, dict[str, WebSocket]] = {}  # room_code → {player_id: ws}
    log.info("TPPE v%s 就绪：模板 %d 条（热梗 %d 条），词库 %s，计时 %s", __version__, len(tpls),
             sum(1 for t in tpls if t.meme), db.word_stats(), timing)

    async def ticker():
        """定时推进所有房间的计时状态；单个房间出错只记日志，不影响其他房间。"""
        while True:
            await asyncio.sleep(tick_seconds)
            for room in list(manager.rooms.values()):
                try:
                    if room.tick():
                        await broadcast(room, "tick")
                except Exception:
                    log.exception("room=%s tick 异常", room.code)

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(ticker())
        log.info("计时任务已启动（间隔 %.2fs）", tick_seconds)
        try:
            yield
        finally:
            task.cancel()
            log.info("服务关闭（房间 %d 个）", len(manager.rooms))

    app = FastAPI(title="TPPE", version=__version__, lifespan=lifespan)
    app.state.manager, app.state.db = manager, db

    # ---------- HTTP 请求日志 ----------
    @app.middleware("http")
    async def access_log(request: Request, call_next):
        t0 = time.perf_counter()
        ip = _client_ip(request.headers, request.client)
        try:
            resp = await call_next(request)
        except Exception:
            http_log.exception("%s %s 未处理异常 ip=%s", request.method, request.url.path, ip)
            raise
        ms = (time.perf_counter() - t0) * 1000
        level = logging.DEBUG if request.url.path == "/health" else logging.INFO
        if resp.status_code >= 500:
            level = logging.ERROR
        elif resp.status_code >= 400:
            level = logging.WARNING
        http_log.log(level, "%s %s → %d %.1fms ip=%s", request.method, request.url.path,
                     resp.status_code, ms, ip)
        return resp

    async def broadcast(room: Room, reason: str) -> None:
        state = room.public_state()
        targets = list(sockets.get(room.code, {}).items())
        ws_log.debug("room=%s 广播(%s) phase=%s 人数=%d", room.code, reason, room.phase, len(targets))
        for pid, ws in targets:
            try:
                await ws.send_json(state)
                await ws.send_json(room.task_for(pid))
            except Exception as e:  # 对方已断开，交给它自己的 disconnect 流程清理
                ws_log.warning("room=%s player=%s 推送失败: %r", room.code, pid, e)

    @app.get("/")
    async def index():
        return FileResponse(INDEX_HTML, media_type="text/html")

    @app.get("/health")
    async def health():
        return {"ok": True, "version": __version__, "rooms": len(manager.rooms),
                "players": sum(len(r.players) for r in manager.rooms.values()),
                "words": db.count_words()}

    @app.post("/api/rooms")
    async def create_room():
        return {"code": manager.create().code}

    @app.get("/api/rooms/{code}")
    async def room_exists(code: str):
        room = manager.get(code)
        if not room:
            raise HTTPException(404, "房间不存在")
        return {"code": code, "players": len(room.players), "phase": room.phase}

    @app.post("/api/client-log")
    async def client_report(request: Request):
        """浏览器端的报错上报：JS 异常、WebSocket 异常断开、朗读失败等。"""
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {"message": body}
        except Exception:
            body = {"message": (await request.body()).decode("utf-8", "replace")}
        level = {"error": logging.ERROR, "info": logging.INFO}.get(body.get("level"), logging.WARNING)
        client_log.log(level, "ip=%s room=%s player=%s kind=%s msg=%s ua=%s",
                       _client_ip(request.headers, request.client), body.get("room"), body.get("player"),
                       body.get("kind"), _clip(body.get("message", ""), CLIENT_LOG_MAX),
                       _clip(request.headers.get("user-agent", ""), 160))
        return {"ok": True}

    @app.websocket("/ws/{code}")
    async def ws_room(ws: WebSocket, code: str, name: str = ""):
        conn = uuid.uuid4().hex[:6]
        ip = _client_ip(ws.headers, ws.client)
        t0 = time.monotonic()
        await ws.accept()
        ws_log.info("conn=%s 连接 room=%s name=%r ip=%s ua=%s", conn, code, name, ip,
                    _clip(ws.headers.get("user-agent", ""), 160))
        room = manager.get(code)
        if not room:
            ws_log.warning("conn=%s 房间 %s 不存在，拒绝加入", conn, code)
            await ws.send_json({"type": "error", "message": "房间不存在，可能已经解散"})
            await ws.close(code=4404)
            return
        player = room.join(name)
        tag = f"conn={conn} room={code} player={player.id}({player.name})"
        sockets.setdefault(code, {})[player.id] = ws
        await ws.send_json({"type": "welcome", "player_id": player.id, "name": player.name})
        await broadcast(room, "join")
        close_reason = "?"
        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                    kind = msg.get("type")
                except (ValueError, AttributeError):
                    ws_log.warning("%s 收到非法 JSON: %s", tag, _clip(raw))
                    await ws.send_json({"type": "error", "message": "消息格式不对"})
                    continue
                if kind == "ping":
                    continue
                ws_log.info("%s ← %s", tag, _clip(raw))
                try:
                    if kind == "start":
                        room.start(player.id)
                    elif kind == "next":
                        room.next_round(player.id)
                    elif kind == "vote":
                        room.vote(player.id, str(msg.get("element", "")))
                    elif kind == "submit":
                        room.submit(player.id, str(msg.get("element", "")), str(msg.get("text", "")),
                                    picked=bool(msg.get("picked")))
                    else:
                        raise GameError(f"未知操作: {kind}")
                except GameError as e:
                    ws_log.warning("%s 操作被拒 type=%s: %s", tag, kind, e)
                    await ws.send_json({"type": "error", "message": str(e)})
                    continue
                await broadcast(room, kind)
        except WebSocketDisconnect as e:
            close_reason = f"客户端断开 code={e.code}"
        except Exception:
            close_reason = "服务端异常"
            ws_log.exception("%s 连接异常", tag)
        finally:
            sockets.get(code, {}).pop(player.id, None)
            ws_log.info("%s 断开（%s，在线 %.0fs）", tag, close_reason, time.monotonic() - t0)
            room.leave(player.id)
            if room.empty:
                sockets.pop(code, None)
                manager.discard_if_empty(code)
            else:
                await broadcast(room, "leave")

    return app


app = create_app()
