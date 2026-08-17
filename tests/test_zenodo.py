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
import zipfile
from pathlib import Path

import pytest
from tests.fake_http import FakeRequestsModule, FakeResponse, ranged

from archivefs.backends import zenodo as zenodo_mod
from archivefs.backends.zenodo import ZenodoFileSystem
from archivefs.errors import (
    ArchiveNotFoundError,
    AuthenticationError,
    NotSupportedError,
    UnsafeArchiveMemberError,
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
            "checksum": f"md5:{hashlib.md5(ZIP_BYTES).hexdigest()}",
            "links": {
                "self": f"https://zenodo.org/api/records/{RECID}/files/archive.zip/content"
            },
        },
        {
            "id": "bbb",
            "key": "samples.csv",
            "size": len(CSV_BYTES),
            "checksum": f"md5:{hashlib.md5(CSV_BYTES).hexdigest()}",
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
    monkeypatch.setattr(zenodo_mod, "requests", FakeRequestsModule(server))
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
    assert info["md5"] == hashlib.md5(CSV_BYTES).hexdigest()
    assert info["version_id"] == DOI
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
    assert hashlib.md5(data).hexdigest() == fs.info(f"{RECID}/samples.csv")["md5"]


def test_ranged_reads(fs):
    assert fs.cat_file(f"{RECID}/samples.csv", start=10, end=25) == CSV_BYTES[10:25]
    with fs.open(f"{RECID}/samples.csv", "rb", block_size=512) as fh:
        fh.seek(1234)
        assert fh.read(100) == CSV_BYTES[1234:1334]


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
    assert "never in the committed" in message


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
    assert info["zenodo"]["container_md5"] == hashlib.md5(ZIP_BYTES).hexdigest()
    assert info["zenodo"]["container"] == f"{RECID}/archive.zip"
    assert "CRC32" in info["zenodo"]["integrity"]
    assert info["version_id"] == DOI  # immutability is what makes it sound


def test_zip_interior_sizes_are_real(zfs):
    info = zfs.info(f"{RECID}/archive.zip/counts/barcodes.tsv")
    with zipfile.ZipFile(io.BytesIO(ZIP_BYTES)) as zf:
        assert info["size"] == zf.getinfo("counts/barcodes.tsv").file_size


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
    assert hashlib.md5(data).hexdigest() == info["md5"]


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
        f"md5:{hashlib.md5(buffer.getvalue()).hexdigest()}"
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
