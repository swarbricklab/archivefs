# Concepts and versions: what an archive URL can promise

Every problem archivefs meets at the boundary with DVC — is a `.dvc` reproducible?
can a reviewer pull it? does `dvc update` mean anything? can a fetch be trusted? —
turns on a single distinction that most archives blur and a few make sharply. This
document names it, grounds it in the ontology it comes from, and says which archives
sit where. The companion [`archive-capabilities.md`](archive-capabilities.md) is the
per-archive table; this is the model behind it.

The terminology is deliberately shared with the sibling **Chirograph** project, whose
[`resolution-and-config.md`](https://github.com/swarbricklab/chirograph) draws the same
line for manifests. archivefs is the consumer; Chirograph is the layer that supplies a
*version* where an archive offers only a *concept*.

---

## 1. Two kinds of reference

**A concept reference** names a *dataset* — "this record, whatever it is now." It is
**mutable by design**: resolving it means "give me the current state," and the answer
changes as the depositor revises the record. A bare accession is a concept reference.

```
bia://S-BIAD3080            geo://GSE176078            zenodo://10.5281/zenodo.1486183   (concept DOI)
```

**A version reference** names an *exact, immutable snapshot* — one byte-string per
file, frozen. Resolving it twice yields the same bytes or fails. It is what makes a
`.dvc` reproducible and a pull trustworthy.

```
zenodo://10.5281/zenodo.1486184   (version DOI)        chiro://md5:6f174b…   (a Chirograph manifest)
```

The two are not interchangeable and **conflating them is the core hazard**: a `.dvc`
that records a concept reference but is treated as if it were pinned *looks*
reproducible and isn't. Where an archive offers both (Zenodo), archivefs keeps them
distinct and warns when a concept resolves to a version
([`decisions.md`](decisions.md) D6). Where it offers only a concept (BIA, GEO), the
version has to be manufactured — that is Chirograph's job.

### The ontology this comes from

This is not an archivefs invention; it is a distinction with several names, and it
helps to see them lined up:

| domain | the mutable thing (concept) | the immutable thing (version) |
|---|---|---|
| **Zenodo DOIs** | concept DOI ("latest") | version DOI (frozen) |
| **FRBR** (library science) | *Work* | *Expression / Manifestation* |
| **Git** | branch (a moving ref) | commit (content-addressed, immutable) |
| **Content addressing** | a name that is looked up | a name that *is* a hash — the proof |

The Git row is the most useful intuition: **you never *update* a commit; you create a
new one and move a branch.** A version reference is a commit; a concept reference is a
branch. Everything downstream — how `dvc update` behaves, whether pull-through is
safe — follows from that, because a content-addressed name cannot both be immutable
*and* track latest. (Chirograph adds what Git has and Zenodo half-has: the version
name is a **digest**, so it is a *proof* rather than a *promise*.)

---

## 2. Three versions, because there are three layers

"Version" is itself ambiguous once a manifest is in play. Kept apart:

- **Record version** — the archive's own notion of "the record changed": a Zenodo
  version DOI, or, where nothing better exists, the record's mtime. Cheap to check,
  **advisory and over-approximate** (a metadata-only edit bumps it without touching a
  byte you imported). A *trigger*, never a *token* — an mtime used as an integrity
  token silently accepts wrong bytes (measured; [`decisions.md`](decisions.md) D34).
- **Manifest version** — a Chirograph manifest's `body_hash`. The authoritative,
  self-verifying identity of a *pinned file-set*. Only exists when there is a manifest.
- **File version** — a single file's identity: its published md5 (Zenodo, EGA, a BIA
  md5 column) or, failing that, a per-file mtime/etag. The finest grain, and the thing
  a fetch is *verified* against.

The cheap-but-noisy record version **gates** the expensive-but-true manifest/file
version: check mtime first, and only re-hash when it moved.

---

## 3. What this means at the DVC boundary

Two archives, two opposite-but-correct behaviours — this is the crux:

| | **concept reference** (BIA, GEO, Zenodo concept DOI) | **version reference** (Zenodo version DOI, `chiro://`) |
|---|---|---|
| model | **track-latest** | **pin-and-notify** |
| `dvc update` | **native** — re-resolve, re-pin to current | **no-op** — the pin is immutable; there is nothing to move |
| reproducible? | no — you get *current*, not a frozen snapshot | yes |
| pull-through | needs a hashless out (partial import) or version-awareness | inherent (the source is immutable) |
| drift from your commit | **undetectable** — nothing enforces the recorded hash on the fetch | detectable — the pin is enforced |

`dvc update` is not "broken" on either. On a concept reference it correctly follows
latest; on a version reference it correctly does nothing. The trap is only in
expecting one from the other. For a Chirograph version reference, the *supersede*
operation lives in a separate verb — **`archivefs update`** — because moving a
content-addressed pin means making a new manifest and repointing, which DVC's in-place
model cannot express. It is archivefs's verb, not Chirograph's, because it reads the
live archive twice (the current record version, then the new checksums) and that I/O is
archive-specific — Chirograph stays network-free and supplies only local manifest
primitives. Its read-only companion is **`archivefs check`** (which today already
reports a manifest's drift from the current record). See Chirograph's
`updatability-and-dvc-update.md`.

Two fields carry the tracking, and they are *not* the same thing: **`concept`** is the
mutable tracker (a CURIE like `doi:10.5281/zenodo.7654320` that re-resolves to latest),
while **`source_url`** is the version-specific fetch base (the exact bytes can be
re-fetched from it). For BIA and GEO they coincide — the accession is both — but for
Zenodo they must not: the concept DOI tracks, the version DOI fetches.

---

## 4. Verification is the consumer's job, because DVC's fetch does not do it

A hard fact, measured ([`decisions.md`](decisions.md) D34): **DVC fetches an import
with `verify = False`.** It records a checksum but does not re-hash the fetched bytes
against it. So a source that serves different bytes than the `.dvc` records is accepted
**silently** — safe for an immutable version reference (the source *cannot* serve
other bytes), unsafe for a concept reference (it can, and does, when the record is
revised).

So the checksum an archive publishes is only worth what the *consumer* enforces:

- **`archivefs get`** streams, hashes, and **refuses on mismatch** against the
  published md5. ([`cli-get.md`](cli-get.md).)
- **`get_file` and `cat_file`** — the library/DVC read paths — now do the same on any
  whole read: they re-hash the bytes against the published md5 and refuse (and, for
  `get_file`, delete) on mismatch. So a `dvc import-url`/`dvc pull` that would have
  silently materialised corrupted bytes now fails loudly. A ranged read has no
  whole-file md5 to check and is served, not refused. (D34.)

Even so, verify-on-read against the archive's *current* md5 catches
**corruption**, not **drift from your commit** — because the filesystem only ever knows
current state, not your `.dvc`'s recorded value. Detecting drift needs a pin that
*travels with the `.dvc` and is enforced on the fetch* — which is a committed
Chirograph manifest, and nothing a bare concept URL can provide.

---

## 5. So: which reference should you use?

- **A concept URL (`bia://`, `geo://`, a Zenodo concept DOI)** is the honest choice
  when you want the *live* record and are willing to re-pin as it moves. Treat it as
  track-latest: `dvc update` follows it, `archivefs get` verifies your copy, and you
  accept that it is not a reproducible, drift-detecting pin.
- **A Zenodo version DOI** gives you a real pin for free, because Zenodo keeps old
  versions addressable.
- **A `chiro://` manifest** is how you get a pin for an archive that offers none —
  reproducibility *and* drift-detection for BIA, GEO, or any mutable, checksum-less
  source. This is why "all roads lead to Chirograph": for a concept-only archive it is
  the *only* thing that can supply the version half. Make one with
  [`archivefs manifest`](cli-manifest.md): download-free where the archive publishes
  checksums, `--download` where it does not.

See [`archive-capabilities.md`](archive-capabilities.md) for exactly what each archive
can and cannot promise.
