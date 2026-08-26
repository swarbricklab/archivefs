"""The shared base, extracted from the EGA + Zenodo diff.

``docs/plan.md`` step 2 says to extract this only *after* two backends exist, and
from the genuine overlap rather than the union of their complications. What the
two turned out to share, in full:

* a path grammar of ``container[/member]`` where the container is an accession or
  DOI and the member listing comes from one metadata call;
* an HTTP loop: bounded retries with backoff, ``Retry-After`` when offered,
  ``404`` → :class:`~archivefs.errors.ArchiveNotFoundError`, ``401``/``403`` →
  :class:`~archivefs.errors.AuthenticationError` *with remediation*, one
  credential refresh on ``401``;
* ranged reads that must be **length-checked** rather than trusted, and (EGA
  only, but the mechanism is general) **alignment-constrained**;
* ``info()`` built from metadata alone — download-free — carrying ``size``, a
  content checksum where the archive has one, and a version token;
* ``exists``/``isdir``/``isfile``/``checksum``/``ukey`` all derived from
  ``info()``, and every write operation refused.

What stayed in the subclasses is exactly the irreducible part named in the design
doc: *how do I list this repository* and *how do I fetch bytes given a path and a
credential*.

Designed with GEO and dbGaP in view, per the plan:

* GEO is anonymous, so authentication is a hook that defaults to "none", not a
  requirement threaded through the constructor;
* GEO also needs more than one *view* of one accession (supplementary files now,
  raw-reads-via-SRA one day), so container resolution is a method that may consult
  a ``view`` attribute rather than a hardcoded single listing;
* dbGaP's credential is a path to a file and its workspace is per-project, so
  credentials are declarative (see :mod:`archivefs.credentials`) rather than
  positional constructor arguments, and nothing here assumes a secret is a string
  that goes in a header.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, ClassVar

import requests
from fsspec.spec import AbstractBufferedFile, AbstractFileSystem

from archivefs.credentials import Credential, CredentialResolver
from archivefs.errors import (
    ArchiveNotFoundError,
    AuthenticationError,
    ManifestError,
    MissingExtraError,
    NotSupportedError,
)

logger = logging.getLogger(__name__)

__all__ = ["ArchiveFile", "ArchiveFileSystem"]

#: Smallest piece worth issuing as its own parallel request.
#:
#: Not because a request is expensive: measured, the fixed cost of one is about
#: **0.1 s** (`docs/evidence/zenodo-piece-size-2026-08-25.md` §2). D26 said "seconds",
#: having read 11.3 s for a 4 MiB block as overhead when it was 4.2 MB at the 0.37
#: MB/s a single connection sustains — a rate, not a price. This floor survives that
#: correction on the measurement instead: throughput falls off below ~1 MiB pieces
#: (512 KiB: 5.76 MB/s, 1 MiB: 8.93, 2 MiB: 9.70), so there is nothing to win by
#: slicing finer, and a floor keeps a pathological `max_concurrent_reads` from
#: shredding a range into kilobytes.
MIN_PARALLEL_PIECE = 512 * 1024

#: Piece size that measured fastest against Zenodo, at every connection count from 2
#: to 8. See §1 of the same evidence note: 1.68x over the 512 KiB pieces we shipped.
TARGET_PARALLEL_PIECE = 2 * 1024 * 1024

#: Ceiling on the bytes in flight across all connections at once, which is the
#: variable that actually predicts throughput — *however* you reach it. Sorting every
#: measured cell by `connections x piece size` (§4) gives a peak at 16 MiB and a cliff
#: past it: 8x2 MiB is 9.7-14.0 MB/s, while 8x4 MiB, 16x2 MiB and 8x8 MiB — 32, 32 and
#: 64 MiB in flight — give 5.05, 4.08 and 2.49. Overshooting costs more than
#: undershooting, so this is a ceiling and not a target.
MAX_BYTES_IN_FLIGHT = 16 * 1024 * 1024


class ArchiveFileSystem(AbstractFileSystem):
    """Base for read-only, accession-addressed archive filesystems.

    Subclasses must provide:

    ``archive_name``
        Human name used in every error message.
    ``credentials``
        A list of :class:`~archivefs.credentials.Credential` declarations, empty
        for anonymous archives.
    :meth:`_load_members`
        ``container -> {path: info}``, one metadata call, no payload bytes.
    :meth:`_data_url`
        ``info -> URL`` to fetch bytes from.

    and may override :meth:`_split`, :meth:`_list_root`, :meth:`_container_info`,
    :meth:`_match_member`, :meth:`_auth_headers`, :meth:`_refresh_credentials`
    and :meth:`_auth_remediation`.
    """

    #: Name used in messages, e.g. "EGA".
    archive_name = "archive"
    #: Declarative credential requirements; empty means anonymous.
    credentials: ClassVar[list[Credential]] = []
    #: Byte alignment the archive requires of a range *start*. 1 means none.
    #: EGA needs 16 and returns wrong bytes otherwise; see its module docstring.
    range_alignment = 1
    #: HTTP statuses worth retrying.
    retry_statuses = (429, 500, 502, 503, 504)

    protocol = "abstract"
    root_marker = ""
    sep = "/"
    cachable = False

    def __init__(
        self,
        block_size: int | None = None,
        max_retries: int = 5,
        request_timeout: int = 300,
        transfer_timeout: int = 1800,
        chiro: str | bool = "auto",
        max_concurrent_reads: int = 8,
        **kwargs: Any,
    ) -> None:
        # Anything a subclass declared as a credential is consumed here rather
        # than passed up to fsspec, which would not know what to do with it.
        credential_options = {
            c.name: kwargs.pop(c.name, None) for c in type(self).credentials
        }
        super().__init__(**kwargs)
        self.max_retries = max_retries
        self.request_timeout = request_timeout
        self.transfer_timeout = transfer_timeout
        self.chiro = chiro
        #: Ceiling on *all* concurrent ranged reads through this filesystem.
        #:
        #: One bound, held here rather than at each call site, because the callers
        #: multiply: `dvc_data.hashfile.build._hash_files` already runs up to
        #: `HASH_JOBS` threads, and each one splitting a block across N connections
        #: would put HASH_JOBS x N sockets on a free public service. Measured on
        #: Zenodo, 8 is also the fastest setting and not merely the politest: at
        #: 2 MiB pieces, 4 connections give 9.12 MB/s, 8 give 14.03, and 16 give
        #: 4.08 — past 8 the extra bytes in flight cost more than the extra socket
        #: wins. See :meth:`parallel_piece_size`.
        self.max_concurrent_reads = max(1, int(max_concurrent_reads))
        self._read_slots = threading.BoundedSemaphore(self.max_concurrent_reads)
        #: Whether the caller chose a block size, as opposed to inheriting the class
        #: default. :meth:`parallel_block_size` enlarges the latter and must not
        #: touch the former — a caller who asks for small blocks is usually asking
        #: for cheap small reads, and silently quadrupling them would be the exact
        #: eager fetch D25 refused.
        self._block_size_chosen = bool(block_size)
        if block_size:
            self.blocksize = block_size
        self.creds = CredentialResolver(
            self.archive_name, type(self).credentials, credential_options
        )
        self.session = self._make_session()
        self._member_cache: dict[str, dict[str, dict]] = {}
        # Re-entrant, and it has to be: resolving a manifest that lives inside the
        # deposit re-enters _members() on the same thread (that is what the cache
        # seed below is for). Without the lock, seeding publishes a *half-resolved*
        # native listing to every other thread — which is worse than the duplicate
        # work it replaced, because those threads then see no manifest, no
        # checksums and no drift report. DVC runs one filesystem instance across a
        # thread pool, so this is the normal case, not an exotic one.
        self._member_lock = threading.RLock()
        #: container -> manifest path actually used (or None)
        self._manifest_used: dict[str, str | None] = {}
        #: container -> DriftReport, when a manifest was applied
        self._drift: dict[str, Any] = {}
        #: container -> parsed manifest, so verification never re-fetches it
        self._manifests: dict[str, Any] = {}

    # ------------------------------------------------------------------ set-up

    def _make_session(self) -> Any:
        # Module-global lookup, deliberately: every HTTP call in archivefs goes
        # through this module, so patching ``archivefs.base.requests`` is the one
        # seam a test needs to run a backend entirely offline.
        return requests.Session()

    # ------------------------------------------------------------------- paths

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        path = super()._strip_protocol(path)
        return path.lstrip("/").rstrip("/")

    @staticmethod
    def _get_kwargs_from_urls(path: str) -> dict:
        # Nothing in an archive URL is configuration: the whole point is that the
        # URL is a portable accession and the configuration is local.
        return {}

    def _split(self, path: str) -> tuple[str, str]:
        """Split into ``(container, member)``. Override for exotic grammars."""
        container, _, member = path.partition("/")
        return container, member

    def relpath(self, container: str, path: str) -> str:
        """A member's path *within* its container.

        Not ``path.split("/", 1)[-1]``: a container is not always one segment.
        Zenodo's DOI form (``10.5281/zenodo.1486184``) is two, and assuming one
        turned ``…/data.csv`` into ``zenodo.1486184/data.csv`` — which then missed
        every manifest lookup and, worse, sent manifest resolution back through
        ``cat_file`` into unbounded recursion (125 live API calls before Python
        gave up).
        """
        if path == container:
            return ""
        prefix = f"{container}/"
        return path[len(prefix) :] if path.startswith(prefix) else path

    # ------------------------------------------------------- subclass contract

    def _load_members(self, container: str) -> dict[str, dict]:
        """``container -> {full path: info}``. One metadata call, no payload."""
        raise NotImplementedError

    def _data_url(self, info: dict) -> str:
        raise NotImplementedError

    def _list_root(self) -> list[dict]:
        raise NotSupportedError(
            f"{self.archive_name} has no listable root: address a specific "
            f"accession or record."
        )

    def _container_info(self, container: str) -> dict:
        return {"name": container, "type": "directory", "size": 0}

    def _match_member(
        self, members: dict[str, dict], path: str, member: str
    ) -> dict | None:
        """Last-chance lookup for a member addressed by an alias.

        Returning ``None`` means "not found"; the base then raises with a message
        listing what *is* there.
        """
        return None

    def _auth_headers(self) -> dict:
        return {}

    def _refresh_credentials(self, sent: dict | None = None) -> bool:
        """Called once on a ``401``. Return ``True`` if a retry is worthwhile.

        ``sent`` is the header block the rejected request actually carried, so an
        implementation can tell "the credential I used has already been replaced by
        a peer thread" from "it is still current and really was refused". Reading
        the *current* credential instead gets that backwards under concurrency.
        """
        return False

    def _auth_remediation(self, status: int, url: str) -> str:
        if not type(self).credentials:
            # Careful about the claim here. *archivefs* sends no credentials to
            # this archive; that is not the same as the archive having no
            # authentication. BioStudies, for one, does let a submitter read a
            # study that is registered but unreleased — archivefs just cannot
            # carry that access yet. Saying "this archive needs no credentials"
            # would tell a submitter their own working access is irrelevant.
            return (
                f"archivefs reads {self.archive_name} anonymously, so it can only "
                f"see public records. Most often this means the record is embargoed, "
                f"not yet released, restricted or withdrawn — check its landing page "
                f"for a release date. If it is registered but unreleased and *you* "
                f"have access to it, archivefs cannot use that access yet: "
                f"authenticated reads for {self.archive_name} are not implemented "
                f"(docs/ideas.md)."
            )
        return (
            f"check the credentials for {self.archive_name}. archivefs resolved "
            f"them from: {self.creds.describe()}."
        )

    # -------------------------------------------------------------- http layer

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        stream: bool = False,
        timeout: int | None = None,
        allow_auth_refresh: bool = True,
    ) -> Any:
        attempt = 0
        may_refresh = allow_auth_refresh
        while True:
            attempt += 1
            auth_headers = self._auth_headers()
            all_headers = dict(auth_headers)
            all_headers.update(headers or {})
            try:
                response = self.session.request(
                    method,
                    url,
                    headers=all_headers,
                    stream=stream,
                    timeout=timeout or self.request_timeout,
                )
            except requests.RequestException as exc:
                if attempt > self.max_retries:
                    raise OSError(
                        f"{self.archive_name} request failed after {attempt} "
                        f"attempts: {exc}"
                    ) from exc
                self._backoff(attempt, str(exc))
                continue

            status = response.status_code
            if status == 401 and may_refresh and self._refresh_credentials(all_headers):
                logger.debug("%s: refreshed credentials after 401", self.archive_name)
                may_refresh = False
                continue
            if status in (401, 403):
                # "Did we actually send a credential", not "does this backend
                # declare one". Zenodo's token is optional, so an anonymous
                # request to a restricted record was reporting "rejected the
                # credential (credential came from: token from not found)" —
                # blaming a credential that was never sent.
                uses_credentials = bool(auth_headers)
                origin = (self.creds.describe() or None) if uses_credentials else None
                raise AuthenticationError(
                    self.archive_name,
                    f"HTTP {status} for {url}",
                    self._auth_remediation(status, url),
                    source=origin,
                    used_credentials=uses_credentials,
                )
            if status == 404:
                raise ArchiveNotFoundError(
                    f"{self.archive_name} has no such resource: {url}"
                )
            if status in self.retry_statuses:
                if attempt > self.max_retries:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    time.sleep(min(60, int(retry_after)))
                else:
                    self._backoff(attempt, f"HTTP {status}")
                continue
            response.raise_for_status()
            return response

    def _backoff(self, attempt: int, reason: str) -> None:
        delay = min(60.0, 0.5 * 2 ** (attempt - 1))
        logger.debug(
            "%s: retry %d in %.1fs (%s)", self.archive_name, attempt, delay, reason
        )
        time.sleep(delay)

    def _get_json(self, url: str) -> Any:
        return self._request(
            "GET", url, headers={"Accept": "application/json"}, timeout=120
        ).json()

    # ---------------------------------------------------------------- metadata

    def _members(self, container: str) -> dict[str, dict]:
        with self._member_lock:
            if container not in self._member_cache:
                native = self._load_members(container)
                self._member_cache[container] = self._resolve_with_manifest(
                    container, native
                )
            return self._member_cache[container]

    def invalidate_cache(self, path: str | None = None) -> None:
        if path is None:
            self._member_cache.clear()
            self._manifest_used.clear()
            self._drift.clear()
            self._manifests.clear()
            return
        container, _ = self._split(self._strip_protocol(path))
        self._member_cache.pop(container, None)
        self._manifest_used.pop(container, None)
        self._drift.pop(container, None)
        self._manifests.pop(container, None)

    # ------------------------------------------------------------ .chiro layer

    def _resolve_with_manifest(
        self, container: str, native: dict[str, dict]
    ) -> dict[str, dict]:
        """Let a ``.chiro`` manifest *be* the listing, when there is one.

        ``docs/design.md``'s central abstraction: the manifest is the front door
        for every backend, and the backend is reduced to a byte-fetcher. Falls back
        to the native listing when there is no manifest, when manifests are turned
        off, or when the ``chirograph`` package is not installed — the last of those
        with a warning, because working with weaker guarantees beats refusing to
        work, but a silent downgrade would be dishonest.
        """
        from archivefs import chiro as chiro_mod

        self._manifest_used[container] = None
        # Config files are INI, so "false" can arrive as a string. Treating that as
        # a *manifest path* would be an obscure way to disobey the user.
        if not self.chiro or (
            isinstance(self.chiro, str)
            and self.chiro.strip().lower() in ("false", "0", "no", "off", "none")
        ):
            return native

        # "auto" and the true-ish spellings all mean "discover one"; anything else
        # is a path. `chiro = true` in an INI config is the obvious way to write
        # "yes", and treating it as a filename fails in a baffling way.
        explicit = (
            self.chiro
            if isinstance(self.chiro, str)
            and self.chiro.strip().lower() not in ("auto", "true", "1", "yes", "on")
            else None
        )
        if explicit is None:
            names = [self.relpath(container, path) for path in native]
            discovered = chiro_mod.discover_manifest(names)
            if discovered is None:
                return native
            manifest_path = f"{container}/{discovered}"
        else:
            manifest_path = explicit

        if not chiro_mod.have_chirograph():
            message = (
                f"{manifest_path} is a Chirograph manifest, which would give "
                f"download-free checksums for {container}, but the 'chirograph' "
                f"package is not installed — falling back to the archive's native "
                f"listing. Install it with: pip install 'archivefs[chiro]'"
            )
            if explicit is not None:
                # The user asked for this manifest by name; refusing is right.
                raise MissingExtraError("chiro", "chirograph", f"reading {explicit}")
            logger.warning("%s", message)
            return native

        # Seed the cache with the native listing *before* fetching the manifest.
        # Fetching it may go back through cat_file() — it does whenever the path
        # is not an exact key of this listing, e.g. a mistyped or withdrawn
        # `chiro=` path — and without this the lookup re-enters _members() for the
        # container currently being resolved, once per frame, hammering the archive
        # until Python's recursion limit stops it (200+ metadata requests observed).
        self._member_cache[container] = native
        try:
            manifest = chiro_mod.load_manifest(
                self, manifest_path, native.get(manifest_path)
            )
            members, drift = chiro_mod.apply_manifest(
                self, container, native, manifest_path, manifest
            )
        finally:
            self._member_cache.pop(container, None)
        if members and all(info.get("missing_upstream") for info in members.values()):
            # Every entry a ghost means the manifest does not describe *this*
            # listing — most often an EGA manifest written against display names
            # while the layout keys members by accession. Producing a listing of
            # files that all fail to read would be a slow way to learn that.
            raise ManifestError(
                f"{manifest_path} describes {len(members)} file(s) and not one of "
                f"them matches anything in {container}. The manifest's source_paths "
                f"are probably written against a different layout or a different "
                f"record; check `record_accession` in the manifest. Pass "
                f"chiro=False to use the archive's own listing instead."
            )
        clash = _prefix_clash(members)
        if clash:
            raise ManifestError(
                f"{manifest_path} maps one file's path inside another's: "
                f"{clash[0]!r} and {clash[1]!r}. One of them cannot be written to "
                f"disk, and a listing that hides it would lose data silently."
            )
        self._manifest_used[container] = manifest_path
        self._manifests[container] = manifest
        self._drift[container] = drift
        return members

    def manifest_path_for(self, container: str) -> str | None:
        """Which manifest (if any) is being used for ``container``."""
        container = self._strip_protocol(container)
        self._members(container)
        return self._manifest_used.get(container)

    def manifest_for(self, container: str) -> Any:
        """The parsed manifest in use for ``container``, or ``None``.

        Cached from the listing pass, so verification does not re-fetch (and does
        not have to re-resolve a path that the manifest itself removed from the
        listing).
        """
        container = self._strip_protocol(container)
        self._members(container)
        return self._manifests.get(container)

    def drift(self, container: str) -> Any:
        """Drift between a deposit and its manifest, or ``None`` without one.

        Exposed because "this deposit no longer matches the manifest it was
        published with" is a finding a reviewer wants, not an internal detail.
        """
        container = self._strip_protocol(container)
        self._members(container)
        return self._drift.get(container)

    def _resolve(self, path: str) -> dict:
        path = self._strip_protocol(path)
        if not path:
            return {"name": "", "type": "directory", "size": 0}
        container, member = self._split(path)
        members = self._members(container)
        if not member:
            return self._container_info(container)
        if path in members:
            return members[path]
        # An intermediate directory. Checked before alias matching, or a nested
        # layout's accession level would resolve to the file inside it.
        if any(p.startswith(f"{path}/") for p in members):
            return {"name": path, "type": "directory", "size": 0}
        matched = self._match_member(members, path, member)
        if matched is not None:
            return matched
        available = sorted(self.relpath(container, p) for p in members)
        shown = ", ".join(available[:6]) + (" …" if len(available) > 6 else "")
        raise ArchiveNotFoundError(
            f"{member!r} is not in {container}. It holds: {shown or '(nothing)'}"
        )

    # ----------------------------------------------------- fsspec read surface

    def ls(self, path: str = "", detail: bool = True, **kwargs: Any) -> list:
        """Immediate children of ``path`` — *not* every descendant.

        The distinction is not cosmetic. fsspec's ``walk`` (and so ``find``,
        ``glob``, ``expand_path`` and ``get(recursive=True)``, which DVC's
        directory pull goes through) keys each directory's files **by basename**.
        Return a flattened tree here and two members with the same basename at
        different depths overwrite each other: the pull writes fewer files than
        the deposit holds, exit code 0, no warning.

        That is not hypothetical for us — it is the normal shape of the two
        features that produce nested paths: a ``.chiro`` manifest relocating a flat
        deposit into a per-sample tree (``sampleA/matrix.mtx``,
        ``sampleB/matrix.mtx``) and EGA's ``layout="nested"``, which exists
        *because* display names repeat across accessions.
        """
        path = self._strip_protocol(path)
        if not path:
            entries = self._list_root()
            return entries if detail else [e["name"] for e in entries]

        container, member = self._split(path)
        members = self._members(container)
        if member and path in members:  # a file lists as itself
            info = members[path]
            return [info] if detail else [info["name"]]

        prefix = f"{container}/" if not member else f"{path}/"
        entries: dict[str, dict] = {}
        for member_path, info in members.items():
            if not member_path.startswith(prefix):
                continue
            head, separator, _ = member_path[len(prefix) :].partition("/")
            if separator:
                child = f"{prefix}{head}"
                entries.setdefault(
                    child, {"name": child, "type": "directory", "size": 0}
                )
            else:
                entries[member_path] = info

        if not entries:
            # Either a path that does not exist (raises, with a helpful message)
            # or an empty directory.
            info = self._resolve(path)
            if info.get("type") == "file":
                return [info] if detail else [info["name"]]
            return []
        out = [entries[key] for key in sorted(entries)]
        return out if detail else [e["name"] for e in out]

    def info(self, path: str, **kwargs: Any) -> dict:
        return self._resolve(path)

    def walk(
        self,
        path: str,
        maxdepth: int | None = None,
        topdown: bool = True,
        on_error: str = "raise",
        **kwargs: Any,
    ):
        """Like fsspec's, but errors are raised rather than omitted.

        fsspec defaults to ``on_error="omit"``, which turns an expired token or a
        503 partway through a tree into *silently fewer files* and exit code 0.
        For a tool whose entire purpose is retrieving a complete, verified copy of
        a deposit, that default is the wrong way round.
        """
        return super().walk(
            path, maxdepth=maxdepth, topdown=topdown, on_error=on_error, **kwargs
        )

    # NB: everything below derives from ``self.info()``, never from ``_resolve()``
    # directly. A subclass that presents something differently — Zenodo showing a
    # ZIP member as a directory, GEO resolving a missing size — overrides ``info``,
    # and calling ``_resolve`` here would silently bypass that. It did: with
    # ``extract_zips`` on, ``info()`` called a ZIP a directory while ``isdir()``
    # called it a file, so DVC materialised the archive instead of its contents.
    def exists(self, path: str, **kwargs: Any) -> bool:
        """``False`` means "not in the archive", never "could not tell".

        An expired token or a revoked DAC approval is not absence, and reporting it
        as absence hides the one message that would tell the user what to fix, so
        :class:`~archivefs.errors.AuthenticationError` propagates.
        """
        try:
            self.info(path)
        except ArchiveNotFoundError:
            return False
        return True

    def isdir(self, path: str) -> bool:
        try:
            return self.info(path).get("type") == "directory"
        except ArchiveNotFoundError:
            return False

    def isfile(self, path: str) -> bool:
        try:
            return self.info(path).get("type") == "file"
        except ArchiveNotFoundError:
            return False

    def checksum(self, path: str) -> str | None:
        """The archive's own content checksum, not a hash of the metadata.

        ``AbstractFileSystem.checksum`` hashes ``info()`` and mtime, which would
        throw away a real MD5 in favour of a token that says nothing about the
        bytes. Every consumer that asks for a checksum wants the former.
        """
        return self.info(path).get("md5")

    def ukey(self, path: str) -> str:
        info = self.info(path)
        return (
            info.get("md5")
            or info.get("archive_version")
            or f"{info.get('name')}:{info.get('size')}"
        )

    def created(self, path: str) -> None:
        return None

    def modified(self, path: str) -> None:
        return None

    # --------------------------------------------------------------- transfer

    def _fetch_range(self, info: dict, start: int, end: int) -> bytes:
        """Fetch ``[start, end)`` of ``info``'s bytes, checked and aligned.

        ``end`` past the end of the object is clamped rather than requested. A
        block-aligned reader asks for the block *containing* the last byte, so the
        final read of almost every file overshoots; sending that overshoot to the
        server buys nothing and — before this clamp — spent five retries deciding
        that a correct response was a server fault.
        """
        size = info.get("size")
        if size is not None:
            end = min(end, size)
        if end <= start:
            return b""
        with self._read_slots:
            return self._fetch_range_locked(info, start, end)

    def _fetch_range_locked(self, info: dict, start: int, end: int) -> bytes:
        alignment = self.range_alignment
        if alignment > 1 and start % alignment:
            aligned = start - (start % alignment)
            return self._fetch_checked(info, aligned, end)[start - aligned :]
        return self._fetch_checked(info, start, end)

    def fetch_ranges(self, info: dict, spans: list[tuple[int, int]]) -> list[bytes]:
        """Fetch several ranges of one object at once, in the order given.

        Archives throttle **per connection**, not per client, so this is where the
        throughput is. Measured on Zenodo, one connection sustains ~0.30 MB/s
        whatever size you ask it for, and eight carrying 2 MiB each sustain
        9.7-14.0 MB/s together.

        Ordinary reads cannot exploit that on their own: a sequential reader asks
        for one block at a time, so there is only ever one range in flight. This
        method is the seam for a caller that knows it wants a contiguous run and can
        cut it up (see ``zenodo._member_view``).

        Concurrency is bounded by :attr:`max_concurrent_reads` inside
        :meth:`_fetch_range`, not here, so splitting cannot multiply with a caller
        that is already threaded.
        """
        if len(spans) < 2:
            return [self._fetch_range(info, *span) for span in spans]
        from concurrent.futures import ThreadPoolExecutor

        workers = min(len(spans), self.max_concurrent_reads)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # `map` preserves order and re-raises the first exception, so a failed
            # sub-range fails the whole read rather than yielding a hole. Silently
            # returning short data here would corrupt a member with no error.
            return list(pool.map(lambda span: self._fetch_range(info, *span), spans))

    def fetch_span(self, info: dict, start: int, end: int) -> bytes:
        """Fetch one contiguous run over several connections, with bounded memory.

        :meth:`fetch_ranges` is the parallel primitive; this is what to call when
        what you have is *a run*, which is what every ordinary read is. It cuts the
        run with :meth:`split_span` and — the part that is not one line — fetches it
        a **window at a time** rather than all at once.

        Without the window a whole-object read is worse than the slowness it fixes:
        `archivefs get` of a 2.5 GB file cuts into 1250 pieces of 2 MiB, every one
        of them live at the same instant, and then joins them. The window holds
        `max_concurrent_reads x piece` in flight instead, which is
        :data:`MAX_BYTES_IN_FLIGHT` — so the bound that keeps memory flat is the
        same bound that makes the transfer fast (§4 of
        ``docs/evidence/zenodo-piece-size-2026-08-25.md``).

        The result is still assembled whole, because that is :meth:`cat_file`'s
        contract. Streaming a large object to disk wants ``fs.open()``, which reads
        a block at a time and gets the same parallelism within each block.

        At ``max_concurrent_reads == 1`` this is the single request it always was:
        opting out of concurrency should not buy a stream of small requests instead.
        """
        size = info.get("size")
        if size is not None:
            end = min(end, size)
        if end <= start:
            return b""
        window = self.max_concurrent_reads * self.parallel_piece_size()
        if self.max_concurrent_reads < 2 or end - start <= window:
            return b"".join(self.fetch_ranges(info, self.split_span(start, end)))
        out = bytearray(end - start)
        buffer = memoryview(out)
        for base in range(start, end, window):
            spans = self.split_span(base, min(base + window, end))
            for (first, last), piece in zip(spans, self.fetch_ranges(info, spans)):
                if len(piece) != last - first:
                    # Placing a short piece at a running offset would slide every
                    # later byte and yield a plausible, wrong file. Fail closed.
                    raise OSError(
                        f"{self.archive_name} returned {len(piece)} bytes where "
                        f"{last - first} were requested at offset {first} of "
                        f"{info.get('name')}; refusing to assemble a short read."
                    )
                buffer[first - start : last - start] = piece
        return bytes(out)

    def parallel_piece_size(self) -> int:
        """Bytes per parallel request: the measured optimum, or less if that would
        put more than :data:`MAX_BYTES_IN_FLIGHT` on the wire at once.

        Divided by the connection bound rather than fixed, because the cliff is in
        the *product*. At the default 8 connections this is the full 2 MiB; raise
        `max_concurrent_reads` to 16 and it becomes 1 MiB, which keeps the bytes in
        flight at the peak instead of walking into the measured collapse (16 x 2 MiB
        is 4.08 MB/s against 8 x 2 MiB's 14.03 — *more* connections, less throughput).
        """
        share = MAX_BYTES_IN_FLIGHT // self.max_concurrent_reads
        return max(MIN_PARALLEL_PIECE, min(TARGET_PARALLEL_PIECE, share))

    def parallel_block_size(self, container_size: int | None = None) -> int:
        """Block size for a cache whose misses are split across connections.

        One block is one range is one round of parallel pieces, so a block that is
        smaller than the bytes the path wants in flight leaves connections carrying
        less than they could: the 4 MiB block we shipped became 8 x 512 KiB and ran
        at 5.76 MB/s where 8 x 2 MiB runs at 9.70-14.03.

        ``container_size`` bounds it to an eighth of the object, because a block is
        also what a *small* read costs. Unbounded, a 16 MiB block would mean reading
        one 7-byte member of a 25 MB archive fetched two blocks that overlap the
        whole archive — the eager container fetch D25 refused, arrived at sideways.
        Bounding the block bounds that at a quarter of the container however large the
        blocks would otherwise be, which is *better* than the 33% the shipped 4 MiB
        block spent on that same 25 MB archive.

        Issue #12 proposed this shape (`clamp(container_size // 512, ...)`) for the
        opposite reason — to make blocks *bigger* on large archives. The measurement
        says large archives already reach the ceiling and it is the small ones that
        need the clamp; the constants differ by 32x and the direction is reversed, but
        the idea that the container's size belongs in this decision was right. Zenodo
        publishes it in the record metadata, so it costs no request to consult.

        For any cache whose misses are fetched in parallel: Zenodo's ``mmap`` member
        view, and — since #14 — every plain file opened through :meth:`_open`. The
        ZIP *listing* view must keep the small exact-range cache or ``ls`` stops
        being free (D25), and an explicitly chosen block size is returned untouched.
        """
        if self._block_size_chosen:
            return self.blocksize
        piece = self.parallel_piece_size()
        one_round = self.max_concurrent_reads * piece
        if container_size:
            share = container_size // 8
            one_round = min(one_round, share - share % piece)
        return max(self.blocksize, one_round)

    def split_span(self, start: int, end: int) -> list[tuple[int, int]]:
        """Cut ``[start, end)`` into pieces worth fetching in parallel.

        Pieces of a fixed size, and *not* one per connection — which is the bug this
        replaced. `MMapCache` consolidates every consecutive missing block into a
        single range, so reading one 100 MB member asked for one 100 MB range, which
        this method dutifully cut into 8 pieces of 12.5 MB: 100 MB in flight, four
        times past the cliff, at a measured ~2.5 MB/s. Cutting to a size instead
        gives 50 pieces of 2 MiB, of which the semaphore keeps 8 moving — the same
        connections, the same bytes, at the peak of the curve rather than the tail.

        The count is rounded up to a **whole number of full rounds**, one piece per
        connection per round, and this is not cosmetic: cutting a 23 MB member into
        2 MiB pieces gives 12, which is eight connections followed by four, and the
        second round leaves half the connections idle. Measured end to end, that lost
        more than the smaller pieces won (1.74 MB/s against 2.08 for the uncut rule).
        Sixteen pieces of 1.44 MB is the same two rounds with none of them idle.

        A range too short to be worth cutting is left whole.
        """
        length = end - start
        if self.max_concurrent_reads < 2 or length < 2 * MIN_PARALLEL_PIECE:
            return [(start, end)]
        per_round = self.max_concurrent_reads * self.parallel_piece_size()
        rounds = -(-length // per_round)
        # Never below the floor, which also stops a short range being shredded into
        # one tiny piece per connection just to fill a round.
        pieces = min(rounds * self.max_concurrent_reads, length // MIN_PARALLEL_PIECE)
        pieces = max(2, pieces)
        # Even pieces rather than N full ones and a runt: the runt would finish
        # early and leave its connection idle while the rest ran on.
        step = -(-length // pieces)
        return [
            (offset, min(offset + step, end))
            for offset in range(start, end, step)
        ]

    def _fetch_checked(self, info: dict, start: int, end: int) -> bytes:
        if info.get("missing_upstream"):
            manifest = (info.get("chiro") or {}).get("manifest")
            raise ManifestError(
                f"{info['name']} is promised by {manifest} but its bytes are not "
                f"in the deposit any more. The manifest and the archive have "
                f"diverged — check the record upstream; do not substitute another "
                f"file for it."
            )
        want = end - start
        url = self._data_url(info)
        last_error = "no attempt made"
        for attempt in range(1, self.max_retries + 1):
            response = self._request(
                "GET",
                url,
                headers={"Range": f"bytes={start}-{end - 1}"},
                stream=True,
                timeout=self.transfer_timeout,
            )
            try:
                served = (
                    self._range_start(response)
                    if response.status_code == 206
                    else None
                )
                if served is not None and served != start:
                    # Free to check, and it catches both a regression in our own
                    # alignment arithmetic and a proxy that re-ranges the request.
                    # EGA answers a misaligned start with the right length and the
                    # wrong bytes, so length alone is not evidence of correctness.
                    raise OSError(
                        f"{self.archive_name} served bytes from offset {served} "
                        f"when {start} was requested for {info.get('name')}; "
                        f"refusing the response rather than trusting it."
                    )
                chunks = []
                got = 0
                for chunk in response.iter_content(1024 * 1024):
                    chunks.append(chunk)
                    got += len(chunk)
            except requests.RequestException as exc:
                last_error = f"transfer failed: {exc}"
            else:
                if got == want:
                    return b"".join(chunks)
                if response.status_code == 200 and got > want:
                    # The server ignored the Range and sent the whole object. Say
                    # so: for a large object with a small block size this repeats
                    # per read, and a slow pull with no explanation is worse than a
                    # slow pull with one.
                    logger.warning(
                        "%s ignored the Range header for %s and sent %d bytes for a "
                        "%d-byte request; slicing locally. Reads of this object will "
                        "be slow.",
                        self.archive_name,
                        info.get("name"),
                        got,
                        want,
                    )
                    return b"".join(chunks)[start:end]
                total = self._range_total(response)
                if total is not None and start + got >= total:
                    # The server served to the end of the object, which is correct
                    # HTTP for a range that overshoots — not a short read.
                    #
                    # Judged against `Content-Range`'s own total rather than the
                    # size in `info`, because the two cases that get here are
                    # exactly the ones where `info` cannot be trusted: a reader
                    # asking for the block *containing* the last byte (which
                    # `_fetch_range` now clamps), and a declared size that is
                    # stale. Treating either as a fault burnt `max_retries`
                    # requests and then reported a server problem that did not
                    # exist, advising the caller to reduce concurrency.
                    return b"".join(chunks)
                last_error = f"asked for {want} bytes, got {got}"
            finally:
                response.close()
            logger.warning(
                "%s: short read on %s bytes=%d-%d (%s); retry %d/%d",
                self.archive_name,
                info.get("name"),
                start,
                end - 1,
                last_error,
                attempt,
                self.max_retries,
            )
            self._backoff(attempt, last_error)
        raise OSError(
            f"{self.archive_name} would not serve bytes {start}-{end - 1} of "
            f"{info.get('name')} ({last_error}) after {self.max_retries} attempts. "
            f"This is usually transient server-side; retry, and reduce concurrency "
            f"if it persists."
        )

    @staticmethod
    def _range_start(response: Any) -> int | None:
        """The offset a ``206`` says it is serving from, if it says."""
        header = response.headers.get("Content-Range", "")
        match = re.match(r"\s*bytes\s+(\d+)-", header)
        return int(match.group(1)) if match else None

    @staticmethod
    def _range_total(response: Any) -> int | None:
        """The object's full length a ``206`` declares, if it declares one.

        ``Content-Range: bytes 0-99/2048`` — the part after the slash. A server
        may send ``*`` there when it does not know, hence the ``None``.
        """
        header = response.headers.get("Content-Range", "")
        match = re.match(r"\s*bytes\s+\d+-\d+/(\d+)\s*$", header)
        return int(match.group(1)) if match else None

    def _size_of(self, info: dict) -> int:
        size = info.get("size")
        if size is None:
            raise OSError(
                f"{self.archive_name} did not report a size for {info.get('name')}; "
                f"cannot read it without downloading the whole object."
            )
        return size

    def cat_file(
        self, path: str, start: int | None = None, end: int | None = None, **kwargs: Any
    ) -> bytes:
        info = self.info(path)
        if info.get("type") == "directory":
            raise IsADirectoryError(path)
        size = self._size_of(info)
        begin = 0 if start is None else (start if start >= 0 else max(0, size + start))
        finish = size if end is None else min(size, end if end >= 0 else size + end)
        # Split, and windowed: this asks for the *whole* range, so before #14 a
        # `cat_file` of a 2.5 GB object was one HTTP request at ~0.30 MB/s.
        return self.fetch_span(info, begin, finish)

    def _open(
        self,
        path: str,
        mode: str = "rb",
        block_size: int | None = None,
        autocommit: bool = True,
        cache_options: dict | None = None,
        **kwargs: Any,
    ) -> ArchiveFile:
        if "r" not in mode:
            raise NotSupportedError(
                f"{self.archive_name} is read-only through archivefs. Deposit is a "
                f"separate, human-supervised process — see the deposition SOP."
            )
        info = self.info(path)
        if info.get("type") == "directory":
            raise IsADirectoryError(path)
        return ArchiveFile(
            self,
            info,
            mode=mode,
            # A block is one round of parallel pieces, so the default follows the
            # connection count rather than staying at the class's 4 MiB: 8 x 512 KiB
            # measured 5.76 MB/s where 8 x 2 MiB measured 9.70-14.03. Bounded by an
            # eighth of the object, so a small read of a small file stays small.
            block_size=block_size or self.parallel_block_size(info.get("size")),
            cache_options=cache_options,
            **kwargs,
        )

    # -------------------------------------------------------------- read-only

    def _unsupported(self, *args: Any, **kwargs: Any) -> None:
        raise NotSupportedError(
            f"archivefs filesystems are read-only; {self.archive_name} is not "
            f"written through this path, and pretending otherwise would silently "
            f"lose data."
        )

    mkdir = _unsupported
    makedirs = _unsupported
    rm = _unsupported
    rm_file = _unsupported
    _rm = _unsupported
    rmdir = _unsupported
    touch = _unsupported
    mv = _unsupported
    copy = _unsupported
    pipe_file = _unsupported


def _prefix_clash(members: dict[str, dict]) -> tuple[str, str] | None:
    """Find a member path that is a strict path-prefix of another.

    ``data.txt`` and ``data.txt/inner.txt`` cannot both exist on a filesystem, and
    a children-only ``ls`` would report the file and never descend — losing the
    second silently, which is the failure mode this whole module is careful about.
    """
    files = sorted(p for p, info in members.items() if info.get("type") != "directory")
    for i, earlier in enumerate(files):
        prefix = f"{earlier}/"
        for later in files[i + 1 :]:
            if later.startswith(prefix):
                return earlier, later
            if not later.startswith(earlier):
                break
    return None


class ArchiveFile(AbstractBufferedFile):
    """A read-only file over an archive's ranged endpoint."""

    def __init__(self, fs: ArchiveFileSystem, info: dict, **kwargs: Any) -> None:
        self._info = info
        super().__init__(fs, info["name"], size=fs._size_of(info), **kwargs)

    def _fetch_range(self, start: int, end: int) -> bytes:
        # One block, but carried by several connections. A single connection to
        # these archives sustains ~0.30 MB/s whatever size it is asked for, so an
        # unsplit block is the whole of the 30x gap in issue #14.
        return self.fs.fetch_span(self._info, start, min(end, self.size))

    def _upload_chunk(self, final: bool = False) -> None:
        raise NotSupportedError(f"{self.fs.archive_name} files are read-only")

    def _initiate_upload(self) -> None:
        raise NotSupportedError(f"{self.fs.archive_name} files are read-only")
