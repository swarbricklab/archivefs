# Importing from the BioImage Archive

*A how-to for a collaborator who wants someone's BIA imaging deposit in their own
project, with provenance.* Everything below was run against the live archive; the
timings and checksums are real, not illustrative.

You need: an accession (e.g. `S-BIAD599`), and about ten minutes for setup. No
credentials — BIA is open.

---

## 1. Prep: install archivefs and register it

`archivefs` gives Python and DVC a `bia://` protocol. Two things have to be true
before any of the commands below work: the package is installed, and DVC has been
told about it.

```console
$ git clone git@github.com:swarbricklab/archivefs.git
$ cd archivefs
$ conda env update -n archivefs -f environment.yaml   # installs archivefs itself, editable
$ conda activate archivefs
```

The `environment.yaml` assumes a sibling `../chirograph` checkout. Chirograph is
*optional* — it buys download-free checksums for archives that publish none. If you
don't have it, delete the `-e ../chirograph` line before running `conda env update`;
nothing in this guide needs it.

Then register the protocols with DVC. This is a real step, not a formality: **DVC
does not discover fsspec filesystems from entry points**, so without it `dvc
import-url bia://…` fails with `Unsupported URL type` ([`decisions.md`](decisions.md)
D4).

```console
$ archivefs dvc enable          # writes a .pth into this env's site-packages
$ archivefs info
archivefs 0.1.0

  ✓ ega://     ready
  ✓ zenodo://  ready
  ✓ geo://     ready
  ✓ bia://     ready                BioImage Archive via BioStudies (open; submitted files + OME-Zarr conversions)
  – dbgap://   deferred (not implemented)

  ✓ chirograph      manifests give download-free checksums
  ✓ dvc             registered for bia, ega, geo, zenodo
```

That last line is the one to check. `archivefs dvc enable` is **per conda
environment** — do it again in every environment you want DVC to work from.
`archivefs dvc disable` undoes it.

---

## 2. Know which of the two views you want

One accession, two quite different sets of bytes:

| view | what it is | checksums | address |
|---|---|---|---|
| `submitted` (default) | exactly what the depositor uploaded — the TIFFs, the metadata spreadsheets | ❌ BioStudies publishes none | `bia://S-BIAD599/<deposited/path>` |
| `derived` | BIA's own OME-Zarr pyramids + thumbnails, keyed by image UUID | ✅ real MD5s, download-free | `bia://S-BIAD599/<image-uuid>/…` with `view=derived` |

Pick `submitted` if you want the original images the authors analysed. Pick
`derived` if you want something viewable/streamable (napari, vizarr, `zarr.open`)
and you care about cheap integrity checks.

Two caveats measured on the live service: **not every study is converted** —
`S-BIAD1` has no objects in the derived bucket at all — and the derived bytes are
*not* the submitted bytes, so their checksums are not interchangeable.

---

## 3. Look before you leap

```console
$ archivefs ls bia://S-BIAD599                  # children of the top level (~9 s)
S-BIAD599/20200220-ftp

$ archivefs ls -R bia://S-BIAD599 | wc -l       # every file
584

$ archivefs ls -R bia://S-BIAD599 | head -2
S-BIAD599/20200220-ftp/experimentA/056F63395C/056F63395C_8bit_lynEGFP.tif
S-BIAD599/20200220-ftp/experimentA/056F63395C/056F63395C_8bit_lynEGFP_seg.tif
```

`ls` prints bare paths, without the `bia://` prefix. If you're piping one into
another command, put the prefix back:

```console
$ archivefs stat bia://S-BIAD599/20200220-ftp/experimentA/056F63395C/056F63395C_8bit_lynEGFP.tif
{
  "name": "S-BIAD599/...056F63395C_8bit_lynEGFP.tif",
  "type": "file",
  "size": 208335712,
  "md5": null,
  "checksum_source": "none — BioStudies publishes no per-file checksum for submitted
                      files; supply a .chiro manifest for download-free verification",
  "url": "https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/599/S-BIAD599/Files/...",
  "bia": { "accession": "S-BIAD599", "view": "submitted", "section": "StudyComponent-1",
           "attributes": { "Characteristics__Organism_": "Danio rerio", ... } }
}
```

`stat` is worth knowing about: the `attributes` block carries the depositor's own
per-file metadata (organism, channel, fluorescent label, image name). That is
usually how you decide *which* files you actually want, rather than guessing from
filenames.

The derived view is addressed by image UUID, and listed lazily — a converted study
is one S3 object per Zarr chunk, over 60,000 for `S-BIAD599`, so it never enumerates
the whole thing:

```console
$ archivefs ls -o view=derived bia://S-BIAD599 | head -3        # ~14 s
S-BIAD599/01776a3a-16a5-4c48-a104-fefcc83cc2ee
S-BIAD599/01d3f195-c42f-4c47-8b0a-6b5d28f1e822
S-BIAD599/01d6dab7-6bcc-4775-8917-7c85b3ad3bad

$ archivefs ls -l -o view=derived bia://S-BIAD599/01776a3a-16a5-4c48-a104-fefcc83cc2ee
    11.9 KiB  a84906a7545b1d47386e7d72ee807c41  …/01776a3a-…-thumbnail-128-128.png
         0 B  -                                 …/01776a3a-….zarr
```

Those MD5s come from the bucket's ETags. Nothing was downloaded to produce them.

---

## 4. Import into your project with DVC

BIA needs **no credentials at all**, for either view. The URL is an accession, the
accession is public, and nothing below reads a secret.

### The derived (OME-Zarr) tier — genuinely download-free

The derived view is a storage option, not a different address space, so pass it with
`--fs-config`. Note the `--`: `--fs-config` takes any number of `name=value` pairs,
so without it DVC swallows the URL as another pair and fails with *"not enough
values to unpack"*.

```console
$ U=01776a3a-16a5-4c48-a104-fefcc83cc2ee
$ dvc import-url --no-download --fs-config view=derived -- \
    "bia://S-BIAD599/$U/$U-thumbnail-128-128.png" thumb.png
$ cat thumb.png.dvc
deps:
- md5: a84906a7545b1d47386e7d72ee807c41     # from the ETag; no bytes transferred
  size: 12234
  path: bia://S-BIAD599/01776a3a-…/01776a3a-…-thumbnail-128-128.png
  fs_config:
    view: derived
outs:
- path: thumb.png

$ dvc pull thumb.png.dvc                     # fetch when you actually need it
```

Commit the `.dvc` file. Anyone who clones your repo and runs `dvc pull` gets the
same bytes, verified — and the accession is right there in the `path`, which is the
point of the exercise. (`dvc pull` may print *"No remote provided and no default
remote set"* — harmless: that refers to your own DVC cache remote, which an
`import-url` stage doesn't need.)

Repeating `--fs-config view=derived` for each file is mildly tedious, and you may be
tempted by `dvc remote add bia bia://S-BIAD599`. Don't — an archive as a DVC remote
is unsupported: push cannot be made to work across archives, and it moves the
accession out of the `.dvc` file. [`using-with-dvc.md`](using-with-dvc.md) has the
full reasoning.

`fs_config` is committed, so it carries settings and not secrets — and DVC now
refuses `--fs-config password=…` outright, so that is not a rule to remember. It
does not arise for BIA, which has no credentials at all, but the same flag works for
`ega://`. See [`credentials.md`](credentials.md).

### The submitted tier — works, but read this first

```console
$ dvc import-url --no-download \
    "bia://S-BIAD599/20200220-ftp/experimentK/EF83427BD6/EF83427BD6_8bit_lynEGFP.tif" f.tif
$ grep -A2 ^deps f.tif.dvc
deps:
- md5: 26c0911fdf86d8a176539f5515eab32f
  size: 120188693
```

That md5 is correct — it matches `curl … | md5sum` exactly. But **`--no-download`
downloaded the file anyway**: BioStudies publishes no checksum for submitted files,
so DVC streams the whole thing to compute one itself. Measured: 83 s and 120 MB of
transfer for a stage that keeps no bytes. Do that across all 584 files of
`S-BIAD599` and you have transferred the entire ~60 GB study to write metadata, and
you will transfer it again on `dvc pull`.

So, for the submitted tier, choose deliberately:

* **Pick your files first.** Import the handful you need, not the study. Use `stat`
  (§3) to choose them. This is the one that saves the most.
* **Supply a `.chiro` manifest** if the deposit has one, or if you can make one —
  `--fs-config chiro=S-BIAD599.chiro.json`, committed beside the `.dvc`. Then the
  checksums come from the manifest, `--no-download` is honest again, **and** a
  collaborator can `dvc pull` from BIA without you hosting a copy. For the submitted
  tier this is the only way they can ([`decisions.md`](decisions.md) D17, D18).
* **Just download it**, accepting the consequence: `dvc import-url` *without*
  `--no-download` costs one transfer instead of two and you keep the bytes, but the
  recorded `outs.md5` stops DVC offering BIA as a fallback source, so nobody else can
  pull it without a DVC remote you have pushed to. Fine for data only you need.
* **Use the derived view** where its bytes are good enough.

`dvc update f.tif.dvc` re-checks the source later and re-imports if it changed.

---

## 4b. Before the study is published

A deposit you are still working on, or reviewing, is not released yet. BioStudies
answers `403` for it — the study exists, you just cannot see it — and shares it
instead through a link carrying a key:

```
https://www.ebi.ac.uk/biostudies/bioimages/studies/S-BIAD3080?key=aaaaaaaa-…
```

Give archivefs that key and everything else works unchanged:

```console
$ export BIA_KEY=aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee
$ archivefs ls -l bia://S-BIAD3080 | head -3
$ dvc import-url 'bia://S-BIAD3080/images/he/…/P17_Xe5k_matrix.csv' matrix.csv
```

For more than one private study, or to avoid a variable in your shell history, let
archivefs file the link for you — it writes `~/.config/archivefs/bia.json` at
`chmod 600`, reading from stdin so the key never reaches your shell history:

```console
$ echo 'https://www.ebi.ac.uk/biostudies/bioimages/studies/S-BIAD3080?key=…' \
    | archivefs credentials import -
```

[`credentials.md`](credentials.md) has the file's shape if you would rather write it
yourself.

**Sending access to reviewers or co-authors** is
[`reviewer-access.md`](reviewer-access.md): `archivefs credentials export` packages
the keys this repository's deposits need — and only those — and tells you which of
its dependencies no key can open.

### The `.dvc` file does not change at publication

This is the property worth checking, because it is what lets a depositor and a
reviewer and a future stranger all use one committed file. The import above writes:

```yaml
deps:
- md5: 46c7f2c87913ab9a02ba72972e04ec77
  size: 115
  hash: md5
  path: bia://S-BIAD3080/images/he/alignment_files/xenium/P17_Xe5k_matrix.csv
```

Nothing there is publication-dependent. The accession and the interior path do not
move when a study is released; the md5 is a hash of the content, so it does not
either. And nothing there is secret — the key appears in **no** file DVC writes, not
the `.dvc`, not `.dvc/config`, not `.dvc/tmp`.

What *does* change at release — the payload path gaining a `.private/<key>` prefix,
`isPublic`, `released` — is resolved from the API at read time and never recorded.

So: commit the `.dvc` file now, share it with reviewers, and it keeps working after
the DOI is minted. A reviewer needs their own copy of the key until then, and
nothing afterwards.

### What you still do not get before release

**No checksums, so no cheap `--no-download`.** BIA publishes no md5 for submitted
files at any point (`decisions.md` D8), so DVC computes one by streaming the whole
file — the cost described in D14. That is unchanged by the key.

**Not a pull-through remote.** BIA-submitted declares no immutability basis, so it is
not version-aware and DVC will not treat it as fallback storage: a reviewer cannot
`dvc pull` your completed import from BIA. They run `dvc import-url` themselves with
their key, or you share the bytes some other way. A `.chiro` sidecar is the route to
changing that, since it supplies the checksums BIA does not.

**Derived view is release-only.** BIA's OME-Zarr conversions are built after
publication, so `-o view=derived` has nothing to find on an unreleased study.

## 5. Without DVC

There is no adaptor to install for plain Python — the entry point is enough:

```python
import fsspec

fs = fsspec.filesystem("bia")                       # submitted view
print(fs.ls("S-BIAD599/20200220-ftp"))
with fs.open("S-BIAD599/.../056F63395C_8bit_lynEGFP.tif", "rb") as f:
    header = f.read(1024)                            # ranged read, not a full fetch

derived = fsspec.filesystem("bia", view="derived")   # OME-Zarr tier
```

`pandas`, `dask` and `intake` reach the same URLs by the same path. Reading the
derived Zarr pyramids with `zarr`/`napari` over this filesystem should work and is
the obvious next thing to try, but it has not been exercised end to end yet.

---

## 6. Things that will bite you

* **Accessions are case-sensitive.** `bia://s-biad599` is not a lowercase
  convenience — the API 404s on it, so archivefs refuses it up front rather than
  reporting "no such study".
* **Study listing is a few seconds**, and paginated behind the scenes. If a listing
  ever fails with a completeness error, that is deliberate: the API can return a
  short listing with HTTP 200, and archivefs refuses a partial answer rather than
  silently handing you fewer files than the study has.
* **Duplicate paths are real.** A study record can list the same file twice
  (`S-BIAD1` does, seven times). They are collapsed; the count you see is distinct
  files.
* **`#` and other awkward characters** appear in deposited paths and are handled —
  6776 of `S-BIAD1`'s files sit under such a path. Quote your shell arguments.
* **Non-`S-BIAD` accessions work too** — `S-BSST…`, `E-MTAB-…`; anything in
  BioStudies.

## Where to ask

Design and rationale: [`design.md`](design.md). Every deviation and measured archive
quirk, including the five BIA-specific ones: [`decisions.md`](decisions.md) D13. The
raw access-model measurements:
[`evidence/bia-access-model-2026-08-17.md`](evidence/bia-access-model-2026-08-17.md).
