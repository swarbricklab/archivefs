# What splitting a plain file's reads is actually worth

Measured 2026-08-26 from the NCI persistent session, for issue #14.

Issue #14 inherited its numbers from #12's work rather than measuring its own: one
connection sustains ~0.30 MB/s, eight carrying 2 MiB each sustain 9.7–14.0, therefore
a plain object read over one connection is leaving **up to 30x** on the table.

It is worth **6.5x**, reliably, on an object Zenodo has not served recently — and
close to **nothing** on one it has. Both halves matter, and the second one is why the
first attempt at this measurement got the wrong answer.

## 1. The first attempt measured a warm object, and said splitting was worthless

[`zenodo-plain-file-split-2026-08-26.py`](zenodo-plain-file-split-2026-08-26.py) —
64 MiB per fetch, three trials, condition order rotated, distinct non-overlapping
offsets throughout, all on `HE_P15-2.ome.tif` (2.53 GB).
[Full log.](zenodo-plain-file-split-2026-08-26.log)

| | before (one request) | after (`fetch_span`) |
|---|---|---|
| range primitive | 10.02 MB/s | 9.27 MB/s (**0.93x**) |
| `fs.open().read()` | 7.90 MB/s | 8.34 MB/s (**1.06x**) |

No effect — and worse than no effect, a *single request* moving 10 MB/s, which is 30x
what §2 of [`zenodo-piece-size-2026-08-25.md`](zenodo-piece-size-2026-08-25.md) says
one connection can do.

The trial-by-trial numbers say what happened. In chronological order the range
primitive ran: **0.37**, 6.52, 20.21, 10.11, 10.02, 9.27 MB/s. The very first fetch
took **179.9 s** for 64 MiB; nothing after it took more than 10.3 s.

That object had been read repeatedly that day, by the #12 replication and by this
probe's own first cell. Zenodo's origin warms to an object, the warming outlives the
run, and rotating conditions cannot cancel something that persists across all of them
— the same trap `zenodo-split-before-after-2026-08-25.py` was kept as a lesson for,
one level up: there it was a re-read of the same *range*, here of the same *object*.

## 2. On cold objects, interleaved, it is 6.5x

[`zenodo-plain-cold-objects-2026-08-26.py`](zenodo-plain-cold-objects-2026-08-26.py) —
the five other objects in record 17411292 large enough to take 128 MiB of reads, none
of them touched before. [Full log.](zenodo-plain-cold-objects-2026-08-26.log)

Interleaved rather than blocked, because with an origin that warms *as you read it*
that is the only design in which both conditions see the same temperature:

    object i:   [before][after][before][after][before][after][before][after]
    object i+1: [after][before][after][before][after][before][after][before]

16 MiB per chunk — one full round of 8 × 2 MiB — four chunks per condition per object,
and the leading condition alternates so "goes first on a cold object" does not land
systematically on one of them.

| object | before | after |
|---|---|---|
| HE_P11-1.ome.tif | 0.42 MB/s | 2.21 MB/s |
| HE_P10-2.ome.tif | 0.33 | 2.30 |
| HE_P2-2.ome.tif | 0.36 | 2.32 |
| HE_P4-2.ome.tif | 0.34 | 2.36 |
| HE_P25-1.ome.tif | 0.37 | 2.35 |
| **median** | **0.36 MB/s** | **2.32 MB/s — 6.48x** |

Five objects, five agreements, and the tightest spread anything in this directory has
produced: 0.33–0.42 against 2.21–2.36. After a fortnight of measurements where Zenodo's
throughput moved 4x within an afternoon, that is worth saying plainly.

## 3. What the two halves mean together

**"One connection sustains ~0.30 MB/s" is a property of a cold object, not of a
connection.** Warm, one connection does 8–10 MB/s. The earlier probes all ran against
a warm object and reported 9.7–14.0 MB/s for eight connections, so they were measuring
the *warm* ceiling — which splitting does not raise, because it was never the
bottleneck there.

**Splitting does not make a cold object fast either.** Eight connections get 2.32 MB/s
out of one, not 8 × 0.36 = 2.9, and nowhere near the 9.7–14.0 a warm object gives. The
cold object itself is the limit; parallelism extracts about 6.5x more from it.

**The case that matters is the cold one.** A `dvc import-url` is by definition the
first time anyone here has asked for that object. Re-reads are already fast and stay
fast. So the change is worth ~6.5x on every import and ~1x on everything else, which
is the right way round.

**It is not the 30x the issue claimed**, and that number should not be repeated. It
came from comparing a cold single connection against a warm eight.

## 4. Cost, memory, and what was not measured

`fetch_span` fetches a window of `max_concurrent_reads × piece` at a time — 16 MiB,
the peak D27 measured — so a whole-object `cat_file` holds one round in flight rather
than the whole file cut into 2 MiB pieces. The result is still assembled whole,
because that is `cat_file`'s contract; a 2.5 GB object still wants `fs.open()`.

**Not measured, and worth being explicit about:** EGA and BIA inherit this and neither
was tested — no live EGA credentials in this session, and BIA's S3 path has its own
throttling story. `max_concurrent_reads` was already 8 for all four backends before
this change; what is new is that plain reads now use it. Setting it to 1 restores the
previous behaviour exactly, including a single request per range.

Nor was the block-size change measured on its own. `_open` now takes its default block
from `parallel_block_size(size)` — one round of pieces, bounded at an eighth of the
object — rather than fsspec's 4 MiB class default, on D27's reasoning rather than on a
fresh probe. The two changes were measured together, as `fs.open().read()` in §1 and
as the range primitive in §2.
