"""Home Assistant level tests: config flow, options flow, and a simulated weigh-in."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from habluetooth import BluetoothServiceInfoBleak
from homeassistant.components.bluetooth import BluetoothChange
from homeassistant.config_entries import SOURCE_BLUETOOTH, SOURCE_USER
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
)

from custom_components.huawei_scale3.const import (
    CONF_C1,
    CONF_C2,
    CONF_CAK,
    CONF_COOLDOWN,
    CONF_PROFILES,
    CONF_READ_HISTORY,
    DOMAIN,
    EVENT_MEASUREMENT,
)
from custom_components.huawei_scale3.session import Secrets

from .fake_scale import FakeScale, realtime_payload

ADDRESS = "38:1E:C7:66:08:D3"
SECRETS = Secrets(bytes(range(16)), bytes(range(16, 32)), bytes(range(32, 48)))
KEYS = {
    CONF_CAK: SECRETS.cak.hex().upper(),
    CONF_C1: SECRETS.c1.hex().upper(),
    CONF_C2: SECRETS.c2.hex().upper(),
}
#: What you'd type into the single "Authentication key" config-flow field.
AUTH_KEY = KEYS[CONF_CAK] + KEYS[CONF_C1] + KEYS[CONF_C2]
AUTH_KEY_INPUT = {"auth_key": AUTH_KEY}
ALEX = {
    "name": "Alex",
    "sex": "male",
    "birth_date": "1980-01-01",
    "height_cm": 178.0,
    "initial_weight_kg": 85.0,
}
BOB = {**ALEX, "name": "Bob", "height_cm": 182.0}

pytestmark = pytest.mark.usefixtures("auto_enable_custom_integrations", "mock_bluetooth")


def service_info(name: str = "HUAWEI Scale 3", address: str = ADDRESS, mfr=None):
    device = BLEDevice(address, name, {})
    adv = AdvertisementData(name, mfr or {}, {}, [], -60, -127, ())
    return BluetoothServiceInfoBleak(
        name, address, -60, mfr or {}, {}, [], "local", device, adv, True, 0.0, None
    )


# --------------------------------------------------------------- config flow


async def _finish_flow(hass: HomeAssistant, flow_id: str):
    result = await hass.config_entries.flow.async_configure(flow_id, AUTH_KEY_INPUT)
    assert result["step_id"] == "profile"
    return await hass.config_entries.flow.async_configure(
        flow_id,
        {
            "name": "Alex",
            "sex": "male",
            "birth_date": "1980-01-01",
            "height_cm": 178,
            "initial_weight_kg": 85,
        },
    )


async def test_bluetooth_discovery_creates_entry(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "bluetooth_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "secrets"

    with patch("custom_components.huawei_scale3.async_setup_entry", return_value=True):
        result = await _finish_flow(hass, result["flow_id"])

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Huawei Scale 3 08D3"
    assert result["result"].unique_id == ADDRESS
    assert result["data"] == {CONF_ADDRESS: ADDRESS, **KEYS}
    assert result["options"][CONF_PROFILES] == [ALEX | {"initial_weight_kg": 85.0}]
    assert result["options"][CONF_READ_HISTORY] is False


async def test_discovery_of_other_devices_is_ignored(hass: HomeAssistant) -> None:
    for name in ("HUAWEI Scale 3 Pro", "HUAWEI Band 8", "Mi Scale"):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info(name)
        )
        assert result["type"] is FlowResultType.ABORT
        assert result["reason"] == "not_supported"


async def test_discovery_aborts_if_already_configured(hass: HomeAssistant) -> None:
    MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={CONF_ADDRESS: ADDRESS}).add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_manual_address_flow_and_validation(hass: HomeAssistant) -> None:
    with patch(
        "custom_components.huawei_scale3.config_flow.async_discovered_service_info",
        return_value=[],
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": SOURCE_USER}
        )
        assert result["step_id"] == "user"

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: "not a mac"}
        )
        assert result["errors"] == {CONF_ADDRESS: "invalid_address"}

        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_ADDRESS: "38-1e-c7-66-08-d3"}
        )
    assert result["step_id"] == "secrets"

    # A key that is too short is rejected, whitespace/colons are tolerated.
    bad = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"auth_key": AUTH_KEY[:-4]}
    )
    assert bad["errors"] == {"auth_key": "invalid_key"}

    spaced = {"auth_key": ":".join(AUTH_KEY[i : i + 2] for i in range(0, 96, 2))}
    with patch("custom_components.huawei_scale3.async_setup_entry", return_value=True):
        ok = await hass.config_entries.flow.async_configure(result["flow_id"], spaced)
        ok = await hass.config_entries.flow.async_configure(
            ok["flow_id"],
            {
                "name": "Alex", "sex": "male", "birth_date": "1980-01-01",
                "height_cm": 178, "initial_weight_kg": 85,
            },
        )
    assert ok["type"] is FlowResultType.CREATE_ENTRY
    assert ok["data"][CONF_ADDRESS] == ADDRESS
    assert ok["data"][CONF_CAK] == KEYS[CONF_CAK]


async def test_profile_validation(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=service_info()
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], AUTH_KEY_INPUT)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            "name": "  ", "sex": "male", "birth_date": "2999-01-01",
            "height_cm": 178, "initial_weight_kg": 85,
        },
    )
    assert result["errors"] == {"name": "name_required", "birth_date": "invalid_birth_date"}


# ---------------------------------------------------------------- runtime


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="Huawei Scale 3 08D3",
        data={CONF_ADDRESS: ADDRESS, **KEYS},
        options={
            CONF_PROFILES: [ALEX, BOB],
            CONF_READ_HISTORY: False,
            CONF_COOLDOWN: 60,
        },
    )
    entry.add_to_hass(hass)
    return entry


class Harness:
    """Patches Bluetooth so a test can 'wake' the scale on demand."""

    def __init__(self, hass: HomeAssistant, entry: MockConfigEntry, scale: FakeScale) -> None:
        self.hass, self.entry, self.scale = hass, entry, scale
        self.callbacks: list = []
        self.connect = AsyncMock(return_value=scale)

    def __enter__(self):
        device = BLEDevice(ADDRESS, "HUAWEI Scale 3", {})
        self._patches = [
            patch(
                "homeassistant.components.bluetooth.async_register_callback",
                side_effect=lambda hass, cb, matcher, mode: (
                    self.callbacks.append((cb, matcher, mode)) or MagicMock()
                ),
            ),
            patch(
                "homeassistant.components.bluetooth.async_ble_device_from_address",
                return_value=device,
            ),
            patch(
                "custom_components.huawei_scale3.coordinator.establish_connection",
                self.connect,
            ),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()

    async def setup(self) -> None:
        assert await self.hass.config_entries.async_setup(self.entry.entry_id)
        await self.hass.async_block_till_done()

    async def wake_scale(self) -> None:
        cb, _, _ = self.callbacks[0]
        cb(service_info(), BluetoothChange.ADVERTISEMENT)
        await self.hass.async_block_till_done(wait_background_tasks=True)


def state(hass: HomeAssistant, domain: str, key: str):
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(domain, DOMAIN, f"{ADDRESS}_{key}")
    assert entity_id, f"no entity for {key}"
    return hass.states.get(entity_id)


async def test_setup_registers_an_active_connectable_listener(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    from homeassistant.components.bluetooth import BluetoothScanningMode

    with Harness(hass, entry, FakeScale(ADDRESS, SECRETS)) as h:
        await h.setup()
        (_, matcher, mode), = h.callbacks
        assert matcher["address"] == ADDRESS and matcher["connectable"] is True
        assert mode is BluetoothScanningMode.ACTIVE
        assert state(hass, "sensor", "weight").state == "unknown"
        assert state(hass, "select", "active_user").state == "Alex"
        assert state(hass, "sensor", "status").state == "idle"


async def test_weigh_in_updates_entities_and_fires_event(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    scale = FakeScale(ADDRESS, SECRETS)
    events = async_capture_events(hass, EVENT_MEASUREMENT)
    with Harness(hass, entry, scale) as h:
        await h.setup()
        await h.wake_scale()

        assert scale.authenticated and scale.disconnected
        assert float(state(hass, "sensor", "weight").state) == pytest.approx(86.25)
        assert float(state(hass, "sensor", "heart_rate").state) == 61
        assert 5 < float(state(hass, "sensor", "body_fat").state) < 50
        assert float(state(hass, "sensor", "bmi").state) == pytest.approx(27.2, abs=0.1)
        assert state(hass, "sensor", "measured_user").state == "Alex"
        assert state(hass, "sensor", "status").state == "idle"
        assert state(hass, "sensor", "last_measurement").state != "unknown"

        assert len(events) == 1
        assert events[0].data["user"] == "Alex"
        assert events[0].data["history"] is False
        assert events[0].data["weight_kg"] == pytest.approx(86.25)
        assert "profile" not in events[0].data

        # Alex's profile was what the scale was told about.
        assert scale.user_info[63] == entry.runtime_data.active_profile.age_on(
            __import__("datetime").date.today()
        )
        assert int.from_bytes(scale.user_info[64:66], "little") == 178

        # Cool-down: a second advertisement right away does not reconnect.
        await h.wake_scale()
        assert h.connect.await_count == 1


async def test_weight_unit_defaults_to_metric(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    scale = FakeScale(ADDRESS, SECRETS)
    with Harness(hass, entry, scale) as h:
        await h.setup()
        await h.wake_scale()
    assert scale.weight_unit == bytes([1])  # kg


async def test_weight_unit_follows_ha_unit_system(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    hass.config.units = US_CUSTOMARY_SYSTEM
    scale = FakeScale(ADDRESS, SECRETS)
    with Harness(hass, entry, scale) as h:
        await h.setup()
        await h.wake_scale()
    assert scale.weight_unit == bytes([2])  # lb


async def test_switching_user_changes_profile_pushed_to_the_scale(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    scale = FakeScale(ADDRESS, SECRETS)
    with Harness(hass, entry, scale) as h:
        await h.setup()
        select_id = er.async_get(hass).async_get_entity_id(
            "select", DOMAIN, f"{ADDRESS}_active_user"
        )
        await hass.services.async_call(
            "select", "select_option", {"entity_id": select_id, "option": "Bob"}, blocking=True
        )
        assert hass.states.get(select_id).state == "Bob"

        await h.wake_scale()
        assert int.from_bytes(scale.user_info[64:66], "little") == 182
        assert state(hass, "sensor", "measured_user").state == "Bob"


async def test_failed_session_backs_off_and_reports_error(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    scale = FakeScale(ADDRESS, SECRETS, status_ready_after=10_000)  # never completes
    with Harness(hass, entry, scale) as h:
        await h.setup()
        await h.wake_scale()
        assert state(hass, "sensor", "status").state == "error"
        assert "timed out" in state(hass, "sensor", "status").attributes["last_error"]
        assert state(hass, "sensor", "weight").state == "unknown"
        assert scale.disconnected

        await h.wake_scale()  # inside the retry back-off: no new attempt
        assert h.connect.await_count == 1


async def test_repeated_auth_failures_start_reauth(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    wrong = Secrets(bytes(16), SECRETS.c1, SECRETS.c2)
    scale = FakeScale(ADDRESS, wrong)  # the scale knows a different CAK
    with Harness(hass, entry, scale) as h:
        await h.setup()
        coordinator = entry.runtime_data
        for _ in range(3):
            coordinator._next_allowed = 0
            await h.wake_scale()
        assert state(hass, "sensor", "status").state == "auth_error"

    flows = [f for f in hass.config_entries.flow.async_progress() if f["context"]["source"] == "reauth"]
    assert len(flows) == 1


async def test_reauth_updates_the_keys(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    new_cak, new_c1, new_c2 = "11" * 16, "22" * 16, "33" * 16
    with Harness(hass, entry, FakeScale(ADDRESS, SECRETS)):
        result = await entry.start_reauth_flow(hass)
        assert result["step_id"] == "reauth_confirm"
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"auth_key": new_cak + new_c1 + new_c2}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    assert entry.data[CONF_CAK] == new_cak and entry.data[CONF_C2] == new_c2
    assert entry.data[CONF_ADDRESS] == ADDRESS


async def test_history_records_become_events_without_duplicates(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_READ_HISTORY: True}
    )
    live = realtime_payload()
    older = realtime_payload(weight_kg=85.0, flags=bytes([1, 1]))
    oldest = realtime_payload(weight_kg=84.0, flags=bytes([1, 0]))
    # The scale's own copy of the live reading is in its history as well.
    same_as_live = realtime_payload(flags=bytes([1, 1]))
    scale = FakeScale(ADDRESS, SECRETS, realtime=live, history=(same_as_live, older, oldest))
    # History records need distinct timestamps, or they dedupe against each other.
    older = bytearray(older); older[9] = 0x10
    scale.history[1] = bytes(older)
    events = async_capture_events(hass, EVENT_MEASUREMENT)
    with Harness(hass, entry, scale) as h:
        await h.setup()
        await h.wake_scale()

    live_events = [e for e in events if not e.data["history"]]
    history_events = [e for e in events if e.data["history"]]
    assert len(live_events) == 1
    assert [round(e.data["weight_kg"]) for e in history_events] == [85, 84]
    # The live reading's entities were not overwritten by history.
    assert float(state(hass, "sensor", "weight").state) == pytest.approx(86.25)


async def test_state_survives_restart(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    with Harness(hass, entry, FakeScale(ADDRESS, SECRETS)) as h:
        await h.setup()
        await h.wake_scale()
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert float(state(hass, "sensor", "weight").state) == pytest.approx(86.25)


async def test_diagnostics_redact_secrets(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    from custom_components.huawei_scale3.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    with Harness(hass, entry, FakeScale(ADDRESS, SECRETS)) as h:
        await h.setup()
        await h.wake_scale()
        diag = await async_get_config_entry_diagnostics(hass, entry)
    blob = repr(diag)
    for secret in (*KEYS.values(), ADDRESS, "1980-01-01", "Alex"):
        assert secret not in blob


# ------------------------------------------------------------ options flow


async def test_options_add_and_remove_profile(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    with Harness(hass, entry, FakeScale(ADDRESS, SECRETS)) as h:
        await h.setup()

        result = await hass.config_entries.options.async_init(entry.entry_id)
        assert result["type"] is FlowResultType.MENU
        assert set(result["menu_options"]) == {"settings", "add_profile", "remove_profile"}

        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "add_profile"}
        )
        dup = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"name": "alex", "sex": "female", "birth_date": "1990-02-02",
             "height_cm": 165, "initial_weight_kg": 60},
        )
        assert dup["errors"] == {"name": "name_exists"}
        done = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {"name": "Cleo", "sex": "female", "birth_date": "1990-02-02",
             "height_cm": 165, "initial_weight_kg": 60},
        )
        assert done["type"] is FlowResultType.CREATE_ENTRY
        await hass.async_block_till_done()
        assert [p["name"] for p in entry.options[CONF_PROFILES]] == ["Alex", "Bob", "Cleo"]
        assert entry.options[CONF_PROFILES][2]["sex"] == "female"

        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "remove_profile"}
        )
        await hass.config_entries.options.async_configure(result["flow_id"], {"name": "Bob"})
        await hass.async_block_till_done()
        assert [p["name"] for p in entry.options[CONF_PROFILES]] == ["Alex", "Cleo"]


async def test_options_cannot_remove_last_profile(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(entry, options={**entry.options, CONF_PROFILES: [ALEX]})
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "remove_profile" not in result["menu_options"]


async def test_options_settings(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    with Harness(hass, entry, FakeScale(ADDRESS, SECRETS)) as h:
        await h.setup()
        result = await hass.config_entries.options.async_init(entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {"next_step_id": "settings"}
        )
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_READ_HISTORY: True, CONF_COOLDOWN: 120}
        )
        await hass.async_block_till_done()
    assert entry.options[CONF_READ_HISTORY] is True and entry.options[CONF_COOLDOWN] == 120
    assert len(entry.options[CONF_PROFILES]) == 2  # untouched
