"""定时任务：每天固定时刻自动出稿（默认只建草稿，不发布）。

不用 APScheduler，一个 asyncio 循环就够：每 30 秒扫一次 schedules 表，
到点且今天没跑过就执行。进程重启也不会重复跑（看 last_run 日期）。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from . import pipeline, topics, writer

log = logging.getLogger("scheduler")

_running: set[int] = set()


def due_schedules(now: datetime | None = None) -> list[dict[str, Any]]:
    now = now or datetime.now()
    rows = db_schedules()
    out = []
    for s in rows:
        if not s.get("enabled"):
            continue
        if s["id"] in _running:
            continue
        if now.hour < s["hour"] or (now.hour == s["hour"] and now.minute < s["minute"]):
            continue
        if (s.get("last_run") or "")[:10] == now.strftime("%Y-%m-%d"):
            continue
        out.append(s)
    return out


def db_schedules() -> list[dict[str, Any]]:
    from . import db

    return [dict(r) for r in db.query("SELECT * FROM schedules ORDER BY id")]


def run_schedule(schedule: dict[str, Any]) -> dict[str, Any]:
    """执行一条定时任务。返回执行摘要。"""
    from . import db

    sid = schedule["id"]
    if sid in _running:
        return {"skipped": True}
    _running.add(sid)
    started = db.now()
    job_id = db.insert("jobs", {
        "kind": "schedule", "article_id": None, "status": "running",
        "log": f"定时任务「{schedule['name']}」开始", "started_at": started,
    })
    try:
        acc_id = schedule["account_id"]
        day = db.today()
        if topics.slot_has_article(day, schedule["slot"]):
            msg = f"{day} 的 {schedule['slot']} 档已有记录，跳过"
            db.update("jobs", job_id, {"status": "success", "log": msg, "finished_at": db.now()})
            db.update("schedules", sid, {"last_run": db.now()})
            return {"skipped": True, "reason": msg}

        # 1. 选题
        cands = topics.candidates(25)
        if not cands:
            raise RuntimeError("热搜抓取失败，三级降级都没有拿到候选")
        picked = None
        selection = ""
        try:
            data = writer.pick_topic(
                cands,
                author="",
                recent_lines=topics.already_written_titles(14),
                candidates_txt=topics.candidates_text(cands),
            )
            picked = data.get("item")
            selection = data.get("reason") or ""
        except writer.LLMError as exc:
            log.warning("LLM 选题失败，退回热度第一：%s", exc)
        if picked is None:
            usable = [c for c in cands if c["usable"]]
            picked = usable[0] if usable else None
        if picked is None:
            raise RuntimeError("没有可用候选（全部被硬性排除）")

        topic_id = topics.remember(
            day, schedule["slot"], picked["word"], source="weibo",
            rank=picked.get("rank"), heat=picked.get("num"),
            note=selection,
        )
        db.update("jobs", job_id, {"log": f"选题：{picked['word']}（{selection}）"})

        # 2. 建文章 + 生成
        article_id = pipeline.create_article(
            account_id=acc_id, title=picked["word"],
            slot=schedule["slot"], created_by="scheduler", topic_id=topic_id,
        )
        db.execute("UPDATE topics SET article_id=? WHERE id=?", (article_id, topic_id))
        db.update("jobs", job_id, {"article_id": article_id})

        pipeline.run_generation(article_id)
        db.update("jobs", job_id, {"log": f"生成完成 article={article_id}"})

        # 3. 建草稿
        if schedule.get("push_draft"):
            pipeline.push_draft(article_id)
            db.update("jobs", job_id, {"log": f"草稿已创建 article={article_id}"})

        db.update("jobs", job_id, {"status": "success", "finished_at": db.now()})
        db.update("schedules", sid, {"last_run": db.now()})
        return {"ok": True, "article_id": article_id, "topic": picked["word"]}
    except Exception as exc:  # noqa: BLE001
        log.exception("定时任务失败")
        db.update("jobs", job_id, {"status": "failed",
                                   "log": f"{type(exc).__name__}: {exc}",
                                   "finished_at": db.now()})
        db.update("schedules", sid, {"last_run": db.now()})
        return {"ok": False, "error": str(exc)}
    finally:
        _running.discard(sid)


async def loop(interval_seconds: int = 30) -> None:
    log.info("定时器启动，每 %s 秒检查一次", interval_seconds)
    while True:
        try:
            for s in due_schedules():
                log.info("触发定时任务 %s", s["name"])
                await asyncio.to_thread(run_schedule, s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("定时器循环异常")
        await asyncio.sleep(interval_seconds)


def next_run_text(s: dict[str, Any]) -> str:
    last = (s.get("last_run") or "")[:10]
    today = datetime.now().strftime("%Y-%m-%d")
    when = "今天" if last != today else "明天"
    return f"{when} {int(s['hour']):02d}:{int(s['minute']):02d}"
