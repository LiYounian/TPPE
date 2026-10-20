"""FastAPI：静态页面 + 建房 HTTP 接口 + 房间 WebSocket + 前端错误上报。

WebSocket 消息（JSON）：
  客户端→服务端  {"type": "start"} / {"type": "submit", "element", "text", "picked"} / {"type": "next"}
  服务端→客户端  welcome / room_state / your_task / error
每个事件处理完后，向房间内所有人推一次 room_state + 各自的 your_task。
"""

import json
import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from tppe import __version__, config, templates
from tppe.db import Database
from tppe.room import GameError, Room, RoomManager
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


def create_app(db_path: Path | str = config.DB_PATH) -> FastAPI:
    db = Database(db_path)
    db.init_db(load_seed())
    tpls = templates.load()
    manager = RoomManager(WordBank(db), db, tpls)
    sockets: dict[str, dict[str, WebSocket]] = {}  # room_code → {player_id: ws}
    log.info("TPPE v%s 就绪：模板 %d 条（热梗 %d 条），词库 %s", __version__, len(tpls),
             sum(1 for t in tpls if t.meme), db.word_stats())

    app = FastAPI(title="TPPE", version=__version__)
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
