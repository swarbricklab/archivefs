# What actually limits a ranged read from Zenodo

Measured 2026-08-25 from the NCI persistent session, against issue #12's precondition:
*before* changing the block size, prove that piece size affects throughput at all.

It does — but not for the reason the issue gives, not in the direction it predicts at
the top end, and the number it proposes (32 MiB blocks, 4 MiB pieces) is **measurably
worse than what we ship today**.

Every probe below is in this directory and re-runnable, and each uses distinct,
non-overlapping byte offsets throughout: a repeated range can be served from a cache
and reads as a speed-up that is not one. §5 explains why that precaution is not
enough on its own, and what it cost to find out.

## 1. Piece size, at the concurrency we actually use

[`zenodo-piece-size-2026-08-25.py`](zenodo-piece-size-2026-08-25.py) — 64 MiB per
fetch, always 8 connections, so only the number of round trips differs. Three trials,
condition order rotated each trial, 1.01 GB moved.

| pieces | median | vs today | across trials |
|---|---|---|---|
| 128 × 512 KiB *(what we ship)* | 5.76 MB/s | 1.00x | 5.72–6.00 |
| 64 × 1 MiB | 8.93 MB/s | 1.55x | 8.84–9.33 |
| **32 × 2 MiB** | **9.70 MB/s** | **1.68x** | 9.65–10.10 |
| 16 × 4 MiB *(issue #12's proposal)* | 5.05 MB/s | 0.88x | 4.94–5.08 |
| 8 × 8 MiB | 2.49 MB/s | 0.43x | 2.49–2.52 |

The spread within each condition is under 5%, which is worth pausing on: D25 and D26
both had to hedge because Zenodo's wall clock moved 2x run to run. Rotating the order
and holding the byte count fixed makes the comparison tight enough to act on.

So there **is** an effect, it is worth 1.7x, and the optimum is at 2 MiB — four times
*smaller* than the 4 MiB pieces issue #12 proposes. Scaling the block to 32 MiB as
proposed would have made a 20 GB import **12% slower**, not 8x faster.

## 2. The stated reason for the effect is wrong

Issue #12's premise, inherited from D26: "Zenodo's per-request overhead is *seconds* (a
whole 4 MiB block took 11.3 s as one request)". If that were so, bigger pieces would
amortise it and would keep winning.

[`zenodo-request-cost-2026-08-25.py`](zenodo-request-cost-2026-08-25.py) — one request
at a time, no concurrency, ten sizes, three samples, order rotated:

| size | time | MB/s | marginal MB/s |
|---|---|---|---|
| 0.25 MiB | 0.96 s | 0.27 | |
| 0.50 MiB | 1.80 s | 0.29 | 0.31 |
| 1.00 MiB | 3.48 s | 0.30 | 0.31 |
| 2.00 MiB | 6.88 s | 0.30 | 0.32 |
| 4.00 MiB | 13.18 s | 0.32 | 0.28 |
| 8.00 MiB | 26.78 s | 0.31 | 0.27 |

A flat line. One connection moves ~0.30 MB/s whatever you ask it for, and the fixed
cost per request is about **0.1 s**, not seconds. D26's 11.3 s for a 4 MiB block was
never overhead — it was 4.2 MB at 0.37 MB/s, the *rate*, read as though it were a
per-request price. That misreading is what put a 512 KiB floor in `split_span` and what
issue #12 then reasoned from.

## 3. It is not socket churn, and not socket age

Per-connection throughput should not depend on how many *other* connections are open,
and here it does: 0.30 MB/s alone, 1.27 MB/s each when eight are busy. Two boring
explanations, both eliminated by
[`zenodo-connection-churn-2026-08-25.py`](zenodo-connection-churn-2026-08-25.py):

* **More sockets than we think.** No: 7 sockets are opened for the first condition and
  then *reused* — later conditions open zero. All to `zenodo.org`, no redirect to
  another host. The D26 politeness bound is bounding what it claims to.
* **A long-lived connection gets shaped.** No: eight sequential 2 MiB requests take
  46.4 s on one reused socket and 46.9 s on a fresh socket each. Identical.

So the effect is real, and neither of the cheap explanations survives.

## 4. Both levers collapse into one: bytes in flight

[`zenodo-concurrency-vs-piece-2026-08-25.py`](zenodo-concurrency-vs-piece-2026-08-25.py)
sweeps connections against piece size, 32 MiB per cell (medians of 2 trials, MB/s):

| connections | 512 KiB pieces | 2 MiB pieces |
|---|---|---|
| 1 | 0.79 | 0.95 |
| 2 | 1.68 | 5.65 |
| 4 | 4.09 | 9.12 |
| **8** | 6.61 | **14.03** |
| 16 | 7.78 | 4.08 |

2 MiB beats 512 KiB at every connection count from 2 to 8 — and then 16 connections at
2 MiB *collapses*, to worse than 16 connections at 512 KiB.

Sorting every cell measured, across both probes, by **connections × piece size** —
the bytes in flight at any instant — explains that collapse and the one in probe 1:

| in flight | cells | MB/s |
|---|---|---|
| 4 MiB | 8×512K, 2×2M | 5.76, 5.65 |
| 8 MiB | 8×1M, 4×2M, 16×512K | 8.93, 9.12, 7.78 |
| **16 MiB** | **8×2M** | **9.70, 14.03** |
| 32 MiB | 8×4M, 16×2M | 5.05, 4.08 |
| 64 MiB | 8×8M | 2.49 |

Throughput rises to a peak at ~16 MiB in flight and falls off a cliff past it,
*however* you reach it — more connections or bigger pieces, it does not matter. Below
the peak the two levers are not interchangeable (1 connection × 2 MiB is 0.95 MB/s,
4 × 512 KiB is 4.09 MB/s: you need several connections), but above it either one
ruins the transfer.

That shape — a window with a cliff, on a ~280 ms path — is what a congestion-limited
link looks like. We did not confirm the mechanism, and the decision does not need it:
what it needs is the peak, and the peak is **8 connections × 2 MiB = 16 MiB**.

## 5. End to end, on nine cold archives

Micro-benchmarks measure `fetch_ranges`. What a `dvc import-url` does is read members
through the mmap-cached view, so
[`zenodo-split-cold-archives-2026-08-25.py`](zenodo-split-cold-archives-2026-08-25.py)
reads the nine `spaceranger_*.zip` of record 17411292 — one depositor, one tool,
495–707 MB each — one configuration per archive, Latin square, calibration either side:

| configuration | median | range |
|---|---|---|
| before: 4 MiB blocks, one piece per connection | 3.87 MB/s | 2.67–13.33 |
| piece cap only: 4 MiB blocks, whole rounds | 8.69 MB/s | 8.54–10.36 |
| **after: 16 MiB blocks, whole rounds** | **18.00 MB/s** | 9.26–19.27 |

Calibration was 5.89 MB/s before the run and 6.32 after, so the path did not move.

**Read the multiplier as "several-fold", not as 4.65.** Individual archives vary about
2x whatever the configuration, three archives per configuration is a small sample, and
one comparison crosses over (a lucky `before` archive at 13.33 beat an unlucky `after`
at 9.26). Eight of the nine go the other way, and the direction agrees with the
micro-benchmarks, which is why this is worth acting on.

The spread is itself a result. `before`'s pieces are `range / 8`, and the range depends
on how `zipfile` happens to read a member — a *stored* member is read whole, so the
range is the member and the pieces are enormous; a *deflated* one is read
incrementally, so the range is block-sized and the pieces are 512 KiB. That is why
`before` runs anywhere from 2.67 to 13.33 MB/s on nine archives from one depositor,
and why capping the piece size tightens the range to 8.54–10.36. Predictability is
worth something on its own.

### What the first attempt at this measured instead

The first version read the *same* archive under all three configurations, rotating the
order. In both rounds the configuration that ran **last** won — 2.99 → 3.42 → 10.92,
then 5.78 → 5.76 → 8.44 — with the calibration flat at 6.62 and 6.65 MB/s either side.
That is Zenodo's origin warming to the object across re-reads, and rotation cannot
cancel it because the warming outlives the round.
[`zenodo-split-before-after-2026-08-25.py`](zenodo-split-before-after-2026-08-25.py) is
kept for that lesson: on a warm-caching origin, a repeated-measures design over one
object measures the origin.

## 6. What this means for the change

`max_concurrent_reads` is already 8 and already the measured optimum — D26 got that
right, for a partly wrong reason. Two things change (`decisions.md` D27):

    piece  = clamp(16 MiB / max_concurrent_reads, 512 KiB, 2 MiB)
    pieces = ceil(length / (max_concurrent_reads × piece)) × max_concurrent_reads
    block  = clamp(max_concurrent_reads × piece, blocksize, container / 8)

The piece size is now bounded *above*, which is what fixes a member read whose range is
the whole member. The count is rounded to whole rounds, which is not cosmetic: without
it, a 23 MB member becomes 12 pieces — eight connections then four — and that idle half
round cost more than the smaller pieces won (1.74 MB/s against 2.08). And the block
follows the connection count, bounded by an eighth of the container so that a small
archive keeps its cheap single-member read.

Together those reproduce the best measured cell at every connection count in the
sweep: 4 MiB blocks at 1–2 connections, 8 MiB at 4, 16 MiB at 8, and 16 MiB of 1 MiB
pieces at 16 — the one setting that avoids the 32 MiB collapse a naive
`max_concurrent_reads × 2 MiB` would walk straight into.

## 7. Also found, and out of scope here

Reading an ordinary (non-ZIP) file uses **one connection for the whole file**:
`ArchiveFile._fetch_range` and `cat_file` call `_fetch_range` directly and never touch
`split_span`. At the 0.30 MB/s a single connection sustains, that is the 30x gap
between probe 1's top and bottom rows, applied to every plain download from every
backend. The machinery to fix it (`fetch_ranges` + `split_span`) already exists and is
tested. Filed as [issue #14](https://github.com/swarbricklab/archivefs/issues/14)
rather than folded in here.
