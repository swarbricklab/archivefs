"""Protocol registration, the "install the extra" contract, and archive profiles.

fsspec discovers our protocols through the ``fsspec.specs`` entry points in
``pyproject.toml``, so ``fsspec.filesystem("zenodo")`` works with no imports from
this package. What entry points *cannot* give us is a good message when a
per-backend extra is missing — fsspec substitutes its own generic "Unable to load
filesystem from ...". Two things follow, and they are the reason this module
exists:

1. Backend modules must import cleanly **without** their extras; a missing
   optional dependency is raised as :class:`~archivefs.errors.MissingExtraError`
   when the filesystem is *instantiated*.
2. :func:`register_implementations` re-registers the same protocols with our own
   error text for anyone who has imported ``archivefs`` directly, and covers
   installs where entry-point discovery did not happen.

:class:`Backend` is also each archive's **capability profile**. It began as
packaging facts only, and the capability facts grew up scattered — four parallel
protocol-keyed dicts in ``dvcfs.py``, a hardcoded key tuple in ``chiro.py``, tables
maintained by hand in ``docs/``. Adding an archive meant touching all of them and
missing one failed in a way that did not point at what you forgot. Now those tables
are *derived* from here.

Two rules keep this from becoming a junk drawer:

* **A profile declares *what*, never *how*.** Behaviour stays in the fsspec
  subclass. If a field would hold a callable, it belongs on the class instead.
* **This module must stay importable with no backend dependency installed**, because
  ``archivefs info`` has to report a missing extra *without* importing the backend
  that needs it. So every field is inert data — no imports from
  ``archivefs.backends`` here, ever.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

__all__ = [
    "PASSTHROUGH_INFO_KEYS",
    "PROTOCOLS",
    "Backend",
    "installed_backends",
    "register_implementations",
]


@dataclass(frozen=True)
class Backend:
    """One archive's packaging facts and capability profile."""

    protocol: str
    class_path: str
    extra: str | None
    summary: str
    #: Import names that must be importable for the backend to work at all.
    requires: tuple[str, ...] = ()
    #: External binaries the backend shells out to, if any.
    requires_tools: tuple[str, ...] = ()
    status: str = "supported"

    # ----------------------------------------------------------- capabilities

    #: Non-secret storage options this archive accepts, mapped to the type each
    #: must be coerced to. **The type is load-bearing**: a DVC config file is INI,
    #: so every value arrives as a string, and registering ``extract_zips`` as an
    #: untyped key means ``extract_zips = false`` reaches the filesystem as the
    #: string ``"false"`` — truthy — presenting ZIPs as directories against the
    #: user's explicit instruction.
    #:
    #: The omissions are load-bearing too. DVC validates remote config and
    #: ``--fs-config`` against exactly this table, so a key that is absent here is
    #: refused by DVC itself before any archivefs code runs::
    #:
    #:     $ dvc remote modify ega password hunter2
    #:     ERROR: configuration error - config file error:
    #:            extra keys not allowed @ data['remote']['ega']['password']
    #:
    #: That is why no credential name appears in any profile's ``config``: with
    #: them absent, committing a secret is a config parse error rather than a
    #: convention we ask users to respect. ``--fs-config password=…`` would be
    #: written verbatim into a ``.dvc`` file's ``deps[].fs_config``.
    #: ``credentials_file`` is allowed because it is a *path* — the ssh
    #: ``IdentityFile`` analogue. ``test_no_credential_name_is_a_config_key``
    #: enforces the rule against the backends' own ``Credential`` declarations.
    config: Mapping[str, type] = field(default_factory=dict)

    #: Class name of this archive's DVC ``FileSystem`` wrapper in
    #: ``archivefs.dvcfs``, or ``None`` to register nothing with DVC. dbGaP is
    #: ``None`` on purpose: it is deferred, and registering it would advertise a
    #: backend that refuses.
    dvc_wrapper: str | None = None

    #: The digest the archive publishes per file **without a download**, or
    #: ``None``. ``None`` means a ``.chiro`` manifest is the only way a consumer
    #: gets a checksum, which is what makes one load-bearing rather than a nicety.
    published_checksum: str | None = None

    #: What an immutable per-entry token can be built from, in one line, or
    #: ``None`` if this archive cannot produce one for every entry.
    #:
    #: This gates DVC's ``version_aware``, and it is the most dangerous field
    #: here: once a version-aware ``FileStorage`` remote is in play,
    #: ``dvc/repo/fetch.py``'s ``_log_unversioned`` **silently drops** every entry
    #: whose ``Meta`` carries no ``version_id``, with a warning and no error. So
    #: ``None`` must mean *never enable*, not *warn and continue*.
    #:
    #: Read it as a **permission, not a promise**. An archive whose views differ
    #: (BIA publishes checksums for derived files and none for submitted ones)
    #: stays ``None`` here, and the runtime must still hard-error on an entry that
    #: turns out to lack a token rather than trust this field.
    immutability_basis: str | None = None

    #: Does this archive present containers (a ZIP) as directories?
    expands_containers: bool = False

    @property
    def error_text(self) -> str:
        if self.extra:
            return (
                f"the {self.protocol}:// backend needs archivefs[{self.extra}]: "
                f"pip install 'archivefs[{self.extra}]'"
            )
        return f"could not load the {self.protocol}:// backend from {self.class_path}"

    def missing(self) -> list[str]:
        """Which of this backend's requirements are not importable."""
        import importlib.util

        return [
            name for name in self.requires if importlib.util.find_spec(name) is None
        ]


PROTOCOLS: dict[str, Backend] = {
    "ega": Backend(
        protocol="ega",
        class_path="archivefs.backends.ega.EGAFileSystem",
        # No extra: the backend speaks EGA's REST API over `requests`, which is
        # a base dependency. The [ega] extra only adds pyega3 for the optional
        # delegated transfer path. See docs/decisions.md D2.
        extra=None,
        summary=(
            "European Genome-phenome Archive "
            "(controlled access; needs credentials)"
        ),
        requires=("requests",),
        config={
            "credentials_file": str,
            "layout": str,
            "chiro": str,
            "api_url": str,
        },
        dvc_wrapper="EGADVCFileSystem",
        # `plainChecksum` is the md5 of the *decrypted* bytes, which is what lands
        # on disk — established against the live API in D1, including the
        # interrupted-and-resumed case.
        published_checksum="md5",
        immutability_basis="plainChecksum md5 of the decrypted bytes",
    ),
    "zenodo": Backend(
        protocol="zenodo",
        class_path="archivefs.backends.zenodo.ZenodoFileSystem",
        extra=None,
        summary="Zenodo records by id or DOI (public; token only for restricted)",
        requires=("requests",),
        config={
            "extract_zips": bool,
            "strip_components": int,
            "api_url": str,
            "chiro": str,
        },
        dvc_wrapper="ZenodoDVCFileSystem",
        published_checksum="md5",
        # A published record is immutable, so the file md5 identifies the entry.
        # A ZIP member has no md5 (D9) but the central directory gives a CRC32 for
        # free, and the container's own md5 authenticates every member extracted
        # from it — so the pair names exactly one member of exactly one record.
        immutability_basis="file md5, or (container md5, member CRC32) inside a ZIP",
        expands_containers=True,
    ),
    "geo": Backend(
        protocol="geo",
        class_path="archivefs.backends.geo.GEOFileSystem",
        extra=None,
        summary="GEO supplementary/processed files over public HTTPS (anonymous)",
        requires=("requests",),
        config={"view": str, "base_url": str, "chiro": str},
        dvc_wrapper="GEODVCFileSystem",
        # GEO publishes no per-file digest at all (D8), so `info()["md5"]` is None
        # with a `checksum_source` saying why rather than an ETag standing in.
        published_checksum=None,
        immutability_basis=None,
    ),
    "bia": Backend(
        protocol="bia",
        class_path="archivefs.backends.bia.BIAFileSystem",
        extra=None,
        summary=(
            "BioImage Archive via BioStudies (open; submitted files + OME-Zarr "
            "conversions)"
        ),
        requires=("requests",),
        config={"view": str, "api_url": str, "s3_url": str, "chiro": str},
        dvc_wrapper="BIADVCFileSystem",
        # Mixed, and recorded conservatively: the derived (OME-Zarr) view carries
        # md5s, submitted files carry none. A profile cannot say "sometimes", and
        # the failure mode of guessing generously is silent data loss, so both
        # fields take the value that is safe for the weaker view.
        published_checksum=None,
        immutability_basis=None,
    ),
    "dbgap": Backend(
        protocol="dbgap",
        class_path="archivefs.backends.dbgap.DbGaPFileSystem",
        extra="dbgap",
        summary="dbGaP via SRA Toolkit — DEFERRED, not implemented",
        requires=(),
        requires_tools=("prefetch", "fasterq-dump"),
        status="deferred",
        # No config and no wrapper: deferred means DVC must not be told it exists.
        # The empty profile is deliberate rather than missing — it shows the shape
        # of the work left to do.
    ),
}

#: Info keys that say *how to fetch this entry* rather than what it contains, and so
#: must survive a ``.chiro`` manifest rewriting an entry's identity. Each backend
#: nests its own metadata under a block named for its protocol, which is why this is
#: derived from :data:`PROTOCOLS` rather than listed: it used to be a tuple in
#: ``chiro.py``, and adding an archive without extending it would have silently lost
#: the information needed to read the bytes.
PASSTHROUGH_INFO_KEYS: frozenset[str] = frozenset(
    {"url", "archive_version", *PROTOCOLS}
)


def register_implementations(clobber: bool = True) -> None:
    """Register every protocol with fsspec, carrying our own error text.

    Idempotent, and safe to call when the entry points have already registered
    the same classes.
    """
    from fsspec import register_implementation

    for backend in PROTOCOLS.values():
        register_implementation(
            backend.protocol,
            backend.class_path,
            clobber=clobber,
            errtxt=backend.error_text,
        )


def installed_backends() -> dict[str, dict]:
    """Report each protocol's readiness — used by ``archivefs info``."""
    import shutil

    report = {}
    for name, backend in PROTOCOLS.items():
        missing_tools = [t for t in backend.requires_tools if shutil.which(t) is None]
        missing_packages = backend.missing()
        report[name] = {
            "summary": backend.summary,
            "status": backend.status,
            "extra": backend.extra,
            "missing_packages": missing_packages,
            "missing_tools": missing_tools,
            "ready": backend.status == "supported"
            and not missing_packages
            and not missing_tools,
            # What the archive can do, not just whether it is installed. Reported
            # from the profile, so this costs no imports and cannot drift from the
            # tables dvcfs.py derives from the same source.
            "capabilities": {
                "published_checksum": backend.published_checksum,
                "immutability_basis": backend.immutability_basis,
                "expands_containers": backend.expands_containers,
                "options": sorted(backend.config),
            },
        }
    return report
