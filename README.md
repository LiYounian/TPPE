# TPPE · 多人填词造句网页游戏

多名玩家进同一房间，每轮随机抽一条缺了「时间 / 地点 / 人物 / 事件」的短句模板，
每人被分配填其中一个元素；全部填完后拼成完整句子，用浏览器自带 TTS 朗读出来。
玩家填过的词进入对应元素的词库，下次作为候选词（至多 3 个）供人一键点选；永远可以自己打字。

- 技术栈：Python（FastAPI + WebSocket）+ SQLite + 单页 HTML（原生 JS，浏览器 Web Speech API 朗读）
- 当前状态：**v1 已完成，本地联调通过**；服务器部署待做（见 `docs/计划/` 第九节）

## 运行

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m tppe --host 0.0.0.0 --port 8000
```

浏览器打开 `http://<服务器IP>:8000/`。一人创建房间，把「复制邀请链接」发给其他人即可一起玩。

可用环境变量覆盖配置：

| 变量 | 默认 | 说明 |
|---|---|---|
| `TPPE_HOST` / `TPPE_PORT` | `0.0.0.0` / `8000` | 监听地址，命令行 `--host/--port` 优先 |
| `TPPE_DB_PATH` | `data/tppe.db` | SQLite 词库文件 |
| `TPPE_LOG_DIR` | `logs/` | 日志目录（`tppe.log`，按模块分 `tppe.server/room/db/wordbank`） |
| `TPPE_LOG_LEVEL` | `INFO` | 日志级别 |

健康检查：`GET /health` → `{"ok": true, "rooms": N, "words": N}`。

## 测试

```bash
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python -m pytest -q --timeout 15 tests
```

## 目录

| 路径 | 内容 |
|---|---|
| `tppe/__main__.py` | 入口 `python -m tppe` |
| `tppe/config.py` | 常量（词 ≤20 字符、候选 3 个等）与环境变量配置 |
| `tppe/db.py` | SQLite：`words` 词库表 + `sentences` 历史句子表 |
| `tppe/wordbank.py` | 词的规范化、校验、入库、随机候选 |
| `tppe/templates.py` / `templates.json` | 短句模板（80 条，只能往末尾追加以保持 id 稳定） |
| `tppe/seed_words.json` | 冷启动预置词，每个元素 15 个 |
| `tppe/room.py` | 房间与回合状态机（纯逻辑） |
| `tppe/server.py` | FastAPI 路由 + 房间 WebSocket |
| `tppe/static/index.html` | 游戏页面 |
| `tests/` | pytest |
| `docs/计划/` | 动手前的方案文档 |
| `docs/日志/开发日志.md` | 工作日志（时间倒序） |

## 已知限制

- 房间状态在内存里，服务重启后房间会消失（词库和历史句子在 SQLite 里不丢）；只支持单进程部署。
- 朗读音色取决于玩家的系统和浏览器；Linux 上可能没有中文语音（页面会给出提示，文字照常显示）。
- 刷新页面等于离开后重新加入（会以新玩家身份进房）。
