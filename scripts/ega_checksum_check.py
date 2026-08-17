#!/usr/bin/env python3
"""Resolve the blocking EGA checksum question from ``docs/design.md`` OPEN ITEMS.

    Does the MD5 in EGA's metadata equal the MD5 of the bytes on disk after
    download + decryption?

Runs the procedure in the design doc against a real EGA file, including the
"interrupted and resumed" variant, and prints a verdict mapped onto the three
branches the design enumerates.

Deliberately standalone: stdlib + ``requests`` only, no ``archivefs`` import, so
it can be handed to anyone with EGA access (or re-run against a *controlled*
dataset later) without installing this package.

Credentials, in precedence order (same chain as the backend):
  1. ``--credentials-file`` (pyega3-style JSON: {"username": ..., "password": ...})
  2. ``EGA_USERNAME`` / ``EGA_PASSWORD``
  3. ``~/.pyega3/credentials.json`` if present
  4. the public EGA test account, only with ``--use-test-account``

Usage
-----
    # quick: small file, single slice
    python scripts/ega_checksum_check.py --use-test-account \\
        --file-id EGAF00007243773

    # the design doc's procedure: >100 MiB, multi-slice, interrupt + resume
    python scripts/ega_checksum_check.py --use-test-account \\
        --file-id EGAF00004854093 --connections 4 --simulate-interrupt
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

# Defaults lifted from pyega3's shipped server config (public values).
URL_AUTH = "https://ega.ebi.ac.uk:8443/ega-openid-connect-server/token"
URL_API = "https://ega.ebi.ac.uk:8443/v2"
CLIENT_ID = "f20cd2d3-682a-4568-a53e-4262ef54c8f4"
CLIENT_SECRET = (
    "AMenuDLjVdVo4BSwi0QD54LL6NeVDEZRzEQUJ7hJOM3g4imDZBHHX0hNfKHPeQIGkskhtCmqAJtt"
    "_jm7EKq-rWw"
)
# Published in pyega3's default_credential_file.json; read-only synthetic data.
TEST_ACCOUNT = ("ega-test-data@ebi.ac.uk", "egarocks")

SLICE = 100 * 1024 * 1024


def resolve_credentials(args) -> tuple[str, str, str]:
    if args.credentials_file:
        cfg = json.loads(Path(args.credentials_file).expanduser().read_text())
        return cfg["username"], cfg["password"], str(args.credentials_file)
    if os.environ.get("EGA_USERNAME") and os.environ.get("EGA_PASSWORD"):
        return os.environ["EGA_USERNAME"], os.environ["EGA_PASSWORD"], "environment"
    default = Path("~/.pyega3/credentials.json").expanduser()
    if default.exists():
        cfg = json.loads(default.read_text())
        if "username" in cfg and "password" in cfg:
            return cfg["username"], cfg["password"], str(default)
    if args.use_test_account:
        return (*TEST_ACCOUNT, "public EGA test account")
    sys.exit(
        "no EGA credentials found; pass --credentials-file, set EGA_USERNAME/"
        "EGA_PASSWORD, or pass --use-test-account for the public synthetic data"
    )


def get_token(username: str, password: str) -> str:
    r = requests.post(
        URL_AUTH,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={
            "grant_type": "password",
            "client_id": CLIENT_ID,
            "scope": "openid",
            "client_secret": CLIENT_SECRET,
            "username": username,
            "password": password,
        },
        timeout=60,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def metadata(token: str, file_id: str) -> dict:
    r = requests.get(
        f"{URL_API}/metadata/files/{file_id}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
        timeout=60,
    )
    r.raise_for_status()
    return r.json()


def plain_size(token: str, file_id: str) -> int:
    """Ask the *data* endpoint how many plaintext bytes there are.

    A 1-byte range read comes back with ``Content-Range: bytes 0-0/<total>``
    where total is the decrypted length. Download-free, and better than assuming
    ``fileSize - 16``.
    """
    r = requests.get(
        f"{URL_API}/files/{file_id}?destinationFormat=plain",
        headers={"Authorization": f"Bearer {token}", "Range": "bytes=0-0"},
        timeout=60,
    )
    r.raise_for_status()
    return int(r.headers["Content-Range"].partition("/")[2])


def fetch_slice(token: str, file_id: str, start: int, length: int, out: Path) -> Path:
    part = out.with_name(f"{out.name}.from-{start}-len-{length}")
    if part.exists() and part.stat().st_size == length:
        return part
    end = start + length - 1
    with requests.get(
        f"{URL_API}/files/{file_id}?destinationFormat=plain",
        headers={"Authorization": f"Bearer {token}", "Range": f"bytes={start}-{end}"},
        stream=True,
        timeout=1800,
    ) as r:
        r.raise_for_status()
        # NB: with_name, not with_suffix — the slice name already contains dots,
        # and with_suffix would collapse every slice onto one temp file.
        tmp = part.with_name(part.name + ".tmp")
        with tmp.open("wb") as fh:
            for chunk in r.iter_content(1024 * 1024):
                fh.write(chunk)
        got = tmp.stat().st_size
        if got != length:
            tmp.unlink()
            raise RuntimeError(f"slice {start}+{length}: got {got} bytes")
        tmp.rename(part)
    return part


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--file-id", required=True, help="EGAF accession")
    ap.add_argument("--connections", type=int, default=1)
    ap.add_argument("--slice-size", type=int, default=SLICE)
    ap.add_argument("--credentials-file")
    ap.add_argument("--use-test-account", action="store_true")
    ap.add_argument(
        "--simulate-interrupt",
        action="store_true",
        help="drop one completed slice and refetch it, to prove the materialised "
        "md5 is stable across a resumed transfer",
    )
    ap.add_argument("--work-dir", default="./egacheck")
    args = ap.parse_args()

    username, password, source = resolve_credentials(args)
    print(f"credential source: {source} (user {username})")
    token = get_token(username, password)

    meta = metadata(token, args.file_id)
    print("metadata:")
    for key in (
        "fileId",
        "displayFileName",
        "fileName",
        "fileSize",
        "plainChecksum",
        "plainChecksumType",
        "fileStatus",
    ):
        print(f"  {key} = {meta.get(key)!r}")
    # API v2 exposes only the plaintext checksum. API v1 exposed
    # unencryptedChecksum; neither exposes a checksum of the encrypted bytes, so
    # the "md5 == encrypted checksum" branch is not even expressible here.
    encrypted_checksum = meta.get("encryptedChecksum") or meta.get("fileChecksum")
    print(f"  encrypted checksum advertised: {encrypted_checksum!r}")

    total = plain_size(token, args.file_id)
    print(f"plain size from Content-Range: {total}")
    print(f"metadata fileSize - 16       : {meta['fileSize'] - 16}")
    print(f"  agree: {total == meta['fileSize'] - 16}")

    work = Path(args.work_dir) / args.file_id
    work.mkdir(parents=True, exist_ok=True)
    out = work / "materialised.bin"

    starts = list(range(0, total, args.slice_size))
    plan = [(s, min(args.slice_size, total - s)) for s in starts]
    print(f"fetching {len(plan)} slice(s) with {args.connections} connection(s)")

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.connections) as pool:
        parts = list(
            pool.map(lambda p: fetch_slice(token, args.file_id, p[0], p[1], out), plan)
        )

    if args.simulate_interrupt and len(parts) > 1:
        victim = parts[len(parts) // 2]
        print(f"simulating interruption: deleting {victim.name} and refetching")
        start, length = plan[len(parts) // 2]
        victim.unlink()
        fetch_slice(token, args.file_id, start, length, out)

    with out.open("wb") as fh:
        for part in parts:
            fh.write(part.read_bytes())
    elapsed = time.time() - t0

    digest = hashlib.md5(out.read_bytes()).hexdigest()
    size = out.stat().st_size
    print(f"materialised {size} bytes in {elapsed:.1f}s -> md5 {digest}")

    print()
    print("VERDICT")
    if digest == meta["plainChecksum"]:
        print("  md5(materialised) == metadata plainChecksum")
        print("  -> design.md branch 1 (happy path): the metadata checksum IS a")
        print("     checksum of the bytes the user ends up with, so download-free")
        print("     info() can advertise it and --no-download manifests are honest.")
        rc = 0
    elif encrypted_checksum and digest == encrypted_checksum:
        print("  md5(materialised) == ENCRYPTED checksum")
        print("  -> design.md branch 2: bytes are left encrypted; info() needs an")
        print("     explicit decrypt-then-verify step.")
        rc = 2
    else:
        print("  md5(materialised) matches NEITHER advertised checksum")
        print("  -> design.md branch 3: fall back to size + accession + version as")
        print("     the change-detection token for EGA.")
        rc = 3
    print(f"  size(materialised) == fileSize - 16: {size == meta['fileSize'] - 16}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
