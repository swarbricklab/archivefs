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


# ------------------------------------------------------- the archive profiles
#
# A profile is only worth having if the rules it encodes are enforced somewhere.
# These are those rules. Each one exists because the fact used to live in a
# different file from the code that depended on it.


def test_no_credential_name_is_a_config_key():
    """The credentials-never-in-a-committed-file invariant, as an assertion.

    `--fs-config password=…` is written verbatim into a .dvc file's
    `deps[].fs_config`, so the *only* thing stopping a secret being committed is
    that DVC's schema — built from `Backend.config` — does not contain the key.
    That used to rest on a comment explaining which names to leave out. Now
    adding one to a profile fails here, and the two sources are compared rather
    than kept in agreement by hand.

    `credentials_file` and dbGaP's `ngc` are paths, not secrets; a path may
    appear in a committed file, and selecting an account per repo is the
    supported ssh-`IdentityFile` pattern. So the check is on secrecy, not on
    being credential-adjacent.
    """
    from fsspec.registry import _import_class

    for name, backend in PROTOCOLS.items():
        cls = _import_class(backend.class_path)
        secrets = {c.name for c in cls.credentials if c.secret}
        assert not secrets & set(backend.config), (
            f"{name}: secret credential(s) {sorted(secrets & set(backend.config))} "
            f"are accepted as storage options and could be committed"
        )


def test_passthrough_info_keys_cover_every_backend_metadata_block():
    """Each backend nests provenance under a block named for its protocol.

    `chiro.py` carries those blocks across a manifest resolution, and it did so
    from a hardcoded tuple that had fallen behind: `bia` was missing, so a
    `bia://` import with a `.chiro` manifest silently dropped the BIA provenance
    block from `info()`. Reads still worked — `url` was in the tuple — but
    `archivefs stat` and `ls --json` lost the accession, view and S3 key.

    Deriving the set from PROTOCOLS makes that class of omission impossible.
    """
    from archivefs.registry import PASSTHROUGH_INFO_KEYS

    assert set(PROTOCOLS) <= PASSTHROUGH_INFO_KEYS
    # plus the two generic keys, which belong to no single archive
    assert {"url", "archive_version"} <= PASSTHROUGH_INFO_KEYS


def test_no_archive_claims_an_immutability_basis_it_cannot_back():
    """`immutability_basis` is the field whose wrong value fails *silently*.

    Once DVC has a version-aware FileStorage remote, `_log_unversioned` drops
    every entry with no `version_id` from the fetch and only warns. So a profile
    must not claim a token unless the archive publishes something immutable about
    the bytes to build it from — and an archive that is *mixed* across views (BIA:
    md5s for derived objects, none for submitted ones) has to record the value
    that is safe for the weaker view.
    """
    for name, backend in PROTOCOLS.items():
        if backend.immutability_basis is not None:
            assert backend.published_checksum is not None, (
                f"{name} claims an immutable per-entry token but publishes no "
                f"checksum to build one from"
            )


def test_concept_schemes_are_unique_so_the_reverse_map_is_unambiguous():
    """`protocol_for_concept` relies on each scheme belonging to exactly one backend.

    A `chiro://` pin reads a manifest's concept CURIE to learn which backend
    re-fetches its bytes; if two archives shared a scheme, that routing would be a
    coin toss. The deferred dbGaP backend has no scheme, which is correct.
    """
    schemes = [b.concept_scheme for b in PROTOCOLS.values() if b.concept_scheme]
    assert len(schemes) == len(set(schemes)), schemes
    assert PROTOCOLS["dbgap"].concept_scheme is None


def test_protocol_for_concept_round_trips_each_scheme():
    from archivefs.registry import protocol_for_concept

    # A DOI keeps its own colons — the split is on the *first* colon only.
    assert protocol_for_concept("doi:10.5281/zenodo.123") == (
        "zenodo",
        "10.5281/zenodo.123",
    )
    assert protocol_for_concept("geo:GSE176078") == ("geo", "GSE176078")
    assert protocol_for_concept("biostudies:S-BIAD3080") == ("bia", "S-BIAD3080")
    assert protocol_for_concept("EGA:EGAD00001006673") == ("ega", "EGAD00001006673")
    # An unknown scheme, or a bare word with no identifier, does not route.
    assert protocol_for_concept("nonsense:x") is None
    assert protocol_for_concept("geo") is None


def test_a_deferred_backend_advertises_nothing():
    """dbGaP is not implemented; every profile field must say so, not just `status`."""
    dbgap = PROTOCOLS["dbgap"]
    assert dbgap.status == "deferred"
    assert dbgap.dvc_wrapper is None  # so DVC is never told it exists
    assert dbgap.config == {}
    assert dbgap.immutability_basis is None


def test_installed_backends_reports_capabilities_not_just_readiness():
    report = installed_backends()
    assert report["zenodo"]["capabilities"]["expands_containers"] is True
    assert report["zenodo"]["capabilities"]["published_checksum"] == "md5"
    assert "extract_zips" in report["zenodo"]["capabilities"]["options"]
    # GEO publishes no digest at all (D8), which is why a manifest is load-bearing
    assert report["geo"]["capabilities"]["published_checksum"] is None
    assert report["geo"]["capabilities"]["immutability_basis"] is None


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
