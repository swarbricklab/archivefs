"""``archivefs update`` — has the upstream moved, and re-pin it if so.

A `.chiro` pin is content-addressed and therefore immutable: `dvc update` on it is
correctly a no-op. But the *dataset* a pin points at does move — a depositor revises a
GEO record, adds a file to a BIA study — and you need a way to notice and to re-pin.
That is a **supersede**, not an in-place edit: you never mutate a content-addressed
object, you make a new one and move the pointer (`concepts-and-versions.md`; the design
in `archivefs-update-design.md`).

The operation is deliberately authoritative rather than cheap: it **regenerates** the
manifest from the live archive — download-free where the archive publishes checksums —
and compares the new `body_hash` to the pinned one. Equal means the file set has not
changed; different means it has, and chirograph's diff says exactly how. There is no
trusting of an mtime here (D34): a checksum is the only proof.

This lives in archivefs, not chirograph, because regeneration reads the live archive and
that I/O is archive-specific — routed through the same `concept` → backend map a
`chiro://` pin uses (D36), so a re-fetch keeps that backend's auth and verification.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from archivefs.chiro import _chirograph
from archivefs.errors import ManifestError
from archivefs.registry import protocol_for_concept
from archivefs.writer import build_manifest

__all__ = ["UpdatePlan", "plan_update"]


@dataclass
class UpdatePlan:
    """The result of regenerating a manifest and comparing it to the pinned one."""

    old: Any
    new: Any
    diff: Any
    #: The local file the pin came from, or ``None`` for a URL (which cannot be
    #: rewritten in place).
    manifest_path: Path | None

    @property
    def moved(self) -> bool:
        """Did the file set change? The one question a re-pin turns on."""
        return not self.diff.identical


def _load_manifest(ref: str) -> tuple[Any, Path | None]:
    """Load a manifest from a local path or a URL, returning ``(manifest, path)``.

    ``path`` is ``None`` for a URL — there is nothing on local disk to rewrite.
    """
    from archivefs.chiro import MANIFEST_SUFFIX

    chirograph = _chirograph()
    if not ref.endswith(MANIFEST_SUFFIX):
        raise ManifestError(
            f"{ref} is not an archive URL's manifest — `archivefs update` takes the "
            f"committed {MANIFEST_SUFFIX} (or a chiro:// URL naming it). To pin an "
            f"archive record for the first time, use `archivefs manifest`."
        )
    # A Windows drive letter ("C:\\") is not a URL scheme; a real scheme has more
    # than one leading character before the colon.
    scheme, sep, _ = ref.partition("://")
    if sep and len(scheme) > 1:
        import fsspec

        with fsspec.open(ref, "rb") as handle:
            return chirograph.loads(handle.read()), None
    path = Path(ref).expanduser()
    if not path.is_file():
        raise ManifestError(
            f"no manifest at {path} — `archivefs update` takes the committed "
            f".chiro.json (or a chiro:// URL naming it), not an archive URL."
        )
    return chirograph.load(path), path


def plan_update(
    target: str, options: dict[str, Any] | None = None, *, download: bool = False
) -> UpdatePlan:
    """Regenerate ``target``'s manifest from the live archive and diff it.

    ``target`` is a path to a committed ``.chiro.json`` or a ``chiro://`` URL naming
    one. Writes nothing — the caller decides whether to re-pin.
    """
    chirograph = _chirograph()
    options = options or {}

    # A chiro:// URL is just sugar for the manifest path it carries.
    ref = target
    if ref.startswith("chiro://"):
        ref = ref[len("chiro://") :]

    old, manifest_path = _load_manifest(ref)

    concept = getattr(old, "concept", None)
    if not concept:
        raise ManifestError(
            f"{target} carries no `concept`, so archivefs cannot tell which archive "
            f"to re-resolve. Regenerate it with a current `archivefs manifest`."
        )
    routed = protocol_for_concept(concept)
    if routed is None:
        raise ManifestError(
            f"{target} names concept {concept!r}, whose scheme belongs to no "
            f"archivefs backend."
        )
    protocol, accession = routed

    import fsspec

    delegate = fsspec.filesystem(protocol, skip_instance_cache=True, **options)
    new = build_manifest(
        delegate,
        accession,
        download=download,
        concept=old.concept,
        source_url=getattr(old, "source_url", None),
        self_deposited=old.provenance_type == chirograph.PROVENANCE_SELF,
    )
    diff = chirograph.diff_manifests(old, new)
    return UpdatePlan(old=old, new=new, diff=diff, manifest_path=manifest_path)
