"""r8link, read live data off a Uniden R8w radar detector over Bluetooth LE.

    from r8link import R8W

    async with R8W("E0:00:00:00:23:D4") as r8:
        async for _ in r8.updates():
            print(r8.telemetry.voltage, r8.alerts)

Not affiliated with or endorsed by Uniden. See PROTOCOL.md for how any of
this was worked out, and for what parts of it are guesses.
"""

from .client import (
    ALERT_UUID,
    Command,
    DeviceInfo,
    FIRMWARE_UUID,
    POI_UUID,
    R8W,
    READ_CMD_UUID,
    SETTINGS_1_UUID,
    SETTINGS_2_UUID,
    SOFTWARE_UUID,
    TELEMETRY_UUID,
    Update,
    WRITE_CMD_UUID,
)
from .errors import NotPairedError, R8Error, WritesDisabledError
from .protocol import (
    Alert,
    LASER_GUNS,
    MUTE_STATUS,
    Poi,
    PoiAlert,
    Telemetry,
    parse_alerts,
    parse_poi,
    parse_telemetry,
)

__version__ = "0.1.0"

__all__ = [
    "R8W", "Update", "Command", "DeviceInfo",
    "Telemetry", "Alert", "Poi", "PoiAlert",
    "parse_telemetry", "parse_alerts", "parse_poi",
    "LASER_GUNS", "MUTE_STATUS",
    "R8Error", "NotPairedError", "WritesDisabledError",
    "TELEMETRY_UUID", "ALERT_UUID", "POI_UUID",
    "SETTINGS_1_UUID", "SETTINGS_2_UUID",
    "WRITE_CMD_UUID", "READ_CMD_UUID",
    "FIRMWARE_UUID", "SOFTWARE_UUID",
    "__version__",
]