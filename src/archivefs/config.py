"""Layered configuration: where a setting comes from, and which settings may.

Three environment variables appeared in one afternoon (``ARCHIVEFS_CACHE_DIR``,
``ARCHIVEFS_SCRATCH_DIR``, ``ARCHIVEFS_CACHE_MAX_AGE_DAYS``) and that is where ad-hoc
stops scaling: a setting you have to remember to ``export`` is a setting you discover
you needed the day after it would have helped.

Four layers, XDG-shaped, lowest precedence first:

=========== =============================================== ==========
layer       file                                            travels?
=========== =============================================== ==========
``system``  ``$XDG_CONFIG_DIRS/archivefs/config.json``      no
``user``    ``$XDG_CONFIG_HOME/archivefs/config.json``      no
``project`` ``.archivefs.json`` at the repository root      **yes**
``local``   ``.archivefs.local.json`` beside it             no
=========== =============================================== ==========

with the environment above every file, and an explicit storage option above that::

    -o / --fs-config / kwarg  >  $ARCHIVEFS_*  >  local  >  project  >  user
                              >  system        >  built-in default

Same order :mod:`archivefs.credentials` uses for secrets — option, environment, file —
so learning it once is enough.

## Which settings a layer may set

**Only a layer that travels with the data may change what the data is.**

``--fs-config extract_zips=true`` is written into the ``.dvc`` file, so it travels and
a colleague's ``dvc pull`` reproduces the same tree. The same setting arriving from
``~/.config/archivefs/config.json`` would leave the ``.dvc`` file with no record of it,
and that identical file would then resolve to a *different tree* on a different machine
— silently. That is the reproducibility hole this project is arranged to avoid, reached
sideways through a convenience.

So keys are classified and a layer that cannot honour a class refuses it:

* :data:`MACHINE_KEYS` — cost, not content: where blocks are cached, how many
  connections, which mirror, which credentials *file*. Every layer but ``project`` may
  set them; ``project`` may not, because a committed ``/scratch`` path is wrong for
  everyone who did not write it.
* :data:`CONTENT_KEYS` — which bytes, or which names, come back: ``extract_zips``,
  ``strip_components``, ``view``, ``layout``, ``chiro``. Only ``project`` may set them,
  because only ``project`` is committed next to the data it describes.
* credentials are refused everywhere, and get a message saying so rather than "unknown
  option". They are absent from the registry's option tables by design
  (``registry.Backend.config``), so they are not settable here in the first place;
  path-valued *pointers* (``credentials_file``, ``ngc``) are not secrets and stay.

Unknown keys are refused. A typo that silently does nothing is the failure mode config
models are famous for; this one fails at load with the list of keys that would work.

JSON, not TOML: ``requires-python`` is 3.9 so there is no ``tomllib``, the CLI is
deliberately stdlib-only, and credential files are already JSON — one syntax to learn.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from archivefs.errors import ConfigError

logger = logging.getLogger(__name__)

__all__ = [
    "CONFIG_ENV",
    "CONTENT_KEYS",
    "MACHINE_KEYS",
    "Layer",
    "known_keys",
    "layers",
    "settings",
]

#: One file to use *as the user layer*, like git's ``GIT_CONFIG_GLOBAL``.
CONFIG_ENV = "ARCHIVEFS_CONFIG"

#: Path under an XDG config directory.
XDG_NAME = "archivefs/config.json"
#: Committed at the repository root; travels with a ``git clone``.
PROJECT_NAME = ".archivefs.json"
#: Beside it and *not* committed — the per-machine escape hatch inside one project.
LOCAL_NAME = ".archivefs.local.json"

#: Settings that change what a read *costs*, never what it returns.
MACHINE_KEYS = frozenset(
    {
        "cache_dir",
        "scratch_dir",
        "cache_max_age_days",
        "max_concurrent_reads",
        "block_size",
        "max_retries",
        "request_timeout",
        "transfer_timeout",
        "api_url",
        "base_url",
        "s3_url",
        "media_url",
        "credentials_file",
    }
)

#: Settings that change which bytes, or which names, come back.
CONTENT_KEYS = frozenset(
    {
        "extract_zips",
        "strip_components",
        "view",
        "layout",
        "chiro",
        "selection",
        "include_index_files",
        "zip_limits",
        # cbioportal: which datahub repo and git ref a study is read from — these
        # select *which bytes/version* come back, so they are committed with the data.
        "repo",
        "ref",
    }
)

#: Valid for every protocol but absent from the registry's option tables, because DVC
#: never needed them in a ``.dvc`` file: they are archivefs-level or base-class knobs.
#: Listed so that a key inside a protocol section can still be checked against *that*
#: protocol's options without these tripping the check.
UNIVERSAL_KEYS = frozenset(
    {
        "cache_max_age_days",
        "block_size",
        "max_retries",
        "request_timeout",
        "transfer_timeout",
    }
)

#: Only to make the error message useful. The *policy* is that these are not settable
#: options at all — see the module docstring — and
#: ``test_a_credential_name_is_never_a_config_key`` pins that against the backends.
CREDENTIAL_HINTS = frozenset({"username", "password", "token", "secret", "api_key"})


def known_keys() -> frozenset[str]:
    """Every key any layer may set. Not every layer may set every one of them."""
    return MACHINE_KEYS | CONTENT_KEYS


@dataclass(frozen=True)
class Layer:
    """One configuration file, whether or not it exists."""

    name: str
    path: Path
    #: committed alongside the data, so a setting here reaches everyone who clones
    travels: bool
    data: dict = field(default_factory=dict)
    exists: bool = False

    @property
    def allowed(self) -> frozenset[str]:
        return CONTENT_KEYS if self.travels else MACHINE_KEYS

    @property
    def refused(self) -> frozenset[str]:
        return MACHINE_KEYS if self.travels else CONTENT_KEYS


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser()


def _xdg_config_dirs() -> list[Path]:
    """``$XDG_CONFIG_DIRS``, earlier entries taking precedence, as the spec says."""
    raw = os.environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"
    return [Path(part).expanduser() for part in raw.split(os.pathsep) if part]


def project_root(start: Path | None = None) -> Path | None:
    """The nearest enclosing repository, or ``None`` if there is not one.

    Walks up until it finds a directory holding ``.git`` or ``.dvc`` and stops there,
    inclusive. Deliberately bounded: without the stop a stray ``~/.archivefs.json``
    would quietly become every project's configuration, which is the invisible-action
    problem the layer split exists to prevent.
    """
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() or (candidate / ".dvc").exists():
            return candidate
    return None


def _read(path: Path) -> tuple[dict, bool]:
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}, False
    except OSError as exc:
        raise ConfigError(f"{path} could not be read: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ConfigError(
            f"{path} is not valid JSON: {exc}. archivefs config files are JSON "
            f"objects; see docs/configuration.md for the shape."
        ) from exc
    if not isinstance(data, dict):
        raise ConfigError(
            f"{path} must hold a JSON object, not a {type(data).__name__}."
        )
    return data, True


def layers(start: Path | None = None) -> list[Layer]:
    """Every layer, **lowest precedence first**, whether or not its file exists."""
    found: list[Layer] = []
    # Reversed: XDG puts the highest-precedence directory first and this list runs
    # the other way, so `/etc/xdg:/usr/local/etc/xdg` means /etc/xdg wins.
    for directory in reversed(_xdg_config_dirs()):
        path = directory / XDG_NAME
        data, exists = _read(path)
        found.append(Layer("system", path, travels=False, data=data, exists=exists))
    override = os.environ.get(CONFIG_ENV)
    user_path = (
        Path(override).expanduser() if override else _xdg_config_home() / XDG_NAME
    )
    data, exists = _read(user_path)
    found.append(Layer("user", user_path, travels=False, data=data, exists=exists))
    root = project_root(start)
    if root is not None:
        for filename, travels, label in (
            (PROJECT_NAME, True, "project"),
            (LOCAL_NAME, False, "local"),
        ):
            path = root / filename
            data, exists = _read(path)
            found.append(Layer(label, path, travels=travels, data=data, exists=exists))
    return found


def _protocols() -> dict:
    from archivefs.registry import PROTOCOLS

    return PROTOCOLS


def _entries(layer: Layer, protocol: str) -> list[tuple[str, Any, bool]]:
    """``(key, value, sectioned)`` for one protocol: top level, then its own section.

    A protocol name at the top level is a *container*, not a setting, and is skipped —
    but only for protocols that exist, so that ``"zendo": {...}`` is still caught as a
    typo rather than quietly doing nothing.
    """
    protocols = _protocols()
    out: list[tuple[str, Any, bool]] = [
        (key, value, False)
        for key, value in layer.data.items()
        if key not in protocols
    ]
    section = layer.data.get(protocol)
    if isinstance(section, dict):
        out += [(key, value, True) for key, value in section.items()]
    return out


def _check(layer: Layer, key: str, protocol: str, sectioned: bool) -> None:
    """Refuse a key this layer may not set, loudly and with somewhere to put it."""
    if key in CREDENTIAL_HINTS:
        raise ConfigError(
            f"{layer.path} sets {key!r}, which is a credential. Credentials never "
            f"come from a config file: they come from the environment, or from a file "
            f"whose permissions are checked. See docs/credentials.md. A *path* to one "
            f"— `credentials_file`, `ngc` — is not a secret and is allowed here."
        )
    if key in layer.refused:
        if layer.travels:
            where = (
                f"it names a place on one machine, and {layer.path.name} is committed "
                f"— a path that travels is wrong for everyone who did not write it. "
                f"Put it in {LOCAL_NAME}, which is not committed, or in your user "
                f"config."
            )
        else:
            where = (
                f"it changes which bytes an archive returns, and this layer does not "
                f"travel with the data — the same .dvc file would resolve differently "
                f"on another machine with nothing to say why. Pass it per import "
                f"(`--fs-config {key}=…`, which DVC records in the .dvc file), or put "
                f"it in {PROJECT_NAME}, which is committed."
            )
        raise ConfigError(
            f"{layer.path} ({layer.name} layer) may not set {key!r}: {where}"
        )
    if key not in known_keys():
        raise ConfigError(
            f"{layer.path} sets unknown option {key!r}. A setting that is silently "
            f"ignored is worse than one that fails, so this is an error. Settable "
            f"from the {layer.name} layer: {', '.join(sorted(layer.allowed))}."
        )
    if sectioned and key not in UNIVERSAL_KEYS:
        options = _protocols()[protocol].config if protocol in _protocols() else {}
        if key not in options:
            raise ConfigError(
                f"{layer.path} sets {protocol}.{key!r}, but {protocol}:// has no such "
                f"option. Its options are: {', '.join(sorted(options))}."
            )


def settings(protocol: str, start: Path | None = None) -> dict:
    """Merged configuration for one protocol, the highest-precedence layer winning.

    Values are validated as they merge, so a bad key fails at filesystem construction
    — naming the file, the layer, and where the setting does belong — rather than
    being dropped on the floor.
    """
    resolved: dict[str, Any] = {}
    for layer in layers(start):
        if not layer.exists:
            continue
        for key, value, sectioned in _entries(layer, protocol):
            _check(layer, key, protocol, sectioned)
            resolved[key] = value
    return resolved
