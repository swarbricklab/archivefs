# BioImage Archive: storage and access model — verified 2026-08-17

`plan.md` lists `bia://` as a candidate backend gated on "verify storage/access
model first", and `design.md` marks the storage question **TODO / (VERIFY)**. This
is that verification, done against the live services rather than the documentation.
Probed with `S-BIAD599` (a public zebrafish imaging study, 584 files).

## 1. Listing: the BioStudies API, and it is generous

```
GET https://www.ebi.ac.uk/biostudies/api/v1/files/S-BIAD599   → 200
{"recordsFiltered": 584,
 "data": [{"Name": "17A1DC6394_8bit_lynEGFP.tif",
           "path": "20200220-ftp/experimentK/17A1DC6394/17A1DC6394_8bit_lynEGFP.tif",
           "Size": 149622587, "type": "file", "isDirectory": false,
           "Characteristics__Organism_": "Danio rerio",
           "Experimental_Condition__Fluorescent_Labels_": "cldnB:lyn-EGFP",
           "Channels": "lynEGFP:cell membranes", …}]}
```

* Anonymous, one call, full recursive listing with **exact sizes** and real
  relative paths. Better than GEO, which needs an HTML index plus a `HEAD` per file.
* Per-file **experimental metadata** comes along for free — organism, labels,
  channels, gene identifiers. Richer than anything EGA/GEO/Zenodo expose per file.
* **No checksum of any kind.** Every key of a file entry was enumerated; there is
  no `md5`, `hash` or `checksum` field. Same weakness as GEO.

`GET /api/v1/studies/S-BIAD599` returns the study metadata but **not** the file
list (its `section.files` was empty), so the `/files/` endpoint is the one to use.

## 2. Payload: HTTPS over FIRE, range-readable

```
https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/599/S-BIAD599/Files/<path>
  → 206 Partial Content
  Accept-Ranges: bytes
  Content-Range: bytes 0-99/149622587
  ETag: "8eb0f3b-5f2736712cf00"
```

* **Ranges work**, so `_open()`/`cat_file()` need nothing special.
* The path is sharded by the accession's **last three digits**
  (`S-BIAD/599/S-BIAD599/`) — the same trick as GEO's `GSE176nnn`, so
  `accession_dir()` has a sibling here.
* The `ETag` is Apache's `inode-size-mtime`, **not** a content hash. Do not record
  it as one.
* `https://www.ebi.ac.uk/biostudies/files/<acc>/<path>` also exists but answers
  `302`; prefer the FIRE URL and skip a redirect per file.

## 3. There is a second tier: an anonymous S3 bucket with **real md5 ETags**

```
GET https://uk1s3.embassy.ebi.ac.uk/bia-integrator-data?list-type=2&max-keys=5 → 200
<Key>S-BIAD599/01776a3a-16a5-…</Key>
<ETag>"a3efecc76ddfb4ebcc9f64872fba4a4c"</ETag> <Size>31102</Size>
```

* **Anonymously listable** (plain S3 ListObjectsV2, no credentials, no signing).
* Of 40 objects sampled, **40/40 ETags were plain single-part md5s** — none of the
  `-N` multipart form. So for this tier the ETag *is* a usable content checksum,
  which is the strong guarantee GEO lacks. (Worth re-checking on genuinely large
  objects before relying on it: multipart ETags are not md5s, and a converted
  whole-slide image could well be uploaded multipart.)
* But the contents are **derived**, not submitted: thumbnails, representative
  images, and OME-NGFF conversions, keyed by uuid under the accession. The
  original submitted files live in FIRE (§2).

## What this means for a backend

**Verdict: tractable — the easiest remaining backend, and it maps onto machinery
that already exists.**

* It is the GEO-supplementary shape with a *better* listing: one anonymous JSON
  call, exact sizes, no HTML parsing, no `HEAD` per file.
* The two tiers are exactly the **view registry** already built for GEO's SRA seam:
  `view="submitted"` (FIRE, byte-exact, no checksum) and `view="ngff"` (S3,
  derived, md5 ETag). One class each, no new abstraction.
* The missing-checksum problem is the one Chirograph already solves, and BIA is a
  repository **we deposit into** — so a sidecar `.chiro.json` in our own
  submissions gives download-free verification, exactly as planned for GEO.
* `s3://` alone is **not** sufficient, which was the optimistic hypothesis in the
  design: the S3 bucket does not hold submitted files, and its keys are uuids
  rather than accession paths. Accession → path resolution is the work, and it is
  small.

Not built: `bia://` remains a candidate, per the plan's own discipline. This
document is what a decision to build it should start from.
