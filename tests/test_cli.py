"""CLI tests. Offline, driving the same fake GEO the backend tests use."""

from __future__ import annotations

import hashlib
import json

import pytest
from tests.fake_http import FakeRequestsModule, FakeResponse, ranged

from archivefs import base as base_mod
from archivefs.cli import main

GSE = "GSE176078"
BODIES = {
    "counts.csv.gz": b"counts" * 500,
    "meta.tsv": b"meta" * 100,
}


def index_html(names) -> bytes:
    links = "\n".join(f'<a href="{name}">{name}</a>' for name in names)
    return f"<html><body><pre>{links}</pre></body></html>".encode()


class FakeGEO:
    def __init__(self, manifest: bytes | None = None) -> None:
        self.manifest = manifest

    @property
    def names(self):
        names = list(BODIES)
        if self.manifest is not None:
            names.append(f"{GSE}.chiro.json")
        return names

    def __call__(self, method, url, headers, kwargs):
        if url.endswith("filelist.txt"):
            return FakeResponse(404, b"absent")
        if url.endswith("/suppl/"):
            return FakeResponse(200, index_html(self.names))
        name = url.rsplit("/", 1)[-1]
        if name.endswith(".chiro.json") and self.manifest is not None:
            if method == "HEAD":
                return FakeResponse(
                    200, b"", {"Content-Length": str(len(self.manifest))}
                )
            return ranged(self.manifest, headers)
        if name in BODIES:
            if method == "HEAD":
                return FakeResponse(
                    200, b"", {"Content-Length": str(len(BODIES[name]))}
                )
            return ranged(BODIES[name], headers)
        return FakeResponse(404, b"not found")


@pytest.fixture
def fake_geo(monkeypatch):
    def make(manifest=None):
        monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(FakeGEO(manifest)))

    return make


def manifest_bytes() -> bytes:
    chirograph = pytest.importorskip("chirograph")
    entries = [
        chirograph.FileEntry(
            relpath=name,
            md5=hashlib.md5(body).hexdigest(),  # noqa: S324
            size=len(body),
        )
        for name, body in BODIES.items()
    ]
    return chirograph.Manifest(
        provenance_type=chirograph.PROVENANCE_SELF,
        files=entries,
        record_accession=GSE,
    ).to_bytes()


# ------------------------------------------------------------------------ info


def test_info_lists_every_protocol_and_its_state(capsys):
    assert main(["info"]) == 0
    out = capsys.readouterr().out
    for protocol in ("ega://", "zenodo://", "geo://", "dbgap://"):
        assert protocol in out
    assert "deferred" in out  # dbgap is honest about itself


def test_info_json(capsys):
    assert main(["info", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["backends"]["geo"]["ready"] is True
    assert payload["backends"]["dbgap"]["status"] == "deferred"


# -------------------------------------------------------------------- ls / stat


def test_ls(fake_geo, capsys):
    fake_geo()
    assert main(["ls", f"geo://{GSE}"]) == 0
    out = capsys.readouterr().out
    assert f"{GSE}/counts.csv.gz" in out


def test_ls_long_shows_size_and_checksum(fake_geo, capsys):
    fake_geo(manifest_bytes())
    assert main(["ls", "-l", f"geo://{GSE}"]) == 0
    out = capsys.readouterr().out
    assert hashlib.md5(BODIES["meta.tsv"]).hexdigest() in out  # noqa: S324


def test_stat_is_json(fake_geo, capsys):
    fake_geo()
    assert main(["stat", f"geo://{GSE}/meta.tsv"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["size"] == len(BODIES["meta.tsv"])


def test_unknown_protocol_is_refused(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["ls", "sftp://nope"])
    assert "archivefs speaks" in str(exc.value)


def test_a_bare_word_is_not_a_url():
    with pytest.raises(SystemExit) as exc:
        main(["ls", "GSE176078"])
    assert "does not look like an archive URL" in str(exc.value)


def test_storage_options_are_passed_through(fake_geo, capsys):
    fake_geo(manifest_bytes())
    assert main(["ls", f"geo://{GSE}", "-o", "chiro=false"]) == 0
    # with manifests off, the manifest itself shows up as a plain file
    assert f"{GSE}.chiro.json" in capsys.readouterr().out


def test_bad_option_syntax_is_refused():
    with pytest.raises(SystemExit) as exc:
        main(["ls", f"geo://{GSE}", "-o", "nonsense"])
    assert "key=value" in str(exc.value)


# ------------------------------------------------------------------------- get


def test_get_verifies_against_the_published_checksum(fake_geo, tmp_path, capsys):
    fake_geo(manifest_bytes())
    target = tmp_path / "meta.tsv"
    assert main(["get", f"geo://{GSE}/meta.tsv", str(target)]) == 0
    assert target.read_bytes() == BODIES["meta.tsv"]
    assert "verified" in capsys.readouterr().out


def test_get_says_so_when_there_is_no_checksum(fake_geo, tmp_path, capsys):
    fake_geo()
    target = tmp_path / "meta.tsv"
    assert main(["get", f"geo://{GSE}/meta.tsv", str(target)]) == 0
    assert "no checksum published" in capsys.readouterr().out


def test_get_reports_a_mismatch_and_fails(fake_geo, tmp_path, capsys, monkeypatch):
    fake_geo(manifest_bytes())
    # corrupt the served bytes so the download disagrees with the manifest
    monkeypatch.setitem(BODIES, "meta.tsv", b"tampered" * 50)
    target = tmp_path / "meta.tsv"
    assert main(["get", f"geo://{GSE}/meta.tsv", str(target)]) == 1
    assert "MISMATCH" in capsys.readouterr().err


def test_get_a_whole_record(fake_geo, tmp_path, capsys):
    fake_geo(manifest_bytes())
    assert main(["get", f"geo://{GSE}", str(tmp_path / "out")]) == 0
    for name, body in BODIES.items():
        assert (tmp_path / "out" / name).read_bytes() == body


# ----------------------------------------------------------------- check/verify


def test_check_without_a_manifest(fake_geo, capsys):
    fake_geo()
    assert main(["check", f"geo://{GSE}"]) == 0
    assert "no .chiro.json manifest found" in capsys.readouterr().out


def test_check_clean(fake_geo, capsys):
    fake_geo(manifest_bytes())
    assert main(["check", f"geo://{GSE}"]) == 0
    assert "matches the manifest" in capsys.readouterr().out


def test_verify_a_downloaded_tree(fake_geo, tmp_path, capsys):
    fake_geo(manifest_bytes())
    assert main(["get", f"geo://{GSE}", str(tmp_path / "out")]) == 0
    assert main(["verify", f"geo://{GSE}", str(tmp_path / "out")]) == 0
    out = capsys.readouterr().out
    assert "body hash: ok" in out
    assert "0 failure(s)" in out


def test_verify_fails_on_a_corrupted_tree(fake_geo, tmp_path, capsys):
    fake_geo(manifest_bytes())
    main(["get", f"geo://{GSE}", str(tmp_path / "out")])
    victim = tmp_path / "out" / "meta.tsv"
    victim.write_bytes(b"x" * victim.stat().st_size)
    assert main(["verify", f"geo://{GSE}", str(tmp_path / "out")]) == 1
    assert "hash-mismatch" in capsys.readouterr().out


# --------------------------------------------------------------- error reporting


def test_our_errors_are_printed_not_raised(fake_geo, capsys):
    fake_geo()
    assert main(["stat", f"geo://{GSE}/absent.txt"]) == 2
    err = capsys.readouterr().err
    assert "ArchiveNotFoundError" in err
    assert "Traceback" not in err
