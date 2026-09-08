# `archivefs manifest`

**Write a `.chiro` manifest for a deposit — download-free where the archive
publishes checksums.**

```
archivefs manifest [-o KEY=VALUE]… [--out PATH] [--download] [--self-deposited] URL
```

A [Chirograph](https://github.com/swarbricklab/chirograph) manifest is a
content-addressed record of exactly which files a deposit holds and their checksums.
Committed next to a `.dvc`, it is what turns a mutable *concept* reference into a
reproducible, drift-detecting *version* reference
([`concepts-and-versions.md`](concepts-and-versions.md)). This command is how you make
one from an archive you can list.

The reader half (`-o chiro=…` on any command) turns a manifest into a listing; this is
the writer half — it turns a listing into a manifest.

By default the manifest is written into the repo-local **store**,
`.chirograph/manifests/`, named by the digest of its own bytes and sharded the way
chirograph prescribes (`.chirograph/manifests/15/576e….chiro.json`). The command prints
the resolvable reference — `chiro://md5:<digest>` — which is what you record in a `.dvc`
([`chiro-protocol.md`](chiro-protocol.md)). Pass `--out PATH` to write a loose file
instead (for an ad-hoc manifest outside a store), or `--out -` for stdout.

## Arguments and options

| | meaning |
|---|---|
| `URL` | a record or subtree, e.g. `zenodo://1486184`, `bia://S-BIAD3080/images/ihc`, `geo://GSE176078` (omit with `--from-dvc`) |
| `--from-dvc FILE.dvc` | reuse the checksums DVC already computed for an import (see below) |
| `--out` | write to an explicit path instead of the store (`-` for stdout) |
| `--download` | read and hash the bytes for files the archive publishes no checksum for |
| `--self-deposited` | mark provenance `self-deposited` (default `observed-download`) |
| `-o`, `--option` | storage option, repeatable |

Needs `chirograph` installed (`pip install 'archivefs[chiro]'`); without it the command
reports the missing extra rather than a traceback.

## Who pays for the checksum

That is the whole design, and it splits by archive:

| archive | what happens |
|---|---|
| **Zenodo, EGA, derived BIA** | the listing already carries an md5, so the manifest is built with **no download at all** — one metadata call |
| **GEO, column-less BIA** | the archive publishes no digest; `--download` reads and hashes each file (the only way to a real checksum), and without it the command **fails, naming the files** |

An etag or mtime is never quietly promoted to a checksum — a manifest either records a
real digest or refuses to record the file.

## ZIP deposits — a container manifest (schema 2.0)

When a deposit is a **ZIP** and it is read transparently (`-o extract_zips=true` — for
Zenodo this is opt-in, not the default), the manifest becomes a chirograph **schema-2.0**
container manifest ([`decisions.md`](decisions.md) D41, `container-model.md`): a top-level
`containers` entry records the ZIP's own identity — its path, `format: zip`, the whole-file
md5 the archive publishes, and its size — and each member gains a `crc32` (from the ZIP's
central directory) and a `source_path` that links it to the container. A member still
carries its own authoritative `md5`, so the DVC `.dir` hash is byte-identical to a plain
import — the container fields are additive.

A ZIP member has no published md5, though, so a container manifest is **not** download-free:
`--download` is required to read and hash each member. What the download buys you, that a
plain (opaque-ZIP) pin does not, is member-granular provenance and a later
**download-free** [`archivefs confirm`](cli-confirm.md): the `crc32` + container md5 let you
cross-check the deposit without fetching a byte. Without `-o extract_zips=true` you get a
plain, download-free pin of the ZIP as one opaque file instead.

**Address the whole record or one ZIP.** Two framings, and the choice sets the workspace
layout:

* `zenodo://<record>` — every member's `relpath` keeps its `archive.zip/…` prefix (the ZIP
  is a child of the record). Pins the whole record in one manifest.
* `zenodo://<record>/archive.zip` — the target is one ZIP, so each `relpath` is the bare
  in-zip path (`visium/x.csv`), while `source_path` still carries the `archive.zip/…` prefix
  for container linkage (a *relocated* entry). This is how a **multi-ZIP record** is pinned
  one ZIP at a time, and it matches the layout an existing
  `dvc import-url zenodo://…/archive.zip` already produced — so re-pinning to `chiro://`
  leaves the DVC `.dir` hash unchanged and needs no re-fetch.

```console
$ archivefs manifest zenodo://1486184 --download -o extract_zips=true
.chirograph/manifests/b3/150a….chiro.json: 2 file(s), 1019 B, chiro://md5:b3150a… (observed-download)
```

## `--from-dvc`: reuse the checksums DVC already computed

If the deposit is **already a DVC import** (`dvc import-url zenodo://…/archive.zip`), DVC
hashed every file when it pulled them. `--from-dvc` reuses those md5s instead of reading the
archive again: it takes the source URL, the per-file md5s, and the import's `fs_config`
straight from the `.dvc`, and lets the archive *listing* supply everything else (crc32,
container md5, size, concept). The result is **byte-for-byte the manifest `--download` would
have produced** — same `body_hash`, same `.dir` hash — for the cost of a metadata listing
instead of the download.

```console
$ archivefs manifest --from-dvc data/anndata_objects.dvc
.chirograph/manifests/…: 58 file(s), 19.4 GiB, chiro://md5:… (observed-download)
```

That 19.4 GiB deposit is pinned without fetching a payload byte — the only cost is reading
the ZIP's central directory. It is scoped to an **import**: a plain `dvc add` has no upstream
URL for a `chiro://` pin to route to, and is refused with a message saying so. The checksums
come from the `.dvc`'s inline `files:` list, or — for an import that records only a directory
hash — from DVC's own view (`dvc.api.DVCFileSystem`), never from a re-hash.

## What it prints

The manifest is written into the store; a one-line summary goes to **stderr**, ending
in the reference you record in a `.dvc`:

```console
$ archivefs manifest zenodo://1486184
.chirograph/manifests/15/576e….chiro.json: 2 file(s), 12.4 KiB, chiro://md5:15576e… (observed-download)
```

## Scenario: pin a GEO subtree that publishes nothing

```console
$ archivefs manifest geo://GSE176078
ManifestError: 3 file(s) in GSE176078 publish no checksum (…), so a
download-free manifest cannot be built for them. Re-run with --download …

$ archivefs manifest geo://GSE176078 --download
.chirograph/manifests/ac/3a5a….chiro.json: 3 file(s), 4.2 MiB, chiro://md5:ac3a5a… (observed-download)

$ dvc import-url --no-download chiro://md5:ac3a5a… data/geo
```

Commit the `.chirograph/` store alongside the `.dvc`, and every later fetch resolves the
digest to the manifest and verifies against it — reproducibility for an archive that
offers none of its own. `md5sum .chirograph/manifests/**` re-checks the whole store with
no tooling.

## Exit status

| code | when |
|---|---|
| 0 | manifest written |
| 2 | archive-level failure, or a checksum was missing and `--download` was not given |

## See also

[`cli-confirm.md`](cli-confirm.md) (cross-check a ZIP deposit, download-free) ·
[`cli-check.md`](cli-check.md) (compare a deposit to its manifest) ·
[`cli-verify.md`](cli-verify.md) (verify a downloaded tree) ·
[`concepts-and-versions.md`](concepts-and-versions.md) (why a manifest is a *version*)
