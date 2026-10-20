"""archivefs — read-only fsspec filesystems for scientific data archives.

Address data by accession or DOI and pull it through the fsspec ecosystem
(including DVC's ``import-url``)::

    ega://EGAD00001006673                  # dataset -> directory
    ega://EGAD00001006673/EGAF00004908765  # one file
    zenodo://<record_id>/<filename>        # or zenodo://10.5281/zenodo.<id>
    mendeley://10.17632/<id>[/<version>]   # Mendeley Data (anonymous; experimental)
    geo://<GSE_accession>                  # supplementary/processed files
    dbgap://<phs_id>/<SRR_accession>       # deferred, not implemented

Importing this package is *not* required to use the protocols — fsspec finds them
through entry points — but it does re-register them with better error messages,
so it is cheap and harmless to do.
"""

from __future__ import annotations

from archivefs.errors import (
    ArchiveFSError,
    ArchiveNotFoundError,
    AuthenticationError,
    CredentialsNotFoundError,
    ManifestError,
    MissingExtraError,
    MissingToolError,
    NotSupportedError,
    UnsafeArchiveMemberError,
    VerificationError,
)
from archivefs.registry import PROTOCOLS, installed_backends, register_implementations

try:  # pragma: no cover - trivial packaging glue
    from archivefs._version import version as __version__
except ImportError:  # pragma: no cover
    try:
        from importlib.metadata import PackageNotFoundError, version

        __version__ = version("archivefs")
    except (ImportError, PackageNotFoundError):
        __version__ = "0.0.0+unknown"

register_implementations()

__all__ = [
    "PROTOCOLS",
    "ArchiveFSError",
    "ArchiveNotFoundError",
    "AuthenticationError",
    "CredentialsNotFoundError",
    "ManifestError",
    "MissingExtraError",
    "MissingToolError",
    "NotSupportedError",
    "UnsafeArchiveMemberError",
    "VerificationError",
    "__version__",
    "installed_backends",
    "register_implementations",
]
