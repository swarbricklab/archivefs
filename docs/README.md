# archivefs documentation

## Start here

| if you want to… | read |
|---|---|
| import someone's imaging deposit, start to finish | [`importing-from-bia.md`](importing-from-bia.md) |
| use any archive as a DVC dependency | [`using-with-dvc.md`](using-with-dvc.md) |
| set up EGA (or restricted Zenodo) access safely | [`credentials.md`](credentials.md) |
| know why something is built the way it is | [`design.md`](design.md), [`decisions.md`](decisions.md) |

## Command reference

One page per subcommand: syntax, every option, worked scenarios, exit codes.

| command | one line |
|---|---|
| [`archivefs info`](cli-info.md) | is this installation wired up? |
| [`archivefs ls`](cli-ls.md) | what is in this record, before downloading it |
| [`archivefs stat`](cli-stat.md) | everything known about one path, as JSON |
| [`archivefs get`](cli-get.md) | download, verifying against published checksums |
| [`archivefs check`](cli-check.md) | has the *deposit* drifted from its manifest? |
| [`archivefs verify`](cli-verify.md) | do the files on *my disk* match the manifest? |
| [`archivefs dvc`](cli-dvc.md) | register the protocols with DVC — required once per environment |

Every command that takes a URL also takes `-o KEY=VALUE`.

## Storage options (`-o KEY=VALUE`)

The same options work three ways: `-o key=value` on the CLI,
`fsspec.filesystem("bia", key=value)` in Python, and `--fs-config key=value` under
DVC. `-o` is repeatable. Only the exact strings `true` and `false` are converted to
booleans; everything else stays a string.

**Credentials are not storage options.** `username`, `password` and `token` are
absent from the tables below on purpose — they come from the environment or a
private file, and every other route is refused. See
[`credentials.md`](credentials.md).

**Every backend:**

| option | values | meaning |
|---|---|---|
| `chiro` | `auto` (default), `false`, or a path/URL | use a `.chiro.json` manifest as the listing and the checksums. `auto` discovers one inside the deposit. An explicit value may be a **file in your repo** (`chiro=GSE176078.chiro.json` — resolved locally first, so it travels with a `git clone`), an absolute path, a URL, or a path inside the deposit. Order and rationale: [`decisions.md`](decisions.md) D18. |

**Per backend:**

| backend | option | values | meaning |
|---|---|---|---|
| `bia` | `view` | `submitted` (default), `derived` | depositor's files, or BIA's OME-Zarr conversions |
| | `api_url`, `s3_url` | URL | override the BioStudies API / Embassy S3 endpoint |
| `ega` | `credentials_file` | path | JSON file to read username + password from — a *path*, so it may be committed |
| | `layout` | `accession` (default), `display`, `nested` | how dataset members are named ([`decisions.md`](decisions.md) D7) |
| | `api_url` | URL | override the EGA API endpoint |
| `zenodo` | `extract_zips` | `true`, `false` (default) | list ZIP contents as directories, read members by range. **Off by default**, though the reason that justified it has gone — an expanded ZIP is now pullable from Zenodo by a reviewer with no remote ([`decisions.md`](decisions.md) D23). Do not combine with `--no-download`: it costs a full transfer and buys nothing |
| | `strip_components` | integer, `1` (default) | drop leading directories from a ZIP's interior, but only while each is its sole top-level entry — so `annotations.zip` holding `annotations/…` expands without the redundant level. A **maximum, not a promise**: a ZIP with two real top-level entries is passed through untouched. `0` presents the interior verbatim ([`decisions.md`](decisions.md) D24) |
| | `api_url` | URL | override the Zenodo API endpoint |
| `geo` | `view` | `supplementary` (default), `raw` | `raw` is a deliberate seam and refuses with an explanation |
| | `base_url` | URL | override the GEO FTP-over-HTTPS root |

## Which archive gives you what

| backend | download-free checksum | notes |
|---|---|---|
| `zenodo://` | ✅ MD5 from record metadata | record id, version DOI, or concept DOI (→ latest) |
| `ega://` | ✅ MD5 of the *decrypted* bytes | needs credentials + DAC authorisation |
| `bia://` derived | ✅ MD5 from S3 ETag | OME-Zarr pyramids and thumbnails |
| `bia://` submitted | ❌ | a `.chiro` sidecar fixes it |
| `geo://` | ❌ | a `.chiro` sidecar fixes it |
| `dbgap://` | — | deferred; refuses with an explanation |

Where a checksum is missing, `dvc import-url --no-download` still records a correct
one — by streaming the whole file. That is the single most surprising cost in the
system: [`decisions.md`](decisions.md) D14.

## Background

* [`design.md`](design.md) — architecture: the shared base, the manifest layer, why
  each backend is only a byte-fetcher
* [`decisions.md`](decisions.md) — every deviation from the plan, and the archive
  behaviours measured the hard way (D1–D14)
* [`ideas.md`](ideas.md) — extensions worth building, and things deliberately not built
* [`plan.md`](plan.md) — the original checklist
* [`evidence/`](evidence) — raw measurements behind the claims above
