# The `chiro://` protocol — a manifest *is* the address

**A committed `.chiro` manifest, used directly as a URL.** Where every other archivefs
protocol addresses a *live record* (`bia://S-BIAD3080`) and can take a manifest as an
*overlay* (`-o chiro=sidecar.json`), `chiro://` turns that inside out: the manifest is
the URL.

```
chiro://S-BIAD3080.chiro.json                  # the whole pinned deposit
chiro://S-BIAD3080.chiro.json/images/a.tif     # one file in it
chiro:///abs/path/GSE176078.chiro.json         # an absolute manifest path
```

This is the form a `.dvc` wants: a single, portable, content-addressed pin that carries
its own checksums and needs no second artefact alongside the URL.

## What it resolves to

A `chiro://` URL is **not** a raw fetch of each file's `source_url`. It:

1. loads the manifest (the part of the URL up to `.chiro.json`);
2. reads its **`concept`** CURIE — `geo:GSE176078`, `biostudies:S-BIAD3080`,
   `doi:10.5281/zenodo.…`, `ega:EGAD…` — and maps the scheme back to the backend that
   owns those bytes;
3. **overlays the manifest on that backend** and reads through it.

So the actual byte fetch is a normal `geo`/`bia`/`ega`/`zenodo` read, with that
backend's authentication, verify-on-read ([`decisions.md`](decisions.md) D34), resumable
block cache (D29) and ZIP-safety all intact. A raw HTTP fetch of `source_url` would
bypass every one of those — and so would silently fail for exactly the controlled-access
archives (EGA, unreleased BIA) that make a manifest worth having. `chiro://` is a pure
*addressing* layer; the owning backend does the work. This is D36.

The fetch routes to the **version**, not the concept ([`decisions.md`](decisions.md) D38).
The scheme (`doi:`, `geo:`, …) picks the backend, but the *accession* is the manifest's
`record_accession` — the version-pinned identity. This only matters for Zenodo, where the
concept DOI resolves to *latest*: a pin built from it records the version recid it resolved
to, so `chiro://` fetches those exact bytes forever, while `archivefs update` uses the
concept to check for a newer one. For GEO/BIA/EGA the two coincide.

## Making one

```console
$ archivefs manifest bia://S-BIAD3080 --out S-BIAD3080.chiro.json
S-BIAD3080.chiro.json: 412 file(s), 88.1 GiB, chiro://md5:… (observed-download)
```

`archivefs manifest` records the `concept` for you ([`cli-manifest.md`](cli-manifest.md)),
which is what makes the resulting manifest routable. Commit it, then address it:

```console
$ archivefs ls chiro://S-BIAD3080.chiro.json
$ archivefs get chiro://S-BIAD3080.chiro.json/images/a.tif ./a.tif   # verified via BIA
$ dvc import-url chiro://S-BIAD3080.chiro.json data/study            # a self-verifying pin
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
