"""调度器：状态同步不被抹、周六基线重算可达、邮件摘要在锁外发送。"""
import json
from datetime import datetime

from webapp import scheduler
from webapp.scheduler import Scheduler, status_label


def _add_instrument(conn, user_id, market="cn_stock", code="600519",
                    sina="sh600519", secid="1.600519"):
    cur = conn.execute(
        "INSERT INTO instruments (user_id, market, code, name, sina_code, "
        "secid, created_at) VALUES (?,?,?,?,?,?,?)",
        (user_id, market, code, "测试标的", sina, secid, "2026-01-01T00:00:00"))
    return cur.lastrowid


def _add_user(conn, name="u1"):
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, role, approved, created_at) "
        "VALUES (?,?,?,?,?)", (name, "x", "user", 1, "2026-01-01T00:00:00"))
    return cur.lastrowid


def _set_status(conn, iid, side, status, below_days=0):
    conn.execute(
        "INSERT INTO instrument_status (instrument_id, side, status, below_days, "
        "updated_at) VALUES (?,?,?,?,?)", (iid, side, status, below_days,
                                           "2026-01-01T00:00:00"))
    conn.commit()


def test_status_label():
    assert status_label("above", False, 0) == "above"
    assert status_label("below", False, 0) == "pending"
    assert status_label("below", True, 1) == "day1"
    assert status_label("below", True, 3) == "effective"


def test_state_lookup_missing_entry():
    assert Scheduler._state_entry({}, "cn_stock", "600519") is None
    assert Scheduler._state_entry({"stocks": {}}, "cn_stock", "600519") is None
    state = {"stocks": {"600519": {"side": "below", "day1": True, "below_days": 2}}}
    assert Scheduler._state_entry(state, "cn_stock", "600519")["below_days"] == 2
    # 分组按市场：美股看 us_items，指数看 indices
    assert Scheduler._state_entry({"us_items": {"NVDA": {"side": "below"}}},
                                  "us_stock", "NVDA")["side"] == "below"
    assert Scheduler._state_entry({"indices": {"000300": {"side": "below"}}},
                                  "cn_index", "000300")["side"] == "below"


def test_sync_status_keeps_unknown_instruments(conn, monkeypatch, tmp_path):
    """状态文件里没有的标的：保留最后已知状态，不能被默认抹成 above。"""
    uid = _add_user(conn)
    iid_known = _add_instrument(conn, uid, code="600519", sina="sh600519")
    iid_gone = _add_instrument(conn, uid, code="000001", sina="sz000001")
    _set_status(conn, iid_known, "below", "day2", 2)
    _set_status(conn, iid_gone, "below", "day3", 3)

    state_file = tmp_path / "detector_state.json"
    state_file.write_text(json.dumps({
        "stocks": {"600519": {"side": "below", "day1": True, "below_days": 3}}
    }), encoding="utf-8")
    monkeypatch.setattr(scheduler.config, "DETECTOR_STATE_PATH", state_file)

    Scheduler()._sync_status_from_state(conn, "2026-11-06T15:05:00")
    conn.commit()

    known = conn.execute("SELECT side, status, below_days FROM instrument_status "
                         "WHERE instrument_id = ?", (iid_known,)).fetchone()
    assert (known["side"], known["status"], known["below_days"]) == ("below", "effective", 3)
    gone = conn.execute("SELECT side, status, below_days FROM instrument_status "
                        "WHERE instrument_id = ?", (iid_gone,)).fetchone()
    assert (gone["side"], gone["status"], gone["below_days"]) == ("below", "day3", 3)


def test_weekly_refresh_due_window():
    s = Scheduler()
    tz = scheduler.TZ
    assert s._weekly_refresh_due(datetime(2026, 11, 7, 6, 0, tzinfo=tz)) is True
    assert s._weekly_refresh_due(datetime(2026, 11, 7, 11, 59, tzinfo=tz)) is True
    assert s._weekly_refresh_due(datetime(2026, 11, 7, 12, 0, tzinfo=tz)) is False
    assert s._weekly_refresh_due(datetime(2026, 11, 7, 5, 59, tzinfo=tz)) is False
    assert s._weekly_refresh_due(datetime(2026, 11, 6, 9, 0, tzinfo=tz)) is False  # 周五


def test_tick_runs_weekly_refresh_on_saturday(monkeypatch):
    """周六 06:00-12:00 全在交易窗口之外：tick 必须先处理每周分支，
    否则这段逻辑永远不可达（window_of 返回 None 会被直接 skip）。"""
    s = Scheduler()
    ran = []
    monkeypatch.setattr(scheduler, "_load_detector", lambda: None)
    monkeypatch.setattr(s, "_weekly_refresh", lambda conn, now: ran.append(now))
    monkeypatch.setattr(s, "_tick_locked",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("周六分支不该跑检测器")))
    saturday = datetime(2026, 11, 7, 7, 30, tzinfo=scheduler.TZ)
    monkeypatch.setattr(scheduler, "datetime",
                        type("F", (datetime,), {"now": classmethod(
                            lambda cls, tz=None: saturday)}))
    out = s.tick()
    assert out["status"] == "ok" and out["reason"] == "weekly baseline refresh"
    assert len(ran) == 1


def test_tick_skips_outside_window(monkeypatch):
    s = Scheduler()
    monkeypatch.setattr(scheduler, "_load_detector",
                        lambda: type("D", (), {"window_of": staticmethod(lambda n: None)}))
    monday = datetime(2026, 11, 9, 3, 0, tzinfo=scheduler.TZ)
    monkeypatch.setattr(scheduler, "datetime",
                        type("F", (datetime,), {"now": classmethod(
                            lambda cls, tz=None: monday)}))
    out = s.tick()
    assert out["status"] == "skipped"


def test_send_mail_digests_uses_own_conn(conn, monkeypatch):
    """邮件摘要在 _run_lock 之外发送：函数自己建连接读邮箱。"""
    uid = _add_user(conn, "mailuser")
    conn.execute("UPDATE users SET email = ? WHERE id = ?", ("a@example.com", uid))
    conn.commit()
    sent = []
    monkeypatch.setattr(scheduler.mailer, "send_event_digest",
                        lambda email, evs: sent.append((email, len(evs))) or True)
    Scheduler()._send_mail_digests([
        {"user_id": uid, "event_type": "close_break_day1", "code": "600519",
         "name": "测试标的", "price": 1.0, "dist_pct": -1.0,
         "day_change_pct": -2.0, "ts": "2026-11-06T15:05:00"},
    ])
    assert sent == [("a@example.com", 1)]


def test_tick_pops_emitted_from_result(conn, monkeypatch):
    """tick 返回给接口层的结果里不含 emitted（含全部用户的明细）。"""
    s = Scheduler()
    tuesday = datetime(2026, 11, 10, 10, 30, tzinfo=scheduler.TZ)
    monkeypatch.setattr(scheduler, "datetime",
                        type("F", (datetime,), {"now": classmethod(
                            lambda cls, tz=None: tuesday)}))
    monkeypatch.setattr(scheduler, "_load_detector",
                        lambda: type("D", (), {"window_of": staticmethod(lambda n: "intraday")}))
    monkeypatch.setattr(s, "_tick_locked", lambda *a, **k: {
        "status": "ok", "decision": "wake", "reason": "r", "events": 1,
        "emitted": [{"user_id": 1, "code": "600519", "price": 1.0}]})
    mails = []
    monkeypatch.setattr(s, "_send_mail_digests", lambda emitted: mails.append(emitted))
    out = s.tick()
    assert "emitted" not in out
    assert out["status"] == "ok"
    assert len(mails) == 1   # 摘要在锁外发出
