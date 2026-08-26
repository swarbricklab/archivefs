# Ideas and extensions

Things worth doing that are **not** in scope of the current plan, collected while
implementing it. Ordered by (my guess at) value per unit of effort, not by size.
Nothing here is committed to; the point is that the reasoning is written down while
it is fresh.

Companion documents: [`decisions.md`](decisions.md) (what I decided and why),
[`plan.md`](plan.md) (the checklist), [`design.md`](design.md) (architecture).

---

## Do these next, probably

### 0. ~~Ask DVC for a re-fetchable-source flag~~ — the mechanism already exists
**Effort: none. Value: none. Withdrawn 2026-08-24, and worth reading as a cautionary
tale about diagnosing a gap as upstream.**

The ask was: *"let a filesystem or dependency declare this source is immutable and
re-fetchable; use it as a fallback storage even for a completed import"*. It asserted
that `version_aware` "is not it — it was tried (D17): it does not restore the
fallback".

**`version_aware` is exactly it.** It is the third disjunct in
`_load_storage_from_import`, and it does restore the fallback. The D17 experiment that
seemed to disprove this never emitted a per-entry `version_id`, so DVC's
`_log_unversioned` silently dropped every entry from the fetch afterwards — a second
gate, downstream of the one being tested, producing a symptom indistinguishable from
the first gate not opening. Supply the tokens and a completed import is pullable from
the archive in a fresh clone with no remote. See the corrections in D6, D17 and D20,
and [issue #1](https://github.com/swarbricklab/archivefs/issues/1).

What survives of this entry: `can_push = False` is a real cost of the existing
mechanism, and *if* we wanted the fallback **and** `dvc push`, that would be a genuine
upstream ask — but it is a much smaller one than "there is no way to express this", and
nobody has asked for both at once.

The storage-backend plugin hook (D4) is still a real upstream gap and should be pitched
on its own. This entry existed partly to give it company; it does not need any.

**One genuine upstream report did come out of implementing this, and it is small
enough to send as a patch.** `dvc/repo/index.py`, `_load_storage_from_import`:

```python
if not out.hash_info or dep.fs.version_aware:
    ...
    storage_map.add_cache(FileStorage(key, fs_cache.fs, .../<protocol>/<token>))
```

For a **completed** version-aware import both disjuncts are live, so the stage's cache
storage is pointed at the fs-cache — but a completed import has already written the
bytes to the *object* cache under `files/md5/…`. The two disagree, and `dvc checkout`
on a deleted file fails with `Is your cache up to date?` while the bytes sit in the
cache directory. One `dvc pull` refills the fs-cache and it never recurs.

Measured on both sides, because it matters that this is specific: the partial-import
route (`--no-download` then `dvc pull`) populates the fs-cache directly and checkout
works. Only *completed import plus version-awareness* hits it. The fix is presumably to
register both storages for that case. Pinned by
`test_checkout_after_a_completed_import_needs_one_pull_first`, and written up in D23.

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

### 0d. ~~Let a `.chiro` manifest supply ZIP-member checksums~~ — mostly obviated
**Effort: was "a few days". Value: now low for ZIPs, unchanged for GEO/BIA. Rewritten
2026-08-24.**

This entry asked the right question — how does a ZIP member get something DVC will
fetch against? — and answered it one field too far to the right. It assumed the answer
had to be an **md5**, which forces a manifest, because the only way to *compute* a
member md5 is to stream the container (which the entry itself, correctly, ruled out).

**DVC's `version_id` is not a checksum field, and filling it is sufficient.** So `md5`
stays `None`, `geo.py`'s rule stands, nothing is streamed inside `info()`, and there is
no manifest. `(container md5, member CRC32)` comes from the central directory for free.
Measured working; see [issue #1](https://github.com/swarbricklab/archivefs/issues/1) and
the second correction in [D20](decisions.md).

There is a further reason not to invent a format here: with `version_aware` on, DVC
writes a per-member `files:` list — `relpath`, `md5`, `size`, `version_id` — **inline
into the committed `.dvc`**, git-tracked and travelling with the clone. That is a
manifest, in a format DVC already reads, that we did not have to design.

**What still stands, unchanged:**

* **The motivation.** Zenodo's file namespace is flat (of 5592 keys across 900 records,
  the only 55 containing a `/` are the GitHub-release integration), so
  **directory-structured data on Zenodo can only be a ZIP**. Expansion is not a
  convenience feature there; it is how you address a deposit's actual structure. And
  ~80% of real Zenodo ZIPs are a single top-level directory
  ([`evidence/zenodo-zip-shape-2026-08-21.md`](evidence/zenodo-zip-shape-2026-08-21.md)).
* **Never stream to compute a hash in `info()`.** The rule the entry established is
  right and is why the 2026-08-21 probe
  ([`evidence/zip-member-md5-pull-probe-2026-08-21.md`](evidence/zip-member-md5-pull-probe-2026-08-21.md))
  could not ship as-is.
* **CRC32 is a real verification asset.** The central directory publishes one per
  member, download-free, and `zipfile` checks it on **every read** — verified:
  a corrupted member raises `BadZipFile: Bad CRC-32`, and `testzip()` names it. DVC
  cannot consume CRC32 as a content hash, but we get member integrity for nothing.
* ~~**Invert the negative tests** in `test_dvc_integration.py`~~ — done; both are
  inverted and pass against live Zenodo (D23).
* **The `extract_zips` default should only be revisited afterwards**, for the reason
  given: flipping it while expansion costs the reviewer route would trade the project's
  core promise for convenience. **That "afterwards" has arrived** (D23) — expansion no
  longer costs the reviewer route, so the only argument left for keeping it off is the
  weak one this evidence undermined. It is now a live decision, and it changes the
  meaning of existing `.dvc` files, so it wants its own entry rather than a default
  flipped in passing.

**And manifests are not going away.** GEO and BIA-submitted files publish no checksum
*and* no immutable token, so they cannot use `version_aware` at all — for them a
`.chiro` sidecar remains the only route, exactly as D17's closing paragraph says. What
changes is that ZIP members are no longer in that category.

A note on integrity, since this entry was the place the argument lived: once a
container's bytes match the md5 Zenodo publishes, **every member extracted from it is
authenticated by that md5 by construction**. Per-member digests were never needed to
*validate* an extraction — only to satisfy DVC's content-addressed *storage*. This
entry conflated the two, and so did the design.

### 0e. Make `pip install 'archivefs[chiro]'` a command that can actually succeed
**Effort: hours, once chirograph is published. Value: it is on the critical path to
going public.**

`pyproject.toml` declares `chiro = ["chirograph>=0.1"]`, and four places tell the
user to install it: `README.md`, `cli.py`'s `info` output, `base.py`'s
manifest-missing warning, and D5's write-up. **chirograph is not on PyPI** — `pip
index versions chirograph` and a `--dry-run` install both come back "No matching
distribution found". It exists only as an unpublished local repo
(`/scratch/a56/jr9959/chirograph`, version 0.1.0).

So today the advice archivefs gives when it hits a manifest it cannot read is advice
that cannot be followed, by anyone, including us. That is worse than no advice: the
warning reads as "you forgot a step" when the step does not exist yet.

Consequences beyond the message:

* **The chiro layer is untested by default.** `tests/test_chiro.py` has a
  module-level `pytest.importorskip("chirograph")`, so the whole file — plus 10
  tests in `test_cli.py` — silently skips. That is the layer `design.md` calls the
  central abstraction, and the one D18 and #0d both build on. With chirograph on
  `PYTHONPATH` all 67 of them pass, so the code is fine; the *default* signal is
  what is misleading, since a green run says nothing about chiro.
* **Going public makes it visible.** A stranger following the README hits it
  immediately.

Options, cheapest first: publish chirograph to PyPI and keep the extra as-is;
or point the extra at a VCS URL, which works for `pip` but cannot be uploaded to
PyPI as a dependency; or drop the extra and document a manual install, which is
honest but gives up the `[chiro]` ergonomics. Publishing is clearly right if
chirograph is meant to be a real package — its own repo, LICENSE and test suite say
it is.

Meanwhile the suite should not report green while skipping its central abstraction:
make the skip loud (a warning summary, or a marker that CI must run) rather than one
quiet `SKIPPED` line.

**This is a pattern, not one bug.** Every gate in the suite — `chirograph` absent,
`ARCHIVEFS_TEST_EGA`, `_DVC`, `_ZENODO`, `_GEO`, `_BIA` — hides code that can rot
unnoticed, and two instances turned up the first time they were all switched on
together (2026-08-21):

* `tests/test_chiro.py` plus 10 CLI tests never ran at all. They pass (67) once
  chirograph is importable.
* The three live EGA tests **errored**: their fixture passed `username`/`password`
  as constructor options, which D16 had made a `CredentialSourceError` months of
  green runs earlier. Fixed by putting them in the environment — which is also the
  better test, since it exercises the lookup chain a user actually goes through.

So a default `pytest` run proves less than it appears to. Worth: a CI job that runs
everything, and a default-run summary that says plainly which capabilities were not
exercised.

### 0f. Per-archive capability profiles, so archive facts live in one place
**Effort: two or three days. Value: high, and it gates #0/#0d's replacement.**
**Tracked in [issue #2](https://github.com/swarbricklab/archivefs/issues/2).**

Archive-specific knowledge is spread over at least six places: `registry.py`'s
`PROTOCOLS`, **four** parallel protocol-keyed dicts in `dvcfs.py` (`_WRAPPERS`,
`DVC_PROTOCOLS`, `_EXTRA_CONFIG_KEYS`, `_BOOL_KEYS`), `pyproject.toml`'s entry points,
the backend class itself, a hardcoded key tuple in `chiro.py`, and hand-maintained
capability tables in `docs/`. Adding an archive means touching all of them, and missing
one fails in a way that does not point at what you forgot.

`Backend` in `registry.py` is already the right shape — it is just restricted to
*packaging* facts (`extra`, `requires`, `status`). The gap is **capabilities**.

Two reasons this is not merely tidiness:

* `_BOOL_KEYS` exists only because DVC config is INI, so every value arrives as a
  string. Register `extract_zips` without a type and `extract_zips = false` reaches the
  filesystem as `"false"` — truthy — expanding ZIPs **against an explicit instruction**.
  The comment in `dvcfs.py` documents the trap; the design still invites it.
* Issue #1 needs a new capability — *can this archive supply an immutable per-entry
  token?* Zenodo and EGA can; GEO and BIA-submitted cannot. Getting it wrong does not
  crash: `dvc/repo/fetch.py`'s `_log_unversioned` **silently drops** untokened files
  from the fetch with a warning. A switch that dangerous belongs in one reviewable
  declaration, not an `if` chain in the DVC adaptor.

Deliberately *not* a plugin framework: a profile declares *what*, never *how*, and
behaviour stays in the fsspec subclass. The main open question is whether capability
facts can live in `registry.py` at all — it is deliberately importable with no backend
dependency, and `archivefs info` must report a missing extra *without* importing the
backend.

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

### 4. Zenodo concept DOIs → a recorded, pinned version
**Effort: a few days. Value: the strongest provenance story available anywhere.**
**Narrowed 2026-08-24** — the storage half of this moved to
[issue #1](https://github.com/swarbricklab/archivefs/issues/1); what is left is the
part that is actually about *versions*.

Zenodo is the one archive where DVC's version-awareness is *honest*: a version DOI
genuinely identifies a distinct, immutable record. The `version_aware` /
`version_path` plumbing this entry called for is issue #1's job, done there for the
reviewer-pull reason rather than the provenance one, and with `version_id` carrying a
per-entry immutability token rather than a DOI.

What remains distinctively here: importing a **concept** DOI still does not record
which version was taken. `zenodo://10.5281/zenodo.1486183` should resolve to a version,
record it, and let `dvc update` move it forward deliberately and visibly — the design
doc's "`dvc update` against a concept DOI re-resolves and re-extracts". Needs care
around what `dvc update` should do when a *new* version has different files, which is
the genuinely hard part and the reason this is not a subtask of #1.

Note the two want *different* things in `version_id`, and issue #1 picks one: a
per-entry content token, not the record's DOI. A concept-DOI resolution therefore has
to be recorded somewhere else — `archive_version` already exists for it.

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

### 18. Zenodo per-record share links, as a second shareable capability
D32's bundle mechanism can carry exactly one credential today, because BIA's per-study
key is the only capability archivefs models — Zenodo's `token` is a personal access
token, which authenticates an account and is refused. But Zenodo *does* issue per-record
preview links for restricted and embargoed records, which are the same shape: scoped to
one deposit, issued to be handed on, revocable without touching the account.

Adding one would be a `Credential(name="share_token", scoped=True, shareable=True)`
beside the existing token, a `grants_from_link` that parses the preview URL, and
whatever query parameter Zenodo actually wants — which needs measuring against the live
API, not reading off the docs, on the D31 precedent. The bundle format needs no change:
grants already carry `(protocol, credential, scope, value)`.

Worth doing the day someone has an embargoed Zenodo record in a paper under review. Not
before — the guessed API shape would be the whole risk, and there is nothing to test it
against.

### 19. A bundle that expires
A sharing key is revocable but not time-limited, so a bundle mailed to a reviewer in
2026 still works in 2029 unless the depositor regenerates the link. An `expires` field
in the manifest was considered and left out: nothing could enforce it, and a field that
merely *claims* an expiry is worse than none, since it invites the belief that access
lapsed on its own.

The honest version is a `credentials list --stale` that flags keys for studies that have
since been released — those are dead weight, and knowing which are safe to delete is the
question a depositor actually has. It needs one API call per accession, which is why it
is here and not built.


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

## ~~Split ordinary file reads too, not only ZIP blocks~~ — done, and worth 6.5x not 30x

**Shipped as [D28](decisions.md); issue #14 closed.** Kept for the part that turned
out to be wrong.

The estimate here was **30x**, from "a single connection sustains ~0.30 MB/s at any
request size, against 9.7–14.0 MB/s across eight". Measured on five cold Zenodo
objects it is **6.48x** — 0.36 MB/s against 2.32.

The 0.30 and the 9.7–14.0 are both real and they are not comparable. Every earlier
probe used one 2.53 GB object, repeatedly; **Zenodo's origin warms to an object**, and
warm, a single connection does 8–10 MB/s. So 0.30 is a cold measurement and 9.7–14.0 a
warm one, and dividing them measured the temperature rather than the parallelism. The
first attempt at the follow-up probe reproduced the mistake in the other direction and
found *no effect at all* (0.93x) before the trial order gave it away.

The concurrency is worth having — an import always reads cold — but the lesson is the
one D27 already flagged and this went on to demonstrate: **absolute MB/s against
Zenodo means nothing outside a within-run comparison at one temperature.**

## Make a plain file's *download* resumable, not just a ZIP's blocks

D29 gave ZIP containers a block cache that survives the process, so an interrupted
20 GB import resumes. **A plain file gets nothing.** `_open` uses a read-ahead cache
and DVC streams straight to a temporary file, so an `archivefs get` or `dvc
import-url` of a 2.5 GB `.ome.tif` that dies at 90% starts again from zero — and at
the 2.32 MB/s a cold Zenodo object gives (D28), that is 18 minutes thrown away for
that file, more for the imaging objects this project exists to move.

It is not the same mechanism. A block cache is the right shape when the reader jumps
around inside a container; a sequential download wants a partial output file plus a
resume offset, which is `HTTP Range` against something already on disk. The
correctness hazard is the mirror image of D29's and just as sharp: resuming into a
partial file whose *tail* is a half-written buffer appends good bytes to bad ones and
produces a file that is the right length and wrong. So the offset has to come from a
sidecar written after an `fsync`, not from `os.path.getsize`.

The identity check D29 already has — protocol, path, size, published md5 — carries
over unchanged, and so does the argument for keying on the checksum. What does not
carry over is where the file lives: a resumable download's partial output belongs
next to its destination, not in a shared cache directory, because that is where the
finished file is going anyway.

## Seed the ZIP reading cache from the listing view

`_member_view` (D25) re-parses the central directory through its own block-aligned
cache, and the central directory is at the end of the archive — so it re-fetches a
whole block the listing view had already read. Measured: one 7-byte member out of a
25 MB Zenodo archive costs 8.2 MB instead of 4.2 MB. Only the single-member
`archivefs get`/`cat` path pays it; a full import is dominated by member data.

`MMapCache` takes a `blocks` argument — the set of block numbers already present — so
the shape of the fix exists: write the tail bytes the listing view holds into the
sparse file and declare those blocks fetched. The awkward part is that
`BytesCache` holds an arbitrary contiguous range, not whole blocks, so only the
fully-covered blocks can be claimed and the bookkeeping has to be exactly right — a
block wrongly marked present serves zeros as data, which is worse than the
inefficiency it removes. Wants a test that reads every byte of a container both ways
and compares, before anyone trusts it.

Not urgent: the case it helps is the one where absolute cost is smallest.

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
