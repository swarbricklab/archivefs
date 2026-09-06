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

    def _resolve_delegate(self, container: str) -> tuple[ArchiveFileSystem, str]:
        """Load the manifest, and instantiate the backend its ``concept`` names."""
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
        # `chiro=<manifest>` makes the manifest the delegate's overlay, so its listing
        # is the manifest's view and each read is verified against the manifest md5.
        delegate = fsspec.filesystem(
            protocol,
            chiro=container,
            skip_instance_cache=True,
            **self._delegate_options,
        )
        return delegate, accession

    def _load_members(self, container: str) -> dict[str, dict]:
        delegate, accession = self._resolve_delegate(container)
        self._delegates[container] = (delegate, accession)
        native = delegate._members(accession)
        members: dict[str, dict] = {}
        for path, info in native.items():
            relpath = delegate.relpath(accession, path)
            name = f"{container}/{relpath}" if relpath else container
            # The delegate's info already carries md5/size and its own fetch metadata;
            # only the *name* changes into this layer's address space. The original
            # delegate path is stashed so a read can be forwarded without re-splitting.
            members[name] = {**info, "name": name, "_chiro_source": path}
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
    # Every byte operation is forwarded to the delegate, which does the real fetch
    # (auth, verify-on-read, block cache) under its own protocol. This layer never
    # touches the network itself, so `_data_url`/`_fetch_range` are never reached.

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
        return delegate.cat_file(dpath, start=start, end=end, **kwargs)

    def _open(self, path: str, mode: str = "rb", **kwargs: Any) -> Any:
        delegate, dpath = self._delegate_read(path)
        return delegate._open(dpath, mode=mode, **kwargs)

    def get_file(self, rpath: str, lpath: str, **kwargs: Any) -> Any:
        delegate, dpath = self._delegate_read(rpath)
        return delegate.get_file(dpath, lpath, **kwargs)
