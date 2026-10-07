import pytest
from support import FakeSocket, block, connect_fake

from pbxonix_agent.collectors.ami import AMIClient, AMIError
from pbxonix_agent.collectors.extensions import apply_states
from pbxonix_agent.config import Config, ConfigError, load
from pbxonix_agent.credentials import Credentials
from pbxonix_agent.panel import PanelPublisher, hint_states, operator_samples, queue_samples
from pbxonix_agent.transport import ApiError

ROSTER = [
    {"extension": "101", "tech": "SIP", "state": "free", "detail": "OK"},
    {"extension": "102", "tech": "SIP", "state": "offline", "detail": "UNREACHABLE"},
    {"extension": "201", "tech": "PJSIP", "state": "free", "detail": None},
]


def publisher():
    return PanelPublisher(
        Config(),
        Credentials(
            pbx_id="p", agent_token="test-token", ami_username="panel", ami_password="test-secret"
        ),
        lambda: (ROSTER, 0.0),
    )


def test_roster_never_freezes_in_a_transient_device_state():
    first = apply_states(ROSTER, {"sip/101": "offline"})
    second = apply_states(ROSTER, {"sip/101": "free"})
    assert first[0]["state"] == "offline"
    assert second[0]["state"] == "free"
    assert ROSTER[0]["state"] == "free"


def test_samples_only_known_devices_with_current_observations():
    rows = operator_samples(
        ROSTER, {"sip/101": "RINGING", "pjsip/201": "INUSE", "custom/999": "NOT_INUSE"}, {}, 0
    )
    assert [(r["extension"], r["state"]) for r in rows] == [("101", "ringing"), ("201", "busy")]


def test_idle_does_not_make_unreachable_handset_online_without_evidence():
    assert operator_samples(ROSTER, {"sip/102": "NOT_INUSE"}, {}, 0)[0]["state"] == "offline"
    assert (
        operator_samples(ROSTER, {"sip/102": "NOT_INUSE"}, {"sip/102": (True, 2)}, 1)[0]["state"]
        == "free"
    )
    assert (
        operator_samples(ROSTER, {"sip/102": "NOT_INUSE"}, {"sip/102": (True, 1)}, 2)[0]["state"]
        == "offline"
    )


def test_busy_device_is_live_evidence_even_with_old_offline_inventory():
    assert operator_samples(ROSTER, {"sip/102": "INUSE"}, {}, 0)[0]["state"] == "busy"


def test_peer_events_are_bounded_to_roster_and_do_not_store_private_fields():
    p = publisher()
    p.on_event(
        {
            "event": "PeerStatus",
            "peer": "SIP/101",
            "peerstatus": "Unreachable",
            "address": "private",
        }
    )
    p.on_event({"event": "PeerStatus", "peer": "SIP/unknown", "peerstatus": "Registered"})
    p.on_event({"event": "Newchannel", "calleridnum": "private"})
    assert list(p.peers) == ["sip/101"]
    assert p.peers["sip/101"][0] is False
    p.on_event({"event": "PeerStatus", "peer": "SIP/101", "peerstatus": "Registered"})
    assert p.peers["sip/101"][0] is True


def test_queue_summary_uses_counts_and_no_caller_data():
    rows = queue_samples(
        [
            {
                "event": "QueueSummary",
                "queue": "500",
                "callers": "2",
                "available": "3",
                "loggedin": "6",
                "longestholdtime": "17",
                "calleridnum": "private",
            },
            {"event": "QueueSummary", "queue": "501", "callers": "0", "longestholdtime": "99"},
        ]
    )
    assert rows[0]["longest_wait_seconds"] == 17
    assert rows[0]["agents_available"] == 3
    assert rows[1]["longest_wait_seconds"] is None
    assert "private" not in str(rows)


def test_fast_collection_never_falls_back_to_expensive_actions():
    actions = []

    class Ami:
        def action_list(self, name, complete):
            actions.append(name)
            if name == "QueueSummary":
                raise AMIError("unsupported")
            return [{"event": "DeviceStateChange", "device": "SIP/101", "state": "NOT_INUSE"}]

    body = publisher().collect(Ami())
    assert actions == ["DeviceStateList", "ExtensionStateList", "QueueSummary"]
    assert body["queues"] is None
    assert body["operators"][0]["state"] == "free"


@pytest.mark.parametrize(
    "status, expected",
    [
        (0, "NOT_INUSE"),
        (1, "INUSE"),
        (2, "BUSY"),
        (4, "UNAVAILABLE"),
        (9, "RINGINUSE"),
        (16, "ONHOLD"),
        (-1, "UNKNOWN"),
    ],
)
def test_legacy_sip_hints_supply_current_states(status, expected):
    row = dict(event="ExtensionStatus", exten="102", hint="SIP/102", status=str(status))
    assert hint_states(ROSTER, [row], {}) == {"sip/102": expected}


def test_hint_identity_dnd_and_multiple_contexts_are_conservative():
    def hint(extension, devices, status):
        return dict(event="ExtensionStatus", exten=extension, hint=devices, status=str(status))

    assert hint_states(
        ROSTER, [hint("102", "SIP/102&Custom:DND102,CustomPresence:102", 4)], {}
    ) == {"sip/102": "UNAVAILABLE"}
    assert hint_states(ROSTER, [hint("102", "SIP/102&Custom:DND102", 2)], {}) == {
        "sip/102": "UNKNOWN"
    }
    assert hint_states(
        ROSTER, [hint("102", "SIP/102&Custom:DND102", 2)], {"custom:dnd102": "NOT_INUSE"}
    ) == {"sip/102": "BUSY"}
    assert (
        hint_states(
            ROSTER,
            [
                hint("102", "SIP/102&SIP/103", 1),
                hint("102", "MWI:102@default", 1),
                hint("102", "PJSIP/102", 1),
            ],
            {},
        )
        == {}
    )
    assert hint_states(ROSTER, [hint("102", "SIP/102", 4)], {"sip/102": "INUSE"}) == {}
    assert hint_states(ROSTER, [hint("102", "SIP/102", 4), hint("102", "SIP/102", 0)], {}) == {
        "sip/102": "UNKNOWN"
    }


def test_hints_work_when_device_list_fails_without_inventing_missing_devices():
    class Ami:
        def action_list(self, name, complete):
            if name == "ExtensionStateList":
                return [dict(event="ExtensionStatus", exten="102", hint="SIP/102", status="4")]
            raise AMIError("unavailable")

    body = publisher().collect(Ami())
    assert [(row["extension"], row["state"]) for row in body["operators"]] == [("102", "offline")]
    assert body["queues"] is None


def test_failed_collectors_never_publish_cached_states_as_fresh():
    class Ami:
        def action_list(self, *args):
            raise AMIError("unavailable")

    with pytest.raises(AMIError):
        publisher().collect(Ami())


def test_unsolicited_events_are_processed_without_corrupting_list_responses(monkeypatch):
    sock = FakeSocket(
        {
            "devicestatelist": [
                {"Event": "DeviceStateChange", "Device": "SIP/101", "State": "INUSE"},
                {"Event": "DeviceStateListComplete"},
            ]
        }
    )
    connect_fake(monkeypatch, sock)
    events = []
    with AMIClient(
        username="panel", secret="test-secret", events="system", event_handler=events.append
    ) as ami:
        sock._out += block(Event="PeerStatus", Peer="SIP/101", PeerStatus="Registered")
        rows = ami.action_list("DeviceStateList", ["DeviceStateListComplete"])
    assert rows[0]["state"] == "INUSE"
    assert events[0]["peerstatus"] == "Registered"
    assert b"Events: system" in sock.raw_sent


@pytest.mark.parametrize("interval", [1, 4, 61])
def test_panel_interval_bounds(tmp_path, interval):
    path = tmp_path / "agent.conf"
    path.write_text("[agent]\npanel_interval_seconds = {}\n".format(interval))
    with pytest.raises(ConfigError):
        load(str(path))


def test_stop_interrupts_panel_wait_and_closes_connection(monkeypatch):
    p = publisher()
    closed = []

    class Ami:
        def __init__(self, **kwargs):
            pass

        def connect(self):
            pass

        def login(self):
            pass

        def close(self):
            closed.append(True)

    monkeypatch.setattr("pbxonix_agent.panel.AMIClient", Ami)
    monkeypatch.setattr(p, "collect", lambda ami: {})

    def posted(*args):
        p.stop()

    monkeypatch.setattr(p.client, "post", posted)
    p.start()
    p.join(timeout=2)
    assert not p.is_alive() and closed and p.active


def test_cloud_refusal_backs_off_without_replaying_payload(monkeypatch):
    p = publisher()
    waits = []

    class Ami:
        def __init__(self, **kwargs):
            pass

        def connect(self):
            pass

        def login(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("pbxonix_agent.panel.AMIClient", Ami)
    monkeypatch.setattr(p, "collect", lambda ami: {})

    def refused(*args):
        raise ApiError(401, "private response")

    monkeypatch.setattr(p.client, "post", refused)

    def waited(delay):
        waits.append(delay)
        p.stop_event.set()

    monkeypatch.setattr(p.stop_event, "wait", waited)
    p.run()
    assert waits[0] > 290 and not p.active


def test_closing_broken_ami_socket_always_releases_it(monkeypatch):
    sock = FakeSocket({})
    connect_fake(monkeypatch, sock)
    ami = AMIClient(username="panel", secret="test-secret")
    ami.connect()
    ami.login()

    def broken_pipe(data):
        raise BrokenPipeError("closed by server")

    monkeypatch.setattr(sock, "sendall", broken_pipe)
    ami.close()
    assert sock.closed and ami._sock is None
    ami.close()


def test_panel_reconnects_after_server_drops_the_persistent_session(monkeypatch):
    p = publisher()
    broken = FakeSocket({})
    healthy = FakeSocket({})
    sockets = iter([broken, healthy])
    monkeypatch.setattr("socket.create_connection", lambda *args, **kwargs: next(sockets))
    attempts = []

    def broken_pipe(data):
        raise BrokenPipeError("closed by server")

    def collect(ami):
        attempts.append(ami)
        if len(attempts) == 1:
            monkeypatch.setattr(broken, "sendall", broken_pipe)
            raise AMIError("server restarted")
        return {}

    monkeypatch.setattr(p, "collect", collect)
    monkeypatch.setattr(p.stop_event, "wait", lambda delay: None)
    monkeypatch.setattr(p.client, "post", lambda *args: p.stop())
    p.run()
    assert len(attempts) == 2 and attempts[0] is not attempts[1]
    assert broken.closed and healthy.closed and p.active
