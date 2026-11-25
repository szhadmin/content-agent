"""密码哈希、AppSecret 加密、会话。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from typing import Optional

from . import db
from .settings import SECRET_KEY_PATH, SESSION_TTL_SECONDS

_ITERATIONS = 180_000


# ---------------- 密码 ----------------

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_hex, hash_hex = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iters)
        )
        return hmac.compare_digest(dk.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


# ---------------- AppSecret 加密 ----------------

def _fernet():
    from cryptography.fernet import Fernet  # type: ignore

    key: bytes
    if SECRET_KEY_PATH.exists():
        key = SECRET_KEY_PATH.read_bytes().strip()
    else:
        key = Fernet.generate_key()
        SECRET_KEY_PATH.write_bytes(key)
        try:
            os.chmod(SECRET_KEY_PATH, 0o600)
        except OSError:
            pass
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception:  # noqa: BLE001 - 兼容早期明文数据
        return token


def mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 8}{secret[-4:]}"


# ---------------- 会话 ----------------

def create_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO sessions (token, username, expires_at, created_at) VALUES (?,?,?,?)",
        (token, username, int(time.time()) + SESSION_TTL_SECONDS, db.now()),
    )
    return token


def session_user(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    row = db.query_one("SELECT * FROM sessions WHERE token=?", (token,))
    if not row:
        return None
    if row["expires_at"] < time.time():
        db.execute("DELETE FROM sessions WHERE token=?", (token,))
        return None
    user = db.query_one("SELECT * FROM users WHERE username=?", (row["username"],))
    return dict(user) if user else None


def destroy_session(token: Optional[str]) -> None:
    if token:
        db.execute("DELETE FROM sessions WHERE token=?", (token,))


def ensure_bootstrap_admin() -> None:
    """首次启动时创建管理员；密码来自 WXWB_ADMIN_PASSWORD 或随机生成并打印。"""
    if db.query_one("SELECT id FROM users LIMIT 1"):
        return
    username = os.environ.get("WXWB_ADMIN_USER", "admin")
    password = os.environ.get("WXWB_ADMIN_PASSWORD")
    generated = False
    if not password:
        password = base64.urlsafe_b64encode(os.urandom(9)).decode().rstrip("=")
        generated = True
    db.insert(
        "users",
        {
            "username": username,
            "password_hash": hash_password(password),
            "display_name": "管理员",
            "role": "admin",
            "created_at": db.now(),
        },
    )
    if generated:
        print("=" * 62)
        print("  已创建管理员账号")
        print(f"  用户名: {username}")
        print(f"  密码  : {password}")
        print("  请立即登录后修改，或用 WXWB_ADMIN_PASSWORD 环境变量覆盖")
        print("=" * 62, flush=True)
