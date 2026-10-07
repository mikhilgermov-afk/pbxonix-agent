"""SIP trunk state, via AMI.

Two kinds of trunk, and they are visible in different places.

A **registering** trunk keeps an outbound registration with the provider, read
from ``PJSIPShowRegistrationsOutbound`` and ``SIPshowregistry``. A PBX can run
both drivers at once, so both are attempted and merged.

An **IP-authenticated** trunk never registers, so it appears in neither list. It
exists only as a configured peer with a static host. Reading just the
registrations makes those trunks invisible -- not merely unreported but
un-alertable, which is the worse half. They are found instead in ``SIPpeers``
(entries that are not ``Dynamic``) and, for PJSIP, in ``PJSIPShowAors`` (AORs
carrying a static contact).

The two kinds cannot be correlated. One provider commonly has a dozen
registrations against one hostname and a dozen peers against the one IP that
hostname resolves to, with unrelated names on each side. So they are reported as
what they are, tagged by ``kind``, rather than guessed into pairs.

Raw Asterisk status strings are normalised to the five states the cloud knows
about. The original string is carried through as ``detail`` -- it is what ends
up in the alert, and "Rejected" tells an operator far more than "down".
"""

from typing import Dict, List, Optional, Set

from pbxonix_agent.collectors.ami import AMIClient, AMIError

# The cloud's TrunkState enum.
REGISTERED = "registered"
UNREGISTERED = "unregistered"
REJECTED = "rejected"
UNREACHABLE = "unreachable"
# A peer that answers qualify. Deliberately not REGISTERED: it never registers,
# and saying it did would be a small lie told on every dashboard.
REACHABLE = "reachable"
UNKNOWN = "unknown"

KIND_REGISTRATION = "registration"
KIND_PEER = "peer"

# res_pjsip_outbound_registration.c: sip_outbound_registration_status_str
_PJSIP_STATUS = {
    "registered": REGISTERED,
    "unregistered": UNREGISTERED,
    "rejected": REJECTED,
    "stopped": UNREGISTERED,
    # Transitional. Alerting on it would page someone during a normal reload.
    "stopping": UNKNOWN,
}

# chan_sip.c: regstate2str
_SIP_STATUS = {
    "registered": REGISTERED,
    "unregistered": UNREGISTERED,
    "rejected": REJECTED,
    "failed": REJECTED,
    "no authentication": REJECTED,
    "timeout": UNREACHABLE,
    # Mid-handshake, not a fault.
    "request sent": UNKNOWN,
    "auth. sent": UNKNOWN,
}

# PJSIP device states that mean the endpoint is not usable.
_ENDPOINT_OFFLINE = {"unavailable", "invalid", "unknown"}


def _peer_state(status: str) -> str:
    """chan_sip qualify status -> a state.

    "Unmonitored" means qualify is switched off for this peer, so Asterisk has
    not checked and neither can we. That is UNKNOWN, never "fine" -- a green
    trunk that nobody actually probed is exactly the reassurance a monitoring
    tool must not invent.

    "Lagged" means the peer answered, slowly. Still usable, so still up; the
    original string rides along in `detail` where an operator can see it.
    """
    lowered = status.strip().lower()
    if lowered.startswith("ok") or lowered.startswith("lagged"):
        return REACHABLE
    if lowered.startswith("unreachable"):
        return UNREACHABLE
    return UNKNOWN


def _normalise(raw: str, table: Dict[str, str]) -> str:
    return table.get(raw.strip().lower(), UNKNOWN)


def pjsip_registrations(client: AMIClient) -> List[Dict[str, Optional[str]]]:
    """Outbound PJSIP registrations. Empty list if the driver is not loaded."""
    try:
        items = client.action_list(
            "PJSIPShowRegistrationsOutbound",
            ("OutboundRegistrationDetailComplete", "PJSIPShowRegistrationsOutboundComplete"),
        )
    except AMIError:
        # chan_pjsip absent, or the action is unknown on this version. Not an
        # error -- plenty of PBXs run chan_sip only.
        return []

    trunks: List[Dict[str, Optional[str]]] = []
    for item in items:
        if item.get("event", "").lower() != "outboundregistrationdetail":
            continue
        name = item.get("objectname") or item.get("clienturi")
        if not name:
            continue
        status = item.get("status", "")
        trunks.append(
            {
                "name": name,
                "technology": "pjsip",
                "kind": KIND_REGISTRATION,
                "state": _normalise(status, _PJSIP_STATUS),
                "detail": status or None,
            }
        )
    return trunks


def sip_registrations(client: AMIClient) -> List[Dict[str, Optional[str]]]:
    """chan_sip outbound registrations. Empty list if the driver is not loaded."""
    try:
        items = client.action_list("SIPshowregistry", ("RegistrationsComplete",))
    except AMIError:
        return []

    trunks: List[Dict[str, Optional[str]]] = []
    for item in items:
        if item.get("event", "").lower() != "registryentry":
            continue
        host = item.get("host", "")
        username = item.get("username", "")
        if not host:
            continue
        # chan_sip has no object name for a registration, so the identity is the
        # pair that uniquely describes it.
        name = "{}@{}".format(username, host) if username else host
        state = item.get("state", "")
        trunks.append(
            {
                "name": name,
                "technology": "sip",
                "kind": KIND_REGISTRATION,
                "state": _normalise(state, _SIP_STATUS),
                "detail": state or None,
            }
        )
    return trunks


def sip_peers(client: AMIClient) -> List[Dict[str, Optional[str]]]:
    """chan_sip peers with a static host -- the trunks that never register.

    `Dynamic: yes` means the peer registers to us, which is an extension, not a
    trunk. Everything else is configured against a fixed host: a provider, or a
    link to another PBX.

    The classification is a heuristic and cannot be perfect. A desk phone pinned
    to a static IP looks the same from here. That errs towards reporting, which
    is the right direction -- an unreachable static device is worth knowing
    about whatever it is, and its name tells the operator which.
    """
    try:
        peers = client.action_list("SIPpeers", ("PeerlistComplete",))
    except AMIError:
        return []

    trunks: List[Dict[str, Optional[str]]] = []
    for peer in peers:
        if peer.get("event", "").lower() != "peerentry":
            continue
        if peer.get("dynamic", "").strip().lower() == "yes":
            continue
        name = peer.get("objectname")
        if not name:
            continue
        status = peer.get("status", "")
        trunks.append(
            {
                "name": name,
                "technology": "sip",
                "kind": KIND_PEER,
                "state": _peer_state(status),
                "detail": status or None,
            }
        )
    return trunks


def _accepts_registrations(item: Dict[str, str]) -> bool:
    """Whether this AOR has room for a REGISTER.

    Absent or unparsable means no: an Asterisk that does not report the field
    tells us nothing, and the older reading -- a configured contact -- is the
    likelier meaning for an AOR we cannot ask about.
    """
    raw = (item.get("maxcontacts") or "").strip()
    try:
        return int(raw) > 0
    except ValueError:
        return False


def pjsip_static_aors(client: AMIClient) -> List[Dict[str, Optional[str]]]:
    """PJSIP AORs carrying a *configured* contact -- the PJSIP equivalent.

    An AOR whose contact was written in configuration is a destination we were
    told about. An AOR whose contact arrived by REGISTER is a phone.

    The `Contacts` field cannot tell them apart: it lists whatever contacts the
    AOR has right now, from either origin. Reading it alone meant that the
    moment a desk phone registered it acquired a contact, turned into a "trunk",
    and vanished from the operator panel -- while a phone going offline could
    open a trunk-down alert about an extension.

    `MaxContacts` is the discriminator, and it is on the AorList event itself.
    An AOR that accepts registrations is somewhere phones sign in; one with a
    contact written in configuration and no room for registrations is a
    destination. From a real PBX:

        666          max_contacts 10   contact sip:666@46.46.129.105:61714
        novofon_aor  max_contacts  0   contact sip:0054519@sip.novofon.ru:5060

    `RegExpire` on ContactStatusDetail is kept as a second signal, but it cannot
    carry the rule alone: it only exists when `qualify` is configured, and on
    the PBX above `qualify_frequency` is 0 for every AOR, so no such event is
    emitted at all. A rule keyed on a field that is never sent is no rule.

    Reachability comes from ContactStatusDetail when qualify is on. Where it is
    not available the state is UNKNOWN rather than assumed good.
    """
    try:
        aors = client.action_list("PJSIPShowAors", ("AorListComplete",))
    except AMIError:
        return []

    statuses: Dict[str, str] = {}
    registered: Set[str] = set()
    for item in aors:
        if item.get("event", "").lower() != "contactstatusdetail":
            continue
        aor = item.get("aor") or ""
        if not aor:
            continue
        statuses[aor] = item.get("status", "")
        # Absent on versions that do not report it; blank and "0" both mean a
        # contact that never expires, which is one written in configuration.
        expiry = (item.get("regexpire") or "").strip()
        if expiry and expiry not in ("0", "-1"):
            registered.add(aor)

    trunks: List[Dict[str, Optional[str]]] = []
    for item in aors:
        if item.get("event", "").lower() != "aorlist":
            continue
        name = item.get("objectname")
        contacts = (item.get("contacts") or "").strip()
        if not name or not contacts:
            continue
        if name in registered or _accepts_registrations(item):
            # It registered to us, or it is configured to let something. Either
            # way that is a phone, and the extension collector reports it.
            continue
        raw = statuses.get(name, "")
        lowered = raw.strip().lower()
        if lowered in ("reachable", "created", "updated"):
            state = REACHABLE
        elif lowered in ("unreachable", "removed"):
            state = UNREACHABLE
        else:
            state = UNKNOWN
        trunks.append(
            {
                "name": name,
                "technology": "pjsip",
                "kind": KIND_PEER,
                "state": state,
                "detail": raw or "no qualify result",
            }
        )
    return trunks


def endpoint_counts(client: AMIClient) -> Dict[str, Optional[int]]:
    """Online and total SIP endpoints, for the asterisk_metrics columns."""
    online = 0
    total = 0
    seen_any = False

    try:
        items = client.action_list(
            "PJSIPShowEndpoints", ("EndpointListComplete", "PJSIPShowEndpointsComplete")
        )
    except AMIError:
        items = []
    for item in items:
        if item.get("event", "").lower() != "endpointlist":
            continue
        seen_any = True
        total += 1
        if item.get("devicestate", "").strip().lower() not in _ENDPOINT_OFFLINE:
            online += 1

    try:
        peers = client.action_list("SIPpeers", ("PeerlistComplete",))
    except AMIError:
        peers = []
    for peer in peers:
        if peer.get("event", "").lower() != "peerentry":
            continue
        seen_any = True
        total += 1
        status = peer.get("status", "").strip().lower()
        # chan_sip reports "OK (12 ms)" for a reachable peer, and "Unmonitored"
        # when qualify is off -- which is not the same as unreachable.
        if status.startswith("ok") or status.startswith("unmonitored"):
            online += 1

    if not seen_any:
        return {"sip_endpoints_online": None, "sip_endpoints_total": None}
    return {"sip_endpoints_online": online, "sip_endpoints_total": total}
