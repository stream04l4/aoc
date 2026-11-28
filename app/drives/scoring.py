"""Shared scoring utilities for independent drive evaluators."""

from __future__ import annotations

import math
from typing import Any

from app.drives.interfaces import clamp01
from app.perception.interfaces import is_unavailable


def linear_band(value: float, low_zero: float, high_one: float) -> float:
    """Map value linearly to [0, 1] where <= low_zero -> 0 and >= high_one -> 1.
    Also works when low_zero > high_one (inverted metric like latency or load).
    """
    if low_zero == high_one:
        return 1.0 if value >= high_one else 0.0
    if low_zero < high_one:
        if value <= low_zero:
            return 0.0
        if value >= high_one:
            return 1.0
        return clamp01((value - low_zero) / (high_one - low_zero))
    # inverted: smaller value is healthier
    if value >= low_zero:
        return 0.0
    if value <= high_one:
        return 1.0
    return clamp01((low_zero - value) / (low_zero - high_one))


def weighted_dimensions(
    dimensions: dict[str, float | None],
    weights: dict[str, float],
    default_when_all_missing: float = 0.5,
) -> tuple[float, float, list[str]]:
    """Aggregate normalized dimensions into (satisfaction, data_confidence, notes).

    Missing (None) dimensions are EXCLUDED from the denominator and lower
    data_confidence proportionally — missing data is NEVER interpreted as normal.
    """
    notes: list[str] = []
    total_possible_w = sum(max(0.0, float(w)) for w in weights.values())
    if total_possible_w <= 0:
        return clamp01(default_when_all_missing), 0.0, ["no positive dimension weights configured"]

    avail_w = 0.0
    weighted_sum = 0.0
    for name, w in weights.items():
        w = max(0.0, float(w))
        val = dimensions.get(name)
        if val is None:
            notes.append(f"dimension '{name}' unavailable")
            continue
        score = clamp01(val)
        weighted_sum += score * w
        avail_w += w

    if avail_w <= 0:
        notes.append("all dimensions unavailable; falling back to conservative score")
        return clamp01(default_when_all_missing), 0.0, notes

    satisfaction = clamp01(weighted_sum / avail_w)
    confidence = clamp01(avail_w / total_possible_w)
    return satisfaction, confidence, notes


def tolerance_gate(
    score: float,
    *,
    threshold: float,
    lethal: float = 0.0,
    gamma: float = 1.3,
    floor: float = 0.0,
) -> float:
    """Two-regime tolerance gate: 1.0 in safe zone (>= threshold), nonlinear drop below, floor at lethal."""
    x = clamp01(score)
    thr = clamp01(threshold)
    leth = clamp01(lethal)
    fl = clamp01(floor)
    g = max(0.1, float(gamma))
    if x >= thr:
        return 1.0
    if thr <= leth or x <= leth:
        return fl
    u = (x - leth) / (thr - leth)
    return clamp01(fl + (1.0 - fl) * (u**g))


def aggregate_dimensions(
    dimensions: dict[str, float | None],
    weights: dict[str, float],
    *,
    default_when_all_missing: float = 0.5,
    softmin_beta: float = 0.0,
    gates: dict[str, dict[str, Any]] | None = None,
) -> tuple[float, float, list[str]]:
    """Aggregate dimensions as series tolerance gate product * compensatory (weighted mean or Softmin)."""
    notes: list[str] = []
    total_possible_w = sum(max(0.0, float(w)) for w in weights.values())
    if total_possible_w <= 0:
        return clamp01(default_when_all_missing), 0.0, ["no positive dimension weights configured"]

    avail_w = 0.0
    avail_items: list[tuple[str, float, float]] = []
    for name, raw_w in weights.items():
        w = max(0.0, float(raw_w))
        val = dimensions.get(name)
        if val is None:
            notes.append(f"dimension '{name}' unavailable")
            continue
        if w <= 0.0:
            continue
        score = clamp01(val)
        avail_items.append((name, score, w))
        avail_w += w

    if avail_w <= 0.0 or not avail_items:
        notes.append("all dimensions unavailable; falling back to conservative score")
        return clamp01(default_when_all_missing), 0.0, notes

    beta = max(0.0, float(softmin_beta))
    if beta <= 1e-6:
        s_base = clamp01(sum((w / avail_w) * score for _, score, w in avail_items))
    else:
        m = min(score for _, score, _ in avail_items)
        exp_sum = sum((w / avail_w) * math.exp(-beta * (score - m)) for _, score, w in avail_items)
        s_base = clamp01(m - (1.0 / beta) * math.log(max(1e-12, exp_sum)))

    gate_prod = 1.0
    if gates:
        for name, score, _ in avail_items:
            g_cfg = gates.get(name)
            if not isinstance(g_cfg, dict):
                continue
            thr = float(g_cfg.get("threshold", 1.0))
            leth = float(g_cfg.get("lethal", 0.0))
            gamma = float(g_cfg.get("gamma", 1.3))
            fl = float(g_cfg.get("floor", 0.0))
            gi = tolerance_gate(score, threshold=thr, lethal=leth, gamma=gamma, floor=fl)
            gate_prod *= gi
            if gi < 0.999:
                notes.append(f"bottleneck gate '{name}': score={score:.2f} < {thr:.2f} (gate={gi:.2f})")

    satisfaction = clamp01(gate_prod * s_base)
    confidence = clamp01(avail_w / total_possible_w)
    return satisfaction, confidence, notes


def extract_number(mapping: dict[str, Any], key: str) -> float | None:
    if not isinstance(mapping, dict) or key not in mapping:
        return None
    val = mapping.get(key)
    if is_unavailable(val):
        return None
    if isinstance(val, (int, float)) and not isinstance(val, bool):
        f = float(val)
        return None if f != f else f
    return None
