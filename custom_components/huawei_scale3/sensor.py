"""Sensors for the Huawei Scale 3."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfMass
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ScaleConfigEntry
from .const import STATUS_OPTIONS
from .coordinator import HuaweiScale3Coordinator
from .entity import HuaweiScale3Entity
from .models import ScaleReading


@dataclass(frozen=True, kw_only=True)
class ScaleSensorDescription(SensorEntityDescription):
    """Describes a sensor fed from the latest reading."""

    value_fn: Callable[[ScaleReading], Any]


READING_SENSORS: tuple[ScaleSensorDescription, ...] = (
    ScaleSensorDescription(
        key="weight",
        translation_key="weight",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda r: r.weight_kg,
    ),
    ScaleSensorDescription(
        key="bmi",
        translation_key="bmi",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.bmi,
    ),
    ScaleSensorDescription(
        key="body_fat",
        translation_key="body_fat",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.body_fat_percent,
    ),
    ScaleSensorDescription(
        key="body_water",
        translation_key="body_water",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.water_percent,
    ),
    ScaleSensorDescription(
        key="skeletal_muscle",
        translation_key="skeletal_muscle",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.skeletal_muscle_percent,
    ),
    ScaleSensorDescription(
        key="bone_mass",
        translation_key="bone_mass",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda r: r.bone_mass_kg,
    ),
    ScaleSensorDescription(
        key="lean_body_mass",
        translation_key="lean_body_mass",
        device_class=SensorDeviceClass.WEIGHT,
        native_unit_of_measurement=UnitOfMass.KILOGRAMS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.lean_body_mass_kg,
    ),
    ScaleSensorDescription(
        key="protein",
        translation_key="protein",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda r: r.protein_percent,
    ),
    ScaleSensorDescription(
        key="bmr",
        translation_key="bmr",
        native_unit_of_measurement="kcal",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda r: r.bmr_kcal,
    ),
    ScaleSensorDescription(
        key="visceral_fat",
        translation_key="visceral_fat",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda r: r.visceral_fat_level,
    ),
    ScaleSensorDescription(
        key="heart_rate",
        translation_key="heart_rate",
        native_unit_of_measurement="bpm",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=0,
        value_fn=lambda r: r.heart_rate,
    ),
    ScaleSensorDescription(
        key="impedance",
        translation_key="impedance",
        native_unit_of_measurement="Ω",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.impedance_ohm,
    ),
    ScaleSensorDescription(
        key="measured_user",
        translation_key="measured_user",
        value_fn=lambda r: r.profile,
    ),
    ScaleSensorDescription(
        key="last_measurement",
        translation_key="last_measurement",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda r: r.timestamp,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        ReadingSensor(coordinator, description) for description in READING_SENSORS
    ]
    entities.append(StatusSensor(coordinator))
    async_add_entities(entities)


class ReadingSensor(HuaweiScale3Entity, SensorEntity):
    """A value from the most recent measurement."""

    entity_description: ScaleSensorDescription

    def __init__(
        self, coordinator: HuaweiScale3Coordinator, description: ScaleSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | str | datetime | None:
        reading = self.coordinator.reading
        return self.entity_description.value_fn(reading) if reading else None


class StatusSensor(HuaweiScale3Entity, SensorEntity):
    """What the integration is doing right now; handy for troubleshooting."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HuaweiScale3Coordinator) -> None:
        super().__init__(coordinator, "status")

    @property
    def native_value(self) -> str:
        return self.coordinator.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"last_error": self.coordinator.last_error}
