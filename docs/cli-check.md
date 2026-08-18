# `archivefs check`

**Has the deposit drifted from what its manifest promises?**

```
archivefs check [-o KEY=VALUE]… URL
```

Compares the *upstream* record against its `.chiro.json` manifest. Nothing is
downloaded and nothing local is examined — for that, see
[`cli-verify.md`](cli-verify.md).

The two commands answer different questions:

| | question | looks at |
|---|---|---|
| `check` | does the archive still hold what the manifest says it holds? | the archive |
| `verify` | do the files on my disk match the manifest? | your disk |

## Arguments and options

| | meaning |
|---|---|
| `URL` | a record. A path inside one works too; only the record part is used. |
| `-o chiro=…` | name the manifest explicitly: a path inside the deposit, a local file, or a URL |
| `-o`, `--option` | other storage options, repeatable — see [the table in `docs/README.md`](README.md#storage-options--o-keyvalue) |

Requires the `chirograph` package (`archivefs info` tells you). Without it, manifests
are ignored — and if you named one with `-o chiro=`, the command refuses rather than
silently checking nothing.

## No manifest is not an error

```console
$ archivefs check zenodo://1486184
1486184: no .chiro.json manifest found (native listing in use)
```

Exit 0. Most public deposits have no manifest; there is simply nothing to compare
against. Auto-discovery looks for a single `*.chiro.json` among the record's files and
**refuses to guess** if there are several — name one with `-o chiro=` in that case.

## A clean deposit

```console
$ archivefs check -o chiro=./1486184.chiro.json zenodo://1486184
…/1486184.chiro.json: deposit matches the manifest
```

Every file the manifest promises is present upstream, every file upstream is recorded,
and sizes and checksums agree.

## Drift, and what each kind means

```console
$ archivefs check -o chiro=./drifted.chiro.json zenodo://1486184
chiro drift in 1486184: …/drifted.chiro.json: 1 promised file(s) absent upstream
…/drifted.chiro.json: 1 promised file(s) absent upstream
  missing upstream: notes.txt
```

(The summary appears twice: once as a warning logged while the listing resolves, once
as the report itself. Harmless, and a hint that any *other* command touching this
record will warn too.)

Drift in the other direction — the archive holding files the manifest never recorded,
which is what you get from a manifest written from a partial download:

```console
$ archivefs check -o chiro=./GSE176078.chiro.json geo://GSE176078
chiro drift in GSE176078: ./GSE176078.chiro.json: 2 deposit file(s) not in the manifest
./GSE176078.chiro.json: 2 deposit file(s) not in the manifest
  not in manifest:  GSE176078_RAW.tar
  not in manifest:  GSE176078_Wu_etal_2021_BRCA_scRNASeq.tar.gz
```

| line | meaning | usual cause |
|---|---|---|
| `missing upstream:` | the manifest promises a file the archive does not have | the record was revised, a file withdrawn, or the manifest describes a different version |
| `not in manifest:` | the archive has a file the manifest never recorded | files added after the manifest was written |
| `size differs:` | same path, different size | the file was replaced |
| `checksum differs:` | same path and size, different bytes | replaced content — the one that matters most, and the reason a manifest is worth having |

A file that is `missing upstream` still appears in `ls`, deliberately: hiding it would
hide the drift. It will fail when read.

## Scenario: is this analysis still reproducible?

Run `check` in CI, or before re-running a pipeline you last ran a year ago:

```console
$ archivefs check -o chiro=manifests/GSE176078.chiro.json geo://GSE176078 || {
    echo "upstream changed — see the drift report above" >&2
    exit 1
  }
```

Exit 1 is a real signal: someone changed the deposit under you. That is exactly the
situation a `.dvc` file's pinned checksum protects you from — and `check` tells you it
happened rather than letting a later `dvc pull` fail mysteriously.

## Scenario: the manifest describes a different layout

The shape of the message when a manifest matches nothing in the record (numbers and
accession will be yours):

```
ManifestError: …chiro.json describes N file(s) and not one of them matches
anything in EGAD00001006673. The manifest's source_paths are probably written
against a different layout or a different record; check `record_accession` in the
manifest. Pass chiro=False to use the archive's own listing instead.
```

Most often an EGA manifest written against display names while the filesystem keys
members by accession — fix with `-o layout=display`, or check you have the right
record. Refusing here is the point: a listing whose every entry fails to read would be
a slow way to learn the same thing.

## Exit status

| code | when |
|---|---|
| 0 | manifest matches the deposit — *or* there is no manifest at all |
| 1 | drift found, or bad usage |
| 2 | archive-level failure, unusable manifest, or `chirograph` missing when `-o chiro=` named one |

Note that 0 covers both "verified clean" and "nothing to verify". If you need to know
which, look at the output, or call `manifest_path_for()` from Python.

## See also

[`cli-verify.md`](cli-verify.md) · [`cli-ls.md`](cli-ls.md) for how a manifest changes
a listing · [`decisions.md`](decisions.md) D5 on manifest absence versus manifest
failure
