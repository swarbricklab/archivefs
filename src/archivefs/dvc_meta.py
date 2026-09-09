"""Stamp human-readable provenance into a `chiro://` pin's ``.dvc`` file.

Addressing a DVC import by digest (`chiro://md5:…`) buys content-addressing at the cost
of legibility: the `.dvc` no longer says *where* the data came from — you must resolve
the manifest in `.chirograph/` to learn it is even Zenodo. This writes that provenance
back into the `.dvc` as a free-form ``meta.archivefs`` block, derived from the pin's own
manifest: the archive, the record / DOI (or BIA study), the concept, the container, a
clickable landing URL, and the ``zenodo://``/``bia://`` source to re-import from.

It uses DVC's own `meta:` field, which the spec allows as an arbitrary object and —
crucially — **excludes from the stage md5** and **preserves across `dvc update`** (both
verified). So stamping a pin does not change its identity and survives a refresh. The
block is **advisory**: the `chiro://` digest and the manifest remain the authoritative,
verifiable record; ``meta`` is a summary for a human reading the file on GitHub, a
``grep`` for "zenodo", or a recovery hint if the store is ever lost. Being derived from
the manifest rather than hand-written, it does not drift — re-run to refresh it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archivefs.chiro import _chirograph
from archivefs.errors import ManifestError

__all__ = ["provenance_meta", "stamp"]

#: concept scheme → the archive's short name.
_ARCHIVE = {"doi": "zenodo", "biostudies": "bia", "geo": "geo", "ega": "ega"}

_NOTE = (
    "provenance summary only; the chiro:// pin and .chirograph manifest "
    "are authoritative"
)


def provenance_meta(manifest: Any) -> dict[str, Any]:
    """Build the ``{"archivefs": {...}}`` provenance block from a parsed manifest.

    Everything comes from the manifest's own routing fields (``concept``,
    ``record_accession``, ``source_url``, ``containers``), so the block is a *view* of
    the authoritative record, not a second source of truth. Shapes differ a little per
    archive (a Zenodo DOI vs a BIA study/subtree), which is fine: ``meta`` is free-form.
    """
    concept = getattr(manifest, "concept", None) or ""
    scheme, _, concept_value = concept.partition(":")
    archive = _ARCHIVE.get(scheme, scheme or "unknown")
    record = manifest.record_accession or ""

    info: dict[str, Any] = {"archive": archive}
    if archive == "zenodo":
        # record_accession is the numeric recid; present the citable version DOI + link.
        is_recid = record.isdigit()
        info["record"] = f"10.5281/zenodo.{record}" if is_recid else record
        if concept_value:
            info["concept_doi"] = concept_value
        if manifest.containers:
            info["container"] = manifest.containers[0].source_root
        if is_recid:
            info["url"] = f"https://doi.org/10.5281/zenodo.{record}"
        if getattr(manifest, "source_url", None):
            info["source"] = manifest.source_url
    elif archive == "bia":
        study, _, subtree = record.partition("/")
        info["study"] = study
        if subtree:
            info["subtree"] = subtree
        info["url"] = f"https://www.ebi.ac.uk/biostudies/studies/{study}"
        info["source"] = f"bia://{record}"
    elif archive == "geo":
        info["accession"] = record
        info["url"] = f"https://www.ncbi.nlm.nih.gov/geo/query/acc.cgi?acc={record}"
        info["source"] = f"geo://{record}"
    else:
        info["accession"] = record
        if concept_value:
            info["concept"] = concept_value
        if getattr(manifest, "source_url", None):
            info["source"] = manifest.source_url

    info["nfiles"] = manifest.nfiles
    info["note"] = _NOTE
    return {"archivefs": info}


def stamp(dvc_file: str | Path, *, write: bool = True) -> dict[str, Any]:
    """Resolve a `chiro://` ``.dvc``'s manifest and write its provenance ``meta`` block.

    Reads the ``chiro://md5:`` dependency, resolves it through the store to a manifest,
    and (with ``write``) rewrites the ``.dvc`` with a refreshed ``meta.archivefs`` block
    — preserving every other field and any non-``archivefs`` ``meta`` keys the user
    added. The stage md5 is unchanged (``meta`` is excluded from it), so the pin's
    identity and DVC validity are untouched. Returns the block either way. Idempotent.
    """
    import yaml

    from archivefs import store

    chirograph = _chirograph()
    path = Path(dvc_file).expanduser()
    if not path.is_file():
        raise ManifestError(f"no .dvc file at {path}.")
    doc = yaml.safe_load(path.read_text()) or {}

    dep = next(
        (
            d
            for d in (doc.get("deps") or [])
            if str(d.get("path", "")).startswith("chiro://")
        ),
        None,
    )
    if dep is None:
        raise ManifestError(
            f"{path} has no chiro:// dependency — provenance meta is for chiro:// pins "
            f"(a plain zenodo://…/bia://… dep is already self-describing)."
        )
    ref = store.as_ref(dep["path"])
    if ref is None:
        raise ManifestError(
            f"{path}: {dep['path']} is not a resolvable chiro://md5 ref."
        )
    manifest = chirograph.load(store.resolve_ref(ref, start=path))
    block = provenance_meta(manifest)

    if write:
        # Merge our key into any existing meta, then re-emit with meta after md5/frozen
        # so the file reads top-down: identity, provenance, then the DVC wiring.
        merged_meta = {**(doc.get("meta") or {}), **block}
        ordered: dict[str, Any] = {}
        for key in ("md5", "frozen", "wdir"):
            if key in doc:
                ordered[key] = doc[key]
        ordered["meta"] = merged_meta
        for key, value in doc.items():
            if key not in ordered and key != "meta":
                ordered[key] = value
        path.write_text(yaml.safe_dump(ordered, sort_keys=False))
    return block
