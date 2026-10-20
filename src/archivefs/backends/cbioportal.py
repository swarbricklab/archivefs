"""``cbioportal://`` — cBioPortal studies, via the ``cBioPortal/datahub`` GitHub repo.

**EXPERIMENTAL (issue #47).** The issue proposed addressing a cBioPortal study's
member files and flagged three blockers for the obvious route (the datahub S3
tarball): it is a whole-study ``.tar.gz`` with no per-file URLs, carries no per-file
checksums, and 403s from our network. All three dissolve once you notice the datahub
*is also a GitHub repo* — ``cBioPortal/datahub``, with each study under
``public/<study>/`` — whose large data files are tracked with **git-lfs**. That gives
exactly what archivefs wants, and on documented, stable infrastructure rather than a
reverse-engineered site (so this is a firmer footing than ``mendeley://``):

* **Per-file listing** — the GitHub contents API lists a study directory (and its
  ``case_lists/`` subdir), one request each.
* **Download-free sha256** — fetching an LFS-tracked file's *raw* URL returns the LFS
  *pointer* (``oid sha256:…`` + ``size``), not the bytes, so the content digest and
  size are known without moving the (often multi-GB) payload. Small non-LFS files
  (``meta_*``, case lists) return their real content, which is hashed in place — so
  **every** member gets a sha256.
* **Ranged content** — an LFS file's bytes come from
  ``media.githubusercontent.com/media/…`` (HTTP 206, Range-capable); a non-LFS file's
  come from its ``raw.githubusercontent.com`` URL.

Consequences, the same shape as ``mendeley://`` (``docs/decisions.md`` D44):

* **Checksums are sha256, not md5.** ``info()`` reports ``md5=None`` with the sha256
  under ``checksum`` and as the ``version_id``. The LFS oid *is* the content's
  immutable identity, so version-aware pins and reviewer pull-through work; a
  download-free ``--no-download`` md5 pin does not (DVC's cache is md5-keyed).
* **GitHub needs a User-Agent** (its API 403s without one), and its unauthenticated
  API allows 60 requests/hour — enough to list a study (a couple of calls), since the
  raw/LFS fetches do not count against it. A ``$GITHUB_TOKEN`` raises the limit.

URL grammar
-----------
::

    cbioportal://brca_metabric                         # study -> directory
    cbioportal://brca_metabric/data_clinical_sample.txt     # one member file
    cbioportal://brca_metabric/case_lists/cases_all.txt     # a file in a subdir

Studies are not versioned like a DOI; the datahub repo evolves on ``master``. The
per-file sha256 still pins the exact bytes, and a ``.chiro`` pin records it; a future
revision can address a specific git ref.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, ClassVar
from urllib.parse import quote

from archivefs.base import ArchiveFileSystem
from archivefs.credentials import Credential
from archivefs.errors import ArchiveNotFoundError, NotSupportedError

logger = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.github.com"
DEFAULT_MEDIA_URL = "https://media.githubusercontent.com/media"
#: owner/repo holding the studies, and the branch + path prefix they live under.
DEFAULT_REPO = "cBioPortal/datahub"
DEFAULT_REF = "master"
PATH_PREFIX = "public"

#: GitHub requires a User-Agent or returns 403.
USER_AGENT = (
    "archivefs/cbioportal (+https://github.com/swarbricklab/archivefs)"
)

#: The first line of every git-lfs pointer file.
LFS_MAGIC = b"version https://git-lfs.github.com/spec/v1"


class CBioPortalFileSystem(ArchiveFileSystem):
    """Read-only fsspec filesystem over cBioPortal datahub studies (experimental).

    Anonymous; an optional ``$GITHUB_TOKEN`` only raises GitHub's API rate limit.

    Parameters
    ----------
    api_url, media_url:
        Override the GitHub API / LFS media endpoints (a mirror, or a test double).
    repo, ref:
        The datahub repository (``owner/repo``) and git ref. Defaults pin the public
        datahub on ``master``.
    """

    protocol = "cbioportal"
    archive_name = "cBioPortal"
    credentials: ClassVar[list[Credential]] = [
        Credential(
            name="token",
            env=("GITHUB_TOKEN", "GH_TOKEN"),
            files=("~/.config/archivefs/cbioportal.json",),
            secret=True,
            required=False,
            help=(
                "a GitHub token, optional — it only raises the API rate limit "
                "(60/hour anonymous) for listing studies."
            ),
        )
    ]

    def __init__(
        self,
        api_url: str | None = None,
        media_url: str | None = None,
        repo: str | None = None,
        ref: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.api_url = self._setting("api_url", api_url, DEFAULT_API_URL).rstrip("/")
        self.media_url = self._setting(
            "media_url", media_url, DEFAULT_MEDIA_URL
        ).rstrip("/")
        self.repo = self._setting("repo", repo, DEFAULT_REPO).strip("/")
        self.ref = self._setting("ref", ref, DEFAULT_REF)
        self.token = self.creds.get("token")

    def _make_session(self) -> Any:
        session = super()._make_session()
        session.headers["User-Agent"] = USER_AGENT
        session.headers["Accept"] = "application/vnd.github+json"
        return session

    # ------------------------------------------------------------------- auth

    def _auth_headers(self) -> dict:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        return {}

    def _auth_remediation(self, status: int, url: str) -> str:
        return (
            "GitHub rate-limited or refused the request. Set $GITHUB_TOKEN to raise "
            "the 60-requests/hour anonymous limit (it need only be a token with no "
            "scopes, for public data)."
        )

    # ------------------------------------------------------------------ paths

    def _split(self, path: str) -> tuple[str, str]:
        head, _, tail = path.partition("/")
        return head, tail

    def _study_path(self, study: str) -> str:
        return f"{PATH_PREFIX}/{study}"

    # --------------------------------------------------------------- metadata

    def _contents(self, repo_path: str) -> list[dict]:
        """One directory's entries from the GitHub contents API."""
        url = (
            f"{self.api_url}/repos/{self.repo}/contents/{quote(repo_path)}"
            f"?ref={quote(self.ref)}"
        )
        reply = self._get_json(url)
        if isinstance(reply, dict):  # a file, not a directory
            return [reply]
        return reply or []

    def _resolve_file(self, study: str, entry: dict) -> dict:
        """Build a member ``info`` from a contents entry, resolving its sha256.

        Fetches the file's *raw* bytes once: an LFS-tracked file returns only its
        pointer (so its sha256 and real size are read without downloading the
        payload), while a small plain file returns its content, which is hashed in
        place. Either way the member ends up with a real sha256.
        """
        repo_path = entry["path"]
        raw_url = entry.get("download_url")
        # relpath from the study root — may include a subdir like case_lists/.
        member_rel = self.relpath(self._study_path(study), repo_path)
        name = f"{study}/{member_rel}"

        body = self._request("GET", raw_url).content
        is_lfs = body.startswith(LFS_MAGIC)
        if is_lfs:
            fields = self._parse_lfs_pointer(body)
            sha256 = fields.get("sha256")
            size = fields.get("size")
            # LFS bytes come from the media endpoint; raw only serves the pointer.
            content_url = (
                f"{self.media_url}/{self.repo}/{quote(self.ref)}/{quote(repo_path)}"
            )
        else:
            # A small plain file (meta_*, case lists): raw already gave the content,
            # so hash it in place and serve it from raw.
            sha256 = hashlib.sha256(body).hexdigest()
            size = len(body)
            content_url = raw_url
        return {
            "name": name,
            "type": "file",
            "size": size,
            "md5": None,
            "checksum": sha256,
            "checksum_type": "SHA256" if sha256 else None,
            "version_id": sha256,
            "url": content_url,
            "cbioportal": {
                "study": study,
                "repo": self.repo,
                "ref": self.ref,
                "repo_path": repo_path,
                "git_lfs": is_lfs,
                "sha256": sha256,
            },
        }

    @staticmethod
    def _parse_lfs_pointer(body: bytes) -> dict:
        """``oid sha256:<hex>`` and ``size <n>`` out of an LFS pointer file."""
        out: dict[str, Any] = {}
        for line in body.decode("utf-8", "replace").splitlines():
            if line.startswith("oid sha256:"):
                out["sha256"] = line[len("oid sha256:") :].strip()
            elif line.startswith("size "):
                value = line[len("size ") :].strip()
                if value.isdigit():
                    out["size"] = int(value)
        return out

    def _walk_study(self, study: str) -> list[dict]:
        """Every file under a study, recursing subdirectories (e.g. ``case_lists/``)."""
        root = self._study_path(study)
        try:
            top = self._contents(root)
        except ArchiveNotFoundError:
            raise ArchiveNotFoundError(
                f"cBioPortal has no study {study!r} in {self.repo} (looked for "
                f"{root}). Check the study id, e.g. cbioportal://brca_metabric."
            ) from None
        files: list[dict] = []
        stack = list(top)
        while stack:
            entry = stack.pop()
            if entry.get("type") == "dir":
                stack.extend(self._contents(entry["path"]))
            elif entry.get("type") == "file":
                files.append(entry)
        return files

    def _load_members(self, container: str) -> dict[str, dict]:
        study = container
        members: dict[str, dict] = {}
        for entry in self._walk_study(study):
            info = self._resolve_file(study, entry)
            members[info["name"]] = info
        if not members:
            raise ArchiveNotFoundError(
                f"study {study} lists no files in {self.repo}."
            )
        return members

    def _list_root(self) -> list[dict]:
        raise NotSupportedError(
            "cBioPortal has no listable root: address a study, e.g. "
            "cbioportal://brca_metabric."
        )

    # --------------------------------------------------------------- transfer

    def _data_url(self, info: dict) -> str:
        url = info.get("url")
        if not url:
            raise OSError(f"no content URL for {info.get('name')}.")
        return url

    def get_file(self, rpath: str, lpath: str, **kwargs: Any) -> Any:
        """Download, then verify against the published **sha256** (not md5)."""
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
            from archivefs.errors import VerificationError

            try:
                os.remove(lpath)
            except OSError:  # pragma: no cover - best-effort
                logger.debug("could not remove unverified %s", lpath)
            raise VerificationError(info.get("name", "?"), "sha256", expected, actual)
