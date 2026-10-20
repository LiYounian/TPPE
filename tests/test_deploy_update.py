"""deploy/update.sh 集成测试：在临时目录里模拟"远端仓库 + 服务器 clone + 真实运行的服务"，
验证 无变更静默 / 正常更新 / 坏代码自动回滚 / 服务器被改过时强制对齐 / dry-run 不改动 / 缺 venv 报错。
重启命令用本地脚本代替 systemctl。需要 bash、git、curl。"""

import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.timeout(180),
              pytest.mark.skipif(not all(shutil.which(c) for c in ("bash", "git", "curl")),
                                 reason="需要 bash / git / curl")]


def sh(cmd, cwd, env=None, check=True):
    r = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True, env=env, errors="backslashreplace")
    if check and r.returncode:
        raise AssertionError(f"{cmd}\n{r.stdout}\n{r.stderr}")
    return r


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def health(port):
    try:
        import json
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return json.load(r)
    except Exception:
        return None


@pytest.fixture
def world(tmp_path):
    port = free_port()
    git_env = "-c user.name=t -c user.email=t@t"
    origin, dev, srv = tmp_path / "origin.git", tmp_path / "dev", tmp_path / "srv"
    sh(f"git init -q --bare -b main {origin}", tmp_path)
    sh(f"git clone -q {origin} {dev}", tmp_path)
    # 把当前工作区代码（含未提交的 update.sh）作为远端 main 的初始版本
    shutil.copytree(ROOT, dev, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git", ".venv", "data", "logs", "__pycache__", ".pytest_cache"))
    sh(f"git add -A && git {git_env} commit -qm base && git push -q origin HEAD:main", dev)
    sh(f"git clone -q -b main {origin} {srv}", tmp_path)
    os.symlink(ROOT / ".venv", srv / ".venv")

    pidfile = tmp_path / "pid"
    restart = tmp_path / "restart.sh"
    restart.write_text(f"""#!/usr/bin/env bash
[ -f {pidfile} ] && kill $(cat {pidfile}) 2>/dev/null; sleep 0.5
cd {srv}
# 注意：不能写成 "cd && nohup ... &"，那样整条链在后台子 shell 里跑，子 shell 会一直占着调用方的输出管道
TPPE_DB_PATH={tmp_path}/t.db TPPE_LOG_DIR={tmp_path}/logs \\
  nohup ./.venv/bin/python -m tppe --host 127.0.0.1 --port {port} > {tmp_path}/run.log 2>&1 < /dev/null &
echo $! > {pidfile}
""")
    # 用 C locale 跑，模拟 systemd 定时任务的极简环境（中文紧跟 $变量 在这种环境下会被当成变量名的一部分）
    env = {**os.environ, "LC_ALL": "C", "LANG": "C", "TPPE_PORT": str(port), "TPPE_RESTART_CMD": f"bash {restart}",
           "TPPE_HEALTH_TRIES": "15", "TPPE_HEALTH_INTERVAL": "1", "TPPE_PIP_INDEX": ""}
    sh(f"bash {restart}", tmp_path)
    for _ in range(20):
        if health(port):
            break
        time.sleep(0.5)
    assert health(port), (tmp_path / "run.log").read_text()

    def push(path, content, msg):
        p = dev / path
        p.write_text(content)
        sh(f"git add -A && git {git_env} commit -qm '{msg}' && git push -q origin HEAD:main", dev)
        return sh("git rev-parse HEAD", dev).stdout.strip()

    def update(*args):
        return sh(f"bash deploy/update.sh {' '.join(args)}", srv, env=env, check=False)

    yield {"port": port, "dev": dev, "srv": srv, "push": push, "update": update, "env": env}
    subprocess.run(f"pkill -f 'tppe --host 127.0.0.1 --port {port}'", shell=True)  # 按端口清理，不漏掉重启过的进程


def srv_head(w):
    return sh("git rev-parse HEAD", w["srv"]).stdout.strip()


def autolog(w):
    p = w["srv"] / "logs" / "autoupdate.log"
    return p.read_text() if p.exists() else ""


def set_version(w, v):
    init = (w["dev"] / "tppe" / "__init__.py").read_text()
    import re
    return w["push"]("tppe/__init__.py", re.sub(r'__version__ = ".*"', f'__version__ = "{v}"', init), f"v{v}")


def test_no_change_is_silent(world):
    r = world["update"]()
    assert r.returncode == 0
    assert "发现更新" not in autolog(world)


def test_update_then_broken_push_rolls_back(world):
    w = world
    good = set_version(w, "9.9.1")
    r = w["update"]()
    assert r.returncode == 0, r.stdout + r.stderr
    assert srv_head(w) == good
    assert health(w["port"])["version"] == "9.9.1"
    assert "更新成功" in autolog(w)

    server_py = (w["dev"] / "tppe" / "server.py").read_text()
    w["push"]("tppe/server.py", server_py + "\nraise RuntimeError('故意写坏')\n", "broken")
    r = w["update"]()
    assert r.returncode == 1, r.stdout + r.stderr
    assert srv_head(w) == good, f"stdout={r.stdout}\nstderr={r.stderr}\nlog={autolog(w)}"  # 代码回到好版本
    assert health(w["port"])["version"] == "9.9.1"  # 服务恢复
    assert "已回滚，服务恢复健康" in autolog(w)

    # 远端修好后（再推一个好版本），下一轮自动装上
    fixed = w["push"]("tppe/server.py", server_py, "fix")
    set_version(w, "9.9.2")
    assert w["update"]().returncode == 0
    assert health(w["port"])["version"] == "9.9.2"
    assert sh("git log --format=%H -2", w["srv"]).stdout.split()[1] == fixed


def test_dirty_server_is_forced_to_follow_remote(world):
    w = world
    (w["srv"] / "README.md").write_text("服务器上被人手改了")
    sh("git -c user.name=t -c user.email=t@t commit -qam local-edit", w["srv"])  # 制造分叉
    new = set_version(w, "9.9.3")
    r = w["update"]()
    assert r.returncode == 0, r.stdout + r.stderr
    assert srv_head(w) == new
    assert "强制对齐" in autolog(w)


def test_dry_run_changes_nothing(world):
    w = world
    before = srv_head(w)
    set_version(w, "9.9.4")
    r = w["update"]("--dry-run")
    assert r.returncode == 0
    assert srv_head(w) == before
    assert "[dry-run] 有新提交" in autolog(w) and "v9.9.4" in autolog(w)


def test_missing_venv_fails_fast(world):
    w = world
    os.unlink(w["srv"] / ".venv")
    r = w["update"]()
    assert r.returncode == 1
    assert "缺少虚拟环境" in autolog(w)


def test_scripts_brace_vars_before_non_ascii():
    """bash 在 C locale 下会把 "$why（" 里全角字符的字节当成变量名一部分 → unbound variable，脚本中途退出、回滚不执行。
    所以 $变量 后面紧跟中文/全角符号时必须写成 ${变量}。"""
    import re
    for p in (ROOT / "deploy").glob("*.sh"):
        bad = re.findall(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", p.read_text(encoding="utf-8"))
        assert not bad, f"{p.name}: {bad}"


def test_fetch_failures_are_quiet_until_threshold_then_recover(world):
    """国内服务器连 GitHub 时好时坏：偶发失败不写日志；连续 5 次才记一条；之后每 30 次提醒一次；恢复时记一条并清零。"""
    w = world
    good_url = sh("git remote get-url origin", w["srv"]).stdout.strip()
    sh("git remote set-url origin /nonexistent/repo.git", w["srv"])
    for i in range(1, 5):
        r = w["update"]()
        assert r.returncode == 0, r.stdout + r.stderr
        assert f"连续第 {i} 次" in r.stdout
    assert "失败" not in autolog(w)                    # 前 4 次：日志文件里什么都没有

    r = w["update"]()
    assert r.returncode == 1
    assert "已连续失败 5 次" in autolog(w)             # 第 5 次：记一条

    for _ in range(29):                                 # 第 6~34 次：不重复刷
        assert w["update"]().returncode == 0
    assert autolog(w).count("已连续失败") == 1
    assert w["update"]().returncode == 1                # 第 35 次（5 + 30）：再提醒一次
    assert "已连续失败 35 次" in autolog(w)

    sh(f"git remote set-url origin {good_url}", w["srv"])
    new = set_version(w, "9.9.5")
    assert w["update"]().returncode == 0
    log_text = autolog(w)
    assert "已恢复（此前连续失败 35 次）" in log_text and "更新成功" in log_text
    assert srv_head(w) == new
    assert not (w["srv"] / "logs" / ".fetch_failures").exists()


def test_single_fetch_failure_then_success_leaves_no_trace(world):
    w = world
    good_url = sh("git remote get-url origin", w["srv"]).stdout.strip()
    sh("git remote set-url origin /nonexistent/repo.git", w["srv"])
    assert w["update"]().returncode == 0
    sh(f"git remote set-url origin {good_url}", w["srv"])
    assert w["update"]().returncode == 0
    assert autolog(w) == ""                             # 一次波动 + 恢复：日志文件保持干净
    assert not (w["srv"] / "logs" / ".fetch_failures").exists()
