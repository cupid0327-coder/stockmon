"""行情键与数据源健壮性：sina_code 唯一性、curl 退出码、快照合并。"""
import subprocess

import pytest

from webapp import quotes


RAW = (
    'var hq_str_sh000001="上证指数,3200.10,3195.00,3210.55,3220.00,3180.00,'
    '0,0,123456789,987654321,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'
    '2026-11-06,15:00:00,00";\n'
    'var hq_str_sz000001="平安银行,11.10,11.00,11.25,11.30,11.05,'
    '0,0,123456789,987654321,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,'
    '2026-11-06,15:00:00,00";\n'
)


def test_parse_cn_keeps_exchange_prefix():
    out = quotes._parse_cn(RAW)
    # 裸 6 位 code 不唯一：指数与个股都是 "000001"，键必须带交易所前缀
    assert set(out) == {"sh000001", "sz000001"}
    assert out["sh000001"]["price"] == 3210.55
    assert out["sz000001"]["price"] == 11.25
    assert out["sh000001"]["prev_close"] == 3195.00
    assert round(out["sz000001"]["day_change_pct"], 2) == 2.27


def test_parse_us_keys_upper():
    raw = ('var hq_str_gb_nvda="英伟达,180.50,1.25,2026-11-06 16:00:00,'
           '2.23,178.00,182.00,177.50,190.00,150.00";\n')
    out = quotes._parse_us(raw)
    assert out["NVDA"]["price"] == 180.50
    assert out["NVDA"]["day_change_pct"] == 1.25


def test_fetch_raises_when_curl_fails(monkeypatch):
    """urllib 失败 + curl 非 0 退出：必须抛错（否则全量失败会被当成成功）。"""
    def boom(*a, **k):
        raise OSError("urllib blocked")
    monkeypatch.setattr(quotes.urllib.request, "urlopen", boom)
    # quotes._fetch 在函数内 import subprocess：patch 全局模块对象
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 6, b"", b""))
    with pytest.raises(RuntimeError):
        quotes._fetch("https://hq.sinajs.cn/list=sh000001")


def test_fetch_raises_on_empty_curl_stdout(monkeypatch):
    monkeypatch.setattr(quotes.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, b"", b""))
    with pytest.raises(RuntimeError):
        quotes._fetch("https://hq.sinajs.cn/list=sh000001")


def test_snap_save_merges_instead_of_replacing(monkeypatch):
    """只补齐部分标的时，不能把库内其他标的的快照抹掉。"""
    store = {}

    def fake_load(key, max_age=None):
        return (store[key], 0) if key in store else None

    def fake_save(key, data, min_interval=0):
        store[key] = data

    from webapp import baseline_gen
    monkeypatch.setattr(baseline_gen, "_blob_load", fake_load)
    monkeypatch.setattr(baseline_gen, "_blob_save", fake_save)

    quotes._snap_save("cn", {"sh600519": {"price": 1}})
    quotes._snap_save("cn", {"sz000001": {"price": 2}})
    assert set(store["quotes:cn"]) == {"sh600519", "sz000001"}


def test_fetch_quotes_keys_by_sina_code(monkeypatch):
    monkeypatch.setattr(quotes, "_fetch", lambda url: RAW
                        if "sh000001" in url else "")
    rows = [
        {"market": "cn_stock", "code": "000001", "sina_code": "sz000001",
         "secid": "0.000001"},
        {"market": "cn_index", "code": "000001", "sina_code": "sh000001",
         "secid": "1.000001"},
    ]
    out, partial = quotes.fetch_quotes(rows)
    assert out["sh000001"]["price"] == 3210.55
    assert out["sz000001"]["price"] == 11.25
