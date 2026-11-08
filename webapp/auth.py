"""Auth: pbkdf2 password hashing + DB-backed session tokens (stdlib only)."""
import hashlib
import re
import secrets
import threading
import time
from collections import deque
from datetime import datetime, timedelta

from . import config

_PBKDF2_ITERS = 200_000
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")

# naive in-memory login throttle: max 10 attempts / 5 min per IP
_login_hits = {}
_login_lock = threading.Lock()


class AuthError(ValueError):
    pass


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERS)
    return f"{salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$", 1)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(),
                                 bytes.fromhex(salt_hex), _PBKDF2_ITERS)
        return secrets.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


def check_login_rate(ip: str):
    now = time.monotonic()
    with _login_lock:
        hits = _login_hits.setdefault(ip, deque())
        while hits and now - hits[0] > 300:
            hits.popleft()
        if len(hits) >= 10:
            raise AuthError("尝试过于频繁，请 5 分钟后再试")
        hits.append(now)


def _now() -> datetime:
    return datetime.utcnow()


def validate_username_password(username: str, password: str):
    if not _USERNAME_RE.match(username or ""):
        raise AuthError("用户名需 3-32 位字母、数字或下划线")
    if not password or len(password) < 8:
        raise AuthError("密码至少 8 位")


def create_user(conn, username: str, password: str):
    """Create a user. The FIRST user of an instance becomes its admin
    (auto-approved); everyone else starts pending admin review.
    Returns (user_id, auto_approved)."""
    validate_username_password(username, password)
    cur = conn.execute("SELECT id FROM users WHERE username = ?", (username,))
    if cur.fetchone():
        raise AuthError("用户名已存在")
    is_first = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 0
    role = "admin" if is_first else "user"
    approved = 1 if is_first else 0
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, role, approved, created_at) "
        "VALUES (?,?,?,?,?)",
        (username, hash_password(password), role, approved, _now().isoformat()))
    conn.commit()
    return cur.lastrowid, bool(is_first)


def login(conn, username: str, password: str) -> str:
    row = conn.execute("SELECT id, password_hash, approved FROM users WHERE username = ?",
                       (username or "",)).fetchone()
    if not row or not verify_password(password or "", row["password_hash"]):
        raise AuthError("账号或密码错误")
    if not row["approved"]:
        raise AuthError("账号已创建，等待管理员审核通过后登录")
    token = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=config.SESSION_TTL_DAYS)
    conn.execute("INSERT INTO sessions (token, user_id, expires_at, created_at) "
                 "VALUES (?,?,?,?)",
                 (token, row["id"], expires.isoformat(), _now().isoformat()))
    # opportunistic cleanup of dead sessions
    conn.execute("DELETE FROM sessions WHERE expires_at < ?",
                 (_now().isoformat(),))
    conn.commit()
    return token


def change_password(conn, user_id: int, old_password: str, new_password: str):
    """Verify the old password, set the new one, and revoke ALL sessions of
    the user (forced re-login everywhere with the new credentials)."""
    row = conn.execute("SELECT password_hash FROM users WHERE id = ?",
                       (user_id,)).fetchone()
    if not row or not verify_password(old_password or "", row["password_hash"]):
        raise AuthError("当前密码不正确")
    if not new_password or len(new_password) < 8:
        raise AuthError("新密码至少 8 位")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                 (hash_password(new_password), user_id))
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    conn.commit()


def logout(conn, token: str):
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()


def user_for_token(conn, token: str):
    """Return the user row for a valid session, sliding the expiry forward."""
    if not token:
        return None
    row = conn.execute(
        "SELECT u.id, u.username, u.role, u.email, u.created_at FROM sessions s "
        "JOIN users u ON u.id = s.user_id WHERE s.token = ? AND s.expires_at > ?",
        (token, _now().isoformat())).fetchone()
    if row:
        new_exp = _now() + timedelta(days=config.SESSION_TTL_DAYS)
        conn.execute("UPDATE sessions SET expires_at = ? WHERE token = ?",
                     (new_exp.isoformat(), token))
        conn.commit()
    return row
