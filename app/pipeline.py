"""流水线编排：选题 → 写作 → 自检 → 排版 → 封面 → 建草稿 → 回读校验。

设计原则和原来那套一致：
  · 只调 draft/add，**发布必须人工二次确认**
  · 正文图走 media/uploadimg（换 mmbiz url），封面走 material/add_material（拿 media_id），
    两者不能混
  · 回读只认硬指标（图片计数、本地 src、h1/style/class），绝不靠中文关键字匹配
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path
from typing import Any

from . import (aigen, characters, checks, cover as cover_mod, db, images, imgsource,
               render, topics, toutiao, writer)
from .settings import ARTICLE_DIR, PLATFORM_LABELS, UPLOAD_DIR
from .wechat import WechatClient, WechatError, client_for

IMG_MD = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# 同一篇稿子的「AI 重生成」必须串行排队。
# 一次重生成要 50~90 秒，用户连点三次时三个线程各自读到同一份旧正文，最后写入的那个把
# 另外两个的替换顶掉；而被顶掉的原图在各自流程末尾已被删掉 —— 正文里就留下指向已删文件
# 的 url（前端表现：碎裂图标 + undefined×undefined）。
_IMG_LOCKS: dict[int, threading.Lock] = {}
_IMG_LOCKS_GUARD = threading.Lock()


def _img_lock(article_id: int) -> threading.Lock:
    with _IMG_LOCKS_GUARD:
        lk = _IMG_LOCKS.get(article_id)
        if lk is None:
            lk = threading.Lock()
            _IMG_LOCKS[article_id] = lk
        return lk


class WrongPlatform(RuntimeError):
    """账号平台和投递方式对不上。属于用法问题，前端要显示成人话而不是服务器故障。"""


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------

def get_account(account_id: int | None, platform: str | None = None) -> dict[str, Any]:
    """取账号。给了 platform 就只在这个平台里挑（默认号也只在这个平台里找）。"""
    row = None
    if account_id:
        row = db.query_one("SELECT * FROM accounts WHERE id=?", (account_id,))
        if row is not None and platform and (row["platform"] or "wechat") != platform:
            raise RuntimeError(
                f"账号「{row['name']}」属于"
                f"{(PLATFORM_LABELS.get(row['platform']) or row['platform'])}，不是"
                f"{PLATFORM_LABELS.get(platform, platform)}，别拿错平台发")
    if row is None:
        if platform:
            row = db.query_one(
                "SELECT * FROM accounts WHERE COALESCE(platform,'wechat')=? "
                "ORDER BY is_default DESC, id ASC LIMIT 1", (platform,))
        else:
            row = db.query_one("SELECT * FROM accounts ORDER BY is_default DESC, id ASC LIMIT 1")
    if row is None:
        if platform:
            raise RuntimeError(
                f"还没有{PLATFORM_LABELS.get(platform, platform)}账号，"
                "先去「账号」页加一个")
        raise RuntimeError("还没有配置账号，先去「账号」页添加")
    return dict(row)


def list_accounts(platform: str | None = None) -> list[dict[str, Any]]:
    if platform:
        return [dict(r) for r in db.query(
            "SELECT * FROM accounts WHERE COALESCE(platform,'wechat')=? "
            "ORDER BY is_default DESC, id", (platform,))]
    return [dict(r) for r in db.query(
        "SELECT * FROM accounts ORDER BY COALESCE(platform,'wechat'), is_default DESC, id")]


# --------------------------------------------------------------------------
# 文章 CRUD
# --------------------------------------------------------------------------

def create_article(*, account_id: int | None, title: str = "", markdown: str = "",
                   slot: str = "extra", created_by: str = "",
                   topic_id: int | None = None) -> int:
    acc = get_account(account_id)
    ts = db.now()
    return db.insert("articles", {
        "account_id": acc["id"],
        "platform": acc.get("platform") or "wechat",
        "topic_id": topic_id,
        "title": title,
        "digest": "",
        "author": acc.get("author") or "",
        "slot": slot,
        "markdown": markdown,
        "html": "",
        "status": "draft",
        "checks": "{}",
        "images": "[]",
        "created_by": created_by,
        "created_at": ts,
        "updated_at": ts,
    })


def get_article(article_id: int) -> dict[str, Any]:
    row = db.query_one("SELECT * FROM articles WHERE id=?", (article_id,))
    if not row:
        raise RuntimeError(f"文章 {article_id} 不存在")
    return db.row_to_dict(row) or {}


def save_article(article_id: int, **fields: Any) -> dict[str, Any]:
    allowed = {"title", "digest", "author", "markdown", "html", "cover_path",
               "status", "checks", "images", "images_meta", "log", "error",
               "media_id", "thumb_media_id", "slot", "publish_id",
               "tt_url", "tt_at", "tt_state"}
    data = {k: v for k, v in fields.items() if k in allowed}
    for k in ("checks", "images", "tt_state"):
        if k in data and not isinstance(data[k], str):
            data[k] = json.dumps(data[k], ensure_ascii=False)
    if data:
        data["updated_at"] = db.now()
        db.update("articles", article_id, data)
    return get_article(article_id)


def log_append(article_id: int, line: str) -> None:
    art = get_article(article_id)
    stamp = db.now()[11:]
    text = (art.get("log") or "") + f"[{stamp}] {line}\n"
    db.update("articles", article_id, {"log": text[-20000:]})


def list_articles(limit: int = 50, status: str | None = None,
                  platform: str | None = None) -> list[dict[str, Any]]:
    where, params = [], []
    if status:
        where.append("a.status=?")
        params.append(status)
    if platform:
        where.append("COALESCE(a.platform,'wechat')=?")
        params.append(platform)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    rows = db.query(
        "SELECT a.*, c.name AS account_name, c.platform AS account_platform "
        "FROM articles a LEFT JOIN accounts c ON c.id=a.account_id "
        f"{clause} ORDER BY a.id DESC LIMIT ?", params + [limit])
    out = []
    for r in rows:
        d = db.row_to_dict(r) or {}
        d.pop("html", None)
        d.pop("log", None)
        out.append(d)
    return out


# --------------------------------------------------------------------------
# 生成
# --------------------------------------------------------------------------

def _set_stage(article_id: int, stage: str, note: str = "") -> None:
    """记录当前进度，前端轮询用。"""
    db.execute("UPDATE articles SET stage=?, updated_at=? WHERE id=?",
               (stage, db.now(), article_id))
    if note:
        log_append(article_id, note)


def image_quota(account: dict[str, Any]) -> int:
    """这个号一篇稿子配几张图：账号设置优先，其次全局，默认 4。"""
    try:
        n = int(account.get("image_count") or 0)
    except (TypeError, ValueError):
        n = 0
    if n > 0:
        return min(n, 8)
    try:
        return min(int(db.get_setting("images_per_article") or 4), 8)
    except (TypeError, ValueError):
        return 4


def check_kwargs(account: dict[str, Any] | None) -> dict[str, Any]:
    """按内容模式给自检换一套阈值。

    人物日常的稿子是「几百字 + 几张图」，用长文那套 500 字下限/5-8 处加粗去卡它，
    界面上会一直挂着自相矛盾的建议，等于噪音。
    """
    if account and (account.get("content_mode") or "article") == "photo":
        return {"min_words": 20, "max_words": 600, "photo_mode": True}
    return {}


def account_theme(account: dict[str, Any]) -> str:
    """这个号该用哪套排版主题。

    人物日常模式的稿子以图为主，长文那几套（图片只当插花）套上去会显得信息挤、
    黄色高亮也脏，所以 photo 模式强制用「看图日常」这套，不看长文主题设置。
    """
    if account and (account.get("content_mode") or "article") == "photo":
        return "photo"
    return (account.get("theme") or "").strip() or (db.get_setting("theme") or "default")


# 配图来源。以前只有「联网抓图」一条路，AI 生图只能一张张在稿件页手动换，
# 于是「我在设置里配了生图，怎么草稿里还是网图」变成了最常见的困惑。
IMAGE_SOURCES: dict[str, str] = {
    "web": "联网抓图",
    "ai": "AI 生图",
    "mixed": "一半网图 + 一半 AI",
}


def image_source(account: dict[str, Any] | None) -> str:
    """账号的配图来源，脏值一律当 web（跟老行为一致，不会突然改成花钱生图）。"""
    v = str((account or {}).get("image_source") or "web").strip()
    return v if v in IMAGE_SOURCES else "web"


def _strip_prev_images(art: dict[str, Any]) -> str:
    """去掉上一次自动插进正文的图，避免重跑一次就多一套图。"""
    markdown = art.get("markdown") or ""
    prev = art.get("images_meta") or []
    if isinstance(prev, str):
        try:
            prev = json.loads(prev)
        except ValueError:
            prev = []
    for m in prev:
        if not isinstance(m, dict):
            continue
        markdown = markdown.replace(f"\n\n![{m.get('caption','')}]({m.get('url','')})", "")
        markdown = markdown.replace(f"![{m.get('caption','')}]({m.get('url','')})", "")
    return markdown


def fetch_images_for(article_id: int, want: int | None = None) -> dict[str, Any]:
    """按当前选题去网上抓配图，插进正文，并用其中一张做封面。"""
    art = get_article(article_id)
    acc = get_account(art["account_id"])
    topic = art.get("title") or ""
    if art.get("topic_id"):
        row = db.query_one("SELECT title FROM topics WHERE id=?", (art["topic_id"],))
        if row:
            topic = row["title"] or topic

    want = image_quota(acc) if want is None else max(0, min(int(want), 8))
    if want <= 0:
        return {"images": [], "note": "配图数量设为 0，跳过"}

    heads = [m.group(1).strip() for m in re.finditer(r"^#{1,3}\s+(.+)$",
                                                     art.get("markdown") or "", re.M)][:2]
    log_append(article_id, f"抓配图：{topic}（要 {want} 张）")

    # 长句选题直接当搜索词搜不到什么（「内娱全女拍烂了都不如一部AI」这种），
    # 先让模型拆成 2-3 个短关键词（人名/作品/事件/物件），再拿去搜。
    kw_queries = writer.image_queries(topic)
    if kw_queries:
        log_append(article_id, f"配图搜索词：{topic} ｜ {'、'.join(kw_queries)}")
    pool = imgsource.candidates(topic, want, extra_queries=kw_queries)
    good = [p for p in pool if float(p.get("score") or 0) > 0]
    if heads and len(good) < want:
        log_append(article_id, f"命中仍不足（{len(good)} 条），再用小标题补充搜索")
        pool = imgsource.candidates(topic, want, extra_queries=kw_queries + heads)
    if not pool:
        log_append(article_id, "图源没返回候选")
        return {"images": [], "note": "图源没有返回候选"}

    # 先让模型按标题/来源挑，挑不到就按相关度直接取
    picks = writer.rank_images(topic, pool, want)
    ordered: list[dict[str, Any]] = [pool[i] for i in picks if i < len(pool)]
    seen = {p["url"] for p in ordered}
    ordered += [p for p in pool if p["url"] not in seen]

    try:
        max_data = int(db.get_setting("max_data_images", 1))
    except (TypeError, ValueError):
        max_data = 1
    images = imgsource.download_many(ordered, want, max_data_images=max_data)
    n_data = sum(1 for m in images if m.get("is_data"))
    n_low = sum(1 for m in images if float(m.get("score") or 0) <= 0.0)
    log_append(article_id, f"下载成功 {len(images)} 张（候选 {len(pool)}，"
                           f"数据截图 {n_data} 张，低相关 {n_low} 张）")
    if n_low:
        log_append(article_id, f"⚠ 有 {n_low} 张与选题没明显关联，建议在配图区点「换一张」")
    if not images:
        return {"images": [], "note": "候选图都没通过校验"}

    markdown = _strip_prev_images(art)
    markdown = imgsource.insert_into_markdown(markdown, images)

    # 兜底：如果正文里依然没有图片行，说明 Markdown 结构异常，规范化后重插
    if images and "![" not in markdown:
        fixed = render.normalize_markdown(markdown)
        markdown = imgsource.insert_into_markdown(fixed, images)
        if "![" in markdown:
            log_append(article_id, "正文结构异常（缺少空行），已自动规范化后插入配图")
        else:
            log_append(article_id, "⚠ 配图未能插入正文，请手动在编辑器里插图")

    theme = account_theme(acc)
    html = render.render_markdown(markdown, theme=theme,
                                  font_size=(db.get_setting("font_size") or "medium"))
    cover_src = imgsource.best_cover_source(images) if acc.get("cover_from_image", 1) else None
    cover_path = _cover_for(article_id, art.get("title") or "", cover_src, theme)

    save_article(article_id, markdown=markdown, html=html,
                 cover_path=str(cover_path),
                 images_meta=json.dumps(images, ensure_ascii=False),
                 images=[m["url"] for m in images])
    log_append(article_id, f"配图已插入正文，封面用{'网图' if cover_src else '纯色底'}")
    return {"images": images, "cover": str(cover_path)}


def _url_dims(url: str) -> dict[str, int]:
    """本地图片的实际宽高（配图卡片靠它算比例，缺了会显示成方块）。"""
    p = _local_upload_path(url)
    if p is None:
        return {}
    try:
        from PIL import Image
        with Image.open(p) as im:
            return {"width": im.size[0], "height": im.size[1]}
    except Exception:  # noqa: BLE001
        return {}


def generate_ai_images_for(article_id: int, want: int | None = None,
                           account: dict[str, Any] | None = None, *,
                           append: bool = False) -> dict[str, Any]:
    """用 AI 生图给正文配 N 张图（账号 image_source=ai / mixed 时走这里）。

    和 fetch_images_for 的差别只有「图从哪来」：插入位置、封面、元数据、日志全部复用同一套，
    否则两条链路迟早写出两种行为（这正是这个项目里最容易复发的一类 bug）。

    append=True 用于混合模式：不动正文里已有的图，只在后面补几张。
    """
    art = get_article(article_id)
    acc = account or get_account(art["account_id"])
    want = image_quota(acc) if want is None else max(0, min(int(want), 8))
    if want <= 0:
        return {"images": [], "note": "配图数量设为 0，跳过"}

    ok, why = aigen.available()
    if not ok:
        # 这里不静默跳过：账号明说了要 AI 图，退回网图会让人以为设置没生效
        raise RuntimeError(why)

    topic = art.get("title") or ""
    if art.get("topic_id"):
        row = db.query_one("SELECT title FROM topics WHERE id=?", (art["topic_id"],))
        if row and row["title"]:
            topic = row["title"]

    ch = characters.for_account(acc)
    log_append(article_id, f"AI 配图：{topic[:40]}（要 {want} 张"
                           + (f"｜人物：{ch['name']}" if ch else "") + "）")
    scenes = writer.image_scenes(topic, art.get("markdown") or "", want, acc)
    log_append(article_id, "画面：" + "；".join(s[:24] for s in scenes))

    images: list[dict[str, Any]] = []
    for i, scene in enumerate(scenes, 1):
        _set_stage(article_id, "images", f"正在生成第 {i}/{len(scenes)} 张 AI 配图")
        try:
            if ch:
                url = aigen.generate_scene(ch, scene, account=acc, kind="inline")
            else:
                url = aigen.generate(scene, acc, kind="inline")
            images.append({"url": url, "caption": "", "source": "ai", "site": "AI 生成",
                           **_url_dims(url)})
            log_append(article_id, f"第 {i} 张完成：{url}")
        except Exception as exc:  # noqa: BLE001 - 单张失败不该让整篇稿子作废
            log_append(article_id, f"第 {i} 张失败（跳过）：{type(exc).__name__}: {str(exc)[:120]}")
    if not images:
        raise RuntimeError("AI 配图一张都没生成出来：检查「设置 → AI 生图」的模型和余额")

    markdown = (art.get("markdown") or "") if append else _strip_prev_images(art)
    markdown = imgsource.insert_into_markdown(markdown, images)
    if "![" not in markdown:
        fixed = render.normalize_markdown(markdown)
        markdown = imgsource.insert_into_markdown(fixed, images)
        if "![" not in markdown:
            log_append(article_id, "⚠ 配图未能插入正文，请手动在编辑器里插图")

    theme = account_theme(acc)
    html = render.render_markdown(markdown, theme=theme,
                                  font_size=(db.get_setting("font_size") or "medium"))
    meta = (_imgs_meta(art) if append else []) + images
    # 混合模式下封面在第一轮（网图）就做好了，别用 AI 图再压一次
    cover_src = None
    if not append and acc.get("cover_from_image", 1):
        cover_src = str(_local_upload_path(images[0]["url"]) or "")
    cover_path = _cover_for(article_id, art.get("title") or "", cover_src or None, theme)

    save_article(article_id, markdown=markdown, html=html,
                 cover_path=str(cover_path),
                 images_meta=json.dumps(meta, ensure_ascii=False),
                 images=[m["url"] for m in meta])
    log_append(article_id, f"AI 配图 {len(images)} 张已插入正文")
    return {"images": images, "cover": str(cover_path)}


def fill_images(article_id: int, want: int | None = None,
                account: dict[str, Any] | None = None) -> dict[str, Any]:
    """按账号的「配图来源」决定走哪条链路。生成流程和稿件页「重新配图」都调这一个入口。

    - web   只有网图（老行为）
    - ai    全 AI
    - mixed 先抓一半网图，再补 AI；网图抓不到就全交给 AI
    """
    art = get_article(article_id)
    acc = account or get_account(art["account_id"])
    want = image_quota(acc) if want is None else max(0, min(int(want), 8))
    src = image_source(acc)

    if src == "ai":
        return generate_ai_images_for(article_id, want, acc)

    if src == "mixed" and want >= 2:
        n_web = want - want // 2
        web = fetch_images_for(article_id, n_web)
        got = web.get("images") or []
        rest = want - len(got)
        if rest <= 0:
            return web
        try:
            ai = generate_ai_images_for(article_id, rest, acc, append=bool(got))
        except Exception as exc:  # noqa: BLE001 - 网图已经有了，AI 这半挂了不至于开天窗
            log_append(article_id, f"AI 补图失败（保留网图）：{type(exc).__name__}: {str(exc)[:120]}")
            return web
        return {"images": got + (ai.get("images") or []),
                "cover": web.get("cover") or ai.get("cover")}

    return fetch_images_for(article_id, want)


def _photo_post_markdown(lines: list[str], image_urls: list[str]) -> str:
    """把「几句话 + 几张图」交错拼成正文。

    图片插在段落之间（不是全堆在最后），读起来才像真实的看图分享。
    """
    if not image_urls:
        return "\n\n".join(lines)
    out: list[str] = []
    n = len(lines)
    per = max(1, n // max(1, len(image_urls)))
    img_i = 0
    for i, line in enumerate(lines):
        out.append(line)
        if img_i < len(image_urls) and (i + 1) % per == 0:
            out.append(f"![]({image_urls[img_i]})")
            img_i += 1
    while img_i < len(image_urls):
        out.append(f"![]({image_urls[img_i]})")
        img_i += 1

    # 别让最后一张图孤零零挂在末尾（读起来像没写完）——
    # 把末尾的图往前挪到最后一个句子之前。
    def is_img(s: str) -> bool:
        return s.strip().startswith("![")

    while out and is_img(out[-1]):
        img = out.pop()
        pos = len(out)
        for j in range(len(out) - 1, -1, -1):
            if not is_img(out[j]):
                pos = j
                break
        out.insert(pos, img)
    return "\n\n".join(out)


def _run_photo_mode(article_id: int, art: dict[str, Any], acc: dict[str, Any],
                    topic_text: str, extra: str) -> dict[str, Any]:
    """人物日常模式：生成几张人物图 + 几句口语，做成一条轻量稿。

    只在这个号绑了虚拟人物、且内容模式=photo 时走这里。
    """
    ch = characters.for_account(acc)
    if not ch:
        raise RuntimeError("这个号开了「人物日常」模式，但还没绑虚拟人物："
                           "先去「人物」页建一个并绑定到这个号。")
    count = max(1, min(int(acc.get("photo_count") or 3), 6))

    _set_stage(article_id, "writing", "正在写口语化短文案")
    log_append(article_id, f"人物日常模式：{ch['name']}｜计划出 {count} 张图")
    data = writer.write_photo_post(topic=topic_text, account=acc, count=count, extra=extra)
    lines = data["lines"]
    scenes = data["scenes"]
    log_append(article_id, f"文案：{len(lines)} 句；画面：{len(scenes)} 个")

    # 逐张生成（每张 45~60 秒，所以必须有进度反馈）
    uploads: list[str] = []
    for i, scene in enumerate(scenes, 1):
        _set_stage(article_id, "images", f"正在生成第 {i}/{len(scenes)} 张人物图")
        log_append(article_id, f"生成第 {i} 张：{scene[:40]}")
        try:
            url = aigen.generate_scene(ch, scene, account=acc, kind="inline")
            uploads.append(url)
            log_append(article_id, f"第 {i} 张完成：{url}")
        except Exception as exc:  # noqa: BLE001
            log_append(article_id, f"第 {i} 张失败（跳过）：{type(exc).__name__}: {str(exc)[:120]}")
    if not uploads:
        raise RuntimeError("一张图都没生成出来，检查生图配置（设置 → AI 生图）")

    markdown = render.normalize_markdown(_photo_post_markdown(lines, uploads))
    title = data["title"] or topic_text
    digest = data["digest"]
    result = checks.run_checks(title, digest, markdown, data["keywords"] or [],
                               min_words=20, max_words=600, photo_mode=True)
    theme = account_theme(acc)
    html = render.render_markdown(markdown, theme=theme,
                                  font_size=(db.get_setting("font_size") or "medium"))
    # 封面直接用第一张人物图压字，不用再单独找图
    cover_path = _cover_for(article_id, title, _local_upload_path(uploads[0]), theme)
    save_article(article_id, title=title, digest=digest, markdown=markdown, html=html,
                 cover_path=str(cover_path), checks=result,
                 author=art.get("author") or acc.get("author") or "",
                 images_meta=json.dumps([{"url": u, "caption": "", "source": "ai",
                                          "site": "AI 生成", **_url_dims(u)} for u in uploads],
                                        ensure_ascii=False))
    _set_stage(article_id, "done", "")
    log_append(article_id, f"生成完成：{len(uploads)} 张图、{len(lines)} 句文案")
    now_art = get_article(article_id)
    if now_art.get("status") == "generating":
        db.update("articles", article_id, {"status": "draft"})
    out = get_article(article_id)
    return {"article": out, "checks": out.get("checks") or result}


def run_generation(article_id: int, *, extra: str = "", humanize: bool = False,
                   with_images: bool = True) -> dict[str, Any]:
    art = get_article(article_id)
    acc = get_account(art["account_id"])
    author = art.get("author") or acc.get("author") or ""
    theme = account_theme(acc)

    topic_row = None
    if art.get("topic_id"):
        row = db.query_one("SELECT * FROM topics WHERE id=?", (art["topic_id"],))
        topic_row = dict(row) if row else None
    topic_text = (topic_row or {}).get("title") or art.get("title") or "（未指定，请自由发挥一个合格选题）"

    # 人物日常模式是一条完全不同的产线：图为主、字为辅
    if (acc.get("content_mode") or "article") == "photo":
        log_append(article_id, f"开始生成（人物日常）：{topic_text}")
        return _run_photo_mode(article_id, art, acc, topic_text, extra)

    log_append(article_id, f"开始生成：{topic_text}")
    _set_stage(article_id, "writing", "正在写稿（约 20-60 秒）")
    data = writer.generate_article(topic=topic_text, author=author, extra=extra, account=acc)

    markdown = render.normalize_markdown(data["markdown"])
    if humanize:
        log_append(article_id, "去 AI 味改写中…")
        try:
            markdown = render.normalize_markdown(writer.humanize(markdown))
        except writer.LLMError as exc:
            log_append(article_id, f"去 AI 味失败（已跳过）：{exc}")
    # 标签只留一组、放最底部（模型经常一次给「公众号话题」「头条话题」两块）
    markdown = writer.normalize_tags(markdown, acc.get("platform") or "wechat")

    title = data["title"] or topic_text
    digest = data["digest"]
    kw = data["keywords"] or ((topic_row or {}).get("keywords") or [])

    # 标题不合「反问 + 结论 / ≤15 字」时，让模型重拟一批挑一个，别让规范靠自觉
    if checks.title_length(title) > 15 or "？" not in title and "?" not in title:
        try:
            log_append(article_id, f"标题不合规范（{title}），重新拟标题")
            for cand in writer.suggest_titles(markdown):
                if "？" in cand or "?" in cand:
                    if checks.title_length(cand) <= 15:
                        title = cand
                        log_append(article_id, f"改用标题：{cand}")
                        break
        except writer.LLMError as exc:
            log_append(article_id, f"重拟标题失败（保留原题）：{exc}")

    result = checks.run_checks(title, digest, markdown, kw)
    log_append(article_id, f"正文 {result['words']} 汉字，标签 {result['tags_count']} 个，"
                           f"加粗 {result['structure']['bold']} 处")

    html = render.render_markdown(markdown, theme=theme,
                                  font_size=(db.get_setting("font_size") or "medium"))
    cover_path = _cover_for(article_id, title, None, theme)

    save_article(article_id, title=title, digest=digest, markdown=markdown, html=html,
                 cover_path=str(cover_path), checks=result, author=author)
    if topic_row and kw:
        db.update("topics", topic_row["id"],
                  {"keywords": json.dumps(kw, ensure_ascii=False)})

    # 配图 + 封面取图（来源看账号的 image_source：网图 / AI / 混合）
    src_label = IMAGE_SOURCES[image_source(acc)]
    _set_stage(article_id, "images", f"正在配图（{src_label}）")
    if with_images:
        try:
            fill_images(article_id, image_quota(acc), acc)
        except Exception as exc:  # noqa: BLE001 - 配图失败不该让整篇稿子作废
            log_append(article_id, f"配图失败（已跳过）：{type(exc).__name__}: {exc}")

    _set_stage(article_id, "done", "")
    log_append(article_id, "生成完成")
    # 后台生成时状态是 generating，跑完收敛成草稿
    now_art = get_article(article_id)
    if now_art.get("status") == "generating":
        db.update("articles", article_id, {"status": "draft"})
    out = get_article(article_id)
    return {"article": out, "checks": out.get("checks") or result}


def _cover_for(article_id: int, title: str, source_image: Path | None = None,
               theme: str | None = None) -> Path:
    theme = theme or (db.get_setting("theme") or "default")
    out = ARTICLE_DIR / f"a{article_id}" / "cover.png"
    try:
        max_chars = int(db.get_setting("cover_max_chars") or 10)
    except (TypeError, ValueError):
        max_chars = 10
    if isinstance(source_image, str):
        source_image = Path(source_image)
    if source_image is not None and not source_image.exists():
        source_image = None
    return cover_mod.build_cover(title, out, source_image, theme, max_chars=max_chars)


def rerender(article_id: int) -> dict[str, Any]:
    art = get_article(article_id)
    acc = get_account(art["account_id"])
    html = render.render_markdown(
        art.get("markdown") or "",
        theme=account_theme(acc),
        font_size=(db.get_setting("font_size") or "medium"),
    )
    checks_out = checks.run_checks(art.get("title") or "", art.get("digest") or "",
                                   art.get("markdown") or "", None)
    return save_article(article_id, html=html, checks=checks_out)


def regenerate_cover(article_id: int, source_image: str = "") -> dict[str, Any]:
    art = get_article(article_id)
    acc = get_account(art["account_id"])
    src = Path(source_image) if source_image else None
    if src and not src.is_absolute():
        src = UPLOAD_DIR / src
    path = _cover_for(article_id, art.get("title") or "",
                      src if src and src.exists() else None, account_theme(acc))
    return save_article(article_id, cover_path=str(path))


# --------------------------------------------------------------------------
# 建草稿
# --------------------------------------------------------------------------

def _local_upload_path(src: str) -> Path | None:
    """把 markdown 里的图片地址映射到本地文件；外链返回 None。"""
    if not src:
        return None
    if src.startswith(("http://", "https://", "data:")):
        return None
    name = src.split("?")[0].rstrip("/").split("/")[-1]
    for base in (UPLOAD_DIR, UPLOAD_DIR.parent, ARTICLE_DIR):
        cand = base / name
        if cand.exists():
            return cand
    return None


def push_draft(article_id: int) -> dict[str, Any]:
    art = get_article(article_id)
    acc = get_account(art["account_id"])
    if (acc.get("platform") or "wechat") != "wechat":
        raise WrongPlatform(
            f"「{acc['name']}」是"
            f"{PLATFORM_LABELS.get(acc.get('platform') or 'wechat')}账号，"
            "公众号草稿箱投不了它，请用对应平台的投递按钮")
    client = client_for(acc)

    markdown = art.get("markdown") or ""
    if not markdown.strip():
        raise RuntimeError("正文是空的，先在编辑器里写点东西")

    html = render.render_markdown(markdown, theme=account_theme(acc),
                                  font_size=(db.get_setting("font_size") or "medium"))

    refs = IMG_MD.findall(markdown)
    log_append(article_id, f"正文图片 {len(refs)} 张")

    uploaded: dict[str, str] = {}
    for idx, (alt, src) in enumerate(refs, 1):
        src = src.strip()
        if src.startswith("data:"):
            raise RuntimeError("正文里有粘贴进来的 base64 图片，请先「保存到素材」再插入")
        path = _local_upload_path(src)
        if path is None:
            if src.startswith("http") and "mmbiz.qpic.cn" in src:
                continue  # 已经是微信图，不用重传
            if src.startswith("http"):
                try:
                    meta = images.download_image(src)
                    path = Path(meta["path"])
                    log_append(article_id, f"外链图下载 → {path.name}")
                except images.ImageError as exc:
                    raise RuntimeError(f"第 {idx} 张图下载失败：{exc}") from exc
            else:
                raise RuntimeError(f"第 {idx} 张图找不到本地文件：{src}")
        if src in uploaded:
            continue
        url = client.upload_content_image(path)
        uploaded[src] = url
        log_append(article_id, f"图片 {idx} 上传 → {url[:58]}…")

    for src, url in uploaded.items():
        html = html.replace(f'src="{src}"', f'src="{url}"')
        html = html.replace(f"src='{src}'", f'src="{url}"')

    problems = render.draft_blockers(html)
    if problems:
        msg = "；".join(problems)
        log_append(article_id, f"提交前自检未通过：{msg}")
        raise RuntimeError(f"提交前自检未通过：{msg}")

    cover_path = Path(art.get("cover_path") or "")
    if not cover_path.exists():
        cover_path = _cover_for(article_id, art.get("title") or "")
        save_article(article_id, cover_path=str(cover_path))

    thumb = client.upload_permanent_image(cover_path)
    log_append(article_id, f"封面上传 → thumb_media_id={thumb[:14]}…")

    payload = {
        "title": (art.get("title") or "")[:32],
        "author": (art.get("author") or "")[:16],
        "digest": (art.get("digest") or "")[:120],
        "content": html,
        "content_source_url": "",
        "thumb_media_id": thumb,
        "need_open_comment": 1,
        "only_fans_can_comment": 0,
    }
    media_id = client.add_draft(payload)
    log_append(article_id, f"草稿创建成功 media_id={media_id[:18]}…")

    # 回读校验
    verify = verify_draft(client, media_id, expected_img=len(refs))
    save_article(article_id, html=html, media_id=media_id, thumb_media_id=thumb,
                 status="pushed", error="")
    log_append(article_id, "回读校验：" + json.dumps(
        {k: verify[k] for k in ("img", "mmbiz_img", "local_src", "h1", "style_block", "class_count")},
        ensure_ascii=False))

    if not verify["ok"]:
        log_append(article_id, "⚠ 回读有问题：" + "；".join(verify["problems"]))

    invalidate_drafts(acc["id"])
    day = (art.get("created_at") or db.now())[:10]
    topics.remember(day, art.get("slot") or "extra", art.get("title") or "",
                    source="manual", article_id=article_id)
    return {"media_id": media_id, "thumb_media_id": thumb, "verify": verify}


def verify_draft(client: WechatClient, media_id: str, expected_img: int = 0) -> dict[str, Any]:
    items = client.get_draft(media_id)
    if not items:
        return {"ok": False, "problems": ["回读为空"], "img": 0, "mmbiz_img": 0,
                "local_src": 0, "h1": 0, "style_block": 0, "class_count": 0}
    content = items[0].get("content") or ""
    info = render.inspect_html(content)
    problems: list[str] = []
    if expected_img and info["mmbiz_img"] < expected_img:
        problems.append(f"应有 {expected_img} 张 mmbiz 图，回读只有 {info['mmbiz_img']} 张")
    if info["local_src"]:
        problems.append(f"回读仍有 {info['local_src']} 个非 http 图片地址")
    if info["h1"]:
        problems.append(f"回读出现 {info['h1']} 个 h1")
    if info["style_block"]:
        problems.append("回读出现 style 块")
    # class 会被微信异步规范化，只提示不判失败
    info.update({"ok": not problems, "problems": problems, "title": items[0].get("title", "")})
    return info


# --------------------------------------------------------------------------
# 头条草稿（浏览器驱动后台发文页，只存草稿）
# --------------------------------------------------------------------------

def _tt_guard(acc: dict[str, Any]) -> None:
    """头条号的推送冷却闸。

    2026-11-27 实测：一小时内连续推十几次之后，**所有**保存都开始返回 7050
    （连纯文字、连几分钟前刚成功过的那篇也一样）。等一段时间再推就恢复。
    所以这不是"内容有问题"，是这个号被短期限流了——继续硬推只会把窗口越拖越长。

    默认两次投递至少间隔 3 分钟，可在设置里用 toutiao_min_gap（秒）调。
    """
    try:
        gap = int(db.get_setting("toutiao_min_gap") or 180)
    except (TypeError, ValueError):
        gap = 180
    if gap <= 0:
        return
    key = f"tt_last_push_{acc['id']}"
    raw = db.get_setting(key) or ""
    try:
        last = float(raw)
    except (TypeError, ValueError):
        return
    passed = time.time() - last
    if passed < gap:
        wait = int(gap - passed)
        raise RuntimeError(
            f"「{acc['name']}」刚推过一次头条草稿（{int(passed)} 秒前），还差 {wait} 秒。"
            f"头条号短时间内连续存草稿会被判 7050，越推恢复越慢——等一下再点。")


def _tt_mark_push(acc: dict[str, Any]) -> None:
    db.set_setting(f"tt_last_push_{acc['id']}", str(time.time()))


def push_toutiao(article_id: int) -> dict[str, Any]:
    """把这篇稿子存进头条草稿箱。

    和公众号那条链的根本区别：公众号有接口，头条只能开浏览器点。所以错误要原样
    往上抛，尤其是 LoginRequired / ManualActionRequired —— 前端要靠它们提示怎么做。
    """
    art = get_article(article_id)
    acc = get_account(art["account_id"])
    if (acc.get("platform") or "wechat") != "toutiao":
        raise WrongPlatform(
            f"「{acc['name']}」不是头条号账号（属于"
            f"{PLATFORM_LABELS.get(acc.get('platform') or 'wechat')}），头条草稿不能往它名下投")
    if not (art.get("markdown") or "").strip():
        raise RuntimeError("正文是空的，先在编辑器里写点东西")
    _tt_guard(acc)
    _tt_mark_push(acc)
    log_append(article_id, f"头条草稿：开始投递到「{acc['name']}」")
    try:
        res = toutiao.create_draft(
            art,
            account_id=acc["id"],
            account_name=acc["name"],
            theme=account_theme(acc),
            font_size=(db.get_setting("font_size") or "medium"),
            log=lambda s: log_append(article_id, "头条 · " + s),
        )
    except (toutiao.LoginRequired, toutiao.ManualActionRequired) as exc:
        # 没登录 / 要人工绑手机：都不是稿子的问题，别把 error 字段弄脏，只记日志
        log_append(article_id, f"头条投递被挡：{exc}")
        raise
    except Exception as exc:  # noqa: BLE001
        log_append(article_id, f"头条草稿失败：{exc}")
        save_article(article_id, error=str(exc))
        raise
    save_article(
        article_id,
        tt_url=res.get("url") or "",
        tt_at=db.now(),
        tt_state={k: res.get(k) for k in
                  ("title", "images", "images_failed", "skipped", "verify",
                   "fallback_no_images", "note")},
        error="",
    )
    if res.get("fallback_no_images"):
        log_append(article_id, f"头条草稿已保存（纯文字版）：{res.get('note') or ''}")
    else:
        log_append(article_id, f"头条草稿已保存：图片 {res.get('images', 0)} 张进正文")
    return res


# --------------------------------------------------------------------------
# 发布（必须人工确认）
# --------------------------------------------------------------------------

def publish(article_id: int, confirm: bool = False) -> dict[str, Any]:
    if not confirm:
        raise RuntimeError("发布需要显式确认")
    art = get_article(article_id)
    acc = get_account(art["account_id"])
    if (acc.get("platform") or "wechat") != "wechat":
        raise RuntimeError("只有公众号账号能走「正式发布」")
    if not acc.get("allow_publish"):
        raise RuntimeError("这个账号没有开启「允许发布」，到账号设置里打开后再试")
    if not art.get("media_id"):
        raise RuntimeError("还没有建草稿，先点「存入草稿箱」")
    client = client_for(acc)
    try:
        pid = client.publish(art["media_id"])
    except WechatError as exc:
        save_article(article_id, error=str(exc))
        log_append(article_id, f"发布失败：{exc}")
        raise
    save_article(article_id, status="published", publish_id=pid, published_at=db.now(), error="")
    log_append(article_id, f"已提交发布 publish_id={pid}")
    return {"publish_id": pid}


def publish_status(article_id: int) -> dict[str, Any]:
    art = get_article(article_id)
    if not art.get("publish_id"):
        raise RuntimeError("没有 publish_id")
    client = client_for(get_account(art["account_id"], platform="wechat"))
    d = client.publish_status(art["publish_id"])
    st = d.get("publish_status")
    if st == 0 and art.get("status") != "published":
        save_article(article_id, status="published")
    return d


# --------------------------------------------------------------------------
# 草稿箱同步
# --------------------------------------------------------------------------

def list_wechat_drafts(account_id: int | None = None, count: int = 20) -> dict[str, Any]:
    acc = get_account(account_id, platform="wechat")
    client = client_for(acc)
    d = client.batchget_drafts(count=count, no_content=True)
    items = []
    for it in d.get("item") or []:
        news = ((it.get("content") or {}).get("news_item") or [{}])[0]
        items.append({
            "media_id": it.get("media_id"),
            "title": news.get("title", ""),
            "digest": news.get("digest", ""),
            "author": news.get("author", ""),
            "update_time": it.get("update_time"),
            "url": news.get("url", ""),
        })
    return {"total": d.get("total_count", 0), "drafts": items}


_DRAFTS_CACHE: dict[int, tuple[float, dict[str, Any]]] = {}
DRAFTS_TTL = 45


def list_wechat_drafts_cached(account_id: int | None = None, count: int = 20,
                              refresh: bool = False) -> dict[str, Any]:
    """带缓存的草稿箱列表。

    微信 batchget 要 0.4-3 秒，而这是每次打开草稿箱页都要的 —— 缓存住，
    并把页面本身改成"先出壳、数据异步来"，打开就不用等微信接口了。
    """
    acc = get_account(account_id, platform="wechat")
    key = int(acc["id"])
    if not refresh:
        hit = _DRAFTS_CACHE.get(key)
        if hit and time.time() - hit[0] < DRAFTS_TTL:
            return {**hit[1], "cached": True, "age": int(time.time() - hit[0])}
    data = list_wechat_drafts(key, count)
    _DRAFTS_CACHE[key] = (time.time(), data)
    return {**data, "cached": False, "age": 0}


def invalidate_drafts(account_id: int | None = None) -> None:
    if account_id is None:
        _DRAFTS_CACHE.clear()
    else:
        _DRAFTS_CACHE.pop(int(account_id), None)


def delete_draft(article_id: int) -> dict[str, Any]:
    art = get_article(article_id)
    if not art.get("media_id"):
        raise RuntimeError("这篇还没有 media_id")
    client = client_for(get_account(art["account_id"], platform="wechat"))
    client.delete_draft(art["media_id"])
    invalidate_drafts(art["account_id"])
    save_article(article_id, media_id="", thumb_media_id="", status="draft")
    log_append(article_id, "已删除草稿箱里的旧稿")
    return {"ok": True}


# --------------------------------------------------------------------------
# 配图的单张操作（移除 / 改图注 / 调序 / 换一张）
# --------------------------------------------------------------------------

_IMG_LINE = re.compile(r"^!\[(.*?)\]\((.+?)\)\s*$")


def _imgs_meta(art: dict) -> list[dict[str, Any]]:
    meta = art.get("images_meta") or []
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = []
    return [m for m in meta if isinstance(m, dict)]


def annotate_missing(meta: Any) -> list[dict[str, Any]]:
    """给每张图标注本地文件还在不在，前端据此显示「文件丢失」而不是 undefined×undefined。

    为什么要在出口统一标：卡片上的宽高、缩略图都依赖本地文件，文件没了就只剩一串
    undefined 和碎裂图标，看不出是「图丢了」还是「页面坏了」。标出来之后前端能直接
    提示「文件丢失，点 AI 重生成」，也方便脚本一键体检。
    """
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            meta = []
    out: list[dict[str, Any]] = []
    for m in meta or []:
        if not isinstance(m, dict):
            continue
        u = m.get("url") or ""
        gone = bool(u.startswith("/media/")) and _local_upload_path(u) is None
        out.append({**m, "missing": True} if gone else m)
    return out


def _apply_images(article_id: int, markdown: str, meta: list[dict[str, Any]],
                  *, cover_src: str | None = None) -> dict[str, Any]:
    """写回正文 + 图片元数据，并重渲染（必要时重做封面）。"""
    art = get_article(article_id)
    acc = get_account(art["account_id"])
    theme = account_theme(acc)
    # 插图/换图/删图之后，标签行很可能被挤到图片上面去，这里统一再收一次：
    # 只留一组、贴在最底部。幂等，重复调用没有副作用。
    markdown = writer.normalize_tags(markdown, acc.get("platform") or "wechat")
    html = render.render_markdown(markdown, theme=theme,
                                  font_size=(db.get_setting("font_size") or "medium"))
    fields: dict[str, Any] = {
        "markdown": markdown, "html": html,
        "images_meta": json.dumps(meta, ensure_ascii=False),
        "images": [m.get("url", "") for m in meta],
        "checks": checks.run_checks(art.get("title") or "", art.get("digest") or "",
                                    markdown, None, **check_kwargs(acc)),
    }
    if cover_src is not None:
        fields["cover_path"] = str(_cover_for(article_id, art.get("title") or "", cover_src, theme))
    return save_article(article_id, **fields)


def _scene_near(markdown: str, url: str) -> str:
    """取这张图「上下文那句话」当场景描述。

    优先用图片紧前面那段非空文字（看图说话型的稿子里，那句话就是画面内容），
    没有再往下找，最后退回空串。
    """
    lines = markdown.split("\n")
    idx = -1
    for i, ln in enumerate(lines):
        m = _IMG_LINE.match(ln.strip())
        if m and m.group(2) == url:
            idx = i
            break
    if idx < 0:
        return ""
    for i in range(idx - 1, -1, -1):
        t = lines[i].strip()
        if t and not t.startswith("!["):
            return re.sub(r"[*#>`]", "", t)[:120]
    for i in range(idx + 1, len(lines)):
        t = lines[i].strip()
        if t and not t.startswith("!["):
            return re.sub(r"[*#>`]", "", t)[:120]
    return ""


def regen_ai_image(article_id: int, url: str, extra: str = "",
                   scene: str = "") -> dict[str, Any]:
    """把正文里某张图用 AI 重新生成，**就地替换**（可附加新的要求）。

    同一篇稿子同时只允许一次重生成（`_img_lock`）：并发写入会互相覆盖，还会把仍在
    正文里的原图删掉。后来的点击排队等前面那次跑完，排队耗时可忽略（用户本来就在等图）。
    """
    with _img_lock(article_id):
        return _regen_ai_image_locked(article_id, url, extra, scene)


def _regen_ai_image_locked(article_id: int, url: str, extra: str = "",
                           scene: str = "") -> dict[str, Any]:
    """真正的重生成逻辑。调用方必须已持有该稿件的锁。

    场景描述怎么来的：优先用调用方给的，其次取这张图上下文那句话
    （看图说话的稿子里，旁边那句文案就是画面内容），最后才退回文章标题。
    """
    art = get_article(article_id)
    acc = get_account(art["account_id"])
    md = art.get("markdown") or ""
    old = (url or "").strip()
    if not old or old not in md:
        raise RuntimeError("这张图已经不在正文里了，刷新页面再试")

    ch = characters.for_account(acc)
    prompt_scene = (scene or "").strip() or _scene_near(md, old) or (art.get("title") or "")
    log_append(article_id,
               f"AI 重生成配图，场景：{prompt_scene[:32]}"
               + (f"｜补充要求：{extra[:40]}" if extra else "")
               + (f"｜人物：{ch['name']}" if ch else ""))

    if ch:
        new_url = aigen.generate_scene(ch, prompt_scene, account=acc, style=extra)
    else:
        new_url = aigen.generate(prompt_scene, acc, extra=extra)

    # 生成花了 50~90 秒，期间正文可能被别处改过（改图注、抓新图、另一张图重生成）。
    # 重新读一次再改，绝不拿开头那份快照写回——那是丢图事故的直接原因。
    art = get_article(article_id)
    md = art.get("markdown") or ""
    if old not in md:
        raise RuntimeError(f"这张图在生成期间已被替换或移除，新图已存但没写进正文：{new_url}")
    # 正文替换（只换这一处）
    lines = []
    for ln in md.split("\n"):
        m = _IMG_LINE.match(ln.strip())
        if m and m.group(2) == old:
            lines.append(f"![{m.group(1)}]({new_url})")
        else:
            lines.append(ln)
    new_md = "\n".join(lines)

    # 元数据同步（宽高按新图实际尺寸，图注保留原来的）
    new_path = _local_upload_path(new_url)
    w = h = 0
    if new_path is not None:
        try:
            from PIL import Image
            with Image.open(new_path) as im:
                w, h = im.size
        except Exception:  # noqa: BLE001
            pass
    meta = _imgs_meta(art)
    for i, m in enumerate(meta):
        if m.get("url") == old:
            meta[i] = {**m, "url": new_url, "width": w or m.get("width") or 0,
                       "height": h or m.get("height") or 0,
                       "source": "ai", "site": m.get("site") or "AI 生成"}

    # 换掉的是第一张图的话，封面也要跟着重做（封面默认取第一张压字）
    first_img = next((mm.group(2) for mm in
                      (_IMG_LINE.match(x.strip()) for x in new_md.split("\n")) if mm), "")
    cover_src = None
    if acc.get("cover_from_image", 1) and first_img == new_url and new_path is not None:
        cover_src = str(new_path)

    out = _apply_images(article_id, new_md, meta, cover_src=cover_src)

    # 旧图没人用了才删，免得反复重生成把磁盘撑爆。
    # 判断要覆盖正文 / 配图元数据 / images 列表 / 封面四条引用路径——只查 markdown 的话，
    # 卡片里还挂着、正文却临时没引用的图会被误删（这次丢图就是这么放大的）。
    try:
        if old.startswith("/media/uploads/") and old not in new_md:
            still = db.query_one(
                "SELECT id FROM articles WHERE id<>? AND (markdown LIKE ? OR images_meta LIKE ?"
                " OR images LIKE ? OR cover_path LIKE ?)",
                (article_id, f"%{old}%", f"%{old}%", f"%{old}%", f"%{old}%"))
            if not still:
                p = _local_upload_path(old)
                if p is not None:
                    p.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass
    log_append(article_id, f"配图已重生成 → {new_url}")
    return out


def remove_image(article_id: int, url: str) -> dict[str, Any]:
    art = get_article(article_id)
    key = (url or "").split("/")[-1] or url      # 只给文件名也能匹配
    lines = [ln for ln in (art.get("markdown") or "").split("\n") if key not in ln]
    markdown = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    meta = [m for m in _imgs_meta(art)
            if (m.get("url") or "").split("/")[-1] != key and m.get("url") != url]
    out = _apply_images(article_id, markdown, meta)
    log_append(article_id, f"移除配图 {url.split('/')[-1]}")
    return out


def set_caption(article_id: int, url: str, caption: str) -> dict[str, Any]:
    art = get_article(article_id)
    caption = (caption or "").strip().replace("]", " ").replace(")", " ")[:40]
    lines = []
    for ln in (art.get("markdown") or "").split("\n"):
        m = _IMG_LINE.match(ln.strip())
        if m and m.group(2) == url:
            lines.append(f"![{caption}]({url})")
        else:
            lines.append(ln)
    meta = _imgs_meta(art)
    for m in meta:
        if m.get("url") == url:
            m["caption"] = caption
    return _apply_images(article_id, "\n".join(lines), meta)


def reorder_images(article_id: int, urls: list[str]) -> dict[str, Any]:
    """按给定 url 顺序重排正文里的图片行。"""
    art = get_article(article_id)
    markdown = art.get("markdown") or ""
    order = {u: i for i, u in enumerate(urls)}
    lines = markdown.split("\n")

    # 收集图片行的位置和内容
    slots: list[tuple[int, str]] = []
    for idx, ln in enumerate(lines):
        m = _IMG_LINE.match(ln.strip())
        if m:
            slots.append((idx, ln.strip()))
    if not slots:
        return art

    img_lines = {}
    for _, ln in slots:
        m = _IMG_LINE.match(ln)
        img_lines[m.group(2)] = ln

    # 按新顺序重新填回原来的位置
    ordered_urls = [u for u in urls if u in img_lines]
    ordered_urls += [u for u in img_lines if u not in ordered_urls]
    for (idx, _), u in zip(slots, ordered_urls):
        lines[idx] = img_lines[u]

    meta = _imgs_meta(art)
    meta_by_url = {m.get("url"): m for m in meta}
    new_meta = [meta_by_url[u] for u in ordered_urls if u in meta_by_url]
    out = _apply_images(article_id, "\n".join(lines), new_meta)
    log_append(article_id, "调整了配图顺序")
    return out


def swap_image(article_id: int, url: str) -> dict[str, Any]:
    """把某一张换成同选题的另一张网图。"""
    art = get_article(article_id)
    acc = get_account(art["account_id"])
    topic = art.get("title") or ""
    if art.get("topic_id"):
        row = db.query_one("SELECT title FROM topics WHERE id=?", (art["topic_id"],))
        if row and row["title"]:
            topic = row["title"]

    meta = _imgs_meta(art)
    # 去重要按「原始图片地址」和「内容哈希」来比 —— 本地文件名每次下载都会变，
    # 拿本地路径比永远匹配不上，会换回来同一张图。
    have_locals = {m.get("url") for m in meta}
    have_origins = {m.get("origin") for m in meta if m.get("origin")}
    have_hashes = [m.get("hash") for m in meta if m.get("hash")]

    pool = [c for c in imgsource.candidates(topic, max(6, len(meta) + 2))
            if c["url"] not in have_origins and c["url"] not in have_locals]
    if not pool:
        raise RuntimeError("没找到可选的新图，换个选题词再试")

    try:
        max_data = int(db.get_setting("max_data_images", 1))
    except (TypeError, ValueError):
        max_data = 1

    new: list[dict[str, Any]] = []
    for item in pool[:25]:
        got = imgsource.download_many([item], 1, avoid=have_hashes, max_data_images=max_data)
        if got:
            new = got
            break
    if not new:
        raise RuntimeError("候选图都没通过校验")
    fresh = new[0]

    lines = []
    for ln in (art.get("markdown") or "").split("\n"):
        m = _IMG_LINE.match(ln.strip())
        if m and m.group(2) == url:
            lines.append(f"![{fresh.get('caption') or ''}]({fresh['url']})")
        else:
            lines.append(ln)

    replaced = False
    for i, m in enumerate(meta):
        if m.get("url") == url:
            meta[i] = fresh
            replaced = True
    if not replaced:
        meta.append(fresh)

    cover_src = fresh["path"] if acc.get("cover_from_image", 1) else None
    out = _apply_images(article_id, "\n".join(lines), meta, cover_src=cover_src)
    log_append(article_id, f"换了一张配图 → {fresh.get('caption') or fresh['name']}")
    return out
