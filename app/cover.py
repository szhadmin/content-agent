"""封面生成：900×383，底部渐变 + 亮黄标题（10 字内自动两行）。

公众号封面必须 900×383（2.35:1）。这里做两件事：
  1) 有真实图 → 居中裁切成 900×383，底部压半透明深色渐变条，标题压上去
  2) 没图 → 用主题色做渐变底 + 标题（不依赖任何外部素材）
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COVER_W, COVER_H = 900, 383

FONT_CANDIDATES = [
    os.environ.get("WXWB_FONT", ""),
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Bold.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

THEME_COLORS = {
    "default": ((28, 28, 30), (72, 72, 78)),
    "elegant": ((56, 44, 32), (122, 98, 66)),
    "tech": ((10, 34, 62), (24, 84, 148)),
    "warm": ((64, 32, 20), (198, 96, 62)),
}

TITLE_COLOR = (255, 216, 77)  # #FFD84D


def find_font() -> str | None:
    for p in FONT_CANDIDATES:
        if p and Path(p).exists():
            return p
    return None


def _font(size: int) -> ImageFont.FreeTypeFont:
    path = find_font()
    if not path:
        return ImageFont.load_default()
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def cover_crop(img: Image.Image, w: int = COVER_W, h: int = COVER_H) -> Image.Image:
    img = img.convert("RGB")
    sw, sh = img.size
    scale = max(w / sw, h / sh)
    nw, nh = max(w, int(sw * scale + 0.5)), max(h, int(sh * scale + 0.5))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _gradient(w: int, h: int, top_rgba: tuple[int, int, int, int],
              bottom_rgba: tuple[int, int, int, int]) -> Image.Image:
    """竖直线性渐变，先做 1×h 再拉伸，比逐像素快得多。"""
    strip = Image.new("RGBA", (1, h))
    px = strip.load()
    for y in range(h):
        t = y / max(1, h - 1)
        px[0, y] = tuple(
            int(top_rgba[i] + (bottom_rgba[i] - top_rgba[i]) * t) for i in range(4)
        )
    return strip.resize((w, h), Image.BILINEAR)


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """按实际像素宽度断行，中文逐字、英文按词尽量。"""
    text = (text or "").strip()
    if not text:
        return []
    lines: list[str] = []
    cur = ""
    for ch in text:
        trial = cur + ch
        if font.getlength(trial) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = ch
        if len(lines) >= 2:
            break
    if cur and len(lines) < 2:
        lines.append(cur)
    if len(lines) == 2 and len("".join(lines)) < len(text):
        last = lines[1]
        while last and font.getlength(last + "…") > max_width:
            last = last[:-1]
        lines[1] = last + "…"
    return lines


def build_cover(title: str, out_path: Path, source_image: Path | None = None,
                theme: str = "default", max_chars: int = 10) -> Path:
    # 允许两行：单行上限 max_chars，总计最多 2 行
    title = (title or "").strip()
    if len(title) > max_chars * 2:
        title = title[: max_chars * 2]
    c1, c2 = THEME_COLORS.get(theme, THEME_COLORS["default"])

    if source_image and Path(source_image).exists():
        try:
            with Image.open(source_image) as im:
                base = cover_crop(im)
        except Exception:  # noqa: BLE001 - 坏图就退化成纯色底
            base = _gradient(COVER_W, COVER_H, c1 + (255,), c2 + (255,))
    else:
        base = _gradient(COVER_W, COVER_H, c1 + (255,), c2 + (255,))

    base = base.convert("RGBA")

    # 底部渐变遮罩：保证黄字在任何底图上都能读
    band_h = int(COVER_H * 0.62)
    mask = _gradient(COVER_W, band_h, (0, 0, 0, 0), (0, 0, 0, 205))
    base.alpha_composite(mask, (0, COVER_H - band_h))

    draw = ImageDraw.Draw(base)
    font = _font(54)
    lines = _wrap(title, font, COVER_W - 96) or [""]
    line_h = 66
    total_h = line_h * len(lines)
    y = COVER_H - 34 - total_h

    for line in lines:
        tw = font.getlength(line)
        x = (COVER_W - tw) / 2
        draw.text((x + 2, y + 2), line, font=font, fill=(0, 0, 0, 120))
        draw.text((x, y), line, font=font, fill=TITLE_COLOR)
        y += line_h

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base.convert("RGB").save(out_path, "PNG", optimize=True)
    return out_path


def font_status() -> dict:
    path = find_font()
    return {"path": path, "ok": bool(path), "candidates": FONT_CANDIDATES[:6]}
