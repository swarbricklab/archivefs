"""Handing a reviewer the access a paper's data needs, in one file and one command.

A pre-publication deposit is readable only with something the depositor holds. Today
that is BioStudies' per-study sharing key; the same shape will fit Zenodo's per-record
preview link when it lands. Getting it to three reviewers currently means an email
explaining JSON, a path under ``~/.config``, and ``chmod 600`` — three chances to
either fail or to end up with the key pasted somewhere it should not be.

So: ``archivefs credentials export`` writes a bundle, ``archivefs credentials import``
installs it. Four things make that safe enough to offer at all.

**1. Only capabilities travel, never identities.** :attr:`Credential.shareable` splits
them, defaults to ``False``, and the exporter refuses anything unmarked — see the field
for the argument. It matters more than it looks: run naively over one researcher's
config, "bundle up the credentials" would put their EGA password and their Zenodo
personal access token in a file addressed to four strangers. Exactly one credential in
archivefs is marked shareable, and that is not a limitation to route around.

**2. The bundle is derived from the repository, not from the credential store.** We
scan the DVC files for archivefs URLs, work out which deposits *this* paper depends on,
and export keys for those. A reviewer for one paper does not get keys to an unrelated
deposit that happened to be in the same config file, and the export report doubles as
an access audit — including for the deposits a bundle cannot help with.

**3. No crypto here.** ``zipfile`` cannot write encrypted archives, and ZipCrypto is
broken anyway, so a ``--password`` flag would be a claim we could not honour. Instead
the bundle is written ``0600``, refused into a git repository where one ``git add -A``
would publish it, and readable from stdin — so ``age`` or ``gpg`` composes and does the
job properly::

    archivefs credentials export --out - | age -r age1… > pack.age
    age -d pack.age | archivefs credentials import -

**4. Import never overwrites silently.** A key that differs from one already held is a
conflict, reported and refused, because the failure it prevents — a stale key quietly
replacing a working one — surfaces later as an unexplained 403.

There is a route with no artefact at all, and it is the better one when it applies:
``credentials import`` also takes the sharing *link* the depositor already has,
because for BioStudies the URL *is* the capability (see
:meth:`ArchiveFileSystem.grants_from_link`). One line in a cover letter, one command
for the reviewer, nothing to look after.

The bundle is a zip holding :data:`MANIFEST_NAME` and a README for whoever
double-clicks it. ``--out x.json`` writes the manifest alone. Reading sniffs the bytes,
so either works, as does a link on stdin.
"""

from __future__ import annotations

import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

from archivefs.credentials import Credential, CredentialResolver
from archivefs.errors import (
    ArchiveFSError,
    CredentialsNotFoundError,
    CredentialSourceError,
)

__all__ = [
    "BUNDLE_VERSION",
    "Bundle",
    "Change",
    "Grant",
    "Row",
    "Usage",
    "collect",
    "from_links",
    "install",
    "plan",
    "read",
    "scan",
    "write",
    "write_private",
]

#: Bumped only for a change an older archivefs would mis-read. Additive fields do not
#: count: an unknown key is ignored, so a newer bundle stays installable.
BUNDLE_VERSION = 1

MANIFEST_NAME = "archivefs-bundle.json"
README_NAME = "README.txt"

#: The zip epoch. Fixed so the same grants produce byte-identical bundles — a bundle
#: that differs every time it is written cannot be diffed, checksummed in a cover
#: letter, or compared against the one a reviewer says they received.
ZIP_EPOCH = (1980, 1, 1, 0, 0, 0)

#: Files that can name an archivefs dependency. Narrow on purpose: a wider sweep would
#: pick URLs out of a README and offer to export keys for deposits the code never
#: touches.
DVC_FILES = ("dvc.lock", "dvc.yaml")
DVC_CONFIG = ("config", "config.local")


@dataclass(frozen=True)
class Grant:
    """One credential, for one scope, on its way to or from a bundle."""

    protocol: str
    credential: str
    scope: str | None
    value: str

    @property
    def label(self) -> str:
        return f"{self.protocol}://{self.scope}" if self.scope else f"{self.protocol}"


@dataclass
class Bundle:
    grants: list[Grant] = field(default_factory=list)
    description: str = ""
    #: One URL per grant, so ``import`` can print a command that proves it worked.
    verify: list[str] = field(default_factory=list)


@dataclass
class Usage:
    """One deposit this repository depends on, and where it was referenced."""

    protocol: str
    scope: str
    urls: set[str] = field(default_factory=set)
    files: set[str] = field(default_factory=set)


@dataclass
class Row:
    """One line of the export report: a deposit, and what we could do about it."""

    protocol: str
    scope: str | None
    #: ``included`` | ``missing`` | ``public`` | ``personal`` | ``unavailable``
    status: str
    detail: str
    references: int = 0


@dataclass
class Change:
    """What importing one grant would do to one file on disk."""

    path: Path
    grant: Grant
    #: ``add`` | ``unchanged`` | ``conflict``
    action: str


# --------------------------------------------------------------- backend lookup


def backend_class(protocol: str):
    """The filesystem class for ``protocol``, or raise with the extra to install."""
    from archivefs.registry import PROTOCOLS

    try:
        backend = PROTOCOLS[protocol]
    except KeyError:
        raise ArchiveFSError(
            f"unknown protocol {protocol!r}; archivefs speaks: "
            f"{', '.join(sorted(PROTOCOLS))}"
        ) from None
    module_name, _, class_name = backend.class_path.rpartition(".")
    import importlib

    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        extra = f"archivefs[{backend.extra}]" if backend.extra else "archivefs"
        raise ArchiveFSError(
            f"{protocol}:// is not installed here ({exc}); install {extra}"
        ) from exc
    return getattr(module, class_name)


def declared(protocol: str) -> list[Credential]:
    return list(getattr(backend_class(protocol), "credentials", []))


def _archive_name(protocol: str) -> str:
    return getattr(backend_class(protocol), "archive_name", protocol)


def _destination(protocol: str, credential: Credential) -> Path:
    """Which of a credential's files we *write* to.

    Reading tries several — EGA also honours ``~/.pyega3/credentials.json``, which is
    pyega3's file and not ours to rewrite. So writing picks the archivefs-owned one,
    and the same expanded path the resolver will later read.
    """
    for candidate in credential.files:
        if "archivefs" in candidate:
            return Path(candidate).expanduser()
    if credential.files:
        return Path(credential.files[0]).expanduser()
    raise ArchiveFSError(
        f"{protocol}:// declares no credentials file for {credential.name!r}, so "
        f"there is nowhere to install it. Set ${credential.env[0]} instead."
        if credential.env
        else f"{protocol}:// declares nowhere to keep {credential.name!r}."
    )


# ------------------------------------------------------------------- scanning


def _dvc_paths(root: Path):
    """Every file in the repository that can carry an archivefs dependency."""
    skip = {".git", "cache", "tmp", "site-packages", "node_modules", ".venv"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        here = Path(dirpath)
        for name in filenames:
            if name.endswith(".dvc") or name in DVC_FILES:
                yield here / name
    for name in DVC_CONFIG:
        candidate = root / ".dvc" / name
        if candidate.exists():
            yield candidate


def scan(root: Path) -> list[Usage]:
    """Which deposits this repository depends on, from its DVC files.

    Text matching, not YAML parsing: the CLI is stdlib-only and has no YAML reader,
    and the question here — "does this repo mention ``bia://S-BIAD3080``" — does not
    need one. It over-matches rather than under-matches (a URL in a ``desc:`` field
    counts), which is the safe direction: the report lists everything it found and a
    spurious row costs a line of output, where a missed one costs a reviewer their
    access.
    """
    from archivefs.registry import PROTOCOLS

    pattern = re.compile(
        r"\b(" + "|".join(sorted(PROTOCOLS)) + r")://([^\s\"'<>,)\]}]+)"
    )
    found: dict[tuple[str, str], Usage] = {}
    for path in _dvc_paths(root):
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        for protocol, rest in pattern.findall(text):
            scope = rest.split("/")[0].split("?")[0].strip()
            if not scope:
                continue
            usage = found.setdefault((protocol, scope), Usage(protocol, scope))
            usage.urls.add(f"{protocol}://{rest}")
            usage.files.add(str(path.relative_to(root)))
    return sorted(found.values(), key=lambda u: (u.protocol, u.scope))


# -------------------------------------------------------------------- export


def collect(
    root: Path, include: list[str] | None = None
) -> tuple[Bundle, list[Row], list[Usage]]:
    """Plan an export: what this repo needs, and which of it we can hand over.

    Returns the bundle, one report row per deposit, and the raw scan. Rows are
    produced for deposits we can do nothing about too — a reviewer told nothing about
    the EGA dataset in the repository will discover it the hard way, at ``dvc pull``.
    """
    usages = scan(root)
    extra: list[tuple[str, str]] = []
    for item in include or []:
        protocol, _, scope = item.partition(":")
        if not scope:
            raise ArchiveFSError(
                f"--include expects PROTOCOL:ACCESSION, got {item!r} "
                f"(e.g. --include bia:S-BIAD3080)"
            )
        extra.append((protocol, scope))
        if not any(u.protocol == protocol and u.scope == scope for u in usages):
            usages.append(Usage(protocol, scope))
    usages.sort(key=lambda u: (u.protocol, u.scope))

    bundle = Bundle()
    rows: list[Row] = []
    resolvers: dict[str, CredentialResolver] = {}
    for usage in usages:
        rows.append(_row_for(usage, bundle, resolvers))
    return bundle, rows, usages


def _row_for(
    usage: Usage, bundle: Bundle, resolvers: dict[str, CredentialResolver]
) -> Row:
    references = len(usage.urls)
    try:
        credentials = declared(usage.protocol)
    except ArchiveFSError as exc:
        return Row(usage.protocol, usage.scope, "unavailable", str(exc), references)

    if not credentials:
        return Row(
            usage.protocol,
            usage.scope,
            "public",
            "needs no credential; nothing to hand over",
            references,
        )

    shareable = [c for c in credentials if c.shareable]
    if not shareable:
        names = ", ".join(sorted(c.name for c in credentials))
        verb = "authenticates" if len(credentials) == 1 else "authenticate"
        # Optional-only means the archive is public unless this particular deposit is
        # restricted; saying "a reviewer needs their own account" for a public Zenodo
        # record would send someone to request access they already have.
        if all(not c.required for c in credentials):
            detail = (
                f"public unless this deposit is restricted; {names} {verb} an account "
                f"and cannot be handed on"
            )
        else:
            detail = (
                f"{names} {verb} an account, not a deposit — a reviewer needs their "
                f"own, and no bundle can supply them"
            )
        return Row(usage.protocol, usage.scope, "personal", detail, references)

    if usage.protocol not in resolvers:
        resolvers[usage.protocol] = CredentialResolver(
            _archive_name(usage.protocol), credentials
        )
    resolver = resolvers[usage.protocol]

    held: list[str] = []
    for credential in shareable:
        scope = usage.scope if credential.scoped else None
        try:
            found = resolver.resolve(credential.name, scope=scope)
        except CredentialsNotFoundError:
            continue
        except CredentialSourceError as exc:
            return Row(usage.protocol, usage.scope, "unavailable", str(exc), references)
        if not found.value:
            continue
        bundle.grants.append(Grant(usage.protocol, credential.name, scope, found.value))
        # Where it came from, not just that it was found. A scoped credential can
        # resolve from a *flat* source — $BIA_KEY, or a bare "key" entry — which
        # answers for every accession asked of it. Exporting that pins one key to a
        # deposit it may not open, and the depositor is the only person who can tell.
        # Silence here would ship a bundle that fails on the reviewer's machine.
        if scope and not found.source.endswith(f"({scope})"):
            held.append(
                f"{credential.name} from {found.source}, which is not per-study — "
                f"check it is this deposit's"
            )
        else:
            held.append(f"{credential.name} from {found.source}")
    if held:
        if usage.urls:
            bundle.verify.append(min(usage.urls, key=len))
        return Row(usage.protocol, usage.scope, "included", "; ".join(held), references)
    return Row(
        usage.protocol,
        usage.scope,
        "missing",
        f"no {', '.join(c.name for c in shareable)} held for this deposit; if it is "
        f"still unreleased, reviewers will not be able to read it",
        references,
    )


# ------------------------------------------------------------ serialise / read


def _manifest(bundle: Bundle) -> dict:
    return {
        "archivefs_bundle": BUNDLE_VERSION,
        "description": bundle.description,
        "grants": [
            {
                "protocol": g.protocol,
                "credential": g.credential,
                "scope": g.scope,
                "value": g.value,
            }
            for g in bundle.grants
        ],
        "verify": sorted(set(bundle.verify)),
    }


def readme(bundle: Bundle, filename: str = "archivefs-access.zip") -> str:
    """For whoever opens the zip instead of reading the email.

    A bundle arrives at a reviewer who has, most likely, never heard of archivefs, and
    a bare JSON of unlabelled UUIDs is indistinguishable from something to ignore.
    """
    deposits = sorted({g.label for g in bundle.grants})
    listed = "\n".join(f"  {d}" for d in deposits) or "  (none)"
    verify = bundle.verify[0] if bundle.verify else "bia://S-BIAD…"
    described = f"\n{bundle.description}\n" if bundle.description else ""
    return f"""This file grants read access to data held in a public archive but not
yet released, so that it can be reviewed before publication.
{described}
It contains live access keys. Treat it as you would a password: do not
commit it to a repository, and delete it once it is installed.

Deposits it opens:
{listed}

To install it:

    pip install archivefs
    archivefs credentials import {filename}

That writes the keys to a file under your home directory that only you can
read. Nothing is sent anywhere. Then check it worked:

    archivefs ls {verify}

The data itself is fetched on demand from the archive, either with
`archivefs get`, or by `dvc pull` if you were given a repository.

Keys stop working when the depositor revokes them, and keep working after
the data is published, at which point they are no longer needed.
"""


def write(
    bundle: Bundle, as_zip: bool = True, filename: str = "archivefs-access.zip"
) -> bytes:
    """Serialise a bundle. Deterministic: same grants, same bytes."""
    manifest = json.dumps(_manifest(bundle), indent=2, sort_keys=True) + "\n"
    if not as_zip:
        return manifest.encode()
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        contents = ((MANIFEST_NAME, manifest), (README_NAME, readme(bundle, filename)))
        for name, text in contents:
            info = zipfile.ZipInfo(name, date_time=ZIP_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, text)
    return buffer.getvalue()


def _from_manifest(data: dict, where: str) -> Bundle:
    version = data.get("archivefs_bundle")
    if version is None:
        raise ArchiveFSError(
            f"{where} is JSON but not an archivefs bundle: it has no "
            f"'archivefs_bundle' key."
        )
    if not isinstance(version, int) or version > BUNDLE_VERSION:
        raise ArchiveFSError(
            f"{where} is a version {version} bundle and this archivefs understands "
            f"up to {BUNDLE_VERSION}. Upgrade archivefs rather than editing it."
        )
    grants = []
    for entry in data.get("grants") or []:
        try:
            grants.append(
                Grant(
                    protocol=str(entry["protocol"]),
                    credential=str(entry["credential"]),
                    scope=entry["scope"] if entry.get("scope") else None,
                    value=str(entry["value"]),
                )
            )
        except (KeyError, TypeError) as exc:
            raise ArchiveFSError(
                f"{where} has a malformed grant ({exc}); each needs protocol, "
                f"credential and value."
            ) from exc
    return Bundle(
        grants=grants,
        description=str(data.get("description") or ""),
        verify=[str(u) for u in (data.get("verify") or [])],
    )


def from_links(text: str) -> Bundle:
    """Grants carried by sharing links, one per line. Empty if none are recognised."""
    from archivefs.registry import PROTOCOLS

    bundle = Bundle(description="from a sharing link")
    for line in text.splitlines():
        line = line.strip()
        if not line or "://" not in line:
            continue
        for protocol in sorted(PROTOCOLS):
            try:
                cls = backend_class(protocol)
            except ArchiveFSError:
                continue
            for name, scope, value in cls.grants_from_link(line):
                bundle.grants.append(Grant(protocol, name, scope, value))
                if scope:
                    bundle.verify.append(f"{protocol}://{scope}")
    return bundle


def read(data: bytes, where: str = "the bundle") -> Bundle:
    """Parse a bundle from bytes: a zip, a bare manifest, or sharing links."""
    if data[:4] == b"PK\x03\x04":
        with zipfile.ZipFile(BytesIO(data)) as archive:
            try:
                raw = archive.read(MANIFEST_NAME)
            except KeyError:
                raise ArchiveFSError(
                    f"{where} is a zip but holds no {MANIFEST_NAME}; it contains "
                    f"{', '.join(archive.namelist()) or 'nothing'}."
                ) from None
        return _from_manifest(json.loads(raw), where)
    text = data.decode("utf-8", errors="replace").strip()
    if text.startswith("{"):
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise ArchiveFSError(f"{where} is not valid JSON: {exc}") from exc
        return _from_manifest(parsed, where)
    bundle = from_links(text)
    if not bundle.grants:
        head = text[:60].replace("\n", " ")
        raise ArchiveFSError(
            f"{where} is neither an archivefs bundle nor a sharing link carrying a "
            f"key (it starts {head!r}). Expected the .zip or .json from `archivefs "
            f"credentials export`, or a link such as "
            f"https://www.ebi.ac.uk/biostudies/…/studies/S-BIAD3080?key=…"
        )
    return bundle


# -------------------------------------------------------------------- install


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text())
    except ValueError as exc:
        raise ArchiveFSError(
            f"{path} already exists and is not valid JSON ({exc}). archivefs will not "
            f"overwrite it; move it aside and import again."
        ) from exc
    if not isinstance(payload, dict):
        raise ArchiveFSError(f"{path} must hold a JSON object, not a {type(payload)}.")
    return payload


def _current(payload: dict, credential: Credential, scope: str | None) -> str | None:
    key = credential.file_key or credential.name
    if credential.scoped and scope:
        mapping = payload.get(key)
        if isinstance(mapping, dict) and scope in mapping:
            return str(mapping[scope])
        return None
    if credential.scoped:
        return payload.get(credential.name)
    return payload.get(key)


def _place(payload: dict, credential: Credential, scope: str | None, value: str):
    key = credential.file_key or credential.name
    if credential.scoped and scope:
        mapping = payload.setdefault(key, {})
        if not isinstance(mapping, dict):
            raise ArchiveFSError(
                f"{key!r} is already set to something that is not a mapping, so a "
                f"per-deposit key cannot be added beside it."
            )
        mapping[scope] = value
    elif credential.scoped:
        payload[credential.name] = value
    else:
        payload[key] = value


def _credential(grant: Grant) -> Credential:
    for candidate in declared(grant.protocol):
        if candidate.name == grant.credential:
            return candidate
    raise ArchiveFSError(
        f"the bundle carries {grant.credential!r} for {grant.protocol}://, which this "
        f"version of archivefs does not know about. Upgrade archivefs."
    )


def plan(bundle: Bundle) -> list[Change]:
    """What importing would do, without doing any of it."""
    cache: dict[Path, dict] = {}
    changes: list[Change] = []
    for grant in bundle.grants:
        credential = _credential(grant)
        if not credential.shareable:
            raise ArchiveFSError(
                f"the bundle carries {grant.protocol}:// {grant.credential}, which "
                f"archivefs does not treat as shareable — it authenticates an account "
                f"rather than opening one deposit. Refusing to install it; whoever "
                f"sent this should rotate it."
            )
        path = _destination(grant.protocol, credential)
        payload = cache.setdefault(path, _load(path))
        existing = _current(payload, credential, grant.scope)
        if existing is None:
            action = "add"
        elif existing == grant.value:
            action = "unchanged"
        else:
            action = "conflict"
        changes.append(Change(path, grant, action))
    return changes


def install(changes: list[Change], force: bool = False) -> list[Path]:
    """Apply a plan. Returns the files written; conflicts raise unless ``force``."""
    conflicts = [c for c in changes if c.action == "conflict"]
    if conflicts and not force:
        listed = "\n".join(
            f"    {c.grant.label} {c.grant.credential} in {c.path}" for c in conflicts
        )
        raise ArchiveFSError(
            f"{len(conflicts)} credential(s) already set to a different value:\n"
            f"{listed}\n"
            f"  Nothing was written. A key replaced without you noticing fails later, "
            f"as a 403 with no obvious cause.\n"
            f"  Keep what you have, or re-run with --force to take the bundle's."
        )
    todo = [
        c
        for c in changes
        if c.action == "add" or (force and c.action != "unchanged")
    ]
    written: list[Path] = []
    by_path: dict[Path, list[Change]] = {}
    for change in todo:
        by_path.setdefault(change.path, []).append(change)
    for path, group in by_path.items():
        payload = _load(path)
        for change in group:
            _place(
                payload,
                _credential(change.grant),
                change.grant.scope,
                change.grant.value,
            )
        write_private(
            path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
        )
        written.append(path)
    return written


def write_private(path: Path, data: bytes) -> None:
    """Write ``0600``, atomically, creating a ``0700`` parent.

    Atomic because the alternative — truncate, then write — loses every other key in
    the file if it fails halfway, and the file holds credentials for deposits this
    import is not touching. The mode is set on the temporary file *before* the content
    goes in, so there is no window in which a secret sits at the default umask.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    # os.replace keeps the *temporary* file's mode, but an existing destination that
    # was group-readable would otherwise stay that way and be refused at read time.
    os.chmod(path, 0o600)


def exposed(path: Path) -> bool:
    """Would the resolver refuse this file for its permissions?"""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return bool(stat.S_IMODE(mode) & (stat.S_IRWXG | stat.S_IRWXO))


# --------------------------------------------------------------- what is held


@dataclass
class Holding:
    """What we hold for one declared credential. Never the value."""

    protocol: str
    credential: str
    shareable: bool
    scoped: bool
    required: bool
    #: ``$VAR``, a path, or ``None`` when nothing was found
    source: str | None = None
    #: deposits a scoped credential is held for; ``["*"]`` for a flat catch-all entry
    scopes: list[str] = field(default_factory=list)
    #: set when the file exists but its permissions mean the resolver will refuse it
    warning: str | None = None


def holdings(protocol: str) -> list[Holding]:
    """A secret-free inventory: which credentials exist here, and from where.

    Reads the files directly rather than going through
    :class:`CredentialResolver`, because this must answer "which studies do I have
    keys for" — a question with no single answer for a scoped credential, and one the
    resolver deliberately cannot be asked without naming a scope up front.
    """
    out: list[Holding] = []
    for credential in declared(protocol):
        holding = Holding(
            protocol=protocol,
            credential=credential.name,
            shareable=credential.shareable,
            scoped=credential.scoped,
            required=credential.required,
        )
        for variable in credential.env:
            if os.environ.get(variable):
                holding.source = f"${variable}"
                holding.scopes = ["*"]
                break
        for candidate in credential.files:
            path = Path(candidate).expanduser()
            if not path.exists():
                continue
            if exposed(path):
                holding.warning = (
                    f"{path} is readable by others; the resolver will refuse it "
                    f"(chmod 600 {path})"
                )
                continue
            try:
                payload = json.loads(path.read_text())
            except (OSError, ValueError):
                holding.warning = f"{path} could not be read as JSON"
                continue
            if not isinstance(payload, dict):
                continue
            key = credential.file_key or credential.name
            scopes: list[str] = []
            if credential.scoped:
                mapping = payload.get(key)
                if isinstance(mapping, dict):
                    scopes += sorted(str(k) for k in mapping)
                if payload.get(credential.name):
                    scopes.append("*")
            elif payload.get(key):
                scopes.append("*")
            if scopes and holding.source is None:
                holding.source = str(path)
            holding.scopes = sorted(set(holding.scopes) | set(scopes))
        out.append(holding)
    return out


# --------------------------------------------------------------- write safety


def refuse_in_repo(path: Path) -> str | None:
    """Why ``path`` is a dangerous place for a bundle, or ``None`` if it is fine.

    The realistic accident is not an attacker, it is ``git add -A`` on the same
    repository the bundle was generated from. An already-ignored path is safe from
    that, so it is allowed without ceremony; git answers that question far better than
    we could by re-implementing ``.gitignore``.
    """
    import subprocess

    resolved = path.expanduser().resolve()
    root = next(
        (p for p in (resolved, *resolved.parents) if (p / ".git").exists()), None
    )
    if root is None:
        return None
    try:
        ignored = subprocess.run(  # noqa: S603
            ["git", "-C", str(root), "check-ignore", "-q", str(resolved)],  # noqa: S607
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        ignored = None
    if ignored is not None and ignored.returncode == 0:
        return None
    return (
        f"{path} is inside the git repository at {root} and is not ignored by it.\n"
        f"  A bundle holds live access keys; one `git add -A` publishes them to "
        f"everyone who can read the repo, and rewriting history does not un-publish "
        f"a key.\n"
        f"  Write it somewhere outside the repository, or add it to .gitignore "
        f"first, or send it to stdout and encrypt it:\n"
        f"      archivefs credentials export --out - | age -r <recipient> > pack.age\n"
        f"  Use --force to write it here anyway."
    )
