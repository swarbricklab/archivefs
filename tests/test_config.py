"""The layered config model.

Two things under test, and the second is the interesting one. Layering is
mechanical: four files, a fixed precedence, per-protocol sections. The **policy** is
not: a layer may only set settings it can honour, and "can honour" turns out to mean
"travels with the data if it changes the data". Most of these tests are about the
refusals, because a config model's characteristic failure is a setting that looks
applied and is not.
"""

from __future__ import annotations

import json

import pytest

from archivefs import config
from archivefs.errors import ConfigError
from archivefs.registry import PROTOCOLS


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """An isolated set of layers, and a project that looks like a repository."""
    system = tmp_path / "etc"
    user = tmp_path / "xdg"
    project = tmp_path / "proj"
    (project / ".git").mkdir(parents=True)
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(system))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(user))
    monkeypatch.delenv(config.CONFIG_ENV, raising=False)
    monkeypatch.chdir(project)

    def write(layer: str, data: dict) -> None:
        paths = {
            "system": system / config.XDG_NAME,
            "user": user / config.XDG_NAME,
            "project": project / config.PROJECT_NAME,
            "local": project / config.LOCAL_NAME,
        }
        path = paths[layer]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    write.root = project  # type: ignore[attr-defined]
    return write


# ---------------------------------------------------------------- the layers


def test_all_four_layers_are_found_in_precedence_order(tree):
    found = config.layers()
    assert [layer.name for layer in found] == ["system", "user", "project", "local"]
    # only the committed one may change what the data is
    assert [layer.travels for layer in found] == [False, False, True, False]


def test_outside_a_repository_there_is_no_project_layer(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "etc"))
    monkeypatch.chdir(tmp_path)
    assert [layer.name for layer in config.layers()] == ["system", "user"]


def test_project_discovery_stops_at_the_repository(tmp_path, monkeypatch):
    """Bounded on purpose.

    Without the stop, a stray `~/.archivefs.json` would quietly become every
    project's configuration — the invisible action the layer split exists to prevent.
    """
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    deep = tmp_path / "repo" / "a" / "b"
    deep.mkdir(parents=True)
    monkeypatch.chdir(deep)
    assert config.project_root() == (tmp_path / "repo").resolve()

    outside = tmp_path / "no-repo"
    outside.mkdir()
    monkeypatch.chdir(outside)
    assert config.project_root() is None


def test_xdg_config_dirs_puts_the_earliest_directory_highest(tmp_path, monkeypatch):
    """The spec's order, which is the opposite of the merge order."""
    first, second = tmp_path / "first", tmp_path / "second"
    for directory, value in ((first, 2), (second, 8)):
        path = directory / config.XDG_NAME
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"max_concurrent_reads": value}))
    monkeypatch.setenv("XDG_CONFIG_DIRS", f"{first}:{second}")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "none"))
    monkeypatch.chdir(tmp_path)
    assert config.settings("zenodo")["max_concurrent_reads"] == 2


def test_archivefs_config_replaces_the_user_layer(tmp_path, monkeypatch):
    """git's `GIT_CONFIG_GLOBAL` shape — one file, named explicitly."""
    explicit = tmp_path / "elsewhere.json"
    explicit.write_text(json.dumps({"cache_dir": "/from/env/file"}))
    monkeypatch.setenv(config.CONFIG_ENV, str(explicit))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "ignored"))
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "etc"))
    monkeypatch.chdir(tmp_path)
    assert config.settings("zenodo")["cache_dir"] == "/from/env/file"


# ------------------------------------------------------------ merge and scope


def test_a_higher_layer_wins_key_by_key(tree):
    tree("system", {"scratch_dir": "/site", "max_concurrent_reads": 4})
    tree("user", {"cache_dir": "/mine"})
    tree("local", {"scratch_dir": "/mine/fast"})
    resolved = config.settings("zenodo")
    assert resolved == {
        "scratch_dir": "/mine/fast",
        "max_concurrent_reads": 4,
        "cache_dir": "/mine",
    }


def test_a_protocol_section_beats_the_top_level_of_the_same_layer(tree):
    tree("user", {"max_concurrent_reads": 4, "zenodo": {"max_concurrent_reads": 6}})
    assert config.settings("zenodo")["max_concurrent_reads"] == 6
    assert config.settings("geo")["max_concurrent_reads"] == 4


def test_a_protocol_section_does_not_leak_to_other_protocols(tree):
    tree("project", {"zenodo": {"extract_zips": True}})
    assert config.settings("zenodo")["extract_zips"] is True
    assert "extract_zips" not in config.settings("geo")


def test_a_missing_file_is_not_an_error(tree):
    assert config.settings("zenodo") == {}


# ------------------------------------------------------------- the refusals


def test_a_layer_that_does_not_travel_may_not_change_the_data(tree):
    """The reason the whole classification exists.

    `--fs-config extract_zips=true` is recorded in the .dvc file and travels. The
    same setting from a user config leaves the .dvc file with no record of it, so the
    identical file resolves to a different tree on another machine — silently.
    """
    tree("user", {"extract_zips": True})
    with pytest.raises(ConfigError) as exc:
        config.settings("zenodo")
    assert "may not set 'extract_zips'" in str(exc.value)
    assert "--fs-config" in str(exc.value)
    assert config.PROJECT_NAME in str(exc.value)


def test_the_committed_layer_may_change_the_data(tree):
    tree("project", {"zenodo": {"extract_zips": True, "strip_components": 0}})
    assert config.settings("zenodo") == {"extract_zips": True, "strip_components": 0}


def test_the_committed_layer_may_not_name_a_machine(tree):
    """A committed absolute path is wrong for everyone who did not write it."""
    tree("project", {"cache_dir": "/scratch/mine"})
    with pytest.raises(ConfigError) as exc:
        config.settings("zenodo")
    assert "may not set 'cache_dir'" in str(exc.value)
    assert config.LOCAL_NAME in str(exc.value)


@pytest.mark.parametrize("layer", ["system", "user", "project", "local"])
@pytest.mark.parametrize("key", ["password", "token", "username"])
def test_a_credential_is_refused_from_every_layer(tree, layer, key):
    """No layer, committed or not. Credentials have their own file and their own
    permission check, and the config file has neither."""
    tree(layer, {key: "hunter2"})
    with pytest.raises(ConfigError) as exc:
        config.settings("ega")
    assert "credential" in str(exc.value)
    assert "docs/credentials.md" in str(exc.value)


def test_a_path_to_a_credential_is_not_a_credential(tree):
    """ssh has no `-o Password=`; it has `IdentityFile`, which is a path."""
    tree("user", {"ega": {"credentials_file": "~/.config/archivefs/ega-b.json"}})
    assert config.settings("ega")["credentials_file"].endswith("ega-b.json")


def test_an_unknown_key_fails_rather_than_being_ignored(tree):
    """The classic config bug: a typo that silently does nothing."""
    tree("user", {"cach_dir": "/x"})
    with pytest.raises(ConfigError) as exc:
        config.settings("zenodo")
    assert "unknown option 'cach_dir'" in str(exc.value)
    assert "cache_dir" in str(exc.value)  # the message lists what would work


def test_a_misspelled_protocol_section_is_caught(tree):
    """A section is a container, not a setting — but only for protocols that exist,
    or `"zendo": {...}` would be silently skipped as though it were one."""
    tree("user", {"zendo": {"api_url": "x"}})
    with pytest.raises(ConfigError) as exc:
        config.settings("zenodo")
    assert "'zendo'" in str(exc.value)


def test_an_option_the_protocol_does_not_have_is_caught_in_its_section(tree):
    tree("user", {"geo": {"s3_url": "https://example"}})
    with pytest.raises(ConfigError) as exc:
        config.settings("geo")
    assert "geo:// has no such option" in str(exc.value)


def test_a_top_level_key_may_apply_to_only_some_protocols(tree):
    """The natural reading of "top level": applies where it applies.

    Erroring here would make a sensible file impossible — you would have to section
    every setting even when you meant all of them.
    """
    tree("user", {"s3_url": "https://example"})
    assert config.settings("bia")["s3_url"] == "https://example"
    assert config.settings("geo")["s3_url"] == "https://example"


@pytest.mark.parametrize("body", ["not json", "[1, 2]", '"a string"'])
def test_an_unparseable_file_says_which_file_and_why(tree, tmp_path, body):
    path = tmp_path / "xdg" / config.XDG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    with pytest.raises(ConfigError) as exc:
        config.settings("zenodo")
    assert str(path) in str(exc.value)


# --------------------------------------------------------------- anti-drift


def test_every_backend_option_is_classified():
    """A new option cannot be added without deciding whether it changes the data.

    This is the guard that keeps the two key sets from drifting away from the
    registry, which is the only reason it is safe for them to be hand-written lists.
    """
    unclassified = {
        f"{name}.{key}"
        for name, backend in PROTOCOLS.items()
        for key in backend.config
        if key not in config.known_keys()
    }
    assert not unclassified, (
        f"classify these in archivefs.config as MACHINE_KEYS (cost) or CONTENT_KEYS "
        f"(what comes back): {sorted(unclassified)}"
    )


def test_a_credential_name_is_never_a_config_key():
    """The policy, not just the error message.

    `CREDENTIAL_HINTS` exists only to make the refusal readable. What actually keeps
    a secret out of a config file is that it is not a settable option at all — the
    same absence that makes `--fs-config password=…` a DVC config error.
    """
    import importlib

    for name, backend in PROTOCOLS.items():
        module_path, _, class_name = backend.class_path.rpartition(".")
        try:
            declared = getattr(importlib.import_module(module_path), class_name).credentials
        except (ImportError, AttributeError) as exc:
            pytest.skip(f"{name} does not import here: {exc}")
        for credential in declared:
            if credential.is_path:
                continue
            assert credential.name not in config.known_keys(), (
                f"{name}'s {credential.name!r} is a secret and must not be settable "
                f"from a config file"
            )
