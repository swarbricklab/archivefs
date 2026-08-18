# `archivefs verify`

**Do the files on my disk match what the deposit's manifest says?**

```
archivefs verify [--source-layout] [-o KEY=VALUE]… URL DIRECTORY
```

Local, offline except for fetching the manifest itself. Nothing is re-downloaded —
this is the command for a tree you already have, whether it arrived five minutes or
five years ago.

## Arguments and options

| | meaning |
|---|---|
| `URL` | the record whose manifest to verify against |
| `DIRECTORY` | the local tree to check |
| `--source-layout` | files still sit as they do **in the deposit** (matched by `source_path`) rather than as they belong in a workspace (matched by `relpath`) |
| `-o chiro=…` | name the manifest explicitly, if it is not discoverable in the deposit |

Requires `chirograph`; the verification logic is chirograph's, not a second
implementation.

## Reading the output

```console
$ archivefs verify -o chiro=./1486184.chiro.json zenodo://1486184 ./record
body hash: ok · checked 1 file(s) · 0 failure(s)
```

Two independent claims on that line:

* **`body hash`** — the *manifest's* own integrity: its recorded `body_hash` versus
  one recomputed from its entries. `ok` means the manifest has not been edited or
  truncated. It says nothing about your files.
* **`checked N · M failure(s)`** — your files against the manifest's entries.

So `body hash: ok · 1 failure(s)` is not a contradiction — it means "a trustworthy
manifest, and a file that does not match it":

```console
$ archivefs verify -o chiro=./1486184.chiro.json zenodo://1486184 ./corrupt
body hash: ok · checked 1 file(s) · 1 failure(s)
  size-mismatch    github-mark.zip

$ archivefs verify -o chiro=./1486184.chiro.json zenodo://1486184 ./empty
body hash: ok · checked 1 file(s) · 1 failure(s)
  missing          github-mark.zip
```

| status | meaning |
|---|---|
| `ok` | size and checksum both match (not printed; only failures are listed) |
| `missing` | the manifest records it; it is not in the directory |
| `size-mismatch` | wrong size — cheap to detect, so checked first |
| `hash-mismatch` | right size, wrong bytes. Silent corruption, or a substituted file. |
| `unreadable` | present but could not be read (permissions, I/O error) |

## `--source-layout`: which of two path shapes to expect

A manifest carries both paths for every file: where it sits **in the deposit**
(`source_path`) and where it belongs **in a workspace** (`relpath`). Use:

* **no flag** (default) — the tree has been laid out for analysis, matched by
  `relpath`. This is the normal case.
* **`--source-layout`** — the tree is exactly what came out of the archive, nothing
  renamed or reorganised. Use it immediately after a download.

Getting this backwards reports every file as `missing`. If that is what you see, and
the files are plainly there, try the other one.

## Scenario: verify a download before starting work

```console
$ archivefs get zenodo://1486184 ./raw
raw/github-mark.zip (853 B, verified)
$ archivefs verify -o chiro=./1486184.chiro.json --source-layout zenodo://1486184 ./raw
body hash: ok · checked 1 file(s) · 0 failure(s)
```

`get` already verifies per file as it streams, so this is belt-and-braces there. It
earns its keep on archives that publish no checksums, where `get` can only say
*"no checksum published"* — the manifest is the only thing that can say `verified`.

## Scenario: the data has been sitting on scratch for a year

```console
$ archivefs verify -o chiro=./GSE176078.chiro.json geo://GSE176078 ./raw
chiro drift in GSE176078: ./GSE176078.chiro.json: 2 deposit file(s) not in the manifest
body hash: ok · checked 1 file(s) · 0 failure(s)
```

Here the tree is intact and the warning is about the *deposit* holding files the
manifest never recorded — a different question, and one [`cli-check.md`](cli-check.md)
answers properly. Had the local copy rotted, you would see `hash-mismatch` instead:
bit rot, a partial copy, an interrupted `rsync`, a well-meaning colleague's `gzip -d`.
None of those change what `ls -l` tells you.

## Scenario: no manifest to verify against

```
ManifestError: GSE176078 has no discoverable .chiro.json, so there is nothing to
verify against. Point at one with the `chiro` storage option, or verify against the
archive's own checksums instead.
```

Options: write a manifest for the deposit (Chirograph's `capture_from_directory` does
this from a downloaded tree), or fall back to per-file checks against the archive's
published checksums with [`cli-get.md`](cli-get.md) / [`cli-stat.md`](cli-stat.md).

## Exit status

| code | when |
|---|---|
| 0 | manifest body hash ok **and** no file failures |
| 1 | any failure, a body-hash mismatch, or bad usage |
| 2 | no manifest, unusable manifest, or `chirograph` missing |

Suitable for a pre-analysis gate:

```console
$ archivefs verify -o chiro=./m.chiro.json zenodo://1486184 ./raw || exit 1
```

## See also

[`cli-check.md`](cli-check.md) (the upstream half of the same question) ·
[`cli-get.md`](cli-get.md) · [`credentials.md`](credentials.md)
