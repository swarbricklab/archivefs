"""BioImage Archive backend tests.

The fake serves the real captured payloads (``tests/data/bia_*``) and reproduces the
four ways the live API misleads a reasonable implementation — a 5-row default page,
an *empty* response to an over-large page request, duplicate paths inside one study,
and ``isDirectory`` as the string ``"false"``. Each of those was measured, not
imagined; see ``docs/decisions.md`` D13.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from tests.fake_http import FakeRequestsModule, FakeResponse, ranged

from archivefs import base as base_mod
from archivefs.backends.bia import PAGE_SIZE, BIAFileSystem, is_true
from archivefs.errors import ArchiveNotFoundError, NotSupportedError

DATA = Path(__file__).parent / "data"
INFO = json.loads((DATA / "bia_S-BIAD599_info.json").read_text())
FILES_PAGE = json.loads((DATA / "bia_S-BIAD599_files.json").read_text())
S3_STUDY_LEVEL = (DATA / "bia_s3_study_level.xml").read_bytes()
S3_IMAGE_LEVEL = (DATA / "bia_s3_image_level.xml").read_bytes()
S3_IMAGE_PREFIX = (
    "S-BIAD599/01776a3a-16a5-4c48-a104-fefcc83cc2ee/"
)
ACC = "S-BIAD599"
FIRE = "https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/599/S-BIAD599/Files"

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


#: the image directories the study-level fixture advertises, plus page two's
STUDY_IMAGE_PREFIXES = [
    prefix.rstrip("/")
    for prefix in __import__("re")
    .findall(r"<Prefix>([^<]+/)</Prefix>", (DATA / "bia_s3_study_level.xml").read_text())
    if prefix.count("/") == 2
] + ["S-BIAD599/ffffffff-0000-0000-0000-000000000000"]


def s3_body(key: str) -> bytes:
    """A body whose length matches the Size the listing fixtures advertise."""
    sizes = {
        f"{S3_IMAGE_PREFIX}01776a3a-16a5-4c48-a104-fefcc83cc2ee"
        "-thumbnail-128-128.png": 12234
    }
    return (key.encode() * 400)[: sizes.get(key, 19)]


#: page two of the truncated study-level listing
STUDY_PAGE_TWO = _listing(
    "S-BIAD599/", prefixes=["S-BIAD599/ffffffff-0000-0000-0000-000000000000/"]
)
EMPTY_LISTING = _listing("S-BIAD599/nothing/")
#: a server that keeps handing back the same continuation token
TRUNCATED_LOOP = _listing(
    "S-BIAD599/",
    contents=[("S-BIAD599/loop.bin", "0" * 32, "1")],
    truncated=True,
    token="always-the-same",
)


class FakeBIA:
    """The BioStudies API and FIRE, including their sharp edges."""

    #: what the live API does when asked for more rows than it will serve
    cap = 1000

    def __init__(self, rows=None, exists=True, repeat_token_forever=False):
        self.rows = list(ROWS if rows is None else rows)
        self.exists = exists
        self.repeat_token_forever = repeat_token_forever
        self.page_requests: list[tuple[int, int]] = []
        self.fetched_urls: list[str] = []
        self.s3_listings: list[tuple[str, str]] = []

    def __call__(self, method: str, url: str, headers: dict, kwargs: dict):
        if "/studies/" in url and url.endswith("/info"):
            return FakeResponse(200, json=INFO if self.exists else {})
        if "/files/" in url and "biostudies/api" in url:
            from urllib.parse import parse_qs, urlparse

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
        if url.startswith(FIRE):
            from urllib.parse import unquote

            self.fetched_urls.append(url)
            path = unquote(url[len(FIRE) + 1 :])
            if path not in BODIES:
                return FakeResponse(404, b"not found")
            return ranged(BODIES[path], headers)
        if "uk1s3.embassy.ebi.ac.uk" in url and "list-type=2" in url:
            return self._s3_listing(url)
        if "uk1s3.embassy.ebi.ac.uk" in url:
            from urllib.parse import unquote, urlparse

            key = unquote(urlparse(url).path).lstrip("/")
            key = key.split("/", 1)[-1] if key.startswith("bia-integrator-data/") else key
            return ranged(s3_body(key), headers)
        return FakeResponse(404, b"not found")

    def _s3_listing(self, url: str):
        """A delimited ListObjectsV2, including the second page of a truncated one."""
        from urllib.parse import parse_qs, unquote, urlparse

        query = parse_qs(urlparse(url).query)
        prefix = unquote(query.get("prefix", [""])[0])
        token = query.get("continuation-token", [""])[0]
        self.s3_listings.append((prefix, token))
        if self.repeat_token_forever:
            return FakeResponse(200, TRUNCATED_LOOP)
        if prefix == "S-BIAD599/":
            # the real study-level listing is truncated; serve page two on the token
            if token:
                return FakeResponse(200, STUDY_PAGE_TWO)
            return FakeResponse(200, S3_STUDY_LEVEL)
        if prefix.startswith(S3_IMAGE_PREFIX):
            return FakeResponse(200, S3_IMAGE_LEVEL)
        if prefix.startswith(f"{ACC}/") and not prefix.endswith("/"):
            # S3 semantics: a prefix that is not a whole key still matches the keys
            # (and common prefixes) beginning with it — which is how info() decides
            # a path is a directory.
            children = [
                child
                for child in STUDY_IMAGE_PREFIXES
                if child.startswith(prefix)
            ]
            if children:
                return FakeResponse(
                    200, _listing(prefix, prefixes=[f"{child}/" for child in children])
                )
        return FakeResponse(200, EMPTY_LISTING)


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
