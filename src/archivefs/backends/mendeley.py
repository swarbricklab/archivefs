"""``mendeley://`` — Mendeley Data deposits, addressed by DOI or dataset id.

**EXPERIMENTAL (issue #47).** This backend is built on Mendeley Data's *frontend*
API — the one its own website calls — because, as live testing established, Mendeley
exposes **no documented, stable, third-party file-manifest API**:

* the official ``/public-api/.../files`` endpoint, even with a valid OAuth user
  token, lists files only for datasets *you own* (a third-party deposit returns
  ``[]``); and
* the dataset metadata endpoint carries no file list.

What *does* work — fully anonymously — is the set of ``/public-api`` routes the
website drives (discovered by reading the site's JS bundle, ``docs/decisions.md``
D44):

* ``GET /public-api/datasets/{id}/folders/{version}`` → the folder list;
* ``GET /public-api/datasets/{id}/files?folder_id={fid}&version={N}`` → the files in
  one folder (paginated via literal ``$start``/``$limit`` params; ``fid=root`` for
  loose files);
* each file carries ``content_details`` with ``sha256_hash``, ``size`` and a
  **stable** ``download_url`` (``/public-files/.../file_downloaded``, whose nominal
  expiry is a century out) that 302-redirects to S3 and supports ranged reads.

So this is an *undocumented-but-functional* backend, not a contract Mendeley
promises to keep — which is the whole point of landing it on a branch: a working
prototype to take to Mendeley and ask for a documented per-file endpoint (ideally
with an md5). Two consequences follow and are deliberate:

* **Checksums are sha256, never md5.** Mendeley publishes no md5 (verified across
  all 33 files of the test deposit). So ``info()`` reports ``md5=None`` with the
  sha256 under ``checksum``/``version_id``. sha256 is a fine *immutable version
  token* (a published version is immutable), so version-aware pins work; but a
  *download-free* ``--no-download`` md5 pin is impossible, because DVC's cache is
  md5-keyed and the only way to an md5 is to download and hash.
* **Cloudflare blocks the default client User-Agent.** The ``requests`` / ``urllib``
  default UA 403s, so the session sends a browser-like UA.

URL grammar
-----------
::

    mendeley://10.17632/yzxtxn4nmd        # concept DOI -> directory, latest version
    mendeley://10.17632/yzxtxn4nmd/4      # a pinned version
    mendeley://yzxtxn4nmd                 # bare dataset id, latest version
    mendeley://yzxtxn4nmd/4               # bare id, pinned version
    mendeley://10.17632/yzxtxn4nmd/4/<folder>/<file>   # one file (in a folder)

Files are organised in folders, so a member path is ``<folder name>/<file name>``
(or just ``<file name>`` for a file at the deposit root). A leading bare-numeric
segment after the id is always read as the *version*.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any, ClassVar
from urllib.parse import quote

from archivefs.base import ArchiveFileSystem
from archivefs.errors import (
    ArchiveNotFoundError,
    NotSupportedError,
    VerificationError,
)

logger = logging.getLogger(__name__)

#: The public API root — the same host the website's own JS calls.
DEFAULT_API_URL = "https://data.mendeley.com/public-api"

#: Mendeley's DOI prefix, for reconstructing a concept DOI from a bare id.
DOI_PREFIX = "10.17632"

#: Cloudflare 403s the default python-requests / urllib User-Agent, so send a
#: browser-like one. Not an attempt to hide — it identifies archivefs — just enough
#: shape to get past the bot filter the website itself passes.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) archivefs/mendeley "
    "(+https://github.com/swarbricklab/archivefs)"
)

#: A dataset id is a short alphanumeric token; the version DOI appends ``.<version>``.
SUFFIX_RE = re.compile(r"^(?P<id>[A-Za-z0-9]+)(?:\.(?P<version>\d+))?$")

#: Files-endpoint page size (the website uses 10; we ask for more to cut requests).
PAGE_SIZE = 100


class MendeleyFileSystem(ArchiveFileSystem):
    """Read-only fsspec filesystem over Mendeley Data deposits (experimental).

    Anonymous — no credential. See the module docstring for why this is built on
    the frontend API and what that costs (sha256 not md5; undocumented routes).

    Parameters
    ----------
    api_url:
        Override the public API root (a mirror, or a test double).
    """

    protocol = "mendeley"
    archive_name = "Mendeley Data"
    #: Anonymous: everything this backend needs is public. (The official
    #: authenticated files API is owner-scoped and so useless for third-party
    #: deposits — D44 — which is exactly why there is no token here.)
    credentials: ClassVar[list] = []

    def __init__(self, api_url: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.api_url = self._setting("api_url", api_url, DEFAULT_API_URL).rstrip("/")
        #: container spec -> dataset JSON
        self._datasets: dict[str, dict] = {}

    def _make_session(self) -> Any:
        session = super()._make_session()
        # The one non-obvious requirement: without a browser-like UA, Cloudflare
        # 403s every request (verified — the default requests/urllib UA is blocked).
        session.headers["User-Agent"] = USER_AGENT
        return session

    def invalidate_cache(self, path: str | None = None) -> None:
        super().invalidate_cache(path)
        if path is None:
            self._datasets.clear()
            return
        container, _ = self.split_path(self._strip_protocol(path))
        self._datasets.pop(container, None)

    # ------------------------------------------------------------------ paths

    @staticmethod
    def split_path(path: str) -> tuple[str, str]:
        """Split into ``(container spec, member path)``.

        The container spec is the dataset identifier plus an optional version; the
        member path is ``<folder>/<file>`` (files live in folders) or just
        ``<file>``. A DOI is two segments (``10.17632/<id>``); a leading all-digits
        segment after the id is the version.
        """
        parts = [p for p in path.split("/") if p != ""]
        if not parts:
            return "", ""
        if parts[0].startswith("10."):
            if len(parts) < 2:
                raise ArchiveNotFoundError(
                    f"{path!r} looks like a DOI prefix with no suffix; expected "
                    f"mendeley://10.17632/<id>[/<version>][/<folder>/<file>]"
                )
            spec, rest = parts[:2], parts[2:]
        else:
            spec, rest = parts[:1], parts[1:]
        if rest and rest[0].isdigit():
            spec.append(rest[0])
            rest = rest[1:]
        return "/".join(spec), "/".join(rest)

    def _split(self, path: str) -> tuple[str, str]:
        return self.split_path(path)

    @staticmethod
    def _parse_spec(container: str) -> tuple[str, int | None]:
        """A container spec -> ``(dataset id, version or None)``."""
        parts = container.split("/")
        if parts[0].startswith("10."):
            if len(parts) < 2:
                raise ArchiveNotFoundError(
                    f"{container!r} is not a Mendeley dataset; expected a DOI "
                    f"(10.17632/<id>) or a dataset id."
                )
            suffix = parts[1]
            version = parts[2] if len(parts) > 2 else None
        else:
            suffix = parts[0]
            version = parts[1] if len(parts) > 1 else None
        match = SUFFIX_RE.match(suffix)
        if not match:
            raise ArchiveNotFoundError(
                f"{container!r} is not a Mendeley dataset id or DOI. Expected e.g. "
                f"mendeley://10.17632/yzxtxn4nmd or mendeley://yzxtxn4nmd."
            )
        resolved = version or match.group("version")
        return match.group("id"), (int(resolved) if resolved is not None else None)

    # --------------------------------------------------------------- metadata

    def _dataset(self, container: str) -> dict:
        """Fetch a dataset's JSON (anonymous), resolving latest or a pinned version."""
        if container in self._datasets:
            return self._datasets[container]
        dataset_id, version = self._parse_spec(container)
        url = f"{self.api_url}/datasets/{quote(dataset_id)}"
        if version is not None:
            url += f"?version={version}"
        try:
            dataset = self._get_json(url)
        except ArchiveNotFoundError:
            raise ArchiveNotFoundError(
                f"Mendeley has no dataset {container} (looked up id {dataset_id!r}"
                + (f", version {version}" if version is not None else "")
                + "). Check the DOI or dataset id."
            ) from None
        self._datasets[container] = dataset
        return dataset

    def _provenance(self, dataset: dict, requested: str | None = None) -> dict:
        dataset_id = dataset.get("id")
        version = dataset.get("version")
        version_doi = (dataset.get("doi") or {}).get("id")
        if version_doi and re.search(r"\.\d+$", version_doi):
            concept_doi = version_doi.rsplit(".", 1)[0]
        else:
            concept_doi = version_doi or f"{DOI_PREFIX}/{dataset_id}"
        resolved_from_concept = False
        if requested is not None:
            _, asked_version = self._parse_spec(requested)
            resolved_from_concept = asked_version is None
        return {
            "dataset_id": dataset_id,
            "version": version,
            "doi": version_doi,
            "concept_doi": concept_doi,
            "name": dataset.get("name"),
            "publish_date": dataset.get("publish_date"),
            "licence": (dataset.get("data_licence") or {}).get("short_name"),
            "resolved_from_concept": resolved_from_concept,
        }

    def _folders(self, dataset_id: str, version: int) -> list[dict]:
        """The deposit's folders (anonymous). Flat list of ``{id, name, …}``."""
        return (
            self._get_json(
                f"{self.api_url}/datasets/{quote(dataset_id)}/folders/{version}"
            )
            or []
        )

    def _folder_files(
        self, dataset_id: str, folder_id: str, version: int
    ) -> list[dict]:
        """Every file in one folder, following the ``$start``/``$limit`` paging.

        The ``$`` param names are literal (the API rejects percent-encoded ones),
        so the query string is built by hand rather than via a params dict.
        """
        out: list[dict] = []
        start = 0
        while True:
            url = (
                f"{self.api_url}/datasets/{quote(dataset_id)}/files"
                f"?folder_id={quote(folder_id)}&version={version}"
                f"&$start={start}&$limit={PAGE_SIZE}"
            )
            page = self._get_json(url) or []
            out.extend(page)
            if len(page) < PAGE_SIZE:
                return out
            start += PAGE_SIZE

    def _make_info(
        self, container: str, folder_name: str, entry: dict, provenance: dict
    ) -> dict:
        details = entry.get("content_details") or {}
        filename = entry.get("filename") or entry.get("name")
        relpath = f"{folder_name}/{filename}" if folder_name else filename
        size = details.get("size")
        if size is None:
            size = entry.get("size")
        sha256 = details.get("sha256_hash")
        return {
            "name": f"{container}/{relpath}",
            "type": "file",
            "size": size,
            # Mendeley publishes sha256, not md5 (D44). The md5 field DVC reads stays
            # None; the real published digest goes under checksum, and doubles as the
            # immutable per-entry version token (a published version is immutable).
            "md5": None,
            "checksum": sha256,
            "checksum_type": "SHA256" if sha256 else None,
            "version_id": sha256,
            "archive_version": provenance["doi"],
            # Stable per-file URL (nominal expiry a century out); 302s to S3 and
            # supports ranged reads.
            "url": details.get("download_url"),
            "mendeley": {
                **provenance,
                "folder": folder_name or None,
                "folder_id": entry.get("folder_id"),
                "file_id": entry.get("id"),
                "filename": filename,
                "sha256": sha256,
                "content_type": details.get("content_type"),
            },
        }

    def _load_members(self, container: str) -> dict[str, dict]:
        dataset = self._dataset(container)
        dataset_id = dataset.get("id")
        version = dataset.get("version")
        provenance = self._provenance(dataset, requested=container)
        if provenance["resolved_from_concept"]:
            logger.warning(
                "%s is a concept identifier; resolved to version %s (%s). The "
                "checksum pins the bytes, but nothing records the version — use "
                "mendeley://%s/%s to put the version in the URL.",
                container,
                version,
                provenance["doi"],
                provenance["concept_doi"],
                version,
            )
        folders = self._folders(dataset_id, version)
        folder_names = {f["id"]: f.get("name", f["id"]) for f in folders}
        members: dict[str, dict] = {}
        # Root (loose files), then each folder. The folders list is flat for the
        # deposits seen; a nested layout would need parent-walking, noted as a TODO.
        groups = [("root", "")] + [(fid, folder_names[fid]) for fid in folder_names]
        for folder_id, folder_name in groups:
            for entry in self._folder_files(dataset_id, folder_id, version):
                filename = entry.get("filename") or entry.get("name")
                if not filename:
                    continue
                info = self._make_info(container, folder_name, entry, provenance)
                members[info["name"]] = info
        if not members:
            raise ArchiveNotFoundError(
                f"{container} (version {version}) lists no files. If the deposit is "
                f"not empty, Mendeley may have changed its frontend API — this "
                f"backend is experimental (see the module docstring)."
            )
        return members

    def _container_info(self, container: str) -> dict:
        dataset = self._dataset(container)
        return {
            "name": container,
            "type": "directory",
            "size": 0,
            "mendeley": self._provenance(dataset, requested=container),
        }

    def _list_root(self) -> list[dict]:
        raise NotSupportedError(
            "Mendeley Data has no listable root: address a dataset, e.g. "
            "mendeley://10.17632/yzxtxn4nmd or mendeley://yzxtxn4nmd."
        )

    # --------------------------------------------------------- concept/version

    def concept(self, container: str) -> str | None:
        """The concept DOI (``mendeley:`` scheme — ``doi:`` belongs to Zenodo)."""
        provenance = self._container_info(container).get("mendeley") or {}
        concept_doi = provenance.get("concept_doi")
        return f"mendeley:{concept_doi}" if concept_doi else super().concept(container)

    def version_accession(self, container: str) -> str:
        """``<concept DOI>/<version>`` — the version a ``chiro://`` pin fetches."""
        provenance = self._container_info(container).get("mendeley") or {}
        concept_doi = provenance.get("concept_doi")
        version = provenance.get("version")
        if concept_doi and version is not None:
            return f"{concept_doi}/{version}"
        return super().version_accession(container)

    # --------------------------------------------------------------- transfer

    def _data_url(self, info: dict) -> str:
        url = info.get("url")
        if not url:
            raise OSError(
                f"Mendeley published no download URL for {info.get('name')}."
            )
        return url

    def get_file(self, rpath: str, lpath: str, **kwargs: Any) -> Any:
        """Download, then verify against the published **sha256** (not md5).

        The base verifies md5; Mendeley publishes sha256, so the whole-file check is
        re-implemented here against the digest archivefs actually has. Same
        fail-closed contract: a mismatch deletes the bytes rather than leaving them
        where DVC (which fetches with ``verify=False``) would pick them up.
        """
        import os

        info = self.info(rpath)
        super(ArchiveFileSystem, self).get_file(rpath, lpath, **kwargs)
        is_sha256 = info.get("checksum_type") == "SHA256"
        expected = info.get("checksum") if is_sha256 else None
        if (
            info.get("type") == "directory"
            or not expected
            or kwargs.get("outfile") is not None
            or kwargs.get("start") is not None
            or kwargs.get("end") is not None
        ):
            return
        digest = hashlib.sha256()
        with open(lpath, "rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            try:
                os.remove(lpath)
            except OSError:  # pragma: no cover - best-effort
                logger.debug("could not remove unverified %s", lpath)
            raise VerificationError(info.get("name", "?"), "sha256", expected, actual)

    # --------------------------------------------------------------- discovery

    def versions(self, container: str) -> list[dict]:
        """Every version of a dataset, newest first (anonymous)."""
        dataset = self._dataset(container)
        dataset_id = dataset.get("id")
        out: list[dict] = []
        for entry in dataset.get("versions") or []:
            number = entry.get("version")
            if number is None:
                continue
            try:
                out.append(self._provenance(self._dataset(f"{dataset_id}/{number}")))
            except ArchiveNotFoundError:  # pragma: no cover - defensive
                continue
        out.sort(key=lambda p: p.get("version") or 0, reverse=True)
        return out or [self._provenance(dataset)]

    def latest_version(self, container: str) -> dict:
        dataset_id, _ = self._parse_spec(container)
        return self._provenance(self._dataset(dataset_id))
