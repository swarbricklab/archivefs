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

### 7. `bia://` — BioImage Archive · **built**
Implemented on 2026-08-18 after the access-model verification
([`decisions.md` D13](decisions.md)). Both tiers are there: `submitted` (FIRE,
byte-exact, no checksums) and `derived` (OME-Zarr + thumbnails, real MD5 ETags,
listed lazily one S3 directory at a time).

What is *not* done, and is worth doing when the Visium/Xenium deposit lands:

* **Zarr through the derived view.** `bia://…` with `view="derived"` already reads
  chunks lazily, so pointing `zarr`/`ome-zarr-py` at it should work — I have not
  tried it, and the sensible test is opening a real pyramid.
* **Cross-linking the imaging half to the counts half.** BIA is the imaging tier of
  a deposit whose matrices live in GEO/Zenodo; idea #14 is the general version.
* **A study-level `.chiro`** would close the submitted tier's checksum gap the same
  way it does for GEO, and BIA is a repository we deposit *into*, so we control it.

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

### 15. Concurrency, once someone imports something large
Two things I fixed rather than built out, both worth revisiting the first time a
multi-hundred-GB EGA dataset is imported:

* **Listing resolution is serialised** behind one re-entrant lock
  ([`decisions.md` D11](decisions.md)). Correct, and cheap because listings are
  cached per container, but a per-container lock would let unrelated records resolve
  in parallel.
* **Reads are serial** (idea #9), and EGA's own throughput (~425 KB/s on one
  connection, ~1 MB/s on four) means a large dataset takes hours. Any parallel
  fetcher must respect the 16-byte alignment rule and keep the length checks — the
  short-body behaviour that motivated them was *observed under concurrency*.

### 16. Give DVC an honest way to say "no content hash, but here's an etag"
[`decisions.md` D14](decisions.md): where the archive publishes no checksum, DVC's
`--no-download` quietly streams the whole file to compute one. The dep hash it
records is genuinely correct, so this is a cost bug, not a correctness bug — but for
BIA-submitted and bare GEO it makes "no download" mean a full transfer, twice.

DVC already has the concept (`Meta.etag`, used by the S3/HTTP filesystems for
exactly this case: a source fingerprint that detects change without claiming to be
content). What is missing is a way for a *plugin* filesystem to populate it —
`Meta.from_info` reads its own field names, so our `info()` can't supply one. Two
routes: get `etag` recognised from plugin `info()` dicts upstream, or find whether
setting `PARAM_CHECKSUM = "etag"` on just the checksum-less wrappers already works
(untested, and it changes what `dvc update` compares). Worth pairing with idea #3,
since both are "DVC needs one small seam for third-party filesystems".

Until then the sidecar manifest (`-o chiro=…`) is the real answer, and the one the
BIA guide recommends.

### 17. Lint committed `.dvc` files for secrets in `fs_config`
`CredentialResolver.committed_secrets` was written so "no secret in the committed
artifact" could be a *check* rather than a promise — and then nothing ever called it.
`dvc import-url --fs-config password=… -- ega://…` writes the password straight into
the `.dvc` file, and archivefs cannot refuse it at the filesystem layer, because
`fs_config` and `.dvc/config.local` arrive as the same `storage_options` dict. The
distinction only exists in the file DVC wrote.

So the check belongs one level up, reading `.dvc` files rather than options:
`archivefs check-config [<path>…]` walks `*.dvc` (plus `.dvc/config`), pulls
`deps[].fs_config`, and reports any key the resolver declares `secret=True`. A dozen
lines given `committed_secrets` already exists, useful as a pre-commit hook, and it
turns the invariant back into something enforced. Pairs with idea #10
(`archivefs doctor`) — same "tell me what is wrong with my setup" shape.

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
