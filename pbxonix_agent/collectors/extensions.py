"""The phones that register to this PBX, and what each is doing.

The trunk collector already walks the same peer lists and throws these away:
``Dynamic: yes`` on a chan_sip peer means something that registers *to us*,
which is an extension rather than a provider. That was the right call for trunk
monitoring and the wrong one for an operator panel, so this reads the half that
was discarded.

**Two questions, two sources.** Whether a phone is registered at all comes from
the peer list -- it is a property of the peer and changes when somebody unplugs
a handset. Whether the person on it is busy comes from ``DeviceStateList``,
which is Asterisk's own device-state core and answers for every driver at once.
The first is stable and cheap to ask rarely; the second is the whole point of a
panel and is one small action, so it can be asked often.

**No new connection.** Everything here runs on the AMI session the inventory
pass already opens.

Nothing identifying is read. A peer entry is an extension number and a
reachability string; no caller, no number, no name of a person. The panel shows
who is free, not who they are talking to.
"""

from typing import Dict, List, Optional, Set

from pbxonix_agent.collectors.ami import AMIClient, AMIError

# The panel's vocabulary. Asterisk says "OK (12 ms)", "UNREACHABLE",
# "Unmonitored", "Not in use", "NOT_INUSE" and more besides, depending on driver
# and version; a wallboard has room for one word.
FREE = "free"
BUSY = "busy"
RINGING = "ringing"
OFFLINE = "offline"
UNKNOWN = "unknown"

# DeviceStateList / DeviceStateChange states, which are the same strings the
# dialplan's DEVICE_STATE() returns.
_DEVICE_STATES = {
    "not_inuse": FREE,
    "inuse": BUSY,
    "busy": BUSY,
    "ringing": RINGING,
    "ringinuse": BUSY,
    "onhold": BUSY,
    "unavailable": OFFLINE,
    "invalid": UNKNOWN,
    "unknown": UNKNOWN,
}

# PJSIPShowEndpoints spells the same thing with spaces and capitals.
_ENDPOINT_STATES = {
    "not in use": FREE,
    "in use": BUSY,
    "busy": BUSY,
    "ringing": RINGING,
    "ring, in use": BUSY,
    "on hold": BUSY,
    "unavailable": OFFLINE,
    "invalid": UNKNOWN,
    "unknown": UNKNOWN,
}


def _reachable(status: str) -> str:
    """A chan_sip peer's Status line, reduced to registered or not.

    It cannot say whether the person is busy -- only whether the handset is
    talking to us. "OK (12 ms)" is a qualify round-trip, and a peer that is not
    qualified at all reports "Unmonitored", which is neither good nor bad news.
    """
    text = status.strip().lower()
    if text.startswith("ok"):
        return FREE
    if text.startswith("unreachable") or text.startswith("lagged"):
        return OFFLINE
    if text.startswith("unmonitored"):
        # Registered, but nobody is measuring it. Not the same as unreachable.
        return UNKNOWN
    return UNKNOWN


def sip_extensions(client: AMIClient) -> List[Dict[str, Optional[str]]]:
    """chan_sip peers that register to us.

    The mirror image of the trunk collector's ``sip_peers``: it keeps the
    static ones, this keeps the dynamic ones.
    """
    rows: List[Dict[str, Optional[str]]] = []
    try:
        entries = client.action_list("SIPpeers", ("PeerlistComplete",))
    except AMIError:
        # chan_sip is not loaded, which is normal on Asterisk 21 and later.
        return rows

    for item in entries:
        if item.get("event", "").lower() != "peerentry":
            continue
        if (item.get("dynamic") or "").strip().lower() != "yes":
            continue
        name = (item.get("objectname") or "").strip()
        if not name:
            continue
        status = (item.get("status") or "").strip()
        rows.append(
            {
                "extension": name,
                "tech": "SIP",
                "state": _reachable(status),
                "detail": status or None,
            }
        )
    return rows


def pjsip_extensions(
    client: AMIClient, trunk_names: Optional[Set[str]] = None
) -> List[Dict[str, Optional[str]]]:
    """PJSIP endpoints, minus the ones already known to be trunks.

    PJSIP does not label an endpoint as extension or trunk; the distinction is
    in how its AOR is configured, which the trunk collector has already worked
    out. Rather than repeat that reasoning here and risk the two disagreeing,
    the names it found are passed in and excluded.

    **An endpoint and its AOR are separate objects with separate names**, and
    matching on the endpoint's own name alone is not enough. On a real PBX:

        endpoint elevenlabs-endpoint  ->  aor elevenlabs-aor

    Only the AOR is classifiable, so only the AOR appears in the trunk list;
    the endpoint sailed past the exclusion and showed up as a person. The
    endpoint carries its AOR names in the event, so the link is read rather
    than inferred. Asterisk has spelled that field both `Aor` and `Aors`
    depending on version, and both are checked because guessing which is
    another round of this.
    """
    skip = {n.lower() for n in (trunk_names or set())}
    rows: List[Dict[str, Optional[str]]] = []
    try:
        entries = client.action_list("PJSIPShowEndpoints", ("EndpointListComplete",))
    except AMIError:
        return rows

    for item in entries:
        if item.get("event", "").lower() != "endpointlist":
            continue
        name = (item.get("objectname") or "").strip()
        if not name or name.lower() in skip:
            continue
        # An endpoint may list several AORs. If any one of them is a trunk, the
        # endpoint is the trunk's endpoint.
        aors = item.get("aors") or item.get("aor") or ""
        if any(part.strip().lower() in skip for part in aors.split(",") if part.strip()):
            continue
        raw = (item.get("devicestate") or "").strip()
        rows.append(
            {
                "extension": name,
                "tech": "PJSIP",
                "state": _ENDPOINT_STATES.get(raw.lower(), UNKNOWN),
                "detail": raw or None,
            }
        )
    return rows


def device_states(client: AMIClient) -> Dict[str, str]:
    """Every device Asterisk tracks a state for, keyed by name.

    One action, no walking of driver-internal containers, and it answers for
    chan_sip and PJSIP alike. Absent before Asterisk 12 -- an Issabel 4 running
    Asterisk 11 gets an error here, which is why the caller treats an empty
    result as "no better information" rather than as "everything is idle".
    """
    states: Dict[str, str] = {}
    try:
        entries = client.action_list("DeviceStateList", ("DeviceStateListComplete",))
    except AMIError:
        return states

    for item in entries:
        if item.get("event", "").lower() != "devicestatechange":
            continue
        device = (item.get("device") or "").strip()
        raw = (item.get("state") or "").strip()
        if device and raw:
            states[device.lower()] = _DEVICE_STATES.get(raw.lower(), UNKNOWN)
    return states


def roster(
    client: AMIClient, trunk_names: Optional[Set[str]] = None
) -> List[Dict[str, Optional[str]]]:
    """Who exists, and whether their handset is reachable.

    Walking the peer lists is the expensive half -- ``SIPpeers`` holds
    chan_sip's whole peer container while it iterates -- so this belongs on the
    slow inventory cadence. It is also the half that barely changes: extensions
    appear when somebody provisions a phone, not from minute to minute.
    """
    rows = sip_extensions(client)
    seen = {str(row["extension"]).lower() for row in rows}
    for row in pjsip_extensions(client, trunk_names):
        if str(row["extension"]).lower() not in seen:
            rows.append(row)
    return rows


def apply_states(
    rows: List[Dict[str, Optional[str]]], states: Dict[str, str]
) -> List[Dict[str, Optional[str]]]:
    """Layer device state over a copy of the roster.

    Order matters. The roster decides *who exists*: a device state for
    something not in it is a queue, a park lot or a custom hint, none of which
    belong on a panel of people. Device state then decides *what they are
    doing*, because it knows about calls and the peer list does not.

    A device state may not overwrite an unreachable extension. Asterisk reports
    NOT_INUSE for a handset that is switched off, and letting that win would put
    a green card on the board for a phone nobody can answer.
    """
    rows = [dict(row) for row in rows]
    if not states:
        return rows
    for row in rows:
        if row["state"] == OFFLINE:
            continue
        for tech in (row["tech"], "SIP", "PJSIP"):
            live = states.get("{}/{}".format(tech, row["extension"]).lower())
            if live is not None:
                row["state"] = live
                break
    return rows


def collect(
    client: AMIClient, trunk_names: Optional[Set[str]] = None
) -> List[Dict[str, Optional[str]]]:
    """Both halves in one pass, for a caller that has no roster cached yet."""
    return apply_states(roster(client, trunk_names), device_states(client))
