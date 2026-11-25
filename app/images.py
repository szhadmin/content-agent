"""图片入库：下载 / 上传 / 校验。

只放横向、像素够、格式正常的图；竖图和关注引导图直接拒。
"""
from __future__ import annotations

import hashlib
import io
import re
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from .settings import UPLOAD_DIR

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")

MIN_W, MIN_H = 400, 200
MAX_BYTES = 8 * 1024 * 1024

# 明显是"关注我 / 二维码 / 引导"的图，正文里不要
BAD_HINT = re.compile(r"(qrcode|erweima|gzh|follow|关注|二维码|引导)", re.I)


class ImageError(RuntimeError):
    pass


def inspect_image(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as im:
            im.verify()
        with Image.open(path) as im:
            w, h = im.size
            fmt = im.format or ""
    except Exception as exc:  # noqa: BLE001
        raise ImageError(f"图片无法解析：{exc}") from exc
    ratio = w / h if h else 0
    return {
        "width": w,
        "height": h,
        "format": fmt,
        "ratio": round(ratio, 3),
        "horizontal": ratio >= 1.05,
        "big_enough": w >= MIN_W and h >= MIN_H,
        "bytes": path.stat().st_size,
    }


def _save_bytes(data: bytes, suffix: str, prefix: str = "img") -> Path:
    if len(data) < 1024:
        raise ImageError("图片内容过小，可能下载失败")
    if len(data) > MAX_BYTES:
        raise ImageError(f"图片过大（{len(data)//1024}KB），请先压缩")
    digest = hashlib.sha1(data).hexdigest()[:10]
    name = f"{prefix}_{int(time.time())}_{digest}{suffix}"
    path = UPLOAD_DIR / name
    path.write_bytes(data)
    inspect_image(path)  # 校验，不合法就抛
    return path


def save_upload(filename: str, data: bytes) -> dict[str, Any]:
    suffix = Path(filename or "").suffix.lower() or ".png"
    if suffix not in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
        raise ImageError(f"不支持的图片格式：{suffix}")
    path = _save_bytes(data, suffix, prefix="up")
    meta = inspect_image(path)
    meta["path"] = str(path)
    meta["name"] = path.name
    meta["url"] = f"/media/uploads/{path.name}"
    return meta


def download_image(url: str, referer: str = "") -> dict[str, Any]:
    if not url.startswith(("http://", "https://")):
        raise ImageError("只支持 http(s) 图片地址")
    headers = {"User-Agent": UA, "Accept": "image/*,*/*;q=0.8"}
    if referer:
        headers["Referer"] = referer
    try:
        r = requests.get(url, headers=headers, timeout=45, stream=True)
        r.raise_for_status()
    except requests.RequestException as exc:
        raise ImageError(f"下载失败：{exc}") from exc
    ctype = (r.headers.get("Content-Type") or "").lower()
    suffix = ".jpg" if "jpeg" in ctype or "jpg" in ctype else \
             ".png" if "png" in ctype else \
             ".webp" if "webp" in ctype else \
             ".gif" if "gif" in ctype else ".jpg"
    data = b""
    for chunk in r.iter_content(65536):
        data += chunk
        if len(data) > MAX_BYTES:
            raise ImageError("图片过大，已中止下载")
    path = _save_bytes(data, suffix, prefix="dl")
    meta = inspect_image(path)
    meta["path"] = str(path)
    meta["name"] = path.name
    meta["url"] = f"/media/uploads/{path.name}"
    meta["source_url"] = url
    return meta


def suggest_reject(meta: dict[str, Any], alt: str = "") -> str:
    """返回拒绝理由，空字符串代表可用。"""
    if not meta.get("big_enough"):
        return f"尺寸偏小（{meta.get('width')}×{meta.get('height')}）"
    if not meta.get("horizontal"):
        return f"竖图（比例 {meta.get('ratio')}），公众号正文里会很难看"
    if alt and BAD_HINT.search(alt):
        return "疑似关注引导图"
    return ""


def read_bytes(path: Path) -> bytes:
    p = Path(path)
    if not p.exists():
        raise ImageError(f"文件不存在：{p}")
    return p.read_bytes()


def as_data_uri(path: Path) -> str:
    import base64

    p = Path(path)
    suffix = p.suffix.lower().lstrip(".") or "png"
    if suffix == "jpg":
        suffix = "jpeg"
    return f"data:image/{suffix};base64," + base64.b64encode(p.read_bytes()).decode("ascii")
