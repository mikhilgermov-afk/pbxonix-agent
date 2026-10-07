"""HTTP client.

urllib rather than requests or httpx: the agent must install offline from a
single wheel, so it has no third-party dependencies at all.

Retries use exponential backoff with jitter. Without jitter, a fleet of agents
that all lost the cloud at the same moment would come back in lockstep and
hammer the API in synchronised waves.
"""

import json
import random
import ssl
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from pbxonix_agent import __version__

USER_AGENT = "pbxonix-agent/{}".format(__version__)

DEFAULT_TIMEOUT = 20.0
MAX_ATTEMPTS = 5
BASE_DELAY = 1.0
MAX_DELAY = 60.0


class TransportError(Exception):
    """A request failed in a way that is worth retrying."""


class RateLimited(TransportError):
    """The server is asking us to slow down.

    Deliberately a TransportError and not an ApiError. It arrives as a 4xx, but
    every other 4xx means "this will never be accepted" while 429 means "this
    will be accepted later" -- and the buffer flush discards data on the former.
    Classifying it here means any caller that only knows about TransportError
    still keeps its data.
    """

    def __init__(self, retry_after: int, message: str = "") -> None:
        super().__init__("rate limited, retry in {}s: {}".format(retry_after, message))
        self.retry_after = retry_after


class ApiError(Exception):
    """The server answered, and the answer was a refusal."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__("HTTP {}: {}".format(status, message))
        self.status = status
        self.message = message


def _retry_after(exc: urllib.error.HTTPError, *, default: int = 60) -> int:
    """Seconds to wait, from the header, clamped to something sane.

    Only the integer form is handled: it is the only form this API sends. A
    missing or unparseable value falls back rather than retrying immediately,
    because guessing low is what the header exists to prevent.
    """
    try:
        value = int((exc.headers.get("Retry-After") or "").strip())
    except (AttributeError, TypeError, ValueError):
        return default
    return min(max(value, 1), 3600)


def backoff_delay(attempt: int, *, base: float = BASE_DELAY, cap: float = MAX_DELAY) -> float:
    """Full-jitter backoff: uniform(0, min(cap, base * 2**attempt))."""
    ceiling = min(cap, base * (2**attempt))
    return random.uniform(0, ceiling)  # noqa: S311 - jitter, not cryptography


class Client:
    def __init__(
        self,
        base_url: str,
        *,
        token: Optional[str] = None,
        verify_tls: bool = True,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        if verify_tls:
            self._ssl_context = ssl.create_default_context()
        else:
            # Only ever for a lab with a private CA. Never the default.
            self._ssl_context = ssl._create_unverified_context()  # noqa: S323

    def post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - scheme is validated in config
            "{}{}".format(self.base_url, path),
            data=body,
            method="POST",
        )
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", USER_AGENT)
        if path == "/v1/agent/reports":
            request.add_header("X-PBXonix-Report-Privacy", "1")
        if self.token:
            request.add_header("Authorization", "Bearer {}".format(self.token))

        try:
            with urllib.request.urlopen(  # noqa: S310
                request, timeout=self.timeout, context=self._ssl_context
            ) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:  # noqa: S110 - the status matters, not the body
                pass
            if exc.code == 429:
                raise RateLimited(_retry_after(exc), detail) from exc
            if 500 <= exc.code < 600:
                # A server-side fault is transient by assumption; keep the data
                # buffered and try again rather than discarding it.
                raise TransportError("server error {}: {}".format(exc.code, detail)) from exc
            raise ApiError(exc.code, detail) from exc
        except (urllib.error.URLError, ssl.SSLError, OSError, ValueError) as exc:
            raise TransportError(str(exc)) from exc

    def post_with_retry(
        self, path: str, payload: Dict[str, Any], *, attempts: int = MAX_ATTEMPTS
    ) -> Dict[str, Any]:
        last: Optional[Exception] = None
        for attempt in range(attempts):
            try:
                return self.post(path, payload)
            except RateLimited:
                # Not retried here. The window is minutes and the backoff is
                # seconds, so every attempt would fail and count against us
                # again. The caller pauses for retry_after instead.
                raise
            except TransportError as exc:
                last = exc
                if attempt == attempts - 1:
                    break
                time.sleep(backoff_delay(attempt))
        raise TransportError(str(last))
