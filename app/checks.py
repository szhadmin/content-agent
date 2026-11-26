"""稿件自检：字数、违禁词、标题、结构。

验收口径：**只认纯正文汉字数**（数 [\\u4e00-\\u9fff]）。
"""
from __future__ import annotations

import re
from typing import Any

CJK = re.compile(r"[\u4e00-\u9fff]")

# 绝对化/营销化违禁词 —— 命中就是硬伤，必须改。
BANNED_WORDS = [
    "最好", "最大", "最强", "最高", "最低", "最佳", "最多", "最少", "最新",
    "最优", "最难", "最美", "最优惠", "最划算",
    "唯一", "首个", "首家", "独家", "国家级", "世界级", "顶级", "天花板",
    "永远", "百分百", "100%", "躺赚", "稳赚", "暴富", "暴利",
    "震惊", "重磅", "必看", "史上", "绝对", "必然",
]

# 只提示、不拦。『最』在「最后/最近/最终」里是正常中文；
# 「第一」在「第一，第二」里是序号，不算绝对化宣称。
SOFT_WORDS = ["最", "第一", "全部"]

# 「第一」紧跟这些字才算绝对化宣称（此处不单独判定，统一按软提示处理）

# 只检查正文，不检查这些结构性内容
_FENCE = re.compile(r"```.*?```", re.S)


def plain_text(markdown: str) -> str:
    """去掉 markdown 语法，得到接近纯正文的文本。"""
    text = _FENCE.sub("", markdown or "")
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)          # 图片
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)       # 链接保留文字
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)  # 标题符号
    text = re.sub(r"^\s*>\s?", "", text, flags=re.M)           # 引用符号
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)       # 列表符号
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.M)
    text = text.replace("*", "").replace("`", "").replace("~", "")
    return text


def cjk_count(markdown: str) -> int:
    return len(CJK.findall(plain_text(markdown)))


def find_banned(text: str, words: list[str] | None = None) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for w in (words or BANNED_WORDS):
        for m in re.finditer(re.escape(w), text):
            start = max(0, m.start() - 8)
            end = min(len(text), m.end() + 8)
            hits.append({"word": w, "context": text[start:end].replace("\n", " ")})
            break  # 每个词只报一次，够定位了
    return hits


def title_length(title: str) -> int:
    """英文字母数字各算 1 字，中文 1 字，标点不计（问号尤其不计）。"""
    return len(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]", title or ""))


def structure_stats(markdown: str) -> dict[str, int]:
    text = _FENCE.sub("", markdown or "")
    h2 = len(re.findall(r"^\s{0,3}##\s+\S", text, re.M))
    h3 = len(re.findall(r"^\s{0,3}###\s+\S", text, re.M))
    bold = len(re.findall(r"\*\*[^*\n]+\*\*", text))
    imgs = len(re.findall(r"!\[[^\]]*\]\([^)]*\)", text))
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()
             and not p.strip().startswith(("#", ">", "-", "*", "|"))]
    return {
        "h2": h2,
        "h3": h3,
        "subheads": h2 + h3,
        "bold": bold,
        "images": imgs,
        "paragraphs": len(paras),
    }


def parse_tags(markdown: str) -> list[str]:
    """取文末 #话题 标签。"""
    tags = re.findall(r"#([\u4e00-\u9fffA-Za-z0-9_]{1,20})", markdown or "")
    return tags


def run_checks(title: str, digest: str, markdown: str,
               keywords: list[str] | None = None,
               min_words: int = 500, max_words: int = 1000,
               photo_mode: bool = False) -> dict[str, Any]:
    """返回结构化体检结果。warnings 是提醒，blockers 是必须处理。

    photo_mode=True 用于「人物日常」这类以图为主的稿子：反问标题、小标题、5-8 处加粗
    都是长文那套规范，套在几句口语上只会互相打架，所以整段跳过。
    """
    keywords = keywords or []
    words = cjk_count(markdown)
    stats = structure_stats(markdown)
    body_text = plain_text(markdown)
    banned = find_banned(body_text + "\n" + (digest or "") + "\n" + (title or ""))
    soft = find_banned(body_text, SOFT_WORDS)
    tlen = title_length(title)
    # 文末 #话题 和模型给的 keywords 合并，去重保序
    tags: list[str] = []
    for t in list(keywords) + parse_tags(markdown):
        t = str(t).strip().lstrip("#")
        if t and t not in tags:
            tags.append(t)

    warnings: list[str] = []
    blockers: list[str] = []

    if words < min_words:
        warnings.append(f"正文 {words} 汉字，低于基准 {min_words}，容易退化成新闻搬运")
    elif words > max_words:
        warnings.append(f"正文 {words} 汉字，超过 {max_words}，完读率会掉")
    if tlen > 15:
        warnings.append(f"标题 {tlen} 字，超过 15 字，列表页可能被截断")
    if photo_mode:
        # 看图稿：标题本来就该是句大白话，小标题越少越好，加粗只用来点情绪
        if stats["subheads"] > 2:
            warnings.append(f"看图稿不建议加小标题（现在 {stats['subheads']} 处）")
        if stats["bold"] > 3:
            warnings.append(f"加粗 {stats['bold']} 处偏多，看图稿 0-2 处就够")
    else:
        if "？" not in (title or "") and "?" not in (title or ""):
            warnings.append("标题里没有反问句，不符合「反问 + 结论」句式")
        if stats["subheads"] == 0:
            warnings.append("没有小标题，建议加 1-2 处")
        elif stats["subheads"] > 3:
            warnings.append(f"小标题 {stats['subheads']} 处，短文建议 1-2 处")
        if stats["bold"] < 4:
            warnings.append(f"加粗只有 {stats['bold']} 处，建议 5-8 处")
        elif stats["bold"] > 10:
            warnings.append(f"加粗 {stats['bold']} 处偏多，冲淡重点")
    if stats["paragraphs"] < 3:
        warnings.append("自然段偏少，段落块建议 4-6 个")
    tag_min, tag_max = (2, 5) if photo_mode else (8, 10)
    if len(tags) < tag_min:
        warnings.append(f"话题标签只有 {len(tags)} 个，建议 {tag_min}-{tag_max} 个")
    elif len(tags) > tag_max:
        warnings.append(f"话题标签 {len(tags)} 个偏多，建议 {tag_min}-{tag_max} 个")
    if banned:
        blockers.append("命中违禁词：" + "、".join(sorted({b['word'] for b in banned})))
    if soft:
        warnings.append("出现「" + "」「".join(sorted({b['word'] for b in soft})) +
                        "」—— 若是「最后/最近/第一，第二」这类正常用法可忽略，否则请改掉")

    return {
        "words": words,
        "title_length": tlen,
        "tags": tags,
        "tags_count": len(tags),
        "structure": stats,
        "banned": banned,
        "warnings": warnings,
        "blockers": blockers,
        "ok": not blockers,
    }
