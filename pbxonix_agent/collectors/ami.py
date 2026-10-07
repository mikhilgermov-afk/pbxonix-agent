"""Asterisk Manager Interface client.

A socket and a line parser, because the agent ships with no third-party
dependencies. AMI is a simple protocol and the surface we need is small: log in,
issue a handful of list actions, log out.

Inventory connections are opened per collection cycle. The panel can also use
a persistent session with an event handler and reconnect with backoff.

By default the session logs in with ``Events: off``, receiving only direct
responses to its own actions. That makes parsing deterministic and means the
manager user needs no event permissions at all.
"""

import itertools
import select
import socket
import time
from typing import Callable, Dict, Iterable, List, Optional, Set

DEFAULT_TIMEOUT = 10.0
_RECV_SIZE = 65536
# One list action returning more than this is a malformed or hostile response;
# reading it into memory on a PBX is not worth the risk.
_MAX_LIST_ITEMS = 5000


class AMIError(Exception):
    """Any AMI failure. The caller treats every one of them the same way:
    report nothing rather than report something wrong."""


class AMIAuthError(AMIError):
    pass


class AMIClient:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5038,
        username: str = "",
        secret: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        events: str = "off",
        event_handler: Optional[Callable[[Dict[str, str]], None]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.secret = secret
        self.timeout = timeout
        self.events = events
        self.event_handler = event_handler
        self.banner = ""
        self._sock: Optional[socket.socket] = None
        self._buf = b""
        self._ids = itertools.count(1)

    # ------------------------------------------------------------ lifecycle --
    def connect(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), self.timeout)
        except OSError as exc:
            raise AMIError(f"cannot reach AMI at {self.host}:{self.port}: {exc}") from exc
        self._sock.settimeout(self.timeout)
        self._buf = b""
        self.banner = self._read_line()
        if "Asterisk Call Manager" not in self.banner:
            raise AMIError("unexpected banner: {!r}".format(self.banner[:80]))

    def login(self) -> None:
        response = self.action(
            "Login", Username=self.username, Secret=self.secret, Events=self.events
        )
        if response.get("response", "").lower() != "success":
            # Never echo the secret, and never echo Asterisk's message verbatim
            # in case it contains the username.
            raise AMIAuthError("AMI authentication was refused")

    def close(self) -> None:
        if self._sock is None:
            return
        try:
            self._send({"Action": "Logoff"})
        except (AMIError, OSError):
            # _send wraps broken pipes in AMIError. Cleanup must still close
            # the socket so the caller can reconnect after an AMI restart.
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._sock = None

    def __enter__(self) -> "AMIClient":
        self.connect()
        self.login()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -------------------------------------------------------------- protocol --
    def _fill(self) -> None:
        assert self._sock is not None
        try:
            chunk = self._sock.recv(_RECV_SIZE)
        except socket.timeout as exc:
            raise AMIError("timed out reading from AMI") from exc
        except OSError as exc:
            raise AMIError("AMI read failed: {}".format(exc)) from exc
        if not chunk:
            raise AMIError("AMI closed the connection")
        self._buf += chunk

    def _read_line(self) -> str:
        while b"\r\n" not in self._buf:
            self._fill()
        line, _, self._buf = self._buf.partition(b"\r\n")
        return line.decode("utf-8", "replace")

    def _read_block(self) -> Dict[str, str]:
        """Read one message: Key: Value lines terminated by a blank line."""
        while b"\r\n\r\n" not in self._buf:
            self._fill()
        raw, _, self._buf = self._buf.partition(b"\r\n\r\n")

        block: Dict[str, str] = {}
        for line in raw.split(b"\r\n"):
            if not line:
                continue
            key, sep, value = line.decode("utf-8", "replace").partition(":")
            if not sep:
                continue
            # Keys are lowercased because Asterisk's capitalisation varies
            # between versions and channel drivers.
            block[key.strip().lower()] = value.strip()
        return block

    def _send(self, fields: Dict[str, str]) -> None:
        if self._sock is None:
            raise AMIError("not connected")
        payload = "".join("{}: {}\r\n".format(k, v) for k, v in fields.items()) + "\r\n"
        try:
            self._sock.sendall(payload.encode("utf-8"))
        except OSError as exc:
            raise AMIError("AMI write failed: {}".format(exc)) from exc

    def _next_id(self) -> str:
        return "pbxonix-{}".format(next(self._ids))

    # --------------------------------------------------------------- actions --
    def pump_events(self, timeout=0.5, maximum=500):
        """Drain a bounded number of events on a dedicated reporting session."""
        if self._sock is None:
            raise AMIError("not connected")
        for index in range(maximum):
            if b"\r\n\r\n" not in self._buf:
                ready, _, _ = select.select([self._sock], [], [], timeout if index == 0 else 0)
                if not ready:
                    return
            block = self._read_block()
            if self.event_handler and block.get("event"):
                self.event_handler(block)

    def action(self, name: str, **fields: str) -> Dict[str, str]:
        action_id = self._next_id()
        self._send(dict({"Action": name, "ActionID": action_id}, **fields))
        return self._await_response(action_id)

    def _await_response(self, action_id: str) -> Dict[str, str]:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            block = self._read_block()
            if block.get("actionid") == action_id and "response" in block:
                return block
            if self.event_handler and block.get("event"):
                self.event_handler(block)
            # Anything else is either a stray event or a response to an action
            # we are no longer waiting on. Correlating by ActionID rather than
            # assuming order is what makes that safe.
        raise AMIError("timed out waiting for a response to {}".format(action_id))

    def action_list(
        self, name: str, complete_events: Iterable[str], **fields: str
    ) -> List[Dict[str, str]]:
        """Issue a list action and collect its items.

        ``complete_events`` are the lowercase Event names that terminate the
        list; they differ per action and per Asterisk version, so callers pass
        every spelling they know about.
        """
        terminators: Set[str] = {e.lower() for e in complete_events}
        action_id = self._next_id()
        self._send(dict({"Action": name, "ActionID": action_id}, **fields))

        response = self._await_response(action_id)
        if response.get("response", "").lower() != "success":
            raise AMIError("{} failed: {}".format(name, response.get("message", "no reason given")))

        items: List[Dict[str, str]] = []
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            block = self._read_block()
            if block.get("actionid") != action_id:
                if self.event_handler and block.get("event"):
                    self.event_handler(block)
                continue
            event = block.get("event", "").lower()
            if event in terminators:
                return items
            if not event:
                continue
            items.append(block)
            if len(items) >= _MAX_LIST_ITEMS:
                raise AMIError("{} returned more than {} items".format(name, _MAX_LIST_ITEMS))

        raise AMIError("timed out collecting results for {}".format(name))
