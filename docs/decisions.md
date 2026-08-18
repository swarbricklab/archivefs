# Decisions taken during implementation

Written for review. Everything here is either **a spec question the plan left open
and I had to answer**, or **a place where reality contradicted the design and I
deviated**. Each entry says what the design assumed, what is actually true, what I
did, and what it would cost to change my mind.

Companion documents: [`design.md`](design.md) (architecture), [`plan.md`](plan.md)
(checklist), [`ideas.md`](ideas.md) (things worth doing that I did not do).

Evidence for the empirical claims is in [`evidence/`](evidence/) and reproducible
via [`scripts/ega_checksum_check.py`](../scripts/ega_checksum_check.py).

---

## D1 — ⚠️ The blocking EGA checksum question is **resolved: happy path**

**Status: closed.** This was the design's one "single fact the manifest design
currently rests on as an assumption".

**The question.** Does the MD5 in EGA's metadata equal the MD5 of the bytes on disk
after download + decryption?

**Answer: yes.** `md5(materialised decrypted bytes) == metadata plainChecksum`, and
`size(materialised) == fileSize - 16`. That is *branch 1* of the three the design
enumerates, so download-free `info()` is honest and `--no-download` manifests are
sound for EGA.

**How it was established.** Not from documentation. The design doc's own procedure,
run against the live EGA API v2 with the public test account that ships inside
pyega3 (`ega-test-data@ebi.ac.uk`, synthetic data only), on a 100 MB file, in 13
slices across 4 connections, **including the interrupted-and-resumed variant the
procedure asks for**:

```
plain size from Content-Range: 105203907
metadata fileSize - 16       : 105203907   (agree)
simulating interruption: deleting slice 6 and refetching
materialised 105203907 bytes -> md5 7626e4caa33ef4ba1a3b3fe42c2d011c
plainChecksum                            7626e4caa33ef4ba1a3b3fe42c2d011c
```

Full log: [`evidence/ega-checksum-check-2026-08-17.log`](evidence/ega-checksum-check-2026-08-17.log).

**Two refinements the exercise produced:**

1. **The "md5 == encrypted checksum" branch is not expressible.** EGA API v2
   publishes *only* `plainChecksum`; there is no advertised checksum of the
   encrypted object at all. So branch 2 of the design's decision tree cannot
   occur — a useful narrowing.
2. **`Content-Range` is a better size source than `fileSize - 16`.** A one-byte
   ranged read of the plain endpoint returns `Content-Range: bytes 0-0/<plain
   size>`. The two agreed for every file checked, but the former is authoritative
   and the latter is an inference about EGA's encryption header. `plain_size()`
   uses it when a listing lacks a size.

**Caveat worth keeping.** This was established on EGA's *synthetic test* dataset,
because that is what is reachable without a DAC approval. The mechanism (server-side
decryption with `destinationFormat=plain`) is the same for controlled data, but
re-running the script against one real authorised file would close the last gap.
It takes one command and is worth doing before the first production import.

---

## D2 — EGA is implemented over its REST API; **pyega3 is not a dependency**

**Design assumed:** an `[ega]` extra pulling in `pyega3`, with the backend calling
into it ("dataset → EGAF members via pyega3", "lean on pyega3's refresh").

**What is actually true, in order of how much it decided this:**

1. **pyega3 pins `requests==2.26.0` exactly.** Not `>=`. Taking it as a dependency
   drags every other backend — and the user's whole environment — back to a 2021
   requests. It broke the conda environment solve on the first attempt, which is
   how I found out.
2. **pyega3 calls `sys.exit()` on error**, in a library function, for auth failures
   and unauthorised datasets. Inside `dvc pull` that is not an exception you can
   catch and turn into a good message; it kills the process.
3. **An fsspec filesystem needs ranged reads, not a file-at-a-time downloader.**
   pyega3's unit of work is "download this file to this directory"; fsspec's is
   "give me bytes `[a, b)` of this object". The former cannot implement the latter
   without writing the whole file to disk first.
4. **The API pyega3 wraps is plain HTTPS.** OAuth2 password grant, `GET
   /metadata/datasets/{EGAD}/files`, `GET /files/{EGAF}?destinationFormat=plain`
   with a `Range` header. About 150 lines, all of which we would need anyway.

**Decision.** `archivefs.backends.ega` speaks the REST API directly with `requests`,
and there is **no `[ega]` extra**: EGA works in the base install. archivefs still
*follows* pyega3 rather than inventing anything — the endpoint URLs, OIDC client id
and public client secret are pyega3's shipped defaults, so we track EGA's own client
rather than a second source of truth, and all are overridable.

**Cost of reversing:** low. A `transfer="pyega3"` option could delegate large
downloads to pyega3 (for its parallel-slice tuning) behind the same interface. It
would need its own extra and a `SystemExit` guard.

---

## D3 — 🩹 **EGA ranged reads must be 16-byte aligned or they return wrong bytes**

**Not in the design at all**, and the most dangerous thing I found.

A ranged read of `?destinationFormat=plain` whose **range start is not a multiple of
16** returns HTTP `206`, the correct `Content-Range`, the correct number of bytes —
and **the wrong bytes**. No error, no warning. Measured live:

| range start | 0 | 16 | 32 | 48 | 1008 | 4096 | 12352 | 17 | 31 | 999 | 1009 | 4097 | 12345 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| correct? | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ |

16 bytes is the AES block size: the server maps a plaintext offset onto a cipher
block and does not re-align the output.

**Why nobody has hit it:** pyega3 only ever requests multiples of its slice size, so
it is aligned by construction. An fsspec filesystem reads wherever the caller
seeks, so it hits this immediately — my own first live test caught it (a read at
offset 1000 disagreed with the same bytes taken from a whole-file read).

**Decision.** `EGAFileSystem.range_alignment = 16`; every read is aligned down and
the surplus trimmed locally. The mechanism lives in the shared base
(`_fetch_range`), so any future archive with the same property gets it by setting one
attribute. Tests pin it both ways: a unit test asserts we never *ask* for an
unaligned start (the fake returns zeros if we do, exactly like the real service), and
a live test compares an unaligned sub-range against the whole file.

**Reported upstream?** Not yet — worth doing, with the table above. See
[`ideas.md`](ideas.md).

**Related, also undocumented:** a ranged read can return success with a **short or
empty body** (observed with 25 MB slices at 4 connections). Every read is
length-checked and retried rather than trusted; a silently truncated read would
corrupt data with no error anywhere.

---

## D4 — 🩹 **"DVC inherits fsspec filesystems for free" is false**; there is an adaptor

**Design assumed** (core architecture, first section): "DVC delegates storage to
fsspec … So the deliverable is a set of fsspec filesystems registered under custom
protocols; DVC inherits them for free."

**Measured against DVC 3.67**, the first clause is true and the conclusion is not:

* DVC publishes *itself* to fsspec via an `fsspec.specs` entry point but **does not
  consume that group**. Its protocol table is a plain dict,
  `dvc_objects.fs.known_implementations`, with no plugin mechanism.
* `dvc.config_schema` rejects unknown URL schemes *before* that table is consulted
  ("Unsupported URL type zenodo://"), and its validator **snapshots** the schema
  table when it is built, so adding an entry later silently does nothing.
* DVC wraps every fsspec filesystem in its own `dvc_objects.fs.base.FileSystem`,
  which is where `PARAM_CHECKSUM` — the field DVC treats as the content hash — is
  declared. Without that wrapper, `import-url --no-download` records no checksum.
* `dvc_objects`' registry **falls back to `LocalFileSystem`** for an unknown
  protocol instead of raising. A half-registration would mean a URL silently
  resolving as a local path.

**Decision.** A small adaptor, `archivefs.dvcfs`: three generated `FileSystem`
wrappers (`PARAM_CHECKSUM = "md5"`, protocol-stripping delegated to the fsspec
class) plus `register()`, which patches both DVC tables and rebuilds the validator.

**The awkward part, flagged for review:** something has to call `register()` inside
the `dvc` process, and `dvc` is a console script — there is no import hook for a
user to add. Options considered:

| option | verdict |
|---|---|
| monkey-patch on `import archivefs` | rejected: a library that mutates another library's globals on import is a menace, and it would not help — nothing imports us |
| ask users to `python -c "import archivefs; ..."` | does not work: `dvc` is a CLI |
| a `.pth` in site-packages, installed by an explicit command | **chosen** |
| upstream a plugin entry point in DVC | best long-term answer; see [`ideas.md`](ideas.md) |

`archivefs dvc enable` writes `archivefs_dvc.pth` into the *active environment's*
site-packages. It imports one module that installs a one-shot `sys.meta_path` finder,
which registers immediately after `dvc.config_schema` finishes loading — deterministic,
no polling, nothing heavy imported at interpreter start-up, and wrapped so a failure
can never break the interpreter. `ARCHIVEFS_NO_DVC_HOOK=1` opts out;
`archivefs dvc status` reports and *probes* it; `archivefs dvc disable` removes it.

**It is opt-in, not automatic on install.** Writing a `.pth` behind a `pip install`
would be the kind of thing that makes people distrust a package.

**Verified end to end** (`tests/test_dvc_integration.py`, and by hand):

```
$ dvc import-url --no-download zenodo://1486184/github-mark.zip logo.zip
$ cat logo.zip.dvc
deps:
- md5: 49f6a914ea33912471512ac9228c6923     # download-free, from record metadata
  size: 853
  checksum: 49f6a914ea33912471512ac9228c6923
  path: zenodo://1486184/github-mark.zip     # an accession, no secrets
$ dvc pull logo.zip.dvc && md5sum logo.zip
49f6a914ea33912471512ac9228c6923  logo.zip   # matches what was recorded
```

and the same for `ega://` with credentials only in the environment — the `.dvc`
contains neither username nor password. `dvc list-url` and directory imports (`.dir`
manifest, `nfiles`) work for all three protocols.

---

## D5 — Manifest **parsing** requires the `chirograph` package; manifest *absence* does not

**Design says** archivefs "optionally reads Chirograph manifests … but works
**without** them (native listing, native checksums)". Ambiguous between "works
without manifests" and "works without the library".

**Decision: works without *manifests*; needs the library to read one.** No second
implementation of the format lives here. The format spec's own warning is the
argument — "a reimplementation that canonicalises *almost* identically produces
non-matching hashes silently" — and `chirograph` is deliberately dependency-free, so
the cost of requiring it *when a manifest is present* is one small pure-Python
package.

**Behaviour when it is missing:**

* manifest auto-discovered, library absent → **warn** (naming the manifest and the
  `pip install 'archivefs[chiro]'` that would use it) and fall back to native
  listing. Weaker guarantees beat refusing to work, but not silently.
* manifest requested explicitly (`chiro=<path>`) → **error**. The user asked for it.

**Also decided here:** manifests are **on by default** (`chiro="auto"`), matching
the design's "the plugin's front door is the manifest, for every backend". Cost is
one extra fetch, only when a manifest is actually found.

**One consequence worth confirming you want**, because it is a real behaviour
change and not obviously right: when a manifest is applied, it *is* the listing, so
a file that is in the deposit but **not** in the manifest disappears from `ls()` and
`exists()` returns `False` for it. That is what "the manifest is the directory
listing" means, and hiding it would be worse — so it is reported rather than
silent: the drift report lists it under `unrecorded`, a warning is logged, and
`archivefs check` prints it. If you would rather unrecorded extras stayed visible
in the listing, that is a one-line change in `apply_manifest`; I chose the design
doc's reading. (A manifest that matches *nothing* is a different case and is now
refused outright — see D11.)

---

## D6 — 🩹 The archive version token is **not** DVC's `version_id`

**Found by breakage.** Emitting `version_id: 10.5281/zenodo.1486184` in `info()`
made a beautiful `.dvc` file — and then `dvc pull` failed with
`'ZenodoDVCFileSystem' object has no attribute 'coalesce_version'`. DVC treats
*any* `version_id` in a `Meta` as "this is a cloud object version", flips the
filesystem into `version_aware` mode, and expects the S3/GCS/Azure versioning API.

**Decision.** The token lives under our own key, `archive_version`, plus inside the
per-archive metadata block (`zenodo.doi`, and so on). The DVC wrapper forces
`version_aware = False` and provides a defensive no-op `coalesce_version` so a
`.dvc` written before this change does not crash.

**What is lost:** the version DOI no longer appears in the committed `.dvc`. In
practice the pin is the `md5`, and for a *version* URL the path itself names the
immutable record. It matters only when the URL is a **concept** DOI, where the
`.dvc` cannot record which version was resolved.

**Better answer, not built:** implement real version-awareness for Zenodo, where a
version DOI genuinely identifies a different record. That is a proper feature with
`dvc update` semantics to think through — [`ideas.md`](ideas.md).

---

## D7 — EGA member naming: three layouts, accession-keyed by default

**Design is ambiguous.** It shows `ega://EGAD00001006673/EGAF00004908765` (so
accession-keyed) but also wants a "friendly workspace layout", and DVC names
workspace files from `ls()`.

**Decision.** `layout` storage option, default `"accession"` so the documented URL
grammar is literally true:

| layout | member path | why you would pick it |
|---|---|---|
| `accession` (default) | `EGAD…/EGAF…` | exactly as documented; stable, opaque |
| `display` | `EGAD…/sample.bam` | friendly; collisions get the accession appended so the mapping stays injective |
| `nested` | `EGAD…/EGAF…/sample.bam` | pyega3's own layout: friendly *and* collision-proof |

**All three resolve all three spellings**, so a `.dvc` written under one layout keeps
working if someone reconfigures the layout later — the accession is the identity, the
layout is presentation. Under `nested`, `ega://EGAD…/EGAF…` is a one-file directory
rather than a file; that is the one place the documented grammar shifts meaning, and
it is tested.

The real answer to friendly layout is a `.chiro` manifest's two-path model (D5),
which is what the design intends. These layouts are what you get without one.

---

## D8 — GEO's `filelist.txt` is **not** a listing

**Found by using a real record.** For GSE176078 (our own Wu et al. deposit),
`suppl/filelist.txt` describes only `GSE176078_RAW.tar` and its 26 members — and
omits two top-level supplementary files that the directory index shows, including
the bulk RNA-seq counts. Trusting the filelist as the listing would have silently
dropped deposited data from every import.

**Decision.** The HTML directory index is the listing; `filelist.txt` only
*enriches* it (exact sizes, file types, and what is inside an archive member). The
index's own sizes are human-rounded ("532M") and are discarded — a rounded size would
corrupt a ranged read — so `info()` resolves an unknown size with a `HEAD`, and a
listing may legitimately carry `size: None` until asked.

**Also:** GEO publishes no per-file digest, so `info()["md5"]` is `None` with a
`checksum_source` field explaining why, rather than substituting an ETag or a
mtime-hash that says nothing about the bytes. This is exactly the gap a sidecar
`.chiro` closes (D5).

---

## D9 — ZIP safety is enforced at **listing** time, not only on extraction

**Design says** zip-slip is "the one real security burden" and gives the rule:
either route extraction through `extractall()`, or validate every resolved member
path before writing.

**Decision: both, plus earlier.** `archivefs.zipsafe` vets an archive from its
central directory *before any member is advertised*. The reasoning: DVC materialises
a directory listing by writing the paths `ls()` returned, so a path we would refuse
to write must never be offered. Refusing at listing time also makes the failure
loud and early rather than half-way through a pull.

Checks: containment (absolute paths, `..`, Windows drive letters, backslash
separators), **resolved-path** containment so an escape through an existing symlink
in the target is caught too, symlink members, zip bombs by declared total and
per-member ratio (both free from the central directory), CRC verification on
extraction, and Windows-portability complaints returned as *warnings* — an archive
that cannot be extracted on Windows is still perfectly extractable here, and
refusing it would be overreach.

**One consequence worth stating plainly**, because it looks like a regression and is
not: over a *transparent* ZIP, `dvc import-url --no-download` is **not**
download-free. It cannot be — a member's md5 is only knowable by decompressing it.
What DVC records is a `.dir` manifest of real per-member md5s (verified: the
`--no-download` run produced the identical `.dir` hash to the downloading run, and
that object contains real content hashes, not nulls), so it obtains them by reading
the bytes. Nothing is dishonest, and the design's asymmetric contract is exactly
this: the **container** md5 is download-free and strong (Zenodo publishes it),
member-level integrity is CRC32 from the central directory plus the workspace md5
computed at extraction. If you want a genuinely download-free pin, import the ZIP
opaquely (`extract_zips` off) and the dep carries Zenodo's own md5.

**On the "identify and evaluate a robust ZIP library" open item:** I looked and did
not adopt one, deliberately. `zipfile` already handles the parsing tail
(encodings, ZIP64, CRC) and is the only implementation guaranteed present;
the candidates each cover a different axis (`stream-unzip` for non-seekable
streaming, `libarchive-c` for formats beyond ZIP) and **none of them do the
write-side containment check**, which is the part that actually matters here. So the
checks are ours, in one ~270-line module with tests, rather than a dependency plus
the same checks anyway. Revisit if we meet a real archive `zipfile` cannot open.

---

## D10 — 🩹 Every derived predicate goes through `info()`, never the internal lookup

**Found by driving the real thing**, which is why it is worth writing down: no unit
test caught it, and the symptom was silent.

The base implemented `exists`/`isdir`/`isfile`/`checksum`/`ukey`/`cat_file`/`_open`
in terms of its *internal* metadata lookup (`_resolve`) rather than the public
`info()`. Zenodo overrides `info()` to present a ZIP member as a directory when
`extract_zips` is on — so `info()` said "directory" while `isdir()` said "file".

DVC's download path asks `isdir()`. Result: `dvc import-url` recorded the dep
correctly as a directory (`.dir` hash, `nfiles: 2`) and then **wrote the `.zip`
into the workspace instead of its contents** — precisely the "half-solution where
the manifest says ZIP but the workspace holds a directory" the design warns against,
with no error anywhere.

**Fix:** the derived methods call `self.info()`; `_resolve()` is only what `info()`
is built from. A comment in the base says why, because the tempting "optimisation"
is to call `_resolve` and skip a layer. Regression tests at both levels: fsspec
(`isdir`/`isfile`/`exists` must agree with `info()["type"]`) and end-to-end (a DVC
remote with `extract_zips` must leave an extracted *tree* in the workspace).

**Verified after the fix:**

```
$ dvc remote add zen zenodo://1486184 && dvc remote modify zen extract_zips true
$ dvc import-url remote://zen/github-mark.zip tree/
$ find tree -type f
tree/md5sum
tree/github-mark.svg
```

**The general lesson**, worth keeping in mind for the next backend: a subclass
expresses "how this archive presents itself" by overriding `info()`, so anything in
the base that answers a question about a path must ask `info()`.

---

## D11 — 🩹 What adversarial review of the finished code found

Worth recording separately, because the pattern is instructive: **every one of these
was silent**, and the two worst were in the features the design is proudest of. A
reviewer was pointed at the finished package and asked for correctness bugs the tests
did not cover. Findings, with what each would have done to real data:

**`ls()` returned every descendant, not the immediate children.** fsspec's `walk`
— and therefore `find`, `glob`, `expand_path` and `get(recursive=True)`, which is
how DVC pulls a directory — keys each directory's files **by basename**. Given a
flattened listing, two members with the same basename at different depths overwrite
each other, so the pull writes fewer files than the deposit holds, exit code 0, no
warning. And this is the *normal* shape of the two features that produce nested
paths: a `.chiro` manifest relocating a flat deposit into `sampleA/matrix.mtx` +
`sampleB/matrix.mtx`, and EGA's `layout="nested"`, which exists precisely *because*
display names repeat. Demonstrated against this repo's own test fixture: `ls` showed
three files, `find` showed two. Fixed by returning children and synthesising
directory entries; regression tests assert both matrices survive `find` and `glob`,
and that their bytes differ so a collision would be real loss.

**`path.split("/", 1)[-1]` assumed a container is one path segment.** Zenodo's DOI
form is two (`10.5281/zenodo.1486184`). Every manifest lookup missed, so a
manifest-bearing record looked like it had lost every file — and the fallback fetch
re-entered the listing being built, once per frame, for **125 live API calls** before
Python's recursion limit stopped it. Fixed with one `relpath(container, path)` helper
used everywhere, plus a cache seed so that path cannot recurse at all even when a
`chiro=` setting is mistyped.

**An unfetchable manifest became an empty directory.** `ArchiveNotFoundError`
subclasses `FileNotFoundError`, which fsspec's `walk` swallows — so a 404 on the
manifest turned into a listing of nothing, i.e. a pull that succeeds and writes
nothing. Manifest fetch failures are now `ManifestError`, which is deliberately
*not* a `FileNotFoundError`.

**An ambiguous EGA display name returned the first match.** Display names repeat
across accessions, so this handed back a *different* file's bytes under the requested
name — with that file's own md5 alongside, so a checksum check would have **passed**.
Now refused, naming the accessions.

**`extract_zips = false` in a DVC config meant true.** DVC config files are INI, so
every value arrives as a string, and `"false"` is truthy. The keys were registered as
bare `object`; booleans now go through DVC's own `Bool`, and `chiro` tolerates a
stringified "false" rather than treating it as the name of a manifest to fetch.

**A zero-byte Zenodo file was unreadable** (`size or filesize` turned `0` into
`None`, then into an `OSError` on read), and a file with no published checksum got
`md5 == ""` instead of `None` — which every consumer would read as "a checksum was
published".

**A `fileSize` below the encryption header gave a negative size**, and a read then
returned `b""` with no error. EGA answers `200`-with-empty-fields for objects an
account cannot see, so this was reachable.

**Token refresh stampeded.** Every worker thread hitting the hour boundary POSTed its
own token request, serialised behind the lock the class docstring said prevented
exactly that. Refresh now passes the token it saw rejected and returns early if
someone else already replaced it.

**`invalidate_cache()` did not invalidate the backends' own caches.** A Zenodo
record's JSON *contains* its file listing, so a refresh re-derived the same stale
listing from a cache hit and appeared to do nothing.

**`exists()` swallowed `AuthenticationError`**, reporting an expired token or revoked
DAC approval as "not there" — hiding the one message that says what to fix.

Smaller: two ZIP member names that normalise to one key silently dropped one
(now refused); a `#` in a GEO filename truncated its URL at what `urljoin` reads as a
fragment (now quoted); a top-level GEO file listed after an archive block was
attributed to that archive (now promoted back out using the directory index, which
*can* tell them apart); and `_fetch_checked` now checks the `Content-Range` start it
was served, which costs nothing and guards D3's alignment arithmetic against both a
regression here and a proxy that re-ranges.

**A second pass, after those fixes, found one regression I had introduced and eight
more:**

* **The cache seed that stopped the manifest recursion made listing
  thread-unsafe.** Seeding the *native* listing before fetching the manifest
  published a half-resolved listing to every other thread — and DVC runs one
  filesystem instance across a thread pool, so this is the normal case. A second
  thread saw no manifest, no checksums and no drift report, or a spurious
  `FileNotFoundError` on a relocated path. Reproduced with two threads and a slow
  manifest fetch. Fixed with a re-entrant lock (re-entrant because the
  same-thread re-entry is exactly what the seed is for), and a regression test that
  races three readers.
* **Zenodo's fallback data URL was unquoted** — and it is the *common* path, since
  real record JSON carries the URL under `links.self` rather than `links.content`.
  A `#` or `?` in a filename truncated the URL. Now prefers the API's own link and
  quotes the fallback.
* **The token-stampede fix was incomplete**: it read the *current* token after the
  401, which under concurrency is a token a peer may already have refreshed, so the
  guard matched nothing. The rejected token is now threaded down from the failed
  request's own headers, which is the only value that is definitionally correct.
* **A 401 or 503 while opening a ZIP was reported as "File is not a zip file"** —
  `zipfile` reads the end of a file inside `except OSError: pass`, so an auth
  failure told the user their perfectly good archive was corrupt. Now re-reads four
  bytes to surface the real error, and only then says "not a ZIP".
* **`walk()` now defaults to `on_error="raise"`.** fsspec's default omits the
  subtree an error came from, which is the same silently-fewer-files failure as the
  `ls()` bug arriving through a different door.
* **`chiro = true` was treated as a manifest filename** (the false-ish spellings had
  been handled, the true-ish ones had not).
* **A manifest that matches nothing is refused**, instead of producing a listing
  where every entry is a ghost that fails only when read — usually a manifest
  written against a different layout.
* **A manifest that nests one file inside another's path is refused** (`data.txt`
  plus `data.txt/inner.txt` cannot both exist, and the children-only `ls` would
  report the file and never descend).
* `archivefs verify` raised a bare `ImportError` without chirograph rather than the
  documented `MissingExtraError`; the `Content-Range` check now applies only to
  `206` so it cannot pre-empt the whole-object-`200` fallback (which now warns,
  since it silently re-downloads per block); `invalidate_cache` clears a record's
  DOI aliases as well as its id; `archivefs get <file> <dir>` writes into the
  directory; and `archivefs ls -l` resolves the sizes a listing deliberately leaves
  unknown.

**The lesson I would keep:** the tests were green at every stage, and each of three
different cheap checks — driving the real consumer (`dvc import-url` with
`extract_zips`), installing into a clean venv, and two adversarial reads — found
things the others did not. The most valuable single habit was asking "if this went
wrong, would anything say so?", because nearly every bug above answered no.

---

## D13 — `bia://`: five traps, and a verification note that was wrong

The BioImage Archive was a *candidate* in the plan, gated on verifying its access
model. That verification happened first (`docs/evidence/bia-access-model-2026-08-17.md`),
the backend was then built from it — and **two of that note's central claims turned
out to be wrong**, both in the direction of silent data loss. The note is corrected
in place, with the errors marked rather than quietly edited away, because the way
they arose is instructive: reading the *first page* of a paginated API and the
*count* printed beside it makes the API look generous and complete.

What the live service actually does, all measured:

1. **The file listing is paginated, five rows at a time.** The response is
   DataTables-shaped, and `recordsFiltered: 584` sits next to a five-element `data`
   array. Reading that as the listing imports five files out of 584 and exits 0.
2. **Asking for too much returns nothing.** `length` is capped at exactly 1000, and
   `length=2000` answers `200` with `data: []` and `recordsFiltered: 0` — not an
   error, not a clamp. `length=-1` "works" on a small study and returns nothing on a
   large one, which is the same trap in disguise.
3. **Page by the size you asked for, not the rows you got**, or pages overlap and
   files appear to vanish. And `recordsFiltered` counts *rows*: S-BIAD1 lists seven
   paths twice (same file, different `file_position`), so 6784 rows are 6777 files.
   Completeness is reconciled against rows fetched; duplicates are collapsed and
   logged.
4. **The payload path cannot be derived from the accession.** BIA shards FIRE at
   least two ways — `S-BIAD/599/S-BIAD599` but `S-BIAD/S-BIAD0-99/S-BIAD1` — so a
   rule inferred from one study 404s on the other. It comes from
   `/studies/{acc}/info`, which doubles as the existence check, because the files
   endpoint answers an unknown accession with an empty list.
5. **`isDirectory` is the string `"false"`**, which is truthy; and 6776 of
   S-BIAD1's paths live under a directory called `#1_Raw_Images`, which truncates an
   unquoted URL.

**Two views, on GEO's registry.** `submitted` (default) is the deposit: byte-exact,
range-readable, **no checksums** — the GEO problem with the GEO answer, a sidecar
`.chiro`, which for our own deposits we control. `derived` is BIA's own OME-Zarr and
thumbnail conversions in an anonymously listable Embassy S3 bucket, and it publishes
**single-part ETags that really are MD5s** (8000/8000 sampled, including 4 MB chunks).
That makes it the only place in archivefs where a *derived* artefact is verified
download-free — `dvc import-url --no-download` on a converted thumbnail records a
real content hash without moving the bytes, and `dvc pull` then matches it.

**The derived view lists lazily**, one delimited S3 request per directory. Flat
enumeration is not merely slow: one object per Zarr chunk means S-BIAD599's converted
tree is **over 60,000 objects**, and enumerating it ran past 280 s without finishing,
while a one-level listing takes ~11 s for 100 images and under a second per image.
This is also what makes the view useful for what it is *for* — pointing zarr at a
pyramid and reading the chunks it wants. It works because of D10: everything in the
base derives from `info()`, so overriding `ls()` and `info()` was enough for
`exists`/`isdir`/`checksum`/`cat_file`/`open`/`find`/`glob` to follow.

**A repeated S3 continuation token is refused, not silently truncated.** Stopping
quietly would return a partial directory as though it were complete.

---

## D12 — smaller calls, in one place

* **`checksum()` is overridden** on every backend. `AbstractFileSystem.checksum`
  hashes `info()` + mtime, which would throw away a real MD5 in favour of a token
  that says nothing about the bytes. Every consumer asking for a checksum wants the
  former.
* **`ls("")` differs per archive, deliberately.** EGA lists your *authorised
  datasets* (which makes `dvc list-url ega://` genuinely useful); Zenodo and GEO
  refuse, because a root listing of either is meaningless and searching is a
  different operation (`ZenodoFileSystem.search`). A magic root that means something
  different per archive is worse than no root.
* **Errors carry remediation, and it is tested.** `CredentialsNotFoundError` lists
  every place it looked in precedence order, so the message documents the chain; a
  test asserts the "never in a committed .dvc" line is in it. `require()` reports
  *all* missing credentials at once rather than one per attempt.
* **Missing credentials are reported for the whole set**, and a `Credential`
  declares whether it is a secret, so `committed_secrets()` can turn the "no secret
  in the committed artifact" rule into a check rather than a promise.
* **GEO views are a registry, not a branch.** A test adds a fourth view from
  outside the module to prove the SRA seam is real: implementing raw-reads-via-SRA
  later means writing one class, not editing the backend.
* **`dbgap://` is registered but refuses**, so the error is "this is deferred, here
  is what to do instead" rather than "unknown protocol", which reads like a typo.
  It is deliberately *not* registered with DVC — advertising a backend to DVC that
  refuses would be worse than the protocol being absent there.
* **The EGA re-encryption timestamp** (embedded in the archive's own `.cip` storage
  name) is used as EGA's weak version token. It is the only thing EGA exposes that
  changes when an object is replaced.
* **Test doubles are hand-rolled** (`tests/fake_http.py`, under 200 lines) rather than a
  recording library, because the interesting cases cannot be recorded: a 401
  mid-transfer, a short body on a 206, an unaligned range returning garbage. Bodies
  are trimmed copies of *real* API replies (`tests/data/`), so field names and
  quirks are the archives', not mine.

---

## Environment notes (NCI-specific, not design)

* Dev environment is conda, defined in [`environment.yaml`](../environment.yaml) and
  git-tracked, per the lab convention: `conda env update -n archivefs -f
  environment.yaml`. Python 3.12, fsspec 2026.7, dvc 3.67.
* `chirograph` is not on PyPI yet, so the env installs it editable from the sibling
  checkout `../chirograph`. Replace that line with `chirograph>=0.1` when it
  publishes.
* EGA's endpoints are on **port 8443**, which many institutional firewalls block.
  It is reachable from NCI. The auth-failure path says so explicitly, because
  "connection refused" on a non-standard port is otherwise a long afternoon.
* Live tests are opt-in per archive: `ARCHIVEFS_TEST_EGA=1`,
  `ARCHIVEFS_TEST_ZENODO=1`, `ARCHIVEFS_TEST_GEO=1`, `ARCHIVEFS_TEST_DVC=1`. The
  default `pytest` run is entirely offline.
