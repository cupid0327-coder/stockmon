"""邮件配置与 HTTP 接口契约（TestClient 走真实路由与依赖）。"""
import pytest
from fastapi.testclient import TestClient

from webapp import auth, config, db, mailer


@pytest.fixture
def client():
    from webapp.app import app
    # 不用 with：跳过 lifespan（不启调度线程、不重复 init_db）
    return TestClient(app)


def _make_user(username="admin01", password="supersecret", role="admin",
               approved=1):
    conn = db.connect()
    try:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, role, approved, "
            "created_at) VALUES (?,?,?,?,?)",
            (username, auth.hash_password(password), role, approved,
             auth._now().isoformat()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _login(client, username="admin01", password="supersecret"):
    r = client.post("/api/login", json={"username": username,
                                        "password": password})
    assert r.status_code == 200, r.text
    return client


# ---------- mailer ----------

def test_effective_settings_rejects_bad_port(conn):
    conn.execute("INSERT INTO settings (key, value) VALUES ('smtp_port', 'abc')")
    conn.commit()
    with pytest.raises(mailer.MailerError):
        mailer.effective_settings()


def test_effective_settings_rejects_out_of_range_port(conn):
    conn.execute("INSERT INTO settings (key, value) VALUES ('smtp_port', '70000')")
    conn.commit()
    with pytest.raises(mailer.MailerError):
        mailer.effective_settings()


def test_effective_settings_defaults(conn, monkeypatch):
    monkeypatch.setattr(config, "SMTP_HOST", "")
    cfg = mailer.effective_settings()
    assert cfg["port"] == config.SMTP_PORT
    assert cfg["configured"] is False


# ---------- HTTP 契约 ----------

def test_run_once_requires_admin(client):
    _make_user("plain_user", role="user")
    _login(client, "plain_user")
    r = client.post("/api/run-once")
    assert r.status_code == 403


def test_run_once_response_has_no_event_details(client, monkeypatch):
    from webapp import app as app_mod
    _make_user("admin01")
    _login(client, "admin01")
    monkeypatch.setattr(app_mod, "get_scheduler", lambda: type(
        "S", (), {"tick": lambda self, force=False: {
            "status": "ok", "decision": "wake", "reason": "r", "window": "close",
            "events": 2,
            "emitted": [{"user_id": 1, "code": "600519", "price": 1.0}]}})())
    r = client.post("/api/run-once")
    assert r.status_code == 200
    body = r.json()
    assert "emitted" not in body
    assert set(body) <= {"status", "decision", "reason", "window", "events"}


def test_mail_settings_port_validation(client):
    _make_user("admin01")
    _login(client, "admin01")
    for bad in ("abc", "0", "70000", "-1", "12.5"):
        r = client.post("/api/admin/mail", json={"host": "smtp.example.com",
                                                 "port": bad,
                                                 "user": "u", "passwd": "p",
                                                 "mail_from": "a@b.c",
                                                 "tls": "ssl"})
        assert r.status_code == 400, f"port={bad!r} -> {r.status_code}"
    r = client.post("/api/admin/mail", json={"host": "smtp.example.com",
                                            "port": "465", "user": "u",
                                            "passwd": "p", "mail_from": "a@b.c",
                                            "tls": "bogus"})
    assert r.status_code == 400   # tls 只接受 ssl / starttls
    r = client.post("/api/admin/mail", json={"host": "smtp.example.com",
                                            "port": "587", "user": "u",
                                            "passwd": "p", "mail_from": "a@b.c",
                                            "tls": "starttls"})
    assert r.status_code == 200


def test_mail_settings_get_tolerates_broken_port(client):
    """库里存了非法端口时，管理页读取不能 500。"""
    _make_user("admin01")
    _login(client, "admin01")
    conn = db.connect()
    try:
        conn.execute("INSERT INTO settings (key, value) VALUES ('smtp_port', 'x')")
        conn.commit()
    finally:
        conn.close()
    r = client.get("/api/admin/mail")
    assert r.status_code == 200
    assert r.json()["configured"] is False


def test_delete_user_removes_events(client):
    admin_id = _make_user("admin01")
    other_id = _make_user("other_user", role="user")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO events (ts, event_type, user_id, market, code, name) "
            "VALUES ('2026-11-06T15:05:00', 'close_break_day1', ?, 'cn_stock', "
            "'600519', 'x')", (other_id,))
        conn.commit()
    finally:
        conn.close()
    _login(client, "admin01")
    r = client.delete(f"/api/admin/users/{other_id}")
    assert r.status_code == 200
    conn = db.connect()
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM events WHERE user_id = ?",
                         (other_id,)).fetchone()["n"]
    finally:
        conn.close()
    assert n == 0


def _add_000001_pair(uid):
    """上证指数 sh000001 与平安银行 sz000001：code 相同、sina_code 不同。"""
    conn = db.connect()
    try:
        for market, code, name, sina, secid in (
                ("cn_index", "000001", "上证指数", "sh000001", "1.000001"),
                ("cn_stock", "000001", "平安银行", "sz000001", "0.000001")):
            conn.execute(
                "INSERT INTO instruments (user_id, market, code, name, "
                "sina_code, secid, created_at) VALUES (?,?,?,?,?,?,?)",
                (uid, market, code, name, sina, secid, "2026-01-01T00:00:00"))
        conn.commit()
    finally:
        conn.close()


def test_quotes_are_keyed_by_sina_code(client, monkeypatch):
    """上证指数与平安银行 code 相同但 sina_code 不同：两个键都要有值。"""
    from webapp import app as app_mod, quotes
    from webapp import baseline_gen
    uid = _make_user("admin01")
    _add_000001_pair(uid)
    monkeypatch.setattr(baseline_gen, "cn_market_open", lambda: True)
    monkeypatch.setattr(baseline_gen, "us_market_open", lambda: False)
    app_mod._QUOTES_MEM.clear()
    monkeypatch.setattr(quotes, "fetch_quotes", lambda rows: (
        {str(r["sina_code"]): {"price": 3200.0 if str(r["sina_code"]) == "sh000001"
                               else 11.0, "day_change_pct": 1.0} for r in rows},
        False))
    _login(client, "admin01")
    r = client.get("/api/quotes")
    assert r.status_code == 200
    q = r.json()["quotes"]
    assert q["sh000001"]["price"] == 3200.0
    assert q["sz000001"]["price"] == 11.0


def test_closed_market_replays_snapshot_without_fetch(client, monkeypatch):
    """闭市且快照全部命中：回放库内快照，零数据源请求。"""
    from webapp import app as app_mod, quotes
    from webapp import baseline_gen
    uid = _make_user("admin01")
    _add_000001_pair(uid)
    monkeypatch.setattr(baseline_gen, "cn_market_open", lambda: False)
    monkeypatch.setattr(baseline_gen, "us_market_open", lambda: False)
    monkeypatch.setattr(quotes, "market_snapshot", lambda mkt: {
        "sh000001": {"price": 3200.0, "day_change_pct": 0.1},
        "sz000001": {"price": 11.0, "day_change_pct": -0.5}})
    calls = []
    monkeypatch.setattr(quotes, "fetch_quotes",
                        lambda rows: calls.append(rows) or ({}, False))
    app_mod._QUOTES_MEM.clear()
    _login(client, "admin01")
    q = client.get("/api/quotes").json()["quotes"]
    assert q["sh000001"]["price"] == 3200.0
    assert q["sz000001"]["price"] == 11.0
    assert calls == []          # 全部键命中快照 → 不拉数据源


def test_closed_market_fetches_only_missing_keys(client, monkeypatch):
    """闭市但快照只覆盖部分标的：只补齐缺的键，不能整体缺价。"""
    from webapp import app as app_mod, quotes
    from webapp import baseline_gen
    uid = _make_user("admin01")
    _add_000001_pair(uid)
    monkeypatch.setattr(baseline_gen, "cn_market_open", lambda: False)
    monkeypatch.setattr(baseline_gen, "us_market_open", lambda: False)
    monkeypatch.setattr(quotes, "market_snapshot", lambda mkt: {
        "sh000001": {"price": 3200.0, "day_change_pct": 0.1}})
    calls = []

    def fake_fetch(rows):
        calls.append(sorted(str(r["sina_code"]) for r in rows))
        return ({str(r["sina_code"]): {"price": 11.0, "day_change_pct": 0.0}
                 for r in rows}, False)

    monkeypatch.setattr(quotes, "fetch_quotes", fake_fetch)
    app_mod._QUOTES_MEM.clear()
    _login(client, "admin01")
    q = client.get("/api/quotes").json()["quotes"]
    assert calls == [["sz000001"]]      # 只补缺失的那个键
    assert q["sh000001"]["price"] == 3200.0
    assert q["sz000001"]["price"] == 11.0



def test_dashboard_quotes_merged_by_sina_code(client, monkeypatch):
    """看板与 /api/quotes 共用共享缓存：第二次请求不再打数据源。"""
    from webapp import app as app_mod, quotes
    from webapp import baseline_gen
    uid = _make_user("admin01")
    conn = db.connect()
    try:
        conn.execute(
            "INSERT INTO instruments (user_id, market, code, name, sina_code, "
            "secid, created_at) VALUES (?,?,?,?,?,?,?)",
            (uid, "cn_index", "000001", "上证指数", "sh000001", "1.000001",
             "2026-01-01T00:00:00"))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(baseline_gen, "cn_market_open", lambda: True)
    monkeypatch.setattr(baseline_gen, "us_market_open", lambda: False)
    monkeypatch.setattr(app_mod, "_live_short_term", lambda row: {})
    app_mod._QUOTES_MEM.clear()
    calls = []

    def fake_fetch(rows):
        calls.append(len(rows))
        return ({str(r["sina_code"]): {"price": 3200.0, "day_change_pct": 0.5}
                 for r in rows}, False)

    monkeypatch.setattr(quotes, "fetch_quotes", fake_fetch)
    _login(client, "admin01")
    r1 = client.get("/api/dashboard")
    r2 = client.get("/api/dashboard")
    assert r1.status_code == 200 and r2.status_code == 200
    for r in (r1, r2):
        item = r.json()["groups"]["cn_index"][0]
        assert item["price"] == 3200.0
        assert item["day_change_pct"] == 0.5
    assert calls == [1]   # 第二次命中 30s 共享缓存


def test_dashboard_skips_short_term_for_disabled(client, monkeypatch):
    from webapp import app as app_mod
    from webapp import baseline_gen
    uid = _make_user("admin01")
    conn = db.connect()
    try:
        for code, sina, enabled in (("600519", "sh600519", 1),
                                    ("000002", "sz000002", 0)):
            conn.execute(
                "INSERT INTO instruments (user_id, market, code, name, "
                "sina_code, secid, enabled, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (uid, "cn_stock", code, code, sina, f"1.{code}", enabled,
                 "2026-01-01T00:00:00"))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(baseline_gen, "cn_market_open", lambda: False)
    monkeypatch.setattr(baseline_gen, "us_market_open", lambda: False)
    seen = []
    monkeypatch.setattr(app_mod, "_live_short_term",
                        lambda row: seen.append(row["code"]) or {})
    _login(client, "admin01")
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    assert seen == ["600519"]                       # 停用标的不打数据源
    items = r.json()["groups"]["cn_stock"]
    assert {i["code"] for i in items} == {"600519", "000002"}   # 但仍在响应里


def test_client_ip_uses_last_forwarded_hop(monkeypatch):
    from webapp.app import client_ip

    class Req:
        headers = {"x-forwarded-for": "1.2.3.4, 203.0.113.7"}
        client = type("C", (), {"host": "10.0.0.1"})()

    assert client_ip(Req()) == "203.0.113.7"
