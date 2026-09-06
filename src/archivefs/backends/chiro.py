"""``chiro://`` — a manifest *is* the address, and the backend it names does the work.

Everywhere else in archivefs a ``.chiro`` manifest is an *overlay* on a live archive
URL (``bia://S-BIAD3080 -o chiro=sidecar.json``): the accession says which archive,
the manifest re-keys and checksums it. ``chiro://`` inverts that — the manifest *is*
the URL::

    chiro://S-BIAD3080.chiro.json                 # the whole pinned deposit
    chiro://S-BIAD3080.chiro.json/images/a.tif    # one file in it

which is what a ``.dvc`` wants to record: a single, portable, content-addressed pin
that carries its own checksums and needs no second artefact.

The resolution (``docs/decisions.md`` D36) is deliberately *not* a raw fetch of each
file's URL. It reads the manifest's ``concept`` CURIE, maps it back to the backend
that owns those bytes (``geo:GSE…`` → ``geo://``), and **overlays the manifest on that
backend** — so the actual read is a normal ``geo``/``bia``/``ega``/``zenodo`` read,
with that backend's authentication, verify-on-read (D34), resumable block cache (D29)
and ZIP-safety all intact. A raw HTTP fetch of ``source_url`` would bypass every one
of those, and so would silently fail for exactly the controlled-access archives that
make a manifest worth having. ``chiro://`` is therefore a pure *addressing* layer: it
translates ``chiro://<manifest>/<relpath>`` into a delegate read and forwards it.
"""

from __future__ import annotations

import hashlib
from typing import Any

from archivefs.base import ArchiveFileSystem
from archivefs.chiro import MANIFEST_SUFFIX, _chirograph, load_manifest
from archivefs.errors import ManifestError
from archivefs.registry import protocol_for_concept

__all__ = ["ChiroFileSystem"]

#: Storage options that mean something to *this* layer and must not be forwarded to
#: the delegate backend as its own config.
_OWN_OPTIONS = frozenset({"chiro"})


class ChiroFileSystem(ArchiveFileSystem):
    """Resolve a ``.chiro`` manifest reference and read *through* the backend it names.

    The container is the manifest reference (a local path or a URL ending in
    ``.chiro.json``); the member is a file's ``relpath`` within the manifest. Listing
    is the manifest's own view; every read is forwarded to the delegate backend the
    manifest's ``concept`` identifies, so nothing here re-implements fetching, auth or
    verification.
    """

    protocol = "chiro"
    archive_name = "chiro"

    def __init__(self, **kwargs: Any) -> None:
        # Options to hand to whichever backend a manifest routes to, captured before
        # the base consumes its own. The delegate gets `chiro=<manifest>` set for it,
        # so the manifest becomes that backend's overlay.
        self._delegate_options = {
            k: v for k, v in kwargs.items() if k not in _OWN_OPTIONS
        }
        super().__init__(**kwargs)
        # This layer *is* the manifest layer, so the base manifest-overlay pass must
        # not run again over our already-resolved listing.
        self.chiro = False
        #: container (manifest ref) -> (delegate filesystem, accession within it)
        self._delegates: dict[str, tuple[ArchiveFileSystem, str]] = {}

    # ------------------------------------------------------------- URL grammar

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        """Strip ``chiro://`` while **keeping** a leading slash.

        The base strips it, which is right for an accession but wrong here: a manifest
        reference can be an absolute path (``chiro:///data/x.chiro.json``), and losing
        the leading slash would turn it into a relative one that resolves nowhere.
        """
        for prefix in (f"{cls.protocol}://", f"{cls.protocol}:"):
            if path.startswith(prefix):
                path = path[len(prefix) :]
                break
        return path.rstrip("/")

    def _split(self, path: str) -> tuple[str, str]:
        """Split into ``(manifest reference, relpath)`` on the ``.chiro.json`` suffix.

        The manifest reference itself contains slashes (and, for a URL, a ``://``), so
        the ordinary "first segment is the container" rule cannot apply. The format's
        compound suffix is the only unambiguous boundary.
        """
        marker = MANIFEST_SUFFIX
        cut = path.find(marker)
        if cut == -1:
            # No manifest suffix at all: treat the whole thing as the reference so the
            # loader raises a message about *this* path rather than a split fragment.
            return path, ""
        end = cut + len(marker)
        return path[:end], path[end:].lstrip("/")

    # --------------------------------------------------------- listing surface

    def _resolve_delegate(
        self, container: str
    ) -> tuple[ArchiveFileSystem, str, Any]:
        """Load the manifest and instantiate the backend its ``concept`` names.

        Returns ``(delegate, accession, manifest)``. ``accession`` is the manifest's
        version-pinned ``record_accession`` — which may name a *subtree*
        (``S-BIAD3080/images/ihc``), not just a record; the delegate's own listing
        splits and scopes it, so a pin over part of a study routes and fetches exactly
        that part.
        """
        import fsspec

        _chirograph()  # raises MissingExtraError naming the install if absent
        manifest = load_manifest(self, container)
        concept = getattr(manifest, "concept", None)
        if not concept:
            raise ManifestError(
                f"{container} carries no `concept`, so archivefs cannot tell which "
                f"backend re-fetches its bytes. Regenerate it with a current "
                f"`archivefs manifest` (which records the concept), or use the "
                f"overlay form (`<archive-url> -o chiro={container}`) instead."
            )
        routed = protocol_for_concept(concept)
        if routed is None:
            raise ManifestError(
                f"{container} names concept {concept!r}, whose scheme belongs to no "
                f"archivefs backend. Known schemes: doi, geo, biostudies, ega."
            )
        protocol, concept_accession = routed
        # Fetch through the **version-pinned** accession (`record_accession`), not the
        # concept: for Zenodo the concept DOI resolves to *latest*, so routing on it
        # would let a newer version replace the pinned bytes. `concept` is only for
        # `archivefs update` (track-latest); a pin must be reproducible. Falls back to
        # the concept's own identifier for a manifest that predates this field.
        accession = getattr(manifest, "record_accession", None) or concept_accession
        # A plain delegate, no `chiro=` overlay: this layer applies the manifest
        # itself, in the accession's frame, so a subtree pin matches a subtree listing.
        delegate = fsspec.filesystem(
            protocol,
            skip_instance_cache=True,
            **self._delegate_options,
        )
        return delegate, accession, manifest

    def _load_members(self, container: str) -> dict[str, dict]:
        from archivefs.chiro import apply_manifest

        delegate, accession, manifest = self._resolve_delegate(container)
        self._delegates[container] = (delegate, accession)
        # List in the accession's frame (records OR subtrees), then let the manifest
        # be the listing. `find` scopes to the subtree; `apply_manifest` matches each
        # entry's source against it and carries the delegate's fetch metadata across.
        native = delegate.find(accession, detail=True)
        resolved, _drift = apply_manifest(
            delegate, accession, native, container, manifest
        )
        members: dict[str, dict] = {}
        for dpath, info in resolved.items():
            relpath = delegate.relpath(accession, dpath)
            name = f"{container}/{relpath}" if relpath else container
            members[name] = {**info, "name": name}
        return members

    def _container_info(self, container: str) -> dict:
        return {"name": container, "type": "directory", "size": 0}

    def _list_root(self) -> list[dict]:
        from archivefs.errors import NotSupportedError

        raise NotSupportedError(
            "chiro:// has no listable root — address a manifest, e.g. "
            "chiro://GSE176078.chiro.json"
        )

    # ------------------------------------------------------------------- reads
    #
    # Bytes are fetched by the delegate (auth, block cache under its own protocol),
    # but **verified here**, against the *manifest's* md5 — because the delegate lists
    # natively and a checksum-less archive (GEO, BIA-submitted) has no md5 of its own
    # to check against. The manifest is the only thing that can, which is the point of
    # a pin. So `_open`/`get_file` route through this layer's own verifying read path
    # (base `_open`/`get_file` off `self.info`), and only the raw byte fetch is
    # delegated, via `_data_url`/`_fetch_range` below.

    def _delegate_read(self, path: str) -> tuple[ArchiveFileSystem, str]:
        path = self._strip_protocol(path)
        container, member = self._split(path)
        self._members(container)  # ensures the delegate is resolved and cached
        delegate, accession = self._delegates[container]
        return delegate, f"{accession}/{member}" if member else accession

    def cat_file(
        self, path: str, start: int | None = None, end: int | None = None, **kwargs: Any
    ) -> bytes:
        delegate, dpath = self._delegate_read(path)
        data = delegate.cat_file(dpath, start=start, end=end, **kwargs)
        # A whole read is checkable against the manifest's md5 (a range is not); the
        # delegate could not check it because it read natively, without the manifest.
        if start is None and end is None:
            self._verify_checksum(self.info(path), hashlib.md5(data).hexdigest())  # noqa: S324
        return data

    def _open(self, path: str, mode: str = "rb", **kwargs: Any) -> Any:
        delegate, dpath = self._delegate_read(path)
        return delegate._open(dpath, mode=mode, **kwargs)

    # `get_file` is intentionally *not* overridden: the base copies via `self._open`
    # (→ delegate) and then re-hashes the result against `self.info(rpath)` — the
    # manifest md5 — refusing and deleting on mismatch. Forwarding to the delegate's
    # own `get_file` would instead verify against a checksum it does not have.
