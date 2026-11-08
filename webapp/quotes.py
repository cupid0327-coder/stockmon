"""Sina batch live quotes for dashboard/detail real-time display.

One HTTP call per market group (CN / US), same endpoint the detector uses.
Polling from the frontend is throttled (15s in-session); this module does no
caching of its own — every call is a fresh fetch.
"""
import json
import re
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_CN_RE = re.compile(r'hq_str_([a-z]{2}\d{6})="([^"]*)"')
_US_RE = re.compile(r'hq_str_gb_([a-z0-9$]+)="([^"]*)"')


def _fetch(url):
    req = urllib.request.Request(
        url, headers={"Referer": "https://finance.sina.com.cn/",
                      "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=8) as r:
        return r.read().decode("gbk", "replace")


def _f(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _parse_cn(raw):
    out = {}
    for m in _CN_RE.finditer(raw):
        f = m.group(2).split(",")
        if len(f) < 32:
            continue
        price = _f(f[3])
        if price is None:
            continue
        prev = _f(f[2])
        chg = round((price / prev - 1) * 100, 2) if prev else None
        out[m.group(1)[2:]] = {"price": price, "day_change_pct": chg,
                               "time": f[31],
                               "open": _f(f[1]), "prev_close": prev,
                               "high": _f(f[4]), "low": _f(f[5])}
    return out


def _parse_us(raw):
    out = {}
    for m in _US_RE.finditer(raw):
        f = m.group(2).split(",")
        if len(f) < 3:
            continue
        price = _f(f[1])
        chg = _f(f[2])
        if price is None or chg is None:
            continue
        out[m.group(1).upper()] = {
            "price": price, "day_change_pct": chg,
            "time": f[25] if len(f) > 25 else "",
            "open": _f(f[5]), "high": _f(f[6]), "low": _f(f[7]),
            "prev_close": _f(f[26]) if len(f) > 26 else None,
            "wk_high": _f(f[8]), "wk_low": _f(f[9])}
    return out


_EM_FUND_RE = re.compile(r'"diff"\s*:\s*(\[.*?\]|\{.*?\})', re.S)
_EM_FUND_FIELDS = "f13,f12,f10,f8,f115,f9,f20,f21,f23"


def _parse_em_fundamentals(raw, secids):
    """EastMoney ulist fundamentals keyed by our instrument code.
    secids: {secid: code}. f10 量比, f8 换手率, f115/f9/f114 市盈率,
    f20 总市值, f21 流通市值, f23 市净率 ('-' when N/A)."""
    data = json.loads(raw).get("data") or {}
    diff = data.get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    # EM echoes f13 (market id) + f12 (symbol); map back to our codes
    by_market = {}
    for secid, code in secids.items():
        market, symbol = secid.split(".", 1)
        by_market.setdefault(market, {})[symbol] = code
    out = {}
    for item in diff:
        code = by_market.get(str(item.get("f13", "")), {}) \
            .get(str(item.get("f12", "")))
        if not code:
            continue
        out[code] = {
            "volume_ratio": _f(item.get("f10")),
            "turnover_rate": _f(item.get("f8")),
            "pe_ttm": _f(item.get("f115")) or _f(item.get("f9"))
                      or _f(item.get("f114")),
            "total_cap": _f(item.get("f20")),
            "float_cap": _f(item.get("f21")),
            "pb": _f(item.get("f23")),
        }
    return out


def fetch_quotes(instruments):
    """instruments: rows with market/code/sina_code/secid.
    Returns ({code: {price, day_change_pct, time, ...fundamentals}},
    partial_failed)."""
    cn = [r for r in instruments if not str(r["market"]).startswith("us_")]
    us = [r for r in instruments if str(r["market"]).startswith("us_")]
    quotes, partial = {}, False

    if cn:
        try:
            raw = _fetch("https://hq.sinajs.cn/list=" +
                         ",".join(r["sina_code"] for r in cn))
            quotes.update(_parse_cn(raw))
        except Exception:
            partial = True

    if us:
        try:
            raw = _fetch("https://hq.sinajs.cn/list=" +
                         ",".join(r["sina_code"] for r in us))
            parsed = _parse_us(raw)
            by_sina = {str(r["sina_code"])[3:].upper(): str(r["code"])
                       for r in us}
            for key, q in parsed.items():
                code = by_sina.get(key)
                if code:
                    quotes[code] = q
        except Exception:
            partial = True

    # EastMoney ulist fundamentals (量比/换手/市盈/市净/市值), one call per
    # market group; merged into the same quote dicts. 量比 f10 与检测器
    # 破位判定同源同口径。
    for rows in (cn, us):
        if not rows:
            continue
        secids = {str(r["secid"]): str(r["code"]) for r in rows
                  if r["secid"] and not str(r["secid"]).startswith("YAHOO:")}
        if not secids:
            continue
        try:
            raw = _fetch(
                "https://push2delay.eastmoney.com/api/qt/ulist.np/get"
                "?fltt=2&invt=2&fields=" + _EM_FUND_FIELDS +
                "&secids=" + urllib.parse.quote(",".join(secids)))
            for code, fund in _parse_em_fundamentals(raw, secids).items():
                quotes.setdefault(code, {}).update(fund)
        except Exception:
            partial = True

    return quotes, partial
