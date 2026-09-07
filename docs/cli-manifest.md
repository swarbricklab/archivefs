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
| `URL` | a record or subtree, e.g. `zenodo://1486184`, `bia://S-BIAD3080/images/ihc`, `geo://GSE176078` |
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

[`cli-check.md`](cli-check.md) (compare a deposit to its manifest) ·
[`cli-verify.md`](cli-verify.md) (verify a downloaded tree) ·
[`concepts-and-versions.md`](concepts-and-versions.md) (why a manifest is a *version*)
