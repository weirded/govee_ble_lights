"""Connection handling for a Govee BLE light.

One GATT connection per device, reused for bursts of commands and released
after ``DISCONNECT_DELAY`` seconds idle so it doesn't pin a proxy slot or
lock the Govee phone app out. Status queries are answered on the notify
characteristic and merged into ``self.state``.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from . import protocol as p

_LOGGER = logging.getLogger(__name__)

WRITE_UUID = "00010203-0405-0607-0809-0a0b0c0d2b11"
NOTIFY_UUID = "00010203-0405-0607-0809-0a0b0c0d2b10"

# Longer than the 30 s poll interval, so a polled light keeps one proxy slot
# instead of reconnecting (≈8 s through a proxy) on every poll.
DISCONNECT_DELAY = 45
REPLY_TIMEOUT = 1.5
INTER_FRAME_DELAY = 0.05


class GoveeCommandRejected(Exception):
    """The light acknowledged a command with a non-zero status."""


class GoveeBLEDevice:
    def __init__(self, ble_device: BLEDevice, model: str, name: str) -> None:
        self._ble_device = ble_device
        self.model = model
        self.name = name
        self.state = p.LightState()
        self._client: BleakClientWithServiceCache | None = None
        self._lock = asyncio.Lock()
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._pending: dict[int, asyncio.Future[None]] = {}
        self._pending_acks: dict[int, asyncio.Future[bool]] = {}
        self._listeners: list[Callable[[], None]] = []

    # ------------------------------------------------------------ plumbing

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        self._ble_device = ble_device

    @property
    def address(self) -> str:
        return self._ble_device.address

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    def _notify_listeners(self) -> None:
        for cb in list(self._listeners):
            cb()

    def _on_notify(self, _char, data: bytearray) -> None:
        frame = bytes(data)
        ack = p.parse_ack(frame)
        if ack is not None:
            fut = self._pending_acks.pop(ack[0], None)
            if fut and not fut.done():
                fut.set_result(ack[1])
            return
        cmd = p.apply_status_frame(self.state, frame)
        if cmd is None:
            _LOGGER.debug("%s: ignoring notify %s", self.name, frame.hex())
            return
        _LOGGER.debug("%s: status %s", self.name, frame.hex())
        key = (cmd << 8) | (frame[2] if cmd == p.CMD_SEGMENTS else 0)
        fut = self._pending.pop(key, None)
        if fut and not fut.done():
            fut.set_result(None)

    def _on_disconnected(self, _client) -> None:
        _LOGGER.debug("%s: disconnected", self.name)
        self._client = None
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
        for fut in self._pending_acks.values():
            if not fut.done():
                fut.cancel()
        self._pending_acks.clear()

    async def _ensure_connected(self) -> BleakClientWithServiceCache:
        if self._client and self._client.is_connected:
            return self._client
        _LOGGER.debug("%s: connecting to %s", self.name, self.address)
        client = await establish_connection(
            BleakClientWithServiceCache,
            self._ble_device,
            self.name,
            disconnected_callback=self._on_disconnected,
            ble_device_callback=lambda: self._ble_device,
        )
        await client.start_notify(NOTIFY_UUID, self._on_notify)
        self._client = client
        return client

    def _schedule_disconnect(self) -> None:
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        loop = asyncio.get_running_loop()
        self._disconnect_timer = loop.call_later(
            DISCONNECT_DELAY, lambda: asyncio.ensure_future(self._idle_disconnect())
        )

    async def _idle_disconnect(self) -> None:
        async with self._lock:
            client, self._client = self._client, None
            if client and client.is_connected:
                _LOGGER.debug("%s: idle, disconnecting", self.name)
                try:
                    await client.disconnect()
                except BleakError:
                    pass

    async def async_stop(self) -> None:
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        await self._idle_disconnect()

    async def _write(self, frames: list[bytes]) -> None:
        async with self._lock:
            try:
                client = await self._ensure_connected()
                loop = asyncio.get_running_loop()
                for frame in frames:
                    _LOGGER.debug("%s: write %s", self.name, frame.hex())
                    if frame[0] != p.HEAD_COMMAND:
                        await client.write_gatt_char(WRITE_UUID, frame, response=False)
                        await asyncio.sleep(INTER_FRAME_DELAY)
                        continue
                    fut = loop.create_future()
                    self._pending_acks[frame[1]] = fut
                    await client.write_gatt_char(WRITE_UUID, frame, response=False)
                    try:
                        accepted = await asyncio.wait_for(fut, REPLY_TIMEOUT)
                    except (TimeoutError, asyncio.CancelledError):
                        self._pending_acks.pop(frame[1], None)
                        _LOGGER.debug("%s: no ack for %s", self.name, frame.hex())
                        continue
                    if not accepted:
                        raise GoveeCommandRejected(f"{self.name} rejected command {frame.hex()}")
            finally:
                self._schedule_disconnect()

    # ------------------------------------------------------------ queries

    async def async_update(self) -> p.LightState:
        """Query power, brightness, mode and segment colours."""
        async with self._lock:
            try:
                client = await self._ensure_connected()
                loop = asyncio.get_running_loop()
                for frame in p.status_queries(self.model):
                    key = (frame[1] << 8) | (frame[2] if frame[1] == p.CMD_SEGMENTS else 0)
                    fut = loop.create_future()
                    self._pending[key] = fut
                    await client.write_gatt_char(WRITE_UUID, frame, response=False)
                    try:
                        await asyncio.wait_for(fut, REPLY_TIMEOUT)
                    except (TimeoutError, asyncio.CancelledError):
                        _LOGGER.debug("%s: no reply to %s", self.name, frame.hex())
                        self._pending.pop(key, None)
                if self.state.firmware is None:
                    fut = loop.create_future()
                    self._pending[p.CMD_FIRMWARE << 8] = fut
                    await client.write_gatt_char(WRITE_UUID, p.query(p.CMD_FIRMWARE), response=False)
                    try:
                        await asyncio.wait_for(fut, REPLY_TIMEOUT)
                    except (TimeoutError, asyncio.CancelledError):
                        self._pending.pop(p.CMD_FIRMWARE << 8, None)
                p.trim_segments(self.state, self.model)
            finally:
                self._schedule_disconnect()
        self._notify_listeners()
        return self.state

    # ------------------------------------------------------------ commands

    @property
    def percent_brightness(self) -> bool:
        return self.model in p.PERCENT_BRIGHTNESS_MODELS

    @property
    def segmented(self) -> bool:
        return self.model in p.SEGMENTED_MODELS

    async def async_set_power(self, on: bool) -> None:
        await self._write([p.power(on)])
        self.state.power = on
        self._notify_listeners()

    async def async_set_brightness(self, ha_brightness: int) -> None:
        """HA brightness 1-255 -> device scale."""
        raw = round(ha_brightness * 100 / 255) if self.percent_brightness else ha_brightness
        raw = max(1, raw)
        await self._write([p.brightness(raw)])
        self.state.brightness_raw = raw
        self._notify_listeners()

    def ha_brightness(self) -> int | None:
        raw = self.state.brightness_raw
        if raw is None:
            return None
        return round(raw * 255 / 100) if self.percent_brightness else raw

    async def async_set_rgb(self, rgb: tuple[int, int, int], segments: list[int] | None = None) -> None:
        if self.segmented:
            mask = p.segment_mask(segments) if segments else p.all_segments_mask(self.model)
            frame = p.rgb_segments(*rgb, mask)
        else:
            if segments:
                raise ValueError(f"{self.model} has no segment control")
            frame = p.rgb_manual(*rgb)
        await self._write([frame])
        self.state.mode = p.MODE_SEGMENTED if self.segmented else p.MODE_MANUAL
        self.state.mode_data = b""
        for seg in segments or list(self.state.segments):
            if seg in self.state.segments:
                self.state.segments[seg].rgb = tuple(rgb)
        self._notify_listeners()

    async def async_set_color_temp(
        self, kelvin: int, white: tuple[int, int, int], segments: list[int] | None = None
    ) -> None:
        if not self.segmented:
            raise ValueError(f"colour temperature not implemented for {self.model}")
        mask = p.segment_mask(segments) if segments else p.all_segments_mask(self.model)
        await self._write([p.color_temp_segments(kelvin, white, mask)])
        self.state.mode = p.MODE_SEGMENTED
        self.state.mode_data = bytes([0, (kelvin >> 8) & 0xFF, kelvin & 0xFF])
        self._notify_listeners()

    async def async_set_segment_brightness(self, pct: int, segments: list[int]) -> None:
        if not self.segmented:
            raise ValueError(f"{self.model} has no segment control")
        await self._write([p.segment_brightness(pct, p.segment_mask(segments))])
        for seg in segments:
            if seg in self.state.segments:
                self.state.segments[seg].brightness = pct
        self._notify_listeners()

    async def async_set_scene(self, code: int, param: bytes) -> None:
        frames = p.multi_packet(p.HEAD_MULTI, [0x02], param) if param else []
        frames.append(p.scene_activate(code))
        await self._write(frames)
        self.state.mode = p.MODE_SCENE
        self.state.mode_data = bytes([code & 0xFF, code >> 8])
        self._notify_listeners()
