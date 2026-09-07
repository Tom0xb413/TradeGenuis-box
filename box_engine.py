#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
箱体识别引擎（多模式）。

模式：
  classic — 原 compute_box：窗口最高/最低 + BOX_NEAR/CLOSE/SHADOW/TEST_VOL
  p0      — 分位数边界 + 振幅门控 + 更严上沿试盘
  p1      — 斜向通道：OLS 中轴 + 残差分位数带 + 温和斜率/R²/ADX 门控

纯函数，不读配置文件。扫描/看板把所选 mode 传进来即可。
上沿事件（试盘 / 潜在突破 / 确认 / 失败）相对该模式的 R（P1 为逐根 R(t)）另行标注，不改四条件评分。
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

# 上沿事件分类（相对当前模式的 R；P1 为动态 R(t)；与评分 tests 独立）
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

# p1 斜向通道（定稿，见 docs/box-modes.md）
# 窗口：复用 select_box_window 的突破截断，回看 P1_LOOK=50（30–60 建议区间中段）。
P1_LOOK = 50                    # 专用回看根数
P1_MIN_BARS = 30                # 窗口短于 30 根不成通道
P1_HIGH_Q = 0.95                # 上轨 = quantile(High - mid, 0.95)
P1_LOW_Q = 0.05                 # 下轨 = quantile(Low - mid, 0.05)
P1_SLOPE_NORM_MIN = 0.0005      # |β1|/mean(Close) 低于此 → 近水平（flat_fallback，不硬拒）
P1_SLOPE_NORM_MAX = 0.004       # 每根归一斜率绝对值超过此 → slope_reject
P1_R2_MIN = 0.35                # Close~t 的 R² 下限（过噪）
P1_R2_MAX = 0.80                # R² 上限（近直线趋势，不像通道震荡）
P1_MIN_MID_CROSSES = 3          # Close-mid 符号变化少于此 → V 形，vshape_reject
P1_ADX_PERIOD = 14
P1_ADX_MAX = 40.0               # ADX>40 视为过猛趋势；20–40 的温和上升通道放行
P1_WIDTH_MAX = 0.25             # (R-S)/mid ≥ 25% → amplitude_reject
P1_KINDS = ("ascending", "descending", "flat_fallback")
BOX_QUALITY_REJECTS = frozenset((
    "amplitude_reject",
    "slope_reject",
    "r2_reject",
    "vshape_reject",
    "adx_reject",
))


def normalize_box_mode(mode: str | None) -> str:
    """非法 / 空值回落 classic，保证旧调用与缺省配置行为不变。"""
    m = (mode or "").strip().lower()
    return m if m in BOX_MODES else DEFAULT_BOX_MODE


def is_box_quality_reject(quality: str | None) -> bool:
    """振幅/通道门控失败：看板不应再画假箱体。"""
    return (quality or "") in BOX_QUALITY_REJECTS


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


def _r_at(r, idx: int) -> float | None:
    """常数上沿或 per-bar 序列在 idx 处的 R；无效时返回 None。"""
    if r is None:
        return None
    if isinstance(r, (int, float)):
        rv = float(r)
        return rv if rv > 0 else None
    try:
        val = r[idx]
    except (IndexError, TypeError, KeyError):
        return None
    if val is None:
        return None
    rv = float(val)
    return rv if rv > 0 else None


def _shape_vs_r(b: dict, r: float, v_ma: float) -> str | None:
    """
    单根形态（不含时间过滤）。High 触及或刺穿 R 才参与。
    突破与试盘互斥：Close ≥ R×1.015 走突破，Close ≤ R×1.005 走试盘；中间地带不标。
    """
    geom = _bar_geom(b)
    if geom is None or r is None or r <= 0:
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


def _resolve_breakout(bars: list[dict], idx: int, r) -> str:
    """
    潜在突破的事后时间过滤。确认需要 t+2 两根都已收盘且收盘价仍 > 该根 R，
    因此最新一根最多标 breakout_candidate，不能在当日标 confirmed。
    常数 R 与 per-bar R(t) 均可：后续 K 与「当时」的上沿比较。
    """
    n = len(bars)
    r0 = _r_at(r, idx)

    def later_r(j: int) -> float | None:
        rv = _r_at(r, j)
        return rv if rv is not None else r0

    if idx + 1 >= n:
        return "breakout_candidate"
    r1 = later_r(idx + 1)
    if r1 is not None and bars[idx + 1]["close"] < r1:
        return "breakout_failed"
    if idx + 2 >= n:
        return "breakout_candidate"
    r2 = later_r(idx + 2)
    if r2 is not None and bars[idx + 2]["close"] > r2:
        return "breakout_confirmed"
    return "breakout_failed"


def classify_edge_events(bars: list[dict], r, start_idx: int = 0) -> dict:
    """
    相对上沿 R 扫描 start_idx 之后的 K 线（含箱体截断后的突破日）。
    r 可以是常数，或与 bars 等长的 per-bar 序列（P1 动态 R(t)；缺省/非正视为该根无上沿）。
    tests 计分仍由各模式自己统计；这里只附加 edge_* / breakout_* 元数据。
    """
    out = empty_edge_fields()
    if not bars or r is None:
        return out
    if isinstance(r, (int, float)) and float(r) <= 0:
        return out
    start_idx = max(0, min(int(start_idx), len(bars)))
    events = []
    for idx in range(start_idx, len(bars)):
        rv = _r_at(r, idx)
        if rv is None:
            continue
        raw = _shape_vs_r(bars[idx], rv, _vol_ma(bars, idx))
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


def _with_edge_events(packed: dict, bars: list[dict], r, start: int) -> dict:
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


def _ols_line(y: list[float]) -> tuple[float, float, float]:
    """
    Close 对 t=0..n-1 的 OLS：返回 (β0, β1, R²)。
    无方差时若残差也为 0 则 R²=1，否则 0。
    """
    n = len(y)
    if n <= 0:
        return 0.0, 0.0, 0.0
    if n == 1:
        return float(y[0]), 0.0, 1.0
    xs = list(range(n))
    mean_x = (n - 1) / 2.0
    mean_y = sum(y) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (yi - mean_y) for x, yi in zip(xs, y))
    syy = sum((yi - mean_y) ** 2 for yi in y)
    beta1 = (sxy / sxx) if sxx else 0.0
    beta0 = mean_y - beta1 * mean_x
    ss_res = sum((yi - (beta0 + beta1 * x)) ** 2 for x, yi in zip(xs, y))
    if syy <= 0:
        r2 = 1.0 if ss_res <= 1e-18 else 0.0
    else:
        r2 = 1.0 - ss_res / syy
    return float(beta0), float(beta1), float(r2)


def _mid_cross_count(closes: list[float], mids: list[float]) -> int:
    """Close-mid 的符号变化次数（跳过贴中轴的 0），用于滤 V 形。"""
    signs: list[int] = []
    for c, m in zip(closes, mids):
        d = c - m
        if d > 1e-12:
            signs.append(1)
        elif d < -1e-12:
            signs.append(-1)
    if len(signs) < 2:
        return 0
    return sum(1 for a, b in zip(signs, signs[1:]) if a != b)


def compute_adx(bars: list[dict], period: int = P1_ADX_PERIOD) -> list:
    """
    Wilder ADX，与 bars 等长；预热完成前为 None。
    TR/+DM/-DM 从第 2 根起算；第一条平滑值在 index=period；
    第一条 ADX 在 index=2*period-1（period=14 → index 27）。
    """
    n = len(bars)
    adx = [None] * n
    if n < period + 1 or period <= 0:
        return adx
    tr = [0.0] * n
    pdm = [0.0] * n
    ndm = [0.0] * n
    for i in range(1, n):
        h, l = float(bars[i]["high"]), float(bars[i]["low"])
        ph, pl = float(bars[i - 1]["high"]), float(bars[i - 1]["low"])
        pc = float(bars[i - 1]["close"])
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
        up = h - ph
        dn = pl - l
        pdm[i] = up if up > dn and up > 0 else 0.0
        ndm[i] = dn if dn > up and dn > 0 else 0.0

    str_ = sum(tr[1:period + 1])
    sp = sum(pdm[1:period + 1])
    sn = sum(ndm[1:period + 1])
    dx = [None] * n

    def _dx(str_s: float, sp_s: float, sn_s: float) -> float:
        if str_s <= 0:
            return 0.0
        pdi = 100.0 * sp_s / str_s
        ndi = 100.0 * sn_s / str_s
        denom = pdi + ndi
        return (100.0 * abs(pdi - ndi) / denom) if denom else 0.0

    dx[period] = _dx(str_, sp, sn)
    for i in range(period + 1, n):
        str_ = str_ - str_ / period + tr[i]
        sp = sp - sp / period + pdm[i]
        sn = sn - sn / period + ndm[i]
        dx[i] = _dx(str_, sp, sn)

    first = period * 2 - 1
    if first >= n:
        return adx
    acc = 0.0
    cnt = 0
    for i in range(period, first + 1):
        if dx[i] is not None:
            acc += dx[i]
            cnt += 1
    if cnt <= 0:
        return adx
    adx[first] = acc / cnt
    for i in range(first + 1, n):
        adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    return adx


def _p1_kind(slope_norm: float) -> str:
    if slope_norm > P1_SLOPE_NORM_MIN:
        return "ascending"
    if slope_norm < -P1_SLOPE_NORM_MIN:
        return "descending"
    return "flat_fallback"


def _p1_tests_quality(tests: int) -> str:
    if tests >= 3:
        return "ok"
    if tests >= 1:
        return "weak"
    return "none"


def _p1_count_tests(bars: list[dict], start: int, win: list[dict], r_win: list[float]) -> tuple[int, list]:
    """P1 试盘：与 P0 同精神，但上沿为该根 R(t)。"""
    tests = 0
    test_dates = []
    for j, b in enumerate(win):
        idx = start + j
        r = r_win[j]
        h, l, c, o, v = b["high"], b["low"], b["close"], b["open"], b["vol"]
        if h <= l or r <= 0 or h < r:
            continue
        if c > r * P0_CLOSE_MULT:
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
    return tests, test_dates


def compute_box_p1(bars: list[dict]) -> dict | None:
    """
    P1 斜向通道：
      - 突破截断与 classic/p0 相同，回看 P1_LOOK=50 根（短于 30 根则放弃）
      - Close ~ t 的 OLS 得中轴 mid(t)=β1·t+β0
      - 残差：High-mid、Low-mid；上轨偏移 0.95 分位、下轨 0.05 分位 → 平行通道
      - 卡片 box_high/box_low = 全序列最后一根的 R(n-1)/S(n-1)
      - 门控：宽度、过陡斜率、R² 带、中轴穿越、ADX>40
      - 试盘 / 上沿事件相对动态 R(t)；四条件评分仍只吃 tests
    """
    sliced = select_box_window(bars, look=P1_LOOK)
    if sliced is None:
        return None
    start, box_end, win = sliced
    if len(win) < P1_MIN_BARS:
        return None

    closes = [float(b["close"]) for b in win]
    beta0, beta1, r2 = _ols_line(closes)
    mean_close = sum(closes) / len(closes) if closes else 0.0
    slope_norm = (beta1 / mean_close) if mean_close > 0 else 0.0
    mids = [beta0 + beta1 * t for t in range(len(win))]
    high_resid = [float(win[t]["high"]) - mids[t] for t in range(len(win))]
    low_resid = [float(win[t]["low"]) - mids[t] for t in range(len(win))]
    b_up = _quantile(high_resid, P1_HIGH_Q)
    b_down = _quantile(low_resid, P1_LOW_Q)
    last_t = len(bars) - 1 - start
    last_mid = beta0 + beta1 * last_t
    high = last_mid + b_up
    low = last_mid + b_down
    width = ((high - low) / last_mid) if last_mid > 0 else 0.0
    crosses = _mid_cross_count(closes, mids)
    adx_series = compute_adx(bars, P1_ADX_PERIOD)
    adx_idx = box_end - 1
    adx = adx_series[adx_idx] if 0 <= adx_idx < len(adx_series) else None
    kind = _p1_kind(slope_norm)

    r_all: list[float | None] = [None] * len(bars)
    points = []
    for idx in range(start, len(bars)):
        t = idx - start
        mid = beta0 + beta1 * t
        ru, rd = mid + b_up, mid + b_down
        r_all[idx] = ru
        points.append({
            "date": bars[idx]["date"],
            "mid": round(mid, 4),
            "up": round(ru, 4),
            "down": round(rd, 4),
        })

    extra = {
        "box_kind": kind,
        "slope": round(beta1, 6),
        "slope_norm": round(slope_norm, 6),
        "r2": round(r2, 4),
        "adx": None if adx is None else round(float(adx), 1),
        "b_up": round(b_up, 4),
        "b_down": round(b_down, 4),
        "width_pct": round(width * 100.0, 1),
        "width_max": round(P1_WIDTH_MAX * 100.0, 1),
        "mid_crosses": crosses,
        "p1_look": P1_LOOK,
        "channel_points": points,
    }
    price = bars[-1]["close"]

    reject = None
    if last_mid <= 0 or high <= low or width >= P1_WIDTH_MAX:
        reject = "amplitude_reject"
    elif abs(slope_norm) > P1_SLOPE_NORM_MAX:
        reject = "slope_reject"
    elif r2 < P1_R2_MIN or r2 > P1_R2_MAX:
        reject = "r2_reject"
    elif crosses < P1_MIN_MID_CROSSES:
        reject = "vshape_reject"
    elif adx is not None and float(adx) > P1_ADX_MAX:
        reject = "adx_reject"

    if reject:
        packed = _pack(low, high, 0, [], win, box_end, price, "p1", {
            **extra,
            "box_quality": reject,
        })
        return _with_edge_events(packed, bars, r_all, start)

    r_win = [mids[j] + b_up for j in range(len(win))]
    tests, test_dates = _p1_count_tests(bars, start, win, r_win)
    packed = _pack(low, high, tests, test_dates, win, box_end, price, "p1", {
        **extra,
        "box_quality": _p1_tests_quality(tests),
    })
    return _with_edge_events(packed, bars, r_all, start)


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
            "box_kind": None,
            "slope_norm": None,
            "r2": None,
            "adx": None,
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
        "box_kind": box.get("box_kind"),
        "slope_norm": box.get("slope_norm"),
        "r2": box.get("r2"),
        "adx": box.get("adx"),
        "edge_event": box.get("edge_event") or "none",
        "edge_event_date": box.get("edge_event_date"),
        "last_edge_event": box.get("last_edge_event") or "none",
        "last_edge_event_date": box.get("last_edge_event_date"),
        "breakout_candidates": int(box.get("breakout_candidates") or 0),
        "breakout_confirmed": int(box.get("breakout_confirmed") or 0),
        "breakout_failed": int(box.get("breakout_failed") or 0),
    }
