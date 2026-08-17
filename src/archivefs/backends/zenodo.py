"""``zenodo://`` — Zenodo records, addressed by record id or DOI.

Second in the build order (``docs/plan.md`` step 2) and, like EGA, written
standalone: the shared base is extracted afterwards from the diff between this
clean case and EGA's auth-complex one, so it factors the *genuine* common shape.

URL grammar
-----------
::

    zenodo://1486184                          # record -> directory
    zenodo://1486184/github-mark.zip          # one file
    zenodo://10.5281/zenodo.1486184           # version DOI (immutable)
    zenodo://10.5281/zenodo.1486183           # concept DOI -> latest version
    zip://::zenodo://1486184/github-mark.zip  # the ZIP's interior (fsspec chain)

Zenodo is the reference case for a reason: every file comes with an MD5 in the
record metadata, so ``info()`` is download-free *and* content-verified, and a
version DOI is immutable, which is what makes the ZIP integrity argument below
sound.
"""

from __future__ import annotations

import logging
import os
import posixpath
import re
import time
from typing import Any

import requests
from fsspec.spec import AbstractBufferedFile, AbstractFileSystem

from archivefs.errors import (
    ArchiveNotFoundError,
    AuthenticationError,
    NotSupportedError,
)

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://zenodo.org/api"
#: Sandbox for testing deposits; same API shape.
SANDBOX_API_URL = "https://sandbox.zenodo.org/api"

#: ``10.5281/zenodo.1486184`` and the record id inside it.
DOI_RE = re.compile(
    r"^(?P<prefix>10\.\d{4,9})/(?P<suffix>zenodo\.(?P<recid>\d+))$", re.I
)
RECORD_RE = re.compile(r"^\d+$")


class ZenodoFileSystem(AbstractFileSystem):
    """Read-only fsspec filesystem over Zenodo records.

    Parameters
    ----------
    token:
        Personal access token. Only needed for restricted records; public
        records work anonymously, which is the common case. Resolved from
        ``storage_options`` then ``$ZENODO_TOKEN``.
    api_url:
        Override for the sandbox or a self-hosted InvenioRDM.
    extract_zips:
        Present ``*.zip`` members as directories, listing and reading their
        interiors through fsspec's ``ZipFileSystem``. Off by default: some ZIPs
        are genuine archival units and should not be sprayed across a workspace.
        The explicit per-URL form is the fsspec chain
        ``zip://::zenodo://record/archive.zip``; this flag is the per-remote
        default, and both go through the same code.
    zip_limits:
        A :class:`~archivefs.zipsafe.ZipLimits` overriding the zip-bomb bounds.
        Genomics deposits are large; raise them deliberately rather than
        disabling the check.
    """

    protocol = "zenodo"
    root_marker = ""
    sep = "/"
    cachable = False

    def __init__(
        self,
        token: str | None = None,
        api_url: str = DEFAULT_API_URL,
        extract_zips: bool = False,
        zip_limits: Any = None,
        block_size: int | None = None,
        max_retries: int = 5,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.api_url = api_url.rstrip("/")
        self.extract_zips = extract_zips
        self.zip_limits = zip_limits
        self.max_retries = max_retries
        if block_size:
            self.blocksize = block_size
        if token:
            self.token = token
            self.token_source = "storage_options (.dvc/config.local)"
        elif os.environ.get("ZENODO_TOKEN"):
            self.token = os.environ["ZENODO_TOKEN"]
            self.token_source = "$ZENODO_TOKEN"
        else:
            self.token = None
            self.token_source = "none (anonymous)"
        self.session = requests.Session()
        #: record id -> record JSON
        self._records: dict[str, dict] = {}
        #: (record id, member) -> info
        self._members: dict[str, dict[str, dict]] = {}
        #: zip member path -> ZipFileSystem, created lazily
        self._zip_views: dict[str, Any] = {}

    # ------------------------------------------------------------------ paths

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        path = super()._strip_protocol(path)
        return path.lstrip("/").rstrip("/")

    @staticmethod
    def _get_kwargs_from_urls(path: str) -> dict:
        return {}

    @staticmethod
    def split_path(path: str) -> tuple[str, str]:
        """Split a path into ``(record spec, member path)``.

        A DOI contains a slash, so the split is not simply the first one:
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

    # ------------------------------------------------------------- http layer

    def _headers(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        stream: bool = False,
        timeout: int = 300,
    ) -> requests.Response:
        attempt = 0
        while True:
            attempt += 1
            all_headers = self._headers()
            all_headers.update(headers or {})
            try:
                response = self.session.request(
                    method, url, headers=all_headers, stream=stream, timeout=timeout
                )
            except requests.RequestException as exc:
                if attempt > self.max_retries:
                    raise OSError(
                        f"Zenodo request failed after {attempt} tries: {exc}"
                    ) from exc
                self._backoff(attempt, str(exc))
                continue

            if response.status_code in (401, 403):
                raise AuthenticationError(
                    "Zenodo",
                    f"HTTP {response.status_code} for {url}",
                    (
                        "this record is restricted or embargoed. Put a personal "
                        "access token in $ZENODO_TOKEN (or `dvc remote modify "
                        "--local <remote> token ...`) — never in the committed "
                        ".dvc file."
                    )
                    if not self.token
                    else (
                        "the token was rejected. Check it is still valid and has "
                        "the deposit:actions/deposit:write scopes needed to read "
                        "restricted records."
                    ),
                    source=self.token_source,
                )
            if response.status_code == 404:
                raise ArchiveNotFoundError(f"Zenodo has no such resource: {url}")
            if response.status_code == 410:
                raise ArchiveNotFoundError(
                    f"{url} has been withdrawn by Zenodo (HTTP 410). A withdrawn "
                    f"record cannot be pulled; the DOI stays as provenance."
                )
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt > self.max_retries:
                    response.raise_for_status()
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    time.sleep(min(60, int(retry_after)))
                else:
                    self._backoff(attempt, f"HTTP {response.status_code}")
                continue
            response.raise_for_status()
            return response

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        delay = min(60.0, 0.5 * 2 ** (attempt - 1))
        logger.debug("Zenodo retry %d in %.1fs (%s)", attempt, delay, reason)
        time.sleep(delay)

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

        # A concept id/DOI returns a redirect to the latest version; requests
        # follows it, and the payload tells us which version we actually got.
        record = self._request("GET", f"{self.api_url}/records/{recid}").json()
        self._records[spec] = record
        self._records[str(record.get("id", recid))] = record
        return record

    def _record_provenance(self, record: dict) -> dict:
        """The immutable bits worth writing into a ``.dvc`` file."""
        concept_doi = record.get("conceptdoi")
        doi = record.get("doi")
        requested_is_concept = False
        return {
            "record_id": str(record.get("id")),
            "doi": doi,
            "concept_doi": concept_doi,
            "concept_record_id": str(record.get("conceptrecid"))
            if record.get("conceptrecid")
            else None,
            "revision": record.get("revision"),
            "updated": record.get("updated"),
            "access_right": (record.get("metadata") or {}).get("access_right"),
            "title": record.get("title"),
            "resolved_from_concept": requested_is_concept,
        }

    def _members_of(self, spec: str) -> dict[str, dict]:
        record = self._resolve_record(spec)
        recid = str(record.get("id"))
        if recid in self._members:
            return self._members[recid]

        files = record.get("files")
        if files is None:
            # Some views omit the file list; ask the dedicated endpoint.
            files = (
                self._request("GET", f"{self.api_url}/records/{recid}/files")
                .json()
                .get("entries", [])
            )
        provenance = self._record_provenance(record)
        members: dict[str, dict] = {}
        for entry in files:
            key = entry.get("key") or entry.get("filename")
            if not key:
                continue
            checksum = entry.get("checksum") or ""
            algorithm, _, digest = checksum.partition(":")
            if not digest:  # some responses give a bare hex digest
                algorithm, digest = "md5", checksum
            info = {
                "name": f"{spec}/{key}",
                "type": "file",
                "size": entry.get("size") or entry.get("filesize"),
                "checksum": digest if algorithm == "md5" else None,
                "md5": digest if algorithm == "md5" else None,
                "checksum_type": algorithm.upper() or None,
                # Version DOI: an immutable provenance token for this exact byte
                # sequence, which is what makes the ZIP integrity argument work.
                "version_id": provenance["doi"],
                "url": (entry.get("links") or {}).get(
                    "content", f"{self.api_url}/records/{recid}/files/{key}/content"
                ),
                "zenodo": {**provenance, "key": key},
            }
            members[key] = info
        self._members[recid] = members
        return members

    def _member(self, spec: str, member: str) -> dict:
        members = self._members_of(spec)
        if member in members:
            return members[member]
        raise ArchiveNotFoundError(
            f"{member!r} is not in Zenodo record {spec}. It holds: "
            f"{', '.join(sorted(members)) or '(no files)'}"
        )

    # ------------------------------------------------------- ZIP transparency

    def _zip_container(self, path: str) -> tuple[str, str] | None:
        """If ``path`` sits inside a ZIP member, return ``(zip path, interior)``.

        Only consulted when ``extract_zips`` is on; the explicit chained-URL form
        never comes through here (fsspec handles it above us).
        """
        if not self.extract_zips:
            return None
        spec, member = self.split_path(path)
        if not member:
            return None
        members = self._members_of(spec)
        # longest ZIP key that prefixes this path
        for key in sorted(members, key=len, reverse=True):
            if not key.lower().endswith(".zip"):
                continue
            if member == key:
                return f"{spec}/{key}", ""
            if member.startswith(f"{key}/"):
                return f"{spec}/{key}", member[len(key) + 1 :]
        return None

    def zip_view(self, zip_path: str):
        """A ``ZipFileSystem`` over one ZIP member, created lazily.

        Composition, not reimplementation (``docs/design.md`` §"Zenodo ZIP
        transparency"): fsspec does the archive traversal, we supply a seekable
        file. Because our file object serves byte ranges, ``zipfile`` reads only
        the central directory — listing a ZIP does not download it.

        The archive is vetted (zip slip, symlink members, zip bombs) from that
        same central directory *before* any member is advertised: a path we would
        refuse to write must not be offered to a consumer that will write it.
        """
        from fsspec.implementations.zip import ZipFileSystem

        from archivefs.zipsafe import inspect_zip

        zip_path = self._strip_protocol(zip_path)
        if zip_path not in self._zip_views:
            view = ZipFileSystem(fo=self._open_container(zip_path))
            inspect_zip(view.zip, self.zip_limits, target=zip_path)
            self._zip_views[zip_path] = view
        return self._zip_views[zip_path]

    def _open_container(self, path: str) -> ZenodoFile:
        """Open a ZIP member *as the file it is*, bypassing the directory view.

        Needed because with ``extract_zips`` on, ``info()`` reports the ZIP as a
        directory — which is the point — so the ordinary open path would refuse.
        """
        spec, member = self.split_path(self._strip_protocol(path))
        return ZenodoFile(
            self,
            self._member(spec, member),
            mode="rb",
            block_size=self.blocksize,
            cache_type="bytes",
        )

    def chained_url(self, path: str, member: str = "") -> str:
        """The explicit fsspec chain for a ZIP's interior.

        With no ``member`` this is the *filesystem* form — good for
        ``fsspec.filesystem(...)``/``ls`` and what ``docs/design.md`` writes.
        ``fsspec.open()`` needs a member, since a directory has no bytes::

            zip://::zenodo://1486184/archive.zip              # the tree
            zip://counts/matrix.mtx::zenodo://1486184/archive.zip
        """
        return f"zip://{member}::zenodo://{self._strip_protocol(path)}"

    def _zip_info(self, zip_path: str, interior: str) -> dict:
        view = self.zip_view(zip_path)
        try:
            info = dict(view.info(interior) if interior else {"type": "directory"})
        except (FileNotFoundError, KeyError) as exc:
            msg = f"{interior!r} is not inside {zip_path}"
            raise ArchiveNotFoundError(msg) from exc
        name = f"{zip_path}/{interior}" if interior else zip_path
        info["name"] = name
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
            info["version_id"] = container["version_id"]
        return info

    # ----------------------------------------------------- fsspec read surface

    def ls(self, path: str = "", detail: bool = True, **kwargs: Any) -> list:
        path = self._strip_protocol(path)
        if not path:
            raise NotSupportedError(
                "Zenodo has no listable root: address a record, e.g. "
                "zenodo://1486184 or zenodo://10.5281/zenodo.1486184. "
                "(Search is a different operation from listing; see "
                "ZenodoFileSystem.search.)"
            )

        inside = self._zip_container(path)
        if inside and inside[1]:
            zip_path, interior = inside
            view = self.zip_view(zip_path)
            entries = []
            for entry in view.ls(interior, detail=True):
                entries.append(
                    self._zip_info(zip_path, entry["name"].lstrip("/"))
                )
            return entries if detail else [e["name"] for e in entries]

        spec, member = self.split_path(path)
        members = self._members_of(spec)

        if not member:
            entries = []
            for key, info in members.items():
                if self.extract_zips and key.lower().endswith(".zip"):
                    entries.append(
                        {**info, "type": "directory", "size": info.get("size")}
                    )
                else:
                    entries.append(info)
            return entries if detail else [e["name"] for e in entries]

        if inside:  # the ZIP member itself, presented as a directory
            zip_path, _ = inside
            view = self.zip_view(zip_path)
            entries = [
                self._zip_info(zip_path, entry["name"].lstrip("/"))
                for entry in view.ls("", detail=True)
            ]
            return entries if detail else [e["name"] for e in entries]

        info = self._member(spec, member)
        return [info] if detail else [info["name"]]

    def info(self, path: str, **kwargs: Any) -> dict:
        path = self._strip_protocol(path)
        spec, member = self.split_path(path)
        if not member:
            record = self._resolve_record(spec)
            return {
                "name": path,
                "type": "directory",
                "size": 0,
                "zenodo": self._record_provenance(record),
            }
        inside = self._zip_container(path)
        if inside:
            zip_path, interior = inside
            if not interior:
                return {
                    "name": path,
                    "type": "directory",
                    "size": self._member(spec, member).get("size"),
                    "zenodo": self._member(spec, member)["zenodo"],
                }
            return self._zip_info(zip_path, interior)
        return self._member(spec, member)

    def exists(self, path: str, **kwargs: Any) -> bool:
        try:
            self.info(path)
        except (ArchiveNotFoundError, AuthenticationError):
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
        return self.info(path).get("md5")

    def ukey(self, path: str) -> str:
        info = self.info(path)
        return info.get("md5") or f"{info.get('version_id')}:{info.get('size')}"

    def created(self, path: str) -> None:
        return None

    def modified(self, path: str) -> None:
        return None

    # --------------------------------------------------------------- discovery

    def versions(self, spec: str) -> list[dict]:
        """Every version of a record, newest first — what ``dvc update`` needs."""
        record = self._resolve_record(spec)
        url = (record.get("links") or {}).get("versions")
        if not url:
            return [self._record_provenance(record)]
        hits = self._request("GET", url).json().get("hits", {}).get("hits", [])
        return [self._record_provenance(hit) for hit in hits]

    def latest_version(self, spec: str) -> dict:
        record = self._resolve_record(spec)
        url = (record.get("links") or {}).get("latest")
        if not url:
            return self._record_provenance(record)
        return self._record_provenance(self._request("GET", url).json())

    def search(self, query: str, size: int = 10) -> list[dict]:
        """Thin passthrough to Zenodo search — handy, and keeps ``ls`` honest."""
        response = self._request(
            "GET", f"{self.api_url}/records?q={requests.utils.quote(query)}&size={size}"
        )
        hits = response.json().get("hits", {}).get("hits", [])
        return [self._record_provenance(hit) for hit in hits]

    # --------------------------------------------------------------- transfer

    def _fetch_range(self, info: dict, start: int, end: int) -> bytes:
        if end <= start:
            return b""
        want = end - start
        last_error = "no attempt made"
        for attempt in range(1, self.max_retries + 1):
            response = self._request(
                "GET",
                info["url"],
                headers={"Range": f"bytes={start}-{end - 1}"},
                stream=True,
                timeout=1800,
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
                if response.status_code == 200 and got >= want:
                    # Server ignored the Range and sent the whole file.
                    return b"".join(chunks)[start:end]
                last_error = f"asked for {want} bytes, got {got}"
            finally:
                response.close()
            self._backoff(attempt, last_error)
        raise OSError(
            f"Zenodo would not serve bytes {start}-{end - 1} of {info['name']} "
            f"({last_error}) after {self.max_retries} attempts."
        )

    def cat_file(
        self, path: str, start: int | None = None, end: int | None = None, **kwargs: Any
    ) -> bytes:
        path = self._strip_protocol(path)
        inside = self._zip_container(path)
        if inside and inside[1]:
            zip_path, interior = inside
            data = self.zip_view(zip_path).cat_file(interior)
            return data[start:end]
        info = self.info(path)
        if info.get("type") == "directory":
            raise IsADirectoryError(path)
        size = info["size"]
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
    ):
        if "r" not in mode:
            raise NotSupportedError(
                "Zenodo deposits are made through Zenodo, not through archivefs: "
                "these filesystems are read-only by design."
            )
        path = self._strip_protocol(path)
        inside = self._zip_container(path)
        if inside and inside[1]:
            zip_path, interior = inside
            return self.zip_view(zip_path)._open(interior, mode=mode, **kwargs)
        info = self.info(path)
        if info.get("type") == "directory":
            raise IsADirectoryError(path)
        return ZenodoFile(
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
            "archivefs filesystems are read-only. Zenodo does support deposit, "
            "but through its own (human-supervised, DOI-minting) workflow."
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


class ZenodoFile(AbstractBufferedFile):
    def __init__(self, fs: ZenodoFileSystem, info: dict, **kwargs: Any) -> None:
        self._info = info
        super().__init__(fs, info["name"], size=info["size"], **kwargs)

    def _fetch_range(self, start: int, end: int) -> bytes:
        return self.fs._fetch_range(self._info, start, min(end, self.size))

    def _upload_chunk(self, final: bool = False) -> None:
        raise NotSupportedError("Zenodo files are read-only")

    def _initiate_upload(self) -> None:
        raise NotSupportedError("Zenodo files are read-only")


def zenodo_url_for(record: str, member: str = "") -> str:
    """Build a ``zenodo://`` URL, quoting nothing that must not be quoted."""
    base = f"zenodo://{record}"
    return posixpath.join(base, member) if member else base
