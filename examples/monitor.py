#!/usr/bin/env python3
"""
Live monitor demo

Deliberately plain. It exists to be read as much as run, so
between them this and minimal.py demonstrate everything r8link does:

    - reconnecting forever, because the link drops and that is normal
    - telemetry and alerts from one updates() loop
    - telemetry_age, which is how you catch a link that is up but frozen
    - the one-shot reads: device info and the stored POI database
    - Alert.description / is_muted / direction_name, i.e. the parts you
      would actually put on a screen

    python examples/monitor.py [address]
"""

import asyncio
import sys

from r8link import R8W

ADDRESS = sys.argv[1] if len(sys.argv) > 1 else "E0:00:00:00:23:D4" # My address, it's MINE >:3

CLEAR = "\033[2J\033[H"


def draw(r8, info, pois):
    t = r8.telemetry
    age = r8.telemetry_age

    out = [CLEAR, f"r8link  {ADDRESS}", ""]

    # Telemetry arrives every 1-2s. If it stops, the link can still claim
    # to be up while the numbers quietly go stale
    if t is None:
        out.append("waiting for telemetry...")
    else:
        stale = " STALE" if age and age > 5 else ""
        gps = "locked" if t.gps_locked else "no fix"
        out += [
            f"{t.voltage} V     {t.speed} mph  {t.heading}     {t.altitude} ft",
            f"gps {gps}     scan {t.scan_count}     {age:.1f}s ago{stale}",
        ]
        if t.poi:
            out.append(f"POI  {t.poi.kind}  {t.poi.distance}  limit {t.poi.speed_limit}")

    out += ["", "-" * 52]

    if not r8.alerts:
        out.append("clear")
    for a in r8.alerts:
        bars = "#" * (a.strength or 0)
        mute = "  [muted]" if a.is_muted else ""
        out.append(f"{a.band:<6} {a.description:<22} {bars:<8} {a.direction_name}{mute}")

    out += ["", "-" * 52, f"fw {info.firmware}", f"{len(pois)} stored POI"]
    for p in pois[:5]:
        out.append(f"  {p.kind}  {p.latitude:.5f},{p.longitude:.5f}  {p.speed_limit or ''}")

    print("\n".join(out), flush=True)


async def session():
    async with R8W(ADDRESS) as r8:
        # One-shots, read once on connect. Neither characteristic notifies.
        info = await r8.read_device_info()
        pois = await r8.read_poi()

        draw(r8, info, pois)
        async for _ in r8.updates():
            # update.kind tells you what arrived, but r8.telemetry and
            # r8.alerts always hold the latest of both, so a redraw loop
            # can ignore the payload entirely and just use the tick.
            draw(r8, info, pois)


async def main():
    # No automatic reconnect in the library, on purpose: what you want when
    # the link drops depends on your program. Here it is a retry loop.
    while True:
        try:
            await session()
            print("\ndisconnected")
        except Exception as exc:
            print(f"\n{exc}")
        await asyncio.sleep(3)


try:
    asyncio.run(main())
except KeyboardInterrupt:
    # Without this, Ctrl-C skips __aexit__ and leaves the detector holding
    # a half-open link that the next run cannot get past.
    print()
