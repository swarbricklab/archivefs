# Changelog

All notable changes to **archivefs** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/). The version is derived from git
tags by `setuptools-scm`.

## [Unreleased]

## [0.4.1] — 2026-11-12

Adds `archivefs confirm --strict`, so a deposit holding files its manifest does not list
fails the check.

### Added

- **`archivefs confirm --strict`** also fails on deposit files the manifest does not list,
  loose files included, so a stray or leftover upload no longer passes (#60). Without it,
  `confirm` still reports only unlisted members of a container the manifest declares,
  since a manifest may pin part of a deposit. The check uses the listing `confirm`
  already fetches: no extra request, no bytes read. A `.chiro.json` in the deposit is
  exempt. Matches `chirograph verify --strict`, and with `chirograph validate` makes a
  complete gate for a self-deposited record, draft or published.

## [0.4.0] — 2026-11-12

Reads your own unpublished Zenodo drafts, so a deposit's manifest and `chiro://` pins
can be built and tested before publication. Also quiets two warnings that fired on
every ordinary pull.

### Added

- **`zenodo://` reads your own unpublished drafts.** Zenodo answers 404 for a draft at
  `/records/<id>` and serves it, to its owner only, at `/records/<id>/draft`. With a
  token, archivefs now falls back there, so a depositor can build the manifest and the
  `chiro://` pins before publishing and pull them with their own token. A draft has no
  concept DOI yet; archivefs fills in the one Zenodo mints at publication
  (`10.5281/zenodo.<conceptrecid>`), so a manifest built from the draft lists the same
  record, concept and files as one built afterwards. A warning says the record is a
  draft, and `info()` carries `draft: true`. Without a token the 404 stands, with a
  hint that the record may be your draft.

### Fixed

- **A `chiro://` pin of one file warned about every other file in its record.**
  Pinning a single file of a multi-file Zenodo record (the natural unit for a file
  imported on its own) logged `chiro drift … N deposit file(s) not in the manifest` on
  every pull. A pin reads only the files it lists, so the record's other files are no
  longer counted as its drift. A listed file that goes missing, or whose size or
  checksum changes, still warns, and `archivefs confirm` still names files the
  manifest does not list. A manifest shipped inside a deposit, which claims to
  describe all of it, still reports them as drift.
- **Every DVC pull in a repository under the home directory warned about the block
  cache**, as `ARCHIVEFS_CACHE_DIR=… is inside your home directory`, though nobody had
  set that variable. Under DVC the cache defaults to a directory beside the
  repository's DVC cache, and on a laptop that is always under `$HOME`. That default
  follows a location the user chose for their DVC cache, so it no longer warns. A
  location someone sets explicitly under the home directory still does, and the
  warning now names what set it: `cache_dir=`, `$ARCHIVEFS_CACHE_DIR` or the archivefs
  config (likewise for `scratch_dir`).

## [0.3.2] — 2026-11-11

Fixes `web://` against hosts that compress responses on the fly. Upgrade, and rebuild
any `web://` manifest of a text file made with 0.3.0 or 0.3.1.

### Fixed

- **`web://` recorded the wrong size and md5 for a file a host gzips in transit**
  (GitHub raw, the Broad's MSigDB mirror). `requests` asks for `gzip` by default, so
  `HEAD`'s `Content-Length` was the compressed length: a `--download` capture then
  hashed only that many bytes of the file, and a host that dropped the length (and
  ignored `Range` on the gzip stream) could not be sized at all. The session now sends
  `Accept-Encoding: identity`, and a length or `Content-Range` from a response that is
  encoded anyway is not taken as the file's size. Binary files (`.xlsx`, `.zip`) were
  not compressed by any host seen and were unaffected.

## [0.3.1] — 2026-11-11

Fixes `archivefs dvc-meta` for single-file pins and `web://` pins. No API changes.

### Fixed

- **`archivefs dvc-meta`** now stamps a pin of one file (`chiro://md5:<digest>/<relpath>`)
  instead of refusing it as "not a resolvable chiro://md5 ref", records the pinned
  `file`, and refuses a file the manifest does not list. A `web://` pin is stamped as
  `archive: web` with the file's `https://` `url` and `web://` `source`, rather than
  the generic `archive: url`.

## [0.3.0] — 2026-11-11

Adds `web://`, so a file at a plain HTTPS URL (a journal supplement, a lab website, a
release directory) can be pinned with a `.chiro` manifest and imported through
`chiro://`. Backward-compatible: every `0.2.0` URL and `.dvc` import keeps working.

### Added

- **`web://`**: any file at a plain HTTPS URL (journal supplements, lab websites,
  release directories) as an archive. No listing (a file is found by `HEAD`) and, as a
  rule, no checksum, so it is meant to be pinned: `archivefs manifest web://… --download`,
  then `dvc import-url --no-download chiro://md5:…/<file>`. Concept scheme `url:`.
  Unlike DVC's own `https://`, a `--no-download` import of a server that sends no ETag
  still pulls, and the bytes are verified against the pinned md5. See
  `docs/decisions.md` D45.
- `archivefs manifest` accepts a single file and pins it under its parent directory.
- `ArchiveFileSystem.expect_members()`, the hook `chiro://` uses to announce a pin's files
  to a backend that cannot list. A no-op for every backend except `web://`.

## [0.2.0] — 2026-10-26

Adds four new access paths (`mendeley://`, `cbioportal://`, `chiro://`, and Chirograph
schema 2.0 containers), a batch of CLI verbs, and first-class packaging for PyPI /
conda-forge. Backward-compatible: every `0.1.0` URL and `.dvc` import keeps working.

### Added

- **`mendeley://`** — Mendeley Data deposits by DOI or dataset id, versioned and
  folder-aware (anonymous; **experimental**, sha256). See `docs/decisions.md` D44.
- **`cbioportal://`** — cBioPortal studies via the `cBioPortal/datahub` GitHub repo and
  git-lfs, with version pinning by git ref (`cbioportal://<study>@<ref>`) and a
  download-free sha256 for the LFS data files (**experimental**). See D44.
- **`chiro://`** — a committed `.chiro` manifest as a portable, self-verifying pin that
  routes to the archive its concept names; digest-addressed manifest store.
- Chirograph **schema 2.0** adoption: `containers` + member `crc32` for ZIP deposits,
  with a download-free `archivefs confirm`.
- New CLI verbs: `manifest` (incl. `--from-dvc`, fully offline), `update`, `confirm`,
  `versions`, `ping`, `dvc-meta`; subtree and ZIP-subtree (relocated) pins.
- Per-file `concept` CURIEs and version-aware DVC pull-through (`push: false`), so a
  reviewer can `dvc pull` straight from the archive with no DVC remote.
- Packaging: `LICENSE` (Apache-2.0), `NOTICE`, `py.typed`, a CHANGELOG, GitHub Actions
  CI, and a dormant trusted-publishing release workflow.

### Fixed

- Thread-safe concurrent ZIP member reads under DVC's default `--jobs` (#40).
- A `chiro://` pin fetches the pinned *version*, not the concept (found live).
- Python 3.9 compatibility: a runtime `int | str` union in the BIA backend's
  `isinstance` raised `TypeError` on 3.9 (the first CI run across the matrix caught it).

## [0.1.0] — 2026-08-19

First tagged release: the shared read-only base and the initial backends —
`ega://`, `zenodo://` (with ZIP transparency), `geo://`, `bia://` — addressable by
accession/DOI and consumable through fsspec and DVC's `import-url`. `dbgap://` is
registered but deferred.

[Unreleased]: https://github.com/swarbricklab/archivefs/compare/v0.4.1...HEAD
[0.4.1]: https://github.com/swarbricklab/archivefs/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/swarbricklab/archivefs/compare/v0.3.2...v0.4.0
[0.3.2]: https://github.com/swarbricklab/archivefs/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/swarbricklab/archivefs/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/swarbricklab/archivefs/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/swarbricklab/archivefs/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/swarbricklab/archivefs/releases/tag/v0.1.0
