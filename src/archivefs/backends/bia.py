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
from urllib.parse import parse_qs, quote, urlparse
from xml.etree import ElementTree

from archivefs.base import ArchiveFileSystem
from archivefs.credentials import Credential
from archivefs.errors import ArchiveNotFoundError, NotSupportedError

logger = logging.getLogger(__name__)

#: The two shapes a sharing key takes in a URL, both of which must be masked before
#: the URL can appear in a message.
#:
#: **The query parameter** is how archivefs sends it. **The path** is how BioStudies
#: serves an unreleased study: ``.private/<key[:2]>/<key[2:]>/…``, which makes the URL
#: itself the capability — a ranged GET on it needs no key parameter at all.
#:
#: Both, and not just the obvious one: the first version of this masked only the path
#: and a test caught the query parameter leaking through the very error handler added
#: to stop it.
KEY_IN_URL = (
    re.compile(r"([?&]key=)[^&\s]+"),
    re.compile(r"(\.private/)[0-9a-fA-F]{2}/[0-9a-fA-F][0-9a-fA-F-]{8,}"),
)

#: Column names a depositor might use for a per-file MD5 in their submitted file
#: list. BioStudies' file table is **depositor-defined** — measured against
#: S-BIAD3080, whose columns are ``Assay``, ``Tissue``, ``Donor``, ``Target`` — so
#: there is no fixed schema to read and the only options are to guess at names or to
#: throw away a checksum the depositor took the trouble to publish. Guessing is the
#: better failure: a wrong digest here fails *loudly* at the next `archivefs check`
#: or `dvc status`, where discarding a right one silently costs a full extra read of
#: the deposit (three, under `dvc import-url` — see docs/decisions.md D33).
MD5_COLUMNS = (
    "md5",
    "md5sum",
    "md5_checksum",
    "file_md5",
    "content_md5",
    # Last, and only if it is md5-shaped: a `checksum` column could hold a sha256 or
    # a CRC, and calling one of those an md5 would be a lie DVC acts on.
    "checksum",
)

#: 32 lowercase hex digits and nothing else. The shape check is what makes reading an
#: unschema'd column safe: a sha256 is 64, a CRC32 is 8, and a truncated paste is
#: neither.
MD5_VALUE = re.compile(r"^[0-9a-f]{32}$")

DEFAULT_API_URL = "https://www.ebi.ac.uk/biostudies/api/v1"
#: The Embassy S3 bucket holding BIA's own conversions. Anonymous, no signing.
DEFAULT_S3_URL = "https://uk1s3.embassy.ebi.ac.uk/bia-integrator-data"

#: BioStudies accessions come in more shapes than S-BIAD: S-BSST, S-EPMC, and
#: ArrayExpress-style E-MTAB-1234 are all real, public studies. This only has to
#: catch obvious garbage — ``/studies/{acc}/info`` is the authoritative existence
#: check — so it stays deliberately loose. Case-sensitive on purpose: the API 404s
#: on ``s-biad599``, and accepting it here only buys a misleading "no such study".
ACCESSION_RE = re.compile(r"^[A-Z]-[A-Z]{4,}[-\d][-\w]*$")

#: Rows per page. The server caps ``length`` at exactly 1000 and answers an
#: over-large request with an **empty list and ``recordsFiltered: 0``** rather than
#: an error or a clamp — measured: 1000 returns 1000 rows, 2000 returns nothing. So
#: "ask for everything" is not a strategy, and this must never be raised.
PAGE_SIZE = 1000
#: Ceiling on pages for one *file listing*. A study with more than this many files
#: does not exist today (the largest seen is 8237); it bounds a server that ignores
#: `start`, which would otherwise page forever.
MAX_LISTING_PAGES = 500
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
        info = (
            self.fs._get_json(
                self.fs._keyed(
                    f"{self.fs.api_url}/studies/{accession}/info", accession
                )
            )
            or {}
        )
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
                self.fs._keyed(
                    f"{self.fs.api_url}/files/{accession}"
                    f"?start={start}&length={length}",
                    accession,
                )
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
        nameless = 0
        expected = 0
        start = 0

        for page_number in range(MAX_LISTING_PAGES + 1):
            if page_number == MAX_LISTING_PAGES:
                raise ArchiveNotFoundError(
                    f"listing {accession} did not terminate within "
                    f"{MAX_LISTING_PAGES} pages of {PAGE_SIZE} rows. Refusing to "
                    f"keep paging a listing that will not end."
                )
            page, advertised = self._page(accession, start, PAGE_SIZE)
            if not expected:
                # Pin the total from the first response that offers one. Taking it
                # from *every* page lets a final empty page (recordsFiltered: 0)
                # quietly disarm the completeness check below.
                expected = advertised or int(
                    self.fs.study_info(accession).get("files") or 0
                )
            if not page:
                break
            fetched += len(page)
            for entry in page:
                path = entry.get("path") or entry.get("Name")
                if not path:
                    nameless += 1
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
        if duplicates or skipped_directories or nameless:
            logger.info(
                "%s: %d file(s), after collapsing %d duplicate path(s), skipping %d "
                "directory entr(y/ies) and %d row(s) with no path, in the study "
                "record",
                accession,
                len(rows),
                duplicates,
                skipped_directories,
                nameless,
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
        """One object, or the directory it turns out to be. One request.

        The directory test is ``path in directories``, **not** "the listing returned
        anything": S3 answers a *partial-name* prefix with the siblings that begin
        with it, so ``…/0177`` looks populated when only ``…/01776a3a-…/`` exists.
        Getting this wrong makes ``exists``/``isdir`` return confident nonsense that
        disagrees with ``ls`` — and it matters in practice, because OME-Zarr chunk
        directories are named ``0,1,10,100,…``, so a *missing* chunk ``1`` would
        resolve as a directory whenever ``10`` exists. zarr expects a
        FileNotFoundError there and treats it as the fill value; a directory gets it
        an IsADirectoryError instead.
        """
        directories, objects = self._list(path, delimiter="/")
        for entry in objects:
            if entry["name"] == path:
                return entry
        if path in directories:
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
    #: BioStudies shares an unreleased study through a link carrying ``?key=<uuid>``
    #: — the reviewer link a depositor sends out. Undocumented in the REST API and
    #: verified against it (D31): every endpoint answers 403 without the key and 200
    #: with it, as a **query parameter only** (an ``X-Session-Token`` header is
    #: refused). BioStudies' own name for it, in the ``/info`` payload, is ``seckey``.
    #:
    #: Scoped per accession, because that is the shape of the thing: one key opens
    #: one study, not an account. ``secret=True`` keeps it out of
    #: ``registry.Backend.config``, so ``--fs-config key=…`` dies inside DVC's own
    #: validator and ``-o key=…`` is refused here — which is what lets the *same*
    #: `.dvc` file serve a reviewer holding a key and everyone else after release.
    credentials: ClassVar[list] = [
        Credential(
            name="key",
            env=("BIA_KEY",),
            files=("~/.config/archivefs/bia.json",),
            file_key="keys",
            scoped=True,
            required=False,
            # A capability, not an identity: BioStudies issues it per study so the
            # depositor can mail it to reviewers. The only credential archivefs will
            # put into a bundle — see docs/reviewer-access.md.
            shareable=True,
            help=(
                "the key from a BioStudies sharing link "
                "(…/studies/S-BIAD3080?key=<uuid>). Only needed before the study "
                "is released; harmless afterwards."
            ),
        )
    ]
    views: ClassVar[dict[str, type[BIAView]]] = {
        "submitted": SubmittedView,
        "derived": DerivedView,
    }

    def __init__(
        self,
        view: str | None = None,
        api_url: str | None = None,
        s3_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        view = self._setting("view", view, "submitted")
        api_url = self._setting("api_url", api_url, DEFAULT_API_URL)
        s3_url = self._setting("s3_url", s3_url, DEFAULT_S3_URL)
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
        #: derived-view listings, keyed by directory path. The lazy view does not
        #: go through _members(), so it does not inherit that cache — and a
        #: study-level listing costs ~11s, which find()/walk()/DVC would otherwise
        #: pay again on every visit. Guarded by the base's member lock, for the same
        #: reason it has one: DVC runs one filesystem across a thread pool. (The
        #: worst a race could do here is duplicate a request, but the asymmetry
        #: would be a trap for the next person.)
        self._derived_listings: dict[str, list[dict]] = {}

    # --------------------------------------------------------------- metadata

    def study_info(self, accession: str) -> dict:
        """Cached ``/studies/{accession}/info``, which is also the existence check."""
        accession = self._strip_protocol(accession).split("/")[0]
        if accession not in self._studies:
            self._studies[accession] = SubmittedView(self).study_info(accession)
        return self._studies[accession]

    def invalidate_cache(self, path: str | None = None) -> None:
        """Also drop the study metadata and any derived-view listings."""
        super().invalidate_cache(path)
        if path is None:
            self._studies.clear()
            self._derived_listings.clear()
            return
        container, _ = self._split(self._strip_protocol(path))
        self._studies.pop(container, None)
        for cached in [
            key
            for key in self._derived_listings
            if key == container or key.startswith(f"{container}/")
        ]:
            self._derived_listings.pop(cached, None)

    def _check_accession(self, accession: str) -> None:
        if not ACCESSION_RE.match(accession):
            raise ArchiveNotFoundError(
                f"{accession!r} is not a BioStudies accession. Expected e.g. "
                f"bia://S-BIAD599 (BioImages) or bia://S-BSST123."
            )

    @staticmethod
    def _published_md5(entry: dict) -> tuple[str | None, str | None]:
        """``(md5, which column it came from)`` if the depositor published one."""
        normalised = {
            str(key).strip().lower().replace(" ", "_").replace("-", "_"): (key, value)
            for key, value in entry.items()
        }
        for candidate in MD5_COLUMNS:
            found = normalised.get(candidate)
            if not found:
                continue
            column, value = found
            text = str(value).strip().lower()
            if MD5_VALUE.match(text):
                return text, str(column)
        return None, None

    def _make_info(
        self, accession: str, path: str, entry: dict, base: str
    ) -> dict:
        size = entry.get("Size")
        md5, md5_column = self._published_md5(entry)
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
            "size": int(size) if isinstance(size, (int, str)) and str(size).isdigit()
            else None,
            # BioStudies itself publishes no per-file digest, and the FIRE ETag is
            # not one: measured, it is `"187eb43-64dfaf2d57cc9"` where 0x187eb43 is
            # the byte count — Apache's size-mtime token, which says nothing about
            # the bytes. But the file table's columns are the *depositor's*, so a
            # conscientious one can put an md5 there, and that is a published
            # checksum in the sense that matters.
            "md5": md5,
            "checksum": md5,
            "checksum_source": (
                f"md5 published by the depositor in the {md5_column!r} column of "
                f"the submitted file list"
                if md5
                else "none — BioStudies publishes no per-file checksum for submitted "
                "files, and the FIRE ETag is a size-mtime token, not a digest; "
                "supply a .chiro manifest for download-free verification"
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
        with self._member_lock:
            if path not in self._derived_listings:
                try:
                    self._derived_listings[path] = self.view.children(path)
                except ArchiveNotFoundError:
                    # fsspec requires ls() of a *file* to return that one entry.
                    # The base does it ("a file lists as itself") and this override
                    # must too, or find()/walk()/du()/get(recursive=True) raise on
                    # every derived file path — and those are exactly the calls
                    # DVC's directory pull makes. walk() cannot rescue it either,
                    # since on_error defaults to "raise" here and
                    # ArchiveNotFoundError is a FileNotFoundError.
                    info = self.info(path)
                    if info.get("type") == "file":
                        return [info] if detail else [info["name"]]
                    raise
            entries = self._derived_listings[path]
        # A copy: the base hands back a fresh list every call, and a caller that
        # mutates what it got should not be editing our cache.
        return list(entries) if detail else [entry["name"] for entry in entries]

    def info(self, path: str, **kwargs: Any) -> dict:
        if self.view_name != "derived" or not self._strip_protocol(path):
            return super().info(path, **kwargs)
        path = self._strip_protocol(path)
        accession = path.split("/")[0]
        self._check_accession(accession)
        if path == accession:
            return self._container_info(accession)
        # A listing of the parent, if we already have one, answers this without a
        # request — which is the common case, since fsspec walks before it reads.
        parent = path.rsplit("/", 1)[0]
        for entry in self._derived_listings.get(parent, ()):
            if entry["name"] == path:
                return entry
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
                # Masked, not dropped: knowing the deposit is served from a private
                # path is useful, and the path itself embeds the study's key. DVC
                # keeps only a fixed field set out of `info` (`dvc_data`'s
                # `Meta.from_info`), so this never reached a `.dvc` file — but it
                # does reach `archivefs stat`, and stdout ends up in logs too.
                "relpath": self._redact(info.get("relPath") or "") or None,
            },
        }

    # A manifest describes a *deposit*. The derived view is BIA's conversion of
    # one, keyed by image UUID, and it already publishes md5s — so there is nothing
    # for the chiro layer to resolve against, and reaching for it would surface the
    # lazy view's internal "should not be reached" error to anyone who ran
    # `archivefs check -o view=derived`.
    def manifest_path_for(self, container: str) -> str | None:
        if self.view_name == "derived":
            return None
        return super().manifest_path_for(container)

    def manifest_for(self, container: str) -> Any:
        if self.view_name == "derived":
            return None
        return super().manifest_for(container)

    def drift(self, container: str) -> Any:
        if self.view_name == "derived":
            return None
        return super().drift(container)

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

    def _key_for(self, accession: str) -> str | None:
        """This study's sharing key, if one is configured for it."""
        return self.creds.get("key", scope=accession)

    def _keyed(self, url: str, accession: str) -> str:
        """Add ``?key=`` if we hold a key for ``accession``.

        Sent whenever we have one, without first checking whether the study is
        public — measured, a public study answers 200 with a stale or even a
        nonsense key, so there is nothing to be gained by asking twice and a
        depositor's key can stay in place after release without breaking the pull.
        """
        key = self._key_for(accession)
        if not key:
            return url
        return f"{url}{'&' if '?' in url else '?'}key={quote(key)}"

    def _redact(self, url: str) -> str:
        for pattern in KEY_IN_URL:
            url = pattern.sub(r"\1<key>", url)
        return url

    @classmethod
    def grants_from_link(cls, url: str) -> list[tuple[str, str | None, str]]:
        """Pull the study and its key out of a BioStudies sharing link.

        The link is the thing a depositor already has and already sends —
        ``…/studies/S-BIAD3080?key=<uuid>`` — so a reviewer pasting it back is the
        shortest path from "I was emailed access" to "dvc pull works", with no second
        artefact to mislay.

        The accession is read out of the path so the key lands scoped to one study.
        A link with a key and no recognisable accession still yields a grant, with
        ``None`` for the scope: the resolver treats a flat entry as a catch-all, and
        refusing a working key because we could not name what it opens would be
        pedantry. The caller says which it got.
        """
        parsed = urlparse(url.strip())
        keys = parse_qs(parsed.query).get("key") or []
        if not keys or not keys[0]:
            return []
        parts = [part for part in parsed.path.split("/") if part]
        if parsed.scheme == cls.protocol and parsed.netloc:
            # bia://S-BIAD3080?key=… — the accession is the netloc, not a path part.
            parts = [parsed.netloc, *parts]
        accession = next(
            (
                candidate
                for previous, candidate in zip(parts, parts[1:])
                if previous in ("studies", "files") and ACCESSION_RE.match(candidate)
            ),
            None,
        )
        if accession is None and parts and ACCESSION_RE.match(parts[0]):
            accession = parts[0]
        return [("key", accession, keys[0])]

    def _auth_remediation(self, status: int, url: str) -> str:
        """Two different 403s, and conflating them sends people the wrong way.

        Without a key, "check the credentials" is unactionable — there is nothing to
        check. With one, the embargo explanation is wrong, because access was
        attempted and refused. So the two are answered separately, and the accession
        comes out of the URL so the message can name the study it means.
        """
        if status not in (401, 403):
            return super()._auth_remediation(status, url)
        match = re.search(r"/(?:studies|files)/([^/?]+)", url)
        accession = match.group(1) if match else "S-BIADnnnn"
        if self._key_for(accession):
            return (
                f"the sharing key archivefs holds for {accession} was refused. It "
                f"came from {self.creds.describe(['key'])}. Sharing keys are per "
                f"study and can be revoked, so check the link is current and that "
                f"its key is filed under {accession} and not another accession."
            )
        return (
            f"archivefs read {self.archive_name} anonymously, so it can only see "
            f"released studies. Most often {accession} is embargoed, not yet "
            f"released, restricted or withdrawn — check its landing page for a "
            f"release date.\n"
            f"  If it is unreleased and you have a sharing link for it "
            f"(.../studies/{accession}?key=<uuid>), archivefs can use that key:\n"
            f"    export BIA_KEY=<uuid>\n"
            f"  or, for more than one study, ~/.config/archivefs/bia.json at "
            f"chmod 600:\n"
            f'    {{"keys": {{"{accession}": "<uuid>"}}}}\n'
            f"  The key is a secret: it cannot be passed as an option and never "
            f"reaches a .dvc file. See docs/credentials.md."
        )

    def _data_url(self, info: dict) -> str:
        return self.view.data_url(info)
