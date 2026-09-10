"""`archivefs ping` — is a pin pullable now, and if not, whose fault?

The verdict taxonomy is the point, so most of this pins the exception → verdict mapping.
The end-to-end paths run against a fake filesystem (no network) whose `info`/`cat_file`
raise the failure being classified.
"""

from __future__ import annotations

import fsspec
import pytest
import requests

from archivefs import ping as ping_mod
from archivefs.errors import (
    ArchiveNotFoundError,
    AuthenticationError,
    ManifestError,
    MissingExtraError,
)
from archivefs.ping import (
    ARCHIVE_UNAVAILABLE,
    AUTH,
    GONE,
    NETWORK,
    PULLABLE,
    SETUP,
    classify,
    ping,
)


def _http_error(status: int) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = status
    return requests.HTTPError(f"{status} Server Error", response=resp)


def _wrapped(cause: Exception) -> OSError:
    """An OSError chained from a RequestException, as base._request raises it."""
    try:
        raise cause
    except Exception as exc:  # noqa: BLE001
        try:
            raise OSError("request failed after 5 attempts: ...") from exc
        except OSError as wrapped:
            return wrapped


# --------------------------------------------------------------- taxonomy


@pytest.mark.parametrize(
    "exc, verdict",
    [
        (ArchiveNotFoundError("no such record"), GONE),
        (AuthenticationError("ega", "HTTP 403", "get a DAC"), AUTH),
        (MissingExtraError("chiro", "chirograph", "reading manifests"), SETUP),
        (ManifestError("no manifest in store"), SETUP),
        (_http_error(504), ARCHIVE_UNAVAILABLE),
        (_http_error(503), ARCHIVE_UNAVAILABLE),
        (_wrapped(requests.exceptions.ConnectionError("no route")), NETWORK),
        (_wrapped(requests.exceptions.ReadTimeout("slow")), ARCHIVE_UNAVAILABLE),
    ],
)
def test_classify(exc, verdict):
    assert classify(exc)[0] == verdict


# ---------------------------------------------------------- end-to-end


class _FakeFS:
    """Minimal fs: info()/cat_file() either succeed or raise the configured error."""

    def __init__(self, *, info_exc=None, cat_exc=None):
        self._info_exc = info_exc
        self._cat_exc = cat_exc

    def info(self, path):
        if self._info_exc and "/" not in path.split("://")[-1]:
            raise self._info_exc  # the record-level metadata probe
        return {"type": "file" if "/" in path else "directory"}

    def find(self, path):
        return [f"{path}/only.tif"]

    def cat_file(self, path, start=0, end=1):
        if self._cat_exc:
            raise self._cat_exc
        return b"x"


@pytest.fixture
def fake_backend(monkeypatch):
    """Install a fake fs so ping never touches the network."""

    holder = {}

    def factory(protocol, **kwargs):
        return holder["fs"]

    monkeypatch.setattr(fsspec, "filesystem", factory)
    return holder


def test_pullable(fake_backend):
    fake_backend["fs"] = _FakeFS()
    result = ping("bia://S-BIAD1/images/ihc")
    assert result.verdict == PULLABLE
    assert result.ok
    assert result.archive == "bia"


def test_metadata_transient(fake_backend):
    fake_backend["fs"] = _FakeFS(info_exc=_http_error(504))
    result = ping("zenodo://123")
    assert result.verdict == ARCHIVE_UNAVAILABLE
    assert result.transient


def test_gone(fake_backend):
    fake_backend["fs"] = _FakeFS(info_exc=ArchiveNotFoundError("no such record"))
    result = ping("zenodo://123")
    assert result.verdict == GONE
    assert not result.transient


def test_content_endpoint_fails(fake_backend):
    # metadata ok, but the content endpoint is down — still a transient verdict
    fake_backend["fs"] = _FakeFS(cat_exc=_http_error(502))
    result = ping("zenodo://123")
    assert result.verdict == ARCHIVE_UNAVAILABLE


def test_retries_a_transient_then_gives_up(fake_backend, monkeypatch):
    monkeypatch.setattr(ping_mod.time, "sleep", lambda _s: None)
    fake_backend["fs"] = _FakeFS(info_exc=_http_error(504))
    steps = []
    ping("zenodo://123", retries=3, on_step=lambda kind, msg: steps.append(kind))
    assert steps.count("wait") == 2  # 3 attempts → 2 waits between them


# --------------------------------------------------------------- CLI


def test_cli_exit_codes(fake_backend, capsys):
    from archivefs.cli import main

    fake_backend["fs"] = _FakeFS()
    assert main(["ping", "zenodo://123"]) == 0

    fake_backend["fs"] = _FakeFS(info_exc=_http_error(504))
    assert main(["ping", "zenodo://123"]) == 1  # transient
    err = capsys.readouterr().err
    assert "--retries" in err  # suggests retrying

    fake_backend["fs"] = _FakeFS(info_exc=ArchiveNotFoundError("gone"))
    assert main(["ping", "zenodo://123"]) == 2  # real problem


def test_cli_unresolvable_target(capsys):
    from archivefs.cli import main

    assert main(["ping", "not-a-url"]) == 2
    assert "SETUP" in capsys.readouterr().err
