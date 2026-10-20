# TPPE · 多人填词造句网页游戏

多名玩家进同一房间（昵称 10 个字符以内），每轮随机抽一条缺了「时间 / 地点 / 人物 / 事件」的短句模板，
每人被分配填其中一个元素；全部填完后拼成完整句子，用浏览器自带 TTS 朗读出来。

- **填词**：每个空限时 30 秒。可点候选词（至多 3 个，标明是谁贡献的），也永远可以自己打字（≤20 字符）；超时从候选里随机选一个。
- **词库**：玩家填过的词都进对应元素的词库（记录最早贡献者），下次就可能出现在候选里。冷启动每个元素有 15 个预置词。
- **投票**：朗读完后 20 秒投票，点别人填的词投一票（每人每轮 1 票，可改票，不能投自己），每得一票 +1 分，积分在房间内累计。
- **热梗**：模板里混有一批网络热梗句，揭晓时显示梗的出处和走红时间。

- 技术栈：Python 3.10+（FastAPI + WebSocket）+ SQLite + 单页 HTML（原生 JS，浏览器 Web Speech API 朗读）
- 默认端口：**6333**
- 当前状态：v1.1，可拉起试玩

---

## 部署到服务器（给部署 agent 的操作说明）

> 目标：在 Linux 云服务器（阿里云 ECS）上用 systemd 常驻运行本服务，监听 `0.0.0.0:6333`，外网访问 `http://<公网IP>:6333/` 即可游玩；
> 同时启用**自动更新**：每 2 分钟检查 GitHub 上的 main，有新提交就自动拉取、重启，重启后不健康就自动回滚到上一版。
> 只需要 Python 3.10+、git、curl，不需要数据库服务（SQLite 文件自动创建）。**只能单进程运行**（房间状态在内存里），不要用 gunicorn 多 worker。

### 1. 装系统依赖

先看系统和 Python 版本：`cat /etc/os-release; python3 --version`。Python 低于 3.10 就装 3.11：

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y git curl python3.11 python3.11-venv
# Alibaba Cloud Linux 3 / CentOS / RHEL 系
sudo dnf install -y git curl python3.11
```

### 2. 拉代码

用**普通用户**（如 `admin`）clone 到自己有写权限的目录，路径随意，例如：

```bash
git clone https://github.com/LiYounian/TPPE.git ~/TPPE
```

### 3. 一键安装（服务 + 自动更新）

```bash
cd ~/TPPE && sudo bash deploy/install.sh
```

脚本会自动完成（可重复执行，重装也安全）：
1. 找到 Python 3.10+，建 `.venv`，用阿里云 pip 镜像装依赖（换源：`TPPE_PIP_INDEX=<url> sudo -E bash deploy/install.sh`；改端口：`TPPE_PORT=xxxx`）；
2. 以"执行 sudo 的用户"和当前仓库路径渲染并安装 3 个 systemd 单元：`tppe.service`（游戏服务，崩溃 3 秒自动拉起）、`tppe-autoupdate.service` + `tppe-autoupdate.timer`（每 2 分钟跑一轮 `deploy/update.sh`）；
3. 写 `/etc/sudoers.d/tppe-autoupdate`：**只**允许该用户免密执行 `systemctl restart tppe`（自动更新要用）；
4. 启动服务并探活，最后打印 `{"ok":true,...}` 和定时器状态即成功。

### 4. 放行端口

- **阿里云控制台 → ECS 实例 → 安全组 → 入方向**：添加规则，协议 TCP，端口 `6333/6333`，授权对象 `0.0.0.0/0`（或只放行你们的 IP）。
- 系统防火墙如果开着也要放行：`sudo ufw allow 6333/tcp`（Ubuntu）或 `sudo firewall-cmd --permanent --add-port=6333/tcp && sudo firewall-cmd --reload`（CentOS 系）。
- 外网验证：在自己电脑上打开 `http://<公网IP>:6333/`。

### 5. 自动更新是怎么工作的

本地 `git push` 到 main 后，约 2 分钟内服务器自动完成更新，无需登录服务器。每一轮（`deploy/update.sh`）：

1. **环境自检**：不是 git 仓库 / 缺 `.venv` / 缺 curl → 记日志退出，不往下走；
2. **检查远端**：`git fetch`（30 秒超时）；没有新提交就**静默退出、不重启**。国内服务器连 GitHub 时好时坏：偶尔失败只算网络波动，**不写日志、不告警**，下轮重试；**连续失败 5 次（约 10 分钟）**才在日志里记一条（之后每小时提醒一次），恢复时也记一条；
3. **拉取**：能快进就快进；服务器上的代码被手改过或分叉 → 强制对齐远端（服务器只读跟随，不要在服务器上改代码）；
4. **部署**：`requirements.txt` 变了才重装依赖，然后重启服务；
5. **探活**：轮询 `/health` 最多 30 秒（服务启动需要几秒，不会一探就判死）；
6. **自愈**：装依赖 / 重启 / 探活任一失败 → 回滚到更新前的版本并重启，记日志（可选 webhook 告警），等远端修好再推送时自动重试。

常用命令：

| 目的 | 命令 |
|---|---|
| 看自动更新记录（只有发生更新/失败时才写） | `tail -n 50 ~/TPPE/logs/autoupdate.log` |
| 看定时器下次什么时候跑 | `systemctl list-timers tppe-autoupdate.timer --no-pager` |
| 看每轮的运行输出 | `journalctl -u tppe-autoupdate -n 50 --no-pager` |
| 立刻手动跑一轮 | `cd ~/TPPE && bash deploy/update.sh` |
| 只看远端有什么新提交、不动任何东西 | `cd ~/TPPE && bash deploy/update.sh --dry-run` |
| 暂停 / 恢复自动更新 | `sudo systemctl stop tppe-autoupdate.timer` / `sudo systemctl start tppe-autoupdate.timer` |
| 失败告警 | 编辑 `/etc/systemd/system/tppe-autoupdate.service` 里的 `TPPE_ALERT_WEBHOOK=`，然后 `sudo systemctl daemon-reload` |

### 6. 管理后台

浏览器打开 `http://<公网IP>:6333/admin`，输入管理口令即可管理数据：
- **词库**：按元素筛选、搜索、按使用/点选次数排序；**新增**词；**改字**（原词隐藏，新词继承使用次数）；**删除**（其实是隐藏：不再出现在候选里，可在"只看已删除"里**恢复**）。
- **历史句子**：每轮拼出的句子、四个空的填写人（⏰ 表示超时随机）、得票。
- **概览**：各元素词数、已删除数、历史句子数、当前在线房间与人数。

口令：
- 第一次执行 `install.sh` 时自动生成，**只在安装输出里显示一次**，之后查看：`sudo cat /etc/tppe/admin.env`。
- 换口令：编辑这个文件，把 `TPPE_ADMIN_TOKEN=` 后面改成新的值（建议 ≥12 位随机字符），然后 `sudo systemctl restart tppe`。
- 口令文件不存在或为空时，后台整体关闭（返回 404）。同一 IP 1 分钟内输错 10 次会被锁定 5 分钟。
- 后台的所有写操作都记在 `logs/tppe.log`（`grep tppe.admin`），带操作者 IP；口令不会出现在任何日志里。

短句模板和预置词不在后台管理，它们在仓库的 `tppe/templates.json`、`tppe/seed_words.json` 里，改完推送即可。

### 7. 看日志 / 排错

| 看什么 | 命令 |
|---|---|
| 服务状态 | `systemctl status tppe` |
| 进程输出（含启动失败原因） | `journalctl -u tppe -n 100 --no-pager` |
| 全量业务日志 | `tail -f ~/TPPE/logs/tppe.log` |
| 只看警告和错误（**查错先看这个**） | `tail -n 100 ~/TPPE/logs/error.log` |
| 某个房间发生了什么 | `grep "room=1234" ~/TPPE/logs/tppe.log` |
| 浏览器端报的错（JS 异常、断线、朗读失败） | `grep "tppe.client" ~/TPPE/logs/tppe.log` |
| 管理后台的操作记录 | `grep "tppe.admin" ~/TPPE/logs/tppe.log` |
| 自动更新做了什么 | `tail -n 50 ~/TPPE/logs/autoupdate.log` |
| 端口被谁占了 | `sudo lsof -nP -iTCP:6333 -sTCP:LISTEN` |

常见问题：
- **外网打不开**：依次查安全组是否放行 6333 → `systemctl is-active tppe` → `curl 127.0.0.1:6333/health`。
- **启动即退出，日志写"端口 6333 已被占用"**：用上表命令找出占用进程；或者在 service 文件里把 `TPPE_PORT` 改成别的端口（安全组也跟着改）。
- **启动报"需要 Python 3.10 及以上"**：按第 1 步装 python3.11，删掉 `.venv` 后重建。
- **能打开页面但进房后一直"连接断开"**：多半是前面挂了 nginx 却没转发 WebSocket，见下一节。
- **push 了但服务器没更新**：先看 `logs/autoupdate.log`。写着"已连续失败 N 次" → 服务器连不上 GitHub（偶发失败不会记录，只能在 `journalctl -u tppe-autoupdate` 里看到），会自动重试；写着"已回滚" → 新代码起不来，看 `journalctl -u tppe -n 100` 找原因，修好再推；什么都没写 → `systemctl list-timers` 看定时器是否启用。

### 8.（可选）挂在 nginx 后面 / 用域名

WebSocket 必须带 Upgrade 头，否则进不了房间：

```nginx
location / {
    proxy_pass http://127.0.0.1:6333;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 3600s;
}
```

用 nginx 时把 `/etc/systemd/system/tppe.service` 里的 `TPPE_HOST` 改成 `127.0.0.1`（重跑 install.sh 会覆盖回 0.0.0.0，需再改），安全组改为放行 80/443、关掉 6333。

### 9. 数据备份

所有持久数据只有一个 SQLite 文件 `data/tppe.db`（词库 + 历史句子）。在线备份：

```bash
sqlite3 ~/TPPE/data/tppe.db ".backup '$HOME/TPPE/data/tppe-$(date +%F).db'"
```

---

## 本地运行

```bash
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m tppe            # 默认 0.0.0.0:6333，--port 可改
```

浏览器打开 `http://127.0.0.1:6333/`。一人创建房间，把「复制邀请链接」发给其他人即可一起玩。

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `TPPE_HOST` / `TPPE_PORT` | `0.0.0.0` / `6333` | 监听地址，命令行 `--host/--port` 优先 |
| `TPPE_DB_PATH` | `data/tppe.db` | SQLite 文件 |
| `TPPE_LOG_DIR` | `logs/` | 日志目录：`tppe.log`（全量）+ `error.log`（WARNING 以上） |
| `TPPE_LOG_LEVEL` | `INFO` | 设成 `DEBUG` 可看到每次广播和候选词抽取 |
| `TPPE_ADMIN_TOKEN` | 空 | 管理后台口令；为空则后台关闭（服务器上由 `/etc/tppe/admin.env` 注入） |

计时参数（填写 30 秒、投票 20 秒、朗读时长估算）在 `tppe/config.py` 顶部。

接口：`GET /health`（健康检查）、`POST /api/rooms`（建房）、`GET /api/rooms/{code}`、`WS /ws/{code}?name=`（消息协议见 `tppe/server.py` 顶部）、`POST /api/client-log`（前端错误上报）。

### 日志命名空间

| logger | 记什么 |
|---|---|
| `tppe.app` | 启动配置、版本、模板与词库数量、端口占用 |
| `tppe.http` | 每个 HTTP 请求：方法、路径、状态码、耗时、来源 IP |
| `tppe.ws` | 每条 WebSocket 连接（`conn=` 编号）：连接、收到的每条消息、被拒操作、断开原因与时长 |
| `tppe.room` | 建房、加入/离开、每轮分配与模板、每次提交（点选/手填/超时随机、进度）、掉线改派、揭晓、每张票、投票结算与积分、销毁 |
| `tppe.wordbank` | 词入库（新词/已有）、被拒原因、候选（DEBUG） |
| `tppe.db` | 初始化、结构迁移、预置词写入、数据库异常 |
| `tppe.client` | 浏览器上报：JS 异常、连接异常断开、朗读失败、没有中文语音 |
| `tppe.admin` | 管理后台：登录、口令错误/锁定、新增/改字/删除/恢复（带 IP 和改动前后内容） |

## 测试

```bash
./.venv/bin/pip install -r requirements-dev.txt
./.venv/bin/python -m pytest -q --timeout 15 tests
```

## 目录

| 路径 | 内容 |
|---|---|
| `tppe/__main__.py` | 入口 `python -m tppe`（启动前检查 Python 版本和端口占用） |
| `tppe/config.py` | 常量（词 ≤20 字符、候选 3 个等）与环境变量配置 |
| `tppe/logging_setup.py` | 日志配置 |
| `tppe/db.py` | SQLite：`words` 词库表（含贡献者、使用/点选次数）+ `sentences` 历史句子表（含各空答案、填写人、得票、超时项、热梗），带版本迁移 |
| `tppe/wordbank.py` | 词的规范化、校验、入库、随机候选 |
| `tppe/templates.py` / `templates.json` | 短句模板（普通句 + 热梗句，热梗带出处和时间） |
| `tppe/seed_words.json` | 冷启动预置词，每个元素 15 个 |
| `tppe/room.py` | 房间与回合状态机（填写 → 朗读 → 投票 → 结算，计时与积分，纯逻辑） |
| `tppe/server.py` | FastAPI 路由 + 房间 WebSocket + 前端错误上报 |
| `tppe/admin.py` · `static/admin.html` | 管理后台（口令保护）：词库增删改查、历史句子、概览 |
| `tppe/static/index.html` | 游戏页面（深色主题） |
| `deploy/` | `install.sh`（一键安装）、`update.sh`（一轮自动更新 + 探活 + 回滚）、`tppe.service` / `tppe-autoupdate.service` / `tppe-autoupdate.timer`（systemd 单元模板） |
| `tests/` | pytest |
| `docs/计划/` · `docs/日志/开发日志.md` | 方案文档 · 工作日志 |

## 已知限制

- 房间状态在内存里，服务重启后房间会消失（词库和历史句子在 SQLite 里不丢）；只支持单进程。
- 朗读音色取决于玩家的系统和浏览器；Linux 上可能没有中文语音（页面会提示，文字照常显示）。
- 刷新页面等于离开后重新加入（会以新玩家身份进房）。
