"""RTCP statistics only. No audio, addresses, caller IDs or raw events leave RAM.

A separate AMI reporting session cannot delay the existing monitoring loop.
Minute aggregates and the retry queue contain only allowlisted inventory names
and numbers. The queue is deliberately bounded; gaps are reported, never filled.
"""

import logging
import math
import re
import socket
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone

from pbxonix_agent.collectors.ami import AMIAuthError, AMIClient, AMIError
from pbxonix_agent.collectors.asterisk import _run_detailed
from pbxonix_agent.report_privacy import endpoint
from pbxonix_agent.transport import ApiError, Client, TransportError

log = logging.getLogger("pbxonix.quality")
LIMITS = {"loss": 2.0, "jitter": 30.0, "rtt": 300.0}
# RTP clock, not audio sample rate: G.722 is explicitly 8 kHz (RFC 3551).
CLOCKS = {
    "ulaw": 8000,
    "alaw": 8000,
    "gsm": 8000,
    "g729": 8000,
    "g722": 8000,
    "g726": 8000,
    "g726aal2": 8000,
    "g723": 8000,
    "ilbc": 8000,
    "opus": 48000,
}
CHANNEL = re.compile(r"^(?:SIP|PJSIP)/[A-Za-z0-9_.@+:-]{1,200}-[a-fA-F0-9]{6,}$")


def numeric(value, maximum):
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= maximum else None
    except (TypeError, ValueError):
        return None


def codec_clock(text):
    match = re.search(r"^\s*NativeFormats:\s*\(([^)]+)\)\s*$", text, re.M)
    if not match:
        return None
    formats = match.group(1).split("|")
    clocks = {CLOCKS.get(value.strip()) for value in formats}
    return clocks.pop() if len(clocks) == 1 and None not in clocks else None


class Accumulator:
    def __init__(self, inventory, clock=None):
        self.inventory = inventory
        self.clock = clock or (lambda channel: None)
        self.rows = {}
        self.dropped = 0

    def on_event(self, event):
        kind = event.get("event", "").lower()
        if kind not in ("rtcpreceived", "rtcpsent"):
            return
        # Ignore non-audio channel technologies and multi-report media where
        # the report SSRC cannot be matched safely to a negotiated codec.
        channel = event.get("channel", "")
        if not CHANNEL.fullmatch(channel):
            return
        count = numeric(event.get("reportcount"), 31)
        if not count or count != int(count):
            return
        inventory = self.inventory() or {}
        name = endpoint(channel)
        subject_kind = "unmapped"
        if name in inventory.get("trunks", []):
            subject_kind = "trunk"
        elif name in inventory.get("extensions", []):
            subject_kind = "extension"
        if subject_kind == "unmapped":
            name = ""
        direction = "outbound" if kind == "rtcpreceived" else "inbound"
        key = (subject_kind, name, direction)
        if key not in self.rows:
            if len(self.rows) >= 500:
                self.dropped += int(count)
                return
            self.rows[key] = dict(
                subject_kind=subject_kind,
                subject=name,
                direction=direction,
                reports=0,
                degraded=0,
                **{metric: dict(count=0, total=0.0, peak=None) for metric in LIMITS},
            )
        row = self.rows[key]
        rate = self.clock(channel) if count == 1 else None
        for index in range(int(count)):
            prefix = "report{}".format(index)
            fraction = numeric(event.get(prefix + "fractionlost"), 255)
            jitter = numeric(event.get(prefix + "iajitter"), 4294967295)
            rtt = numeric(event.get("rtt"), 60) if direction == "outbound" else None
            # RTT=0 before an SR has been acknowledged is not a measured zero.
            if not numeric(event.get(prefix + "lsr"), 4294967295):
                rtt = None
            values = dict(
                loss=fraction * 100 / 256 if fraction is not None else None,
                jitter=jitter * 1000 / rate if jitter is not None and rate else None,
                rtt=rtt * 1000 if rtt is not None else None,
            )
            values = {
                k: v if v is not None and v <= (100 if k == "loss" else 60000) else None
                for k, v in values.items()
            }
            if all(value is None for value in values.values()):
                continue
            row["reports"] += 1
            row["degraded"] += int(any(v is not None and v >= LIMITS[k] for k, v in values.items()))
            for metric, value in values.items():
                if value is not None:
                    aggregate = row[metric]
                    aggregate["count"] += 1
                    aggregate["total"] += value
                    aggregate["peak"] = max(aggregate["peak"] or 0, value)

    def drain(self, at, state):
        rows = [row for row in self.rows.values() if row["reports"]]
        payload = dict(
            batch_id=str(uuid.uuid4()),
            recorded_at=at.isoformat(),
            state=state,
            dropped=self.dropped,
            rows=rows,
        )
        self.rows = {}
        self.dropped = 0
        return payload


class QualityPublisher(threading.Thread):
    def __init__(self, config, credentials, inventory):
        super().__init__(name="pbxonix-quality", daemon=True)
        self.config = config
        self.credentials = credentials
        self.stop_event = threading.Event()
        self.cache = {}
        self.lookup_count = 0
        self.accumulator = Accumulator(inventory, self.clock)
        self.client = Client(
            config.api_base_url,
            token=credentials.agent_token,
            verify_tls=config.verify_tls,
            timeout=10,
        )

    def stop(self):
        self.stop_event.set()

    def clock(self, channel):
        now = time.monotonic()
        cached = self.cache.get(channel)
        if cached and now - cached[0] < 60:
            return cached[1]
        # Bound CLI work on busy PBXs. Unknown clocks produce null jitter.
        if self.lookup_count >= 30:
            return None
        self.lookup_count += 1
        output, error = _run_detailed(["asterisk", "-rx", "core show channel " + channel], 1)
        value = codec_clock(output or "") if not error else None
        if value is None:
            # The narrow socket service returns only an RTP clock, while the
            # main agent keeps its existing OS and AMI monitoring restrictions.
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                    connection.settimeout(1.5)
                    connection.connect("/run/pbxonix-quality-clock.sock")
                    connection.sendall((channel + "\n").encode("ascii"))
                    value = int(connection.recv(16).strip())
                    if value not in set(CLOCKS.values()):
                        value = None
            except (OSError, ValueError, UnicodeError):
                value = None
        self.cache = {k: v for k, v in self.cache.items() if now - v[0] < 60}
        self.cache[channel] = (now, value)
        return value

    def run(self):
        ami = None
        queue = deque()
        reconnect = 0.0
        next_publish = time.monotonic() + 60
        next_ping = 0.0
        try:
            while not self.stop_event.is_set():
                now = time.monotonic()
                try:
                    if ami is None and now >= reconnect:
                        ami = AMIClient(
                            self.config.asterisk.ami_host,
                            self.config.asterisk.ami_port,
                            self.credentials.ami_username,
                            self.credentials.ami_password,
                            timeout=4,
                            events="reporting",
                            event_handler=self.accumulator.on_event,
                        )
                        ami.connect()
                        ami.login()
                    if ami:
                        ami.pump_events(0.5)
                        if now >= next_ping:
                            ami.action("Ping")
                            next_ping = now + 15
                    else:
                        self.stop_event.wait(0.5)
                except (AMIError, OSError) as exc:
                    if ami:
                        ami.close()
                    ami = None
                    reconnect = now + (900 if isinstance(exc, AMIAuthError) else 30)
                    log.warning("RTCP session unavailable (%s)", type(exc).__name__)
                if time.monotonic() >= next_publish:
                    payload = self.accumulator.drain(
                        datetime.now(timezone.utc), "listening" if ami else "unavailable"
                    )
                    if len(queue) >= 5:
                        old = queue.popleft()
                        payload["dropped"] += (
                            sum(row["reports"] for row in old["rows"]) + old["dropped"]
                        )
                    queue.append(payload)
                    self.lookup_count = 0
                    next_publish = time.monotonic() + 60
                    try:
                        while queue and not self.stop_event.is_set():
                            self.client.post("/v1/agent/quality", queue[0])
                            queue.popleft()
                    except (TransportError, ApiError) as exc:
                        log.warning("RTCP delivery postponed (%s)", type(exc).__name__)
        finally:
            if ami:
                ami.close()
