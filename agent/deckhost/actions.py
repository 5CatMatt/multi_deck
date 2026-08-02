"""Executes the agent-side half of an action.

Device-local steps (`hid`, `hid_text`, `media`, `page`) are never executed here. When a
sequence mixes both kinds, the agent is the sequencer: it runs its own steps in order and
sends the local ones back to the device as `hid_exec`, so ordering across the boundary
stays correct.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .config import DEVICE_LOCAL_TYPES

log = logging.getLogger(__name__)

# Called with a device-local action that the device should perform.
HidExecCallback = Callable[[dict[str, Any]], Awaitable[None]]


class ActionRunner:
    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._ahk: Any | None = None
        self._ahk_tried = False

    # -- AutoHotkey ---------------------------------------------------------------------

    def _get_ahk(self) -> Any | None:
        """Lazily starts a persistent AutoHotkey process.

        Persistent rather than one process per keypress: spawning AutoHotkey per action adds
        roughly a hundred milliseconds to every macro, which is very noticeable on a deck.
        """
        if self._ahk_tried:
            return self._ahk

        self._ahk_tried = True
        try:
            from ahk import AHK  # type: ignore[import-not-found]

            self._ahk = AHK(version="v2")
            log.info("AutoHotkey backend ready")
        except Exception as exc:  # pragma: no cover - depends on local install
            log.warning("AutoHotkey unavailable (%s); ahk actions will be skipped", exc)
            self._ahk = None

        return self._ahk

    # -- dispatch -----------------------------------------------------------------------

    async def run(
        self,
        action: dict[str, Any],
        on_hid_exec: HidExecCallback | None = None,
    ) -> None:
        kind = action.get("type")

        if kind in DEVICE_LOCAL_TYPES:
            if on_hid_exec is not None:
                await on_hid_exec(action)
            else:
                log.debug("ignoring device-local action %r with no callback", kind)
            return

        handler = {
            "launch": self._launch,
            "shell": self._shell,
            "ahk": self._ahk_action,
            "delay": self._delay,
            "seq": self._seq,
        }.get(kind)

        if handler is None:
            log.warning("unknown action type %r", kind)
            return

        try:
            await handler(action, on_hid_exec)
        except Exception:
            log.exception("action %r failed", kind)

    async def _seq(
        self, action: dict[str, Any], on_hid_exec: HidExecCallback | None
    ) -> None:
        for step in action.get("steps", []):
            await self.run(step, on_hid_exec)

    async def _delay(
        self, action: dict[str, Any], _: HidExecCallback | None
    ) -> None:
        await asyncio.sleep(int(action.get("ms", 0)) / 1000.0)

    async def _launch(
        self, action: dict[str, Any], _: HidExecCallback | None
    ) -> None:
        target = action.get("target")
        if not target:
            log.warning("launch action has no target")
            return

        args = [str(a) for a in action.get("args", [])]
        cwd = action.get("cwd")

        if self.dry_run:
            log.info("[dry-run] launch %s %s", target, args)
            return

        # URLs and documents go through the shell so the user's default handler wins.
        if "://" in target or (not args and shutil.which(target) is None):
            log.info("launch (shell) %s", target)
            os.startfile(target)  # type: ignore[attr-defined]  # Windows-only, by design
            return

        log.info("launch %s %s", target, args)
        subprocess.Popen([target, *args], cwd=cwd, shell=False)

    async def _shell(
        self, action: dict[str, Any], _: HidExecCallback | None
    ) -> None:
        cmd = action.get("cmd")
        if not cmd:
            log.warning("shell action has no cmd")
            return

        if self.dry_run:
            log.info("[dry-run] shell %s", cmd)
            return

        log.info("shell %s", cmd)
        subprocess.Popen(cmd, shell=True)

    async def _ahk_action(
        self, action: dict[str, Any], _: HidExecCallback | None
    ) -> None:
        fn = action.get("fn")
        if not fn:
            log.warning("ahk action has no fn")
            return

        if self.dry_run:
            log.info("[dry-run] ahk %s(%s)", fn, action.get("args", []))
            return

        ahk = self._get_ahk()
        if ahk is None:
            return

        args = [str(a) for a in action.get("args", [])]
        log.info("ahk %s(%s)", fn, args)

        # Window-management helpers live in agent/ahk/lib.ahk; the function name from
        # deck.json selects one.
        await asyncio.to_thread(ahk.run_script, _ahk_call_script(fn, args))


def _ahk_call_script(fn: str, args: list[str]) -> str:
    """Builds a one-shot AHK v2 script that calls a helper from agent/ahk/lib.ahk."""
    lib = Path(__file__).resolve().parents[1] / "ahk" / "lib.ahk"
    quoted = ", ".join('"' + a.replace('"', '""') + '"' for a in args)
    return f'#Include "{lib}"\n{fn}({quoted})\n'
