"""Lightweight panel sampling, independent of metrics and recording scans.

Only cached device/hint states and QueueSummary run on this cadence. SIPpeers and
QueueStatus remain on the existing slow inventory schedule. System PeerStatus
events on the persistent session track reachability between inventories.
"""

import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Tuple

from pbxonix_agent.collectors.ami import AMIAuthError, AMIClient, AMIError
from pbxonix_agent.collectors.extensions import _DEVICE_STATES
from pbxonix_agent.config import Config
from pbxonix_agent.credentials import Credentials
from pbxonix_agent.transport import ApiError, Client, RateLimited, TransportError

log = logging.getLogger("pbxonix.panel")


def hint_states(roster, items, devices):
    """Fill missing physical devices from unambiguous dialplan hints.

    chan_sip does not populate DeviceStateList for every idle/offline peer.
    Never interpret MWI, queue, parking or another handset as this operator.
    """
    known = {
        row["extension"]: "{}/{}".format(row.get("tech") or "", row["extension"]).lower()
        for row in roster
    }
    result = {}
    values = {
        0: "NOT_INUSE",
        1: "INUSE",
        2: "BUSY",
        4: "UNAVAILABLE",
        8: "RINGING",
        9: "RINGINUSE",
        16: "ONHOLD",
        17: "ONHOLD",
    }
    for item in items:
        extension = item.get("exten", "")
        device = known.get(extension)
        if item.get("event", "").lower() != "extensionstatus" or not device or device in devices:
            continue
        parts = set(item.get("hint", "").lower().split(",", 1)[0].split("&"))
        dnd = "custom:dnd" + extension.lower()
        if device not in parts or parts - {device, dnd}:
            continue
        try:
            status = int(item.get("status", ""))
        except ValueError:
            continue
        raw = values.get(status, "UNKNOWN")
        # A composite busy hint can mean DND rather than a phone call.
        if dnd in parts and status not in (0, 4) and devices.get(dnd, "").upper() != "NOT_INUSE":
            raw = "UNKNOWN"
        if device in result and result[device] != raw:
            raw = "UNKNOWN"
        result[device] = raw
    return result


def operator_samples(roster, states, peers, roster_at):
    # Missing device states are not fresh observations of the cached roster.
    result = []
    for row in roster:
        device = "{}/{}".format(row.get("tech") or "", row["extension"]).lower()
        raw = states.get(device)
        if raw is None:
            continue
        state = _DEVICE_STATES.get(raw.lower(), "unknown")
        peer = peers.get(device)
        reachable = peer[0] if peer and peer[1] > roster_at else None
        if state not in ("busy", "ringing"):
            if reachable is False:
                state = "offline"
            elif state == "free" and row.get("state") == "offline" and reachable is not True:
                # NOT_INUSE by itself does not establish that an offline SIP
                # handset registered again. PeerStatus or inventory must do it.
                state = "offline"
        result.append(
            dict(
                extension=row["extension"],
                tech=row.get("tech"),
                state=state,
                detail=raw if state != "offline" else "Unavailable",
            )
        )
    return result


def queue_samples(items):
    def number(value):
        try:
            return max(0, int(value))
        except (ValueError, TypeError):
            return None

    result = []
    for item in items:
        if item.get("event", "").lower() != "queuesummary" or not item.get("queue"):
            continue
        waiting = number(item.get("callers"))
        result.append(
            dict(
                name=item["queue"],
                calls_waiting=waiting,
                agents_available=number(item.get("available")),
                agents_logged_in=number(item.get("loggedin")),
                longest_wait_seconds=number(item.get("longestholdtime")) if waiting else None,
            )
        )
    return result


class PanelPublisher(threading.Thread):
    def __init__(
        self,
        config: Config,
        credentials: Credentials,
        roster: Callable[[], Tuple[List[Dict[str, Any]], float]],
    ) -> None:
        super().__init__(name="pbxonix-panel", daemon=True)
        self.config = config
        self.credentials = credentials
        self.roster = roster
        self.stop_event = threading.Event()
        self.active = False
        self.peers = {}  # type: Dict[str, Tuple[bool, float]]
        self.client = Client(
            config.api_base_url,
            token=credentials.agent_token,
            verify_tls=config.verify_tls,
            timeout=10,
        )

    def stop(self) -> None:
        self.stop_event.set()

    def on_event(self, event: Dict[str, str]) -> None:
        if event.get("event", "").lower() != "peerstatus":
            return
        device = event.get("peer", "").lower()
        roster, _ = self.roster()
        known = {"{}/{}".format(row.get("tech") or "", row["extension"]).lower() for row in roster}
        if device not in known:
            return
        status = event.get("peerstatus", "").lower()
        if status in ("reachable", "registered"):
            self.peers[device] = (True, time.monotonic())
        elif status in ("unreachable", "unregistered", "lagged", "rejected"):
            self.peers[device] = (False, time.monotonic())
        self.peers = {key: value for key, value in self.peers.items() if key in known}

    def collect(self, ami: AMIClient) -> Dict[str, Any]:
        measured = datetime.now(timezone.utc).isoformat()
        roster, roster_at = self.roster()
        operators, queues = None, None
        states = {}
        observed = False
        try:
            items = ami.action_list("DeviceStateList", ("DeviceStateListComplete",))
            states = {
                item.get("device", "").lower(): item["state"]
                for item in items
                if item.get("event", "").lower() == "devicestatechange" and item.get("state")
            }
            observed = True
        except AMIError:
            pass
        if any(
            "{}/{}".format(row.get("tech") or "", row["extension"]).lower() not in states
            for row in roster
        ):
            try:
                hints = ami.action_list("ExtensionStateList", ("ExtensionStateListComplete",))
                states.update(hint_states(roster, hints, states))
                observed = True
            except AMIError:
                pass
        if observed:
            operators = operator_samples(roster, states, self.peers, roster_at)
        try:
            queues = queue_samples(ami.action_list("QueueSummary", ("QueueSummaryComplete",)))
        except AMIError:
            pass
        if operators is None and queues is None:
            raise AMIError("Lightweight panel actions are unavailable")
        return dict(
            recorded_at=measured,
            interval_seconds=self.config.panel_interval_seconds,
            operators=operators,
            queues=queues,
        )

    def run(self) -> None:
        ami = None
        failures = 0
        try:
            while not self.stop_event.is_set():
                start = time.monotonic()
                delay = self.config.panel_interval_seconds
                try:
                    if ami is None:
                        ami = AMIClient(
                            host=self.config.asterisk.ami_host,
                            port=self.config.asterisk.ami_port,
                            username=self.credentials.ami_username,
                            secret=self.credentials.ami_password,
                            timeout=5,
                            events="system",
                            event_handler=self.on_event,
                        )
                        ami.connect()
                        ami.login()
                        self.peers.clear()
                    payload = self.collect(ami)
                    self.client.post("/v1/agent/panel", payload)
                    self.active = True
                    failures = 0
                except (AMIError, TransportError, ApiError) as exc:
                    self.active = False
                    failures = min(failures + 1, 6)
                    delay = min(300, 5 * (2 ** (failures - 1)))
                    if isinstance(exc, AMIAuthError):
                        delay = 900
                    elif isinstance(exc, RateLimited):
                        delay = max(delay, exc.retry_after)
                    elif isinstance(exc, ApiError) and exc.status in (401, 403, 404):
                        delay = 300
                    # Never log AMI payloads, usernames, tokens, or HTTP bodies.
                    log.warning(
                        "panel sample unavailable (%s); next attempt in %ss",
                        type(exc).__name__,
                        delay,
                    )
                    if ami:
                        ami.close()
                        ami = None
                self.stop_event.wait(max(0.2, delay - (time.monotonic() - start)))
        finally:
            if ami:
                ami.close()
