"""Connects to the scale when it wakes up and publishes what it measures.

The scale only advertises while someone is standing on it. We register an
active Bluetooth callback for its address; the first advertisement starts a
session (connect -> authenticate -> sync profile -> measure), after which we
disconnect and ignore the scale for a cool-down period.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import (
    BluetoothCallbackMatcher,
    BluetoothChange,
    BluetoothScanningMode,
    BluetoothServiceInfoBleak,
)
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import METRIC_SYSTEM

from . import protocol as p
from .const import (
    AUTH_FAILURES_BEFORE_REAUTH,
    AUTH_RETRY_DELAY,
    CONF_BIND_UNBOUND,
    CONF_C1,
    CONF_C2,
    CONF_CAK,
    CONF_COOLDOWN,
    CONF_PROFILES,
    CONF_READ_HISTORY,
    DEFAULT_BIND_UNBOUND,
    DEFAULT_C1,
    DEFAULT_C2,
    DEFAULT_CAK,
    DEFAULT_COOLDOWN,
    DEFAULT_READ_HISTORY,
    DOMAIN,
    EVENT_MEASUREMENT,
    RETRY_DELAY,
    SESSION_TIMEOUT,
)
from .models import Profile, ScaleReading, build_reading
from .session import (
    AuthenticationError,
    Scale3Session,
    ScaleUser,
    Secrets,
    SessionError,
)

if TYPE_CHECKING:
    from . import ScaleConfigEntry

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
MAX_HISTORY_KEYS = 256


class HuaweiScale3Coordinator:
    """Event-driven coordinator (the scale pushes, nothing is polled)."""

    def __init__(self, hass: HomeAssistant, entry: ScaleConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.address: str = entry.data[CONF_ADDRESS].upper()
        self.signal = f"{DOMAIN}_update_{entry.entry_id}"

        self.reading: ScaleReading | None = None
        self.status = "idle"
        self.last_error: str | None = None
        self.sw_version: str | None = None
        self.product_id: str | None = None
        self.active_profile_name: str | None = None

        self._last_weights: dict[str, float] = {}
        self._history_keys: list[str] = []
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self._task: asyncio.Task[None] | None = None
        self._next_allowed = 0.0
        self._auth_failures = 0
        self._failures = 0  # consecutive failed sessions
        self._reauth_started = False
        self._last_live_key: str | None = None

    # ---------------------------------------------------------------- config

    @property
    def profiles(self) -> list[Profile]:
        return [Profile.from_dict(raw) for raw in self.entry.options.get(CONF_PROFILES, [])]

    @property
    def active_profile(self) -> Profile | None:
        profiles = self.profiles
        for profile in profiles:
            if profile.name == self.active_profile_name:
                return profile
        return profiles[0] if profiles else None

    @property
    def secrets(self) -> Secrets:
        data = self.entry.data
        return Secrets.from_hex(
            data.get(CONF_CAK, DEFAULT_CAK),
            data.get(CONF_C1, DEFAULT_C1),
            data.get(CONF_C2, DEFAULT_C2),
        )

    async def async_set_active_profile(self, name: str) -> None:
        self.active_profile_name = name
        self._save()
        async_dispatcher_send(self.hass, self.signal)

    # ------------------------------------------------------------- lifecycle

    async def async_load(self) -> None:
        data = await self._store.async_load() or {}
        self.active_profile_name = data.get("active_profile")
        self._last_weights = dict(data.get("last_weights", {}))
        self._history_keys = list(data.get("history_keys", []))
        self.sw_version = data.get("sw_version")
        self.product_id = data.get("product_id")
        if raw := data.get("reading"):
            try:
                self.reading = ScaleReading.from_dict(raw)
            except (KeyError, TypeError, ValueError):
                _LOGGER.debug("Ignoring unreadable stored reading")

    @callback
    def async_start(self) -> CALLBACK_TYPE:
        """Start listening for the scale; returns the stop callback."""
        cancel = bluetooth.async_register_callback(
            self.hass,
            self._async_on_advertisement,
            BluetoothCallbackMatcher(address=self.address, connectable=True),
            BluetoothScanningMode.ACTIVE,
        )

        @callback
        def _stop() -> None:
            cancel()
            if self._task and not self._task.done():
                self._task.cancel()

        return _stop

    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 2)

    async def async_flush(self) -> None:
        """Write pending state now (a delayed save is lost if the entry reloads)."""
        await self._store.async_save(self._data_to_save())

    def _data_to_save(self) -> dict[str, Any]:
        return {
            "active_profile": self.active_profile_name,
            "last_weights": self._last_weights,
            "history_keys": self._history_keys[-MAX_HISTORY_KEYS:],
            "sw_version": self.sw_version,
            "product_id": self.product_id,
            "reading": self.reading.to_dict() if self.reading else None,
        }

    def _set_status(self, status: str, error: str | None = None) -> None:
        self.status = status
        if status == "idle":
            self.last_error = None
        elif error is not None:
            self.last_error = error
        async_dispatcher_send(self.hass, self.signal)

    # ----------------------------------------------------------- BLE session

    @callback
    def _async_on_advertisement(
        self, service_info: BluetoothServiceInfoBleak, change: BluetoothChange
    ) -> None:
        if self._task and not self._task.done():
            return
        if time.monotonic() < self._next_allowed:
            return
        self._task = self.entry.async_create_background_task(
            self.hass,
            self._async_run_session(),
            name=f"{DOMAIN} session {self.address}",
        )

    def _backoff(self, seconds: float) -> None:
        self._next_allowed = time.monotonic() + seconds

    async def _async_run_session(self) -> None:
        profile = self.active_profile
        if profile is None:
            _LOGGER.warning("No user profile configured for %s", self.entry.title)
            self._backoff(RETRY_DELAY)
            return

        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            _LOGGER.debug("%s is not currently connectable", self.address)
            self._backoff(5)
            return

        today = dt_util.now().date()
        scale_user = ScaleUser(
            user_id=profile.user_id,
            male=profile.male,
            age=max(0, profile.age_on(today)),
            height_cm=profile.height_cm,
            weight_kg=self._last_weights.get(profile.name, profile.initial_weight_kg),
        )
        options = self.entry.options
        client: BleakClient | None = None
        session: Scale3Session | None = None
        self._set_status("connecting")
        try:
            async with asyncio.timeout(SESSION_TIMEOUT):
                client = await establish_connection(
                    BleakClientWithServiceCache,
                    ble_device,
                    self.entry.title,
                    disconnected_callback=lambda _c: session and session.notify_disconnected(),
                    ble_device_callback=lambda: bluetooth.async_ble_device_from_address(
                        self.hass, self.address, connectable=True
                    )
                    or ble_device,
                )
                session = Scale3Session(
                    client,
                    self.address,
                    self.secrets,
                    scale_user,
                    read_history=options.get(CONF_READ_HISTORY, DEFAULT_READ_HISTORY),
                    bind_if_unbound=options.get(CONF_BIND_UNBOUND, DEFAULT_BIND_UNBOUND),
                    metric=self.hass.config.units is METRIC_SYSTEM,
                    now=dt_util.now,
                    on_state=self._set_status,
                    on_measurement=lambda m, live: self._on_measurement(m, live, profile),
                    on_info=self._on_info,
                )
                await session.run()
        except AuthenticationError as err:
            await self._async_handle_auth_failure(err)
            return
        except (SessionError, BleakError, TimeoutError, OSError) as err:
            # A failed attempt is routine (stepping off early, out of range), so
            # only the first failure in a row is a warning.
            self._failures += 1
            _LOGGER.log(
                logging.WARNING if self._failures == 1 else logging.DEBUG,
                "Could not read the scale (%s): %s", self.address, err or type(err).__name__,
            )
            self._set_status("error", str(err) or type(err).__name__)
            self._backoff(RETRY_DELAY)
            return
        finally:
            if client is not None:
                try:
                    await client.disconnect()
                except (BleakError, OSError, TimeoutError):
                    pass

        self._auth_failures = 0
        self._failures = 0
        self._set_status("idle")
        self._backoff(options.get(CONF_COOLDOWN, DEFAULT_COOLDOWN))

    async def _async_handle_auth_failure(self, err: AuthenticationError) -> None:
        self._auth_failures += 1
        _LOGGER.warning(
            "The scale rejected the authentication (%d/%d): %s",
            self._auth_failures, AUTH_FAILURES_BEFORE_REAUTH, err,
        )
        self._set_status("auth_error", str(err))
        self._backoff(AUTH_RETRY_DELAY)
        if self._auth_failures >= AUTH_FAILURES_BEFORE_REAUTH and not self._reauth_started:
            self._reauth_started = True
            self.entry.async_start_reauth(self.hass)

    def reset_auth_state(self) -> None:
        self._auth_failures = 0
        self._reauth_started = False

    # ------------------------------------------------------------ data in

    def _on_info(self, key: str, value: str) -> None:
        if key == "sw_version" and value != self.sw_version:
            self.sw_version = value
            registry = dr.async_get(self.hass)
            for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
                if (DOMAIN, self.address) in device.identifiers:
                    registry.async_update_device(device.id, sw_version=value)
            self._save()
        elif key == "product_id" and value != self.product_id:
            self.product_id = value
            self._save()

    def _on_measurement(
        self, measurement: p.WeightMeasurement, live: bool, profile: Profile
    ) -> None:
        key = measurement.dedupe_key
        if live:
            if key == self._last_live_key:
                _LOGGER.debug("Ignoring repeat of the previous measurement")
                return
            self._last_live_key = key
            if measurement.timestamp:
                self._remember_history_key(key)  # so the same record is not replayed
            timestamp = dt_util.utcnow()
        else:
            if key in self._history_keys:
                return
            self._remember_history_key(key)
            if measurement.timestamp is None:
                return
            timestamp = dt_util.as_utc(
                measurement.timestamp.replace(tzinfo=dt_util.get_default_time_zone())
            )

        reading = build_reading(
            measurement,
            profile,
            timestamp=timestamp,
            today=dt_util.now().date(),
            history=not live,
        )
        self.hass.bus.async_fire(EVENT_MEASUREMENT, self._event_data(reading))
        if live:
            self.reading = reading
            self._last_weights[profile.name] = reading.weight_kg
            self._save()
            async_dispatcher_send(self.hass, self.signal)
            _LOGGER.debug("New measurement for %s: %.2f kg", profile.name, reading.weight_kg)

    def _remember_history_key(self, key: str) -> None:
        if key not in self._history_keys:
            self._history_keys.append(key)
            del self._history_keys[:-MAX_HISTORY_KEYS]
            self._save()

    def _event_data(self, reading: ScaleReading) -> dict[str, Any]:
        data = reading.to_dict()
        data["address"] = self.address
        data["entry_id"] = self.entry.entry_id
        data["user"] = data.pop("profile")
        return data
