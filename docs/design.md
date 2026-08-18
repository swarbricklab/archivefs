# archivefs — Design

Full architecture for the fsspec/DVC integration. The high-level checklist is in
[`plan.md`](plan.md); the manifest format it optionally consumes is
[Chirograph](https://github.com/swarbricklab/chirograph).

## Purpose

A family of **read-only fsspec filesystems** for accession/DOI-addressable
scientific data archives, packaged so that DVC (via `import-url`) — and any other
fsspec consumer (Dask, Intake, HF Datasets, pandas) — can resolve custom-scheme
URLs to data, given appropriate credentials.

Target user experience: a committed `.dvc` import file references an
accession/DOI (never a secret), and `dvc pull` materialises the bytes on demand
using locally-resolved credentials.

```
ega://EGAD00001006673                  # dataset → directory
ega://EGAD00001006673/EGAF00004908765  # single file
zenodo://<record_id>/<filename>
geo://<GSE_accession>                   # supplementary/processed files
dbgap://<phs_id>/<SRR_accession>        # deferred, see below
```

## Repo topology

The project is **not** one or two repos — it is ~four components with genuinely
different change-rates, audiences, and (crucially) one that is *data, not code*.
Repo boundaries chosen now are expensive to change later, so they're pinned here.

| Repo | What it is | Kind | Cadence | When |
|---|---|---|---|---|
| **`chirograph`** | The `.chiro.json` format **spec** + the reference **library/CLI** (generate / verify / reconcile manifests). | code + spec | moderate | **start now** |
| **`archivefs`** | The fsspec filesystems (EGA/Zenodo/GEO/…), the DVC-consumable plugin. Optionally imports `chirograph`; works without it. | code | frequent | **start now** |
| **`chirograph-registry`** | The registry **framework**: CI templates, sharded-storage convention, resolver logic, "run your own registry" docs. | code/tooling | occasional | **deferred** |
| **`<lab>-manifests`** (our reference registry instance) | The actual **manifest data** + CI config instantiated from the framework. Thousands of small commits over time. | **data** | high, data-churn | **deferred** |

**Rationale for the boundaries:**
- **Spec + library together in `chirograph`** — while we are the sole implementer,
  the reference library *is* the spec's meaning; they version together with no
  cross-repo tax. Split the spec out only if a second-language implementation ever
  appears.
- **`archivefs` separate** — different concerns (fsspec backends, credentials,
  per-repo quirks) and a *frequent* cadence vs. the format's *moderate* one. The
  Chirograph dependency is **soft/optional** (native listing works without it), so
  coupling them in one repo would wrongly imply a hard dependency.
- **Registry framework separate from registry data** — the framework is reusable
  *code*; the reference registry is *data* that will churn with every manifest
  added. **Never put manifest data in a code repo:** you do not want manifest
  additions polluting library git history, and disentangling data-churn from code
  history later is a migration nobody enjoys.

**Start with two** (`chirograph`, `archivefs`); the two registry repos are **gated
on the adoption path** (start with in-repo manifests; stand up the registry only
when cross-project dedup or public discovery is concrete — see the registry design
note). Do **not** shortcut by folding registry tooling or manifest data into
`chirograph` now, even though it's tempting while it's just us.

## Core architecture

### It's fsspec, not a bespoke DVC plugin
DVC delegates storage to **fsspec**. Every DVC protocol (`s3://`, `gs://`,
`ssh://`, `webhdfs://`) is an fsspec `AbstractFileSystem`. So the deliverable is
a set of fsspec filesystems registered under custom protocols; DVC inherits them
for free, and so does the rest of the Python data ecosystem.

### Target the `import-url` concept, not `import`
`dvc import` is for data tracked in *another DVC/Git repo* — not us. The right
primitive is `import-url`: the `.dvc` file stores the external URL in `deps` and
the local path in `outs`, with `dvc update` bringing it up to date from source
and `dvc pull` fetching from the original location. Neither archive supports
push, which fits the import model exactly (imported data pulls from source).

### Read-only filesystem surface
Each backend implements a thin slice of `AbstractFileSystem`:
- `ls()` — dataset → member listing (drives directory import + `dvc list-url`)
- `info()` — size + a **stable, content-correlated version token**, ideally
  **download-free** (this is what makes `import-url --no-download` honest)
- `_open()` / `cat_file()` — authenticated, resumable retrieval
- `exists()`
- `glob()` comes free from fsspec once `ls()` works

### Packaging: one package, per-backend extras
**Package name: `archivefs`** (follows the fsspec `s3fs`/`gcsfs` `…fs` convention;
"archive filesystem"; PyPI-available; no collision). Fallback `sci-fsspec`. Avoid
`sci-fs`/`scifs` (phonetic/semantic clash with SCIF, the niche Scientific Filesystem
standard — small but same-domain audience) and `archfs` (Arch Linux / architecture
collision). Distinct from **Chirograph** (the manifest format/registry layer) — `archivefs`
is the fsspec consumer that *optionally* reads Chirograph manifests; each works without the
other.

Modelled on how DVC packages its own remotes (`dvc[s3]`, `dvc[ssh]`, `dvc[all]`).
- Thin base install → base classes, credential resolver, and the
  near-dependency-free backends (Zenodo, GEO-supplementary).
- `[ega]` extra → pulls in `pyega3`.
- `[dbgap]` extra → toolkit-orchestration layer + runtime check that SRA Toolkit
  binaries exist (clear "install SRA Toolkit" message, not an import error).
- Protocol registration via entry points, so an uninstalled extra means the
  protocol simply isn't registered and fails cleanly ("install the [ega] extra").

### Shared core (extract from the first two backends, not up front)
- `AbstractFileSystem` base: dataset→`ls()`, download-free `info()` from a
  metadata call, the `--no-download` manifest contract.
- `CredentialResolver`: chain **env var → private on-disk file**, and nothing
  else — an option-supplied credential is refused (decisions.md D16), so the
  invariant that **no secret ever enters the committed artifact** is enforced
  rather than asked for; a uniform "auth failed — here is the specific
  remediation" error contract.

## Per-repository summary

| Repo | Addressing | Download-free checksum | Versioning/immutability | Auth | Byte fidelity |
|---|---|---|---|---|---|
| **Zenodo** | DOI / record_id | Yes (MD5) | Strong (concept vs version DOI) | None (public); PAT for restricted | Exact |
| **EGA** | EGAD / EGAF | **OPEN — see check** | Weak/implicit | OAuth user/pass → token | Exact after decrypt |
| **GEO (suppl.)** | GSE | Weak natively → **strong via sidecar manifest** (self-deposits) | Conventional only | None (public) | Exact |
| **dbGaP** *(deferred)* | phs / SRR | Poor (SRA conversion) | Weak | NGC key **file** | Lossy (SRA→FASTQ) |
| **BioImage Archive** *(candidate)* | BIA accession | TBD (likely S3-backed) | TBD | Likely none (open) | Exact (expected) |

The two variables that decide backend quality: **download-free content-verified
`info()`** and **byte-fidelity of materialised data**. Zenodo wins both; SRA-rooted
sources lose both. GEO-supplementary wins byte-fidelity and — for self-deposits —
regains download-free checksums via the sidecar manifest.

### BioImage Archive (BIA) — spatial imaging modality

Different *axis* from the others: BIA (EMBL-EBI, same house as EGA/ENA) is the open
repository for biological **imaging** data. Relevant because **Visium/Xenium** are
multi-part depositions:
- expression/sequencing component → EGA (controlled, if human) or GEO/open
  (processed matrices);
- **imaging component** (H&E/DAPI/morphology images, Xenium morphology + per-
  transcript data) → **BIA**, which is built for image data the others handle
  poorly.

So BIA is not a competitor — it's the home for a *modality*, slotting into the
same "split by sensitivity + modality, cross-link the records" pattern as
matrices-vs-raw, adding an imaging tier.

As a **plugin backend** (`bia://`): plausibly toward the tractable (Zenodo/GEO-
suppl) end — EMBL-EBI archive, structured metadata, and (VERIFY) likely S3-backed
open storage, which could make it easy or even reachable via plain `s3://` on a
public bucket. **Candidate, not committed** — evaluate later, same discipline as
dbGaP. **TODO: verify BIA storage/access model and checksum exposure.**

## Credential management

| Repo | Secret | DVC config holds | Env fallback | Lifecycle concern |
|---|---|---|---|---|
| Zenodo | Bearer token (often none) | `token` (optional) | `ZENODO_TOKEN` | None; static |
| EGA | OAuth user/pass → token | `username` | `EGA_PASSWORD` | **Token refresh mid-download** |
| GEO | None (public) | — | — | None |
| dbGaP | NGC key **file** | path to `.ngc` | `DBGAP_NGC` | Key expiry → manual re-download |

Design notes:
- Credentials resolved entirely at **pull-time** from the local environment —
  never baked into the (portable, committed) `.dvc` artifact.
- **EGA**: token expiry can outlive a large download → needs refresh-on-401
  inside the retrieval loop (or lean on `pyega3`'s refresh). Decryption material,
  if still required by EGA's current transfer path, is a *second* secret with its
  own lifecycle — resolve via the same precedence chain, don't special-case.
  On shared HPC: per-user env vars from each researcher's own profile, never a
  shared credential file (traceability + per-person revocation).
- **dbGaP**: the "credential" is a *path to the NGC file*, not a secret string;
  backend passes it to `prefetch --ngc`. NGC is project-scoped and coupled to a
  per-project workspace (the `cd` requirement). Expiry → human re-downloads from
  the portal; detect this failure mode and say so explicitly.

## Zenodo ZIP transparency (data-as-stored vs data-as-used)

Zenodo often serves a `dataset.zip` rather than individual files. Goal: optionally
present the ZIP's *interior* as the directory structure, with `ls`/`list`,
hashing, and materialisation all consistent (no half-solution where the manifest
says ZIP but the workspace holds a directory).

Decisions:
- **Compose with fsspec's existing `ZipFileSystem` + chained-URL convention**
  (`zip://::zenodo://record/archive.zip`) rather than reimplementing archive
  traversal. The backend decides *when* to present a ZIP transparently; fsspec
  does the traversal.
- **Opt-in, not automatic.** Some ZIPs are genuine archival units and shouldn't
  be sprayed into the workspace. Signal intent via the chained-URL form (backend
  may sugar a friendlier alias). Optional per-remote `extract_zips` default with
  per-URL override; same mechanism underneath.
- **Download-free listing via HTTP range reads of the ZIP central directory**
  (names, sizes, CRC32 live at end of file; Zenodo supports ranges). Non-negotiable
  — without it every listing pulls the whole archive.
- **Integrity contract for ZIP-interior files (asymmetric, but sound):**
  upstream verification = container-level Zenodo MD5 (strong, on download) +
  member-level CRC32 (from central directory); workspace `outs.md5` computed
  full-strength from extracted bytes at pull time. Soundness rests on Zenodo
  version **immutability**. Document the asymmetry rather than hiding it.
- `.dvc` artifact: `deps` = the ZIP (Zenodo MD5 + version-specific DOI as
  immutable provenance token); `outs` = extracted tree with per-member
  post-extraction md5s. `dvc update` against a concept DOI re-resolves and
  re-extracts.
- **zip-slip is the one real security burden.** `zipfile.extractall()` sanitises
  member paths; reading members through fsspec and writing to computed paths
  yourself does **not** inherit that protection. Rule: either route final
  extraction through `extractall()`, or validate every resolved member path is
  contained within the target directory before writing. Matters more because
  other labs will point this at arbitrary public archives.
- Other ZIP edge cases (corruption via `testzip()`/CRC32, filesystem-illegal
  member names, zip-bombs via summing declared sizes from the central directory,
  nested ZIPs → treat inner ZIP as a leaf unless recursion is explicitly
  requested) are manageable; likely a robust library exists for the tail —
  **TODO: identify and evaluate one** rather than hand-rolling.

## Dataset-level import (EGA, and the general pattern)

- A dataset accession = a **directory** in fsspec terms; DVC imports directories
  natively (builds a `.dir` manifest of `{md5, relpath}` by calling `ls()` then
  `info()` per member). So `dvc import-url ega://EGAD... raw_seq/` versions the
  whole dataset as one `.dvc` file.
- "All files" case: trivial, `ls()` returns everything.
- "Required subset" case: prefer a **selection-driven virtual `ls()`**
  (parameterise the backend with a sample sheet / selection so `ls()` returns
  only the resolved members) over relying on DVC to forward glob semantics
  intact. Makes the `.dvc` file self-documenting about the subset taken.

## GEO scope — the difficulty is in the part we don't use

**Correcting an earlier over-pessimistic framing ("EGA/Zenodo easy, GEO hard").**
GEO's difficulty is *not* uniform — it splits sharply, and the hard half is out
of scope for us:

- **GEO-supplementary (processed/derived files over public FTP/HTTPS)** —
  anonymous, near-dependency-free, byte-exact. This is the *tractable* part of
  GEO, close to the Zenodo-easy case. Its one real wrinkle is weak/absent
  per-file checksums (see the Chirograph manifest layer).
- **GEO-raw (raw reads)** — defers to **SRA**, inheriting the toolkit-orchestration
  and SRA→FASTQ round-trip problems. This is the *hard* part.

**The key realisation:** the scary GEO backend is the one we don't need. Our raw
reads live in **EGA** (human data → controlled access), never GEO. When we use
GEO it is *only* for open processed matrices — the supplementary layer. So for
our own round-trips (deposit matrices to GEO → later `dvc pull` them back), we
only ever touch the tractable supplementary layer. The SRA-hard path is excluded
by our data model, not by extra engineering.

**Why GEO for deposit at all (vs Zenodo):** Zenodo's per-record size/file-count
limits don't suit thousands-of-samples processed data; GEO is the conventional,
expected home for processed expression matrices anyway (reviewers/readers look
there). Being pushed to GEO for the open tier is the mainstream path, not a
compromise. (Our Wu et al. distribution already used GEO for exactly this.)

**Revised build-difficulty ranking:** Zenodo easy · EGA moderate (auth + checksum
unknown) · **GEO-supplementary also fairly easy** (weak checksums the main wrinkle)
· GEO-raw-via-SRA genuinely hard **but out of scope**. Realistic plugin family =
EGA + Zenodo + GEO-supplementary, all three tractable.

**Forward-design seam (unchanged, for community benefit):** other labs (non-human
data) *do* put raw reads in GEO/SRA, so draw GEO's backend seam so "supplementary
files" is *one resolvable view*, with a future "raw reads via SRA" view able to
slot in beside it without redesign. Do **not** build SRA-orchestration now. That
one seam is the only speculative design the community ambition justifies.

## The `.chiro` manifest — central abstraction (defined in Chirograph)

The manifest format lives in the [Chirograph](https://github.com/swarbricklab/chirograph)
project; `archivefs` is a *consumer* of it. Summary of how the plugin uses it:

### `.chiro`-first URL resolution (the unifying move)
The plugin's front door is the manifest, **for every backend**. If the URL resolves
to (or auto-discovers) a `.chiro`, the plugin fetches+parses it and *that is* the
directory listing + checksums, download-free by construction. Backend-specific code
then shrinks to **"fetch the payload bytes this manifest references"** (HTTP GET /
`s3://` / `pyega3` call) — the irreducible part.

**This is the central abstraction:** a **`.chiro` resolver + per-backend
byte-fetchers**, NOT per-backend filesystems each reinventing listing and hashing.
It's what makes the backends thin.

- **Auto-discovery** (a courtesy, demoted below registry resolution): manifest at a
  well-known name within the deposit; backend looks for it, falls back to native
  `ls()` if absent.
- The plugin **imports Chirograph's verify functionality** so download-and-verify is
  a single operation, not two tools chained.

### Manifest-related tooling
- **`.chiro` writer** — generate from workspace data or the **DVC cache** (cache case
  is nearly free: DVC's md5s already exist → transcribe). Build **early** — usable
  for the imminent Visium/Xenium deposit before any backend exists. Lives in
  Chirograph, called from here.
- For our own deposits, `.chiro` gives strong download-free checksums; for consuming
  others' records with no manifest, we fall back to native checksums or capture an
  `observed-download` `.chiro` on first fetch.

See the Chirograph [format spec](https://github.com/swarbricklab/chirograph/blob/main/docs/chiro-format-spec.md)
and [registry design](https://github.com/swarbricklab/chirograph/blob/main/docs/manifest-registry-design.md)
for the manifest, `chiro://` resolution, and trust model.

## Build order

1. **EGA** — highest current operational pain (tardy help desk, standing
   access-request admin burden we want to escape), accession-clean. Build as if
   standalone. **Run the EGA checksum check first (below).**
2. **Zenodo** — cleanest reference shape (public, checksummed, immutable,
   DOI-versioned). Build second so the base class factors out the *genuine*
   common shape, not EGA's idiosyncrasies. **Extract base class + CredentialResolver
   from the EGA+Zenodo diff**, designed in awareness of all four.
3. **GEO-supplementary** — nearly free once base exists; draw the SRA seam noted
   above.
4. **dbGaP** — **deferred**: in scope but not yet in use (adoption under active
   consideration). Build last, against the by-then-proven base and, critically,
   against real portal access — not documentation. Its ability to *conform* to
   the base (NGC-file credential, per-project workspace state, SRA round-trip)
   without violating it is the test that the abstraction held.

Rationale for doing the *clean* case (Zenodo) second rather than the second-hardest:
letting the clean case + the auth-complex case (EGA) define the base — then making
the awkward cases (dbGaP orchestration, GEO/SRA) conform — avoids extracting a base
that's merely "the union of two sets of complications."

---

## OPEN ITEMS

### ⚠️ EGA checksum check (blocking for EGA `info()` contract)
**Question:** Does the MD5 in EGA's metadata equal the MD5 of the bytes on disk
*after `pyega3` download + decryption*? The whole `--no-download` manifest design
assumes the metadata checksum corresponds to the **materialised, decrypted**
bytes.

**Procedure** (one small-ish file you're authorised for; use a few-hundred-MB
file, not a tiny one, to cross chunk/resume boundaries):
1. Pull metadata; record **both** EGA checksums (unencrypted + encrypted).
2. `pyega3 -cf credentials.json fetch <EGAF...> --output-dir ./egacheck`
3. `md5sum ./egacheck/<EGAF...>/*`
4. Compare computed md5 against both reported checksums.
5. Repeat once with an **interrupted-and-resumed** download to confirm the
   materialised md5 is stable across transfer paths.

**Branches:**
- md5 == **unencrypted** checksum → happy path; metadata token = materialised
  bytes; download-free manifests honest. Proceed as designed.
- md5 == **encrypted** checksum only → `pyega3` leaves encrypted bytes /
  decryption is separate; `info()` needs an explicit decrypt-then-verify step.
- md5 == **neither** → decryption non-deterministic or format normalisation
  differs; recorded-vs-materialised checksum contract is **unsafe** for EGA. Fall
  back to size + accession + version as the change-detection token; accept EGA
  can't offer Zenodo's strong download-free guarantee.

This is the single fact the manifest design currently rests on as an assumption,
and it sets the reasoning pattern for the Zenodo-ZIP integrity case
(container-vs-member) too. **Same underlying question in both: does the token the
source gives me correspond to the bytes the user ends up with, and does something
— immutability / decryption determinism — guarantee that correspondence holds?**

### Other open decisions
- **ZIP edge-case library:** identify and evaluate a robust package for the
  pathological-content tail (illegal names, zip-bombs, encoding) rather than
  hand-rolling. Confirm it covers the write-side path-containment check, or plan
  to add that ourselves.
- **EGA decryption material:** confirm against EGA's *current* transfer path
  (Aspera/encrypted-file vs htsget) whether a separate decryption key is still
  required, and if so wire it through the same credential precedence chain.
- **Base-class timing:** extract only after EGA + Zenodo exist; resist over-fitting
  the base to those two — GEO and dbGaP must land on it.
- **dbGaP adoption:** gated on the institute's decision (separately: NIH IC
  sponsorship is reportedly lined up, which clears the main submission-eligibility
  hurdle — but consumption-side build stays deferred until real portal access).
- **SRA-orchestration layer:** currently scoped as a dbGaP-only internal detail
  (we don't pull GEO raw reads). Revisit *only* if GEO-raw becomes in scope for
  the community-facing version; if so, build it so GEO-raw can reuse it rather
  than burying it in the dbGaP backend.
- **BioImage Archive (`bia://`):** BIA is increasingly the **standard for the
  imaging component** of spatial assays, and also accepts count data — but journals
  sometimes still want counts on GEO, so cross-link. For the imminent Visium/Xenium
  paper: imaging → BIA; counts → wherever the journal wants (BIA and/or GEO),
  cross-linked. If counts are dual-deposited, emit the **same `.chiro`** to both so
  they're provably identical. Still verify BIA storage/access model (S3-backed?
  `s3://`-reachable? checksum exposure) for the eventual backend.
- **Deposit-side split rule (SOP, not tooling — cross-ref deposition doc):** split
  by *sensitivity* and *modality*, not raw-vs-derived: raw reads + all genotype/
  array data → EGA (controlled); processed matrices → GEO/Zenodo (open, after a
  single-cell re-identification screen); spatial imaging → BIA (open). Cross-link
  records. Note single-cell derived objects may retain genotype-informative signal
  → case-by-case screen before any open deposit.
