"""Choose which person the scale is currently set up for."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import ScaleConfigEntry
from .coordinator import HuaweiScale3Coordinator
from .entity import HuaweiScale3Entity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ScaleConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    async_add_entities([ActiveUserSelect(entry.runtime_data)])


class ActiveUserSelect(HuaweiScale3Entity, SelectEntity):
    """The profile pushed to the scale at the next weigh-in.

    The scale needs age, height and sex *before* it measures, so whoever steps
    on next is assumed to be the selected person. Measurements are attributed
    to them and are also fired as ``huawei_scale3_measurement`` events.
    """

    def __init__(self, coordinator: HuaweiScale3Coordinator) -> None:
        super().__init__(coordinator, "active_user")

    @property
    def options(self) -> list[str]:
        return [profile.name for profile in self.coordinator.profiles]

    @property
    def current_option(self) -> str | None:
        profile = self.coordinator.active_profile
        return profile.name if profile else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_active_profile(option)
