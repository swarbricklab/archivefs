"""``archivefs`` — a small CLI, mostly for finding out *why* something failed.

The library is the product; this exists because three questions come up constantly
and none of them should require writing Python:

* "is this thing even installed properly, and does DVC know about it?"
  → ``archivefs info`` / ``archivefs dvc status``
* "what is in this record, and what does it claim about itself?"
  → ``archivefs ls`` / ``archivefs stat``
* "does what I downloaded match what the deposit says it should be?"
  → ``archivefs check`` / ``archivefs verify``

Deliberately stdlib-only: a diagnostic tool that needs its own dependency tree is
a diagnostic tool that cannot run when you need it.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from archivefs import __version__
from archivefs.errors import ArchiveFSError
from archivefs.registry import PROTOCOLS, installed_backends


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
    storage_options: dict[str, Any] = {}
    for item in options or []:
        key, _, value = item.partition("=")
        if not value:
            raise SystemExit(f"--option expects key=value, got {item!r}")
        if value in ("true", "false"):
            storage_options[key] = value == "true"
        else:
            storage_options[key] = value
    fs = fsspec.filesystem(protocol, **storage_options)
    return fs, rest


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
        print(json.dumps(entries, indent=2, default=str))
        return 0
    for entry in sorted(entries, key=lambda e: e["name"]):
        if args.long:
            digest = entry.get("md5") or "-"
            print(f"{_human(entry.get('size')):>12}  {digest:32}  {entry['name']}")
        else:
            print(entry["name"])
    return 0


def cmd_stat(args: argparse.Namespace) -> int:
    fs, path = _filesystem(args.url, args.option)
    print(json.dumps(fs.info(path), indent=2, default=str))
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

    cfg = sub.add_parser(
        "config", help="the configuration layers, and what they resolve to"
    )
    cfg.add_argument("action", nargs="?", default="show", choices=["show", "path"])
    cfg.add_argument("--json", action="store_true")
    cfg.set_defaults(func=cmd_config)

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
    cache.add_argument(
        "--dir", metavar="DIR", help=f"override ${blockcache_env()}"
    )
    cache.add_argument("--json", action="store_true")
    cache.set_defaults(func=cmd_cache)

    dvc = sub.add_parser("dvc", help="register archivefs' protocols with DVC")
    dvc.add_argument("action", choices=["status", "enable", "disable"])
    dvc.add_argument("--json", action="store_true")
    dvc.set_defaults(func=cmd_dvc)

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
