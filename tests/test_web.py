"""``web://`` — plain HTTPS files, offline against a fake server.

The fake reproduces the behaviours that made this backend necessary, each seen on a
real host this project pins: no ETag at all (Elsevier's CDN), a server that ignores
``Range`` and sends the whole body (the Weizmann 3CA site), a ``size-mtime`` ETag
(Apache, the Broad's MSigDB mirror) and a real ``x-goog-hash`` md5 (HGNC on GCS), plus
percent-encoded path segments that must reach the server untouched (Springer).
"""

from __future__ import annotations

import base64
import hashlib

import pytest
from tests.fake_http import FakeRequestsModule, FakeResponse, ranged

from archivefs import base as base_mod
from archivefs.backends.chiro import ChiroFileSystem
from archivefs.backends.web import USER_AGENT, WebFileSystem, _md5_from_headers
from archivefs.errors import (
    ArchiveNotFoundError,
    AuthenticationError,
    NotSupportedError,
    VerificationError,
)
from archivefs.registry import PROTOCOLS, protocol_for_concept

HOST = "cdn.example.org"
DIR = f"{HOST}/content/supp"
WORKBOOK = "table-s4.xlsx"
ENCODED = "art%3A10.1038%2Fs41467-021-27651-4/MediaObjects/esm9.xlsx"
BODIES = {
    f"/content/supp/{WORKBOOK}": b"PK\x03\x04 workbook " * 300,
    "/content/supp/notes.txt": b"some notes\n" * 50,
    f"/esm/{ENCODED}": b"springer bytes" * 40,
}


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324


class FakeWeb:
    """One host, configurable per test."""

    def __init__(self) -> None:
        self.bodies = dict(BODIES)
        self.ignore_range = False
        self.send_length_on_head = True
        self.extra_headers: dict[str, dict] = {}
        self.forbidden: set[str] = set()
        self.requests_made: list[tuple[str, str, dict]] = []

    def __call__(self, method: str, url: str, headers: dict, kwargs: dict):
        self.requests_made.append((method, url, headers))
        prefix = f"https://{HOST}"
        if not url.startswith(prefix):
            return FakeResponse(404, b"not found")
        path = url[len(prefix) :]
        if path in self.forbidden:
            return FakeResponse(403, b"blocked")
        body = self.bodies.get(path)
        if body is None:
            return FakeResponse(404, b"not found")
        extra = self.extra_headers.get(path, {})
        if method == "HEAD":
            head = dict(extra)
            if self.send_length_on_head:
                head["Content-Length"] = str(len(body))
            return FakeResponse(200, b"", head)
        if self.ignore_range:
            return FakeResponse(200, body, {"Content-Length": str(len(body)), **extra})
        return ranged(body, headers)


@pytest.fixture
def server(monkeypatch):
    fake = FakeWeb()
    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(fake))
    return fake


@pytest.fixture
def fs(server):
    return WebFileSystem(skip_instance_cache=True)


# ----------------------------------------------------------------- addressing


@pytest.mark.parametrize(
    "given",
    [
        f"web://{DIR}/{WORKBOOK}",
        f"{DIR}/{WORKBOOK}",
        f"https://{DIR}/{WORKBOOK}",  # a pasted URL, as a url: concept carries
        f"web://https://{DIR}/{WORKBOOK}",
    ],
)
def test_every_spelling_names_the_same_path(given):
    assert WebFileSystem._strip_protocol(given) == f"{DIR}/{WORKBOOK}"


def test_plain_http_is_refused():
    with pytest.raises(NotSupportedError, match="https only"):
        WebFileSystem._strip_protocol("http://example.org/a.csv")


def test_the_host_is_the_container(fs):
    assert fs._split(f"{DIR}/{WORKBOOK}") == (HOST, f"content/supp/{WORKBOOK}")


def test_no_listable_root(fs):
    with pytest.raises(NotSupportedError):
        fs.ls("")


# ------------------------------------------------------------------- metadata


def test_info_is_one_head_and_claims_no_checksum(fs, server):
    info = fs.info(f"web://{DIR}/{WORKBOOK}")
    body = BODIES[f"/content/supp/{WORKBOOK}"]
    assert info["type"] == "file"
    assert info["size"] == len(body)
    assert info["md5"] is None
    assert "no content checksum" in info["checksum_source"]
    assert info["url"] == f"https://{DIR}/{WORKBOOK}"
    assert [m for m, _, _ in server.requests_made] == ["HEAD"]
    # Remembered: asking again costs nothing.
    fs.info(f"{DIR}/{WORKBOOK}")
    assert len(server.requests_made) == 1


def test_sends_the_identifying_browser_like_user_agent(fs, server):
    fs.info(f"{DIR}/{WORKBOOK}")
    _, _, headers = server.requests_made[0]
    assert headers["User-Agent"] == USER_AGENT
    assert "archivefs" in USER_AGENT and USER_AGENT.startswith("Mozilla/5.0")


def test_an_etag_is_a_version_never_a_checksum(fs, server):
    server.extra_headers[f"/content/supp/{WORKBOOK}"] = {"ETag": '"be32-61f4667b93dfd"'}
    info = fs.info(f"{DIR}/{WORKBOOK}")
    assert info["md5"] is None
    assert info["archive_version"] == '"be32-61f4667b93dfd"'
    assert fs.checksum(f"{DIR}/{WORKBOOK}") is None


def test_a_published_content_digest_is_used(fs, server):
    body = BODIES[f"/content/supp/{WORKBOOK}"]
    b64 = base64.b64encode(hashlib.md5(body).digest()).decode()  # noqa: S324
    server.extra_headers[f"/content/supp/{WORKBOOK}"] = {
        "x-goog-hash": f"crc32c=AAAAAA==, md5={b64}"
    }
    assert fs.info(f"{DIR}/{WORKBOOK}")["md5"] == md5(body)


@pytest.mark.parametrize(
    "headers",
    [{"Content-MD5": "not base64!"}, {"Content-MD5": "AAAA"}, {"x-goog-hash": "crc32c=AAAAAA=="}],
)
def test_malformed_digest_headers_are_ignored(headers):
    assert _md5_from_headers(headers) is None


def test_missing_file_is_absent_not_an_error(fs):
    assert not fs.exists(f"{DIR}/no-such.xlsx")
    with pytest.raises(ArchiveNotFoundError):
        fs.info(f"{DIR}/no-such.xlsx")


def test_a_bot_wall_is_explained_without_blaming_a_credential(fs, server):
    server.forbidden.add(f"/content/supp/{WORKBOOK}")
    with pytest.raises(AuthenticationError) as exc:
        fs.info(f"{DIR}/{WORKBOOK}")
    message = str(exc.value)
    assert "anonymous" in message and "private browser window" in message


def test_size_falls_back_to_a_one_byte_range(fs, server):
    server.send_length_on_head = False
    body = BODIES[f"/content/supp/{WORKBOOK}"]
    assert fs.info(f"{DIR}/{WORKBOOK}")["size"] == len(body)
    method, _, headers = server.requests_made[-1]
    assert method == "GET" and headers["Range"] == "bytes=0-0"


def test_ls_shows_what_has_been_looked_at(fs):
    fs.info(f"{DIR}/{WORKBOOK}")
    assert fs.ls(DIR, detail=False) == [f"{DIR}/{WORKBOOK}"]
    assert fs.ls(HOST, detail=False) == [f"{HOST}/content"]


# -------------------------------------------------------------------- transfer


def test_cat_file_reads_the_bytes(fs):
    body = BODIES[f"/content/supp/{WORKBOOK}"]
    assert fs.cat_file(f"{DIR}/{WORKBOOK}") == body
    assert fs.cat_file(f"{DIR}/{WORKBOOK}", start=10, end=30) == body[10:30]


def test_a_server_that_ignores_range_still_reads_correctly(fs, server):
    server.ignore_range = True
    body = BODIES[f"/content/supp/{WORKBOOK}"]
    assert fs.cat_file(f"{DIR}/{WORKBOOK}", start=10, end=30) == body[10:30]
    with fs.open(f"{DIR}/{WORKBOOK}", "rb") as handle:
        assert handle.read() == body


def test_percent_encoding_reaches_the_server_untouched(fs, server):
    path = f"{HOST}/esm/{ENCODED}"
    assert fs.cat_file(path) == BODIES[f"/esm/{ENCODED}"]
    assert all(url == f"https://{path}" for _, url, _ in server.requests_made)


def test_writes_are_refused(fs):
    with pytest.raises(NotSupportedError):
        fs.rm(f"{DIR}/{WORKBOOK}")


# ---------------------------------------------------------- registry & concept


def test_registered_with_a_url_concept(fs):
    assert PROTOCOLS["web"].concept_scheme == "url"
    assert PROTOCOLS["web"].published_checksum is None
    assert PROTOCOLS["web"].immutability_basis is None  # never version_aware
    assert fs.concept(DIR) == f"url:https://{DIR}"
    assert protocol_for_concept(f"url:https://{DIR}") == ("web", f"https://{DIR}")


def test_fsspec_finds_the_protocol(server):
    import fsspec

    web = fsspec.filesystem("web", skip_instance_cache=True)
    assert isinstance(web, WebFileSystem)


# ------------------------------------------------------------ chiro:// pinning

chirograph = pytest.importorskip("chirograph")


@pytest.fixture
def manifest_file(server, tmp_path):
    """A captured manifest over two files of one directory — the shape of a pin."""
    from archivefs.writer import build_manifest

    src = WebFileSystem(skip_instance_cache=True)
    for name in (WORKBOOK, "notes.txt"):
        src.info(f"{DIR}/{name}")  # nothing to list; look at the files first
    manifest = build_manifest(src, DIR, download=True)
    assert manifest.concept == f"url:https://{DIR}"
    assert manifest.record_accession == DIR
    path = tmp_path / "web.chiro.json"
    path.write_text(chirograph.dumps(manifest))
    return path


def test_a_pin_lists_and_reads_through_a_fresh_web_filesystem(manifest_file):
    fs = ChiroFileSystem(skip_instance_cache=True)
    listed = sorted(p.rsplit("/", 1)[-1] for p in fs.find(str(manifest_file)))
    assert listed == ["notes.txt", WORKBOOK]
    info = fs.info(f"{manifest_file}/{WORKBOOK}")
    assert info["md5"] == md5(BODIES[f"/content/supp/{WORKBOOK}"])
    assert fs.cat_file(f"{manifest_file}/{WORKBOOK}") == BODIES[f"/content/supp/{WORKBOOK}"]


def test_a_pin_refuses_bytes_that_changed_upstream(manifest_file, server, tmp_path):
    server.bodies[f"/content/supp/{WORKBOOK}"] = b"a revised workbook, same size?"
    fs = ChiroFileSystem(skip_instance_cache=True)
    with pytest.raises(VerificationError):
        fs.cat_file(f"{manifest_file}/{WORKBOOK}")
    out = tmp_path / "out.xlsx"
    with pytest.raises(VerificationError):
        fs.get_file(f"{manifest_file}/{WORKBOOK}", str(out))
    assert not out.exists()


def test_a_pin_whose_files_all_vanished_lists_them_as_missing(manifest_file, server):
    server.bodies.clear()
    fs = ChiroFileSystem(skip_instance_cache=True)
    listed = fs.find(str(manifest_file), detail=True)
    assert len(listed) == 2
    assert all(info.get("missing_upstream") for info in listed.values())


def test_a_pin_with_one_file_gone_reports_drift(manifest_file, server):
    del server.bodies["/content/supp/notes.txt"]
    fs = ChiroFileSystem(skip_instance_cache=True)
    fs.find(str(manifest_file))
    assert fs.info(f"{manifest_file}/notes.txt").get("missing_upstream") is True
    # The file that is still there reads as normal.
    assert fs.cat_file(f"{manifest_file}/{WORKBOOK}") == BODIES[f"/content/supp/{WORKBOOK}"]
    with pytest.raises(ArchiveNotFoundError):
        fs.cat_file(f"{manifest_file}/notes.txt")


def test_an_announced_directory_is_never_probed(manifest_file, server):
    """A directory URL often answers 200 with an HTML index; probing it would turn
    the pin's own directory into a "file" and hide every pinned member."""
    server.bodies[f"/{DIR.split('/', 1)[1]}"] = b"<html>index</html>"
    del server.bodies["/content/supp/notes.txt"]
    del server.bodies[f"/content/supp/{WORKBOOK}"]
    fs = ChiroFileSystem(skip_instance_cache=True)
    fs.find(str(manifest_file))
    probed = {url for _, url, _ in server.requests_made}
    assert f"https://{DIR}" not in probed


def test_a_single_file_url_pins_under_its_parent(server):
    from archivefs.writer import build_manifest

    src = WebFileSystem(skip_instance_cache=True)
    manifest = build_manifest(src, f"web://{DIR}/{WORKBOOK}", download=True)
    assert manifest.record_accession == DIR
    assert [entry.relpath for entry in manifest.files] == [WORKBOOK]
    assert manifest.files[0].md5 == md5(BODIES[f"/content/supp/{WORKBOOK}"])
