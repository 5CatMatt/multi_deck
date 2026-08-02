"""Frame encoding and decoding for the multi_deck wire protocol.

See docs/protocol.md. Newline-delimited JSON, so this module is deliberately tiny — the
value is in having exactly one place that knows the frame shapes.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

PROTO_VERSION = 1

# Matches MD_LINK_RX_MAX in the firmware. A line longer than this means the stream has
# desynchronised, and both sides drop it rather than buffering without bound.
MAX_LINE_BYTES = 8192


class ProtocolError(Exception):
    """Raised when the peer speaks a version we cannot interoperate with."""


def encode(frame: dict[str, Any]) -> bytes:
    """Serialises one frame, including its terminating newline."""
    # separators without spaces keeps frames compact; the device parses either way.
    return (json.dumps(frame, separators=(",", ":")) + "\n").encode("utf-8")


class FrameReader:
    """Accumulates bytes and yields complete frames.

    Tolerates split reads, which serial ports produce constantly.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self.dropped_lines = 0

    def feed(self, chunk: bytes) -> Iterator[dict[str, Any]]:
        self._buf.extend(chunk)

        while True:
            index = self._buf.find(b"\n")

            if index < 0:
                if len(self._buf) > MAX_LINE_BYTES:
                    # No newline in an over-long buffer: resynchronise rather than grow.
                    self._buf.clear()
                    self.dropped_lines += 1
                return

            line = bytes(self._buf[:index])
            del self._buf[: index + 1]

            line = line.strip()
            if not line:
                continue

            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                self.dropped_lines += 1
                continue

            if isinstance(frame, dict):
                yield frame
            else:
                self.dropped_lines += 1


# --- host -> device -------------------------------------------------------------------


def welcome(host: str, rev: int) -> dict[str, Any]:
    return {"t": "welcome", "proto": PROTO_VERSION, "host": host, "rev": rev}


def ping(seq: int) -> dict[str, Any]:
    return {"t": "ping", "seq": seq}


def identify() -> dict[str, Any]:
    """Asks the device to send its `hello` now.

    The device only announces itself unprompted while it believes no session exists, so an
    agent that reconnects inside that window would otherwise wait in silence. Sending this on
    connect makes the handshake work regardless of which side arrives first.
    """
    return {"t": "identify"}


def layout(rev: int, data: dict[str, Any]) -> dict[str, Any]:
    return {"t": "layout", "rev": rev, "data": data}


def toast(msg: str, level: str = "info") -> dict[str, Any]:
    return {"t": "toast", "msg": msg, "lvl": level}


def hid_exec(action: dict[str, Any]) -> dict[str, Any]:
    return {"t": "hid_exec", "action": action}


def backlight(value: int) -> dict[str, Any]:
    return {"t": "backlight", "v": max(0, min(100, int(value)))}


def stats(sample: dict[str, Any]) -> dict[str, Any]:
    return {"t": "stats", **sample}


# --- device -> host (constructed by the simulator and the tests) ----------------------


def hello(fw: str = "0.0.0", rev: int = 0, assets: str | None = None) -> dict[str, Any]:
    """`assets` omitted entirely means "no card mounted", matching Link::sendHello().

    An empty string is a different answer — a mounted card that carries no stamp — so None
    and "" must not collapse into each other here either.
    """
    frame = {"t": "hello", "proto": PROTO_VERSION, "fw": fw, "dev": "multi_deck", "rev": rev}
    if assets is not None:
        frame["assets"] = assets
    return frame


def press(button_id: str, page: str = "") -> dict[str, Any]:
    return {"t": "press", "id": button_id, "page": page}


def pong(seq: int) -> dict[str, Any]:
    return {"t": "pong", "seq": seq}


def layout_req() -> dict[str, Any]:
    return {"t": "layout_req"}


def check_proto(frame: dict[str, Any]) -> None:
    """Rejects a peer speaking a different protocol version.

    Failing loudly here is much cheaper to diagnose than fields quietly going missing.
    """
    peer = frame.get("proto")
    if peer != PROTO_VERSION:
        raise ProtocolError(
            f"protocol mismatch: peer speaks {peer!r}, this agent speaks {PROTO_VERSION}"
        )
