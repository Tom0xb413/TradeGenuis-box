#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
趋势线（摆动高低点连线）识别。

形态族 `pattern_family=trendline`：把最近一段摆动低点连成上升支撑、
摆动高点连成下降压力，得到价格×时间边界 L(t)。若最新 K 的 **Close**
越过该边界（带 α 缓冲），则趋势结构改变——对应看板用户把趋势线比作
「期权到期」：到点、越线，结构即切换。

与 `box_engine` 四条件 100 分制、`pattern_flag` 高位旗形互相独立；本模块
只产出 `tl_*` 字段，不改评分。A 股与币圈共用同一套 OHLC 规则。

Phase 1：上升支撑 + 下降压力 + 跌破/突破事件。
Phase 2（脚手架）：三角形/楔形，`tl_pattern` 预留 `triangle`，本版恒为 `none`。

定稿参数见模块常量与 docs/pattern-trendline.md。
"""
from __future__ import annotations

TL_FAMILY = "trendline"
TL_EVENTS = ("none", "support_break", "resistance_break")
TL_QUALITY = ("ok", "weak", "none")
# Phase 2：triangle 未启用；normalize 时非法值回落 none
TL_PATTERNS = ("none", "triangle")

# --------------------------------------------------------------------------- #
# 定稿阈值（合成测试与文档以此为准）
# --------------------------------------------------------------------------- #
SWING_K = 3                 # 摆动窗口：左右各 k 根（闭区间 [i-k, i+k]）
LOOKBACK = 80               # 只用最近 N 根上的摆动点拟合
MIN_SPAN_BARS = 8           # 锚点最小间隔（根）
MIN_TOUCHES = 2             # 有效线至少 2 个触点（含两端）
TOUCH_TOL = 0.006           # 触点容差：|price−L|/L ≤ 0.6%（0.3%–1% 中段）
PIERCE_TOL = 0.008          # 中段「重刺穿」：Close 越过 L 达 0.8%
MAX_MID_PIERCE = 2          # 两锚点之间最多允许 2 根收盘重刺穿，否则否决
MIN_RISE = 0.002            # 支撑要求更高低 / 压力要求更低高，至少 0.2%
MAX_SLOPE_NORM = 0.02       # |Δp/Δt| / 均价 超过 2%/根视为过陡，丢弃
BREACH_ALPHA = 0.003        # 突破缓冲 α=0.3%（0.2%–0.5% 中段）
EVENT_LOOK = 3              # 只在最近 3 根上标注「刚」跌破/突破
MAX_SWINGS = 10             # 每侧最多取最近 10 个摆动点做两两拟合
MIN_BARS = 24               # 序列过短则不拟合


def empty_trendline_fields() -> dict:
    """无趋势线时的扫描行 / K 线字段，保证 key 齐全。"""
    return {
        "tl_support": None,
        "tl_resistance": None,
        "tl_event": "none",
        "tl_event_bar": None,
        "tl_quality": "none",
        "tl_pattern": "none",
        "tl_has_line": False,
        "tl_points": {"support": [], "resistance": []},
    }


def trendline_row_fields(tl: dict | None) -> dict:
    """扫描行上的趋势线字段；不含 K 线用的 tl_points，避免 JSON 膨胀。"""
    src = tl if tl else empty_trendline_fields()
    return {
        "tl_support": src.get("tl_support"),
        "tl_resistance": src.get("tl_resistance"),
        "tl_event": src.get("tl_event") or "none",
        "tl_event_bar": src.get("tl_event_bar"),
        "tl_quality": src.get("tl_quality") or "none",
        "tl_pattern": src.get("tl_pattern") or "none",
        "tl_has_line": bool(src.get("tl_has_line")),
    }


def line_at(t1: int, p1: float, t2: int, p2: float, t: int) -> float:
    """L(t) = p1 + (p2−p1)/(t2−t1)×(t−t1)。t 为 K 线整数下标。"""
    dt = t2 - t1
    if dt == 0:
        return float(p1)
    return float(p1) + (float(p2) - float(p1)) / dt * (t - t1)


def find_swing_pivots(bars: list[dict] | None, k: int = SWING_K) -> tuple[list[tuple], list[tuple]]:
    """
    找局部高低点。返回 (highs, lows)，元素为 (index, price, date)。

    局部高：High[i] == max(High[i−k : i+k] 闭区间)；局部低对 Low 同理。
    需求示例切片 [i−k:i+k] 为示意；实现用左右各 k 根（含自身共 2k+1），
    与常见 fractal / 摆动点一致。序列两端不足 k 根的位置不标。
    相邻 ≤k 根的同侧点合并为更极端的那个（低点取更低，高点取更高）。
    """
    if not bars or k < 1:
        return [], []
    n = len(bars)
    highs: list[tuple] = []
    lows: list[tuple] = []
    if n < 2 * k + 1:
        return [], []
    for i in range(k, n - k):
        window = bars[i - k:i + k + 1]
        h = float(bars[i]["high"])
        lo = float(bars[i]["low"])
        if h == max(float(b["high"]) for b in window):
            highs.append((i, h, str(bars[i].get("date") or "")))
        if lo == min(float(b["low"]) for b in window):
            lows.append((i, lo, str(bars[i].get("date") or "")))
    return _collapse_pivots(highs, "high", k), _collapse_pivots(lows, "low", k)


def _collapse_pivots(pivots: list[tuple], kind: str, k: int) -> list[tuple]:
    """把间隔 ≤k 的同侧摆动点收成一个，避免平台期连标。"""
    if not pivots:
        return []
    out = [pivots[0]]
    for p in pivots[1:]:
        prev = out[-1]
        if p[0] - prev[0] <= k:
            if kind == "low":
                out[-1] = p if p[1] < prev[1] else prev
            else:
                out[-1] = p if p[1] > prev[1] else prev
        else:
            out.append(p)
    return out


def _count_touches(pivots: list[tuple], t1: int, p1: float, t2: int, p2: float) -> int:
    n = 0
    for idx, price, _date in pivots:
        L = line_at(t1, p1, t2, p2, idx)
        if L <= 0:
            continue
        if abs(float(price) - L) / L <= TOUCH_TOL:
            n += 1
    return n


def _mid_close_pierces(bars: list[dict], t1: int, p1: float, t2: int, p2: float,
                       side: str) -> int:
    """两锚点之间（不含端点）收盘重刺穿次数。用 Close，与突破口径一致。"""
    n = 0
    lo, hi = min(t1, t2) + 1, max(t1, t2)
    for i in range(lo, hi):
        L = line_at(t1, p1, t2, p2, i)
        if L <= 0:
            continue
        c = float(bars[i]["close"])
        if side == "support" and c < L * (1.0 - PIERCE_TOL):
            n += 1
        elif side == "resistance" and c > L * (1.0 + PIERCE_TOL):
            n += 1
    return n


def _stale_broken(bars: list[dict], t1: int, p1: float, t2: int, p2: float,
                  side: str) -> bool:
    """
    第二锚点之后、EVENT_LOOK 之前若已有收盘越过 α，则这条线是旧结构，丢弃。
    最近 EVENT_LOOK 根允许越线（那是「刚」事件窗口）。
    """
    last = len(bars) - 1
    hold_end = last - EVENT_LOOK
    for i in range(t2 + 1, hold_end + 1):
        L = line_at(t1, p1, t2, p2, i)
        if L <= 0:
            continue
        c = float(bars[i]["close"])
        if side == "support" and c < L * (1.0 - BREACH_ALPHA):
            return True
        if side == "resistance" and c > L * (1.0 + BREACH_ALPHA):
            return True
    return False


def _best_line(bars: list[dict], pivots: list[tuple], side: str) -> dict | None:
    """
    在最近摆动点中两两连线，挑触点最多、其次端点更新、再次跨度更长的一条。

    支撑：更高低（升线）；压力：更低高（降线）。不做水平箱（那是 box 族）。
    """
    if len(pivots) < 2:
        return None
    last_idx = len(bars) - 1
    cut = max(0, last_idx + 1 - LOOKBACK)
    pts = [p for p in pivots if p[0] >= cut][-MAX_SWINGS:]
    if len(pts) < 2:
        return None
    best: dict | None = None
    best_key = None
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            t1, p1, d1 = pts[i]
            t2, p2, d2 = pts[j]
            if t2 - t1 < MIN_SPAN_BARS:
                continue
            if side == "support":
                if p2 <= p1 * (1.0 + MIN_RISE):
                    continue
            else:
                if p2 >= p1 * (1.0 - MIN_RISE):
                    continue
            slope = (p2 - p1) / (t2 - t1)
            mean_p = 0.5 * (p1 + p2)
            if mean_p <= 0:
                continue
            if abs(slope) / mean_p > MAX_SLOPE_NORM:
                continue
            touches = _count_touches(pts, t1, p1, t2, p2)
            if touches < MIN_TOUCHES:
                continue
            pierces = _mid_close_pierces(bars, t1, p1, t2, p2, side)
            if pierces > MAX_MID_PIERCE:
                continue
            if _stale_broken(bars, t1, p1, t2, p2, side):
                continue
            key = (touches, t2, t2 - t1, -pierces)
            if best_key is None or key > best_key:
                best_key = key
                L_now = line_at(t1, p1, t2, p2, last_idx)
                quality = "ok" if touches >= 3 else "weak"
                best = {
                    "side": side,
                    "t1": t1,
                    "p1": round(float(p1), 4),
                    "t2": t2,
                    "p2": round(float(p2), 4),
                    "date1": d1,
                    "date2": d2,
                    "slope": round(float(slope), 6),
                    "touches": int(touches),
                    "L_now": round(float(L_now), 4),
                    "quality": quality,
                }
    return best


def _event_on(bars: list[dict], line: dict | None, side: str) -> tuple | None:
    """
    突破确认规则（定稿，及时性优先，不要求放量 / 第二根）：

      支撑跌破：前一根 Close ≥ L(t−1)，当前 Close < L(t)×(1−α)
      压力突破：前一根 Close ≤ L(t−1)，当前 Close > L(t)×(1+α)

    只扫最近 EVENT_LOOK 根，从新到旧，命中即返回 (event, date, index)。
    影线刺穿但收盘未越过 α 不算结构改变。
    """
    if not line or len(bars) < 2:
        return None
    last = len(bars) - 1
    start = max(1, last - EVENT_LOOK + 1)
    t1, p1, t2, p2 = line["t1"], line["p1"], line["t2"], line["p2"]
    for i in range(last, start - 1, -1):
        L = line_at(t1, p1, t2, p2, i)
        Lp = line_at(t1, p1, t2, p2, i - 1)
        if L <= 0 or Lp <= 0:
            continue
        c = float(bars[i]["close"])
        cp = float(bars[i - 1]["close"])
        if side == "support":
            if cp >= Lp and c < L * (1.0 - BREACH_ALPHA):
                return ("support_break", str(bars[i].get("date") or ""), i)
        else:
            if cp <= Lp and c > L * (1.0 + BREACH_ALPHA):
                return ("resistance_break", str(bars[i].get("date") or ""), i)
    return None


def _points_for(line: dict | None, bars: list[dict]) -> list[dict]:
    """图表用端点：锚点 1、锚点 2、序列最后一根（外推 L(t)）。"""
    if not line or not bars:
        return []
    last = len(bars) - 1
    t1, p1, t2, p2 = line["t1"], line["p1"], line["t2"], line["p2"]
    idxs = []
    for i in (t1, t2, last):
        if 0 <= i <= last and i not in idxs:
            idxs.append(i)
    out = []
    for i in idxs:
        out.append({
            "date": str(bars[i].get("date") or ""),
            "price": round(line_at(t1, p1, t2, p2, i), 4),
        })
    return out


def _overall_quality(support: dict | None, resistance: dict | None) -> str:
    quals = []
    if support:
        quals.append(support.get("quality") or "weak")
    if resistance:
        quals.append(resistance.get("quality") or "weak")
    if not quals:
        return "none"
    if "ok" in quals:
        return "ok"
    return "weak"


def detect_trendline(bars: list[dict] | None) -> dict:
    """
    在 OHLCV 序列上拟合最近一段上升支撑 / 下降压力，并标注最新越线事件。

    摆动点 → 同侧两两连线 → 触点/刺穿/斜率门控 → 独立保留最佳支撑与压力。
    事件只看 Close 与 α，见 _event_on。Phase 2 的 tl_pattern 本版恒为 none。
    """
    out = empty_trendline_fields()
    if not bars or len(bars) < MIN_BARS:
        return out
    _highs, lows = find_swing_pivots(bars, SWING_K)
    highs, lows = _highs, lows
    support = _best_line(bars, lows, "support")
    resistance = _best_line(bars, highs, "resistance")
    out["tl_support"] = support
    out["tl_resistance"] = resistance
    out["tl_has_line"] = bool(support or resistance)
    out["tl_quality"] = _overall_quality(support, resistance)
    out["tl_pattern"] = "none"
    out["tl_points"] = {
        "support": _points_for(support, bars),
        "resistance": _points_for(resistance, bars),
    }

    ev_s = _event_on(bars, support, "support")
    ev_r = _event_on(bars, resistance, "resistance")
    chosen = None
    if ev_s and ev_r:
        # 同一窗口两条都破：取更新的一根；同根则取相对偏离更大的
        if ev_s[2] != ev_r[2]:
            chosen = ev_s if ev_s[2] > ev_r[2] else ev_r
        else:
            i = ev_s[2]
            Ls = line_at(support["t1"], support["p1"], support["t2"], support["p2"], i)
            Lr = line_at(resistance["t1"], resistance["p1"],
                         resistance["t2"], resistance["p2"], i)
            c = float(bars[i]["close"])
            ds = abs(c - Ls) / Ls if Ls else 0.0
            dr = abs(c - Lr) / Lr if Lr else 0.0
            chosen = ev_s if ds >= dr else ev_r
    else:
        chosen = ev_s or ev_r
    if chosen:
        out["tl_event"] = chosen[0]
        out["tl_event_bar"] = chosen[1]
    return out
