"""Is a plain file's single-connection read slow on a *cold* object?

The first attempt at this (`zenodo-plain-file-split-2026-08-26.py`) measured one
2.53 GB object that had already been read several times that day, and found a single
request moving 10 MB/s — which is not what §2 of `zenodo-piece-size-2026-08-25.md`
says a single connection can do. The one genuinely cold cell in that run *did*: the
very first fetch took 179.9 s for 64 MiB (0.37 MB/s), and every fetch after it was
6-20 MB/s. So the object warms, and warm is where the earlier run lived.

This measures the case a user actually meets — an object nobody has asked Zenodo for
recently — and it interleaves rather than blocks the conditions, because with an
origin that warms *as you read it* that is the only design in which both conditions
see the same temperature:

    object i:   [before][after][before][after][before][after][before][after]
    object i+1: [after][before][after][before][after][before][after][before]

16 MiB per chunk, four chunks per condition per object, so each condition gets the
same bytes at interleaved offsets; the leading condition alternates so that "goes
first on a cold object" does not land on one of them.

Run on the persistent session -- Gadi compute nodes have no outbound network.

    python docs/evidence/zenodo-plain-cold-objects-2026-08-26.py
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
# Everything in the record big enough for 128 MiB of reads, minus HE_P15-2, which
# the first attempt warmed thoroughly.
OBJECTS = [
    "HE_P11-1.ome.tif",
    "HE_P10-2.ome.tif",
    "HE_P2-2.ome.tif",
    "HE_P4-2.ome.tif",
    "HE_P25-1.ome.tif",
]
CHUNK = 16 * MiB  # one full round of 8 x 2 MiB, the measured peak
PAIRS = 4

fs = fsspec.filesystem("zenodo", skip_instance_cache=True)
print(f"{len(OBJECTS)} cold objects x {2 * PAIRS * CHUNK // MiB} MiB "
      f"= {len(OBJECTS) * 2 * PAIRS * CHUNK / 1e9:.2f} GB, "
      f"{fs.max_concurrent_reads} slots\n")

rates: dict[str, list[float]] = {"before": [], "after": []}
for index, key in enumerate(OBJECTS):
    info = fs.info(f"{RECORD}/{key}")
    order = ["before", "after"] if index % 2 == 0 else ["after", "before"]
    seconds = {"before": 0.0, "after": 0.0}
    for pair in range(PAIRS):
        for slot, kind in enumerate(order):
            start = (pair * 2 + slot) * CHUNK
            began = time.time()
            if kind == "before":
                # The line `cat_file` and `ArchiveFile._fetch_range` used before #14.
                got = fs._fetch_range(info, start, start + CHUNK)
            else:
                got = fs.fetch_span(info, start, start + CHUNK)
            seconds[kind] += time.time() - began
            assert len(got) == CHUNK, f"short read ({len(got)})"
    line = [f"{key:<20}  first={order[0]:<6}"]
    for kind in ("before", "after"):
        rate = PAIRS * CHUNK / seconds[kind] / 1e6
        rates[kind].append(rate)
        line.append(f"{kind} {seconds[kind]:7.1f} s {rate:6.2f} MB/s")
    print("  ".join(line), flush=True)

print()
base = statistics.median(rates["before"])
for kind in ("before", "after"):
    values = rates[kind]
    print(f"= {kind:>6}  {statistics.median(values):6.2f} MB/s  "
          f"{statistics.median(values) / base:5.2f}x  "
          f"[{min(values):.2f}-{max(values):.2f}]")
