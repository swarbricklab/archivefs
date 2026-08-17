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
        if block_size:
            self.blocksize = block_size
        self.creds = CredentialResolver(
            self.archive_name, type(self).credentials, credential_options
        )
        self.session = self._make_session()
        self._member_cache: dict[str, dict[str, dict]] = {}
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

    def _refresh_credentials(self) -> bool:
        """Called once on a ``401``. Return ``True`` if a retry is worthwhile."""
        return False

    def _auth_remediation(self, status: int, url: str) -> str:
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
            all_headers = self._auth_headers()
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
            if status == 401 and may_refresh and self._refresh_credentials():
                logger.debug("%s: refreshed credentials after 401", self.archive_name)
                may_refresh = False
                continue
            if status in (401, 403):
                raise AuthenticationError(
                    self.archive_name,
                    f"HTTP {status} for {url}",
                    self._auth_remediation(status, url),
                    source=self.creds.describe() or None,
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
        if self.chiro is False or self.chiro is None:
            return native

        explicit = (
            self.chiro
            if isinstance(self.chiro, str) and self.chiro != "auto"
            else None
        )
        if explicit is None:
            names = [path.split("/", 1)[-1] for path in native]
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

        manifest = chiro_mod.load_manifest(
            self, manifest_path, native.get(manifest_path)
        )
        members, drift = chiro_mod.apply_manifest(
            self, container, native, manifest_path, manifest
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
        available = sorted(p.split("/", 1)[-1] for p in members)
        shown = ", ".join(available[:6]) + (" …" if len(available) > 6 else "")
        raise ArchiveNotFoundError(
            f"{member!r} is not in {container}. It holds: {shown or '(nothing)'}"
        )

    # ----------------------------------------------------- fsspec read surface

    def ls(self, path: str = "", detail: bool = True, **kwargs: Any) -> list:
        path = self._strip_protocol(path)
        if not path:
            entries = self._list_root()
            return entries if detail else [e["name"] for e in entries]

        container, member = self._split(path)
        members = self._members(container)
        if not member:
            entries = list(members.values())
            return entries if detail else [e["name"] for e in entries]

        prefix = f"{path}/"
        nested = [info for p, info in members.items() if p.startswith(prefix)]
        if nested:
            return nested if detail else [i["name"] for i in nested]
        info = self._resolve(path)
        return [info] if detail else [info["name"]]

    def info(self, path: str, **kwargs: Any) -> dict:
        return self._resolve(path)

    def exists(self, path: str, **kwargs: Any) -> bool:
        try:
            self._resolve(path)
        except (ArchiveNotFoundError, AuthenticationError):
            return False
        return True

    def isdir(self, path: str) -> bool:
        try:
            return self._resolve(path).get("type") == "directory"
        except ArchiveNotFoundError:
            return False

    def isfile(self, path: str) -> bool:
        try:
            return self._resolve(path).get("type") == "file"
        except ArchiveNotFoundError:
            return False

    def checksum(self, path: str) -> str | None:
        """The archive's own content checksum, not a hash of the metadata.

        ``AbstractFileSystem.checksum`` hashes ``info()`` and mtime, which would
        throw away a real MD5 in favour of a token that says nothing about the
        bytes. Every consumer that asks for a checksum wants the former.
        """
        return self._resolve(path).get("md5")

    def ukey(self, path: str) -> str:
        info = self._resolve(path)
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
        """Fetch ``[start, end)`` of ``info``'s bytes, checked and aligned."""
        if end <= start:
            return b""
        alignment = self.range_alignment
        if alignment > 1 and start % alignment:
            aligned = start - (start % alignment)
            return self._fetch_checked(info, aligned, end)[start - aligned :]
        return self._fetch_checked(info, start, end)

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
                    # The server ignored the Range and sent the whole object.
                    return b"".join(chunks)[start:end]
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
        info = self._resolve(path)
        if info.get("type") == "directory":
            raise IsADirectoryError(path)
        size = self._size_of(info)
        begin = 0 if start is None else (start if start >= 0 else max(0, size + start))
        finish = size if end is None else min(size, end if end >= 0 else size + end)
        return self._fetch_range(info, begin, finish)

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
        info = self._resolve(path)
        if info.get("type") == "directory":
            raise IsADirectoryError(path)
        return ArchiveFile(
            self,
            info,
            mode=mode,
            block_size=block_size or self.blocksize,
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


class ArchiveFile(AbstractBufferedFile):
    """A read-only file over an archive's ranged endpoint."""

    def __init__(self, fs: ArchiveFileSystem, info: dict, **kwargs: Any) -> None:
        self._info = info
        super().__init__(fs, info["name"], size=fs._size_of(info), **kwargs)

    def _fetch_range(self, start: int, end: int) -> bytes:
        return self.fs._fetch_range(self._info, start, min(end, self.size))

    def _upload_chunk(self, final: bool = False) -> None:
        raise NotSupportedError(f"{self.fs.archive_name} files are read-only")

    def _initiate_upload(self) -> None:
        raise NotSupportedError(f"{self.fs.archive_name} files are read-only")
