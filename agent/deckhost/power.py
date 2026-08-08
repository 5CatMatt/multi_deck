"""Windows sleep and wake notifications.

Two things need this. The link has to come back promptly when the machine resumes, and the
deck has to be *told* the PC is asleep so it can show something useful instead of tiles that
cannot do anything.

Written against `ctypes` rather than pywin32 deliberately: it is the only Windows API this
agent needs, and adding a compiled dependency for one notification would be out of proportion.
Everything here degrades to "no power events" rather than failing, in the same way the tray
icon and the GPU stats do — losing this should cost you faster reconnects, never the deck.

## Why the display state, and not just suspend

This laptop reports only **Modern Standby (S0 Low Power Idle)** — `powercfg /a` lists no S3.
That matters more than it sounds:

- The process is never frozen. It keeps running throttled, so "detect a jump in the wall clock"
  — the usual trick for spotting a suspend — detects nothing at all here.
- `PBT_APMSUSPEND` arrives very late, seconds before the USB bus goes down, which is not much
  margin for getting a frame out to the deck.

`GUID_CONSOLE_DISPLAY_STATE` is the signal that actually corresponds to "the user has walked
away", it fires well before the bus suspends, and it fires on the way back too. The `PBT_APM*`
pair is kept as a backstop for machines that do have S3.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

log = logging.getLogger(__name__)

# -- Win32 constants ---------------------------------------------------------------------

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_POWERBROADCAST = 0x0218

PBT_APMSUSPEND = 0x0004
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012
PBT_POWERSETTINGCHANGE = 0x8013

DEVICE_NOTIFY_WINDOW_HANDLE = 0x00000000

# HWND_MESSAGE. A message-only window: never shown, never in the taskbar, but it receives
# broadcasts, which is all we want.
HWND_MESSAGE = -3

# {6FE69556-704A-47A0-8F24-C28D936FDA47} — 0 off, 1 on, 2 dimmed.
_DISPLAY_STATE_GUID = (0x6FE69556, 0x704A, 0x47A0, (0x8F, 0x24, 0xC2, 0x8D, 0x93, 0x6F, 0xDA, 0x47))


class PowerMonitor:
    """Calls `on_sleep` / `on_wake` as the machine suspends and resumes.

    Callbacks fire on the monitor's own thread, so anything touching the asyncio loop must hop
    across with `loop.call_soon_threadsafe` — `DeckHost.on_wake` is written to be safe that way.
    """

    def __init__(
        self,
        *,
        on_sleep: Callable[[], None],
        on_wake: Callable[[], None],
    ) -> None:
        self._on_sleep = on_sleep
        self._on_wake = on_wake

        self._hwnd = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        # Both signals can describe the same transition — a display-off followed by an
        # APMSUSPEND is one sleep, not two — so only edges are reported.
        self._asleep = False

        # ctypes callbacks are collected the moment nothing references them, and a WNDPROC that
        # vanishes while Windows still holds the pointer crashes the process rather than raising.
        self._wndproc_ref = None
        self._registration = None

    # -- lifecycle -----------------------------------------------------------------------

    def start(self) -> bool:
        try:
            import ctypes  # noqa: F401
        except ImportError:  # pragma: no cover - ctypes is always present on CPython
            log.info("ctypes unavailable — running without power notifications")
            return False

        import sys

        if sys.platform != "win32":
            log.info("not Windows — running without power notifications")
            return False

        ready = threading.Event()
        self._thread = threading.Thread(
            target=self._run, args=(ready,), name="power", daemon=True
        )
        self._thread.start()

        # Wait for the window to exist (or fail), so a caller that starts the monitor and
        # immediately sleeps the machine is not racing the registration.
        ready.wait(timeout=2.0)
        return self._hwnd is not None

    def stop(self) -> None:
        self._stop.set()

        if self._hwnd is None:
            return

        try:
            import ctypes

            ctypes.windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        except Exception:  # pragma: no cover - teardown is best-effort
            log.debug("power monitor teardown failed", exc_info=True)

    # -- the window ----------------------------------------------------------------------

    def _run(self, ready: threading.Event) -> None:
        try:
            self._pump(ready)
        except Exception:
            log.info("power notifications unavailable", exc_info=True)
            ready.set()

    def _pump(self, ready: threading.Event) -> None:
        import ctypes
        from ctypes import wintypes

        # use_last_error, or ctypes.get_last_error() reports 0 for every failure and a
        # diagnostic that always says "error 0" is worse than none.
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", ctypes.c_ubyte * 8),
            ]

        class POWERBROADCAST_SETTING(ctypes.Structure):
            _fields_ = [
                ("PowerSetting", GUID),
                ("DataLength", wintypes.DWORD),
                ("Data", ctypes.c_ubyte * 1),
            ]

        # LRESULT and the pointer-sized params are 64-bit here; c_ssize_t is right on both.
        WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )

        class WNDCLASSW(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HANDLE),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        # Every signature spelled out. Without argtypes, ctypes widens a Python int to 32 bits
        # and HWND_MESSAGE (-3) arrives in the 64-bit handle slot as 0x00000000FFFFFFFD rather
        # than a sign-extended -3 — so CreateWindowExW fails with a null return and no clue.
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.RegisterClassW.restype = wintypes.ATOM
        user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [
            wintypes.DWORD,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.HWND,
            wintypes.HMENU,
            wintypes.HINSTANCE,
            wintypes.LPVOID,
        ]
        user32.RegisterPowerSettingNotification.restype = wintypes.HANDLE
        user32.RegisterPowerSettingNotification.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(GUID),
            wintypes.DWORD,
        ]
        user32.DestroyWindow.argtypes = [wintypes.HWND]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

        def handler(hwnd, msg, wparam, lparam):
            if msg == WM_POWERBROADCAST:
                self._on_broadcast(wparam, lparam, POWERBROADCAST_SETTING)
                # Documented requirement: return TRUE for PBT_APM* rather than passing them on.
                return 1

            if msg == WM_CLOSE:
                user32.DestroyWindow(hwnd)
                return 0

            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0

            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc_ref = WNDPROC(handler)

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = f"multi_deck_power_{threading.get_ident()}"

        wndclass = WNDCLASSW()
        wndclass.lpfnWndProc = self._wndproc_ref
        wndclass.hInstance = hinstance
        wndclass.lpszClassName = class_name

        if not user32.RegisterClassW(ctypes.byref(wndclass)):
            raise ctypes.WinError(ctypes.get_last_error())

        hwnd = user32.CreateWindowExW(
            0,
            class_name,
            class_name,
            0,
            0,
            0,
            0,
            0,
            wintypes.HWND(HWND_MESSAGE),
            None,
            hinstance,
            None,
        )
        if not hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

        guid = GUID(
            _DISPLAY_STATE_GUID[0],
            _DISPLAY_STATE_GUID[1],
            _DISPLAY_STATE_GUID[2],
            (ctypes.c_ubyte * 8)(*_DISPLAY_STATE_GUID[3]),
        )
        self._registration = user32.RegisterPowerSettingNotification(
            hwnd, ctypes.byref(guid), DEVICE_NOTIFY_WINDOW_HANDLE
        )
        if not self._registration:
            # Not fatal: the PBT_APM* backstop still works, it just arrives later.
            log.debug("could not register for display-state notifications")

        self._hwnd = hwnd
        self._display_guid = guid
        log.info("power monitor started")
        ready.set()

        message = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

        self._hwnd = None
        log.debug("power monitor stopped")

    # -- events --------------------------------------------------------------------------

    def _on_broadcast(self, wparam: int, lparam: int, setting_type) -> None:
        if wparam == PBT_APMSUSPEND:
            self._sleep("suspend")
            return

        if wparam in (PBT_APMRESUMEAUTOMATIC, PBT_APMRESUMESUSPEND):
            self._wake("resume")
            return

        if wparam != PBT_POWERSETTINGCHANGE:
            return

        try:
            import ctypes

            setting = ctypes.cast(lparam, ctypes.POINTER(setting_type)).contents
        except Exception:  # pragma: no cover - a malformed broadcast is not worth crashing on
            log.debug("unreadable power setting broadcast", exc_info=True)
            return

        if setting.PowerSetting.Data1 != _DISPLAY_STATE_GUID[0]:
            return

        self.on_display_state(setting.Data[0])

    def on_display_state(self, state: int) -> None:
        """0 off, 1 on, 2 dimmed.

        Separate from the broadcast decoding above so the decision is reachable without
        conjuring a POWERBROADCAST_SETTING — the ctypes cast is the part that has to be
        verified against a live window, this part is just a rule.
        """
        if state == 0:
            self._sleep("display off")
        elif state == 1:
            self._wake("display on")
        # 2 is "dimmed" — the user is still there, so it is deliberately ignored.

    def _sleep(self, why: str) -> None:
        if self._asleep:
            return
        self._asleep = True
        log.info("PC going to sleep (%s)", why)
        self._safely(self._on_sleep)

    def _wake(self, why: str) -> None:
        if not self._asleep:
            return
        self._asleep = False
        log.info("PC awake (%s)", why)
        self._safely(self._on_wake)

    @staticmethod
    def _safely(callback: Callable[[], None]) -> None:
        """A raising callback must not kill the message pump and take future events with it."""
        try:
            callback()
        except Exception:
            log.exception("power callback failed")
