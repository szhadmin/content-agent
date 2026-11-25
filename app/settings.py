"""全局配置与路径。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("WXWB_DATA") or (BASE_DIR / "data")).resolve()
UPLOAD_DIR = DATA_DIR / "uploads"
COVER_DIR = DATA_DIR / "covers"
ARTICLE_DIR = DATA_DIR / "articles"
LOG_DIR = DATA_DIR / "logs"
for _d in (DATA_DIR, UPLOAD_DIR, COVER_DIR, ARTICLE_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "workbench.db"
SECRET_KEY_PATH = DATA_DIR / "secret.key"
SESSION_COOKIE = "wxwb_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600

APP_NAME = "公众号 Agent 工作台"

# 子路径部署前缀（例如反代把 /wxwb/* 转发到本应用时设成 /wxwb）。
# 反代必须把前缀剥掉再转发，路由本身不感知前缀。
URL_PREFIX = (os.environ.get("WXWB_PREFIX", "") or "").rstrip("/")

DEFAULT_SLOTS = ("morning", "noon", "evening", "extra")
SLOT_LABELS = {
    "morning": "早报",
    "noon": "午报",
    "evening": "晚报",
    "extra": "专题",
}

# 内容平台。账号挂在平台上，稿件跟着账号走；两边的排版和投递方式完全不同，
# 所以从数据层就分开，不在一个流程里混着判断。
PLATFORMS: dict[str, dict[str, str]] = {
    "wechat": {
        "label": "公众号",
        "short": "公众号",
        "how": "接口投递：建草稿走 draft/add，正文图和封面两套接口",
        "status": "ready",
    },
    "toutiao": {
        "label": "头条号",
        "short": "头条",
        "how": "浏览器投递：扫码登录一次，之后无头浏览器填发文页，只存草稿",
        "status": "ready",
    },
    "dianping": {
        "label": "大众点评",
        "short": "点评",
        "how": "未接入：点评没有对外发文能力，需要单独做后台自动化",
        "status": "todo",
    },
}
PLATFORM_LABELS = {k: v["label"] for k, v in PLATFORMS.items()}
DEFAULT_PLATFORM = "wechat"

# 微信接口（服务器出口 IP 必须在该公众号的 IP 白名单内）
WX_API = "https://api.weixin.qq.com"

# DeepSeek 默认
DEFAULT_LLM = {
    "base_url": "https://api.deepseek.com",
    "model": "deepseek-chat",
    "api_key": "",
    "temperature": 0.9,
}


def public_base_url() -> str:
    return os.environ.get("WXWB_PUBLIC_URL", "").rstrip("/")
