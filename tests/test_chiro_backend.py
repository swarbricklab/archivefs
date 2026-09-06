"""The ``chiro://`` protocol backend — a manifest as a portable, self-verifying pin.

Exercised end-to-end against the GEO fake: a manifest is generated with the writer,
then ``chiro://<manifest>`` lists and reads *through* the GEO backend the manifest's
concept names — so the bytes arrive verified, the auth/cache/verify machinery all
belongs to the delegate, and this layer only does addressing. GEO is the sharp case:
it publishes no checksum, so a manifest is the only thing that makes its files
verifiable at all, and the pin has to route back to GEO to fetch them.
"""

from __future__ import annotations

import pytest
from tests.fake_http import FakeRequestsModule
from tests.test_chiro import BODIES, GSE, FakeGEO, md5

from archivefs import base as base_mod
from archivefs.backends.chiro import ChiroFileSystem
from archivefs.backends.geo import GEOFileSystem
from archivefs.errors import ManifestError, VerificationError
from archivefs.writer import build_manifest

chirograph = pytest.importorskip("chirograph")


@pytest.fixture
def geo(monkeypatch):
    """Install the GEO fake globally, so a *delegate* GEO fs also uses it."""

    def make(**kwargs):
        server = FakeGEO(**kwargs)
        monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
        return server

    return make


@pytest.fixture
def manifest_file(geo, tmp_path):
    """A real manifest over the GEO fake, written to disk — download-free is
    impossible for GEO, so it is captured."""
    geo()
    src = GEOFileSystem(skip_instance_cache=True)
    manifest = build_manifest(src, GSE, download=True)
    path = tmp_path / f"{GSE}.chiro.json"
    path.write_text(chirograph.dumps(manifest))
    return path


def test_lists_the_manifest_view(manifest_file):
    fs = ChiroFileSystem(skip_instance_cache=True)
    listed = sorted(fs.find(f"{manifest_file}"))
    # Every file the manifest pins is addressable under the manifest reference.
    assert listed == [f"{manifest_file}/{name}" for name in sorted(BODIES)]
    info = fs.info(f"{manifest_file}/{next(iter(BODIES))}")
    assert info["md5"] == md5(BODIES[next(iter(BODIES))])


def test_reads_through_the_delegate_backend(manifest_file):
    fs = ChiroFileSystem(skip_instance_cache=True)
    name = next(iter(BODIES))
    # cat_file forwards to GEO and returns the real bytes...
    assert fs.cat_file(f"{manifest_file}/{name}") == BODIES[name]
    # ...and open() streams them the same way.
    with fs.open(f"{manifest_file}/{name}", "rb") as handle:
        assert handle.read() == BODIES[name]


def test_get_file_verifies_via_the_delegate(manifest_file, tmp_path):
    fs = ChiroFileSystem(skip_instance_cache=True)
    name = next(iter(BODIES))
    out = tmp_path / "out.bin"
    fs.get_file(f"{manifest_file}/{name}", str(out))
    assert out.read_bytes() == BODIES[name]


def test_corruption_is_refused_because_the_delegate_verifies(geo, tmp_path):
    # The delegate's verify-on-read (D34) is what protects a chiro:// read. The
    # manifest stays valid (a tampered manifest would fail chirograph's own body_hash
    # check first); instead the *archive* serves wrong bytes, the drift case a pin
    # exists to catch.
    server = geo()
    src = GEOFileSystem(skip_instance_cache=True)
    manifest = build_manifest(src, GSE, download=True)  # md5s of the clean bytes
    path = tmp_path / f"{GSE}.chiro.json"
    path.write_text(chirograph.dumps(manifest))

    name = next(iter(BODIES))
    server.bodies[name] = b"corrupted-not-what-the-manifest-recorded"

    fs = ChiroFileSystem(skip_instance_cache=True)
    with pytest.raises(VerificationError):
        fs.cat_file(f"{path}/{name}")


def test_a_manifest_without_a_concept_cannot_route(tmp_path, geo):
    geo()
    # A pre-1.1 manifest (no concept) cannot say which backend owns its bytes.
    manifest = chirograph.Manifest(
        files=[chirograph.FileEntry(relpath="a.txt", md5="0" * 32, size=1)],
        record_accession=GSE,
    )
    path = tmp_path / f"{GSE}.chiro.json"
    path.write_text(chirograph.dumps(manifest))

    fs = ChiroFileSystem(skip_instance_cache=True)
    with pytest.raises(ManifestError, match="concept"):
        fs.ls(f"{path}")


def test_an_unknown_concept_scheme_is_refused(tmp_path, geo):
    geo()
    manifest = chirograph.Manifest(
        files=[chirograph.FileEntry(relpath="a.txt", md5="0" * 32, size=1)],
        record_accession="x",
        concept="nonsense:x",
    )
    path = tmp_path / "x.chiro.json"
    path.write_text(chirograph.dumps(manifest))

    fs = ChiroFileSystem(skip_instance_cache=True)
    with pytest.raises(ManifestError, match="no archivefs backend"):
        fs.ls(f"{path}")
