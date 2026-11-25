"""虚拟人物（人物参考）。

做「虚拟人物号」的核心是**同一个人在不同场景里长相一致**：今天是他在工位加班，
明天是他在路边吃饭，脸得是同一个人。纯靠文字描述碰运气，所以这里单独存一份人物资料：

    · ref_images   1~3 张参考照（生图时作为「参考图」传给支持参考的模型）
    · appearance   外形描述（给纯文本模型兜底，也作为提示词的一部分）
    · scene_hint   整体画面偏好，比如「日系胶片、暖色调、浅景深」

人物和账号是多对一：一个号（账号）绑定一个人物，之后这个号所有 AI 配图都带它。
"""
from __future__ import annotations

import io
import json
import secrets
import time
from typing import Any

from PIL import Image

from . import db
from .settings import UPLOAD_DIR

REF_MAX = 3            # 最多留几张参考图
REF_SIDE = 1280        # 参考图最长边，够模型看清长相，又不至于太大


def _refs(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    try:
        v = json.loads(raw or "[]")
        return [str(x) for x in v if x] if isinstance(v, list) else []
    except (ValueError, TypeError):
        return []


def _row(r: Any) -> dict[str, Any]:
    d = dict(r)
    d["ref_images"] = _refs(d.get("ref_images"))
    return d


def list_characters() -> list[dict[str, Any]]:
    conn = db.get_conn()
    return [_row(r) for r in conn.execute(
        "SELECT * FROM characters ORDER BY id DESC")]


def get_character(cid: int | str) -> dict[str, Any] | None:
    try:
        cid = int(cid or 0)
    except (TypeError, ValueError):
        return None
    if cid <= 0:
        return None
    conn = db.get_conn()
    r = conn.execute("SELECT * FROM characters WHERE id=?", (cid,)).fetchone()
    return _row(r) if r else None


def for_account(account: dict[str, Any] | None) -> dict[str, Any] | None:
    """账号绑定了人物就返回它，否则 None（退化成普通生图）。"""
    if not account:
        return None
    return get_character(account.get("character_id") or 0)


def save_character(data: dict[str, Any]) -> int:
    """新建或更新。有 id 就更新，没有就插入。返回 id。"""
    conn = db.get_conn()
    name = (data.get("name") or "").strip() or "未命名人物"
    appearance = (data.get("appearance") or "").strip()
    scene_hint = (data.get("scene_hint") or "").strip()
    note = (data.get("note") or "").strip()
    now = db.now()
    cid = int(data.get("id") or 0)
    if cid > 0 and get_character(cid):
        conn.execute(
            "UPDATE characters SET name=?, appearance=?, scene_hint=?, note=?, updated_at=? "
            "WHERE id=?", (name, appearance, scene_hint, note, now, cid))
        return cid
    cur = conn.execute(
        "INSERT INTO characters (name, appearance, ref_images, scene_hint, note, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (name, appearance, "[]", scene_hint, note, now, now))
    return int(cur.lastrowid or 0)


def delete_character(cid: int | str) -> None:
    conn = db.get_conn()
    conn.execute("DELETE FROM characters WHERE id=?", (int(cid or 0),))
    conn.execute("UPDATE accounts SET character_id=0 WHERE character_id=?", (int(cid or 0),))


def _set_refs(cid: int, refs: list[str]) -> None:
    conn = db.get_conn()
    conn.execute("UPDATE characters SET ref_images=?, updated_at=? WHERE id=?",
                 (json.dumps(refs[:REF_MAX], ensure_ascii=False), db.now(), cid))


def save_ref_image(raw: bytes, cid: int) -> str:
    """存一张参考图（缩到最长边 1280），返回 /media/uploads/xxx.jpg 并挂到人物上。"""
    im = Image.open(io.BytesIO(raw)).convert("RGB")
    w, h = im.size
    if max(w, h) > REF_SIDE:
        scale = REF_SIDE / max(w, h)
        im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    name = f"char_{cid}_{int(time.time())}_{secrets.token_hex(3)}.jpg"
    im.save(UPLOAD_DIR / name, "JPEG", quality=92)
    url = f"/media/uploads/{name}"
    c = get_character(cid)
    refs = list((c or {}).get("ref_images") or [])
    refs.append(url)
    _set_refs(cid, refs)
    return url


def remove_ref(cid: int, index: int) -> list[str]:
    c = get_character(cid)
    refs = list((c or {}).get("ref_images") or [])
    if 0 <= index < len(refs):
        refs.pop(index)
        _set_refs(cid, refs)
    return refs


def fallback_hint(character: dict[str, Any] | None) -> str:
    """当前生图后端没有参考图能力时，用外形描述硬凑的提示词片段。"""
    if not character:
        return ""
    bits = []
    if character.get("appearance"):
        bits.append(f"人物固定设定（每张图都要一致）：{character['appearance']}")
    if character.get("scene_hint"):
        bits.append(f"画面偏好：{character['scene_hint']}")
    return "；".join(bits)
