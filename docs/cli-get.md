# `archivefs get`

**Download, verifying against whatever checksum the deposit publishes.**

```
archivefs get [-o KEY=VALUE]… URL DEST
```

For ad-hoc work: grab a file, know whether it arrived intact. For anything that
belongs in a project, use `dvc import-url` instead — it records provenance as well as
fetching bytes ([`using-with-dvc.md`](using-with-dvc.md)).

## Arguments and options

| | meaning |
|---|---|
| `URL` | a file **or** a directory inside a record, or a whole record |
| `DEST` | where to put it — see the `cp` rules below |
| `-o`, `--option` | storage option, repeatable — see [the table in `docs/README.md`](README.md#storage-options--o-keyvalue) |

Destination rules follow `cp`:

| source | `DEST` | result |
|---|---|---|
| file | path that is not a directory | written to exactly that path |
| file | **existing** directory | `DEST/<basename>` |
| directory or whole record | anything | created if needed; every file underneath, relative structure preserved |

The word *existing* matters, and a trailing slash does not help:

```console
$ archivefs get "geo://GSE176078/GSE176078_…_raw_counts.txt.gz" ./raw/
raw (5.3 MiB, no checksum published)          # ./raw is now a 5.3 MiB *file*
```

`mkdir` the destination first if you meant a directory.

## What it prints

One line per file, and the last field is the point of the command:

```console
$ archivefs get zenodo://1486184/github-mark.zip ./logo.zip
logo.zip (853 B, verified)

$ mkdir dl && archivefs get zenodo://1486184/github-mark.zip dl
dl/github-mark.zip (853 B, verified)
```

| tail | meaning |
|---|---|
| `verified` | the archive published an MD5 and the bytes match it |
| `no checksum published` | the file arrived; **nothing was checked**. The archive publishes no checksum (GEO, BIA-submitted) and no manifest was in play. |
| `MISMATCH …` on stderr | the bytes do not match the published checksum. The file is left on disk for inspection; exit status is 1. |

`no checksum published` is the case to notice. It is not a warning about the download
— it is the archive telling you it cannot help you detect corruption. Supply a
manifest (`-o chiro=…`) and the same command starts saying `verified`.

## What it does not do

Honest limits, so you do not discover them at 200 GB:

* **No resume, no retry-from-offset, no parallelism.** One stream, 4 MiB at a time.
  A large EGA dataset will take hours (EGA gives ~425 KB/s on one connection).
* **Overwrites without asking**, including when recursing into an existing directory.
* **No cache.** Fetch twice, download twice. `dvc import-url` gets you the DVC cache.
* **Checksums are verified after the fact**, streaming — so a mismatch means bytes are
  already on disk. That is deliberate: you want to inspect a corrupt file, not lose it.

## Scenario: fetch a whole record, keeping its layout

```console
$ archivefs get zenodo://1486184 ./record
record/github-mark.zip (853 B, verified)
$ find record -type f
record/github-mark.zip
```

The record prefix (`1486184/`) is stripped: you get the deposit's *contents*, not a
directory named after the accession. For EGA, the layout of what lands here is
controlled by `-o layout=` (`accession`, `display`, `nested`) — see
[`decisions.md`](decisions.md) D7.

## Scenario: one file out of a big imaging study

```console
$ archivefs ls -R bia://S-BIAD599 | grep 056F63395C
S-BIAD599/20200220-ftp/experimentA/056F63395C/056F63395C_8bit_lynEGFP.tif
S-BIAD599/20200220-ftp/experimentA/056F63395C/056F63395C_8bit_lynEGFP_seg.tif

$ mkdir images
$ archivefs get "bia://S-BIAD599/20200220-ftp/experimentA/056F63395C/056F63395C_8bit_lynEGFP.tif" images
```

Quote the URL: deposited paths legitimately contain `#` and spaces. That file is
199 MiB and BIA publishes no checksum for submitted files, so expect
`no checksum published` — the transfer is unverified unless you bring a manifest.

## Scenario: download and verify in one step

Point at a manifest and `get` starts checking, even on an archive that publishes
nothing of its own. GEO, whose files carry no checksums:

```console
$ mkdir raw && archivefs get "geo://GSE176078/GSE176078_…_raw_counts.txt.gz" ./raw
raw/GSE176078_…_raw_counts.txt.gz (5.3 MiB, no checksum published)

$ mkdir again && archivefs get -o chiro=./GSE176078.chiro.json \
    "geo://GSE176078/GSE176078_…_raw_counts.txt.gz" ./again
chiro drift in GSE176078: ./GSE176078.chiro.json: 2 deposit file(s) not in the manifest
again/GSE176078_…_raw_counts.txt.gz (5.3 MiB, verified)
```

Same file, same archive: `no checksum published` becomes `verified` purely because a
manifest was in play. (The drift warning is honest — that manifest was written from a
one-file download, so the other two files in the record are unrecorded. See
[`cli-check.md`](cli-check.md).)

To check a tree you already have — after reorganising it, or months later — use
[`cli-verify.md`](cli-verify.md) rather than downloading again.

## Exit status

| code | when |
|---|---|
| 0 | every file transferred; each one either verified or unchecked |
| 1 | at least one `MISMATCH`, or bad usage |
| 2 | archive-level failure: not found, credentials missing, manifest unusable |

Exit 0 does **not** mean "verified" — it means "no mismatch was detected", and nothing
can be detected without a published checksum. If you need "verified or fail", check
for a checksum first with [`cli-stat.md`](cli-stat.md), or verify against a manifest.

## See also

[`cli-verify.md`](cli-verify.md) · [`using-with-dvc.md`](using-with-dvc.md) ·
[`credentials.md`](credentials.md) for EGA
