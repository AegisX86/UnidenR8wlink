"""
Parsing for everything the R8w sends over BLE.

Nothing in this file imports bleak or touches a radio. It takes bytes or
strings in and gives dataclasses back, which means:

  - you can test it against captured packets with no hardware,
  - PROTOCOL.md and this file can be checked against each other,
  - anyone porting this to ESP32 or Rust (lol) only has to read one file.

nothing here raises on unexpected input. Firmware updates and
laser guns I have never seen should not crash a program that is running on
a dashboard at 70 mph. Unknown values come back as None or as the original
string, and every model keeps its `raw` field so you can see what actually
arrived.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

# --------------------------------------------------------------------------
# Lookup tables (see PROTOCOL.md for where these come from)
# --------------------------------------------------------------------------

DIRECTIONS = {"F": "front", "S": "side", "R": "rear"}

MUTE_STATUS = {
    "1": "not muted",
    "2": "muted",
    "3": "mute memory",
    "4": "auto mute memory",
    "5": "blocked mute",
    "6": "quiet ride mute",
}

# For LASER alerts the frequency field holds a gun ID instead of a frequency.
LASER_GUNS = {
    "0": "Generic Laser", "1": "LTI 20/20", "2": "Stalker",
    "3": "RIEGL", "4": "Laser Ally", "5": "Kustom",
    "6": "Atlanta", "7": "Laveg", "8": "SL700",
    "9": "SCS-102", "10": "TraffiPat", "11": "Truspeed S",
    "12": "Stealth", "13": "TruCam", "14": "XLR",
    "15": "DragonEye Compact", "16": "DragonEye Full-Size",
    "17": "PoliScan", "18": "Traffistar s350", "19": "Vitronic Poliscan",
}

POI_KINDS = {1: "speed camera", 2: "red light camera", 3: "user mark"}

# Record length in BYTES, keyed by the leading type byte. Type 0 ("none") is
# deliberately absent: it has length 0 and would spin the parser forever.
POI_RECORD_LEN = {1: 13, 2: 12, 3: 10}


# --------------------------------------------------------------------------
# Small helpers. All of them answer "give me this or None, never explode".
# --------------------------------------------------------------------------

def _text(payload: str | bytes | bytearray) -> str:
    """Characteristic payloads are UTF-8 text. Bad bytes become U+FFFD
    rather than a UnicodeDecodeError, so a garbled packet costs you one
    reading instead of the process."""
    if isinstance(payload, (bytes, bytearray)):
        return payload.decode("utf-8", "replace").strip("\x00").strip()
    return payload.strip()


def _at(parts: list[str], i: int) -> str | None:
    """Field i, or None if the packet was shorter than expected. Field
    counts have already varied between my captures and the decompiled
    source, so nothing indexes a list directly."""
    if i < len(parts):
        value = parts[i].strip()
        return value or None
    return None


def _int(value: str | None) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _float(value: str | None) -> float | None:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Telemetry ("ETC data")
# --------------------------------------------------------------------------

@dataclass
class PoiAlert:
    """The POI the detector is currently warning you about. Not the same
    thing as the stored POI database, see Poi below."""
    kind: str | None = None          # "SPEEDCAM" and friends, verbatim
    distance: int | None = None      # units unconfirmed, see PROTOCOL.md
    speed_limit: int | None = None   # mph


@dataclass
class Telemetry:
    """One ETC packet: voltage, heading, and status flags.

    The R8w does NOT send your latitude and longitude. It sends heading,
    speed and altitude only; the phone app supplies the actual position
    from the phone's own GPS. If you need coordinates on a Pi, that is a
    separate GPS module, sorry.
    """
    voltage: float | None = None
    poi: PoiAlert | None = None
    heading: str | None = None        # N, NE, E, SE, S, SW, W, NW
    speed: int | None = None          # mph
    altitude: int | None = None       # feet
    gps_status: str | None = None     # "C" = connected
    warning: str | None = None
    scan_count: int | None = None
    wifi: str | None = None
    brightness: str | None = None
    raw: str = ""

    @property
    def gps_locked(self) -> bool:
        return self.gps_status == "C"


def parse_telemetry(payload: str | bytes | bytearray) -> Telemetry:
    """Parse an ETC packet, e.g. `12.1&0&W,0,193,C&0&12&D&D`.

    Seven &-separated fields, two of which (POI and GPS) are themselves
    comma-separated groups. The field order is the one in the app's
    parseETCData switch statement, which is authoritative; my earliest
    notes described nine flat fields and were wrong.
    """
    text = _text(payload)
    fields = text.split("&")
    t = Telemetry(raw=text)

    t.voltage = _float(_at(fields, 0))

    poi = _at(fields, 1)
    if poi and poi != "0":
        p = poi.split(",")
        t.poi = PoiAlert(
            kind=_at(p, 0),
            distance=_int(_at(p, 1)),
            speed_limit=_int(_at(p, 2)),
        )

    gps = _at(fields, 2)
    if gps and gps != "0":
        g = gps.split(",")
        t.heading = _at(g, 0)
        t.speed = _int(_at(g, 1))
        t.altitude = _int(_at(g, 2))
        t.gps_status = _at(g, 3)

    warning = _at(fields, 3)
    t.warning = warning if warning != "0" else None
    t.scan_count = _int(_at(fields, 4))
    t.wifi = _at(fields, 5)
    t.brightness = _at(fields, 6)
    return t


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------

@dataclass
class Alert:
    """One active detection.

    Field 5 is the frequency, even though the decompiled source calls it
    `info`. Field 4 is called `raw_value` there and is not the frequency.
    Trust the packets, not the variable names.
    """
    band: str = ""                     # K, KA, X, LASER, MRCD, MRCT, RT3, ...
    strength: int | None = None        # 1-8, the bars on the display
    raw_signal: int | None = None      # finer-grained strength, see notes
    frequency_ghz: float | None = None # None for LASER and Gatso
    laser_gun: str | None = None       # set only when band == "LASER"
    direction: str | None = None       # F / S / R
    mute_code: str | None = None
    receive_mode: str | None = None
    alert_id: str | None = None
    raw: str = ""

    @property
    def direction_name(self) -> str:
        return DIRECTIONS.get(self.direction or "", self.direction or "unknown")

    @property
    def mute_status(self) -> str:
        return MUTE_STATUS.get(self.mute_code or "", "unknown")

    @property
    def is_muted(self) -> bool:
        return self.mute_code in ("2", "3", "4", "5", "6")

    @property
    def description(self) -> str:
        """What you would actually print: a frequency, a laser gun name, or
        'Gatso' for the photo radar bands that have no frequency."""
        if self.laser_gun:
            return self.laser_gun
        if self.band in ("RT3", "RT4"):
            return "Gatso"
        if self.frequency_ghz is not None:
            return f"{self.frequency_ghz:g} GHz"
        return self.band or "unknown"

    def __str__(self) -> str:
        return f"{self.band} {self.description} {self.strength}/8 {self.direction_name}"


def parse_alerts(payload: str | bytes | bytearray) -> list[Alert]:
    """Parse an alert packet, e.g. `1,00,KA,3,33,33.7850,R,1&0&0&0`.

    Every packet is a full snapshot of what is currently being detected,
    not a delta. `0&0&0&0` means all clear and gives you an empty list.
    Segments are separated by &, fields within a segment by comma.
    """
    text = _text(payload)
    alerts: list[Alert] = []

    for segment in text.split("&"):
        segment = segment.strip()
        if not segment or segment == "0":
            continue
        f = segment.split(",")
        if _at(f, 0) == "0":  # slot present but nothing in it
            continue

        band = (_at(f, 2) or "").upper()
        freq_field = _at(f, 5)

        alerts.append(Alert(
            band=band,
            strength=_int(_at(f, 3)),
            raw_signal=_int(_at(f, 4)),
            # For laser the same field carries a gun ID, so only one of
            # these two ever gets filled in.
            frequency_ghz=None if band == "LASER" else _float(freq_field),
            laser_gun=(
                LASER_GUNS.get(freq_field or "", f"unknown laser ({freq_field})")
                if band == "LASER" else None
            ),
            direction=_at(f, 6),
            mute_code=_at(f, 7),
            receive_mode=_at(f, 8),
            alert_id=_at(f, 1),
            raw=segment,
        ))

    return alerts


# --------------------------------------------------------------------------
# Stored POI database
# --------------------------------------------------------------------------

@dataclass
class Poi:
    """A saved camera or user mark. Unlike everything else on this device
    these DO carry real coordinates, as big-endian IEEE 754 floats."""
    kind: str = ""
    latitude: float | None = None
    longitude: float | None = None
    angle: int | None = None         # cameras only, heading they face
    speed_limit: int | None = None   # speed cameras only, mph
    raw: str = ""


def parse_poi(payload: bytes | bytearray) -> list[Poi]:
    """Parse the POI characteristic.

    This one is a byte stream, not a fixed record array: read a type byte,
    look up how long that record is, consume it, repeat. An unrecognised
    type byte stops the walk rather than guessing a length, because
    guessing wrong desyncs everything after it.
    """
    data = bytes(payload)
    out: list[Poi] = []
    i = 0

    while i < len(data):
        kind = data[i]
        length = POI_RECORD_LEN.get(kind)
        if length is None or i + length > len(data):
            break

        record = data[i:i + length]
        # Byte 1 is unknown and ignored; coordinates start at byte 2.
        lat, lon = struct.unpack(">ff", record[2:10])
        out.append(Poi(
            kind=POI_KINDS[kind],
            latitude=lat,
            longitude=lon,
            angle=struct.unpack(">H", record[10:12])[0] if kind in (1, 2) else None,
            speed_limit=record[12] if kind == 1 else None,
            raw=record.hex(),
        ))
        i += length

    return out
