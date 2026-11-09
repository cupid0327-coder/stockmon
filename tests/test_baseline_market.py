"""市场判定与时段：美股指数（100.*）不能落进 A 股时段。"""
from datetime import datetime, timezone

from webapp import baseline_gen


def test_market_of():
    assert baseline_gen._market_of("100.DJIA") == "us"
    assert baseline_gen._market_of("100.IXIC") == "us"
    assert baseline_gen._market_of("100.SPX") == "us"
    assert baseline_gen._market_of("105.NVDA") == "us"
    assert baseline_gen._market_of("107.BABA") == "us"
    assert baseline_gen._market_of("YAHOO:^SOX") == "us"
    assert baseline_gen._market_of("1.000300") == "cn"
    assert baseline_gen._market_of("0.000001") == "cn"
    assert baseline_gen._market_of("1.600519") == "cn"


def test_us_index_uses_us_session():
    """北京 00:30（= 纽约 11:30 前一天）美股指数必须判为开市。"""
    # 2026-11-06 是周五；北京 11-07 00:30 = 纽约 11-06 11:30
    bj = datetime(2026, 11, 7, 0, 30, tzinfo=baseline_gen._zone("Asia/Shanghai", 480))
    assert baseline_gen.us_market_open(bj) is True
    assert baseline_gen.cn_market_open(bj) is False


def test_market_open_for_routes_by_secid(monkeypatch):
    calls = []
    monkeypatch.setattr(baseline_gen, "us_market_open",
                        lambda: calls.append("us") or True)
    monkeypatch.setattr(baseline_gen, "cn_market_open",
                        lambda: calls.append("cn") or False)
    assert baseline_gen._market_open_for("100.DJIA") is True
    assert baseline_gen._market_open_for("1.000300") is False
    assert calls == ["us", "cn"]


def test_cn_market_open_windows():
    tz = baseline_gen._zone("Asia/Shanghai", 480)
    assert baseline_gen.cn_market_open(datetime(2026, 11, 6, 10, 0, tzinfo=tz)) is True
    assert baseline_gen.cn_market_open(datetime(2026, 11, 6, 12, 0, tzinfo=tz)) is False
    assert baseline_gen.cn_market_open(datetime(2026, 11, 6, 14, 0, tzinfo=tz)) is True
    assert baseline_gen.cn_market_open(datetime(2026, 11, 7, 10, 0, tzinfo=tz)) is False  # 周六


def _freeze(monkeypatch, moment):
    """把 baseline_gen 里的 datetime.now(tz) 冻结在某个北京时刻。"""
    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)
    monkeypatch.setattr(baseline_gen, "datetime", FakeDatetime)


def test_session_date_uses_market_clock(monkeypatch):
    """快照新鲜度按各自市场时钟取日期：北京周一 00:30 时，美股上一场是
    周五（纽约还是周日），A 股则是周一 —— 修复前美股指数被当 A 股，
    会拿上海日期去比，把有效快照判成过期。"""
    bj = baseline_gen._zone("Asia/Shanghai", 480)
    _freeze(monkeypatch, datetime(2026, 11, 9, 0, 30, tzinfo=bj))
    assert baseline_gen._session_date_for("100.DJIA") == "2026-11-06"
    assert baseline_gen._session_date_for("1.000300") == "2026-11-09"
