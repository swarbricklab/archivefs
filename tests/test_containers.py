"""Schema-2.0 container support — the ``containers`` block, ``crc32``, and ``confirm``.

A Zenodo ZIP is the driving case (issue #37): its members have no published md5, but
its central directory exposes each member's ``crc32``, and Zenodo publishes the whole
ZIP's md5. A schema-2.0 manifest records both — a ``containers`` entry per ZIP and a
``crc32`` per member — which is what lets ``archivefs confirm`` cross-check a deposit
against the manifest **without downloading member bytes**.

Exercised against the Zenodo fake, whose ``archive.zip`` is a real in-memory ZIP, so
the crc32s and container md5 are genuine, not invented. The manifests round-trip through
the real ``chirograph`` package (its `2.0` read-validation is the check that the emitted
shape is legal).
"""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest
from tests.fake_http import FakeRequestsModule
from tests.test_zenodo import CSV_BYTES, RECID, ZIP_BYTES, FakeZenodo

from archivefs import base as base_mod
from archivefs.backends.zenodo import ZenodoFileSystem
from archivefs.writer import build_manifest

chirograph = pytest.importorskip("chirograph")


#: The ZIP's members and their true crc32 (hex), read from its central directory — the
#: same coordinate the writer must record and ``confirm`` must re-check.
def _zip_crcs() -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(ZIP_BYTES)) as zf:
        return {i.filename: f"{i.CRC:08x}" for i in zf.infolist() if not i.is_dir()}


ZIP_CRCS = _zip_crcs()
ZIP_MD5 = hashlib.md5(ZIP_BYTES).hexdigest()  # noqa: S324


@pytest.fixture
def zenodo(monkeypatch):
    """Install the Zenodo fake globally, so a *delegate* Zenodo fs also uses it."""
    server = FakeZenodo()
    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
    monkeypatch.delenv("ZENODO_TOKEN", raising=False)
    return server


@pytest.fixture
def container_manifest(zenodo, tmp_path):
    """A real schema-2.0 manifest over the fake's ZIP, written to disk.

    ``--download`` is required: a ZIP member has no published md5, so the writer must
    read and hash it. The container md5 and every crc32 come free from the metadata.
    """
    fs = ZenodoFileSystem(extract_zips=True, skip_instance_cache=True)
    manifest = build_manifest(fs, RECID, download=True)
    path = tmp_path / f"{RECID}.chiro.json"
    path.write_text(chirograph.dumps(manifest))
    return manifest, path


# --------------------------------------------------------------------- the writer


def test_zip_members_get_a_container_and_a_crc32(container_manifest):
    manifest, _ = container_manifest

    # A containers block tips the manifest to 2.0; its one entry is the ZIP.
    assert manifest.schema_version == "2.0"
    assert len(manifest.containers) == 1
    container = manifest.containers[0]
    assert container.source_root == "archive.zip"
    assert container.format == "zip"
    assert container.md5 == ZIP_MD5
    assert container.size == len(ZIP_BYTES)
    # zip → the download-free confirm class.
    assert container.confirm_class == "indexed"

    by_relpath = {e.relpath: e for e in manifest.files}
    # Each ZIP member carries its crc32 (from the central directory) and a source_path
    # that sits under the container's source_root — the linkage the format requires.
    for interior, crc in ZIP_CRCS.items():
        entry = by_relpath[f"archive.zip/{interior}"]
        assert entry.crc32 == crc
        assert entry.source_path == f"archive.zip/{interior}"
        assert entry.source_path.startswith("archive.zip/")
        # The member still carries its own authoritative md5 (from the download).
        assert len(entry.md5) == 32


def test_a_loose_file_stays_a_plain_entry(container_manifest):
    manifest, _ = container_manifest
    # samples.csv is not in the ZIP: no crc32, no source_path, under no container.
    loose = {e.relpath: e for e in manifest.files}["samples.csv"]
    assert loose.crc32 is None
    assert loose.source_path is None
    assert loose.md5 == hashlib.md5(CSV_BYTES).hexdigest()  # noqa: S324


def test_container_fields_do_not_change_the_dvc_dir_hash(container_manifest):
    """The .dir hash projects to {md5, relpath}; crc32/containers must not perturb it.

    This is what lets an existing extract_zips import be re-pinned to chiro:// with no
    cache miss — the DVC identity is byte-identical.
    """
    manifest, _ = container_manifest
    stripped = chirograph.Manifest(
        files=[
            chirograph.FileEntry(relpath=e.relpath, md5=e.md5, size=e.size)
            for e in manifest.files
        ],
        record_accession=manifest.record_accession,
    )
    assert manifest.dvc_dir_hash() == stripped.dvc_dir_hash()


def test_the_manifest_round_trips_through_chirograph(container_manifest):
    manifest, _ = container_manifest
    reloaded = chirograph.loads(chirograph.dumps(manifest))
    assert reloaded.body_hash == manifest.body_hash
    assert len(reloaded.containers) == 1


# -------------------------------------------------------------------- confirm


def test_confirm_matches_a_clean_deposit_download_free(container_manifest, zenodo):
    from archivefs.confirm import confirm

    _, path = container_manifest
    before = zenodo.bytes_served
    report = confirm(str(path))

    assert report.ok
    assert report.download_free
    # No member payload was fetched — only metadata / central-directory reads. (A
    # ZIP central-directory read touches a little of the ZIP tail, so allow a small
    # budget rather than exactly zero.)
    assert zenodo.bytes_served - before < len(ZIP_BYTES) / 4
    # The container md5 was confirmed, and every member matched via crc32.
    assert all(c.ok for c in report.containers)
    assert all(m.status == "matched" for m in report.members)
    vias = {m.via for m in report.members if m.relpath.startswith("archive.zip/")}
    assert vias == {"crc32"}


def test_confirm_localises_a_crc_mismatch(container_manifest, zenodo, tmp_path):
    from archivefs.confirm import confirm

    manifest, _ = container_manifest
    victim = next(e for e in manifest.files if e.crc32 is not None)
    tampered = chirograph.Manifest(
        files=[
            chirograph.FileEntry(
                relpath=e.relpath, md5=e.md5, size=e.size,
                source_path=e.source_path,
                crc32=("deadbeef" if e is victim else e.crc32),
            )
            for e in manifest.files
        ],
        containers=list(manifest.containers),
        concept=manifest.concept,
        record_accession=manifest.record_accession,
    )
    path = tmp_path / "tampered.chiro.json"
    path.write_text(chirograph.dumps(tampered))

    report = confirm(str(path))
    assert not report.ok
    bad = [m for m in report.members if not m.ok]
    assert len(bad) == 1
    assert bad[0].relpath == victim.relpath
    assert bad[0].status == "crc32"


def test_confirm_flags_a_missing_promised_member(container_manifest, zenodo, tmp_path):
    from archivefs.confirm import confirm

    manifest, _ = container_manifest
    phantom = chirograph.FileEntry(
        relpath="archive.zip/counts/ghost.mtx", md5="0" * 32, size=1,
        source_path="archive.zip/counts/ghost.mtx", crc32="00000000",
    )
    tampered = chirograph.Manifest(
        files=[*manifest.files, phantom],
        containers=list(manifest.containers),
        concept=manifest.concept,
        record_accession=manifest.record_accession,
    )
    path = tmp_path / "phantom.chiro.json"
    path.write_text(chirograph.dumps(tampered))

    report = confirm(str(path))
    assert not report.ok
    missing = [m for m in report.members if m.status == "missing"]
    assert [m.relpath for m in missing] == ["archive.zip/counts/ghost.mtx"]


def test_confirm_cli_exit_codes(container_manifest, zenodo, tmp_path, capsys):
    from archivefs.cli import main

    _, path = container_manifest
    assert main(["confirm", str(path)]) == 0
    assert "matches the manifest" in capsys.readouterr().out


# -------------------------------------------------- chiro:// read round-trip


def test_chiro_url_reads_a_container_manifest(container_manifest, zenodo):
    """A chiro:// pin over a ZIP resolves, expands the members, and verifies a read.

    The delegate must auto-enable ZIP expansion (its native listing has to produce the
    per-member paths the manifest's source_path references), and a member read is
    verified against the manifest md5 — Zenodo publishes none for a member.
    """
    from archivefs.backends.chiro import ChiroFileSystem

    _, path = container_manifest
    fs = ChiroFileSystem(skip_instance_cache=True)

    listed = sorted(fs.find(str(path)))
    expected = [f"{path}/archive.zip/{i}" for i in ZIP_CRCS] + [f"{path}/samples.csv"]
    assert listed == sorted(expected)

    # Read one member back through the delegate; the bytes match the ZIP interior.
    with zipfile.ZipFile(io.BytesIO(ZIP_BYTES)) as zf:
        interior = next(iter(ZIP_CRCS))
        expected = zf.read(interior)
    assert fs.cat_file(f"{path}/archive.zip/{interior}") == expected
