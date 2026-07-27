"""Exceptions. There are only three, and one of them exists purely to save
you the four hours it cost me."""

from __future__ import annotations


class R8Error(Exception):
    """Base class for everything this library raises."""


class NotPairedError(R8Error):
    """The BLE link came up but the detector will not answer GATT requests.

    This is the failure that wasted a lot of my time, because nothing 
    about it looks like an error: you connect fine, the characteristics
    are all there, and reads come back empty or with an authentication 
    complaint.The R8w wants a system-level Bluetooth pairing first.
    Phones do that invisibly. BlueZ does not.
    """

    def __init__(self, address: str, cause: BaseException | None = None):
        detail = f"\n\nIf you ARE already paired, the underlying error was:\n    {cause!r}" if cause else ""
        super().__init__(
            f"Connected to {address}, but it will not answer GATT requests.\n"
            f"Nine times out of ten that means the device is not paired yet.\n"
            f"\n"
            f"On the detector:\n"
            f"    MENU -> BT Pairing -> MENU        (display shows 'Pairing R8W@..')\n"
            f"\n"
            f"Then on this machine:\n"
            f"    bluetoothctl\n"
            f"    power on\n"
            f"    agent on\n"
            f"    default-agent\n"
            f"    scan on                           # wait for R8W@.. to appear\n"
            f"    pair {address}\n"
            f"    disconnect {address}              # let go so Python can connect\n"
            f"    exit\n"
            f"\n"
            f"Pairing is persistent, you only do this once. The Bluetooth icon\n"
            f"on the detector's display is how you know it took."
            f"{detail}"
        )
        self.address = address
        self.cause = cause


class WritesDisabledError(R8Error):
    """You called send_command() without allow_writes=True.

    The write commands are lifted from the decompiled app and I have never
    sent a single one to real hardware. The flag is there so you have to
    say out loud that you know that.
    """
