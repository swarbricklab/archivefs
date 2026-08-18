# Using archivefs with DVC

**Goal: someone else's archived data becomes an input to your project, with a
committed, checksummed record of exactly which data it was.**

`dvc import-url` is the one command that does both halves — fetch, and pin. This guide
covers the whole loop: explore, import, commit, refresh, and what a collaborator needs
to reproduce it.

Prerequisite: `archivefs dvc enable` once per environment. If you skip it, everything
here fails with `config file error: Unsupported URL type …`. See
[`cli-dvc.md`](cli-dvc.md).

## The shortest useful example

```console
$ dvc import-url --no-download zenodo://1486184/github-mark.zip logo.zip
$ cat logo.zip.dvc
md5: 99e008a3fc0db89fe03ab936dcc216cd
frozen: true
deps:
- md5: 49f6a914ea33912471512ac9228c6923     # from Zenodo's metadata, no bytes fetched
  size: 853
  hash: md5
  path: zenodo://1486184/github-mark.zip
outs:
- hash: md5
  path: logo.zip

$ git add logo.zip.dvc .gitignore && git commit -m "Import the logo from Zenodo"
$ dvc pull logo.zip.dvc          # now, or on someone else's machine, months later
```

What is worth noticing in that file:

* **`deps[].path` is an accession**, not a signed URL and not a secret. It is safe to
  commit and it means the same thing to everyone.
* **`deps[].md5` is the archive's own checksum**, so `dvc pull` verifies the bytes
  against what the archive published — not against what you happened to download.
* **`frozen: true`** — an import does not re-check upstream on every command. Use
  `dvc update` when you want it to.

## Explore before importing

```console
$ dvc list-url bia://S-BIAD599
20200220-ftp

$ dvc list-url -R --size zenodo://1486184
$ dvc list-url --fs-config view=derived -- bia://S-BIAD599 | head -3
S-BIAD599-annotation-representative-512-512.png
01776a3a-16a5-4c48-a104-fefcc83cc2ee
01d3f195-c42f-4c47-8b0a-6b5d28f1e822
```

`archivefs ls -l` shows the same thing plus checksums, which is usually what you
actually want ([`cli-ls.md`](cli-ls.md)).

## `--no-download` is about *when*, and sometimes costs a full transfer

| form | bytes now | `.dvc` written | use when |
|---|---|---|---|
| `dvc import-url URL out` | yes | yes | you want the data now |
| `dvc import-url --no-download URL out` | *usually* not | yes | you want the pin now, the data later (CI, a shared repo, a big study) |
| `dvc import-url --no-exec URL out` | no | yes, **without checksums** | you only want the wiring |

The catch, measured: `--no-download` records a **correct** checksum, and where the
archive publishes none, DVC computes it by streaming the whole file. On a 120 MB BIA
submitted file that is 83 s of transfer for a stage that keeps no bytes, and you will
transfer it again on `dvc pull`.

| archive | publishes checksums? | `--no-download` cost |
|---|---|---|
| `zenodo://` | ✅ MD5 in record metadata | metadata only |
| `ega://` | ✅ MD5 of the decrypted bytes | metadata only |
| `bia://` derived (OME-Zarr) | ✅ MD5 from S3 ETag | metadata only |
| `bia://` submitted | ❌ | **full transfer** |
| `geo://` | ❌ | **full transfer** |

Check before you commit to a big import:

```console
$ archivefs stat geo://GSE176078/GSE176078_RAW.tar | grep '"md5"'
  "md5": null,
```

`null` means: either drop `--no-download` (one transfer instead of two), or supply a
`.chiro` manifest so the checksum comes from metadata again. Background:
[`decisions.md`](decisions.md) D14.

## Passing storage options: `--fs-config` or a remote

Some backends need a setting — which BIA view, which EGA layout, a manifest to use.
Two ways, and they differ in what ends up in the committed file.

**`--fs-config` (preferred).** The accession stays in the `.dvc` file:

```console
$ dvc import-url --no-download --fs-config view=derived -- \
    "bia://S-BIAD599/$U/$U-thumbnail-128-128.png" thumb.png
$ cat thumb.png.dvc
deps:
- md5: a84906a7545b1d47386e7d72ee807c41
  size: 12234
  path: bia://S-BIAD599/01776a3a-…/01776a3a-…-thumbnail-128-128.png
  fs_config:
    view: derived
```

Note the `--`. `--fs-config` takes any number of `name=value` pairs, so without it DVC
swallows the URL and fails with *"not enough values to unpack"*.

**A named remote**, when you are importing many files from one record:

```console
$ dvc remote add bia bia://S-BIAD599
$ dvc remote modify bia view derived
$ dvc import-url --no-download "remote://bia/$U/$U-thumbnail-128-128.png" thumb.png
```

The trade-off: the committed `.dvc` then records `path: remote://bia/…`, moving the
accession into `.dvc/config`. Still reproducible — that file is committed too — but a
reader of the `.dvc` file alone can no longer see which study the data came from.

> **`fs_config` is committed. Never put a secret in it.** `view`, `layout`, `api_url`,
> `chiro` are fine. A `password` or `token` passed this way is written into a file you
> are about to `git commit`. Secrets belong in `.dvc/config.local` or the environment —
> see [`credentials.md`](credentials.md).

## Importing a whole record

```console
$ dvc import-url --no-download geo://GSE176078 raw/
```

DVC treats it as a directory import and records a `.dir` hash over every file. Two
warnings:

* On a checksum-less archive this transfers **everything** (see the table above). For
  a 60 GB imaging study, import the files you need instead — `archivefs ls -R` then a
  loop, or a manifest.
* The directory listing is resolved once, at import time. Files added upstream later
  appear only on `dvc update`.

## Refreshing when the archive changes

```console
$ dvc update thumb.png.dvc                 # re-check upstream, re-fetch if changed
$ dvc update --no-download thumb.png.dvc   # re-check the pin only
```

`dvc update` compares the dep checksum against the archive's current one, so it
detects a replaced file. To ask the same question without touching DVC — and to see
*what* changed — use `archivefs check` against a manifest ([`cli-check.md`](cli-check.md)).

Related: `archive_version` in `archivefs stat` (Zenodo DOI/revision, EGA re-encryption
timestamp) is the archive's own version token. It is deliberately **not** mapped to
DVC's `version_id`, because DVC's cloud-versioning model assumes something these
archives do not provide — so `--version-aware` does not apply here
([`decisions.md`](decisions.md) D6).

## What a collaborator needs to reproduce your import

Cloning your repo and running `dvc pull` requires, in their environment:

1. **archivefs installed** — same protocols, so the `path` in your `.dvc` resolves.
2. **`archivefs dvc enable`** — theirs, not yours. This is the usual failure.
3. **Credentials, only for controlled access.** `zenodo://`, `geo://` and `bia://` are
   open. `ega://` needs their *own* EGA account, authorised by the relevant DAC — the
   `.dvc` file contains no credentials by design.
4. **Nothing else.** No copy of your cache, no shared remote — the archive is the
   remote.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `config file error: Unsupported URL type bia://` | DVC cannot see archivefs | `archivefs dvc enable` in that env ([`cli-dvc.md`](cli-dvc.md)) |
| `--no-download` takes minutes and moves GBs | the archive publishes no checksum | expected — see the table above; drop the flag or use a manifest |
| `No remote provided and no default remote set` on `dvc pull` | you have no DVC cache remote configured | harmless for imports; the archive is the source |
| `CredentialsNotFoundError: …` listing four places it looked | EGA credentials absent | [`credentials.md`](credentials.md) |
| `not enough values to unpack (expected 2, got 1)` | `--fs-config` ate the URL | add `--` before the URL |
| `ArchiveNotFoundError: 'x' is not in Y. It holds: …` | typo in the path | the message lists the real contents |

## See also

[`importing-from-bia.md`](importing-from-bia.md) — the same loop, start to finish, for
one archive · [`credentials.md`](credentials.md) · [`cli-dvc.md`](cli-dvc.md) ·
[`decisions.md`](decisions.md) D4, D6, D14
