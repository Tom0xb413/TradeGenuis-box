#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
箱体识别引擎（多模式）。

模式：
  classic — 原 compute_box：窗口最高/最低 + BOX_NEAR/CLOSE/SHADOW/TEST_VOL
  p0      — 分位数边界 + 振幅门控 + 更严上沿试盘
  p1      — 斜向通道骨架（OLS + 残差分位数 + ADX），本版本未实现

纯函数，不读配置文件。扫描/看板把所选 mode 传进来即可。
上沿事件（试盘 / 潜在突破 / 确认 / 失败）相对该模式的 R 另行标注，不改四条件评分。
"""
from __future__ import annotations

# --------------------------------------------------------------------------- #
# 常量（classic 数值保持与历史 scanner.compute_box 一致）
# --------------------------------------------------------------------------- #
BOX_MODES = ("classic", "p0", "p1")
DEFAULT_BOX_MODE = "classic"

BOX_LOOK = 60           # 箱体窗口（根K）
BOX_NEAR = 0.985        # classic：逼近上沿（high >= box_high*0.985）
BOX_CLOSE = 1.005       # classic / p0：收盘未有效站上上沿
BOX_SHADOW = 0.30       # classic：上影线占比阈值
TEST_VOL = 0.70         # classic：试盘日量能下限（对箱体窗口均量）

# p0 参数（可微调，以下为本 PR 定稿）
P0_AMP_MAX = 0.15           # 振幅门控： (box_high-box_low) / mean(close) < 15%
P0_HIGH_Q = 0.95            # 上沿 = 窗口 High 的 0.95 分位
P0_LOW_Q = 0.05             # 下沿 = 窗口 Low 的 0.05 分位
P0_CLOSE_MULT = 1.005       # 收盘回落到上沿内侧（与 BOX_CLOSE 相同）
P0_SHADOW_BODY = 2.0        # 上影线 ≥ 2 倍实体
P0_SHADOW_RANGE = 0.40      # 上影线 / (H-L) ≥ 0.4
P0_TEST_VOL_MULT = 1.8      # 量能 ≥ 1.8 × 前 20 根均量
P0_TEST_VOL_MA = 20
P0_POST_HOLD_BARS = 3       # 软校验：随后 1–3 根收盘站上中轴（仅元数据，不否决试盘）

# 上沿事件分类（相对当前模式的 R=box_high；与评分 tests 独立）
EDGE_EVENTS = (
    "none",
    "test",
    "breakout_candidate",
    "breakout_confirmed",
    "breakout_failed",
)
EVT_TEST_CLOSE = 1.005          # 试盘：收盘回到 R 内侧
EVT_TEST_SHADOW_RANGE = 0.40    # 上影 / (H-L) ≥ 0.4（对齐 P0）
EVT_TEST_SHADOW_BODY = 2.0      # 上影 ≥ 2× 实体（对齐 P0，取 1.5–2 的上沿）
EVT_TEST_VOL_MULT = 1.8         # 试盘量能 ≥ 1.8× MA20（对齐 P0）
EVT_BO_ALPHA = 0.015            # 潜在突破：Close ≥ R × (1+1.5%)
EVT_BO_BODY_RANGE = 0.60        # 实体 / 振幅 ≥ 0.60
EVT_BO_SHADOW_RANGE_MAX = 0.15  # 上影 / 振幅 ≤ 0.15（光脚阳线）
EVT_BO_VOL_MULT = 1.8           # 突破量能 ≥ 1.8× MA20
EVT_BO_CONFIRM_BARS = 2         # 确认：随后 2 根收盘仍 > R（事后标签，需 t+2 已收盘）

P1_STATUS = "not_implemented"
P1_NOTE = "P1 斜向通道尚未实现（计划：OLS 中轴 + 残差分位数通道 + ADX 过滤），当前回退经典箱体"


def normalize_box_mode(mode: str | None) -> str:
    """非法 / 空值回落 classic，保证旧调用与缺省配置行为不变。"""
    m = (mode or "").strip().lower()
    return m if m in BOX_MODES else DEFAULT_BOX_MODE


def _quantile(values: list[float], q: float) -> float:
    """
    线性插值分位数，口径对齐 numpy.quantile(..., method='linear')：
    位置 = q * (n-1)，在相邻排序样本之间插值。
    """
    if not values:
        raise ValueError("quantile of empty sequence")
    xs = sorted(values)
    if q <= 0:
        return float(xs[0])
    if q >= 1:
        return float(xs[-1])
    n = len(xs)
    pos = q * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(xs[lo] * (1.0 - frac) + xs[hi] * frac)


def select_box_window(bars: list[dict], look: int = BOX_LOOK):
    """
    与 classic 相同的突破截断：
    若最近 15 根内收盘有效突破「前 40 根最高价 × 1.005」，窗口截止到突破日（不含突破 K）。
    然后取截止日前 look 根。
    返回 (start, box_end, win)；样本不足时返回 None。
    """
    n = len(bars)
    if n < 40:
        return None
    box_end = n
    for i in range(max(0, n - 15), n):
        if i >= 40:
            prev_high = max(b["high"] for b in bars[i - 40:i])
            if bars[i]["close"] > prev_high * 1.005:
                box_end = i
                break
    start = max(0, box_end - look)
    win = bars[start:box_end]
    if not win:
        return None
    return start, box_end, win


def _pos_span(price: float, low: float, high: float) -> tuple[float, float]:
    pos = (price - low) / (high - low) * 100 if high > low else 0.0
    span = (high - low) / low * 100 if low > 0 else 0.0
    return round(max(0.0, min(100.0, pos)), 1), round(span, 1)


def _pack(low: float, high: float, tests: int, test_dates: list, win: list[dict],
          box_end: int, price: float, mode: str, extra: dict | None = None) -> dict:
    pos, span = _pos_span(price, low, high)
    out = {
        "box_low": round(low, 2),
        "box_high": round(high, 2),
        "tests": tests,
        "test_dates": test_dates[-8:],
        "pos_pct": pos,
        "span_pct": span,
        "window": f"{win[0]['date']} ~ {win[-1]['date']}",
        "box_end": box_end,
        "box_mode": mode,
    }
    if extra:
        out.update(extra)
    return out


def compute_box_classic(bars: list[dict]) -> dict | None:
    """
    经典箱体：窗口 max(high)/min(low)，试盘规则 BOX_NEAR / BOX_CLOSE / BOX_SHADOW / TEST_VOL。
    逻辑与历史 scanner.compute_box 一致，仅多返回 box_mode=classic。
    """
    sliced = select_box_window(bars)
    if sliced is None:
        return None
    _start, box_end, win = sliced
    high = max(b["high"] for b in win)
    low = min(b["low"] for b in win)
    if high <= low:
        return None
    vol_avg = sum(b["vol"] for b in win) / len(win) or 1.0

    tests = 0
    test_dates = []
    for b in win:
        h, l, c, o, v = b["high"], b["low"], b["close"], b["open"], b["vol"]
        if h <= l:
            continue
        if h >= high * BOX_NEAR and c <= high * BOX_CLOSE and v >= TEST_VOL * vol_avg:
            up_shadow = h - max(o, c)
            if up_shadow > 0 and (up_shadow / (h - l) >= BOX_SHADOW or h >= high * 0.995):
                tests += 1
                test_dates.append(b["date"])

    packed = _pack(low, high, tests, test_dates, win, box_end, bars[-1]["close"], "classic")
    return _with_edge_events(packed, bars, high, _start)


def empty_edge_fields() -> dict:
    """无箱体 / 无上沿时的事件字段，保证扫描行 key 齐全。"""
    return {
        "edge_event": "none",
        "edge_event_date": None,
        "last_edge_event": "none",
        "last_edge_event_date": None,
        "edge_events": [],
        "breakout_candidates": 0,
        "breakout_confirmed": 0,
        "breakout_failed": 0,
    }


def _bar_geom(b: dict) -> tuple[float, float, float, float, float] | None:
    h, l, c, o = float(b["high"]), float(b["low"]), float(b["close"]), float(b["open"])
    rng = h - l
    if rng <= 0:
        return None
    body = abs(c - o)
    up_shadow = h - max(o, c)
    return h, c, rng, body, up_shadow


def _shape_vs_r(b: dict, r: float, v_ma: float) -> str | None:
    """
    单根形态（不含时间过滤）。High 触及或刺穿 R 才参与。
    突破与试盘互斥：Close ≥ R×1.015 走突破，Close ≤ R×1.005 走试盘；中间地带不标。
    """
    geom = _bar_geom(b)
    if geom is None or r <= 0:
        return None
    h, c, rng, body, up_shadow = geom
    if h < r:
        return None
    v = float(b.get("vol") or 0)
    if (c >= r * (1.0 + EVT_BO_ALPHA)
            and (body / rng) >= EVT_BO_BODY_RANGE
            and (up_shadow / rng) <= EVT_BO_SHADOW_RANGE_MAX
            and v_ma > 0 and v >= EVT_BO_VOL_MULT * v_ma):
        return "breakout_candidate"
    if (c <= r * EVT_TEST_CLOSE
            and (up_shadow / rng) >= EVT_TEST_SHADOW_RANGE
            and up_shadow >= EVT_TEST_SHADOW_BODY * body
            and v_ma > 0 and v >= EVT_TEST_VOL_MULT * v_ma):
        return "test"
    return None


def _resolve_breakout(bars: list[dict], idx: int, r: float) -> str:
    """
    潜在突破的事后时间过滤。确认需要 t+2 两根都已收盘且收盘价仍 > R，
    因此最新一根最多标 breakout_candidate，不能在当日标 confirmed。
    """
    n = len(bars)
    if idx + 1 >= n:
        return "breakout_candidate"
    if bars[idx + 1]["close"] < r:
        return "breakout_failed"
    if idx + 2 >= n:
        return "breakout_candidate"
    if bars[idx + 2]["close"] > r:
        return "breakout_confirmed"
    return "breakout_failed"


def classify_edge_events(bars: list[dict], r: float, start_idx: int = 0) -> dict:
    """
    相对上沿 R 扫描 start_idx 之后的 K 线（含箱体截断后的突破日）。
    tests 计分仍由各模式自己统计；这里只附加 edge_* / breakout_* 元数据。
    """
    out = empty_edge_fields()
    if not bars or r is None or r <= 0:
        return out
    start_idx = max(0, min(int(start_idx), len(bars)))
    events = []
    for idx in range(start_idx, len(bars)):
        raw = _shape_vs_r(bars[idx], r, _vol_ma(bars, idx))
        if raw is None:
            continue
        kind = _resolve_breakout(bars, idx, r) if raw == "breakout_candidate" else raw
        events.append({
            "date": bars[idx]["date"],
            "kind": kind,
            "index": idx,
        })
    n_cand = sum(1 for e in events if e["kind"] in (
        "breakout_candidate", "breakout_confirmed", "breakout_failed"))
    out["breakout_candidates"] = n_cand
    out["breakout_confirmed"] = sum(1 for e in events if e["kind"] == "breakout_confirmed")
    out["breakout_failed"] = sum(1 for e in events if e["kind"] == "breakout_failed")
    out["edge_events"] = events[-12:]
    if events:
        out["last_edge_event"] = events[-1]["kind"]
        out["last_edge_event_date"] = events[-1]["date"]
    last_idx = len(bars) - 1
    for e in reversed(events):
        if e["index"] == last_idx:
            out["edge_event"] = e["kind"]
            out["edge_event_date"] = e["date"]
            break
    return out


def _with_edge_events(packed: dict, bars: list[dict], r: float, start: int) -> dict:
    packed.update(classify_edge_events(bars, r, start_idx=start))
    return packed


def _vol_ma(bars: list[dict], idx: int, period: int = P0_TEST_VOL_MA) -> float:
    """
    前 period 根均量（不含当前 K）。前序不足 5 根时，退化为含当前在内的可得样本均量。
    """
    prev = bars[max(0, idx - period):idx]
    if len(prev) >= 5:
        return sum(b["vol"] for b in prev) / len(prev)
    sl = bars[max(0, idx - period):idx + 1]
    return (sum(b["vol"] for b in sl) / len(sl)) if sl else 1.0


def compute_box_p0(bars: list[dict]) -> dict | None:
    """
    P0 增强箱体：
      - 窗口截断与 classic 相同（突破判定仍用前 40 根 raw max，不用分位数）
      - 上沿 = quantile(High, 0.95)，下沿 = quantile(Low, 0.05)
      - 振幅 (high-low)/mean(close) ≥ 15% → 不成箱（仍返回边界，tests=0, box_quality=amplitude_reject）
      - 试盘更严：刺到上沿、收盘回到内侧、长上影、放量 1.8×MA20
      - 随后 1–3 根站上中轴仅为 post_hold_* 元数据，不作为扫描硬条件
    """
    sliced = select_box_window(bars)
    if sliced is None:
        return None
    start, box_end, win = sliced
    highs = [float(b["high"]) for b in win]
    lows = [float(b["low"]) for b in win]
    closes = [float(b["close"]) for b in win]
    high = _quantile(highs, P0_HIGH_Q)
    low = _quantile(lows, P0_LOW_Q)
    if high <= low:
        return None

    mean_close = sum(closes) / len(closes) if closes else 0.0
    amp = ((high - low) / mean_close) if mean_close > 0 else 0.0
    amp_pct = round(amp * 100.0, 1)
    price = bars[-1]["close"]

    extra_base = {
        "amp_pct": amp_pct,
        "amp_max": round(P0_AMP_MAX * 100.0, 1),
        "high_q": P0_HIGH_Q,
        "low_q": P0_LOW_Q,
    }

    if mean_close <= 0 or amp >= P0_AMP_MAX:
        packed = _pack(low, high, 0, [], win, box_end, price, "p0", {
            **extra_base,
            "box_quality": "amplitude_reject",
            "post_hold_tests": 0,
            "post_hold_dates": [],
        })
        return _with_edge_events(packed, bars, high, start)

    tests = 0
    test_dates = []
    hold_dates = []
    mid = (high + low) / 2.0
    for j, b in enumerate(win):
        idx = start + j
        h, l, c, o, v = b["high"], b["low"], b["close"], b["open"], b["vol"]
        if h <= l:
            continue
        if h < high:
            continue
        if c > high * P0_CLOSE_MULT:
            continue
        up_shadow = h - max(o, c)
        body = abs(c - o)
        rng = h - l
        if up_shadow <= 0:
            continue
        if up_shadow < P0_SHADOW_BODY * body:
            continue
        if up_shadow / rng < P0_SHADOW_RANGE:
            continue
        v_ma = _vol_ma(bars, idx, P0_TEST_VOL_MA)
        if v_ma <= 0 or v < P0_TEST_VOL_MULT * v_ma:
            continue
        tests += 1
        test_dates.append(b["date"])
        follow = bars[idx + 1:idx + 1 + P0_POST_HOLD_BARS]
        if follow and all(fb["close"] >= mid for fb in follow):
            hold_dates.append(b["date"])

    if tests >= 3:
        quality = "ok"
    elif tests >= 1:
        quality = "weak"
    else:
        quality = "none"

    packed = _pack(low, high, tests, test_dates, win, box_end, price, "p0", {
        **extra_base,
        "box_quality": quality,
        "post_hold_tests": len(hold_dates),
        "post_hold_dates": hold_dates[-8:],
    })
    return _with_edge_events(packed, bars, high, start)


def compute_box_p1(bars: list[dict]) -> dict | None:
    """
    P1 斜向上升通道骨架。完整算法（OLS 中轴、残差分位数通道、ADX）留给后续 PR。

    为避免误把 config 写成 p1 后扫描中断，这里回退 classic，并标注 p1_status。
    若调用方需要硬失败，可检查返回值 p1_status == 'not_implemented'。
    """
    out = compute_box_classic(bars)
    extra = {
        "p1_status": P1_STATUS,
        "box_quality": "p1_pending",
        "note": P1_NOTE,
    }
    if out is None:
        return {
            "box_low": None,
            "box_high": None,
            "tests": 0,
            "test_dates": [],
            "pos_pct": None,
            "span_pct": None,
            "window": None,
            "box_end": None,
            "box_mode": "p1",
            **empty_edge_fields(),
            **extra,
        }
    packed = dict(out)
    packed["box_mode"] = "p1"
    packed.update(extra)
    return packed


def compute_box(bars: list[dict], mode: str = DEFAULT_BOX_MODE) -> dict | None:
    """按 mode 分发。缺省 classic，与历史 compute_box(bars) 行为兼容。"""
    mode = normalize_box_mode(mode)
    if mode == "p0":
        return compute_box_p0(bars)
    if mode == "p1":
        return compute_box_p1(bars)
    return compute_box_classic(bars)


def box_row_fields(box: dict | None, mode: str = DEFAULT_BOX_MODE) -> dict:
    """扫描行上的箱体字段，供 A 股 / 币圈评分共用。不含评分逻辑。"""
    mode = normalize_box_mode(mode)
    edge = empty_edge_fields()
    if not box:
        return {
            "box_mode": mode,
            "box_low": None,
            "box_high": None,
            "pos_pct": None,
            "box_span_pct": None,
            "box_window": None,
            "tests": 0,
            "test_dates": [],
            "box_quality": None,
            **edge,
        }
    return {
        "box_mode": box.get("box_mode") or mode,
        "box_low": box.get("box_low"),
        "box_high": box.get("box_high"),
        "pos_pct": box.get("pos_pct"),
        "box_span_pct": box.get("span_pct"),
        "box_window": box.get("window"),
        "tests": int(box.get("tests") or 0),
        "test_dates": list(box.get("test_dates") or []),
        "box_quality": box.get("box_quality"),
        "edge_event": box.get("edge_event") or "none",
        "edge_event_date": box.get("edge_event_date"),
        "last_edge_event": box.get("last_edge_event") or "none",
        "last_edge_event_date": box.get("last_edge_event_date"),
        "breakout_candidates": int(box.get("breakout_candidates") or 0),
        "breakout_confirmed": int(box.get("breakout_confirmed") or 0),
        "breakout_failed": int(box.get("breakout_failed") or 0),
    }
