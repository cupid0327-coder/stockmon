"""Scheduler thread: generate baselines from DB, run the detector
subprocess in every active trading window, ingest wake payloads into
SQLite. No LLM anywhere — the detector's own discipline (dedup, dual-source
checks, freshness) is preserved; this module only persists and attributes.
"""
import importlib.util
import json
import logging
import os
import subprocess
import sys
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

from . import baseline_gen, config, mailer

log = logging.getLogger("stockmon.scheduler")

TZ = ZoneInfo(config.TZ_NAME)

MODULE_CACHE = {}


def _load_detector():
    """Import the detector module from its file path (for window_of)."""
    if "mod" not in MODULE_CACHE:
        path = str(config.DETECTOR_SCRIPT)
        spec = importlib.util.spec_from_file_location(
            "a_share_breakdown", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        MODULE_CACHE["mod"] = mod
    return MODULE_CACHE["mod"]


def status_label(side, day1, below_days):
    """Human-usable status token from the detector's state-machine fields."""
    if side == "above":
        return "above"
    if not day1:
        return "pending"
    return "effective" if (below_days or 0) >= 3 else f"day{below_days or 1}"


class Scheduler:
    def __init__(self):
        self.stop_event = threading.Event()
        self._run_lock = threading.Lock()
        self._thread = None
        self._last_weekly = None  # date str of last Saturday baseline refresh

    # ---- baseline refresh (Saturday morning, Beijing) ----
    def _weekly_refresh_due(self, now):
        return now.weekday() == 5 and 6 <= now.hour < 12

    def _weekly_refresh(self, conn, now):
        from . import db as dbm
        today = now.strftime("%Y-%m-%d")
        if self._last_weekly == today:
            return
        rows = conn.execute(
            "SELECT id, market, code FROM instruments WHERE enabled = 1").fetchall()
        ok = fail = 0
        for r in rows:
            try:
                b = baseline_gen.build_instrument(r["market"], r["code"])
                conn.execute(
                    "UPDATE instruments SET ma250=?, ma250_asof=?, last_price=?, "
                    "weekly_macd=? WHERE id=?",
                    (b["ma250"], b["ma250_asof"], b["last_price"],
                     b["weekly_macd"], r["id"]))
                ok += 1
            except Exception as e:  # keep going; next week retries
                log.warning("weekly refresh %s failed: %s", r["code"], e)
                fail += 1
        conn.commit()
        self._last_weekly = today
        conn.execute(
            "INSERT INTO runs (ts, window, decision, reason) VALUES (?,?,?,?)",
            (now.isoformat(timespec="seconds"), "weekly",
             "info", f"每周基线重算：成功 {ok}，失败 {fail}"))
        conn.commit()

    # ---- one detection cycle ----
    def tick(self, force=False):
        """Run one cycle. force bypasses the window gate (for /api/run-once).
        Returns a small dict describing what happened."""
        now = datetime.now(TZ)
        win = _load_detector().window_of(now)
        if win is None and not force:
            return {"status": "skipped", "reason": "outside trading windows",
                    "at": now.isoformat(timespec="seconds")}

        with self._run_lock:
            return self._tick_locked(now, win, force)

    def _tick_locked(self, now, win, force):
        from . import db as dbm
        conn = dbm.connect()
        try:
            if self._weekly_refresh_due(now):
                try:
                    self._weekly_refresh(conn, now)
                except Exception as e:
                    log.warning("weekly refresh crashed: %s", e)

            counts = baseline_gen.generate_baseline_files(
                conn, asof=now.strftime("%Y-%m-%d"))
            conn.commit()

            env = dict(os.environ)
            env["BREAKDOWN_BASELINE_DIR"] = str(config.BASELINE_DIR)
            env["BREAKDOWN_STATE_PATH"] = str(config.DETECTOR_STATE_PATH)
            env.setdefault("PYTHONIOENCODING", "utf-8")
            try:
                proc = subprocess.run(
                    [sys.executable, "-X", "utf8", str(config.DETECTOR_SCRIPT),
                     "--state", str(config.DETECTOR_STATE_PATH)],
                    env=env, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=240,
                    cwd=str(config.BASE_DIR))
            except subprocess.TimeoutExpired:
                conn.execute(
                    "INSERT INTO runs (ts, window, decision, reason) VALUES (?,?,?,?)",
                    (now.isoformat(timespec="seconds"), win or "manual",
                     "error", "detector timed out after 240s"))
                conn.commit()
                return {"status": "error", "reason": "detector timeout"}

            data = self._parse_stdout(proc.stdout)
            ts = now.isoformat(timespec="seconds")
            if data is None:
                tail = (proc.stderr or "").strip().splitlines()[-3:]
                reason = " / ".join(tail) or f"no parseable stdout (rc={proc.returncode})"
                conn.execute(
                    "INSERT INTO runs (ts, window, decision, reason) VALUES (?,?,?,?)",
                    (ts, win or "manual", "error", reason[:500]))
                conn.commit()
                return {"status": "error", "reason": reason[:200]}

            conn.execute(
                "INSERT INTO runs (ts, window, decision, reason, source, payload_json) "
                "VALUES (?,?,?,?,?,?)",
                (ts, win or "manual", data.get("decision", "unknown"),
                 data.get("reason", ""), data.get("payload", {}).get("source", ""),
                 json.dumps(data.get("payload", {}), ensure_ascii=False)))
            run_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.commit()

            ingested = {"events": 0}
            if data.get("decision") == "wake":
                ingested = self._ingest(conn, run_id, ts, data.get("payload", {}))
            conn.commit()
            return {"status": "ok", "decision": data.get("decision"),
                    "reason": data.get("reason"), "window": win,
                    "counts": counts, **ingested}
        finally:
            conn.close()

    @staticmethod
    def _parse_stdout(stdout):
        for line in reversed((stdout or "").strip().splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
        return None

    # ---- payload ingestion ----
    def _ingest(self, conn, run_id, ts, payload):
        kind = payload.get("kind", "")
        emitted = []
        close = kind in ("close", "us_close")
        if kind in ("intraday", "close"):
            emitted += self._ingest_events(
                conn, run_id, ts, payload.get("events") or [], "cn_stock")
            emitted += self._ingest_events(
                conn, run_id, ts, payload.get("index_events") or [], "cn_index")
            if close:
                self._apply_rows(conn, ts, payload.get("rows") or [], "cn_stock")
                self._apply_rows(conn, ts, payload.get("index_rows") or [], "cn_index")
                self._sync_status_from_state(conn, ts)
        elif kind in ("us_intraday", "us_close"):
            for ev in payload.get("events") or []:
                mkt = "us_stock" if ev.get("kind") == "stock" else "us_index"
                emitted += self._ingest_events(conn, run_id, ts, [ev], mkt)
            if close:
                self._apply_rows(conn, ts, payload.get("rows") or [], None)
                self._sync_status_from_state(conn, ts)
        return {"events": len(emitted), "emitted": emitted}

    def _ingest_events(self, conn, run_id, ts, events, market):
        emitted = []
        for ev in events:
            owners = conn.execute(
                "SELECT id, user_id FROM instruments WHERE market=? AND code=?",
                (market, str(ev.get("code", "")))).fetchall()
            for inst in owners:
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO events (run_id, ts, event_type, "
                        "user_id, instrument_id, market, code, name, price, ma250, "
                        "dist_pct, day_change_pct, volume_ratio, weekly_macd, note) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (run_id, ts, ev.get("type", "unknown"), inst["user_id"],
                         inst["id"], market, str(ev.get("code", "")),
                         ev.get("name", ""), ev.get("price"), ev.get("ma250"),
                         ev.get("dist_pct"), ev.get("day_change_pct"),
                         ev.get("volume_ratio"), ev.get("weekly_macd"),
                         (ev.get("take_profit_note") or ev.get("add_note")
                          or ev.get("short_note") or ev.get("sell_note"))))
                    emitted.append({
                        "user_id": inst["user_id"], "ts": ts,
                        "event_type": ev.get("type", "unknown"),
                        "name": ev.get("name", ""), "code": str(ev.get("code", "")),
                        "price": ev.get("price"), "dist_pct": ev.get("dist_pct"),
                        "day_change_pct": ev.get("day_change_pct"),
                        "note": (ev.get("take_profit_note") or ev.get("add_note")
                                 or ev.get("short_note") or ev.get("sell_note")),
                    })
                except Exception as e:
                    log.warning("event insert failed: %s", e)
        return emitted

    def _apply_rows(self, conn, ts, rows, market):
        """Upsert instrument_status from close-window rows (sell fields ride
        on cn_stock rows; us rows carry a kind field and pass market=None)."""
        state = self._read_detector_state()
        for r in rows:
            mkt = market
            if mkt is None:
                mkt = "us_stock" if r.get("kind") == "stock" else "us_index"
            insts = conn.execute(
                "SELECT id, user_id FROM instruments WHERE market=? AND code=?",
                (mkt, str(r.get("code", "")))).fetchall()
            for inst in insts:
                side, day1, below_days = self._state_lookup(
                    state, mkt, r.get("code"), r.get("side"))
                status = status_label(side, day1, below_days)
                conn.execute(
                    "INSERT INTO instrument_status (instrument_id, side, status, "
                    "below_days, price, day_change_pct, volume_ratio, sell_signal, "
                    "sell_note, buy_signal, buy_note, st_macd, st_rsi, st_note, "
                    "updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(instrument_id) DO UPDATE SET side=excluded.side, "
                    "status=excluded.status, below_days=excluded.below_days, "
                    "price=excluded.price, day_change_pct=excluded.day_change_pct, "
                    "volume_ratio=COALESCE(excluded.volume_ratio, "
                    "instrument_status.volume_ratio), "
                    "sell_signal=COALESCE(excluded.sell_signal, instrument_status.sell_signal), "
                    "sell_note=COALESCE(excluded.sell_note, instrument_status.sell_note), "
                    "buy_signal=COALESCE(excluded.buy_signal, instrument_status.buy_signal), "
                    "buy_note=COALESCE(excluded.buy_note, instrument_status.buy_note), "
                    "st_macd=excluded.st_macd, st_rsi=excluded.st_rsi, "
                    "st_note=excluded.st_note, updated_at=excluded.updated_at",
                    (inst["id"], side, status, below_days, r.get("price"),
                     r.get("day_change_pct"), r.get("volume_ratio"),
                     r.get("sell_signal"), r.get("sell_note"),
                     r.get("buy_signal"), r.get("buy_note"),
                     r.get("st_macd"), r.get("st_rsi"), r.get("st_note"), ts))
                if r.get("price") is not None:
                    conn.execute("UPDATE instruments SET last_price=? WHERE id=?",
                                 (r["price"], inst["id"]))

    def _send_mail_digests(self, conn, emitted):
        """按用户邮箱合并发送本轮事件摘要；失败仅记日志。"""
        if not emitted:
            return
        try:
            by_user = {}
            for e in emitted:
                by_user.setdefault(e["user_id"], []).append(e)
            for uid, evs in by_user.items():
                row = conn.execute("SELECT email FROM users WHERE id = ?",
                                   (uid,)).fetchone()
                email = (row["email"] or "").strip() if row and row["email"] else None
                if email:
                    mailer.send_event_digest(email, evs)
        except Exception as e:
            log.warning("mail digest failed: %s", e)

    def _read_detector_state(self):
        try:
            return json.loads(config.DETECTOR_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _state_lookup(state, market, code, row_side):
        """Pull (side, day1, below_days) from the detector's state file."""
        group = ("us_items" if market.startswith("us_")
                 else "indices" if market == "cn_index" else "stocks")
        entry = (state.get(group) or {}).get(str(code)) or {}
        side = entry.get("side") or row_side or "above"
        return side, bool(entry.get("day1")), int(entry.get("below_days") or 0)

    def _sync_status_from_state(self, conn, ts):
        """Keep instrument_status aligned with the detector state even when a
        close wake carried no row for an instrument (defensive; cheap)."""
        state = self._read_detector_state()
        rows = conn.execute(
            "SELECT s.instrument_id, i.market, i.code FROM instrument_status s "
            "JOIN instruments i ON i.id = s.instrument_id").fetchall()
        for r in rows:
            side, day1, below_days = self._state_lookup(
                state, r["market"], r["code"], None)
            status = status_label(side, day1, below_days)
            conn.execute(
                "UPDATE instrument_status SET side=?, status=?, below_days=?, "
                "updated_at=? WHERE instrument_id=?",
                (side, status, below_days, ts, r["instrument_id"]))

    # ---- thread plumbing ----
    def loop(self):
        interval = max(30, config.POLL_INTERVAL_SECS)
        # wake up every 15s to check stop_event; full tick on the interval
        next_tick = 0.0
        while not self.stop_event.is_set():
            if self.stop_event.wait(timeout=15):
                break
            now = datetime.now(TZ).timestamp()
            if now >= next_tick:
                next_tick = now + interval
                try:
                    result = self.tick()
                    if result.get("status") != "skipped":
                        log.info("tick: %s", result)
                except Exception:
                    log.exception("scheduler tick failed")
        log.info("scheduler stopped")

    def start(self):
        self._thread = threading.Thread(target=self.loop, name="stockmon-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_event.set()


_SCHED = None


def get_scheduler():
    global _SCHED
    if _SCHED is None:
        _SCHED = Scheduler()
    return _SCHED


def detector_module():
    """Public access to the loaded detector module (indicator functions)."""
    return _load_detector()
