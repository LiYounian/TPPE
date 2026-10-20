"""入口：python -m tppe [--host 0.0.0.0] [--port 6333]"""

import argparse
import logging
import platform
import socket
import sys

import uvicorn

from tppe import __version__, config
from tppe.logging_setup import setup_logging

log = logging.getLogger("tppe.app")


def port_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def main() -> None:
    p = argparse.ArgumentParser(description="TPPE 多人填词造句游戏服务")
    p.add_argument("--host", default=config.HOST)
    p.add_argument("--port", type=int, default=config.PORT)
    args = p.parse_args()
    setup_logging()
    log.info("TPPE v%s 启动中 | Python %s | %s", __version__, platform.python_version(), platform.platform())
    log.info("配置: host=%s port=%d db=%s logs=%s level=%s",
             args.host, args.port, config.DB_PATH, config.LOG_DIR, config.LOG_LEVEL)
    if not port_free(args.host, args.port):
        log.error("端口 %d 已被占用，启动失败。查占用: lsof -nP -iTCP:%d -sTCP:LISTEN", args.port, args.port)
        sys.exit(1)
    uvicorn.run("tppe.server:app", host=args.host, port=args.port,
                log_config=None, access_log=False, ws_ping_interval=20, ws_ping_timeout=20)


if __name__ == "__main__":
    main()
