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

## Arguments and options

| | meaning |
|---|---|
| `URL` | a record, e.g. `zenodo://1486184`, `bia://S-BIAD3080`, `geo://GSE176078` |
| `--out` | where to write; default `<accession>.chiro.json` in the cwd, `-` for stdout |
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

The document is written to the file (or stdout); a one-line summary goes to **stderr**,
so a piped document stays clean:

```console
$ archivefs manifest zenodo://1486184 --out record.chiro.json
record.chiro.json: 2 file(s), 12.4 KiB, chiro://md5:6f174b… (observed-download)

$ archivefs manifest zenodo://1486184 --out - | jq .body_hash
"6f174b…"
```

## Scenario: pin a GEO record that publishes nothing

```console
$ archivefs manifest geo://GSE176078
ManifestError: 3 file(s) in GSE176078 publish no checksum (…), so a
download-free manifest cannot be built for them. Re-run with --download …

$ archivefs manifest geo://GSE176078 --download --out GSE176078.chiro.json
GSE176078.chiro.json: 3 file(s), 4.2 MiB, chiro://md5:… (observed-download)
```

Commit `GSE176078.chiro.json` next to the `.dvc` and every later fetch verifies against
it — reproducibility for an archive that offers none of its own.

## Exit status

| code | when |
|---|---|
| 0 | manifest written |
| 2 | archive-level failure, or a checksum was missing and `--download` was not given |

## See also

[`cli-check.md`](cli-check.md) (compare a deposit to its manifest) ·
[`cli-verify.md`](cli-verify.md) (verify a downloaded tree) ·
[`concepts-and-versions.md`](concepts-and-versions.md) (why a manifest is a *version*)
