"""FastAPI：静态页面 + 建房 HTTP 接口 + 房间 WebSocket。

WebSocket 消息（JSON）：
  客户端→服务端  {"type": "start"} / {"type": "submit", "element", "text"} / {"type": "next"}
  服务端→客户端  welcome / room_state / your_task / error
每个事件处理完后，向房间内所有人推一次 room_state + 各自的 your_task。
"""

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from tppe import config, templates
from tppe.db import Database
from tppe.room import GameError, Room, RoomManager
from tppe.wordbank import WordBank, load_seed

log = logging.getLogger("tppe.server")

INDEX_HTML = config.PKG_DIR / "static" / "index.html"


def create_app(db_path: Path | str = config.DB_PATH) -> FastAPI:
    db = Database(db_path)
    db.init_db(load_seed())
    manager = RoomManager(WordBank(db), db, templates.load())
    sockets: dict[str, dict[str, WebSocket]] = {}  # room_code → {player_id: ws}

    app = FastAPI(title="TPPE")
    app.state.manager, app.state.db = manager, db

    async def broadcast(room: Room) -> None:
        state = room.public_state()
        for pid, ws in list(sockets.get(room.code, {}).items()):
            try:
                await ws.send_json(state)
                await ws.send_json(room.task_for(pid))
            except Exception:  # 对方已断开，交给它自己的 disconnect 流程清理
                log.debug("推送失败 room=%s player=%s", room.code, pid)

    @app.get("/")
    async def index():
        return FileResponse(INDEX_HTML, media_type="text/html")

    @app.get("/health")
    async def health():
        return {"ok": True, "rooms": len(manager.rooms), "words": db.count_words()}

    @app.post("/api/rooms")
    async def create_room():
        return {"code": manager.create().code}

    @app.get("/api/rooms/{code}")
    async def room_exists(code: str):
        room = manager.get(code)
        if not room:
            raise HTTPException(404, "房间不存在")
        return {"code": code, "players": len(room.players), "phase": room.phase}

    @app.websocket("/ws/{code}")
    async def ws_room(ws: WebSocket, code: str, name: str = ""):
        await ws.accept()
        room = manager.get(code)
        if not room:
            await ws.send_json({"type": "error", "message": "房间不存在，可能已经解散"})
            await ws.close()
            return
        player = room.join(name)
        sockets.setdefault(code, {})[player.id] = ws
        await ws.send_json({"type": "welcome", "player_id": player.id, "name": player.name})
        await broadcast(room)
        try:
            while True:
                msg = await ws.receive_json()
                kind = msg.get("type")
                try:
                    if kind == "start":
                        room.start(player.id)
                    elif kind == "next":
                        room.next_round(player.id)
                    elif kind == "submit":
                        room.submit(player.id, str(msg.get("element", "")), str(msg.get("text", "")))
                    else:
                        raise GameError(f"未知操作: {kind}")
                except GameError as e:
                    await ws.send_json({"type": "error", "message": str(e)})
                    continue
                await broadcast(room)
        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("房间 %s 玩家 %s 连接异常", code, player.name)
        finally:
            sockets.get(code, {}).pop(player.id, None)
            room.leave(player.id)
            if room.empty:
                sockets.pop(code, None)
                manager.discard_if_empty(code)
            else:
                await broadcast(room)

    return app


app = create_app()
