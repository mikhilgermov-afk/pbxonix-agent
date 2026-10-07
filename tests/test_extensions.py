"""Extension collection over AMI.

The payloads below are the real wire shapes. Two sources answer two different
questions -- the peer list says who exists and whether their handset is
reachable, DeviceStateList says what they are doing -- and most of what is worth
testing here is how the two are combined when they disagree.
"""

from support import FakeSocket, connect_fake

from pbxonix_agent.collectors import extensions as ext
from pbxonix_agent.collectors.ami import AMIClient

SCRIPT = {
    "sippeers": [
        # Dynamic: registers to us. A phone.
        {"Event": "PeerEntry", "ObjectName": "101", "Dynamic": "yes", "Status": "OK (12 ms)"},
        {"Event": "PeerEntry", "ObjectName": "102", "Dynamic": "yes", "Status": "UNREACHABLE"},
        # Not qualified at all: registered, but nobody is measuring it.
        {"Event": "PeerEntry", "ObjectName": "103", "Dynamic": "yes", "Status": "Unmonitored"},
        # Static host: a provider, and the trunk collector's business, not ours.
        {"Event": "PeerEntry", "ObjectName": "sipnet", "Dynamic": "no", "Status": "OK (30 ms)"},
        {"Event": "PeerlistComplete", "ListItems": "4"},
    ],
    "pjsipshowendpoints": [
        {"Event": "EndpointList", "ObjectName": "201", "Aor": "201",
         "DeviceState": "Not in use"},
        {"Event": "EndpointList", "ObjectName": "202", "Aor": "202",
         "DeviceState": "In use"},
        {"Event": "EndpointList", "ObjectName": "trunk-a", "Aor": "trunk-a",
         "DeviceState": "Not in use"},
        # Endpoint and AOR are separate objects with separate names. Only the
        # AOR is classifiable, so only the AOR is in the trunk list -- taken
        # from a real PBX, where this endpoint showed up as a person.
        {"Event": "EndpointList", "ObjectName": "elevenlabs-endpoint",
         "Aor": "elevenlabs-aor", "DeviceState": "Not in use"},
        {"Event": "EndpointListComplete", "ListItems": "4"},
    ],
    "devicestatelist": [
        {"Event": "DeviceStateChange", "Device": "SIP/101", "State": "INUSE"},
        {"Event": "DeviceStateChange", "Device": "SIP/102", "State": "NOT_INUSE"},
        {"Event": "DeviceStateChange", "Device": "SIP/103", "State": "RINGING"},
        # Not a person: a queue's availability hint, and it must not become a card.
        {"Event": "DeviceStateChange", "Device": "Queue:support_avail", "State": "NOT_INUSE"},
        {"Event": "DeviceStateListComplete", "ListItems": "4"},
    ],
}


def collect(script=SCRIPT, trunks=frozenset({"trunk-a", "elevenlabs-aor"}), monkeypatch=None):
    connect_fake(monkeypatch, FakeSocket(script))
    with AMIClient(username="u", secret="p") as client:
        return {row["extension"]: row for row in ext.collect(client, set(trunks))}


# ------------------------------------------------------------------ roster --
def test_only_phones_that_register_to_us_are_extensions(monkeypatch):
    """`Dynamic: no` is a provider. The trunk collector already reports it."""
    rows = collect(monkeypatch=monkeypatch)
    assert "sipnet" not in rows
    assert {"101", "102", "103"} <= set(rows)


def test_a_pjsip_endpoint_known_to_be_a_trunk_is_excluded(monkeypatch):
    """PJSIP does not label endpoints, so the classification is passed in.

    Repeating the trunk collector's reasoning here would risk the two
    disagreeing, and an endpoint would show up as both.
    """
    rows = collect(monkeypatch=monkeypatch)
    assert "trunk-a" not in rows
    assert rows["201"]["tech"] == "PJSIP"


def test_an_endpoint_is_followed_to_its_aor(monkeypatch):
    """`elevenlabs-endpoint` has the AOR `elevenlabs-aor`, which is a trunk.

    Matching the endpoint's own name against the trunk list misses this
    entirely: the two objects are named differently, and the endpoint arrived
    on the panel as an operator.
    """
    rows = collect(monkeypatch=monkeypatch)
    assert "elevenlabs-endpoint" not in rows


def test_the_older_spelling_of_the_field_works_too(monkeypatch):
    """Asterisk has called it both `Aor` and `Aors`."""
    script = dict(SCRIPT)
    script["pjsipshowendpoints"] = [
        {"Event": "EndpointList", "ObjectName": "gw", "Aors": "gw-aor",
         "DeviceState": "Not in use"},
        {"Event": "EndpointListComplete", "ListItems": "1"},
    ]
    rows = collect(script=script, trunks=frozenset({"gw-aor"}), monkeypatch=monkeypatch)
    assert "gw" not in rows


def test_a_device_state_alone_does_not_invent_a_person(monkeypatch):
    """Asterisk tracks states for queues and park lots too."""
    rows = collect(monkeypatch=monkeypatch)
    assert not any(name.startswith("Queue:") for name in rows)


# ------------------------------------------------------------------- state --
def test_device_state_wins_over_reachability(monkeypatch):
    """The peer list says 101 is reachable; only DeviceStateList knows it is busy."""
    rows = collect(monkeypatch=monkeypatch)
    assert rows["101"]["state"] == ext.BUSY
    # Asterisk's own wording survives for the tooltip.
    assert rows["101"]["detail"] == "OK (12 ms)"


def test_an_unreachable_phone_stays_unreachable(monkeypatch):
    """Asterisk reports NOT_INUSE for a handset that is switched off.

    Letting that win would put a green card on the board and somebody would
    ring a phone that nobody can answer.
    """
    rows = collect(monkeypatch=monkeypatch)
    assert rows["102"]["state"] == ext.OFFLINE


def test_ringing_is_its_own_state(monkeypatch):
    rows = collect(monkeypatch=monkeypatch)
    assert rows["103"]["state"] == ext.RINGING


def test_without_device_state_reachability_is_all_there_is(monkeypatch):
    """DeviceStateList does not exist before Asterisk 12.

    An Issabel 4 on Asterisk 11 gets an error, and the roster must still be
    reported -- with what the peer list knows, and nothing invented.
    """
    script = dict(SCRIPT)
    script["devicestatelist"] = [{"Response": "Error", "Message": "Invalid/unknown command"}]
    rows = collect(script=script, monkeypatch=monkeypatch)
    assert rows["101"]["state"] == ext.FREE      # reachable, nothing better known
    assert rows["102"]["state"] == ext.OFFLINE
    # Unmonitored is neither reachable nor not: saying "free" would be a guess.
    assert rows["103"]["state"] == ext.UNKNOWN


def test_no_channel_driver_at_all_reports_nobody(monkeypatch):
    """Both drivers absent is not the same as everybody being idle."""
    script = {
        "sippeers": [{"Response": "Error", "Message": "no such command"}],
        "pjsipshowendpoints": [{"Response": "Error", "Message": "no such command"}],
        "devicestatelist": [{"Event": "DeviceStateListComplete", "ListItems": "0"}],
    }
    assert collect(script=script, monkeypatch=monkeypatch) == {}


# ----------------------------------------------------------------- privacy --
def test_nothing_identifying_is_collected(monkeypatch):
    """A panel says who is free, never who they are talking to."""
    rows = collect(monkeypatch=monkeypatch)
    for row in rows.values():
        assert set(row) == {"extension", "tech", "state", "detail"}


# ------------------------------------------- registered phone versus trunk --
# A PJSIP AOR's `Contacts` field lists whatever contacts it has right now,
# whether written in pjsip.conf or created by a REGISTER. Reading it alone meant
# a desk phone turned into a "trunk" the moment it registered: it vanished from
# the panel, and going offline could open a trunk-down alert about an extension.
# `RegExpire` separates them -- a registered contact lapses, a configured one
# never does and reports 0.
# Field values taken from a real PBX. Both AORs there have qualify_frequency 0,
# so no ContactStatusDetail is emitted for either -- which is exactly why
# MaxContacts has to carry the rule.
AOR_SCRIPT = {
    "pjsipshowaors": [
        # A provider: contact written in configuration, no room to register.
        {"Event": "AorList", "ObjectName": "novofon_aor", "MaxContacts": "0",
         "Contacts": "novofon_aor/sip:0054519@sip.novofon.ru:5060"},
        # A desk phone: ten slots for registrations, one of them taken.
        {"Event": "AorList", "ObjectName": "666", "MaxContacts": "10",
         "Contacts": "666/sip:666@46.46.129.105:61714"},
        # A phone that has never registered: slots, but no contact yet.
        {"Event": "AorList", "ObjectName": "101", "MaxContacts": "1", "Contacts": ""},
        {"Event": "AorListComplete", "EventList": "Complete"},
    ]
}

# The same estate with qualify switched on, where the second signal is available.
QUALIFIED_AOR_SCRIPT = {
    "pjsipshowaors": [
        {"Event": "AorList", "ObjectName": "trunk-x", "Contacts": "trunk-x/sip:host"},
        {"Event": "ContactStatusDetail", "AOR": "trunk-x", "Status": "Reachable",
         "RegExpire": "0", "URI": "sip:host"},
        {"Event": "AorList", "ObjectName": "777", "Contacts": "777/sip:777@10.0.0.9"},
        {"Event": "ContactStatusDetail", "AOR": "777", "Status": "Reachable",
         "RegExpire": "1788251234", "URI": "sip:777@10.0.0.9"},
        {"Event": "AorListComplete", "EventList": "Complete"},
    ]
}


def aor_names(monkeypatch, script=AOR_SCRIPT):
    from pbxonix_agent.collectors import trunks as trunk_collector

    connect_fake(monkeypatch, FakeSocket(script))
    with AMIClient(username="u", secret="p") as client:
        return {row["name"] for row in trunk_collector.pjsip_static_aors(client)}


def test_a_registered_phone_is_not_a_trunk(monkeypatch):
    assert "666" not in aor_names(monkeypatch)


def test_a_configured_contact_still_is_a_trunk(monkeypatch):
    assert "novofon_aor" in aor_names(monkeypatch)


def test_an_aor_with_no_contact_is_neither(monkeypatch):
    """Waiting for a registration. The extension collector reports it."""
    assert "101" not in aor_names(monkeypatch)


def test_a_registered_contact_also_marks_a_phone(monkeypatch):
    """Where qualify is on, RegExpire says the same thing a second way."""
    names = aor_names(monkeypatch, QUALIFIED_AOR_SCRIPT)
    assert "777" not in names
    assert "trunk-x" in names


def test_with_neither_signal_the_older_reading_stands(monkeypatch):
    """Nothing to go on, and a configured contact is the likelier meaning.

    Changing the answer for an Asterisk that reports neither field would trade
    a known misclassification for an unknown one.
    """
    script = {
        "pjsipshowaors": [
            {"Event": "AorList", "ObjectName": "666", "Contacts": "666/sip:666@10.0.0.9"},
            {"Event": "AorListComplete", "EventList": "Complete"},
        ]
    }
    assert aor_names(monkeypatch, script) == {"666"}


def test_an_unparsable_max_contacts_is_not_read_as_a_phone(monkeypatch):
    script = {
        "pjsipshowaors": [
            {"Event": "AorList", "ObjectName": "odd", "MaxContacts": "many",
             "Contacts": "odd/sip:host"},
            {"Event": "AorListComplete", "EventList": "Complete"},
        ]
    }
    assert aor_names(monkeypatch, script) == {"odd"}
