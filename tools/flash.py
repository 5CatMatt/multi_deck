"""Build and flash the deck, using each USB port for what it is good at.

    python tools/flash.py                 # build, flash, reset, tail the log
    python tools/flash.py --no-build      # flash what is already built
    python tools/flash.py --monitor       # just tail port A

`arduino-cli upload` drives one port for both the reset and the data, which on this board means
pushing 1.1MB through the CH343 on port A. That was observed dropping off the USB bus partway
through five consecutive writes — at 28%, 36% and 28% again, at 921600, 460800 and 115200 baud
alike, while few-KB images wrote fine every time. Lowering the baud rate does not help, because
the connection is not corrupting data, it is disappearing.

So this splits the job:

    port A   the reset pulse only, a few microseconds of DTR/RTS wiggling
    port B   all 1.1MB, over the ESP32-S3's built-in USB-Serial-JTAG, no bridge chip involved

Same write took 7 seconds, first attempt, having failed five times the other way.

The three steps are all load-bearing, and the third is the one that looks optional and is not:
esptool's `--after hard-reset` toggles RTS on the *JTAG* port, which is not wired to EN, so the
board would sit in the ROM afterwards looking exactly like a failed flash.

See docs/hardware-notes.md.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SKETCH = REPO / "firmware" / "multi_deck"
BUILD = REPO / "build"

FQBN = (
    "esp32:esp32:esp32s3:PSRAM=opi,FlashSize=8M,PartitionScheme=default_8MB,"
    "USBMode=default,CDCOnBoot=default"
)

ARDUINO_CLI = Path(
    r"C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
)
ESPTOOL = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Arduino15/packages/esp32/tools/esptool_py/5.3.1/esptool.exe"
)

TASK = "multi_deck agent"

# Espressif's VID. Both the running app's CDC and the ROM's USB-JTAG use it, on different
# COM numbers, which is exactly why the port has to be looked up rather than remembered.
ESPRESSIF_VID = 0x303A

APP_OFFSET = "0x10000"


def fail(message: str) -> None:
    sys.exit(f"error: {message}")


def espressif_ports() -> set[str]:
    from serial.tools import list_ports

    return {p.device for p in list_ports.comports() if p.vid == ESPRESSIF_VID}


def uart_port() -> str:
    """Port A, the CH343 bridge — the only thing wired to EN and IO0."""
    from serial.tools import list_ports

    for info in list_ports.comports():
        if info.vid == 0x1A86:
            return info.device
    fail("port A (CH343) not found — it carries the reset lines, so it must be plugged in")


def reset(port: str, *, download: bool) -> None:
    """Pulse the board. RTS drives EN, DTR drives IO0.

    `download` leaves IO0 low across the release, which is what makes the ROM wait for a flash
    instead of booting the app.
    """
    import serial

    with serial.Serial(port, 115200, timeout=0.2) as s:
        s.setDTR(False)
        s.setRTS(True)
        time.sleep(0.15)
        if download:
            s.setDTR(True)
        s.setRTS(False)
        time.sleep(0.15)
        s.setDTR(False)


def agent(action: str) -> None:
    """Stop or start the autostart task, ignoring its absence.

    Worth doing: the agent polls its port every couple of seconds, and having it reconnect to a
    board that is disappearing into download mode adds USB churn to the host controller that
    port A shares.
    """
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", f"{action}-ScheduledTask -TaskName '{TASK}'"],
        capture_output=True,
    )


def build() -> None:
    if not ARDUINO_CLI.is_file():
        fail(f"arduino-cli not found at {ARDUINO_CLI}")

    print("building...")
    result = subprocess.run(
        [str(ARDUINO_CLI), "compile", "--fqbn", FQBN, "--build-path", str(BUILD), str(SKETCH)],
        capture_output=True,
        text=True,
    )
    for line in (result.stdout + result.stderr).splitlines():
        # The linker's .note.GNU-stack complaints are toolchain noise, documented as such.
        if "error" in line.lower() or "Sketch uses" in line or "Global variables" in line:
            if "GNU-stack" not in line:
                print("  " + line.strip())
    if result.returncode != 0:
        fail("build failed")


def flash() -> None:
    binary = BUILD / "multi_deck.ino.bin"
    if not binary.is_file():
        fail(f"no built image at {binary} — run without --no-build")
    if not ESPTOOL.is_file():
        fail(f"esptool not found at {ESPTOOL}")

    uart = uart_port()
    before = espressif_ports()

    print(f"entering download mode via {uart}...")
    reset(uart, download=True)
    time.sleep(2.5)

    appeared = sorted(espressif_ports() - before)
    if not appeared:
        # The JTAG unit only exists while the app is stopped. If the app is still running, its
        # own CDC is occupying port B and nothing new shows up.
        fail(
            "no USB-JTAG port appeared on port B. Is port B plugged in? "
            f"(Espressif ports seen: {sorted(espressif_ports()) or 'none'})"
        )

    jtag = appeared[0]
    print(f"flashing over {jtag} (port B, no bridge chip)...")

    result = subprocess.run(
        [
            str(ESPTOOL), "--chip", "esp32s3", "-p", jtag,
            "--before", "no-reset", "--after", "no-reset",
            "write-flash", "--flash-mode", "dio", "--flash-freq", "80m",
            "--flash-size", "8MB", APP_OFFSET, str(binary),
        ],
        capture_output=True,
        text=True,
    )
    for line in (result.stdout + result.stderr).splitlines():
        if any(k in line for k in ("Wrote", "Hash of", "fatal", "error")):
            print("  " + line.strip())
    if result.returncode != 0:
        fail("flash failed")

    # The step that is easy to leave out. esptool cannot reset the board through the JTAG port,
    # so without this it stays in the ROM and looks like the flash did not take.
    print(f"resetting into run mode via {uart}...")
    reset(uart, download=False)


def monitor(seconds: float) -> None:
    import serial

    port = uart_port()
    print(f"--- {port} ---")
    deadline = time.time() + seconds
    with serial.Serial(port, 115200, timeout=0.3) as s:
        while time.time() < deadline:
            chunk = s.read(4096)
            if chunk:
                sys.stdout.write(chunk.decode("utf-8", "replace"))
                sys.stdout.flush()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flash", description=__doc__.splitlines()[0])
    parser.add_argument("--no-build", action="store_true", help="flash what is already built")
    parser.add_argument("--monitor", action="store_true", help="only tail port A")
    parser.add_argument("--seconds", type=float, default=15.0, help="how long to tail")
    parser.add_argument(
        "--keep-agent", action="store_true", help="do not stop the agent while flashing"
    )
    args = parser.parse_args(argv)

    if args.monitor:
        monitor(args.seconds)
        return 0

    if not args.no_build:
        build()

    if not args.keep_agent:
        agent("Stop")
        time.sleep(1.0)

    try:
        flash()
        time.sleep(1.0)
        monitor(args.seconds)
    finally:
        if not args.keep_agent:
            agent("Start")
            print("\nagent restarted")

    return 0


if __name__ == "__main__":
    sys.exit(main())
