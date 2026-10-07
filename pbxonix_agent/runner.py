"""The agent's main loop.

Two independent schedules: a fast heartbeat that proves the PBX is alive, and a
slower metrics collection that is buffered and replayed.

Heartbeats are deliberately *not* buffered. A heartbeat is a claim about right
now; replaying an hour-old one after an outage would tell the cloud something
that was true then and is not necessarily true now. Losing them is correct --
the cloud detects the silence itself.
"""

import logging
import signal
import socket
import time
from datetime import datetime, timezone
from types import FrameType
from typing import Any, Dict, List, Optional

from pbxonix_agent import __version__
from pbxonix_agent import credentials as creds
from pbxonix_agent.buffer import Buffer, BufferFull
from pbxonix_agent.collectors import asterisk as asterisk_collector
from pbxonix_agent.collectors import extensions as extension_collector
from pbxonix_agent.collectors import recordings as recording_collector
from pbxonix_agent.collectors import snapshot as ami_snapshot
from pbxonix_agent.collectors import system as system_collector
from pbxonix_agent.collectors.ami import AMIAuthError, AMIClient, AMIError
from pbxonix_agent.config import Config
from pbxonix_agent.panel import PanelPublisher
from pbxonix_agent.quality import QualityPublisher
from pbxonix_agent.queue_daily import QueueDailyPublisher
from pbxonix_agent.reports import ReportsPublisher
from pbxonix_agent.transport import ApiError, Client, RateLimited, TransportError

log = logging.getLogger("pbxonix.agent")

FLUSH_BATCH = 200
# Backing off to a slow retry beats hammering the API with a credential the
# server has already told us is invalid.
UNAUTHORIZED_BACKOFF_SECONDS = 300
# A wrong AMI secret will still be wrong in sixty seconds, and every attempt
# writes a failed-login line into Asterisk's log.
AMI_AUTH_BACKOFF_SECONDS = 900


class Agent:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.credentials = creds.load(config.credentials_path)
        if self.credentials is None:
            raise RuntimeError(
                "not enrolled: {} is missing. Run 'pbxonix-agent enroll --token TOKEN'".format(
                    config.credentials_path
                )
            )

        self.client = Client(
            config.api_base_url,
            token=self.credentials.agent_token,
            verify_tls=config.verify_tls,
        )
        self.buffer = Buffer(
            config.buffer.path,
            max_rows=config.buffer.max_rows,
            max_bytes=config.buffer.max_bytes,
        )
        self.cpu = system_collector.CpuSampler()
        # Filesystem only: this works on every install, with or without AMI.
        self.recordings = recording_collector.RecordingScanner(
            config.recordings.paths,
            max_seconds=config.recordings.max_scan_seconds,
            max_entries=config.recordings.max_entries,
        )
        self._stop = False
        self._unauthorized_until = 0.0
        self._throttled_until = 0.0
        self._ami_quiet_until = 0.0
        # None until the first attempt. Absence is not failure, so the
        # dashboard is told 'pending' rather than 'unreachable'.
        self._ami_ok = None  # type: Optional[bool]
        # Endpoint counts come from AMI but belong on the asterisk sample, so
        # the most recent reading is carried between cycles.
        self._endpoint_counts: Dict[str, Any] = {}
        # Channel counts read over AMI, for PBXs where the local CLI socket
        # is unreachable. Timestamped because AMI is polled on the metrics
        # cadence while heartbeats are twice as frequent -- a value that
        # stops being refreshed must decay to unknown, not freeze.
        self._ami_core: Dict[str, Any] = {}
        # The extension roster from the last inventory pass. Empty until one has
        # run, which is why the fast poll asks for device states only when there
        # is something to layer them onto -- a state with no roster is a queue
        # hint or a park lot, not a person.
        self._extensions: List[Dict[str, Any]] = []
        self._extension_roster_at = 0.0
        self.panel_publisher = None  # type: Optional[PanelPublisher]
        self.queue_daily_publisher = None  # type: Optional[QueueDailyPublisher]
        self.quality_publisher = None
        self._report_inventory = None
        self.reports_publisher = None  # type: Optional[ReportsPublisher]
        # Queue strategies, which change only when somebody edits a queue. Cached
        # so the expensive action that carries them is asked for once rather than
        # every inventory pass.
        self._queue_strategies: Dict[str, str] = {}
        self._ami_core_at = 0.0

    @property
    def ami_enabled(self) -> bool:
        return bool(self.config.asterisk.ami_enabled and self.credentials.has_ami)

    def _ami_state(self) -> str:
        """Why SIP trunks and queues are, or are not, being reported.

        Sent with every heartbeat so the dashboard can say which of these it is.
        Without it an empty trunk list is indistinguishable from AMI never
        having been switched on -- which is what every fresh install looks like,
        and what the dashboard used to report as "no trunks reported yet".
        """
        if not self.config.asterisk.ami_enabled:
            return "disabled"
        if not self.credentials.has_ami:
            return "unconfigured"
        if self._ami_ok is None:
            return "pending"
        return "ok" if self._ami_ok else "unreachable"

    # ------------------------------------------------------------ lifecycle --
    def request_stop(self, signum: int, _frame: Optional[FrameType]) -> None:
        log.info("received signal %s, shutting down", signum)
        self._stop = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

    def close(self) -> None:
        if self.quality_publisher:
            self.quality_publisher.stop()
            self.quality_publisher.join(timeout=16)
        if self.reports_publisher:
            self.reports_publisher.stop()
            self.reports_publisher.join(timeout=22)
        if self.panel_publisher:
            self.panel_publisher.stop()
            self.panel_publisher.join(timeout=12)
        if self.queue_daily_publisher:
            self.queue_daily_publisher.stop()
            self.queue_daily_publisher.join(timeout=16)
        self.buffer.close()

    # -------------------------------------------------------------- payloads --
    def _heartbeat_payload(self) -> Dict[str, Any]:
        metrics = system_collector.collect(self.cpu)
        state = asterisk_collector.collect()
        return {
            "agent_version": __version__,
            # Sent every time so a rename on the PBX shows up in the dashboard
            # without needing a re-enrollment.
            "hostname": socket.gethostname(),
            "ami_state": self._ami_state(),
            "asterisk_running": state.get("running"),
            "active_calls": self._channel_count(state, "active_calls"),
            "active_channels": self._channel_count(state, "active_channels"),
            "cpu": metrics.get("cpu_pct"),
            "ram": metrics.get("ram_pct"),
            "disk": metrics.get("disk_pct"),
            "uptime_seconds": metrics.get("uptime_seconds"),
        }

    def _channel_count(self, state: Dict[str, Any], key: str) -> Optional[int]:
        """Prefer the local CLI, fall back to AMI, otherwise say nothing.

        The CLI answer is about this instant. The AMI one is up to a metrics
        interval old, which is worth having when the alternative is a blank
        dashboard -- but only while it is being refreshed. Past that it decays
        to None rather than reporting a call count from an unknown time as if
        it were current.
        """
        value = state.get(key)
        if value is not None:
            return value
        if not self._ami_core:
            return None
        age = time.monotonic() - self._ami_core_at
        if age > self.config.metrics_interval_seconds + 30:
            return None
        return self._ami_core.get(key)

    def _paused(self) -> bool:
        """True while the cloud has told us to stop, for either reason."""
        now = time.monotonic()
        return now < self._unauthorized_until or now < self._throttled_until

    def _note_throttle(self, exc: RateLimited, what: str) -> None:
        self._throttled_until = time.monotonic() + exc.retry_after
        log.warning("cloud is throttling us (%s); pausing %ss", what, exc.retry_after)

    # ----------------------------------------------------------------- steps --
    def send_heartbeat(self) -> bool:
        if self._paused():
            return False
        payload = {k: v for k, v in self._heartbeat_payload().items() if v is not None}
        payload["agent_version"] = __version__
        try:
            self.client.post("/v1/agent/heartbeat", payload)
        except ApiError as exc:
            if exc.status in (401, 403):
                self._unauthorized_until = time.monotonic() + UNAUTHORIZED_BACKOFF_SECONDS
                log.error(
                    "credential rejected (HTTP %s). Re-enroll with a new token. Retrying in %ss.",
                    exc.status,
                    UNAUTHORIZED_BACKOFF_SECONDS,
                )
            else:
                log.warning("heartbeat rejected: %s", exc)
            return False
        except RateLimited as exc:
            self._note_throttle(exc, "heartbeat")
            return False
        except TransportError as exc:
            log.warning("heartbeat failed: %s", exc)
            return False

        log.debug("heartbeat delivered")
        return True

    def collect_metrics(self) -> None:
        now = datetime.now(timezone.utc)
        system_sample = system_collector.collect(self.cpu)
        system_sample["recorded_at"] = now.isoformat()

        asterisk_sample = asterisk_collector.collect()
        asterisk_sample["recorded_at"] = now.isoformat()
        asterisk_sample.update(self._endpoint_counts)

        try:
            self.buffer.append("system", system_sample, now)
            self.buffer.append("asterisk", asterisk_sample, now)
        except BufferFull as exc:
            log.error("dropping sample, local buffer is full: %s", exc)

    def send_extensions(self, extensions: List[Dict[str, Any]]) -> None:
        """Push the roster with fresh states. Never buffered.

        Like trunk state, this is a claim about the present. Replaying an
        hour-old "free" after an outage would put a green card on the board for
        somebody who went home.
        """
        try:
            self.client.post("/v1/agent/extensions", {"extensions": extensions})
        except ApiError as exc:
            if exc.status in (401, 403):
                self._unauthorized_until = time.monotonic() + UNAUTHORIZED_BACKOFF_SECONDS
                log.error("credential rejected while reporting extensions: %s", exc)
            else:
                # A 404 means this cloud predates the endpoint. Nothing else the
                # agent reports depends on it.
                log.debug("extension report rejected: %s", exc)
        except (RateLimited, TransportError) as exc:
            log.debug("extension report not delivered: %s", exc)

    def collect_channel_counts(self) -> None:
        """The cheap half of AMI: how many calls are up, and who is on one.

        Two actions, both of which Asterisk answers from state it already holds
        -- no walking of a channel driver's internals, and nothing that takes a
        lock the media path might want. That is what makes them safe on the fast
        cadence, and why the inventory pass they do *not* belong to runs every
        five minutes instead.

        Whether somebody is free has to be asked often or not at all: a call
        lasts a couple of minutes, so a five-minute-old "free" is wrong more
        often than it is right, and a panel is worse than useless when it is
        confidently wrong. The roster it is layered onto is cached from the slow
        pass, because who *exists* changes when somebody provisions a phone, not
        from minute to minute.
        """
        if not self.ami_enabled or self._paused():
            return
        if time.monotonic() < self._ami_quiet_until:
            return
        try:
            with AMIClient(
                host=self.config.asterisk.ami_host,
                port=self.config.asterisk.ami_port,
                username=self.credentials.ami_username,
                secret=self.credentials.ami_password,
            ) as ami:
                self._ami_core = ami_snapshot.core_counts(ami)
                states = extension_collector.device_states(ami) if self._extensions else {}
            self._ami_core_at = time.monotonic()
            self._ami_ok = True
            if self._extensions and not (self.panel_publisher and self.panel_publisher.active):
                self.send_extensions(extension_collector.apply_states(self._extensions, states))
        except AMIAuthError:
            self._ami_ok = False
            self._ami_quiet_until = time.monotonic() + AMI_AUTH_BACKOFF_SECONDS
        except AMIError as exc:
            self._ami_ok = False
            log.debug("channel count over AMI failed: %s", exc)

    def collect_and_send_ami(self) -> int:
        """Read trunk and queue state over AMI and push both.

        Neither is buffered. Unlike a metric sample, each is a claim about the
        present; replaying an hour-old "registered" after an outage would tell
        the cloud a trunk is up when it may have been down the whole time. If
        AMI is unreachable we send nothing at all -- an empty trunk list reads
        as "no trunks configured" and would resolve every open alert.
        """
        if not self.ami_enabled or time.monotonic() < self._ami_quiet_until:
            return 0
        # 0 means the operator switched the inventory pass off. The cheap
        # per-minute poll keeps running, so call counts and extension state
        # survive; only trunks and queues stop being refreshed.
        if self.config.asterisk.ami_interval_seconds == 0:
            return 0
        if self._paused():
            return 0

        try:
            result = ami_snapshot.collect(
                host=self.config.asterisk.ami_host,
                port=self.config.asterisk.ami_port,
                username=self.credentials.ami_username,
                secret=self.credentials.ami_password,
                strategies=self._queue_strategies,
            )
        except AMIAuthError:
            self._ami_ok = False
            self._ami_quiet_until = time.monotonic() + AMI_AUTH_BACKOFF_SECONDS
            log.error(
                "AMI rejected the credential. Check the [pbxonix] section of "
                "manager.conf and re-run 'pbxonix-agent set-ami'. Retrying in %ss.",
                AMI_AUTH_BACKOFF_SECONDS,
            )
            return 0
        except AMIError as exc:
            self._ami_ok = False
            log.warning("AMI collection failed, sending nothing this cycle: %s", exc)
            return 0

        self._ami_ok = True
        self._ami_core = result.get("core") or {}
        self._ami_core_at = time.monotonic()

        counts = result.get("endpoints") or {}
        self._endpoint_counts = {k: v for k, v in counts.items() if v is not None}

        trunks = result.get("trunks") or []
        queues = result.get("queues") or []
        # Kept so the fast poll can layer fresh device states onto it without
        # walking the peer containers again.
        self._extensions = result.get("extensions") or []
        self._extension_roster_at = time.monotonic()
        self._report_inventory = {
            "extensions": [str(row["extension"]) for row in self._extensions],
            "trunks": [str(row["name"]) for row in trunks],
            "queues": [str(row["name"]) for row in queues],
        }

        for path, payload, label in (
            ("/v1/agent/trunks", {"trunks": trunks}, "trunk"),
            ("/v1/agent/queues", {"queues": queues}, "queue"),
            ("/v1/agent/extensions", {"extensions": self._extensions}, "extension"),
        ):
            try:
                self.client.post(path, payload)
            except ApiError as exc:
                if exc.status in (401, 403):
                    self._unauthorized_until = time.monotonic() + UNAUTHORIZED_BACKOFF_SECONDS
                    log.error("credential rejected while reporting %ss: %s", label, exc)
                    return 0
                # A 404 means this cloud predates the endpoint. Keep going: the
                # other report is still useful and still wanted.
                log.warning("%s report rejected: %s", label, exc)
            except RateLimited as exc:
                self._note_throttle(exc, label + " report")
                return 0
            except TransportError as exc:
                log.debug("%s report deferred: %s", label, exc)

        log.debug("reported %s trunks, %s queues", len(trunks), len(queues))
        return len(trunks) + len(queues)

    def flush_buffer(self) -> int:
        if self._paused():
            return 0

        system_rows = self.buffer.take("system", FLUSH_BATCH)
        asterisk_rows = self.buffer.take("asterisk", FLUSH_BATCH)
        if not system_rows and not asterisk_rows:
            return 0

        payload = {
            "system": [row for _, row in system_rows],
            "asterisk": [row for _, row in asterisk_rows],
        }
        try:
            self.client.post("/v1/agent/metrics", payload)
        except ApiError as exc:
            if exc.status in (401, 403):
                self._unauthorized_until = time.monotonic() + UNAUTHORIZED_BACKOFF_SECONDS
                log.error("credential rejected while flushing: %s", exc)
                return 0
            # A 4xx that is not an auth problem means the server will never
            # accept these rows. Keeping them would block the queue forever.
            log.error("server rejected buffered samples, discarding them: %s", exc)
            self.buffer.delete([i for i, _ in system_rows])
            self.buffer.delete([i for i, _ in asterisk_rows])
            return 0
        except RateLimited as exc:
            # The rows stay in the buffer. This is the branch the discard above
            # would otherwise have taken.
            self._note_throttle(exc, "metrics flush")
            return 0
        except TransportError as exc:
            log.debug("flush deferred: %s", exc)
            return 0

        self.buffer.delete([i for i, _ in system_rows])
        self.buffer.delete([i for i, _ in asterisk_rows])
        sent = len(system_rows) + len(asterisk_rows)
        log.info("flushed %s buffered samples (%s remaining)", sent, self.buffer.count())
        return sent

    def collect_and_send_recordings(self) -> int:
        """Scan the recording spool and push the aggregates.

        Never buffered and never retried. A scan is cheap to repeat and its
        result is a statement about the filesystem right now; replaying an old
        one would tell the cloud recordings were fresh when they were not,
        which is precisely the failure this exists to catch.
        """
        if self._paused():
            return 0

        try:
            payload = self.recordings.collect()
        except OSError as exc:
            log.warning("recording scan failed: %s", exc)
            return 0
        if payload is None:
            return 0

        if payload.get("truncated"):
            log.info(
                "recording scan hit its limit after %ss; totals are best-effort "
                "but the freshness signal is not",
                payload.get("scan_seconds"),
            )

        try:
            self.client.post("/v1/agent/recordings", payload)
        except ApiError as exc:
            if exc.status in (401, 403):
                self._unauthorized_until = time.monotonic() + UNAUTHORIZED_BACKOFF_SECONDS
                log.error("credential rejected while reporting recordings: %s", exc)
            else:
                log.warning("recording report rejected: %s", exc)
            return 0
        except RateLimited as exc:
            self._note_throttle(exc, "recording report")
            return 0
        except TransportError as exc:
            log.debug("recording report deferred: %s", exc)
            return 0

        return 1

    # ------------------------------------------------------------------ loop --
    def run(self) -> int:
        log.info(
            "pbxonix-agent %s starting for pbx %s (%s), AMI %s, recording paths %s",
            __version__,
            self.credentials.pbx_name or "unnamed",
            self.credentials.pbx_id,
            "enabled" if self.ami_enabled else "disabled",
            len(self.config.recordings.paths),
        )
        if self.config.asterisk.ami_enabled and not self.credentials.has_ami:
            log.warning(
                "ami_enabled is true but no AMI credential is stored. "
                "Run 'pbxonix-agent set-ami --username pbxonix' to add one."
            )

        # Prime the CPU sampler so the first heartbeat carries a real reading
        # rather than a null.
        self.cpu.sample()

        if self.ami_enabled and self.config.panel_interval_seconds:
            self.panel_publisher = PanelPublisher(
                self.config, self.credentials, lambda: (self._extensions, self._extension_roster_at)
            )
            self.panel_publisher.start()

        if self.ami_enabled:
            self.quality_publisher = QualityPublisher(
                self.config, self.credentials, lambda: self._report_inventory
            )
            self.quality_publisher.start()

        if self.config.queue_daily.enabled:
            self.queue_daily_publisher = QueueDailyPublisher(self.config, self.credentials)
            self.queue_daily_publisher.start()

        if self.config.reports.enabled:
            self.reports_publisher = ReportsPublisher(
                self.config, self.credentials, lambda: self._report_inventory
            )
            self.reports_publisher.start()

        next_heartbeat = 0.0
        next_metrics = 0.0
        next_ami = 0.0
        next_recordings = 0.0
        try:
            while not self._stop:
                now = time.monotonic()

                if now >= next_heartbeat:
                    self.send_heartbeat()
                    next_heartbeat = now + self.config.heartbeat_interval_seconds

                if now >= next_ami:
                    # The expensive half: SIPpeers locks chan_sip's peer
                    # container while it walks it, and QueueStatus iterates every
                    # member of every queue. Running this every minute on a busy
                    # PBX made call audio break up, so it has its own cadence.
                    self.collect_and_send_ami()
                    # 0 means off. Rescheduling at now + 0 would call it again
                    # on the next tick forever, so park it a minute out where it
                    # will return immediately and cost nothing.
                    next_ami = now + (self.config.asterisk.ami_interval_seconds or 60)

                if now >= next_metrics:
                    # Cheap enough to keep on the fast cadence, and the only
                    # genuinely live figure AMI gives us.
                    self.collect_channel_counts()
                    self.collect_metrics()
                    self.flush_buffer()
                    next_metrics = now + self.config.metrics_interval_seconds

                if now >= next_recordings:
                    self.collect_and_send_recordings()
                    next_recordings = now + self.config.recordings.interval_seconds

                time.sleep(1)
        finally:
            self.close()
        log.info("pbxonix-agent stopped")
        return 0
