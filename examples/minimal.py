#!/usr/bin/env python3
"""The smallest useful program: print every alert as it happens."""

import asyncio
import sys

from r8link import R8W

ADDRESS = sys.argv[1] if len(sys.argv) > 1 else "E0:00:00:00:23:D4"


async def main():
    async with R8W(ADDRESS) as r8:
        print(f"connected, {r8.telemetry.voltage}V")
        async for update in r8.updates():
            if update.kind == "alerts":
                for alert in update.alerts:
                    print(alert)


asyncio.run(main())