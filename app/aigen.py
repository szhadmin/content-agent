"""AI 生图（配图/封面都能用）。

设计说明
--------
服务器上没有现成的 SD/ComfyUI，写作走的是 DeepSeek（纯文本，不生图）。所以这里做一个
**可配置的生图后端**，和 writer.py 的 LLM 配置完全分开——文本模型和图像模型本来就不是一家。

内置三种 OpenAI 兼容形态：
  · `images`     POST {base}/images/generations        —— 智谱 CogView、OpenAI、SiliconFlow 等
  · `dashscope`  POST {base}/services/aigc/text2image/image-synthesis
                 （阿里云百炼 通义万相，带 X-DashScope-Async: enable 异步轮询）
  · `sd`         POST {base}/sdapi/v1/txt2img          —— 本地 Stable Diffusion WebUI

默认给智谱 CogView（`https://open.bigmodel.cn/api/paas/v4`，模型 `cogview-3-flash` 免费、
`cogview-3-plus` 收费但质量高），因为它和 DeepSeek 一样是国内直连、一个 key 就能用。

**关于「穿搭/美女图」**：真人脸/真人穿搭图各家都有内容安全限制，效果也最容易翻车。
这里默认走「不露脸/氛围感/平铺/街拍剪影」的提示词路线（见 build_prompt），
既规避审核，也更适合当正文配图。要真人脸得自己在提示词里写，后果自负。

**内容过滤开关（nsfw）**：设置页「AI 生图 → 内容过滤」开着时，请求体多带一个
`"nsfw": true`（中转站认这个字段，官方原生接口会忽略），同时人物出图的服装措辞
换成宽松版（`_dress_clause`）；关掉就是原来的保守写法，请求体一个多余字段都没有。

**提示词怎么写得「像照片」**：全部交给 photo.py。那边按焦距/光圈、光的方向和色温、
可数的质感细节、不完美构图和「动作进行中」来拼提示词，并且自带一组反 AI 味的负向词。
这个文件只负责「把什么场景交给它」，不再自己堆形容词。
"""
from __future__ import annotations

import base64
import json
import re
import time
from pathlib import Path
from typing import Any

import requests
from PIL import Image

from . import characters, db, photo
from .settings import UPLOAD_DIR, public_base_url

DEFAULT_IMAGE_CFG = {
    "provider": "images",                       # images | dashscope | sd
    "base_url": "https://open.bigmodel.cn/api/paas/v4",
    "model": "cogview-3-flash",
    "api_key": "",
    # 参考图（人物一致性）用的模型，和上面的文生图模型分开配：
    #   dashscope → 通义万相图像编辑 wanx2.1-imageedit
    #   images    → gpt-image-1 / Seeddream 这类支持 /images/edits 的
    "ref_model": "",
    # 内容过滤开关：请求体里带 `"nsfw": true`。中转站（one-api / new-api 系）认这个字段，
    # 用来放开被上游内容安全拦掉的写实提示词；官方原生接口不认，多余字段会被忽略。
    # 配成 false 就彻底不发这个字段，等于走上游默认审核。
    "nsfw": True,
}

# 按人设预设给一点「画面感」提示，别让美妆号生出科技感的图
_SCENE_HINTS = {
    "beauty": "化妆品平铺、柔和自然光、浅粉/米白背景、干净高级的静物摄影",
    "fashion": "服装店陈列、街拍氛围、布料质感特写、浅景深、不露脸",
    "food": "食物特写、热气、木质桌面、暖色自然光、诱人色泽",
    "tech": "极简产品摄影、蓝白冷色、几何构图、科技感",
    "car": "汽车外观/内饰、城市道路、金属光泽、冷色调",
    "history": "老街、古建筑、旧物件、暖黄复古色调、胶片质感",
    "finance": "城市天际线、商务场景、简洁图表感、冷色调",
    "travel": "自然风景、目的地街景、通透天空、明亮色彩",
    "life": "生活场景、咖啡、书桌、窗边自然光、温暖色调",
}


def image_config() -> dict[str, Any]:
    cfg = dict(DEFAULT_IMAGE_CFG)
    cfg.update(db.get_setting("image_llm", {}) or {})
    key = cfg.get("api_key") or ""
    if key.startswith("gAAAA"):
        from .security import decrypt_secret
        try:
            key = decrypt_secret(key)
        except Exception:  # noqa: BLE001
            pass
    cfg["api_key"] = key
    return cfg


def available() -> tuple[bool, str]:
    cfg = image_config()
    if not cfg.get("api_key") and cfg.get("provider") != "sd":
        return False, "还没配生图服务：到「设置 → AI 生图」填一个（智谱 CogView 有免费额度）"
    return True, ""


def nsfw_on(cfg: dict[str, Any] | None = None) -> bool:
    """当前配置要不要在请求体里带 `nsfw: true`。设置页「内容过滤」存的就是它。"""
    cfg = cfg or image_config()
    v = cfg.get("nsfw")
    if isinstance(v, str):
        return v.strip().lower() not in ("", "0", "false", "off", "no")
    return bool(v)


def _nsfw_params(cfg: dict[str, Any]) -> dict[str, Any]:
    """JSON 请求体里要合并的额外参数。关掉时返回空 dict，请求体和以前完全一样。"""
    return {"nsfw": True} if nsfw_on(cfg) else {}


# 服装/尺度措辞跟着 nsfw 开关走：
#   关 → 按平台能发的写（公众号/头条对暴露、软色情会限流甚至封号）
#   开 → 不主动加衣服也不主动脱，交给场景和参考图决定
_DRESS_SAFE = ("人物穿着日常得体的服装（通勤装/休闲装），不要暴露，不要低胸，"
               "构图健康、适合公开发布。")
_DRESS_OPEN = ("人物服装按场景和参考图里的风格自然决定（通勤、居家、运动、泳装、贴身衣物都行），"
               "以真实照片的穿着质感为准；不要为了遮挡额外加衣服，也不要刻意摆成影楼写真，"
               "露肤程度跟着场景走。")


def _dress_clause(cfg: dict[str, Any]) -> str:
    """当前该用哪套服装措辞。设置页的「内容过滤」开关就是它。"""
    return _DRESS_OPEN if nsfw_on(cfg) else _DRESS_SAFE


def _scene_hint(account: dict | None) -> str:
    """按账号的领域关键词猜画面风格。预设 id 没存进账号，就用领域词模糊匹配。"""
    domain = ((account or {}).get("topic_domain") or "") + ((account or {}).get("persona") or "")
    for key, hint in _SCENE_HINTS.items():
        words = {
            "beauty": ["美妆", "护肤", "彩妆"],
            "fashion": ["穿搭", "搭配", "时尚"],
            "food": ["美食", "探店", "小吃"],
            "tech": ["科技", "AI", "数码", "手机"],
            "car": ["汽车", "新能源", "电动车"],
            "history": ["历史", "城市", "古镇", "老街"],
            "finance": ["财经", "经济", "股市"],
            "travel": ["旅行", "旅游", "攻略"],
            "life": ["职场", "生活", "情绪"],
        }[key]
        if any(w in domain for w in words):
            return hint
    return "简洁构图、自然光、真实感、适合当文章配图"


def photo_style() -> str:
    """当前选的写实风格（photo.STYLES 的 key）。设置页「生图风格」存的就是它。"""
    v = str(db.get_setting("photo_style", "") or "").strip()
    return v if v in photo.STYLES else "auto"


def _next_plan_index() -> int:
    """出图计数器：每生成一张 +1，用来轮换焦距/光线/构图。

    为什么不用随机：随机的话同一篇稿子重生成两次会拿到完全不同的光，排查问题时无法复现；
    而且「随机」在小词库里经常连着撞同一个值，看着还是像复制粘贴。自增取模必定轮换。
    """
    n = int(db.get_setting("photo_plan_idx", 0) or 0)
    try:
        db.set_setting("photo_plan_idx", (n + 1) % 100000)
    except Exception:  # noqa: BLE001
        pass
    return n


def build_prompt(topic: str, account: dict | None = None, *, kind: str = "inline",
                 extra: str = "", character: dict | None = None,
                 plan_idx: int | None = None, expression: str = "") -> str:
    """把选题翻成生图提示词。

    刻意走「无脸/氛围感」路线：一是各家对真人脸审核都严，二是文章配图本来就不需要
    一张具体的脸。穿搭/美妆要的是**画面氛围**，不是某个真人。

    具体的摄影参数（焦距和光圈、光的方向、质感、不完美构图）由 photo.py 给，
    这里只决定「画面里有什么」。`expression` 用来覆盖 photo 里的通用表情——
    走机位表的时候，表情是和机位/动作一起挑的，不能被通用表情顶掉。
    """
    scene = (character or {}).get("scene_hint") or _scene_hint(account)
    ratio = "横版 16:9" if kind == "cover" else "横版 3:2"
    portrait = bool(character)
    idx = _next_plan_index() if plan_idx is None else int(plan_idx)

    p = photo.plan(idx, portrait=portrait, style=photo_style(),
                   look=(character or {}).get("appearance") or "",
                   scene_hint=scene)
    if expression:
        p["expression"] = expression
    parts = [
        f"{ratio}的写实照片。画面内容：{topic}。",
        photo.prompt_block(p, portrait=portrait),
    ]
    if character:
        # 绑了虚拟人物：要的是"同一个人的不同场景"。
        # 文本模型没有参考图能力时，外形描述是唯一能保住长相的抓手。
        parts.append("人物自然地出现在画面里，动作不要摆好等快门。")
    else:
        parts.append("画面主体明确，一眼能看出拍的是什么。")
    if extra:
        parts.append(f"额外要求：{extra}")
    parts.append(photo.negative_clause(portrait=portrait))
    return "\n".join(parts)


# --------------------------------------------------------------------------
# 人物参考（同一个虚拟人物的多场景一致性）
# --------------------------------------------------------------------------

def _local_path(media_url: str) -> Path | None:
    """/media/uploads/x.jpg → 磁盘路径。"""
    name = (media_url or "").rsplit("/", 1)[-1]
    if not name:
        return None
    p = UPLOAD_DIR / name
    return p if p.exists() else None


def _abs_url(media_url: str) -> str:
    """拼成公网可访问的绝对地址（万相要求 base_image_url 是公网 URL）。"""
    base = public_base_url() or str(db.get_setting("public_base", "") or "").rstrip("/")
    if not base:
        raise RuntimeError(
            "用参考图生成需要「对外访问地址」：到「设置 → AI 生图」填一下本站公网地址"
            "（例如 https://your-domain.com/wxwb），因为生图服务得能下载到你的参考图。")
    return f"{base}{media_url}"


def reference_ready(cfg: dict[str, Any] | None = None) -> tuple[bool, str]:
    """当前生图后端能不能吃参考图。返回 (支持吗, 不支持的原因)。

    看的是 `ref_model`（参考图专用模型）优先，没有再退回主模型——
    文生图和"图生图"往往不是同一个模型名。
    """
    cfg = cfg or image_config()
    provider = cfg.get("provider") or "images"
    ref_model = (cfg.get("ref_model") or "").strip().lower()
    model = ref_model or (cfg.get("model") or "").lower()
    if provider == "dashscope":
        return True, ""
    if provider == "sd":
        return True, ""          # img2img，一致性一般，但能用
    if provider == "images":
        if model.startswith("gpt-image") or "seedream" in model or "seededit" in model \
                or "kontext" in model:
            return True, ""
        return False, (f"当前生图模型「{model or '未填'}」不支持参考图。要人物一致，"
                       "请在下面「参考图模型」里填一个支持图生图的模型"
                       "（比如 gpt-image-1 / gpt-image-2 / seedream），"
                       "或把生图服务换成阿里云百炼（通义万相图像编辑）。")
    return False, "当前生图后端不支持参考图"


def _dashscope_reference(cfg: dict[str, Any], base: str, prompt: str,
                         refs: list[str]) -> bytes:
    """通义万相「图像编辑」：一张底图 + 一句指令，保持人、换场景。走异步轮询。"""
    model = cfg.get("ref_model") or "wanx2.1-imageedit"
    d = _post(f"{base}/services/aigc/image2image/image-synthesis", cfg, {
        "model": model,
        "input": {
            "function": "description_edit",
            "prompt": prompt,
            "base_image_url": _abs_url(refs[0]),
        },
        "parameters": {"n": 1},
    }, headers={"X-DashScope-Async": "enable"})
    task_id = (d.get("output") or {}).get("task_id")
    if not task_id:
        return _extract_image(d)
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(3)
        r = requests.get(f"{base}/tasks/{task_id}",
                         headers={"Authorization": f"Bearer {cfg['api_key']}"}, timeout=30)
        dd = r.json()
        out = dd.get("output") or {}
        status = out.get("task_status")
        if status == "SUCCEEDED":
            return _extract_image(dd)
        if status in ("FAILED", "CANCELED"):
            msg = out.get("message") or json.dumps(dd, ensure_ascii=False)[:200]
            raise RuntimeError(f"万相图像编辑失败：{msg}")
    raise RuntimeError("万相图像编辑超时（180s）")


def _combo_cache() -> dict[str, Any]:
    v = db.get_setting("img_edits_combo", {}) or {}
    return v if isinstance(v, dict) else {}


def _remember_combo(model: str, field: str, size: str) -> None:
    """记住哪个「字段名 + 尺寸」组合能用，下次直接命中。

    为什么必须记：/images/edits 每家字段名和尺寸支持都不一样，试错一次要 45~50 秒。
    不缓存的话每次生成都要先撞几次错的，一张图从 45 秒变成两三分钟。
    """
    c = _combo_cache()
    c[model] = [field, size]
    try:
        db.set_setting("img_edits_combo", c)
    except Exception:  # noqa: BLE001
        pass


def _openai_reference(cfg: dict[str, Any], base: str, prompt: str,
                      refs: list[str], *, size: str = "1536x1024") -> bytes:
    """OpenAI 兼容的 /images/edits：multipart 传参考图（gpt-image-1 / gpt-image-2 等）。

    两个实测差异，所以组合着试并**把成功的组合记下来**：
      · 字段名：有 `image` 也有 `image[]`（同一中转站上 gpt-image-2 要 `image`）
      · 尺寸：有的模型只吃 `1024x1024`，传横版直接 502
    每个失败组合约 45~50 秒，所以顺序很重要：先缓存，再按模型名猜，最后兜底。
    """
    model = cfg.get("ref_model") or cfg.get("model") or ""

    combos: list[tuple[str, str]] = []
    cached = _combo_cache().get(model)
    if isinstance(cached, (list, tuple)) and len(cached) == 2:
        combos.append((str(cached[0]), str(cached[1])))
    # 模型名里带 2 的（gpt-image-2 / seedream 2）多数吃 image + 横版；带 1 的多半只吃方图
    prefer = [("image", size), ("image", "1024x1024"),
              ("image[]", size), ("image[]", "1024x1024")]
    for combo in prefer:
        if combo not in combos:
            combos.append(combo)

    last_err = ""
    for field, sz in combos:
        files, opened = [], []
        try:
            for u in refs[:3]:
                p = _local_path(u)
                if p is None:
                    continue
                fh = open(p, "rb")
                opened.append(fh)
                files.append((field, (p.name, fh, "image/jpeg")))
            if not files:
                raise RuntimeError("参考图文件都找不到了，重新上传一下")
            r = requests.post(
                f"{base}/images/edits",
                headers={"Authorization": f"Bearer {cfg['api_key']}"},
                timeout=120,
                data={"model": model, "prompt": prompt, "n": "1", "size": sz,
                      **({"nsfw": "true"} if nsfw_on(cfg) else {})},
                files=files)
            if r.status_code == 402:
                # 账户积分/余额不足：换字段名也没用，直接报清楚，别白等四次
                raise RuntimeError(
                    f"生图服务余额不足，需要充值（{r.text[:120]}）。"
                    "这不是代码问题——到服务商后台充值后重试即可。")
            if r.status_code in (401, 403):
                raise RuntimeError(f"生图服务拒绝访问（{r.status_code}），检查 API Key 是否有效：{r.text[:120]}")
            if r.status_code == 429:
                raise RuntimeError(f"生图服务限流了（429），等一会儿再试：{r.text[:120]}")
            if r.status_code >= 400:
                last_err = f"参考图生图接口 {r.status_code}：{r.text[:160]}"
                continue
            _remember_combo(model, field, sz)
            return _extract_image(r.json())
        except RuntimeError:
            raise
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {str(exc)[:140]}"
        finally:
            for fh in opened:
                try:
                    fh.close()
                except Exception:  # noqa: BLE001
                    pass
    raise RuntimeError(f"参考图生图失败（已试 {len(combos)} 种组合）：{last_err}")


def _sd_reference(cfg: dict[str, Any], base: str, prompt: str,
                  refs: list[str]) -> bytes:
    """SD WebUI 的 img2img：底图 = 参考照，denoising 调低保留人物轮廓。"""
    p = _local_path(refs[0])
    if p is None:
        raise RuntimeError("参考图文件找不到了，重新上传一下")
    b64 = base64.b64encode(p.read_bytes()).decode()
    d = _post(f"{base}/sdapi/v1/img2img", cfg, {
        "prompt": prompt,
        "negative_prompt": ", ".join(photo.negative_terms(portrait=True)),
        "init_images": [b64],
        "denoising_strength": 0.55,
        "steps": 28, "width": 1024, "height": 576, "n_iter": 1,
        **_nsfw_params(cfg),
    })
    imgs = d.get("images") or []
    if not imgs:
        raise RuntimeError("SD 没返回图片（检查是否装了 img2img）")
    return base64.b64decode(imgs[0])


def generate_scene(character: dict[str, Any], scene: str, *,
                   account: dict | None = None, kind: str = "inline",
                   style: str = "", shot_index: int | None = None,
                   plan_idx: int | None = None,
                   return_meta: bool = False) -> Any:
    """按人物参考生成「这个人在某个场景」的图，返回 /media/uploads/xxx.png。

    只锁**长相**（五官/脸型/发型/肤色），机位、动作、表情每次轮换——
    否则模型会一直给同一张正面大头照，做人物号毫无意义。

    有参考图且后端支持 → 下"编辑指令"，人不变、换场景换机位；
    否则退回纯文字描述（外形描述拼进提示词），并如实告诉调用方降级了。

    `return_meta=True` 时返回 {url, shot, action, expression, used_reference}，
    给前端显示"这次用的是什么角度"。
    """
    ok, msg = available()
    if not ok:
        raise RuntimeError(msg)
    cfg = image_config()
    base = (cfg.get("base_url") or "").rstrip("/")
    refs = list(character.get("ref_images") or [])
    can_ref, _why = reference_ready(cfg)
    if plan_idx is None:
        plan_idx = _next_plan_index()

    if refs and can_ref:
        if shot_index is not None:
            shot, action, expr = _SHOTS[int(shot_index) % len(_SHOTS)]
        else:
            shot, action, expr = _next_shot(character)
        look = character.get("appearance") or ""
        st = style or character.get("scene_hint") or ""
        # 摄影参数单独轮换：机位表管「从哪拍、人在干嘛」，photo 管「这光怎么来的、什么镜头」
        p = photo.plan(plan_idx, portrait=True, style=photo_style(),
                       look=look, scene_hint=st)
        # 表情用机位表里配好的那条：它和机位/动作是一起挑的，photo 里的通用表情会让
        # 「背着包往前走」配上「无奈叹气」这种别扭组合。
        p.pop("expression", None)
        prompt = (
            "以参考图中的人物为原型，**只保持脸部特征一致**"
            "（五官、脸型、眼睛、鼻子、嘴型、发型、发色、肤色），"
            "是同一个人；但服装、姿态、机位、表情都要按下面的要求来，"
            "不要照搬参考图的姿势和构图。\n"
            f"机位：{shot}。\n"
            f"动作：{action}。\n"
            f"表情：{expr}。\n"
            f"场景：{scene}。\n"
            + photo.prompt_block(p, portrait=True) + "\n"
            # 明确禁止模型偷懒回到"证件照"构图
            + "避免：正面大头照、证件照式构图、正对镜头摆拍、影楼写真感、重复上一张的角度。\n"
            # 服装/尺度：开关关着就是平台能发的写法，开着交给场景（见 _dress_clause）
            + _dress_clause(cfg) + "\n"
            + photo.negative_clause(portrait=True)
        )
        provider = cfg.get("provider")
        if provider == "dashscope":
            raw = _dashscope_reference(cfg, base, prompt, refs)
        elif provider == "sd":
            raw = _sd_reference(cfg, base, prompt, refs)
        else:
            # 文章配图 3:2，别生成方图
            raw = _openai_reference(cfg, base, prompt, refs, size="1536x1024")
        url = _save_image(raw, prefix="char")
        if return_meta:
            return {"url": url, "shot": shot, "action": action, "expression": expr,
                    "used_reference": True}
        return url

    # 降级：没有参考图 / 后端不支持参考图
    if shot_index is None:
        shot, action, expr = _next_shot(character)
    else:
        shot, action, expr = _SHOTS[int(shot_index) % len(_SHOTS)]
    prompt = build_prompt(scene, account, kind=kind, character=character,
                          expression=expr, plan_idx=plan_idx,
                          extra=f"画面里要出现这个人。机位：{shot}。动作：{action}。"
                                "不要正面大头照，不要证件照构图。")
    url = _text_image(cfg, prompt, prefix="char", portrait=True)
    if return_meta:
        return {"url": url, "shot": shot, "action": action, "expression": expr,
                "used_reference": False}
    return url


def _save_image(raw: bytes, prefix: str = "ai") -> str:
    """统一校验 + 缩到 1600 宽 + 存 uploads，返回 /media/uploads/xxx.png。"""
    im = Image.open(__import__("io").BytesIO(raw)).convert("RGB")
    if im.width > 1600:
        im = im.resize((1600, int(im.height * 1600 / im.width)), Image.LANCZOS)
    name = f"{prefix}_{int(time.time())}_{__import__('secrets').token_hex(3)}.png"
    path = UPLOAD_DIR / name
    im.save(path, "PNG", optimize=True)
    return f"/media/uploads/{name}"


# --------------------------------------------------------------------------
# 镜头 / 动作 / 表情池（人物参考出图用）
# --------------------------------------------------------------------------
# 只锁**长相**（五官、脸型、发型、肤色），机位、动作、表情每次都要换。
# 不换的话模型会一直吐同一张"正面大头照"，做人物号就跟复读机一样。
# 结构：(机位, 动作, 表情)
_SHOTS: list[tuple[str, str, str]] = [
    ("全身远景，人物在画面中偏小，环境占主导", "正走进画面，迈步中", "自然放松"),
    ("半身中景，镜头在人物侧后方约 45 度", "低头看手机", "专注"),
    ("过肩镜头，从人物左后方越过肩膀拍向前方", "背着包往前走", "看不到表情"),
    ("俯拍视角，镜头从上方斜下", "坐在台阶上微微抬头", "若有所思"),
    ("仰拍视角，镜头从下往上约 30 度", "站着整理袖口", "平静"),
    ("平视，人物位于画面右侧三分线，留白在左", "靠在墙边捧着杯子", "放松"),
    ("侧面 3/4 脸近景，不正对镜头", "转头看向窗外", "出神"),
    ("全身，人物背对镜头走进远处", "手里拎着袋子", "看不到表情"),
    ("中景，镜头略高于人物视线", "弯腰系鞋带", "专注"),
    ("广角环境照，人物站在画面一角", "站在地铁站台等车", "有些疲惫"),
    ("半身，镜头从低处向上", "抬手挡了一下阳光", "微微眯眼"),
    ("抓拍，人物动作进行中、没有看镜头", "伸手接过递来的东西", "自然"),
    ("中近景，从门后/窗框外向内拍（有前景遮挡）", "正在低头写东西", "认真"),
    ("平视全身，走在人群里被前景路人半遮", "边走边回头说话", "带笑"),
    ("中景俯拍桌面视角，人物低头", "双手捧着碗吃东西", "满足"),
    ("全身，逆光剪影感，人物侧身站立", "抬手拨头发", "松弛"),
    ("半身侧后，人物正把外套脱下一半", "动作进行中", "放松"),
    ("中景，镜头与人物同高但从侧方平移跟拍", "快步过马路", "匆忙"),
]

_EXPRESSIONS = ["自然微笑", "平静无表情", "若有所思", "有点困", "放松地笑开", "认真专注",
                "惊讶", "无奈叹气", "心情不错", "发呆"]
# 注：表情目前由 _SHOTS 里每条的第三项决定（挑过，和机位/动作配套）。
# _EXPRESSIONS 留作以后做"自定义表情"功能时用。


def _next_shot(character: dict[str, Any]) -> tuple[str, str, str]:
    """按人物轮换机位，保证连续出图不会重复同一构图。

    每条机位都自带一个贴合它的表情（已经挑过，不会出现"背着包往前走还无奈叹气"
    这种别扭组合），变化靠 18 条机位轮换来保证。
    """
    cid = str(character.get("id") or 0)
    idx_map = db.get_setting("char_shot_idx", {}) or {}
    if not isinstance(idx_map, dict):
        idx_map = {}
    i = int(idx_map.get(cid, 0) or 0) % len(_SHOTS)
    idx_map[cid] = (i + 1) % len(_SHOTS)
    try:
        db.set_setting("char_shot_idx", idx_map)
    except Exception:  # noqa: BLE001
        pass
    return _SHOTS[i]


def _status_error(code: int, body: str) -> str:
    """把生图服务的错误码翻成人话。402=余额不足、401/403=Key 无效、429=限流。"""
    body = (body or "")[:160]
    if code == 402:
        return f"生图服务余额不足，需要充值（{body}）。这不是代码问题，去服务商后台充值后重试即可。"
    if code in (401, 403):
        return f"生图服务拒绝访问（{code}），检查 API Key 是否有效（{body}）"
    if code == 429:
        return f"生图服务限流了（429），等一会儿再试（{body}）"
    return f"生图接口 {code}：{body}"


def _post(url: str, cfg: dict[str, Any], payload: dict, headers: dict | None = None) -> dict:
    h = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        h["Authorization"] = f"Bearer {cfg['api_key']}"
    if headers:
        h.update(headers)
    try:
        r = requests.post(url, headers=h, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                          timeout=180)
    except requests.RequestException as exc:
        raise RuntimeError(f"生图服务连不上：{exc}") from exc
    if r.status_code >= 400:
        raise RuntimeError(_status_error(r.status_code, r.text))
    try:
        return r.json()
    except ValueError as exc:
        raise RuntimeError(f"生图接口返回不是 JSON：{r.text[:200]}") from exc


def _extract_image(d: dict) -> bytes:
    """从各家返回里把图片字节抠出来：b64_json / url 两种都接。"""
    for item in d.get("data") or []:
        if item.get("b64_json"):
            return base64.b64decode(item["b64_json"])
        if item.get("url"):
            r = requests.get(item["url"], timeout=60)
            r.raise_for_status()
            return r.content
    # 通义万相异步结果
    out = (d.get("output") or {})
    for item in out.get("results") or []:
        if item.get("url"):
            r = requests.get(item["url"], timeout=60)
            r.raise_for_status()
            return r.content
    if out.get("task_id"):
        raise RuntimeError(f"异步任务还没好：{out['task_id']}")
    raise RuntimeError(f"生图接口没返回图片：{json.dumps(d, ensure_ascii=False)[:300]}")


def _text_image(cfg: dict[str, Any], prompt: str, *, prefix: str = "ai",
                portrait: bool = False) -> str:
    """纯文字生图（三种后端）。人物一致性做不到，只能靠提示词描述长相。

    SD 有独立的 negative_prompt 字段，这里直接把 photo 的负向词表接上去——
    原来只写了 "text, watermark, logo, lowres, deformed" 五个词，压不住塑料感。
    """
    provider = cfg.get("provider") or "images"
    base = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model") or ""

    if provider == "sd":
        d = _post(f"{base}/sdapi/v1/txt2img", cfg, {
            "prompt": prompt,
            "negative_prompt": ", ".join(photo.negative_terms(portrait=portrait)),
            "width": 1024, "height": 576, "steps": 24, "n_iter": 1,
            **_nsfw_params(cfg),
        })
        imgs = d.get("images") or []
        if not imgs:
            raise RuntimeError("SD 没返回图片")
        return _save_image(base64.b64decode(imgs[0]), prefix=prefix)

    if provider == "dashscope":
        # 万相的参数是白名单校验的，塞 nsfw 会 400，所以这一家不发这个字段
        d = _post(f"{base}/services/aigc/text2image/image-synthesis", cfg, {
            "model": model or "wanx-v1",
            "input": {"prompt": prompt},
            "parameters": {"size": "1280*720", "n": 1},
        }, headers={"X-DashScope-Async": "enable"})
        # 万相是异步的：先拿 task_id，再轮询
        task_id = (d.get("output") or {}).get("task_id")
        if not task_id:
            return _save_image(_extract_image(d), prefix=prefix)
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(3)
            r = requests.get(f"{base}/tasks/{task_id}",
                             headers={"Authorization": f"Bearer {cfg['api_key']}"}, timeout=30)
            dd = r.json()
            status = (dd.get("output") or {}).get("task_status")
            if status == "SUCCEEDED":
                return _save_image(_extract_image(dd), prefix=prefix)
            if status in ("FAILED", "CANCELED"):
                raise RuntimeError(f"万相生图失败：{json.dumps(dd, ensure_ascii=False)[:200]}")
        raise RuntimeError("万相生图超时（120s）")

    # 默认 OpenAI images 兼容（智谱/OpenAI/SiliconFlow）
    d = _post(f"{base}/images/generations", cfg, {
        "model": model, "prompt": prompt, "size": "1024x1024", "n": 1,
        **_nsfw_params(cfg),
    })
    return _save_image(_extract_image(d), prefix=prefix)


def generate(topic: str, account: dict | None = None, *, kind: str = "inline",
             extra: str = "", character: dict | None = None) -> str:
    """生成一张图，返回 /media/uploads/xxx.png。没配服务就抛 RuntimeError。

    传了 character 就把外形描述拼进提示词（纯文字后端只有这个办法保住长相）；
    真正要"同一个人多场景"，用 generate_scene()。
    """
    ok, msg = available()
    if not ok:
        raise RuntimeError(msg)
    cfg = image_config()
    prompt = build_prompt(topic, account, kind=kind, extra=extra, character=character)
    return _text_image(cfg, prompt)


def test() -> dict[str, Any]:
    """设置页的「测试生图」：真生成一张小图，别只 ping 一下。"""
    ok, msg = available()
    if not ok:
        return {"ok": False, "error": msg}
    try:
        url = generate("一杯咖啡放在窗边木桌上，早晨自然光", kind="inline")
        return {"ok": True, "url": url}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
