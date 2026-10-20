"""Huawei Scale 3 ("Hagrid" / WSP) wire protocol.

Pure Python, no Home Assistant imports, so it can be unit-tested on its own.

This is a port of the protocol handling in the openScale project
(``HuaweiHagridWspLib.kt``, GPL-3.0, https://github.com/oliexdev/openScale).
The CAK / C1 / C2 secrets are passed in by the caller (see ``const.py`` for the
built-in defaults); nothing in this module extracts them from anywhere.

Framing
-------
Every GATT write/notification carries one WSP frame::

    +--------+--------+-----------+----------------+-----------+
    | prefix | length | total/idx | payload (<=15) | CRC16 LE  |
    +--------+--------+-----------+----------------+-----------+

* prefix: ``DB`` plain write, ``DC`` encrypted write,
  ``BD`` plain notification, ``CD`` encrypted notification
* length: payload length + 3
* total/idx: high nibble = frame count - 1, low nibble = frame index
* CRC: CRC-16/XMODEM over the write frame (CRC bytes zeroed), CRC-16/MODBUS
  over the notification frame (without the CRC bytes)

Encrypted payloads are ``IV(16) || AES-128-CTR(plaintext)``.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import datetime

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# --------------------------------------------------------------------------
# GATT layout
# --------------------------------------------------------------------------


def uuid16(value: int) -> str:
    """Expand a 16-bit Bluetooth SIG UUID to its 128-bit string form."""
    return f"0000{value:04x}-0000-1000-8000-00805f9b34fb"


SVC_USER_DATA = uuid16(0x181C)
SVC_CURRENT_TIME = uuid16(0x1805)
SVC_BODY_COMPOSITION = uuid16(0x181B)
SVC_WEIGHT_SCALE = uuid16(0x181D)

CHR_REQUEST_AUTH = "02b2a08e-f8b0-4047-b1fd-f4e0efeee679"
CHR_AUTH_TOKEN = "32330a04-15d9-421a-91c5-2a2d5c7525c9"
CHR_SEND_WORK_KEY = "a3d330f8-b84f-4f48-a78c-f8d1e33b597a"
CHR_BIND_REQUEST = "42596cbe-d291-4da3-8ca6-d1ae5d1c9174"
CHR_SET_USER_INFO = "8cc61d7d-66c0-4802-89c3-38c5a163592e"
CHR_GET_MANAGER_INFO = "4338c65e-ed8e-4085-bbea-a25e33ca6b54"
CHR_SET_MANAGER_INFO = "3a3a7d71-37f6-4b94-b44a-21ac1916ac4f"
CHR_CURRENT_TIME = uuid16(0x2A2B)
CHR_PRODUCT_INFO = "75143e79-f878-4a00-a628-edc40509de7e"
CHR_SCALE_VERSION = "1f5d3d5c-496d-4290-af03-c7a8d5419741"
CHR_GET_WEIGHT_UNIT = "7e6dbc73-42e7-45b9-a6ec-6aa2d7834695"
CHR_SET_WEIGHT_UNIT = "11872f15-a91d-49da-ac89-5107284f3425"
CHR_STATUS_POLL = "bfc36f6e-4150-4a4b-9052-3d359e52962e"
CHR_STATUS_RESULT = "ba216311-1787-472b-bef6-3eb29e62293e"
CHR_WEIGHT_MEASUREMENT = uuid16(0x2A9D)
CHR_BODY_COMPOSITION_MEASUREMENT = uuid16(0x2A9C)
CHR_REALTIME_WEIGHT = "46797c17-d639-488d-9476-4789e8472878"
CHR_HISTORY_WEIGHT = "0212f42a-5f19-4bc1-ba52-d7ec7ccb71a4"

#: Standard-looking User Data Service characteristics (service 181c) that the
#: app also writes individually, alongside the encrypted SET_USER_INFO blob
#: we already send. Declared for completeness; not written. The app's own
#: payload encoding for these three isn't known (only the UUIDs are), while
#: SET_USER_INFO's 69-byte layout has been validated byte-for-byte against a
#: real capture (see protocol docstring / tests), so it already covers the
#: same profile data with a format we can trust.
CHR_SET_AGE = uuid16(0x2A80)
CHR_SET_GENDER = uuid16(0x2A8C)
CHR_SET_HEIGHT = uuid16(0x2A8E)

#: (service, characteristic, kind) for everything we subscribe to. ``kind`` is
#: how a payload received on that characteristic is routed by the session.
NOTIFY_CHARACTERISTICS: tuple[tuple[str, str, str], ...] = (
    (SVC_USER_DATA, CHR_REQUEST_AUTH, "request_auth"),
    (SVC_USER_DATA, CHR_AUTH_TOKEN, "auth_token"),
    (SVC_USER_DATA, CHR_SEND_WORK_KEY, "send_work_key"),
    (SVC_USER_DATA, CHR_BIND_REQUEST, "realtime"),
    (SVC_USER_DATA, CHR_SET_USER_INFO, "set_user_info"),
    (SVC_USER_DATA, CHR_GET_MANAGER_INFO, "manager_info"),
    (SVC_USER_DATA, CHR_SET_MANAGER_INFO, "set_manager_info"),
    (SVC_CURRENT_TIME, CHR_CURRENT_TIME, "time"),
    (SVC_CURRENT_TIME, CHR_PRODUCT_INFO, "product_info"),
    (SVC_CURRENT_TIME, CHR_SCALE_VERSION, "scale_version"),
    (SVC_CURRENT_TIME, CHR_GET_WEIGHT_UNIT, "weight_unit"),
    (SVC_CURRENT_TIME, CHR_STATUS_POLL, "status"),
    (SVC_CURRENT_TIME, CHR_STATUS_RESULT, "status"),
    (SVC_BODY_COMPOSITION, CHR_BODY_COMPOSITION_MEASUREMENT, "realtime"),
    (SVC_WEIGHT_SCALE, CHR_WEIGHT_MEASUREMENT, "realtime"),
    (SVC_BODY_COMPOSITION, CHR_REALTIME_WEIGHT, "realtime"),
    (SVC_BODY_COMPOSITION, CHR_HISTORY_WEIGHT, "history"),
)

# --------------------------------------------------------------------------
# Framing
# --------------------------------------------------------------------------

FRAME_WRITE_PLAIN = 0xDB
FRAME_WRITE_ENCRYPTED = 0xDC
FRAME_NOTIFY_PLAIN = 0xBD
FRAME_NOTIFY_ENCRYPTED = 0xCD

#: The app's own ceiling for a plain chunk before BLE-frame splitting is 238
#: bytes (222 for an encrypted one, which needs room for the 16-byte IV); we
#: never send more than 69 bytes (SET_USER_INFO) so this is never reached
#: either way.
MAX_SIMPLE_PAYLOAD_BYTES = 238
SIMPLE_CHUNK_BYTES = 15
USER_INFO_PAYLOAD_BYTES = 69
MANAGER_HUID_BYTES = 30
MANAGER_DEVICE_ID_BYTES = 40
MANAGER_INFO_PAYLOAD_BYTES = MANAGER_HUID_BYTES + MANAGER_DEVICE_ID_BYTES

AUTH_TOKEN_SUFFIX_TO_SCALE = b"1123"
AUTH_TOKEN_SUFFIX_FROM_SCALE = b"9856"


def _build_crc16_xmodem_table() -> tuple[int, ...]:
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
        table.append(crc & 0xFFFF)
    return tuple(table)


_CRC16_TABLE = _build_crc16_xmodem_table()


def crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM, used for frames written to the scale."""
    crc = 0
    for byte in data:
        crc = ((crc << 8) ^ _CRC16_TABLE[(byte ^ (crc >> 8)) & 0xFF]) & 0xFFFF
    return crc


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS, used for frames notified by the scale."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def build_write_frames(payload: bytes, encrypted: bool = False) -> list[bytes]:
    """Split ``payload`` into WSP write frames (15 payload bytes per frame)."""
    if len(payload) > MAX_SIMPLE_PAYLOAD_BYTES:
        raise ValueError(
            f"WSP payloads > {MAX_SIMPLE_PAYLOAD_BYTES} bytes are not supported"
        )
    total = max(1, -(-len(payload) // SIMPLE_CHUNK_BYTES))
    frames: list[bytes] = []
    offset = 0
    for index in range(total):
        chunk = payload[offset : offset + SIMPLE_CHUNK_BYTES]
        offset += len(chunk)
        frame = bytearray(len(chunk) + 5)
        frame[0] = FRAME_WRITE_ENCRYPTED if encrypted else FRAME_WRITE_PLAIN
        frame[1] = len(chunk) + 3
        frame[2] = (((total - 1) & 0x0F) << 4) | (index & 0x0F)
        frame[3 : 3 + len(chunk)] = chunk
        crc = crc16_xmodem(bytes(frame))  # CRC bytes are still zero here
        frame[-2] = crc & 0xFF
        frame[-1] = (crc >> 8) & 0xFF
        frames.append(bytes(frame))
    return frames


@dataclass(frozen=True)
class WspFrame:
    encrypted: bool
    total_frames: int
    frame_index: int
    payload: bytes
    crc_valid: bool


def has_notification_prefix(frame: bytes) -> bool:
    return bool(frame) and frame[0] in (FRAME_NOTIFY_PLAIN, FRAME_NOTIFY_ENCRYPTED)


def has_valid_notification_crc(frame: bytes) -> bool:
    if len(frame) < 5:
        return False
    expected = frame[-2] | (frame[-1] << 8)
    return crc16_modbus(frame[:-2]) == expected


def parse_notification_frame(
    frame: bytes, require_valid_crc: bool = True
) -> WspFrame | None:
    if len(frame) < 5 or not has_notification_prefix(frame):
        return None
    crc_valid = has_valid_notification_crc(frame)
    if require_valid_crc and not crc_valid:
        return None
    payload_len = frame[1] - 3
    if payload_len < 0 or len(frame) < payload_len + 5:
        return None
    total = ((frame[2] >> 4) & 0x0F) + 1
    index = frame[2] & 0x0F
    if index >= total:
        return None
    return WspFrame(
        encrypted=frame[0] == FRAME_NOTIFY_ENCRYPTED,
        total_frames=total,
        frame_index=index,
        payload=bytes(frame[3 : 3 + payload_len]),
        crc_valid=crc_valid,
    )


class FrameAccumulator:
    """Reassemble multi-frame notifications for a single characteristic."""

    def __init__(self, require_valid_crc: bool = True) -> None:
        self._require_valid_crc = require_valid_crc
        self._expected = 0
        self._encrypted = False
        self._chunks: dict[int, bytes] = {}
        self.last_completed_encrypted = False

    def _reset(self) -> None:
        self._expected = 0
        self._encrypted = False
        self._chunks.clear()

    def reset(self) -> None:
        self._reset()

    def ingest(self, raw_frame: bytes) -> bytes | None:
        """Feed one notification; return the full payload once complete."""
        frame = parse_notification_frame(raw_frame, self._require_valid_crc)
        if frame is None:
            return None
        if frame.total_frames <= 1:
            self.last_completed_encrypted = frame.encrypted
            self._reset()
            return frame.payload

        if frame.frame_index == 0:  # always starts a new logical packet
            self._reset()
            self._expected = frame.total_frames
            self._encrypted = frame.encrypted
        else:
            if self._expected == 0:  # orphan continuation frame
                return None
            if self._expected != frame.total_frames or self._encrypted != frame.encrypted:
                self._reset()
                return None

        self._chunks[frame.frame_index] = frame.payload
        if len(self._chunks) < self._expected:
            return None

        out = b"".join(self._chunks.get(i, b"") for i in range(self._expected))
        if any(i not in self._chunks for i in range(self._expected)):
            self._reset()
            return None
        self.last_completed_encrypted = self._encrypted
        self._reset()
        return out


# --------------------------------------------------------------------------
# Authentication and encryption
# --------------------------------------------------------------------------


def _hmac_sha256(key: bytes, message: bytes) -> bytes:
    return hmac.new(key, message, hashlib.sha256).digest()


def _auth_token(rand_a: bytes, rand_b: bytes, cak: bytes, suffix: bytes) -> bytes:
    rand = rand_a + rand_b
    inner_key = _hmac_sha256(cak + suffix, rand)
    return _hmac_sha256(inner_key, rand)


def build_auth_token_payload(rand_a: bytes, rand_b: bytes, cak: bytes) -> bytes:
    """Payload for the auth-token characteristic: ``randB || HMAC token``."""
    _require_len("randA", rand_a, 16)
    _require_len("randB", rand_b, 16)
    _require_len("CAK", cak, 16)
    return rand_b + _auth_token(rand_a, rand_b, cak, AUTH_TOKEN_SUFFIX_TO_SCALE)


def expected_auth_response(rand_a: bytes, rand_b: bytes, cak: bytes) -> bytes:
    return _auth_token(rand_a, rand_b, cak, AUTH_TOKEN_SUFFIX_FROM_SCALE)


def is_valid_auth_response(
    response: bytes, rand_a: bytes, rand_b: bytes, cak: bytes
) -> bool:
    expected = expected_auth_response(rand_a, rand_b, cak)
    # Some firmwares append a status byte, so only compare the prefix.
    return len(response) >= len(expected) and hmac.compare_digest(
        expected, bytes(response[: len(expected)])
    )


def c3_from_bluetooth_address(address: str) -> bytes:
    """C3 is the ASCII of the upper-case MAC (no separators) plus ``0000``."""
    compact = address.replace(":", "").replace("-", "").upper()
    if len(compact) != 12 or any(c not in "0123456789ABCDEF" for c in compact):
        raise ValueError("Bluetooth address must contain 12 hex digits")
    return (compact + "0000").encode("ascii")


def _sha256_first16(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()[:16]


def derive_root_key(c1: bytes, c2: bytes, c3: bytes) -> bytes:
    if not (len(c1) == len(c2) == len(c3)) or not c1:
        raise ValueError("C1, C2 and C3 must be non-empty and the same length")
    mixed = bytes(((c1[i] << 4) ^ c2[i]) & 0xFF for i in range(len(c1)))
    key_data = _sha256_first16(mixed)
    folded = bytes(((key_data[i] >> 6) ^ c3[i]) & 0xFF for i in range(len(key_data)))
    return _sha256_first16(folded)


def _aes_ctr(data: bytes, key: bytes, iv: bytes) -> bytes:
    _require_len("AES key", key, 16)
    _require_len("AES IV", iv, 16)
    encryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def encrypt_payload(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    """Return ``IV || ciphertext``."""
    return iv + _aes_ctr(plaintext, key, iv)


def decrypt_payload(iv_and_ciphertext: bytes, key: bytes) -> bytes:
    if len(iv_and_ciphertext) < 16:
        raise ValueError("Encrypted payload must include a 16-byte IV")
    return _aes_ctr(iv_and_ciphertext[16:], key, iv_and_ciphertext[:16])


def build_encrypted_write_frames(plaintext: bytes, key: bytes, iv: bytes) -> list[bytes]:
    return build_write_frames(encrypt_payload(plaintext, key, iv), encrypted=True)


def _require_len(name: str, value: bytes, length: int) -> None:
    if len(value) != length:
        raise ValueError(f"{name} must be {length} bytes, got {len(value)}")


# --------------------------------------------------------------------------
# Outgoing payloads
# --------------------------------------------------------------------------


def _put_fixed(target: bytearray, text: str, offset: int, length: int) -> None:
    raw = text.encode("utf-8")[:length]
    target[offset : offset + len(raw)] = raw


def build_user_info_payload(
    huid: str,
    uid: str,
    gender: int,
    age_years: int,
    height_cm: int,
    weight_kg: float,
    user_type: int = 0,
) -> bytes:
    """69-byte user profile sent (encrypted) before a measurement."""
    payload = bytearray(USER_INFO_PAYLOAD_BYTES)
    _put_fixed(payload, huid, 0, 30)
    _put_fixed(payload, uid, 30, 32)
    payload[62] = max(0, min(255, gender))
    payload[63] = max(0, min(255, age_years))
    payload[64:66] = max(0, min(0xFFFF, height_cm)).to_bytes(2, "little")
    centi_kg = int(round(weight_kg * 100)) if weight_kg and weight_kg > 0 else 0
    payload[66:68] = max(0, min(0xFFFF, centi_kg)).to_bytes(2, "little")
    payload[68] = max(0, min(255, user_type))
    return bytes(payload)


def build_manager_info_payload(huid: str, device_id: str) -> bytes:
    """70-byte SET_MANAGER_INFO payload (HUID, then device id) that binds a scale."""
    payload = bytearray(MANAGER_INFO_PAYLOAD_BYTES)
    _put_fixed(payload, huid, 0, MANAGER_HUID_BYTES)
    _put_fixed(payload, device_id, MANAGER_HUID_BYTES, MANAGER_DEVICE_ID_BYTES)
    return bytes(payload)


def weight_unit_payload(metric: bool) -> bytes:
    """Payload for SET_WEIGHT_UNIT: 1=kg, 2=lb (mirrors ``parse_weight_unit``)."""
    return bytes([1 if metric else 2])


def current_time_payload(now: datetime) -> bytes:
    """8-byte current-time value: year(LE16) month day hour min sec weekday."""
    return bytes(
        [
            now.year & 0xFF,
            (now.year >> 8) & 0xFF,
            now.month,
            now.day,
            now.hour,
            now.minute,
            now.second,
            now.isoweekday(),
        ]
    )


# --------------------------------------------------------------------------
# Incoming payloads
# --------------------------------------------------------------------------


def _u16(data: bytes, offset: int) -> int:
    return data[offset] | (data[offset + 1] << 8)


def _read_timestamp(data: bytes, offset: int) -> datetime | None:
    if offset + 6 >= len(data):
        return None
    year = _u16(data, offset)
    month, day, hour, minute, second = data[offset + 2 : offset + 7]
    try:
        if year < 2000:
            return None
        return datetime(year, month, day, hour, minute, second)
    except ValueError:
        return None


@dataclass(frozen=True)
class WeightMeasurement:
    raw_length: int
    weight_kg: float
    fat_percent: float
    #: Scale-local wall clock time; ``None`` if the bytes did not decode.
    timestamp: datetime | None
    low_impedance: tuple[int, ...]
    high_impedance: tuple[int, ...]
    heart_rate: int | None
    history_suspected_flag: int | None = None
    history_complete_flag: int | None = None

    @property
    def impedance_ohm(self) -> float | None:
        """Low-frequency impedance in ohm.

        The Scale 3 reports it in 0.1 ohm units (a real 350 ohm arrives as
        3500), per the openScale driver.
        """
        if self.low_impedance and self.low_impedance[0] > 0:
            return self.low_impedance[0] / 10.0
        return None

    @property
    def is_history_complete(self) -> bool | None:
        if self.history_complete_flag is None:
            return None
        return self.history_complete_flag == 0

    @property
    def dedupe_key(self) -> str:
        stamp = self.timestamp.isoformat() if self.timestamp else ""
        return "|".join(
            (
                stamp,
                str(round(self.weight_kg * 100)),
                str(round(self.fat_percent * 10)),
                str(self.heart_rate or ""),
                ",".join(map(str, self.low_impedance)),
                ",".join(map(str, self.high_impedance)),
            )
        )


def _parse_weight_measurement(
    payload: bytes, allowed_lengths: frozenset[int], timestamp_required: bool
) -> WeightMeasurement | None:
    if len(payload) not in allowed_lengths:
        return None
    high_present = len(payload) >= 38
    base = 38 if high_present else 26

    timestamp = _read_timestamp(payload, 4)
    if timestamp is None and timestamp_required:
        return None

    low = tuple(_u16(payload, 12 + i * 2) for i in range(6))
    high = tuple(_u16(payload, 26 + i * 2) for i in range(6)) if high_present else ()
    hr_raw = _u16(payload, 24)
    return WeightMeasurement(
        raw_length=len(payload),
        weight_kg=_u16(payload, 0) / 100.0,
        fat_percent=_u16(payload, 2) / 10.0,
        timestamp=timestamp,
        low_impedance=low,
        high_impedance=high,
        heart_rate=hr_raw if 1 <= hr_raw <= 240 else None,
        history_suspected_flag=payload[base] if len(payload) > base else None,
        history_complete_flag=payload[base + 1] if len(payload) > base + 1 else None,
    )


def parse_realtime_measurement(payload: bytes) -> WeightMeasurement | None:
    """Live measurement. A bad timestamp does not reject the packet."""
    return _parse_weight_measurement(payload, frozenset({26, 38}), False)


def parse_history_measurement(payload: bytes) -> WeightMeasurement | None:
    """Stored record. A valid timestamp is mandatory."""
    return _parse_weight_measurement(payload, frozenset({27, 28, 39, 40}), True)


def _decode_text(raw: bytes) -> str:
    nul = raw.find(0)
    if nul >= 0:
        raw = raw[:nul]
    raw = raw.rstrip(b"\x00 ")
    if not raw:
        return ""
    if all(0x20 <= b <= 0x7E for b in raw):
        return raw.decode("ascii")
    return "hex:" + raw.hex().upper()


@dataclass(frozen=True)
class ManagerInfo:
    huid: str
    device_id: str
    account_info: str | None


def parse_manager_info(payload: bytes) -> ManagerInfo | None:
    if not payload:
        return ManagerInfo("", "", None)
    if len(payload) <= MANAGER_HUID_BYTES:
        return None
    huid = _decode_text(payload[:MANAGER_HUID_BYTES])
    id_end_base = MANAGER_HUID_BYTES + MANAGER_DEVICE_ID_BYTES
    extended = len(payload) > id_end_base
    device_id = _decode_text(
        payload[MANAGER_HUID_BYTES : id_end_base if extended else len(payload)]
    )
    account: str | None = None
    if extended:
        declared = payload[id_end_base]
        end = min(len(payload), id_end_base + 1 + declared)
        if end > id_end_base + 1:
            account = _decode_text(payload[id_end_base + 1 : end]) or None
    return ManagerInfo(huid, device_id, account)


@dataclass(frozen=True)
class ScaleVersion:
    version: str
    serial: str


def parse_scale_version(payload: bytes) -> ScaleVersion | None:
    if len(payload) < 5 or len(payload) > 20:
        return None
    bits = "".join(f"{b:08b}" for b in payload[1:4])
    major = int(bits[0:4], 2)
    minor = int(bits[4:8], 2)
    patch = int(bits[8:12], 2)
    build = int(bits[12:22], 2)
    return ScaleVersion(f"{major}.{minor}.{patch}.{build}", _decode_text(payload[4:]))


@dataclass(frozen=True)
class WeightUnit:
    raw: int
    label: str  # "kg", "lb", "g", or "unknown"


def parse_weight_unit(payload: bytes) -> WeightUnit | None:
    if not payload:
        return None
    raw = payload[0]
    return WeightUnit(raw=raw, label={1: "kg", 2: "lb", 3: "g"}.get(raw, "unknown"))


def parse_product_id(payload: bytes) -> str | None:
    """First four ASCII characters of the product-info payload (e.g. ``M0CJ``)."""
    text = _decode_text(payload)
    if not text or text.startswith("hex:"):
        return None
    return text[:4]


# --------------------------------------------------------------------------
# Device identification
# --------------------------------------------------------------------------

#: Local names / ASCII markers that identify a Scale 3. ``HUAWEI SCALE 3 PRO``
#: also contains ``HUAWEI SCALE 3`` and is a different (unsupported) device.
SCALE_3_MARKERS = ("M00D", "HEM-B19", "HERM-B19", "HUAWEI SCALE 3")
OTHER_HAGRID_MARKERS = (
    "HUAWEI SCALE 3 PRO",
    "HAGRID-B29",
    "HAGRID2021-B19",
    "M00F",
    "HUAWEI SCALE 2 PRO",
    "HAG-B19",
    "HAGRID-B19",
    "007B",
    "HONOR SCALE 2",
    "LUP-B19",
    "LUPIN-B19HN",
    "N001",
    "DOBBY-B19",
    "M0CJ",
)


def _ascii_view(raw: bytes) -> str:
    return "".join(chr(b) if 0x20 <= b <= 0x7E else " " for b in raw)


def identify_scale_3(
    name: str | None,
    manufacturer_data: dict[int, bytes] | None = None,
    service_data: dict[str, bytes] | None = None,
) -> bool:
    """True if the advertisement looks like a Huawei Scale 3.

    Mirrors the openScale driver: look for a product marker in the local name
    or as ASCII inside the manufacturer / service data.
    """
    texts = [name or ""]
    texts += [_ascii_view(v) for v in (manufacturer_data or {}).values()]
    texts += [_ascii_view(v) for v in (service_data or {}).values()]
    for text in texts:
        upper = text.upper()
        if any(marker in upper for marker in OTHER_HAGRID_MARKERS):
            return False
        if any(marker in upper for marker in SCALE_3_MARKERS):
            return True
    return False
