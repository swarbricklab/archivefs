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


def manifest_bytes(relocate: bool = False) -> bytes:
    chirograph = pytest.importorskip("chirograph")
    samples = {"counts.csv.gz": "sampleA", "meta.tsv": "sampleB"}
    entries = [
        chirograph.FileEntry(
            relpath=f"{samples[name]}/{name}" if relocate else name,
            md5=hashlib.md5(body).hexdigest(),  # noqa: S324
            size=len(body),
            source_path=name if relocate else None,
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


CBIO_COMMITS = [
    {"sha": "a" * 40, "commit": {"committer": {"date": "2026-01-30T19:02:42Z"},
                                 "message": "Gene Table v7: migrate symbols\n\nbody"}},
    {"sha": "b" * 40, "commit": {"committer": {"date": "2023-03-17T16:10:25Z"},
                                 "message": "update meta"}},
]


def test_versions_lists_a_cbioportal_study_history(monkeypatch, capsys):
    def handler(method, url, headers, kwargs):
        if "/commits" in url:
            return FakeResponse(200, json=CBIO_COMMITS)
        return FakeResponse(404, json={})

    monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(handler))
    assert main(["versions", "cbioportal://brca_metabric"]) == 0
    out = capsys.readouterr().out
    assert f"brca_metabric@{'a' * 40}" in out  # accession is a usable pin URL
    assert "Gene Table v7: migrate symbols" in out  # first line of the message only
    # a member URL still lists the study's versions
    assert main(["versions", "cbioportal://brca_metabric/data_mutations.txt"]) == 0
    assert f"brca_metabric@{'a' * 40}" in capsys.readouterr().out
    # --json emits the structured records
    assert main(["versions", "--json", "cbioportal://brca_metabric"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["accession"] == f"brca_metabric@{'a' * 40}"


def test_versions_refused_for_a_backend_without_history(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["versions", f"geo://{GSE}"])
    assert "does not expose a version history" in str(exc.value)


def test_unknown_protocol_is_refused(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["ls", "sftp://nope"])
    assert "archivefs speaks" in str(exc.value)


def test_a_chained_url_is_refused_with_the_cli_equivalent():
    """The chain is a documented form, so "unknown protocol 'zip'" is a dead end.

    `docs/design.md` and `ZenodoFileSystem`'s docstring both write
    ``zip://::zenodo://...``, so a reader will try it here first. It cannot work
    — `-o` has no way to say which protocol an option belongs to — but the
    refusal has to hand back the two forms that do.
    """
    with pytest.raises(SystemExit) as exc:
        main(["ls", "zip://::zenodo://1486184/github-mark.zip"])
    message = str(exc.value)
    assert "chained fsspec URL" in message
    assert "extract_zips=true zenodo://1486184/github-mark.zip" in message
    assert "url_to_fs" in message


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


def test_ls_is_children_only_and_R_recurses(fake_geo, capsys):
    """ls() must be children-only (fsspec's walk keys files by basename), so the
    recursive listing needs its own flag."""
    fake_geo(manifest_bytes(relocate=True))
    assert main(["ls", f"geo://{GSE}"]) == 0
    shallow = capsys.readouterr().out.split()
    assert shallow == [f"{GSE}/sampleA", f"{GSE}/sampleB"]

    assert main(["ls", "-R", f"geo://{GSE}"]) == 0
    deep = capsys.readouterr().out.split()
    assert sorted(deep) == [
        f"{GSE}/sampleA/counts.csv.gz",
        f"{GSE}/sampleB/meta.tsv",
    ]


def test_get_into_an_existing_directory_keeps_the_name(fake_geo, tmp_path, capsys):
    """`cp`-style: a directory destination means "in here, under the same name"."""
    fake_geo(manifest_bytes())
    target_dir = tmp_path / "into"
    target_dir.mkdir()
    assert main(["get", f"geo://{GSE}/meta.tsv", str(target_dir)]) == 0
    assert (target_dir / "meta.tsv").read_bytes() == BODIES["meta.tsv"]


def test_ls_long_resolves_unknown_sizes(fake_geo, capsys):
    """A listing leaves an unknown size unknown; -l is a request to resolve it."""
    fake_geo()  # no manifest, so GEO's index gives no usable sizes
    assert main(["ls", f"geo://{GSE}"]) == 0
    assert main(["ls", "-l", f"geo://{GSE}"]) == 0
    out = capsys.readouterr().out
    assert "?" not in out.split("\n")[-2]  # the -l lines carry real numbers
    assert str(len(BODIES["meta.tsv"])) in out


# ------------------------------------------------- `archivefs cache` (issue #15)


def test_cache_says_how_to_turn_itself_on_when_it_is_off(capsys):
    """Off is the default, so this is the output most people will ever see.

    It has to be actionable: someone reads it *after* losing three hours of an
    import, and "no cache configured" alone would not tell them what to do next.
    """
    assert main(["cache"]) == 0
    out = capsys.readouterr().out
    assert "ARCHIVEFS_CACHE_DIR" in out
    assert "cache_dir=" in out


def test_cache_path_is_script_friendly(tmp_path, monkeypatch, capsys):
    """One line or nothing, and a non-zero exit when there is nothing — so
    `dir=$(archivefs cache path) || ...` works."""
    monkeypatch.delenv("ARCHIVEFS_CACHE_DIR", raising=False)
    assert main(["cache", "path"]) == 1
    assert capsys.readouterr().out == ""
    monkeypatch.setenv("ARCHIVEFS_CACHE_DIR", str(tmp_path / "blocks"))
    assert main(["cache", "path"]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "blocks")


def test_cache_reports_where_unpersisted_blocks_go(tmp_path, monkeypatch, capsys):
    """The question the cache being off does not answer on its own.

    With no `cache_dir` the sparse file still has to live somewhere, and fsspec
    inherits `$TMPDIR` — or `/tmp`, a tmpfs on Gadi. Saying so is the whole point of
    making it configurable.
    """
    monkeypatch.delenv("ARCHIVEFS_CACHE_DIR", raising=False)
    monkeypatch.setenv("ARCHIVEFS_SCRATCH_DIR", str(tmp_path / "spill"))
    assert main(["cache"]) == 0
    out = capsys.readouterr().out
    assert str(tmp_path / "spill") in out
    assert "ARCHIVEFS_SCRATCH_DIR" in out


def test_cache_size_on_an_empty_cache(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARCHIVEFS_CACHE_DIR", str(tmp_path / "blocks"))
    assert main(["cache", "size"]) == 0
    assert capsys.readouterr().out.strip() == "0 B"
    assert main(["cache"]) == 0
    assert "empty" in capsys.readouterr().out


def test_cache_lists_and_clears_what_is_there(tmp_path, monkeypatch, capsys):
    from archivefs import blockcache

    root = tmp_path / "blocks"
    monkeypatch.setenv("ARCHIVEFS_CACHE_DIR", str(root))
    body = b"x" * 4096
    identity = blockcache.identity_of(
        "zenodo", {"name": "1486184/archive.zip", "size": 4096, "md5": "abc"}, 1024
    )
    cache = blockcache.ResumableBlockCache(
        1024, lambda s, e: body[s:e], 4096, **blockcache.prepare(root, identity)
    )
    cache._fetch(0, 4096)
    cache.release()

    assert main(["cache"]) == 0
    listed = capsys.readouterr().out
    assert "zenodo://1486184/archive.zip" in listed
    assert "1 entries" in listed

    assert main(["cache", "--json"]) == 0
    (entry,) = json.loads(capsys.readouterr().out)
    assert entry["path"] == "1486184/archive.zip"

    assert main(["cache", "clear"]) == 0
    assert "removed 1 entry" in capsys.readouterr().out
    assert main(["cache"]) == 0
    assert "0 entries" in capsys.readouterr().out


def test_cache_dir_overrides_the_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ARCHIVEFS_CACHE_DIR", str(tmp_path / "from-env"))
    assert main(["cache", "path", "--dir", str(tmp_path / "explicit")]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "explicit")


# ------------------------------------------------ `archivefs config` (the model)


def test_config_lists_the_layers_highest_first(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "etc"))
    monkeypatch.chdir(tmp_path)
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert out.index("user") < out.index("system")
    assert "nothing configured" in out


def test_config_shows_what_each_layer_resolved_to(tmp_path, monkeypatch, capsys):
    from archivefs import config

    (tmp_path / ".git").mkdir()
    user = tmp_path / "xdg" / config.XDG_NAME
    user.parent.mkdir(parents=True)
    user.write_text(
        json.dumps({"cache_dir": "/mine", "zenodo": {"api_url": "https://m"}})
    )
    (tmp_path / config.PROJECT_NAME).write_text(
        json.dumps({"zenodo": {"extract_zips": True}})
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "etc"))
    monkeypatch.chdir(tmp_path)

    assert main(["config", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["layers"][0]["name"] == "local"
    assert report["settings"]["zenodo"]["extract_zips"] is True
    assert report["settings"]["zenodo"]["api_url"] == "https://m"
    assert report["settings"]["geo"]["cache_dir"] == "/mine"


def test_config_path_names_the_user_layer_for_an_editor(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.chdir(tmp_path)
    assert main(["config", "path"]) == 0
    assert capsys.readouterr().out.strip().endswith("archivefs/config.json")


def test_a_refused_config_key_is_reported_not_raised(tmp_path, monkeypatch, capsys):
    """ConfigError is an ArchiveFSError, so the CLI prints it instead of a traceback."""
    from archivefs import config

    user = tmp_path / "xdg" / config.XDG_NAME
    user.parent.mkdir(parents=True)
    user.write_text(json.dumps({"extract_zips": True}))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "etc"))
    monkeypatch.chdir(tmp_path)
    assert main(["config"]) == 2
    assert "may not set 'extract_zips'" in capsys.readouterr().err
