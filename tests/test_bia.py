"""BioImage Archive backend tests.

The fake serves the real captured payloads (``tests/data/bia_*``) and reproduces the
four ways the live API misleads a reasonable implementation — a 5-row default page,
an *empty* response to an over-large page request, duplicate paths inside one study,
and ``isDirectory`` as the string ``"false"``. Each of those was measured, not
imagined; see ``docs/decisions.md`` D13.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import pytest
from tests.fake_http import FakeRequestsModule, FakeResponse, ranged

from archivefs import base as base_mod
from archivefs.backends.bia import PAGE_SIZE, BIAFileSystem, is_true
from archivefs.errors import (
    ArchiveNotFoundError,
    AuthenticationError,
    CredentialSourceError,
    NotSupportedError,
)

DATA = Path(__file__).parent / "data"
INFO = json.loads((DATA / "bia_S-BIAD599_info.json").read_text())
FILES_PAGE = json.loads((DATA / "bia_S-BIAD599_files.json").read_text())
#: kept as a shape reference for the hand-built listings below
S3_STUDY_LEVEL = (DATA / "bia_s3_study_level.xml").read_bytes()
S3_IMAGE_LEVEL = (DATA / "bia_s3_image_level.xml").read_bytes()
ACC = "S-BIAD599"
FIRE = "https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/599/S-BIAD599/Files"
#: Where BioStudies serves an unreleased study from. Real shape, verified against
#: the live API: the access key is *inside the path*, so the URL is the capability.
PRIVATE_FIRE_ROOT = "https://ftp.ebi.ac.uk/pub/databases/biostudies"
KEY = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

#: A study whose paths exercise the awkward cases: nesting, a '#', a duplicate.
ROWS = [
    {"path": "images/plate1/a.tif", "Name": "a.tif", "Size": 1024, "type": "file",
     "isDirectory": "false", "Characteristics__Organism_": "Danio rerio"},
    {"path": "images/plate1/b.tif", "Name": "b.tif", "Size": 2048, "type": "file",
     "isDirectory": "false"},
    {"path": "images/plate2/a.tif", "Name": "a.tif", "Size": 4096, "type": "file",
     "isDirectory": "false"},
    {"path": "results/#1_raw/summary.csv", "Name": "summary.csv", "Size": 32,
     "type": "file", "isDirectory": "false"},
    # the same path twice, differing only in file_position — seven real ones exist
    # in S-BIAD1
    {"path": "results/#1_raw/summary.csv", "Name": "summary.csv", "Size": 32,
     "type": "file", "isDirectory": "false", "file_position": "99"},
    # a directory row: isDirectory is the *string* "true"
    {"path": "images/plate1", "Name": "plate1", "Size": 0, "type": "directory",
     "isDirectory": "true"},
]
BODIES = {
    "images/plate1/a.tif": b"MM\x00*" + b"A" * 1020,
    "images/plate1/b.tif": b"MM\x00*" + b"B" * 2044,
    "images/plate2/a.tif": b"MM\x00*" + b"C" * 4092,
    "results/#1_raw/summary.csv": b"sample,value\n" + b"x" * 19,
}


def _listing(prefix: str, *, prefixes=(), contents=(), truncated=False, token=""):
    body = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
        f"<Name>bia-integrator-data</Name><Prefix>{prefix}</Prefix>",
        f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>",
    ]
    if token:
        body.append(f"<NextContinuationToken>{token}</NextContinuationToken>")
    for child in prefixes:
        body.append(f"<CommonPrefixes><Prefix>{child}</Prefix></CommonPrefixes>")
    for key, etag, size in contents:
        body.append(
            f"<Contents><Key>{key}</Key>"
            f"<LastModified>2023-10-04T14:51:09.050Z</LastModified>"
            f"<ETag>&quot;{etag}&quot;</ETag><Size>{size}</Size></Contents>"
        )
    body.append("</ListBucketResult>")
    return "".join(body).encode()


#: The derived bucket, as a flat list of objects — which is what S3 is. The fake
#: computes CommonPrefixes/Contents from these the way ListObjectsV2 does, so the
#: tests exercise real prefix semantics (including the partial-name case) rather
#: than a special case per URL.
IMAGE = "01776a3a-16a5-4c48-a104-fefcc83cc2ee"
ZARR = f"{ACC}/{IMAGE}/{IMAGE}.zarr"
S3_OBJECTS: dict[str, int] = {
    f"{ACC}/{IMAGE}/{IMAGE}-thumbnail-128-128.png": 12234,
    f"{ZARR}/.zattrs": 33,
    f"{ZARR}/.zgroup": 23,
    f"{ZARR}/OME/.zattrs": 1782,
    f"{ZARR}/0/.zarray": 327,
    # chunk names are 0,1,10,… — the shape that makes a partial-name prefix
    # look like a directory
    f"{ZARR}/0/0/0/0/1": 4096,
    f"{ZARR}/0/0/0/0/10": 4096,
    f"{ZARR}/0/0/0/0/12": 4096,
    # other images in the study, one object each
    **{
        f"{ACC}/{uuid}/{uuid}-thumbnail-128-128.png": 1000
        for uuid in [f"{i:08x}-0000-0000-0000-000000000000" for i in range(1, 19)]
    },
    # the object that only appears on page two of a truncated study listing
    f"{ACC}/ffffffff-0000-0000-0000-000000000000/thumb.png": 1000,
}
#: how many entries the fake serves per page, to force the truncated path
S3_FAKE_PAGE = 12


def s3_etag(key: str) -> str:
    return hashlib.md5(key.encode()).hexdigest()  # noqa: S324


def s3_body(key: str) -> bytes:
    """A body whose length matches the Size the fake's listing advertises."""
    size = S3_OBJECTS.get(key, 19)
    return (key.encode() * (size // max(len(key), 1) + 1))[:size]


def s3_list(prefix: str, token: str) -> bytes:
    """ListObjectsV2 with delimiter=/, computed from S3_OBJECTS like S3 would."""
    directories: list[str] = []
    contents: list[tuple[str, str, str]] = []
    for key in sorted(S3_OBJECTS):
        if not key.startswith(prefix):
            continue
        rest = key[len(prefix) :]
        head, sep, _ = rest.partition("/")
        if sep:
            child = f"{prefix}{head}/"
            if child not in directories:
                directories.append(child)
        else:
            contents.append((key, s3_etag(key), str(S3_OBJECTS[key])))
    entries = [("dir", d) for d in directories] + [("key", c) for c in contents]
    offset = int(token) if token.isdigit() else 0
    page = entries[offset : offset + S3_FAKE_PAGE]
    truncated = offset + S3_FAKE_PAGE < len(entries)
    return _listing(
        prefix,
        prefixes=[value for kind, value in page if kind == "dir"],
        contents=[value for kind, value in page if kind == "key"],
        truncated=truncated,
        token=str(offset + S3_FAKE_PAGE) if truncated else "",
    )


#: nothing under this prefix at all — a study BIA has not converted
EMPTY_LISTING = _listing(f"{ACC}/nothing/")
#: a server that keeps handing back the same continuation token
TRUNCATED_LOOP = _listing(
    f"{ACC}/",
    contents=[(f"{ACC}/loop.bin", "0" * 32, "1")],
    truncated=True,
    token="always-the-same",
)


class FakeBIA:
    """The BioStudies API and FIRE, including their sharp edges."""

    #: what the live API does when asked for more rows than it will serve
    cap = 1000

    def __init__(
        self,
        rows=None,
        exists=True,
        repeat_token_forever=False,
        key=None,
        released=True,
    ):
        self.rows = list(ROWS if rows is None else rows)
        self.exists = exists
        self.repeat_token_forever = repeat_token_forever
        #: the study's sharing key; when set, an unkeyed API request 403s
        self.key = key
        #: released studies are served from the public path, unreleased ones from
        #: `.private/<key[:2]>/<key[2:]>/…` — which is why the URL is the capability
        self.released = released
        self.page_requests: list[tuple[int, int]] = []
        self.keys_seen: list[str | None] = []
        self.fetched_urls: list[str] = []
        self.s3_listings: list[tuple[str, str]] = []

    def _study_info(self) -> dict:
        info = dict(INFO)
        if self.released:
            return info
        prefix = f".private/{self.key[:2]}/{self.key[2:]}"
        info["relPath"] = f"{prefix}/{INFO['relPath']}"
        info["httpLink"] = f"{PRIVATE_FIRE_ROOT}/{prefix}/S-BIAD/599/S-BIAD599"
        info["isPublic"] = False
        info["seckey"] = self.key
        return info

    def _keyed_ok(self, url: str) -> bool:

        offered = parse_qs(urlparse(url).query).get("key", [None])[0]
        self.keys_seen.append(offered)
        return self.key is None or offered == self.key

    def __call__(self, method: str, url: str, headers: dict, kwargs: dict):
        # `endswith("/info")` here, before `?key=` existed. Matching on the *path*
        # rather than the whole URL, because a query string now follows it.
        if "/studies/" in url and urlparse(url).path.endswith("/info"):
            if not self._keyed_ok(url):
                return FakeResponse(403, json={"errorMessage": "Study not accessible"})
            return FakeResponse(200, json=self._study_info() if self.exists else {})
        if "/files/" in url and "biostudies/api" in url:

            if not self._keyed_ok(url):
                # The live API answers 200 {} here rather than 403, which is why
                # /info is the existence check. Reproduced so the test suite meets
                # the same edge the backend was written for.
                return FakeResponse(200, json={})
            query = parse_qs(urlparse(url).query)
            start = int(query.get("start", ["0"])[0])
            length = int(query.get("length", ["5"])[0])  # the real default is 5
            self.page_requests.append((start, length))
            if length > self.cap:
                # Not an error, not a clamp: an empty listing with a zero total.
                return FakeResponse(
                    200, json={"data": [], "recordsFiltered": 0, "recordsTotal": 0}
                )
            page = self.rows[start : start + length]
            return FakeResponse(
                200,
                json={
                    "data": page,
                    "recordsFiltered": len(self.rows),
                    "recordsTotal": len(self.rows),
                },
            )
        # Either FIRE root: a released study is served from the public path and an
        # unreleased one from `.private/<key>/…`, and both end `/Files/<member>`.
        if "ftp.ebi.ac.uk" in url and "/Files/" in url:
            self.fetched_urls.append(url)
            path = unquote(url.split("/Files/", 1)[1])
            if path not in BODIES:
                return FakeResponse(404, b"not found")
            return ranged(BODIES[path], headers)
        if "uk1s3.embassy.ebi.ac.uk" in url and "list-type=2" in url:
            return self._s3_listing(url)
        if "uk1s3.embassy.ebi.ac.uk" in url:

            key = unquote(urlparse(url).path).lstrip("/")
            key = key.split("/", 1)[-1] if key.startswith("bia-integrator-data/") else key
            return ranged(s3_body(key), headers)
        return FakeResponse(404, b"not found")

    def _s3_listing(self, url: str):

        query = parse_qs(urlparse(url).query)
        prefix = unquote(query.get("prefix", [""])[0])
        token = query.get("continuation-token", [""])[0]
        self.s3_listings.append((prefix, token))
        if self.repeat_token_forever:
            return FakeResponse(200, TRUNCATED_LOOP)
        return FakeResponse(200, s3_list(prefix, token))


@pytest.fixture
def fake_bia(monkeypatch):
    def make(**kwargs):
        server = FakeBIA(**kwargs)
        monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
        return server

    return make


@pytest.fixture
def fs(fake_bia):
    fake_bia()
    return BIAFileSystem(skip_instance_cache=True)


# ------------------------------------------------------------------- paging


def test_the_whole_listing_is_paged_not_just_the_first_page(fs, fake_bia):
    """The API's default page is five rows; the study has more."""
    assert sorted(fs.find(ACC)) == [
        f"{ACC}/images/plate1/a.tif",
        f"{ACC}/images/plate1/b.tif",
        f"{ACC}/images/plate2/a.tif",
        f"{ACC}/results/#1_raw/summary.csv",
    ]


def test_paging_never_asks_for_more_than_the_server_will_serve(fake_bia):
    """Over-large requests come back empty, so a big `length` silently loses files."""
    server = fake_bia()
    fs = BIAFileSystem(skip_instance_cache=True)
    fs.find(ACC)
    assert server.page_requests
    assert all(length <= FakeBIA.cap for _, length in server.page_requests)


def test_paging_advances_by_the_page_size_asked_for(fake_bia):
    server = fake_bia(rows=[
        {"path": f"f{i:04d}.tif", "Size": 1, "type": "file", "isDirectory": "false"}
        for i in range(2500)
    ])
    fs = BIAFileSystem(skip_instance_cache=True)
    assert len(fs.find(ACC)) == 2500
    assert [start for start, _ in server.page_requests] == [0, PAGE_SIZE, 2 * PAGE_SIZE]


def test_duplicate_paths_are_collapsed_not_counted_as_loss(fs, caplog):
    """A study record can list one path twice; seven do in S-BIAD1.

    recordsFiltered counts *rows*, so it legitimately exceeds the file count.
    """
    with caplog.at_level("INFO"):
        found = fs.find(ACC)
    assert len([p for p in found if p.endswith("summary.csv")]) == 1
    assert "duplicate path" in caplog.text


def test_a_genuinely_short_listing_is_refused(fake_bia, monkeypatch):
    """Refusing beats importing a study that is quietly missing files."""
    server = fake_bia()

    def truncated(method, url, headers, kwargs):
        if "/files/" in url and "biostudies/api" in url:
            return FakeResponse(
                200, json={"data": [], "recordsFiltered": 99, "recordsTotal": 99}
            )
        return FakeBIA.__call__(server, method, url, headers, kwargs)

    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(truncated))
    fs = BIAFileSystem(skip_instance_cache=True)
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls(ACC)
    assert "incomplete" in str(exc.value)


# ------------------------------------------------------------- stringly typed


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("True", True), (True, True), ("false", False),
     ("False", False), (False, False), ("", False), (None, False)],
)
def test_is_true_reads_biostudies_booleans(value, expected):
    """bool("false") is True, which would file every entry under "directory"."""
    assert is_true(value) is expected


def test_directory_rows_are_not_listed_as_files(fs):
    assert f"{ACC}/images/plate1" not in fs.find(ACC)
    assert fs.isdir(f"{ACC}/images/plate1")  # it is a directory, from the paths


# ------------------------------------------------------------------ listing


def test_ls_is_children_only(fs):
    assert fs.ls(ACC, detail=False) == [f"{ACC}/images", f"{ACC}/results"]
    assert fs.ls(f"{ACC}/images", detail=False) == [
        f"{ACC}/images/plate1",
        f"{ACC}/images/plate2",
    ]


def test_colliding_basenames_across_plates_both_survive(fs):
    """a.tif exists under two plates — the fsspec walk trap from decisions.md D11."""
    tifs = [p for p in fs.find(ACC) if p.endswith("a.tif")]
    assert len(tifs) == 2
    assert fs.cat_file(tifs[0]) != fs.cat_file(tifs[1])


def test_container_info_carries_the_study_metadata(fs):
    info = fs.info(ACC)
    assert info["type"] == "directory"
    assert info["bia"]["files"] == INFO["files"]
    assert info["bia"]["relpath"] == INFO["relPath"]
    assert info["bia"]["public"] is True


# --------------------------------------------------------------------- URLs


def test_payload_location_comes_from_the_api_not_a_guessed_rule(fs):
    """BIA shards FIRE two different ways; a rule inferred from one study 404s
    on the other."""
    assert fs.info(f"{ACC}/images/plate1/a.tif")["url"] == (
        f"{FIRE}/images/plate1/a.tif"
    )


def test_a_hash_in_a_path_is_quoted(fs, fake_bia):
    """Deposited imaging paths really do contain '#', which would truncate the URL
    at what urlsplit reads as a fragment."""
    info = fs.info(f"{ACC}/results/#1_raw/summary.csv")
    assert "%231_raw" in info["url"]
    assert "#" not in info["url"]
    assert fs.cat_file(f"{ACC}/results/#1_raw/summary.csv") == (
        BODIES["results/#1_raw/summary.csv"]
    )


def test_ranged_reads(fs):
    body = BODIES["images/plate2/a.tif"]
    assert fs.cat_file(f"{ACC}/images/plate2/a.tif", start=0, end=4) == body[:4]
    with fs.open(f"{ACC}/images/plate2/a.tif", "rb", block_size=512) as handle:
        handle.seek(1000)
        assert handle.read(20) == body[1000:1020]


# ------------------------------------------------------------------ metadata


def test_per_file_experimental_metadata_is_kept(fs):
    """No other archive here hands out organism/channel/label per file."""
    attributes = fs.info(f"{ACC}/images/plate1/a.tif")["bia"]["attributes"]
    assert attributes["Characteristics__Organism_"] == "Danio rerio"


def test_submitted_files_admit_they_have_no_checksum(fs):
    info = fs.info(f"{ACC}/images/plate1/a.tif")
    assert info["md5"] is None
    assert "no per-file checksum" in info["checksum_source"]
    assert fs.checksum(f"{ACC}/images/plate1/a.tif") is None


# ------------------------------------------------------------------- errors


def test_an_unknown_study_is_distinguished_from_an_empty_one(fake_bia):
    """The files endpoint answers an unknown accession with an empty list, so the
    check has to be the /info endpoint."""
    fake_bia(exists=False)
    fs = BIAFileSystem(skip_instance_cache=True)
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls(ACC)
    assert "no study" in str(exc.value)


def test_a_non_accession_is_explained(fs):
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls("not-an-accession")
    assert "S-BIAD599" in str(exc.value)


def test_no_listable_root(fs):
    with pytest.raises(NotSupportedError):
        fs.ls("")


def test_unknown_view_is_rejected_at_construction(fake_bia):
    fake_bia()
    with pytest.raises(ValueError, match="submitted"):
        BIAFileSystem(view="hologram", skip_instance_cache=True)


def test_read_only(fs):
    with pytest.raises(NotSupportedError):
        fs.rm(f"{ACC}/images/plate1/a.tif")


# ------------------------------------------------------------- derived view


@pytest.fixture
def derived(fake_bia):
    fake_bia()
    return BIAFileSystem(view="derived", skip_instance_cache=True)


def test_derived_view_lists_one_level_at_a_time(derived, fake_bia):
    """A converted study is one object per zarr chunk — over 60,000 for S-BIAD599,
    and >280s to enumerate. ls() must be one delimited request per directory."""
    images = derived.ls(ACC, detail=False)
    assert images
    assert all(name.startswith(f"{ACC}/") for name in images)
    # directories, not a flattened chunk dump
    assert all(derived.isdir(name) for name in images[:1])


def test_derived_view_follows_a_truncated_listing(derived, fake_bia):
    """The real study-level listing is truncated even with a delimiter."""
    images = derived.ls(ACC, detail=False)
    assert f"{ACC}/ffffffff-0000-0000-0000-000000000000" in images  # page two


def test_derived_view_publishes_real_md5s(derived):
    """The one place in archivefs where a *derived* artefact is checksum-verified
    download-free: a single-part S3 ETag is the object's MD5."""
    entries = derived.ls(f"{ACC}/01776a3a-16a5-4c48-a104-fefcc83cc2ee")
    thumbnails = [e for e in entries if e["type"] == "file"]
    assert thumbnails
    info = thumbnails[0]
    assert info["md5"] and len(info["md5"]) == 32
    assert info["checksum_type"] == "MD5"
    assert "download-free" in info["checksum_source"]
    # and the same answer through info(), which is what reads go through
    assert derived.info(info["name"])["md5"] == info["md5"]


def test_derived_view_descends_into_a_zarr(derived):
    zarr = f"{ACC}/01776a3a-16a5-4c48-a104-fefcc83cc2ee/01776a3a-16a5-4c48-a104-fefcc83cc2ee.zarr"
    assert derived.isdir(zarr)
    assert derived.info(zarr)["type"] == "directory"


def test_derived_view_reads_bytes(derived):
    entries = derived.ls(f"{ACC}/01776a3a-16a5-4c48-a104-fefcc83cc2ee")
    thumbnail = next(e for e in entries if e["type"] == "file")
    data = derived.cat_file(thumbnail["name"])
    assert len(data) == thumbnail["size"]


def test_derived_view_says_when_a_study_has_no_conversions(fake_bia, monkeypatch):
    """S-BIAD1 really has none — the bucket holds only converted studies."""
    server = fake_bia()

    def handler(method, url, headers, kwargs):
        if "uk1s3" in url and "list-type=2" in url:
            return FakeResponse(200, EMPTY_LISTING)
        return FakeBIA.__call__(server, method, url, headers, kwargs)

    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(handler))
    fs = BIAFileSystem(view="derived", skip_instance_cache=True)
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls(ACC)
    assert "no converted data" in str(exc.value)
    assert "default view" in str(exc.value)  # points at the submitted files


def test_a_looping_continuation_token_does_not_hang(fake_bia):
    """A server that repeats its token would otherwise page the same response
    forever, with no error and no output."""
    fake_bia(repeat_token_forever=True)
    fs = BIAFileSystem(view="derived", skip_instance_cache=True)
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls(ACC)
    # and it refuses rather than returning the partial listing it did fetch
    assert "repeated its continuation token" in str(exc.value)
    assert "may be partial" in str(exc.value)


def test_a_multipart_etag_is_not_reported_as_an_md5(fake_bia, monkeypatch):
    """"<hex>-<parts>" is not an MD5 of the object, and saying so would be a lie."""
    server = fake_bia()
    multipart = _listing(
        "S-BIAD599/img/",
        contents=[("S-BIAD599/img/big.zarr.chunk", "d41d8cd98f00b204e9800998ecf8427e-3", "9")],
    )

    def handler(method, url, headers, kwargs):
        if "uk1s3" in url and "list-type=2" in url:
            return FakeResponse(200, multipart)
        return FakeBIA.__call__(server, method, url, headers, kwargs)

    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(handler))
    fs = BIAFileSystem(view="derived", skip_instance_cache=True)
    info = fs.info("S-BIAD599/img/big.zarr.chunk")
    assert info["md5"] is None
    assert info["checksum_type"] is None
    assert "multipart" in info["checksum_source"]


def test_views_are_a_registry(fake_bia):
    assert set(BIAFileSystem.views) == {"submitted", "derived"}


# --------------------------------------------------------------------- network


def requires_network() -> None:
    if os.environ.get("ARCHIVEFS_TEST_BIA", "") != "1":
        pytest.skip("set ARCHIVEFS_TEST_BIA=1 to hit the live BioImage Archive")


@pytest.mark.network
def test_live_listing_is_complete():
    """584 files, and the count must match what the study advertises."""
    requires_network()
    fs = BIAFileSystem(skip_instance_cache=True)
    files = fs.find("S-BIAD599")
    assert len(files) == fs.info("S-BIAD599")["bia"]["files"] == 584


@pytest.mark.network
def test_live_bytes_are_a_tiff():
    requires_network()
    fs = BIAFileSystem(skip_instance_cache=True)
    path = next(p for p in fs.find("S-BIAD599") if p.endswith(".tif"))
    assert fs.cat_file(path, start=0, end=4) in (b"MM\x00*", b"II*\x00")


@pytest.mark.network
def test_live_second_sharding_scheme_and_quoted_paths():
    """S-BIAD1 lives under S-BIAD/S-BIAD0-99/, and its paths contain '#'."""
    requires_network()
    fs = BIAFileSystem(skip_instance_cache=True)
    info = fs.info("S-BIAD1")
    assert info["bia"]["relpath"] == "S-BIAD/S-BIAD0-99/S-BIAD1"
    hashed = [p for p in fs.find("S-BIAD1") if "#" in p]
    assert hashed
    assert fs.cat_file(hashed[0], start=0, end=1)


@pytest.mark.network
def test_live_derived_view_lists_lazily_and_its_etags_are_md5s():
    """Measured: the study's converted tree is >60,000 objects and >280s to
    enumerate; one delimited level is ~11s, and an image directory under a second."""
    requires_network()
    fs = BIAFileSystem(view="derived", skip_instance_cache=True)
    images = fs.ls("S-BIAD599", detail=False)
    assert len(images) > 10
    entries = fs.ls(images[0])
    thumbnail = next(e for e in entries if e["type"] == "file" and e["md5"])
    data = fs.cat_file(thumbnail["name"])
    assert hashlib.md5(data).hexdigest() == thumbnail["md5"]  # noqa: S324
    # and the OME-Zarr tree is walkable one level at a time, which is the point
    zarr = next(e for e in entries if e["type"] == "directory")
    assert any(name.endswith(".zattrs") for name in fs.ls(zarr["name"], detail=False))


@pytest.mark.network
def test_live_a_study_with_no_conversions_says_so():
    """S-BIAD1 has no derived data at all."""
    requires_network()
    fs = BIAFileSystem(view="derived", skip_instance_cache=True)
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls("S-BIAD1")
    assert "no converted data" in str(exc.value)


def test_derived_listings_are_cached(derived, fake_bia):
    """A study-level listing costs ~11s live; find()/walk()/DVC revisit directories."""
    server = fake_bia()
    fs = BIAFileSystem(view="derived", skip_instance_cache=True)
    fs.ls(ACC)
    after_first = len(server.s3_listings)
    fs.ls(ACC)
    fs.ls(ACC)
    assert len(server.s3_listings) == after_first  # served from cache


def test_info_uses_the_parent_listing_when_it_has_one(fake_bia):
    """fsspec walks before it reads, so the parent listing is usually already here."""
    server = fake_bia()
    fs = BIAFileSystem(view="derived", skip_instance_cache=True)
    entries = fs.ls(f"{ACC}/01776a3a-16a5-4c48-a104-fefcc83cc2ee")
    thumbnail = next(e for e in entries if e["type"] == "file")
    before = len(server.s3_listings)
    assert fs.info(thumbnail["name"])["md5"] == thumbnail["md5"]
    assert len(server.s3_listings) == before  # no extra request


def test_invalidate_cache_drops_derived_listings(derived, fake_bia):
    server = fake_bia()
    fs = BIAFileSystem(view="derived", skip_instance_cache=True)
    fs.ls(ACC)
    before = len(server.s3_listings)
    fs.invalidate_cache()
    fs.ls(ACC)
    assert len(server.s3_listings) > before


def test_manifests_do_not_apply_to_the_derived_view(fake_bia):
    """A .chiro describes a deposit; the derived tier is a conversion of one, and
    already publishes md5s. Asking used to surface an internal error."""
    fake_bia()
    fs = BIAFileSystem(view="derived", skip_instance_cache=True)
    assert fs.manifest_path_for(ACC) is None
    assert fs.manifest_for(ACC) is None
    assert fs.drift(ACC) is None


# ---------------------------------------------- the derived view's sharp edges


def test_a_partial_name_prefix_is_not_a_directory(derived):
    """S3 answers a partial prefix with the *siblings* that begin with it.

    So "…/0177" looked populated when only "…/01776a3a-…/" existed, and
    exists/isdir returned confident nonsense that disagreed with ls(). It matters
    for zarr: chunk directories are named 0,1,10,100,…, so a missing chunk "1"
    would resolve as a directory whenever "10" exists — and zarr needs a
    FileNotFoundError there to use the fill value.
    """
    real = f"{ACC}/01776a3a-16a5-4c48-a104-fefcc83cc2ee"
    assert derived.isdir(real)
    partial = f"{ACC}/0177"
    with pytest.raises(ArchiveNotFoundError):
        derived.info(partial)
    assert derived.exists(partial) is False
    assert derived.isdir(partial) is False


def test_ls_of_a_derived_file_returns_that_file(derived):
    """fsspec's contract, and what find()/walk()/du()/DVC's pull all rely on."""
    entries = derived.ls(f"{ACC}/01776a3a-16a5-4c48-a104-fefcc83cc2ee")
    thumbnail = next(e for e in entries if e["type"] == "file")
    path = thumbnail["name"]
    assert derived.ls(path, detail=False) == [path]
    assert derived.find(path) == [path]
    assert derived.du(path) == thumbnail["size"]


def test_ls_of_a_derived_nonexistent_path_still_raises(derived):
    with pytest.raises(ArchiveNotFoundError):
        derived.ls(f"{ACC}/definitely-not-here")


def test_the_cached_listing_is_not_handed_out_to_be_mutated(derived):
    first = derived.ls(ACC)
    first.clear()
    assert derived.ls(ACC), "a caller emptied our cache"


# --------------------------------------------------------------- accessions


@pytest.mark.parametrize(
    "accession",
    ["S-BIAD599", "S-BSST123", "S-EPMC1234", "E-MTAB-1234"],
)
def test_real_biostudies_accession_shapes_are_accepted(fake_bia, accession):
    """E-MTAB-1234 is a real public BioStudies study; refusing it was wrong."""
    fake_bia()
    fs = BIAFileSystem(skip_instance_cache=True)
    fs._check_accession(accession)  # must not raise


@pytest.mark.parametrize("accession", ["not-an-accession", "s-biad599", "", "S-BIA"])
def test_obvious_garbage_is_still_refused(fake_bia, accession):
    """Lower case included: the API 404s on it, so accepting it here would only buy
    a misleading "no such study"."""
    fake_bia()
    fs = BIAFileSystem(skip_instance_cache=True)
    with pytest.raises(ArchiveNotFoundError):
        fs._check_accession(accession)


# ------------------------------------------------------- listing completeness


def test_a_final_zero_total_page_cannot_disarm_the_completeness_check(fake_bia, monkeypatch):
    """`expected` is pinned from the first response; taking it from every page let a
    trailing recordsFiltered:0 turn a short listing into a silent success."""
    server = fake_bia(rows=[
        {"path": f"f{i:04d}.tif", "Size": 1, "type": "file", "isDirectory": "false"}
        for i in range(1500)
    ])
    calls = {"n": 0}
    original = FakeBIA.__call__

    def handler(method, url, headers, kwargs):
        if "/files/" in url and "biostudies/api" in url:
            calls["n"] += 1
            if calls["n"] == 2:  # the second page claims the study is empty
                return FakeResponse(
                    200, json={"data": [], "recordsFiltered": 0, "recordsTotal": 0}
                )
        return original(server, method, url, headers, kwargs)

    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(handler))
    fs = BIAFileSystem(skip_instance_cache=True)
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls(ACC)
    assert "incomplete" in str(exc.value)


def test_rows_with_no_path_are_counted_not_silently_dropped(fake_bia, caplog):
    fake_bia(rows=[
        {"path": "a.tif", "Size": 1, "type": "file", "isDirectory": "false"},
        {"Size": 1, "type": "file", "isDirectory": "false"},  # no path at all
    ])
    fs = BIAFileSystem(skip_instance_cache=True)
    with caplog.at_level("INFO"):
        assert fs.find(ACC) == [f"{ACC}/a.tif"]
    assert "with no path" in caplog.text


def test_a_listing_that_never_ends_is_refused(fake_bia, monkeypatch):
    """A server that ignores `start` would otherwise page forever."""
    server = fake_bia()

    def handler(method, url, headers, kwargs):
        if "/files/" in url and "biostudies/api" in url:
            return FakeResponse(
                200,
                json={
                    "data": [
                        {"path": "same.tif", "Size": 1, "type": "file",
                         "isDirectory": "false"}
                    ],
                    "recordsFiltered": 10**9,
                    "recordsTotal": 10**9,
                },
            )
        return FakeBIA.__call__(server, method, url, headers, kwargs)

    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(handler))
    fs = BIAFileSystem(skip_instance_cache=True)
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls(ACC)
    assert "did not terminate" in str(exc.value)


@pytest.mark.network
def test_live_an_arrayexpress_style_accession_works():
    """E-MTAB-1234 is a real public BioStudies study, and a third sharding scheme
    (E-MTAB-/234/E-MTAB-1234). The accession pattern used to refuse it outright."""
    requires_network()
    fs = BIAFileSystem(skip_instance_cache=True)
    info = fs.info("E-MTAB-1234")
    assert info["bia"]["files"] == 8
    assert info["bia"]["relpath"] == "E-MTAB-/234/E-MTAB-1234"
    assert len(fs.find("E-MTAB-1234")) == 8


@pytest.mark.network
def test_live_a_partial_prefix_is_not_a_directory():
    """S3 answers a partial-name prefix with siblings; treating that as a directory
    made exists/isdir confidently wrong (and breaks zarr's fill-value path)."""
    requires_network()
    fs = BIAFileSystem(view="derived", skip_instance_cache=True)
    image = fs.ls("S-BIAD599", detail=False)[0]
    assert fs.isdir(image)
    partial = image[: len("S-BIAD599/") + 4]
    assert fs.exists(partial) is False
    assert fs.isdir(partial) is False


@pytest.mark.network
def test_live_ls_of_a_derived_file_returns_it():
    requires_network()
    fs = BIAFileSystem(view="derived", skip_instance_cache=True)
    image = fs.ls("S-BIAD599", detail=False)[0]
    entry = next(e for e in fs.ls(image) if e["type"] == "file")
    assert fs.ls(entry["name"], detail=False) == [entry["name"]]
    assert fs.find(entry["name"]) == [entry["name"]]


# ------------------------------------------- an embargoed study is not an auth failure


def test_a_403_does_not_blame_credentials_bia_has_none(monkeypatch):
    """Measured against the live archive: an unreleased study 403s.

    BioStudies answers `403 {"errorMessage":"Study not accessible"}` for a record
    that is embargoed or not yet released. archivefs used to render that as
    "BioImage Archive rejected the credential … check the credentials for BioImage
    Archive", which is unactionable advice for an archive that has no credential
    mechanism at all — it sends the user looking for a login that does not exist.
    """

    def server(method, url, headers, kwargs):
        return FakeResponse(403, json={"errorMessage": "Study not accessible"})

    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
    fs = BIAFileSystem(skip_instance_cache=True)
    with pytest.raises(AuthenticationError) as exc:
        fs.ls("S-BIAD3080")
    message = str(exc.value)
    assert "refused access" in message
    assert "rejected the credential" not in message
    assert "check the credentials" not in message
    # and it names what is actually likely, plus where to look
    assert "embargoed" in message
    assert "landing page" in message
    assert exc.value.used_credentials is False
    assert exc.value.source is None
    # It must not claim the *archive* needs no credentials. BioStudies lets a
    # submitter read their own unreleased study, and archivefs can now carry that
    # access — so the message that used to end "authenticated reads are not
    # implemented" has to end with how to supply the key instead.
    assert "needs no credentials" not in message
    assert "anonymously" in message
    assert "not implemented" not in message
    assert "BIA_KEY" in message
    assert "S-BIAD3080" in message  # names the study, not a placeholder


def test_a_refused_key_is_not_reported_as_an_embargo(monkeypatch, tmp_path):
    """Two different 403s, and conflating them sends people the wrong way.

    With no key, "check the credentials" is unactionable — there is nothing to
    check. With one, the embargo explanation is actively wrong: access *was*
    attempted and refused, and the likely causes are a revoked link or a key filed
    under the wrong accession.
    """

    def server(method, url, headers, kwargs):
        return FakeResponse(403, json={"errorMessage": "Study not accessible"})

    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
    monkeypatch.setenv("BIA_KEY", "aaaaaaaa-0000-0000-0000-000000000000")
    fs = BIAFileSystem(skip_instance_cache=True)
    with pytest.raises(AuthenticationError) as exc:
        fs.ls("S-BIAD3080")
    message = str(exc.value)
    assert "was refused" in message
    assert "S-BIAD3080" in message
    assert "embargoed" not in message
    # and never the key itself, in any message
    assert "aaaaaaaa" not in message


# ------------------------------------ pre-publication studies and their keys


@pytest.fixture
def private(fake_bia, monkeypatch, tmp_path):
    """An unreleased study, plus a way to file its key like a real user would."""
    server = fake_bia(key=KEY, released=False)
    monkeypatch.delenv("BIA_KEY", raising=False)

    def key_file(payload: dict) -> str:
        path = tmp_path / "bia.json"
        path.write_text(json.dumps(payload))
        path.chmod(0o600)
        return str(path)

    server.key_file = key_file  # type: ignore[attr-defined]
    return server


def keyed_fs(path: str | None = None, **options):
    """A filesystem whose only credential file is `path`, if given.

    `Credential` is frozen, so this replaces the declaration rather than mutating
    it — which also keeps one test's key file from leaking into the next.
    """
    import dataclasses

    fs = BIAFileSystem(skip_instance_cache=True, **options)
    if path is not None:
        declared = fs.creds.credentials["key"]
        fs.creds.credentials["key"] = dataclasses.replace(declared, files=(path,))
        fs.creds._cache.clear()
    return fs


def test_an_unreleased_study_explains_itself_rather_than_404ing(private):
    """403, not 404 — the study exists, you just cannot see it.

    That distinction is why the backend checks `/studies/{acc}/info` for existence
    and not `/files/{acc}`: the files endpoint answers an unkeyed request with
    `200 {}`, so a private study and a missing one look identical there.
    """
    fs = keyed_fs()
    with pytest.raises(AuthenticationError) as exc:
        fs.ls(ACC)
    assert "BIA_KEY" in str(exc.value)
    assert ACC in str(exc.value)


def test_the_key_from_the_environment_unlocks_the_study(private, monkeypatch):
    monkeypatch.setenv("BIA_KEY", KEY)
    fs = keyed_fs()
    assert fs.ls(ACC, detail=False)
    assert KEY in private.keys_seen, "the key never reached the API"


def test_a_key_file_is_scoped_per_study(private):
    """One key opens one study, which is the shape of the thing.

    EGA hands out one username and password for every dataset you are authorised
    for; BioStudies hands out a reviewer link per deposit. A single value stops
    working the moment you have two private studies, so the file is a mapping.
    """
    path = private.key_file({"keys": {ACC: KEY, "S-BIAD0000": "wrong"}})
    assert keyed_fs(path).ls(ACC, detail=False)


def test_a_key_filed_under_another_study_does_not_unlock_this_one(private):
    path = private.key_file({"keys": {"S-BIAD0000": KEY}})
    with pytest.raises(AuthenticationError):
        keyed_fs(path).ls(ACC)


def test_a_flat_key_in_the_file_is_a_catch_all(private):
    """The common case is exactly one private study; it should not need a mapping."""
    path = private.key_file({"key": KEY})
    assert keyed_fs(path).ls(ACC, detail=False)


def test_a_scoped_key_beats_the_catch_all(private):
    path = private.key_file({"key": "wrong", "keys": {ACC: KEY}})
    assert keyed_fs(path).ls(ACC, detail=False)


def test_a_key_file_anyone_can_read_is_refused(private, tmp_path):
    """Same rule as every other credential file, for the same reason."""
    path = tmp_path / "loose.json"
    path.write_text(json.dumps({"key": KEY}))
    path.chmod(0o644)
    with pytest.raises(CredentialSourceError) as exc:
        keyed_fs(str(path)).ls(ACC)
    assert "readable by others" in str(exc.value)


def test_the_key_cannot_be_passed_as_an_option(private):
    """The standing rule: `-o key=…` would reach a .dvc file as deps[].fs_config."""
    with pytest.raises(CredentialSourceError) as exc:
        BIAFileSystem(key=KEY, skip_instance_cache=True)
    assert "cannot be passed as an option" in str(exc.value)


def test_key_is_not_a_dvc_settable_option():
    """Belt and braces: absent from the profile, so DVC's own validator refuses it.

    `--fs-config key=…` fails inside DVC's config schema before archivefs is
    reached, which is the half of the guard that works even if the check above is
    ever bypassed.
    """
    from archivefs.registry import PROTOCOLS

    assert "key" not in PROTOCOLS["bia"].config


# ------------------------------------------- the key must not leak, anywhere


def test_the_private_path_is_masked_in_provenance(private, monkeypatch):
    """`archivefs stat` prints this, and stdout ends up in logs too."""
    monkeypatch.setenv("BIA_KEY", KEY)
    info = keyed_fs().info(ACC)
    relpath = info["bia"]["relpath"]
    assert relpath is not None
    assert ".private/<key>" in relpath
    assert KEY not in relpath
    assert KEY[:8] not in relpath


def test_the_key_never_appears_in_an_error_message(private, monkeypatch):
    """Both shapes it takes in a URL, not just the obvious one.

    The first version of `_redact` masked the `.private/` path and missed the
    `?key=` query parameter — which archivefs itself adds — so the error handler
    written to stop the leak was leaking through it. Hence both directions here.
    """
    monkeypatch.setenv("BIA_KEY", KEY)
    fs = keyed_fs()
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.info(f"{ACC}/does/not/exist.tif")
    assert KEY not in str(exc.value)

    private.key = "a-different-key"  # now our key is refused
    fs = keyed_fs()
    with pytest.raises(AuthenticationError) as exc2:
        fs.ls(ACC)
    assert KEY not in str(exc2.value)


def test_reading_bytes_needs_no_key_because_the_path_carries_it(private, monkeypatch):
    """Verified against the live archive: a ranged GET on the private path is 206
    with no key parameter. That is what makes the URL a capability rather than an
    address, and it is why every message has to be redacted."""
    monkeypatch.setenv("BIA_KEY", KEY)
    fs = keyed_fs()
    body = fs.cat_file(f"{ACC}/images/plate1/a.tif")
    assert body == BODIES["images/plate1/a.tif"]
    assert all("key=" not in url for url in private.fetched_urls)
    assert any(".private/" in url for url in private.fetched_urls)


# --------------------------- the point of all of it: an invariant .dvc file


#: The only fields DVC keeps out of an `info()` dict — `dvc_data.hashfile.meta`'s
#: `Meta.from_info`. Everything else archivefs reports, including the whole `bia`
#: provenance block, is dropped before anything is written.
DVC_RECORDS = ("type", "size", "md5", "etag", "checksum", "version_id", "mtime")


def test_the_recorded_fields_are_identical_before_and_after_publication(fake_bia,
                                                                       monkeypatch):
    """The requirement: one `.dvc` file, valid on both sides of the release date.

    A reviewer with a key and a stranger after publication must be able to use the
    *same* file. That holds only if nothing publication-dependent is recorded — and
    plenty is publication-dependent: `relPath` gains a `.private/<key>` prefix,
    `httpLink` changes host and path, `isPublic` flips. All of those are resolved at
    read time from `/info` and none of them is in the recorded set.
    """
    monkeypatch.setenv("BIA_KEY", KEY)
    path = f"{ACC}/images/plate1/a.tif"

    fake_bia(key=KEY, released=False)
    before = BIAFileSystem(skip_instance_cache=True).info(path)
    fake_bia(released=True)
    after = BIAFileSystem(skip_instance_cache=True).info(path)

    assert before["name"] == after["name"]
    assert {k: before.get(k) for k in DVC_RECORDS} == {
        k: after.get(k) for k in DVC_RECORDS
    }
    # and the things that *do* differ are confined to provenance DVC discards
    fake_bia(key=KEY, released=False)
    private_study = BIAFileSystem(skip_instance_cache=True).info(ACC)
    fake_bia(released=True)
    public_study = BIAFileSystem(skip_instance_cache=True).info(ACC)
    assert private_study["bia"]["public"] != public_study["bia"]["public"]
    assert private_study["bia"]["relpath"] != public_study["bia"]["relpath"]


def test_dvc_itself_agrees_that_nothing_publication_dependent_is_recorded(fake_bia,
                                                                          monkeypatch):
    """The same claim, but asked of DVC rather than of a list maintained here.

    `DVC_RECORDS` above could drift from what `Meta.from_info` actually keeps. This
    puts both infos through the real thing, so the invariance claim rests on DVC's
    code and not on our reading of it.
    """
    pytest.importorskip("dvc_data")
    from dvc_data.hashfile.meta import Meta

    monkeypatch.setenv("BIA_KEY", KEY)
    path = f"{ACC}/images/plate1/a.tif"

    fake_bia(key=KEY, released=False)
    before = Meta.from_info(BIAFileSystem(skip_instance_cache=True).info(path), "bia")
    fake_bia(released=True)
    after = Meta.from_info(BIAFileSystem(skip_instance_cache=True).info(path), "bia")
    assert before == after


@pytest.mark.network
def test_live_private_study_needs_and_accepts_a_sharing_key():
    """Against the real service, with a key supplied out of band.

    Gated on `$BIA_KEY` rather than shipped with one: a sharing key is a live
    capability and has no business in a repository. Set it to a key for
    `$ARCHIVEFS_TEST_BIA_STUDY` to run this.
    """
    accession = os.environ.get("ARCHIVEFS_TEST_BIA_STUDY")
    if not (accession and os.environ.get("BIA_KEY")):
        pytest.skip(
            "set ARCHIVEFS_TEST_BIA_STUDY=S-BIADnnnn and BIA_KEY=<uuid> to run"
        )
    fs = BIAFileSystem(skip_instance_cache=True)
    names = fs.ls(accession, detail=False)
    assert names

    # and without the key, the same study refuses with something actionable
    bare = BIAFileSystem(skip_instance_cache=True)
    bare.creds.credentials["key"] = dataclasses.replace(
        bare.creds.credentials["key"], env=(), files=()
    )
    bare.creds._cache.clear()
    with pytest.raises(AuthenticationError) as exc:
        bare.ls(accession)
    assert "BIA_KEY" in str(exc.value)
    assert os.environ["BIA_KEY"] not in str(exc.value)
