"""微信公众号 API 封装。

只做需要的事：稳定 token、正文图上传、永久图片素材、草稿增删查、发布。
踩过的坑都写在注释里（IP 白名单、封面与正文图是两套接口、content 不支持 class/style 块）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import requests

from .settings import WX_API

_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_TOKEN_LOCK = threading.Lock()

ERR_HINTS = {
    40001: "AppSecret 不对，或 access_token 已失效（本程序会自动重取一次）",
    40013: "AppID 不合法",
    40125: "AppSecret 无效，请到公众号后台「设置与开发 → 基本配置」重新生成",
    40164: "调用方 IP 不在公众号 IP 白名单内。请把本服务器的公网 IP 加到"
           "「设置与开发 → 基本配置 → IP白名单」",
    45009: "接口调用超过频率限制，稍后重试",
    48001: "该接口未授权：这个公众号没有发布/相应接口权限（多数订阅号只能建草稿）",
    48002: "接口权限不足：需要「草稿箱 / 素材管理」权限",
    50002: "用户受限，非官方公众号",
    61024: "调用方不是第三方平台，接口不可用",
}


class WechatError(RuntimeError):
    def __init__(self, code: int, msg: str, endpoint: str = ""):
        self.code = code
        self.msg = msg
        self.endpoint = endpoint
        hint = ERR_HINTS.get(code, "")
        super().__init__(f"[{code}] {msg}" + (f" —— {hint}" if hint else ""))


def _parse_json(r) -> dict:
    """微信接口的 Content-Type 常常不带 charset，requests 会按 latin-1 解码，
    中文就成乱码。统一自己按 utf-8 解。
    """
    raw = r.content
    for enc in ("utf-8", "utf-8-sig", "gbk"):
        try:
            return json.loads(raw.decode(enc))
        except (UnicodeDecodeError, ValueError):
            continue
    raise WechatError(-1, f"接口返回无法解析: {raw[:200]!r}", "")


class WechatClient:
    def __init__(self, appid: str, secret: str, name: str = ""):
        self.appid = appid
        self.secret = secret
        self.name = name

    # ---------------- token ----------------

    def token(self, force: bool = False) -> str:
        key = self.appid
        if not force:
            with _TOKEN_LOCK:
                cached = _TOKEN_CACHE.get(key)
                if cached and cached[1] > time.time() + 120:
                    return cached[0]
        r = requests.post(
            f"{WX_API}/cgi-bin/stable_token",
            json={
                "grant_type": "client_credential",
                "appid": self.appid,
                "secret": self.secret,
                "force_refresh": bool(force),
            },
            timeout=30,
        )
        d = _parse_json(r)
        if "access_token" not in d:
            raise WechatError(int(d.get("errcode", -1)), d.get("errmsg", "获取 token 失败"),
                              "stable_token")
        tok = d["access_token"]
        with _TOKEN_LOCK:
            _TOKEN_CACHE[key] = (tok, time.time() + int(d.get("expires_in", 7200)))
        return tok

    def _invalidate(self) -> None:
        with _TOKEN_LOCK:
            _TOKEN_CACHE.pop(self.appid, None)

    def _json(self, method: str, path: str, *, params: dict | None = None,
              body: dict | None = None, retry: bool = True) -> dict:
        tok = self.token()
        p = {"access_token": tok}
        if params:
            p.update(params)
        url = f"{WX_API}{path}"
        qs = dict(p)
        if method == "POST" and path.startswith("/cgi-bin/draft"):
            # 老接口仍吃 query 里的 token，但也接受 body；统统放 query 最稳
            pass
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        r = requests.request(method, url, params=qs, data=data, headers=headers, timeout=120)
        d = _parse_json(r)
        code = int(d.get("errcode", 0) or 0)
        if code in (40001, 42001) and retry:
            self._invalidate()
            return self._json(method, path, params=params, body=body, retry=False)
        if code != 0:
            raise WechatError(code, d.get("errmsg", ""), path)
        return d

    def _post_file(self, path: str, params: dict, file_path: Path,
                   mime: str | None = None, form: dict | None = None) -> dict:
        tok = self.token()
        p = {"access_token": tok}
        p.update(params)
        mime = mime or _guess_mime(file_path)
        with open(file_path, "rb") as fh:
            r = requests.post(
                f"{WX_API}{path}",
                params=p,
                files={"media": (file_path.name, fh, mime)},
                data=form or {},
                timeout=300,
            )
        d = _parse_json(r)
        code = int(d.get("errcode", 0) or 0)
        if code in (40001, 42001):
            self._invalidate()
            return self._post_file(path, params, file_path, mime, form)
        if code != 0:
            raise WechatError(code, d.get("errmsg", ""), path)
        return d

    # ---------------- 素材 ----------------

    def upload_content_image(self, path: Path) -> str:
        """正文图：media/uploadimg → 返回 mmbiz.qpic.cn 的 url（不是 media_id）。"""
        d = self._post_file("/cgi-bin/media/uploadimg", {}, path)
        url = d.get("url") or ""
        if not url:
            raise WechatError(-1, f"上传正文图未返回 url: {d}", "media/uploadimg")
        return url

    def upload_permanent_image(self, path: Path) -> str:
        """封面：material/add_material?type=image → 返回永久素材 media_id（thumb_media_id）。"""
        d = self._post_file("/cgi-bin/material/add_material", {"type": "image"}, path)
        mid = d.get("media_id") or ""
        if not mid:
            raise WechatError(-1, f"上传封面素材未返回 media_id: {d}", "material/add_material")
        return mid

    def upload_video_material(self, path: Path, title: str, introduction: str = "") -> dict:
        """视频：material/add_material?type=video，description 必须是 form-data 字符串字段。"""
        desc = json.dumps({"title": title, "introduction": introduction}, ensure_ascii=False)
        return self._post_file(
            "/cgi-bin/material/add_material", {"type": "video"}, path,
            mime="video/mp4", form={"description": desc},
        )

    # ---------------- 草稿 ----------------

    def add_draft(self, article: dict[str, Any]) -> str:
        body = {"articles": [article]}
        last: dict = {}
        for attempt in range(1, 4):
            try:
                d = self._json("POST", "/cgi-bin/draft/add", body=body)
            except WechatError as exc:
                if exc.code == 45009 and attempt < 3:
                    time.sleep(3 * attempt)
                    continue
                raise
            mid = d.get("media_id")
            if mid:
                return mid
            last = d
            time.sleep(2 * attempt)
        raise WechatError(-1, f"创建草稿失败: {last}", "draft/add")

    def get_draft(self, media_id: str) -> list[dict]:
        d = self._json("POST", "/cgi-bin/draft/get", body={"media_id": media_id})
        return d.get("news_item") or []

    def batchget_drafts(self, offset: int = 0, count: int = 20, no_content: bool = True) -> dict:
        return self._json(
            "POST", "/cgi-bin/draft/batchget",
            body={"offset": offset, "count": count, "no_content": 1 if no_content else 0},
        )

    def delete_draft(self, media_id: str) -> dict:
        return self._json("POST", "/cgi-bin/draft/delete", body={"media_id": media_id})

    # ---------------- 发布 ----------------

    def publish(self, media_id: str) -> str:
        d = self._json("POST", "/cgi-bin/freepublish/submit", body={"media_id": media_id})
        pid = d.get("publish_id") or ""
        if not pid:
            raise WechatError(-1, f"发布未返回 publish_id: {d}", "freepublish/submit")
        return pid

    def publish_status(self, publish_id: str) -> dict:
        return self._json("POST", "/cgi-bin/freepublish/get", body={"publish_id": publish_id})

    # ---------------- 体检 ----------------

    def probe(self) -> dict:
        out: dict[str, Any] = {"appid": self.appid, "name": self.name, "ok": False}
        try:
            self.token(force=True)
            out["token"] = "ok"
        except WechatError as exc:
            out["error"] = str(exc)
            out["errcode"] = exc.code
            return out
        try:
            d = self.batchget_drafts(count=1, no_content=True)
            out["draft_total"] = d.get("total_count", 0)
        except WechatError as exc:
            out["draft_error"] = str(exc)
        out["ok"] = True
        return out


def _guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
    }.get(ext, "application/octet-stream")


def client_for(account: dict) -> WechatClient:
    from .security import decrypt_secret

    return WechatClient(account["appid"], decrypt_secret(account["secret_enc"]), account["name"])
