# Uniden R8w Bluetooth protocol

How the R8w talks over BLE: characteristics, packet formats, and what
every field means. Worked out by decompiling the R/Tach Android app
(`com.uniden.rtach`) with JADX and capturing live traffic from a real
unit.

Not affiliated with or endorsed by Uniden. This is a reverse engineering
writeup, not official documentation.

If you just want to read data, use the library and skip this: see
[README.md](https://github.com/AegisX86/UnidenR8wlink/blob/main/README.md).

## Disclaimer

Your mileage may vary. This was written using a single R8w on a single
firmware version. I only have one of these things and I am not buying
another one to test edge cases. It is entirely possible I have missed or
misinterpreted something. That said, based on my testing, everything below
is accurate for my unit.

Yours may differ in MAC address (obviously), firmware behaviour, and
possibly characteristic UUIDs, though those are probably consistent across
R8 units. I have no idea whether any of this applies to the R4W or
anything else Uniden sells. The decompiled parser has an `isI9` flag in
it, which suggests shared code across models, but I cannot verify that.

Where something below says "confirmed", it means I have seen it on
hardware in a capture. Everything else came out of the decompiled source
and is only as good as my reading of it.

Contributions welcome. If you have a different model, or a packet this
gets wrong, open an issue with the raw bytes.

The unit these captures come from reports firmware
`R8W/NA/NA/NA/NA/20251002/NA/NA` and software
`R8W/127/109/122/103/20251002/999/999/120`.

---

## Connection

### Address type

The detector advertises with a random static address. Mine is
`E0:00:00:00:23:D4`, and the top two bits being set is what makes it
static rather than resolvable-private. It does not change across power
cycles, so hardcoding it in a config file is safe. It is not a
manufacturer-assigned MAC, so do not go looking up the OUI.

### Advertising

The R8w advertises in exactly two situations: when it is unpaired and in
pairing mode (`MENU -> BT Pairing -> MENU`), or when it is paired and not
currently connected to anything.

An unpaired R8w sitting idle does not advertise at all. It will not appear
in a scan, in `bluetoothctl`, or in nRF Connect, and there is no
indication anywhere that this is deliberate. If you are scanning and
finding nothing, that is almost always why. Confirmed the hard way.

It also stops advertising the instant anything connects to it, including
BlueZ itself and including your phone in the background, which produces
the same empty scan for a completely different reason.

### Pairing is mandatory

The R8w requires a system-level Bluetooth pairing before it will accept
GATT operations. Without it the BLE connection establishes normally, the
service and characteristic enumeration works, and every read returns empty
or fails. Nothing tells you why.

BlueZ automatically tries to subscribe to the Service Changed
characteristic (`0x2a05`) after connecting, which requires authentication.
Unpaired, the R8w answers "Insufficient Authentication", BlueZ then
attempts to pair, the detector is not in pairing mode so it rejects that
with "Repeated attempts", and BlueZ drops the link. The btmon trace is the
only place any of this is visible.

Phone apps work because iOS and Android handle pairing transparently
during connection. BlueZ does not.

To pair: `MENU -> BT Pairing -> MENU` on the detector, then `bluetoothctl`,
`power on`, `agent on`, `default-agent`, `scan on`, `pair <address>`. Then
`disconnect <address>` so BlueZ lets go and your own code can take the
link. Pairing is persistent. The Bluetooth icon on the display means it
worked.

Two failure modes worth knowing about, both confirmed:

- **The pairing agent must outlive the pair.** `bluetoothctl pair <addr>`
  as a one-shot registers an agent, fires the request, and exits, taking
  the agent with it. BlueZ reports
  `org.bluez.Error.AuthenticationCanceled`, which reads as though the
  detector rejected you. Use an interactive session.
- **The first attempt often fails with `AuthenticationFailed`.** An
  identical immediate retry succeeds. Seen twice, months apart, on two
  separate re-pairings. No explanation.

### MTU

The longest alert packets run about 55 bytes. The default BLE ATT MTU only
carries 20 bytes of payload, so something has to negotiate upward.

BlueZ does this for us and I have never seen a truncated packet, but you
cannot easily verify it from Python. BlueZ does not expose the negotiated
MTU over D-Bus until the notify channel has been acquired, so bleak
reports the 23-byte default as a placeholder and warns about it. The
evidence that it really did negotiate up is in the packets: a 25-byte
telemetry notification arriving whole is impossible under a 23-byte MTU.

If you port this somewhere else and see packets cut off mid-field, that is
the first thing to check.

---

## Services and characteristics

Two custom services, plus the standard Device Information service.

```
18424398-7cbc-11e9-8f9e-2a86e4085a59    Uniden data service
1842467c-7cbc-11e9-8f9e-2a86e4085a59    Uniden command service
0000180a-0000-1000-8000-00805f9b34fb    Device information (standard)
```

| UUID | What | Properties |
|------|------|------------|
| `6c290d2e-1c03-aca1-ab48-a9b908bae79e` | Telemetry ("ETC data") | Read, Write w/o response, Notify |
| `6eb675ab-8bd1-1b9a-7444-621e52ec6823` | Alerts | Read, Write w/o response, Notify |
| `15005991-b131-3396-014c-664c9867b917` | POI database | Read, Write w/o response, Notify |
| `2d86686a-53dc-25b3-0c4a-f0e10c8dee20` | Settings 1 | Read, Write w/o response, Notify |
| `5a87b4ef-3bfa-76a8-e642-92933c31434f` | Settings 2 | Read, Write w/o response, Notify |
| `2c86686a-53dc-25b3-0c4a-f0e10c8dee20` | Write command | Write w/o response |
| `5987b4ef-3bfa-76a8-e642-92933c31434f` | Command response | Read, Notify |
| `00002a26-0000-1000-8000-00805f9b34fb` | Firmware version | Read |
| `00002a28-0000-1000-8000-00805f9b34fb` | Software version | Read |

The Device Information service also carries Manufacturer Name String
(`0x2a29`) and Model Number String (`0x2a24`), which I have never bothered
reading.

Every vendor characteristic has a Characteristic User Description
descriptor (`0x2901`) attached. Those often contain a human-readable name
and might well identify the two settings characteristics outright. I have
not read them. If you do, please tell me what they say.

### Version strings

Both are slash-delimited, and neither is documented anywhere I could find:

```
firmware  R8W/NA/NA/NA/NA/20251002/NA/NA
software  R8W/127/109/122/103/20251002/999/999/120
```

The `20251002` in both is presumably a build date. The rest is anyone's
guess, and `999` looks like a placeholder.

### Notification behaviour

- **Telemetry**: notifies every 1-2 seconds, whether anything changed or
  not.
- **Alerts**: notifies when the detection state changes.
- **POI and settings**: never notify. Read on demand.

"Changes" is looser than it sounds for alerts. Field 4 (`raw_signal`)
moves constantly on a live signal, and each move counts as a change, so a
weak steady detection produces several packets a second. Worth knowing if
you are redrawing a display on every one.

Alert packets are full snapshots, not deltas. Every packet describes the
complete current state.

---

## Telemetry ("ETC data")

`6c290d2e-1c03-aca1-ab48-a9b908bae79e`

UTF-8 text, `&` between fields, `,` inside grouped fields.

```
voltage & poi & gps & warning & scanCount & wifiStatus & brightnessStatus
```

Seven fields. My original notes described nine, with the GPS group
flattened out; that was wrong. The authoritative order is the one in the
app's `parseETCData` switch statement, which assigns cases 0 through 6 to
the fields below.

| # | Field | Format | Meaning |
|---|-------|--------|---------|
| 0 | voltage | `12.1` | Battery voltage |
| 1 | poi | `0` or `type,distance,limit` | Active POI warning |
| 2 | gps | `0` or `dir,speed,alt,status` | GPS group |
| 3 | warning | `0` | Warning flag |
| 4 | scanCount | `12` | See below, not a count |
| 5 | wifiStatus | `D` | Radar WiFi status |
| 6 | brightnessStatus | `D` | Auto brightness status |

GPS sub-fields:

| # | Field | Values |
|---|-------|--------|
| 0 | direction | `N`, `NE`, `E`, `SE`, `S`, `SW`, `W`, `NW` |
| 1 | speed | mph |
| 2 | altitude | feet |
| 3 | status | `C` = connected |

Captured examples:

| Packet | Meaning |
|--------|---------|
| `12.1&0&W,0,193,C&0&12&D&D` | 12.1V, no POI, heading west, stopped, 193 ft, GPS locked |
| `12.2&0&S,0,2634,C&0&4&D&D` | parked, engine off, 2634 ft |
| `14.1&0&SW,0,203,C&0&13&D&B` | 14.1V (engine running), heading southwest |
| `11.8&SPEEDCAM,500,35&N,45,312,C&0&5&D&D` | Speed camera ahead, 35 limit, doing 45 north |

The POI distance units are unconfirmed. Feet would be my guess from the
values I have seen, but I have not measured it.

**`scanCount` is not a count.** The app's field name says it is and the
data says otherwise. Consecutive packets from a stationary detector read
4, 2, 4, 5, 4, so it goes down as often as up. Position in a sweep cycle,
maybe, or a number of segments currently active. Whatever it is, do not
treat it as monotonic.

Heading is noisy at a standstill. A parked detector flips between adjacent
compass points every few packets. That is GPS, not the protocol.

**The R8w does not send your coordinates.** Heading, speed and altitude
only. The phone app gets position from the phone's GPS. I spent a while
convinced I was missing a characteristic. I was not. If you want position
on a Pi, that is your own GPS module.

---

## Alerts

`6eb675ab-8bd1-1b9a-7444-621e52ec6823`

UTF-8 text. Segments separated by `&`, one per simultaneous detection,
each either `0` (empty slot) or a comma-separated field list.

```
segment & segment & segment & segment
```

Every packet I captured had four segments, and the display tracks up to
four alerts, but the app's parser does not cap the count and my older
notes say five. Do not hard-code it.

| # | Field | Example | Meaning |
|---|-------|---------|---------|
| 0 | flag | `1` | Alert active |
| 1 | alert_id | `00` | Internal identifier |
| 2 | band | `KA` | See band table |
| 3 | strength | `3` | Display bars, 1-8 |
| 4 | raw_signal | `33` | Finer-grained strength |
| 5 | frequency | `33.7850` | GHz, or laser gun ID |
| 6 | direction | `R` | `F` front, `S` side, `R` rear |
| 7 | mute | `1` | See mute table |
| 8 | rcv_mode | `0` | Receive mode status |

Captured examples:

| Packet | Meaning |
|--------|---------|
| `0&0&0&0` | Clear |
| `1,00,KA,3,33,33.7850,R,1&0&0&0` | Ka, 3/8, 33.785 GHz, rear |
| `1,00,KA,1,40,35.2000,F,1&0&0&0` | Ka, 1/8, 35.2 GHz, front, unmuted |
| `1,00,KA,1,31,35.2000,F,2&0&0&0` | same source, mute pressed on the unit |
| `1,00,K,4,45,24.1500,F,1&0&0&0` | K, 4/8, 24.15 GHz, front |
| `1,00,K,3,25,24.1500,F,1&1,00,KA,6,55,35.5000,R,1&0&0` | K front and Ka rear at once |
| `1,00,LASER,8,0,5,F,1&0&0&0` | Kustom laser, max strength, front (synthetic) |

**The field names in the decompiled source lie.** `parseAlertBandData`
calls field 4 `raw_value` and field 5 `info`. Field 5 is the frequency. I
lost a good while trying to work out why my "frequency" was reading 33,
45, 67, 76, 26 and changing with signal strength. When the data does not
match the names, believe the data.

Field 4 is confirmed to vary independently of `strength`. On a steady 35.2
GHz source, `strength` sat at `1` across dozens of consecutive packets
while field 4 wandered between 26 and 41, climbing and falling with
proximity. So it is a finer-grained signal measurement of some kind. The
scale, units and range are still unknown, since I have only ever seen it
on one weak source.

Field 1 (`alert_id`) reads `00` in every capture I have, across two
separate sessions months apart. If you need to track an alert across
packets, matching on band plus direction plus frequency works better than
trusting this field. Worth revisiting if somebody catches it incrementing.

### Bands

| Value | Meaning |
|-------|---------|
| `K` | K band, 24.05-24.25 GHz |
| `Ka` | Ka band, 33.4-36.0 GHz |
| `X` | X band, 10.5-10.55 GHz |
| `LASER` | Laser / LIDAR |
| `MRCD` | MultaRadar CD |
| `MRCT` | MultaRadar CT |
| `RT3` | Gatso RT3 |
| `RT4` | Gatso RT4 |
| `K POP` | K band POP mode |
| `Ka POP` | Ka band POP mode |

The band table in `Constant.java` writes Ka as `Ka`, real packets send
`KA`. The library upper-cases everything before comparing.

Ka frequencies come through to four decimal places (`33.7850`). RT3 and
RT4 have no frequency; the app displays "Gatso" instead.

Only `K` and `KA` have ever appeared in my captures.

### Mute status

From `getMuteStatus`:

| Value | Meaning | Confirmed on hardware |
|-------|---------|-----------------------|
| `1` | Not muted | yes |
| `2` | Muted (you pressed mute) | yes |
| `3` | Mute memory (you saved this location) | no |
| `4` | Auto mute memory (learned false alert) | no |
| `5` | Blocked mute | no |
| `6` | Quiet ride mute | no |

Field 7 is sticky per-alert state rather than an edge, so a UI can render
a mute indicator straight from it without tracking transitions. Pressing
mute on the detector produces a notification immediately.

### Laser guns

When the band is `LASER`, field 5 carries a gun type ID instead of a
frequency. From `getBandFrequency`:

| ID | Gun | | ID | Gun |
|----|-----|-|----|-----|
| 0 | Generic Laser | | 10 | TraffiPat |
| 1 | LTI 20/20 | | 11 | Truspeed S |
| 2 | Stalker | | 12 | Stealth |
| 3 | RIEGL | | 13 | TruCam |
| 4 | Laser Ally | | 14 | XLR |
| 5 | Kustom | | 15 | DragonEye Compact |
| 6 | Atlanta | | 16 | DragonEye Full-Size |
| 7 | Laveg | | 17 | PoliScan |
| 8 | SL700 | | 18 | Traffistar s350 |
| 9 | SCS-102 | | 19 | Vitronic Poliscan |

(The decompiled switch is on `String.hashCode()`, so the cases jump from
54 to 1567 between IDs 9 and 10. That is just how single characters versus
two-character strings hash. I am not skipping anything.)

None of this has ever been tested against a real laser packet.

---

## POI database

`15005991-b131-3396-014c-664c9867b917`

The stored cameras and user marks, and the only place the R8w hands out
actual coordinates. Binary, unlike everything else, for reasons known only
to Uniden.

It is a stream of variable-length records, not an array. Read the type
byte, look up that type's length, consume, repeat.

| Type byte | Kind | Record length |
|-----------|------|---------------|
| `00` | None | 0 |
| `01` | Speed camera | 13 bytes |
| `02` | Red light camera | 12 bytes |
| `03` | User mark | 10 bytes |

Record layout:

| Offset | Length | Field | Format |
|--------|--------|-------|--------|
| 0 | 1 | Type | `01`, `02`, `03` |
| 1 | 1 | Unknown | |
| 2 | 4 | Latitude | Big-endian IEEE 754 float |
| 6 | 4 | Longitude | Big-endian IEEE 754 float |
| 10 | 2 | Angle | Big-endian uint16, cameras only |
| 12 | 1 | Speed limit | uint8, speed cameras only, mph |

An unrecognised type byte should stop the walk. Guessing a length desyncs
every record after it.

An empty database reads back as `0000`, a single type-`00` record. Since
type 0 has length 0, a parser that does not treat it as a terminator will
spin on it forever.

**The record layout above is untested against real data.** My detector has
nothing stored, so every POI test in the repo is synthetic. If you have a
populated unit, a hex dump of this characteristic would be the single most
useful thing anyone could send me.

---

## Settings

`2d86686a-53dc-25b3-0c4a-f0e10c8dee20` (settings 1)
`5a87b4ef-3bfa-76a8-e642-92933c31434f` (settings 2)

Both readable, neither documented. Settings 2 on my unit is entirely `ff`,
so either it is unused or everything in it is at some default.

Settings 1 is about 200 bytes and stable across reads. One region in it
looks decodable:

```
01 02 5e82 5e8c
01 02 5e66 5e6a
01 02 5e3b 5e3c
00 02 5d5c 5d5c     (x5)
```

Read as big-endian uint16 MHz, those pairs are 24194-24204, 24166-24170,
24123-24124, and 23900-23900 five times over. The first three sit squarely
inside the K band (24.05-24.25 GHz). 23900 is outside it and repeated, so
it looks like an empty-slot sentinel.

Best guess: eight K-band frequency lockout slots, three of them in use,
with the leading byte as enabled/disabled and the `02` as a band code.
Unverified. I have not correlated it against the detector's own lockout
menu, which is the obvious next step. If someone does that before I get to
it, please open an issue.

The `0x2901` descriptors on both characteristics may well just name them.
Nobody has looked.

---

## Write commands

`2c86686a-53dc-25b3-0c4a-f0e10c8dee20`, write without response.

Plain ASCII strings, from `Constant.java`:

| Command | Description |
|---------|-------------|
| `BTreqMUTE:1` | Mute current alert |
| `BTreqMUTE:0` | Unmute |
| `BTreqMMEM:1` | Add current location to mute memory |
| `BTreqMMEM:0` | Delete from mute memory |
| `BTreqUMRK:1` | Add user mark at current location |
| `BTreqUMRK:0` | Delete user mark |
| `BTreqRLCD:0` | Delete red light camera |

**None of these have been tested against hardware.** They are documented
because leaving them out would make this a worse reference, not because I
have verified them. The library requires `allow_writes=True` before it
will send one.

Responses presumably come back on
`5987b4ef-3bfa-76a8-e642-92933c31434f`. Also untested.

Note that you do not need any of this to observe mute state. The detector
reports it in field 7 of the alert packet, so pressing the button on the
unit is a perfectly good way to test the read path.

---

## Known unknowns

Everything I could not work out, in one place:

- Settings 2 entirely (all `ff` on my unit).
- Most of settings 1, apart from the suspected K-band lockout block.
- The `0x2901` user description descriptors on every vendor
  characteristic. Never read. Might name things outright.
- Alert field 4 (`raw_signal`). Confirmed to be a fine-grained strength;
  scale and units unknown.
- Alert field 1 (`alert_id`). Always `00`.
- Alert field 8 (`rcv_mode`). Always `0`.
- Telemetry field 4 (`scanCount`). Not a count. No idea what it is.
- POI distance units in telemetry field 1.
- The unknown byte at offset 1 of every POI record.
- Whether the POI record layout is right at all, since I have never seen
  real data.
- The `isI9` flag in `parseAlertBandData`, which suggests the same parser
  handles another model.
- Whether the command characteristic actually answers.
- Why the first pairing attempt fails and the second one does not.

---

## Notes from doing this

- **Uniden chose a text protocol for real-time data and a binary one for
  static data.** Alerts arrive as `1,00,KA,3,33,33.7850,R,1` in UTF-8. The
  POI database is big-endian IEEE 754 floats.

- **Do not trust variable names in decompiled code.** Twice now, see the
  alert field notes above.

- **The pairing situation is annoying.** The detector requires
  pairing, does not advertise unless you ask it to, gives you no
  indication of either, fails the first attempt for no reason, and then
  holds the link afterwards so your own code cannot connect. Four hours
  the first time. Rather less the second time, but only because I had
  written it all down.

- **Testing this required driving past the same school zone radar sign
  roughly 6 or 7 times.** My neighbours probably think I am casing the high
  school. I am not. I am a nerd with a radar detector and poor planning.

- **There is a 35.2 GHz Ka source somewhere in my house.** Found it while
  testing on a desk. Best guess is a door sensor or a neighbour's
  blind-spot monitor. It has been an unreasonably convenient test signal.

---

MIT licensed. Written by Aigis (P.R_Aigis), 2026.
