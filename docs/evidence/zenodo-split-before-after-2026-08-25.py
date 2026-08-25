"""Before and after on one archive — **superseded, and wrong about why**.

Kept because the way it failed is the finding: reading the same ZIP three times in a
run measures Zenodo's origin warming to that object, not the configuration. In both
rounds, whichever configuration ran *last* won, with the path calibration flat either
side (6.62 and 6.65 MB/s):

    round 1   before 2.99   piece cap 3.42   after 10.92
    round 2   piece cap 5.78   after 5.76   before 8.44

Rotating the order does not fix it — the warming outlives the round.
`zenodo-split-cold-archives-2026-08-25.py` is the measurement to trust: nine sibling
archives, each read once, cold, configurations assigned by a Latin square.

The original description follows.

Before and after, on the path a `dvc import-url` actually takes.

Reads every member of a real Zenodo ZIP through `_member_view`, under three cutting
policies, interleaved in one process so Zenodo's drift cannot flatter one of them:

* **before** — 4 MiB blocks cut into `max_concurrent_reads` pieces, whatever that
  makes them. This is what shipped.
* **piece cap** — the same 4 MiB blocks, cut into 2 MiB pieces. Isolates the change
  that matters for members *larger* than a block, where `MMapCache` consolidates the
  whole member into one range.
* **after** — 16 MiB blocks cut into 2 MiB pieces. Adds the change that matters for
  members *smaller* than a block, where one block is one round of parallel pieces.

Every configuration must return identical bytes for all 27 members; a policy that is
fast and wrong is the failure mode worth testing for, since a rejoined-out-of-order
member is hashed and committed rather than raising.

    python docs/evidence/zenodo-split-before-after-2026-08-25.py [zip_path] [rounds]
"""

import hashlib
import statistics
import sys
import time
import warnings

sys.path.insert(0, "/scratch/a56/jr9959/archivefs/src")
warnings.filterwarnings("ignore")

import fsspec  # noqa: E402
from archivefs.base import MIN_PARALLEL_PIECE  # noqa: E402
from archivefs.backends.zenodo import ZenodoFileSystem  # noqa: E402

MiB = 1 << 20
ZIP = sys.argv[1] if len(sys.argv) > 1 else "22101663/ses_outputs.zip"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 2


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
    ("before      4 MiB blocks, 1/connection ", 4 * MiB, True),
    ("piece cap   4 MiB blocks, whole rounds ", 4 * MiB, False),
    ("after      16 MiB blocks, whole rounds ", 16 * MiB, False),
]

#: A plain 64 MiB read of an unrelated object, at the shipped piece size, run inside
#: every round. Zenodo's throughput to this host moved 4x within one afternoon, so a
#: configuration measured at 2 MB/s and another at 9 MB/s may differ by nothing but
#: the hour. This says what the path was doing while each row was collected — without
#: it, none of the rows below can be compared to a row from another run.
CALIBRATION = ("17411292/HE_P15-2.ome.tif", 64 * MiB, 512 * 1024)


def calibrate(offset: int) -> float:
    fs = fsspec.filesystem("zenodo", skip_instance_cache=True)
    path, span, piece = CALIBRATION
    info = fs.info(path)
    spans = [(o, o + piece) for o in range(offset, offset + span, piece)]
    start = time.time()
    got = fs.fetch_ranges(info, spans)
    elapsed = time.time() - start
    assert sum(len(b) for b in got) == span
    return span / elapsed / 1e6

listing = fsspec.filesystem("zenodo", extract_zips=True, skip_instance_cache=True)
members = listing.find(ZIP)
container = listing._member(*listing.split_path(ZIP))["size"]
print(f"{ZIP}  {container / 1e6:.0f} MB, {len(members)} members, "
      f"{ROUNDS} rounds x {len(CONFIGS)} configs\n")

digests: set[str] = set()
rates: dict[str, list[float]] = {name: [] for name, _, _ in CONFIGS}
pieces_seen: dict[str, int] = {}
calibration: list[float] = []
for round_ in range(ROUNDS):
    reference = calibrate(round_ * 64 * MiB)
    calibration.append(reference)
    print(f"  round {round_ + 1}  calibration: the path is at {reference:5.2f} MB/s")
    order = CONFIGS[round_ % len(CONFIGS):] + CONFIGS[: round_ % len(CONFIGS)]
    for name, block, old in order:
        fs = ZenodoFileSystem(
            extract_zips=True, block_size=block, skip_instance_cache=True
        )
        if old:
            fs.split_span = lambda s, e, _fs=fs: old_split_span(_fs, s, e)
        widest = [0]
        real_split = fs.split_span

        def measured(start, end, _real=real_split, _acc=widest):
            spans = _real(start, end)
            _acc[0] = max(_acc[0], max(e - s for s, e in spans))
            return spans

        fs.split_span = measured
        counter = [0]
        real_fetch = fs._fetch_range

        def counted(info, start, end, _real=real_fetch, _acc=counter):
            data = _real(info, start, end)
            _acc[0] += len(data)
            return data

        fs._fetch_range = counted
        rolling = hashlib.md5()  # noqa: S324 - identity across configs, not security
        start = time.time()
        for path in members:
            rolling.update(hashlib.md5(fs.cat_file(path)).digest())  # noqa: S324
        elapsed = time.time() - start
        digests.add(rolling.hexdigest())
        rates[name].append(container / elapsed / 1e6)
        pieces_seen[name] = widest[0]
        print(f"  round {round_ + 1}  {name}  {elapsed:6.1f} s  "
              f"{container / elapsed / 1e6:5.2f} MB/s  "
              f"{counter[0] / 1e6:6.1f} MB ({counter[0] / container:.2f}x)  "
              f"widest piece {widest[0] / MiB:5.1f} MiB")
        fs._close_zip_views()

baseline = statistics.median(rates[CONFIGS[0][0]])
print(f"\n=== median of {ROUNDS} rounds")
for name, _, _ in CONFIGS:
    median = statistics.median(rates[name])
    print(f"  {name}  {median:5.2f} MB/s  {median / baseline:4.2f}x  "
          f"(widest piece {pieces_seen[name] / MiB:.1f} MiB)")

print(f"  path calibration across rounds: "
      f"{', '.join(f'{c:.2f}' for c in calibration)} MB/s")

print(f"\n  {len(digests)} distinct digest(s) over "
      f"{ROUNDS * len(CONFIGS)} reads of {len(members)} members")
if len(digests) != 1:
    raise SystemExit("member bytes differ by cutting policy — do not ship this")
