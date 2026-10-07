"""Agent unit tests.

Everything here runs without a network and without a PBX. The collectors are
tested against synthetic /proc content so the results are deterministic on any
machine, including a CI runner with no Asterisk installed.
"""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from pbxonix_agent import credentials as creds
from pbxonix_agent import transport
from pbxonix_agent.buffer import Buffer
from pbxonix_agent.config import ConfigError
from pbxonix_agent.config import load as load_config


# ------------------------------------------------------------------ config --
def write_config(tmp_path, body: str) -> str:
    path = tmp_path / "agent.conf"
    path.write_text(body, encoding="utf-8")
    return str(path)


MINIMAL = """
[agent]
api_base_url = https://api.example.com
heartbeat_interval_seconds = 45
metrics_interval_seconds = 90
log_level = debug

[buffer]
path = /tmp/pbxonix-test/buffer.sqlite3
max_rows = 1234
max_bytes = 5678

[asterisk]
ami_enabled = true
ami_port = 5039

[recordings]
paths = /var/spool/asterisk/monitor, /srv/recordings
"""


def test_config_parses_every_section(tmp_path):
    config = load_config(write_config(tmp_path, MINIMAL))

    assert config.api_base_url == "https://api.example.com"
    assert config.heartbeat_interval_seconds == 45
    assert config.metrics_interval_seconds == 90
    assert config.log_level == "DEBUG"

    assert config.buffer.max_rows == 1234
    assert config.buffer.max_bytes == 5678
    assert config.asterisk.ami_enabled is True
    assert config.asterisk.ami_port == 5039
    assert config.recordings.paths == ["/var/spool/asterisk/monitor", "/srv/recordings"]


def test_config_uses_defaults_for_missing_sections(tmp_path):
    config = load_config(
        write_config(tmp_path, "[agent]\napi_base_url = https://api.example.com\n")
    )
    assert config.heartbeat_interval_seconds == 30
    assert config.asterisk.ami_enabled is False
    assert config.recordings.paths == []


def test_config_strips_inline_comments(tmp_path):
    config = load_config(
        write_config(
            tmp_path,
            "[agent]\napi_base_url = https://api.example.com\n"
            "heartbeat_interval_seconds = 60  # every minute\n",
        )
    )
    assert config.heartbeat_interval_seconds == 60


def test_config_rejects_plaintext_http(tmp_path):
    """Bearer credentials travel on every request; http:// must not be possible."""
    with pytest.raises(ConfigError, match="https"):
        load_config(write_config(tmp_path, "[agent]\napi_base_url = http://api.example.com\n"))


def test_config_rejects_absurd_interval(tmp_path):
    with pytest.raises(ConfigError, match="at least 5"):
        load_config(
            write_config(
                tmp_path,
                "[agent]\napi_base_url = https://api.example.com\nheartbeat_interval_seconds = 1\n",
            )
        )


def test_config_rejects_non_integer(tmp_path):
    with pytest.raises(ConfigError, match="integer"):
        load_config(
            write_config(
                tmp_path,
                "[agent]\napi_base_url = https://api.example.com\n"
                "heartbeat_interval_seconds = soon\n",
            )
        )


def test_missing_config_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(str(tmp_path / "nope.conf"))


def test_credentials_path_sits_next_to_the_config(tmp_path):
    config = load_config(write_config(tmp_path, MINIMAL))
    assert config.credentials_path == os.path.join(str(tmp_path), "credentials.json")


# ------------------------------------------------------------- credentials --
def test_credentials_round_trip_and_permissions(tmp_path):
    path = str(tmp_path / "credentials.json")
    creds.save(path, creds.Credentials(pbx_id="abc", agent_token="pbxa_secret", pbx_name="Office"))

    loaded = creds.load(path)
    assert loaded is not None
    assert loaded.pbx_id == "abc"
    assert loaded.agent_token == "pbxa_secret"
    assert loaded.pbx_name == "Office"

    # The file holds a live credential and must not be world readable.
    mode = os.stat(path).st_mode & 0o777
    assert mode == 0o640, oct(mode)


def test_missing_credentials_return_none(tmp_path):
    assert creds.load(str(tmp_path / "absent.json")) is None


def test_incomplete_credentials_are_rejected(tmp_path):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"pbx_id": "abc"}), encoding="utf-8")
    with pytest.raises(creds.CredentialsError):
        creds.load(str(path))


# ------------------------------------------------------------- local buffer --
def test_buffer_round_trips_samples(tmp_path):
    with Buffer(str(tmp_path / "buf.sqlite3")) as buffer:
        buffer.append("system", {"cpu_pct": 12.5})
        buffer.append("system", {"cpu_pct": 13.5})
        buffer.append("asterisk", {"running": True})

        assert buffer.count() == 3
        assert buffer.count("system") == 2

        rows = buffer.take("system", 10)
        assert [payload["cpu_pct"] for _, payload in rows] == [12.5, 13.5]

        buffer.delete([rows[0][0]])
        assert buffer.count("system") == 1


def test_buffer_preserves_supplied_timestamps(tmp_path):
    when = datetime.now(timezone.utc) - timedelta(hours=2)
    with Buffer(str(tmp_path / "buf.sqlite3")) as buffer:
        buffer.append("system", {"recorded_at": when.isoformat(), "cpu_pct": 1.0}, when)
        ((_, payload),) = buffer.take("system", 10)
        assert payload["recorded_at"] == when.isoformat()


def test_buffer_drops_oldest_when_full(tmp_path):
    """A monitoring agent must never be the reason a PBX runs out of disk."""
    with Buffer(str(tmp_path / "buf.sqlite3"), max_rows=10) as buffer:
        for i in range(25):
            buffer.append("system", {"seq": i})

        assert buffer.count() <= 10
        remaining = [payload["seq"] for _, payload in buffer.take("system", 100)]
        # Oldest go first, so the newest sample must still be there.
        assert 24 in remaining
        assert 0 not in remaining


def test_buffer_survives_reopen(tmp_path):
    path = str(tmp_path / "buf.sqlite3")
    with Buffer(path) as buffer:
        buffer.append("system", {"cpu_pct": 7.0})
    with Buffer(path) as buffer:
        assert buffer.count("system") == 1


def test_buffer_discards_corrupt_rows(tmp_path):
    path = str(tmp_path / "buf.sqlite3")
    with Buffer(path) as buffer:
        buffer.append("system", {"cpu_pct": 1.0})
        buffer._conn.execute(
            "INSERT INTO samples (kind, recorded_at, payload) VALUES (?,?,?)",
            ("system", "2026-01-01T00:00:00+00:00", "{not json"),
        )
        assert buffer.count("system") == 2

        rows = buffer.take("system", 10)
        # The unreadable row is dropped rather than blocking the queue forever.
        assert len(rows) == 1
        assert buffer.count("system") == 1


# ------------------------------------------------------------ retry/backoff --
def test_backoff_grows_and_is_capped():
    for attempt in range(10):
        delay = transport.backoff_delay(attempt, base=1.0, cap=60.0)
        assert 0 <= delay <= 60.0

    # Full jitter means any single draw can be small, so compare ceilings.
    assert max(transport.backoff_delay(0, base=1.0, cap=60.0) for _ in range(200)) <= 1.0
    assert max(transport.backoff_delay(3, base=1.0, cap=60.0) for _ in range(200)) <= 8.0


def test_backoff_is_jittered():
    """Without jitter a whole fleet would reconnect in lockstep after an outage."""
    draws = {round(transport.backoff_delay(4, base=1.0, cap=60.0), 6) for _ in range(50)}
    assert len(draws) > 1


def test_post_with_retry_gives_up_and_reports(monkeypatch):
    attempts = {"n": 0}

    def always_fail(self, path, payload):
        attempts["n"] += 1
        raise transport.TransportError("connection refused")

    monkeypatch.setattr(transport.Client, "post", always_fail)
    monkeypatch.setattr(transport.time, "sleep", lambda _s: None)

    client = transport.Client("https://api.example.com")
    with pytest.raises(transport.TransportError):
        client.post_with_retry("/v1/agent/heartbeat", {}, attempts=4)
    assert attempts["n"] == 4


def test_post_with_retry_succeeds_after_a_transient_failure(monkeypatch):
    attempts = {"n": 0}

    def flaky(self, path, payload):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise transport.TransportError("temporary")
        return {"ok": True}

    monkeypatch.setattr(transport.Client, "post", flaky)
    monkeypatch.setattr(transport.time, "sleep", lambda _s: None)

    client = transport.Client("https://api.example.com")
    assert client.post_with_retry("/v1/agent/heartbeat", {}) == {"ok": True}
    assert attempts["n"] == 3


def test_api_error_is_not_retried(monkeypatch):
    """A 400 will be a 400 next time too. Retrying just wastes the token."""
    attempts = {"n": 0}

    def refuse(self, path, payload):
        attempts["n"] += 1
        raise transport.ApiError(400, "invalid or expired enrollment token")

    monkeypatch.setattr(transport.Client, "post", refuse)
    client = transport.Client("https://api.example.com")

    with pytest.raises(transport.ApiError):
        client.post_with_retry("/v1/agent/enroll", {}, attempts=5)
    assert attempts["n"] == 1


def test_saving_hands_the_credential_to_the_service_group(tmp_path, monkeypatch):
    """0640 root:root is unreadable by the agent, which runs as pbxonix.

    The installer used to fix this afterwards, so enrolling by hand produced a
    credential the service could not read and a startup failure that pointed
    nowhere near the cause.
    """
    import grp as grp_module

    chowned = {}

    class FakeGroup:
        gr_gid = 4242

    monkeypatch.setattr(grp_module, "getgrnam", lambda name: FakeGroup())
    monkeypatch.setattr(
        creds.os, "chown", lambda path, uid, gid: chowned.update(path=path, uid=uid, gid=gid)
    )

    path = str(tmp_path / "credentials.json")
    creds.save(path, creds.Credentials(pbx_id="p", agent_token="t", pbx_name="n"))

    assert chowned["gid"] == 4242
    assert chowned["uid"] == -1, "the owner must be left alone"
    assert chowned["path"] == path


def test_saving_survives_a_box_without_the_service_group(tmp_path, monkeypatch):
    """Developer laptops and one-off runs have no pbxonix group. Mode is enough."""
    import grp as grp_module

    def no_such_group(_name):
        raise KeyError("pbxonix")

    monkeypatch.setattr(grp_module, "getgrnam", no_such_group)

    path = str(tmp_path / "credentials.json")
    creds.save(path, creds.Credentials(pbx_id="p", agent_token="t", pbx_name="n"))

    assert creds.load(path).pbx_id == "p"
    assert oct(os.stat(path).st_mode & 0o777) == oct(0o640)


def test_never_asking_the_cli_is_not_the_same_as_asking_and_being_fine(monkeypatch):
    """collect() skips the CLI when Asterisk is not running.

    Reporting "ok" there would be a confident wrong answer about something
    never checked -- the failure this whole module is built to avoid.
    """
    from pbxonix_agent.collectors import asterisk

    monkeypatch.setattr(asterisk, "_cli_attempted", False, raising=False)
    monkeypatch.setattr(asterisk, "_last_cli_error", None, raising=False)
    monkeypatch.setattr(asterisk, "is_running", lambda: None)

    state = asterisk.collect()
    assert state["running"] is None
    assert asterisk.cli_attempted() is False
    assert asterisk.cli_error() is None


def test_ami_config_reprints_the_stored_secret_instead_of_the_placeholder(tmp_path, capsys):
    """Running it twice must not invalidate what is already in manager.conf.

    The first run generated a secret and the operator pasted it into Asterisk.
    A second run that printed a fresh placeholder -- or worse, a fresh secret --
    would leave the two sides disagreeing with nothing to indicate it.
    """
    from pbxonix_agent import amiconf, cli

    config_path = write_config(tmp_path, AMI_OFF)
    creds.save(
        str(tmp_path / "credentials.json"),
        creds.Credentials(
            pbx_id="p",
            agent_token="t",
            pbx_name="n",
            ami_username="pbxonix",
            ami_password="already-in-manager-conf",
        ),
    )

    args = type("Args", (), {"config": config_path, "generate": False, "username": "pbxonix"})()
    assert cli.cmd_ami_config(args) == 0

    printed = capsys.readouterr().out
    assert "already-in-manager-conf" in printed
    assert amiconf.SECRET_PLACEHOLDER not in printed


def test_generate_still_replaces_a_stored_secret_when_asked(tmp_path, capsys):
    from pbxonix_agent import cli

    config_path = write_config(tmp_path, AMI_OFF)
    path = str(tmp_path / "credentials.json")
    creds.save(
        path,
        creds.Credentials(
            pbx_id="p",
            agent_token="t",
            pbx_name="n",
            ami_username="pbxonix",
            ami_password="old-secret",
        ),
    )

    args = type("Args", (), {"config": config_path, "generate": True, "username": "pbxonix"})()
    assert cli.cmd_ami_config(args) == 0

    printed = capsys.readouterr().out
    assert "old-secret" not in printed
    assert creds.load(path).ami_password != "old-secret"


def test_a_filled_in_config_stops_explaining_how_to_fill_it_in(tmp_path, capsys):
    """Header said "run --generate", footer said "do not re-run --generate".

    Both accurate, read together contradictory. Third time this file managed to
    say two things at once on one screen.
    """
    from pbxonix_agent import cli

    config_path = write_config(tmp_path, AMI_OFF)
    creds.save(
        str(tmp_path / "credentials.json"),
        creds.Credentials(
            pbx_id="p",
            agent_token="t",
            pbx_name="n",
            ami_username="pbxonix",
            ami_password="stored-secret",
        ),
    )

    args = type("Args", (), {"config": config_path, "generate": False, "username": "pbxonix"})()
    cli.cmd_ami_config(args)
    printed = capsys.readouterr().out

    assert "stored-secret" in printed
    assert "pbxonix-agent ami-config --generate" not in printed
    assert "the one the agent has stored" in printed


def test_an_unfilled_config_still_says_how_to_fill_it(tmp_path, capsys):
    from pbxonix_agent import amiconf, cli

    config_path = write_config(tmp_path, AMI_OFF)
    args = type("Args", (), {"config": config_path, "generate": False, "username": "pbxonix"})()
    cli.cmd_ami_config(args)
    printed = capsys.readouterr().out

    assert amiconf.SECRET_PLACEHOLDER in printed
    assert "pbxonix-agent ami-config --generate" in printed


# ------------------------------------------------------------ the ACL order --
def test_deny_comes_before_permit():
    """Asterisk applies the LAST matching rule.

    Written permit-then-deny, the catch-all deny lands last and matches every
    address including 127.0.0.1, so it overrides the loopback permit and
    refuses the agent's own connection. Every sample config Asterisk ships puts
    deny first, and this is why.
    """
    from pbxonix_agent import amiconf

    rules = [
        line
        for line in amiconf.MANAGER_CONF.split("\n")
        if line.startswith("deny") or line.startswith("permit")
    ]
    assert rules == ["deny = 0.0.0.0/0.0.0.0", "permit = 127.0.0.1/255.255.255.255"]


def test_check_reaches_the_ami_section_even_with_recordings_configured(tmp_path):
    """It did not, for every default install.

    cmd_check returned inside the recordings block, so the AMI connection test
    and the trunk listing below it were unreachable whenever recording paths
    were set -- which is the shipped default. Diagnostics nobody could ever see.
    """
    import inspect

    from pbxonix_agent import cli

    source = inspect.getsource(cli.cmd_check)
    # One exit, at the end. An early one is what hid the AMI section.
    assert source.count("return 0") == 1
    assert source.rstrip().endswith("return 0")
    assert "ami_snapshot.collect" in source


# ------------------------------------------------------- ami poll cadence --
# Reported from a live PBX: call audio broke up after AMI was switched on. Every
# sixty seconds the agent ran SIPpeers (128 peers, chan_sip holds the peer
# container locked while walking it) and QueueStatus (30 queues, ~200 member
# events) -- about 370 events a minute of data that changes maybe daily.
def test_inventory_polling_defaults_to_five_minutes(tmp_path):
    config = load_config(write_config(tmp_path, MINIMAL))
    assert config.asterisk.ami_interval_seconds == 300


def test_the_interval_has_a_floor(tmp_path):
    """This is the setting that cost a production system its call quality."""
    import pytest as _pytest

    body = MINIMAL.replace(
        "ami_port = 5039", "ami_port = 5039" + chr(10) + "ami_interval_seconds = 5"
    )
    with _pytest.raises(ConfigError):
        load_config(write_config(tmp_path, body))


def test_the_interval_is_configurable(tmp_path):
    body = MINIMAL.replace(
        "ami_port = 5039", "ami_port = 5039" + chr(10) + "ami_interval_seconds = 900"
    )
    assert load_config(write_config(tmp_path, body)).asterisk.ami_interval_seconds == 900


def test_the_cheap_poll_sends_no_trunk_report(tmp_path, monkeypatch):
    """The fast path must never post trunks.

    An empty trunk list means "this PBX has none" and would resolve every open
    trunk alert. The cheap poll only knows about channels, so it says nothing.
    """
    from pbxonix_agent import runner
    from pbxonix_agent.collectors import snapshot as ami_snapshot

    posted = []

    class Client:
        def post(self, path, payload):
            posted.append(path)

    class FakeAMI:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    agent = runner.Agent.__new__(runner.Agent)
    agent.config = load_config(write_config(tmp_path, AMI_ON))
    agent.credentials = creds.Credentials(
        pbx_id="p",
        agent_token="t",
        pbx_name="n",
        ami_username="pbxonix",
        ami_password="s",
    )
    agent.client = Client()
    agent._ami_ok = None
    agent._ami_core = {}
    agent._ami_core_at = 0.0
    agent._ami_quiet_until = 0.0
    agent._unauthorized_until = 0.0
    agent._throttled_until = 0.0
    agent._extensions = []

    monkeypatch.setattr(runner, "AMIClient", lambda **kw: FakeAMI())
    monkeypatch.setattr(
        ami_snapshot, "core_counts", lambda ami: {"active_calls": 3, "active_channels": 5}
    )

    agent.collect_channel_counts()

    assert posted == [], "the cheap poll must not report trunks or queues"
    assert agent._ami_core["active_calls"] == 3


def test_the_cheap_poll_refreshes_extension_state_but_never_the_roster(tmp_path, monkeypatch):
    """Who is free has to be asked often; who exists does not.

    A call lasts a couple of minutes, so a five-minute-old "free" is wrong more
    often than right. The roster it is layered onto comes from the slow pass,
    because walking the peer containers is the part that must stay rare.
    """
    from pbxonix_agent import runner
    from pbxonix_agent.collectors import extensions as extension_collector
    from pbxonix_agent.collectors import snapshot as ami_snapshot

    posted = []

    class Client:
        def post(self, path, payload):
            posted.append((path, payload))

    class FakeAMI:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    agent = runner.Agent.__new__(runner.Agent)
    agent.config = load_config(write_config(tmp_path, AMI_ON))
    agent.credentials = creds.Credentials(
        pbx_id="p",
        agent_token="t",
        pbx_name="n",
        ami_username="pbxonix",
        ami_password="s",
    )
    agent.client = Client()
    agent._ami_ok = None
    agent._ami_core = {}
    agent._ami_core_at = 0.0
    agent._ami_quiet_until = 0.0
    agent._unauthorized_until = 0.0
    agent._throttled_until = 0.0
    agent.panel_publisher = None
    agent._extensions = [{"extension": "101", "tech": "SIP", "state": "free", "detail": None}]

    monkeypatch.setattr(runner, "AMIClient", lambda **kw: FakeAMI())
    monkeypatch.setattr(ami_snapshot, "core_counts", lambda ami: {})
    monkeypatch.setattr(extension_collector, "device_states", lambda ami: {"sip/101": "busy"})

    agent.collect_channel_counts()

    assert [path for path, _ in posted] == ["/v1/agent/extensions"]
    assert posted[0][1]["extensions"][0]["state"] == "busy"


def test_the_cheap_poll_asks_for_no_states_without_a_roster(tmp_path, monkeypatch):
    """A device state with no roster behind it is a queue hint or a park lot.

    Asking for them before the first inventory pass would cost an action per
    minute and produce nothing that belongs on a panel of people.
    """
    from pbxonix_agent import runner
    from pbxonix_agent.collectors import extensions as extension_collector
    from pbxonix_agent.collectors import snapshot as ami_snapshot

    asked = []
    posted = []

    class Client:
        def post(self, path, payload):
            posted.append(path)

    class FakeAMI:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    agent = runner.Agent.__new__(runner.Agent)
    agent.config = load_config(write_config(tmp_path, AMI_ON))
    agent.credentials = creds.Credentials(
        pbx_id="p",
        agent_token="t",
        pbx_name="n",
        ami_username="pbxonix",
        ami_password="s",
    )
    agent.client = Client()
    agent._ami_ok = None
    agent._ami_core = {}
    agent._ami_core_at = 0.0
    agent._ami_quiet_until = 0.0
    agent._unauthorized_until = 0.0
    agent._throttled_until = 0.0
    agent._extensions = []

    monkeypatch.setattr(runner, "AMIClient", lambda **kw: FakeAMI())
    monkeypatch.setattr(ami_snapshot, "core_counts", lambda ami: {})
    monkeypatch.setattr(extension_collector, "device_states", lambda ami: asked.append(1) or {})

    agent.collect_channel_counts()

    assert asked == []
    assert posted == []


# ------------------------------------------------- calls versus channels --
# CoreStatus.CoreCurrentCalls is a channel counter wearing a misleading name, so
# reporting it as calls put the same number on the dashboard twice -- 6825
# consecutive samples from a live PBX agreed exactly. A channel count is no
# stand-in either: on that PBX 27 channels were ONE call plus 21 media forks
# from a recording system, each fork holding its own linkedid.
def channel(name, linkedid, event="CoreShowChannel"):
    return {"event": event, "channel": name, "linkedid": linkedid, "uniqueid": name}


class ChannelAMI:
    def __init__(self, channels):
        self.channels = channels

    def action_list(self, name, complete_events, **fields):
        assert name == "CoreShowChannels"
        return self.channels


def test_a_media_fork_is_not_a_call():
    """The case that started this: a recording system's ExternalMedia legs.

    Each carries its own linkedid and no endpoint, so neither a channel count
    nor a linkedid count can tell them from conversations.
    """
    from pbxonix_agent.collectors import snapshot

    forks = [channel(f"UnicastRTP/198.51.100.{i}-0", f"fork-{i}") for i in range(21)]
    real = [
        channel("SIP/trunk-0001", "call-1"),
        channel("SIP/agent-0002", "call-1"),
        channel("Local/100@from-queue-0003;1", "call-1"),
        channel("Local/100@from-queue-0003;2", "call-1"),
    ]

    counts = snapshot.core_counts(ChannelAMI(forks + real))
    assert counts["active_calls"] == 1, "21 media forks must not read as calls"
    assert counts["active_channels"] == 2, "only the SIP legs are endpoint legs"


def test_two_separate_calls_count_as_two():
    from pbxonix_agent.collectors import snapshot

    counts = snapshot.core_counts(
        ChannelAMI(
            [
                channel("SIP/a-1", "call-a"),
                channel("SIP/b-2", "call-a"),
                channel("PJSIP/c-3", "call-b"),
            ]
        )
    )
    assert counts["active_calls"] == 2
    assert counts["active_channels"] == 3


def test_a_ringing_leg_is_already_a_call():
    """Nobody has answered, but the line is in use and the seat is occupied."""
    from pbxonix_agent.collectors import snapshot

    counts = snapshot.core_counts(ChannelAMI([channel("SIP/trunk-0001", "call-1")]))
    assert counts["active_calls"] == 1


def test_routing_legs_alone_are_not_a_call():
    """Local channels are dialplan plumbing; without an endpoint there is no
    party on either end."""
    from pbxonix_agent.collectors import snapshot

    counts = snapshot.core_counts(
        ChannelAMI(
            [
                channel("Local/x@ctx-0001;1", "l-1"),
                channel("Local/x@ctx-0001;2", "l-1"),
            ]
        )
    )
    assert counts["active_calls"] == 0
    assert counts["active_channels"] == 0


def test_an_unknown_technology_counts_as_real():
    """The exclude list errs towards counting on purpose. An include list would
    silently drop a driver nobody thought of, making a busy system look idle --
    and an invisible undercount is worse than a visible overcount."""
    from pbxonix_agent.collectors import snapshot

    counts = snapshot.core_counts(ChannelAMI([channel("SomethingNew/x-1", "call-1")]))
    assert counts["active_calls"] == 1


def test_a_failed_query_is_unknown_not_zero():
    """A PBX we could not ask is not a quiet one, and charting it as idle would
    hide a busy hour."""
    from pbxonix_agent.collectors import snapshot
    from pbxonix_agent.collectors.ami import AMIError

    class Broken:
        def action_list(self, *a, **k):
            raise AMIError("denied")

    assert snapshot.core_counts(Broken()) == {
        "active_calls": None,
        "active_channels": None,
    }


def test_the_cli_no_longer_guesses_at_calls():
    """`core show channels count` prints an "active calls" line, but it is the
    same misnamed counter. A dash beats a confident wrong number."""
    import sys as _sys

    from pbxonix_agent.collectors import asterisk

    def fake(argv, **kwargs):
        return asterisk.subprocess.CompletedProcess(
            argv, 0, "12 active channels" + chr(10) + "12 active calls" + chr(10), ""
        )

    import pytest as _pytest  # noqa: F401 - keeps the fixture-free style explicit

    original_which = asterisk.shutil.which
    original_run = asterisk.subprocess.run
    asterisk.shutil.which = lambda name: _sys.executable
    asterisk.subprocess.run = fake
    try:
        counts = asterisk.channel_counts()
    finally:
        asterisk.shutil.which = original_which
        asterisk.subprocess.run = original_run

    assert counts["active_channels"] == 12
    assert counts["active_calls"] is None


# ------------------------------------------- trunks that never register --
# A trunk authenticated by IP holds no registration, so it appears in neither
# SIPshowregistry nor PJSIPShowRegistrationsOutbound. Reading only those made it
# invisible -- and, worse, un-alertable. On the Issabel this was found on, two
# such trunks were UNREACHABLE while the dashboard showed fourteen healthy ones.
class PeerAMI:
    """Answers SIPpeers with a scripted list and nothing else."""

    def __init__(self, peers):
        self.peers = peers

    def action_list(self, name, complete_events, **fields):
        if name == "SIPpeers":
            return self.peers
        from pbxonix_agent.collectors.ami import AMIError

        raise AMIError("not scripted: " + name)


def peer(name, dynamic="no", status="OK (3 ms)"):
    return {
        "event": "PeerEntry",
        "objectname": name,
        "chanobjecttype": "peer",
        "dynamic": dynamic,
        "status": status,
    }


def test_a_static_peer_is_reported_as_a_trunk():
    from pbxonix_agent.collectors import trunks

    found = trunks.sip_peers(PeerAMI([peer("Novofon")]))
    assert len(found) == 1
    assert found[0]["name"] == "Novofon"
    assert found[0]["state"] == trunks.REACHABLE
    assert found[0]["kind"] == trunks.KIND_PEER
    assert found[0]["detail"] == "OK (3 ms)"


def test_a_dynamic_peer_is_an_extension_not_a_trunk():
    """90 desk phones must not become 90 trunks."""
    from pbxonix_agent.collectors import trunks

    assert trunks.sip_peers(PeerAMI([peer("1001", dynamic="yes")])) == []


def test_an_unreachable_peer_is_reported_down():
    """The case that prompted all of this."""
    from pbxonix_agent.collectors import trunks

    found = trunks.sip_peers(PeerAMI([peer("vsk", status="UNREACHABLE")]))
    assert found[0]["state"] == trunks.UNREACHABLE
    assert found[0]["detail"] == "UNREACHABLE"


def test_unmonitored_is_unknown_never_up():
    """qualify is off, so Asterisk has not checked and neither have we.

    Reporting it as up would be a green trunk nobody probed -- exactly the
    reassurance a monitoring tool must not invent.
    """
    from pbxonix_agent.collectors import trunks

    found = trunks.sip_peers(PeerAMI([peer("partner", status="Unmonitored")]))
    assert found[0]["state"] == trunks.UNKNOWN


def test_lagged_is_still_up():
    """It answered, slowly. The raw word rides along for the operator."""
    from pbxonix_agent.collectors import trunks

    found = trunks.sip_peers(PeerAMI([peer("slow", status="Lagged (450 ms)")]))
    assert found[0]["state"] == trunks.REACHABLE
    assert "450" in found[0]["detail"]


def test_registrations_are_tagged_as_such():
    from pbxonix_agent.collectors import trunks

    class RegAMI:
        def action_list(self, name, complete_events, **fields):
            if name == "SIPshowregistry":
                return [
                    {
                        "event": "RegistryEntry",
                        "username": "247108",
                        "host": "sip.novofon.com",
                        "state": "Registered",
                    }
                ]
            from pbxonix_agent.collectors.ami import AMIError

            raise AMIError("not scripted")

    found = trunks.sip_registrations(RegAMI())
    assert found[0]["kind"] == trunks.KIND_REGISTRATION
    assert found[0]["state"] == trunks.REGISTERED


def test_a_missing_sip_driver_yields_nothing_rather_than_an_error():
    """An empty report means "no trunks configured". It must never mean
    "could not ask" -- that would resolve every open trunk alert."""
    from pbxonix_agent.collectors import trunks
    from pbxonix_agent.collectors.ami import AMIError

    class Broken:
        def action_list(self, *a, **k):
            raise AMIError("unknown action")

    assert trunks.sip_peers(Broken()) == []
    assert trunks.pjsip_static_aors(Broken()) == []


def test_a_pjsip_aor_without_a_static_contact_is_an_extension():
    from pbxonix_agent.collectors import trunks

    class AorAMI:
        def action_list(self, name, complete_events, **fields):
            if name == "PJSIPShowAors":
                return [
                    {"event": "AorList", "objectname": "1001", "contacts": ""},
                    {
                        "event": "AorList",
                        "objectname": "provider",
                        "contacts": "sip:provider@1.2.3.4",
                    },
                    {"event": "ContactStatusDetail", "aor": "provider", "status": "Reachable"},
                ]
            from pbxonix_agent.collectors.ami import AMIError

            raise AMIError("not scripted")

    found = trunks.pjsip_static_aors(AorAMI())
    assert [f["name"] for f in found] == ["provider"]
    assert found[0]["state"] == trunks.REACHABLE


def test_a_pjsip_aor_with_no_qualify_result_is_unknown():
    from pbxonix_agent.collectors import trunks

    class AorAMI:
        def action_list(self, name, complete_events, **fields):
            if name == "PJSIPShowAors":
                return [{"event": "AorList", "objectname": "p", "contacts": "sip:p@1.2.3.4"}]
            from pbxonix_agent.collectors.ami import AMIError

            raise AMIError("not scripted")

    assert trunks.pjsip_static_aors(AorAMI())[0]["state"] == trunks.UNKNOWN


# ------------------------------------------------ channel counts over AMI --
# A stock Issabel creates /var/run/asterisk/asterisk.ctl as 0755, and connecting
# to a unix socket needs *write*. So only the asterisk user can use the CLI;
# group membership does not help, and changing it means restarting Asterisk on a
# live phone system. AMI is TCP on loopback and answers the same questions.
def core_agent(tmp_path, cli_state, ami_core, age=0.0):
    from pbxonix_agent import runner

    agent = runner.Agent.__new__(runner.Agent)
    agent.config = load_config(write_config(tmp_path, AMI_ON))
    agent._ami_core = ami_core
    agent._ami_core_at = __import__("time").monotonic() - age
    return agent, cli_state


def test_the_local_cli_wins_when_it_answers(tmp_path):
    agent, state = core_agent(
        tmp_path, {"active_calls": 3}, {"active_calls": 99, "active_channels": 99}
    )
    assert agent._channel_count(state, "active_calls") == 3


def test_ami_fills_in_when_the_cli_socket_is_unreachable(tmp_path):
    agent, state = core_agent(
        tmp_path, {"active_calls": None}, {"active_calls": 2, "active_channels": 4}
    )
    assert agent._channel_count(state, "active_calls") == 2
    assert agent._channel_count(state, "active_channels") == 4


def test_zero_from_the_cli_is_a_number_not_a_gap(tmp_path):
    """0 active calls is an answer. Falling through to AMI would discard it."""
    agent, state = core_agent(tmp_path, {"active_calls": 0}, {"active_calls": 7})
    assert agent._channel_count(state, "active_calls") == 0


def test_a_stale_ami_reading_decays_to_unknown(tmp_path):
    """A count from an unknown time reported as current is worse than a blank.

    AMI is polled on the metrics cadence; if it stops being refreshed the last
    value must not sit on the dashboard looking live.
    """
    agent, state = core_agent(tmp_path, {"active_calls": None}, {"active_calls": 5}, age=10_000)
    assert agent._channel_count(state, "active_calls") is None


def test_no_ami_reading_at_all_is_unknown(tmp_path):
    agent, state = core_agent(tmp_path, {"active_calls": None}, {})
    assert agent._channel_count(state, "active_calls") is None


def test_core_counts_reports_none_rather_than_zero_when_asterisk_refuses():
    """The distinction the whole codebase rests on: absence is not zero."""
    from pbxonix_agent.collectors import snapshot
    from pbxonix_agent.collectors.ami import AMIError

    class RefusingAMI:
        def action(self, name, **fields):
            raise AMIError("permission denied")

        def action_list(self, name, complete_events, **fields):
            raise AMIError("permission denied")

    assert snapshot.core_counts(RefusingAMI()) == {
        "active_calls": None,
        "active_channels": None,
    }


# ------------------------------------------------------- enrolling over junk --
def test_enrolling_survives_an_unreadable_credential_file(tmp_path, monkeypatch, capsys):
    """Enrolling is what *creates* this file; a damaged one must not block it.

    Found the hard way: a truncated credentials.json made `enroll` raise before
    it ever wrote anything, so the only path forward was deleting the file --
    which the error message did not mention. A filled disk or a hand edit gets
    an operator there without any help from us.
    """
    from pbxonix_agent import cli, transport

    config_path = write_config(tmp_path, AMI_OFF)
    credentials = tmp_path / "credentials.json"
    credentials.write_text('"not an object"', encoding="utf-8")

    monkeypatch.setattr(
        transport.Client,
        "post_with_retry",
        lambda self, path, payload, attempts=3: {
            "pbx_id": "new-id",
            "agent_token": "pbxa_new",
            "pbx_name": "Recovered",
        },
    )

    args = type("Args", (), {"config": config_path, "token": "pbxe_x"})()
    assert cli.cmd_enroll(args) == 0

    stored = creds.load(str(credentials))
    assert stored.pbx_id == "new-id"
    assert stored.agent_token == "pbxa_new"
    # There was nothing readable to preserve, and that is not an error.
    assert stored.has_ami is False


def test_a_readable_file_still_has_its_ami_credential_preserved(tmp_path, monkeypatch):
    """The behaviour the try/except must not have quietly removed."""
    from pbxonix_agent import cli, transport

    config_path = write_config(tmp_path, AMI_OFF)
    credentials = str(tmp_path / "credentials.json")
    creds.save(
        credentials,
        creds.Credentials(
            pbx_id="old",
            agent_token="old-token",
            pbx_name="Old",
            ami_username="pbxonix",
            ami_password="kept",
        ),
    )

    monkeypatch.setattr(
        transport.Client,
        "post_with_retry",
        lambda self, path, payload, attempts=3: {
            "pbx_id": "new-id",
            "agent_token": "pbxa_new",
            "pbx_name": "Renamed",
        },
    )

    assert cli.cmd_enroll(type("Args", (), {"config": config_path, "token": "t"})()) == 0

    stored = creds.load(credentials)
    assert stored.agent_token == "pbxa_new"
    assert stored.ami_username == "pbxonix"
    assert stored.ami_password == "kept"


# ------------------------------------------------------- shipped ami config --
def test_the_manager_snippet_is_carried_in_the_package(tmp_path):
    """The installer ships one wheel and nothing else.

    Both the docs and the agent's own post-enrollment message used to point at
    `packaging/manager_pbxonix.conf.example`, a path that does not exist on a
    customer's PBX. Third instance of the same class of bug: instructions
    naming something the reader does not have.
    """
    from pbxonix_agent import amiconf

    assert "[pbxonix]" in amiconf.MANAGER_CONF
    assert amiconf.SECRET_PLACEHOLDER in amiconf.MANAGER_CONF
    # The permissions the collector actually needs, and the ones it must not get.
    assert "read = system,reporting" in amiconf.MANAGER_CONF
    assert "write = system,reporting" in amiconf.MANAGER_CONF
    for dangerous in ("originate", "= all"):
        assert "\n{}".format(dangerous) not in amiconf.MANAGER_CONF
    assert "permit = 127.0.0.1" in amiconf.MANAGER_CONF


def test_the_repository_copy_has_not_drifted():
    """Two copies of the same text is a promise to keep them equal."""
    import os

    from pbxonix_agent import amiconf

    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(here, "packaging", "manager_pbxonix.conf.example")
    if not os.path.exists(path):
        return  # running from an installed wheel, where packaging/ is absent
    with open(path, encoding="utf-8") as handle:
        assert handle.read() == amiconf.MANAGER_CONF


def test_generate_fills_in_a_real_secret_and_stores_it(tmp_path, monkeypatch, capsys):
    from pbxonix_agent import amiconf, cli

    config_path = write_config(tmp_path, AMI_OFF)
    creds.save(
        str(tmp_path / "credentials.json"),
        creds.Credentials(pbx_id="p", agent_token="t", pbx_name="n"),
    )

    args = type(
        "Args",
        (),
        {
            "config": config_path,
            "generate": True,
            "username": "pbxonix",
        },
    )()
    assert cli.cmd_ami_config(args) == 0

    printed = capsys.readouterr().out
    assert amiconf.SECRET_PLACEHOLDER not in printed, "the placeholder was left in"

    stored = creds.load(str(tmp_path / "credentials.json"))
    assert stored.has_ami
    assert stored.ami_username == "pbxonix"
    # The secret Asterisk is told to expect must be the one the agent will send.
    assert stored.ami_password in printed


def test_without_generate_it_only_prints(tmp_path, capsys):
    from pbxonix_agent import amiconf, cli

    config_path = write_config(tmp_path, AMI_OFF)
    args = type("Args", (), {"config": config_path, "generate": False, "username": "pbxonix"})()
    assert cli.cmd_ami_config(args) == 0

    assert amiconf.SECRET_PLACEHOLDER in capsys.readouterr().out
    # Nothing was written anywhere.
    assert not (tmp_path / "credentials.json").exists()


# ------------------------------------------------------------- ami state --
# Reported with every heartbeat so the dashboard can say why trunks and queues
# are empty. Off is the default, so "no trunks reported yet" was what every
# fresh install saw -- a setup step nobody mentioned, phrased as a finding.
def make_bare_agent(tmp_path, body, ami_username=""):
    from pbxonix_agent import runner

    agent = runner.Agent.__new__(runner.Agent)
    agent.config = load_config(write_config(tmp_path, body))
    agent.credentials = creds.Credentials(
        pbx_id="p",
        agent_token="t",
        pbx_name="n",
        ami_username=ami_username,
        ami_password="s" if ami_username else "",
    )
    agent._ami_ok = None
    return agent


AMI_OFF = "[agent]\napi_base_url = https://api.example.com\n"
AMI_ON = AMI_OFF + "\n[asterisk]\nami_enabled = true\n"


def test_ami_off_is_reported_as_disabled(tmp_path):
    assert make_bare_agent(tmp_path, AMI_OFF)._ami_state() == "disabled"


def test_ami_on_without_a_credential_is_unconfigured(tmp_path):
    assert make_bare_agent(tmp_path, AMI_ON)._ami_state() == "unconfigured"


def test_before_the_first_attempt_it_is_pending_not_broken(tmp_path):
    """Absence of a result is not a failure. Reporting "unreachable" here would
    accuse a PBX that has simply not been asked yet."""
    agent = make_bare_agent(tmp_path, AMI_ON, ami_username="pbxonix")
    assert agent._ami_state() == "pending"


def test_a_successful_collection_reports_ok(tmp_path):
    agent = make_bare_agent(tmp_path, AMI_ON, ami_username="pbxonix")
    agent._ami_ok = True
    assert agent._ami_state() == "ok"


def test_a_refused_connection_reports_unreachable(tmp_path):
    agent = make_bare_agent(tmp_path, AMI_ON, ami_username="pbxonix")
    agent._ami_ok = False
    assert agent._ami_state() == "unreachable"


# ------------------------------------------------ asterisk cli diagnostics --
# A blank "active calls" on the dashboard has two very different causes: a quiet
# PBX, and a CLI the agent cannot reach. The collector used to swallow the
# second, so both looked identical -- an em dash and no way to find out.
def test_a_failing_cli_records_why(monkeypatch):
    import sys as _sys

    from pbxonix_agent.collectors import asterisk

    monkeypatch.setattr(asterisk, "_last_cli_error", None, raising=False)
    monkeypatch.setattr(asterisk, "_reported_cli_error", None, raising=False)
    monkeypatch.setattr(asterisk.shutil, "which", lambda name: _sys.executable)

    # Stand in for `asterisk -rx` refusing: non-zero exit with a message.
    def refuse(argv, **kwargs):
        return asterisk.subprocess.CompletedProcess(
            argv, 1, "", "Unable to connect to remote asterisk (does asterisk.ctl exist?)"
        )

    monkeypatch.setattr(asterisk.subprocess, "run", refuse)

    assert asterisk.channel_counts() == {"active_channels": None, "active_calls": None}
    reason = asterisk.cli_error()
    assert reason is not None
    assert "exit 1" in reason
    assert "asterisk.ctl" in reason


def test_a_missing_binary_says_so(monkeypatch):
    from pbxonix_agent.collectors import asterisk

    monkeypatch.setattr(asterisk, "_last_cli_error", None, raising=False)
    monkeypatch.setattr(asterisk, "_reported_cli_error", None, raising=False)
    monkeypatch.setattr(asterisk.shutil, "which", lambda _name: None)

    asterisk.channel_counts()
    assert "not found on PATH" in (asterisk.cli_error() or "")


def test_a_working_cli_clears_the_reason(monkeypatch):
    import sys as _sys

    from pbxonix_agent.collectors import asterisk

    monkeypatch.setattr(asterisk, "_last_cli_error", "stale", raising=False)
    monkeypatch.setattr(asterisk, "_reported_cli_error", "stale", raising=False)
    monkeypatch.setattr(asterisk.shutil, "which", lambda name: _sys.executable)
    monkeypatch.setattr(
        asterisk.subprocess,
        "run",
        lambda argv, **kw: asterisk.subprocess.CompletedProcess(
            argv, 0, "2 active channels\n1 active call\n", ""
        ),
    )

    # Channels still come from the CLI. Calls no longer do: the CLI's "active
    # calls" line is the same misnamed channel counter, and separating the two
    # needs the per-channel list that only AMI provides.
    assert asterisk.channel_counts() == {"active_channels": 2, "active_calls": None}
    assert asterisk.cli_error() is None


def test_the_version_fallback_does_not_need_the_socket(monkeypatch):
    """Why a version can show while every other CLI field is blank.

    `asterisk -V` reads the binary; `-rx` needs the running process. Seeing one
    without the other is the tell that the CLI socket is unreachable, and it is
    exactly what a real Issabel box reported.
    """
    import sys as _sys

    from pbxonix_agent.collectors import asterisk

    monkeypatch.setattr(asterisk.shutil, "which", lambda name: _sys.executable)

    def only_dash_capital_v(argv, **kwargs):
        if "-rx" in argv:
            return asterisk.subprocess.CompletedProcess(argv, 1, "", "cannot connect")
        return asterisk.subprocess.CompletedProcess(argv, 0, "Asterisk 16.16.1", "")

    monkeypatch.setattr(asterisk.subprocess, "run", only_dash_capital_v)

    assert asterisk.version() == "16.16.1"
    assert asterisk.channel_counts()["active_calls"] is None


# ------------------------------------------------- oldest supported runtime --
# The floor is Python 3.6, which is what a stock Issabel 4 / CentOS 7 box ships.
# Compiling on 3.6 proves nothing about these: `capture_output=`, `text=` and
# `add_subparsers(required=)` are all 3.7+ keyword arguments, so they parse
# cleanly and raise TypeError only when the line actually runs. Every test here
# exists to make that line run.
def test_the_subprocess_helpers_actually_run():
    """Both copies of _run, exercised against a real process.

    TypeError is not in the except clause, so on an interpreter that rejects
    these keywords the collector crashes rather than reporting "unknown" --
    the failure mode the collector was written to avoid.
    """
    import sys as _sys

    from pbxonix_agent import facts
    from pbxonix_agent.collectors import asterisk

    for module in (asterisk, facts):
        out = module._run([_sys.executable, "-c", "print('pbxonix')"])
        assert out == "pbxonix", module.__name__


def test_the_subprocess_helpers_swallow_a_failing_command():
    import sys as _sys

    from pbxonix_agent.collectors import asterisk

    assert asterisk._run([_sys.executable, "-c", "raise SystemExit(3)"]) is None


def test_the_cli_parser_builds_and_demands_a_subcommand():
    from pbxonix_agent import cli

    parser = cli._build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args([])

    assert parser.parse_args(["run", "--config", "/tmp/x.conf"]).command == "run"
    assert parser.parse_args(["check"]).command == "check"


def test_the_cli_reports_its_version():
    from pbxonix_agent import __version__, cli

    with pytest.raises(SystemExit) as caught:
        cli._build_parser().parse_args(["--version"])
    assert caught.value.code == 0
    assert __version__


# ------------------------------------------------------------- throttling --
def http_error(code: int, headers=None):
    import email.message
    import urllib.error

    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return urllib.error.HTTPError("https://api.example.com/x", code, "nope", message, None)


def test_a_429_is_transient_not_a_refusal(monkeypatch):
    """The distinction the buffer flush depends on.

    Every other 4xx means "this will never be accepted"; 429 means "this will be
    accepted later". Classifying it as an ApiError would make the flush discard
    the very backlog the limit exists to protect.
    """
    monkeypatch.setattr(
        transport.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(http_error(429, {"Retry-After": "42"})),
    )
    client = transport.Client("https://api.example.com")

    with pytest.raises(transport.RateLimited) as caught:
        client.post("/v1/agent/metrics", {})

    assert caught.value.retry_after == 42
    assert isinstance(caught.value, transport.TransportError)
    assert not isinstance(caught.value, transport.ApiError)


@pytest.mark.parametrize("headers", [{}, {"Retry-After": "soon"}, {"Retry-After": ""}])
def test_retry_after_falls_back_rather_than_guessing_low(monkeypatch, headers):
    monkeypatch.setattr(
        transport.urllib.request,
        "urlopen",
        lambda *a, **k: (_ for _ in ()).throw(http_error(429, headers)),
    )
    client = transport.Client("https://api.example.com")

    with pytest.raises(transport.RateLimited) as caught:
        client.post("/v1/agent/metrics", {})
    assert caught.value.retry_after == 60


def test_a_429_is_not_retried_in_place(monkeypatch):
    """The window is minutes; the backoff is seconds. Every attempt would fail
    and count against us again."""
    attempts = {"n": 0}

    def limited(self, path, payload):
        attempts["n"] += 1
        raise transport.RateLimited(120)

    monkeypatch.setattr(transport.Client, "post", limited)
    monkeypatch.setattr(transport.time, "sleep", lambda _s: None)
    client = transport.Client("https://api.example.com")

    with pytest.raises(transport.RateLimited):
        client.post_with_retry("/v1/agent/metrics", {}, attempts=5)
    assert attempts["n"] == 1


def make_agent(tmp_path, client):
    """An Agent with only the pieces flush_buffer touches."""
    from pbxonix_agent import runner

    agent = runner.Agent.__new__(runner.Agent)
    agent.buffer = Buffer(str(tmp_path / "buf.sqlite3"))
    agent.client = client
    agent._unauthorized_until = 0.0
    agent._throttled_until = 0.0
    return agent


class RefusingClient:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def post(self, path, payload):
        self.calls += 1
        raise self.error


def test_throttling_never_discards_buffered_metrics(tmp_path):
    """The agent that trips the limit is the one with the most to lose.

    Hitting the limit means sending a lot -- a busy PBX, or one catching up
    after an outage. Discarding here would delete exactly the backlog that
    matters, and the agent would look healthy while doing it.
    """
    client = RefusingClient(transport.RateLimited(90))
    agent = make_agent(tmp_path, client)
    with agent.buffer:
        for i in range(5):
            agent.buffer.append("system", {"seq": i})

        assert agent.flush_buffer() == 0

        assert agent.buffer.count() == 5, "buffered samples were thrown away"
        assert agent._throttled_until > 0, "the agent did not back off"


def test_a_real_refusal_still_discards(tmp_path):
    """The contrast that makes the case above a decision rather than an oversight.

    A 400 will be a 400 next time too; keeping those rows would block the queue
    forever.
    """
    client = RefusingClient(transport.ApiError(400, "malformed sample"))
    agent = make_agent(tmp_path, client)
    with agent.buffer:
        agent.buffer.append("system", {"seq": 1})

        assert agent.flush_buffer() == 0
        assert agent.buffer.count() == 0


def test_a_throttled_agent_stops_sending_until_the_window_passes(tmp_path):
    client = RefusingClient(transport.RateLimited(90))
    agent = make_agent(tmp_path, client)
    with agent.buffer:
        agent.buffer.append("system", {"seq": 1})
        agent.flush_buffer()
        before = client.calls

        # A second cycle inside the window must not touch the network at all.
        assert agent.flush_buffer() == 0
        assert client.calls == before

        agent._throttled_until = 0.0
        agent.flush_buffer()
        assert client.calls == before + 1


# --------------------------------------------------------- system collector --
def test_cpu_sampler_needs_two_readings(monkeypatch):
    from pbxonix_agent.collectors import system

    readings = iter([(100, 200), (150, 300)])
    monkeypatch.setattr(system, "_cpu_totals", lambda: next(readings))

    sampler = system.CpuSampler()
    # Nothing to diff against yet -- a made-up number would be worse than None.
    assert sampler.sample() is None
    # busy +50 of total +100 == 50%
    assert sampler.sample() == 50.0


def test_cpu_sampler_handles_unreadable_proc(monkeypatch):
    from pbxonix_agent.collectors import system

    monkeypatch.setattr(system, "_cpu_totals", lambda: None)
    assert system.CpuSampler().sample() is None


def test_memory_uses_available_not_free(monkeypatch):
    from pbxonix_agent.collectors import system

    proc_meminfo = (
        "MemTotal:       8000000 kB\n"
        "MemFree:         100000 kB\n"
        "MemAvailable:   6000000 kB\n"
        "SwapTotal:      2000000 kB\n"
        "SwapFree:       1500000 kB\n"
    )
    monkeypatch.setattr(system, "_read", lambda _p: proc_meminfo)

    result = system.memory()
    # 2 GB of 8 GB genuinely in use == 25%. Using MemFree would report 98.75%
    # and page someone every time the page cache filled.
    assert result["ram_pct"] == 25.0
    assert result["ram_total_mb"] == 8000000 // 1024
    assert result["swap_used_mb"] == 500000 // 1024


def test_disk_reports_percentages(tmp_path):
    from pbxonix_agent.collectors import system

    result = system.disk(str(tmp_path))
    assert result["disk_total_gb"] is not None
    assert 0 <= result["disk_pct"] <= 100
    assert result["inode_pct"] is None or 0 <= result["inode_pct"] <= 100


def test_collect_returns_every_expected_key(monkeypatch):
    from pbxonix_agent.collectors import system

    sampler = system.CpuSampler()
    sampler.sample()
    metrics = system.collect(sampler, "/")

    for key in (
        "cpu_pct",
        "load_1",
        "ram_pct",
        "ram_total_mb",
        "disk_pct",
        "disk_total_gb",
        "uptime_seconds",
    ):
        assert key in metrics, key


# --------------------------------------------------- heartbeat serialization --
def test_heartbeat_payload_matches_the_wire_format(monkeypatch, tmp_path):
    """The payload must serialize to exactly the documented JSON shape."""
    from pbxonix_agent import runner
    from pbxonix_agent.collectors import asterisk as asterisk_collector
    from pbxonix_agent.collectors import system as system_collector

    monkeypatch.setattr(
        system_collector,
        "collect",
        lambda _sampler, _path="/": {
            "cpu_pct": 21.4,
            "ram_pct": 44.1,
            "disk_pct": 63.8,
            "uptime_seconds": 98765,
        },
    )
    monkeypatch.setattr(
        asterisk_collector,
        "collect",
        lambda: {"running": True, "active_calls": 12, "active_channels": 24},
    )
    monkeypatch.setattr(runner.socket, "gethostname", lambda: "pbx01")

    agent = runner.Agent.__new__(runner.Agent)
    agent.cpu = system_collector.CpuSampler()
    # The payload now reports why trunks and queues are or are not flowing, so
    # it needs the config and credential that answer it.
    agent.config = load_config(write_config(tmp_path, MINIMAL))
    agent.credentials = creds.Credentials(pbx_id="p", agent_token="t", pbx_name="n")
    agent._ami_ok = None
    payload = agent._heartbeat_payload()

    assert payload["hostname"] == "pbx01"
    assert payload["asterisk_running"] is True
    assert payload["active_calls"] == 12
    assert payload["cpu"] == 21.4
    assert payload["ram"] == 44.1
    assert payload["disk"] == 63.8
    # ami_enabled is true in MINIMAL but no AMI credential is stored.
    assert payload["ami_state"] == "unconfigured"

    # Must survive a JSON round trip with no custom encoder.
    assert json.loads(json.dumps(payload))["cpu"] == 21.4


def test_asterisk_collector_reports_unknown_rather_than_stopped(monkeypatch):
    """None means 'could not tell'. Reporting False would raise a false alarm."""
    from pbxonix_agent.collectors import asterisk

    monkeypatch.setattr(asterisk.shutil, "which", lambda _name: None)
    assert asterisk.is_running() is None

    result = asterisk.collect()
    assert result["running"] is None
    assert result["active_calls"] is None
