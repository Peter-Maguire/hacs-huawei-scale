"""Protocol tests, anchored on the real Scale 3 capture from openScale's tests."""
from __future__ import annotations

from datetime import datetime

import pytest

from custom_components.huawei_scale3 import protocol as p

# ---- Real capture (HUAWEI Scale 3, 38:1E:C7:66:08:D3), from openScale's test suite
CAK_UNUSED = bytes(16)
C1 = bytes.fromhex("CA4946D061C9FE534F6044F930EBB69B")
C2 = bytes.fromhex("FBCE6E2B4BAF80ED969BA26B4A4B9325")
ADDRESS = "38:1E:C7:66:08:D3"
ROOT_KEY = bytes.fromhex("d680f0f3b50576a74c8815653e8b7243")
WORK_KEY = bytes.fromhex("36697370363671706d64386733696b62")
WORK_KEY_FRAMES = [
    "dc12203b033efce70954e24a3a06ac75603b418e",
    "dc12218a68cb34e5250a6c185813b62e6958c13f",
    "dc0522a49ddc06",
]
MEASUREMENT_FRAMES = [
    "cd1220ddae5ad500d352ad002d9ab60055a313f1",
    "cd12216d4c5b1e4fa529d8fc4225d8db8b3726b3",
    "cd0f222285c5b012308c6ee4a7829efa52",
]
MEASUREMENT_PLAIN = bytes.fromhex("b1212d01ea07080917271da0ea13000000000000000000006100")


def _deframe(frames: list[str]) -> bytes:
    return b"".join(bytes.fromhex(f)[3:-2] for f in frames)


def test_crc_table_matches_openscale_constants():
    # Spot values from the Kotlin lookup table.
    assert p._CRC16_TABLE[1] == 4129
    assert p._CRC16_TABLE[16] == 4657
    assert p._CRC16_TABLE[255] == 7920


def test_root_key_matches_capture():
    c3 = p.c3_from_bluetooth_address(ADDRESS)
    assert c3 == b"381EC76608D30000"
    assert p.derive_root_key(C1, C2, c3) == ROOT_KEY


def test_work_key_decrypts_from_capture():
    assert p.decrypt_payload(_deframe(WORK_KEY_FRAMES), ROOT_KEY) == WORK_KEY


def test_write_frames_are_bit_identical_to_the_capture():
    """Our encoder (incl. XMODEM CRC) must reproduce what the phone sent."""
    iv = _deframe(WORK_KEY_FRAMES)[:16]
    frames = p.build_encrypted_write_frames(WORK_KEY, ROOT_KEY, iv)
    assert [f.hex() for f in frames] == WORK_KEY_FRAMES


def test_notification_frames_have_valid_modbus_crc():
    for hexframe in MEASUREMENT_FRAMES:
        assert p.has_valid_notification_crc(bytes.fromhex(hexframe))


def test_accumulator_reassembles_and_decrypts_capture():
    acc = p.FrameAccumulator()
    out = None
    for hexframe in MEASUREMENT_FRAMES:
        out = acc.ingest(bytes.fromhex(hexframe))
    assert out is not None
    assert acc.last_completed_encrypted is True
    assert p.decrypt_payload(out, WORK_KEY) == MEASUREMENT_PLAIN


def test_capture_decodes_to_expected_measurement():
    m = p.parse_realtime_measurement(MEASUREMENT_PLAIN)
    assert m is not None
    assert m.weight_kg == pytest.approx(86.25)
    assert m.fat_percent == pytest.approx(30.1)
    assert m.heart_rate == 97
    assert m.timestamp == datetime(2026, 8, 9, 23, 39, 29)
    assert m.low_impedance[0] == 0x13EA
    assert m.impedance_ohm == pytest.approx(509.8)


def test_corrupt_notification_crc_is_dropped():
    frame = bytearray(bytes.fromhex(MEASUREMENT_FRAMES[0]))
    frame[5] ^= 0xFF
    assert p.FrameAccumulator().ingest(bytes(frame)) is None


def _to_notification(write_frame: bytes) -> bytes:
    frame = bytearray(write_frame)
    frame[0] = p.FRAME_NOTIFY_PLAIN
    crc = p.crc16_modbus(bytes(frame[:-2]))
    frame[-2], frame[-1] = crc & 0xFF, crc >> 8
    return bytes(frame)


def test_multi_frame_roundtrip():
    payload = bytes(range(40))
    frames = p.build_write_frames(payload)
    assert len(frames) == 3
    acc = p.FrameAccumulator()
    assert [acc.ingest(_to_notification(f)) for f in frames] == [None, None, payload]


def test_empty_payload_is_single_frame():
    (frame,) = p.build_write_frames(b"")
    assert len(frame) == 5 and frame[:3] == bytes([0xDB, 0x03, 0x00])


def test_accumulator_drops_orphan_and_restarts_on_index_zero():
    a = p.build_write_frames(bytes(range(26)))
    b = p.build_write_frames(bytes(range(100, 126)))
    acc = p.FrameAccumulator()
    assert acc.ingest(_to_notification(a[1])) is None  # orphan
    assert acc.ingest(_to_notification(a[0])) is None
    assert acc.ingest(_to_notification(b[0])) is None  # restart
    assert acc.ingest(_to_notification(b[1])) == bytes(range(100, 126))


def test_oversized_payload_rejected():
    with pytest.raises(ValueError):
        p.build_write_frames(bytes(241))


def test_auth_handshake_is_symmetric_and_checks_cak():
    rand_a, rand_b = bytes(range(16)), bytes(range(16, 32))
    cak = bytes(range(32, 48))
    token = p.build_auth_token_payload(rand_a, rand_b, cak)
    assert token[:16] == rand_b and len(token) == 48
    resp = p.expected_auth_response(rand_a, rand_b, cak)
    assert len(resp) == 32
    assert p.is_valid_auth_response(resp, rand_a, rand_b, cak)
    assert p.is_valid_auth_response(resp + b"\x00", rand_a, rand_b, cak)
    assert not p.is_valid_auth_response(resp[:-1], rand_a, rand_b, cak)
    assert not p.is_valid_auth_response(resp, rand_a, rand_b, bytes(16))


def test_user_info_layout():
    payload = p.build_user_info_payload(
        huid="openscale:001122334455",
        uid="u:00000007",
        gender=1,
        age_years=26,
        height_cm=171,
        weight_kg=83.2,
    )
    assert len(payload) == 69
    assert payload[62] == 1 and payload[63] == 26
    assert payload[64:66] == bytes([171, 0])
    assert payload[66:68] == bytes([0x80, 0x20])  # 8320 centi-kg
    assert payload.startswith(b"openscale:001122334455\x00")


def test_current_time_payload():
    # Monday 2026-08-10 07:05:09
    assert p.current_time_payload(datetime(2026, 8, 10, 7, 5, 9)) == bytes(
        [0xEA, 0x07, 8, 10, 7, 5, 9, 1]
    )


def _realtime_payload() -> bytes:
    return bytes(
        [0x34, 0x1E, 0xB9, 0x00, 0xEA, 0x07, 0x06, 0x16, 0x0F, 0x0E, 0x2A, 0x00]
        + [0xF4, 0x01, 0xF5, 0x01, 0xF6, 0x01, 0xF7, 0x01, 0xF8, 0x01, 0xF9, 0x01]
        + [0x48, 0x00]
        + [0x58, 0x02, 0x59, 0x02, 0x5A, 0x02, 0x5B, 0x02, 0x5C, 0x02, 0x5D, 0x02]
    )


def test_realtime_with_high_band():
    m = p.parse_realtime_measurement(_realtime_payload())
    assert m is not None and m.raw_length == 38
    assert m.weight_kg == pytest.approx(77.32)
    assert m.fat_percent == pytest.approx(18.5)
    assert m.heart_rate == 72
    assert m.low_impedance == (500, 501, 502, 503, 504, 505)
    assert m.high_impedance == (600, 601, 602, 603, 604, 605)


def test_realtime_tolerates_bad_timestamp_but_history_does_not():
    bad = bytearray(_realtime_payload())
    bad[4:7] = b"\x00\x00\x00"
    assert p.parse_realtime_measurement(bytes(bad)).timestamp is None
    assert p.parse_history_measurement(bytes(bad) + b"\x01\x00") is None


def test_history_record_flags():
    done = p.parse_history_measurement(_realtime_payload() + b"\x01\x00")
    more = p.parse_history_measurement(_realtime_payload() + b"\x01\x01")
    assert done.is_history_complete is True and more.is_history_complete is False
    assert done.timestamp == datetime(2026, 6, 22, 15, 14, 42)
    assert done.dedupe_key == p.parse_history_measurement(
        _realtime_payload() + b"\x01\x00"
    ).dedupe_key


def test_wrong_lengths_rejected():
    assert p.parse_realtime_measurement(bytes(25)) is None
    assert p.parse_history_measurement(bytes(26)) is None


def test_manager_info_basic_and_extended():
    huid = b"HUID-123".ljust(30, b"\x00")
    dev = b"DEV-9".ljust(40, b"\x00")
    assert p.parse_manager_info(huid + dev).huid == "HUID-123"
    info = p.parse_manager_info(huid + dev + bytes([4]) + b"acct")
    assert info.device_id == "DEV-9" and info.account_info == "acct"
    assert p.parse_manager_info(b"") == p.ManagerInfo("", "", None)
    assert p.parse_manager_info(b"x" * 30) is None


def test_scale_version_bitfield():
    # major=2 minor=0 patch=0 build=70 -> 0010 0000 0000 0001000110...
    bits = "0010" "0000" "0000" + format(70, "010b") + "00"
    raw = int(bits, 2).to_bytes(3, "big")
    v = p.parse_scale_version(b"\x00" + raw + b"SN12345")
    assert v.version == "2.0.0.70" and v.serial == "SN12345"


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("HUAWEI Scale 3", True),
        ("HEM-B19", True),
        ("HUAWEI Scale 3 Pro", False),
        ("Hagrid-B29", False),
        ("HUAWEI Scale 2 Pro", False),
        ("Mi Body Composition Scale", False),
        (None, False),
    ],
)
def test_identify_by_name(name, expected):
    assert p.identify_scale_3(name) is expected


def test_identify_by_manufacturer_data():
    assert p.identify_scale_3("x", {0x027D: b"model=M00D"})
    assert not p.identify_scale_3("x", {0x027D: b"model=007B"})


def test_weight_unit_payload_roundtrips_through_parse():
    assert p.parse_weight_unit(p.weight_unit_payload(metric=True)) == p.WeightUnit(1, "kg")
    assert p.parse_weight_unit(p.weight_unit_payload(metric=False)) == p.WeightUnit(2, "lb")


def test_parse_weight_unit_edge_cases():
    assert p.parse_weight_unit(b"") is None
    assert p.parse_weight_unit(bytes([3])) == p.WeightUnit(3, "g")
    assert p.parse_weight_unit(bytes([99])) == p.WeightUnit(99, "unknown")


def test_max_payload_matches_the_decompiled_apps_chunk_ceiling():
    # 238 bytes plain, or 222 ciphertext + 16-byte IV once encrypted.
    assert p.MAX_SIMPLE_PAYLOAD_BYTES == 238
    p.build_write_frames(bytes(238))
    with pytest.raises(ValueError):
        p.build_write_frames(bytes(239))
