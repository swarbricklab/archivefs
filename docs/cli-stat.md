# `archivefs stat`

**Everything known about one path, as JSON — including where the checksum came from.**

```
archivefs stat [-o KEY=VALUE]… URL
```

Prints the `info()` dict the filesystem would give any fsspec consumer, so it is also
the honest answer to "what will DVC record for this file?".

## Arguments and options

| | meaning |
|---|---|
| `URL` | one path inside a record. A container also works, and reports the directory. |
| `-o`, `--option` | storage option, repeatable — see [the table in `docs/README.md`](README.md#storage-options--o-keyvalue) |

There are no formatting options: output is always indented JSON, ready for `jq`.

## The fields that matter

```console
$ archivefs stat bia://S-BIAD599/20200220-ftp/experimentA/056F63395C/056F63395C_8bit_lynEGFP.tif
{
  "name": "S-BIAD599/…/056F63395C_8bit_lynEGFP.tif",
  "type": "file",
  "size": 208335712,
  "md5": null,
  "checksum": null,
  "checksum_source": "none — BioStudies publishes no per-file checksum for submitted
                      files; supply a .chiro manifest for download-free verification",
  "url": "https://ftp.ebi.ac.uk/biostudies/fire/S-BIAD/599/S-BIAD599/Files/…",
  "bia": {
    "accession": "S-BIAD599",
    "view": "submitted",
    "section": "StudyComponent-1",
    "attributes": {
      "Characteristics__Organism_": "Danio rerio",
      "Channels": "lynEGFP:cell membranes",
      "Comment__Image_File_Type_": "raw",
      …
    }
  }
}
```

| field | why you care |
|---|---|
| `md5` / `checksum` | `null` means the archive publishes nothing. Anything else is download-free and is what DVC will record. |
| `checksum_source` | *why* it is null, and what would fix it. Read this before concluding archivefs is broken. |
| `size` | from metadata, not a `HEAD` |
| `url` | the actual byte source. Paste it into `curl` when you suspect the archive rather than us. |
| `archive_version` | the archive's own version token, where one exists (Zenodo DOI/revision, EGA re-encryption timestamp) |
| `<backend>` block | per-archive detail: `bia.attributes`, `zenodo.doi`/`concept_doi`, EGA display names |
| `chiro` block | present when a manifest supplied this entry, naming the manifest |

## Scenario: select images by annotation rather than filename

`stat` is where BIA's depositor metadata surfaces. Filenames like `056F63395C` say
nothing; `bia.attributes` says the organism, the channel and the label. Combined with
`ls --json`, that is a filter:

```console
$ archivefs ls -R --json bia://S-BIAD599/20200220-ftp/experimentA/056F63395C \
  | python -c '
import json,sys
for e in json.load(sys.stdin):
    a = (e.get("bia") or {}).get("attributes", {})
    if "lyn-EGFP" in a.get("Experimental_Condition__Fluorescent_Labels_", ""):
        print(e["name"])'
S-BIAD599/20200220-ftp/experimentA/056F63395C/056F63395C_8bit_lynEGFP.tif
S-BIAD599/20200220-ftp/experimentA/056F63395C/056F63395C_8bit_lynEGFP_seg.tif
```

**`-R` is load-bearing here.** `ls` is children-only, and the children of a BIA
experiment directory are *directories* — one per image — which carry no
`bia.attributes`. Without `-R` the filter silently matches nothing, which looks
identical to "no image uses that label". Scope the recursion to one sample or one
experiment rather than the whole study: `-R` calls `info()` per file, and `S-BIAD599`
has 584 of them ([`cli-ls.md`](cli-ls.md)).

## Scenario: will this import be download-free?

```console
$ archivefs stat zenodo://1486184/github-mark.zip | grep -E '"md5"|checksum_source'
  "md5": "49f6a914ea33912471512ac9228c6923",
```

An `md5` here means `dvc import-url --no-download` records a checksum without
fetching bytes. A `null` means DVC will stream the whole file to compute one —
correct, but not free ([`decisions.md`](decisions.md) D14). So `stat` on one
representative file tells you what a 500-file import is going to cost.

## Scenario: which view am I actually looking at?

```console
$ archivefs stat -o view=derived bia://S-BIAD599/01776a3a-…/01776a3a-…-thumbnail-128-128.png \
  | grep -E '"md5"|"view"'
  "md5": "a84906a7545b1d47386e7d72ee807c41",
    "view": "derived",
```

Same accession, different bytes, and this time a real MD5 from the bucket's ETag. The
`view` field is there so a confusing result is one command away from an explanation.

## Exit status

| code | when |
|---|---|
| 0 | printed |
| 1 | bad usage — malformed URL, unknown protocol, `-o` without `=` |
| 2 | archive-level failure |

The not-found message lists what the record *does* hold, which is usually the typo:

```console
$ archivefs stat zenodo://1486184/nope.zip
ArchiveNotFoundError: 'nope.zip' is not in 1486184. It holds: github-mark.zip
```

## See also

[`cli-ls.md`](cli-ls.md) · [`cli-get.md`](cli-get.md) ·
[`using-with-dvc.md`](using-with-dvc.md) for how `md5` becomes a `.dvc` file
