"""``archivefs ping`` — is a pin pullable right now, and if not, whose fault?

A reviewer who runs ``dvc pull`` and hits a gateway timeout cannot tell *the archive is
down, retry later* from *my setup is broken* from *I lack access* — they learn it only
minutes into a download, as a traceback. ``ping`` answers in seconds and cheaply: it
walks the exact chain a pull uses — resolve the pin, reach the archive's metadata
endpoint, fetch a **single sample byte** from the content endpoint — and returns a
verdict that names the culprit. Crucially it separates a *transient* archive outage (not
your fault, retry) from a real one (gone / auth / network / setup).

It drives the archive directly (the record's ``info`` = the metadata endpoint; a ranged
``cat_file`` = the content endpoint), with the backend built to fail fast, so a single
attempt is quick and ``--retries`` controls how long you wait for a flaky archive.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from archivefs.errors import (
    ArchiveNotFoundError,
    AuthenticationError,
    ManifestError,
    MissingExtraError,
)

__all__ = ["PULLABLE", "PingResult", "ping"]

# ---- verdicts -------------------------------------------------------------
PULLABLE = "pullable"
ARCHIVE_UNAVAILABLE = "archive-unavailable"  # transient — retry helps
GONE = "gone"
AUTH = "auth"
NETWORK = "network"
SETUP = "setup"

#: verdict -> (headline, what to do). Transient ones say so.
_ADVICE = {
    PULLABLE: ("PULLABLE", "the data is reachable, go ahead"),
    ARCHIVE_UNAVAILABLE: (
        "ARCHIVE-UNAVAILABLE (transient)",
        "the archive is down, not your setup; retry shortly",
    ),
    GONE: ("GONE", "the record or file is missing or withdrawn — the pin is broken"),
    AUTH: ("AUTH", "you need credentials for this record (or they were rejected)"),
    NETWORK: (
        "NETWORK",
        "cannot reach the archive from here — no internet? (a compute node has none; "
        "use a login/data-mover node)",
    ),
    SETUP: ("SETUP", "a local problem — see the message"),
}

#: The sample read: one byte proves the content endpoint without moving data.
_SAMPLE_BYTES = 1
#: Fast-fail per attempt; --retries controls patience, not per-request backoff.
_PING_OPTIONS = {"max_retries": 1, "request_timeout": 30}
_ARCHIVE_OF_SCHEME = {"doi": "zenodo", "biostudies": "bia", "geo": "geo", "ega": "ega"}


@dataclass
class PingResult:
    target: str
    archive: str | None = None
    detail: str | None = None
    verdict: str = PULLABLE
    message: str = ""
    sample: str | None = None

    @property
    def ok(self) -> bool:
        return self.verdict == PULLABLE

    @property
    def transient(self) -> bool:
        return self.verdict == ARCHIVE_UNAVAILABLE


def classify(exc: BaseException) -> tuple[str, str]:
    """Map an exception raised while reaching an archive to a ``(verdict, message)``."""
    import requests

    if isinstance(exc, ArchiveNotFoundError):
        return GONE, str(exc)
    if isinstance(exc, AuthenticationError):
        return AUTH, str(exc)
    if isinstance(exc, (MissingExtraError, ManifestError)):
        return SETUP, str(exc)
    if isinstance(exc, requests.HTTPError):
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code and 500 <= code < 600:
            return ARCHIVE_UNAVAILABLE, f"the archive returned HTTP {code}"
        return ARCHIVE_UNAVAILABLE, str(exc)
    cause = exc.__cause__ or exc.__context__
    if isinstance(cause, requests.exceptions.ConnectionError):
        return NETWORK, "cannot reach the archive (connection failed / no route / DNS)"
    if isinstance(cause, requests.exceptions.Timeout):
        return ARCHIVE_UNAVAILABLE, "the archive timed out"
    if isinstance(exc, OSError):
        text = str(exc)
        if any(t in text for t in ("500", "502", "503", "504", "Gateway", "imeout")):
            return ARCHIVE_UNAVAILABLE, text
        return NETWORK, text
    return SETUP, f"{type(exc).__name__}: {exc}"


def _dep_url(dvc_file: Path) -> str:
    import yaml

    doc = yaml.safe_load(dvc_file.read_text()) or {}
    url = next(
        (d.get("path") for d in (doc.get("deps") or []) if d.get("path")), None
    )
    if not url:
        raise ManifestError(f"{dvc_file}: no dependency URL to ping.")
    return url


def _resolve(target: str) -> dict[str, Any]:
    """Work out what to probe — no network. Returns the routing + a sample to fetch."""
    from archivefs import store

    if target.endswith(".dvc"):
        path = Path(target).expanduser()
        if not path.is_file():
            raise ManifestError(f"no .dvc file at {path}.")
        target = _dep_url(path)

    if target.startswith("chiro://"):
        from archivefs.chiro import _chirograph
        from archivefs.registry import protocol_for_concept

        chirograph = _chirograph()
        ref = store.as_ref(target)
        if ref is None:
            raise ManifestError(f"{target} is not a resolvable chiro://md5 reference.")
        manifest = chirograph.load(store.resolve_ref(ref))
        concept = getattr(manifest, "concept", None)
        if not concept:
            raise ManifestError(f"{target}: manifest has no concept to route on.")
        routed = protocol_for_concept(concept)
        if routed is None:
            raise ManifestError(f"{target}: concept {concept!r} routes to no backend.")
        protocol, concept_accession = routed
        accession = getattr(manifest, "record_accession", None) or concept_accession
        entry = manifest.files[0]
        scheme = concept.split(":", 1)[0]
        return {
            "protocol": protocol,
            "accession": accession,
            "sample_path": f"{accession}/{entry.source}",
            "extract_zips": bool(getattr(manifest, "containers", None)),
            "archive": _ARCHIVE_OF_SCHEME.get(scheme, scheme),
            "detail": concept,
        }

    # A raw archive URL (zenodo://…, bia://…): probe it directly, list for a sample.
    protocol, sep, rest = target.partition("://")
    if not sep:
        raise ManifestError(
            f"{target!r} is not a chiro:// / archive URL or a .dvc file."
        )
    return {
        "protocol": protocol,
        "accession": rest,
        "sample_path": None,  # discovered by listing
        "extract_zips": False,
        "archive": _ARCHIVE_OF_SCHEME.get(protocol, protocol),
        "detail": rest,
    }


def _attempt(plan: dict[str, Any], step: Callable[[str, str], None]) -> PingResult:
    """One ping pass: metadata reach, then a single content byte."""
    import fsspec

    result = PingResult(
        target=plan["target"], archive=plan["archive"], detail=plan["detail"]
    )
    options = {**_PING_OPTIONS}
    if plan["extract_zips"]:
        options["extract_zips"] = True
    fs = fsspec.filesystem(plan["protocol"], skip_instance_cache=True, **options)
    accession = plan["accession"]

    step("start", "reaching the archive (record metadata)")
    try:
        fs.info(accession)
    except Exception as exc:  # noqa: BLE001 - classified into a verdict
        result.verdict, result.message = classify(exc)
        step("fail", f"metadata: {result.message}")
        return result
    step("ok", "metadata ok")

    sample_path = plan["sample_path"]
    if sample_path is None:  # raw archive URL — find one file to sample
        files = [p for p in fs.find(accession) if fs.info(p).get("type") == "file"]
        if not files:
            result.verdict, result.message = SETUP, f"{accession} lists no files."
            return result
        sample_path = files[0]
    result.sample = sample_path.split("/", 1)[-1]

    step("start", f"fetching a sample byte from {result.sample}")
    try:
        fs.cat_file(sample_path, start=0, end=_SAMPLE_BYTES)
    except Exception as exc:  # noqa: BLE001 - classified into a verdict
        result.verdict, result.message = classify(exc)
        step("fail", f"content: {result.message}")
        return result
    step("ok", "content ok")
    result.verdict, result.message = PULLABLE, _ADVICE[PULLABLE][1]
    return result


def ping(
    target: str,
    *,
    retries: int = 1,
    delay: float = 15.0,
    on_step: Callable[[str, str], None] | None = None,
) -> PingResult:
    """Probe whether ``target`` is pullable now, retrying a transient failure.

    ``target`` is a ``chiro://md5:`` reference, an archive URL, or a ``.dvc`` file. Each
    attempt reaches the record's metadata endpoint and fetches one content byte, with a
    fail-fast backend. ``on_step(kind, message)`` reports progress (``kind`` is one of
    ``start``/``ok``/``fail``) so a slow request does not look like a hang. Only a
    *transient* verdict is retried (``delay`` seconds apart); a real one returns now.
    """
    step = on_step or (lambda _k, _m: None)
    plan = _resolve(target)
    plan["target"] = target
    step("ok", f"resolved: {plan['archive']} · {plan['detail']}")
    result = _attempt(plan, step)
    tried = 1
    while result.transient and tried < retries:
        step("wait", f"transient — retrying in {delay:.0f}s ({tried}/{retries})")
        time.sleep(delay)
        result = _attempt(plan, step)
        tried += 1
    return result


def verdict_line(result: PingResult) -> str:
    """The final headline + advice for a result."""
    headline, advice = _ADVICE.get(result.verdict, (result.verdict.upper(), ""))
    tail = result.message or advice
    return f"{headline} — {tail}"
