"""The local manifest store, and where ``chiro://<digest>`` references resolve.

A ``chiro://`` reference names a manifest by the **digest of the whole file**
(chirograph's spec §13): ``chiro://md5:15576e…`` addresses the manifest whose bytes
hash to that digest. The reference says *what*, never *where* — resolution finds it,
by searching an ordered list of stores, first hit wins. That indirection is the whole
point: it is what lets the same reference resolve from a repo-local copy today and,
later, from a shared registry or a hosted URL without any committed ``.dvc`` changing.

Today the list has one entry: the repo-local store, ``.chirograph/`` at the repository
root (found by walking up for ``.chirograph``/``.git``/``.dvc``, so a ``chiro://`` read
resolves the same from any working subdirectory). The list is deliberately a *list* so
the future lookup adds entries here rather than reworking callers.

Manifests are named by their own whole-file digest, sharded the way chirograph
prescribes (``ChiroRef.shard_path`` → ``manifests/15/576e….chiro.json``). Digest naming
is not incidental: it lets multiple dated observations of one record coexist (the pair
*is* the drift record), makes ``md5sum .chirograph/manifests/**`` self-verifying with no
tooling, and makes concurrent writes collision-free (chirograph's format rationale §13).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from archivefs.chiro import _chirograph
from archivefs.errors import ManifestError

if TYPE_CHECKING:  # pragma: no cover
    from chirograph import ChiroRef

__all__ = [
    "STORE_DIRNAME",
    "as_ref",
    "resolve_ref",
    "store_roots",
    "write_to_store",
]

#: The repo-root directory that holds the manifest store (and, later, the resolution
#: config). ``manifests/`` lives inside it — the same parent chirograph's
#: ``shard_path`` assumes.
STORE_DIRNAME = ".chirograph"

#: Markers that identify a repository root when walking up from the cwd.
_ROOT_MARKERS = (STORE_DIRNAME, ".git", ".dvc")


def _repo_root(start: Path) -> Path | None:
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in _ROOT_MARKERS):
            return directory
    return None


def store_roots(start: str | Path | None = None) -> list[Path]:
    """The ordered list of store roots to search — today, just the repo-local one.

    Returns ``[<repo>/.chirograph]`` when a repository is found, else ``[]``. The list
    shape is the seam the DNS-style lookup grows into (a user-global store, a registry,
    a hosted base URL); nothing else needs to change when it does.
    """
    origin = Path(start).expanduser().resolve() if start else Path.cwd()
    root = _repo_root(origin)
    return [root / STORE_DIRNAME] if root is not None else []


def as_ref(text: str) -> ChiroRef | None:
    """Parse ``text`` as a chiro reference (``md5:<digest>`` or ``chiro://md5:<digest>``).

    Returns ``None`` for anything that is not a well-formed reference — a path, a bare
    word — so a caller can fall back to treating it as a filesystem path.
    """
    chirograph = _chirograph()
    candidate = text if text.startswith("chiro://") else f"chiro://{text}"
    try:
        return chirograph.ChiroRef.parse(candidate)
    except chirograph.ChirographError:  # RefError et al. — malformed, so not a ref
        return None


def resolve_ref(ref: ChiroRef, start: str | Path | None = None) -> Path:
    """Find the manifest for ``ref`` across the ordered stores, first hit wins.

    Verifies the found file's digest against the reference — the ``md5sum``-checkable
    guarantee that a digest name promises — so a corrupted or mis-named file is refused
    rather than silently trusted.
    """
    roots = store_roots(start)
    searched: list[str] = []
    for root in roots:
        candidate = root / ref.shard_path
        searched.append(str(candidate))
        if candidate.is_file():
            actual = hashlib.new(ref.algorithm, candidate.read_bytes()).hexdigest()
            if actual != ref.digest:
                raise ManifestError(
                    f"{candidate} is named for {ref.digest} but hashes to {actual} — "
                    f"the manifest has been edited or corrupted. Do not use it."
                )
            return candidate
    where = "\n  ".join(searched) if searched else "(no repository found from here)"
    raise ManifestError(
        f"no manifest for {ref} in any store. Looked in:\n  {where}\n"
        f"Generate it with `archivefs manifest`, or commit the .chirograph store."
    )


def write_to_store(
    manifest: Any, start: str | Path | None = None
) -> tuple[Path, ChiroRef]:
    """Write ``manifest`` into the local store, named by its whole-file digest.

    Idempotent by construction: the same manifest bytes hash to the same digest and so
    write the same path with the same content. Returns ``(path, ref)``.
    """
    chirograph = _chirograph()
    roots = store_roots(start)
    if not roots:
        raise ManifestError(
            "no repository here to hold a .chirograph store — run inside the repo "
            "(or pass an explicit --out path)."
        )
    text = chirograph.dumps(manifest)
    data = text.encode()
    ref = chirograph.ChiroRef.for_bytes(data)
    dest = roots[0] / ref.shard_path
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest, ref
