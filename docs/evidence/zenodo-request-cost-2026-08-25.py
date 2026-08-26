"""How long does *one* Zenodo range request take, as a function of its size?

`zenodo-piece-size-2026-08-25.py` found that aggregate throughput at a fixed 8
connections peaks around 1-2 MiB per request and then collapses — 8 MiB pieces are
4x *worse* than 512 KiB ones. That is not the smooth "amortise a fixed overhead"
curve issue #12 assumed, so this measures the shape directly, one request at a time,
no concurrency: the connection, not the client ceiling.

Reported as marginal MB/s between adjacent sizes rather than as a fitted line,
because a line is exactly the assumption under test.

Distinct offsets throughout: a repeated range could be served from a cache and would
read as a speed-up that is not one.

    python docs/evidence/zenodo-request-cost-2026-08-25.py [samples]
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
SIZES = [
    256 * 1024, 512 * 1024, 1 * MiB, 3 * MiB // 2, 2 * MiB,
    5 * MiB // 2, 3 * MiB, 4 * MiB, 6 * MiB, 8 * MiB,
]
SAMPLES = int(sys.argv[1]) if len(sys.argv) > 1 else 3

fs = fsspec.filesystem("zenodo", skip_instance_cache=True)
info = fs.info(f"{RECORD}/{KEY}")
print(f"{info['name']}  {info['size'] / 1e9:.2f} GB, one connection at a time")
print(f"{SAMPLES} samples x {len(SIZES)} sizes = "
      f"{SAMPLES * sum(SIZES) / 1e6:.0f} MB\n")

offset = 0
times: dict[int, list[float]] = {s: [] for s in SIZES}
for sample in range(SAMPLES):
    # Rotated, so a drift in Zenodo's throughput over the run does not land
    # systematically on whichever size always goes first.
    for size in SIZES[sample % len(SIZES):] + SIZES[: sample % len(SIZES)]:
        start = time.time()
        got = fs._fetch_range(info, offset, offset + size)
        elapsed = time.time() - start
        assert len(got) == size, f"short read: {len(got)} of {size}"
        offset += size
        times[size].append(elapsed)
        print(f"  sample {sample + 1}  {size / MiB:>5.2f} MiB  {elapsed:7.2f} s  "
              f"{size / elapsed / 1e6:5.2f} MB/s")

print(f"\n=== median of {SAMPLES}, one request at a time")
print(f"  {'size':>9}  {'time':>7}  {'MB/s':>6}  {'marginal MB/s':>13}")
previous = None
for size in SIZES:
    med = statistics.median(times[size])
    marginal = ""
    if previous:
        d_bytes, d_time = size - previous[0], med - previous[1]
        marginal = f"{d_bytes / d_time / 1e6:13.2f}" if d_time > 0 else "            -"
    print(f"  {size / MiB:>5.2f} MiB  {med:6.2f} s  {size / med / 1e6:6.2f}{marginal}")
    previous = (size, med)
