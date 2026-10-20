"""A simulated Huawei Scale 3 for exercising ``Scale3Session`` without hardware.

It implements the *scale side* of the protocol as documented by the openScale
driver, built on the same primitives as the production code. It therefore
proves the session logic is self-consistent and handles framing, encryption and
ordering; it cannot prove the real firmware behaves the same way.
"""
from __future__ import annotations

import asyncio

from custom_components.huawei_scale3 import protocol as p
from custom_components.huawei_scale3.session import Secrets

SCALE_RAND_A = bytes(range(0xA0, 0xB0))


class FakeChar:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid


class FakeService:
    def __init__(self, uuids: list[str]) -> None:
        self._chars = {u: FakeChar(u) for u in uuids}

    def get_characteristic(self, uuid: str) -> FakeChar | None:
        return self._chars.get(uuid)


#: Write-only characteristics (no notification) that the simulated device
#: also needs to expose, so writes to them aren't silently dropped as absent.
_WRITE_ONLY_CHARACTERISTICS: tuple[tuple[str, str], ...] = (
    (p.SVC_CURRENT_TIME, p.CHR_SET_WEIGHT_UNIT),
)


class FakeServices:
    def __init__(self) -> None:
        layout: dict[str, list[str]] = {}
        for service, characteristic, _kind in p.NOTIFY_CHARACTERISTICS:
            layout.setdefault(service, []).append(characteristic)
        for service, characteristic in _WRITE_ONLY_CHARACTERISTICS:
            layout.setdefault(service, []).append(characteristic)
        self._services = {s: FakeService(c) for s, c in layout.items()}

    def get_service(self, uuid: str) -> FakeService | None:
        return self._services.get(uuid)


def realtime_payload(
    weight_kg: float = 86.25,
    fat: float = 30.1,
    impedance_raw: int = 3625,
    heart_rate: int = 61,
    flags: bytes = b"",
) -> bytes:
    """26-byte realtime payload (+ optional history flag bytes)."""
    out = bytearray(26)
    out[0:2] = round(weight_kg * 100).to_bytes(2, "little")
    out[2:4] = round(fat * 10).to_bytes(2, "little")
    out[4:11] = bytes([0xEA, 0x07, 0x08, 0x09, 0x17, 0x27, 0x1D])  # 2026-08-09 23:39:29
    out[12:14] = impedance_raw.to_bytes(2, "little")
    out[24:26] = heart_rate.to_bytes(2, "little")
    return bytes(out) + flags


class FakeScale:
    """Stands in for a connected ``BleakClient``."""

    def __init__(
        self,
        address: str,
        secrets: Secrets,
        *,
        realtime: bytes | None = None,
        history: tuple[bytes, ...] = (),
        status_ready_after: int = 1,
        reject_work_key: bool = False,
        answer_auth: bool = True,
        work_key_reply_encrypted: bool = True,
        with_gatt: bool = True,
        unbound: bool = False,
    ) -> None:
        self.address = address
        self.secrets = secrets
        self.realtime = realtime if realtime is not None else realtime_payload()
        self.history = list(history)
        self.status_ready_after = status_ready_after
        self.reject_work_key = reject_work_key
        self.answer_auth = answer_auth
        self.work_key_reply_encrypted = work_key_reply_encrypted
        self.services = FakeServices() if with_gatt else _EmptyServices()

        self.work_key: bytes | None = None
        self.user_info: bytes | None = None
        self.unbound = unbound  # no manager until SET_MANAGER_INFO is written
        self.manager_info: bytes | None = None
        self.bind_request: bytes | None = None
        self.clock: bytes | None = None
        self.weight_unit: bytes | None = None
        self.polls = 0
        self.history_requests = 0
        self.authenticated = False
        self.disconnected = False
        self._callbacks: dict[str, object] = {}
        self._rx: dict[str, dict[int, bytes]] = {}
        self._rand_a = SCALE_RAND_A
        self._rand_b: bytes | None = None
        self.disconnect_on_poll: object | None = None  # callable invoked on first poll

    # --- bleak client surface -------------------------------------------------

    async def disconnect(self) -> None:
        self.disconnected = True

    async def start_notify(self, char: FakeChar, callback) -> None:
        self._callbacks[char.uuid] = callback

    async def write_gatt_char(self, char: FakeChar, data: bytes, response: bool = True) -> None:
        payload = self._receive_frame(char.uuid, bytes(data))
        if payload is not None:
            self._handle(char.uuid, payload)

    # --- plumbing --------------------------------------------------------------

    def _receive_frame(self, uuid: str, frame: bytes) -> bytes | None:
        assert frame[0] in (p.FRAME_WRITE_PLAIN, p.FRAME_WRITE_ENCRYPTED), "bad prefix"
        zeroed = frame[:-2] + b"\x00\x00"
        crc = p.crc16_xmodem(zeroed)
        assert frame[-2:] == bytes([crc & 0xFF, crc >> 8]), "bad write CRC"
        chunk = frame[3:-2]
        assert frame[1] == len(chunk) + 3
        total, index = ((frame[2] >> 4) & 0xF) + 1, frame[2] & 0xF
        parts = self._rx.setdefault(uuid, {})
        parts[index] = chunk
        if len(parts) < total:
            return None
        payload = b"".join(parts[i] for i in range(total))
        self._rx[uuid] = {}
        if frame[0] == p.FRAME_WRITE_ENCRYPTED:
            assert self.work_key is not None or uuid == p.CHR_SEND_WORK_KEY
            key = self._root_key() if uuid == p.CHR_SEND_WORK_KEY else self.work_key
            payload = p.decrypt_payload(payload, key)
        return payload

    def _root_key(self) -> bytes:
        s = self.secrets
        return p.derive_root_key(s.c1, s.c2, p.c3_from_bluetooth_address(self.address))

    def _notify(self, uuid: str, payload: bytes, encrypted: bool = False) -> None:
        if encrypted:
            payload = p.encrypt_payload(payload, self.work_key, bytes(range(16)))
        loop = asyncio.get_running_loop()
        callback = self._callbacks[uuid]
        for wf in p.build_write_frames(payload, encrypted=encrypted):
            frame = bytearray(wf)
            frame[0] = p.FRAME_NOTIFY_ENCRYPTED if encrypted else p.FRAME_NOTIFY_PLAIN
            crc = p.crc16_modbus(bytes(frame[:-2]))
            frame[-2], frame[-1] = crc & 0xFF, crc >> 8
            loop.call_soon(callback, FakeChar(uuid), bytearray(frame))

    # --- the scale's behaviour -------------------------------------------------

    def _handle(self, uuid: str, payload: bytes) -> None:
        s = self.secrets
        if uuid == p.CHR_REQUEST_AUTH:
            # Scale 3 fw 2.0.0.70 appends a status byte to the challenge.
            self._notify(uuid, self._rand_a + b"\x00")
        elif uuid == p.CHR_AUTH_TOKEN:
            rand_b, token = payload[:16], payload[16:]
            expected = p.build_auth_token_payload(self._rand_a, rand_b, s.cak)[16:]
            if token == expected and self.answer_auth:
                self._notify(uuid, p.expected_auth_response(self._rand_a, rand_b, s.cak))
            elif self.answer_auth:
                self._notify(uuid, bytes(32))  # garbage: token did not verify
            # else: stay silent
        elif uuid == p.CHR_SEND_WORK_KEY:
            self.work_key = payload
            status = b"\x01" if self.reject_work_key else b"\x00"
            self.authenticated = not self.reject_work_key
            self._notify(uuid, status, encrypted=self.work_key_reply_encrypted)
        elif uuid == p.CHR_CURRENT_TIME:
            self.clock = payload
            self._notify(uuid, b"\x00")
        elif uuid == p.CHR_SET_WEIGHT_UNIT:
            self.weight_unit = payload
        elif uuid == p.CHR_GET_MANAGER_INFO:
            if self.manager_info is not None:
                self._notify(uuid, self.manager_info)
            elif self.unbound:
                self._notify(uuid, b"")
            else:
                self._notify(
                    uuid, b"HUID-FROM-SCALE".ljust(30, b"\x00") + b"DEVICE-1".ljust(40, b"\x00")
                )
        elif uuid == p.CHR_BIND_REQUEST:
            self.bind_request = payload
        elif uuid == p.CHR_SET_MANAGER_INFO:
            self.manager_info = payload
            self._notify(uuid, b"\x00", encrypted=True)
        elif uuid == p.CHR_SET_USER_INFO:
            self.user_info = payload
            unbound = self.unbound and self.manager_info is None
            self._notify(uuid, b"\x01" if unbound else b"\x00", encrypted=True)
        elif uuid == p.CHR_REALTIME_WEIGHT:
            if self.status_ready_after == 0 or self.polls >= self.status_ready_after:
                self._notify(uuid, self.realtime, encrypted=True)
        elif uuid == p.CHR_STATUS_POLL:
            self.polls += 1
            if self.disconnect_on_poll and self.polls == 1:
                self.disconnect_on_poll()
                return
            ready = self.polls >= self.status_ready_after
            self._notify(uuid, b"\x00" if ready else b"\x01")
            if ready and self.polls == self.status_ready_after:
                self._notify(p.CHR_REALTIME_WEIGHT, self.realtime, encrypted=True)
        elif uuid == p.CHR_PRODUCT_INFO:
            self._notify(uuid, b"M00D1")
        elif uuid == p.CHR_SCALE_VERSION:
            bits = "0010" "0000" "0000" + format(70, "010b") + "00"
            self._notify(uuid, b"\x00" + int(bits, 2).to_bytes(3, "big") + b"SN1")
        elif uuid == p.CHR_HISTORY_WEIGHT:
            self.history_requests += 1
            if self.history:
                self._notify(uuid, self.history.pop(0), encrypted=True)
            else:
                self._notify(uuid, b"", encrypted=True)


class _EmptyServices:
    def get_service(self, uuid: str):
        return None
