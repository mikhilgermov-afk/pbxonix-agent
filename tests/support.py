"""Shared fixtures for the AMI tests.

A scripted fake socket. No Asterisk, no network -- the payloads are the real
wire format, captured from the shapes res_pjsip, chan_sip and app_queue emit.
"""

import socket

BANNER = b"Asterisk Call Manager/7.0.3\r\n"


def block(**fields: str) -> bytes:
    """Render one AMI message: Key: Value lines, blank line terminated."""
    return ("".join("{}: {}\r\n".format(k, v) for k, v in fields.items()) + "\r\n").encode()


class FakeSocket:
    """A socket that answers actions from a scripted table.

    Responses are keyed by lowercase action name and are rendered with the
    ActionID the client actually sent, which is what lets the correlation tests
    be meaningful rather than assuming order.
    """

    def __init__(self, responses, banner=BANNER, login_ok=True, noise=False):
        self._responses = responses
        self._out = bytearray(banner)
        self._sent = bytearray()
        # _sent is consumed as messages are parsed; this keeps the full record
        # so tests can assert on what actually went over the wire.
        self.raw_sent = bytearray()
        self.login_ok = login_ok
        self.noise = noise
        self.closed = False
        self.actions = []

    # -- socket surface -----------------------------------------------------
    def settimeout(self, _t):
        pass

    def sendall(self, data: bytes) -> None:
        self._sent += data
        self.raw_sent += data
        while b"\r\n\r\n" in self._sent:
            raw, _, rest = bytes(self._sent).partition(b"\r\n\r\n")
            self._sent = bytearray(rest)
            self._handle(raw.decode())

    def recv(self, _size: int) -> bytes:
        if not self._out:
            # Mirrors a peer that closed: the client must raise, not hang.
            return b""
        chunk, self._out = bytes(self._out), bytearray()
        return chunk

    def close(self) -> None:
        self.closed = True

    # -- scripted behaviour -------------------------------------------------
    def _handle(self, raw: str) -> None:
        fields = {}
        for line in raw.split("\r\n"):
            key, sep, value = line.partition(":")
            if sep:
                fields[key.strip().lower()] = value.strip()

        action = fields.get("action", "").lower()
        action_id = fields.get("actionid", "")
        self.actions.append(action)

        if action == "logoff":
            return

        if self.noise:
            # An unsolicited event with no ActionID, and a response to some
            # other action. Both must be ignored.
            self._out += block(Event="Newchannel", Channel="PJSIP/x-0001")
            self._out += block(Response="Success", ActionID="someone-else")

        if action == "login":
            if self.login_ok:
                self._out += block(
                    Response="Success", ActionID=action_id, Message="Authentication accepted"
                )
            else:
                self._out += block(
                    Response="Error", ActionID=action_id, Message="Authentication failed"
                )
            return

        script = self._responses.get(action)
        if script is None:
            self._out += block(
                Response="Error", ActionID=action_id, Message="Invalid/unknown command"
            )
            return

        self._out += block(Response="Success", ActionID=action_id, EventList="start")
        for event in script:
            self._out += block(ActionID=action_id, **event)


def connect_fake(monkeypatch, sock):
    monkeypatch.setattr(socket, "create_connection", lambda *_a, **_k: sock)
    return sock


PJSIP_SCRIPT = {
    "pjsipshowregistrationsoutbound": [
        {"Event": "OutboundRegistrationDetail", "ObjectType": "registration",
         "ObjectName": "provider1", "Status": "Registered"},
        {"Event": "OutboundRegistrationDetail", "ObjectType": "registration",
         "ObjectName": "provider2", "Status": "Rejected"},
        {"Event": "OutboundRegistrationDetail", "ObjectType": "registration",
         "ObjectName": "provider3", "Status": "Stopped"},
        {"Event": "OutboundRegistrationDetail", "ObjectType": "registration",
         "ObjectName": "provider4", "Status": "Stopping"},
        {"Event": "OutboundRegistrationDetailComplete", "EventList": "Complete"},
    ]
}


ENDPOINT_SCRIPT = {
    "pjsipshowendpoints": [
        {"Event": "EndpointList", "ObjectName": "101", "DeviceState": "Not in use"},
        {"Event": "EndpointList", "ObjectName": "102", "DeviceState": "In use"},
        {"Event": "EndpointList", "ObjectName": "103", "DeviceState": "Unavailable"},
        {"Event": "EndpointListComplete", "EventList": "Complete"},
    ],
    "sippeers": [
        {"Event": "PeerEntry", "ObjectName": "201", "Status": "OK (12 ms)"},
        {"Event": "PeerEntry", "ObjectName": "202", "Status": "UNREACHABLE"},
        {"Event": "PeerEntry", "ObjectName": "203", "Status": "Unmonitored"},
        {"Event": "PeerlistComplete", "EventList": "Complete"},
    ],
}
