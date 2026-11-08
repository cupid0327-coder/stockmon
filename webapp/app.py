"""FastAPI entry: auth + instruments CRUD + dashboard/events/runs API,
static SPA hosting, and the scheduler thread lifecycle.
Run:  uvicorn webapp.app:app --host 127.0.0.1 --port 8100   (single worker!)
"""
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import re

from . import auth, baseline_gen, config, db, mailer, quotes as quotes_mod
from .scheduler import get_scheduler, detector_module

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("stockmon.app")

STATIC_DIR = Path(__file__).resolve().parent / "static"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    sched = get_scheduler()
    sched.start()
    log.info("stockmon started (poll=%ss)", config.POLL_INTERVAL_SECS)
    yield
    sched.stop()


app = FastAPI(title="stockmon", lifespan=lifespan)


@app.middleware("http")
async def no_cache_static(request, call_next):
    """Static assets and the SPA shell must always revalidate — stale JS
    after an update otherwise breaks the page until a hard refresh."""
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.endswith((".js", ".css", ".html")):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- helpers ----------

def get_conn():
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return (fwd.split(",")[0].strip() if fwd else None) or \
        (request.client.host if request.client else "?")


def current_user(request: Request, conn=Depends(get_conn)):
    token = request.cookies.get(config.SESSION_COOKIE)
    user = auth.user_for_token(conn, token)
    if not user:
        raise HTTPException(401, "未登录或会话已过期")
    return user


def admin_user(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "需要管理员权限")
    return user


def auth_error(e: auth.AuthError):
    return HTTPException(400, str(e))


class Credentials(BaseModel):
    username: str
    password: str


class InstrumentBody(BaseModel):
    market: str
    code: str


def set_session_cookie(response: Response, token: str):
    response.set_cookie(
        config.SESSION_COOKIE, token,
        max_age=config.SESSION_TTL_DAYS * 86400,
        httponly=True, samesite="lax", secure=False)  # nginx+TLS 终结时可手动改 True


# ---------- auth ----------

@app.post("/api/register")
def register(body: Credentials, request: Request, conn=Depends(get_conn)):
    if not config.REGISTRATION_OPEN:
        raise HTTPException(403, "注册已关闭")
    auth.check_login_rate(client_ip(request))
    try:
        _uid, auto_approved = auth.create_user(conn, body.username, body.password)
        if not auto_approved:
            return JSONResponse({
                "ok": True, "pending": True, "username": body.username,
                "message": "注册成功，等待管理员审核通过后登录"})
        token = auth.login(conn, body.username, body.password)
    except auth.AuthError as e:
        raise auth_error(e)
    resp = JSONResponse({"ok": True, "username": body.username})
    set_session_cookie(resp, token)
    return resp


@app.post("/api/login")
def login(body: Credentials, request: Request, conn=Depends(get_conn)):
    auth.check_login_rate(client_ip(request))
    try:
        token = auth.login(conn, body.username, body.password)
    except auth.AuthError as e:
        raise auth_error(e)
    resp = JSONResponse({"ok": True, "username": body.username})
    set_session_cookie(resp, token)
    return resp


class PasswordChange(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/change-password")
def change_password(body: PasswordChange, user=Depends(current_user),
                    conn=Depends(get_conn)):
    try:
        auth.change_password(conn, user["id"], body.old_password,
                             body.new_password)
    except auth.AuthError as e:
        raise auth_error(e)
    # all sessions of the user were revoked server-side
    resp = JSONResponse({"ok": True,
                         "message": "密码已修改，请用新密码重新登录"})
    resp.delete_cookie(config.SESSION_COOKIE)
    return resp


class EmailBody(BaseModel):
    email: str


@app.post("/api/me/email")
def set_email(body: EmailBody, user=Depends(current_user), conn=Depends(get_conn)):
    email = (body.email or "").strip()
    if email and not _EMAIL_RE.match(email):
        raise HTTPException(400, "邮箱格式不正确")
    conn.execute("UPDATE users SET email = ? WHERE id = ?",
                 (email or None, user["id"]))
    conn.commit()
    return {"ok": True, "email": email or None}


@app.post("/api/test-email")
def test_email(user=Depends(current_user), conn=Depends(get_conn)):
    row = conn.execute("SELECT email FROM users WHERE id = ?",
                       (user["id"],)).fetchone()
    email = (row["email"] or "").strip() if row else None
    if not email:
        raise HTTPException(400, "请先设置通知邮箱")
    try:
        mailer.send_mail(
            email, "STOCKMON 测试邮件",
            "这是一封测试邮件——SMTP 配置成功后，信号触发时你会在这里收到买卖提醒。\n"
            "所有信号仅供参考，不构成投资建议。")
        return {"ok": True}
    except mailer.MailerError as e:
        raise HTTPException(502, str(e))


# ---------- admin: SMTP 设置（存 DB，环境变量兜底） ----------

_MAIL_KEYS = ("smtp_host", "smtp_port", "smtp_user", "smtp_pass",
              "mail_from", "smtp_tls")


@app.get("/api/admin/mail")
def admin_mail_get(_=Depends(admin_user), conn=Depends(get_conn)):
    rows = {r["key"]: r["value"] for r in
            conn.execute("SELECT key, value FROM settings")}
    cfg = mailer.effective_settings()
    return {
        "host": rows.get("smtp_host") or "", "port": rows.get("smtp_port") or "465",
        "user": rows.get("smtp_user") or "", "mail_from": rows.get("mail_from") or "",
        "tls": rows.get("smtp_tls") or "ssl",
        "has_pass": bool(rows.get("smtp_pass")),
        "configured": cfg["configured"],
        "env_fallback": bool(config.SMTP_HOST),
    }


class MailSettings(BaseModel):
    host: str = ""
    port: str = "465"
    user: str = ""
    passwd: str = ""      # 留空 = 保持已保存的密码
    mail_from: str = ""
    tls: str = "ssl"


@app.post("/api/admin/mail")
def admin_mail_save(body: MailSettings, _=Depends(admin_user),
                    conn=Depends(get_conn)):
    vals = {
        "smtp_host": body.host.strip(),
        "smtp_port": body.port.strip() or "465",
        "smtp_user": body.user.strip(),
        "mail_from": body.mail_from.strip(),
        "smtp_tls": body.tls.strip() or "ssl",
    }
    if body.passwd.strip():
        vals["smtp_pass"] = body.passwd.strip()
    for k, v in vals.items():
        conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     (k, v))
    conn.commit()
    cfg = mailer.effective_settings()
    return {"ok": True, "configured": cfg["configured"]}


@app.post("/api/admin/mail/test")
def admin_mail_test(admin=Depends(admin_user), conn=Depends(get_conn)):
    row = conn.execute("SELECT email FROM users WHERE id = ?",
                       (admin["id"],)).fetchone()
    email = (row["email"] or "").strip() if row else None
    if not email:
        raise HTTPException(400, "请先在「账号设置」里设置你的通知邮箱")
    try:
        mailer.send_mail(email, "STOCKMON SMTP 测试",
                         "SMTP 配置测试成功——信号触发时提醒将发送到此邮箱。")
        return {"ok": True}
    except mailer.MailerError as e:
        raise HTTPException(502, str(e))


@app.post("/api/logout")
def logout(request: Request, conn=Depends(get_conn)):
    token = request.cookies.get(config.SESSION_COOKIE)
    if token:
        auth.logout(conn, token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(config.SESSION_COOKIE)
    return resp


@app.get("/api/me")
def me(user=Depends(current_user)):
    return {"id": user["id"], "username": user["username"],
            "role": user["role"], "email": user["email"],
            "created_at": user["created_at"],
            "intraday_refresh_secs": config.INTRADAY_REFRESH_SECS}


# ---------- admin ----------

@app.get("/api/admin/users")
def admin_users(_=Depends(admin_user), conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT id, username, role, approved, created_at FROM users "
        "ORDER BY approved ASC, id ASC").fetchall()
    return {"users": [dict(r) for r in rows]}


@app.post("/api/admin/users/{user_id}/approve")
def admin_approve(user_id: int, _=Depends(admin_user), conn=Depends(get_conn)):
    cur = conn.execute(
        "UPDATE users SET approved = 1 WHERE id = ? AND role != 'admin'",
        (user_id,))
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "用户不存在或已是管理员")
    return {"ok": True}


class RoleChange(BaseModel):
    role: str


@app.post("/api/admin/users/{user_id}/set-role")
def admin_set_role(user_id: int, body: RoleChange, admin=Depends(admin_user),
                   conn=Depends(get_conn)):
    """Promote a user to admin or demote an admin to user. Promoting also
    approves the account. Guards: no self-change; the system always keeps
    at least one admin."""
    role = body.role
    if role not in ("admin", "user"):
        raise HTTPException(400, "role 取值无效")
    if user_id == admin["id"]:
        raise HTTPException(400, "不能修改自己的角色")
    row = conn.execute("SELECT id, role, approved FROM users WHERE id = ?",
                       (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "用户不存在")
    if row["role"] == role:
        return {"ok": True}
    if role == "user" and row["role"] == "admin":
        n_admins = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'admin'").fetchone()["n"]
        if n_admins <= 1:
            raise HTTPException(400, "系统至少保留一名管理员")
    conn.execute("UPDATE users SET role = ?, approved = 1 WHERE id = ?",
                 (role, user_id))
    conn.commit()
    return {"ok": True}


@app.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, admin=Depends(admin_user),
                      conn=Depends(get_conn)):
    if user_id == admin["id"]:
        raise HTTPException(400, "不能删除自己的账号")
    row = conn.execute("SELECT role FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row:
        raise HTTPException(404, "用户不存在")
    if row["role"] == "admin":
        raise HTTPException(400, "不能删除管理员账号")
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    conn.commit()
    return {"ok": True}


# ---------- instruments ----------

@app.get("/api/instruments")
def list_instruments(user=Depends(current_user), conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT i.id, i.market, i.code, i.name, i.ma250, i.ma250_asof, "
        "i.last_price, i.weekly_macd, i.volume_waived, i.enabled, i.created_at, "
        "s.status, s.side, s.below_days, s.price AS status_price, "
        "s.day_change_pct, s.sell_signal, s.sell_note, "
        "s.buy_signal, s.buy_note, s.st_macd, s.st_rsi, s.st_note, s.updated_at "
        "FROM instruments i "
        "LEFT JOIN instrument_status s ON s.instrument_id = i.id "
        "WHERE i.user_id = ? ORDER BY i.market, i.code",
        (user["id"],)).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.post("/api/instruments")
def add_instrument(body: InstrumentBody, user=Depends(current_user),
                   conn=Depends(get_conn)):
    try:
        b = baseline_gen.build_instrument(body.market, body.code)
    except baseline_gen.BaselineError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"行情数据源请求失败：{e}")
    try:
        cur = conn.execute(
            "INSERT INTO instruments (user_id, market, code, name, sina_code, "
            "secid, ma250, ma250_asof, last_price, weekly_macd, volume_waived, "
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (user["id"], body.market, b["code"], b["name"], b["sina_code"],
             b["secid"], b["ma250"], b["ma250_asof"], b["last_price"],
             b["weekly_macd"], b["volume_waived"]))
        conn.commit()
    except Exception:
        raise HTTPException(409, "该标的已在你的自选中")
    row = conn.execute("SELECT * FROM instruments WHERE id = ?",
                       (cur.lastrowid,)).fetchone()
    return {"ok": True, "item": dict(row),
            "computed": {"ma250": b["ma250"], "weekly_macd": b["weekly_macd"],
                         "price": b["last_price"], "bars": b["bars"]}}


@app.patch("/api/instruments/{instrument_id}")
def toggle_instrument(instrument_id: int, body: dict,
                      user=Depends(current_user), conn=Depends(get_conn)):
    enabled = body.get("enabled")
    if enabled not in (0, 1, True, False):
        raise HTTPException(400, "enabled 取值无效")
    cur = conn.execute("UPDATE instruments SET enabled=? WHERE id=? AND user_id=?",
                       (1 if enabled in (1, True) else 0, instrument_id,
                        user["id"]))
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "标的不存在")
    return {"ok": True}


@app.delete("/api/instruments/{instrument_id}")
def delete_instrument(instrument_id: int, user=Depends(current_user),
                      conn=Depends(get_conn)):
    cur = conn.execute("DELETE FROM instruments WHERE id=? AND user_id=?",
                       (instrument_id, user["id"]))
    conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "标的不存在")
    return {"ok": True}


# ---------- dashboard / events / runs ----------

def _live_short_term(item):
    """Live short-term state computed from daily closes (5-min cached) —
    same indicator functions as the detector's close-window evaluation, so
    the dashboard/detail badges carry real readings before the first close
    run and between close runs."""
    try:
        secid, sina, proxy = baseline_gen.chart_request_for(
            item["code"], item["secid"], item["sina_code"])
        data = baseline_gen.daily_closes(secid, sina_code=sina,
                                         proxy_secid=proxy)
        closes = [b["close"] for b in data["bars"]]
        det = detector_module()
        out = {"st_macd": "", "st_rsi": ""}
        macd = det.daily_macd_state(closes)
        if macd:
            out["st_macd"] = macd
        rsi, _ = det.rsi14_series(closes)
        if rsi is not None:
            out["st_rsi"] = ("oversold" if rsi < 30
                             else "overbought" if rsi > 70 else "normal")
        return out
    except Exception:
        return {}


@app.get("/api/backdrop")
def backdrop(user=Depends(current_user), conn=Depends(get_conn)):
    """Normalized 90-day close series per enabled instrument — powers the
    dashboard backdrop field and row sparklines (both read the same
    5-min-cached daily closes)."""
    rows = conn.execute(
        "SELECT market, code, name, sina_code, secid FROM instruments "
        "WHERE user_id = ? AND enabled = 1 ORDER BY id",
        (user["id"],)).fetchall()
    out = []
    for r in rows:
        try:
            secid, sina, proxy = baseline_gen.chart_request_for(
                r["code"], r["secid"], r["sina_code"])
            tsym = baseline_gen.tencent_symbol_for(r["market"], r["code"],
                                                   r["secid"], r["sina_code"])
            data = baseline_gen.daily_closes(secid, sina_code=sina,
                                             proxy_secid=proxy, tencent_symbol=tsym)
            closes = [b["close"] for b in data["bars"]][-90:]
            if len(closes) < 20:
                continue
            lo, hi = min(closes), max(closes)
            span = (hi - lo) or 1.0
            entry = {"code": r["code"], "name": r["name"],
                     "ret": round(closes[-1] / closes[0] - 1, 4),
                     "series": [round((c - lo) / span, 4) for c in closes],
                     "intraday": None}
            # 当日分时（供行内 sparkline；失败回退 90 日日线）
            try:
                intr = baseline_gen.intraday_series(secid, sina_code=sina,
                                                    proxy_secid=proxy)
                if intr["points"]:
                    entry["intraday"] = {"pts": intr["points"],
                                         "preClose": intr["preClose"]}
            except Exception:
                pass
            out.append(entry)
        except Exception:
            continue
    return {"series": out}


@app.get("/api/dashboard")
def dashboard(user=Depends(current_user), conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT i.id, i.market, i.code, i.name, i.ma250, i.enabled, "
        "i.secid, i.sina_code, "
        "s.side, s.status, s.below_days, "
        "COALESCE(s.price, i.last_price) AS price, s.day_change_pct, "
        "s.volume_ratio, s.sell_signal, s.sell_note, "
        "s.buy_signal, s.buy_note, s.st_macd, s.st_rsi, s.st_note, s.updated_at "
        "FROM instruments i "
        "LEFT JOIN instrument_status s ON s.instrument_id = i.id "
        "WHERE i.user_id = ? ORDER BY i.market, i.code",
        (user["id"],)).fetchall()
    groups = {}
    for r in rows:
        d = dict(r)
        live = _live_short_term(d)
        d["st_macd"] = live.get("st_macd") or d["st_macd"] or ""
        d["st_rsi"] = live.get("st_rsi") or d["st_rsi"] or ""
        groups.setdefault(d["market"], []).append(d)

    # merge live quotes server-side so the FIRST paint carries real-time
    # price/change — no "--" flash waiting for the frontend poll
    try:
        all_rows = [item for items in groups.values() for item in items]
        q, _partial = quotes_mod.fetch_quotes(all_rows)
    except Exception:
        q = {}
    for items in groups.values():
        for d in items:
            live = q.get(d["code"])
            if live:
                if live.get("price") is not None:
                    d["price"] = live["price"]
                if live.get("day_change_pct") is not None:
                    d["day_change_pct"] = live["day_change_pct"]
    return {"groups": groups,
            "generated_at": datetime.now().isoformat(timespec="seconds")}


@app.get("/api/events")
def events(limit: int = 50, user=Depends(current_user), conn=Depends(get_conn)):
    limit = max(1, min(limit, 200))
    rows = conn.execute(
        "SELECT id, ts, event_type, market, code, name, price, ma250, dist_pct, "
        "day_change_pct, volume_ratio, weekly_macd, note "
        "FROM events WHERE user_id = ? ORDER BY ts DESC, id DESC LIMIT ?",
        (user["id"], limit)).fetchall()
    return {"events": [dict(r) for r in rows]}


@app.get("/api/runs")
def runs(limit: int = 50, conn=Depends(get_conn), _=Depends(current_user)):
    limit = max(1, min(limit, 200))
    rows = conn.execute(
        "SELECT id, ts, window, decision, reason, source FROM runs "
        "ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return {"runs": [dict(r) for r in rows]}


@app.post("/api/run-once")
def run_once(user=Depends(current_user)):
    """Force one detection cycle regardless of the window gate (debug/verify)."""
    result = get_scheduler().tick(force=True)
    return result


@app.get("/api/instruments/{instrument_id}/intraday")
def instrument_intraday(instrument_id: int, user=Depends(current_user),
                        conn=Depends(get_conn)):
    row = _own_instrument(conn, instrument_id, user["id"])
    secid, sina, proxy = baseline_gen.chart_request_for(
        row["code"], row["secid"], row["sina_code"])
    try:
        data = baseline_gen.intraday_series(secid, sina_code=sina,
                                            proxy_secid=proxy)
    except Exception as e:
        raise HTTPException(502, f"分时数据源请求失败：{e}")
    return {**data, "name": row["name"], "code": row["code"]}


# ---------- catalog / live quotes / detail ----------

@app.get("/api/index-catalog")
def index_catalog(_=Depends(current_user)):
    us_canonical = ["DJI", "IXIC", "SPX", "SOX"]
    return {
        "cn": [{"code": c, "name": n} for c, _, _, n in
               baseline_gen.CN_INDEX_CATALOG],
        "us": [{"code": c, "name": baseline_gen.US_INDEX_MAP[c][2]}
               for c in us_canonical],
    }


@app.get("/api/quotes")
def get_quotes(user=Depends(current_user), conn=Depends(get_conn)):
    rows = conn.execute(
        "SELECT market, code, sina_code, secid FROM instruments "
        "WHERE user_id = ? AND enabled = 1", (user["id"],)).fetchall()
    if not rows:
        return {"quotes": {}, "partial": False}
    q, partial = quotes_mod.fetch_quotes(rows)
    return {"quotes": q, "partial": partial}


def _own_instrument(conn, instrument_id, user_id):
    row = conn.execute(
        "SELECT * FROM instruments WHERE id = ? AND user_id = ?",
        (instrument_id, user_id)).fetchone()
    if not row:
        raise HTTPException(404, "标的不存在")
    return row


@app.get("/api/instruments/{instrument_id}")
def instrument_detail(instrument_id: int, user=Depends(current_user),
                      conn=Depends(get_conn)):
    row = _own_instrument(conn, instrument_id, user["id"])
    status = conn.execute(
        "SELECT * FROM instrument_status WHERE instrument_id = ?",
        (instrument_id,)).fetchone()
    events = conn.execute(
        "SELECT id, ts, event_type, price, ma250, dist_pct, day_change_pct, "
        "volume_ratio, note FROM events WHERE instrument_id = ? "
        "ORDER BY ts DESC, id DESC LIMIT 100", (instrument_id,)).fetchall()
    return {"item": dict(row), "status": dict(status) if status else None,
            "events": [dict(e) for e in events],
            "live_st": _live_short_term(row)}


@app.get("/api/instruments/{instrument_id}/kline")
def instrument_kline(instrument_id: int, user=Depends(current_user),
                     conn=Depends(get_conn)):
    row = _own_instrument(conn, instrument_id, user["id"])
    secid, sina, proxy = baseline_gen.chart_request_for(
        row["code"], row["secid"], row["sina_code"])
    tsym = baseline_gen.tencent_symbol_for(row["market"], row["code"],
                                           row["secid"], row["sina_code"])
    try:
        data = baseline_gen.daily_closes(secid, sina_code=sina,
                                         proxy_secid=proxy, tencent_symbol=tsym)
    except Exception as e:
        raise HTTPException(502, f"日K数据源请求失败：{e}")
    # short-term indicators off the same closes (detector's own functions —
    # same calibre as the close-window evaluation)
    closes = [b["close"] for b in data["bars"]]
    det = detector_module()
    dif, dea, macd_state = det.daily_macd_values(closes)
    rsi, _ = det.rsi14_series(closes)
    st = {
        "macd_state": macd_state,
        "dif": dif, "dea": dea,
        "rsi": rsi,
        "ma5": (sum(closes[-5:]) / 5) if len(closes) >= 5 else None,
        "ma10": (sum(closes[-10:]) / 10) if len(closes) >= 10 else None,
    }
    return {"bars": data["bars"], "source": data["source"],
            "ma250": row["ma250"], "st": st}


# ---------- static SPA ----------

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
