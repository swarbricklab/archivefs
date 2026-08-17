"""The DVC adaptor.

These tests exist because the design doc's assumption — "DVC delegates storage to
fsspec, so DVC inherits our filesystems for free" — is false in practice, and the
ways in which it is false are silent. Each test below pins one of them, so a future
DVC release that changes the situation fails here rather than in someone's pull.

``dvc`` is an optional dependency, so the whole module skips without it.
"""

from __future__ import annotations

import pytest

dvc = pytest.importorskip("dvc")

from archivefs import dvcfs  # noqa: E402
from archivefs.registry import PROTOCOLS  # noqa: E402


@pytest.fixture(autouse=True)
def restore_dvc_tables():
    """Undo registration between tests: it mutates another library's globals."""
    from dvc import config_schema
    from dvc_objects.fs import known_implementations

    saved_known = dict(known_implementations)
    saved_schemas = dict(config_schema.REMOTE_SCHEMAS)
    saved_remote = config_schema.SCHEMA["remote"]
    yield
    known_implementations.clear()
    known_implementations.update(saved_known)
    config_schema.REMOTE_SCHEMAS.clear()
    config_schema.REMOTE_SCHEMAS.update(saved_schemas)
    config_schema.SCHEMA["remote"] = saved_remote


# ------------------------------------------------------- the false assumption


def _unregistered(code: str) -> str:
    """Run ``code`` in a fresh interpreter with the start-up hook opted out.

    Needed because in a *working* environment the hook has already registered by
    the time a test runs, so the un-registered behaviour — the thing worth pinning
    — is not observable in-process.
    """
    import os
    import subprocess
    import sys

    environment = {**os.environ, "ARCHIVEFS_NO_DVC_HOOK": "1"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=environment,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_dvc_does_not_read_fsspec_entry_points():
    """The fact that makes this whole module necessary.

    fsspec knows our protocols from entry points alone; DVC has its own table and
    no plugin mechanism for it.
    """
    output = _unregistered(
        "from fsspec.registry import known_implementations as f\n"
        "from dvc_objects.fs import known_implementations as d\n"
        "print('zenodo' in f, 'zenodo' in d)"
    )
    assert output == "True False"


def test_unknown_scheme_is_rejected_by_the_config_schema():
    """Loud failure, not a silent local-path fallback — worth knowing which it is."""
    output = _unregistered(
        "from dvc.config_schema import SCHEMA, Invalid\n"
        "try:\n"
        "    SCHEMA['remote'][str]({'url': 'zenodo://1486184'})\n"
        "    print('accepted')\n"
        "except Invalid as exc:\n"
        "    print('rejected:', exc)"
    )
    assert output.startswith("rejected:")
    assert "Unsupported URL type" in output


def test_dvc_registry_falls_back_to_local_for_unknown_schemes():
    """The trap: dvc_objects' Registry does not raise on an unknown protocol.

    Nothing in archivefs relies on this, but it is why register() must patch the
    schema *and* the implementation table: half a registration would mean a URL
    silently resolving as a local path.
    """
    from dvc_objects.fs import registry
    from dvc_objects.fs.local import LocalFileSystem

    assert registry["definitely-not-a-protocol"] is LocalFileSystem


# ------------------------------------------------------------------- register


def test_register_makes_the_schema_accept_our_urls():
    dvcfs.register()
    from dvc.config_schema import SCHEMA

    for protocol in dvcfs.DVC_PROTOCOLS:
        assert SCHEMA["remote"][str]({"url": f"{protocol}://x"})


def test_register_is_idempotent():
    assert dvcfs.register() == dvcfs.register()


def test_register_rebuilds_the_validator_not_just_the_table():
    """ByUrl snapshots the schema table, so mutating it alone does nothing."""
    from dvc import config_schema

    before = config_schema.SCHEMA["remote"]
    dvcfs.register()
    assert config_schema.SCHEMA["remote"] is not before


def test_dbgap_is_not_registered_with_dvc():
    """It is deferred; advertising it to DVC would promise a backend that refuses."""
    dvcfs.register()
    from dvc_objects.fs import known_implementations

    assert "dbgap" in PROTOCOLS
    assert "dbgap" not in dvcfs.DVC_PROTOCOLS
    assert "dbgap" not in known_implementations


# --------------------------------------------------------------- the wrappers


@pytest.mark.parametrize("protocol", ["ega", "zenodo", "geo"])
def test_wrapper_declares_md5_as_the_checksum_field(protocol):
    """This one line is what makes `import-url --no-download` record a real hash."""
    from fsspec.registry import _import_class

    cls = _import_class(dvcfs.DVC_PROTOCOLS[protocol])
    assert cls.PARAM_CHECKSUM == "md5"
    assert cls.protocol == protocol


def test_strip_protocol_delegates_to_the_fsspec_class():
    """DVC's default is the identity, which makes every relpath come out "../..".

    Caught by `dvc list-url` printing ../../1486184/github-mark.zip.
    """
    assert dvcfs.ZenodoDVCFileSystem._strip_protocol("zenodo://1486184") == "1486184"
    assert (
        dvcfs.EGADVCFileSystem._strip_protocol("ega://EGAD1/EGAF2") == "EGAD1/EGAF2"
    )


def test_url_parts_are_not_treated_as_config():
    """fsspec's infer_storage_options reads an accession as a hostname."""
    assert dvcfs.ZenodoDVCFileSystem._get_kwargs_from_urls("zenodo://1486184") == {}


def test_unstrip_protocol_round_trips():
    fs = dvcfs.ZenodoDVCFileSystem(url="zenodo://1486184")
    assert fs.unstrip_protocol("1486184/a.csv") == "zenodo://1486184/a.csv"


def test_not_version_aware_even_when_dvc_asks():
    """Our version tokens are DOIs, not cloud object versions (decisions.md D6)."""
    fs = dvcfs.ZenodoDVCFileSystem(url="zenodo://1486184", version_aware=True)
    assert fs.version_aware is False
    # and an old .dvc carrying a version_id must not crash
    assert fs.coalesce_version("1486184/a.csv", "10.5281/zenodo.1486184") == (
        "1486184/a.csv",
        "10.5281/zenodo.1486184",
    )


def test_config_is_filtered_before_reaching_the_filesystem():
    fs = dvcfs.ZenodoDVCFileSystem(
        url="zenodo://1486184", jobs=4, checksum_jobs=2, extract_zips=True
    )
    args = fs.fs_args
    assert "url" not in args
    assert "jobs" not in args
    assert args["extract_zips"] is True


def test_backend_options_are_accepted_by_the_dvc_config_schema():
    """A remote's config must be able to carry the options a backend needs."""
    dvcfs.register()
    from dvc.config_schema import SCHEMA

    assert SCHEMA["remote"][str](
        {"url": "ega://EGAD1", "username": "me@example.org", "layout": "display"}
    )
    assert SCHEMA["remote"][str]({"url": "geo://GSE1", "view": "supplementary"})


# ------------------------------------------------------------------ the hook


def test_hook_is_installable_and_removable(monkeypatch, tmp_path):
    monkeypatch.setattr(dvcfs, "_site_packages", lambda: tmp_path)
    path = dvcfs.enable()
    assert path.exists()
    assert "archivefs.dvcfs_hook" in path.read_text()
    assert dvcfs.status(probe=False)["hook_installed"] is True
    assert dvcfs.disable() == path
    assert not path.exists()
    assert dvcfs.disable() is None  # idempotent


def test_hook_never_raises_even_with_a_broken_import(monkeypatch):
    """A .pth that throws breaks every interpreter in the environment."""
    import archivefs.dvcfs_hook as hook

    def explode() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(hook.sys, "modules", {hook.TRIGGER: object()})
    monkeypatch.setattr(dvcfs, "register", explode)
    hook.install()  # must not raise


def test_status_probe_reports_what_actually_works():
    report = dvcfs.status()
    assert report["dvc_installed"] is True
    assert sorted(report["registered"]) == sorted(dvcfs.DVC_PROTOCOLS)
    assert report["deferred"] == ["dbgap"]
