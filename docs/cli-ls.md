# `archivefs ls`

**What is in this record, before you download it.**

```
archivefs ls [-l] [-R] [--json] [-o KEY=VALUE]… URL
```

## Arguments and options

| | meaning |
|---|---|
| `URL` | an archive URL: `zenodo://1486184`, `bia://S-BIAD599`, `geo://GSE176078`, `ega://EGAD00001006673`, optionally with a path inside it |
| `-l`, `--long` | show size and MD5 as well as the path |
| `-R`, `--recursive` | every file underneath, not just this level's children |
| `--json` | full metadata per entry, as a JSON array |
| `-o`, `--option` | a storage option, repeatable — see [the table in `docs/README.md`](README.md#storage-options--o-keyvalue) |

## Basic use

```console
$ archivefs ls zenodo://1486184
1486184/github-mark.zip

$ archivefs ls -l zenodo://1486184
       853 B  49f6a914ea33912471512ac9228c6923  1486184/github-mark.zip
```

Three things to know about the output:

* **Paths have no `bia://` / `zenodo://` prefix.** They are filesystem paths, not
  URLs. If you pipe one into another `archivefs` command, put the protocol back —
  `archivefs stat "bia://$(…)"` — or you get *"does not look like an archive URL"*.
* **`-` in the checksum column means the archive publishes none.** It is not a
  failure. GEO and BIA-submitted files look like this; Zenodo, EGA and the BIA
  derived tier do not. A `.chiro` manifest fills the column in.
* **Text output is sorted by name; `--json` is not.** JSON preserves the archive's
  own order, which for some records is meaningful (`file_position` in BioStudies).

## `ls` is children-only; `-R` costs requests

Without `-R` you get exactly one level, directories included — the fsspec contract,
and what `walk()`/`glob()` need to work. With `-R`, archivefs walks the whole record
*and calls `info()` on every file it finds*. On archives that resolve metadata
lazily, that is one request per file:

```console
$ archivefs ls -R bia://S-BIAD599 | wc -l
584
```

For a big imaging study, prefer descending a level at a time. The BIA derived view in
particular is one S3 object per Zarr chunk — over 60,000 for `S-BIAD599` — so `-R`
there is a mistake, and the view is deliberately lazy to make one-level listing cheap.

`-l` has the same shape of cost: a listing leaves an unknown size unknown, and asking
for `-l` asks archivefs to resolve each one. Cheap on Zenodo (the record metadata has
everything), a request per file on GEO.

## Scenario: choose files by their metadata, not their names

`--json` carries everything the archive said, which is how you pick files without
guessing from filenames:

```console
$ archivefs ls --json zenodo://1486184 | head -20
[
  {
    "name": "1486184/github-mark.zip",
    "type": "file",
    "size": 853,
    "checksum": "49f6a914ea33912471512ac9228c6923",
    "md5": "49f6a914ea33912471512ac9228c6923",
    "checksum_type": "MD5",
    "archive_version": "10.5281/zenodo.1486184",
    "url": "https://zenodo.org/api/records/1486184/files/github-mark.zip/content",
    "zenodo": {
      "record_id": "1486184",
      "doi": "10.5281/zenodo.1486184",
      "concept_doi": "10.5281/zenodo.1486183",
      …
```

For BIA the per-entry `bia.attributes` block holds the depositor's own annotation
(organism, channel, fluorescent label), which is usually the only sane way to select
images. See [`cli-stat.md`](cli-stat.md).

## Scenario: the same record, addressed four ways

```console
$ archivefs ls zenodo://1486184                      # record id
1486184/github-mark.zip

$ archivefs ls zenodo://10.5281/zenodo.1486184       # version DOI
10.5281/zenodo.1486184/github-mark.zip
```

A *concept* DOI resolves to the latest version; a version DOI or record id pins one.
The path prefix echoes what you asked for, so a `.dvc` file records how you addressed
it — a pinned version stays pinned.

## Scenario: look inside a ZIP without downloading it

```console
$ archivefs ls -R -o extract_zips=true zenodo://1486184
1486184/github-mark.zip/github-mark.svg
1486184/github-mark.zip/md5sum
```

Opt-in, Zenodo only, and it reads the ZIP's central directory over ranged requests
rather than fetching the archive. Members are checked for zip-slip, symlinks and
absurd compression ratios at *listing* time ([`decisions.md`](decisions.md) D9).

## Scenario: list with a manifest instead of the archive's listing

```console
$ archivefs ls -l -o chiro=/path/to/1486184.chiro.json zenodo://1486184
       853 B  49f6a914ea33912471512ac9228c6923  1486184/github-mark.zip
```

When a manifest is in play, the manifest *is* the listing — which is how GEO and
BIA-submitted files get checksums they do not publish. A manifest that promises files
the deposit no longer has still lists them, with a warning on stderr:

```console
$ archivefs ls -l -o chiro=/path/to/drifted.chiro.json zenodo://1486184
chiro drift in 1486184: …/drifted.chiro.json: 1 promised file(s) absent upstream
       853 B  49f6a914ea33912471512ac9228c6923  1486184/github-mark.zip
        15 B  4468b6848c08defad3dfb278fa445cc8  1486184/notes.txt
```

`notes.txt` is a ghost: listed because the manifest promises it, unreadable because
the archive does not have it. That is deliberate — dropping it would hide the drift.
[`cli-check.md`](cli-check.md) reports it properly.

## Exit status

| code | when |
|---|---|
| 0 | listed |
| 1 | bad usage — malformed URL, unknown protocol, `-o` without `=` |
| 2 | archive-level failure: record not found, credentials missing, manifest unusable |

```console
$ archivefs ls sra://foo
unknown protocol 'sra'. archivefs speaks: bia, dbgap, ega, geo, zenodo

$ archivefs ls zenodo://
'zenodo://' does not look like an archive URL. Expected e.g. zenodo://1486184 or ega://EGAD00001006673/EGAF00004908765.
```

## See also

[`cli-stat.md`](cli-stat.md) for one file in full · [`cli-get.md`](cli-get.md) to
download · `dvc list-url` does the same job from DVC
