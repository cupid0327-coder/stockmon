#!/usr/bin/env python3
# A-share breakdown detector for the Muse hook pilot.
# Pure stdlib. Prints ONE JSON line: {"decision": "silent"|"wake", "reason": str, "payload": {...}}
# No LLM involved. State lives next to the hook state dir (or --state path for tests).
import argparse, json, os, re, sys, urllib.request
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

try:
    TZ = ZoneInfo("Asia/Shanghai")
    ET = ZoneInfo("America/New_York")
except Exception as e:  # Windows images without the tzdata pip package
    sys.stderr.write(f"zoneinfo database unavailable ({e}); install with: pip install tzdata\n")
    raise SystemExit(2)
def _hidden(name):
    # Baselines live in the Muse goal's hidden_files dir; a standalone
    # deployment (webapp scheduler) relocates them via BREAKDOWN_BASELINE_DIR.
    base = os.environ.get("BREAKDOWN_BASELINE_DIR")
    if base:
        return os.path.join(base, name)
    return os.path.expanduser(
        "~/workspace/goals/a-share-long-term-breakdown-monitoring/hidden_files/" + name)


BASELINE_PATH = _hidden("hook_baseline.json")
INDEX_BASELINE_PATH = _hidden("hook_baseline_indices.json")
US_BASELINE_PATH = _hidden("hook_baseline_us.json")
DEFAULT_STATE = (os.environ.get("BREAKDOWN_STATE_PATH")
                 or os.path.expanduser("~/hooks/state/a-share-breakdown-detector.json"))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"


def fetch(url, headers=None, timeout=10):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_sina(stocks):
    codes = ",".join(s["sina"] for s in stocks)
    raw = fetch("https://hq.sinajs.cn/list=" + codes,
                {"Referer": "https://finance.sina.com.cn/", "User-Agent": UA}).decode("gbk", "replace")
    out = {}
    for m in re.finditer(r'hq_str_([a-z]{2}\d{6})="([^"]*)"', raw):
        key, payload = m.group(1), m.group(2)
        f = payload.split(",")
        if len(f) < 32:
            continue
        try:
            price = float(f[3])
        except ValueError:
            continue
        try:
            prev_close = float(f[2])
        except ValueError:
            prev_close = None
        code = key[2:]
        out[code] = {"price": price, "prev_close": prev_close,
                     "volume_shares": float(f[8] or 0),
                     "quote_date": f[30], "quote_time": f[31]}
    return out


def fetch_em(stocks):
    secids = ",".join(s["secid"] for s in stocks)
    url = ("https://push2delay.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
           "&fields=f12,f2,f10&secids=" + secids)
    raw = fetch(url, {"User-Agent": UA}).decode("utf-8", "replace")
    data = json.loads(raw)
    out = {}
    diff = (data.get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    for item in diff:
        code = str(item.get("f12", "")).zfill(6)
        vr = item.get("f10")
        # ulist f10 with fltt=2 is the official volume-ratio value directly
        # (verified 2026-10-23: intraday values 1.28-1.75). Guard junk.
        try:
            vr = float(vr)
            if not (0 < vr < 30):
                vr = None
        except (TypeError, ValueError):
            vr = None
        try:
            price = float(item.get("f2"))
        except (TypeError, ValueError):
            price = None
        out[code] = {"price": price, "volume_ratio": vr}
    return out


def fetch_index_vr(secid, now):
    """Index volume ratio = today volume / mean prior-5-trading-day volumes,
    from EastMoney daily K. Indices have no stock-style ulist f10 ratio, so
    this is a different calibre and is only fetched on a close-cross day.
    Returns None on any failure (caller then treats day1 as unproven)."""
    beg = (now - timedelta(days=45)).strftime("%Y%m%d")
    end = now.strftime("%Y%m%d")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" + secid +
           "&klt=101&fqt=1&beg=" + beg + "&end=" + end +
           "&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57")
    for _ in range(3):
        try:
            raw = fetch(url, {"User-Agent": UA}, timeout=10).decode("utf-8", "replace")
            klines = (json.loads(raw).get("data") or {}).get("klines") or []
            if len(klines) < 6:
                return None
            rows = [k.split(",") for k in klines]
            if rows[-1][0] != now.strftime("%Y-%m-%d"):
                return None
            vols = [float(r[5]) for r in rows]
            avg = sum(vols[-6:-1]) / 5
            return vols[-1] / avg if avg > 0 else None
        except Exception:
            import time as _time
            _time.sleep(3)
    return None


def fetch_sina_us(items):
    """Sina US quotes (gb_ codes). Fields: [1] price, [2] change %, [10]
    volume (0 for SOX: the index has no volume), [25] ET timestamp,
    [26] prev close. Returns {item code: {...}}."""
    by_sina = {s["sina"][3:].upper(): s["code"] for s in items}
    codes = ",".join(s["sina"] for s in items)
    raw = fetch("https://hq.sinajs.cn/list=" + codes,
                {"Referer": "https://finance.sina.com.cn/", "User-Agent": UA}).decode("gbk", "replace")
    out = {}
    # US index codes contain "$" (verified 2026-11-07: gb_$dji, gb_$ixic,
    # gb_$inx, gb_$sox all come back with a "$" in the key), so the key
    # charset must include it or the indices never parse.
    for m in re.finditer(r'hq_str_gb_([a-z0-9$]+)="([^"]*)"', raw):
        code = by_sina.get(m.group(1).upper())
        if not code:
            continue
        f = m.group(2).split(",")
        if len(f) < 27:
            continue
        try:
            price = float(f[1])
        except ValueError:
            continue
        try:
            chg = float(f[2])
        except ValueError:
            chg = None
        try:
            vol = float(f[10] or 0)
        except ValueError:
            vol = 0
        try:
            pc = float(f[26])
        except ValueError:
            pc = None
        out[code] = {"price": price, "day_change_pct": chg,
                     "volume_shares": vol, "prev_close": pc,
                     "et_stamp": f[25] if len(f) > 25 else ""}
    return out


def fetch_us_klines(secid, end_date, days=60):
    """Daily K for a US item. EastMoney push2his for 1xx secids; Yahoo
    chart API for YAHOO:^SOX (EastMoney has no SOX K-line). Returns
    [{date, close, volume}] sorted by date, or [] on failure. Dates are
    US trading dates."""
    if secid.startswith("YAHOO:"):
        sym = secid.split(":", 1)[1]
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
               f"?range=3mo&interval=1d")
        for _ in range(2):
            try:
                raw = fetch(url, {"User-Agent": UA}, timeout=15).decode("utf-8", "replace")
                res = (json.loads(raw).get("chart") or {}).get("result") or []
                if not res:
                    return []
                r0 = res[0]
                ts = r0.get("timestamp") or []
                q = (r0.get("indicators") or {}).get("quote", [{}])[0]
                out = []
                for t, c, v in zip(ts, q.get("close") or [], q.get("volume") or []):
                    if c is None:
                        continue
                    out.append({"date": datetime.fromtimestamp(t, ET).strftime("%Y-%m-%d"),
                                "close": float(c), "volume": float(v or 0)})
                out.sort(key=lambda b: b["date"])
                return out
            except Exception:
                import time as _time
                _time.sleep(2)
        return []
    beg = (end_date - timedelta(days=days)).strftime("%Y%m%d")
    end = end_date.strftime("%Y%m%d")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" + secid +
           "&klt=101&fqt=1&beg=" + beg + "&end=" + end +
           "&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57")
    for _ in range(2):
        try:
            raw = fetch(url, {"User-Agent": UA}, timeout=10).decode("utf-8", "replace")
            klines = (json.loads(raw).get("data") or {}).get("klines") or []
            out = []
            for k in klines:
                f = k.split(",")
                if len(f) < 7:
                    continue
                out.append({"date": f[0], "close": float(f[2]), "volume": float(f[5])})
            out.sort(key=lambda b: b["date"])
            return out
        except Exception:
            import time as _time
            _time.sleep(2)
    return []


def fetch_us_close_vr(item, us_date, sina_volume):
    """US close volume ratio = today volume / mean prior-5-trading-day
    volumes. Today's volume prefers the K-line bar when it is already in
    (date == us_date), else the live Sina volume (final at close). Returns
    None when no volume exists anywhere (SOX) or on failure; the caller
    then treats day1 as unproven (except volume-waived items)."""
    try:
        kl = fetch_us_klines(item["secid"], us_date)
        if len(kl) < 6:
            return None
        if kl[-1]["date"] == us_date.strftime("%Y-%m-%d"):
            today_vol, priors = kl[-1]["volume"], [b["volume"] for b in kl[-6:-1]]
        else:
            today_vol, priors = sina_volume, [b["volume"] for b in kl[-5:]]
        if not today_vol or not priors or any(v <= 0 for v in priors):
            return None
        avg = sum(priors) / len(priors)
        return today_vol / avg if avg > 0 else None
    except Exception:
        return None


def fetch_daily_klines(secid, now, days=300):
    """EastMoney daily K (forward-adjusted, fqt=1), most recent ~300 calendar
    days. Used only by the close-window take-profit evaluation. Returns a
    list of {date, open, close, high, low, volume} sorted by date, or []
    on failure (caller then skips the rules that need K data)."""
    beg = (now - timedelta(days=days)).strftime("%Y%m%d")
    end = now.strftime("%Y%m%d")
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" + secid +
           "&klt=101&fqt=1&beg=" + beg + "&end=" + end +
           "&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57")
    for _ in range(2):
        try:
            raw = fetch(url, {"User-Agent": UA}, timeout=10).decode("utf-8", "replace")
            klines = (json.loads(raw).get("data") or {}).get("klines") or []
            out = []
            for k in klines:
                f = k.split(",")
                if len(f) < 7:
                    continue
                out.append({"date": f[0], "open": float(f[1]), "close": float(f[2]),
                            "high": float(f[3]), "low": float(f[4]), "volume": float(f[5])})
            out.sort(key=lambda b: b["date"])
            return out
        except Exception:
            import time as _time
            _time.sleep(2)
    return []


def _ema_full(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def weekly_closes(klines):
    """Friday (last-bar-of-week) closes from daily bars, as [(year, week), close]."""
    weeks = {}
    for b in klines:
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        weeks[d.isocalendar()[:2]] = b["close"]
    return sorted(weeks.items())


def macd_dif(weekly_close_list):
    e12 = _ema_full(weekly_close_list, 12)
    e26 = _ema_full(weekly_close_list, 26)
    return [a - b for a, b in zip(e12, e26)]


def eval_take_profit(row, klines):
    """Close-only take-profit (止赢卖出) evaluation. Thresholds confirmed by
    the user 2026-11-07 (webpage held only qualitative wording, no numbers):
    - resistance_stall (放量滞涨): price within +/-3% of MA250 or within 3%
      of the 60-day high, volume_ratio > 1.5, day change < +1%.
    - top_divergence (周线MACD顶背离): new 8-week closing high while the
      weekly MACD DIF is below its maximum over the prior 8 weeks
      (momentum peaked earlier, price did not).
    - ma10_break (跌破10日均线放量下跌): close < MA10 (10 closes incl.
      today), volume_ratio > 1.5, day change < 0.
    Priority on same-day multiple triggers: resistance_stall >
    top_divergence > ma10_break; the others are noted in sell_note.
    Returns (signal, note) or (None, None). Never raises."""
    try:
        price = row["price"]
        vr = row.get("volume_ratio")
        dchg = row.get("day_change_pct")
        ma = row.get("ma250")
        reasons = []
        ma10 = None
        high60 = None
        if klines:
            closes = [b["close"] for b in klines]
            if len(closes) >= 10:
                ma10 = sum(closes[-10:]) / 10
            highs = [b["high"] for b in klines[-60:]]
            if highs:
                high60 = max(highs)
        near_ma = bool(ma) and abs(price / ma - 1) <= 0.03
        near_high = bool(high60) and price >= high60 * 0.97
        if (near_ma or near_high) and vr and vr > 1.5 and dchg is not None and dchg < 1.0:
            where = "年线" if near_ma else "60日高点"
            reasons.append(("resistance_stall",
                            f"量比{vr:.2f}，距年线{row.get('dist_pct')}%（{where}附近），"
                            f"单日{dchg:+.2f}%（放量滞涨）"))
        if klines and len(klines) >= 50:
            weeks = weekly_closes(klines)
            if len(weeks) >= 10:
                closes_w = [c for _, c in weeks]
                difs = macd_dif(closes_w)
                prev = closes_w[-9:-1]
                prev_difs = difs[-9:-1]
                if closes_w[-1] > max(prev) and difs[-1] < max(prev_difs):
                    reasons.append(("top_divergence",
                                    f"8周新高{closes_w[-1]:.2f}，周线MACD DIF "
                                    f"{difs[-1]:.2f}低于近8周峰值{max(prev_difs):.2f}（顶背离）"))
        if ma10 and vr and vr > 1.5 and dchg is not None and dchg < 0 and price < ma10:
            reasons.append(("ma10_break",
                            f"收盘{price:.2f}跌破10日线{ma10:.2f}，量比{vr:.2f}，"
                            f"单日{dchg:+.2f}%（跌破10日线放量下跌）"))
        if not reasons:
            return None, None
        sig, note = reasons[0]
        if len(reasons) > 1:
            note += "；另触发：" + "、".join(r[0] for r in reasons[1:])
        return sig, note[:500]
    except Exception:
        return None, None


def eval_add_position(row, klines):
    """Close-only add-position (加仓) evaluation. Thresholds confirmed by
    the user 2026-11-07:
    - breakout_high60 (放量突破60日新高): close strictly above the prior 60
      closes, volume_ratio > 1.5, price at/above MA250.
    - ma250_pullback (回踩年线缩量企稳): close at/above MA250 and within 2%
      of it, 缩量 volume_ratio < 1.0.
    - recross_confirm (站回年线3日确认) is a state-machine milestone handled
      in the close loop, not here.
    Priority: breakout_high60 > ma250_pullback. Returns (signal, note) or
    (None, None). Never raises."""
    try:
        price = row["price"]
        vr = row.get("volume_ratio")
        ma = row.get("ma250")
        reasons = []
        if klines and len(klines) >= 61:
            closes = [b["close"] for b in klines]
            if (vr and vr > 1.5 and ma and price >= ma
                    and price > max(closes[-61:-1])):
                reasons.append(("breakout_high60",
                                f"收盘{price:.2f}创60日新高，量比{vr:.2f}，"
                                f"站上年线（放量突破）"))
        if (ma and vr and vr < 1.0 and price >= ma
                and price / ma - 1 <= 0.02):
            reasons.append(("ma250_pullback",
                            f"回踩年线缩量企稳：现价{price:.2f}距年线"
                            f"{row.get('dist_pct')}%以内，量比{vr:.2f}"
                            f"（缩量站住年线上方）"))
        if not reasons:
            return None, None
        sig, note = reasons[0]
        if len(reasons) > 1:
            note += "；另触发：" + "、".join(r[0] for r in reasons[1:])
        return sig, note[:500]
    except Exception:
        return None, None


def daily_macd_values(closes):
    """(dif, dea, state) of the last bar, or (None, None, None).
    Needs >= 40 bars for the EMA26 to be meaningful."""
    if closes is None or len(closes) < 40:
        return None, None, None
    e12 = _ema_full(closes, 12)
    e26 = _ema_full(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = _ema_full(dif, 9)
    return dif[-1], dea[-1], ("golden" if dif[-1] > dea[-1] else "dead")


def daily_macd_state(closes):
    """Current daily MACD state: 'golden' (DIF above DEA) or 'dead'."""
    return daily_macd_values(closes)[2]


def daily_macd_cross(closes):
    """'golden' / 'dead' when the LAST bar is the cross bar, else None."""
    if closes is None or len(closes) < 41:
        return None
    e12 = _ema_full(closes, 12)
    e26 = _ema_full(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = _ema_full(dif, 9)
    if dif[-2] <= dea[-2] and dif[-1] > dea[-1]:
        return "golden"
    if dif[-2] >= dea[-2] and dif[-1] < dea[-1]:
        return "dead"
    return None


def rsi14_series(closes, period=14):
    """Wilder RSI. Returns (current, previous) values or (None, None)."""
    if closes is None or len(closes) < period + 2:
        return None, None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas]
    losses = [max(-d, 0.0) for d in deltas]
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    vals = []
    for i in range(period, len(deltas)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
        if avg_l <= 0:
            vals.append(100.0 if avg_g > 0 else 50.0)
        else:
            vals.append(100.0 - 100.0 / (1.0 + avg_g / avg_l))
    if not vals:
        return None, None
    return vals[-1], vals[-2]


_KLINE_CACHE = {}  # (secid, date) -> (monotonic_ts, klines)


def fetch_daily_klines_cached(secid, now, days=300):
    """60s in-process cache shared by take-profit / add-position /
    short-term evaluation (one fetch per instrument per close poll)."""
    import time as _time
    key = (secid, now.strftime("%Y-%m-%d"))
    hit = _KLINE_CACHE.get(key)
    now_m = _time.monotonic()
    if hit and now_m - hit[0] < 60:
        return hit[1]
    kl = fetch_daily_klines(secid, now, days)
    _KLINE_CACHE[key] = (now_m, kl)
    return kl


def short_term_eval(rows, get_klines, st_state, events):
    """短线 (short-term) evaluation over a close-window row list — daily
    MACD cross (bar-date dedup) + RSI(14) zone transitions. Emits st_*
    events and sets row st_macd / st_rsi / st_note for the badge."""
    for row in rows:
        code = row["code"]
        st = st_state.setdefault(code, {"rsi_zone": "normal", "macd_date": None})
        kl = None
        try:
            kl = get_klines(row)
        except Exception:
            kl = None
        closes = [b["close"] for b in kl] if kl else []
        if not closes:
            row["st_macd"] = row["st_rsi"] = row["st_note"] = ""
            continue
        notes = []
        # daily MACD cross — dedup on the bar date so the same cross is
        # never emitted twice (multi-poll close window / restarts)
        cross = daily_macd_cross(closes)
        bar_date = kl[-1]["date"]
        if cross and st.get("macd_date") != bar_date:
            st["macd_date"] = bar_date
            kind = "golden" if cross == "golden" else "dead"
            txt = ("日线MACD金叉（买点）：DIF上穿DEA" if kind == "golden"
                   else "日线MACD死叉（卖点）：DIF下穿DEA")
            events.append({"type": "st_macd_" + kind, **row,
                           "short_signal": "macd_" + kind, "short_note": txt})
            notes.append(txt)
        # RSI zone state machine
        cur, _prev = rsi14_series(closes)
        zone = st.get("rsi_zone") or "normal"
        new_zone = "normal"
        if cur is not None:
            if cur < 30:
                new_zone = "oversold"
            elif cur > 70:
                new_zone = "overbought"
        if cur is not None and new_zone != zone:
            st["rsi_zone"] = new_zone
            # a one-day jump (crash/rally) can leave one zone AND enter
            # another — emit the leave event first, then the enter event
            if zone == "oversold" and new_zone != "oversold":
                txt = f"RSI({cur:.1f})离开超卖区（买点确认·回升）"
                events.append({"type": "st_rsi_oversold_out", **row,
                               "short_signal": "rsi_oversold_out",
                               "short_note": txt})
                notes.append(txt)
            elif zone == "overbought" and new_zone != "overbought":
                txt = f"RSI({cur:.1f})离开超买区（卖点确认·回落）"
                events.append({"type": "st_rsi_overbought_out", **row,
                               "short_signal": "rsi_overbought_out",
                               "short_note": txt})
                notes.append(txt)
            if new_zone == "oversold":
                txt = f"RSI({cur:.1f})进入超卖区（买点提示）"
                events.append({"type": "st_rsi_oversold_in", **row,
                               "short_signal": "rsi_oversold_in",
                               "short_note": txt})
                notes.append(txt)
            elif new_zone == "overbought":
                txt = f"RSI({cur:.1f})进入超买区（卖点提示）"
                events.append({"type": "st_rsi_overbought_in", **row,
                               "short_signal": "rsi_overbought_in",
                               "short_note": txt})
                notes.append(txt)
        row["st_macd"] = daily_macd_state(closes) or ""
        row["st_rsi"] = new_zone if cur is not None else ""
        row["st_note"] = "；".join(notes)


def window_of(now):
    # US windows in ET (checked first: a Friday US session runs to 05:00
    # Saturday Beijing time, so the Beijing weekend check must not block it).
    now_et = now.astimezone(ET)
    if now_et.weekday() < 5:
        et_t = now_et.time()
        if dtime(9, 30) <= et_t < dtime(16, 0):
            return "us_intraday"
        if dtime(16, 0) <= et_t < dtime(16, 35):
            return "us_close"
    t = now.time()
    if now.weekday() >= 5:
        return None
    if dtime(9, 30) <= t < dtime(11, 30) or dtime(13, 0) <= t < dtime(15, 0):
        return "intraday"
    if dtime(15, 0) <= t < dtime(15, 35):
        return "close"
    return None


def prune_days(d, today, keep=10):
    """Drop day-keyed state entries (intraday_warned / recrossed maps)
    older than `keep` days so the state file stops growing unboundedly.
    Dedup only ever looks up today's key, so old entries are safe to drop."""
    cutoff = (today - timedelta(days=keep)).strftime("%Y-%m-%d")
    for k in [k for k in d if str(k) < cutoff]:
        del d[k]


def us_main(args, now, win, emit, no_write):
    """US branch: NVDA + 4 US indices, same discipline as A-share indices
    (MA250 + volume ratio > 1.5 + weekly MACD dead cross + 3 close days).
    No webpage, no take-profit: events only, main chat only. The US
    trading date is the ET date; the close round runs 05:00-05:35 Beijing
    (16:00-16:35 ET). The close round wakes once per US trading day so
    the worker can write the webpage snapshot (like the A-share close);
    it sends a message only when there is an event."""
    now_et = now.astimezone(ET)
    us_today = now_et.strftime("%Y-%m-%d")
    try:
        us_baseline = json.load(open(US_BASELINE_PATH, encoding="utf-8"))
    except Exception as e:
        return emit("silent", f"us baseline load failed: {e}")
    items = us_baseline.get("items") or []
    state = {}
    if os.path.exists(args.state):
        try:
            state = json.load(open(args.state, encoding="utf-8"))
        except Exception:
            state = {}
    ust = state.setdefault("us_items", {})
    for s in items:
        ust.setdefault(s["code"], {"side": s["side"], "below_since": None,
                                   "below_days": 0, "day1": False, "effective_sent": False})
    us_warned = state.setdefault("us_intraday_warned", {})
    us_recrossed = state.setdefault("us_intraday_recrossed", {})
    prune_days(us_warned, now_et.date())
    prune_days(us_recrossed, now_et.date())

    quotes, source_note = {}, ""
    if args.snapshot:
        snap = json.load(open(args.snapshot, encoding="utf-8"))
        codes = {s["code"] for s in items}
        for code, q in snap.items():
            if code in codes:
                quotes[code] = {"price": float(q["price"]),
                                "day_change_pct": q.get("day_change_pct"),
                                "volume_shares": q.get("volume_shares", 0),
                                "volume_ratio": q.get("volume_ratio"),
                                "prev_close": q.get("prev_close"),
                                "et_stamp": q.get("et_stamp", "")}
        source_note = "snapshot(test)"
    else:
        try:
            sina = fetch_sina_us(items)
        except Exception as e:
            return emit("silent", f"us sina fetch failed: {e}")
        if len(sina) < len(items):
            return emit("silent", f"us sina incomplete: {len(sina)}/{len(items)}")
        # trading-day freshness: Sina ET stamp must be today's US date
        # (catches US holidays, when quotes freeze on the last trading day)
        stamps = {}
        for code, q in sina.items():
            m = re.search(r"([A-Za-z]{3})\s+(\d{1,2})", q.get("et_stamp") or "")
            if not m:
                stamps[code] = None
                continue
            try:
                mon = datetime.strptime(m.group(1), "%b").month
            except ValueError:
                stamps[code] = None
                continue
            stamps[code] = (mon, int(m.group(2)))
        et_md = (now_et.month, now_et.day)
        if any(v != et_md for v in stamps.values()):
            return emit("silent", f"us quote ET stamp {stamps} != today {et_md} (holiday/stale)")
        quotes = sina
        source_note = f"us sina {len(sina)}/{len(items)}"

    events, rows = [], []
    for s in items:
        code = s["code"]
        q = quotes.get(code)
        if not q:
            continue
        price = q["price"]
        ma = s["ma250"]
        side = "above" if price >= ma else "below"
        dist = (price / ma - 1) * 100
        row = {"code": code, "name": s["name"], "kind": s.get("kind", "index"),
               "price": price, "ma250": ma, "dist_pct": round(dist, 2),
               "day_change_pct": q.get("day_change_pct"), "side": side,
               "volume_ratio": None, "weekly_macd": s["weekly_macd"],
               "status_baseline": s["status"]}
        rows.append(row)
        entry = ust[code]
        prev_side = entry.get("side", s["side"])
        if win == "us_intraday":
            if prev_side == "above" and side == "below":
                if code not in us_warned.setdefault(us_today, []):
                    us_warned[us_today].append(code)
                    events.append({"type": "us_intraday_warning", **row})
            elif prev_side == "below" and side == "above":
                if code not in us_recrossed.setdefault(us_today, []):
                    us_recrossed[us_today].append(code)
                    events.append({"type": "us_intraday_recross", **row})
        else:  # us_close
            if prev_side == "above" and side == "below":
                vr = q.get("volume_ratio") if args.snapshot else \
                    fetch_us_close_vr(s, now_et.date(), q.get("volume_shares") or 0)
                row["volume_ratio"] = vr
                waived = bool(s.get("volume_waived"))
                day1 = bool(s["weekly_macd"] == "dead_cross" and
                            (waived or (vr and vr > 1.5)))
                entry.update({"side": "below", "below_since": us_today,
                              "below_days": 1, "day1": day1, "effective_sent": False})
                events.append({"type": "us_close_break_day1" if day1 else
                               "us_close_break_pending", **row})
            elif prev_side == "below" and side == "above":
                entry.update({"side": "above", "below_since": None,
                              "below_days": 0, "day1": False, "effective_sent": False})
                events.append({"type": "us_close_recross_release", **row})
            elif side == "below" and entry.get("day1"):
                if entry.get("last_counted") != us_today:
                    entry["below_days"] = int(entry.get("below_days") or 1) + 1
                    entry["last_counted"] = us_today
                if entry["below_days"] >= 3 and not entry.get("effective_sent"):
                    entry["effective_sent"] = True
                    events.append({"type": "us_effective_break", **row,
                                   "below_days": entry["below_days"]})
            elif side == "above":
                entry.update({"side": "above", "below_since": None,
                              "below_days": 0, "day1": False})

    # --- short-term (短线) for US items: daily MACD cross + RSI zones ---
    if win == "us_close":
        us_by_code = {s["code"]: s for s in items}
        short_term_eval(rows, lambda r: fetch_us_klines(
            us_by_code[r["code"]]["secid"], now_et.date(), days=120),
            state.setdefault("short_term_us", {}), events)

    if not no_write:
        os.makedirs(os.path.dirname(args.state), exist_ok=True)
        tmp = args.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, args.state)

    if win == "us_intraday":
        if events:
            return emit("wake", f"us intraday events: {len(events)}",
                        {"kind": "us_intraday", "date": us_today, "events": events,
                         "source": source_note})
        return emit("silent", f"us intraday no cross events ({source_note})")

    # us close: wake once per US trading day for the web snapshot write
    # (like the A-share close); the worker sends a message only on events.
    if state.get("us_close_done_date") == us_today and not args.snapshot:
        if not events:
            return emit("silent", "us close wake already done today")
        # late close events after the day's first wake still wake so the
        # worker delivers them instead of silently dropping them.
    if not no_write:
        state["us_close_done_date"] = us_today
        tmp = args.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, args.state)
    return emit("wake", "us daily close snapshot",
                {"kind": "us_close", "date": us_today, "events": events, "rows": rows,
                 "source": source_note, "us_baseline_date": us_baseline.get("baseline_date"),
                 "is_friday": now_et.weekday() == 4})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--at", help="override now, ISO e.g. 2026-10-26T10:00:00 (tests)")
    ap.add_argument("--snapshot", help="JSON file {code: {price, volume_ratio}}; skips live fetch (tests)")
    ap.add_argument("--state", default=DEFAULT_STATE)
    ap.add_argument("--no-write", action="store_true")
    args = ap.parse_args()

    no_write = args.no_write or os.environ.get("HATCH_HOOK_DRY_RUN") == "1"
    now = datetime.fromisoformat(args.at) if args.at else datetime.now(TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    today = now.strftime("%Y-%m-%d")

    def emit(decision, reason, payload=None):
        print(json.dumps({"decision": decision, "reason": reason, "payload": payload or {}},
                         ensure_ascii=False))
        return 0

    win = window_of(now)
    if win is None:
        return emit("silent", f"outside trading windows ({now:%Y-%m-%d %H:%M} CST; "
                              f"US {now.astimezone(ET):%Y-%m-%d %H:%M} ET)")
    if win in ("us_intraday", "us_close"):
        return us_main(args, now, win, emit, no_write)

    baseline = json.load(open(BASELINE_PATH, encoding="utf-8"))
    stocks = baseline["stocks"]
    by_code = {s["code"]: s for s in stocks}

    state = {}
    if os.path.exists(args.state):
        try:
            state = json.load(open(args.state, encoding="utf-8"))
        except Exception:
            state = {}
    st = state.setdefault("stocks", {})
    for s in stocks:
        st.setdefault(s["code"], {"side": s["side"], "below_since": None,
                                  "below_days": 0, "day1": False, "effective_sent": False})
    warned = state.setdefault("intraday_warned", {})
    recrossed = state.setdefault("intraday_recrossed", {})
    prune_days(warned, now.date())
    prune_days(recrossed, now.date())

    # --- get quotes ---
    quotes, source_note = {}, ""
    if args.snapshot:
        snap = json.load(open(args.snapshot, encoding="utf-8"))
        for code, q in snap.items():
            quotes[code] = {"price": float(q["price"]),
                            "volume_ratio": q.get("volume_ratio"),
                            "em_price": q.get("em_price"),
                            "prev_close": q.get("prev_close")}
        source_note = "snapshot(test)"
    else:
        try:
            sina = fetch_sina(stocks)
        except Exception as e:
            return emit("silent", f"sina fetch failed: {e}")
        if len(sina) < len(stocks):
            return emit("silent", f"sina incomplete: {len(sina)}/{len(stocks)}")
        # trading-day freshness: quotes must be stamped today
        qdates = {q["quote_date"] for q in sina.values()}
        if qdates != {today}:
            return emit("silent", f"quote date {sorted(qdates)} != today {today} (holiday/stale)")
        em = {}
        try:
            em = fetch_em(stocks)
        except Exception:
            em = {}
        for code, q in sina.items():
            e = em.get(code) or {}
            quotes[code] = {"price": q["price"], "prev_close": q.get("prev_close"),
                            "volume_ratio": e.get("volume_ratio"),
                            "em_price": e.get("price")}
        source_note = (f"sina {len(sina)}/{len(stocks)}, "
                       f"eastmoney {len(em)}/{len(stocks)}")

    # --- index quotes (separate from stocks: failures here must not break stocks) ---
    indices, index_baseline_date = [], None
    try:
        idx_base = json.load(open(INDEX_BASELINE_PATH, encoding="utf-8"))
        indices = idx_base.get("indices") or []
        index_baseline_date = idx_base.get("baseline_date")
    except Exception:
        indices = []
    index_quotes, index_source_note = {}, "indices: no baseline"
    if indices:
        if args.snapshot:
            index_quotes = {s["code"]: quotes[s["code"]] for s in indices if s["code"] in quotes}
            index_source_note = "indices: snapshot(test)"
        else:
            try:
                sina_idx = fetch_sina(indices)
                qdates_idx = {q["quote_date"] for q in sina_idx.values()}
                if qdates_idx == {today}:
                    for code, q in sina_idx.items():
                        index_quotes[code] = {"price": q["price"], "volume_ratio": None,
                                              "em_price": None}
                    index_source_note = f"indices: sina {len(sina_idx)}/5"
                else:
                    index_source_note = f"indices: quote date {sorted(qdates_idx)} != today"
            except Exception as e:
                index_source_note = f"indices: sina fetch failed: {e}"

    events = []
    rows = []
    for s in stocks:
        code = s["code"]
        q = quotes.get(code)
        if not q:
            continue
        price = q["price"]
        ma = s["ma250"]
        side = "above" if price >= ma else "below"
        dist = (price / ma - 1) * 100
        em_p = q.get("em_price")
        cross_ok = em_p is None or abs(em_p - price) / price < 0.005
        pc = q.get("prev_close")
        day_chg = round((price / pc - 1) * 100, 2) if pc else None
        row = {"code": code, "name": s["name"], "price": price, "ma250": ma,
               "ma250_is_proxy": s.get("ma250_is_proxy", False),
               "dist_pct": round(dist, 2), "day_change_pct": day_chg, "side": side,
               "volume_ratio": q.get("volume_ratio"),
               "weekly_macd": s["weekly_macd"], "status_baseline": s["status"],
               "em_price": em_p, "dual_source_ok": bool(cross_ok)}
        rows.append(row)
        prev_side = st[code].get("side", s["side"])

        if win == "intraday":
            if prev_side == "above" and side == "below":
                if code not in warned.setdefault(today, []):
                    warned[today].append(code)
                    events.append({"type": "intraday_warning", **row})
            elif prev_side == "below" and side == "above":
                if code not in recrossed.setdefault(today, []):
                    recrossed[today].append(code)
                    events.append({"type": "intraday_recross", **row})
            # intraday: keep baseline side as reference; do not flip on noise.
        else:  # close window: settle the day on close prices
            entry = st[code]
            if prev_side == "above" and side == "below":
                day1 = bool(q.get("volume_ratio") and q["volume_ratio"] > 1.5
                            and s["weekly_macd"] == "dead_cross")
                entry.update({"side": "below", "below_since": today,
                              "below_days": 1, "day1": day1, "effective_sent": False,
                              "above_days": 0, "add_confirm_sent": False})
                events.append({"type": "close_break_day1" if day1 else "close_break_pending", **row})
            elif prev_side == "below" and side == "above":
                # release day counts as day 1 of the add-position
                # 3-day confirmation (mirror of below_days)
                entry.update({"side": "above", "below_since": None,
                              "below_days": 0, "day1": False, "effective_sent": False,
                              "above_days": 1, "add_confirm_sent": False,
                              "add_last_counted": today})
                events.append({"type": "close_recross_release", **row})
            elif side == "below" and entry.get("day1"):
                if entry.get("last_counted") != today:
                    entry["below_days"] = int(entry.get("below_days") or 1) + 1
                    entry["last_counted"] = today
                if entry["below_days"] >= 3 and not entry.get("effective_sent"):
                    entry["effective_sent"] = True
                    events.append({"type": "effective_break", **row,
                                   "below_days": entry["below_days"]})
            elif side == "above":
                if entry.get("above_days") and entry.get("add_last_counted") != today:
                    entry["above_days"] = int(entry["above_days"]) + 1
                    entry["add_last_counted"] = today
                if (entry.get("above_days") or 0) >= 3 \
                        and not entry.get("add_confirm_sent"):
                    entry["add_confirm_sent"] = True
                    row["buy_signal"] = "recross_confirm"
                    row["buy_note"] = "站回年线3日确认"
                    events.append({"type": "close_add_position", **row,
                                   "add_signal": "recross_confirm",
                                   "add_note": row["buy_note"]})
                entry.update({"side": "above", "below_since": None,
                              "below_days": 0, "day1": False})

    # --- take-profit (close window only; indices excluded) ---
    # Detector-side dedup: alert only on inactive -> active transition.
    # The row's sell_signal/sell_note are always set at close so the
    # webpage badge matches the detector; only a NEW trigger emits an event.
    if win == "close":
        tp = state.setdefault("take_profit", {})
        for row in rows:
            code = row["code"]
            sig, note = None, None
            try:
                kl = fetch_daily_klines(by_code[code]["secid"], now)
                sig, note = eval_take_profit(row, kl)
            except Exception:
                sig, note = None, None
            row["sell_signal"] = sig or "none"
            row["sell_note"] = note
            cur = tp.setdefault(code, {"active": False, "signal": "none",
                                       "sent_date": None})
            if sig:
                if not cur.get("active"):
                    events.append({"type": "close_take_profit", **row,
                                   "take_profit_signal": sig,
                                   "take_profit_note": note})
                    cur.update({"active": True, "signal": sig, "sent_date": today})
                else:
                    cur.update({"signal": sig})
            else:
                cur.update({"active": False, "signal": "none"})

    # --- add-position (close window; stocks only; indices excluded) ---
    # recross_confirm is emitted by the state machine above; pullback /
    # breakout are kline-evaluated here with the same inactive->active
    # dedup as take-profit. Display priority: recross_confirm first.
    if win == "close":
        ap = state.setdefault("add_position", {})
        for row in rows:
            code = row["code"]
            entry = st[code]
            if entry.get("add_confirm_sent") and row.get("side") == "above":
                row["buy_signal"] = "recross_confirm"
                row["buy_note"] = "站回年线3日确认"
                # sync the kline dedup silently so an overlapping pullback
                # episode does not double-alert after the milestone
                try:
                    kl = fetch_daily_klines_cached(by_code[code]["secid"], now)
                    sig, _ = eval_add_position(row, kl)
                except Exception:
                    sig = None
                cur = ap.setdefault(code, {"active": False, "signal": "none"})
                cur.update({"active": bool(sig), "signal": sig or "none"})
                continue
            sig, note = None, None
            try:
                kl = fetch_daily_klines(by_code[code]["secid"], now)
                sig, note = eval_add_position(row, kl)
            except Exception:
                sig, note = None, None
            row["buy_signal"] = sig or "none"
            row["buy_note"] = note
            cur = ap.setdefault(code, {"active": False, "signal": "none"})
            if sig:
                if not cur.get("active"):
                    events.append({"type": "close_add_position", **row,
                                   "add_signal": sig, "add_note": note})
                    cur.update({"active": True, "signal": sig})
                else:
                    cur.update({"signal": sig})
            else:
                cur.update({"active": False, "signal": "none"})

    # --- short-term (短线): daily MACD cross + RSI zones, stocks ---
    if win == "close":
        short_term_eval(rows, lambda r: fetch_daily_klines_cached(
            by_code[r["code"]]["secid"], now),
            state.setdefault("short_term", {}), events)

    # --- indices: same 4-condition discipline, but NO backfilled history ---
    # Initial state is below with day1=False, so staying below emits nothing.
    # Only a close reclaim (release) or a post-reclaim new break (day1 ->
    # 3-day confirmation -> effective) emits. Intraday crossings warn only.
    # Add-position / take-profit signals never enter index events.
    ist = state.setdefault("indices", {})
    for s in indices:
        ist.setdefault(s["code"], {"side": s["side"], "below_since": None,
                                   "below_days": 0, "day1": False, "effective_sent": False})
    index_warned = state.setdefault("index_intraday_warned", {})
    index_recrossed = state.setdefault("index_intraday_recrossed", {})
    prune_days(index_warned, now.date())
    prune_days(index_recrossed, now.date())
    index_events, index_rows = [], []
    for s in indices:
        code = s["code"]
        q = index_quotes.get(code)
        if not q:
            continue
        price = q["price"]
        ma = s["ma250"]
        side = "above" if price >= ma else "below"
        dist = (price / ma - 1) * 100
        row = {"code": code, "name": s["name"], "kind": "index", "price": price,
               "ma250": ma, "ma250_source": s.get("ma250_source"),
               "ma250_tradingview_verified": s.get("ma250_tradingview_verified", False),
               "dist_pct": round(dist, 2), "side": side,
               "volume_ratio": q.get("volume_ratio"),
               "weekly_macd": s["weekly_macd"], "status_baseline": s["status"],
               "em_price": None, "dual_source_ok": None}
        index_rows.append(row)
        entry = ist[code]
        prev_side = entry.get("side", s["side"])
        if win == "intraday":
            if prev_side == "above" and side == "below":
                if code not in index_warned.setdefault(today, []):
                    index_warned[today].append(code)
                    index_events.append({"type": "index_intraday_warning", **row})
            elif prev_side == "below" and side == "above":
                if code not in index_recrossed.setdefault(today, []):
                    index_recrossed[today].append(code)
                    index_events.append({"type": "index_intraday_recross", **row})
        else:  # close window
            if prev_side == "above" and side == "below":
                vr = q.get("volume_ratio")
                if vr is None and not args.snapshot:
                    vr = fetch_index_vr(s["secid"], now)
                    row["volume_ratio"] = vr
                day1 = bool(vr and vr > 1.5 and s["weekly_macd"] == "dead_cross")
                entry.update({"side": "below", "below_since": today,
                              "below_days": 1, "day1": day1, "effective_sent": False})
                index_events.append({"type": "index_close_break_day1" if day1 else
                                     "index_close_break_pending", **row})
            elif prev_side == "below" and side == "above":
                entry.update({"side": "above", "below_since": None,
                              "below_days": 0, "day1": False, "effective_sent": False})
                index_events.append({"type": "index_close_recross_release", **row})
            elif side == "below" and entry.get("day1"):
                if entry.get("last_counted") != today:
                    entry["below_days"] = int(entry.get("below_days") or 1) + 1
                    entry["last_counted"] = today
                if entry["below_days"] >= 3 and not entry.get("effective_sent"):
                    entry["effective_sent"] = True
                    index_events.append({"type": "index_effective_break", **row,
                                         "below_days": entry["below_days"]})
            elif side == "above":
                entry.update({"side": "above", "below_since": None,
                              "below_days": 0, "day1": False})

    # --- short-term (短线) for indices too: daily MACD + RSI ---
    if win == "close" and index_rows:
        idx_by_code = {s["code"]: s for s in indices}
        short_term_eval(index_rows, lambda r: fetch_daily_klines_cached(
            idx_by_code[r["code"]]["secid"], now, days=120),
            state.setdefault("short_term_idx", {}), index_events)

    if not no_write:
        os.makedirs(os.path.dirname(args.state), exist_ok=True)
        tmp = args.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, args.state)

    if win == "intraday":
        all_events = events + index_events
        if all_events:
            return emit("wake", f"intraday events: {len(all_events)}",
                        {"kind": "intraday", "date": today, "events": all_events,
                         "index_events": index_events, "index_rows": index_rows,
                         "source": source_note + "; " + index_source_note})
        return emit("silent", f"intraday no cross events ({source_note}; {index_source_note})")

    # close window: wake once per trading day for web sync + adjudication
    if state.get("close_done_date") == today and not args.snapshot:
        if not events and not index_events:
            return emit("silent", "close wake already done today")
        # late close events after the day's first wake (typically a
        # take-profit whose K-line fetch only succeeded on a later poll)
        # still wake so the worker delivers them instead of losing them.
    if not no_write:
        state["close_done_date"] = today
        tmp = args.state + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, args.state)
    return emit("wake", "daily close snapshot",
                {"kind": "close", "date": today, "events": events, "rows": rows,
                 "index_events": index_events, "index_rows": index_rows,
                 "source": source_note + "; " + index_source_note,
                 "baseline_date": baseline["baseline_date"],
                 "index_baseline_date": index_baseline_date if indices else None,
                 "is_friday": now.weekday() == 4})


if __name__ == "__main__":
    sys.exit(main())
