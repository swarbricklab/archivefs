# archivefs

**Read-only [fsspec](https://filesystem-spec.readthedocs.io/) filesystems for
scientific data archives.**

`archivefs` lets you address data in scientific repositories by accession/DOI and
pull it through the fsspec ecosystem — including [DVC](https://dvc.org/) via
`dvc import-url`. A reviewer, collaborator, or student can then start from the *same
raw data* you used and reproduce your results, with checksum certainty and clear,
version-controlled provenance.

```
ega://EGAD00001006673                  # dataset → directory
ega://EGAD00001006673/EGAF00004908765  # single file
zenodo://<record_id>/<filename>
geo://<GSE_accession>                   # supplementary / processed files
dbgap://<phs_id>/<SRR_accession>        # deferred
```

## Why

- **Reproducible provenance in one step.** `dvc import` someone's archived data into
  your project and get download + a pinned, checksummed reference together.
- **Checksum certainty.** Once data is in the DVC ecosystem you get integrity
  guarantees and version control over inputs.
- **Look before you leap.** `dvc list-url` shows what's in a record before you
  download it.
- **Works on anyone's deposits**, whether or not our tooling was used to create them.

## Design

A single package with **per-backend extras** (modelled on `dvc[s3]`, `dvc[ssh]`):

```
pip install archivefs           # base + light backends (Zenodo, GEO)
pip install archivefs[ega]      # + pyega3
pip install archivefs[dbgap]    # + SRA-toolkit orchestration (deferred)
```

Each backend is a thin fsspec `AbstractFileSystem`: "how do I list this repo" and
"how do I fetch bytes given a path + credential." Everything else (credential
resolution, the `--no-download` manifest contract, materialisation) is shared.

Credentials are always resolved at **pull time** from the local environment
(`.dvc/config.local` → env var → on-disk cache) and **never** written into the
committed `.dvc` file.

## Relationship to Chirograph

[**Chirograph**](https://github.com/swarbricklab/chirograph) is a separate project
defining the `.chiro.json` manifest format (portable, content-addressed checksums +
provenance). `archivefs` **optionally** reads Chirograph manifests to gain
download-free checksums, drift detection, and friendly workspace layout — but works
**without** them (native listing, native checksums). Chirograph is the manifest
format; `archivefs` is the data-access layer. Neither depends on the other to be
useful.

## Status

Design stage — no backends implemented yet. See the plan for build order.

- **Full design plan:** [`docs/plan.md`](docs/plan.md)
- **Format the plugin optionally consumes:**
  [Chirograph spec](https://github.com/swarbricklab/chirograph/blob/main/docs/chiro-format-spec.md)

## Build order (summary)

1. **EGA** — highest current pain, accession-clean. (First: resolve the EGA
   decrypted-checksum question — it gates the `info()` contract.)
2. **Zenodo** — cleanest reference case; extract the shared base from EGA+Zenodo.
3. **GEO-supplementary** — tractable; the SRA-raw path is out of scope (our raw
   reads live in EGA, not GEO).
4. **dbGaP** — deferred (in scope, not yet adopted).

See [`docs/plan.md`](docs/plan.md) for the full rationale, credential model, Zenodo
ZIP-transparency handling, and per-repository notes.
