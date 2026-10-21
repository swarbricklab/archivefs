# `archivefs info`

**Answers one question: is this installation actually wired up?**

```
archivefs info [--json]
```

No URL, no network. It reports what is importable in *this* Python environment, so
it is the first thing to run when something else fails in a confusing way.

## Options

| option | meaning |
|---|---|
| `--json` | machine-readable form: `{version, backends, dvc}`. Same information, plus the raw DVC hook report. |

## What the output means

```console
$ archivefs info
archivefs 0.1.0

  ✓ ega://        ready                           European Genome-phenome Archive (controlled access; needs credentials)
  ✓ zenodo://     ready                           Zenodo records by id or DOI (public; token only for restricted)
  ✓ mendeley://   ready                           Mendeley Data deposits by DOI or dataset id (versioned; anonymous; EXPERIMENTAL — frontend API, sha256)
  ✓ geo://        ready                           GEO supplementary/processed files over public HTTPS (anonymous)
  ✓ bia://        ready                           BioImage Archive via BioStudies (open; submitted files + OME-Zarr conversions)
  ✓ cbioportal:// ready                           cBioPortal studies via the datahub GitHub repo + git-lfs (anonymous; EXPERIMENTAL, sha256)
  – dbgap://      deferred (not implemented)      dbGaP via SRA Toolkit — DEFERRED, not implemented

  ✓ chirograph      manifests give download-free checksums
  ✓ dvc             registered for bia, cbioportal, ega, geo, mendeley, zenodo
```

Per backend:

| mark | meaning | what to do |
|---|---|---|
| `✓ ready` | the backend's imports and any external tools are present | nothing |
| `✗ missing: …` | the protocol is registered but a package or binary is absent | install what it names |
| `– deferred` | registered so the error explains itself, but not implemented (`dbgap://`) | nothing; it will refuse clearly if you use it |

**`✓ ready` is not a statement about credentials or connectivity.** `ega://` shows
ready with no account configured — it means the code will load, not that a pull will
succeed. Use `archivefs ls` for that.

The last two lines are the ones that catch real problems:

* **`– chirograph not installed`** — `.chiro.json` manifests will be ignored, so you
  get the archive's native listing and whatever checksums it publishes. Not an error;
  see [`credentials.md`](credentials.md) for what manifests add.
* **`✗ dvc installed but archivefs is NOT registered with it`** — every `dvc
  import-url bia://…` will fail with `Unsupported URL type` until you run `archivefs dvc
  enable`. This is the single most common setup failure, because DVC has no plugin
  hook for storage backends. See [`cli-dvc.md`](cli-dvc.md).
* **`– dvc not installed`** — fine if you are using plain fsspec.

## Exit status

**Always 0**, even when something is missing — it is a report, not a test. If you
want a check that fails in a script, use `archivefs dvc status`, which exits 1 when
the hook is absent:

```console
$ archivefs dvc status >/dev/null || echo "DVC integration is not set up"
```

## Scenario: a colleague's `dvc pull` fails and yours works

```console
$ archivefs info --json | python -c 'import json,sys; d=json.load(sys.stdin); print(d["dvc"])'
{'dvc_installed': True, 'hook_path': '/…/site-packages/archivefs_dvc.pth',
 'hook_installed': False,
 'registered': ['bia', 'cbioportal', 'ega', 'geo', 'mendeley', 'zenodo'],
 'protocols': ['bia', 'cbioportal', 'ega', 'geo', 'mendeley', 'zenodo'],
 'deferred': ['dbgap']}
```

`hook_installed: False` with `dvc_installed: True` is the whole diagnosis: they have
DVC, they have archivefs, and DVC cannot see it. One `archivefs dvc enable` in *their*
environment fixes it. Note that the hook is per-environment — activating a different
conda env is enough to lose it.

**Do not read `registered` as the signal.** It is populated here even though the hook is
missing, because `status()` *probes* — it performs the registration in this process
before reporting, so the answer means "registration works" rather than "registration
happened". `hook_installed` is the field that tells you whether it will happen in a
`dvc` process.

## See also

[`cli-dvc.md`](cli-dvc.md) · [`using-with-dvc.md`](using-with-dvc.md) ·
[`decisions.md`](decisions.md) D4 for why the hook has to exist at all
