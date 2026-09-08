"""``archivefs manifest --from-dvc`` — rebuild a pin from a DVC import, offline.

A DVC import records everything a manifest needs in its ``.dvc``: per-file md5/size/relpath,
and — for a Zenodo ZIP — the ``crc32`` + container md5 in the ``version_id`` overload. So the
manifest is reconstructed with **no archive access** (these tests hit no network and stub no
backend). The reused md5s are DVC's, so the DVC ``.dir`` hash is unchanged — a clean re-pin.
"""

from __future__ import annotations

import hashlib

import pytest
import yaml

from archivefs.errors import ManifestError
from archivefs.from_dvc import manifest_from_dvc

chirograph = pytest.importorskip("chirograph")


def _md5(seed: str) -> str:
    return hashlib.md5(seed.encode()).hexdigest()  # noqa: S324 - test fixture id


def _write(path, doc: dict):
    path.write_text(yaml.safe_dump(doc))
    return path


def _zip_import(tmp_path, members, *, url, out="archive"):
    """A `dvc import-url zenodo://…/archive.zip` .dvc with the version_id overload."""
    container_md5 = _md5("container")
    files = [
        {
            "relpath": rel,
            "md5": _md5(rel),
            "size": size,
            "version_id": f"{container_md5}:{crc}",
        }
        for rel, size, crc in members
    ]
    doc = {
        "frozen": True,
        "deps": [{"path": url, "fs_config": {"extract_zips": "true"}, "files": files}],
        "outs": [{"path": out, "md5": "x.dir"}],
    }
    return _write(tmp_path / "archive.dvc", doc), files, container_md5


# ------------------------------------------------------------- container (ZIP)


def test_from_dvc_rebuilds_a_container_manifest_offline(tmp_path):
    members = [("visium/a.h5ad", 100, "0be89e6a"), ("visium/b.h5ad", 200, "8a979769")]
    dvc, _files, container_md5 = _zip_import(
        tmp_path, members, url="zenodo://10.5281/zenodo.18917100/anndata_objects.zip"
    )

    manifest = manifest_from_dvc(dvc)

    assert manifest.schema_version == "2.0"
    # One container, its md5 lifted from the version_id overload.
    assert len(manifest.containers) == 1
    assert manifest.containers[0].source_root == "anndata_objects.zip"
    assert manifest.containers[0].md5 == container_md5
    assert manifest.containers[0].confirm_class == "indexed"
    # Members: relpath is the interior, crc32 from the overload, source_path prefixed.
    by_relpath = {e.relpath: e for e in manifest.files}
    for rel, _size, crc in members:
        entry = by_relpath[rel]
        assert entry.crc32 == crc
        assert entry.source_path == f"anndata_objects.zip/{rel}"
        assert entry.md5 == _md5(rel)
    # Routing, all derived from the URL — the Zenodo recid and a routable concept.
    assert manifest.record_accession == "18917100"
    assert manifest.concept == "doi:10.5281/zenodo.18917100"


def test_from_dvc_dir_hash_matches_the_member_md5s(tmp_path):
    """The reused md5s give the same DVC .dir hash — that is what makes it a clean swap."""
    members = [("a.txt", 1, "00000001"), ("sub/b.txt", 2, "00000002")]
    dvc, files, _ = _zip_import(
        tmp_path, members, url="zenodo://10.5281/zenodo.42/archive.zip"
    )
    manifest = manifest_from_dvc(dvc)

    expected = chirograph.Manifest(
        files=[
            chirograph.FileEntry(relpath=f["relpath"], md5=f["md5"], size=f["size"])
            for f in files
        ],
        record_accession="x",
    )
    assert manifest.dvc_dir_hash() == expected.dvc_dir_hash()


def test_from_dvc_round_trips_through_chirograph(tmp_path):
    dvc, *_ = _zip_import(
        tmp_path,
        [("x.h5ad", 10, "deadbeef")],
        url="zenodo://10.5281/zenodo.7/archive.zip",
    )
    manifest = manifest_from_dvc(dvc)
    reloaded = chirograph.loads(chirograph.dumps(manifest))  # validates 2.0 on read
    assert reloaded.body_hash == manifest.body_hash


# --------------------------------------------------------------- loose files


def test_from_dvc_loose_import_has_no_container(tmp_path):
    # A non-ZIP import (e.g. BIA/GEO): plain md5s, no version_id overload → loose entries.
    doc = {
        "frozen": True,
        "deps": [
            {
                "path": "bia://S-BIAD3080/images/ihc",
                "files": [
                    {"relpath": "P01.tif", "md5": _md5("P01"), "size": 5},
                    {"relpath": "P02.tif", "md5": _md5("P02"), "size": 6},
                ],
            }
        ],
        "outs": [{"path": "ihc", "md5": "y.dir"}],
    }
    dvc = _write(tmp_path / "ihc.dvc", doc)

    manifest = manifest_from_dvc(dvc)
    assert manifest.containers == []
    assert all(e.crc32 is None and e.source_path is None for e in manifest.files)
    # The subtree accession and its concept, straight from the URL.
    assert manifest.record_accession == "S-BIAD3080/images/ihc"
    assert manifest.concept == "biostudies:S-BIAD3080/images/ihc"


# ------------------------------------------------------------------- errors


def test_from_dvc_rejects_a_plain_add(tmp_path):
    dvc = _write(tmp_path / "local.dvc", {"outs": [{"path": "data", "md5": "z.dir"}]})
    with pytest.raises(ManifestError, match="not an import"):
        manifest_from_dvc(dvc)


def test_from_dvc_missing_file(tmp_path):
    with pytest.raises(ManifestError, match=r"no \.dvc file"):
        manifest_from_dvc(tmp_path / "nope.dvc")


# --------------------------------------------------------------------- the CLI


def test_cli_manifest_from_dvc_offline(tmp_path, capsys):
    from archivefs.cli import main

    members = [("visium/a.h5ad", 100, "0be89e6a")]
    dvc, _files, _ = _zip_import(
        tmp_path, members, url="zenodo://10.5281/zenodo.18917100/anndata_objects.zip"
    )
    out = tmp_path / "pin.chiro.json"
    assert main(["manifest", "--from-dvc", str(dvc), "--out", str(out)]) == 0

    manifest = chirograph.load(out)
    assert manifest.schema_version == "2.0"
    assert {e.relpath for e in manifest.files} == {"visium/a.h5ad"}
    assert "chiro://" in capsys.readouterr().err


def test_cli_manifest_needs_a_url_or_from_dvc(capsys):
    from archivefs.cli import main

    assert main(["manifest"]) == 2
    assert "--from-dvc" in capsys.readouterr().err
