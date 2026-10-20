"""入口：python -m tppe [--host 0.0.0.0] [--port 6333]"""

import argparse

import uvicorn

from tppe import config
from tppe.logging_setup import setup_logging


def main() -> None:
    p = argparse.ArgumentParser(description="TPPE 多人填词造句游戏服务")
    p.add_argument("--host", default=config.HOST)
    p.add_argument("--port", type=int, default=config.PORT)
    args = p.parse_args()
    setup_logging()
    uvicorn.run("tppe.server:app", host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
