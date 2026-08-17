"""Protocol registration, and the "install the extra" failure contract.

fsspec discovers our protocols through the ``fsspec.specs`` entry points in
``pyproject.toml``, so ``fsspec.filesystem("zenodo")`` works with no imports from
this package. What entry points *cannot* give us is a good message when a
per-backend extra is missing — fsspec substitutes its own generic "Unable to load
filesystem from ...". Two things follow, and they are the reason this module
exists:

1. Backend modules must import cleanly **without** their extras; a missing
   optional dependency is raised as :class:`~archivefs.errors.MissingExtraError`
   when the filesystem is *instantiated*.
2. :func:`register_implementations` re-registers the same protocols with our own
   error text for anyone who has imported ``archivefs`` directly, and covers
   installs where entry-point discovery did not happen.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["PROTOCOLS", "Backend", "installed_backends", "register_implementations"]


@dataclass(frozen=True)
class Backend:
    protocol: str
    class_path: str
    extra: str | None
    summary: str
    #: Import names that must be importable for the backend to work at all.
    requires: tuple[str, ...] = ()
    #: External binaries the backend shells out to, if any.
    requires_tools: tuple[str, ...] = ()
    status: str = "supported"

    @property
    def error_text(self) -> str:
        if self.extra:
            return (
                f"the {self.protocol}:// backend needs archivefs[{self.extra}]: "
                f"pip install 'archivefs[{self.extra}]'"
            )
        return f"could not load the {self.protocol}:// backend from {self.class_path}"

    def missing(self) -> list[str]:
        """Which of this backend's requirements are not importable."""
        import importlib.util

        return [
            name for name in self.requires if importlib.util.find_spec(name) is None
        ]


PROTOCOLS: dict[str, Backend] = {
    "ega": Backend(
        protocol="ega",
        class_path="archivefs.backends.ega.EGAFileSystem",
        # No extra: the backend speaks EGA's REST API over `requests`, which is
        # a base dependency. The [ega] extra only adds pyega3 for the optional
        # delegated transfer path. See docs/decisions.md D2.
        extra=None,
        summary="European Genome-phenome Archive (controlled access; needs credentials)",
        requires=("requests",),
    ),
    "zenodo": Backend(
        protocol="zenodo",
        class_path="archivefs.backends.zenodo.ZenodoFileSystem",
        extra=None,
        summary="Zenodo records by id or DOI (public; token only for restricted)",
        requires=("requests",),
    ),
    "geo": Backend(
        protocol="geo",
        class_path="archivefs.backends.geo.GEOFileSystem",
        extra=None,
        summary="GEO supplementary/processed files over public HTTPS (anonymous)",
        requires=("requests",),
    ),
    "dbgap": Backend(
        protocol="dbgap",
        class_path="archivefs.backends.dbgap.DbGaPFileSystem",
        extra="dbgap",
        summary="dbGaP via SRA Toolkit — DEFERRED, not implemented",
        requires=(),
        requires_tools=("prefetch", "fasterq-dump"),
        status="deferred",
    ),
}


def register_implementations(clobber: bool = True) -> None:
    """Register every protocol with fsspec, carrying our own error text.

    Idempotent, and safe to call when the entry points have already registered
    the same classes.
    """
    from fsspec import register_implementation

    for backend in PROTOCOLS.values():
        register_implementation(
            backend.protocol,
            backend.class_path,
            clobber=clobber,
            errtxt=backend.error_text,
        )


def installed_backends() -> dict[str, dict]:
    """Report each protocol's readiness — used by ``archivefs info``."""
    import shutil

    report = {}
    for name, backend in PROTOCOLS.items():
        missing_tools = [t for t in backend.requires_tools if shutil.which(t) is None]
        missing_packages = backend.missing()
        report[name] = {
            "summary": backend.summary,
            "status": backend.status,
            "extra": backend.extra,
            "missing_packages": missing_packages,
            "missing_tools": missing_tools,
            "ready": backend.status == "supported"
            and not missing_packages
            and not missing_tools,
        }
    return report
