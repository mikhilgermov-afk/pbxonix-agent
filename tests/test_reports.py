import csv
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from pbxonix_agent.config import ConfigError, ReportsConfig, load
from pbxonix_agent.reports import (
    CSV_KEYS,
    cdr_record,
    csv_chunk,
    csv_start_offset,
    mysql_chunk,
    publish_batch,
    save_state,
    stamp,
)


def csv_line(start, uid="id-1", source="+70000000000"):
    raw = dict(
        src=source,
        dst="101",
        channel="SIP/carrier-main-00000001",
        dstchannel="SIP/101-00000002",
        start=start.strftime("%Y-%m-%d %H:%M:%S"),
        end=(start + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S"),
        answer=(start + timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S"),
        duration="30",
        billsec="20",
        disposition="ANSWERED",
        uniqueid=uid,
    )
    result = io.StringIO()
    csv.writer(result, lineterminator="\n").writerow([raw.get(key, "") for key in CSV_KEYS])
    return result.getvalue().encode()


def config(path):
    result = ReportsConfig()
    result.cdr_mode = "csv"
    result.cdr_csv = str(path)
    result.cdr_time_basis = "utc"
    return result


def test_csv_checkpoint_replay_partial_append_and_no_double_keys(tmp_path):
    path = tmp_path / "Master.csv"
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    line = csv_line(start)
    path.write_bytes(line[:-2])
    state = dict(
        history_from=(start - timedelta(minutes=1)).timestamp(),
        covered_until=(start - timedelta(minutes=1)).timestamp(),
    )
    rows, checkpoint, _ = csv_chunk(config(path), state)
    assert rows == [] and checkpoint["offset"] == 0
    path.write_bytes(line)
    rows, after, live = csv_chunk(config(path), state)
    assert len(rows) == 1 and live and after["offset"] == len(line)
    assert csv_chunk(config(path), state)[0][0]["source_key"] == rows[0]["source_key"]
    assert csv_chunk(config(path), after)[0] == []


def test_csv_binary_seek_reads_recent_history_without_scanning_whole_file(tmp_path):
    path = tmp_path / "Master.csv"
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with path.open("wb") as stream:
        for index in range(10000):
            stream.write(csv_line(start + timedelta(minutes=index), str(index)))
    cutoff = (start + timedelta(minutes=9000)).timestamp()
    position = csv_start_offset(str(path), cutoff, "utc")
    assert position > path.stat().st_size * 0.7
    rows, _, _ = csv_chunk(config(path), dict(history_from=cutoff, covered_until=cutoff))
    assert rows and all(
        row["ended_at"] >= datetime.fromtimestamp(cutoff, timezone.utc).isoformat() for row in rows
    )


def test_rotation_finishes_old_file_before_new_file(tmp_path):
    path = tmp_path / "Master.csv"
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    path.write_bytes(csv_line(start, "first"))
    initial = dict(
        history_from=(start - timedelta(minutes=1)).timestamp(),
        covered_until=(start - timedelta(minutes=1)).timestamp(),
    )
    _, checkpoint, _ = csv_chunk(config(path), initial)
    with path.open("ab") as stream:
        stream.write(csv_line(start + timedelta(minutes=1), "old-tail"))
    path.rename(tmp_path / "Master.csv.1")
    path.write_bytes(csv_line(start + timedelta(minutes=2), "new-file"))
    rows, checkpoint, _ = csv_chunk(config(path), checkpoint)
    assert [row["uniqueid"] for row in rows] == ["old-tail"]
    rows, checkpoint, _ = csv_chunk(config(path), checkpoint)
    assert [row["uniqueid"] for row in rows] == ["new-file"]


def test_mysql_queue_poll_is_bounded_and_does_not_send_enterqueue_caller_number():
    cfg = ReportsConfig()
    cfg.queue_table = "queue_log"
    cfg.queue_time_basis = "utc"
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    raw = {
        "time": start.strftime("%Y-%m-%d %H:%M:%S"),
        "callid": "1",
        "queuename": "support",
        "agent": "NONE",
        "event": "ENTERQUEUE",
        "data2": "+79999999999",
    }
    requested = ["time", "callid", "queuename", "agent", "event", "data2"]
    with patch(
        "pbxonix_agent.reports.mysql", return_value=[[raw[key].encode().hex() for key in requested]]
    ) as execute:
        rows, checkpoint, _ = mysql_chunk(
            cfg,
            "queues",
            {"history_from": start.timestamp(), "covered_until": start.timestamp()},
            set(requested),
        )
        assert rows[0]["data2"] == ""
        assert "LIMIT 1001" in execute.call_args[0][1]
        assert checkpoint["cursor"] > start.timestamp()


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="POSIX timezone conversion")
def test_mysql_local_dates_and_csv_utc_dates_represent_same_call():
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        assert stamp("2026-09-11 17:00:00", "local") == stamp("2026-09-11 14:00:00", "utc")
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()


def test_source_config_is_opt_in_and_validated(tmp_path):
    path = tmp_path / "agent.conf"
    path.write_text("[agent]\n")
    assert not load(str(path)).reports.enabled
    path.write_text("[reports]\nenabled=true\ncdr_database=bad;DROP\n")
    with pytest.raises(ConfigError):
        load(str(path))


def test_checkpoint_is_json_and_private(tmp_path):
    path = str(tmp_path / "state.json")
    save_state(path, {"cdr": {"offset": 123}})
    assert json.loads(open(path).read())["cdr"]["offset"] == 123
    if os.name == "posix":
        assert os.stat(path).st_mode & 0o777 == 0o600


def test_wire_batches_stay_small_and_coverage_follows_last_ack():
    from unittest.mock import Mock

    client = Mock()
    rows = [
        dict(
            source_key=("%064x" % index),
            call_key="1700000000.1",
            uniqueid="1700000000.1",
            channel="",
            destination_channel="",
            disposition="ANSWERED",
            started_at="2026-09-11T00:00:00Z",
            ended_at="2026-09-11T00:01:00Z",
            duration_seconds=60,
            bill_seconds=50,
        )
        for index in range(100)
    ]
    source = {"kind": "cdr", "covered_until": "2026-09-11T00:00:00Z"}
    publish_batch(client, "cdr", rows, source)
    payloads = [call[0][1] for call in client.post_with_retry.call_args_list]
    assert len(payloads) > 1
    assert [row for payload in payloads for row in payload["cdr"]] == rows
    assert all(len(json.dumps(payload).encode()) <= 12000 for payload in payloads)
    assert all("sources" not in payload for payload in payloads[:-1])
    assert payloads[-1]["sources"] == [source]


def test_cdr_handles_no_answer_and_keeps_identifiers_as_text():
    raw = dict(
        start="2026-09-11 14:00:00",
        duration="30",
        billsec="0",
        uniqueid="abc",
        src="00101",
        dst="00646",
        disposition="NO ANSWER",
    )
    row = cdr_record(raw, "utc")
    assert row["answered_at"] is None and row["source"] == "00101" and row["destination"] == "00646"
    assert row["ended_at"] == "2026-09-11T14:00:30+00:00"
    assert row["source_key"] == cdr_record(raw, "utc")["source_key"]


def test_mysql_exact_second_boundaries_and_dense_replay_progress():
    cfg = ReportsConfig()
    cfg.queue_table = "queue_log"
    cfg.queue_time_basis = "utc"
    fields = {"time", "callid", "queuename", "agent", "event"}
    state = {"history_from": 1700000000, "cursor": 1700001000, "covered_until": 1700001000}
    queries = []

    def execute(config, query):
        queries.append(query)
        # The first replay window is too dense. Each half fits.
        return [[""] * 5] * 1001 if len(queries) == 1 else []

    with patch("pbxonix_agent.reports.time.time", return_value=1700001010), patch(
        "pbxonix_agent.reports.mysql", side_effect=execute
    ):
        _, middle, live = mysql_chunk(cfg, "queues", state, fields)
        assert middle["cursor"] == state["cursor"]
        assert middle["replay_cursor"] == 1700000850
        assert not live
        _, after, _ = mysql_chunk(cfg, "queues", middle, fields)
        assert "replay_cursor" not in after
        assert after["last_replay"] == 1700001010
        _, tail, _ = mysql_chunk(cfg, "queues", after, fields)
        assert tail["cursor"] == 1700001010
        assert all(".000000" not in query for query in queries)


@pytest.mark.skipif(not hasattr(time, "tzset"), reason="POSIX timezone conversion")
def test_normalized_source_key_survives_csv_to_sql_timezone_change():
    previous = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Europe/Moscow"
        time.tzset()
        raw = dict(start="2026-09-11 14:00:00", duration="30", billsec="20", uniqueid="a")
        utc = cdr_record(raw, "utc")
        local = cdr_record(dict(raw, start="2026-09-11 17:00:00"), "local")
        assert utc["source_key"] == local["source_key"]
    finally:
        if previous is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = previous
        time.tzset()
