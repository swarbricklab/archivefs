# Changelog

All notable changes to **archivefs** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/). The version is derived from git
tags by `setuptools-scm`.

## [Unreleased]

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

[Unreleased]: https://github.com/swarbricklab/archivefs/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/swarbricklab/archivefs/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/swarbricklab/archivefs/releases/tag/v0.1.0
