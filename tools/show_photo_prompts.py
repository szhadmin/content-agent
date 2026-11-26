"""看一眼生图提示词长什么样（不调生图接口，纯本地拼串）。

用途
----
1. 改 photo.py 的词库后，直接跑这个看效果，不用真花钱生图；
2. CI 里做自检：连出 6 张，同一套方案不能重复、不能出现典型 AI 味词；
3. 排查「这张图为什么难看」时，把当时的 idx 丢进来复现提示词（plan 是纯函数）。

用法：
    python tools/show_photo_prompts.py                 # 人像 3 张 + 静物 1 张
    python tools/show_photo_prompts.py --style film     # 换风格
    python tools/show_photo_prompts.py --check          # 只跑自检，不打印全文
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows 终端默认 GBK，中文/符号直接 print 会炸，强制 UTF-8 输出
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding='utf-8')
    except Exception:
        pass

from app import aigen, photo  # noqa: E402

# 提示词里不该出现的词：这些词会把画面推向「渲染图」，是 AI 味的常见来源
BANNED = ["8k", "8K", "4k", "masterpiece", "best quality", "ultra detailed",
          "hyperrealistic", "perfect lighting", "award winning", "highly detailed",
          "cinematic lighting", "trending on artstation"]


def check() -> int:
    """自检：轮换、必要性、禁用词。返回失败数。"""
    bad = 0

    # 1. 相邻 6 张必须各不相同（同一个 idx 才应该一样）
    prompts = [aigen.build_prompt("窗边的一杯手冲咖啡", None, plan_idx=i) for i in range(6)]
    if len(set(prompts)) != len(prompts):
        print("[x] 连续生成的提示词出现重复")
        bad += 1
    if aigen.build_prompt("窗边的一杯手冲咖啡", None, plan_idx=3) != prompts[3]:
        print("[x] plan() 不是纯函数：同一个 idx 出来的提示词不一样")
        bad += 1

    # 2. 人像提示词要有皮肤/表情/光的方向，静物不该有皮肤
    ch = {"id": 1, "appearance": "短发，方脸，单眼皮", "scene_hint": ""}
    portrait = aigen.build_prompt("在阳台晾衣服", None, character=ch, plan_idx=0)
    for need in ("皮肤", "表情", "光：", "拍法："):
        if need not in portrait:
            print(f"[x] 人像提示词缺少「{need}」")
            bad += 1
    if "皮肤：" in prompts[0]:
        print("[x] 静物提示词里不该有皮肤描写")
        bad += 1

    # 3. 禁用词（AI 味来源）一个都不能有
    for p in prompts + [portrait]:
        for w in BANNED:
            if w in p:
                print(f"[x] 提示词里出现了「{w}」")
                bad += 1

    # 4. 负向词要真的被带进去
    if "不要出现" not in portrait:
        print("[x] 人像提示词缺少负向约束段")
        bad += 1

    # 5. 每个风格都能出方案（别有点开就 KeyError 的），而且连出 6 张不能重复——
    #    风格锁的是器材/光/调子，构图、瑕疵、质感、表情必须还在轮换。
    for sid in photo.STYLE_ORDER:
        pp = photo.plan(0, portrait=True, style=sid)
        if not pp.get("camera") or not pp.get("light"):
            print(f"[x] 风格 {sid} 的方案不完整")
            bad += 1
        blocks = [photo.prompt_block(photo.plan(i, portrait=True, style=sid), portrait=True)
                  for i in range(6)]
        if len(set(blocks)) != len(blocks):
            print(f"[x] 风格 {sid} 连出 6 张有重复（风格把参数锁死了）")
            bad += 1

    print("check ok" if not bad else f"check failed: {bad}")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--style", default="auto", choices=photo.STYLE_ORDER)
    ap.add_argument("--check", action="store_true", help="只跑自检")
    args = ap.parse_args()

    if not args.check:
        ch = {"id": 1, "appearance": "短发，方脸，单眼皮，鼻梁略低", "scene_hint": ""}
        for i in range(3):
            p = photo.plan(i, portrait=True, style=args.style,
                           look=ch["appearance"], scene_hint="")
            print(f"\n===== 人像 idx={i} style={args.style} =====")
            print(photo.prompt_block(p, portrait=True))
        print("\n===== 静物（不带人物）=====")
        print(photo.prompt_block(photo.plan(0, portrait=False, style=args.style),
                                 portrait=False))
        print("\n===== 负向词 =====")
        print("、".join(photo.negative_terms(portrait=True)))

    return 1 if check() else 0


if __name__ == "__main__":
    raise SystemExit(main())

