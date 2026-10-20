"""Mendeley Data backend tests (EXPERIMENTAL backend, issue #47 / D44).

Run entirely offline. The fake mirrors the live frontend API this backend is built
on: anonymous dataset metadata, an anonymous ``/folders/{version}`` list, and an
anonymous paginated ``/files?folder_id=…`` call whose entries carry sha256 (never
md5) and a stable ``download_url``. Shapes are trimmed from real replies for
``10.17632/yzxtxn4nmd``.
"""

from __future__ import annotations

import hashlib

import pytest
from tests.fake_http import FakeRequestsModule, FakeResponse, ranged

from archivefs import base as base_mod
from archivefs.backends.mendeley import MendeleyFileSystem
from archivefs.errors import ArchiveNotFoundError, NotSupportedError, VerificationError

DATASET_ID = "yzxtxn4nmd"
CONCEPT_DOI = "10.17632/yzxtxn4nmd"
VERSION_DOI = "10.17632/yzxtxn4nmd.4"
CONCEPT = CONCEPT_DOI  # mendeley://10.17632/yzxtxn4nmd

README = b"# SCAN-B SSP release\n"
GENE = b"gene\tFPKM\n" + b"".join(f"ENSG{i}\t{i}.0\n".encode() for i in range(300))

FOLDERS = [
    {"id": "fold-supp", "name": "Supplemental Data Table", "description": ""},
    {"id": "fold-gene", "name": "StringTie FPKM Gene Data", "description": ""},
]
# folder_id -> list of file entries
FILES = {
    "fold-supp": [
        {
            "filename": "README.txt",
            "id": "file-readme",
            "folder_id": "fold-supp",
            "content_details": {
                "id": "cd-readme",
                "size": len(README),
                "sha256_hash": hashlib.sha256(README).hexdigest(),
                "content_type": "text/plain",
                "download_url": "https://data.mendeley.com/public-files/datasets/"
                "yzxtxn4nmd/files/file-readme/file_downloaded",
            },
        }
    ],
    "fold-gene": [
        {
            "filename": "SCANB.9206.genematrix_noNeg.txt",
            "id": "file-gene",
            "folder_id": "fold-gene",
            "content_details": {
                "id": "cd-gene",
                "size": len(GENE),
                "sha256_hash": hashlib.sha256(GENE).hexdigest(),
                "content_type": "text/plain",
                "download_url": "https://data.mendeley.com/public-files/datasets/"
                "yzxtxn4nmd/files/file-gene/file_downloaded",
            },
        }
    ],
}
BODIES = {"file-readme": README, "file-gene": GENE}


def dataset_json(version: int = 4) -> dict:
    return {
        "id": DATASET_ID,
        "doi": {"id": f"10.17632/yzxtxn4nmd.{version}"},
        "name": "RNA Sequencing-Based Single Sample Predictors",
        "version": version,
        "versions": [
            {"version": 4, "publish_date": "2025-01-10T14:57:24.012Z"},
            {"version": 3, "publish_date": "2023-01-17T09:29:38.059Z"},
        ],
        "publish_date": "2025-01-10T14:57:24.012Z",
        "data_licence": {"short_name": "CC BY 4.0"},
    }


class FakeMendeley:
    def __init__(self) -> None:
        self.bytes_served = 0
        self.saw_user_agent = False

    def __call__(self, method: str, url: str, headers: dict, kwargs: dict):
        if "archivefs" in headers.get("User-Agent", ""):
            self.saw_user_agent = True
        # a presigned-style file download (ranged)
        for fid, body in BODIES.items():
            if f"/files/{fid}/file_downloaded" in url:
                resp = ranged(body, headers)
                self.bytes_served += len(resp.content)
                return resp
        # folders list: /datasets/{id}/folders/{version}
        if "/folders/" in url:
            return FakeResponse(200, json=FOLDERS)
        # files in a folder: /datasets/{id}/files?folder_id=...
        if "/files" in url and "folder_id=" in url:
            fid = url.split("folder_id=")[1].split("&")[0]
            return FakeResponse(200, json=FILES.get(fid, []))
        # dataset metadata
        if "/datasets/" in url:
            version = 4
            if "version=" in url:
                version = int(url.split("version=")[1].split("&")[0])
            return FakeResponse(200, json=dataset_json(version))
        return FakeResponse(404, json={"error": 404})


@pytest.fixture
def fake_mendeley(monkeypatch):
    server = FakeMendeley()
    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
    return server


@pytest.fixture
def fs(fake_mendeley):
    return MendeleyFileSystem(skip_instance_cache=True)


# ------------------------------------------------------------------ addressing


def test_anonymous_no_credentials_declared():
    """The backend is anonymous — the owner-scoped token API is useless (D44)."""
    assert MendeleyFileSystem.credentials == []


def test_ls_shows_folders_as_directories(fs):
    children = sorted(n[len(CONCEPT) + 1 :] for n in fs.ls(CONCEPT, detail=False))
    assert children == ["StringTie FPKM Gene Data", "Supplemental Data Table"]
    assert all(e["type"] == "directory" for e in fs.ls(CONCEPT))


def test_find_lists_every_file_under_its_folder(fs):
    files = sorted(fs.find(CONCEPT))
    assert files == [
        f"{CONCEPT}/StringTie FPKM Gene Data/SCANB.9206.genematrix_noNeg.txt",
        f"{CONCEPT}/Supplemental Data Table/README.txt",
    ]


def test_ls_into_a_folder(fs):
    sub = f"{CONCEPT}/Supplemental Data Table"
    assert fs.ls(sub, detail=False) == [f"{sub}/README.txt"]


def test_split_path_keeps_folder_and_file(fs):
    assert MendeleyFileSystem.split_path(
        "10.17632/yzxtxn4nmd/4/Supplemental Data Table/README.txt"
    ) == ("10.17632/yzxtxn4nmd/4", "Supplemental Data Table/README.txt")


def test_bad_spec_is_explained(fs):
    with pytest.raises(ArchiveNotFoundError):
        fs.ls("10.17632")


def test_no_listable_root(fs):
    with pytest.raises(NotSupportedError):
        fs.ls("")


# ------------------------------------------------------------------------ info


def test_info_is_sha256_not_md5(fs, fake_mendeley):
    info = fs.info(f"{CONCEPT}/Supplemental Data Table/README.txt")
    assert info["size"] == len(README)
    assert info["md5"] is None  # Mendeley publishes no md5 (D44)
    assert info["checksum"] == hashlib.sha256(README).hexdigest()
    assert info["checksum_type"] == "SHA256"
    assert info["version_id"] == info["checksum"]  # sha256 is the immutable token
    assert info["archive_version"] == VERSION_DOI
    assert fake_mendeley.bytes_served == 0  # listing is download-free


def test_concept_and_version_accession(fs):
    assert fs.concept(CONCEPT) == f"mendeley:{CONCEPT_DOI}"
    assert fs.version_accession(CONCEPT) == f"{CONCEPT_DOI}/4"


def test_versions(fs):
    assert [v["version"] for v in fs.versions(CONCEPT)] == [4, 3]


# -------------------------------------------------------------------- transfer


def test_a_browser_user_agent_is_sent(fs, fake_mendeley):
    """Cloudflare 403s the default client UA, so the session must override it."""
    fs.ls(CONCEPT)
    assert fake_mendeley.saw_user_agent


def test_cat_file_and_ranged_reads(fs):
    path = f"{CONCEPT}/StringTie FPKM Gene Data/SCANB.9206.genematrix_noNeg.txt"
    assert fs.cat_file(path) == GENE
    assert fs.cat_file(path, start=10, end=25) == GENE[10:25]


def test_get_file_verifies_sha256(fs, tmp_path):
    dest = tmp_path / "readme.txt"
    fs.get_file(f"{CONCEPT}/Supplemental Data Table/README.txt", str(dest))
    assert dest.read_bytes() == README


def test_get_file_refuses_and_deletes_bytes_that_fail_sha256(fs, tmp_path, monkeypatch):
    real = fs._fetch_range
    monkeypatch.setattr(
        fs,
        "_fetch_range",
        lambda info, s, e: (lambda b: b[:-1] + bytes([b[-1] ^ 0x01]) if b else b)(
            real(info, s, e)
        ),
    )
    dest = tmp_path / "readme.txt"
    with pytest.raises(VerificationError):
        fs.get_file(f"{CONCEPT}/Supplemental Data Table/README.txt", str(dest))
    assert not dest.exists()
