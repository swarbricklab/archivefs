"""``cbioportal://`` — **DEFERRED**. Registered, documented, not implemented.

Requested alongside ``mendeley://`` in issue #47, and deferred for the same reason
dbGaP is: the archive does not currently expose anything archivefs can address by
accession without violating the contract the rest of the project rests on. This
module exists to do the three things :mod:`archivefs.backends.dbgap` does — make
``cbioportal://`` a *known* protocol so the failure reads as "deferred, here is why"
rather than "unknown scheme", record the blockers in code, and fix the eventual URL
grammar — and nothing more.

The intended scheme (study id → member file of the study bundle)::

    cbioportal://brca_metabric                                    # study -> directory
    cbioportal://brca_metabric/data_mrna_illumina_microarray.txt  # one member file

Three blockers, each observed against the live service (issue #47):

**Only a whole-study tarball is published.** cBioPortal's datahub serves one
``https://cbioportal-datahub.s3.amazonaws.com/<study>.tar.gz`` per study — there are
no per-file URLs. A backend would have to address a member *inside* that tarball,
and a ``.tar.gz`` has no central directory and cannot be range-read, so listing or
reading one member means fetching and decompressing the whole archive. That is the
opposite of the download-free listing the project is built on (D9), and unlike a ZIP
there is no cheap index to lean on.

**No per-file checksums are published.** The datahub ships no digests at all, so a
member's md5 can only be had by downloading and hashing it (``--download``) — there
is no ``--no-download`` metadata path here, unlike Zenodo or Mendeley. ``info()``
would have to report ``md5=None`` and say why, exactly as ``geo://`` does, and the
archive would be ineligible for DVC's ``version_aware`` route.

**The S3 object is unreachable from our network.** The tarball returned HTTP 403
from both the regional and global S3 endpoints during the provenance pass that filed
#47 — so even the download-to-hash fallback cannot be exercised yet. An
implementation needs a reachable endpoint (the documented datahub mirror, the
GitHub-hosted datahub, or an institutional proxy) established first.

The licit interim, noted in the issue: cBioPortal studies are openly
redistributable, so mirroring one to a supported archive (Zenodo) and pinning *that*
keeps the analysis reproducible today. A native ``cbioportal://`` scheme — resolving
a study id to its tarball and addressing a member path, download-to-hash — is the
archivefs-idiomatic fix, deferred until a reachable byte source makes it testable.
"""

from __future__ import annotations

from typing import Any, ClassVar

from archivefs.base import ArchiveFileSystem
from archivefs.credentials import Credential
from archivefs.errors import NotSupportedError

#: The datahub tarball endpoint an implementation would resolve a study id against,
#: recorded so the eventual code starts from the observed URL rather than guessing.
#: It returned 403 from our network (see the module docstring); a reachable mirror
#: has to be found before this can be used.
DATAHUB_URL = "https://cbioportal-datahub.s3.amazonaws.com"

DEFERRED_MESSAGE = """\
the cbioportal:// backend is deferred, not missing (issue #47).

cBioPortal is in scope but cannot yet be addressed by accession without breaking the
contract the rest of archivefs relies on. Three things make it different from the
archives archivefs already speaks:

  * the datahub publishes only a whole-study .tar.gz — no per-file URLs, and a
    .tar.gz has no central directory, so a member cannot be listed or range-read
    without fetching and decompressing the whole archive;
  * no per-file checksums are published, so a member's md5 needs download-to-hash
    (no --no-download metadata path, unlike Zenodo/Mendeley);
  * the tarball returned HTTP 403 from our network on both S3 endpoints, so even
    that fallback is untestable until a reachable mirror is found.

If you need a cBioPortal study now: cBioPortal data is openly redistributable, so
mirror the study tarball to a supported archive (e.g. Zenodo) and pin that with
zenodo://, or record what you downloaded with a Chirograph 'observed-download'
manifest so the analysis stays reproducible.\
"""


class CBioPortalFileSystem(ArchiveFileSystem):
    """Placeholder that refuses clearly. See the module docstring."""

    protocol = "cbioportal"
    archive_name = "cBioPortal"
    #: Anonymous: the datahub needs no credential — the blocker is the byte source,
    #: not authentication. Declared empty so the profile is honest about that.
    credentials: ClassVar[list[Credential]] = []

    def __init__(self, **kwargs: Any) -> None:
        # Raise before touching the network: there is nothing here to configure yet.
        raise NotSupportedError(DEFERRED_MESSAGE)

    def _load_members(self, container: str) -> dict[str, dict]:  # pragma: no cover
        raise NotSupportedError(DEFERRED_MESSAGE)

    def _data_url(self, info: dict) -> str:  # pragma: no cover
        raise NotSupportedError(DEFERRED_MESSAGE)
