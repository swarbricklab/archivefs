"""Generate a ``.chiro`` manifest *from* an archive listing — the writer half.

``chiro.py`` is the reader: given a manifest, it becomes the listing and the
checksums. This is the other direction, and it is what makes "all roads lead to
Chirograph" real (``docs/ideas.md`` §5). For an archive that already publishes a
per-file digest — Zenodo, EGA, the derived (OME-Zarr) BIA view — a manifest can be
built **without downloading a byte**: the listing already carries ``md5`` and
``size``, so we hand those straight to chirograph as :class:`FileEntry` tuples and
let it compute the content-addressed identity. That is the download-free path.

For an archive that publishes *no* digest (GEO, a column-less BIA study) there is
no shortcut: the only way to a real checksum is to read the bytes and hash them.
``download=True`` does exactly that, streaming each file through the filesystem and
hashing as it goes — the same md5 that verify-on-read (``docs/decisions.md`` D34)
would later enforce. Without it, a missing checksum is a hard error naming the
files, not an etag quietly standing in for a digest.

The manifest chirograph builds here is the same object ``chiro.py`` reads back, so a
deposit can be captured on one machine and verified on another with no shared state
but the committed ``.chiro.json``. Building and parsing both go through the
``chirograph`` package (``docs/decisions.md`` D5): a second canonicaliser that
agrees *almost* exactly would produce non-matching ``body_hash`` values in silence.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING, Any

from archivefs.chiro import MANIFEST_SUFFIX, _chirograph
from archivefs.errors import ManifestError

if TYPE_CHECKING:  # pragma: no cover
    from archivefs.base import ArchiveFileSystem

logger = logging.getLogger(__name__)

__all__ = ["build_manifest"]

#: Read size for the ``download=True`` hashing loop. Matches ``get_file`` /
#: ``cmd_get`` so a capture reads in the same 4 MiB strides the rest of the tool does.
_HASH_CHUNK = 4 * 1024 * 1024


def _hash_through_fs(fs: ArchiveFileSystem, path: str) -> tuple[str, int]:
    """Stream one member through ``fs`` and return ``(md5, size)``.

    The unavoidable cost of the capture path: an archive that publishes no digest
    can only be pinned by reading it. Streamed in strides rather than held whole so
    a multi-gigabyte supplementary file does not have to fit in memory.
    """
    digest = hashlib.md5()  # noqa: S324 - md5 is what these archives publish
    size = 0
    with fs.open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def build_manifest(
    fs: ArchiveFileSystem,
    container: str,
    *,
    self_deposited: bool = False,
    download: bool = False,
    retrieved: str | None = None,
    source_url: str | None = None,
    concept: str | None = None,
    record_version: str | None = None,
) -> Any:
    """Build a chirograph :class:`Manifest` over every file in ``container``.

    ``self_deposited`` picks the provenance type: an archive listing is something we
    *observed*, so the honest default is ``observed-download`` — reserve
    ``self-deposited`` for a deposit you control and vouch for. ``download`` forces
    the capture path (read-and-hash) even where the archive publishes checksums, and
    is the *only* way to build a manifest for an archive that publishes none.

    ``source_url`` / ``concept`` / ``record_version`` are the schema-1.1 staleness
    fields; the writer only carries whatever the caller supplies (populating them
    per-backend is ``archivefs update``'s job, ``archivefs-update-design.md``). The
    per-file ``version_id`` *is* filled in from the listing, because a backend that
    sets it has declared it the entry's immutability token.
    """
    chirograph = _chirograph()
    container = fs._strip_protocol(container)

    listing = fs.find(container, detail=True)
    entries: list[Any] = []
    missing: list[str] = []
    for path in sorted(listing):
        info = listing[path]
        if info.get("type") != "file":
            continue
        relpath = fs.relpath(container, path)
        # A manifest never lists itself: it would change its own body_hash, and
        # re-verifying a deposit would flag the manifest as an unrecorded file.
        if relpath.endswith(MANIFEST_SUFFIX):
            continue

        md5 = info.get("md5")
        size = info.get("size")
        if md5 is None:
            if download:
                md5, size = _hash_through_fs(fs, path)
            else:
                missing.append(relpath)
                continue

        entries.append(
            chirograph.FileEntry(
                relpath=relpath,
                md5=md5,
                size=size,
                # None where the backend published no immutability token; harmless.
                version_id=info.get("version_id"),
            )
        )

    if missing:
        shown = ", ".join(missing[:6]) + (" …" if len(missing) > 6 else "")
        raise ManifestError(
            f"{len(missing)} file(s) in {container} publish no checksum "
            f"({shown}), so a download-free manifest cannot be built for them. "
            f"Re-run with download=True (CLI: --download) to read and hash the "
            f"bytes, or exclude them."
        )
    if not entries:
        raise ManifestError(f"{container} lists no files to put in a manifest.")

    return chirograph.Manifest(
        provenance_type=(
            chirograph.PROVENANCE_SELF
            if self_deposited
            else chirograph.PROVENANCE_OBSERVED
        ),
        files=entries,
        record_accession=container,
        retrieved=retrieved,
        source_url=source_url,
        concept=concept,
        record_version=record_version,
        generator=chirograph.GENERATOR,
    )
