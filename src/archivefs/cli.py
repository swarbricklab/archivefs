"""``archivefs`` — a small CLI, mostly for finding out *why* something failed.

The library is the product; this exists because three questions come up constantly
and none of them should require writing Python:

* "is this thing even installed properly, and does DVC know about it?"
  → ``archivefs info`` / ``archivefs dvc status``
* "what is in this record, and what does it claim about itself?"
  → ``archivefs ls`` / ``archivefs stat``
* "does what I downloaded match what the deposit says it should be?"
  → ``archivefs check`` / ``archivefs verify``
* "give me a portable, content-addressed pin of this deposit"
  → ``archivefs manifest``
* "has the upstream moved since I pinned it, and re-pin if so"
  → ``archivefs update``

Deliberately stdlib-only: a diagnostic tool that needs its own dependency tree is
a diagnostic tool that cannot run when you need it.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from archivefs import __version__
from archivefs.errors import ArchiveFSError
from archivefs.registry import PROTOCOLS, installed_backends


def _coerce_option(value: Any) -> Any:
    """``"true"``/``"false"`` strings become bools; everything else passes through.

    Needed because ``bool("false")`` is ``True`` — an option carried as a string (from
    `-o`) has to be coerced or ``extract_zips=false`` silently turns the feature *on*.
    """
    if isinstance(value, str) and value in ("true", "false"):
        return value == "true"
    return value


def _filesystem(url: str, options: list[str] | None = None) -> tuple[Any, str]:
    import fsspec

    protocol, _, rest = url.partition("://")
    if not rest:
        raise SystemExit(
            f"{url!r} does not look like an archive URL. Expected e.g. "
            f"zenodo://1486184 or ega://EGAD00001006673/EGAF00004908765."
        )
    if "::" in url:
        # A chained URL is an fsspec/Python form: its storage options nest per
        # protocol, and `-o key=value` has nowhere unambiguous to go. Say so and
        # give the CLI equivalent, rather than leave the reader with "unknown
        # protocol 'zip'" for a URL the docs told them to write.
        inner = url.split("::", 1)[1]
        raise SystemExit(
            f"{url!r} is a chained fsspec URL, which this CLI does not take — "
            f"`-o` could not tell which protocol an option belonged to. Either\n"
            f"  archivefs ls -o extract_zips=true {inner}\n"
            f"or, for the chain itself, use Python:\n"
            f'  fsspec.core.url_to_fs("{url}")'
        )
    if protocol not in PROTOCOLS:
        raise SystemExit(
            f"unknown protocol {protocol!r}. archivefs speaks: "
            f"{', '.join(sorted(PROTOCOLS))}"
        )
    # `extra` (e.g. a .dvc's `fs_config`) is the base; explicit `-o` on the command
    # line overrides it.
    fs = fsspec.filesystem(protocol, **_parse_options(options))
    return fs, rest


def _parse_options(options: list[str] | None) -> dict[str, Any]:
    storage_options: dict[str, Any] = {}
    for item in options or []:
        key, _, value = item.partition("=")
        if not value:
            raise SystemExit(f"--option expects key=value, got {item!r}")
        storage_options[key] = _coerce_option(value)
    return storage_options


def blockcache_env() -> str:
    from archivefs.blockcache import CACHE_DIR_ENV

    return CACHE_DIR_ENV


def _human(size: int | None) -> str:
    if size is None:
        return "?"
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if value < 1024 or unit == "PiB":
            return f"{value:,.0f} {unit}" if unit == "B" else f"{value:,.1f} {unit}"
        value /= 1024
    return f"{size} B"  # pragma: no cover


# ------------------------------------------------------------------- commands


def cmd_info(args: argparse.Namespace) -> int:
    report = installed_backends()
    if args.json:
        from archivefs.dvcfs import status as dvc_status

        try:
            dvc = dvc_status()
        except Exception as exc:  # noqa: BLE001
            dvc = {"error": str(exc)}
        payload = {"version": __version__, "backends": report, "dvc": dvc}
        print(json.dumps(payload, indent=2))
        return 0

    print(f"archivefs {__version__}")
    print()
    for name, entry in report.items():
        if entry["status"] == "deferred":
            mark, note = "–", "deferred (not implemented)"
        elif entry["ready"]:
            mark, note = "✓", "ready"
        else:
            missing = entry["missing_packages"] + entry["missing_tools"]
            mark, note = "✗", f"missing: {', '.join(missing)}"
        print(f"  {mark} {name + '://':10} {note:34} {entry['summary']}")
    print()
    from archivefs.chiro import have_chirograph

    if have_chirograph():
        print("  ✓ chirograph      manifests give download-free checksums")
    else:
        print(
            "  – chirograph      not installed; .chiro manifests will be ignored "
            "(pip install 'archivefs[chiro]')"
        )
    try:
        from archivefs.dvcfs import status as dvc_status

        dvc = dvc_status()
    except Exception as exc:  # noqa: BLE001
        print(f"  ? dvc             could not check: {exc}")
        return 0
    if not dvc["dvc_installed"]:
        print(
            "  – dvc             not installed (archivefs works with plain fsspec "
            "regardless)"
        )
    elif dvc["hook_installed"]:
        print(f"  ✓ dvc             registered for {', '.join(dvc['protocols'])}")
    else:
        print(
            "  ✗ dvc             installed but archivefs is NOT registered with it; "
            "run: archivefs dvc enable"
        )
    return 0


def cmd_ls(args: argparse.Namespace) -> int:
    fs, path = _filesystem(args.url, args.option)
    if args.recursive:
        # ls() is children-only, as fsspec requires; find() is the recursive one.
        entries = [fs.info(p) for p in fs.find(path)]
    else:
        entries = fs.ls(path, detail=True)
        if args.long:
            # A listing leaves an unknown size unknown, because resolving one can
            # cost a request per file (GEO). Asking for -l is asking for that.
            entries = [
                fs.info(e["name"])
                if e.get("type") == "file" and e.get("size") is None
                else e
                for e in entries
            ]
    if args.json:
        _emit(fs, entries)
        return 0
    for entry in sorted(entries, key=lambda e: e["name"]):
        if args.long:
            digest = entry.get("md5") or "-"
            print(f"{_human(entry.get('size')):>12}  {digest:32}  {entry['name']}")
        else:
            print(entry["name"])
    return 0


def _emit(fs: Any, payload: Any) -> None:
    """Print a filesystem's own metadata as JSON, with any secret in it masked.

    Serialise first, then redact the *text*. Applying the backend's ``_redact`` to
    the whole document rather than to a named field is the point: for BioStudies an
    unreleased study's URL literally contains the sharing key
    (``.private/<key[:2]>/<key[2:]>/…``), and that URL has to stay real inside
    ``info()`` because it is what we fetch with. So the capability is allowed to
    exist in the dict and is never allowed to reach a stream — and doing it by field
    name would have missed ``url`` exactly as it was missed once already, then missed
    the next field someone adds.

    Identity for every archive whose URLs are only addresses.
    """
    print(fs._redact(json.dumps(payload, indent=2, default=str)))


def cmd_stat(args: argparse.Namespace) -> int:
    fs, path = _filesystem(args.url, args.option)
    _emit(fs, fs.info(path))
    return 0


def _version_row(entry: dict) -> tuple[str, str, str]:
    """``(identifier, date, label)`` from a backend's version dict, generically.

    The backends return different shapes — cBioPortal a git commit, Zenodo/Mendeley a
    record/version — so pick the most identifying field each publishes rather than
    hard-code one backend's keys here.
    """
    ident = (
        entry.get("accession")
        or entry.get("doi")
        or entry.get("commit")
        or str(entry.get("version") or entry.get("record_id") or "?")
    )
    date = entry.get("date") or entry.get("publish_date") or entry.get("updated") or ""
    label = entry.get("message") or entry.get("title") or entry.get("name") or ""
    return ident, date, label


def cmd_versions(args: argparse.Namespace) -> int:
    """List a deposit's versions, newest first (cbioportal/zenodo/mendeley)."""
    fs, path = _filesystem(args.url, args.option)
    versions = getattr(fs, "versions", None)
    if not callable(versions):
        raise SystemExit(
            f"{getattr(fs, 'protocol', '?')}:// does not expose a version history. "
            f"Versions are available for cbioportal://, zenodo:// and mendeley://."
        )
    # A member URL still lists the *container's* versions, so reduce to the container.
    container, _ = fs._split(fs._strip_protocol(path))
    entries = versions(container)
    if args.json:
        _emit(fs, entries)
        return 0
    if not entries:
        print("no versions found")
        return 0
    for entry in entries:
        ident, date, label = _version_row(entry)
        print(f"{ident}  {date}  {label}".rstrip())
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    import hashlib
    import posixpath
    from pathlib import Path

    fs, path = _filesystem(args.url, args.option)
    destination = Path(args.dest)
    if fs.isdir(path):
        destination.mkdir(parents=True, exist_ok=True)
        targets = [(p, destination / p[len(path) :].lstrip("/")) for p in fs.find(path)]
    else:
        if destination.is_dir():
            # `cp`-style: a directory destination means "in here, same name".
            destination = destination / posixpath.basename(path)
        targets = [(path, destination)]

    failures = 0
    for source, target in targets:
        target.parent.mkdir(parents=True, exist_ok=True)
        info = fs.info(source)
        with fs.open(source, "rb") as handle, target.open("wb") as sink:
            digest = hashlib.md5()  # noqa: S324 - md5 is what these archives publish
            while True:
                chunk = handle.read(4 * 1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                sink.write(chunk)
        expected = info.get("md5")
        if expected and digest.hexdigest() != expected:
            print(
                f"MISMATCH {target}: expected {expected}, got {digest.hexdigest()}",
                file=sys.stderr,
            )
            failures += 1
        else:
            state = "verified" if expected else "no checksum published"
            print(f"{target} ({_human(info.get('size'))}, {state})")
    return 1 if failures else 0


def cmd_check(args: argparse.Namespace) -> int:
    fs, path = _filesystem(args.url, args.option)
    container, _ = fs._split(fs._strip_protocol(path))
    manifest = fs.manifest_path_for(container)
    if manifest is None:
        print(f"{container}: no .chiro.json manifest found (native listing in use)")
        return 0
    drift = fs.drift(container)
    print(drift.summary())
    for relpath in drift.missing_upstream:
        print(f"  missing upstream: {relpath}")
    for name in drift.unrecorded:
        print(f"  not in manifest:  {name}")
    for entry in drift.size_mismatches:
        print(f"  size differs:     {entry}")
    for entry in drift.checksum_mismatches:
        print(f"  checksum differs: {entry}")
    return 0 if drift.clean else 1


def cmd_verify(args: argparse.Namespace) -> int:
    from archivefs.chiro import verify_materialised

    fs, path = _filesystem(args.url, args.option)
    container, _ = fs._split(fs._strip_protocol(path))
    report = verify_materialised(
        fs, container, args.directory, use_source_layout=args.source_layout
    )
    print(
        f"body hash: {'ok' if report.body_hash_ok else 'MISMATCH'} · "
        f"checked {report.checked} file(s) · {len(report.failures)} failure(s)"
    )
    for failure in report.failures:
        print(f"  {failure.status:16} {failure.relpath}")
    return 0 if (report.body_hash_ok and not report.failures) else 1


def cmd_manifest(args: argparse.Namespace) -> int:
    """Generate a .chiro manifest from a deposit, download-free where it can be."""
    from archivefs.chiro import _chirograph
    from archivefs.writer import build_manifest

    chirograph = _chirograph()  # MissingExtraError names the install, not a traceback

    if args.from_dvc:
        # Offline: the .dvc already holds every md5/size/relpath and (for a ZIP) the
        # crc32 + container md5, so the manifest is rebuilt without touching the archive
        # — the whole point being to pin (or prepare) a deposit the archive need not be
        # reachable, or even to exist, for.
        from archivefs.from_dvc import manifest_from_dvc

        manifest = manifest_from_dvc(
            args.from_dvc,
            self_deposited=args.self_deposited,
            concept=args.concept,
        )
    else:
        if not args.url:
            print(
                "manifest needs a URL, or --from-dvc <file.dvc>.", file=sys.stderr
            )
            return 2
        fs, path = _filesystem(args.url, args.option)
        # The whole addressed path, not just its container: `bia://S-BIAD3080/images/ihc`
        # pins that subtree, not the entire study. build_manifest lists the subtree and
        # scopes the concept to it.
        target = fs._strip_protocol(path)
        manifest = build_manifest(
            fs,
            target,
            self_deposited=args.self_deposited,
            download=args.download,
            concept=args.concept,
        )

    if args.out == "-":
        # The document goes to stdout clean (for a pipe); the summary to stderr.
        text = chirograph.dumps(manifest)
        print(text)
        sink = "stdout"
        ref = chirograph.ChiroRef.for_bytes(text.encode())
    elif args.out:
        # An explicit path: write there verbatim. The addressable digest ref still
        # comes from the file's own bytes, but it only resolves if the file is in a
        # store — so report the path form the caller chose.
        out = Path(args.out).expanduser()
        text = chirograph.dumps(manifest)
        out.write_text(text)
        sink = str(out)
        ref = f"chiro://{out}"
    else:
        # Default: into the repo-local store, named by the whole-file digest, so the
        # `chiro://md5:…` reference resolves without anyone recording where it lives.
        from archivefs import store

        dest, chiro_ref = store.write_to_store(manifest)
        sink = str(dest)
        ref = chiro_ref

    print(
        f"{sink}: {manifest.nfiles} file(s), {_human(manifest.size)}, "
        f"{ref} ({manifest.provenance_type})",
        file=sys.stderr,
    )
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    """Regenerate a .chiro pin from the live archive and, with --write, re-pin it."""
    from archivefs import store
    from archivefs.chiro import _chirograph
    from archivefs.update import plan_update

    _chirograph()
    plan = plan_update(
        args.target, _parse_options(args.option), download=args.download
    )
    old_ref = chiroref_for(plan.old)
    if not plan.moved:
        print(f"up to date: {plan.old.concept} — {old_ref} unchanged")
        return 0

    print(f"upstream moved: {plan.diff.summary()}")
    for entry in plan.diff.added:
        print(f"  + {entry.relpath}")
    for entry in plan.diff.removed:
        print(f"  - {entry.relpath}")
    for change in plan.diff.changed:
        print(f"  ~ {change.relpath} ({change.kind})")

    if not args.write:
        print("\nre-run with --write to re-pin to the current upstream.")
        return 1

    # Supersede, do not overwrite: the new manifest is a *new* digest-named file, so
    # the old observation coexists — that pair is the drift record (spec §13). Repoint
    # your .dvc dep to the new reference and commit both. The new file goes in the same
    # store the old pin resolved from (anchored on its path), not wherever the cwd is.
    dest, new_ref = store.write_to_store(plan.new, start=plan.manifest_path)
    print(
        f"\nre-pinned: {old_ref}\n"
        f"       ->  {new_ref}\n"
        f"wrote {dest}\n"
        f"Repoint the dependency to {new_ref} and commit (then `dvc update` if it is a "
        f"DVC import)."
    )
    return 0


def cmd_confirm(args: argparse.Namespace) -> int:
    """Cross-check a manifest against the live deposit, download-free where it can."""
    from archivefs.chiro import _chirograph
    from archivefs.confirm import confirm

    _chirograph()  # MissingExtraError names the install, not a traceback
    report = confirm(
        args.target, _parse_options(args.option), download=args.download
    )
    print(report.summary())
    for container in report.containers:
        mark = "ok" if container.ok else "DIFFERS"
        print(
            f"  container {container.source_root} [{container.confirm_class}]: "
            f"md5 {mark} ({container.actual_md5})"
        )
    for member in report.members:
        if member.ok and member.status == "matched":
            continue  # keep the report to what needs attention
        label = {
            "matched": "ok",
            "size-only": "size-only",
        }.get(member.status, member.status)
        detail = f" — {member.detail}" if member.detail else ""
        print(f"  {label:10} {member.relpath} [{member.via}]{detail}")
    for name in report.unrecorded:
        print(f"  unrecorded {name} (in the deposit, not the manifest)")
    return 0 if report.ok else 1


def chiroref_for(manifest: Any) -> Any:
    """The whole-file `chiro://md5:…` reference for a manifest as re-serialised."""
    from archivefs.chiro import _chirograph

    chirograph = _chirograph()
    return chirograph.ChiroRef.for_bytes(chirograph.dumps(manifest).encode())


def cmd_dvc(args: argparse.Namespace) -> int:
    from archivefs import dvcfs

    if args.action == "status":
        report = dvcfs.status()
        if args.json:
            print(json.dumps(report, indent=2))
            return 0
        if not report["dvc_installed"]:
            print("dvc is not installed in this environment")
            return 1
        print(f"hook: {report['hook_path']}")
        print(f"installed: {report['hook_installed']}")
        works = ", ".join(report["registered"]) or "none"
        print(f"registration works for: {works}")
        print(f"deferred: {', '.join(report['deferred']) or 'none'}")
        if not report["hook_installed"]:
            print()
            print(
                "DVC has no plugin hook for storage backends, so archivefs has to "
                "register itself at interpreter start-up. Run `archivefs dvc enable`."
            )
        return 0 if report["hook_installed"] else 1
    if args.action == "enable":
        path = dvcfs.enable()
        print(f"installed {path}")
        protocols = ", ".join(f"{name}://" for name in sorted(dvcfs.DVC_PROTOCOLS))
        print(f"dvc will now resolve {protocols} URLs.")
        return 0
    path = dvcfs.disable()
    print(f"removed {path}" if path else "nothing to remove")
    return 0


def cmd_dvc_meta(args: argparse.Namespace) -> int:
    """Stamp provenance into a chiro:// pin's .dvc, derived from its manifest."""
    from archivefs.chiro import _chirograph
    from archivefs.dvc_meta import stamp

    _chirograph()  # MissingExtraError names the install, not a traceback
    rc = 0
    for target in args.files:
        try:
            block = stamp(target, write=not args.dry_run)
        except ArchiveFSError as exc:
            print(f"{target}: {exc}", file=sys.stderr)
            rc = 2
            continue
        info = block["archivefs"]
        where = info.get("url") or info.get("source") or info.get("archive")
        verb = "would stamp" if args.dry_run else "stamped"
        print(f"{verb} {target}: {info['archive']} · {where}", file=sys.stderr)
        if args.dry_run:
            import yaml

            print(yaml.safe_dump({"meta": block}, sort_keys=False))
    return rc


def cmd_ping(args: argparse.Namespace) -> int:
    """Is a pin pullable right now? Reach the archive + fetch one sample byte."""
    from archivefs.ping import ping, verdict_line

    def on_step(kind: str, message: str) -> None:
        mark = {"start": "  … ", "ok": "  ✓ ", "fail": "  ✗ ", "wait": "  ⏳ "}
        suffix = "…" if kind == "start" else ""
        print(f"{mark.get(kind, '  ')}{message}{suffix}", file=sys.stderr, flush=True)

    print(f"pinging {args.target}", file=sys.stderr)
    try:
        result = ping(args.target, retries=args.retries, on_step=on_step)
    except ArchiveFSError as exc:
        print(f"  ✗ {exc}", file=sys.stderr)
        print("verdict: SETUP — could not resolve the pin", file=sys.stderr)
        return 2

    print(f"verdict: {verdict_line(result)}", file=sys.stderr)
    if result.ok:
        return 0
    if result.transient and args.retries <= 1:
        print(
            f"  → re-run, or `archivefs ping {args.target} --retries 5` to keep trying "
            f"while the archive recovers.",
            file=sys.stderr,
        )
    return 1 if result.transient else 2


def cmd_config(args: argparse.Namespace) -> int:
    """Show the configuration layers and what they resolve to (issue: config model)."""
    from archivefs import config

    found = config.layers()
    if args.action == "path":
        # The user layer, so `$EDITOR $(archivefs config path)` works.
        user = next(layer for layer in found if layer.name == "user")
        print(user.path)
        return 0

    protocols = sorted(PROTOCOLS)
    resolved = {name: config.settings(name) for name in protocols}
    if args.json:
        print(
            json.dumps(
                {
                    "layers": [
                        {
                            "name": layer.name,
                            "path": str(layer.path),
                            "exists": layer.exists,
                            "travels": layer.travels,
                            "may_set": sorted(layer.allowed),
                        }
                        # highest precedence first, which is how people read it
                        for layer in reversed(found)
                    ],
                    "settings": {k: v for k, v in resolved.items() if v},
                },
                indent=2,
            )
        )
        return 0

    print("layers, highest precedence first:")
    for layer in reversed(found):
        mark = "*" if layer.exists else " "
        note = "content settings, committed" if layer.travels else "machine settings"
        print(f"  {mark} {layer.name:<8} {layer.path}")
        print(f"              {note}")
    print("  (* = present;  above all of these: $ARCHIVEFS_* then -o/--fs-config)")
    active = {name: values for name, values in resolved.items() if values}
    if not active:
        print("\nnothing configured")
        return 0
    print("\nresolved:")
    for name in sorted(active):
        print(f"  {name}://")
        for key in sorted(active[name]):
            print(f"      {key} = {active[name][key]!r}")
    return 0


def _read_input(source: str) -> tuple[bytes, str]:
    if source == "-":
        return sys.stdin.buffer.read(), "the input"
    path = Path(source).expanduser()
    if not path.exists():
        raise SystemExit(f"no such bundle: {path}")
    return path.read_bytes(), str(path)


def cmd_credentials(args: argparse.Namespace) -> int:
    """Inventory, package and install access credentials (issue: reviewer access)."""
    from archivefs import bundle as bundles

    if args.action == "list":
        return _credentials_list(args, bundles)
    if args.action == "export":
        return _credentials_export(args, bundles)
    return _credentials_import(args, bundles)


def _credentials_list(args: argparse.Namespace, bundles) -> int:
    report = []
    for protocol in sorted(PROTOCOLS):
        try:
            held = bundles.holdings(protocol)
        except ArchiveFSError as exc:
            report.append({"protocol": protocol, "error": str(exc)})
            continue
        report.append(
            {
                "protocol": protocol,
                "credentials": [
                    {
                        "name": h.credential,
                        "shareable": h.shareable,
                        "required": h.required,
                        "source": h.source,
                        "scopes": h.scopes,
                        "warning": h.warning,
                    }
                    for h in held
                ],
            }
        )
    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print("credentials archivefs can see. Values are never shown.\n")
    for entry in report:
        name = entry["protocol"] + "://"
        if "error" in entry:
            print(f"  {name:10} ? {entry['error']}")
            continue
        if not entry["credentials"]:
            print(f"  {name:10} public; needs no credential")
            continue
        for index, credential in enumerate(entry["credentials"]):
            shown = name if index == 0 else ""
            kind = "shareable" if credential["shareable"] else "personal "
            if credential["source"]:
                scopes = credential["scopes"]
                where = credential["source"]
                held = (
                    f"{where}"
                    if scopes in ([], ["*"])
                    else f"{where}  [{', '.join(scopes)}]"
                )
            else:
                held = "not found" + ("" if credential["required"] else " (optional)")
            print(f"  {shown:10} {credential['name']:<10} {kind}  {held}")
            if credential["warning"]:
                print(f"             ! {credential['warning']}")
    print(
        "\n  shareable = issued to be handed on, scoped to named data\n"
        "  personal  = authenticates you; a bundle will refuse to carry it"
    )
    return 0


def _credentials_export(args: argparse.Namespace, bundles) -> int:
    """Package this repository's shareable access. The report goes to stderr.

    stderr because ``--out -`` puts a zip on stdout, and a report interleaved with it
    would corrupt the bundle — the pipe into ``age`` has to stay clean.
    """
    from archivefs import config

    root = Path(args.repo).expanduser() if args.repo else config.project_root()
    if root is None:
        raise SystemExit(
            "not inside a repository, so there is nothing to work out which deposits "
            "reviewers need. Run this in the project, or pass --repo, or name the "
            "deposits with --include bia:S-BIAD3080."
        )
    pack, rows, _ = bundles.collect(root, args.include)
    pack.description = args.description or ""

    def report(written: str | None, exit_code: int) -> int:
        if args.json:
            print(
                json.dumps(
                    {
                        "repo": str(root),
                        "deposits": [
                            {
                                "protocol": r.protocol,
                                "scope": r.scope,
                                "status": r.status,
                                "detail": r.detail,
                                "references": r.references,
                            }
                            for r in rows
                        ],
                        "grants": len(pack.grants),
                        "written": written,
                    },
                    indent=2,
                ),
                file=sys.stderr,
            )
            return exit_code
        print(f"{root} depends on {len(rows)} deposit(s):", file=sys.stderr)
        for row in rows:
            where = f"{row.protocol}://{row.scope}" if row.scope else row.protocol
            print(f"  {where:<34} {row.status:<12} {row.detail}", file=sys.stderr)
        if written is None:
            print(
                "\nNothing shareable to export. That is the expected answer when the "
                "data is public, or when access is by personal account — see the "
                "statuses above.",
                file=sys.stderr,
            )
        else:
            print(
                f"\nwrote {written} ({len(pack.grants)} grant(s), mode 0600)\n"
                f"  It holds live keys. Send it the way you would a password, and "
                f"tell the recipient to run:\n"
                f"      archivefs credentials import {Path(written).name}",
                file=sys.stderr,
            )
        return exit_code

    if not pack.grants:
        return report(None, 1)

    if args.out == "-" and sys.stdout.isatty():
        raise SystemExit(
            "refusing to write a bundle to a terminal: it is binary, and it holds "
            "live access keys that would then be in your scrollback and shell "
            "history.\n"
            "  Give --out a path, or pipe it:\n"
            "      archivefs credentials export --out - | age -r <recipient> > pack.age"
        )

    named = "archivefs-access.zip" if args.out == "-" else Path(args.out).name
    payload = bundles.write(
        pack, as_zip=not str(args.out).endswith(".json"), filename=named
    )
    if args.out == "-":
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
        return report("stdout", 0)

    out = Path(args.out).expanduser()
    if not args.force:
        objection = bundles.refuse_in_repo(out)
        if objection:
            raise SystemExit(objection)
    bundles.write_private(out, payload)
    return report(str(out), 0)


def _credentials_import(args: argparse.Namespace, bundles) -> int:
    data, where = _read_input(args.bundle)
    pack = bundles.read(data, where)
    changes = bundles.plan(pack)

    for change in changes:
        mark = {"add": "+", "unchanged": "=", "conflict": "!"}[change.action]
        scope = change.grant.scope or "any deposit"
        print(
            f"  {mark} {change.grant.protocol:<7} {change.grant.credential:<8} "
            f"{scope:<22} {change.path}"
        )
    if pack.description:
        print(f"  ({pack.description})")

    if args.dry_run:
        print("\n--dry-run: nothing written")
        return 0

    written = bundles.install(changes, force=args.force)
    if not written:
        print("\nalready installed; nothing to do")
    else:
        for path in written:
            print(f"\nwrote {path} (mode 0600)")
    if pack.verify:
        print("\nverify with:")
        for url in sorted(set(pack.verify))[:5]:
            print(f"  archivefs ls {url}")
    return 0


def _scratch_note() -> str:
    """Where blocks land when nothing is persisted — worth saying even when it is.

    fsspec gives `tempfile.TemporaryFile()` no directory, so this follows `$TMPDIR`
    and otherwise `/tmp`. On Gadi `/tmp` is a 63 GB tmpfs, i.e. RAM, so "where do
    the bytes actually go" is a question with a surprising default answer.
    """
    import tempfile

    from archivefs import blockcache

    root = blockcache.scratch_root()
    if root is not None:
        return f"scratch (blocks not kept): {root}  [${blockcache.SCRATCH_DIR_ENV}]"
    return (
        f"scratch (blocks not kept): {tempfile.gettempdir()}  "
        f"[$TMPDIR; set ${blockcache.SCRATCH_DIR_ENV} to choose]"
    )


def cmd_cache(args: argparse.Namespace) -> int:
    """Show or clear the block cache that lets a long import resume (issue #15)."""
    import time

    from archivefs import blockcache

    root = blockcache.cache_root(args.dir)
    if args.action == "path":
        # Pure, for scripts: the directory or nothing.
        if root is not None:
            print(root)
        return 0 if root is not None else 1
    if root is None:
        print(
            f"No block cache is configured, so an interrupted import starts again "
            f"from zero.\nTurn it on with, e.g.\n"
            f"  export {blockcache.CACHE_DIR_ENV}="
            f"/scratch/$PROJECT/$USER/archivefs-cache\n"
            f"or per command: -o cache_dir=/some/where"
        )
        print(_scratch_note())
        return 0
    if not root.exists():
        if args.action == "size":
            print(_human(0))
        elif args.action == "clear":
            print(f"nothing to remove; {root} does not exist")
        else:
            print(f"{root} (empty)")
        return 0
    if args.action == "clear":
        gone = blockcache.clear(root)
        print(f"removed {gone} entr{'y' if gone == 1 else 'ies'} from {root}")
        return 0
    found = blockcache.entries(root)
    if args.action == "size":
        print(_human(sum(entry["on_disk"] for entry in found)))
        return 0
    if args.json:
        print(json.dumps(found, indent=2))
        return 0
    print(f"{root}")
    now = time.time()
    for entry in found:
        share = entry["cached"] / entry["size"] if entry["size"] else 0
        age = (now - entry["mtime"]) / 86400
        print(
            f"  {entry['protocol']}://{entry['path']}\n"
            f"      {_human(entry['on_disk'])} on disk, "
            f"{share:.0%} of {_human(entry['size'])} cached, "
            f"{age:.1f} days old"
        )
    total = sum(entry["on_disk"] for entry in found)
    print(f"  {len(found)} entries, {_human(total)} on disk")
    print(_scratch_note())
    return 0


# ---------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="archivefs",
        description="Read-only filesystems for scientific data archives.",
    )
    parser.add_argument(
        "--version", action="version", version=f"archivefs {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "-o",
            "--option",
            action="append",
            metavar="KEY=VALUE",
            help="storage option, repeatable (e.g. -o layout=display)",
        )

    info = sub.add_parser("info", help="what is installed and wired up")
    info.add_argument("--json", action="store_true")
    info.set_defaults(func=cmd_info)

    ls = sub.add_parser("ls", help="list a record's files")
    ls.add_argument("url")
    ls.add_argument("-l", "--long", action="store_true", help="show size and checksum")
    ls.add_argument(
        "-R",
        "--recursive",
        action="store_true",
        help="list every file, not just this level's children",
    )
    ls.add_argument("--json", action="store_true")
    add_common(ls)
    ls.set_defaults(func=cmd_ls)

    stat = sub.add_parser("stat", help="everything known about one path, as JSON")
    stat.add_argument("url")
    add_common(stat)
    stat.set_defaults(func=cmd_stat)

    versions = sub.add_parser(
        "versions",
        help="list a deposit's versions, newest first (cbioportal/zenodo/mendeley)",
    )
    versions.add_argument("url")
    add_common(versions)
    versions.add_argument("--json", action="store_true")
    versions.set_defaults(func=cmd_versions)

    get = sub.add_parser("get", help="download, verifying against published checksums")
    get.add_argument("url")
    get.add_argument("dest")
    add_common(get)
    get.set_defaults(func=cmd_get)

    check = sub.add_parser(
        "check", help="compare a deposit against its .chiro manifest"
    )
    check.add_argument("url")
    add_common(check)
    check.set_defaults(func=cmd_check)

    verify = sub.add_parser(
        "verify", help="verify a downloaded tree against a manifest"
    )
    verify.add_argument("url")
    verify.add_argument("directory")
    verify.add_argument(
        "--source-layout",
        action="store_true",
        help="files are still in the deposit's layout, not the workspace layout",
    )
    add_common(verify)
    verify.set_defaults(func=cmd_verify)

    manifest = sub.add_parser(
        "manifest",
        help="write a .chiro manifest for a deposit (download-free where possible)",
        description=(
            "Build a Chirograph manifest over every file in a deposit. Where the "
            "archive publishes per-file checksums (Zenodo, EGA, derived BIA) this "
            "downloads nothing; for an archive that publishes none (GEO), pass "
            "--download to read and hash the bytes."
        ),
    )
    manifest.add_argument(
        "url",
        nargs="?",
        help="a record or subtree URL, e.g. zenodo://1486184 (omit when --from-dvc)",
    )
    manifest.add_argument(
        "--from-dvc",
        metavar="FILE.dvc",
        help=(
            "build the manifest offline from a DVC import's FILE.dvc — reusing the "
            "md5s, sizes and crc32s it already holds, with no archive access at all"
        ),
    )
    manifest.add_argument(
        "--concept",
        metavar="CURIE",
        help=(
            "the concept CURIE to record (e.g. doi:10.5281/zenodo.123 — Zenodo's "
            "'all versions' DOI), overriding the derived one; for --from-dvc offline, "
            "where only the version DOI is knowable, supply the true concept here"
        ),
    )
    manifest.add_argument(
        "--out",
        metavar="PATH",
        help="where to write (default: <accession>.chiro.json; - for stdout)",
    )
    manifest.add_argument(
        "--download",
        action="store_true",
        help="read and hash the bytes for files the archive publishes no checksum for",
    )
    manifest.add_argument(
        "--self-deposited",
        action="store_true",
        help="mark provenance as self-deposited (default: observed-download)",
    )
    add_common(manifest)
    manifest.set_defaults(func=cmd_manifest)

    update = sub.add_parser(
        "update",
        help="has a .chiro pin's upstream moved? regenerate and, with --write, re-pin",
        description=(
            "Regenerate a committed .chiro manifest from the live archive (download-"
            "free where it publishes checksums) and compare it to the pin. Reports "
            "what changed; --write supersedes the pin by rewriting the manifest. This "
            "is the supersede `dvc update` cannot do on a content-addressed pin."
        ),
    )
    update.add_argument(
        "target", help="a chiro:// URL or a path to the committed .chiro.json"
    )
    update.add_argument(
        "--write",
        action="store_true",
        help="rewrite the manifest to the current upstream (default: report only)",
    )
    update.add_argument(
        "--download",
        action="store_true",
        help="read and hash the bytes for files the archive publishes no checksum for",
    )
    add_common(update)
    update.set_defaults(func=cmd_update)

    confirm = sub.add_parser(
        "confirm",
        help="does the live deposit still match this .chiro manifest? (download-free)",
        description=(
            "Cross-check a manifest against the live deposit it pins. For a ZIP "
            "deposit this reads only the central directory — checking each member's "
            "crc32 and size, and the container's published md5, without downloading "
            "member bytes. For loose files it compares the archive's published md5. "
            "Where a container format offers no cheap check (tar.gz and friends), pass "
            "--download to read and hash the bytes."
        ),
    )
    confirm.add_argument(
        "target", help="a chiro:// URL or a path to the committed .chiro.json"
    )
    confirm.add_argument(
        "--download",
        action="store_true",
        help="read and hash bytes where no cheap check exists (tar.gz, unchecksummed)",
    )
    add_common(confirm)
    confirm.set_defaults(func=cmd_confirm)

    cfg = sub.add_parser(
        "config", help="the configuration layers, and what they resolve to"
    )
    cfg.add_argument("action", nargs="?", default="show", choices=["show", "path"])
    cfg.add_argument("--json", action="store_true")
    cfg.set_defaults(func=cmd_config)

    creds = sub.add_parser(
        "credentials",
        aliases=["creds"],
        help="what access you hold, and how to hand it to a reviewer",
        description=(
            "Package the access a reviewer needs for this project's deposits, and "
            "install one you were sent. Only credentials an archive issues to be "
            "handed on — a per-study sharing key — can be exported; an account "
            "password or API token is refused."
        ),
    )
    creds.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "export", "import"],
    )
    creds.add_argument(
        "bundle",
        nargs="?",
        default="-",
        help="for import: the .zip/.json to install, a sharing link, or - for stdin",
    )
    creds.add_argument(
        "--out",
        metavar="PATH",
        default="-",
        help="for export: where to write the bundle (.zip or .json), or - for stdout",
    )
    creds.add_argument(
        "--include",
        action="append",
        metavar="PROTOCOL:ACCESSION",
        help="export a deposit the repository does not mention, repeatable",
    )
    creds.add_argument(
        "--repo",
        metavar="DIR",
        help="the repository to read dependencies from (default: the enclosing one)",
    )
    creds.add_argument(
        "--description",
        metavar="TEXT",
        help='what this bundle is for, e.g. "reviewer access, Smith et al. 2026"',
    )
    creds.add_argument(
        "--dry-run",
        action="store_true",
        help="for import: say what would change and write nothing",
    )
    creds.add_argument(
        "--force",
        action="store_true",
        help="overwrite a differing credential, or write a bundle inside a git repo",
    )
    creds.add_argument("--json", action="store_true")
    creds.set_defaults(func=cmd_credentials)

    cache = sub.add_parser(
        "cache",
        help="the on-disk block cache that lets an interrupted import resume",
    )
    cache.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "path", "size", "clear"],
    )
    cache.add_argument("--dir", metavar="DIR", help=f"override ${blockcache_env()}")
    cache.add_argument("--json", action="store_true")
    cache.set_defaults(func=cmd_cache)

    dvc = sub.add_parser("dvc", help="register archivefs' protocols with DVC")
    dvc.add_argument("action", choices=["status", "enable", "disable"])
    dvc.add_argument("--json", action="store_true")
    dvc.set_defaults(func=cmd_dvc)

    dvc_meta = sub.add_parser(
        "dvc-meta",
        help="stamp human-readable provenance into a chiro:// pin's .dvc",
        description=(
            "A chiro://md5 dependency is opaque about its source. This writes a "
            "free-form meta.archivefs block into the .dvc — archive, record/DOI, "
            "concept, container, landing URL and the archive source URL — derived from "
            "the pin's own .chirograph manifest. DVC excludes meta from the stage md5 "
            "and preserves it across `dvc update`, so the pin is unchanged; the block "
            "is advisory (the pin + manifest stay authoritative). Idempotent — re-run "
            "to refresh."
        ),
    )
    dvc_meta.add_argument("files", nargs="+", metavar="FILE.dvc")
    dvc_meta.add_argument(
        "--dry-run",
        "-n",
        action="store_true",
        help="print the block instead of writing it",
    )
    dvc_meta.set_defaults(func=cmd_dvc_meta)

    ping = sub.add_parser(
        "ping",
        help="is a pin pullable right now? (reach the archive + fetch one sample byte)",
        description=(
            "Probe whether TARGET can be pulled from its archive now, without "
            "downloading it. Walks the same chain a `dvc pull` uses — resolve the pin, "
            "reach the record's metadata endpoint, fetch a single sample byte — and "
            "reports a verdict that says whose fault a failure is: a transient archive "
            "outage (retry), gone, auth, network, or local setup. TARGET is a "
            "chiro://md5 reference, an archive URL, or a .dvc file."
        ),
    )
    ping.add_argument("target", metavar="TARGET")
    ping.add_argument(
        "--retries",
        type=int,
        default=1,
        metavar="N",
        help="on a transient failure, keep retrying up to N times (default 1)",
    )
    ping.set_defaults(func=cmd_ping)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ArchiveFSError as exc:
        # Our errors are written to be read; a traceback would bury them.
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except (FileNotFoundError, PermissionError, OSError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
