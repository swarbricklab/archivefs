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
voluptuous = pytest.importorskip("voluptuous")

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
    """It is deferred; advertising it to DVC would promise a backend that refuses.

    Now a consequence of `dbgap`'s profile setting no `dvc_wrapper`, rather than of
    remembering to leave it out of a second dict here.
    """
    dvcfs.register()
    from dvc_objects.fs import known_implementations

    assert "dbgap" in PROTOCOLS
    assert "dbgap" not in dvcfs.DVC_PROTOCOLS
    assert "dbgap" not in known_implementations


def test_the_dvc_tables_are_derived_from_the_profiles():
    """These were four parallel protocol-keyed dicts, and they drifted.

    Nothing here should be maintainable independently of `registry.PROTOCOLS`, so
    assert the correspondence rather than the contents: a new archive with a
    profile is registered by adding the profile, and one without a wrapper is
    absent from DVC without a second decision being recorded anywhere.
    """
    from fsspec.registry import _import_class

    expected = {
        name for name, backend in PROTOCOLS.items() if backend.dvc_wrapper
    }
    assert set(dvcfs.DVC_PROTOCOLS) == expected

    for name in expected:
        backend = PROTOCOLS[name]
        cls = _import_class(dvcfs.DVC_PROTOCOLS[name])
        assert cls.__name__ == backend.dvc_wrapper
        assert cls.protocol == name
        # and the class it will build is the one *this* profile names — not a path
        # recorded in a second place that could point elsewhere. Asserted through
        # _WRAPPERS rather than by touching `.fs`, because instantiating the EGA
        # filesystem resolves credentials and would fail here for the wrong reason.
        assert dvcfs._WRAPPERS[backend.dvc_wrapper] == (name, backend.class_path)

    # `_WRAPPERS` is keyed by wrapper *name*, so two profiles sharing one would
    # silently collapse into a single class serving the wrong protocol.
    wrappers = [b.dvc_wrapper for b in PROTOCOLS.values() if b.dvc_wrapper]
    assert len(wrappers) == len(set(wrappers))


def test_every_profile_option_reaches_dvcs_schema():
    """A key in a profile that DVC does not accept is refused before we run.

    So a profile option that fails to reach the schema is not a cosmetic problem:
    the option simply cannot be used, and the error names DVC's config rather than
    anything about archivefs.
    """
    dvcfs.register()
    from dvc.config_schema import SCHEMA

    for name, backend in PROTOCOLS.items():
        if not backend.dvc_wrapper:
            continue
        for key, kind in backend.config.items():
            value = "true" if kind is bool else "x"
            validated = SCHEMA["remote"][str]({"url": f"{name}://x", key: value})
            assert key in validated, f"{name}.{key} did not survive DVC validation"


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
    """Our version tokens are DOIs, not cloud object versions (decisions.md D6).

    Two things depend on this staying False, and both fail *silently*:

    * `dvc/repo/imp_url.py` sets `outs[0].can_push = False` for a version-aware
      dependency. Flipping this on writes `push: false` into every `.dvc` and
      `dvc push` then stops pushing archive data — so every collaborator's
      `dvc pull` breaks, with no error at import time (decisions.md D17).
    * A version-aware filesystem must also provide `version_path`; without it
      `dvc import-url` dies with `object has no attribute 'version_path'`.

    **This pins a decision, not a fact about DVC.** Setting it True *does* buy the
    thing it looks like it should — pulling a completed import from the archive, an
    expanded ZIP included — provided each entry also carries a `version_id`, which the
    D17 experiment omitted. See the corrections in D6/D17/D20 and issue #1. Until that
    lands the wrappers are hard-False, and this test says so; when it lands, replace
    this test rather than deleting it, because `push: false` above stays true.
    """
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
        {"url": "ega://EGAD1", "credentials_file": "~/ega.json", "layout": "display"}
    )
    assert SCHEMA["remote"][str]({"url": "geo://GSE1", "view": "supplementary"})


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("ega://EGAD1", "password"),
        ("ega://EGAD1", "username"),
        ("zenodo://1486184", "token"),
    ],
)
def test_dvc_itself_refuses_a_credential_in_remote_config(url, key):
    """The invariant is a config parse error, not a convention.

    These keys are absent from every profile's `config` on purpose, so DVC's own
    validator rejects them — which covers `dvc remote modify`, `.dvc/config`,
    `.dvc/config.local` and `--fs-config` in one go, all before archivefs runs.
    Putting any of them back would make `--fs-config password=…` writable into
    the committed .dvc file's deps[].fs_config again.
    """
    dvcfs.register()
    from dvc.config_schema import SCHEMA

    with pytest.raises(voluptuous.MultipleInvalid, match="extra keys not allowed"):
        SCHEMA["remote"][str]({"url": url, key: "sekrit"})


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


def test_boolean_config_values_are_coerced_not_passed_as_strings():
    """A DVC config file is INI, so `extract_zips = false` arrives as "false".

    Registered as a bare object, that string reached the filesystem and was
    truthy: ZIPs were presented as directories against the user's instruction.
    The fix is that the profile records `extract_zips: bool` and `_remote_schema`
    turns that into DVC's own `Bool` validator — so the type is declared next to
    the key rather than in a second set this one has to stay in step with.
    """
    dvcfs.register()
    from dvc.config_schema import SCHEMA

    validated = SCHEMA["remote"][str](
        {"url": "zenodo://1486184", "extract_zips": "false"}
    )
    assert validated["extract_zips"] is False
    validated = SCHEMA["remote"][str](
        {"url": "zenodo://1486184", "extract_zips": "true"}
    )
    assert validated["extract_zips"] is True


def test_the_module_imports_without_dvc_installed(tmp_path):
    """The default install has no DVC, and `archivefs info` must still say so.

    Building a wrapper class needs dvc_objects, so the classes are built on first
    attribute access rather than at import — otherwise importing this module (which
    `archivefs info` does, to report DVC's absence) raises ImportError.
    """
    import os
    import subprocess
    import sys

    code = (
        "import sys, importlib\n"
        # hide DVC from the import system entirely
        "for name in list(sys.modules):\n"
        "    if name.split('.')[0] in ('dvc', 'dvc_objects'):\n"
        "        del sys.modules[name]\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('dvc', 'dvc_objects'):\n"
        "            raise ImportError('blocked')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Block())\n"
        "import archivefs.dvcfs as d\n"
        "print('imported', sorted(d.DVC_PROTOCOLS))\n"
        "print('wrappers advertised', 'ZenodoDVCFileSystem' in dir(d))\n"
    )
    env = {**os.environ, "ARCHIVEFS_NO_DVC_HOOK": "1"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    # derived from the registry, so adding a backend does not edit this test
    assert f"imported {sorted(dvcfs.DVC_PROTOCOLS)}" in result.stdout
    assert "wrappers advertised True" in result.stdout


def test_unknown_attributes_still_raise_attribute_error():
    with pytest.raises(AttributeError):
        dvcfs.NoSuchFileSystem  # noqa: B018
