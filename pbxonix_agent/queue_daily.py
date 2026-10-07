"""Read calendar-day outcomes from an Asterisk realtime queue log.

A separate thread and a SELECT-only account keep this away from the AMI/status
loop. No caller numbers, call IDs or DB credentials leave the PBX.
"""

import logging
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone

from pbxonix_agent.transport import ApiError, Client, RateLimited, TransportError

log = logging.getLogger("pbxonix.queue_daily")
IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


class CollectionError(Exception):
    pass


def day_window(now=None):
    # Converting each naive local midnight separately honors the host's DST
    # rules (also on Python 3.6); a fixed-offset timedelta would not.
    local = datetime.now() if now is None else now
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return start.astimezone(), end.astimezone()


def query(config, start, end):
    if not all(IDENTIFIER.fullmatch(value) for value in (config.database, config.table)):
        raise CollectionError("Invalid database identifier")
    if config.time_basis == "utc":
        start, end = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
    bounds = [value.strftime("%Y-%m-%d %H:%M:%S") for value in (start, end)]
    # RINGNOANSWER is a member attempt, not an unanswered queue visit. CONNECT
    # is counted once at answer, not again on COMPLETEAGENT/COMPLETECALLER.
    wait = """CASE
        WHEN event='CONNECT' THEN data1
        WHEN event IN ('ABANDON','EXITWITHTIMEOUT','EXITEMPTY') THEN data3
        WHEN event='EXITWITHKEY' THEN data4 END"""
    valid_wait = (
        "CASE WHEN ({0}) REGEXP '^[0-9]{{1,6}}$' "
        "AND CAST(({0}) AS UNSIGNED)<=604800 THEN CAST(({0}) AS UNSIGNED) END"
    ).format(wait)
    return """SELECT HEX(queuename),  -- calendar-day aggregate
        SUM(event='ENTERQUEUE'), SUM(event='CONNECT'),
        SUM(event IN ('ABANDON','EXITWITHTIMEOUT','EXITEMPTY','EXITWITHKEY')),
        AVG(CASE WHEN event='CONNECT' THEN {wait} END), MAX({wait})
        FROM `{database}`.`{table}`
        WHERE time >= '{start}' AND time < '{end}'
        AND event IN ('ENTERQUEUE','CONNECT','ABANDON','EXITWITHTIMEOUT','EXITEMPTY','EXITWITHKEY')
        GROUP BY queuename;""".format(  # noqa: S608 -- allowlisted identifiers, formatted dates
        wait=valid_wait,
        database=config.database,
        table=config.table,
        start=bounds[0],
        end=bounds[1],
    )


def collect(config):
    measured = datetime.now(timezone.utc)
    start, end = day_window(measured.astimezone().replace(tzinfo=None))
    try:
        # Defaults file must contain only a local SELECT-only queue-log account.
        # Kill timed-out reads; never log mysql stderr (may contain credentials).
        result = subprocess.run(  # noqa: S603 -- fixed executable, no shell
            [
                "/usr/bin/mysql",
                "--defaults-extra-file=" + config.defaults_file,
                "--batch",
                "--skip-column-names",
                "--connect-timeout=3",
                "--default-character-set=utf8mb4",
            ],
            input=query(config, start, end).encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=5,
        )
        if result.returncode:
            raise CollectionError("Queue log read failed")
        rows = []
        for line in result.stdout.decode("utf-8").splitlines():
            name, entered, answered, unanswered, average, maximum = line.split("\t")
            rows.append(
                dict(
                    name=bytes.fromhex(name).decode("utf-8"),
                    entered=int(entered),
                    answered=int(answered),
                    unanswered=int(unanswered),
                    average_wait_seconds=None if average == "NULL" else round(float(average), 2),
                    max_wait_seconds=None if maximum == "NULL" else int(maximum),
                )
            )
        if len(rows) > 500:
            raise CollectionError("Too many queues")
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        raise CollectionError("Queue log unavailable") from exc
    # Empty results from a successful configured source mean zero activity.
    # An unavailable source never fabricates a zero snapshot.
    return dict(
        recorded_at=measured.isoformat(),
        period_start=start.isoformat(),
        period_end=end.isoformat(),
        day=start.date().isoformat(),
        timezone="{} (UTC{})".format(start.tzname(), start.strftime("%z")),
        queues=rows,
    )


class QueueDailyPublisher(threading.Thread):
    def __init__(self, config, credentials):
        super().__init__(name="pbxonix-queue-daily", daemon=True)
        self.config = config.queue_daily
        self.stop_event = threading.Event()
        self.client = Client(
            config.api_base_url,
            token=credentials.agent_token,
            verify_tls=config.verify_tls,
            timeout=10,
        )

    def stop(self):
        self.stop_event.set()

    def run(self):
        failures = 0
        while not self.stop_event.is_set():
            started = time.monotonic()
            delay = 30
            try:
                self.client.post("/v1/agent/queue-daily", collect(self.config))
                failures = 0
            except (CollectionError, TransportError, ApiError) as exc:
                failures = min(failures + 1, 4)
                delay = min(300, 30 * (2 ** (failures - 1)))
                if isinstance(exc, RateLimited):
                    delay = max(delay, exc.retry_after)
                if isinstance(exc, ApiError) and exc.status in (401, 403, 404):
                    delay = 300
                log.warning(
                    "queue statistics unavailable (%s); retry in %ss", type(exc).__name__, delay
                )
            self.stop_event.wait(max(0.2, delay - (time.monotonic() - started)))
