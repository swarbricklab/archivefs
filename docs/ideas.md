# Ideas and extensions

Things worth doing that are **not** in scope of the current plan, collected while
implementing it. Ordered by (my guess at) value per unit of effort, not by size.
Nothing here is committed to; the point is that the reasoning is written down while
it is fresh.

Companion documents: [`decisions.md`](decisions.md) (what I decided and why),
[`plan.md`](plan.md) (the checklist), [`design.md`](design.md) (architecture).

---

## Do these next, probably

### 0. Ask DVC for a re-fetchable-source flag, and send the PR
**Effort: a day for the request plus a PR. Value: this is the project's core promise.**

[`decisions.md` D17](decisions.md) measures the gap. A *completed* `dvc import-url`
records `outs.md5`, and DVC then refuses to treat the source URL as a place the bytes
could come from — `_load_storage_from_import` registers the source as a fallback remote
only when `not out.hash_info`. So the author who downloads the data and tracks its
checksum, which is the normal and desirable thing to do, produces a repo that **no
reviewer can pull from the archive**. They need a DVC remote hosting a second copy of
already-published data, which is precisely what archivefs exists to avoid.

The workaround (`--no-download` then `dvc pull`) works and is now documented, but it is
a shape you have to know about, and it does not survive someone doing the obvious thing.

What to ask for: a way for a filesystem or a dependency to declare *"this source is
immutable and re-fetchable; use it as a fallback storage even for a completed import"*.
`version_aware` is not it — it was tried (D17): it does not restore the fallback, and it
sets `can_push = False`, breaking the DVC-remote route as well. Either a new
`FileSystem` attribute (`immutable_source`?) alongside `version_aware` in that
disjunct, or a per-stage `pull_from_source: true`.

Natural companion to the storage-backend plugin hook ([D4](decisions.md)) — both are
"DVC needs one small seam for third-party filesystems", and both are better solved
upstream than worked around here. Pitch them together.

### 0b. Authenticated reads for BioStudies, so a submitter can import their own unreleased study
**Effort: unknown until the mechanism is confirmed — a day if it is a header. Value:
high; it is the first thing a depositor tries.**

`bia.py` declares `credentials = []`: archivefs reads BioStudies anonymously, so it can
only see released studies. But a **registered-but-unreleased** study is exactly what a
depositor has in hand while writing the paper, and BioStudies does let them read it. So
the natural first test — "can the person who deposited it import it?" — cannot even be
attempted. Measured on `S-BIAD3080`:

```console
$ curl -s -o /dev/null -w '%{http_code}\n' \
    https://www.ebi.ac.uk/biostudies/api/v1/studies/S-BIAD3080/info
403                                   # {"errorMessage":"Study not accessible"}
$ … same endpoint for S-BIAD599 → 200
```

Note the landing page returns **200** regardless — it is a JS shell that still contains
`{{releaseDate}}` placeholders — so the browser gives no signal. Only the API does.

**What is not yet established**, and should be before writing code:

* The public host exposes `POST /biostudies/login` (405 for a JSON body, so it exists
  and wants something else) but not `/auth/login`, `/api/v1/auth/login` or
  `/api/auth/login` (all 404).
* Both `?key=<secret>` and an `X-Session-Token:` header return **403** with a bogus
  value, which distinguishes nothing: unsupported and wrong-value look identical.
* `ebi-ait/biostudies-client` does login → session id, but its README names neither the
  endpoint nor the header. Reading its source, or asking biostudies@ebi.ac.uk, settles
  it in one message.

Two candidate mechanisms, and they want different designs. A **secret-link key** is a
URL parameter, so it is a *path* not a secret in our terms — but it grants read access,
so treating it as non-secret would be wrong; it should be a `secret=True` credential
resolved from env/file like any other (D16). A **session token** obtained from a
username/password login needs a refresh path, which the EGA backend already models.

Until then the 403 message says so explicitly rather than blaming credentials, which is
what it used to do — see the note in `base.py::_auth_remediation`.

### 0c. Address records by DOI, not just accession
**Effort: a day for per-backend prefixes; a week for a real `doi://`. Value: the DOI is
the identifier papers actually cite.**

`zenodo://` already accepts a DOI (`zenodo://10.5281/zenodo.1486184`), so there is
precedent. Two quite different shapes:

**(a) Per-backend DOI acceptance.** Teach `bia://` to accept `bia://10.6019/S-BIAD3080`
by recognising the prefix and taking the suffix as the accession — for BioImage Archive
DOIs the suffix *is* the accession, verified: `https://doi.org/10.6019/S-BIAD3080`
302s to `https://www.ebi.ac.uk/biostudies/studies/S-BIAD3080`. Cheap, but it only helps
someone who already knows the record is in BIA, and if they know that they have the
accession. Low value on its own.

**(b) A `doi://` protocol that dispatches.** `doi://10.6019/S-BIAD3080` resolves the DOI
and routes to whichever backend owns it. This is the version worth building: paste the
DOI from a paper and it works, and the committed `.dvc` records the most durable
identifier there is rather than a repository-local accession.

Three things to settle first, one of which is a real obstacle:

1. **Resolution is a third network dependency.** `doi.org` being slow or down would fail
   a `dvc pull` for a reason unrelated to the archive. Wants caching, and the resolved
   target recorded so a later pull need not re-resolve.
2. **Registration agencies differ.** `10.6019/S-BIAD3080` has **no DataCite record** —
   content negotiation shows it is registered with **Crossref** (`publisher: EMBL-EBI`,
   `type: dataset`), while Zenodo's are DataCite. So "read the repository out of the
   DOI's metadata" needs at least two agencies, or a fallback that pattern-matches the
   redirect target. The redirect is the more robust signal and needs no API key.
3. **What goes in the `.dvc`.** If `doi://…` resolves to `bia://S-BIAD3080`, does `path`
   keep the DOI (durable, opaque, needs the network to interpret) or the resolved
   accession (transparent, resolvable offline)? Recording the DOI in `path` and the
   resolved target in the backend metadata block gives both, but interacts with
   `dvc update` the way D6's concept-DOI problem does: a *concept* DOI legitimately
   moves, and "update" then means something the user must have chosen deliberately.

Worth doing (b) and skipping (a): a half-measure that only works when you already know
the answer is not worth the surface area.

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

### 17. Audit *history* for secrets that were committed before the policy landed
This idea originally proposed linting new commits, because
`dvc import-url --fs-config password=… -- ega://…` wrote the password straight into
the `.dvc` file and archivefs could not refuse it: `fs_config` and
`.dvc/config.local` arrived as the same `storage_options` dict, so the distinction
only existed in the file DVC wrote.

That premise is gone. Credential keys are absent from the schema `dvcfs.register()`
hands DVC, so `--fs-config password=…` and `dvc remote modify … password …` now fail
in DVC's own validator, and the resolver refuses an option-supplied credential
outright (decisions.md D16). A new commit cannot contain one.

What remains is the past: repositories configured before the policy, and secrets
already in git history, where the fix is to **rotate** rather than to edit. So
`archivefs check-config [<path>…]` is still worth building, but scoped to auditing —
walk `*.dvc` and `.dvc/config` across `git log`, pull `deps[].fs_config`, report any
key the resolver declares `secret=True`, and say "rotate this" rather than "remove
this". `CredentialResolver.committed_secrets` remains for exactly that caller. Pairs
with idea #10 (`archivefs doctor`) — same "tell me what is wrong with my setup"
shape.

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
