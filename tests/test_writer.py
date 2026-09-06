"""The writer half of ``.chiro`` support — ``archivefs manifest``.

Two archives, two paths. Zenodo publishes an md5 per file, so a manifest is built
**without downloading a byte** (``bytes_served`` stays at zero). GEO publishes no
digest at all, so the download-free path must *refuse* — and ``--download`` is the
only way to a real checksum, by reading and hashing the bytes.

As in ``test_chiro.py``, the manifests are parsed back through the real
``chirograph`` package rather than a fixture we invented, so the round-trip is
genuine.
"""

from __future__ import annotations

import pytest
from tests.fake_http import FakeRequestsModule
from tests.test_chiro import BODIES, GSE, FakeGEO, md5
from tests.test_zenodo import CSV_BYTES, RECID, ZIP_BYTES, FakeZenodo

from archivefs import base as base_mod
from archivefs.backends.geo import GEOFileSystem
from archivefs.backends.zenodo import ZenodoFileSystem
from archivefs.errors import ManifestError
from archivefs.writer import build_manifest

chirograph = pytest.importorskip("chirograph")


# ------------------------------------------------------------ download-free path


@pytest.fixture
def zenodo(monkeypatch):
    server = FakeZenodo()
    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
    monkeypatch.delenv("ZENODO_TOKEN", raising=False)
    return server


def test_download_free_manifest_over_zenodo(zenodo):
    fs = ZenodoFileSystem(skip_instance_cache=True)
    manifest = build_manifest(fs, RECID)

    # Not a single content byte was fetched: the listing already carried the md5s.
    assert zenodo.bytes_served == 0
    by_relpath = {e.relpath: e for e in manifest.files}
    assert by_relpath["archive.zip"].md5 == md5(ZIP_BYTES)
    assert by_relpath["samples.csv"].md5 == md5(CSV_BYTES)
    assert by_relpath["samples.csv"].size == len(CSV_BYTES)
    # Observed, not vouched-for: we read the archive, we did not deposit it.
    assert manifest.provenance_type == chirograph.PROVENANCE_OBSERVED
    assert manifest.record_accession == RECID

    # Round-trips through chirograph unchanged — body_hash is stable across a
    # serialise/parse cycle, which is the whole point of a content-addressed pin.
    reloaded = chirograph.loads(chirograph.dumps(manifest))
    assert reloaded.body_hash == manifest.body_hash


def test_self_deposited_flag_sets_provenance(zenodo):
    fs = ZenodoFileSystem(skip_instance_cache=True)
    manifest = build_manifest(fs, RECID, self_deposited=True)
    assert manifest.provenance_type == chirograph.PROVENANCE_SELF


# ------------------------------------------------------------- the capture path


@pytest.fixture
def geo(monkeypatch):
    def make(**kwargs):
        server = FakeGEO(**kwargs)
        monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
        return server

    return make


def test_manifest_refuses_when_no_checksum_is_published(geo):
    geo()  # no manifest: GEO's native listing, which carries no md5
    fs = GEOFileSystem(skip_instance_cache=True)
    with pytest.raises(ManifestError) as excinfo:
        build_manifest(fs, GSE)
    message = str(excinfo.value)
    assert "publish no checksum" in message
    assert "--download" in message


def test_download_captures_checksums_for_a_checksumless_archive(geo):
    server = geo()
    fs = GEOFileSystem(skip_instance_cache=True)
    manifest = build_manifest(fs, GSE, download=True)

    # Reading the bytes is the unavoidable cost, so bytes were served this time...
    assert server.bytes_served > 0
    # ...and the resulting digests are the real md5 of each file.
    # GEO's native listing keeps the flat deposited names, so relpath is the name.
    by_relpath = {e.relpath: e for e in manifest.files}
    for deposited_name, body in BODIES.items():
        assert by_relpath[deposited_name].md5 == md5(body)
        assert by_relpath[deposited_name].size == len(body)
    reloaded = chirograph.loads(chirograph.dumps(manifest))
    assert reloaded.body_hash == manifest.body_hash


# --------------------------------------------------------------------- the CLI


def test_cli_manifest_writes_a_file(zenodo, tmp_path, capsys):
    from archivefs.cli import main

    out = tmp_path / "record.chiro.json"
    rc = main(["manifest", f"zenodo://{RECID}", "--out", str(out)])
    assert rc == 0
    assert out.is_file()
    manifest = chirograph.load(out)
    assert {e.relpath for e in manifest.files} == {"archive.zip", "samples.csv"}
    # The summary goes to stderr so a piped document on stdout stays clean.
    assert "chiro://" in capsys.readouterr().err


def test_cli_manifest_to_stdout_is_a_clean_document(zenodo, capsys):
    from archivefs.cli import main

    rc = main(["manifest", f"zenodo://{RECID}", "--out", "-"])
    assert rc == 0
    captured = capsys.readouterr()
    manifest = chirograph.loads(captured.out.encode())
    assert manifest.nfiles == 2
    # stderr carries the human summary, stdout only the JSON.
    assert "chiro://" in captured.err
    assert captured.out.lstrip().startswith("{")
