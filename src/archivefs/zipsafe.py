"""Safety checks for ZIP archives from arbitrary third parties.

``docs/design.md`` calls zip-slip "the one real security burden", and the reason
is specific: ``zipfile.extractall()`` sanitises member paths, but reading members
through fsspec and writing them to computed paths — which is exactly what DVC
does with a directory listing — inherits none of that protection. archivefs is
*designed* to be pointed at arbitrary public archives, including by other labs,
so the checks live here and are applied at **listing** time as well as at
extraction time. A path that must not be written must not be advertised either.

Checks, in order of how much they matter:

* **containment / zip slip** — absolute paths, ``..`` segments, Windows drive
  letters and backslash separators, all of which can escape the target;
* **symlink members** — a symlink whose target is outside the tree turns a later
  innocuous write into an escape;
* **zip bombs** — the declared uncompressed total and the compression ratio, both
  readable from the central directory, so this costs no download;
* **filesystem-illegal names** — NUL, reserved Windows device names, trailing
  dots/spaces: not a security problem, but a failed extraction halfway through
  a pull is worse than a refusal up front.

Nested ZIPs are treated as leaves: a member that is itself a ZIP is a file, not
a directory, unless the caller explicitly asks to recurse.
"""

from __future__ import annotations

import os
import posixpath
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from archivefs.errors import ArchiveFSError, UnsafeArchiveMemberError

__all__ = [
    "ZipLimits",
    "check_member_name",
    "contained_path",
    "inspect_zip",
    "is_safe_member_name",
    "safe_extract",
]

#: Windows reserved device names; a file called ``aux`` cannot be created there.
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

_ILLEGAL_CHARS = re.compile(r'[\x00-\x1f<>:"|?*]')


@dataclass(frozen=True)
class ZipLimits:
    """Bounds for the zip-bomb check, all read from the central directory.

    Defaults are deliberately generous — genomics deposits *are* big — while
    still catching the classic 42 KB → 4.5 PB case.
    """

    #: total declared uncompressed bytes
    max_total_uncompressed: int = 2 * 1024**4  # 2 TiB
    #: per-member compression ratio (uncompressed / compressed)
    max_ratio: float = 1000.0
    #: how many members before we call it pathological
    max_members: int = 500_000
    #: ignore the ratio check for members this small (tiny files skew ratios)
    ratio_floor_bytes: int = 1024 * 1024


class ZipBombError(ArchiveFSError):
    """A ZIP's declared contents are implausible enough to refuse."""


def is_safe_member_name(name: str) -> bool:
    try:
        check_member_name(name)
    except UnsafeArchiveMemberError:
        return False
    return True


def check_member_name(name: str, target: str = "<target>") -> str:
    """Validate a ZIP member name, returning its normalised relative form.

    Raises :class:`~archivefs.errors.UnsafeArchiveMemberError` for anything that
    could land outside the extraction directory.
    """
    if not name or name in (".", "./"):
        raise UnsafeArchiveMemberError(name, target)
    if "\x00" in name:
        raise UnsafeArchiveMemberError(name, target)

    # ZIP says forward slashes, but archives written on Windows carry
    # backslashes, and a POSIX-only reading of "a\\..\\..\\x" looks harmless.
    candidate = name.replace("\\", "/")

    if candidate.startswith("/") or PureWindowsPath(name).is_absolute():
        raise UnsafeArchiveMemberError(name, target)
    if re.match(r"^[A-Za-z]:", candidate):  # bare drive letter, e.g. "C:data"
        raise UnsafeArchiveMemberError(name, target)

    parts = [p for p in PurePosixPath(candidate).parts if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise UnsafeArchiveMemberError(name, target)
    if not parts:
        raise UnsafeArchiveMemberError(name, target)
    return posixpath.join(*parts)


def check_member_portability(name: str) -> list[str]:
    """Non-fatal complaints about a member name.

    Returned rather than raised: an archive that cannot be extracted *on
    Windows* is still perfectly extractable here, and refusing it would be
    overreach. Callers surface these as warnings.
    """
    problems = []
    for part in PurePosixPath(name.replace("\\", "/")).parts:
        stem = part.split(".")[0].lower()
        if stem in _WINDOWS_RESERVED:
            problems.append(f"{part!r} is a reserved device name on Windows")
        if _ILLEGAL_CHARS.search(part):
            problems.append(f"{part!r} contains characters illegal on Windows")
        if part != part.rstrip(". "):
            problems.append(f"{part!r} ends with a dot or space (illegal on Windows)")
    return problems


def contained_path(target: str | os.PathLike, member: str) -> Path:
    """Resolve ``member`` inside ``target``, refusing anything that escapes.

    The check is done on *resolved* paths, so it also catches escapes through an
    existing symlink in the target tree — which a purely lexical check misses.
    """
    root = Path(target).resolve()
    relative = check_member_name(member, str(root))
    destination = (root / relative).resolve()
    if destination != root and root not in destination.parents:
        raise UnsafeArchiveMemberError(member, str(root))
    return destination


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    if info.create_system != 3:  # 3 == Unix; other systems have no symlink bit
        return False
    return stat.S_ISLNK(info.external_attr >> 16)


def inspect_zip(
    zf: zipfile.ZipFile,
    limits: ZipLimits | None = None,
    *,
    target: str = "<target>",
) -> dict:
    """Vet a ZIP from its central directory alone (no payload read).

    Returns a report with the safe members, and raises on anything that must not
    proceed. This is the function to call *before* listing an archive's interior
    to a consumer that will later write those paths to disk.
    """
    limits = limits or ZipLimits()
    infos = zf.infolist()
    if len(infos) > limits.max_members:
        raise ZipBombError(
            f"archive declares {len(infos)} members, above the "
            f"{limits.max_members} limit; refusing to treat it as a directory. "
            f"Import it as a single opaque file instead."
        )

    total = 0
    members: dict[str, zipfile.ZipInfo] = {}
    warnings: list[str] = []
    for info in infos:
        if info.is_dir():
            continue
        if _is_symlink(info):
            raise UnsafeArchiveMemberError(
                f"{info.filename} (symlink)",
                target,
            )
        name = check_member_name(info.filename, target)
        total += info.file_size
        if (
            info.compress_size
            and info.file_size > limits.ratio_floor_bytes
            and info.file_size / info.compress_size > limits.max_ratio
        ):
            raise ZipBombError(
                f"member {info.filename!r} declares a compression ratio of "
                f"{info.file_size / info.compress_size:.0f}:1 "
                f"({info.compress_size} -> {info.file_size} bytes), above the "
                f"{limits.max_ratio:.0f}:1 limit."
            )
        warnings.extend(check_member_portability(info.filename))
        members[name] = info

    if total > limits.max_total_uncompressed:
        raise ZipBombError(
            f"archive declares {total} uncompressed bytes, above the "
            f"{limits.max_total_uncompressed} limit. Raise ZipLimits explicitly "
            f"if this deposit really is that large."
        )

    return {
        "members": members,
        "total_uncompressed": total,
        "member_count": len(members),
        "warnings": warnings,
    }


def safe_extract(
    zf: zipfile.ZipFile,
    target: str | os.PathLike,
    members: list[str] | None = None,
    limits: ZipLimits | None = None,
    *,
    verify_crc: bool = True,
) -> list[Path]:
    """Extract with containment enforced, returning the paths written.

    Uses :meth:`zipfile.ZipFile.extract`, which sanitises names itself, *and*
    checks containment ourselves — belt and braces, because the design doc's rule
    is "either route through extractall() or validate every resolved path", and
    doing both costs nothing.
    """
    report = inspect_zip(zf, limits, target=str(target))
    root = Path(target)
    root.mkdir(parents=True, exist_ok=True)

    wanted = report["members"] if members is None else {}
    if members is not None:
        for member in members:
            normalised = check_member_name(member, str(root))
            if normalised not in report["members"]:
                raise ArchiveFSError(f"{member!r} is not in this archive")
            wanted[normalised] = report["members"][normalised]

    if verify_crc:
        bad = zf.testzip()
        if bad is not None:
            raise ArchiveFSError(
                f"CRC check failed for member {bad!r}: the archive is corrupt. "
                f"Re-fetch it; if it fails again the deposit itself is damaged."
            )

    written = []
    for name, info in wanted.items():
        destination = contained_path(root, name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as source, destination.open("wb") as sink:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                sink.write(chunk)
        written.append(destination)
    return written
