#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
高位旗形 / 杯柄（柄）识别。

形态族 `pattern_family=high_flag`：放量突破 pole 之后，价格不深回撤，
在高位缩量收窄整理（旗面 / 杯柄的柄），当前 K 可选择向上二次突破买入。

与 `box_engine` 的 classic/p0/p1 箱体四条件评分互相独立；本模块只产出
旗形字段，不改 100 分制。A 股与币圈共用同一套 OHLCV 规则。

定稿参数见模块常量与 docs/pattern-high-flag.md。
"""
from __future__ import annotations

PATTERN_FAMILIES = ("box", "high_flag")
DEFAULT_PATTERN_FAMILY = "box"

# --------------------------------------------------------------------------- #
# 定稿阈值（合成测试与文档以此为准；可在小范围内微调）
# --------------------------------------------------------------------------- #
POLE_BODY_PCT = 0.05            # pole：实体涨幅或相对昨收 ≥ 5%
POLE_VOL_MULT = 2.0             # pole 量能 ≥ 2.0 × 前 20 根均量
POLE_LOOKBACK = 40              # 从最新一根往前最多搜 40 根找 pole
POLE_BASE_BARS = 5              # pole_gain：含 pole 在内最近 1–5 根最低点
POLE_GAIN_STRONG = 0.15         # 累计涨幅 ≥ 15% 作为 quality=strong 条件之一
VOL_MA = 20

HANDLE_MIN = 2                  # 旗面最短根数
HANDLE_MAX = 15                 # 旗面最长根数
HANDLE_DEPTH = 0.08             # 浅回撤：相对 pole 收盘最多 8%
HANDLE_VOL_DRY = 0.55           # 缩量：旗面均量 ≤ 0.55 × pole 量
HANDLE_RANGE_MAX = 0.08         # 收窄：(maxHigh−minLow)/minLow < 8%
HANDLE_BODY_RATIO = 0.40        # 软条件：旗面均实体 < 0.4 × pole 实体（只影响 quality）

SECONDARY_VOL_MULT = 1.40       # 二次买点：现量 > 1.4 × 旗面均量

FLAG_STAGES = ("none", "handle", "secondary_buy")


def normalize_pattern_family(family: str | None) -> str:
    """非法 / 空值回落 box，保证旧配置与缺省行为不变。"""
    f = (family or "").strip().lower()
    return f if f in PATTERN_FAMILIES else DEFAULT_PATTERN_FAMILY


def empty_flag_fields() -> dict:
    """无旗形时的扫描行 / K 线字段，保证 key 齐全。"""
    return {
        "pattern": "none",
        "flag_stage": "none",
        "pole_date": None,
        "pole_gain": None,
        "handle_len": None,
        "vol_dry_ratio": None,
        "retrace_depth": None,
        "handle_high": None,
        "handle_low": None,
        "flag_quality": None,
        "is_handle_consolidation": False,
        "secondary_breakout_buy": False,
        "pole_index": None,
        "handle_start": None,
        "handle_end": None,
        "secondary_break_level": None,
        "pole_vol": None,
        "pole_close": None,
    }


def flag_row_fields(flag: dict | None) -> dict:
    """扫描行上的旗形字段；不含箱体评分。"""
    src = flag if flag else empty_flag_fields()
    out = empty_flag_fields()
    for k in out:
        if k in src:
            out[k] = src[k]
    return out


def _vol_ma(bars: list[dict], idx: int, period: int = VOL_MA) -> float:
    """前 period 根均量（不含当前 K）。前序不足 5 根时用可得样本。"""
    prev = bars[max(0, idx - period):idx]
    if len(prev) >= 5:
        return sum(float(b["vol"]) for b in prev) / len(prev)
    sl = bars[max(0, idx - period):idx + 1]
    return (sum(float(b["vol"]) for b in sl) / len(sl)) if sl else 1.0


def _body(b: dict) -> float:
    return abs(float(b["close"]) - float(b["open"]))


def _is_pole(bars: list[dict], idx: int) -> bool:
    """
    Pole 日：放量大阳 / 突破日。

    硬条件（两条都要）：
      1. 涨幅：(close−open)/open ≥ 5%，或相对昨收 (close−prev)/prev ≥ 5%
         （覆盖 A 股一字涨停：实体可为 0，但相对昨收约 10%/20%）
      2. 量能 ≥ 2.0 × MA20

    不强制累计 15%：该值写入 pole_gain，用于 quality，见 detect 文档。
    """
    if idx < 0 or idx >= len(bars):
        return False
    b = bars[idx]
    o = float(b["open"])
    c = float(b["close"])
    v = float(b.get("vol") or 0)
    if o <= 0 or c <= 0:
        return False
    body_pct = (c - o) / o
    prev_pct = 0.0
    if idx > 0:
        prev_c = float(bars[idx - 1]["close"])
        if prev_c > 0:
            prev_pct = (c - prev_c) / prev_c
    if max(body_pct, prev_pct) < POLE_BODY_PCT:
        return False
    vma = _vol_ma(bars, idx)
    if vma <= 0 or v < POLE_VOL_MULT * vma:
        return False
    return True


def _pole_gain_pct(bars: list[dict], pole_idx: int) -> float:
    """含 pole 在内最近最多 5 根最低价 → pole 收盘的累计涨幅（%）。"""
    sl = bars[max(0, pole_idx - (POLE_BASE_BARS - 1)):pole_idx + 1]
    base = min(float(b["low"]) for b in sl) if sl else 0.0
    close = float(bars[pole_idx]["close"])
    if base <= 0:
        return 0.0
    return round((close - base) / base * 100.0, 2)


def _handle_ok(handle: list[dict], pole: dict, pole_idx: int, bars: list[dict]) -> dict | None:
    """
    旗面硬条件（全部满足才返回指标 dict）：
      1. 浅回撤：min(Low) ≥ min(pole 中轴, Close_pole×(1−8%))
      2. 缩量：mean(Vol) ≤ 0.55 × Vol_pole
      3. 收窄：(maxHigh−minLow)/minLow < 8%
    软条件（quality）：均实体 < 0.4×pole 实体；min(Vol) < pole 处 MA20。
    """
    if not handle or not (HANDLE_MIN <= len(handle) <= HANDLE_MAX):
        return None
    p_h = float(pole["high"])
    p_l = float(pole["low"])
    p_c = float(pole["close"])
    p_v = float(pole.get("vol") or 0)
    if p_v <= 0 or p_c <= 0:
        return None
    pole_mid = p_l + 0.5 * (p_h - p_l)
    floor = min(pole_mid, p_c * (1.0 - HANDLE_DEPTH))

    h_high = max(float(b["high"]) for b in handle)
    h_low = min(float(b["low"]) for b in handle)
    if h_low <= 0 or h_low < floor:
        return None
    rng = (h_high - h_low) / h_low
    if rng >= HANDLE_RANGE_MAX:
        return None

    mean_vol = sum(float(b["vol"]) for b in handle) / len(handle)
    dry = mean_vol / p_v
    if dry > HANDLE_VOL_DRY:
        return None

    retrace = max(0.0, (p_c - h_low) / p_c)
    pole_body = _body(pole)
    mean_body = sum(_body(b) for b in handle) / len(handle)
    tight_bodies = pole_body <= 0 or mean_body < HANDLE_BODY_RATIO * pole_body
    min_hv = min(float(b["vol"]) for b in handle)
    vma = _vol_ma(bars, pole_idx)
    dry_min = vma <= 0 or min_hv < vma

    return {
        "handle_high": round(h_high, 4),
        "handle_low": round(h_low, 4),
        "vol_dry_ratio": round(dry, 4),
        "retrace_depth": round(retrace * 100.0, 2),
        "mean_vol": mean_vol,
        "tight_bodies": tight_bodies,
        "dry_min": dry_min,
        "range_pct": round(rng * 100.0, 2),
    }


def _quality(pole_gain_pct: float, info: dict, stage: str) -> str:
    """strong：累计涨幅≥15% 且缩量比≤0.45 且振幅<5% 且软条件命中；否则 ok。"""
    if (
        pole_gain_pct >= POLE_GAIN_STRONG * 100.0
        and info["vol_dry_ratio"] <= 0.45
        and info["range_pct"] < 5.0
        and info["tight_bodies"]
        and info["dry_min"]
    ):
        return "strong"
    if stage == "secondary_buy" and info["vol_dry_ratio"] <= 0.50:
        return "ok"
    return "ok"


def _pack(bars: list[dict], pole_idx: int, handle: list[dict],
          handle_start: int, handle_end: int, info: dict, stage: str) -> dict:
    pole = bars[pole_idx]
    gain = _pole_gain_pct(bars, pole_idx)
    out = empty_flag_fields()
    out.update({
        "pattern": "high_flag",
        "flag_stage": stage,
        "pole_date": pole.get("date"),
        "pole_gain": gain,
        "handle_len": len(handle),
        "vol_dry_ratio": info["vol_dry_ratio"],
        "retrace_depth": info["retrace_depth"],
        "handle_high": info["handle_high"],
        "handle_low": info["handle_low"],
        "flag_quality": _quality(gain, info, stage),
        "is_handle_consolidation": stage == "handle",
        "secondary_breakout_buy": stage == "secondary_buy",
        "pole_index": pole_idx,
        "handle_start": handle_start,
        "handle_end": handle_end,
        "secondary_break_level": info["handle_high"],
        "pole_vol": round(float(pole.get("vol") or 0), 2),
        "pole_close": round(float(pole["close"]), 4),
    })
    return out


def _is_secondary_buy(bar: dict, info: dict) -> bool:
    """当前 K：收盘站上旗面最高，且量能 > 1.4 × 旗面均量。"""
    if not bar or not info:
        return False
    mean_vol = info.get("mean_vol") or 0.0
    if mean_vol <= 0:
        return False
    c = float(bar["close"])
    v = float(bar.get("vol") or 0)
    return c > float(info["handle_high"]) and v > SECONDARY_VOL_MULT * mean_vol


def detect_high_flag(bars: list[dict] | None) -> dict:
    """
    在 OHLCV 序列上识别「最近一段」高位旗形。

    Pole 规则（硬）：单日实体或相对昨收 ≥ 5%，且量 ≥ 2.0×MA20。
    累计涨幅（pole 日前最多 5 根最低点到 pole 收盘）只写入 pole_gain，
    ≥15% 用于 quality=strong，**不是**入选硬门槛——避免把单日放量突破挡掉。

    从最近的 pole 往回搜：同一根 pole 优先判定「二次买点」（旗面不含最后一根），
    否则判定「旗面整理中」（旗面含最后一根）。窗口长度必须 ∈ [2, 15]。

    返回字段见 empty_flag_fields()；未命中时 pattern/flag_stage 为 none。
    """
    if not bars:
        return empty_flag_fields()
    n = len(bars)
    # MA20 + pole + 最短旗面
    if n < VOL_MA + 1 + HANDLE_MIN:
        return empty_flag_fields()

    last = n - 1
    oldest = max(VOL_MA, last - POLE_LOOKBACK)
    # 最老的 pole 仍要给得起 HANDLE_MAX 旗面 + 可选 1 根二次突破
    for pole_idx in range(last - HANDLE_MIN, oldest - 1, -1):
        bars_after = last - pole_idx
        if bars_after < HANDLE_MIN:
            continue
        if bars_after > HANDLE_MAX + 1:
            continue
        if not _is_pole(bars, pole_idx):
            continue
        pole = bars[pole_idx]

        # 二次买点：旗面 = pole 之后到倒数第二根
        handle_buy = bars[pole_idx + 1:last]
        if HANDLE_MIN <= len(handle_buy) <= HANDLE_MAX:
            info = _handle_ok(handle_buy, pole, pole_idx, bars)
            if info and _is_secondary_buy(bars[last], info):
                return _pack(
                    bars, pole_idx, handle_buy,
                    pole_idx + 1, last - 1, info, "secondary_buy",
                )

        # 整理中：旗面含当前 K
        handle = bars[pole_idx + 1:n]
        if HANDLE_MIN <= len(handle) <= HANDLE_MAX:
            info = _handle_ok(handle, pole, pole_idx, bars)
            if info:
                return _pack(
                    bars, pole_idx, handle,
                    pole_idx + 1, last, info, "handle",
                )
    return empty_flag_fields()
