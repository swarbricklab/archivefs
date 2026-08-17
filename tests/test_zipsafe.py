"""ZIP safety: containment, symlinks, bombs, portability, corruption.

These are the checks that let archivefs be pointed at an *arbitrary* public
archive, which is the stated use case.
"""

from __future__ import annotations

import io
import stat
import zipfile

import pytest

from archivefs.errors import ArchiveFSError, UnsafeArchiveMemberError
from archivefs.zipsafe import (
    ZipBombError,
    ZipLimits,
    check_member_name,
    check_member_portability,
    contained_path,
    inspect_zip,
    is_safe_member_name,
    safe_extract,
)


def make_zip(
    entries: dict[str, bytes],
    symlinks: dict[str, str] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
        for name, target in (symlinks or {}).items():
            info = zipfile.ZipInfo(name)
            info.create_system = 3  # Unix
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            zf.writestr(info, target)
    return buffer.getvalue()


# ----------------------------------------------------------------- containment


@pytest.mark.parametrize(
    "name",
    [
        "../escape.txt",
        "a/../../escape.txt",
        "/etc/passwd",
        "C:/Windows/system32/x",
        "C:data",
        "..\\..\\escape.txt",
        "a\\..\\..\\escape.txt",
        "",
        ".",
        "with\x00nul",
    ],
)
def test_unsafe_names_refused(name):
    assert not is_safe_member_name(name)
    with pytest.raises(UnsafeArchiveMemberError):
        check_member_name(name)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("counts/matrix.mtx", "counts/matrix.mtx"),
        ("./counts/matrix.mtx", "counts/matrix.mtx"),
        ("a/b/../b/c.txt", None),  # a ".." anywhere is refused, even if harmless
        ("deep/nested/path/file.h5ad", "deep/nested/path/file.h5ad"),
        ("spaces in name.txt", "spaces in name.txt"),
        ("unicode-ᛒᚨᛗ.txt", "unicode-ᛒᚨᛗ.txt"),
    ],
)
def test_safe_names(name, expected):
    if expected is None:
        with pytest.raises(UnsafeArchiveMemberError):
            check_member_name(name)
    else:
        assert check_member_name(name) == expected


def test_contained_path_resolves_inside(tmp_path):
    assert contained_path(tmp_path, "a/b.txt") == (tmp_path / "a/b.txt").resolve()


def test_contained_path_catches_escape_through_an_existing_symlink(tmp_path):
    """A lexical check passes this; a resolved check does not."""
    outside = tmp_path / "outside"
    outside.mkdir()
    target = tmp_path / "target"
    target.mkdir()
    (target / "link").symlink_to(outside)
    with pytest.raises(UnsafeArchiveMemberError):
        contained_path(target / "link", "../../outside/evil.txt")


# -------------------------------------------------------------------- symlinks


def test_symlink_members_are_refused():
    data = make_zip({"ok.txt": b"fine"}, symlinks={"evil": "/etc/passwd"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf, pytest.raises(
        UnsafeArchiveMemberError
    ) as exc:
        inspect_zip(zf)
    assert "symlink" in str(exc.value)


# ----------------------------------------------------------------------- bombs


def test_zip_bomb_ratio_refused():
    data = make_zip({"bomb.bin": b"\0" * (8 * 1024 * 1024)})
    with zipfile.ZipFile(io.BytesIO(data)) as zf, pytest.raises(ZipBombError) as exc:
        inspect_zip(zf, ZipLimits(max_ratio=10, ratio_floor_bytes=1024))
    assert "compression ratio" in str(exc.value)


def test_zip_bomb_total_size_refused():
    data = make_zip({"a.bin": b"x" * 100_000})
    with zipfile.ZipFile(io.BytesIO(data)) as zf, pytest.raises(ZipBombError) as exc:
        inspect_zip(zf, ZipLimits(max_total_uncompressed=1000))
    assert "uncompressed bytes" in str(exc.value)


def test_member_count_limit_refused():
    data = make_zip({f"f{i}.txt": b"x" for i in range(50)})
    with zipfile.ZipFile(io.BytesIO(data)) as zf, pytest.raises(ZipBombError):
        inspect_zip(zf, ZipLimits(max_members=10))


def test_generous_defaults_pass_a_realistic_deposit():
    """Real genomics ZIPs compress well; the defaults must not cry wolf."""
    data = make_zip(
        {
            "counts/matrix.mtx": b"1 2 3\n" * 200_000,  # highly compressible
            "counts/barcodes.tsv": b"CELL\n" * 50_000,
        }
    )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        report = inspect_zip(zf)
    assert report["member_count"] == 2


# ----------------------------------------------------------------- portability


def test_portability_problems_are_warnings_not_errors():
    data = make_zip({"aux.txt": b"x", "trailing. ": b"y", "fine.txt": b"z"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        report = inspect_zip(zf)
    assert report["member_count"] == 3  # still usable here
    assert any("reserved device name" in w for w in report["warnings"])
    assert any("dot or space" in w for w in report["warnings"])


def test_portability_check_is_pure():
    assert check_member_portability("counts/matrix.mtx") == []


# ---------------------------------------------------------------- extraction


def test_safe_extract_writes_everything(tmp_path):
    data = make_zip({"a/b.txt": b"one", "c.txt": b"two"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        written = safe_extract(zf, tmp_path / "out")
    assert sorted(p.name for p in written) == ["b.txt", "c.txt"]
    assert (tmp_path / "out/a/b.txt").read_bytes() == b"one"


def test_safe_extract_subset(tmp_path):
    data = make_zip({"a/b.txt": b"one", "c.txt": b"two"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        written = safe_extract(zf, tmp_path / "out", members=["c.txt"])
    assert [p.name for p in written] == ["c.txt"]
    assert not (tmp_path / "out/a").exists()


def test_safe_extract_refuses_a_slipping_archive(tmp_path):
    """Built by hand: zipfile.writestr sanitises, real attackers do not."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        info = zipfile.ZipInfo("../escaped.txt")
        zf.writestr(info, b"pwned")
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as zf:
        assert zf.namelist() == ["../escaped.txt"]  # the name really is that
        with pytest.raises(UnsafeArchiveMemberError):
            safe_extract(zf, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()


def test_safe_extract_detects_corruption(tmp_path):
    # Stored, not deflated, so a flip at a known offset certainly lands in the
    # payload rather than in a header or the central directory.
    payload = b"hello world" * 500
    data = bytearray(make_zip({"a.txt": payload}, compression=zipfile.ZIP_STORED))
    data[1000] ^= 0xFF
    with zipfile.ZipFile(io.BytesIO(bytes(data))) as zf, pytest.raises(
        ArchiveFSError
    ) as exc:
        safe_extract(zf, tmp_path / "out")
    assert "corrupt" in str(exc.value).lower()


def test_nested_zip_is_a_leaf(tmp_path):
    inner = make_zip({"deep.txt": b"deep"})
    data = make_zip({"inner.zip": inner, "top.txt": b"top"})
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        report = inspect_zip(zf)
    assert set(report["members"]) == {"inner.zip", "top.txt"}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        safe_extract(zf, tmp_path / "out")
    assert (tmp_path / "out/inner.zip").read_bytes() == inner
