"""``web://`` — a file at a plain HTTPS URL, as one more archive.

Not every source a paper depends on lives in an archive. Journal supplements, lab
websites and release directories (MSigDB, GENCODE, a publisher's CDN) are reachable
only as a bare HTTPS URL, with no accession, no listing and — often — no checksum. DVC
can already import such a URL through its own ``https://`` filesystem, but that pins
the server's **ETag**, and that falls apart in two ways we have hit in practice:

* a server that sends *no* ETag (Elsevier's CDN, the Weizmann 3CA site) leaves DVC
  inventing a token it cannot reproduce, so a ``--no-download`` import never pulls;
* an ETag is not a content digest. Apache's is ``size-mtime``, so a re-upload of
  identical bytes "changes" the file, and a changed file served with a stale
  validator is not caught at all.

``web://`` exists so the fix for both is the one archivefs already has: a ``.chiro``
manifest pins the bytes by md5, and ``chiro://`` routes the read back here::

    web://ars.els-cdn.com/content/image/1-s2.0-S0092867425007937-mmc4.xlsx
    chiro://md5:<digest>/1-s2.0-S0092867425007937-mmc4.xlsx

URL grammar
-----------
``web://<host>/<path>`` is ``https://<host>/<path>``, byte for byte. The path is
**not** decoded or re-encoded: publisher URLs carry ``%3A``/``%2F`` inside a path
segment (``art%3A10.1038%2Fs41467-…``), and normalising them would request a
different resource. Only ``https`` is served; a plain ``http`` source is out of scope
because nothing fetched over it is worth pinning.

The **container is the host** and the member is the URL path. HTTP has no directory
listing, so a container lists only the files that have been looked at — each one by a
``HEAD`` the first time it is addressed. A ``chiro://`` pin over a subtree
(``record_accession = data.broadinstitute.org/gsea-msigdb/…/2024.1.Hs``) works because
the pin first announces its files with :meth:`WebFileSystem.expect_members`, which
probes each one; the base's prefix-based ``relpath`` then scopes them to the subtree
exactly as it does for a BIA subtree pin.

Checksums
---------
A web server publishes no content digest as a rule, so ``md5`` is ``None`` and
``checksum_source`` says why — with two exceptions that *are* content digests and are
used when present: ``Content-MD5`` (RFC 1864) and Google Cloud Storage's
``x-goog-hash: md5=…``. An ETag is recorded as ``archive_version`` (change detection),
never as a checksum.

Content encoding
----------------
The session asks for ``Accept-Encoding: identity``. Left to ``requests``' default
(``gzip, deflate``) many hosts compress text on the fly (GitHub raw, the Broad's MSigDB
mirror), and then every byte count describes the *compressed* response: ``HEAD``'s
``Content-Length`` is the gzip size, or is dropped altogether, and a ``Range`` is
ignored or applies to the gzip stream. A pin is of the file's own bytes, so archivefs
only ever asks for those, and a size from a response that is encoded anyway is not used.

User-Agent
----------
Some hosts refuse the ``python-requests`` default outright (the Weizmann 3CA site
resets the connection), so the session sends a browser-like UA that still names
archivefs — the same choice, for the same reason, as the Mendeley backend.
"""

from __future__ import annotations

import base64
import binascii
import logging
from collections.abc import Iterable
from typing import Any, ClassVar

from archivefs.base import ArchiveFileSystem
from archivefs.errors import ArchiveNotFoundError, NotSupportedError

logger = logging.getLogger(__name__)

__all__ = ["WebFileSystem"]

#: Browser-like, as for Mendeley (see the module docstring): enough shape to pass a
#: bot filter that a browser passes, while still identifying archivefs.
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) archivefs/web "
    "(+https://github.com/swarbricklab/archivefs)"
)

_SCHEMES = ("https://", "http://")


def _md5_from_headers(headers: Any) -> str | None:
    """A content md5 the server published, as hex — or ``None``.

    Only headers defined to be a digest of the body count. An ETag never does: it is
    an opaque validator, and on the servers we use it is ``size-mtime`` (Apache), a
    sha256 (GitHub raw) or an md5 only by coincidence of implementation (GCS).
    """
    candidates = []
    content_md5 = headers.get("Content-MD5")
    if content_md5:
        candidates.append(content_md5.strip())
    for part in (headers.get("x-goog-hash") or "").split(","):
        name, _, value = part.strip().partition("=")
        if name == "md5" and value:
            candidates.append(value.strip())
    for value in candidates:
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError):
            continue
        if len(raw) == 16:
            return raw.hex()
    return None


def _is_encoded(headers: Any) -> bool:
    """Whether a response's body is a content-coded rendering (gzip, br, …)."""
    encoding = (headers.get("Content-Encoding") or "").strip().lower()
    return encoding not in ("", "identity")


class WebFileSystem(ArchiveFileSystem):
    """Read-only fsspec filesystem over individual files at HTTPS URLs.

    Anonymous. No listing: a file is known once it has been addressed (or announced
    by a ``chiro://`` pin through :meth:`expect_members`).
    """

    protocol = "web"
    archive_name = "web"
    credentials: ClassVar[list] = []

    def _make_session(self) -> Any:
        session = super()._make_session()
        session.headers["User-Agent"] = USER_AGENT
        # The file's own bytes, never a compressed rendering of them: see the module
        # docstring. Sizes, ranges and the hash a manifest records all depend on it.
        session.headers["Accept-Encoding"] = "identity"
        return session

    # ------------------------------------------------------------------- paths

    @classmethod
    def _strip_protocol(cls, path: str) -> str:
        """Accept ``web://host/p``, ``host/p`` and a pasted ``https://host/p``.

        The last matters because a ``url:`` concept carries the full URL, and a
        manifest that predates ``record_accession`` routes on it.
        """
        if isinstance(path, str):
            for prefix in (f"{cls.protocol}://", f"{cls.protocol}:"):
                if path.startswith(prefix):
                    path = path[len(prefix) :]
                    break
            if path.startswith("http://"):
                raise NotSupportedError(
                    f"web:// serves https only, and {path!r} is plain http — bytes "
                    f"fetched without TLS are not worth pinning. Use the https URL "
                    f"if the host offers one."
                )
            if path.startswith("https://"):
                path = path[len("https://") :]
        return path.lstrip("/").rstrip("/")

    def _auth_remediation(self, status: int, url: str) -> str:
        # There is no credential to blame: web:// sends none. A 401/403 from a
        # public host is a login wall or a bot filter.
        return (
            f"{url} refused an anonymous request (HTTP {status}). web:// sends no "
            f"credentials, so the file is behind a login, or the host is blocking "
            f"automated clients. Check that the URL opens in a private browser "
            f"window; if it does not, it cannot be pinned through web://."
        )

    def _split(self, path: str) -> tuple[str, str]:
        host, _, member = path.partition("/")
        return host, member

    def url_for(self, path: str) -> str:
        """The https URL a ``web://`` path names."""
        return f"https://{self._strip_protocol(path)}"

    # ---------------------------------------------------------------- metadata

    def _load_members(self, container: str) -> dict[str, dict]:
        # Nothing to list: HTTP has no index. Members are added as they are probed.
        return {}

    def _list_root(self) -> list[dict]:
        raise NotSupportedError(
            "web:// has no listable root: address a file, e.g. "
            "web://example.org/data/file.csv."
        )

    def _probe(self, path: str) -> dict | None:
        """``HEAD`` one URL and describe it, or ``None`` if the server says 404."""
        url = f"https://{path}"
        try:
            response = self._request("HEAD", url)
        except ArchiveNotFoundError:
            return None
        headers = response.headers
        length = headers.get("Content-Length", "")
        if _is_encoded(headers):
            # A length of the compressed response, not of the file.
            length = ""
        md5 = _md5_from_headers(headers)
        etag = headers.get("ETag")
        modified = headers.get("Last-Modified")
        info: dict[str, Any] = {
            "name": path,
            "type": "file",
            "size": int(length) if length.isdigit() else None,
            "md5": md5,
            "checksum": md5,
            "checksum_source": (
                "the server's Content-MD5 / x-goog-hash header"
                if md5
                else "none — a web server publishes no content checksum; pin it "
                "with a .chiro manifest for verification"
            ),
            "url": url,
            "web": {
                "etag": etag,
                "last_modified": modified,
                "content_type": headers.get("Content-Type"),
                "accept_ranges": headers.get("Accept-Ranges"),
            },
        }
        if etag or modified:
            # Change detection, not content identity — see the module docstring.
            info["archive_version"] = etag or modified
        return info

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        #: Directories a ``chiro://`` pin has announced. They are directories even
        #: when every file under them has gone, and must never be HEAD-probed: many
        #: servers answer a directory URL with an HTML page, which would then be
        #: taken for a file.
        self._expected_dirs: set[str] = set()

    def _match_member(
        self, members: dict[str, dict], path: str, member: str
    ) -> dict | None:
        """Any path not seen yet is looked up live, then remembered."""
        if any(d == path or d.startswith(f"{path}/") for d in self._expected_dirs):
            return {"name": path, "type": "directory", "size": 0}
        info = self._probe(path)
        if info is not None:
            with self._member_lock:
                members[path] = info
        return info

    def expect_members(self, container: str, relpaths: Iterable[str]) -> None:
        """Probe the files a pin says live under ``container``, so a listing shows them.

        The hook :class:`~archivefs.backends.chiro.ChiroFileSystem` calls before it
        lists a delegate. Every other backend lists natively and ignores it; this one
        cannot list, so without it every file of a ``web`` pin would read as
        missing upstream. A file that 404s is left out, and the manifest layer then
        reports it as drift — which is what it is.
        """
        container = self._strip_protocol(container)
        host, _ = self._split(container)
        members = self._members(host)
        if container != host:
            self._expected_dirs.add(container)
        for relpath in relpaths:
            path = f"{container}/{relpath}" if relpath else container
            if path in members:
                continue
            self._match_member(members, path, relpath)

    def info(self, path: str, **kwargs: Any) -> dict:
        """Like the base, but a file always has a size.

        A server that omits ``Content-Length`` on ``HEAD`` usually still answers a
        one-byte range with ``Content-Range: bytes 0-0/<total>``.
        """
        info = super().info(path, **kwargs)
        if info.get("type") == "file" and info.get("size") is None:
            self._size_of(info)
        return info

    def _size_of(self, info: dict) -> int:
        size = info.get("size")
        if size is not None:
            return size
        response = self._request(
            "GET", info["url"], headers={"Range": "bytes=0-0"}, stream=True
        )
        try:
            # Content-Range of an encoded response counts encoded bytes.
            total = (
                None if _is_encoded(response.headers) else self._range_total(response)
            )
        finally:
            response.close()
        if total is None:
            raise OSError(
                f"{info['url']} reports no Content-Length and ignores Range, so its "
                f"size cannot be known without downloading it."
            )
        info["size"] = total
        return total

    def concept(self, container: str) -> str | None:
        """``url:https://<host>/<path>`` — the URL itself is the only tracker."""
        return f"url:{self.url_for(container)}"

    # --------------------------------------------------------------- transfer

    def _data_url(self, info: dict) -> str:
        return info["url"]
