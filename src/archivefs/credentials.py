"""Credential resolution, shared by every backend that needs a secret.

Extracted from the EGA/Zenodo diff (``docs/plan.md`` step 2), and deliberately
shaped with the two backends that do not exist yet in view:

============ ============================== ==========================
archive      secret                         shape
============ ============================== ==========================
Zenodo       bearer token (usually none)    opaque string, static
EGA          username + password -> token   two parts, one refreshable
GEO          none                           —
dbGaP        path to an ``.ngc`` **file**    a *path*, and it must exist
============ ============================== ==========================

Three invariants come out of that table and are enforced here rather than in each
backend:

1. **Precedence is fixed**: ``storage_options`` (i.e. what ``.dvc/config.local``
   supplies) → environment variable → on-disk file. Same order everywhere, so a
   user who learns it once has learned it for all archives.
2. **No secret enters the committed artifact.** Non-secret identifiers
   (``username``, a path to a key file) may legitimately live in a committed
   ``.dvc``; secrets may not. Each :class:`Credential` says which it is, and
   :meth:`CredentialResolver.committed_secrets` exists so a caller can check a
   committed config rather than hoping.
3. **A failure names every place that was searched**, in order — the error
   message doubles as the documentation of the chain.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from archivefs.errors import ArchiveFSError, CredentialsNotFoundError

__all__ = ["Credential", "CredentialResolver", "Resolved"]


@dataclass(frozen=True)
class Credential:
    """Declaration of one thing a backend needs to authenticate."""

    #: the ``storage_options`` key, e.g. ``"username"``, ``"token"``, ``"ngc"``
    name: str
    #: environment variables to try, in order
    env: tuple[str, ...] = ()
    #: JSON files to try, in order; ``~`` is expanded
    files: tuple[str, ...] = ()
    #: key to read out of those JSON files (defaults to ``name``)
    file_key: str | None = None
    #: is this a secret (must never be committed) or a plain identifier?
    secret: bool = True
    #: is the value a filesystem path that must exist? (dbGaP's ``.ngc``)
    is_path: bool = False
    required: bool = True
    #: shown in error messages when the credential is missing
    help: str = ""


@dataclass
class Resolved:
    """A resolved value plus where it came from — for error messages and audit."""

    name: str
    value: str | None
    source: str
    searched: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return self.value is not None

    def __str__(self) -> str:  # never print the value
        return f"{self.name} from {self.source}"


class CredentialResolver:
    """Resolves an archive's declared credentials against the local environment.

    Resolution happens at **pull time**, in the process doing the pulling, so a
    ``.dvc`` file stays portable and a shared HPC filesystem never has to hold a
    shared secret: each researcher's own environment supplies their own.
    """

    def __init__(
        self,
        archive: str,
        credentials: list[Credential],
        storage_options: dict | None = None,
        *,
        environ: dict | None = None,
    ) -> None:
        self.archive = archive
        self.credentials = {c.name: c for c in credentials}
        self.storage_options = {
            k: v for k, v in (storage_options or {}).items() if v is not None
        }
        self.environ = environ if environ is not None else os.environ
        self._cache: dict[str, Resolved] = {}

    # ------------------------------------------------------------------ lookup

    def resolve(self, name: str) -> Resolved:
        if name in self._cache:
            return self._cache[name]
        try:
            credential = self.credentials[name]
        except KeyError:
            raise ArchiveFSError(
                f"{self.archive} declares no credential called {name!r}; it knows "
                f"about: {', '.join(sorted(self.credentials)) or '(none)'}"
            ) from None

        searched: list[str] = []
        value: str | None = None
        source = "not found"

        searched.append(
            f"storage_options[{credential.name!r}] "
            + (
                "(put it in .dvc/config.local — never the committed .dvc)"
                if credential.secret
                else "(from .dvc/config or .dvc/config.local)"
            )
        )
        if credential.name in self.storage_options:
            value = str(self.storage_options[credential.name])
            source = searched[-1]

        for variable in credential.env:
            searched.append(f"${variable}")
            if value is None and self.environ.get(variable):
                value = self.environ[variable]
                source = f"${variable}"

        for candidate in credential.files:
            path = Path(candidate).expanduser()
            searched.append(str(path))
            if value is not None or not path.exists():
                continue
            try:
                payload = json.loads(path.read_text())
            except ValueError as exc:
                raise ArchiveFSError(
                    f"{path} is not valid JSON ({exc}); expected an object "
                    f"containing {credential.file_key or credential.name!r}"
                ) from exc
            key = credential.file_key or credential.name
            if isinstance(payload, dict) and payload.get(key):
                value = str(payload[key])
                source = str(path)

        if value is not None and credential.is_path:
            expanded = Path(value).expanduser()
            if not expanded.exists():
                raise CredentialsNotFoundError(
                    self.archive,
                    f"{credential.name} file (resolved to {expanded}, which does "
                    f"not exist)",
                    searched,
                )
            value = str(expanded)

        resolved = Resolved(name, value, source, searched)
        if value is None and credential.required:
            message_parts = searched
            if credential.help:
                message_parts = [*searched, f"note: {credential.help}"]
            raise CredentialsNotFoundError(self.archive, name, message_parts)
        self._cache[name] = resolved
        return resolved

    def get(self, name: str, default: str | None = None) -> str | None:
        """Resolve, returning ``default`` for an optional missing credential."""
        credential = self.credentials.get(name)
        if credential is not None and not credential.required:
            try:
                resolved = self.resolve(name)
            except CredentialsNotFoundError:
                return default
            return resolved.value if resolved.value is not None else default
        return self.resolve(name).value

    def require(self, *names: str) -> dict[str, str]:
        """Resolve several credentials, reporting *all* the missing ones at once.

        Resolving one at a time would tell a user their username is missing, let
        them fix it, and only then mention the password. One error, one fix.
        """
        values: dict[str, str] = {}
        missing: list[str] = []
        searched: list[str] = []
        for name in names:
            try:
                resolved = self.resolve(name)
            except CredentialsNotFoundError as exc:
                missing.append(name)
                searched.extend(exc.searched)
                continue
            if resolved.value is None:
                missing.append(name)
                searched.extend(resolved.searched)
            else:
                values[name] = resolved.value
        if missing:
            raise CredentialsNotFoundError(
                self.archive, " and ".join(missing), searched
            )
        return values

    def source_of(self, name: str) -> str:
        return self.resolve(name).source

    def describe(self, names: list[str] | None = None) -> str:
        """A one-line, secret-free summary of where credentials came from."""
        wanted = names or list(self.credentials)
        parts = []
        for name in wanted:
            resolved = self._cache.get(name)
            if resolved is None:
                continue
            parts.append(str(resolved))
        return "; ".join(parts) or "no credentials resolved"

    # ------------------------------------------------------------- invariants

    def committed_secrets(self, committed: dict) -> list[str]:
        """Which of ``committed``'s keys are secrets that must not be there.

        Give it the ``storage_options`` block of a *committed* ``.dvc`` file (or
        ``.dvc/config``, as opposed to ``config.local``) and it names the
        problems. Cheap to call, and turns an invariant we merely documented into
        one a backend can enforce.
        """
        return sorted(
            name
            for name, credential in self.credentials.items()
            if credential.secret and name in committed and committed[name]
        )
