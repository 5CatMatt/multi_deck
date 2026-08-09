"""Agent orchestration: owns the session, dispatches presses, pushes stats."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import protocol
from .actions import ActionRunner
from .assets import STAMP_FILE, asset_stamp
from .config import ConfigError, DeckConfig
from .link import Link, LinkError, SerialLink, SimulatedLink
from .power import PowerMonitor
from .stats import StatsCollector
from .tray import TrayIcon

log = logging.getLogger("deckhost")

PING_INTERVAL_S = 2.0
STATS_INTERVAL_S = 1.0

# Reconnect backoff: start here, double on each failed cycle, cap, reset on a handshake.
RECONNECT_DELAY_S = 1.0
RECONNECT_DELAY_MAX_S = 15.0

# How long an open port may go without sending `hello` before we close it and start again.
#
# This is the fix for "the link never comes back after the laptop sleeps". The port itself
# recovers on resume — the log shows it reopening two seconds after Windows leaves Modern
# Standby — but the device's CDC does not always come back with it, so the handshake never
# completes. Nothing used to notice: the agent sent `identify` every 2s forever, session_up
# stayed False, and the state ended only when some unrelated stale-handle error surfaced. On
# one morning that took 2, then 4.5, then 6 minutes across three cycles.
#
# Closing and reopening is the remedy rather than merely the retry, because a fresh
# serial.Serial() re-asserts DTR, and that is what prods the device into announcing itself.
HANDSHAKE_TIMEOUT_S = 10.0

# How long an established session may go silent. The device answers every ping and pings run
# at 2s, so 10s is five missed answers. Deliberately the same order as the firmware's own
# MD_LINK_TIMEOUT_MS (5s): both ends should agree about what "gone" means, or one sits in a
# session the other has already abandoned.
SILENCE_TIMEOUT_S = 10.0

WATCHDOG_INTERVAL_S = 1.0

# The deck has no battery-backed RTC, so its clock is whatever we last told it.
TIME_SYNC_INTERVAL_S = 60.0


class DeckHost:
    def __init__(
        self,
        link: Link,
        config: DeckConfig,
        runner: ActionRunner,
        stats: StatsCollector,
        *,
        host_name: str | None = None,
        reconnect: bool = True,
    ) -> None:
        self.link = link
        self.config = config
        self.runner = runner
        self.stats = stats
        self.host_name = host_name or socket.gethostname()
        self.reconnect = reconnect

        self.session_up = False
        self._reader = protocol.FrameReader()
        self._seq = 0
        self._link_failed = asyncio.Event()
        self._layout_pushed = False

        # Watchdog state, reset per session.
        self._opened_at = 0.0
        self._last_inbound = 0.0

        # Backoff state, reset by a completed handshake rather than by a port that merely
        # opened — the failure this exists for is precisely a port that opens and then does
        # nothing, and resetting on open would hammer it once a second forever.
        self._retry_delay = RECONNECT_DELAY_S
        self._failures = 0
        self._failed_since: float | None = None
        self._last_open_error = ""

        # Set by the power monitor when Windows resumes, so a wake cuts the backoff short
        # instead of waiting it out. Never set until the monitor is wired up.
        self._wake = asyncio.Event()

    async def run(self, duration: float | None = None) -> None:
        """Runs sessions until `duration` elapses, or forever.

        A dropped link is expected, not exceptional — the deck gets unplugged, or reflashed
        mid-session. This reconnects rather than exiting, because an agent launched at logon
        that dies the first time a cable moves is not much use.
        """
        end = None if duration is None else time.monotonic() + duration

        while True:
            self._link_failed = asyncio.Event()
            self._reader = protocol.FrameReader()
            self._layout_pushed = False
            self._reload_from_disk()

            try:
                await self.link.open()
            except Exception as exc:
                if not self.reconnect:
                    raise
                self._note_open_failure(exc)
                if not await self._pause_before_retry(end):
                    return
                continue

            self._opened_at = time.monotonic()
            self._last_inbound = self._opened_at

            await self._run_session(end)
            self.session_up = False
            await self.link.close()

            if not self.reconnect or _expired(end):
                return

            log.info("link down — reconnecting in %.0fs", self._retry_delay)
            if not await self._pause_before_retry(end):
                return

    async def _run_session(self, end: float | None) -> None:
        tasks = [
            asyncio.create_task(self._read_loop(), name="read"),
            asyncio.create_task(self._ping_loop(), name="ping"),
            asyncio.create_task(self._stats_loop(), name="stats"),
            asyncio.create_task(self._watchdog_loop(), name="watchdog"),
            asyncio.create_task(self._time_loop(), name="time"),
        ]

        try:
            if end is None:
                await self._link_failed.wait()
            else:
                remaining = end - time.monotonic()
                if remaining > 0:
                    try:
                        await asyncio.wait_for(self._link_failed.wait(), remaining)
                    except TimeoutError:
                        pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _reload_from_disk(self) -> None:
        """Re-reads deck.json at the start of every session.

        Without this, the agent keeps whatever it read at startup. That bites hardest during
        the edit-flash-test loop: reflashing drops the link, and on reconnect the agent and
        the device both still claim the revision from before the edit — so they agree, no
        push happens, and the change silently never appears. Flashing firmware looks like it
        failed to add a page, when in truth nobody ever mentioned the page to the device.

        A file that will not parse is ignored rather than fatal: the running layout is better
        than no layout, and the tray still reports the error.
        """
        if self.config.path is None:
            return

        try:
            fresh = DeckConfig.load(self.config.path)
        except ConfigError as exc:
            log.error("deck.json unreadable, keeping rev %d: %s", self.config.rev, exc)
            return

        if fresh.rev != self.config.rev:
            log.info("deck.json changed on disk: rev %d -> %d", self.config.rev, fresh.rev)
        self.config = fresh

    def _note_open_failure(self, exc: Exception) -> None:
        """Logs a failed open, loudly once and quietly thereafter.

        A deck that is simply unplugged produces this every retry for as long as it stays
        unplugged. Logging each one at WARNING buried an overnight run under 1273 identical
        lines — which is not merely noise: the log rotates at 512KB, so the spam throws away
        the history you actually want when you sit down to work out what happened.
        """
        message = str(exc)
        first = message != self._last_open_error

        if first:
            log.warning("cannot open link: %s", message)
            self._last_open_error = message
        else:
            log.debug("cannot open link: %s", message)

    def _note_recovered(self) -> None:
        """Called on a completed handshake: clears the backoff and reports the outage."""
        if self._failures > 1 and self._failed_since is not None:
            log.info(
                "link restored after %s (%d attempts)",
                _format_duration(time.monotonic() - self._failed_since),
                self._failures,
            )

        self._retry_delay = RECONNECT_DELAY_S
        self._failures = 0
        self._failed_since = None
        self._last_open_error = ""

    async def _pause_before_retry(self, end: float | None) -> bool:
        """Sleeps before the next attempt. Returns False if the run should stop instead.

        Wakes early if the machine resumes from sleep, since that is the single most likely
        moment for the deck to become reachable again and there is no sense sitting out a
        15 second timer once we have been told.
        """
        if _expired(end):
            return False

        if self._failed_since is None:
            self._failed_since = time.monotonic()
        self._failures += 1

        delay = self._retry_delay
        if end is not None:
            delay = min(delay, max(0.0, end - time.monotonic()))

        self._wake.clear()
        try:
            await asyncio.wait_for(self._wake.wait(), delay)
            log.info("woke from sleep — retrying the link now")
        except TimeoutError:
            pass

        self._retry_delay = min(self._retry_delay * 2, RECONNECT_DELAY_MAX_S)
        return not _expired(end)

    def on_wake(self) -> None:
        """Hook for the power monitor. Safe to call from any thread via call_soon_threadsafe."""
        self._wake.set()

    async def on_sleep(self) -> None:
        """Tells the deck the PC is going away, while there is still a bus to say it on.

        Best-effort by nature: on a machine that cuts USB at suspend this frame may be the last
        thing that gets through, or may not get through at all. The deck is written so that
        never receiving it simply means no sleep screen, rather than a wedged state.
        """
        if not self.session_up:
            return
        await self._send(protocol.power("sleep"))

    # -- loops ---------------------------------------------------------------------------

    async def _read_loop(self) -> None:
        while True:
            try:
                chunk = await self.link.read()
            except LinkError as exc:
                log.warning("%s", exc)
                self._link_failed.set()
                return

            if not chunk:
                await asyncio.sleep(0.05)
                continue

            for frame in self._reader.feed(chunk):
                try:
                    await self._on_frame(frame)
                except Exception:
                    log.exception("failed handling frame %r", frame.get("t"))

    async def _ping_loop(self) -> None:
        # Prod the device straight away rather than waiting for it to announce itself.
        await self._send(protocol.identify())

        while True:
            await asyncio.sleep(PING_INTERVAL_S)
            if self.session_up:
                self._seq += 1
                await self._send(protocol.ping(self._seq))
            else:
                # Keep asking until it answers. Covers the device booting later than us, and
                # the device still holding a stale session from a previous agent process.
                await self._send(protocol.identify())

    async def _time_loop(self) -> None:
        """Keeps the deck's clock honest.

        Once a minute is far more often than the ESP32's ~20ppm drift needs (about 1.7 seconds
        a day), but it is also what carries a daylight-saving change across, and it means a
        deck that misses one frame is never more than a minute stale.
        """
        while True:
            if self.session_up:
                await self._send(protocol.time_sync())
            await asyncio.sleep(TIME_SYNC_INTERVAL_S)

    async def _watchdog_loop(self) -> None:
        """Ends a session that has stopped being one.

        Two failures used to be invisible here, both of them looking like a healthy link:

        A port that opens but never handshakes. This is what a laptop waking from Modern
        Standby produces — Windows brings COM back within seconds, but the device's CDC does
        not always come back with it. `identify` went out every 2s into nothing, forever.

        A session that goes quiet without erroring. Reads returning b'' and writes buffered
        into the void are both silent, so `session_up` stayed True with nothing behind it.

        Neither is distinguishable from a working link by looking at return values, which is
        exactly why both need a clock rather than an error to catch them.
        """
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_S)
            now = time.monotonic()

            if not self.session_up:
                if now - self._opened_at > HANDSHAKE_TIMEOUT_S:
                    log.warning(
                        "port open %.0fs with no hello — reopening to re-assert DTR",
                        now - self._opened_at,
                    )
                    self._link_failed.set()
                    return
                continue

            quiet = now - self._last_inbound
            if quiet > SILENCE_TIMEOUT_S:
                log.warning("device silent for %.0fs — ending the session", quiet)
                self.session_up = False
                self._link_failed.set()
                return

    async def _stats_loop(self) -> None:
        while True:
            await asyncio.sleep(STATS_INTERVAL_S)
            if not self.session_up:
                continue

            try:
                sample = self.stats.sample()
            except Exception:
                log.exception("stats collection failed")
                continue

            await self._send(protocol.stats(sample))

    # -- frame handling ------------------------------------------------------------------

    async def _on_frame(self, frame: dict[str, Any]) -> None:
        # Any frame at all is proof of life, which is why this is stamped here rather than in
        # the handlers below — `pong` is the one that arrives most reliably and it is also the
        # one this method does nothing else with.
        self._last_inbound = time.monotonic()

        kind = frame.get("t")

        if kind == "hello":
            await self._on_hello(frame)
        elif kind == "press":
            await self._on_press(frame)
        elif kind == "release":
            pass  # reserved for hold-to-repeat behaviours
        elif kind == "layout_req":
            await self._push_layout()
        elif kind == "log":
            log.log(
                _level_for(frame.get("lvl", "info")),
                "device: %s",
                frame.get("msg", ""),
            )
        elif kind == "error":
            log.error("device error [%s] %s", frame.get("code"), frame.get("msg"))
        elif kind == "pong":
            pass
        else:
            log.debug("unhandled frame %r", kind)

    async def _on_hello(self, frame: dict[str, Any]) -> None:
        try:
            protocol.check_proto(frame)
        except protocol.ProtocolError as exc:
            # Deliberately fatal to the session rather than a warning: interoperating across
            # a version gap produces confusing partial failures later.
            log.error("%s — refusing session", exc)
            self.session_up = False
            return

        was_up = self.session_up
        self.session_up = True

        if was_up:
            # A duplicate hello is harmless but must not re-announce or re-push.
            log.debug("repeat hello from device")
        else:
            # A handshake, not merely an open port, is what proves the link works — so this is
            # where the backoff earns its reset.
            self._note_recovered()
            log.info(
                "device connected: fw %s, layout rev %s",
                frame.get("fw"),
                frame.get("rev"),
            )

        await self._send(protocol.welcome(self.host_name, self.config.rev))

        # Immediately, not on the next minute tick. The deck loses its clock across a power
        # cycle, so the gap between connecting and the first sync is exactly when it would be
        # showing "--:--" — and a reconnect is the most likely moment for someone to be
        # looking at it.
        await self._send(protocol.time_sync())

        if not was_up:
            await self._check_assets(frame)

        device_rev = frame.get("rev", -1)
        if device_rev != self.config.rev and not self._layout_pushed:
            log.info(
                "layout rev %s on device, %s here — pushing", device_rev, self.config.rev
            )
            self._layout_pushed = True
            await self._push_layout()

    async def _check_assets(self, frame: dict[str, Any]) -> None:
        """Tells the deck when the images on its card are older than the ones here.

        The layout cannot go stale — it is pushed whenever the revisions disagree. Images can,
        because they only reach the card by hand, and a card that was never rewritten fails
        without failing: the old wallpaper is still present, so it loads, and nothing anywhere
        says the version on screen is not the one you made. That is the gap this closes.

        Deliberately quiet in every ambiguous case. A check that cries wolf gets ignored, and
        this one only gets read when something is already confusing.
        """
        if "assets" not in frame:
            # No card mounted, or firmware older than the stamp. Either way there is nothing
            # to compare against — and a deck with no card already says so on screen.
            log.debug("device reported no asset stamp")
            return

        root = self.config.asset_root
        if root is None:
            return

        # Hashes every file under sdcard/ — a few megabytes, so single-digit milliseconds, and
        # only on connect. Worth revisiting if the asset tree ever gets large enough to notice.
        expected = asset_stamp(root)
        if not expected:
            return  # nothing here to sync, so nothing on the card can be out of date

        device = frame["assets"]
        if device == expected:
            log.debug("assets current (%s)", expected)
            return

        if device:
            log.warning(
                "assets on the card are stale: card %s, %s %s — copy %s to the card",
                device,
                root.name,
                expected,
                root,
            )
            await self._send(
                protocol.toast("Assets are stale — copy sdcard/ to the card", "warn")
            )
        else:
            # A card written before stamps existed. Says nothing about whether the images on
            # it are current, so the wording asks for a copy without claiming they are wrong.
            log.warning(
                "card carries no %s, so its assets cannot be checked — copy %s to the card",
                STAMP_FILE,
                root,
            )
            await self._send(
                protocol.toast("Card has no asset stamp — copy sdcard/ to it", "warn")
            )

    async def _on_press(self, frame: dict[str, Any]) -> None:
        button_id = frame.get("id", "")
        action = self.config.action_for(button_id)

        if action is None:
            log.warning("press for unknown button %r", button_id)
            await self._send(protocol.toast(f"Unknown button {button_id}", "error"))
            return

        log.info("press %s -> %s", button_id, action.get("type"))
        await self.runner.run(action, on_hid_exec=self._send_hid_exec)

    async def _send_hid_exec(self, action: dict[str, Any]) -> None:
        await self._send(protocol.hid_exec(action))

    async def _push_layout(self) -> None:
        await self._send(protocol.layout(self.config.rev, self.config.raw))

    async def reload_config(self) -> None:
        """Re-reads deck.json from disk and pushes it to the device.

        Invoked from the tray, so a layout edit does not need the agent restarted. A layout
        that fails validation is rejected and the running one kept — a typo should not leave
        you with a blank deck.
        """
        path = self.config.path

        try:
            fresh = DeckConfig.load(path)
        except ConfigError as exc:
            log.error("reload failed, keeping current layout: %s", exc)
            await self._send(protocol.toast("deck.json has errors", "error"))
            return

        self.config = fresh
        log.info("reloaded %s (rev %d, %d buttons)", path, fresh.rev, len(fresh.buttons))
        for warning in fresh.warnings():
            log.warning("%s", warning)

        if self.session_up:
            await self._push_layout()
            await self._send(protocol.toast("Layout reloaded"))

    async def _send(self, frame: dict[str, Any]) -> None:
        try:
            await self.link.write(protocol.encode(frame))
        except LinkError as exc:
            # Non-fatal by design: end the session and let run() reconnect.
            log.warning("%s", exc)
            self.session_up = False
            self._link_failed.set()


def _expired(end: float | None) -> bool:
    return end is not None and time.monotonic() >= end


def _format_duration(seconds: float) -> str:
    """Coarse, for one log line. '64m' reads faster than '3847s' when scanning a log."""
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _level_for(name: str) -> int:
    return {
        "debug": logging.DEBUG,
        "info": logging.INFO,
        "warn": logging.WARNING,
        "error": logging.ERROR,
    }.get(name, logging.INFO)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="deckhost", description="multi_deck PC agent"
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="run against an in-process fake deck; implies --dry-run",
    )
    parser.add_argument("--port", help="serial port; autodetected when omitted")
    parser.add_argument("--deck", type=Path, help="path to deck.json")
    parser.add_argument(
        "--dry-run", action="store_true", help="log actions instead of performing them"
    )
    parser.add_argument(
        "--duration", type=float, help="exit after N seconds (used by the test harness)"
    )
    parser.add_argument(
        "--tray",
        action="store_true",
        help="show a system tray icon and log to a file; used by the autostart task",
    )
    parser.add_argument("--log-file", type=Path, help="append logs to this file")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def default_log_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "multi_deck" / "deckhost.log"


def setup_logging(verbose: bool, log_file: Path | None) -> Path | None:
    """Configures logging, adding a file handler when there may be no console.

    Under pythonw.exe stderr goes nowhere, so without this an autostarted agent would fail
    completely silently.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    resolved: Path | None = None

    if log_file is not None:
        resolved = log_file
        try:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    resolved, maxBytes=512_000, backupCount=3, encoding="utf-8"
                )
            )
        except OSError as exc:
            print(f"could not open log file {resolved}: {exc}", file=sys.stderr)
            resolved = None

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
    )
    return resolved


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    log_file = args.log_file or (default_log_path() if args.tray else None)
    resolved_log = setup_logging(args.verbose, log_file)

    try:
        config = DeckConfig.load(args.deck)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    log.info("loaded %s (rev %d, %d buttons)", config.path, config.rev, len(config.buttons))
    for warning in config.warnings():
        log.warning("%s", warning)

    if args.simulate:
        # Drive a few buttons that between them cover a device-local action, an agent
        # action, and a mixed sequence.
        script = [
            button_id
            for button_id in ("launch.notepad", "win.snap_left", "macro.print")
            if button_id in config.buttons
        ]
        link: Link = SimulatedLink(script, rev=-1)
        dry_run = True
    else:
        link = SerialLink(args.port)
        dry_run = args.dry_run

    if dry_run:
        log.info("dry-run: actions will be logged, not performed")

    host = DeckHost(
        link,
        config,
        ActionRunner(dry_run=dry_run),
        StatsCollector(synthetic=args.simulate),
        # Nothing to reconnect to with a fake device.
        reconnect=not args.simulate,
    )

    tray: TrayIcon | None = None
    monitor: PowerMonitor | None = None
    stop = asyncio.Event()

    if not args.simulate:
        loop = asyncio.get_running_loop()
        monitor = PowerMonitor(
            # Both callbacks arrive on the monitor's thread, so both hop back onto the loop.
            on_sleep=lambda: asyncio.run_coroutine_threadsafe(host.on_sleep(), loop),
            on_wake=lambda: loop.call_soon_threadsafe(host.on_wake),
        )
        monitor.start()

    if args.tray:
        loop = asyncio.get_running_loop()
        tray = TrayIcon(
            # Called from the tray thread, so hop back onto the event loop.
            on_quit=lambda: loop.call_soon_threadsafe(stop.set),
            on_reload=lambda: asyncio.run_coroutine_threadsafe(host.reload_config(), loop),
            status_getter=lambda: host.session_up,
            log_path=resolved_log,
        )
        tray.start()

    try:
        if args.duration is not None or tray is None:
            await host.run(duration=args.duration)
        else:
            # Run until the tray asks us to stop.
            runner = asyncio.create_task(host.run(), name="host")
            await stop.wait()
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        log.error("%s", exc)
        return 1
    finally:
        if tray is not None:
            tray.stop()
        if monitor is not None:
            monitor.stop()

    log.info("deckhost stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    if os.name != "nt":
        log.warning("this agent targets Windows; some actions will not work here")
    try:
        return asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
