"""Call queue state, via AMI ``QueueSummary``.

``QueueSummary`` returns **one event per queue** carrying exactly the four
numbers the dashboard uses: ``LoggedIn``, ``Available``, ``Callers`` and
``LongestHoldTime``. Nothing else is read from a queue, so nothing else is
asked for.

This used to use ``QueueStatus``, which returns those numbers by enumerating
every member and every waiting caller of every queue -- on a real PBX with 31
queues that is upwards of 250 events, and ``app_queue`` holds each queue's lock
while it walks it. That was measured on a production system whose operators
reported audible artefacts during calls. A monitoring agent that degrades the
calls it is watching has failed at its job however good its data is.

``strategy`` is the one field ``QueueSummary`` does not carry. It changes when
somebody edits the queue, never on its own, so it is fetched once with
``QueueStatus`` for queues whose strategy is not yet known and never asked for
again.

**Privacy.** The heavy action carries ``CallerIDNum`` and ``CallerIDName`` on
every waiting caller -- the phone number of a real person on hold. Nothing that
identifies a caller was ever extracted, and ``QueueSummary`` does not report one
at all, so now there is nothing to decline to read.
"""

from typing import Dict, List, Optional

from pbxonix_agent.collectors.ami import AMIClient, AMIError

_SUMMARY_COMPLETE = ("QueueSummaryComplete",)
_STATUS_COMPLETE = ("QueueStatusComplete",)


def _int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value.strip())
    except (ValueError, AttributeError):
        return None


def queue_strategies(client: AMIClient) -> Dict[str, str]:
    """Every queue's strategy, from the expensive action.

    Called once per agent lifetime, and again only if a queue appears that
    nothing has a strategy for. QueueStatus enumerates members and waiting
    callers to tell us a word that changes when somebody edits a config file.
    """
    try:
        items = client.action_list("QueueStatus", _STATUS_COMPLETE)
    except AMIError:
        return {}

    found: Dict[str, str] = {}
    for item in items:
        if item.get("event", "").lower() != "queueparams":
            continue
        name = item.get("queue")
        strategy = item.get("strategy")
        if name and strategy:
            found[name] = strategy
    return found


def collect_queues(
    client: AMIClient, strategies: Optional[Dict[str, str]] = None
) -> List[Dict[str, object]]:
    """Read every queue. Empty list if app_queue is not loaded.

    `strategies` is a cache the caller owns. Any queue missing from it triggers
    one QueueStatus, after which it will not be asked for again -- so the cost
    is paid on the first pass and when a queue is added, not every cycle.
    """
    try:
        items = client.action_list("QueueSummary", _SUMMARY_COMPLETE)
    except AMIError:
        # Either app_queue is not loaded -- plenty of installs have no queues --
        # or this Asterisk does not offer the light action. The two are
        # indistinguishable from here, and returning nothing would empty the
        # dashboard's queue list, which reads as "this PBX has no queues" rather
        # than "we could not ask". So fall back to the expensive action and let
        # it decide: it returns nothing of its own accord when app_queue is
        # genuinely absent.
        return _collect_via_status(client, strategies)

    queues: List[Dict[str, object]] = []
    names: List[str] = []
    for item in items:
        if item.get("event", "").lower() != "queuesummary":
            continue
        name = item.get("queue")
        if not name:
            continue
        names.append(name)
        waiting = _int(item.get("callers"))
        longest = _int(item.get("longestholdtime"))
        # QueueSummary reports LongestHoldTime 0 for an empty queue, which is a
        # different claim from the one the dashboard should make: "nobody is
        # waiting" is not "somebody has waited no time at all", and a gap renders
        # as a dash rather than a reassuring zero.
        if waiting == 0:
            longest = None
        queues.append(
            {
                "name": name,
                "strategy": None,
                "calls_waiting": waiting,
                "agents_logged_in": _int(item.get("loggedin")),
                "agents_available": _int(item.get("available")),
                "longest_wait_seconds": longest,
            }
        )

    if strategies is not None:
        if any(name not in strategies for name in names):
            strategies.update(queue_strategies(client))
        for entry in queues:
            entry["strategy"] = strategies.get(str(entry["name"]))

    return queues


def _collect_via_status(
    client: AMIClient, strategies: Optional[Dict[str, str]]
) -> List[Dict[str, object]]:
    """The old reading, kept as a fallback rather than as the normal path.

    Costly by construction: it arrives at four numbers per queue by enumerating
    every member and every waiting caller. That is what made a production PBX's
    calls audibly worse, so it runs only where the summary is unavailable.
    """
    try:
        items = client.action_list("QueueStatus", _STATUS_COMPLETE)
    except AMIError:
        return []

    queues: Dict[str, Dict[str, object]] = {}

    def bucket(name: str) -> Dict[str, object]:
        return queues.setdefault(
            name,
            {
                "name": name,
                "strategy": None,
                "calls_waiting": None,
                "agents_logged_in": 0,
                "agents_available": 0,
                "longest_wait_seconds": None,
            },
        )

    for item in items:
        event = item.get("event", "").lower()
        name = item.get("queue")
        if not name:
            continue
        entry = bucket(name)

        if event == "queueparams":
            entry["strategy"] = item.get("strategy") or None
            entry["calls_waiting"] = _int(item.get("calls"))
            if strategies is not None and entry["strategy"]:
                strategies[name] = str(entry["strategy"])

        elif event == "queuemember":
            entry["agents_logged_in"] = int(entry["agents_logged_in"]) + 1
            paused = (item.get("paused") or "0").strip()
            # Status 1 is the only device state that means "could take a call
            # right now"; the rest are in use, ringing, invalid or unreachable.
            if item.get("status", "").strip() == "1" and paused == "0":
                entry["agents_available"] = int(entry["agents_available"]) + 1

        elif event == "queueentry":
            # Deliberately only Wait. CallerIDNum and CallerIDName are right
            # there in this event and are never touched.
            wait = _int(item.get("wait"))
            if wait is None:
                continue
            longest = entry["longest_wait_seconds"]
            if longest is None or wait > int(longest):
                entry["longest_wait_seconds"] = wait

    return list(queues.values())
