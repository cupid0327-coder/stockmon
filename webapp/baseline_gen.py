"""Instrument code resolution + baseline computation (MA250, weekly MACD).

Same data calibre as the detector: EastMoney daily K (forward-adjusted) for
everything, Yahoo chart API for ^SOX. Pure stdlib HTTP.
"""
import json
import logging
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

log = logging.getLogger("stockmon.baseline_gen")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
ET = timezone(timedelta(hours=-5))  # adequate for ISO-week dating of US bars


class BaselineError(ValueError):
    pass


def _f(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _fetch(url, headers=None, timeout=12):
    """urllib 优先；失败时回退系统 curl——两者 TLS 指纹不同，
    部分数据源 WAF（如东财）会按指纹断连，curl 路径实测可绕过。"""
    req = urllib.request.Request(url, headers=headers or {})
    last_err = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        last_err = e
    import subprocess
    cmd = ["curl", "-s", "-m", str(max(5, timeout)), "--compressed", "-L"]
    for k, v in (headers or {}).items():
        cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout + 5)
        if r.returncode == 0 and r.stdout:
            return r.stdout
    except Exception:
        pass
    raise last_err


def _em_quote(secid):
    """{name, price} via EastMoney delayed ulist; None when unknown secid."""
    url = ("https://push2delay.eastmoney.com/api/qt/ulist.np/get?fltt=2&invt=2"
           "&fields=f12,f2,f14&secids=" + urllib.parse.quote(secid))
    raw = _fetch(url, {"User-Agent": UA}).decode("utf-8", "replace")
    diff = (json.loads(raw).get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    if not diff:
        return None
    item = diff[0]
    try:
        return {"name": str(item.get("f14") or ""), "price": float(item.get("f2"))}
    except (TypeError, ValueError):
        return None


def em_klines(secid, days=430):
    """Daily bars (forward-adjusted) as [{date, open, close, high, low}]."""
    end = datetime.now(timezone.utc).astimezone()
    beg = end - timedelta(days=days)
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=" +
           urllib.parse.quote(secid) +
           "&klt=101&fqt=1&beg=" + beg.strftime("%Y%m%d") + "&end=" +
           end.strftime("%Y%m%d") +
           "&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57")
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
    import time as _time
    last_err = None
    for _attempt in range(2):
        try:
            raw = _fetch(url, headers).decode("utf-8", "replace")
            klines = (json.loads(raw).get("data") or {}).get("klines") or []
            out = []
            for k in klines:
                f = k.split(",")
                if len(f) < 7:
                    continue
                try:
                    out.append({"date": f[0], "open": float(f[1]), "close": float(f[2]),
                                "high": float(f[3]), "low": float(f[4]),
                                "volume": float(f[5] or 0)})
                except ValueError:
                    continue
            out.sort(key=lambda b: b["date"])
            return out
        except Exception as e:
            last_err = e
            _time.sleep(1)
    raise last_err if last_err else RuntimeError("em_klines failed")


def yahoo_klines(symbol, months=18):
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol)}"
           f"?range={months}mo&interval=1d")
    raw = _fetch(url, {"User-Agent": UA}, timeout=15).decode("utf-8", "replace")
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


def weekly_closes(klines):
    """Last close per ISO week, as sorted [(iso (year, week), close)]."""
    weeks = {}
    for b in klines:
        d = datetime.strptime(b["date"], "%Y-%m-%d").date()
        weeks[d.isocalendar()[:2]] = b["close"]
    return sorted(weeks.items())


def _ema(values, period):
    k = 2 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _sina_gb_price(sina_code):
    """Live price for a Sina US gb_ code, e.g. 'gb_$sox'."""
    raw = _fetch("https://hq.sinajs.cn/list=" + sina_code,
                 {"Referer": "https://finance.sina.com.cn/", "User-Agent": UA}
                 ).decode("gbk", "replace")
    m = re.search(r'"([^"]*)"', raw)
    f = m.group(1).split(",") if m else []
    if len(f) < 2:
        raise BaselineError(f"新浪实时价获取失败：{sina_code}")
    return float(f[1])


def weekly_macd_state(week_closes):
    """DIF(12,26) vs DEA(9 of DIF) on weekly closes -> 'dead_cross'|'alive'."""
    if len(week_closes) < 35:
        raise BaselineError("周线数据不足（<35周），无法计算周线MACD")
    e12 = _ema(week_closes, 12)
    e26 = _ema(week_closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = _ema(dif, 9)
    return "dead_cross" if dif[-1] < dea[-1] else "alive"


# --- 市场时段 + SQLite blob 持久化（暖重启 / 闭市免刷新）---------------------
_CLOSED_INTRADAY_TTL = 900       # 闭市分时曲线冻结：15 分钟足够
_INTRADAY_STALE_CLOSED = 4 * 86400   # 闭市旧曲线可跨周末供应
_CLOSED_KLINE_TTL = 1800         # 闭市日线不变：30 分钟 TTL
_KLINE_FAIL_FALLBACK_AGE = 7 * 86400  # 源全挂时回退库内 7 日内日K
_BLOB_MAX_AGE = 7 * 86400

_ZONES = {}


def _zone(tzname, fallback_min):
    if tzname not in _ZONES:
        try:
            from zoneinfo import ZoneInfo
            _ZONES[tzname] = ZoneInfo(tzname)
        except Exception:
            _ZONES[tzname] = timezone(timedelta(minutes=fallback_min))
    return _ZONES[tzname]


def cn_market_open(now=None):
    """A股交易时段（周一至五 09:25-11:35 / 12:55-15:10，含缓冲；不含节假日表）。
    now 可传 aware datetime（任意时区）做测试。"""
    tz = _zone("Asia/Shanghai", 480)
    now = now.astimezone(tz) if now else datetime.now(tz)
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return (565 <= m <= 695) or (775 <= m <= 910)


def us_market_open(now=None):
    """美股常规时段（周一至五 ET 09:25-16:05，含缓冲；不含节假日表）。"""
    tz = _zone("America/New_York", -300)
    now = now.astimezone(tz) if now else datetime.now(tz)
    if now.weekday() >= 5:
        return False
    m = now.hour * 60 + now.minute
    return 565 <= m <= 965


def _market_of(secid):
    """secid 前缀 → 市场。105/106/107 是美股个股（NASDAQ/NYSE/AMEX），
    100 是美股指数（100.DJIA/IXIC/SPX，见 US_INDEX_MAP）；A 股 secid 只以
    单个 "0." / "1." 开头，无歧义。"""
    if secid.startswith("YAHOO:"):
        return "us"
    return ("us" if secid.split(".", 1)[0] in ("100", "105", "106", "107")
            else "cn")


def _market_open_for(secid):
    return us_market_open() if _market_of(secid) == "us" else cn_market_open()


def _session_date_for(secid):
    """最近交易日（简化：周末回退周五；不含节假日）。用于分时快照的新鲜度判定。"""
    tz = (_zone("America/New_York", -300) if _market_of(secid) == "us"
          else _zone("Asia/Shanghai", 480))
    d = datetime.now(tz).date()
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y-%m-%d")


_PERSIST_CONN = None
_PERSIST_LOCK = threading.Lock()
_LAST_PERSIST = {}


def _persist_db():
    global _PERSIST_CONN
    if _PERSIST_CONN is None:
        import sqlite3
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(config.DB_PATH), timeout=30,
                               check_same_thread=False)
        conn.execute("CREATE TABLE IF NOT EXISTS cache_blob ("
                     "key TEXT PRIMARY KEY, val TEXT NOT NULL, "
                     "updated_ts REAL NOT NULL)")
        conn.commit()
        _PERSIST_CONN = conn
    return _PERSIST_CONN


def _blob_save(key, val, min_interval=0.0):
    now = time.monotonic()
    if min_interval and now - _LAST_PERSIST.get(key, 0.0) < min_interval:
        return
    try:
        with _PERSIST_LOCK:
            db = _persist_db()
            db.execute(
                "INSERT INTO cache_blob (key, val, updated_ts) VALUES (?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET val=excluded.val, "
                "updated_ts=excluded.updated_ts",
                (key, json.dumps(val, ensure_ascii=False), time.time()))
            db.commit()
        _LAST_PERSIST[key] = now
    except Exception as e:
        # 落库失败只影响暖启动，不能让请求路径挂掉——但必须留痕（磁盘满/库被锁）
        log.warning("blob save failed for %s: %s", key, e)


def _blob_load(key, max_age=None):
    """返回 (val, wall_age_sec)；无记录/超龄/解析失败返回 None。"""
    try:
        with _PERSIST_LOCK:
            row = _persist_db().execute(
                "SELECT val, updated_ts FROM cache_blob WHERE key = ?",
                (key,)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    age = time.time() - row[1]
    if max_age is not None and age > max_age:
        return None
    try:
        return json.loads(row[0]), age
    except Exception:
        return None


def _seed_from_blob(cache, key, blob_key, accept=None):
    """重启暖启动：把库内最近值按真实年龄放回内存缓存（随后的 SWR 逻辑接管刷新）。
    accept(val) 为可选校验，不通过视为库内无可用值。"""
    got = _blob_load(blob_key, max_age=_BLOB_MAX_AGE)
    if not got or (accept is not None and not accept(got[0])):
        return False
    val, age = got
    cache[key] = (time.monotonic() - age, val)
    return True


def tencent_daily(symbol, bars=320):
    """腾讯日K备用源（东财被拒时）：[{date, open, close, high, low, volume}]。
    覆盖：A股/指数（sh/sz 前缀）、美股 NASDAQ（usXXX.OQ）、SMH（usSMH.OQ）。
    注意：NYSE 与美股大盘指数在腾讯只有单根，不可用作基线。"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
           + urllib.parse.quote(symbol) + ",day,,," + str(bars) + ",qfq")
    raw = _fetch(url, {"User-Agent": UA}).decode("utf-8", "replace")
    d = json.loads(raw).get("data") or {}
    node = d.get(symbol) or {}
    day = node.get("day") or node.get("qfqday") or []
    out = []
    for row in day:
        if len(row) < 6:
            continue
        try:
            out.append({"date": row[0], "open": float(row[1]), "close": float(row[2]),
                        "high": float(row[3]), "low": float(row[4]),
                        "volume": float(row[5] or 0)})
        except ValueError:
            continue
    out.sort(key=lambda b: b["date"])
    return out


def tencent_symbol_for(market, code, secid, sina_code):
    """该标的在腾讯源的日K代码；None = 腾讯不覆盖（NYSE/美股大盘指数等）。"""
    if market in ("cn_stock", "cn_index"):
        return sina_code
    if market == "us_stock":
        mkt = (secid or "").split(".")[0]
        sfx = {"105": ".OQ", "106": ".N", "107": ".N"}.get(mkt)
        # 腾讯对 NYSE/AMEX 只有单根历史，不可用
        return ("us" + code + sfx) if mkt == "105" else None
    return None


_XQ_OPENER = None


def _xueqiu_opener():
    """雪球 cookie 引导（访问首页拿 token），进程内复用。"""
    global _XQ_OPENER
    if _XQ_OPENER is None:
        import http.cookiejar as _cj
        jar = _cj.CookieJar()
        _XQ_OPENER = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(jar))
        _XQ_OPENER.open(urllib.request.Request(
            "https://xueqiu.com/about", headers={"User-Agent": UA}), timeout=10)
    return _XQ_OPENER


def xueqiu_daily(symbol, bars=320):
    """雪球日K备用源（美股指数/个股，含 NYSE）：[{date, close, volume}]。"""
    import time as _time
    from datetime import timezone as _tz, timedelta as _td2
    sym = urllib.parse.quote(symbol)
    begin = int(_time.time() * 1000)
    url = ("https://stock.xueqiu.com/v5/stock/chart/kline.json?symbol=" + sym
           + "&begin=" + str(begin) + "&period=day&type=before&count=-" + str(bars))
    raw = _xueqiu_opener().open(urllib.request.Request(
        url, headers={"User-Agent": UA}), timeout=12).read().decode("utf-8", "replace")
    d = json.loads(raw)
    items = (d.get("data") or {}).get("item") or []
    out = []
    bjtz = _tz(_td2(hours=8))
    for it in items:
        try:
            ts, close, vol = it[0], float(it[2]), float(it[1] or 0)
            out.append({"date": datetime.fromtimestamp(ts / 1000, bjtz).strftime("%Y-%m-%d"),
                        "close": close, "volume": vol})
        except (ValueError, TypeError, IndexError):
            continue
    out.sort(key=lambda b: b["date"])
    if len(out) < 30:
        raise BaselineError(f"雪球 {symbol} 数据不足（{len(out)}根）")
    return out


def compute_baseline(secid=None, yahoo_symbol=None, tencent_symbol=None,
                     xueqiu_symbol=None):
    """MA250 + weekly MACD from daily bars. Returns dict for the DB row."""
    klines = None
    attempts = []
    if yahoo_symbol:
        attempts.append(("yahoo", yahoo_symbol))
    attempts.append(("em", secid))
    if tencent_symbol:
        attempts.append(("tencent", tencent_symbol))
    if xueqiu_symbol:
        attempts.append(("xueqiu", xueqiu_symbol))
    for kind, arg in attempts:
        try:
            if kind == "yahoo":
                klines = yahoo_klines(arg)
            elif kind == "tencent":
                klines = tencent_daily(arg)
            elif kind == "xueqiu":
                klines = xueqiu_daily(arg)
            elif kind == "stooq":
                klines = stooq_daily(arg)
            else:
                klines = em_klines(arg)
            break
        except Exception:
            klines = None
    if not klines:
        raise BaselineError("日K数据不可用（东财/雅虎/腾讯均失败）")
    closes = [b["close"] for b in klines]
    if len(closes) < 250:
        raise BaselineError(f"日K历史不足（{len(closes)}根<250），无法计算年线")
    ma250 = sum(closes[-250:]) / 250
    weeks = [c for _, c in weekly_closes(klines)]
    macd = weekly_macd_state(weeks)
    price = closes[-1]
    return {
        "ma250": round(ma250, 4),
        "ma250_asof": klines[-1]["date"],
        "weekly_macd": macd,
        "last_price": price,
        "bars": len(closes),
    }


# --- code resolution -------------------------------------------------------

# Clickable catalog of commonly-watched CN indices (incl. B-share indices);
# secids verified against EastMoney daily K on 2026-11-07.
CN_INDEX_CATALOG = [
    ("000001", "sh000001", "1.000001", "上证指数"),
    ("000016", "sh000016", "1.000016", "上证50"),
    ("000300", "sh000300", "1.000300", "沪深300"),
    ("000010", "sh000010", "1.000010", "上证180"),
    ("000688", "sh000688", "1.000688", "科创50"),
    ("000015", "sh000015", "1.000015", "红利指数"),
    ("000852", "sh000852", "1.000852", "中证1000"),
    ("399001", "sz399001", "0.399001", "深证成指"),
    ("399006", "sz399006", "0.399006", "创业板指"),
    ("399905", "sz399905", "0.399905", "中证500"),
    ("899050", "bj899050", "0.899050", "北证50"),
    ("000003", "sh000003", "1.000003", "上证B股指数"),
    ("399003", "sz399003", "0.399003", "深证B股指数"),
]
CN_INDEX_MAP = {code: (sina, secid, name)
                for code, sina, secid, name in CN_INDEX_CATALOG}

US_INDEX_MAP = {
    "DJI":  ("gb_$dji", "100.DJIA", "道琼斯工业指数", False, None),
    "DJIA": ("gb_$dji", "100.DJIA", "道琼斯工业指数", False, None),
    "IXIC": ("gb_$ixic", "100.IXIC", "纳斯达克综合指数", False, None),
    "SPX":  ("gb_$inx", "100.SPX", "标普500指数", False, None),
    "INX":  ("gb_$inx", "100.SPX", "标普500指数", False, None),
    # SOX daily K: Yahoo first; when unreachable, fall back to the SMH ETF
    # (VanEck Semiconductor, EM 105.SMH) as a closely-tracking proxy.
    "SOX":  ("gb_$sox", "YAHOO:^SOX", "费城半导体指数", True, "105.SMH"),
}


def _resolve_cn_stock(code):
    if not re.match(r"^\d{6}$", code):
        raise BaselineError("A股代码应为 6 位数字")
    if code.startswith("6"):
        sina, secid = "sh" + code, "1." + code
    elif code.startswith(("0", "3")):
        sina, secid = "sz" + code, "0." + code
    else:
        raise BaselineError("暂不支持该板（北交所等）：仅支持 60x/00x/30x")
    q = _em_quote(secid)
    if not q or not q["name"]:
        raise BaselineError(f"未在东方财富找到该代码：{code}")
    return {"code": code, "name": q["name"], "sina_code": sina, "secid": secid,
            "volume_waived": 0}


def _resolve_cn_index(code):
    if code not in CN_INDEX_MAP:
        raise BaselineError("暂只支持指数代码：" + "、".join(sorted(CN_INDEX_MAP)))
    sina, secid, name = CN_INDEX_MAP[code]
    return {"code": code, "name": name, "sina_code": sina, "secid": secid,
            "volume_waived": 0}


def _resolve_us_stock(code):
    sym = code.strip().upper()
    if not re.match(r"^[A-Z.]{1,6}$", sym):
        raise BaselineError("美股代码应为 1-6 位字母，如 NVDA")
    for prefix in ("105", "106", "107"):  # NASDAQ / NYSE / AMEX
        q = _em_quote(f"{prefix}.{sym}")
        if q and q["name"]:
            return {"code": sym, "name": q["name"],
                    "sina_code": "gb_" + sym.lower(),
                    "secid": f"{prefix}.{sym}", "volume_waived": 0}
    raise BaselineError(f"未在东方财富找到美股代码：{sym}")


def _resolve_us_index(code):
    sym = code.strip().upper()
    if sym not in US_INDEX_MAP:
        raise BaselineError("暂只支持美股指数代码：DJI、IXIC、SPX、SOX")
    sina, secid, name, waived, proxy = US_INDEX_MAP[sym]
    return {"code": sym, "name": name, "sina_code": sina, "secid": secid,
            "volume_waived": 1 if waived else 0,
            "baseline_proxy_secid": proxy}


def resolve(market, code):
    """Resolve (market, code) -> identity dict; raises BaselineError."""
    code = (code or "").strip()
    if market == "cn_stock":
        return _resolve_cn_stock(code)
    if market == "cn_index":
        return _resolve_cn_index(code)
    if market == "us_stock":
        return _resolve_us_stock(code)
    if market == "us_index":
        return _resolve_us_index(code)
    raise BaselineError(f"未知市场类型：{market}")


def build_instrument(market, code):
    """Resolve + compute baseline（按市场构建有序数据源链，任一成功即用）：
    - A股/指数:  东财 → 腾讯（sina 代码同名）
    - 美股个股:  东财 → 腾讯(仅NASDAQ) → 雪球 → 雅虎
    - 美股指数:  东财 → 雪球(.DJI/.IXIC) → 雅虎
    - SOX:       雅虎 ^SOX → 东财 SMH → 腾讯 SMH → 雪球 .SOX（SMH 标度 → 重标定）
    """
    ident = resolve(market, code)
    base, used, chain = None, None, []

    if ident["secid"].startswith("YAHOO:"):
        sox = True
        chain = [("yahoo", ident["secid"].split(":", 1)[1]),
                 ("em", ident.get("baseline_proxy_secid") or "105.SMH"),
                 ("tencent", "usSMH.OQ"),
                 ("xueqiu", ".SOX")]
    elif market == "us_index":
        sox = False
        chain = [("em", ident["secid"])]
        xq = {"DJI": ".DJI", "IXIC": ".IXIC"}.get(ident["code"].upper())
        if xq:
            chain.append(("xueqiu", xq))
        ym = {"DJI": "^DJI", "DJIA": "^DJI", "IXIC": "^IXIC",
              "SPX": "^GSPC"}.get(ident["code"].upper())
        if ym:
            chain.append(("yahoo", ym))
    else:
        sox = False
        chain = [("em", ident["secid"])]
        tsym = tencent_symbol_for(market, ident["code"], ident["secid"], ident["sina_code"])
        if tsym:
            chain.append(("tencent", tsym))
        chain.append(("xueqiu", ident["code"]))
        chain.append(("yahoo", ident["code"]))

    base, used = None, None
    for kind, arg in chain:
        try:
            if kind == "yahoo":
                base = compute_baseline(yahoo_symbol=arg)
            elif kind == "tencent":
                base = compute_baseline(tencent_symbol=arg)
            elif kind == "xueqiu":
                base = compute_baseline(xueqiu_symbol=arg)
            else:
                base = compute_baseline(secid=arg)
            used = kind
            break
        except Exception:
            continue
    if base is None:
        raise BaselineError("日K数据不可用（全部数据源均失败：东财/腾讯/雪球/雅虎）")

    if sox and used != "yahoo":
        # SMH 标度 → SOX 指数标度（按新浪实时比值重标定；周线MACD标度不变）
        live = _sina_gb_price(ident["sina_code"])
        factor = live / base["last_price"]
        base["ma250"] = round(base["ma250"] * factor, 4)
        base["last_price"] = live
        base["ma250_source"] = "SMH_proxy_rescaled"
        base["side"] = "above" if live >= base["ma250"] else "below"
    ident.update(base)
    return ident


# --- chart series (detail view) ---------------------------------------------

_KLINE_CACHE = {}  # secid -> (monotonic_ts, {"bars": [...], "source": str})
_KLINE_TTL = 300


_INTRADAY_CACHE = {}  # secid -> (monotonic_ts, data)
_INTRADAY_TTL = 30
_INTRADAY_STALE = 600   # 过期旧值最多继续供应 10 分钟（分时实时性敏感）
_KLINE_STALE = 86400    # 日K旧值可供应一整天（跨数据源中断保图）

_NEG_TTL = 60           # 全源失败的负缓存：快速失败，不在请求路径重演超时链
_REFRESHING = set()
_REFRESH_LOCK = threading.Lock()


def _refresh_async(cache, key, fn, args=(), keep_stale=False):
    """后台单线程刷新一个缓存键（_REFRESHING 防并发重入）。"""
    with _REFRESH_LOCK:
        if key in _REFRESHING:
            return
        _REFRESHING.add(key)

    def _worker():
        try:
            cache[key] = (time.monotonic(), fn(*args))
        except Exception:
            old = cache.get(key)
            keep = old[1] if (keep_stale and old and old[1] is not None) else None
            cache[key] = (time.monotonic(), keep)
        finally:
            with _REFRESH_LOCK:
                _REFRESHING.discard(key)

    threading.Thread(target=_worker, daemon=True,
                     name="swr:" + str(key)).start()


def _cache_lookup(cache, key, ttl, stale_limit, refresh, args, keep_stale,
                  allow_refresh=True):
    """返回 (value, hit)。hit=False = 冷缓存（调用方同步计算）。
    新鲜 → 直接用；过期但有旧值（< stale_limit）→ 返回旧值并触发后台刷新；
    值为 None 的失败记录在负缓存期内视为命中（快速失败）。"""
    hit = cache.get(key)
    if not hit:
        return None, False
    ts, val = hit
    age = time.monotonic() - ts
    if val is None:
        return None, age < _NEG_TTL
    if age < ttl:
        return val, True
    if age < stale_limit:
        if allow_refresh:
            _refresh_async(cache, key, refresh, args, keep_stale)
        return val, True
    return None, False


def _trends2(secid):
    """当日 1 分钟分时：[[HH:MM, price]...] + preClose（东财 trends2）。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid="
           + urllib.parse.quote(secid) + "&iscr=0&ndays=1"
           "&fields1=f1,f2,f3,f4,f5,f7,f8&fields2=f51,f53")
    raw = _fetch(url, {"User-Agent": UA}).decode("utf-8", "replace")
    d = json.loads(raw).get("data") or {}
    pts = []
    day = None
    for line in d.get("trends") or []:
        f = line.split(",")
        if len(f) >= 2:
            if day is None:
                day = f[0][:10]
            try:
                pts.append([f[0][-5:], float(f[1])])
            except ValueError:
                continue
    pre = _f(d.get("preClose"))
    return {"points": pts, "preClose": pre, "date": day}


def _intraday_compute(secid, sina_code=None, proxy_secid=None):
    if secid.startswith("YAHOO:"):
        if not proxy_secid:
            raise BaselineError("该标的无分时数据源")
        data = _trends2(proxy_secid)
        live = _sina_gb_price(sina_code)
        last = data["points"][-1][1] if data["points"] else 0
        factor = (live / last) if last else 1.0
        data["points"] = [[t, round(pr * factor, 2)] for t, pr in data["points"]]
        if data["preClose"]:
            data["preClose"] = round(data["preClose"] * factor, 2)
        data["source"] = proxy_secid.split(".", 1)[-1] + "_proxy_rescaled"
    else:
        try:
            data = _trends2(secid)
            data["source"] = "eastmoney"
        except Exception:
            data = None
        if (not data or not data.get("points")) and sina_code:
            # 东财分时被拒（OCI 机房间歇性）→ 腾讯分时备用源
            try:
                data = tencent_intraday(sina_code)
            except Exception:
                data = None
        if data is None:
            raise BaselineError("分时数据不可用（东财/腾讯均失败）")
    return data


def intraday_series(secid, sina_code=None, proxy_secid=None):
    """当日分时（SOX 走 SMH 代理 + 实时比值重标定）。
    开市：30s 新鲜；过期先回旧值并后台刷新（旧值最多供应 10 分钟）。
    闭市：曲线已冻结 → 15 分钟 TTL、旧值跨周末有效、不做后台刷新。
    重启暖启动：先用 SQLite 最近快照立即返回（开市时段仅限当日快照）；
    源全挂时回退库内当日快照；
    失败负缓存 60s —— 快速失败，不在请求路径重演超时链。"""
    args = (secid, sina_code, proxy_secid)
    open_ = _market_open_for(secid)
    ttl = _INTRADAY_TTL if open_ else _CLOSED_INTRADAY_TTL
    stale = _INTRADAY_STALE if open_ else _INTRADAY_STALE_CLOSED

    def _lookup():
        return _cache_lookup(_INTRADAY_CACHE, secid, ttl, stale,
                             _intraday_compute, args, keep_stale=False,
                             allow_refresh=open_)

    val, hit = _lookup()
    # 开市时段只认当日快照：上一交易日的整条分时不可当作新鲜值回放
    accept = ((lambda v: str((v or {}).get("date") or "")[:10]
               == _session_date_for(secid)) if open_ else None)
    if not hit and _seed_from_blob(_INTRADAY_CACHE, secid, "intra:" + secid,
                                   accept=accept):
        val, hit = _lookup()
    if hit:
        if val is None:
            raise BaselineError("分时数据暂不可用（后台重试中）")
        return val
    try:
        data = _intraday_compute(*args)
    except Exception:
        got = _blob_load("intra:" + secid)
        if got and str((got[0] or {}).get("date") or "")[:10] == _session_date_for(secid):
            _INTRADAY_CACHE[secid] = (time.monotonic(), got[0])
            return got[0]
        _INTRADAY_CACHE[secid] = (time.monotonic(), None)
        raise
    _INTRADAY_CACHE[secid] = (time.monotonic(), data)
    _blob_save("intra:" + secid, data, min_interval=60)
    return data


def tencent_intraday(sina_code):
    """腾讯分时备用源（东财 trends2 被拒时，覆盖 A股/指数）：
    {points: [[HH:MM, price]...], preClose, date}"""
    url = ("https://web.ifzq.gtimg.cn/appstock/app/minute/query?code="
           + urllib.parse.quote(sina_code))
    raw = _fetch(url, {"User-Agent": UA}).decode("utf-8", "replace")
    d = json.loads(raw).get("data") or {}
    node = d.get(sina_code) or {}
    dd = (node.get("data") or {}).get("data") or []
    qt = (node.get("qt") or {}).get(sina_code) or []
    pts = []
    for row in dd:
        f = row.split(" ")
        if len(f) >= 2:
            try:
                hm = f[0]
                pts.append([hm[:2] + ":" + hm[2:], float(f[1])])
            except (ValueError, IndexError):
                continue
    pre = _f(qt[4]) if len(qt) > 4 else None
    day = None
    if len(qt) > 30 and qt[30]:
        s = str(qt[30])[:8]
        if len(s) == 8:
            day = s[:4] + "-" + s[4:6] + "-" + s[6:8]
    return {"points": pts, "preClose": pre, "date": day, "source": "tencent"}


def stooq_daily(symbol, bars=320):
    """stooq CSV 日K备用源（美股指数/个股）：
    [{date, close, volume}]。symbol 如 ^dji / ^spx / nvda.us。"""
    url = ("https://stooq.com/q/d/l/?s=" + urllib.parse.quote(symbol) + "&i=d")
    raw = _fetch(url, {"User-Agent": UA}).decode("utf-8", "replace")
    if raw.lstrip().startswith("<"):
        raise BaselineError("stooq 返回 HTML（被 JS 挑战拦截）")
    lines = [ln for ln in raw.strip().splitlines() if ln]
    if len(lines) < 2 or not lines[0].lower().startswith("date"):
        raise BaselineError("stooq 数据格式异常")
    out = []
    for line in lines[1:]:
        f = line.split(",")
        if len(f) < 6:
            continue
        try:
            out.append({"date": f[0], "close": float(f[4]),
                        "volume": float(f[5] or 0)})
        except ValueError:
            continue
    out.sort(key=lambda b: b["date"])
    if len(out) < 30:
        raise BaselineError(f"stooq 数据不足（{len(out)}行）")
    return out


def _kline_compute(secid, sina_code=None, proxy_secid=None, tencent_symbol=None,
                   xueqiu_symbol=None, stooq_symbol=None, bars=140):
    if secid.startswith("YAHOO:"):
        kl, source = None, None
        try:
            kl = yahoo_klines(secid.split(":", 1)[1])
            source = "yahoo"
        except Exception:
            kl = None
        if kl is None and proxy_secid:
            try:
                kl = em_klines(proxy_secid)
                live = _sina_gb_price(sina_code)
                factor = live / kl[-1]["close"]
                kl = [dict(b, close=round(b["close"] * factor, 2)) for b in kl]
                source = proxy_secid.split(".", 1)[-1] + "_proxy_rescaled"
            except Exception:
                kl = None
        if kl is None:
            try:
                kl = tencent_daily("usSMH.OQ")
                live = _sina_gb_price(sina_code)
                factor = live / kl[-1]["close"]
                kl = [dict(b, close=round(b["close"] * factor, 2)) for b in kl]
                source = "SMH_tencent_rescaled"
            except Exception:
                kl = None
        if kl is None and xueqiu_symbol:
            try:
                kl = xueqiu_daily(xueqiu_symbol)
                source = "xueqiu_sox"
            except Exception:
                kl = None
        if kl is None:
            try:
                kl = stooq_daily("^sox")
                source = "stooq_sox"
            except Exception:
                kl = None
        if kl is None:
            raise BaselineError("SOX 日K不可用（雅虎/东财SMH/腾讯SMH/雪球/stooq均失败）")
    else:
        kl, source = None, None
        try:
            kl = em_klines(secid)
            source = "eastmoney"
        except Exception:
            kl = None
        if kl is None and tencent_symbol:
            try:
                kl = tencent_daily(tencent_symbol)
                source = "tencent"
            except Exception:
                kl = None
        if kl is None and xueqiu_symbol:
            try:
                kl = xueqiu_daily(xueqiu_symbol)
                source = "xueqiu"
            except Exception:
                kl = None
        if kl is None and stooq_symbol:
            try:
                kl = stooq_daily(stooq_symbol)
                source = "stooq"
            except Exception:
                kl = None
        if kl is None:
            raise BaselineError("日K数据不可用（东财/腾讯/雪球/stooq均失败）")
    return {"bars": [{"date": b["date"], "close": b["close"],
                      "volume": b.get("volume") or 0}
                     for b in kl[-bars:]],
            "source": source}


def daily_closes(secid, sina_code=None, proxy_secid=None, tencent_symbol=None,
                 xueqiu_symbol=None, stooq_symbol=None, bars=140):
    """Recent daily closes for the detail chart. SOX goes Yahoo-first, then
    the SMH proxy rescaled into index scale (same calibre as the baseline).
    开市 5 分钟新鲜 + SWR 后台刷新（旧值可供应 24h）；闭市 30 分钟 TTL 且
    不做后台刷新（日线闭市不变）。重启暖启动取库内最近值；源全挂时回退
    库内 7 日内快照；失败负缓存 60s。"""
    args = (secid, sina_code, proxy_secid, tencent_symbol, xueqiu_symbol,
            stooq_symbol, bars)
    open_ = _market_open_for(secid)
    ttl = _KLINE_TTL if open_ else _CLOSED_KLINE_TTL

    def _lookup():
        return _cache_lookup(_KLINE_CACHE, secid, ttl, _KLINE_STALE,
                             _kline_compute, args, keep_stale=True,
                             allow_refresh=open_)

    val, hit = _lookup()
    if not hit and _seed_from_blob(_KLINE_CACHE, secid, "kline:" + secid):
        val, hit = _lookup()
    if hit:
        if val is None:
            raise BaselineError("日K数据暂不可用（后台重试中）")
        return val
    try:
        out = _kline_compute(*args)
    except Exception:
        got = _blob_load("kline:" + secid, max_age=_KLINE_FAIL_FALLBACK_AGE)
        if got:
            _KLINE_CACHE[secid] = (time.monotonic(), got[0])
            return got[0]
        _KLINE_CACHE[secid] = (time.monotonic(), None)
        raise
    _KLINE_CACHE[secid] = (time.monotonic(), out)
    _blob_save("kline:" + secid, out, min_interval=60)
    return out


def chart_request_for(code, secid, sina_code):
    """Resolve (secid, sina_for_rescale, proxy_secid) for daily_closes()."""
    if secid.startswith("YAHOO:"):
        entry = US_INDEX_MAP.get((code or "").upper())
        return secid, sina_code, (entry[4] if entry else None)
    # 非 YAHOO 也必须透出 sina_code：分时接口靠它走腾讯备用源
    return secid, sina_code, None


# --- DB -> baseline files (for the detector) --------------------------------

def _row_to_cn_stock(r):
    return {"code": r["code"], "name": r["name"], "sina": r["sina_code"],
            "secid": r["secid"], "ma250": r["ma250"],
            "weekly_macd": r["weekly_macd"],
            "ma250_is_proxy": False}


def _row_to_cn_index(r):
    d = _row_to_cn_stock(r)
    d["ma250_source"] = "eastmoney_kline_self"
    d["ma250_tradingview_verified"] = False
    return d


def _row_to_us_item(r):
    kind = "stock" if r["market"] == "us_stock" else "index"
    return {"code": r["code"], "name": r["name"], "sina": r["sina_code"],
            "secid": r["secid"], "ma250": r["ma250"],
            "weekly_macd": r["weekly_macd"], "kind": kind,
            "volume_waived": bool(r["volume_waived"])}


def _initial_side(r):
    """Side for rows that never ran through a close window yet: reflect
    reality from the price captured at add time, so a stock already below
    its MA250 does not fire a spurious 'new break' on the first run."""
    if r["last_price"] is not None and r["ma250"]:
        return "above" if r["last_price"] >= r["ma250"] else "below"
    return "above"


def generate_baseline_files(conn, baseline_dir=None, asof=None):
    """Dump enabled instruments into the detector's three baseline files.

    Returns {"cn_stock": n, "cn_index": n, "us": n}."""
    baseline_dir = Path(baseline_dir or config.BASELINE_DIR)
    baseline_dir.mkdir(parents=True, exist_ok=True)
    asof = asof or datetime.now().strftime("%Y-%m-%d")

    rows = conn.execute(
        "SELECT i.*, s.side AS status_side FROM instruments i "
        "LEFT JOIN instrument_status s ON s.instrument_id = i.id "
        "WHERE i.enabled = 1 ORDER BY i.market, i.code").fetchall()

    stocks, indices, us = [], [], []
    for r in rows:
        side = r["status_side"] or _initial_side(r)
        if r["market"] == "cn_stock":
            d = _row_to_cn_stock(r)
        elif r["market"] == "cn_index":
            d = _row_to_cn_index(r)
        else:
            d = _row_to_us_item(r)
        d["side"] = d["status"] = side
        if r["market"].startswith("us_"):
            us.append(d)
        elif r["market"] == "cn_index":
            indices.append(d)
        else:
            stocks.append(d)

    for name, obj in (
            ("hook_baseline.json", {"baseline_date": asof, "stocks": stocks}),
            ("hook_baseline_indices.json", {"baseline_date": asof, "indices": indices}),
            ("hook_baseline_us.json", {"baseline_date": asof, "items": us})):
        tmp = baseline_dir / (name + ".tmp")
        tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(baseline_dir / name)
    return {"cn_stock": len(stocks), "cn_index": len(indices), "us": len(us)}
