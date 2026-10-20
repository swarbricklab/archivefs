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

    cbioportal://brca_metabric                         # study -> dir (latest on master)
    cbioportal://brca_metabric/data_clinical_sample.txt     # one member file
    cbioportal://brca_metabric/case_lists/cases_all.txt     # a file in a subdir
    cbioportal://brca_metabric@v1.0.0                  # pinned to a tag
    cbioportal://brca_metabric@86690e1                 # pinned to a commit SHA
    cbioportal://brca_metabric@v1.0.0/data_mutations.txt    # a member at that version

Versioning is by **git ref**: the datahub evolves on ``master`` and each commit that
touches ``public/<study>/`` is a release of that study (:meth:`versions` lists them).
``<study>@<ref>`` pins a version — a tag, a commit SHA, or a branch — and
:meth:`version_accession` resolves whatever ref was addressed to the study's **head
commit** at that ref, so a ``.chiro`` pin is immutable (a later ``master`` move cannot
change it) *and* stable (an unrelated study's update does not move it). The git ref
carries no slash, so it attaches to the study segment rather than taking a path
position. Within a version the per-file sha256 still pins the exact bytes.
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
        # The container is the first segment, `<study>` or `<study>@<ref>`; a git
        # ref carries no slash, so partitioning on the first "/" leaves it intact.
        head, _, tail = path.partition("/")
        return head, tail

    def _parse_container(self, container: str) -> tuple[str, str]:
        """``<study>[@<ref>]`` -> ``(study, ref)``, defaulting ref to ``self.ref``.

        The ``@<ref>`` is how a specific *version* is pinned in the URL: a commit
        SHA, a tag (``v1.0.0``), or a branch. Without it the filesystem's ``ref``
        (``master`` by default) is used.
        """
        study, sep, ref = container.partition("@")
        return study, (ref if sep else self.ref)

    def _study_path(self, study: str) -> str:
        return f"{PATH_PREFIX}/{study}"

    # --------------------------------------------------------------- metadata

    def _contents(self, repo_path: str, ref: str) -> list[dict]:
        """One directory's entries from the GitHub contents API, at ``ref``."""
        url = (
            f"{self.api_url}/repos/{self.repo}/contents/{quote(repo_path)}"
            f"?ref={quote(ref)}"
        )
        reply = self._get_json(url)
        if isinstance(reply, dict):  # a file, not a directory
            return [reply]
        return reply or []

    def _resolve_file(
        self, container: str, study: str, ref: str, entry: dict
    ) -> dict:
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
        name = f"{container}/{member_rel}"

        body = self._request("GET", raw_url).content
        is_lfs = body.startswith(LFS_MAGIC)
        if is_lfs:
            fields = self._parse_lfs_pointer(body)
            sha256 = fields.get("sha256")
            size = fields.get("size")
            # LFS bytes come from the media endpoint; raw only serves the pointer.
            content_url = (
                f"{self.media_url}/{self.repo}/{quote(ref)}/{quote(repo_path)}"
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
                "ref": ref,
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

    def _walk_study(self, study: str, ref: str) -> list[dict]:
        """Every file under a study at ``ref``, recursing subdirs (``case_lists/``)."""
        root = self._study_path(study)
        try:
            top = self._contents(root, ref)
        except ArchiveNotFoundError:
            raise ArchiveNotFoundError(
                f"cBioPortal has no study {study!r} at ref {ref!r} in {self.repo} "
                f"(looked for {root}). Check the study id and version, e.g. "
                f"cbioportal://brca_metabric or cbioportal://brca_metabric@v1.0.0."
            ) from None
        files: list[dict] = []
        stack = list(top)
        while stack:
            entry = stack.pop()
            if entry.get("type") == "dir":
                stack.extend(self._contents(entry["path"], ref))
            elif entry.get("type") == "file":
                files.append(entry)
        return files

    def _load_members(self, container: str) -> dict[str, dict]:
        study, ref = self._parse_container(container)
        members: dict[str, dict] = {}
        for entry in self._walk_study(study, ref):
            info = self._resolve_file(container, study, ref, entry)
            members[info["name"]] = info
        if not members:
            raise ArchiveNotFoundError(
                f"study {study} lists no files at ref {ref!r} in {self.repo}."
            )
        return members

    def _list_root(self) -> list[dict]:
        raise NotSupportedError(
            "cBioPortal has no listable root: address a study, e.g. "
            "cbioportal://brca_metabric."
        )

    # ----------------------------------------------------- versions / pinning

    def _study_commits(self, study: str, ref: str, per_page: int = 30) -> list[dict]:
        """Commits that touched a study's directory, newest first, at/under ``ref``.

        This is a study's *version history*: the datahub evolves on ``master`` and
        each commit that changes ``public/<study>/`` is a distinct release of the
        data (for ``brca_metabric``, gene-table migrations, panel additions, …).
        """
        url = (
            f"{self.api_url}/repos/{self.repo}/commits"
            f"?path={quote(self._study_path(study))}&sha={quote(ref)}"
            f"&per_page={per_page}"
        )
        return self._get_json(url) or []

    def _commit_provenance(self, study: str, commit: dict) -> dict:
        info = commit.get("commit") or {}
        sha = commit.get("sha")
        return {
            "study": study,
            "commit": sha,
            # the version-pinned accession: a chiro:// pin fetches through this, so
            # a later `master` move cannot change the bytes it resolves to.
            "accession": f"{study}@{sha}" if sha else study,
            "date": (info.get("committer") or {}).get("date"),
            "message": (info.get("message") or "").splitlines()[0] if info else None,
        }

    def concept(self, container: str) -> str | None:
        """``cbioportal:<study>`` — tracks the study on the default branch.

        The concept is version-*less*: it is what ``archivefs update``/``check``
        re-resolve to ask "is there a newer commit for this study". The pinned
        version lives in :meth:`version_accession`.
        """
        study, _ = self._parse_container(self._strip_protocol(container))
        return f"cbioportal:{study}"

    def version_accession(self, container: str) -> str:
        """``<study>@<commit sha>`` — the immutable version a pin fetches through.

        Resolves whatever ref was addressed (``master``, a tag, a short SHA) to the
        **study's head commit** at that ref — the newest commit that actually
        touched ``public/<study>/``. Pinning that rather than the bare ref makes the
        pin both immutable and stable: two pins taken while ``master`` moved but the
        study did not resolve to the same commit.
        """
        container = self._strip_protocol(container)
        study, ref = self._parse_container(container)
        commits = self._study_commits(study, ref, per_page=1)
        if commits and commits[0].get("sha"):
            return f"{study}@{commits[0]['sha']}"
        return container

    def versions(self, container: str) -> list[dict]:
        """Every version of a study (commits touching it), newest first.

        What ``dvc update`` / ``archivefs check`` compare a pin against.
        """
        study, ref = self._parse_container(self._strip_protocol(container))
        return [
            self._commit_provenance(study, commit)
            for commit in self._study_commits(study, ref)
        ]

    def latest_version(self, container: str) -> dict:
        study, _ = self._parse_container(self._strip_protocol(container))
        commits = self._study_commits(study, self.ref, per_page=1)
        if not commits:
            return {"study": study, "commit": None, "accession": study}
        return self._commit_provenance(study, commits[0])

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
