#!/usr/bin/env bash
# 服务器上的一键更新：拉最新 main → 有变更才装依赖并重启 → 探活（带重试）→ 不健康就回滚到更新前版本。
# 用法：bash deploy/update.sh            （在仓库根目录执行，需要能免密 sudo systemctl restart tppe）
# 退出码：0 = 已是最新或更新成功；1 = 更新失败且已回滚。
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${TPPE_PORT:-6333}"
SERVICE="${TPPE_SERVICE:-tppe}"
PY=./.venv/bin/python
log() { echo "$(date '+%F %T') [update] $*"; }

healthy() {  # 重启后服务要几秒才能起来，轮询 15 次 × 2s，别一探就判死
  for _ in $(seq 1 15); do
    curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && return 0
    sleep 2
  done
  return 1
}

before=$(git rev-parse HEAD)
git fetch -q origin main
if ! git merge -q --ff-only origin/main 2>/dev/null; then
  log "无法快进（本地有改动或分叉），强制对齐 origin/main"
  git reset -q --hard origin/main
fi
after=$(git rev-parse HEAD)
if [ "$before" = "$after" ]; then
  log "已是最新 ${after:0:7}，无需更新"
  exit 0
fi

log "更新 ${before:0:7} → ${after:0:7}"
$PY -m pip install -q -r requirements.txt
sudo systemctl restart "$SERVICE"
if healthy; then
  log "更新成功，服务健康：$(curl -fsS http://127.0.0.1:${PORT}/health)"
  exit 0
fi

log "重启后不健康，回滚到 ${before:0:7}"
git reset -q --hard "$before"
$PY -m pip install -q -r requirements.txt
sudo systemctl restart "$SERVICE"
healthy && log "已回滚，服务恢复" || log "回滚后仍不健康，请查 journalctl -u $SERVICE 和 logs/error.log"
exit 1
