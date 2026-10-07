"""Asterisk state.

Phase 1 reads what can be had without credentials: whether the process is up,
its version, and the channel count from the local CLI socket. AMI and PJSIP
detail arrive in Phase 2 -- see docs/AGENT.md.

Nothing here ever runs as root and nothing is passed through a shell.
"""

import logging
import re
import shutil
import subprocess
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("pbxonix.agent")

# Why the local CLI last refused to answer. Kept so the reason can be reported
# instead of silently turning into a dash on the dashboard -- an operator
# looking at a blank "active calls" has no way to tell a quiet PBX from a
# permission problem.
_last_cli_error = None  # type: Optional[str]
_reported_cli_error = None  # type: Optional[str]
# Never asked is not the same as asked and fine. collect() skips the CLI
# entirely when Asterisk is not running, and reporting that as "ok" would
# be the exact kind of confident wrong answer this module avoids elsewhere.
_cli_attempted = False

_CHANNEL_COUNT = re.compile(r"(\d+)\s+active channel")
_UPTIME = re.compile(
    r"System uptime:\s*(?:(\d+)\s*weeks?,?\s*)?(?:(\d+)\s*days?,?\s*)?"
    r"(?:(\d+)\s*hours?,?\s*)?(?:(\d+)\s*minutes?,?\s*)?(?:(\d+)\s*seconds?)?",
    re.IGNORECASE,
)


def cli_error() -> Optional[str]:
    """The reason the last CLI call failed, if one did."""
    return _last_cli_error


def cli_attempted() -> bool:
    """Whether the local CLI has been asked anything at all."""
    return _cli_attempted


def _note_cli_error(reason: Optional[str]) -> None:
    """Remember why, and say so once rather than every cycle."""
    global _last_cli_error, _reported_cli_error, _cli_attempted
    _cli_attempted = True
    _last_cli_error = reason
    if reason and reason != _reported_cli_error:
        _reported_cli_error = reason
        log.warning(
            "the local Asterisk CLI is not answering (%s); active call and channel "
            "counts will be blank. Check that the agent user can reach "
            "asterisk.ctl -- `sudo -u pbxonix asterisk -rx 'core show version'`",
            reason,
        )
    elif not reason:
        _reported_cli_error = None


def _run(argv: List[str], timeout: float = 5.0) -> Optional[str]:
    out, _ = _run_detailed(argv, timeout)
    return out


def _run_detailed(argv: List[str], timeout: float = 5.0) -> Tuple[Optional[str], Optional[str]]:
    """Returns (stdout, failure reason). Exactly one of them is meaningful."""
    binary = shutil.which(argv[0])
    if binary is None:
        return None, "{} not found on PATH".format(argv[0])
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
    except subprocess.TimeoutExpired:
        return None, "{} timed out after {}s".format(argv[0], timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "{}: {}".format(type(exc).__name__, exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return None, "exit {}{}".format(
            result.returncode, ": " + detail[0][:160] if detail else ""
        )
    return result.stdout.strip() or None, None


def is_running() -> Optional[bool]:
    """True/False when we can tell, None when we cannot.

    The distinction matters: reporting False for "could not determine" would
    raise a false ASTERISK_STOPPED alert on every PBX where the agent lacks
    permission to ask.
    """
    if shutil.which("systemctl"):
        out = _run(["systemctl", "is-active", "asterisk"])
        if out is not None:
            return out.strip() == "active"
        # is-active exits non-zero when inactive, which _run turns into None.
        # Fall through to the CLI probe rather than guessing.

    if shutil.which("asterisk"):
        return _run(["asterisk", "-rx", "core show version"]) is not None

    return None


def _cli(command: str) -> Optional[str]:
    """Ask the running Asterisk. Records why not, if not."""
    out, reason = _run_detailed(["asterisk", "-rx", command])
    _note_cli_error(reason)
    return out


def channel_counts() -> Dict[str, Optional[int]]:
    """Channel count from the local CLI. Calls are deliberately not reported.

    `core show channels count` prints an "active calls" line, but it is the same
    counter as CoreStatus.CoreCurrentCalls -- a channel count under another name.
    Passing it through produced two identical numbers on the dashboard labelled
    as different things.

    Separating calls from channels needs the per-channel list, which only the AMI
    route provides. Where AMI is configured it fills this in; where it is not,
    a blank is the honest answer. A wrong number is worse than a dash.
    """
    out = _cli("core show channels count")
    if not out:
        return {"active_channels": None, "active_calls": None}

    channels = _CHANNEL_COUNT.search(out)
    return {
        "active_channels": int(channels.group(1)) if channels else None,
        "active_calls": None,
    }


def uptime_seconds() -> Optional[int]:
    out = _cli("core show uptime")
    if not out:
        return None
    match = _UPTIME.search(out)
    if not match:
        return None
    weeks, days, hours, minutes, seconds = (int(g or 0) for g in match.groups())
    total = ((weeks * 7 + days) * 24 + hours) * 3600 + minutes * 60 + seconds
    return total or None


def version() -> Optional[str]:
    # `asterisk -V` reads the binary and needs no running Asterisk, so it keeps
    # working when the CLI socket does not. That fallback is why a version can
    # appear while every other CLI-derived field is blank -- a useful tell.
    out = _cli("core show version") or _run(["asterisk", "-V"])
    if not out:
        return None
    match = re.search(r"Asterisk\s+(\S+)", out)
    return match.group(1) if match else None


def collect() -> Dict[str, object]:
    running = is_running()
    metrics: Dict[str, object] = {"running": running}
    if running is not True:
        # A stopped Asterisk has no channels to count, and asking would just
        # burn a five-second subprocess timeout on every cycle.
        metrics.update({"active_channels": None, "active_calls": None, "version": None})
        return metrics

    metrics.update(channel_counts())
    metrics["version"] = version()
    metrics["uptime_seconds"] = uptime_seconds()
    return metrics
