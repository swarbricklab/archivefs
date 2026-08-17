"""The ``.chiro``-first resolution layer.

Exercised through the GEO backend, because GEO is the case that gains the most:
it publishes no per-file checksum at all, so a manifest is the difference between
"trust me" and download-free verification.

These tests use the real ``chirograph`` package to *write* the manifests they then
read, so the interop is genuine rather than a fixture we invented.
"""

from __future__ import annotations

import hashlib
import json

import pytest
from tests.fake_http import FakeRequestsModule, FakeResponse, ranged

from archivefs import base as base_mod
from archivefs import chiro as chiro_mod
from archivefs.backends.geo import GEOFileSystem
from archivefs.errors import ManifestError, MissingExtraError

chirograph = pytest.importorskip("chirograph")

GSE = "GSE176078"
SUPPL = "https://ftp.ncbi.nlm.nih.gov/geo/series/GSE176nnn/GSE176078/suppl/"
BODIES = {
    "GSE176078_sampleA_matrix.mtx.gz": b"matrix-A" * 1000,
    "GSE176078_sampleA_barcodes.tsv.gz": b"barcodes-A" * 100,
    "GSE176078_sampleB_matrix.mtx.gz": b"matrix-B" * 1000,
}
MANIFEST_NAME = f"{GSE}.chiro.json"


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def build_manifest(names=None, *, relocate=True, tamper_size=False) -> bytes:
    """A real chirograph manifest over (some of) the fake deposit's files."""
    names = names if names is not None else list(BODIES)
    entries = []
    for name in names:
        body = BODIES.get(name, b"")
        sample, _, leaf = name[len(f"{GSE}_") :].partition("_")
        relpath = f"{sample}/{leaf}" if relocate else name
        entries.append(
            chirograph.FileEntry(
                relpath=relpath,
                md5=md5(body),
                size=len(body) + (1 if tamper_size else 0),
                source_path=name if relpath != name else None,
            )
        )
    manifest = chirograph.Manifest(
        provenance_type=chirograph.PROVENANCE_SELF,
        files=entries,
        record_accession=GSE,
        source_url=SUPPL,
    )
    return manifest.to_bytes()


def index_html(names) -> bytes:
    links = "\n".join(f'<a href="{name}">{name}</a>' for name in names)
    return f"<html><body><pre>{links}</pre></body></html>".encode()


class FakeGEO:
    def __init__(self, manifest: bytes | None = None, extra_manifests=()):
        self.manifest = manifest
        self.extra_manifests = list(extra_manifests)
        self.bodies = dict(BODIES)
        self.bytes_served = 0

    @property
    def names(self) -> list[str]:
        names = list(self.bodies)
        if self.manifest is not None:
            names.append(MANIFEST_NAME)
        names.extend(self.extra_manifests)
        return names

    def __call__(self, method: str, url: str, headers: dict, kwargs: dict):
        if url.endswith("suppl/filelist.txt"):
            return FakeResponse(404, b"absent")
        if url.endswith("/suppl/"):
            return FakeResponse(200, index_html(self.names))
        name = url.rsplit("/", 1)[-1]
        if name == MANIFEST_NAME and self.manifest is not None:
            if method == "HEAD":
                return FakeResponse(
                    200, b"", {"Content-Length": str(len(self.manifest))}
                )
            self.bytes_served += len(self.manifest)
            return ranged(self.manifest, headers)
        if name in self.extra_manifests:
            return ranged(build_manifest([]), headers)
        if name in self.bodies:
            body = self.bodies[name]
            if method == "HEAD":
                return FakeResponse(200, b"", {"Content-Length": str(len(body))})
            self.bytes_served += len(body)
            return ranged(body, headers)
        return FakeResponse(404, b"not found")


@pytest.fixture
def geo(monkeypatch):
    def make(**kwargs):
        server = FakeGEO(**kwargs)
        monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
        return server

    return make


# ------------------------------------------------------------------- discovery


def test_manifest_becomes_the_listing_and_relocates_paths(geo):
    geo(manifest=build_manifest())
    fs = GEOFileSystem(skip_instance_cache=True)
    names = sorted(fs.ls(GSE, detail=False))
    # workspace layout from relpath, not the flat deposited names
    assert names == [
        f"{GSE}/sampleA/barcodes.tsv.gz",
        f"{GSE}/sampleA/matrix.mtx.gz",
        f"{GSE}/sampleB/matrix.mtx.gz",
    ]
    assert fs.manifest_path_for(GSE) == f"{GSE}/{MANIFEST_NAME}"


def test_checksums_are_download_free_where_geo_had_none(geo):
    server = geo(manifest=build_manifest())
    fs = GEOFileSystem(skip_instance_cache=True)
    info = fs.info(f"{GSE}/sampleA/matrix.mtx.gz")
    assert info["md5"] == md5(BODIES["GSE176078_sampleA_matrix.mtx.gz"])
    assert info["size"] == len(BODIES["GSE176078_sampleA_matrix.mtx.gz"])
    assert "download-free" in info["checksum_source"]
    # only the manifest itself was read, no payload
    assert server.bytes_served == len(server.manifest)


def test_info_records_the_two_path_model(geo):
    geo(manifest=build_manifest())
    fs = GEOFileSystem(skip_instance_cache=True)
    block = fs.info(f"{GSE}/sampleA/matrix.mtx.gz")["chiro"]
    assert block["relpath"] == "sampleA/matrix.mtx.gz"
    assert block["source_path"] == "GSE176078_sampleA_matrix.mtx.gz"
    assert block["relocated"] is True
    assert block["provenance_type"] == "self-deposited"
    assert block["record_accession"] == GSE
    assert block["body_hash"]


def test_bytes_are_fetched_from_source_path_and_match(geo):
    geo(manifest=build_manifest())
    fs = GEOFileSystem(skip_instance_cache=True)
    data = fs.cat_file(f"{GSE}/sampleA/matrix.mtx.gz")
    assert data == BODIES["GSE176078_sampleA_matrix.mtx.gz"]
    assert md5(data) == fs.info(f"{GSE}/sampleA/matrix.mtx.gz")["md5"]


def test_terse_manifest_without_relocation(geo):
    geo(manifest=build_manifest(relocate=False))
    fs = GEOFileSystem(skip_instance_cache=True)
    names = sorted(fs.ls(GSE, detail=False))
    assert names == sorted(f"{GSE}/{n}" for n in BODIES)
    assert fs.info(f"{GSE}/{next(iter(BODIES))}")["chiro"]["relocated"] is False


def test_no_manifest_means_native_listing(geo):
    geo(manifest=None)
    fs = GEOFileSystem(skip_instance_cache=True)
    assert sorted(fs.ls(GSE, detail=False)) == sorted(f"{GSE}/{n}" for n in BODIES)
    assert fs.manifest_path_for(GSE) is None
    assert fs.info(f"{GSE}/{next(iter(BODIES))}")["md5"] is None


def test_manifests_can_be_switched_off(geo):
    geo(manifest=build_manifest())
    fs = GEOFileSystem(chiro=False, skip_instance_cache=True)
    names = sorted(fs.ls(GSE, detail=False))
    assert f"{GSE}/{MANIFEST_NAME}" in names  # just another file
    assert fs.manifest_path_for(GSE) is None


def test_explicit_manifest_path(geo):
    geo(manifest=build_manifest())
    fs = GEOFileSystem(chiro=f"{GSE}/{MANIFEST_NAME}", skip_instance_cache=True)
    assert f"{GSE}/sampleA/matrix.mtx.gz" in fs.ls(GSE, detail=False)


def test_ambiguous_discovery_refuses_to_guess(geo):
    geo(manifest=build_manifest(), extra_manifests=["other.chiro.json"])
    fs = GEOFileSystem(skip_instance_cache=True)
    with pytest.raises(ManifestError) as exc:
        fs.ls(GSE)
    assert "will not guess" in str(exc.value)


def test_unparseable_manifest_is_reported(geo):
    server = geo(manifest=b"{not json")
    fs = GEOFileSystem(skip_instance_cache=True)
    with pytest.raises(ManifestError) as exc:
        fs.ls(GSE)
    assert MANIFEST_NAME in str(exc.value)
    assert server.bytes_served  # it did try


# ----------------------------------------------------------------------- drift


def test_clean_deposit_reports_no_drift(geo):
    geo(manifest=build_manifest())
    fs = GEOFileSystem(skip_instance_cache=True)
    drift = fs.drift(GSE)
    assert drift.clean
    assert "matches the manifest" in drift.summary()


def test_unrecorded_deposit_file_is_surfaced(geo):
    """A file in the deposit that the manifest does not mention."""
    geo(manifest=build_manifest(names=["GSE176078_sampleA_matrix.mtx.gz"]))
    fs = GEOFileSystem(skip_instance_cache=True)
    drift = fs.drift(GSE)
    assert not drift.clean
    assert sorted(drift.unrecorded) == [
        "GSE176078_sampleA_barcodes.tsv.gz",
        "GSE176078_sampleB_matrix.mtx.gz",
    ]
    assert "not in the manifest" in drift.summary()


def test_missing_upstream_file_is_listed_but_refuses_to_read(geo):
    """The serious direction: the manifest promises bytes the deposit lost."""
    server = geo(manifest=build_manifest())
    del server.bodies["GSE176078_sampleB_matrix.mtx.gz"]
    fs = GEOFileSystem(skip_instance_cache=True)
    drift = fs.drift(GSE)
    assert drift.missing_upstream == ["sampleB/matrix.mtx.gz"]
    # still listed, so the loss is visible rather than silently absent
    assert f"{GSE}/sampleB/matrix.mtx.gz" in fs.ls(GSE, detail=False)
    with pytest.raises(ManifestError) as exc:
        fs.cat_file(f"{GSE}/sampleB/matrix.mtx.gz")
    assert "diverged" in str(exc.value)


def test_size_disagreement_is_drift(geo):
    """GEO's own size vs the manifest's, when both are known."""
    geo(manifest=build_manifest(tamper_size=True))
    fs = GEOFileSystem(skip_instance_cache=True)
    fs.ls(GSE)
    # GEO's HTML index gives no usable size, so nothing to compare against yet...
    assert fs.drift(GSE).size_mismatches == []
    # ...but once a size is known (HEAD on info), the mismatch is real. Resolve
    # through the native listing to prove the comparison logic itself works.
    from archivefs.chiro import apply_manifest

    native = {
        f"{GSE}/GSE176078_sampleA_matrix.mtx.gz": {
            "name": f"{GSE}/GSE176078_sampleA_matrix.mtx.gz",
            "type": "file",
            "size": len(BODIES["GSE176078_sampleA_matrix.mtx.gz"]),
            "md5": "0" * 32,
            "url": SUPPL + "GSE176078_sampleA_matrix.mtx.gz",
        }
    }
    _, drift = apply_manifest(
        fs, GSE, native, f"{GSE}/{MANIFEST_NAME}", fs.manifest_for(GSE)
    )
    assert drift.size_mismatches[0]["relpath"] == "sampleA/matrix.mtx.gz"
    assert drift.checksum_mismatches[0]["archive_md5"] == "0" * 32


# ------------------------------------------------------ chirograph not installed


def test_missing_chirograph_falls_back_with_a_warning(geo, monkeypatch, caplog):
    geo(manifest=build_manifest())
    monkeypatch.setattr(chiro_mod, "have_chirograph", lambda: False)
    fs = GEOFileSystem(skip_instance_cache=True)
    with caplog.at_level("WARNING"):
        names = fs.ls(GSE, detail=False)
    assert sorted(names) == sorted(
        [*(f"{GSE}/{n}" for n in BODIES), f"{GSE}/{MANIFEST_NAME}"]
    )
    assert "archivefs[chiro]" in caplog.text


def test_missing_chirograph_with_an_explicit_manifest_is_an_error(geo, monkeypatch):
    """Falling back silently is fine for discovery, not for an explicit request."""
    geo(manifest=build_manifest())
    monkeypatch.setattr(chiro_mod, "have_chirograph", lambda: False)
    fs = GEOFileSystem(chiro=f"{GSE}/{MANIFEST_NAME}", skip_instance_cache=True)
    with pytest.raises(MissingExtraError) as exc:
        fs.ls(GSE)
    assert "pip install 'archivefs[chiro]'" in str(exc.value)


# ---------------------------------------------------------------- verification


def test_verify_materialised_delegates_to_chirograph(geo, tmp_path):
    geo(manifest=build_manifest())
    fs = GEOFileSystem(skip_instance_cache=True)
    # materialise the deposit the way DVC would, then verify it
    for path in fs.ls(GSE, detail=False):
        relative = path[len(GSE) + 1 :]
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fs.cat_file(path))

    report = chiro_mod.verify_materialised(fs, GSE, str(tmp_path))
    assert report.body_hash_ok
    assert not report.failures
    assert report.checked == 3


def test_verify_catches_a_corrupted_file(geo, tmp_path):
    geo(manifest=build_manifest())
    fs = GEOFileSystem(skip_instance_cache=True)
    for path in fs.ls(GSE, detail=False):
        relative = path[len(GSE) + 1 :]
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(fs.cat_file(path))
    victim = tmp_path / "sampleA" / "matrix.mtx.gz"
    victim.write_bytes(b"x" * victim.stat().st_size)  # same size, wrong bytes

    report = chiro_mod.verify_materialised(fs, GSE, str(tmp_path))
    assert [f.relpath for f in report.failures] == ["sampleA/matrix.mtx.gz"]
    assert report.failures[0].status == "hash-mismatch"


def test_verify_without_a_manifest_says_so(geo, tmp_path):
    geo(manifest=None)
    fs = GEOFileSystem(skip_instance_cache=True)
    with pytest.raises(ManifestError) as exc:
        chiro_mod.verify_materialised(fs, GSE, str(tmp_path))
    assert "no discoverable .chiro.json" in str(exc.value)


# --------------------------------------------------------- format-level interop


def test_body_hash_matches_chirographs_own(geo):
    """We must never compute manifest hashes ourselves — only read theirs."""
    payload = build_manifest()
    manifest = chirograph.loads(payload)
    declared = json.loads(payload)["body_hash"]
    assert manifest.body_hash == declared
    geo(manifest=payload)
    fs = GEOFileSystem(skip_instance_cache=True)
    assert fs.info(f"{GSE}/sampleA/matrix.mtx.gz")["chiro"]["body_hash"] == declared
