#!/usr/bin/env bash
# 一键安装 / 重装：游戏服务 + 自动更新定时器 + 重启所需的窄授权 sudo。可重复执行（幂等）。
#
# 用法（在服务器上，仓库根目录里）：  sudo bash deploy/install.sh
#   - 运行用户自动取"执行 sudo 的那个用户"（如 admin），仓库路径自动取本脚本所在仓库
#   - 端口默认 6333，可用 TPPE_PORT=xxxx sudo -E bash deploy/install.sh 覆盖
#   - pip 默认走阿里云镜像，可用 TPPE_PIP_INDEX=<url> 覆盖（设为空串则用官方源）
set -euo pipefail

[ "$(id -u)" = 0 ] || { echo "请用 sudo 执行：sudo bash deploy/install.sh"; exit 1; }
DIR=$(cd "$(dirname "$0")/.." && pwd)
RUN_USER="${SUDO_USER:-}"
[ -n "$RUN_USER" ] && [ "$RUN_USER" != root ] || { echo "请用普通用户 + sudo 执行（不要直接以 root 登录执行），服务将以该用户运行"; exit 1; }
RUN_HOME=$(getent passwd "$RUN_USER" | cut -d: -f6)
PORT="${TPPE_PORT:-6333}"
PIP_INDEX="${TPPE_PIP_INDEX-https://mirrors.aliyun.com/pypi/simple/}"
SYSTEMCTL=$(command -v systemctl)
say() { echo "[install] $*"; }

say "仓库: $DIR  运行用户: $RUN_USER  端口: $PORT  pip 源: ${PIP_INDEX:-官方}"
[ "$(stat -c %U "$DIR")" = "$RUN_USER" ] || say "注意：仓库目录属主不是 ${RUN_USER}，自动更新可能没有写权限"

# 1. Python ≥3.10 的虚拟环境
if [ ! -x "$DIR/.venv/bin/python" ]; then
  PYBIN=""
  for c in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then PYBIN=$(command -v "$c"); break; fi
  done
  [ -n "$PYBIN" ] || { say "没找到 Python 3.10+，请先安装（apt install python3.11 python3.11-venv 或 dnf install python3.11）"; exit 1; }
  say "创建虚拟环境（${PYBIN}）"
  sudo -u "$RUN_USER" "$PYBIN" -m venv "$DIR/.venv"
fi
say "安装依赖"
PIP_ARGS=(-q install -r "$DIR/requirements.txt")
[ -n "$PIP_INDEX" ] && PIP_ARGS+=(-i "$PIP_INDEX")
sudo -u "$RUN_USER" "$DIR/.venv/bin/python" -m pip "${PIP_ARGS[@]}"
sudo -u "$RUN_USER" mkdir -p "$DIR/data" "$DIR/logs"
chmod +x "$DIR/deploy/update.sh"

# 2. 渲染并安装 systemd 单元
render() {
  sed -e "s#__USER__#$RUN_USER#g" -e "s#__DIR__#$DIR#g" -e "s#__HOME__#$RUN_HOME#g" \
      -e "s#__PORT__#$PORT#g" -e "s#__PIP_INDEX__#$PIP_INDEX#g" "$DIR/deploy/$1" > "/etc/systemd/system/$1"
  say "已安装 /etc/systemd/system/$1"
}
render tppe.service
render tppe-autoupdate.service
render tppe-autoupdate.timer

# 3. 窄授权：只允许运行用户免密重启 tppe 这一个服务（自动更新要用）
SUDOERS=/etc/sudoers.d/tppe-autoupdate
echo "$RUN_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart tppe" > "$SUDOERS.tmp"
chmod 440 "$SUDOERS.tmp"
visudo -cf "$SUDOERS.tmp" >/dev/null && mv "$SUDOERS.tmp" "$SUDOERS" && say "已安装 ${SUDOERS}（仅允许免密 systemctl restart tppe）"

# 4. 管理后台口令：不存在才生成（重装不换口令）；只 root 可读，systemd 通过 EnvironmentFile 注入
ADMIN_ENV=/etc/tppe/admin.env
NEW_TOKEN=""
mkdir -p /etc/tppe && chmod 700 /etc/tppe
if [ ! -s "$ADMIN_ENV" ]; then
  NEW_TOKEN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
  umask 077
  echo "TPPE_ADMIN_TOKEN=${NEW_TOKEN}" > "$ADMIN_ENV"
  say "已生成管理后台口令 ${ADMIN_ENV}"
fi
chmod 600 "$ADMIN_ENV"

# 5. 启动
systemctl daemon-reload
systemctl enable -q tppe
systemctl restart tppe
systemctl enable -q --now tppe-autoupdate.timer
say "等待服务就绪…"
for _ in $(seq 1 15); do curl -fsS -m 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break; sleep 2; done
if curl -fsS -m 3 "http://127.0.0.1:$PORT/health"; then
  echo; say "✅ 服务健康，自动更新定时器已启用："
  systemctl list-timers tppe-autoupdate.timer --no-pager | head -3
  say "记得在阿里云安全组放行 TCP ${PORT}；之后 push 到 main，约 2 分钟内服务器自动更新。"
  say "管理后台：http://<服务器公网IP>:${PORT}/admin"
  if [ -n "$NEW_TOKEN" ]; then
    say "管理口令（只显示这一次，之后用 sudo cat ${ADMIN_ENV} 查看）：${NEW_TOKEN}"
  else
    say "管理口令沿用已有的，查看：sudo cat ${ADMIN_ENV}"
  fi
else
  say "❌ 服务没起来，看日志：journalctl -u tppe -n 50 --no-pager"; exit 1
fi
