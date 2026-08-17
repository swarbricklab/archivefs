"""``ega://`` — the European Genome-phenome Archive.

Built first and standalone (``docs/plan.md`` step 1), then re-seated on
:class:`~archivefs.base.ArchiveFileSystem` once Zenodo existed, so the base was
factored from a real diff. What is left here is what the design doc predicted
would be irreducible: how to list a dataset, how to authenticate, and how to
fetch bytes.

URL grammar
-----------
::

    ega://                                 # datasets you are authorised for
    ega://EGAD00001006673                  # dataset  -> directory
    ega://EGAD00001006673/EGAF00004908765  # file     -> one member
    ega://EGAF00004908765                  # file, without dataset context

What the archive actually gives us (verified against the live API v2 — see
``scripts/ega_checksum_check.py`` and ``docs/decisions.md``):

* ``GET {api}/metadata/datasets`` -> the authorised dataset list.
* ``GET {api}/metadata/datasets/{EGAD}/files`` -> members, each with
  ``fileId``, ``displayFileName``, ``fileSize`` (of the *encrypted* object),
  ``plainChecksum`` (MD5 of the *decrypted* bytes) and ``fileStatus``.
* ``GET {api}/files/{EGAF}?destinationFormat=plain`` with a ``Range`` header ->
  decrypted bytes, ``206``, and a ``Content-Range`` whose total is the decrypted
  length. So both halves of a download-free ``info()`` — size and a
  content-correlated checksum — are available without moving payload bytes.

Three live behaviours shape the retrieval loop, and only the first was
anticipated by the design:

* the token expires (1 h) well inside a large transfer -> refresh on ``401``;
* **a ranged read is only correct when the range start is a multiple of 16**;
* a ranged read can return success with a short body -> length-check everything.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any, ClassVar

from archivefs import base
from archivefs.base import ArchiveFileSystem
from archivefs.credentials import Credential
from archivefs.errors import ArchiveNotFoundError, AuthenticationError

logger = logging.getLogger(__name__)

#: Default endpoints. These mirror the server config that ``pyega3`` ships, so
#: archivefs follows EGA's own client rather than inventing a second source of
#: truth; all are overridable via ``storage_options``.
DEFAULT_API_URL = "https://ega.ebi.ac.uk:8443/v2"
DEFAULT_AUTH_URL = "https://ega.ebi.ac.uk:8443/ega-openid-connect-server/token"
DEFAULT_CLIENT_ID = "f20cd2d3-682a-4568-a53e-4262ef54c8f4"
# Not a secret in any meaningful sense: it is a public OAuth2 client secret,
# shipped inside pyega3's own config for every user of EGA. The *user's* password
# is the credential, and that is never hard-coded anywhere.
DEFAULT_CLIENT_SECRET = (
    "AMenuDLjVdVo4BSwi0QD54LL6NeVDEZRzEQUJ7hJOM3g4imDZBHHX0hNfKHPeQIGkskhtCmqAJtt"  # noqa: S105
    "_jm7EKq-rWw"
)

#: EGA's encrypted objects carry a 16-byte header that the plain transfer
#: strips, so ``fileSize`` overstates the materialised size by exactly this much.
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

#: Datasets EGA has retired behind a helpdesk request.
LEGACY_DATASET_PREFIXES = ("EGAD00000000", "EGAD00010000")

TOKEN_LIFETIME_SECONDS = 3600
#: Refresh slightly early so a long read does not race the expiry.
TOKEN_REFRESH_MARGIN = 300


class EGAAuth:
    """OAuth2 password-grant token holder with refresh.

    Thread-safe: fsspec (and DVC on top of it) reads several members of a dataset
    concurrently, and they must not all stampede the token endpoint.
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        credential_source: str,
        session: Any,
        auth_url: str = DEFAULT_AUTH_URL,
        client_id: str = DEFAULT_CLIENT_ID,
        client_secret: str = DEFAULT_CLIENT_SECRET,
    ) -> None:
        self.username = username
        self._password = password
        self.credential_source = credential_source
        self.auth_url = auth_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session
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
            response = self.session.post(
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
        except base.requests.RequestException as exc:
            raise AuthenticationError(
                "EGA",
                f"could not reach the authentication service at {self.auth_url}: {exc}",
                "check network egress — EGA's OIDC endpoint is on a non-standard "
                "port (8443) that firewalls frequently block.",
                source=self.credential_source,
            ) from exc

        if response.status_code in (400, 401, 403):
            raise AuthenticationError(
                "EGA",
                f"authentication rejected ({response.status_code})",
                f"check the password for {self.username!r}. archivefs took it "
                f"from: {self.credential_source}. If the account is right, EGA "
                f"passwords expire — reset at https://ega-archive.org/ and "
                f"update that source (never the committed .dvc file).",
                source=self.credential_source,
            )
        response.raise_for_status()
        payload = response.json()
        self._token = payload["access_token"]
        self._expires_at = time.time() + int(
            payload.get("expires_in") or TOKEN_LIFETIME_SECONDS
        )
        logger.debug("EGA token acquired for %s", self.username)
        return self._token


class EGAFileSystem(ArchiveFileSystem):
    """Read-only fsspec filesystem over EGA datasets and files.

    Parameters
    ----------
    username, password:
        Usually only ``username`` is configured (in ``.dvc/config.local``, or
        legitimately even in the committed ``.dvc`` — it is not a secret); the
        password comes from ``$EGA_PASSWORD``. Passing ``password`` here works but
        is discouraged: on shared HPC it tends to end up in a file.
    credentials_file:
        pyega3-style JSON, consulted only if the earlier links in the chain miss.
    layout:
        How dataset members are named, which is what they will be called in a DVC
        workspace:

        ``"accession"`` (default)
            ``EGAD.../EGAF...`` — exactly the URL grammar in ``docs/design.md``.
        ``"display"``
            ``EGAD.../<displayFileName>``; collisions get their accession
            appended, so the mapping stays injective.
        ``"nested"``
            ``EGAD.../EGAF.../<displayFileName>`` — pyega3's own layout: friendly
            names, collision-proof.
    selection:
        Restrict a dataset's listing to these members (accessions or display
        names): the *selection-driven virtual ``ls()``* of the design doc. A
        sample sheet becomes a subset import whose ``.dvc`` file documents the
        subset it took.
    include_index_files:
        Keep ``.bai``/``.crai``/``.tbi`` members. Default ``True``.
    """

    protocol = "ega"
    archive_name = "EGA"
    range_alignment = PLAIN_RANGE_ALIGNMENT
    credentials: ClassVar[list[Credential]] = [
        Credential(
            name="username",
            env=("EGA_USERNAME",),
            files=("~/.pyega3/credentials.json", "~/.config/archivefs/ega.json"),
            secret=False,
            help="your EGA account email address",
        ),
        Credential(
            name="password",
            env=("EGA_PASSWORD",),
            files=("~/.pyega3/credentials.json", "~/.config/archivefs/ega.json"),
            secret=True,
            help=(
                "EGA account password. On shared HPC use a per-user environment "
                "variable from your own profile, never a shared file — that keeps "
                "access traceable and revocable per person."
            ),
        ),
    ]

    def __init__(
        self,
        credentials_file: str | os.PathLike | None = None,
        api_url: str = DEFAULT_API_URL,
        auth_url: str = DEFAULT_AUTH_URL,
        client_id: str = DEFAULT_CLIENT_ID,
        client_secret: str = DEFAULT_CLIENT_SECRET,
        layout: str = "accession",
        selection: list[str] | None = None,
        include_index_files: bool = True,
        **kwargs: Any,
    ) -> None:
        if layout not in ("accession", "display", "nested"):
            raise ValueError(
                f"layout must be 'accession', 'display' or 'nested', got {layout!r}"
            )
        super().__init__(**kwargs)
        self.api_url = api_url.rstrip("/")
        self.layout = layout
        self.selection = set(selection) if selection else None
        self.include_index_files = include_index_files

        if credentials_file:
            # An explicit file outranks the defaults but not storage_options/env.
            for credential in self.creds.credentials.values():
                self.creds.credentials[credential.name] = type(credential)(
                    **{
                        **credential.__dict__,
                        "files": (str(credentials_file), *credential.files),
                    }
                )
        resolved = self.creds.require("username", "password")
        self.auth = EGAAuth(
            resolved["username"],
            resolved["password"],
            credential_source=self.creds.describe(),
            session=self.session,
            auth_url=auth_url,
            client_id=client_id,
            client_secret=client_secret,
        )
        #: EGAF accession -> info, for dataset-less addressing
        self._file_cache: dict[str, dict] = {}

    # ------------------------------------------------------------------- auth

    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.auth.token()}"}

    def _refresh_credentials(self) -> bool:
        # EGA tokens live an hour; a multi-hundred-GB dataset import does not fit
        # in one, so this path is routine rather than exceptional.
        self.auth.token(force=True)
        return True

    def _auth_remediation(self, status: int, url: str) -> str:
        if status == 403:
            return (
                "the account authenticated but is not authorised for this "
                "dataset/file. Check your DAC approvals at "
                "https://ega-archive.org/my-datasets — approval is per-dataset and "
                "can expire or be revoked."
            )
        return (
            f"EGA rejected the credentials archivefs resolved from "
            f"{self.creds.describe()}. Check $EGA_PASSWORD is current."
        )

    # --------------------------------------------------------------- metadata

    def _split(self, path: str) -> tuple[str, str]:
        head, _, tail = path.partition("/")
        return head, tail

    def _get_metadata(self, path: str) -> Any:
        return self._get_json(f"{self.api_url}/metadata{path}")

    def authorized_datasets(self) -> list[str]:
        reply = self._get_metadata("/datasets") or []
        # API v1 returns bare accession strings, v2 returns objects.
        return sorted(e if isinstance(e, str) else e["datasetId"] for e in reply)

    def _container_info(self, container: str) -> dict:
        # A bare EGAF is a *file*, not a container: EGAF accessions are globally
        # unique, so ega://EGAF... needs no dataset context.
        if FILE_RE.match(container):
            return self._load_file(container)
        return {"name": container, "type": "directory", "size": 0}

    def _list_root(self) -> list[dict]:
        return [
            {"name": ds, "type": "directory", "size": 0}
            for ds in self.authorized_datasets()
        ]

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
        is_md5 = checksum_type.upper() == "MD5"
        info: dict[str, Any] = {
            "name": path,
            "type": "file",
            "size": plain_size,
            # Verified: md5(decrypted bytes) == plainChecksum, so this checksum
            # describes the bytes the user ends up with, download-free.
            # See docs/decisions.md D1.
            "checksum": checksum if is_md5 else None,
            "md5": checksum if is_md5 else None,
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
        stamp = re.search(r"\.(\d{10,})\.cip$", meta.get("fileName") or "")
        if stamp:
            # Deliberately NOT called `version_id`: DVC reads that key as a cloud
            # object version and switches on machinery we cannot honour (see
            # docs/decisions.md D6).
            info["archive_version"] = stamp.group(1)
        return info

    def _load_members(self, container: str) -> dict[str, dict]:
        if FILE_RE.match(container):
            info = self._load_file(container)
            return {container: info}
        if not DATASET_RE.match(container):
            raise ArchiveNotFoundError(
                f"{container!r} is not an EGA accession. Expected "
                f"ega://EGAD<digits>[/member] or ega://EGAF<digits>."
            )
        if container.startswith(LEGACY_DATASET_PREFIXES):
            authorized = self.authorized_datasets()
            if container not in authorized:
                raise ArchiveNotFoundError(
                    f"{container} is not available: it is either unknown to this "
                    f"account or one of EGA's legacy datasets, which the current "
                    f"API does not serve (contact helpdesk@ega-archive.org for "
                    f"those). Datasets this account is authorised for: "
                    f"{', '.join(authorized) or '(none)'}"
                )
        try:
            reply = self._get_metadata(f"/datasets/{container}/files") or []
        except ArchiveNotFoundError:
            raise ArchiveNotFoundError(
                f"dataset {container} not found. Datasets this account is "
                f"authorised for: {', '.join(self.authorized_datasets()) or '(none)'}"
            ) from None
        if not reply:
            raise ArchiveNotFoundError(
                f"dataset {container} returned no files. Either it is empty or the "
                f"account is not authorised for it; authorised datasets: "
                f"{', '.join(self.authorized_datasets()) or '(none)'}"
            )

        members: dict[str, dict] = {}
        taken: set[str] = set()
        for meta in reply:
            path = self._member_path(container, meta, taken)
            info = self._info_from_metadata(path, container, meta)
            members[path] = info
            self._file_cache[meta["fileId"]] = info
        return self._selected(members)

    def _load_file(self, file_id: str) -> dict:
        if file_id in self._file_cache:
            return self._file_cache[file_id]
        meta = self._get_metadata(f"/files/{file_id}")
        if not meta or not meta.get("fileId"):
            raise ArchiveNotFoundError(
                f"no metadata for {file_id}. EGA answers 200-with-empty-fields when "
                f"an account lacks access, so this most likely means you are not "
                f"authorised for it (check https://ega-archive.org/my-datasets)."
            )
        datasets = meta.get("datasetId") or []
        dataset = datasets[0] if isinstance(datasets, list) and datasets else None
        info = self._info_from_metadata(file_id, dataset, meta)
        self._file_cache[file_id] = info
        return info

    def _selected(self, members: dict[str, dict]) -> dict[str, dict]:
        if self.selection is None and self.include_index_files:
            return members
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
                f"selection matched none of the {len(members)} members. Selections "
                f"are matched against EGAF accessions and display names; the first "
                f"few available are: {', '.join(sorted(members)[:3])}"
            )
        return out

    def _match_member(
        self, members: dict[str, dict], path: str, member: str
    ) -> dict | None:
        """Accept any layout's spelling, so a ``.dvc`` stays resolvable.

        A ``.dvc`` file written while ``layout="accession"`` must still resolve if
        someone later configures ``layout="display"``; the accession is the
        identity, the layout is presentation.
        """
        wanted = member.split("/")[0]
        for member_path, info in members.items():
            ega = info.get("ega", {})
            if wanted in (ega.get("file_id"), ega.get("display_name")):
                return info
            if member_path.endswith(f"/{member}"):
                return info
        return None

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

    def _size_of(self, info: dict) -> int:
        size = info.get("size")
        if size is None:
            return self.plain_size(info["name"])
        return size
