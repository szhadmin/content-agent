#!/usr/bin/env python3
"""可选：一键部署到自己的 VPS（systemd + Caddy 反代）。

这不是必须的——大多数人用 `docker compose up -d` 就够了（见 README）。
只有当你像我一样：一台裸机、用 Caddy 做反代、想直接推代码上去重启服务时，
这个脚本才省事。

用法（凭据一律走环境变量，不写在代码里）：

    export WXWB_SSH_HOST=your.server.com
    export WXWB_SSH_USER=root
    export WXWB_SSH_PASS='你的密码或令牌'
    python deploy/deploy.py
    python deploy/deploy.py --skip-caddy     # 只更新代码并重启

它会做这几件事（可重复执行，幂等）：
  1. 打包 app/ run.py requirements.txt tools/ → 上传 → 解压到 /opt/wxwb
  2. 建 venv，装依赖
  3. 写 systemd 单元并启动
  4. 在 Caddyfile 里给这个站点挂 /wxwb 反代（先备份 + validate，失败自动回滚）
"""
from __future__ import annotations

import argparse
import io
import os
import posixpath
import secrets
import string
import sys
import tarfile
import time
from pathlib import Path

import paramiko

HOST = os.environ.get("WXWB_SSH_HOST", "").strip()
USER = os.environ.get("WXWB_SSH_USER", "root").strip()
PASSWORD = os.environ.get("WXWB_SSH_PASS", "").strip()
if not HOST or not PASSWORD:
    raise SystemExit(
        "需要先设置 SSH 目标：\n"
        "  export WXWB_SSH_HOST=your.server.com\n"
        "  export WXWB_SSH_USER=root\n"
        "  export WXWB_SSH_PASS='你的密码或令牌'\n"
        "（凭据不要写进代码，也不要把这个脚本的输出贴到公开地方）")
REMOTE_DIR = "/opt/wxwb"
SERVICE = "wxwb"
PORT = 8899
PREFIX = "/wxwb"

LOCAL = Path(__file__).resolve().parent.parent
INCLUDE = ["app", "run.py", "requirements.txt", "tools"]


def build_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in INCLUDE:
            p = LOCAL / item
            if not p.exists():
                continue
            tar.add(p, arcname=item,
                    filter=lambda ti: None if "__pycache__" in ti.name or ti.name.endswith(".pyc") else ti)
    return buf.getvalue()


class Remote:
    def __init__(self) -> None:
        self.c = paramiko.SSHClient()
        self.c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        self.c.connect(HOST, username=USER, password=PASSWORD, timeout=25, banner_timeout=40)
        self.sftp = self.c.open_sftp()

    def run(self, cmd: str, timeout: int = 300, check: bool = True) -> str:
        _, out, err = self.c.exec_command(cmd, timeout=timeout)
        o = out.read().decode("utf-8", "replace")
        e = err.read().decode("utf-8", "replace")
        rc = out.channel.recv_exit_status()
        if check and rc != 0:
            raise RuntimeError(f"命令失败({rc}): {cmd}\n{o}\n{e}")
        return o + e

    def put(self, data: bytes, remote: str) -> None:
        with self.sftp.file(remote, "wb") as fh:
            fh.write(data)

    def close(self) -> None:
        self.sftp.close()
        self.c.close()


def read_remote(r: Remote, path: str) -> str:
    with r.sftp.file(path, "rb") as fh:
        return fh.read().decode("utf-8", "replace")


def caddy_patch(r: Remote) -> str:
    """在 IP 站点里插入 /wxwb 反代块。"""
    path = "/etc/caddy/Caddyfile"
    text = read_remote(r, path)
    if f"handle {PREFIX}*" in text:
        return "Caddy 已经配过 /wxwb，跳过"

    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("http://" + HOST):
            start = i
            break
    if start is None:
        return "⚠ Caddyfile 里没找到 http://%s 站点，没动它" % HOST

    depth = 0
    end = None
    for j in range(start, len(lines)):
        depth += lines[j].count("{") - lines[j].count("}")
        if depth == 0 and j > start:
            end = j
            break
    if end is None:
        return "⚠ Caddyfile 站点块没闭合，没动它"

    new_block = f"""http://{HOST} {{
\tencode gzip

\thandle {PREFIX}* {{
\t\treverse_proxy 127.0.0.1:{PORT}
\t}}

\thandle {{
\t\troot * /var/www/blog-vue
\t\tfile_server
\t}}

\theader {{
\t\tX-Frame-Options "SAMEORIGIN"
\t\tX-Content-Type-Options "nosniff"
\t\tReferrer-Policy "strict-origin-when-cross-origin"
\t}}
}}"""

    backup = f"/etc/caddy/Caddyfile.wxwb-backup.{int(time.time())}"
    new_text = "\n".join(lines[:start]) + "\n" + new_block + "\n" + "\n".join(lines[end + 1:])

    r.run(f"cp {path} {backup}")
    r.put(new_text.encode("utf-8"), path)
    out = r.run("caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1", check=False)
    if "Valid configuration" not in out and "valid" not in out.lower():
        r.run(f"cp {backup} {path}")
        return f"⚠ Caddy 校验失败，已回滚。原始输出：{out.strip()[:300]}"
    r.run("systemctl reload caddy")
    return "Caddy 已挂上 %s* → 127.0.0.1:%d（备份 %s）" % (PREFIX, PORT, backup)


def cdn_is_fast(r: Remote) -> bool:
    """官方 CDN 拉得动吗？拉不动就别让 playwright 慢慢磨。

    实测：国内服务器 cdn.playwright.dev 只有 ~120KB/s，headless shell 126MB + chrome
    199MB，半小时起步。镜像能到 12-15MB/s。下载地址问 playwright 自己要，别写死版本号。
    """
    py = f"{REMOTE_DIR}/venv/bin/python"
    url = r.run(f"{py} -m playwright install --dry-run chromium 2>&1 "
                "| grep -o 'https://cdn.playwright.dev/builds/cft/[^ ]*headless-shell[^ ]*' "
                "| head -1", timeout=90, check=False).strip()
    if not url:
        return False
    out = r.run(f'curl -s -L -m 10 -o /dev/null -w "%{{speed_download}}" -r 0-3000000 "{url}"',
                timeout=30, check=False)
    try:
        speed = float(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False
    print(f"      官方 CDN 实测 {speed / 1048576:.2f} MB/s")
    return speed > 2 * 1048576          # 2MB/s 以上才值得等


def ensure_playwright(r: Remote) -> str:
    """装 playwright + chromium（头条草稿要用）。

    幂等：能真的启动一次浏览器就直接跳过——这步要拉几百 MB，每次都重装没意义。
    装在 venv 里，内核落在 /root/.cache/ms-playwright（systemd 单元没有 User=，
    服务也是 root 跑，路径对得上）。
    """
    py = f"{REMOTE_DIR}/venv/bin/python"
    chk = f"{py} {REMOTE_DIR}/tools/install_browser.py --check"
    out = r.run(chk, timeout=120, check=False)
    if "✅" in out:
        return out.strip().splitlines()[-1]

    out = r.run(f"{py} -m pip install -q playwright 2>&1 | tail -2", timeout=600, check=False)
    print("      pip: " + (out.strip() or "ok"))

    if cdn_is_fast(r):
        r.run(f"timeout 900 {py} -m playwright install --with-deps chromium 2>&1 | tail -4",
              timeout=960, check=False)
        out = r.run(chk, timeout=120, check=False)
        if "✅" in out:
            return out.strip().splitlines()[-1]
        print("      官方源没装上，改走国内镜像")

    # 国内镜像：直接下 zip、解压到 playwright 的目录并写 INSTALLATION_COMPLETE
    out = r.run(f"{py} {REMOTE_DIR}/tools/install_browser.py --deps 2>&1 | tail -14",
                timeout=1200, check=False)
    print("      " + (out.strip().replace("\n", "\n      ") or "ok"))
    out = r.run(chk, timeout=120, check=False)
    return out.strip().splitlines()[-1] if out.strip() else "⚠ playwright 状态未知"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-caddy", action="store_true")
    ap.add_argument("--skip-playwright", action="store_true",
                    help="不装/不检查 playwright 和 chromium（头条草稿要用）")
    ap.add_argument("--password", default="", help="工作台管理员密码，留空则随机生成")
    ap.add_argument("--reset-password", default="",
                    help="强制把管理员密码重置成这个值（忘记密码时用）")
    args = ap.parse_args()

    r = Remote()
    try:
        print(f"[1/6] 打包并上传到 {HOST}:{REMOTE_DIR}")
        blob = build_tarball()
        r.put(blob, "/tmp/wxwb.tar.gz")
        r.run(f"mkdir -p {REMOTE_DIR}/data")
        r.run(f"tar -xzf /tmp/wxwb.tar.gz -C {REMOTE_DIR}")
        print(f"      上传 {len(blob)//1024} KB")

        print("[2/6] 准备虚拟环境")
        r.run(f"test -d {REMOTE_DIR}/venv || python3 -m venv --system-site-packages {REMOTE_DIR}/venv",
              timeout=180)
        r.run(f"{REMOTE_DIR}/venv/bin/pip install -q --upgrade pip 2>&1 | tail -1", check=False)
        out = r.run(f"{REMOTE_DIR}/venv/bin/pip install -q fastapi uvicorn python-multipart 2>&1 | tail -3",
                    timeout=600, check=False)
        print("      " + (out.strip() or "ok"))

        if args.skip_playwright:
            print("      playwright 跳过（--skip-playwright），头条草稿会不可用")
        else:
            print("      " + ensure_playwright(r))

        print("[3/6] 依赖自检")
        probe = r.run(
            f"{REMOTE_DIR}/venv/bin/python -c \""
            "import fastapi, uvicorn, markdown_it, PIL, jinja2, requests, multipart, cryptography;"
            "print('deps ok', fastapi.__version__, markdown_it.__version__)\"",
            check=False)
        print("      " + probe.strip())

        print("[4/6] 写 systemd 单元")
        unit = f"""[Unit]
Description=WeChat Official Account Agent Workbench
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory={REMOTE_DIR}
Environment=WXWB_DATA={REMOTE_DIR}/data
Environment=WXWB_HOST=127.0.0.1
Environment=WXWB_PORT={PORT}
Environment=WXWB_PREFIX={PREFIX}
Environment=PYTHONUNBUFFERED=1
ExecStart={REMOTE_DIR}/venv/bin/python {REMOTE_DIR}/run.py
Restart=always
RestartSec=3
StandardOutput=append:{REMOTE_DIR}/data/logs/service.log
StandardError=append:{REMOTE_DIR}/data/logs/service.log

[Install]
WantedBy=multi-user.target
"""
        r.run(f"mkdir -p {REMOTE_DIR}/data/logs")
        # 首次部署生成管理员密码；已存在则不覆盖
        pw_file = f"{REMOTE_DIR}/data/.admin_password"
        if args.reset_password:
            # 显式重置时，密码文件也要同步，避免以后自检拿旧值比对
            r.run(f"printf '%s' '{args.reset_password}' > {pw_file}")
        elif args.password:
            r.run(f"printf '%s' '{args.password}' > {pw_file}")
        else:
            r.run(f"test -f {pw_file} || "
                  f"printf '%s' \"$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 14)\" > {pw_file}")
        pw = (args.reset_password or r.run(f"cat {pw_file}").strip())
        unit = unit.replace("Environment=PYTHONUNBUFFERED=1",
                            f"Environment=PYTHONUNBUFFERED=1\nEnvironment=WXWB_ADMIN_PASSWORD={pw}")
        r.put(unit.encode("utf-8"), f"/etc/systemd/system/{SERVICE}.service")

        print("[5/6] 启动服务")
        r.run("systemctl daemon-reload")
        r.run(f"systemctl enable {SERVICE} >/dev/null 2>&1 || true")
        r.run(f"systemctl restart {SERVICE}")
        time.sleep(4)
        status = r.run(f"systemctl is-active {SERVICE}", check=False).strip()
        print(f"      服务状态: {status}")
        print("      " + r.run(f"curl -s -o /dev/null -w 'local /healthz -> %{{http_code}}\\n' "
                               f"http://127.0.0.1:{PORT}/healthz", check=False).strip())

        # 密码自检：库里的 hash 和 .admin_password 对不上时提示（不自作主张改）
        if args.reset_password:
            out = r.run(f"{REMOTE_DIR}/venv/bin/python {REMOTE_DIR}/tools/reset_admin.py "
                        f"'{args.reset_password}' 2>&1", check=False)
            print("      已重置管理员密码：" + out.strip())
        else:
            out = r.run(f"{REMOTE_DIR}/venv/bin/python {REMOTE_DIR}/tools/reset_admin.py "
                        f"'{pw}' --if-mismatch 2>&1", check=False)
            print("      密码自检：" + out.strip())

        print("[6/6] 反代")
        if args.skip_caddy:
            print("      跳过（--skip-caddy）")
        else:
            print("      " + caddy_patch(r))

        print("      " + r.run(
            f"curl -s -o /dev/null -w 'public {PREFIX}/login -> %{{http_code}}\\n' "
            f"http://127.0.0.1{PREFIX}/login", check=False).strip())

        print()
        print("=" * 64)
        print(f"  访问地址: http://{HOST}{PREFIX}/")
        print(f"  用户名  : admin")
        print(f"  密码    : {pw}")
        print(f"  数据目录: {REMOTE_DIR}/data （数据库、图片、封面、日志）")
        print(f"  日志    : tail -f {REMOTE_DIR}/data/logs/service.log")
        print("=" * 64)
    finally:
        r.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        print(f"部署失败: {exc}", file=sys.stderr)
        sys.exit(1)
