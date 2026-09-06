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
            # As DVC would hand it over: a config file is INI, so every value is
            # a string on the way in, whatever the profile declares it to be.
            value = {bool: "true", int: "1"}.get(kind, "x")
            validated = SCHEMA["remote"][str]({"url": f"{name}://x", key: value})
            assert key in validated, f"{name}.{key} did not survive DVC validation"
            assert isinstance(validated[key], kind), (
                f"{name}.{key} reached the filesystem as {type(validated[key])}, "
                f"not {kind} — an INI string that is never coerced is the bug "
                f"`Backend.config`'s types exist to prevent"
            )


# --------------------------------------------------------------- the wrappers


@pytest.mark.parametrize("protocol", ["ega", "zenodo", "geo", "chiro"])
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


# -------------------------------------------------------- version-awareness
#
# Replaces `test_not_version_aware_even_when_dvc_asks`, which pinned the opposite
# and said in its own docstring that it was pinning a decision rather than a fact.
# The decision changed: see the corrections to decisions.md D6/D17/D20.


@pytest.mark.parametrize(
    ("protocol", "expected"),
    [("zenodo", True), ("ega", True), ("geo", False), ("bia", False), ("chiro", False)],
)
def test_version_awareness_follows_the_archive_profile(protocol, expected):
    """On where an immutable per-entry token exists, off where it does not.

    This is the whole reviewer route: `dvc/repo/index.py` registers the source URL
    as a fallback storage when `dep.fs.version_aware`, which is what lets a
    *completed* `dvc import-url` be pulled by someone with no DVC remote.

    Off for GEO and BIA is not a conservative default, it is a correctness
    requirement. `dvc/repo/fetch.py`'s `_log_unversioned` removes untokened
    entries from a fetch with only a warning, so switching those on would make
    `dvc pull` report success having silently skipped files.
    """
    from fsspec.registry import _import_class

    cls = _import_class(dvcfs.DVC_PROTOCOLS[protocol])
    fs = cls(url=f"{protocol}://x")
    assert fs.version_aware is expected
    # and it is the profile that decides, not config a user can pass
    assert (PROTOCOLS[protocol].immutability_basis is not None) is expected


def test_version_aware_cannot_be_switched_on_by_config():
    """`--fs-config version_aware=true` must not reach an archive that cannot honour it.

    It is filtered out of the config handed to the filesystem, and the property
    ignores it regardless — two independent reasons, because the consequence of
    getting it wrong is a silent partial fetch.
    """
    fs = dvcfs.GEODVCFileSystem(url="geo://GSE1", version_aware=True)
    assert fs.version_aware is False
    assert "version_aware" not in fs.fs_args


def test_version_path_is_the_identity():
    """Not a shim: a published record is immutable and its id is in the path.

    S3 needs `?versionId=` because a key is mutable. A Zenodo record id is not, so
    "the path at this version" is the path. Its *absence* is what broke the first
    attempt at this — a hard AttributeError from dvc/dependency/base.py, which
    `dvc_data/index/checkout.py` also asserts on.
    """
    fs = dvcfs.ZenodoDVCFileSystem(url="zenodo://1486184")
    assert fs.version_path("1486184/a.csv", "abc123") == "1486184/a.csv"
    assert fs.version_path("1486184/a.csv", None) == "1486184/a.csv"
    # nothing to reconcile, so whatever a caller arrives with it gets back
    assert fs.coalesce_version("1486184/a.csv", "10.5281/zenodo.1486184") == (
        "1486184/a.csv",
        "10.5281/zenodo.1486184",
    )


def test_an_untokened_file_is_refused_rather_than_dropped():
    """The dangerous case, made loud.

    A Zenodo file whose published digest is not an md5 yields no token. DVC's
    response would be to drop it from the fetch and log a warning, so `dvc pull`
    exits 0 having not written the file. We refuse instead, and the message says
    what to do rather than only what happened.
    """
    from archivefs.errors import NotSupportedError

    fs = dvcfs.ZenodoDVCFileSystem(url="zenodo://1486184")
    ok = {"name": "1486184/a.csv", "type": "file", "version_id": "abc"}
    assert fs._check_versioned([ok]) == [ok]

    bad = {"name": "1486184/b.csv", "type": "file", "version_id": None}
    with pytest.raises(NotSupportedError, match="no immutable version token"):
        fs._check_versioned([bad])
    # directories legitimately carry none, and DVC ignores theirs
    assert fs._check_versioned([{"name": "1486184", "type": "directory"}])


def test_the_check_covers_walk_because_that_is_how_directories_are_built(monkeypatch):
    """`dvc_data.index.build.safe_walk` calls the wrapper's walk with detail=True.

    Validating `info` alone would cover a single-file import and miss every member
    of a directory or an expanded ZIP — which is the case the whole change is for.
    """
    from archivefs.errors import NotSupportedError

    fs = dvcfs.ZenodoDVCFileSystem(url="zenodo://1486184")
    files = {"b.csv": {"name": "1486184/b.csv", "type": "file", "version_id": None}}
    monkeypatch.setattr(
        type(fs).__mro__[1],
        "walk",
        lambda self, path, **kw: iter([("1486184", {}, files)]),
    )
    with pytest.raises(NotSupportedError, match="no immutable version token"):
        list(fs.walk("1486184", detail=True))


def test_a_non_version_aware_archive_is_not_checked(monkeypatch):
    """GEO publishes no digest, so every entry lacks a token — and that is fine.

    The check must be scoped to archives that claimed they could produce one, or
    it would refuse every GEO import instead of the manifest-less ones.
    """
    fs = dvcfs.GEODVCFileSystem(url="geo://GSE1")
    files = {"a.txt": {"name": "GSE1/a.txt", "type": "file", "version_id": None}}
    monkeypatch.setattr(
        type(fs).__mro__[1],
        "walk",
        lambda self, path, **kw: iter([("GSE1", {}, files)]),
    )
    assert list(fs.walk("GSE1", detail=True)) == [("GSE1", {}, files)]


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


def test_turning_stripping_off_through_config_actually_turns_it_off():
    """`strip_components = 0` is the same INI trap wearing a different hat.

    `int("0")` is falsy but the string `"0"` is not, so an uncoerced value would
    read as "strip one level" from a user who had just written 0 — the failure
    mode `extract_zips = false` had, in the one setting whose whole purpose is
    to say *do not rearrange my tree*.
    """
    dvcfs.register()
    from dvc.config_schema import SCHEMA

    validated = SCHEMA["remote"][str](
        {"url": "zenodo://1486184", "strip_components": "0"}
    )
    assert validated["strip_components"] == 0
    assert not validated["strip_components"]


def test_a_negative_strip_depth_is_refused_by_dvc():
    """There is no sensible reading of -1, so it is a config error, not a clamp."""
    import voluptuous

    dvcfs.register()
    from dvc.config_schema import SCHEMA

    with pytest.raises(voluptuous.MultipleInvalid):
        SCHEMA["remote"][str]({"url": "zenodo://1486184", "strip_components": "-1"})


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


# ------------------------------------ the zero-config resume default (D29 follow-up)


def test_the_block_cache_default_sits_inside_the_dvc_cache(tmp_path, monkeypatch):
    """Zero-config resume: a directory beside DVC's own object store.

    Passed as ``cache_dir_default`` (a last resort), so nothing turns on unless the
    caller left ``cache_dir`` unset — and it lands on the filesystem the user already
    chose for large data, which is what let this be on by default at all.
    """
    from dvc.repo import Repo

    Repo.init(str(tmp_path), no_scm=True).close()
    monkeypatch.chdir(tmp_path)
    default = dvcfs._dvc_block_cache_dir()
    assert default == str(tmp_path / ".dvc" / "cache" / "archivefs")


def test_no_block_cache_default_outside_a_dvc_repo(tmp_path, monkeypatch):
    """Off where there is no cache to sit beside — so plain use never surprises."""
    monkeypatch.chdir(tmp_path)
    assert dvcfs._dvc_block_cache_dir() is None
