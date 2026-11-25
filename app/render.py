"""Markdown → 微信公众号内联样式 HTML。

为什么自己写：公众号正文只认内联样式（不支持 <style> 块、不认 class），
所以必须逐元素注入 style。这里用 markdown-it 的 token 流直接产出 HTML，
比"先生成 HTML 再正则替换"稳，不会破坏嵌套结构。

硬约束（来自实测踩坑）：
  · 不能有 <style> 块、不能有 class
  · 不能有 <h1>（一级标题降级成 h2 样式）
  · 图片必须全部换成 mmbiz 链接后才能提交草稿
  · 图注用 12.5px 灰字，每张图单独一行
"""
from __future__ import annotations

import html as _html
import re
from typing import Any

from markdown_it import MarkdownIt

# --------------------------------------------------------------------------
# 主题
# --------------------------------------------------------------------------

THEMES: dict[str, dict[str, Any]] = {
    "default": {
        "label": "克制黑白",
        "desc": "评论/热点稿通用，最耐读，不抢内容",
        "body": "font-size:16px;color:#3f3f3f;line-height:1.9;letter-spacing:.4px;"
                "word-break:break-word;text-align:justify;",
        "p": "margin:0 0 20px;",
        "h2": "font-size:17px;font-weight:700;color:#111;margin:34px 0 16px;padding-left:10px;"
              "border-left:4px solid #111;line-height:1.4;",
        "h3": "font-size:16px;font-weight:700;color:#111;margin:26px 0 12px;line-height:1.4;",
        "strong": "font-weight:700;color:#111;background:linear-gradient(transparent 62%,#ffe9a8 62%);",
        "em": "font-style:normal;color:#8a8a8a;",
        "blockquote": "margin:24px 0;padding:14px 16px;background:#f7f7f7;"
                      "border-left:3px solid #d0d0d0;color:#555;font-size:15px;line-height:1.8;",
        "ul": "margin:0 0 20px;padding-left:22px;",
        "ol": "margin:0 0 20px;padding-left:22px;",
        "li": "margin:0 0 8px;",
        "code_inline": "background:#f2f2f2;padding:2px 5px;border-radius:3px;font-size:14px;color:#c7254e;",
        "pre": "margin:20px 0;padding:14px;background:#f7f7f7;border-radius:6px;"
               "overflow-x:auto;font-size:13px;line-height:1.6;",
        "hr": "border:none;border-top:1px solid #ececec;margin:32px 0;",
        "img": "max-width:100%;height:auto;display:block;margin:0 auto;border-radius:4px;",
        "caption": "font-size:12.5px;color:#999;text-align:center;margin:8px 0 24px;line-height:1.5;",
        "a": "color:#576b95;text-decoration:none;",
        "table": "width:100%;border-collapse:collapse;font-size:14px;margin:20px 0;",
        "th": "border:1px solid #e5e5e5;padding:8px;background:#f7f7f7;font-weight:600;text-align:left;",
        "td": "border:1px solid #e5e5e5;padding:8px;",
    },
    "elegant": {
        "label": "文艺米白",
        "desc": "深度长文、文化观察，暖底色更柔和",
        "body": "font-size:16px;color:#4a4238;line-height:1.95;letter-spacing:.5px;"
                "word-break:break-word;text-align:justify;",
        "p": "margin:0 0 20px;",
        "h2": "font-size:17px;font-weight:700;color:#3a3229;margin:34px 0 16px;"
              "padding:0 0 6px;border-bottom:1px solid #e6ddcd;line-height:1.4;",
        "h3": "font-size:16px;font-weight:700;color:#3a3229;margin:26px 0 12px;",
        "strong": "font-weight:700;color:#8c6a34;",
        "em": "font-style:normal;color:#9b9187;",
        "blockquote": "margin:24px 0;padding:14px 16px;background:#faf6ef;"
                      "border-left:3px solid #dcc9a6;color:#6b6055;font-size:15px;line-height:1.85;",
        "ul": "margin:0 0 20px;padding-left:22px;",
        "ol": "margin:0 0 20px;padding-left:22px;",
        "li": "margin:0 0 8px;",
        "code_inline": "background:#f4efe6;padding:2px 5px;border-radius:3px;font-size:14px;color:#a2563a;",
        "pre": "margin:20px 0;padding:14px;background:#faf6ef;border-radius:6px;"
               "overflow-x:auto;font-size:13px;line-height:1.6;",
        "hr": "border:none;border-top:1px solid #eae2d4;margin:32px 0;",
        "img": "max-width:100%;height:auto;display:block;margin:0 auto;border-radius:6px;",
        "caption": "font-size:12.5px;color:#a89e93;text-align:center;margin:8px 0 24px;line-height:1.5;",
        "a": "color:#8c6a34;text-decoration:none;",
        "table": "width:100%;border-collapse:collapse;font-size:14px;margin:20px 0;",
        "th": "border:1px solid #e6ddcd;padding:8px;background:#faf6ef;font-weight:600;text-align:left;",
        "td": "border:1px solid #e6ddcd;padding:8px;",
    },
    "tech": {
        "label": "理性科技蓝",
        "desc": "AI/数码/财经分析，信息密度高",
        "body": "font-size:16px;color:#33383d;line-height:1.85;letter-spacing:.3px;"
                "word-break:break-word;text-align:justify;",
        "p": "margin:0 0 20px;",
        "h2": "font-size:17px;font-weight:700;color:#0f2b46;margin:34px 0 16px;"
              "padding:8px 12px;background:#eef4fb;border-radius:4px;line-height:1.4;",
        "h3": "font-size:16px;font-weight:700;color:#124a7a;margin:26px 0 12px;",
        "strong": "font-weight:700;color:#1464a5;",
        "em": "font-style:normal;color:#8b949e;",
        "blockquote": "margin:24px 0;padding:14px 16px;background:#f5f9fd;"
                      "border-left:3px solid #7fb0dd;color:#42505c;font-size:15px;line-height:1.8;",
        "ul": "margin:0 0 20px;padding-left:22px;",
        "ol": "margin:0 0 20px;padding-left:22px;",
        "li": "margin:0 0 8px;",
        "code_inline": "background:#eef2f6;padding:2px 5px;border-radius:3px;font-size:14px;color:#1464a5;",
        "pre": "margin:20px 0;padding:14px;background:#0f2b46;color:#dbe7f3;border-radius:6px;"
               "overflow-x:auto;font-size:13px;line-height:1.6;",
        "hr": "border:none;border-top:1px solid #e3eaf1;margin:32px 0;",
        "img": "max-width:100%;height:auto;display:block;margin:0 auto;border-radius:4px;",
        "caption": "font-size:12.5px;color:#94a3b1;text-align:center;margin:8px 0 24px;line-height:1.5;",
        "a": "color:#1464a5;text-decoration:none;",
        "table": "width:100%;border-collapse:collapse;font-size:14px;margin:20px 0;",
        "th": "border:1px solid #e3eaf1;padding:8px;background:#eef4fb;font-weight:600;text-align:left;",
        "td": "border:1px solid #e3eaf1;padding:8px;",
    },
    "warm": {
        "label": "暖橙生活",
        "desc": "民生/消费/职场话题，亲和力强",
        "body": "font-size:16px;color:#40372f;line-height:1.9;letter-spacing:.4px;"
                "word-break:break-word;text-align:justify;",
        "p": "margin:0 0 20px;",
        "h2": "font-size:17px;font-weight:700;color:#2f2620;margin:34px 0 16px;padding-left:10px;"
              "border-left:4px solid #e8734a;line-height:1.4;",
        "h3": "font-size:16px;font-weight:700;color:#2f2620;margin:26px 0 12px;",
        "strong": "font-weight:700;color:#c8542c;",
        "em": "font-style:normal;color:#95897d;",
        "blockquote": "margin:24px 0;padding:14px 16px;background:#fdf5f1;"
                      "border-left:3px solid #f0b79f;color:#5d5148;font-size:15px;line-height:1.8;",
        "ul": "margin:0 0 20px;padding-left:22px;",
        "ol": "margin:0 0 20px;padding-left:22px;",
        "li": "margin:0 0 8px;",
        "code_inline": "background:#f6efe9;padding:2px 5px;border-radius:3px;font-size:14px;color:#c8542c;",
        "pre": "margin:20px 0;padding:14px;background:#faf3ee;border-radius:6px;"
               "overflow-x:auto;font-size:13px;line-height:1.6;",
        "hr": "border:none;border-top:1px solid #f0e5dd;margin:32px 0;",
        "img": "max-width:100%;height:auto;display:block;margin:0 auto;border-radius:4px;",
        "caption": "font-size:12.5px;color:#a99c90;text-align:center;margin:8px 0 24px;line-height:1.5;",
        "a": "color:#c8542c;text-decoration:none;",
        "table": "width:100%;border-collapse:collapse;font-size:14px;margin:20px 0;",
        "th": "border:1px solid #f0e5dd;padding:8px;background:#fdf5f1;font-weight:600;text-align:left;",
        "td": "border:1px solid #f0e5dd;padding:8px;",
    },
}

FONT_SCALES = {"small": 0.94, "medium": 1.0, "large": 1.08}

# 领域主题放在单独文件里，避免这里堆成几百行样式表；合并逻辑见下面
# --------------------------------------------------------------------------
# 「看图日常」主题：给人物日常/生活分享这类**以图为主**的稿子用
# --------------------------------------------------------------------------
# 长文那几套主体是文字，图片只当插花，所以 img 都是 `margin:0 auto`（没有上下留白），
# 放在晒图稿里就变成"文字贴图、图贴文字"，看着闷。
# 这一套反过来：图大、圆角、有投影、上下留白足，文字短行，加粗不用黄色高亮
# （高亮在长文里是重点线，在晒图稿里只会显得脏）。
THEMES["photo"] = {
    "label": "看图日常",
    "desc": "虚拟人物/生活分享：图片圆角留白有投影，文字短行，整体轻",
    "body": "font-size:16px;color:#22262e;line-height:2.05;letter-spacing:.5px;"
            "word-break:break-word;text-align:left;",
    "p": "margin:0 0 24px;",
    "h2": "font-size:17px;font-weight:700;color:#111;margin:34px 0 18px;line-height:1.4;",
    "h3": "font-size:16px;font-weight:700;color:#111;margin:26px 0 12px;line-height:1.4;",
    "strong": "font-weight:700;color:#111;",
    "em": "font-style:normal;color:#8a8f99;",
    "blockquote": "margin:26px 0;padding:14px 16px;background:#f6f7fa;"
                  "border-left:none;border-radius:12px;color:#4b5160;font-size:15px;line-height:1.9;",
    "ul": "margin:0 0 22px;padding-left:22px;",
    "ol": "margin:0 0 22px;padding-left:22px;",
    "li": "margin:0 0 10px;",
    "code_inline": "background:#f2f3f7;padding:2px 5px;border-radius:4px;font-size:14px;color:#4b5160;",
    "pre": "margin:22px 0;padding:14px;background:#f6f7fa;border-radius:10px;"
           "overflow-x:auto;font-size:13px;line-height:1.6;",
    "hr": "border:none;border-top:1px solid #eef0f5;margin:34px 0;",
    # 关键：上下留白 + 大圆角 + 轻投影，图才"立"得起来
    "img": "max-width:100%;height:auto;display:block;margin:8px auto 12px;"
           "border-radius:14px;box-shadow:0 6px 20px rgba(16,24,40,.08);",
    "caption": "font-size:12.5px;color:#9aa1ad;text-align:center;margin:0 0 26px;line-height:1.6;",
    "a": "color:#576b95;text-decoration:none;",
    "table": "width:100%;border-collapse:collapse;font-size:14px;margin:22px 0;",
    "th": "border:1px solid #eceef3;padding:8px;background:#f7f8fb;font-weight:600;text-align:left;",
    "td": "border:1px solid #eceef3;padding:8px;",
}

from ._themes_extra import merge_extra_themes  # noqa: E402

THEMES = merge_extra_themes(THEMES)
_CJK = re.compile(r"[\u4e00-\u9fff]")
_IMG_SRC = re.compile(r'<img[^>]+src="([^"]+)"')


def _md() -> MarkdownIt:
    md = MarkdownIt("commonmark", {"html": True, "breaks": True, "linkify": False})
    md.enable("table")
    md.enable("strikethrough")
    return md


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------

class _Renderer:
    def __init__(self, theme: dict[str, Any], font_scale: float = 1.0):
        self.t = theme
        self.scale = font_scale
        self.last_block_was_image = False
        self._last_img_caption = False

    # ---------- 内联 ----------

    def inline(self, children) -> str:
        out: list[str] = []
        for c in children:
            tt = c.type
            if tt == "text":
                out.append(_html.escape(c.content).replace("\n", " "))
            elif tt in ("softbreak", "hardbreak"):
                out.append("<br>")
            elif tt == "strong_open":
                out.append(f'<strong style="{self.t["strong"]}">')
            elif tt == "strong_close":
                out.append("</strong>")
            elif tt == "em_open":
                out.append(f'<em style="{self.t["em"]}">')
            elif tt == "em_close":
                out.append("</em>")
            elif tt == "s_open":
                out.append("<s>")
            elif tt == "s_close":
                out.append("</s>")
            elif tt == "code_inline":
                out.append(f'<code style="{self.t["code_inline"]}">{_html.escape(c.content)}</code>')
            elif tt == "link_open":
                href = _html.escape(str(c.attrs.get("href", "")), quote=True)
                out.append(f'<a style="{self.t["a"]}" href="{href}">')
            elif tt == "link_close":
                out.append("</a>")
            elif tt == "image":
                # markdown-it 的图片 token：src 在 attrs，alt 文本在 content
                alt = (c.content or c.attrs.get("alt") or "")
                out.append(self.image(c.attrs.get("src", ""), alt, title=None, block=False))
            elif tt == "html_inline":
                out.append(c.content)
            else:
                if c.content:
                    out.append(_html.escape(c.content))
        return "".join(out)

    def image(self, src: str, alt: str, title: str | None, block: bool = True) -> str:
        src_attr = _html.escape(src or "", quote=True)
        alt_attr = _html.escape(alt or "", quote=True)
        img = f'<img src="{src_attr}" alt="{alt_attr}" style="{self.t["img"]}" />'
        # alt 上有实质文字就当作图注（不要把文件名/URL 当图注）
        caption = ""
        cand = (alt or "").strip()
        if cand and not re.search(r"\.(jpe?g|png|gif|webp|bmp)$", cand, re.I) and not cand.startswith("http"):
            caption = f'<p style="{self.t["caption"]}">{_html.escape(cand)}</p>'
        if not block:
            self._last_img_caption = bool(caption)
            return img
        self._last_img_caption = bool(caption)
        return img + caption

    # ---------- 块 ----------

    def blocks(self, tokens, start: int, stop: tuple[str, ...]) -> tuple[str, int]:
        out: list[str] = []
        i = start
        while i < len(tokens):
            tok = tokens[i]
            if tok.type in stop:
                return "".join(out), i

            if tok.type == "paragraph_open":
                inner = tokens[i + 1]
                kids = [c for c in (inner.children or []) if not (c.type == "text" and not c.content.strip())]
                if len(kids) == 1 and kids[0].type == "image":
                    im = kids[0]
                    alt = (im.content or im.attrs.get("alt") or "")
                    out.append(self.image(im.attrs.get("src", ""), alt, None, block=True))
                    self.last_block_was_image = True
                elif (self.last_block_was_image and not self._last_img_caption
                      and len(kids) >= 2
                      and kids[0].type == "em_open" and kids[-1].type == "em_close"):
                    # 图片后紧跟的斜体短段 → 当作图注
                    cap = "".join(c.content for c in kids if c.type in ("text", "code_inline")).strip()
                    if cap and len(cap) <= 60:
                        out.append(f'<p style="{self.t["caption"]}">{_html.escape(cap)}</p>')
                        self.last_block_was_image = False
                    else:
                        out.append(f'<p style="{self.t["p"]}">{self.inline(inner.children or [])}</p>')
                else:
                    out.append(f'<p style="{self.t["p"]}">{self.inline(inner.children or [])}</p>')
                    self.last_block_was_image = False
                i += 2  # paragraph_open, inline, paragraph_close
                if i < len(tokens) and tokens[i].type == "paragraph_close":
                    i += 1
                continue

            if tok.type == "heading_open":
                level = int(tok.tag[1])
                main_level = 2 if level <= 2 else 3
                key = "h2" if main_level == 2 else "h3"
                style = self._scale(self.t[key], 1.0 if main_level == 2 else 0.94)
                body = self.inline(tokens[i + 1].children or [])
                out.append(f'<h{main_level} style="{style}">{body}</h{main_level}>')
                self.last_block_was_image = False
                i += 3  # open, inline, close
                continue

            if tok.type == "blockquote_open":
                inner, i = self.blocks(tokens, i + 1, ("blockquote_close",))
                out.append(f'<blockquote style="{self.t["blockquote"]}">{inner}</blockquote>')
                self.last_block_was_image = False
                i += 1
                continue

            if tok.type in ("bullet_list_open", "ordered_list_open"):
                tag = "ul" if tok.type.startswith("bullet") else "ol"
                close = f"{tag}_close"
                items: list[str] = []
                j = i + 1
                while j < len(tokens) and tokens[j].type != close:
                    if tokens[j].type == "list_item_open":
                        inner, j = self.blocks(tokens, j + 1, ("list_item_close",))
                        items.append(f'<li style="{self.t["li"]}">{inner}</li>')
                        j += 1
                    else:
                        j += 1
                out.append(f'<{tag} style="{self.t[tag]}">{"".join(items)}</{tag}>')
                self.last_block_was_image = False
                i = j + 1
                continue

            if tok.type in ("fence", "code_block"):
                code = _html.escape(tok.content.rstrip("\n"))
                out.append(f'<pre style="{self.t["pre"]}"><code>{code}</code></pre>')
                self.last_block_was_image = False
                i += 1
                continue

            if tok.type == "hr":
                out.append(f'<hr style="{self.t["hr"]}" />')
                self.last_block_was_image = False
                i += 1
                continue

            if tok.type == "table_open":
                inner, i = self.blocks(tokens, i + 1, ("table_close",))
                out.append(f'<table style="{self.t["table"]}">{inner}</table>')
                self.last_block_was_image = False
                i += 1
                continue

            if tok.type in ("thead_open", "tbody_open", "tr_open"):
                tag = tok.type.split("_")[0]
                close = f"{tag}_close"
                inner, i = self.blocks(tokens, i + 1, (close,))
                if tag == "tr":
                    out.append(f"<tr>{inner}</tr>")
                else:
                    out.append(f"<{tag}>{inner}</{tag}>")
                i += 1
                continue

            if tok.type in ("th_open", "td_open"):
                tag = tok.type.split("_")[0]
                body = self.inline(tokens[i + 1].children or [])
                out.append(f'<{tag} style="{self.t[tag]}">{body}</{tag}>')
                i += 3
                continue

            if tok.type == "html_block":
                out.append(tok.content)
                i += 1
                continue

            if tok.type == "inline":
                out.append(f'<p style="{self.t["p"]}">{self.inline(tok.children or [])}</p>')
                i += 1
                continue

            i += 1
        return "".join(out), i

    def _scale(self, style: str, base: float) -> str:
        factor = base * self.scale
        if abs(factor - 1.0) < 1e-6:
            return style

        def repl(m: re.Match) -> str:
            return f"font-size:{round(float(m.group(1)) * factor, 1)}px"

        return re.sub(r"font-size:([\d.]+)px", repl, style)

    def render(self, markdown_text: str) -> str:
        tokens = _md().parse(markdown_text or "")
        inner, _ = self.blocks(tokens, 0, ())
        if not inner.strip():
            inner = '<p style="margin:0 0 20px;color:#999;">（正文为空）</p>'
        return f'<section style="{self.t["body"]}">{inner}</section>'


def normalize_markdown(md: str) -> str:
    """把"整篇用单换行"的 Markdown 补成规范的块结构。

    模型偶尔会输出 `## 小标题` 紧接正文、段与段之间没有空行的版本。
    这种文本按 `\n\n` 切只有一块，插图、段落识别全会失效。
    """
    if not md:
        return md
    lines = md.split("\n")
    nonempty = [x for x in lines if x.strip()]
    blanks = len(lines) - len(nonempty)
    if not nonempty:
        return md
    # 空行已经够用（大致每段之间都有），就不动
    if blanks >= max(1, len(nonempty) // 2):
        return md

    out: list[str] = []
    in_fence = False
    prev = ""
    for raw in lines:
        ln = raw.rstrip()
        s = ln.lstrip()
        if s.startswith("```"):
            in_fence = not in_fence
        if not ln.strip():
            if out and out[-1] != "":
                out.append("")
            prev = ""
            continue
        if in_fence:
            kind = "fence"
        elif s.startswith("#"):
            kind = "heading"
        elif s.startswith("!"):
            kind = "img"
        elif s.startswith(">"):
            kind = "quote"
        elif s.startswith("|"):
            kind = "table"
        elif re.match(r"^([-*+]|\d+[.)])\s", s):
            kind = "list"
        else:
            kind = "text"
        if out and out[-1] != "" and kind != prev:
            out.append("")
        out.append(ln)
        prev = kind
    return "\n".join(out)


def render_markdown(markdown_text: str, theme: str = "default", font_size: str = "medium") -> str:
    t = THEMES.get(theme) or THEMES["default"]
    return _Renderer(t, FONT_SCALES.get(font_size, 1.0)).render(markdown_text)


# --------------------------------------------------------------------------
# 体检（提交前/回读后都跑这一套）
# --------------------------------------------------------------------------

def count_cjk(text: str) -> int:
    return len(_CJK.findall(text or ""))


def inspect_html(html: str) -> dict[str, Any]:
    """只认硬指标。不要用关键字匹配判断中文是否正确（回读会是转义形态）。"""
    img_srcs = _IMG_SRC.findall(html or "")
    local = [s for s in img_srcs if not s.startswith("http")]
    mmbiz_imgs = [s for s in img_srcs if "mmbiz.qpic.cn" in s]
    return {
        "class_count": len(re.findall(r'\sclass\s*=', html or "")),
        "style_block": len(re.findall(r"<style", html or "", re.I)),
        "h1": len(re.findall(r"<h1[\s>]", html or "", re.I)),
        "h2": len(re.findall(r"<h2[\s>]", html or "", re.I)),
        "img": len(re.findall(r"<img", html or "", re.I)),
        "img_src": len(img_srcs),
        "mmbiz_img": len(mmbiz_imgs),
        "local_src": len(local),
        "local_src_list": local[:5],
        "chars": len(html or ""),
    }


def classify_srcs(html: str) -> dict[str, list[str]]:
    """把图片地址分成三类：已上传(微信) / 待上传(本地素材库) / 无效。"""
    srcs = _IMG_SRC.findall(html or "")
    wechat = [s for s in srcs if "mmbiz.qpic.cn" in s]
    pending = [s for s in srcs if s.startswith(("/media/uploads/", "uploads/"))]
    bad = [s for s in srcs if s not in wechat and s not in pending]
    return {"wechat": wechat, "pending": pending, "bad": bad}


def draft_blockers(html: str, allow_pending_uploads: bool = False) -> list[str]:
    """提交草稿前必须为空的项。

    allow_pending_uploads=True 用于「编辑期预览」：/media/uploads/ 下的图
    会在 submit 时自动 uploadimg 换掉，不该当成错误吓人。
    """
    info = inspect_html(html)
    groups = classify_srcs(html)
    problems = []
    if info["class_count"]:
        problems.append(f"存在 {info['class_count']} 个 class 属性（公众号会丢样式）")
    if info["style_block"]:
        problems.append("存在 <style> 块（公众号会整段清除）")
    if info["h1"]:
        problems.append("存在 <h1>（公众号正文不支持一级标题）")
    bad = groups["bad"]
    if bad:
        problems.append(f"存在 {len(bad)} 个无法上传的图片地址：{bad[:3]}")
    if groups["pending"] and not allow_pending_uploads:
        problems.append(f"有 {len(groups['pending'])} 张本地图还没换成微信链接")
    return problems


def pending_note(html: str) -> str:
    groups = classify_srcs(html)
    if not groups["pending"]:
        return ""
    return f"{len(groups['pending'])} 张本地图，存入草稿箱时会自动上传并替换"
