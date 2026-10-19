"""Aerobic decoupling on synthetic laps."""

from __future__ import annotations

from fastr.analysis import decoupling_from_laps, lap_pace_cv


def laps(paces_s_per_km: list[float], hrs: list[float], dist_m: float = 1000.0) -> list[dict]:
    return [
        {"distance_m": dist_m, "duration_s": p * dist_m / 1000.0, "avg_hr": hr}
        for p, hr in zip(paces_s_per_km, hrs)
    ]


def test_no_drift_is_zero():
    res = decoupling_from_laps(laps([360] * 10, [140] * 10))
    assert res is not None
    p1, h1, p2, h2, pct = res
    assert abs(pct) < 0.01
    assert p1 == p2 == 360 and h1 == h2 == 140


def test_rising_hr_at_constant_pace_is_positive():
    res = decoupling_from_laps(laps([360] * 10, [140, 141, 142, 143, 144, 152, 153, 154, 155, 156]))
    assert res is not None
    pct = res[4]
    assert 7.5 < pct < 8.5  # EF drops from 1/142 to 1/154


def test_slowing_pace_at_constant_hr_is_positive():
    res = decoupling_from_laps(laps([360] * 5 + [390] * 5, [145] * 10))
    assert res is not None
    assert 7.5 < res[4] < 8.5


def test_too_few_laps():
    assert decoupling_from_laps(laps([360] * 3, [140] * 3)) is None


def test_structured_session_has_high_cv():
    steady = lap_pace_cv(laps([360, 362, 358, 361, 359], [140] * 5))
    intervals = lap_pace_cv(laps([400, 280, 400, 280, 400, 280], [140] * 6))
    assert steady is not None and steady < 0.02
    assert intervals is not None and intervals > 0.15
