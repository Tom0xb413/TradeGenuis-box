#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""趋势线检测、形态族配置/缓存、看板契约。不跑全市场扫描。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pattern_flag as pf  # noqa: E402
import pattern_trendline as tl  # noqa: E402
import scanner as sc  # noqa: E402
import server  # noqa: E402

BJT = timezone(timedelta(hours=8))


def _bar(i: int, *, o, h, l, c, vol=1000.0, d0=None) -> dict:
    d0 = d0 or date(2024, 1, 2)
    low = min(float(l), float(o), float(c))
    high = max(float(h), float(o), float(c), low)
    return {
        "date": (d0 + timedelta(days=i)).isoformat(),
        "open": float(o), "high": high, "low": low, "close": float(c),
        "vol": float(vol),
    }


def line_px(i: int, t0: int = 10, p0: float = 10.0, slope: float = 0.0625) -> float:
    return p0 + slope * (i - t0)


def _ascending_hl_bars(n: int = 70, *, break_last: bool = False,
                       pierce_mid: bool = False) -> list[dict]:
    """上升更高低：摆动低点在 10 / 26 / 42，斜率为 0.0625。"""
    pivots = {10: 10.0, 26: 11.0, 42: 12.0}
    bars = []
    for i in range(n):
        lv = line_px(i)
        if i in pivots:
            low = pivots[i]
            o, c = low + 0.12, low + 0.18
            h = c + 0.22
        else:
            low = lv + 0.35
            o, c = lv + 0.42, lv + 0.48
            h = c + 0.18
        if pierce_mid and i in (32, 33, 34):
            c = lv - 0.25
            o = lv - 0.05
            low = c - 0.08
            h = lv + 0.05
        if break_last and i == n - 2:
            o, c = lv + 0.20, lv + 0.22
            low, h = lv + 0.10, c + 0.12
        if break_last and i == n - 1:
            c = lv * (1.0 - 0.012)
            o = lv + 0.04
            low = c - 0.06
            h = o + 0.03
        bars.append(_bar(i, o=o, h=h, l=low, c=c))
    return bars


def _descending_lh_bars(n: int = 70, *, break_last: bool = False) -> list[dict]:
    """下降更低高：摆动高点在 10 / 26 / 42，斜率 −0.0625。"""
    pivots = {10: 15.0, 26: 14.0, 42: 13.0}
    bars = []
    for i in range(n):
        lv = line_px(i, p0=15.0, slope=-0.0625)
        if i in pivots:
            high = pivots[i]
            o, c = high - 0.12, high - 0.18
            low = c - 0.22
        else:
            high = lv - 0.35
            o, c = lv - 0.42, lv - 0.48
            low = c - 0.18
        if break_last and i == n - 2:
            o, c = lv - 0.20, lv - 0.22
            high, low = lv - 0.10, c - 0.12
        if break_last and i == n - 1:
            c = lv * (1.0 + 0.012)
            o = lv - 0.04
            high = c + 0.06
            low = o - 0.03
        bars.append(_bar(i, o=o, h=high, l=low, c=c))
    return bars


class TrendlineDetectTest(unittest.TestCase):
    def test_constants_documented(self):
        self.assertEqual(tl.SWING_K, 3)
        self.assertEqual(tl.MIN_TOUCHES, 2)
        self.assertEqual(tl.TOUCH_TOL, 0.006)
        self.assertEqual(tl.BREACH_ALPHA, 0.003)
        self.assertEqual(tl.EVENT_LOOK, 3)
        self.assertEqual(tl.PIERCE_TOL, 0.008)
        self.assertIn("triangle", tl.TL_PATTERNS)

    def test_ascending_lows_fit_support(self):
        bars = _ascending_hl_bars()
        got = tl.detect_trendline(bars)
        self.assertIsNotNone(got["tl_support"])
        sup = got["tl_support"]
        self.assertGreaterEqual(sup["touches"], 2)
        self.assertGreater(sup["slope"], 0)
        self.assertGreater(sup["L_now"], sup["p1"])
        self.assertEqual(got["tl_event"], "none")
        self.assertTrue(got["tl_has_line"])
        self.assertIn(got["tl_quality"], ("ok", "weak"))
        self.assertEqual(got["tl_pattern"], "none")
        self.assertGreaterEqual(len(got["tl_points"]["support"]), 2)
        lows = tl.find_swing_pivots(bars)[1]
        idxs = {p[0] for p in lows}
        self.assertTrue({10, 26, 42}.issubset(idxs) or len(idxs) >= 2)

    def test_close_below_is_support_break(self):
        bars = _ascending_hl_bars(break_last=True)
        got = tl.detect_trendline(bars)
        self.assertIsNotNone(got["tl_support"])
        self.assertEqual(got["tl_event"], "support_break")
        self.assertEqual(got["tl_event_bar"], bars[-1]["date"])
        L = tl.line_at(
            got["tl_support"]["t1"], got["tl_support"]["p1"],
            got["tl_support"]["t2"], got["tl_support"]["p2"],
            len(bars) - 1,
        )
        self.assertLess(bars[-1]["close"], L * (1.0 - tl.BREACH_ALPHA))
        self.assertGreaterEqual(bars[-2]["close"], tl.line_at(
            got["tl_support"]["t1"], got["tl_support"]["p1"],
            got["tl_support"]["t2"], got["tl_support"]["p2"],
            len(bars) - 2,
        ))

    def test_lower_highs_fit_resistance(self):
        bars = _descending_lh_bars()
        got = tl.detect_trendline(bars)
        self.assertIsNotNone(got["tl_resistance"])
        res = got["tl_resistance"]
        self.assertGreaterEqual(res["touches"], 2)
        self.assertLess(res["slope"], 0)
        self.assertEqual(got["tl_event"], "none")
        self.assertTrue(got["tl_has_line"])

    def test_close_above_is_resistance_break(self):
        bars = _descending_lh_bars(break_last=True)
        got = tl.detect_trendline(bars)
        self.assertIsNotNone(got["tl_resistance"])
        self.assertEqual(got["tl_event"], "resistance_break")
        self.assertEqual(got["tl_event_bar"], bars[-1]["date"])
        L = tl.line_at(
            got["tl_resistance"]["t1"], got["tl_resistance"]["p1"],
            got["tl_resistance"]["t2"], got["tl_resistance"]["p2"],
            len(bars) - 1,
        )
        self.assertGreater(bars[-1]["close"], L * (1.0 + tl.BREACH_ALPHA))

    def test_mid_pierce_rejects_support(self):
        clean = tl.detect_trendline(_ascending_hl_bars())
        pierced = tl.detect_trendline(_ascending_hl_bars(pierce_mid=True))
        self.assertIsNotNone(clean["tl_support"])
        orig = (clean["tl_support"]["t1"], clean["tl_support"]["t2"])
        self.assertGreaterEqual(clean["tl_support"]["touches"], 3)
        got = pierced["tl_support"]
        # 原 10–42 三触点线因中段收盘重刺穿被否决；允许拟合到别的短线
        self.assertTrue(got is None or (got["t1"], got["t2"]) != orig)

    def test_close_inside_alpha_is_not_break(self):
        bars = _ascending_hl_bars()
        last = dict(bars[-1])
        sup_probe = tl.detect_trendline(bars)
        self.assertIsNotNone(sup_probe["tl_support"])
        L = tl.line_at(
            sup_probe["tl_support"]["t1"], sup_probe["tl_support"]["p1"],
            sup_probe["tl_support"]["t2"], sup_probe["tl_support"]["p2"],
            len(bars) - 1,
        )
        last["close"] = L * (1.0 - 0.001)  # 低于线但未达 α
        last["low"] = min(last["low"], last["close"] - 0.02)
        bars[-1] = last
        got = tl.detect_trendline(bars)
        self.assertEqual(got["tl_event"], "none")

    def test_empty_and_short_series(self):
        self.assertEqual(tl.detect_trendline([])["tl_quality"], "none")
        self.assertEqual(tl.detect_trendline([_bar(0, o=10, h=10.1, l=9.9, c=10)])["tl_has_line"], False)
        self.assertFalse(tl.trendline_row_fields(None)["tl_has_line"])
        row = tl.trendline_row_fields(tl.detect_trendline(_ascending_hl_bars()))
        self.assertNotIn("tl_points", row)
        self.assertIn("tl_support", row)

    def test_wick_through_not_a_break(self):
        bars = _ascending_hl_bars()
        got0 = tl.detect_trendline(bars)
        self.assertIsNotNone(got0["tl_support"])
        L = tl.line_at(
            got0["tl_support"]["t1"], got0["tl_support"]["p1"],
            got0["tl_support"]["t2"], got0["tl_support"]["p2"],
            len(bars) - 1,
        )
        last = dict(bars[-1])
        last["low"] = L * 0.97
        last["close"] = L + 0.15
        last["open"] = L + 0.12
        last["high"] = last["close"] + 0.1
        bars[-1] = last
        got = tl.detect_trendline(bars)
        self.assertEqual(got["tl_event"], "none")


class BoxScoreUntouchedByTrendlineTest(unittest.TestCase):
    def test_tl_metadata_does_not_change_score(self):
        base = {
            "theme_ok": True, "volume_days": 3, "volume_ratio": 2.0,
            "fund_state": "流入", "control": "高", "tests": 3,
        }
        a = sc.score_row(dict(base))
        b = sc.score_row({
            **base,
            "tl_event": "support_break",
            "tl_has_line": True,
            "tl_quality": "ok",
        })
        self.assertEqual(a["score"], 100)
        self.assertEqual(a["score"], b["score"])

    def test_normalize_family_accepts_trendline(self):
        self.assertEqual(pf.normalize_pattern_family("trendline"), "trendline")
        self.assertEqual(pf.normalize_pattern_family("TRENDLINE"), "trendline")
        self.assertEqual(sc.normalize_pattern_family("trendline"), "trendline")
        self.assertEqual(pf.normalize_pattern_family("triangle"), "box")


class PatternFamilyTrendlineConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self.tmp.name) / "config.json"
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.p = patch.object(server, "CONFIG_FILE", self.cfg_path)
        self.p.start()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.p.stop()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.tmp.cleanup()

    def _req(self, method: str, path: str, body=None):
        import urllib.request
        url = f"http://127.0.0.1:{self.port}/{path.lstrip('/')}"
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode("utf-8"))

    def test_post_trendline_persists(self):
        saved = self._req("POST", "api/config", {"pattern_family": "trendline"})
        self.assertTrue(saved["ok"])
        self.assertEqual(saved["config"]["pattern_family"], "trendline")
        got = self._req("GET", "api/config")
        self.assertEqual(got["pattern_family"], "trendline")
        self.assertIn("trendline", got["pattern_families"])
        disk = json.loads(self.cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(disk["pattern_family"], "trendline")


class ScanCacheTrendlineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.watch = Path(self.tmp.name) / "watchlist.json"
        self.p_watch = patch.object(server, "WATCH_FILE", self.watch)
        self.p_watch.start()
        server.STATE["scanning"] = False
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)

    def tearDown(self):
        self.p_watch.stop()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.tmp.cleanup()

    def _payload(self, **extra):
        now = datetime.now(BJT)
        rows = [{"code": "000001", "score": 90, "qualified": True, "tl_has_line": True}]
        payload = {
            "as_of": now.strftime("%Y-%m-%d %H:%M:%S"),
            "updated": now.isoformat(timespec="seconds"),
            "scope": "market",
            "done": True,
            "universe_size": 10,
            "candidates": rows,
            "items": rows,
            "box_mode": "classic",
            "pattern_family": "box",
        }
        payload.update(extra)
        return payload

    def test_miss_when_switching_to_trendline(self):
        self.watch.write_text(json.dumps(self._payload(pattern_family="box")), encoding="utf-8")
        server.STATE["config"]["pattern_family"] = "trendline"
        self.assertIsNone(server.scan_cache_response("market", force=False))

    def test_hit_when_trendline_matches(self):
        self.watch.write_text(json.dumps(self._payload(pattern_family="trendline")), encoding="utf-8")
        server.STATE["config"]["pattern_family"] = "trendline"
        hit = server.scan_cache_response("market", force=False)
        self.assertIsNotNone(hit)


class KlineTrendlineOverlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.disk = Path(self.tmp.name) / "kline"
        server.STATE["kline_cache"] = {}
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        self.patches = [
            patch.object(server, "KLINE_DISK_DIR", self.disk),
            patch.object(sc, "fetch_quote", return_value={
                "price": 12.5, "chg": 0.0, "name": "测", "turnover": 1.0, "volume_ratio": 1.0,
            }),
            patch.object(sc, "fetch_kline", return_value=_ascending_hl_bars(break_last=True)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        server.STATE["config"] = dict(server.DEFAULT_CONFIG)
        server.STATE["kline_cache"] = {}
        self.tmp.cleanup()

    def test_kline_includes_trendline_without_refetch(self):
        a = server.get_kline("600000")
        self.assertEqual(a["pattern_family"], "box")
        self.assertIsNotNone(a.get("trendline"))
        self.assertEqual(a["trendline"]["tl_event"], "support_break")
        server.STATE["config"]["pattern_family"] = "trendline"
        b = server.get_kline("600000")
        self.assertEqual(b["pattern_family"], "trendline")
        self.assertIsNotNone(b["trendline"]["tl_support"])
        self.assertIsNotNone(b.get("box"))


class AnalyzeAttachesTrendlineTest(unittest.TestCase):
    def test_crypto_row_has_tl_keys(self):
        bars = _ascending_hl_bars(break_last=True)
        row = sc.analyze_crypto("BTCUSDT", 12.4, 1.2, bars, box_mode="classic")
        self.assertIsNotNone(row)
        self.assertTrue(row["tl_has_line"])
        self.assertEqual(row["tl_event"], "support_break")
        self.assertGreaterEqual(row["score"], 0)
        self.assertNotIn("tl_points", row)


class DashboardTrendlineContractTest(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "dashboard.html").read_text(encoding="utf-8")

    def test_family_selector_and_filters(self):
        self.assertIn("趋势线", self.html)
        self.assertIn('data-family="trendline"', self.html)
        self.assertIn("全部有线", self.html)
        self.assertIn("刚跌破支撑", self.html)
        self.assertIn("刚突破压力", self.html)
        self.assertIn("tlFilterWrap", self.html)
        self.assertIn("setTlFilter", self.html)
        self.assertIn("FAMILY_TOAST", self.html)

    def test_chart_draws_slanted_lines(self):
        self.assertIn("tl_points", self.html)
        self.assertIn("strokeTl", self.html)
        self.assertIn("跌破支撑", self.html)
        self.assertIn("突破压力", self.html)

    def test_rescan_hint_and_hide_box_mode(self):
        self.assertIn("请强制重扫以更新", self.html)
        self.assertIn('fam !== "box"', self.html)


if __name__ == "__main__":
    unittest.main()
