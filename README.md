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
zenodo://1486184/data.zip              # record id …
zenodo://10.5281/zenodo.1486184        # … or DOI (concept DOI → latest version)
mendeley://10.17632/yzxtxn4nmd/4       # Mendeley Data (anonymous; EXPERIMENTAL, sha256)
geo://GSE176078                        # supplementary / processed files
bia://S-BIAD599                        # BioImage Archive: imaging depositions
dbgap://<phs_id>/<SRR_accession>       # deferred, registered, refuses clearly
cbioportal://brca_metabric             # deferred, registered, refuses clearly
```

## What it does today

```console
$ archivefs ls -l zenodo://1486184        # -R to recurse
       853 B  49f6a914ea33912471512ac9228c6923  1486184/github-mark.zip

$ dvc import-url --no-download zenodo://1486184/github-mark.zip logo.zip
$ cat logo.zip.dvc
deps:
- md5: 49f6a914ea33912471512ac9228c6923    # download-free, from record metadata
  size: 853
  path: zenodo://1486184/github-mark.zip   # an accession — never a secret
outs:
- path: logo.zip

$ dvc pull logo.zip.dvc                    # bytes on demand, verified by DVC
```

That order matters. `--no-download` then `dvc pull` gives you the data locally *and*
leaves the committed `.dvc` in the one shape a reviewer can pull from the archive
without you hosting a copy — which is the point of the exercise.
[`docs/decisions.md`](docs/decisions.md) D17.

The same works for `ega://` with credentials resolved from the environment at pull
time, and for `geo://` and `bia://` anonymously. For imaging, BIA's own OME-Zarr
conversions carry real MD5s, so that tier is download-free too:

```console
$ dvc import-url --no-download --fs-config view=derived -- \
    bia://S-BIAD599/<image-uuid>/<image>-thumbnail-128-128.png t.png
                                           # md5 recorded without fetching bytes
```

Where an archive publishes *no* checksum (BIA's submitted files, bare GEO),
`--no-download` still records a correct md5 — but DVC computes it by streaming the
file, so it is not free. [`docs/decisions.md`](docs/decisions.md) D14.

## Why

- **Reproducible provenance in one step.** `dvc import-url` someone's archived data
  into your project and get download + a pinned, checksummed reference together.
- **Checksum certainty.** Once data is in the DVC ecosystem you get integrity
  guarantees and version control over inputs.
- **Look before you leap.** `dvc list-url` / `archivefs ls` show what is in a record
  before you download it.
- **Works on anyone's deposits**, whether or not our tooling was used to create them.

## Install

```
pip install archivefs           # all four backends: Zenodo, GEO, EGA, BIA
pip install archivefs[chiro]    # + read .chiro.json manifests
pip install archivefs[dvc]      # + the DVC adaptor
```

EGA needs no extra: the backend speaks EGA's REST API directly. (`pyega3` pins
`requests==2.26.0`, which would hold the whole environment hostage — see
[`docs/decisions.md`](docs/decisions.md) D2.)

### Using it with DVC

DVC does **not** discover fsspec filesystems from entry points, so one extra step is
needed once per environment:

```console
$ archivefs dvc enable     # writes a .pth that registers our protocols with DVC
$ archivefs dvc status     # check it works
```

Why this is necessary, and what was tried instead, is
[`docs/decisions.md`](docs/decisions.md) D4. `archivefs dvc disable` undoes it;
`ARCHIVEFS_NO_DVC_HOOK=1` disables it for one command.

## Design

A single package, a shared base, and thin per-backend byte-fetchers:

- **`archivefs.base`** — path grammar, retry/error contract, length-checked and
  alignment-aware ranged reads, download-free `info()`, read-only enforcement.
- **`archivefs.credentials`** — one chain for every archive: environment → a
  private on-disk file, and nothing else. Passing a credential as an option is
  refused, and DVC's own validator rejects the keys, so "no secret in the committed
  artifact" is a config parse error rather than a promise.
- **`archivefs.chiro`** — `.chiro`-first resolution: when a deposit carries a
  manifest, the manifest *is* the listing and the checksums.
- **`archivefs.zipsafe`** — zip-slip, symlink, zip-bomb and corruption checks,
  applied at listing time as well as on extraction.
- **`archivefs.backends.*`** — per-archive: how to list this repository, and where its
  bytes live. That is all that is left.

Credentials are always resolved at **pull time** from the local environment and
**never** written into the committed `.dvc` file.

## Status

All four backends work against the live archives, with the plan's blocking question
resolved.

| backend | state | download-free checksum | notes |
|---|---|---|---|
| `zenodo://` | working | ✅ MD5 from record metadata | DOI + concept-DOI addressing; opt-in ZIP transparency |
| `ega://` | working | ✅ MD5 of the *decrypted* bytes (verified) | OAuth2 + refresh-on-401; 16-byte range alignment |
| `geo://` | working | ⚠️ none published — a `.chiro` sidecar fixes it | index-based listing; SRA seam drawn, not built |
| `bia://` | working | ⚠️ none for submitted files · ✅ MD5 for the OME-Zarr tier | two views; the imaging half of a split deposit |
| `dbgap://` | deferred | — | registered so the error explains itself |

Verified end to end against the live archives: `dvc list-url`, `dvc import-url
--no-download` (single file and directory), `dvc update`, `dvc pull`, and ZIP
transparency via `--fs-config` — including a controlled-access EGA pull whose `.dvc`
file contains neither username nor password, which DVC would now refuse to write in
any case. Plain `fsspec.open()` works with no adaptor at all, so pandas/Dask/Intake
reach the same data by the same path.

Live tests are opt-in (`ARCHIVEFS_TEST_{EGA,ZENODO,GEO,DVC}=1`); the default `pytest`
run is entirely offline.

**Read next:**

- **All the documentation, indexed:** [`docs/README.md`](docs/README.md) — a page per
  subcommand, the storage-option tables, and the guides below.
- **Importing from BIA, step by step:**
  [`docs/importing-from-bia.md`](docs/importing-from-bia.md) — a collaborator-facing
  how-to, from `conda env update` to a committed `.dvc` file.
- **Using any archive with DVC:** [`docs/using-with-dvc.md`](docs/using-with-dvc.md) ·
  **credentials:** [`docs/credentials.md`](docs/credentials.md)
  **reviewer access:** [`docs/reviewer-access.md`](docs/reviewer-access.md)
- **Decisions and deviations:** [`docs/decisions.md`](docs/decisions.md) — thirty-three
  entries, including the resolved EGA checksum question, several undocumented archive
  behaviours worth knowing about, the credential routes closed off in D16, why a
  reviewer can only pull from the archive for a *partial* import (D17), and the silent
  bugs an adversarial review of the finished code turned up.
- **Ideas and extensions:** [`docs/ideas.md`](docs/ideas.md)
- **Architecture:** [`docs/design.md`](docs/design.md) · **checklist:**
  [`docs/plan.md`](docs/plan.md)

## Relationship to Chirograph

[**Chirograph**](https://github.com/swarbricklab/chirograph) is a separate project
defining the `.chiro.json` manifest format (portable, content-addressed checksums +
provenance). `archivefs` **optionally** reads Chirograph manifests to gain
download-free checksums, drift detection, and friendly workspace layout — but works
**without** them (native listing, native checksums). Chirograph is the manifest
format; `archivefs` is the data-access layer. Neither depends on the other to be
useful.

## Development

```console
$ conda env update -n archivefs -f environment.yaml
$ conda activate archivefs
$ pytest                       # offline
$ pytest -m network            # add ARCHIVEFS_TEST_*=1 to actually run these
$ ruff check src tests scripts
```
