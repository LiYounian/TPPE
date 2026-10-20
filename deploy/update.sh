#!/usr/bin/env bash
# 服务器自动更新 + 自愈（跑一轮）。由 tppe-autoupdate.timer 每 2 分钟触发，也可手动执行。
#
#   ① 环境自检：git 仓库 / .venv / curl，缺了就报错退出（不往下走）
#   ② 检查远端：git fetch（带超时），没有新提交 → 静默退出，不重启
#   ③ 拉取：merge --ff-only；快进失败（本地被改过或分叉）→ reset --hard 跟随远端（服务器只读跟随）
#   ④ 部署：requirements.txt 变了才 pip install → 重启服务
#   ⑤ 探活：轮询 /health（默认 15 次 × 2 秒，给服务启动留时间），通过即完成
#   ⑥ 自愈：装依赖 / 重启 / 探活任一失败 → 回滚到更新前的提交 → 重装 → 重启 → 告警，退出码 1
#
# 用法：bash deploy/update.sh [--dry-run]       （--dry-run 只看远端有什么新提交，不改任何东西）
# 退出码：0 = 已是最新 / 更新成功；1 = 环境不满足 / 拉取失败 / 更新失败已回滚
#
# 可用环境变量（systemd 单元里设置；都有默认值）：
#   TPPE_BRANCH=main  TPPE_PORT=6333  TPPE_SERVICE=tppe
#   TPPE_RESTART_CMD="sudo -n systemctl restart tppe"   重启命令（本地测试时可替换）
#   TPPE_PIP_INDEX=<pip 镜像地址>                         国内服务器建议设阿里云镜像
#   TPPE_HEALTH_TRIES=15  TPPE_HEALTH_INTERVAL=2          探活轮询次数与间隔（秒）
#   TPPE_FETCH_TIMEOUT=90                                 git fetch 超时（秒）
#   TPPE_ALERT_WEBHOOK=<url>                              失败时 POST 一条 JSON 告警（可选）
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

BRANCH="${TPPE_BRANCH:-main}"
PORT="${TPPE_PORT:-6333}"
SERVICE="${TPPE_SERVICE:-tppe}"
RESTART_CMD="${TPPE_RESTART_CMD:-sudo -n systemctl restart $SERVICE}"
PIP_INDEX="${TPPE_PIP_INDEX:-}"
HEALTH_TRIES="${TPPE_HEALTH_TRIES:-15}"
HEALTH_INTERVAL="${TPPE_HEALTH_INTERVAL:-2}"
FETCH_TIMEOUT="${TPPE_FETCH_TIMEOUT:-90}"
ALERT_WEBHOOK="${TPPE_ALERT_WEBHOOK:-}"
PY=./.venv/bin/python
DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

mkdir -p logs data
LOG_FILE=logs/autoupdate.log
log() { echo "$(date '+%F %T') [autoupdate] $*" | tee -a "$LOG_FILE"; }

alert() {
  [ -n "$ALERT_WEBHOOK" ] || return 0
  curl -fsS -m 10 -X POST -H 'Content-Type: application/json' \
    -d "{\"text\": \"[TPPE 自动更新] $(hostname): $*\"}" "$ALERT_WEBHOOK" >/dev/null 2>&1 \
    || log "告警发送失败（webhook 不可达）"
}

fail() { log "失败：$*"; alert "$*"; exit 1; }

# 防并发：上一轮（比如 pip 装得慢）还没结束时，这一轮直接跳过
if command -v flock >/dev/null 2>&1; then
  exec 9>"logs/.autoupdate.lock"
  flock -n 9 || { echo "上一轮自动更新还在进行，本轮跳过"; exit 0; }
fi

# ① 环境自检
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail "当前目录不是 git 仓库: $(pwd)"
[ -x "$PY" ] || fail "缺少虚拟环境 $(pwd)/.venv，请先执行 deploy/install.sh 或按 README 建 venv"
command -v curl >/dev/null 2>&1 || fail "缺少 curl"

healthy() {  # 服务重启后要几秒才能起来，轮询等待，别一探就判死
  local i
  for ((i = 1; i <= HEALTH_TRIES; i++)); do
    curl -fsS -m 3 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && return 0
    sleep "$HEALTH_INTERVAL"
  done
  return 1
}

pip_install() {
  local args=(-q install -r requirements.txt)
  [ -n "$PIP_INDEX" ] && args+=(-i "$PIP_INDEX")
  "$PY" -m pip "${args[@]}"
}

# ② 检查远端
TIMEOUT_CMD=()
command -v timeout >/dev/null 2>&1 && TIMEOUT_CMD=(timeout "$FETCH_TIMEOUT")
if ! GIT_TERMINAL_PROMPT=0 ${TIMEOUT_CMD[@]+"${TIMEOUT_CMD[@]}"} git fetch -q origin "$BRANCH" 2>>"$LOG_FILE"; then
  fail "git fetch origin/$BRANCH 失败或超时（${FETCH_TIMEOUT}s），本轮不更新，下轮重试"
fi
before=$(git rev-parse HEAD)
target=$(git rev-parse "origin/$BRANCH")
if [ "$before" = "$target" ]; then
  [ "$DRY_RUN" = 1 ] && log "[dry-run] 已是最新 ${before:0:7}"
  exit 0   # 无变更：静默退出，不写日志（每 2 分钟一次，免得刷屏）
fi

if [ "$DRY_RUN" = 1 ]; then
  log "[dry-run] 有新提交 ${before:0:7} → ${target:0:7}："
  git log --oneline "$before..$target" | tee -a "$LOG_FILE"
  git diff --quiet "$before" "$target" -- requirements.txt || log "[dry-run] requirements.txt 有变化，会重装依赖"
  exit 0
fi

rollback() {
  local why="$1"
  log "更新失败（${why}），回滚到 ${before:0:7}"
  git reset -q --hard "$before"
  git diff --quiet "$before" "$after" -- requirements.txt || pip_install || log "回滚时重装依赖失败"
  if eval "$RESTART_CMD" && healthy; then
    log "已回滚，服务恢复健康（远端 ${after:0:7} 有问题，修好后推送会自动再试）"
    alert "更新 ${before:0:7}→${after:0:7} 失败（${why}），已回滚，服务正常"
  else
    log "回滚后服务仍不健康！请查: journalctl -u $SERVICE -n 100 / logs/error.log"
    alert "更新失败且回滚后服务仍不健康（${why}），需要人工处理"
  fi
  exit 1
}

# ③ 拉取
if ! git merge -q --ff-only "origin/$BRANCH" 2>/dev/null; then
  log "无法快进（服务器上的代码被改过或与远端分叉），强制对齐 origin/$BRANCH"
  git reset -q --hard "origin/$BRANCH"
fi
after=$(git rev-parse HEAD)
log "发现更新 ${before:0:7} → ${after:0:7}："
git log --oneline "$before..$after" | sed 's/^/    /' | tee -a "$LOG_FILE"

# ④ 部署
if ! git diff --quiet "$before" "$after" -- requirements.txt; then
  log "requirements.txt 有变化，安装依赖"
  pip_install || rollback "pip install 失败"
fi
log "重启服务：$RESTART_CMD"
eval "$RESTART_CMD" || rollback "重启命令失败"

# ⑤ 探活
if healthy; then
  log "更新成功，服务健康：$(curl -fsS -m 3 "http://127.0.0.1:${PORT}/health")"
  exit 0
fi
# ⑥ 自愈
rollback "重启后 $((HEALTH_TRIES * HEALTH_INTERVAL)) 秒内 /health 不通"
