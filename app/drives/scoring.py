"""Shared scoring utilities for independent drive evaluators."""

from __future__ import annotations

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
