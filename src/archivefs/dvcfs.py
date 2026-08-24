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
        # On for any archive whose profile declares an immutability basis, off for
        # the rest. This is the whole reviewer route: `dvc/repo/index.py`, in
        # `_load_storage_from_import`, registers the source URL as a fallback
        # storage only when
        #
        #     out.stage.is_repo_import or not out.hash_info or dep.fs.version_aware
        #
        # so with this off, a *completed* `dvc import-url` — the normal thing to do
        # — records `outs.md5` and DVC then refuses to treat the archive as a place
        # the bytes could come from. That is what forced `--no-download`, a flag
        # whose name has nothing to do with what it does.
        #
        # It must be gated per-archive rather than switched on globally, because
        # `dvc/repo/fetch.py`'s `_log_unversioned` *silently drops* every entry
        # whose Meta carries no `version_id` — a warning, not an error. GEO and
        # BIA-submitted publish nothing immutable to build a token from, so they
        # would quietly lose files. `_check_versioned` below turns the same
        # condition into a refusal for the archives that are on.
        #
        # docs/decisions.md D6 and D17 have the corrected reasoning, including why
        # the earlier attempt at this appeared to fail.
        return PROTOCOLS[protocol].immutability_basis is not None

    def version_path(self: Any, path: str, version_id: str | None) -> str:
        # Identity, and that is not a shim standing in for something better: a
        # published record is immutable and its id is already in the path, so
        # "the path at this version" *is* the path. S3 answers this with a
        # ?versionId= query because an S3 key is mutable; a Zenodo record id or an
        # EGA accession is not.
        #
        # Its absence is a hard AttributeError from `dvc/dependency/base.py`, and
        # `dvc_data/index/checkout.py` asserts on it, which is the concrete failure
        # D6 generalised from.
        return path

    def coalesce_version(self: Any, path: str, version_id: str | None) -> tuple:
        # Nothing to reconcile: there is one version of a given path, so whatever
        # version_id a caller arrives with is the one it gets back. Written
        # defensively for pre-D6 `.dvc` files; now load-bearing, since
        # `dvc/output.py` calls it for every out of a version-aware dependency.
        return path, version_id

    def _check_versioned(self: Any, entries: Any) -> Any:
        """Refuse to hand DVC a file entry with no version token.

        The failure this prevents is silent. Once a version-aware ``FileStorage``
        remote is in play, ``_log_unversioned`` removes untokened entries from the
        fetch and logs a warning; ``dvc pull`` then reports success having quietly
        skipped files. A Zenodo file whose published digest is not an md5 is a real
        instance — the profile says "this archive can produce a token", and this is
        where that claim gets checked per entry rather than trusted.
        """
        from archivefs.errors import NotSupportedError

        materialised = list(entries)
        for entry in materialised:
            if not isinstance(entry, dict) or entry.get("type") != "file":
                continue
            if entry.get("version_id"):
                continue
            name = entry.get("name", "?")
            msg = (
                f"{protocol}://{name} carries no immutable version token, so DVC "
                f"would drop it from a fetch and report success. Expected one "
                f"from: {PROTOCOLS[protocol].immutability_basis}. Import the "
                f"containing archive as a single file instead, or supply a "
                f".chiro manifest."
            )
            raise NotSupportedError(msg)
        return materialised

    def info(self: Any, path: Any, *args: Any, **kwargs: Any) -> Any:
        # `path` may be a *list* — DVC's base batches info() across a thread pool —
        # so delegate rather than reimplement, and normalise before checking.
        entry = base.info(self, path, *args, **kwargs)
        if self.version_aware:
            self._check_versioned([entry] if isinstance(entry, dict) else entry)
        return entry

    def walk(self: Any, path: str, **kwargs: Any) -> Any:
        # The important one. DVC builds a directory index through
        # `dvc_data.index.build.safe_walk`, which calls *this* method with
        # detail=True; checking only `info` would validate a single-file import and
        # miss every member of a directory or expanded ZIP.
        #
        # `ls` is deliberately not overridden. Nothing builds a `Meta` from it —
        # DVC reaches for `self.fs.ls` directly in `isdir`, bypassing the wrapper —
        # so an override there would add a failure mode without adding coverage.
        for root, dirs, files in base.walk(self, path, **kwargs):
            if self.version_aware and isinstance(files, dict):
                self._check_versioned(files.values())
            yield root, dirs, files

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
            "version_path": version_path,
            "coalesce_version": coalesce_version,
            "_check_versioned": _check_versioned,
            "info": info,
            "walk": walk,
            "fs": cached_property(fs),
            "__doc__": f"DVC wrapper around archivefs' {protocol}:// filesystem.",
        },
    )


#: wrapper class name -> (protocol, the fsspec class it wraps). Derived from the
#: archive profiles in ``registry.py`` rather than written out, because this used to
#: be one of four parallel protocol-keyed dicts here and they drifted; a profile with
#: no ``dvc_wrapper`` (dbGaP, deferred) is absent from all of them by construction.
#:
#: The classes are built on first *attribute access*, not at import, because building
#: one needs ``dvc_objects`` — and this module must be importable without DVC
#: installed, or ``archivefs info`` cannot even report that DVC is absent. DVC
#: resolves our wrappers by string path (``archivefs.dvcfs.ZenodoDVCFileSystem``) and
#: so goes through ``getattr`` on the module, which is exactly what ``__getattr__``
#: serves.
_WRAPPERS: dict[str, tuple[str, str]] = {
    backend.dvc_wrapper: (backend.protocol, backend.class_path)
    for backend in PROTOCOLS.values()
    if backend.dvc_wrapper
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


#: protocol -> our DVC wrapper's import path. Derived, like ``_WRAPPERS``: dbGaP is
#: absent because its profile sets no ``dvc_wrapper``, which is the one place that
#: fact is now recorded. Registering it with DVC would advertise a backend that
#: refuses.
DVC_PROTOCOLS: dict[str, str] = {
    protocol: f"{__name__}.{wrapper}" for wrapper, (protocol, _) in _WRAPPERS.items()
}


def _remote_schema(backend: Any) -> dict:
    """Translate one archive profile's ``config`` into a DVC remote schema.

    The config keys and their types come from ``registry.py``; all this adds is
    DVC's own validator for each type. ``Bool`` is the one that matters — see the
    ``Backend.config`` docstring for why an untyped boolean silently expands ZIPs
    against the user's instruction.
    """
    from dvc import config_schema

    validators: dict[type, Any] = {bool: config_schema.Bool, str: object}
    return {
        key: validators.get(kind, object) for key, kind in backend.config.items()
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
            **_remote_schema(PROTOCOLS[protocol]),
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
