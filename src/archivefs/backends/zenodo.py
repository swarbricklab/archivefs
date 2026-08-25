"""``zenodo://`` — Zenodo records, addressed by record id or DOI.

Second in the build order (``docs/plan.md`` step 2) and, like EGA, written
standalone first; the shared base in :mod:`archivefs.base` was then factored from
the diff between this clean case and EGA's auth-complex one, so it reflects the
genuine common shape rather than a guess.

URL grammar
-----------
::

    zenodo://1486184                          # record -> directory
    zenodo://1486184/github-mark.zip          # one file
    zenodo://10.5281/zenodo.1486184           # version DOI (immutable)
    zenodo://10.5281/zenodo.1486183           # concept DOI -> latest version
    zip://::zenodo://1486184/github-mark.zip  # the ZIP's interior (fsspec chain)

The first four work anywhere a URL does, including the CLI. The chain is Python
only — ``fsspec.core.url_to_fs``/``fsspec.open`` — because chained storage options
nest per protocol and the CLI's flat ``-o key=value`` cannot express that; the CLI
equivalent is ``-o extract_zips=true`` on the ZIP's own path.

Zenodo is the reference case for a reason: every file comes with an MD5 in the
record metadata, so ``info()`` is download-free *and* content-verified, and a
version DOI is immutable — which is what makes the ZIP integrity argument below
sound rather than merely convenient.
"""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar
from urllib.parse import quote

from archivefs.base import ArchiveFileSystem
from archivefs.credentials import Credential
from archivefs.errors import (
    ArchiveNotFoundError,
    AuthenticationError,
    NotSupportedError,
)

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://zenodo.org/api"
#: Sandbox for rehearsing deposits; same API shape.
SANDBOX_API_URL = "https://sandbox.zenodo.org/api"

#: ``10.5281/zenodo.1486184``, and the record id inside it.
DOI_RE = re.compile(
    r"^(?P<prefix>10\.\d{4,9})/(?P<suffix>zenodo\.(?P<recid>\d+))$", re.I
)
RECORD_RE = re.compile(r"^\d+$")

#: Path components that are packaging cruft rather than deposit content. macOS
#: writes ``__MACOSX/._name`` resource forks into **30% of the Zenodo ZIPs
#: surveyed** (``docs/evidence/zenodo-zip-shape-2026-08-21.md``). Two reasons to
#: drop them and not merely tolerate them: nobody wants them in a workspace, and
#: with ``version_aware`` on they would land in the committed ``files:`` list and
#: in the tracked directory hash. They also mask the wrapper directory — a ZIP
#: holding ``outs/`` plus ``__MACOSX/`` has *two* top-level entries, so without
#: this the strip below would not fire on precisely the 30% that need it most.
ZIP_CRUFT = frozenset({"__MACOSX"})


def _is_cruft(interior: str) -> bool:
    return any(part in ZIP_CRUFT for part in interior.split("/"))


class ZenodoFileSystem(ArchiveFileSystem):
    """Read-only fsspec filesystem over Zenodo records.

    Parameters
    ----------
    token:
        Personal access token, needed only for restricted or embargoed records;
        public records — the common case — work anonymously. Resolved from
        ``storage_options`` then ``$ZENODO_TOKEN``.
    api_url:
        Override for the sandbox or a self-hosted InvenioRDM.
    extract_zips:
        Present ``*.zip`` members as directories, listing and reading their
        interiors through fsspec's ``ZipFileSystem``. Off by default — though not
        for the reason once given here: "some ZIPs are genuine archival units" is
        a sound worry in general and **unevidenced on Zenodo**, where none of 40
        sampled ZIPs had to stay zipped
        (``docs/evidence/zenodo-zip-shape-2026-08-21.md``). The default is now
        simply undecided; see ``docs/decisions.md`` D24.
        The explicit per-URL form is the fsspec chain
        ``zip://::zenodo://record/archive.zip``; this flag is the per-remote
        default, and both run the same code underneath.
    strip_components:
        How many leading path components to drop from a ZIP's interior, and *at
        most* — a component is only dropped while it is the archive's sole
        top-level entry and a directory. Defaults to 1, which turns the near
        universal ``archive.zip`` → ``archive/…`` wrapper into a workspace tree
        with no redundant level. Set to 0 to present the interior verbatim.
        See ``docs/decisions.md`` D24 for why this defaults on and why it checks
        rather than stripping blindly as ``tar --strip-components`` does.
    zip_limits:
        A :class:`~archivefs.zipsafe.ZipLimits` overriding the zip-bomb bounds.
        Genomics deposits are large; raise them deliberately rather than
        disabling the check.
    """

    protocol = "zenodo"
    archive_name = "Zenodo"
    credentials: ClassVar[list[Credential]] = [
        Credential(
            name="token",
            env=("ZENODO_TOKEN",),
            files=("~/.config/archivefs/zenodo.json",),
            secret=True,
            required=False,
            help=(
                "a Zenodo personal access token, needed only for restricted or "
                "embargoed records"
            ),
        )
    ]

    def __init__(
        self,
        api_url: str = DEFAULT_API_URL,
        extract_zips: bool = False,
        strip_components: int = 1,
        zip_limits: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.api_url = api_url.rstrip("/")
        self.extract_zips = extract_zips
        self.strip_components = int(strip_components)
        self.zip_limits = zip_limits
        self.token = self.creds.get("token")
        self.token_source = self.creds.source_of("token") if self.token else "anonymous"
        #: record spec or id -> record JSON
        self._records: dict[str, dict] = {}
        #: zip member path -> ZipFileSystem for *listing*, created lazily
        self._zip_views: dict[str, Any] = {}
        #: zip member path -> ZipFileSystem for *reading bytes*; see `_member_view`
        self._member_views: dict[str, Any] = {}
        #: zip member path -> the interior prefix `strip_components` removed
        self._zip_prefixes: dict[str, str] = {}

    def invalidate_cache(self, path: str | None = None) -> None:
        """Also drop the record JSON and any open ZIP views.

        The base only knows about the member listing, but a Zenodo record's JSON
        *contains* that listing — so without this, a refresh re-derived the same
        stale listing from a cache hit and looked like it had done nothing.
        """
        super().invalidate_cache(path)
        if path is None:
            self._records.clear()
            self._close_zip_views()
            self._zip_prefixes.clear()
            return
        container, _ = self._split(self._strip_protocol(path))
        record = self._records.pop(container, None)
        if record is not None:
            # One record answers to several spellings — record id, version DOI,
            # concept DOI — and dropping only the one the caller used leaves the
            # others serving the listing we were asked to forget.
            for alias in (
                str(record.get("id")),
                record.get("doi"),
                record.get("conceptdoi"),
                str(record.get("conceptrecid")),
            ):
                if alias:
                    self._records.pop(alias, None)
                    self._member_cache.pop(alias, None)
                    self._manifest_used.pop(alias, None)
                    self._manifests.pop(alias, None)
                    self._drift.pop(alias, None)
        stale = [p for p in self._zip_views if p.startswith(f"{container}/")]
        stale += [p for p in self._member_views if p.startswith(f"{container}/")]
        for zip_path in set(stale):
            self._zip_views.pop(zip_path, None)
            self._zip_prefixes.pop(zip_path, None)
            self._close_member_view(zip_path)

    # ------------------------------------------------------------------- auth

    def _auth_headers(self) -> dict:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def _auth_remediation(self, status: int, url: str) -> str:
        if not self.token:
            return (
                "this record is restricted or embargoed. If you have been granted "
                "access, put a personal access token in $ZENODO_TOKEN or in "
                "~/.config/archivefs/zenodo.json (mode 0600). It cannot go in DVC "
                "config or on the command line — see docs/credentials.md."
            )
        return (
            f"the token archivefs resolved from {self.token_source} was rejected. "
            f"Check it is current, and that your account has been granted access "
            f"to this record."
        )

    # ------------------------------------------------------------------ paths

    @staticmethod
    def split_path(path: str) -> tuple[str, str]:
        """Split a path into ``(record spec, member path)``.

        A DOI contains a slash, so this is not simply the first one:
        ``10.5281/zenodo.1486184/data.csv`` has a two-segment record spec.
        """
        parts = path.split("/")
        if parts and parts[0].startswith("10."):
            if len(parts) < 2:
                raise ArchiveNotFoundError(
                    f"{path!r} looks like a DOI prefix with no suffix; expected "
                    f"zenodo://10.5281/zenodo.<record_id>[/<file>]"
                )
            return "/".join(parts[:2]), "/".join(parts[2:])
        return parts[0], "/".join(parts[1:])

    def _split(self, path: str) -> tuple[str, str]:
        return self.split_path(path)

    # --------------------------------------------------------------- metadata

    def _resolve_record(self, spec: str) -> dict:
        """Fetch a record from a record id or a DOI (version or concept)."""
        if spec in self._records:
            return self._records[spec]

        match = DOI_RE.match(spec)
        if match:
            recid = match.group("recid")
        elif RECORD_RE.match(spec):
            recid = spec
        else:
            raise ArchiveNotFoundError(
                f"{spec!r} is not a Zenodo record id or DOI. Expected e.g. "
                f"zenodo://1486184 or zenodo://10.5281/zenodo.1486184."
            )

        # A concept id/DOI answers with a redirect to the latest version, which
        # requests follows; the payload then tells us which version we got.
        record = self._get_json(f"{self.api_url}/records/{recid}")
        self._records[spec] = record
        self._records[str(record.get("id", recid))] = record
        return record

    def _record_provenance(self, record: dict, requested: str | None = None) -> dict:
        """The immutable bits worth writing into a ``.dvc`` file."""
        concept_recid = record.get("conceptrecid")
        resolved_from_concept = bool(
            requested
            and concept_recid
            and requested in (str(concept_recid), record.get("conceptdoi"))
        )
        return {
            "record_id": str(record.get("id")),
            "doi": record.get("doi"),
            "concept_doi": record.get("conceptdoi"),
            "concept_record_id": str(concept_recid) if concept_recid else None,
            "revision": record.get("revision"),
            "updated": record.get("updated"),
            "access_right": (record.get("metadata") or {}).get("access_right"),
            "title": record.get("title"),
            # True when the URL named a concept and we resolved it to a specific
            # version: the caller asked for "latest", so `dvc update` is expected
            # to move, and the .dvc should record which version it actually took.
            "resolved_from_concept": resolved_from_concept,
        }

    def _load_members(self, container: str) -> dict[str, dict]:
        record = self._resolve_record(container)
        recid = str(record.get("id"))
        files = record.get("files")
        if files is None:
            # Some record views omit the file list; ask the dedicated endpoint.
            files = (self._get_json(f"{self.api_url}/records/{recid}/files") or {}).get(
                "entries", []
            )
        # A record you may not read comes back HTTP 200 with metadata and an
        # *empty* file list — not a 401, not a 403. Returning that as an empty
        # directory is the worst outcome available: `ls` says the record is
        # empty, and `dvc import-url` of the record records an empty .dir and
        # exits 0, so a pipeline imports nothing and reports success. Measured on
        # 10.5281/zenodo.18917100 (access_right: restricted).
        metadata = record.get("metadata") or {}
        access = metadata.get("access_right")
        if not files and access in ("restricted", "embargoed", "closed"):
            embargo = metadata.get("embargo_date")
            detail = f"record {recid} is {access}"
            if embargo:
                detail += f" until {embargo}"
            raise AuthenticationError(
                self.archive_name,
                f"{detail}; Zenodo served its metadata but no file list",
                self._auth_remediation(403, container),
                source=self.token_source if self.token else None,
                used_credentials=bool(self.token),
            )
        provenance = self._record_provenance(record, requested=container)
        if provenance["resolved_from_concept"]:
            # Worth saying out loud: a concept DOI means "latest", so a committed
            # .dvc pins the *bytes* (by md5) but does not record which version they
            # came from. Naming the version DOI instead makes that explicit.
            logger.warning(
                "%s is a concept identifier; resolved to version %s (%s). The "
                "checksum pins the bytes, but nothing records the version — use "
                "%s if you want the version in the URL.",
                container,
                provenance["record_id"],
                provenance["doi"],
                provenance["doi"],
            )
        members: dict[str, dict] = {}
        for entry in files:
            key = entry.get("key") or entry.get("filename")
            if not key:
                continue
            checksum = entry.get("checksum") or ""
            algorithm, _, digest = checksum.partition(":")
            if not digest:  # some responses give a bare hex digest
                algorithm, digest = "md5", checksum
            # No digest at all is not "md5 of nothing": leaving it as "" would make
            # every consumer believe a checksum was published.
            is_md5 = bool(digest) and algorithm.lower() == "md5"
            # `or` would turn a legitimately empty (0-byte) file into an unknown
            # size, and then into an OSError on the first read.
            size = entry.get("size")
            if size is None:
                size = entry.get("filesize")
            members[f"{container}/{key}"] = {
                "name": f"{container}/{key}",
                "type": "file",
                "size": size,
                "checksum": digest if is_md5 else None,
                "md5": digest if is_md5 else None,
                "checksum_type": algorithm.upper() if digest else None,
                # The version DOI is an immutable token for this exact byte
                # sequence — the load-bearing part of the ZIP integrity argument.
                "archive_version": provenance["doi"],
                # And the same fact in the field DVC reads, which is what lets a
                # *completed* `dvc import-url` stay pullable from Zenodo by someone
                # with no DVC remote (decisions.md D17, corrected).
                #
                # The md5 rather than the DOI, even though "which version" is what
                # the field is named for: `version_id` also drives `dvc update`'s
                # change detection, and a token derived from the bytes cannot miss
                # a change that a re-used identifier would. The DOI is not lost —
                # it is right above, and in the `zenodo` block.
                #
                # None when Zenodo published something other than an md5. That is
                # not silently tolerated: the DVC wrapper refuses such an entry
                # rather than let DVC drop it from a fetch with a warning.
                "version_id": digest if is_md5 else None,
                # The API's own link first (real records carry it under "self"),
                # and a *quoted* fallback: a "#" or "?" in a filename would
                # otherwise truncate the URL at what urlsplit reads as a fragment
                # or query, fetching something else entirely.
                "url": (entry.get("links") or {}).get("content")
                or (entry.get("links") or {}).get("self")
                or (
                    f"{self.api_url}/records/{recid}/files/"
                    f"{quote(key, safe='/')}/content"
                ),
                "zenodo": {**provenance, "key": key},
            }
        return members

    def _container_info(self, container: str) -> dict:
        record = self._resolve_record(container)
        return {
            "name": container,
            "type": "directory",
            "size": 0,
            "zenodo": self._record_provenance(record, requested=container),
        }

    def _member(self, container: str, member: str) -> dict:
        """The member as a plain file, ignoring any ZIP presentation."""
        members = self._members(container)
        path = f"{container}/{member}"
        if path in members:
            return members[path]
        available = sorted(self.relpath(container, key) for key in members)
        raise ArchiveNotFoundError(
            f"{member!r} is not in Zenodo record {container}. It holds: "
            f"{', '.join(available) or '(no files)'}"
        )

    def _list_root(self) -> list[dict]:
        raise NotSupportedError(
            "Zenodo has no listable root: address a record, e.g. zenodo://1486184 "
            "or zenodo://10.5281/zenodo.1486184. (Searching is a different "
            "operation from listing — see ZenodoFileSystem.search.)"
        )

    # ------------------------------------------------------- ZIP transparency

    def _zip_container(self, path: str) -> tuple[str, str] | None:
        """If ``path`` sits inside a ZIP member, return ``(zip path, interior)``.

        Only consulted when ``extract_zips`` is on; the explicit chained-URL form
        never reaches here, because fsspec handles it above us.
        """
        if not self.extract_zips:
            return None
        container, member = self.split_path(path)
        if not member:
            return None
        keys = [self.relpath(container, p) for p in self._members(container)]
        # longest ZIP key that prefixes this path
        for key in sorted(keys, key=len, reverse=True):
            if not key.lower().endswith(".zip"):
                continue
            if member == key:
                return f"{container}/{key}", ""
            if member.startswith(f"{key}/"):
                return f"{container}/{key}", member[len(key) + 1 :]
        return None

    # -------------------------------------------------- the wrapper directory

    def _zip_prefix(self, zip_path: str) -> str:
        """The leading interior directories ``strip_components`` removes, or ``""``.

        Depositors zip a *directory*, because ``unzip`` creates no wrapper of its
        own and a bare bag of files detonates into whatever directory the reader
        was standing in. So the convention is a service to everyone downloading
        from the record page — and it means the obvious import,
        ``pathology_annotations.zip`` into ``pathology_annotations/``, lands the
        contents at ``pathology_annotations/pathology_annotations/…``. ~80% of
        sampled Zenodo ZIPs are shaped this way
        (``docs/evidence/zenodo-zip-shape-2026-08-21.md``), so the redundant level
        is the common case, not the exception.

        Unlike ``tar --strip-components``, this **checks before it strips**: a
        component comes off only while it is the archive's sole top-level entry
        *and* a directory. A ZIP that is a flat bag, or holds two real
        directories, or wraps one file, is passed through untouched rather than
        losing its first sibling. So the setting is a maximum, not a promise, and
        the failure mode of asking for too much is "nothing happened".
        """
        if not self.strip_components:
            return ""
        if zip_path in self._zip_prefixes:
            return self._zip_prefixes[zip_path]
        view = self.zip_view(zip_path)
        # Files only. A ZIP's directory entries are optional markers, not content:
        # counting them as top-level entries would make the strip depend on which
        # tool wrote the archive.
        remaining = [
            name
            for name in view.zip.namelist()
            if not name.endswith("/") and not _is_cruft(name)
        ]
        parts: list[str] = []
        for _ in range(self.strip_components):
            tops = {name.split("/", 1)[0] for name in remaining}
            if len(tops) != 1:
                break
            top = tops.pop()
            if not all(name.startswith(f"{top}/") for name in remaining):
                break  # a lone *file* at this level, so there is no directory here
            parts.append(top)
            remaining = [name[len(top) + 1 :] for name in remaining]
        prefix = "/".join(parts)
        if prefix:
            # Not a warning — this is the designed common case — but it must be
            # visible, because it is the one thing about the resulting tree that
            # the URL does not tell you. `dvc -v` shows it; the committed `files:`
            # list records the result either way.
            logger.info(
                "%s: presenting its contents from %r (strip_components=%d); "
                "pass strip_components=0 for the interior verbatim",
                zip_path,
                prefix,
                self.strip_components,
            )
        self._zip_prefixes[zip_path] = prefix
        return prefix

    def _to_interior(self, zip_path: str, member: str) -> str:
        """Workspace-relative path inside a ZIP -> the real interior path."""
        prefix = self._zip_prefix(zip_path)
        if not prefix:
            return member
        return f"{prefix}/{member}" if member else prefix

    def _from_interior(self, zip_path: str, interior: str) -> str:
        """The inverse of :meth:`_to_interior`, for building workspace names.

        The two must stay exact inverses: ``_to_interior`` is what ``cat_file``
        and ``_open`` fetch through, so a strip applied in one direction only
        would list a tidy tree and then read the wrong member — or nothing.
        ``test_stripping_is_symmetric_so_reads_follow_listings`` pins that.
        """
        prefix = self._zip_prefix(zip_path)
        if prefix and interior.startswith(f"{prefix}/"):
            return interior[len(prefix) + 1 :]
        return interior

    def zip_view(self, zip_path: str):
        """A ``ZipFileSystem`` over one ZIP member, created lazily.

        Composition, not reimplementation (``docs/design.md`` §"Zenodo ZIP
        transparency"): fsspec does the archive traversal, we supply a seekable
        file. Because that file serves byte ranges, ``zipfile`` reads only the
        central directory — listing a ZIP does not download it.

        The archive is vetted (zip slip, symlink members, zip bombs) from that
        same central directory *before* any member is advertised: a path we would
        refuse to write must not be offered to a consumer that will write it.
        """
        import zipfile

        from fsspec.implementations.zip import ZipFileSystem

        from archivefs.zipsafe import inspect_zip

        zip_path = self._strip_protocol(zip_path)
        if zip_path not in self._zip_views:
            try:
                view = ZipFileSystem(fo=self._open_container(zip_path))
            except zipfile.BadZipFile as exc:
                # zipfile reads the end of the file inside `except OSError: pass`,
                # so an expired token (PermissionError) or a 503 partway through
                # comes back as "File is not a zip file" — telling the user their
                # perfectly good archive is corrupt. Re-read four bytes to surface
                # whatever actually went wrong; if that succeeds, it really is not
                # a ZIP.
                container, member = self.split_path(zip_path)
                self._fetch_range(self._member(container, member), 0, 4)
                raise ArchiveNotFoundError(
                    f"{zip_path} is not a readable ZIP archive ({exc}). Import it "
                    f"as an opaque file (extract_zips off) if it is not one."
                ) from exc
            inspect_zip(view.zip, self.zip_limits, target=zip_path)
            self._zip_views[zip_path] = view
        return self._zip_views[zip_path]

    def _open_container(self, path: str, cache_type: str = "bytes"):
        """Open a ZIP member *as the file it is*, bypassing the directory view.

        Needed because with ``extract_zips`` on, ``info()`` reports the ZIP as a
        directory — which is the point — so the ordinary open path would refuse.

        ``cache_type`` is the whole of :meth:`_member_view`'s argument; see there.
        """
        from archivefs.base import ArchiveFile

        container, member = self.split_path(self._strip_protocol(path))
        info = self._member(container, member)
        options: dict[str, Any] = {}
        if cache_type == "mmap":
            # Every block this cache misses is fetched over several connections at
            # once: Zenodo throttles per connection, so the same 4 MiB arrives in
            # 2.4s instead of 11.3s. Passed as a cache option rather than assigned
            # afterwards, so the cache is never briefly live without it.
            options["multi_fetcher"] = lambda spans: self._fetch_block_ranges(
                info, spans
            )
        return ArchiveFile(
            self,
            info,
            mode="rb",
            block_size=self.blocksize,
            cache_type=cache_type,
            cache_options=options,
        )

    def _member_view(self, zip_path: str):
        """A second view of the same ZIP, for reading member *bytes*.

        Listing and reading want opposite caches, and one ``ZipFileSystem`` has
        one file object — so there are two.

        **Listing** (:meth:`zip_view`) touches a few hundred bytes at the very end
        of the archive and then the central directory. fsspec's ``"bytes"`` cache
        fetches exact ranges with read-ahead clamped at EOF, so that costs three
        requests and ~0 bytes on a 25 MB archive. Keeping that is not optional: it
        is the download-free listing the project is built on (D9).

        **Reading members** walks the whole archive, jumping between members. The
        same ``"bytes"`` cache holds one contiguous range and drops it on any
        forward jump longer than a block (`fsspec/caching.py`, `BytesCache._fetch`),
        so each member re-fetched a full 4 MiB block: a `dvc import-url` of a 25 MB,
        26-member ZIP moved **497.8 MB — 19.9x the archive** across two passes.

        ``"mmap"`` is the answer for this half: a sparse temp file on local disk,
        block-fetched on demand, each block fetched **at most once for the life of
        the filesystem object**. That covers both of DVC's passes
        (`dvc/stage/imports.py:47` hashes the dependency, then downloads it) without
        materialising the container up front — so reading one small member out of a
        1.4 GB ZIP still costs one block, not 1.4 GB. No size threshold and no
        heuristic about what the caller intends, which is the part of this that took
        three wrong turns to get to.

        **The cost, measured rather than waved at.** This view parses the central
        directory again through its own block-aligned cache, and the central
        directory lives at the *end* of the archive — so that is a whole block,
        fetched a second time. Reading one 7-byte member out of the 25 MB archive
        went from 4.2 MB (one block) to **8.2 MB (two)**.

        So a single-member ``archivefs get`` pays about double, while a full import
        went from 497.8 MB to 25.0 MB. That is the trade, and it is worth taking
        because the import is the common case and the reported bug — but it is a
        real regression for one-file reads, not a rounding error.
        ``test_one_member_costs_two_blocks_at_the_default_size`` pins the number so
        it cannot drift unnoticed, and ``ideas.md`` records the obvious follow-up:
        seed this cache from the bytes the listing view already fetched.
        """
        import zipfile

        from fsspec.implementations.zip import ZipFileSystem

        from archivefs.zipsafe import inspect_zip

        zip_path = self._strip_protocol(zip_path)
        if zip_path not in self._member_views:
            handle = self._open_container(zip_path, cache_type="mmap")
            try:
                view = ZipFileSystem(fo=handle)
            except zipfile.BadZipFile as exc:
                handle.close()
                raise ArchiveNotFoundError(
                    f"{zip_path} is not a readable ZIP archive ({exc})."
                ) from exc
            # Vetted again rather than trusted from `zip_view`: this is a separate
            # parse of separately-fetched bytes, and the rule is that a path we
            # would refuse to write is never offered to something that will write
            # it. Cheap — the central directory is already local by now.
            inspect_zip(view.zip, self.zip_limits, target=zip_path)
            self._member_views[zip_path] = view
        return self._member_views[zip_path]

    def _fetch_block_ranges(
        self, info: dict, spans: list[tuple[int, int]]
    ) -> list[bytes]:
        """fsspec's ``multi_fetcher``: one bytes object per requested range.

        Each range is cut into parallel pieces, every piece across every range is
        fetched together, and the pieces are then rejoined per range. Splitting
        *within* a range is the part that matters: a sequential reader misses one
        block at a time, so without it there would never be more than one request
        in flight.
        """
        cuts = [self.split_span(start, end) for start, end in spans]
        flat = [piece for pieces in cuts for piece in pieces]
        fetched = self.fetch_ranges(info, flat)
        out: list[bytes] = []
        at = 0
        for pieces in cuts:
            out.append(b"".join(fetched[at : at + len(pieces)]))
            at += len(pieces)
        return out

    def _close_member_view(self, zip_path: str) -> None:
        """Drop one member view, releasing its sparse temp file."""
        view = self._member_views.pop(zip_path, None)
        if view is None:
            return
        try:
            view.zip.close()
        except Exception:  # noqa: BLE001 - a failed close must not mask the caller
            logger.debug("could not close the member view for %s", zip_path)

    def _close_zip_views(self) -> None:
        self._zip_views.clear()
        for zip_path in list(self._member_views):
            self._close_member_view(zip_path)

    def chained_url(self, path: str, member: str = "") -> str:
        """The explicit fsspec chain for a ZIP's interior.

        With no ``member`` this is the *filesystem* form — good for
        ``fsspec.filesystem(...)``/``ls`` and what ``docs/design.md`` writes.
        ``fsspec.open()`` needs a member, since a directory has no bytes::

            zip://::zenodo://1486184/archive.zip              # the tree
            zip://counts/matrix.mtx::zenodo://1486184/archive.zip
        """
        return f"zip://{member}::zenodo://{self._strip_protocol(path)}"

    def _container_as_directory(self, zip_path: str) -> dict:
        """A transparent ZIP, presented as the directory it stands in for.

        Its ``md5`` moves into the ``zenodo`` block as ``container_md5``: a
        directory has no content hash of its own, and leaving the archive's md5 in
        the ``md5`` field would offer a consumer a checksum for something it is not
        going to write. The container md5 is still the strong, download-free half
        of the integrity contract — it just is not this entry's checksum.
        """
        container = self._member(*self.split_path(zip_path))
        return {
            **container,
            "type": "directory",
            "md5": None,
            "checksum": None,
            "checksum_type": None,
            # Same rule as the md5 above: the container's token identifies the ZIP
            # file, and this entry is no longer presenting itself as that file.
            # DVC ignores a directory's version_id anyway; dropping it keeps the
            # entry from claiming an identity for something it will not write.
            "version_id": None,
            "zenodo": {
                **container["zenodo"],
                "container": container["name"],
                "container_md5": container["md5"],
                "integrity": (
                    "container MD5 (Zenodo, strong, download-free); per-member md5s "
                    "are computed from the extracted bytes, since a member's hash "
                    "cannot be known without decompressing it"
                ),
            },
        }

    def _zip_info(self, zip_path: str, interior: str) -> dict:
        view = self.zip_view(zip_path)
        try:
            info = dict(view.info(interior) if interior else {"type": "directory"})
        except (FileNotFoundError, KeyError) as exc:
            msg = f"{interior!r} is not inside {zip_path}"
            raise ArchiveNotFoundError(msg) from exc
        member = self._from_interior(zip_path, interior)
        info["name"] = f"{zip_path}/{member}" if member else zip_path
        if info.get("type") == "file":
            # Asymmetric but sound integrity contract (design.md): the container
            # is verified by Zenodo's MD5 on download, members carry only the
            # CRC32 from the central directory, and the workspace md5 is computed
            # from the extracted bytes. Soundness rests on version immutability.
            container = self._member(*self.split_path(zip_path))
            info["crc32"] = info.get("CRC")
            info["md5"] = None
            info["checksum"] = None
            info["zenodo"] = {
                **container["zenodo"],
                "container": container["name"],
                "container_md5": container["md5"],
                "member": interior,
                "integrity": (
                    "container MD5 (Zenodo, strong) + member CRC32 (ZIP central "
                    "directory); per-member md5 is computed at extraction time"
                ),
            }
            info["archive_version"] = container["archive_version"]
            # A member has no md5 and must not acquire one — computing it means
            # streaming the archive, and `md5` must never hold anything that is not
            # a content checksum (geo.py states the rule). `version_id` is *not* a
            # checksum field, which is what makes this legitimate rather than a
            # substitution: the container's md5 authenticates every member
            # extracted from it, and the CRC32 says which member. Together they
            # name exactly one byte sequence inside exactly one immutable record,
            # download-free, and that is enough for DVC to fetch against — so an
            # expanded ZIP no longer needs a manifest to be pullable
            # (decisions.md D20, corrected twice; ideas.md #0d).
            container_md5 = info["zenodo"]["container_md5"]
            crc = info.get("crc32")
            if container_md5 and crc is not None:
                info["version_id"] = f"{container_md5}:{crc:08x}"
        return info

    # ---------------------------------------------------- read surface (+ZIP)

    def ls(self, path: str = "", detail: bool = True, **kwargs: Any) -> list:
        path = self._strip_protocol(path)
        inside = self._zip_container(path) if path else None
        if inside:
            zip_path, member = inside
            view = self.zip_view(zip_path)
            entries = [
                self._zip_info(zip_path, name)
                for entry in view.ls(self._to_interior(zip_path, member), detail=True)
                # Cruft is hidden, never unreadable: a caller who names a
                # `__MACOSX` path explicitly still gets it. Hiding is a listing
                # decision, and `find`/`walk` inherit it, which is what keeps
                # resource forks out of the tracked directory hash.
                if not _is_cruft(name := entry["name"].lstrip("/"))
            ]
            return entries if detail else [e["name"] for e in entries]

        entries = super().ls(path, detail=True, **kwargs)
        if self.extract_zips:
            # Through `_container_as_directory`, not a local `type` flip: a
            # listing and an `info()` of the same path must agree, and that
            # helper is where the "a directory carries no md5 of its own" rule
            # lives. It reads the member cache `super().ls` just populated, so
            # this costs no requests and does not open the ZIP.
            entries = [
                self._container_as_directory(str(e["name"]))
                if str(e.get("name", "")).lower().endswith(".zip")
                else e
                for e in entries
            ]
        return entries if detail else [e["name"] for e in entries]

    def info(self, path: str, **kwargs: Any) -> dict:
        path = self._strip_protocol(path)
        inside = self._zip_container(path) if path else None
        if inside:
            zip_path, member = inside
            if not member:
                return self._container_as_directory(zip_path)
            return self._zip_info(zip_path, self._to_interior(zip_path, member))
        return super().info(path, **kwargs)

    def cat_file(
        self, path: str, start: int | None = None, end: int | None = None, **kwargs: Any
    ) -> bytes:
        path = self._strip_protocol(path)
        inside = self._zip_container(path)
        if inside and inside[1]:
            zip_path, member = inside
            interior = self._to_interior(zip_path, member)
            return self._member_view(zip_path).cat_file(interior)[start:end]
        return super().cat_file(path, start=start, end=end, **kwargs)

    def _open(self, path: str, mode: str = "rb", **kwargs: Any):
        path = self._strip_protocol(path)
        inside = self._zip_container(path) if "r" in mode else None
        if inside and inside[1]:
            zip_path, member = inside
            interior = self._to_interior(zip_path, member)
            return self._member_view(zip_path)._open(interior, mode=mode)
        return super()._open(path, mode=mode, **kwargs)

    # --------------------------------------------------------------- transfer

    def _data_url(self, info: dict) -> str:
        return info["url"]

    # --------------------------------------------------------------- discovery

    def versions(self, spec: str) -> list[dict]:
        """Every version of a record, newest first — what ``dvc update`` needs."""
        record = self._resolve_record(spec)
        url = (record.get("links") or {}).get("versions")
        if not url:
            return [self._record_provenance(record)]
        hits = (self._get_json(url) or {}).get("hits", {}).get("hits", [])
        return [self._record_provenance(hit) for hit in hits]

    def latest_version(self, spec: str) -> dict:
        record = self._resolve_record(spec)
        url = (record.get("links") or {}).get("latest")
        if not url:
            return self._record_provenance(record)
        return self._record_provenance(self._get_json(url))

    def search(self, query: str, size: int = 10) -> list[dict]:
        """Thin passthrough to Zenodo search, which keeps ``ls`` honest.

        Listing and searching are different operations; conflating them is how
        filesystems grow a magic root directory that means something different on
        every archive.
        """
        from urllib.parse import quote

        payload = self._get_json(
            f"{self.api_url}/records?q={quote(query)}&size={size}"
        )
        hits = (payload or {}).get("hits", {}).get("hits", [])
        return [self._record_provenance(hit) for hit in hits]
