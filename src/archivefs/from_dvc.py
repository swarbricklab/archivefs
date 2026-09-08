"""Build a ``.chiro`` manifest from a DVC import — **offline**, from the ``.dvc`` alone.

A DVC import records, in the ``.dvc``'s ``files:`` list, everything a manifest needs and
nothing it has to go back to the archive for: each member's ``relpath``, ``md5`` and
``size``, plus — for a Zenodo ZIP — a ``version_id`` of ``<container_md5>:<crc32>`` (the
pre-2.0 overload archivefs writes). The dependency URL supplies the rest: the ZIP's name
(``source_root``), the record it fetches from (``record_accession``), and the concept
scheme. So a container manifest is reconstructed **without touching the archive** — no
listing, no download, no dependency on the archive being reachable or even existing yet.

That last point is the use case: a depositor prepares the manifest *before* uploading,
from the data they are about to deposit, and confirms the deposit against it afterwards.
The manifest a `chiro://` pin needs is the same one — so re-pinning an existing DVC
import is just the same transform, and it does not care that the archive is slow, down,
or unaware of the pin. The reconstructed manifest carries the member ``md5`` DVC already
trusted, so its DVC ``.dir`` hash is unchanged and re-pinning is a clean swap.

Scoped to a DVC **import** (an out from an archivefs URL): a plain ``dvc add`` has no
upstream for a `chiro://` pin to route to.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from archivefs.chiro import _chirograph
from archivefs.errors import ManifestError

__all__ = ["manifest_from_dvc"]

#: The Zenodo ZIP-member ``version_id`` overload: ``<container md5>:<crc32>``.
_ZIP_VERSION_ID = re.compile(r"^(?P<md5>[0-9a-f]{32}):(?P<crc>[0-9a-f]{1,8})$")
#: Container extensions archivefs expands and can therefore pin member-granularly.
_CONTAINER_EXTS = (".zip",)


def manifest_from_dvc(
    dvc_file: str | Path,
    *,
    self_deposited: bool = False,
    concept: str | None = None,
) -> Any:
    """Reconstruct a chirograph :class:`Manifest` from a DVC import's ``.dvc``, offline.

    Reads the source URL and the per-file records (``relpath``, ``md5``, ``size``,
    ``version_id``) from the ``.dvc``; parses the ZIP overload for each member's
    ``crc32`` and container md5; and derives ``source_root`` / ``record_accession`` /
    ``concept`` from the URL. No archive request is made. Raises :class:`ManifestError``
    on a ``dvc add`` (no URL) or a ``.dvc`` with no reusable per-file checksums.

    ``concept`` overrides the derived concept CURIE. Offline, only the *version* DOI is
    knowable from a Zenodo URL, so a depositor who knows the true concept DOI (from the
    record page, or a reserved DOI) passes it here to get concept-latest tracking for
    ``archivefs update``, instead of the version-DOI approximation.
    """
    import yaml

    chirograph = _chirograph()

    path = Path(dvc_file).expanduser()
    if not path.is_file():
        raise ManifestError(f"no .dvc file at {path}.")
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path} is not readable YAML: {exc}") from exc

    deps = doc.get("deps") or []
    dep = next((d for d in deps if d.get("path")), None)
    if dep is None:
        raise ManifestError(
            f"{path} has no dependency URL — it is a `dvc add`, not an import, so "
            f"there is no archive for a chiro:// pin to route to. `manifest "
            f"--from-dvc` is for data imported from an archive (dvc import-url …)."
        )
    url = dep["path"]
    files = _files_of(dep, path)

    scheme, _, rest = url.partition("://")
    source_root, record_accession, derived_concept = _route(scheme, rest)
    # A caller-supplied concept (the true concept DOI a depositor knows) wins over the
    # version-DOI approximation the URL can offer offline.
    concept = concept or derived_concept

    entries: list[Any] = []
    container_md5: str | None = None
    for entry in files:
        relpath, md5, size = entry["relpath"], entry["md5"], entry["size"]
        match = _ZIP_VERSION_ID.match(entry.get("version_id") or "")
        if match and source_root:
            # A ZIP member: crc32 and the container's md5 come from the overload;
            # source_path carries the archive-side `<zip>/<interior>` for linkage.
            container_md5 = container_md5 or match["md5"]
            entries.append(
                chirograph.FileEntry(
                    relpath=relpath,
                    md5=md5,
                    size=size,
                    source_path=f"{source_root}/{relpath}",
                    crc32=match["crc"],
                )
            )
        else:
            # A loose file: the md5 is the whole story (no container, no crc32).
            entries.append(
                chirograph.FileEntry(
                    relpath=relpath,
                    md5=md5,
                    size=size,
                    version_id=entry.get("version_id") or None,
                )
            )
    if not entries:
        raise ManifestError(f"{path} lists no files to put in a manifest.")

    containers = []
    if container_md5 and source_root:
        containers = [
            chirograph.Container(
                source_root=source_root, format="zip", md5=container_md5
            )
        ]

    return chirograph.Manifest(
        provenance_type=(
            chirograph.PROVENANCE_SELF
            if self_deposited
            else chirograph.PROVENANCE_OBSERVED
        ),
        files=entries,
        containers=containers,
        record_accession=record_accession,
        source_url=url,
        concept=concept,
        generator=chirograph.GENERATOR,
    )


def _files_of(dep: dict, path: Path) -> list[dict]:
    """The per-file records to reuse: the inline ``files:`` list, else DVC's own view.

    ``dvc import-url`` writes ``files:`` inline (with the ``version_id`` overload); an
    import that records only a directory hash falls back to DVC's own view
    (:class:`dvc.api.DVCFileSystem`), which surfaces ``md5``/``size`` without
    materialising it (but no ``version_id``, so it pins loose, no container crc32s).
    """
    inline = [
        f for f in (dep.get("files") or []) if f.get("relpath") and f.get("md5")
    ]
    if inline:
        return inline
    fallback = _files_via_dvcfs(path)
    if not fallback:
        raise ManifestError(
            f"{path} records no per-file md5s to reuse (neither an inline `files:` "
            f"list nor a resolvable DVC directory hash). Build the manifest from the "
            f"archive with `archivefs manifest <url> --download` instead."
        )
    return fallback


def _route(scheme: str, rest: str) -> tuple[str | None, str, str | None]:
    """From a dep URL, derive ``(source_root, record_accession, concept)`` — no network.

    ``source_root`` is the trailing container segment (``archive.zip``) or ``None``.
    ``record_accession`` is what a `chiro://` pin fetches — the record the members live
    in (a Zenodo recid, or the accession/subtree otherwise). ``concept`` is the routing
    CURIE; for Zenodo it is the *version* DOI from the URL, not the resolved concept DOI
    (unknowable offline) — enough to route and fetch, though `archivefs update` then
    tracks that version rather than latest.
    """
    from archivefs.registry import PROTOCOLS

    backend = PROTOCOLS.get(scheme)
    concept_scheme = backend.concept_scheme if backend else scheme

    segments = rest.split("/")
    if segments[-1].endswith(_CONTAINER_EXTS):
        source_root = segments[-1]
        record_ref = "/".join(segments[:-1])
    else:
        source_root = None
        record_ref = rest

    concept = f"{concept_scheme}:{record_ref}" if concept_scheme else None
    if scheme == "zenodo":
        # A Zenodo pin fetches the numeric record id; extract it from the DOI form.
        doi = re.search(r"zenodo\.(\d+)", record_ref)
        record_accession = doi.group(1) if doi else record_ref
    else:
        record_accession = record_ref
    return source_root, record_accession, concept


def _files_via_dvcfs(dvc_file: Path) -> list[dict]:
    """Per-file ``{relpath, md5, size}`` from DVC's own view, keyed to the out.

    Reads what DVC has on hand (no re-hash, no materialisation); ``[]`` if DVC is absent
    or the path is untracked, so the caller raises one clear error, not a traceback.
    """
    try:
        from dvc.api import DVCFileSystem
    except ImportError:
        return {}  # type: ignore[return-value]
    import yaml

    doc = yaml.safe_load(dvc_file.read_text()) or {}
    out_path = next(
        (o.get("path") for o in (doc.get("outs") or []) if o.get("path")),
        dvc_file.stem,
    )
    try:
        fs = DVCFileSystem(str(dvc_file.resolve().parent))
        abs_out = (dvc_file.parent / out_path).resolve()
        rel_out = str(abs_out.relative_to(fs.repo.root_dir))
        found = fs.find(rel_out, detail=True)
    except Exception:  # noqa: BLE001 - any DVC-side failure degrades to "nothing reused"
        return []
    prefix = f"{rel_out}/"
    out: list[dict] = []
    for repo_path, info in found.items():
        md5 = info.get("md5")
        if info.get("type") == "file" and md5 and repo_path.startswith(prefix):
            out.append(
                {
                    "relpath": repo_path[len(prefix):],
                    "md5": md5,
                    "size": info.get("size"),
                }
            )
    return out
