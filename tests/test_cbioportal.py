"""cBioPortal backend tests (EXPERIMENTAL backend, issue #47 / D44).

Run offline. The fake mirrors the three live surfaces the backend uses: the GitHub
contents API (directory listing, recursing a subdir), ``raw.githubusercontent`` (an
LFS *pointer* for data files, real content for small plain files), and
``media.githubusercontent`` (the LFS payload, range-capable).
"""

from __future__ import annotations

import hashlib

import pytest
from tests.fake_http import FakeRequestsModule, FakeResponse, ranged

from archivefs import base as base_mod
from archivefs.backends.cbioportal import CBioPortalFileSystem
from archivefs.errors import ArchiveNotFoundError, NotSupportedError, VerificationError

STUDY = "brca_metabric"
REPO = "cBioPortal/datahub"

# an LFS-tracked data file: raw serves a pointer, media serves these bytes
BIG = b"Hugo_Symbol\tEntrez\tS1\tS2\n" + b"".join(
    f"G{i}\t{i}\t1.0\t2.0\n".encode() for i in range(400)
)
BIG_SHA = hashlib.sha256(BIG).hexdigest()
LFS_POINTER = (
    b"version https://git-lfs.github.com/spec/v1\n"
    b"oid sha256:" + BIG_SHA.encode() + b"\n"
    b"size " + str(len(BIG)).encode() + b"\n"
)
# a small plain (non-LFS) file: raw serves the content directly
META = b"type_of_cancer: brca\ncancer_study_identifier: brca_metabric\n"
CASES = b"case_list_name: All\ncase_list_ids: TCGA-1\tTCGA-2\n"

RAW = "https://raw.githubusercontent.com/cBioPortal/datahub/master/public/brca_metabric"


def entry(name, path, size, download_url, type_="file"):
    return {"name": name, "path": path, "size": size, "type": type_,
            "download_url": download_url}


# GitHub contents API, keyed by the repo path requested
CONTENTS = {
    "public/brca_metabric": [
        entry("data_mrna.txt", "public/brca_metabric/data_mrna.txt", 130,
              f"{RAW}/data_mrna.txt"),
        entry("meta_study.txt", "public/brca_metabric/meta_study.txt", len(META),
              f"{RAW}/meta_study.txt"),
        entry("case_lists", "public/brca_metabric/case_lists", 0, None, "dir"),
    ],
    "public/brca_metabric/case_lists": [
        entry("cases_all.txt", "public/brca_metabric/case_lists/cases_all.txt",
              len(CASES), f"{RAW}/case_lists/cases_all.txt"),
    ],
}
# raw bodies (LFS files -> pointer; plain files -> content)
RAW_BODIES = {
    f"{RAW}/data_mrna.txt": LFS_POINTER,
    f"{RAW}/meta_study.txt": META,
    f"{RAW}/case_lists/cases_all.txt": CASES,
}
MEDIA = "https://media.githubusercontent.com/media/cBioPortal/datahub/master/public/brca_metabric"


class FakeGitHub:
    def __init__(self) -> None:
        self.bytes_served = 0
        self.saw_user_agent = False

    def __call__(self, method: str, url: str, headers: dict, kwargs: dict):
        if "archivefs" in headers.get("User-Agent", ""):
            self.saw_user_agent = True
        # LFS media endpoint (ranged payload)
        if url.startswith(MEDIA):
            resp = ranged(BIG, headers)
            self.bytes_served += len(resp.content)
            return resp
        # raw.githubusercontent
        if url in RAW_BODIES:
            return ranged(RAW_BODIES[url], headers)
        # GitHub contents API
        if "/contents/" in url:
            path = url.split("/contents/")[1].split("?")[0]
            from urllib.parse import unquote
            path = unquote(path)
            if path in CONTENTS:
                return FakeResponse(200, json=CONTENTS[path])
            return FakeResponse(404, json={"message": "Not Found"})
        return FakeResponse(404, json={"message": "Not Found"})


@pytest.fixture
def fake_github(monkeypatch):
    server = FakeGitHub()
    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    return server


@pytest.fixture
def fs(fake_github):
    return CBioPortalFileSystem(skip_instance_cache=True)


# ------------------------------------------------------------------ addressing


def test_find_lists_files_recursing_subdirs(fs):
    assert sorted(fs.find(STUDY)) == [
        f"{STUDY}/case_lists/cases_all.txt",
        f"{STUDY}/data_mrna.txt",
        f"{STUDY}/meta_study.txt",
    ]


def test_ls_shows_the_subdir_as_a_directory(fs):
    entries = {e["name"]: e["type"] for e in fs.ls(STUDY)}
    assert entries[f"{STUDY}/case_lists"] == "directory"
    assert entries[f"{STUDY}/data_mrna.txt"] == "file"


def test_unknown_study_is_explained(fs):
    with pytest.raises(ArchiveNotFoundError) as exc:
        fs.ls("no_such_study")
    assert "no_such_study" in str(exc.value)


def test_no_listable_root(fs):
    with pytest.raises(NotSupportedError):
        fs.ls("")


# ------------------------------------------------------------------------ info


def test_lfs_file_has_download_free_sha256_and_real_size(fs, fake_github):
    info = fs.info(f"{STUDY}/data_mrna.txt")
    assert info["size"] == len(BIG)  # the pointer's size, not the 130-byte pointer
    assert info["md5"] is None
    assert info["checksum"] == BIG_SHA
    assert info["version_id"] == BIG_SHA
    assert info["cbioportal"]["git_lfs"] is True
    # the payload was never fetched — only the pointer
    assert fake_github.bytes_served == 0


def test_small_plain_file_gets_a_computed_sha256(fs):
    info = fs.info(f"{STUDY}/meta_study.txt")
    assert info["cbioportal"]["git_lfs"] is False
    assert info["checksum"] == hashlib.sha256(META).hexdigest()
    assert info["size"] == len(META)


# -------------------------------------------------------------------- transfer


def test_a_github_user_agent_is_sent(fs, fake_github):
    fs.find(STUDY)
    assert fake_github.saw_user_agent


def test_lfs_payload_reads_from_media_with_ranges(fs):
    path = f"{STUDY}/data_mrna.txt"
    assert fs.cat_file(path) == BIG
    assert fs.cat_file(path, start=0, end=11) == BIG[:11]


def test_plain_file_bytes(fs):
    assert fs.cat_file(f"{STUDY}/meta_study.txt") == META


def test_get_file_verifies_sha256(fs, tmp_path):
    dest = tmp_path / "mrna.txt"
    fs.get_file(f"{STUDY}/data_mrna.txt", str(dest))
    assert dest.read_bytes() == BIG


def test_get_file_refuses_bytes_that_fail_sha256(fs, tmp_path, monkeypatch):
    real = fs._fetch_range
    monkeypatch.setattr(
        fs,
        "_fetch_range",
        lambda info, s, e: (lambda b: b[:-1] + bytes([b[-1] ^ 0x01]) if b else b)(
            real(info, s, e)
        ),
    )
    dest = tmp_path / "mrna.txt"
    with pytest.raises(VerificationError):
        fs.get_file(f"{STUDY}/data_mrna.txt", str(dest))
    assert not dest.exists()


def test_lfs_pointer_parsing():
    fields = CBioPortalFileSystem._parse_lfs_pointer(LFS_POINTER)
    assert fields == {"sha256": BIG_SHA, "size": len(BIG)}
