#!/usr/bin/env python3
"""
Pairing wizard for the Uniden R8w.

Everything in the README's pairing section, done for you, in order, with
the failure modes checked before they happen instead of after. It:

  - makes sure Bluetooth is not rfkill blocked
  - makes sure bluetoothd is running and the adapter is powered
  - makes sure you are in the `bluetooth` group
  - scans for an R8W and lets you pick one
  - pairs it, retrying, because the first attempt often fails for no reason
  - makes sure it is NOT trusted (trusted devices get auto-reconnected by
    BlueZ, which breaks every subsequent connection from Python)
  - disconnects, because BlueZ holds the link and will not share it
  - connects with r8link and reads real telemetry off the thing

Run it with the venv active:

    r8link-pair                      # scan and pick
    r8link-pair E0:00:00:00:23:D4    # skip the scan, use this address

The second form doubles as a health check on an already-paired unit: say
no when it offers to re-pair and it will just disconnect, connect, and
read.

Do not run it with sudo. It will ask you to run the one command that
needs root, if it needs it at all.
"""

from __future__ import annotations

import asyncio
import grp
import os
import platform
import queue
import shutil
import subprocess
import sys
import threading
import time

from .client import R8W


# ---------------------------------------------------------------- output

_TTY = sys.stdout.isatty()

def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text

def step(text: str) -> None:
    print(f"\n{_c('1', '==')} {_c('1', text)}")

def ok(text: str) -> None:
    print(f"  {_c('32', 'ok')}   {text}")

def warn(text: str) -> None:
    print(f"  {_c('33', 'warn')} {text}")

def bad(text: str) -> None:
    print(f"  {_c('31', 'fail')} {text}")

def die(text: str, fix: str | None = None) -> None:
    bad(text)
    if fix:
        print(f"\n{fix}\n")
    sys.exit(1)

def ask(prompt: str) -> bool:
    try:
        return input(f"  {prompt} [y/N] ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)

def pause(prompt: str) -> None:
    try:
        input(f"  {prompt}")
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(1)


def arm_prompt(again: bool = False) -> None:
    """Tell the user to put the detector into pairing mode."""

    print()
    if again:
        print("  The pairing window may have closed during the scan. Re-arm it")
        print("  if the display is no longer showing 'Pairing R8W@..':")
        print(f"      {_c('1', 'MENU -> BT Pairing -> MENU')}")
        print()
        pause("Press Enter to pair.")
        return

    print("  Put the detector into pairing mode:")
    print()
    print(f"      {_c('1', 'MENU -> BT Pairing -> MENU')}")
    print()
    print("  The display should read 'Pairing R8W@..'. An unpaired R8w does")
    print("  not advertise until you arm it, so nothing can find it before")
    print("  this. The window closes on its own, so keep it within reach.")
    print()
    pause("Press Enter once the display shows 'Pairing R8W@..'.")


# ------------------------------------------------------------- subprocess

def run_cmd(args: list[str], timeout: float = 20.0) -> tuple[int, str]:
    """Run a command, return (returncode, stdout+stderr). Never raises."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{args[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{args[0]}: timed out after {timeout}s"


def bctl(*args: str, timeout: float = 25.0) -> tuple[int, str]:
    """One-shot bluetoothctl. Fine for queries like `info` and `remove`.

    NOT fine for pairing: see BluetoothCtl below for why.
    """
    return run_cmd(["bluetoothctl", *args], timeout=timeout)


def device_info(address: str) -> dict[str, str]:
    """Parse `bluetoothctl info <addr>` into a dict. Empty if unknown."""
    code, out = bctl("info", address)
    if code != 0 or "not available" in out.lower():
        return {}
    info: dict[str, str] = {}
    for line in out.splitlines():
        if ":" in line:
            key, _, value = line.strip().partition(":")
            info[key.strip()] = value.strip()
    return info


class BluetoothCtl:
    """A persistent interactive bluetoothctl session.

    This has to be persistent. `bluetoothctl pair <addr>` as a one-shot
    registers an agent, starts the pairing, and then exits, taking the
    agent with it before the detector has finished. BlueZ reports that as
    AuthenticationCanceled, which reads like the detector rejected you.

    Output is bumped off the pipe by a background thread so a full buffer
    can never wedge the child, and everything seen is kept in `transcript`
    so a failure can show you what actually happened.
    """

    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        self.lines: queue.Queue[str] = queue.Queue()
        self.transcript: list[str] = []
        self._reader = threading.Thread(target=self._pump, daemon=True)
        self._reader.start()
        time.sleep(0.5)

    def _pump(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))

    def send(self, cmd: str) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(cmd + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError):
            pass  # child died; the caller's wait_for will time out

    def wait_for(self, needles: tuple[str, ...], timeout: float) -> str | None:
        """Consume output until one of `needles` shows up. Returns the
        matching line, or None on timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self.lines.get(timeout=0.25)
            except queue.Empty:
                continue
            self.transcript.append(line)
            for needle in needles:
                if needle in line:
                    return line
        return None

    def drain(self) -> None:
        """Throw away anything buffered, keeping it in the transcript."""
        while True:
            try:
                self.transcript.append(self.lines.get_nowait())
            except queue.Empty:
                return

    def close(self) -> None:
        try:
            self.send("exit")
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# ------------------------------------------------------------ preflight

def check_platform() -> None:
    step("Checking the basics")

    if platform.system() != "Linux":
        die(f"This is {platform.system()}, and this script is BlueZ only.")
    ok(f"Linux ({platform.release()})")

    for tool in ("bluetoothctl", "rfkill"):
        if shutil.which(tool) is None:
            die(f"{tool} not found on PATH.",
                "    sudo apt install bluez rfkill")
    ok("bluetoothctl and rfkill present")

    if os.geteuid() == 0:
        warn("Running as root. This usually means the venv is not the one you")
        warn("think it is, and the pairing will be stored for root rather than")
        warn("for you. Ctrl-C and re-run without sudo if that was not intended.")


def check_rfkill() -> None:
    step("Checking rfkill")

    code, out = run_cmd(["rfkill", "list", "bluetooth"])
    if code != 0:
        warn("Could not read rfkill state, carrying on anyway.")
        return

    if "Hard blocked: yes" in out:
        die("Bluetooth is HARD blocked.",
            "That is a physical switch or a firmware setting, not something\n"
            "this script can undo. Check /boot/firmware/config.txt for\n"
            "`dtoverlay=disable-bt` and remove it, then reboot.")

    if "Soft blocked: yes" in out:
        warn("Bluetooth is soft blocked. This is the default on some Pi images.")
        if ask("Run `sudo rfkill unblock bluetooth` now?"):
            code, out = run_cmd(["sudo", "rfkill", "unblock", "bluetooth"], timeout=60)
            if code != 0:
                die("Unblock failed.", out)
            ok("Unblocked")
        else:
            die("Cannot continue while Bluetooth is blocked.",
                "    sudo rfkill unblock bluetooth")
    else:
        ok("Not blocked")


def check_group() -> None:
    step("Checking group membership")

    try:
        gid = grp.getgrnam("bluetooth").gr_gid
    except KeyError:
        ok("No `bluetooth` group on this system, so nothing to check")
        return

    if gid in os.getgroups():
        ok("You are in the `bluetooth` group")
        return

    warn("You are NOT in the `bluetooth` group.")
    warn("BlueZ's D-Bus policy grants pairing to that group. Without it, the")
    warn("pair below may fail with AccessDenied or 'Rejected send message',")
    warn("which looks nothing at all like a permissions problem.")
    print()
    print("    sudo usermod -aG bluetooth $USER")
    print()
    print("  Then log out and back in. Group membership only applies to new")
    print("  sessions, so a new shell in the same SSH session is not enough.")
    print()
    if not ask("Try to continue anyway?"):
        sys.exit(1)


def check_daemon() -> None:
    step("Checking bluetoothd")

    code, out = run_cmd(["systemctl", "is-active", "bluetooth"])
    if out.strip() != "active":
        warn(f"bluetooth.service is '{out.strip() or 'unknown'}'.")
        if ask("Start it with `sudo systemctl enable --now bluetooth`?"):
            code, out = run_cmd(["sudo", "systemctl", "enable", "--now", "bluetooth"], timeout=60)
            if code != 0:
                die("Could not start bluetoothd.", out)
        else:
            die("Nothing works without bluetoothd.",
                "    sudo systemctl enable --now bluetooth")
    ok("bluetoothd is running")

    bctl("power", "on")
    code, out = bctl("show")
    if "Powered: yes" not in out:
        die("The adapter will not power on.",
            "Check `hciconfig -a` and `dmesg | grep -i blue`. If there is no\n"
            "adapter listed at all, the hardware is not being seen by the kernel.")
    ok("Adapter powered on")


# -------------------------------------------------------------- scanning

async def find_detector() -> str | None:
    """Arm pairing mode, then scan.

    Order matters. An unpaired R8w does not advertise at all unless it is
    in pairing mode, so scanning first just times out three times and makes
    you think something is broken.
    """
    step("Looking for the detector")

    arm_prompt()

    for attempt in (1, 2, 3):
        print(f"  Scanning ({attempt}/3)...")
        try:
            devices = await R8W.discover(timeout=10.0)
        except Exception as exc:
            bad(f"Scan failed: {exc}")
            return None

        if devices:
            break

        warn("Nothing found.")
        if attempt < 3:
            print("  Either the pairing window closed, or your phone has the")
            print("  detector, it stops advertising the moment anything else")
            print("  connects. Re-arm pairing mode, and turn Bluetooth off on")
            print("  the phone if that does not do it.")
            pause("Press Enter to rescan.")
    else:
        bad("No R8W found after three attempts.")
        return None

    if len(devices) == 1:
        d = devices[0]
        ok(f"Found {d.name} at {d.address}")
        return d.address

    print()
    for i, d in enumerate(devices, 1):
        print(f"    {i}. {d.name}  {d.address}")
    print()
    while True:
        try:
            choice = int(input(f"  Which one? [1-{len(devices)}] ").strip())
            if 1 <= choice <= len(devices):
                return devices[choice - 1].address
        except (ValueError, EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)


# --------------------------------------------------------------- pairing

PAIR_OK = ("Pairing successful",)
PAIR_FAIL = ("Failed to pair:", "org.bluez.Error", "not available")


def pair(address: str, already_armed: bool = False) -> None:
    step(f"Pairing {address}")

    removed = False
    info = device_info(address)
    if info.get("Paired") == "yes":
        ok("Already paired")
        if not ask("Remove the existing pairing and start over?"):
            return
        bctl("remove", address)
        time.sleep(1.0)
        ok("Removed")
        removed = True
        already_armed = False  # the bond is gone, so it needs arming again

    ctl = BluetoothCtl()
    try:
        ctl.send("power on")
        ctl.send("agent on")
        ctl.send("default-agent")
        time.sleep(1.0)
        ctl.drain()

        # Active scanning competes with connecting on a single-radio
        # adapter, which the Pi's onboard Cypress part is.
        ctl.send("scan off")
        time.sleep(1.0)
        ctl.drain()

        arm_prompt(again=already_armed)

        # BlueZ cannot pair with an address it has not seen advertise. If we
        # just removed the bond its cache is empty, and `pair` then sits
        # there until the timeout without reporting anything at all.
        if removed:
            print("  Rediscovering...")
            ctl.send("scan on")
            found = ctl.wait_for((address.upper(),), timeout=20.0)
            ctl.send("scan off")
            time.sleep(1.0)
            ctl.drain()
            if found:
                ok("Advertising")
            else:
                warn("Never saw it advertise. The pair below will probably fail.")
                warn("Re-arm pairing mode before continuing.")

        for attempt in (1, 2, 3):
            print(f"  Attempt {attempt}/3...")
            ctl.drain()
            ctl.send(f"pair {address}")

            line = ctl.wait_for(PAIR_OK + PAIR_FAIL, timeout=40.0)

            if line is None:
                warn("No response in 40s. BlueZ is probably not seeing the")
                warn("device at all, check it still shows 'Pairing R8W@..'.")
            elif "Pairing successful" in line:
                ok("Paired")
                break
            elif "AuthenticationFailed" in line:
                # Fails on the first try constantly, works on an identical
                # retry. No explanation, just retry it.
                warn("AuthenticationFailed. Common on the first try, retrying.")
            elif "AuthenticationCanceled" in line:
                warn("AuthenticationCanceled. Usually a timed-out pairing window.")
            elif "AccessDenied" in line or "Rejected send" in line:
                die("Permission denied by D-Bus.",
                    "You are almost certainly not in the `bluetooth` group:\n"
                    "    sudo usermod -aG bluetooth $USER\n"
                    "then log out and back in.")
            elif "not available" in line.lower():
                warn("BlueZ cannot see the device. It is not advertising.")
            else:
                warn(line.strip())

            if attempt < 3:
                print("  Re-arm pairing mode on the detector if it has dropped out.")
                pause("Press Enter to retry.")
        else:
            print()
            print("  Last 25 lines of bluetoothctl output:")
            for l in ctl.transcript[-25:]:
                print(f"    {l}")
            die("Could not pair after three attempts.",
                "Try pairing by hand, it is the same sequence and sometimes\n"
                "just works:\n"
                "    bluetoothctl\n"
                "    scan on            # arm pairing mode, wait for R8W@..\n"
                "    scan off\n"
                f"    pair {address}\n"
                f"    disconnect {address}\n"
                "    exit")

        # Trusted devices get auto-reconnected by BlueZ on every boot, which
        # reproduces the held-link problem forever instead of just once.
        if device_info(address).get("Trusted") == "yes":
            ctl.send(f"untrust {address}")
            time.sleep(0.5)
            ok("Untrusted (BlueZ would otherwise auto-reconnect and steal the link)")
        else:
            ok("Not trusted, which is what we want")

        # Disconnect from inside this session, while it is still alive.
        if device_info(address).get("Connected") == "yes":
            ctl.send(f"disconnect {address}")
            ctl.wait_for(("Disconnection successful", "Failed to disconnect"), timeout=15.0)
    finally:
        ctl.close()


def release(address: str) -> None:
    step("Letting go of the link")

    print("  BlueZ holds the connection after pairing. Until it lets go, your")
    print("  own code cannot have it, and because the detector stops")
    print("  advertising while connected it vanishes from Python entirely.")
    print()

    for _ in range(10):
        if device_info(address).get("Connected") != "yes":
            ok("Disconnected")
            return
        bctl("disconnect", address, timeout=20)
        time.sleep(1.0)

    warn("Still connected. The read below may time out; if it does, run")
    warn(f"`bluetoothctl disconnect {address}` and try again.")


# ---------------------------------------------------------- verification

async def verify(address: str) -> bool:
    step("Reading from the detector")

    try:
        async with R8W(address) as r8:
            t = r8.telemetry
            ok(f"Connected. {t.voltage}V, heading {t.heading}, "
               f"GPS {'locked' if t.gps_locked else 'not locked'}")

            info = await r8.read_device_info()
            ok(f"Firmware: {info.firmware}")
            ok(f"Software: {info.software}")

            pois = await r8.read_poi()
            ok(f"POI database: {len(pois)} record(s)")

            print("  Waiting for notifications...")
            seen = {"telemetry": 0, "alerts": 0}

            async def collect() -> None:
                async for u in r8.updates():
                    seen[u.kind] = seen.get(u.kind, 0) + 1
                    if seen["telemetry"] >= 3:
                        return

            try:
                await asyncio.wait_for(collect(), timeout=20.0)
            except asyncio.TimeoutError:
                warn("No telemetry notifications arrived in 20s.")
                return False

            ok(f"{seen['telemetry']} telemetry packet(s), "
               f"{seen['alerts']} alert packet(s)")

            if r8.alerts:
                for a in r8.alerts:
                    ok(f"Live alert: {a}")
            else:
                ok("No active alerts, which is what a desk usually looks like")

    except Exception as exc:
        bad(f"{type(exc).__name__}")
        print()
        print(exc)
        print()
        return False

    return True


# ------------------------------------------------------------------ main

async def run(argv: list[str] | None = None) -> int:
    print(_c("1", "\nr8link pairing helper"))
    print("The wizard will now setup your Uniden R8w for use on this machine ^_^")

    check_platform()
    check_rfkill()
    check_group()
    check_daemon()

    argv = sys.argv[1:] if argv is None else argv

    if argv:
        address = argv[0].strip().upper()
        already_armed = False
        step(f"Using address {address} from the command line")
    else:
        address = await find_detector()
        if address is None:
            return 1
        already_armed = True

    pair(address, already_armed=already_armed)
    release(address)

    if not await verify(address):
        print()
        bad("Paired, but the read did not work.")
        print()
        print("Things to try, roughly in order:")
        print(f"  1. bluetoothctl disconnect {address}")
        print("  2. Turn Bluetooth off on your phone (it auto-reconnects)")
        print("  3. Power-cycle the detector")
        print("  4. sudo systemctl restart bluetooth")
        print("  5. Reboot. This has fixed it before and I do not know why.")
        print()
        print("If none of that works, run `sudo btmon` in another shell and")
        print("reproduce. It is the only place the real failure is visible.")
        return 1

    print()
    print(_c("32", "Done."))
    print()
    print("Pairing is persistent, you only do this once.")
    print(f"Your detector's address is {_c('1', address)} - pass it to R8W()")
    print("and you are away. Examples, if you want them:")
    print()
    print("    https://github.com/AegisX86/UnidenR8wlink/tree/main/examples")
    print()
    return 0


def main() -> None:
    """Console entry point. Installed as `r8link-pair`."""
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        print("\nCancelled.")
        sys.exit(130)


if __name__ == "__main__":
    main()