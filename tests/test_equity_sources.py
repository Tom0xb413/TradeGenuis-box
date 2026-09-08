#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sina / Naver 解析器与股票适配器（mock HTTP，不打全市场）。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import equity_sources as eq  # noqa: E402
import global_pool as gp  # noqa: E402


SINA_JSONP_AAPL = (
    '/*<script>location.href=\'//sina.com\';</script>*/ '
    'IO.XSRV2.CallbackList(['
    '{"d":"2024-01-02","o":"100","h":"110","l":"90","c":"105","v":"1000","a":"0"},'
    '{"d":"2024-01-03","o":"105","h":"112","l":"101","c":"108","v":"1100","a":"0"}'
    '])'
)
SINA_JSONP_MINK = (
    '/*<script>location.href=\'//sina.com\';</script>*/ '
    'IO.XSRV2.CallbackList(['
    '{"d":"2026-02-05 12:00:00","o":"100","h":"110","l":"90","c":"105","v":"1000","a":"0"},'
    '{"d":"2026-02-05 16:00:00","o":"105","h":"112","l":"101","c":"108","v":"1100","a":"0"}'
    '])'
)
SINA_JSONP_MINK_HOUR = (
    'IO.XSRV2.CallbackList(['
    '{"d":"2024-01-02 00:00:00","o":"100","h":"101","l":"99","c":"100.5","v":"10"},'
    '{"d":"2024-01-02 01:00:00","o":"101","h":"102","l":"100","c":"101.5","v":"11"},'
    '{"d":"2024-01-02 02:00:00","o":"102","h":"103","l":"101","c":"102.5","v":"12"},'
    '{"d":"2024-01-02 03:00:00","o":"103","h":"104","l":"102","c":"103.5","v":"13"},'
    '{"d":"2024-01-02 04:00:00","o":"104","h":"105","l":"103","c":"104.5","v":"14"},'
    '{"d":"2024-01-02 05:00:00","o":"105","h":"106","l":"104","c":"105.5","v":"15"},'
    '{"d":"2024-01-02 06:00:00","o":"106","h":"107","l":"105","c":"106.5","v":"16"},'
    '{"d":"2024-01-02 07:00:00","o":"107","h":"108","l":"106","c":"107.5","v":"17"}'
    '])'
)
SINA_HQ_AAPL = (
    'var hq_str_gb_aapl="苹果,319.9700,-2.51,2026-09-05 09:46:58,-8.2400,'
    '328.3050,328.9300,317.8600";\n'
)
SINA_HQ_EMPTY = 'var hq_str_gb_zzzzzz="";\n'
SINA_GI = {
    "code": 0, "message": "",
    "result": {"data": [
        {"d": "2024-01-02", "o": "28000", "h": "28100", "l": "27900", "c": "28050", "v": "0"},
        {"d": "2024-01-03", "o": "28050", "h": "28200", "l": "28000", "c": "28100", "v": "0"},
    ]},
}
NAVER_JP = [
    {
        "localTradedAt": "2026-09-07T15:00:00+09:00",
        "closePrice": "3,096.0", "openPrice": "3,110.0",
        "highPrice": "3,120.0", "lowPrice": "3,088.0",
        "accumulatedTradingVolume": "1000",
    },
    {
        "localTradedAt": "2026-09-04T15:00:00+09:00",
        "closePrice": "3,081.0", "openPrice": "3,070.0",
        "highPrice": "3,090.0", "lowPrice": "3,060.0",
    },
]
NAVER_SISE = """
  [['날짜', '시가', '고가', '저가', '종가', '거래량', '외국인소진율'],
   ["20240102", 78200, 79800, 78200, 79600, 17142847, 54.05],
   ["20240103", 78500, 78800, 77000, 77000, 21753644, 54.04]
  ]
"""
EM_QUOTE = {
    "rc": 0, "data": {
        "f43": 2987.5, "f44": 3040.0, "f45": 2981.5, "f46": 3040.0,
        "f57": "7203", "f58": "丰田汽车", "f60": 3096.0, "f170": -3.5,
    },
}


class ParserTest(unittest.TestCase):
    def test_sina_jsonp_daily(self):
        bars = eq.parse_sina_jsonp_daily(SINA_JSONP_AAPL)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["date"], "2024-01-02")
        self.assertEqual(bars[0]["open"], 100.0)
        self.assertEqual(bars[1]["close"], 108.0)
        self.assertEqual(eq.parse_sina_jsonp_daily("IO.XSRV2.CallbackList(null);"), [])

    def test_sina_jsonp_mink_keeps_time(self):
        bars = eq.parse_sina_jsonp_ohlc(SINA_JSONP_MINK, keep_time=True)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["date"], "2026-02-05 12:00")
        self.assertEqual(eq.parse_sina_jsonp_ohlc(SINA_JSONP_AAPL, keep_time=True), [])

    def test_sina_hq(self):
        q = eq.parse_sina_hq(SINA_HQ_AAPL)
        self.assertEqual(q["name"], "苹果")
        self.assertAlmostEqual(q["price"], 319.97)
        self.assertAlmostEqual(q["chg"], -2.51)
        self.assertIsNone(eq.parse_sina_hq(SINA_HQ_EMPTY))

    def test_sina_gi(self):
        bars = eq.parse_sina_gi_daily(SINA_GI)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[1]["close"], 28100.0)

    def test_naver_price_list(self):
        bars = eq.parse_naver_price_list(NAVER_JP)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["date"], "2026-09-07")
        self.assertEqual(bars[0]["close"], 3096.0)
        self.assertEqual(eq.parse_naver_price_list({"code": "StockConflict"}), [])

    def test_naver_sise(self):
        bars = eq.parse_naver_sise_json(NAVER_SISE)
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0]["date"], "2024-01-02")
        self.assertEqual(bars[0]["open"], 78200.0)
        self.assertEqual(bars[0]["high"], 79800.0)
        self.assertEqual(bars[0]["low"], 78200.0)
        self.assertEqual(bars[0]["close"], 79600.0)
        self.assertEqual(bars[0]["vol"], 17142847.0)

    def test_em_quote(self):
        q = eq.parse_em_quote(EM_QUOTE)
        self.assertEqual(q["name"], "丰田汽车")
        self.assertEqual(q["price"], 2987.5)
        self.assertEqual(q["chg"], -3.5)


class FetchRoutingTest(unittest.TestCase):
    def test_us_stock_uses_sina_jsonp(self):
        def fake_get(url, params=None, headers=None, timeout=10):
            if "US_MinKService.getDailyK" in url:
                return 200, SINA_JSONP_AAPL
            if "hq.sinajs" in url:
                return 200, SINA_HQ_AAPL
            return 404, ""

        ident = gp.resolve_symbol("AAPL")
        with patch.object(eq, "http_get", side_effect=fake_get):
            inst = eq.fetch_equity_instrument(ident, interval="1d", lookback=10)
        self.assertIsNotNone(inst)
        self.assertEqual(inst["source"], "sina")
        self.assertEqual(inst["asset_class"], "us_stock")
        self.assertGreaterEqual(len(inst["bars"]), 2)
        self.assertIsNone(inst.get("interval_note"))

    def test_equity_4h_falls_back_to_1d_with_note(self):
        def fake_get(url, params=None, headers=None, timeout=10):
            if "US_MinKService" in url:
                return 200, SINA_JSONP_AAPL
            return 200, SINA_HQ_EMPTY

        ident = gp.resolve_symbol("AAPL")
        with patch.object(eq, "http_get", side_effect=fake_get):
            inst = eq.fetch_equity_instrument(ident, interval="4h", lookback=10)
        self.assertTrue(inst["interval_limited"])
        self.assertIn("日K", inst["interval_note"])

    def test_us_4h_uses_mink_without_note(self):
        def fake_get(url, params=None, headers=None, timeout=10):
            if "getMinK" in (url or "") and str((params or {}).get("type")) == "240":
                return 200, SINA_JSONP_MINK
            if "hq.sinajs" in (url or ""):
                return 200, SINA_HQ_AAPL
            return 404, ""

        ident = gp.resolve_symbol("MA")
        with patch.object(eq, "http_get", side_effect=fake_get):
            inst = eq.fetch_equity_instrument(ident, interval="4h", lookback=10)
        self.assertIsNotNone(inst)
        self.assertFalse(inst.get("interval_limited"))
        self.assertIsNone(inst.get("interval_note"))
        self.assertEqual(inst["interval"], "4h")
        self.assertEqual(inst["bars"][0]["date"], "2026-02-05 12:00")

    def test_us_8h_resamples_hour_mink(self):
        def fake_get(url, params=None, headers=None, timeout=10):
            if "getMinK" in (url or "") and str((params or {}).get("type")) == "60":
                return 200, SINA_JSONP_MINK_HOUR
            if "hq.sinajs" in (url or ""):
                return 200, SINA_HQ_EMPTY
            return 404, ""

        ident = gp.resolve_symbol("MA")
        with patch.object(eq, "http_get", side_effect=fake_get):
            inst = eq.fetch_equity_instrument(ident, interval="8h", lookback=10)
        self.assertEqual(inst["interval"], "8h")
        self.assertIsNone(inst.get("interval_note"))
        self.assertEqual(len(inst["bars"]), 1)
        self.assertEqual(inst["bars"][0]["date"], "2024-01-02 00:00")
        self.assertEqual(inst["bars"][0]["open"], 100.0)
        self.assertEqual(inst["bars"][0]["close"], 107.5)

    def test_jp_4h_still_daily_note(self):
        def fake_json(url, params=None, headers=None, timeout=10):
            if "7203.T" in url and "/price" in url:
                return NAVER_JP
            return {"code": "StockConflict"}

        ident = gp.resolve_symbol("7203.T")
        with patch.object(eq, "http_json", side_effect=fake_json), \
             patch.object(eq, "http_get", return_value=(404, "")):
            inst = eq.fetch_equity_instrument(ident, interval="4h", lookback=10)
        self.assertTrue(inst["interval_limited"])
        self.assertIn("日K", inst["interval_note"])

    def test_jp_stock_uses_naver(self):
        def fake_json(url, params=None, headers=None, timeout=10):
            if "7203.T" in url and "/price" in url:
                return NAVER_JP
            return {"code": "StockConflict"}

        ident = gp.resolve_symbol("7203.T")
        with patch.object(eq, "http_json", side_effect=fake_json), \
             patch.object(eq, "http_get", return_value=(404, "")):
            inst = eq.fetch_equity_instrument(ident, interval="1d", lookback=10)
        self.assertEqual(inst["source"], "naver")
        self.assertEqual(inst["asset_class"], "jp_stock")
        self.assertEqual(inst["bars"][0]["close"], 3081.0)  # sorted by date

    def test_kr_uses_sise(self):
        def fake_get(url, params=None, headers=None, timeout=10):
            if "siseJson" in url:
                return 200, NAVER_SISE
            return 404, ""

        ident = gp.resolve_symbol("005930")
        with patch.object(eq, "http_get", side_effect=fake_get):
            inst = eq.fetch_equity_instrument(ident, interval="1d", lookback=10)
        self.assertEqual(inst["source"], "naver")
        self.assertEqual(len(inst["bars"]), 2)

    def test_nky_uses_gi(self):
        ident = gp.resolve_symbol("日经225指数")
        with patch.object(eq, "http_json", return_value=SINA_GI), \
             patch.object(eq, "http_get", return_value=(200, 'var hq_str_znb_NKY="日经225,28000,0,0";')):
            inst = eq.fetch_equity_instrument(ident, interval="1d", lookback=10)
        self.assertEqual(ident["code"], "NKY")
        self.assertEqual(inst["source"], "sina")
        self.assertEqual(inst["asset_class"], "jp_index")


if __name__ == "__main__":
    unittest.main()
