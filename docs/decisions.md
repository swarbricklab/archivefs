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
import with `extract_zips` must leave an extracted *tree* in the workspace).

**Verified after the fix** (originally through a named remote; re-verified and the
test rewritten on the supported `--fs-config` path when D15 dropped that pattern):

```
$ dvc import-url --fs-config extract_zips=true -- zenodo://1486184/github-mark.zip tree
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

**What adversarial review then found in the derived view** — two more
silent-wrong-answer bugs, both verified live, and both worth stating because they
are the same *shape* as the ones D11 caught:

* **A partial-name prefix was reported as a directory.** S3 answers a prefix that
  is not a whole key with the *siblings* beginning with it, so `S-BIAD599/0177`
  looked populated when only `S-BIAD599/01776a3a-…/` existed. `exists`/`isdir`
  returned confident nonsense while `ls` raised. It bites hardest in the case this
  view exists for: OME-Zarr chunk directories are named `0,1,10,100,…`, so a
  *missing* chunk `1` resolved as a directory whenever `10` existed — and zarr needs
  a `FileNotFoundError` there to fall back to the fill value.
* **`ls()` of a derived *file* raised**, which breaks `find`/`walk`/`du`/
  `get(recursive=True)` — precisely the calls DVC's directory pull makes. fsspec
  requires `ls(file)` to return that one entry; the base does it and the override
  did not.

Plus: the accession pattern refused real accessions (`E-MTAB-1234` is a public
BioStudies study, on a *third* sharding scheme); the completeness check took
`recordsFiltered` from every page, so a trailing `recordsFiltered: 0` could disarm
it; and the file listing had no page ceiling.

**The recurring lesson, now three for three:** the bug is never in the part the
archive documents. It is in what the API does when you ask it something slightly
off-centre — a prefix that is not a key, a page past the end, a study that lists a
file twice — and the failure is a confident wrong answer rather than an error.

---

## D14 — 🩹 `--no-download` **downloads**, wherever the archive publishes no checksum

Found while writing [`importing-from-bia.md`](importing-from-bia.md), by timing the
commands rather than trusting them.

`dvc import-url --no-download bia://…/EF83427BD6_8bit_lynEGFP.tif` records
`md5: 26c0911fdf86d8a176539f5515eab32f`, which is **correct** — it matches
`curl … | md5sum` byte for byte. It is correct because DVC streamed all 120 MB to
compute it: 83 s, no cache written, nothing kept. The path is
`dvc_data.hashfile.hash._hash_file`: our `info()` has `md5: None`, the filesystem
exposes no `md5` attribute for DVC to call, so DVC falls through to `file_md5()`,
which opens the file and reads it.

This is not BIA-specific. It is the behaviour of **every checksum-less tier** —
`bia://` submitted, `geo://` without a sidecar — and it means the cost model people
assume from the Zenodo/EGA examples (`--no-download` is metadata-only) is wrong for
half the backends. Importing all 584 files of `S-BIAD599` "without downloading"
would move the whole ~60 GB study, then move it again on `dvc pull`.

Not fixed, on purpose. The alternatives are all worse than documenting it:

* Making the DVC wrapper expose `md5()` returning `None` gets DVC to record the
  *string* `"None"` as the hash — a fake checksum, silently.
* Synthesising a metadata fingerprint (size + FIRE ETag) and calling it `md5` puts a
  value in the `md5` field of a committed `.dvc` file that is not the md5 of
  anything. That is exactly the class of confident wrong answer the rest of this
  document is about avoiding.
* A distinct `PARAM_CHECKSUM` per backend would let the checksum-less tiers record
  an honest `etag:` instead, but DVC's `Meta.from_info` only understands its own
  field names, so this needs work upstream — [`ideas.md`](ideas.md) #16.

So: the honest answer is that download-free checksums require the archive (or a
`.chiro` manifest) to publish one, and where neither does, `--no-download` buys you
provenance, not bandwidth. The guide says so in those words.

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
  test asserts the chain advertises no route the policy has closed (D16).
  `require()` reports *all* missing credentials at once rather than one per attempt.
* **Missing credentials are reported for the whole set**, and a `Credential`
  declares whether it is a secret. Since D16 that declaration drives refusal at the
  door rather than a post-hoc check; `committed_secrets()` survives for auditing
  history written before the policy.
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

---

## D15 — the archive-as-a-DVC-remote pattern is dropped, though it works

**What happened.** While writing the BIA guide I found that
`dvc remote add bia bia://S-BIAD599` + `dvc import-url remote://bia/<path>` works,
and documented it as a way to avoid repeating a long URL prefix. That was a
discovery, not a design goal — the intended pattern was always plain
`dvc import-url bia://<accession>/<path>`.

**Why it is now refused in the docs.** Two reasons, in order of weight:

1. **Push cannot be abstracted.** A DVC remote is a read/write contract. Depositing
   into a scientific archive is not a file upload: it is accession reservation,
   metadata registration, and a curation workflow that differs completely between
   EGA, Zenodo, GEO and BIA. We could make `pull` work everywhere and `push`
   nowhere. A remote that silently honours half of DVC's contract is worse than one
   that does not exist, because the half that fails does so after the user has
   organised their project around it.
2. **It hides the accession.** With a remote, the committed `.dvc` records
   `path: remote://bia/…` and the accession moves into `.dvc/config`. Still
   reproducible — that file is committed too — but a reader of the `.dvc` file alone
   can no longer see which study the data came from. That is the exact property the
   whole project exists to provide.

**Cost of dropping it: none.** `--fs-config key=value` carries every storage option a
remote could have configured (verified live with `view=derived` on BIA and
`extract_zips=true` on Zenodo), and keeps the accession in the artifact. The only
thing lost is brevity when importing many files from one record.

**What was done rather than just deleting the recipes.** `dvc remote add` with an
archive URL still *works*; deleting the documentation would leave a reader to
rediscover it and hit the push wall alone. So `using-with-dvc.md` states plainly that
it is unsupported and why. The two live integration tests that drove DVC through a
named remote (`zip transparency`, `bia derived import`) were rewritten to use
`--fs-config`, so the regression coverage sits on the path we ship rather than the
one we discourage.

**Changing my mind** would mean deciding that pull-only remotes are worth shipping —
at which point the honest form is a distinct, clearly read-only concept, not DVC's
remote.

---

## D16 — 🩹 credentials come from the environment or a private file, and nowhere else

**What the design said.** The precedence chain was
`storage_options` (i.e. `.dvc/config.local`) → environment → on-disk file, with the
invariant "no secret enters the committed artifact" documented, tested for in the
error message, and backed by `committed_secrets()` — which nothing called.

**What was actually true.** The invariant was a request, not a rule. Measured:

```console
$ dvc import-url --no-exec --fs-config password=hunter2 -- ega://EGAD…/EGAF… b.bam
$ cat b.bam.dvc
deps:
- path: ega://EGAD00001006673/EGAF00004908765
  fs_config:
    password: hunter2          # in the file you are about to commit
```

`dvc remote modify ega password …` did the same thing into `.dvc/config`. archivefs
could not distinguish the two at the filesystem layer either, because `fs_config`
and `.dvc/config.local` arrive as the same `storage_options` dict.

**What changed.** Credential keys were removed from `_EXTRA_CONFIG_KEYS` in
`dvcfs.py`, and `CredentialResolver` refuses an option-supplied credential in its
constructor. The chain is now **environment variable → private on-disk JSON file**.
Verified against the real CLI:

| attempt | result |
|---|---|
| `dvc remote modify e password …` | `extra keys not allowed @ data['remote']['e']['password']` |
| `dvc remote modify --local e password …` | same |
| `dvc remote modify e username …` | same |
| `dvc import-url --fs-config password=…` | `extra keys not allowed @ data['password']` |
| `archivefs ls -o password=…` | `CredentialSourceError`, naming `$EGA_PASSWORD` and the two files |
| `layout`, `credentials_file` | still accepted |

**The three that are load-bearing:**

* **DVC does the enforcing for the first four.** It validates remote config and
  `--fs-config` against the schema we register, so an absent key is a config *parse*
  error, raised before any archivefs code runs. That is why this is a fix rather
  than a convention: there is no code path in which we are trusted to behave.
* **`username` went too, though it is not a secret.** A credential set assembled
  half from a committed option and half from the environment is the configuration
  nobody can debug. Both halves now come from one place.
* **`.dvc/config.local` is refused as well**, even though git ignores it. It is
  still a real file on a shared filesystem with default permissions.

**Two things kept deliberately.** `credentials_file` (EGA) and `ngc` (dbGaP) remain
settable, because they are *paths*. This is ssh's distinction exactly: there is no
`ssh -o Password=`, there is `IdentityFile`. A path in a committed config says which
account is expected without saying anything secret, and it is what makes two
projects with two EGA accounts possible now that `username` cannot be configured.

**File modes are checked too.** A credentials file readable by group or other is
refused with `chmod 600 <path>`, as ssh does with a private key. Refused rather than
warned: a warning in a PBS job log is invisible at the moment it matters. The known
cost is that **pyega3 may have left `~/.pyega3/credentials.json` at 0644**, so an
existing setup needs one `chmod` on upgrade. That was judged acceptable against a
credential silently readable by everyone in the project group.

**Changing my mind** is a one-line edit per key in `_EXTRA_CONFIG_KEYS` plus deleting
`_refuse_option_credentials`, so nothing here is hard to walk back — but doing so
re-opens `--fs-config password=…` writing plaintext into a committed file, which is
the thing that motivated it.

---

## D17 — ⚠️ **`dvc pull` can only reach the archive for a *partial* import**

**The requirement this threatens.** The project exists so a reviewer or reproducer can
clone an analysis repo and pull the raw data **from the archive**, as tracked, without
the authors having to re-host a copy on a DVC remote. Anything that quietly requires
re-hosting defeats the purpose.

**What is actually true**, measured against DVC 3.67.1 on the live archives. Whether a
fresh clone with **no DVC remote** can materialise the data:

| import | `outs.md5` | `deps.checksum` | `dvc pull` in a fresh clone |
|---|---|---|---|
| `import-url` (full download), zenodo | ✓ | ✓ | ✗ **fails** |
| `import-url` (full download), geo | ✓ | ✗ | ✗ fails |
| `import-url --no-download`, zenodo | ✗ | ✓ | ✓ **works** |
| `import-url --no-download`, bia derived | ✗ | ✓ | ✓ works |
| `import-url --no-download`, geo | ✗ | ✗ | ✗ fails |
| `import-url --no-download` + `.chiro`, geo | ✗ | ✓ | ✓ works |

Two conditions, **both** required: the stage must be a *partial* import, and the
dependency must carry a published `checksum`.

**Where the first comes from.** `dvc/repo/index.py`, `_load_storage_from_import`:

```python
if out.stage.is_repo_import or not out.hash_info or dep.fs.version_aware:
    storage_map.add_remote(FileStorage(key, dep.fs, dep.fs_path, read_only=True))
```

`out.hash_info` is `outs[0].md5`. When it is **present** — a completed download — DVC
does not register the source as a fallback remote at all. It assumes a finished import
means the bytes belong in your DVC remote. The failure is then
`Checkout failed … Is your cache up to date?`, because `.dvc/.gitignore` lists
`/cache`: the clone starts empty and there is nowhere to fetch from.

Note what this is *not*. The checksum is known — it is sitting in the committed
`.dvc`. The clone knows exactly which bytes it wants. What it lacks is a **source**.
The more complete the import, the less able a reviewer is to pull it.

The second condition is empirical: rows 5 and 6 differ only in `deps.checksum`, and
only row 6 works. Without it the fetch reports "Everything is up to date" and
transfers nothing.

**`version_aware = True` does not solve it, and makes things worse.** That third
disjunct above looks like the answer, so it was tried: set `version_aware` and add the
`version_path` shim DVC then demands (a no-op is semantically right for us — a
published record is immutable, so the path *is* the version). Result: full download
still could not be pulled in a fresh clone, for zenodo *or* geo, **and** DVC wrote
`push: false` into the `.dvc` (`dvc/repo/imp_url.py` sets `can_push = False` for a
version-aware dependency), which would break the one workflow that does work. Closed.

**What does work, and is now the recommended pattern:** `--no-download` **followed by
`dvc pull`**.

```console
$ dvc import-url --no-download zenodo://1486184/github-mark.zip logo.zip
$ dvc pull                       # the bytes, locally, verified
$ git diff logo.zip.dvc          # unchanged — the source-fallback shape is preserved
```

`--no-download` does not mean "do without the data"; it means "do not record an out
hash". You then pull, which gives you the bytes in the workspace and the cache, leaves
the committed `.dvc` byte-identical, and keeps the stage in the shape a reviewer can
pull from. Verified end to end: clone with no remote configured anywhere → `dvc pull`
→ correct md5.

**Consequences for the checksum-less archives.** `geo://` and `bia://` submitted files
still need a `.chiro` manifest to supply `deps.checksum`; without one, no reviewer
route exists and the `--no-download` step streams the whole file anyway (D14) before
`dvc pull` fetches it a second time. For those archives a manifest is not a nicety, it
is the thing that makes the reviewer story work at all.

**`dvc push` remains available and pushes archive data**, unlike `dvc import` from
another DVC repo — measured: `import-url` + `push` puts the object in the remote and a
clone pulls it, while `import` + `push` reports "Everything is up to date" and pushes
nothing. That works for every archive regardless of checksums, but it means hosting a
copy, which is what we are trying to avoid.

**Tripwire.** `can_push` is only forced off for a version-aware dependency, so our
hard `version_aware = False` (D6) is what keeps `dvc push` working. A future change
that makes archivefs version-aware would silently stop `dvc push` pushing archive data
and break every collaborator's pull. The test pinning `version_aware is False` now
says so.

**Where this should go next.** The gap is upstream: DVC has no way to say "this
dependency's source is immutable and re-fetchable, use it as a fallback even for a
completed import". That is a feature request against DVC, and a natural companion to
the storage-backend plugin hook (D4) — see [`ideas.md`](ideas.md).

---

## D18 — a `chiro=` manifest may be a file committed in your own repo

**The gap D17 left.** For `geo://` and `bia://` submitted files the archive publishes
no checksum, so `deps.checksum` — and with it any reviewer's ability to `dvc pull` from
the archive — can only come from a `.chiro` manifest. But a manifest for *someone
else's* deposit has to live somewhere, and the deposit is the one place it cannot go.

Before this change, `chiro=` resolved a value that was not a URL or an absolute path as
a path *inside the archive*, so the obvious thing failed in a way that read like a
different problem entirely:

```console
$ dvc import-url --fs-config chiro=GSE176078.chiro.json -- geo://GSE176078/… out
ERROR: GSE176078.chiro.json is named as this deposit's manifest but could not be
fetched ('GSE176078.chiro.json' is not a GEO accession…)
```

That left an absolute path (machine-specific, so useless to anyone you send the repo
to) or an `http(s)` URL — which works, but means hosting a copy of a file whose natural
home is the analysis repo, next to the `.dvc` it describes. "Set up somewhere to host
manifests" is a large answer to a small problem.

**Decision.** `load_manifest` now tries a **local file** before falling back to a path
inside the archive. Resolution order, first hit wins: the passed-in `info` (a manifest
that is a member of the deposit being listed) → a URL or absolute path → an existing
local file, `~` expanded → a path inside the filesystem. So:

```console
$ cp …/GSE176078.chiro.json .                # committed beside the .dvc
$ dvc import-url --no-download --fs-config chiro=GSE176078.chiro.json -- \
    geo://GSE176078/GSE176078_Wu_etal_2021_bulkRNAseq_raw_counts.txt.gz bulk.txt.gz
$ dvc pull && git add -A && git commit -m "GEO import with committed manifest"
```

and a reviewer who clones that repo — no DVC remote, no hosted manifest, nothing to
configure — runs `dvc pull` and gets the bytes, verified. Measured end to end on
GSE176078, which is the case that motivated it.

**Why local wins over deposit-relative**, given local is checked first: there is
nothing to shadow. A manifest that genuinely is a member of the deposit arrives through
the `info` short-circuit, because `_members()` passes `native.get(manifest_path)`; and
a bare deposit-relative name never resolved anyway, since the fallback resolves from
the filesystem *root* rather than the container. A test pins the precedence with a
deliberate local decoy, so the depositor's own manifest still wins.

**The error message also got the fix it needed.** It now names every place it looked,
absolute, so "which directory did it mean?" is answerable:

```
nosuch.chiro.json is named as this deposit's manifest but could not be fetched (…).
Looked for /home/you/project/nosuch.chiro.json (local file), then nosuch.chiro.json
(inside the archive). Fix or remove the `chiro` setting, or pass chiro=False …
```

**What this does not change.** A manifest you write over someone else's deposit records
*your* observation, not the archive's claim — chirograph types it `observed-download`,
and that distinction is worth stating when the checksums are offered as evidence. It is
strictly better than no checksum, and it is the best available for an archive that
publishes none, but it warrants differently from a Zenodo md5. A registry of published
manifests would be a different thing again, and is not needed for this: see
[`ideas.md`](ideas.md) #5 for the writing side.

---

## D19 — 🩹 a record you may not read is not an empty directory

**Found by trying `zenodo://10.5281/zenodo.18917100`.** It listed nothing, exit 0.

Zenodo answers a restricted record with **HTTP 200**, full metadata, and an *empty*
`files` list — not 401, not 403:

```console
$ curl -s https://zenodo.org/api/records/18917100 | python -c '…'
title:   Spatial Transcriptomics Data for Breast Cancer TLS and HEV Analysis
files:   0 entries
metadata.access_right: restricted
```

`_load_members` took `files: []` at face value, so `ls` reported an empty record and
`archivefs`/DVC had nothing to complain about. That is the worst available outcome:
`dvc import-url zenodo://<recid> dir/` on such a record writes an empty `.dir` and
**exits 0**, so a pipeline imports nothing and reports success. The failure only
surfaces later, as an analysis with no input.

**Decision.** An empty file list is an error when `metadata.access_right` is
`restricted`, `embargoed` or `closed`, naming the access level, the embargo date when
there is one, and how to supply a token. An empty list on an *open* record stays empty:
a metadata-only deposit is a legitimate thing, and a test pins both directions.

This is the same principle the BIA backend already applied to short listings —
*refuse a partial answer rather than silently hand over fewer files than the record
has* (D13) — extended to the case where the partial answer is "none at all".

**Two smaller things it exposed.**

* **The remediation was stale.** It advised `dvc remote modify --local <remote>
  token …`, which D16 made impossible — DVC now rejects the key outright. It now
  points at `$ZENODO_TOKEN` or `~/.config/archivefs/zenodo.json` and says the setting
  cannot go in DVC config at all.
* **"Rejected the credential" was reported when none was sent.** The 401/403 path keyed
  its wording on whether the *backend declares* a credential, so an anonymous request
  to a restricted record produced `Zenodo rejected the credential (credential came
  from: token from not found)`. It now keys on whether a credential was actually put on
  the request, which is the honest signal and matters for any backend whose credential
  is optional. Same distinction as D18's BIA fix, one layer down.

---

## D20 — 🩹 an **expanded** ZIP cannot be pulled; import zips opaquely if a reviewer must

**Found by checking a report that `dvc import-url` "downloaded ZIP files without
expanding them".** It had not: `extract_zips` is off by default, exactly as
`design.md` specifies ("Opt-in, not automatic. Some ZIPs are genuine archival units
and shouldn't" be sprayed across a workspace) and as `plan.md` records. All three
documented routes to expansion work, verified on `zenodo://1486184`:

| route | result |
|---|---|
| `dvc import-url --fs-config extract_zips=true -- zenodo://1486184/github-mark.zip tree` | `tree/{github-mark.svg,md5sum}`, dep is a `.dir` with `nfiles: 2` |
| the same on a whole record | `record/github-mark.zip/{…}` — the zip becomes a *directory* keeping its `.zip` name |
| `zip://::zenodo://1486184/github-mark.zip` through plain fsspec | `ZipFileSystem`, members listed |

**What the check did find.** Adding `--no-download` to an expanded-zip import writes
a stage that **cannot be materialised at all** — not by a reviewer, not by you:

```console
$ dvc import-url --no-download --fs-config extract_zips=true -- \
    zenodo://1486184/github-mark.zip tree
$ dvc pull
ERROR: failed to pull data from the cloud - Checkout failed for following targets:
tree
```

Fetch transfers nothing, then checkout fails. The filesystem is not at fault:
`archivefs get -o extract_zips=true zenodo://1486184/github-mark.zip/md5sum` reads a
member fine, and so does plain `fsspec.filesystem("zenodo", extract_zips=True)`. The
missing piece is a per-member checksum — a ZIP member publishes no md5 (D9: member
integrity is CRC32 from the central directory plus the workspace hash at extraction),
so the recorded `.dir` gives DVC nothing to fetch against, and the source-fallback
that makes an ordinary partial import pullable (D17) has nothing to work with. A
*plain* directory partial import is pullable, which is what localises this to zip
expansion.

**Decision: document and pin rather than work around.** Two tests hold both halves —
the expanded partial import fails, the expanded full import succeeds — so a future
DVC that fixes this fails the negative test and tells us to update this entry.

> **Corrected 2026-08-21 — this is our gap, not DVC's.** The paragraph above says
> "a future DVC that fixes this", which was the wrong diagnosis. A probe
> ([`evidence/zip-member-md5-pull-probe-2026-08-21.md`](evidence/zip-member-md5-pull-probe-2026-08-21.md))
> forced a per-member md5 into `_zip_info` and re-ran the exact sequence: `dvc pull`
> then materialises the tree from Zenodo — in the same repo, from a stage written
> *before* the change, and in a fresh `git clone` with no remote. The `.dvc` file is
> byte-identical either way, so nothing about the recorded format needs to change and
> already-committed stages become pullable retroactively. DVC needs no fix.
>
> The real cause is that `_zip_info` sets `md5 = None`, which is the same thing
> `geo.py` does for the same principled reason — never substitute a token that is not
> a content checksum. So ZIP members are in GEO's position, and the answer is GEO's
> answer: a `.chiro` sidecar. The gap is that the chiro layer runs at *record* level
> (`_resolve_with_manifest` fills the container's member cache) and never reaches ZIP
> interiors. The probe's `md5(view.cat_file(member))` must not ship — computing a
> member hash inside `info()` would turn a download-free listing into a full download
> of the archive.
>
> So the trade-off below is real **today**, but it is not inherent, and the negative
> test pins our behaviour rather than DVC's. See `ideas.md` #0d.

**The guidance that follows, and it is a real trade-off.** Combining the D17 reviewer
route with ZIP expansion is not currently possible:

* **Expansion, no reviewer route.** Full `dvc import-url --fs-config
  extract_zips=true`. Bytes land at import; anyone else needs a DVC remote you have
  pushed to.
* **Reviewer route, no expansion.** `--no-download` then `dvc pull` on the ZIP as an
  opaque file. The archive publishes the *container's* md5, so this is download-free,
  pinned, and pullable from the archive by anyone. Expand it in the pipeline
  (`unzip`, or `zip://::` in the code that reads it) rather than at import.

The second is the better default for a published analysis, and it is what D9 already
implied — "if you want a genuinely download-free pin, import the ZIP" — now with the
reviewer consequence spelled out.

## D21 — 🩹 a listing and an `info()` of the same expanded ZIP must agree; the CLI takes no chained URLs

**Found by checking a report that neither `archivefs ls -o extract_zips=true
zenodo://<recid>` nor `archivefs ls zip://::zenodo://<recid>` "was able to list
inside the zip files".** Neither was a ZIP-expansion failure, and both were real
defects — one in the code, one in what the docs promise.

**The listing bug.** `ZenodoFileSystem.ls` flipped only `type`:

```python
{**e, "type": "directory"} if name.lower().endswith(".zip") else e
```

leaving the container's md5 in the `md5` field of something it had just called a
directory — the exact thing `_container_as_directory` exists to prevent, and whose
docstring, one screen away, says why: "leaving the archive's md5 in the `md5` field
would offer a consumer a checksum for something it is not going to write." So
`ls --json` reported `type: directory` with `md5: 49f6a914…` while `stat` on the
same path reported `md5: null, container_md5: 49f6a914…`. `archivefs ls -l` printed
a checksum next to a directory. `ls` now routes ZIP entries through that helper; it
reads the member cache `super().ls` has just populated, so it costs no requests and
does not open the ZIP — a second test pins that, because reaching for `zip_view`
here would open every ZIP in a record just to list it.

No test caught this: the only coverage asserted `type == "directory"` and stopped.
The replacement asserts the whole entry equals `info()` on the same path — an
equality, so it cannot rot into checking one field again.

**The listing that looked broken but wasn't.** `ls` is children-only, as fsspec
requires, so a record-level listing shows the ZIP as one entry whether expansion is
on or off. Byte-identical output for both, and before the fix `-l` showed a size and
an md5 too, so there was *no* signal available to the user that expansion was
working. Now `-l` shows `-` in the digest column, which is the tell; `-R`, or
addressing the ZIP directly, is how to see inside. Written up in `cli-ls.md`.

**The chained URL.** `zip://::zenodo://…` is an fsspec/Python form only — verified
working through `fsspec.core.url_to_fs` — but the CLI refuses it, and refusing is
right: chained storage options nest per protocol and a flat `-o key=value` cannot
say which protocol an option belongs to. The bug was that `zenodo.py` listed it in
the same URL-grammar block as the four forms that *do* work anywhere, so a reader
would reasonably type it at a shell and get `unknown protocol 'zip'` — a dead end
that names neither of the two forms that work.

**Decision: keep the CLI protocol-only, and make the refusal do the teaching.** It
now names the `-o extract_zips=true` equivalent and the `url_to_fs` form, both
verified before shipping. Widening the CLI to accept chains would mean inventing a
syntax for per-protocol options, which is a real feature and not one anybody has
asked for. The URL-grammar block now marks the chain as Python-only.
