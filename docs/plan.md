# archivefs — Implementation Plan

High-level checklist. Detail lives in [`design.md`](design.md) — this file links
rather than repeats. **Boxes ticked below were implemented and tested against the
live archives**; deviations and open questions are in
[`decisions.md`](decisions.md), and things deliberately not done are in
[`ideas.md`](ideas.md).

- **Architecture / rationale:** [`design.md`](design.md)
- **Decisions & deviations:** [`decisions.md`](decisions.md)
- **Ideas & extensions:** [`ideas.md`](ideas.md)
- **Manifest format (optional dependency):**
  [Chirograph](https://github.com/swarbricklab/chirograph)

## Build order

Build backends in an order that lets the shared base fall out of the diff between a
clean case and an auth-complex case (design.md §"Build order"):

1. [x] **EGA** — highest current pain, accession-clean. Built as if standalone, then
   re-seated on the extracted base.
   - [x] **⚠️ Resolve the EGA checksum check FIRST** — **RESOLVED: happy path.**
         `md5(materialised decrypted bytes) == plainChecksum`, size `== fileSize-16`,
         holding across a 13-slice/4-connection/interrupted-and-resumed 100 MB
         transfer. [`decisions.md` D1](decisions.md);
         [`scripts/ega_checksum_check.py`](../scripts/ega_checksum_check.py);
         log in [`evidence/`](evidence/).
   - [x] `ls()` (dataset → EGAF members), `info()` (size + version token),
         `_open()` (authenticated, **refresh-on-401**), `exists()`
   - [x] Credential resolution: `username` in `.dvc/config.local`, `EGA_PASSWORD`
         env fallback; never in the committed `.dvc` (asserted on the artifact)
   - [x] Confirm current transfer path — **API v2 with
         `?destinationFormat=plain`**: the server decrypts, so no separate
         decryption key is needed. Two undocumented behaviours found and handled:
         **16-byte range alignment** ([D3](decisions.md)) and short-but-successful
         bodies.
   - [x] Selection-driven virtual `ls()` for subset imports (accessions or display
         names; a sample-sheet front end is in [`ideas.md`](ideas.md))
   - [ ] Re-run the checksum check once against a *real authorised* file (D1 was
         established on EGA's synthetic test dataset) — [`ideas.md`](ideas.md) #2
2. [x] **Zenodo** — cleanest reference shape; second so the base is factored from the
       genuine common shape.
   - [x] Backend over Zenodo REST (record → files, md5s, DOI + concept-DOI
         versioning, `versions()`/`latest_version()`/`search()`)
   - [x] ZIP transparency: composes with fsspec `ZipFileSystem`, opt-in via chained
         URL *or* `extract_zips`, **range-read central directory** (download-free
         listing, asserted by byte accounting), **zip-slip path-containment** —
         enforced at *listing* time as well as on write ([D9](decisions.md))
   - [x] **Extracted the shared base + `CredentialResolver` from the EGA+Zenodo
         diff**, designed in awareness of GEO and dbGaP; both suites passed
         unchanged through the refactor
3. [x] **GEO-supplementary** — nearly free once the base existed.
   - [x] Backend over GEO HTTPS supplementary files (anonymous)
   - [x] Drew the **SRA seam**: views are a registry, `supplementary` implemented,
         `raw` registered and refusing with an explanation. A test adds a view from
         outside the module to prove one slots in. **No SRA orchestration built.**
   - [x] Found that `filelist.txt` is *not* a complete listing — trusting it would
         silently drop deposited files ([D8](decisions.md))
4. [ ] **dbGaP** — **DEFERRED** (in scope, not yet adopted), as planned. Registered
       with a stub that explains the deferral and holds the NGC-file credential
       declaration and the SRA-Toolkit check; deliberately *not* registered with DVC.

## Cross-cutting

- [x] Packaging: one package, per-backend extras (`[chiro]`, `[dvc]`, `[dbgap]`);
      entry-point protocol registration with clean "install the extra" failure.
      **No `[ega]` extra** — see [D2](decisions.md).
- [x] Import Chirograph's verify functions so download-and-verify is one operation
      (`archivefs.chiro.verify_materialised`, `archivefs verify`)
- [x] `.chiro`-first resolution when a manifest is available; native listing when
      not. Includes the two-path model (deposit layout → workspace layout) and
      **drift detection** in both directions.
- [x] **DVC integration actually works** — but not "for free" as the design assumed;
      it needs an adaptor and a registration hook ([D4](decisions.md)). Verified:
      `list-url`, `import-url --no-download` (file + directory), `pull`, for all
      three backends including controlled-access EGA.
- [x] A CLI for the questions that otherwise need Python: `archivefs info | ls |
      stat | get | check | verify | dvc`
- [ ] Candidate backend: **BioImage Archive (`bia://`)** — still needs its
      storage/access model verified. Notes and a cheaper framing in
      [`ideas.md`](ideas.md) #7.

## Deferred / candidate

- [ ] BioImage Archive backend (spatial imaging tier)
- [x] ZIP edge-case library evaluation — **evaluated, none adopted**, with reasons
      ([D9](decisions.md)): the candidates do not cover the write-side containment
      check, which is the part that matters.
- [ ] SRA-orchestration layer — only if GEO-raw ever comes into scope (the seam is
      drawn so it is one class)

## Related

- Manifest format & registry: [Chirograph](https://github.com/swarbricklab/chirograph)
  ([spec](https://github.com/swarbricklab/chirograph/blob/main/docs/chiro-format-spec.md),
  [registry design](https://github.com/swarbricklab/chirograph/blob/main/docs/manifest-registry-design.md))
