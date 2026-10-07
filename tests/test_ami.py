"""AMI protocol and trunk normalisation tests.

Everything runs against a scripted fake socket. No Asterisk, no network -- the
payloads below are the real wire format, captured from the shapes res_pjsip and
chan_sip actually emit.
"""

import pytest
from support import ENDPOINT_SCRIPT, PJSIP_SCRIPT, FakeSocket, connect_fake

from pbxonix_agent.collectors import trunks as trunk_collector
from pbxonix_agent.collectors.ami import AMIAuthError, AMIClient, AMIError


# ------------------------------------------------------------------ protocol --
def test_connect_reads_the_banner(monkeypatch):
    connect_fake(monkeypatch, FakeSocket({}))
    client = AMIClient()
    client.connect()
    assert "Asterisk Call Manager" in client.banner


def test_unexpected_banner_is_rejected(monkeypatch):
    connect_fake(monkeypatch, FakeSocket({}, banner=b"220 ESMTP postfix\r\n"))
    with pytest.raises(AMIError, match="banner"):
        AMIClient().connect()


def test_login_success(monkeypatch):
    sock = connect_fake(monkeypatch, FakeSocket({}))
    with AMIClient(username="pbxonix", secret="s3cret"):
        pass
    assert sock.actions[0] == "login"
    # Logoff is sent on the way out so Asterisk does not log an abrupt close.
    assert "logoff" in sock.actions
    assert sock.closed


def test_login_failure_raises_auth_error(monkeypatch):
    connect_fake(monkeypatch, FakeSocket({}, login_ok=False))
    with pytest.raises(AMIAuthError):
        with AMIClient(username="pbxonix", secret="wrong"):
            pass


def test_auth_error_does_not_echo_the_secret(monkeypatch):
    connect_fake(monkeypatch, FakeSocket({}, login_ok=False))
    try:
        with AMIClient(username="pbxonix", secret="hunter2-should-not-appear"):
            pass
    except AMIAuthError as exc:
        assert "hunter2" not in str(exc)
    else:
        pytest.fail("expected AMIAuthError")


def test_login_sends_events_off(monkeypatch):
    sock = connect_fake(monkeypatch, FakeSocket({}))
    with AMIClient(username="u", secret="p"):
        pass
    # Without Events: off the session receives every channel event on a busy
    # PBX -- wasteful, and it makes response parsing non-deterministic.
    assert b"Events: off" in bytes(sock.raw_sent)


def test_stray_events_are_ignored(monkeypatch):
    """Correlation is by ActionID, not by arrival order."""
    script = {
        "sipshowregistry": [
            {"Event": "RegistryEntry", "Host": "sip.example.com", "Username": "u1",
             "State": "Registered"},
            {"Event": "RegistrationsComplete", "EventList": "Complete", "ListItems": "1"},
        ]
    }
    connect_fake(monkeypatch, FakeSocket(script, noise=True))
    with AMIClient(username="u", secret="p") as client:
        items = client.action_list("SIPshowregistry", ("RegistrationsComplete",))
    assert len(items) == 1
    assert items[0]["host"] == "sip.example.com"


def test_unknown_action_raises(monkeypatch):
    connect_fake(monkeypatch, FakeSocket({}))
    with AMIClient(username="u", secret="p") as client:
        with pytest.raises(AMIError, match="Invalid/unknown command"):
            client.action_list("PJSIPShowRegistrationsOutbound", ("Whatever",))


def test_closed_connection_raises(monkeypatch):
    sock = FakeSocket({}, banner=b"")
    connect_fake(monkeypatch, sock)
    with pytest.raises(AMIError, match="closed"):
        AMIClient().connect()


def test_values_containing_colons_survive_parsing(monkeypatch):
    script = {
        "pjsipshowregistrationsoutbound": [
            {"Event": "OutboundRegistrationDetail", "ObjectName": "prov1",
             "ClientUri": "sip:user@sip.example.com:5060", "Status": "Registered"},
            {"Event": "OutboundRegistrationDetailComplete"},
        ]
    }
    connect_fake(monkeypatch, FakeSocket(script))
    with AMIClient(username="u", secret="p") as client:
        items = client.action_list(
            "PJSIPShowRegistrationsOutbound", ("OutboundRegistrationDetailComplete",)
        )
    assert items[0]["clienturi"] == "sip:user@sip.example.com:5060"


# ------------------------------------------------------------------- pjsip --



def test_pjsip_status_mapping(monkeypatch):
    connect_fake(monkeypatch, FakeSocket(PJSIP_SCRIPT))
    with AMIClient(username="u", secret="p") as client:
        trunks = trunk_collector.pjsip_registrations(client)

    by_name = {t["name"]: t for t in trunks}
    assert by_name["provider1"]["state"] == "registered"
    assert by_name["provider2"]["state"] == "rejected"
    assert by_name["provider3"]["state"] == "unregistered"
    # Transitional: alerting on a reload would page someone for nothing.
    assert by_name["provider4"]["state"] == "unknown"
    # The raw Asterisk word is what makes the alert useful.
    assert by_name["provider2"]["detail"] == "Rejected"
    assert all(t["technology"] == "pjsip" for t in trunks)


def test_missing_pjsip_driver_is_not_an_error(monkeypatch):
    connect_fake(monkeypatch, FakeSocket({}))
    with AMIClient(username="u", secret="p") as client:
        assert trunk_collector.pjsip_registrations(client) == []


# ----------------------------------------------------------------- chan_sip --
SIP_SCRIPT = {
    "sipshowregistry": [
        {"Event": "RegistryEntry", "Host": "sip.a.com", "Port": "5060",
         "Username": "acct1", "State": "Registered"},
        {"Event": "RegistryEntry", "Host": "sip.b.com", "Port": "5060",
         "Username": "acct2", "State": "Timeout"},
        {"Event": "RegistryEntry", "Host": "sip.c.com", "Port": "5060",
         "Username": "", "State": "Request Sent"},
        {"Event": "RegistryEntry", "Host": "sip.d.com", "Port": "5060",
         "Username": "acct4", "State": "No Authentication"},
        {"Event": "RegistrationsComplete", "EventList": "Complete"},
    ]
}


def test_sip_status_mapping_and_naming(monkeypatch):
    connect_fake(monkeypatch, FakeSocket(SIP_SCRIPT))
    with AMIClient(username="u", secret="p") as client:
        trunks = trunk_collector.sip_registrations(client)

    by_name = {t["name"]: t for t in trunks}
    assert by_name["acct1@sip.a.com"]["state"] == "registered"
    # A timeout is the provider not answering, which is distinct from refusing.
    assert by_name["acct2@sip.b.com"]["state"] == "unreachable"
    # No username: the host alone identifies the registration.
    assert by_name["sip.c.com"]["state"] == "unknown"
    assert by_name["acct4@sip.d.com"]["state"] == "rejected"
    assert all(t["technology"] == "sip" for t in trunks)


# ---------------------------------------------------------------- endpoints --



def test_endpoint_counts_across_both_drivers(monkeypatch):
    connect_fake(monkeypatch, FakeSocket(ENDPOINT_SCRIPT))
    with AMIClient(username="u", secret="p") as client:
        counts = trunk_collector.endpoint_counts(client)

    # pjsip: 2 of 3 usable. chan_sip: OK and Unmonitored count, UNREACHABLE does
    # not -- "unmonitored" means qualify is off, not that the peer is down.
    assert counts["sip_endpoints_total"] == 6
    assert counts["sip_endpoints_online"] == 4


def test_endpoint_counts_are_null_when_nothing_answers(monkeypatch):
    connect_fake(monkeypatch, FakeSocket({}))
    with AMIClient(username="u", secret="p") as client:
        counts = trunk_collector.endpoint_counts(client)
    # None, not zero: "we could not tell" must not render as "you have no phones".
    assert counts["sip_endpoints_online"] is None
    assert counts["sip_endpoints_total"] is None
