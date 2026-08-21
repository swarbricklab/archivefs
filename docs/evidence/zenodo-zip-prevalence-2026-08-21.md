# How often is a Zenodo record actually made of ZIPs?

Measured 2026-08-21 to test the claim that "Zenodo always serves zip files … so a
Zenodo record always consists of one or more zip archives", which was the premise
of a proposal to expand outer ZIPs unconditionally.

Method: `zenodo-zip-prevalence-2026-08-21.py` — Zenodo search API,
`resource_type=dataset`, two populations of 300 records each (most recent; and a
bio/omics keyword query, since that is archivefs's actual target). Page size caps
at 25, so 12 pages each. Extensions tallied from the `files[].key` in the search
response. Records whose file list is absent (restricted/embargoed) are excluded
from the percentages below, since their composition is unknown.

## Recent datasets — 258 listable records, 1247 files

| record shape | records | share |
|---|---|---|
| no `.zip` at all | 169 | **65.5%** |
| every file a `.zip` | 52 | 20.2% |
| mixed | 37 | 14.3% |

Top extensions: `.csv` 25.9%, **`.zip` 15.2%**, `.pdf` 11.5%, `.txt` 8.4%,
`.gz` 6.3%, `.xlsx` 6.2%.

## Bio/omics datasets — 260 listable records, 3046 files

| record shape | records | share |
|---|---|---|
| no `.zip` at all | 182 | **70.0%** |
| every file a `.zip` | 44 | 16.9% |
| mixed | 34 | 13.1% |

Top extensions: `.gz` 29.0%, `.rds` 21.3%, **`.zip` 8.8%**, `.txt` 7.0%,
`.csv` 4.0%, `.bed` 3.2%, `.bam` 2.7%, `.fastq` 1.9%.

## Conclusion

The premise does not hold: about two thirds of dataset records contain no ZIP at
all, and ZIP is a minority extension in both populations — a smaller minority in
the bio/omics case, where `.gz` and `.rds` dominate. Individual files are served
as-is from `/api/records/<id>/files/<key>/content`; nothing is zipped in transit.

**What is always a ZIP** is the *whole-record* "Download all" archive at
`/api/records/<id>/files-archive` (verified: HTTP 200, `application/zip`, 1025
bytes for record 1486184, wrapping its 853-byte member). That is a transport
artifact synthesised per request — not a member of the record, and it carries no
published MD5, so it cannot be verified or pinned. archivefs does not use it and
lists files individually instead.

So "always expand outer ZIPs" would be a no-op for ~two thirds of records and, for
the ~20% that are all-ZIP, would replace a pinned MD5-bearing file with a directory
of members that publish no md5 and therefore cannot be pulled from the archive at
all (D20). That trades away the reviewer route precisely where it currently works.
