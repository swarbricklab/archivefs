"""``geo://`` — NCBI GEO supplementary (processed/derived) files.

Third in the build order (``docs/plan.md`` step 3) and, as the design predicted,
nearly free once the base exists: anonymous HTTPS, byte-exact, range-readable.

::

    geo://GSE176078                    # a series' supplementary files
    geo://GSE176078/<filename>         # one of them
    geo://GSM5354513                   # a sample's supplementary files
    geo://GPL570                       # a platform's, if it has any

**The SRA seam.** GEO's difficulty is not uniform: supplementary files are the
tractable half, and raw reads defer to SRA with all its toolkit-orchestration and
SRA→FASTQ round-trip problems. Our own raw reads live in EGA, never GEO, so the
hard half is excluded by our data model rather than by engineering. But other labs
(non-human data) *do* put raw reads in GEO/SRA, so the seam is drawn now and only
the seam: an accession has **views**, ``supplementary`` is one of them, and
``raw`` is a registered view that refuses with an explanation. Adding SRA later
means writing one view class — no redesign, and no speculative orchestration code
in the meantime.

**Checksums.** GEO publishes sizes, not per-file checksums (``filelist.txt`` has
name/time/size/type and no digest). ``info()`` says so rather than inventing a
token: ``md5`` is ``None`` and ``checksum_source`` explains why. For our *own*
deposits the gap is closed by a sidecar ``.chiro`` manifest — see
:mod:`archivefs.chiro`, which augments any backend's listing when a manifest is
discoverable.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Any, ClassVar
from urllib.parse import quote, unquote, urljoin

from archivefs.base import ArchiveFileSystem
from archivefs.errors import ArchiveNotFoundError, NotSupportedError

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://ftp.ncbi.nlm.nih.gov/geo"

#: ``GSE176078`` -> ``series/GSE176nnn/GSE176078``. NCBI buckets records by
#: replacing the last three digits with ``nnn`` (so ``GPL570`` -> ``GPLnnn``).
ACCESSION_RE = re.compile(r"^(?P<prefix>GSE|GSM|GPL|GDS)(?P<digits>\d+)$", re.I)
_DIRECTORY = {
    "GSE": "series",
    "GSM": "samples",
    "GPL": "platforms",
    "GDS": "datasets",
}


def accession_dir(accession: str) -> str:
    """The GEO FTP directory for an accession, without a trailing slash."""
    match = ACCESSION_RE.match(accession)
    if not match:
        raise ArchiveNotFoundError(
            f"{accession!r} is not a GEO accession. Expected GSE/GSM/GPL/GDS "
            f"followed by digits, e.g. geo://GSE176078."
        )
    prefix = match.group("prefix").upper()
    digits = match.group("digits")
    bucket = f"{prefix}{digits[:-3]}nnn"
    return f"{_DIRECTORY[prefix]}/{bucket}/{prefix}{digits}"


class _IndexParser(HTMLParser):
    """Pulls hrefs out of an Apache-style directory index.

    Only used when ``filelist.txt`` is absent; it is present for most records and
    is both cheaper and richer.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag != "a":
            return
        for name, value in attrs:
            if name == "href" and value:
                self.hrefs.append(value)


def parse_filelist(text: str) -> dict[str, dict]:
    """Parse GEO's ``filelist.txt``.

    Tab-separated, with a header, and one wrinkle worth capturing: an ``Archive``
    row is followed by ``File`` rows describing that archive's *contents*. So a
    single fetch tells us what is inside ``GSE..._RAW.tar`` without reading the
    tar — worth recording as metadata even though we do not present tar interiors
    as directories (tar has no central directory, so that would not be
    download-free, and a ``.tar.gz`` cannot be ranged into at all).
    """
    entries: dict[str, dict] = {}
    current_archive: str | None = None
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 4:
            continue
        kind, name, when, size = (f.strip() for f in fields[:4])
        file_type = fields[4].strip() if len(fields) > 4 else None
        record = {
            "name": name,
            "size": int(size) if size.isdigit() else None,
            "modified": when or None,
            "file_type": file_type,
        }
        if kind.lower() == "archive":
            current_archive = name
            record["contains"] = []
            entries[name] = record
        elif current_archive and name not in entries:
            # a member of the preceding archive, not a top-level file
            entries[current_archive]["contains"].append(record)
        else:
            entries.setdefault(name, record)
    return entries


class GEOView:
    """One resolvable view of a GEO accession.

    The seam the plan asks for. A view answers two questions — what is in this
    accession, and where do its bytes live — which is exactly the split the design
    doc identifies as irreducible per backend.
    """

    name = "abstract"

    def __init__(self, fs: GEOFileSystem) -> None:
        self.fs = fs

    def list_members(self, accession: str) -> dict[str, dict]:
        raise NotImplementedError

    def data_url(self, info: dict) -> str:
        raise NotImplementedError


class SupplementaryView(GEOView):
    """Processed/derived files under ``<record>/suppl/`` over public HTTPS."""

    name = "supplementary"

    def directory_url(self, accession: str) -> str:
        return f"{self.fs.base_url}/{accession_dir(accession)}/suppl/"

    def list_members(self, accession: str) -> dict[str, dict]:
        """List the directory, then enrich it from ``filelist.txt``.

        The directory index is authoritative and ``filelist.txt`` is *not*: for
        GSE176078 the filelist describes only ``GSE176078_RAW.tar`` and its
        members, omitting two top-level supplementary files that the index shows.
        Trusting the filelist alone would silently drop them — so it is used only
        to fill in exact sizes, file types and archive contents for the names the
        index found. (The index's own sizes are human-rounded, "532M", and are
        deliberately discarded.)
        """
        directory = self.directory_url(accession)
        members = self._from_index(accession, directory)
        if not members:
            raise ArchiveNotFoundError(
                f"{accession} has no supplementary files at {directory}. If you "
                f"are after raw reads, GEO defers those to SRA, which archivefs "
                f"does not orchestrate (see the geo:// module docstring)."
            )
        self._enrich_from_filelist(accession, directory, members)
        return members

    def _enrich_from_filelist(
        self, accession: str, directory: str, members: dict[str, dict]
    ) -> None:
        try:
            response = self.fs._request("GET", urljoin(directory, "filelist.txt"))
        except ArchiveNotFoundError:
            logger.debug("%s has no filelist.txt; sizes resolve lazily", accession)
            return
        entries = parse_filelist(response.text)
        # A `File` row is attributed to the preceding `Archive` row, because that
        # is what GEO's format means — but the format cannot distinguish a genuine
        # top-level file listed after an archive block. The directory index can:
        # anything the index also shows is top-level, so promote it back out.
        for archive in [e for e in entries.values() if e.get("contains")]:
            promoted = [
                member
                for member in archive["contains"]
                if f"{accession}/{member['name']}" in members
            ]
            for member in promoted:
                archive["contains"].remove(member)
                entries.setdefault(member["name"], member)

        for name, entry in entries.items():
            info = members.get(f"{accession}/{name}")
            if info is None:
                continue
            info["size"] = entry.get("size")
            info["geo"]["file_type"] = entry.get("file_type")
            contained = [m["name"] for m in entry.get("contains", [])]
            info["geo"]["contains"] = contained or None
            if entry.get("modified"):
                info["geo"]["modified"] = entry["modified"]
                info["archive_version"] = str(entry["modified"])

    def _from_index(self, accession: str, directory: str) -> dict[str, dict]:
        try:
            response = self.fs._request("GET", directory)
        except ArchiveNotFoundError:
            # Distinguish "no such accession" from "nothing supplementary", which
            # are very different problems for the person reading the error.
            record_url = f"{self.fs.base_url}/{accession_dir(accession)}/"
            try:
                self.fs._request("HEAD", record_url)
            except ArchiveNotFoundError:
                raise ArchiveNotFoundError(
                    f"GEO has no record {accession} (looked at {record_url}). Check "
                    f"the accession; note GEO series are GSE, samples GSM."
                ) from None
            raise ArchiveNotFoundError(
                f"{accession} exists but has no supplementary-file directory "
                f"({directory} is absent). Its data may be raw reads in SRA, which "
                f"archivefs does not orchestrate."
            ) from None

        parser = _IndexParser()
        parser.feed(response.text)
        members = {}
        for href in parser.hrefs:
            if href.startswith(("/", "?", "#", "http://", "https://")):
                continue  # parent link, sort links, HHS footer
            name = unquote(href).rstrip("/")
            if not name or name == "filelist.txt":
                continue
            members[f"{accession}/{name}"] = self.fs._make_info(
                accession, name, {"name": name}, directory
            )
        return members

    def data_url(self, info: dict) -> str:
        return info["url"]


class RawReadsView(GEOView):
    """Placeholder for "raw reads via SRA" — deliberately not implemented.

    Registered rather than absent so that the failure explains the situation
    instead of looking like a typo, and so the shape of the eventual
    implementation is already fixed: ``list_members`` returns run accessions,
    ``data_url`` points at whatever SRA transfer path is chosen.
    """

    name = "raw"

    def _refuse(self) -> None:
        raise NotSupportedError(
            "the geo:// 'raw' view is a seam, not an implementation: raw reads in "
            "GEO live in SRA, which needs toolkit orchestration and an SRA→FASTQ "
            "round-trip that is lossy. archivefs deliberately does not build that "
            "(docs/design.md §'GEO scope'). For human raw reads use ega://; if you "
            "need SRA, run the SRA Toolkit directly and deposit the result "
            "somewhere byte-exact."
        )

    def list_members(self, accession: str) -> dict[str, dict]:
        self._refuse()
        return {}

    def data_url(self, info: dict) -> str:
        self._refuse()
        return ""


class GEOFileSystem(ArchiveFileSystem):
    """Read-only fsspec filesystem over GEO supplementary files.

    Parameters
    ----------
    view:
        Which view of an accession to resolve. ``"supplementary"`` (default) is
        the byte-exact, anonymous, range-readable half of GEO. ``"raw"`` is
        registered and refuses — see :class:`RawReadsView`.
    base_url:
        Override the FTP-over-HTTPS mirror.
    """

    protocol = "geo"
    archive_name = "GEO"
    #: Anonymous: GEO supplementary files need no credential at all.
    credentials: ClassVar[list] = []
    views: ClassVar[dict[str, type[GEOView]]] = {
        "supplementary": SupplementaryView,
        "raw": RawReadsView,
    }

    def __init__(
        self,
        view: str = "supplementary",
        base_url: str = DEFAULT_BASE_URL,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if view not in self.views:
            raise ValueError(
                f"unknown GEO view {view!r}; known views: "
                f"{', '.join(sorted(self.views))}"
            )
        self.base_url = base_url.rstrip("/")
        self.view_name = view
        self.view = self.views[view](self)

    # --------------------------------------------------------------- metadata

    def _make_info(
        self, accession: str, name: str, entry: dict, directory: str
    ) -> dict:
        info: dict[str, Any] = {
            "name": f"{accession}/{name}",
            "type": "file",
            "size": entry.get("size"),
            # GEO publishes no per-file digest. Say so, loudly and in the payload,
            # instead of substituting something that is not a content checksum.
            "md5": None,
            "checksum": None,
            "checksum_source": (
                "none — GEO does not publish per-file checksums; supply a .chiro "
                "manifest for download-free verification"
            ),
            # quote: a "#" in a filename would otherwise truncate the URL at
            # what urljoin reads as a fragment.
            "url": urljoin(directory, quote(name)),
            "geo": {
                "accession": accession,
                "view": self.view_name,
                "file_type": entry.get("file_type"),
                "modified": entry.get("modified"),
                # From filelist.txt when available: what is inside an archive
                # member, without reading it.
                "contains": [m["name"] for m in entry.get("contains", [])] or None,
            },
        }
        if entry.get("modified"):
            # Weak, but honest, and it is what GEO gives: a conventional record
            # plus a modification time. Change detection, not content identity.
            info["archive_version"] = str(entry["modified"])
        return info

    def _load_members(self, container: str) -> dict[str, dict]:
        return self.view.list_members(container)

    def _list_root(self) -> list[dict]:
        raise NotSupportedError(
            "GEO has no listable root: address an accession, e.g. geo://GSE176078."
        )

    def info(self, path: str, **kwargs: Any) -> dict:
        """Like the base, but never returns an unknown size for a file.

        GEO's directory index gives human-rounded sizes, so the listing leaves
        ``size`` unset for files the filelist did not cover. A consumer asking
        about one specific file (DVC building a directory manifest, say) needs a
        real number, so it is resolved with a HEAD and cached on the member.
        """
        info = super().info(path, **kwargs)
        if info.get("type") == "file" and info.get("size") is None:
            self._size_of(info)
        return info

    def _size_of(self, info: dict) -> int:
        """Fall back to a HEAD when the listing had no size.

        The HTML index gives human-rounded sizes ("532M"), which are useless, so
        those are dropped at parse time and recovered here exactly.
        """
        size = info.get("size")
        if size is not None:
            return size
        response = self._request("HEAD", info["url"])
        length = response.headers.get("Content-Length", "")
        if not length.isdigit():
            raise OSError(
                f"GEO did not report a Content-Length for {info['name']}; cannot "
                f"read it without downloading the whole file."
            )
        info["size"] = int(length)
        if response.headers.get("Last-Modified"):
            info.setdefault("archive_version", response.headers["Last-Modified"])
        return info["size"]

    # --------------------------------------------------------------- transfer

    def _data_url(self, info: dict) -> str:
        return self.view.data_url(info)
