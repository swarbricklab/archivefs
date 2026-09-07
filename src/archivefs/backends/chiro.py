"""``chiro://`` — a manifest *is* the address, and the backend it names does the work.

Everywhere else in archivefs a ``.chiro`` manifest is an *overlay* on a live archive
URL (``bia://S-BIAD3080 -o chiro=sidecar.json``): the accession says which archive,
the manifest re-keys and checksums it. ``chiro://`` inverts that — the manifest *is*
the URL, addressed by the digest of the whole manifest file (the spec's naming, §13)::

    chiro://md5:15576e…                 # the whole pinned deposit
    chiro://md5:15576e…/images/a.tif    # one file in it

The digest is resolved to a manifest by :mod:`archivefs.store` — searching an ordered
list of stores, first hit wins (today just the repo-local ``.chirograph/``). A path
reference (``chiro://…/x.chiro.json``) still works, for an ad-hoc manifest outside a
store. Either way it is what a ``.dvc`` wants: a single, portable, content-addressed
pin that carries its own checksums and needs no second artefact.

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
        """Split into ``(manifest reference, relpath)``.

        Two reference forms: a **digest** (``md5:<hex>[/member]``, the spec's
        addressable name) and a **path** (``…/x.chiro.json[/member]``, resolved
        directly). A digest is unambiguous — its own segment parses as a chiro ref — so
        it is tried first; otherwise the format's compound ``.chiro.json`` suffix is the
        only reliable boundary, because a path reference itself contains slashes.
        """
        from archivefs import store

        head = path.split("/", 1)[0]
        if store.as_ref(head) is not None:
            _, _, member = path.partition("/")
            return head, member

        marker = MANIFEST_SUFFIX
        cut = path.find(marker)
        if cut == -1:
            # No manifest suffix at all: treat the whole thing as the reference so the
            # loader raises a message about *this* path rather than a split fragment.
            return path, ""
        end = cut + len(marker)
        return path[:end], path[end:].lstrip("/")

    def _manifest_source(self, container: str) -> str:
        """The local file to load for ``container`` — resolving a digest via the store.

        A ``md5:<digest>`` reference is looked up across the ordered store list
        (:mod:`archivefs.store`); a path reference is returned as-is for the loader.
        """
        from archivefs import store

        ref = store.as_ref(container)
        return str(store.resolve_ref(ref)) if ref is not None else container

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
        manifest = load_manifest(self, self._manifest_source(container))
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
        delegate_options = dict(self._delegate_options)
        if getattr(manifest, "containers", None):
            # A schema-2.0 manifest whose members live *inside* archive containers
            # (a ZIP). The delegate must expand them, or its native listing carries
            # the container as one opaque file and none of the manifest's per-member
            # `source_path`s match. The expansion knob is backend-specific
            # (`extract_zips` on Zenodo); set it only where the backend accepts it,
            # and never override a value the caller passed.
            import inspect

            init = fsspec.get_filesystem_class(protocol).__init__
            if "extract_zips" in inspect.signature(init).parameters:
                delegate_options.setdefault("extract_zips", True)
        delegate = fsspec.filesystem(
            protocol,
            skip_instance_cache=True,
            **delegate_options,
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
        members = self._members(container)  # resolves and caches the delegate
        delegate, accession = self._delegates[container]
        if not member:
            return delegate, accession
        # Fetch from the member's *archive-side* path, which `apply_manifest` recorded
        # as `source_native_path`. For a **relocated** container member the workspace
        # relpath (`visium/x.csv`) is not the delegate's path (`archive.zip/visium/x`);
        # the naive `accession/member` join only holds when the two coincide.
        info = members.get(f"{container}/{member}")
        native = info.get("source_native_path") if info else None
        return delegate, native or f"{accession}/{member}"

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
