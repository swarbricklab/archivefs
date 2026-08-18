"""The DVC adaptor — because "DVC delegates to fsspec" is only half true.

``docs/design.md`` says: "DVC delegates storage to **fsspec** … So the deliverable
is a set of fsspec filesystems registered under custom protocols; DVC inherits them
for free." Measured against DVC 3.67, the first sentence is true and the conclusion
is **not**. What is actually the case (``docs/decisions.md`` D4):

* DVC exposes *itself* to fsspec via an ``fsspec.specs`` entry point, but it does
  **not consume** that entry-point group. Protocols come from a plain dict,
  ``dvc_objects.fs.known_implementations``, with no plugin mechanism.
* Before that dict is even consulted, ``dvc.config_schema`` rejects unknown URL
  schemes outright ("Unsupported URL type zenodo://"), and its validator snapshots
  the schema table at import time, so adding an entry later is not enough.
* DVC wraps every fsspec filesystem in its own ``dvc_objects.fs.base.FileSystem``,
  which is where ``PARAM_CHECKSUM`` — the field DVC reads as the content hash —
  is declared. That declaration is what makes ``import-url --no-download`` record
  a real md5 for us instead of nothing.
* Worst of all, an unknown scheme does not raise from the registry: ``Registry``
  falls back to ``LocalFileSystem``, so a typo'd or unregistered scheme can be
  silently treated as a *local path*.

So the adaptor is small but not optional: three thin ``FileSystem`` subclasses and
a :func:`register` that patches the two DVC tables. Nothing here monkey-patches at
import time — a library that mutates another library's globals on import is a
menace — so something has to call :func:`register` inside the ``dvc`` process.
:func:`enable`/``archivefs dvc enable`` writes a ``.pth`` into the active
environment's ``site-packages`` to do that, which is the only mechanism that works
without patching DVC itself. ``archivefs dvc status`` reports whether it is on.
"""

from __future__ import annotations

import logging
from typing import Any

from archivefs.registry import PROTOCOLS

logger = logging.getLogger(__name__)

__all__ = [
    "DVC_PROTOCOLS",
    # The three wrapper classes are built lazily by __getattr__ below, so they are
    # reachable as attributes but are deliberately not module-level names.
    "disable",
    "enable",
    "register",
    "status",
]

_PTH_NAME = "archivefs_dvc.pth"
#: The .pth runs at interpreter start-up, so it must be cheap and must not fail.
#: It imports one module that imports nothing heavy and registers lazily.
_PTH_CONTENT = "import archivefs.dvcfs_hook\n"


def _dvc_filesystem_base() -> Any:
    from dvc_objects.fs.base import FileSystem

    return FileSystem


def _make_dvc_class(name: str, protocol: str, fsspec_class_path: str) -> type:
    """Build a DVC ``FileSystem`` wrapper for one archivefs protocol.

    Generated rather than written out three times: the wrappers differ only in
    which fsspec class they instantiate, and a hand-written copy each would drift.
    """
    from dvc_objects.compat import cached_property

    base = _dvc_filesystem_base()

    def _prepare_credentials(self: Any, **config: Any) -> dict:
        # DVC passes the whole remote config; hand the filesystem only what it
        # understands, and never the URL (which is the path, not an option).
        drop = {
            "url",
            "jobs",
            "checksum_jobs",
            "verify",
            "version_aware",
            "worktree",
            "no_traverse",
            "index",
        }
        return {k: v for k, v in config.items() if k not in drop and v is not None}

    def _fsspec_class() -> Any:
        from fsspec.registry import _import_class

        return _import_class(fsspec_class_path)

    def fs(self: Any) -> Any:
        return _fsspec_class()(**self.fs_args)

    def _strip_protocol(cls: Any, path: str) -> str:
        # DVC's default is the identity, which leaves "zenodo://1486184" as the
        # path and makes every relpath DVC computes come out as "../../…".
        return _fsspec_class()._strip_protocol(path)

    def unstrip_protocol(self: Any, path: str) -> str:
        return f"{protocol}://{path}"

    def version_aware(self: Any) -> bool:
        # Hard False. DVC turns this on whenever a Meta carries a version_id and
        # then expects cloud-object-versioning methods (coalesce_version and
        # friends) that only S3/GCS/Azure implement. archivefs' version tokens are
        # DOIs and re-encryption stamps, not object versions, so they live under
        # `archive_version` in info() and this stays off. docs/decisions.md D6.
        return False

    def coalesce_version(self: Any, path: str, version_id: str | None) -> tuple:
        # Defensive: a .dvc written before D6 may still carry a version_id, and
        # crashing on someone's existing file would be rude.
        return path, version_id

    def _get_kwargs_from_urls(urlpath: str) -> dict:
        # DVC's default runs fsspec's infer_storage_options, which reads an
        # accession as a *hostname* and passes it on as config. Nothing in one of
        # our URLs is configuration.
        return {}

    return type(
        name,
        (base,),
        {
            "protocol": protocol,
            # The field DVC reads as "the content hash of this file". Our info()
            # supplies a real md5 wherever the archive (or a .chiro manifest)
            # publishes one, which is what makes --no-download honest.
            "PARAM_CHECKSUM": "md5",
            "REQUIRES": {"archivefs": "archivefs"},
            "_prepare_credentials": _prepare_credentials,
            "_strip_protocol": classmethod(_strip_protocol),
            "_get_kwargs_from_urls": staticmethod(_get_kwargs_from_urls),
            "unstrip_protocol": unstrip_protocol,
            "version_aware": property(version_aware),
            "coalesce_version": coalesce_version,
            "fs": cached_property(fs),
            "__doc__": f"DVC wrapper around archivefs' {protocol}:// filesystem.",
        },
    )


#: wrapper class name -> (protocol, the fsspec class it wraps). The classes are
#: built on first *attribute access*, not at import, because building one needs
#: ``dvc_objects`` — and this module must be importable without DVC installed, or
#: ``archivefs info`` cannot even report that DVC is absent. DVC resolves our
#: wrappers by string path (``archivefs.dvcfs.ZenodoDVCFileSystem``) and so goes
#: through ``getattr`` on the module, which is exactly what ``__getattr__`` serves.
_WRAPPERS: dict[str, tuple[str, str]] = {
    "EGADVCFileSystem": ("ega", "archivefs.backends.ega.EGAFileSystem"),
    "ZenodoDVCFileSystem": ("zenodo", "archivefs.backends.zenodo.ZenodoFileSystem"),
    "GEODVCFileSystem": ("geo", "archivefs.backends.geo.GEOFileSystem"),
    "BIADVCFileSystem": ("bia", "archivefs.backends.bia.BIAFileSystem"),
}
_built: dict[str, type] = {}


def __getattr__(name: str) -> Any:  # PEP 562
    if name not in _WRAPPERS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name not in _built:
        protocol, fsspec_class_path = _WRAPPERS[name]
        _built[name] = _make_dvc_class(name, protocol, fsspec_class_path)
    return _built[name]


def __dir__() -> list[str]:
    return sorted({*globals(), *_WRAPPERS})


#: protocol -> our DVC wrapper's import path. dbGaP is absent on purpose: it is
#: deferred, and registering it with DVC would advertise a backend that refuses.
DVC_PROTOCOLS: dict[str, str] = {
    "ega": "archivefs.dvcfs.EGADVCFileSystem",
    "zenodo": "archivefs.dvcfs.ZenodoDVCFileSystem",
    "geo": "archivefs.dvcfs.GEODVCFileSystem",
    "bia": "archivefs.dvcfs.BIADVCFileSystem",
}

#: Config keys DVC must accept for our remotes, beyond its common ones, and the
#: type to coerce each to. **The type matters:** a DVC config file is INI, so every
#: value arrives as a string — register `extract_zips` as a bare object and
#: `extract_zips = false` reaches the filesystem as the string "false", which is
#: truthy, so ZIPs get presented as directories against the user's explicit
#: instruction. DVC's own booleans go through its `Bool`, and so must ours.
#:
#: These are the *non-secret* options, and the omissions are load-bearing. DVC
#: validates remote config and `--fs-config` against exactly this table, so a key
#: that is absent here is refused by DVC itself:
#:
#:     $ dvc remote modify ega password hunter2
#:     ERROR: configuration error - config file error:
#:            extra keys not allowed @ data['remote']['ega']['password']
#:
#: That is why `username`, `password` and `token` are not listed. It is not a
#: convention we ask users to respect — with them absent, the anti-pattern is a
#: config parse error, and one that fires before any archivefs code runs. Adding
#: any of them back would make it possible to commit a credential again:
#: `--fs-config password=…` is written verbatim into the `.dvc` file's
#: `deps[].fs_config`. `credentials_file` stays because it is a *path* — the ssh
#: `IdentityFile` analogue, and the supported way to select an account per repo.
_BOOL_KEYS = frozenset({"extract_zips"})
_EXTRA_CONFIG_KEYS: dict[str, tuple[str, ...]] = {
    "ega": ("credentials_file", "layout", "chiro", "api_url"),
    "zenodo": ("extract_zips", "api_url", "chiro"),
    "geo": ("view", "base_url", "chiro"),
    "bia": ("view", "api_url", "s3_url", "chiro"),
}


def register() -> list[str]:
    """Teach the *current process's* DVC about ``ega://``, ``zenodo://``, ``geo://``.

    Idempotent. Returns the protocols registered. Raises ``ImportError`` if DVC is
    not installed, since calling this at all implies you expected it to be.
    """
    from dvc import config_schema
    from dvc_objects.fs import known_implementations

    for protocol, class_path in DVC_PROTOCOLS.items():
        known_implementations[protocol] = {
            "class": class_path,
            "err": (
                f"{protocol} is supported, but requires 'archivefs' to be installed"
            ),
        }
        config_schema.REMOTE_SCHEMAS[protocol] = {
            **{
                key: (config_schema.Bool if key in _BOOL_KEYS else object)
                for key in _EXTRA_CONFIG_KEYS.get(protocol, ())
            },
            **config_schema.REMOTE_COMMON,
        }

    # ByUrl snapshots the schema table when it is called, so the validator has to
    # be rebuilt — mutating REMOTE_SCHEMAS alone silently does nothing.
    config_schema.SCHEMA["remote"] = {str: config_schema.ByUrl(
        config_schema.REMOTE_SCHEMAS
    )}
    logger.debug("registered archivefs protocols with DVC: %s", list(DVC_PROTOCOLS))
    return list(DVC_PROTOCOLS)


# --------------------------------------------------------- process-wide enabling


def _site_packages() -> Any:
    import site
    import sysconfig
    from pathlib import Path

    # Prefer the environment's own site-packages over the user one, so the hook
    # follows the environment (conda env, venv) rather than the user's home.
    candidates = [sysconfig.get_paths().get("purelib")]
    if hasattr(site, "getsitepackages"):
        candidates.extend(site.getsitepackages())
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    raise RuntimeError("could not locate this environment's site-packages")


def enable() -> Any:
    """Install a ``.pth`` so every interpreter in this environment registers.

    Blunt, and deliberately explicit: DVC offers no plugin hook for storage
    backends, so the alternatives are this, patching DVC, or asking users to
    remember an import that ``dvc`` (a CLI, not a library call) never gives them
    the chance to make.
    """
    path = _site_packages() / _PTH_NAME
    path.write_text(_PTH_CONTENT)
    return path


def disable() -> Any | None:
    path = _site_packages() / _PTH_NAME
    if path.exists():
        path.unlink()
        return path
    return None


def status(probe: bool = True) -> dict:
    """Whether DVC is present, whether the hook is installed, and what works.

    With ``probe`` (the default) this actually performs the registration in this
    process before reporting, so the answer reflects whether registration *works*
    rather than whether it happened to have run already — the start-up hook fires
    on ``dvc.config_schema`` being imported, which a status check otherwise never
    triggers.
    """
    import importlib.util
    from pathlib import Path

    report: dict[str, Any] = {
        # find_spec, not an import: this must answer "no DVC here" without
        # importing anything of DVC's.
        "dvc_installed": importlib.util.find_spec("dvc_objects") is not None
        and importlib.util.find_spec("dvc") is not None,
        "hook_path": None,
        "hook_installed": False,
        "registered": [],
        "protocols": sorted(DVC_PROTOCOLS),
        "deferred": sorted(set(PROTOCOLS) - set(DVC_PROTOCOLS)),
    }
    try:
        hook = _site_packages() / _PTH_NAME
    except RuntimeError:
        hook = None
    if hook is not None:
        report["hook_path"] = str(hook)
        report["hook_installed"] = Path(hook).exists()
    if report["dvc_installed"]:
        if probe:
            try:
                register()
            except Exception as exc:  # noqa: BLE001
                report["error"] = str(exc)
        from dvc_objects.fs import known_implementations

        report["registered"] = sorted(
            protocol
            for protocol, class_path in DVC_PROTOCOLS.items()
            if known_implementations.get(protocol, {}).get("class") == class_path
        )
    return report
