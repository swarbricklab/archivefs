"""``archivefs update`` — regenerate a pin from the live archive and diff it.

Driven against the GEO fake, mutated between capture and re-check to stand in for a
depositor revising a record. GEO is the meaningful case: it publishes no checksum, so
the only authority on "did it change" is to regenerate and compare the manifest
``body_hash`` — never an mtime (D34).
"""

from __future__ import annotations

import pytest
from tests.fake_http import FakeRequestsModule
from tests.test_chiro import GSE, FakeGEO, md5

from archivefs import base as base_mod
from archivefs.backends.geo import GEOFileSystem
from archivefs.errors import ManifestError
from archivefs.update import plan_update
from archivefs.writer import build_manifest

chirograph = pytest.importorskip("chirograph")


@pytest.fixture
def geo(monkeypatch):
    def make(**kwargs):
        server = FakeGEO(**kwargs)
        monkeypatch.setattr(base_mod, "requests", FakeRequestsModule(server))
        return server

    return make


def _capture(tmp_path):
    """A committed manifest over the current GEO fake, written to disk."""
    manifest = build_manifest(GEOFileSystem(skip_instance_cache=True), GSE, download=True)
    path = tmp_path / f"{GSE}.chiro.json"
    path.write_text(chirograph.dumps(manifest))
    return path


def test_up_to_date_when_nothing_changed(geo, tmp_path):
    geo()
    path = _capture(tmp_path)
    plan = plan_update(str(path), download=True)
    assert not plan.moved
    assert plan.diff.identical
    assert plan.old.body_hash == plan.new.body_hash


def test_detects_a_new_file_upstream(geo, tmp_path):
    server = geo()
    path = _capture(tmp_path)
    # The depositor adds a file after we pinned.
    server.bodies[f"{GSE}_sampleC_new.tsv.gz"] = b"brand-new" * 50

    plan = plan_update(str(path), download=True)
    assert plan.moved
    added = {e.relpath for e in plan.diff.added}
    assert f"{GSE}_sampleC_new.tsv.gz" in added
    # And --write would re-pin: the new body_hash differs from the old.
    assert plan.new.body_hash != plan.old.body_hash


def test_detects_changed_bytes(geo, tmp_path):
    server = geo()
    path = _capture(tmp_path)
    name = next(iter(server.bodies))
    server.bodies[name] = server.bodies[name] + b"appended"

    plan = plan_update(str(path), download=True)
    assert plan.moved
    changed = {c.relpath for c in plan.diff.changed}
    assert name in changed
    assert plan.new.entry(name).md5 == md5(server.bodies[name])


def test_a_chiro_url_target_is_accepted(geo, tmp_path):
    geo()
    path = _capture(tmp_path)
    plan = plan_update(f"chiro://{path}", download=True)
    assert not plan.moved


def test_a_manifest_without_a_concept_cannot_be_updated(geo, tmp_path):
    geo()
    manifest = chirograph.Manifest(
        files=[chirograph.FileEntry(relpath="a.txt", md5="0" * 32, size=1)],
        record_accession=GSE,
    )
    path = tmp_path / f"{GSE}.chiro.json"
    path.write_text(chirograph.dumps(manifest))
    with pytest.raises(ManifestError, match="concept"):
        plan_update(str(path), download=True)


def test_an_archive_url_is_not_a_manifest(geo, tmp_path):
    geo()
    with pytest.raises(ManifestError, match="not an archive URL"):
        plan_update(f"geo://{GSE}", download=True)


# --------------------------------------------------------------------- the CLI


def test_cli_update_supersedes_into_the_store(geo, tmp_path, capsys):
    from archivefs.cli import main

    # A store next to the manifest, so --write has somewhere to put the new observation.
    (tmp_path / ".chirograph").mkdir()
    server = geo()
    path = _capture(tmp_path)
    old_text = path.read_text()

    # Clean: exit 0, nothing written.
    assert main(["update", str(path), "--download"]) == 0
    assert "up to date" in capsys.readouterr().out

    # Now the upstream moves.
    server.bodies[f"{GSE}_extra.tsv.gz"] = b"x" * 100

    # Report-only default: exit 1, nothing written.
    assert main(["update", str(path), "--download"]) == 1
    out = capsys.readouterr().out
    assert "upstream moved" in out and "+ " in out
    assert list((tmp_path / ".chirograph").rglob("*.chiro.json")) == []

    # --write: exit 0. The old manifest is untouched (supersede, not overwrite); the
    # new observation is a fresh digest-named file in the store.
    assert main(["update", str(path), "--download", "--write"]) == 0
    assert "re-pinned" in capsys.readouterr().out
    assert path.read_text() == old_text  # the old observation is preserved
    written = list((tmp_path / ".chirograph" / "manifests").rglob("*.chiro.json"))
    assert len(written) == 1
    reloaded = chirograph.load(written[0])
    assert reloaded.entry(f"{GSE}_extra.tsv.gz") is not None
