"""DeepSeek（OpenAI 兼容）写作客户端。"""
from __future__ import annotations

import json
import re
from typing import Any

import requests

from . import db
from .personas import platform_writing_rule
from .prompts import (
    DEFAULT_STYLE_PROMPT,
    HUMANIZE_PROMPT,
    LAYOUT_PROMPT,
    TOPIC_SELECT_PROMPT,
    title_rule_for,
)
from .settings import DEFAULT_LLM


class LLMError(RuntimeError):
    pass


def llm_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_LLM)
    cfg.update(db.get_setting("llm", {}) or {})
    cfg["api_key"] = _decrypt_key(cfg.get("api_key") or "")
    if not cfg.get("api_key"):
        import os

        cfg["api_key"] = os.environ.get("WXWB_LLM_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")
    return cfg


def _decrypt_key(value: str) -> str:
    """库里的 Key 是 Fernet 密文（gAAAA 开头），读出来要解密。"""
    if not value:
        return ""
    if value.startswith("gAAAA"):
        from .security import decrypt_secret

        try:
            return decrypt_secret(value)
        except Exception:  # noqa: BLE001
            return value
    return value


def style_prompt(author: str, account: dict | None = None) -> str:
    """账号有自己的写作规范就用它，否则用全局规范；人设拼最前，平台规则拼最后。"""
    account = account or {}
    own = (account.get("style_prompt") or "").strip()
    tpl = own or (db.get_setting("style_prompt") or DEFAULT_STYLE_PROMPT)
    try:
        base = tpl.format(author=author or "主笔")
    except (KeyError, IndexError, ValueError):
        base = tpl.replace("{author}", author or "主笔")

    persona = (account.get("persona") or "").strip()
    if persona:
        domain = (account.get("topic_domain") or "").strip()
        head = "## 你的身份\n" + persona
        if domain:
            head += f"\n\n选题领域限定：{domain}。写作时的例子、类比、论据优先从这个领域里取。"
        base = head + "\n\n" + base

    # 平台差异拼在最后：公众号的「反问+结论 ≤15 字」不该硬套给头条
    base += "\n\n## 平台要求\n" + platform_writing_rule(account.get("platform") or "wechat")
    return base


def chat(messages: list[dict[str, str]], *, json_mode: bool = False,
         temperature: float | None = None, timeout: int = 240) -> str:
    cfg = llm_config()
    if not cfg.get("api_key"):
        raise LLMError("还没有配置 DeepSeek API Key，去「设置」页填一下")
    base = (cfg.get("base_url") or DEFAULT_LLM["base_url"]).rstrip("/")
    url = f"{base}/chat/completions"
    payload: dict[str, Any] = {
        "model": cfg.get("model") or DEFAULT_LLM["model"],
        "messages": messages,
        "temperature": cfg.get("temperature", 0.9) if temperature is None else temperature,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(
            url,
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
            },
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise LLMError(f"调用模型失败：{exc}") from exc
    if r.status_code >= 400:
        raise LLMError(f"模型接口 {r.status_code}：{r.text[:300]}")
    try:
        d = r.json()
        return d["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as exc:
        raise LLMError(f"模型返回格式异常：{r.text[:300]}") from exc


def _extract_json(text: str) -> Any:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except ValueError:
        pass
    for pattern in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pattern, text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                continue
    raise LLMError("模型没有返回可解析的 JSON（可以再点一次生成）")


# --------------------------------------------------------------------------

def pick_topic(items: list[dict[str, Any]], author: str, recent_lines: list[str],
               candidates_txt: str, account: dict | None = None) -> dict[str, Any]:
    account = account or {}
    persona = (account.get("persona") or "").strip() or "（未设置人设，按综合社会热点评论号处理）"
    domain = (account.get("topic_domain") or "").strip() or "社会热点综合"
    prompt = TOPIC_SELECT_PROMPT.format(
        author=author or "主笔",
        persona=persona,
        domain=domain,
        candidates=candidates_txt,
        recent="\n".join(recent_lines[:20]) or "（无）",
    )
    out = chat([
        {"role": "system", "content": "你是资深公众号选题编辑，只输出 JSON。"},
        {"role": "user", "content": prompt},
    ], json_mode=True, temperature=0.5)
    data = _extract_json(out)
    if not isinstance(data, dict):
        raise LLMError("选题结果格式不对")
    idx = int(data.get("rank") or 0)
    usable = [x for x in items if x["usable"]]
    chosen = None
    if 1 <= idx <= len(usable):
        chosen = usable[idx - 1]
    elif data.get("keyword"):
        chosen = next((x for x in usable if x["word"] == data["keyword"]), None)
    data["item"] = chosen
    return data


_ORDINAL = re.compile(r"^\s*(?:[一二三四五六七八九十]+[、.．]|[（(][一二三四五六七八九十]+[)）]|\d+[、.．])\s*")


def _strip_ordinal(text: str) -> str:
    """规范里禁止「一、二、三」编号式小标题，这里统一剥掉。"""
    prev = None
    while prev != text:
        prev = text
        text = _ORDINAL.sub("", text)
    return text.strip()


def _to_markdown(data: dict[str, Any]) -> str:
    """模型有时不按 markdown 字段输出，这里把几种常见形状都接住。"""
    for key in ("markdown", "content", "body", "article"):
        val = data.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()

    parts: list[str] = []
    for sec in data.get("sections") or data.get("parts") or []:
        if not isinstance(sec, dict):
            continue
        head = _strip_ordinal(str(sec.get("heading") or sec.get("title") or ""))
        if head:
            parts.append("## " + head)
        paras = sec.get("paragraphs") or sec.get("content") or []
        if isinstance(paras, str):
            paras = [paras]
        for p in paras:
            if isinstance(p, dict):
                p = p.get("text") or p.get("content") or ""
            p = str(p).strip()
            if p:
                parts.append(p)
    if parts:
        concl = data.get("conclusion") or data.get("ending")
        if isinstance(concl, str) and concl.strip():
            parts.append(concl.strip())
        return "\n\n".join(parts)
    return ""


def generate_article(*, topic: str, author: str, extra: str = "",
                     digest_hint: str = "", account: dict | None = None) -> dict[str, Any]:
    from .checks import cjk_count

    system = style_prompt(author, account)
    platform = (account or {}).get("platform") or "wechat"
    user = [f"今天的选题：{topic}"]
    if digest_hint:
        user.append(f"补充事实/角度：{digest_hint}")
    if extra:
        user.append(f"额外要求：{extra}")
    # 标题规则按平台来，不再把公众号那套硬塞给头条/点评
    user.append("标题规则：" + title_rule_for(platform))
    tag_lo, tag_hi = TAG_COUNTS.get(platform, TAG_COUNTS["wechat"])
    user.append(
        "硬性要求：\n"
        "1. 纯正文控制在 500-800 汉字之间，超过 900 字这篇就作废。\n"
        "2. 正文里必须有 5-8 处 **加粗**（标在关键判断句上），1-2 个 ## 小标题，小标题不要带「一、二、三」序号。\n"
        f"3. 结尾留一句具体的互动提问；然后**空一行，在全文最后单独一行**给 {tag_lo}-{tag_hi} 个 #话题标签。\n"
        "   **只给这一组，不要再按平台分成「公众号话题」「头条话题」两块，也不要写平台名**——\n"
        "   这篇稿子只会投一个平台，多写的那块会原样出现在读者面前。\n"
        "4. 只输出下面这个 JSON 结构，不要加别的字段，不要用 sections/paragraphs 嵌套：\n"
        '{"title": "符合上面标题规则的标题", "digest": "40-70字摘要", '
        '"keywords": ["标签1", "标签2"], "markdown": "完整正文 Markdown，含 ## 小标题和 **加粗**"}'
    )
    out = chat([
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(user)},
    ], json_mode=True)
    data = _extract_json(out)
    if not isinstance(data, dict):
        raise LLMError("模型没有返回 JSON 对象（可以再点一次生成）")

    markdown = _to_markdown(data)
    if not markdown:
        raise LLMError("模型没有返回正文（可以再点一次生成）")

    # 超长就压一次。规范和实测都说明破 1000 字完读率会掉。
    words = cjk_count(markdown)
    if words > 1100:
        try:
            markdown = compress(markdown, keep=int(words * 0.6))
            words = cjk_count(markdown)
        except LLMError:
            pass

    title = _strip_ordinal(str(data.get("title") or "").strip())
    digest = str(data.get("digest") or data.get("summary") or data.get("abstract") or "").strip()
    if not digest:
        # 摘要缺失就从正文首段截一段，别让草稿列表页空着
        from .checks import plain_text

        first = next((p.strip() for p in plain_text(markdown).split("\n") if p.strip()), "")
        digest = re.sub(r"[*#>`]", "", first)[:70]
    raw_kw = data.get("keywords") or data.get("tags") or []
    if isinstance(raw_kw, str):
        raw_kw = [k.strip() for k in re.split(r"[,，、\s]+", raw_kw) if k.strip()]
    keywords = [str(k).strip().lstrip("#") for k in raw_kw if str(k).strip()][:12]

    return {
        "title": title,
        "digest": digest[:120],
        "keywords": keywords,
        "markdown": markdown,
        "words": words,
    }


PHOTO_PROMPT = """你在运营一个「虚拟人物」生活分享号。人设：{persona}

今天这条的内容方向：{topic}

请写一条**看图说话型**的生活分享（像真实女生发朋友圈/小红书那种），要求：

【内容要有看点，不能只是流水账】
必须有一条**具体的事件线**撑起来，例如：
- 今天遇到的糟心事 / 小插曲（被人插队、外卖洒了、地铁末班车跑了）
- 一个吐槽（公司的、房东的、通勤的、涨价的）
- 一点反差或意外（以为很难吃结果很好 / 以为要加班结果没加）
- 一点轻八卦（同事、邻居、路人的事，不要指名道姓）
把这些写成"我就遇到过"的口吻，**具体到细节**：几点、多少钱、什么店、对方做了什么。

【正文】
1. 标题：口语化、10 字以内，像随手写的一句。不要公众号那种「反问+结论」标题，
   不要书名号，不要煽情升华。例：「下班后去吃了关东煮」「被物业气到的一天」。
2. 句子：{lines} 句中带一件具体的事和一句吐槽/情绪，第一人称、大白话。
   每句单独一段，10~40 字。
3. 允许 1~2 处 **加粗**，只用来强调最有情绪的那几个字，别整句加粗。
4. 不要小标题、不要排比、不要鸡汤结尾、不要喊口号。

【画面】
给出 {count} 个**可以拍成照片的具体画面**，按时间顺序推进，每张都要有动作、地点，
以及和上一张不同的机位感（远景/近景/背影/过肩不要重复）。画面里必须出现"我"。

只输出 JSON，不要别的字段：
{{"title": "标题", "digest": "25-45字摘要", "lines": ["句子1", "句子2", "…"],
  "scenes": ["画面1：…", "画面2：…", "画面3：…"], "keywords": ["标签1", "标签2"]}}
"""


def write_photo_post(*, topic: str, account: dict | None = None, count: int = 3,
                     extra: str = "") -> dict[str, Any]:
    """人物日常模式：出「几张图 + 几句口语」的稿子，不写长文。

    和 generate_article 是两条完全不同的路子——这里要的是画面感 + 一件具体的事，
    不是论述。字数少但必须有看点（吐槽、小插曲、反差），否则就是无意义的流水账。
    """
    persona = ((account or {}).get("persona") or "").strip() or "一个在一线城市打工的年轻人"
    count = max(1, min(int(count or 3), 6))
    # 图多的时候多给几句话，让每张图旁边都有内容，别一张图干晾着
    lines = max(5, min(4 + count, 9))
    prompt = PHOTO_PROMPT.format(persona=persona, topic=topic, count=count, lines=lines)
    if extra:
        prompt += f"\n额外要求：{extra}"
    out = chat([
        {"role": "system", "content": "你是生活方式博主的内容编辑，擅长写图和字搭配的短分享，"
                                      "尤其擅长把日常小事写得有情绪、有看点。"},
        {"role": "user", "content": prompt},
    ], json_mode=True)
    data = _extract_json(out)
    if not isinstance(data, dict):
        raise LLMError("模型没有返回 JSON（可以再点一次）")

    title = _strip_ordinal(str(data.get("title") or "").strip())[:20] or topic[:20]
    got_lines = [str(x).strip() for x in (data.get("lines") or []) if str(x).strip()]
    if not got_lines:
        raise LLMError("模型没有给出正文句子（可以再点一次）")
    scenes = [str(x).strip() for x in (data.get("scenes") or []) if str(x).strip()]
    if not scenes:
        scenes = [f"{topic}，人物自然地出现在画面里"] * count
    scenes = scenes[:count]
    while len(scenes) < count:
        scenes.append(scenes[-1])

    digest = str(data.get("digest") or "").strip() or " ".join(got_lines)[:60]
    raw_kw = data.get("keywords") or []
    if isinstance(raw_kw, str):
        raw_kw = [k.strip() for k in re.split(r"[,，、\s]+", raw_kw) if k.strip()]
    keywords = [str(k).strip().lstrip("#") for k in raw_kw if str(k).strip()][:12]
    return {"title": title, "digest": digest[:120], "lines": got_lines,
            "scenes": scenes, "keywords": keywords}


# --------------------------------------------------------------------------
# 话题标签：收成一组，放最底部
# --------------------------------------------------------------------------
# 为什么需要这层后处理：提示词里写「按平台给标签」时，模型经常一次给出
# 「公众号话题：…」和「头条/点评话题：…」两块，中间还夹着配图，读起来像两份稿子拼的。
# 光靠提示词约束不住（模型时好时坏），所以出口再兜一遍：合并去重 → 只留一行 → 挪到最底。
_HASHTAG = re.compile(r"#([\u4e00-\u9fffA-Za-z0-9_]{1,20})")
# 平台名 → 标签数量上限（跟 personas.PLATFORM_RULES 保持一致）
TAG_COUNTS = {"wechat": (8, 10), "toutiao": (3, 5), "dianping": (3, 5)}
_PLATFORM_WORDS = {
    "wechat": ("公众号", "微信"),
    "toutiao": ("头条",),
    "dianping": ("点评", "大众点评"),
}


def _tag_line_info(line: str) -> tuple[list[str], str]:
    """判断这行是不是「纯标签行」，返回 (标签列表, 平台名)；不是就返回 ([], "")。

    判据：至少 2 个 #话题，且把话题词抠掉后只剩标点/平台前缀——这样正文里
    「#3 号链接」这种带井号的句子不会被误删。
    """
    t = line.strip()
    if not t:
        return [], ""
    tags = _HASHTAG.findall(t)
    if len(tags) < 2:
        return [], ""
    rest = _HASHTAG.sub("", t)
    rest = re.sub(r"[\s，,、:：|/·\-—_【】\[\]（）()]+", "", rest)
    rest = re.sub(r"(公众号|微信|头条|点评|大众点评|小红书|视频号|抖音|微博|话题|标签|和|及|的)", "", rest)
    if len(rest) > 6:            # 抠完还剩一截说明是正文，不是标签行
        return [], ""
    platform = ""
    for pid, words in _PLATFORM_WORDS.items():
        if any(w in t for w in words):
            platform = pid
            break
    return tags, platform


def normalize_tags(markdown: str, platform: str = "wechat") -> str:
    """把文末的多组话题标签合并成**一组**，并挪到全文最底部（配图之后）。

    · 多块标签（公众号一块、头条一块）→ 合并去重，只留一行
    · 数量按当前平台上限截断，当前平台那块排前面
    · 正文里没有标签行就原样返回，不凭空造标签
    """
    md = markdown or ""
    lines = md.split("\n")
    platform = (platform or "wechat").strip() or "wechat"

    primary: list[str] = []      # 当前平台那块的标签（排前面）
    others: list[str] = []       # 无平台标注 / 别的平台那块
    kept: list[str] = []
    found = False
    for ln in lines:
        tags, tag_platform = _tag_line_info(ln)
        if not tags:
            kept.append(ln)
            continue
        found = True
        bucket = primary if tag_platform == platform else others
        for t in tags:
            if t not in bucket:
                bucket.append(t)
    if not found:
        return md

    merged = primary + [t for t in others if t not in primary]
    _, hi = TAG_COUNTS.get(platform, TAG_COUNTS["wechat"])
    merged = merged[:hi]

    while kept and not kept[-1].strip():      # 去掉尾部空行，标签贴在最底
        kept.pop()
    return "\n".join(kept + ["", " ".join("#" + t for t in merged)])


COMPRESS_PROMPT = """把下面这篇公众号文章压到 {target} 汉字左右（±80 字）。

要求：
- 事实、数据、引述、时间地点一个都不能丢，一个都不能改。
- 只删这些东西：重复的铺陈、同义反复、可有可无的过渡句、举例里的第二个例子。
- 保留 1-2 个小标题、5-8 处加粗，保持原来的判断链和语气。
- 保留末尾的互动提问和话题标签。
- 只输出压缩后的 Markdown 正文，不要解释，不要开头结尾客套。

原文：

{content}
"""


def compress(markdown: str, keep: int = 650) -> str:
    out = chat([
        {"role": "system", "content": "你是中文公众号编辑，负责把长稿压短且不丢信息。只输出 Markdown 正文。"},
        {"role": "user", "content": COMPRESS_PROMPT.format(target=keep, content=markdown)},
    ], temperature=0.5)
    out = out.strip()
    out = re.sub(r"^```(?:markdown)?\s*", "", out)
    out = re.sub(r"\s*```$", "", out)
    return out.strip()


def humanize(markdown: str) -> str:
    out = chat([
        {"role": "system", "content": "你是中文母语的公众号编辑，只输出改写后的 Markdown 正文。"},
        {"role": "user", "content": HUMANIZE_PROMPT.format(content=markdown)},
    ], temperature=0.8)
    return out.strip()


def suggest_titles(markdown: str, platform: str = "wechat") -> list[str]:
    rule = title_rule_for(platform)
    prompt = ("为下面这篇文章拟 6 个标题。\n\n规则：\n- " + rule
              + "\n\n文章：\n\n" + markdown[:4000]
              + "\n\n只输出 JSON 数组，不要解释：[\"标题1\", \"标题2\", ...]")
    out = chat([
        {"role": "system", "content": "你是标题编辑，只输出 JSON 数组。"},
        {"role": "user", "content": prompt},
    ], temperature=0.95)
    data = _extract_json(out)
    if isinstance(data, dict):
        data = data.get("titles") or []
    return [str(t).strip() for t in data if str(t).strip()][:8]


# --------------------------------------------------------------------------
# AI 排版优化
# --------------------------------------------------------------------------

def optimize_layout(markdown: str, account: dict | None = None) -> str:
    """只动 Markdown 结构（段落/小标题/加粗/清单），不动事实和语气。

    和 humanize 的区别：humanize 改的是**语言**（去 AI 腔），这个改的是**版式**。
    用途：内容定稿后，按平台阅读节奏重排，比如把公众号长段拆成头条的短段。
    """
    account = account or {}
    platform = account.get("platform") or "wechat"
    from .personas import PLATFORM_RULES

    rule = PLATFORM_RULES.get(platform, PLATFORM_RULES["wechat"])
    persona = (account.get("persona") or "").strip() or "（未设置，按通用自媒体处理）"
    h2_hint = "1-2 个" if platform == "wechat" else "2-3 个"
    bold_hint = "5-8" if platform == "wechat" else "3-6"
    out = chat([
        {"role": "system", "content": "你是排版编辑，只输出优化后的 Markdown 正文。"},
        {"role": "user", "content": LAYOUT_PROMPT.format(
            platform_label=rule["label"], persona=persona,
            platform_rule=rule["layout"] + "\n" + rule["writing"],
            h2_hint=h2_hint, bold_hint=bold_hint, content=markdown)},
    ], temperature=0.4)
    out = out.strip()
    out = re.sub(r"^```(?:markdown)?\s*", "", out)
    out = re.sub(r"\s*```$", "", out)
    return out.strip()


IMAGE_QUERY_PROMPT = """下面是一篇公众号文章的选题，请给我 2-3 个用于**搜索引擎图片搜索**的短关键词。

选题：{topic}

要求：
- 每个词 2-6 个汉字，是一个具体的东西（人名、作品名、事件、场所、物件），
  不要用抽象短语（"质量保证""方法差距"这种搜不到图）。
- 优先能直接搜到**这件事的现场画面**的词。
- 按重要性排序，第一个最重要。

只输出 JSON 数组：["关键词1", "关键词2"]
"""


def image_queries(topic: str) -> list[str]:
    """把长选题拆成 2-3 个适合图片搜索的短关键词。失败就返回空。"""
    try:
        out = chat([
            {"role": "system", "content": "你是搜索词专家，只输出 JSON 数组。"},
            {"role": "user", "content": IMAGE_QUERY_PROMPT.format(topic=topic[:120])},
        ], json_mode=True, temperature=0.4, timeout=60)
        data = _extract_json(out)
    except (LLMError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("queries") or data.get("keywords") or []
    out_words: list[str] = []
    for w in data if isinstance(data, list) else []:
        w = str(w).strip()
        if 2 <= len(w) <= 10 and w not in out_words:
            out_words.append(w)
    return out_words[:3]


IMAGE_SCENE_PROMPT = """一篇文章要配 {count} 张图，图由 AI 生图生成。你只负责写「画面里有什么」。

选题：{topic}

正文（可能被截断）：
{content}

要求：
1. 每条只写画面内容：拍什么、在哪、什么物件或动作、什么时刻。
   **不要写镜头、焦距、光线、色调、画质、风格**——那些由程序统一加，你写了会打架。
2. 每条对应正文里**不同的段落或小标题**，按正文出现顺序排，不要几张图都是同一个画面。
3. 一句中文，15-40 字，具体到能画出来：「便利店冷柜前伸手拿酸奶，玻璃门上有雾」比
   「都市生活的温度」强一百倍。不要形容词堆砌。
4. 画面里不要出现文字、logo、界面截图、数据图表、PPT。
5. 有人物时穿着日常得体、构图健康、适合公开发布；不要暴露、不要亲密或私房类画面。
6. 一共 {count} 条，宁少勿凑。

只输出 JSON 数组，元素是字符串：["画面描述1", "画面描述2"]
"""


def image_scenes(topic: str, markdown: str, count: int,
                 account: dict[str, Any] | None = None) -> list[str]:
    """给 AI 配图生成 count 条画面描述。失败时退化成「小标题 + 标题」，不会中断生成。"""
    count = max(1, min(int(count or 1), 8))
    content = (markdown or "")[:3000]
    data: Any = []
    try:
        out = chat([
            {"role": "system", "content": "你是摄影主题策划，只输出 JSON 数组。"},
            {"role": "user", "content": IMAGE_SCENE_PROMPT.format(
                count=count, topic=topic[:120], content=content)},
        ], json_mode=True, temperature=0.75, timeout=90)
        data = _extract_json(out)
    except (LLMError, ValueError):
        data = []
    if isinstance(data, dict):
        data = data.get("scenes") or data.get("items") or []
    scenes: list[str] = []
    for s in data if isinstance(data, list) else []:
        s = str(s).strip().strip("。")
        if 4 <= len(s) <= 90 and s not in scenes:
            scenes.append(s)
    scenes = scenes[:count]

    # 补齐：模型没给够就用小标题凑，别让配图数量对不上
    if len(scenes) < count:
        heads = [m.group(1).strip() for m in re.finditer(r"^#{1,3}\s+(.+)$", markdown or "", re.M)]
        pool = heads or [topic]
        i = 0
        while len(scenes) < count and i < count * 2:
            base = pool[i % len(pool)]
            cand = f"{topic}：{base}" if base and base != topic else topic
            if cand not in scenes:
                scenes.append(cand)
            i += 1
        while len(scenes) < count:
            scenes.append(topic)
    return scenes


IMAGE_RANK_PROMPT = """一篇文章的选题是：{topic}

下面是候选配图的「序号 | 标题 | 来源站 | 尺寸」。图我看不到，只能靠标题和来源判断，
你的任务是挑出 {want} 张**最可能真的是这个话题的画面**的图。

选题：{topic}

{candidates}

挑选标准：
- 标题说的必须是**这件事本身**（同一事件、同一主体、同一场景），
  而不是"沾边领域"的泛泛配图（比如讲某展会时给一张通用办公室图，就是错的）。
- 优先新闻媒体来源（中华网、凤凰、网易、澎湃、新浪、搜狐、人民网、新华网等），
  它们的图一般是现场图。
- 优先尺寸大的横向图。
- **避开纯数据类的图**：榜单截图、报表、表格、行情图、PPT 截图 —— 这类图放进正文很"素"，
  读者一眼就知道是凑数的。标题里带「排名/榜单/排行/报表/统计/指数/走势/评测/一览/名单」的多半是这种，
  除非这条数据本身就是文章的核心证据（例如文章就是在讲这个榜单），否则不要选。
- 优先有现场感的图：人物、场景、实物、新闻现场。
- 如果候选里根本没有相关图，就少挑几张，宁缺勿滥。

只输出 JSON 数组，元素是选中的序号（整数），按用于正文的先后顺序排列：
[3, 7, 1]
"""


def rank_images(topic: str, pool: list[dict[str, Any]], want: int) -> list[int]:
    """让模型按标题/来源把候选图筛一遍，返回选中的下标（0 基）。失败就返回空。"""
    want = max(1, int(want))
    lines = []
    for i, m in enumerate(pool[:24]):
        cap = (m.get("caption") or "").strip() or "（无标题）"
        lines.append(f"{i} | {cap} | {m.get('site') or '?'} | {m.get('width')}x{m.get('height')}")
    if not lines:
        return []
    try:
        out = chat([
            {"role": "system", "content": "你是公众号配图编辑，只输出 JSON 数组。"},
            {"role": "user", "content": IMAGE_RANK_PROMPT.format(
                topic=topic, want=want, candidates="\n".join(lines))},
        ], json_mode=True, temperature=0.3)
        data = _extract_json(out)
    except (LLMError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("picks") or data.get("indices") or []
    picks: list[int] = []
    for v in data if isinstance(data, list) else []:
        try:
            idx = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < len(pool) and idx not in picks:
            picks.append(idx)
    return picks[:want]


def test_llm() -> dict[str, Any]:
    cfg = llm_config()
    if not cfg.get("api_key"):
        return {"ok": False, "error": "未配置 API Key"}
    try:
        out = chat([{"role": "user", "content": "只回复两个字：可用"}],
                   temperature=0, timeout=40)
        return {"ok": True, "model": cfg.get("model"), "reply": out.strip()[:20]}
    except LLMError as exc:
        return {"ok": False, "error": str(exc)}
