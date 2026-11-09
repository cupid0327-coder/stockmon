"""认证：注册原子性、密码校验、限流、会话滑动过期与时间格式。"""
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from webapp import auth, db


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    auth._login_hits.clear()
    yield
    auth._login_hits.clear()


def test_validate_username_password():
    auth.validate_username_password("vincent_01", "supersecret")
    for bad in ("ab", "has space", "x" * 33, "中文名"):
        with pytest.raises(auth.AuthError):
            auth.validate_username_password(bad, "supersecret")
    with pytest.raises(auth.AuthError):
        auth.validate_username_password("vincent_01", "short")


def test_verify_password_roundtrip():
    stored = auth.hash_password("supersecret")
    assert auth.verify_password("supersecret", stored)
    assert not auth.verify_password("wrong", stored)
    assert not auth.verify_password("supersecret", "not-a-hash")
    assert not auth.verify_password("supersecret", "")


def test_first_user_is_admin_then_pending(conn):
    uid, auto = auth.create_user(conn, "first_admin", "supersecret")
    assert auto is True
    row = conn.execute("SELECT role, approved FROM users WHERE id = ?",
                       (uid,)).fetchone()
    assert row["role"] == "admin" and row["approved"] == 1

    uid2, auto2 = auth.create_user(conn, "second_user", "supersecret")
    assert auto2 is False
    row2 = conn.execute("SELECT role, approved FROM users WHERE id = ?",
                        (uid2,)).fetchone()
    assert row2["role"] == "user" and row2["approved"] == 0


def test_create_user_duplicate_raises_autherror(conn):
    auth.create_user(conn, "vincent_01", "supersecret")
    with pytest.raises(auth.AuthError):
        auth.create_user(conn, "vincent_01", "supersecret")
    # 显式事务已回滚且隔离级别已还原：后续隐式事务照常工作
    conn.execute("INSERT INTO runs (ts, window, decision, reason) "
                 "VALUES ('2026-01-01T00:00:00', 'manual', 'info', 'ok')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"] == 1


def test_create_user_race_is_atomic():
    """并发注册同名用户：恰好一个成功，另一个得到 AuthError（不是 IntegrityError 500）。"""
    results = []
    barrier = threading.Barrier(2)

    def worker():
        c = db.connect()
        try:
            barrier.wait(timeout=10)
            results.append(auth.create_user(c, "race_user", "supersecret"))
        except Exception as e:   # noqa: BLE001 - 断言里区分类型
            results.append(e)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(results) == 2
    assert not any(isinstance(r, sqlite3.IntegrityError) for r in results)
    assert sum(isinstance(r, tuple) for r in results) == 1
    assert sum(isinstance(r, auth.AuthError) for r in results) == 1
    # 恰好一个 admin（首个用户判定与插入在同一事务里）
    conn = db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"] == 1
        assert conn.execute("SELECT role FROM users").fetchone()["role"] == "admin"
    finally:
        conn.close()


def test_login_requires_approval(conn):
    auth.create_user(conn, "first_admin", "supersecret")
    auth.create_user(conn, "pending_user", "supersecret")
    with pytest.raises(auth.AuthError):
        auth.login(conn, "pending_user", "supersecret")
    with pytest.raises(auth.AuthError):
        auth.login(conn, "first_admin", "wrong-password")
    token = auth.login(conn, "first_admin", "supersecret")
    assert token and auth.user_for_token(conn, token)["username"] == "first_admin"
    assert auth.user_for_token(conn, "bogus-token") is None


def test_session_slide_only_when_near_expiry(conn):
    auth.create_user(conn, "first_admin", "supersecret")
    uid = conn.execute("SELECT id FROM users").fetchone()["id"]
    far = auth._now() + timedelta(days=20)
    conn.execute("INSERT INTO sessions (token, user_id, expires_at, created_at) "
                 "VALUES ('tok_far', ?, ?, ?)",
                 (uid, far.isoformat(), auth._now().isoformat()))
    conn.commit()
    auth.user_for_token(conn, "tok_far")
    got = conn.execute("SELECT expires_at FROM sessions WHERE token='tok_far'"
                       ).fetchone()["expires_at"]
    assert got == far.isoformat()   # 未写库

    near = auth._now() + timedelta(hours=2)
    conn.execute("INSERT INTO sessions (token, user_id, expires_at, created_at) "
                 "VALUES ('tok_near', ?, ?, ?)",
                 (uid, near.isoformat(), auth._now().isoformat()))
    conn.commit()
    auth.user_for_token(conn, "tok_near")
    got2 = conn.execute("SELECT expires_at FROM sessions WHERE token='tok_near'"
                        ).fetchone()["expires_at"]
    assert datetime.fromisoformat(got2) > near


def test_now_is_naive_utc_and_comparable():
    now = auth._now()
    assert now.tzinfo is None
    # 与库里旧行的字符串格式同量级：naive ISO 串可直接比较
    old = "2020-01-01T00:00:00.000000"
    assert old < now.isoformat()
    utc_now = datetime.now(timezone.utc).replace(tzinfo=None)
    assert abs(now - utc_now) < timedelta(seconds=5)


def test_login_rate_limit_blocks_11th():
    for _ in range(10):
        auth.check_login_rate("203.0.113.9")
    with pytest.raises(auth.AuthError):
        auth.check_login_rate("203.0.113.9")
    auth.check_login_rate("203.0.113.10")   # 其他 IP 不受影响


def test_login_rate_limit_prunes_empty_buckets(monkeypatch):
    monkeypatch.setattr(auth, "_LOGIN_HITS_MAX_KEYS", 3)
    for i in range(6):
        auth.check_login_rate(f"198.51.100.{i}")
        assert len(auth._login_hits) <= 3   # 硬上限：不会无界增长
