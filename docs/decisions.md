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

## D6 — 🩹 A **record-level DOI** is not DVC's `version_id` — but a per-entry token is

> **Superseded 2026-08-24 — the conclusion below was too broad, and it is the root of
> D17 and D20 as well.** What this entry got right: a *version DOI* is the wrong value
> to put in `version_id`. What it got wrong: concluding from that that the whole
> `version_aware` mechanism is unusable for us. It is usable, it is the intended
> escape hatch for exactly our situation, and turning it on is what makes an import
> pullable by a reviewer with no DVC remote — including an expanded ZIP, which D20
> says is impossible. **Now implemented — see D23**, which supersedes the decision
> recorded here. The reasoning below is kept because it is where the wrong turn was
> taken and the shape of the mistake is instructive.

**Found by breakage.** Emitting `version_id: 10.5281/zenodo.1486184` in `info()`
made a beautiful `.dvc` file — and then `dvc pull` failed with
`'ZenodoDVCFileSystem' object has no attribute 'coalesce_version'`. DVC treats
*any* `version_id` in a `Meta` as "this is a cloud object version", flips the
filesystem into `version_aware` mode, and expects the S3/GCS/Azure versioning API.

**Decision (as shipped).** The token lives under our own key, `archive_version`, plus
inside the per-archive metadata block (`zenodo.doi`, and so on). The DVC wrapper
forces `version_aware = False` and provides a defensive no-op `coalesce_version` so a
`.dvc` written before this change does not crash.

**What is lost:** the version DOI no longer appears in the committed `.dvc`. In
practice the pin is the `md5`, and for a *version* URL the path itself names the
immutable record. It matters only when the URL is a **concept** DOI, where the
`.dvc` cannot record which version was resolved.

### Where the reasoning went wrong

**A DOI is a *record* fact; `version_id` is a *per-entry* field.** Putting the record's
DOI on every member said "all these files are the same object version", which is not
what DVC asks. The missing move was not to abandon `version_id` but to give each entry
a token that identifies *that entry*: the published file md5 for a plain Zenodo file,
and `(container md5, member CRC32)` for a member inside a ZIP. Both are honest — they
name exactly one byte-string inside exactly one immutable record.

**`version_id` is not a checksum field**, which is what makes this legitimate rather
than a fudge. A ZIP member's `md5` can stay `None` — so D8's rule, restated in
`geo.py`, *never substitute a token that is not a content checksum*, is untouched —
while `version_id` carries the immutability token DVC actually wanted.

**The API surface DVC then demands is one method, not a family.** The concrete failure
is `AttributeError: version_path` from `dvc/dependency/base.py:53`. Identity is the
right implementation: a Zenodo record id already pins the version, so
`version_path(path, version_id) == path`. `coalesce_version` was the symptom this
entry generalised from, and the no-op it already has is also correct.

**What it costs**, and this is real: DVC sets `can_push = False` for a version-aware
dependency (`dvc/repo/imp_url.py`), so `push: false` lands on every out. Accepted — if
the bytes can be pulled from Zenodo again, a second hosted copy is not needed — but it
removes the DVC-remote backup route, which D17's last paragraph relies on.

**The concept-DOI gap this entry named is still open** and is now the *only* thing
`archive_version` is carrying that `version_id` could not: which version a concept DOI
resolved to. That remains a `dvc update`-semantics question, not a storage one.

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

> **Superseded 2026-08-24 — the measurements below all stand; the conclusion drawn
> from them does not.** "`--no-download` then `dvc pull`" is the best route *available
> today*, but it is not the only possible one, and the reason this entry thought
> otherwise is a flaw in one experiment — see "Why the `version_aware` attempt failed"
> below, which replaces the paragraph that closed it. In particular **the tripwire at
> the end of this entry is now wrong in its reasoning**, and the test it describes was
> pinning a decision rather than a fact. **Implemented — see D23**, which is where the
> current guidance lives. `--no-download` is no longer required for `zenodo://` or
> `ega://`.

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

### Why the `version_aware` attempt failed, and what fixes it

That third disjunct above looks like the answer, so it was tried: set `version_aware`
and add the `version_path` shim DVC then demands (a no-op is semantically right for us
— a published record is immutable, so the path *is* the version). Result: full download
still could not be pulled in a fresh clone, for zenodo *or* geo, **and** DVC wrote
`push: false` into the `.dvc` (`dvc/repo/imp_url.py` sets `can_push = False` for a
version-aware dependency). This entry recorded that as "closed".

**It was not closed; the experiment was incomplete.** Flipping the flag restores the
source as a fallback storage, but a second gate then throws the work away.
`dvc/repo/fetch.py`, `_log_unversioned`:

```python
if entry.meta and not entry.meta.isdir and entry.meta.version_id is None:
    unversioned.append(...)          # dropped from the fetch
else:
    index[key] = entry
```

Once a `FileStorage` remote is version-aware, **every entry whose `Meta` carries no
`version_id` is silently removed from the fetch** — a `logger.warning`, not an error.
The attempt above set the flag but never emitted a `version_id`, so every entry was
dropped, nothing was fetched, and checkout failed. The observed symptom is identical to
"the fallback was not registered", which is why it was misread. *(Mechanism read from
DVC 3.64.2's source, not re-measured — the re-measurement is the corrected experiment
below, which succeeds.)*

**With a per-entry token it works.** Adding the third ingredient — a `version_id` on
each entry, emitted from `_zip_info` and from `info()` — makes a **completed**
`import-url` pullable in a fresh clone with no remote, no cache and no manifest, for a
plain file *and* for an expanded ZIP (which D20 says is impossible). DVC writes the
per-member checksum map inline into the committed `.dvc` as a `files:` list, git-tracked
and travelling with the clone:

```yaml
deps:
- path: zenodo://1486184/github-mark.zip
  fs_config: {extract_zips: 'true'}
  files:
  - relpath: github-mark.svg
    md5: 07883e93734b98cae0f7b9c55d287250
    version_id: 49f6a914ea33912471512ac9228c6923:b8576d45
outs:
- md5: fed36ce076379aa131932699d4879eb5.dir
  push: false
```

So **the row of the table above that says a full download cannot be pulled is a
statement about our filesystem, not about DVC.** Every row still reproduces as written;
what changes is that the constraint is ours to lift. D6 has the corrected reasoning
about why a DOI was the wrong token and a per-entry one is the right one.

**`push: false` is the price**, and it is genuinely a loss — see the closing paragraphs
of this entry, which depend on `dvc push` working. Accepted on the grounds that an
archive we can re-pull from does not need a second hosted copy. That trade is the
subject of issue #1, not settled here.

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

**Tripwire, and it cuts both ways.** `can_push` is only forced off for a version-aware
dependency, so our hard `version_aware = False` (D6) is what keeps `dvc push` working.
Turning version-awareness on therefore **does** stop `dvc push` pushing archive data —
that part of the warning is accurate and measured. What the warning gets wrong is
calling that a reason not to do it: it trades a route that requires hosting a second
copy for one that does not, which is the trade this project exists to make. Read it as
"this is the cost", not "do not go here". The test pinning `version_aware is False`
pins a *decision*, not a fact about DVC, and it should be rewritten to say which — an
unlabelled pin of a reversible choice reads like a discovered constraint, and this one
misled its own author for three days.

**Where this should go next.** Not upstream. This entry proposed a DVC feature request
— "let a dependency declare its source immutable and re-fetchable, and use it as a
fallback even for a completed import" — on the belief that no existing mechanism could
express it. `dep.fs.version_aware` in the disjunct above *is* that mechanism; it just
also requires per-entry tokens, which we were not supplying. See `ideas.md` #0, now
downgraded, and issue #1 for the work that replaces it.

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

## D20 — 🩹 an **expanded** ZIP cannot be pulled *as shipped*; two corrections follow

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

> **Corrected again 2026-08-24 — right that it is our gap, wrong about the fix.** The
> block above concludes that a ZIP member needs an **md5** from somewhere, and that a
> `.chiro` sidecar must therefore supply it. That framing is what made the fix look
> like "a few days plus a manifest format", and it also created the dead end the block
> itself flags: the only way to *compute* a member md5 is to stream the archive, which
> destroys the download-free listing.
>
> There is no need for a member md5 at all. DVC's `version_id` is **not** a checksum
> field, and populating it is enough to make the fetch work — so `md5` stays `None`,
> `geo.py`'s rule stands, nothing is streamed inside `info()`, and no manifest is
> involved. `(container md5, member CRC32)` is available from the central directory for
> free and names exactly one member of exactly one immutable archive. Measured: a
> completed expanded-ZIP import then pulls in a fresh clone with no remote and no cache.
> The prerequisite is `version_aware = True`, which D6 and D17 wrongly ruled out; see
> both, and [issue #1](https://github.com/swarbricklab/archivefs/issues/1).
>
> The two negative tests below therefore pin **our current behaviour**, and inverting
> them is part of issue #1 rather than something to wait for.
>
> **Both were inverted on 2026-08-24; see D23.** A completed expanded-ZIP import now
> pulls in a fresh clone with no remote, verified live, and `--no-download` combined
> with expansion is no longer a trap either — it is merely pointless.

**The guidance that follows.** ⚠️ **Superseded as advice, still accurate as
description.** Combining the D17 reviewer route with ZIP expansion is not possible
*with the code as it ships today*:

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

**Do not read this as an architectural recommendation**, which is how it was written and
how it has since been quoted. Our first real user wants a Zenodo ZIP to arrive as a
directory, and told us so; on Zenodo a ZIP is the *only* way a deposit can express
directory structure at all (a flat namespace — 55 of 5592 keys contain a `/`, all from
the GitHub-release integration). "Import the ZIP opaquely and unzip it in a stage" asks
every user to write the same uninteresting stage, and issue #1 removes the need for it.

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

---

## D22 — archive capabilities are **declared once** in `registry.Backend`, and the tables derive from it

**Found by counting.** Adding an archive meant editing at least six places:
`registry.PROTOCOLS`, **four** parallel protocol-keyed dicts in `dvcfs.py`
(`_WRAPPERS`, `DVC_PROTOCOLS`, `_EXTRA_CONFIG_KEYS`, `_BOOL_KEYS`), the entry points
in `pyproject.toml`, the backend class, a hardcoded key tuple in `chiro.py`, and
capability tables kept by hand in `docs/`. Missing one fails in a way that does not
point at what you forgot.

**It had already drifted, and this found it.** `chiro.py`'s tuple was
`("url", "ega", "geo", "zenodo", "archive_version")` — no `bia`. So a `bia://`
import resolved through a `.chiro` manifest silently dropped the BIA provenance
block from `info()`: reads still worked, because `url` was in the list, but
`archivefs stat` and `ls --json` lost the accession, view and S3 key. Nobody would
have looked for that in `chiro.py`.

**Decision.** Extend the existing `Backend` dataclass rather than adding a parallel
`Profile` type, and derive the scattered tables from it. `Backend` was already the
right shape — it was just restricted to *packaging* facts. New fields: `config`
(option name → type), `dvc_wrapper`, `published_checksum`, `immutability_basis`,
`expands_containers`. `_EXTRA_CONFIG_KEYS`, `_BOOL_KEYS`, `_WRAPPERS` and
`DVC_PROTOCOLS` are gone or derived; `chiro.py` reads `PASSTHROUGH_INFO_KEYS`, which
is built from `PROTOCOLS` so the omission above cannot recur.

**Two constraints shaped it**, and both are recorded in the module docstring because
they are what stops this becoming a junk drawer:

1. **A profile declares *what*, never *how*.** No field holds a callable; behaviour
   stays in the fsspec subclass. This is why `credentials` is *not* a profile field
   even though it looks like one — the backends' `Credential` declarations carry env
   vars, file paths and help text, and a second copy would drift on the one topic
   where drift is a security bug.
2. **`registry.py` must stay importable with no backend installed**, or `archivefs
   info` cannot report a missing extra without importing the thing that is missing.
   So every field is inert data.

**`_BOOL_KEYS` was the argument for doing this at all.** It existed only because DVC
config is INI, so `extract_zips = false` arrives as the string `"false"` — truthy —
and ZIPs get expanded against an explicit instruction. That is a fact about a key,
recorded in a different structure from the key. Now the type sits next to the name
(`"extract_zips": bool`) and `_remote_schema` maps it to DVC's `Bool`.

**Where a profile cannot tell the truth, it lies safe.** BIA is *mixed*: derived
OME-Zarr objects carry md5s, submitted files carry none. A per-protocol field cannot
say "sometimes", so `bia` records the value that is safe for the weaker view —
`published_checksum=None`, `immutability_basis=None`. The alternative, a per-view
profile, is a real design and not one anything needs yet.

**The field name changed under review.** It was `version_token`, and ruff's
flake8-bandit flagged every assignment as a possible hardcoded password. A false
positive, but a fair one: in this codebase "token" means *credential*, and a field
holding a one-line description of how immutability is established should not borrow
the word. `immutability_basis` says what it is, and `is not None` is the gate.

**What it buys, concretely:** the dangerous switch in D6/D17's correction is one
reviewable field instead of an `if` chain in the DVC adaptor; `archivefs info`
reports capabilities rather than only installed-or-not; and the invariants are
tests — no secret credential may be a config option (compared against the backends'
own declarations, not a comment), no archive may claim an `immutability_basis`
without a published checksum to build it from, no two profiles may share a
`dvc_wrapper` name, and a deferred backend advertises nothing.

**Cost to change my mind:** the fields are additive and nothing outside `registry.py`
constructs a `Backend`, so splitting capabilities into their own `profiles.py` later
is a move, not a rewrite.

---

## D23 — ✅ version-aware imports: a **completed** `dvc import-url` is pullable from the archive

**Supersedes D6, D17 and D20**, and closes the gap `ideas.md` #0 asked DVC to close.
Implemented for `zenodo://` and `ega://`; `geo://` and `bia://` are deliberately
unchanged. Verified against the live archives with DVC 3.64.2 — 20 end-to-end tests,
including a `git clone` with no DVC remote.

**What now works.** The plain thing, with no flags:

```console
$ dvc import-url --fs-config extract_zips=true -- zenodo://<rec>/dir.zip tree
$ git add -A && git commit -m "import"
# …someone else, fresh clone, no remote, no cache, no manifest:
$ dvc pull
A       tree/
2 files fetched and 2 files added
```

This is `design.md`'s line 196 as originally intended, and the first customer's actual
request: *"`dvc import-url zenodo://record/dir.zip` such that pulling unzips it to
`outs.path`."*

**Three changes, no upstream patch.**

1. `version_aware` is derived from the archive profile — `immutability_basis is not
   None` (D22) — instead of hard `False`.
2. `version_path` is the identity. Not a shim: a published record is immutable and its
   id is already in the path, so "the path at this version" *is* the path. S3 needs
   `?versionId=` because a key is mutable; a Zenodo record id is not.
3. Each entry carries a `version_id`. Zenodo files use the published md5; ZIP members
   use `container md5 : member CRC32`, both free from the central directory; EGA uses
   `plainChecksum`.

**Why a ZIP member may carry this while `md5` stays `None`.** `version_id` is not a
checksum field. D20's correction spells it out: the container's md5 authenticates every
member extracted from it, and the CRC32 says which member, so the pair names one byte
sequence inside one immutable record — download-free. `md5` remains the honest
post-extraction hash, so `geo.py`'s rule stands and nothing is streamed inside
`info()`. **No manifest is involved**, which is why `ideas.md` #0d shrank.

**The silent failure this had to avoid.** `dvc/repo/fetch.py`'s `_log_unversioned`
removes every entry whose `Meta` has no `version_id` from the fetch and logs a
*warning*. A blanket switch would therefore make `dvc pull` exit 0 having quietly
skipped files — GEO publishes no digest at all, so every GEO file would qualify. Hence
the per-archive gate, and hence `_check_versioned` in the DVC wrapper, which refuses
such an entry outright. It hooks `info` **and `walk`**: DVC builds a directory index
through `dvc_data.index.build.safe_walk`, so checking `info` alone would have covered a
single-file import and missed every member of a directory.

### What it costs, measured

**1. `push: false` on every out.** `dvc/repo/imp_url.py` forces `can_push = False` for a
version-aware dependency, so the DVC-remote route in D17's closing paragraph is gone.
Accepted deliberately: if the bytes can be re-pulled from Zenodo, a second hosted copy
is not needed. Note this is a *trade*, not a free win — a repo whose archive record is
later restricted or withdrawn no longer has a fallback.

**2. `dvc checkout` needs one `dvc pull` first.** ⚠️ The one genuinely unpleasant
finding, and it was not predicted. A completed import writes the bytes to
`cache/files/md5/…`, but `_load_storage_from_import` points a version-aware stage's
*cache* storage at `cache/fs/<protocol>/<token>`. They disagree, so restoring a deleted
file with `dvc checkout` fails with `Is your cache up to date?` even though the bytes
are cached. One `dvc pull` fills the fs-cache and every later checkout works — so the
cost is **one extra archive fetch per stage, the first time you restore from cache**.

Measured on both branches, because it matters whether this is new: it is. The old
recommended route (`--no-download` then `dvc pull`) populates the fs-cache directly and
checkout works there. This is specific to *completed import* plus version-awareness.
Pinned by a test, and a candidate upstream fix — register both cache storages for that
case — is in `ideas.md`.

**3. `--no-download` plus expansion is no longer a trap, but is still pointless.** It
completes and pulls now, but DVC still streams every member to compute the `files:`
md5s, so it costs the same transfer as a full import and keeps none of the bytes.

**What is still not done.** The `extract_zips` default stays **off**. The reviewer
argument for keeping it off has gone, so what remains is only the weaker one — a ZIP
that is genuinely one archival unit — which the Zenodo survey found no instance of.
Flipping it is now a live question rather than a blocked one, and it would change the
meaning of existing `.dvc` files, so it wants its own decision.

**Cost to change my mind:** set `immutability_basis=None` on a profile and that archive
reverts entirely, tests included. The gate is one field.

## D24 — a ZIP's **wrapper directory is stripped by default**, and `__MACOSX` is hidden

**Found by using it.** A real import of a lab deposit:

```console
$ dvc import-url --fs-config extract_zips=true -- \
    zenodo://10.5281/zenodo.18917100/pathology_annotations.zip pathology_annotations
$ tree
├── pathology_annotations
│   └── pathology_annotations
│       ├── visium
│       ├── visium_hd
│       └── visium_lymph_nodes
└── pathology_annotations.dvc
```

Note what this is *not*: it is not a bad choice of output name. The destination was
typed by hand, and the obvious name is the one that collides. `dvc import-url`'s own
default is `os.path.basename` of the URL path — `pathology_annotations.zip`, a
directory named like a file — so every available name is wrong in some way, and
computing a better one is `dvc/utils/resolve_output`'s job, upstream, before any
filesystem exists. Naming cannot fix this. Only the shape can.

**Why the archive is shaped that way, and why that is correct.** `unzip foo.zip`
extracts into the current directory and creates no wrapper of its own, so a ZIP that is
a bare bag of files detonates into whatever directory the reader was standing in. The
single-top-level-directory convention exists to prevent that, and
[`evidence/zenodo-zip-shape-2026-08-21.md`](evidence/zenodo-zip-shape-2026-08-21.md)
measures ~80% of sampled Zenodo ZIPs following it. So this is not sloppy zipping to be
corrected on our side of the deposit: the wrapper is a service to everyone who
downloads from the record page, and a bare-bag deposit would optimise for archivefs at
their expense. **archivefs adapts; the deposits stay conventional.**

**Decision.** `strip_components`, default **1**: drop leading interior components while
each is the ZIP's sole top-level entry *and* a directory. `0` presents the interior
verbatim. It is per-import, lives in `fs_config`, and is committed like any other
storage option.

**Why the default is on.** The usual objection to a content-dependent default is that
the tree stops being predictable from the URL. Three things answer it here:

* **The cost of being wrong is one directory level**, visible in `tree`, with no data
  loss. That is a different universe from `_log_unversioned` (D23), where being wrong
  means files silently missing. Defaults should be timid in proportion to their failure
  mode, and this one's is benign.
* **D23 already made the outcome legible.** A version-aware import writes an explicit
  `files:` list of relpaths into the committed `.dvc`. Whatever the strip did is in
  git, and changing the rule later shows up as a stage mismatch rather than a
  differently-shaped workspace nobody notices.
* **The dominant use is our own deposits**, where the convention is ours to keep
  uniform, and a strip that announces itself is what says when someone broke it.

**Why it checks instead of stripping blindly.** `tar --strip-components=N` removes N
levels whatever is there, which on the 20% — a flat bag, two real directories, a ZIP
wrapping one file — deletes a sibling. So `strip_components` is a **maximum, not a
promise**, and the failure mode of asking for too much is "nothing happened". The
alternative reading would have made the setting's name honest at the cost of making it
dangerous.

**`__MACOSX` is dropped from listings**, and this is load-bearing rather than tidiness:
30% of sampled ZIPs carry one, and a ZIP holding `outs/` *plus* `__MACOSX/` has two
top-level entries — so without the filter the strip declines on precisely the archives
that most need it, firing in far fewer than 80% of cases. Cruft is **hidden, not
unreadable**: a caller naming a `__MACOSX` path explicitly still gets the bytes.
Hiding is a listing decision, and `find`/`walk` inherit it, which is what keeps
resource forks out of the tracked directory hash.

**The implementation risk was symmetry, not policy.** `_zip_container` maps a workspace
path to `(zip, interior)` and is what `cat_file` and `_open` fetch through. A strip
applied to listings only would present a tidy tree and then read the wrong member — or
nothing. `_to_interior`/`_from_interior` are exact inverses and
`test_stripping_is_symmetric_so_reads_follow_listings` walks every listed path back to
its bytes. Deciding the strip needs the central directory, so
`test_deciding_the_strip_does_not_open_a_records_zips` pins that listing a record still
opens none of its ZIPs.

**Two details that would have bitten.** Directory *markers* (`annotations/`) are
optional in a ZIP and only some writers emit them, so counting them as top-level
entries would make the same deposit expand differently depending on which tool zipped
it — the strip reads file names only. And `strip_components = 0` from an INI config
arrives as the string `"0"`, which is truthy: the same trap `extract_zips = false` fell
into (D22), in the one setting whose entire purpose is *do not rearrange my tree*.
Measured, because the two routes differ: `dvc remote modify` goes through the config
schema, so the profile records `strip_components: int` and `_remote_schema` turns that
into `Coerce(int)` plus a non-negative `Range`. **`--fs-config` does not** — DVC dumps
those pairs into the `.dvc` verbatim, which is why the committed value reads
`strip_components: '0'`, a string. So the schema is not what saves the flag people
actually use; `__init__`'s own `int()` is, and it covers the CLI's `-o` and direct
Python calls too. `test_stripping_can_be_turned_off_per_import` asserts the string in
the file *and* the unstripped tree, so the two cannot drift apart silently.

**What this does not settle.** `extract_zips` still defaults **off**. Both arguments
for that are now gone — the reviewer objection to D23, and the archival-unit worry the
Zenodo survey found no instance of — so it is a live question, but flipping it changes
the meaning of existing `.dvc` files and belongs in its own entry with its own
migration note.

**Cost to change my mind:** `strip_components=0` restores the previous behaviour
exactly, per import. If the default proves wrong, the change is one number in
`registry.PROTOCOLS["zenodo"].config` and one default in `ZenodoFileSystem.__init__`.

## D25 — listing and reading a ZIP get **separate caches**, because they want opposite ones

**Found by a bug report, not by review.** "Downloading big files from Zenodo is very
slow", against an expanded-ZIP import. Instrumenting `_fetch_range` and attributing
bytes by phase:

```
25MB archive, 26 members -> 497.8MB in 611s   (19.9x)
   282.5MB   70 reqs   1. save_deps  (hash the dependency)
   215.3MB   50 reqs   2. download   (materialise the out)
```

Two multipliers, and the one I fixated on first was the smaller.

**2x — DVC reads the dependency twice**, by design (`dvc/stage/imports.py:47`:
`stage.save_deps()` then `stage.deps[0].download(...)`). Pass 1 exists because a ZIP
member publishes no md5 and one cannot be known without decompressing it (D9).

**~9-10x inside each pass** — the actual culprit. `_open_container` used fsspec's
`"bytes"` cache, which holds **one** contiguous range and drops it on any forward jump
longer than a block (`fsspec/caching.py`, `BytesCache._fetch`: `elif end - self.end >
self.blocksize: self.cache = self.fetcher(start, bend)`). `zipfile` reads a member's
local header, then its data, then seeks away — so with 4 MiB blocks, a
few-hundred-byte member cost a full block and nothing was ever reused.

### Two things that look like the fix and are not

**`cache_type="blockcache"`.** Fails
`test_zip_interior_listing_is_download_free` immediately. Block-aligned caching reads
the whole block containing the end-of-central-directory record, so listing goes from
`O(central directory)` to `O(blocksize)` — on a 1.4 GB ZIP, megabytes to answer `ls`.
That is an ~80x regression on the download-free listing the project is built on, traded
for ~10x on reads. The test caught it on the first run.

**`BytesCache(trim=False)`.** The trimming is not the problem; the discard-on-jump
above is, and it happens regardless. A `BytesCache` cannot hold two non-adjacent
members at all.

### Decision

**Two views of the same ZIP.** `zip_view` keeps the exact-range `"bytes"` cache and
serves `ls`/`info`. `_member_view` uses `"mmap"` — a sparse file on local disk,
block-fetched on demand, each block fetched **at most once for the life of the
filesystem object** — and serves `cat_file`/`_open`.

Why `mmap` rather than fetching the container once up front, which was the plan in the
issue: an eager fetch means `archivefs get <1.4GB zip>/<4KB file>` pulls 1.4 GB. Block
fetching needs **no size threshold and no guess about what the caller intends**, which
is the part that took three wrong turns to reach. `test_reading_one_small_member_does_
not_fetch_the_container` is that property.

The two-pass cost disappears as a *side effect*: pass 2 fetched **0 bytes**, because
every block it wants is already local from pass 1.

### Measured

| | before | after |
|---|---|---|
| bytes for one import | 497.8 MB (19.9x) | **25.0 MB (1.00x)** |
| requests | 120 | **9** |
| wall clock | 611 s | **84 s** |
| listing 26 members | 1910 B, 3 reqs | unchanged |

Wall clock is the least trustworthy row: the same import measured 324 s, 323 s and
611 s before the change, so Zenodo's throughput varies ~2x run to run. Bytes and
request counts are the honest metrics.

**The cost, stated as a number rather than waved at.** The reading view re-parses the
central directory through its own block-aligned cache, and the central directory is at
the *end* — so that is a whole extra block. One 7-byte member out of the 25 MB archive
went from 4.2 MB to **8.2 MB**. A single-member `archivefs get` pays about double.
Worth it against 20x on the common case, but a real regression, not a rounding error;
pinned by `test_one_member_costs_two_blocks_at_the_default_size`, with the obvious
follow-up (seed the reading cache from what the listing view already fetched) in
`ideas.md`.

**Also fixed here, because it blocked the measurement:** a range request reaching EOF
was treated as a server fault, retried five times, and reported as
`Zenodo would not serve bytes … reduce concurrency if it persists` — advice that is
actively wrong. `_fetch_range` now clamps to the declared size, and `_fetch_checked`
accepts a short read whose bytes reach `Content-Range`'s own total, which is the only
authority when the declared size is stale.

**Cost to change my mind:** the two views are two dict lookups and one method. Point
`cat_file`/`_open` back at `zip_view` and the old behaviour returns exactly.

## D26 — archives throttle **per connection**, so one block is fetched over several

> **Corrected by [D27](#d27--a-range-is-cut-into-fixed-2-mib-pieces-not-one-piece-per-connection).**
> The conclusion below holds — splitting a range across connections is the win, and 8
> is the right bound. The *reason* given for the 512 KiB floor does not: "the fixed
> cost of a request here is seconds" reads a rate as a price. One connection moves
> ~0.30 MB/s at **any** request size, so the 11.3 s below is 4.2 MB at 0.37 MB/s, and
> the real per-request cost is ~0.1 s. D27 has the measurement and what it changed.

**Measured, three trials each, one 4 MiB block of a Zenodo object:**

| | median | throughput |
|---|---|---|
| one request | 11.3 s | 0.37 MB/s |
| split 4 ways | 4.3 s | 0.97 MB/s |
| split 8 ways | **2.4 s** | **1.76 MB/s** |

Consistent across trials (`12.7/11.3/11.3`, `4.7/3.8/4.3`, `3.2/2.3/2.4`), which
matters because Zenodo's wall clock is otherwise unreliable — the same import varied
323–611 s run to run. The limit is per *connection*, not per client.

**Why the obvious hook does not work.** `MMapCache` (D25) takes a `multi_fetcher` for
fetching several blocks at once, but it only ever passes more than one range when a
single read spans several missing blocks. A sequential reader misses one block at a
time, so there is never more than one range in flight and the hook does nothing. The
parallelism has to come from splitting **within** a range.

**Decision.** `fetch_ranges` fetches many spans of one object concurrently; `split_span`
cuts one span into `max_concurrent_reads` pieces, with a floor
(`MIN_PARALLEL_PIECE = 512 KiB`) below which a range is left whole — the fixed cost of a
request here is *seconds*, so slicing into kilobytes would be pure loss. Zenodo's
member view supplies a `multi_fetcher` that splits every range and rejoins the pieces.

### The bound is one per filesystem, and that is the load-bearing part

`dvc_data.hashfile.build._hash_files` already runs up to `HASH_JOBS` threads. Each one
splitting a block eight ways independently would put 32 sockets on a free public
archive from a single `dvc pull`. So the semaphore lives in `_fetch_range` — every path
shares it, and splitting cannot multiply with a caller that is already threaded.
`test_concurrency_is_bounded_no_matter_how_many_callers` drives six threads at a bound
of four and watches the peak.

**An honest cost: this caps reads that were previously unbounded.** DVC's transfer
concurrency is `4 * cpu_count()`, so a many-file record import could previously have
had far more than eight reads in flight, and now cannot. That is deliberate — 192
concurrent connections to a free service is not something to ship on purpose, and it
would be throttled anyway — but **the many-small-files case is not measured**, only
argued. `max_concurrent_reads` is a storage option on every backend so it can be raised
without a code change, and that is where to start if a record of many files gets slower.

### What could have gone wrong, and what pins it

A split range rejoined out of order, or short by a piece, yields a member that is
confidently wrong rather than an error — then hashed, committed and trusted. So:

* `fetch_ranges` uses `ThreadPoolExecutor.map`, which preserves order and re-raises the
  first exception. A failed piece fails the read.
  `test_a_failed_piece_fails_the_read_rather_than_leaving_a_hole` pins that, because the
  alternative is silent truncation.
* `test_split_pieces_reassemble_to_the_same_bytes` reads a whole archive with the bound
  at 1 and at 8 and compares byte for byte, and
  `test_splitting_actually_happened` exists so that test cannot pass by never splitting.
* Verified live against a real 25 MB Zenodo archive: 26 members, md5 per member,
  **identical serial and parallel**, twice — once before and once after the refactor
  that moved the `multi_fetcher` into the cache options.

End-to-end speedup on the same 26 members: **2.38x** and **3.51x** on two runs
(52.5s/22.1s and 82.6s/23.5s). Quote the range, not a point — the serial baseline
itself moved by 60% between runs. The 4.7x from the single-block table above is a
micro-benchmark and should not be expected end to end, where central-directory reads
and small members are not worth splitting.

Alignment is not a special case: `_fetch_range` already aligns a range start down and
slices, which is what EGA's 16-byte requirement needs, so piece boundaries can fall
anywhere.

**Not measured on EGA or BIA.** Only Zenodo's member view splits today; the bound
applies everywhere, the splitting does not.

**Cost to change my mind:** `max_concurrent_reads=1` restores single-request reads
exactly, per filesystem, with no code change.

## D27 — 🩹 a range is cut into **fixed 2 MiB pieces**, not one piece per connection

Issue #12 proposed scaling the ZIP block size with the container — 32 MiB blocks on a
20 GB archive, so that eight parallel pieces are 4 MiB each instead of 512 KiB — on the
premise, taken from D26, that "Zenodo's per-request overhead is *seconds*".

**The premise is false, and the proposal is a pessimisation.** Both were measured
before any code was written, which is what the issue asked for.
[`evidence/zenodo-piece-size-2026-08-25.md`](evidence/zenodo-piece-size-2026-08-25.md)
has the four probes; the short version:

| pieces, 8 connections, 64 MiB, 3 rotated trials | median |
|---|---|
| 128 × 512 KiB *(what we shipped)* | 5.76 MB/s |
| **32 × 2 MiB** | **9.70 MB/s** |
| 16 × 4 MiB *(issue #12's proposal)* | 5.05 MB/s |
| 8 × 8 MiB | 2.49 MB/s |

### What D26 got wrong

D26 read "one 4 MiB block takes 11.3 s as a single request" as a per-request price. It
is not: a single connection to Zenodo moves **~0.30 MB/s whatever you ask it for** —
0.25 MiB in 0.96 s, 8 MiB in 26.8 s, a flat line — so 11.3 s was 4.2 MB at the rate.
The real fixed cost of a request is about **0.1 s**. That misreading is what put a
512 KiB floor in `split_span`, and what issue #12 then reasoned from.

Neither cheap explanation for the concurrency effect survived either: the fast
conditions do not open more sockets (7, reused, all to `zenodo.org`), and socket age is
irrelevant (eight sequential 2 MiB reads take 46.4 s on one reused socket and 46.9 s on
a fresh socket each).

### What actually predicts throughput: bytes in flight

Sorting every measured cell by `connections × piece size` gives a single curve with a
peak at **16 MiB in flight** and a cliff past it — and it does not matter which lever
gets you there. 8×2 MiB is 9.70–14.03 MB/s; 8×4 MiB, 16×2 MiB and 8×8 MiB (32, 32 and
64 MiB in flight) are 5.05, 4.08 and 2.49. Below the peak the levers are *not*
interchangeable — 1 connection × 2 MiB is 0.95 MB/s against 4 × 512 KiB's 4.09 — so
both are needed, but above it either one alone ruins the transfer.

That shape on a ~280 ms path looks like a congestion-limited link. **We did not confirm
the mechanism**, and the decision does not rest on one: it rests on the peak.

### Decision

`split_span` cuts a range into pieces of a **fixed size**, `TARGET_PARALLEL_PIECE`
(2 MiB), rather than into `max_concurrent_reads` pieces of whatever size falls out:

    piece  = clamp(MAX_BYTES_IN_FLIGHT / max_concurrent_reads, 512 KiB, 2 MiB)
    pieces = ceil(length / (max_concurrent_reads × piece)) × max_concurrent_reads
    block  = clamp(max_concurrent_reads × piece, blocksize, container / 8)  # mmap only

The division by the connection bound is what keeps a raised `max_concurrent_reads` from
walking into the cliff: at 16 connections the piece becomes 1 MiB, holding the bytes in
flight at the peak instead of doubling past it.

**Whole rounds, and that line was bought the hard way.** The first version cut by piece
size alone, which turns a 23 MB member into 12 pieces — eight connections, then four,
with half of them idle for the second round. Measured end to end it was **slower than
not capping the piece size at all** (1.74 MB/s against 2.08), which is the sort of
result that looks like the whole idea is wrong. Rounding the count up to a multiple of
the connection bound gives 16 pieces of 1.44 MB: the same two rounds, none of them
half-empty.

### End to end, on nine cold archives

Nine sibling `spaceranger_*.zip` from record 17411292 (495–707 MB each), one
configuration per archive, assigned by a Latin square, each read exactly once:

| | median | range |
|---|---|---|
| before | 3.87 MB/s | 2.67–13.33 |
| piece cap only | 8.69 MB/s | 8.54–10.36 |
| **both** | **18.00 MB/s** | 9.26–19.27 |

Path calibration 5.89 MB/s before the run and 6.32 after, so the path held still.
**"Several-fold", not 4.65x**: individual archives vary ~2x whatever the setting, three
archives per configuration is a small sample, and one of nine comparisons crosses over.

The spread is a result in itself. `before`'s pieces are `range / 8` and the range
depends on how `zipfile` happens to read a member — a *stored* member is read whole, so
the pieces are enormous; a *deflated* one is read incrementally, so they are 512 KiB.
Hence 2.67 to 13.33 MB/s across nine archives from one depositor. Capping the piece
tightens that to 8.54–10.36 before the block size adds anything.

**Each archive must be cold, and finding that out cost a run.** The first version read
one archive three times, rotating the order; in both rounds whichever configuration ran
*last* won, calibration flat either side. Zenodo's origin warms to an object across
re-reads and the warming outlives the round, so a repeated-measures design over one
object measures the origin. Both scripts are kept —
[`…-before-after`](evidence/zenodo-split-before-after-2026-08-25.py) for the lesson,
[`…-cold-archives`](evidence/zenodo-split-cold-archives-2026-08-25.py) for the answer.

**The container's size bounds the block**, which is the one part of issue #12's shape
that survived. Not to make blocks *bigger* on large archives — they already reach the
ceiling — but to stop them getting big on small ones: a flat 16 MiB block would mean
one 7-byte member of a 25 MB archive fetching two blocks that between them span the
whole archive, which is D25's eager container fetch arrived at sideways. An eighth of
the container caps a single-member read at a quarter of it, and Zenodo publishes the
size in the record, so consulting it costs no request. A 25 MB archive therefore keeps
the 4 MiB block and the 8.2 MB single-member cost it has today; nothing gets worse than
what shipped.

### The bug this fixes is not the one the issue described

`MMapCache` consolidates every consecutive missing block into **one range**. So reading
a 100 MB member asked for 100 MB at once, and the old rule cut that into 8 pieces of
12.5 MB — 100 MB in flight, four times past the cliff, on the exact case issue #12 was
raised for. Block size was never the variable there: measured end to end on a 395 MB
archive of 23 MB members, 4 / 8 / 16 MiB blocks gave **2.09 / 2.09 / 2.19 MB/s** — a
tie, because the pieces are `member/8` at any block size
([`evidence/zenodo-zip-blocksize-2026-08-25.py`](evidence/zenodo-zip-blocksize-2026-08-25.py)).

Block size still earns its change for the *other* half — members smaller than a block,
where one block is one round of pieces and 4 MiB of it means 512 KiB pieces.

### The cost, as a number

The reading view's block is up to 4x bigger, and D25's single-member cost scales with
it — bounded by the container clamp above:

| container | block | one small member costs | share of the archive |
|---|---|---|---|
| 25 MB | 4 MiB *(unchanged)* | 8.4 MB *(unchanged)* | 34% |
| 395 MB | 16 MiB | 33.6 MB | 8% |
| 20 GB | 16 MiB | 33.6 MB | 0.2% |

A full traversal is unaffected — every block is read once at either size. So the cost
lands where D25's did, on `archivefs get <big.zip>/<one small file>`, and only for
archives big enough that it is a small fraction of them.

An explicitly chosen `block_size` is never enlarged — a caller asking for small blocks
is asking for cheap small reads, and every test that reasons about which blocks were
fetched sets one. `ideas.md`'s "seed the reading cache from the listing view" would
remove one of the two blocks entirely; it is worth more now than it was.

**Cost to change my mind:** `TARGET_PARALLEL_PIECE = 512 * 1024` restores the old piece
size, and `parallel_block_size` returning `self.blocksize` restores the old block. Both
are one line. The measurement is re-runnable in ~10 minutes from the persistent
session, and **must** be re-run rather than trusted: Zenodo's absolute throughput to
this host moved 4x within one afternoon, and every number above is a within-run,
order-rotated comparison for that reason.

---

## D28 — 🩹 a **plain file** is read over several connections too, in bounded windows

Everything D26 and D27 built — `fetch_ranges`, `split_span`, the shared semaphore —
was reachable from exactly one place: the `multi_fetcher` on Zenodo's ZIP member view.
A plain file went out over a single connection, and `cat_file` was worse: it passed the
**whole object** as one range, so reading a 2.5 GB `.ome.tif` was one HTTP request.
Issue #14, found while measuring #12 and filed rather than folded in.

### What it is worth, and what the issue got wrong

Issue #14 predicted **30x**, inheriting #12's numbers. Measured, it is **6.48x** —
and the gap between those two figures is the interesting part.

[`evidence/zenodo-plain-file-split-2026-08-26.md`](evidence/zenodo-plain-file-split-2026-08-26.md)
has both probes. The first, against the 2.53 GB object every earlier measurement used,
found **no effect at all** (0.93x on the range primitive, 1.06x through `fs.open()`) —
and a single request moving 10 MB/s, which is 30x what D27 §2 says one connection can
do.

The trial order explains it: 0.37, 6.52, 20.21, 10.11, 10.02, 9.27 MB/s, in that
sequence. The first fetch took 179.9 s for 64 MiB and nothing afterwards took more than
10.3. **Zenodo's origin warms to an object, and the warming outlives the run**, so
rotating conditions cannot cancel it — the same trap
`zenodo-split-before-after-2026-08-25.py` was kept as a lesson for, one level up: there
it was a re-read of the same *range*, here of the same *object*.

Repeated on the five other objects in that record that nobody had touched, interleaved
16 MiB chunks so both conditions meet the same temperature:

| | median | across five cold objects |
|---|---|---|
| before: one request per range | 0.36 MB/s | 0.33–0.42 |
| **after: 2 MiB pieces, 8 connections** | **2.32 MB/s** | 2.21–2.36 |

**"One connection sustains ~0.30 MB/s" is a property of a cold object, not of a
connection.** Warm, one connection does 8–10 MB/s. D26 and D27 measured a warm object
throughout and were therefore measuring the warm ceiling, which splitting does not
raise because it was never the binding constraint there. Splitting does not make a cold
object fast either — eight connections get 2.32 MB/s out of one, not 9.7–14.0. The cold
object is the limit; concurrency extracts about 6.5x more from it.

None of that changes D27's constants, which were chosen from within-run comparisons at
one temperature. It does mean **the absolute MB/s figures in D26 and D27 describe warm
objects** and should not be quoted for a first read.

### Decision

`fetch_span(info, start, end)` is the seam: it cuts a contiguous run with `split_span`
and fetches it a **window at a time**, one round of `max_concurrent_reads × piece`.
`cat_file` and `ArchiveFile._fetch_range` both route through it, so the streaming path
— `archivefs get`, `dvc import-url`, dvc's hashing — gets it as well.

The window is not an optimisation, it is what makes the change safe. Without it a
whole-object read cuts a 2.5 GB file into 1250 pieces of 2 MiB, all live at the same
instant, and then joins them: the peak would be the file twice over. One round is
`MAX_BYTES_IN_FLIGHT`, so **the bound that keeps memory flat is the same bound that
makes the transfer fast** — which is the only reason a single constant can do both jobs
honestly.

`_open`'s default block now comes from `parallel_block_size(size)` rather than fsspec's
4 MiB class default, on D27's reasoning: a block is one round of pieces, and 4 MiB of it
is eight connections carrying 512 KiB each. The eighth-of-the-object clamp is what stops
that being an eager fetch — a small read of a small file still costs 4 MiB, and a
100-byte peek at a 100 MB file costs 12 MiB rather than a flat 16.

### Failing closed

Assembly places each piece at **its own** offset and checks the length. Appending at a
running offset would absorb a short piece by sliding every later byte left, producing a
file of the wrong length that still looks like data — and it would then be hashed and
committed. `_fetch_checked` already refuses short reads, so this guard should be
unreachable; it is there because "should be unreachable" and "is not written to disk"
are different claims.

`max_concurrent_reads == 1` is unchanged: one request per range, exactly as before.
Opting out of concurrency must not buy a stream of small requests instead.

### Not measured

EGA and BIA inherit this and neither was tested — no live EGA credentials here, and
BIA's S3 path has its own throttling behaviour. What is new for them is only that plain
reads now use a bound (`max_concurrent_reads`, default 8) they already had.

**Cost to change my mind:** `max_concurrent_reads=1` restores the old behaviour per
filesystem without a code change; `fetch_span` returning `self._fetch_range(info, start,
end)` restores it globally, in one line.
## D29 — a ZIP's block cache can **outlive the process**, if you name a directory for it

D25 gave the member view an `mmap` cache so each block of a container is fetched once.
It is thrown away: `MMapCache` with no `location` falls through to
`tempfile.TemporaryFile()`, unlinked at creation, so the sparse file dies with the
process.

Measured, not inferred: a real `dvc import-url` of a 20.09 GB Zenodo record ran for
**2 hours 50 minutes**, had **16.1 GB** in that file — visible as
`/proc/<pid>/fd/12 -> /scratch/…/tmp870ebnra (deleted)` — did not complete, and the
retry started from zero. At the ~1.6 MB/s such a record reads at, that is a 3.5 hour
window in which a cancelled job, a walltime kill or an unrelated DVC exception costs
the entire transfer. Issue #15.

**This is a worse property than the 19.9x amplification D25/#10 fixed.** Amplification
made imports slow. This makes long ones a coin flip, and a coin flip is not something
you can put in a pipeline.

### Decision

`ARCHIVEFS_CACHE_DIR`, or `cache_dir=` per filesystem, names a directory where a
container's blocks persist between processes. The next run finds them and fetches only
what is missing. `archivefs cache` reports and clears it; see
[`cli-cache.md`](cli-cache.md).

The machinery was already there — `MMapCache` takes both `location` and `blocks`, and
its `__getstate__` deliberately keeps them. What the decision is really about is the
four ways of being wrong that a *persistent* block set opens up.

### Off by default, and that is the uncomfortable part

The property people want is "my three-hour import survives an interruption", and they
want it **before** the interruption, which is exactly when nobody has configured
anything. An opt-in cache is one you turn on the day after it would have helped.

It is still off, because the alternative is worse. A cache of 20 GB archives needs a
bound, and no default byte cap is both **useful** — necessarily bigger than the archive
being imported, or it cannot resume the case that motivated it — and **safe** on a
filesystem whose quota we do not know. A 20 GB default under `~/.cache` would blow an
NCI home directory (10 GB) on first use. Age-based eviction is a real bound and is
applied (14 days), but it bounds *accumulation*, not peak size.

So: named directory, or nothing. `$ARCHIVEFS_CACHE_DIR` in a shell profile is the
intended shape — set once, applies to every import — and `cache_dir=false` turns it off
for one command without unsetting it.

### And the *ephemeral* destination is configurable too, which is the sharper problem

Turning the cache off does not mean nothing is written: a container's blocks still need
somewhere to live for the length of the read. fsspec creates that file with
`tempfile.TemporaryFile()` and passes **no directory**, so it silently follows
`$TMPDIR` — and `/tmp` when `$TMPDIR` is unset.

**On Gadi `/tmp` is a 63 GB tmpfs.** That is RAM. An unset `$TMPDIR` therefore turns a
20 GB import into 20 GB of memory pressure on a node shared with everyone else's work,
which is a worse outcome than filling any disk — and it happens without anyone choosing
it, which is the part that makes it a defect rather than a trade-off.

`scratch_dir` / `$ARCHIVEFS_SCRATCH_DIR` names that directory. It is deliberately a
*second* option rather than a reuse of `cache_dir`, because the two answer different
questions — "should these blocks outlive the process" and "which filesystem absorbs
them while they do not" — and the second matters to someone who wants nothing kept.

Implementing it costs six duplicated lines: `MMapCache._makefile` hard-codes the
`TemporaryFile()` call, so `ResumableBlockCache` overrides it. That override is also
why our cache class is now used **even when nothing is persisted** — which brings a
second fix with it. fsspec marks blocks present *before* fetching them, so a read
retried after a failed fetch is served zeros out of the mmap. That is a live bug in the
ephemeral path too, not only the persistent one, and `_fetch`'s snapshot-and-restore
now covers both.

### Neither destination is ever `$HOME`

Nothing in archivefs writes payload to a home directory. Credentials are *read* from
`~/.config/archivefs/` and `~/.pyega3/` — a few hundred bytes of JSON, read-only — and
verified empirically: a ZIP-member read with `$HOME` redirected to an empty directory
leaves it empty.

Pointing either option at `$HOME` **warns once** rather than refusing. `~/.cache` is
the conventional place for a cache and a laptop is welcome to it; the reason for the
warning is that on a cluster the failure arrives three hours into an import as a quota
error attributed to whatever wrote last. This host's home allowance is 10 GB with
6.9 GB already used — 3.3 GB free against a 20 GB archive.

**Cost of being wrong about this:** if it turns out that everyone sets the variable and
nobody is surprised by the disk use, flipping the default is a one-line change plus a
default cap. If it were on by default and filled a shared `/scratch`, that would be
somebody else's job failing, not ours.

### The hazard everything else is shaped around

A block recorded as present but never written serves **zeros as data** — silently — and
DVC then hashes and commits them. That is a corrupt dataset with a clean checksum,
which is the worst failure mode in this project. Four consequences:

* **The index is written after the bytes.** fsspec's own `_fetch` does
  `self.blocks.update(_blocks)` *before* fetching them, which is a latent bug in-process
  (the object is normally abandoned along with the exception) and a loaded gun on disk.
  `ResumableBlockCache._fetch` snapshots the set, and restores it if the fetch raises.
  The index is then `msync`'d and `os.replace`d, so a crash between the two loses
  blocks — which costs time — rather than inventing them, which costs correctness.
* **The entry names the object.** Protocol, path, size, published md5 and **block
  size**, in the cache key *and* inside the index. Any mismatch discards the entry.
  Block size is in there because block *numbers* mean nothing under a different one, and
  D27 made the block size depend on `max_concurrent_reads` and the container's size — so
  it moves.
* **No checksum, no persistent cache.** Without a published md5, "same path, same size"
  is not evidence of the same bytes, and resuming into a re-deposited file produces
  something that hashes cleanly against nothing. Zenodo publishes an md5 for every file
  (which is why this landed there); GEO publishes none (D8) and keeps the
  process-lifetime cache it always had.
* **One writer per entry.** A non-blocking `flock`; the loser gets a private cache
  rather than interleaving writes into one sparse file. Non-blocking on purpose — two
  imports of one record should not queue behind each other for hours, and falling back
  is exactly the behaviour everyone had before this existed.

Every one of those paths is a *fallback*, never an error. A cache is an optimisation,
and an import that cannot write one must still run.

### What this does not cover

**Plain files.** `_open` uses a read-ahead cache and streams to disk, so an interrupted
`archivefs get` of a 2.5 GB `.ome.tif` still starts again. That wants a different
mechanism — a partial output file and a resume offset, not a block cache — and it is
not written yet.

**A clean exit does not clean up.** An entry survives until it ages out or `archivefs
cache clear` removes it, including after an import that finished perfectly. "Delete it
when the container has been read whole" was rejected: DVC's `sync_import` reads the
dependency twice (`dvc/stage/imports.py:47`), so deleting at the end of the first pass
would reintroduce the amplification #10 fixed.

**Cost to change my mind:** the cache is one `cache_type` string. Returning `"mmap", {}`
unconditionally from `block_cache_options` restores the previous behaviour exactly, and
`tests/test_blockcache.py` fails loudly rather than silently if it is ever half-wired.

---

## D30 — configuration is **layered**, and only the layer that travels may change the data

Three environment variables appeared in one afternoon — `ARCHIVEFS_CACHE_DIR`,
`ARCHIVEFS_SCRATCH_DIR`, `ARCHIVEFS_CACHE_MAX_AGE_DAYS` — which is where ad-hoc stops
scaling. A setting you have to remember to `export` is a setting you discover you
needed the day after it would have helped, and D29's cache is exactly that shape: the
property you want is "my three-hour import survives an interruption", and you want it
*before* the interruption.

Four layers, XDG-shaped, lowest precedence first — `system`
(`$XDG_CONFIG_DIRS/archivefs/config.json`), `user`
(`$XDG_CONFIG_HOME/archivefs/config.json`), `project` (`.archivefs.json`, committed),
`local` (`.archivefs.local.json`, gitignored) — with the environment above every file
and an explicit option above that. The same order `credentials.py` uses for secrets, so
learning it once is enough. Details and worked setups:
[`configuration.md`](configuration.md).

### The decision that is not obvious

**Only a layer that travels with the data may change what the data is.**

`--fs-config extract_zips=true` is written into the `.dvc` file, so it travels: a
colleague's `dvc pull` reproduces the same tree. The same setting arriving from
`~/.config/archivefs/config.json` would leave the `.dvc` file with **no record of it**,
and that identical file would then resolve to a different tree on a different machine —
silently. That is the reproducibility hole this whole project is arranged to avoid,
reached sideways through a convenience.

So options are classified, and a layer that cannot honour a class refuses it:

| kind | examples | settable from | refused in |
|---|---|---|---|
| machine — changes cost | `cache_dir`, `scratch_dir`, `max_concurrent_reads`, `api_url`, `credentials_file` | `system`, `user`, `local` | `project` |
| content — changes what comes back | `extract_zips`, `strip_components`, `view`, `layout`, `chiro` | `project` only | everywhere else |
| credentials | `password`, `token` | nowhere | everywhere |

The `project` refusal has the mirror-image justification: a committed `/scratch/...`
path is wrong for everyone who did not write it.

That split is what makes refusing content keys in the user config a *design* rather
than a restriction. Without `project`, the answer to "I want `extract_zips` on by
default" would just be no. With it, the answer is "yes, and committed, so your
collaborators get it too" — which is the answer that was wanted anyway.

### Refused, not warned; and unknown keys too

Every failure here has the same shape: the setting looks applied and is not, or is
applied somewhere it will not follow the data. A warning in a pipeline log is invisible
at exactly the moment it matters, which is the argument `credentials.py` already makes
for refusing a password on the command line rather than discouraging one. So a bad key
raises `ConfigError` at filesystem construction, naming the file, the layer, and where
the setting does belong.

That extends to typos. `cach_dir` silently doing nothing is the failure mode config
models are famous for, so an unknown key is an error listing the ones that would work,
and a misspelled protocol section (`"zendo": {…}`) is caught rather than skipped as
though it were a container.

`test_every_backend_option_is_classified` is what keeps the two key sets honest: a new
backend option cannot be added without deciding whether it changes the data. That guard
is the only reason it is safe for those sets to be hand-written lists rather than
another field on `registry.Backend`.

### JSON, and `None` defaults

JSON rather than TOML: `requires-python` is 3.9 so there is no `tomllib`, the CLI is
deliberately stdlib-only, and credential files are already JSON — one syntax to learn.

The unglamorous half of this change is that every configurable option's signature
default became `None`. With `extract_zips: bool = False` there is no way to distinguish
"the user said no" from "the user said nothing", so a configured `true` would be
silently overridden by the signature — and `strip_components: int = 1` has the same
problem at `0`. Both are pinned by tests, because it is the kind of bug that looks like
the config file not being read at all.

### Cost of being wrong

### One thing this made worse before it made it better

The first version of the chain ended at `$XDG_CACHE_HOME`, on the reasoning that XDG
already answers "where do caches go" and honouring a standard beats inventing a
variable. That was wrong, and the config model is what makes it clearly wrong: XDG
names a *location*, not consent for an entry the size of the archive. Someone
exporting `XDG_CACHE_HOME` is thinking about pip wheels and font caches, and on a
cluster the directory they point it at is shared project space — on this one, 78% full
and at 85% of its inode quota. Honouring it would have enabled a 20 GB cache for
anyone following ordinary XDG advice, which is exactly what D29 declined to do by
default, arrived at through a standard instead of a hardcoded path.

It is dropped. The `system` layer is the better answer to the same wish — lab-wide,
explicit, one file, and visible in `archivefs config`.

`archivefs config` shows every layer and what it resolved to, which is the diagnostic
that makes a layered model debuggable rather than mysterious. If the classification
turns out to be too strict, moving a key between `MACHINE_KEYS` and `CONTENT_KEYS` is
one line and the test above will notice. If layering turns out to be unwanted, deleting
`config.py` and the `_setting` calls restores exactly the environment-variable
behaviour that preceded it.

---

## D31 — BioStudies' **sharing key** is a credential, and the `.dvc` file survives publication

A depositor sharing an unreleased BioImage Archive study sends a link with a key on
it: `…/studies/S-BIAD3080?key=<uuid>`. Reviewers get the same link. The requirement
that came with it is sharper than "support the key": **one committed `.dvc` file that
works for a reviewer holding a key before release, for the depositor, and for anyone
at all afterwards.**

### The API behaviour, which upstream does not document

Measured against the live service, because the help pages and the REST docs do not
mention it:

| request | no key | `?key=` |
|---|---|---|
| `/api/v1/studies/{acc}/info` | **403** | 200 |
| `/api/v1/studies/{acc}` | **403** | 200 |
| `/api/v1/files/{acc}?…` | **200 `{}`** | 200, full listing |

Three things fall out of that table:

* **Query parameter only.** `X-Session-Token: <key>` is refused with 403.
* **`/files` does not 403; it returns an empty object.** So a private study and a
  missing one are indistinguishable there — which is why `study_info` uses
  `/studies/{acc}/info` as the existence check (a decision made for a different
  reason, and it pays again here).
* **A stale key is harmless.** A released study answers 200 with a key that is wrong,
  expired, or nonsense. So archivefs sends the key whenever it holds one, without
  first checking whether the study is public: nothing is gained by asking twice, and a
  depositor's key can stay in place after release.

BioStudies' own name for it, in the `/info` payload, is `seckey`.

### The URL *is* the capability

The important discovery is not the query parameter. An unreleased study is served from
a path that contains the key:

    relPath = .private/aa/aaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/S-BIAD/080/S-BIAD3080

that is `key[:2]/key[2:]`. A ranged GET on it returns **206 with no key parameter at
all**, and the same path minus the key is 404. It is a presigned URL that never
expires — so the key leaks through anything that prints a URL, which for this codebase
means every error message: `_request` interpolates the URL into `AuthenticationError`
and `ArchiveNotFoundError`.

Hence `_redact`, a new hook on `ArchiveFileSystem` — identity everywhere except BIA,
which masks both shapes. **Both**, and that was not the first version: the first masked
only the `.private/` path, and a test caught the `?key=` query parameter — which
archivefs itself adds — leaking through the very handler written to stop it.

### Decision

`key` is a declared `Credential` with `secret=True`, `required=False`, and a new
`scoped=True`. Scoping is the shape of the thing rather than a nicety: EGA issues one
username and password for every dataset you are authorised for, while BioStudies issues
a link per deposit, so a single value stops working at the second private study. The
file holds a mapping, with a flat entry beside it as the catch-all for the reviewer who
has exactly one:

    {"keys": {"S-BIAD3080": "…"}}      # several
    {"key": "…"}                        # one, applies to whatever you ask for

Everything else follows the existing policy unchanged: `secret=True` keeps `key` out of
`registry.Backend.config`, so `--fs-config key=…` dies inside DVC's own config
validator; `-o key=…` is refused by `CredentialResolver`; the file's mode is checked.

### Why the `.dvc` file is invariant

Not by care — by construction, and this is the part worth being precise about.
`dvc_data`'s `Meta.from_info` keeps a **fixed** field set out of an `info()` dict
(`size`, `md5`, `etag`, `checksum`, `version_id`, `mtime`, …). Everything else archivefs
reports, including the whole `bia` provenance block, is discarded before anything is
written. So of the things that change at publication —

| field | changes at release? | recorded? |
|---|---|---|
| accession + interior path | no | **yes** (`deps[].path`) |
| content md5 | no | **yes** |
| size | no | **yes** |
| `relPath` / `httpLink` | **yes** — gains/loses `.private/<key>` | no |
| `isPublic`, `released` | **yes** | no |

— none of the mutable ones is in the file. Verified end to end against the live private
study: `dvc import-url` succeeded with the key, and the key and the string `.private`
appear in **no** file DVC wrote — not the `.dvc`, not `.dvc/config`, not `.dvc/tmp`.

`relpath` survives in the provenance block, masked to `.private/<key>` rather than
dropped: that a deposit is unreleased is worth reporting, and `archivefs stat` prints
it to stdout, which ends up in logs as readily as stderr does.

### What this does *not* buy

BIA-submitted still declares no immutability basis, so it is not version-aware and DVC
will not treat it as fallback storage (D6/D17/D23). A reviewer cannot `dvc pull` a
depositor's completed import from BIA; they run `dvc import-url` themselves with their
own copy of the key. That is arguably correct rather than merely unfinished — an
unreleased deposit is *mutable*, so treating it as immutable storage would be a lie —
and a `.chiro` sidecar is the route to changing it, since it supplies the checksums BIA
does not.

**Cost to change my mind:** the credential declaration is one list entry; deleting it
returns BIA to anonymous-only, and `_redact` becomes dead code rather than wrong code.

## D32 — a credential bundle carries **capabilities, never identities**, and is derived from the repository

D31 made an unreleased BioStudies study readable with a sharing key. The next question
is operational: how does a depositor get that key to three reviewers? The ask was "a
zip file of the credentials they need, set up in one go", and the mechanics of that are
easy. The security shape of it is the part worth writing down.

### The distinction the feature turns on

Ask archivefs to "bundle up the credentials" and, run naively over one researcher's
configuration, it would produce a file containing their EGA account password, their
Zenodo personal access token, and their dbGaP `.ngc` — addressed to four strangers.
None of those grants access to a *deposit*. They grant access to a *person*.

So `Credential` gained one field, `shareable`, defaulting to `False`:

| | capability | identity |
|---|---|---|
| example | BioStudies per-study sharing key | EGA password, Zenodo token, dbGaP `.ngc` |
| opens | one named deposit | everything the account can reach |
| revoked by | regenerating that deposit's link | rotating the account |
| in a bundle | **yes** | **refused, with no flag** |

Exactly one credential in archivefs is marked, and
`test_exactly_one_credential_is_shareable_and_it_is_a_per_study_key` enumerates the
answer so that marking a second one has to come past a test whose reviewer is looking
at the security question rather than at plumbing. A companion test asserts that
`shareable` implies `scoped`, because unscoped-and-shareable is a contradiction: it
would open everything.

The flag lives on the declaration rather than in the exporter deliberately — the person
who knows whether a new archive's secret is safe to hand on is the person adding that
archive, and a default of `False` means forgetting to think about it fails closed.

**Import enforces it too**, not just export. A hand-edited manifest carrying
`ega:password` is refused at install with "whoever sent this should rotate it". The
producer and the consumer are on different machines and only one of them is ours.

### Derived from the repository, not from the credential store

The bundle is built by scanning the project's `*.dvc`, `dvc.lock`, `dvc.yaml` and
`.dvc/config` for archivefs URLs and exporting keys for exactly the deposits found.
A depositor with three papers in review has three keys in one file; reviewers of the
first get one.

Text matching, not YAML parsing: the CLI is deliberately stdlib-only and has no YAML
reader, and "does this repo mention `bia://S-BIAD3080`" does not need one. It
over-matches rather than under-matches — a URL in a `desc:` field counts — which is the
safe direction, since a spurious row costs a line of output where a missed one costs a
reviewer their access.

The report is the second half of the value, and it covers the deposits a bundle
*cannot* help with:

```
  bia://S-BIAD3080          included     key from ~/.config/archivefs/bia.json (S-BIAD3080)
  ega://EGAD00001006673     personal     password, username authenticate an account, not a
                                         deposit — a reviewer needs their own …
  geo://GSE12345            public       needs no credential; nothing to hand over
  zenodo://1486184          personal     public unless this deposit is restricted; …
```

An "included" row names its *source*, not just that a value was found, because a scoped
credential can resolve from a flat one: `$BIA_KEY` answers for any accession asked of
it. Exporting on that basis pins one study's key to another, and the failure surfaces
as a 403 on the reviewer's machine with nothing connecting it to the depositor's
environment. So the row says `from $BIA_KEY, which is not per-study — check it is this
deposit's`.

### No crypto, on purpose

There is no `--password`. Python's `zipfile` cannot *write* encrypted archives at all,
and the legacy ZipCrypto it can read has been broken for decades; adding `pyzipper` to
a stdlib-only CLI would buy weaker protection than the alternative. The alternative is
that the bundle reads and writes stdin/stdout, so `age` or `gpg` composes and does the
job properly:

```console
archivefs credentials export --out - | age -r age1… > pack.age
age -d pack.age | archivefs credentials import -
```

What archivefs does instead is the accident-proofing, because the realistic threat is
not an interceptor, it is `git add -A`:

* `0600` on write, via an atomic temp file whose mode is set *before* the content goes
  in, so there is no window at the default umask;
* **refused inside a git repository** unless the path is already ignored — `git
  check-ignore` is asked rather than `.gitignore` re-implemented — because rewriting
  history does not un-publish a key;
* refused onto a terminal, where binary output is useless and the keys would be in the
  scrollback;
* no value in any human-readable output, in any mode: not the report, not
  `credentials list`, not the conflict error, not `--json`. Asserted directly against
  the literal secret in tests, because "we do not print it" decays one helpful error
  message at a time.

### Import merges, and never overwrites silently

A key differing from one already held is a **conflict**: nothing is written, the error
names the deposit, and `--force` is required. The failure that prevents — a stale key
quietly replacing a working one — surfaces much later as an unexplained 403. Same
argument as D30's refusal to warn: an invisible action is worse than a loud stop.

`test_what_import_writes_is_what_the_resolver_reads` pins the two halves together.
They live in different modules and read the same declaration, and nothing else would
notice them drifting apart until a reviewer's pull failed.

### The route with no artefact

For BioStudies the URL *is* the capability (D31), so a sharing link is already a
bundle. `credentials import -` accepts one:

```console
echo 'https://www.ebi.ac.uk/biostudies/…/studies/S-BIAD3080?key=…' \
  | archivefs credentials import -
```

Parsed by `ArchiveFileSystem.grants_from_link`, a classmethod on the backend — the
inverse of `_redact` and, for the same reason, the backend's business rather than the
bundler's. **stdin, not an argument**: an argument lands in shell history and the
process table, which would sit oddly beside D16's refusal of credentials on the command
line.

Prefer this when it applies. It is the whole feature in one line of a cover letter,
with nothing to mislay and nothing to go stale.

### Naming

`archivefs credentials`, not `archivefs config import` as originally sketched.
D30's config model raises `ConfigError` on a credential key by design; a `config`
subcommand whose job is installing secrets would undercut the one sentence that model
is built to make true.

**Cost to change my mind:** the `shareable` flag is one line per credential. Deleting
`bundle.py` and its subcommand removes the feature entirely; nothing else imports it,
and the sharing-key support from D31 stands on its own without it.
