# `archivefs update`

**Has a `.chiro` pin's upstream moved? Regenerate it, and — with `--write` — re-pin.**

```
archivefs update [-o KEY=VALUE]… [--write] [--download] TARGET
```

`TARGET` is a committed `.chiro.json` or a [`chiro://`](chiro-protocol.md) URL naming
one. A pin is content-addressed and immutable, so `dvc update` on it is correctly a
no-op — but the *dataset* it points at moves. This is the verb that notices, and that
supersedes the pin, because moving a content-addressed reference means making a **new**
manifest and repointing, not editing one in place
([`concepts-and-versions.md`](concepts-and-versions.md)).

## What it does

1. Loads the committed manifest and reads its `concept` — which archive it pins.
2. **Regenerates** the manifest from the *live* archive, routed through that backend
   ([`decisions.md`](decisions.md) D36). Download-free where the archive publishes
   checksums; `--download` reads and hashes for one that does not (GEO).
3. Compares the new `body_hash` to the pinned one and reports the difference.

There is no shortcut through an mtime or a version token (D34): a checksum is the only
proof of change, so the answer is always "regenerate and compare", never "trust the
date".

## What it prints

```console
$ archivefs update GSE176078.chiro.json --download
up to date: geo:GSE176078 — body_hash 6f174b… unchanged

$ archivefs update GSE176078.chiro.json --download        # after the depositor adds a file
upstream moved: 1 added
  + GSE176078_sampleC_new.tsv.gz

re-run with --write to re-pin to the current upstream.

$ archivefs update GSE176078.chiro.json --download --write
upstream moved: 1 added
  + GSE176078_sampleC_new.tsv.gz

re-pinned GSE176078.chiro.json: 6f174b… → 91ac02…
Commit the rewritten manifest (and re-run `dvc update` if it is a DVC import).
```

`+` added, `-` removed, `~` changed. `--write` rewrites the local manifest; commit that
change (it is a reviewable diff, exactly like advancing a branch to a new commit). A pin
that is a URL has no local file to rewrite — fetch a local copy first.

## Exit status

| code | when |
|---|---|
| 0 | up to date, **or** re-pinned with `--write` |
| 1 | the upstream moved and `--write` was not given (report only) |
| 2 | not a manifest, no `concept` to route on, or an archive-level failure |

## Limitations

* Regeneration lists the archive's *current* layout, so it assumes the manifest's
  `relpath`s match the archive's native paths — true for manifests written by
  [`archivefs manifest`](cli-manifest.md), which do not relocate. A hand-relocated
  manifest would diff as wholesale added/removed.
* The cheap "record moved?" pre-check (a metadata-only version token before the full
  regenerate) is not yet wired — `update` always does the authoritative regenerate. See
  `archivefs-update-design.md` §3.

## See also

[`cli-manifest.md`](cli-manifest.md) (make a pin) ·
[`chiro-protocol.md`](chiro-protocol.md) ·
[`cli-check.md`](cli-check.md) (a deposit vs its manifest, read-only)
