"""One-off host identification, gathered at enrollment and on start-up."""

import os
import platform
import re
import shutil
import socket
import subprocess
from typing import Dict, List, Optional

_ASTERISK_VERSION = re.compile(r"Asterisk\s+(\S+)")


def _run(argv: List[str], timeout: float = 5.0) -> Optional[str]:
    """Run a fixed command list.

    Never a shell string, so there is nothing for an attacker-controlled
    hostname or version banner to inject into.
    """
    binary = shutil.which(argv[0])
    if binary is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - argv is a literal list
            [binary, *argv[1:]],
            # capture_output and text= are both 3.7+. These spellings mean
            # exactly the same thing and work on 3.6, which is what a stock
            # Issabel 4 box runs.
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def os_release() -> Dict[str, str]:
    values: Dict[str, str] = {}
    try:
        with open("/etc/os-release", encoding="utf-8") as handle:
            for line in handle:
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def asterisk_version() -> Optional[str]:
    out = _run(["asterisk", "-V"])
    if not out:
        return None
    match = _ASTERISK_VERSION.search(out)
    return match.group(1) if match else out


def freepbx_version() -> Optional[str]:
    if not (os.path.exists("/etc/freepbx.conf") or os.path.isdir("/var/www/html/admin")):
        return None
    out = _run(["fwconsole", "--version"])
    if out:
        match = re.search(r"(\d+\.\d+[\w.]*)", out)
        if match:
            return match.group(1)
    return "present"


def issabel_version() -> Optional[str]:
    markers = ("/etc/issabel.conf", "/etc/issabel-release", "/usr/share/issabel")
    if not any(os.path.exists(path) for path in markers):
        return None
    try:
        with open("/etc/issabel-release", encoding="utf-8") as handle:
            return handle.read().strip() or "present"
    except OSError:
        return "present"


def platform_kind() -> str:
    """Most specific match wins: FreePBX and Issabel both imply Asterisk."""
    if issabel_version():
        return "issabel"
    if freepbx_version():
        return "freepbx"
    if asterisk_version():
        return "asterisk"
    return "unknown"


def total_ram_mb() -> Optional[int]:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def total_disk_gb(path: str = "/") -> Optional[float]:
    try:
        return round(shutil.disk_usage(path).total / (1024**3), 2)
    except OSError:
        return None


def collect() -> Dict[str, object]:
    release = os_release()
    return {
        "hostname": socket.gethostname(),
        "os_name": release.get("NAME") or platform.system(),
        "os_version": release.get("VERSION_ID") or platform.release(),
        "architecture": platform.machine(),
        "platform": platform_kind(),
        "asterisk_version": asterisk_version(),
        "freepbx_version": freepbx_version(),
        "issabel_version": issabel_version(),
        "cpu_cores": os.cpu_count(),
        "ram_total_mb": total_ram_mb(),
        "disk_total_gb": total_disk_gb(),
    }
