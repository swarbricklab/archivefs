# `archivefs cache`

**The on-disk block cache that lets an interrupted import resume instead of starting
again.**

```
archivefs cache [list|path|size|clear] [--dir DIR] [--json]
```

**Nothing archivefs does ever writes to `$HOME`.** Credentials are *read* from
`~/.config/archivefs/` and `~/.pyega3/` (a few hundred bytes of JSON, read-only);
every byte of payload goes either where you asked for it or to one of the two
directories below, both configurable, neither defaulting to your home directory.

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

**Under DVC it is already on — no `export` needed.** A `dvc import-url` caches to a
directory beside the repository's own DVC cache (`.dvc/cache/archivefs`), so an
interrupted import resumes with no configuration at all. That is safe to default
precisely because the DVC cache is a filesystem you already chose for large data
(`decisions.md` D29, follow-up). The cost, paid by default: one copy of the imported
bytes on that filesystem, which ages out on the 14-day eviction — `archivefs cache
clear` removes it sooner.

**Everywhere else it stays off until you name a directory.** A plain
`fsspec.filesystem(...)` or `archivefs get` has no cache to sit beside and no way to
know your quota, so it caches nothing unless told to:

```console
$ export ARCHIVEFS_CACHE_DIR=/scratch/$PROJECT/$USER/archivefs-cache
$ archivefs get 'zenodo://17411292/spaceranger_P15.zip' data/p15
```

Set `cache_dir` once in `~/.config/archivefs/config.json` (`{"cache_dir": "…"}`) to
apply it to every command, DVC or not, without an environment variable. To send DVC's
imports somewhere other than beside its cache, name it explicitly:

```console
$ dvc import-url --fs-config cache_dir=/g/data/$PROJECT/blocks 'bia://S-BIAD3080/…' out
```

Somewhere with room, and **not** `$HOME`: these entries are as large as the archive.
On NCI that means `/scratch` or `/g/data`, not the 10 GB home quota.

Per command instead of per shell:

```console
$ archivefs get -o cache_dir=/scratch/tmp/blocks 'zenodo://17411292/big.zip' big.zip
$ dvc import-url --fs-config cache_dir=/scratch/tmp/blocks 'zenodo://…' out
```

`cache_dir` also turns it **off** for one command — `-o cache_dir=false` — whether it
was on from the environment or from the DVC default. That is the direction that has to
work without unsetting a variable the rest of your shell depends on.

## Where the bytes go when you *don't* turn it on

The cache being off does not mean nothing is written — a container's blocks still need
somewhere to live for the length of the read. fsspec creates that file with
`tempfile.TemporaryFile()` and passes **no directory**, so it follows `$TMPDIR`, and
`/tmp` when `$TMPDIR` is unset.

**On Gadi `/tmp` is a 63 GB tmpfs — that is RAM.** An unset `$TMPDIR` therefore turns
a 20 GB import into 20 GB of memory pressure on a shared node, which is a worse
outcome than any disk. `ARCHIVEFS_SCRATCH_DIR` names that directory instead of
inheriting it:

```console
$ export ARCHIVEFS_SCRATCH_DIR=/scratch/$PROJECT/$USER/tmp
```

or `-o scratch_dir=…` / `--fs-config scratch_dir=…` per command, and
`scratch_dir=false` to fall back to `tempfile`'s own choice. The file is still
unlinked at creation and still dies with the process — this only decides *which
filesystem* absorbs it.

`archivefs cache` reports the resolved value either way:

```console
$ archivefs cache
No block cache is configured, so an interrupted import starts again from zero.
Turn it on with, e.g.
  export ARCHIVEFS_CACHE_DIR=/scratch/$PROJECT/$USER/archivefs-cache
or per command: -o cache_dir=/some/where
scratch (blocks not kept): /scratch/a56/jr9959/tmp  [$TMPDIR; set $ARCHIVEFS_SCRATCH_DIR to choose]
```

## Not your home directory

Neither `cache_dir` nor `scratch_dir` defaults to `$HOME`, and pointing either there
logs a warning — one line, once:

```
cache_dir=/home/913/jr9959/.cache/archivefs is inside your home directory, and one
entry is as large as the archive it caches. Home quotas are usually far smaller than
that (10 GB on Gadi) — prefer /scratch or /g/data.
```

It is a warning and not a refusal: `~/.cache` is the conventional place for a cache
and a laptop is welcome to it. On a cluster the failure mode is a blown quota three
hours into an import, with the error pointing at whatever happened to write last.

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
| `path` | print the directory and nothing else — for scripts. Exits 1 when no cache is configured, so `dir=$(archivefs cache path) \|\| …` works |
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
