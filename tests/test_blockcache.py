"""The block cache that outlives its process (issue #15).

Two things are being pinned here, and only one of them is "does resuming work".
The other is the hazard the whole module is shaped around: a block recorded as
present but never written serves **zeros as data**, silently, and DVC then hashes
and commits them. Most of these tests are about refusing to do that.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from archivefs import blockcache


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.delenv(blockcache.CACHE_DIR_ENV, raising=False)
    monkeypatch.delenv(blockcache.SCRATCH_DIR_ENV, raising=False)
    blockcache._pruned.clear()
    blockcache._warned.clear()
    return tmp_path / "cache"


def identity(size: int = 4096, md5: str = "abc", blocksize: int = 1024) -> dict:
    return blockcache.identity_of(
        "zenodo", {"name": "1/big.zip", "size": size, "md5": md5}, blocksize
    )


def fill(cache: blockcache.ResumableBlockCache, start: int, end: int) -> bytes:
    return cache._fetch(start, end)


def build(root, ident, size, blocksize, fetcher):
    options = blockcache.prepare(root, ident)
    assert options is not None
    return blockcache.ResumableBlockCache(blocksize, fetcher, size, **options)


# ------------------------------------------------------------------ configuring


def test_no_cache_unless_asked_for(monkeypatch):
    """Default off, and deliberately so.

    A cache of 20 GB archives that nobody asked for is a way to fill a shared
    filesystem, and no default byte cap is both useful (bigger than the archive
    being imported) and safe (smaller than someone else's quota).
    """
    monkeypatch.delenv(blockcache.CACHE_DIR_ENV, raising=False)
    assert blockcache.cache_root() is None


def test_the_environment_turns_it_on(monkeypatch, tmp_path):
    monkeypatch.setenv(blockcache.CACHE_DIR_ENV, str(tmp_path))
    assert blockcache.cache_root() == tmp_path


@pytest.mark.parametrize("value", ["", "false", "no", "off", "NONE"])
def test_one_command_can_opt_out_of_a_globally_enabled_cache(
    monkeypatch, tmp_path, value
):
    """The direction that has to work.

    Turning the cache *off* for a single import must not mean unsetting a variable
    the rest of the shell depends on — and `--fs-config` hands values over as
    strings, so `cache_dir=false` arrives here as the word, not a bool.
    """
    monkeypatch.setenv(blockcache.CACHE_DIR_ENV, str(tmp_path))
    assert blockcache.cache_root(value) is None


def test_the_dvc_default_is_consulted_last_and_never_overrides(monkeypatch, tmp_path):
    """The zero-config default (a dir beside the DVC cache) is a *fallback*.

    It turns resume on when nobody configured anything, but every explicit decision
    still wins: an argument, the environment, a config value — and `cache_dir=false`
    at any of those layers keeps it off even though a default was offered.
    """
    monkeypatch.delenv(blockcache.CACHE_DIR_ENV, raising=False)
    default = tmp_path / "beside-dvc-cache"

    # nobody else said anything -> the default is used
    assert blockcache.cache_root(None, None, str(default)) == default
    # an argument, the environment, or config each beat it
    assert blockcache.cache_root(str(tmp_path / "arg"), None, str(default)) == (
        tmp_path / "arg"
    )
    monkeypatch.setenv(blockcache.CACHE_DIR_ENV, str(tmp_path / "env"))
    assert blockcache.cache_root(None, None, str(default)) == tmp_path / "env"
    monkeypatch.delenv(blockcache.CACHE_DIR_ENV)
    assert blockcache.cache_root(None, str(tmp_path / "cfg"), str(default)) == (
        tmp_path / "cfg"
    )
    # off at any higher layer wins over the default
    assert blockcache.cache_root("false", None, str(default)) is None
    assert blockcache.cache_root(None, "off", str(default)) is None


def test_an_object_with_no_published_checksum_gets_no_persistent_cache():
    """Without a checksum, "same path, same size" is not evidence of same bytes.

    Resuming into a re-deposited file would produce something that hashes cleanly
    against nothing and is then committed. Zenodo publishes an md5 for every file;
    GEO publishes none (D8), and GEO keeps the process-lifetime cache it had.
    """
    assert blockcache.identity_of("geo", {"name": "x", "size": 10}, 1024) is None
    assert blockcache.identity_of("zenodo", {"name": "x", "md5": "a"}, 1024) is None
    assert blockcache.prepare(None, identity()) is None
    assert blockcache.prepare("/tmp", None) is None  # noqa: S108


# ------------------------------------------------------------------- resuming


def test_blocks_survive_the_object_that_fetched_them(root):
    """The whole point: a second process finds the bytes the first one paid for."""
    body = os.urandom(4096)
    calls = []

    def fetch(start, end):
        calls.append((start, end))
        return body[start:end]

    first = build(root, identity(), 4096, 1024, fetch)
    assert fill(first, 0, 2048) == body[:2048]
    assert calls
    first.release()

    calls.clear()
    second = build(root, identity(), 4096, 1024, fetch)
    # fsspec fetches the block *containing* `end`, so a 2048-byte read is three.
    assert second.blocks == {0, 1, 2}
    assert fill(second, 0, 2048) == body[:2048]
    assert calls == [], "the second reader re-fetched blocks that were on disk"
    second.release()


def test_a_partial_read_resumes_and_fetches_only_the_rest(root):
    body = os.urandom(4096)
    first = build(root, identity(), 4096, 1024, lambda s, e: body[s:e])
    fill(first, 0, 1024)
    first.release()

    calls = []

    def fetch(start, end):
        calls.append((start, end))
        return body[start:end]

    second = build(root, identity(), 4096, 1024, fetch)
    assert fill(second, 0, 4096) == body
    # blocks 0-1 came from disk; only 2-3 were bought again
    assert calls == [(2048, 4096)]
    second.release()


def test_a_read_past_the_last_block_does_not_poison_the_index(root):
    """fsspec marks ``end // blocksize`` present even when that block starts past EOF.

    A whole-file read whose ``end`` is rounded up to a block boundary records a block
    that cannot exist — which DVC's download does, though a plain chunked read does
    not. Left in the index, ``_read_index`` then rejects the *entire* file on the next
    run (a block past the end is its corruption signal), so a completed 20 GB import
    resumes from zero. Regression for the plain-file open path (D29 follow-up).
    """
    body = os.urandom(3500)  # 3.42 blocks of 1024: the real blocks are 0..3
    ident = identity(size=3500, blocksize=1024)
    first = build(root, ident, 3500, 1024, lambda s, e: body[s:e])
    # end rounds up to 4096, one block past the 3500-byte file
    assert fill(first, 0, 4096) == body
    assert first.blocks == {0, 1, 2, 3}, first.blocks  # not {0, 1, 2, 3, 4}
    first.release()

    # the index reloads rather than being discarded wholesale
    data, index, _ = blockcache._paths(root, ident)
    assert blockcache._read_index(index, data, ident) == {0, 1, 2, 3}

    calls = []

    def fetch(start, end):
        calls.append((start, end))
        return body[start:end]

    second = build(root, ident, 3500, 1024, fetch)
    assert second.blocks == {0, 1, 2, 3}
    assert fill(second, 0, 3500) == body
    assert calls == [], "a resumed file was re-fetched because of a phantom block"
    second.release()


# -------------------------------------------------------- refusing to be wrong


def test_a_failed_fetch_records_nothing(root):
    """fsspec marks blocks present *before* fetching them.

    In-process that is a latent bug — the object is usually abandoned with the
    exception. Written to disk it is a loaded gun: the next run reads those blocks
    as zeros, hashes them, and commits them without a word.
    """
    def explode(start, end):
        raise OSError("the connection went away")

    cache = build(root, identity(), 4096, 1024, explode)
    with pytest.raises(OSError, match="went away"):
        fill(cache, 0, 2048)
    assert cache.blocks == set(), cache.blocks
    cache.release()

    body = os.urandom(4096)
    again = build(root, identity(), 4096, 1024, lambda s, e: body[s:e])
    assert again.blocks == set()
    assert fill(again, 0, 2048) == body[:2048]
    again.release()


def test_a_redeposited_file_does_not_resume_into_the_old_bytes(root):
    """The md5 is in the key *and* in the index, so staleness is decided, not guessed."""
    old = os.urandom(4096)
    first = build(root, identity(md5="old"), 4096, 1024, lambda s, e: old[s:e])
    fill(first, 0, 4096)
    first.release()

    new = os.urandom(4096)
    second = build(root, identity(md5="new"), 4096, 1024, lambda s, e: new[s:e])
    assert second.blocks == set()
    assert fill(second, 0, 4096) == new
    second.release()


def test_a_different_block_size_invalidates_the_index(root):
    """Block *numbers* are meaningless under a different block size."""
    body = os.urandom(4096)
    first = build(root, identity(blocksize=1024), 4096, 1024, lambda s, e: body[s:e])
    fill(first, 0, 4096)
    first.release()

    calls = []

    def fetch(start, end):
        calls.append((start, end))
        return body[start:end]

    second = build(root, identity(blocksize=2048), 4096, 2048, fetch)
    assert second.blocks == set()
    assert fill(second, 0, 4096) == body
    assert calls, "block 0 of 2048 was served out of an index written for 1024"
    second.release()


def test_a_truncated_data_file_is_discarded(root):
    """A filesystem-full mid-write leaves block numbers pointing at nothing."""
    body = os.urandom(4096)
    ident = identity()
    first = build(root, ident, 4096, 1024, lambda s, e: body[s:e])
    fill(first, 0, 4096)
    first.release()

    data, index, _ = blockcache._paths(root, ident)
    with open(data, "r+b") as handle:
        handle.truncate(2048)
    assert blockcache._read_index(index, data, ident) is None

    second = build(root, ident, 4096, 1024, lambda s, e: body[s:e])
    assert second.blocks == set()
    assert fill(second, 0, 4096) == body
    second.release()


@pytest.mark.parametrize("damage", ["not json at all", '{"version": 99}', "{}"])
def test_an_unreadable_index_starts_over_rather_than_guessing(root, damage):
    body = os.urandom(4096)
    ident = identity()
    first = build(root, ident, 4096, 1024, lambda s, e: body[s:e])
    fill(first, 0, 4096)
    first.release()

    _, index, _ = blockcache._paths(root, ident)
    index.write_text(damage)
    second = build(root, ident, 4096, 1024, lambda s, e: body[s:e])
    assert second.blocks == set()
    second.release()


def test_the_index_is_written_after_the_bytes_and_replaced_atomically(root):
    """A crash between the two must lose blocks, never invent them."""
    body = os.urandom(4096)
    ident = identity()
    cache = build(root, ident, 4096, 1024, lambda s, e: body[s:e])
    fill(cache, 0, 1024)
    _, index, _ = blockcache._paths(root, ident)
    payload = json.loads(index.read_text())
    assert payload["identity"] == ident
    assert payload["runs"] == [[0, 2]]
    # nothing half-written left behind
    assert not list(index.parent.glob("*.tmp"))
    cache.release()


def test_a_second_reader_falls_back_instead_of_interleaving_writes(root):
    """Two imports of one record must not write into the same sparse file.

    Non-blocking on purpose: the loser gets a private cache, which is exactly what
    everyone had before this module existed, rather than waiting hours for a lock.
    """
    body = os.urandom(4096)
    ident = identity()
    holder = build(root, ident, 4096, 1024, lambda s, e: body[s:e])
    assert blockcache.prepare(root, ident) is None
    holder.release()
    assert blockcache.prepare(root, ident) is not None


# ------------------------------------------------------------------- eviction


def test_stale_entries_are_pruned_and_live_ones_are_not(root):
    body = os.urandom(4096)
    stale = identity(md5="stale")
    fresh = identity(md5="fresh")
    for ident in (stale, fresh):
        cache = build(root, ident, 4096, 1024, lambda s, e: body[s:e])
        fill(cache, 0, 4096)
        cache.release()

    data, _, _ = blockcache._paths(root, stale)
    long_ago = time.time() - 40 * 86400
    os.utime(data, (long_ago, long_ago))

    assert blockcache.prune(root, force=True) == 1
    assert not data.exists()
    assert blockcache._paths(root, fresh)[0].exists()


def test_an_entry_in_use_is_never_pruned_out_from_under_a_read(root):
    body = os.urandom(4096)
    ident = identity()
    cache = build(root, ident, 4096, 1024, lambda s, e: body[s:e])
    fill(cache, 0, 4096)
    data = blockcache._paths(root, ident)[0]
    long_ago = time.time() - 40 * 86400
    os.utime(data, (long_ago, long_ago))

    assert blockcache.prune(root, force=True) == 0
    assert data.exists()
    cache.release()


def test_clear_removes_everything_and_reports_it(root):
    body = os.urandom(4096)
    for name in ("a", "b"):
        cache = build(root, identity(md5=name), 4096, 1024, lambda s, e: body[s:e])
        fill(cache, 0, 4096)
        cache.release()
    assert len(blockcache.entries(root)) == 2
    assert blockcache.clear(root) == 2
    assert blockcache.entries(root) == []


def test_entries_report_what_is_on_disk_not_what_the_hole_claims(root):
    """A sparse file's `st_size` is the archive; `st_blocks` is the quota."""
    body = os.urandom(1 << 20)
    ident = identity(size=1 << 20, blocksize=1 << 16)
    cache = build(root, ident, 1 << 20, 1 << 16, lambda s, e: body[s:e])
    fill(cache, 0, 1 << 16)
    cache.release()

    (entry,) = blockcache.entries(root)
    assert entry["path"] == "1/big.zip"
    assert entry["protocol"] == "zenodo"
    assert entry["size"] == 1 << 20
    assert entry["cached"] == 2 << 16
    assert entry["on_disk"] < entry["size"]


def test_an_index_claiming_blocks_past_the_end_is_rejected(root):
    """Whatever else matched, a block past the object cannot have been written.

    Cheap to check, and it also bounds how much memory a corrupt `runs` can be made
    to allocate before anyone notices it is corrupt.
    """
    body = os.urandom(4096)
    ident = identity()
    cache = build(root, ident, 4096, 1024, lambda s, e: body[s:e])
    fill(cache, 0, 4096)
    cache.release()

    _, index, _ = blockcache._paths(root, ident)
    payload = json.loads(index.read_text())
    payload["runs"] = [[0, 4], [999, 1000]]
    index.write_text(json.dumps(payload))

    data, _, _ = blockcache._paths(root, ident)
    assert blockcache._read_index(index, data, ident) is None


def test_a_lock_is_not_stranded_when_the_cache_cannot_be_built(root, monkeypatch):
    """A held lock with nothing behind it pushes every later reader onto a private
    cache, silently — which is exactly the failure the lock exists to avoid."""
    ident = identity()

    def broken(self):
        raise OSError("no space left on device")

    monkeypatch.setattr(blockcache.ResumableBlockCache, "_makefile", broken)
    with pytest.raises(OSError, match="no space"):
        build(root, ident, 4096, 1024, lambda s, e: b"")
    monkeypatch.undo()
    assert blockcache.prepare(root, ident) is not None


# ------------------------------------------- where the *ephemeral* file goes


def test_by_default_the_scratch_directory_is_tempfile_s_own_choice(monkeypatch):
    """Unchanged behaviour when nothing is configured — `$TMPDIR`, then `/tmp`."""
    monkeypatch.delenv(blockcache.SCRATCH_DIR_ENV, raising=False)
    assert blockcache.scratch_root() is None
    assert blockcache.scratch_options() == {}


def test_the_scratch_directory_is_configurable_and_created(tmp_path, monkeypatch):
    """The point of the option.

    fsspec calls `tempfile.TemporaryFile()` with no `dir`, so a 20 GB container's
    sparse file silently follows `$TMPDIR` — or `/tmp` if that is unset, which on
    Gadi is a 63 GB **tmpfs**. Inheriting a destination that might be RAM is not a
    choice anyone made on purpose.
    """
    monkeypatch.delenv(blockcache.SCRATCH_DIR_ENV, raising=False)
    target = tmp_path / "spill" / "here"
    assert blockcache.scratch_options(str(target)) == {"scratch": str(target)}
    assert target.is_dir()


def test_the_environment_sets_the_scratch_directory_too(tmp_path, monkeypatch):
    monkeypatch.setenv(blockcache.SCRATCH_DIR_ENV, str(tmp_path))
    assert blockcache.scratch_root() == tmp_path
    assert blockcache.scratch_options() == {"scratch": str(tmp_path)}
    # and one command can opt back out, same as `cache_dir`
    assert blockcache.scratch_options("false") == {}


def test_an_unusable_scratch_directory_falls_back_instead_of_failing(tmp_path):
    """A directory we cannot create is a fallback, not a failed import."""
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("in the way")
    assert blockcache.scratch_options(str(blocker / "under")) == {}


def test_the_ephemeral_file_lands_in_the_scratch_directory(tmp_path):
    """Not just recorded — the sparse file has to actually be created there."""
    spill = tmp_path / "spill"
    body = os.urandom(4096)
    before = _open_inodes()
    cache = blockcache.ResumableBlockCache(
        1024, lambda s, e: body[s:e], 4096, **blockcache.scratch_options(str(spill))
    )
    cache._fetch(0, 4096)
    # The file is unlinked at creation, so it has no name to look for; what is
    # visible is a new open descriptor whose target is under `spill`.
    assert any(str(spill) in path for path in _open_inodes() - before)
    cache.release()


def _open_inodes() -> set[str]:
    import os as _os

    found = set()
    for entry in _os.listdir("/proc/self/fd"):
        try:
            found.add(_os.readlink(f"/proc/self/fd/{entry}"))
        except OSError:
            continue
    return found


@pytest.mark.parametrize("option", ["cache_dir", "scratch_dir"])
def test_pointing_either_directory_at_home_warns_but_is_allowed(
    tmp_path, monkeypatch, caplog, option
):
    """Not a refusal — `~/.cache` is conventional and a laptop is welcome to it.

    But on a cluster the failure is a blown quota three hours into an import, with
    the traceback pointing at whatever wrote last. One line up front is cheap.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    blockcache._warned.clear()
    under_home = tmp_path / "cache"
    with caplog.at_level("WARNING"):
        if option == "scratch_dir":
            assert blockcache.scratch_options(str(under_home)) == {
                "scratch": str(under_home)
            }
        else:
            assert blockcache.prepare(under_home, identity()) is not None
    assert "inside your home directory" in caplog.text


def test_xdg_cache_home_does_not_enable_the_cache(tmp_path, monkeypatch):
    """XDG names a place for caches; it is not consent for a 20 GB entry.

    Someone exporting `XDG_CACHE_HOME` on a cluster is thinking about pip wheels, and
    the directory is usually shared project space. Honouring it here would turn the
    cache on for everyone who follows ordinary XDG advice — which is the "quietly
    filling a shared filesystem" outcome D29 declined. The config file is the way to
    ask for it, and it is visible in `archivefs config`.
    """
    monkeypatch.delenv(blockcache.CACHE_DIR_ENV, raising=False)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert blockcache.cache_root() is None
    assert blockcache.cache_root(configured=str(tmp_path / "asked-for")) is not None
