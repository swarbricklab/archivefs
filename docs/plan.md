# archivefs — Implementation Plan

High-level checklist. Detail lives in [`design.md`](design.md) — this file links
rather than repeats.

- **Architecture / rationale:** [`design.md`](design.md)
- **Manifest format (optional dependency):**
  [Chirograph](https://github.com/swarbricklab/chirograph)

## Build order

Build backends in an order that lets the shared base fall out of the diff between a
clean case and an auth-complex case (design.md §"Build order"):

1. [ ] **EGA** — highest current pain, accession-clean. Build as if standalone.
   - [ ] **⚠️ Resolve the EGA checksum check FIRST** — does EGA's reported md5 equal
         the md5 of the decrypted bytes `pyega3` materialises? Gates the `info()`
         contract and EGA Tier-2a verification. (design.md "OPEN ITEMS")
   - [ ] `ls()` (dataset → EGAF members via pyega3), `info()` (size + version token),
         `_open()` (authenticated, **refresh-on-401** for long downloads), `exists()`
   - [ ] Credential resolution: `username` in `.dvc/config.local`, `EGA_PASSWORD`
         env fallback; never in the committed `.dvc`
   - [ ] Confirm current transfer path (Aspera/encrypted vs htsget); wire any
         decryption key through the same credential chain
   - [ ] Selection-driven virtual `ls()` for subset imports (sample sheet)
2. [ ] **Zenodo** — cleanest reference shape; second so the base is factored from the
       genuine common shape, not EGA's quirks.
   - [ ] Backend over Zenodo REST (record → files, md5s, DOI versioning)
   - [ ] ZIP transparency: compose with fsspec `ZipFileSystem`, opt-in via chained
         URL, **range-read central directory** (download-free listing),
         **zip-slip path-containment** on write (design.md §"Zenodo ZIP transparency")
   - [ ] **Extract the shared base + `CredentialResolver` from the EGA+Zenodo diff**,
         designed in awareness of GEO and dbGaP
3. [ ] **GEO-supplementary** — nearly free once the base exists.
   - [ ] Backend over GEO FTP/HTTPS supplementary files (anonymous)
   - [ ] Draw the **SRA seam**: "supplementary files" as one resolvable view, so a
         future "raw reads via SRA" view can slot beside it — but **do not** build
         SRA orchestration (our raw reads live in EGA, not GEO)
4. [ ] **dbGaP** — **DEFERRED** (in scope, not yet adopted). Build last, against the
       proven base and real portal access. NGC-file credential, per-project
       workspace, SRA round-trip.

## Cross-cutting

- [ ] Packaging: one package, per-backend extras (`archivefs`, `[ega]`, `[dbgap]`);
      entry-point protocol registration with clean "install the extra" failure
- [ ] Import Chirograph's verify functions so download-and-verify is one operation
- [ ] `.chiro`-first resolution when a manifest is available; native listing when not
- [ ] Candidate backend: **BioImage Archive (`bia://`)** for spatial imaging — verify
      storage/access model first (design.md §"BioImage Archive")

## Deferred / candidate

- [ ] BioImage Archive backend (spatial imaging tier)
- [ ] ZIP edge-case library evaluation (design.md "OPEN ITEMS")
- [ ] SRA-orchestration layer — only if GEO-raw ever comes into scope

## Related

- Manifest format & registry: [Chirograph](https://github.com/swarbricklab/chirograph)
  ([spec](https://github.com/swarbricklab/chirograph/blob/main/docs/chiro-format-spec.md),
  [registry design](https://github.com/swarbricklab/chirograph/blob/main/docs/manifest-registry-design.md))
