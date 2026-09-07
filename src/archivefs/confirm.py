"""``archivefs confirm`` — does the live deposit still match this manifest, cheaply?

A depositor prepares a `.chiro` manifest (often from local files, *before* uploading)
and later needs to answer: did the archive receive exactly those bytes? For loose files
that is easy — compare the archive's published per-file md5. For a **ZIP deposit** it is
not: a member has no published checksum anywhere. What a ZIP exposes cheaply is a tail
read of its *central directory* — per member ``(path, size, crc32)`` — plus the whole
container's md5, which the archive publishes. That ``(container_md5, crc32)`` pair
confirms a member **without downloading its bytes**, and it survives re-zipping
(non-deterministic packaging changes the container bytes but not the members'
``(path, size, crc32)``). This is chirograph's schema-2.0 container model
(``container-model.md`` §6, §10).

Confirm is the download-free counterpart to ``archivefs update``: update *regenerates*
the manifest (which for a ZIP needs member md5s, i.e. a download) to ask "is there a
newer version"; confirm *cross-checks* the pinned version against the manifest as
cheaply as the container format allows. The strategy is chosen by
``chirograph.Container.confirm_class`` (chirograph owns the format→class map):

* ``indexed`` (``zip``)   — tail-read the central directory, check ``crc32`` + size. No
  member bytes fetched.
* ``sequential`` (``tar``) / ``stream`` (``tar.gz`` …, or any unrecognised format) — no
  cheap per-member check exists, so a content check means reading the member (``tar``)
  or the whole stream (compressed). Confirm does that **only** with ``download=True``;
  without it, it reports the member as size-checked-only and says why.

The routing is the ``concept`` → backend map a ``chiro://`` pin uses (D36), but the
listing is at the manifest's ``record_accession`` — the *version* it pins, not the
concept's latest — because confirm asks "does the deposit I pinned still match?", a
version-pinned question.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from archivefs.chiro import _chirograph
from archivefs.errors import ManifestError
from archivefs.registry import protocol_for_concept
from archivefs.update import _load_manifest

__all__ = ["ConfirmReport", "MemberResult", "confirm"]

#: Read size for the ``download=True`` md5 loop, matching the writer's capture stride.
_HASH_CHUNK = 4 * 1024 * 1024


@dataclass
class MemberResult:
    """One manifest entry's verdict against the live deposit."""

    relpath: str
    #: ``matched`` · ``crc32`` · ``size`` · ``md5`` · ``missing`` · ``size-only``
    status: str
    #: How it was checked: ``crc32`` (download-free), ``md5`` (bytes read), ``size``.
    via: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("matched", "size-only")


@dataclass
class ContainerResult:
    """One container's whole-file md5 verdict."""

    source_root: str
    confirm_class: str
    expected_md5: str
    actual_md5: str | None

    @property
    def ok(self) -> bool:
        return self.actual_md5 == self.expected_md5


@dataclass
class ConfirmReport:
    """The outcome of confirming a manifest against a live deposit."""

    manifest_ref: str
    record_accession: str
    download_free: bool = True
    containers: list[ContainerResult] = field(default_factory=list)
    members: list[MemberResult] = field(default_factory=list)
    #: live files under a declared container that the manifest does not list
    unrecorded: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            all(c.ok for c in self.containers)
            and all(m.ok for m in self.members)
            and not self.unrecorded
        )

    def summary(self) -> str:
        matched = sum(m.status == "matched" for m in self.members)
        size_only = sum(m.status == "size-only" for m in self.members)
        bad = [m for m in self.members if not m.ok]
        bits = [f"{matched} matched"]
        if size_only:
            bits.append(f"{size_only} size-only")
        if bad:
            bits.append(f"{len(bad)} changed/missing")
        if self.unrecorded:
            bits.append(f"{len(self.unrecorded)} unrecorded")
        bad_containers = [c for c in self.containers if not c.ok]
        if bad_containers:
            bits.append(f"{len(bad_containers)} container md5 mismatch")
        note = "" if self.download_free else " (a download was needed)"
        verb = "matches" if self.ok else "DIFFERS from"
        return (
            f"{self.manifest_ref}: deposit {self.record_accession} {verb} the "
            f"manifest — {', '.join(bits)}{note}"
        )


def _member_of(delegate: Any, accession: str, info: dict) -> dict | None:
    """The live container facts for one file, via the backend's own hook."""
    return delegate.container_of(accession, info)


def _hash_member(delegate: Any, path: str) -> str:
    digest = hashlib.md5()  # noqa: S324 - md5 is the archive-published algorithm
    with delegate.open(path, "rb") as handle:
        while True:
            chunk = handle.read(_HASH_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def confirm(
    target: str, options: dict[str, Any] | None = None, *, download: bool = False
) -> ConfirmReport:
    """Cross-check ``target``'s manifest against the live deposit it pins.

    ``target`` is a ``chiro://md5:<digest>`` reference (resolved via the store), a
    ``chiro://`` / path to a committed ``.chiro.json``, or a URL. Reads only metadata
    (central-directory tail reads) for ZIP members and loose files that publish a
    checksum; falls back to reading bytes only where the container format offers no
    cheap check *and* ``download=True`` was given.
    """
    _chirograph()  # raises MissingExtraError naming the install if absent
    options = dict(options or {})

    manifest, _path = _load_manifest(target)

    concept = getattr(manifest, "concept", None)
    if not concept:
        raise ManifestError(
            f"{target} carries no `concept`, so archivefs cannot tell which archive "
            f"to confirm against. Regenerate it with a current `archivefs manifest`."
        )
    routed = protocol_for_concept(concept)
    if routed is None:
        raise ManifestError(
            f"{target} names concept {concept!r}, whose scheme belongs to no "
            f"archivefs backend."
        )
    protocol, concept_accession = routed
    # The *version* the manifest pins, not the concept's latest: confirm asks whether
    # the deposit I pinned still matches, so it lists exactly that version.
    accession = getattr(manifest, "record_accession", None) or concept_accession

    import fsspec

    # A container manifest needs the delegate to expand its ZIPs, so the live listing
    # produces the same per-member paths (and crc32s) the manifest recorded. The knob is
    # backend-specific (`extract_zips` on Zenodo); set it only where the backend accepts
    # it, and never override a caller's value. skip_instance_cache so confirm reads the
    # live archive, not a warm listing from an earlier call.
    if getattr(manifest, "containers", None):
        import inspect

        init = fsspec.get_filesystem_class(protocol).__init__
        if "extract_zips" in inspect.signature(init).parameters:
            options.setdefault("extract_zips", True)
    delegate = fsspec.filesystem(protocol, skip_instance_cache=True, **options)

    live = delegate.find(accession, detail=True)
    live_by_source = {
        delegate.relpath(accession, path): info
        for path, info in live.items()
        if info.get("type") == "file"
    }

    report = ConfirmReport(
        manifest_ref=target,
        record_accession=str(accession),
    )
    # Container class by source_root, and the container md5 we must confirm once.
    class_by_root = {c.source_root: c for c in manifest.containers}
    live_container_md5: dict[str, str | None] = {}
    accounted: set[str] = set()

    for entry in manifest.files:
        source = entry.source
        container = _container_for(manifest.containers, source)
        got = live_by_source.get(source)
        if got is not None:
            accounted.add(source)

        if got is None:
            report.members.append(
                MemberResult(entry.relpath, "missing", "—", "not in the live deposit")
            )
            continue

        if container is None:
            # A loose file: compare the archive's published md5 (download-free) if it
            # has one; otherwise size, or a real read under --download.
            report.members.append(
                _confirm_loose(delegate, entry, got, download, report)
            )
            continue

        # A container member. Record the live container md5 once (for the container
        # verdict), then check the member by its class.
        facts = _member_of(delegate, accession, got) or {}
        live_container_md5.setdefault(container.source_root, facts.get("md5"))
        report.members.append(
            _confirm_member(delegate, entry, got, container, facts, download, report)
        )

    # Container-level md5 verdicts, once each.
    for root, container in class_by_root.items():
        report.containers.append(
            ContainerResult(
                source_root=root,
                confirm_class=container.confirm_class,
                expected_md5=container.md5,
                actual_md5=live_container_md5.get(root),
            )
        )

    # Live files under a declared container that the manifest omits.
    roots = tuple(class_by_root)
    report.unrecorded = sorted(
        source
        for source in live_by_source
        if source not in accounted
        and any(source == r or source.startswith(f"{r}/") for r in roots)
    )
    return report


def _container_for(containers: list[Any], source: str) -> Any | None:
    """The container whose ``source_root`` delivers ``source``, or ``None`` (loose)."""
    for container in containers:
        root = container.source_root
        if source == root or source.startswith(f"{root}/"):
            return container
    return None


def _confirm_member(
    delegate: Any,
    entry: Any,
    got: dict,
    container: Any,
    facts: dict,
    download: bool,
    report: ConfirmReport,
) -> MemberResult:
    """Verify one container member by its container's confirm class."""
    live_size = got.get("size")
    if live_size is not None and live_size != entry.size:
        return MemberResult(
            entry.relpath, "size", "size",
            f"manifest {entry.size} vs deposit {live_size}",
        )

    if container.confirm_class == "indexed":
        # Class A: download-free. The live crc32 comes from the central directory.
        live_crc = facts.get("crc32")
        if entry.crc32 is None or live_crc is None:
            return MemberResult(entry.relpath, "size-only", "size",
                                "no crc32 to compare (size checked)")
        if live_crc == entry.crc32:
            return MemberResult(entry.relpath, "matched", "crc32")
        return MemberResult(entry.relpath, "crc32", "crc32",
                            f"manifest {entry.crc32} vs deposit {live_crc}")

    # Class B/C: no cheap per-member check. A content check means a read.
    if not download:
        report.download_free = False  # we could not fully confirm without bytes
        return MemberResult(
            entry.relpath, "size-only", "size",
            f"{container.confirm_class} format — re-run with --download for a "
            f"content check",
        )
    report.download_free = False
    native = got.get("name")
    live_md5 = _hash_member(delegate, native)
    if live_md5 == entry.md5:
        return MemberResult(entry.relpath, "matched", "md5")
    return MemberResult(entry.relpath, "md5", "md5",
                        f"manifest {entry.md5} vs deposit {live_md5}")


def _confirm_loose(
    delegate: Any,
    entry: Any,
    got: dict,
    download: bool,
    report: ConfirmReport,
) -> MemberResult:
    """Verify a loose (non-container) file: published md5, else size, else a read."""
    live_size = got.get("size")
    if live_size is not None and live_size != entry.size:
        return MemberResult(entry.relpath, "size", "size",
                            f"manifest {entry.size} vs deposit {live_size}")
    live_md5 = got.get("md5")
    if live_md5:
        if live_md5 == entry.md5:
            return MemberResult(entry.relpath, "matched", "md5")
        return MemberResult(entry.relpath, "md5", "md5",
                            f"manifest {entry.md5} vs deposit {live_md5}")
    if not download:
        report.download_free = False
        return MemberResult(entry.relpath, "size-only", "size",
                            "archive publishes no checksum — re-run with --download")
    report.download_free = False
    computed = _hash_member(delegate, got.get("name"))
    if computed == entry.md5:
        return MemberResult(entry.relpath, "matched", "md5")
    return MemberResult(entry.relpath, "md5", "md5",
                        f"manifest {entry.md5} vs deposit {computed}")
