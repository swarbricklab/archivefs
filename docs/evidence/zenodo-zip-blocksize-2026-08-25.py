"""Does the piece-size finding survive on the path that actually reads a ZIP?

The micro-benchmarks measure `fetch_ranges` in isolation. This one reads *every
member* of a real Zenodo ZIP through `_member_view` — the mmap-cached path a
`dvc import-url` uses — at several block sizes, and reports wall clock, bytes moved
and a digest of every member.

The digest is the point of the test, not a bonus: block size changes how a member is
cut up and rejoined, and a member that is confidently wrong is worse than a slow one.
All configurations must agree, member for member.

Rounds are rotated, and that is not decoration: Zenodo's absolute throughput to this
host drifted 4x between two trials of the concurrency sweep half an hour apart. A
block size measured once, first, would be measuring the hour.

    python docs/evidence/zenodo-zip-blocksize-2026-08-25.py [zip_path] [rounds]
"""

import hashlib
import statistics
import sys
import time
import warnings

sys.path.insert(0, "/scratch/a56/jr9959/archivefs/src")
warnings.filterwarnings("ignore")

import fsspec  # noqa: E402
from archivefs.backends.zenodo import ZenodoFileSystem  # noqa: E402

MiB = 1 << 20
ZIP = sys.argv[1] if len(sys.argv) > 1 else "22101663/ses_outputs.zip"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 2
BLOCKS = [4 * MiB, 8 * MiB, 16 * MiB]

listing = fsspec.filesystem("zenodo", extract_zips=True, skip_instance_cache=True)
members = listing.find(ZIP)
container = listing._member(*listing.split_path(ZIP))["size"]
print(f"{ZIP}  {container / 1e6:.0f} MB, {len(members)} members\n")

digests: set[str] = set()
rates: dict[int, list[float]] = {b: [] for b in BLOCKS}
for round_ in range(ROUNDS):
    for block in BLOCKS[round_ % len(BLOCKS):] + BLOCKS[: round_ % len(BLOCKS)]:
        fs = ZenodoFileSystem(
            extract_zips=True, block_size=block, skip_instance_cache=True
        )
        real_fetch = fs._fetch_range
        # A one-element list, not a plain int: the wrapper runs on the pool threads
        # and rebinding a closed-over name there would only shadow it.
        counter = [0]

        def counted(info, start, end, _real=real_fetch, _acc=counter):
            data = _real(info, start, end)
            _acc[0] += len(data)
            return data

        fs._fetch_range = counted
        # A digest of digests: identity of every member's bytes, in listing order,
        # comparable across configurations without holding 400 MB in memory.
        rolling = hashlib.md5()  # noqa: S324 - identity, not security
        start = time.time()
        for path in members:
            rolling.update(hashlib.md5(fs.cat_file(path)).digest())  # noqa: S324
        elapsed = time.time() - start
        served = counter[0]
        digests.add(rolling.hexdigest())
        rates[block].append(container / elapsed / 1e6)
        # Deliberately *not* labelled with a piece size. The point this measurement
        # made is that on an archive of large members there isn't one: `MMapCache`
        # consolidates the whole member into a single range, so the pieces are
        # member/8 whatever the block size is — which is why all three rows tie.
        print(f"  round {round_ + 1}  {block // MiB:>3} MiB blocks  {elapsed:7.1f} s  "
              f"{container / elapsed / 1e6:5.2f} MB/s  {served / 1e6:7.1f} MB moved "
              f"({served / container:.2f}x)  {rolling.hexdigest()[:12]}")
        fs._close_zip_views()

print(f"\n=== median of {ROUNDS} rounds")
for block in BLOCKS:
    median = statistics.median(rates[block])
    print(f"  {block // MiB:>3} MiB blocks  {median:5.2f} MB/s  "
          f"{median / statistics.median(rates[BLOCKS[0]]):4.2f}x")

print(f"\n  {len(digests)} distinct digest(s) across "
      f"{ROUNDS * len(BLOCKS)} reads of {len(members)} members")
if len(digests) != 1:
    raise SystemExit("member bytes differ by block size — do not ship this")
