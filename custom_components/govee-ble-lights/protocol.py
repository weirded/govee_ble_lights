"""Govee BLE light wire protocol: packet builders and status-frame parsers.

Pure functions, no I/O, so they can be unit-tested without Home Assistant.

Frames are 20 bytes: ``<head> <cmd> <payload...> <pad 0x00> <xor>``.
``0x33`` frames are commands, ``0xAA`` frames are status queries/replies,
``0xA3`` frames carry multi-packet scene data.

Status replies below were verified on an H6072 (hardware 1.00.01/1.00.12,
firmware 1.03.05, advertising as ``ihoment_H6072_*``) on 2026-11-23:

    AA 01 -> aa 01 01 ...              power (01 = on)
    AA 04 -> aa 04 64 ...              brightness, 0-100 scale on H6072
    AA 05 -> aa 05 04 33 08 ...        mode 0x04 (scene), scene code 0x0833 LE
    AA 06 -> aa 06 "1.03.05"           firmware version (ASCII)
    AA A5 NN -> aa a5 NN [b r g b]x3   segment brightness + RGB, 3 per frame
    AA 13 -> no reply (unsupported on H6072)

Every 0x33 command is acknowledged on notify with ``33 <cmd> <status>``:
status 00 = accepted, 01 = rejected (e.g. ``33 05 02`` manual RGB on H6072).
In colour-temperature mode ``AA 05`` replies ``aa 05 15 00 <K_hi> <K_lo>``
and the segment colours read back as 000000.
"""

from __future__ import annotations

from dataclasses import dataclass, field

FRAME_LEN = 20

HEAD_COMMAND = 0x33
HEAD_STATUS = 0xAA
HEAD_MULTI = 0xA3

CMD_POWER = 0x01
CMD_BRIGHTNESS = 0x04
CMD_MODE = 0x05
CMD_FIRMWARE = 0x06
CMD_SEGMENTS = 0xA5

# Sub-modes of CMD_MODE (byte 2)
MODE_MANUAL = 0x02
MODE_SCENE = 0x04
MODE_MANUAL_ALT = 0x0D
MODE_MUSIC = 0x0F
MODE_SEGMENTED = 0x15

# Ops inside MODE_SEGMENTED (byte 3)
SEG_OP_COLOR = 0x01
SEG_OP_BRIGHTNESS = 0x02

SEGMENTS_PER_STATUS_FRAME = 3

# Microphone music modes. H6072 (fw 1.03.05) takes ``33 05 0F <mode>
# <sensitivity 0-100> <autoColor> R G B`` and reads it back verbatim in
# ``AA 05 0F``; every other 0x05 music sub-mode (01/0C/11/13) is rejected.
# Names/indices are Govee's own musicMode enum for the SKU.
MUSIC_MODES = {
    "H6072": ("Energic", "Dynamic", "Calm", "Bounce", "Hopping", "Strike", "Vibrate",
              "Skittles", "Torch", "CandyCrush", "Fusion", "Luminous", "Separation"),
}
DEFAULT_MUSIC_SENSITIVITY = 80

# Models that take the 0x15 segmented colour encoding.
SEGMENTED_MODELS = frozenset({"H6053", "H6072", "H6102", "H6199", "H7061"})

# Models whose brightness byte is 0-100 rather than 0-255 (verified via AA 04).
PERCENT_BRIGHTNESS_MODELS = frozenset({"H6072", "H7013", "H7061"})

# Colour sub-mode for non-segmented models. H7013 (fw 1.00.23) acks 33 05 02
# but drops the RGB; 33 05 0D stores it and reads back as aa 05 0d RR GG BB.
MANUAL_COLOR_MODES = {"H7013": MODE_MANUAL_ALT}

# Segment counts for models where it is known; used for the all-segments mask.
# H7061 (fw 1.00.05): two flood heads; only mask bits 0-1 have any effect and
# AA A5 01 returns two [bri,R,G,B] entries (the cloud claims 15 segments).
SEGMENT_COUNTS = {"H6072": 8, "H7061": 2}

MIN_KELVIN = 2000
MAX_KELVIN = 9000

# Per-model colour-temperature range and white-point tints. The H6072 values
# come from Govee-app captures (cromulus/govee_ble_lights 5e74328): the app
# offers 2200-6500 K in 100 K steps and sends 2-degree blackbody RGB with it.
CT_RANGES = {"H6072": (2200, 6500)}
_H6072_TINTS = tuple(bytes.fromhex(v) for v in (
    "ff9829 ff9d33 ffa23c ffa645 ffaa4d ffae54 ffb25b ffb662 ffb969 ffbd6f "
    "ffc076 ffc37c ffc682 ffc987 ffcb8d ffce92 ffd097 ffd39c ffd5a1 ffd7a6 "
    "ffd9ab ffdbaf ffddb4 ffdfb8 ffe1bc ffe2c0 ffe4c4 ffe5c8 ffe7cc ffe8d0 "
    "ffead3 ffebd7 ffedda ffeede ffefe1 fff0e4 fff1e7 fff3ea fff4ed fff5f0 "
    "fff6f3 fff7f5 fff8f8 fff9fb").split())


def ct_range(model: str) -> tuple[int, int]:
    return CT_RANGES.get(model, (MIN_KELVIN, MAX_KELVIN))


def ct_tint(model: str, kelvin: int) -> tuple[int, int, int] | None:
    """App-captured white point for ``kelvin`` (nearest 100 K), if known."""
    if model != "H6072":
        return None
    lo, hi = CT_RANGES["H6072"]
    idx = (max(lo, min(hi, int(kelvin))) - lo + 50) // 100
    return tuple(_H6072_TINTS[min(idx, len(_H6072_TINTS) - 1)])


def checksum(data: bytes | list[int]) -> int:
    c = 0
    for b in data:
        c ^= b
    return c & 0xFF


def build_frame(data: list[int] | bytes) -> bytes:
    data = list(data)
    if len(data) > FRAME_LEN - 1:
        raise ValueError(f"frame data too long: {len(data)}")
    data += [0] * (FRAME_LEN - 1 - len(data))
    return bytes(data + [checksum(data)])


def valid_frame(frame: bytes) -> bool:
    return len(frame) == FRAME_LEN and checksum(frame[:19]) == frame[19]


# ---------------------------------------------------------------- commands


def power(on: bool) -> bytes:
    return build_frame([HEAD_COMMAND, CMD_POWER, 0x01 if on else 0x00])


def brightness(value: int) -> bytes:
    """Raw brightness byte (caller scales to the model's range)."""
    return build_frame([HEAD_COMMAND, CMD_BRIGHTNESS, max(0, min(255, int(value)))])


def all_segments_mask(model: str) -> int:
    count = SEGMENT_COUNTS.get(model, 15)
    return (1 << count) - 1


def segment_mask(segments: list[int]) -> int:
    """Segment numbers are 1-based, as in the Govee app."""
    mask = 0
    for s in segments:
        if s < 1 or s > 16:
            raise ValueError(f"segment out of range: {s}")
        mask |= 1 << (s - 1)
    return mask


def rgb_manual(r: int, g: int, b: int, sub_mode: int = MODE_MANUAL) -> bytes:
    return build_frame([HEAD_COMMAND, CMD_MODE, sub_mode, r, g, b])


def rgb_segments(r: int, g: int, b: int, mask: int) -> bytes:
    return build_frame([
        HEAD_COMMAND, CMD_MODE, MODE_SEGMENTED, SEG_OP_COLOR,
        r, g, b, 0, 0, 0, 0, 0, mask & 0xFF, (mask >> 8) & 0xFF,
    ])


def color_temp_segments(kelvin: int, white: tuple[int, int, int], mask: int) -> bytes:
    """Colour temperature on the 0x15 encoding: RGB=FFFFFF, Kelvin BE, white point."""
    kelvin = max(MIN_KELVIN, min(MAX_KELVIN, int(kelvin)))
    return build_frame([
        HEAD_COMMAND, CMD_MODE, MODE_SEGMENTED, SEG_OP_COLOR,
        0xFF, 0xFF, 0xFF, (kelvin >> 8) & 0xFF, kelvin & 0xFF,
        white[0], white[1], white[2], mask & 0xFF, (mask >> 8) & 0xFF,
    ])


def segment_brightness(pct: int, mask: int) -> bytes:
    return build_frame([
        HEAD_COMMAND, CMD_MODE, MODE_SEGMENTED, SEG_OP_BRIGHTNESS,
        max(0, min(100, int(pct))), mask & 0xFF, (mask >> 8) & 0xFF,
    ])


def music(mode: int, sensitivity: int = DEFAULT_MUSIC_SENSITIVITY,
          rgb: tuple[int, int, int] | None = None) -> bytes:
    """Music mode; ``rgb`` None = automatic colours."""
    auto = rgb is None
    r, g, b = rgb or (0, 0, 0)
    return build_frame([HEAD_COMMAND, CMD_MODE, MODE_MUSIC, mode & 0xFF,
                        max(0, min(100, int(sensitivity))), 0x01 if auto else 0x00, r, g, b])


def scene_activate(code: int) -> bytes:
    return build_frame([HEAD_COMMAND, CMD_MODE, MODE_SCENE, code & 0xFF, (code >> 8) & 0xFF])


def multi_packet(protocol_type: int, header: list[int], data: bytes) -> list[bytes]:
    """Split a large payload (scene parameters) into 0xA3 frames.

    First frame: ``a3 00 01 <count> <header> <data...>``; middle frames
    ``a3 <i> <17 bytes>``; last frame ``a3 ff <rest>``.
    """
    first_space = 15 - len(header)
    first = list(data[:first_space])
    rest = list(data[first_space:])
    chunks = [rest[i:i + 17] for i in range(0, len(rest), 17)] or [[]]
    middle, last = chunks[:-1], chunks[-1]
    frames = [build_frame([protocol_type, 0x00, 0x01, len(middle) + 2, *header, *first])]
    for i, chunk in enumerate(middle, start=1):
        frames.append(build_frame([protocol_type, i, *chunk]))
    frames.append(build_frame([protocol_type, 0xFF, *last]))
    return frames


# ---------------------------------------------------------------- queries


def query(cmd: int, *args: int) -> bytes:
    return build_frame([HEAD_STATUS, cmd, *args])


def status_queries(model: str) -> list[bytes]:
    frames = [query(CMD_POWER), query(CMD_BRIGHTNESS), query(CMD_MODE)]
    if model in SEGMENTED_MODELS:
        count = SEGMENT_COUNTS.get(model, 15)
        groups = (count + SEGMENTS_PER_STATUS_FRAME - 1) // SEGMENTS_PER_STATUS_FRAME
        frames += [query(CMD_SEGMENTS, g) for g in range(1, groups + 1)]
    return frames


# ---------------------------------------------------------------- state


@dataclass
class Segment:
    brightness: int  # 0-100
    rgb: tuple[int, int, int]


@dataclass
class LightState:
    power: bool | None = None
    brightness_raw: int | None = None
    mode: int | None = None
    mode_data: bytes = b""
    firmware: str | None = None
    segments: dict[int, Segment] = field(default_factory=dict)  # 1-based

    @property
    def kelvin(self) -> int | None:
        """Colour temperature when the light is in white (CT) mode."""
        # H6072 reports CT as aa 05 15 00 <K_hi> <K_lo>; a non-zero first byte
        # is segment data (H7061 inlines [bri,R,G,B] per head here).
        if self.mode == MODE_SEGMENTED and len(self.mode_data) >= 3 and self.mode_data[0] == 0:
            k = (self.mode_data[1] << 8) | self.mode_data[2]
            return k or None
        return None

    @property
    def music_mode(self) -> int | None:
        if self.mode == MODE_MUSIC and self.mode_data:
            return self.mode_data[0]
        return None

    @property
    def scene_code(self) -> int | None:
        if self.mode == MODE_SCENE and len(self.mode_data) >= 2:
            return self.mode_data[0] | (self.mode_data[1] << 8)
        return None

    def uniform_rgb(self) -> tuple[int, int, int] | None:
        """RGB shared by all lit segments, or None if they differ.

        Non-segmented lights report their colour in ``AA 05 <02|0D> R G B``.
        """
        if not self.segments:
            if self.mode in (MODE_MANUAL, MODE_MANUAL_ALT) and len(self.mode_data) >= 3:
                rgb = tuple(self.mode_data[:3])
                return rgb if any(rgb) else None
            return None
        colours = {s.rgb for s in self.segments.values() if s.brightness > 0}
        return colours.pop() if len(colours) == 1 else None


def parse_ack(frame: bytes) -> tuple[int, bool] | None:
    """``33 <cmd> <status>`` ack -> (cmd, accepted), else None."""
    if not valid_frame(frame) or frame[0] != HEAD_COMMAND:
        return None
    return frame[1], frame[2] == 0x00


def apply_status_frame(state: LightState, frame: bytes) -> int | None:
    """Merge one 0xAA reply into ``state``. Returns the status cmd, or None."""
    if not valid_frame(frame) or frame[0] != HEAD_STATUS:
        return None
    cmd = frame[1]
    body = frame[2:19]
    if cmd == CMD_POWER:
        state.power = body[0] == 0x01
    elif cmd == CMD_BRIGHTNESS:
        state.brightness_raw = body[0]
    elif cmd == CMD_MODE:
        state.mode = body[0]
        state.mode_data = bytes(body[1:])
    elif cmd == CMD_FIRMWARE:
        state.firmware = bytes(body).split(b"\x00", 1)[0].decode("ascii", "replace")
    elif cmd == CMD_SEGMENTS:
        group = body[0]
        for i in range(SEGMENTS_PER_STATUS_FRAME):
            off = 1 + i * 4
            seg = (group - 1) * SEGMENTS_PER_STATUS_FRAME + i + 1
            b, r, g, bl = body[off:off + 4]
            state.segments[seg] = Segment(b, (r, g, bl))
    return cmd


def trim_segments(state: LightState, model: str) -> None:
    count = SEGMENT_COUNTS.get(model)
    if count:
        for seg in [s for s in state.segments if s > count]:
            del state.segments[seg]
