"""Before and after, on nine *cold* archives — because re-reading one warms it.

The first version of this measurement read the same 395 MB ZIP under three cutting
policies, rotated across rounds. It produced this, with the path calibration flat
either side (6.62 and 6.65 MB/s), so drift cannot explain it:

    round 1   before 2.99   piece cap 3.42   after 10.92
    round 2   piece cap 5.78   after 5.76   before 8.44

In both rounds, whichever configuration ran **last** won. That is Zenodo's origin
warming to the object as we read it a second and third time, and no amount of rotating
the order fixes it: the warming outlives the round.

So each configuration gets its own archive, read exactly once, never touched before.
The nine `spaceranger_*.zip` of record 17411292 are the sample — one depositor, one
tool, 495-707 MB each, so they are as alike as real archives get. Configurations are
assigned by a Latin square, which puts every configuration at mean position 4.0 of 9
and so cancels any drift over the run as well.

Each archive is read member by member until `LIMIT` bytes of payload have come out;
throughput is over that payload. That understates the big-block configuration slightly
— it overshoots the stop point by up to one block — which is the safe direction.

    python docs/evidence/zenodo-split-cold-archives-2026-08-25.py [limit_mb]
"""

import statistics
import sys
import time
import warnings

sys.path.insert(0, "/scratch/a56/jr9959/archivefs/src")
warnings.filterwarnings("ignore")

import fsspec  # noqa: E402
from archivefs.backends.zenodo import ZenodoFileSystem  # noqa: E402
from archivefs.base import MIN_PARALLEL_PIECE  # noqa: E402

MiB = 1 << 20
RECORD = "17411292"
ARCHIVES = [
    "spaceranger_P2-1.zip", "spaceranger_P10-2.zip", "spaceranger_P4-2.zip",
    "spaceranger_P10-1.zip", "spaceranger_P20-2.zip", "spaceranger_P11-1.zip",
    "spaceranger_P15-2.zip", "spaceranger_P4-1.zip", "spaceranger_P2-2.zip",
]
LIMIT = (int(sys.argv[1]) if len(sys.argv) > 1 else 120) * MiB


def old_split_span(fs, start, end):
    """`split_span` as it shipped: one piece per connection, any size."""
    length = end - start
    pieces = fs.max_concurrent_reads
    if pieces < 2 or length < 2 * MIN_PARALLEL_PIECE:
        return [(start, end)]
    pieces = min(pieces, length // MIN_PARALLEL_PIECE)
    step = -(-length // pieces)
    return [(o, min(o + step, end)) for o in range(start, end, step)]


CONFIGS = [
    ("before     4 MiB blocks, 1/connection", 4 * MiB, True),
    ("piece cap  4 MiB blocks, whole rounds", 4 * MiB, False),
    ("after     16 MiB blocks, whole rounds", 16 * MiB, False),
]
# Latin square: every configuration lands once in each third of the run.
SQUARE = [0, 1, 2, 1, 2, 0, 2, 0, 1]


def calibrate(offset: int) -> float:
    """A plain 64 MiB read at the shipped piece size, to say what the path is doing."""
    fs = fsspec.filesystem("zenodo", skip_instance_cache=True)
    info = fs.info(f"{RECORD}/spaceranger_P4_P13.zip")  # not one of the nine
    piece = 512 * 1024
    spans = [(o, o + piece) for o in range(offset, offset + 64 * MiB, piece)]
    start = time.time()
    got = fs.fetch_ranges(info, spans)
    elapsed = time.time() - start
    assert sum(len(b) for b in got) == 64 * MiB
    return 64 * MiB / elapsed / 1e6


print(f"record {RECORD}, {len(ARCHIVES)} sibling archives, "
      f"{LIMIT // MiB} MiB read from each\n")
print(f"  calibration before: {calibrate(0):5.2f} MB/s")

rates: dict[str, list[float]] = {name: [] for name, _, _ in CONFIGS}
for position, (key, which) in enumerate(zip(ARCHIVES, SQUARE)):
    name, block, old = CONFIGS[which]
    path = f"{RECORD}/{key}"
    fs = ZenodoFileSystem(
        extract_zips=True, block_size=block, skip_instance_cache=True
    )
    if old:
        fs.split_span = lambda s, e, _fs=fs: old_split_span(_fs, s, e)
    counter = [0]
    real_fetch = fs._fetch_range

    def counted(info, start, end, _real=real_fetch, _acc=counter):
        data = _real(info, start, end)
        _acc[0] += len(data)
        return data

    fs._fetch_range = counted
    members = fs.find(path)
    counter[0] = 0  # the listing is not what is being measured
    payload = 0
    start = time.time()
    for member in members:
        payload += len(fs.cat_file(member))
        if payload >= LIMIT:
            break
    elapsed = time.time() - start
    rate = payload / elapsed / 1e6
    rates[name].append(rate)
    print(f"  {position + 1}/{len(ARCHIVES)}  {key:22} {name}  {elapsed:6.1f} s  "
          f"{rate:5.2f} MB/s  ({counter[0] / 1e6:.0f} MB fetched for "
          f"{payload / 1e6:.0f} MB read)")
    fs._close_zip_views()

print(f"  calibration after:  {calibrate(64 * MiB):5.2f} MB/s")

baseline = statistics.median(rates[CONFIGS[0][0]])
print(f"\n=== median of {len(ARCHIVES) // len(CONFIGS)} cold archives each")
for name, _, _ in CONFIGS:
    values = sorted(rates[name])
    median = statistics.median(values)
    print(f"  {name}  {median:5.2f} MB/s  {median / baseline:4.2f}x  "
          f"[{values[0]:.2f}-{values[-1]:.2f}]")
