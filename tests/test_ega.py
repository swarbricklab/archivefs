"""EGA backend tests.

Offline by default: a fake EGA built from a real API-v2 reply
(``tests/data/ega_EGAD00001009826_files.json``). The ``network`` tests at the
bottom hit the live service with EGA's own published test account, which is how
the checksum contract in ``docs/decisions.md`` was established — keep them
runnable.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest
from tests.fake_http import FakeRequestsModule, FakeResponse, parse_range, ranged

from archivefs.backends import ega as ega_mod
from archivefs.backends.ega import EGAFileSystem
from archivefs.errors import (
    ArchiveNotFoundError,
    AuthenticationError,
    CredentialsNotFoundError,
    NotSupportedError,
)

DATA = Path(__file__).parent / "data"
DATASET = "EGAD00001009826"
FILES = json.loads((DATA / f"ega_{DATASET}_files.json").read_text())
BAM = "EGAF00007243773"  # 194837 encrypted / 194821 plain


def payload_for(file_id: str) -> bytes:
    """Deterministic stand-in for a file's decrypted bytes."""
    meta = next(f for f in FILES if f["fileId"] == file_id)
    plain_size = meta["fileSize"] - 16
    blob = (file_id.encode() * (plain_size // len(file_id) + 1))[:plain_size]
    return blob


class FakeEGA:
    """The three endpoints the backend uses, plus the failure modes we care about."""

    def __init__(self) -> None:
        self.token_requests = 0
        self.range_starts: list[int] = []
        self.expire_next_data_call = False
        self.truncate_next_data_call = False
        self.authorized = [
            {"datasetId": DATASET, "description": "test", "dacStableId": "EGAC1"}
        ]

    def __call__(self, method: str, url: str, headers: dict, kwargs: dict):
        if "openid-connect-server/token" in url:
            self.token_requests += 1
            data = kwargs.get("data") or {}
            if data.get("password") != "correct-horse":
                return FakeResponse(401, json={"error": "invalid_grant"})
            return FakeResponse(
                200,
                json={"access_token": f"tok-{self.token_requests}", "expires_in": 3600},
            )

        if headers.get("Authorization", "").startswith("Bearer tok-"):
            token = headers["Authorization"].split("Bearer ")[1]
        else:
            return FakeResponse(401, json={"error": "unauthorized"})

        if url.endswith("/metadata/datasets"):
            return FakeResponse(200, json=self.authorized)
        if url.endswith(f"/metadata/datasets/{DATASET}/files"):
            return FakeResponse(200, json=FILES)
        if "/metadata/datasets/" in url:
            return FakeResponse(404, json={"error": "not found"})
        if "/metadata/files/" in url:
            file_id = url.rsplit("/", 1)[-1]
            match = [f for f in FILES if f["fileId"] == file_id]
            if not match:
                return FakeResponse(404, json={})
            return FakeResponse(200, json=match[0])
        if "/files/" in url:
            file_id = url.rsplit("/", 1)[-1].split("?")[0]
            if self.expire_next_data_call:
                self.expire_next_data_call = False
                return FakeResponse(401, json={"error": "token expired"})
            truncate = None
            if self.truncate_next_data_call:
                self.truncate_next_data_call = False
                truncate = 3
            self.last_token = token
            body = payload_for(file_id)
            start, _ = parse_range(headers)
            self.range_starts.append(start)
            if start % 16:
                # Exactly what the live service does: 206, right length, wrong
                # bytes. See docs/decisions.md D3.
                body = bytes(len(body))
            return ranged(body, headers, truncate_to=truncate)
        raise AssertionError(f"unexpected URL: {url}")


@pytest.fixture
def fake_ega(monkeypatch):
    server = FakeEGA()
    fake_requests = FakeRequestsModule(server)
    monkeypatch.setattr(ega_mod, "requests", fake_requests)
    monkeypatch.setenv("EGA_USERNAME", "user@example.org")
    monkeypatch.setenv("EGA_PASSWORD", "correct-horse")
    server.requests = fake_requests
    return server


@pytest.fixture
def fs(fake_ega):
    return EGAFileSystem(skip_instance_cache=True)


# --------------------------------------------------------------- credentials


def test_credentials_from_env(fake_ega):
    fs = EGAFileSystem(skip_instance_cache=True)
    assert fs.auth.username == "user@example.org"
    assert "$EGA_PASSWORD" in fs.auth.credential_source


def test_username_from_storage_options_password_from_env(fake_ega, monkeypatch):
    """The committed .dvc may carry a username; the password must not be in it."""
    monkeypatch.delenv("EGA_USERNAME")
    fs = EGAFileSystem(username="from-dvc-config@example.org", skip_instance_cache=True)
    assert fs.auth.username == "from-dvc-config@example.org"
    assert "storage_options" in fs.auth.credential_source
    assert "$EGA_PASSWORD" in fs.auth.credential_source


def test_credentials_from_file(fake_ega, monkeypatch, tmp_path):
    monkeypatch.delenv("EGA_USERNAME")
    monkeypatch.delenv("EGA_PASSWORD")
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"username": "f@x.org", "password": "correct-horse"}))
    fs = EGAFileSystem(credentials_file=str(creds), skip_instance_cache=True)
    assert fs.auth.username == "f@x.org"
    assert str(creds) in fs.auth.credential_source


def test_missing_credentials_lists_every_place_it_looked(fake_ega, monkeypatch):
    monkeypatch.delenv("EGA_USERNAME")
    monkeypatch.delenv("EGA_PASSWORD")
    monkeypatch.setattr(Path, "exists", lambda self: False)
    with pytest.raises(CredentialsNotFoundError) as exc:
        EGAFileSystem(skip_instance_cache=True)
    message = str(exc.value)
    assert "$EGA_PASSWORD" in message
    assert ".dvc/config.local" in message
    assert "never be written into a committed .dvc" in message


def test_bad_password_is_actionable(fake_ega, monkeypatch):
    monkeypatch.setenv("EGA_PASSWORD", "wrong")
    fs = EGAFileSystem(skip_instance_cache=True)
    with pytest.raises(AuthenticationError) as exc:
        fs.ls("")
    message = str(exc.value)
    assert "$EGA_PASSWORD" in message  # tells you which source it used
    assert "What to do" in message


# ------------------------------------------------------------------- listing


def test_ls_root_gives_authorized_datasets(fs):
    assert fs.ls("", detail=False) == [DATASET]


def test_ls_dataset_default_layout_is_accessions(fs):
    names = fs.ls(DATASET, detail=False)
    assert sorted(names) == sorted(f"{DATASET}/{f['fileId']}" for f in FILES)


def test_ls_display_layout_uses_filenames(fake_ega):
    fs = EGAFileSystem(layout="display", skip_instance_cache=True)
    names = {n.split("/", 1)[1] for n in fs.ls(DATASET, detail=False)}
    assert "HG00096.GRCh38DH__1097r__10.10000-10100__21.5000000-5050000.bam" in names
    assert len(names) == len(FILES)  # injective


def test_ls_nested_layout_is_pyega3_shaped(fake_ega):
    fs = EGAFileSystem(layout="nested", skip_instance_cache=True)
    names = fs.ls(DATASET, detail=False)
    assert f"{DATASET}/{BAM}/HG00096.GRCh38DH__1097r__10.10000-10100__21.5000000-5050000.bam" in names
    # the accession level is a real directory
    assert fs.isdir(f"{DATASET}/{BAM}")
    assert fs.ls(f"{DATASET}/{BAM}", detail=False) == [
        f"{DATASET}/{BAM}/HG00096.GRCh38DH__1097r__10.10000-10100__21.5000000-5050000.bam"
    ]


def test_display_layout_disambiguates_collisions(fake_ega, monkeypatch):
    clashing = [dict(f) for f in FILES[:2]]
    for f in clashing:
        f["displayFileName"] = "same.bam"
    monkeypatch.setattr(ega_mod, "requests", fake_ega.requests)
    fake_ega_files = clashing
    server = fake_ega

    def handler(method, url, headers, kwargs):
        if url.endswith(f"/metadata/datasets/{DATASET}/files"):
            return FakeResponse(200, json=fake_ega_files)
        return FakeEGA.__call__(server, method, url, headers, kwargs)

    monkeypatch.setattr(ega_mod, "requests", FakeRequestsModule(handler))
    fs = EGAFileSystem(layout="display", skip_instance_cache=True)
    names = fs.ls(DATASET, detail=False)
    assert len(set(names)) == 2, names
    assert all("same" in n for n in names)


@pytest.mark.parametrize(
    "accession",
    [
        "EGAD00001999999",  # plausible but unknown
        "EGAD00000000003",  # one of EGA's retired legacy datasets
    ],
)
def test_unknown_dataset_says_what_you_can_see(fs, accession):
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls(accession)
    assert DATASET in str(exc.value)


def test_glob_comes_free_from_ls(fake_ega):
    fs = EGAFileSystem(layout="display", skip_instance_cache=True)
    hits = fs.glob(f"{DATASET}/*.bam")
    assert hits
    assert all(h.endswith(".bam") for h in hits)


# ---------------------------------------------------------------------- info


def test_info_is_download_free_and_carries_the_plain_md5(fs):
    info = fs.info(f"{DATASET}/{BAM}")
    meta = next(f for f in FILES if f["fileId"] == BAM)
    assert info["size"] == meta["fileSize"] - 16
    assert info["md5"] == meta["plainChecksum"]
    assert info["checksum"] == meta["plainChecksum"]
    assert fs.checksum(f"{DATASET}/{BAM}") == meta["plainChecksum"]
    # download-free: no data-endpoint call was made
    assert not [c for c in fs.session.calls if "?destinationFormat" in c["url"]]


def test_info_version_token_from_reencryption_stamp(fs):
    info = fs.info(f"{DATASET}/{BAM}")
    assert info["version_id"] == "1664408722194"


def test_file_addressable_without_its_dataset(fs):
    info = fs.info(BAM)
    assert info["ega"]["file_id"] == BAM
    assert info["size"] == 194821


@pytest.mark.parametrize("layout", ["accession", "display", "nested"])
def test_any_layout_resolves_any_url_form(fake_ega, layout):
    """A .dvc written under one layout must still resolve under another."""
    display = "HG00096.GRCh38DH__1097r__10.10000-10100__21.5000000-5050000.bam"
    fs = EGAFileSystem(layout=layout, skip_instance_cache=True)
    assert fs.info(f"{DATASET}/{display}")["ega"]["file_id"] == BAM
    if layout == "nested":
        # Under the nested layout the accession is a directory holding the one
        # file, so ega://EGAD.../EGAF... imports a one-file directory.
        assert fs.info(f"{DATASET}/{BAM}")["type"] == "directory"
        assert fs.info(f"{DATASET}/{BAM}/{display}")["ega"]["file_id"] == BAM
    else:
        assert fs.info(f"{DATASET}/{BAM}")["ega"]["file_id"] == BAM


def test_exists(fs):
    assert fs.exists(DATASET)
    assert fs.exists(f"{DATASET}/{BAM}")
    assert not fs.exists(f"{DATASET}/EGAF00000000000")
    assert not fs.exists("not-an-accession")


# ------------------------------------------------------------------ selection


def test_selection_driven_virtual_ls(fake_ega):
    fs = EGAFileSystem(selection=[BAM], skip_instance_cache=True)
    assert fs.ls(DATASET, detail=False) == [f"{DATASET}/{BAM}"]


def test_selection_accepts_display_names(fake_ega):
    name = "HG00096.GRCh38DH__1097r__10.10000-10100__21.5000000-5050000.bam"
    fs = EGAFileSystem(selection=[name], skip_instance_cache=True)
    assert fs.ls(DATASET, detail=False) == [f"{DATASET}/{BAM}"]


def test_empty_selection_is_an_error_not_an_empty_import(fake_ega):
    fs = EGAFileSystem(selection=["EGAF99999999999"], skip_instance_cache=True)
    with pytest.raises(ArchiveNotFoundError):
        fs.ls(DATASET)


def test_index_files_can_be_dropped(fake_ega):
    fs = EGAFileSystem(
        layout="display", include_index_files=False, skip_instance_cache=True
    )
    names = fs.ls(DATASET, detail=False)
    assert not [n for n in names if n.endswith((".bai", ".crai"))]


# ------------------------------------------------------------------- transfer


def test_cat_file_matches_the_advertised_md5(fs):
    data = fs.cat_file(f"{DATASET}/{BAM}")
    assert data == payload_for(BAM)
    assert len(data) == fs.info(f"{DATASET}/{BAM}")["size"]


def test_partial_reads(fs):
    body = payload_for(BAM)
    assert fs.cat_file(f"{DATASET}/{BAM}", start=10, end=20) == body[10:20]
    assert fs.cat_file(f"{DATASET}/{BAM}", start=-5) == body[-5:]


def test_open_and_seek(fs):
    body = payload_for(BAM)
    with fs.open(f"{DATASET}/{BAM}", "rb", block_size=4096) as fh:
        assert fh.size == len(body)
        assert fh.read(100) == body[:100]
        fh.seek(50000)
        assert fh.read(100) == body[50000:50100]
        fh.seek(-10, 2)
        assert fh.read() == body[-10:]


def test_token_refresh_on_401_mid_transfer(fs, fake_ega):
    """The one EGA lifecycle problem the design doc calls out by name."""
    fs.cat_file(f"{DATASET}/{BAM}")
    tokens_before = fake_ega.token_requests
    fake_ega.expire_next_data_call = True
    data = fs.cat_file(f"{DATASET}/{BAM}")
    assert data == payload_for(BAM)
    assert fake_ega.token_requests == tokens_before + 1


def test_short_body_is_retried_not_trusted(fs, fake_ega, monkeypatch):
    monkeypatch.setattr(ega_mod.time, "sleep", lambda _: None)
    fake_ega.truncate_next_data_call = True
    data = fs.cat_file(f"{DATASET}/{BAM}")
    assert data == payload_for(BAM)  # retried, not silently truncated


def test_plain_size_from_content_range(fs):
    assert fs.plain_size(f"{DATASET}/{BAM}") == 194821


def test_unaligned_reads_are_aligned_before_asking(fs, fake_ega):
    """EGA returns *wrong bytes*, not an error, for an unaligned range start."""
    body = payload_for(BAM)
    for start, end in [(1, 100), (999, 1500), (12345, 12400), (17, 18)]:
        assert fs.cat_file(f"{DATASET}/{BAM}", start=start, end=end) == body[start:end]
    assert all(s % 16 == 0 for s in fake_ega.range_starts), fake_ega.range_starts


def test_unaligned_seek_through_open(fs, fake_ega):
    body = payload_for(BAM)
    with fs.open(f"{DATASET}/{BAM}", "rb", block_size=1000) as fh:
        fh.seek(4097)
        assert fh.read(50) == body[4097:4147]
    assert all(s % 16 == 0 for s in fake_ega.range_starts), fake_ega.range_starts


# ------------------------------------------------------------------ read-only


@pytest.mark.parametrize("op", ["mkdir", "rm", "touch", "rmdir"])
def test_write_operations_refuse(fs, op):
    with pytest.raises(NotSupportedError):
        getattr(fs, op)(f"{DATASET}/x")


def test_open_for_write_refuses(fs):
    with pytest.raises(NotSupportedError):
        fs.open(f"{DATASET}/{BAM}", "wb")


# -------------------------------------------------------------------- network

pytestmark_network = pytest.mark.network


@pytest.fixture
def live_fs():
    """The live EGA service, via EGA's own published test account.

    ``ega-test-data@ebi.ac.uk`` ships inside pyega3
    (``config/default_credential_file.json``) and serves only synthetic data.
    """
    if os.environ.get("ARCHIVEFS_TEST_EGA", "") != "1":
        pytest.skip("set ARCHIVEFS_TEST_EGA=1 to hit the live EGA service")
    return EGAFileSystem(
        username="ega-test-data@ebi.ac.uk",
        password="egarocks",
        skip_instance_cache=True,
    )


@pytest.mark.network
def test_live_ls_and_info(live_fs):
    datasets = live_fs.ls("", detail=False)
    assert "EGAD00001009826" in datasets
    info = live_fs.info(f"EGAD00001009826/{BAM}")
    assert info["size"] == 194821
    assert info["md5"] == "ed365c71461eac21a64d2c29e7216e50"


@pytest.mark.network
def test_live_bytes_match_advertised_md5(live_fs):
    """The contract the whole --no-download design rests on (docs/decisions.md D1)."""
    path = f"EGAD00001009826/{BAM}"
    info = live_fs.info(path)
    data = live_fs.cat_file(path)
    assert len(data) == info["size"]
    assert hashlib.md5(data).hexdigest() == info["md5"]


@pytest.mark.network
def test_live_ranged_read(live_fs):
    path = f"EGAD00001009826/{BAM}"
    whole = live_fs.cat_file(path)
    assert live_fs.cat_file(path, start=1000, end=2000) == whole[1000:2000]
