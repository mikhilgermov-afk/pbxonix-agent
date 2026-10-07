"""Agent configuration.

INI via configparser, because the agent ships with no third-party dependencies
and because it is the format PBX administrators already read every day.
"""

import configparser
import os
import re
from typing import List, Optional

DEFAULT_CONFIG_PATH = "/etc/pbxonix/agent.conf"


class ConfigError(Exception):
    pass


# Plain classes rather than dataclasses: those are 3.7+, and the floor is 3.6 so
# that a stock Issabel 4 / CentOS 7 box works with the interpreter it already
# has. Nothing here needs the generated __eq__ or __repr__.
class BufferConfig:
    def __init__(
        self,
        path: str = "/var/lib/pbxonix/buffer.sqlite3",
        max_rows: int = 50_000,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        self.path = path
        self.max_rows = max_rows
        self.max_bytes = max_bytes


class AsteriskConfig:
    def __init__(
        self,
        ami_enabled: bool = False,
        ami_host: str = "127.0.0.1",
        ami_port: int = 5038,
        # Trunks, peers and queues are inventory: they change when somebody
        # edits the PBX, not from second to second. Asking every minute cost a
        # live system its call quality -- SIPpeers walks and locks the whole
        # peer container, QueueStatus iterates every member of every queue.
        # Five minutes still notices a dropped trunk quickly.
        ami_interval_seconds: int = 300,
    ) -> None:
        self.ami_enabled = ami_enabled
        self.ami_host = ami_host
        self.ami_port = ami_port
        self.ami_interval_seconds = ami_interval_seconds


class RecordingsConfig:
    def __init__(
        self,
        paths: Optional[List[str]] = None,
        # Recordings do not need per-minute resolution, and a large spool is
        # expensive to walk. Five minutes is well inside any useful alert window.
        interval_seconds: int = 300,
        # Safety valves for a spool with hundreds of thousands of files. The walk
        # visits newest-first, so hitting either limit still leaves the freshness
        # signal correct -- only the totals become best-effort.
        max_scan_seconds: float = 10.0,
        max_entries: int = 200_000,
    ) -> None:
        # Copied, not aliased: a shared default list is the classic mutable
        # default bug, and dataclasses used default_factory for the same reason.
        self.paths = list(paths) if paths is not None else []
        self.interval_seconds = interval_seconds
        self.max_scan_seconds = max_scan_seconds
        self.max_entries = max_entries


class QueueDailyConfig:
    def __init__(
        self,
        enabled=False,
        defaults_file="/etc/pbxonix/queue-mysql.cnf",
        database="asteriskcdrdb",
        table="queuelog",
        time_basis="local",
    ):
        self.enabled = enabled
        self.defaults_file = defaults_file
        self.database = database
        self.table = table
        self.time_basis = time_basis


class ReportsConfig:
    def __init__(self):
        self.enabled = False
        self.defaults_file = "/etc/pbxonix/reports-mysql.cnf"
        self.cdr_mode = "disabled"
        self.cdr_database = "asteriskcdrdb"
        self.cdr_table = "cdr"
        self.cdr_csv = "/var/log/asterisk/cdr-csv/Master.csv"
        self.cdr_time_basis = "local"
        self.cel_table = ""
        self.queue_database = "asteriskcdrdb"
        self.queue_table = ""
        self.queue_time_basis = "local"
        self.history_days = 30


class Config:
    def __init__(
        self,
        api_base_url: str = "https://api.pbxonix.com",
        heartbeat_interval_seconds: int = 30,
        metrics_interval_seconds: int = 60,
        panel_interval_seconds: int = 5,
        log_level: str = "INFO",
        verify_tls: bool = True,
        config_path: str = DEFAULT_CONFIG_PATH,
        buffer: Optional[BufferConfig] = None,
        asterisk: Optional[AsteriskConfig] = None,
        recordings: Optional[RecordingsConfig] = None,
        queue_daily: Optional[QueueDailyConfig] = None,
        reports: Optional[ReportsConfig] = None,
    ) -> None:
        self.api_base_url = api_base_url
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.metrics_interval_seconds = metrics_interval_seconds
        self.panel_interval_seconds = panel_interval_seconds
        self.log_level = log_level
        self.verify_tls = verify_tls
        self.config_path = config_path
        self.buffer = buffer if buffer is not None else BufferConfig()
        self.asterisk = asterisk if asterisk is not None else AsteriskConfig()
        self.recordings = recordings if recordings is not None else RecordingsConfig()
        self.queue_daily = queue_daily if queue_daily is not None else QueueDailyConfig()
        self.reports = reports if reports is not None else ReportsConfig()

    @property
    def credentials_path(self) -> str:
        return os.path.join(os.path.dirname(self.config_path), "credentials.json")


def _get_int(parser: configparser.ConfigParser, section: str, key: str, fallback: int) -> int:
    try:
        return parser.getint(section, key, fallback=fallback)
    except ValueError as exc:
        raise ConfigError("[{}] {} must be an integer".format(section, key)) from exc


def _get_bool(parser: configparser.ConfigParser, section: str, key: str, fallback: bool) -> bool:
    try:
        return parser.getboolean(section, key, fallback=fallback)
    except ValueError as exc:
        raise ConfigError("[{}] {} must be true or false".format(section, key)) from exc


def load(path: str = DEFAULT_CONFIG_PATH) -> Config:
    if not os.path.exists(path):
        raise ConfigError("configuration file not found: {}".format(path))

    parser = configparser.ConfigParser(inline_comment_prefixes=("#", ";"))
    try:
        parser.read(path, encoding="utf-8")
    except configparser.Error as exc:
        raise ConfigError("could not parse {}: {}".format(path, exc)) from exc

    config = Config(config_path=path)

    config.api_base_url = parser.get("agent", "api_base_url", fallback=config.api_base_url).rstrip(
        "/"
    )
    config.heartbeat_interval_seconds = _get_int(
        parser, "agent", "heartbeat_interval_seconds", config.heartbeat_interval_seconds
    )
    config.metrics_interval_seconds = _get_int(
        parser, "agent", "metrics_interval_seconds", config.metrics_interval_seconds
    )
    config.panel_interval_seconds = _get_int(
        parser, "agent", "panel_interval_seconds", config.panel_interval_seconds
    )
    config.log_level = parser.get("agent", "log_level", fallback=config.log_level).upper()
    config.verify_tls = _get_bool(parser, "agent", "verify_tls", config.verify_tls)

    config.buffer = BufferConfig(
        path=parser.get("buffer", "path", fallback=config.buffer.path),
        max_rows=_get_int(parser, "buffer", "max_rows", config.buffer.max_rows),
        max_bytes=_get_int(parser, "buffer", "max_bytes", config.buffer.max_bytes),
    )

    config.asterisk = AsteriskConfig(
        ami_enabled=_get_bool(parser, "asterisk", "ami_enabled", False),
        ami_host=parser.get("asterisk", "ami_host", fallback=config.asterisk.ami_host),
        ami_port=_get_int(parser, "asterisk", "ami_port", config.asterisk.ami_port),
        ami_interval_seconds=_get_int(
            parser, "asterisk", "ami_interval_seconds", config.asterisk.ami_interval_seconds
        ),
    )

    raw_paths = parser.get("recordings", "paths", fallback="")
    config.recordings = RecordingsConfig(
        paths=[p.strip() for p in raw_paths.split(",") if p.strip()],
        interval_seconds=_get_int(
            parser, "recordings", "interval_seconds", config.recordings.interval_seconds
        ),
        max_scan_seconds=float(
            _get_int(
                parser,
                "recordings",
                "max_scan_seconds",
                int(config.recordings.max_scan_seconds),
            )
        ),
        max_entries=_get_int(parser, "recordings", "max_entries", config.recordings.max_entries),
    )

    config.queue_daily = QueueDailyConfig(
        enabled=_get_bool(parser, "queue_daily", "enabled", False),
        defaults_file=parser.get(
            "queue_daily", "defaults_file", fallback=config.queue_daily.defaults_file
        ),
        database=parser.get("queue_daily", "database", fallback=config.queue_daily.database),
        table=parser.get("queue_daily", "table", fallback=config.queue_daily.table),
        time_basis=parser.get("queue_daily", "time_basis", fallback="local"),
    )
    if config.queue_daily.time_basis not in ("local", "utc"):
        raise ConfigError("queue_daily time_basis must be local or utc")
    if not all(
        re.fullmatch(r"[A-Za-z0-9_]+", v)
        for v in (config.queue_daily.database, config.queue_daily.table)
    ):
        raise ConfigError("queue_daily database and table must be SQL identifiers")
    if not os.path.isabs(config.queue_daily.defaults_file):
        raise ConfigError("queue_daily defaults_file must be an absolute path")

    config.reports.enabled = _get_bool(parser, "reports", "enabled", False)
    for key in (
        "defaults_file",
        "cdr_mode",
        "cdr_database",
        "cdr_table",
        "cdr_csv",
        "cdr_time_basis",
        "cel_table",
        "queue_database",
        "queue_table",
        "queue_time_basis",
    ):
        setattr(
            config.reports, key, parser.get("reports", key, fallback=getattr(config.reports, key))
        )
    config.reports.history_days = _get_int(parser, "reports", "history_days", 30)
    if config.reports.cdr_mode not in ("disabled", "csv", "mysql"):
        raise ConfigError("reports cdr_mode must be disabled, csv or mysql")
    if config.reports.cdr_time_basis not in (
        "local",
        "utc",
    ) or config.reports.queue_time_basis not in ("local", "utc"):
        raise ConfigError("reports time_basis must be local or utc")
    if not 1 <= config.reports.history_days <= 180:
        raise ConfigError("reports history_days must be between 1 and 180")
    for key in ("cdr_database", "cdr_table", "cel_table", "queue_database", "queue_table"):
        value = getattr(config.reports, key)
        if value and not re.fullmatch(r"[A-Za-z0-9_]+", value):
            raise ConfigError("reports database and table names must be SQL identifiers")
    if not all(os.path.isabs(getattr(config.reports, key)) for key in ("defaults_file", "cdr_csv")):
        raise ConfigError("reports file paths must be absolute")

    if not config.api_base_url.startswith("https://"):
        # Enrollment and heartbeat both carry a bearer credential. Refusing
        # plaintext here is cheaper than discovering it in a packet capture.
        raise ConfigError("api_base_url must be an https:// URL")
    if config.panel_interval_seconds != 0 and not 5 <= config.panel_interval_seconds <= 60:
        raise ConfigError("panel_interval_seconds must be 0 (off) or between 5 and 60")
    if config.heartbeat_interval_seconds < 5:
        raise ConfigError("heartbeat_interval_seconds must be at least 5")
    if config.asterisk.ami_interval_seconds != 0 and config.asterisk.ami_interval_seconds < 60:
        # Below a minute the inventory actions start costing the PBX more than
        # the freshness is worth. This is the setting that caused audio to break
        # up on a production system, so it has a floor.
        #
        # 0 is not "as fast as possible" but "not at all": it switches the
        # inventory pass off while leaving the cheap per-minute channel counts
        # running. That is the escape hatch for an operator who hears artefacts
        # during calls and needs the agent quiet on the PBX *now* without losing
        # monitoring altogether.
        raise ConfigError("asterisk ami_interval_seconds must be 0 (off) or at least 60")
    if config.recordings.interval_seconds < 30:
        # Below this the scan cost stops being negligible on a real spool, and
        # nothing about recording activity changes that fast.
        raise ConfigError("recordings interval_seconds must be at least 30")

    return config
