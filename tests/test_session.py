"""Session flow against a simulated scale (see fake_scale.py)."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from custom_components.huawei_scale3 import session as s

from .fake_scale import FakeScale, realtime_payload

ADDRESS = "38:1E:C7:66:08:D3"
SECRETS = s.Secrets(bytes(range(16)), bytes(range(16, 32)), bytes(range(32, 48)))
USER = s.ScaleUser(user_id=7, male=True, age=46, height_cm=178, weight_kg=91.4)


async def go(
    scale: FakeScale, *, secrets=SECRETS, read_history=False, user=USER, metric=True, before=None,
    bind_if_unbound=False,
):
    """Run a session to completion. Raises whatever the session raises."""
    out = SimpleNamespace(states=[], measurements=[], info={}, session=None)

    async def main():
        out.session = s.Scale3Session(
            scale,
            ADDRESS,
            secrets,
            user,
            read_history=read_history,
            bind_if_unbound=bind_if_unbound,
            metric=metric,
            now=lambda: datetime(2026, 8, 10, 7, 5, 9),
            on_state=out.states.append,
            on_measurement=lambda m, live: out.measurements.append((m, live)),
            on_info=out.info.__setitem__,
        )
        if before:
            before(out.session)
        await out.session.run()

    await main()
    return out


async def test_happy_path_publishes_one_live_measurement():
    scale = FakeScale(ADDRESS, SECRETS)
    out = await go(scale)

    assert scale.authenticated
    assert out.states == ["authenticating", "syncing", "measuring"]
    assert len(out.measurements) == 1
    m, live = out.measurements[0]
    assert live is True
    assert m.weight_kg == pytest.approx(86.25)
    assert m.impedance_ohm == pytest.approx(362.5)
    assert m.heart_rate == 61
    assert out.session.measurement_published


async def test_profile_and_clock_are_pushed_to_the_scale():
    scale = FakeScale(ADDRESS, SECRETS)
    await go(scale)

    ui = scale.user_info
    assert ui is not None and len(ui) == 69
    assert ui[:30].rstrip(b"\x00") == b"HUID-FROM-SCALE"  # taken from manager info
    assert ui[30:62].rstrip(b"\x00") == b"u:00000007"
    assert ui[62] == 1  # Scale 3: male = 1
    assert ui[63] == 46
    assert int.from_bytes(ui[64:66], "little") == 178
    assert int.from_bytes(ui[66:68], "little") == 9140
    assert scale.clock == bytes([0xEA, 0x07, 8, 10, 7, 5, 9, 1])


async def test_female_is_encoded_as_zero():
    scale = FakeScale(ADDRESS, SECRETS)
    await go(scale, user=s.ScaleUser(1, False, 40, 165, 65))
    assert scale.user_info[62] == 0


async def test_identity_info_is_reported():
    scale = FakeScale(ADDRESS, SECRETS)
    out = await go(scale)
    assert out.info.get("sw_version") == "2.0.0.70"
    assert out.info.get("product_id") == "M00D"


async def test_weight_unit_write_follows_the_metric_flag():
    metric_scale = FakeScale(ADDRESS, SECRETS)
    await go(metric_scale, metric=True)
    assert metric_scale.weight_unit == bytes([1])

    imperial_scale = FakeScale(ADDRESS, SECRETS)
    await go(imperial_scale, metric=False)
    assert imperial_scale.weight_unit == bytes([2])


async def test_plain_work_key_ack_also_works():
    await go(FakeScale(ADDRESS, SECRETS, work_key_reply_encrypted=False))


async def test_weight_without_impedance_is_published_when_scale_reports_ready():
    scale = FakeScale(
        ADDRESS, SECRETS, realtime=realtime_payload(impedance_raw=0), status_ready_after=2
    )
    out = await go(scale)
    assert len(out.measurements) == 1
    assert out.measurements[0][0].impedance_ohm is None
    assert out.measurements[0][0].weight_kg == pytest.approx(86.25)


async def test_wrong_cak_is_an_authentication_error():
    bad = s.Secrets(bytes(16), SECRETS.c1, SECRETS.c2)
    with pytest.raises(s.AuthenticationError):
        await go(FakeScale(ADDRESS, SECRETS), secrets=bad)


async def test_silent_scale_after_token_is_an_authentication_error():
    with pytest.raises(s.AuthenticationError):
        await go(FakeScale(ADDRESS, SECRETS, answer_auth=False))


async def test_rejected_work_key_is_an_authentication_error():
    with pytest.raises(s.AuthenticationError, match="work key"):
        await go(FakeScale(ADDRESS, SECRETS, reject_work_key=True))


async def test_disconnect_mid_measurement_raises_session_error():
    scale = FakeScale(ADDRESS, SECRETS, status_ready_after=3)
    with pytest.raises(s.SessionError, match="disconnected"):
        await go(scale, before=lambda sess: setattr(scale, "disconnect_on_poll", sess.notify_disconnected))


async def test_device_without_hagrid_layout_is_rejected():
    with pytest.raises(s.SessionError, match="Hagrid"):
        await go(FakeScale(ADDRESS, SECRETS, with_gatt=False))


async def test_no_measurement_before_timeout_raises():
    scale = FakeScale(ADDRESS, SECRETS, status_ready_after=10_000)
    with pytest.raises(s.SessionError, match="timed out"):
        await go(scale)


async def test_history_is_drained_until_complete_flag():
    def rec(weight, complete):
        return realtime_payload(weight_kg=weight, flags=bytes([1, complete]))

    scale = FakeScale(ADDRESS, SECRETS, history=(rec(80.0, 1), rec(81.0, 1), rec(82.0, 0)))
    out = await go(scale, read_history=True)

    assert out.states[-1] == "reading_history"
    live = [m for m, is_live in out.measurements if is_live]
    history = [m for m, is_live in out.measurements if not is_live]
    assert len(live) == 1
    assert [round(m.weight_kg) for m in history] == [80, 81, 82]
    assert scale.history_requests == 3


async def test_history_off_by_default():
    scale = FakeScale(ADDRESS, SECRETS, history=(realtime_payload(flags=bytes([1, 0])),))
    await go(scale)
    assert scale.history_requests == 0


class _IndicateOnlyChar:
    properties = ["indicate"]

    def __init__(self, with_cccd: bool = True) -> None:
        self._cccd = object() if with_cccd else None

    def get_descriptor(self, uuid: str):
        return self._cccd if uuid == s.CCCD_UUID else None


class _RecordingClient:
    def __init__(self) -> None:
        self.descriptor_writes: list[tuple[object, bytes]] = []

    async def write_gatt_descriptor(self, descriptor, data) -> None:
        self.descriptor_writes.append((descriptor, bytes(data)))


async def test_indicate_only_characteristics_get_the_indicate_bit() -> None:
    client = _RecordingClient()
    session = s.Scale3Session(client, ADDRESS, SECRETS, USER)
    char = _IndicateOnlyChar()
    await session._prefer_indication(char, "2a9d")
    assert client.descriptor_writes == [(char._cccd, b"\x02\x00")]


async def test_notify_capable_or_cccd_less_characteristics_are_left_alone() -> None:
    client = _RecordingClient()
    session = s.Scale3Session(client, ADDRESS, SECRETS, USER)

    both = _IndicateOnlyChar()
    both.properties = ["notify", "indicate"]
    await session._prefer_indication(both, "x")
    await session._prefer_indication(_IndicateOnlyChar(with_cccd=False), "y")
    await session._prefer_indication(object(), "z")  # no properties at all
    assert client.descriptor_writes == []


class _FlakyNotifyClient:
    """Refuses to subscribe to ``refused`` until ``allow`` is called."""

    def __init__(self, refused: str) -> None:
        self.refused = refused
        self.allowed = False
        self.subscribed: list[str] = []
        self.services = _OneCharServices(refused)

    def allow(self) -> None:
        self.allowed = True

    async def start_notify(self, char, callback) -> None:
        if char.uuid == self.refused and not self.allowed:
            raise RuntimeError("Application error")
        self.subscribed.append(char.uuid)


class _Char:
    properties = ["notify"]

    def __init__(self, uuid: str) -> None:
        self.uuid = uuid


class _OneCharServices:
    def __init__(self, uuid: str) -> None:
        self._char = _Char(uuid)

    def get_service(self, _uuid: str):
        return self

    def get_characteristic(self, _uuid: str):
        return self._char


async def test_refused_subscription_is_retried_later() -> None:
    from custom_components.huawei_scale3 import protocol as p

    uuid = p.CHR_REALTIME_WEIGHT
    client = _FlakyNotifyClient(uuid)
    session = s.Scale3Session(client, ADDRESS, SECRETS, USER)
    service, characteristic, kind = next(
        item for item in p.NOTIFY_CHARACTERISTICS if item[1] == uuid
    )

    assert not await session._start_notify(service, characteristic, kind)
    session._deferred.append((service, characteristic, kind))
    await session._retry_deferred_subscriptions()
    assert session._deferred and client.subscribed == []  # still refused

    client.allow()
    await session._retry_deferred_subscriptions()
    assert session._deferred == [] and client.subscribed == [uuid]


async def test_unbound_scale_is_left_alone_unless_binding_is_enabled():
    scale = FakeScale(ADDRESS, SECRETS, unbound=True)
    await go(scale)
    assert scale.manager_info is None
    assert scale.user_info[:30].rstrip(b"\x00") == b"openscale:381EC76608D3"


async def test_unbound_scale_is_bound_when_enabled():
    scale = FakeScale(ADDRESS, SECRETS, unbound=True)
    out = await go(scale, bind_if_unbound=True)

    assert scale.bind_request is not None and len(scale.bind_request) == 30
    assert scale.bind_request.rstrip(b"\x00") == b"openscale:381EC76608D3"
    info = scale.manager_info
    assert info is not None and len(info) == 70
    assert info[:30].rstrip(b"\x00") == b"openscale:381EC76608D3"
    assert info[30:].rstrip(b"\x00") == b"homeassistant:381EC76608D3"
    # The profile that follows uses the new manager's HUID and is accepted.
    assert scale.user_info[:30] == info[:30]
    assert len(out.measurements) == 1


async def test_bound_scale_is_never_rebound():
    scale = FakeScale(ADDRESS, SECRETS)
    await go(scale, bind_if_unbound=True)
    assert scale.manager_info is None
