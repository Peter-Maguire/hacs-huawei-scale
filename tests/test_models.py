"""Profile / reading logic (no Home Assistant needed)."""
from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from custom_components.huawei_scale3 import protocol as p
from custom_components.huawei_scale3.models import Profile, ScaleReading, build_reading

from .fake_scale import realtime_payload

PROFILE = Profile("Alex", True, date(1980, 1, 1), 178.0, 80.0)
NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)


def _measure(**kw):
    return p.parse_realtime_measurement(realtime_payload(**kw))


def test_age_handles_birthdays():
    assert PROFILE.age_on(date(2026, 1, 1)) == 46
    assert PROFILE.age_on(date(2025, 12, 31)) == 45


def test_user_id_is_stable_and_positive():
    assert PROFILE.user_id == Profile("Alex", False, date(1990, 5, 5), 160, 50).user_id
    assert 0 <= PROFILE.user_id < 2**31


def test_profile_roundtrip():
    assert Profile.from_dict(PROFILE.to_dict()) == PROFILE


def test_full_composition_when_impedance_present():
    r = build_reading(_measure(), PROFILE, timestamp=NOW, today=date(2026, 8, 9))
    assert r.composition == "model"
    assert r.weight_kg == pytest.approx(86.25)
    assert r.impedance_ohm == pytest.approx(362.5)
    assert r.bmi == pytest.approx(86.25 / 1.78**2)
    assert 5 < r.body_fat_percent < 50
    assert r.bmr_kcal > 1000 and r.visceral_fat_level >= 1
    assert r.heart_rate == 61


def test_falls_back_to_scale_fat_without_impedance():
    r = build_reading(_measure(impedance_raw=0), PROFILE, timestamp=NOW, today=date(2026, 8, 9))
    assert r.composition == "scale"
    assert r.body_fat_percent == pytest.approx(30.1)
    assert r.water_percent is None and r.bmr_kcal is None


def test_minors_get_no_model_composition():
    kid = Profile("Kid", True, date(2015, 1, 1), 150.0, 40.0)
    r = build_reading(_measure(), kid, timestamp=NOW, today=date(2026, 8, 9))
    assert r.composition == "scale"
    assert r.skeletal_muscle_percent is None


def test_weight_only_when_scale_fat_is_junk():
    r = build_reading(
        _measure(impedance_raw=0, fat=0.0), PROFILE, timestamp=NOW, today=date(2026, 8, 9)
    )
    assert r.composition == "none" and r.body_fat_percent is None


def test_reading_serialises():
    r = build_reading(_measure(), PROFILE, timestamp=NOW, today=date(2026, 8, 9))
    assert ScaleReading.from_dict(r.to_dict()) == r
