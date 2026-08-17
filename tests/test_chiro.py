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
    return hashlib.md5(data).hexdigest()  # noqa: S324


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
    # workspace layout from relpath, not the flat deposited names: the manifest's
    # per-sample tree shows up as directories...
    assert sorted(fs.ls(GSE, detail=False)) == [f"{GSE}/sampleA", f"{GSE}/sampleB"]
    # ...and every file is reachable. find(), not ls(), is the recursive one — and
    # it must not lose sampleA/matrix vs sampleB/matrix to a basename collision.
    assert sorted(fs.find(GSE)) == [
        f"{GSE}/sampleA/barcodes.tsv.gz",
        f"{GSE}/sampleA/matrix.mtx.gz",
        f"{GSE}/sampleB/matrix.mtx.gz",
    ]
    assert fs.manifest_path_for(GSE) == f"{GSE}/{MANIFEST_NAME}"


def test_colliding_basenames_across_samples_all_survive(geo):
    """The bug this shape hides: fsspec's walk keys files by basename.

    A flattened ls() made find()/glob()/DVC's directory pull drop one of two
    same-named files in different sample directories, silently and with exit 0.
    """
    geo(manifest=build_manifest())
    fs = GEOFileSystem(skip_instance_cache=True)
    matrices = [p for p in fs.find(GSE) if p.endswith("matrix.mtx.gz")]
    assert len(matrices) == 2, matrices
    assert sorted(fs.glob(f"{GSE}/*/matrix.mtx.gz")) == matrices
    # and the bytes differ, so a collision would be real data loss
    assert fs.cat_file(matrices[0]) != fs.cat_file(matrices[1])


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
    assert f"{GSE}/sampleA/matrix.mtx.gz" in fs.find(GSE)


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
    assert f"{GSE}/sampleB/matrix.mtx.gz" in fs.find(GSE)
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
    for path in fs.find(GSE):
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
    for path in fs.find(GSE):
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


def test_a_bogus_explicit_manifest_path_fails_fast(geo):
    """It used to recurse: cat_file re-entered the listing being built, once per
    frame, hammering the archive until Python's recursion limit stopped it."""
    geo(manifest=build_manifest())
    fs = GEOFileSystem(chiro=f"{GSE}/typo.chiro.json", skip_instance_cache=True)
    with pytest.raises((ManifestError, FileNotFoundError)) as exc:
        fs.ls(GSE)
    assert "RecursionError" not in str(exc.value)


def test_a_multi_segment_container_resolves_manifests(monkeypatch):
    """Zenodo's DOI form is two path segments; assuming one broke everything.

    The manifest lookups all missed (so every entry looked missing-upstream) and
    the fetch fell back through cat_file into unbounded recursion.
    """
    from archivefs import base as base_mod_
    from archivefs.backends.zenodo import ZenodoFileSystem

    doi = "10.5281/zenodo.1486184"
    bodies = {"data.csv": b"a,b\n1,2\n"}
    manifest = chirograph.Manifest(
        provenance_type=chirograph.PROVENANCE_SELF,
        files=[
            chirograph.FileEntry(
                relpath="tidy/data.csv",
                md5=md5(bodies["data.csv"]),
                size=len(bodies["data.csv"]),
                source_path="data.csv",
            )
        ],
        record_accession="1486184",
    ).to_bytes()
    payloads = {**bodies, "rec.chiro.json": manifest}

    def handler(method, url, headers, kwargs):
        if url.endswith("/records/1486184"):
            return FakeResponse(
                200,
                json={
                    "id": 1486184,
                    "doi": doi,
                    "conceptrecid": "1486183",
                    "revision": 1,
                    "files": [
                        {
                            "key": name,
                            "size": len(body),
                            "checksum": f"md5:{md5(body)}",
                            "links": {"content": f"https://zenodo.invalid/{name}"},
                        }
                        for name, body in payloads.items()
                    ],
                },
            )
        name = url.rsplit("/", 1)[-1]
        if name in payloads:
            return ranged(payloads[name], headers)
        return FakeResponse(404, b"not found")

    monkeypatch.setattr(base_mod_, "requests", FakeRequestsModule(handler))
    fs = ZenodoFileSystem(skip_instance_cache=True)
    assert fs.find(doi) == [f"{doi}/tidy/data.csv"]
    info = fs.info(f"{doi}/tidy/data.csv")
    assert not info.get("missing_upstream")
    assert info["md5"] == md5(bodies["data.csv"])
    assert fs.cat_file(f"{doi}/tidy/data.csv") == bodies["data.csv"]
    assert fs.drift(doi).clean


def test_an_unfetchable_manifest_is_not_an_empty_directory(geo):
    """fsspec's walk() swallows FileNotFoundError, so ManifestError must not be one.

    Otherwise a manifest that 404s turns the whole deposit into an empty listing —
    a pull that succeeds and writes nothing.
    """
    geo(manifest=build_manifest())
    fs = GEOFileSystem(chiro=f"{GSE}/absent.chiro.json", skip_instance_cache=True)
    with pytest.raises(ManifestError):
        fs.ls(GSE)
    assert not isinstance(ManifestError("x"), FileNotFoundError)
    # and the failure survives find(), which is what DVC's directory pull uses
    fs2 = GEOFileSystem(chiro=f"{GSE}/absent.chiro.json", skip_instance_cache=True)
    with pytest.raises(ManifestError):
        fs2.find(GSE)


def test_concurrent_readers_all_see_the_manifest(geo, monkeypatch):
    """The cache seed that stopped manifest recursion must not publish a
    half-resolved listing to other threads.

    DVC runs one filesystem instance across a thread pool (and cachable=False
    guarantees they share it), so a second thread hitting the seeded native listing
    would silently lose the manifest's checksums, paths and drift report.
    """
    import threading
    import time as time_mod

    server = geo(manifest=build_manifest())
    fs = GEOFileSystem(skip_instance_cache=True)

    # make the manifest fetch slow enough for a second thread to arrive mid-flight
    slow = fs._fetch_range

    def delayed(info, start, end):
        if str(info.get("name", "")).endswith(".chiro.json"):
            time_mod.sleep(0.3)
        return slow(info, start, end)

    monkeypatch.setattr(fs, "_fetch_range", delayed)

    results: dict[str, object] = {}

    def read(tag: str) -> None:
        try:
            results[tag] = sorted(fs.find(GSE))
        except Exception as exc:  # noqa: BLE001 - recorded, asserted below
            results[tag] = exc

    threads = [threading.Thread(target=read, args=(f"t{i}",)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    expected = [
        f"{GSE}/sampleA/barcodes.tsv.gz",
        f"{GSE}/sampleA/matrix.mtx.gz",
        f"{GSE}/sampleB/matrix.mtx.gz",
    ]
    assert list(results.values()) == [expected] * 3, results
    assert fs.manifest_path_for(GSE) == f"{GSE}/{MANIFEST_NAME}"
    assert server.manifest is not None


def test_a_manifest_that_matches_nothing_is_refused(geo):
    """Every entry a ghost means the manifest describes a different layout.

    Producing a listing of files that all fail to read would be a slow way to
    learn that.
    """
    geo(manifest=build_manifest(names=["GSE176078_sampleZ_absent.mtx.gz"]))
    fs = GEOFileSystem(skip_instance_cache=True)
    with pytest.raises(ManifestError) as exc:
        fs.ls(GSE)
    message = str(exc.value)
    assert "not one of them matches" in message
    assert "chiro=False" in message


def test_a_manifest_that_nests_one_file_inside_another_is_refused(geo, monkeypatch):
    """`data.txt` and `data.txt/inner.txt` cannot both exist on a filesystem, and a
    children-only ls would report the file and never descend."""
    entries = [
        chirograph.FileEntry(relpath="data.txt", md5=md5(b"a"), size=1, source_path=n)
        for n in ["GSE176078_sampleA_matrix.mtx.gz"]
    ] + [
        chirograph.FileEntry(
            relpath="data.txt/inner.txt",
            md5=md5(b"b"),
            size=1,
            source_path="GSE176078_sampleB_matrix.mtx.gz",
        )
    ]
    manifest = chirograph.Manifest(
        provenance_type=chirograph.PROVENANCE_SELF, files=entries, record_accession=GSE
    ).to_bytes()
    geo(manifest=manifest)
    fs = GEOFileSystem(skip_instance_cache=True)
    with pytest.raises(ManifestError) as exc:
        fs.ls(GSE)
    assert "inside another" in str(exc.value)


@pytest.mark.parametrize("flag", ["true", "TRUE", "yes", "on", "1", "auto"])
def test_true_ish_chiro_flags_mean_discover(geo, flag):
    """`chiro = true` in an INI config is the obvious way to write "yes"."""
    geo(manifest=build_manifest())
    fs = GEOFileSystem(chiro=flag, skip_instance_cache=True)
    assert fs.manifest_path_for(GSE) == f"{GSE}/{MANIFEST_NAME}"


@pytest.mark.parametrize("flag", ["false", "FALSE", "no", "off", "0", "none"])
def test_false_ish_chiro_flags_mean_off(geo, flag):
    geo(manifest=build_manifest())
    fs = GEOFileSystem(chiro=flag, skip_instance_cache=True)
    assert fs.manifest_path_for(GSE) is None


def test_verify_without_chirograph_names_the_extra(geo, monkeypatch, tmp_path):
    """`from chirograph import verify` at the top of the function bypassed the guard,
    so the user got a traceback instead of the install instruction."""
    geo(manifest=build_manifest())
    fs = GEOFileSystem(skip_instance_cache=True)
    fs.ls(GSE)  # resolve while chirograph is still importable

    real_import = chiro_mod._chirograph

    def missing():
        from archivefs.errors import MissingExtraError

        raise MissingExtraError("chiro", "chirograph", "verifying a tree")

    monkeypatch.setattr(chiro_mod, "_chirograph", missing)
    try:
        with pytest.raises(MissingExtraError) as exc:
            chiro_mod.verify_materialised(fs, GSE, str(tmp_path))
        assert "pip install 'archivefs[chiro]'" in str(exc.value)
    finally:
        monkeypatch.setattr(chiro_mod, "_chirograph", real_import)
