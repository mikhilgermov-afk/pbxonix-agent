"""Incremental report history. Uses local files/SELECT only, without AMI calls."""

import csv
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from pbxonix_agent.report_privacy import sanitize_cdr, sanitize_queue
from pbxonix_agent.transport import ApiError, Client, RateLimited, TransportError

log = logging.getLogger("pbxonix.reports")
UTC = timezone.utc
IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")
QUEUE_EVENTS = {
    "ENTERQUEUE",
    "CONNECT",
    "COMPLETEAGENT",
    "COMPLETECALLER",
    "ABANDON",
    "EXITWITHTIMEOUT",
    "EXITWITHKEY",
    "EXITEMPTY",
    "TRANSFER",
    "BLINDTRANSFER",
    "ATTENDEDTRANSFER",
    "RINGNOANSWER",
}


class CollectionError(Exception):
    pass


def identifier(value):
    if not IDENTIFIER.fullmatch(value):
        raise CollectionError("Invalid source identifier")
    return "`" + value + "`"


def stamp(value, basis):
    if not value or value.startswith("0000-00-00"):
        return None
    for pattern in ["%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"]:
        try:
            parsed = datetime.strptime(value, pattern)
            return (
                parsed.replace(tzinfo=UTC) if basis == "utc" else parsed.astimezone()
            ).astimezone(UTC)
        except ValueError:
            pass
    raise CollectionError("Invalid source date")


def sql_time(epoch, basis):
    value = datetime.fromtimestamp(epoch, UTC)
    if basis == "local":
        value = value.astimezone()
    return value.strftime("%Y-%m-%d %H:%M:%S")


def digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def mysql(config, query):
    try:
        result = subprocess.run(  # noqa: S603 -- fixed executable, no shell
            [
                "/usr/bin/mysql",
                "--defaults-extra-file=" + config.defaults_file,
                "--batch",
                "--skip-column-names",
                "--connect-timeout=3",
                "--default-character-set=utf8mb4",
            ],
            input=query.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        if result.returncode:
            raise CollectionError("Report source unavailable")
        return [line.split("\t") for line in result.stdout.decode("utf-8").splitlines()]
    except (OSError, subprocess.TimeoutExpired, UnicodeError) as exc:
        raise CollectionError("Report source unavailable") from exc


def columns(config, database, table):
    # All identifiers originate in validated local configuration.
    query = "SHOW COLUMNS FROM {}.{}".format(identifier(database), identifier(table))
    return {row[0] for row in mysql(config, query)}


def decode_hex(values):
    try:
        return [bytes.fromhex(value).decode("utf-8", "replace") for value in values]
    except ValueError as exc:
        raise CollectionError("Invalid source encoding") from exc


def cdr_record(raw, basis):
    start = stamp(raw.get("start", ""), basis)
    if start is None:
        raise CollectionError("Missing CDR start")
    duration = max(0, int(raw.get("duration") or 0))
    billed = min(duration, max(0, int(raw.get("billsec") or 0)))
    end = stamp(raw.get("end", ""), basis) or start + timedelta(seconds=duration)
    # Some backends round the timestamps but retain a different duration.
    if end < start:
        end = start + timedelta(seconds=duration)
    answer = stamp(raw.get("answer", ""), basis)
    if answer is not None and not start <= answer <= end:
        answer = None
    uid = raw.get("uniqueid") or digest(raw)
    result = dict(
        call_key=raw.get("linkedid") or uid,
        uniqueid=uid,
        started_at=start.isoformat(),
        answered_at=answer.isoformat() if answer else None,
        ended_at=end.isoformat(),
        source=raw.get("src", "")[:120],
        destination=raw.get("dst", "")[:120],
        channel=raw.get("channel", "")[:255],
        destination_channel=raw.get("dstchannel", "")[:255],
        context=raw.get("dcontext", "")[:120],
        application=raw.get("lastapp", "")[:80],
        disposition=(raw.get("disposition") or "UNKNOWN")[:40],
        duration_seconds=duration,
        bill_seconds=billed,
    )
    # Hash normalized values so switching from CSV/UTC to a local-time SQL
    # backend does not insert the same channel path a second time.
    result["source_key"] = digest(
        {key: value for key, value in result.items() if key != "call_key"}
    )
    return result


def enrich_linked_ids(config, rows):
    if not config.cel_table or not rows:
        return
    values = sorted({row["uniqueid"] for row in rows})
    literals = ["0x" + value.encode("utf-8").hex() for value in values]
    query = (  # noqa: S608 -- allowlisted identifiers and hex-encoded values
        "SELECT HEX(uniqueid),HEX(MIN(NULLIF(linkedid,''))) FROM {}.{} "
        "WHERE uniqueid IN ({}) GROUP BY uniqueid"
    ).format(identifier(config.cdr_database), identifier(config.cel_table), ",".join(literals))
    linked = {}
    for uid, value in mysql(config, query):
        if value and value != "NULL":
            decoded = decode_hex([uid, value])
            linked[decoded[0]] = decoded[1]
    for row in rows:
        row["call_key"] = linked.get(row["uniqueid"], row["call_key"])


CSV_KEYS = [
    "accountcode",
    "src",
    "dst",
    "dcontext",
    "clid",
    "channel",
    "dstchannel",
    "lastapp",
    "lastdata",
    "start",
    "answer",
    "end",
    "duration",
    "billsec",
    "disposition",
    "amaflags",
    "uniqueid",
    "userfield",
]


def csv_start_offset(path, cutoff, basis):
    """Seek by completion time; avoid walking a multi-gigabyte append-only file."""
    with open(path, "rb") as stream:
        low, high = 0, os.fstat(stream.fileno()).st_size
        while high - low > 65536:
            middle = (low + high) // 2
            stream.seek(middle)
            stream.readline()
            position = stream.tell()
            line = stream.readline()
            try:
                row = next(csv.reader([line.decode("utf-8", "replace")]))
                when = stamp(row[11], basis)
            except (IndexError, csv.Error, CollectionError, StopIteration):
                high = middle
                continue
            if when is not None and when.timestamp() < cutoff:
                low = position
            else:
                high = middle
        # A small overlap also tolerates locally batched, slightly out-of-order
        # dispatch. Exact timestamp filtering happens in the record reader.
        stream.seek(max(0, low - 65536))
        if stream.tell():
            stream.readline()
        return stream.tell()


def csv_chunk(config, state):
    path = config.cdr_csv
    stat = os.stat(path)
    cursor = dict(state)
    if "inode" not in cursor:
        cursor.update(
            inode=stat.st_ino,
            device=stat.st_dev,
            offset=csv_start_offset(path, cursor["history_from"], config.cdr_time_basis),
        )
    elif (cursor["inode"], cursor["device"]) != (stat.st_ino, stat.st_dev) or stat.st_size < cursor[
        "offset"
    ]:
        # Finish the previous file if logrotate renamed it between polls.
        directory, basename = os.path.split(path)
        old = None
        for name in os.listdir(directory):
            if name.startswith(basename + ".") and not name.endswith(".gz"):
                candidate = os.path.join(directory, name)
                info = os.stat(candidate)
                if (info.st_ino, info.st_dev) == (
                    cursor["inode"],
                    cursor["device"],
                ) and info.st_size > cursor["offset"]:
                    old = candidate
                    break
        if old:
            path = old
        else:
            cursor.update(inode=stat.st_ino, device=stat.st_dev, offset=0)
    rows = []
    scanned = 0
    with open(path, "rb") as stream:
        stream.seek(cursor["offset"])
        while len(rows) < 500 and scanned < 2 * 1024 * 1024:
            position = stream.tell()
            line = stream.readline(65537)
            if not line:
                break
            # Do not checkpoint a partial append: read it again next time.
            if not line.endswith(b"\n"):
                stream.seek(position)
                break
            if len(line) > 65536:
                raise CollectionError("Invalid CDR row")
            scanned += len(line)
            try:
                raw = next(csv.reader([line.decode("utf-8", "replace")]))
            except csv.Error as exc:
                raise CollectionError("Invalid CDR row") from exc
            if len(raw) < 17:
                raise CollectionError("Invalid CDR column count")
            record = cdr_record(dict(zip(CSV_KEYS, raw)), config.cdr_time_basis)
            end = stamp(dict(zip(CSV_KEYS, raw))["end"], config.cdr_time_basis)
            if end is not None and end.timestamp() >= cursor["history_from"]:
                rows.append(record)
                cursor["covered_until"] = max(
                    cursor.get("covered_until", cursor["history_from"]), end.timestamp()
                )
            cursor["offset"] = stream.tell()
        eof = stream.tell() == os.fstat(stream.fileno()).st_size
    if eof and path == config.cdr_csv:
        cursor["covered_until"] = time.time()
    enrich_linked_ids(config, rows)
    return rows, cursor, eof


def mysql_chunk(config, kind, state, fields):
    # Whole-second, half-open bounds work for both DATETIME and legacy CHAR
    # queue timestamps; a fractional lower bound would skip an exact second.
    current = int(time.time())
    cursor = dict(state)
    frontier = int(cursor.get("cursor", cursor["history_from"]))
    interval = 3600 if kind == "cdr" else 300
    if (
        "replay_cursor" not in cursor
        and current - frontier < 120
        and current - cursor.get("last_replay", 0) >= interval
    ):
        cursor.update(
            replay_cursor=max(
                int(cursor["history_from"]), frontier - (86400 if kind == "cdr" else 300)
            ),
            replay_until=frontier,
        )
    replay = "replay_cursor" in cursor
    query_start = int(cursor["replay_cursor"]) if replay else frontier
    target = int(cursor["replay_until"]) if replay else current
    end = min(target, query_start + 21600)
    if end <= query_start:
        if replay:
            cursor.pop("replay_cursor", None)
            cursor.pop("replay_until", None)
            cursor["last_replay"] = current
        return [], cursor, current - frontier < 2
    if kind == "cdr":
        database, table, basis = config.cdr_database, config.cdr_table, config.cdr_time_basis
        aliases = {"start": "start" if "start" in fields else "calldate"}
        requested = [
            "src",
            "dst",
            "dcontext",
            "channel",
            "dstchannel",
            "lastapp",
            "start",
            "answer",
            "end",
            "duration",
            "billsec",
            "disposition",
            "uniqueid",
            "linkedid",
            "sequence",
        ]
        available = [
            (name, aliases.get(name, name))
            for name in requested
            if aliases.get(name, name) in fields
        ]
        date_column = "end" if "end" in fields else aliases["start"]
        maximum = 500
    else:
        database, table, basis = config.queue_database, config.queue_table, config.queue_time_basis
        requested = [
            "id",
            "time",
            "callid",
            "queuename",
            "agent",
            "event",
            "data1",
            "data2",
            "data3",
            "data4",
            "data5",
        ]
        available = [(name, name) for name in requested if name in fields]
        date_column = "time"
        maximum = 1000
    selected = ",".join(
        "HEX(COALESCE(CAST({} AS CHAR),''))".format(identifier(column)) for _, column in available
    )
    while True:
        query = "SELECT {} FROM {}.{} WHERE {} >= '{}' AND {} < '{}' ORDER BY {} LIMIT {}".format(  # noqa: S608 -- identifiers validated, bounds are formatted datetimes
            selected,
            identifier(database),
            identifier(table),
            identifier(date_column),
            sql_time(query_start, basis),
            identifier(date_column),
            sql_time(end, basis),
            identifier(date_column),
            maximum + 1,
        )
        raw_rows = mysql(config, query)
        if len(raw_rows) <= maximum:
            break
        if end - query_start <= 1:
            raise CollectionError("Source interval exceeds batch limit")
        end = query_start + (end - query_start) // 2
    rows = []
    for values in raw_rows:
        raw = dict(zip([name for name, _ in available], decode_hex(values)))
        if kind == "cdr":
            rows.append(cdr_record(raw, basis))
        elif raw.get("event") in QUEUE_EVENTS and raw.get("callid") not in {
            None,
            "",
            "NONE",
            "REALTIME",
        }:
            when = stamp(raw["time"], basis)
            if when is None:
                raise CollectionError("Missing queue event date")
            key = digest([database, table, raw.get("id")]) if raw.get("id") else digest(raw)
            value = dict(
                source_key=key,
                call_key=raw["callid"][:200],
                recorded_at=when.isoformat(),
                queue=raw["queuename"][:200],
                agent=raw.get("agent", "")[:200],
                event=raw["event"][:40],
            )
            for field in ["data1", "data2", "data3", "data4", "data5"]:
                value[field] = raw.get(field, "")[:200]
            # ENTERQUEUE data2 is the caller number; CDR already supplies it.
            # Avoid carrying duplicate caller identity in the event feed.
            if value["event"] == "ENTERQUEUE":
                value["data2"] = ""
            rows.append(value)
    if kind == "cdr":
        enrich_linked_ids(config, rows)
    cursor.update(cursor=max(frontier, end), covered_until=max(cursor.get("covered_until", 0), end))
    if replay:
        if end >= target:
            cursor.pop("replay_cursor", None)
            cursor.pop("replay_until", None)
            cursor["last_replay"] = current
        else:
            cursor["replay_cursor"] = end
    return rows, cursor, "replay_cursor" not in cursor and current - cursor["cursor"] < 2


def save_state(path, value):
    temporary = path + ".tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def publish_batch(client, kind, rows, source, inventory=None):
    """Small HTTPS requests also work behind legacy PBX network equipment.

    Coverage is published only after every slice has been acknowledged.
    If a later slice fails, replaying earlier slices is idempotent.
    """
    inventory = inventory or {}
    extensions = set(inventory.get("extensions", []))
    endpoints = extensions | set(inventory.get("trunks", []))
    queues = set(inventory.get("queues", []))
    rows = [
        sanitize_cdr(row, endpoints) if kind == "cdr" else sanitize_queue(row, extensions, queues)
        for row in rows
    ]
    chunk = []
    for row in rows:
        candidate = {kind: [*chunk, row], "sources": [source]}
        if chunk and len(json.dumps(candidate).encode("utf-8")) > 12000:
            client.post_with_retry("/v1/agent/reports", {kind: chunk}, attempts=3)
            chunk = []
        chunk.append(row)
    return client.post_with_retry(
        "/v1/agent/reports", {kind: chunk, "sources": [source]}, attempts=3
    )


class ReportsPublisher(threading.Thread):
    def __init__(self, config, credentials, inventory=None):
        super().__init__(name="pbxonix-reports", daemon=True)
        self.config = config.reports
        self.inventory = inventory or (lambda: None)
        self.stop_event = threading.Event()
        self.client = Client(
            config.api_base_url,
            token=credentials.agent_token,
            verify_tls=config.verify_tls,
            timeout=15,
        )
        self.path = os.path.join(os.path.dirname(config.buffer.path), "reports-state.json")
        self.fields = {}
        self.states = {}
        if os.path.exists(self.path):
            try:
                with open(self.path) as stream:
                    self.states = json.load(stream)
                if not isinstance(self.states, dict):
                    self.states = {}
            except (OSError, ValueError):
                log.warning("Report checkpoint unreadable; history will be replayed")

    def stop(self):
        self.stop_event.set()

    def run(self):
        history = (
            (
                datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                - timedelta(days=self.config.history_days)
            )
            .astimezone()
            .timestamp()
        )
        for kind in ["cdr", "queues"]:
            self.states.setdefault(kind, dict(history_from=history, covered_until=history))
        failures = 0
        while not self.stop_event.is_set():
            inventory = self.inventory()
            if inventory is None:
                self.stop_event.wait(2)
                continue
            all_live = True
            delay = 2
            for kind in ["cdr", "queues"]:
                if self.stop_event.is_set():
                    break
                if kind == "cdr" and self.config.cdr_mode == "disabled":
                    continue
                if kind == "queues" and not self.config.queue_table:
                    continue
                state = self.states[kind]
                mode = self.config.cdr_mode if kind == "cdr" else "mysql"
                try:
                    if mode == "csv":
                        rows, updated, live = csv_chunk(self.config, state)
                    else:
                        if kind not in self.fields:
                            database = (
                                self.config.cdr_database
                                if kind == "cdr"
                                else self.config.queue_database
                            )
                            table = (
                                self.config.cdr_table if kind == "cdr" else self.config.queue_table
                            )
                            self.fields[kind] = columns(self.config, database, table)
                        rows, updated, live = mysql_chunk(
                            self.config, kind, state, self.fields[kind]
                        )
                    all_live = all_live and live
                    source = dict(
                        kind=kind,
                        mode=mode,
                        history_from=datetime.fromtimestamp(
                            updated["history_from"], UTC
                        ).isoformat(),
                        covered_until=datetime.fromtimestamp(
                            updated["covered_until"], UTC
                        ).isoformat(),
                        source_timezone=self.config.cdr_time_basis
                        if kind == "cdr"
                        else self.config.queue_time_basis,
                    )
                    publish_batch(self.client, kind, rows, source, inventory)
                    self.states[kind] = updated
                    save_state(self.path, self.states)
                    failures = 0
                except (CollectionError, TransportError, ApiError, OSError, ValueError) as exc:
                    failures = min(failures + 1, 5)
                    delay = max(delay, min(300, 15 * 2 ** (failures - 1)))
                    if isinstance(exc, RateLimited):
                        delay = max(delay, exc.retry_after)
                    # Only the exception class is logged: no CDRs, numbers,
                    # credentials, database output or HTTP response bodies.
                    log.warning(
                        "report history unavailable (%s); retry in %ss", type(exc).__name__, delay
                    )
                    if isinstance(exc, (CollectionError, OSError, ValueError)):
                        try:
                            self.client.post(
                                "/v1/agent/reports",
                                {
                                    "sources": [
                                        dict(
                                            kind=kind,
                                            mode=mode,
                                            history_from=datetime.fromtimestamp(
                                                state["history_from"], UTC
                                            ).isoformat(),
                                            covered_until=datetime.fromtimestamp(
                                                state.get("covered_until", state["history_from"]),
                                                UTC,
                                            ).isoformat(),
                                            source_timezone=self.config.cdr_time_basis
                                            if kind == "cdr"
                                            else self.config.queue_time_basis,
                                            error="source_unavailable",
                                        )
                                    ]
                                },
                            )
                        except (TransportError, ApiError):
                            pass
                    all_live = False
            if all_live:
                delay = max(delay, 60)
            self.stop_event.wait(delay)
