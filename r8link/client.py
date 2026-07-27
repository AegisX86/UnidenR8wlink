"""
The BLE client. This is a thin wrapper over bleak: connect, subscribe,
hand you parsed packets. All the actual understanding lives in protocol.py.

Async only

Linux only, realistically. It is developed and used on a Raspberry Pi 4
with BlueZ. macOS will not work as-is because CoreBluetooth refuses to
hand out MAC addresses (you get a system-assigned UUID instead), so the
addresses in the examples are meaningless there. Good luck, open a PR.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import AsyncIterator

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDeviceNotFoundError

from .errors import NotPairedError, R8Error, WritesDisabledError
from .protocol import Alert, Poi, Telemetry, parse_alerts, parse_poi, parse_telemetry

# --------------------------------------------------------------------------
# Characteristic UUIDs. Exported because you will want them for poking at
# the undecoded ones with read_raw().
# --------------------------------------------------------------------------

TELEMETRY_UUID = "6c290d2e-1c03-aca1-ab48-a9b908bae79e"
ALERT_UUID     = "6eb675ab-8bd1-1b9a-7444-621e52ec6823"
POI_UUID       = "15005991-b131-3396-014c-664c9867b917"
SETTINGS_1_UUID = "2d86686a-53dc-25b3-0c4a-f0e10c8dee20"
SETTINGS_2_UUID = "5a87b4ef-3bfa-76a8-e642-92933c31434f"
WRITE_CMD_UUID = "2c86686a-53dc-25b3-0c4a-f0e10c8dee20"
READ_CMD_UUID  = "5987b4ef-3bfa-76a8-e642-92933c31434f"
FIRMWARE_UUID  = "00002a26-0000-1000-8000-00805f9b34fb"
SOFTWARE_UUID  = "00002a28-0000-1000-8000-00805f9b34fb"


class Command:
    """Write commands, straight out of the app's Constant.java.

    UNTESTED. Every one of these. I only ever implemented reading, and I
    am not debugging a bricked $900 radar detector. They are here because
    leaving them out would be worse documentation, not because I vouch
    for them.
    """
    MUTE = "BTreqMUTE:1"
    UNMUTE = "BTreqMUTE:0"
    ADD_MUTE_MEMORY = "BTreqMMEM:1"
    DELETE_MUTE_MEMORY = "BTreqMMEM:0"
    ADD_USER_MARK = "BTreqUMRK:1"
    DELETE_USER_MARK = "BTreqUMRK:0"
    DELETE_RED_LIGHT_CAMERA = "BTreqRLCD:0"


@dataclass
class DeviceInfo:
    firmware: str | None = None
    software: str | None = None


@dataclass
class Update:
    """Something arrived. `kind` is "telemetry" or "alerts"; the matching
    field is filled in and the other is None. The client's .telemetry and
    .alerts properties always hold the latest of both, so a redraw loop
    can ignore the payload entirely and just use the tick."""
    kind: str
    telemetry: Telemetry | None = None
    alerts: list[Alert] | None = None


class R8W:
    """A connection to one detector.

        async with R8W("E0:00:00:00:23:D4") as r8:
            async for update in r8.updates():
                print(r8.telemetry.voltage, r8.alerts)

    Notifications are pushed onto a bounded queue and handed to you from
    there, rather than calling you back directly from bleak's notification
    path. That way a slow consumer stalls itself instead of the BLE stack.
    If you fall far enough behind, the oldest updates get dropped; for
    live radar data stale packets are worthless anyway.

    There is no automatic reconnect. The link WILL drop, and what you want
    to do about it depends on your program, so updates() simply returns
    when the link goes away and you wrap the whole thing in a retry loop.
    See examples/monitor.py, it is four lines.
    """

    def __init__(
        self,
        address: str | BLEDevice,
        *,
        timeout: float = 20.0,
        allow_writes: bool = False,
        queue_size: int = 64,
    ):
        self.address = address
        self.allow_writes = allow_writes
        self._timeout = timeout
        self._client: BleakClient | None = None
        self._queue: asyncio.Queue[Update | None] = asyncio.Queue(maxsize=queue_size)
        self._telemetry: Telemetry | None = None
        self._alerts: list[Alert] = []
        self._telemetry_at: float | None = None

    # ---------------------------------------------------------------- state

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def telemetry(self) -> Telemetry | None:
        """Most recent telemetry packet, or None if nothing has arrived."""
        return self._telemetry

    @property
    def alerts(self) -> list[Alert]:
        """Currently active detections. Empty list means all clear."""
        return self._alerts

    @property
    def telemetry_age(self) -> float | None:
        """Seconds since the last telemetry packet, or None if there has
        never been one. Telemetry arrives every 1-2 seconds, so anything
        over ~5 means the link is sick even if it claims to be up. A frozen
        display is worse than a blank one when you are moving."""
        if self._telemetry_at is None:
            return None
        return time.monotonic() - self._telemetry_at

    @property
    def mtu(self) -> int | None:
        """Negotiated ATT MTU, or None if we cannot actually tell.

        Do not trust this on BlueZ. BlueZ does not expose the negotiated
        MTU over D-Bus until the notify channel has been acquired, so bleak
        hands back the 23-byte ATT default as a placeholder and warns about
        it on stderr. 23 is therefore indistinguishable from "no idea", and
        this returns None for it rather than a number that looks like a
        measurement and is not.

        You can tell it is a placeholder (larp) by looking at the packets. A real
        23-byte MTU caps notification payloads at 20 bytes, and telemetry
        packets are routinely 25 or more and arrive whole.

        Still worth checking if you port this somewhere that reports it
        honestly. The longest alert packets are ~55 bytes, so a stack that
        does not negotiate upward will truncate them mid-field.
        """
        if self._client is None:
            return None
        value = getattr(self._client, "mtu_size", None)
        if value is None or value <= 23:
            return None
        return value

    # ------------------------------------------------------------ discovery

    @staticmethod
    async def discover(timeout: float = 8.0) -> list[BLEDevice]:
        """Scan for detectors. They advertise as `R8W@xx`.

        An unpaired R8w does NOT advertise until you put it into pairing
        mode (MENU -> BT Pairing -> MENU), and a paired one stops
        advertising the moment anything connects to it. An empty list here
        is usually one of those two, not a broken scan.
        """
        devices = await BleakScanner.discover(timeout=timeout)
        return [d for d in devices if (d.name or "").upper().startswith("R8W")]

    # ----------------------------------------------------------- connection

    async def connect(self) -> None:
        self._client = BleakClient(
            self.address,
            timeout=self._timeout,
            disconnected_callback=self._on_disconnect,
        )

        try:
            await self._client.connect()
        except BleakDeviceNotFoundError as exc:
            # BlueZ could not resolve the address to a D-Bus path at all. The
            # detector stops advertising the moment anything connects to it,
            # so a link BlueZ is still holding makes the device vanish from
            # Python while `bluetoothctl info` cheerfully lists it.
            raise R8Error(
                f"BlueZ has no visible device at {self.address}.\n"
                f"\n"
                f"Usually something else is holding the link, and most often that\n"
                f"something is BlueZ itself, right after you paired:\n"
                f"\n"
                f"    bluetoothctl disconnect {self.address}\n"
                f"\n"
                f"The detector stops advertising while connected, so it disappears\n"
                f"from Python's point of view even though `bluetoothctl info\n"
                f"{self.address}` will happily tell you all about it.\n"
                f"\n"
                f"If that is not it: check the detector is powered on, check it is\n"
                f"not connected to your phone, and check the address is right."
            ) from exc
        except asyncio.TimeoutError as exc:
            # Different failure. BlueZ knows the device, the connect request
            # went out over D-Bus, and nothing ever came back.
            raise R8Error(
                f"Timed out connecting to {self.address} after {self._timeout:.0f}s.\n"
                f"\n"
                f"BlueZ knows about the device, so this is not a pairing problem.\n"
                f"Something is stopping it from answering. In rough order:\n"
                f"\n"
                f"    1. bluetoothctl disconnect {self.address}\n"
                f"       (a half-open link from a Ctrl-C'd script does this)\n"
                f"    2. Turn Bluetooth off on your phone. It auto-reconnects to\n"
                f"       paired devices without asking, and the detector will only\n"
                f"       talk to one thing at a time.\n"
                f"    3. Power-cycle the detector. Clears link state on its side,\n"
                f"       which nothing on this machine can reach.\n"
                f"    4. sudo systemctl restart bluetooth\n"
                f"    5. Reboot. This has fixed it and I do not know why.\n"
                f"\n"
                f"If none of that works, `sudo btmon` in another shell while you\n"
                f"reproduce is the only place the real failure is visible."
            ) from exc

        # Probe read. This doubles as the pairing check: an unpaired R8w
        # lets you connect and then either errors on the read or hands
        # back nothing at all, which is exactly why the failure is so
        # confusing the first time you meet it.
        try:
            payload = await self._client.read_gatt_char(TELEMETRY_UUID)
        except Exception as exc:
            await self._quiet_disconnect()
            raise NotPairedError(str(self.address), exc) from exc
        if not payload:
            await self._quiet_disconnect()
            raise NotPairedError(str(self.address))

        self._store_telemetry(parse_telemetry(payload))

        # Nice to have, not worth failing over: notifications will fill it
        # in within a second or two anyway.
        try:
            self._alerts = parse_alerts(await self._client.read_gatt_char(ALERT_UUID))
        except Exception:
            pass

        await self._client.start_notify(TELEMETRY_UUID, self._on_telemetry)
        await self._client.start_notify(ALERT_UUID, self._on_alert)

    async def disconnect(self) -> None:
        await self._quiet_disconnect()
        self._push(None)

    async def __aenter__(self) -> "R8W":
        await self.connect()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.disconnect()

    async def _quiet_disconnect(self) -> None:
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                pass  # already gone, which is where we wanted it

    # -------------------------------------------------------------- streaming

    async def updates(self) -> AsyncIterator[Update]:
        """Yield updates until the link drops, then return."""
        while True:
            item = await self._queue.get()
            if item is None:
                return
            yield item

    def _push(self, item: Update | None) -> None:
        """Queue an update, dropping the oldest if the consumer is behind."""
        while True:
            try:
                self._queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                try:
                    self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    return

    def _store_telemetry(self, telemetry: Telemetry) -> None:
        self._telemetry = telemetry
        self._telemetry_at = time.monotonic()

    # bleak calls these from the event loop. They must not block and must
    # not raise: an exception here disappears into the BLE machinery and
    # takes the notification with it.
    def _on_telemetry(self, _sender, data: bytearray) -> None:
        try:
            telemetry = parse_telemetry(data)
        except Exception:
            return
        self._store_telemetry(telemetry)
        self._push(Update("telemetry", telemetry=telemetry))

    def _on_alert(self, _sender, data: bytearray) -> None:
        try:
            alerts = parse_alerts(data)
        except Exception:
            return
        self._alerts = alerts
        self._push(Update("alerts", alerts=alerts))

    def _on_disconnect(self, _client) -> None:
        self._alerts = []
        self._push(None)  # wakes updates() so it can return

    # ------------------------------------------------------------ one-shots

    async def read_telemetry(self) -> Telemetry:
        return parse_telemetry(await self.read_raw(TELEMETRY_UUID))

    async def read_alerts(self) -> list[Alert]:
        return parse_alerts(await self.read_raw(ALERT_UUID))

    async def read_poi(self) -> list[Poi]:
        """The stored camera and user-mark database, with coordinates.
        Read once after connecting; this characteristic does not notify."""
        return parse_poi(await self.read_raw(POI_UUID))

    async def read_device_info(self) -> DeviceInfo:
        info = DeviceInfo()
        for uuid, name in ((FIRMWARE_UUID, "firmware"), (SOFTWARE_UUID, "software")):
            try:
                raw = await self.read_raw(uuid)
                setattr(info, name, raw.decode("utf-8", "replace").strip("\x00").strip())
            except Exception:
                pass  # not every firmware (?) exposes both
        return info

    async def read_raw(self, uuid: str) -> bytes:
        """Escape hatch. The settings characteristics are readable and I
        never worked out what is in them, so this is how you go looking."""
        if self._client is None or not self._client.is_connected:
            raise RuntimeError("not connected")
        return bytes(await self._client.read_gatt_char(uuid))

    # ------------------------------------------------------------- writing

    async def send_command(self, command: str) -> None:
        """Send one of the Command constants.

        Gated behind allow_writes=True because none of these have ever been
        tested against hardware. If you turn it on and something goes
        sideways, that is between you and Uniden.
        """
        if not self.allow_writes:
            raise WritesDisabledError(
                f"Refusing to send {command!r}: write commands are decompiled "
                f"but untested. Pass allow_writes=True if you accept that."
            )
        if self._client is None or not self._client.is_connected:
            raise RuntimeError("not connected")
        await self._client.write_gatt_char(WRITE_CMD_UUID, command.encode(), response=False)
