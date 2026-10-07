"""Recording activity, from the filesystem.

The signal worth having is the silent failure: calls are connecting, but nothing
is being written to disk. Catching that needs one number -- when the newest
recording appeared -- and everything here is arranged around getting it cheaply
and honestly.

**Privacy.** Asterisk names recordings after the call:
``out-15551234567-101-20260824-141233-1724508753.42.wav``. That filename is
personal data. Nothing here ever returns, logs or transmits a name or a path
below the configured roots -- only counts, sizes and timestamps.

**Cost.** A busy PBX accumulates hundreds of thousands of files, and walking all
of them every cycle would make the monitoring agent the heaviest thing on the
box. Two things keep it bounded:

* directories are visited newest-first, so a walk that is cut short has still
  seen the newest files and ``last_recording_at`` stays correct;
* the walk stops at a wall-clock limit and an entry limit, and says so.

Totals are best-effort under those limits. Freshness is not.
"""

import os
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Every codec Asterisk writes by default, plus the usual converted formats.
AUDIO_SUFFIXES = frozenset(
    {".wav", ".wav49", ".gsm", ".mp3", ".ogg", ".g722", ".g729", ".alaw", ".ulaw", ".sln", ".raw"}
)

DEFAULT_MAX_SECONDS = 10.0
DEFAULT_MAX_ENTRIES = 200_000


class ScanResult:
    def __init__(self) -> None:
        self.total_files = 0
        self.total_size_bytes = 0
        self.new_files = 0
        self.last_recording_at: Optional[float] = None
        self.truncated = False
        self.missing_paths: List[str] = []

    def to_payload(self) -> Dict[str, Any]:
        """Only aggregates. There is deliberately no field for a filename."""
        return {
            # Under truncation these are a floor, not a total, so they are sent
            # as None rather than as a number that would silently be wrong.
            "total_files": None if self.truncated else self.total_files,
            "total_size_bytes": None if self.truncated else self.total_size_bytes,
            "new_files": self.new_files,
            "last_recording_at": self.last_recording_at,
            "truncated": self.truncated,
            "paths_missing": len(self.missing_paths),
        }


def _is_audio(name: str) -> bool:
    dot = name.rfind(".")
    return dot != -1 and name[dot:].lower() in AUDIO_SUFFIXES


def _sorted_dirs(entries: Iterable[os.DirEntry]) -> List[Tuple[float, os.DirEntry]]:
    """Newest directory first.

    FreePBX and Asterisk lay the spool out as YYYY/MM/DD, so descending by
    mtime reaches today's recordings almost immediately. That is what makes a
    truncated walk still produce a correct ``last_recording_at``.
    """
    ranked: List[Tuple[float, os.DirEntry]] = []
    for entry in entries:
        try:
            ranked.append((entry.stat(follow_symlinks=False).st_mtime, entry))
        except OSError:
            continue
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    return ranked


def scan(
    paths: Iterable[str],
    since: Optional[float] = None,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> ScanResult:
    """Walk the recording roots and summarise what is there.

    ``since`` is the newest mtime seen last time; files newer than it are
    counted as new. On the first run it is None and ``new_files`` stays 0 --
    everything on disk is history, not activity.
    """
    result = ScanResult()
    deadline = time.monotonic() + max_seconds
    seen = 0

    for root in paths:
        if not os.path.isdir(root):
            result.missing_paths.append(root)
            continue

        # Explicit stack rather than os.walk: os.walk gives no control over the
        # order directories are visited, and the order is the whole trick.
        stack: List[str] = [root]
        while stack:
            if time.monotonic() > deadline or seen >= max_entries:
                result.truncated = True
                break

            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    entries = list(it)
            except OSError:
                # Unreadable directory. Skipping it is better than aborting the
                # whole scan; the agent runs unprivileged by design.
                continue

            subdirs: List[os.DirEntry] = []
            for entry in entries:
                if seen >= max_entries:
                    result.truncated = True
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        subdirs.append(entry)
                        continue
                    if not entry.is_file(follow_symlinks=False) or not _is_audio(entry.name):
                        continue
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue

                seen += 1
                result.total_files += 1
                result.total_size_bytes += info.st_size
                if result.last_recording_at is None or info.st_mtime > result.last_recording_at:
                    result.last_recording_at = info.st_mtime
                if since is not None and info.st_mtime > since:
                    result.new_files += 1

            # Pushed oldest-first so the newest is popped first.
            for _mtime, entry in reversed(_sorted_dirs(subdirs)):
                stack.append(entry.path)

    return result


class RecordingScanner:
    """Holds the previous newest-mtime so 'new since last time' means something."""

    def __init__(
        self,
        paths: Iterable[str],
        max_seconds: float = DEFAULT_MAX_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
    ) -> None:
        self.paths = list(paths)
        self.max_seconds = max_seconds
        self.max_entries = max_entries
        self._watermark: Optional[float] = None

    def collect(self) -> Optional[Dict[str, Any]]:
        """Scan once. Returns None when no paths are configured."""
        if not self.paths:
            return None

        started = time.monotonic()
        result = scan(self.paths, self._watermark, self.max_seconds, self.max_entries)

        # Advanced even after a truncated walk, and that is safe precisely
        # because directories are visited newest-first: whatever the walk did
        # not reach is older than what it did, so it could never have counted
        # as new. Holding the watermark back on truncation would instead pin it
        # forever on exactly the large spools this feature exists for.
        if result.last_recording_at is not None:
            self._watermark = result.last_recording_at

        payload = result.to_payload()
        payload["scan_seconds"] = round(time.monotonic() - started, 3)
        return payload
