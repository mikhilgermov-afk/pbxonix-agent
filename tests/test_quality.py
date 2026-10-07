import json
from datetime import datetime, timezone

import pytest

from pbxonix_agent import quality_clock_helper
from pbxonix_agent.quality import Accumulator, codec_clock


def event(**changes):
    data = dict(
        event="RTCPReceived",
        channel="PJSIP/carrier-00000001",
        reportcount="1",
        report0fractionlost="8",
        report0iajitter="240",
        report0lsr="1",
        rtt="0.12",
        calleridnum="+79999999999",
        connectedlinename="PRIVATE PERSON",
        **{},
    )
    data.update(changes)
    return data


def rows(acc):
    return acc.drain(datetime.now(timezone.utc), "listening")["rows"]


def test_rtcp_units_and_directions_and_no_caller_id():
    acc = Accumulator(lambda: {"trunks": ["carrier"]}, lambda ch: 8000)
    acc.on_event(event())
    acc.on_event(event(event="RTCPSent", report0fractionlost="0"))
    data = rows(acc)
    outbound, inbound = data
    assert outbound["loss"]["total"] == 3.125
    assert outbound["jitter"]["total"] == 30
    assert outbound["rtt"]["total"] == 120
    assert inbound["rtt"] == dict(count=0, total=0, peak=None)
    assert inbound["direction"] == "inbound"
    assert "79999999999" not in json.dumps(data) and "PRIVATE" not in json.dumps(data)
    assert "channel" not in json.dumps(data)


def test_unknown_endpoints_clocks_and_unacknowledged_rtt_are_not_invented():
    acc = Accumulator(lambda: {})
    acc.on_event(event(channel="SIP/+79999999999-00000001", report0lsr="0", rtt="0"))
    row = rows(acc)[0]
    assert row["subject"] == "" and row["subject_kind"] == "unmapped"
    assert row["jitter"]["count"] == row["rtt"]["count"] == 0
    assert row["jitter"]["peak"] is None


@pytest.mark.parametrize(
    "formats,expected",
    [
        ("ulaw", 8000),
        ("alaw|ulaw", 8000),
        ("g722", 8000),
        ("opus", 48000),
        ("opus|ulaw", None),
        ("h264|ulaw", None),
        ("unknown", None),
    ],
)
def test_rtp_clock_not_audio_sample_rate(formats, expected):
    assert codec_clock("  NativeFormats: ({})\n ReadFormat: slin\n".format(formats)) == expected


def test_invalid_fields_do_not_become_zeros_and_no_report_blocks_are_not_samples():
    acc = Accumulator(lambda: {})
    acc.on_event(event(reportcount="0"))
    assert rows(acc) == []
    acc.on_event(event(report0fractionlost="nan", report0iajitter="-1", rtt="inf"))
    assert rows(acc) == []
    acc.on_event(event(channel="PJSIP/bad\nAction: Originate-00000001"))
    assert rows(acc) == []


def test_average_is_sum_and_count_and_buffer_is_sanitized():
    acc = Accumulator(lambda: {"trunks": ["carrier"]}, lambda ch: 48000)
    acc.on_event(event(report0fractionlost="0", report0iajitter="480"))
    acc.on_event(event(report0fractionlost="128", report0iajitter="960"))
    row = rows(acc)[0]
    assert row["reports"] == 2
    assert row["loss"] == dict(count=2, total=50, peak=50)
    assert row["jitter"] == dict(count=2, total=30, peak=20)
    assert rows(acc) == []


@pytest.mark.parametrize(
    "channel",
    [
        "SIP/a-00000001\nAction: Originate",
        "SIP/a; core stop now-00000001",
        "../../etc/shadow",
        "SIP/a-00000001\r",
        "SIP/a-00000001 foo",
        "PJSIP/" + "a" * 300,
    ],
)
def test_restricted_clock_helper_rejects_commands_before_spawning(channel, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("must not spawn a process for invalid input")

    monkeypatch.setattr(quality_clock_helper.subprocess, "run", forbidden)
    assert quality_clock_helper.read_clock(channel) is None


def test_restricted_helper_only_returns_clock_not_channel_details(monkeypatch):
    class Result:
        returncode = 0
        stdout = " Caller ID: +79999999999\n NativeFormats: (g722)\n ReadFormat: slin16\n"

    monkeypatch.setattr(quality_clock_helper.os.path, "isfile", lambda path: True)
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(quality_clock_helper.subprocess, "run", run)
    assert quality_clock_helper.read_clock("SIP/carrier-00000001") == 8000
    assert calls == [["/usr/sbin/asterisk", "-rx", "core show channel SIP/carrier-00000001"]]
