"""Queue collection over AMI.

QueueSummary returns one event per queue carrying the four numbers the
dashboard uses. QueueStatus, which enumerates every member and every waiting
caller to arrive at the same figures, is kept only for `strategy` -- a word that
changes when somebody edits a config file. The payloads below are the real wire
shapes of both.
"""

import socket

import pytest
from support import ENDPOINT_SCRIPT, PJSIP_SCRIPT, FakeSocket, connect_fake

from pbxonix_agent.collectors import queues as queue_collector
from pbxonix_agent.collectors import snapshot as ami_snapshot
from pbxonix_agent.collectors.ami import AMIClient, AMIError

QUEUE_SCRIPT = {
    "queuesummary": [
        {"Event": "QueueSummary", "Queue": "support", "LoggedIn": "3",
         "Available": "1", "Callers": "3", "HoldTime": "12", "TalkTime": "90",
         "LongestHoldTime": "117"},
        {"Event": "QueueSummary", "Queue": "sales", "LoggedIn": "1",
         "Available": "1", "Callers": "0", "HoldTime": "0", "TalkTime": "0",
         "LongestHoldTime": "0"},
        {"Event": "QueueSummaryComplete", "EventList": "Complete"},
    ],
    "queuestatus": [
        {"Event": "QueueParams", "Queue": "support", "Strategy": "ringall", "Calls": "3",
         "Holdtime": "12", "Completed": "40", "Abandoned": "2"},
        # Status 1 == not in use, and not paused: genuinely able to take a call.
        {"Event": "QueueMember", "Queue": "support", "Name": "SIP/101",
         "Status": "1", "Paused": "0"},
        # Status 2 == in use: logged in, but busy.
        {"Event": "QueueMember", "Queue": "support", "Name": "SIP/102",
         "Status": "2", "Paused": "0"},
        # Available device state, but on a break.
        {"Event": "QueueMember", "Queue": "support", "Name": "SIP/103",
         "Status": "1", "Paused": "1"},
        {"Event": "QueueEntry", "Queue": "support", "Position": "1",
         "Channel": "PJSIP/inbound-0001", "CallerIDNum": "+15551234567",
         "CallerIDName": "A Real Person", "Wait": "42"},
        {"Event": "QueueEntry", "Queue": "support", "Position": "2",
         "Channel": "PJSIP/inbound-0002", "CallerIDNum": "+15559876543",
         "CallerIDName": "Someone Else", "Wait": "117"},

        {"Event": "QueueParams", "Queue": "sales", "Strategy": "leastrecent", "Calls": "0"},
        {"Event": "QueueMember", "Queue": "sales", "Name": "SIP/201",
         "Status": "1", "Paused": "0"},

        {"Event": "QueueStatusComplete", "EventList": "Complete"},
    ]
}


def collect(script=QUEUE_SCRIPT, monkeypatch=None, strategies=None):
    connect_fake(monkeypatch, FakeSocket(script))
    with AMIClient(username="u", secret="p") as client:
        return {
            q["name"]: q
            for q in queue_collector.collect_queues(
                client, {} if strategies is None else strategies
            )
        }


def test_queue_figures_are_read(monkeypatch):
    queues = collect(monkeypatch=monkeypatch)
    assert set(queues) == {"support", "sales"}
    assert queues["support"]["calls_waiting"] == 3
    assert queues["sales"]["calls_waiting"] == 0


def test_the_strategy_is_fetched_once_and_then_cached(monkeypatch):
    """QueueStatus walks every member of every queue to tell us one word.

    On a PBX with 31 queues that is upwards of 250 events per pass, with
    app_queue locking each queue as it goes. It changes when somebody edits a
    config file, so it is worth asking for once and not again.
    """
    cache = {}
    queues = collect(monkeypatch=monkeypatch, strategies=cache)
    assert queues["support"]["strategy"] == "ringall"
    assert cache == {"support": "ringall", "sales": "leastrecent"}

    # Second pass with everything known: the heavy action must not be issued.
    sock = connect_fake(monkeypatch, FakeSocket(QUEUE_SCRIPT))
    with AMIClient(username="u", secret="p") as client:
        queue_collector.collect_queues(client, cache)
    assert "queuestatus" not in sock.actions
    assert "queuesummary" in sock.actions


def test_agents_logged_in_counts_every_member(monkeypatch):
    queues = collect(monkeypatch=monkeypatch)
    assert queues["support"]["agents_logged_in"] == 3


def test_agents_available_excludes_busy_and_paused(monkeypatch):
    """app_queue applies the same rule the collector used to apply itself."""
    queues = collect(monkeypatch=monkeypatch)
    assert queues["support"]["agents_available"] == 1


def test_longest_wait_is_the_maximum(monkeypatch):
    queues = collect(monkeypatch=monkeypatch)
    assert queues["support"]["longest_wait_seconds"] == 117


def test_a_queue_with_no_callers_has_no_wait(monkeypatch):
    queues = collect(monkeypatch=monkeypatch)
    assert queues["sales"]["longest_wait_seconds"] is None


def test_caller_identity_is_never_extracted(monkeypatch):
    """QueueEntry carries the phone number of someone currently on hold.

    Only Wait is read from those events. This asserts on the whole collected
    structure rather than on one field, so a future edit that starts copying
    CallerIDNum through fails here.
    """
    queues = collect(monkeypatch=monkeypatch)
    blob = repr(queues)
    assert "+15551234567" not in blob
    assert "+15559876543" not in blob
    assert "A Real Person" not in blob
    assert "PJSIP/inbound-0001" not in blob

    for queue in queues.values():
        assert set(queue) == {
            "name",
            "strategy",
            "calls_waiting",
            "agents_logged_in",
            "agents_available",
            "longest_wait_seconds",
        }


def test_missing_app_queue_is_not_an_error(monkeypatch):
    connect_fake(monkeypatch, FakeSocket({}))
    with AMIClient(username="u", secret="p") as client:
        assert queue_collector.collect_queues(client) == []


def test_unparsable_numbers_become_none(monkeypatch):
    script = {
        "queuesummary": [
            {"Event": "QueueSummary", "Queue": "odd", "Callers": "n/a",
             "LongestHoldTime": "soon"},
            {"Event": "QueueSummaryComplete"},
        ],
        "queuestatus": [{"Event": "QueueStatusComplete"}],
    }
    queues = collect(script, monkeypatch=monkeypatch)
    assert queues["odd"]["calls_waiting"] is None
    assert queues["odd"]["longest_wait_seconds"] is None


def test_events_without_a_queue_name_are_skipped(monkeypatch):
    script = {
        "queuesummary": [
            {"Event": "QueueSummary", "Callers": "5"},
            {"Event": "QueueSummary", "Queue": "real", "Callers": "1"},
            {"Event": "QueueSummaryComplete"},
        ],
        "queuestatus": [{"Event": "QueueStatusComplete"}],
    }
    queues = collect(script, monkeypatch=monkeypatch)
    assert set(queues) == {"real"}


# ------------------------------------------------------------- snapshot --
def test_snapshot_reads_everything_in_one_session(monkeypatch):
    script = dict(PJSIP_SCRIPT)
    script.update(ENDPOINT_SCRIPT)
    script.update(QUEUE_SCRIPT)
    sock = connect_fake(monkeypatch, FakeSocket(script))

    result = ami_snapshot.collect("127.0.0.1", 5038, "u", "p")

    assert {t["name"] for t in result["trunks"]} >= {"provider1", "provider2"}
    assert result["endpoints"]["sip_endpoints_total"] == 6
    assert {q["name"] for q in result["queues"]} == {"support", "sales"}
    # One login, not three: each costs a round trip and a line in Asterisk's log.
    assert sock.actions.count("login") == 1


def test_snapshot_propagates_connection_failure(monkeypatch):
    def refuse(*_a, **_k):
        raise OSError("connection refused")

    monkeypatch.setattr(socket, "create_connection", refuse)
    with pytest.raises(AMIError, match="cannot reach AMI"):
        ami_snapshot.collect("127.0.0.1", 5038, "u", "p")


def test_an_asterisk_without_the_light_action_still_reports_queues(monkeypatch):
    """Returning nothing would read as "this PBX has no queues"."""
    script = {
        "queuesummary": [{"Response": "Error", "Message": "Invalid/unknown command"}],
        "queuestatus": QUEUE_SCRIPT["queuestatus"],
    }
    queues = collect(script, monkeypatch=monkeypatch)
    assert set(queues) == {"support", "sales"}
    assert queues["support"]["agents_logged_in"] == 3
    assert queues["support"]["agents_available"] == 1
    assert queues["support"]["longest_wait_seconds"] == 117


def test_the_fallback_reads_no_caller_identity_either(monkeypatch):
    script = {
        "queuesummary": [{"Response": "Error", "Message": "Invalid/unknown command"}],
        "queuestatus": QUEUE_SCRIPT["queuestatus"],
    }
    blob = repr(collect(script, monkeypatch=monkeypatch))
    assert "+15551234567" not in blob
    assert "A Real Person" not in blob
