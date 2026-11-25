# 这个片段会被追加到 app/render.py 的 THEMES 定义之后（见 merge_extra_themes）。
# 领域主题只写和基础主题**不一样**的键，其余从 base 继承，避免每套主题抄一遍 26 个键。

EXTRA_THEMES = {
    "beauty": {
        "base": "warm",
        "label": "美妆 · 柔雾粉",
        "desc": "美妆/护肤，柔和粉底色，适合体验分享",
        "body": "font-size:16px;color:#4a3a3e;line-height:1.95;letter-spacing:.4px;"
                "word-break:break-word;text-align:justify;",
        "h2": "font-size:17px;font-weight:700;color:#8a3a55;margin:30px 0 16px;"
              "padding:8px 14px;background:#fdf0f4;border-radius:8px;line-height:1.4;",
        "h3": "font-size:16px;font-weight:700;color:#a34a66;margin:24px 0 12px;",
        "strong": "font-weight:700;color:#b23a5e;background:linear-gradient(transparent 65%,#ffe0ea 65%);",
        "blockquote": "margin:22px 0;padding:14px 16px;background:#fdf5f7;"
                      "border-left:3px solid #eeb9c9;color:#7a5a63;font-size:15px;line-height:1.85;",
        "caption": "font-size:12.5px;color:#b89aa4;text-align:center;margin:8px 0 24px;line-height:1.5;",
        "a": "color:#b23a5e;text-decoration:none;",
    },
    "fashion": {
        "base": "default",
        "label": "穿搭 · 极简灰",
        "desc": "穿搭/时尚，大留白、细线分割，突出图片",
        "body": "font-size:16px;color:#333;line-height:2.0;letter-spacing:.6px;"
                "word-break:break-word;text-align:left;",
        "h2": "font-size:16px;font-weight:600;color:#111;margin:36px 0 18px;"
              "padding:0 0 8px;border-bottom:1px solid #111;letter-spacing:1px;line-height:1.4;",
        "h3": "font-size:15px;font-weight:600;color:#555;margin:26px 0 12px;letter-spacing:.5px;",
        "strong": "font-weight:600;color:#000;border-bottom:1px solid #000;",
        "blockquote": "margin:26px 0;padding:16px 18px;background:#fafafa;"
                      "border-left:2px solid #ccc;color:#666;font-size:14px;line-height:1.9;",
        "img": "max-width:100%;height:auto;display:block;margin:0 auto;border-radius:0;",
        "caption": "font-size:12px;color:#aaa;text-align:center;margin:10px 0 28px;letter-spacing:.5px;",
        "a": "color:#111;text-decoration:underline;",
    },
    "food": {
        "base": "warm",
        "label": "美食 · 烟火橙",
        "desc": "探店/美食，暖色标题，读起来有食欲",
        "body": "font-size:16px;color:#453a30;line-height:1.95;letter-spacing:.4px;"
                "word-break:break-word;text-align:justify;",
        "h2": "font-size:17px;font-weight:700;color:#7a3a10;margin:30px 0 16px;"
              "padding:8px 14px;background:#fff2e2;border-radius:8px;line-height:1.4;",
        "h3": "font-size:16px;font-weight:700;color:#9a4a18;margin:24px 0 12px;",
        "strong": "font-weight:700;color:#c2601a;background:linear-gradient(transparent 65%,#ffe6c2 65%);",
        "blockquote": "margin:22px 0;padding:14px 16px;background:#fff8ef;"
                      "border-left:3px solid #f0c68a;color:#7a6248;font-size:15px;line-height:1.85;",
        "caption": "font-size:12.5px;color:#b09a80;text-align:center;margin:8px 0 24px;line-height:1.5;",
        "a": "color:#c2601a;text-decoration:none;",
    },
    "auto": {
        "base": "tech",
        "label": "汽车 · 深空蓝",
        "desc": "汽车/出行，冷静数据感，适合选车用车",
        "body": "font-size:16px;color:#2f3540;line-height:1.9;letter-spacing:.3px;"
                "word-break:break-word;text-align:justify;",
        "h2": "font-size:17px;font-weight:700;color:#0e2a4a;margin:32px 0 16px;"
              "padding:8px 14px;background:#eaf0f8;border-radius:6px;line-height:1.4;",
        "h3": "font-size:16px;font-weight:700;color:#1a4a7a;margin:24px 0 12px;",
        "strong": "font-weight:700;color:#155a9e;",
        "blockquote": "margin:22px 0;padding:14px 16px;background:#f4f7fb;"
                      "border-left:3px solid #8aa8cc;color:#4a5a6e;font-size:15px;line-height:1.85;",
        "caption": "font-size:12.5px;color:#8a9aae;text-align:center;margin:8px 0 24px;line-height:1.5;",
        "a": "color:#155a9e;text-decoration:none;",
    },
    "history": {
        "base": "elegant",
        "label": "地方历史 · 宣纸墨",
        "desc": "历史/人文，宣纸底、墨色标题，适合长文",
        "body": "font-size:16px;color:#3a342a;line-height:2.05;letter-spacing:.6px;"
                "word-break:break-word;text-align:justify;",
        "h2": "font-size:18px;font-weight:700;color:#2a241a;margin:36px 0 18px;"
              "padding:0 0 8px;border-bottom:2px solid #6a5a3a;line-height:1.5;letter-spacing:1px;",
        "h3": "font-size:16px;font-weight:700;color:#4a3f2a;margin:26px 0 12px;letter-spacing:.5px;",
        "strong": "font-weight:700;color:#7a5a1a;",
        "blockquote": "margin:26px 0;padding:16px 20px;background:#f7f2e8;"
                      "border-left:3px solid #b09a6a;color:#5a4f3a;font-size:15px;line-height:2.0;",
        "img": "max-width:100%;height:auto;display:block;margin:0 auto;border-radius:2px;",
        "caption": "font-size:12.5px;color:#9a8f7a;text-align:center;margin:10px 0 26px;line-height:1.6;",
        "a": "color:#7a5a1a;text-decoration:none;",
    },
    "finance": {
        "base": "tech",
        "label": "财经 · 墨绿金",
        "desc": "财经/商业，克制专业，数据清晰",
        "body": "font-size:16px;color:#2e3532;line-height:1.9;letter-spacing:.3px;"
                "word-break:break-word;text-align:justify;",
        "h2": "font-size:17px;font-weight:700;color:#123a2e;margin:32px 0 16px;"
              "padding:8px 14px;background:#eaf4ef;border-radius:6px;line-height:1.4;",
        "h3": "font-size:16px;font-weight:700;color:#1a5a42;margin:24px 0 12px;",
        "strong": "font-weight:700;color:#0f7a4a;",
        "blockquote": "margin:22px 0;padding:14px 16px;background:#f2f7f4;"
                      "border-left:3px solid #7ab09a;color:#425a4e;font-size:15px;line-height:1.85;",
        "caption": "font-size:12.5px;color:#8aa398;text-align:center;margin:8px 0 24px;line-height:1.5;",
        "a": "color:#0f7a4a;text-decoration:none;",
    },
    "travel": {
        "base": "default",
        "label": "旅行 · 晴空青",
        "desc": "旅行/攻略，清爽通透，图片优先",
        "body": "font-size:16px;color:#33424a;line-height:1.95;letter-spacing:.4px;"
                "word-break:break-word;text-align:justify;",
        "h2": "font-size:17px;font-weight:700;color:#0f4a5a;margin:32px 0 16px;"
              "padding:8px 14px;background:#e8f4f7;border-radius:8px;line-height:1.4;",
        "h3": "font-size:16px;font-weight:700;color:#1a6a7a;margin:24px 0 12px;",
        "strong": "font-weight:700;color:#0f7a8a;background:linear-gradient(transparent 65%,#d2f0f4 65%);",
        "blockquote": "margin:22px 0;padding:14px 16px;background:#f2f9fa;"
                      "border-left:3px solid #8ac4ce;color:#4a6a72;font-size:15px;line-height:1.85;",
        "caption": "font-size:12.5px;color:#8aabb2;text-align:center;margin:8px 0 24px;line-height:1.5;",
        "a": "color:#0f7a8a;text-decoration:none;",
    },
    "magazine": {
        "base": "default",
        "label": "杂志 · 大标题",
        "desc": "通用杂志感，大标题、宽行距，适合深度长文",
        "body": "font-size:17px;color:#222;line-height:2.1;letter-spacing:.5px;"
                "word-break:break-word;text-align:justify;",
        "h2": "font-size:20px;font-weight:800;color:#000;margin:40px 0 20px;line-height:1.4;",
        "h3": "font-size:17px;font-weight:700;color:#333;margin:28px 0 14px;",
        "strong": "font-weight:700;color:#000;background:linear-gradient(transparent 60%,#ffe9a8 60%);",
        "blockquote": "margin:28px 0;padding:20px 24px;background:#f8f8f8;"
                      "border-left:4px solid #000;color:#444;font-size:16px;line-height:2.0;",
        "caption": "font-size:12.5px;color:#999;text-align:center;margin:10px 0 28px;line-height:1.6;",
    },
    "minimal": {
        "base": "default",
        "label": "极简 · 无装饰",
        "desc": "几乎无装饰，纯文字流，适合观点输出",
        "body": "font-size:16px;color:#333;line-height:2.0;letter-spacing:.4px;"
                "word-break:break-word;text-align:justify;",
        "h2": "font-size:17px;font-weight:700;color:#111;margin:34px 0 16px;line-height:1.4;",
        "h3": "font-size:16px;font-weight:700;color:#333;margin:26px 0 12px;",
        "strong": "font-weight:700;color:#000;",
        "blockquote": "margin:24px 0;padding:4px 0 4px 16px;"
                      "border-left:2px solid #ddd;color:#666;font-size:15px;line-height:1.9;",
        "caption": "font-size:12px;color:#aaa;text-align:center;margin:8px 0 24px;line-height:1.5;",
    },
}


def merge_extra_themes(themes: dict) -> dict:
    """把 EXTRA_THEMES 合进 THEMES：base 先复制，再覆盖差异键。"""
    for key, extra in EXTRA_THEMES.items():
        base_key = extra.get("base") or "default"
        base = themes.get(base_key) or themes["default"]
        merged = dict(base)
        merged.update({k: v for k, v in extra.items() if k != "base"})
        themes[key] = merged
    return themes
