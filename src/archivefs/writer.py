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
    known_checksums: dict[str, str] | None = None,
) -> Any:
    """Build a chirograph :class:`Manifest` over every file in ``container``.

    ``self_deposited`` picks the provenance type: an archive listing is something we
    *observed*, so the honest default is ``observed-download`` — reserve
    ``self-deposited`` for a deposit you control and vouch for. ``download`` forces
    the capture path (read-and-hash) even where the archive publishes checksums, and
    is the *only* way to build a manifest for an archive that publishes none.

    ``concept`` defaults to the backend's own concept CURIE (``fs.concept``), so a
    manifest is self-identifying: a ``chiro://`` pin reads it to learn which backend
    re-fetches the bytes, and ``archivefs update`` reads it to know what to re-resolve.
    Pass ``concept`` explicitly to override. ``source_url`` / ``record_version`` are the
    other schema-1.1 staleness fields; the writer carries whatever the caller supplies.
    The per-file ``version_id`` *is* filled in from the listing, because a backend that
    sets it has declared it the entry's immutability token.

    ``known_checksums`` maps an archive-side relpath to an md5 that was *already
    computed* for those bytes — DVC's, when it imported the deposit
    (:mod:`archivefs.from_dvc`). A member the archive publishes no md5 for takes its
    md5 from here before falling back to ``download`` or failing, so a ZIP of
    gigabytes is pinned from the metadata listing alone. Everything else (crc32,
    container md5, size, concept) still comes from that listing, so the manifest is
    byte-for-byte the one ``download=True`` would have built.
    """
    chirograph = _chirograph()
    container = fs._strip_protocol(container)
    if concept is None:
        concept = fs.concept(container)

    listing = fs.find(container, detail=True)
    entries: list[Any] = []
    missing: list[str] = []
    # Container identity is recorded once, keyed by source_root; the first member
    # under a ZIP contributes it, the rest just link to it (`container-model.md` §2).
    containers: dict[str, Any] = {}
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
        if md5 is None and known_checksums is not None:
            # A checksum the archive does not publish but *someone already computed* —
            # e.g. DVC, which hashed this exact byte sequence when it first imported it.
            # Reusing it is what makes a manifest of a multi-gigabyte ZIP free: the
            # listing supplies everything else (crc32, container md5, size), so no byte
            # is read. Keyed by the archive-side relpath, which is how DVC records it.
            md5 = known_checksums.get(relpath)
        if md5 is None:
            if download:
                md5, size = _hash_through_fs(fs, path)
            else:
                missing.append(relpath)
                continue

        # Is this file delivered by an archive container (a ZIP member)? If so it
        # carries a download-free `crc32`, and its enclosing container's identity
        # goes in the `containers` block. `source_path` is the archive-side layout
        # (here == relpath), the anchor the linkage rule matches against.
        member = fs.container_of(container, info)
        crc32 = None
        source_path = None
        if member is not None:
            crc32 = member.get("crc32")
            # `source_path` is the archive-side layout — `<source_root>/<in-zip path>`
            # — which is what the linkage rule matches on and what the delegate lists
            # when a `chiro://` pin re-reads at the record. When the manifest is built
            # addressing the ZIP directly, `relpath` is the bare interior (the clean
            # workspace path), so `source_path` differs from it (a *relocated* entry);
            # addressing the whole record, they coincide.
            in_zip = member.get("in_zip_path") or relpath
            source_path = f"{member['source_root']}/{in_zip}"
            _record_container(chirograph, containers, member, relpath)

        entries.append(
            chirograph.FileEntry(
                relpath=relpath,
                md5=md5,
                size=size,
                source_path=source_path,
                crc32=crc32,
                # None where the backend published no immutability token; harmless.
                version_id=info.get("version_id"),
                # The archive's own per-file version tag, where it publishes one
                # (schema 1.1). Distinct from `version_id`, which is the deposit's
                # object version; `file_version` is the file's revision within it.
                file_version=info.get("file_version"),
            )
        )

    if missing:
        shown = ", ".join(missing[:6]) + (" …" if len(missing) > 6 else "")
        raise ManifestError(
            f"{len(missing)} file(s) in {container} publish no checksum "
            f"({shown}), so a download-free manifest cannot be built for them. "
            f"Re-run with download=True (CLI: --download) to read and hash the "
            f"bytes, reuse checksums something already computed (CLI: --from-dvc "
            f"<file.dvc>), or exclude them."
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
        # A `containers` block is what tips a manifest to schema 2.0; absent (a
        # loose deposit), the manifest stays 1.x byte-for-byte (`container-model.md`
        # §4). Sorted by source_root so the manifest is deterministic.
        containers=[containers[root] for root in sorted(containers)],
        # The version-pinned accession, not the address the user typed: a Zenodo
        # concept DOI resolves to the *version* recid here, so `chiro://` fetches the
        # exact pinned version rather than following the concept to latest.
        record_accession=fs.version_accession(container),
        retrieved=retrieved,
        source_url=source_url,
        concept=concept,
        record_version=record_version,
        generator=chirograph.GENERATOR,
    )


def _record_container(
    chirograph: Any, containers: dict[str, Any], member: dict, relpath: str
) -> None:
    """Fold one member's container into ``containers``, deduped by ``source_root``.

    The container's identity is invariant across its members, so the first member
    under a given ``source_root`` builds the :class:`chirograph.Container` and the
    rest are a no-op. A member's ``source_path`` must sit under its container's
    ``source_root`` or the manifest is rejected on read (`container-model.md` §5);
    the writer keys the two off the same native layout so they always agree.
    """
    root = member["source_root"]
    if root in containers:
        return
    containers[root] = chirograph.Container(
        source_root=root,
        format=member["format"],
        md5=member["md5"],
        size=member.get("size"),
    )
