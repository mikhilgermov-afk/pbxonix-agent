"""Recording spool scanning.

Runs against a real temporary directory tree rather than a mocked filesystem:
the whole point of this collector is how it behaves on a directory layout, so
mocking os.scandir would test the mock.
"""

import os
import time

from pbxonix_agent.collectors import recordings


def make_recording(root, relative: str, when: float, size: int = 1024) -> str:
    """Create an audio file with a specific mtime."""
    path = os.path.join(str(root), relative)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(b"\0" * size)
    os.utime(path, (when, when))
    # The parent directory's mtime drives the newest-first walk order.
    os.utime(os.path.dirname(path), (when, when))
    return path


def test_empty_spool_reports_nothing(tmp_path):
    result = recordings.scan([str(tmp_path)])
    assert result.total_files == 0
    assert result.total_size_bytes == 0
    assert result.last_recording_at is None
    assert result.truncated is False


def test_missing_path_is_recorded_not_fatal(tmp_path):
    result = recordings.scan([str(tmp_path / "nope"), str(tmp_path)])
    assert result.missing_paths == [str(tmp_path / "nope")]
    assert result.total_files == 0


def test_counts_sizes_and_newest_timestamp(tmp_path):
    now = time.time()
    make_recording(tmp_path, "2026/08/23/old.wav", now - 86400, size=100)
    make_recording(tmp_path, "2026/08/24/new.wav", now - 60, size=200)
    make_recording(tmp_path, "2026/08/24/newer.gsm", now - 30, size=300)

    result = recordings.scan([str(tmp_path)])
    assert result.total_files == 3
    assert result.total_size_bytes == 600
    assert abs(result.last_recording_at - (now - 30)) < 2


def test_non_audio_files_are_ignored(tmp_path):
    now = time.time()
    make_recording(tmp_path, "2026/08/24/call.wav", now - 30)
    for junk in ("notes.txt", "index.html", "call.wav.tmp", "archive.tar.gz"):
        make_recording(tmp_path, f"2026/08/24/{junk}", now - 10)

    result = recordings.scan([str(tmp_path)])
    assert result.total_files == 1


def test_every_asterisk_codec_counts(tmp_path):
    now = time.time()
    for index, suffix in enumerate(sorted(recordings.AUDIO_SUFFIXES)):
        make_recording(tmp_path, f"2026/08/24/call{index}{suffix}", now - 30)

    result = recordings.scan([str(tmp_path)])
    assert result.total_files == len(recordings.AUDIO_SUFFIXES)


def test_uppercase_extensions_count(tmp_path):
    make_recording(tmp_path, "2026/08/24/CALL.WAV", time.time() - 30)
    assert recordings.scan([str(tmp_path)]).total_files == 1


def test_new_files_are_counted_against_a_watermark(tmp_path):
    now = time.time()
    make_recording(tmp_path, "2026/08/24/a.wav", now - 300)
    make_recording(tmp_path, "2026/08/24/b.wav", now - 60)
    make_recording(tmp_path, "2026/08/24/c.wav", now - 30)

    result = recordings.scan([str(tmp_path)], since=now - 120)
    assert result.new_files == 2


def test_first_scan_counts_nothing_as_new(tmp_path):
    """Everything already on disk is history, not activity."""
    make_recording(tmp_path, "2026/08/24/a.wav", time.time() - 30)
    assert recordings.scan([str(tmp_path)]).new_files == 0


def test_multiple_roots_are_merged(tmp_path):
    now = time.time()
    first, second = tmp_path / "one", tmp_path / "two"
    make_recording(first, "2026/08/24/a.wav", now - 300, size=50)
    make_recording(second, "2026/08/24/b.wav", now - 30, size=70)

    result = recordings.scan([str(first), str(second)])
    assert result.total_files == 2
    assert result.total_size_bytes == 120
    assert abs(result.last_recording_at - (now - 30)) < 2


# ----------------------------------------------------------------- bounds --
def test_entry_limit_truncates_but_keeps_the_newest(tmp_path):
    """The whole reason directories are walked newest-first.

    A truncated walk must still produce a correct last_recording_at, because
    that is the number the alert depends on.
    """
    now = time.time()
    for day in range(1, 20):
        for index in range(20):
            make_recording(
                tmp_path, f"2026/08/{day:02d}/call{index}.wav", now - (20 - day) * 86400
            )
    newest = make_recording(tmp_path, "2026/08/24/latest.wav", now - 10)
    assert os.path.exists(newest)

    result = recordings.scan([str(tmp_path)], max_entries=5)
    assert result.truncated is True
    assert abs(result.last_recording_at - (now - 10)) < 2


def test_truncated_scan_reports_no_totals(tmp_path):
    now = time.time()
    for index in range(10):
        make_recording(tmp_path, f"2026/08/24/call{index}.wav", now - 30)

    payload = recordings.scan([str(tmp_path)], max_entries=3).to_payload()
    # Under a limit these are a floor, not a total. A wrong number is worse
    # than a missing one.
    assert payload["total_files"] is None
    assert payload["total_size_bytes"] is None
    assert payload["truncated"] is True
    assert payload["last_recording_at"] is not None


def test_time_limit_truncates(tmp_path):
    make_recording(tmp_path, "2026/08/24/a.wav", time.time() - 30)
    result = recordings.scan([str(tmp_path)], max_seconds=-1)
    assert result.truncated is True


# ---------------------------------------------------------------- payload --
def test_payload_carries_no_names_or_paths(tmp_path):
    """Asterisk names recordings after the call, so a name is personal data."""
    now = time.time()
    make_recording(tmp_path, "2026/08/24/out-15551234567-101-20260824-141233.wav", now - 30)

    payload = recordings.scan([str(tmp_path)]).to_payload()
    blob = repr(payload)
    assert "15551234567" not in blob
    assert ".wav" not in blob
    assert str(tmp_path) not in blob

    assert set(payload) == {
        "total_files",
        "total_size_bytes",
        "new_files",
        "last_recording_at",
        "truncated",
        "paths_missing",
    }


# ---------------------------------------------------------------- scanner --
def test_scanner_returns_none_without_paths():
    assert recordings.RecordingScanner([]).collect() is None


def test_scanner_tracks_new_files_between_scans(tmp_path):
    now = time.time()
    make_recording(tmp_path, "2026/08/24/a.wav", now - 300)

    scanner = recordings.RecordingScanner([str(tmp_path)])
    first = scanner.collect()
    assert first["new_files"] == 0
    assert first["total_files"] == 1

    make_recording(tmp_path, "2026/08/24/b.wav", now - 10)
    second = scanner.collect()
    assert second["new_files"] == 1
    assert second["total_files"] == 2

    # Nothing added: the same file must not be counted as new twice.
    assert scanner.collect()["new_files"] == 0


def test_watermark_advances_even_when_truncated(tmp_path):
    """Otherwise the feature dies on exactly the spools it exists for.

    A large spool truncates every scan. Holding the watermark back there would
    pin it forever and new_files would always read zero. Newest-first ordering
    is what makes advancing it safe.
    """
    now = time.time()
    for day in range(1, 15):
        for index in range(10):
            make_recording(
                tmp_path, f"2026/08/{day:02d}/c{index}.wav", now - (15 - day) * 86400
            )
    make_recording(tmp_path, "2026/08/24/newest.wav", now - 120)

    scanner = recordings.RecordingScanner([str(tmp_path)], max_entries=4)
    first = scanner.collect()
    assert first["truncated"] is True
    assert first["new_files"] == 0

    make_recording(tmp_path, "2026/08/24/fresher.wav", now - 5)
    second = scanner.collect()
    assert second["truncated"] is True
    assert second["new_files"] == 1


def test_scanner_reports_scan_duration(tmp_path):
    make_recording(tmp_path, "2026/08/24/a.wav", time.time() - 30)
    payload = recordings.RecordingScanner([str(tmp_path)]).collect()
    assert payload["scan_seconds"] >= 0
