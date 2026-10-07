"""Report wire contract: technical statistics only, never caller identities.

Keep this dependency-free module identical to the cloud copy. Unknown endpoint
and queue-agent strings are discarded, not hashed into customer identifiers.
"""

import re

CALL_ID = re.compile(
    r"^(?:[0-9]{9,13}\.[0-9]{1,20}|[a-f0-9]{32}|[a-f0-9]{64}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$"
)
DISPOSITIONS = {"ANSWERED", "BUSY", "NO ANSWER", "NO_ANSWER", "FAILED", "CONGESTION", "CANCEL"}
CDR_FIELDS = {
    "source_key",
    "call_key",
    "uniqueid",
    "started_at",
    "answered_at",
    "ended_at",
    "channel",
    "destination_channel",
    "disposition",
    "duration_seconds",
    "bill_seconds",
}
QUEUE_FIELDS = {"source_key", "call_key", "recorded_at", "queue", "agent", "event"}
QUEUE_NUMBERS = {
    "CONNECT": {"data1"},
    "COMPLETEAGENT": {"data1", "data2"},
    "COMPLETECALLER": {"data1", "data2"},
    "TRANSFER": {"data3", "data4"},
    "BLINDTRANSFER": {"data3", "data4"},
    "ATTENDEDTRANSFER": {"data3", "data4"},
    "ABANDON": {"data3"},
    "EXITWITHTIMEOUT": {"data3"},
    "EXITEMPTY": {"data3"},
    "EXITWITHKEY": {"data4"},
}


def endpoint(value):
    if not value or "/" not in value:
        return ""
    technology, name = value.split("/", 1)
    if technology.lower() == "local":
        return name.split("@", 1)[0]
    if technology == "Endpoint":
        return name
    return re.sub(r"-[0-9a-fA-F]{6,}(?:;[12])?$", "", name).split(";", 1)[0]


def call_id(value, fallback):
    # Asterisk's timestamp.sequence is a per-call technical ID. A custom ID
    # may embed CallerID; retain only a per-record digest in that case.
    return value if CALL_ID.fullmatch(value or "") else fallback


def sanitize_cdr(row, endpoints):
    clean = {key: value for key, value in row.items() if key in CDR_FIELDS}
    for key in ["call_key", "uniqueid"]:
        clean[key] = call_id(row.get(key), row["source_key"])
    for key in ["channel", "destination_channel"]:
        raw = row.get(key, "")
        name = endpoint(raw)
        clean[key] = (
            (
                "Local/" + name + "@pbxonix"
                if raw.lower().startswith("local/")
                else "Endpoint/" + name
            )
            if name and name in endpoints
            else ""
        )
    value = row.get("disposition", "").upper()
    clean["disposition"] = value if value in DISPOSITIONS else "UNKNOWN"
    return clean


def sanitize_queue(row, extensions, queues):
    clean = {key: value for key, value in row.items() if key in QUEUE_FIELDS}
    clean["call_key"] = call_id(row.get("call_key"), row["source_key"])
    clean["event"] = (
        row["event"]
        if row.get("event") in set(QUEUE_NUMBERS) | {"ENTERQUEUE", "RINGNOANSWER"}
        else "UNKNOWN"
    )
    clean["queue"] = row["queue"] if row["queue"] in queues else "unmapped"
    agent = endpoint(row.get("agent", "")) or row.get("agent", "")
    clean["agent"] = agent if agent in extensions else ""
    for key in QUEUE_NUMBERS.get(row.get("event"), set()):
        value = row.get(key, "")
        if re.fullmatch(r"[0-9]{1,7}", value) and int(value) <= 2_678_400:
            clean[key] = str(int(value))
    return clean
