# `archivefs cache`

**The on-disk block cache that lets an interrupted import resume instead of starting
again.**

```
archivefs cache [list|path|size|clear] [--dir DIR] [--json]
```

## Why this exists

Reading a ZIP member fetches the container in blocks and caches them, so an import
reads each byte once (`decisions.md` D25). That cache is a sparse temporary file
which is **unlinked at creation**, so it dies with the process.

A real import of a 20.09 GB Zenodo record ran for 2 hours 50 minutes, had 16.1 GB
cached, did not finish — and the retry started from zero. At the ~1.6 MB/s such a
record actually reads at, that is a 3.5 hour window in which a cancelled job, a
walltime kill or an unrelated DVC exception costs the whole transfer.

Point `ARCHIVEFS_CACHE_DIR` somewhere and those blocks survive the process.

## Turning it on

**Off by default, and it stays off until you name a directory.** A cache of 20 GB
archives that nobody asked for is a way to fill a shared filesystem, and there is no
default size cap that is both useful — bigger than the archive you are importing —
and safe on someone else's quota.

```console
$ export ARCHIVEFS_CACHE_DIR=/scratch/$PROJECT/$USER/archivefs-cache
$ dvc import-url 'zenodo://17411292/spaceranger_P15.zip' data/p15
```

Somewhere with room, and **not** `$HOME`: these entries are as large as the archive.
On NCI that means `/scratch` or `/g/data`, not the 10 GB home quota.

Per command instead of per shell:

```console
$ archivefs get -o cache_dir=/scratch/tmp/blocks 'zenodo://17411292/big.zip' big.zip
$ dvc import-url --fs-config cache_dir=/scratch/tmp/blocks 'zenodo://…' out
```

`cache_dir` also turns it **off** for one command when the environment has it on —
`-o cache_dir=false` — which is the direction that has to work without unsetting a
variable the rest of your shell depends on.

## What resuming looks like

```console
$ dvc import-url 'zenodo://17411292/spaceranger_P15.zip' data/p15
^C                                                    # three hours in

$ dvc import-url 'zenodo://17411292/spaceranger_P15.zip' data/p15
INFO: resuming 17411292/spaceranger_P15.zip: 921 of 1260 blocks already cached
```

The resume line is at `INFO`, so `dvc -v` shows it.

## The actions

| action | what it does |
|---|---|
| `list` (default) | one line per entry: what it is, how much is on disk, how much of the object is cached, how old it is |
| `path` | print the directory and nothing else — for scripts |
| `size` | total bytes actually occupied |
| `clear` | remove every entry not currently in use |

```console
$ archivefs cache
/scratch/a56/jr9959/archivefs-cache
  zenodo://17411292/spaceranger_P15.zip
      15.0 GiB on disk, 75% of 20.1 GiB cached, 0.1 days old
  1 entries, 15.0 GiB on disk
```

`on disk` is what the sparse file really occupies (`st_blocks`), which is the number
your quota cares about; `of … cached` is how much of the object is present.

## Eviction

Entries untouched for **14 days** are removed the next time the cache is opened.
`ARCHIVEFS_CACHE_MAX_AGE_DAYS` changes that. An entry a running import holds a lock
on is never pruned, and neither `clear` nor the age sweep will touch it.

Nothing evicts by size. If you want a bound, put the cache on a filesystem that has
one, or run `archivefs cache clear` when you are done with a large import.

## What it refuses to do

The failure this is built around is not "the cache missed". It is **a block recorded
as present but never written**, which serves zeros as data — silently, and then DVC
hashes and commits them. So:

* the block index is written **after** the bytes, never before, and replaced
  atomically. Crashing between the two loses blocks, which costs time; the other
  order would cost correctness.
* the index names the exact object it belongs to — protocol, path, size, published
  md5 and block size. **Any** mismatch discards the entry rather than guessing, as
  does a truncated data file or an index that will not parse.
* an object with **no published checksum gets no persistent cache at all**. Without
  one, "same path, same size" is not evidence of the same bytes. Zenodo publishes an
  md5 for every file; GEO publishes none (`decisions.md` D8), so GEO keeps the
  process-lifetime cache it always had.
* one process holds an exclusive `flock` per entry. A second import of the same
  container falls back to a private cache rather than interleaving writes — no
  waiting, and no worse than the behaviour before this existed.

Full reasoning: [`decisions.md`](decisions.md) D28.

## Exit codes

| code | meaning |
|---|---|
| 0 | fine, including "no cache configured" and "cache is empty" |
| 2 | the cache directory could not be read or written |
