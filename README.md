# r8link

A Python library for reading a Uniden R8w radar detector over Bluetooth
LE. Alerts, band, frequency, signal strength, direction, voltage, heading,
speed, and the stored camera database, as dataclasses, in about ten lines
of your own code.

Not affiliated with or endorsed by Uniden.

```python
from r8link import R8W

async with R8W("E0:00:00:00:23:D4") as r8:
    async for update in r8.updates():
        if update.kind == "alerts":
            for alert in update.alerts:
                print(alert)     # KA 33.785 GHz 5/8 front
```

There is a full protocol writeup in
[PROTOCOL.md](https://github.com/AegisX86/UnidenR8wlink/blob/main/PROTOCOL.md):
packet formats, what every field means, and which parts of it I am
guessing about.

## Where this has been

Woah, it has been a while since I touched this.

I started the reverse engineering in December, finished it on vacation in
January, and then it sat on `git.wired/` (my internal research network)
for seven months doing nothing at all. It "worked", I used it, I never
published it. It is on GitHub now because code rotting on a box in my
house helps nobody.

Which means the install instructions below are freshly re-verified rather
than remembered. I set the whole thing up again from scratch on a clean Pi
to check they were still true. Two things had screwed me back in January
that I had completely forgotten about, and both of them screwed me again in
July. They are written down properly now, in the pairing section. There is
also an `r8link-pair` command that does the whole setup for you, since
writing the warnings down was evidently not enough to make me follow them.

## Requirements

- Linux with BlueZ.
- Python 3.10+
- `bleak` (installed for you)
- A Uniden R8w, obviously

What I actually tested, in case shit goes sideways and you want to
know how far you are from a known-good setup: a Raspberry Pi 4 on its
onboard Bluetooth, Debian Trixie (Raspberry Pi OS), Python 3.13, BlueZ
5.82, bleak 3.0.2.

Other Pi models, other Debians, other distros, a normal desktop with a
working BLE adapter: those should all be fine. Nothing in here is
Pi-specific. But "should be fine" is me guessing rather than me having
run it, so if yours does something interesting, open an issue and
I will look into it.

macOS is the one I would actually expect to break. CoreBluetooth does not
hand out MAC addresses (you get a system-assigned UUID instead), so every
address in these examples is meaningless there and you would need to find
the device by name. Never tried it. PRs welcome.

## Installing

```
python3 -m venv .venv
source .venv/bin/activate
pip install r8link
```

The venv is not optional on anything recent. Debian marks the system
Python as externally managed and `pip install` fails with
`error: externally-managed-environment` before it gets anywhere near this
code.

That gives you the library and the `r8link-pair` command. The examples
live in the repo rather than the package; if you want those, or you want
to edit the library, install from source instead.

### From source

```
git clone https://github.com/AegisX86/UnidenR8wlink
cd UnidenR8wlink
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

`-e` so you can edit `r8link/` without reinstalling, and the `dev` extra
pulls in pytest. Run the tests before you try and pair a R8w:

```
pytest
```

If they pass, the package imports and the packet formats are intact,
and anything that goes wrong from here is Bluetooth's fault
rather than mine.

## Pairing

The R8w requires a system-level Bluetooth pairing before it will respond
any GATT request. Without it you connect successfully, you enumerate all
the characteristics, and then every read comes back empty or with an
authentication complaint. Phones hide this from you, which is why nRF
Connect works out of the box and your Python script does not.

There is a command for it:

```
r8link-pair
```

It checks the things that would otherwise waste your evening (rfkill,
group membership, bluetoothd), walks you through pairing mode, retries the
pair, lets go of the link afterwards, and then connects with the library
and reads real telemetry so you know it actually worked rather than just
that it paired. If you already know the address:

```
r8link-pair E0:00:00:00:23:D4
```

That form also works as a health check on an already-paired unit. Say no
when it offers to re-pair and it will just connect and read.

Do not run it with sudo. It will ask you to run the one command that
needs root, if it needs it at all.

### Manual pairing

If you would rather not run my script and diy it, here's the guide:

Bluetooth may be soft blocked. Most Raspberry Pi OS images ship it that
way, and `systemd-rfkill` will restore that state for you persistantly.

```
rfkill list bluetooth
sudo rfkill unblock bluetooth
```

`hciconfig -a` saying `DOWN` when the hardware is clearly present is the
symptom. So is `Failed to set mode: Failed (0x03)` in
`journalctl -u bluetooth`.

You also need to be in the `bluetooth` group. BlueZ's D-Bus policy grants
pairing and agent operations to that group. Without it, `power on` works
fine and then `agent on` or `pair` fails with `AccessDenied` or a bare
"Rejected send message", which looks nothing whatsoever like a permissions
problem.

```
sudo usermod -aG bluetooth $USER
```

Then log out and back in. Group membership only applies to new sessions,
so if `groups` does not list `bluetooth`, you are still in the old one.

Now the pairing itself. On the detector:

```
MENU -> BT Pairing -> MENU        display shows "Pairing R8W@.."
```

**An unpaired R8w does not advertise at all until you do this.** It will
not turn up in a scan, in `bluetoothctl`, in nRF Connect, or anywhere
else. If you are scanning and finding nothing, that is almost always why.
The window closes on its own after a while, so keep the detector within
reach. You will probably need to re-arm it at least once.

Then on the machine. Do this in one interactive session. The pairing agent
has to stay alive across the pair, and `bluetoothctl pair ...` as a
one-shot from your shell exits the moment it has sent the command, taking
the agent with it. BlueZ calls that `AuthenticationCanceled`, which reads
as though the detector rejected you:

```
bluetoothctl
power on
agent on                         # 5.82 registers one at startup already,
default-agent                    # both of these are no-ops now. harmless.
scan on                          # wait for R8W@.. to appear, note the address
scan off                         # single-radio adapters cannot scan and connect at once
pair E0:00:00:00:23:D4
```

**If the first `pair` fails with `AuthenticationFailed`, run the exact same
command again.** Mine failed and then succeeded immediately on an identical
second attempt, twice, months apart. I have no explanation. Do not start
taking things apart until you have tried it twice.

You will know it worked when bluetoothctl dumps the entire GATT tree at
you and says `Pairing successful`. The Bluetooth icon on the detector's
display is the other confirmation.

### Then disconnect

```
disconnect E0:00:00:00:23:D4
exit
```

BlueZ is holding the link. Until you let go, your own code cannot have it,
and because the detector stops advertising the moment something is
connected to it, the failure is `BleakDeviceNotFoundError`: a device you
can see in `bluetoothctl info` and that Python swears does not exist.

Pairing is persistent, you only do this once. If it goes wrong later,
"forget" the device on both sides and redo it.

## Running the examples

These live in the repo, not the package. Clone it, or grab them from
[GitHub](https://github.com/AegisX86/UnidenR8wlink/tree/main/examples).

```
python examples/minimal.py [address]     # print alerts as they arrive
python examples/monitor.py [address]     # live terminal
```

Note that `minimal.py` only prints when an alert arrives, so on a desk with
no radar around it connects, prints the voltage, and then sits there
looking broken. It is not broken.

## Using it

```python
from r8link import R8W, Command

# find one (needs to be in pairing mode, or already paired and idle)
devices = await R8W.discover()

async with R8W("E0:00:00:00:23:D4") as r8:
    r8.telemetry          # latest Telemetry, updates every 1-2s
    r8.alerts             # list[Alert], empty means clear
    r8.telemetry_age      # seconds since the last packet
    r8.is_connected

    await r8.read_poi()           # stored cameras and user marks, with coords
    await r8.read_device_info()   # firmware / software version
    await r8.read_raw(uuid)       # for poking at the undecoded characteristics

    async for update in r8.updates():
        ...               # update.kind is "telemetry" or "alerts"
```

`Alert` gives you `band`, `strength` (1-8), `frequency_ghz`, `laser_gun`,
`direction_name`, `mute_status`, `is_muted`, and `description`, which is
the one you usually want to print: a frequency for radar, a gun name for
laser, "Gatso" for the photo radar bands.

`Telemetry` gives you `voltage`, `heading`, `speed`, `altitude`,
`gps_locked`, `scan_count`, and `poi` when the detector is warning you
about a stored camera.

Everything keeps a `raw` field with the original packet, and nothing in the
parser raises on input it does not recognise. An unknown laser ID comes
back as `"unknown laser (23)"`, a short packet leaves the missing fields as
`None`. Crashing a program that is running on a dashboard because Uniden
broke something in a firmware update is not an acceptable failure mode.

### Reconnecting

The client does not reconnect for you. `updates()` returns when the link
drops and you decide what happens next:

```python
while True:
    try:
        async with R8W(ADDRESS) as r8:
            async for _ in r8.updates():
                draw(r8)
    except Exception as exc:
        print(exc)
    await asyncio.sleep(3)
```

Catch `KeyboardInterrupt` around `asyncio.run` while you are at it. Ctrl-C
otherwise skips the context manager's exit and leaves the detector holding
a half-open link that the next run cannot get past.

### Writing commands

Mute, user marks and so on exist in the protocol. I have never sent one to
real hardware, so they are behind a flag:

```python
async with R8W(ADDRESS, allow_writes=True) as r8:
    await r8.send_command(Command.MUTE)
```

Without `allow_writes=True` you get a `WritesDisabledError`. If you brick a
$900 radar detector somehow while playing with this, that's not my problem.

Reading mute state needs none of this. The detector reports it in the alert
packet, so pressing mute on the unit itself shows up in `alert.is_muted`
immediately.

## Status / honesty section

This was reverse engineered from the decompiled R/Tach Android app (JADX)
plus live BLE captures from one detector. I am not a reverse engineer, this
is outside my field, I am a computer engineering major who pointed a
decompiler at an APK and stared at Java until it made sense. Everything
documented here matches what my unit actually sends. That is a sample size
of one, and I probably got something wrong somewhere.

- **One device.** One R8w, on one firmware version. It cost $900 and I am
  not buying a second one to test edge cases.
- **One setup.** Pi 4, Trixie, BlueZ 5.82, bleak 3.0.2. Other Linux should
  be fine.
- **Read-only in practice.** Writes are implemented but untested, see
  above.
- **No idea about other models.** R4W, anything else: the UUIDs and
  formats could be completely different. The decompiled parser has an
  `isI9` flag suggesting shared infrastructure across models, which I
  cannot verify.
- **Some fields are guesses.** The two settings characteristics are mostly
  undecoded, `scan_count` does not appear to be a count, and alert field 1
  reads `00` in every capture I have. All mentioned in PROTOCOL.md.
- **The laser path has never seen a real packet.** Nor have the Gatso
  bands, nor mute codes 3 through 6, nor a populated POI database. Those
  are implemented from the decompiled source and tested against synthetic
  input only.

If your unit sends something this parser gets wrong, open an issue with the
raw packet, that field is on every model for exactly this reason. More data
points make the reference better.

## Code tour

- `r8link/protocol.py` - all the parsing. Pure functions, no bleak, no
  I/O. Every packet format lives here and can be tested from a string.
- `r8link/client.py` - the bleak wrapper. Connect, subscribe, queue, hand
  you parsed packets. UUIDs and the write command constants.
- `r8link/pair.py` - the setup dance, automated, with a read at the end to
  prove it worked. This is `r8link-pair`.
- `r8link/errors.py` - three exceptions, one of which is a four-page
  apology about pairing.
- `r8link/__init__.py` - the public surface.
- `examples/` - `minimal.py` and the dashboard. Repo only.
- `tests/test_protocol.py` - parser tests against captured packets, no
  hardware needed.

## Credits

- Written by Aigis (P.R_Aigis), 2026
- Built on [bleak](https://github.com/hbldh/bleak)
- Protocol worked out with [JADX](https://github.com/skylot/jadx), btmon, and nRF connect
- MIT licensed, see LICENSE