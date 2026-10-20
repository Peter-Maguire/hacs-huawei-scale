"""Diagnostics for the Huawei Scale 3 (secrets and personal data redacted)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant

from . import ScaleConfigEntry
from .const import CONF_BIRTH_DATE, CONF_C1, CONF_C2, CONF_CAK, CONF_NAME

TO_REDACT = {CONF_CAK, CONF_C1, CONF_C2, CONF_ADDRESS, CONF_BIRTH_DATE, CONF_NAME, "profile"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ScaleConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    reading = coordinator.reading.to_dict() if coordinator.reading else None
    return {
        "entry": async_redact_data(
            {"data": dict(entry.data), "options": dict(entry.options)}, TO_REDACT
        ),
        "status": coordinator.status,
        "last_error": coordinator.last_error,
        "sw_version": coordinator.sw_version,
        "product_id": coordinator.product_id,
        "last_reading": async_redact_data(reading, TO_REDACT) if reading else None,
    }
