"""``ega://`` — the European Genome-phenome Archive.

Built first and deliberately *standalone* (``docs/plan.md`` step 1): everything
this backend needs lives in this module, so that when Zenodo lands the shared
base can be factored out of a real diff rather than guessed at up front.

URL grammar
-----------
::

    ega://                                 # datasets you are authorised for
    ega://EGAD00001006673                  # dataset  -> directory
    ega://EGAD00001006673/EGAF00004908765  # file     -> one member
    ega://EGAF00004908765                  # file, without dataset context

What the archive actually gives us (verified against the live API v2, see
``scripts/ega_checksum_check.py`` and ``docs/decisions.md``):

* ``GET {api}/metadata/datasets`` -> the authorised dataset list.
* ``GET {api}/metadata/datasets/{EGAD}/files`` -> members, each with
  ``fileId``, ``displayFileName``, ``fileSize`` (of the *encrypted* object),
  ``plainChecksum`` (MD5 of the *decrypted* bytes) and ``fileStatus``.
* ``GET {api}/files/{EGAF}?destinationFormat=plain`` with a ``Range`` header ->
  decrypted bytes, ``206``, and a ``Content-Range`` whose total is the decrypted
  length. So both halves of a download-free ``info()`` — size and a
  content-correlated checksum — are available without moving payload bytes.

Two behaviours of the live service shape the retrieval loop:

* the token expires (1 h) well inside a large transfer -> refresh on ``401``;
* a range request can return ``200``/``206`` with a **short or empty body**
  (observed on parallel slices of one file) -> every read is length-checked and
  retried rather than trusted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

import requests
from fsspec.spec import AbstractBufferedFile, AbstractFileSystem

from archivefs.errors import (
    ArchiveNotFoundError,
    AuthenticationError,
    CredentialsNotFoundError,
    NotSupportedError,
)

logger = logging.getLogger(__name__)

#: Default endpoints. These mirror the server config that ``pyega3`` ships, so
#: archivefs follows EGA's own client rather than inventing a second source of
#: truth; all are overridable via ``storage_options`` or a server-config file.
DEFAULT_API_URL = "https://ega.ebi.ac.uk:8443/v2"
DEFAULT_AUTH_URL = "https://ega.ebi.ac.uk:8443/ega-openid-connect-server/token"
DEFAULT_CLIENT_ID = "f20cd2d3-682a-4568-a53e-4262ef54c8f4"
DEFAULT_CLIENT_SECRET = (
    "AMenuDLjVdVo4BSwi0QD54LL6NeVDEZRzEQUJ7hJOM3g4imDZBHHX0hNfKHPeQIGkskhtCmqAJtt"
    "_jm7EKq-rWw"
)

#: EGA's encrypted objects carry a 16-byte header that the plain transfer
#: strips, so ``fileSize`` overstates the materialised size by exactly this much.
#: We prefer the ``Content-Range`` total when we have it and use this to predict
#: the size download-free.
ENCRYPTION_HEADER_BYTES = 16

#: **A ranged read of ``destinationFormat=plain`` is only correct when the range
#: START is a multiple of this.** Off-boundary starts come back with a ``206``,
#: the right length, and *wrong bytes* — no error anywhere. Measured against the
#: live service (``docs/decisions.md`` D3): starts at 16/32/48/1008/4096/12352
#: are correct, starts at 17/31/999/1009/4097/12345 are garbage. It is the AES
#: block size, i.e. the server maps a plaintext offset onto a cipher block and
#: does not re-align. pyega3 never trips over this because it only ever asks for
#: multiples of its slice size; an fsspec filesystem, which reads wherever the
#: caller seeks, absolutely does.
PLAIN_RANGE_ALIGNMENT = 16

DATASET_RE = re.compile(r"^EGAD\d+$")
FILE_RE = re.compile(r"^EGAF\d+$")

#: Datasets EGA has retired behind a helpdesk request; listing them fails in a
#: way worth explaining rather than passing through as an empty directory.
LEGACY_DATASET_PREFIXES = ("EGAD00000000", "EGAD00010000")

TOKEN_LIFETIME_SECONDS = 3600
#: Refresh slightly early so a long read does not race the expiry.
TOKEN_REFRESH_MARGIN = 300


class EGAAuth:
    """OAuth2 password-grant token holder with refresh.

    Thread-safe: fsspec (and DVC on top of it) will read several members of a
    dataset concurrently, and they must not all stampede the token endpoint.
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        credential_source: str,
        auth_url: str = DEFAULT_AUTH_URL,
        client_id: str = DEFAULT_CLIENT_ID,
        client_secret: str = DEFAULT_CLIENT_SECRET,
        session: requests.Session | None = None,
    ) -> None:
        self.username = username
        self._password = password
        self.credential_source = credential_source
        self.auth_url = auth_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()
        self._token: str | None = None
        self._expires_at = 0.0
        self._lock = threading.Lock()

    def token(self, *, force: bool = False) -> str:
        with self._lock:
            fresh_enough = (
                self._token is not None
                and time.time() < self._expires_at - TOKEN_REFRESH_MARGIN
            )
            if fresh_enough and not force:
                return self._token  # type: ignore[return-value]
            return self._fetch()

    def _fetch(self) -> str:
        try:
            r = self.session.post(
                self.auth_url,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "grant_type": "password",
                    "client_id": self.client_id,
                    "scope": "openid",
                    "client_secret": self.client_secret,
                    "username": self.username,
                    "password": self._password,
                },
                timeout=60,
            )
        except requests.RequestException as exc:
            raise AuthenticationError(
                "EGA",
                f"could not reach the authentication service at {self.auth_url}: {exc}",
                "check network egress — EGA's OIDC endpoint is on a non-standard "
                "port (8443) that firewalls frequently block.",
                source=self.credential_source,
            ) from exc

        if r.status_code in (400, 401, 403):
            raise AuthenticationError(
                "EGA",
                f"authentication rejected ({r.status_code})",
                f"check the password for {self.username!r}. archivefs took it "
                f"from: {self.credential_source}. If the account is right, EGA "
                f"passwords expire — reset at https://ega-archive.org/ and "
                f"update that source (never the committed .dvc file).",
                source=self.credential_source,
            )
        r.raise_for_status()
        payload = r.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + int(
            payload.get("expires_in") or TOKEN_LIFETIME_SECONDS
        )
        logger.debug("EGA token acquired for %s", self.username)
        return self._token


def _resolve_credentials(
    username: str | None,
    password: str | None,
    credentials_file: str | os.PathLike | None,
) -> tuple[str, str, str]:
    """Find a username/password pair, and say where it came from.

    Precedence (``docs/design.md`` "Credential management"): explicit
    ``storage_options`` — which is what ``.dvc/config.local`` becomes — then the
    environment, then an on-disk credentials file. A committed ``.dvc`` file may
    legitimately carry ``username``; it must never carry the password, so the
    password is allowed to come from a *later* link in the chain than the
    username.
    """
    searched: list[str] = []
    source_bits: list[str] = []

    if username:
        source_bits.append("username from storage_options (.dvc/config.local)")
    searched.append("storage_options['username'] (from .dvc/config.local)")

    if not username:
        username = os.environ.get("EGA_USERNAME")
        if username:
            source_bits.append("username from $EGA_USERNAME")
    searched.append("$EGA_USERNAME")

    if password:
        source_bits.append("password from storage_options")
    searched.append("storage_options['password'] — discouraged, prefer the env")

    if not password:
        password = os.environ.get("EGA_PASSWORD")
        if password:
            source_bits.append("password from $EGA_PASSWORD")
    searched.append("$EGA_PASSWORD")

    if not (username and password):
        candidates = [Path(credentials_file)] if credentials_file else []
        candidates += [
            Path("~/.pyega3/credentials.json").expanduser(),
            Path("~/.config/archivefs/ega.json").expanduser(),
        ]
        for candidate in candidates:
            searched.append(str(candidate))
            if not candidate.exists():
                continue
            try:
                cfg = json.loads(candidate.read_text())
            except ValueError as exc:
                raise AuthenticationError(
                    "EGA",
                    f"{candidate} is not valid JSON ({exc})",
                    'expected a pyega3-style file: {"username": ..., "password": ...}',
                    source=str(candidate),
                ) from exc
            username = username or cfg.get("username")
            password = password or cfg.get("password")
            if username and password:
                source_bits.append(f"{candidate}")
                break

    if not (username and password):
        missing = "username and password" if not username else "password"
        raise CredentialsNotFoundError("EGA", missing, searched)

    return username, password, "; ".join(source_bits)


class EGAFileSystem(AbstractFileSystem):
    """Read-only fsspec filesystem over EGA datasets and files.

    Parameters
    ----------
    username, password:
        Usually only ``username`` is configured (in ``.dvc/config.local``); the
        password comes from ``$EGA_PASSWORD``. Passing ``password`` here works
        but is discouraged — on shared HPC it tends to end up in a file.
    credentials_file:
        pyega3-style JSON, used only if the earlier links in the chain miss.
    layout:
        How dataset members are named, which is what they will be called in a
        DVC workspace:

        ``"accession"`` (default)
            ``EGAD.../EGAF...`` — exactly the URL grammar in ``docs/design.md``.
        ``"display"``
            ``EGAD.../<displayFileName>``; collisions get their accession
            appended, so the mapping stays injective.
        ``"nested"``
            ``EGAD.../EGAF.../<displayFileName>`` — pyega3's own layout:
            friendly names, collision-proof.
    selection:
        Restrict a dataset's listing to these members (accessions or display
        names), i.e. the *selection-driven virtual ``ls()``* from the design
        doc. A sample sheet becomes a subset import whose ``.dvc`` file
        documents the subset it took.
    include_index_files:
        Keep ``.bai``/``.crai``/``.tbi`` members. Default ``True``.
    """

    protocol = "ega"
    root_marker = ""
    sep = "/"
    #: Written into ``.dvc`` files, so bump only with a migration in mind.
    version_aware = False
    cachable = False

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        credentials_file: str | os.PathLike | None = None,
        api_url: str = DEFAULT_API_URL,
        auth_url: str = DEFAULT_AUTH_URL,
        client_id: str = DEFAULT_CLIENT_ID,
        client_secret: str = DEFAULT_CLIENT_SECRET,
        layout: str = "accession",
        selection: list[str] | None = None,
        include_index_files: bool = True,
        block_size: int | None = None,
        max_retries: int = 5,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        if layout not in ("accession", "display", "nested"):
            raise ValueError(
                f"layout must be 'accession', 'display' or 'nested', got {layout!r}"
            )
        self.api_url = api_url.rstrip("/")
        self.layout = layout
        self.selection = set(selection) if selection else None
        self.include_index_files = include_index_files
        self.max_retries = max_retries
        if block_size:
            self.blocksize = block_size

        username, password, source = _resolve_credentials(
            username, password, credentials_file
        )
        self.session = requests.Session()
        self.auth = EGAAuth(
            username,
            password,
            credential_source=source,
            auth_url=auth_url,
            client_id=client_id,
            client_secret=client_secret,
            session=self.session,
        )
        #: dataset accession -> {member path: info dict}
        self._dataset_cache: dict[str, dict[str, dict]] = {}
        #: EGAF accession -> info dict, for dataset-less addressing
        self._file_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------ paths

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        path = super()._strip_protocol(path)
        return path.lstrip("/").rstrip("/")

    @staticmethod
    def _get_kwargs_from_urls(path: str) -> dict:
        return {}

    # ------------------------------------------------------------- http layer

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        stream: bool = False,
        timeout: int = 300,
        _retry_on_auth: bool = True,
    ) -> requests.Response:
        """One EGA request, with token refresh on 401 and bounded retries.

        The 401 path is not hypothetical: EGA tokens live an hour and a
        multi-hundred-GB dataset import does not fit in one.
        """
        attempt = 0
        while True:
            attempt += 1
            all_headers = {"Authorization": f"Bearer {self.auth.token()}"}
            all_headers.update(headers or {})
            try:
                response = self.session.request(
                    method, url, headers=all_headers, stream=stream, timeout=timeout
                )
            except requests.RequestException as exc:
                if attempt > self.max_retries:
                    raise OSError(f"EGA request failed after {attempt} tries: {exc}")
                self._backoff(attempt, reason=str(exc))
                continue

            if response.status_code == 401 and _retry_on_auth:
                logger.debug("EGA returned 401; refreshing token and retrying")
                self.auth.token(force=True)
                _retry_on_auth = False
                continue
            if response.status_code == 403:
                raise AuthenticationError(
                    "EGA",
                    f"403 Forbidden for {url}",
                    "the account authenticated but is not authorised for this "
                    "dataset/file. Check your DAC approvals at "
                    "https://ega-archive.org/my-datasets — approval is per-dataset "
                    "and can be revoked or expire.",
                    source=self.auth.credential_source,
                )
            if response.status_code == 404:
                raise ArchiveNotFoundError(f"EGA has no such resource: {url}")
            if response.status_code in (429, 500, 502, 503, 504):
                if attempt > self.max_retries:
                    response.raise_for_status()
                self._backoff(attempt, reason=f"HTTP {response.status_code}")
                continue
            response.raise_for_status()
            return response

    @staticmethod
    def _backoff(attempt: int, reason: str) -> None:
        delay = min(60.0, 0.6 * 2 ** (attempt - 1))
        logger.debug("EGA retry %d in %.1fs (%s)", attempt, delay, reason)
        time.sleep(delay)

    def _get_json(self, path: str) -> Any:
        response = self._request(
            "GET",
            f"{self.api_url}/metadata{path}",
            headers={"Accept": "application/json"},
            timeout=120,
        )
        return response.json()

    # --------------------------------------------------------------- metadata

    def authorized_datasets(self) -> list[str]:
        reply = self._get_json("/datasets") or []
        out = []
        for entry in reply:
            # API v1 returns bare accession strings, v2 returns objects.
            out.append(entry if isinstance(entry, str) else entry["datasetId"])
        return sorted(out)

    def _member_path(self, dataset: str, meta: dict, taken: set[str]) -> str:
        accession = meta["fileId"]
        display = os.path.basename(meta.get("displayFileName") or accession)
        if display.endswith(".cip"):  # never surface the encrypted name
            display = display[: -len(".cip")]
        if self.layout == "accession":
            leaf = accession
        elif self.layout == "nested":
            leaf = f"{accession}/{display}"
        else:
            leaf = display
            if leaf in taken:
                stem, dot, ext = leaf.partition(".")
                leaf = f"{stem}.{accession}{dot}{ext}" if dot else f"{leaf}.{accession}"
        taken.add(leaf)
        return f"{dataset}/{leaf}"

    def _info_from_metadata(self, path: str, dataset: str | None, meta: dict) -> dict:
        encrypted_size = meta.get("fileSize")
        plain_size = (
            encrypted_size - ENCRYPTION_HEADER_BYTES
            if isinstance(encrypted_size, int)
            else None
        )
        checksum = meta.get("plainChecksum") or meta.get("unencryptedChecksum")
        checksum_type = (
            meta.get("plainChecksumType") or meta.get("checksumType") or "MD5"
        )
        info: dict[str, Any] = {
            "name": path,
            "type": "file",
            "size": plain_size,
            # Verified: md5(decrypted bytes) == plainChecksum, so this checksum
            # describes the bytes the user ends up with, download-free.
            # See docs/decisions.md D1.
            "checksum": checksum if checksum_type.upper() == "MD5" else None,
            "md5": checksum if checksum_type.upper() == "MD5" else None,
            "checksum_type": checksum_type,
            "ega": {
                "file_id": meta.get("fileId"),
                "dataset_id": dataset,
                "display_name": meta.get("displayFileName"),
                "encrypted_name": meta.get("fileName"),
                "encrypted_size": encrypted_size,
                "status": meta.get("fileStatus"),
                "index_file_id": meta.get("indexFileId") or None,
            },
        }
        # EGA has no version token of its own (design.md: "Weak/implicit"). The
        # closest honest thing is the archive's own storage name, which embeds a
        # re-encryption timestamp and changes if the object is replaced.
        encrypted_name = meta.get("fileName") or ""
        stamp = re.search(r"\.(\d{10,})\.cip$", encrypted_name)
        if stamp:
            info["version_id"] = stamp.group(1)
        return info

    def _load_dataset(self, dataset: str) -> dict[str, dict]:
        if dataset in self._dataset_cache:
            return self._dataset_cache[dataset]
        if dataset.startswith(LEGACY_DATASET_PREFIXES):
            authorized = self.authorized_datasets()
            if dataset not in authorized:
                raise ArchiveNotFoundError(
                    f"{dataset} is not available: it is either unknown to this "
                    f"account or one of EGA's legacy datasets, which the current "
                    f"API does not serve (contact helpdesk@ega-archive.org for "
                    f"those). Datasets this account is authorised for: "
                    f"{', '.join(authorized) or '(none)'}"
                )
        try:
            reply = self._get_json(f"/datasets/{dataset}/files") or []
        except ArchiveNotFoundError:
            authorized = self.authorized_datasets()
            raise ArchiveNotFoundError(
                f"dataset {dataset} not found. Datasets this account is "
                f"authorised for: {', '.join(authorized) or '(none)'}"
            ) from None
        if not reply:
            raise ArchiveNotFoundError(
                f"dataset {dataset} returned no files. Either it is empty or the "
                f"account is not authorised for it; authorised datasets: "
                f"{', '.join(self.authorized_datasets()) or '(none)'}"
            )

        members: dict[str, dict] = {}
        taken: set[str] = set()
        for meta in reply:
            path = self._member_path(dataset, meta, taken)
            info = self._info_from_metadata(path, dataset, meta)
            members[path] = info
            self._file_cache[meta["fileId"]] = info
        self._dataset_cache[dataset] = members
        return members

    def _load_file(self, file_id: str) -> dict:
        if file_id in self._file_cache:
            return self._file_cache[file_id]
        meta = self._get_json(f"/files/{file_id}")
        if not meta or not meta.get("fileId"):
            raise ArchiveNotFoundError(
                f"no metadata for {file_id}. EGA answers 200-with-empty-fields when "
                f"the account lacks access, so this most likely means you are not "
                f"authorised for it (check https://ega-archive.org/my-datasets)."
            )
        datasets = meta.get("datasetId") or []
        dataset = datasets[0] if isinstance(datasets, list) and datasets else None
        info = self._info_from_metadata(file_id, dataset, meta)
        self._file_cache[file_id] = info
        return info

    def _resolve(self, path: str) -> dict:
        """Map any accepted path form onto a member info dict."""
        path = self._strip_protocol(path)
        head, _, tail = path.partition("/")

        if FILE_RE.match(head) and not tail:
            return self._load_file(head)

        if not DATASET_RE.match(head):
            raise ArchiveNotFoundError(
                f"{path!r} is not an EGA path. Expected ega://EGAD<digits>[/member] "
                f"or ega://EGAF<digits>."
            )
        members = self._load_dataset(head)
        if not tail:
            return {"name": head, "type": "directory", "size": 0}
        if path in members:
            return members[path]
        # An intermediate directory, e.g. EGAD.../EGAF... under the nested
        # layout. Checked before the fuzzy match below, or the accession would
        # resolve to the file it contains and `isdir` would lie.
        if any(p.startswith(f"{path}/") for p in members):
            return {"name": path, "type": "directory", "size": 0}
        # Accept any of the three layouts, whichever the URL used, so a .dvc
        # file written under one layout still resolves under another.
        wanted = tail.split("/")[0]
        for member_path, info in members.items():
            ega = info.get("ega", {})
            if wanted in (ega.get("file_id"), ega.get("display_name")):
                return info
            if member_path.endswith(f"/{tail}"):
                return info
        raise ArchiveNotFoundError(f"{tail!r} is not a member of {head}")

    def _selected(self, members: dict[str, dict]) -> dict[str, dict]:
        out = {}
        for path, info in members.items():
            ega = info.get("ega", {})
            if self.selection is not None and not (
                path in self.selection
                or ega.get("file_id") in self.selection
                or ega.get("display_name") in self.selection
                or os.path.basename(path) in self.selection
            ):
                continue
            if not self.include_index_files and str(
                ega.get("display_name") or ""
            ).endswith((".bai", ".crai", ".tbi", ".csi", ".idx")):
                continue
            out[path] = info
        if self.selection is not None and not out and members:
            raise ArchiveNotFoundError(
                f"selection matched none of the {len(members)} members. "
                f"Selections are matched against EGAF accessions and display "
                f"names; the first few available are: "
                f"{', '.join(sorted(members)[:3])}"
            )
        return out

    # ----------------------------------------------------- fsspec read surface

    def ls(self, path: str = "", detail: bool = True, **kwargs: Any) -> list:
        path = self._strip_protocol(path)
        if not path:
            entries = [
                {"name": ds, "type": "directory", "size": 0}
                for ds in self.authorized_datasets()
            ]
            return entries if detail else [e["name"] for e in entries]

        head, _, tail = path.partition("/")
        if FILE_RE.match(head) and not tail:
            info = self._load_file(head)
            return [info] if detail else [info["name"]]

        members = self._selected(self._load_dataset(head))
        if tail:
            # Listing inside a dataset: either a nested-layout accession
            # directory, or a single member.
            prefix = f"{path}/"
            nested = [i for p, i in members.items() if p.startswith(prefix)]
            if nested:
                return nested if detail else [i["name"] for i in nested]
            info = self._resolve(path)
            return [info] if detail else [info["name"]]

        out = list(members.values())
        return out if detail else [i["name"] for i in out]

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
        """MD5 of the *decrypted* bytes, straight from metadata.

        Overridden because ``AbstractFileSystem.checksum`` hashes
        ``info()``+mtime, which for us would throw away a real content checksum
        in favour of a meaningless one.
        """
        return self._resolve(path).get("md5")

    def created(self, path: str) -> None:
        return None

    def modified(self, path: str) -> None:
        return None

    def ukey(self, path: str) -> str:
        info = self._resolve(path)
        return info.get("md5") or f"{info.get('name')}:{info.get('size')}"

    # --------------------------------------------------------------- transfer

    def _data_url(self, info: dict) -> str:
        file_id = info.get("ega", {}).get("file_id") or info["name"].rsplit("/", 1)[-1]
        return f"{self.api_url}/files/{file_id}?destinationFormat=plain"

    def plain_size(self, path: str) -> int:
        """Ask the data endpoint for the decrypted length (download-free).

        ``fileSize - 16`` has matched this for every file checked, but the
        ``Content-Range`` total is authoritative and costs one byte.
        """
        info = self._resolve(path)
        response = self._request(
            "GET", self._data_url(info), headers={"Range": "bytes=0-0"}, timeout=120
        )
        total = response.headers.get("Content-Range", "").partition("/")[2]
        response.close()
        if not total.isdigit():
            raise OSError(
                f"EGA did not report a Content-Range total for {path}; cannot "
                f"establish the decrypted size without downloading."
            )
        return int(total)

    def _fetch_range(self, info: dict, start: int, end: int) -> bytes:
        """Fetch ``[start, end)`` of the decrypted bytes.

        Two safeguards, both from observed live behaviour:

        * the request start is aligned down to :data:`PLAIN_RANGE_ALIGNMENT` and
          the surplus trimmed locally, because EGA silently returns *wrong bytes*
          for an unaligned start;
        * the body length is checked, because a ranged request can come back
          successful but short, and a truncated read would corrupt data with no
          error anywhere.
        """
        if end <= start:
            return b""
        aligned = start - (start % PLAIN_RANGE_ALIGNMENT)
        trim = start - aligned
        if trim:
            return self._fetch_aligned(info, aligned, end)[trim:]
        return self._fetch_aligned(info, aligned, end)

    def _fetch_aligned(self, info: dict, start: int, end: int) -> bytes:
        assert start % PLAIN_RANGE_ALIGNMENT == 0
        want = end - start
        url = self._data_url(info)
        last_error = "no attempt made"
        for attempt in range(1, self.max_retries + 1):
            response = self._request(
                "GET",
                url,
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
                last_error = f"asked for {want} bytes, got {got}"
            finally:
                response.close()
            logger.warning(
                "EGA short read on %s bytes=%d-%d (%s); retry %d/%d",
                info["name"],
                start,
                end - 1,
                last_error,
                attempt,
                self.max_retries,
            )
            self._backoff(attempt, reason=last_error)
        raise OSError(
            f"EGA would not serve bytes {start}-{end - 1} of {info['name']} "
            f"({last_error}) after {self.max_retries} attempts. This is usually "
            f"transient server-side; retry, and reduce concurrency if it persists."
        )

    def cat_file(
        self, path: str, start: int | None = None, end: int | None = None, **kwargs: Any
    ) -> bytes:
        info = self._resolve(path)
        if info.get("type") == "directory":
            raise IsADirectoryError(path)
        size = info.get("size")
        if size is None:
            size = self.plain_size(path)
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
    ) -> EGAFile:
        if "r" not in mode:
            raise NotSupportedError(
                "EGA is read-only through archivefs; deposit is a separate, "
                "human-supervised process (see the deposition SOP)."
            )
        info = self._resolve(path)
        if info.get("type") == "directory":
            raise IsADirectoryError(path)
        return EGAFile(
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
            "archivefs filesystems are read-only: EGA does not accept writes "
            "through this path, and pretending otherwise would silently lose data."
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


class EGAFile(AbstractBufferedFile):
    """A readable file over EGA's ranged plain-format endpoint."""

    def __init__(self, fs: EGAFileSystem, info: dict, **kwargs: Any) -> None:
        self._info = info
        size = info.get("size")
        if size is None:
            size = fs.plain_size(info["name"])
        super().__init__(fs, info["name"], size=size, **kwargs)

    def _fetch_range(self, start: int, end: int) -> bytes:
        return self.fs._fetch_range(self._info, start, min(end, self.size))

    def _upload_chunk(self, final: bool = False) -> None:
        raise NotSupportedError("EGA files are read-only")

    def _initiate_upload(self) -> None:
        raise NotSupportedError("EGA files are read-only")
