import json
from unittest.mock import Mock

import pytest

from pbxonix_agent.report_privacy import sanitize_cdr, sanitize_queue
from pbxonix_agent.reports import publish_batch

PHONE = "+79999999999"
NAME = "Private Customer Name"


def cdr():
    return dict(
        source_key="a" * 64,
        call_key="1700000000.1",
        uniqueid="1700000000.2",
        source=PHONE,
        destination=PHONE,
        channel="SIP/provider-00000001",
        destination_channel="Local/690@from-queue-00000002;1",
        context=NAME,
        application=PHONE,
        disposition="ANSWERED",
        duration_seconds=90,
        bill_seconds=60,
        started_at="2026-09-14T00:00:00Z",
        ended_at="2026-09-14T00:01:30Z",
        caller_name=NAME,
    )


def test_no_caller_identity_reaches_http_even_in_extra_fields():
    client = Mock()
    publish_batch(
        client, "cdr", [cdr()], {"kind": "cdr"}, {"extensions": ["690"], "trunks": ["provider"]}
    )
    payload = client.post_with_retry.call_args[0][1]
    serialized = json.dumps(payload)
    assert PHONE not in serialized and NAME not in serialized
    row = payload["cdr"][0]
    assert not {"source", "destination", "context", "application", "caller_name"} & set(row)
    assert row["channel"] == "Endpoint/provider"
    assert row["destination_channel"] == "Local/690@pbxonix"
    assert row["bill_seconds"] == 60 and row["source_key"] == "a" * 64


def test_unknown_channel_and_custom_call_id_are_discarded_not_customer_hashes():
    row = cdr()
    row.update(
        call_key=PHONE, uniqueid=NAME, channel="SIP/" + PHONE, destination_channel="PJSIP/" + NAME
    )
    clean = sanitize_cdr(row, set())
    assert clean["channel"] == clean["destination_channel"] == ""
    assert clean["call_key"] == clean["uniqueid"] == row["source_key"]
    assert sanitize_cdr(clean, set()) == clean


@pytest.mark.parametrize(
    "event",
    [
        "ENTERQUEUE",
        "CONNECT",
        "RINGNOANSWER",
        "COMPLETECALLER",
        "COMPLETEAGENT",
        "TRANSFER",
        "BLINDTRANSFER",
        "ATTENDEDTRANSFER",
        "EXITWITHKEY",
        "ABANDON",
    ],
)
def test_queue_free_text_and_phone_numbers_never_leave_pbx(event):
    row = dict(
        source_key="b" * 64,
        call_key="1700000000.1",
        recorded_at="2026-09-14T00:00:00Z",
        queue="support",
        agent=NAME,
        event=event,
        data1=PHONE,
        data2=PHONE,
        data3=NAME,
        data4=NAME,
        data5=NAME,
    )
    client = Mock()
    publish_batch(
        client, "queues", [row], {"kind": "queues"}, {"extensions": ["690"], "queues": ["support"]}
    )
    clean = client.post_with_retry.call_args[0][1]["queues"][0]
    assert PHONE not in json.dumps(clean) and NAME not in json.dumps(clean)
    assert clean["agent"] == "" and clean["queue"] == "support"


def test_internal_numbers_are_allowlisted_without_length_heuristics_and_times_survive():
    row = dict(
        source_key="b" * 64,
        call_key="1700000000.1",
        queue="051",
        agent="Local/646@from-queue/n",
        event="COMPLETECALLER",
        data1="12",
        data2="3600",
        data3="79999999999",
    )
    clean = sanitize_queue(row, {"646"}, {"051"})
    assert clean["queue"] == "051" and clean["agent"] == "646"
    assert clean["data1"] == "12" and clean["data2"] == "3600" and "data3" not in clean
    assert sanitize_queue(clean, {"646"}, {"051"}) == clean
