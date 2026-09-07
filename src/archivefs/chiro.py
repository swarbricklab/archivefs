"""``.chiro``-first resolution — the unifying move.

``docs/design.md`` calls this the central abstraction: *a ``.chiro`` resolver plus
per-backend byte-fetchers*, rather than per-backend filesystems each reinventing
listing and hashing. When a deposit carries a manifest, the manifest **is** the
directory listing and the checksums — download-free by construction — and the
backend shrinks to "fetch the bytes this manifest points at".

What that buys, per archive:

* **GEO**, which publishes no per-file digest, gains real download-free md5s.
* **EGA**, whose ``ls`` is authenticated and whose version token is weak, gains a
  stable content identity that does not depend on EGA's own metadata.
* **Zenodo**, which already has md5s, gains the *two-path model*: a flat deposit can
  materialise as a tidy per-sample workspace tree.
* **All of them** gain drift detection: a deposit that has gained, lost or changed a
  file relative to its manifest is a fact worth surfacing, not hiding.

Parsing and verification are delegated to the ``chirograph`` package rather than
reimplemented. That is a deliberate decision (``docs/decisions.md`` D5): the format
spec warns that "a reimplementation that canonicalises *almost* identically produces
non-matching hashes silently", and chirograph is dependency-free, so importing it
costs nothing but a package. Without it installed, manifests are ignored with a
warning and everything falls back to native listing — which is the promise archivefs
makes: it works with Chirograph, and it works without.
"""

from __future__ import annotations

import logging
import posixpath
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from archivefs.errors import ManifestError, MissingExtraError
from archivefs.registry import PASSTHROUGH_INFO_KEYS

if TYPE_CHECKING:  # pragma: no cover
    from archivefs.base import ArchiveFileSystem

logger = logging.getLogger(__name__)

#: The format's compound extension; auto-discovery globs for it.
MANIFEST_SUFFIX = ".chiro.json"

__all__ = [
    "MANIFEST_SUFFIX",
    "DriftReport",
    "apply_manifest",
    "discover_manifest",
    "have_chirograph",
    "load_manifest",
    "verify_materialised",
]


def have_chirograph() -> bool:
    import importlib.util

    return importlib.util.find_spec("chirograph") is not None


def _chirograph() -> Any:
    try:
        import chirograph
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise MissingExtraError(
            "chiro",
            "chirograph",
            "reading .chiro.json manifests",
        ) from exc
    return chirograph


# --------------------------------------------------------------------- loading


def discover_manifest(member_names: list[str]) -> str | None:
    """Pick the manifest out of a deposit's file names, or return ``None``.

    Auto-discovery is a courtesy (the format spec demotes it below explicit
    reference and registry resolution), so it is deliberately conservative: with
    more than one candidate it refuses to guess.
    """
    candidates = sorted(n for n in member_names if n.endswith(MANIFEST_SUFFIX))
    if not candidates:
        return None
    if len(candidates) > 1:
        # Accession-derived names are the convention, so several manifests in one
        # deposit most likely means several records' manifests were bundled
        # together. Guessing would silently verify against the wrong one.
        raise ManifestError(
            f"found {len(candidates)} .chiro.json manifests in this deposit "
            f"({', '.join(candidates)}); auto-discovery will not guess. Name one "
            f"explicitly with the `chiro` storage option."
        )
    return candidates[0]


def load_manifest(
    fs: ArchiveFileSystem, path: str, info: dict | None = None
) -> Any:
    """Fetch and parse one manifest through ``fs``.

    Resolution order for ``path``, first hit wins:

    1. ``info`` — the manifest is a member of the deposit being listed, and its
       ``info`` dict was passed in. This short-circuit has to exist: such a
       manifest is fetched *while* that deposit's listing is being built, so going
       back through ``cat_file`` would re-enter the listing and recurse forever.
    2. A URL or an absolute path — a manifest that lives somewhere else entirely
       (a registry, a hosted copy, a git checkout). This is the case the format's
       ``chiro://`` references exist for.
    3. **A local file**, including a plain relative name. This is the manifest
       committed next to the ``.dvc`` that references it: the common case for a
       deposit you do not control, where the archive publishes no checksum and
       cannot be made to carry your manifest. Without it the only portable
       reference is a URL, which means hosting a copy of a file that belongs in
       the analysis repo. See ``docs/decisions.md`` D18.
    4. A path inside the filesystem, resolved from its root (not the deposit) —
       kept for a manifest that is in the deposit but absent from the listing.

    Note that 3 precedes 4, so a local file shadows a same-named remote path. In
    practice there is nothing to shadow: a manifest that really is a member of the
    deposit arrives via 1, and a deposit-relative bare name never resolved under 4
    anyway.
    """
    chirograph = _chirograph()
    searched: list[str] = []
    try:
        if info is not None:
            payload = fs._fetch_range(info, 0, fs._size_of(info))
        elif "://" in path or path.startswith("/"):
            import fsspec

            searched.append(path)
            with fsspec.open(path, "rb") as handle:
                payload = handle.read()
        else:
            local = Path(path).expanduser()
            # Absolute in the message even when the setting is relative: "looked
            # for foo.json" does not say *where*, and the answer is whichever
            # directory dvc happened to run in.
            searched.append(f"{local.resolve()} (local file)")
            if local.is_file():
                payload = local.read_bytes()
            else:
                searched.append(f"{path} (inside the archive)")
                payload = fs.cat_file(path)
    except FileNotFoundError as exc:
        # Deliberately *not* re-raised as a FileNotFoundError: fsspec's walk()
        # swallows those, so an unfetchable manifest would surface as an empty
        # directory — a listing that silently loses every file in the deposit.
        places = ", then ".join(searched)
        raise ManifestError(
            f"{path} is named as this deposit's manifest but could not be "
            f"fetched ({exc})."
            + (f" Looked for {places}." if places else "")
            + " Fix or remove the `chiro` setting, or pass "
            "chiro=False to fall back to the archive's own listing."
        ) from exc
    try:
        return chirograph.loads(payload)
    except Exception as exc:  # chirograph raises its own SchemaError family
        raise ManifestError(f"{path} is not a usable .chiro.json: {exc}") from exc


# ---------------------------------------------------------------------- drift


@dataclass
class DriftReport:
    """How a deposit differs from its manifest.

    Both directions matter and mean different things: a file in the deposit but
    not the manifest is usually an unrecorded addition; a file in the manifest but
    not the deposit means the deposit lost something the manifest promises, which
    is the serious case.
    """

    manifest_path: str
    provenance_type: str | None = None
    #: manifest entries whose bytes are not in the deposit
    missing_upstream: list[str] = field(default_factory=list)
    #: deposit files the manifest does not mention (excluding the manifest itself)
    unrecorded: list[str] = field(default_factory=list)
    #: entries whose size disagrees with the deposit's own metadata
    size_mismatches: list[dict] = field(default_factory=list)
    #: entries where both sides publish a checksum and they disagree
    checksum_mismatches: list[dict] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (
            self.missing_upstream
            or self.unrecorded
            or self.size_mismatches
            or self.checksum_mismatches
        )

    def summary(self) -> str:
        if self.clean:
            return f"{self.manifest_path}: deposit matches the manifest"
        bits = []
        if self.missing_upstream:
            bits.append(
                f"{len(self.missing_upstream)} promised file(s) absent upstream"
            )
        if self.unrecorded:
            bits.append(f"{len(self.unrecorded)} deposit file(s) not in the manifest")
        if self.size_mismatches:
            bits.append(f"{len(self.size_mismatches)} size mismatch(es)")
        if self.checksum_mismatches:
            bits.append(f"{len(self.checksum_mismatches)} checksum mismatch(es)")
        return f"{self.manifest_path}: " + ", ".join(bits)


# ------------------------------------------------------------------ resolution


def apply_manifest(
    fs: ArchiveFileSystem,
    container: str,
    native: dict[str, dict],
    manifest_path: str,
    manifest: Any = None,
) -> tuple[dict[str, dict], DriftReport]:
    """Re-key a native listing by the manifest, returning the listing and drift.

    The manifest's ``relpath`` becomes the workspace path (so a flat deposit can
    materialise as a tree) while the fetch still goes through the native member
    identified by ``source_path`` — the two-path model, with archivefs supplying
    the byte-fetcher half.
    """
    if manifest is None:
        manifest = load_manifest(fs, manifest_path)
    by_source = {fs.relpath(container, path): path for path in native}
    members: dict[str, dict] = {}
    drift = DriftReport(
        manifest_path=manifest_path,
        provenance_type=getattr(manifest, "provenance_type", None),
    )
    accounted: set[str] = {fs.relpath(container, manifest_path)}

    for entry in manifest.files:
        source = entry.source
        native_path = by_source.get(source)
        path = posixpath.join(container, entry.relpath)
        info: dict[str, Any] = {
            "name": path,
            "type": "file",
            "size": entry.size,
            "md5": entry.md5,
            "checksum": entry.md5,
            "checksum_type": manifest.hash_algorithm.upper(),
            "checksum_source": (
                f"{manifest_path} ({manifest.provenance_type}) — download-free"
            ),
            "chiro": {
                "manifest": manifest_path,
                "provenance_type": manifest.provenance_type,
                "record_accession": manifest.record_accession,
                "body_hash": manifest.body_hash,
                "hash_algorithm": manifest.hash_algorithm,
                "relpath": entry.relpath,
                "source_path": source,
                "relocated": entry.relocated,
                "retrieved": manifest.retrieved,
            },
        }
        if native_path is None:
            # The manifest promises bytes the deposit does not have. Surfaced now
            # and refused on read, rather than failing obscurely mid-pull.
            info["missing_upstream"] = True
            drift.missing_upstream.append(entry.relpath)
        else:
            accounted.add(source)
            native_info = native[native_path]
            # Carry everything the backend needs to fetch, and its own metadata.
            # Derived from the archive profiles, so a new backend's metadata block
            # is carried through without anyone remembering to add it here.
            for key in PASSTHROUGH_INFO_KEYS:
                if key in native_info:
                    info.setdefault(key, native_info[key])
            info["source_native_path"] = native_path
            native_size = native_info.get("size")
            if native_size is not None and native_size != entry.size:
                drift.size_mismatches.append(
                    {
                        "relpath": entry.relpath,
                        "manifest_size": entry.size,
                        "archive_size": native_size,
                    }
                )
            native_md5 = native_info.get("md5")
            comparable = native_md5 and manifest.hash_algorithm == "md5"
            if comparable and native_md5 != entry.md5:
                drift.checksum_mismatches.append(
                    {
                        "relpath": entry.relpath,
                        "manifest_md5": entry.md5,
                        "archive_md5": native_md5,
                    }
                )
        members[path] = info

    containers = getattr(manifest, "containers", None)
    if containers:
        # A container manifest may pin *one* ZIP of a multi-ZIP record, listed at the
        # record (so the delegate's `source_path`s resolve). The record's *other* ZIPs
        # are then in the native listing but were never claimed by this pin — they are
        # not drift. Scope "unrecorded" to files under a container this manifest
        # actually declares; a member missing from a claimed container still shows.
        roots = tuple(c.source_root for c in containers)
        drift.unrecorded = sorted(
            source
            for source in by_source
            if source not in accounted
            and any(source == r or source.startswith(f"{r}/") for r in roots)
        )
    else:
        drift.unrecorded = sorted(
            source for source in by_source if source not in accounted
        )
    if not drift.clean:
        logger.warning("chiro drift in %s: %s", container, drift.summary())
    return members, drift


# ---------------------------------------------------------------- verification


def verify_materialised(
    fs: ArchiveFileSystem,
    container: str,
    root: str,
    *,
    use_source_layout: bool = False,
    **kwargs: Any,
) -> Any:
    """Verify a materialised tree against the deposit's manifest.

    This is the "download-and-verify is one operation" part of the plan: the
    verification logic is chirograph's, not a second implementation living here.
    Returns chirograph's ``VerificationReport``.
    """
    # Through the guard, so an absent package is a MissingExtraError naming the
    # install rather than a traceback: "missing extras are not import errors".
    chirograph = _chirograph()
    chiro_verify = chirograph.verify

    manifest = fs.manifest_for(container)
    if manifest is None:
        raise ManifestError(
            f"{container} has no discoverable .chiro.json, so there is nothing to "
            f"verify against. Point at one with the `chiro` storage option, or "
            f"verify against the archive's own checksums instead."
        )
    return chiro_verify.verify_tree(
        manifest, root, use_source_layout=use_source_layout, **kwargs
    )
