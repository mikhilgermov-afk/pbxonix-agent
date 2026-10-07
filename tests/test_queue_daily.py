import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from pbxonix_agent.config import QueueDailyConfig
from pbxonix_agent.queue_daily import CollectionError, collect, day_window, query


def test_sql_counts_queue_visits_and_outcomes_without_member_attempts():
    db = sqlite3.connect(":memory:")
    db.create_function("REGEXP", 2, lambda pattern, value: bool(re.match(pattern, value or "")))
    db.execute("ATTACH DATABASE ':memory:' AS asteriskcdrdb")
    db.execute("CREATE TABLE asteriskcdrdb.queuelog (time,queuename,event,data1,data3,data4)")
    events = [
        ("ENTERQUEUE", "", "", ""),
        ("ENTERQUEUE", "", "", ""),
        ("ENTERQUEUE", "", "", ""),
        ("CONNECT", "10", "", ""),
        ("ABANDON", "1", "30", ""),
        ("EXITWITHTIMEOUT", "1", "40", ""),
        ("EXITWITHKEY", "1", "3", "50"),
        ("EXITEMPTY", "1", "invalid", ""),
        ("RINGNOANSWER", "999", "", ""),
        ("COMPLETECALLER", "10", "", ""),
    ]
    for event in events:
        db.execute(
            "INSERT INTO asteriskcdrdb.queuelog VALUES (?,?,?,?,?,?)",
            ("2026-09-11 10:00:00.000001", "Поддержка", *event),
        )
    for stamp in ("2026-09-10 23:59:59.999999", "2026-09-12 00:00:00"):
        db.execute(
            "INSERT INTO asteriskcdrdb.queuelog VALUES (?,?,?,?,?,?)",
            (stamp, "Поддержка", "ENTERQUEUE", "", "", ""),
        )
    start = datetime(2026, 9, 11, tzinfo=timezone.utc)
    end = datetime(2026, 9, 12, tzinfo=timezone.utc)
    row = db.execute(query(QueueDailyConfig(), start, end)).fetchone()
    assert bytes.fromhex(row[0]).decode() == "Поддержка"
    assert row[1:] == (3, 1, 4, 10.0, 50)
    db.close()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="POSIX timezone conversion")
@pytest.mark.parametrize(
    "zone,date,hours,utc_start",
    [
        ("Europe/Moscow", (2026, 9, 11), 24, "2026-09-10 21:00:00"),
        ("Europe/Berlin", (2026, 3, 29), 23, "2026-03-28 23:00:00"),
        ("Europe/Berlin", (2026, 10, 25), 25, "2026-10-24 22:00:00"),
    ],
)
def test_local_day_boundaries_and_utc_realtime_logs(zone, date, hours, utc_start):
    old = os.environ.get("TZ")
    try:
        os.environ["TZ"] = zone
        time.tzset()
        start, end = day_window(datetime(*date, hour=12))
        assert (end - start).total_seconds() == hours * 3600
        assert utc_start in query(QueueDailyConfig(time_basis="utc"), start, end)
        assert start.hour == 0 and end.hour == 0
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_empty_success_is_zero_activity_but_failed_reads_never_publish_zero():
    with patch("pbxonix_agent.queue_daily.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, b"", b"")
        assert collect(QueueDailyConfig())["queues"] == []
        assert run.call_args[1]["timeout"] == 5
        run.return_value = subprocess.CompletedProcess([], 1, b"", b"secret")
        with pytest.raises(CollectionError, match="Queue log read failed"):
            collect(QueueDailyConfig())
        run.side_effect = subprocess.TimeoutExpired("mysql", 5)
        with pytest.raises(CollectionError):
            collect(QueueDailyConfig())


def test_snapshot_preserves_unicode_and_unknown_waits():
    line = "{}\t9\t3\t2\t12.3456\tNULL\n".format("Очередь".encode().hex())
    with patch("pbxonix_agent.queue_daily.subprocess.run") as run:
        run.return_value = subprocess.CompletedProcess([], 0, line.encode(), b"")
        row = collect(QueueDailyConfig())["queues"][0]
        assert row == dict(
            name="Очередь",
            entered=9,
            answered=3,
            unanswered=2,
            average_wait_seconds=12.35,
            max_wait_seconds=None,
        )


@pytest.mark.parametrize("identifier", ["x`;DROP TABLE x", "a.b", "", "table\n"])
def test_identifiers_cannot_change_the_query(identifier):
    start, end = day_window()
    with pytest.raises(CollectionError):
        query(QueueDailyConfig(table=identifier), start, end)
