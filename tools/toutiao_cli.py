#!/usr/bin/env python3
"""头条号草稿相关的运维工具（命令行）。

三个子命令，分别对应三种场景：

  dry     不联网、不开浏览器，只看「这篇稿子会被拆成什么样」
          —— 段落数、图片是本地还是外链、标题会不会被截断。
            先把内容问题排掉，再上浏览器，省得在头条页面上白等。

  login   本机开发用：开**有头**浏览器，人工扫码，扫完自动存登录态。
          服务器上不用这个（没显示器），直接在网页上点「扫码登录」——
          那边是无头浏览器 + 把二维码截图给前端。

  probe   校准用：登录后打开真实发文页，报告候选选择器命中情况和所有按钮文案，
          整页 HTML 存到 data/logs/toutiao_probe.html。头条改版时先跑这个，
          照着结果改 app/toutiao.py 顶部的选择器常量。

用法（在项目根目录）：
  python tools/toutiao_cli.py dry --article 12
  python tools/toutiao_cli.py dry --md data/_a1.md --title "标题"
  python tools/toutiao_cli.py login
  python tools/toutiao_cli.py probe
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, pipeline, toutiao  # noqa: E402


def cmd_dry(args: argparse.Namespace) -> int:
    title, md = args.title, ""
    if args.article:
        art = pipeline.get_article(args.article)
        title = title or (art.get("title") or "")
        md = art.get("markdown") or ""
    if args.md:
        md = Path(args.md).read_text(encoding="utf-8")
    if not md.strip():
        print("没有正文，用 --article 或 --md 指定一篇")
        return 1

    payload = toutiao.build_payload(md, title)
    print(f"标题：{payload['title']}（{len(payload['title'])} 字"
          f"{'，超长已截断' if payload['title_cut'] else ''}）")
    print(f"正文字数：{payload['words']}")
    print(f"块数：{len(payload['blocks'])}（文字 "
          f"{sum(1 for b in payload['blocks'] if b['type'] == 'text')} / 图片 "
          f"{len(payload['images'])}）")
    for i, b in enumerate(payload["blocks"], 1):
        if b["type"] == "text":
            first = b["text"].splitlines()[0][:40] if b["text"] else ""
            print(f"  {i:02d} 文字块  {len(b['html']):5d} 字节 HTML  「{first}…」")
        elif b.get("url"):
            print(f"  {i:02d} 图片    外链 {b['url'][:60]}")
        elif b.get("path"):
            print(f"  {i:02d} 图片    本地 {Path(b['path']).name}（要在头条页面上传）")
        else:
            print(f"  {i:02d} 图片    ⚠ 找不到文件：{b.get('src')}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    """有头浏览器扫码，扫到就存登录态。仅限有桌面的机器。"""
    ok, msg = toutiao.deps()
    if not ok:
        print(msg)
        return 1
    from playwright.sync_api import sync_playwright

    target = toutiao.state_file(args.account)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)
        ctx = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai",
                                  user_agent=toutiao.UA,
                                  viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(toutiao.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        print("浏览器已打开，请用今日头条 App 扫码。等登录成功…（Ctrl+C 退出）")
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            if toutiao._has_login_cookie(ctx):  # 内部函数，工具里用一下没关系
                page.wait_for_timeout(1500)
                ctx.storage_state(path=str(target))
                print(f"登录成功，登录态已存到 {target}")
                browser.close()
                return 0
            page.wait_for_timeout(1500)
        print(f"{args.timeout}s 内没等到登录，退出")
        browser.close()
    return 1


def cmd_probe(args: argparse.Namespace) -> int:
    try:
        res = toutiao.probe(args.account, headless=not args.headed)
    except toutiao.LoginRequired as exc:
        print(f"没登录：{exc}\n先跑 python tools/toutiao_cli.py login --account {args.account}")
        return 1
    except toutiao.ToutiaoError as exc:
        print(f"失败：{exc}")
        return 1
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res.get("need_login"):
        print("\n⚠ 页面跳到登录了，登录态已失效，先重新 login")
    elif res.get("wall"):
        kind, hit = res["wall"]
        print(f"\n⚠ 被人工关卡挡住：{kind}（页面原话：{hit}）")
        print("   绑手机号 / 实名认证这类只能人来，程序过不去；做完再投。")
    else:
        hit_t = [k for k, v in (res.get("title") or {}).items() if v]
        hit_b = [k for k, v in (res.get("body") or {}).items() if v]
        print("\n标题选择器命中：" + ("、".join(hit_t) if hit_t else "一个都没有 ⚠"))
        print("正文选择器命中：" + ("、".join(hit_b) if hit_b else "一个都没有 ⚠"))
        print(f"整页 HTML 已存：{res.get('html_saved')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="头条号草稿工具")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_dry = sub.add_parser("dry", help="只看拆块结果，不开浏览器")
    p_dry.add_argument("--article", type=int, default=0, help="工作台里的稿件 id")
    p_dry.add_argument("--md", default="", help="直接给一个 markdown 文件")
    p_dry.add_argument("--title", default="", help="标题（--md 时要给）")
    p_dry.set_defaults(func=cmd_dry)

    p_login = sub.add_parser("login", help="有头浏览器扫码登录（本机用）")
    p_login.add_argument("--account", type=int, default=0, help="头条号账号 id（工作台里那个）")
    p_login.add_argument("--timeout", type=int, default=300)
    p_login.set_defaults(func=cmd_login)

    p_probe = sub.add_parser("probe", help="打开真实发文页，核对选择器 / 看有没有人工关卡")
    p_probe.add_argument("--account", type=int, default=0, help="头条号账号 id")
    p_probe.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    p_probe.set_defaults(func=cmd_probe)

    args = ap.parse_args()
    db.init_db()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
