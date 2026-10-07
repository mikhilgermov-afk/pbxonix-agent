"""System metrics, read straight from /proc and statvfs.

No psutil: keeping the agent dependency-free is what lets the installer do an
offline install on a PBX with no route to PyPI.
"""

import os
import shutil
from typing import Dict, Optional, Tuple


def _read(path: str) -> Optional[str]:
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return None


def _cpu_totals() -> Optional[Tuple[int, int]]:
    """Return (busy, total) jiffies from the aggregate cpu line."""
    content = _read("/proc/stat")
    if not content:
        return None
    for line in content.splitlines():
        if not line.startswith("cpu "):
            continue
        try:
            values = [int(v) for v in line.split()[1:]]
        except ValueError:
            return None
        if len(values) < 4:
            return None
        idle = values[3] + (values[4] if len(values) > 4 else 0)  # idle + iowait
        total = sum(values)
        return total - idle, total
    return None


class CpuSampler:
    """CPU usage is a rate, so it needs two readings.

    The first call has nothing to compare against and returns None rather than
    inventing a number.
    """

    def __init__(self) -> None:
        self._previous: Optional[Tuple[int, int]] = None

    def sample(self) -> Optional[float]:
        current = _cpu_totals()
        if current is None:
            return None
        previous, self._previous = self._previous, current
        if previous is None:
            return None

        busy_delta = current[0] - previous[0]
        total_delta = current[1] - previous[1]
        if total_delta <= 0:
            return None
        return round(max(0.0, min(100.0, 100.0 * busy_delta / total_delta)), 1)


def memory() -> Dict[str, Optional[float]]:
    content = _read("/proc/meminfo")
    if not content:
        return {}
    fields: Dict[str, int] = {}
    for line in content.splitlines():
        key, _, rest = line.partition(":")
        try:
            fields[key.strip()] = int(rest.split()[0])  # kB
        except (ValueError, IndexError):
            continue

    total_kb = fields.get("MemTotal", 0)
    available_kb = fields.get("MemAvailable", fields.get("MemFree", 0))
    swap_total_kb = fields.get("SwapTotal", 0)
    swap_free_kb = fields.get("SwapFree", 0)
    used_kb = max(0, total_kb - available_kb)

    return {
        "ram_total_mb": total_kb // 1024 if total_kb else None,
        "ram_used_mb": used_kb // 1024 if total_kb else None,
        # Derived from MemAvailable rather than MemFree: page cache is not
        # memory pressure, and alerting on MemFree would page someone every
        # night during a backup.
        "ram_pct": round(100.0 * used_kb / total_kb, 1) if total_kb else None,
        "swap_total_mb": swap_total_kb // 1024 if swap_total_kb else 0,
        "swap_used_mb": (swap_total_kb - swap_free_kb) // 1024 if swap_total_kb else 0,
    }


def disk(path: str = "/") -> Dict[str, Optional[float]]:
    out: Dict[str, Optional[float]] = {
        "disk_total_gb": None,
        "disk_used_gb": None,
        "disk_pct": None,
        "inode_pct": None,
    }
    try:
        usage = shutil.disk_usage(path)
        out["disk_total_gb"] = round(usage.total / (1024**3), 2)
        out["disk_used_gb"] = round(usage.used / (1024**3), 2)
        out["disk_pct"] = round(100.0 * usage.used / usage.total, 1) if usage.total else None
    except OSError:
        pass

    try:
        stat = os.statvfs(path)
        if stat.f_files:
            used = stat.f_files - stat.f_ffree
            # Inode exhaustion looks exactly like a full disk to Asterisk while
            # df still reports free space, so it is tracked separately.
            out["inode_pct"] = round(100.0 * used / stat.f_files, 1)
    except OSError:
        pass
    return out


def load_average() -> Dict[str, Optional[float]]:
    try:
        one, five, fifteen = os.getloadavg()
    except OSError:
        return {"load_1": None, "load_5": None, "load_15": None}
    return {"load_1": round(one, 2), "load_5": round(five, 2), "load_15": round(fifteen, 2)}


def uptime_seconds() -> Optional[int]:
    content = _read("/proc/uptime")
    if not content:
        return None
    try:
        return int(float(content.split()[0]))
    except (ValueError, IndexError):
        return None


def collect(sampler: CpuSampler, disk_path: str = "/") -> Dict[str, Optional[float]]:
    metrics: Dict[str, Optional[float]] = {"cpu_pct": sampler.sample()}
    metrics.update(load_average())
    metrics.update(memory())
    metrics.update(disk(disk_path))
    metrics["uptime_seconds"] = uptime_seconds()
    return metrics
