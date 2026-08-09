"""System tray icon.

Runs the tray in a daemon thread while asyncio keeps the main thread, since pystray's
Windows backend is happy to own a message loop in whatever thread calls run().

Entirely optional: if pystray or Pillow are missing the agent logs once and carries on
headless, because losing the tray should never cost you the deck.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# Matches the deck's own theme, so the tray icon and the device look related.
COLOR_BODY = (0x1B, 0x21, 0x29, 255)
COLOR_CONNECTED = (0x3F, 0xB9, 0x50, 255)
COLOR_WAITING = (0x6E, 0x76, 0x81, 255)

REFRESH_S = 2.0

# The size the proportions were drawn for. Everything below scales from it, so the same shape
# can be asked for at 256px for an application icon without being redrawn by hand.
BASE_SIZE = 64


def make_image(connected: bool, size: int = BASE_SIZE):
    """The deck mark: a rounded body with a 3x2 tile grid inside it.

    Module-level rather than a method on TrayIcon because the theme builder's application icon
    is the same drawing at a larger size, and two hand-drawn versions of one mark drift — you
    notice the day they are side by side in the taskbar.
    """
    from PIL import Image, ImageDraw

    accent = COLOR_CONNECTED if connected else COLOR_WAITING
    k = size / BASE_SIZE

    def s(value: float) -> float:
        return value * k

    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        [s(2), s(2), size - s(3), size - s(3)],
        radius=s(10), fill=COLOR_BODY, outline=accent, width=max(1, round(s(3))),
    )

    # A little 3x2 tile grid, so the icon reads as "deck" at 16px.
    for row in range(2):
        for col in range(3):
            x, y = s(13 + col * 14), s(18 + row * 17)
            draw.rounded_rectangle([x, y, x + s(10), y + s(11)], radius=s(2), fill=accent)

    return image


class TrayIcon:
    def __init__(
        self,
        *,
        on_quit: Callable[[], None],
        on_reload: Callable[[], None],
        status_getter: Callable[[], bool],
        log_path: Path | None = None,
    ) -> None:
        self._on_quit = on_quit
        self._on_reload = on_reload
        self._status_getter = status_getter
        self._log_path = log_path

        self._icon = None
        self._stop = threading.Event()
        self._last_state: bool | None = None

    # -- drawing -------------------------------------------------------------------------

    def _make_image(self, connected: bool):
        return make_image(connected)

    def _status_text(self) -> str:
        return "Deck connected" if self._status_getter() else "Waiting for deck..."

    # -- menu actions --------------------------------------------------------------------

    def _reload(self) -> None:
        log.info("tray: reload requested")
        self._on_reload()

    def _open_log(self) -> None:
        if self._log_path is None or not self._log_path.exists():
            log.warning("tray: no log file to open")
            return
        try:
            os.startfile(self._log_path)  # type: ignore[attr-defined]
        except Exception:
            subprocess.Popen(["notepad.exe", str(self._log_path)])

    def _quit(self) -> None:
        log.info("tray: quit requested")
        self._stop.set()
        if self._icon is not None:
            self._icon.stop()
        self._on_quit()

    # -- lifecycle -----------------------------------------------------------------------

    def start(self) -> bool:
        try:
            import pystray
        except ImportError:
            log.info("pystray not installed — running without a tray icon")
            return False

        try:
            image = self._make_image(False)
        except ImportError:
            log.info("Pillow not installed — running without a tray icon")
            return False

        menu = pystray.Menu(
            pystray.MenuItem(lambda _: self._status_text(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Reload deck.json", lambda: self._reload()),
            pystray.MenuItem("Open log", lambda: self._open_log()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", lambda: self._quit()),
        )

        self._icon = pystray.Icon("multi_deck", image, "multi_deck", menu)

        threading.Thread(target=self._icon.run, name="tray", daemon=True).start()
        threading.Thread(target=self._refresh_loop, name="tray-refresh", daemon=True).start()

        log.info("tray icon started")
        return True

    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                connected = self._status_getter()
                if connected != self._last_state and self._icon is not None:
                    self._icon.icon = self._make_image(connected)
                    self._icon.title = (
                        "multi_deck — connected" if connected else "multi_deck — waiting"
                    )
                    self._last_state = connected
            except Exception:
                log.debug("tray refresh failed", exc_info=True)

            self._stop.wait(REFRESH_S)

    def stop(self) -> None:
        self._stop.set()
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
