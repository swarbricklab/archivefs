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

Zenodo is the reference case for a reason: every file comes with an MD5 in the
record metadata, so ``info()`` is download-free *and* content-verified, and a
version DOI is immutable — which is what makes the ZIP integrity argument below
sound rather than merely convenient.
"""

from __future__ import annotations

import logging
import re
from typing import Any, ClassVar

from archivefs.base import ArchiveFileSystem
from archivefs.credentials import Credential
from archivefs.errors import ArchiveNotFoundError, NotSupportedError

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://zenodo.org/api"
#: Sandbox for rehearsing deposits; same API shape.
SANDBOX_API_URL = "https://sandbox.zenodo.org/api"

#: ``10.5281/zenodo.1486184``, and the record id inside it.
DOI_RE = re.compile(
    r"^(?P<prefix>10\.\d{4,9})/(?P<suffix>zenodo\.(?P<recid>\d+))$", re.I
)
RECORD_RE = re.compile(r"^\d+$")


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
        interiors through fsspec's ``ZipFileSystem``. Off by default: some ZIPs
        are genuine archival units and should not be sprayed across a workspace.
        The explicit per-URL form is the fsspec chain
        ``zip://::zenodo://record/archive.zip``; this flag is the per-remote
        default, and both run the same code underneath.
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
        zip_limits: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.api_url = api_url.rstrip("/")
        self.extract_zips = extract_zips
        self.zip_limits = zip_limits
        self.token = self.creds.get("token")
        self.token_source = self.creds.source_of("token") if self.token else "anonymous"
        #: record spec or id -> record JSON
        self._records: dict[str, dict] = {}
        #: zip member path -> ZipFileSystem, created lazily
        self._zip_views: dict[str, Any] = {}

    # ------------------------------------------------------------------- auth

    def _auth_headers(self) -> dict:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def _auth_remediation(self, status: int, url: str) -> str:
        if not self.token:
            return (
                "this record is restricted or embargoed. Put a personal access "
                "token in $ZENODO_TOKEN (or `dvc remote modify --local <remote> "
                "token ...`) — never in the committed .dvc file."
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
        provenance = self._record_provenance(record, requested=container)
        members: dict[str, dict] = {}
        for entry in files:
            key = entry.get("key") or entry.get("filename")
            if not key:
                continue
            checksum = entry.get("checksum") or ""
            algorithm, _, digest = checksum.partition(":")
            if not digest:  # some responses give a bare hex digest
                algorithm, digest = "md5", checksum
            is_md5 = algorithm.lower() == "md5"
            members[f"{container}/{key}"] = {
                "name": f"{container}/{key}",
                "type": "file",
                "size": entry.get("size") or entry.get("filesize"),
                "checksum": digest if is_md5 else None,
                "md5": digest if is_md5 else None,
                "checksum_type": algorithm.upper() or None,
                # The version DOI is an immutable token for this exact byte
                # sequence — the load-bearing part of the ZIP integrity argument.
                "version_id": provenance["doi"],
                "url": (entry.get("links") or {}).get(
                    "content", f"{self.api_url}/records/{recid}/files/{key}/content"
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
        raise ArchiveNotFoundError(
            f"{member!r} is not in Zenodo record {container}. It holds: "
            f"{', '.join(sorted(k.split('/', 1)[-1] for k in members)) or '(no files)'}"
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
        keys = [p.split("/", 1)[-1] for p in self._members(container)]
        # longest ZIP key that prefixes this path
        for key in sorted(keys, key=len, reverse=True):
            if not key.lower().endswith(".zip"):
                continue
            if member == key:
                return f"{container}/{key}", ""
            if member.startswith(f"{key}/"):
                return f"{container}/{key}", member[len(key) + 1 :]
        return None

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
        from fsspec.implementations.zip import ZipFileSystem

        from archivefs.zipsafe import inspect_zip

        zip_path = self._strip_protocol(zip_path)
        if zip_path not in self._zip_views:
            view = ZipFileSystem(fo=self._open_container(zip_path))
            inspect_zip(view.zip, self.zip_limits, target=zip_path)
            self._zip_views[zip_path] = view
        return self._zip_views[zip_path]

    def _open_container(self, path: str):
        """Open a ZIP member *as the file it is*, bypassing the directory view.

        Needed because with ``extract_zips`` on, ``info()`` reports the ZIP as a
        directory — which is the point — so the ordinary open path would refuse.
        """
        from archivefs.base import ArchiveFile

        container, member = self.split_path(self._strip_protocol(path))
        return ArchiveFile(
            self,
            self._member(container, member),
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

    # ---------------------------------------------------- read surface (+ZIP)

    def ls(self, path: str = "", detail: bool = True, **kwargs: Any) -> list:
        path = self._strip_protocol(path)
        inside = self._zip_container(path) if path else None
        if inside:
            zip_path, interior = inside
            view = self.zip_view(zip_path)
            entries = [
                self._zip_info(zip_path, entry["name"].lstrip("/"))
                for entry in view.ls(interior, detail=True)
            ]
            return entries if detail else [e["name"] for e in entries]

        entries = super().ls(path, detail=True, **kwargs)
        if self.extract_zips:
            entries = [
                {**e, "type": "directory"}
                if str(e.get("name", "")).lower().endswith(".zip")
                else e
                for e in entries
            ]
        return entries if detail else [e["name"] for e in entries]

    def info(self, path: str, **kwargs: Any) -> dict:
        path = self._strip_protocol(path)
        inside = self._zip_container(path) if path else None
        if inside:
            zip_path, interior = inside
            if not interior:
                container = self._member(*self.split_path(zip_path))
                return {**container, "type": "directory"}
            return self._zip_info(zip_path, interior)
        return super().info(path, **kwargs)

    def cat_file(
        self, path: str, start: int | None = None, end: int | None = None, **kwargs: Any
    ) -> bytes:
        path = self._strip_protocol(path)
        inside = self._zip_container(path)
        if inside and inside[1]:
            zip_path, interior = inside
            return self.zip_view(zip_path).cat_file(interior)[start:end]
        return super().cat_file(path, start=start, end=end, **kwargs)

    def _open(self, path: str, mode: str = "rb", **kwargs: Any):
        path = self._strip_protocol(path)
        inside = self._zip_container(path) if "r" in mode else None
        if inside and inside[1]:
            zip_path, interior = inside
            return self.zip_view(zip_path)._open(interior, mode=mode)
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
