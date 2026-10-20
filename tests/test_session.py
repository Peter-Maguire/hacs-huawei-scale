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
    scale: FakeScale, *, secrets=SECRETS, read_history=False, user=USER, metric=True, before=None
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


async def test_subscriptions_stay_within_the_proxy_limit():
    """ESPHome proxies refuse (GATT error 128) beyond ~12 concurrent subscriptions."""
    scale = FakeScale(ADDRESS, SECRETS, history=(realtime_payload(flags=bytes([1, 0])),))
    out = await go(scale, read_history=True)
    assert len(out.measurements) >= 1
    assert scale.peak_subscriptions <= 9


async def test_realtime_characteristic_is_subscribed_while_measuring():
    from custom_components.huawei_scale3 import protocol as p

    scale = FakeScale(ADDRESS, SECRETS)
    seen = []

    original = scale.write_gatt_char

    async def spy(char, data, response=True):
        if char.uuid == p.CHR_REALTIME_WEIGHT:
            seen.append(p.CHR_REALTIME_WEIGHT in scale._callbacks)
        await original(char, data, response)

    scale.write_gatt_char = spy
    await go(scale)
    assert seen and all(seen)
