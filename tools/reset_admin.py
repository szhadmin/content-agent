#!/usr/bin/env python3
"""管理员密码修复/重置工具。

用法（在 /opt/wxwb 下）：
  ./venv/bin/python tools/reset_admin.py '新密码'
  ./venv/bin/python tools/reset_admin.py '密码' --if-mismatch   # 只在库里的 hash 对不上时才改
  ./venv/bin/python tools/reset_admin.py --list                 # 看有哪些用户
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, security  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("password", nargs="?", default="")
    ap.add_argument("--user", default="admin")
    ap.add_argument("--if-mismatch", action="store_true")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    db.init_db()

    if args.list:
        for r in db.query("SELECT id, username, role, created_at FROM users ORDER BY id"):
            print(f"{r['id']}\t{r['username']}\t{r['role']}\t{r['created_at']}")
        return 0

    if not args.password:
        print("缺少密码参数", file=sys.stderr)
        return 2

    row = db.query_one("SELECT * FROM users WHERE username=?", (args.user,))
    if row is None:
        db.insert("users", {
            "username": args.user,
            "password_hash": security.hash_password(args.password),
            "display_name": "管理员",
            "role": "admin",
            "created_at": db.now(),
        })
        print(f"已创建管理员 {args.user}")
        return 0

    if args.if_mismatch:
        # 只做体检，不改任何东西（避免把用户在界面上改过的密码又改回去）
        if security.verify_password(args.password, row["password_hash"]):
            print("密码一致")
            return 0
        print("⚠ 库里的密码和记录值不一致。"
              "如确实忘记密码，用 --reset-password 指定新密码重新部署，"
              "或在本机执行 tools/reset_admin.py '新密码'")
        return 3

    db.update("users", row["id"], {"password_hash": security.hash_password(args.password)})
    print(f"已重置 {args.user} 的密码")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
