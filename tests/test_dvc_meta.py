"""`archivefs dvc-meta` — stamp provenance into a chiro:// pin's .dvc, from its manifest.

Fully offline: builds a manifest, writes it to a store, drops a `chiro://md5:` `.dvc` beside
it, and checks the derived `meta.archivefs` block — no archive, no network.
"""

from __future__ import annotations

import pytest
import yaml

from archivefs import store
from archivefs.dvc_meta import provenance_meta, stamp
from archivefs.errors import ManifestError

chirograph = pytest.importorskip("chirograph")

M = "a" * 32


def _zenodo_manifest():
    return chirograph.Manifest(
        files=[
            chirograph.FileEntry(
                relpath="archive.zip/x.csv", md5=M, size=1,
                source_path="archive.zip/x.csv", crc32="00000001",
            )
        ],
        containers=[
            chirograph.Container(source_root="archive.zip", format="zip", md5="b" * 32)
        ],
        concept="doi:10.5281/zenodo.999",
        record_accession="1000",
        source_url="zenodo://10.5281/zenodo.1000/archive.zip",
        generator="test",
    )


def _bia_manifest():
    return chirograph.Manifest(
        files=[chirograph.FileEntry(relpath="P01.tif", md5=M, size=1)],
        concept="biostudies:S-BIAD1/images/ihc",
        record_accession="S-BIAD1/images/ihc",
        generator="test",
    )


def _write_pin(tmp_path, manifest, out="data"):
    """A `chiro://md5:` .dvc beside a store holding `manifest`."""
    (tmp_path / ".chirograph").mkdir(exist_ok=True)
    _dest, ref = store.write_to_store(manifest, start=tmp_path)
    dvc = tmp_path / f"{out}.dvc"
    dvc.write_text(
        yaml.safe_dump(
            {
                "md5": "0" * 32,
                "frozen": True,
                "deps": [{"hash": "md5", "path": str(ref)}],
                "outs": [{"hash": "md5", "path": out}],
            },
            sort_keys=False,
        )
    )
    return dvc


# ------------------------------------------------------------- derivation


def test_zenodo_provenance_fields():
    meta = provenance_meta(_zenodo_manifest())["archivefs"]
    assert meta["archive"] == "zenodo"
    assert meta["record"] == "10.5281/zenodo.1000"      # citable version DOI
    assert meta["concept_doi"] == "10.5281/zenodo.999"
    assert meta["container"] == "archive.zip"
    assert meta["url"] == "https://doi.org/10.5281/zenodo.1000"
    assert meta["source"] == "zenodo://10.5281/zenodo.1000/archive.zip"
    assert meta["nfiles"] == 1
    assert "authoritative" in meta["note"]


def test_bia_provenance_fields():
    meta = provenance_meta(_bia_manifest())["archivefs"]
    assert meta["archive"] == "bia"
    assert meta["study"] == "S-BIAD1"
    assert meta["subtree"] == "images/ihc"
    assert meta["url"] == "https://www.ebi.ac.uk/biostudies/studies/S-BIAD1"
    assert meta["source"] == "bia://S-BIAD1/images/ihc"
    assert "container" not in meta  # loose files, no container


# ----------------------------------------------------------------- stamp


def test_stamp_writes_meta_and_keeps_md5(tmp_path):
    dvc = _write_pin(tmp_path, _zenodo_manifest())
    before = yaml.safe_load(dvc.read_text())["md5"]

    stamp(dvc)

    doc = yaml.safe_load(dvc.read_text())
    assert doc["meta"]["archivefs"]["archive"] == "zenodo"
    assert doc["md5"] == before          # meta is excluded from the stage md5
    # everything else preserved
    assert doc["deps"][0]["path"].startswith("chiro://md5:")
    assert doc["outs"][0]["path"] == "data"


def test_stamp_is_idempotent_and_preserves_user_meta(tmp_path):
    dvc = _write_pin(tmp_path, _zenodo_manifest())
    # a user-authored meta key must survive
    doc = yaml.safe_load(dvc.read_text())
    doc["meta"] = {"owner": "someone"}
    dvc.write_text(yaml.safe_dump(doc, sort_keys=False))

    stamp(dvc)
    first = yaml.safe_load(dvc.read_text())
    stamp(dvc)
    second = yaml.safe_load(dvc.read_text())

    assert first == second                        # idempotent
    assert second["meta"]["owner"] == "someone"   # user key kept
    assert second["meta"]["archivefs"]["archive"] == "zenodo"


def test_stamp_rejects_a_non_chiro_dep(tmp_path):
    dvc = tmp_path / "plain.dvc"
    dvc.write_text(
        yaml.safe_dump(
            {"deps": [{"path": "zenodo://1486184/x.zip"}], "outs": [{"path": "x"}]}
        )
    )
    with pytest.raises(ManifestError, match="no chiro:// dependency"):
        stamp(dvc)


def test_dry_run_does_not_write(tmp_path):
    dvc = _write_pin(tmp_path, _zenodo_manifest())
    original = dvc.read_text()
    block = stamp(dvc, write=False)
    assert block["archivefs"]["archive"] == "zenodo"
    assert dvc.read_text() == original


# ------------------------------------------------------------------ CLI


def test_cli_dvc_meta(tmp_path, capsys):
    from archivefs.cli import main

    dvc = _write_pin(tmp_path, _bia_manifest())
    assert main(["dvc-meta", str(dvc)]) == 0
    assert "bia" in capsys.readouterr().err
    assert yaml.safe_load(dvc.read_text())["meta"]["archivefs"]["study"] == "S-BIAD1"
