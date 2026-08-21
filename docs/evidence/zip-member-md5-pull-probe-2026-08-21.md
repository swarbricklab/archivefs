# Is D20 a DVC limitation, or ours? (probe, 2026-08-21)

D20 concluded that an **expanded** ZIP import "cannot be materialised at all" and
that expansion and the D17 reviewer route are therefore mutually exclusive. This
probe tests *why*, and the conclusion needs correcting: it is not a DVC limitation.

## Method

Isolate one variable. Rather than build the chiro plumbing first, patch
`_zip_info` in a throwaway copy of `src/` to fill `md5` by hashing the member's
bytes, and re-run the exact D20 sequence. All against `zenodo://1486184`
(853-byte ZIP, 2 members), `dvc 3.67.1`, no DVC remote configured anywhere.

## Results

| # | scenario | member md5 | `dvc pull` |
|---|---|---|---|
| 1 | baseline: `--no-download --fs-config extract_zips=true` | `None` | ❌ `Checkout failed for following targets: tree` |
| 2 | control: `--no-download` on a plain record *directory* | published by Zenodo | ✅ `1 file fetched and 1 file added` |
| 3 | probe: as 1, with member md5 forced | computed | ✅ `2 files fetched and 2 files added` |
| 4 | retrofit: stage from **1**, pulled by patched code | computed at pull | ✅ `2 files fetched and 2 files added` |
| 5 | reviewer: commit stage from 1, `git clone`, pull in clone | computed at pull | ✅ `2 files fetched and 2 files added` |

Bytes verified in 3–5: `tree/github-mark.svg` and `tree/md5sum` match their
member md5s.

Two findings beyond the headline:

* **The stage format does not change.** `tree.dvc` from 1 and from 3 are
  byte-identical — same `md5: 52544edff951fb8acbd8fd6a0713f0fb`, same dep
  `fed36ce076379aa131932699d4879eb5.dir`. DVC was *already* computing the same
  `.dir` hash at import (D14's streaming cost). So this is purely a **read-side**
  gap, and #4 confirms the consequence: stages already committed become pullable
  with no rewrite and no migration.
* **The missing `.dir` object is a red herring.** Neither 1 nor 2 has a
  `.dvc/cache` at all before `pull`, yet 2 pulls fine — DVC re-lists the source at
  pull time. The discriminator is solely whether `info()` yields an md5 per member.

## So what is the real cause?

`_zip_info` hard-sets `info["md5"] = None` for ZIP members. That is deliberate and
documented (D9: a member's hash cannot be known without decompressing it), and it
is *exactly* what `geo.py` does for the same reason — GEO publishes no per-file
digest, so `md5` is `None` with a `checksum_source` explaining why, "instead of
substituting something that is not a content checksum".

ZIP members are therefore in the same position as GEO files, and the answer should
be the same one: **a `.chiro` sidecar**, which is the existing, designed mechanism
for "the archive publishes no checksum". The gap is that the chiro layer runs at
*record* level — `_resolve_with_manifest` populates the container's member cache —
and never reaches ZIP interiors.

The probe's brute-force `md5(view.cat_file(member))` must **not** ship: computing a
member md5 inside `info()` would turn a download-free listing into a full download
of the archive, breaking what `design.md` calls non-negotiable, and would violate
the same rule GEO states explicitly. It was a probe, not a patch.

Worth noting ZIP members are better placed than GEO files: the central directory
publishes a **CRC32** per member, download-free, and `_zip_info` already records it
as `crc32`. DVC cannot consume CRC32 as a hash, but archivefs can verify with it.

## Consequence for D20 and the docs

"An expanded ZIP cannot be pulled" is true *today* but its stated cause is wrong,
and so is the conclusion drawn from it. Expansion and the reviewer route are not
inherently incompatible — they are incompatible until a manifest can supply member
checksums. `using-with-dvc.md`'s "expansion and the reviewer route are mutually
exclusive" needs the same qualification.
