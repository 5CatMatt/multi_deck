"""Transports to the deck.

`SerialLink` talks to the real device over USB CDC. `SimulatedLink` is an in-process fake
device, so the action dispatch and stats pipeline can be developed and tested with no
hardware attached — which is most of the agent.
"""

from __future__ import annotations

import abc
import asyncio
import logging
from typing import Any

from . import protocol

log = logging.getLogger(__name__)

# Espressif's USB vendor id. The ESP32-S3's native USB uses it unless overridden.
ESPRESSIF_VID = 0x303A

# Cap on a single write. Without one pyserial blocks indefinitely, which would hang the
# event loop thread pool rather than surfacing a problem.
WRITE_TIMEOUT_S = 2.0


class LinkError(Exception):
    """Transport-level failure.

    Signals that this session is over but the agent should recover, as distinct from a bug
    worth crashing on. Unplugging the deck must not take the agent down with it.
    """


class Link(abc.ABC):
    @abc.abstractmethod
    async def open(self) -> None: ...

    @abc.abstractmethod
    async def read(self) -> bytes:
        """Returns the next chunk of bytes, or b'' when the link has closed."""

    @abc.abstractmethod
    async def write(self, data: bytes) -> None: ...

    async def close(self) -> None:
        return None


class SerialLink(Link):
    def __init__(self, port: str | None = None, baud: int = 115200) -> None:
        self.port = port
        self.baud = baud
        self._serial: Any | None = None
        self._open = False
        # Created in open(), where a running event loop is guaranteed.
        self._write_lock: asyncio.Lock | None = None

    @staticmethod
    def discover() -> str | None:
        """Finds the deck by USB vendor id, preferring a matching product string."""
        try:
            from serial.tools import list_ports
        except ImportError:
            log.error("pyserial not installed — run: pip install -r agent/requirements.txt")
            return None

        candidates = []
        for info in list_ports.comports():
            if info.vid == ESPRESSIF_VID:
                product = (info.product or "") + (info.description or "")
                candidates.append((("multi_deck" in product), info.device))

        if not candidates:
            return None

        # A device that names itself wins over a bare Espressif VID match.
        candidates.sort(reverse=True)
        return candidates[0][1]

    async def open(self) -> None:
        import serial

        port = self.port or self.discover()
        if port is None:
            raise RuntimeError(
                "no deck found — is it plugged into the native USB port (port B)?"
            )

        self._serial = serial.Serial(
            port, self.baud, timeout=0.1, write_timeout=WRITE_TIMEOUT_S
        )
        self._write_lock = asyncio.Lock()
        self._open = True
        self.port = port
        log.info("serial link open on %s", port)

    @property
    def is_open(self) -> bool:
        return self._open

    async def read(self) -> bytes:
        if not self._open or self._serial is None:
            return b""

        def _read() -> bytes:
            assert self._serial is not None
            waiting = self._serial.in_waiting
            return self._serial.read(waiting if waiting else 1)

        # pyserial is blocking, so keep it off the event loop.
        try:
            return await asyncio.to_thread(_read)
        except Exception as exc:
            self._open = False
            raise LinkError(f"read failed: {exc}") from exc

    async def write(self, data: bytes) -> None:
        if not self._open or self._serial is None or self._write_lock is None:
            raise LinkError("port is not open")

        # Serialised deliberately. pyserial keeps a *single* OVERLAPPED structure per port on
        # Windows, so two concurrent write() calls corrupt each other's completion state:
        # GetOverlappedResult then returns the other write's byte count, the length check
        # fails, and it raises SerialTimeoutException('Write timeout') on a perfectly healthy
        # port. The ping, stats and press paths all send independently, so without this lock
        # the collision is a matter of when, not if.
        async with self._write_lock:
            try:
                await asyncio.to_thread(self._serial.write, data)
            except Exception as exc:
                self._open = False
                raise LinkError(f"write failed: {exc}") from exc

    async def close(self) -> None:
        self._open = False
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None


class SimulatedLink(Link):
    """A fake deck that follows a script of button presses.

    Exercises the real code path: it performs the handshake, answers pings, and reports what
    the agent sends back — including `hid_exec` callbacks, which is how mixed-sequence
    ordering gets verified without hardware.
    """

    def __init__(
        self,
        script: list[str] | None = None,
        *,
        rev: int = 0,
        step_delay: float = 0.4,
    ) -> None:
        self.script = script or []
        self.rev = rev
        self.step_delay = step_delay

        self._outbound: asyncio.Queue[bytes] = asyncio.Queue()
        self._reader = protocol.FrameReader()
        self._closed = False
        self._script_task: asyncio.Task[None] | None = None

        # What the agent sent us, for assertions in tests.
        self.received: list[dict[str, Any]] = []
        self.hid_exec_calls: list[dict[str, Any]] = []

    async def open(self) -> None:
        await self._outbound.put(protocol.encode(protocol.hello(fw="sim", rev=self.rev)))
        log.info("simulated deck attached (layout rev %d)", self.rev)

    async def read(self) -> bytes:
        if self._closed:
            return b""
        return await self._outbound.get()

    async def write(self, data: bytes) -> None:
        for frame in self._reader.feed(data):
            self.received.append(frame)
            await self._react(frame)

    async def _react(self, frame: dict[str, Any]) -> None:
        kind = frame.get("t")

        if kind == "welcome":
            log.info("simulated deck welcomed by %s", frame.get("host"))
            if self._script_task is None:
                self._script_task = asyncio.create_task(self._run_script())

        elif kind == "ping":
            await self._outbound.put(protocol.encode(protocol.pong(frame.get("seq", 0))))

        elif kind == "identify":
            # Mirrors the firmware: answer with hello whatever the session state.
            await self._outbound.put(
                protocol.encode(protocol.hello(fw="sim", rev=self.rev))
            )

        elif kind == "layout":
            self.rev = frame.get("rev", self.rev)
            log.info("simulated deck accepted layout rev %d", self.rev)

        elif kind == "hid_exec":
            action = frame.get("action", {})
            self.hid_exec_calls.append(action)
            log.info("simulated deck performs local action: %s", action.get("type"))

        elif kind == "toast":
            log.info("simulated deck toast: %s", frame.get("msg"))

    async def _run_script(self) -> None:
        for button_id in self.script:
            await asyncio.sleep(self.step_delay)
            if self._closed:
                return
            log.info("simulated deck presses %s", button_id)
            await self._outbound.put(protocol.encode(protocol.press(button_id, "sim")))

    async def close(self) -> None:
        self._closed = True
        if self._script_task is not None:
            self._script_task.cancel()
        await self._outbound.put(b"")
