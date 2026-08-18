"""``bia://`` — the BioImage Archive (EMBL-EBI), over BioStudies.

The imaging tier of the deposit-side split rule in ``docs/design.md``: raw reads and
genotypes to EGA, processed matrices to GEO/Zenodo, **spatial imaging to BIA**. For
a Visium/Xenium study the H&E/DAPI/morphology images live here while the counts live
elsewhere, cross-linked — so this is the backend that makes a multi-part deposit
pullable as one thing.

``docs/plan.md`` gated it on "verify the storage/access model first"; that
verification is
[`docs/evidence/bia-access-model-2026-08-17.md`](../../../docs/evidence/bia-access-model-2026-08-17.md),
and this is the implementation of what it found.

URL grammar
-----------
::

    bia://S-BIAD599                    # study -> directory of submitted files
    bia://S-BIAD599/<path>             # one file, at its deposited path
    bia://S-BIAD1                      # any BioStudies accession works

Two **views** of one accession, the same seam GEO introduced:

``submitted`` (default)
    What the depositor uploaded, served over HTTPS from EBI's FIRE object store.
    Byte-exact, range-readable, **no checksums published** — the GEO problem, with
    the same answer (a sidecar ``.chiro`` manifest, which for our own deposits we
    control).
``derived``
    BIA's own conversions — OME-Zarr pyramids and thumbnails — in an anonymously
    listable S3 bucket, keyed by image UUID. Different bytes from the submission,
    but it publishes **real MD5 ETags**, so this view has download-free content
    checksums that no other archivefs backend gets for free.

Four things the live service does that a reasonable implementation would get wrong
(all measured, see the evidence note):

* **The file listing is paginated** DataTables-style (``start``/``length``) and
  ``recordsFiltered`` is the *total*, not the page. The default page is **5 rows**,
  so reading the first response as the listing silently truncates a 584-file study
  to five.
* **Asking for too many rows returns an empty list**, HTTP 200, with
  ``recordsFiltered: 0`` — not an error. So "give me everything" is not a strategy.
* **Paging must advance by the page size asked for, not by the rows returned.** At
  1000 rows a page that is the same thing; at 500 it is not, and pages overlap. The
  listing pages at 1000 and reconciles the rows fetched against the advertised total,
  refusing rather than handing back a short listing.
* **A study record can list the same path twice** (same file, different
  ``file_position``; seven do in S-BIAD1), so ``recordsFiltered`` legitimately
  exceeds the number of distinct files. Duplicates are collapsed and reported, not
  treated as loss.
* **``isDirectory`` is the string** ``"false"``, which is truthy.

And two more that would produce wrong bytes rather than an error: the FIRE path is
**not** derivable from the accession (two different sharding schemes are in use, so
it comes from the API), and deposited paths contain characters like ``#`` that
truncate an unquoted URL.
"""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar
from urllib.parse import quote
from xml.etree import ElementTree

from archivefs.base import ArchiveFileSystem
from archivefs.errors import ArchiveNotFoundError, NotSupportedError

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://www.ebi.ac.uk/biostudies/api/v1"
#: The Embassy S3 bucket holding BIA's own conversions. Anonymous, no signing.
DEFAULT_S3_URL = "https://uk1s3.embassy.ebi.ac.uk/bia-integrator-data"

#: BioStudies accessions: S-BIAD (BioImages), S-BSST (generic), and friends.
ACCESSION_RE = re.compile(r"^[A-Z]-[A-Z]{4}\d+$", re.I)

#: Rows per page. The server caps ``length`` at exactly 1000 and answers an
#: over-large request with an **empty list and ``recordsFiltered: 0``** rather than
#: an error or a clamp — measured: 1000 returns 1000 rows, 2000 returns nothing. So
#: "ask for everything" is not a strategy, and this must never be raised.
PAGE_SIZE = 1000
#: Ceiling on pages for one S3 listing. A converted study runs to tens of thousands
#: of objects — S-BIAD599's is over 60,000 — so listings are delimited to one level
#: and this only ever bites on a pathological prefix or a looping server.
MAX_S3_PAGES = 200


def is_true(value: Any) -> bool:
    """Interpret BioStudies' stringly-typed booleans.

    ``isDirectory`` comes back as the *string* ``"false"``, and ``bool("false")`` is
    ``True`` — which would file every entry in the archive under "directory".
    """
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() == "true"


class BIAView:
    """One resolvable view of a BioImage Archive accession."""

    name = "abstract"

    def __init__(self, fs: BIAFileSystem) -> None:
        self.fs = fs

    def list_members(self, accession: str) -> dict[str, dict]:
        raise NotImplementedError

    def data_url(self, info: dict) -> str:
        raise NotImplementedError


class SubmittedView(BIAView):
    """What the depositor uploaded, served from FIRE over HTTPS."""

    name = "submitted"

    def study_info(self, accession: str) -> dict:
        """The study's own metadata — and, crucially, where its bytes live.

        ``relPath``/``httpLink`` are read rather than derived: BIA shards FIRE by
        accession in at least two different ways (``S-BIAD/599/S-BIAD599`` but
        ``S-BIAD/S-BIAD0-99/S-BIAD1``), so a rule inferred from one study fetches
        404s for another. This is also the existence check, because the *files*
        endpoint answers an unknown accession with an empty list rather than a 404.
        """
        info = self.fs._get_json(f"{self.fs.api_url}/studies/{accession}/info") or {}
        if not info.get("relPath") and not info.get("httpLink"):
            raise ArchiveNotFoundError(
                f"BioStudies has no study {accession} (or it is not public). Note "
                f"that its files endpoint answers an unknown accession with an "
                f"empty list, so an empty study and a missing one look alike there; "
                f"this checked /studies/{accession}/info, which does distinguish."
            )
        return info

    def files_base(self, accession: str) -> str:
        info = self.fs.study_info(accession)
        link = info.get("httpLink") or (
            f"https://ftp.ebi.ac.uk/biostudies/fire/{info['relPath']}"
        )
        return f"{link.rstrip('/')}/Files"

    def _page(self, accession: str, start: int, length: int) -> tuple[list[dict], int]:
        payload = (
            self.fs._get_json(
                f"{self.fs.api_url}/files/{accession}?start={start}&length={length}"
            )
            or {}
        )
        return payload.get("data") or [], int(payload.get("recordsFiltered") or 0)

    def list_members(self, accession: str) -> dict[str, dict]:
        """Page the whole listing, and reconcile it against the advertised total.

        Two details are load-bearing, both established by measurement rather than
        by reading the API docs (``docs/decisions.md`` D13):

        * **advance by the requested page size, not by the number of rows
          returned.** Paging at 1000 retrieves exactly ``recordsFiltered`` rows;
          an earlier attempt at 500 produced overlapping pages and appeared to lose
          files.
        * **the count and the distinct-path count legitimately differ.** A study
          record can list the same path twice (same file, different
          ``file_position``); on S-BIAD1 seven do. So completeness is checked
          against *rows fetched*, and duplicate paths are collapsed and reported,
          not treated as an error.
        """
        base = self.files_base(accession)
        rows: dict[str, dict] = {}
        fetched = 0
        duplicates = 0
        skipped_directories = 0
        expected = 0
        start = 0

        while True:
            page, expected = self._page(accession, start, PAGE_SIZE)
            if not page:
                break
            fetched += len(page)
            for entry in page:
                path = entry.get("path") or entry.get("Name")
                if not path:
                    continue
                if is_true(entry.get("isDirectory")):
                    skipped_directories += 1
                    continue
                if path in rows:
                    duplicates += 1
                    continue
                rows[path] = entry
            # Advance by the page size we asked for. Advancing by len(page) is the
            # obvious thing and is wrong here.
            start += PAGE_SIZE
            if expected and start >= expected:
                break

        if expected and fetched < expected:
            # Never hand back a short listing: a directory import would quietly
            # write fewer files than the study holds and exit 0.
            raise ArchiveNotFoundError(
                f"{accession} advertises {expected} files but its paginated listing "
                f"only yielded {fetched} rows. Refusing to import a study whose "
                f"listing is incomplete — retry, and report the accession upstream "
                f"if it persists."
            )
        if duplicates or skipped_directories:
            logger.info(
                "%s: %d file(s), after collapsing %d duplicate path(s) and skipping "
                "%d directory entr(y/ies) in the study record",
                accession,
                len(rows),
                duplicates,
                skipped_directories,
            )
        if not rows:
            raise ArchiveNotFoundError(
                f"{accession} exists but its file listing is empty."
            )
        return {
            f"{accession}/{path}": self.fs._make_info(accession, path, entry, base)
            for path, entry in rows.items()
        }

    def data_url(self, info: dict) -> str:
        return info["url"]


class DerivedView(BIAView):
    """BIA's own conversions (OME-Zarr, thumbnails) from the Embassy S3 bucket.

    Different bytes from the submission — this is *not* the deposit — but it is the
    form you actually analyse for imaging, and unlike everything else in this
    backend it publishes a real content checksum.

    **Listed one level at a time, with an S3 delimiter.** An OME-Zarr pyramid is one
    object per chunk, so a single study's converted tree runs to tens or hundreds of
    thousands of objects: enumerating it to answer ``ls`` took over five minutes for
    S-BIAD599 and produced a listing nobody asked for. A delimited listing is one
    request per directory, which is also what makes this view usable for the thing
    it exists for — pointing zarr at a pyramid and reading the chunks it wants.
    """

    name = "derived"

    #: S3 ListObjectsV2 responses are namespaced XML.
    NS: ClassVar[dict[str, str]] = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}

    def _list(self, prefix: str, delimiter: str = "/") -> tuple[list[str], list[dict]]:
        """One level of the bucket under ``prefix``: (subdirectories, objects)."""
        directories: list[str] = []
        objects: list[dict] = []
        token = ""
        seen_tokens: set[str] = set()
        for page in range(MAX_S3_PAGES + 1):
            if page == MAX_S3_PAGES:
                raise ArchiveNotFoundError(
                    f"listing {prefix!r} in BIA's derived-data bucket did not "
                    f"terminate within {MAX_S3_PAGES} pages "
                    f"({len(objects)} objects so far). Refusing to keep paging; "
                    f"list a narrower path."
                )
            url = (
                f"{self.fs.s3_url}?list-type=2&max-keys=1000"
                f"&prefix={quote(prefix, safe='')}"
            )
            if delimiter:
                url += f"&delimiter={quote(delimiter, safe='')}"
            if token:
                url += f"&continuation-token={quote(token, safe='')}"
            response = self.fs._request("GET", url)
            # stdlib ElementTree over a TLS connection to a known EBI endpoint,
            # parsing a response bounded by max-keys. It resolves no external
            # entities, so the XXE half of S314 does not apply; pulling in
            # defusedxml to harden a bucket listing against its own operator would
            # be a dependency for its own sake.
            root = ElementTree.fromstring(response.content)  # noqa: S314
            for common in root.findall("s3:CommonPrefixes", self.NS):
                child = common.findtext("s3:Prefix", "", self.NS)
                if child:
                    directories.append(child.rstrip("/"))
            for contents in root.findall("s3:Contents", self.NS):
                key = contents.findtext("s3:Key", "", self.NS)
                if not key or key.endswith("/"):
                    continue
                objects.append(self._object_info(contents, key))
            if root.findtext("s3:IsTruncated", "false", self.NS) != "true":
                break
            token = root.findtext("s3:NextContinuationToken", "", self.NS)
            if not token:
                break
            if token in seen_tokens:
                # A server that repeats its continuation token would otherwise keep
                # us paging the same response forever. Stopping quietly would be
                # worse than hanging, though: we would return a *partial* listing as
                # if it were complete.
                raise ArchiveNotFoundError(
                    f"listing {prefix!r} in BIA's derived-data bucket repeated its "
                    f"continuation token, so the listing cannot be completed and "
                    f"what was fetched ({len(objects)} object(s)) may be partial. "
                    f"Refusing to treat it as the whole directory."
                )
            seen_tokens.add(token)
        return directories, objects

    def _object_info(self, contents: Any, key: str) -> dict:
        accession = key.split("/", 1)[0]
        etag = contents.findtext("s3:ETag", "", self.NS).strip('"')
        size = contents.findtext("s3:Size", "", self.NS)
        # A multipart upload's ETag is "<hex>-<parts>" and is NOT an md5 of the
        # object. Every object sampled was single-part, but calling one that is not
        # an md5 would be exactly the kind of lie the rest of this package refuses.
        is_md5 = bool(etag) and "-" not in etag
        return {
            "name": key,
            "type": "file",
            "size": int(size) if size.isdigit() else None,
            "md5": etag if is_md5 else None,
            "checksum": etag if is_md5 else None,
            "checksum_type": "MD5" if is_md5 else None,
            "checksum_source": (
                "S3 ETag (single-part upload, so it is the object's MD5) — "
                "download-free"
                if is_md5
                else "none — this object's ETag is a multipart digest, which is "
                "not an MD5 of its bytes"
            ),
            "archive_version": contents.findtext("s3:LastModified", None, self.NS),
            "url": f"{self.fs.s3_url}/{quote(key, safe='/')}",
            "bia": {
                "accession": accession,
                "view": self.name,
                "s3_key": key,
                "derived": True,
            },
        }

    def children(self, path: str) -> list[dict]:
        """Immediate children of ``path`` (an accession or a directory under one)."""
        directories, objects = self._list(f"{path.rstrip('/')}/")
        if not directories and not objects:
            accession = path.split("/", 1)[0]
            if path == accession:
                raise ArchiveNotFoundError(
                    f"no converted data for {accession} in BIA's derived-data "
                    f"bucket. Not every study has conversions; the submitted files "
                    f"are at bia://{accession} with the default view."
                )
            raise ArchiveNotFoundError(f"{path} is not in the derived-data bucket")
        entries = [
            {"name": directory, "type": "directory", "size": 0}
            for directory in directories
        ]
        entries.extend(objects)
        return sorted(entries, key=lambda entry: entry["name"])

    def info(self, path: str) -> dict:
        """One object, or the directory it turns out to be. One request."""
        directories, objects = self._list(path, delimiter="/")
        for entry in objects:
            if entry["name"] == path:
                return entry
        if directories or objects:
            # Keys exist *under* this prefix, so it is a directory.
            return {"name": path, "type": "directory", "size": 0}
        raise ArchiveNotFoundError(
            f"{path} is not in BIA's derived-data bucket. Converted data is keyed "
            f"by image UUID under the accession; list bia://{path.split('/')[0]} "
            f"with view='derived' to see what is there."
        )

    def list_members(self, accession: str) -> dict[str, dict]:
        raise NotSupportedError(
            "the derived view lists lazily, one directory at a time — a converted "
            "study is one object per zarr chunk, and enumerating it up front takes "
            "minutes. This method should not be reached; use ls()/info()."
        )

    def data_url(self, info: dict) -> str:
        return info["url"]


class BIAFileSystem(ArchiveFileSystem):
    """Read-only fsspec filesystem over the BioImage Archive.

    Parameters
    ----------
    view:
        ``"submitted"`` (default) for the deposited files, ``"derived"`` for BIA's
        OME-Zarr/thumbnail conversions. See the module docstring.
    api_url, s3_url:
        Overrides for the BioStudies API and the derived-data bucket.
    """

    protocol = "bia"
    archive_name = "BioImage Archive"
    #: Anonymous: public BioStudies data needs no credential.
    credentials: ClassVar[list] = []
    views: ClassVar[dict[str, type[BIAView]]] = {
        "submitted": SubmittedView,
        "derived": DerivedView,
    }

    def __init__(
        self,
        view: str = "submitted",
        api_url: str = DEFAULT_API_URL,
        s3_url: str = DEFAULT_S3_URL,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if view not in self.views:
            raise ValueError(
                f"unknown BIA view {view!r}; known views: "
                f"{', '.join(sorted(self.views))}"
            )
        self.api_url = api_url.rstrip("/")
        self.s3_url = s3_url.rstrip("/")
        self.view_name = view
        self.view = self.views[view](self)
        #: accession -> study /info payload
        self._studies: dict[str, dict] = {}

    # --------------------------------------------------------------- metadata

    def study_info(self, accession: str) -> dict:
        """Cached ``/studies/{accession}/info``, which is also the existence check."""
        accession = self._strip_protocol(accession).split("/")[0]
        if accession not in self._studies:
            self._studies[accession] = SubmittedView(self).study_info(accession)
        return self._studies[accession]

    def invalidate_cache(self, path: str | None = None) -> None:
        """Also drop the study metadata, which carries the payload location."""
        super().invalidate_cache(path)
        if path is None:
            self._studies.clear()
            return
        container, _ = self._split(self._strip_protocol(path))
        self._studies.pop(container, None)

    def _check_accession(self, accession: str) -> None:
        if not ACCESSION_RE.match(accession):
            raise ArchiveNotFoundError(
                f"{accession!r} is not a BioStudies accession. Expected e.g. "
                f"bia://S-BIAD599 (BioImages) or bia://S-BSST123."
            )

    def _make_info(
        self, accession: str, path: str, entry: dict, base: str
    ) -> dict:
        size = entry.get("Size")
        # Everything BioStudies knows about the file *except* the plumbing — organism,
        # channels, labels, gene identifiers. No other archive here hands out
        # per-file experimental metadata, and throwing it away would be a waste.
        attributes = {
            key: value
            for key, value in entry.items()
            if key
            not in {"Name", "path", "Size", "type", "isDirectory", "file_position"}
            and value not in (None, "")
        }
        return {
            "name": f"{accession}/{path}",
            "type": "file",
            "size": int(size) if isinstance(size, int | str) and str(size).isdigit()
            else None,
            # BIA publishes no per-file digest for submitted files. Say so rather
            # than offering the FIRE ETag, which is an inode-size-mtime token and
            # says nothing about the bytes.
            "md5": None,
            "checksum": None,
            "checksum_source": (
                "none — BioStudies publishes no per-file checksum for submitted "
                "files; supply a .chiro manifest for download-free verification"
            ),
            # quote: deposited imaging paths really do contain '#', which truncates
            # an unquoted URL at what urlsplit reads as a fragment.
            "url": f"{base}/{quote(path, safe='/')}",
            "bia": {
                "accession": accession,
                "view": self.view_name,
                "file_type": entry.get("file_type") or entry.get("type"),
                "section": entry.get("Section"),
                "attributes": attributes or None,
            },
        }

    def _load_members(self, container: str) -> dict[str, dict]:
        self._check_accession(container)
        return self.view.list_members(container)

    # ------------------------------------------------- the lazily-listed view
    #
    # The derived view does not go through _members(): a converted study is one
    # object per zarr chunk, so "load every member of this container" is minutes of
    # requests for a listing nobody asked for. It answers ls() and info() directly,
    # one S3 delimiter-listing per directory — and because everything else in the
    # base derives from info() (docs/decisions.md D10), overriding those two is
    # enough for exists/isdir/isfile/checksum/cat_file/open/find/glob to follow.

    def ls(self, path: str = "", detail: bool = True, **kwargs: Any) -> list:
        if self.view_name != "derived" or not self._strip_protocol(path):
            return super().ls(path, detail=detail, **kwargs)
        path = self._strip_protocol(path)
        self._check_accession(path.split("/")[0])
        entries = self.view.children(path)
        return entries if detail else [entry["name"] for entry in entries]

    def info(self, path: str, **kwargs: Any) -> dict:
        if self.view_name != "derived" or not self._strip_protocol(path):
            return super().info(path, **kwargs)
        path = self._strip_protocol(path)
        accession = path.split("/")[0]
        self._check_accession(accession)
        if path == accession:
            return self._container_info(accession)
        return self.view.info(path)

    def _list_root(self) -> list[dict]:
        raise NotSupportedError(
            "the BioImage Archive has no listable root: address a study, e.g. "
            "bia://S-BIAD599. (Searching is a different operation from listing — "
            "see BIAFileSystem.search.)"
        )

    def _container_info(self, container: str) -> dict:
        info = self.study_info(container)
        return {
            "name": container,
            "type": "directory",
            "size": 0,
            "bia": {
                "accession": container,
                "view": self.view_name,
                "files": info.get("files"),
                "released": info.get("released"),
                "modified": info.get("modified"),
                "public": info.get("isPublic"),
                "relpath": info.get("relPath"),
            },
        }

    # --------------------------------------------------------------- discovery

    def search(self, query: str, size: int = 10) -> list[dict]:
        """BioStudies search, restricted to the BioImages collection."""
        payload = self._get_json(
            f"{self.api_url}/search?query={quote(query)}&pageSize={size}"
            f"&collection=BioImages"
        )
        return [
            {
                "accession": hit.get("accession"),
                "title": hit.get("title"),
                "files": hit.get("files"),
                "released": hit.get("release_date"),
            }
            for hit in (payload or {}).get("hits", [])
        ]

    # --------------------------------------------------------------- transfer

    def _data_url(self, info: dict) -> str:
        return self.view.data_url(info)
