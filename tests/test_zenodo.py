"""Zenodo backend tests, including ZIP transparency.

The offline fake serves a real ZIP built in-process, which lets us assert the
thing the design doc calls non-negotiable: listing a ZIP's interior must read
only the central directory, not the whole archive.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import zipfile
from pathlib import Path

import pytest
from tests.fake_http import FakeRequestsModule, FakeResponse, ranged

from archivefs import base as base_mod
from archivefs import blockcache
from archivefs.backends.zenodo import ZenodoFileSystem
from archivefs.base import MAX_BYTES_IN_FLIGHT, TARGET_PARALLEL_PIECE
from archivefs.errors import (
    ArchiveNotFoundError,
    AuthenticationError,
    NotSupportedError,
    UnsafeArchiveMemberError,
    VerificationError,
)
from archivefs.zipsafe import ZipBombError, ZipLimits

DATA = Path(__file__).parent / "data"
RECORD = json.loads((DATA / "zenodo_record_1486184.json").read_text())
RECID = "1486184"
CONCEPT_RECID = "1486183"
DOI = "10.5281/zenodo.1486184"
CONCEPT_DOI = "10.5281/zenodo.1486183"


def build_zip() -> bytes:
    """A ZIP big enough that "did you download it all" is a meaningful question."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("counts/matrix.mtx", "%%MatrixMarket\n" + "1 2 3\n" * 20000)
        zf.writestr("counts/barcodes.tsv", "\n".join(f"CELL{i}" for i in range(5000)))
        zf.writestr("README.md", "# a deposit\n")
    return buffer.getvalue()


ZIP_BYTES = build_zip()
CSV_BYTES = b"sample,value\n" + b"".join(f"s{i},{i}\n".encode() for i in range(2000))


def record_json() -> dict:
    """The captured record, re-pointed at our synthetic files."""
    record = json.loads(json.dumps(RECORD))
    record["files"] = [
        {
            "id": "aaa",
            "key": "archive.zip",
            "size": len(ZIP_BYTES),
            "checksum": f"md5:{hashlib.md5(ZIP_BYTES).hexdigest()}",  # noqa: S324
            "links": {
                "self": f"https://zenodo.org/api/records/{RECID}/files/archive.zip/content"
            },
        },
        {
            "id": "bbb",
            "key": "samples.csv",
            "size": len(CSV_BYTES),
            "checksum": f"md5:{hashlib.md5(CSV_BYTES).hexdigest()}",  # noqa: S324
            "links": {
                "self": f"https://zenodo.org/api/records/{RECID}/files/samples.csv/content"
            },
        },
    ]
    return record


class FakeZenodo:
    def __init__(self) -> None:
        self.record = record_json()
        self.bytes_served = 0
        self.restricted = False
        self.bodies = {"archive.zip": ZIP_BYTES, "samples.csv": CSV_BYTES}

    def __call__(self, method: str, url: str, headers: dict, kwargs: dict):
        if self.restricted and "Authorization" not in headers:
            return FakeResponse(403, json={"status": 403, "message": "restricted"})

        if url.endswith(f"/records/{CONCEPT_RECID}"):
            # Zenodo 302s a concept id to the latest version; requests follows.
            return FakeResponse(200, json=self.record)
        if url.endswith(f"/records/{RECID}"):
            return FakeResponse(200, json=self.record)
        if url.endswith(f"/records/{RECID}/versions"):
            return FakeResponse(200, json={"hits": {"hits": [self.record]}})
        if url.endswith(f"/records/{RECID}/versions/latest"):
            return FakeResponse(200, json=self.record)
        if "/files/" in url and url.endswith("/content"):
            key = url.split("/files/")[1][: -len("/content")]
            body = self.bodies[key]
            response = ranged(body, headers)
            self.bytes_served += len(response.content)
            return response
        if url.rstrip("/").endswith("/records"):
            return FakeResponse(200, json={"hits": {"hits": [self.record]}})
        if "/records/" in url and "q=" in url:
            return FakeResponse(200, json={"hits": {"hits": [self.record]}})
        return FakeResponse(404, json={"status": 404, "message": "not found"})


@pytest.fixture
def fake_zenodo(monkeypatch):
    server = FakeZenodo()
    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
    monkeypatch.delenv("ZENODO_TOKEN", raising=False)
    return server


@pytest.fixture
def fs(fake_zenodo):
    return ZenodoFileSystem(skip_instance_cache=True)


# ------------------------------------------------------------------ addressing


def test_ls_record_by_id(fs):
    assert sorted(fs.ls(RECID, detail=False)) == [
        f"{RECID}/archive.zip",
        f"{RECID}/samples.csv",
    ]


def test_ls_record_by_version_doi(fs):
    names = fs.ls(DOI, detail=False)
    assert f"{DOI}/samples.csv" in names


def test_concept_doi_resolves_to_a_version(fs):
    info = fs.info(f"{CONCEPT_DOI}/samples.csv")
    # The listing key stays as addressed, but provenance records the *version*
    # actually resolved, which is what belongs in a .dvc file.
    assert info["zenodo"]["record_id"] == RECID
    assert info["zenodo"]["doi"] == DOI
    assert info["zenodo"]["concept_doi"] == CONCEPT_DOI


def test_doi_split_does_not_eat_the_filename(fs):
    assert ZenodoFileSystem.split_path("10.5281/zenodo.1486184/a/b.csv") == (
        DOI,
        "a/b.csv",
    )
    assert ZenodoFileSystem.split_path("1486184/a/b.csv") == (RECID, "a/b.csv")


def test_bad_spec_is_explained(fs):
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls("not-a-record")
    assert "record id or DOI" in str(exc.value)


def test_no_listable_root(fs):
    with pytest.raises(NotSupportedError):
        fs.ls("")


def test_missing_member_lists_what_is_there(fs):
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.info(f"{RECID}/nope.txt")
    assert "samples.csv" in str(exc.value)


# ------------------------------------------------------------------------ info


def test_info_is_download_free_and_md5_bearing(fs, fake_zenodo):
    info = fs.info(f"{RECID}/samples.csv")
    assert info["size"] == len(CSV_BYTES)
    assert info["md5"] == hashlib.md5(CSV_BYTES).hexdigest()  # noqa: S324
    assert info["archive_version"] == DOI
    assert fake_zenodo.bytes_served == 0


def test_versions_and_latest(fs):
    assert fs.versions(RECID)[0]["doi"] == DOI
    assert fs.latest_version(RECID)["doi"] == DOI


def test_exists(fs):
    assert fs.exists(RECID)
    assert fs.exists(f"{RECID}/samples.csv")
    assert not fs.exists(f"{RECID}/absent")


# -------------------------------------------------------------------- transfer


def test_cat_file_and_md5_agree(fs):
    data = fs.cat_file(f"{RECID}/samples.csv")
    assert data == CSV_BYTES
    assert hashlib.md5(data).hexdigest() == fs.info(f"{RECID}/samples.csv")["md5"]  # noqa: S324


def test_ranged_reads(fs):
    assert fs.cat_file(f"{RECID}/samples.csv", start=10, end=25) == CSV_BYTES[10:25]
    with fs.open(f"{RECID}/samples.csv", "rb", block_size=512) as fh:
        fh.seek(1234)
        assert fh.read(100) == CSV_BYTES[1234:1334]


def test_cat_file_verifies_a_whole_read_but_leaves_a_range_alone(fs, monkeypatch):
    """A whole `cat_file` is checked against the published md5; a range cannot be.

    A whole read has a whole-file md5 to check against and is refused on mismatch; a
    ranged read has no such anchor, so it is served rather than falsely rejected.
    """
    real = fs._fetch_range
    monkeypatch.setattr(
        fs,
        "_fetch_range",
        lambda info, s, e: (lambda b: b[:-1] + bytes([b[-1] ^ 0x01]) if b else b)(
            real(info, s, e)
        ),
    )
    with pytest.raises(VerificationError):
        fs.cat_file(f"{RECID}/samples.csv")
    # a range cannot be verified against a whole-file md5 — served, not refused
    assert len(fs.cat_file(f"{RECID}/samples.csv", start=0, end=5)) == 5


def test_get_file_verifies_the_download_against_the_published_md5(fs, tmp_path):
    """The DVC download seam (measured: `dvc import-url` calls `get_file`) verifies.

    A correct download materialises and passes.
    """
    dest = tmp_path / "samples.csv"
    fs.get_file(f"{RECID}/samples.csv", str(dest))
    assert dest.read_bytes() == CSV_BYTES


def test_get_file_refuses_and_deletes_bytes_that_fail_their_checksum(
    fs, tmp_path, monkeypatch
):
    """DVC fetches with verify=False (D34); the filesystem is what refuses wrong bytes.

    A source that serves different bytes than its own md5 — corruption, or a mutable
    record that drifted — must fail loudly and leave nothing behind for DVC to pick up.
    """
    real = fs._fetch_range
    monkeypatch.setattr(
        fs,
        "_fetch_range",
        lambda info, s, e: (lambda b: b[:-1] + bytes([b[-1] ^ 0x01]) if b else b)(
            real(info, s, e)
        ),
    )
    dest = tmp_path / "samples.csv"
    with pytest.raises(VerificationError):
        fs.get_file(f"{RECID}/samples.csv", str(dest))
    assert not dest.exists(), "unverified bytes were left on disk"


def test_glob(fs):
    assert fs.glob(f"{RECID}/*.csv") == [f"{RECID}/samples.csv"]


# ---------------------------------------------------------------- credentials


def test_restricted_record_without_token_says_what_to_do(fake_zenodo):
    fake_zenodo.restricted = True
    fs = ZenodoFileSystem(skip_instance_cache=True)
    with pytest.raises(AuthenticationError) as exc:
        fs.ls(RECID)
    message = str(exc.value)
    assert "ZENODO_TOKEN" in message
    assert "docs/credentials.md" in message


def test_token_from_env_is_used(fake_zenodo, monkeypatch):
    fake_zenodo.restricted = True
    monkeypatch.setenv("ZENODO_TOKEN", "sekrit")
    fs = ZenodoFileSystem(skip_instance_cache=True)
    assert fs.ls(RECID, detail=False)
    assert fs.token_source == "$ZENODO_TOKEN"


# ----------------------------------------------------------- ZIP transparency


def test_zips_are_opaque_by_default(fs):
    assert fs.info(f"{RECID}/archive.zip")["type"] == "file"
    assert fs.cat_file(f"{RECID}/archive.zip") == ZIP_BYTES


def test_chained_url_is_the_explicit_form(fs):
    assert (
        fs.chained_url(f"{RECID}/archive.zip")
        == f"zip://::zenodo://{RECID}/archive.zip"
    )


@pytest.fixture
def zfs(fake_zenodo):
    return ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)


def test_zip_member_becomes_a_directory(zfs):
    entries = {e["name"]: e for e in zfs.ls(RECID)}
    assert entries[f"{RECID}/archive.zip"]["type"] == "directory"
    assert entries[f"{RECID}/samples.csv"]["type"] == "file"


def test_concurrent_reads_of_one_zip_serialise(zfs):
    """Reads of one ZIP's members take a per-ZIP lock held for the file's lifetime.

    The member view hands out ``zipfile.ZipExtFile``s over one shared file pointer,
    and ``zipfile`` is not thread-safe — so concurrent streaming reads would corrupt
    it. Regression for #40: opening a member takes the lock; a second open of the
    same ZIP blocks until the first is closed.
    """
    zip_path = f"{RECID}/archive.zip"
    lock = zfs._zip_read_lock(zip_path)

    handle = zfs._open(f"{zip_path}/counts/matrix.mtx")
    second_opened = threading.Event()

    def open_other() -> None:
        with zfs._open(f"{zip_path}/README.md"):
            second_opened.set()

    thread = threading.Thread(target=open_other)
    try:
        assert lock.locked()  # held while a member file is open
        thread.start()
        # Blocked: the lock is ours until we close the first handle.
        assert not second_opened.wait(0.3)
    finally:
        handle.close()

    # Released on close, so the waiting open now proceeds.
    assert second_opened.wait(2)
    thread.join()


def test_concurrent_zip_member_reads_return_correct_bytes(zfs):
    """A stress check that the per-ZIP lock keeps concurrent reads correct (#40).

    Reading in small chunks widens the interleaving window, so an unsynchronised
    shared view would corrupt bytes or raise; the lock keeps every read exact.
    """
    from concurrent.futures import ThreadPoolExecutor

    members = [
        p for p in zfs.find(f"{RECID}/archive.zip") if zfs.info(p)["type"] == "file"
    ]
    expected = {p: zfs.cat_file(p) for p in members}  # sequential ground truth

    def read_in_chunks(path: str) -> tuple[str, bytes]:
        out = []
        with zfs.open(path, "rb") as handle:
            while chunk := handle.read(64):
                out.append(chunk)
        return path, b"".join(out)

    work = members * 12  # contend hard on the three members
    with ThreadPoolExecutor(max_workers=12) as pool:
        for path, data in pool.map(read_in_chunks, work):
            assert data == expected[path]


def test_an_inner_zip_stays_a_file(fake_zenodo):
    """Expansion is one level deep: outer ZIPs open, ZIPs *inside* them do not.

    Recursing would make listing cost unbounded ranged reads (every nested ZIP
    opened to list it) and would bury the point at which the integrity contract
    stops being checkable. One level is where the container md5 still means
    something.
    """
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("secret/deep.txt", b"inside the inner zip")
    outer = io.BytesIO()
    with zipfile.ZipFile(outer, "w") as zf:
        zf.writestr("top.txt", b"plain member")
        zf.writestr("nested/payload.zip", inner.getvalue())
    fake_zenodo.bodies["archive.zip"] = outer.getvalue()
    fake_zenodo.record["files"][0]["size"] = len(outer.getvalue())
    fake_zenodo.record["files"][0]["checksum"] = (
        f"md5:{hashlib.md5(outer.getvalue()).hexdigest()}"  # noqa: S324
    )
    fs = ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)

    assert fs.info(f"{RECID}/archive.zip")["type"] == "directory"
    inner_path = f"{RECID}/archive.zip/nested/payload.zip"
    assert fs.info(inner_path)["type"] == "file"
    assert not fs.isdir(inner_path)
    # its bytes are readable as the ZIP it is, and it has no interior here
    assert fs.cat_file(inner_path) == inner.getvalue()
    assert f"{inner_path}/secret/deep.txt" not in fs.find(f"{RECID}/archive.zip")


def test_listing_a_record_does_not_open_its_zips(zfs, fake_zenodo):
    """Routing `ls` through `_container_as_directory` must stay request-free.

    It reads the member cache `super().ls` just populated; if it ever reached for
    `zip_view` instead, listing a record of ZIPs would open every one of them.
    """
    zfs.ls(RECID)
    assert fake_zenodo.bytes_served == 0


def test_listing_and_info_agree_about_an_expanded_zip(zfs):
    """A listed entry and an ``info()`` of the same path must not disagree.

    `ls` used to flip only ``type``, leaving the container's md5 in the ``md5``
    field of something it had just called a directory — precisely what
    ``_container_as_directory`` exists to prevent. ``archivefs ls -l`` then
    printed a checksum for a directory, and any consumer reading md5 off a
    listing got a hash for bytes it was never going to write.
    """
    listed = {e["name"]: e for e in zfs.ls(RECID)}[f"{RECID}/archive.zip"]
    assert listed == zfs.info(f"{RECID}/archive.zip")
    assert listed["md5"] is None
    assert listed["checksum"] is None
    assert listed["checksum_type"] is None
    # ...and the strong download-free hash is still reachable, just relabelled.
    assert listed["zenodo"]["container_md5"]


def test_zip_interior_listing_is_download_free(zfs, fake_zenodo):
    """Non-negotiable per docs/design.md: list from the central directory only."""
    names = zfs.find(f"{RECID}/archive.zip")
    assert f"{RECID}/archive.zip/counts/matrix.mtx" in names
    assert f"{RECID}/archive.zip/README.md" in names
    served = fake_zenodo.bytes_served
    assert served < len(ZIP_BYTES) / 4, (
        f"listing the ZIP read {served} of {len(ZIP_BYTES)} bytes — the central "
        f"directory should be enough"
    )


def test_zip_member_bytes_are_readable(zfs):
    data = zfs.cat_file(f"{RECID}/archive.zip/README.md")
    assert data == b"# a deposit\n"


def test_zip_member_integrity_contract_is_explicit(zfs):
    info = zfs.info(f"{RECID}/archive.zip/README.md")
    # Asymmetric by construction: no member-level md5, a container md5, and a
    # CRC32 from the central directory. Documented rather than hidden.
    assert info["md5"] is None
    assert info["crc32"] is not None
    assert info["zenodo"]["container_md5"] == hashlib.md5(ZIP_BYTES).hexdigest()  # noqa: S324
    assert info["zenodo"]["container"] == f"{RECID}/archive.zip"
    assert "CRC32" in info["zenodo"]["integrity"]
    assert info["archive_version"] == DOI  # immutability is what makes it sound


def test_zip_interior_sizes_are_real(zfs):
    info = zfs.info(f"{RECID}/archive.zip/counts/barcodes.tsv")
    with zipfile.ZipFile(io.BytesIO(ZIP_BYTES)) as zf:
        assert info["size"] == zf.getinfo("counts/barcodes.tsv").file_size


# ------------------------------------ listing and reading are cached apart (D25)
#
# The fake's ZIP is 12 KB — smaller than one 4 MiB block — so the default blocksize
# hides every question these tests ask. They set `block_size` small enough that
# "which blocks were fetched" is a real distinction.


def test_reading_every_member_costs_about_one_archive(fake_zenodo):
    """The measured bug: a `dvc import-url` moved 19.9x the archive size.

    Each block must be fetched at most once for the life of the filesystem, so
    reading all members — twice, as DVC does — costs one archive, not twenty.
    """
    fs = ZenodoFileSystem(
        extract_zips=True, block_size=4096, skip_instance_cache=True
    )
    members = fs.find(f"{RECID}/archive.zip")
    for _pass in range(2):  # dvc/stage/imports.py:47 — hash, then materialise
        for path in members:
            fs.cat_file(path)
    served = fake_zenodo.bytes_served
    assert served <= len(ZIP_BYTES) * 1.2, (
        f"read {served} bytes to extract a {len(ZIP_BYTES)}-byte archive twice; "
        f"blocks are being refetched"
    )


def test_reading_one_small_member_does_not_fetch_the_container(zipped, fake_zenodo):
    """Why this is a block cache and not an eager fetch of the whole container.

    `archivefs get <1.4GB zip>/<4KB file>` must not pull 1.4 GB. Materialising the
    container on first byte read would have been simpler and would have broken
    this, which is what ruled that design out.
    """
    # Incompressible, so the ZIP really is ~200 KB and the assertion has teeth.
    fs = zipped(
        {"small.txt": b"hi", "big.bin": os.urandom(200_000)}, block_size=4096
    )
    container = len(fake_zenodo.bodies["archive.zip"])
    assert container > 150_000
    fake_zenodo.bytes_served = 0

    assert fs.cat_file(f"{RECID}/archive.zip/small.txt") == b"hi"
    assert fake_zenodo.bytes_served < container / 2, (
        f"fetched {fake_zenodo.bytes_served} of {container} bytes to read 2"
    )


def test_one_member_costs_two_blocks_at_the_default_size(zipped, fake_zenodo):
    """The cost of the split, pinned so it cannot drift unnoticed.

    The reading view re-parses the central directory through its own block-aligned
    cache, and the central directory sits at the *end* of the archive — so that is
    a whole extra block. Live, one 7-byte member out of a 25 MB Zenodo archive went
    from 4.2 MB (one block) to 8.2 MB (two).

    Worth it: the same change took a full `dvc import-url` from 497.8 MB to 25.0 MB.
    But it is a real regression for `archivefs get` of a single file, so it is
    recorded as a number rather than described as negligible.
    """
    fs = zipped(
        {"a.bin": os.urandom(60_000), "b.bin": os.urandom(60_000)}, block_size=4096
    )
    fake_zenodo.bytes_served = 0
    fs.cat_file(f"{RECID}/archive.zip/a.bin")
    one_member = fake_zenodo.bytes_served
    # The member is 60 KB and a block is 4 KB, so this is dominated by the member
    # itself plus the re-read tail — not by the whole 120 KB container.
    assert 60_000 < one_member < 120_000, one_member


def test_listing_does_not_open_the_reading_view(zfs, fake_zenodo):
    """Listing keeps the exact-range cache, which is what makes it ~free.

    A block-aligned cache reads the whole block containing the end-of-central-
    directory record — on a 1.4 GB ZIP that is megabytes to answer `ls`. So the
    two views exist, and `ls`/`info` must never reach for the reading one.
    """
    zfs.find(f"{RECID}/archive.zip")
    assert zfs._member_views == {}
    assert fake_zenodo.bytes_served < len(ZIP_BYTES) / 4

    zfs.cat_file(f"{RECID}/archive.zip/README.md")
    assert list(zfs._member_views) == [f"{RECID}/archive.zip"]


def test_the_reading_view_is_vetted_too(fake_zenodo):
    """A second parse of separately-fetched bytes gets its own check.

    `zip_view` vets at listing time, but bytes reach the workspace through the
    *reading* view, and "a path we would refuse to write must not be offered to
    something that will write it" has to hold on the path that actually writes.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(zipfile.ZipInfo("../escaped.txt"), b"pwned")
    payload = buffer.getvalue()
    fake_zenodo.bodies["archive.zip"] = payload
    fake_zenodo.record["files"][0]["size"] = len(payload)
    fake_zenodo.record["files"][0]["checksum"] = (
        f"md5:{hashlib.md5(payload).hexdigest()}"  # noqa: S324
    )
    fs = ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)
    with pytest.raises(UnsafeArchiveMemberError):
        fs._member_view(f"{RECID}/archive.zip")


def test_invalidating_the_cache_releases_the_reading_view(zfs):
    """The view holds a sparse temp file; a refresh must not leak it."""
    zfs.cat_file(f"{RECID}/archive.zip/README.md")
    assert zfs._member_views
    zfs.invalidate_cache(f"{RECID}/archive.zip")
    assert zfs._member_views == {}
    # and it still works afterwards
    assert zfs.cat_file(f"{RECID}/archive.zip/README.md") == b"# a deposit\n"


# --------------------------------------- parallel ranged reads (D26), and bounds


BIG = {"a.bin": os.urandom(900_000), "b.bin": os.urandom(900_000)}


def test_split_pieces_reassemble_to_the_same_bytes(zipped, fake_zenodo):
    """The only test that really matters here: concurrency must not corrupt.

    A range split across connections and rejoined in the wrong order — or short by
    a piece — produces a member that is confidently wrong rather than an error. So
    the same archive is read with splitting off and on and compared byte for byte.
    """
    serial = zipped(BIG, max_concurrent_reads=1)
    expected = {
        path[len(f"{RECID}/archive.zip") + 1 :]: serial.cat_file(path)
        for path in sorted(serial.find(f"{RECID}/archive.zip"))
    }
    assert expected == BIG

    parallel = zipped(BIG, max_concurrent_reads=8)
    actual = {
        path[len(f"{RECID}/archive.zip") + 1 :]: parallel.cat_file(path)
        for path in sorted(parallel.find(f"{RECID}/archive.zip"))
    }
    assert actual == expected


def test_the_reading_view_really_is_wired_for_parallel_fetches(zipped):
    """A multi_fetcher that never got attached is *correct*, just slow.

    Nothing else here can tell the difference: the fake server answers instantly,
    so the byte-identical test passes either way and only wall clock against a real
    archive would show it. That makes this the test that has to be explicit — it
    caught nothing, but it is the reason a refactor of how the fetcher is attached
    cannot quietly undo the whole point of the change.
    """
    fs = zipped(BIG, max_concurrent_reads=8)
    view = fs._member_view(f"{RECID}/archive.zip")
    assert view.zip.fp.cache.multi_fetcher is not None

    seen: list[list[tuple[int, int]]] = []
    real = fs._fetch_block_ranges
    fs._fetch_block_ranges = lambda info, spans: (
        seen.append(list(spans)) or real(info, spans)
    )
    fs._close_member_view(f"{RECID}/archive.zip")
    fs.cat_file(f"{RECID}/archive.zip/a.bin")
    assert seen, "the multi_fetcher was never called, so nothing ran in parallel"


def test_splitting_actually_happened(zipped):
    """Guard against the reassembly test passing because nothing was ever split."""
    fs = zipped(BIG, max_concurrent_reads=8)
    block = fs.parallel_block_size()
    spans = fs.split_span(0, block)
    assert len(spans) == 8, spans
    assert spans[0][0] == 0
    assert spans[-1][1] == block
    # contiguous, no gaps and no overlap
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))


def test_a_range_too_small_to_split_is_left_whole(fs):
    """Below the piece floor, extra connections cost more round trips than they win."""
    assert fs.split_span(0, 1000) == [(0, 1000)]
    assert fs.split_span(0, 600_000) == [(0, 600_000)]  # under 2x the floor
    # A 4 MiB range is one full round of eight, which is what it always was: the
    # piece cap only binds once a range is bigger than the connections can carry.
    assert len(fs.split_span(0, 4 << 20)) == 8


def test_a_range_is_cut_into_whole_rounds_so_no_connection_idles(fs):
    """Twelve pieces across eight connections is eight busy, then four.

    Measured end to end, that half-empty second round cost more than the smaller
    pieces won — 1.74 MB/s against the 2.08 of not capping the piece size at all. The
    cap has to come with a count that fills every round it starts.
    """
    spans = fs.split_span(0, 23_000_000)
    assert len(spans) % fs.max_concurrent_reads == 0, len(spans)
    assert all(end - start <= TARGET_PARALLEL_PIECE for start, end in spans)


def test_a_long_range_is_cut_by_piece_size_not_by_connection_count(fs):
    """The measured bug: one range per member, cut into `max_concurrent_reads`.

    `MMapCache` consolidates consecutive missing blocks into a single range, so
    reading a 100 MB member asked for 100 MB at once — which the old rule cut into 8
    pieces of 12.5 MB, putting 100 MB in flight against a path that peaks at 16 MiB
    and measured ~2.5 MB/s at 64 MiB. Piece size has to be bounded above, not just
    below.
    """
    spans = fs.split_span(0, 100 << 20)
    assert all(end - start <= TARGET_PARALLEL_PIECE for start, end in spans)
    assert spans[0][0] == 0 and spans[-1][1] == 100 << 20
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))


@pytest.mark.parametrize("conns", [1, 2, 4, 8, 16])
def test_bytes_in_flight_stay_at_or_under_the_measured_peak(fake_zenodo, conns):
    """The cliff is in `connections x piece size`, so neither may be raised alone.

    16 connections of 2 MiB measured *slower* than 8 of 2 MiB (4.08 against 14.03
    MB/s) — raising the bound has to shrink the piece, or it buys a regression.
    """
    fs = ZenodoFileSystem(max_concurrent_reads=conns, skip_instance_cache=True)
    assert conns * fs.parallel_piece_size() <= MAX_BYTES_IN_FLIGHT
    # and a large container's block is exactly one round of those pieces
    block = fs.parallel_block_size(2_000_000_000)
    assert block % fs.parallel_piece_size() == 0
    assert block <= MAX_BYTES_IN_FLIGHT


def test_a_large_container_gets_a_block_that_fills_every_connection(fs):
    """Eight connections carrying 2 MiB each, which is the measured peak."""
    assert fs.parallel_block_size(2_000_000_000) == MAX_BYTES_IN_FLIGHT
    assert len(fs.split_span(0, fs.parallel_block_size(2_000_000_000))) == 8


@pytest.mark.parametrize("container", [12_000, 25_000_000, 50_000_000, 400_000_000])
def test_one_member_never_costs_more_than_a_quarter_of_the_container(fs, container):
    """The block is what a *small* read costs, so it cannot scale freely.

    A flat 16 MiB block would have meant reading one 7-byte member of a 25 MB archive
    fetched two blocks spanning the whole archive — D25's eager container fetch,
    arrived at sideways. Bounding the block by the container bounds that; Zenodo
    publishes the size in the record, so consulting it costs no request.
    """
    block = fs.parallel_block_size(container)
    assert min(2 * block, container) <= max(container / 4, 2 * fs.blocksize)
    # never *smaller* than what shipped, either — this may only trade upwards
    assert block >= fs.blocksize


def test_the_reading_view_sizes_its_block_from_the_container(zfs, monkeypatch):
    """The container's size has to actually reach the block calculation.

    Everything else here tests the arithmetic in isolation; this is the wiring, and
    without it the clamp would be computed from `None` and silently do nothing.
    """
    real = zfs._member

    def big(container, member):
        return {**real(container, member), "size": 2_000_000_000}

    monkeypatch.setattr(zfs, "_member", big)
    handle = zfs._open_container(f"{RECID}/archive.zip", cache_type="mmap")
    assert handle.blocksize == MAX_BYTES_IN_FLIGHT


def test_the_listing_view_keeps_its_small_block(zfs):
    """The two caches are sized for opposite jobs, and only one may grow.

    The listing cache reads *ahead* by a block from the end-of-central-directory
    record, so growing it would put megabytes on the wire to answer `ls` — the ~80x
    regression D25 rejected `blockcache` for.
    """
    listing = zfs._open_container(f"{RECID}/archive.zip")
    assert listing.blocksize == zfs.blocksize


def test_an_explicitly_chosen_block_size_is_left_alone(fake_zenodo):
    """A caller who asks for small blocks is asking for cheap small reads.

    Quadrupling it under them would be the eager container fetch D25 refused, and
    every test in this file that reasons about which blocks were fetched sets this.
    """
    fs = ZenodoFileSystem(
        extract_zips=True, block_size=4096, skip_instance_cache=True
    )
    assert fs.parallel_block_size() == 4096
    handle = fs._open_container(f"{RECID}/archive.zip", cache_type="mmap")
    assert handle.blocksize == 4096


def test_concurrency_is_bounded_no_matter_how_many_callers(zipped):
    """The bound is per filesystem, not per call site.

    `dvc_data` already runs up to `HASH_JOBS` hash threads; if each split a block
    eight ways independently, a free public archive would see 32 sockets from one
    `dvc pull`. The semaphore lives in `_fetch_range` so every path shares it.
    """
    from concurrent.futures import ThreadPoolExecutor

    fs = zipped(BIG, max_concurrent_reads=4)
    peak = {"now": 0, "max": 0}
    guard = threading.Lock()
    real = fs._fetch_range_locked

    def watched(info, start, end):
        with guard:
            peak["now"] += 1
            peak["max"] = max(peak["max"], peak["now"])
        try:
            return real(info, start, end)
        finally:
            with guard:
                peak["now"] -= 1

    fs._fetch_range_locked = watched
    members = sorted(fs.find(f"{RECID}/archive.zip"))
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(fs.cat_file, members * 3))
    assert peak["max"] <= 4, f"{peak['max']} concurrent reads against a bound of 4"


def test_a_failed_piece_fails_the_read_rather_than_leaving_a_hole(zipped, monkeypatch):
    """A dropped piece must raise, not silently short the member.

    `ThreadPoolExecutor.map` re-raises, which is the behaviour relied on; this pins
    it, because the alternative failure is a member that is quietly truncated and
    then hashed and committed as correct.
    """
    fs = zipped(BIG, max_concurrent_reads=8)
    real = fs._fetch_range_locked
    calls = {"n": 0}

    def flaky(info, start, end):
        calls["n"] += 1
        if calls["n"] == 3:
            raise OSError("piece three is gone")
        return real(info, start, end)

    fs._fetch_range_locked = flaky
    with pytest.raises(OSError, match="piece three is gone"):
        fs.cat_file(f"{RECID}/archive.zip/a.bin")


# ------------------------------------------------ plain files (issue #14)

#: Bigger than one 16 MiB round, so a whole-object read has to window rather than
#: put every piece of it on the wire at once.
PLAIN = os.urandom(20 << 20)


@pytest.fixture
def plain(fake_zenodo):
    """Serve one plain (non-ZIP) object big enough to be worth splitting."""

    def make(**options):
        fake_zenodo.bodies["big.bin"] = PLAIN
        fake_zenodo.record["files"].append(
            {
                "id": "ccc",
                "key": "big.bin",
                "size": len(PLAIN),
                "checksum": f"md5:{hashlib.md5(PLAIN).hexdigest()}",  # noqa: S324
                "links": {
                    "self": f"https://zenodo.org/api/records/{RECID}/files/big.bin/content"
                },
            }
        )
        return ZenodoFileSystem(skip_instance_cache=True, **options)

    return make


def rounds_issued(fs) -> list[list[tuple[int, int]]]:
    """Record each parallel round `fs` issues, without changing what it fetches."""
    seen: list[list[tuple[int, int]]] = []
    real = fs.fetch_ranges

    def spy(info, spans):
        seen.append(list(spans))
        return real(info, spans)

    fs.fetch_ranges = spy
    return seen


def test_a_plain_file_is_read_over_many_connections(plain):
    """Issue #14: `fetch_ranges` and `split_span` were reachable from one place.

    Only Zenodo's ZIP member view had a `multi_fetcher`, so a plain object went out
    as a single request — and a single connection sustains ~0.30 MB/s whatever it is
    asked for, against 9.70-14.03 for the same bytes cut into 2 MiB pieces. A 2.5 GB
    `.ome.tif` was hours where it should be minutes.
    """
    fs = plain()
    rounds = rounds_issued(fs)
    assert fs.cat_file(f"{RECID}/big.bin") == PLAIN
    spans = [span for round_ in rounds for span in round_]
    assert len(spans) > 8, f"only {len(spans)} pieces for 20 MiB"
    assert all(end - start <= TARGET_PARALLEL_PIECE for start, end in spans)
    # contiguous and complete: no gap, no overlap, nothing beyond the object
    assert spans[0][0] == 0 and spans[-1][1] == len(PLAIN)
    assert all(a[1] == b[0] for a, b in zip(spans, spans[1:]))


def test_a_whole_object_read_holds_only_one_round_in_flight(plain):
    """The split has to be windowed or it trades slowness for a memory blow-up.

    20 MiB is 10 pieces; a 2.5 GB `.ome.tif` is 1250, all live at once before the
    join, so the peak would be the file twice over. Windowing at one round keeps
    that flat — and one round is `MAX_BYTES_IN_FLIGHT`, the measured peak of the
    throughput curve, so the same bound does both jobs.
    """
    fs = plain()
    rounds = rounds_issued(fs)
    fs.cat_file(f"{RECID}/big.bin")
    assert len(rounds) > 1, "20 MiB went out as one round; nothing was windowed"
    for round_ in rounds:
        in_flight = sum(end - start for start, end in round_)
        assert in_flight <= MAX_BYTES_IN_FLIGHT, in_flight


def test_reading_a_plain_file_through_open_splits_each_block(plain):
    """`fs.open()` is the streaming path — the CLI's `get` and DVC's hashing.

    It never reaches `cat_file`, so splitting there alone would leave every actual
    download exactly as slow as it was.
    """
    fs = plain()
    rounds = rounds_issued(fs)
    with fs.open(f"{RECID}/big.bin", "rb") as handle:
        body = handle.read()
    assert body == PLAIN
    assert sum(len(round_) for round_ in rounds) > 8
    assert all(
        end - start <= TARGET_PARALLEL_PIECE for round_ in rounds for start, end in round_
    )


def test_a_large_plain_file_gets_a_block_that_fills_every_connection(fs, monkeypatch):
    """A block is one round of pieces, so 4 MiB of it was 8 x 512 KiB.

    Measured, that is 5.76 MB/s where 8 x 2 MiB is 9.70-14.03 — the block has to
    follow the connection count, not stay at fsspec's class default.
    """
    real = fs.info
    monkeypatch.setattr(
        fs, "info", lambda path, **kw: {**real(path, **kw), "size": 2_000_000_000}
    )
    with fs.open(f"{RECID}/samples.csv", "rb") as handle:
        assert handle.blocksize == MAX_BYTES_IN_FLIGHT


def test_a_small_plain_file_keeps_a_small_block(plain):
    """The eighth-of-the-object bound is what stops this being an eager fetch.

    Enlarging every block would mean a 100-byte peek at a modest file dragged 16 MiB
    behind it — D25's eager container fetch, arrived at from the other side.
    """
    fs = plain()
    with fs.open(f"{RECID}/samples.csv", "rb") as handle:
        assert handle.blocksize == fs.blocksize


def test_one_connection_is_still_one_request(plain):
    """`max_concurrent_reads=1` is how a caller opts out; it must stay an opt-out.

    Windowing an unsplit read would turn one request into forty small ones, each
    with a ~0.1 s fixed cost, which is a regression handed to the person who asked
    to be gentle.
    """
    fs = plain(max_concurrent_reads=1)
    requests = {"n": 0}
    real = fs._fetch_range_locked

    def counted(info, start, end):
        requests["n"] += 1
        return real(info, start, end)

    fs._fetch_range_locked = counted
    assert fs.cat_file(f"{RECID}/big.bin") == PLAIN
    assert requests["n"] == 1, requests["n"]


def test_a_short_piece_fails_closed_rather_than_sliding_the_file(plain):
    """Assembly places each piece at *its own* offset and checks the length.

    Appending at a running offset instead would absorb a short piece by shifting
    every later byte left — producing a file of the wrong length that still looks
    like data, which then gets hashed and committed.
    """
    fs = plain()
    real = fs.fetch_ranges

    def shorten(info, spans):
        pieces = real(info, spans)
        return [pieces[0][:-16], *pieces[1:]]

    fs.fetch_ranges = shorten
    with pytest.raises(OSError, match="refusing to assemble a short read"):
        fs.cat_file(f"{RECID}/big.bin")


# ------------------------------------------------- the wrapper directory (D24)

ZIP = f"{RECID}/archive.zip"


@pytest.fixture
def zipped(fake_zenodo):
    """Serve a ZIP of exactly these members, and hand back a filesystem over it."""

    def make(members: dict[str, bytes], **options):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, body in members.items():
                zf.writestr(name, body)
        payload = buffer.getvalue()
        fake_zenodo.bodies["archive.zip"] = payload
        fake_zenodo.record["files"][0]["size"] = len(payload)
        fake_zenodo.record["files"][0]["checksum"] = (
            f"md5:{hashlib.md5(payload).hexdigest()}"  # noqa: S324
        )
        return ZenodoFileSystem(
            extract_zips=True, skip_instance_cache=True, **options
        )

    return make


#: The reported case, and the shape ~80% of sampled Zenodo ZIPs have: the
#: depositor zipped a directory, because `unzip` creates no wrapper of its own.
WRAPPED = {
    "annotations/visium/a.csv": b"a\n",
    "annotations/visium_hd/b.csv": b"b\n",
    "annotations/README.md": b"# notes\n",
}


def test_a_wrapper_directory_is_stripped(zipped):
    """`annotations.zip` imported to `annotations/` must not nest twice."""
    fs = zipped(WRAPPED)
    assert sorted(fs.ls(ZIP, detail=False)) == [
        f"{ZIP}/README.md",
        f"{ZIP}/visium",
        f"{ZIP}/visium_hd",
    ]
    assert f"{ZIP}/annotations/visium/a.csv" not in fs.find(ZIP)


def test_stripping_is_symmetric_so_reads_follow_listings(zipped):
    """The one that matters: a strip applied in one direction only reads wrong.

    `_to_interior` is what `cat_file` and `_open` fetch through, so listing a
    tidy tree while fetching un-stripped paths would 404 — or, with an unlucky
    name collision, silently return a different member's bytes.
    """
    fs = zipped(WRAPPED)
    for path in fs.find(ZIP):
        member = path[len(ZIP) + 1 :]
        assert fs.cat_file(path) == WRAPPED[f"annotations/{member}"]
        with fs.open(path, "rb") as handle:
            assert handle.read() == WRAPPED[f"annotations/{member}"]
        assert fs.info(path)["name"] == path


def test_strip_components_zero_presents_the_interior_verbatim(zipped):
    fs = zipped(WRAPPED, strip_components=0)
    assert sorted(fs.ls(ZIP, detail=False)) == [f"{ZIP}/annotations"]
    assert fs.cat_file(f"{ZIP}/annotations/README.md") == b"# notes\n"


def test_several_real_top_level_entries_are_left_alone(zfs):
    """The 20% case. Stripping here would drop a sibling, so it must not fire."""
    assert sorted(zfs.ls(ZIP, detail=False)) == [
        f"{ZIP}/README.md",
        f"{ZIP}/counts",
    ]


def test_a_zip_wrapping_one_file_is_left_alone(zipped):
    """A lone top-level *file* is not a wrapper directory — nothing to strip."""
    fs = zipped({"md.xtc": b"trajectory"})
    assert fs.ls(ZIP, detail=False) == [f"{ZIP}/md.xtc"]
    assert fs.cat_file(f"{ZIP}/md.xtc") == b"trajectory"


def test_macos_resource_forks_do_not_mask_the_wrapper(zipped):
    """30% of sampled ZIPs carry `__MACOSX`, which makes two top-level entries.

    Without cruft filtering the strip would decline on exactly the archives that
    need it, so this is what makes the default worth having rather than a rule
    that fires four times in five.
    """
    fs = zipped({**WRAPPED, "__MACOSX/._annotations": b"\x00rsrc"})
    assert sorted(fs.ls(ZIP, detail=False)) == [
        f"{ZIP}/README.md",
        f"{ZIP}/visium",
        f"{ZIP}/visium_hd",
    ]


def test_cruft_is_hidden_from_listings_but_stays_readable(zipped):
    """Hidden, not deleted — and hiding is what keeps it out of the tracked hash.

    With `version_aware` on, anything `walk` yields lands in the committed
    `files:` list, so a resource fork listed is a resource fork versioned.
    """
    fs = zipped({"a.txt": b"real", "__MACOSX/._a.txt": b"\x00rsrc"})
    assert fs.find(ZIP) == [f"{ZIP}/a.txt"]
    assert fs.cat_file(f"{ZIP}/__MACOSX/._a.txt") == b"\x00rsrc"


def test_asking_to_strip_deeper_than_the_archive_goes_stops_early(zipped):
    """`strip_components` is a maximum, not a promise."""
    fs = zipped(WRAPPED, strip_components=5)
    assert sorted(fs.ls(ZIP, detail=False)) == [
        f"{ZIP}/README.md",
        f"{ZIP}/visium",
        f"{ZIP}/visium_hd",
    ]


def test_nested_wrappers_strip_to_the_requested_depth(zipped):
    members = {"outer/inner/deep.txt": b"payload"}
    assert zipped(members, strip_components=1).ls(ZIP, detail=False) == [
        f"{ZIP}/inner"
    ]
    assert zipped(members, strip_components=2).ls(ZIP, detail=False) == [
        f"{ZIP}/deep.txt"
    ]


def test_directory_markers_do_not_decide_the_strip(fake_zenodo):
    """Whether a writer emitted explicit directory entries must not change the tree.

    `zipfile` omits them; many GUI archivers write them. Counting one as a
    top-level entry would make the same deposit expand differently depending on
    which tool zipped it.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("annotations/", b"")  # the marker
        zf.writestr("annotations/a.csv", b"a\n")
    payload = buffer.getvalue()
    fake_zenodo.bodies["archive.zip"] = payload
    fake_zenodo.record["files"][0]["size"] = len(payload)
    fake_zenodo.record["files"][0]["checksum"] = (
        f"md5:{hashlib.md5(payload).hexdigest()}"  # noqa: S324
    )
    fs = ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)
    assert fs.find(ZIP) == [f"{ZIP}/a.csv"]


def test_deciding_the_strip_does_not_open_a_records_zips(zfs, fake_zenodo):
    """`_zip_prefix` needs the central directory, which `ls(RECID)` must not pay for.

    Companion to `test_listing_a_record_does_not_open_its_zips`: that pinned
    `_container_as_directory`, this pins that the strip did not add a second
    reason to open every ZIP in a record just to list it.
    """
    zfs.ls(RECID)
    assert fake_zenodo.bytes_served == 0


def test_a_string_depth_is_coerced_at_the_door(zipped):
    """The CLI's `-o key=value` only special-cases true/false, so 0 arrives as "0".

    DVC coerces through the config schema, but nothing does for `-o` or a direct
    Python call, and truthy `"0"` would strip when told not to. `__init__` is the
    one place both routes pass through.
    """
    fs = zipped(WRAPPED, strip_components="0")
    assert fs.strip_components == 0
    assert fs.ls(ZIP, detail=False) == [f"{ZIP}/annotations"]


def test_the_version_token_still_names_the_real_member(zipped):
    """Stripping is a presentation choice; provenance must stay archive-truthful.

    The workspace path loses `annotations/`, but `zenodo.member` — and the CRC32
    the version token is built from — still identify the member as the ZIP
    stores it.
    """
    fs = zipped(WRAPPED)
    info = fs.info(f"{ZIP}/README.md")
    assert info["zenodo"]["member"] == "annotations/README.md"
    assert info["version_id"].endswith(f"{info['crc32']:08x}")


# ------------------------------------------------------- ranged reads at EOF


def _spy_on_ranges(fs) -> list[str]:
    """Record the ``Range`` header of every request ``fs`` makes."""
    seen: list[str] = []
    real = fs._request

    def spy(method, url, **kwargs):
        header = (kwargs.get("headers") or {}).get("Range")
        if header:
            seen.append(header)
        return real(method, url, **kwargs)

    fs._request = spy
    return seen


def test_a_read_past_the_end_is_clamped_not_requested(fs):
    """A block-aligned reader asks for the block containing the last byte.

    That overshoot is normal — every file's final read does it — and sending it to
    the server buys nothing.
    """
    seen = _spy_on_ranges(fs)
    info = fs.info(f"{RECID}/samples.csv")
    size = info["size"]
    assert fs._fetch_range(info, size - 10, size + 4_000_000) == CSV_BYTES[-10:]
    assert seen == [f"bytes={size - 10}-{size - 1}"], (
        "the request should stop at the last byte, not ask for what is not there"
    )


def test_an_empty_read_at_the_end_costs_no_request(fs):
    info = fs.info(f"{RECID}/samples.csv")
    seen = _spy_on_ranges(fs)
    assert fs._fetch_range(info, info["size"], info["size"] + 4096) == b""
    assert seen == []


def test_a_short_read_that_reached_eof_is_accepted_not_retried(fs, fake_zenodo):
    """A stale declared size must not look like a broken server.

    This is the case the clamp above cannot catch: the size archivefs was given is
    larger than the object, so the request looks in-bounds and the server answers
    correctly by stopping early. It used to burn `max_retries` requests and then
    raise, blaming the archive and advising the caller to reduce concurrency —
    advice that is actively misleading, since concurrency has nothing to do with
    it. Judged against `Content-Range`'s own total instead.
    """
    fake_zenodo.record["files"][1]["size"] = len(CSV_BYTES) + 5000
    seen = _spy_on_ranges(fs)
    info = fs.info(f"{RECID}/samples.csv")
    assert fs._fetch_range(info, 0, info["size"]) == CSV_BYTES
    assert len(seen) == 1, f"one request, not a retry storm: {seen}"


def test_a_short_read_below_eof_is_still_a_failure(fs, fake_zenodo, monkeypatch):
    """The guard above must not have disabled the check it lives inside.

    A server that truncates mid-object is a real fault, and the retry exists for
    it. Distinguished from the case above purely by whether the bytes served reach
    `Content-Range`'s stated total.

    `test_ega.py::test_short_body_is_retried_not_trusted` covers a short read that
    succeeds on retry; this covers the other end, where every attempt is short and
    the error is raised — the path the EOF guard sits directly in front of.
    """
    honest = ranged  # bind before the patch, or `truncating` calls itself

    def truncating(body, headers, **kwargs):
        # `truncate_to` is the fake's own facility for "says 206, sends fewer
        # bytes than promised"; 4093 of a requested 4096 stays far below the
        # object's real end, so it is a fault rather than EOF.
        return honest(body, headers, truncate_to=4093, **kwargs)

    # `FakeZenodo` resolves `ranged` in *this* module's namespace, so that is what
    # has to be replaced — patching tests.fake_http would leave the fake calling
    # the original through its own import.
    monkeypatch.setattr(f"{__name__}.ranged", truncating)
    monkeypatch.setattr(base_mod.ArchiveFileSystem, "_backoff", lambda *a: None)
    info = fs.info(f"{RECID}/samples.csv")
    seen = _spy_on_ranges(fs)
    with pytest.raises(OSError, match="would not serve bytes"):
        fs._fetch_range(info, 0, 4096)
    assert len(seen) == fs.max_retries


# ------------------------------------------------------- credential isolation


def test_a_token_file_outside_the_test_home_is_invisible(fake_zenodo, tmp_path):
    """`tests/conftest.py`'s job, pinned.

    Three tests started failing the day a real `~/.config/archivefs/zenodo.json`
    appeared on the developer's machine: they assert "no credential configured"
    behaviour, and silently began exercising the rejected-credential path instead.
    The fixtures cleared the credential *environment* and not the *files*, so the
    suite's result depended on who ran it.
    """
    assert ZenodoFileSystem(skip_instance_cache=True).token is None

    # The redirect is real, not a coincidence of the file being absent: a token
    # written into the *test* home is found.
    home = Path.home()
    (home / ".config" / "archivefs").mkdir(parents=True, exist_ok=True)
    token_file = home / ".config" / "archivefs" / "zenodo.json"
    token_file.write_text('{"token": "t0ken"}')
    token_file.chmod(0o600)  # archivefs refuses a group-readable credential
    assert ZenodoFileSystem(skip_instance_cache=True).token == "t0ken"


# --------------------------------------------------------------------- network


@pytest.fixture
def live_fs():
    if os.environ.get("ARCHIVEFS_TEST_ZENODO", "") != "1":
        pytest.skip("set ARCHIVEFS_TEST_ZENODO=1 to hit zenodo.org")
    return ZenodoFileSystem(skip_instance_cache=True)


@pytest.mark.network
def test_live_record_listing_and_bytes():
    """Record 1486184 is a tiny published ZIP, kept as a decompression demo."""
    if os.environ.get("ARCHIVEFS_TEST_ZENODO", "") != "1":
        pytest.skip("set ARCHIVEFS_TEST_ZENODO=1 to hit zenodo.org")
    fs = ZenodoFileSystem(skip_instance_cache=True)
    assert fs.ls("1486184", detail=False) == ["1486184/github-mark.zip"]
    info = fs.info("1486184/github-mark.zip")
    assert info["size"] == 853
    assert info["md5"] == "49f6a914ea33912471512ac9228c6923"
    data = fs.cat_file("1486184/github-mark.zip")
    assert hashlib.md5(data).hexdigest() == info["md5"]  # noqa: S324


@pytest.mark.network
def test_live_concept_doi_resolves_to_latest(live_fs):
    info = live_fs.info(CONCEPT_DOI)
    assert info["zenodo"]["record_id"] == "1486184"
    assert info["zenodo"]["doi"] == DOI


@pytest.mark.network
def test_live_zip_transparency(live_fs):
    fs = ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)
    names = fs.find("1486184/github-mark.zip")
    assert names
    assert all(n.startswith("1486184/github-mark.zip/") for n in names)


@pytest.mark.network
def test_live_a_cached_container_resumes_against_zenodo(live_fs, tmp_path):
    """The offline suite proves the bookkeeping; this proves it against a real ZIP.

    Reading a member costs one block, so a second reader that fetches *nothing* has
    genuinely served those bytes out of the file the first one left behind — which
    is the whole claim of issue #15, and the one thing a fake server cannot check
    for us because it has no notion of a process ending.
    """
    fs = ZenodoFileSystem(
        extract_zips=True, cache_dir=str(tmp_path), skip_instance_cache=True
    )
    member = fs.find("1486184/github-mark.zip")[0]
    first = fs.cat_file(member)
    fs._close_zip_views()

    (entry,) = blockcache.entries(tmp_path)
    assert entry["cached"] > 0

    later = ZenodoFileSystem(
        extract_zips=True, cache_dir=str(tmp_path), skip_instance_cache=True
    )
    fetched = counted_container_fetches(later)
    assert later.cat_file(member) == first
    assert fetched == [], "the resumed read went back to Zenodo"


@pytest.mark.network
def test_live_chained_url_through_fsspec():
    """The documented explicit form must work with no help from us."""
    if os.environ.get("ARCHIVEFS_TEST_ZENODO", "") != "1":
        pytest.skip("set ARCHIVEFS_TEST_ZENODO=1 to hit zenodo.org")
    import fsspec

    # Filesystem form: the whole interior as a tree.
    zfs, _, _ = fsspec.get_fs_token_paths(
        "zip://::zenodo://1486184/github-mark.zip"
    )
    members = zfs.ls("", detail=False)
    assert members
    # File form: one member's bytes. fsspec.open needs a member — a directory
    # has no bytes — which is why chained_url() takes one.
    member = members[0]
    with fsspec.open(f"zip://{member}::zenodo://1486184/github-mark.zip", "rb") as fh:
        assert fh.read(4)


def test_zip_view_refuses_a_slipping_archive(fake_zenodo):
    """A path we would refuse to write must never be advertised either."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr(zipfile.ZipInfo("../escaped.txt"), b"pwned")
    fake_zenodo.bodies["archive.zip"] = buffer.getvalue()
    fake_zenodo.record["files"][0]["size"] = len(buffer.getvalue())
    fake_zenodo.record["files"][0]["checksum"] = (
        f"md5:{hashlib.md5(buffer.getvalue()).hexdigest()}"  # noqa: S324
    )
    fs = ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)
    with pytest.raises(UnsafeArchiveMemberError):
        fs.ls(f"{RECID}/archive.zip")


def test_zip_bomb_is_refused_before_listing(fake_zenodo):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.bin", b"\0" * (8 * 1024 * 1024))
    fake_zenodo.bodies["archive.zip"] = buffer.getvalue()
    fake_zenodo.record["files"][0]["size"] = len(buffer.getvalue())
    fs = ZenodoFileSystem(
        extract_zips=True,
        zip_limits=ZipLimits(max_ratio=10, ratio_floor_bytes=1024),
        skip_instance_cache=True,
    )
    with pytest.raises(ZipBombError):
        fs.ls(f"{RECID}/archive.zip")


def test_concept_identifier_warns_that_the_version_is_not_recorded(fs, caplog):
    """A concept DOI means "latest", so the URL alone does not pin a version."""
    with caplog.at_level("WARNING"):
        fs.ls(CONCEPT_DOI)
    assert "concept identifier" in caplog.text
    assert DOI in caplog.text  # tells you which URL to use instead


def test_a_version_identifier_does_not_warn(fs, caplog):
    with caplog.at_level("WARNING"):
        fs.ls(DOI)
    assert "concept identifier" not in caplog.text


def test_isdir_agrees_with_info_for_a_transparent_zip(zfs):
    """They disagreed once, and DVC materialised the ZIP instead of its contents.

    Every derived predicate must go through info(), which is where a backend
    expresses "this member is presented as a directory".
    """
    path = f"{RECID}/archive.zip"
    assert zfs.info(path)["type"] == "directory"
    assert zfs.isdir(path) is True
    assert zfs.isfile(path) is False
    assert zfs.exists(path) is True
    # and the interior really is walkable, which is what DVC's download path needs
    assert zfs.find(path)


def test_opaque_zip_is_a_file_everywhere(fs):
    path = f"{RECID}/archive.zip"
    assert fs.info(path)["type"] == "file"
    assert fs.isfile(path) is True
    assert fs.isdir(path) is False


def test_a_zero_byte_file_is_readable(fake_zenodo):
    """`size or filesize` turned a legitimately empty file into an unknown size,
    and then into an OSError on the first read — failing the whole pull."""
    fake_zenodo.record["files"].append(
        {
            "id": "ccc",
            "key": "empty.txt",
            "size": 0,
            "checksum": f"md5:{hashlib.md5(b'').hexdigest()}",  # noqa: S324
            "links": {
                "self": f"https://zenodo.org/api/records/{RECID}/files/empty.txt/content"
            },
        }
    )
    fake_zenodo.bodies["empty.txt"] = b""
    fs = ZenodoFileSystem(skip_instance_cache=True)
    assert fs.info(f"{RECID}/empty.txt")["size"] == 0
    assert fs.cat_file(f"{RECID}/empty.txt") == b""
    with fs.open(f"{RECID}/empty.txt", "rb") as handle:
        assert handle.read() == b""


def test_a_file_with_no_published_checksum_reports_none_not_empty_string(fake_zenodo):
    fake_zenodo.record["files"].append(
        {
            "id": "ddd",
            "key": "nochecksum.txt",
            "size": 3,
            "links": {
                "self": f"https://zenodo.org/api/records/{RECID}/files/nochecksum.txt/content"
            },
        }
    )
    fake_zenodo.bodies["nochecksum.txt"] = b"abc"
    fs = ZenodoFileSystem(skip_instance_cache=True)
    info = fs.info(f"{RECID}/nochecksum.txt")
    assert info["md5"] is None  # not ""
    assert info["checksum"] is None
    assert info["checksum_type"] is None
    assert fs.checksum(f"{RECID}/nochecksum.txt") is None


def test_invalidate_cache_drops_the_record_json_too(fs, fake_zenodo):
    """The record JSON *contains* the file listing, so a stale record is a stale
    listing however thoroughly the member cache is cleared."""
    assert "samples.csv" in " ".join(fs.ls(RECID, detail=False))
    fake_zenodo.record["files"] = [
        entry for entry in fake_zenodo.record["files"] if entry["key"] != "samples.csv"
    ]
    fs.invalidate_cache()
    assert "samples.csv" not in " ".join(fs.ls(RECID, detail=False))


def test_extract_zips_off_as_a_string_means_off(fake_zenodo):
    """DVC config files are INI, so booleans arrive as strings; "false" is truthy."""
    fs = ZenodoFileSystem(extract_zips=False, skip_instance_cache=True)
    assert fs.info(f"{RECID}/archive.zip")["type"] == "file"


def test_chiro_off_as_a_string_is_not_a_manifest_path(fake_zenodo):
    fs = ZenodoFileSystem(chiro="false", skip_instance_cache=True)
    # "false" must not be treated as the name of a manifest to fetch
    assert fs.ls(RECID, detail=False)
    assert fs.manifest_path_for(RECID) is None


def test_container_md5_is_download_free_but_member_md5_cannot_be(zfs, fake_zenodo):
    """The asymmetric integrity contract, stated as a test.

    A member's md5 is only knowable by decompressing it, so it is *not* published
    download-free — but the container's is, and immutability of the version DOI is
    what makes verifying the container enough to trust the members.
    """
    container = zfs.info(f"{RECID}/archive.zip")
    assert container["zenodo"]["container_md5"] == hashlib.md5(ZIP_BYTES).hexdigest()  # noqa: S324
    member = zfs.info(f"{RECID}/archive.zip/README.md")
    assert member["md5"] is None
    assert member["crc32"] is not None  # from the central directory
    # listing the interior read only the central directory, not the payload
    assert fake_zenodo.bytes_served < len(ZIP_BYTES) / 4


def test_a_failed_zip_fetch_is_not_reported_as_a_corrupt_archive(fake_zenodo):
    """zipfile reads the end of a file inside `except OSError: pass`, so a 401 or a
    503 came back as "File is not a zip file" — telling the user their perfectly
    good archive was corrupt."""
    fake_zenodo.restricted = True
    fs = ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)
    with pytest.raises(AuthenticationError) as exc:
        fs.find(f"{RECID}/archive.zip")
    assert "ZENODO_TOKEN" in str(exc.value)
    assert "not a zip" not in str(exc.value).lower()


def test_a_genuinely_non_zip_member_still_says_so(fake_zenodo):
    fake_zenodo.bodies["archive.zip"] = b"this is not a zip at all"
    fake_zenodo.record["files"][0]["size"] = len(fake_zenodo.bodies["archive.zip"])
    fs = ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls(f"{RECID}/archive.zip")
    assert "not a readable ZIP" in str(exc.value)


def test_invalidating_one_spelling_invalidates_the_aliases(fs, fake_zenodo):
    """A record answers to a record id, a version DOI and a concept DOI."""
    fs.ls(DOI)
    fs.ls(RECID)
    fs.invalidate_cache(RECID)
    assert DOI not in fs._records
    assert CONCEPT_DOI not in fs._records


def test_a_restricted_record_is_not_an_empty_directory(fake_zenodo):
    """Zenodo answers 200 with metadata and *no* file list for a record you
    cannot read — not 401, not 403.

    Reporting that as an empty directory is the worst available outcome: `ls`
    says the record holds nothing, and `dvc import-url` of the record writes an
    empty .dir and exits 0, so a pipeline imports nothing and calls it success.
    Measured on 10.5281/zenodo.18917100 (access_right: restricted).
    """
    fake_zenodo.record["files"] = []
    fake_zenodo.record["metadata"]["access_right"] = "restricted"
    fs = ZenodoFileSystem(skip_instance_cache=True)
    with pytest.raises(AuthenticationError) as exc:
        fs.ls(RECID)
    message = str(exc.value)
    assert "restricted" in message
    assert "no file list" in message
    assert "ZENODO_TOKEN" in message
    # nothing was sent, so nothing was "rejected"
    assert "refused access" in message
    assert "rejected the credential" not in message
    assert exc.value.used_credentials is False


def test_an_open_record_with_no_files_is_still_empty_not_an_error(fake_zenodo):
    """A metadata-only deposit is a legitimate thing; only restriction is an error."""
    fake_zenodo.record["files"] = []
    fake_zenodo.record["metadata"]["access_right"] = "open"
    fs = ZenodoFileSystem(skip_instance_cache=True)
    assert fs.ls(RECID, detail=False) == []


# ------------------------------------- a cache that survives the process (#15)


def counted_container_fetches(fs) -> list[list[tuple[int, int]]]:
    """Record only the *member view's* fetches.

    The listing view goes through `_fetch_range` and the member view through
    `_fetch_block_ranges`, so watching the latter isolates the cache under test from
    the central-directory reads that happen either way.
    """
    seen: list[list[tuple[int, int]]] = []
    real = fs._fetch_block_ranges

    def spy(info, spans):
        seen.append(list(spans))
        return real(info, spans)

    fs._fetch_block_ranges = spy
    return seen


def test_without_a_cache_dir_the_blocks_die_with_the_process(zipped):
    """The default, and what issue #15 describes: `tempfile.TemporaryFile()`.

    Unlinked at creation, so a 2h50m import that had cached 16.1 GB started again
    from zero. Nothing here is opt-out; the cache is opt-*in*, so with no `cache_dir`
    there must be no file on disk to find and no index recording one.
    """
    fs = zipped(BIG)
    handle = fs._open_container(ZIP, cache_type="mmap")
    assert handle.cache.location is None
    assert handle.cache.index is None
    assert handle.cache.lock is None


def test_an_interrupted_container_read_resumes_from_disk(zipped, tmp_path):
    """The property the issue is about: the second attempt keeps the first's bytes."""
    fs = zipped(BIG, cache_dir=str(tmp_path))
    handle = fs._open_container(ZIP, cache_type="mmap")
    assert isinstance(handle.cache, blockcache.ResumableBlockCache)
    prefix = handle.read(600_000)
    handle.cache.release()  # what a clean exit does; a kill -9 does not

    later = ZenodoFileSystem(
        extract_zips=True, cache_dir=str(tmp_path), skip_instance_cache=True
    )
    fetched = counted_container_fetches(later)
    resumed = later._open_container(ZIP, cache_type="mmap")
    assert resumed.read(600_000) == prefix
    assert fetched == [], "the resumed read went back to Zenodo for cached blocks"
    resumed.cache.release()


def test_a_member_read_populates_and_then_reuses_the_cache(zipped, tmp_path):
    """End to end through `cat_file`, which is the path a `dvc import-url` takes."""
    fs = zipped(BIG, cache_dir=str(tmp_path))
    assert fs.cat_file(f"{ZIP}/a.bin") == BIG["a.bin"]
    fs._close_zip_views()

    (entry,) = blockcache.entries(tmp_path)
    assert entry["protocol"] == "zenodo"
    assert entry["cached"] > 0

    later = ZenodoFileSystem(
        extract_zips=True, cache_dir=str(tmp_path), skip_instance_cache=True
    )
    fetched = counted_container_fetches(later)
    assert later.cat_file(f"{ZIP}/a.bin") == BIG["a.bin"]
    assert fetched == []


def test_closing_a_view_releases_the_lock_for_the_next_import(zipped, tmp_path):
    """A held lock pushes the next reader onto a private cache, silently.

    So the lock has to go when the view does, not merely when the process does —
    DVC keeps one filesystem alive across a whole command.
    """
    fs = zipped(BIG, cache_dir=str(tmp_path))
    fs.cat_file(f"{ZIP}/a.bin")
    fs._close_zip_views()

    later = ZenodoFileSystem(
        extract_zips=True, cache_dir=str(tmp_path), skip_instance_cache=True
    )
    handle = later._open_container(ZIP, cache_type="mmap")
    assert isinstance(handle.cache, blockcache.ResumableBlockCache)
    handle.cache.release()


def test_a_republished_container_is_not_resumed_into(zipped, fake_zenodo, tmp_path):
    """Zenodo records are immutable, but a *concept* DOI moves between versions.

    The published md5 is in the cache key, so a different deposit at the same path
    gets a different entry rather than the previous version's blocks.
    """
    fs = zipped(BIG, cache_dir=str(tmp_path))
    fs.cat_file(f"{ZIP}/a.bin")
    fs._close_zip_views()

    fake_zenodo.record["files"][0]["checksum"] = "md5:" + "0" * 32
    later = ZenodoFileSystem(
        extract_zips=True, cache_dir=str(tmp_path), skip_instance_cache=True
    )
    fetched = counted_container_fetches(later)
    assert later.cat_file(f"{ZIP}/a.bin") == BIG["a.bin"]
    assert fetched, "a changed md5 resumed into the previous deposit's blocks"


# ------------------------------------------ configuration reaches the backend


@pytest.fixture
def layered(tmp_path, monkeypatch):
    """Write config layers and run as though inside a repository that has them."""
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "etc"))
    monkeypatch.chdir(project)

    def write(layer: str, data: dict) -> None:
        from archivefs import config

        path = {
            "user": tmp_path / "xdg" / config.XDG_NAME,
            "project": project / config.PROJECT_NAME,
            "local": project / config.LOCAL_NAME,
        }[layer]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    return write


def test_a_committed_config_can_turn_on_zip_expansion(fake_zenodo, layered):
    """The reason `project` is allowed to set content keys at all.

    It is committed, so a colleague's clone reads the same setting and gets the same
    tree — which is exactly what a user-level setting could not promise.
    """
    layered("project", {"zenodo": {"extract_zips": True}})
    fs = ZenodoFileSystem(skip_instance_cache=True)
    assert fs.extract_zips is True
    assert fs.isdir(f"{RECID}/archive.zip")


def test_an_explicit_option_still_beats_the_committed_config(fake_zenodo, layered):
    """Precedence has to hold at the top or the config is a trap rather than a default."""
    layered("project", {"zenodo": {"extract_zips": True}})
    fs = ZenodoFileSystem(extract_zips=False, skip_instance_cache=True)
    assert fs.extract_zips is False
    assert not fs.isdir(f"{RECID}/archive.zip")


def test_configured_zero_is_honoured_not_treated_as_absent(fake_zenodo, layered):
    """The bug the `None` defaults exist to prevent.

    With `strip_components: int = 1` in the signature there is no way to tell "the
    user said 0" from "the user said nothing", so a configured 0 would be silently
    replaced by the default — the same trap as a configured `false`.
    """
    layered("project", {"zenodo": {"extract_zips": True, "strip_components": 0}})
    fs = ZenodoFileSystem(skip_instance_cache=True)
    assert fs.strip_components == 0


def test_a_user_config_can_turn_on_the_resumable_cache(fake_zenodo, layered, tmp_path):
    """The setting that started all this: naming the directory *is* the opt-in."""
    blocks = tmp_path / "blocks"
    layered("user", {"cache_dir": str(blocks)})
    fs = ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)
    handle = fs._open_container(ZIP, cache_type="mmap")
    assert handle.cache.location is not None
    assert str(blocks) in handle.cache.location
    handle.cache.release()


def test_the_environment_beats_a_configured_cache_dir(fake_zenodo, layered, tmp_path,
                                                      monkeypatch):
    """A shell variable is more specific than a file, and must win."""
    monkeypatch.setenv("ARCHIVEFS_CACHE_DIR", str(tmp_path / "from-env"))
    layered("user", {"cache_dir": str(tmp_path / "from-file")})
    fs = ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)
    handle = fs._open_container(ZIP, cache_type="mmap")
    assert "from-env" in handle.cache.location
    handle.cache.release()


def test_one_command_can_still_opt_out_of_a_configured_cache(fake_zenodo, layered,
                                                             tmp_path):
    """`cache_dir=false` has to beat an enabled config, or turning it off for one
    import would mean editing a file the rest of your work depends on."""
    layered("user", {"cache_dir": str(tmp_path / "blocks")})
    fs = ZenodoFileSystem(
        extract_zips=True, cache_dir="false", skip_instance_cache=True
    )
    handle = fs._open_container(ZIP, cache_type="mmap")
    assert handle.cache.location is None
    assert handle.cache.index is None
