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

> 目标：在一台 Linux 云服务器（阿里云 ECS）上，用 systemd 常驻运行本服务，监听 `0.0.0.0:6333`，外网浏览器访问 `http://<公网IP>:6333/` 即可游玩。
> 只需要 Python 3.10+ 和 git，不需要数据库服务（SQLite 文件随程序自动创建）。**只能单进程运行**（房间状态在内存里），不要用 gunicorn 多 worker。

### 1. 装系统依赖

先看系统和 Python 版本：`cat /etc/os-release; python3 --version`。Python 低于 3.10 就装 3.11：

```bash
# Ubuntu / Debian
sudo apt update && sudo apt install -y git python3.11 python3.11-venv curl
# Alibaba Cloud Linux 3 / CentOS / RHEL 系
sudo dnf install -y git python3.11 curl
```

下文用 `python3.11`；如果系统自带的 `python3` 已经 ≥3.10，换成 `python3` 也可以。

### 2. 拉代码、建虚拟环境、冒烟

```bash
sudo useradd -m -s /bin/bash deploy 2>/dev/null || true
sudo mkdir -p /srv && sudo chown deploy:deploy /srv
sudo -u deploy git clone https://github.com/LiYounian/TPPE.git /srv/tppe
sudo -u deploy bash -lc 'cd /srv/tppe && python3.11 -m venv .venv && ./.venv/bin/pip install -U pip && ./.venv/bin/pip install -r requirements.txt'
# 冒烟：前台起一下，另开终端 curl 到 {"ok":true,...} 就 Ctrl+C 停掉
sudo -u deploy bash -lc 'cd /srv/tppe && ./.venv/bin/python -m tppe'
curl -s http://127.0.0.1:6333/health
```

### 3. 用 systemd 常驻

```bash
sudo cp /srv/tppe/deploy/tppe.service /etc/systemd/system/tppe.service   # 路径/用户不同就先改文件里的 User / WorkingDirectory / ExecStart
sudo systemctl daemon-reload
sudo systemctl enable --now tppe
systemctl is-active tppe                      # 期望 active
curl -s http://127.0.0.1:6333/health          # 期望 {"ok":true,...}
```

### 4. 放行端口

- **阿里云控制台 → ECS 实例 → 安全组 → 入方向**：添加规则，协议 TCP，端口 `6333/6333`，授权对象 `0.0.0.0/0`（或只放行你们的 IP）。
- 系统防火墙如果开着也要放行：`sudo ufw allow 6333/tcp`（Ubuntu）或 `sudo firewall-cmd --permanent --add-port=6333/tcp && sudo firewall-cmd --reload`（CentOS 系）。
- 外网验证：在自己电脑上打开 `http://<公网IP>:6333/`。

### 5. 更新代码

```bash
cd /srv/tppe && sudo -u deploy bash deploy/update.sh
```

脚本会拉最新 main，有变更才装依赖并重启，重启后轮询 `/health` 最多约 30 秒，不健康就自动回滚到更新前的版本。`deploy` 用户需要能免密执行重启，加一条窄授权：

```bash
echo 'deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart tppe' | sudo tee /etc/sudoers.d/tppe
sudo chmod 440 /etc/sudoers.d/tppe && sudo visudo -cf /etc/sudoers.d/tppe
```

想定时自动更新，可以加一条 cron（每 5 分钟）：`*/5 * * * * cd /srv/tppe && bash deploy/update.sh >> logs/update.log 2>&1`（加在 deploy 用户的 crontab 里）。

### 6. 看日志 / 排错

| 看什么 | 命令 |
|---|---|
| 服务状态 | `systemctl status tppe` |
| 进程输出（含启动失败原因） | `journalctl -u tppe -n 100 --no-pager` |
| 全量业务日志 | `tail -f /srv/tppe/logs/tppe.log` |
| 只看警告和错误（**查错先看这个**） | `tail -n 100 /srv/tppe/logs/error.log` |
| 某个房间发生了什么 | `grep "room=1234" /srv/tppe/logs/tppe.log` |
| 浏览器端报的错（JS 异常、断线、朗读失败） | `grep "tppe.client" /srv/tppe/logs/tppe.log` |
| 端口被谁占了 | `sudo lsof -nP -iTCP:6333 -sTCP:LISTEN` |

常见问题：
- **外网打不开**：依次查安全组是否放行 6333 → `systemctl is-active tppe` → `curl 127.0.0.1:6333/health`。
- **启动即退出，日志写"端口 6333 已被占用"**：用上表命令找出占用进程；或者在 service 文件里把 `TPPE_PORT` 改成别的端口（安全组也跟着改）。
- **启动报"需要 Python 3.10 及以上"**：按第 1 步装 python3.11，删掉 `.venv` 后重建。
- **能打开页面但进房后一直"连接断开"**：多半是前面挂了 nginx 却没转发 WebSocket，见下一节。

### 7.（可选）挂在 nginx 后面 / 用域名

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

用 nginx 时把 service 里的 `TPPE_HOST` 改成 `127.0.0.1`，安全组改为放行 80/443、关掉 6333。

### 8. 数据备份

所有持久数据只有一个 SQLite 文件 `data/tppe.db`（词库 + 历史句子）。在线备份：

```bash
sqlite3 /srv/tppe/data/tppe.db ".backup '/srv/tppe/data/tppe-$(date +%F).db'"
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
| `tppe/static/index.html` | 游戏页面（深色主题） |
| `deploy/` | `tppe.service`（systemd）、`update.sh`（拉取更新 + 探活 + 回滚） |
| `tests/` | pytest |
| `docs/计划/` · `docs/日志/开发日志.md` | 方案文档 · 工作日志 |

## 已知限制

- 房间状态在内存里，服务重启后房间会消失（词库和历史句子在 SQLite 里不丢）；只支持单进程。
- 朗读音色取决于玩家的系统和浏览器；Linux 上可能没有中文语音（页面会提示，文字照常显示）。
- 刷新页面等于离开后重新加入（会以新玩家身份进房）。
