# Ideas and extensions

Things worth doing that are **not** in scope of the current plan, collected while
implementing it. Ordered by (my guess at) value per unit of effort, not by size.
Nothing here is committed to; the point is that the reasoning is written down while
it is fresh.

Companion documents: [`decisions.md`](decisions.md) (what I decided and why),
[`plan.md`](plan.md) (the checklist), [`design.md`](design.md) (architecture).

---

## Do these next, probably

### 1. Report the EGA range-alignment bug upstream
**Effort: an hour. Value: high, and not just to us.**

[`decisions.md` D3](decisions.md) documents it: a ranged read of
`?destinationFormat=plain` with a start that is not a multiple of 16 returns `206`,
the right length and the wrong bytes, silently. Anyone building a random-access
client against EGA will hit this and may not notice. The table in D3 plus
`scripts/ega_checksum_check.py` is most of a bug report; `helpdesk@ega-archive.org`
is the address, and the design doc already notes their response times, so file it
early. Worth also asking whether the alignment is guaranteed to stay 16 — we hard-code
that constant.

### 2. Re-run the checksum check against one *real* authorised EGA file
**Effort: one command. Value: closes the last gap in D1.**

D1 was established on EGA's synthetic test dataset (all that is reachable without a
DAC approval). The transfer mechanism is identical for controlled data, but the claim
"the metadata checksum describes the bytes the user ends up with" is load-bearing for
every EGA import we will ever do. Run it once on a real file before the first
production import:

```
python scripts/ega_checksum_check.py --file-id EGAF… --connections 4 --simulate-interrupt
```

### 3. Propose a filesystem-plugin entry point to DVC
**Effort: a small PR plus patience. Value: removes our worst wart.**

The `.pth` start-up hook ([D4](decisions.md)) exists only because DVC has no plugin
mechanism for storage backends: `dvc_objects.fs.known_implementations` is a literal
dict and `dvc.config_schema.REMOTE_SCHEMAS` is snapshotted at import. DVC already
consumes entry points elsewhere and *publishes* one for fsspec, so a `dvc.fs` group
would be idiomatic and would benefit every out-of-tree backend, not only us. If it
lands, `archivefs dvc enable` becomes unnecessary and the hook can be deleted.

Worth pairing with a second, smaller report: `dvc_objects.fs.Registry.__getitem__`
falls back to `LocalFileSystem` for an unknown protocol instead of raising, which
turns a typo into a silent local-path read.

### 4. Zenodo version-aware imports (concept DOI → pinned version)
**Effort: a few days. Value: the strongest provenance story available anywhere.**

Zenodo is the one archive where DVC's version-awareness would be *honest*: a version
DOI genuinely identifies a distinct, immutable record. Today
([D6](decisions.md)) we sidestep it — the token lives under `archive_version` and the
DVC wrapper forces `version_aware = False` — which means importing a **concept** DOI
does not record which version was taken.

Done properly: implement `coalesce_version`/`version_path` so
`zenodo://10.5281/zenodo.1486183` (concept) resolves to a version, records it, and
`dvc update` moves it forward deliberately and visibly. The design doc's
"`dvc update` against a concept DOI re-resolves and re-extracts" is exactly this.
Needs care around what `dvc update` should do when a *new* version has different
files.

### 5. A `chirograph`-writing path for our own deposits, driven from archivefs
**Effort: small (chirograph does the work). Value: closes the GEO checksum gap for
our own data.**

`archivefs.chiro` currently only *reads* manifests. The complement is: after
downloading a record that has no manifest, emit an `observed-download` manifest
recording what we got — the design doc calls this out ("capture an
`observed-download` `.chiro` on first fetch"). chirograph already has the writer;
this is one CLI command (`archivefs observe geo://GSE… -o GSE….chiro.json`) plus
plumbing. It makes any third-party record reproducible-by-reference even when the
archive publishes no digest, which is precisely GEO's weakness.

---

## Worth building when someone needs them

### 6. Sample-sheet-driven selection as a first-class thing
The design's "selection-driven virtual `ls()`" is implemented for EGA
(`selection=[…]`), but the ergonomic version is a **file**: point the backend at a
TSV/CSV sample sheet and let it resolve accessions, so the `.dvc` documents the
subset in terms the study uses ("these 12 samples") rather than a list of EGAFs. Two
extras worth having: fail loudly when a sheet names something the dataset does not
contain (silent partial imports are how people lose samples), and record the sheet's
own md5 in the import so "which subset was this?" stays answerable.

### 7. `bia://` — BioImage Archive · **access model now verified**
The design gated this on "verify storage/access model first". Done, against the live
services: [`evidence/bia-access-model-2026-08-17.md`](evidence/bia-access-model-2026-08-17.md).

Summary: it is the **easiest remaining backend**. One anonymous BioStudies API call
(`/api/v1/files/{accession}`) returns a full recursive listing with exact sizes *and*
per-file experimental metadata — better than GEO, which needs HTML parsing plus a
`HEAD` per file. Payload is range-readable HTTPS over FIRE, sharded by the
accession's last three digits exactly like GEO's `GSE176nnn`.

Two things worth knowing before building it:

* **No checksums on submitted files** — same gap as GEO, same fix (a sidecar
  `.chiro` in our own deposits, which is the case that matters since BIA is a
  repository we deposit *into*).
* **The "just use `s3://`" hypothesis is wrong.** There *is* an anonymously listable
  Embassy S3 bucket, and its ETags really are plain md5s — but it holds *derived*
  data (thumbnails, OME-NGFF conversions) keyed by uuid, not the submitted files.
  So accession→path resolution is the actual work.

Those two tiers are precisely the **view registry** built for GEO's SRA seam:
`view="submitted"` and `view="ngff"`, one class each. The imminent Visium/Xenium
deposit is the motivating case.

### 8. Tar transparency for GEO, cautiously
GEO's convention is one `GSE…_RAW.tar` containing per-sample archives, and
`filelist.txt` gives its interior listing *for free* — we already record it as
`info()["geo"]["contains"]`. So a "list what is inside" view is nearly done. What
does **not** work is presenting it as a directory the way we do for ZIP: tar has no
central directory, so member offsets require scanning, and `.tar.gz` cannot be ranged
into at all. A defensible middle path: allow `tar://` chaining for *uncompressed*
tars only (one scan, cached offsets), and refuse `.tar.gz` with an explanation.
Do it only when someone actually wants per-sample imports from a RAW tar.

### 9. Parallel range fetching in the base
Every backend currently reads ranges serially. EGA gave ~425 KB/s on one connection
and ~1 MB/s on four, so a multi-GB dataset import is currently slow. The base's
`_fetch_checked` is the single place to add a thread pool. Two cautions from
measurement: EGA returned a short/empty body under 25 MB slices at 4 connections
(hence the length checks), and any concurrency here must respect the 16-byte
alignment rule. Moderate slices (1–8 MB) behaved well.

### 10. `archivefs doctor <url>`
`info` reports the environment and `check` compares against a manifest; the missing
one is "diagnose this URL end to end": can we resolve credentials, does the accession
exist, is the record open/embargoed, does a one-byte ranged read work, does the
archive publish a checksum, is a manifest present, how big is the whole thing. Every
one of those is already implemented; it is a composition and a nice error-message
surface. This is the command that turns "my pull failed" into a one-line answer.

---

## Speculative, but I would enjoy being wrong about the value

### 11. A verification-report artifact for papers
We can already produce, download-free, a per-file table of "what the archive claims"
and after a pull "what we got". A tiny renderer over that — a signed-off
`data-provenance.md` listing accession, version token, per-file md5, retrieval
timestamp and verification outcome — is the thing reviewers actually want in a
supplementary methods section, and nobody has it. Chirograph's `VerificationReport`
already carries the content.

### 12. Read-through caching keyed by content hash
Because `info()` gives a content md5 *before* any bytes move, archivefs could cache
by digest rather than by URL: two records containing the same file (common for
reference data) share one cached object, and a re-import of a re-published record is
free if the bytes did not change. fsspec has `filecache`/`blockcache` but they key on
path. On shared HPC this is worth real disk: one lab-wide cache under
`/g/data/…/archivefs-cache` would deduplicate across every project. Interacts
usefully with DVC's own content-addressed cache — possibly the right move is to
*write into* the DVC cache directly and let DVC take it from there.

### 13. Treat the EGA authorised-dataset list as a discovery surface
`ega://` with no path already lists the datasets an account can see, which turned out
to be genuinely useful (`dvc list-url ega://`). One step further: a
`archivefs ls ega:// --long` that also shows file counts and total sizes would answer
"what do I actually have access to, and how big is it" — a question that currently
requires the web portal and some patience. Cheap: it is one metadata call per
dataset.

### 14. Cross-archive record linking
The deposit-side SOP splits one study across archives by sensitivity and modality
(raw → EGA, matrices → GEO/Zenodo, imaging → BIA) and cross-links the records. If
those cross-links are machine-readable, `archivefs related zenodo://…` could walk
them, and a single "import this study" operation becomes possible. The Chirograph
registry design is the natural home for the link data; the archivefs side is small.
Worth revisiting once the imminent multi-part deposit exists as a concrete example.

---

## Things I deliberately did **not** do

* **No SRA orchestration.** The GEO view registry makes it a one-class addition
  ([`decisions.md` D12](decisions.md)), and a test proves an external view slots in.
  Building it now would be speculative work on the one path our data model excludes.
* **No `[ega]` extra / pyega3 delegation.** See [D2](decisions.md). Reversible if
  someone wants pyega3's transfer tuning.
* **No second implementation of the `.chiro` format.** See [D5](decisions.md) — the
  spec's own warning about near-identical canonicalisation is the argument.
* **No ZIP library dependency.** See [D9](decisions.md) — the candidates do not cover
  the part that matters (write-side containment), and `zipfile` covers the parsing
  tail.
* **No write/deposit support.** Every filesystem refuses writes explicitly. Deposit
  is a human-supervised, DOI-minting, accession-reserving process; a filesystem
  interface would make it look routine.
