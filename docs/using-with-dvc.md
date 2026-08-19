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

## If a reviewer must be able to pull from the archive, use `--no-download` then `dvc pull`

This is the most important thing on this page, and it is not obvious.

**A completed `dvc import-url` cannot be pulled from the archive by anyone else.** Once
`outs.md5` is recorded, DVC stops treating the source URL as a place the bytes could
come from — it assumes they are in your DVC remote. A reviewer who clones your repo
with no remote configured gets `Checkout failed … Is your cache up to date?`, even
though the checksum is sitting right there in the committed `.dvc`. The problem is not
verification, it is that there is no byte source. Full measurements:
[`decisions.md`](decisions.md) D17.

The fix costs one extra command:

```console
$ dvc import-url --no-download zenodo://1486184/github-mark.zip logo.zip
$ dvc pull                       # you get the bytes, locally, verified
$ git diff logo.zip.dvc          # unchanged: still pullable from the archive
```

`--no-download` does not mean "do without the data" — it means "do not record an out
hash". Pulling afterwards gives you the file in your workspace and cache while leaving
the committed `.dvc` in the shape a reviewer can use. Verified: a clone with no remote
anywhere runs `dvc pull` and gets the right bytes.

**It also needs the archive to publish a checksum.** `geo://` and `bia://` submitted
files publish none, so a reviewer route requires a `.chiro` manifest to supply one
(`--fs-config chiro=…`). For those two, a manifest is not a nicety — without it there
is no way for anyone else to pull from the source.

| form | bytes now | `.dvc` written | reviewer can pull from archive | use when |
|---|---|---|---|---|
| `dvc import-url --no-download URL out` then `dvc pull` | yes | yes | **yes**, if a checksum is published | you want the data *and* a reproducible repo |
| `dvc import-url URL out` | yes | yes | no — needs `dvc push` to a remote | the data is only for you, or you host a remote |
| `dvc import-url --no-download URL out` | *usually* not | yes | as above | you want the pin now, the data later |
| `dvc import-url --no-exec URL out` | no | yes, **without checksums** | no | you only want the wiring |

`dvc push` does work for `import-url` stages and covers every archive regardless of
checksums — unlike `dvc import` from another DVC repo, which pushes nothing. But it
means hosting a copy of data that is already published, which is usually the thing you
were trying to avoid.

### The cost side of `--no-download`

`--no-download` records a **correct** checksum, and where the archive publishes none,
DVC computes it by streaming the whole file. On a 120 MB BIA submitted file that is 83 s
of transfer for a stage that keeps no bytes, and you transfer it again on `dvc pull`.

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

`null` means you have a decision to make, and it is the one from the top of this page:
supply a `.chiro` manifest and keep the reviewer route (the checksum then comes from
metadata, so `--no-download` is cheap *and* pullable), or drop `--no-download` to save
one transfer and accept that only a DVC remote will get the data to anyone else.
Background: [`decisions.md`](decisions.md) D14 for the cost, D17 for the reviewer route.

## Passing storage options with `--fs-config`

Some backends need a setting — which BIA view, which EGA layout, a manifest to use.
Two ways, and they differ in what ends up in the committed file.

Pass them with `--fs-config`, which keeps the accession in the `.dvc` file:

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

`fs_config` is committed, so it holds settings, never secrets — and it is no longer
possible to put a secret there: DVC rejects `--fs-config password=…` outright. See
[`credentials.md`](credentials.md).

### Using an archive as a DVC remote is not supported

`dvc remote add bia bia://S-BIAD599` will appear to work, and `import-url` through
`remote://bia/…` will even pull. Don't. It is unsupported and will stay that way:

* **Push cannot be made to work.** Depositing into an archive is not a file upload —
  it is accession reservation, metadata registration and a curation workflow, and it
  differs completely between EGA, Zenodo, GEO and BIA. A remote that silently
  supports half of DVC's contract is worse than one that does not exist.
* **It hides the accession.** The committed `.dvc` records `path: remote://bia/…`,
  moving the accession into `.dvc/config`. Still reproducible, but a reader of the
  `.dvc` file alone can no longer see which study the data came from — which is the
  entire point of importing this way.

`dvc import-url` with `--fs-config` is the supported pattern, and it covers every
storage option a remote could have carried. Rationale in
[`decisions.md`](decisions.md) D15.

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
4. **A stage they can actually pull.** This is on *you*, not them, and it is the part
   people get wrong. The archive is only usable as the remote when the stage was
   imported with `--no-download` **and** the archive publishes a checksum. Otherwise
   they need a DVC remote you have pushed to. Check before you hand the repo over:

   ```console
   $ grep -A3 '^outs' bulk.txt.gz.dvc
   outs:
   - hash: md5
     path: bulk.txt.gz        # no `md5:` here → pullable from the archive
   ```

   An `md5:` under `outs` means a completed import, and a reviewer without your cache
   or a remote will get `Checkout failed … Is your cache up to date?`. Fix it by
   re-importing with `--no-download` and then `dvc pull` — you keep the data, they gain
   the route. [`decisions.md`](decisions.md) D17.

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
