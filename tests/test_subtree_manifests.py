"""Pinning a *subtree* of a record, not the whole thing — `bia://ACC/images/ihc`.

The real case (P08): the imports are subdirectories of a large study, each its own
`.dvc`. `archivefs manifest bia://S-BIAD.../images/ihc` must pin exactly that subtree
— list only it, scope the concept to it, and make `chiro://` fetch only it. Found by
using it on a real repo, where addressing a subtree wrongly manifested the whole study.
"""

from __future__ import annotations

import hashlib

import pytest
from tests.fake_http import FakeRequestsModule
from tests.test_bia import ACC, BODIES, FakeBIA

from archivefs import base as base_mod
from archivefs.backends.bia import BIAFileSystem
from archivefs.backends.chiro import ChiroFileSystem
from archivefs.errors import VerificationError
from archivefs.writer import build_manifest

chirograph = pytest.importorskip("chirograph")


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()  # noqa: S324


# Submitted rows carrying a depositor md5 column (D33), so a manifest is download-free.
ROWS = [
    {
        "path": path,
        "Name": path.rsplit("/", 1)[-1],
        "Size": len(body),
        "type": "file",
        "isDirectory": "false",
        "md5": _md5(body),
    }
    for path, body in BODIES.items()  # images/plate1/a.tif, .../b.tif, images/plate2/a.tif
]


@pytest.fixture
def bia(monkeypatch):
    server = FakeBIA(rows=ROWS)
    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
    return server


def test_writer_pins_only_the_addressed_subtree(bia):
    fs = BIAFileSystem(skip_instance_cache=True)
    manifest = build_manifest(fs, f"{ACC}/images/plate1")

    # Only plate1's files, and their paths are relative to the subtree, not the study.
    assert {e.relpath for e in manifest.files} == {"a.tif", "b.tif"}
    # The concept and version-pin are scoped to the subtree, so `update` re-resolves
    # exactly it and `chiro://` fetches exactly it.
    assert manifest.concept == f"biostudies:{ACC}/images/plate1"
    assert manifest.record_accession == f"{ACC}/images/plate1"


def test_chiro_lists_and_reads_a_subtree_pin(bia, tmp_path):
    src = BIAFileSystem(skip_instance_cache=True)
    manifest = build_manifest(src, f"{ACC}/images/plate1")
    path = tmp_path / "plate1.chiro.json"
    path.write_text(chirograph.dumps(manifest))

    fs = ChiroFileSystem(skip_instance_cache=True)
    assert sorted(fs.find(str(path))) == [f"{path}/a.tif", f"{path}/b.tif"]
    # Reads route back through BIA to the subtree path and verify against the manifest.
    assert fs.cat_file(f"{path}/a.tif") == BODIES["images/plate1/a.tif"]

    # The delegate is scoped to the subtree accession, not the whole study.
    _delegate, accession = fs._delegates[str(path)]
    assert accession == f"{ACC}/images/plate1"


def test_a_subtree_pin_still_verifies(bia, tmp_path):
    src = BIAFileSystem(skip_instance_cache=True)
    manifest = build_manifest(src, f"{ACC}/images/plate1")
    path = tmp_path / "plate1.chiro.json"
    path.write_text(chirograph.dumps(manifest))

    # Serve wrong bytes for one file; verify-on-read at the chiro:// layer must refuse
    # (the delegate reads natively and has the depositor md5 only via the manifest).
    original = BODIES["images/plate1/a.tif"]
    try:
        BODIES["images/plate1/a.tif"] = b"MM\x00*" + b"Z" * 1020
        fs = ChiroFileSystem(skip_instance_cache=True)
        with pytest.raises(VerificationError):
            fs.cat_file(f"{path}/a.tif")
    finally:
        BODIES["images/plate1/a.tif"] = original
