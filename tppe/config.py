"""全局常量与可被环境变量覆盖的运行配置。"""

import os
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent
ROOT_DIR = PKG_DIR.parent

# 四个元素：内部键 → 中文名。顺序即页面展示顺序。
ELEMENTS = {"time": "时间", "place": "地点", "person": "人物", "event": "事件"}
# 模板占位符用中文名，如 {时间}
PLACEHOLDER = {k: "{" + v + "}" for k, v in ELEMENTS.items()}

WORD_MAX_LEN = 20       # 每个词最多 20 个字符
WORD_MIN_LEN = 1
SUGGEST_COUNT = 3       # 候选词至多 3 个
NAME_MAX_LEN = 10       # 昵称上限（字符）
SLOTS_PER_ROUND = len(ELEMENTS)
RECENT_TEMPLATE_WINDOW = 10   # 同一房间避开最近 N 条模板

# 回合计时（秒）。服务端计时，客户端只负责显示倒计时。
FILL_SECONDS = 30        # 每个空的填写时限，超时从候选里随机选一个
VOTE_SECONDS = 20        # 朗读结束后的投票时长
READ_SECS_PER_CHAR = 0.25  # 按字数估算朗读时长（浏览器朗读时长服务端拿不到）
READ_MIN_SECONDS = 3
READ_MAX_SECONDS = 15
TICK_SECONDS = 0.5       # 服务端检查超时的间隔

# 超时且该元素词库为空时的兜底词（不入库）
FALLBACK_WORDS = {"time": "某一天", "place": "某个地方", "person": "某个人", "event": "发了一会儿呆"}

HOST = os.environ.get("TPPE_HOST", "0.0.0.0")
PORT = int(os.environ.get("TPPE_PORT", "6333"))
DB_PATH = Path(os.environ.get("TPPE_DB_PATH", ROOT_DIR / "data" / "tppe.db"))
LOG_DIR = Path(os.environ.get("TPPE_LOG_DIR", ROOT_DIR / "logs"))
LOG_LEVEL = os.environ.get("TPPE_LOG_LEVEL", "INFO")
