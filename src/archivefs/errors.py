"""The error contract.

Two rules, both from ``docs/design.md``:

* **Uniform auth failure.** Every backend that can fail on credentials raises
  :class:`AuthenticationError` carrying *specific remediation* — not a bare 401.
  The user should never have to guess which of four secrets was missing or where
  the backend looked for it.
* **Missing extras are not import errors.** An uninstalled per-backend extra
  raises :class:`MissingExtraError` naming the exact ``pip install`` to run.

All exceptions subclass :class:`ArchiveFSError` so a consumer can catch the whole
family, and (where it makes semantic sense) a builtin, so fsspec/DVC code paths
that expect e.g. ``FileNotFoundError`` keep working.
"""

from __future__ import annotations

__all__ = [
    "ArchiveFSError",
    "ArchiveNotFoundError",
    "AuthenticationError",
    "CredentialSourceError",
    "CredentialsNotFoundError",
    "ManifestError",
    "MissingExtraError",
    "MissingToolError",
    "NotSupportedError",
    "UnsafeArchiveMemberError",
    "VerificationError",
]


class ArchiveFSError(Exception):
    """Base class for every archivefs error."""


class MissingExtraError(ArchiveFSError, ImportError):
    """A backend needs an optional dependency that is not installed.

    Raised at *instantiation* time, never at module-import time: backend modules
    must import cleanly without their extras so that protocol registration
    (which is what tells the user the protocol exists at all) still works.
    """

    def __init__(self, extra: str, package: str, purpose: str) -> None:
        self.extra = extra
        self.package = package
        super().__init__(
            f"archivefs[{extra}] is not installed: {purpose} needs {package!r}.\n"
            f"  Install it with:  pip install 'archivefs[{extra}]'\n"
            f"  (conda users: add {package!r} to the pip: block of your "
            f"environment.yaml and re-run `conda env update`.)"
        )


class MissingToolError(ArchiveFSError, RuntimeError):
    """An external *binary* (not a Python package) is missing.

    Distinct from :class:`MissingExtraError` because ``pip install`` will not fix
    it — e.g. the SRA Toolkit for dbGaP.
    """

    def __init__(self, tool: str, purpose: str, how: str) -> None:
        self.tool = tool
        super().__init__(
            f"required external tool {tool!r} was not found on PATH: {purpose}.\n"
            f"  {how}"
        )


class CredentialsNotFoundError(ArchiveFSError):
    """No credential could be resolved for an archive that requires one.

    Carries the full list of places that were searched, in precedence order, so
    the message doubles as documentation of the resolution chain.
    """

    def __init__(
        self,
        archive: str,
        secret: str,
        searched: list[str],
        sections: dict[str, list[str]] | None = None,
        hints: dict[str, str] | None = None,
    ) -> None:
        self.archive = archive
        self.secret = secret
        self.searched = list(searched)
        self.sections = sections
        # Grouped when several credentials are missing at once: one numbered
        # chain each, because a single run of ten numbered lines covering two
        # different secrets reads as one chain that it is not.
        if sections:
            blocks = []
            for name, places in sections.items():
                listed = "\n".join(f"      {i + 1}. {p}" for i, p in enumerate(places))
                blocks.append(f"    {name}:\n{listed}")
                hint = (hints or {}).get(name)
                if hint:
                    blocks.append(f"      ({hint})")
            body = "\n".join(blocks)
        else:
            body = "\n".join(f"    {i + 1}. {p}" for i, p in enumerate(searched))
            hint = (hints or {}).get(secret)
            if hint:
                body += f"\n    ({hint})"
        super().__init__(
            f"no {secret} found for {archive}. Looked, in order:\n{body}\n"
            f"  Nothing was found, so the request was not attempted.\n"
            f"  Note: those are the only places archivefs looks. Credentials cannot "
            f"be passed as options or written into DVC config — see "
            f"docs/credentials.md."
        )


class CredentialSourceError(ArchiveFSError):
    """A credential was offered from a source archivefs refuses to read.

    Distinct from :class:`CredentialsNotFoundError`: the value *was* supplied, and
    we are declining it. Two cases — a credential passed as a storage option (the
    route that ends up in a committed ``.dvc`` file), and an on-disk credentials
    file that other users can read.
    """


class AuthenticationError(ArchiveFSError, PermissionError):
    """The archive refused access: it rejected our credential, or wanted none.

    ``remediation`` is mandatory: an auth failure the user cannot act on is a
    bug in the backend, not in their setup.

    ``used_credentials=False`` for an archive that has no credential mechanism at
    all (BIA, GEO). A 403 from one of those is the archive declining to serve a
    record — embargoed, restricted or withdrawn — and saying "rejected the
    credential" would send the user looking for a login that does not exist.
    """

    def __init__(
        self,
        archive: str,
        reason: str,
        remediation: str,
        *,
        source: str | None = None,
        used_credentials: bool = True,
    ) -> None:
        self.archive = archive
        self.reason = reason
        self.remediation = remediation
        self.source = source
        self.used_credentials = used_credentials
        origin = f" (credential came from: {source})" if source else ""
        lead = (
            f"{archive} rejected the credential{origin}"
            if used_credentials
            else f"{archive} refused access"
        )
        super().__init__(f"{lead}: {reason}\n  What to do: {remediation}")


class ArchiveNotFoundError(ArchiveFSError, FileNotFoundError):
    """An accession/record/path does not exist in the archive.

    Subclasses ``FileNotFoundError`` because fsspec (and DVC through it) treats
    that as a normal, expected outcome of ``exists()``/``info()``.
    """


class NotSupportedError(ArchiveFSError, NotImplementedError):
    """A write-side or otherwise unsupported operation was attempted.

    These filesystems are deliberately read-only; the message says so rather
    than letting a half-implemented write path corrupt an archive.
    """


class ManifestError(ArchiveFSError):
    """A ``.chiro`` manifest was found but could not be used."""


class VerificationError(ArchiveFSError):
    """Materialised bytes did not match the checksum the archive advertised."""

    def __init__(self, path: str, algorithm: str, expected: str, actual: str) -> None:
        self.path = path
        self.algorithm = algorithm
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"checksum mismatch for {path}: {algorithm} expected {expected}, "
            f"got {actual}.\n"
            f"  The bytes on disk are NOT what the archive says they should be. "
            f"Do not use them; re-fetch, and if it recurs report it upstream."
        )


class UnsafeArchiveMemberError(ArchiveFSError):
    """A ZIP member's path escapes the extraction directory ("zip slip").

    Refusing is not optional: archivefs is pointed at arbitrary third-party
    archives by design.
    """

    def __init__(self, member: str, target: str) -> None:
        self.member = member
        self.target = target
        super().__init__(
            f"refusing to extract {member!r}: it resolves outside the target "
            f"directory {target!r} (zip-slip). The archive is malformed or "
            f"malicious."
        )
