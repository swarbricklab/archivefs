"""A minimal stand-in for ``requests``, so backends can be tested offline.

Deliberately hand-rolled rather than pulled from a mocking library: the backends
touch a tiny slice of the requests API (``Session.request``, ``Session.post``,
``iter_content``, ``raise_for_status``), and a fake that small is easier to read
than a recording, while letting a test express things a recording cannot — a 401
mid-transfer, a short body on a ranged read, a flaky endpoint that recovers.

Response bodies used by the EGA/Zenodo/GEO fakes are trimmed copies of *real*
API replies (``tests/data/``), so field names and quirks are the archive's, not
an invention of the test.
"""

from __future__ import annotations

import json as jsonlib
import re
from typing import Any, Callable


class FakeRequestException(Exception):
    """Stands in for ``requests.RequestException``."""


class FakeHTTPError(FakeRequestException):
    def __init__(self, message: str, response: FakeResponse) -> None:
        super().__init__(message)
        self.response = response


class FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        content: bytes = b"",
        headers: dict | None = None,
        json: Any = None,
        *,
        truncate_to: int | None = None,
    ) -> None:
        self.status_code = status_code
        if json is not None:
            content = jsonlib.dumps(json).encode()
            headers = {"Content-Type": "application/json", **(headers or {})}
        self._content = content
        self.headers = headers or {}
        #: emulate a server that says 206 but sends fewer bytes than promised
        self._truncate_to = truncate_to
        self.closed = False

    @property
    def content(self) -> bytes:
        body = self._content
        if self._truncate_to is not None:
            body = body[: self._truncate_to]
        return body

    @property
    def text(self) -> str:
        return self.content.decode()

    def json(self) -> Any:
        return jsonlib.loads(self.content)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise FakeHTTPError(f"HTTP {self.status_code}", self)

    def iter_content(self, chunk_size: int = 8192):
        body = self.content
        for i in range(0, len(body), chunk_size):
            yield body[i : i + chunk_size]

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


Handler = Callable[[str, str, dict, dict], FakeResponse]


class FakeSession:
    """Routes calls to a handler and records everything it was asked."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.calls: list[dict] = []
        #: session-level default headers, merged into every request like requests does
        #: (so a backend that sets, e.g., a User-Agent on the session is observable).
        self.headers: dict = {}

    def request(
        self,
        method: str,
        url: str,
        headers: dict | None = None,
        stream: bool = False,
        timeout: int | None = None,
        **kwargs: Any,
    ) -> FakeResponse:
        merged = {**self.headers, **(headers or {})}
        record = {
            "method": method,
            "url": url,
            "headers": dict(merged),
            "stream": stream,
            **kwargs,
        }
        self.calls.append(record)
        return self.handler(method, url, dict(merged), kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("POST", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.request("HEAD", url, **kwargs)

    def mount(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover
        pass


class FakeRequestsModule:
    """Just enough of the ``requests`` module surface for the backends."""

    RequestException = FakeRequestException
    HTTPError = FakeHTTPError
    Response = FakeResponse

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.sessions: list[FakeSession] = []

    def Session(self) -> FakeSession:
        session = FakeSession(self.handler)
        self.sessions.append(session)
        return session

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.Session().get(url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self.Session().post(url, **kwargs)

    @property
    def calls(self) -> list[dict]:
        return [call for session in self.sessions for call in session.calls]


RANGE_RE = re.compile(r"bytes=(\d+)-(\d*)")


def parse_range(headers: dict) -> tuple[int, int | None]:
    """Return ``(start, end_inclusive_or_None)`` from a Range header."""
    match = RANGE_RE.search(headers.get("Range", ""))
    if not match:
        return 0, None
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else None
    return start, end


def ranged(body: bytes, headers: dict, **kwargs: Any) -> FakeResponse:
    """Serve a byte range out of ``body`` the way a real archive does."""
    start, end = parse_range(headers)
    if "Range" not in headers:
        return FakeResponse(
            200,
            body,
            {"Content-Length": str(len(body)), "Accept-Ranges": "bytes"},
            **kwargs,
        )
    stop = len(body) - 1 if end is None else min(end, len(body) - 1)
    chunk = body[start : stop + 1]
    return FakeResponse(
        206,
        chunk,
        {
            "Content-Range": f"bytes {start}-{stop}/{len(body)}",
            "Content-Length": str(len(chunk)),
            "Accept-Ranges": "bytes",
        },
        **kwargs,
    )
