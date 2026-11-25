"""头条号（今日头条）草稿投递。

为什么不用接口
--------------
头条号没有对外的发文/草稿 API。后台发文页走的是自己的 agw 接口，除了登录 Cookie，
还要 `x-secsdk-csrf-token` 之类的头，而且这些要求跟着前端版本变——写死等于三个月后
自己炸。所以这里驱动真浏览器：人工扫码登录一次 → 把 storage_state 存下来 → 之后
无头浏览器按 UI 操作，最后点「存草稿」。

三条铁律（和微信那条链保持一致）
----------------------------
1. **只存草稿，绝不点「预览并发布」**。发布永远留给人。
2. 选择器失配就报错 + 存现场截图，不许静默当成成功。
3. 登录态失效单独抛 LoginRequired，前端据此弹二维码，不混进普通错误里。

对外只有四个入口
----------------
    deps()                 playwright 装了没
    login_status()         登录态 / 扫码进度（前端轮询）
    start_login()          起一个后台浏览器等着扫码
    create_draft(article)  真正干活：把稿子填进发文页并存草稿
另有两个运维工具函数 build_payload()（纯计算，dry-run 用）和 probe()（校准选择器用）。
"""
from __future__ import annotations

import json
import os
import platform
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode

from . import db, render
from .settings import DATA_DIR, UPLOAD_DIR

MP = "https://mp.toutiao.com"
LOGIN_URL = f"{MP}/auth/page/login"
PUBLISH_URL = f"{MP}/profile_v4/graphic/publish"
WORKBENCH_URL = f"{MP}/profile_v4/index"

STATE_PATH = DATA_DIR / "toutiao_state.json"      # 老的单账号登录态，只用来迁移
QR_PATH = UPLOAD_DIR / "_toutiao_qr.png"
TITLE_MAX = 30          # 头条文章标题上限
SHOT_KEEP = 12          # 现场截图最多留几张

# 登录态用「持久化浏览器 profile」而不是 storage_state。
# 为什么：storage_state 只存 cookies + localStorage，**不含 IndexedDB**。
# 头条的保存接口返回 7050「保存失败」很可能是环境层面的风控在作怪
# （同样的内容、同样的必填项，人在浏览器里点就能存，脚本就是不行）。
# 持久化 profile 会把 IndexedDB / Cache / 指纹相关状态全部留在磁盘上，
# 是当前最接近「真实浏览器」的做法。
PROFILE_ROOT = DATA_DIR / "tt_profiles"

# 头条对无头浏览器的检测比公众号那套积极，能关的特征尽量关掉。
LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--no-sandbox",
]
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# 候选选择器按「越靠前越可能是真的」排；失配时会把这些都写进错误里，方便照着改。
TITLE_SELECTORS = [
    'textarea[placeholder*="标题"]',
    'input[placeholder*="标题"]',
    '.article-title textarea',
    '.title-input textarea',
    '.publish-title textarea',
    'div[contenteditable="true"][data-placeholder*="标题"]',
]
BODY_SELECTORS = [
    '.ProseMirror',
    'div.ProseMirror[contenteditable="true"]',
    '.ql-editor',
    '.article-editor div[contenteditable="true"]',
    'div[contenteditable="true"]',
]
TOAST_SELECTORS = [
    '[class*="toast"]',
    '[class*="Toast"]',
    '[class*="message"]',
    '[class*="Message"]',
    '[role="alert"]',
]
QR_SELECTORS = [
    'img[src^="data:image"]',
    'canvas',
    '[class*="qrcode"] img',
    '[class*="qrCode"] img',
    '[class*="scan"] img',
    '[class*="code"] img',
]
DRAFT_BUTTON_TEXTS = ("存草稿", "保存草稿", "存为草稿", "存入草稿")
IMAGE_BUTTON_SELECTORS = [
    'button:has-text("图片")',
    '[class*="toolbar"] [class*="image"]',
    'input[type="file"][accept*="image"]',
    '[class*="upload"] input[type="file"]',
]
SAVED_HINTS = ("保存成功", "已保存", "已存入草稿", "草稿保存成功", "保存草稿成功")

# 登录之后的人工地雷：命中就得人去做，程序没法替
WALLS = [
    ("bind_phone", ("手机绑定", "绑定手机", "请绑定手机号")),
    ("verify", ("实名认证", "身份认证", "资质认证")),
]

_img_line = re.compile(r"^\s*!\[([^\]]*)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)\s*$")

# 字节组件库（byte-*）的弹窗关闭入口。头条进发文页常弹新手引导/公告，
# 它 aria-hidden="true" 但整层盖住在页面上，playwright 点任何元素都会报
# "intercepts pointer events"（实测踩过：标题框定位到了但点不进去）。
CLOSE_SELECTORS = [
    ".byte-modal-close-icon",
    ".byte-modal-close-btn",
    ".byte-modal-wrapper [class*='close']",
    '[aria-label="close"]',
    '[aria-label="关闭"]',
    'button:has-text("我知道了")',
    'button:has-text("知道了")',
    'button:has-text("不再提示")',
    'button:has-text("确定")',
    'button:has-text("关闭")',
]


class ToutiaoError(RuntimeError):
    """头条这边任何一步没走通，都抛这个（带可读原因）。"""


class LoginRequired(ToutiaoError):
    """登录态没有或已过期，前端据此弹二维码。"""


class ManualActionRequired(ToutiaoError):
    """登录了，但头条卡在人工步骤上（绑手机、实名认证这类）。

    这种卡点程序过不去——要短信验证码、要身份证，只能人来。所以单独一类，
    前端要显示成「去处理」而不是「出错了」。
    """

    def __init__(self, kind: str, message: str):
        self.kind = kind          # bind_phone | verify | other
        super().__init__(message)


# --------------------------------------------------------------------------
# 依赖 / 登录态
# --------------------------------------------------------------------------

def deps() -> tuple[bool, str]:
    """playwright 装了没。浏览器内核没装的情况下要到第一次启动才炸，这里管不了。"""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "服务器没装 playwright：pip install playwright && python -m playwright install chromium"
    return True, ""


def state_file(account_id: int | str = 0) -> Path:
    """每个头条号账号一份登录态。平台分开了，登录态也得分开。"""
    return DATA_DIR / f"toutiao_state_{account_id or 'default'}.json"


def profile_dir(account_id: int | str = 0) -> Path:
    """持久化浏览器 profile 目录（cookies + localStorage + IndexedDB 全在里面）。"""
    return PROFILE_ROOT / f"acc_{account_id or 'default'}"


def profile_ready(account_id: int | str = 0) -> bool:
    """profile 是否已经登录过（有 cookies 文件就算建过了）。"""
    d = profile_dir(account_id)
    if not d.exists():
        return False
    return (d / "Default").exists() or any(d.iterdir())


def _profile_expiry(account_id: int | str = 0) -> float:
    """从 profile 的 Cookies SQLite 里读 sessionid 的过期时间。

    Chromium 把 cookie 存在 <profile>/Default/Cookies，expires_utc 是
    「1601-01-01 起的微秒数」。这里不解密值（也用不着），只看有没有、什么时候过期。
    """
    import sqlite3

    p = profile_dir(account_id) / "Default" / "Cookies"
    if not p.exists():
        return -1.0
    best = -1.0
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        rows = con.execute(
            "SELECT name, expires_utc FROM cookies WHERE name LIKE 'sessionid%'").fetchall()
        con.close()
    except Exception:  # noqa: BLE001
        return -1.0
    now = time.time()
    for _name, exp in rows:
        try:
            ts = float(exp) / 1_000_000 - 11644473600 if exp else 0
        except (TypeError, ValueError):
            continue
        if ts > now and (best < 0 or ts < best):
            best = ts
    return best


def _make_context(pw: Any, account_id: int | str = 0, *, headless: bool = True,
                  use_state_fallback: bool = True) -> tuple[Any, Any]:
    """统一开浏览器的入口，返回 (context, browser_or_None)。

    优先用持久化 profile；没有 profile 时才退回老的 storage_state（兼容旧登录态）。
    注意 persistent context 没有 browser 对象，关闭时只 close context 即可。
    """
    if profile_ready(account_id):
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir(account_id)),
            headless=headless, args=LAUNCH_ARGS,
            locale="zh-CN", timezone_id="Asia/Shanghai", user_agent=UA,
            viewport={"width": 1440, "height": 1100},
        )
        return ctx, None

    browser = pw.chromium.launch(headless=headless, args=LAUNCH_ARGS)
    kwargs: dict[str, Any] = {}
    if use_state_fallback and state_file(account_id).exists():
        kwargs["storage_state"] = str(state_file(account_id))
    ctx = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai",
                              user_agent=UA, viewport={"width": 1440, "height": 1100},
                              **kwargs)
    return ctx, browser


def _first_page(ctx: Any) -> Any:
    """persistent context 通常会自带一个空白页，复用它，别开新的。"""
    page = None
    try:
        pages = ctx.pages
        if pages:
            page = pages[0]
    except Exception:  # noqa: BLE001
        pass
    if page is None:
        page = ctx.new_page()
    _apply_client_hints(ctx, page)
    return page


# --------------------------------------------------------------------------
# 反爬关键：抹掉 HeadlessChrome 客户端提示
# --------------------------------------------------------------------------
# 头条服务端风控在读 `sec-ch-ua` 这个请求头。带 `"HeadlessChrome"` 时，
# 保存草稿接口（POST /mp/agw/article/publish，save=0）一律返回
# `{"code":7050,"message":"保存失败"}`——不管内容多完整、实名与否、必填项是否满足。
# 这个头是浏览器在网络层发的，JS 改不了（navigator.userAgent 改了也没用），
# 只能用 CDP 的 Network.setUserAgentOverride 连同 userAgentMetadata 一起覆盖。
# 换掉之后同样的操作 2 秒内就返回 `{"code":0,...}` 保存成功。
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/159.0.0.0 Safari/537.36")

_UA_META = {
    "brands": [
        {"brand": "Google Chrome", "version": "159"},
        {"brand": "Not/A)Brand", "version": "8"},
        {"brand": "Chromium", "version": "159"},
    ],
    "fullVersionList": [
        {"brand": "Google Chrome", "version": "159.0.8154.6"},
        {"brand": "Not/A)Brand", "version": "8.0.0.0"},
        {"brand": "Chromium", "version": "159.0.8154.6"},
    ],
    "fullVersion": "159.0.8154.6",
    "platform": "Windows",
    "platformVersion": "15.0.0",
    "architecture": "x86",
    "model": "",
    "mobile": False,
    "bitness": "64",
    "wow64": False,
}


def _apply_client_hints(ctx: Any, page: Any) -> bool:
    """把 sec-ch-ua 从 HeadlessChrome 改成 Google Chrome。成功返回 True。"""
    try:
        cdp = ctx.new_cdp_session(page)
        cdp.send("Network.setUserAgentOverride", {
            "userAgent": CHROME_UA,
            "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
            "platform": "Win32",
            "userAgentMetadata": _UA_META,
        })
        return True
    except Exception:  # noqa: BLE001
        return False


def read_state(account_id: int | str = 0) -> dict[str, Any] | None:
    _adopt_legacy(account_id)
    path = state_file(account_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _adopt_legacy(account_id: int | str) -> None:
    """老版本只有一个 toutiao_state.json，第一次用到时认领给这个账号，别让人重扫。

    注意 `toutiao_state_default.json` 是 account_id=0 的临时兜底（页面查登录态时用过），
    不能因为它存在就判定"已经分账号存过了"，否则真实账号永远认领不到。
    """
    target = state_file(account_id)
    if target.exists() or not STATE_PATH.exists():
        return
    others = [p for p in DATA_DIR.glob("toutiao_state_*.json")
              if p.name != "toutiao_state_default.json" and p != target]
    if others:                       # 已经有别的账号的登录态了，不猜
        return
    try:
        shutil.copy(STATE_PATH, target)
    except OSError:
        return
    if str(account_id) != "default" and account_id:
        # 真实账号接过去了，删掉那份兜底，免得列表上两个地方都显示"已登录"
        try:
            state_file(0).unlink()
        except OSError:
            pass


def _cookie_expiry(state: dict[str, Any]) -> float:
    """取登录 Cookie 里最早过期的那个时间戳（秒）。-1 表示拿不到（会话级 Cookie）。"""
    now = time.time()
    best = -1.0
    for c in state.get("cookies") or []:
        name = c.get("name") or ""
        if name not in ("sessionid", "sessionid_ss", "sid_tt", "sid_guard"):
            continue
        exp = c.get("expires")
        if not exp or exp < 0:
            continue
        if exp > now and (best < 0 or exp < best):
            best = exp
    return best


def _fmt_ts(ts: float) -> str:
    if ts <= 0:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def logout(account_id: int | str = 0) -> None:
    """删掉这个头条号账号在本机的登录态（含浏览器 profile）。头条那边不会掉线。"""
    for p in (state_file(account_id),):
        try:
            p.unlink()
        except OSError:
            pass
    d = profile_dir(account_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------
# 扫码登录（后台线程 + 二维码截图给前端）
# --------------------------------------------------------------------------

_login_lock = threading.Lock()
_login: dict[str, Any] = {
    "state": "idle",       # idle | starting | waiting | ok | timeout | error | cancelled
    "message": "",
    "started": 0.0,
    "stop": None,
    "thread": None,
    "account_id": 0,
}


def _set_login(state: str, message: str = "") -> None:
    with _login_lock:
        _login["state"] = state
        _login["message"] = message


def _login_snapshot() -> dict[str, Any]:
    with _login_lock:
        th = _login.get("thread")
        return {
            "state": _login["state"],
            "message": _login["message"],
            "running": bool(th and th.is_alive()),
            "elapsed": int(time.time() - _login["started"]) if _login["started"] else 0,
            # 必须带出去：login_status 靠它判断「这张二维码是哪个账号在扫」，
            # 少了它 qr 永远返回空（踩过这个坑）。
            "account_id": _login.get("account_id") or 0,
        }


def _has_login_cookie(ctx: Any) -> bool:
    try:
        for c in ctx.cookies():
            if c.get("name") in ("sessionid", "sessionid_ss") and (c.get("value") or ""):
                return True
    except Exception:  # noqa: BLE001 - 浏览器可能已经关了
        return False
    return False


def _dismiss_dialogs(page: Any, say: Callable[[str], None] | None = None,
                     escape: bool = False) -> int:
    """关掉盖在发文页上的弹窗，返回关掉了几个。

    字节的 `byte-modal` 有个特别坑的地方：它可能 `aria-hidden="true"`（语义上已经"隐藏"），
    但 DOM 和遮罩层还在，z-index 盖住整页。这时 playwright 所有 click 都会卡在
    "element intercepts pointer events" 上重试到超时。

    所以三重保险：① 点关闭按钮 ② JS 直接把残留遮罩摘掉。

    ⚠️ `escape` 默认 **False**，别随便改。在头条编辑器里按 ESC 会让页面进入「放弃编辑」
    状态：之后它发出的自动保存请求里 **content 是空的**，草稿箱里就留下一篇
    「只有标题、正文空白」的稿子（用户实际遇到过，排查了很久）。
    只有明确在"还没开始写字"的阶段（比如登录页、刚打开发文页）才传 escape=True。
    """
    closed = 0
    for _ in range(4):                       # 可能叠了不止一层弹窗
        hit = False
        for sel in CLOSE_SELECTORS:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=600):
                    loc.click(timeout=2500, force=True)
                    page.wait_for_timeout(350)
                    closed += 1
                    hit = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not hit:
            break

    if escape:
        try:
            page.keyboard.press("Escape")
            page.wait_for_timeout(250)
        except Exception:  # noqa: BLE001
            pass

    # 兜底：动画跑完后仍挂在 DOM 上的遮挡层，直接摘掉（不点，避免动画卡住）。
    # 优先只摘 aria-hidden="true" 的僵尸层（用户报错里正是这种），
    # 真正的业务弹窗 aria-hidden 不是 true，不会被误删。
    try:
        removed = page.evaluate("""() => {
            const kill = sel => document.querySelectorAll(sel).forEach(el => {
                if (!el || !el.parentNode) return;
                el.parentNode.removeChild(el);
                window.__ttRemoved = (window.__ttRemoved || 0) + 1;
            });
            window.__ttRemoved = 0;
            ['.byte-modal-wrapper', '.byte-modal-mask', '[class*="zoomModal"]'].forEach(s => {
                document.querySelectorAll(s + '[aria-hidden="true"]').forEach(el => {
                    if (el.parentNode) { el.parentNode.removeChild(el); window.__ttRemoved++; }
                });
            });
            // 还有遮罩残留（aria-hidden 没写或为 false 但盖住了）才动刀
            if (document.querySelector('.byte-modal-mask')) {
                kill('.byte-modal-mask');
            }
            document.body.style.overflow = '';
            document.documentElement.style.overflow = '';
            return window.__ttRemoved;
        }""")
        if removed:
            closed += int(removed)
            page.wait_for_timeout(200)
    except Exception:  # noqa: BLE001
        pass

    if closed and say:
        say(f"关掉 {closed} 个遮挡弹窗")
    return closed


def _safe_fill(loc: Any, value: str, say: Callable[[str], None]) -> None:
    """填输入框：正常填 → 失败就清弹窗再 force 填。头条弹窗挡住了就靠这个兜住。"""
    try:
        loc.fill(value, timeout=6000)
        return
    except Exception:  # noqa: BLE001
        say("输入框填不进去，清一次弹窗再试")
    try:
        loc.fill(value, timeout=6000, force=True)
        return
    except Exception:  # noqa: BLE001
        pass
    # 最后一招：绕过 playwright 的可见性检查，直接走 DOM
    loc.evaluate("(el, v) => { el.value = v; el.dispatchEvent(new Event('input', {bubbles:true})); }", value)


def _safe_click(loc: Any, say: Callable[[str], None], what: str = "元素") -> None:
    """点元素：正常点 → 失败清弹窗再 force 点。"""
    try:
        loc.click(timeout=6000)
        return
    except Exception:  # noqa: BLE001
        say(f"{what}点不进去，清弹窗后强制点")
    try:
        loc.click(timeout=6000, force=True)
        return
    except Exception as exc:  # noqa: BLE001
        raise ToutiaoError(f"{what}始终点不进去（可能被弹窗挡住）：{exc}") from exc


def _check_account_ready(page: Any) -> tuple[bool, str, str]:
    """发文前先查账号资质，返回 (就绪?, 关卡类型, 详情)。

    为什么必须在填内容**之前**查：头条没实名认证时，正文填完也只会收到
    `{"code":7050,"message":"保存失败"}`——一个完全看不出原因的错误码。
    与其让用户白等一遍填稿，不如进来先问一次。

    用的接口和页面同源（`jingxuan_account_check`），返回里
    `verified.bind_mobile / real_name / finish` 就是资质状态：
      · 没绑手机 → bind_phone，要短信验证码
      · 绑了手机但 real_name 空 / finish=false → verify，要身份证+人脸
    查不到（接口改了/网络抖动）就当就绪，让后面的流程自己报错，不误拦。
    """
    try:
        raw = page.evaluate("""async () => {
            const r = await fetch('/mp/agw/mass_profit/jingxuan_account_check',
                                  {credentials: 'include'});
            return await r.text();
        }""")
        d = json.loads(raw)
        v = ((d.get("data") or {}).get("verified") or {})
        mobile = (v.get("bind_mobile") or "").strip()
        real = (v.get("real_name") or "").strip()
        finished = bool(v.get("finish"))
        if not mobile:
            return False, "bind_phone", "没绑手机号"
        if not real or not finished:
            return False, "verify", "没完成实名认证"
        return True, "", ""
    except Exception:  # noqa: BLE001
        return True, "", ""


def _list_drafts(page: Any) -> list[dict[str, Any]] | None:
    """调头条草稿箱列表接口，拿当前草稿（用来确认真的存上了）。失败返回 None。"""
    try:
        raw = page.evaluate("""async () => {
            const r = await fetch('/mp/agw/creator_center/draft_list?type=2&count=20&app_id=1231',
                                  {credentials: 'include'});
            return await r.text();
        }""")
        d = json.loads(raw)
        items = d.get("draft_list")
        return items if isinstance(items, list) else []
    except Exception:  # noqa: BLE001
        return None


def _draft_ids(items: list[dict[str, Any]]) -> set[str]:
    """草稿的唯一 id 集合。注意：草稿列表里真正的 id 字段叫 `gid`
    （不是 pgc_id / item_id），漏了它会导致「明明存上了却永远判定没存上」。"""
    out = set()
    for it in items or []:
        for k in ("gid", "pgc_id", "id", "item_id", "group_id"):
            v = it.get(k)
            if v:
                out.add(str(v))
    return out


def _first_visible(page: Any, selectors: list[str], timeout: int = 800) -> Any:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                return loc
        except Exception:  # noqa: BLE001
            continue
    return None


def _click_scan_tab(page: Any) -> None:
    """登录页有时默认是密码登录，先切到扫码。切不到就照原样截，不报错。"""
    for text in ("扫码登录", "二维码登录", "扫码"):
        try:
            loc = page.get_by_text(text, exact=False).first
            if loc.is_visible(timeout=1500):
                loc.click()
                page.wait_for_timeout(800)
                return
        except Exception:  # noqa: BLE001
            continue


def _grab_qr(page: Any) -> bool:
    """把二维码抠出来存成 png 给前端显示。

    关键坑：头条登录页的二维码 `<img>` 是**异步生成**的，加载中会先显示一个"刷新圆圈"
    占位图标（src 也确实是 data:image），等真正的 base64 二维码图出来后才替换 src。
    所以不能只看 src 以 data:image 开头就截——那样截到的就是占位图，扫不了。
    必须等 naturalWidth 足够大（真码 512×512，占位图很小）且 src 内容足够长。
    """
    # 先精确定位真正的二维码 img（class 稳定），等它加载完成
    deadline = time.time() + 12
    while time.time() < deadline:
        try:
            info = page.evaluate("""() => {
                const im = document.querySelector(
                    'img.web-login-scan-code__content__qrcode-wrapper__qrcode,'
                    + 'img[class*="qrcode-wrapper__qrcode"],'
                    + '[class*="qrcode"] img[src^="data:image"]');
                if (!im) return null;
                return {
                    src_len: (im.src||'').length,
                    nw: im.naturalWidth, nh: im.naturalHeight,
                    complete: im.complete
                };
            }""")
            # 真码：base64 很长（512×512 PNG 至少几 KB）且 naturalWidth ≥ 256
            if info and info.get("complete") and info.get("nw", 0) >= 256 and info.get("src_len", 0) > 2000:
                loc = page.locator(
                    'img.web-login-scan-code__content__qrcode-wrapper__qrcode,'
                    'img[class*="qrcode-wrapper__qrcode"]').first
                loc.screenshot(path=str(QR_PATH))
                return True
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(700)
    return False


def _login_worker(stop: threading.Event, timeout: float, account_id: int | str = 0) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        _set_login("error", deps()[1])
        return
    try:
        with sync_playwright() as pw:
            # 用**持久化 profile** 登录：扫码成功后，头条会把它自己的一整套状态
            # （cookies / localStorage / IndexedDB / 各种风控 token）写进这个目录。
            # 只存 storage_state 的话 IndexedDB 会丢，后面存草稿就可能被判 7050。
            prof = profile_dir(account_id)
            prof.mkdir(parents=True, exist_ok=True)
            ctx = pw.chromium.launch_persistent_context(
                user_data_dir=str(prof),
                headless=True, args=LAUNCH_ARGS,
                locale="zh-CN", timezone_id="Asia/Shanghai", user_agent=UA,
                viewport={"width": 1440, "height": 900},
                device_scale_factor=2,   # 二维码截出来才够清晰好扫
            )
            page = _first_page(ctx)
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            _click_scan_tab(page)
            _set_login("waiting", "用今日头条 App 扫下面这个码")

            deadline = time.time() + timeout
            last_qr = 0.0
            done = False
            while time.time() < deadline and not stop.is_set():
                if _has_login_cookie(ctx):
                    page.wait_for_timeout(2500)          # 等头条把 cookie/IndexedDB 写全
                    try:
                        # 顺带存一份 storage_state 做备份（profile 才是主用）
                        ctx.storage_state(path=str(state_file(account_id)))
                    except Exception:  # noqa: BLE001
                        pass
                    _set_login("ok", "登录成功，登录态已保存")
                    done = True
                    break
                if time.time() - last_qr > 3.0:          # 二维码会过期自动刷新，定期重截
                    got = _grab_qr(page)
                    # 还没截到真码时，别让前端继续显示上一次那张占位图/旧码
                    if not got:
                        try:
                            QR_PATH.unlink()
                        except OSError:
                            pass
                        _set_login("waiting", "二维码加载中…")
                    else:
                        _set_login("waiting", "用今日头条 App 扫下面这个码")
                    last_qr = time.time()
                page.wait_for_timeout(1500)
            if not done:
                if stop.is_set():
                    _set_login("cancelled", "已取消扫码")
                else:
                    _set_login("timeout", f"{int(timeout)} 秒内没扫上码，重新点一次「扫码登录」")
            try:
                ctx.close()          # 关掉时 profile 会自动落盘
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        _set_login("error", f"打开头条登录页失败：{exc}")


def start_login(account_id: int | str = 0, timeout: float = 240.0) -> dict[str, Any]:
    """起后台浏览器等扫码。已经在等的话直接返回当前进度，不叠开第二个。"""
    ok, msg = deps()
    if not ok:
        raise ToutiaoError(msg)
    snap = _login_snapshot()
    if snap["running"] and snap["state"] in ("starting", "waiting"):
        same = str(snap.get("account_id") or 0) == str(account_id or 0)
        if same:
            return snap
        # 正在给别的账号扫：先把那个停掉，否则两个浏览器抢同一个二维码文件
        with _login_lock:
            stop = _login.get("stop")
        if stop is not None:
            stop.set()
        time.sleep(0.6)
    stop = threading.Event()
    try:
        QR_PATH.unlink()
    except OSError:
        pass
    with _login_lock:
        _login.update(state="starting", message="正在打开头条登录页…",
                      started=time.time(), stop=stop, account_id=account_id)
        th = threading.Thread(target=_login_worker, args=(stop, timeout, account_id),
                              daemon=True)
        _login["thread"] = th
        th.start()
    return _login_snapshot()


def cancel_login() -> dict[str, Any]:
    with _login_lock:
        stop = _login.get("stop")
    if stop is not None:
        stop.set()
    return _login_snapshot()


def login_status(account_id: int | str = 0) -> dict[str, Any]:
    """给前端看的完整状态：装了没 / 扫到哪一步 / 登录态什么时候过期。"""
    ok, msg = deps()
    snap = _login_snapshot()
    state = read_state(account_id)
    has_profile = profile_ready(account_id)
    # 优先看持久化 profile 里的 cookie 过期时间；没有 profile 才看老 state 文件
    expiry = _profile_expiry(account_id) if has_profile else -1
    if expiry <= 0 and state:
        expiry = _cookie_expiry(state)
    info: dict[str, Any] = {
        "account_id": account_id,
        "installed": ok,
        "dep_error": "" if ok else msg,
        "logged_in": False,
        "mode": "profile" if has_profile else ("state" if state else ""),
        "saved_at": "",
        "expires_at": "",
        "expired": False,
        "login": snap,
        "qr": "",
    }
    if has_profile:
        try:
            info["saved_at"] = _fmt_ts(profile_dir(account_id).stat().st_mtime)
        except OSError:
            pass
        info["logged_in"] = expiry <= 0 or expiry > time.time()
        info["expires_at"] = _fmt_ts(expiry)
        info["expired"] = bool(expiry > 0 and expiry < time.time())
    elif state:
        try:
            info["saved_at"] = _fmt_ts(state_file(account_id).stat().st_mtime)
        except OSError:
            pass
        info["expires_at"] = _fmt_ts(expiry)
        info["expired"] = bool(expiry > 0 and expiry < time.time())
        info["logged_in"] = not info["expired"]
    # 二维码只给正在扫的那个账号看
    if (snap["state"] in ("starting", "waiting")
            and str(snap.get("account_id") or 0) == str(account_id or 0)
            and QR_PATH.exists()):
        info["qr"] = f"/media/uploads/{QR_PATH.name}?v={int(QR_PATH.stat().st_mtime)}"
    return info


# --------------------------------------------------------------------------
# 稿子 → 头条草稿的内容（纯计算，不起浏览器）
# --------------------------------------------------------------------------

def _local_path(src: str) -> Path | None:
    """markdown 里的图片地址 → 本地文件。外链返回 None。"""
    if not src or src.startswith(("http://", "https://", "data:")):
        return None
    name = src.split("?")[0].rstrip("/").split("/")[-1]
    for base in (UPLOAD_DIR, UPLOAD_DIR.parent, DATA_DIR / "articles"):
        cand = base / name
        if cand.exists():
            return cand
    return None


def _plain(md: str) -> str:
    """粘贴失败时用来兜底的纯文本：粗暴去标记，别指望好看。"""
    out = []
    for line in (md or "").splitlines():
        s = line.strip()
        if not s:
            continue
        s = re.sub(r"^#{1,6}\s*", "", s)
        s = re.sub(r"^>\s*", "", s)
        s = re.sub(r"^[-*+]\s+", "", s)
        s = s.replace("**", "").replace("__", "")
        out.append(s)
    return "\n".join(out)


def build_payload(markdown: str, title: str = "", *, theme: str = "default",
                  font_size: str = "medium") -> dict[str, Any]:
    """把 markdown 切成「文字块 / 图片块」交替的序列。

    为什么要切：图片得就地处理（公网图直接粘 <img>，本地图要传头条图床），
    所以不能把整篇 HTML 一次性糊进去。文字块保留排版（h2/引用/列表都在）。
    """
    blocks: list[dict[str, Any]] = []
    buf: list[str] = []

    def flush() -> None:
        if not buf:
            return
        md = "\n".join(buf).strip()
        buf.clear()
        if not md:
            return
        blocks.append({
            "type": "text",
            "md": md,
            "html": render.render_markdown(md, theme=theme, font_size=font_size),
            "text": _plain(md),
        })

    for line in (markdown or "").splitlines():
        m = _img_line.match(line)
        if not m:
            buf.append(line)
            continue
        flush()
        alt, src = m.group(1).strip(), m.group(2).strip()
        if src.startswith(("http://", "https://")):
            blocks.append({"type": "image", "alt": alt, "src": src, "url": src, "path": ""})
        else:
            p = _local_path(src)
            blocks.append({"type": "image", "alt": alt, "src": src, "url": "",
                           "path": str(p) if p else ""})
    flush()

    used_title = (title or "").strip()
    return {
        "title": used_title[:TITLE_MAX],
        "title_cut": len(used_title) > TITLE_MAX,
        "blocks": blocks,
        "images": [b for b in blocks if b["type"] == "image"],
        "words": len(re.sub(r"\s", "", markdown or "")),
    }


# --------------------------------------------------------------------------
# 浏览器操作的小工具
# --------------------------------------------------------------------------

def _shot(page: Any, kind: str) -> str:
    """现场截图存到 uploads 下（能直接被前端 /media/uploads 取到）。"""
    ts = time.strftime("%Y%m%d-%H%M%S")
    path = UPLOAD_DIR / f"_toutiao_{kind}_{ts}.png"
    try:
        page.screenshot(path=str(path), full_page=False)
    except Exception:  # noqa: BLE001
        return ""
    _prune_shots()
    return path.name


def _prune_shots() -> None:
    shots = sorted(UPLOAD_DIR.glob("_toutiao_*.png"), key=lambda p: p.stat().st_mtime,
                   reverse=True)
    for old in shots[SHOT_KEEP:]:
        try:
            old.unlink()
        except OSError:
            pass


def _paste_key() -> str:
    return "Meta+V" if platform.system() == "Darwin" else "Control+V"


_CLIP_JS = """
async ([html, text]) => {
  const item = new ClipboardItem({
    'text/html': new Blob([html], {type: 'text/html'}),
    'text/plain': new Blob([text], {type: 'text/plain'}),
  });
  await navigator.clipboard.write([item]);
  return true;
}
"""


def _paste_html(page: Any, html: str, text: str, log: Callable[[str], None]) -> bool:
    """富文本粘贴：先写系统剪贴板再 Ctrl+V。

    头条的正文编辑器是 ProseMirror 那一类，直接改 innerHTML 或 execCommand 都会被
    它自愈掉，只有真粘贴事件才认。剪贴板写不进去（某些无头环境）就返回 False，
    调用方退化成纯文本输入。
    """
    try:
        page.context.grant_permissions(["clipboard-read", "clipboard-write"], origin=MP)
    except Exception:  # noqa: BLE001 - 老版本 playwright 没有 grant_permissions 的 origin 参数
        pass
    try:
        page.evaluate(_CLIP_JS, [html, text])
        page.keyboard.press(_paste_key())
        page.wait_for_timeout(200)
        return True
    except Exception as exc:  # noqa: BLE001
        log(f"富文本粘贴不可用（{exc}），这一段改用纯文本")
        return False


def _open_image_drawer(page: Any, say: Callable[[str], None]) -> bool:
    """点工具栏「图片」按钮，打开上传抽屉。返回是否打开成功。

    两个坑（都踩过）：
    1. 这个按钮**不弹系统文件选择框**，而是打开 `byte-drawer` 抽屉
       （上传图片 / 免费正版图片 / 热点图库 / 我的素材）。用 expect_file_chooser 会超时。
    2. **必须先让正文编辑器有焦点**。填完标题后焦点在标题框，正文工具栏整体失活
       （按钮带 disable 类），这时点图片按钮毫无反应——这是"打不开面板"的真因。
    """
    if page.locator(".byte-drawer-wrapper input[type=file]").count() > 0:
        return True                       # 已经开着

    # 先点一下正文，把工具栏激活
    try:
        ed = _first_visible(page, BODY_SELECTORS, timeout=3000)
        if ed is not None:
            ed.click(timeout=3000, force=True)
            page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001
        pass

    target = _first_visible(page, IMAGE_BUTTON_SELECTORS, timeout=3000)
    if target is None:
        say("页面上找不到图片按钮")
        return False
    for attempt in range(2):
        try:
            target.click(timeout=5000, force=True)
        except Exception as exc:  # noqa: BLE001
            say(f"点图片按钮第 {attempt + 1} 次失败：{str(exc)[:60]}")
            _dismiss_dialogs(page, say)
            continue
        # 等抽屉真正出现（里面有 file input 才算）
        for _ in range(20):
            page.wait_for_timeout(400)
            if page.locator(".byte-drawer-wrapper input[type=file]").count() > 0:
                return True
    return False


def _close_image_drawer(page: Any) -> None:
    """关掉上传抽屉。点关闭按钮 → ESC → JS 隐藏，三级兜底。"""
    for sel in (".byte-drawer-wrapper [class*='close']", ".byte-drawer-close-icon",
                '.byte-drawer-wrapper button:has-text("取消")'):
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1000):
                loc.click(timeout=2500, force=True)
                page.wait_for_timeout(600)
                break
        except Exception:  # noqa: BLE001
            continue
    # 这里不按 Escape：头条编辑器里按 ESC 会进入「放弃编辑」，之后自动保存的内容就是空的。
    # 抽屉靠上面的关闭按钮 + 下面的 JS 隐藏就够干净了。
    # 抽屉是覆盖层，关不干净会挡住后面的编辑器点击，直接摘掉最省事
    try:
        page.evaluate("""() => {
            document.querySelectorAll('.byte-drawer-wrapper').forEach(el => {
                el.style.display = 'none';
                el.setAttribute('aria-hidden', 'true');
            });
        }""")
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(400)


def _upload_images(page: Any, paths: list[Path],
                   say: Callable[[str], None]) -> dict[str, str]:
    """把一批本地图上传到头条素材库，返回 {本地路径: 头条CDN地址}。

    做法：开一次抽屉，逐个 `set_input_files`，从 `/spice/image` 的上传响应里收
    `image_url`（按提交顺序对应）。上传完关抽屉，正文那边再用 CDN 地址拼 HTML 粘贴
    —— 这样每张图只上传一次，也不需要去点抽屉里那张图试图"插入"。
    """
    result: dict[str, str] = {}
    todo = [p for p in paths if p.exists()]
    missing = [p for p in paths if not p.exists()]
    for p in missing:
        say(f"图片文件不在了：{p.name}")
    if not todo:
        return result

    captured: list[str] = []

    def _on_resp(r: Any) -> None:
        try:
            if "/spice/image" in r.url and r.request.method == "POST":
                data = (json.loads(r.text()).get("data") or {})
                url = data.get("image_url") or ""
                if url:
                    captured.append(url)
        except Exception:  # noqa: BLE001
            pass

    page.on("response", _on_resp)
    try:
        if not _open_image_drawer(page, say):
            say("打不开图片上传面板，正文图片跳过")
            return result
        fi = page.locator('.byte-drawer-wrapper input[type=file]').first
        for idx, p in enumerate(todo):
            before = len(captured)
            try:
                fi.set_input_files(str(p))
            except Exception as exc:  # noqa: BLE001
                say(f"提交 {p.name} 失败：{str(exc)[:80]}")
                continue
            # 等这一张的上传响应
            for _ in range(30):
                page.wait_for_timeout(500)
                if len(captured) > before:
                    break
            if len(captured) > before:
                result[str(p)] = captured[before]
                say(f"图片 {idx + 1}/{len(todo)} 上传成功")
            else:
                say(f"图片 {idx + 1}/{len(todo)}（{p.name}）等不到上传结果")
        page.wait_for_timeout(1000)
    finally:
        try:
            page.remove_listener("response", _on_resp)
        except Exception:  # noqa: BLE001
            pass
        _close_image_drawer(page)
    return result


def _need_login(page: Any) -> bool:
    url = (page.url or "").lower()
    return "login" in url or "passport" in url


def _detect_wall(page: Any) -> tuple[str, str] | None:
    """头条登录之后还有几道人工关卡。返回 (kind, 命中的原话) 或 None。

    目前见过的是「手机绑定」：新号第一次发东西，头条要求先绑手机号，页面整页变成
    绑定表单，标题框自然找不到。这种程序过不去（要短信验证码），只能人来。
    """
    try:
        text = page.inner_text("body", timeout=4000)
    except Exception:  # noqa: BLE001
        text = ""
    for kind, kws in WALLS:
        for kw in kws:
            if kw in text:
                return kind, kw
    return None


def _wall_message(wall: tuple[str, str], shot: str, account_name: str) -> tuple[str, str]:
    kind, hit = wall
    who = f"「{account_name}」" if account_name else ""
    tail = f"（现场截图 /media/uploads/{shot}）" if shot else ""
    if kind == "bind_phone":
        return kind, (
            f"头条号{who}登录成功了，但头条要求先绑定手机号才能发文（页面提示：{hit}）。\n"
            "这不是代码问题，程序也过不去——要短信验证码。请先用今日头条 App 或电脑浏览器"
            "登录这个号，把手机号绑定好，再回来点「存到头条草稿」。" + tail)
    if kind == "verify":
        return kind, (
            f"头条号{who}还没完成实名认证（{hit}），头条不允许未实名的账号发文/存草稿。\n"
            "这一步只能人工做：登录头条号后台 → 账号设置 → 实名认证（身份证 + 人脸识别），"
            "通过后回来再投递。" + tail)
    return kind, f"头条发文页被挡住了（页面提示：{hit}），需要人工处理后再投递。{tail}"


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def create_draft(article: dict[str, Any], *, account_id: int | str = 0,
                 account_name: str = "", theme: str = "default",
                 font_size: str = "medium", headless: bool | None = None,
                 log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """把一篇稿子存进头条草稿箱。

    只点「存草稿」，代码里没有任何点「发布」的路径。
    """
    say = log or (lambda _s: None)
    ok, msg = deps()
    if not ok:
        raise ToutiaoError(msg)
    if not state_file(account_id).exists():
        raise LoginRequired(f"头条号{('「' + account_name + '」') if account_name else ''}"
                            "还没登录，先扫码登录一次")

    payload = build_payload(article.get("markdown") or "",
                           article.get("title") or "",
                           theme=theme, font_size=font_size)
    if not payload["title"]:
        raise ToutiaoError("标题是空的，头条草稿必须带标题")
    if not any(b["type"] == "text" for b in payload["blocks"]):
        raise ToutiaoError("正文是空的，先在编辑器里写点东西")
    if payload["title_cut"]:
        say(f"标题超过 {TITLE_MAX} 字，已截断（头条标题上限）")
    say(f"准备投递：标题 {len(payload['title'])} 字，文字块 "
        f"{sum(1 for b in payload['blocks'] if b['type'] == 'text')} 个，图片 "
        f"{len(payload['images'])} 张")

    if headless is None:
        headless = bool(int(db.get_setting("toutiao_headless", 1)))

    from playwright.sync_api import sync_playwright

    skipped: list[str] = []
    with sync_playwright() as pw:
        # 优先用持久化 profile（含 IndexedDB 等完整状态），没有才退回老 storage_state
        ctx, browser = _make_context(pw, account_id, headless=headless)
        page = _first_page(ctx)
        try:
            say("打开头条发文页…")
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

            # 从这一刻起就盯着保存请求：连请求参数带响应一起缓存。
            # 头条前端每次内容变化都会自己发一次 `POST /mp/agw/article/publish`，
            # 我们只是观察者——**绝不自己构造或重发这个请求**（历史上这么干误发过文章）。
            save_params: dict[str, str] = {}
            # 判定"这次保存带没带正文"的门槛：按正文纯文字长度的一半算（HTML 还会更长）
            expected_body = sum(len(re.sub(r"\s", "", b["text"]))
                                for b in payload["blocks"] if b["type"] == "text")
            min_content = max(50, expected_body // 2)

            def _on_publish_resp(resp: Any) -> None:
                try:
                    if "/article/publish" not in resp.url or resp.request.method != "POST":
                        return
                    data = resp.request.post_data or ""
                    if not data:
                        return
                    save_params["url"] = resp.url
                    save_params["data"] = data
                    # 这次请求带了多少正文？标题一填完那次是空的，不能算数。
                    try:
                        clen = len((parse_qs(data).get("content") or [""])[0])
                    except Exception:  # noqa: BLE001
                        clen = 0
                    save_params["clen"] = str(clen)
                    try:
                        txt = resp.text()
                    except Exception as exc:  # noqa: BLE001
                        save_params["read_err"] = str(exc)[:80]
                        return
                    if '"code":0' in txt:
                        # 只有这次请求真的带了正文（够长）才算"保存成功"
                        if clen >= max(min_content, 30):
                            save_params["ok"] = "1"
                            save_params["resp_ok"] = txt[:300]
                        else:
                            save_params["ok_empty"] = "1"
                    elif "7050" in txt:
                        save_params["bad"] = "1"
                except Exception:  # noqa: BLE001
                    pass

            page.on("response", _on_publish_resp)

            if _need_login(page):
                raise LoginRequired("头条登录态已失效，重新扫码登录一下")

            # 头条进来常弹新手引导/公告，整层盖住页面，不关掉后面所有点击都会超时
            _dismiss_dialogs(page, say)

            # 先查账号资质：没绑手机/没实名时，填完内容也只会得到 7050「保存失败」，
            # 与其白填一遍，不如进来就拦下说清楚。
            ready, kind, detail = _check_account_ready(page)
            if not ready:
                shot = _shot(page, f"not_ready_{kind}")
                raise ManualActionRequired(*_wall_message((kind, detail), shot, account_name))

            # 记下当前草稿箱，存完靠「多了一篇」来确认
            before_ids = _draft_ids(_list_drafts(page) or [])

            # ---- 标题 ----
            title_box = _first_visible(page, TITLE_SELECTORS, timeout=8000)
            if title_box is None:
                # 先看清楚是「改版了」还是「头条把发文页挡住了」。这两种处理方式完全不同：
                # 前者改选择器，后者只能人去绑手机/认证，别混成一句"大概改版了"。
                shot = _shot(page, "no_title")
                wall = _detect_wall(page)
                if wall:
                    raise ManualActionRequired(*_wall_message(wall, shot, account_name))
                raise ToutiaoError(
                    "没找到标题输入框，头条大概改版了。现场截图："
                    f"/media/uploads/{shot}；候选选择器：{', '.join(TITLE_SELECTORS)}")
            _safe_fill(title_box, payload["title"], say)
            say("标题已填")

            # ---- 正文编辑器：必须先点一下 ----
            # 一是让光标进正文，二是**激活正文工具栏**（填完标题后工具栏是失活的，
            # 不激活的话图片按钮点不动，这也是"打不开上传面板"的真因）。
            editor = _first_visible(page, BODY_SELECTORS, timeout=8000)
            if editor is None:
                shot = _shot(page, "no_body")
                raise ToutiaoError(
                    "没找到正文编辑器，头条大概改版了。现场截图："
                    f"/media/uploads/{shot}；候选选择器：{', '.join(BODY_SELECTORS)}")
            try:
                _safe_click(editor, say, "正文编辑器")
            except ToutiaoError:
                _dismiss_dialogs(page, say)
                _safe_click(editor, say, "正文编辑器")
            page.wait_for_timeout(500)

            # ---- 批量上传本地图片到头条图床 ----
            # 头条编辑器不吃本地路径，必须换成它自己 CDN 的地址才能显示。
            local_imgs = [Path(b["path"]) for b in payload["blocks"]
                          if b["type"] == "image" and b.get("path")]
            cdn_map: dict[str, str] = {}
            if local_imgs:
                say(f"上传正文图片 {len(local_imgs)} 张到头条图床…")
                cdn_map = _upload_images(page, local_imgs, say)
                if cdn_map:
                    say(f"图片上传完成 {len(cdn_map)}/{len(local_imgs)} 张")
                # 传完抽屉关了，把焦点放回正文
                try:
                    _safe_click(editor, say, "正文编辑器")
                except ToutiaoError:
                    pass
                page.wait_for_timeout(300)

            # ---- 正文：一次性粘贴整篇 ----
            # 之前是「逐块粘贴」，实测有严重问题：每次 Ctrl+V 都会落在上一次的选区上，
            # 把前面粘好的内容**覆盖掉**（772 字的稿子最后只剩 58 字，字数忽增忽减）。
            # 内容残缺 → 存草稿必失败。所以这里拼成一个完整 HTML 一次粘完，
            # 全程只有一次粘贴操作，不会被自己覆盖。
            html_parts: list[str] = []
            plain_parts: list[str] = []
            img_ok = img_fail = 0
            for block in payload["blocks"]:
                if block["type"] == "text":
                    html_parts.append(block["html"])
                    plain_parts.append(block["text"])
                    continue
                if block.get("url"):
                    html_parts.append(f'<p><img src="{block["url"]}" /></p>')
                    img_ok += 1
                elif block.get("path") and cdn_map.get(block["path"]):
                    html_parts.append(f'<p><img src="{cdn_map[block["path"]]}" /></p>')
                    img_ok += 1
                else:
                    img_fail += 1
                    skipped.append(block.get("alt") or (Path(block["path"]).name
                                                        if block.get("path") else "(空图片)"))

            full_html = "".join(html_parts)
            full_text = "\n".join(plain_parts)
            say(f"正文准备完成：{len(plain_parts)} 段、{img_ok} 张图，粘贴进编辑器…")
            if not _fill_body(page, full_html, full_text, say):
                shot = _shot(page, "empty_body")
                raise ToutiaoError(
                    "正文没能填进头条编辑器，为避免存出一篇空白草稿，这次先中止。"
                    f"（现场截图 /media/uploads/{shot}）")

            say(f"正文写完：段落 {len(plain_parts)} 段，"
                f"图片进正文 {img_ok} 张，没进去 {img_fail} 张")
            if img_fail:
                say("没进去的图：" + "、".join(skipped[:6])
                    + "——草稿可以事后人工补，不当成失败")

            # ---- 存草稿 ----
            # 头条没有「存草稿」按钮，只能靠页面自己的自动保存，然后去草稿箱核实。
            say("提交草稿…")
            _dismiss_dialogs(page, say)
            save = _save_draft_via_api(page, say, captured=save_params, wait=60,
                                       min_content=min_content)

            # 关键：草稿箱才是唯一可信的证据，而且必须是**带正文**的那篇。
            verify = _verify_saved(page, say, before=before_ids,
                                   min_words=max(20, expected_body // 2))
            if not save["ok"] and verify["ok"]:
                say("页面接口没回成功，但草稿箱里已经有新稿了，按成功算")
                save = {"ok": True, "evidence": verify["evidence"], "pgc_id": ""}

            url = page.url or PUBLISH_URL
            if not save["ok"]:
                shot = _shot(page, "save_fail")
                hint = ""
                if save.get("code") == 7050:
                    hint = ("\n头条对「无头浏览器」有风控：请求头里 sec-ch-ua 若带 "
                            "HeadlessChrome，保存接口会一律返回 7050。代码里已用 CDP "
                            "覆盖成 Google Chrome；如果还报这个，说明头条又加了新检测。")
                raise ToutiaoError(
                    f"存草稿失败：{save['evidence']}{hint}（现场截图 /media/uploads/{shot}）")

            if not verify["ok"] and save.get("pgc_id"):
                verify = {"ok": True,
                          "evidence": f"接口已返回 pgc_id={save['pgc_id']}",
                          "draft_id": save["pgc_id"]}
            if not verify["ok"]:
                shot = _shot(page, "verify")
                raise ToutiaoError(
                    f"接口说成功但草稿箱没查到：{verify['evidence']}"
                    f"（现场截图 /media/uploads/{shot}）")
            say(f"回读：{verify['evidence']}")
            return {
                "url": url,
                "title": payload["title"],
                "images": img_ok,
                "images_failed": img_fail,
                "skipped": skipped,
                "verify": verify,
                "pgc_id": save.get("pgc_id") or verify.get("draft_id") or "",
            }
        finally:
            try:
                ctx.close()
                if browser is not None:
                    browser.close()
            except Exception:  # noqa: BLE001
                pass


def _body_len(page: Any) -> int:
    """编辑器里现在有多少个非空白字符。读不到返回 -1。"""
    try:
        return int(page.evaluate("""() => {
            const e = document.querySelector('.ProseMirror');
            return e ? e.innerText.replace(/\\s/g, '').length : 0;
        }"""))
    except Exception:  # noqa: BLE001
        return -1


def _fill_body(page: Any, full_html: str, full_text: str,
               say: Callable[[str], None]) -> bool:
    """把正文塞进头条编辑器，并**确认真的进去了**。

    踩过的坑：上传图片那一步会开关一次抽屉，焦点就丢了；这时再 Ctrl+V
    什么都没有粘进去，编辑器还是空的，但流程照样往下走，
    最后存出一篇「只有标题、正文空白」的草稿（用户实际遇到过）。
    所以这里反复试，并且以「编辑器里实际字数」为准，不是以粘贴有没有报错为准。
    """
    want = len(re.sub(r"\s", "", full_text))
    floor = max(20, int(want * 0.5))

    for attempt in range(1, 4):
        # 每次都重新点一下编辑器，确保焦点真的在里面
        try:
            ed = _first_visible(page, BODY_SELECTORS, timeout=4000)
            if ed is not None:
                ed.click(timeout=4000, force=True)
                page.wait_for_timeout(300)
        except Exception:  # noqa: BLE001
            pass

        # 先清空（上一次可能粘了一半）
        if _body_len(page) > 0:
            try:
                page.keyboard.press("Control+a")
                page.keyboard.press("Delete")
                page.wait_for_timeout(200)
            except Exception:  # noqa: BLE001
                pass

        _paste_html(page, full_html, full_text, say)
        page.wait_for_timeout(1500)
        got = _body_len(page)
        if got >= floor:
            say(f"正文已填入 {got} 字")
            return True

        say(f"正文没落进去（第 {attempt} 次，编辑器 {max(got, 0)} 字 / 预期 {want} 字），重试")
        # 备用方案：绕过剪贴板，直接输入纯文本（丢格式但至少正文在）
        try:
            ed = _first_visible(page, BODY_SELECTORS, timeout=3000)
            if ed is not None:
                ed.click(timeout=3000, force=True)
            page.keyboard.insert_text(full_text)
            page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001
            pass
        got = _body_len(page)
        if got >= floor:
            say(f"正文已填入 {got} 字（纯文本模式，格式会简单一些）")
            return True
    return False


def _save_draft_via_api(page: Any, say: Callable[[str], None],
                        captured: dict[str, str] | None = None,
                        wait: float = 60.0,
                        min_content: int = 0) -> dict[str, Any]:
    """等页面自己把草稿存下去，然后确认。

    ⚠️ 历史教训（很重要，别再踩）：
    曾经尝试"抄页面参数 + 改成 save=1 重发"来强制存草稿。实测下来**极其危险**：
    同样的 save=1，有时进草稿箱、有时直接把文章**发布出去**（作品管理里出现
    「审核中」「已发布」），已经因此误发过两篇文章、只能手动删。
    原因大概是 save 的语义还取决于服务端对该 title_id / pgc_id 的状态判断，
    不是"1 就是草稿"这么简单。**所以绝不再自己构造/重发保存请求。**

    现在的做法：填完内容后什么都不注入，只等页面自己的自动保存
    （`POST /mp/agw/article/publish`，参数由头条前端生成），再用草稿箱列表确认。
    存不上就明确报错，绝不代替页面做写操作。
    """
    if captured is None:
        captured = {}
    save_err: dict[str, Any] = {}

    # 让页面发一次保存：制造一点内容变化触发 debounce
    for _ in range(2):
        try:
            ed = _first_visible(page, BODY_SELECTORS, timeout=3000)
            if ed is not None:
                ed.click(timeout=3000, force=True)
                page.keyboard.press("Control+End")
                page.keyboard.type("x")
                page.keyboard.press("Backspace")
                break
        except Exception:  # noqa: BLE001
            pass

    deadline = time.time() + wait
    last_nudge = time.time()
    while time.time() < deadline:
        page.wait_for_timeout(1500)
        # 页面任何一次「带了正文」的保存回了 code:0 才算成功。
        # 注意：标题一填完页面就会先存一次（content 是空的），那次也会返回 code:0。
        # 如果只看 code:0，就会在正文还没填/还没存上去的时候误判成功，
        # 结果存出一篇 0 字空稿——这个事故真实发生过。
        if captured.get("ok"):
            pgc = ""
            try:
                pgc = str((json.loads(captured.get("resp_ok") or "{}").get("data") or {})
                          .get("pgc_id") or "")
            except Exception:  # noqa: BLE001
                pass
            return {"ok": True, "evidence": "页面自动保存成功", "pgc_id": pgc}
        if captured.get("bad"):
            save_err["code"] = 7050
        # footer 也能给信号
        try:
            foot = page.evaluate("""() => {
                const f = document.querySelector('.footer-draft-save, .footer-tip-save');
                return f ? (f.innerText||'').trim() : '';
            }""")
            if foot and ("已保存" in foot or "保存成功" in foot) and "中" not in foot:
                return {"ok": True, "evidence": foot}
        except Exception:  # noqa: BLE001
            pass
        # 每 15 秒轻推一次内容变化，逼页面重新发保存请求。
        # 长文+多图第一次保存可能失败，重发一次就过了（实测过）。
        if time.time() - last_nudge > 15:
            try:
                ed = _first_visible(page, BODY_SELECTORS, timeout=2000)
                if ed is not None:
                    ed.click(timeout=2000, force=True)
                    page.keyboard.press("Control+End")
                    page.keyboard.type("x")
                    page.keyboard.press("Backspace")
                    say("再触发一次保存…")
            except Exception:  # noqa: BLE001
                pass
            last_nudge = time.time()

    if save_err.get("code") == 7050:
        return {"ok": False,
                "evidence": "头条保存接口返回 7050「保存失败」（页面的自动保存也存不上）",
                "code": 7050}
    if not captured.get("data"):
        return {"ok": False, "evidence": "页面没有发出保存请求"}
    return {"ok": False, "evidence": "页面保存请求没返回成功"}


def _verify_saved(page: Any, say: Callable[[str], None],
                  before: set[str] | None = None,
                  min_words: int = 0) -> dict[str, Any]:
    """确认草稿**真的**存进草稿箱了。

    头条发文页没有「存草稿」按钮，草稿靠自动保存，而且前端 toast 文案很不稳定
    （有时干脆不弹）。所以这里不看提示、直接查草稿箱列表：只要出现了一篇之前没有的草稿，
    就是硬证据。

    `min_words`：要求新草稿的字数至少这么多。**必须传**——标题一填完页面就会先自动
    存一篇「只有标题、正文空白」的草稿，那篇也算"新增"，不校验字数就会把它当成成功
    （实际就出过这个事故：草稿箱里全是 0 字空稿）。

    同时盯一下 footer 的「保存失败」和已知错误码 7050。
    """
    before = before or set()
    last_err = ""
    deadline = time.time() + 40
    while time.time() < deadline:
        # 1) 先看有没有明确的失败提示
        try:
            foot = page.evaluate("""() => {
                const f = document.querySelector('.footer-draft-save,.footer-tip-save,[class*=footer-tip]');
                return f ? (f.innerText||'').trim() : '';
            }""")
            if foot and "失败" in foot:
                last_err = foot
        except Exception:  # noqa: BLE001
            pass

        # 2) 再查草稿箱：多了新的一篇、而且那篇真的有内容 = 成功
        items = _list_drafts(page)
        if items is not None:
            now_ids = _draft_ids(items)
            new_items = []
            for it in items:
                if not (_draft_ids([it]) & before):
                    new_items.append(it)
            if new_items:
                best = max(new_items, key=lambda x: _draft_words(x))
                words = _draft_words(best)
                if words >= max(min_words, 1):
                    say(f"草稿箱已新增 {len(new_items)} 篇（{words} 字）")
                    return {"ok": True,
                            "evidence": f"草稿箱新增 {len(new_items)} 篇，正文 {words} 字",
                            "draft_id": sorted(_draft_ids([best]))[0],
                            "count": len(items), "words": words}
                # 只有空壳草稿：继续等页面把正文存上去
                if words == 0:
                    say("草稿箱里出现了空壳草稿（正文还没存上），继续等…")
        page.wait_for_timeout(2500)

    shot = _shot(page, "verify")
    if last_err:
        return {"ok": False, "evidence": f"{last_err}；现场截图 /media/uploads/{shot}",
                "error_hint": "save_failed"}
    return {"ok": False,
            "evidence": f"40 秒内草稿箱没出现「带正文」的新草稿；现场截图 /media/uploads/{shot}",
            "error_hint": "no_new_draft"}


def _draft_words(item: dict[str, Any]) -> int:
    """草稿的字数（头条把它塞在 graphic_extra 这个 JSON 字符串里）。"""
    try:
        ge = item.get("graphic_extra") or "{}"
        g = json.loads(ge) if isinstance(ge, str) else ge
        return int(g.get("content_word_cnt") or 0)
    except Exception:  # noqa: BLE001
        return 0


# --------------------------------------------------------------------------
# 校准工具：把真实 DOM 摸一遍
# --------------------------------------------------------------------------

def probe(account_id: int | str = 0, *, headless: bool = True) -> dict[str, Any]:
    """登录后打开发文页，报告候选选择器命中情况、页面上所有按钮文案、以及有没有人工关卡。

    头条改版时先跑这个（tools/toutiao_cli.py probe），照着结果改上面的常量，不用瞎猜。
    """
    ok, msg = deps()
    if not ok:
        raise ToutiaoError(msg)
    if not profile_ready(account_id) and not state_file(account_id).exists():
        raise LoginRequired("还没登录头条号")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        ctx, browser = _make_context(pw, account_id, headless=headless)
        page = _first_page(ctx)
        try:
            page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            html = page.content()
            out = DATA_DIR / "logs" / "toutiao_probe.html"
            out.write_text(html, encoding="utf-8")

            def hits(selectors: list[str]) -> dict[str, int]:
                res = {}
                for sel in selectors:
                    try:
                        res[sel] = page.locator(sel).count()
                    except Exception:  # noqa: BLE001
                        res[sel] = -1
                return res

            buttons = []
            try:
                for t in page.locator("button").all_inner_texts():
                    t = (t or "").strip()
                    if t:
                        buttons.append(t[:20])
            except Exception:  # noqa: BLE001
                pass
            file_inputs = 0
            try:
                file_inputs = page.locator('input[type="file"]').count()
            except Exception:  # noqa: BLE001
                pass
            wall = _detect_wall(page)
            ready, rkind, rdetail = _check_account_ready(page)
            return {
                "url": page.url,
                "need_login": _need_login(page),
                "wall": wall,
                # 账号资质：这是头条能不能发文的真正门槛（没实名时保存只会返回 7050）
                "account_ready": ready,
                "account_gate": rkind,
                "account_detail": rdetail,
                "drafts": len(_list_drafts(page) or []),
                "html_len": len(html),
                "html_saved": str(out),
                "title": hits(TITLE_SELECTORS),
                "body": hits(BODY_SELECTORS),
                "buttons": buttons[:60],
                "file_inputs": file_inputs,
            }
        finally:
            try:
                ctx.close()
                if browser is not None:
                    browser.close()
            except Exception:  # noqa: BLE001
                pass
