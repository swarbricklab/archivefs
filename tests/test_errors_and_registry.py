"""The error contract and the protocol registry.

Both are promises the rest of the package (and ``docs/decisions.md``) makes about
what a user sees when something goes wrong, so they are worth asserting directly
rather than only incidentally through the backends.
"""

from __future__ import annotations

import pytest

from archivefs import errors
from archivefs.registry import PROTOCOLS, Backend, installed_backends

# ------------------------------------------------------------------- errors


def test_every_error_is_catchable_as_one_family():
    for name in errors.__all__:
        cls = getattr(errors, name)
        assert issubclass(cls, errors.ArchiveFSError)


@pytest.mark.parametrize(
    ("cls", "builtin"),
    [
        (errors.ArchiveNotFoundError, FileNotFoundError),
        (errors.AuthenticationError, PermissionError),
        (errors.NotSupportedError, NotImplementedError),
        (errors.MissingExtraError, ImportError),
        (errors.MissingToolError, RuntimeError),
    ],
)
def test_errors_subclass_the_builtin_that_callers_expect(cls, builtin):
    """fsspec and DVC branch on builtins; our errors must land in the right one."""
    assert issubclass(cls, builtin)


def test_manifest_error_is_deliberately_not_a_file_not_found():
    """fsspec's walk() swallows FileNotFoundError, which would turn an unfetchable
    manifest into an empty directory (docs/decisions.md D11)."""
    assert not issubclass(errors.ManifestError, FileNotFoundError)


def test_missing_extra_names_the_exact_install():
    exc = errors.MissingExtraError("chiro", "chirograph", "reading manifests")
    message = str(exc)
    assert "pip install 'archivefs[chiro]'" in message
    assert "chirograph" in message
    assert "conda" in message  # the environment most of our users are in
    assert exc.extra == "chiro"


def test_missing_tool_says_pip_will_not_help():
    exc = errors.MissingToolError(
        "prefetch", "dbGaP needs the SRA Toolkit", "Install it from …"
    )
    assert "prefetch" in str(exc)
    assert "Install it from" in str(exc)


def test_authentication_error_requires_remediation():
    exc = errors.AuthenticationError(
        "EGA", "401", "check $EGA_PASSWORD", source="$EGA_PASSWORD"
    )
    message = str(exc)
    assert "What to do: check $EGA_PASSWORD" in message
    assert "credential came from: $EGA_PASSWORD" in message


def test_verification_error_says_do_not_use_the_bytes():
    exc = errors.VerificationError("a.bam", "md5", "aaa", "bbb")
    message = str(exc)
    assert "aaa" in message and "bbb" in message
    assert "Do not use them" in message
    assert exc.path == "a.bam"


def test_unsafe_member_error_names_both_sides():
    exc = errors.UnsafeArchiveMemberError("../escape", "/target")
    assert "../escape" in str(exc)
    assert "/target" in str(exc)
    assert "zip-slip" in str(exc)


# ----------------------------------------------------------------- registry


def test_every_protocol_is_declared_and_consistent():
    for name, backend in PROTOCOLS.items():
        assert backend.protocol == name
        assert backend.class_path.startswith("archivefs.backends.")
        assert backend.summary
        assert backend.status in ("supported", "deferred")


def test_error_text_names_the_extra_when_there_is_one():
    with_extra = Backend(
        protocol="x", class_path="archivefs.backends.x.X", extra="x", summary="s"
    )
    assert "pip install 'archivefs[x]'" in with_extra.error_text
    without = Backend(
        protocol="y", class_path="archivefs.backends.y.Y", extra=None, summary="s"
    )
    assert "archivefs.backends.y.Y" in without.error_text


def test_missing_reports_uninstalled_requirements():
    backend = Backend(
        protocol="x",
        class_path="archivefs.backends.x.X",
        extra="x",
        summary="s",
        requires=("requests", "definitely_not_installed_xyz"),
    )
    assert backend.missing() == ["definitely_not_installed_xyz"]


def test_installed_backends_reports_readiness():
    report = installed_backends()
    assert report["zenodo"]["ready"] is True
    assert report["geo"]["ready"] is True
    # deferred is never "ready", however complete the environment is
    assert report["dbgap"]["ready"] is False
    assert report["dbgap"]["status"] == "deferred"
    assert report["dbgap"]["extra"] == "dbgap"


def test_register_implementations_is_idempotent_and_carries_our_error_text():
    from fsspec.registry import known_implementations

    from archivefs.registry import register_implementations

    register_implementations()
    register_implementations()  # must not raise
    assert known_implementations["dbgap"]["class"] == PROTOCOLS["dbgap"].class_path
    assert "archivefs[dbgap]" in known_implementations["dbgap"]["err"]
