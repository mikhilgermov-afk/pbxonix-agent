"""One AMI session, everything it can tell us.

Trunks, endpoint counts and queues are read in a single connection rather than
three. Each login writes a line to Asterisk's log and costs a round trip, and
there is no reason to pay that three times a minute for data that is read
together anyway.
"""

from typing import Dict, List, Optional

from pbxonix_agent.collectors import extensions as extension_collector
from pbxonix_agent.collectors import queues as queue_collector
from pbxonix_agent.collectors import trunks as trunk_collector
from pbxonix_agent.collectors.ami import AMIClient, AMIError

# Channel technologies Asterisk uses for its own plumbing rather than for a
# party to a call. A `Local` leg is dialplan routing; `UnicastRTP`, `Snoop` and
# `Recorder` are media taps an ARI application creates; `Bridge` and `Announcer`
# are bridge internals.
#
# Written as an exclude list on purpose. An include list would silently drop a
# channel driver nobody thought of -- undercounting calls, which makes a busy
# system look idle. An unknown technology counting as real errs the other way,
# and that error is visible.
_PSEUDO_TECHNOLOGIES = frozenset(
    {
        "local",
        "unicastrtp",
        "multicastrtp",
        "bridge",
        "announcer",
        "snoop",
        "recorder",
        "message",
        "stasis",
    }
)


def _is_endpoint(channel: str) -> bool:
    """True for a leg that has a party on the other end of it."""
    return channel.split("/")[0].strip().lower() not in _PSEUDO_TECHNOLOGIES


def core_counts(client: AMIClient) -> Dict[str, Optional[int]]:
    """How many calls are up, and how many endpoint legs they use.

    Not `CoreStatus.CoreCurrentCalls`: that field is a channel counter wearing a
    misleading name -- Asterisk's own source calls the variable behind it "number
    of active channels" -- so reporting it as calls showed the same number twice.

    A call is a `linkedid` group with at least one endpoint channel in it. That
    is what separates a conversation from the machinery around it: on the PBX
    this was written for, 27 channels were one real call plus 21 media forks
    belonging to a recording system, each fork holding its own linkedid.

    The CLI route is not used for this. It needs write access to
    /var/run/asterisk/asterisk.ctl, and Asterisk creates that socket 0755 unless
    `astctlpermissions` is set -- which a stock Issabel does not -- so only the
    asterisk user can use it, group membership does not help, and changing it
    means restarting Asterisk on a live phone system.

    AMI is a TCP connection to 127.0.0.1 and has no such problem. The manager
    user already holds `system` and `reporting`, which is exactly what these
    two actions need, so this costs no new permission.

    Either value is None when Asterisk did not answer for it. None means "could
    not tell" here as everywhere else -- never zero.
    """
    try:
        items = client.action_list("CoreShowChannels", ("CoreShowChannelsComplete",))
    except AMIError:
        # Could not ask. None, never zero: a PBX we failed to query is not a
        # quiet one, and charting it as idle would hide a busy hour.
        return {"active_calls": None, "active_channels": None}

    endpoint_legs = 0
    calls = set()
    for item in items:
        if item.get("event", "").lower() != "coreshowchannel":
            continue
        channel = item.get("channel") or ""
        if not _is_endpoint(channel):
            continue
        endpoint_legs += 1
        # linkedid ties every leg of one call together; uniqueid is the fallback
        # for a lone channel on an Asterisk too old to set it.
        calls.add(item.get("linkedid") or item.get("uniqueid") or channel)

    return {"active_calls": len(calls), "active_channels": endpoint_legs}


def collect(
    host: str,
    port: int,
    username: str,
    secret: str,
    timeout: float = 10.0,
    strategies: Optional[Dict[str, str]] = None,
) -> Dict[str, object]:
    """Open a short-lived AMI session and read everything in one pass.

    Raises AMIError on any failure. The caller must then send *nothing* rather
    than send empty lists -- the cloud reads an empty trunk list as "this PBX
    has no trunks configured" and would resolve every open alert.
    """
    trunks: List[Dict[str, Optional[str]]] = []
    with AMIClient(host=host, port=port, username=username, secret=secret, timeout=timeout) as ami:
        trunks.extend(trunk_collector.pjsip_registrations(ami))
        trunks.extend(trunk_collector.sip_registrations(ami))
        # Trunks that never register appear in neither list above.
        trunks.extend(trunk_collector.sip_peers(ami))
        trunks.extend(trunk_collector.pjsip_static_aors(ami))
        endpoints = trunk_collector.endpoint_counts(ami)
        # After the trunks, and using their names: PJSIP does not label an
        # endpoint as extension or trunk, and the classification has already
        # been made above. Repeating it here would risk the two disagreeing.
        extensions = extension_collector.collect(
            ami, {str(t["name"]) for t in trunks if t.get("name")}
        )
        queues = queue_collector.collect_queues(ami, strategies)
        core = core_counts(ami)

    # The same trunk under both drivers would otherwise produce two rows that
    # fight over one unique constraint. Registrations are added first, so a name
    # collision keeps the registration -- it carries the more specific state.
    deduped: Dict[str, Dict[str, Optional[str]]] = {}
    for trunk in trunks:
        key = str(trunk["name"])
        if key not in deduped:
            deduped[key] = trunk

    return {
        "trunks": list(deduped.values()),
        "endpoints": endpoints,
        "extensions": extensions,
        "queues": queues,
        "core": core,
    }
