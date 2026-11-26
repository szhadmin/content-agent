"""SQLite 数据层：连接、建表、便捷查询。"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from typing import Any, Iterable

from .settings import DB_PATH

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'member',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    username   TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    platform      TEXT NOT NULL DEFAULT 'wechat',
    appid         TEXT NOT NULL,
    secret_enc    TEXT NOT NULL,
    author        TEXT DEFAULT '',
    alias         TEXT DEFAULT '',
    is_default    INTEGER NOT NULL DEFAULT 0,
    allow_publish INTEGER NOT NULL DEFAULT 0,
    note          TEXT DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS topics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    day        TEXT NOT NULL,
    slot       TEXT NOT NULL,
    title      TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'weibo',
    weibo_rank INTEGER,
    weibo_heat INTEGER,
    keywords   TEXT NOT NULL DEFAULT '[]',
    note       TEXT NOT NULL DEFAULT '',
    article_id INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_topics_day ON topics(day, slot);

CREATE TABLE IF NOT EXISTS articles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     INTEGER NOT NULL,
    platform       TEXT NOT NULL DEFAULT 'wechat',
    topic_id       INTEGER,
    title          TEXT NOT NULL DEFAULT '',
    digest         TEXT NOT NULL DEFAULT '',
    author         TEXT NOT NULL DEFAULT '',
    slot           TEXT NOT NULL DEFAULT 'extra',
    markdown       TEXT NOT NULL DEFAULT '',
    html           TEXT NOT NULL DEFAULT '',
    cover_path     TEXT NOT NULL DEFAULT '',
    thumb_media_id TEXT NOT NULL DEFAULT '',
    media_id       TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'draft',
    checks         TEXT NOT NULL DEFAULT '{}',
    images         TEXT NOT NULL DEFAULT '[]',
    log            TEXT NOT NULL DEFAULT '',
    error          TEXT NOT NULL DEFAULT '',
    created_by     TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    pushed_at      TEXT,
    published_at   TEXT,
    publish_id     TEXT NOT NULL DEFAULT '',
    tt_url         TEXT NOT NULL DEFAULT '',
    tt_at          TEXT NOT NULL DEFAULT '',
    tt_state       TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_articles_status ON articles(status, created_at);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    article_id  INTEGER,
    status      TEXT NOT NULL,
    log         TEXT NOT NULL DEFAULT '',
    started_at  TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS schedules (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    account_id INTEGER NOT NULL,
    slot       TEXT NOT NULL DEFAULT 'morning',
    mode       TEXT NOT NULL DEFAULT 'weibo',
    hour       INTEGER NOT NULL DEFAULT 8,
    minute     INTEGER NOT NULL DEFAULT 0,
    push_draft INTEGER NOT NULL DEFAULT 1,
    enabled    INTEGER NOT NULL DEFAULT 1,
    last_run   TEXT,
    created_at TEXT NOT NULL
);

-- 虚拟人物（人物参考）。做「虚拟人物号」时，同一个人的工作/下班/吃饭各场景
-- 都要保持长相一致，所以把参考图和外形描述单独存起来，生图时带上。
CREATE TABLE IF NOT EXISTS characters (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    appearance  TEXT NOT NULL DEFAULT '',   -- 外形描述，纯文本模型靠它兜底
    ref_images  TEXT NOT NULL DEFAULT '[]', -- JSON 数组：/media/uploads/xxx.jpg
    scene_hint  TEXT NOT NULL DEFAULT '',   -- 整体画面偏好（摄影风格/色调）
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    # 轻量迁移：补齐后续新增列
    _ensure_columns(conn)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    wanted = {
        "articles": {
            "error": "TEXT NOT NULL DEFAULT ''",
            "publish_id": "TEXT NOT NULL DEFAULT ''",
            "slot": "TEXT NOT NULL DEFAULT 'extra'",
            "images_meta": "TEXT NOT NULL DEFAULT '[]'",
            "stage": "TEXT NOT NULL DEFAULT ''",
            # 头条草稿：投递结果单独存，别和公众号的 media_id 混
            "tt_url": "TEXT NOT NULL DEFAULT ''",
            "tt_at": "TEXT NOT NULL DEFAULT ''",
            "tt_state": "TEXT NOT NULL DEFAULT '{}'",
            # 稿件跟账号走，平台冗余一份，列表筛选不用每次 join
            "platform": "TEXT NOT NULL DEFAULT 'wechat'",
        },
        "schedules": {
            "push_draft": "INTEGER NOT NULL DEFAULT 1",
            "platform": "TEXT NOT NULL DEFAULT 'wechat'",
        },
        # 每个账号自己的人设、写作规范、排版主题、配图数量、选题领域、平台
        "accounts": {
            "persona": "TEXT NOT NULL DEFAULT ''",
            "style_prompt": "TEXT NOT NULL DEFAULT ''",
            "theme": "TEXT NOT NULL DEFAULT ''",
            "image_count": "INTEGER NOT NULL DEFAULT 0",
            "topic_domain": "TEXT NOT NULL DEFAULT ''",
            "cover_from_image": "INTEGER NOT NULL DEFAULT 1",
            "platform": "TEXT NOT NULL DEFAULT 'wechat'",
            # 这个号用哪个虚拟人物出图（0 = 不用）
            "character_id": "INTEGER NOT NULL DEFAULT 0",
            # 内容模式：article=长文；photo=人物日常（几张图+几句口语，做虚拟人物号用）
            "content_mode": "TEXT NOT NULL DEFAULT 'article'",
            # 配图来源：web=联网抓图（默认）；ai=AI 生图；mixed=一半网图一半 AI
            "image_source": "TEXT NOT NULL DEFAULT 'web'",
            # 人物日常模式生成几张图
            "photo_count": "INTEGER NOT NULL DEFAULT 3",
        },
    }
    added: list[tuple[str, str]] = []
    for table, cols in wanted.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                added.append((table, col))

    # 老库补平台：账号默认公众号；稿件的平台跟它所属账号对齐
    if ("accounts", "platform") in added or ("articles", "platform") in added:
        conn.execute(
            "UPDATE articles SET platform=COALESCE("
            "(SELECT a.platform FROM accounts a WHERE a.id=articles.account_id), 'wechat') "
            "WHERE platform='' OR platform IS NULL"
        )


def query(sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return list(get_conn().execute(sql, tuple(params)))


def query_one(sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    rows = query(sql, params)
    return rows[0] if rows else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    cur = get_conn().execute(sql, tuple(params))
    return cur.lastrowid or 0


def insert(table: str, data: dict[str, Any]) -> int:
    cols = ", ".join(data)
    marks = ", ".join("?" for _ in data)
    return execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", data.values())


def update(table: str, row_id: int, data: dict[str, Any]) -> None:
    sets = ", ".join(f"{k}=?" for k in data)
    execute(f"UPDATE {table} SET {sets} WHERE id=?", list(data.values()) + [row_id])


# ---------- settings KV ----------

def get_setting(key: str, default: Any = None) -> Any:
    row = query_one("SELECT value FROM settings WHERE key=?", (key,))
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return row["value"]


def set_setting(key: str, value: Any) -> None:
    payload = json.dumps(value, ensure_ascii=False)
    execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, payload),
    )


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k in ("checks", "images", "images_meta", "keywords", "tt_state"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except (TypeError, ValueError):
                d[k] = {} if k in ("checks", "tt_state") else []
    return d
