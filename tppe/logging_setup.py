"""日志：统一挂在 tppe.* 命名空间下（tppe.server / tppe.room / tppe.db / tppe.wordbank），
同时输出到控制台和 logs/tppe.log。"""

import logging
from logging.handlers import RotatingFileHandler

from tppe import config

_FMT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


def setup_logging(level: str = config.LOG_LEVEL, to_file: bool = True) -> None:
    root = logging.getLogger("tppe")
    if root.handlers:
        return
    root.setLevel(level)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(_FMT))
    root.addHandler(console)
    if to_file:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = RotatingFileHandler(config.LOG_DIR / "tppe.log", maxBytes=5_000_000,
                                 backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter(_FMT))
        root.addHandler(fh)
