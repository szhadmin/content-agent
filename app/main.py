"""FastAPI 入口。"""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import aigen, characters, checks, cover as cover_mod, db, images, personas, photo, pipeline, render, scheduler, security, topics, toutiao, writer
from .prompts import DEFAULT_STYLE_PROMPT
from .settings import (
    APP_NAME,
    ARTICLE_DIR,
    BASE_DIR,
    COVER_DIR,
    DEFAULT_LLM,
    PLATFORM_LABELS,
    PLATFORMS,
    SESSION_COOKIE,
    SLOT_LABELS,
    UPLOAD_DIR,
    URL_PREFIX,
    public_base_url,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("wxwb")

TEMPLATES_DIR = BASE_DIR / "app" / "templates"
STATIC_DIR = BASE_DIR / "app" / "static"


def seed_defaults() -> None:
    if db.get_setting("style_prompt") is None:
        db.set_setting("style_prompt", DEFAULT_STYLE_PROMPT)
    if db.get_setting("theme") is None:
        db.set_setting("theme", "default")
    if db.get_setting("font_size") is None:
        db.set_setting("font_size", "medium")
    if db.get_setting("llm") is None:
        db.set_setting("llm", DEFAULT_LLM)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    seed_defaults()
    security.ensure_bootstrap_admin()
    # 服务重启会把后台生成任务带走，这些稿子会永远卡在"生成中"，启动时收尾
    stale = db.query("SELECT id FROM articles WHERE status='generating'")
    if stale:
        db.execute("UPDATE articles SET status='failed', stage='failed', "
                   "error='服务重启，生成中断，请点「重新写一篇」' WHERE status='generating'")
        log.info("已把 %d 篇中断的生成任务标为失败", len(stale))
    task = asyncio.create_task(scheduler.loop(30))
    log.info("%s 已启动", APP_NAME)
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title=APP_NAME, lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/media/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")
app.mount("/media/covers", StaticFiles(directory=str(COVER_DIR)), name="covers")
app.mount("/media/articles", StaticFiles(directory=str(ARTICLE_DIR)), name="articlemedia")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["SLOT_LABELS"] = SLOT_LABELS
templates.env.globals["PLATFORMS"] = PLATFORMS
templates.env.globals["PLATFORM_LABELS"] = PLATFORM_LABELS
templates.env.globals["P"] = URL_PREFIX


def media_url(path: str) -> str:
    """给 /media/... 补上子路径前缀。模板里统一用 {{ media(x) }}，别再手拼。"""
    p = str(path or "")
    if p.startswith(("/media/", "media/")):
        return URL_PREFIX + (p if p.startswith("/") else "/" + p)
    return p


templates.env.globals["media"] = media_url
templates.env.globals["APP_NAME"] = APP_NAME


@app.middleware("http")
async def _strip_prefix(request: Request, call_next):
    """挂在子路径下时，反代可能保留也可能剥掉前缀，两种都要能跑。"""
    if URL_PREFIX:
        path = request.scope.get("path", "")
        if path == URL_PREFIX or path.startswith(URL_PREFIX + "/"):
            request.scope["path"] = path[len(URL_PREFIX):] or "/"
            request.scope["root_path"] = ""
    return await call_next(request)


# --------------------------------------------------------------------------
# 鉴权
# --------------------------------------------------------------------------

def current_user(request: Request) -> dict[str, Any] | None:
    return security.session_user(request.cookies.get(SESSION_COOKIE))


def need_user(request: Request) -> dict[str, Any]:
    user = current_user(request)
    if not user:
        raise PermissionError("未登录")
    return user


def guard(request: Request) -> tuple[dict[str, Any] | None, Response | None]:
    user = current_user(request)
    if user:
        return user, None
    if request.url.path.startswith("/api/"):
        return None, JSONResponse({"ok": False, "error": "未登录"}, status_code=401)
    return None, RedirectResponse(URL_PREFIX + "/login", status_code=303)


@app.exception_handler(PermissionError)
async def _perm_handler(request: Request, exc: PermissionError):
    if request.url.path.startswith("/api/"):
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=401)
    return RedirectResponse(URL_PREFIX + "/login", status_code=303)


def _split_terms(text: str) -> list[str]:
    import re as _re

    return [x for x in _re.split(r"[,，、/|\s;；]+", text or "") if x.strip()]


def ok(**kw: Any) -> JSONResponse:
    return JSONResponse({"ok": True, **kw})


def fail(msg: str, code: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


def _tpl_style() -> str:
    """探测 Starlette 版本的 TemplateResponse 签名（新版 / 老版）。"""
    import inspect

    try:
        params = inspect.signature(templates.TemplateResponse).parameters
    except (TypeError, ValueError):
        return "new"
    return "new" if "request" in params else "old"


_TPL_STYLE = _tpl_style()


def render_page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("request", request)
    ctx.setdefault("active", "")
    if _TPL_STYLE == "new":
        return templates.TemplateResponse(request=request, name=name, context=ctx)
    return templates.TemplateResponse(name, ctx)


# --------------------------------------------------------------------------
# 登录
# --------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user(request):
        return RedirectResponse(URL_PREFIX + "/", status_code=303)
    return render_page(request, "login.html", error="")


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    row = db.query_one("SELECT * FROM users WHERE username=?", (username.strip(),))
    if not row or not security.verify_password(password, row["password_hash"]):
        return render_page(request, "login.html", error="用户名或密码不对")
    token = security.create_session(row["username"])
    resp = RedirectResponse(URL_PREFIX + "/", status_code=303)
    resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=security.SESSION_TTL_SECONDS)
    return resp


@app.get("/logout")
async def logout(request: Request):
    security.destroy_session(request.cookies.get(SESSION_COOKIE))
    resp = RedirectResponse(URL_PREFIX + "/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# --------------------------------------------------------------------------
# 首页
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, platform: str | None = None):
    user, redirect = guard(request)
    if redirect:
        return redirect
    sel = platform if platform in PLATFORMS else None
    accs = pipeline.list_accounts()
    all_arts = pipeline.list_articles(400)
    arts = [a for a in all_arts if not sel or (a.get("platform") or "wechat") == sel]
    platform_counts = {
        k: len([a for a in all_arts if (a.get("platform") or "wechat") == k])
        for k in PLATFORMS
    }
    stats = {
        "accounts": len([a for a in accs if not sel or (a.get("platform") or "wechat") == sel]),
        "total": len(arts),
        "drafts": len([a for a in arts if a["status"] == "draft"]),
        "generating": len([a for a in arts if a["status"] == "generating"]),
        "pushed": len([a for a in arts if a["status"] == "pushed"]),
        "published": len([a for a in arts if a["status"] == "published"]),
    }
    return render_page(
        request, "dashboard.html", user=user, active="home",
        accounts=accs, articles=arts, stats=stats,
        platform=sel, platform_counts=platform_counts,
        topics_recent=topics.recent(8),
        llm_ready=bool(writer.llm_config().get("api_key")),
        font=cover_mod.font_status(),
        schedules=scheduler.db_schedules(),
    )


# --------------------------------------------------------------------------
# 选题
# --------------------------------------------------------------------------

@app.get("/topics", response_class=HTMLResponse)
async def topics_page(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    return render_page(request, "topics.html", user=user, active="topics",
                       recent=topics.recent(60), accounts=pipeline.list_accounts())


@app.post("/api/topics/fetch")
async def api_topics_fetch(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    limit = int(body.get("limit") or 25)
    try:
        items = await asyncio.to_thread(topics.candidates, limit)
    except Exception as exc:  # noqa: BLE001
        return fail(f"抓取失败：{exc}")

    # 按账号领域标注"这条适不适合本号"
    acc = None
    if body.get("account_id"):
        try:
            acc = pipeline.get_account(body["account_id"])
        except RuntimeError:
            acc = None
    # 默认只走关键词规则（快，1 秒内出列表）；模型分类由前端随后单独调 /api/topics/classify
    if int(body.get("classify") or 0):
        try:
            items = await asyncio.to_thread(
                topics.refine_categories, items,
                use_llm=bool(writer.llm_config().get("api_key")))
        except Exception:  # noqa: BLE001
            log.exception("热搜分类失败，保留规则结果")

    domain_terms = [t for t in _split_terms((acc or {}).get("topic_domain") or "") if len(t) >= 2]
    star_cat = topics.category_for_domain((acc or {}).get("topic_domain") or "")
    for it in items:
        hits = [t for t in domain_terms if t in it["word"]]
        # 词条命中关键词，或者它所属分类就是这个号的主分类，都算"适合本号"
        cat_match = bool(star_cat) and it.get("category") == star_cat
        if cat_match and not hits:
            hits = [f"{star_cat}分类"]
        it["fit_terms"] = hits
        it["domain_hit"] = bool(hits) or not domain_terms
    return ok(items=items, text=topics.candidates_text(items),
              counts=topics.category_counts(items),
              categories=list(topics.CATEGORIES),
              default_category=star_cat)


@app.post("/api/topics/classify")
async def api_topics_classify(request: Request):
    """把还没归好类的热词批量交给模型（带缓存），前端拿到后原地更新 tab。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    words = [str(w) for w in (body.get("words") or [])][:60]
    if not words:
        return ok(mapping={})
    try:
        mapping = await asyncio.to_thread(
            topics.classify_words, words,
            bool(writer.llm_config().get("api_key")))
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 500)
    return ok(mapping=mapping)


@app.post("/api/topics/pick")
async def api_topics_pick(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    limit = int(body.get("limit") or 25)
    acc = pipeline.get_account(body.get("account_id"))
    items = topics.candidates(limit)
    if not items:
        return fail("没抓到候选")
    try:
        data = await asyncio.to_thread(
            writer.pick_topic, items, acc.get("author") or "",
            topics.already_written_titles(14),
            topics.candidates_text(items), account=acc)
    except writer.LLMError as exc:
        return fail(str(exc))
    item = data.get("item")
    return ok(selection=data, item=item, candidates=items,
              text=topics.candidates_text(items),
              counts=topics.category_counts(items),
              categories=list(topics.CATEGORIES),
              default_category=topics.category_for_domain(acc.get("topic_domain") or ""))


@app.post("/api/topics/manual")
async def api_topics_manual(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        return fail("标题不能为空")
    tid = topics.remember(db.today(), body.get("slot") or "extra", title, source="manual")
    return ok(topic_id=tid)


# --------------------------------------------------------------------------
# 生成 / 文章
# --------------------------------------------------------------------------

async def _bg_generate(article_id: int, extra: str, humanize: bool) -> None:
    """后台跑生成：前端可以立刻跳到稿子页看进度。"""
    try:
        await asyncio.to_thread(pipeline.run_generation, article_id,
                                extra=extra, humanize=humanize)
    except Exception as exc:  # noqa: BLE001
        log.exception("后台生成失败 article=%s", article_id)
        try:
            pipeline.save_article(article_id, status="failed", error=str(exc))
            pipeline._set_stage(article_id, "failed", "")
            pipeline.log_append(article_id, f"生成失败：{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            log.exception("写失败状态也失败了")


@app.get("/api/article/{article_id}/progress")
async def api_article_progress(article_id: int, request: Request):
    """轻量进度查询：只取需要的列，前端 1-2 秒轮询一次。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    row = db.query_one(
        "SELECT id,title,status,stage,log,error,images_meta,updated_at,"
        "LENGTH(markdown) AS md_len FROM articles WHERE id=?", (article_id,))
    if not row:
        return fail("文章不存在", 404)
    try:
        imgs = json.loads(row["images_meta"] or "[]")
    except ValueError:
        imgs = []
    return ok(progress={
        "id": row["id"],
        "title": row["title"] or "",
        "status": row["status"],
        "stage": row["stage"] or "",
        "error": row["error"] or "",
        "log": (row["log"] or "")[-4000:],
        "images": len(imgs),
        "has_body": bool(row["md_len"]),
        "cover_url": f"{URL_PREFIX}/media/articles/a{article_id}/cover.png?v={row['updated_at']}",
    })


@app.post("/api/generate")
async def api_generate(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    topic_title = (body.get("topic") or "").strip()
    slot = body.get("slot") or "extra"
    if not topic_title:
        return fail("先选一个选题，或自己填一个")
    try:
        acc = pipeline.get_account(body.get("account_id"))
        tid = topics.remember(db.today(), slot, topic_title, source=body.get("source") or "manual",
                              rank=body.get("rank"), heat=body.get("heat"))
        article_id = pipeline.create_article(
            account_id=acc["id"], title=topic_title, slot=slot,
            created_by=user["username"], topic_id=tid,
        )
        db.execute("UPDATE topics SET article_id=? WHERE id=?", (article_id, tid))

        if body.get("async_mode"):
            # 立刻返回，前端直接跳稿子页看实时进度
            db.update("articles", article_id,
                      {"status": "generating", "stage": "queued"})
            pipeline.log_append(article_id, "已排队，开始生成…")
            asyncio.create_task(_bg_generate(
                article_id, (body.get("extra") or "").strip(), bool(body.get("humanize"))))
            return ok(article_id=article_id, async_mode=True)

        result = await asyncio.to_thread(
            pipeline.run_generation, article_id,
            extra=(body.get("extra") or "").strip(),
            humanize=bool(body.get("humanize")),
        )
    except (writer.LLMError, RuntimeError) as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("生成失败")
        return fail(f"{type(exc).__name__}: {exc}", 500)
    return ok(article_id=article_id, checks=result["checks"])


@app.get("/article/{article_id}", response_class=HTMLResponse)
async def article_page(request: Request, article_id: int):
    user, redirect = guard(request)
    if redirect:
        return redirect
    try:
        art = pipeline.get_article(article_id)
    except RuntimeError as exc:
        return render_page(request, "error.html", user=user, message=str(exc))
    acc = pipeline.get_account(art["account_id"])
    platform = art.get("platform") or (acc.get("platform") or "wechat")
    return render_page(request, "article.html", user=user, active="home",
                       article=art, account=acc,
                       platform=platform,
                       platforms=PLATFORMS,
                       tt_status=toutiao.login_status(acc["id"]) if platform == "toutiao" else None,
                       themes=render.THEMES,
                       theme=pipeline.account_theme(acc),
                       font_size=db.get_setting("font_size") or "medium",
                       cover_url=f"{URL_PREFIX}/media/articles/a{article_id}/cover.png?v={art['updated_at']}",
                       images_meta=art.get("images_meta") or [],
                       image_quota=pipeline.image_quota(acc),
                       character=characters.for_account(acc),
                       generating=(art.get("status") == "generating"))


@app.get("/api/article/{article_id}")
async def api_article(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    try:
        art = pipeline.get_article(article_id)
    except RuntimeError as exc:
        return fail(str(exc), 404)
    art.pop("html", None)
    return ok(article=art)


@app.post("/api/article/{article_id}/save")
async def api_article_save(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    fields = {k: body[k] for k in ("title", "digest", "author", "markdown") if k in body}
    art = pipeline.save_article(article_id, **fields)
    result = checks.run_checks(art["title"], art["digest"], art["markdown"], art.get("images") or [])
    art = pipeline.save_article(article_id, checks=result)
    return ok(article={k: art[k] for k in ("id", "title", "digest", "author", "markdown")},
              checks=result)


@app.post("/api/article/{article_id}/preview")
async def api_article_preview(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    markdown = body.get("markdown")
    art = pipeline.get_article(article_id)
    if markdown is None:
        markdown = art["markdown"]
    # 预览默认用**这个账号自己的主题**，不是全局主题（账号页可以按人设选主题）
    acc = pipeline.get_account(art["account_id"])
    default_theme = pipeline.account_theme(acc)
    html = render.render_markdown(markdown, theme=body.get("theme") or default_theme,
                                  font_size=body.get("font_size") or (db.get_setting("font_size") or "medium"))
    info = render.inspect_html(html)
    result = checks.run_checks(body.get("title") or "", body.get("digest") or "", markdown,
                               (art.get("images") or []),
                               **pipeline.check_kwargs(acc))
    return ok(html=html, info=info, checks=result,
              blockers=render.draft_blockers(html, allow_pending_uploads=True),
              pending=render.pending_note(html))


@app.post("/api/article/{article_id}/regenerate")
async def api_article_regenerate(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    try:
        result = await asyncio.to_thread(
            pipeline.run_generation, article_id,
            extra=(body.get("extra") or "").strip(),
            humanize=bool(body.get("humanize")))
    except writer.LLMError as exc:
        return fail(str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("重新生成失败")
        return fail(f"{type(exc).__name__}: {exc}", 500)
    return ok(article=pipeline.get_article(article_id), checks=result["checks"])


@app.post("/api/article/{article_id}/humanize")
async def api_article_humanize(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    art = pipeline.get_article(article_id)
    try:
        md = await asyncio.to_thread(writer.humanize, art["markdown"])
    except writer.LLMError as exc:
        return fail(str(exc))
    pipeline.save_article(article_id, markdown=md)
    pipeline.log_append(article_id, "已过去 AI 味")
    return ok(markdown=md)


@app.post("/api/article/{article_id}/optimize_layout")
async def api_article_optimize_layout(article_id: int, request: Request):
    """AI 排版优化：只动 Markdown 结构，不动事实和语气。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    art = pipeline.get_article(article_id)
    acc = pipeline.get_account(art["account_id"])
    if not (art.get("markdown") or "").strip():
        return fail("正文是空的，先写点东西再优化排版")
    try:
        md = await asyncio.to_thread(writer.optimize_layout, art["markdown"], acc)
    except writer.LLMError as exc:
        return fail(str(exc))
    pipeline.save_article(article_id, markdown=md)
    pipeline.log_append(article_id, f"AI 排版优化完成（按{PLATFORMS.get(acc.get('platform') or 'wechat', {}).get('label', '公众号')}版式）")
    return ok(markdown=md)


@app.post("/api/article/{article_id}/titles")
async def api_article_titles(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    art = pipeline.get_article(article_id)
    acc = pipeline.get_account(art["account_id"])
    try:
        titles = await asyncio.to_thread(writer.suggest_titles, art["markdown"],
                                         acc.get("platform") or "wechat")
    except writer.LLMError as exc:
        return fail(str(exc))
    return ok(titles=titles)


@app.post("/api/article/{article_id}/cover")
async def api_article_cover(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    try:
        await asyncio.to_thread(pipeline.regenerate_cover, article_id,
                                (body.get("source") or "").strip())
    except Exception as exc:  # noqa: BLE001
        return fail(f"封面生成失败：{exc}")
    return ok(state=_art_state(article_id))


@app.post("/api/article/{article_id}/fetch_images")
async def api_article_fetch_images(article_id: int, request: Request):
    """按选题重新配图并插入正文（会替换上一次自动插的图）。

    走 fill_images：来源看账号的 image_source，网图号抓图、AI 号生图，别让按钮行为和设置不一致。
    """
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    want = body.get("want")
    try:
        result = await asyncio.to_thread(pipeline.fill_images, article_id, want)
    except Exception as exc:  # noqa: BLE001
        log.exception("配图失败")
        return fail(f"配图失败：{type(exc).__name__}: {exc}", 500)
    return ok(images=result.get("images") or [], note=result.get("note") or "",
              state=_art_state(article_id))


def _art_state(article_id: int) -> dict[str, Any]:
    art = pipeline.get_article(article_id)
    return {
        "id": art["id"],
        "status": art["status"],
        "media_id": art.get("media_id") or "",
        "thumb_media_id": art.get("thumb_media_id") or "",
        "title": art.get("title") or "",
        "digest": art.get("digest") or "",
        "author": art.get("author") or "",
        "markdown": art.get("markdown") or "",
        "log": art.get("log") or "",
        "checks": art.get("checks") or {},
        "images_meta": art.get("images_meta") or [],
        "error": art.get("error") or "",
        "tt_url": art.get("tt_url") or "",
        "tt_at": art.get("tt_at") or "",
        "cover_url": f"{URL_PREFIX}/media/articles/a{article_id}/cover.png?v={art['updated_at']}",
        "updated_at": art.get("updated_at") or "",
    }


@app.get("/api/article/{article_id}/state")
async def api_article_state(article_id: int, request: Request):
    """局部刷新用：拿到最新状态，避免整页 reload。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    try:
        return ok(state=_art_state(article_id))
    except RuntimeError as exc:
        return fail(str(exc), 404)


@app.post("/api/article/{article_id}/images/remove")
async def api_image_remove(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    try:
        pipeline.remove_image(article_id, body.get("url") or "")
    except Exception as exc:  # noqa: BLE001
        return fail(f"移除失败：{exc}", 500)
    return ok(state=_art_state(article_id))


@app.post("/api/article/{article_id}/images/caption")
async def api_image_caption(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    try:
        pipeline.set_caption(article_id, body.get("url") or "", body.get("caption") or "")
    except Exception as exc:  # noqa: BLE001
        return fail(f"改图注失败：{exc}", 500)
    return ok(state=_art_state(article_id))


@app.post("/api/article/{article_id}/images/reorder")
async def api_image_reorder(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    urls = body.get("urls") or []
    if not isinstance(urls, list):
        return fail("urls 必须是数组")
    try:
        pipeline.reorder_images(article_id, [str(u) for u in urls])
    except Exception as exc:  # noqa: BLE001
        return fail(f"调序失败：{exc}", 500)
    return ok(state=_art_state(article_id))


@app.post("/api/article/{article_id}/images/swap")
async def api_image_swap(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    try:
        await asyncio.to_thread(pipeline.swap_image, article_id, body.get("url") or "")
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 500)
    return ok(state=_art_state(article_id))


@app.post("/api/article/{article_id}/images/regen_ai")
async def api_image_regen_ai(article_id: int, request: Request):
    """把某张配图用 AI 重新生成并就地替换（可带补充要求）。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url:
        return fail("缺少图片地址")
    try:
        await asyncio.to_thread(pipeline.regen_ai_image, article_id, url,
                                (body.get("extra") or "").strip(),
                                (body.get("scene") or "").strip())
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 500)
    return ok(state=_art_state(article_id))


@app.post("/api/article/{article_id}/push")
async def api_article_push(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    try:
        result = await asyncio.to_thread(pipeline.push_draft, article_id)
    except pipeline.WrongPlatform as exc:      # 平台不对：用法问题，400 就够
        log.info("建草稿被拒: %s", exc)
        pipeline.log_append(article_id, f"建草稿被拒：{exc}")
        return JSONResponse({"ok": False, "error": str(exc), "state": _art_state(article_id)},
                            status_code=400)
    except Exception as exc:  # noqa: BLE001
        log.exception("建草稿失败")
        pipeline.log_append(article_id, f"建草稿失败：{exc}")
        return JSONResponse({"ok": False, "error": str(exc), "state": _art_state(article_id)},
                            status_code=500)
    return ok(**result, state=_art_state(article_id))


# ---------------- 头条号草稿（浏览器驱动，只存草稿） ----------------

@app.get("/api/toutiao/status")
async def api_toutiao_status(request: Request, account_id: int | None = None):
    """前端轮询：装了 playwright 没、扫到哪一步、登录态什么时候过期。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    return ok(toutiao=toutiao.login_status(account_id or 0))


@app.post("/api/toutiao/login")
async def api_toutiao_login(request: Request):
    """起后台浏览器等扫码，二维码写到 uploads 下，前端拿 /media/uploads 显示。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json() if await request.body() else {}
    aid = body.get("account_id") or 0
    try:
        return ok(toutiao=await asyncio.to_thread(toutiao.start_login, aid))
    except toutiao.ToutiaoError as exc:
        return fail(str(exc), 400)


@app.post("/api/toutiao/login/cancel")
async def api_toutiao_login_cancel(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    return ok(toutiao=toutiao.cancel_login())


@app.post("/api/toutiao/logout")
async def api_toutiao_logout(request: Request):
    """只删本机登录态，头条那边不会掉线。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json() if await request.body() else {}
    aid = body.get("account_id") or 0
    toutiao.logout(aid)
    return ok(toutiao=toutiao.login_status(aid))


@app.post("/api/article/{article_id}/toutiao_draft")
async def api_article_toutiao_draft(article_id: int, request: Request):
    """把这篇稿子存进头条草稿箱。只存草稿，不会发布。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    try:
        result = await asyncio.to_thread(pipeline.push_toutiao, article_id)
    except toutiao.LoginRequired as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc), "need_login": True,
             "state": _art_state(article_id), "toutiao": toutiao.login_status()},
            status_code=409)
    except toutiao.ManualActionRequired as exc:
        # 登录了但卡在绑手机/认证上：这不是报错，是「要人去处理」
        return JSONResponse(
            {"ok": False, "error": str(exc), "need_action": True, "action": exc.kind,
             "state": _art_state(article_id)},
            status_code=409)
    except pipeline.WrongPlatform as exc:
        return JSONResponse(
            {"ok": False, "error": str(exc), "state": _art_state(article_id)},
            status_code=400)
    except Exception as exc:  # noqa: BLE001
        log.exception("头条草稿失败")
        return JSONResponse(
            {"ok": False, "error": str(exc), "state": _art_state(article_id)},
            status_code=500)
    return ok(result=result, state=_art_state(article_id))


@app.post("/api/article/{article_id}/publish")
async def api_article_publish(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    try:
        result = await asyncio.to_thread(
            pipeline.publish, article_id, confirm=bool(body.get("confirm")))
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 500)
    return ok(**result)


@app.get("/api/article/{article_id}/publish_status")
async def api_article_publish_status(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    try:
        return ok(result=await asyncio.to_thread(pipeline.publish_status, article_id))
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 500)


@app.post("/api/article/{article_id}/delete")
async def api_article_delete(article_id: int, request: Request):
    """删除工作台里的这篇稿（不影响公众号草稿箱；草稿箱请用「删掉草稿箱旧稿」）。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    art = pipeline.get_article(article_id)
    db.execute("DELETE FROM articles WHERE id=?", (article_id,))
    db.execute("UPDATE topics SET article_id=NULL WHERE article_id=?", (article_id,))
    return ok(deleted=article_id, had_draft=bool(art.get("media_id")))


@app.post("/api/article/{article_id}/delete_draft")
async def api_article_delete_draft(article_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    try:
        return ok(**pipeline.delete_draft(article_id))
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 500)


# --------------------------------------------------------------------------
# 图片
# --------------------------------------------------------------------------

@app.post("/api/upload")
async def api_upload(request: Request, file: UploadFile = File(...)):
    user, redirect = guard(request)
    if redirect:
        return redirect
    try:
        meta = images.save_upload(file.filename or "upload.png", await file.read())
    except images.ImageError as exc:
        return fail(str(exc))
    hint = images.suggest_reject(meta)
    return ok(**meta, reject_hint=hint)


@app.post("/api/upload_url")
async def api_upload_url(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    try:
        meta = await asyncio.to_thread(
            images.download_image, (body.get("url") or "").strip(),
            body.get("referer") or "")
    except images.ImageError as exc:
        return fail(str(exc))
    return ok(**meta, reject_hint=images.suggest_reject(meta))


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------

@app.get("/accounts", response_class=HTMLResponse)
async def accounts_page(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    accs = pipeline.list_accounts()
    # 头条号的登录态在浏览器里，跟账号绑定，所以列表上直接带出来
    tt = {a["id"]: toutiao.login_status(a["id"]) for a in accs
          if (a.get("platform") or "wechat") == "toutiao"}
    grouped = {k: [a for a in accs if (a.get("platform") or "wechat") == k] for k in PLATFORMS}
    # 老版本只有一个头条登录态文件；还没建头条号账号时提示一下，建完会自动认领过去
    legacy_tt = bool(toutiao.STATE_PATH.exists()) and not grouped.get("toutiao")
    return render_page(request, "accounts.html", user=user, active="accounts",
                       accounts=accs, grouped=grouped, tt_status=tt,
                       legacy_tt=legacy_tt, platforms=PLATFORMS,
                       persona_presets=personas.PERSONA_PRESETS,
                       themes=render.THEMES,
                       characters=characters.list_characters())


@app.get("/characters", response_class=HTMLResponse)
async def characters_page(request: Request):
    """虚拟人物库：参考图 + 外形描述，做「同一个人多场景」的号靠它。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    chars = characters.list_characters()
    accs = pipeline.list_accounts()
    used: dict[int, list[str]] = {}
    for a in accs:
        cid = int(a.get("character_id") or 0)
        if cid:
            used.setdefault(cid, []).append(a.get("name") or "")
    ok_ref, ref_hint = aigen.reference_ready()
    return render_page(request, "characters.html", user=user, active="characters",
                       chars=chars, used=used, ref_ok=ok_ref, ref_hint=ref_hint,
                       img_cfg=aigen.image_config(), accounts=accs)


@app.post("/api/characters")
async def api_characters_save(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    cid = characters.save_character(body)
    return ok(id=cid, character=characters.get_character(cid))


@app.post("/api/characters/{cid}/delete")
async def api_characters_delete(cid: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    characters.delete_character(cid)
    return ok()


@app.post("/api/characters/{cid}/ref")
async def api_characters_upload_ref(cid: int, request: Request,
                                    file: UploadFile = File(...)):
    """上传一张人物参考图。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    c = characters.get_character(cid)
    if not c:
        return fail("人物不存在")
    if len(c["ref_images"]) >= characters.REF_MAX:
        return fail(f"最多 {characters.REF_MAX} 张参考图，先删一张再传")
    raw = await file.read()
    if not raw:
        return fail("文件是空的")
    try:
        url = await asyncio.to_thread(characters.save_ref_image, raw, cid)
    except Exception as exc:  # noqa: BLE001
        return fail(f"参考图处理失败：{exc}", 500)
    return ok(url=url, character=characters.get_character(cid))


@app.post("/api/characters/{cid}/ref/remove")
async def api_characters_remove_ref(cid: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    refs = characters.remove_ref(cid, int(body.get("index") or 0))
    return ok(refs=refs)


@app.post("/api/characters/{cid}/test")
async def api_characters_test(cid: int, request: Request):
    """试生成一个场景，看看人像有没有被保住。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    scene = (body.get("scene") or "在办公室里对着电脑工作").strip()
    c = characters.get_character(cid)
    if not c:
        return fail("人物不存在")
    try:
        meta = await asyncio.to_thread(aigen.generate_scene, c, scene, kind="inline",
                                       return_meta=True)
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 500)
    warned = ""
    if c["ref_images"] and not aigen.reference_ready()[0]:
        warned = aigen.reference_ready()[1]
    elif not c["ref_images"]:
        warned = "这个人物还没传参考图，现在是按「外形描述」生成的，长相不会真的固定。"
    return ok(url=meta.get("url"), scene=scene, warning=warned,
              shot=meta.get("shot") or "", action=meta.get("action") or "",
              expression=meta.get("expression") or "",
              used_reference=bool(meta.get("used_reference")))


@app.post("/api/accounts")
async def api_accounts_create(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    name = (body.get("name") or "").strip()
    platform = (body.get("platform") or "wechat").strip()
    if platform not in PLATFORMS:
        return fail(f"不认识的平台：{platform}")
    appid = (body.get("appid") or "").strip()
    secret = (body.get("secret") or "").strip()
    if not name:
        return fail("名称要填")
    if platform == "wechat" and (not appid or not secret):
        return fail("公众号要填 AppID 和 AppSecret")
    if platform == "toutiao":
        # 头条号没有 AppID/Secret，登录态是扫码来的，这里只占个名字
        appid = appid or f"toutiao:{name}"
    if db.query_one("SELECT id FROM accounts WHERE name=?", (name,)):
        return fail("这个名字已经有了")
    acc_id = db.insert("accounts", {
        "name": name,
        "platform": platform,
        "appid": appid,
        "secret_enc": security.encrypt_secret(secret) if secret else "",
        "author": (body.get("author") or "").strip(),
        "alias": (body.get("alias") or "").strip(),
        "persona": (body.get("persona") or "").strip(),
        "style_prompt": (body.get("style_prompt") or "").strip(),
        "theme": (body.get("theme") or "").strip(),
        "topic_domain": (body.get("topic_domain") or "").strip(),
        "image_count": int(body.get("image_count") or 0),
        "image_source": (body.get("image_source") if (body.get("image_source") or "web") in pipeline.IMAGE_SOURCES
                         else "web"),
        "cover_from_image": 1 if body.get("cover_from_image", True) else 0,
        "character_id": int(body.get("character_id") or 0),
        "content_mode": ("photo" if (body.get("content_mode") or "article") == "photo"
                         else "article"),
        "photo_count": max(1, min(int(body.get("photo_count") or 3), 6)),
        "is_default": 1 if body.get("is_default") else 0,
        "allow_publish": 1 if body.get("allow_publish") else 0,
        "note": (body.get("note") or "").strip(),
        "created_at": db.now(),
    })
    if body.get("is_default"):
        db.execute("UPDATE accounts SET is_default=0 WHERE id<>?", (acc_id,))
    return ok(id=acc_id)


@app.post("/api/accounts/{account_id}/update")
async def api_accounts_update(account_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    fields: dict[str, Any] = {}
    for k in ("name", "appid", "author", "alias", "note",
              "persona", "style_prompt", "theme", "topic_domain"):
        if k in body:
            fields[k] = (body.get(k) or "").strip()
    if "image_count" in body:
        try:
            fields["image_count"] = max(0, min(int(body["image_count"] or 0), 8))
        except (TypeError, ValueError):
            fields["image_count"] = 0
    if "image_source" in body:
        src = (body.get("image_source") or "web").strip()
        fields["image_source"] = src if src in pipeline.IMAGE_SOURCES else "web"
    if "cover_from_image" in body:
        fields["cover_from_image"] = 1 if body["cover_from_image"] else 0
    if "character_id" in body:
        try:
            fields["character_id"] = max(0, int(body["character_id"] or 0))
        except (TypeError, ValueError):
            fields["character_id"] = 0
    if "content_mode" in body:
        mode = (body.get("content_mode") or "article").strip()
        fields["content_mode"] = mode if mode in ("article", "photo") else "article"
    if "photo_count" in body:
        try:
            fields["photo_count"] = max(1, min(int(body["photo_count"] or 3), 6))
        except (TypeError, ValueError):
            fields["photo_count"] = 3
    if body.get("secret"):
        fields["secret_enc"] = security.encrypt_secret(body["secret"].strip())
    if "is_default" in body:
        fields["is_default"] = 1 if body["is_default"] else 0
    if "allow_publish" in body:
        fields["allow_publish"] = 1 if body["allow_publish"] else 0
    if fields:
        db.update("accounts", account_id, fields)
    if body.get("is_default"):
        db.execute("UPDATE accounts SET is_default=0 WHERE id<>?", (account_id,))
    return ok()


@app.post("/api/accounts/{account_id}/delete")
async def api_accounts_delete(account_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    if db.query_one("SELECT id FROM articles WHERE account_id=? LIMIT 1", (account_id,)):
        return fail("这个账号下已经有文章，不能删（可以先改配置）")
    db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    return ok()


@app.post("/api/accounts/{account_id}/test")
async def api_accounts_test(account_id: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    try:
        acc = pipeline.get_account(account_id, platform="wechat")
        from .wechat import client_for

        result = await asyncio.to_thread(lambda: client_for(acc).probe())
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 500)
    return ok(result=result)


# --------------------------------------------------------------------------
# 草稿箱
# --------------------------------------------------------------------------

@app.get("/drafts", response_class=HTMLResponse)
async def drafts_page(request: Request, account_id: int | None = None):
    user, redirect = guard(request)
    if redirect:
        return redirect
    # 页面只出壳：草稿箱数据由前端异步拉，别再让打开页面等微信接口
    accs = pipeline.list_accounts(platform="wechat")
    return render_page(request, "drafts.html", user=user, active="drafts",
                       accounts=accs,
                       current_account=account_id or (accs[0]["id"] if accs else None))


@app.get("/api/drafts")
async def api_drafts(request: Request, account_id: int | None = None, refresh: int = 0):
    user, redirect = guard(request)
    if redirect:
        return redirect
    try:
        accs = pipeline.list_accounts(platform="wechat")
        if not accs:
            return ok(total=0, drafts=[], cached=False, age=0)
        data = await asyncio.to_thread(
            pipeline.list_wechat_drafts_cached,
            account_id or accs[0]["id"], 20, bool(refresh))
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 500)
    return ok(**data)


# --------------------------------------------------------------------------
# 设置
# --------------------------------------------------------------------------

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    return render_page(
        request, "settings.html", user=user, active="settings",
        llm=writer.llm_config(),
        image_llm=aigen.image_config(),
        public_base=public_base_url() or str(db.get_setting("public_base", "") or ""),
        ref_ready=aigen.reference_ready(),
        photo_style=aigen.photo_style(),
        photo_styles=photo.style_labels(),
        style_prompt=db.get_setting("style_prompt") or DEFAULT_STYLE_PROMPT,
        theme=db.get_setting("theme") or "default",
        font_size=db.get_setting("font_size") or "medium",
        cover_max_chars=db.get_setting("cover_max_chars") or 10,
        images_per_article=db.get_setting("images_per_article") or 4,
        max_data_images=db.get_setting("max_data_images", 1),
        image_rank_by_model=db.get_setting("image_rank_by_model", True),
        themes=render.THEMES,
        font=cover_mod.font_status(),
        schedules=scheduler.db_schedules(),
        accounts=pipeline.list_accounts(),
    )


@app.post("/api/settings")
async def api_settings_save(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    if "llm" in body:
        cur = db.get_setting("llm", {}) or {}
        incoming = body["llm"] or {}
        merged = {**DEFAULT_LLM, **cur}
        for k in ("base_url", "model", "temperature"):
            if k in incoming and incoming[k]:
                merged[k] = incoming[k]
        # Key 只在填了新的情况下覆盖，且一律加密存库
        if incoming.get("api_key"):
            merged["api_key"] = security.encrypt_secret(incoming["api_key"].strip())
        if merged.get("temperature"):
            try:
                merged["temperature"] = float(merged["temperature"])
            except (TypeError, ValueError):
                merged["temperature"] = DEFAULT_LLM["temperature"]
        db.set_setting("llm", merged)
    if "image_llm" in body:
        cur = db.get_setting("image_llm", {}) or {}
        incoming = body["image_llm"] or {}
        merged = {**aigen.DEFAULT_IMAGE_CFG, **cur}
        for k in ("provider", "base_url", "model", "ref_model"):
            if k in incoming and incoming[k]:
                merged[k] = incoming[k]
        if incoming.get("api_key"):
            merged["api_key"] = security.encrypt_secret(incoming["api_key"].strip())
        db.set_setting("image_llm", merged)
    if "public_base" in body:
        # 人物参考图要给生图服务下载，必须是对外可访问的绝对地址
        db.set_setting("public_base", (body.get("public_base") or "").strip().rstrip("/"))
    for key in ("style_prompt", "theme", "font_size", "cover_max_chars",
                "images_per_article", "image_rank_by_model", "max_data_images"):
        if key in body:
            db.set_setting(key, body[key])
    if "photo_style" in body:
        # 只接受 photo.STYLES 里有的 id，别把脏值写进库（提示词会直接用它）
        sid = str(body.get("photo_style") or "").strip()
        db.set_setting("photo_style", sid if sid in photo.STYLES else "auto")
    return ok()


@app.post("/api/settings/test_llm")
async def api_test_llm(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    return ok(result=await asyncio.to_thread(writer.test_llm))


@app.post("/api/settings/test_image")
async def api_test_image(request: Request):
    """测试生图：真生成一张，别只 ping 一下。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    return ok(result=await asyncio.to_thread(aigen.test))


@app.post("/api/article/{article_id}/gen_image")
async def api_article_gen_image(article_id: int, request: Request):
    """给这篇稿子 AI 生一张配图或封面。返回 /media/uploads 地址，前端决定插哪。"""
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    art = pipeline.get_article(article_id)
    acc = pipeline.get_account(art["account_id"])
    kind = (body.get("kind") or "inline").strip()
    topic = (body.get("topic") or art.get("title") or "").strip()
    extra = (body.get("extra") or "").strip()
    if not topic:
        return fail("先给个主题（默认用稿件标题）")
    # 账号绑了虚拟人物就用人物参考出图（同一个人、换场景），否则走普通生图
    ch = characters.for_account(acc)
    try:
        if ch:
            url = await asyncio.to_thread(aigen.generate_scene, ch, topic,
                                          account=acc, kind=kind, style=extra)
        else:
            url = await asyncio.to_thread(aigen.generate, topic, acc, kind=kind, extra=extra)
    except Exception as exc:  # noqa: BLE001
        return fail(str(exc), 500)
    pipeline.log_append(
        article_id,
        f"AI 生图（{'封面' if kind == 'cover' else '配图'}"
        + (f"，人物：{ch['name']}" if ch else "") + f"）：{url}")
    return ok(url=url, topic=topic, character=(ch or {}).get("name") or "")


@app.post("/api/password")
async def api_password(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    old = body.get("old") or ""
    new = body.get("new") or ""
    if len(new) < 6:
        return fail("新密码至少 6 位")
    row = db.query_one("SELECT * FROM users WHERE id=?", (user["id"],))
    if not security.verify_password(old, row["password_hash"]):
        return fail("原密码不对")
    db.update("users", user["id"], {"password_hash": security.hash_password(new)})
    return ok()


# --------------------------------------------------------------------------
# 定时任务配置
# --------------------------------------------------------------------------

@app.post("/api/schedules")
async def api_schedules_create(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    body = await request.json()
    try:
        hour = int(body.get("hour") or 8)
        minute = int(body.get("minute") or 0)
    except (TypeError, ValueError):
        return fail("时间格式不对")
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return fail("时间超出范围")
    sid = db.insert("schedules", {
        "name": (body.get("name") or f"{hour:02d}:{minute:02d} 自动出稿").strip(),
        "account_id": int(body.get("account_id") or 0),
        "slot": body.get("slot") or "morning",
        "mode": "weibo",
        "hour": hour,
        "minute": minute,
        "push_draft": 1 if body.get("push_draft", True) else 0,
        "enabled": 1,
        "created_at": db.now(),
    })
    return ok(id=sid)


@app.post("/api/schedules/{sid}/toggle")
async def api_schedules_toggle(sid: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    row = db.query_one("SELECT * FROM schedules WHERE id=?", (sid,))
    if not row:
        return fail("任务不存在", 404)
    db.update("schedules", sid, {"enabled": 0 if row["enabled"] else 1})
    return ok()


@app.post("/api/schedules/{sid}/delete")
async def api_schedules_delete(sid: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    db.execute("DELETE FROM schedules WHERE id=?", (sid,))
    return ok()


@app.post("/api/schedules/{sid}/run")
async def api_schedules_run(sid: int, request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    row = db.query_one("SELECT * FROM schedules WHERE id=?", (sid,))
    if not row:
        return fail("任务不存在", 404)
    result = await asyncio.to_thread(scheduler.run_schedule, dict(row))
    return ok(result=result)


@app.get("/api/jobs")
async def api_jobs(request: Request):
    user, redirect = guard(request)
    if redirect:
        return redirect
    rows = db.query("SELECT * FROM jobs ORDER BY id DESC LIMIT 30")
    return ok(jobs=[dict(r) for r in rows])


# --------------------------------------------------------------------------
# 健康检查
# --------------------------------------------------------------------------

@app.get("/healthz")
async def healthz():
    return {"ok": True, "app": APP_NAME}
