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

Four invariants come out of that table and are enforced here rather than in each
backend:

1. **Precedence is fixed**: environment variable → on-disk JSON file. Same order
   everywhere, so a user who learns it once has learned it for all archives.
2. **A credential can only come from those two places.** There is deliberately no
   third route: passing ``password=`` as a storage option — via ``-o`` on the CLI,
   ``--fs-config`` under DVC, ``dvc remote modify``, or an fsspec keyword — is
   *refused*, not merely discouraged. This follows ssh, which has no
   ``-o Password=``: it has ``IdentityFile``, a **path**. Path-valued credentials
   (dbGaP's ``ngc``, EGA's ``credentials_file``) are pointers rather than secrets
   and remain settable, which is what lets two projects use two accounts.
3. **A credentials file only you can read.** A file that is group- or
   world-readable is refused, again as ssh does with a private key, because a
   warning in a pipeline log is invisible at exactly the moment it matters.
4. **A failure names every place that was searched**, in order — the error
   message doubles as the documentation of the chain.

:meth:`CredentialResolver.committed_secrets` remains for the case the invariants
cannot reach: a repository configured before this policy, or a secret already in
git history, where the answer is to rotate rather than to edit.

Invariant 2 is belt *and* braces: :func:`archivefs.dvcfs.register` also omits the
credential keys from the schema it hands DVC, so ``dvc remote modify ega password
…`` and ``--fs-config password=…`` fail inside DVC's own config validator with
*"extra keys not allowed"* before archivefs is reached. The check here catches the
routes DVC does not police — ``-o`` and the Python API.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

from archivefs.errors import (
    ArchiveFSError,
    CredentialsNotFoundError,
    CredentialSourceError,
)

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
    #: Is the secret scoped to *one accession* rather than to an account?
    #:
    #: EGA hands out one username and password that open every dataset you have been
    #: authorised for. BioStudies hands out a key per **study** — the reviewer link
    #: for one pre-publication deposit — so a single value is not enough once you
    #: have two of them. When set, the JSON file's ``file_key`` holds a mapping from
    #: accession to secret, and a flat ``name`` entry beside it still works as a
    #: catch-all for the common case of exactly one private study::
    #:
    #:     {"keys": {"S-BIAD3080": "d62f…", "S-BIAD1234": "…"}}
    #:     {"key": "d62f…"}          # applies to whatever you ask for
    scoped: bool = False
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
        self._refuse_option_credentials()

    # ------------------------------------------------------------- enforcement

    def _refuse_option_credentials(self) -> None:
        """Fail if a credential was passed as an option. Invariant 2.

        Raised from ``__init__`` rather than from :meth:`resolve` on purpose: the
        point is that the value is never read, so the refusal must not depend on
        anyone getting as far as resolving it.
        """
        offered = sorted(
            name
            for name, credential in self.credentials.items()
            if not credential.is_path and name in self.storage_options
        )
        if not offered:
            return
        names = ", ".join(offered)
        places = []
        for name in offered:
            credential = self.credentials[name]
            where = [f"${v}" for v in credential.env]
            where += [str(Path(f).expanduser()) for f in credential.files]
            places.append(f"    {name}: {', '.join(where) or '(no source declared)'}")
        listed = "\n".join(places)
        raise CredentialSourceError(
            f"{names} cannot be passed as an option to {self.archive}. archivefs "
            f"reads credentials only from the environment or a file only you can "
            f"read:\n{listed}\n"
            f"  This is refused rather than discouraged because the same option "
            f"reaches DVC as deps[].fs_config, which is written into the .dvc file "
            f"you commit.\n"
            f"  To use a specific account, point at its file instead — that is a "
            f"path, not a secret, and may be committed. See docs/credentials.md."
        )

    @staticmethod
    def _check_file_mode(path: Path) -> None:
        """Refuse a credentials file that anyone else can read. Invariant 3."""
        try:
            mode = path.stat().st_mode
        except OSError:  # unreadable for other reasons; the caller will report it
            return
        exposed = stat.S_IMODE(mode) & (stat.S_IRWXG | stat.S_IRWXO)
        if not exposed:
            return
        raise CredentialSourceError(
            f"{path} is readable by others (mode {stat.S_IMODE(mode):04o}); "
            f"archivefs will not read a credential out of it.\n"
            f"  Fix it with:  chmod 600 {path}\n"
            f"  On a shared filesystem this is the difference between a credential "
            f"that is traceable to you and one that is not."
        )

    # ------------------------------------------------------------------ lookup

    def resolve(self, name: str, scope: str | None = None) -> Resolved:
        cache_key = name if scope is None else f"{name}\0{scope}"
        if cache_key in self._cache:
            return self._cache[cache_key]
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

        # A path-valued credential (dbGaP's ngc) is a pointer, not a secret, so it
        # may still be supplied as an option; _refuse_option_credentials has
        # already rejected every other kind.
        if credential.is_path and credential.name in self.storage_options:
            searched.append(f"the {credential.name} option")
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
            self._check_file_mode(path)
            try:
                payload = json.loads(path.read_text())
            except ValueError as exc:
                raise ArchiveFSError(
                    f"{path} is not valid JSON ({exc}); expected an object "
                    f"containing {credential.file_key or credential.name!r}"
                ) from exc
            if not isinstance(payload, dict):
                continue
            key = credential.file_key or credential.name
            if credential.scoped:
                # The scoped entry for *this* accession first, then a flat entry as
                # the catch-all — so the one-private-study case stays a two-line file
                # and the many-studies case does not need one file per study.
                mapping = payload.get(key)
                if isinstance(mapping, dict) and scope and mapping.get(scope):
                    value = str(mapping[scope])
                    source = f"{path} ({scope})"
                elif payload.get(credential.name):
                    value = str(payload[credential.name])
                    source = str(path)
            elif payload.get(key):
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
            raise CredentialsNotFoundError(
                self.archive,
                name if scope is None else f"{name} for {scope}",
                searched,
                hints={name: credential.help} if credential.help else None,
            )
        self._cache[cache_key] = resolved
        return resolved

    def get(
        self, name: str, default: str | None = None, scope: str | None = None
    ) -> str | None:
        """Resolve, returning ``default`` for an optional missing credential."""
        credential = self.credentials.get(name)
        if credential is not None and not credential.required:
            try:
                resolved = self.resolve(name, scope)
            except CredentialsNotFoundError:
                return default
            return resolved.value if resolved.value is not None else default
        return self.resolve(name, scope).value

    def require(self, *names: str) -> dict[str, str]:
        """Resolve several credentials, reporting *all* the missing ones at once.

        Resolving one at a time would tell a user their username is missing, let
        them fix it, and only then mention the password. One error, one fix.
        """
        values: dict[str, str] = {}
        sections: dict[str, list[str]] = {}
        hints: dict[str, str] = {}
        for name in names:
            try:
                resolved = self.resolve(name)
            except CredentialsNotFoundError as exc:
                sections[name] = exc.searched
            else:
                if resolved.value is None:
                    sections[name] = resolved.searched
                else:
                    values[name] = resolved.value
            help_text = self.credentials[name].help
            if name in sections and help_text:
                hints[name] = help_text
        if sections:
            raise CredentialsNotFoundError(
                self.archive,
                " and ".join(sections),
                [place for places in sections.values() for place in places],
                sections=sections,
                hints=hints,
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

        Current archivefs cannot *write* one of these: DVC's validator rejects the
        keys outright (see the module docstring). This exists for the cases that
        remain — auditing git history, and repositories configured before the
        policy landed, where a secret may already be committed and needs rotating.
        Give it the ``fs_config`` block of a ``.dvc`` file, or a ``.dvc/config``
        remote section, and it names the problems.
        """
        return sorted(
            name
            for name, credential in self.credentials.items()
            if credential.secret and name in committed and committed[name]
        )
