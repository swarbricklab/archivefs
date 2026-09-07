# The `chiro://` protocol — a manifest *is* the address

**A committed `.chiro` manifest, addressed by its digest.** Where every other archivefs
protocol addresses a *live record* (`bia://S-BIAD3080`) and can take a manifest as an
*overlay* (`-o chiro=sidecar.json`), `chiro://` turns that inside out: the manifest is
the URL, named by the digest of its own bytes (chirograph's spec §13).

```
chiro://md5:15576e…                    # the whole pinned deposit
chiro://md5:15576e…/images/a.tif       # one file in it
chiro://./some/loose.chiro.json        # a path, for an ad-hoc manifest outside a store
```

This is the form a `.dvc` wants: a single, portable, content-addressed pin that carries
its own checksums and needs no second artefact alongside the URL.

## Where the digest resolves

`chiro://md5:<digest>` says *what*, never *where*. Resolution
([`archivefs.store`](../src/archivefs/store.py)) searches an **ordered list of stores**,
first hit wins — today one entry, the repo-local `.chirograph/` (found by walking up for
`.chirograph`/`.git`/`.dvc`, so it resolves the same from any subdirectory). The list is
the seam a future DNS-style lookup (a user-global store, a shared registry, a hosted URL)
grows into, with no committed `.dvc` changing. The found file's bytes are checked against
the digest — a corrupted or mis-named manifest is refused, the guarantee a digest name
promises.

## What it resolves to

A `chiro://` URL is **not** a raw fetch of each file's `source_url`. It:

1. resolves the digest to a manifest (above), and verifies its bytes;
2. reads its **`concept`** CURIE — `geo:GSE176078`, `biostudies:S-BIAD3080`,
   `doi:10.5281/zenodo.…`, `ega:EGAD…` — and maps the scheme back to the backend that
   owns those bytes;
3. lists that backend in the manifest's frame and reads through it.

So the actual byte fetch is a normal `geo`/`bia`/`ega`/`zenodo` read, with that
backend's authentication, verify-on-read ([`decisions.md`](decisions.md) D34), resumable
block cache (D29) and ZIP-safety all intact. A raw HTTP fetch of `source_url` would
bypass every one of those — and so would silently fail for exactly the controlled-access
archives (EGA, unreleased BIA) that make a manifest worth having. `chiro://` is a pure
*addressing* layer; the owning backend does the work. This is D36.

If the manifest is a **container** manifest (schema 2.0 — a ZIP deposit, with a
`containers` block; [`decisions.md`](decisions.md) D41), the delegate is told to **expand**
the container (`extract_zips` on Zenodo), so its native listing produces the same
per-member paths the manifest's `source_path`s reference. Without that the ZIP lists as one
opaque file and no member matches. A member read is then verified against the manifest's
own `md5` — the archive publishes none for a ZIP member.

The fetch routes to the **version**, not the concept ([`decisions.md`](decisions.md) D38).
The scheme (`doi:`, `geo:`, …) picks the backend, but the *accession* is the manifest's
`record_accession` — the version-pinned identity. This only matters for Zenodo, where the
concept DOI resolves to *latest*: a pin built from it records the version recid it resolved
to, so `chiro://` fetches those exact bytes forever, while `archivefs update` uses the
concept to check for a newer one. For GEO/BIA/EGA the two coincide.

## Making one

```console
$ archivefs manifest bia://S-BIAD3080/images/ihc
.chirograph/manifests/ac/3a5a….chiro.json: 18 file(s), 397.1 GiB, chiro://md5:ac3a5a… (observed-download)
```

`archivefs manifest` writes into the store and records the `concept` for you
([`cli-manifest.md`](cli-manifest.md)), which is what makes the manifest routable. Commit
the `.chirograph/` store, then address it:

```console
$ archivefs ls chiro://md5:ac3a5a…
$ archivefs get chiro://md5:ac3a5a…/P01_ihc.tif ./a.tif        # verified via BIA
$ dvc import-url chiro://md5:ac3a5a… data/images/ihc           # a self-verifying pin
```

## Requirements and limits

* Needs `chirograph` (`pip install 'archivefs[chiro]'`) — it parses the manifest.
* Needs the **owning backend installed and reachable**: a `chiro://` pin whose concept
  routes to `ega://` needs EGA credentials, exactly as a direct `ega://` read would. If
  the concept scheme is unknown, or the manifest carries no concept (a pre-1.1 manifest),
  the URL fails with a message saying so rather than falling back to an unverified fetch.
* **Not version-aware** in DVC: the pin is a committed, immutable manifest, so a completed
  `dvc import-url` records `outs.md5` like any normal import — there is no mutable source
  to re-resolve. To *move* a pin as the upstream changes, that is `archivefs update`'s job
  (design in `archivefs-update-design.md`), not `dvc update`.

## See also

[`cli-manifest.md`](cli-manifest.md) (write one) ·
[`concepts-and-versions.md`](concepts-and-versions.md) (why a manifest is a *version*) ·
[`decisions.md`](decisions.md) D36 (route-through-backend)
