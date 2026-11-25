"""配图素材源：从网络抓与话题相关的真实图片。

图源（都实测过，服务器可访问）：
  1. 360 图片搜索 image.so.com —— 主图源，返回 宽/高/标题/来源站，能在下载前就筛掉
     水印站和竖图，并且能按"标题与选题的词重叠"打分
  2. Bing 图片搜索 —— 兜底

后处理：域名黑名单（水印图库/表情包）→ PIL 校验（尺寸/比例/格式）→ 平均哈希去重
→ 过大图缩到 1600 宽 → 打图注。

注意：图片是对搜索引擎索引的抓取，相关性有波动，
所以界面上每张图都能单独替换/删除，另有「用模型按标题复选」的一步。
"""
from __future__ import annotations

import hashlib
import io
import re
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

import requests
from PIL import Image, ImageFilter

from .images import ImageError, inspect_image
from .settings import UPLOAD_DIR

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

# 水印图库、表情包、壁纸站、纯视频站
DOMAIN_BLOCK = (
    "699pic.com", "58pic.com", "588ku.com", "veer.com", "zcool.cn", "tuchong.com",
    "nipic.com", "123rf.com", "bigstock", "shutterstock", "gettyimages", "istockphoto",
    "dreamstime", "alamy.com", "photophoto.cn", "ooopic.com", "51yuansu.com",
    "doutula.com", "fabiaoqing.com", "zhuangbi", "qqbiaoqing", "wallpaper",
    "duitang.com", "huaban.com", "gstatic", "google",
    "bilibili.com", "douyin.com", "kuaishou.com", "ixigua.com",
)

MIN_W, MIN_H = 480, 260
MIN_RATIO, MAX_RATIO = 1.05, 2.9
MAX_BYTES = 6 * 1024 * 1024
SAVE_MAX_W = 1600

_BAD_EXT = re.compile(r"\.(gif|svg|ico|webp)(\?|$)", re.I)
_SITE_SUFFIX = re.compile(
    r"\s*[-_—|｜]\s*(搜狐|新浪|网易|腾讯|澎湃|百家号|知乎|头条|观察者网|凤凰网|"
    r"中国新闻网|人民网|新华网|央视网|环球网|界面新闻|微博|豆瓣|小红书|"
    r"中国天气网|东方财富网|证券时报|每日经济新闻|快科技|中华网|网易订阅)\s*$")
_TAG_LIKE = re.compile(r"<[^>]+>")
_CJK = re.compile(r"[\u4e00-\u9fff]")

_STOP_BIGRAM = {
    "主播", "直播", "视频", "网友", "回应", "通报", "今日", "今天", "什么",
    "怎么", "为何", "可以", "我们", "大家", "现在", "已经", "这个", "那个",
    "一个", "这种", "那些", "自己", "不是", "就是", "没有", "还是", "以及",
}

# 标题里出现这些词，基本就是榜单/报表/数据截图
DATA_TITLE_HINTS = (
    "排名", "榜单", "排行", "排行榜", "报表", "统计", "指数", "走势", "评测",
    "测评", "对比表", "一览", "名单", "价格表", "财报", "研报", "年报", "季报",
    "占比", "同比增长", "份额", "数据图", "折线图", "柱状图", "表格",
)


# --------------------------------------------------------------------------
# 相关度
# --------------------------------------------------------------------------

def _bigrams(text: str) -> set[str]:
    chars = _CJK.findall(_TAG_LIKE.sub("", text or ""))
    return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)} - _STOP_BIGRAM


def relevance(query: str, title: str) -> float:
    qs, ts = _bigrams(query), _bigrams(title)
    if not qs or not ts:
        return 0.0
    return len(qs & ts) / len(qs)


def _query_variants(topic: str) -> list[str]:
    """一个选题给几个查询变体，提高命中相关图的概率。"""
    base = re.sub(r"[，,。.！!？?、；;：:\"'“”‘’（）()]", " ", topic or "").strip()
    base = re.sub(r"\s+", " ", base)
    variants = [base]
    core = " ".join(w for w in base.split() if len(w) >= 2)[:30]
    if core and core != base:
        variants.append(core)
    return [v for v in dict.fromkeys(variants) if v][:2]


# --------------------------------------------------------------------------
# 图源
# --------------------------------------------------------------------------

def _blocked(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(b in host for b in DOMAIN_BLOCK)


def so360_images(query: str, pages: int = 2) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for page in range(max(1, pages)):
        try:
            r = requests.get(
                "https://image.so.com/j",
                params={"q": query, "sn": page * 30, "pn": 30},
                headers={"User-Agent": UA, "Referer": "https://image.so.com/"},
                timeout=25,
            )
            d = r.json()
        except (requests.RequestException, ValueError):
            break
        for it in d.get("list") or []:
            url = it.get("img") or ""
            if not url.startswith("http") or _BAD_EXT.search(url):
                continue
            site = it.get("site") or it.get("dspurl") or ""
            if _blocked(url) or any(b in site.lower() for b in DOMAIN_BLOCK):
                continue
            w, h = int(it.get("width") or 0), int(it.get("height") or 0)
            if w and h:
                ratio = w / h if h else 0
                if w < MIN_W or h < MIN_H or not (MIN_RATIO <= ratio <= MAX_RATIO):
                    continue
            title = it.get("title") or ""
            out.append({
                "url": url,
                "title": title,
                "site": site,
                "page": it.get("link") or "",
                "width": w,
                "height": h,
                "source": "360",
                "score": relevance(query, title),
            })
    return out


def bing_images(query: str, limit: int = 35) -> list[dict[str, Any]]:
    try:
        r = requests.get(
            "https://www.bing.com/images/search",
            params={"q": query, "first": 1, "count": max(limit, 35)},
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
            timeout=25,
        )
        r.raise_for_status()
    except requests.RequestException:
        return []

    out: list[dict[str, Any]] = []
    for m in re.finditer(r'murl&quot;:&quot;(.*?)&quot;(.*?)(?=murl&quot;:|$)', r.text, re.S):
        url, tail = m.group(1), m.group(2)
        if not url.startswith("http") or _BAD_EXT.search(url) or _blocked(url):
            continue
        t = re.search(r'&quot;t&quot;:&quot;(.*?)&quot;', tail)
        title = _TAG_LIKE.sub("", t.group(1)) if t else ""
        out.append({"url": url, "title": title, "site": urlparse(url).hostname or "",
                    "page": "", "width": 0, "height": 0, "source": "bing",
                    "score": relevance(query, title) * 0.85})
        if len(out) >= limit:
            break
    return out


def candidates(topic: str, want: int = 4,
               extra_queries: list[str] | None = None) -> list[dict[str, Any]]:
    """汇总多路图源的候选，按相关度排序（不下载）。

    extra_queries 一般是文章自己的小标题 —— 用它们再搜一轮，
    能捞到"事件里出现过的东西"（比如文章提到某部剧，就搜那部剧），相关性明显更好。
    """
    seen: set[str] = set()
    pool: list[dict[str, Any]] = []
    pages = 2 if want <= 4 else 3
    variants = _query_variants(topic)
    for q in (extra_queries or [])[:4]:
        q = (q or "").strip()
        if len(q) >= 2 and q not in variants:
            variants.append(q)
    all_q = list(variants)
    for q in variants:
        for item in so360_images(q, pages=pages):
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            pool.append(item)
    if len([p for p in pool if p["score"] > 0]) < 4:
        for item in bing_images(topic, limit=35):
            if item["url"] in seen:
                continue
            seen.add(item["url"])
            pool.append(item)
    # 相关度统一按"所有查询词里最高的那个"来算：
    # 一条图命中短关键词「全女综艺」也算相关，不该因为整句选题没命中而被判 0 分
    for p in pool:
        title = p.get("title") or ""
        p["score"] = max((relevance(q, title) for q in all_q), default=0.0)
    pool.sort(key=lambda p: (-p.get("score", 0), -(p.get("width") or 0)))
    return pool


# --------------------------------------------------------------------------
# 下载与校验
# --------------------------------------------------------------------------

def _ahash(img: Image.Image) -> str:
    small = img.convert("L").resize((8, 8), Image.LANCZOS)
    px = list(small.getdata())
    avg = sum(px) / len(px)
    return f"{int(''.join('1' if p >= avg else '0' for p in px), 2):016x}"


def _similar(a: str, b: str) -> bool:
    return len(a) == len(b) and bin(int(a, 16) ^ int(b, 16)).count("1") <= 4


def image_profile(img: Image.Image) -> dict[str, float]:
    """算一张图的几个可解释特征（都在 240px 宽的小图上算，快）。"""
    w, h = img.size
    small = img.convert("RGB").resize((240, max(1, int(240 * h / w))), Image.LANCZOS)
    raw = small.tobytes()
    n = len(raw) // 3
    if not n:
        return {"colors": 0, "edge": 0.0, "white": 0.0, "dark": 0.0}
    white = dark = 0
    colors: set[tuple[int, int, int]] = set()
    for i in range(0, len(raw), 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        colors.add((r >> 4, g >> 4, b >> 4))
        if r > 228 and g > 228 and b > 228:
            white += 1
        elif r < 80 and g < 80 and b < 80:
            dark += 1
    eraw = small.convert("L").filter(ImageFilter.FIND_EDGES).tobytes()
    edge = sum(1 for v in eraw if v > 40) / max(1, len(eraw))
    return {"colors": float(len(colors)), "edge": edge,
            "white": white / n, "dark": dark / n}


DATA_IMAGE_THRESHOLD = 0.55


def looks_like_data_image(img: Image.Image, title: str = "") -> tuple[bool, float]:
    """判断是不是"榜单/报表/文档截图"。

    实测：单看边缘密度不行 —— 人挤人的现场照片边缘一样密（16/17 被误判）。
    真正的分界是**颜色数**：截图/表格/UI 只有一两百种颜色，新闻照片三四百到一千多。
    所以用「颜色数少」做主判据，再用边缘/白底/黑底确认它确实是"文字或表格构成"的画面。

    返回值第二个是 score（0-1，越大越像数据图），仅用于排序参考。
    """
    f = image_profile(img)
    fewer_colors = f["colors"] < 300
    texty = f["edge"] >= 0.12 or f["white"] >= 0.50 or f["dark"] >= 0.30
    score = round(min(1.0, f["edge"] * 2.0 + max(0.0, f["white"] - 0.35)
                      + max(0.0, f["dark"] - 0.20) + (0.35 if fewer_colors else 0.0)), 3)
    is_data = fewer_colors and texty
    if title and any(k in title for k in DATA_TITLE_HINTS) and is_data:
        score = min(1.0, score + 0.15)
    return is_data, score


def clean_caption(text: str) -> str:
    text = _TAG_LIKE.sub("", text or "").strip()
    text = _SITE_SUFFIX.sub("", text).strip()
    text = re.sub(r"[【】\[\]（）()]", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" -—|｜·,，.。?？!！")
    return text[:24]


def download_candidate(item: dict[str, Any]) -> dict[str, Any] | None:
    url = item["url"]
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://image.so.com/"},
                         timeout=15, stream=True)
        if r.status_code != 200:
            return None
        ctype = (r.headers.get("Content-Type") or "").lower()
        if ctype and not ctype.startswith("image/"):
            return None
        data = b""
        for chunk in r.iter_content(65536):
            data += chunk
            if len(data) > MAX_BYTES:
                return None
        if len(data) < 12_000:
            return None
    except requests.RequestException:
        return None

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception:  # noqa: BLE001
        return None
    if img.format in ("GIF", "SVG", "WEBP"):
        return None

    w, h = img.size
    if w < MIN_W or h < MIN_H:
        return None
    ratio = w / h if h else 0
    if not (MIN_RATIO <= ratio <= MAX_RATIO):
        return None

    if w > SAVE_MAX_W:
        img = img.convert("RGB").resize((SAVE_MAX_W, int(h * SAVE_MAX_W / w)), Image.LANCZOS)
        w, h = img.size
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=88, optimize=True)
        data, fmt = buf.getvalue(), "JPEG"
    else:
        fmt = img.format or "JPEG"

    digest = hashlib.sha1(data).hexdigest()[:10]
    name = f"src_{int(time.time())}_{digest}{'.jpg' if fmt == 'JPEG' else '.png'}"
    path = UPLOAD_DIR / name
    path.write_bytes(data)
    try:
        inspect_image(path)
    except ImageError:
        path.unlink(missing_ok=True)
        return None

    is_data, data_score = looks_like_data_image(img, item.get("title", ""))

    return {
        "path": str(path),
        "name": name,
        "url": f"/media/uploads/{name}",
        "is_data": is_data,
        "data_score": data_score,
        "width": w,
        "height": h,
        "ratio": round(ratio, 2),
        "bytes": len(data),
        "source": item.get("source", ""),
        "site": item.get("site", ""),
        "origin": url,
        "caption": clean_caption(item.get("title", "")),
        "hash": _ahash(img),
        "score": round(float(item.get("score") or 0), 3),
    }


def download_many(pool: list[dict[str, Any]], want: int, *, avoid: Iterable[str] = (),
                  scan_limit: int = 80, max_data_images: int = 1) -> list[dict[str, Any]]:
    """按给定顺序下载，凑够 want 张已去重的图。

    实拍图优先；被判为"数据截图"的不丢、不删，放进备用区：
      ① 先用最多 max_data_images 张数据图补位
      ② 还不够就把备用区剩下的也全用上 —— **宁可给数据图，也不给空位**
    之前在这里把超配额的数据图直接删掉了，结果一张被误判就整篇只剩 1 张图。
    """
    seen = list(avoid)
    picked: list[dict[str, Any]] = []
    reserve: list[dict[str, Any]] = []
    max_data_images = max(0, int(max_data_images))
    reserve_cap = max(want * 3, 6)

    fails = 0
    used_caps: set[str] = set()
    for item in pool[:scan_limit]:
        if len(picked) >= want or len(reserve) >= reserve_cap:
            break
        meta = download_candidate(item)
        if not meta:
            fails += 1
            # 连续失败说明这批域名多半防盗链/超时，别硬耗
            if fails >= 15:
                break
            continue
        fails = 0
        if any(_similar(meta["hash"], h) for h in seen):
            continue
        # 同一张图的不同尺寸能靠哈希去掉，但"同名不同图"（同一篇文章的配图组）去不掉，
        # 6 张里出现 3 张同一标题的图很跳，这里按清理过的标题再去一次
        cap_key = (meta.get("caption") or "").strip()
        if cap_key and cap_key in used_caps:
            try:
                Path(meta["path"]).unlink(missing_ok=True)
            except OSError:
                pass
            continue
        seen.append(meta["hash"])
        if cap_key:
            used_caps.add(cap_key)
        (reserve if meta.get("is_data") else picked).append(meta)

    for meta in reserve[:max_data_images]:
        if len(picked) >= want:
            break
        picked.append(meta)

    if len(picked) < want:
        for meta in reserve:
            if len(picked) >= want:
                break
            if meta not in picked:
                picked.append(meta)
    return picked


def collect(topic: str, want: int = 4, *, avoid: Iterable[str] = (),
            max_data_images: int = 1) -> list[dict[str, Any]]:
    """一步到位：按话题抓 want 张可用图（实拍优先）。"""
    want = max(0, int(want))
    if not want:
        return []
    return download_many(candidates(topic, want), want, avoid=avoid,
                         max_data_images=max_data_images)


# --------------------------------------------------------------------------
# 正文插入 / 封面选图
# --------------------------------------------------------------------------

def insert_into_markdown(markdown: str, images: list[dict[str, Any]]) -> str:
    """把配图按行插进正文，**不依赖段落之间有没有空行**。

    之前的版本按 `\n\n` 切块，遇到"整篇单换行"的稿子会切出一块、以 # 开头，
    于是判定没有可插位置、原样返回 —— 图记进了库却没进正文。
    现在按行找"正文行"（非空、且不是标题/引用/表格/图片/代码围栏）作为插入点。
    """
    if not images:
        return markdown
    lines = markdown.split("\n")
    body = [i for i, ln in enumerate(lines)
            if ln.strip() and not ln.lstrip().startswith(("#", ">", "|", "!", "```"))]
    if not body:
        # 极端情况：正文全是标题/列表 —— 退化成直接追加到末尾
        tail = "\n\n".join(f"![{m.get('caption') or ''}]({m['url']})" for m in images)
        return markdown.rstrip() + "\n\n" + tail + "\n"

    n = len(images)
    picks: list[int] = []
    for k in range(n):
        frac = (k + 1) / (n + 1)
        idx = body[min(len(body) - 1, int(frac * len(body)))]
        step = 0
        while idx in picks and step < len(lines):
            idx += 1
            step += 1
        picks.append(idx)

    # 从后往前插，避免下标位移；前后各留一个空行，保证是独立块
    for idx, img in sorted(zip(picks, images), key=lambda x: x[0], reverse=True):
        cap = img.get("caption") or ""
        lines[idx + 1:idx + 1] = ["", f"![{cap}]({img['url']})", ""]
    return "\n".join(lines)


def best_cover_source(images: list[dict[str, Any]]) -> str:
    """挑最适合做封面的那张：比例接近 2:1、宽度够。"""
    if not images:
        return ""
    ranked = sorted(images, key=lambda m: (abs(m["ratio"] - 2.0), -(m["width"] or 0)))
    return ranked[0]["path"] if ranked else ""
