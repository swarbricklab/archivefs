# How the archives measure up

What each archive can promise, along the axes that decide whether a `.dvc` is
reproducible, pullable, and trustworthy. The model behind the columns is
[`concepts-and-versions.md`](concepts-and-versions.md); this is the table.

Two columns are declared once, in `registry.Backend`, and everything else is derived
or observed ([`decisions.md`](decisions.md) D22): **published checksum**
(`published_checksum`) and **immutable token** (`immutability_basis`, which gates DVC
`version_aware`). The rest is measured behaviour of the live services.

---

## The matrix

| capability | `zenodo` | `mendeley` | `ega` | `bia` submitted | `bia` derived | `geo` |
|---|---|---|---|---|---|---|
| **Published per-file checksum** (download-free) | ✅ md5 | ✅ sha256⁵ | ✅ md5¹ | ⚠️ only a depositor md5 column | ✅ md5 (S3 ETag) | ❌ |
| **Immutable version token** (`version_aware`) | ✅ | ✅ | ✅ | ❌ | ❌² | ❌ |
| **Source is immutable** (a name that can't serve other bytes) | ✅ version DOI | ✅ version DOI | ✅ stable accession | ❌ record mutates in place | ❌ regenerated | ❌ record mutates in place |
| **Version-addressable retrieval** (fetch a *past* snapshot) | ✅ | ✅ | ⚠️ latest only³ | ❌ | ❌ | ❌ |
| **Native concept reference** (track-latest) | ✅ concept DOI | ✅ concept DOI | ✅ accession | ✅ accession | ✅ | ✅ accession |
| **Native version reference** (a frozen pin) | ✅ version DOI | ✅ version DOI | ⚠️ via md5³ | ❌ | ❌ | ❌ |
| **Safe DVC pull-through** (fallback from the archive) | ✅ | ✅ | ✅ | ❌⁴ | ❌ | ❌⁴ |
| **Verify-on-read possible** (a checksum to enforce) | ✅ | ✅ | ✅ | ⚠️ if column present | ✅ | ❌ |
| **Needs Chirograph for a reproducible pin** | no | no | no | **yes** | **yes** | **yes** |

¹ md5 of the **decrypted** bytes — the ones that land on disk (D1).
² the derived view *has* md5s, but the profile declares `immutability_basis=None`
   conservatively because the *submitted* view has none and a profile cannot say
   "sometimes" (D-in-registry comment).
³ EGA identifies bytes by a stable accession + md5, not by a versioned URL; the md5 is
   the token, so it behaves as a version reference in practice, but you cannot ask for
   "the previous bytes" — there is only one.
⁴ the partial-import route (`--no-download`, don't `dvc update`) *works* mechanically,
   but with no integrity gate on the fetch (D34); it is not *safe* against a revised
   record without a Chirograph pin.
⁵ **sha256, not md5** — and `mendeley://` is EXPERIMENTAL (D44). Mendeley exposes no documented
   third-party file API (the official one is owner-scoped), so the backend is built on the site's
   *undocumented, anonymous* frontend API; it may break if Mendeley changes it. sha256 is the
   immutable per-entry token (version-aware pins and pull-through both verified live), but it is not
   an md5, so a *download-free* `--no-download` pin is impossible — DVC hashes the downloaded bytes
   to md5 itself. Reads need a browser-like User-Agent (Cloudflare blocks the default).

---

## The one line that explains the table

**Version-awareness is safe exactly when the source is immutable.** Zenodo (a version
DOI) and EGA (a stable, controlled-access accession) name bytes that *cannot* change
under the reference, so DVC's trust-the-source fetch (`verify=False`, D34) is sound and
pull-through is safe. BIA-submitted and GEO name records that **mutate in place** — the
same accession can serve different bytes next month — so treating them as version
references would let a `dvc pull` silently return the wrong data. That is why their
`immutability_basis` is `None`, and why the only way to pin them is to introduce an
external immutable object: a Chirograph manifest.

Everything else in the table is a consequence of that row.

---

## Per-archive notes

**`zenodo`** — the reference case. Publishes an md5 per file, and distinguishes a
concept DOI (latest) from a version DOI (frozen), so it supplies *both* halves of the
model natively. `dvc update` on a concept DOI tracks latest; on a version DOI it is a
no-op. Expands ZIP members as directories (`extract_zips`). Nothing here needs
Chirograph.

**`mendeley`** — Mendeley Data, **EXPERIMENTAL** (D44). A versioned, DOI-bearing record, like
Zenodo in shape — but Mendeley exposes no documented third-party file API (the official one lists
only datasets *you own*). So this backend is built on the site's **undocumented, anonymous**
frontend API (folders + per-folder files), which may break if Mendeley changes it — hence a branch
prototype, not merged. Checksums are **sha256, not md5**: enough for version-aware pins and
pull-through (both verified live against `dvc`), but no download-free `--no-download` pin. Concept
scheme is `mendeley:` (not `doi:`, which Zenodo owns). Files live in folders, so a member path is
`<folder>/<file>`. Needs a browser-like User-Agent (Cloudflare blocks the default client).

**`ega`** — controlled access (credentials + DAC authorisation). Publishes the md5 of
the decrypted bytes and is version-aware, so completed imports are pullable and
verifiable. Accessions are stable, so this is safe; but there is no "previous version"
to retrieve — an EGA file is one byte-string, identified by its md5.

**`bia` submitted** — the depositor's uploaded files. **No native checksum**, a
**mutable** accession, **no versioned retrieval**. A depositor *can* publish an md5
column (D33), which gives `archivefs get` something to verify against and makes
`--no-download` honest — but it is not an archive capability (`published_checksum`
stays `None`), it does not make the record immutable, and it does not give
version-addressable retrieval. For a *reproducible, drift-detecting* pin, this view
needs a Chirograph manifest. Unreleased studies are readable with a sharing key
([`reviewer-access.md`](reviewer-access.md)); a released study is **not** frozen — it
can still be revised.

**`bia` derived** — BIA's own OME-Zarr pyramids and thumbnails, in an anonymous S3
bucket, with real single-part md5 ETags. Verifiable and download-free, but these are
**not the submitted bytes** (a conversion), and they are not version-addressable. Good
for streaming/visualisation; not a substitute for the deposit.

**`geo`** — supplementary/processed files over public HTTPS. The archetype of the
problem: no checksums, and records that mutate under a stable accession. Chirograph is
not optional here if you want either verification or reproducibility.

**`dbgap`** — deferred; the backend refuses with an explanation rather than pretending.

**`cbioportal`** — cBioPortal studies, **EXPERIMENTAL** (D44). The datahub's S3 tarball is a
dead end (whole-study `.tar.gz`, no per-file URLs, no checksums, 403 from our network), but the
datahub is *also* the `cBioPortal/datahub` GitHub repo, whose large **data files are git-lfs**
tracked — so an LFS pointer gives a **published, download-free sha256** for those (verified on a
689 MB matrix). The non-LFS files (`meta_*`, case lists, licence) carry no published digest, so
their sha256 is **derived** by downloading and hashing them (small, but a study listing therefore is
*not* wholly download-free — for `brca_metabric`, 19 of 27 files). Every member still gets a sha256
(`info().checksum_source` says which kind); content streams from `media.githubusercontent.com` with
ranges, on documented GitHub infrastructure. Same sha256 (not md5) story as `mendeley`: version-aware pins and
pull-through work (verified live), no download-free `--no-download` md5. A study is versioned by the
datahub's git history: `cbioportal://<study>@<ref>` pins a release tag (`@v1.0.0`), a commit SHA or
a branch (default `master`), and the pin records the resolved immutable commit while the per-file
sha256 pins the exact bytes. Needs a User-Agent (GitHub 403s without); `$GITHUB_TOKEN` is optional
(it only raises the API rate limit).

**`chiro`** — not a source archive but the *pin* over one: a committed `.chiro` manifest
addressed directly ([`chiro-protocol.md`](chiro-protocol.md)). It has no row of its own
because it inherits its delegate's — a `chiro://` URL whose concept is `geo:…` reads the
GEO row, but now *with* a checksum and a reproducible pin, which is the whole point. It is
the "yes, needs Chirograph" cell made concrete.

---

## Reading a column back to a decision

- **"Needs Chirograph for a reproducible pin: yes"** ⇒ the archive is concept-only, so
  a `.dvc` that points straight at it is track-latest, not a pin. See
  [`concepts-and-versions.md`](concepts-and-versions.md) §5 and
  [`importing-from-bia.md`](importing-from-bia.md).
- **"Safe DVC pull-through: ✅"** ⇒ a completed `dvc import-url` is pullable from the
  archive by a fresh clone, because the source is immutable (D23 for the mechanism).
- **"Verify-on-read possible: ⚠️/❌"** ⇒ the fetch cannot be checked against a
  published digest, so integrity has to come from a Chirograph manifest instead (D34).
