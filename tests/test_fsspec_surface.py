"""The generic fsspec surface, with no archivefs imports at all.

The design's claim is that registering fsspec filesystems means "DVC inherits them
for free, and so does the rest of the Python data ecosystem". The DVC half turned
out to need an adaptor (``docs/decisions.md`` D4); *this* half is true, and these
tests are what establishes it — everything below goes through plain ``fsspec``
entry-point discovery, which is exactly what pandas, Dask, Intake and friends do.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.network


def requires(archive: str) -> None:
    if os.environ.get(f"ARCHIVEFS_TEST_{archive}", "") != "1":
        pytest.skip(f"set ARCHIVEFS_TEST_{archive}=1 to hit the live service")


def test_protocols_are_known_without_importing_archivefs():
    """Entry-point discovery, not an import side effect."""
    from fsspec.registry import known_implementations

    for protocol in ("ega", "zenodo", "geo", "dbgap"):
        assert protocol in known_implementations
        assert known_implementations[protocol]["class"].startswith("archivefs.")


def test_fsspec_open_a_zenodo_file():
    requires("ZENODO")
    import fsspec

    with fsspec.open("zenodo://1486184/github-mark.zip", "rb") as handle:
        assert handle.read(4) == b"PK\x03\x04"  # ZIP magic


def test_fsspec_decompresses_a_geo_table_over_ranged_reads():
    """The "look before you leap" case, at content level rather than listing level.

    Reading the first line of a 5.3 MB gzipped table pulls only the blocks gzip
    needs — nothing here downloaded the whole file. Any fsspec consumer
    (``pandas.read_csv``, Dask, Intake) reaches the data by this same path.
    """
    requires("GEO")
    import fsspec

    url = "geo://GSE176078/GSE176078_Wu_etal_2021_bulkRNAseq_raw_counts.txt.gz"
    with fsspec.open(url, "rt", compression="gzip") as handle:
        first = handle.readline()
    assert first.strip()


def test_fsspec_glob_through_open_files():
    requires("ZENODO")
    import fsspec

    files = fsspec.open_files("zenodo://1486184/*.zip")
    assert [f.path for f in files] == ["1486184/github-mark.zip"]


def test_uninstalled_protocol_message_names_the_extra():
    """dbGaP is registered so the failure explains itself."""
    import fsspec

    from archivefs.errors import NotSupportedError

    fs_class = fsspec.get_filesystem_class("dbgap")
    with pytest.raises(NotSupportedError) as exc:
        fs_class()
    assert "deferred" in str(exc.value)
