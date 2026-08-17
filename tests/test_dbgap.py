"""The deferred dbGaP stub.

Its entire job is to fail *well*, so that is what is tested: the refusal explains
that it is deferred rather than broken, points at what to use instead, and the parts
that can be designed without portal access (the NGC-file credential, the SRA Toolkit
check) are correct and ready.
"""

from __future__ import annotations

import pytest

from archivefs.backends.dbgap import REQUIRED_TOOLS, DbGaPFileSystem
from archivefs.credentials import CredentialResolver
from archivefs.errors import (
    CredentialsNotFoundError,
    MissingToolError,
    NotSupportedError,
)


def test_instantiating_refuses_with_an_explanation():
    with pytest.raises(NotSupportedError) as exc:
        DbGaPFileSystem()
    message = str(exc.value)
    assert "deferred, not missing" in message
    # the three properties that make it awkward, so the reader starts from those
    assert "NGC key file" in message
    assert "workspace" in message
    assert "not byte-preserving" in message
    # and what to do in the meantime
    assert "ega://" in message
    assert "observed-download" in message


def test_it_refuses_before_touching_credentials_or_the_network(monkeypatch):
    """Nothing to configure yet, so nothing should be demanded."""
    monkeypatch.delenv("DBGAP_NGC", raising=False)
    with pytest.raises(NotSupportedError):
        DbGaPFileSystem(ngc="/definitely/not/here.ngc")


def test_the_credential_is_a_path_and_not_a_secret():
    """The interesting half of dbGaP, and the one part designable in advance."""
    (ngc,) = DbGaPFileSystem.credentials
    assert ngc.name == "ngc"
    assert ngc.is_path is True
    # a *path* may legitimately appear in a committed .dvc; the file it points at
    # must not be committed
    assert ngc.secret is False
    assert "DBGAP_NGC" in ngc.env


def test_the_credential_resolves_like_every_other(tmp_path):
    key = tmp_path / "prj12345.ngc"
    key.write_text("not a real key")
    resolver = CredentialResolver(
        "dbGaP",
        DbGaPFileSystem.credentials,
        {},
        environ={"DBGAP_NGC": str(key)},
    )
    assert resolver.resolve("ngc").value == str(key)


def test_a_missing_ngc_file_is_reported_as_such(tmp_path):
    resolver = CredentialResolver(
        "dbGaP",
        DbGaPFileSystem.credentials,
        {},
        environ={"DBGAP_NGC": str(tmp_path / "expired.ngc")},
    )
    with pytest.raises(CredentialsNotFoundError) as exc:
        resolver.resolve("ngc")
    assert "does not exist" in str(exc.value)


def test_tool_check_reports_what_is_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    assert DbGaPFileSystem.check_tools() == list(REQUIRED_TOOLS)
    with pytest.raises(MissingToolError) as exc:
        DbGaPFileSystem.require_tools()
    message = str(exc.value)
    assert "prefetch" in message
    # pip cannot fix this, and the message says so
    assert "pip will not fix this" in message
    assert "sra-tools" in message


def test_tool_check_is_quiet_when_the_toolkit_is_present(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    assert DbGaPFileSystem.check_tools() == []
    DbGaPFileSystem.require_tools()  # must not raise


def test_it_is_registered_with_fsspec_so_the_error_is_ours():
    """An unknown protocol reads like a typo; a deferred one explains itself."""
    import fsspec

    assert fsspec.get_filesystem_class("dbgap") is DbGaPFileSystem
