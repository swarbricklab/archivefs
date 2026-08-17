"""GEO backend tests, using real captured ``filelist.txt`` and directory index.

Both fixtures come from GSE176078 (Wu et al. 2021) — the lab's own deposit, and
the exact round-trip case ``docs/design.md`` cites for "deposit matrices to GEO,
later pull them back".
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.fake_http import FakeRequestsModule, FakeResponse, ranged

from archivefs import base as base_mod
from archivefs.backends.geo import (
    GEOFileSystem,
    accession_dir,
    parse_filelist,
)
from archivefs.errors import ArchiveNotFoundError, NotSupportedError

DATA = Path(__file__).parent / "data"
FILELIST = (DATA / "geo_GSE176078_filelist.txt").read_text()
INDEX_HTML = (DATA / "geo_GSE176078_index.html").read_text()
GSE = "GSE176078"
COUNTS = "GSE176078_Wu_etal_2021_bulkRNAseq_raw_counts.txt.gz"
BODIES = {
    COUNTS: b"counts" * 5000,
    "GSE176078_RAW.tar": b"tarball" * 1000,
    "GSE176078_Wu_etal_2021_BRCA_scRNASeq.tar.gz": b"scrna" * 100,
}


class FakeGEO:
    def __init__(self, *, serve_filelist: bool = True, record_exists: bool = True):
        self.serve_filelist = serve_filelist
        self.record_exists = record_exists
        self.requests_made: list[str] = []

    def __call__(self, method: str, url: str, headers: dict, kwargs: dict):
        self.requests_made.append(f"{method} {url}")
        suppl = "/geo/series/GSE176nnn/GSE176078/suppl/"
        if url.endswith(f"{suppl}filelist.txt"):
            if not self.serve_filelist:
                return FakeResponse(404, b"not found")
            return FakeResponse(200, FILELIST.encode())
        if url.endswith(suppl):
            if not self.record_exists:
                return FakeResponse(404, b"not found")
            return FakeResponse(200, INDEX_HTML.encode())
        if url.endswith("/geo/series/GSE176nnn/GSE176078/"):
            return FakeResponse(200 if self.record_exists else 404, b"")
        for name, body in BODIES.items():
            if url.endswith(name):
                if method == "HEAD":
                    return FakeResponse(
                        200, b"", {"Content-Length": str(len(body))}
                    )
                return ranged(body, headers)
        return FakeResponse(404, b"not found")


@pytest.fixture
def fake_geo(monkeypatch):
    server = FakeGEO()
    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
    return server


@pytest.fixture
def fs(fake_geo):
    return GEOFileSystem(skip_instance_cache=True)


# ----------------------------------------------------------------- addressing


@pytest.mark.parametrize(
    ("accession", "expected"),
    [
        ("GSE176078", "series/GSE176nnn/GSE176078"),
        ("GSE1000", "series/GSE1nnn/GSE1000"),
        ("GSM5354513", "samples/GSM5354nnn/GSM5354513"),
        ("GPL570", "platforms/GPLnnn/GPL570"),  # fewer than four digits
        ("GPL16791", "platforms/GPL16nnn/GPL16791"),
        ("GDS1234", "datasets/GDS1nnn/GDS1234"),
    ],
)
def test_accession_bucketing(accession, expected):
    assert accession_dir(accession) == expected


def test_non_accession_is_explained():
    with pytest.raises(ArchiveNotFoundError) as exc:
        accession_dir("not-an-accession")
    assert "GSE176078" in str(exc.value)


def test_no_listable_root(fs):
    with pytest.raises(NotSupportedError):
        fs.ls("")


# -------------------------------------------------------------------- listing


def test_ls_is_the_directory_index_enriched_by_filelist(fs, fake_geo):
    """The filelist is *not* authoritative — for this real record it omits two
    top-level files that the directory index lists. Trusting it would drop them."""
    names = fs.ls(GSE, detail=False)
    assert f"{GSE}/{COUNTS}" in names  # absent from filelist.txt
    assert f"{GSE}/GSE176078_RAW.tar" in names  # present in both
    assert f"{GSE}/filelist.txt" not in names  # not data
    assert len(fake_geo.requests_made) == 2  # index, then filelist
    # exact size for the file the filelist covers...
    assert fs.info(f"{GSE}/GSE176078_RAW.tar")["size"] == 558264320
    # ...and a HEAD, not an invented number, for the one it does not
    assert fs.ls(GSE)[0].get("size") in (None, 558264320)
    assert fs.info(f"{GSE}/{COUNTS}")["size"] == len(BODIES[COUNTS])


def test_archive_members_are_metadata_not_directory_entries(fs):
    """filelist.txt tells us what is inside the tar; we do not pretend it is a dir."""
    info = fs.info(f"{GSE}/GSE176078_RAW.tar")
    assert info["type"] == "file"
    contains = info["geo"]["contains"]
    assert contains and any(m.startswith("GSM5354513") for m in contains)
    # members of the archive are not top-level entries
    assert f"{GSE}/GSM5354513_CID3586.tar.gz" not in fs.ls(GSE, detail=False)


def test_ls_works_with_no_filelist_at_all(fake_geo):
    fake_geo.serve_filelist = False
    fs = GEOFileSystem(skip_instance_cache=True)
    names = fs.ls(GSE, detail=False)
    assert f"{GSE}/{COUNTS}" in names
    # the parent-directory link and the HHS footer link are not files
    assert not [n for n in names if "hhs.gov" in n or n.endswith("/GSE176078")]


def test_html_index_sizes_are_not_trusted(fake_geo):
    """The index shows "532M"; a rounded size would corrupt a ranged read."""
    fs = GEOFileSystem(skip_instance_cache=True)
    listed = {e["name"]: e for e in fs.ls(GSE)}
    assert listed[f"{GSE}/{COUNTS}"]["size"] is None  # never the rounded "5.3M"
    # resolved exactly, on demand, with a HEAD
    assert fs.info(f"{GSE}/{COUNTS}")["size"] == len(BODIES[COUNTS])


def test_unknown_accession_says_so(fake_geo):
    fake_geo.serve_filelist = False
    fake_geo.record_exists = False
    fs = GEOFileSystem(skip_instance_cache=True)
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls(GSE)
    assert "has no record" in str(exc.value)


def test_glob(fs):
    assert fs.glob(f"{GSE}/*.txt.gz") == [f"{GSE}/{COUNTS}"]


# ------------------------------------------------------------------------ info


def test_info_is_honest_about_missing_checksums(fs):
    info = fs.info(f"{GSE}/GSE176078_RAW.tar")
    assert info["size"] == 558264320
    assert info["md5"] is None
    assert "GEO does not publish per-file checksums" in info["checksum_source"]
    # a weak-but-real change-detection token
    assert info["archive_version"]


def test_checksum_returns_none_rather_than_a_fake(fs):
    assert fs.checksum(f"{GSE}/{COUNTS}") is None
    # ukey still gives something stable to compare against
    assert fs.ukey(f"{GSE}/GSE176078_RAW.tar")


def test_exists(fs):
    assert fs.exists(GSE)
    assert fs.exists(f"{GSE}/{COUNTS}")
    assert not fs.exists(f"{GSE}/absent.txt")


# -------------------------------------------------------------------- transfer


def test_cat_file_and_ranges(fs):
    body = BODIES[COUNTS]
    assert fs.cat_file(f"{GSE}/{COUNTS}") == body
    assert fs.cat_file(f"{GSE}/{COUNTS}", start=10, end=20) == body[10:20]
    with fs.open(f"{GSE}/{COUNTS}", "rb", block_size=1024) as fh:
        fh.seek(4321)
        assert fh.read(50) == body[4321:4371]


def test_read_only(fs):
    with pytest.raises(NotSupportedError):
        fs.rm(f"{GSE}/{COUNTS}")


# ------------------------------------------------------------------- SRA seam


def test_raw_view_refuses_with_an_explanation(fake_geo):
    fs = GEOFileSystem(view="raw", skip_instance_cache=True)
    with pytest.raises(NotSupportedError) as exc:
        fs.ls(GSE)
    message = str(exc.value)
    assert "SRA" in message
    assert "ega://" in message  # points at the path that does work


def test_unknown_view_is_rejected_at_construction(fake_geo):
    with pytest.raises(ValueError) as exc:
        GEOFileSystem(view="telepathy", skip_instance_cache=True)
    assert "supplementary" in str(exc.value)


def test_views_are_a_registry_so_a_new_one_slots_in(fake_geo):
    """The seam's whole point: adding a view must not require editing the backend."""
    from archivefs.backends.geo import GEOView

    class FakeView(GEOView):
        name = "fake"

        def list_members(self, accession):
            return {
                f"{accession}/synthetic.txt": {
                    "name": f"{accession}/synthetic.txt",
                    "type": "file",
                    "size": 3,
                    "url": "https://example.invalid/synthetic.txt",
                }
            }

        def data_url(self, info):
            return info["url"]

    GEOFileSystem.views["fake"] = FakeView
    try:
        fs = GEOFileSystem(view="fake", skip_instance_cache=True)
        assert fs.ls(GSE, detail=False) == [f"{GSE}/synthetic.txt"]
    finally:
        del GEOFileSystem.views["fake"]


# ------------------------------------------------------------------- filelist


def test_parse_filelist_shape():
    entries = parse_filelist(FILELIST)
    assert "GSE176078_RAW.tar" in entries
    assert entries["GSE176078_RAW.tar"]["size"] == 558264320
    assert entries["GSE176078_RAW.tar"]["file_type"] == "TAR"
    assert len(entries["GSE176078_RAW.tar"]["contains"]) == 26
    # the real filelist covers only the archive and its members
    assert COUNTS not in entries


def test_parse_filelist_tolerates_junk():
    assert parse_filelist("") == {}
    assert parse_filelist("#header only\n") == {}
    assert parse_filelist("File\tonly-three\tfields\n") == {}


# --------------------------------------------------------------------- network


@pytest.fixture
def live_fs():
    if os.environ.get("ARCHIVEFS_TEST_GEO", "") != "1":
        pytest.skip("set ARCHIVEFS_TEST_GEO=1 to hit ftp.ncbi.nlm.nih.gov")
    return GEOFileSystem(skip_instance_cache=True)


@pytest.mark.network
def test_live_listing(live_fs):
    names = live_fs.ls(GSE, detail=False)
    assert f"{GSE}/{COUNTS}" in names
    assert live_fs.info(f"{GSE}/{COUNTS}")["size"] == 5532343


@pytest.mark.network
def test_live_ranged_read_is_byte_exact(live_fs):
    """GEO supplementary files are byte-exact, which is half of why they are easy."""
    head = live_fs.cat_file(f"{GSE}/{COUNTS}", start=0, end=3)
    assert head == b"\x1f\x8b\x08"  # gzip magic
    middle = live_fs.cat_file(f"{GSE}/{COUNTS}", start=1000, end=1100)
    assert len(middle) == 100


@pytest.mark.network
def test_live_sample_accession(live_fs):
    names = live_fs.ls("GSM5354513", detail=False)
    assert names == ["GSM5354513/GSM5354513_CID3586.tar.gz"]


def test_a_top_level_file_listed_after_an_archive_is_not_swallowed(fake_geo):
    """GEO's filelist attributes every File row to the preceding Archive row.

    The format cannot distinguish a genuine top-level file that happens to be
    listed after an archive block — but the directory index can, so anything the
    index also shows is promoted back out of the archive's contents.
    """
    fs = GEOFileSystem(skip_instance_cache=True)
    counts = fs.info(f"{GSE}/{COUNTS}")
    # the filelist lists COUNTS nowhere, but were it listed inside the tar block it
    # must still come back as a top-level file with its own size
    assert counts["type"] == "file"
    contains = fs.info(f"{GSE}/GSE176078_RAW.tar")["geo"]["contains"]
    assert COUNTS not in (contains or [])


def test_filelist_promotion(fake_geo, monkeypatch):
    listing = (
        "#Archive/File\tName\tTime\tSize\tType\n"
        "Archive\tGSE176078_RAW.tar\t04/10/2022 15:24:46\t558264320\tTAR\n"
        "File\tGSM1_a.tar.gz\t06/02/2021 08:51:11\t19721175\tTAR\n"
        f"File\t{COUNTS}\t04/10/2022 15:24:46\t5532343\tTXT\n"
    )
    original = FakeGEO.__call__

    def handler(method, url, headers, kwargs):
        if url.endswith("suppl/filelist.txt"):
            return FakeResponse(200, listing.encode())
        return original(fake_geo, method, url, headers, kwargs)

    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(handler))
    fs = GEOFileSystem(skip_instance_cache=True)
    # the counts file is in the directory index, so it is top-level with a size...
    assert fs.info(f"{GSE}/{COUNTS}")["size"] == 5532343
    # ...while the genuine archive member (absent from the index) stays inside
    contains = fs.info(f"{GSE}/GSE176078_RAW.tar")["geo"]["contains"]
    assert contains == ["GSM1_a.tar.gz"]


def test_member_names_are_quoted_in_urls(fake_geo, monkeypatch):
    """A "#" in a filename would truncate the URL at what urljoin reads as a
    fragment, quietly fetching the wrong object."""
    index = '<html><a href="odd#name.txt">odd#name.txt</a></html>'
    original = FakeGEO.__call__

    def handler(method, url, headers, kwargs):
        if url.endswith("suppl/filelist.txt"):
            return FakeResponse(404, b"absent")
        if url.endswith("/suppl/"):
            return FakeResponse(200, index.encode())
        return original(fake_geo, method, url, headers, kwargs)

    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(handler))
    fs = GEOFileSystem(skip_instance_cache=True)
    url = fs.ls(GSE)[0]["url"]
    assert url.endswith("odd%23name.txt")
    assert "#" not in url


def test_walk_raises_rather_than_omitting_a_failing_subtree(fake_geo, monkeypatch):
    """fsspec's walk defaults to on_error="omit", which turns a mid-tree failure
    into silently fewer files and exit 0 — the wrong default for a tool whose job
    is a complete, verified copy."""
    fs = GEOFileSystem(skip_instance_cache=True)
    fs.ls(GSE)  # warm the listing

    def explode(*args, **kwargs):
        raise OSError("network went away")

    monkeypatch.setattr(fs, "ls", explode)
    with pytest.raises(OSError, match="network went away"):
        fs.find(GSE)
    # the default is ours, and a caller can still ask for the old behaviour
    assert list(fs.walk(GSE, on_error="omit")) == []
