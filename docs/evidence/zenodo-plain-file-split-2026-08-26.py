"""Does splitting a *plain* file's reads actually buy the 30x issue #14 predicts?

#14 was found while measuring #12 and inherits its numbers rather than its own:
one connection sustains ~0.30 MB/s, eight carrying 2 MiB each sustain 9.7-14.0, so
a plain object read over one connection is leaving up to 30x on the table. That is
an inference from the ZIP path. This measures the plain path itself.

Two sections, because a plain object is reached two ways and only one of them is
`cat_file`:

    1. the range primitive   before: fs._fetch_range(info, a, b)   -- the old line
                             after:  fs.fetch_span(info, a, b)
    2. the streaming path    fs.open(...).read(), which is what `archivefs get`,
                             `dvc import-url` and dvc's hashing all use

`before` in section 2 is emulated exactly: the class-default 4 MiB block, and
`fetch_span` routed back to a single request per block.

Every fetch lands on distinct, non-overlapping bytes of a 2.53 GB object, and the
condition order is rotated each trial, for the reasons set out in
`zenodo-piece-size-2026-08-25.md` §5: a repeated range can be served from a warm
origin and reads as a speed-up that is not one.

Run on the persistent session -- Gadi compute nodes have no outbound network.

    python docs/evidence/zenodo-plain-file-split-2026-08-26.py [trials]
"""

import statistics
import sys
import time
import warnings

sys.path.insert(0, "/scratch/a56/jr9959/archivefs/src")
warnings.filterwarnings("ignore")

import fsspec  # noqa: E402

MiB = 1 << 20
RECORD = "17411292"
KEY = "HE_P15-2.ome.tif"
SPAN = 64 * MiB
CHUNK = 4 * MiB  # what `archivefs get` reads with
TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 3

fs = fsspec.filesystem("zenodo", skip_instance_cache=True)
info = fs.info(f"{RECORD}/{KEY}")
path = f"{RECORD}/{KEY}"
print(f"{info['name']}  {info['size'] / 1e9:.2f} GB, {fs.max_concurrent_reads} slots")
print(f"2 sections x {TRIALS} trials x 2 conditions x {SPAN // MiB} MiB "
      f"= {2 * TRIALS * 2 * SPAN / 1e9:.2f} GB\n")

offset = 0


def unsplit_span(_info, start, end):
    """What `cat_file` and `ArchiveFile._fetch_range` did before #14."""
    return fs._fetch_range(_info, start, end)


def primitive(kind: str, start: int) -> bytes:
    if kind == "before":
        return fs._fetch_range(info, start, start + SPAN)
    return fs.fetch_span(info, start, start + SPAN)


def streamed(kind: str, start: int) -> bytes:
    real = fs.fetch_span
    if kind == "before":
        fs.fetch_span = unsplit_span
    try:
        with fs.open(path, "rb", block_size=CHUNK if kind == "before" else None) as fh:
            fh.seek(start)
            out = []
            while sum(map(len, out)) < SPAN:
                out.append(fh.read(min(CHUNK, SPAN - sum(map(len, out)))))
            return b"".join(out)
    finally:
        fs.fetch_span = real


for name, run in (("range primitive", primitive), ("fs.open().read()", streamed)):
    print(f"--- {name}")
    results: dict[str, list[float]] = {"before": [], "after": []}
    for trial in range(TRIALS):
        order = ["before", "after"] if trial % 2 == 0 else ["after", "before"]
        for kind in order:
            began = time.time()
            got = run(kind, offset)
            elapsed = time.time() - began
            assert len(got) == SPAN, f"short read ({len(got)}) -- timing is meaningless"
            offset += SPAN
            rate = SPAN / elapsed / 1e6
            results[kind].append(rate)
            print(f"  trial {trial + 1}  {kind:>6}  {elapsed:7.1f} s  {rate:6.2f} MB/s")
    base = statistics.median(results["before"])
    for kind in ("before", "after"):
        rates = results[kind]
        print(f"  = {kind:>6}  {statistics.median(rates):6.2f} MB/s  "
              f"{statistics.median(rates) / base:5.2f}x  "
              f"[{min(rates):.2f}-{max(rates):.2f}]")
    print()
