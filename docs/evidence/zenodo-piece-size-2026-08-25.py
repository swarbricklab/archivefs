"""Does a *larger* parallel piece move more bytes per second on Zenodo?

Issue #12's precondition. Same bytes, same concurrency (8 connections, the
`max_concurrent_reads` bound), different piece size:

    64 MiB span   as  128 x 512 KiB | 64 x 1 MiB | 32 x 2 MiB | 16 x 4 MiB | 8 x 8 MiB

Every condition therefore keeps all 8 slots busy; only the number of round trips
differs. If aggregate MB/s is flat, Zenodo's ceiling is per *client* and issue #12
should be closed without code.

Run on the persistent session — Gadi compute nodes have no outbound network.

    python docs/evidence/zenodo-piece-size-2026-08-25.py [trials]
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
KEY = "HE_P15-2.ome.tif"  # 2.5 GB, so every fetch below lands on fresh bytes
SPAN = 64 * MiB
PIECES = [512 * 1024, 1 * MiB, 2 * MiB, 4 * MiB, 8 * MiB]
TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 3


def label(piece: int) -> str:
    n = SPAN // piece
    return f"{n:>3} x {piece // 1024:>5} KiB"


fs = fsspec.filesystem("zenodo", skip_instance_cache=True)
info = fs.info(f"{RECORD}/{KEY}")
print(f"{info['name']}  {info['size'] / 1e9:.2f} GB, {fs.max_concurrent_reads} slots")
print(f"{TRIALS} trials x {len(PIECES)} conditions x {SPAN // MiB} MiB "
      f"= {TRIALS * len(PIECES) * SPAN / 1e9:.2f} GB\n")

# Distinct, non-overlapping offsets for every single fetch: a repeat of the same
# range could be served from a cache and would read as a speed-up that is not one.
offset = 0
results: dict[int, list[float]] = {p: [] for p in PIECES}
for trial in range(TRIALS):
    # Rotated each trial, so a drift in Zenodo's own throughput over the run does
    # not land systematically on whichever condition always went first.
    for piece in PIECES[trial % len(PIECES):] + PIECES[: trial % len(PIECES)]:
        spans = [(o, o + piece) for o in range(offset, offset + SPAN, piece)]
        start = time.time()
        got = fs.fetch_ranges(info, spans)
        elapsed = time.time() - start
        assert sum(len(b) for b in got) == SPAN, "short read — timing is meaningless"
        offset += SPAN
        rate = SPAN / elapsed / 1e6
        results[piece].append(rate)
        print(f"  trial {trial + 1}  {label(piece)}  {elapsed:7.1f} s  {rate:5.2f} MB/s")

print(f"\n=== median of {TRIALS} trials, {SPAN // MiB} MiB per fetch, 8 connections")
base = statistics.median(results[PIECES[0]])
for piece in PIECES:
    rates = sorted(results[piece])
    med = statistics.median(rates)
    print(f"  {label(piece)}   {med:5.2f} MB/s   {med / base:4.2f}x   "
          f"[{rates[0]:.2f}-{rates[-1]:.2f}]")
