"""Profiles and readings. No Home Assistant imports (except nothing at all)."""
from __future__ import annotations

import zlib
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from typing import Any

from . import body_composition
from .protocol import WeightMeasurement


@dataclass(frozen=True)
class Profile:
    """A person who weighs themselves on the scale."""

    name: str
    male: bool
    birth_date: date
    height_cm: float
    initial_weight_kg: float

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Profile":
        return cls(
            name=raw["name"],
            male=raw["sex"] == "male",
            birth_date=date.fromisoformat(raw["birth_date"]),
            height_cm=float(raw["height_cm"]),
            initial_weight_kg=float(raw.get("initial_weight_kg") or 70.0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "sex": "male" if self.male else "female",
            "birth_date": self.birth_date.isoformat(),
            "height_cm": self.height_cm,
            "initial_weight_kg": self.initial_weight_kg,
        }

    @property
    def user_id(self) -> int:
        """Stable positive 31-bit id sent to the scale as ``u:XXXXXXXX``."""
        return zlib.crc32(self.name.encode("utf-8")) & 0x7FFFFFFF

    def age_on(self, today: date) -> int:
        years = today.year - self.birth_date.year
        if (today.month, today.day) < (self.birth_date.month, self.birth_date.day):
            years -= 1
        return years


@dataclass(frozen=True)
class ScaleReading:
    """One processed measurement, as exposed to Home Assistant."""

    timestamp: datetime  # timezone-aware
    profile: str
    weight_kg: float
    history: bool = False
    #: "model" = computed from impedance, "scale" = fat % as reported by the
    #: scale only, "none" = weight only.
    composition: str = "none"
    heart_rate: int | None = None
    impedance_ohm: float | None = None
    bmi: float | None = None
    body_fat_percent: float | None = None
    water_percent: float | None = None
    skeletal_muscle_percent: float | None = None
    bone_mass_kg: float | None = None
    lean_body_mass_kg: float | None = None
    protein_percent: float | None = None
    bmr_kcal: float | None = None
    visceral_fat_level: float | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ScaleReading":
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in raw.items() if k in known}
        values["timestamp"] = datetime.fromisoformat(values["timestamp"])
        return cls(**values)


def build_reading(
    measurement: WeightMeasurement,
    profile: Profile,
    *,
    timestamp: datetime,
    today: date,
    history: bool = False,
) -> ScaleReading:
    """Turn a raw scale measurement into a reading for ``profile``."""
    weight = measurement.weight_kg
    height_m = profile.height_cm / 100.0
    bmi = weight / (height_m * height_m) if height_m > 0 else None

    common = dict(
        timestamp=timestamp,
        profile=profile.name,
        weight_kg=weight,
        history=history,
        heart_rate=measurement.heart_rate,
        impedance_ohm=measurement.impedance_ohm,
        bmi=bmi,
    )

    impedance = measurement.impedance_ohm
    comp = (
        body_composition.calculate(
            height_cm=profile.height_cm,
            weight_kg=weight,
            age_years=profile.age_on(today),
            male=profile.male,
            impedance_ohm=impedance,
        )
        if impedance
        else None
    )
    if comp is not None:
        return ScaleReading(
            composition="model",
            body_fat_percent=comp.body_fat_percent,
            water_percent=comp.water_percent,
            skeletal_muscle_percent=comp.skeletal_muscle_percent,
            bone_mass_kg=comp.bone_mineral_kg,
            lean_body_mass_kg=comp.lean_body_mass_kg,
            protein_percent=comp.protein_percent,
            bmr_kcal=comp.bmr_kcal,
            visceral_fat_level=comp.visceral_fat_level,
            **common,
        )

    # No model result (child profile, implausible input, no impedance):
    # fall back to the scale's own body-fat figure when it looks sane.
    if 0.0 < measurement.fat_percent < 80.0:
        return ScaleReading(
            composition="scale", body_fat_percent=measurement.fat_percent, **common
        )
    return ScaleReading(composition="none", **common)
