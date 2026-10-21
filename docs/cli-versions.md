# `archivefs versions`

**List a deposit's versions, newest first — so you can pin one.**

```
archivefs versions [-o KEY=VALUE]… [--json] URL
```

A versioned archive publishes more than one state of the same deposit over time. This
lists them, newest first, with the identifier you can pin, a date, and a label. It is
available for the backends that expose a version history: **`cbioportal://`**,
`zenodo://` and `mendeley://`. For an archive without one (`geo://`, `ega://`, `bia://`)
it refuses with that explanation rather than printing nothing.

A member URL works too — it lists the *containing* deposit's versions, not the file's.

## Arguments and options

| | meaning |
|---|---|
| `URL` | a deposit (a container URL), or any path inside one |
| `--json` | emit the raw version records instead of the table |
| `-o`, `--option` | storage option, repeatable — see [the table in `docs/README.md`](README.md#storage-options--o-keyvalue) |

## cBioPortal: git history is the version history

A cBioPortal study is versioned by the datahub repo's git history — each commit that
touches the study is a release. The first column is a ready-to-use pin URL:

```console
$ archivefs versions cbioportal://brca_metabric
brca_metabric@86690e1ed9752b1dcd50b5657f5f05eafa4b6b78  2026-01-30T19:02:42Z  Gene Table v7: migrate outdated gene symbols in data files (#2252)
brca_metabric@a022eca324072ce6575586e5ea5fda305fc90a90  2025-02-13T22:44:37Z  Added gene panel files
brca_metabric@9b965f24060360bafdb7fcb745e56099a00c668b  2024-04-22T13:35:45Z  update survival data
…
brca_metabric@1ea328cd77574cf31ccca4224a1d015539c44928  2023-03-17T16:10:25Z  update meta

# pin one — the accession in column 1 is the URL:
$ dvc import-url "cbioportal://brca_metabric@1ea328cd77/data_cna.txt" cna.txt
```

A tag (`@v1.0.0`) pins the same way. `archivefs` resolves whatever ref you address to the
study's head commit, so the pin is immutable even after the datahub's `master` moves
([`decisions.md`](decisions.md) D44).

## `--json`

```console
$ archivefs versions --json cbioportal://brca_metabric
[
  {
    "study": "brca_metabric",
    "commit": "86690e1ed9752b1dcd50b5657f5f05eafa4b6b78",
    "accession": "brca_metabric@86690e1ed9752b1dcd50b5657f5f05eafa4b6b78",
    "date": "2026-01-30T19:02:42Z",
    "message": "Gene Table v7: migrate outdated gene symbols in data files (#2252)"
  },
  …
]
```

The record shape differs by backend (a cBioPortal git commit, a Zenodo/Mendeley record),
so the table picks the most identifying field each publishes; `--json` gives you all of
them.

## Exit status

`0` on success, including an empty history. A backend that has no version history exits
non-zero with the explanation.
