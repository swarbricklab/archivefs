# Why Zenodo records are full of ZIPs, and what is inside them

Measured 2026-08-21, following up
[`zenodo-zip-prevalence-2026-08-21.md`](zenodo-zip-prevalence-2026-08-21.md). That
survey disproved "Zenodo always serves ZIPs". This one tests the *better* version
of the same intuition — that ZIPs appear whenever a record has directory structure
— and it holds.

## 1. Zenodo's file namespace is flat, so a directory can only be a ZIP

`zenodo-key-slashes-2026-08-21.py`: 900 records, 5592 file keys. **55 keys (1.0%)
contain a `/`**, and every one is Zenodo's GitHub-release integration —
`pandas-dev/pandas-v3.0.5.zip`, `huggingface/transformers-v4.25.1.zip`. That is a
flat `owner/repo` prefix on a single file, not a directory tree; all 55 are
themselves `.zip`.

So a key is a *name*, not a path. No record in 900 used slashes to represent a
tree of individual files. A depositor with directory-structured data has exactly
one option: zip it. Zenodo does no zipping — the depositor does, because the
namespace leaves no choice.

## 2. Worked example: record 17411292

Visium spatial transcriptomics, 18 files:

* 12 × `spaceranger_*.zip` (0.5–1.5 GB each)
* 6 × `HE_*.ome.tif` (0.2–2.5 GB each), **not zipped**

`spaceranger_P10-1.zip` holds 47 members under a single top-level `outs/` — a
verbatim Space Ranger output directory. The `.ome.tif` files are single files and
were left alone, including a 2.5 GB one.

This is the pattern exactly: the depositor zipped what was directory-shaped and
uploaded what was not. Not "each top-level directory gets zipped by Zenodo" — but
observationally close enough that the prediction is reliable.

## 3. What is actually inside a Zenodo ZIP — 40 samples

`zenodo-zip-shape-2026-08-21.py` reads only central directories (ranged reads,
via archivefs itself), 40 ZIPs across 20 bio/imaging records:

| shape | count | share |
|---|---|---|
| single top-level directory | 24 | 60.0% |
| several top-level entries | 13 | 32.5% |
| flat bag of files | 3 | 7.5% |

**12 of 40 (30%) contain `__MACOSX`** — macOS resource-fork cruft. Discounting it,
most of the multi-entry cases collapse to a single directory: `Control1` +
`__MACOSX`; `annotation files` + `__MACOSX`; `outs` + `__MACOSX` (Space Ranger
again, in three further records); `coregistration` + `__MACOSX`. Corrected, ~80%
are a single top-level directory.

The residue is small and benign: two records with loose top-level files
(`LICENSE`/`README.md`/`code`, and a handful of `.py`), three flat bags of `.czi`
/ `.tif` / `.mat`, and two ZIPs wrapping one large file each
(`Molecular Dynamics Ech1.xtc.zip` → `md.xtc` + `__MACOSX`) — ZIP used purely as
compression.

**Not one of the 40 was a ZIP that must stay zipped to remain meaningful** — no
Zarr `ZipStore`, no shapefile bundle, no ZIP-based container format.

## Consequences for the `extract_zips` default

`design.md`'s stated reason for opt-in — "Some ZIPs are genuine archival units and
shouldn't be sprayed into the workspace" — is **not evidenced on Zenodo**. It is a
sound worry in general (a Zarr `ZipStore` is exactly that), just not one this
sample supports. Whatever keeps the default off, it is not that.

What does argue for caution is different, and both parts are real:

1. **D20.** An expanded ZIP publishes no per-member md5, so its stage cannot be
   pulled from the archive — the reviewer route dies for exactly the records where
   expansion is most wanted. This, not archival units, is the live objection.
2. **`__MACOSX` in 30% of ZIPs.** Expanding sprays `__MACOSX/._*` resource forks
   into the workspace and into the tracked directory hash.

Both are fixable, and neither is an argument that expansion is semantically wrong.
The chiro layer already exists to supply checksums an archive does not publish
(GEO, BIA-submitted), which is the natural place to solve (1) — but it currently
runs at the *record* level only: `_resolve_with_manifest` populates the container's
member cache, while ZIP interiors go through `_zip_info`, which hard-nulls `md5`.
Whether a manifest-supplied member md5 would make `dvc pull` reach the archive is
**untested**, and is the experiment that decides the design.
