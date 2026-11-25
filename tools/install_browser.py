#!/usr/bin/env python3
"""在服务器上装 Playwright 的浏览器内核（走国内镜像）。

为什么需要它
------------
`python -m playwright install chromium` 从 cdn.playwright.dev 拉包，国内服务器实测只有
~120KB/s：headless shell 126MB + chrome 199MB，加起来要半小时以上，经常等到超时放弃。
这个脚本先问 playwright 自己要装什么（`install --dry-run` 会打印每个产物的 install
location 和 download url），再把 URL 换成 npmmirror / 阿里云镜像的同名产物，
解压到 playwright 期望的目录并写 `INSTALLATION_COMPLETE` 标记 —— 和官方安装器
落盘的结果一致（只是不校验官方签名，国内镜像站自己带 md5/etag）。

用法（在 /opt/wxwb 下）：
  ./venv/bin/python tools/install_browser.py            # 装 chromium + headless shell + ffmpeg
  ./venv/bin/python tools/install_browser.py --deps     # 顺手 apt 装系统库（无头运行需要）
  ./venv/bin/python tools/install_browser.py --check    # 只检查能不能起来，什么都不装
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

CFT_PREFIX = "https://cdn.playwright.dev/builds/cft/"
CFT_MIRROR = "https://registry.npmmirror.com/-/binary/chrome-for-testing/"
PW_MIRROR = "https://cdn.npmmirror.com/binaries/playwright/"
UA = "Mozilla/5.0 (X11; Linux x86_64) install_browser/1.0"


def mirror_url(url: str) -> str:
    """官方 URL → 国内镜像 URL。认不出来就原样返回（大不了一样的慢）。"""
    if url.startswith(CFT_PREFIX):                     # Chrome for Testing 构建
        return CFT_MIRROR + url[len(CFT_PREFIX):]
    if "/builds/" in url:                              # ffmpeg 之类 playwright 自家产物
        return PW_MIRROR + url[url.index("/builds/") + 1:]
    return url


def plan(py: str) -> list[dict[str, str]]:
    """问 playwright 要装哪些东西、装到哪、从哪下。"""
    out = subprocess.run([py, "-m", "playwright", "install", "--dry-run", "chromium"],
                         capture_output=True, text=True)
    text = (out.stdout or "") + (out.stderr or "")
    seen: set[str] = set()
    items: list[dict[str, str]] = []
    cur: dict[str, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if s and not line.startswith((" ", "\t")) and "(" in s and "v" in s:
            cur = {"name": s}
        elif s.startswith("Install location:") and cur:
            cur["dir"] = s.split(":", 1)[1].strip()
        elif s.startswith("Download url:") and cur:
            cur["url"] = s.split(":", 1)[1].strip()
            if cur.get("dir") and cur["dir"] not in seen:   # 同一产物可能被列多次
                seen.add(cur["dir"])
                items.append(cur)
            cur = {}
    return items


def already(dirpath: str) -> bool:
    return (Path(dirpath) / "INSTALLATION_COMPLETE").exists()


def find_exe(dest: Path) -> Path | None:
    """装完自检：里面有没有像样的可执行文件。结构不对时早点喊，别等 launch 报天书。"""
    names = {"chrome", "chrome-headless-shell", "ffmpeg-linux", "ffmpeg-win64.exe",
             "ffmpeg", "chrome-win64.exe", "chrome.exe", "winldd.exe", "winldd"}
    for p in dest.rglob("*"):
        if p.is_file() and p.name in names:
            return p
    return None


def download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if total and got / total < 1:
                pct = got * 100 // total
                mb = got / 1048576
                sys.stdout.write(f"\r    {pct:3d}%  {mb:7.1f} MB")
                sys.stdout.flush()
    dt = max(time.time() - t0, 0.001)
    print(f"\r    {got / 1048576:7.1f} MB  {got / dt / 1048576:6.1f} MB/s "
          f"（{dt:.0f}s）" + " " * 12)


def extract(zip_path: Path, dest: Path) -> None:
    """解压到 dest，落盘结构和官方安装器一致。

    关键：playwright 找的是 `<安装目录>/<顶层目录>/<可执行文件>`，比如
        chromium_headless_shell-1255/chrome-headless-shell-linux64/chrome-headless-shell
    所以带顶层目录的包要把**目录本身**搬进去，而不是把目录里的东西摊平
    （摊平过一次，playwright 直接报 "Executable doesn't exist"）。
    ffmpeg 那种根目录直接是文件的，就按文件搬。
    """
    dest.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(zip_path.parent)) as tmp:
        with zipfile.ZipFile(zip_path) as zf:
            # zipfile 默认不还原 Unix 权限位，chrome 解出来就没有 +x，
            # 之后 browser_type.launch 直接 "Permission denied"。手动按 external_attr 补。
            for info in zf.infolist():
                zf.extract(info, tmp)
                mode = (info.external_attr >> 16) & 0o777
                if mode and not info.is_dir():
                    try:
                        (Path(tmp) / info.filename).chmod(mode)
                    except OSError:
                        pass
        entries = list(Path(tmp).iterdir())
        top_dirs = [p for p in entries if p.is_dir()]
        loose = [p for p in entries if p.is_file()]
        if len(top_dirs) == 1 and not loose:
            src = top_dirs[0]
            target = dest / src.name
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)
            shutil.move(str(src), str(target))
        else:
            for p in entries:
                target = dest / p.name
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                elif target.exists():
                    target.unlink()
                shutil.move(str(p), str(target))
    chmod_exes(dest)


def chmod_exes(dest: Path) -> None:
    """兜底：该有执行权限的几个名字统一 +x（镜像打的包偶尔不带权限位）。"""
    names = {"chrome", "chrome-headless-shell", "ffmpeg-linux", "chrome_sandbox",
             "chrome-wrapper", "CHROME_HEADLESS_SHELL"}
    for p in dest.rglob("*"):
        if p.is_file() and p.name in names:
            try:
                p.chmod(p.stat().st_mode | 0o755)
            except OSError:
                pass


def probe(py: str) -> tuple[bool, str]:
    code = ("from playwright.sync_api import sync_playwright as s\n"
            "with s() as p:\n"
            "    b = p.chromium.launch(args=['--no-sandbox'])\n"
            "    v = b.version\n"
            "    b.close()\n"
            "print('chromium ok', v)\n")
    out = subprocess.run([py, "-c", code], capture_output=True, text=True)
    txt = ((out.stdout or "") + (out.stderr or "")).strip()
    return "chromium ok" in (out.stdout or ""), txt


def brief(text: str, keep: int = 3) -> str:
    """playwright 的报错框有半屏是 ╔═╗ 画出来的，只留真正有用的几行。"""
    lines = [l.strip() for l in (text or "").splitlines()]
    lines = [l for l in lines if l and not set(l) <= set("╔╗╚╝═║ ")]
    if not lines:
        return "（无输出）"
    if any("error while loading shared libraries" in l for l in lines):
        lines = [l for l in lines if "shared libraries" in l or ".so" in l] or lines
    return " | ".join(lines[-keep:])[:400]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deps", action="store_true", help="顺手 apt 装系统库")
    ap.add_argument("--check", action="store_true", help="只检查，不安装")
    ap.add_argument("--force", action="store_true", help="已装好的也重下（修坏了的时候用）")
    ap.add_argument("--python", default=sys.executable)
    args = ap.parse_args()
    py = args.python

    ok, msg = probe(py)
    if args.check:
        print(("✅ 能启动：" if ok else "❌ 起不来：") + msg.splitlines()[-1][:200])
        return 0 if ok else 1
    if ok:
        print("✅ chromium 已经能启动了，跳过：" + msg.splitlines()[-1][:120])
    else:
        items = plan(py)
        if not items:
            print("⚠ 读不到 playwright 的安装清单（版本太老？）直接试官方安装")
            subprocess.run([py, "-m", "playwright", "install", "chromium"], check=False)
        for it in items:
            dest = Path(it["dir"])
            name = it["name"].split("(")[0].strip()
            if already(it["dir"]) and not args.force:
                print(f"  {name}: 已装好，跳过")
                continue
            url = mirror_url(it["url"])
            print(f"  {name}: {url}")
            with tempfile.TemporaryDirectory() as tmp:
                zp = Path(tmp) / "pkg.zip"
                try:
                    download(url, zp)
                except Exception as exc:  # noqa: BLE001
                    print(f"    镜像下不来（{exc}），退回官方 {it['url']}")
                    download(it["url"], zp)
                extract(zp, dest)
            (dest / "INSTALLATION_COMPLETE").write_text("", encoding="utf-8")
            exe = find_exe(dest)
            print(f"    → {dest}（可执行文件：{exe.name if exe else '⚠ 没找到，结构可能不对'}）")
        ok, msg = probe(py)
        print(("✅ 装好了：" if ok else "❌ 还起不来：") + brief(msg))
        if not ok and not args.deps:
            print("  提示：加 --deps 补系统库（libnss3、字体之类），无头跑必需")
            return 1

    if args.deps:
        print("装系统库（apt，可能要一两分钟）…")
        out = subprocess.run([py, "-m", "playwright", "install-deps", "chromium"],
                             capture_output=True, text=True)
        tail = ((out.stdout or "") + (out.stderr or "")).strip().splitlines()[-3:]
        print("  " + "\n  ".join(tail) if tail else "  （无输出）")
        ok2, msg2 = probe(py)
        print(("✅ 系统库补齐后能启动：" if ok2 else "⚠ 仍然起不来：") + brief(msg2))
        return 0 if ok2 else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
