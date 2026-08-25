"""Where does the piece-size effect actually come from — piece size, or sockets?

Two measurements disagree. One connection at a time is a flat ~0.30 MB/s at every
request size from 256 KiB to 8 MiB (`zenodo-request-cost-2026-08-25.py`), so there is
no fixed per-request overhead to amortise. Yet 8 concurrent connections fetching
2 MiB pieces reach 9.7 MB/s — 1.27 MB/s *per connection*, 4x what a lone connection
sustains (`zenodo-piece-size-2026-08-25.py`).

Per-connection throughput cannot depend on piece size unless the connections are not
the same connections. So count them: how many TCP sockets does each condition
actually open, and which host serves the bytes?

This matters beyond tuning. D26 bounds concurrency at 8 deliberately — 8 is a
politeness budget for a free public service. If the fast conditions are fast because
they churn through dozens of sockets, the bound is not bounding what it claims to.

    python docs/evidence/zenodo-connection-churn-2026-08-25.py
"""

import sys
import time
import warnings
from collections import Counter

sys.path.insert(0, "/scratch/a56/jr9959/archivefs/src")
warnings.filterwarnings("ignore")

import fsspec  # noqa: E402
import urllib3  # noqa: E402

MiB = 1 << 20
RECORD = "17411292"
KEY = "HE_P15-2.ome.tif"
SPAN = 64 * MiB
PIECES = [512 * 1024, 2 * MiB, 8 * MiB]

opened: Counter = Counter()
requested: Counter = Counter()

pool_cls = urllib3.connectionpool.HTTPSConnectionPool
real_new_conn = pool_cls._new_conn
real_urlopen = pool_cls.urlopen


def counting_new_conn(self):
    opened[self.host] += 1
    return real_new_conn(self)


def counting_urlopen(self, method, url, *args, **kwargs):
    requested[self.host] += 1
    return real_urlopen(self, method, url, *args, **kwargs)


pool_cls._new_conn = counting_new_conn
pool_cls.urlopen = counting_urlopen

fs = fsspec.filesystem("zenodo", skip_instance_cache=True)
info = fs.info(f"{RECORD}/{KEY}")
print(f"{info['name']}  {fs.max_concurrent_reads} slots, {SPAN // MiB} MiB per fetch")
print(f"{'pieces':>16}  {'time':>7}  {'MB/s':>6}  {'sockets':>7}  {'requests':>8}  hosts")

offset = 0
for piece in PIECES:
    opened.clear()
    requested.clear()
    spans = [(o, o + piece) for o in range(offset, offset + SPAN, piece)]
    start = time.time()
    got = fs.fetch_ranges(info, spans)
    elapsed = time.time() - start
    assert sum(len(b) for b in got) == SPAN
    offset += SPAN
    hosts = ", ".join(f"{h}={n}" for h, n in opened.most_common())
    print(f"{len(spans):>4} x {piece // 1024:>6} KiB  {elapsed:6.1f} s  "
          f"{SPAN / elapsed / 1e6:6.2f}  {sum(opened.values()):>7}  "
          f"{sum(requested.values()):>8}  {hosts}")

# The sequential measurement pinned every request to *one* socket, held for the
# whole run, and saw a flat 0.30 MB/s. The concurrent one spreads over eight and
# sees up to 1.27 MB/s on each. Two candidate causes, and they call for opposite
# code: concurrency itself (keep 8 slots busy, piece size is the lever) or socket
# age (a long-lived connection is shaped, and the lever is not piece size at all).
#
# So: eight requests of the same size, one at a time, on a socket that is reused —
# then eight more on a fresh socket each. If reuse is the problem, the second row
# is fast and the piece-size result needs re-reading.
PIECE = 2 * MiB
print(f"\none request at a time, {PIECE // MiB} MiB each, 8 in a row")
for reuse in (True, False):
    opened.clear()
    start = time.time()
    for _ in range(8):
        if not reuse:
            fs.session.close()
            fs.session = fs._make_session()
        got = fs._fetch_range(info, offset, offset + PIECE)
        assert len(got) == PIECE
        offset += PIECE
    elapsed = time.time() - start
    label = "one socket reused" if reuse else "fresh socket each"
    print(f"  {label}  {elapsed:6.1f} s  {8 * PIECE / elapsed / 1e6:5.2f} MB/s  "
          f"{sum(opened.values())} sockets")
