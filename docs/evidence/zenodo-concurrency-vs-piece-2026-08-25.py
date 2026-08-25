"""Two levers, swept together: how many connections, and how big a piece?

The other three probes leave one thing unexplained. A single connection is capped
near 0.30 MB/s at *every* request size, and reconnecting does not lift it — but eight
connections fetching 2 MiB pieces reach 1.27 MB/s **each**. Per-connection throughput
should not depend on how many other connections are open, and it does.

So sweep the two dimensions against each other rather than one at a time. Two things
fall out that a one-dimensional sweep cannot give:

* whether D26's "the gain flattens at 8" still holds at a piece size it was not
  measured at (it was measured with 512 KiB pieces)
* whether piece size matters at all once the connection count is right

Span is fixed, so every cell moves the same bytes.

    python docs/evidence/zenodo-concurrency-vs-piece-2026-08-25.py [trials]
"""

import statistics
import sys
import threading
import time
import warnings

sys.path.insert(0, "/scratch/a56/jr9959/archivefs/src")
warnings.filterwarnings("ignore")

import fsspec  # noqa: E402

MiB = 1 << 20
RECORD = "17411292"
KEY = "HE_P15-2.ome.tif"
SPAN = 32 * MiB
CONNS = [1, 2, 4, 8, 16]
PIECES = [512 * 1024, 2 * MiB]
TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 1

fs = fsspec.filesystem("zenodo", skip_instance_cache=True)
info = fs.info(f"{RECORD}/{KEY}")
cells = [(c, p) for c in CONNS for p in PIECES]
print(f"{info['name']}  {SPAN // MiB} MiB per cell, "
      f"{TRIALS} x {len(cells)} cells = {TRIALS * len(cells) * SPAN / 1e9:.2f} GB\n")

offset = 0
rates: dict[tuple[int, int], list[float]] = {c: [] for c in cells}
for trial in range(TRIALS):
    for conns, piece in cells:
        # Both, not just the attribute: the semaphore is what actually bounds the
        # reads, and setting only `max_concurrent_reads` would measure a bound of 8
        # while reporting whatever was asked for.
        fs.max_concurrent_reads = conns
        fs._read_slots = threading.BoundedSemaphore(conns)
        spans = [(o, o + piece) for o in range(offset, offset + SPAN, piece)]
        start = time.time()
        got = fs.fetch_ranges(info, spans)
        elapsed = time.time() - start
        assert sum(len(b) for b in got) == SPAN, "short read — timing is meaningless"
        offset += SPAN
        rate = SPAN / elapsed / 1e6
        rates[(conns, piece)].append(rate)
        print(f"  trial {trial + 1}  {conns:>2} conns x {piece // 1024:>4} KiB  "
              f"{elapsed:7.1f} s  {rate:6.2f} MB/s  ({rate / conns:.2f} per conn)")

print(f"\n=== median of {TRIALS}, {SPAN // MiB} MiB per cell")
print(f"  {'conns':>5}" + "".join(f"{p // 1024:>9} KiB" for p in PIECES))
for conns in CONNS:
    row = "".join(
        f"{statistics.median(rates[(conns, p)]):9.2f}    " for p in PIECES
    )
    print(f"  {conns:>5}{row}")
print("  (MB/s aggregate)")
