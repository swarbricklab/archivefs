"""The manifest store: repo-root discovery, digest naming, and ref resolution."""

from __future__ import annotations

import hashlib

import pytest

from archivefs import store
from archivefs.errors import ManifestError

chirograph = pytest.importorskip("chirograph")


def _manifest():
    return chirograph.Manifest(
        files=[chirograph.FileEntry(relpath="a.txt", md5="0" * 32, size=1)],
        concept="geo:X",
        record_accession="X",
    )


def test_store_roots_walks_up_to_the_repo(tmp_path):
    (tmp_path / ".chirograph").mkdir()
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    # Found from any subdirectory — the fix for run-from-a-subdir resolution.
    assert store.store_roots(deep) == [tmp_path / ".chirograph"]
    # A .git or .dvc marker also identifies the root (store need not exist yet).
    other = tmp_path / "repo2"
    (other / ".git").mkdir(parents=True)
    assert store.store_roots(other) == [other / ".chirograph"]


def test_no_repo_no_roots(tmp_path):
    assert store.store_roots(tmp_path) == []


def test_write_is_digest_named_sharded_and_idempotent(tmp_path):
    (tmp_path / ".chirograph").mkdir()
    dest, ref = store.write_to_store(_manifest(), start=tmp_path)

    assert dest.is_file()
    assert str(ref).startswith("chiro://md5:")
    # The filename IS the whole-file digest (md5sum-checkable), sharded by first two.
    digest = hashlib.md5(dest.read_bytes()).hexdigest()  # noqa: S324
    assert ref.digest == digest
    assert dest.name == f"{digest[2:]}.chiro.json"
    assert dest.parent.name == digest[:2]

    # Same manifest bytes -> same path, no duplicate (safe concurrent writes).
    dest2, ref2 = store.write_to_store(_manifest(), start=tmp_path)
    assert (dest2, str(ref2)) == (dest, str(ref))


def test_resolve_finds_and_verifies(tmp_path):
    (tmp_path / ".chirograph").mkdir()
    dest, ref = store.write_to_store(_manifest(), start=tmp_path)
    assert store.resolve_ref(ref, start=tmp_path) == dest


def test_resolve_missing_is_a_clear_error(tmp_path):
    (tmp_path / ".chirograph").mkdir()
    ref = chirograph.ChiroRef.parse("chiro://md5:" + "0" * 32)
    with pytest.raises(ManifestError, match="no manifest for"):
        store.resolve_ref(ref, start=tmp_path)


def test_resolve_refuses_a_corrupted_file(tmp_path):
    (tmp_path / ".chirograph").mkdir()
    _dest, ref = store.write_to_store(_manifest(), start=tmp_path)
    # Tamper the stored file so its bytes no longer hash to its name.
    (tmp_path / ".chirograph" / ref.shard_path).write_bytes(b"not the manifest")
    with pytest.raises(ManifestError, match="edited or corrupted"):
        store.resolve_ref(ref, start=tmp_path)


@pytest.mark.parametrize(
    ("text", "is_ref"),
    [
        ("md5:" + "a" * 32, True),
        ("chiro://md5:" + "a" * 32, True),
        ("sha256:" + "a" * 64, True),
        ("data/x.chiro.json", False),
        ("chiro://data/x.chiro.json", False),
        ("S-BIAD3080", False),
        ("md5:tooshort", False),
    ],
)
def test_as_ref_distinguishes_refs_from_paths(text, is_ref):
    assert (store.as_ref(text) is not None) is is_ref
