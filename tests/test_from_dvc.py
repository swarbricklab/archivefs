"""``archivefs manifest --from-dvc`` — pin a DVC import by reusing its checksums.

DVC hashed every file when it imported a deposit; a ZIP member has no *published* md5,
so pinning it would otherwise mean re-reading the whole archive. This reuses DVC's md5s
(from the ``.dvc``'s ``files:`` list) and lets the archive listing supply everything else
— so the manifest is byte-for-byte the one ``--download`` builds, with no download. The
fake serves a real ZIP, so the reused md5s are the genuine extracted-member hashes.
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
import yaml
from tests.fake_http import FakeRequestsModule
from tests.test_zenodo import RECID, ZIP_BYTES, FakeZenodo

from archivefs import base as base_mod
from archivefs.backends.zenodo import ZenodoFileSystem
from archivefs.errors import ManifestError
from archivefs.from_dvc import read_dvc_import
from archivefs.writer import build_manifest

chirograph = pytest.importorskip("chirograph")


def _member_md5s() -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(ZIP_BYTES)) as zf:
        return {
            i.filename: hashlib.md5(zf.read(i.filename)).hexdigest()  # noqa: S324
            for i in zf.infolist()
            if not i.is_dir()
        }


def _write_import_dvc(path, *, url: str, checksums: dict[str, str], out: str = "archive"):
    """Write a minimal `dvc import-url` .dvc with an inline files list."""
    doc = {
        "deps": [
            {
                "path": url,
                "fs_config": {"extract_zips": "true"},
                "files": [
                    {"relpath": rel, "md5": md5, "size": 1}
                    for rel, md5 in checksums.items()
                ],
            }
        ],
        "outs": [{"path": out, "md5": "abc.dir"}],
    }
    path.write_text(yaml.safe_dump(doc))
    return path


@pytest.fixture
def zenodo(monkeypatch):
    server = FakeZenodo()
    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
    monkeypatch.delenv("ZENODO_TOKEN", raising=False)
    return server


# ------------------------------------------------------------------- parsing


def test_read_dvc_import_pulls_url_checksums_and_fs_config(tmp_path):
    md5s = _member_md5s()
    dvc = _write_import_dvc(
        tmp_path / "archive.dvc",
        url=f"zenodo://{RECID}/archive.zip",
        checksums=md5s,
    )
    source = read_dvc_import(dvc)
    assert source.url == f"zenodo://{RECID}/archive.zip"
    assert source.checksums == md5s
    # fs_config carries the import's options — `extract_zips` in particular.
    assert source.fs_config == {"extract_zips": "true"}


def test_read_dvc_import_rejects_a_plain_add(tmp_path):
    # A `dvc add` has outs but no dependency URL — nothing to route a chiro:// pin to.
    dvc = tmp_path / "local.dvc"
    dvc.write_text(yaml.safe_dump({"outs": [{"path": "data", "md5": "x.dir"}]}))
    with pytest.raises(ManifestError, match="not an import"):
        read_dvc_import(dvc)


# ---------------------------------------------------- equals the download build


def test_from_dvc_equals_the_download_build(zenodo, tmp_path):
    """The reused-checksum manifest is byte-identical to the downloaded one."""
    md5s = _member_md5s()

    fs = ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)
    downloaded = build_manifest(fs, f"{RECID}/archive.zip", download=True)
    assert zenodo.bytes_served > 0  # the download path read the ZIP

    served_before = zenodo.bytes_served
    from_dvc = build_manifest(
        fs, f"{RECID}/archive.zip", known_checksums=md5s
    )
    # No member bytes read this time — only metadata.
    assert zenodo.bytes_served - served_before < len(ZIP_BYTES) / 4

    assert from_dvc.body_hash == downloaded.body_hash
    assert from_dvc.dvc_dir_hash() == downloaded.dvc_dir_hash()
    assert [c.source_root for c in from_dvc.containers] == [
        c.source_root for c in downloaded.containers
    ]


def test_download_free_without_checksums_still_fails(zenodo):
    """Without checksums to reuse, a ZIP member manifest still refuses (needs bytes)."""
    fs = ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)
    with pytest.raises(ManifestError, match="--from-dvc"):
        build_manifest(fs, f"{RECID}/archive.zip")  # no download, no known_checksums


# --------------------------------------------------------------------- the CLI


def test_cli_manifest_from_dvc(zenodo, tmp_path, capsys):
    from archivefs.cli import main

    md5s = _member_md5s()
    dvc = _write_import_dvc(
        tmp_path / "archive.dvc",
        url=f"zenodo://{RECID}/archive.zip",
        checksums=md5s,
    )
    out = tmp_path / "pin.chiro.json"
    rc = main(["manifest", "--from-dvc", str(dvc), "--out", str(out)])
    assert rc == 0

    manifest = chirograph.load(out)
    assert manifest.schema_version == "2.0"
    assert {e.relpath for e in manifest.files} == set(md5s)
    assert {e.md5 for e in manifest.files} == set(md5s.values())
    assert "chiro://" in capsys.readouterr().err


def test_cli_manifest_needs_a_url_or_from_dvc(capsys):
    from archivefs.cli import main

    assert main(["manifest"]) == 2
    assert "--from-dvc" in capsys.readouterr().err
