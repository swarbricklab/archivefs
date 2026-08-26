"""A block cache that outlives the process that filled it (issue #15).

D25 gave a ZIP's member view an ``mmap`` cache so each block is fetched once. It
works, and it is thrown away: ``MMapCache`` with no ``location`` falls through to
``tempfile.TemporaryFile()``, which is unlinked at creation, so the sparse file dies
with the process. A real import of a 20.09 GB record ran for 2h50m, had **16.1 GB**
cached, did not finish, and started again from zero.

At the ~1.6 MB/s such an archive actually reads at, that is a 3.5 hour window in
which any interruption — a cancelled job, a walltime kill, an unrelated exception in
DVC — costs everything. Amplification (#10) made imports slow; this makes long ones
unreliable, which is worse.

The fix is small in machinery and careful in everything else. ``MMapCache`` already
takes ``location`` and ``blocks``, so the mechanism is just "name the file and
remember which blocks are in it". What needs care is that **a block recorded as
present but not written serves zeros as data** — silently, and then DVC hashes and
commits them. Everything here is arranged around that one hazard:

* the block set is written **after** the bytes, never before (fsspec's own
  ``_fetch`` marks blocks present up front, which is fine for a set that dies with
  the process and fatal for one that does not — see :meth:`ResumableBlockCache._fetch`);
* the index is replaced atomically, so a crash mid-write leaves the previous index
  or none, never half of one;
* the index names the exact object it belongs to — protocol, path, size, published
  md5 and block size — and any mismatch discards the cache rather than guessing;
* an object with **no published checksum gets no persistent cache at all**, because
  without one "same path, same size" is not evidence that it is the same bytes;
* one process holds an exclusive ``flock`` per entry, and a second falls back to a
  private cache rather than interleaving writes into the same sparse file.

**Off unless asked for.** A cache of 20 GB archives that nobody asked for is a way to
fill a shared filesystem, and no default byte cap is both useful (bigger than the
archive you are importing) and safe (smaller than someone else's quota). Set
``ARCHIVEFS_CACHE_DIR``, or pass ``cache_dir=`` per filesystem::

    export ARCHIVEFS_CACHE_DIR=/scratch/$PROJECT/$USER/archivefs-cache
    dvc import-url 'zenodo://17411292/big.zip' data/big

Entries older than ``ARCHIVEFS_CACHE_MAX_AGE_DAYS`` (default 14) are removed when the
cache is next opened, and ``archivefs cache`` reports and clears it by hand.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from fsspec.caching import MMapCache, register_cache

logger = logging.getLogger(__name__)

__all__ = [
    "CACHE_DIR_ENV",
    "ResumableBlockCache",
    "cache_root",
    "entries",
    "prepare",
    "prune",
]

#: Where the cache lives. Unset means no persistent cache, which is the default.
CACHE_DIR_ENV = "ARCHIVEFS_CACHE_DIR"
#: How long an untouched entry survives, in days.
MAX_AGE_ENV = "ARCHIVEFS_CACHE_MAX_AGE_DAYS"
DEFAULT_MAX_AGE_DAYS = 14

#: Bumped if the index layout changes; an older index is discarded, not read.
INDEX_VERSION = 1

DATA_SUFFIX = ".data"
INDEX_SUFFIX = ".blocks"
LOCK_SUFFIX = ".lock"

#: Values of ``cache_dir`` that mean "no". `--fs-config` hands over strings, so
#: `cache_dir=false` has to be a string here and not a bool.
_DISABLED = {"", "0", "false", "no", "none", "off"}

#: Pruning is a directory walk; once per root per process is enough.
_pruned: set[str] = set()


def cache_root(explicit: Any = None) -> Path | None:
    """Where persistent blocks go, or ``None`` for "do not persist".

    ``explicit`` (a filesystem's ``cache_dir``) wins over the environment so that a
    single import can opt out of a globally enabled cache, which is the direction
    that has to work: turning it *off* for one command must not require unsetting a
    variable the rest of the shell depends on.
    """
    if explicit is None:
        explicit = os.environ.get(CACHE_DIR_ENV)
    if explicit is None or explicit is False:
        return None
    if isinstance(explicit, str) and explicit.strip().lower() in _DISABLED:
        return None
    if explicit is True:
        raise ValueError("cache_dir needs a directory, not a bare true")
    return Path(explicit).expanduser()


def _max_age_seconds() -> float:
    try:
        days = float(os.environ.get(MAX_AGE_ENV, DEFAULT_MAX_AGE_DAYS))
    except ValueError:
        days = DEFAULT_MAX_AGE_DAYS
    return max(0.0, days) * 86400


def identity_of(protocol: str, info: dict, blocksize: int) -> dict | None:
    """What makes this cache entry *this* object, or ``None`` if nothing does.

    The published checksum is load-bearing rather than decorative. Without one, the
    only things distinguishing two deposits at the same path are size and luck, and
    resuming into the wrong bytes produces a file that hashes cleanly against
    nothing and is then committed. Zenodo publishes an md5 for every file, which is
    why this landed there first; GEO publishes none (D8), so GEO gets the
    process-lifetime cache it has always had.
    """
    md5 = info.get("md5")
    size = info.get("size")
    if not md5 or not size:
        return None
    return {
        "protocol": protocol,
        "path": info.get("name"),
        "size": int(size),
        "md5": md5,
        "blocksize": int(blocksize),
        "version": INDEX_VERSION,
    }


def _digest(identity: dict) -> str:
    canonical = json.dumps(identity, sort_keys=True).encode()
    return hashlib.sha256(canonical).hexdigest()


def _paths(root: Path, identity: dict) -> tuple[Path, Path, Path]:
    digest = _digest(identity)
    folder = root / str(identity["protocol"]) / digest[:2]
    stem = folder / digest
    return (
        stem.with_suffix(DATA_SUFFIX),
        stem.with_suffix(INDEX_SUFFIX),
        stem.with_suffix(LOCK_SUFFIX),
    )


def _to_runs(blocks: set[int]) -> list[list[int]]:
    """``{0,1,2,7}`` -> ``[[0,3],[7,8]]``.

    Runs rather than a flat list because a 20 GB archive in 16 MiB blocks is 1280
    entries and a sequential read makes all of them one run — and this file is
    rewritten after every block fetched.
    """
    runs: list[list[int]] = []
    for block in sorted(blocks):
        if runs and runs[-1][1] == block:
            runs[-1][1] = block + 1
        else:
            runs.append([block, block + 1])
    return runs


def _from_runs(runs: Any) -> set[int]:
    out: set[int] = set()
    for run in runs:
        first, last = run
        out.update(range(int(first), int(last)))
    return out


def _lock(path: Path) -> int | None:
    """An exclusive, non-blocking ``flock``, or ``None`` if someone else holds it.

    Non-blocking on purpose: two imports of the same record should not queue behind
    each other for hours, and the loser falling back to a private cache is exactly
    the behaviour everyone had before this module existed.
    """
    import fcntl

    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(handle)
        return None
    return handle


def _unlink(*paths: Path) -> None:
    for path in paths:
        try:
            path.unlink()
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                logger.debug("could not remove %s: %s", path, exc)


def _read_index(index: Path, data: Path, identity: dict) -> set[int] | None:
    """The blocks known to be in ``data``, or ``None`` if it cannot be trusted.

    Every way of *not* being sure returns ``None``, and the caller then starts from
    an empty cache. Reading zeros as data is silent; re-fetching is merely slow.
    """
    try:
        payload = json.loads(index.read_text())
    except (OSError, ValueError) as exc:
        logger.debug("unreadable block index %s: %s", index, exc)
        return None
    if payload.get("version") != INDEX_VERSION:
        return None
    if payload.get("identity") != identity:
        # A re-deposited file, a different block size, a different archive: all
        # decidable from the record, none of them a reason to guess.
        logger.debug("block index %s is for a different object", index)
        return None
    try:
        stat = data.stat()
    except OSError:
        return None
    if stat.st_size != identity["size"]:
        # Truncated by a filesystem-full, or a partial write: the block numbers no
        # longer mean what they meant.
        logger.debug(
            "cached %s is %d bytes, expected %d", data, stat.st_size, identity["size"]
        )
        return None
    try:
        blocks = _from_runs(payload["runs"])
    except (KeyError, TypeError, ValueError):
        return None
    count = -(-identity["size"] // identity["blocksize"])
    if blocks and max(blocks) >= count:
        # A block past the end of the object cannot have been written, so the index
        # is not what it claims whatever else matched. Cheap, and it also bounds how
        # much a corrupt `runs` can be made to allocate.
        logger.debug("block index %s claims blocks past the end of the object", index)
        return None
    return blocks


def prepare(root: Path | None, identity: dict | None) -> dict | None:
    """Cache options for a resumable cache, or ``None`` to use a private one.

    Every failure here is a fallback rather than an error: a cache is an
    optimisation, and an import that cannot write one should still run.
    """
    if root is None or identity is None:
        return None
    data, index, lock = _paths(root, identity)
    try:
        data.parent.mkdir(parents=True, exist_ok=True)
        prune(root)
        handle = _lock(lock)
    except OSError as exc:
        logger.debug("cannot use the block cache at %s: %s", root, exc)
        return None
    if handle is None:
        logger.debug("another process holds %s; using a private cache", data)
        return None
    blocks = _read_index(index, data, identity) if data.exists() else set()
    if blocks is None:
        _unlink(data, index)
        blocks = set()
    elif blocks:
        logger.info(
            "resuming %s from %s: %d of %d blocks already cached",
            identity["path"],
            data,
            len(blocks),
            -(-identity["size"] // identity["blocksize"]),
        )
    return {
        "location": str(data),
        "blocks": blocks,
        "index": str(index),
        "lock": handle,
        "identity": identity,
    }


def entries(root: Path) -> list[dict]:
    """Every entry under ``root``, newest first — what ``archivefs cache`` shows."""
    found: list[dict] = []
    for data in sorted(root.glob(f"*/*/*{DATA_SUFFIX}")):
        index = data.with_suffix(INDEX_SUFFIX)
        try:
            stat = data.stat()
        except OSError:  # pragma: no cover - raced with a prune
            continue
        identity: dict = {}
        blocks = 0
        try:
            payload = json.loads(index.read_text())
            identity = payload.get("identity") or {}
            blocks = len(_from_runs(payload.get("runs") or []))
        except (OSError, ValueError, TypeError):
            pass
        blocksize = identity.get("blocksize") or 0
        found.append(
            {
                "path": identity.get("path") or data.name,
                "protocol": identity.get("protocol") or data.parent.parent.name,
                "size": identity.get("size") or stat.st_size,
                # `st_blocks` is what the sparse file actually occupies, which is
                # the number that matters on a quota; `st_size` is the hole.
                "on_disk": stat.st_blocks * 512,
                "cached": blocks * blocksize,
                "mtime": stat.st_mtime,
                "data": str(data),
            }
        )
    return sorted(found, key=lambda e: e["mtime"], reverse=True)


def prune(root: Path, max_age: float | None = None, force: bool = False) -> int:
    """Remove entries untouched for ``max_age`` seconds. Returns how many went.

    Only entries this process can lock are removed, so a running import is never
    pruned out from under itself. Runs at most once per root per process unless
    ``force``, because it is a directory walk on a path that may be on Lustre.
    """
    key = str(root)
    if not force and key in _pruned:
        return 0
    _pruned.add(key)
    cutoff = time.time() - (_max_age_seconds() if max_age is None else max_age)
    removed = 0
    for data in root.glob(f"*/*/*{DATA_SUFFIX}"):
        try:
            if data.stat().st_mtime >= cutoff:
                continue
        except OSError:  # pragma: no cover - raced with another prune
            continue
        lock = data.with_suffix(LOCK_SUFFIX)
        handle = _lock(lock)
        if handle is None:
            continue  # in use; not ours to remove
        try:
            _unlink(data, data.with_suffix(INDEX_SUFFIX), lock)
            removed += 1
        finally:
            os.close(handle)
    if removed:
        logger.info("removed %d stale block cache entries from %s", removed, root)
    return removed


def clear(root: Path) -> int:
    """Remove every entry that is not currently in use. Returns how many went."""
    return prune(root, max_age=-1.0, force=True)


class ResumableBlockCache(MMapCache):
    """``MMapCache`` that records, durably, which blocks are really in the file.

    The override is one method, and it is there for one reason. fsspec's ``_fetch``
    does ``self.blocks.update(_blocks)`` *before* it fetches them, so a fetch that
    raises leaves the set claiming blocks that were never written. In-process that
    is a latent bug; written to disk it is a loaded gun, because the next run would
    read those blocks as zeros, hash them, and commit them without a word.
    """

    name = "archivefs-resumable"

    def __init__(
        self,
        blocksize: int,
        fetcher: Any,
        size: int,
        location: str | None = None,
        blocks: set[int] | None = None,
        multi_fetcher: Any = None,
        index: str | None = None,
        lock: int | None = None,
        identity: dict | None = None,
    ) -> None:
        self.index = index
        self.lock = lock
        self.identity = identity or {}
        try:
            super().__init__(blocksize, fetcher, size, location, blocks, multi_fetcher)
        except BaseException:
            # `_makefile` can fail — a full filesystem, a file that will not mmap —
            # and the lock would then be held by nothing for the rest of the process,
            # quietly pushing every later reader onto a private cache.
            self.release()
            raise

    def _fetch(self, start: int | None, end: int | None) -> bytes:
        known = set(self.blocks)
        try:
            data = super()._fetch(start, end)
        except BaseException:
            # Whatever was optimistically marked present did not arrive.
            self.blocks = known
            raise
        if self.index and self.blocks != known:
            self._persist()
        return data

    def _persist(self) -> None:
        """Bytes to disk first, then the index that says they are there.

        The order is the whole point: crashing between them loses blocks, which
        costs time. Crashing the other way round loses *correctness*, which costs a
        silently wrong dataset.
        """
        try:
            flush = getattr(self.cache, "flush", None)
            if flush is not None:
                flush()
            payload = {
                "version": INDEX_VERSION,
                "identity": self.identity,
                "runs": _to_runs(self.blocks),
            }
            temporary = f"{self.index}.{os.getpid()}.tmp"
            with open(temporary, "w") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.index)
        except OSError as exc:
            # A cache that cannot be written is a slow import, not a failed one.
            logger.warning("could not record cached blocks in %s: %s", self.index, exc)
            self.index = None

    def release(self) -> None:
        """Drop the entry lock. Idempotent, and safe to call from a ``finally``."""
        if self.lock is None:
            return
        # Cleared first: `__del__` can run during interpreter shutdown, and a second
        # attempt on a half-torn-down module must not be able to close a *reused* fd.
        handle, self.lock = self.lock, None
        try:
            os.close(handle)
        except OSError:  # pragma: no cover - already closed
            logger.debug("block cache lock was already released")

    def __del__(self) -> None:
        self.release()

    def __getstate__(self) -> dict[str, Any]:
        state = super().__getstate__()
        # An fd does not survive a pickle, and neither does the flock it holds — so
        # the copy on the other side reads what is already recorded and writes
        # nothing. Two unsynchronised writers is the one thing the lock exists to
        # prevent, and a pickle is not a reason to hand it back.
        state["lock"] = None
        state["index"] = None
        return state


register_cache(ResumableBlockCache, clobber=True)
