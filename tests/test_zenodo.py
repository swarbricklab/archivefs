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

from archivefs import base as base_mod
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
