"""日志：统一挂在 tppe.* 命名空间下，按模块区分来源：

  tppe.app      启动 / 关闭 / 配置
  tppe.http     每个 HTTP 请求（方法、路径、状态码、耗时、来源 IP）
  tppe.ws       WebSocket 连接生命周期与收发的每条消息（带连接编号 conn=）
  tppe.room     房间与回合状态变化（建房、加入、分配、提交、改派、揭晓、销毁）
  tppe.wordbank 词入库 / 候选
  tppe.db       数据库初始化、迁移与异常
  tppe.client   浏览器端上报的错误（JS 异常、连接断开、朗读失败）

输出：控制台 + logs/tppe.log（全部）+ logs/error.log（WARNING 及以上，查错先看这个）。
uvicorn 自身的日志也接到同一套 handler 上；uvicorn 的 access 日志关闭，由 tppe.http 代替（带耗时）。
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

from tppe import config

FMT = "%(asctime)s.%(msecs)03d %(levelname)-7s [%(name)s] %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = config.LOG_LEVEL, to_file: bool = True) -> None:
    root = logging.getLogger("tppe")
    if getattr(root, "_tppe_configured", False):
        return
    fmt = logging.Formatter(FMT, DATEFMT)
    handlers: list[logging.Handler] = []

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    handlers.append(console)

    if to_file:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        full = RotatingFileHandler(config.LOG_DIR / "tppe.log", maxBytes=10_000_000,
                                   backupCount=5, encoding="utf-8")
        full.setFormatter(fmt)
        err = RotatingFileHandler(config.LOG_DIR / "error.log", maxBytes=5_000_000,
                                  backupCount=3, encoding="utf-8")
        err.setLevel(logging.WARNING)
        err.setFormatter(fmt)
        handlers += [full, err]

    for name in ("tppe", "uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers = []
        for h in handlers:
            lg.addHandler(h)
        lg.propagate = False
    root.setLevel(level)
    logging.getLogger("uvicorn").setLevel("INFO")
    logging.getLogger("uvicorn.access").disabled = True
    root._tppe_configured = True
