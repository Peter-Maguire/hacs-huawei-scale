"""Huawei Scale 3 (Bluetooth) integration."""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .coordinator import HuaweiScale3Coordinator

type ScaleConfigEntry = ConfigEntry[HuaweiScale3Coordinator]

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SELECT]


async def async_setup_entry(hass: HomeAssistant, entry: ScaleConfigEntry) -> bool:
    """Set up a scale from a config entry."""
    coordinator = HuaweiScale3Coordinator(hass, entry)
    await coordinator.async_load()
    entry.runtime_data = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # Registered last so no session can start before the entities listen.
    entry.async_on_unload(coordinator.async_start())
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ScaleConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.async_flush()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
