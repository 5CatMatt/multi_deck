"""System stats providers.

Every provider is optional and degrades to "field absent" rather than raising or reporting
a stale value. The device renders a missing field as "--", so a machine with no NVIDIA GPU
or no LibreHardwareMonitor still gets a working stats page.
"""

from __future__ import annotations

import logging
import math
import time
import warnings
from typing import Any

log = logging.getLogger(__name__)

LHM_URL = "http://localhost:8085/data.json"


class StatsCollector:
    def __init__(self, *, synthetic: bool = False) -> None:
        self.synthetic = synthetic
        self._psutil: Any | None = None
        self._nvml: Any | None = None
        self._last_net: tuple[float, float, float] | None = None
        self._warned: set[str] = set()

        if not synthetic:
            self._psutil = self._try_import("psutil")
            self._init_nvml()

    def _try_import(self, name: str) -> Any | None:
        try:
            return __import__(name)
        except ImportError:
            log.warning("%s not installed — related stats will be omitted", name)
            return None

    def _init_nvml(self) -> None:
        try:
            # The legacy `pynvml` distribution emits a FutureWarning on import telling the
            # user to install `nvidia-ml-py` instead. Both provide this same module name and
            # both work here, so the warning is noise the user can do nothing useful about.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", FutureWarning)
                import pynvml  # type: ignore[import-not-found]

            pynvml.nvmlInit()
            self._nvml = pynvml
            log.info("NVML ready — GPU stats available")
        except Exception:
            log.info("no NVIDIA GPU stats (NVML unavailable)")
            self._nvml = None

    # -- collection ---------------------------------------------------------------------

    def sample(self) -> dict[str, Any]:
        if self.synthetic:
            return self._synthetic()

        out: dict[str, Any] = {}

        self._collect_psutil(out)
        self._collect_nvml(out)
        self._collect_lhm(out)

        # `cpu` is the one field the protocol guarantees, so make sure it exists even if
        # psutil is missing entirely.
        out.setdefault("cpu", 0.0)
        return out

    def _collect_psutil(self, out: dict[str, Any]) -> None:
        ps = self._psutil
        if ps is None:
            return

        out["cpu"] = round(ps.cpu_percent(interval=None), 1)
        out["cpu_cores"] = [round(c, 1) for c in ps.cpu_percent(interval=None, percpu=True)]

        mem = ps.virtual_memory()
        out["mem"] = round(mem.percent, 1)
        out["mem_used_gb"] = round(mem.used / 1024**3, 1)
        out["mem_total_gb"] = round(mem.total / 1024**3, 1)

        try:
            out["disk"] = round(ps.disk_usage("C:\\").percent, 1)
        except OSError:
            pass

        net = ps.net_io_counters()
        now = time.monotonic()
        if self._last_net is not None:
            prev_t, prev_sent, prev_recv = self._last_net
            dt = now - prev_t
            if dt > 0:
                out["net_up_mbps"] = round((net.bytes_sent - prev_sent) * 8 / dt / 1e6, 2)
                out["net_down_mbps"] = round((net.bytes_recv - prev_recv) * 8 / dt / 1e6, 2)
        self._last_net = (now, net.bytes_sent, net.bytes_recv)

        out["uptime_s"] = int(time.time() - ps.boot_time())

    def _collect_nvml(self, out: dict[str, Any]) -> None:
        if self._nvml is None:
            return

        try:
            handle = self._nvml.nvmlDeviceGetHandleByIndex(0)
            util = self._nvml.nvmlDeviceGetUtilizationRates(handle)
            out["gpu"] = float(util.gpu)

            mem = self._nvml.nvmlDeviceGetMemoryInfo(handle)
            out["gpu_mem"] = round(mem.used / mem.total * 100, 1)

            out["gpu_temp"] = float(
                self._nvml.nvmlDeviceGetTemperature(handle, self._nvml.NVML_TEMPERATURE_GPU)
            )
        except Exception:
            self._warn_once("nvml", "NVML query failed — dropping GPU fields")

    def _collect_lhm(self, out: dict[str, Any]) -> None:
        """CPU package temperature via LibreHardwareMonitor's HTTP server.

        Reading temperatures on Windows otherwise means a kernel driver; LHM already ships
        one and exposes it over localhost, so this is by far the cheapest route.
        """
        if "cpu_temp" in out:
            return

        try:
            import urllib.request

            with urllib.request.urlopen(LHM_URL, timeout=0.4) as response:
                import json

                data = json.load(response)
        except Exception:
            self._warn_once(
                "lhm", "LibreHardwareMonitor not reachable — temperatures omitted"
            )
            return

        temp = _find_lhm_cpu_temp(data)
        if temp is not None:
            out["cpu_temp"] = temp

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.info(message)

    def _synthetic(self) -> dict[str, Any]:
        """Plausible-looking data for --simulate, so the stats path can be exercised."""
        t = time.monotonic()
        return {
            "cpu": round(45 + 35 * math.sin(t / 3), 1),
            "cpu_cores": [round(40 + 40 * math.sin(t / 3 + i), 1) for i in range(4)],
            "cpu_temp": round(52 + 8 * math.sin(t / 7), 1),
            "mem": round(60 + 5 * math.sin(t / 11), 1),
            "mem_used_gb": 19.4,
            "mem_total_gb": 32.0,
            "gpu": round(25 + 20 * math.sin(t / 5), 1),
            "gpu_temp": round(55 + 6 * math.sin(t / 9), 1),
            "net_up_mbps": round(abs(2 * math.sin(t / 2)), 2),
            "net_down_mbps": round(abs(12 * math.sin(t / 4)), 2),
            "uptime_s": int(t),
        }


def _find_lhm_cpu_temp(node: Any) -> float | None:
    """Walks LibreHardwareMonitor's nested sensor tree for a CPU package temperature."""
    if isinstance(node, dict):
        text = str(node.get("Text", ""))
        value = str(node.get("Value", ""))

        if "Package" in text or "CPU Package" in text:
            if "°C" in value:
                try:
                    return float(value.replace("°C", "").strip())
                except ValueError:
                    pass

        for child in node.get("Children", []) or []:
            found = _find_lhm_cpu_temp(child)
            if found is not None:
                return found

    elif isinstance(node, list):
        for child in node:
            found = _find_lhm_cpu_temp(child)
            if found is not None:
                return found

    return None
