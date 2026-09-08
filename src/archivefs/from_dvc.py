"""Build a ``.chiro`` manifest from a DVC import, reusing the checksums DVC already has.

When DVC imports a deposit (``dvc import-url zenodo://…/archive.zip``) it hashes every
file it pulls and records those md5s — inline in the ``.dvc`` file's ``files:`` list,
and in its own state. A ZIP member has no *published* md5, so a plain ``archivefs
manifest`` (:mod:`archivefs.writer`) would have to re-read and re-hash the whole archive
to pin it. But DVC already did that work. This module reuses it: it reads the source URL
and the per-file md5s out of the ``.dvc``, and hands the md5s to
:func:`~archivefs.writer.build_manifest` as ``known_checksums`` — so the archive is
*listed* (cheap: crc32, container md5, size, concept all come from the metadata) but
never *downloaded*. The manifest is byte-for-byte the one ``--download`` would have
produced, for the cost of a metadata listing instead of tens of gigabytes.

It is deliberately scoped to a DVC **import** — an out that came from an archivefs URL —
because that URL is what makes the manifest a routable ``chiro://`` pin. A plain ``dvc
add`` has no upstream to route to; that is a different artefact (a self-deposited local
capture), not this.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archivefs.errors import ManifestError

__all__ = ["DvcSource", "read_dvc_import"]


class DvcSource:
    """The source URL and DVC-computed checksums pulled from a ``.dvc`` import."""

    def __init__(
        self,
        url: str,
        checksums: dict[str, str],
        out_path: str,
        fs_config: dict[str, Any] | None = None,
    ) -> None:
        #: the dependency URL, e.g. ``zenodo://10.5281/zenodo.…/archive.zip``
        self.url = url
        #: archive-side relpath -> md5, exactly as DVC recorded it
        self.checksums = checksums
        #: the workspace path DVC materialises to (for messages)
        self.out_path = out_path
        #: the dep's ``fs_config`` — storage options the import was made with, such
        #: as ``extract_zips: true``. Applied when re-listing, or the archive is read
        #: in a different shape than DVC saw it and the relpaths no longer line up.
        self.fs_config = fs_config or {}


def read_dvc_import(dvc_file: str | Path) -> DvcSource:
    """Read a ``.dvc`` import: its source URL and the md5s DVC already computed.

    Prefers the ``files:`` list DVC writes inline (present for ``import-url``); falls
    back to DVC's own live view (:class:`dvc.api.DVCFileSystem`) for an import whose
    ``.dvc`` records only a directory hash. Raises :class:`ManifestError` with a pointed
    message when the target is not an import, or carries no checksums to reuse.
    """
    import yaml

    path = Path(dvc_file).expanduser()
    if not path.is_file():
        raise ManifestError(f"no .dvc file at {path}.")
    try:
        doc = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ManifestError(f"{path} is not readable YAML: {exc}") from exc

    deps = doc.get("deps") or []
    outs = doc.get("outs") or []
    url = next((d.get("path") for d in deps if d.get("path")), None)
    if url is None:
        raise ManifestError(
            f"{path} has no dependency URL — it is a `dvc add`, not an import, so "
            f"there is no archive for a chiro:// pin to route to. `manifest "
            f"--from-dvc` is for data imported from an archive (dvc import-url "
            f"zenodo://…)."
        )
    out_path = next((o.get("path") for o in outs if o.get("path")), str(path.stem))

    # 1. The inline `files:` list — present for `dvc import-url`, and self-contained.
    dep = next(d for d in deps if d.get("path") == url)
    checksums = {
        f["relpath"]: f["md5"]
        for f in (dep.get("files") or [])
        if f.get("relpath") and f.get("md5")
    }
    # 2. Fall back to DVC's live view (a directory-hash-only import).
    if not checksums:
        checksums = _checksums_via_dvcfs(path, out_path)
    if not checksums:
        raise ManifestError(
            f"{path} records no per-file md5s to reuse (neither an inline `files:` "
            f"list nor a resolvable DVC directory hash). Build the manifest directly "
            f"with `archivefs manifest {url} --download` instead."
        )
    return DvcSource(
        url=url,
        checksums=checksums,
        out_path=out_path,
        fs_config=dict(dep.get("fs_config") or {}),
    )


def _checksums_via_dvcfs(dvc_file: Path, out_path: str) -> dict[str, str]:
    """Per-file md5s from DVC's own filesystem view, keyed by out-relative path.

    Reads the checksums DVC has on hand (no re-hash, no materialisation needed); returns
    ``{}`` if DVC is not importable or the path is not tracked, so the caller can raise
    a single, clear error rather than a traceback.
    """
    try:
        from dvc.api import DVCFileSystem
    except ImportError:
        return {}
    # The repo root is above the .dvc file; DVC finds it by walking up.
    repo_root = dvc_file.resolve().parent
    try:
        fs = DVCFileSystem(str(repo_root))
        # `out_path` is relative to the .dvc's directory; DVCFileSystem keys from the
        # repo root, so address it there and strip that prefix back off.
        abs_out = (dvc_file.parent / out_path).resolve()
        rel_out = str(abs_out.relative_to(fs.repo.root_dir))
        found = fs.find(rel_out, detail=True)
    except Exception:  # noqa: BLE001 - any DVC-side failure degrades to "nothing reused"
        return {}
    prefix = f"{rel_out}/"
    out: dict[str, str] = {}
    for repo_path, info in found.items():
        if info.get("type") != "file":
            continue
        md5 = info.get("md5")
        if md5 and repo_path.startswith(prefix):
            out[repo_path[len(prefix):]] = md5
    return out
