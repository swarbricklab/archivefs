# BioImage Archive: storage and access model — verified 2026-08-17

`plan.md` listed `bia://` as a candidate backend gated on "verify storage/access
model first", and `design.md` marked the storage question **TODO / (VERIFY)**. This
is that verification, done against the live services rather than the documentation.
Probed with `S-BIAD599` (584 files) and `S-BIAD1` (6784).

> **Revised 2026-08-18, while implementing the backend.** Two claims in the first
> version of this note were **wrong**, and both would have shipped a silent
> data-loss bug. They are corrected in place below and marked ⚠️. The lesson is
> uncomfortable and worth keeping: a verification pass that reads the *first* page
> of a paginated API and the *count* beside it will conclude the API is generous
> and complete. Only implementing against it, with an eye on the totals, showed
> otherwise.

## 1. Listing: the BioStudies API, ⚠️ **paginated**

```
GET https://www.ebi.ac.uk/biostudies/api/v1/files/S-BIAD599   → 200
{"recordsFiltered": 584, "recordsTotal": 584, "draw": 1,
 "data": [{"Name": "17A1DC6394_8bit_lynEGFP.tif",
           "path": "20200220-ftp/experimentK/17A1DC6394/17A1DC6394_8bit_lynEGFP.tif",
           "Size": 149622587, "type": "file", "isDirectory": "false",
           "Characteristics__Organism_": "Danio rerio",
           "Experimental_Condition__Fluorescent_Labels_": "cldnB:lyn-EGFP", …}]}
```

* ⚠️ **`data` holds five rows, not 584.** The response is DataTables-shaped
  (`draw`/`recordsTotal`/`recordsFiltered`) and the default page is **5**. The
  first version of this note read `recordsFiltered: 584` beside a five-element
  list and called the listing complete. Reading it that way in code would import
  five files out of 584 and exit 0.
* Paging is `?start=&length=`. No `limit`, `offset`, `pageSize` or `max`
  parameter has any effect, and no sort parameter is honoured (`sortBy=…` is
  ignored; DataTables' `order[0][column]=…` is a 400).
* ⚠️ **`length` is capped at exactly 1000, and exceeding it returns an empty
  list** — HTTP 200, `data: []`, `recordsFiltered: 0`. Not an error, not a clamp.
  Measured: `length=1000` → 1000 rows; `length=2000` → nothing. `length=-1`
  returned everything for the 584-file study and nothing for the 6784-file one,
  which is the same trap wearing a different hat.
* **Advance by the page size you asked for**, not by the rows you got. Paging
  `S-BIAD1` at `length=1000` yields exactly 6784 rows; an attempt at 500,
  advancing by `len(page)`, produced overlapping pages and *looked* like it had
  lost 7 files.
* **`recordsFiltered` counts rows, not distinct paths.** `S-BIAD1` really does
  list 7 paths twice (same file, differing only in `file_position`), so 6784 rows
  are 6777 files. Reconcile completeness against rows fetched; collapse duplicate
  paths and say so.
* **`isDirectory` is the string `"false"`**, and `bool("false")` is `True`.
* **No checksum of any kind.** Every key of a file entry was enumerated; there is
  no `md5`, `hash` or `checksum` field. Same weakness as GEO.
* Per-file **experimental metadata** comes free — organism, labels, channels, gene
  identifiers. Richer than anything EGA/GEO/Zenodo expose per file.
* An **unknown accession returns 200 with an empty list**, so the files endpoint
  cannot distinguish "no such study" from "study with no files".

## 2. Where the bytes are: ask, do not derive

```
GET /api/v1/studies/S-BIAD599/info → {"files": 584, "isPublic": true,
  "relPath": "S-BIAD/599/S-BIAD599",
  "httpLink": "https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/599/S-BIAD599", …}
GET /api/v1/studies/S-BIAD1/info   → {"relPath": "S-BIAD/S-BIAD0-99/S-BIAD1", …}
```

* ⚠️ The first version of this note inferred a sharding rule ("last three digits,
  like GEO's `GSE176nnn`") from one study. **It is wrong for the other**:
  `S-BIAD1` lives under `S-BIAD/S-BIAD0-99/`, and the derived URL 404s. The path
  comes from `relPath`/`httpLink`, which the API hands over for free.
* `/info` also answers `{}` for an unknown accession, so it is the existence check
  the files endpoint cannot provide.
* Payload URLs are `<httpLink>/Files/<path>`, range-readable:
  `206`, `Accept-Ranges: bytes`, `Content-Range: bytes 0-99/149622587`.
* The `ETag` is Apache's `inode-size-mtime`, **not** a content hash.
* **Deposited paths need quoting.** 6776 of S-BIAD1's 6777 files sit under a
  directory called `#1_Raw_Images`; unquoted, the URL truncates at the `#`.

## 3. A second tier: an anonymous S3 bucket with **real md5 ETags**

```
GET https://uk1s3.embassy.ebi.ac.uk/bia-integrator-data?list-type=2&prefix=S-BIAD599/
<Key>S-BIAD599/01776a3a-…/01776a3a-…-thumbnail-128-128.png</Key>
<ETag>"a84906a7545b1d47386e7d72ee807c41"</ETag> <Size>12234</Size>
```

* **Anonymously listable** (plain ListObjectsV2, no credentials, no signing), and
  range-readable.
* **8000/8000 sampled ETags were plain single-part md5s**, including 4 MB Zarr
  chunks — so for this tier the ETag *is* a content checksum. (Multipart ETags
  would be `<hex>-<parts>` and are not md5s; the backend checks for that rather
  than assuming.)
* Contents are **derived**: OME-Zarr pyramids and thumbnails, keyed by image UUID
  under the accession. Not the submitted files.
* **Scale matters.** One object per Zarr chunk: `S-BIAD599`'s converted tree is
  **over 60,000 objects** and a flat enumeration ran **>280 s without finishing**.
  A delimited (one-level) listing of the same study is **19 prefixes in ~3.5 s**.
  Any usable implementation lists lazily, per directory.
* Not every study is converted: `S-BIAD1` has **no** objects in this bucket.

## 4. Two more, found while reviewing the finished backend

* **A prefix that is not a whole key matches siblings, not children.**
  `?prefix=S-BIAD599/0177&delimiter=/` returns `CommonPrefixes:
  S-BIAD599/01776a3a-…/`. So "the listing returned something" is *not* a test for
  "this is a directory" — the test is whether the path itself comes back as a
  common prefix. Matters because OME-Zarr chunk directories are named
  `0,1,10,100,…`.
* **`E-MTAB-1234` is a real, public BioStudies study** on a third sharding scheme
  (`relPath: E-MTAB-/234/E-MTAB-1234`), so an accession pattern written for
  `S-BIAD…` refuses valid input. `/studies/{acc}/info` is the authoritative check;
  the pattern should only catch obvious garbage.

## What this means for a backend

**Verdict: tractable, and now implemented** — `src/archivefs/backends/bia.py`, with
the two tiers as the same *view* registry GEO introduced (`submitted` by default,
`derived` for the conversions). Every trap above is handled and pinned by a test;
see `docs/decisions.md` D13.

The one design consequence worth restating: `s3://` alone was never going to be
enough, which was the optimistic hypothesis in `design.md`. The bucket holds
derived data under UUID keys, the submitted files live in FIRE, and the mapping
from accession to either one comes from the API.
