# `archivefs confirm`

**Does the live deposit still match this `.chiro` manifest? — download-free for a ZIP.**

```
archivefs confirm [-o KEY=VALUE]… [--download] TARGET
```

`TARGET` is a [`chiro://md5:<digest>`](chiro-protocol.md) reference (or a path to a
committed `.chiro.json`). A depositor prepares a manifest — often from local files,
*before* uploading — and then needs to answer: did the archive receive exactly those
bytes? For loose files that is a published-md5 comparison. For a **ZIP deposit** it is
not, because a ZIP *member* has no published checksum anywhere. `confirm` is the verb
that cross-checks it anyway, as cheaply as the container format allows.

## What it does

1. Loads the manifest and reads its `concept` — which archive owns the bytes — and lists
   the **version** it pins (`record_accession`), routed through that backend
   ([`decisions.md`](decisions.md) D36/D41). For a ZIP deposit the backend expands the
   archive, so its members surface with their `crc32`.
2. For each container, picks a strategy from `chirograph.Container.confirm_class`
   (chirograph owns the format → class map):

   | `format` | class | what confirm checks | member bytes? |
   |---|---|---|---|
   | `zip` | `indexed` | each member's `crc32` + size, and the container's published md5 | **none** — a central-directory tail read |
   | `tar` | `sequential` | member `md5` via a targeted read | with `--download` |
   | `tar.gz`, `tar.bz2`, `tar.xz`, `tar.zst`, *unrecognised* | `stream` | member `md5` after full decompress | with `--download` |

3. For a **loose** file (under no container) it compares the archive's published md5 where
   there is one — download-free — else size, or a real read under `--download`.

The value split is deliberate: `crc32` is weak on its own, but here it is *anchored* — the
container's md5 authenticates the whole ZIP (so its central directory, and the `crc32`s in
it, are trusted), and the member's own `md5` is still in the manifest for everything that
needs a real content hash. `confirm` is the cheap remote cross-check; verify-on-read and
DVC hashing use the `md5`.

## Confirm vs update vs check

* **`confirm`** — does the *live deposit* match this manifest, checked cheaply? Version-
  pinned. This is the depositor's "did my upload arrive intact" and the reviewer's "is the
  archive still serving what the pin promises".
* [**`update`**](cli-update.md) — is there a *newer version* upstream? It regenerates the
  manifest (for a ZIP that needs member md5s, i.e. a download) and diffs. Concept-tracking.
* [**`check`**](cli-check.md) — drift between a deposit and the manifest it was published
  with, from metadata only.

## What it prints

```console
$ archivefs confirm chiro://md5:b3150a…
chiro://md5:b3150a…: deposit 1486184 matches the manifest — 2 matched
  container github-mark.zip [indexed]: md5 ok (49f6a914…)

$ archivefs confirm chiro://md5:b3150a…        # after a member was re-packed differently
chiro://md5:b3150a…: deposit 1486184 DIFFERS from the manifest — 1 matched, 1 changed/missing
  container github-mark.zip [indexed]: md5 ok (49f6a914…)
  crc32      github-mark.zip/github-mark.svg [crc32] — manifest b8576d45 vs deposit 0af31c22
```

A clean member is not printed — the report is scoped to what needs attention (`crc32` /
`size` / `md5` mismatches, `missing` promised members, `unrecorded` extras). A `size-only`
line means the format offered no cheap content check and `--download` was not given.

## Exit status

| code | when |
|---|---|
| 0 | the deposit matches the manifest |
| 1 | a container md5, member, or file set differs |
| 2 | not a manifest, no `concept` to route on, or an archive-level failure |

## Limitations

* The check is against the archive's *presentation* of the ZIP, re-listed through the same
  backend, so a manifest built with a non-default `strip_components` must be confirmed with
  the same value ([`decisions.md`](decisions.md) D41).
* `sequential` / `stream` confirm is not cheap and, for `sequential` (tar), the targeted-read
  path is only reachable once a tar-expanding backend exists; today confirm falls back to a
  full read under `--download`.

## See also

[`cli-manifest.md`](cli-manifest.md) (write a container manifest) ·
[`cli-update.md`](cli-update.md) (is there a newer version?) ·
[`chiro-protocol.md`](chiro-protocol.md) ·
[`concepts-and-versions.md`](concepts-and-versions.md)
